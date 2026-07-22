import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import UserProfileConfig
from mem0.configs.enums import MemoryType
from mem0.memory.main import AsyncMemory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_updater import ProfileUpdater
from mem0.memory.storage import SQLiteManager


class _RecordingSyncLLM:
    def __init__(self, response=None):
        self.response = response or {"operations": [], "unmapped_facts": []}
        self.calls = []
        self.thread_ids = []

    def generate_response(self, **kwargs):
        self.calls.append(kwargs)
        self.thread_ids.append(threading.get_ident())
        return self.response


class _RecordingAsyncLLM(_RecordingSyncLLM):
    def generate_response(self, **kwargs):
        raise AssertionError("sync LLM entry point must not be used")

    async def generate_response_async(self, **kwargs):
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        return self.response


@pytest.fixture
def db():
    manager = SQLiteManager(":memory:")
    yield manager
    manager.close()


def _set_risk_plan(value="balanced"):
    return ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {"operation": "set", "attribute_key": "risk_level", "value": value},
            ]
        }
    )


def _append_product_plan(product="ETF"):
    return ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_products",
                    "items": [product],
                }
            ]
        }
    )


def _build_async_memory(db, *, config=None, llm=None):
    profile_config = config or UserProfileConfig(enabled=True)
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(
        profile=profile_config,
        llm=SimpleNamespace(config={}),
        history_db_path=db.db_path,
        vector_store=SimpleNamespace(provider="mock", config=SimpleNamespace()),
        midterm=SimpleNamespace(enabled=False, short_term_capacity=2),
    )
    memory.db = db
    memory.llm = llm or _RecordingSyncLLM()
    memory._profile_manager = None
    memory._profile_updater = None
    memory._profile_user_locks = {}
    memory._profile_user_locks_guard = asyncio.Lock()
    memory._entity_store = None
    return memory


def _disable_async_add_notices(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice_async", AsyncMock())
    monkeypatch.setattr("mem0.memory.main.display_scale_threshold_notice_async", AsyncMock())
    monkeypatch.setattr("mem0.memory.main.display_temporal_usage_notice_async", AsyncMock())


@pytest.mark.asyncio
async def test_profile_manager_and_updater_are_lazy(db):
    llm = _RecordingSyncLLM()
    memory = _build_async_memory(db, llm=llm)

    assert memory._profile_manager is None
    assert memory._profile_updater is None

    manager = memory.profile_manager

    assert isinstance(manager, ProfileManager)
    assert memory._profile_updater is None
    assert llm.calls == []
    assert isinstance(memory.profile_updater, ProfileUpdater)
    assert llm.calls == []


@pytest.mark.asyncio
async def test_async_get_profile_reads_current_value(db):
    db.upsert_user_profile_value("user-1", "risk_level", "balanced")
    memory = _build_async_memory(db)

    profile = await memory.get_profile("user-1")

    assert profile == {"user_id": "user-1", "profile": {"risk_level": "balanced"}}


@pytest.mark.asyncio
async def test_async_update_profile_writes_value(db):
    llm = _RecordingSyncLLM(
        {
            "operations": [
                {"operation": "set", "attribute_key": "risk_level", "value": "balanced"},
            ],
            "unmapped_facts": [],
        }
    )
    memory = _build_async_memory(db, llm=llm)

    profile = await memory.update_profile("user-1", [{"role": "user", "content": "I prefer balanced risk."}])

    assert profile["profile"]["risk_level"] == "balanced"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_disabled_async_update_does_not_initialize_updater_or_call_llm(db):
    llm = _RecordingSyncLLM()
    memory = _build_async_memory(db, config=UserProfileConfig(enabled=False), llm=llm)

    with pytest.raises(ValueError, match="profile updates are disabled"):
        await memory.update_profile("user-1", [{"role": "user", "content": "I prefer ETFs."}])

    assert memory._profile_updater is None
    assert llm.calls == []


@pytest.mark.asyncio
async def test_assistant_only_async_update_does_not_initialize_updater(db):
    memory = _build_async_memory(db)

    profile = await memory.update_profile(
        "user-1",
        [{"role": "assistant", "content": "The user prefers ETFs."}],
    )

    assert profile["profile"] == {}
    assert memory._profile_updater is None


@pytest.mark.asyncio
async def test_empty_async_update_does_not_call_llm(db):
    llm = _RecordingSyncLLM()
    memory = _build_async_memory(db, llm=llm)

    profile = await memory.update_profile("user-1", [])

    assert profile["profile"] == {}
    assert llm.calls == []


@pytest.mark.asyncio
async def test_async_automatic_update_respects_update_on_add_switch(db):
    config = UserProfileConfig(enabled=True, update_on_add=False)
    memory = _build_async_memory(db, config=config)

    await memory._update_profile_after_add(
        "user-1",
        [{"role": "user", "content": "I prefer ETFs."}],
    )

    assert memory._profile_updater is None


@pytest.mark.asyncio
async def test_async_add_profile_failure_preserves_result(db, monkeypatch, caplog):
    memory = _build_async_memory(db)
    memory._save_short_term_messages = AsyncMock(return_value=[{"role": "user", "content": "I prefer ETFs"}])
    memory._process_evicted_long_term_memories = AsyncMock(return_value=[{"id": "memory-1", "event": "ADD"}])
    memory._process_midterm_evictions = MagicMock()
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan_async = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    _disable_async_add_notices(monkeypatch)

    result = await memory.add("I prefer ETFs", user_id="user-1", run_id="run-1", infer=False)

    assert result == {"results": [{"id": "memory-1", "event": "ADD"}]}
    assert "Automatic profile update failed" in caplog.text


@pytest.mark.asyncio
async def test_async_procedural_add_updates_normalized_profile(db, monkeypatch):
    memory = _build_async_memory(db)
    memory._create_procedural_memory = AsyncMock(return_value={"results": [{"id": "procedure-1"}]})
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan_async = AsyncMock(return_value=_append_product_plan())
    _disable_async_add_notices(monkeypatch)

    result = await memory.add(
        "I prefer ETFs",
        user_id=" user-1 ",
        agent_id="agent-1",
        memory_type=MemoryType.PROCEDURAL.value,
    )

    assert result == {"results": [{"id": "procedure-1"}]}
    assert (await memory.get_profile(" user-1 "))["profile"]["preferred_products"] == ["ETF"]


@pytest.mark.asyncio
async def test_async_add_with_infer_false_still_updates_profile(db, monkeypatch):
    memory = _build_async_memory(db)
    memory._process_evicted_long_term_memories = AsyncMock()
    memory._process_midterm_evictions = MagicMock()
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan_async = AsyncMock(return_value=_append_product_plan())
    _disable_async_add_notices(monkeypatch)

    result = await memory.add("I prefer ETFs", user_id="user-1", run_id="run-1", infer=False)

    assert result == {"results": []}
    memory._process_evicted_long_term_memories.assert_not_awaited()
    assert (await memory.get_profile("user-1"))["profile"]["preferred_products"] == ["ETF"]


@pytest.mark.asyncio
async def test_async_delete_profile_value(db):
    db.upsert_user_profile_value("user-1", "risk_level", "balanced")
    memory = _build_async_memory(db)

    deleted = await memory.delete_profile_value("user-1", "risk_level")

    assert deleted is True
    assert (await memory.get_profile("user-1"))["profile"] == {}


@pytest.mark.asyncio
async def test_async_delete_profile(db):
    db.upsert_user_profile_value("user-1", "risk_level", "balanced")
    db.upsert_user_profile_value("user-1", "investment_horizon", "long_term")
    memory = _build_async_memory(db)

    deleted = await memory.delete_profile("user-1")

    assert deleted == 2
    assert (await memory.get_profile("user-1"))["profile"] == {}


@pytest.mark.asyncio
async def test_async_dynamic_attribute_switch(db):
    config = UserProfileConfig(enabled=True, allow_dynamic_attributes=False)
    memory = _build_async_memory(db, config=config)
    definition = {
        "attribute_key": "custom_note",
        "attribute_name": "Custom note",
        "attribute_category": "custom",
        "description": "Custom note",
        "value_type": "string",
        "value_schema": {"type": "string"},
        "merge_policy": "replace",
    }

    with pytest.raises(ValueError, match="Dynamic profile attributes are disabled"):
        await memory.create_profile_attribute(definition)

    config.allow_dynamic_attributes = True
    created = await memory.create_profile_attribute(definition)
    assert created["attribute_key"] == "custom_note"


@pytest.mark.asyncio
async def test_async_reset_recreates_profile_storage(tmp_path, monkeypatch):
    db_path = str(tmp_path / "async-reset.db")
    db = SQLiteManager(db_path)
    db.upsert_user_profile_value("user-1", "risk_level", "balanced")
    memory = _build_async_memory(db)
    memory.config.history_db_path = db_path
    memory.vector_store = MagicMock()
    memory._reset_midterm_state = MagicMock()
    memory._profile_manager = memory.profile_manager
    memory._profile_updater = memory.profile_updater
    memory._profile_user_locks["user-1"] = asyncio.Lock()
    monkeypatch.setattr("mem0.memory.main.VectorStoreFactory.reset", lambda store: store)
    monkeypatch.setattr("mem0.memory.main.capture_event", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice_async", AsyncMock())

    await memory.reset()

    assert memory._profile_manager is None
    assert memory._profile_updater is None
    assert memory._profile_user_locks == {}
    assert len(memory.db.list_profile_attributes()) == 9
    assert memory.db.get_user_profile_values("user-1") == []
    assert memory.profile_manager.db is memory.db
    memory.close()


def test_async_close_releases_profile_references():
    db = SQLiteManager(":memory:")
    memory = _build_async_memory(db)
    memory._profile_manager = memory.profile_manager
    memory._profile_updater = memory.profile_updater
    memory._profile_user_locks["user-1"] = asyncio.Lock()

    memory.close()

    assert memory._profile_manager is None
    assert memory._profile_updater is None
    assert memory._profile_user_locks == {}
    assert memory.db is None


@pytest.mark.asyncio
async def test_async_profile_user_id_is_normalized(db):
    memory = _build_async_memory(db)
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan_async = AsyncMock(return_value=_set_risk_plan())

    updated = await memory.update_profile(" user-1 ", [{"role": "user", "content": "Balanced risk."}])

    assert updated["user_id"] == "user-1"
    assert await memory.get_profile(" user-1 ") == await memory.get_profile("user-1")
    assert set(memory._profile_user_locks) == {"user-1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["get_profile", "update_profile", "delete_profile"])
async def test_async_profile_apis_reject_internal_whitespace(db, method_name):
    memory = _build_async_memory(db)
    method = getattr(memory, method_name)
    args = (
        ("user 1", [{"role": "user", "content": "Balanced risk."}]) if method_name == "update_profile" else ("user 1",)
    )

    with pytest.raises(ValueError, match="cannot contain whitespace"):
        await method(*args)


@pytest.mark.asyncio
async def test_same_user_async_updates_are_serialized(db):
    memory = _build_async_memory(db)
    active = 0
    max_active = 0

    async def generate_update_plan_async(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return ProfileUpdatePlan()

    memory._profile_updater = SimpleNamespace(generate_update_plan_async=generate_update_plan_async)

    await asyncio.gather(
        memory.update_profile(" user-1 ", [{"role": "user", "content": "First"}]),
        memory.update_profile("user-1", [{"role": "user", "content": "Second"}]),
    )

    assert max_active == 1
    assert set(memory._profile_user_locks) == {"user-1"}


@pytest.mark.asyncio
async def test_different_user_async_updates_can_overlap(db):
    memory = _build_async_memory(db)
    active = 0
    max_active = 0

    async def generate_update_plan_async(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return ProfileUpdatePlan()

    memory._profile_updater = SimpleNamespace(generate_update_plan_async=generate_update_plan_async)

    await asyncio.gather(
        memory.update_profile("user-1", [{"role": "user", "content": "First"}]),
        memory.update_profile("user-2", [{"role": "user", "content": "Second"}]),
    )

    assert max_active == 2


@pytest.mark.asyncio
async def test_sync_llm_runs_in_worker_thread(db):
    llm = _RecordingSyncLLM()
    updater = ProfileUpdater(llm, UserProfileConfig())
    main_thread_id = threading.get_ident()

    await updater.generate_update_plan_async(
        current_profile={"user_id": "user-1", "profile": {}},
        attribute_catalog=ProfileManager(db).list_attributes(),
        messages=["What are the risks of ETFs?"],
    )

    assert llm.thread_ids[0] != main_thread_id


@pytest.mark.asyncio
async def test_magic_mock_sync_llm_uses_thread_fallback(db):
    llm = MagicMock()
    llm.generate_response.return_value = {"operations": [], "unmapped_facts": []}
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = await updater.generate_update_plan_async(
        current_profile={"user_id": "user-1", "profile": {}},
        attribute_catalog=ProfileManager(db).list_attributes(),
        messages=["What are the risks of ETFs?"],
    )

    assert plan == ProfileUpdatePlan()
    llm.generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_native_async_llm_entry_point_is_used(db):
    llm = _RecordingAsyncLLM()
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = await updater.generate_update_plan_async(
        current_profile={"user_id": "user-1", "profile": {}},
        attribute_catalog=ProfileManager(db).list_attributes(),
        messages=["What are the risks of ETFs?"],
    )

    assert plan == ProfileUpdatePlan()
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_async_profile_database_calls_use_to_thread(db, monkeypatch):
    memory = _build_async_memory(db)
    original_to_thread = asyncio.to_thread
    calls = []

    async def recording_to_thread(func, *args, **kwargs):
        calls.append(func)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("mem0.memory.main.asyncio.to_thread", recording_to_thread)

    await memory.get_profile("user-1")

    assert any(getattr(func, "__name__", "") == "get_profile" for func in calls)


@pytest.mark.asyncio
async def test_async_profile_return_structure_matches_sync(db):
    db.upsert_user_profile_value("user-1", "risk_level", "balanced")
    memory = _build_async_memory(db)
    sync_profile = ProfileManager(db).get_profile("user-1", include_metadata=True)

    async_profile = await memory.get_profile("user-1", include_metadata=True)

    assert async_profile == sync_profile


@pytest.mark.asyncio
async def test_async_profile_payload_contains_only_user_messages(db):
    llm = _RecordingSyncLLM()
    memory = _build_async_memory(db, llm=llm)

    await memory.update_profile(
        "user-1",
        [
            {"role": "assistant", "content": "The user prefers stocks."},
            {"role": "user", "content": "What are the risks of ETFs?"},
        ],
    )

    payload = json.loads(llm.calls[0]["messages"][1]["content"])
    assert payload["user_messages"] == ["What are the risks of ETFs?"]
