import os
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig, MemoryConfig
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.process_lock import ProcessInstanceLock
from mem0.memory.storage import SQLiteManager
from mem0.utils.bounded_timeout import BoundedTimeoutExecutor


class _Row:
    def __init__(self, row_id, payload):
        self.id = row_id
        self.payload = payload


def test_external_timeout_defaults_and_validation():
    config = MemoryConfig()

    assert config.llm_timeout_seconds == 60.0
    assert config.embedding_timeout_seconds == 30.0
    assert config.vector_store_timeout_seconds == 15.0
    assert config.reranker_timeout_seconds == 30.0
    assert config.entity_extraction_timeout_seconds == 60.0
    for field in (
        "llm_timeout_seconds",
        "embedding_timeout_seconds",
        "vector_store_timeout_seconds",
        "reranker_timeout_seconds",
        "entity_extraction_timeout_seconds",
    ):
        with pytest.raises(ValueError):
            MemoryConfig(**{field: 0})


def test_entity_extraction_timeout_uses_bounded_workers():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(entity_extraction_timeout_seconds=0.01)
    release = threading.Event()

    def blocked_extraction():
        release.wait(1)

    try:
        for _ in range(8):
            with pytest.raises(TimeoutError, match="Entity extraction timed out"):
                memory._run_entity_extraction(blocked_extraction)
        timeout_threads = list(memory._entity_extraction_executor._executor._threads)
        assert len(timeout_threads) <= 2
        assert all(thread.name.startswith("mem0-entity-extraction") for thread in timeout_threads)
    finally:
        release.set()
        memory._entity_extraction_executor.shutdown()


def test_bounded_timeout_executor_capacity_timeout_and_shutdown():
    executor = BoundedTimeoutExecutor(
        max_workers=1,
        max_pending=1,
        thread_name_prefix="mem0-test-bounded-timeout",
    )
    release = threading.Event()
    finished = threading.Event()

    def blocked_extraction():
        release.wait(1)
        finished.set()

    try:
        with pytest.raises(TimeoutError, match="timed out after"):
            executor.run(
                blocked_extraction,
                timeout_seconds=0.01,
                operation_name="Test extraction",
            )
        assert not finished.is_set()
        with pytest.raises(TimeoutError, match="timed out after"):
            executor.run(
                blocked_extraction,
                timeout_seconds=0.01,
                operation_name="Test extraction",
            )
        with pytest.raises(TimeoutError, match="capacity is exhausted"):
            executor.run(
                blocked_extraction,
                timeout_seconds=0.01,
                operation_name="Test extraction",
            )
    finally:
        release.set()
        executor.shutdown()
        executor.shutdown()

    assert finished.is_set()
    with pytest.raises(RuntimeError, match="after shutdown"):
        executor.run(lambda: None, timeout_seconds=1, operation_name="Test extraction")


def test_memory_instances_own_independent_entity_extraction_executors():
    config = SimpleNamespace(
        background=BackgroundTaskConfig(
            enabled=False,
            entity_extraction_worker_count=3,
            entity_extraction_pending_capacity=5,
        ),
        entity_extraction_timeout_seconds=1,
    )
    first = Memory.__new__(Memory)
    second = Memory.__new__(Memory)
    first.config = config
    second.config = config
    first._initialize_entity_extraction_executor()
    second._initialize_entity_extraction_executor()

    assert first._entity_extraction_executor is not second._entity_extraction_executor
    first_executor = first._entity_extraction_executor
    first._initialize_entity_extraction_executor()
    assert first._entity_extraction_executor is first_executor
    assert first._entity_extraction_executor._max_workers == 3
    assert first._entity_extraction_executor._max_pending == 5
    assert not first._entity_extraction_executor._executor._threads
    assert not second._entity_extraction_executor._executor._threads

    first._entity_extraction_executor.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        first._run_entity_extraction(lambda: "closed")
    assert second._run_entity_extraction(lambda: "open") == "open"
    second._entity_extraction_executor.shutdown()


def test_closing_one_memory_does_not_shutdown_another_entity_executor():
    def memory_instance():
        memory = Memory.__new__(Memory)
        memory.config = SimpleNamespace(
            background=BackgroundTaskConfig(enabled=False),
            entity_extraction_timeout_seconds=1,
            history_db_path=":memory:",
        )
        memory.db = SQLiteManager(":memory:")
        memory._background_worker = None
        memory.vector_store = MagicMock()
        memory.embedding_model = MagicMock()
        memory.llm = MagicMock()
        memory.reranker = None
        memory._entity_store = None
        memory._midterm_memory = None
        memory._initialize_entity_extraction_executor()
        return memory

    first = memory_instance()
    second = memory_instance()
    try:
        assert first._run_entity_extraction(lambda: "first") == "first"
        assert second._run_entity_extraction(lambda: "second") == "second"
        assert first.close() is True
        with pytest.raises(RuntimeError, match="after shutdown"):
            first._run_entity_extraction(lambda: "closed")
        assert second._run_entity_extraction(lambda: "still-open") == "still-open"
    finally:
        second.close()


def test_memory_forwards_external_timeouts_to_factories(monkeypatch):
    embedder_create = MagicMock(return_value=MagicMock())
    vector_create = MagicMock(return_value=MagicMock())
    llm_create = MagicMock(return_value=MagicMock())
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False)
    monkeypatch.setattr("mem0.memory.main.EmbedderFactory.create", embedder_create)
    monkeypatch.setattr("mem0.memory.main.VectorStoreFactory.create", vector_create)
    monkeypatch.setattr("mem0.memory.main.LlmFactory.create", llm_create)
    config = MemoryConfig(
        history_db_path=":memory:",
        background=BackgroundTaskConfig(
            enabled=False,
            entity_extraction_worker_count=3,
            entity_extraction_pending_capacity=7,
        ),
        llm_timeout_seconds=11,
        embedding_timeout_seconds=12,
        vector_store_timeout_seconds=13,
    )

    memory = Memory(config)
    executor_threads = []
    try:
        assert embedder_create.call_args.kwargs["timeout_seconds"] == 12
        assert vector_create.call_args.kwargs["timeout_seconds"] == 13
        assert llm_create.call_args.kwargs["timeout_seconds"] == 11
        assert memory._entity_extraction_executor._max_workers == 3
        assert memory._entity_extraction_executor._max_pending == 7
        assert not memory._entity_extraction_executor._executor._threads
        assert memory._run_entity_extraction(lambda: "ok") == "ok"
        executor_threads = list(memory._entity_extraction_executor._executor._threads)
        assert executor_threads
    finally:
        memory.close()
    assert all(not thread.is_alive() for thread in executor_threads)


def _run_process_lock_probe(db_path: str) -> subprocess.CompletedProcess:
    code = """
import importlib.util
import sys
spec = importlib.util.spec_from_file_location("mem0_process_lock_probe", sys.argv[2])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = module.ProcessInstanceLock(sys.argv[1])
try:
    lock.acquire()
except RuntimeError:
    raise SystemExit(23)
lock.release()
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd() + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            db_path,
            os.path.join(os.getcwd(), "mem0", "memory", "process_lock.py"),
        ],
        env=env,
        check=False,
        timeout=5,
    )


def test_second_process_lock_for_same_db_fails_and_release_allows_next(tmp_path):
    db_path = str(tmp_path / "history.db")
    process_lock = ProcessInstanceLock(db_path)
    process_lock.acquire()
    try:
        assert _run_process_lock_probe(db_path).returncode == 23
    finally:
        process_lock.release()

    assert _run_process_lock_probe(db_path).returncode == 0


def test_in_memory_and_disabled_process_locks_are_noops(tmp_path):
    in_memory = ProcessInstanceLock(":memory:")
    disabled = ProcessInstanceLock(str(tmp_path / "history.db"), enabled=False)

    in_memory.acquire()
    disabled.acquire()

    assert not in_memory.acquired
    assert not disabled.acquired


def test_profile_handler_false_is_stale_and_does_not_record_failure(caplog):
    db = MagicMock()
    manager = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(enabled=False),
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda job: False,
    )
    heartbeat = MagicMock()
    manager._start_profile_heartbeat = MagicMock(return_value=heartbeat)
    manager._stop_heartbeat = MagicMock()
    job = {
        "job_id": "profile-job",
        "user_id": "user-1",
        "attempts": 2,
        "recovery_count": 1,
        "lease_token": "abcdefgh-more",
    }

    with caplog.at_level("INFO"):
        manager._run_profile_job(job)

    db.finish_profile_job.assert_not_called()
    db.record_profile_failure.assert_not_called()
    assert "Background profile job abandoned because lease is stale" in caplog.text
    assert "abcdefgh" in caplog.text


def test_profile_handler_none_uses_legacy_finish_and_logs_stale(caplog):
    db = MagicMock()
    db.finish_profile_job.return_value = False
    manager = BackgroundWorkerManager(
        db,
        BackgroundTaskConfig(enabled=False),
        process_midterm=lambda *args: None,
        process_longterm=lambda *args: None,
        process_profile=lambda job: None,
    )
    manager._start_profile_heartbeat = MagicMock(return_value=MagicMock())
    manager._stop_heartbeat = MagicMock()
    job = {
        "job_id": "profile-job",
        "user_id": "user-1",
        "attempts": 0,
        "recovery_count": 0,
        "lease_token": "abcdefgh-more",
    }

    with caplog.at_level("INFO"):
        manager._run_profile_job(job)

    db.finish_profile_job.assert_called_once_with("profile-job", "abcdefgh-more")
    db.record_profile_failure.assert_not_called()
    assert "Background profile job abandoned because lease is stale" in caplog.text


@pytest.mark.parametrize("memory_class", [Memory, AsyncMemory])
def test_close_failure_keeps_database_resources_and_process_lock(tmp_path, memory_class):
    db_path = str(tmp_path / "history.db")
    memory = memory_class.__new__(memory_class)
    memory.config = SimpleNamespace(
        history_db_path=db_path,
        background=BackgroundTaskConfig(enabled=True, shutdown_timeout_seconds=0.01),
    )
    memory.db = SQLiteManager(db_path)
    memory._background_worker = MagicMock()
    memory._background_worker.stop.side_effect = [False, True]
    memory._process_instance_lock = ProcessInstanceLock(db_path)
    memory._process_instance_lock.acquire()
    memory.vector_store = MagicMock()
    memory.embedding_model = MagicMock()
    memory.llm = MagicMock()
    memory.reranker = None
    memory._entity_store = None
    memory._midterm_memory = None
    memory._entity_extraction_executor = MagicMock()

    assert memory.close() is False
    assert memory.db is not None
    assert memory.db.connection is not None
    assert memory._process_instance_lock.acquired
    memory.vector_store.close.assert_not_called()
    memory._entity_extraction_executor.shutdown.assert_not_called()

    assert memory.close() is True
    memory._entity_extraction_executor.shutdown.assert_called_once_with(wait=True)
    assert memory.db is None
    assert not memory._process_instance_lock.acquired
    memory.vector_store.close.assert_called_once()
    assert memory.close() is True


def _orphan_cleanup_memory(*, stage_status, page_payload=None, longterm_payload=None):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        midterm=SimpleNamespace(enabled=page_payload is not None),
        vector_store=SimpleNamespace(provider="fake"),
    )
    memory.db = MagicMock()
    memory.db.get_background_job.return_value = {
        "midterm_status": stage_status,
        "longterm_status": stage_status,
        "filters": {"user_id": "user-1"},
    }
    page_rows = [_Row("page-1", page_payload)] if page_payload is not None else []
    memory._midterm_memory = MagicMock()
    memory._midterm_memory.list_pages.side_effect = lambda **kwargs: page_rows
    memory._midterm_memory.list_sessions.return_value = []
    memory._midterm_memory.get_page.side_effect = lambda page_id: page_rows[0] if page_rows else None
    memory._midterm_memory.update_page.side_effect = (
        lambda page_id, payload, reembed=False: page_rows[0].payload.update(payload)
    )

    def discard_midterm(source_job_id, lease_token):
        for row in page_rows:
            if (
                row.payload.get("source_job_id") == source_job_id
                and row.payload.get("output_lease_token") == lease_token
            ):
                row.payload["output_state"] = "discarded"
                row.payload["output_lease_token"] = None
        return None

    memory._midterm_updater = MagicMock()
    memory._midterm_updater.discard_source_job_outputs.side_effect = discard_midterm
    longterm_rows = [_Row("memory-1", longterm_payload)] if longterm_payload is not None else []
    memory.vector_store = MagicMock()
    memory.vector_store.list.return_value = (longterm_rows, None)

    def update_longterm(*, vector_id, vector, payload):
        assert vector is None
        for row in longterm_rows:
            if str(row.id) == str(vector_id):
                row.payload.clear()
                row.payload.update(payload)

    memory.vector_store.update.side_effect = update_longterm
    memory._entity_store = None
    return memory


def test_startup_cleanup_discards_terminal_midterm_staging():
    payload = {
        "source_job_id": "job-1",
        "source_stage": "midterm",
        "output_state": "staging",
        "output_lease_token": "old-token",
    }
    memory = _orphan_cleanup_memory(stage_status="succeeded", page_payload=payload)

    result = memory._cleanup_orphan_staging_outputs()

    assert result["midterm_discarded"] == 1
    assert payload["output_state"] == "discarded"
    assert payload["output_lease_token"] is None
    assert payload["cleanup_reason"] == "orphan staging after terminal stage"


def test_startup_cleanup_keeps_active_stage_staging():
    page_payload = {
        "source_job_id": "job-1",
        "source_stage": "midterm",
        "output_state": "staging",
        "output_lease_token": "active-token",
    }
    longterm_payload = {
        "source_job_id": "job-1",
        "source_stage": "longterm",
        "output_state": "staging",
        "output_lease_token": "active-token",
    }
    memory = _orphan_cleanup_memory(
        stage_status="retry",
        page_payload=page_payload,
        longterm_payload=longterm_payload,
    )

    result = memory._cleanup_orphan_staging_outputs()

    assert result["midterm_discarded"] == 0
    assert result["longterm_discarded"] == 0
    assert page_payload["output_state"] == "staging"
    assert longterm_payload["output_state"] == "staging"


def test_startup_cleanup_discards_terminal_longterm_staging():
    payload = {
        "source_job_id": "job-1",
        "source_stage": "longterm",
        "output_state": "staging",
        "output_lease_token": "old-token",
    }
    memory = _orphan_cleanup_memory(stage_status="discarded", longterm_payload=payload)

    result = memory._cleanup_orphan_staging_outputs()

    assert result["longterm_discarded"] == 1
    assert payload["output_state"] == "discarded"
    assert payload["output_lease_token"] is None
    assert payload["cleanup_reason"] == "orphan staging after terminal stage"
    memory.db.delete_history_for_memory_ids.assert_called_once_with(["memory-1"])


def test_startup_cleanup_failure_does_not_escape_and_staging_remains_invisible():
    payload = {
        "source_job_id": "job-1",
        "source_stage": "longterm",
        "output_state": "staging",
        "output_lease_token": "old-token",
    }
    memory = _orphan_cleanup_memory(stage_status="discarded", longterm_payload=payload)
    memory.vector_store.update.side_effect = RuntimeError("storage unavailable")

    result = memory._cleanup_orphan_staging_outputs()

    assert result["longterm_discarded"] == 0
    assert result["cleanup_errors"]
    assert payload["output_state"] == "staging"
    assert memory._stage_output_is_visible(payload, "longterm") is False
