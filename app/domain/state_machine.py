"""The one place that knows which status may follow which.

Every write path goes through `ensure_transition`, so an illegal move (say,
resuming a COMPLETED job) fails loudly instead of quietly corrupting state.
"""

from app.domain.enums import JobStatus
from app.domain.errors import IllegalTransition

ALLOWED: dict[JobStatus, set[JobStatus]] = {
    JobStatus.SCHEDULED: {
        JobStatus.RUNNING,
        JobStatus.PAUSED,
        JobStatus.FAILED,     
        JobStatus.COMPLETED,
    },
    JobStatus.RUNNING: {
        JobStatus.SCHEDULED,  
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTER,
    },
    JobStatus.PAUSED: {JobStatus.SCHEDULED, JobStatus.FAILED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.SCHEDULED},       
    JobStatus.DEAD_LETTER: {JobStatus.SCHEDULED},  
}


def can_transition(current: str, target: str) -> bool:
    return JobStatus(target) in ALLOWED[JobStatus(current)]


def ensure_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(f"Cannot move a job from {current} to {target}")
