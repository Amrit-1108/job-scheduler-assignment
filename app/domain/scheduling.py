"""Pure scheduling rules.

No database, no I/O - just the arithmetic. Keeping it here means the tricky
parts (backoff, interval catch-up, cron) can be unit tested on their own.
"""

from datetime import datetime, timedelta

from croniter import croniter

from app.domain.enums import ScheduleType
from app.domain.errors import ValidationError


def validate_cron(expression: str) -> None:
    if not croniter.is_valid(expression):
        raise ValidationError(f"'{expression}' is not a valid cron expression")


def first_run_at(
    schedule_type: str,
    run_at: datetime | None,
    interval_seconds: int | None,
    cron_expression: str | None,
    now: datetime,
) -> datetime:
    """When should this job fire for the first time?"""
    if schedule_type == ScheduleType.ONE_TIME.value:
        if run_at is None:
            raise ValidationError("run_at is required for one_time jobs")
        return run_at

    if schedule_type == ScheduleType.INTERVAL.value:
        if not interval_seconds or interval_seconds <= 0:
            raise ValidationError("interval_seconds must be greater than 0")
        return run_at or now + timedelta(seconds=interval_seconds)

    if schedule_type == ScheduleType.CRON.value:
        if not cron_expression:
            raise ValidationError("cron_expression is required for cron jobs")
        validate_cron(cron_expression)
        return croniter(cron_expression, run_at or now).get_next(datetime)

    raise ValidationError(f"Unsupported schedule_type '{schedule_type}'")


def next_run_after_success(job, now: datetime) -> datetime | None:
    """Next fire time for a recurring job, or None if the job is finished."""
    if job.schedule_type == ScheduleType.INTERVAL.value:
        nxt = (job.next_run_at or now) + timedelta(seconds=job.interval_seconds)
        if nxt <= now:
            nxt = now + timedelta(seconds=job.interval_seconds)
        return nxt

    if job.schedule_type == ScheduleType.CRON.value:
        return croniter(job.cron_expression, now).get_next(datetime)

    return None  


def retry_delay_seconds(attempt: int, base: int, cap: int) -> int:
    """Exponential backoff, 1-indexed on attempt, capped so it stays sane."""
    if attempt < 1:
        attempt = 1
    return min(base * (2 ** (attempt - 1)), cap)


def next_retry_at(attempt: int, now: datetime, base: int, cap: int) -> datetime:
    return now + timedelta(seconds=retry_delay_seconds(attempt, base, cap))
