import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.clock import as_utc
from app.domain.enums import ExecutionStatus, JobStatus, ScheduleType


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=80, examples=["ashish"])
    password: str = Field(min_length=8, max_length=72, examples=["supersecret123"])


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("created_at", mode="after")
    @classmethod
    def _utc(cls, value):
        return as_utc(value)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200, examples=["send-daily-digest"])
    payload: dict[str, Any] = Field(default_factory=dict)
    schedule_type: ScheduleType
    run_at: datetime | None = Field(
        default=None,
        description="Required for one_time jobs; optional start time for recurring ones.",
    )
    interval_seconds: int | None = Field(default=None, gt=0)
    cron_expression: str | None = Field(default=None, max_length=120)
    max_retries: int = Field(default=3, ge=0, le=20)
    idempotency_key: str | None = Field(
        default=None,
        max_length=120,
        description="Send the same key to make retries of this POST safe.",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "send-invoice-email",
                    "payload": {"task": "simulated", "invoice_id": 42},
                    "schedule_type": "one_time",
                    "run_at": "2030-01-01T10:00:00Z",
                    "max_retries": 3,
                },
                {
                    "name": "sync-inventory",
                    "payload": {"task": "simulated"},
                    "schedule_type": "interval",
                    "interval_seconds": 60,
                    "max_retries": 2,
                },
            ]
        }
    )

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class ExecutionOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    attempt_number: int
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime | None
    duration_seconds: float | None
    error_message: str | None
    worker_id: str | None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def _utc(cls, value):
        return as_utc(value)


class JobOut(BaseModel):
    id: uuid.UUID
    name: str
    payload: dict[str, Any]
    schedule_type: ScheduleType
    status: JobStatus
    run_at: datetime | None
    interval_seconds: int | None
    cron_expression: str | None
    max_retries: int
    attempt_count: int = Field(description="Attempts used for the current occurrence.")
    retries_left: int
    total_runs: int
    next_run_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("run_at", "next_run_at", "created_at", "updated_at", mode="after")
    @classmethod
    def _utc(cls, value):
        return as_utc(value)


class JobDetail(JobOut):
    last_execution: ExecutionOut | None = None
    recent_executions: list[ExecutionOut] = Field(default_factory=list)


class JobListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


class StatsResponse(BaseModel):
    counts_by_status: dict[str, int]


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    database: bool


class ErrorResponse(BaseModel):
    code: str
    message: str
