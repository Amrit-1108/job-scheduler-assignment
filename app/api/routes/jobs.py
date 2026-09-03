import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response, status

from app.api.deps import get_job_service
from app.api.schemas import (
    ErrorResponse,
    ExecutionOut,
    JobCreate,
    JobDetail,
    JobListResponse,
    JobOut,
    StatsResponse,
)
from app.core.clock import to_utc_naive
from app.domain.enums import JobStatus, ScheduleType
from app.services.job_service import JobService, NewJob

router = APIRouter(prefix="/jobs", tags=["jobs"])

COMMON_ERRORS = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


@router.post(
    "",
    response_model=JobOut,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule a job",
    responses=COMMON_ERRORS,
)
def create_job(
    body: JobCreate,
    response: Response,
    service: JobService = Depends(get_job_service),
):
    """Create a one-off, interval or cron job.

    Pass the same `idempotency_key` when retrying a request that may have
    already been applied - the original job comes back with 200 instead of a
    duplicate being created.
    """
    job, created = service.create(
        NewJob(
            name=body.name,
            payload=body.payload,
            schedule_type=body.schedule_type.value,
            run_at=body.run_at,
            interval_seconds=body.interval_seconds,
            cron_expression=body.cron_expression,
            max_retries=body.max_retries,
            idempotency_key=body.idempotency_key,
        )
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return job


@router.get("", response_model=JobListResponse, summary="List jobs")
def list_jobs(
    job_status: JobStatus | None = Query(default=None, alias="status"),
    schedule_type: ScheduleType | None = Query(default=None),
    next_run_before: datetime | None = Query(
        default=None, description="Only jobs due at or before this time."
    ),
    next_run_after: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: JobService = Depends(get_job_service),
):
    items, total = service.list(
        status=job_status.value if job_status else None,
        schedule_type=schedule_type.value if schedule_type else None,
        next_run_before=to_utc_naive(next_run_before) if next_run_before else None,
        next_run_after=to_utc_naive(next_run_after) if next_run_after else None,
        limit=limit,
        offset=offset,
    )
    return JobListResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/stats", response_model=StatsResponse, summary="Job counts by status")
def job_stats(service: JobService = Depends(get_job_service)):
    return StatsResponse(counts_by_status=service.stats())


@router.get(
    "/{job_id}",
    response_model=JobDetail,
    summary="Job status, last execution and next run",
    responses=COMMON_ERRORS,
)
def get_job(job_id: uuid.UUID, service: JobService = Depends(get_job_service)):
    job, last, recent = service.get_with_history(job_id)
    detail = JobDetail.model_validate(job)
    detail.last_execution = ExecutionOut.model_validate(last) if last else None
    detail.recent_executions = [ExecutionOut.model_validate(e) for e in recent]
    return detail


@router.get(
    "/{job_id}/executions",
    response_model=list[ExecutionOut],
    summary="Execution history",
    responses=COMMON_ERRORS,
)
def job_executions(
    job_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: JobService = Depends(get_job_service),
):
    service.get(job_id) 
    return service.executions.list_for_job(job_id, limit=limit, offset=offset)


@router.post("/{job_id}/pause", response_model=JobOut, responses=COMMON_ERRORS)
def pause_job(job_id: uuid.UUID, service: JobService = Depends(get_job_service)):
    return service.pause(job_id)


@router.post("/{job_id}/resume", response_model=JobOut, responses=COMMON_ERRORS)
def resume_job(job_id: uuid.UUID, service: JobService = Depends(get_job_service)):
    return service.resume(job_id)


@router.post("/{job_id}/cancel", response_model=JobOut, responses=COMMON_ERRORS)
def cancel_job(job_id: uuid.UUID, service: JobService = Depends(get_job_service)):
    return service.cancel(job_id)


@router.post(
    "/{job_id}/replay",
    response_model=JobOut,
    summary="Requeue a failed or dead-lettered job",
    responses=COMMON_ERRORS,
)
def replay_job(job_id: uuid.UUID, service: JobService = Depends(get_job_service)):
    return service.replay(job_id)
