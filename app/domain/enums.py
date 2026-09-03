from enum import Enum


class JobStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    DEAD_LETTER = "DEAD_LETTER"


class ScheduleType(str, Enum):
    ONE_TIME = "one_time"
    INTERVAL = "interval"
    CRON = "cron"


class ExecutionStatus(str, Enum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
