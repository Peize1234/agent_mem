from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable

from mem0.memory.main import _build_session_scope
from mem0.memory.memory_evolution import forgetting_factor, heat_modulations
from mem0.memory.midterm import compute_recency, compute_session_heat
from memory_monitor.models.demo_pipeline import MEMORY_STATE_SECTIONS

_LEGACY_SNAPSHOT_SECTIONS = (
    "short_term",
    "midterm_sessions",
    "midterm_pages",
    "long_term",
    "profile",
    "migration_jobs",
    "profile_jobs",
)
# ``long_term`` remains accepted for old persisted Demo snapshots. New live
# state defaults to the canonical split sections and never uses that name in
# the monitor UI.
SNAPSHOT_SECTIONS = tuple(MEMORY_STATE_SECTIONS)
_SUPPORTED_SECTIONS = frozenset((*SNAPSHOT_SECTIONS, "long_term"))
_SQLITE_SECTIONS = frozenset(
    {
        "short_term",
        "profile",
        "migration_jobs",
        "longterm_extraction_jobs",
        "profile_jobs",
        "promotion_jobs",
    }
)
_SESSION_SCOPE_SECTIONS = frozenset({"short_term", "migration_jobs", "longterm_extraction_jobs"})
_MIDTERM_SECTIONS = frozenset({"midterm_sessions", "midterm_pages"})
_SECTION_RECORD_KEYS = {
    "short_term": "id",
    "midterm_sessions": "id",
    "midterm_pages": "id",
    "long_term": "id",
    "fine_grained_longterm": "id",
    "promoted_longterm": "id",
    "profile": "attribute_id",
    "migration_jobs": "job_id",
    "longterm_extraction_jobs": "job_id",
    "profile_jobs": "job_id",
    "promotion_jobs": "job_id",
}


class MemoryStateService:
    """Read current core memory state and compute stable record-level diffs."""

    def __init__(self, memory):
        self.memory = memory
        self.db_path = Path(memory.config.history_db_path).expanduser().resolve()

    def snapshot(
        self,
        *,
        user_id: str,
        run_id: str,
        sections: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        """Capture the active memory view used by persisted step snapshots."""
        # Persisted step snapshots predate the two long-term stores.  Keep the
        # legacy default shape for old turns while explicit sections (and all
        # live ``current_state`` reads) expose the complete production view.
        effective_sections = _LEGACY_SNAPSHOT_SECTIONS if sections is None else sections
        return self._read_state(
            user_id=user_id,
            run_id=run_id,
            sections=effective_sections,
            include_all_messages=False,
        )

    def current_state(
        self,
        *,
        user_id: str,
        run_id: str,
        sections: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        """Read live monitor state, including every message status in the session."""
        return self._read_state(
            user_id=user_id,
            run_id=run_id,
            sections=sections,
            include_all_messages=True,
        )

    def _read_state(
        self,
        *,
        user_id: str,
        run_id: str,
        sections: Iterable[str] | None,
        include_all_messages: bool,
    ) -> Dict[str, Any]:
        selected = self._normalize_sections(sections)
        session_scope = None
        if selected & _SESSION_SCOPE_SECTIONS:
            scope_builder = getattr(self.memory, "session_scope_for_demo", None)
            session_scope = (
                scope_builder(user_id=user_id, run_id=run_id)
                if callable(scope_builder)
                else _build_session_scope({"user_id": user_id, "run_id": run_id})
            )
        sqlite_state = self._sqlite_state(
            user_id=user_id,
            session_scope=session_scope,
            sections=selected,
            include_all_messages=include_all_messages,
        )
        filters = {"user_id": user_id, "run_id": run_id}
        midterm_state = self._midterm_state(filters, selected)
        snapshot: Dict[str, Any] = {}
        if "short_term" in selected:
            snapshot["short_term"] = sqlite_state["short_term"]
        if "midterm_sessions" in selected:
            snapshot["midterm_sessions"] = midterm_state["midterm_sessions"]
        if "midterm_pages" in selected:
            snapshot["midterm_pages"] = midterm_state["midterm_pages"]
        if "long_term" in selected:
            snapshot["long_term"] = self._vector_rows(self.memory.vector_store, filters)
        if "fine_grained_longterm" in selected:
            snapshot["fine_grained_longterm"] = self._fine_grained_longterm_state(filters)
        if "promoted_longterm" in selected:
            snapshot["promoted_longterm"] = self._promoted_longterm_state(user_id)
        if "profile" in selected:
            snapshot["profile"] = sqlite_state["profile"]
        if selected & {
            "migration_jobs",
            "longterm_extraction_jobs",
            "profile_jobs",
            "promotion_jobs",
        }:
            snapshot["jobs"] = {}
            if "migration_jobs" in selected:
                snapshot["jobs"]["migration"] = sqlite_state["migration_jobs"]
            if "longterm_extraction_jobs" in selected:
                snapshot["jobs"]["longterm_extraction"] = sqlite_state["longterm_extraction_jobs"]
            if "profile_jobs" in selected:
                snapshot["jobs"]["profile"] = sqlite_state["profile_jobs"]
            if "promotion_jobs" in selected:
                snapshot["jobs"]["promotion"] = sqlite_state["promotion_jobs"]
        return snapshot

    @staticmethod
    def _normalize_sections(sections: Iterable[str] | None) -> frozenset[str]:
        if sections is None:
            return frozenset(SNAPSHOT_SECTIONS)
        selected = frozenset({sections} if isinstance(sections, str) else sections)
        unknown = selected.difference(_SUPPORTED_SECTIONS)
        if unknown:
            raise ValueError(f"Unknown memory snapshot sections: {sorted(unknown)}")
        return selected

    def _sqlite_state(
        self,
        *,
        user_id: str,
        session_scope: str | None,
        sections: frozenset[str],
        include_all_messages: bool,
    ) -> Dict[str, Any]:
        selected = sections & _SQLITE_SECTIONS
        if not selected:
            return {}
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            state: Dict[str, Any] = {}
            if "short_term" in selected:
                status_filter = "" if include_all_messages else "AND status = 'active'"
                state["short_term"] = self._query(
                    connection,
                    f"""
                    SELECT * FROM messages
                    WHERE session_scope = ? {status_filter}
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_scope,),
                )
            if "migration_jobs" in selected:
                state["migration_jobs"] = self._query(
                    connection,
                    """
                    SELECT * FROM memory_migration_jobs
                    WHERE session_scope = ? ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_scope,),
                )
            if "longterm_extraction_jobs" in selected:
                state["longterm_extraction_jobs"] = self._list_longterm_extraction_jobs(session_scope)
            if "profile_jobs" in selected:
                state["profile_jobs"] = self._query(
                    connection,
                    """
                    SELECT * FROM profile_update_jobs
                    WHERE user_id = ? ORDER BY created_at ASC, rowid ASC
                    """,
                    (user_id,),
                )
            if "promotion_jobs" in selected:
                state["promotion_jobs"] = self._list_promotion_jobs(user_id)
            if "profile" in selected:
                state["profile"] = self._query(
                    connection,
                    """
                    SELECT v.*, a.attribute_key, a.attribute_name, a.attribute_category
                    FROM user_profile_values AS v
                    JOIN profile_attributes AS a ON a.attribute_id = v.attribute_id
                    WHERE v.user_id = ? ORDER BY a.attribute_id ASC
                    """,
                    (user_id,),
                )
            return state
        finally:
            connection.close()

    def has_active_jobs(self, *, user_id: str, run_id: str) -> bool:
        """Return whether this monitor scope still has pending backend work."""
        scope_builder = getattr(self.memory, "session_scope_for_demo", None)
        session_scope = (
            scope_builder(user_id=user_id, run_id=run_id)
            if callable(scope_builder)
            else _build_session_scope({"user_id": user_id, "run_id": run_id})
        )
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.execute("PRAGMA query_only = ON")
        try:
            migration = connection.execute(
                """
                SELECT 1 FROM memory_migration_jobs
                WHERE session_scope = ?
                  AND (
                    status IN ('pending', 'running', 'retry')
                    OR midterm_status IN ('pending', 'running', 'retry')
                    OR longterm_status IN ('pending', 'running', 'retry')
                  )
                LIMIT 1
                """,
                (session_scope,),
            ).fetchone()
            if migration is not None:
                return True
            profile = connection.execute(
                """
                SELECT 1 FROM profile_update_jobs
                WHERE user_id = ? AND status IN ('pending', 'running', 'retry')
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if profile is not None:
                return True
            extraction = connection.execute(
                """
                SELECT 1 FROM longterm_extraction_jobs
                WHERE session_scope = ? AND status IN ('pending', 'running', 'retry')
                LIMIT 1
                """,
                (session_scope,),
            ).fetchone()
            if extraction is not None:
                return True
            promotion = connection.execute(
                """
                SELECT 1 FROM memory_promotion_jobs
                WHERE user_id = ? AND status IN ('pending', 'running', 'retry')
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            return promotion is not None
        finally:
            connection.close()

    def _list_longterm_extraction_jobs(self, session_scope: str | None) -> list[Dict[str, Any]]:
        db = getattr(self.memory, "db", None)
        lister = getattr(db, "list_longterm_extraction_jobs", None)
        if callable(lister):
            return [dict(row) for row in lister(session_scope=session_scope)]
        connection = self._read_only_connection()
        try:
            return self._query(
                connection,
                "SELECT * FROM longterm_extraction_jobs WHERE session_scope = ? ORDER BY sequence_no, rowid",
                (session_scope,),
            )
        finally:
            connection.close()

    def _list_promotion_jobs(self, user_id: str) -> list[Dict[str, Any]]:
        db = getattr(self.memory, "db", None)
        lister = getattr(db, "list_promotion_jobs", None)
        if callable(lister):
            return [dict(row) for row in lister(user_id=user_id)]
        connection = self._read_only_connection()
        try:
            return self._query(
                connection,
                "SELECT * FROM memory_promotion_jobs WHERE user_id = ? ORDER BY created_at, rowid",
                (user_id,),
            )
        finally:
            connection.close()

    def _read_only_connection(self) -> sqlite3.Connection:
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def _fine_grained_longterm_state(self, filters: Dict[str, Any]) -> list[Dict[str, Any]]:
        rows = self._vector_rows(self.memory.vector_store, filters)
        visible = getattr(self.memory, "_stage_output_is_visible", None)
        result = []
        for row in rows:
            payload = row.get("payload") or {}
            if payload.get("source_job_type") != "longterm_extraction":
                continue
            if callable(visible):
                try:
                    if not visible(payload, "longterm"):
                        continue
                except Exception:
                    continue
            result.append(row)
        return result

    def _promoted_longterm_state(self, user_id: str) -> list[Dict[str, Any]]:
        enabled = getattr(self.memory, "_promoted_longterm_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return []
            except Exception:
                return []
        promoted = getattr(self.memory, "promoted_longterm", None)
        if promoted is None:
            promoted = getattr(self.memory, "cross_session_longterm", None)
        if promoted is None:
            return []
        try:
            return self._serialize_vectors(promoted.list(filters={"user_id": user_id}, top_k=1000))
        except TypeError:
            return self._serialize_vectors(promoted.list(filters={"user_id": user_id}))

    @staticmethod
    def _query(
        connection: sqlite3.Connection,
        query: str,
        parameters: tuple[Any, ...],
    ) -> list[Dict[str, Any]]:
        rows = [dict(row) for row in connection.execute(query, parameters).fetchall()]
        for row in rows:
            for key, value in list(row.items()):
                if key.endswith("_json") and isinstance(value, str):
                    try:
                        row[key.removesuffix("_json")] = json.loads(value)
                    except json.JSONDecodeError:
                        pass
        return rows

    def _midterm_state(
        self,
        filters: Dict[str, Any],
        sections: frozenset[str],
    ) -> Dict[str, list[Dict[str, Any]]]:
        selected = sections & _MIDTERM_SECTIONS
        if not selected:
            return {}
        if not getattr(self.memory.config.midterm, "enabled", False):
            return {section: [] for section in selected}
        midterm = self.memory.midterm_memory
        current_turn_index = self._current_midterm_turn_index(midterm, filters)
        sessions = []
        if "midterm_sessions" in selected or ("midterm_pages" in selected and current_turn_index is not None):
            sessions = self._serialize_vectors(midterm.list_sessions(filters=filters, top_k=1000))
        session_states = self._midterm_session_monitor_states(sessions, current_turn_index)
        state = {}
        if "midterm_sessions" in selected:
            state["midterm_sessions"] = self._annotate_midterm_sessions(
                sessions,
                filters,
                session_states,
            )
        if "midterm_pages" in selected:
            pages = self._serialize_vectors(midterm.list_pages(filters=filters, top_k=1000))
            state["midterm_pages"] = self._annotate_midterm_pages(
                pages,
                session_states,
                current_turn_index,
            )
        return state

    @staticmethod
    def _current_midterm_turn_index(midterm, filters: Dict[str, Any]) -> int | None:
        provider = getattr(midterm, "current_turn_index", None)
        if not callable(provider):
            return None
        try:
            return int(provider(filters))
        except (TypeError, ValueError):
            return None

    def _midterm_session_monitor_states(
        self,
        rows: list[Dict[str, Any]],
        current_turn_index: int | None,
    ) -> Dict[str, Dict[str, Any]]:
        if current_turn_index is None:
            return {}
        config = self.memory.config.midterm
        states: Dict[str, Dict[str, Any]] = {}
        current_heats: Dict[str, float] = {}
        for row in rows:
            session_id = str(row.get("id") or "")
            payload = row.get("payload") or {}
            calculation_payload = {
                **payload,
                "last_visit_turn_index": payload.get("last_visit_turn_index") or 0,
                "N_visit": payload.get("N_visit") or 0,
                "L_interaction": payload.get("L_interaction") or 0,
            }
            try:
                current_recency = compute_recency(
                    int(calculation_payload["last_visit_turn_index"]),
                    current_turn_index,
                    config.heat_recency_tau_turns,
                )
                current_heat = compute_session_heat(calculation_payload, config, current_turn_index)
            except (KeyError, TypeError, ValueError):
                current_recency = None
                current_heat = None
            states[session_id] = {
                "current_turn_index": current_turn_index,
                "stored_R_recency": payload.get("R_recency"),
                "current_R_recency": current_recency,
                "stored_H_segment": payload.get("H_segment"),
                "current_H_segment": current_heat,
                "valid_recall_count": int(payload.get("valid_recall_count", 0) or 0),
            }
            if current_heat is not None:
                current_heats[session_id] = current_heat

        factors = heat_modulations(
            current_heats,
            minimum=config.heat_modulation_min,
            maximum=config.heat_modulation_max,
        )
        for session_id, factor in factors.items():
            states[session_id]["heat_factor"] = factor
        return states

    def _annotate_midterm_sessions(
        self,
        rows: list[Dict[str, Any]],
        filters: Dict[str, Any],
        session_states: Dict[str, Dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        """Expose the exact production promotion inputs and persisted job link."""
        midterm_config = getattr(getattr(self.memory, "config", None), "midterm", None)
        min_recalls = int(getattr(midterm_config, "promotion_min_recall_count", 0) or 0)
        heat_threshold = float(getattr(midterm_config, "promotion_heat_threshold", 0.0) or 0.0)
        promotion_jobs = {}
        lister = getattr(getattr(self.memory, "db", None), "list_promotion_jobs", None)
        if callable(lister):
            try:
                promotion_jobs = {
                    str(job.get("source_midterm_session_id")): job for job in lister(user_id=filters.get("user_id"))
                }
            except Exception:
                promotion_jobs = {}
        promoted_rows = self._promoted_longterm_state(str(filters.get("user_id") or ""))
        promoted_by_session = {
            str((row.get("payload") or {}).get("source_midterm_session_id")): row.get("id")
            for row in promoted_rows
            if (row.get("payload") or {}).get("source_midterm_session_id")
        }
        annotated = []
        for row in rows:
            item = dict(row)
            payload = item.get("payload") or {}
            recalls = int(payload.get("valid_recall_count", 0) or 0)
            stored_heat = float(payload.get("H_segment", 0.0) or 0.0)
            monitor_state = dict(session_states.get(str(item.get("id"))) or {})
            monitor_state["promotion_threshold"] = {
                "min_valid_recall_count": min_recalls,
                "heat_threshold": heat_threshold,
            }
            stored_promotion_eligible = recalls >= min_recalls and stored_heat >= heat_threshold
            monitor_state["stored_promotion_eligible"] = stored_promotion_eligible
            monitor_state["promotion_eligible"] = stored_promotion_eligible
            current_heat = monitor_state.get("current_H_segment")
            monitor_state["current_promotion_eligible"] = (
                recalls >= min_recalls and current_heat >= heat_threshold if current_heat is not None else None
            )
            job = promotion_jobs.get(str(item.get("id")))
            if job:
                monitor_state["promotion_job_id"] = job.get("job_id")
                monitor_state["promotion_job_status"] = job.get("status")
            if str(item.get("id")) in promoted_by_session:
                monitor_state["promoted_longterm_id"] = promoted_by_session[str(item.get("id"))]
            item["monitor_state"] = monitor_state
            annotated.append(item)
        return annotated

    def _annotate_midterm_pages(
        self,
        rows: list[Dict[str, Any]],
        session_states: Dict[str, Dict[str, Any]],
        current_turn_index: int | None,
    ) -> list[Dict[str, Any]]:
        if current_turn_index is None:
            return rows
        config = self.memory.config.midterm
        base_half_life = getattr(config, "retention_half_life_turns", None)
        annotated = []
        for row in rows:
            item = dict(row)
            payload = item.get("payload") or {}
            session_id = str(payload.get("session_id") or "")
            session_state = session_states.get(session_id) or {}
            heat_factor = session_state.get("heat_factor")
            last_recall_turn_index = payload.get("last_recall_turn_index")
            turn_index = payload.get("turn_index")
            decay_anchor_turn_index = last_recall_turn_index if last_recall_turn_index is not None else turn_index
            turns_since_last_recall = self._turn_distance(current_turn_index, last_recall_turn_index)
            turns_since_decay_anchor = self._turn_distance(current_turn_index, decay_anchor_turn_index)
            effective_half_life = self._effective_half_life(base_half_life, heat_factor)
            retention = None
            if heat_factor is not None and decay_anchor_turn_index is not None:
                try:
                    retention = forgetting_factor(
                        payload,
                        config,
                        current_turn_index=current_turn_index,
                        heat_factor=heat_factor,
                    )
                except (TypeError, ValueError):
                    retention = None
            item["monitor_state"] = {
                "session_id": payload.get("session_id"),
                "current_turn_index": current_turn_index,
                "turn_index": turn_index,
                "last_recall_turn_index": last_recall_turn_index,
                "turns_since_last_valid_recall": turns_since_last_recall,
                "decay_anchor_turn_index": decay_anchor_turn_index,
                "turns_since_decay_anchor": turns_since_decay_anchor,
                "valid_recall_count": int(payload.get("valid_recall_count", 0) or 0),
                "current_H_segment": session_state.get("current_H_segment"),
                "heat_factor": heat_factor,
                "base_half_life_turns": base_half_life,
                "effective_half_life_turns": effective_half_life,
                "forgetting_factor": retention,
                "retention": retention,
            }
            annotated.append(item)
        return annotated

    @staticmethod
    def _turn_distance(current_turn_index: int, anchor_turn_index: Any) -> int | float | None:
        if anchor_turn_index is None:
            return None
        try:
            return max(float(current_turn_index) - float(anchor_turn_index), 0.0)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _effective_half_life(base_half_life: Any, heat_factor: Any) -> float | None:
        if base_half_life is None or heat_factor is None:
            return None
        try:
            return float(base_half_life) * float(heat_factor)
        except (TypeError, ValueError):
            return None

    def _vector_rows(self, store, filters: Dict[str, Any]) -> list[Dict[str, Any]]:
        listed = store.list(filters=filters, top_k=1000)
        if isinstance(listed, tuple):
            listed = listed[0]
        return self._serialize_vectors(listed or [])

    @staticmethod
    def _serialize_vectors(rows: Iterable[Any]) -> list[Dict[str, Any]]:
        return [
            {
                "id": str(getattr(row, "id", "")),
                "score": getattr(row, "score", None),
                "payload": dict(getattr(row, "payload", None) or {}),
            }
            for row in rows
        ]

    @classmethod
    def compare(
        cls,
        before: Dict[str, Any],
        after: Dict[str, Any],
        sections: Iterable[str] | None = None,
    ) -> Dict[str, Any]:
        selected = cls._normalize_sections(sections)
        diff = {}
        for name in _SUPPORTED_SECTIONS:
            if name not in selected:
                continue
            rows_before = cls._section_rows(before, name)
            rows_after = cls._section_rows(after, name)
            if rows_before is None and rows_after is None:
                rows_before = rows_after = []
            elif rows_before is None:
                rows_before = rows_after
            elif rows_after is None:
                rows_after = rows_before
            diff[name] = cls._section_diff(
                _SECTION_RECORD_KEYS[name],
                rows_before,
                rows_after,
            )
        return diff

    @staticmethod
    def _section_rows(snapshot: Dict[str, Any], section: str) -> list[Dict[str, Any]] | None:
        if section == "migration_jobs":
            jobs = snapshot.get("jobs")
            return None if not isinstance(jobs, dict) or "migration" not in jobs else jobs["migration"]
        if section == "profile_jobs":
            jobs = snapshot.get("jobs")
            return None if not isinstance(jobs, dict) or "profile" not in jobs else jobs["profile"]
        if section == "longterm_extraction_jobs":
            jobs = snapshot.get("jobs")
            return None if not isinstance(jobs, dict) or "longterm_extraction" not in jobs else jobs["longterm_extraction"]
        if section == "promotion_jobs":
            jobs = snapshot.get("jobs")
            return None if not isinstance(jobs, dict) or "promotion" not in jobs else jobs["promotion"]
        return snapshot.get(section) if section in snapshot else None

    @staticmethod
    def _section_diff(
        key: str,
        rows_before: list[Dict[str, Any]],
        rows_after: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        previous = {str(row.get(key)): row for row in rows_before}
        current = {str(row.get(key)): row for row in rows_after}
        return {
            "before_count": len(rows_before),
            "after_count": len(rows_after),
            "added": [row for item_key, row in current.items() if item_key not in previous],
            "updated": [row for item_key, row in current.items() if item_key in previous and row != previous[item_key]],
            "deleted": [row for item_key, row in previous.items() if item_key not in current],
        }
