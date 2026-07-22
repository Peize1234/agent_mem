from unittest.mock import MagicMock, patch

import pytest

from mem0.memory.main import AsyncMemory, Memory
from mem0.utils.entity_extraction import extract_entities
from mem0.utils.lemmatization import lemmatize_for_bm25


def _memory_shell(memory_type):
    memory = memory_type.__new__(memory_type)
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory.embedding_model.embed_batch.return_value = [[0.1, 0.2]]
    memory.vector_store = MagicMock()
    memory.vector_store.search.return_value = []
    memory.vector_store.keyword_search.return_value = []
    memory.db = MagicMock()
    memory._entity_store = MagicMock()
    memory._entity_store.list.return_value = []
    memory._entity_store.search.return_value = []
    return memory


def _inserted_entity_payload(memory):
    payloads = memory._entity_store.insert.call_args.kwargs["payloads"]
    assert len(payloads) == 1
    return payloads[0]


async def _run_inline(function, *args, **kwargs):
    """Deterministic stand-in for asyncio.to_thread in unit tests."""
    return function(*args, **kwargs)


def test_sync_entity_store_persists_entity_not_long_term_memory():
    memory = _memory_shell(Memory)
    text = "用户长期关注华夏沪深300ETF。"

    with patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None):
        memory._link_entities_for_memory("memory-1", text, {"user_id": "user-1"})

    assert _inserted_entity_payload(memory) == {
        "data": "华夏沪深300ETF",
        "entity_type": "FINANCIAL_PRODUCT",
        "linked_memory_ids": ["memory-1"],
        "user_id": "user-1",
    }


@pytest.mark.asyncio
async def test_async_entity_store_matches_sync_behavior():
    sync_memory = _memory_shell(Memory)
    async_memory = _memory_shell(AsyncMemory)
    text = "用户长期关注华夏沪深300ETF。"

    with (
        patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None),
        patch("mem0.memory.main.asyncio.to_thread", new=_run_inline),
    ):
        sync_memory._link_entities_for_memory("memory-1", text, {"run_id": "run-1"})
        await async_memory._link_entities_for_memory("memory-1", text, {"run_id": "run-1"})

    assert _inserted_entity_payload(async_memory) == _inserted_entity_payload(sync_memory)


def test_write_and_query_paths_share_entity_extractor():
    memory = _memory_shell(Memory)
    text = "用户长期关注华夏沪深300ETF。"

    with (
        patch("mem0.utils.spacy_models.get_nlp_chinese", return_value=None),
        patch("mem0.memory.main.extract_entities", wraps=extract_entities) as extractor,
    ):
        memory._link_entities_for_memory("memory-1", text, {"user_id": "user-1"})
        memory._compute_entity_boosts = MagicMock(return_value={})
        memory._search_vector_store(text, {"user_id": "user-1"}, limit=5)

    assert extractor.call_args_list[0].args == (text,)
    assert extractor.call_args_list[1].args == (text,)
    assert memory._compute_entity_boosts.call_args.args[0] == [("FINANCIAL_PRODUCT", "华夏沪深300ETF")]


def test_sync_document_and_query_use_same_bm25_preprocessor():
    memory = _memory_shell(Memory)
    text = "华夏沪深300ETF适合长期持有吗"

    with (
        patch("mem0.memory.main.lemmatize_for_bm25", wraps=lemmatize_for_bm25) as preprocess,
        patch("mem0.memory.main.extract_entities", return_value=[]),
    ):
        memory._create_memory(text, {text: [0.1, 0.2]}, {"user_id": "user-1"})
        memory._search_vector_store(text, {"user_id": "user-1"}, limit=5)

    payload = memory.vector_store.insert.call_args.kwargs["payloads"][0]
    keyword_query = memory.vector_store.keyword_search.call_args.kwargs["query"]
    assert preprocess.call_count == 2
    assert payload["text_lemmatized"] == keyword_query == lemmatize_for_bm25(text)


@pytest.mark.asyncio
async def test_async_document_and_query_use_same_bm25_preprocessor():
    memory = _memory_shell(AsyncMemory)
    text = "华夏沪深300ETF适合长期持有吗"

    with (
        patch("mem0.memory.main.lemmatize_for_bm25", wraps=lemmatize_for_bm25) as preprocess,
        patch("mem0.memory.main.extract_entities", return_value=[]),
        patch("mem0.memory.main.asyncio.to_thread", new=_run_inline),
    ):
        await memory._create_memory(text, {text: [0.1, 0.2]}, {"user_id": "user-1"})
        await memory._search_vector_store(text, {"user_id": "user-1"}, limit=5)

    payload = memory.vector_store.insert.call_args.kwargs["payloads"][0]
    keyword_query = memory.vector_store.keyword_search.call_args.kwargs["query"]
    assert preprocess.call_count == 2
    assert payload["text_lemmatized"] == keyword_query == lemmatize_for_bm25(text)
