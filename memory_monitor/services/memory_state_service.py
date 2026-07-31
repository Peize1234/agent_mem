from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable

from mem0.memory.main import _build_session_scope

SNAPSHOT_SECTIONS = (
    "short_term",
    "midterm_sessions",
    "midterm_pages",
    "long_term",
    "profile",
    "migration_jobs",
    "profile_jobs",
)
_SQLITE_SECTIONS = frozenset({"short_term", "profile", "migration_jobs", "profile_jobs"})
_SESSION_SCOPE_SECTIONS = frozenset({"short_term", "migration_jobs"})
_MIDTERM_SECTIONS = frozenset({"midterm_sessions", "midterm_pages"})
_SECTION_RECORD_KEYS = {
    "short_term": "id",
    "midterm_sessions": "id",
    "midterm_pages": "id",
    "long_term": "id",
    "profile": "attribute_id",
    "migration_jobs": "job_id",
    "profile_jobs": "job_id",
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
        if "profile" in selected:
            snapshot["profile"] = sqlite_state["profile"]
        if selected & {"migration_jobs", "profile_jobs"}:
            snapshot["jobs"] = {}
            if "migration_jobs" in selected:
                snapshot["jobs"]["migration"] = sqlite_state["migration_jobs"]
            if "profile_jobs" in selected:
                snapshot["jobs"]["profile"] = sqlite_state["profile_jobs"]
        return snapshot

    @staticmethod
    def _normalize_sections(sections: Iterable[str] | None) -> frozenset[str]:
        if sections is None:
            return frozenset(SNAPSHOT_SECTIONS)
        selected = frozenset({sections} if isinstance(sections, str) else sections)
        unknown = selected.difference(SNAPSHOT_SECTIONS)
        if unknown:
            raise ValueError(f"Unknown memory snapshot sections: {sorted(unknown)}")
        return selected

    def _sqlite_state(
        self,
        *,
        user_id: str,
        session_scope: str | None,
        sections: frozenset[str],
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
                state["short_term"] = self._query(
                    connection,
                    """
                    SELECT * FROM messages
                    WHERE session_scope = ? AND status = 'active'
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
            if "profile_jobs" in selected:
                state["profile_jobs"] = self._query(
                    connection,
                    """
                    SELECT * FROM profile_update_jobs
                    WHERE user_id = ? ORDER BY created_at ASC, rowid ASC
                    """,
                    (user_id,),
                )
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
        state = {}
        if "midterm_sessions" in selected:
            state["midterm_sessions"] = self._serialize_vectors(
                midterm.list_sessions(filters=filters, top_k=1000)
            )
        if "midterm_pages" in selected:
            state["midterm_pages"] = self._serialize_vectors(
                midterm.list_pages(filters=filters, top_k=1000)
            )
        return state

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
        for name in SNAPSHOT_SECTIONS:
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
