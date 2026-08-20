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
    recall_count_key: str = "valid_recall_count",
    anchor_keys: tuple[str, ...] = ("last_recall_at", "created_at", "updated_at"),
    half_life_hours: Optional[float] = None,
    retention_floor: Optional[float] = None,
    reinforcement_gain: Optional[float] = None,
    current_turn_index: Optional[int] = None,
    anchor_index_keys: tuple[str, ...] = ("last_recall_turn_index", "page_sequence", "turn_index"),
    half_life_turns: Optional[float] = None,
    heat_factor: float = 1.0,
) -> float:
    """Compute retrieval-only retention without deleting or mutating the memory.

    Mid-term records use conversation distance. The explicit
    ``half_life_hours`` path is retained for cross-session long-term memory.
    """
    configured_turn_half_life = getattr(config, "retention_half_life_turns", None)
    if half_life_turns is not None or (half_life_hours is None and configured_turn_half_life is not None):
        current_index = current_turn_index
        if current_index is None:
            current_index = payload.get("current_turn_index")
        anchor_index = next((payload.get(key) for key in anchor_index_keys if payload.get(key) is not None), None)
        if current_index is None or anchor_index is None:
            # Legacy pages have no stable conversation index. Keeping them at
            # full retention avoids silently applying wall-clock decay.
            return 1.0
        try:
            distance_turns = max(float(current_index) - float(anchor_index), 0.0)
            base_half_life = float(configured_turn_half_life if half_life_turns is None else half_life_turns)
            heat_value = float(heat_factor)
            heat_minimum = float(getattr(config, "heat_modulation_min", heat_value))
            heat_maximum = float(getattr(config, "heat_modulation_max", heat_value))
        except (TypeError, ValueError):
            return 1.0
        if not math.isfinite(heat_value):
            return 1.0
        bounded_heat_factor = min(max(heat_value, heat_minimum), heat_maximum)
        if base_half_life <= 0 or bounded_heat_factor <= 0:
            return 1.0
        floor = float(config.retention_floor if retention_floor is None else retention_floor)
        effective_half_life = base_half_life * bounded_heat_factor
        retention = 2.0 ** (-distance_turns / effective_half_life)
        return max(floor, min(retention, 1.0))

    current = _timestamp(now or beijing_now_iso())
    anchor = _timestamp(next((payload.get(key) for key in anchor_keys if payload.get(key)), None))
    if current is None or anchor is None:
        return 1.0

    elapsed_hours = max((current - anchor).total_seconds() / 3600.0, 0.0)
    gain = float(getattr(config, "reinforcement_gain", 0.0) if reinforcement_gain is None else reinforcement_gain)
    strength = memory_strength(
        payload.get(recall_count_key, 0) or 0,
        gain,
    )
    base_half_life = float(config.retention_half_life_hours if half_life_hours is None else half_life_hours)
    floor = float(config.retention_floor if retention_floor is None else retention_floor)
    effective_half_life = base_half_life * strength
    retention = 2.0 ** (-elapsed_hours / effective_half_life)
    return max(floor, min(retention, 1.0))


def heat_modulations(
    heats_by_session: Dict[str, float],
    *,
    minimum: float,
    maximum: float,
) -> Dict[str, float]:
    """Map each session's absolute heat to a bounded half-life factor."""
    modulation_width = float(maximum) - float(minimum)
    factors = {}
    for session_id, heat in heats_by_session.items():
        try:
            value = max(float(heat), 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        normalized = value / (1.0 + value)
        factor = float(minimum) + normalized * modulation_width
        factors[session_id] = min(max(factor, float(minimum)), float(maximum))
    return factors


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
