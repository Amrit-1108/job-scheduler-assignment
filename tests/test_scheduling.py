from datetime import datetime, timedelta

import pytest

from app.domain.enums import ScheduleType
from app.domain.errors import ValidationError
from app.domain.scheduling import (
    first_run_at,
    next_retry_at,
    next_run_after_success,
    retry_delay_seconds,
)
from app.domain.models import Job

NOW = datetime(2030, 1, 1, 12, 0, 0)


def test_one_time_uses_the_requested_time():
    when = NOW + timedelta(hours=1)
    assert first_run_at(ScheduleType.ONE_TIME.value, when, None, None, NOW) == when


def test_one_time_needs_a_run_at():
    with pytest.raises(ValidationError):
        first_run_at(ScheduleType.ONE_TIME.value, None, None, None, NOW)


def test_interval_without_run_at_starts_one_interval_from_now():
    assert first_run_at(ScheduleType.INTERVAL.value, None, 60, None, NOW) == NOW + timedelta(
        seconds=60
    )


def test_cron_rejects_garbage():
    with pytest.raises(ValidationError):
        first_run_at(ScheduleType.CRON.value, None, None, "not a cron", NOW)


def test_cron_picks_the_next_slot():
    nxt = first_run_at(ScheduleType.CRON.value, None, None, "0 * * * *", NOW)
    assert nxt == datetime(2030, 1, 1, 13, 0)


def test_backoff_doubles_then_flattens_at_the_cap():
    assert [retry_delay_seconds(n, base=5, cap=60) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert retry_delay_seconds(10, base=5, cap=60) == 60


def test_next_retry_is_relative_to_now():
    assert next_retry_at(2, NOW, base=5, cap=300) == NOW + timedelta(seconds=10)


def test_interval_job_keeps_its_cadence():
    job = Job(
        schedule_type=ScheduleType.INTERVAL.value,
        interval_seconds=60,
        next_run_at=NOW,
    )
    assert next_run_after_success(job, NOW + timedelta(seconds=2)) == NOW + timedelta(
        seconds=60
    )


def test_interval_job_skips_missed_slots_after_downtime():
    job = Job(
        schedule_type=ScheduleType.INTERVAL.value,
        interval_seconds=60,
        next_run_at=NOW,
    )
    later = NOW + timedelta(hours=1)
    assert next_run_after_success(job, later) == later + timedelta(seconds=60)


def test_one_time_job_has_no_next_run():
    job = Job(schedule_type=ScheduleType.ONE_TIME.value, next_run_at=NOW)
    assert next_run_after_success(job, NOW) is None
