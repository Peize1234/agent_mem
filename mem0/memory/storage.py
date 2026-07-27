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
        self._create_background_job_tables()
        self._create_profile_tables()
        self._create_observation_events_table()
        self._sync_predefined_profile_attributes()

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
                        trace_id TEXT,
                        user_id TEXT,
                        run_id TEXT,
                        role TEXT,
                        content TEXT,
                        name TEXT,
                        created_at DATETIME,
                        status TEXT NOT NULL DEFAULT 'active',
                        migration_job_id TEXT
                    )
                """
                )
                columns = {row[1] for row in self.connection.execute("PRAGMA table_info(messages)").fetchall()}
                if "status" not in columns:
                    self.connection.execute("ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
                if "migration_job_id" not in columns:
                    self.connection.execute("ALTER TABLE messages ADD COLUMN migration_job_id TEXT")
                if "trace_id" not in columns:
                    self.connection.execute("ALTER TABLE messages ADD COLUMN trace_id TEXT")
                added_scope_columns = False
                if "user_id" not in columns:
                    self.connection.execute("ALTER TABLE messages ADD COLUMN user_id TEXT")
                    added_scope_columns = True
                if "run_id" not in columns:
                    self.connection.execute("ALTER TABLE messages ADD COLUMN run_id TEXT")
                    added_scope_columns = True
                if added_scope_columns:
                    self.connection.execute(
                        """
                        UPDATE messages
                        SET user_id = COALESCE(
                                user_id,
                                CASE
                                    WHEN INSTR(session_scope, 'user_id=') > 0
                                    THEN SUBSTR(
                                        session_scope || '&',
                                        INSTR(session_scope, 'user_id=') + LENGTH('user_id='),
                                        INSTR(
                                            SUBSTR(
                                                session_scope || '&',
                                                INSTR(session_scope, 'user_id=') + LENGTH('user_id=')
                                            ),
                                            '&'
                                        ) - 1
                                    )
                                END
                            ),
                            run_id = COALESCE(
                                run_id,
                                CASE
                                    WHEN INSTR(session_scope, 'run_id=') > 0
                                    THEN SUBSTR(
                                        session_scope || '&',
                                        INSTR(session_scope, 'run_id=') + LENGTH('run_id='),
                                        INSTR(
                                            SUBSTR(
                                                session_scope || '&',
                                                INSTR(session_scope, 'run_id=') + LENGTH('run_id=')
                                            ),
                                            '&'
                                        ) - 1
                                    )
                                END
                            )
                        WHERE user_id IS NULL OR run_id IS NULL
                        """
                    )
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
                    CREATE INDEX IF NOT EXISTS idx_messages_trace
                    ON messages(trace_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_user_created
                    ON messages(user_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_messages_run_created
                    ON messages(run_id, created_at)
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to create messages table: {e}")
                raise

    def _create_background_job_tables(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_migration_jobs (
                        job_id TEXT PRIMARY KEY,
                        trace_id TEXT,
                        session_scope TEXT NOT NULL,
                        user_id TEXT,
                        run_id TEXT,
                        status TEXT NOT NULL DEFAULT 'pending',
                        midterm_done INTEGER NOT NULL DEFAULT 0,
                        longterm_done INTEGER NOT NULL DEFAULT 0,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error TEXT,
                        filters_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        infer INTEGER NOT NULL DEFAULT 1,
                        prompt TEXT,
                        sequence_no INTEGER NOT NULL,
                        degraded INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(session_scope, sequence_no)
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_claim
                    ON memory_migration_jobs(status, next_retry_at, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS profile_update_jobs (
                        job_id TEXT PRIMARY KEY,
                        trace_id TEXT,
                        user_id TEXT NOT NULL,
                        run_id TEXT,
                        session_scope TEXT,
                        messages_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        next_retry_at TEXT,
                        last_error TEXT,
                        sequence_no INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE(user_id, sequence_no)
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_profile_jobs_claim
                    ON profile_update_jobs(status, next_retry_at, created_at)
                    """
                )
                migration_columns = {
                    row[1] for row in self.connection.execute("PRAGMA table_info(memory_migration_jobs)").fetchall()
                }
                if "trace_id" not in migration_columns:
                    self.connection.execute("ALTER TABLE memory_migration_jobs ADD COLUMN trace_id TEXT")
                added_migration_scope_columns = False
                if "user_id" not in migration_columns:
                    self.connection.execute("ALTER TABLE memory_migration_jobs ADD COLUMN user_id TEXT")
                    added_migration_scope_columns = True
                if "run_id" not in migration_columns:
                    self.connection.execute("ALTER TABLE memory_migration_jobs ADD COLUMN run_id TEXT")
                    added_migration_scope_columns = True
                if added_migration_scope_columns:
                    self.connection.execute(
                        """
                        UPDATE memory_migration_jobs
                        SET user_id = COALESCE(user_id, json_extract(filters_json, '$.user_id')),
                            run_id = COALESCE(run_id, json_extract(filters_json, '$.run_id'))
                        WHERE user_id IS NULL OR run_id IS NULL
                        """
                    )
                profile_columns = {
                    row[1] for row in self.connection.execute("PRAGMA table_info(profile_update_jobs)").fetchall()
                }
                if "trace_id" not in profile_columns:
                    self.connection.execute("ALTER TABLE profile_update_jobs ADD COLUMN trace_id TEXT")
                if "run_id" not in profile_columns:
                    self.connection.execute("ALTER TABLE profile_update_jobs ADD COLUMN run_id TEXT")
                if "session_scope" not in profile_columns:
                    self.connection.execute("ALTER TABLE profile_update_jobs ADD COLUMN session_scope TEXT")
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_trace
                    ON memory_migration_jobs(trace_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_user_created
                    ON memory_migration_jobs(user_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_migration_jobs_run_created
                    ON memory_migration_jobs(run_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_profile_jobs_trace
                    ON profile_update_jobs(trace_id, created_at)
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create background job tables: %s", e)
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
                        trace_id TEXT,
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
                        CHECK (source_type IN ('explicit', 'inferred', 'imported')),
                        CHECK (confidence >= 0 AND confidence <= 1),
                        CHECK (value_version >= 1),
                        CHECK (json_valid(value_json))
                    )
                    """
                )
                columns = {
                    row[1] for row in self.connection.execute("PRAGMA table_info(user_profile_values)").fetchall()
                }
                if "trace_id" not in columns:
                    self.connection.execute("ALTER TABLE user_profile_values ADD COLUMN trace_id TEXT")
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_profile_values_trace
                    ON user_profile_values(trace_id, updated_at)
                    """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to create profile tables: %s", e)
                raise

    def _create_observation_events_table(self) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS memory_observation_events (
                        event_id TEXT PRIMARY KEY,
                        trace_id TEXT NOT NULL,
                        job_id TEXT,
                        job_type TEXT,
                        stage TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        user_id TEXT,
                        run_id TEXT,
                        session_scope TEXT,
                        entity_type TEXT,
                        entity_id TEXT,
                        duration_ms REAL,
                        input_json TEXT,
                        output_json TEXT,
                        before_json TEXT,
                        after_json TEXT,
                        error_type TEXT,
                        error_message TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_trace_created
                    ON memory_observation_events(trace_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_job_created
                    ON memory_observation_events(job_id, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_status_created
                    ON memory_observation_events(status, created_at)
                    """
                )
                self.connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_observation_user_created
                    ON memory_observation_events(user_id, created_at)
                    """
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                logger.exception("Failed to create memory observation event table")
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

    def save_messages(
        self,
        messages: List[Dict[str, Any]],
        session_scope: str,
        max_messages: int = 10,
        return_evicted: bool = False,
        trace_id: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        if not messages:
            return [] if return_evicted else None
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                for message in messages:
                    now = normalize_iso_timestamp_to_beijing(message.get("created_at")) or beijing_now_iso()
                    self.connection.execute(
                        """
                        INSERT INTO messages (
                            id, session_scope, trace_id, user_id, run_id,
                            role, content, name, created_at, status, migration_job_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)
                    """,
                        (
                            str(uuid.uuid4()),
                            session_scope,
                            trace_id,
                            self._session_scope_value(session_scope, "user_id"),
                            self._session_scope_value(session_scope, "run_id"),
                            message.get("role"),
                            message.get("content"),
                            message.get("name"),
                            now,
                        ),
                    )
                max_messages = max(int(max_messages), 0)

                rows = self.connection.execute(
                    """
                    SELECT id, role, content, name, created_at, trace_id
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

                evicted_messages = []
                for row in evicted_rows:
                    message = {
                        "id": row[0],
                        "role": row[1],
                        "content": row[2],
                        "name": row[3],
                        "created_at": row[4],
                        "session_scope": session_scope,
                    }
                    if row[5] is not None:
                        message["trace_id"] = row[5]
                    evicted_messages.append(message)

                if not return_evicted:
                    evicted_messages = None

                self.connection.execute("COMMIT")
                return evicted_messages
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to save messages: {e}")
                raise

    def get_messages(self, session_scope: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self.connection.execute(
                """
                SELECT id, role, content, name, created_at, trace_id
                FROM messages
                WHERE session_scope = ? AND status = 'active'
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                LIMIT ?
            """,
                (session_scope, limit),
            )
            rows = cur.fetchall()

        messages = []
        for row in rows:
            message = {
                "id": row[0],
                "role": row[1],
                "content": row[2],
                "name": row[3],
                "created_at": row[4],
                "session_scope": session_scope,
            }
            if row[5] is not None:
                message["trace_id"] = row[5]
            messages.append(message)
        return messages

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
                SELECT role, content, name, created_at FROM (
                    SELECT rowid, role, content, name, created_at
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

    def insert_observation_event(self, event: Dict[str, Any]) -> None:
        """Insert a normalized monitoring event using the manager's write lock."""

        columns = (
            "event_id",
            "trace_id",
            "job_id",
            "job_type",
            "stage",
            "event_type",
            "status",
            "user_id",
            "run_id",
            "session_scope",
            "entity_type",
            "entity_id",
            "duration_ms",
            "input_json",
            "output_json",
            "before_json",
            "after_json",
            "error_type",
            "error_message",
            "created_at",
        )
        with self._lock:
            self.connection.execute(
                f"""
                INSERT INTO memory_observation_events ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                tuple(event.get(column) for column in columns),
            )
            self.connection.commit()

    @staticmethod
    def _decode_migration_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        decoded = dict(job)
        decoded["filters"] = json.loads(decoded.pop("filters_json"))
        decoded["metadata"] = json.loads(decoded.pop("metadata_json"))
        decoded["infer"] = bool(decoded["infer"])
        decoded["midterm_done"] = bool(decoded["midterm_done"])
        decoded["longterm_done"] = bool(decoded["longterm_done"])
        decoded["degraded"] = bool(decoded["degraded"])
        if decoded.get("trace_id") is None:
            decoded.pop("trace_id", None)
        return decoded

    @staticmethod
    def _decode_profile_job(job: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if job is None:
            return None
        decoded = dict(job)
        decoded["messages"] = json.loads(decoded.pop("messages_json"))
        if decoded.get("trace_id") is None:
            decoded.pop("trace_id", None)
        return decoded

    @staticmethod
    def _session_scope_value(session_scope: str, key: str) -> Optional[str]:
        prefix = f"{key}="
        for item in (session_scope or "").split("&"):
            if item.startswith(prefix):
                return item.split("=", 1)[1]
        return None

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
        trace_id: Optional[str] = None,
    ) -> Optional[str]:
        """Synchronously save messages and atomically reserve active overflow for migration."""
        if not messages:
            return None

        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                for message in messages:
                    created_at = normalize_iso_timestamp_to_beijing(message.get("created_at")) or beijing_now_iso()
                    self.connection.execute(
                        """
                        INSERT INTO messages (
                            id, session_scope, trace_id, user_id, run_id,
                            role, content, name, created_at, status, migration_job_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL)
                        """,
                        (
                            str(uuid.uuid4()),
                            session_scope,
                            trace_id,
                            filters.get("user_id"),
                            filters.get("run_id"),
                            message.get("role"),
                            message.get("content"),
                            message.get("name"),
                            created_at,
                        ),
                    )

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

                # Do not split a user/assistant QA pair at the migration boundary.
                if (
                    reserved_rows
                    and reserved_rows[-1][2] == "user"
                    and len(active_rows) > len(reserved_rows)
                    and active_rows[len(reserved_rows)][2] == "assistant"
                ):
                    reserved_rows = active_rows[: len(reserved_rows) + 1]

                if not reserved_rows:
                    self.connection.execute("COMMIT")
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
                self.connection.execute(
                    """
                    INSERT INTO memory_migration_jobs (
                        job_id, trace_id, session_scope, user_id, run_id,
                        status, midterm_done, longterm_done,
                        attempts, next_retry_at, last_error, filters_json,
                        metadata_json, infer, prompt, sequence_no, degraded,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'pending', 0, 0, 0, NULL, NULL, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        job_id,
                        trace_id,
                        session_scope,
                        filters.get("user_id"),
                        filters.get("run_id"),
                        self._json_dumps(filters),
                        self._json_dumps(metadata),
                        int(bool(infer)),
                        prompt,
                        sequence_no,
                        now,
                        now,
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
                self.connection.execute("COMMIT")
                return job_id
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error("Failed to save messages and create migration job for %s: %s", session_scope, e)
                raise

    def create_profile_update_job(
        self,
        user_id: str,
        messages: List[Dict[str, Any]],
        *,
        trace_id: Optional[str] = None,
        run_id: Optional[str] = None,
        session_scope: Optional[str] = None,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
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
                        job_id, trace_id, user_id, run_id, session_scope, messages_json, status, attempts,
                        next_retry_at, last_error, sequence_no, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        job_id,
                        trace_id,
                        user_id,
                        run_id,
                        session_scope,
                        self._json_dumps(messages),
                        sequence_no,
                        now,
                        now,
                    ),
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
                SELECT id, session_scope, trace_id, role, content, name, created_at, status
                FROM messages
                WHERE migration_job_id = ?
                ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (job_id,),
            ).fetchall()
        messages = []
        for row in rows:
            message = {
                "id": row[0],
                "session_scope": row[1],
                "role": row[3],
                "content": row[4],
                "name": row[5],
                "created_at": row[6],
                "status": row[7],
            }
            if row[2] is not None:
                message["trace_id"] = row[2]
            messages.append(message)
        return messages

    def get_context_messages(
        self,
        session_scope: str,
        *,
        active_limit: int,
        include_pending: bool,
        max_pending: int,
        include_failed: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return active-window messages plus a bounded migration-in-flight bridge."""
        with self._lock:
            active_rows = self.connection.execute(
                """
                SELECT rowid, role, content, name, created_at, status FROM (
                    SELECT rowid, role, content, name, created_at, status
                    FROM messages
                    WHERE session_scope = ? AND status = 'active'
                    ORDER BY DATETIME(created_at) DESC, rowid DESC
                    LIMIT ?
                ) ORDER BY DATETIME(created_at) ASC, rowid ASC
                """,
                (session_scope, max(int(active_limit), 0)),
            ).fetchall()
            migration_rows = []
            if include_pending and max_pending > 0:
                statuses = ["pending", "processing"]
                if include_failed:
                    statuses.append("failed")
                placeholders = ",".join("?" for _ in statuses)
                migration_rows = self.connection.execute(
                    f"""
                    SELECT rowid, role, content, name, created_at, status FROM (
                        SELECT rowid, role, content, name, created_at, status
                        FROM messages
                        WHERE session_scope = ? AND status IN ({placeholders})
                        ORDER BY DATETIME(created_at) DESC, rowid DESC
                        LIMIT ?
                    ) ORDER BY DATETIME(created_at) ASC, rowid ASC
                    """,
                    (session_scope, *statuses, int(max_pending)),
                ).fetchall()

        rows = sorted([*migration_rows, *active_rows], key=lambda row: (row[4] or "", row[0]))
        return [
            {
                "role": row[1],
                "content": row[2],
                "name": row[3],
                "created_at": row[4],
                "status": row[5],
            }
            for row in rows
        ]

    def recover_stale_background_jobs(self, stale_timeout_seconds: int) -> Dict[str, int]:
        cutoff = (beijing_now() - timedelta(seconds=max(int(stale_timeout_seconds), 1))).isoformat()
        now = beijing_now_iso()
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                migration_ids = [
                    row[0]
                    for row in self.connection.execute(
                        """
                        SELECT job_id FROM memory_migration_jobs
                        WHERE status = 'running' AND updated_at <= ?
                        """,
                        (cutoff,),
                    ).fetchall()
                ]
                if migration_ids:
                    placeholders = ",".join("?" for _ in migration_ids)
                    self.connection.execute(
                        f"""
                        UPDATE memory_migration_jobs
                        SET status = 'retry', next_retry_at = ?, updated_at = ?,
                            last_error = COALESCE(last_error, 'recovered stale running job')
                        WHERE job_id IN ({placeholders})
                        """,
                        (now, now, *migration_ids),
                    )
                    self.connection.execute(
                        f"""
                        UPDATE messages SET status = 'pending'
                        WHERE migration_job_id IN ({placeholders}) AND status = 'processing'
                        """,
                        tuple(migration_ids),
                    )

                profile_cursor = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = 'retry', next_retry_at = ?, updated_at = ?,
                        last_error = COALESCE(last_error, 'recovered stale running job')
                    WHERE status = 'running' AND updated_at <= ?
                    """,
                    (now, now, cutoff),
                )
                self.connection.execute("COMMIT")
                return {"migration": len(migration_ids), "profile": profile_cursor.rowcount}
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def claim_next_migration_job(self) -> Optional[Dict[str, Any]]:
        return self._claim_migration_job()

    def claim_migration_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Claim one runnable migration job without bypassing per-session ordering."""

        return self._claim_migration_job(job_id)

    def _claim_migration_job(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = beijing_now_iso()
        target_clause = "AND candidate.job_id = ?" if job_id else ""
        parameters = (now, job_id) if job_id else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.*
                    FROM memory_migration_jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry')
                      AND (candidate.next_retry_at IS NULL OR candidate.next_retry_at <= ?)
                      {target_clause}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM memory_migration_jobs AS earlier
                          WHERE earlier.session_scope = candidate.session_scope
                            AND earlier.sequence_no < candidate.sequence_no
                            AND earlier.status NOT IN ('succeeded', 'succeeded_degraded', 'dead')
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
                claimed_from_status = job["status"]
                updated = self.connection.execute(
                    """
                    UPDATE memory_migration_jobs
                    SET status = 'running', updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                    """,
                    (now, job["job_id"]),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self.connection.execute(
                    """
                    UPDATE messages SET status = 'processing'
                    WHERE migration_job_id = ? AND status IN ('pending', 'failed')
                    """,
                    (job["job_id"],),
                )
                self.connection.execute("COMMIT")
                job["status"] = "running"
                job["updated_at"] = now
                job["claimed_from_status"] = claimed_from_status
                return self._decode_migration_job(job)
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def claim_next_profile_job(self) -> Optional[Dict[str, Any]]:
        return self._claim_profile_job()

    def claim_profile_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Claim one runnable profile job without bypassing per-user ordering."""

        return self._claim_profile_job(job_id)

    def _claim_profile_job(self, job_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = beijing_now_iso()
        target_clause = "AND candidate.job_id = ?" if job_id else ""
        parameters = (now, job_id) if job_id else (now,)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    SELECT candidate.*
                    FROM profile_update_jobs AS candidate
                    WHERE candidate.status IN ('pending', 'retry')
                      AND (candidate.next_retry_at IS NULL OR candidate.next_retry_at <= ?)
                      {target_clause}
                      AND NOT EXISTS (
                          SELECT 1
                          FROM profile_update_jobs AS earlier
                          WHERE earlier.user_id = candidate.user_id
                            AND earlier.sequence_no < candidate.sequence_no
                            AND earlier.status NOT IN ('succeeded', 'dead')
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
                claimed_from_status = job["status"]
                updated = self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = 'running', updated_at = ?
                    WHERE job_id = ? AND status IN ('pending', 'retry')
                    """,
                    (now, job["job_id"]),
                )
                if updated.rowcount != 1:
                    self.connection.execute("ROLLBACK")
                    return None
                self.connection.execute("COMMIT")
                job["status"] = "running"
                job["updated_at"] = now
                job["claimed_from_status"] = claimed_from_status
                return self._decode_profile_job(job)
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def mark_migration_stage_done(self, job_id: str, stage: str, *, degraded: bool = False) -> None:
        if stage not in {"midterm", "longterm"}:
            raise ValueError("stage must be 'midterm' or 'longterm'")
        column = f"{stage}_done"
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    f"""
                    UPDATE memory_migration_jobs
                    SET {column} = 1, attempts = 0, next_retry_at = NULL,
                        last_error = NULL, degraded = MAX(degraded, ?), updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (int(degraded), beijing_now_iso(), job_id),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def record_migration_failure(
        self,
        job_id: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT attempts FROM memory_migration_jobs WHERE job_id = ? AND status = 'running'",
                    (job_id,),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "missing"
                attempts = int(row[0]) + 1
                now = beijing_now()
                if attempts <= max_retries:
                    status = "retry"
                    next_retry_at = (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                    self.connection.execute(
                        """
                        UPDATE memory_migration_jobs
                        SET status = 'retry', attempts = ?, next_retry_at = ?,
                            last_error = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (attempts, next_retry_at, error, now.isoformat(), job_id),
                    )
                    self.connection.execute(
                        """
                        UPDATE messages SET status = 'pending'
                        WHERE migration_job_id = ? AND status = 'processing'
                        """,
                        (job_id,),
                    )
                else:
                    status = "exhausted"
                    self.connection.execute(
                        """
                        UPDATE memory_migration_jobs
                        SET attempts = ?, last_error = ?, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (attempts, error, now.isoformat(), job_id),
                    )
                self.connection.execute("COMMIT")
                return status
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def finish_migration_job(self, job_id: str) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    """
                    SELECT midterm_done, longterm_done, degraded
                    FROM memory_migration_jobs
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (job_id,),
                ).fetchone()
                if row is None or not bool(row[0]) or not bool(row[1]):
                    raise RuntimeError(f"Migration job {job_id} is not ready to finish")
                self.connection.execute("DELETE FROM messages WHERE migration_job_id = ?", (job_id,))
                status = "succeeded_degraded" if bool(row[2]) else "succeeded"
                self.connection.execute(
                    """
                    UPDATE memory_migration_jobs
                    SET status = ?, next_retry_at = NULL, last_error = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, beijing_now_iso(), job_id),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def mark_migration_dead(self, job_id: str, error: str) -> None:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                now = beijing_now_iso()
                self.connection.execute(
                    """
                    UPDATE memory_migration_jobs
                    SET status = 'dead', next_retry_at = NULL, last_error = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (error, now, job_id),
                )
                self.connection.execute(
                    "UPDATE messages SET status = 'failed' WHERE migration_job_id = ?",
                    (job_id,),
                )
                self.connection.execute("COMMIT")
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

    def finish_profile_job(self, job_id: str) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE profile_update_jobs
                SET status = 'succeeded', next_retry_at = NULL, last_error = NULL, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (beijing_now_iso(), job_id),
            )
            self.connection.commit()

    def record_profile_failure(
        self,
        job_id: str,
        error: str,
        *,
        max_retries: int,
        retry_delay_seconds: float,
    ) -> str:
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT attempts FROM profile_update_jobs WHERE job_id = ? AND status = 'running'",
                    (job_id,),
                ).fetchone()
                if row is None:
                    self.connection.execute("COMMIT")
                    return "missing"
                attempts = int(row[0]) + 1
                now = beijing_now()
                if attempts <= max_retries:
                    status = "retry"
                    next_retry_at = (now + timedelta(seconds=max(float(retry_delay_seconds), 0))).isoformat()
                else:
                    status = "dead"
                    next_retry_at = None
                self.connection.execute(
                    """
                    UPDATE profile_update_jobs
                    SET status = ?, attempts = ?, next_retry_at = ?,
                        last_error = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, attempts, next_retry_at, error, now.isoformat(), job_id),
                )
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
                WHERE status IN ('pending', 'running', 'retry')
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
            return migration is not None or profile is not None

    def get_background_job(self, job_id: str, job_type: str = "migration") -> Optional[Dict[str, Any]]:
        table = "memory_migration_jobs" if job_type == "migration" else "profile_update_jobs"
        if job_type not in {"migration", "profile"}:
            raise ValueError("job_type must be 'migration' or 'profile'")
        with self._lock:
            cursor = self.connection.execute(f"SELECT * FROM {table} WHERE job_id = ?", (job_id,))
            job = self._row_as_dict(cursor, cursor.fetchone())
        if job_type == "migration":
            return self._decode_migration_job(job)
        return self._decode_profile_job(job)

    def retry_background_job(self, job_id: str, job_type: str = "migration") -> bool:
        if job_type not in {"migration", "profile"}:
            raise ValueError("job_type must be 'migration' or 'profile'")
        table = "memory_migration_jobs" if job_type == "migration" else "profile_update_jobs"
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                cursor = self.connection.execute(
                    f"""
                    UPDATE {table}
                    SET status = 'pending', attempts = 0, next_retry_at = NULL,
                        last_error = NULL, updated_at = ?
                    WHERE job_id = ? AND status = 'dead'
                    """,
                    (beijing_now_iso(), job_id),
                )
                if job_type == "migration" and cursor.rowcount:
                    self.connection.execute(
                        "UPDATE messages SET status = 'pending' WHERE migration_job_id = ?",
                        (job_id,),
                    )
                self.connection.execute("COMMIT")
                return cursor.rowcount == 1
            except Exception:
                self.connection.execute("ROLLBACK")
                raise

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
        value = {
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
        if row[15] is not None:
            value["trace_id"] = row[15]
        return value

    def _get_user_profile_values_locked(self, user_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT v.user_id, a.attribute_id, a.attribute_key, a.attribute_name,
                   a.attribute_category, a.description, a.value_type,
                   a.value_schema_json, a.merge_policy, v.value_json,
                   v.source_type, v.confidence, v.value_version,
                   v.created_at, v.updated_at, v.trace_id
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
            SELECT value_json, source_type, confidence, value_version, created_at, updated_at, trace_id
            FROM user_profile_values
            WHERE user_id = ? AND attribute_id = ?
            """,
            (user_id, attribute_id),
        ).fetchone()
        if row is None:
            return None
        value = {
            "value": json.loads(row[0]),
            "source_type": row[1],
            "confidence": row[2],
            "value_version": row[3],
            "created_at": row[4],
            "updated_at": row[5],
        }
        if row[6] is not None:
            value["trace_id"] = row[6]
        return value

    def _upsert_user_profile_value_locked(
        self,
        user_id: str,
        attribute_id: int,
        value_json: str,
        source_type: str,
        confidence: float,
        trace_id: Optional[str] = None,
    ) -> None:
        now = beijing_now_iso()
        self.connection.execute(
            """
            INSERT INTO user_profile_values (
                user_id, attribute_id, trace_id, value_json, source_type, confidence,
                value_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(user_id, attribute_id) DO UPDATE SET
                trace_id = excluded.trace_id,
                value_json = excluded.value_json,
                source_type = excluded.source_type,
                confidence = excluded.confidence,
                value_version = user_profile_values.value_version + 1,
                updated_at = excluded.updated_at
            """,
            (user_id, attribute_id, trace_id, value_json, source_type, confidence, now, now),
        )

    @staticmethod
    def _validate_value_metadata(source_type: str, confidence: float) -> None:
        if source_type not in {"explicit", "inferred", "imported"}:
            raise ValueError("source_type must be explicit, inferred, or imported")
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
        trace_id: Optional[str] = None,
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
                    trace_id,
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
        trace_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Atomically apply a validated profile plan against the latest stored values."""
        update_plan = plan if isinstance(plan, ProfileUpdatePlan) else ProfileUpdatePlan.model_validate(plan)
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
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
                        "explicit",
                        1.0,
                        trace_id,
                    )
                values = self._get_user_profile_values_locked(user_id)
                self.connection.execute("COMMIT")
                return values
            except Exception:
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
                self.connection.execute("DROP TABLE IF EXISTS memory_migration_jobs")
                self.connection.execute("DROP TABLE IF EXISTS memory_observation_events")
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
