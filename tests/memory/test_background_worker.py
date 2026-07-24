import asyncio
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig, MemoryConfig, UserProfileConfig
from mem0.memory import main as memory_main
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.storage import SQLiteManager
from mem0.utils.timestamps import beijing_now


def _messages(value):
    return [
        {"role": "user", "content": f"user-{value}"},
        {"role": "assistant", "content": f"assistant-{value}"},
    ]


def _reserve(db, value, *, scope="user_id=u1&run_id=r1", max_messages=0):
    job_id = db.save_messages_and_create_migration_job(
        _messages(value),
        scope,
        max_messages=max_messages,
        filters={"user_id": "u1", "run_id": scope},
        metadata={"user_id": "u1", "run_id": scope},
        infer=True,
        prompt=None,
    )
    assert job_id
    return job_id


def _manager(db, *, config=None, midterm=None, longterm=None, profile=None):
    config = config or BackgroundTaskConfig(poll_interval_seconds=0.01)
    return BackgroundWorkerManager(
        db,
        config,
        process_midterm=midterm or (lambda *args: None),
        process_longterm=longterm or (lambda *args: None),
        process_profile=profile or (lambda *args: None),
    )


@pytest.fixture
def db():
    manager = SQLiteManager(":memory:")
    yield manager
    if manager.connection:
        manager.close()


def test_background_config_defaults():
    config = MemoryConfig().background

    assert config.enabled is True
    assert config.max_retries == 3
    assert config.poll_interval_seconds == 1.0
    assert config.stale_running_timeout_seconds == 300
    assert config.shutdown_timeout_seconds == 30.0
    assert config.include_pending_in_context is True
    assert config.max_pending_context_messages == 20
    assert config.include_failed_in_context is False


def test_reservation_keeps_source_messages_and_only_counts_active_rows(db):
    first_job = _reserve(db, 1)
    second_job = _reserve(db, 2)

    assert first_job != second_job
    assert [item["content"] for item in db.get_migration_job_messages(first_job)] == [
        "user-1",
        "assistant-1",
    ]
    assert db.get_messages("user_id=u1&run_id=r1") == []
    context = db.get_context_messages(
        "user_id=u1&run_id=r1",
        active_limit=10,
        include_pending=True,
        max_pending=2,
    )
    assert [item["content"] for item in context] == ["user-2", "assistant-2"]
    assert db.get_background_job(first_job)["status"] == "pending"
    assert db.get_background_job(second_job)["sequence_no"] == 2


def test_migration_runs_midterm_then_longterm_and_deletes_only_after_both(db):
    job_id = _reserve(db, 1)
    longterm_started = threading.Event()
    release_longterm = threading.Event()
    events = []

    def midterm(job, messages, degraded):
        events.append(("midterm", job["job_id"], degraded))

    def longterm(job, messages, degraded):
        events.append(("longterm", job["job_id"], degraded))
        longterm_started.set()
        assert db.get_migration_job_messages(job_id)
        release_longterm.wait(2)

    manager = _manager(db, midterm=midterm, longterm=longterm)
    manager.start()
    manager.wake_migration()
    assert longterm_started.wait(1)

    running = db.get_background_job(job_id)
    assert running["midterm_done"] is True
    assert running["longterm_done"] is False
    assert db.get_migration_job_messages(job_id)

    release_longterm.set()
    assert manager.flush(2)
    assert events == [("midterm", job_id, False), ("longterm", job_id, False)]
    assert db.get_background_job(job_id)["status"] == "succeeded"
    assert db.get_migration_job_messages(job_id) == []
    assert manager.stop(timeout=1)


def test_longterm_retry_keeps_messages_and_does_not_repeat_completed_midterm(db, monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    job_id = _reserve(db, 1)
    midterm_calls = []
    longterm_calls = []

    def midterm(job, messages, degraded):
        midterm_calls.append(job["job_id"])

    def longterm(job, messages, degraded):
        longterm_calls.append(degraded)
        if len(longterm_calls) == 1:
            raise RuntimeError("temporary longterm failure")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=1, poll_interval_seconds=0.01),
        midterm=midterm,
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    deadline = time.monotonic() + 1
    while db.get_background_job(job_id)["status"] != "retry" and time.monotonic() < deadline:
        time.sleep(0.005)
    assert db.get_background_job(job_id)["status"] == "retry"
    assert db.get_migration_job_messages(job_id)

    assert manager.flush(2)
    assert midterm_calls == [job_id]
    assert longterm_calls == [False, False]
    assert db.get_background_job(job_id)["status"] == "succeeded"
    assert manager.stop(timeout=1)


def test_exhausted_longterm_uses_degraded_storage(db):
    job_id = _reserve(db, 1)
    calls = []

    def longterm(job, messages, degraded):
        calls.append(degraded)
        if not degraded:
            raise RuntimeError("LLM unavailable")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert calls == [False, True]
    job = db.get_background_job(job_id)
    assert job["status"] == "succeeded_degraded"
    assert job["degraded"] is True
    assert db.get_migration_job_messages(job_id) == []
    assert manager.stop(timeout=1)


def test_failed_degradation_marks_dead_and_does_not_block_next_job(db):
    first_job = _reserve(db, 1)
    second_job = _reserve(db, 2)

    def longterm(job, messages, degraded):
        if job["job_id"] == first_job:
            raise RuntimeError("all storage paths unavailable")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert db.get_background_job(first_job)["status"] == "dead"
    assert {item["status"] for item in db.get_migration_job_messages(first_job)} == {"failed"}
    assert db.get_background_job(second_job)["status"] == "succeeded"
    assert manager.stop(timeout=1)


def test_same_session_migration_jobs_are_processed_in_sequence(db):
    job_ids = [_reserve(db, index) for index in range(3)]
    seen = []

    def midterm(job, messages, degraded):
        seen.append(job["job_id"])

    manager = _manager(db, midterm=midterm)
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert seen == job_ids
    assert manager.stop(timeout=1)


def test_same_user_profile_jobs_are_processed_in_sequence(db):
    job_ids = [db.create_profile_update_job("u1", _messages(index)) for index in range(3)]
    seen = []

    def profile(job):
        seen.append(job["job_id"])

    manager = _manager(db, profile=profile)
    manager.start()
    manager.wake_profile()

    assert manager.flush(2)
    assert seen == job_ids
    assert all(db.get_background_job(job_id, "profile")["status"] == "succeeded" for job_id in job_ids)
    assert manager.stop(timeout=1)


def test_retry_callback_can_make_vector_write_idempotent(db, monkeypatch):
    monkeypatch.setattr("mem0.memory.background_worker._RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    job_id = _reserve(db, 1)
    persisted_job_ids = set()
    physical_writes = []
    attempts = 0

    def longterm(job, messages, degraded):
        nonlocal attempts
        attempts += 1
        if job["job_id"] not in persisted_job_ids:
            persisted_job_ids.add(job["job_id"])
            physical_writes.append(job["job_id"])
        if attempts == 1:
            raise RuntimeError("crash after vector write")

    manager = _manager(
        db,
        config=BackgroundTaskConfig(max_retries=1, poll_interval_seconds=0.005),
        longterm=longterm,
    )
    manager.start()
    manager.wake_migration()

    assert manager.flush(2)
    assert attempts == 2
    assert physical_writes == [job_id]
    assert manager.stop(timeout=1)


def test_longterm_degraded_storage_uses_stable_id_and_source_metadata(db):
    memory = _partial_memory(db, profile_enabled=False)
    memory.vector_store = MagicMock()
    memory.vector_store.get.side_effect = [None, SimpleNamespace(id="existing")]
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory._create_memory = MagicMock(return_value="created")
    job = {
        "job_id": "migration-job-1",
        "metadata": {"user_id": "u1"},
    }

    memory._store_longterm_fallback(job, _messages(1))
    memory._store_longterm_fallback(job, _messages(1))

    expected_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "mem0:longterm-fallback:migration-job-1"))
    memory._create_memory.assert_called_once()
    args = memory._create_memory.call_args.args
    assert args[2] == {
        "user_id": "u1",
        "source_job_id": "migration-job-1",
        "memory_type": "raw_fallback",
        "needs_reprocessing": True,
        "degraded": True,
    }
    assert memory._create_memory.call_args.kwargs["memory_id"] == expected_id


def test_startup_recovers_pending_retry_and_stale_running_jobs(tmp_path):
    path = str(tmp_path / "recovery.db")
    original = SQLiteManager(path)
    pending_job = _reserve(original, "pending", scope="user_id=u1&run_id=pending")
    retry_job = _reserve(original, "retry", scope="user_id=u1&run_id=retry")
    running_job = _reserve(original, "running", scope="user_id=u1&run_id=running")

    stale_time = (beijing_now() - timedelta(seconds=10)).isoformat()
    original.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET status = 'retry', next_retry_at = NULL
        WHERE job_id = ?
        """,
        (retry_job,),
    )
    original.connection.execute(
        """
        UPDATE memory_migration_jobs
        SET status = 'running', updated_at = ?
        WHERE job_id = ?
        """,
        (stale_time, running_job),
    )
    original.connection.execute(
        "UPDATE messages SET status = 'processing' WHERE migration_job_id = ?",
        (running_job,),
    )
    original.connection.commit()
    original.close()

    reopened = SQLiteManager(path)
    seen = []
    manager = _manager(
        reopened,
        config=BackgroundTaskConfig(
            poll_interval_seconds=0.01,
            stale_running_timeout_seconds=1,
        ),
        midterm=lambda job, messages, degraded: seen.append(job["job_id"]),
    )
    try:
        manager.start()
        manager.wake_migration()
        assert manager.flush(2)
        assert set(seen) == {pending_job, retry_job, running_job}
        assert all(
            reopened.get_background_job(job_id)["status"] == "succeeded"
            for job_id in (pending_job, retry_job, running_job)
        )
    finally:
        manager.stop(timeout=1)
        reopened.close()


def test_flush_returns_false_on_timeout_then_true_after_release(db):
    _reserve(db, 1)
    started = threading.Event()
    release = threading.Event()

    def midterm(job, messages, degraded):
        started.set()
        release.wait(2)

    manager = _manager(db, midterm=midterm)
    manager.start()
    manager.wake_migration()
    assert started.wait(1)
    assert manager.flush(0.02) is False
    release.set()
    assert manager.flush(2) is True
    assert manager.stop(timeout=1)


def _partial_memory(db, *, capacity=0, profile_enabled=True):
    memory = Memory.__new__(Memory)
    profile_config = UserProfileConfig(enabled=profile_enabled)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=True, short_term_capacity=capacity),
        profile=profile_config,
        background=BackgroundTaskConfig(
            max_retries=0,
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=2,
        ),
        history_db_path=db.db_path,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = None
    memory._entity_store = None
    return memory


def _partial_async_memory(db, *, capacity=0, profile_enabled=True):
    memory = AsyncMemory.__new__(AsyncMemory)
    profile_config = UserProfileConfig(enabled=profile_enabled)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=True, short_term_capacity=capacity),
        profile=profile_config,
        background=BackgroundTaskConfig(
            max_retries=0,
            poll_interval_seconds=0.01,
            shutdown_timeout_seconds=2,
        ),
        history_db_path=db.db_path,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = None
    memory._profile_user_locks = {}
    memory._profile_user_locks_guard = asyncio.Lock()
    memory._entity_store = None
    return memory


def test_add_returns_without_waiting_for_migration_or_profile_llms(db, monkeypatch):
    memory = _partial_memory(db)
    migration_started = threading.Event()
    profile_started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        migration_started.set()
        release.wait(2)
        return []

    class BlockingProfileUpdater:
        async def generate_update_plan_async(self, **kwargs):
            profile_started.set()
            release.wait(2)
            return ProfileUpdatePlan()

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._profile_updater = BlockingProfileUpdater()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)

    started_at = time.monotonic()
    result = memory.add(_messages(1), user_id="u1", run_id="r1")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert result["results"] == []
    assert result["background"]["migration_job_id"]
    assert result["background"]["profile_job_id"]
    assert db.get_migration_job_messages(result["background"]["migration_job_id"])
    assert migration_started.wait(1)
    assert profile_started.wait(1)

    release.set()
    assert memory.flush_background_tasks(2)
    memory.close()
    assert db.connection is None


@pytest.mark.asyncio
async def test_async_add_returns_without_waiting_for_migration_or_profile_llms(db, monkeypatch):
    memory = _partial_async_memory(db)
    migration_started = threading.Event()
    profile_started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        migration_started.set()
        release.wait(2)
        return []

    class BlockingProfileUpdater:
        async def generate_update_plan_async(self, **kwargs):
            profile_started.set()
            release.wait(2)
            return ProfileUpdatePlan()

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[])
    memory._profile_updater = BlockingProfileUpdater()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", AsyncMock())

    started_at = time.monotonic()
    result = await memory.add(_messages(1), user_id="u1", run_id="r1")
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert result["results"] == []
    assert result["background"]["migration_job_id"]
    assert result["background"]["profile_job_id"]
    assert db.get_migration_job_messages(result["background"]["migration_job_id"])
    assert migration_started.wait(1)
    assert profile_started.wait(1)

    release.set()
    assert await memory.flush_background_tasks(2)
    memory.close()
    assert db.connection is None


def test_close_waits_for_running_worker_before_closing_sqlite(db, monkeypatch):
    memory = _partial_memory(db, profile_enabled=False)
    started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        started.set()
        release.wait(2)
        return []

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)
    memory.add(_messages(1), user_id="u1", run_id="r1")
    assert started.wait(1)

    close_result = []
    close_thread = threading.Thread(target=lambda: close_result.append(memory.close()))
    close_thread.start()
    time.sleep(0.05)
    assert db.connection is not None
    release.set()
    close_thread.join(2)

    assert close_result == [None]
    assert db.connection is None


def test_reset_waits_for_worker_before_clearing_storage(db, monkeypatch):
    memory = _partial_memory(db, profile_enabled=False)
    started = threading.Event()
    release = threading.Event()

    def midterm(*args, **kwargs):
        started.set()
        release.wait(2)
        return []

    memory._process_midterm_evictions = midterm
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._reset_midterm_state = MagicMock()
    memory.vector_store = MagicMock()
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)
    memory.add(_messages(1), user_id="u1", run_id="r1")
    assert started.wait(1)

    reset_thread = threading.Thread(target=memory.reset)
    reset_thread.start()
    time.sleep(0.05)
    assert reset_thread.is_alive()
    assert db.connection is not None
    release.set()
    reset_thread.join(2)

    assert not reset_thread.is_alive()
    assert memory.db.connection is not None
    assert memory.db.background_jobs_pending() is False
    memory.close()
    assert memory.db is None
