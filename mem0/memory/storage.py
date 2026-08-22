import json
import logging
import sqlite3
import threading
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional

from mem0.configs.predefined_profile_attributes import PREDEFINED_PROFILE_ATTRIBUTES
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_validator import (
    merge_profile_value,
    normalize_profile_value,
    serialize_profile_value,
    validate_attribute_definition,
    validate_operation,
)
from mem0.utils.timestamps import beijing_now, beijing_now_iso, normalize_iso_timestamp_to_beijing

logger = logging.getLogger(__name__)

MIGRATION_STAGE_ACTIVE_STATUSES = frozenset({"pending", "running", "retry"})
MIGRATION_STAGE_SUCCESS_STATUSES = frozenset({"succeeded", "succeeded_degraded"})
MIGRATION_STAGE_TERMINAL_STATUSES = frozenset(
    {
        *MIGRATION_STAGE_SUCCESS_STATUSES,
        "discarded",
    }
)
MIGRATION_PARENT_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "succeeded_degraded",
        "completed_with_loss",
    }
)
PROFILE_ACTIVE_STATUSES = frozenset({"pending", "running", "retry"})
PROFILE_TERMINAL_STATUSES = frozenset({"succeeded", "discarded"})
PROMOTION_ACTIVE_STATUSES = frozenset({"pending", "running", "retry"})
PROMOTION_TERMINAL_STATUSES = frozenset({"succeeded", "discarded"})
LONGTERM_EXTRACTION_ACTIVE_STATUSES = frozenset({"pending", "running", "retry"})
LONGTERM_EXTRACTION_TERMINAL_STATUSES = frozenset({"succeeded", "discarded"})


class IdempotencyConflictError(ValueError):
    """Raised when one idempotency key is reused for a different request."""


class SQLiteManager:
    def __init__(self, db_path: str = ":memory:"):
        self.db_path = db_path
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.Lock()
        self._migrate_history_table()
        self._create_history_table()
        self._create_messages_table()
        self._create_conversation_turns_table()
        self._create_background_job_tables()
        self._create_idempotency_table()
        self._create_profile_tables()
        self._sync_predefined_profile_attributes()

    def _table_columns(self, table: str) -> set[str]:
        return {str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()}

    def _add_missing_columns(self, table: str, definitions: Dict[str, str]) -> set[str]:
        """Add backward-compatible columns before creating indexes that reference them."""
        existing = self._table_columns(table)
        for column, definition in definitions.items():
            if column in existing:
                continue
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            existing.add(column)
        return existing

    def _migrate_history_table(self) -> None:
        """
        If a pre-existing history table had the old group-chat columns,
        rename it, create the new schema, copy the intersecting data, then
        drop the old table.
        """
        with self._lock:
            try:
                # Start a transaction
                self.connection.execute("BEGIN")
                cur = self.connection.cursor()

                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='history'")
                if cur.fetchone() is None:
                    self.connection.execute("COMMIT")
                    return  # nothing to migrate

                cur.execute("PRAGMA table_info(history)")
                old_cols = {row[1] for row in cur.fetchall()}

                expected_cols = {
                    "id",
                    "memory_id",
                    "old_memory",
                    "new_memory",
                    "event",
                    "created_at",
                    "updated_at",
                    "is_deleted",
                    "actor_id",
                    "role",
                }

                if old_cols == expected_cols:
                    self.connection.execute("COMMIT")
                    return

                logger.info("Migrating history table to new schema (no convo columns).")

                # Clean up any existing history_old table from previous failed migration
                cur.execute("DROP TABLE IF EXISTS history_old")

                # Rename the current history table
                cur.execute("ALTER TABLE history RENAME TO history_old")

                # Create the new history table with updated schema
                cur.execute(
                    """
                    CREATE TABLE history (
                        id           TEXT PRIMARY KEY,
                        memory_id    TEXT,
                        old_memory   TEXT,
                        new_memory   TEXT,
                        event        TEXT,
                        created_at   DATETIME,
                        updated_at   DATETIME,
                        is_deleted   INTEGER,
                        actor_id     TEXT,
                        role         TEXT
                    )
                """
                )

                # Copy data from old table to new table
                intersecting = list(expected_cols & old_cols)
                if intersecting:
                    cols_csv = ", ".join(intersecting)
                    cur.execute(f"INSERT INTO history ({cols_csv}) SELECT {cols_csv} FROM history_old")

                # Drop the old table
                cur.execute("DROP TABLE history_old")

                # Commit the transaction
                self.connection.execute("COMMIT")
                logger.info("History table migration completed successfully.")

            except Exception as e:
                # Rollback the transaction on any error
                self.connection.execute("ROLLBACK")
                logger.error(f"History table migration failed: {e}")
                raise

    def _create_history_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS history (
                        id           TEXT PRIMARY KEY,
                        memory_id    TEXT,
                        old_memory   TEXT,
                        new_memory   TEXT,
                        event        TEXT,
                        created_at   DATETIME,
                        updated_at   DATETIME,
                        is_deleted   INTEGER,
                        actor_id     TEXT,
                        role         TEXT
                    )
                """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to create history table: {e}")
                raise

    def _create_messages_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_scope TEXT,
                        role TEXT,
                        content TEXT,
                        name TEXT,
                        created_at DATETIME,
                        turn_index INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        migration_job_id TEXT,
                        source_operation_key TEXT,
                        source_message_index INTEGER
                    )
                """
                )
                columns_before = self._table_columns("messages")
                self._add_missing_columns(
                    "messages",
                    {
                        "name": "TEXT",
                        "turn_index": "INTEGER NOT NULL DEFAULT 0",
                        "status": "TEXT NOT NULL DEFAULT 'active'",
                        "migration_job_id": "TEXT",
                        "source_operation_key": "TEXT",
                        "source_message_index": "INTEGER",
                    },
                )
                if "turn_index" not in columns_before:
                    self._backfill_legacy_message_turn_indices()
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_scope_status
                    ON messages(session_scope, status, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_migration_job
                    ON messages(migration_job_id)
                    """
                )
                self.connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_source_operation
                    ON messages(source_operation_key, source_message_index)
                    WHERE source_operation_key IS NOT NULL
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to create messages table: {e}")
                raise

    def _backfill_legacy_message_turn_indices(self) -> None:
        """Assign stable QA turn indices when upgrading pre-turn-index message rows."""
        scopes = self.connection.execute(
            "SELECT DISTINCT session_scope FROM messages WHERE session_scope IS NOT NULL"
        ).fetchall()
        for (session_scope,) in scopes:
            rows = self.connection.execute(
                """
                SELECT rowid, role FROM messages
                WHERE session_scope = ?
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (session_scope,),
            ).fetchall()
            turn_index = 0
            open_user_turn = False
            for rowid, role in rows:
                if role == "user":
                    turn_index += 1
                    open_user_turn = True
                elif role == "assistant":
                    if not open_user_turn:
                        turn_index += 1
                    open_user_turn = False
                elif turn_index == 0:
                    turn_index = 1
                self.connection.execute(
                    "UPDATE messages SET turn_index = ? WHERE rowid = ?",
                    (turn_index, rowid),
                )

    def _create_conversation_turns_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_turns (
                        session_scope TEXT PRIMARY KEY,
                        current_turn_index INTEGER NOT NULL DEFAULT 0,
                        open_turn_index INTEGER
                    )
                    """
                )
                self.connection.execute(
                    """
                    INSERT INTO conversation_turns (session_scope, current_turn_index, open_turn_index)
                    SELECT session_scope, MAX(turn_index),
                           CASE
                               WHEN SUM(CASE WHEN turn_index = max_turn AND role = 'user' THEN 1 ELSE 0 END) > 0
                                AND SUM(CASE WHEN turn_index = max_turn AND role = 'assistant' THEN 1 ELSE 0 END) = 0
                               THEN max_turn
                               ELSE NULL
                           END
                    FROM (
                        SELECT messages.*,
                               MAX(turn_index) OVER (PARTITION BY session_scope) AS max_turn
                        FROM messages
                        WHERE session_scope IS NOT NULL AND turn_index > 0
                    )
                    GROUP BY session_scope
                    ON CONFLICT(session_scope) DO NOTHING
                    """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def _create_background_job_tables(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_migration_jobs (
                        job_id TEXT PRIMARY KEY,
                        session_scope TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        midterm_status TEXT NOT NULL DEFAULT 'pending',
                        midterm_attempts INTEGER NOT NULL DEFAULT 0,
                        midterm_next_retry_at TEXT,
                        midterm_last_error TEXT,
                        midterm_degraded INTEGER NOT NULL DEFAULT 0,
                        midterm_started_at TEXT,
                        midterm_finished_at TEXT,
                        midterm_lease_token TEXT,
                        midterm_heartbeat_at TEXT,
                        midterm_lease_expires_at TEXT,
                        midterm_recovery_count INTEGER NOT NULL DEFAULT 0,
                        midterm_force_degraded INTEGER NOT NULL DEFAULT 0,
                        midterm_cleanup_error TEXT,
                        longterm_status TEXT NOT NULL DEFAULT 'pending',
                        longterm_attempts INTEGER NOT NULL DEFAULT 0,
                        longterm_next_retry_at TEXT,
                        longterm_last_error TEXT,
                        longterm_degraded INTEGER NOT NULL DEFAULT 0,
                        longterm_started_at TEXT,
                        longterm_finished_at TEXT,
                        longterm_lease_token TEXT,
                        longterm_heartbeat_at TEXT,
                        longterm_lease_expires_at TEXT,
                        longterm_recovery_count INTEGER NOT NULL DEFAULT 0,
                        longterm_force_degraded INTEGER NOT NULL DEFAULT 0,
                        longterm_cleanup_error TEXT,
                        filters_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        infer INTEGER NOT NULL DEFAULT 1,
                        prompt TEXT,
                        sequence_no INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        finalized_at TEXT,
                        source_operation_key TEXT,
                        UNIQUE(session_scope, sequence_no),
                        CHECK (
                            status IN (
                                'pending', 'running', 'retry', 'succeeded',
                                'succeeded_degraded', 'completed_with_loss'
                            )
                        ),
                        CHECK (
                            midterm_status IN (
                                'pending', 'running', 'retry', 'succeeded',
                                'succeeded_degraded', 'discarded'
                            )
                        ),
                        CHECK (
                            longterm_status IN (
                                'pending', 'running', 'retry', 'succeeded',
                                'succeeded_degraded', 'discarded'
                            )
                        )
                    )
                    """
                )
                migration_columns_before = self._table_columns("memory_migration_jobs")
                self._add_missing_columns(
                    "memory_migration_jobs",
                    {
                        "midterm_status": "TEXT NOT NULL DEFAULT 'pending'",
                        "midterm_attempts": "INTEGER NOT NULL DEFAULT 0",
                        "midterm_next_retry_at": "TEXT",
                        "midterm_last_error": "TEXT",
                        "midterm_degraded": "INTEGER NOT NULL DEFAULT 0",
                        "midterm_started_at": "TEXT",
                        "midterm_finished_at": "TEXT",
                        "midterm_lease_token": "TEXT",
                        "midterm_heartbeat_at": "TEXT",
                        "midterm_lease_expires_at": "TEXT",
                        "midterm_recovery_count": "INTEGER NOT NULL DEFAULT 0",
                        "midterm_force_degraded": "INTEGER NOT NULL DEFAULT 0",
                        "midterm_cleanup_error": "TEXT",
                        "longterm_status": "TEXT NOT NULL DEFAULT 'pending'",
                        "longterm_attempts": "INTEGER NOT NULL DEFAULT 0",
                        "longterm_next_retry_at": "TEXT",
                        "longterm_last_error": "TEXT",
                        "longterm_degraded": "INTEGER NOT NULL DEFAULT 0",
                        "longterm_started_at": "TEXT",
                        "longterm_finished_at": "TEXT",
                        "longterm_lease_token": "TEXT",
                        "longterm_heartbeat_at": "TEXT",
                        "longterm_lease_expires_at": "TEXT",
                        "longterm_recovery_count": "INTEGER NOT NULL DEFAULT 0",
                        "longterm_force_degraded": "INTEGER NOT NULL DEFAULT 0",
                        "longterm_cleanup_error": "TEXT",
                        "finalized_at": "TEXT",
                        "source_operation_key": "TEXT",
                    },
                )
                if "midterm_status" not in migration_columns_before:
                    self._migrate_legacy_migration_job_state(migration_columns_before)
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_midterm_claim
                    ON memory_migration_jobs(midterm_status, midterm_next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_longterm_claim
                    ON memory_migration_jobs(longterm_status, longterm_next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS longterm_extraction_jobs (
                        job_id TEXT PRIMARY KEY,
                        session_scope TEXT NOT NULL,
                        turn_index INTEGER NOT NULL,
                        messages_json TEXT NOT NULL,
                        filters_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        infer INTEGER NOT NULL DEFAULT 1,
                        prompt TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        lease_token TEXT,
                        heartbeat_at TEXT,
                        lease_expires_at TEXT,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        sequence_no INTEGER NOT NULL,
                        source_operation_key TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_scope, turn_index),
                        CHECK (status IN ('pending', 'running', 'retry', 'succeeded', 'discarded'))
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_longterm_extraction_jobs_claim
                    ON longterm_extraction_jobs(status, next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_longterm_extraction_jobs_source_operation
                    ON longterm_extraction_jobs(source_operation_key, turn_index)
                    WHERE source_operation_key IS NOT NULL
                    """
                )
                self.connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_migration_jobs_source_operation
                    ON memory_migration_jobs(source_operation_key)
                    WHERE source_operation_key IS NOT NULL
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_update_jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        messages_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error TEXT,
                        started_at TEXT,
                        lease_token TEXT,
                        heartbeat_at TEXT,
                        lease_expires_at TEXT,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        sequence_no INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        source_operation_key TEXT,
                        UNIQUE(user_id, sequence_no),
                        CHECK (
                            status IN (
                                'pending', 'running', 'retry', 'succeeded', 'discarded'
                            )
                        )
                    )
                    """
                )
                self._add_missing_columns(
                    "profile_update_jobs",
                    {
                        "started_at": "TEXT",
                        "lease_token": "TEXT",
                        "heartbeat_at": "TEXT",
                        "lease_expires_at": "TEXT",
                        "recovery_count": "INTEGER NOT NULL DEFAULT 0",
                        "source_operation_key": "TEXT",
                    },
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_profile_jobs_claim
                    ON profile_update_jobs(status, next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_jobs_source_operation
                    ON profile_update_jobs(source_operation_key)
                    WHERE source_operation_key IS NOT NULL
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_promotion_jobs (
                        job_id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        source_midterm_session_id TEXT NOT NULL,
                        source_run_id TEXT,
                        source_version TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error TEXT,
                        started_at TEXT,
                        finished_at TEXT,
                        lease_token TEXT,
                        heartbeat_at TEXT,
                        lease_expires_at TEXT,
                        recovery_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, source_midterm_session_id, source_version),
                        CHECK (status IN ('pending', 'running', 'retry', 'succeeded', 'discarded'))
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_promotion_jobs_claim
                    ON memory_promotion_jobs(status, next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_promotion_jobs_source_session
                    ON memory_promotion_jobs(user_id, source_midterm_session_id, created_at)
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create background job tables: %s", e)
                raise

    def _migrate_legacy_migration_job_state(self, legacy_columns: set[str]) -> None:
        """Project the single-state legacy worker schema into the two stage states."""
        midterm_done = "COALESCE(midterm_done, 0)" if "midterm_done" in legacy_columns else "0"
        longterm_done = "COALESCE(longterm_done, 0)" if "longterm_done" in legacy_columns else "0"
        attempts = "COALESCE(attempts, 0)" if "attempts" in legacy_columns else "0"
        next_retry_at = "next_retry_at" if "next_retry_at" in legacy_columns else "NULL"
        last_error = "last_error" if "last_error" in legacy_columns else "NULL"
        degraded = "COALESCE(degraded, 0)" if "degraded" in legacy_columns else "0"
        self.connection.execute(
            f"""
            UPDATE memory_migration_jobs
            SET midterm_status = CASE
                    WHEN {midterm_done} = 1 OR status = 'succeeded' THEN 'succeeded'
                    WHEN status = 'discarded' THEN 'discarded'
                    WHEN status IN ('running', 'retry') THEN 'retry'
                    ELSE 'pending'
                END,
                longterm_status = CASE
                    WHEN {longterm_done} = 1 OR status = 'succeeded' THEN 'succeeded'
                    WHEN status = 'discarded' THEN 'discarded'
                    WHEN status IN ('running', 'retry') THEN 'retry'
                    ELSE 'pending'
                END,
                midterm_attempts = {attempts},
                longterm_attempts = {attempts},
                midterm_next_retry_at = {next_retry_at},
                longterm_next_retry_at = {next_retry_at},
                midterm_last_error = {last_error},
                longterm_last_error = {last_error},
                midterm_degraded = {degraded},
                longterm_degraded = {degraded},
                finalized_at = CASE
                    WHEN status IN ('succeeded', 'discarded') THEN updated_at
                    ELSE finalized_at
                END
            """
        )

    def _create_idempotency_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_idempotency_operations (
                        idempotency_key TEXT PRIMARY KEY,
                        operation_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        result_json TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (status IN ('processing', 'succeeded', 'failed'))
                    )
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create idempotency table: %s", e)
                raise

    def _create_profile_tables(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_attributes (
                        attribute_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        attribute_key TEXT NOT NULL UNIQUE,
                        attribute_name TEXT NOT NULL,
                        attribute_category TEXT NOT NULL,
                        description TEXT NOT NULL,
                        value_type TEXT NOT NULL,
                        value_schema_json TEXT NOT NULL,
                        merge_policy TEXT NOT NULL DEFAULT 'replace',
                        is_predefined INTEGER NOT NULL DEFAULT 0,
                        is_active INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        CHECK (
                            value_type IN (
                                'string', 'number', 'boolean', 'string_list',
                                'number_list', 'object', 'object_list'
                            )
                        ),
                        CHECK (merge_policy IN ('replace', 'append_unique')),
                        CHECK (is_predefined IN (0, 1)),
                        CHECK (is_active IN (0, 1)),
                        CHECK (json_valid(value_schema_json))
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_profile_values (
                        user_id TEXT NOT NULL,
                        attribute_id INTEGER NOT NULL,
                        value_json TEXT NOT NULL,
                        source_type TEXT NOT NULL DEFAULT 'explicit',
                        confidence REAL NOT NULL DEFAULT 1.0,
                        value_version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, attribute_id),
                        FOREIGN KEY (attribute_id)
                            REFERENCES profile_attributes(attribute_id)
                            ON DELETE CASCADE,
                        CHECK (source_type IN ('explicit', 'inferred', 'repeated', 'correction', 'imported')),
                        CHECK (confidence >= 0 AND confidence <= 1),
                        CHECK (value_version >= 1),
                        CHECK (json_valid(value_json))
                    )
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create profile tables: %s", e)
                raise

    def _sync_predefined_profile_attributes(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                now = beijing_now_iso()
                for raw_definition in PREDEFINED_PROFILE_ATTRIBUTES:
                    definition = validate_attribute_definition(raw_definition)
                    self.connection.execute(
                        """
                        INSERT INTO profile_attributes (
                            attribute_key, attribute_name, attribute_category, description,
                            value_type, value_schema_json, merge_policy, is_predefined,
                            is_active, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
                        ON CONFLICT(attribute_key) DO UPDATE SET
                            attribute_name = excluded.attribute_name,
                            attribute_category = excluded.attribute_category,
                            description = excluded.description,
                            value_type = excluded.value_type,
                            value_schema_json = excluded.value_schema_json,
                            merge_policy = excluded.merge_policy,
                            is_predefined = 1,
                            is_active = 1,
                            updated_at = excluded.updated_at
                        """,
                        (
                            definition.attribute_key,
                            definition.attribute_name,
                            definition.attribute_category,
                            definition.description,
                            definition.value_type,
                            serialize_profile_value(definition.value_schema),
                            definition.merge_policy,
                            now,
                            now,
                        ),
                    )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to synchronize predefined profile attributes: %s", e)
                raise

    def add_history(
        self,
        memory_id: str,
        old_memory: Optional[str],
        new_memory: Optional[str],
        event: str,
        *,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        is_deleted: int = 0,
        actor_id: Optional[str] = None,
        role: Optional[str] = None,
    ) -> None:
        created_at = normalize_iso_timestamp_to_beijing(created_at)
        updated_at = normalize_iso_timestamp_to_beijing(updated_at)
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    INSERT INTO history (
                        id, memory_id, old_memory, new_memory, event,
                        created_at, updated_at, is_deleted, actor_id, role
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        str(uuid.uuid4()),
                        memory_id,
                        old_memory,
                        new_memory,
                        event,
                        created_at,
                        updated_at,
                        is_deleted,
                        actor_id,
                        role,
                    ),
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to add history record: {e}")
                raise

    def batch_add_history(self, records: List[Dict[str, Any]]) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.executemany(
                    """
                    INSERT INTO history (
                        id, memory_id, old_memory, new_memory, event,
                        created_at, updated_at, is_deleted, actor_id, role
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        (
                            str(uuid.uuid4()),
                            record.get("memory_id"),
                            record.get("old_memory"),
                            record.get("new_memory"),
                            record.get("event"),
                            normalize_iso_timestamp_to_beijing(record.get("created_at")),
                            normalize_iso_timestamp_to_beijing(record.get("updated_at")),
                            record.get("is_deleted", 0),
                            record.get("actor_id"),
                            record.get("role"),
                        )
                        for record in records
                    ],
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to batch add history records: {e}")
                raise

    def get_history(self, memory_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self.connection.execute(
                """
                SELECT id, memory_id, old_memory, new_memory, event,
                       created_at, updated_at, is_deleted, actor_id, role
                FROM history
                WHERE memory_id = ?
                ORDER BY created_at ASC, DATETIME(updated_at) ASC
            """,
                (memory_id,),
            )
            rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "memory_id": r[1],
                "old_memory": r[2],
                "new_memory": r[3],
                "event": r[4],
                "created_at": r[5],
                "updated_at": r[6],
                "is_deleted": bool(r[7]),
                "actor_id": r[8],
                "role": r[9],
            }
            for r in rows
        ]

    def delete_history_for_memory_ids(self, memory_ids: List[str]) -> int:
        """Delete derived history rows during an idempotent background-output cleanup."""
        ids = [str(memory_id) for memory_id in memory_ids if memory_id]
        if not ids:
            return 0
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                placeholders = ",".join("?" for _ in ids)
                cursor = self.connection.execute(
                    f"DELETE FROM history WHERE memory_id IN ({placeholders})",
                    tuple(ids),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def save_messages(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        max_messages: int = 10,
        return_evicted: bool = False,
    ) -> Optional[List[Dict[str, Any]]]:
        if not messages:
            return [] if return_evicted else None
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self._insert_messages_in_transaction(messages, session_scope, source_operation_key=None)
                evicted_messages = self._evict_active_messages_in_transaction(session_scope, max_messages)

                if not return_evicted:
                    evicted_messages = None

                self.connection.execute("COMMIT")
                return evicted_messages
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to save messages: {e}")
                raise

    def save_messages_and_get_completed_qa(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        max_messages: int = 10,
    ) -> tuple[List[Dict[str, Any]], List[tuple[int, List[Dict[str, Any]]]]]:
        """Persist messages and return newly completed QA turns plus active overflow.

        This is the direct-execution counterpart to durable LongTerm job creation:
        both paths use the same turn allocator and complete-QA selector.
        """
        if not messages:
            return [], []
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                touched_turn_indices = self._insert_messages_in_transaction(
                    messages,
                    session_scope,
                    source_operation_key=None,
                )
                completed_qa = self._complete_qa_messages_in_transaction(session_scope, touched_turn_indices)
                evicted_messages = self._evict_active_messages_in_transaction(session_scope, max_messages)
                self.connection.execute("COMMIT")
                return evicted_messages, completed_qa
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to save messages and collect completed QA for %s: %s", session_scope, e)
                raise

    def _evict_active_messages_in_transaction(
        self,
        session_scope: str,
        max_messages: int,
    ) -> List[Dict[str, Any]]:
        max_messages = max(int(max_messages), 0)
        rows = self.connection.execute(
            """
            SELECT id, role, content, name, created_at, turn_index
            FROM messages
            WHERE session_scope = ? AND status = 'active'
            ORDER BY DATETIME(created_at) ASC, rowid ASC
            """,
            (session_scope,),
        ).fetchall()

        evict_count = max(len(rows) - max_messages, 0)
        evicted_rows = rows[:evict_count]
        evicted_ids = [row[0] for row in evicted_rows]
        if evicted_ids:
            placeholders = ",".join("?" for _ in evicted_ids)
            self.connection.execute(
                f"""
                DELETE FROM messages
                WHERE session_scope = ? AND id IN ({placeholders})
                """,
                (session_scope, *evicted_ids),
            )

        return [
            {
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "name": row[3],
                "created_at": row[4],
                "turn_index": row[5],
                "session_scope": session_scope,
            }
            for row in evicted_rows
        ]

    def get_messages(self, session_scope: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self.connection.execute(
                """
                SELECT id, role, content, name, created_at, turn_index
                FROM messages
                WHERE session_scope = ? AND status = 'active'
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                LIMIT ?
            """,
                (session_scope, limit),
            )
            rows = cur.fetchall()

        return [
            {
                "id": r[0],
                "role": r[1],
                "content": r[2],
                "name": r[3],
                "created_at": r[4],
                "turn_index": r[5],
                "session_scope": session_scope,
            }
            for r in rows
        ]

    def delete_messages(self, message_ids: List[str]) -> int:
        if not message_ids:
            return 0
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                placeholders = ",".join("?" for _ in message_ids)
                cursor = self.connection.execute(
                    f"""
                    DELETE FROM messages
                    WHERE id IN ({placeholders})
                """,
                    tuple(message_ids),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to delete messages: {e}")
                raise

    def get_last_messages(self, session_scope: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            # Subquery picks the latest N rows (DESC + LIMIT), outer query
            # re-sorts them chronologically (ASC) for the caller.
            cur = self.connection.execute(
                """
                SELECT role, content, name, created_at, turn_index FROM (
                    SELECT rowid, role, content, name, created_at, turn_index
                    FROM messages
                    WHERE session_scope = ? AND status = 'active'
                    ORDER BY DATETIME(created_at) DESC, rowid DESC
                    LIMIT ?
                ) ORDER BY DATETIME(created_at) ASC, rowid ASC
            """,
                (session_scope, limit),
            )
            rows = cur.fetchall()

        return [
            {
                "role": r[0],
                "content": r[1],
                "name": r[2],
                "created_at": r[3],
                "turn_index": r[4],
            }
            for r in rows
        ]

    @staticmethod
    def _json_dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    @staticmethod
    def _row_as_dict(cursor: sqlite3.Cursor, row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {description[0]: value for description, value in zip(cursor.description, row)}

    @staticmethod
    def _decode_migration_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        decoded = dict(job)
        decoded["filters"] = json.loads(decoded.pop("filters_json"))
        decoded["metadata"] = json.loads(decoded.pop("metadata_json"))
        decoded["infer"] = bool(decoded["infer"])
        decoded["midterm_degraded"] = bool(decoded["midterm_degraded"])
        decoded["longterm_degraded"] = bool(decoded["longterm_degraded"])
        return decoded

    @staticmethod
    def _decode_profile_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        decoded = dict(job)
        decoded["messages"] = json.loads(decoded.pop("messages_json"))
        return decoded

    @staticmethod
    def _decode_longterm_extraction_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        decoded = dict(job)
        decoded["messages"] = json.loads(decoded.pop("messages_json"))
        decoded["filters"] = json.loads(decoded.pop("filters_json"))
        decoded["metadata"] = json.loads(decoded.pop("metadata_json"))
        decoded["infer"] = bool(decoded["infer"])
        return decoded

    def get_idempotency_operation(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        """Return one persisted operation without changing its state."""
        with self._lock:
            cursor = self.connection.execute(
                """
                SELECT idempotency_key, operation_type, status, request_hash,
                       result_json, error_message, created_at, updated_at
                FROM memory_idempotency_operations
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            )
            row = self._row_as_dict(cursor, cursor.fetchone())
        if row is not None and row.get("result_json") is not None:
            row["result"] = json.loads(row["result_json"])
        return row

    def save_background_add_idempotently(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        *,
        idempotency_key: str,
        request_hash: str,
        max_messages: int,
        filters: Dict[str, Any],
        metadata: Dict[str, Any],
        infer: bool,
        prompt: Optional[str],
        profile_user_id: Optional[str],
    ) -> Dict[str, Any]:
        """Atomically persist one background add and its reusable result."""
        if not messages:
            raise ValueError("messages are required for an idempotent background add")
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        operation_type = "memory.add"
        owns_operation = False

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    """
                    SELECT operation_type, status, request_hash, result_json
                    FROM memory_idempotency_operations
                    WHERE idempotency_key = ?
                    """,
                    (idempotency_key,),
                )
                operation = self._row_as_dict(cursor, cursor.fetchone())
                if operation is not None:
                    if operation["operation_type"] != operation_type or operation["request_hash"] != request_hash:
                        raise IdempotencyConflictError(
                            f"Idempotency key conflicts with a different request: idempotency_key={idempotency_key}"
                        )
                    if operation["status"] == "succeeded":
                        result = json.loads(operation["result_json"])
                        self.connection.execute("COMMIT")
                        return result

                    recovered = self._recover_idempotent_background_add(idempotency_key, len(messages))
                    if recovered is not None:
                        result_json = self._json_dumps(recovered)
                        now = beijing_now_iso()
                        self.connection.execute(
                            """
                            UPDATE memory_idempotency_operations
                            SET status = 'succeeded', result_json = ?, error_message = NULL, updated_at = ?
                            WHERE idempotency_key = ?
                            """,
                            (result_json, now, idempotency_key),
                        )
                        self.connection.execute("COMMIT")
                        return json.loads(result_json)

                    self.connection.execute(
                        """
                        UPDATE memory_idempotency_operations
                        SET status = 'processing', result_json = NULL,
                            error_message = NULL, updated_at = ?
                        WHERE idempotency_key = ?
                        """,
                        (beijing_now_iso(), idempotency_key),
                    )
                else:
                    now = beijing_now_iso()
                    self.connection.execute(
                        """
                        INSERT INTO memory_idempotency_operations (
                            idempotency_key, operation_type, status, request_hash,
                            result_json, error_message, created_at, updated_at
                        ) VALUES (?, ?, 'processing', ?, NULL, NULL, ?, ?)
                        """,
                        (idempotency_key, operation_type, request_hash, now, now),
                    )
                owns_operation = True

                touched_turn_indices = self._insert_messages_in_transaction(
                    messages,
                    session_scope,
                    source_operation_key=idempotency_key,
                )

                self._create_longterm_extraction_jobs_in_transaction(
                    session_scope,
                    touched_turn_indices,
                    filters=filters,
                    metadata=metadata,
                    infer=infer,
                    prompt=prompt,
                    source_operation_key=idempotency_key,
                )

                migration_job_id = self._reserve_migration_job_in_transaction(
                    session_scope,
                    max_messages=max_messages,
                    filters=filters,
                    metadata=metadata,
                    infer=infer,
                    prompt=prompt,
                    source_operation_key=idempotency_key,
                    skip_longterm_stage=True,
                )
                profile_job_id = None
                if profile_user_id:
                    profile_job_id = self._create_profile_job_in_transaction(
                        profile_user_id,
                        messages,
                        source_operation_key=idempotency_key,
                    )

                result = {
                    "results": [],
                    "background": {
                        "migration_job_id": migration_job_id,
                        "profile_job_id": profile_job_id,
                    },
                }
                result_json = self._json_dumps(result)
                self.connection.execute(
                    """
                    UPDATE memory_idempotency_operations
                    SET status = 'succeeded', result_json = ?, error_message = NULL, updated_at = ?
                    WHERE idempotency_key = ?
                    """,
                    (result_json, beijing_now_iso(), idempotency_key),
                )
                self.connection.execute("COMMIT")
                return json.loads(result_json)
            except Exception as exc:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                if owns_operation:
                    self._record_failed_idempotency_operation(
                        idempotency_key,
                        operation_type,
                        request_hash,
                        exc,
                    )
                raise

    def _recover_idempotent_background_add(
        self,
        idempotency_key: str,
        expected_message_count: int,
    ) -> Optional[Dict[str, Any]]:
        """Recover an operation whose side effects are already durably present."""
        message_count = self.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE source_operation_key = ?",
            (idempotency_key,),
        ).fetchone()[0]
        migration_row = self.connection.execute(
            "SELECT job_id FROM memory_migration_jobs WHERE source_operation_key = ?",
            (idempotency_key,),
        ).fetchone()
        profile_row = self.connection.execute(
            "SELECT job_id FROM profile_update_jobs WHERE source_operation_key = ?",
            (idempotency_key,),
        ).fetchone()
        longterm_rows = self.connection.execute(
            """
            SELECT job_id FROM longterm_extraction_jobs
            WHERE source_operation_key = ? ORDER BY sequence_no, rowid
            """,
            (idempotency_key,),
        ).fetchall()
        if message_count == 0 and migration_row is None and profile_row is None and not longterm_rows:
            return None
        if message_count not in {0, expected_message_count}:
            raise RuntimeError(
                "Cannot safely recover a partially persisted idempotent add: "
                f"idempotency_key={idempotency_key} messages={message_count}/{expected_message_count}"
            )
        return {
            "results": [],
            "background": {
                "migration_job_id": migration_row[0] if migration_row else None,
                "profile_job_id": profile_row[0] if profile_row else None,
            },
        }

    def _record_failed_idempotency_operation(
        self,
        idempotency_key: str,
        operation_type: str,
        request_hash: str,
        error: BaseException,
    ) -> None:
        now = beijing_now_iso()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                INSERT INTO memory_idempotency_operations (
                    idempotency_key, operation_type, status, request_hash,
                    result_json, error_message, created_at, updated_at
                ) VALUES (?, ?, 'failed', ?, NULL, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = CASE
                        WHEN memory_idempotency_operations.status = 'succeeded'
                        THEN memory_idempotency_operations.status
                        ELSE 'failed'
                    END,
                    error_message = CASE
                        WHEN memory_idempotency_operations.status = 'succeeded'
                        THEN memory_idempotency_operations.error_message
                        ELSE excluded.error_message
                    END,
                    updated_at = excluded.updated_at
                WHERE memory_idempotency_operations.operation_type = excluded.operation_type
                  AND memory_idempotency_operations.request_hash = excluded.request_hash
                """,
                (
                    idempotency_key,
                    operation_type,
                    request_hash,
                    f"{type(error).__name__}: {error}",
                    now,
                    now,
                ),
            )
            self.connection.execute("COMMIT")
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            logger.exception("Failed to persist idempotency failure for key=%s", idempotency_key)

    def _insert_messages_in_transaction(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        *,
        source_operation_key: Optional[str],
    ) -> List[int]:
        state = self.connection.execute(
            """
            SELECT current_turn_index, open_turn_index
            FROM conversation_turns
            WHERE session_scope = ?
            """,
            (session_scope,),
        ).fetchone()
        current_turn_index = int(state[0]) if state else 0
        open_turn_index = int(state[1]) if state and state[1] is not None else None

        touched_turn_indices = set()
        for index, message in enumerate(messages):
            role = message.get("role")
            if role == "user":
                current_turn_index += 1
                open_turn_index = current_turn_index
                turn_index = current_turn_index
            elif role == "assistant":
                if open_turn_index is None:
                    current_turn_index += 1
                    open_turn_index = current_turn_index
                turn_index = open_turn_index
                open_turn_index = None
            else:
                turn_index = open_turn_index if open_turn_index is not None else current_turn_index
            if role in {"user", "assistant"} and turn_index > 0:
                touched_turn_indices.add(int(turn_index))

            created_at = normalize_iso_timestamp_to_beijing(message.get("created_at")) or beijing_now_iso()
            self.connection.execute(
                """
                INSERT INTO messages (
                    id, session_scope, role, content, name, created_at, turn_index, status,
                    migration_job_id, source_operation_key, source_message_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    session_scope,
                    role,
                    message.get("content"),
                    message.get("name"),
                    created_at,
                    turn_index,
                    source_operation_key,
                    index if source_operation_key is not None else None,
                ),
            )

        self.connection.execute(
            """
            INSERT INTO conversation_turns (session_scope, current_turn_index, open_turn_index)
            VALUES (?, ?, ?)
            ON CONFLICT(session_scope) DO UPDATE SET
                current_turn_index = excluded.current_turn_index,
                open_turn_index = excluded.open_turn_index
            """,
            (session_scope, current_turn_index, open_turn_index),
        )
        return sorted(touched_turn_indices)

    def _complete_qa_messages_in_transaction(
        self,
        session_scope: str,
        turn_indices: List[int],
    ) -> List[tuple[int, List[Dict[str, Any]]]]:
        """Return only touched turns that durably contain both User and Assistant."""
        completed = []
        for turn_index in sorted({int(value) for value in turn_indices if int(value) > 0}):
            rows = self.connection.execute(
                """
                SELECT role, content, name, created_at, turn_index, status
                FROM messages
                WHERE session_scope = ? AND turn_index = ? AND role IN ('user', 'assistant')
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (session_scope, turn_index),
            ).fetchall()
            roles = {row[0] for row in rows}
            if not {"user", "assistant"} <= roles:
                continue
            messages = [
                {
                    "role": row[0],
                    "content": row[1],
                    "name": row[2],
                    "created_at": row[3],
                    "turn_index": row[4],
                    "status": row[5],
                }
                for row in rows
            ]
            completed.append((turn_index, messages))
        return completed

    def _create_longterm_extraction_jobs_in_transaction(
        self,
        session_scope: str,
        turn_indices: List[int],
        *,
        filters: Dict[str, Any],
        metadata: Dict[str, Any],
        infer: bool,
        prompt: Optional[str],
        source_operation_key: Optional[str],
    ) -> List[str]:
        job_ids = []
        now = beijing_now_iso()
        for turn_index, messages in self._complete_qa_messages_in_transaction(session_scope, turn_indices):
            job_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"mem0:longterm-extraction-job:{session_scope}:{turn_index}",
                )
            )
            job_metadata = {**metadata, "source_turn_index": turn_index}
            self.connection.execute(
                """
                INSERT INTO longterm_extraction_jobs (
                    job_id, session_scope, turn_index, messages_json,
                    filters_json, metadata_json, infer, prompt, status,
                    attempts, sequence_no, source_operation_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(session_scope, turn_index) DO NOTHING
                """,
                (
                    job_id,
                    session_scope,
                    turn_index,
                    self._json_dumps(messages),
                    self._json_dumps(filters),
                    self._json_dumps(job_metadata),
                    int(bool(infer)),
                    prompt,
                    turn_index,
                    source_operation_key,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                """
                SELECT job_id FROM longterm_extraction_jobs
                WHERE session_scope = ? AND turn_index = ?
                """,
                (session_scope, turn_index),
            ).fetchone()
            if row is not None:
                job_ids.append(str(row[0]))
        return job_ids

    def current_turn_index(self, session_scope: str) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT current_turn_index FROM conversation_turns WHERE session_scope = ?",
                (session_scope,),
            ).fetchone()
        return int(row[0]) if row else 0

    def _reserve_migration_job_in_transaction(
        self,
        session_scope: str,
        *,
        max_messages: int,
        filters: Dict[str, Any],
        metadata: Dict[str, Any],
        infer: bool,
        prompt: Optional[str],
        source_operation_key: Optional[str],
        skip_longterm_stage: bool = False,
    ) -> Optional[str]:
        active_rows = self.connection.execute(
            """
            SELECT rowid, id, role
            FROM messages
            WHERE session_scope = ? AND status = 'active'
            ORDER BY DATETIME(created_at) ASC, rowid ASC
            """,
            (session_scope,),
        ).fetchall()
        overflow_count = max(len(active_rows) - max(int(max_messages), 0), 0)
        reserved_rows = active_rows[:overflow_count]

        if (
            reserved_rows
            and reserved_rows[-1][2] == "user"
            and len(active_rows) > len(reserved_rows)
            and active_rows[len(reserved_rows)][2] == "assistant"
        ):
            reserved_rows = active_rows[: len(reserved_rows) + 1]
        if not reserved_rows:
            return None

        job_id = str(uuid.uuid4())
        sequence_no = self.connection.execute(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1
            FROM memory_migration_jobs
            WHERE session_scope = ?
            """,
            (session_scope,),
        ).fetchone()[0]
        now = beijing_now_iso()
        longterm_status = "succeeded" if skip_longterm_stage else "pending"
        self.connection.execute(
            """
            INSERT INTO memory_migration_jobs (
                job_id, session_scope, status,
                midterm_status, midterm_attempts, midterm_degraded,
                longterm_status, longterm_attempts, longterm_degraded,
                filters_json, metadata_json, infer, prompt, sequence_no,
                created_at, updated_at, source_operation_key
            ) VALUES (
                ?, ?, 'pending',
                'pending', 0, 0,
                ?, 0, 0,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                job_id,
                session_scope,
                longterm_status,
                self._json_dumps(filters),
                self._json_dumps(metadata),
                int(bool(infer)),
                prompt,
                sequence_no,
                now,
                now,
                source_operation_key,
            ),
        )
        message_ids = [row[1] for row in reserved_rows]
        placeholders = ",".join("?" for _ in message_ids)
        self.connection.execute(
            f"""
            UPDATE messages
            SET status = 'pending', migration_job_id = ?
            WHERE id IN ({placeholders}) AND status = 'active'
            """,
            (job_id, *message_ids),
        )
        return job_id

    def _create_profile_job_in_transaction(
        self,
        user_id: str,
        messages: List[Dict[str, Any]],
        *,
        source_operation_key: Optional[str],
    ) -> str:
        sequence_no = self.connection.execute(
            """
            SELECT COALESCE(MAX(sequence_no), 0) + 1
            FROM profile_update_jobs
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()[0]
        job_id = str(uuid.uuid4())
        now = beijing_now_iso()
        self.connection.execute(
            """
            INSERT INTO profile_update_jobs (
                job_id, user_id, messages_json, status, attempts,
                next_retry_at, last_error, sequence_no, created_at, updated_at,
                source_operation_key
            ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                job_id,
                user_id,
                self._json_dumps(messages),
                sequence_no,
                now,
                now,
                source_operation_key,
            ),
        )
        return job_id

    def save_messages_and_create_migration_job(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        *,
        max_messages: int,
        filters: Dict[str, Any],
        metadata: Dict[str, Any],
        infer: bool,
        prompt: Optional[str],
    ) -> Optional[str]:
        """Synchronously save messages and atomically reserve active overflow for migration."""
        if not messages:
            return None

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self._insert_messages_in_transaction(
                    messages,
                    session_scope,
                    source_operation_key=None,
                )
                job_id = self._reserve_migration_job_in_transaction(
                    session_scope,
                    max_messages=max_messages,
                    filters=filters,
                    metadata=metadata,
                    infer=infer,
                    prompt=prompt,
                    source_operation_key=None,
                )
                self.connection.execute("COMMIT")
                return job_id
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to save messages and create migration job for %s: %s", session_scope, e)
                raise

    def save_messages_and_create_background_jobs(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        *,
        max_messages: int,
        filters: Dict[str, Any],
        metadata: Dict[str, Any],
        infer: bool,
        prompt: Optional[str],
        profile_user_id: Optional[str],
        create_longterm_jobs: bool = False,
        return_longterm_job_ids: bool = False,
    ) -> Any:
        """Atomically save short-term messages and enqueue every requested job."""
        if not messages:
            return None, None

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                touched_turn_indices = self._insert_messages_in_transaction(
                    messages,
                    session_scope,
                    source_operation_key=None,
                )
                longterm_job_ids = []
                if create_longterm_jobs:
                    longterm_job_ids = self._create_longterm_extraction_jobs_in_transaction(
                        session_scope,
                        touched_turn_indices,
                        filters=filters,
                        metadata=metadata,
                        infer=infer,
                        prompt=prompt,
                        source_operation_key=None,
                    )
                migration_job_id = self._reserve_migration_job_in_transaction(
                    session_scope,
                    max_messages=max_messages,
                    filters=filters,
                    metadata=metadata,
                    infer=infer,
                    prompt=prompt,
                    source_operation_key=None,
                    skip_longterm_stage=create_longterm_jobs,
                )
                profile_job_id = None
                if profile_user_id is not None:
                    profile_job_id = self._create_profile_job_in_transaction(
                        profile_user_id,
                        messages,
                        source_operation_key=None,
                    )
                self.connection.execute("COMMIT")
                if return_longterm_job_ids:
                    return migration_job_id, profile_job_id, longterm_job_ids
                return migration_job_id, profile_job_id
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                logger.exception("Failed to save messages and enqueue background jobs for %s", session_scope)
                raise

    def create_profile_update_job(
        self,
        user_id: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                job_id = self._create_profile_job_in_transaction(
                    user_id,
                    messages,
                    source_operation_key=None,
                )
                self.connection.execute("COMMIT")
                return job_id
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create profile update job for user %s: %s", user_id, e)
                raise

    def get_migration_job_messages(self, job_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT id, session_scope, role, content, name, created_at, turn_index, status
                FROM messages
                WHERE migration_job_id = ?
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (job_id,),
            ).fetchall()
        return [
            {
                "id": row[0],
                "session_scope": row[1],
                "role": row[2],
                "content": row[3],
                "name": row[4],
                "created_at": row[5],
                "turn_index": row[6],
                "status": row[7],
            }
            for row in rows
        ]

    def get_context_messages(
        self,
        session_scope: str,
        *,
        active_limit: int,
        include_pending: bool,
        max_pending: int,
        include_failed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return the active window plus every source message behind an unfinished migration.

        The legacy context flags remain accepted as configuration compatibility knobs,
        but cannot hide migration sources: doing so would create a context gap while
        either independent stage is pending, running, or retrying.
        """
        _ = include_pending, max_pending, include_failed
        with self._lock:
            active_rows = self.connection.execute(
                """
                SELECT rowid, role, content, name, created_at, turn_index, status FROM (
                    SELECT rowid, role, content, name, created_at, turn_index, status
                    FROM messages
                    WHERE session_scope = ? AND status = 'active'
                    ORDER BY DATETIME(created_at) DESC, rowid DESC
                    LIMIT ?
                ) ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (session_scope, max(int(active_limit), 0)),
            ).fetchall()
            migration_rows = self.connection.execute(
                """
                SELECT m.rowid, m.role, m.content, m.name, m.created_at, m.turn_index, m.status
                FROM messages AS m
                JOIN memory_migration_jobs AS job ON job.job_id = m.migration_job_id
                WHERE m.session_scope = ? AND job.finalized_at IS NULL
                ORDER BY DATETIME(m.created_at) ASC, m.rowid ASC
                """,
                (session_scope,),
            ).fetchall()

        rows_by_id = {row[0]: row for row in [*migration_rows, *active_rows]}
        rows = sorted(rows_by_id.values(), key=lambda row: (row[4] or "", row[0]))
        return [
            {
                "role": row[1],
                "content": row[2],
                "name": row[3],
                "created_at": row[4],
                "turn_index": row[5],
                "status": row[6],
            }
            for row in rows
        ]

    def get_following_qa_messages(
        self,
        session_scope: str,
        after_turn_index: int,
        qa_limit: int,
    ) -> List[Dict[str, Any]]:
        """Return the nearest complete following QA turns across all message states."""
        limit = max(int(qa_limit), 0)
        if limit == 0:
            return []
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT role, content, name, created_at, turn_index, status
                FROM messages
                WHERE session_scope = ? AND turn_index > ?
                  AND role IN ('user', 'assistant')
                ORDER BY turn_index ASC, DATETIME(created_at) ASC, rowid ASC
                """,
                (session_scope, int(after_turn_index)),
            ).fetchall()

        turns: Dict[int, List[Any]] = {}
        for row in rows:
            turns.setdefault(int(row[4]), []).append(row)

        result = []
        completed_count = 0
        for turn_index in sorted(turns):
            turn_rows = turns[turn_index]
            if not {row[0] for row in turn_rows} >= {"user", "assistant"}:
                continue
            result.extend(
                {
                    "role": row[0],
                    "content": row[1],
                    "name": row[2],
                    "created_at": row[3],
                    "turn_index": row[4],
                    "status": row[5],
                    "session_scope": session_scope,
                }
                for row in turn_rows
            )
            completed_count += 1
            if completed_count >= limit:
                break
        return result

    def list_longterm_extraction_jobs(
        self,
        *,
        session_scope: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        where = "WHERE session_scope = ?" if session_scope is not None else ""
        parameters = (session_scope,) if session_scope is not None else ()
        with self._lock:
            cursor = self.connection.execute(
                f"SELECT * FROM longterm_extraction_jobs {where} ORDER BY sequence_no, rowid",
                parameters,
            )
            rows = [self._row_as_dict(cursor, row) for row in cursor.fetchall()]
        return [self._decode_longterm_extraction_job(row) for row in rows]

    def claim_next_longterm_extraction_job(
        self,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        return self._claim_longterm_extraction_job(lease_timeout_seconds=lease_timeout_seconds)

    def claim_longterm_extraction_job(
        self,
        job_id: str,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        if not job_id:
            raise ValueError("job_id is required")
        return self._claim_longterm_extraction_job(job_id, lease_timeout_seconds=lease_timeout_seconds)

    def _claim_longterm_extraction_job(
        self,
        job_id: Optional[str] = None,
        *,
        lease_timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        now_value = beijing_now()
        now = now_value.isoformat()
        expires_at = (now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))).isoformat()
        lease_token = str(uuid.uuid4())
        job_filter = "AND candidate.job_id = ?" if job_id is not None else ""
        parameters = (now, job_id) if job_id is not None else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.* FROM longterm_extraction_jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry')
                      AND (candidate.next_retry_at IS NULL OR candidate.next_retry_at <= ?)
                      {job_filter}
                      AND NOT EXISTS (
                          SELECT 1 FROM longterm_extraction_jobs AS earlier
                          WHERE earlier.session_scope = candidate.session_scope
                            AND earlier.sequence_no < candidate.sequence_no
                            AND earlier.status NOT IN ('succeeded', 'discarded')
                      )
                    ORDER BY candidate.created_at ASC, candidate.rowid ASC
                    LIMIT 1
                    """,
                    parameters,
                )
                job = self._row_as_dict(cursor, cursor.fetchone())
                if job is None:
                    self.connection.execute("COMMIT")
                    return None
                updated = self.connection.execute(
                    """
                    UPDATE longterm_extraction_jobs
                    SET status = 'running', started_at = ?, lease_token = ?,
                        heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    """,
                    (now, lease_token, now, expires_at, now, job["job_id"], now),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self.connection.execute("COMMIT")
                job.update(
                    {
                        "status": "running",
                        "started_at": now,
                        "lease_token": lease_token,
                        "heartbeat_at": now,
                        "lease_expires_at": expires_at,
                        "updated_at": now,
                    }
                )
                return self._decode_longterm_extraction_job(job)
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def longterm_extraction_job_lease_is_current(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            now = beijing_now_iso()
            row = self.connection.execute(
                """
                SELECT 1 FROM longterm_extraction_jobs
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (job_id, lease_token, now),
            ).fetchone()
        return row is not None

    def heartbeat_longterm_extraction_job(
        self,
        job_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> bool:
        with self._lock:
            now_value = beijing_now()
            now = now_value.isoformat()
            expires_at = (now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))).isoformat()
            cursor = self.connection.execute(
                """
                UPDATE longterm_extraction_jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (now, expires_at, now, job_id, lease_token, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def complete_longterm_extraction_job(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                updated = self.connection.execute(
                    """
                    UPDATE longterm_extraction_jobs
                    SET status = 'succeeded', next_retry_at = NULL, last_error = NULL,
                        finished_at = ?, lease_token = NULL, heartbeat_at = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (now, now, job_id, lease_token, now),
                )
                self.connection.execute("COMMIT")
                return updated.rowcount == 1
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def record_longterm_extraction_failure(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now()
                now_iso = now.isoformat()
                row = self.connection.execute(
                    """
                    SELECT attempts FROM longterm_extraction_jobs
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (job_id, lease_token, now_iso),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "stale_lease"
                attempts = int(row[0]) + 1
                status = "retry" if attempts <= int(max_retries) else "discarded"
                next_retry_at = (
                    (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                    if status == "retry"
                    else None
                )
                finished_at = now_iso if status == "discarded" else None
                updated = self.connection.execute(
                    """
                    UPDATE longterm_extraction_jobs
                    SET status = ?, attempts = ?, next_retry_at = ?, last_error = ?,
                        finished_at = ?, lease_token = NULL, heartbeat_at = NULL,
                        lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (
                        status,
                        attempts,
                        next_retry_at,
                        str(error),
                        finished_at,
                        now_iso,
                        job_id,
                        lease_token,
                        now_iso,
                    ),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return "stale_lease"
                self.connection.execute("COMMIT")
                return status
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def ensure_promotion_job(
        self,
        *,
        user_id: str,
        source_midterm_session_id: str,
        source_run_id: Optional[str],
        source_version: str,
    ) -> Dict[str, Any]:
        """Create one durable promotion job per source content version."""
        if not user_id or not source_midterm_session_id or not source_version:
            raise ValueError("user_id, source_midterm_session_id, and source_version are required")
        job_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"mem0:promotion-job:{user_id}:{source_midterm_session_id}:{source_version}",
            )
        )
        now = beijing_now_iso()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    INSERT INTO memory_promotion_jobs (
                        job_id, user_id, source_midterm_session_id, source_run_id,
                        source_version, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(user_id, source_midterm_session_id, source_version) DO NOTHING
                    """,
                    (
                        job_id,
                        str(user_id),
                        str(source_midterm_session_id),
                        None if source_run_id is None else str(source_run_id),
                        str(source_version),
                        now,
                        now,
                    ),
                )
                cursor = self.connection.execute(
                    """
                    SELECT * FROM memory_promotion_jobs
                    WHERE user_id = ? AND source_midterm_session_id = ? AND source_version = ?
                    """,
                    (str(user_id), str(source_midterm_session_id), str(source_version)),
                )
                job = self._row_as_dict(cursor, cursor.fetchone())
                self.connection.execute("COMMIT")
                if job is None:
                    raise RuntimeError("Failed to persist promotion job")
                return job
            except Exception:
                if self.connection is not None and self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def list_promotion_jobs(
        self,
        *,
        user_id: Optional[str] = None,
        source_midterm_session_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions = []
        parameters: List[Any] = []
        if user_id is not None:
            conditions.append("user_id = ?")
            parameters.append(str(user_id))
        if source_midterm_session_id is not None:
            conditions.append("source_midterm_session_id = ?")
            parameters.append(str(source_midterm_session_id))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._lock:
            cursor = self.connection.execute(
                f"SELECT * FROM memory_promotion_jobs {where} ORDER BY created_at, rowid",
                tuple(parameters),
            )
            return [self._row_as_dict(cursor, row) for row in cursor.fetchall()]

    def _recover_expired_promotion_jobs_locked(self, now: str, max_stale_recoveries: int) -> int:
        rows = self.connection.execute(
            """
            SELECT job_id, recovery_count FROM memory_promotion_jobs
            WHERE status = 'running' AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            """,
            (now,),
        ).fetchall()
        recovered = 0
        for job_id, previous_count in rows:
            recovery_count = int(previous_count or 0) + 1
            status = "discarded" if recovery_count > max_stale_recoveries else "retry"
            updated = self.connection.execute(
                """
                UPDATE memory_promotion_jobs
                SET status = ?, recovery_count = ?, next_retry_at = ?,
                    last_error = 'recovered expired lease', finished_at = ?,
                    lease_token = NULL, heartbeat_at = NULL,
                    lease_expires_at = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                  AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                """,
                (
                    status,
                    recovery_count,
                    None if status == "discarded" else now,
                    now if status == "discarded" else None,
                    now,
                    job_id,
                    now,
                ),
            )
            recovered += int(updated.rowcount == 1)
        return recovered

    def recover_expired_promotion_jobs(self, max_stale_recoveries: int) -> int:
        max_recoveries = max(int(max_stale_recoveries), 0)
        now = beijing_now_iso()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                recovered = self._recover_expired_promotion_jobs_locked(now, max_recoveries)
                self.connection.execute("COMMIT")
                return recovered
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def recover_expired_background_leases(self, max_stale_recoveries: int) -> Dict[str, int]:
        """Recover only running attempts whose explicit lease has expired.

        A stage that repeatedly loses its lease is fenced and routed directly to
        degradation on its next claim. Profile jobs have no degradation path and
        are discarded after the configured recovery budget.
        """
        max_recoveries = max(int(max_stale_recoveries), 0)
        now = beijing_now_iso()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                recovered: Dict[str, int] = {}
                affected_job_ids = set()
                for stage in ("midterm", "longterm"):
                    status_column = f"{stage}_status"
                    retry_column = f"{stage}_next_retry_at"
                    error_column = f"{stage}_last_error"
                    lease_column = f"{stage}_lease_token"
                    heartbeat_column = f"{stage}_heartbeat_at"
                    expires_column = f"{stage}_lease_expires_at"
                    recovery_column = f"{stage}_recovery_count"
                    force_column = f"{stage}_force_degraded"
                    rows = self.connection.execute(
                        f"""
                        SELECT job_id, {recovery_column}
                        FROM memory_migration_jobs
                        WHERE {status_column} = 'running'
                          AND {expires_column} IS NOT NULL
                          AND {expires_column} <= ?
                        """,
                        (now,),
                    ).fetchall()
                    recovered[stage] = 0
                    for job_id, previous_count in rows:
                        recovery_count = int(previous_count or 0) + 1
                        force_degraded = recovery_count > max_recoveries
                        updated = self.connection.execute(
                            f"""
                            UPDATE memory_migration_jobs
                            SET {status_column} = 'retry', {retry_column} = ?,
                                {error_column} = 'recovered expired lease',
                                {recovery_column} = ?, {force_column} = ?,
                                {lease_column} = NULL, {heartbeat_column} = NULL,
                                {expires_column} = NULL, updated_at = ?
                            WHERE job_id = ? AND {status_column} = 'running'
                              AND {expires_column} IS NOT NULL
                              AND {expires_column} <= ?
                            """,
                            (
                                now,
                                recovery_count,
                                int(force_degraded),
                                now,
                                job_id,
                                now,
                            ),
                        )
                        if updated.rowcount:
                            recovered[stage] += 1
                            affected_job_ids.add(job_id)

                for job_id in affected_job_ids:
                    self._refresh_migration_job_locked(job_id, now=now)

                profile_rows = self.connection.execute(
                    """
                    SELECT job_id, recovery_count
                    FROM profile_update_jobs
                    WHERE status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
                profile_recovered = 0
                for job_id, previous_count in profile_rows:
                    recovery_count = int(previous_count or 0) + 1
                    status = "discarded" if recovery_count > max_recoveries else "retry"
                    updated = self.connection.execute(
                        """
                        UPDATE profile_update_jobs
                        SET status = ?, recovery_count = ?, next_retry_at = ?,
                            last_error = 'recovered expired lease',
                            lease_token = NULL, heartbeat_at = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND status = 'running'
                          AND lease_expires_at IS NOT NULL
                          AND lease_expires_at <= ?
                        """,
                        (
                            status,
                            recovery_count,
                            None if status == "discarded" else now,
                            now,
                            job_id,
                            now,
                        ),
                    )
                    profile_recovered += int(updated.rowcount == 1)
                longterm_extraction_rows = self.connection.execute(
                    """
                    SELECT job_id, recovery_count FROM longterm_extraction_jobs
                    WHERE status = 'running' AND lease_expires_at IS NOT NULL
                      AND lease_expires_at <= ?
                    """,
                    (now,),
                ).fetchall()
                longterm_extraction_recovered = 0
                for job_id, previous_count in longterm_extraction_rows:
                    recovery_count = int(previous_count or 0) + 1
                    status = "discarded" if recovery_count > max_recoveries else "retry"
                    updated = self.connection.execute(
                        """
                        UPDATE longterm_extraction_jobs
                        SET status = ?, recovery_count = ?, next_retry_at = ?,
                            last_error = 'recovered expired lease', finished_at = ?,
                            lease_token = NULL, heartbeat_at = NULL,
                            lease_expires_at = NULL, updated_at = ?
                        WHERE job_id = ? AND status = 'running'
                          AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
                        """,
                        (
                            status,
                            recovery_count,
                            None if status == "discarded" else now,
                            now if status == "discarded" else None,
                            now,
                            job_id,
                            now,
                        ),
                    )
                    longterm_extraction_recovered += int(updated.rowcount == 1)
                promotion_recovered = self._recover_expired_promotion_jobs_locked(now, max_recoveries)
                self.connection.execute("COMMIT")
                return {
                    **recovered,
                    "migration": len(affected_job_ids),
                    "profile": profile_recovered,
                    "longterm_extraction": longterm_extraction_recovered,
                    "promotion": promotion_recovered,
                }
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _validate_migration_stage(stage: str) -> str:
        if stage not in {"midterm", "longterm"}:
            raise ValueError("stage must be 'midterm' or 'longterm'")
        return stage

    @staticmethod
    def _migration_parent_status(midterm_status: str, longterm_status: str) -> str:
        statuses = {midterm_status, longterm_status}
        if statuses <= MIGRATION_STAGE_TERMINAL_STATUSES:
            if "discarded" in statuses:
                return "completed_with_loss"
            if "succeeded_degraded" in statuses:
                return "succeeded_degraded"
            return "succeeded"
        if "running" in statuses:
            return "running"
        if "retry" in statuses:
            return "retry"
        return "pending"

    def _refresh_migration_job_locked(self, job_id: str, *, now: Optional[str] = None) -> Optional[str]:
        row = self.connection.execute(
            """
            SELECT midterm_status, longterm_status, finalized_at
            FROM memory_migration_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        parent_status = self._migration_parent_status(row[0], row[1])
        current_time = now or beijing_now_iso()
        self.connection.execute(
            "UPDATE memory_migration_jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (parent_status, current_time, job_id),
        )
        if row[2] is None:
            message_status = "processing" if "running" in {row[0], row[1]} else "pending"
            self.connection.execute(
                "UPDATE messages SET status = ? WHERE migration_job_id = ?",
                (message_status, job_id),
            )
        return parent_status

    def _finalize_migration_job_locked(self, job_id: str, *, now: Optional[str] = None) -> bool:
        row = self.connection.execute(
            """
            SELECT midterm_status, longterm_status, finalized_at
            FROM memory_migration_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None or row[2] is not None:
            return False
        statuses = {row[0], row[1]}
        if not statuses <= MIGRATION_STAGE_TERMINAL_STATUSES:
            return False

        current_time = now or beijing_now_iso()
        if "discarded" in statuses:
            final_status = "completed_with_loss"
        elif "succeeded_degraded" in statuses:
            final_status = "succeeded_degraded"
        else:
            final_status = "succeeded"
        updated = self.connection.execute(
            """
            UPDATE memory_migration_jobs
            SET status = ?, finalized_at = ?, updated_at = ?
            WHERE job_id = ? AND finalized_at IS NULL
            """,
            (final_status, current_time, current_time, job_id),
        )
        if updated.rowcount != 1:
            return False
        self.connection.execute("DELETE FROM messages WHERE migration_job_id = ?", (job_id,))
        return True

    def claim_next_migration_stage(
        self,
        stage: str,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        return self._claim_migration_stage(stage, lease_timeout_seconds=lease_timeout_seconds)

    def claim_migration_stage(
        self,
        job_id: str,
        stage: str,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim one stage without bypassing that session's stage order."""
        if not job_id:
            raise ValueError("job_id is required")
        return self._claim_migration_stage(stage, job_id, lease_timeout_seconds=lease_timeout_seconds)

    def _claim_migration_stage(
        self,
        stage: str,
        job_id: Optional[str] = None,
        *,
        lease_timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        stage = self._validate_migration_stage(stage)
        status_column = f"{stage}_status"
        retry_column = f"{stage}_next_retry_at"
        started_column = f"{stage}_started_at"
        lease_column = f"{stage}_lease_token"
        heartbeat_column = f"{stage}_heartbeat_at"
        expires_column = f"{stage}_lease_expires_at"
        now_value = beijing_now()
        now = now_value.isoformat()
        lease_expires_at = (
            now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
        ).isoformat()
        lease_token = str(uuid.uuid4())
        job_filter = "AND candidate.job_id = ?" if job_id is not None else ""
        parameters = (now, job_id) if job_id is not None else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.*
                    FROM memory_migration_jobs AS candidate
                    WHERE candidate.{status_column} IN ('pending', 'retry')
                      AND (candidate.{retry_column} IS NULL OR candidate.{retry_column} <= ?)
                      {job_filter}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM memory_migration_jobs AS earlier
                          WHERE earlier.session_scope = candidate.session_scope
                            AND earlier.sequence_no < candidate.sequence_no
                            AND earlier.{status_column} NOT IN ('succeeded', 'succeeded_degraded', 'discarded')
                      )
                    ORDER BY candidate.created_at ASC, candidate.rowid ASC
                    LIMIT 1
                    """,
                    parameters,
                )
                row = cursor.fetchone()
                job = self._row_as_dict(cursor, row)
                if job is None:
                    self.connection.execute("COMMIT")
                    return None
                updated = self.connection.execute(
                    f"""
                    UPDATE memory_migration_jobs
                    SET {status_column} = 'running', {started_column} = ?,
                        {lease_column} = ?, {heartbeat_column} = ?,
                        {expires_column} = ?, updated_at = ?
                    WHERE job_id = ?
                      AND {status_column} IN ('pending', 'retry')
                      AND ({retry_column} IS NULL OR {retry_column} <= ?)
                    """,
                    (now, lease_token, now, lease_expires_at, now, job["job_id"], now),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self._refresh_migration_job_locked(job["job_id"], now=now)
                self.connection.execute("COMMIT")
                job[status_column] = "running"
                job[started_column] = now
                job[lease_column] = lease_token
                job[heartbeat_column] = now
                job[expires_column] = lease_expires_at
                job["status"] = self._migration_parent_status(job["midterm_status"], job["longterm_status"])
                job["updated_at"] = now
                return self._decode_migration_job(job)
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def claim_next_profile_job(self, lease_timeout_seconds: float = 120.0) -> Optional[Dict[str, Any]]:
        return self._claim_profile_job(lease_timeout_seconds=lease_timeout_seconds)

    def claim_profile_job(
        self,
        job_id: str,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        """Atomically claim one runnable profile job without bypassing queue order."""
        if not job_id:
            raise ValueError("job_id is required")
        return self._claim_profile_job(job_id, lease_timeout_seconds=lease_timeout_seconds)

    def _claim_profile_job(
        self,
        job_id: Optional[str] = None,
        *,
        lease_timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        now_value = beijing_now()
        now = now_value.isoformat()
        lease_expires_at = (
            now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
        ).isoformat()
        lease_token = str(uuid.uuid4())
        job_filter = "AND candidate.job_id = ?" if job_id is not None else ""
        parameters = (now, job_id) if job_id is not None else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.*
                    FROM profile_update_jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry')
                      AND (candidate.next_retry_at IS NULL OR candidate.next_retry_at <= ?)
                      {job_filter}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM profile_update_jobs AS earlier
                          WHERE earlier.user_id = candidate.user_id
                            AND earlier.sequence_no < candidate.sequence_no
                            AND earlier.status NOT IN ('succeeded', 'discarded')
                      )
                    ORDER BY candidate.created_at ASC, candidate.rowid ASC
                    LIMIT 1
                    """,
                    parameters,
                )
                row = cursor.fetchone()
                job = self._row_as_dict(cursor, row)
                if job is None:
                    self.connection.execute("COMMIT")
                    return None
                updated = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = 'running', started_at = ?, lease_token = ?,
                        heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                    """,
                    (now, lease_token, now, lease_expires_at, now, job["job_id"]),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self.connection.execute("COMMIT")
                job["status"] = "running"
                job["started_at"] = now
                job["lease_token"] = lease_token
                job["heartbeat_at"] = now
                job["lease_expires_at"] = lease_expires_at
                job["updated_at"] = now
                return self._decode_profile_job(job)
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def claim_next_promotion_job(self, lease_timeout_seconds: float = 120.0) -> Optional[Dict[str, Any]]:
        return self._claim_promotion_job(lease_timeout_seconds=lease_timeout_seconds)

    def claim_promotion_job(
        self,
        job_id: str,
        lease_timeout_seconds: float = 120.0,
    ) -> Optional[Dict[str, Any]]:
        if not job_id:
            raise ValueError("job_id is required")
        return self._claim_promotion_job(job_id, lease_timeout_seconds=lease_timeout_seconds)

    def _claim_promotion_job(
        self,
        job_id: Optional[str] = None,
        *,
        lease_timeout_seconds: float,
    ) -> Optional[Dict[str, Any]]:
        now_value = beijing_now()
        now = now_value.isoformat()
        lease_expires_at = (
            now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
        ).isoformat()
        lease_token = str(uuid.uuid4())
        job_filter = "AND candidate.job_id = ?" if job_id is not None else ""
        parameters = (now, job_id) if job_id is not None else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.* FROM memory_promotion_jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry')
                      AND (candidate.next_retry_at IS NULL OR candidate.next_retry_at <= ?)
                      {job_filter}
                      AND NOT EXISTS (
                          SELECT 1 FROM memory_promotion_jobs AS earlier
                          WHERE earlier.user_id = candidate.user_id
                            AND earlier.source_midterm_session_id = candidate.source_midterm_session_id
                            AND earlier.rowid < candidate.rowid
                            AND earlier.status NOT IN ('succeeded', 'discarded')
                      )
                    ORDER BY candidate.created_at ASC, candidate.rowid ASC
                    LIMIT 1
                    """,
                    parameters,
                )
                job = self._row_as_dict(cursor, cursor.fetchone())
                if job is None:
                    self.connection.execute("COMMIT")
                    return None
                updated = self.connection.execute(
                    """
                    UPDATE memory_promotion_jobs
                    SET status = 'running', started_at = ?, lease_token = ?,
                        heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                      AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    """,
                    (now, lease_token, now, lease_expires_at, now, job["job_id"], now),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self.connection.execute("COMMIT")
                job.update(
                    {
                        "status": "running",
                        "started_at": now,
                        "lease_token": lease_token,
                        "heartbeat_at": now,
                        "lease_expires_at": lease_expires_at,
                        "updated_at": now,
                    }
                )
                return job
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def migration_stage_lease_is_current(self, job_id: str, stage: str, lease_token: str) -> bool:
        stage = self._validate_migration_stage(stage)
        with self._lock:
            now = beijing_now_iso()
            row = self.connection.execute(
                f"""
                SELECT 1 FROM memory_migration_jobs
                WHERE job_id = ? AND {stage}_status = 'running'
                  AND {stage}_lease_token = ?
                  AND {stage}_lease_expires_at IS NOT NULL
                  AND {stage}_lease_expires_at > ?
                """,
                (job_id, lease_token, now),
            ).fetchone()
        return row is not None

    def heartbeat_migration_stage(
        self,
        job_id: str,
        stage: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> bool:
        stage = self._validate_migration_stage(stage)
        with self._lock:
            now_value = beijing_now()
            now = now_value.isoformat()
            expires_at = (
                now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
            ).isoformat()
            cursor = self.connection.execute(
                f"""
                UPDATE memory_migration_jobs
                SET {stage}_heartbeat_at = ?, {stage}_lease_expires_at = ?,
                    updated_at = ?
                WHERE job_id = ? AND {stage}_status = 'running'
                  AND {stage}_lease_token = ?
                  AND {stage}_lease_expires_at IS NOT NULL
                  AND {stage}_lease_expires_at > ?
                """,
                (now, expires_at, now, job_id, lease_token, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def heartbeat_profile_job(
        self,
        job_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> bool:
        with self._lock:
            now_value = beijing_now()
            now = now_value.isoformat()
            expires_at = (
                now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
            ).isoformat()
            cursor = self.connection.execute(
                """
                UPDATE profile_update_jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (now, expires_at, now, job_id, lease_token, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def heartbeat_promotion_job(
        self,
        job_id: str,
        lease_token: str,
        lease_timeout_seconds: float,
    ) -> bool:
        with self._lock:
            now_value = beijing_now()
            now = now_value.isoformat()
            expires_at = (
                now_value + timedelta(seconds=max(float(lease_timeout_seconds), 0.001))
            ).isoformat()
            cursor = self.connection.execute(
                """
                UPDATE memory_promotion_jobs
                SET heartbeat_at = ?, lease_expires_at = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (now, expires_at, now, job_id, lease_token, now),
            )
            self.connection.commit()
            return cursor.rowcount == 1

    def profile_job_lease_is_current(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            now = beijing_now_iso()
            row = self.connection.execute(
                """
                SELECT 1 FROM profile_update_jobs
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at > ?
                """,
                (job_id, lease_token, now),
            ).fetchone()
        return row is not None

    def promotion_job_lease_is_current(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            now = beijing_now_iso()
            row = self.connection.execute(
                """
                SELECT 1 FROM memory_promotion_jobs
                WHERE job_id = ? AND status = 'running' AND lease_token = ?
                  AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                """,
                (job_id, lease_token, now),
            ).fetchone()
        return row is not None

    def mark_migration_stage_succeeded(
        self,
        job_id: str,
        stage: str,
        lease_token: str,
        *,
        degraded: bool = False,
    ) -> bool:
        stage = self._validate_migration_stage(stage)
        status_column = f"{stage}_status"
        retry_column = f"{stage}_next_retry_at"
        error_column = f"{stage}_last_error"
        degraded_column = f"{stage}_degraded"
        finished_column = f"{stage}_finished_at"
        lease_column = f"{stage}_lease_token"
        heartbeat_column = f"{stage}_heartbeat_at"
        expires_column = f"{stage}_lease_expires_at"
        force_column = f"{stage}_force_degraded"
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                updated = self.connection.execute(
                    f"""
                    UPDATE memory_migration_jobs
                    SET {status_column} = ?, {retry_column} = NULL,
                        {error_column} = NULL, {degraded_column} = ?,
                        {finished_column} = ?, {lease_column} = NULL,
                        {heartbeat_column} = NULL, {expires_column} = NULL,
                        {force_column} = 0, updated_at = ?
                    WHERE job_id = ? AND {status_column} = 'running'
                      AND {lease_column} = ?
                      AND {expires_column} IS NOT NULL
                      AND {expires_column} > ?
                    """,
                    (
                        "succeeded_degraded" if degraded else "succeeded",
                        int(degraded),
                        now,
                        now,
                        job_id,
                        lease_token,
                        now,
                    ),
                )

                if updated.rowcount:
                    self._refresh_migration_job_locked(job_id, now=now)
                    self._finalize_migration_job_locked(job_id, now=now)
                    
                self.connection.execute("COMMIT")
                return updated.rowcount == 1
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def record_migration_stage_failure(
        self,
        job_id: str,
        stage: str,
        lease_token: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        stage = self._validate_migration_stage(stage)
        status_column = f"{stage}_status"
        attempts_column = f"{stage}_attempts"
        retry_column = f"{stage}_next_retry_at"
        error_column = f"{stage}_last_error"
        lease_column = f"{stage}_lease_token"
        heartbeat_column = f"{stage}_heartbeat_at"
        expires_column = f"{stage}_lease_expires_at"
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now()
                now_iso = now.isoformat()
                row = self.connection.execute(
                    f"""
                    SELECT {attempts_column} FROM memory_migration_jobs
                    WHERE job_id = ? AND {status_column} = 'running'
                      AND {lease_column} = ?
                      AND {expires_column} IS NOT NULL
                      AND {expires_column} > ?
                    """,
                    (job_id, lease_token, now_iso),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "stale_lease"
                attempts = int(row[0]) + 1
                if attempts <= max_retries:
                    status = "retry"
                    next_retry_at = (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                else:
                    status = "exhausted"
                    next_retry_at = None
                update_now = beijing_now_iso()
                updated = self.connection.execute(
                    f"""
                    UPDATE memory_migration_jobs
                    SET {status_column} = ?, {attempts_column} = ?,
                        {retry_column} = ?, {error_column} = ?,
                        {lease_column} = CASE WHEN ? = 'retry' THEN NULL ELSE {lease_column} END,
                        {heartbeat_column} = CASE WHEN ? = 'retry' THEN NULL ELSE {heartbeat_column} END,
                        {expires_column} = CASE WHEN ? = 'retry' THEN NULL ELSE {expires_column} END,
                        updated_at = ?
                    WHERE job_id = ? AND {status_column} = 'running'
                      AND {lease_column} = ?
                      AND {expires_column} IS NOT NULL
                      AND {expires_column} > ?
                    """,
                    (
                        "retry" if status == "retry" else "running",
                        attempts,
                        next_retry_at,
                        error,
                        status,
                        status,
                        status,
                        update_now,
                        job_id,
                        lease_token,
                        update_now,
                    ),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return "stale_lease"
                self._refresh_migration_job_locked(job_id, now=update_now)
                self.connection.execute("COMMIT")
                return status
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def mark_migration_stage_discarded(
        self,
        job_id: str,
        stage: str,
        lease_token: str,
        error: str,
        *,
        cleanup_error: Optional[str] = None,
    ) -> bool:
        stage = self._validate_migration_stage(stage)
        status_column = f"{stage}_status"
        retry_column = f"{stage}_next_retry_at"
        error_column = f"{stage}_last_error"
        finished_column = f"{stage}_finished_at"
        lease_column = f"{stage}_lease_token"
        heartbeat_column = f"{stage}_heartbeat_at"
        expires_column = f"{stage}_lease_expires_at"
        force_column = f"{stage}_force_degraded"
        cleanup_column = f"{stage}_cleanup_error"
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                updated = self.connection.execute(
                    f"""
                    UPDATE memory_migration_jobs
                    SET {status_column} = 'discarded', {retry_column} = NULL,
                        {error_column} = ?, {cleanup_column} = ?,
                        {finished_column} = ?, {lease_column} = NULL,
                        {heartbeat_column} = NULL, {expires_column} = NULL,
                        {force_column} = 0, updated_at = ?
                    WHERE job_id = ? AND {status_column} = 'running'
                      AND {lease_column} = ?
                      AND {expires_column} IS NOT NULL
                      AND {expires_column} > ?
                    """,
                    (error, cleanup_error, now, now, job_id, lease_token, now),
                )
                if updated.rowcount:
                    self._refresh_migration_job_locked(job_id, now=now)
                    self._finalize_migration_job_locked(job_id, now=now)
                self.connection.execute("COMMIT")
                return updated.rowcount == 1
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def finalize_migration_job_if_ready(self, job_id: str) -> bool:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                finalized = self._finalize_migration_job_locked(job_id)
                self.connection.execute("COMMIT")
                return finalized
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def finish_profile_job(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                cursor = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = 'succeeded', next_retry_at = NULL,
                        last_error = NULL, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (now, job_id, lease_token, now),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                if self.connection is not None and self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def record_profile_failure(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now()
                now_iso = now.isoformat()
                row = self.connection.execute(
                    """
                    SELECT attempts FROM profile_update_jobs
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (job_id, lease_token, now_iso),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "stale_lease"
                attempts = int(row[0]) + 1
                if attempts <= max_retries:
                    status = "retry"
                    next_retry_at = (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                else:
                    status = "discarded"
                    next_retry_at = None
                update_now = beijing_now_iso()
                updated = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = ?, attempts = ?, next_retry_at = ?,
                        last_error = ?, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (
                        status,
                        attempts,
                        next_retry_at,
                        error,
                        update_now,
                        job_id,
                        lease_token,
                        update_now,
                    ),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return "stale_lease"
                self.connection.execute("COMMIT")
                return status
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def complete_promotion_job(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                cursor = self.connection.execute(
                    """
                    UPDATE memory_promotion_jobs
                    SET status = 'succeeded', next_retry_at = NULL,
                        last_error = NULL, finished_at = ?, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (now, now, job_id, lease_token, now),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def discard_promotion_job(self, job_id: str, lease_token: str, reason: str) -> bool:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                cursor = self.connection.execute(
                    """
                    UPDATE memory_promotion_jobs
                    SET status = 'discarded', next_retry_at = NULL,
                        last_error = ?, finished_at = ?, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (str(reason), now, now, job_id, lease_token, now),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def retry_promotion_job(
        self,
        job_id: str,
        lease_token: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now()
                now_iso = now.isoformat()
                row = self.connection.execute(
                    """
                    SELECT attempts FROM memory_promotion_jobs
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (job_id, lease_token, now_iso),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "stale_lease"
                attempts = int(row[0]) + 1
                status = "retry" if attempts <= int(max_retries) else "discarded"
                next_retry_at = (
                    (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                    if status == "retry"
                    else None
                )
                finished_at = now_iso if status == "discarded" else None
                updated = self.connection.execute(
                    """
                    UPDATE memory_promotion_jobs
                    SET status = ?, attempts = ?, next_retry_at = ?,
                        last_error = ?, finished_at = ?, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'running' AND lease_token = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (
                        status,
                        attempts,
                        next_retry_at,
                        str(error),
                        finished_at,
                        now_iso,
                        job_id,
                        lease_token,
                        now_iso,
                    ),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return "stale_lease"
                self.connection.execute("COMMIT")
                return status
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def background_jobs_pending(self) -> bool:
        with self._lock:
            migration = self.connection.execute(
                """
                SELECT 1 FROM memory_migration_jobs
                WHERE midterm_status IN ('pending', 'running', 'retry')
                   OR longterm_status IN ('pending', 'running', 'retry')
                LIMIT 1
                """
            ).fetchone()
            profile = self.connection.execute(
                """
                SELECT 1 FROM profile_update_jobs
                WHERE status IN ('pending', 'running', 'retry')
                LIMIT 1
                """
            ).fetchone()
            longterm_extraction = self.connection.execute(
                """
                SELECT 1 FROM longterm_extraction_jobs
                WHERE status IN ('pending', 'running', 'retry')
                LIMIT 1
                """
            ).fetchone()
            promotion = self.connection.execute(
                """
                SELECT 1 FROM memory_promotion_jobs
                WHERE status IN ('pending', 'running', 'retry')
                LIMIT 1
                """
            ).fetchone()
            return any(item is not None for item in (migration, profile, longterm_extraction, promotion))

    def migration_stage_jobs_pending(self, stage: str) -> bool:
        stage = self._validate_migration_stage(stage)
        with self._lock:
            row = self.connection.execute(
                f"""
                SELECT 1 FROM memory_migration_jobs
                WHERE {stage}_status IN ('pending', 'running', 'retry')
                LIMIT 1
                """
            ).fetchone()
        return row is not None

    def get_background_job(self, job_id: str, job_type: str = "migration") -> Optional[Dict[str, Any]]:
        tables = {
            "migration": "memory_migration_jobs",
            "profile": "profile_update_jobs",
            "promotion": "memory_promotion_jobs",
            "longterm_extraction": "longterm_extraction_jobs",
        }
        if job_type not in tables:
            raise ValueError("job_type must be 'migration', 'profile', 'promotion', or 'longterm_extraction'")
        table = tables[job_type]
        with self._lock:
            cursor = self.connection.execute(f"SELECT * FROM {table} WHERE job_id = ?", (job_id,))
            job = self._row_as_dict(cursor, cursor.fetchone())
        if job_type == "migration":
            return self._decode_migration_job(job)
        if job_type == "profile":
            return self._decode_profile_job(job)
        if job_type == "longterm_extraction":
            return self._decode_longterm_extraction_job(job)
        return job

    @staticmethod
    def _profile_attribute_from_row(row) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {
            "attribute_id": row[0],
            "attribute_key": row[1],
            "attribute_name": row[2],
            "attribute_category": row[3],
            "description": row[4],
            "value_type": row[5],
            "value_schema": json.loads(row[6]),
            "merge_policy": row[7],
            "is_predefined": bool(row[8]),
            "is_active": bool(row[9]),
            "created_at": row[10],
            "updated_at": row[11],
        }

    def _list_profile_attributes_locked(self, active_only: bool = True) -> List[Dict[str, Any]]:
        query = """
            SELECT attribute_id, attribute_key, attribute_name, attribute_category,
                   description, value_type, value_schema_json, merge_policy,
                   is_predefined, is_active, created_at, updated_at
            FROM profile_attributes
        """
        parameters = ()
        if active_only:
            query += " WHERE is_active = ?"
            parameters = (1,)
        query += " ORDER BY attribute_id ASC"
        rows = self.connection.execute(query, parameters).fetchall()
        return [self._profile_attribute_from_row(row) for row in rows]

    def list_profile_attributes(self, active_only: bool = True) -> List[Dict[str, Any]]:
        """List profile attribute definitions without any user values."""
        with self._lock:
            return self._list_profile_attributes_locked(active_only=active_only)

    def _get_profile_attribute_locked(
        self, attribute_key: str, include_inactive: bool = False
    ) -> Optional[Dict[str, Any]]:
        query = """
            SELECT attribute_id, attribute_key, attribute_name, attribute_category,
                   description, value_type, value_schema_json, merge_policy,
                   is_predefined, is_active, created_at, updated_at
            FROM profile_attributes
            WHERE attribute_key = ?
        """
        parameters = [attribute_key]
        if not include_inactive:
            query += " AND is_active = ?"
            parameters.append(1)
        row = self.connection.execute(query, tuple(parameters)).fetchone()
        return self._profile_attribute_from_row(row)

    def get_profile_attribute(self, attribute_key: str, include_inactive: bool = False) -> Optional[Dict[str, Any]]:
        """Return one profile attribute definition by key."""
        with self._lock:
            return self._get_profile_attribute_locked(attribute_key, include_inactive=include_inactive)

    def create_profile_attribute(self, definition: Any, is_predefined: bool = False) -> Dict[str, Any]:
        """Create a profile attribute definition."""
        model = validate_attribute_definition(definition)
        now = beijing_now_iso()
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    INSERT INTO profile_attributes (
                        attribute_key, attribute_name, attribute_category, description,
                        value_type, value_schema_json, merge_policy, is_predefined,
                        is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        model.attribute_key,
                        model.attribute_name,
                        model.attribute_category,
                        model.description,
                        model.value_type,
                        serialize_profile_value(model.value_schema),
                        model.merge_policy,
                        int(is_predefined),
                        now,
                        now,
                    ),
                )
                created = self._get_profile_attribute_locked(model.attribute_key)
                self.connection.execute("COMMIT")
                return created
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create profile attribute %s: %s", model.attribute_key, e)
                raise

    def deactivate_profile_attribute(self, attribute_key: str) -> bool:
        """Deactivate an attribute while preserving its definition and current values."""
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                cursor = self.connection.execute(
                    """
                    UPDATE profile_attributes
                    SET is_active = 0, updated_at = ?
                    WHERE attribute_key = ? AND is_active = 1
                    """,
                    (beijing_now_iso(), attribute_key),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to deactivate profile attribute %s: %s", attribute_key, e)
                raise

    @staticmethod
    def _user_profile_value_from_row(row) -> Dict[str, Any]:
        return {
            "user_id": row[0],
            "attribute_id": row[1],
            "attribute_key": row[2],
            "attribute_name": row[3],
            "attribute_category": row[4],
            "description": row[5],
            "value_type": row[6],
            "value_schema": json.loads(row[7]),
            "merge_policy": row[8],
            "value": json.loads(row[9]),
            "source_type": row[10],
            "confidence": row[11],
            "value_version": row[12],
            "created_at": row[13],
            "updated_at": row[14],
        }

    def _get_user_profile_values_locked(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT v.user_id, a.attribute_id, a.attribute_key, a.attribute_name,
                   a.attribute_category, a.description, a.value_type,
                   a.value_schema_json, a.merge_policy, v.value_json,
                   v.source_type, v.confidence, v.value_version,
                   v.created_at, v.updated_at
            FROM user_profile_values AS v
            JOIN profile_attributes AS a ON a.attribute_id = v.attribute_id
            WHERE v.user_id = ? AND a.is_active = 1
            ORDER BY a.attribute_id ASC
            """,
            (user_id,),
        ).fetchall()
        return [self._user_profile_value_from_row(row) for row in rows]

    def get_user_profile_values(self, user_id: str) -> List[Dict[str, Any]]:
        """Read a user's current profile values joined with their definitions."""
        with self._lock:
            return self._get_user_profile_values_locked(user_id)

    def _get_user_profile_value_locked(self, user_id: str, attribute_id: int) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            """
            SELECT value_json, source_type, confidence, value_version, created_at, updated_at
            FROM user_profile_values
            WHERE user_id = ? AND attribute_id = ?
            """,
            (user_id, attribute_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "value": json.loads(row[0]),
            "source_type": row[1],
            "confidence": row[2],
            "value_version": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }

    def _upsert_user_profile_value_locked(
        self,
        user_id: str,
        attribute_id: int,
        value_json: str,
        source_type: str,
        confidence: float,
    ) -> None:
        now = beijing_now_iso()
        self.connection.execute(
            """
            INSERT INTO user_profile_values (
                user_id, attribute_id, value_json, source_type, confidence,
                value_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, attribute_id) DO UPDATE SET
                value_json = excluded.value_json,
                source_type = excluded.source_type,
                confidence = excluded.confidence,
                value_version = user_profile_values.value_version + 1,
                updated_at = excluded.updated_at
            """,
            (user_id, attribute_id, value_json, source_type, confidence, now, now),
        )

    @staticmethod
    def _validate_value_metadata(source_type: str, confidence: float) -> None:
        if source_type not in {"explicit", "inferred", "repeated", "correction", "imported"}:
            raise ValueError("source_type must be explicit, inferred, repeated, correction, or imported")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be a number between 0 and 1")

    def upsert_user_profile_value(
        self,
        user_id: str,
        attribute_key: str,
        value: Any,
        source_type: str = "explicit",
        confidence: float = 1.0,
        max_value_json_bytes: int = 16384,
    ) -> Dict[str, Any]:
        """Validate and replace one current profile value."""
        self._validate_value_metadata(source_type, confidence)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                definition = self._get_profile_attribute_locked(attribute_key)
                if definition is None:
                    raise ValueError(f"Unknown or inactive profile attribute: {attribute_key}")
                normalized = normalize_profile_value(value, definition)
                value_json = serialize_profile_value(normalized)
                if len(value_json.encode("utf-8")) > max_value_json_bytes:
                    raise ValueError("profile value exceeds max_value_json_bytes")
                self._upsert_user_profile_value_locked(
                    user_id,
                    definition["attribute_id"],
                    value_json,
                    source_type,
                    confidence,
                )
                current = self._get_user_profile_value_locked(user_id, definition["attribute_id"])
                self.connection.execute("COMMIT")
                return current
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def delete_user_profile_value(self, user_id: str, attribute_key: str) -> bool:
        """Delete one user's value without deleting its attribute definition."""
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                definition = self._get_profile_attribute_locked(attribute_key, include_inactive=True)
                if definition is None:
                    self.connection.execute("COMMIT")
                    return False
                cursor = self.connection.execute(
                    "DELETE FROM user_profile_values WHERE user_id = ? AND attribute_id = ?",
                    (user_id, definition["attribute_id"]),
                )
                self.connection.execute("COMMIT")
                return cursor.rowcount > 0
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def delete_all_user_profile_values(self, user_id: str) -> int:
        """Delete all current profile values for one user."""
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                cursor = self.connection.execute("DELETE FROM user_profile_values WHERE user_id = ?", (user_id,))
                self.connection.execute("COMMIT")
                return cursor.rowcount
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def apply_profile_update_plan(
        self,
        user_id: str,
        plan: Any,
        max_value_json_bytes: int = 16384,
    ) -> List[Dict[str, Any]]:
        """Atomically apply a validated profile plan against the latest stored values."""
        update_plan = plan if isinstance(plan, ProfileUpdatePlan) else ProfileUpdatePlan.model_validate(plan)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                values = self._apply_profile_update_plan_locked(
                    user_id,
                    update_plan,
                    max_value_json_bytes=max_value_json_bytes,
                )
                self.connection.execute("COMMIT")
                return values
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def _apply_profile_update_plan_locked(
        self,
        user_id: str,
        update_plan: ProfileUpdatePlan,
        *,
        max_value_json_bytes: int,
    ) -> List[Dict[str, Any]]:
        """Apply a validated plan using the caller's existing SQLite transaction."""
        for operation in update_plan.operations:
            definition = self._get_profile_attribute_locked(operation.attribute_key)
            if definition is None:
                raise ValueError(f"Unknown or inactive profile attribute: {operation.attribute_key}")
            attribute_id = definition["attribute_id"]
            if operation.operation == "delete":
                validate_operation(definition, operation)
                self.connection.execute(
                    "DELETE FROM user_profile_values WHERE user_id = ? AND attribute_id = ?",
                    (user_id, attribute_id),
                )
                continue

            current = self._get_user_profile_value_locked(user_id, attribute_id)
            current_value = current["value"] if current else None
            merged_value, changed = merge_profile_value(current_value, operation, definition)
            if not changed:
                continue
            value_json = serialize_profile_value(merged_value)
            if len(value_json.encode("utf-8")) > max_value_json_bytes:
                raise ValueError(f"Profile attribute '{operation.attribute_key}' exceeds max_value_json_bytes")
            self._upsert_user_profile_value_locked(
                user_id,
                attribute_id,
                value_json,
                operation.source_type,
                operation.confidence,
            )
        return self._get_user_profile_values_locked(user_id)

    def apply_profile_plan_and_finish_job(
        self,
        job_id: str,
        lease_token: str,
        user_id: str,
        plan: Any,
        *,
        max_value_json_bytes: int = 16384,
    ) -> bool:
        """Atomically apply one background profile plan and finish its fenced job."""
        update_plan = plan if isinstance(plan, ProfileUpdatePlan) else ProfileUpdatePlan.model_validate(plan)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                row = self.connection.execute(
                    """
                    SELECT user_id
                    FROM profile_update_jobs
                    WHERE job_id = ? AND status = 'running'
                      AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (job_id, lease_token, now),
                ).fetchone()
                if row is None or row[0] != user_id:
                    self.connection.execute("COMMIT")
                    return False

                self._apply_profile_update_plan_locked(
                    user_id,
                    update_plan,
                    max_value_json_bytes=max_value_json_bytes,
                )
                finish_now = beijing_now_iso()
                updated = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = 'succeeded', next_retry_at = NULL,
                        last_error = NULL, lease_token = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE job_id = ? AND user_id = ? AND status = 'running'
                      AND lease_token = ?
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at > ?
                    """,
                    (finish_now, job_id, user_id, lease_token, finish_now),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return False
                self.connection.execute("COMMIT")
                return True
            except Exception:
                if self.connection is not None and self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def reset(self) -> None:
        """Drop all local tables. Caller is expected to replace this instance."""
        if not self.connection:
            raise RuntimeError("Cannot reset a closed SQLiteManager")
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                self.connection.execute("DROP TABLE IF EXISTS user_profile_values")
                self.connection.execute("DROP TABLE IF EXISTS profile_attributes")
                self.connection.execute("DROP TABLE IF EXISTS profile_update_jobs")
                self.connection.execute("DROP TABLE IF EXISTS longterm_extraction_jobs")
                self.connection.execute("DROP TABLE IF EXISTS memory_promotion_jobs")
                self.connection.execute("DROP TABLE IF EXISTS memory_migration_jobs")
                self.connection.execute("DROP TABLE IF EXISTS memory_idempotency_operations")
                self.connection.execute("DROP TABLE IF EXISTS conversation_turns")
                self.connection.execute("DROP TABLE IF EXISTS history")
                self.connection.execute("DROP TABLE IF EXISTS messages")
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to reset tables: {e}")
                raise

    def close(self) -> None:
        with self._lock:
            if self.connection:
                self.connection.close()
                self.connection = None

    def __del__(self):
        self.close()
