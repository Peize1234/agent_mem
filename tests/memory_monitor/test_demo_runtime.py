from types import SimpleNamespace
from unittest.mock import MagicMock

from mem0.configs.base import BackgroundTaskConfig, MemoryConfig
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import Memory
from mem0.memory.storage import SQLiteManager
from memory_monitor.runtime import DemoBackgroundWorkerManager, DemoMemory


def _messages(label):
    return [
        {"role": "user", "content": f"question-{label}"},
        {"role": "assistant", "content": f"answer-{label}"},
    ]


def _migration_job(db, label):
    job_id = db.save_messages_and_create_migration_job(
        _messages(label),
        "run_id=run-1&user_id=user-1",
        max_messages=0,
        filters={"user_id": "user-1", "run_id": "run-1"},
        metadata={},
        infer=False,
        prompt=None,
    )
    assert job_id
    return job_id


def _demo_worker(db, *, midterm=None, longterm=None, profile=None):
    return DemoBackgroundWorkerManager(
        db,
        BackgroundTaskConfig(enabled=True, max_retries=0),
        process_midterm=midterm or (lambda *args: None),
        process_longterm=longterm or (lambda *args: None),
        process_profile=profile or (lambda *args: None),
    )


def test_default_worker_factory_preserves_production_manager():
    memory = Memory.__new__(Memory)
    memory.db = MagicMock()
    memory.config = SimpleNamespace(background=BackgroundTaskConfig(enabled=False))
    memory._background_process_midterm = MagicMock()
    memory._background_process_longterm = MagicMock()
    memory._background_process_profile = MagicMock()

    worker = memory._create_background_worker_manager()

    assert type(worker) is BackgroundWorkerManager
    assert worker.db is memory.db
    assert worker.config is memory.config.background


def test_production_memory_does_not_create_demo_state(tmp_path, monkeypatch):
    monkeypatch.setattr("mem0.memory.main.MEM0_TELEMETRY", False)
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("mem0.memory.main.EmbedderFactory.create", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("mem0.memory.main.LlmFactory.create", lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr("mem0.memory.main.VectorStoreFactory.create", lambda *args, **kwargs: MagicMock())
    config = MemoryConfig(history_db_path=str(tmp_path / "history.db"))
    config.background.enabled = False

    memory = Memory(config)
    try:
        assert type(memory._background_worker) is BackgroundWorkerManager
        assert not (tmp_path / "demo.db").exists()
        assert not hasattr(memory, "_demo_commit_results")
    finally:
        memory.close()


def test_background_initialization_uses_overridable_factory():
    memory = Memory.__new__(Memory)
    worker = MagicMock()
    memory._create_background_worker_manager = MagicMock(return_value=worker)

    memory._initialize_background_workers()

    assert memory._background_worker is worker
    memory._create_background_worker_manager.assert_called_once_with()
    worker.start.assert_called_once_with()


def test_demo_memory_is_memory_subclass():
    assert issubclass(DemoMemory, Memory)


def test_demo_retrieval_freezes_grouped_context_without_generation():
    memory = DemoMemory.__new__(DemoMemory)
    memory.llm = MagicMock()
    memory._retrieve_context = MagicMock(
        return_value={
            "user_id": "user-1",
            "session_id": "run-1",
            "query": "What changed?",
            "profile": {"risk_level": "balanced"},
            "short_term_messages": [{"role": "user", "content": "recent"}],
            "retrieved_memories": [
                {"id": "mid-1", "source": "mid_term_page", "raw_dialogue": "older"},
                {"id": "long-1", "source": "long_term", "memory": "durable"},
            ],
        }
    )

    context = memory.retrieve_context_for_demo("What changed?", user_id="user-1", session_id="run-1")

    assert context["short_term"][0]["content"] == "recent"
    assert [item["id"] for item in context["mid_term"]] == ["mid-1"]
    assert [item["id"] for item in context["long_term"]] == ["long-1"]
    assert context["user_profile"] == {"risk_level": "balanced"}
    assert len(context["context_hash"]) == 64
    memory.llm.generate_response.assert_not_called()


def test_demo_retrieval_does_not_write_sqlite(tmp_path):
    db = SQLiteManager(str(tmp_path / "history.db"))
    db.save_messages(
        [{"role": "user", "content": "recent"}],
        "run_id=run-1&user_id=user-1",
        max_messages=10,
    )
    memory = DemoMemory.__new__(DemoMemory)
    memory.db = db
    memory.config = SimpleNamespace(
        midterm=SimpleNamespace(enabled=False, short_term_capacity=10),
        background=BackgroundTaskConfig(enabled=False),
    )
    memory.search = MagicMock(return_value={"results": []})
    memory.get_profile = MagicMock(return_value={"user_id": "user-1", "profile": {}})
    before_changes = db.connection.total_changes
    try:
        context = memory.retrieve_context_for_demo("question", user_id="user-1", session_id="run-1")
        assert context["short_term"][0]["content"] == "recent"
        assert db.connection.total_changes == before_changes
    finally:
        db.close()


def test_demo_midterm_retrieval_does_not_record_session_visits():
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(midterm=SimpleNamespace(enabled=True))
    memory._midterm_retriever = MagicMock()
    memory._midterm_retriever.search.return_value = [{"id": "mid-1", "source": "mid_term_page"}]

    results = memory._with_midterm_search_results(
        "query",
        {"user_id": "user-1", "run_id": "run-1"},
        [{"id": "long-1"}],
    )

    assert [item["id"] for item in results] == ["long-1", "mid-1"]
    memory._midterm_retriever.search.assert_called_once_with(
        "query",
        {"user_id": "user-1", "run_id": "run-1"},
        record_visits=False,
    )


def test_prompt_build_uses_frozen_context_and_generation_sends_same_messages():
    memory = DemoMemory.__new__(DemoMemory)
    memory.llm = MagicMock()
    memory.llm.generate_response.return_value = "answer"
    memory._retrieve_context = MagicMock(side_effect=AssertionError("retrieval must not run"))
    context = {
        "user_id": "user-1",
        "session_id": "run-1",
        "query": "What changed?",
        "profile": {},
        "short_term_messages": [],
        "retrieved_memories": [],
    }
    context["context_hash"] = memory.context_hash(context)

    prompt = memory.build_prompt_from_context(context)
    result = memory.generate_response_for_demo(prompt)

    assert result == "answer"
    memory._retrieve_context.assert_not_called()
    memory.llm.generate_response.assert_called_once_with(messages=prompt)


def test_commit_demo_turn_forwards_stable_persisted_idempotency_key(monkeypatch):
    calls = []

    def fake_add(self, messages, **kwargs):
        calls.append((messages, kwargs))
        return {"results": [], "background": {"migration_job_id": "migration-1", "profile_job_id": "profile-1"}}

    monkeypatch.setattr(Memory, "add", fake_add)
    memory = DemoMemory.__new__(DemoMemory)

    result = memory.commit_demo_turn(
        simulation_id="simulation-1",
        turn_id="turn-1",
        user_id="user-1",
        run_id="run-1",
        user_message="question",
        assistant_message="answer",
        metadata={"source": "lab"},
    )

    assert result["background"]["migration_job_id"] == "migration-1"
    assert len(calls) == 1
    assert calls[0][1]["metadata"] == {"source": "lab"}
    assert calls[0][1]["idempotency_key"] == "demo-turn:simulation-1:turn-1"


def test_demo_worker_runs_complete_migration_and_preserves_queue_order():
    db = SQLiteManager(":memory:")
    first = _migration_job(db, "first")
    second = _migration_job(db, "second")
    stages = []
    worker = _demo_worker(
        db,
        midterm=lambda job, messages, degraded: stages.append((job["job_id"], "midterm", degraded)),
        longterm=lambda job, messages, degraded: stages.append((job["job_id"], "longterm", degraded)),
    )
    try:
        worker.start()
        assert worker.threads_alive() is False
        assert worker.flush(timeout=0) is False
        assert worker.process_migration_job(second) is False
        assert worker.process_migration_job(first) is True
        assert worker.process_migration_job(second) is True
        assert worker.flush(timeout=0) is True
        assert stages == [
            (first, "midterm", False),
            (first, "longterm", False),
            (second, "midterm", False),
            (second, "longterm", False),
        ]
        assert worker.get_job_status(first, "migration")["status"] == "succeeded"
        assert worker.get_job_status(second, "migration")["status"] == "succeeded"
    finally:
        worker.stop(timeout=1)
        db.close()


def test_demo_worker_runs_profile_job_manually():
    db = SQLiteManager(":memory:")
    job_id = db.create_profile_update_job("user-1", _messages("profile"))
    processed = []
    worker = _demo_worker(db, profile=lambda job: processed.append(job["job_id"]))
    try:
        worker.start()
        assert worker.process_profile_job(job_id) is True
        assert processed == [job_id]
        assert worker.get_job_status(job_id, "profile")["status"] == "succeeded"
    finally:
        worker.stop(timeout=1)
        db.close()
