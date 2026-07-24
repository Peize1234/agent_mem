import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from mem0.configs.prompts import AGENT_ANSWER_PROMPT
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.storage import SQLiteManager


def _build_async_memory(*, search_result=None, profile_result=None, messages=None, short_term_capacity=10):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.search = AsyncMock(return_value=search_result if search_result is not None else {"results": []})
    memory.get_profile = AsyncMock(
        return_value=profile_result or {"user_id": "user-1", "profile": {"risk_level": "balanced"}}
    )
    memory.db = SimpleNamespace(
        get_last_messages=MagicMock(return_value=messages or []),
        get_messages=MagicMock(side_effect=AssertionError("retrieve_context must use get_last_messages")),
    )
    memory._short_term_capacity = MagicMock(return_value=short_term_capacity)
    memory.llm = MagicMock()
    memory._profile_updater = None
    return memory


def _build_sync_memory(*, search_result, profile_result, messages, short_term_capacity=10):
    memory = Memory.__new__(Memory)
    memory.search = MagicMock(return_value=search_result)
    memory.get_profile = MagicMock(return_value=profile_result)
    memory.db = SimpleNamespace(
        get_last_messages=MagicMock(return_value=messages),
        get_messages=MagicMock(side_effect=AssertionError("retrieve_context must use get_last_messages")),
    )
    memory._short_term_capacity = MagicMock(return_value=short_term_capacity)
    return memory


@pytest.mark.asyncio
async def test_async_retrieve_context_matches_sync_structure_and_uses_existing_apis(monkeypatch):
    search_result = {
        "results": [
            {"id": "memory-1", "memory": "first", "score": 0.9},
            {"id": "memory-2", "memory": "second", "score": 0.8},
        ]
    }
    profile_result = {"user_id": "user-1", "profile": {"risk_level": "balanced"}}
    messages = [
        {"id": "row-1", "role": "user", "content": "hello", "created_at": "2026-07-21T10:00:00"},
        {"id": "row-2", "role": "assistant", "content": "hi", "created_at": "2026-07-21T10:01:00"},
    ]
    sync_memory = _build_sync_memory(
        search_result=search_result,
        profile_result=profile_result,
        messages=messages,
    )
    async_memory = _build_async_memory(
        search_result=search_result,
        profile_result=profile_result,
        messages=messages,
    )
    real_to_thread = asyncio.to_thread

    async def run_in_thread(function, *args):
        return await real_to_thread(function, *args)

    to_thread = AsyncMock(side_effect=run_in_thread)
    monkeypatch.setattr("mem0.memory.main.asyncio.to_thread", to_thread)

    sync_result = sync_memory._retrieve_context(
        " question ",
        user_id=" user-1 ",
        session_id=" session-1 ",
        top_k=6,
        threshold=0.4,
        rerank=True,
        explain=True,
        include_profile_metadata=True,
    )
    async_result = await async_memory._retrieve_context(
        " question ",
        user_id=" user-1 ",
        session_id=" session-1 ",
        top_k=6,
        threshold=0.4,
        rerank=True,
        explain=True,
        include_profile_metadata=True,
    )

    assert async_result == sync_result
    async_memory.search.assert_awaited_once_with(
        "question",
        top_k=6,
        threshold=0.4,
        rerank=True,
        explain=True,
        filters={"user_id": "user-1", "run_id": "session-1"},
    )
    async_memory.get_profile.assert_awaited_once_with("user-1", include_metadata=True)
    to_thread.assert_awaited_once_with(
        async_memory.db.get_last_messages,
        "run_id=session-1&user_id=user-1",
        10,
    )
    async_memory.db.get_messages.assert_not_called()
    async_memory._short_term_capacity.assert_called_once_with()
    assert async_memory._profile_updater is None
    assert async_memory.llm.mock_calls == []


@pytest.mark.asyncio
async def test_async_build_agent_answer_messages_matches_sync_prompt_flow(monkeypatch):
    retrieved_context = {
        "user_id": "user-1",
        "session_id": "session-1",
        "query": "how should I invest?",
        "profile": {"risk_level": "balanced"},
        "short_term_messages": [
            {
                "role": "user",
                "content": "I need liquidity",
                "created_at": "2026-07-24T11:55:00+08:00",
            }
        ],
        "retrieved_memories": [
            {
                "id": "session-1",
                "score": 0.95,
                "source": "mid_term_session",
                "summary": "session summary must not enter the prompt",
                "created_at": "2026-07-20T09:00:00+08:00",
            },
            {
                "id": "mid-1",
                "score": 0.83,
                "source": "mid_term_page",
                "summary": "page summary must not enter the prompt",
                "raw_dialogue": "User: historical question\nAssistant: historical answer",
                "created_at": "2026-07-21T10:00:00+08:00",
                "updated_at": "2026-07-22T10:00:00+08:00",
            },
            {
                "id": "long-1",
                "score": 0.72,
                "source": "long_term",
                "memory": "prefers ETFs",
                "created_at": "2026-07-19T08:00:00+08:00",
                "metadata": {"internal": "must not enter the prompt"},
            },
        ],
    }
    reference_information = {"as_of": "2026-07-24", "market": "reference data"}
    memory = _build_async_memory()
    memory._retrieve_context = AsyncMock(return_value=retrieved_context)
    monkeypatch.setattr("mem0.memory.main.beijing_now_iso", lambda: "2026-07-24T12:00:00+08:00")

    result = await memory.build_agent_answer_messages(
        "  how should I invest?  ",
        user_id=" user-1 ",
        session_id=" session-1 ",
        top_k=7,
        threshold=0.35,
        rerank=True,
        explain=True,
        include_profile_metadata=True,
        reference_information=reference_information,
    )

    memory._retrieve_context.assert_awaited_once_with(
        "  how should I invest?  ",
        user_id=" user-1 ",
        session_id=" session-1 ",
        top_k=7,
        threshold=0.35,
        rerank=True,
        explain=True,
        include_profile_metadata=True,
    )
    expected_prompt = AGENT_ANSWER_PROMPT.format(
        current_time="2026-07-24T12:00:00+08:00",
        user_query="how should I invest?",
        short_term_memory=json.dumps(retrieved_context["short_term_messages"], ensure_ascii=False),
        mid_term_memory=json.dumps(
            [
                {
                    "score": 0.83,
                    "created_at": "2026-07-21T10:00:00+08:00",
                    "content": "User: historical question\nAssistant: historical answer",
                }
            ],
            ensure_ascii=False,
        ),
        long_term_memory=json.dumps(
            [
                {
                    "score": 0.72,
                    "created_at": "2026-07-19T08:00:00+08:00",
                    "content": "prefers ETFs",
                }
            ],
            ensure_ascii=False,
        ),
        user_profile=json.dumps(retrieved_context["profile"], ensure_ascii=False),
        reference_information=json.dumps(reference_information, ensure_ascii=False),
    )
    assert result == [{"role": "system", "content": expected_prompt}]
    memory.llm.generate_response.assert_not_called()


@pytest.mark.asyncio
async def test_async_retrieve_context_normalizes_ids_and_rejects_internal_whitespace():
    memory = _build_async_memory()

    result = await memory._retrieve_context(
        " question ",
        user_id=" user-1 ",
        session_id=" session-1 ",
    )

    assert (result["user_id"], result["session_id"], result["query"]) == (
        "user-1",
        "session-1",
        "question",
    )

    with pytest.raises(ValueError, match="Invalid user_id"):
        await memory._retrieve_context("question", user_id="user one", session_id="session-1")
    with pytest.raises(ValueError, match="Invalid session_id"):
        await memory._retrieve_context("question", user_id="user-1", session_id="session one")


@pytest.mark.asyncio
async def test_async_retrieve_context_propagates_search_failure():
    memory = _build_async_memory()
    memory.search = AsyncMock(side_effect=RuntimeError("search failed"))

    with pytest.raises(RuntimeError, match="search failed"):
        await memory._retrieve_context("question", user_id="user-1", session_id="session-1")


@pytest.mark.asyncio
async def test_async_retrieve_context_allows_different_users_to_run_concurrently():
    memory = _build_async_memory()
    active_searches = 0
    maximum_active_searches = 0
    observed_filters = []

    async def search(query, **kwargs):
        nonlocal active_searches, maximum_active_searches
        observed_filters.append(kwargs["filters"])
        active_searches += 1
        maximum_active_searches = max(maximum_active_searches, active_searches)
        await asyncio.sleep(0.02)
        active_searches -= 1
        return {"results": [{"memory": query}]}

    memory.search = AsyncMock(side_effect=search)

    results = await asyncio.gather(
        memory._retrieve_context("first", user_id="user-1", session_id="session-1"),
        memory._retrieve_context("second", user_id="user-2", session_id="session-2"),
    )

    assert maximum_active_searches == 2
    assert observed_filters == [
        {"user_id": "user-1", "run_id": "session-1"},
        {"user_id": "user-2", "run_id": "session-2"},
    ]
    assert [result["user_id"] for result in results] == ["user-1", "user-2"]


@pytest.mark.asyncio
async def test_async_retrieve_context_does_not_mutate_search_result():
    search_result = {
        "results": [
            {"id": "memory-2", "memory": "second", "score": 0.8},
            {"id": "memory-1", "memory": "first", "score": 0.7},
        ],
        "relations": [{"source": "memory-2", "target": "memory-1"}],
    }
    original = deepcopy(search_result)
    memory = _build_async_memory(search_result=search_result)

    result = await memory._retrieve_context("question", user_id="user-1", session_id="session-1")

    assert search_result == original
    assert result["retrieved_memories"] is search_result["results"]
    assert [item["id"] for item in result["retrieved_memories"]] == ["memory-2", "memory-1"]


@pytest.mark.asyncio
async def test_async_retrieve_context_uses_latest_configured_session_window(monkeypatch):
    db = SQLiteManager(":memory:")
    try:
        db.save_messages(
            [
                {
                    "role": "user",
                    "content": f"message-{index}",
                    "created_at": f"2026-07-21T10:{index:02d}:00+00:00",
                }
                for index in range(1, 13)
            ],
            "run_id=session-1&user_id=user-1",
            max_messages=20,
        )
        db.save_messages(
            [
                {
                    "role": "user",
                    "content": "other session",
                    "created_at": "2026-07-21T11:00:00+00:00",
                }
            ],
            "run_id=session-2&user_id=user-1",
        )
        db.get_messages = MagicMock(side_effect=AssertionError("retrieve_context must use get_last_messages"))

        memory = AsyncMemory.__new__(AsyncMemory)
        memory.config = SimpleNamespace(midterm=SimpleNamespace(short_term_capacity=4))
        memory.db = db
        memory.search = AsyncMock(return_value={"results": []})
        memory.get_profile = AsyncMock(return_value={"user_id": "user-1", "profile": {}})
        memory.llm = MagicMock()
        memory._profile_updater = None

        real_to_thread = asyncio.to_thread

        async def run_in_thread(function, *args):
            return await real_to_thread(function, *args)

        to_thread = AsyncMock(side_effect=run_in_thread)
        monkeypatch.setattr("mem0.memory.main.asyncio.to_thread", to_thread)

        session_1 = await memory._retrieve_context("question", user_id="user-1", session_id="session-1")
        session_2 = await memory._retrieve_context("question", user_id="user-1", session_id="session-2")
        memory.config.midterm.short_term_capacity = 6
        expanded = await memory._retrieve_context("question", user_id="user-1", session_id="session-1")

        assert [message["content"] for message in session_1["short_term_messages"]] == [
            "message-9",
            "message-10",
            "message-11",
            "message-12",
        ]
        assert session_2["short_term_messages"] == [
            {
                "role": "user",
                "content": "other session",
                "created_at": "2026-07-21T19:00:00+08:00",
            }
        ]
        assert [message["content"] for message in expanded["short_term_messages"]] == [
            "message-7",
            "message-8",
            "message-9",
            "message-10",
            "message-11",
            "message-12",
        ]
        assert to_thread.await_args_list == [
            call(db.get_last_messages, "run_id=session-1&user_id=user-1", 4),
            call(db.get_last_messages, "run_id=session-2&user_id=user-1", 4),
            call(db.get_last_messages, "run_id=session-1&user_id=user-1", 6),
        ]
        db.get_messages.assert_not_called()
        assert memory._profile_updater is None
        assert memory.llm.mock_calls == []
    finally:
        db.close()
