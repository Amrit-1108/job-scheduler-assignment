import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.enums import ExecutionStatus
from app.domain.models import JobExecution


class ExecutionRepository:
    def __init__(self, session: Session):
        self.session = session

    def start(
        self, *, job_id: uuid.UUID, attempt_number: int, worker_id: str, now: datetime
    ) -> JobExecution:
        execution = JobExecution(
            job_id=job_id,
            attempt_number=attempt_number,
            started_at=now,
            status=ExecutionStatus.RUNNING.value,
            worker_id=worker_id,
        )
        self.session.add(execution)
        self.session.flush()
        return execution

    def finish(
        self,
        execution: JobExecution,
        *,
        status: ExecutionStatus,
        now: datetime,
        error_message: str | None = None,
    ) -> JobExecution:
        execution.status = status.value
        execution.finished_at = now
        execution.error_message = error_message
        self.session.flush()
        return execution

    def last_for_job(self, job_id: uuid.UUID) -> JobExecution | None:
        return self.session.scalar(
            select(JobExecution)
            .where(JobExecution.job_id == job_id)
            .order_by(JobExecution.started_at.desc(), JobExecution.attempt_number.desc())
            .limit(1)
        )

    def open_for_job(self, job_id: uuid.UUID) -> list[JobExecution]:
        """Executions still marked RUNNING, i.e. abandoned by a dead worker."""
        return list(
            self.session.scalars(
                select(JobExecution).where(
                    JobExecution.job_id == job_id,
                    JobExecution.status == ExecutionStatus.RUNNING.value,
                )
            ).all()
        )

    def list_for_job(
        self, job_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[JobExecution]:
        return list(
            self.session.scalars(
                select(JobExecution)
                .where(JobExecution.job_id == job_id)
                .order_by(JobExecution.started_at.desc())
                .limit(limit)
                .offset(offset)
            ).all()
        )
