import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from mem0.configs import predefined_profile_attributes
from mem0.configs.base import BackgroundTaskConfig, UserProfileConfig
from mem0.configs.enums import MemoryType
from mem0.configs.predefined_profile_attributes import PREDEFINED_PROFILE_ATTRIBUTES
from mem0.configs.profile_prompts import PROFILE_UPDATE_SYSTEM_PROMPT
from mem0.memory.main import Memory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_updater import ProfileUpdater
from mem0.memory.storage import SQLiteManager
from mem0.memory.profile_validator import serialize_profile_value, validate_attribute_definition


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


def _attribute_definition(attribute_key, *, value_type="string", merge_policy="replace", value_schema=None):
    if value_schema is not None:
        pass
    elif value_type == "string":
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
        "analysis_role",
        "financial_analysis_expertise_level",
        "default_report_audience",
        "default_analysis_scope",
        "primary_analysis_tasks",
        "analysis_focus_areas",
        "preferred_kpis",
        "preferred_comparison_baselines",
        "preferred_time_granularity",
        "risk_focus_areas",
        "decision_contexts",
        "default_analysis_horizon",
        "materiality_threshold",
        "anomaly_detection_preference",
        "default_reporting_basis",
        "currency_display_preference",
        "evidence_requirements",
        "recommendation_preferences",
        "response_preferences",
    }
    assert all(item["is_predefined"] for item in attributes)
    assert all(any("\u4e00" <= character <= "\u9fff" for character in item["attribute_name"]) for item in attributes)
    assert all(any("\u4e00" <= character <= "\u9fff" for character in item["description"]) for item in attributes)
    assert all("本轮用户消息" in item["description"] for item in attributes)
    assert all("数据权限" in item["description"] for item in attributes)


def test_profile_extraction_rule_replaces_stable_profile_rule():
    assert not hasattr(predefined_profile_attributes, "_STABLE_PROFILE_RULE")
    rule = predefined_profile_attributes._PROFILE_EXTRACTION_RULE
    assert "本轮用户消息包含与该画像字段直接相关" in rule
    assert "能够可靠归一化" in rule
    assert "current_profile" in rule
    assert "跨会话长期稳定" not in rule
    assert "默认偏好" not in rule


def test_all_predefined_attribute_definitions_pass_validation():
    validated = [validate_attribute_definition(definition) for definition in PREDEFINED_PROFILE_ATTRIBUTES]

    assert len(validated) == len(PREDEFINED_PROFILE_ATTRIBUTES) == 19


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
        {"operation": "set", "attribute_key": "analysis_role", "value": "financial_analyst"},
    )

    # Profile APIs deliberately have no run_id, so both sessions read the same user value.
    run_1_profile = profile_manager.get_profile("user-1")
    run_2_profile = profile_manager.get_profile("user-1")

    assert run_1_profile == run_2_profile
    assert run_1_profile["profile"]["analysis_role"] == "financial_analyst"
    assert profile_manager.get_profile("user-2")["profile"] == {}


def test_repeated_set_replaces_value_and_increments_version(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "analysis_role", "value": "financial_analyst"},
    )
    _apply(
        profile_manager,
        "user-1",
        {"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"},
    )

    profile = profile_manager.get_profile("user-1", include_metadata=True)
    assert profile["profile"]["analysis_role"]["value"] == "fp_and_a"
    assert profile["profile"]["analysis_role"]["value_version"] == 2


def test_list_append_is_unique_and_remove_items_is_supported(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "preferred_kpis",
            "items": ["毛利率", "经营现金流", "毛利率"],
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "preferred_kpis",
            "items": ["毛利率", "应收账款"],
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "remove_items",
            "attribute_key": "preferred_kpis",
            "items": ["经营现金流"],
        },
    )

    assert profile_manager.get_profile("user-1")["profile"]["preferred_kpis"] == ["毛利率", "应收账款"]


def test_append_existing_items_does_not_increment_version(profile_manager):
    operation = {
        "operation": "append_unique",
        "attribute_key": "preferred_kpis",
        "items": ["毛利率"],
    }
    _apply(profile_manager, "user-1", operation)
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_kpis"]

    _apply(profile_manager, "user-1", operation)

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_kpis"]
    assert after["value_version"] == before["value_version"] == 1


def test_empty_append_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "append_unique", "attribute_key": "preferred_kpis", "items": []},
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_remove_items_from_missing_list_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "remove_items", "attribute_key": "preferred_kpis", "items": ["毛利率"]},
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_removing_missing_item_does_not_increment_version(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {"operation": "append_unique", "attribute_key": "preferred_kpis", "items": ["毛利率"]},
    )
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_kpis"]

    _apply(
        profile_manager,
        "user-1",
        {"operation": "remove_items", "attribute_key": "preferred_kpis", "items": ["净利率"]},
    )

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["preferred_kpis"]
    assert after["value_version"] == before["value_version"] == 1


def test_setting_same_value_does_not_change_version_or_timestamp(profile_manager, db):
    operation = {"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"}
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
        {"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"},
    )

    assert profile_manager.delete_value("user-1", "analysis_role") is True
    assert "analysis_role" not in profile_manager.get_profile("user-1")["profile"]
    assert db.get_profile_attribute("analysis_role") is not None


@pytest.mark.parametrize(
    "operation",
    [
        {"operation": "set", "attribute_key": "analysis_role", "value": 1},
        {"operation": "set", "attribute_key": "analysis_role", "value": "unsupported"},
        {"operation": "set", "attribute_key": "materiality_threshold", "value": {"amount": -1}},
        {"operation": "set", "attribute_key": "materiality_threshold", "value": {"amount": True}},
        {"operation": "append_unique", "attribute_key": "analysis_role", "items": ["fp_and_a"]},
    ],
)
def test_invalid_profile_values_are_rejected(profile_manager, operation):
    with pytest.raises((ValueError, ValidationError)):
        _apply(profile_manager, "user-1", operation)


def _object_list_manager(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))
    manager.create_attribute(
        _attribute_definition(
            "report_templates",
            value_type="object_list",
            value_schema={
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "template_type": {"type": "string"},
                        "description": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["active", "inactive"],
                        },
                    },
                    "required": ["template_type", "description"],
                    "additionalProperties": False,
                },
            },
        )
    )
    return manager


def test_object_list_still_accepts_set(db):
    manager = _object_list_manager(db)
    templates = [
        {
            "template_type": "monthly_management_report",
            "description": "月度管理报告",
            "status": "active",
        }
    ]

    profile = _apply(
        manager,
        "user-1",
        {"operation": "set", "attribute_key": "report_templates", "value": templates},
    )

    assert profile["profile"]["report_templates"] == templates


@pytest.mark.parametrize(
    "template",
    [
        {"template_type": "monthly_management_report"},
        {"template_type": "monthly_management_report", "description": "月报", "status": "unknown"},
        {"template_type": "monthly_management_report", "description": "月报", "unexpected": True},
    ],
)
def test_object_list_still_rejects_invalid_objects(db, template):
    manager = _object_list_manager(db)

    with pytest.raises(ValueError):
        _apply(
            manager,
            "user-1",
            {"operation": "set", "attribute_key": "report_templates", "value": [template]},
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


def test_patch_object_preserves_unmodified_fields(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {
                "language": "zh-CN",
                "detail_level": "detailed",
                "include_tables": True,
            },
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"include_charts": True},
            "remove_keys": [],
        },
    )

    assert profile["profile"]["response_preferences"] == {
        "language": "zh-CN",
        "detail_level": "detailed",
        "include_tables": True,
        "include_charts": True,
    }


def test_patch_object_can_create_missing_object(profile_manager):
    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "default_analysis_scope",
            "updates": {
                "organization": "集团总部",
                "regions": ["华东", "华南"],
            },
            "remove_keys": [],
        },
    )

    assert profile["profile"]["default_analysis_scope"] == {
        "organization": "集团总部",
        "regions": ["华东", "华南"],
    }


def test_patch_object_remove_keys_deletes_optional_field(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "zh-CN", "tone": "professional"},
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": ["tone"],
        },
    )

    assert profile["profile"]["response_preferences"] == {"language": "zh-CN"}


def test_patch_object_none_is_saved_as_null(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "zh-CN", "include_charts": True},
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"language": None},
            "remove_keys": [],
        },
    )

    assert profile["profile"]["response_preferences"] == {
        "language": None,
        "include_charts": True,
    }


def test_patch_object_none_requires_nullable_schema(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))
    manager.create_attribute(
        _attribute_definition(
            "strict_object",
            value_type="object",
            value_schema={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "additionalProperties": False,
            },
        )
    )

    with pytest.raises(ValueError, match="updates.label must be of type string"):
        _apply(
            manager,
            "user-1",
            {
                "operation": "patch_object",
                "attribute_key": "strict_object",
                "updates": {"label": None},
                "remove_keys": [],
            },
        )

    assert manager.get_profile("user-1")["profile"] == {}


def test_patch_object_rejects_non_object_attribute(profile_manager):
    with pytest.raises(ValueError, match="preferred_kpis.*patch_object"):
        _apply(
            profile_manager,
            "user-1",
            {
                "operation": "patch_object",
                "attribute_key": "preferred_kpis",
                "updates": {"item": "毛利率"},
                "remove_keys": [],
            },
        )


@pytest.mark.parametrize(
    "operation",
    [
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"unexpected": True},
            "remove_keys": [],
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"include_charts": "yes"},
            "remove_keys": [],
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": ["unexpected"],
        },
    ],
)
def test_patch_object_rejects_unknown_fields_and_wrong_types(profile_manager, operation):
    with pytest.raises(ValueError):
        _apply(profile_manager, "user-1", operation)


def test_patch_object_rejects_overlapping_update_and_remove(profile_manager):
    with pytest.raises(ValueError, match="update and remove the same fields"):
        _apply(
            profile_manager,
            "user-1",
            {
                "operation": "patch_object",
                "attribute_key": "response_preferences",
                "updates": {"tone": "professional"},
                "remove_keys": ["tone"],
            },
        )


def test_patch_object_cannot_remove_required_field_and_preserves_original_value(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))
    manager.create_attribute(
        _attribute_definition(
            "required_scope",
            value_type="object",
            value_schema={
                "type": "object",
                "properties": {
                    "organization": {"type": "string"},
                    "region": {"type": "string"},
                },
                "required": ["organization"],
                "additionalProperties": False,
            },
        )
    )
    _apply(
        manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "required_scope",
            "value": {"organization": "集团总部", "region": "华东"},
        },
    )

    with pytest.raises(ValueError, match="organization is required"):
        _apply(
            manager,
            "user-1",
            {
                "operation": "patch_object",
                "attribute_key": "required_scope",
                "updates": {"region": "华南"},
                "remove_keys": ["organization"],
            },
        )

    item = manager.get_profile("user-1", include_metadata=True)["profile"]["required_scope"]
    assert item["value"] == {"organization": "集团总部", "region": "华东"}
    assert item["value_version"] == 1


def test_empty_and_unchanged_object_patches_do_not_write(profile_manager, db):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": [],
        },
    )
    assert profile_manager.get_profile("user-1")["profile"] == {}

    operation = {
        "operation": "patch_object",
        "attribute_key": "response_preferences",
        "updates": {"include_charts": True},
        "remove_keys": [],
    }
    _apply(profile_manager, "user-1", operation)
    before = db.get_user_profile_values("user-1")[0]

    _apply(profile_manager, "user-1", operation)
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": [],
        },
    )

    after = db.get_user_profile_values("user-1")[0]
    assert after["value_version"] == before["value_version"] == 1
    assert after["updated_at"] == before["updated_at"]


def test_set_still_replaces_complete_object_attribute(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "zh-CN", "include_charts": True},
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "en-US"},
        },
    )

    assert profile["profile"]["response_preferences"] == {"language": "en-US"}


def test_sequential_object_patches_keep_both_changes(profile_manager):
    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"conclusion_first": True},
            "remove_keys": [],
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"include_charts": True},
            "remove_keys": [],
        },
    )

    assert profile["profile"]["response_preferences"] == {
        "conclusion_first": True,
        "include_charts": True,
    }


def test_detailed_profile_contains_definition_and_value_metadata(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "finance_manager",
        },
    )

    item = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["analysis_role"]
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
        "analysis_role",
        "fp_and_a",
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


def test_profile_model_request_uses_pretty_json_without_changing_canonical_serialization():
    updater = ProfileUpdater(_RecordingLLM(), UserProfileConfig())
    current_profile = {"user_id": "用户-1", "profile": {"关注": ["现金流", "毛利率"]}}
    attributes = [{"attribute_key": "focus", "value_schema": {"type": "array"}}]
    user_messages = ['第一行\n第二行，含引号 "、反斜杠 \\ 和 emoji 😀']
    payload = {
        "current_profile": current_profile,
        "available_attributes": attributes,
        "user_messages": user_messages,
    }

    request = updater._build_request(current_profile, attributes, user_messages)
    content = request["messages"][1]["content"]

    assert content == json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    assert json.loads(content) == payload
    assert '\n  "current_profile": {' in content
    assert '\n  "user_messages": [' in content
    assert "\\u" not in content
    assert serialize_profile_value(payload) == json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    assert "\n" not in serialize_profile_value(payload)


def _profile_prompt_return_plans():
    decoder = json.JSONDecoder()
    marker = "返回：\n"
    plans = []
    offset = 0
    while True:
        marker_index = PROFILE_UPDATE_SYSTEM_PROMPT.find(marker, offset)
        if marker_index == -1:
            return plans
        plan, consumed = decoder.raw_decode(PROFILE_UPDATE_SYSTEM_PROMPT[marker_index + len(marker) :])
        plans.append(plan)
        offset = marker_index + len(marker) + consumed


def test_prompt_examples_match_current_attribute_schemas(profile_manager):
    plans = _profile_prompt_return_plans()

    assert len(plans) == 5
    for plan in plans:
        profile_manager.validate_update_plan(plan)

    monthly_operation = plans[0]["operations"][0]
    assert monthly_operation == {
        "operation": "append_unique",
        "attribute_key": "preferred_time_granularity",
        "items": ["monthly"],
    }
    assert plans[1]["operations"][0]["items"] == ["quarterly"]
    assert plans[1]["operations"][1]["items"] == ["year_over_year"]
    assert plans[2]["operations"][0]["updates"] == {
        "unit": "ten_thousand",
        "decimal_places": 2,
    }
    assert plans[3]["operations"][0]["updates"] == {
        "unit": "hundred_million",
        "show_currency_symbol": False,
    }
    assert plans[4] == {"operations": [], "unmapped_facts": []}


def test_currency_display_preference_supports_chinese_amount_units(db):
    definition = db.get_profile_attribute("currency_display_preference")

    assert definition["value_schema"]["properties"]["unit"]["enum"] == [
        "unit",
        "thousand",
        "ten_thousand",
        "million",
        "hundred_million",
        "billion",
        None,
    ]
    assert "ten_thousand" in definition["description"]
    assert "万元" in definition["description"]
    assert "hundred_million" in definition["description"]
    assert "亿元" in definition["description"]


@pytest.mark.parametrize(
    ("message", "updates"),
    [
        (
            "后续财务报告金额统一按万元展示，保留两位小数。",
            {"unit": "ten_thousand", "decimal_places": 2},
        ),
        (
            "集团汇报统一使用亿元，不显示币种符号。",
            {"unit": "hundred_million", "show_currency_symbol": False},
        ),
    ],
)
def test_chinese_amount_unit_message_can_generate_valid_patch(profile_manager, message, updates):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "patch_object",
                    "attribute_key": "currency_display_preference",
                    "updates": updates,
                    "remove_keys": [],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=[message],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["currency_display_preference"] == updates


def test_assistant_messages_do_not_enter_profile_update():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))

    memory._update_profile_after_add(
        "user-1",
        [{"role": "assistant", "content": "The user prefers stocks."}],
    )

    memory._profile_updater.generate_update_plan.assert_not_called()


def test_current_analysis_request_can_generate_direct_profile_updates(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_time_granularity",
                    "items": ["quarterly"],
                },
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_comparison_baselines",
                    "items": ["year_over_year"],
                },
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_kpis",
                    "items": ["毛利率", "经营现金流"],
                },
                {
                    "operation": "patch_object",
                    "attribute_key": "response_preferences",
                    "updates": {"include_tables": True},
                    "remove_keys": [],
                },
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["分析本季度收入的同比变化，重点看毛利率和经营现金流，结果用表格展示。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["preferred_time_granularity"] == ["quarterly"]
    assert profile["profile"]["preferred_comparison_baselines"] == ["year_over_year"]
    assert profile["profile"]["preferred_kpis"] == ["毛利率", "经营现金流"]
    assert profile["profile"]["response_preferences"] == {"include_tables": True}
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "本轮 user_messages 中包含与某个字段直接相关" in system_prompt
    assert "可以作为对应画像字段的更新依据" in system_prompt
    assert "跨会话长期稳定" not in system_prompt
    assert "current_profile：该用户的完整当前画像" in system_prompt
    assert "object_list 只支持 set、delete" in system_prompt


def test_current_output_requirements_can_patch_response_preferences(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "patch_object",
                    "attribute_key": "response_preferences",
                    "updates": {
                        "include_tables": True,
                        "include_charts": True,
                        "conclusion_first": True,
                    },
                    "remove_keys": [],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["结果用表格和图表展示，结论放在最前面。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["response_preferences"] == {
        "include_tables": True,
        "include_charts": True,
        "conclusion_first": True,
    }


def test_budget_analysis_task_does_not_infer_fp_and_a_role(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "primary_analysis_tasks",
                    "items": ["budget_variance_analysis"],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["分析本月预算差异。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"] == {"primary_analysis_tasks": ["budget_variance_analysis"]}
    assert "不能因为用户提出预算差异分析，就推断其岗位一定是 FP&A" in llm.calls[0]["messages"][0]["content"]


def test_current_financial_values_do_not_enter_profile(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["本季度收入为 8,000 万元，毛利率同比下降 3.2%。"],
    )

    assert plan.operations == []
    assert plan.unmapped_facts == []
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "本季度收入为 8,000 万元不能写入用户画像" in system_prompt
    assert "金额统一按万元展示可以写入" in system_prompt


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
            {"role": "assistant", "content": "用户长期关注现金流。"},
            {"role": "user", "content": "请分析本季度现金流。"},
        ],
    )

    payload = json.loads(llm.calls[0]["messages"][1]["content"])
    assert payload["user_messages"] == ["请分析本季度现金流。"]


def test_current_kpi_focus_can_update_preferred_kpis(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_kpis",
                    "items": ["毛利率"],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["这次经营分析重点看毛利率。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["preferred_kpis"] == ["毛利率"]


def test_llm_plan_operations_do_not_include_source_or_confidence(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"},
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["我是负责 FP&A 的财务分析师。"],
    )

    assert plan.operations[0].model_dump() == {
        "operation": "set",
        "attribute_key": "analysis_role",
        "value": "fp_and_a",
    }
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "source_type" not in system_prompt
    assert "confidence" not in system_prompt


def test_llm_patch_object_operation_is_parsed(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "patch_object",
                    "attribute_key": "response_preferences",
                    "updates": {"conclusion_first": True},
                    "remove_keys": [],
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["以后管理报告请把结论放在最前面。"],
    )

    assert plan.operations[0].model_dump() == {
        "operation": "patch_object",
        "attribute_key": "response_preferences",
        "updates": {"conclusion_first": True},
        "remove_keys": [],
    }
    assert "不要复制没有变化的旧字段" in llm.calls[0]["messages"][0]["content"]


def test_inferred_extraction_mode_is_explicitly_unimplemented(profile_manager):
    llm = _RecordingLLM()
    updater = ProfileUpdater(llm, UserProfileConfig(extraction_mode="explicit_and_inferred"))

    with pytest.raises(NotImplementedError, match="explicit_and_inferred"):
        updater.generate_update_plan(
            current_profile=profile_manager.get_profile("user-1"),
            attribute_catalog=profile_manager.list_attributes(),
            messages=["我通常负责财务规划与分析。"],
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
        midterm=SimpleNamespace(enabled=False, short_term_capacity=2),
        background=BackgroundTaskConfig(max_retries=0, poll_interval_seconds=0.01),
    )
    memory.db = db
    memory._profile_manager = ProfileManager(db, profile_config)
    memory._profile_updater = MagicMock()
    plan = ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "preferred_kpis",
                    "items": ["毛利率"],
                }
            ]
        }
    )
    memory._profile_updater.generate_update_plan.return_value = plan
    memory._profile_updater.generate_update_plan_async = AsyncMock(return_value=plan)
    return memory


def test_sync_and_background_profile_updates_are_serialized(db):
    memory = _memory_with_profile_storage(db)
    active = 0
    max_active = 0
    active_guard = threading.Lock()

    def enter_update():
        nonlocal active, max_active
        with active_guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.04)
        with active_guard:
            active -= 1
        return ProfileUpdatePlan()

    memory._profile_updater = SimpleNamespace(
        generate_update_plan=lambda **kwargs: enter_update(),
        generate_update_plan_async=lambda **kwargs: enter_update(),
    )
    profile_job_id = db.create_profile_update_job("user-1", [{"role": "user", "content": "background"}])
    profile_job = db.claim_profile_job(profile_job_id)
    background = threading.Thread(target=memory._background_process_profile, args=(profile_job,))
    foreground = threading.Thread(
        target=memory.update_profile,
        args=("user-1", [{"role": "user", "content": "sync"}]),
    )

    background.start()
    foreground.start()
    background.join(1)
    foreground.join(1)

    assert not background.is_alive()
    assert not foreground.is_alive()
    assert max_active == 1


def test_different_user_profile_updates_can_overlap(db):
    memory = _memory_with_profile_storage(db)
    active = 0
    max_active = 0
    active_guard = threading.Lock()
    both_entered = threading.Event()

    def enter_update():
        nonlocal active, max_active
        with active_guard:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_entered.set()
        both_entered.wait(0.5)
        with active_guard:
            active -= 1
        return ProfileUpdatePlan()

    memory._profile_updater = SimpleNamespace(
        generate_update_plan=lambda **kwargs: enter_update(),
        generate_update_plan_async=lambda **kwargs: enter_update(),
    )
    profile_job_id = db.create_profile_update_job("user-1", [{"role": "user", "content": "background"}])
    profile_job = db.claim_profile_job(profile_job_id)
    first = threading.Thread(target=memory._background_process_profile, args=(profile_job,))
    second = threading.Thread(
        target=memory.update_profile,
        args=("user-2", [{"role": "user", "content": "sync"}]),
    )

    first.start()
    second.start()
    first.join(1)
    second.join(1)

    assert max_active == 2
    assert not first.is_alive()
    assert not second.is_alive()


def test_background_profile_plan_and_job_finish_commit_together(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    asyncio_run = MagicMock(side_effect=AssertionError("background profile processing must stay synchronous"))
    monkeypatch.setattr("mem0.memory.main.asyncio.run", asyncio_run)
    memory._profile_updater.generate_update_plan = MagicMock(
        return_value=ProfileUpdatePlan.model_validate(
            {
                "operations": [
                    {
                        "operation": "set",
                        "attribute_key": "analysis_role",
                        "value": "fp_and_a",
                    }
                ]
            }
        )
    )
    job_id = db.create_profile_update_job("user-1", [{"role": "user", "content": "fp_and_a"}])
    job = db.claim_profile_job(job_id, lease_timeout_seconds=5)

    assert memory._background_process_profile(job) is True
    memory._profile_updater.generate_update_plan.assert_called_once()
    memory._profile_updater.generate_update_plan_async.assert_not_called()
    asyncio_run.assert_not_called()
    assert db.get_background_job(job_id, "profile")["status"] == "succeeded"
    assert memory.get_profile("user-1")["profile"]["analysis_role"] == "fp_and_a"


def test_profile_delete_is_serialized_with_update(db):
    memory = _memory_with_profile_storage(db)
    update_entered = threading.Event()
    release_update = threading.Event()
    delete_entered = threading.Event()
    original_delete = memory.profile_manager.delete_profile

    def blocked_plan(**kwargs):
        update_entered.set()
        assert release_update.wait(1)
        return ProfileUpdatePlan()

    def recording_delete(user_id):
        delete_entered.set()
        return original_delete(user_id)

    memory._profile_updater.generate_update_plan.side_effect = blocked_plan
    memory.profile_manager.delete_profile = recording_delete
    update_thread = threading.Thread(
        target=memory.update_profile,
        args=("user-1", [{"role": "user", "content": "sync"}]),
    )
    delete_thread = threading.Thread(target=memory.delete_profile, args=("user-1",))

    update_thread.start()
    assert update_entered.wait(1)
    delete_thread.start()
    assert not delete_entered.wait(0.05)
    release_update.set()
    update_thread.join(1)
    delete_thread.join(1)

    assert delete_entered.is_set()
    assert not update_thread.is_alive()
    assert not delete_thread.is_alive()


def test_profile_lock_is_released_after_exception(db):
    memory = _memory_with_profile_storage(db)
    memory._profile_updater.generate_update_plan.side_effect = RuntimeError("profile failure")

    with pytest.raises(RuntimeError, match="profile failure"):
        memory.update_profile("user-1", [{"role": "user", "content": "first"}])

    memory._profile_updater.generate_update_plan.side_effect = None
    memory._profile_updater.generate_update_plan.return_value = ProfileUpdatePlan()
    completed = threading.Event()
    thread = threading.Thread(
        target=lambda: (
            memory.update_profile("user-1", [{"role": "user", "content": "second"}]),
            completed.set(),
        )
    )
    thread.start()
    thread.join(1)
    assert completed.is_set()


def _disable_sync_add_notices(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)


def test_disabled_profile_does_not_call_llm():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=False))

    memory._update_profile_after_add("user-1", [{"role": "user", "content": "以后优先看毛利率"}])

    memory._profile_updater.generate_update_plan.assert_not_called()


def test_memory_add_and_profile_apis_share_normalized_user_id(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory._process_evicted_long_term_memories = MagicMock(return_value=[{"id": "memory-1", "event": "ADD"}])
    memory._process_midterm_evictions = MagicMock()
    _disable_sync_add_notices(monkeypatch)

    result = memory.add("以后优先看毛利率", user_id=" user-1 ", run_id="run-1", infer=False)

    assert result["results"] == []
    assert result["background"]["profile_job_id"]
    assert memory.flush_background_tasks(2)
    profile_job = db.get_background_job(result["background"]["profile_job_id"], "profile")
    assert profile_job["user_id"] == "user-1"
    assert memory.get_profile(" user-1 ") == memory.get_profile("user-1")
    assert memory.get_profile("user-1")["profile"]["preferred_kpis"] == ["毛利率"]
    memory.close()


def test_profile_and_memory_layers_use_the_same_normalized_user_id(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory.embedding_model = MagicMock()
    memory.embedding_model.embed.return_value = [0.1, 0.2, 0.3]
    memory._create_memory = MagicMock(return_value="memory-1")
    memory._process_midterm_evictions = MagicMock()
    _disable_sync_add_notices(monkeypatch)

    memory.add(
        [
            {"role": "user", "content": "以后优先看毛利率"},
            {"role": "assistant", "content": "Noted"},
        ],
        user_id=" user-1 ",
        run_id="run-1",
        infer=False,
    )
    second_result = memory.add(
        [
            {"role": "user", "content": "I prefer bonds"},
            {"role": "assistant", "content": "Noted again"},
        ],
        user_id=" user-1 ",
        run_id="run-1",
        infer=False,
    )

    assert memory.flush_background_tasks(2)
    long_term_metadata = memory._create_memory.call_args.args[2]
    assert long_term_metadata["user_id"] == "user-1"
    assert [message["content"] for message in db.get_messages("run_id=run-1&user_id=user-1")] == [
        "I prefer bonds",
        "Noted again",
    ]
    migration_job = db.get_background_job(second_result["background"]["migration_job_id"])
    assert migration_job["filters"]["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_kpis"] == ["毛利率"]
    memory.close()


def test_update_and_delete_profile_normalize_user_id(db):
    memory = _memory_with_profile_storage(db)

    updated = memory.update_profile(" user-1 ", [{"role": "user", "content": "以后优先看毛利率"}])

    assert updated["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_kpis"] == ["毛利率"]
    assert memory.delete_profile(" user-1 ") == 1
    assert memory.get_profile("user-1")["profile"] == {}


@pytest.mark.parametrize("method_name", ["get_profile", "update_profile", "delete_profile"])
def test_profile_apis_reject_internal_whitespace_in_user_id(db, method_name):
    memory = _memory_with_profile_storage(db)
    method = getattr(memory, method_name)
    args = (
        ("user 1", [{"role": "user", "content": "以后优先看毛利率"}])
        if method_name == "update_profile"
        else ("user 1",)
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
        "以后优先看毛利率",
        user_id=" user-1 ",
        agent_id="agent-1",
        memory_type=MemoryType.PROCEDURAL.value,
    )

    assert result["results"] == [{"id": "procedure-1"}]
    assert result["background"]["profile_job_id"]
    assert memory.flush_background_tasks(2)
    assert memory._create_procedural_memory.call_args.kwargs["metadata"]["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["preferred_kpis"] == ["毛利率"]
    memory.close()


def test_explicit_update_is_rejected_without_initializing_updater_when_disabled():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=False))
    memory._profile_updater = None
    memory.llm = MagicMock()

    with pytest.raises(ValueError, match="profile updates are disabled"):
        memory.update_profile("user-1", [{"role": "user", "content": "以后优先看毛利率"}])

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
                {"operation": "set", "attribute_key": "analysis_role", "value": "fp_and_a"},
            ]
        }
    )

    memory._update_profile_after_add(
        "user-1",
        [{"role": "user", "content": "我长期负责 FP&A 分析。"}],
    )

    item = memory.get_profile("user-1", include_metadata=True)["profile"]["analysis_role"]
    assert item["source_type"] == "explicit"
    assert item["confidence"] == 1.0


def test_automatic_update_failure_does_not_escape_add_path(caplog):
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))
    memory._profile_updater.generate_update_plan.side_effect = RuntimeError("LLM unavailable")

    memory._update_profile_after_add("user-1", [{"role": "user", "content": "以后优先看毛利率"}])

    assert "Automatic profile update failed" in caplog.text


def test_profile_failure_preserves_memory_add_result(db, monkeypatch):
    memory = _memory_with_profile_storage(db)
    memory._process_evicted_long_term_memories = MagicMock(return_value=[])
    memory._process_midterm_evictions = MagicMock()
    memory._profile_updater.generate_update_plan.side_effect = RuntimeError("LLM unavailable")
    monkeypatch.setattr("mem0.memory.main.detect_scale_threshold_from_add_result", lambda *args: None)
    monkeypatch.setattr("mem0.memory.main.display_first_run_notice", lambda *args: None)

    result = memory.add("以后优先看毛利率", user_id="user-1", run_id="run-1", infer=False)

    assert result["results"] == []
    assert result["background"]["profile_job_id"]
    assert memory.flush_background_tasks(2)
    assert db.get_background_job(result["background"]["profile_job_id"], "profile")["status"] == "discarded"
    memory.close()


def test_concurrent_appends_do_not_lose_updates(profile_manager):
    barrier = threading.Barrier(3)
    errors = []

    def append(kpi):
        try:
            barrier.wait()
            _apply(
                profile_manager,
                "user-1",
                {"operation": "append_unique", "attribute_key": "preferred_kpis", "items": [kpi]},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(kpi,)) for kpi in ("毛利率", "经营现金流")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(profile_manager.get_profile("user-1")["profile"]["preferred_kpis"]) == {"毛利率", "经营现金流"}


def test_concurrent_appends_across_sqlite_connections_do_not_lose_updates(tmp_path):
    db_path = str(tmp_path / "shared-profile.db")
    first_db = SQLiteManager(db_path)
    second_db = SQLiteManager(db_path)
    first_manager = ProfileManager(first_db, UserProfileConfig())
    second_manager = ProfileManager(second_db, UserProfileConfig())
    barrier = threading.Barrier(3)
    errors = []

    def append(manager, kpi):
        try:
            barrier.wait()
            _apply(
                manager,
                "user-1",
                {"operation": "append_unique", "attribute_key": "preferred_kpis", "items": [kpi]},
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(first_manager, "毛利率")),
        threading.Thread(target=append, args=(second_manager, "经营现金流")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(first_manager.get_profile("user-1")["profile"]["preferred_kpis"]) == {"毛利率", "经营现金流"}
    first_db.close()
    second_db.close()


def test_reset_drops_and_recreates_profile_tables(tmp_path):
    db_path = str(tmp_path / "profile.db")
    manager = SQLiteManager(db_path)
    manager.upsert_user_profile_value("user-1", "analysis_role", "fp_and_a")
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
    assert len(rebuilt.list_profile_attributes()) == 19
    assert rebuilt.get_user_profile_values("user-1") == []
    rebuilt.close()
