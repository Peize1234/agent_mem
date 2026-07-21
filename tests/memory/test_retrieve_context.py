from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import UserProfileConfig
from mem0.memory.main import Memory
from mem0.memory.storage import SQLiteManager


TOP_LEVEL_FIELDS = {
    "user_id",
    "session_id",
    "query",
    "profile",
    "short_term_messages",
    "retrieved_memories",
}


def _build_memory(*, search_result=None, profile_result=None, messages=None, short_term_capacity=10):
    memory = Memory.__new__(Memory)
    memory.search = MagicMock(return_value=search_result if search_result is not None else {"results": []})
    memory.get_profile = MagicMock(
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


def test_retrieve_context_normalizes_scope_and_forwards_search_options():
    memories = [
        {"id": "memory-2", "memory": "second", "score": 0.8, "source": "midterm"},
        {"id": "memory-1", "memory": "first", "score": 0.7, "source": "longterm"},
    ]
    messages = [
        {
            "id": "internal-1",
            "role": "user",
            "content": "first question",
            "name": "Alice",
            "created_at": "2026-07-21T10:00:00+00:00",
        },
        {
            "id": "internal-2",
            "role": "assistant",
            "content": "first answer",
            "name": None,
            "created_at": "2026-07-21T10:01:00+00:00",
        },
    ]
    memory = _build_memory(search_result={"results": memories}, messages=messages)

    result = memory.retrieve_context(
        "  how should I invest?  ",
        user_id=" user-1 ",
        session_id=" session-1 ",
        top_k=7,
        threshold=0.35,
        rerank=True,
        explain=True,
        include_profile_metadata=True,
    )

    assert set(result) == TOP_LEVEL_FIELDS
    assert result["user_id"] == "user-1"
    assert result["session_id"] == "session-1"
    assert result["query"] == "how should I invest?"
    assert result["profile"] == {"risk_level": "balanced"}
    assert result["retrieved_memories"] is memories
    assert result["short_term_messages"] == [
        {"role": "user", "content": "first question", "name": "Alice"},
        {"role": "assistant", "content": "first answer"},
    ]
    memory.search.assert_called_once_with(
        "how should I invest?",
        top_k=7,
        threshold=0.35,
        rerank=True,
        explain=True,
        filters={"user_id": "user-1", "run_id": "session-1"},
    )
    memory.get_profile.assert_called_once_with("user-1", include_metadata=True)
    memory.db.get_last_messages.assert_called_once_with(
        "run_id=session-1&user_id=user-1",
        limit=10,
    )
    memory.db.get_messages.assert_not_called()
    memory._short_term_capacity.assert_called_once_with()
    assert memory._profile_updater is None
    assert memory.llm.mock_calls == []


@pytest.mark.parametrize(
    ("user_id", "session_id", "error"),
    [
        ("user one", "session-1", "Invalid user_id"),
        ("user-1", "session one", "Invalid session_id"),
        ("", "session-1", "Invalid user_id"),
        ("user-1", "  ", "Invalid session_id"),
    ],
)
def test_retrieve_context_rejects_invalid_identifiers(user_id, session_id, error):
    memory = _build_memory()

    with pytest.raises(ValueError, match=error):
        memory.retrieve_context("question", user_id=user_id, session_id=session_id)

    memory.search.assert_not_called()


@pytest.mark.parametrize("query", ["", "   ", None, 123])
def test_retrieve_context_rejects_invalid_query(query):
    memory = _build_memory()

    with pytest.raises(ValueError, match="Invalid query"):
        memory.retrieve_context(query, user_id="user-1", session_id="session-1")

    memory.search.assert_not_called()


@pytest.mark.parametrize(
    "search_result",
    [
        [{"id": "memory-1"}, {"id": "memory-2"}],
        {"results": [{"id": "memory-1"}, {"id": "memory-2"}]},
    ],
)
def test_retrieve_context_accepts_list_and_results_dict_without_reordering(search_result):
    memory = _build_memory(search_result=search_result)

    result = memory.retrieve_context("question", user_id="user-1", session_id="session-1")

    expected = search_result["results"] if isinstance(search_result, dict) else search_result
    assert result["retrieved_memories"] is expected
    assert [item["id"] for item in result["retrieved_memories"]] == ["memory-1", "memory-2"]


def test_retrieve_context_returns_latest_window_in_order_and_isolates_session():
    db = SQLiteManager(":memory:")
    try:
        db.upsert_user_profile_value("user-1", "risk_level", "balanced")
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
            [{"role": "user", "content": "other session"}],
            "run_id=session-2&user_id=user-1",
        )

        memory = Memory.__new__(Memory)
        memory.config = SimpleNamespace(
            profile=UserProfileConfig(),
            midterm=SimpleNamespace(short_term_capacity=4),
        )
        memory.db = db
        memory.search = MagicMock(return_value={"results": []})
        memory._profile_manager = None
        memory._profile_updater = None

        session_1 = memory.retrieve_context("question", user_id="user-1", session_id="session-1")
        session_2 = memory.retrieve_context("question", user_id="user-1", session_id="session-2")

        assert session_1["profile"] == session_2["profile"] == {"risk_level": "balanced"}
        assert [message["content"] for message in session_1["short_term_messages"]] == [
            "message-9",
            "message-10",
            "message-11",
            "message-12",
        ]
        assert session_2["short_term_messages"] == [{"role": "user", "content": "other session"}]
        assert memory._profile_updater is None
    finally:
        db.close()


def test_retrieve_context_uses_configured_short_term_capacity():
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
        memory = Memory.__new__(Memory)
        memory.config = SimpleNamespace(midterm=SimpleNamespace(short_term_capacity=6))
        memory.db = db
        memory.search = MagicMock(return_value={"results": []})
        memory.get_profile = MagicMock(return_value={"user_id": "user-1", "profile": {}})

        result = memory.retrieve_context("question", user_id="user-1", session_id="session-1")

        assert [message["content"] for message in result["short_term_messages"]] == [
            "message-7",
            "message-8",
            "message-9",
            "message-10",
            "message-11",
            "message-12",
        ]
    finally:
        db.close()
