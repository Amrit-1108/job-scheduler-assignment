import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.clock import utcnow
from app.domain.enums import ExecutionStatus, JobStatus, ScheduleType


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    jobs: Mapped[list["Job"]] = relationship(back_populates="owner")

    def __repr__(self) -> str: 
        return f"<User {self.username}>"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    schedule_type: Mapped[str] = mapped_column(String(20), nullable=False)
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)

    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JobStatus.SCHEDULED.value
    )

    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_runs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


    locked_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    idempotency_key: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    owner: Mapped["User | None"] = relationship(back_populates="jobs")

    executions: Mapped[list["JobExecution"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobExecution.attempt_number",
    )

    __table_args__ = (
        Index("ix_jobs_status_next_run_at", "status", "next_run_at"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        UniqueConstraint("owner_id", "idempotency_key", name="uq_jobs_owner_idempotency"),
        CheckConstraint("max_retries >= 0", name="ck_jobs_max_retries_non_negative"),
        CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds > 0",
            name="ck_jobs_interval_positive",
        ),
    )

    @property
    def retries_left(self) -> int:
        return max(self.max_retries - self.attempt_count, 0)

    @property
    def is_recurring(self) -> bool:
        return self.schedule_type in (ScheduleType.INTERVAL.value, ScheduleType.CRON.value)

    def __repr__(self) -> str:
        return f"<Job {self.id} {self.name!r} {self.status}>"


class JobExecution(Base):
    __tablename__ = "job_executions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionStatus.RUNNING.value
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    job: Mapped[Job] = relationship(back_populates="executions")

    __table_args__ = (Index("ix_executions_job_started", "job_id", "started_at"),)

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()
