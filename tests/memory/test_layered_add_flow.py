import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig
from mem0.configs.enums import MemoryType
from mem0.memory import main as memory_main
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.storage import SQLiteManager


def _layered_config(*, capacity=4, midterm_enabled=True, background_enabled=True, profile_enabled=False):
    return SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=midterm_enabled, short_term_capacity=capacity),
        profile=SimpleNamespace(enabled=profile_enabled, update_on_add=profile_enabled),
        background=BackgroundTaskConfig(enabled=background_enabled),
    )


def _sync_memory(
    db,
    *,
    capacity=4,
    midterm_enabled=True,
    background_enabled=True,
    profile_enabled=False,
):
    memory = Memory.__new__(Memory)
    memory.config = _layered_config(
        capacity=capacity,
        midterm_enabled=midterm_enabled,
        background_enabled=background_enabled,
        profile_enabled=profile_enabled,
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
    memory._background_lifecycle_lock = threading.RLock()
    memory._closed = False
    return memory


def _async_memory(
    db,
    *,
    capacity=4,
    midterm_enabled=True,
    background_enabled=True,
    profile_enabled=False,
):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = _layered_config(
        capacity=capacity,
        midterm_enabled=midterm_enabled,
        background_enabled=background_enabled,
        profile_enabled=profile_enabled,
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
    memory._background_lifecycle_lock = threading.RLock()
    memory._closed = False
    return memory


def _qa(index):
    return [
        {"role": "user", "content": f"u{index}"},
        {"role": "assistant", "content": f"a{index}"},
    ]


def _contents(messages):
    return [message["content"] for message in messages]


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

        assert result == {
            "results": [],
            "background": {"migration_job_id": None, "profile_job_id": None},
        }
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


def test_background_longterm_context_excludes_current_job_midterm_pages():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.config.midterm.max_total_pages = 4
        memory._midterm_retriever = MagicMock()
        memory._midterm_retriever.search.return_value = [
            {
                "id": "previous-page",
                "source": "mid_term_page",
                "summary": "previous batch",
                "source_job_id": "migration-previous",
            }
        ]

        session_summary, related = memory_main._additive_midterm_context(
            memory,
            "query",
            {"user_id": "u1", "run_id": "r1"},
            exclude_source_job_id="migration-current",
        )

        assert session_summary == ""
        assert [item["summary"] for item in related] == ["previous batch"]
        assert related[0]["source_job_id"] == "migration-previous"
        memory._midterm_retriever.search.assert_called_once_with(
            "query",
            {"user_id": "u1", "run_id": "r1"},
            exclude_source_job_id="migration-current",
        )
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

        assert result == {
            "results": [{"id": "procedure-1"}],
            "background": {"migration_job_id": None, "profile_job_id": None},
        }
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


def test_concurrent_sync_adds_create_ordered_jobs_without_sqlite_errors():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=0)
        barrier = threading.Barrier(5)
        results = []
        errors = []

        def add(index):
            try:
                barrier.wait(timeout=1)
                results.append(memory.add(_qa(index), user_id="u1", run_id="r1"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=add, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=2)

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert len(results) == 4
        rows = db.connection.execute(
            "SELECT sequence_no FROM memory_migration_jobs ORDER BY sequence_no"
        ).fetchall()
        assert rows == [(1,), (2,), (3,), (4,)]
        assert db.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 8
    finally:
        db.close()


def test_background_disabled_extracts_complete_qa_before_shortterm_overflow():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=100, background_enabled=False)
        memory._process_evicted_long_term_memories.return_value = [{"id": "longterm-1", "event": "ADD"}]

        result = memory.add(_qa(1), user_id="u1", run_id="r1")

        assert result["results"] == [{"id": "longterm-1", "event": "ADD"}]
        memory._process_midterm_evictions.assert_not_called()
        memory._process_evicted_long_term_memories.assert_called_once()
        assert _contents(memory._process_evicted_long_term_memories.call_args.args[0]) == ["u1", "a1"]
        metadata = memory._process_evicted_long_term_memories.call_args.args[1]
        assert metadata["run_id"] == "r1"
        assert metadata["source_turn_index"] == 1
    finally:
        db.close()


def test_background_disabled_waits_for_assistant_before_extracting_split_qa():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=100, background_enabled=False)

        memory.add([{"role": "user", "content": "Q1"}], user_id="u1", run_id="r1")
        memory._process_evicted_long_term_memories.assert_not_called()

        memory.add([{"role": "assistant", "content": "A1"}], user_id="u1", run_id="r1")

        memory._process_evicted_long_term_memories.assert_called_once()
        assert _contents(memory._process_evicted_long_term_memories.call_args.args[0]) == ["Q1", "A1"]
        assert memory._process_evicted_long_term_memories.call_args.args[1]["source_turn_index"] == 1
    finally:
        db.close()


def test_background_disabled_extracts_multiple_qa_in_turn_order_and_ignores_system_messages():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=100, background_enabled=False)
        messages = [
            {"role": "system", "content": "policy"},
            *_qa(1),
            {"role": "system", "content": "transition"},
            *_qa(2),
        ]

        memory.add(messages, user_id="u1", run_id="r1")

        assert memory._process_evicted_long_term_memories.call_count == 2
        calls = memory._process_evicted_long_term_memories.call_args_list
        assert [_contents(call.args[0]) for call in calls] == [["u1", "a1"], ["u2", "a2"]]
        assert [call.args[1]["source_turn_index"] for call in calls] == [1, 2]
        assert all("policy" not in _contents(call.args[0]) for call in calls)
        assert all("transition" not in _contents(call.args[0]) for call in calls)
    finally:
        db.close()


def test_background_disabled_shortterm_eviction_never_reextracts_a_completed_qa():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2, background_enabled=False)

        for index in range(1, 5):
            memory.add(_qa(index), user_id="u1", run_id="r1")

        calls = memory._process_evicted_long_term_memories.call_args_list
        assert [_contents(call.args[0]) for call in calls] == [
            ["u1", "a1"],
            ["u2", "a2"],
            ["u3", "a3"],
            ["u4", "a4"],
        ]
        assert [call.args[1]["source_turn_index"] for call in calls] == [1, 2, 3, 4]
        assert memory._process_midterm_evictions.call_count == 3
    finally:
        db.close()


def test_background_disabled_longterm_metadata_drives_shortterm_duplicate_filter():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2, background_enabled=False)
        memory.add(_qa(1), user_id="u1", run_id="r1")
        metadata = memory._process_evicted_long_term_memories.call_args.args[1]
        longterm = {
            "id": "longterm-q1",
            "source": "long_term",
            "memory": "fact from Q1",
            "metadata": metadata,
        }
        context = {
            "session_id": "r1",
            "short_term_messages": db.get_messages("run_id=r1&user_id=u1"),
            "retrieved_memories": [longterm],
        }

        memory_main._filter_shortterm_duplicate_longterm(context)
        assert context["retrieved_memories"] == []

        memory.add(_qa(2), user_id="u1", run_id="r1")
        context = {
            "session_id": "r1",
            "short_term_messages": db.get_messages("run_id=r1&user_id=u1"),
            "retrieved_memories": [longterm],
        }
        memory_main._filter_shortterm_duplicate_longterm(context)
        assert context["retrieved_memories"] == [longterm]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_background_disabled_sync_and_async_longterm_inputs_are_equivalent():
    sync_db = SQLiteManager(":memory:")
    async_db = SQLiteManager(":memory:")
    try:
        sync_memory = _sync_memory(sync_db, capacity=100, background_enabled=False)
        async_memory = _async_memory(async_db, capacity=100, background_enabled=False)
        extracted = [{"id": "longterm-1", "memory": "same-fact", "event": "ADD"}]
        sync_memory._process_evicted_long_term_memories.return_value = extracted
        async_memory._process_evicted_long_term_memories.return_value = extracted

        sync_result = sync_memory.add(_qa(1), user_id="u1", run_id="r1")
        async_result = await async_memory.add(_qa(1), user_id="u1", run_id="r1")

        assert sync_result["results"] == async_result["results"] == extracted
        sync_call = sync_memory._process_evicted_long_term_memories.call_args
        async_call = async_memory._process_evicted_long_term_memories.call_args
        assert _contents(sync_call.args[0]) == _contents(async_call.args[0]) == ["u1", "a1"]
        assert sync_call.args[1]["source_turn_index"] == async_call.args[1]["source_turn_index"] == 1
        assert sync_call.args[1]["run_id"] == async_call.args[1]["run_id"] == "r1"
        assert sync_call.args[2] == async_call.args[2] == {"user_id": "u1", "run_id": "r1"}
    finally:
        sync_db.close()
        async_db.close()


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

        assert result == {
            "results": [{"id": "longterm-1", "event": "ADD"}],
            "background": {"migration_job_id": None, "profile_job_id": None},
        }
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
        assert memory._process_midterm_evictions.call_args.args[0][0]["content"] == "u1"
        memory._process_evicted_long_term_memories.assert_called_once()
        assert _contents(memory._process_evicted_long_term_memories.call_args.args[0]) == ["u2", "a2"]
        assert memory._process_evicted_long_term_memories.call_args.args[1]["source_turn_index"] == 2
        assert memory._process_evicted_long_term_memories.call_args.args[1]["run_id"] == "r1"
        memory._update_profile_after_add.assert_called_once()
        assert db.background_jobs_pending() is False
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

        assert result == {
            "results": [{"id": "longterm-1", "event": "ADD"}],
            "background": {"migration_job_id": None, "profile_job_id": None},
        }
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
        assert memory._process_midterm_evictions.call_args.args[0][0]["content"] == "u1"
        memory._process_evicted_long_term_memories.assert_awaited_once()
        assert _contents(memory._process_evicted_long_term_memories.call_args.args[0]) == ["u2", "a2"]
        assert memory._process_evicted_long_term_memories.call_args.args[1]["source_turn_index"] == 2
        assert memory._process_evicted_long_term_memories.call_args.args[1]["run_id"] == "r1"
        memory._update_profile_after_add.assert_awaited_once()
        assert db.background_jobs_pending() is False
    finally:
        db.close()


def test_profile_enqueue_failure_rolls_back_add_and_raises(caplog):
    database = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(database, capacity=0, profile_enabled=True)
        database._create_profile_job_in_transaction = MagicMock(side_effect=RuntimeError("profile queue unavailable"))

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="profile queue unavailable"):
                memory.add(_qa(1), user_id="u1", run_id="r1")

        assert database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM memory_migration_jobs").fetchone()[0] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM profile_update_jobs").fetchone()[0] == 0
        memory._background_worker.wake_migration.assert_not_called()
        memory._background_worker.wake_profile.assert_not_called()
        assert "Failed to save messages and enqueue background jobs" in caplog.text
    finally:
        database.close()


@pytest.mark.asyncio
async def test_async_profile_enqueue_failure_rolls_back_add_and_raises(caplog):
    database = SQLiteManager(":memory:")
    try:
        memory = _async_memory(database, capacity=0, profile_enabled=True)
        database._create_profile_job_in_transaction = MagicMock(side_effect=RuntimeError("profile queue unavailable"))

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError, match="profile queue unavailable"):
                await memory.add(_qa(1), user_id="u1", run_id="r1")

        assert database.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM memory_migration_jobs").fetchone()[0] == 0
        assert database.connection.execute("SELECT COUNT(*) FROM profile_update_jobs").fetchone()[0] == 0
        memory._background_worker.wake_migration.assert_not_called()
        memory._background_worker.wake_profile.assert_not_called()
        assert "Failed to save messages and enqueue background jobs" in caplog.text
    finally:
        database.close()
