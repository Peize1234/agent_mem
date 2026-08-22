import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT
from mem0.memory.query_resolver import (
    QueryResolver,
    build_query_resolution_messages,
    parse_resolved_query,
)
from mem0.memory.main import Memory


HISTORY = [
    {"role": "user", "content": "Compare Alpha Fund and Beta Fund."},
    {"role": "assistant", "content": "Alpha has lower volatility."},
]


def test_no_visible_history_returns_original_without_calling_llm():
    llm = MagicMock()

    assert QueryResolver(llm).resolve("What is the fee?", []) == "What is the fee?"
    llm.generate_response.assert_not_called()


def test_independent_query_is_preserved_by_production_contract():
    llm = MagicMock()
    llm.generate_response.return_value = '{"resolved_query":"What is the fee?"}'

    assert QueryResolver(llm).resolve("What is the fee?", HISTORY) == "What is the fee?"


def test_explicit_reference_can_be_resolved_conservatively():
    llm = MagicMock()
    llm.generate_response.return_value = '{"resolved_query":"What is Alpha Fund’s fee?"}'

    assert QueryResolver(llm).resolve("What is its fee?", HISTORY) == "What is Alpha Fund’s fee?"
    request = llm.generate_response.call_args.kwargs["messages"]
    assert request[0]["content"] == QUERY_REFERENCE_RESOLUTION_PROMPT
    assert json.loads(request[1]["content"]) == {
        "current_query": "What is its fee?",
        "recent_history": HISTORY,
    }


@pytest.mark.parametrize(
    "response",
    [
        RuntimeError("LLM unavailable"),
        "not-json",
        '{"resolved_query":""}',
        {"resolved_query": "x" * 2001},
    ],
)
def test_failures_invalid_json_empty_and_overlong_results_fall_back(response):
    llm = MagicMock()
    llm.generate_response.side_effect = response if isinstance(response, Exception) else None
    llm.generate_response.return_value = None if isinstance(response, Exception) else response

    assert QueryResolver(llm).resolve("What is its fee?", HISTORY) == "What is its fee?"


def test_shared_builder_and_parser_define_one_protocol():
    messages = build_query_resolution_messages("What about it?", HISTORY)

    assert messages[0] == {"role": "system", "content": QUERY_REFERENCE_RESOLUTION_PROMPT}
    assert parse_resolved_query('{"resolved_query":"What about Alpha Fund?"}', "What about it?") == (
        "What about Alpha Fund?"
    )


@pytest.mark.asyncio
async def test_sync_async_resolution_parity():
    response = '{"resolved_query":"What is Alpha Fund’s fee?"}'
    sync_llm = MagicMock()
    sync_llm.generate_response.return_value = response
    async_llm = MagicMock()
    async_llm.generate_response_async = AsyncMock(return_value=response)

    sync_value = QueryResolver(sync_llm).resolve("What is its fee?", HISTORY)
    async_value = await QueryResolver(async_llm).resolve_async("What is its fee?", HISTORY)

    assert async_value == sync_value
    async_llm.generate_response_async.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_sync_llm_fallback_does_not_block_event_loop(monkeypatch):
    llm = MagicMock()
    llm.generate_response.return_value = '{"resolved_query":"resolved"}'
    to_thread = AsyncMock(return_value='{"resolved_query":"resolved"}')
    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    assert await QueryResolver(llm).resolve_async("original", HISTORY) == "resolved"
    to_thread.assert_awaited_once()


def test_generic_memory_search_does_not_invoke_query_resolver(monkeypatch):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(longterm_rag_threshold=0.1)
    memory.api_version = "v1.1"
    memory.reranker = None
    memory._search_vector_store = MagicMock(return_value=[])
    memory._with_midterm_search_results = MagicMock(return_value=[])
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *args, **kwargs: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "mem0.memory.query_resolver.QueryResolver.resolve",
        MagicMock(side_effect=AssertionError("generic search must not rewrite")),
    )

    result = memory.search("exact generic query", filters={"user_id": "u1", "run_id": "r1"})

    assert result == {"results": []}
    memory._search_vector_store.assert_called_once()
    assert memory._search_vector_store.call_args.args[0] == "exact generic query"
