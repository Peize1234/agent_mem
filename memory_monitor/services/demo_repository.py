from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from memory_monitor.models.demo_pipeline import (
    INFLIGHT_STEP_STATUSES,
    MEMORY_STEPS,
    OPTIONAL_PIPELINE_STEPS,
    PIPELINE_STEPS,
    TERMINAL_STEP_STATUSES,
    BackgroundStepConfig,
    PipelineStep,
    StepStatus,
)

_JSON_COLUMNS = {
    "input_json": "input",
    "output_json": "output",
    "diff_json": "diff",
    "data_json": "data",
    "generation_json": "generation",
    "commit_json": "commit",
}


class StepAlreadyRunningError(RuntimeError):
    """Raised when another page or process owns the same step lease."""


class StepHeldError(RuntimeError):
    """Raised when a persisted hold wins a race with automatic scheduling."""


class TurnSessionMismatchError(ValueError):
    """Raised when a turn is accessed through a different demo session."""


class DemoRepository:
    """SQLite persistence for original chat history and resumable demo steps."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS demo_sessions (
                    session_id TEXT PRIMARY KEY,
                    simulation_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (simulation_id, user_id, run_id)
                );

                CREATE TABLE IF NOT EXISTS demo_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES demo_sessions(session_id) ON DELETE CASCADE,
                    user_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    user_message TEXT NOT NULL,
                    custom_prompt TEXT,
                    assistant_message TEXT,
                    generation_json TEXT,
                    commit_json TEXT,
                    run_shortterm INTEGER NOT NULL DEFAULT 1,
                    run_midterm INTEGER NOT NULL DEFAULT 1,
                    run_longterm INTEGER NOT NULL DEFAULT 1,
                    run_profile INTEGER NOT NULL DEFAULT 1,
                    background_submitted_at TEXT,
                    execution_target TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS demo_step_runs (
                    turn_id TEXT NOT NULL REFERENCES demo_turns(turn_id) ON DELETE CASCADE,
                    step TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    is_held INTEGER NOT NULL DEFAULT 0,
                    input_json TEXT,
                    output_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
                    queued_at TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    duration_ms REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lock_token TEXT,
                    lease_expires_at TEXT,
                    before_snapshot_id TEXT,
                    after_snapshot_id TEXT,
                    diff_json TEXT,
                    skip_reason TEXT,
                    PRIMARY KEY (turn_id, step)
                );

                CREATE TABLE IF NOT EXISTS demo_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL REFERENCES demo_turns(turn_id) ON DELETE CASCADE,
                    step TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_demo_turns_session
                    ON demo_turns(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_demo_steps_status
                    ON demo_step_runs(turn_id, status, position);
                CREATE INDEX IF NOT EXISTS idx_demo_snapshots_turn
                    ON demo_snapshots(turn_id, step, phase);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            self._migrate_turn_columns(connection)
            self._migrate_pipeline_steps(connection)
            self._recover_expired_step_leases(connection)
            connection.execute("COMMIT")

    @staticmethod
    def _migrate_turn_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(demo_turns)").fetchall()}
        additions = {
            "custom_prompt": "TEXT",
            "run_shortterm": "INTEGER NOT NULL DEFAULT 1",
            "run_midterm": "INTEGER NOT NULL DEFAULT 1",
            "run_longterm": "INTEGER NOT NULL DEFAULT 1",
            "run_profile": "INTEGER NOT NULL DEFAULT 1",
            "background_submitted_at": "TEXT",
            "execution_target": "TEXT",
            "completed_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE demo_turns ADD COLUMN {name} {declaration}")
        step_columns = {row["name"] for row in connection.execute("PRAGMA table_info(demo_step_runs)").fetchall()}
        if "queued_at" not in step_columns:
            connection.execute("ALTER TABLE demo_step_runs ADD COLUMN queued_at TEXT")
        if "is_held" not in step_columns:
            connection.execute("ALTER TABLE demo_step_runs ADD COLUMN is_held INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_pipeline_steps(connection: sqlite3.Connection) -> None:
        missing_agentic_turn_ids = {
            row["turn_id"]
            for row in connection.execute(
                """
                SELECT turn.turn_id
                FROM demo_turns AS turn
                LEFT JOIN demo_step_runs AS step
                  ON step.turn_id = turn.turn_id AND step.step = ?
                WHERE step.turn_id IS NULL
                """,
                (PipelineStep.AGENTIC_RETRIEVAL.value,),
            ).fetchall()
        }
        for position, step in enumerate(PIPELINE_STEPS):
            connection.execute(
                """
                INSERT OR IGNORE INTO demo_step_runs (turn_id, step, position, status)
                SELECT turn_id, ?, ?, ? FROM demo_turns
                """,
                (step.value, position, StepStatus.PENDING.value),
            )
            connection.execute(
                "UPDATE demo_step_runs SET position = ? WHERE step = ?",
                (position, step.value),
            )
        if missing_agentic_turn_ids:
            placeholders = ", ".join("?" for _ in missing_agentic_turn_ids)
            connection.execute(
                f"""
                UPDATE demo_step_runs
                SET status = ?, output_json = ?, skip_reason = ?,
                    started_at = NULL, ended_at = NULL, duration_ms = NULL, attempts = 0
                WHERE step = ?
                  AND turn_id IN ({placeholders})
                  AND turn_id IN (
                      SELECT turn.turn_id
                      FROM demo_turns AS turn
                      LEFT JOIN demo_step_runs AS generation
                        ON generation.turn_id = turn.turn_id AND generation.step = ?
                      WHERE turn.assistant_message IS NOT NULL
                         OR turn.completed_at IS NOT NULL
                         OR generation.status = ?
                  )
                """,
                (
                    StepStatus.SKIPPED.value,
                    _json({"agentic_status": "legacy_compatible", "llm_calls": [], "tool_calls": []}),
                    "Historical turn predates the Agentic retrieval step",
                    PipelineStep.AGENTIC_RETRIEVAL.value,
                    *sorted(missing_agentic_turn_ids),
                    PipelineStep.GENERATE_RESPONSE.value,
                    StepStatus.SUCCEEDED.value,
                ),
            )
        # The previous Demo UI represented a disabled branch as ``skipped``.
        # For unfinished turns, recover only those legacy rows as a persisted
        # hold. Historical completed turns and system-level skips are retained.
        for step, config_column in (
            (PipelineStep.RUN_SHORTTERM, "run_shortterm"),
            (PipelineStep.RUN_MIDTERM, "run_midterm"),
            (PipelineStep.RUN_LONGTERM, "run_longterm"),
            (PipelineStep.RUN_PROFILE, "run_profile"),
        ):
            connection.execute(
                f"""
                UPDATE demo_step_runs
                SET status = ?, is_held = CASE WHEN turn.{config_column} = 0 THEN 1 ELSE 0 END,
                    skip_reason = NULL, ended_at = NULL
                FROM demo_turns AS turn
                WHERE demo_step_runs.turn_id = turn.turn_id
                  AND turn.completed_at IS NULL
                  AND demo_step_runs.step = ?
                  AND demo_step_runs.status = ?
                  AND demo_step_runs.skip_reason = 'Disabled by turn configuration'
                """,
                (StepStatus.PENDING.value, step.value, StepStatus.SKIPPED.value),
            )

    @staticmethod
    def _recover_expired_step_leases(connection: sqlite3.Connection) -> None:
        now = _now()
        connection.execute(
            """
            UPDATE demo_step_runs
            SET status = ?, queued_at = NULL, lock_token = NULL, lease_expires_at = NULL,
                error_type = 'RecoveredExpiredLease',
                error_message = 'Recovered an expired queued Demo step lease'
            WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            """,
            (StepStatus.PENDING.value, StepStatus.QUEUED.value, now),
        )
        connection.execute(
            """
            UPDATE demo_step_runs
            SET status = ?, lock_token = NULL, lease_expires_at = NULL,
                ended_at = ?,
                error_type = 'RecoveredExpiredLease',
                error_message = 'The Demo worker lease expired; retry this step'
            WHERE status = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?
            """,
            (StepStatus.FAILED.value, now, StepStatus.RUNNING.value, now),
        )

    def create_session(
        self,
        simulation_id: str,
        user_id: str,
        run_id: str,
        *,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = _now()
        session_id = session_id or str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO demo_sessions (
                    session_id, simulation_id, user_id, run_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(simulation_id, user_id, run_id)
                DO UPDATE SET updated_at = excluded.updated_at
                """,
                (session_id, simulation_id, user_id, run_id, now, now),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT * FROM demo_sessions
                WHERE simulation_id = ? AND user_id = ? AND run_id = ?
                """,
                (simulation_id, user_id, run_id),
            ).fetchone()
        return dict(row)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM demo_sessions WHERE session_id = ?", (session_id,))

    def find_session(
        self,
        simulation_id: str,
        user_id: str,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Find an existing session without refreshing its persisted timestamp."""
        return self._one(
            """
            SELECT * FROM demo_sessions
            WHERE simulation_id = ? AND user_id = ? AND run_id = ?
            """,
            (simulation_id, user_id, run_id),
        )

    def create_turn(
        self,
        session_id: str,
        *,
        user_id: str,
        run_id: str,
        user_message: str,
        custom_prompt: Optional[str] = None,
        turn_id: Optional[str] = None,
        background_config: BackgroundStepConfig | Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        if not user_message or not user_message.strip():
            raise ValueError("user_message is required")
        config = BackgroundStepConfig.from_mapping(
            background_config.as_dict() if isinstance(background_config, BackgroundStepConfig) else background_config
        )
        custom_prompt = custom_prompt.strip() if custom_prompt else None
        custom_prompt = custom_prompt or None
        turn_id = turn_id or str(uuid.uuid4())
        now = _now()
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                session = connection.execute(
                    "SELECT user_id, run_id FROM demo_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(f"Unknown demo session: {session_id}")
                if session["user_id"] != user_id or session["run_id"] != run_id:
                    raise ValueError("turn scope does not match its demo session")
                connection.execute(
                    """
                    INSERT INTO demo_turns (
                        turn_id, session_id, user_id, run_id, user_message, custom_prompt,
                        run_shortterm, run_midterm, run_longterm, run_profile,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn_id,
                        session_id,
                        user_id,
                        run_id,
                        user_message.strip(),
                        custom_prompt,
                        int(config.run_shortterm),
                        int(config.run_midterm),
                        int(config.run_longterm),
                        int(config.run_profile),
                        now,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO demo_step_runs (turn_id, step, position, status, is_held)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            turn_id,
                            step.value,
                            position,
                            StepStatus.PENDING.value,
                            int(step in MEMORY_STEPS and not config.enabled(step)),
                        )
                        for position, step in enumerate(PIPELINE_STEPS)
                    ],
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM demo_turns WHERE turn_id = ?", (turn_id,))

    def assert_turn_belongs_to_session(self, turn_id: str, session_id: str) -> Dict[str, Any]:
        turn = self.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"Unknown demo turn: {turn_id}")
        if turn["session_id"] != session_id:
            raise TurnSessionMismatchError(
                "Demo turn does not belong to the requested session: "
                f"turn_id={turn_id} expected_session_id={session_id} actual_session_id={turn['session_id']}"
            )
        return turn

    def list_turns(self, session_id: str) -> list[Dict[str, Any]]:
        return self._all(
            "SELECT * FROM demo_turns WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        )

    def list_completed_turns(self, session_id: str) -> list[Dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM demo_turns
            WHERE session_id = ? AND completed_at IS NOT NULL
            ORDER BY created_at ASC, rowid ASC
            """,
            (session_id,),
        )

    def list_scheduled_turns(self) -> list[Dict[str, Any]]:
        return self._all(
            """
            SELECT * FROM demo_turns
            WHERE execution_target IS NOT NULL
            ORDER BY created_at ASC, rowid ASC
            """,
            (),
        )

    def list_active_turns(
        self,
        session_id: str,
        *,
        include_recently_completed_seconds: float = 0,
    ) -> list[Dict[str, Any]]:
        include_seconds = max(float(include_recently_completed_seconds), 0)
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=include_seconds)).isoformat()
        completion_filter = (
            "(turn.completed_at IS NULL OR turn.completed_at >= ?)"
            if include_seconds > 0
            else "turn.completed_at IS NULL"
        )
        step_placeholders = ", ".join("?" for _ in PIPELINE_STEPS)
        turns = self._all(
            f"""
            SELECT turn.*,
                   COUNT(step.step) AS _total_steps,
                   SUM(CASE WHEN step.status IN (?, ?) THEN 1 ELSE 0 END) AS _completed_steps,
                   MAX(CASE WHEN step.status = ? THEN 1 ELSE 0 END) AS _has_failure,
                   MAX(CASE WHEN step.status = ? THEN 1 ELSE 0 END) AS _has_running,
                   MAX(CASE WHEN step.status = ? THEN 1 ELSE 0 END) AS _has_queued
            FROM demo_turns AS turn
            LEFT JOIN demo_step_runs AS step
              ON step.turn_id = turn.turn_id
             AND step.step IN ({step_placeholders})
            WHERE turn.session_id = ? AND {completion_filter}
            GROUP BY turn.turn_id
            ORDER BY turn.created_at ASC, turn.rowid ASC
            """,
            (
                StepStatus.SUCCEEDED.value,
                StepStatus.SKIPPED.value,
                StepStatus.FAILED.value,
                StepStatus.RUNNING.value,
                StepStatus.QUEUED.value,
                *(step.value for step in PIPELINE_STEPS),
                session_id,
                *((cutoff,) if include_seconds > 0 else ()),
            ),
        )
        return [self._turn_summary_from_aggregate(turn) for turn in turns]

    def turn_summary(self, turn: Dict[str, Any] | str) -> Dict[str, Any]:
        if isinstance(turn, str):
            stored = self.get_turn(turn)
            if stored is None:
                raise KeyError(f"Unknown demo turn: {turn}")
            turn = stored
        steps = self.list_steps(turn["turn_id"])
        statuses = [step["status"] for step in steps]
        completed = sum(status in TERMINAL_STEP_STATUSES for status in statuses)
        if turn.get("completed_at"):
            status = StepStatus.SUCCEEDED.value
        elif StepStatus.FAILED.value in statuses:
            status = StepStatus.FAILED.value
        elif StepStatus.RUNNING.value in statuses:
            status = StepStatus.RUNNING.value
        elif StepStatus.QUEUED.value in statuses:
            status = StepStatus.QUEUED.value
        else:
            status = StepStatus.PENDING.value
        return {
            **turn,
            "status": status,
            "completed_steps": completed,
            "total_steps": len(steps),
            "progress": completed / len(steps) if steps else 0,
            "has_failure": StepStatus.FAILED.value in statuses,
            "exiting": bool(turn.get("completed_at")),
        }

    @staticmethod
    def _turn_summary_from_aggregate(turn: Dict[str, Any]) -> Dict[str, Any]:
        completed = int(turn.pop("_completed_steps") or 0)
        total = int(turn.pop("_total_steps") or 0)
        has_failure = bool(turn.pop("_has_failure"))
        has_running = bool(turn.pop("_has_running"))
        has_queued = bool(turn.pop("_has_queued"))
        if turn.get("completed_at"):
            status = StepStatus.SUCCEEDED.value
        elif has_failure:
            status = StepStatus.FAILED.value
        elif has_running:
            status = StepStatus.RUNNING.value
        elif has_queued:
            status = StepStatus.QUEUED.value
        else:
            status = StepStatus.PENDING.value
        return {
            **turn,
            "status": status,
            "completed_steps": completed,
            "total_steps": total,
            "progress": completed / total if total else 0,
            "has_failure": has_failure,
            "exiting": bool(turn.get("completed_at")),
        }

    def session_has_inflight(self, session_id: str) -> bool:
        placeholders = ", ".join("?" for _ in INFLIGHT_STEP_STATUSES)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM demo_step_runs AS step
                JOIN demo_turns AS turn ON turn.turn_id = step.turn_id
                WHERE turn.session_id = ? AND step.status IN ({placeholders})
                LIMIT 1
                """,
                (session_id, *sorted(INFLIGHT_STEP_STATUSES)),
            ).fetchone()
        return row is not None

    def recover_expired_step_leases(self) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._recover_expired_step_leases(connection)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def background_config(self, turn_id: str) -> BackgroundStepConfig:
        turn = self.get_turn(turn_id)
        if turn is None:
            raise KeyError(f"Unknown demo turn: {turn_id}")
        return BackgroundStepConfig.from_mapping(turn)

    def update_background_config(
        self,
        turn_id: str,
        config: BackgroundStepConfig | Dict[str, Any],
    ) -> Dict[str, Any]:
        config = BackgroundStepConfig.from_mapping(
            config.as_dict() if isinstance(config, BackgroundStepConfig) else config
        )
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                turn = connection.execute(
                    "SELECT completed_at FROM demo_turns WHERE turn_id = ?",
                    (turn_id,),
                ).fetchone()
                if turn is None:
                    raise KeyError(f"Unknown demo turn: {turn_id}")
                if turn["completed_at"] is not None:
                    raise RuntimeError("Background configuration is locked after turn completion")
                connection.execute(
                    """
                    UPDATE demo_turns
                    SET run_shortterm = ?, run_midterm = ?, run_longterm = ?,
                        run_profile = ?, updated_at = ?
                    WHERE turn_id = ?
                    """,
                    (
                        int(config.run_shortterm),
                        int(config.run_midterm),
                        int(config.run_longterm),
                        int(config.run_profile),
                        _now(),
                        turn_id,
                    ),
                )
                for step in OPTIONAL_PIPELINE_STEPS:
                    connection.execute(
                        """
                        UPDATE demo_step_runs
                        SET status = ?, is_held = ?, skip_reason = NULL, ended_at = NULL
                        WHERE turn_id = ? AND step = ?
                          AND (
                              status = ?
                              OR (status = ? AND skip_reason = 'Disabled by turn configuration')
                          )
                        """,
                        (
                            StepStatus.PENDING.value,
                            int(not config.enabled(step)),
                            turn_id,
                            step.value,
                            StepStatus.PENDING.value,
                            StepStatus.SKIPPED.value,
                        ),
                    )
                self._update_turn_completion_locked(connection, turn_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_turn(turn_id)

    def mark_background_submitted(self, turn_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_turns
                SET background_submitted_at = COALESCE(background_submitted_at, ?), updated_at = ?
                WHERE turn_id = ?
                """,
                (now, now, turn_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown demo turn: {turn_id}")
            connection.commit()
        return self.get_turn(turn_id)

    def set_execution_target(self, turn_id: str, target: str | None) -> Dict[str, Any]:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_turns
                SET execution_target = ?, updated_at = ?
                WHERE turn_id = ?
                """,
                (target, _now(), turn_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown demo turn: {turn_id}")
            connection.commit()
        return self.get_turn(turn_id)

    def raw_messages(self, session_id: str) -> list[Dict[str, Any]]:
        messages = []
        for turn in self.list_turns(session_id):
            messages.append(
                {
                    "turn_id": turn["turn_id"],
                    "role": "user",
                    "content": turn["user_message"],
                    "created_at": turn["created_at"],
                }
            )
            if turn.get("assistant_message") is not None:
                messages.append(
                    {
                        "turn_id": turn["turn_id"],
                        "role": "assistant",
                        "content": turn["assistant_message"],
                        "created_at": turn["updated_at"],
                    }
                )
        return messages

    def get_step(self, turn_id: str, step: PipelineStep | str) -> Optional[Dict[str, Any]]:
        return self._one(
            "SELECT * FROM demo_step_runs WHERE turn_id = ? AND step = ?",
            (turn_id, _step_value(step)),
        )

    def list_steps(self, turn_id: str) -> list[Dict[str, Any]]:
        placeholders = ", ".join("?" for _ in PIPELINE_STEPS)
        return self._all(
            f"""
            SELECT * FROM demo_step_runs
            WHERE turn_id = ? AND step IN ({placeholders})
            ORDER BY position ASC
            """,
            (turn_id, *(step.value for step in PIPELINE_STEPS)),
        )

    def hold_step(self, turn_id: str, step: PipelineStep | str) -> Dict[str, Any]:
        step = PipelineStep(step)
        if step not in MEMORY_STEPS:
            raise ValueError(f"Only pending memory steps can be held: {step.value}")
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM demo_step_runs WHERE turn_id = ? AND step = ?",
                    (turn_id, step.value),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown pipeline step: turn={turn_id} step={step.value}")
                if row["status"] != StepStatus.PENDING.value:
                    raise ValueError(f"Only pending memory steps can be held: turn={turn_id} step={step.value}")
                if not bool(row["is_held"]):
                    connection.execute(
                        """
                        UPDATE demo_step_runs
                        SET is_held = 1
                        WHERE turn_id = ? AND step = ? AND status = ? AND is_held = 0
                        """,
                        (turn_id, step.value, StepStatus.PENDING.value),
                    )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self.get_step(turn_id, step)

    def release_step(self, turn_id: str, step: PipelineStep | str) -> Dict[str, Any]:
        step = PipelineStep(step)
        if step not in MEMORY_STEPS:
            raise ValueError(f"Only held memory steps can be released: {step.value}")
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM demo_step_runs WHERE turn_id = ? AND step = ?",
                    (turn_id, step.value),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown pipeline step: turn={turn_id} step={step.value}")
                if row["status"] != StepStatus.PENDING.value or not bool(row["is_held"]):
                    raise ValueError(
                        f"Only held pending memory steps can be released: turn={turn_id} step={step.value}"
                    )
                connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET is_held = 0
                    WHERE turn_id = ? AND step = ? AND status = ? AND is_held = 1
                    """,
                    (turn_id, step.value, StepStatus.PENDING.value),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return self.get_step(turn_id, step)

    def queue_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        lease_seconds: int = 300,
        retry: bool = False,
    ) -> Optional[str]:
        step_value = _step_value(step)
        now = datetime.now(timezone.utc)
        token = str(uuid.uuid4())
        lease_expires_at = (now + timedelta(seconds=max(int(lease_seconds), 1))).isoformat()
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM demo_step_runs WHERE turn_id = ? AND step = ?",
                    (turn_id, step_value),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown pipeline step: turn={turn_id} step={step_value}")
                if row["status"] in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
                    connection.execute("COMMIT")
                    return None
                if bool(row["is_held"]):
                    raise StepHeldError(f"Pipeline step is held: turn={turn_id} step={step_value}")
                expected_status = StepStatus.FAILED.value if retry else StepStatus.PENDING.value
                if retry and row["status"] != StepStatus.FAILED.value:
                    raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step_value}")
                if row["status"] in INFLIGHT_STEP_STATUSES:
                    raise StepAlreadyRunningError(f"Pipeline step is already running: turn={turn_id} step={step_value}")
                preserve_output = retry or row["error_type"] == "BackgroundJobDeferred"
                cursor = connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET status = ?, input_json = NULL,
                        output_json = CASE WHEN ? THEN output_json ELSE NULL END,
                        error_type = NULL, error_message = NULL, queued_at = ?,
                        started_at = NULL, ended_at = NULL, duration_ms = NULL,
                        lock_token = ?, lease_expires_at = ?, before_snapshot_id = NULL,
                        after_snapshot_id = NULL, diff_json = NULL, skip_reason = NULL,
                        is_held = 0
                    WHERE turn_id = ? AND step = ? AND status = ? AND is_held = 0
                    """,
                    (
                        StepStatus.QUEUED.value,
                        int(preserve_output),
                        now.isoformat(),
                        token,
                        lease_expires_at,
                        turn_id,
                        step_value,
                        expected_status,
                    ),
                )
                if cursor.rowcount != 1:
                    connection.execute("ROLLBACK")
                    return None
                connection.execute("COMMIT")
                return token
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def start_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        *,
        lease_seconds: int = 300,
    ) -> bool:
        now = datetime.now(timezone.utc)
        lease_expires_at = (now + timedelta(seconds=max(int(lease_seconds), 1))).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_step_runs
                SET status = ?, started_at = ?, ended_at = NULL,
                    attempts = attempts + 1, lease_expires_at = ?
                WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                """,
                (
                    StepStatus.RUNNING.value,
                    now.isoformat(),
                    lease_expires_at,
                    turn_id,
                    _step_value(step),
                    StepStatus.QUEUED.value,
                    token,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def renew_step_lease(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        *,
        lease_seconds: int = 300,
    ) -> bool:
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(int(lease_seconds), 1))).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_step_runs
                SET lease_expires_at = ?
                WHERE turn_id = ? AND step = ? AND status IN (?, ?) AND lock_token = ?
                """,
                (
                    expires_at,
                    turn_id,
                    _step_value(step),
                    StepStatus.QUEUED.value,
                    StepStatus.RUNNING.value,
                    token,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def claim_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        lease_seconds: int = 300,
    ) -> Optional[str]:
        """Compatibility helper for non-coordinator callers."""
        token = self.queue_step(turn_id, step, lease_seconds=lease_seconds)
        if token is None:
            return None
        if not self.start_step(turn_id, step, token, lease_seconds=lease_seconds):
            return None
        return token

    def complete_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        *,
        status: StepStatus = StepStatus.SUCCEEDED,
        input_data: Any = None,
        output_data: Any = None,
        duration_ms: float,
        before_snapshot_id: Optional[str],
        after_snapshot_id: Optional[str],
        diff: Any,
        skip_reason: Optional[str] = None,
        assistant_message: Optional[str] = None,
        generation: Any = None,
        commit: Any = None,
    ) -> Dict[str, Any]:
        if status not in {StepStatus.SUCCEEDED, StepStatus.SKIPPED}:
            raise ValueError("complete_step status must be succeeded or skipped")
        step_value = _step_value(step)
        now = _now()
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET status = ?, input_json = ?, output_json = ?, ended_at = ?,
                        duration_ms = ?, queued_at = NULL, lock_token = NULL, lease_expires_at = NULL,
                        before_snapshot_id = ?, after_snapshot_id = ?, diff_json = ?,
                        skip_reason = ?
                    WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                    """,
                    (
                        status.value,
                        _json(input_data),
                        _json(output_data),
                        now,
                        max(float(duration_ms), 0.0),
                        before_snapshot_id,
                        after_snapshot_id,
                        _json(diff),
                        skip_reason,
                        turn_id,
                        step_value,
                        StepStatus.RUNNING.value,
                        token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StepAlreadyRunningError(f"Pipeline step lease was lost: turn={turn_id} step={step_value}")
                if assistant_message is not None or generation is not None:
                    connection.execute(
                        """
                        UPDATE demo_turns
                        SET assistant_message = COALESCE(?, assistant_message),
                            generation_json = COALESCE(?, generation_json), updated_at = ?
                        WHERE turn_id = ?
                        """,
                        (assistant_message, _json(generation), now, turn_id),
                    )
                if commit is not None:
                    connection.execute(
                        "UPDATE demo_turns SET commit_json = ?, updated_at = ? WHERE turn_id = ?",
                        (_json(commit), now, turn_id),
                    )
                self._update_turn_completion_locked(connection, turn_id, now=now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self.get_step(turn_id, step_value)

    def fail_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        *,
        input_data: Any,
        output_data: Any = None,
        error: BaseException,
        duration_ms: float,
        before_snapshot_id: Optional[str],
        after_snapshot_id: Optional[str],
        diff: Any,
    ) -> Dict[str, Any]:
        step_value = _step_value(step)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_step_runs
                SET status = ?, input_json = ?, output_json = ?, error_type = ?, error_message = ?,
                    ended_at = ?, duration_ms = ?, queued_at = NULL, lock_token = NULL,
                    lease_expires_at = NULL, before_snapshot_id = ?,
                    after_snapshot_id = ?, diff_json = ?
                WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                """,
                (
                    StepStatus.FAILED.value,
                    _json(input_data),
                    _json(output_data),
                    type(error).__name__,
                    str(error),
                    _now(),
                    max(float(duration_ms), 0.0),
                    before_snapshot_id,
                    after_snapshot_id,
                    _json(diff),
                    turn_id,
                    step_value,
                    StepStatus.RUNNING.value,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise StepAlreadyRunningError(
                    f"Pipeline step lease was lost while recording failure: turn={turn_id} step={step_value}"
                )
            connection.commit()
        return self.get_step(turn_id, step_value)

    def fail_queued_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        error: BaseException,
    ) -> Dict[str, Any]:
        step_value = _step_value(step)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_step_runs
                SET status = ?, error_type = ?, error_message = ?,
                    queued_at = NULL, ended_at = ?, lock_token = NULL,
                    lease_expires_at = NULL
                WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                """,
                (
                    StepStatus.FAILED.value,
                    type(error).__name__,
                    str(error),
                    _now(),
                    turn_id,
                    step_value,
                    StepStatus.QUEUED.value,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise StepAlreadyRunningError(f"Pipeline step queue lease was lost: turn={turn_id} step={step_value}")
            connection.commit()
        return self.get_step(turn_id, step_value)

    def defer_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        token: str,
        *,
        input_data: Any,
        output_data: Any = None,
        reason: str,
        duration_ms: float,
        before_snapshot_id: Optional[str],
        after_snapshot_id: Optional[str],
        diff: Any,
    ) -> Dict[str, Any]:
        step_value = _step_value(step)
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE demo_step_runs
                SET status = ?, input_json = ?, output_json = ?, error_type = ?, error_message = ?,
                    ended_at = ?, duration_ms = ?, queued_at = NULL, lock_token = NULL,
                    lease_expires_at = NULL, before_snapshot_id = ?,
                    after_snapshot_id = ?, diff_json = ?
                WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                """,
                (
                    StepStatus.PENDING.value,
                    _json(input_data),
                    _json(output_data),
                    "BackgroundJobDeferred",
                    reason,
                    _now(),
                    max(float(duration_ms), 0.0),
                    before_snapshot_id,
                    after_snapshot_id,
                    _json(diff),
                    turn_id,
                    step_value,
                    StepStatus.RUNNING.value,
                    token,
                ),
            )
            if cursor.rowcount != 1:
                raise StepAlreadyRunningError(
                    f"Pipeline step lease was lost while deferring work: turn={turn_id} step={step_value}"
                )
            connection.commit()
        return self.get_step(turn_id, step_value)

    def create_snapshot(
        self,
        turn_id: str,
        step: PipelineStep | str,
        phase: str,
        data: Any,
    ) -> str:
        if phase not in {"before", "after"}:
            raise ValueError("snapshot phase must be 'before' or 'after'")
        snapshot_id = str(uuid.uuid4())
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO demo_snapshots (snapshot_id, turn_id, step, phase, data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, turn_id, _step_value(step), phase, _json(data), _now()),
            )
            connection.commit()
        return snapshot_id

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        return self._one("SELECT * FROM demo_snapshots WHERE snapshot_id = ?", (snapshot_id,))

    def latest_snapshot(self, turn_id: str) -> Optional[Dict[str, Any]]:
        return self._one(
            """
            SELECT * FROM demo_snapshots
            WHERE turn_id = ?
            ORDER BY CASE phase WHEN 'after' THEN 0 ELSE 1 END,
                     created_at DESC, rowid DESC
            LIMIT 1
            """,
            (turn_id,),
        )

    def latest_session_snapshot(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Return the newest persisted database view across all session turns."""
        return self._one(
            """
            SELECT snapshot.*
            FROM demo_snapshots AS snapshot
            JOIN demo_turns AS turn ON turn.turn_id = snapshot.turn_id
            WHERE turn.session_id = ?
            ORDER BY snapshot.created_at DESC, snapshot.rowid DESC
            LIMIT 1
            """,
            (session_id,),
        )

    def reset_turn(self, turn_id: str) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                inflight = connection.execute(
                    """
                    SELECT 1 FROM demo_step_runs
                    WHERE turn_id = ? AND status IN (?, ?)
                    """,
                    (turn_id, StepStatus.QUEUED.value, StepStatus.RUNNING.value),
                ).fetchone()
                if inflight is not None:
                    raise RuntimeError("A queued or running turn cannot be reset")
                shortterm = connection.execute(
                    """
                    SELECT status, attempts FROM demo_step_runs
                    WHERE turn_id = ? AND step = ?
                    """,
                    (turn_id, PipelineStep.RUN_SHORTTERM.value),
                ).fetchone()
                if shortterm is not None and (
                    shortterm["status"] == StepStatus.SUCCEEDED.value or int(shortterm["attempts"] or 0) > 0
                ):
                    raise RuntimeError("A turn that may have called Memory.add() cannot be safely reset")
                connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET status = ?, input_json = NULL, output_json = NULL,
                        error_type = NULL, error_message = NULL, queued_at = NULL, started_at = NULL,
                        ended_at = NULL, duration_ms = NULL, attempts = 0,
                        lock_token = NULL, lease_expires_at = NULL,
                        before_snapshot_id = NULL, after_snapshot_id = NULL,
                        diff_json = NULL, skip_reason = NULL
                    WHERE turn_id = ?
                    """,
                    (StepStatus.PENDING.value, turn_id),
                )
                connection.execute("DELETE FROM demo_snapshots WHERE turn_id = ?", (turn_id,))
                connection.execute(
                    """
                    UPDATE demo_turns
                    SET assistant_message = NULL, generation_json = NULL,
                        commit_json = NULL, background_submitted_at = NULL,
                        execution_target = NULL, completed_at = NULL, updated_at = ?
                    WHERE turn_id = ?
                    """,
                    (_now(), turn_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _update_turn_completion_locked(
        connection: sqlite3.Connection,
        turn_id: str,
        *,
        now: str | None = None,
    ) -> bool:
        placeholders = ", ".join("?" for _ in MEMORY_STEPS)
        rows = connection.execute(
            f"""
            SELECT status FROM demo_step_runs
            WHERE turn_id = ? AND step IN ({placeholders})
            """,
            (turn_id, *(step.value for step in MEMORY_STEPS)),
        ).fetchall()
        generation = connection.execute(
            """
            SELECT status FROM demo_step_runs
            WHERE turn_id = ? AND step = ?
            """,
            (turn_id, PipelineStep.GENERATE_RESPONSE.value),
        ).fetchone()
        complete = (
            generation is not None
            and generation["status"] == StepStatus.SUCCEEDED.value
            and len(rows) == len(MEMORY_STEPS)
            and all(row["status"] in TERMINAL_STEP_STATUSES for row in rows)
        )
        if complete:
            timestamp = now or _now()
            connection.execute(
                """
                UPDATE demo_turns
                SET completed_at = COALESCE(completed_at, ?),
                    execution_target = NULL, updated_at = ?
                WHERE turn_id = ?
                """,
                (timestamp, timestamp, turn_id),
            )
        else:
            connection.execute(
                "UPDATE demo_turns SET completed_at = NULL WHERE turn_id = ?",
                (turn_id,),
            )
        return complete

    def _one(self, query: str, parameters: tuple[Any, ...]) -> Optional[Dict[str, Any]]:
        with self._connection() as connection:
            row = connection.execute(query, parameters).fetchone()
        return self._decode(dict(row)) if row is not None else None

    def _all(self, query: str, parameters: tuple[Any, ...]) -> list[Dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode(dict(row)) for row in rows]

    @staticmethod
    def _decode(row: Dict[str, Any]) -> Dict[str, Any]:
        for column, output_name in _JSON_COLUMNS.items():
            value = row.get(column)
            if value is None:
                continue
            try:
                row[output_name] = json.loads(value)
            except json.JSONDecodeError:
                row[output_name] = value
        for column in ("run_shortterm", "run_midterm", "run_longterm", "run_profile", "is_held"):
            if column in row:
                row[column] = bool(row[column])
        return row


def _step_value(step: PipelineStep | str) -> str:
    return step.value if isinstance(step, PipelineStep) else PipelineStep(step).value


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
