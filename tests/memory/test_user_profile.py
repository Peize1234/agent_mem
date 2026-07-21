import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from mem0.configs.base import UserProfileConfig
from mem0.configs.enums import MemoryType
from mem0.memory.main import Memory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_updater import ProfileUpdater
from mem0.memory.storage import SQLiteManager


@pytest.fixture
def db():
    manager = SQLiteManager(":memory:")
    yield manager
    manager.close()


@pytest.fixture
def profile_manager(db):
    return ProfileManager(db, UserProfileConfig())


def _apply(profile_manager, user_id, *operations):
    return profile_manager.apply_update_plan(user_id, {"operations": list(operations)})


def _attribute_definition(attribute_key, *, value_type="string", merge_policy="replace"):
    if value_type == "string":
        value_schema = {"type": "string"}
    elif value_type == "string_list":
        value_schema = {"type": "array", "items": {"type": "string"}}
    else:
        raise ValueError(f"Unsupported test value_type: {value_type}")
    return {
        "attribute_key": attribute_key,
        "attribute_name": attribute_key,
        "attribute_category": "custom",
        "description": f"Custom attribute {attribute_key}",
        "value_type": value_type,
        "value_schema": value_schema,
        "merge_policy": merge_policy,
    }


def test_predefined_attributes_are_initialized(db):
    attributes = db.list_profile_attributes()

    assert {item["attribute_key"] for item in attributes} == {
        "risk_level",
        "max_acceptable_loss_ratio",
        "investment_horizon",
        "liquidity_requirement",
        "preferred_products",
        "avoided_products",
        "financial_knowledge_level",
        "current_financial_goals",
        "response_preferences",
    }
    assert all(item["is_predefined"] for item in attributes)


def test_dynamic_attributes_are_disabled_by_default(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=False))

    with pytest.raises(ValueError, match="Dynamic profile attributes are disabled"):
        manager.create_attribute(_attribute_definition("custom_note"))


def test_dynamic_attribute_can_be_created_when_enabled(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))

    created = manager.create_attribute(_attribute_definition("custom_note"))

    assert created["attribute_key"] == "custom_note"
    assert created["is_predefined"] is False


def test_dynamic_attribute_limit_is_enforced(db):
    manager = ProfileManager(
        db,
        UserProfileConfig(allow_dynamic_attributes=True, max_dynamic_attributes=1),
    )
    manager.create_attribute(_attribute_definition("custom_note"))

    with pytest.raises(ValueError, match="max_dynamic_attributes=1"):
        manager.create_attribute(_attribute_definition("second_note"))


@pytest.mark.parametrize("attribute_key", ["Bad Key", "risk-level", "风险等级", "1risk_level"])
def test_invalid_attribute_keys_are_rejected_before_database_write(db, attribute_key):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))

    with pytest.raises(ValidationError):
        manager.create_attribute(_attribute_definition(attribute_key))

    assert db.get_profile_attribute(attribute_key, include_inactive=True) is None


@pytest.mark.parametrize("suggested_key", ["Bad Key", "risk-level", "风险等级", "1risk_level"])
def test_unmapped_fact_suggested_keys_use_same_validation(suggested_key):
    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate(
            {
                "unmapped_facts": [
                    {"suggested_key": suggested_key, "description": "fact", "value": "value"},
                ]
            }
        )


def test_replace_list_attribute_rejects_append_unique(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))
    manager.create_attribute(_attribute_definition("watch_list", value_type="string_list", merge_policy="replace"))

    with pytest.raises(ValueError, match="watch_list.*append_unique"):
        _apply(
            manager,
            "user-1",
            {"operation": "append_unique", "attribute_key": "watch_list", "items": ["ETF"]},
        )


def test_profile_is_shared_across_runs_and_isolated_by_user(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "risk_level", "value": "conservative"},
    )

    # Profile APIs deliberately have no run_id, so both sessions read the same user value.
    run_1_profile = profile_manager.get_profile("user-1")
    run_2_profile = profile_manager.get_profile("user-1")

    assert run_1_profile == run_2_profile
    assert run_1_profile["profile"]["risk_level"] == "conservative"
    assert profile_manager.get_profile("user-2")["profile"] == {}


def test_repeated_set_replaces_value_and_increments_version(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "risk_level", "value": "conservative"},
    )
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "risk_level", "value": "balanced"},
    )

    profile = profile_manager.get_profile("user-1", include_metadata=True)
    assert profile["profile"]["risk_level"]["value"] == "balanced"
    assert profile["profile"]["risk_level"]["value_version"] == 2


def test_list_append_is_unique_and_remove_items_is_supported(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "preferred_products",
            "items": ["ETF", "bond fund", "ETF"],
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "preferred_products",
            "items": ["ETF", "index fund"],
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "remove_items",
            "attribute_key": "preferred_products",
            "items": ["bond fund"],
        },
    )

    assert profile_manager.get_profile("user-1")["profile"]["preferred_products"] == ["ETF", "index fund"]


def test_append_existing_items_does_not_increment_version(profile_manager):
    operation = {
        "operation": "append_unique",
        "attribute_key": "preferred_products",
        "items": ["ETF"],
    }
    _apply(profile_manager, "user-1", operation)
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_products"]

    _apply(profile_manager, "user-1", operation)

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_products"]
    assert after["value_version"] == before["value_version"] == 1


def test_empty_append_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "append_unique", "attribute_key": "preferred_products", "items": []},
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_remove_items_from_missing_list_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "remove_items", "attribute_key": "preferred_products", "items": ["ETF"]},
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_removing_missing_item_does_not_increment_version(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "append_unique", "attribute_key": "preferred_products", "items": ["ETF"]},
    )
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_products"]

    _apply(
        profile_manager,
        "user-1",
        {"operation": "remove_items", "attribute_key": "preferred_products", "items": ["bond fund"]},
    )

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_products"]
    assert after["value_version"] == before["value_version"] == 1


def test_setting_same_value_does_not_change_version_or_timestamp(profile_manager, db):
    operation = {"operation": "set", "attribute_key": "risk_level", "value": "balanced"}
    _apply(profile_manager, "user-1", operation)
    before = db.get_user_profile_values("user-1")[0]

    _apply(profile_manager, "user-1", operation)

    after = db.get_user_profile_values("user-1")[0]
    assert after["value_version"] == before["value_version"] == 1
    assert after["updated_at"] == before["updated_at"]


def test_delete_value_preserves_attribute_definition(profile_manager, db):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "risk_level", "value": "balanced"},
    )

    assert profile_manager.delete_value("user-1", "risk_level") is True
    assert "risk_level" not in profile_manager.get_profile("user-1")["profile"]
    assert db.get_profile_attribute("risk_level") is not None


@pytest.mark.parametrize(
    "operation",
    [
        {"operation": "set", "attribute_key": "risk_level", "value": 1},
        {"operation": "set", "attribute_key": "risk_level", "value": "unsupported"},
        {"operation": "set", "attribute_key": "max_acceptable_loss_ratio", "value": 1.01},
        {"operation": "set", "attribute_key": "max_acceptable_loss_ratio", "value": True},
        {"operation": "append_unique", "attribute_key": "risk_level", "items": ["balanced"]},
    ],
)
def test_invalid_profile_values_are_rejected(profile_manager, operation):
    with pytest.raises((ValueError, ValidationError)):
        _apply(profile_manager, "user-1", operation)


def test_current_financial_goals_accepts_structured_object_list(profile_manager):
    goals = [
        {
            "goal_type": "home_purchase",
            "description": "准备买房",
            "target_date": None,
            "target_amount": None,
            "currency": "CNY",
            "status": "planned",
        }
    ]

    profile = _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "current_financial_goals", "value": goals},
    )

    assert profile["profile"]["current_financial_goals"] == goals


@pytest.mark.parametrize(
    "goal",
    [
        {"goal_type": "home_purchase"},
        {"goal_type": "home_purchase", "description": "买房", "target_amount": -1},
        {"goal_type": "home_purchase", "description": "买房", "status": "unknown"},
        {"goal_type": "home_purchase", "description": "买房", "unexpected": True},
    ],
)
def test_current_financial_goals_rejects_invalid_objects(profile_manager, goal):
    with pytest.raises(ValueError):
        _apply(
            profile_manager,
            "user-1",
            {"operation": "set", "attribute_key": "current_financial_goals", "value": [goal]},
        )


def test_response_preferences_rejects_undefined_fields(profile_manager):
    with pytest.raises(ValueError, match="unexpected"):
        _apply(
            profile_manager,
            "user-1",
            {
                "operation": "set",
                "attribute_key": "response_preferences",
                "value": {"language": "zh-CN", "unexpected": "value"},
            },
        )


def test_detailed_profile_contains_definition_and_value_metadata(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "risk_level",
            "value": "aggressive",
        },
    )

    item = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["risk_level"]
    assert set(item) == {
        "value",
        "attribute_name",
        "category",
        "description",
        "value_type",
        "source_type",
        "confidence",
        "value_version",
    }
    assert item["source_type"] == "explicit"
    assert item["confidence"] == 1.0


def test_low_level_import_interface_still_accepts_imported_metadata(db):
    db.upsert_user_profile_value(
        "user-1",
        "risk_level",
        "balanced",
        source_type="imported",
        confidence=0.8,
    )

    item = db.get_user_profile_values("user-1")[0]
    assert item["source_type"] == "imported"
    assert item["confidence"] == 0.8


class _RecordingLLM:
    def __init__(self, response=None):
        self.response = response or {"operations": [], "unmapped_facts": []}
        self.calls = []

    def generate_response(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_assistant_messages_do_not_enter_profile_update():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))

    memory._update_profile_after_add(
        "user-1",
        [{"role": "assistant", "content": "The user prefers stocks."}],
    )

    memory._profile_updater.generate_update_plan.assert_not_called()


def test_product_question_does_not_generate_preference(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["What are the risks of equity funds?"],
    )

    assert plan.operations == []
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "A question about a product is not evidence that the user prefers that product." in system_prompt


def test_assistant_messages_are_excluded_from_profile_payload(db):
    config = UserProfileConfig(enabled=True)
    llm = _RecordingLLM()
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(profile=config)
    memory.db = db
    memory._profile_manager = ProfileManager(db, config)
    memory._profile_updater = ProfileUpdater(llm, config)

    memory._update_profile_after_add(
        "user-1",
        [
            {"role": "assistant", "content": "The user prefers stocks."},
            {"role": "user", "content": "What are the risks of equity funds?"},
        ],
    )

    payload = json.loads(llm.calls[0]["messages"][1]["content"])
    assert payload["user_messages"] == ["What are the risks of equity funds?"]


def test_explicit_product_preference_can_update_preferred_products(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_products",
                    "items": ["ETF"],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["I prefer ETFs and want to hold them."],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["preferred_products"] == ["ETF"]


def test_llm_plan_operations_do_not_include_source_or_confidence(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {"operation": "set", "attribute_key": "risk_level", "value": "balanced"},
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["I prefer balanced investments."],
    )

    assert plan.operations[0].model_dump() == {
        "operation": "set",
        "attribute_key": "risk_level",
        "value": "balanced",
    }
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "source_type" not in system_prompt
    assert "confidence" not in system_prompt


def test_inferred_extraction_mode_is_explicitly_unimplemented(profile_manager):
    llm = _RecordingLLM()
    updater = ProfileUpdater(llm, UserProfileConfig(extraction_mode="explicit_and_inferred"))

    with pytest.raises(NotImplementedError, match="explicit_and_inferred"):
        updater.generate_update_plan(
            current_profile=profile_manager.get_profile("user-1"),
            attribute_catalog=profile_manager.list_attributes(),
            messages=["I prefer balanced investments."],
        )

    assert llm.calls == []


def _memory_for_automatic_update(config):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(profile=config)
    memory._profile_manager = MagicMock()
    memory._profile_updater = MagicMock()
    return memory


def _memory_with_profile_storage(db, config=None):
    profile_config = config or UserProfileConfig(enabled=True)
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        profile=profile_config,
        llm=SimpleNamespace(config={}),
    )
    memory.db = db
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan.return_value = ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_products",
                    "items": ["ETF"],
                }
            ]
        }
    )
    return memory


def _disable_sync_add_notices(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)


def test_disabled_profile_does_not_call_llm():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=False))

    memory._update_profile_after_add("user-1", [{"role": "user", "content": "I prefer ETFs"}])

    memory._profile_updater.generate_update_plan.assert_not_called()


def test_memory_add_and_profile_apis_share_normalized_user_id(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory._add_to_vector_store = MagicMock(return_value=[{"id": "memory-1", "event": "ADD"}])
    memory._process_midterm_evictions = MagicMock()
    _disable_sync_add_notices(monkeypatch)

    result = memory.add("I prefer ETFs", user_id=" user-1 ", run_id="run-1", infer=False)

    assert result == {"results": [{"id": "memory-1", "event": "ADD"}]}
    _, metadata, effective_filters, *_ = memory._add_to_vector_store.call_args.args
    assert metadata["user_id"] == "user-1"
    assert effective_filters["user_id"] == "user-1"
    assert memory._process_midterm_evictions.call_args.args[1]["user_id"] == "user-1"
    assert memory.get_profile(" user-1 ") == memory.get_profile("user-1")
    assert memory.get_profile("user-1")["profile"]["preferred_products"] == ["ETF"]


def test_profile_and_memory_layers_use_the_same_normalized_user_id(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    memory._create_memory = MagicMock(return_value="memory-1")
    memory._process_midterm_evictions = MagicMock()
    _disable_sync_add_notices(monkeypatch)

    memory.add("I prefer ETFs", user_id=" user-1 ", run_id="run-1", infer=False)

    long_term_metadata = memory._create_memory.call_args.args[2]
    assert long_term_metadata["user_id"] == "user-1"
    assert db.get_messages("run_id=run-1&user_id=user-1")[0]["content"] == "I prefer ETFs"
    assert memory._process_midterm_evictions.call_args.args[1]["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_products"] == ["ETF"]


def test_update_and_delete_profile_normalize_user_id(db):
    memory = _memory_with_profile_storage(db)

    updated = memory.update_profile(" user-1 ", [{"role": "user", "content": "I prefer ETFs"}])

    assert updated["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_products"] == ["ETF"]
    assert memory.delete_profile(" user-1 ") == 1
    assert memory.get_profile("user-1")["profile"] == {}


@pytest.mark.parametrize("method_name", ["get_profile", "update_profile", "delete_profile"])
def test_profile_apis_reject_internal_whitespace_in_user_id(db, method_name):
    memory = _memory_with_profile_storage(db)
    method = getattr(memory, method_name)
    args = (
        ("user 1", [{"role": "user", "content": "I prefer ETFs"}]) if method_name == "update_profile" else ("user 1",)
    )

    with pytest.raises(ValueError, match="cannot contain whitespace"):
        method(*args)


@pytest.mark.parametrize("user_id", [None, 123])
def test_profile_user_id_must_be_a_string(db, user_id):
    memory = _memory_with_profile_storage(db)

    with pytest.raises(ValueError, match="must be a string"):
        memory.get_profile(user_id)


def test_procedural_add_updates_normalized_profile_user(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory._create_procedural_memory = MagicMock(return_value={"results": [{"id": "procedure-1"}]})
    _disable_sync_add_notices(monkeypatch)

    result = memory.add(
        "I prefer ETFs",
        user_id=" user-1 ",
        agent_id="agent-1",
        memory_type=MemoryType.PROCEDURAL.value,
    )

    assert result == {"results": [{"id": "procedure-1"}]}
    assert memory._create_procedural_memory.call_args.kwargs["metadata"]["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_products"] == ["ETF"]


def test_explicit_update_is_rejected_without_initializing_updater_when_disabled():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=False))
    memory._profile_updater = None
    memory.llm = MagicMock()

    with pytest.raises(ValueError, match="profile updates are disabled"):
        memory.update_profile("user-1", [{"role": "user", "content": "I prefer ETFs"}])

    assert memory._profile_updater is None
    memory.llm.generate_response.assert_not_called()


def test_automatic_update_persists_fixed_explicit_metadata(db):
    config = UserProfileConfig(enabled=True)
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(profile=config)
    memory._profile_manager = ProfileManager(db, config)
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan.return_value = ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {"operation": "set", "attribute_key": "risk_level", "value": "balanced"},
            ]
        }
    )

    memory._update_profile_after_add(
        "user-1",
        [{"role": "user", "content": "I prefer balanced investments."}],
    )

    item = memory.get_profile("user-1", include_metadata=True)["profile"]["risk_level"]
    assert item["source_type"] == "explicit"
    assert item["confidence"] == 1.0


def test_automatic_update_failure_does_not_escape_add_path(caplog):
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))
    memory._profile_updater.generate_update_plan.side_effect = RuntimeError("LLM unavailable")

    memory._update_profile_after_add("user-1", [{"role": "user", "content": "I prefer ETFs"}])

    assert "Automatic profile update failed" in caplog.text


def test_profile_failure_preserves_memory_add_result(monkeypatch):
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))
    memory.config.llm = SimpleNamespace(config={})
    memory._add_to_vector_store = MagicMock(return_value=[{"id": "memory-1", "event": "ADD"}])
    memory._process_midterm_evictions = MagicMock()
    memory._profile_updater.generate_update_plan.side_effect = RuntimeError("LLM unavailable")
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)

    result = memory.add("I prefer ETFs", user_id="user-1", run_id="run-1", infer=False)

    assert result == {"results": [{"id": "memory-1", "event": "ADD"}]}


def test_concurrent_appends_do_not_lose_updates(profile_manager):
    barrier = threading.Barrier(3)
    errors = []

    def append(product):
        try:
            barrier.wait()
            _apply(
                profile_manager,
                "user-1",
                {"operation": "append_unique", "attribute_key": "preferred_products", "items": [product]},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(product,)) for product in ("ETF", "bond fund")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(profile_manager.get_profile("user-1")["profile"]["preferred_products"]) == {"ETF", "bond fund"}


def test_concurrent_appends_across_sqlite_connections_do_not_lose_updates(tmp_path):
    db_path = str(tmp_path / "shared-profile.db")
    first_db = SQLiteManager(db_path)
    second_db = SQLiteManager(db_path)
    first_manager = ProfileManager(first_db, UserProfileConfig())
    second_manager = ProfileManager(second_db, UserProfileConfig())
    barrier = threading.Barrier(3)
    errors = []

    def append(manager, product):
        try:
            barrier.wait()
            _apply(
                manager,
                "user-1",
                {"operation": "append_unique", "attribute_key": "preferred_products", "items": [product]},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(first_manager, "ETF")),
        threading.Thread(target=append, args=(second_manager, "bond fund")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(first_manager.get_profile("user-1")["profile"]["preferred_products"]) == {"ETF", "bond fund"}
    first_db.close()
    second_db.close()


def test_reset_drops_and_recreates_profile_tables(tmp_path):
    db_path = str(tmp_path / "profile.db")
    manager = SQLiteManager(db_path)
    manager.upsert_user_profile_value("user-1", "risk_level", "balanced")
    manager.reset()

    tables = manager.connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('profile_attributes', 'user_profile_values')
        """
    ).fetchall()
    assert tables == []
    manager.close()

    rebuilt = SQLiteManager(db_path)
    assert len(rebuilt.list_profile_attributes()) == 9
    assert rebuilt.get_user_profile_values("user-1") == []
    rebuilt.close()
