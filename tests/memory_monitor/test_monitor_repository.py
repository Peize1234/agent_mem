import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

from mem0.configs.base import MemoryConfig
from mem0.memory.storage import SQLiteManager
from memory_monitor.services.monitor_repository import MonitorRepository
from memory_monitor.services.simulation_service import SimulationService
from memory_monitor.services.vector_repository import VectorStoreMonitorRepository


def test_repository_pages_filters_and_decodes_json(tmp_path):
    db_path = tmp_path / "history.db"
    db = SQLiteManager(str(db_path))
    try:
        for index in range(5):
            db.create_profile_update_job(
                "user-1" if index < 3 else "user-2",
                [{"role": "user", "content": str(index)}],
                trace_id=f"trace-{index}",
            )
        db.save_messages_and_create_migration_job(
            [{"role": "user", "content": "migration"}],
            "run_id=run-1&user_id=user-1",
            max_messages=0,
            filters={"user_id": "user-1", "run_id": "run-1"},
            metadata={},
            infer=False,
            prompt=None,
            trace_id="migration-trace",
        )
    finally:
        db.close()

    repository = MonitorRepository(db_path)
    first = repository.page(
        "profile_update_jobs",
        page=1,
        page_size=2,
        filters={"user_id": "user-1"},
        descending=False,
    )
    second = repository.page(
        "profile_update_jobs",
        page=2,
        page_size=2,
        filters={"user_id": "user-1"},
        descending=False,
    )

    assert first["total"] == 3
    assert len(first["items"]) == 2
    assert len(second["items"]) == 1
    assert isinstance(first["items"][0]["messages"], list)
    migration = repository.page(
        "memory_migration_jobs",
        filters={"user_id": "user-1", "run_id": "run-1"},
    )
    messages = repository.page(
        "messages",
        filters={"user_id": "user-1", "run_id": "run-1"},
    )
    assert migration["total"] == 1
    assert messages["total"] == 1


class _FakeWorker:
    def __init__(self):
        self.midterm_calls = []
        self.longterm_calls = []
        self.process_midterm = lambda *args: self.midterm_calls.append(args)
        self.process_longterm = lambda *args: self.longterm_calls.append(args)


class _FakeMemory:
    def __init__(self, config):
        self.config = config
        self.db = SQLiteManager(config.history_db_path)
        self._background_worker = _FakeWorker()

    def close(self):
        self.db.close()

    def _midterm_enabled(self):
        return False


def test_simulation_environment_never_uses_real_database(tmp_path):
    real_db = tmp_path / "real.db"
    connection = sqlite3.connect(real_db)
    connection.execute("CREATE TABLE sentinel (value TEXT)")
    connection.execute("INSERT INTO sentinel VALUES ('untouched')")
    connection.commit()
    connection.close()

    config = MemoryConfig()
    config.history_db_path = str(real_db)
    service = SimulationService(
        tmp_path / "runs",
        base_config=config,
        memory_factory=_FakeMemory,
    )
    environment = service.create_environment("isolated")
    try:
        assert environment.memory.config.history_db_path != str(real_db)
        assert environment.memory.config.vector_store.config.collection_name == "memory_monitor_isolated"
        assert str(environment.memory.config.vector_store.config.path).startswith(str(environment.root))
        check = sqlite3.connect(real_db)
        try:
            assert check.execute("SELECT value FROM sentinel").fetchone()[0] == "untouched"
        finally:
            check.close()
        worker = environment.memory._background_worker
        worker.process_midterm({"metadata": {"memory_monitor_midterm_enabled": False}}, [], False)
        worker.process_longterm({"metadata": {"memory_monitor_longterm_enabled": False}}, [], False)
        assert worker.midterm_calls == []
        assert worker.longterm_calls == []
        worker.process_midterm({"metadata": {"memory_monitor_midterm_enabled": True}}, [], False)
        worker.process_longterm({"metadata": {"memory_monitor_longterm_enabled": True}}, [], False)
        assert len(worker.midterm_calls) == 1
        assert len(worker.longterm_calls) == 1
    finally:
        service.clear_environment("isolated")


def test_simulation_snapshot_diff_reports_jobs_vectors_profiles_and_events():
    before = {
        "messages": [{"id": "message-1", "status": "active"}],
        "migration_jobs": [],
        "profile_jobs": [],
        "profile_values": [{"attribute_id": 1, "value": "before"}],
        "events": [],
        "vectors": {"longterm": []},
    }
    after = {
        "messages": [
            {"id": "message-1", "status": "pending"},
            {"id": "message-2", "status": "active"},
        ],
        "migration_jobs": [{"job_id": "migration-1", "status": "pending"}],
        "profile_jobs": [{"job_id": "profile-1", "status": "pending"}],
        "profile_values": [{"attribute_id": 1, "value": "after"}],
        "events": [{"event_id": "event-1", "event_type": "add.returned"}],
        "vectors": {"longterm": [{"id": "memory-1", "payload": {"trace_id": "trace-1"}}]},
    }

    changes = SimulationService.compare_snapshots(before, after)

    assert [item["id"] for item in changes["added_messages"]] == ["message-2"]
    assert [item["id"] for item in changes["evicted_messages"]] == ["message-1"]
    assert changes["created_migration_jobs"][0]["job_id"] == "migration-1"
    assert changes["created_profile_jobs"][0]["job_id"] == "profile-1"
    assert changes["profile_changes"][0]["value"] == "after"
    assert changes["vector_changes"]["longterm"][0]["id"] == "memory-1"
    assert changes["new_events"][0]["event_id"] == "event-1"


def test_vector_trace_filter_uses_reserved_longterm_metadata_field():
    memory = MagicMock()
    memory.vector_store.list.return_value = [
        SimpleNamespace(id="memory-1", payload={"_mem0_trace_id": "trace-1"}, score=1.0),
    ]
    repository = VectorStoreMonitorRepository(memory)

    result = repository.page("longterm", filters={"trace_id": "trace-1"})

    memory.vector_store.list.assert_called_once_with(filters={"_mem0_trace_id": "trace-1"}, top_k=50)
    assert result["items"][0]["payload"]["_mem0_trace_id"] == "trace-1"
