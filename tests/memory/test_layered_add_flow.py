import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.enums import MemoryType
from mem0.memory import main as memory_main
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.storage import SQLiteManager


def _layered_config(*, capacity=4, midterm_enabled=True):
    return SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=midterm_enabled, short_term_capacity=capacity),
        profile=SimpleNamespace(enabled=False, update_on_add=False),
    )


def _sync_memory(db, *, capacity=4, midterm_enabled=True):
    memory = Memory.__new__(Memory)
    memory.config = _layered_config(capacity=capacity, midterm_enabled=midterm_enabled)
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._process_midterm_evictions = MagicMock()
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._update_profile_after_add = MagicMock()
    return memory


def _async_memory(db, *, capacity=4, midterm_enabled=True):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = _layered_config(capacity=capacity, midterm_enabled=midterm_enabled)
    memory.db = db
    memory.api_version = "v1.1"
    memory.custom_instructions = None
    memory._midterm_memory = None
    memory._midterm_updater = None
    memory._midterm_retriever = None
    memory._process_midterm_evictions = MagicMock()
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[])
    memory._update_profile_after_add = AsyncMock()
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


def test_sync_add_saves_short_term_once_and_skips_migration_without_eviction():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        original_save = db.save_messages
        db.save_messages = MagicMock(wraps=original_save)

        result = memory.add(_qa(1), user_id="u1", run_id="r1")

        assert result == {"results": []}
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u1", "a1"]
        db.save_messages.assert_called_once()
        memory._process_midterm_evictions.assert_not_called()
        memory._process_evicted_long_term_memories.assert_not_called()
        memory._update_profile_after_add.assert_called_once_with("u1", _qa(1))
    finally:
        db.close()


def test_sync_add_at_capacity_does_not_evict_then_migrates_only_oldest_qa_in_order():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        events = []
        original_save = db.save_messages

        def save_messages(*args, **kwargs):
            events.append("short")
            return original_save(*args, **kwargs)

        db.save_messages = MagicMock(side_effect=save_messages)
        memory._process_midterm_evictions.side_effect = lambda *args: events.append("midterm")
        memory._process_evicted_long_term_memories.side_effect = lambda *args, **kwargs: (
            events.append("longterm") or [{"id": "long-1"}]
        )
        memory._update_profile_after_add.side_effect = lambda *args: events.append("profile")

        assert memory.add(_qa(1), user_id="u1", run_id="r1") == {"results": []}
        assert memory.add(_qa(2), user_id="u1", run_id="r1") == {"results": []}
        memory._process_evicted_long_term_memories.assert_not_called()

        events.clear()
        result = memory.add(_qa(3), user_id="u1", run_id="r1", infer=True)

        assert result == {"results": [{"id": "long-1"}]}
        assert events == ["short", "midterm", "longterm", "profile"]
        evicted_messages = memory._process_midterm_evictions.call_args.args[0]
        assert _contents(evicted_messages) == ["u1", "a1"]
        assert memory._process_evicted_long_term_memories.call_args.args[0] == evicted_messages
        assert memory._process_evicted_long_term_memories.call_args.kwargs["infer"] is True
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2", "u3", "a3"]
        assert db.save_messages.call_count == 3
        assert memory._update_profile_after_add.call_count == 3
    finally:
        db.close()


def test_single_user_eviction_expands_assistant_without_deleting_next_user():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.add([*_qa(1), *_qa(2)], user_id="u1", run_id="r1", infer=False)

        memory.add([{"role": "user", "content": "u3"}], user_id="u1", run_id="r1", infer=False)

        evicted_messages = memory._process_evicted_long_term_memories.call_args.args[0]
        assert _contents(evicted_messages) == ["u1", "a1"]
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2", "u3"]

        memory.add([{"role": "assistant", "content": "a3"}], user_id="u1", run_id="r1", infer=False)
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2", "u3", "a3"]
        assert memory._process_evicted_long_term_memories.call_count == 1
    finally:
        db.close()


def test_odd_short_term_capacity_is_normalized_to_even():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=3)

        memory.add([*_qa(1), *_qa(2)], user_id="u1", run_id="r1", infer=False)

        assert memory.config.midterm.short_term_capacity == 4
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u1", "a1", "u2", "a2"]
        memory._process_evicted_long_term_memories.assert_not_called()
    finally:
        db.close()


def test_midterm_disabled_still_bounds_short_term_and_migrates_evictions_to_long_term():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2, midterm_enabled=False)
        memory._process_midterm_evictions = Memory._process_midterm_evictions.__get__(memory, Memory)

        memory.add(_qa(1), user_id="u1", run_id="r1", infer=False)
        memory.add(_qa(2), user_id="u1", run_id="r1", infer=False)

        assert _contents(memory._process_evicted_long_term_memories.call_args.args[0]) == ["u1", "a1"]
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
        assert memory._midterm_memory is None
        assert memory._midterm_updater is None
    finally:
        db.close()


def test_infer_false_directly_writes_only_evicted_messages_without_llm():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=2)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.llm = MagicMock()
        memory._create_memory = MagicMock(side_effect=["memory-u1", "memory-a1"])
        memory._process_evicted_long_term_memories = Memory._process_evicted_long_term_memories.__get__(memory, Memory)

        assert memory.add(_qa(1), user_id="u1", run_id="r1", infer=False) == {"results": []}
        result = memory.add(_qa(2), user_id="u1", run_id="r1", infer=False)

        assert [call.args[0] for call in memory._create_memory.call_args_list] == ["u1", "a1"]
        assert [item["memory"] for item in result["results"]] == ["u1", "a1"]
        memory.llm.generate_response.assert_not_called()
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u2", "a2"]
    finally:
        db.close()


def test_infer_true_passes_declared_additive_prompt_inputs(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db, capacity=4)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.vector_store = MagicMock()
        memory.vector_store.search.return_value = [
            SimpleNamespace(id="long-term-1", payload={"data": "现有长期记忆"})
        ]
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
        assert prompt_kwargs["existing_long_term_memories"] == [
            {"id": "long-term-1", "text": "现有长期记忆"}
        ]
        assert prompt_kwargs["existing_related_memories"] == [
            {"source": "mid_term_page", "summary": "相关中期记忆", "raw_dialogue": "User: 历史问题"}
        ]
        assert _contents(prompt_kwargs["short_term_context"]) == ["u2", "a2"]
        assert "subsequent_messages" not in prompt_kwargs
        memory.llm.generate_response.assert_called_once()
    finally:
        db.close()


def test_procedural_add_keeps_existing_path_and_skips_short_term_migration():
    db = SQLiteManager(":memory:")
    try:
        memory = _sync_memory(db)
        memory._create_procedural_memory = MagicMock(return_value={"results": [{"id": "procedure-1"}]})
        memory._save_short_term_messages = MagicMock()

        result = memory.add(
            "step one, then step two",
            user_id="u1",
            agent_id="agent-1",
            memory_type=MemoryType.PROCEDURAL.value,
        )

        assert result == {"results": [{"id": "procedure-1"}]}
        memory._save_short_term_messages.assert_not_called()
        memory._process_evicted_long_term_memories.assert_not_called()
        memory._update_profile_after_add.assert_called_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_add_matches_sync_no_eviction_semantics():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=4)

        result = await memory.add(_qa(1), user_id="u1", run_id="r1")

        assert result == {"results": []}
        assert _contents(db.get_messages("run_id=r1&user_id=u1")) == ["u1", "a1"]
        memory._process_midterm_evictions.assert_not_called()
        memory._process_evicted_long_term_memories.assert_not_awaited()
        memory._update_profile_after_add.assert_awaited_once_with("u1", _qa(1))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_short_term_save_uses_to_thread(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=2)
        calls = []

        async def to_thread(function, *args, **kwargs):
            calls.append((function, args, kwargs))
            return function(*args, **kwargs)

        monkeypatch.setattr(memory_main.asyncio, "to_thread", to_thread)

        evicted = await memory._save_short_term_messages(_qa(1), "scope")

        assert evicted == []
        assert calls[0][0].__self__ is db
        assert calls[0][0].__name__ == "save_messages"
        assert calls[0][1][2:] == (2, True)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_infer_true_passes_declared_additive_prompt_inputs(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=4)

        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

        monkeypatch.setattr(memory_main.asyncio, "to_thread", to_thread)
        memory.embedding_model = MagicMock()
        memory.embedding_model.embed.return_value = [0.1, 0.2]
        memory.vector_store = MagicMock()
        memory.vector_store.search.return_value = [
            SimpleNamespace(id="long-term-1", payload={"data": "现有长期记忆"})
        ]
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

        result = await AsyncMemory._process_evicted_long_term_memories(
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
        assert prompt_kwargs["existing_long_term_memories"] == [
            {"id": "long-term-1", "text": "现有长期记忆"}
        ]
        assert prompt_kwargs["existing_related_memories"] == [
            {"source": "mid_term_page", "summary": "相关中期记忆", "raw_dialogue": "User: 历史问题"}
        ]
        assert _contents(prompt_kwargs["short_term_context"]) == ["u2", "a2"]
        assert "subsequent_messages" not in prompt_kwargs
        memory.llm.generate_response.assert_called_once()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_concurrent_async_adds_keep_session_and_user_evictions_isolated():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=2)
        long_term_calls = []
        midterm_calls = []

        async def process_long_term(evicted_messages, metadata, filters, **kwargs):
            await asyncio.sleep(0)
            long_term_calls.append((filters["user_id"], filters["run_id"], _contents(evicted_messages)))
            return [{"id": f"{filters['user_id']}-long"}]

        memory._process_evicted_long_term_memories = AsyncMock(side_effect=process_long_term)
        memory._process_midterm_evictions.side_effect = lambda messages, filters: midterm_calls.append(
            (filters["user_id"], filters["run_id"], _contents(messages))
        )

        await memory.add(_qa(1), user_id="user-a", run_id="session-a")
        await memory.add(_qa(1), user_id="user-b", run_id="session-b")
        await asyncio.gather(
            memory.add(_qa(2), user_id="user-a", run_id="session-a"),
            memory.add(_qa(2), user_id="user-b", run_id="session-b"),
        )

        expected = {
            ("user-a", "session-a", ("u1", "a1")),
            ("user-b", "session-b", ("u1", "a1")),
        }
        assert {(user, run, tuple(messages)) for user, run, messages in long_term_calls} == expected
        assert {(user, run, tuple(messages)) for user, run, messages in midterm_calls} == expected
        assert _contents(db.get_messages("run_id=session-a&user_id=user-a")) == ["u2", "a2"]
        assert _contents(db.get_messages("run_id=session-b&user_id=user-b")) == ["u2", "a2"]
        legacy_eviction_attribute = "_last_" + "evicted_messages"
        assert not hasattr(memory, legacy_eviction_attribute)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_async_long_term_failure_does_not_pollute_next_add_evictions():
    db = SQLiteManager(":memory:")
    try:
        memory = _async_memory(db, capacity=0)
        seen_batches = []

        async def process_long_term(evicted_messages, metadata, filters, **kwargs):
            seen_batches.append(_contents(evicted_messages))
            if len(seen_batches) == 1:
                raise RuntimeError("long-term failed")
            return [{"id": "second-long"}]

        memory._process_evicted_long_term_memories = AsyncMock(side_effect=process_long_term)

        with pytest.raises(RuntimeError, match="long-term failed"):
            await memory.add([{"role": "user", "content": "first"}], user_id="u1", run_id="r1")
        result = await memory.add([{"role": "user", "content": "second"}], user_id="u2", run_id="r2")

        assert seen_batches == [["first"], ["second"]]
        assert result == {"results": [{"id": "second-long"}]}
    finally:
        db.close()
