import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import to_utc_naive, utcnow
from app.domain.enums import JobStatus, ScheduleType
from app.domain.errors import DuplicateJob, IllegalTransition, JobNotFound, ValidationError
from app.domain.models import Job, JobExecution
from app.domain.scheduling import first_run_at, validate_cron
from app.domain.state_machine import ensure_transition
from app.repositories.executions import ExecutionRepository
from app.repositories.jobs import JobRepository

log = logging.getLogger(__name__)


@dataclass
class NewJob:
    """What the API layer hands us. Deliberately not a Pydantic model - the
    service shouldn't care whether the request arrived over HTTP."""

    name: str
    payload: dict
    schedule_type: str
    run_at: datetime | None = None
    interval_seconds: int | None = None
    cron_expression: str | None = None
    max_retries: int = 3
    idempotency_key: str | None = None


class JobService:
    """Job use cases, scoped to one owner.

    `owner_id` is passed in by the API dependency rather than by each route, so
    there is no path through this class that can reach another user's jobs.
    The worker constructs it without an owner - the engine executes everyone's
    jobs.
    """

    def __init__(self, session: Session, owner_id: uuid.UUID | None = None):
        self.session = session
        self.owner_id = owner_id
        self.jobs = JobRepository(session)
        self.executions = ExecutionRepository(session)


    def create(self, data: NewJob, now: datetime | None = None) -> tuple[Job, bool]:
        """Create a job. Returns (job, created) - `created` is False when an
        existing job was returned for a repeated idempotency key."""
        now = now or utcnow()

        if data.idempotency_key:
            existing = self.jobs.get_by_idempotency_key(data.idempotency_key, self.owner_id)
            if existing is not None:
                return existing, False

        self._validate(data, now)

        run_at = to_utc_naive(data.run_at) if data.run_at else None
        next_run = first_run_at(
            data.schedule_type, run_at, data.interval_seconds, data.cron_expression, now
        )

        job = Job(
            owner_id=self.owner_id,
            name=data.name.strip(),
            payload=data.payload or {},
            schedule_type=data.schedule_type,
            run_at=run_at,
            interval_seconds=data.interval_seconds,
            cron_expression=data.cron_expression,
            max_retries=data.max_retries,
            status=JobStatus.SCHEDULED.value,
            next_run_at=next_run,
            idempotency_key=data.idempotency_key,
            created_at=now,
            updated_at=now,
        )

        try:
            self.jobs.add(job)
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            if data.idempotency_key:
                existing = self.jobs.get_by_idempotency_key(data.idempotency_key, self.owner_id)
                if existing is not None:
                    return existing, False
            raise DuplicateJob("A job with this idempotency_key already exists")

        log.info("created job %s (%s) due at %s", job.id, job.schedule_type, next_run)
        return job, True

    def pause(self, job_id: uuid.UUID) -> Job:
        job = self._require(job_id)
        if job.status == JobStatus.RUNNING.value:
            raise IllegalTransition("Job is currently running; retry once it settles")
        ensure_transition(job.status, JobStatus.PAUSED.value)
        job.status = JobStatus.PAUSED.value
        job.version += 1
        self.session.commit()
        return job

    def resume(self, job_id: uuid.UUID) -> Job:
        job = self._require(job_id)
        ensure_transition(job.status, JobStatus.SCHEDULED.value)
        now = utcnow()
        job.status = JobStatus.SCHEDULED.value
        job.next_run_at = job.next_run_at or now
        job.locked_by = None
        job.lease_expires_at = None
        job.version += 1
        self.session.commit()
        return job

    def cancel(self, job_id: uuid.UUID) -> Job:
        job = self._require(job_id)
        ensure_transition(job.status, JobStatus.FAILED.value)
        job.status = JobStatus.FAILED.value
        job.next_run_at = None
        job.last_error = "Cancelled by user"
        job.version += 1
        self.session.commit()
        return job

    def replay(self, job_id: uuid.UUID) -> Job:
        """Put a FAILED / DEAD_LETTER job back in the queue with a fresh budget."""
        job = self._require(job_id)
        ensure_transition(job.status, JobStatus.SCHEDULED.value)
        job.status = JobStatus.SCHEDULED.value
        job.attempt_count = 0
        job.next_run_at = utcnow()
        job.last_error = None
        job.locked_by = None
        job.lease_expires_at = None
        job.version += 1
        self.session.commit()
        return job


    def get(self, job_id: uuid.UUID) -> Job:
        return self._require(job_id)

    def get_with_history(
        self, job_id: uuid.UUID
    ) -> tuple[Job, JobExecution | None, list[JobExecution]]:
        job = self._require(job_id)
        last = self.executions.last_for_job(job_id)
        recent = self.executions.list_for_job(job_id, limit=10)
        return job, last, recent

    def list(self, **filters) -> tuple[list[Job], int]:
        return self.jobs.list_jobs(owner_id=self.owner_id, **filters)

    def stats(self) -> dict[str, int]:
        return self.jobs.count_by_status(owner_id=self.owner_id)


    def _require(self, job_id: uuid.UUID) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise JobNotFound(job_id)
        if self.owner_id is not None and job.owner_id != self.owner_id:
            raise JobNotFound(job_id)
        return job

    @staticmethod
    def _validate(data: NewJob, now: datetime) -> None:
        """Reject combinations that are syntactically fine but make no sense."""
        if not data.name or not data.name.strip():
            raise ValidationError("name cannot be empty")
        if data.max_retries < 0:
            raise ValidationError("max_retries cannot be negative")

        run_at = to_utc_naive(data.run_at) if data.run_at else None
        if run_at is not None and run_at <= now:
            raise ValidationError("run_at must be in the future")

        if data.schedule_type == ScheduleType.ONE_TIME.value:
            if run_at is None:
                raise ValidationError("run_at is required for one_time jobs")
            if data.interval_seconds is not None:
                raise ValidationError("interval_seconds is not allowed for one_time jobs")
            if data.cron_expression:
                raise ValidationError("cron_expression is not allowed for one_time jobs")

        elif data.schedule_type == ScheduleType.INTERVAL.value:
            if data.interval_seconds is None:
                raise ValidationError("interval_seconds is required for interval jobs")
            if data.interval_seconds <= 0:
                raise ValidationError("interval_seconds must be greater than 0")
            if data.cron_expression:
                raise ValidationError("cron_expression is not allowed for interval jobs")

        elif data.schedule_type == ScheduleType.CRON.value:
            if not data.cron_expression:
                raise ValidationError("cron_expression is required for cron jobs")
            if data.interval_seconds is not None:
                raise ValidationError("interval_seconds is not allowed for cron jobs")
            validate_cron(data.cron_expression)

        else:
            raise ValidationError(f"Unsupported schedule_type '{data.schedule_type}'")
