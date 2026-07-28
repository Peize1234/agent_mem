import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig
from mem0.memory import main as memory_main
from mem0.memory.main import Memory
from mem0.memory.storage import IdempotencyConflictError, SQLiteManager
from memory_monitor.runtime import DemoMemory


def _memory(db, *, demo=False, capacity=0, profile_enabled=True):
    memory_class = DemoMemory if demo else Memory
    memory = memory_class.__new__(memory_class)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=False, short_term_capacity=capacity),
        profile=SimpleNamespace(enabled=profile_enabled, update_on_add=profile_enabled),
        background=BackgroundTaskConfig(enabled=True),
        history_db_path=db.db_path,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._background_worker = MagicMock()
    memory._background_worker.stop.return_value = True
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._entity_store = None
    memory.vector_store = MagicMock()
    return memory


def _messages(user="question", assistant="answer"):
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]


def _counts(db):
    return {
        "messages": db.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
        "migration": db.connection.execute("SELECT COUNT(*) FROM memory_migration_jobs").fetchone()[0],
        "profile": db.connection.execute("SELECT COUNT(*) FROM profile_update_jobs").fetchone()[0],
        "operations": db.connection.execute("SELECT COUNT(*) FROM memory_idempotency_operations").fetchone()[0],
    }


@pytest.fixture(autouse=True)
def disable_add_notices(monkeypatch):
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)


def test_persisted_idempotency_reuses_messages_and_both_jobs(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        first = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )
        second = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )

        assert second == first
        assert _counts(db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
        operation = db.get_idempotency_operation("operation-1")
        assert operation["status"] == "succeeded"
        assert operation["result"] == first
    finally:
        db.close()


def test_demo_commit_is_idempotent_after_memory_reopens(tmp_path):
    db_path = tmp_path / "history.db"
    first_db = SQLiteManager(str(db_path))
    first_memory = _memory(first_db, demo=True)
    first = first_memory.commit_demo_turn(
        simulation_id="simulation-1",
        turn_id="turn-1",
        user_id="user-1",
        run_id="run-1",
        user_message="question",
        assistant_message="answer",
    )
    first_db.close()

    reopened_db = SQLiteManager(str(db_path))
    reopened_memory = _memory(reopened_db, demo=True)
    try:
        second = reopened_memory.commit_demo_turn(
            simulation_id="simulation-1",
            turn_id="turn-1",
            user_id="user-1",
            run_id="run-1",
            user_message="question",
            assistant_message="answer",
        )

        assert second == first
        assert _counts(reopened_db) == {
            "messages": 2,
            "migration": 1,
            "profile": 1,
            "operations": 1,
        }
    finally:
        reopened_db.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_message", "different question"),
        ("assistant_message", "different answer"),
        ("user_id", "user-2"),
        ("run_id", "run-2"),
    ],
)
def test_idempotency_key_conflict_rejects_changed_business_input(tmp_path, field, value):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db, demo=True)
    request = {
        "simulation_id": "simulation-1",
        "turn_id": "turn-1",
        "user_id": "user-1",
        "run_id": "run-1",
        "user_message": "question",
        "assistant_message": "answer",
    }
    try:
        memory.commit_demo_turn(**request)
        request[field] = value

        with pytest.raises(IdempotencyConflictError, match="conflicts with a different request"):
            memory.commit_demo_turn(**request)

        assert _counts(db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
    finally:
        db.close()


def test_idempotency_key_conflict_includes_business_metadata(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            metadata={"source": "first"},
            idempotency_key="operation-1",
        )

        with pytest.raises(IdempotencyConflictError, match="conflicts with a different request"):
            memory.add(
                _messages(),
                user_id="user-1",
                run_id="run-1",
                metadata={"source": "second"},
                idempotency_key="operation-1",
            )

        assert _counts(db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
    finally:
        db.close()


def test_failed_idempotent_add_can_retry_without_partial_side_effects(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    original = db._reserve_migration_job_in_transaction
    monkeypatch.setattr(
        db,
        "_reserve_migration_job_in_transaction",
        MagicMock(side_effect=RuntimeError("queue write failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="queue write failed"):
            memory.add(
                _messages(),
                user_id="user-1",
                run_id="run-1",
                idempotency_key="operation-1",
            )

        assert _counts(db) == {"messages": 0, "migration": 0, "profile": 0, "operations": 1}
        assert db.get_idempotency_operation("operation-1")["status"] == "failed"

        monkeypatch.setattr(db, "_reserve_migration_job_in_transaction", original)
        memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )

        assert _counts(db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
        assert db.get_idempotency_operation("operation-1")["status"] == "succeeded"
    finally:
        db.close()


def test_processing_record_with_durable_side_effects_is_recovered(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        first = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )
        db.connection.execute(
            """
            UPDATE memory_idempotency_operations
            SET status = 'processing', result_json = NULL
            WHERE idempotency_key = 'operation-1'
            """
        )
        db.connection.commit()

        recovered = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )

        assert recovered == first
        assert _counts(db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
        assert db.get_idempotency_operation("operation-1")["status"] == "succeeded"
    finally:
        db.close()


@pytest.mark.parametrize(
    ("capacity", "profile_enabled", "migration_count", "profile_count"),
    [
        (0, False, 1, 0),
        (10, True, 0, 1),
    ],
)
def test_idempotent_add_handles_optional_background_jobs(
    tmp_path,
    capacity,
    profile_enabled,
    migration_count,
    profile_count,
):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db, capacity=capacity, profile_enabled=profile_enabled)
    try:
        first = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )
        second = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )

        assert second == first
        assert _counts(db) == {
            "messages": 2,
            "migration": migration_count,
            "profile": profile_count,
            "operations": 1,
        }
    finally:
        db.close()


def test_identical_text_in_different_operations_is_not_deduplicated(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-1",
        )
        memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="operation-2",
        )

        assert _counts(db) == {"messages": 4, "migration": 2, "profile": 2, "operations": 2}
    finally:
        db.close()


def test_concurrent_idempotent_adds_have_one_database_winner(tmp_path):
    db_path = tmp_path / "history.db"
    first_db = SQLiteManager(str(db_path))
    second_db = SQLiteManager(str(db_path))
    first_memory = _memory(first_db)
    second_memory = _memory(second_db)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def submit(memory):
        try:
            barrier.wait()
            results.append(
                memory.add(
                    _messages(),
                    user_id="user-1",
                    run_id="run-1",
                    idempotency_key="operation-1",
                )
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=submit, args=(first_memory,)),
        threading.Thread(target=submit, args=(second_memory,)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in threads)
        assert not errors
        assert len(results) == 2
        assert results[0] == results[1]
        assert _counts(first_db) == {"messages": 2, "migration": 1, "profile": 1, "operations": 1}
    finally:
        first_db.close()
        second_db.close()


def test_add_without_idempotency_key_preserves_existing_behavior(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        first = memory.add(_messages(), user_id="user-1", run_id="run-1")
        second = memory.add(_messages(), user_id="user-1", run_id="run-1")

        assert first["background"]["migration_job_id"] != second["background"]["migration_job_id"]
        assert first["background"]["profile_job_id"] != second["background"]["profile_job_id"]
        assert _counts(db) == {"messages": 4, "migration": 2, "profile": 2, "operations": 0}
    finally:
        db.close()


def test_reset_removes_idempotency_table(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    try:
        memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="request-1",
        )
        assert db.get_idempotency_operation("request-1")["status"] == "succeeded"

        db.reset()

        table = db.connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'memory_idempotency_operations'
            """
        ).fetchone()
        assert table is None

        # DROP TABLE IF EXISTS keeps direct storage reset repeatable.
        db.reset()
    finally:
        db.close()


def test_memory_reset_treats_old_idempotency_key_as_a_new_operation(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    reset_worker = MagicMock()
    reset_worker.stop.return_value = True
    memory._create_background_worker_manager = MagicMock(return_value=reset_worker)
    monkeypatch.setattr(memory_main.VectorStoreFactory, "reset", lambda store: store)
    monkeypatch.setattr(memory_main, "capture_event", lambda *args, **kwargs: None)
    try:
        first = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="request-1",
        )
        first_migration_job_id = first["background"]["migration_job_id"]
        first_profile_job_id = first["background"]["profile_job_id"]
        assert first_migration_job_id
        assert first_profile_job_id

        memory.reset()

        assert memory.db.get_idempotency_operation("request-1") is None
        assert memory.db.get_background_job(first_migration_job_id) is None
        assert memory.db.get_background_job(first_profile_job_id, "profile") is None

        second = memory.add(
            _messages(),
            user_id="user-1",
            run_id="run-1",
            idempotency_key="request-1",
        )

        assert second["background"]["migration_job_id"] != first_migration_job_id
        assert second["background"]["profile_job_id"] != first_profile_job_id
        assert _counts(memory.db) == {
            "messages": 2,
            "migration": 1,
            "profile": 1,
            "operations": 1,
        }
    finally:
        memory.close()


def test_memory_add_without_idempotency_key_still_works_after_reset(tmp_path, monkeypatch):
    db = SQLiteManager(str(tmp_path / "history.db"))
    memory = _memory(db)
    reset_worker = MagicMock()
    reset_worker.stop.return_value = True
    memory._create_background_worker_manager = MagicMock(return_value=reset_worker)
    monkeypatch.setattr(memory_main.VectorStoreFactory, "reset", lambda store: store)
    monkeypatch.setattr(memory_main, "capture_event", lambda *args, **kwargs: None)
    try:
        memory.reset()
        result = memory.add(_messages(), user_id="user-1", run_id="run-1")

        assert result["background"]["migration_job_id"]
        assert result["background"]["profile_job_id"]
        assert _counts(memory.db) == {
            "messages": 2,
            "migration": 1,
            "profile": 1,
            "operations": 0,
        }
    finally:
        memory.close()


def test_new_database_uses_independent_migration_stage_schema(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    try:
        migration_columns = {
            row[1] for row in db.connection.execute("PRAGMA table_info(memory_migration_jobs)").fetchall()
        }
        assert {
            "midterm_status",
            "midterm_attempts",
            "midterm_next_retry_at",
            "midterm_last_error",
            "midterm_degraded",
            "midterm_started_at",
            "midterm_finished_at",
            "longterm_status",
            "longterm_attempts",
            "longterm_next_retry_at",
            "longterm_last_error",
            "longterm_degraded",
            "longterm_started_at",
            "longterm_finished_at",
            "finalized_at",
        } <= migration_columns
        assert {"midterm_done", "longterm_done", "attempts", "next_retry_at", "degraded"}.isdisjoint(
            migration_columns
        )
    finally:
        db.close()
