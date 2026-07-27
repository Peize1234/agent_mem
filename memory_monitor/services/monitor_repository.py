from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


class MonitorRepository:
    """Paged, read-only access to the SQLite tables exposed by the monitor."""

    ALLOWED_TABLES = {
        "history",
        "messages",
        "memory_migration_jobs",
        "profile_update_jobs",
        "profile_attributes",
        "user_profile_values",
        "memory_observation_events",
    }
    MAX_PAGE_SIZE = 500

    def __init__(self, db_path: str | Path, *, max_field_length: int = 4000):
        self.db_path = Path(db_path).expanduser().resolve()
        self.max_field_length = max(int(max_field_length), 100)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.db_path.exists():
            raise FileNotFoundError(f"SQLite database does not exist: {self.db_path}")
        uri = f"{self.db_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
        finally:
            connection.close()

    def list_tables(self) -> List[str]:
        with self._connection() as connection:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return [row["name"] for row in rows if row["name"] in self.ALLOWED_TABLES]

    def table_schema(self, table: str) -> List[Dict[str, Any]]:
        table = self._validate_table(table)
        with self._connection() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return [dict(row) for row in rows]

    def page(
        self,
        table: str,
        *,
        page: int = 1,
        page_size: int = 50,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        descending: bool = True,
    ) -> Dict[str, Any]:
        table = self._validate_table(table)
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), self.MAX_PAGE_SIZE)
        columns = {item["name"] for item in self.table_schema(table)}
        where, parameters = self._where_clause(columns, filters or {})
        if order_by is None:
            order_by = "created_at" if "created_at" in columns else "rowid"
        if order_by != "rowid" and order_by not in columns:
            raise ValueError(f"Unsupported order column for {table}: {order_by}")
        direction = "DESC" if descending else "ASC"
        offset = (page - 1) * page_size
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM {table}{where}",
                parameters,
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT * FROM {table}{where} ORDER BY {order_by} {direction}, rowid {direction} LIMIT ? OFFSET ?",
                (*parameters, page_size, offset),
            ).fetchall()
        return {
            "items": [self._decode_row(dict(row)) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def get_job(self, job_id: str, job_type: str) -> Optional[Dict[str, Any]]:
        table = self._job_table(job_type)
        result = self.page(table, page_size=1, filters={"job_id": job_id})
        return result["items"][0] if result["items"] else None

    def list_jobs(
        self,
        job_type: str,
        *,
        page: int = 1,
        page_size: int = 50,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self.page(self._job_table(job_type), page=page, page_size=page_size, filters=filters)

    def trace_events(self, trace_id: str, *, page: int = 1, page_size: int = 200) -> Dict[str, Any]:
        return self.page(
            "memory_observation_events",
            page=page,
            page_size=page_size,
            filters={"trace_id": trace_id},
            order_by="created_at",
            descending=False,
        )

    def trace_summaries(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        user_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), self.MAX_PAGE_SIZE)
        filters = {key: value for key, value in {"user_id": user_id, "run_id": run_id}.items() if value}
        where, parameters = self._where_clause({"user_id", "run_id"}, filters)
        offset = (page - 1) * page_size
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(DISTINCT trace_id) AS count FROM memory_observation_events{where}",
                parameters,
            ).fetchone()["count"]
            rows = connection.execute(
                f"""
                SELECT
                    trace_id,
                    MAX(user_id) AS user_id,
                    MAX(run_id) AS run_id,
                    MIN(created_at) AS created_at,
                    MAX(created_at) AS finished_at,
                    MAX(CASE WHEN event_type = 'messages.saved' THEN status END) AS shortterm_status,
                    MAX(CASE WHEN event_type IN ('midterm.succeeded', 'midterm.degraded', 'midterm.failed')
                        THEN status END) AS midterm_status,
                    MAX(CASE WHEN event_type IN ('longterm.succeeded', 'longterm.degraded', 'longterm.failed')
                        THEN status END) AS longterm_status,
                    MAX(CASE WHEN event_type IN ('profile.succeeded', 'profile.failed')
                        THEN status END) AS profile_status,
                    MAX(
                        (JULIANDAY(MAX(created_at)) - JULIANDAY(MIN(created_at))) * 86400000,
                        0
                    ) AS total_duration_ms
                FROM memory_observation_events
                {where}
                GROUP BY trace_id
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total}

    def scalar_counts(self, table: str, column: str) -> Dict[str, int]:
        table = self._validate_table(table)
        columns = {item["name"] for item in self.table_schema(table)}
        if column not in columns:
            raise ValueError(f"Unsupported count column for {table}: {column}")
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {column} AS value, COUNT(*) AS count FROM {table} GROUP BY {column}"
            ).fetchall()
        return {str(row["value"]): int(row["count"]) for row in rows}

    def aggregate_job_metrics(self, table: str) -> Dict[str, Any]:
        table = self._validate_table(table)
        if table not in {"memory_migration_jobs", "profile_update_jobs"}:
            raise ValueError("Job metrics are only available for background job tables")
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status IN ('succeeded', 'succeeded_degraded') THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END) AS retried,
                    SUM(CASE WHEN status = 'succeeded_degraded' OR degraded = 1 THEN 1 ELSE 0 END) AS degraded,
                    SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead,
                    MIN(CASE WHEN status IN ('pending', 'retry') THEN created_at END) AS oldest_pending_at
                FROM {table}
                """
                if table == "memory_migration_jobs"
                else f"""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN attempts > 0 THEN 1 ELSE 0 END) AS retried,
                    0 AS degraded,
                    SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead,
                    MIN(CASE WHEN status IN ('pending', 'retry') THEN created_at END) AS oldest_pending_at
                FROM {table}
                """
            ).fetchone()
            observed_retries = connection.execute(
                """
                SELECT COUNT(DISTINCT job_id) AS count
                FROM memory_observation_events
                WHERE job_type = ? AND event_type = 'job.retry_scheduled'
                """,
                ("migration" if table == "memory_migration_jobs" else "profile",),
            ).fetchone()["count"]
        metrics = {key: (value or 0) for key, value in dict(row).items()}
        metrics["retried"] = max(int(metrics["retried"]), int(observed_retries or 0))
        return metrics

    def average_event_duration(self, event_type: str) -> float:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT AVG(duration_ms) AS average_duration_ms
                FROM memory_observation_events
                WHERE event_type = ? AND status = 'succeeded' AND duration_ms IS NOT NULL
                """,
                (event_type,),
            ).fetchone()
        return float(row["average_duration_ms"] or 0.0)

    def _decode_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        decoded = {key: self._truncate(value) for key, value in row.items()}
        for key, value in list(decoded.items()):
            if not key.endswith("_json") or not isinstance(value, str):
                continue
            try:
                decoded[key.removesuffix("_json")] = self._truncate(json.loads(row[key]))
            except json.JSONDecodeError:
                decoded[key.removesuffix("_json")] = value
        return decoded

    def _truncate(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self.max_field_length:
            return f"{value[: self.max_field_length]}… [truncated {len(value) - self.max_field_length} chars]"
        if isinstance(value, dict):
            return {key: self._truncate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._truncate(item) for item in value]
        return value

    @staticmethod
    def _where_clause(columns: set[str], filters: Dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        clauses = []
        parameters = []
        for key, value in filters.items():
            if value is None or value == "":
                continue
            if key not in columns:
                raise ValueError(f"Unsupported filter column: {key}")
            clauses.append(f"{key} = ?")
            parameters.append(value)
        return (f" WHERE {' AND '.join(clauses)}" if clauses else "", tuple(parameters))

    def _validate_table(self, table: str) -> str:
        if table not in self.ALLOWED_TABLES:
            raise ValueError(f"Table is not exposed by the monitor: {table}")
        if table not in self.list_tables():
            raise ValueError(f"Table does not exist: {table}")
        return table

    @staticmethod
    def _job_table(job_type: str) -> str:
        if job_type == "migration":
            return "memory_migration_jobs"
        if job_type == "profile":
            return "profile_update_jobs"
        raise ValueError("job_type must be 'migration' or 'profile'")
