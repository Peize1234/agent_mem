import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from mem0.configs.base import AgenticRetrievalConfig, BackgroundTaskConfig, MemoryConfig
from mem0.memory.background_worker import BackgroundWorkerManager
from mem0.memory.main import Memory
from mem0.memory.process_lock import ProcessInstanceLock
from mem0.memory.storage import SQLiteManager
from memory_monitor.runtime import DemoBackgroundWorkerManager, DemoMemory
from memory_monitor.runtime.llm_trace import TracedToolExecutor


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


def _demo_worker(db, *, midterm=None, longterm=None, profile=None, event_recorder=None, **kwargs):
    return DemoBackgroundWorkerManager(
        db,
        BackgroundTaskConfig(enabled=True, max_retries=0),
        process_midterm=midterm or (lambda *args: None),
        process_longterm=longterm or (lambda *args: None),
        process_profile=profile or (lambda *args: None),
        event_recorder=event_recorder,
        **kwargs,
    )


def test_demo_profile_stale_lease_is_not_reported_as_failure():
    db = SQLiteManager(":memory:")
    events = []
    try:
        job_id = db.create_profile_update_job("user-1", _messages("profile"))
        worker = _demo_worker(
            db,
            profile=lambda job: False,
            event_recorder=lambda event_type, payload: events.append((event_type, payload)),
        )

        assert worker.process_profile_job(job_id)

        job = db.get_background_job(job_id, "profile")
        assert job["status"] == "running"
        assert job["attempts"] == 0
        assert all(event_type != "job.failed" for event_type, _payload in events)
    finally:
        db.close()


def test_demo_close_does_not_close_db_while_manual_job_is_alive(tmp_path):
    db_path = str(tmp_path / "history.db")
    db = SQLiteManager(db_path)
    job_id = _migration_job(db, "blocked-close")
    entered = threading.Event()
    release = threading.Event()

    def midterm(job, messages, degraded):
        entered.set()
        assert release.wait(2)

    worker = _demo_worker(db, midterm=midterm)
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(
        history_db_path=db_path,
        background=BackgroundTaskConfig(enabled=True, shutdown_timeout_seconds=0.01),
    )
    memory.db = db
    memory._background_worker = worker
    memory._process_instance_lock = ProcessInstanceLock(db_path, enabled=False)
    memory.vector_store = MagicMock()
    memory.embedding_model = MagicMock()
    memory.llm = MagicMock()
    memory.reranker = None
    memory._entity_store = None
    memory._midterm_memory = None
    processing = threading.Thread(target=worker.process_midterm_job, args=(job_id,))
    processing.start()
    assert entered.wait(1)

    assert memory.close() is False
    assert memory.db is db
    assert db.connection is not None

    release.set()
    processing.join(2)
    assert not processing.is_alive()
    assert memory.close() is True
    assert memory.db is None


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


def _demo_memory_for_warmup(retrieve=None):
    memory = DemoMemory.__new__(DemoMemory)
    memory._demo_retrieval_warmup_lock = threading.Lock()
    memory._demo_retrieval_warmed_up = False
    memory.retrieve_context_for_demo = retrieve or MagicMock(return_value={})
    return memory


def test_demo_retrieval_warmup_runs_bilingual_read_only_queries_once():
    memory = _demo_memory_for_warmup()
    memory.add = MagicMock()
    memory.commit_demo_turn = MagicMock()
    memory.generate_response_for_demo = MagicMock()
    memory.generate_agentic_response_for_demo = MagicMock()
    memory._ensure_background_workers = MagicMock()

    assert memory.warm_up_retrieval_for_demo() is True
    assert memory.warm_up_retrieval_for_demo() is False

    assert memory.retrieve_context_for_demo.call_args_list == [
        call(
            "金融分析预热",
            user_id="__demo_warmup_user__",
            session_id="__demo_warmup_session__",
        ),
        call(
            "retrieval warmup",
            user_id="__demo_warmup_user__",
            session_id="__demo_warmup_session__",
        ),
    ]
    memory.add.assert_not_called()
    memory.commit_demo_turn.assert_not_called()
    memory.generate_response_for_demo.assert_not_called()
    memory.generate_agentic_response_for_demo.assert_not_called()
    memory._ensure_background_workers.assert_not_called()


def test_demo_retrieval_warmup_failure_allows_retry():
    retrieve = MagicMock(side_effect=[RuntimeError("cold start failed"), {}, {}])
    memory = _demo_memory_for_warmup(retrieve)

    with pytest.raises(RuntimeError, match="cold start failed"):
        memory.warm_up_retrieval_for_demo()

    assert memory._demo_retrieval_warmed_up is False
    assert memory.warm_up_retrieval_for_demo() is True
    assert memory.warm_up_retrieval_for_demo() is False
    assert retrieve.call_count == 3


def test_demo_memory_inherits_idempotent_core_vector_client_close():
    client = MagicMock()
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(
        history_db_path="unused.db",
        background=BackgroundTaskConfig(enabled=False),
    )
    memory.db = MagicMock()
    memory._background_worker = None
    memory._process_instance_lock = ProcessInstanceLock("unused.db", enabled=False)
    memory.vector_store = SimpleNamespace(client=client)
    memory.embedding_model = MagicMock()
    memory.llm = MagicMock()
    memory.reranker = None
    memory._entity_store = None
    memory._midterm_memory = None

    assert memory.close() is True
    client.close.assert_called_once_with()
    assert memory.close() is True
    client.close.assert_called_once_with()


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


def test_demo_frozen_context_excludes_monitor_trace_metadata():
    memory = DemoMemory.__new__(DemoMemory)
    context = {
        "query": "它有哪些风险？",
        "retrieval_query": "华辰智能装备有哪些供应链风险？",
        "short_term_messages": [],
        "retrieved_memories": [],
    }
    context["context_hash"] = memory.context_hash(context)
    context["llm_calls"] = [{"purpose": "问题重写"}]
    context["tool_calls"] = []

    frozen = memory._validated_frozen_context(context)

    assert frozen["retrieval_query"] == "华辰智能装备有哪些供应链风险？"
    assert "llm_calls" not in frozen
    assert "tool_calls" not in frozen


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


def test_demo_agentic_generation_does_not_record_midterm_visits(monkeypatch):
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))
    memory.llm = MagicMock()
    core_runner = MagicMock(
        return_value={
            "status": "not_needed",
            "supplement": "",
            "answer": "",
            "iterations": 1,
            "tool_call_count": 0,
            "stop_reason": "not_needed",
            "tool_trace": [],
        }
    )
    monkeypatch.setattr(Memory, "_run_agentic_retrieval_from_context", core_runner)
    context = {
        "query": "question",
        "user_id": "user-1",
        "session_id": "run-1",
        "profile": {},
        "short_term_messages": [],
        "retrieved_memories": [],
    }
    context["context_hash"] = memory.context_hash(context)

    result = memory.generate_agentic_response_for_demo(
        context,
        temperature=0.2,
    )

    assert result["status"] == "not_needed"
    assert result["supplement"] == result["answer"] == ""
    core_context = core_runner.call_args.args[0]
    assert "context_hash" not in core_context
    core_runner.assert_called_once_with(
        core_context,
        reference_information=None,
        generation_kwargs={"temperature": 0.2},
        record_midterm_visits=False,
    )


def test_demo_agentic_tool_tracing_decorates_the_core_executor(monkeypatch):
    memory = DemoMemory.__new__(DemoMemory)
    core_executor = MagicMock()
    core_factory = MagicMock(return_value=core_executor)
    monkeypatch.setattr(Memory, "_create_agentic_tool_executor", core_factory)

    executor = memory._create_agentic_tool_executor(
        user_id="user-1",
        session_id="run-1",
        record_midterm_visits=False,
    )

    assert isinstance(executor, TracedToolExecutor)
    assert executor._wrapped is core_executor
    core_factory.assert_called_once_with(
        user_id="user-1",
        session_id="run-1",
        record_midterm_visits=False,
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

    prompt = memory.build_prompt_from_context(context, agentic_memory_supplement="historical supplement")
    result = memory.generate_response_for_demo(prompt)

    assert result == "answer"
    assert "<agentic_memory_supplement>\nhistorical supplement\n</agentic_memory_supplement>" in prompt[0]["content"]
    assert "外部记忆工具" not in prompt[0]["content"]
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
        assert not hasattr(worker, "_manual_lock")
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


def test_demo_worker_can_run_longterm_and_midterm_stages_separately():
    db = SQLiteManager(":memory:")
    job_id = _migration_job(db, "separate-stages")
    stages = []
    worker = _demo_worker(
        db,
        midterm=lambda job, messages, degraded: stages.append("midterm"),
        longterm=lambda job, messages, degraded: stages.append("longterm"),
    )
    try:
        worker.start()
        assert worker.process_longterm_job(job_id) is True
        job = worker.get_job_status(job_id, "migration")
        assert job["longterm_status"] == "succeeded"
        assert job["midterm_status"] == "pending"
        assert db.get_migration_job_messages(job_id)

        assert worker.process_midterm_job(job_id) is True
        assert worker.get_job_status(job_id, "migration")["status"] == "succeeded"
        assert db.get_migration_job_messages(job_id) == []
        assert stages == ["longterm", "midterm"]
    finally:
        worker.stop(timeout=1)
        db.close()


def test_demo_worker_reports_discarded_stage_and_completed_with_loss():
    db = SQLiteManager(":memory:")
    job_id = _migration_job(db, "discarded")
    events = []

    def fail_longterm(job, messages, degraded):
        raise RuntimeError("longterm unavailable")

    worker = _demo_worker(
        db,
        longterm=fail_longterm,
        event_recorder=lambda event_type, payload: events.append((event_type, payload)),
    )
    try:
        worker.start()
        assert worker.process_migration_job(job_id) is True
        job = worker.get_job_status(job_id, "migration")
        assert job["status"] == "completed_with_loss"
        assert job["midterm_status"] == "succeeded"
        assert job["longterm_status"] == "discarded"
        assert job["longterm_attempts"] == 1
        assert job["longterm_last_error"] == "degradation: longterm unavailable"
        discarded_events = [payload for event_type, payload in events if event_type == "job.discarded"]
        assert discarded_events == [
            {
                "job_type": "migration",
                "stage": "longterm",
                "job_id": job_id,
                "attempts": 1,
                "last_error": "degradation: longterm unavailable",
            }
        ]
        assert worker.process_longterm_job(job_id) is False
        assert len([payload for event_type, payload in events if event_type == "job.discarded"]) == 1
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


def test_demo_worker_records_profile_discarded_once():
    db = SQLiteManager(":memory:")
    job_id = db.create_profile_update_job("user-1", _messages("profile-discarded"))
    events = []

    def fail_profile(job):
        raise RuntimeError("profile unavailable")

    worker = _demo_worker(
        db,
        profile=fail_profile,
        event_recorder=lambda event_type, payload: events.append((event_type, payload)),
    )
    try:
        worker.start()
        assert worker.process_profile_job(job_id) is True
        assert worker.process_profile_job(job_id) is False
        discarded_events = [payload for event_type, payload in events if event_type == "job.discarded"]
        assert discarded_events == [
            {
                "job_type": "profile",
                "job_id": job_id,
                "attempts": 1,
                "last_error": "profile unavailable",
            }
        ]
    finally:
        worker.stop(timeout=1)
        db.close()


def test_demo_worker_handles_unexpected_stage_failure_with_new_signature():
    db = SQLiteManager(":memory:")
    job_id = _migration_job(db, "unexpected")
    worker = _demo_worker(db)
    worker._run_migration_stage = MagicMock(side_effect=RuntimeError("unexpected"))
    worker._persist_unexpected_stage_failure = MagicMock()
    try:
        assert worker.process_midterm_job(job_id) is True
        claimed_job = worker._persist_unexpected_stage_failure.call_args.args[0]
        worker._persist_unexpected_stage_failure.assert_called_once_with(
            claimed_job,
            "midterm",
            worker.process_midterm,
            worker._run_migration_stage.side_effect,
        )
        assert claimed_job["midterm_lease_token"]
    finally:
        db.close()


def test_demo_worker_uses_fenced_output_cleanup():
    db = SQLiteManager(":memory:")
    job_id = _migration_job(db, "fenced-cleanup")
    claimed_tokens = []
    cleanup_tokens = []

    def fail_stage(job, messages, degraded):
        claimed_tokens.append(job["longterm_lease_token"])
        raise RuntimeError("failed")

    worker = _demo_worker(
        db,
        longterm=fail_stage,
        discard_migration_outputs=lambda job, stage, token: cleanup_tokens.append(token),
    )
    try:
        assert worker.process_longterm_job(job_id) is True
        assert cleanup_tokens == [claimed_tokens[-1]]
        assert db.get_background_job(job_id)["longterm_status"] == "discarded"
    finally:
        db.close()
