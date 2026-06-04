"""Eastern time helpers — used for all display times and scheduling."""

from datetime import datetime, timezone, timedelta


def _et_offset() -> timedelta:
    """Return current UTC offset for US/Eastern — EDT (UTC-4) or EST (UTC-5)."""
    # DST starts second Sunday in March, ends first Sunday in November
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year

    # Second Sunday in March
    march1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)  # 2nd Sunday

    # First Sunday in November
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)  # 1st Sunday

    # DST transitions happen at 2 AM local — approximate as 7 AM UTC
    dst_start = dst_start.replace(hour=7)
    dst_end   = dst_end.replace(hour=6)

    if dst_start <= now_utc < dst_end:
        return timedelta(hours=-4)   # EDT
    return timedelta(hours=-5)       # EST


def now_et() -> datetime:
    """Current datetime in Eastern time (timezone-aware)."""
    offset = _et_offset()
    et_tz  = timezone(offset)
    return datetime.now(et_tz)


def utc_to_et(dt: datetime) -> datetime:
    """Convert a UTC datetime to Eastern time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    offset = _et_offset()
    return dt.astimezone(timezone(offset))


def format_et(dt: datetime = None, fmt: str = "%I:%M %p ET") -> str:
    """Format a datetime (or now) as Eastern time string."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return utc_to_et(dt).strftime(fmt)


def et_label() -> str:
    """'EDT' or 'EST' based on current DST state."""
    return "EDT" if _et_offset().total_seconds() == -4 * 3600 else "EST"
