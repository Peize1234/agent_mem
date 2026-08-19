import math
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from mem0.utils.timestamps import BEIJING_TIMEZONE, beijing_now_iso


def _timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TIMEZONE)
    return parsed


def memory_strength(valid_recall_count: int, reinforcement_gain: float) -> float:
    """Return deterministic reinforcement strength for a valid-recall count."""
    return 1.0 + float(reinforcement_gain) * math.log1p(max(int(valid_recall_count), 0))


def forgetting_factor(
    payload: Dict[str, Any],
    config,
    *,
    now: Optional[str] = None,
) -> float:
    """Compute retrieval-only retention without deleting or mutating the memory."""
    current = _timestamp(now or beijing_now_iso())
    anchor = _timestamp(payload.get("last_recall_at") or payload.get("created_at") or payload.get("updated_at"))
    if current is None or anchor is None:
        return 1.0

    elapsed_hours = max((current - anchor).total_seconds() / 3600.0, 0.0)
    strength = memory_strength(
        payload.get("valid_recall_count", 0) or 0,
        config.reinforcement_gain,
    )
    effective_half_life = float(config.retention_half_life_hours) * strength
    retention = 2.0 ** (-elapsed_hours / effective_half_life)
    return max(float(config.retention_floor), min(retention, 1.0))


def heat_modulations(
    heats_by_session: Dict[str, float],
    *,
    minimum: float,
    maximum: float,
) -> Dict[str, float]:
    """Min-max normalize only the sessions represented in one candidate pool."""
    if not heats_by_session:
        return {}
    values = list(heats_by_session.values())
    heat_min = min(values)
    heat_max = max(values)
    if len(values) == 1 or heat_max == heat_min:
        return {session_id: 1.0 for session_id in heats_by_session}
    width = heat_max - heat_min
    modulation_width = float(maximum) - float(minimum)
    return {
        session_id: float(minimum) + ((heat - heat_min) / width) * modulation_width
        for session_id, heat in heats_by_session.items()
    }


def unique_ids(values: Iterable[Any]) -> list[str]:
    """Normalize IDs while preserving first-seen order."""
    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        if value in (None, ""):
            continue
        value = str(value)
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized
