"""The part the spec actually cares about: two workers, one job, one winner."""

from datetime import timedelta

from app.core.clock import utcnow
from app.core.db import SessionLocal
from app.domain.enums import JobStatus, ScheduleType
from app.domain.models import Job
from app.services.runner import JobRunner


def seed(session, count: int) -> list[Job]:
    jobs = [
        Job(
            name=f"job-{i}",
            payload={"task": "noop"},
            schedule_type=ScheduleType.ONE_TIME.value,
            run_at=utcnow() + timedelta(minutes=1),
            max_retries=0,
            status=JobStatus.SCHEDULED.value,
            next_run_at=utcnow() - timedelta(seconds=1),
        )
        for i in range(count)
    ]
    session.add_all(jobs)
    session.commit()
    return jobs


def test_two_workers_never_claim_the_same_job(session):
    seed(session, 1)

    a_session, b_session = SessionLocal(), SessionLocal()
    try:
        a = JobRunner(a_session, "worker-a").claim()
        b = JobRunner(b_session, "worker-b").claim()
    finally:
        a_session.close()
        b_session.close()

    assert len(a) + len(b) == 1


def test_workers_split_the_queue_without_overlap(session):
    seed(session, 6)

    sessions = [SessionLocal() for _ in range(3)]
    try:
        claimed = []
        for i, s in enumerate(sessions):
            claimed.extend(JobRunner(s, f"worker-{i}").claim(limit=2))
    finally:
        for s in sessions:
            s.close()

    ids = [job.id for job in claimed]
    assert len(ids) == 6
    assert len(set(ids)) == 6 


def test_a_stale_worker_cannot_overwrite_a_reclaimed_job(session):
    """Worker A stalls, the reaper requeues its job, then A comes back.

    A's result must be discarded - otherwise the job's state would jump
    backwards and we could end up executing it twice.
    """
    job = seed(session, 1)[0]

    a_session = SessionLocal()
    try:
        runner_a = JobRunner(a_session, "worker-a")
        claimed = runner_a.claim()[0]

        claimed_row = session.get(Job, claimed.id)
        session.refresh(claimed_row)
        claimed_row.lease_expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
        JobRunner(session, "reaper").reap_expired_leases()

        applied = runner_a._commit_outcome(
            claimed,
            claimed_version=claimed.version,
            changes={"status": JobStatus.COMPLETED.value, "next_run_at": None,
                     "attempt_count": 0, "last_error": None},
        )
        a_session.commit()
    finally:
        a_session.close()

    assert applied is False

    session.refresh(job)
    assert job.status == JobStatus.SCHEDULED.value
    assert job.locked_by is None
