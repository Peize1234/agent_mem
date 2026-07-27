from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from memory_monitor.models.demo_pipeline import PIPELINE_STEPS, PipelineStep, StepStatus

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
                    assistant_message TEXT,
                    generation_json TEXT,
                    commit_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS demo_step_runs (
                    turn_id TEXT NOT NULL REFERENCES demo_turns(turn_id) ON DELETE CASCADE,
                    step TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_json TEXT,
                    output_json TEXT,
                    error_type TEXT,
                    error_message TEXT,
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
            connection.commit()

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

    def create_turn(
        self,
        session_id: str,
        *,
        user_id: str,
        run_id: str,
        user_message: str,
        turn_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not user_message or not user_message.strip():
            raise ValueError("user_message is required")
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
                        turn_id, session_id, user_id, run_id, user_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (turn_id, session_id, user_id, run_id, user_message.strip(), now, now),
                )
                connection.executemany(
                    """
                    INSERT INTO demo_step_runs (turn_id, step, position, status)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (turn_id, step.value, position, StepStatus.PENDING.value)
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
        return self._all(
            "SELECT * FROM demo_step_runs WHERE turn_id = ? ORDER BY position ASC",
            (turn_id,),
        )

    def claim_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        lease_seconds: int = 300,
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
                if (
                    row["status"] == StepStatus.RUNNING.value
                    and row["lease_expires_at"]
                    and row["lease_expires_at"] > now.isoformat()
                ):
                    raise StepAlreadyRunningError(f"Pipeline step is already running: turn={turn_id} step={step_value}")
                connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET status = ?, input_json = NULL, output_json = NULL,
                        error_type = NULL, error_message = NULL, started_at = ?,
                        ended_at = NULL, duration_ms = NULL, attempts = attempts + 1,
                        lock_token = ?, lease_expires_at = ?, before_snapshot_id = NULL,
                        after_snapshot_id = NULL, diff_json = NULL, skip_reason = NULL
                    WHERE turn_id = ? AND step = ?
                    """,
                    (
                        StepStatus.RUNNING.value,
                        now.isoformat(),
                        token,
                        lease_expires_at,
                        turn_id,
                        step_value,
                    ),
                )
                connection.execute("COMMIT")
                return token
            except Exception:
                connection.execute("ROLLBACK")
                raise

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
                        duration_ms = ?, lock_token = NULL, lease_expires_at = NULL,
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
                SET status = ?, input_json = ?, error_type = ?, error_message = ?,
                    ended_at = ?, duration_ms = ?, lock_token = NULL,
                    lease_expires_at = NULL, before_snapshot_id = ?,
                    after_snapshot_id = ?, diff_json = ?
                WHERE turn_id = ? AND step = ? AND status = ? AND lock_token = ?
                """,
                (
                    StepStatus.FAILED.value,
                    _json(input_data),
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

    def reset_turn(self, turn_id: str) -> None:
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                committed = connection.execute(
                    """
                    SELECT 1 FROM demo_step_runs
                    WHERE turn_id = ? AND step = ? AND status = ?
                    """,
                    (turn_id, PipelineStep.COMMIT_TURN.value, StepStatus.SUCCEEDED.value),
                ).fetchone()
                if committed is not None:
                    raise RuntimeError("A committed turn cannot be reset without duplicating core memory")
                connection.execute(
                    """
                    UPDATE demo_step_runs
                    SET status = ?, input_json = NULL, output_json = NULL,
                        error_type = NULL, error_message = NULL, started_at = NULL,
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
                        commit_json = NULL, updated_at = ?
                    WHERE turn_id = ?
                    """,
                    (_now(), turn_id),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

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
        return row


def _step_value(step: PipelineStep | str) -> str:
    return step.value if isinstance(step, PipelineStep) else PipelineStep(step).value


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
