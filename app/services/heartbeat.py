"""Keeps a claimed job's lease alive while the task is still running.

Without this, any task that outlives LEASE_SECONDS gets reclaimed by the
reaper and executed a second time - which is exactly the duplicate execution
the whole design is meant to prevent. The heartbeat runs on its own thread
with its own DB session, and deliberately does *not* touch `version`, so the
worker's final compare-and-swap still works the way it was written.
"""

import logging
import threading
import uuid
from datetime import timedelta

from sqlalchemy import update

from app.core.clock import utcnow
from app.core.db import SessionLocal
from app.domain.enums import JobStatus
from app.domain.models import Job

log = logging.getLogger(__name__)


class LeaseHeartbeat:
    def __init__(
        self,
        job_id: uuid.UUID,
        worker_id: str,
        claimed_version: int,
        lease_seconds: int,
    ):
        self.job_id = job_id
        self.worker_id = worker_id
        self.claimed_version = claimed_version
        self.lease_seconds = lease_seconds

        self.interval = max(lease_seconds / 3.0, 1.0)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost = False

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._loop, name=f"lease-{self.job_id.hex[:8]}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                if not self._renew():

                    self.lost = True
                    log.warning("lease on job %s is no longer ours", self.job_id)
                    return
            except Exception:

                log.exception("failed to renew lease for job %s", self.job_id)

    def _renew(self) -> bool:
        session = SessionLocal()
        try:
            result = session.execute(
                update(Job)
                .where(
                    Job.id == self.job_id,
                    Job.locked_by == self.worker_id,
                    Job.version == self.claimed_version,
                    Job.status == JobStatus.RUNNING.value,
                )
                .values(lease_expires_at=utcnow() + timedelta(seconds=self.lease_seconds))
            )
            session.commit()
            return result.rowcount == 1
        finally:
            session.close()
