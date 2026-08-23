from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from mem0.configs.production import load_production_memory_config

_SAFE_EXACT_PATHS = {
    "query_rewrite_prompt",
    "midterm.top_k_sessions",
    "midterm.top_k_pages",
    "midterm.max_total_pages",
    "midterm.midterm_candidate_pool_multiplier",
    "midterm.midterm_rag_threshold",
    "midterm.retrieval_method",
    "midterm.fusion_method",
    "midterm.dense_weight",
    "midterm.rrf_rank_constant",
    "fine_grained_longterm.top_k",
    "fine_grained_longterm.rag_threshold",
    "fine_grained_longterm.candidate_pool_multiplier",
    "fine_grained_longterm.other_session_weight",
    "fine_grained_longterm.entity_similarity_threshold",
    "fine_grained_longterm.semantic_weight",
    "fine_grained_longterm.bm25_weight",
    "fine_grained_longterm.entity_weight",
    "agentic_retrieval.enabled",
    "agentic_retrieval.max_queries",
    "agentic_retrieval.max_total_results",
}
_SAFE_PREFIXES = {
    "midterm.reranker",
    "fine_grained_longterm.reranker",
}
_CROSS_SESSION_PREFIXES = {
    "promoted_longterm",
    "cross_session_longterm_rag_threshold",
    "cross_session_retention_half_life_hours",
    "cross_session_retention_floor",
    "cross_session_reinforcement_gain",
    "promoted_longterm_top_k",
    "promoted_longterm_rag_threshold",
}


@dataclass(frozen=True)
class ActiveUserConfig:
    user_id: str
    config_version: int
    config_overrides: dict[str, Any]
    source_run_dir: str
    dataset_hash: str
    validation_metrics: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OverridePartition:
    deployable: dict[str, Any]
    rebuild_required: dict[str, Any]
    unsupported: dict[str, Any]
    exclusion_reasons: dict[str, str]


def _flatten(value: Mapping[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping) and item:
            rows.extend(_flatten(item, path))
        else:
            rows.append((path, deepcopy(item)))
    return rows


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    cursor = target
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = deepcopy(value)


def _matches_prefix(path: str, prefixes: set[str]) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}.") for prefix in prefixes)


def partition_production_overrides(overrides: Mapping[str, Any]) -> OverridePartition:
    """Classify future-application notes without deciding what is persisted.

    ``deployable`` retains its compatibility name and means that applying the
    field later should not require rebuilding memory sources. Source-changing
    fields remain in ``rebuild_required`` as metadata, not as a persistence
    denylist.
    """

    deployable: dict[str, Any] = {}
    rebuild_required: dict[str, Any] = {}
    unsupported: dict[str, Any] = {}
    reasons: dict[str, str] = {}
    for path, value in _flatten(overrides):
        if path in _SAFE_EXACT_PATHS or _matches_prefix(path, _SAFE_PREFIXES):
            _set_path(deployable, path, value)
        elif _matches_prefix(path, _CROSS_SESSION_PREFIXES):
            _set_path(unsupported, path, value)
            reasons[path] = "CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD"
        else:
            _set_path(rebuild_required, path, value)
            reasons[path] = "REBUILD_REQUIRED"
    return OverridePartition(deployable, rebuild_required, unsupported, reasons)


def deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def production_override_delta(
    overrides: Mapping[str, Any],
    baseline_memory_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Remove values that merely restate the frozen effective baseline."""

    missing = object()
    delta: dict[str, Any] = {}
    for path, value in _flatten(overrides):
        baseline: Any = baseline_memory_config
        for part in path.split("."):
            if not isinstance(baseline, Mapping) or part not in baseline:
                baseline = missing
                break
            baseline = baseline[part]
        if baseline is missing or baseline != value:
            _set_path(delta, path, value)
    return delta


def validate_production_overrides(overrides: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a partial override against repository Production + MemoryConfig."""

    requested = deepcopy(dict(overrides))
    resolved = load_production_memory_config(requested, resolve_environment=False).model_dump(
        mode="json",
        warnings=False,
    )
    normalized: dict[str, Any] = {}
    for path, _value in _flatten(requested):
        value: Any = resolved
        for part in path.split("."):
            value = value[part]
        _set_path(normalized, path, value)
    return normalized


class UserTuningConfigStore:
    def __init__(self, history_db_path: str | Path):
        self.history_db_path = Path(history_db_path).expanduser().resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.history_db_path.is_file():
            raise FileNotFoundError(f"history database does not exist: {self.history_db_path}")
        connection = sqlite3.connect(self.history_db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _create_tables(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memory_tuning_configs (
                user_id TEXT PRIMARY KEY,
                config_version INTEGER NOT NULL,
                config_overrides_json TEXT NOT NULL,
                source_run_dir TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                validation_metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_memory_tuning_config_history (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                config_overrides_json TEXT NOT NULL,
                source_run_dir TEXT NOT NULL,
                dataset_hash TEXT NOT NULL,
                validation_metrics_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT NOT NULL,
                replaced_by_version INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_memory_tuning_history_user_version
            ON user_memory_tuning_config_history(user_id, config_version)
            """
        )

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> ActiveUserConfig | None:
        if row is None:
            return None
        return ActiveUserConfig(
            user_id=str(row["user_id"]),
            config_version=int(row["config_version"]),
            config_overrides=json.loads(row["config_overrides_json"]),
            source_run_dir=str(row["source_run_dir"]),
            dataset_hash=str(row["dataset_hash"]),
            validation_metrics=json.loads(row["validation_metrics_json"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_active(self, user_id: str) -> ActiveUserConfig | None:
        connection = self._connect()
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'user_memory_tuning_configs'"
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT * FROM user_memory_tuning_configs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return self._decode(row)
        finally:
            connection.close()

    def save_active(
        self,
        *,
        user_id: str,
        config_overrides: Mapping[str, Any],
        source_run_dir: str | Path,
        dataset_hash: str,
        validation_metrics: Mapping[str, Any],
    ) -> ActiveUserConfig:
        serialized_config = json.dumps(config_overrides, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        serialized_metrics = json.dumps(
            validation_metrics,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = datetime.now(timezone.utc).isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._create_tables(connection)
            old = connection.execute(
                "SELECT * FROM user_memory_tuning_configs WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            version = int(old["config_version"]) + 1 if old is not None else 1
            if old is not None:
                connection.execute(
                    """
                    INSERT INTO user_memory_tuning_config_history (
                        user_id, config_version, config_overrides_json, source_run_dir,
                        dataset_hash, validation_metrics_json, created_at, updated_at,
                        archived_at, replaced_by_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        old["user_id"],
                        old["config_version"],
                        old["config_overrides_json"],
                        old["source_run_dir"],
                        old["dataset_hash"],
                        old["validation_metrics_json"],
                        old["created_at"],
                        old["updated_at"],
                        now,
                        version,
                    ),
                )
            connection.execute(
                """
                INSERT INTO user_memory_tuning_configs (
                    user_id, config_version, config_overrides_json, source_run_dir,
                    dataset_hash, validation_metrics_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    config_version = excluded.config_version,
                    config_overrides_json = excluded.config_overrides_json,
                    source_run_dir = excluded.source_run_dir,
                    dataset_hash = excluded.dataset_hash,
                    validation_metrics_json = excluded.validation_metrics_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    version,
                    serialized_config,
                    str(Path(source_run_dir).expanduser().resolve()),
                    dataset_hash,
                    serialized_metrics,
                    now,
                    now,
                ),
            )
            connection.commit()
            active = self.get_active(user_id)
            if active is None:
                raise RuntimeError("active user tuning config was not persisted")
            return active
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
