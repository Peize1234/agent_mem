"""Shared timestamp helpers for memory persistence."""

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def beijing_now() -> datetime:
    """Return the current timezone-aware Beijing time."""
    return datetime.now(BEIJING_TIMEZONE)


def beijing_now_iso() -> str:
    """Return the current Beijing time as an ISO 8601 string."""
    return beijing_now().isoformat()


def normalize_iso_timestamp_to_beijing(value: Optional[Union[str, datetime]]) -> Optional[str]:
    """Normalize an ISO timestamp to Beijing time; treat naive values as Beijing-local."""
    if value is None or value == "":
        return value

    if isinstance(value, datetime):
        parsed = value
    else:
        timestamp = str(value)
        normalized = f"{timestamp[:-1]}+00:00" if timestamp.endswith(("Z", "z")) else timestamp
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return timestamp

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    return parsed.astimezone(BEIJING_TIMEZONE).isoformat()
