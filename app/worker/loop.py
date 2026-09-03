"""Worker process: poll for due jobs, run them, repeat.

Run as many of these as you like - the claim in JobRepository.claim_due is
what keeps two workers off the same job.
"""

import logging
import os
import signal
import socket
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from app.core.clock import utcnow
from app.core.config import get_settings
from app.core.db import init_db, session_scope
from app.core.logging import setup_logging
from app.services.runner import JobRunner, LeaseLost

log = logging.getLogger(__name__)


def build_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class Worker:
    def __init__(self, worker_id: str | None = None):
        self.settings = get_settings()
        self.worker_id = worker_id or build_worker_id()
        self.stop_event = threading.Event()
        self._last_reap = 0.0


    def install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.info("received signal %s, finishing in-flight jobs", signum)
            self.stop_event.set()

        signal.signal(signal.SIGINT, handle)
        signal.signal(signal.SIGTERM, handle)


    def _execute(self, job_id) -> None:
        """One job, one session, one thread."""
        with session_scope() as session:
            runner = JobRunner(session, self.worker_id, self.settings)
            job = runner.jobs.get(job_id)
            if job is None:
                return
            try:
                runner.run(job)
            except LeaseLost:
                pass 

    def _reap(self) -> None:
        with session_scope() as session:
            runner = JobRunner(session, self.worker_id, self.settings)
            recovered = runner.reap_expired_leases()
        if recovered:
            log.info("requeued %s job(s) abandoned by dead workers", recovered)

    def run_once(self, pool: ThreadPoolExecutor, in_flight: set[Future]) -> int:
        """One poll cycle. Returns how many jobs were picked up."""
        free_slots = self.settings.worker_concurrency - len(in_flight)
        if free_slots <= 0:
            return 0

        with session_scope() as session:
            runner = JobRunner(session, self.worker_id, self.settings)
            jobs = runner.claim(limit=min(self.settings.batch_size, free_slots))
            job_ids = [job.id for job in jobs]

        for job_id in job_ids:
            future = pool.submit(self._execute, job_id)
            in_flight.add(future)
            future.add_done_callback(in_flight.discard)

        return len(job_ids)

    def run_forever(self) -> None:
        setup_logging()
        init_db()
        self.install_signal_handlers()
        log.info(
            "worker %s starting (concurrency=%s, poll=%ss, lease=%ss)",
            self.worker_id,
            self.settings.worker_concurrency,
            self.settings.poll_interval_seconds,
            self.settings.lease_seconds,
        )

        in_flight: set[Future] = set()
        with ThreadPoolExecutor(
            max_workers=self.settings.worker_concurrency,
            thread_name_prefix="job",
        ) as pool:
            while not self.stop_event.is_set():
                try:
                    self._maybe_reap()
                    picked = self.run_once(pool, in_flight)
                except Exception:
                    log.exception("poll cycle failed")
                    picked = 0

                wait = 0.05 if picked else self.settings.poll_interval_seconds
                self.stop_event.wait(wait)

            log.info("draining %s in-flight job(s)", len(in_flight))

        log.info("worker %s stopped", self.worker_id)

    def _maybe_reap(self) -> None:
        now = utcnow().timestamp()
        if now - self._last_reap >= self.settings.reaper_interval_seconds:
            self._last_reap = now
            self._reap()


def main() -> None:
    Worker().run_forever()


if __name__ == "__main__":
    main()
