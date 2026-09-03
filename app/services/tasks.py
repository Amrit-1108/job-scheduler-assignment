"""The "work" a job actually does.

The spec only asks for a simulated task (sleep 1-3s, fail ~30% of the time),
but real deployments always end up needing more than one kind of task, so the
lookup goes through a small registry keyed on `payload["task"]`. Anything
unregistered falls back to the simulation.
"""

import logging
import random
import time
from collections.abc import Callable

from app.core.config import get_settings

log = logging.getLogger(__name__)

TaskHandler = Callable[[dict], dict | None]
_REGISTRY: dict[str, TaskHandler] = {}


class TaskFailed(Exception):
    """Raised by a handler to signal a retryable failure."""


def register(name: str):
    def decorator(fn: TaskHandler) -> TaskHandler:
        _REGISTRY[name] = fn
        return fn

    return decorator


@register("simulated")
def simulated_task(payload: dict) -> dict:
    """Sleep for a bit, then fail with the configured probability."""
    settings = get_settings()

    duration = payload.get("duration_seconds")
    if duration is None:
        duration = random.uniform(settings.task_min_seconds, settings.task_max_seconds)
    time.sleep(float(duration))

    forced = payload.get("fail")
    if forced is True:
        raise TaskFailed("Forced failure via payload")
    if forced is False:
        return {"ok": True, "slept": duration}

    if random.random() < settings.failure_rate:
        raise TaskFailed("Simulated task failure")

    return {"ok": True, "slept": duration}


@register("noop")
def noop_task(payload: dict) -> dict:
    return {"ok": True}


def run_task(payload: dict) -> dict | None:
    handler = _REGISTRY.get((payload or {}).get("task", "simulated"), simulated_task)
    return handler(payload or {})
