"""Cross-session temporal replay, kept separate from turn-based evolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping


UNVALIDATED_NO_CROSS_SESSION_GOLD = "UNVALIDATED_NO_CROSS_SESSION_GOLD"


@dataclass
class TemporalMemoryState:
    memory_id: str
    user_id: str
    promoted_at: datetime
    last_recall_at: datetime | None = None
    recall_count: int = 0


@dataclass
class TemporalReplayResult:
    status: str
    transitions: list[dict[str, Any]] = field(default_factory=list)
    winner_selection_enabled: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)


class CrossSessionTemporalReplay:
    """Execute promotion/decay/reinforcement transitions using elapsed hours.

    Without temporal Gold this engine remains executable for structural tests,
    but its output is explicitly excluded from winner selection.
    """

    def __init__(self, *, retention_half_life_hours: float = 720.0, retention_floor: float = 0.2, reinforcement_gain: float = 0.25):
        if retention_half_life_hours <= 0 or not 0 <= retention_floor <= 1 or reinforcement_gain < 0:
            raise ValueError("invalid cross-session temporal parameters")
        self.retention_half_life_hours = float(retention_half_life_hours)
        self.retention_floor = float(retention_floor)
        self.reinforcement_gain = float(reinforcement_gain)

    def retention(self, state: TemporalMemoryState, at: datetime) -> float:
        # Reuse the production algorithm; TemporalReplay owns only event
        # ordering and the elapsed-hours clock.
        from mem0.memory.memory_evolution import forgetting_factor

        return forgetting_factor(
            {
                "promoted_at": state.promoted_at.isoformat(),
                "last_recall_at": state.last_recall_at.isoformat() if state.last_recall_at else None,
                "recall_count": state.recall_count,
            },
            SimpleNamespace(
                retention_half_life_hours=self.retention_half_life_hours,
                retention_floor=self.retention_floor,
            ),
            now=at.isoformat(),
            recall_count_key="recall_count",
            anchor_keys=("last_recall_at", "promoted_at"),
            half_life_hours=self.retention_half_life_hours,
            retention_floor=self.retention_floor,
            reinforcement_gain=self.reinforcement_gain,
        )

    def replay(
        self,
        sessions: Iterable[Mapping[str, Any]],
        *,
        cross_session_gold_available: bool = False,
        promote: Callable[[Mapping[str, Any]], Iterable[Mapping[str, Any]]] | None = None,
        retrieve: Callable[[Mapping[str, Any], list[TemporalMemoryState], float], Iterable[str]] | None = None,
    ) -> TemporalReplayResult:
        states: dict[str, TemporalMemoryState] = {}
        transitions: list[dict[str, Any]] = []
        for session in sessions:
            at = session.get("at") or session.get("timestamp")
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            if not isinstance(at, datetime):
                raise ValueError("each temporal Session requires an elapsed-time timestamp")
            promoted = list(promote(session) if promote else session.get("promotions") or [])
            for item in promoted:
                memory_id = str(item.get("memory_id") or item.get("id"))
                if not memory_id:
                    continue
                states[memory_id] = TemporalMemoryState(memory_id, str(session.get("user_id") or ""), at)
                transitions.append({"event": "promotion", "session_id": session.get("session_id"), "memory_id": memory_id, "at": at.isoformat()})
            current_user = str(session.get("user_id") or "")
            visible_states = [state for state in states.values() if state.user_id == current_user]
            # Search-time decay is evaluated before a valid recall mutates
            # last_recall_at/recall_count.  This is the production temporal
            # order: elapsed time -> recall -> reinforcement.
            transitions.extend(
                {
                    "event": "decay",
                    "session_id": session.get("session_id"),
                    "memory_id": state.memory_id,
                    "retention": self.retention(state, at),
                    "at": at.isoformat(),
                }
                for state in visible_states
            )
            visible_ids = list(retrieve(session, visible_states, at) if retrieve else [])
            for memory_id in visible_ids:
                state = states.get(str(memory_id))
                if state is None or state.user_id != current_user:
                    continue
                state.recall_count += 1
                state.last_recall_at = at
                transitions.append({"event": "valid_recall", "session_id": session.get("session_id"), "memory_id": state.memory_id, "at": at.isoformat(), "recall_count": state.recall_count})
                transitions.append(
                    {
                        "event": "reinforcement",
                        "session_id": session.get("session_id"),
                        "memory_id": state.memory_id,
                        "recall_count": state.recall_count,
                        "at": at.isoformat(),
                    }
                )
        status = "COMPLETE" if cross_session_gold_available else UNVALIDATED_NO_CROSS_SESSION_GOLD
        return TemporalReplayResult(
            status=status,
            transitions=transitions,
            winner_selection_enabled=bool(cross_session_gold_available),
            provenance={
                "clock": "elapsed_hours",
                "retention_half_life_hours": self.retention_half_life_hours,
                "retention_floor": self.retention_floor,
                "reinforcement_gain": self.reinforcement_gain,
            },
        )
