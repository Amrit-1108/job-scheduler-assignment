from datetime import timedelta

import pytest

from app.core.clock import utcnow
from app.domain.enums import ExecutionStatus, JobStatus, ScheduleType
from app.domain.models import Job
from app.services.runner import JobRunner


def make_job(session, **overrides) -> Job:
    defaults = dict(
        name="test-job",
        payload={"task": "simulated", "fail": False, "duration_seconds": 0},
        schedule_type=ScheduleType.ONE_TIME.value,
        run_at=utcnow() + timedelta(minutes=5),
        max_retries=2,
        status=JobStatus.SCHEDULED.value,
        next_run_at=utcnow() - timedelta(seconds=1), 
    )
    defaults.update(overrides)
    job = Job(**defaults)
    session.add(job)
    session.commit()
    return job


@pytest.fixture
def runner(session) -> JobRunner:
    return JobRunner(session, worker_id="worker-under-test")


def test_successful_one_time_job_completes(session, runner):
    make_job(session)

    claimed = runner.claim()
    assert len(claimed) == 1
    assert claimed[0].status == JobStatus.RUNNING.value

    assert runner.run(claimed[0]) is ExecutionStatus.SUCCESS

    job = runner.jobs.get(claimed[0].id)
    session.refresh(job)
    assert job.status == JobStatus.COMPLETED.value
    assert job.next_run_at is None
    assert job.locked_by is None

    execution = runner.executions.last_for_job(job.id)
    assert execution.status == ExecutionStatus.SUCCESS.value
    assert execution.attempt_number == 1
    assert execution.finished_at is not None


def test_failure_is_retried_with_backoff(session, runner):
    make_job(session, payload={"task": "simulated", "fail": True, "duration_seconds": 0})

    job = runner.claim()[0]
    assert runner.run(job) is ExecutionStatus.FAILED

    session.refresh(job)
    assert job.status == JobStatus.SCHEDULED.value 
    assert job.attempt_count == 1
    assert job.next_run_at > utcnow()  
    assert "Forced failure" in job.last_error


def test_job_fails_for_good_once_retries_run_out(session, runner):
    make_job(
        session,
        max_retries=1,
        payload={"task": "simulated", "fail": True, "duration_seconds": 0},
    )

    for _ in range(2):
        claimed = runner.claim()
        if not claimed:  
            job = session.query(Job).one()
            job.next_run_at = utcnow() - timedelta(seconds=1)
            session.commit()
            claimed = runner.claim()
        runner.run(claimed[0])

    job = session.query(Job).one()
    session.refresh(job)
    assert job.status == JobStatus.FAILED.value
    assert job.attempt_count == 2
    assert job.next_run_at is None
    assert len(runner.executions.list_for_job(job.id)) == 2


def test_interval_job_reschedules_itself(session, runner):
    make_job(
        session,
        schedule_type=ScheduleType.INTERVAL.value,
        run_at=None,
        interval_seconds=60,
    )

    job = runner.claim()[0]
    previous_due = job.next_run_at
    runner.run(job)

    session.refresh(job)
    assert job.status == JobStatus.SCHEDULED.value
    assert job.next_run_at == previous_due + timedelta(seconds=60)
    assert job.attempt_count == 0  
    assert job.total_runs == 1


def test_recurring_job_is_dead_lettered_when_it_keeps_failing(session, runner):
    make_job(
        session,
        schedule_type=ScheduleType.INTERVAL.value,
        run_at=None,
        interval_seconds=60,
        max_retries=0,
        payload={"task": "simulated", "fail": True, "duration_seconds": 0},
    )

    job = runner.claim()[0]
    runner.run(job)

    session.refresh(job)
    assert job.status == JobStatus.DEAD_LETTER.value
    assert job.next_run_at is None


def test_expired_lease_is_reclaimed(session, runner):
    """Simulates a worker that was killed mid-execution."""
    make_job(session)

    job = runner.claim()[0]
    runner.executions.start(
        job_id=job.id, attempt_number=1, worker_id=runner.worker_id, now=utcnow()
    )
    job.attempt_count = 1
    job.lease_expires_at = utcnow() - timedelta(seconds=1)  
    session.commit()

    assert runner.reap_expired_leases() == 1

    session.refresh(job)
    assert job.status == JobStatus.SCHEDULED.value 
    assert job.locked_by is None
    execution = runner.executions.last_for_job(job.id)
    assert execution.status == ExecutionStatus.FAILED.value
    assert "lease expired" in execution.error_message


def test_reaper_leaves_healthy_leases_alone(session, runner):
    make_job(session)
    runner.claim()

    assert runner.reap_expired_leases() == 0


def test_jobs_scheduled_in_the_future_are_not_picked_up(session, runner):
    make_job(session, next_run_at=utcnow() + timedelta(minutes=10))
    assert runner.claim() == []


def test_paused_jobs_are_never_claimed(session, runner):
    make_job(session, status=JobStatus.PAUSED.value)
    assert runner.claim() == []


def test_long_task_keeps_its_lease_alive(session, runner):
    """The bug this test exists for: a task slower than LEASE_SECONDS used to
    get reclaimed by the reaper and run a second time."""
    runner.settings = runner.settings.model_copy(update={"lease_seconds": 3})
    make_job(session, payload={"task": "simulated", "fail": False, "duration_seconds": 5})

    job = runner.claim()[0]
    original_lease = job.lease_expires_at

    assert runner.run(job) is ExecutionStatus.SUCCESS

    executions = runner.executions.list_for_job(job.id)
    assert len(executions) == 1 
    assert original_lease is not None
