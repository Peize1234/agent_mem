from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import BackgroundTaskConfig
from mem0.memory import main as memory_main
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.storage import SQLiteManager


def make_sync_memory():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=False, short_term_capacity=10),
        profile=SimpleNamespace(enabled=False, update_on_add=False),
        background=BackgroundTaskConfig(enabled=True),
    )
    memory.db = SQLiteManager(":memory:")
    memory.api_version = "v1.1"
    memory.reranker = None
    memory._save_short_term_messages = MagicMock(return_value=[])
    memory._save_and_enqueue_background_jobs = MagicMock(return_value=(None, None))
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._search_vector_store = MagicMock(return_value=[])
    return memory


def make_async_memory():
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(
        llm=SimpleNamespace(config={}),
        midterm=SimpleNamespace(enabled=False, short_term_capacity=10),
        profile=SimpleNamespace(enabled=False, update_on_add=False),
        background=BackgroundTaskConfig(enabled=True),
    )
    memory.db = SQLiteManager(":memory:")
    memory.api_version = "v1.1"
    memory.reranker = None
    memory._save_short_term_messages = AsyncMock(return_value=[])
    memory._save_and_enqueue_background_jobs = MagicMock(return_value=(None, None))
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[])
    memory._search_vector_store = AsyncMock(return_value=[])
    return memory


def test_sync_add_temporal_metadata_triggers_notice_after_success(monkeypatch):
    memory = make_sync_memory()
    temporal_notice = MagicMock()
    first_run_notice = MagicMock()
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", first_run_notice)

    result = Memory.add(
        memory,
        "The user visited Paris.",
        user_id="u1",
        metadata={"event_date": "2025-04-09"},
        infer=False,
    )

    assert result == {
        "results": [],
        "background": {"migration_job_id": None, "profile_job_id": None},
    }
    memory._process_evicted_long_term_memories.assert_not_called()
    temporal_notice.assert_called_once_with(memory, "sync", "add", "metadata", "date_like_metadata")
    first_run_notice.assert_not_called()
    memory.db.close()


def test_sync_add_non_temporal_metadata_uses_first_run_notice(monkeypatch):
    memory = make_sync_memory()
    temporal_notice = MagicMock()
    first_run_notice = MagicMock()
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", first_run_notice)

    Memory.add(memory, "The user likes tea.", user_id="u1", metadata={"topic": "drink"}, infer=False)

    temporal_notice.assert_not_called()
    first_run_notice.assert_called_once_with(memory, "sync", "add")
    memory.db.close()


def test_sync_add_does_not_run_background_failure_before_return(monkeypatch):
    memory = make_sync_memory()
    memory._save_short_term_messages.return_value = [{"role": "user", "content": "The user visited Paris."}]
    memory._process_evicted_long_term_memories.side_effect = RuntimeError("vector failure")
    temporal_notice = MagicMock()
    first_run_notice = MagicMock()
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", first_run_notice)

    result = Memory.add(
        memory,
        "The user visited Paris.",
        user_id="u1",
        metadata={"event_date": "2025-04-09"},
        infer=False,
    )

    assert result["results"] == []
    memory._process_evicted_long_term_memories.assert_not_called()
    temporal_notice.assert_called_once()
    first_run_notice.assert_not_called()
    memory.db.close()


def test_sync_search_temporal_query_triggers_notice_after_success(monkeypatch):
    memory = make_sync_memory()
    temporal_notice = MagicMock()
    first_run_notice = MagicMock()
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", first_run_notice)

    result = Memory.search(memory, "what happened last week?", filters={"user_id": "u1"})

    assert result == {"results": []}
    memory._search_vector_store.assert_called_once()
    temporal_notice.assert_called_once_with(memory, "sync", "search", "query", "relative_phrase")
    first_run_notice.assert_not_called()


def test_sync_search_temporal_filter_triggers_notice_after_success(monkeypatch):
    memory = make_sync_memory()
    temporal_notice = MagicMock()
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", MagicMock())

    Memory.search(
        memory,
        "favorite drink",
        filters={"user_id": "u1", "created_at": {"gte": "2025-04-01"}},
    )

    temporal_notice.assert_called_once_with(memory, "sync", "search", "filter", "date_range_filter")


def test_sync_search_failure_does_not_trigger_temporal_usage_notice(monkeypatch):
    memory = make_sync_memory()
    memory._search_vector_store.side_effect = RuntimeError("search failure")
    temporal_notice = MagicMock()
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice", MagicMock())

    with pytest.raises(RuntimeError, match="search failure"):
        Memory.search(memory, "what happened last week?", filters={"user_id": "u1"})

    temporal_notice.assert_not_called()


@pytest.mark.asyncio
async def test_async_add_temporal_metadata_triggers_notice_after_success(monkeypatch):
    memory = make_async_memory()
    temporal_notice = AsyncMock()
    first_run_notice = AsyncMock()
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice_async", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", first_run_notice)

    result = await AsyncMemory.add(
        memory,
        "The user visited Paris.",
        user_id="u1",
        metadata={"event_date": "2025-04-09"},
        infer=False,
    )

    assert result == {
        "results": [],
        "background": {"migration_job_id": None, "profile_job_id": None},
    }
    memory._process_evicted_long_term_memories.assert_not_awaited()
    temporal_notice.assert_awaited_once_with(memory, "async", "add", "metadata", "date_like_metadata")
    first_run_notice.assert_not_awaited()
    memory.db.close()


@pytest.mark.asyncio
async def test_async_add_runs_scale_detection_in_thread(monkeypatch):
    memory = make_async_memory()
    scale_detector = MagicMock(return_value=("memory_count", "memory_count_threshold", None, 2000, 2000))
    scale_notice = AsyncMock()
    first_run_notice = AsyncMock()
    to_thread_calls = []

    async def to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr(memory_main, "detect_scale_threshold_from_add_result", scale_detector)
    monkeypatch.setattr(memory_main.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(memory_main, "display_scale_threshold_notice_async", scale_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", first_run_notice)

    result = await AsyncMemory.add(memory, "The user likes tea.", user_id="u1", infer=False)

    assert result == {
        "results": [],
        "background": {"migration_job_id": None, "profile_job_id": None},
    }
    assert to_thread_calls[-1] == (scale_detector, (memory, []), {})
    scale_detector.assert_called_once_with(memory, [])
    scale_notice.assert_awaited_once_with(
        memory,
        "async",
        "add",
        "memory_count",
        "memory_count_threshold",
        None,
        2000,
        2000,
    )
    first_run_notice.assert_not_awaited()
    memory.db.close()


@pytest.mark.asyncio
async def test_async_search_temporal_query_triggers_notice_after_success(monkeypatch):
    memory = make_async_memory()
    temporal_notice = AsyncMock()
    first_run_notice = AsyncMock()
    monkeypatch.setattr(memory_main, "capture_event", MagicMock())
    monkeypatch.setattr(memory_main, "display_temporal_usage_notice_async", temporal_notice)
    monkeypatch.setattr(memory_main, "display_first_run_notice_async", first_run_notice)

    result = await AsyncMemory.search(memory, "what happened last week?", filters={"user_id": "u1"})

    assert result == {"results": []}
    memory._search_vector_store.assert_awaited_once()
    temporal_notice.assert_awaited_once_with(memory, "async", "search", "query", "relative_phrase")
    first_run_notice.assert_not_awaited()
