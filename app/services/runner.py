"""The execution engine.

Everything that moves a job between states while it is being worked on lives
here: claiming, running, retrying, and cleaning up after workers that died
mid-flight.
"""

import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.config import Settings, get_settings
from app.domain.enums import ExecutionStatus, JobStatus
from app.domain.models import Job
from app.domain.scheduling import next_retry_at, next_run_after_success
from app.domain.state_machine import ensure_transition
from app.repositories.executions import ExecutionRepository
from app.repositories.jobs import JobRepository
from app.services.heartbeat import LeaseHeartbeat
from app.services.tasks import TaskFailed, run_task

log = logging.getLogger(__name__)


class LeaseLost(Exception):
    """Our claim on the job was taken away while we were working on it."""


class JobRunner:
    def __init__(self, session: Session, worker_id: str, settings: Settings | None = None):
        self.session = session
        self.worker_id = worker_id
        self.settings = settings or get_settings()
        self.jobs = JobRepository(session)
        self.executions = ExecutionRepository(session)


    def claim(self, limit: int | None = None, now: datetime | None = None) -> list[Job]:
        return self.jobs.claim_due(
            worker_id=self.worker_id,
            now=now or utcnow(),
            limit=limit or self.settings.batch_size,
            lease_seconds=self.settings.lease_seconds,
        )


    def run(self, job: Job) -> ExecutionStatus:
        """Execute one already-claimed job and persist the outcome.

        The job must be RUNNING and leased to this worker - `claim()` is the
        only supported way to get here.
        """
        if job.status != JobStatus.RUNNING.value or job.locked_by != self.worker_id:
            raise LeaseLost(f"Job {job.id} is not leased to {self.worker_id}")

        attempt = job.attempt_count + 1
        started = utcnow()

        execution = self.executions.start(
            job_id=job.id, attempt_number=attempt, worker_id=self.worker_id, now=started
        )
        job.attempt_count = attempt
        job.total_runs += 1
        self.session.commit()

        claimed_version = job.version
        error: str | None = None

        with LeaseHeartbeat(job.id, self.worker_id, claimed_version, self.settings.lease_seconds):
            try:
                run_task(job.payload or {})
                outcome = ExecutionStatus.SUCCESS
            except TaskFailed as exc:
                outcome = ExecutionStatus.FAILED
                error = str(exc)
            except Exception as exc: 
                outcome = ExecutionStatus.FAILED
                error = f"{type(exc).__name__}: {exc}"
                log.exception("job %s raised an unexpected error", job.id)

        finished = utcnow()
        self.session.refresh(job)

        if outcome is ExecutionStatus.SUCCESS:
            changes = self._success_changes(job, finished)
        else:
            changes = self._failure_changes(job, error or "Unknown error", finished)

        applied = self._commit_outcome(job, claimed_version, changes)
        self.executions.finish(
            execution,
            status=outcome,
            now=finished,
            error_message=error if applied else (error or "") + " [lease lost; result discarded]",
        )
        self.session.commit()

        if not applied:
            log.warning("lost lease on job %s while running attempt %s", job.id, attempt)
            raise LeaseLost(str(job.id))

        log.info(
            "job %s attempt %s -> %s (job is now %s)",
            job.id,
            attempt,
            outcome.value,
            changes["status"],
        )
        return outcome


    def _success_changes(self, job: Job, now: datetime) -> dict:
        nxt = next_run_after_success(job, now)
        if nxt is None:
            status = JobStatus.COMPLETED.value
        else:
            status = JobStatus.SCHEDULED.value
        ensure_transition(JobStatus.RUNNING.value, status)
        return {
            "status": status,
            "next_run_at": nxt,
            "attempt_count": 0, 
            "last_error": None,
        }

    def _failure_changes(self, job: Job, error: str, now: datetime) -> dict:
        retries_exhausted = job.attempt_count > job.max_retries

        if not retries_exhausted:
            status = JobStatus.SCHEDULED.value
            nxt = next_retry_at(
                job.attempt_count,
                now,
                self.settings.retry_backoff_seconds,
                self.settings.retry_backoff_max_seconds,
            )
        else:
            if job.is_recurring and self.settings.dead_letter_enabled:
                status = JobStatus.DEAD_LETTER.value
            else:
                status = JobStatus.FAILED.value
            nxt = None

        ensure_transition(JobStatus.RUNNING.value, status)
        return {"status": status, "next_run_at": nxt, "last_error": error}

    def _commit_outcome(self, job: Job, claimed_version: int, changes: dict) -> bool:
        """Write the result back, but only if we still own the job.

        Guarding on (version, locked_by) means a reaper that already requeued
        this job wins, and a stale worker coming back to life can't resurrect
        an outdated result.
        """
        result = self.session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.version == claimed_version,
                Job.locked_by == self.worker_id,
                Job.status == JobStatus.RUNNING.value,
            )
            .values(
                locked_by=None,
                lease_expires_at=None,
                version=Job.version + 1,
                updated_at=utcnow(),
                **changes,
            )
        )
        return result.rowcount == 1


    def reap_expired_leases(self, now: datetime | None = None, limit: int = 50) -> int:
        """Recover jobs whose worker died (or whose container was restarted).

        A RUNNING job with an expired lease is treated as a failed attempt: the
        open execution row is closed out and the normal retry policy decides
        whether the job goes back on the queue or gives up.
        """
        now = now or utcnow()
        recovered = 0

        for job in self.jobs.find_expired_leases(now, limit=limit):
            for execution in self.executions.open_for_job(job.id):
                self.executions.finish(
                    execution,
                    status=ExecutionStatus.FAILED,
                    now=now,
                    error_message="Worker lease expired (crash or restart)",
                )

            changes = self._failure_changes(job, "Worker lease expired", now)
            for field, value in changes.items():
                setattr(job, field, value)
            job.locked_by = None
            job.lease_expires_at = None
            job.version += 1
            recovered += 1
            log.warning("reclaimed stuck job %s -> %s", job.id, job.status)

        if recovered:
            self.session.commit()
        return recovered

    def release(self, job: Job) -> None:
        """Hand a claimed-but-not-started job back (used on graceful shutdown)."""
        self.session.execute(
            update(Job)
            .where(
                Job.id == job.id,
                Job.locked_by == self.worker_id,
                Job.status == JobStatus.RUNNING.value,
            )
            .values(
                status=JobStatus.SCHEDULED.value,
                locked_by=None,
                lease_expires_at=None,
                version=Job.version + 1,
                updated_at=utcnow(),
            )
        )
        self.session.commit()
