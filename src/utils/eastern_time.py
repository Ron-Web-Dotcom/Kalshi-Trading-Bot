"""Eastern time helpers — used for all display times and scheduling."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    """Current datetime in Eastern time (timezone-aware)."""
    return datetime.now(_ET)


def utc_to_et(dt: datetime) -> datetime:
    """Convert a UTC datetime to Eastern time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ET)


def format_et(dt: datetime = None, fmt: str = "%I:%M %p ET") -> str:
    """Format a datetime (or now) as Eastern time string."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return utc_to_et(dt).strftime(fmt)


def et_label() -> str:
    """'EDT' or 'EST' based on current DST state."""
    return now_et().strftime("%Z")
