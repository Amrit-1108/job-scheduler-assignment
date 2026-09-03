import uuid
from datetime import datetime, timedelta

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from app.domain.enums import JobStatus
from app.domain.models import Job


class JobRepository:
    """All SQL that touches the jobs table lives here."""

    def __init__(self, session: Session):
        self.session = session


    def get(self, job_id: uuid.UUID) -> Job | None:
        return self.session.get(Job, job_id)

    def get_by_idempotency_key(self, key: str, owner_id: uuid.UUID | None) -> Job | None:
        return self.session.scalar(
            select(Job).where(Job.idempotency_key == key, Job.owner_id == owner_id)
        )

    def _filtered(
        self,
        owner_id: uuid.UUID | None,
        status: str | None,
        schedule_type: str | None,
        next_run_before: datetime | None,
        next_run_after: datetime | None,
    ) -> Select:
        stmt = select(Job)
        if owner_id is not None:
            stmt = stmt.where(Job.owner_id == owner_id)
        if status:
            stmt = stmt.where(Job.status == status)
        if schedule_type:
            stmt = stmt.where(Job.schedule_type == schedule_type)
        if next_run_before:
            stmt = stmt.where(Job.next_run_at <= next_run_before)
        if next_run_after:
            stmt = stmt.where(Job.next_run_at >= next_run_after)
        return stmt

    def list_jobs(
        self,
        *,
        owner_id: uuid.UUID | None = None,
        status: str | None = None,
        schedule_type: str | None = None,
        next_run_before: datetime | None = None,
        next_run_after: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Job], int]:
        stmt = self._filtered(
            owner_id, status, schedule_type, next_run_before, next_run_after
        )
        total = self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = self.session.scalars(
            stmt.order_by(Job.next_run_at.asc(), Job.created_at.asc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), int(total or 0)


    def add(self, job: Job) -> Job:
        self.session.add(job)
        self.session.flush()
        return job

    def claim_due(
        self,
        *,
        worker_id: str,
        now: datetime,
        limit: int,
        lease_seconds: int,
    ) -> list[Job]:
        """Atomically move up to `limit` due jobs into RUNNING for this worker.

        Two guards, deliberately belt-and-braces:

        1. SELECT ... FOR UPDATE SKIP LOCKED, so parallel workers walk past
           rows somebody else is already claiming instead of blocking on them.
        2. A conditional UPDATE with `status = SCHEDULED AND version = <seen>`
           in the WHERE clause. If another worker won the race anyway, the
           rowcount comes back 0 and we drop the job.

        The second check makes the claim correct even if the first is weakened -
        a lower isolation level, a connection pooler in the middle, or a future
        move to a backend without SKIP LOCKED.
        """
        candidates = self.session.execute(
            select(Job.id, Job.version)
            .where(Job.status == JobStatus.SCHEDULED.value, Job.next_run_at <= now)
            .order_by(Job.next_run_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()

        claimed: list[uuid.UUID] = []
        lease_until = now + timedelta(seconds=lease_seconds)

        for job_id, version in candidates:
            result = self.session.execute(
                update(Job)
                .where(
                    Job.id == job_id,
                    Job.status == JobStatus.SCHEDULED.value,
                    Job.version == version,
                )
                .values(
                    status=JobStatus.RUNNING.value,
                    locked_by=worker_id,
                    lease_expires_at=lease_until,
                    version=Job.version + 1,
                    updated_at=now,
                )
            )
            if result.rowcount == 1:
                claimed.append(job_id)

        self.session.commit()

        if not claimed:
            return []
        return list(self.session.scalars(select(Job).where(Job.id.in_(claimed))).all())

    def find_expired_leases(self, now: datetime, limit: int = 50) -> list[Job]:
        return list(
            self.session.scalars(
                select(Job)
                .where(
                    Job.status == JobStatus.RUNNING.value,
                    Job.lease_expires_at.is_not(None),
                    Job.lease_expires_at < now,
                )
                .order_by(Job.lease_expires_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
        )

    def count_by_status(self, owner_id: uuid.UUID | None = None) -> dict[str, int]:
        stmt = select(Job.status, func.count()).group_by(Job.status)
        if owner_id is not None:
            stmt = stmt.where(Job.owner_id == owner_id)
        rows = self.session.execute(stmt).all()
        return {status: int(count) for status, count in rows}
