from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC on the way out, so API responses carry a proper offset."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc)
