import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig, MemoryConfig, ObservabilityConfig
from mem0.memory import main as memory_main
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.observability import (
    NoOpObservationSink,
    ObservationContext,
    ObservationEvent,
    emit_safely,
    observation_stage,
)
from mem0.memory.storage import SQLiteManager


class _Collector:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _FailingSink:
    def emit(self, event):
        raise RuntimeError("observation database unavailable")


class _Unserializable:
    def __str__(self):
        raise RuntimeError("str failed")

    def __repr__(self):
        raise RuntimeError("repr failed")


def _partial_memory(db, *, observability=True):
    memory = Memory.__new__(Memory)
    config = MemoryConfig()
    config.history_db_path = db.db_path
    config.background.execution_mode = "manual"
    config.midterm.short_term_capacity = 0
    config.profile.enabled = True
    config.observability.enabled = observability
    memory.config = config
    memory.db = db
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = None
    memory._profile_updater = None
    memory._initialize_background_workers()
    return memory


def test_observation_stage_records_success_failure_and_duration():
    sink = _Collector()
    context = ObservationContext(trace_id="trace-1")

    with observation_stage(sink, context, "test"):
        time.sleep(0.001)

    with pytest.raises(ValueError, match="broken"):
        with observation_stage(sink, context, "failed"):
            raise ValueError("broken")

    assert [event.status for event in sink.events] == ["started", "succeeded", "started", "failed"]
    assert sink.events[1].duration_ms > 0
    assert sink.events[-1].error_type == "ValueError"
    assert sink.events[-1].error_message == "broken"


def test_observation_payloads_are_json_safe_and_bounded():
    event = ObservationEvent.create(
        trace_id="trace-1",
        stage="test",
        event_type="test.created",
        status="succeeded",
        max_payload_length=100,
        input_data={"object": object(), "text": "x" * 200},
    )

    assert event.input_json is not None
    assert "_truncated" in event.input_json
    assert len(event.input_json) <= 100


def test_observation_payload_serialization_failure_is_isolated():
    event = ObservationEvent.create(
        trace_id="trace-1",
        stage="test",
        event_type="test.created",
        status="succeeded",
        input_data=_Unserializable(),
    )

    assert event.input_json == '"<unserializable>"'


def test_sink_failure_is_isolated():
    event = ObservationEvent.create(
        trace_id="trace-1",
        stage="test",
        event_type="test.created",
        status="succeeded",
    )
    emit_safely(_FailingSink(), event)


def test_noop_sink_skips_event_construction():
    event_factory = MagicMock()

    emit_safely(NoOpObservationSink(), event_factory)

    event_factory.assert_not_called()


def test_sink_failure_does_not_break_memory_add(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _partial_memory(db)
    memory._observation_sink = _FailingSink()
    memory._background_worker.observation_sink = memory._observation_sink
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)
    try:
        result = memory.add("hello", user_id="user-1")

        assert result["observability"]["trace_id"]
        assert result["background"]["migration_job_id"]
        assert result["background"]["profile_job_id"]
    finally:
        memory.close()


def test_add_trace_propagates_to_messages_jobs_and_events(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _partial_memory(db)
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)
    try:
        result = memory.add(
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            user_id="user-1",
            run_id="run-1",
            metadata={"trace_id": "user-owned-trace"},
        )

        trace_id = result["observability"]["trace_id"]
        migration_job_id = result["background"]["migration_job_id"]
        profile_job_id = result["background"]["profile_job_id"]
        migration = db.get_background_job(migration_job_id)
        profile = db.get_background_job(profile_job_id, "profile")
        messages = db.get_migration_job_messages(migration_job_id)
        events = db.connection.execute(
            "SELECT event_type FROM memory_observation_events WHERE trace_id = ?",
            (trace_id,),
        ).fetchall()

        assert migration["trace_id"] == trace_id
        assert migration["metadata"]["trace_id"] == "user-owned-trace"
        assert migration["user_id"] == "user-1"
        assert migration["run_id"] == "run-1"
        assert profile["trace_id"] == trace_id
        assert {message["trace_id"] for message in messages} == {trace_id}
        stored_messages = db.connection.execute(
            "SELECT user_id, run_id FROM messages WHERE migration_job_id = ?",
            (migration_job_id,),
        ).fetchall()
        assert {(row[0], row[1]) for row in stored_messages} == {("user-1", "run-1")}
        assert {"add.received", "messages.saved", "migration.job_created", "profile.job_created", "add.returned"} <= {
            row[0] for row in events
        }
    finally:
        memory.close()


def test_disabled_observability_writes_no_events(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _partial_memory(db, observability=False)
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)
    try:
        metadata = {"trace_id": "user-owned-trace"}
        result = memory.add("hello", user_id="user-1", metadata=metadata)
        migration_job_id = result["background"]["migration_job_id"]
        profile_job_id = result["background"]["profile_job_id"]
        migration = db.get_background_job(migration_job_id)
        profile = db.get_background_job(profile_job_id, "profile")
        messages = db.get_migration_job_messages(migration_job_id)
        count = db.connection.execute("SELECT COUNT(*) FROM memory_observation_events").fetchone()[0]

        assert isinstance(memory._observation_sink, NoOpObservationSink)
        assert memory._create_trace_id() is None
        assert result == {
            "results": [],
            "background": {
                "migration_job_id": migration_job_id,
                "profile_job_id": profile_job_id,
            },
        }
        assert metadata == {"trace_id": "user-owned-trace"}
        assert "trace_id" not in migration
        assert migration["metadata"]["trace_id"] == "user-owned-trace"
        assert "trace_id" not in profile
        assert all("trace_id" not in message for message in messages)
        assert count == 0
    finally:
        memory.close()


def test_manual_mode_does_not_start_threads_and_public_methods_reuse_queue_order():
    db = SQLiteManager(":memory:")
    seen = []
    manager = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(execution_mode="manual"),
        process_midterm=lambda job, messages, degraded: seen.append(job["job_id"]),
        process_longterm=lambda *args: None,
        process_profile=lambda *args: None,
    )
    first = db.save_messages_and_create_migration_job(
        [{"role": "user", "content": "first"}],
        "scope",
        max_messages=0,
        filters={"user_id": "user-1"},
        metadata={},
        infer=True,
        prompt=None,
    )
    second = db.save_messages_and_create_migration_job(
        [{"role": "user", "content": "second"}],
        "scope",
        max_messages=0,
        filters={"user_id": "user-1"},
        metadata={},
        infer=True,
        prompt=None,
    )
    manager.start()

    assert manager.threads_alive() is False
    assert manager.process_migration_job(second) is False
    assert manager.process_next_migration_job() is True
    assert manager.process_migration_job(second) is True
    assert seen == [first, second]
    db.close()


def test_manual_profile_processing_preserves_per_user_order():
    db = SQLiteManager(":memory:")
    seen = []
    manager = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(execution_mode="manual"),
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda job: seen.append(job["job_id"]),
    )
    first = db.create_profile_update_job("user-1", [{"role": "user", "content": "first"}])
    second = db.create_profile_update_job("user-1", [{"role": "user", "content": "second"}])

    assert manager.process_profile_job(second) is False
    assert manager.process_next_profile_job() is True
    assert manager.process_profile_job(second) is True
    assert seen == [first, second]
    db.close()


def test_background_business_result_survives_observation_write_failure():
    db = SQLiteManager(":memory:")
    manager = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(execution_mode="manual"),
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda *args: None,
        observation_sink=_FailingSink(),
        observability_config=ObservabilityConfig(enabled=True),
    )
    job_id = db.save_messages_and_create_migration_job(
        [{"role": "user", "content": "hello"}],
        "user_id=user-1",
        max_messages=0,
        filters={"user_id": "user-1"},
        metadata={},
        infer=False,
        prompt=None,
        trace_id="trace-1",
    )
    try:
        assert manager.process_next_migration_job() is True
        assert db.get_background_job(job_id)["status"] == "succeeded"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("method_name", "args"),
    [
        ("process_next_migration_job", ()),
        ("process_next_profile_job", ()),
        ("process_migration_job", ("job-1",)),
        ("process_profile_job", ("job-1",)),
    ],
)
@pytest.mark.parametrize(
    "background",
    [
        BackgroundTaskConfig(enabled=True, execution_mode="auto"),
        BackgroundTaskConfig(enabled=False, execution_mode="manual"),
    ],
)
def test_public_manual_processing_requires_enabled_manual_mode(method_name, args, background):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(background=background)
    memory._background_worker = MagicMock()

    with pytest.raises(
        RuntimeError,
        match="Manual job processing requires background.enabled=True and background.execution_mode='manual'",
    ):
        getattr(memory, method_name)(*args)


@pytest.mark.asyncio
async def test_async_public_manual_processing_requires_enabled_manual_mode():
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(background=BackgroundTaskConfig(enabled=True, execution_mode="auto"))
    memory._background_worker = MagicMock()

    with pytest.raises(RuntimeError, match="Manual job processing requires"):
        await memory.process_next_migration_job()


@pytest.mark.asyncio
async def test_async_public_manual_processing_delegates_in_manual_mode(monkeypatch):
    async def run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(memory_main.asyncio, "to_thread", run_inline)
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(
        background=BackgroundTaskConfig(enabled=True, execution_mode="manual"),
    )
    memory._background_worker = MagicMock()
    memory._background_worker.process_profile_job.return_value = True

    assert await memory.process_profile_job("job-1") is True
    memory._background_worker.process_profile_job.assert_called_once_with("job-1")


def test_public_manual_processing_delegates_in_manual_mode():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        background=BackgroundTaskConfig(enabled=True, execution_mode="manual"),
    )
    memory._background_worker = MagicMock()
    memory._background_worker.process_migration_job.return_value = True

    assert memory.process_migration_job("job-1") is True
    memory._background_worker.process_migration_job.assert_called_once_with("job-1")


def test_legacy_background_config_without_execution_mode_defaults_to_auto():
    db = SQLiteManager(":memory:")
    config = SimpleNamespace(
        enabled=True,
        max_retries=3,
        poll_interval_seconds=0.01,
        stale_running_timeout_seconds=300,
    )
    manager = BackgroundWorkerManager(
        db,
        config,
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda *args: None,
    )
    try:
        assert manager.automatic is True
        manager.start()
        assert manager.threads_alive() is True
    finally:
        manager.stop(timeout=1)
        db.close()


def test_old_database_gets_trace_columns_and_observation_table(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY, session_scope TEXT, role TEXT, content TEXT,
            name TEXT, created_at DATETIME
        )
        """
    )
    connection.execute(
        """
        INSERT INTO messages (id, session_scope, role, content, name, created_at)
        VALUES ('legacy-message', 'run_id=legacy-run&user_id=legacy-user', 'user', 'hello', NULL, CURRENT_TIMESTAMP)
        """
    )
    connection.commit()
    connection.close()

    db = SQLiteManager(str(db_path))
    try:
        message_columns = {row[1] for row in db.connection.execute("PRAGMA table_info(messages)")}
        event_table = db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'memory_observation_events'"
        ).fetchone()
        migrated_scope = db.connection.execute(
            "SELECT user_id, run_id FROM messages WHERE id = 'legacy-message'"
        ).fetchone()
        assert {"trace_id", "user_id", "run_id", "status", "migration_job_id"} <= message_columns
        assert event_table is not None
        assert tuple(migrated_scope) == ("legacy-user", "legacy-run")
    finally:
        db.close()
