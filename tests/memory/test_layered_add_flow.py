import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig
from mem0.configs.enums import MemoryType
from mem0.memory import main as memory_main
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.storage import SQLiteManager


def _layered_config(
    *,
    capacity=4,
    midterm_enabled=True,
    background_enabled=True,
    profile_enabled=False,
    observability_enabled=False,
):
    return SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=midterm_enabled, short_term_capacity=capacity),
        profile=SimpleNamespace(enabled=profile_enabled, update_on_add=profile_enabled),
        background=BackgroundTaskConfig(enabled=background_enabled),
        observability=SimpleNamespace(
            enabled=observability_enabled,
            capture_payloads=True,
            max_payload_length=20000,
        ),
    )


def _sync_memory(
    db,
    *,
    capacity=4,
    midterm_enabled=True,
    background_enabled=True,
    profile_enabled=False,
    observability_enabled=False,
):
    memory = Memory.__new__(Memory)
    memory.config = _layered_config(
        capacity=capacity,
        midterm_enabled=midterm_enabled,
        background_enabled=background_enabled,
        profile_enabled=profile_enabled,
        observability_enabled=observability_enabled,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._process_midterm_evictions = MagicMock()
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._update_profile_after_add = MagicMock()
    memory._background_worker = MagicMock()
    return memory


def _async_memory(
    db,
    *,
    capacity=4,
    midterm_enabled=True,
    background_enabled=True,
    profile_enabled=False,
    observability_enabled=False,
):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = _layered_config(
        capacity=capacity,
        midterm_enabled=midterm_enabled,
        background_enabled=background_enabled,
        profile_enabled=profile_enabled,
        observability_enabled=observability_enabled,
    )
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._process_midterm_evictions = MagicMock()
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[])
    memory._update_profile_after_add = AsyncMock()
    memory._background_worker = MagicMock()
    return memory


def _qa(index):
    return [
        {"role": "user", "content": f"u{index}"},
        {"role": "assistant", "content": f"a{index}"},
    ]


def _contents(messages):
    return [message["content"] for message in messages]


def _assert_add_result(result, expected_results, migration_job_id=None, profile_job_id=None):
    assert result == {
        "results": expected_results,
        "background": {
            "migration_job_id": migration_job_id,
            "profile_job_id": profile_job_id,
        },
    }


def _assert_observed_add_result(result, expected_results, migration_job_id=None, profile_job_id=None):
    assert result["results"] == expected_results
    assert result["background"] == {
        "migration_job_id": migration_job_id,
        "profile_job_id": profile_job_id,
    }
    assert result["observability"]["trace_id"]
    assert "trace_id" not in result
    assert "migration_job_id" not in result
    assert "profile_job_id" not in result


@pytest.fixture(autouse=True)
def disable_add_notices(monkeypatch):
    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice", lambda *args: None)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", AsyncMock())


def test_sync_add_saves_short_term_synchronously_without_running_extractors():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)

        result = memory.add(_qa(1), user_id="u1", run_id="r1")

        _assert_add_result(result, [])
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u1", "a1"]
        memory._process_midterm_evictions.assert_not_called()
        memory._process_evicted_long_term_memories.assert_not_called()
    finally:
        db.close()


def test_sync_add_reserves_oldest_qa_without_deleting_it():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.add(_qa(1), user_id="u1", run_id="r1")
        memory.add(_qa(2), user_id="u1", run_id="r1")

        result = memory.add(_qa(3), user_id="u1", run_id="r1", infer=True)
        job_id = result["background"]["migration_job_id"]

        assert job_id
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2", "u3", "a3"]
        assert _contents(db.get_migration_job_messages(job_id)) == ["u1", "a1"]
        assert {item["status"] for item in db.get_migration_job_messages(job_id)} == {"pending"}
        memory._process_midterm_evictions.assert_not_called()
        memory._process_evicted_long_term_memories.assert_not_called()
    finally:
        db.close()


def test_split_eviction_reserves_complete_qa_pair():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.add([*_qa(1), *_qa(2)], user_id="u1", run_id="r1", infer=False)

        result = memory.add([{"role": "user", "content": "u3"}], user_id="u1", run_id="r1", infer=False)
        job_id = result["background"]["migration_job_id"]

        assert _contents(db.get_migration_job_messages(job_id)) == ["u1", "a1"]
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2", "u3"]
    finally:
        db.close()


def test_pending_messages_do_not_consume_active_capacity():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2)

        memory.add(_qa(1), user_id="u1", run_id="r1")
        first = memory.add(_qa(2), user_id="u1", run_id="r1")
        second = memory.add(_qa(3), user_id="u1", run_id="r1")

        assert first["background"]["migration_job_id"]
        assert second["background"]["migration_job_id"]
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u3", "a3"]
    finally:
        db.close()


def test_odd_short_term_capacity_is_normalized_to_even():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=3)
        memory.add([*_qa(1), *_qa(2)], user_id="u1", run_id="r1", infer=False)

        assert memory.config.midterm.short_term_capacity == 4
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u1", "a1", "u2", "a2"]
    finally:
        db.close()


def test_midterm_disabled_still_creates_longterm_migration_job():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2, midterm_enabled=False)
        memory.add(_qa(1), user_id="u1", run_id="r1", infer=False)
        result = memory.add(_qa(2), user_id="u1", run_id="r1", infer=False)

        job = db.get_background_job(result["background"]["migration_job_id"])
        assert job["infer"] is False
        assert _contents(db.get_migration_job_messages(job["job_id"])) == ["u1", "a1"]
        assert memory._midterm_memory is None
    finally:
        db.close()


def test_infer_true_passes_declared_additive_prompt_inputs(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.vector_store = MagicMock()
        memory.vector_store.search.return_value = [SimpleNamespace(id="long-term-1", payload={"data": "现有长期记忆"})]
        memory._midterm_retriever = MagicMock()
        memory._midterm_retriever.search.return_value = [
            {"id": "session-internal-id", "source": "mid_term_session", "summary": "当前会话摘要"},
            {
                "id": "page-internal-id",
                "source": "mid_term_page",
                "summary": "相关中期记忆",
                "raw_dialogue": "User: 历史问题",
            },
        ]
        memory.llm = MagicMock()
        memory.llm.generate_response.return_value = '{"memory": []}'
        prompt_builder = MagicMock(return_value="extraction prompt")
        monkeypatch.setattr(memory_main, "generate_additive_extraction_prompt", prompt_builder)
        db.save_messages(_qa(2), "run_id=r1&user_id=u1", max_messages=4)

        result = Memory._process_evicted_long_term_memories(
            memory,
            _qa(1),
            {"user_id": "u1"},
            {"user_id": "u1", "run_id": "r1"},
            infer=True,
        )

        assert result == []
        prompt_kwargs = prompt_builder.call_args.kwargs
        assert _contents(prompt_kwargs["new_messages"]) == ["u1", "a1"]
        assert prompt_kwargs["session_summary"] == "当前会话摘要"
        assert prompt_kwargs["existing_long_term_memories"] == [{"id": "long-term-1", "text": "现有长期记忆"}]
        assert _contents(prompt_kwargs["short_term_context"]) == ["u2", "a2"]
    finally:
        db.close()


def test_longterm_payload_separates_user_trace_metadata_from_internal_trace():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, observability_enabled=True)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.vector_store = MagicMock()
        memory.vector_store.get.return_value = None
        memory._create_memory = MagicMock(return_value="memory-1")

        result = Memory._process_evicted_long_term_memories(
            memory,
            [{"role": "user", "content": "likes tea"}],
            {"user_id": "u1", "trace_id": "user-owned-trace"},
            {"user_id": "u1"},
            infer=False,
            trace_id="internal-trace",
        )

        assert result[0]["id"] == "memory-1"
        stored_metadata = memory._create_memory.call_args.args[2]
        assert stored_metadata["trace_id"] == "user-owned-trace"
        assert stored_metadata["_mem0_trace_id"] == "internal-trace"
    finally:
        db.close()


def test_procedural_add_keeps_existing_path_and_reports_no_migration_job():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db)
        memory._create_procedural_memory = MagicMock(return_value={"results": [{"id": "procedure-1"}]})

        result = memory.add(
            "step one, then step two",
            user_id="u1",
            agent_id="agent-1",
            memory_type=MemoryType.PROCEDURAL.value,
        )

        _assert_add_result(result, [{"id": "procedure-1"}])
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_add_matches_sync_queue_semantics():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=2)
        await memory.add(_qa(1), user_id="u1", run_id="r1")
        result = await memory.add(_qa(2), user_id="u1", run_id="r1")

        job_id = result["background"]["migration_job_id"]
        assert job_id
        assert _contents(db.get_migration_job_messages(job_id)) == ["u1", "a1"]
        memory._process_evicted_long_term_memories.assert_not_awaited()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_async_adds_keep_session_jobs_isolated():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=2)
        await memory.add(_qa(1), user_id="user-a", run_id="session-a")
        await memory.add(_qa(1), user_id="user-b", run_id="session-b")

        result_a, result_b = await asyncio.gather(
            memory.add(_qa(2), user_id="user-a", run_id="session-a"),
            memory.add(_qa(2), user_id="user-b", run_id="session-b"),
        )

        messages_a = db.get_migration_job_messages(result_a["background"]["migration_job_id"])
        messages_b = db.get_migration_job_messages(result_b["background"]["migration_job_id"])
        assert _contents(messages_a) == ["u1", "a1"]
        assert _contents(messages_b) == ["u1", "a1"]
        assert {item["session_scope"] for item in messages_a} == {"run_id=session-a&user_id=user-a"}
        assert {item["session_scope"] for item in messages_b} == {"run_id=session-b&user_id=user-b"}
    finally:
        db.close()


def test_background_disabled_uses_sync_layered_and_profile_path():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2, background_enabled=False, profile_enabled=True)
        memory._process_evicted_long_term_memories.return_value = [{"id": "longterm-1", "event": "ADD"}]
        memory.add(_qa(1), user_id="u1", run_id="r1")
        memory._process_midterm_evictions.reset_mock()
        memory._process_evicted_long_term_memories.reset_mock()
        memory._update_profile_after_add.reset_mock()

        result = memory.add(_qa(2), user_id="u1", run_id="r1", infer=False)

        _assert_add_result(result, [{"id": "longterm-1", "event": "ADD"}])
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
        assert memory._process_midterm_evictions.call_args.args[0][0]["content"] == "u1"
        memory._process_evicted_long_term_memories.assert_called_once()
        memory._update_profile_after_add.assert_called_once()
        assert db.background_jobs_pending() is False
    finally:
        db.close()


def test_disabled_observability_preserves_metadata_and_does_not_propagate_trace():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=0, background_enabled=False, profile_enabled=True)
        metadata = {"trace_id": "user-owned-trace", "topic": "tea"}

        result = memory.add(_qa(1), user_id="u1", metadata=metadata, infer=False)

        _assert_add_result(result, [])
        assert metadata == {"trace_id": "user-owned-trace", "topic": "tea"}
        longterm_call = memory._process_evicted_long_term_memories.call_args
        assert longterm_call.args[1]["trace_id"] == "user-owned-trace"
        assert "_mem0_trace_id" not in longterm_call.args[1]
        assert longterm_call.kwargs["trace_id"] is None
        assert "trace_id" not in memory._process_midterm_evictions.call_args.args[0][0]
        assert memory._update_profile_after_add.call_args.kwargs["trace_id"] is None
    finally:
        db.close()


def test_enabled_observability_keeps_user_metadata_separate_from_internal_trace():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(
            db,
            capacity=0,
            background_enabled=False,
            profile_enabled=True,
            observability_enabled=True,
        )
        metadata = {"trace_id": "user-owned-trace", "topic": "tea"}

        result = memory.add(_qa(1), user_id="u1", metadata=metadata, infer=False)

        trace_id = result["observability"]["trace_id"]
        _assert_observed_add_result(result, [])
        assert metadata == {"trace_id": "user-owned-trace", "topic": "tea"}
        longterm_call = memory._process_evicted_long_term_memories.call_args
        assert longterm_call.args[1]["trace_id"] == "user-owned-trace"
        assert "_mem0_trace_id" not in longterm_call.args[1]
        assert longterm_call.kwargs["trace_id"] == trace_id
        assert memory._process_midterm_evictions.call_args.args[0][0]["trace_id"] == trace_id
        assert memory._update_profile_after_add.call_args.kwargs["trace_id"] == trace_id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_background_disabled_matches_sync_fallback():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=2, background_enabled=False, profile_enabled=True)
        memory._process_evicted_long_term_memories.return_value = [{"id": "longterm-1", "event": "ADD"}]
        await memory.add(_qa(1), user_id="u1", run_id="r1")
        memory._process_midterm_evictions.reset_mock()
        memory._process_evicted_long_term_memories.reset_mock()
        memory._update_profile_after_add.reset_mock()

        result = await memory.add(_qa(2), user_id="u1", run_id="r1", infer=False)

        _assert_add_result(result, [{"id": "longterm-1", "event": "ADD"}])
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
        assert memory._process_midterm_evictions.call_args.args[0][0]["content"] == "u1"
        memory._process_evicted_long_term_memories.assert_awaited_once()
        memory._update_profile_after_add.assert_awaited_once()
        assert db.background_jobs_pending() is False
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_enabled_observability_matches_sync_trace_and_response_shape(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(memory_main.asyncio, "to_thread", run_inline)
        memory = _async_memory(
            db,
            capacity=0,
            background_enabled=False,
            observability_enabled=True,
        )
        metadata = {"trace_id": "user-owned-trace"}

        result = await memory.add(_qa(1), user_id="u1", metadata=metadata, infer=False)

        trace_id = result["observability"]["trace_id"]
        _assert_observed_add_result(result, [])
        assert metadata == {"trace_id": "user-owned-trace"}
        longterm_call = memory._process_evicted_long_term_memories.call_args
        assert longterm_call.args[1]["trace_id"] == "user-owned-trace"
        assert longterm_call.kwargs["trace_id"] == trace_id
        assert memory._process_midterm_evictions.call_args.args[0][0]["trace_id"] == trace_id
    finally:
        db.close()


def test_profile_enqueue_failure_does_not_fail_add_or_duplicate_short_term(caplog):
    database = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(database, capacity=0, profile_enabled=True)
        database.create_profile_update_job = MagicMock(side_effect=RuntimeError("profile queue unavailable"))

        with caplog.at_level("ERROR"):
            result = memory.add(_qa(1), user_id="u1", run_id="r1")

        migration_job_id = result["background"]["migration_job_id"]
        assert migration_job_id
        assert result["background"]["profile_job_id"] is None
        assert _contents(database.get_migration_job_messages(migration_job_id)) == ["u1", "a1"]
        assert database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        memory._background_worker.wake_migration.assert_called_once()
        memory._background_worker.wake_profile.assert_not_called()
        assert "Failed to enqueue profile update job for user_id=u1" in caplog.text
    finally:
        database.close()


@pytest.mark.asyncio
async def test_async_profile_enqueue_failure_is_isolated(caplog):
    database = SQLiteManager(":memory:")
    try:
        memory = _async_memory(database, capacity=0, profile_enabled=True)
        database.create_profile_update_job = MagicMock(side_effect=RuntimeError("profile queue unavailable"))

        with caplog.at_level("ERROR"):
            result = await memory.add(_qa(1), user_id="u1", run_id="r1")

        migration_job_id = result["background"]["migration_job_id"]
        assert migration_job_id
        assert result["background"]["profile_job_id"] is None
        assert _contents(database.get_migration_job_messages(migration_job_id)) == ["u1", "a1"]
        assert database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        memory._background_worker.wake_migration.assert_called_once()
        memory._background_worker.wake_profile.assert_not_called()
        assert "Failed to enqueue profile update job for user_id=u1" in caplog.text
    finally:
        database.close()
