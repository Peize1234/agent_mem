from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable

from mem0.memory.main import _build_session_scope


class MemoryStateService:
    """Read current core memory state and compute stable record-level diffs."""

    def __init__(self, memory):
        self.memory = memory
        self.db_path = Path(memory.config.history_db_path).expanduser().resolve()

    def snapshot(self, *, user_id: str, run_id: str) -> Dict[str, Any]:
        scope_builder = getattr(self.memory, "session_scope_for_demo", None)
        session_scope = (
            scope_builder(user_id=user_id, run_id=run_id)
            if callable(scope_builder)
            else _build_session_scope({"user_id": user_id, "run_id": run_id})
        )
        sqlite_state = self._sqlite_state(user_id=user_id, session_scope=session_scope)
        filters = {"user_id": user_id, "run_id": run_id}
        midterm_sessions, midterm_pages = self._midterm_state(filters)
        return {
            "short_term": sqlite_state["short_term"],
            "midterm_sessions": midterm_sessions,
            "midterm_pages": midterm_pages,
            "long_term": self._vector_rows(self.memory.vector_store, filters),
            "profile": sqlite_state["profile"],
            "jobs": {
                "migration": sqlite_state["migration_jobs"],
                "profile": sqlite_state["profile_jobs"],
            },
        }

    def _sqlite_state(self, *, user_id: str, session_scope: str) -> Dict[str, Any]:
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        try:
            return {
                "short_term": self._query(
                    connection,
                    """
                    SELECT * FROM messages
                    WHERE session_scope = ? AND status = 'active'
                    ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_scope,),
                ),
                "migration_jobs": self._query(
                    connection,
                    """
                    SELECT * FROM memory_migration_jobs
                    WHERE session_scope = ? ORDER BY created_at ASC, rowid ASC
                    """,
                    (session_scope,),
                ),
                "profile_jobs": self._query(
                    connection,
                    """
                    SELECT * FROM profile_update_jobs
                    WHERE user_id = ? ORDER BY created_at ASC, rowid ASC
                    """,
                    (user_id,),
                ),
                "profile": self._query(
                    connection,
                    """
                    SELECT v.*, a.attribute_key, a.attribute_name, a.attribute_category
                    FROM user_profile_values AS v
                    JOIN profile_attributes AS a ON a.attribute_id = v.attribute_id
                    WHERE v.user_id = ? ORDER BY a.attribute_id ASC
                    """,
                    (user_id,),
                ),
            }
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

    def _midterm_state(self, filters: Dict[str, Any]) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
        if not getattr(self.memory.config.midterm, "enabled", False):
            return [], []
        midterm = self.memory.midterm_memory
        return (
            self._serialize_vectors(midterm.list_sessions(filters=filters, top_k=1000)),
            self._serialize_vectors(midterm.list_pages(filters=filters, top_k=1000)),
        )

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
    def compare(cls, before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        sections = {
            "short_term": ("id", before.get("short_term", []), after.get("short_term", [])),
            "midterm_sessions": (
                "id",
                before.get("midterm_sessions", []),
                after.get("midterm_sessions", []),
            ),
            "midterm_pages": ("id", before.get("midterm_pages", []), after.get("midterm_pages", [])),
            "long_term": ("id", before.get("long_term", []), after.get("long_term", [])),
            "profile": ("attribute_id", before.get("profile", []), after.get("profile", [])),
            "migration_jobs": (
                "job_id",
                before.get("jobs", {}).get("migration", []),
                after.get("jobs", {}).get("migration", []),
            ),
            "profile_jobs": (
                "job_id",
                before.get("jobs", {}).get("profile", []),
                after.get("jobs", {}).get("profile", []),
            ),
        }
        return {
            name: cls._section_diff(key, rows_before, rows_after)
            for name, (key, rows_before, rows_after) in sections.items()
        }

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
