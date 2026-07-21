import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
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
        self._create_profile_tables()
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
                self.connection.execute("BEGIN")
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        session_scope TEXT,
                        role TEXT,
                        content TEXT,
                        name TEXT,
                        created_at DATETIME
                    )
                """
                )
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to create messages table: {e}")
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
                        CHECK (source_type IN ('explicit', 'inferred', 'imported')),
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
                now = datetime.now(timezone.utc).isoformat()
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
                            record.get("created_at"),
                            record.get("updated_at"),
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
    ) -> Optional[List[Dict[str, Any]]]:
        if not messages:
            return [] if return_evicted else None
        with self._lock:
            try:
                self.connection.execute("BEGIN")
                for message in messages:
                    now = message.get("created_at") or datetime.now(timezone.utc).isoformat()
                    self.connection.execute(
                        """
                        INSERT INTO messages (id, session_scope, role, content, name, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """,
                        (
                            str(uuid.uuid4()),
                            session_scope,
                            message.get("role"),
                            message.get("content"),
                            message.get("name"),
                            now,
                        ),
                )
                max_messages = max(int(max_messages), 0)

                rows = self.connection.execute(
                    """
                    SELECT id, role, content, name, created_at
                    FROM messages
                    WHERE session_scope = ?
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

                evicted_messages = [
                    {
                        "id": r[0],
                        "role": r[1],
                        "content": r[2],
                        "name": r[3],
                        "created_at": r[4],
                        "session_scope": session_scope,
                    }
                    for r in evicted_rows
                ]

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
                SELECT id, role, content, name, created_at
                FROM messages
                WHERE session_scope = ?
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
                SELECT role, content, name, created_at FROM (
                    SELECT rowid, role, content, name, created_at
                    FROM messages
                    WHERE session_scope = ?
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
        now = datetime.now(timezone.utc).isoformat()
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
                    (datetime.now(timezone.utc).isoformat(), attribute_key),
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
        now = datetime.now(timezone.utc).isoformat()
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
                self.connection.execute("DROP TABLE IF EXISTS history")
                self.connection.execute("DROP TABLE IF EXISTS messages")
                self.connection.execute("COMMIT")
            except Exception as e:
                self.connection.execute("ROLLBACK")
                logger.error(f"Failed to reset tables: {e}")
                raise

    def close(self) -> None:
        if self.connection:
            self.connection.close()
            self.connection = None

    def __del__(self):
        self.close()
