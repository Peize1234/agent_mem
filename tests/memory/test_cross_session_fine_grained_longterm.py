from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.memory.main import AsyncMemory, Memory, _filter_shortterm_duplicate_longterm


def _point(memory_id, score, run_id, *, user_id="u1", turn_index=1):
    return SimpleNamespace(
        id=memory_id,
        score=score,
        payload={
            "data": memory_id,
            "user_id": user_id,
            "run_id": run_id,
            "source_turn_index": turn_index,
        },
    )


class _DualRouteStore:
    def __init__(self, current, historical):
        self.current = current
        self.historical = historical
        self.search_calls = []
        self.keyword_calls = []

    def search(self, **kwargs):
        self.search_calls.append(kwargs)
        if kwargs["filters"].get("run_id"):
            pool = self.current
        else:
            pool = [*self.historical, *self.current]
        return [
            point
            for point in pool
            if all(point.payload.get(key) == value for key, value in kwargs["filters"].items())
        ]

    def keyword_search(self, **kwargs):
        self.keyword_calls.append(kwargs)
        return []


def _memory(current, historical, *, other_weight=0.7):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        longterm_candidate_pool_multiplier=4,
        longterm_other_session_weight=other_weight,
        entity_similarity_threshold=0.5,
    )
    memory.vector_store = _DualRouteStore(current, historical)
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2]
    memory._run_entity_extraction = MagicMock(return_value=[])
    memory._bm25_language = None
    return memory


def test_cross_session_dual_route_returns_current_and_historical_for_same_user():
    memory = _memory([_point("s2", 0.5, "s2")], [_point("s1", 0.7, "s1")])

    results = memory._search_vector_store(
        "query",
        {"user_id": "u1", "run_id": "s2"},
        limit=10,
        threshold=0.1,
        explain=True,
    )

    assert {row["id"] for row in results} == {"s1", "s2"}
    assert [call["filters"] for call in memory.vector_store.search_calls] == [
        {"user_id": "u1", "run_id": "s2"},
        {"user_id": "u1"},
    ]
    assert all(call["filters"].get("user_id") == "u1" for call in memory.vector_store.search_calls)
    details = {row["id"]: row["score_details"] for row in results}
    assert details["s2"]["session_weight"] == 1.0
    assert details["s1"]["session_weight"] == 0.7


def test_cross_session_longterm_never_crosses_user_boundary():
    memory = _memory(
        [_point("current", 0.5, "s2")],
        [
            _point("same-user-history", 0.7, "s1"),
            _point("other-user-history", 0.99, "s1", user_id="u2"),
        ],
    )

    results = memory._search_vector_store(
        "query",
        {"user_id": "u1", "run_id": "s2"},
        limit=10,
        threshold=0.1,
    )

    assert {row["id"] for row in results} == {"current", "same-user-history"}


@pytest.mark.parametrize(
    ("current_score", "historical_score", "expected_first"),
    [(0.5, 0.7, "current"), (0.4, 0.9, "historical")],
)
def test_session_weight_is_applied_before_final_top_k(current_score, historical_score, expected_first):
    memory = _memory(
        [_point("current", current_score, "s2")],
        [_point("historical", historical_score, "s1")],
    )

    results = memory._search_vector_store(
        "query",
        {"user_id": "u1", "run_id": "s2"},
        limit=1,
        threshold=0.1,
        explain=True,
    )

    assert [row["id"] for row in results] == [expected_first]


def test_dual_route_protects_current_session_candidate_from_all_session_pool_pressure():
    historical = [_point(f"history-{index}", 0.99 - index / 1000, "s1") for index in range(80)]
    memory = _memory([_point("current", 0.8, "s2")], historical)

    results = memory._search_vector_store(
        "query",
        {"user_id": "u1", "run_id": "s2"},
        limit=30,
        threshold=0.1,
    )

    assert any(row["id"] == "current" for row in results)


def test_entity_boost_search_is_user_scoped_not_run_scoped():
    memory = _memory([], [])
    memory.embedding_model.embed_batch.return_value = [[0.1, 0.2]]
    memory._entity_store = MagicMock()
    memory._entity_store.search.return_value = [
        SimpleNamespace(score=0.9, payload={"linked_memory_ids": ["historical-memory"]})
    ]

    boosts = memory._compute_entity_boosts(
        [("organization", "Alpha")],
        {"user_id": "u1", "agent_id": "a1", "run_id": "s2"},
    )

    assert boosts["historical-memory"] > 0
    assert memory._entity_store.search.call_args.kwargs["filters"] == {"user_id": "u1", "agent_id": "a1"}


def test_shortterm_duplicate_filter_only_removes_same_run_same_turn():
    context = {
        "session_id": "s2",
        "short_term_messages": [
            {"role": "user", "content": "Q1", "turn_index": 1},
            {"role": "assistant", "content": "A1", "turn_index": 1},
        ],
        "retrieved_memories": [
            {"id": "same", "source": "long_term", "run_id": "s2", "source_turn_index": 1},
            {"id": "other", "source": "long_term", "run_id": "s1", "source_turn_index": 1},
            {"id": "older", "source": "long_term", "run_id": "s2", "source_turn_index": 2},
        ],
    }

    _filter_shortterm_duplicate_longterm(context)
    assert [row["id"] for row in context["retrieved_memories"]] == ["other", "older"]

    context["short_term_messages"] = []
    context["retrieved_memories"].append(
        {"id": "same", "source": "long_term", "run_id": "s2", "source_turn_index": 1}
    )
    _filter_shortterm_duplicate_longterm(context)
    assert [row["id"] for row in context["retrieved_memories"]] == ["other", "older", "same"]


@pytest.mark.asyncio
async def test_cross_session_longterm_sync_async_parity():
    current = [_point("current", 0.5, "s2")]
    historical = [_point("historical", 0.7, "s1")]
    sync_memory = _memory(current, historical)
    async_memory = AsyncMemory.__new__(AsyncMemory)
    async_memory.config = sync_memory.config
    async_memory.vector_store = _DualRouteStore(current, historical)
    async_memory.embedding_model = sync_memory.embedding_model
    async_memory._run_entity_extraction = MagicMock(return_value=[])
    async_memory._bm25_language = None
    request = {
        "query": "query",
        "filters": {"user_id": "u1", "run_id": "s2"},
        "limit": 10,
        "threshold": 0.1,
        "explain": True,
    }

    sync_results = sync_memory._search_vector_store(**request)
    async_results = await async_memory._search_vector_store(**request)

    assert async_results == sync_results
