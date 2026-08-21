"""Cross-session temporal replay, kept separate from turn-based evolution."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

from .io_utils import atomic_write_json, load_json, load_jsonl, sha256_file


UNVALIDATED_NO_CROSS_SESSION_GOLD = "UNVALIDATED_NO_CROSS_SESSION_GOLD"
UNSUPPORTED_TEMPORAL_GOLD_SCHEMA = "UNAVAILABLE_UNSUPPORTED_TEMPORAL_GOLD_SCHEMA"
VALIDATED = "VALIDATED"


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
    metrics: dict[str, Any] = field(default_factory=dict)
    executed: bool = True


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
        temporal_gold_evaluator: Callable[[list[dict[str, Any]]], Mapping[str, Any]] | None = None,
    ) -> TemporalReplayResult:
        states: dict[str, TemporalMemoryState] = {}
        transitions: list[dict[str, Any]] = []
        session_rows = [dict(session) for session in sessions]
        for session in session_rows:
            at = session.get("at") or session.get("timestamp")
            if isinstance(at, str):
                at = datetime.fromisoformat(at)
            if not isinstance(at, datetime):
                raise ValueError("each temporal Session requires an elapsed-time timestamp")
            promoted = list(promote(session) if promote else session.get("promotions") or [])
            for item in promoted:
                raw_memory_id = item.get("memory_id") or item.get("id")
                if raw_memory_id in (None, ""):
                    continue
                memory_id = str(raw_memory_id)
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
        metrics: dict[str, Any] = {
            "session_count": len(session_rows),
            "promotion_event_count": sum(row["event"] == "promotion" for row in transitions),
            "decay_event_count": sum(row["event"] == "decay" for row in transitions),
            "valid_recall_event_count": sum(row["event"] == "valid_recall" for row in transitions),
            "reinforcement_event_count": sum(row["event"] == "reinforcement" for row in transitions),
        }
        winner_selection_enabled = False
        if not cross_session_gold_available:
            status = UNVALIDATED_NO_CROSS_SESSION_GOLD
        elif temporal_gold_evaluator is None:
            status = UNSUPPORTED_TEMPORAL_GOLD_SCHEMA
        else:
            evaluated = dict(temporal_gold_evaluator(transitions) or {})
            metrics.update(evaluated)
            if int(evaluated.get("evaluated_requirement_count") or 0) <= 0:
                status = UNSUPPORTED_TEMPORAL_GOLD_SCHEMA
            else:
                status = VALIDATED
                winner_selection_enabled = True
        return TemporalReplayResult(
            status=status,
            transitions=transitions,
            winner_selection_enabled=winner_selection_enabled,
            metrics=metrics,
            provenance={
                "clock": "elapsed_hours",
                "retention_half_life_hours": self.retention_half_life_hours,
                "retention_floor": self.retention_floor,
                "reinforcement_gain": self.reinforcement_gain,
                "event_order": ["promotion", "decay", "valid_recall", "reinforcement"],
                "gold_evaluator_executed": temporal_gold_evaluator is not None,
            },
        )


def run_cross_session_temporal_replay(
    *,
    dataset: Any,
    audit: Mapping[str, Any],
    baseline: Any,
    memory_config: Mapping[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """Build temporal inputs, execute replay, and persist its provenance."""
    trace_paths: list[Path] = []
    for raw_manifest in baseline.config.get("manifest_paths") or []:
        manifest_path = Path(str(raw_manifest))
        if not manifest_path.is_file():
            continue
        manifest = load_json(manifest_path)
        trace_path = Path(str(manifest.get("trace_path") or ""))
        if trace_path.is_file():
            trace_paths.append(trace_path)
    trace_by_session: dict[str, list[dict[str, Any]]] = {}
    for trace_path in trace_paths:
        for row in load_jsonl(trace_path):
            trace_by_session.setdefault(str(row.get("session_id") or ""), []).append(dict(row))

    base_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
    temporal_sessions: list[dict[str, Any]] = []
    real_timestamp_count = 0
    for index, session_id in enumerate(sorted(dataset.sessions)):
        rows = trace_by_session.get(session_id, [])
        timestamp: datetime | None = None
        for row in rows:
            raw_timestamp = row.get("at") or row.get("timestamp") or row.get("created_at")
            if raw_timestamp:
                try:
                    timestamp = datetime.fromisoformat(str(raw_timestamp))
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    real_timestamp_count += 1
                    break
                except ValueError:
                    continue
        promotions = [
            dict(event)
            for row in rows
            for event in row.get("promotion_events") or []
            if isinstance(event, Mapping)
        ]
        recalled = [
            str(memory_id)
            for row in rows
            for memory_id in row.get("cross_session_valid_recall_ids") or []
            if memory_id not in (None, "")
        ]
        temporal_sessions.append(
            {
                "session_id": session_id,
                "user_id": f"benchmark::{dataset.sha256}",
                "at": timestamp or base_time + timedelta(hours=24 * index),
                "promotions": promotions,
                "valid_recall_ids": list(dict.fromkeys(recalled)),
            }
        )

    structural_probe = False
    if temporal_sessions and not any(session["promotions"] for session in temporal_sessions):
        # Ordinary Session benchmarks have no promotion trace.  Exercise every
        # transition with an explicitly-labelled structural probe; it cannot
        # produce a winner or a temporal Recall claim.
        structural_probe = True
        probe_id = f"structural-probe::{dataset.sha256[:12]}"
        temporal_sessions[0]["promotions"] = [{"memory_id": probe_id, "structural_probe": True}]
        temporal_sessions[-1]["valid_recall_ids"] = [probe_id]

    has_temporal_schema = False
    temporal_gold: dict[str, set[str]] = {}
    if bool(audit.get("cross_session_gold_available")):
        marked_turns = [
            turn
            for turns in dataset.sessions.values()
            for turn in turns
            if any(
                marker in str(turn.dependency_type or "").lower()
                for marker in ("cross", "temporal", "promotion")
            )
        ]
        if marked_turns and all(
            getattr(turn, "temporal_gold_memory_ids", None) and getattr(turn, "timestamp", None)
            for turn in marked_turns
        ):
            has_temporal_schema = True
            temporal_gold = {
                turn.session_id: {str(value) for value in getattr(turn, "temporal_gold_memory_ids")}
                for turn in marked_turns
            }

    def evaluate_gold(transitions: list[dict[str, Any]]) -> dict[str, Any]:
        recalled_by_session: dict[str, set[str]] = {}
        for transition in transitions:
            if transition.get("event") == "valid_recall":
                recalled_by_session.setdefault(str(transition.get("session_id") or ""), set()).add(
                    str(transition.get("memory_id") or "")
                )
        total = sum(len(values) for values in temporal_gold.values())
        hits = sum(
            len(values & recalled_by_session.get(session_id, set()))
            for session_id, values in temporal_gold.items()
        )
        return {
            "evaluated_requirement_count": total,
            "temporal_recall": hits / total if total else 0.0,
        }

    replay = CrossSessionTemporalReplay(
        retention_half_life_hours=float(memory_config.get("cross_session_retention_half_life_hours", 720.0)),
        retention_floor=float(memory_config.get("cross_session_retention_floor", 0.2)),
        reinforcement_gain=float(memory_config.get("cross_session_reinforcement_gain", 0.25)),
    ).replay(
        temporal_sessions,
        cross_session_gold_available=bool(audit.get("cross_session_gold_available")),
        retrieve=lambda session, _states, _at: session.get("valid_recall_ids") or [],
        temporal_gold_evaluator=evaluate_gold if has_temporal_schema else None,
    )
    payload = {
        "status": replay.status,
        "executed": replay.executed,
        "winner_selection_enabled": replay.winner_selection_enabled,
        "metrics": replay.metrics,
        "transitions": replay.transitions,
        "provenance": {
            **replay.provenance,
            "trace_paths": [str(path) for path in trace_paths],
            "trace_sha256": {str(path): sha256_file(path) for path in trace_paths},
            "real_timestamp_count": real_timestamp_count,
            "synthetic_elapsed_hours_for_structural_replay": real_timestamp_count < len(temporal_sessions),
            "structural_probe": structural_probe,
            "temporal_gold_schema_supported": has_temporal_schema,
        },
    }
    atomic_write_json(run_dir / "temporal_replay.json", payload)
    return payload
