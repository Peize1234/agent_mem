import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from mem0.configs.base import BackgroundTaskConfig, UserProfileConfig
from mem0.configs.enums import MemoryType
from mem0.configs.predefined_profile_attributes import PREDEFINED_PROFILE_ATTRIBUTES
from mem0.configs.profile_prompts import PROFILE_UPDATE_SYSTEM_PROMPT
from mem0.llms.base import LLMResponse
from mem0.memory.main import Memory
from mem0.memory.profile_manager import ProfileManager
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_updater import (
    ProfileLLMEmptyResponseError,
    ProfileLLMInvalidJSONError,
    ProfileLLMOutputTruncatedError,
    ProfileLLMSchemaValidationError,
    ProfileUpdater,
    build_profile_prompt_catalog,
)
from mem0.memory.storage import SQLiteManager
from mem0.memory.profile_validator import validate_attribute_definition


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
        "recurring_responsibilities",
        "default_report_audience",
        "response_preferences",
        "evidence_requirements",
        "recommendation_preferences",
        "stable_analysis_preferences",
    }
    assert all(item["is_predefined"] for item in attributes)
    assert all(any("\u4e00" <= character <= "\u9fff" for character in item["attribute_name"]) for item in attributes)
    assert all(any("\u4e00" <= character <= "\u9fff" for character in item["description"]) for item in attributes)
    assert not any("_PROFILE_EXTRACTION_RULE" in item["description"] for item in attributes)


def test_task_oriented_profile_attributes_are_removed():
    keys = {item["attribute_key"] for item in PREDEFINED_PROFILE_ATTRIBUTES}
    assert keys.isdisjoint(
        {
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
        }
    )


def test_all_predefined_attribute_definitions_pass_validation():
    validated = [validate_attribute_definition(definition) for definition in PREDEFINED_PROFILE_ATTRIBUTES]

    assert len(validated) == len(PREDEFINED_PROFILE_ATTRIBUTES) == 8


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


def test_profile_operation_requires_source_type():
    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate(
            {
                "operations": [
                    {
                        "operation": "set",
                        "attribute_key": "analysis_role",
                        "value": "financial_analyst",
                        "confidence": 0.8,
                    }
                ]
            }
        )


def test_profile_operation_requires_confidence():
    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate(
            {
                "operations": [
                    {
                        "operation": "set",
                        "attribute_key": "analysis_role",
                        "value": "financial_analyst",
                        "source_type": "inferred",
                    }
                ]
            }
        )


def test_profile_operation_with_explicit_metadata_is_valid():
    plan = ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {
                    "operation": "set",
                    "attribute_key": "analysis_role",
                    "value": "financial_analyst",
                    "source_type": "inferred",
                    "confidence": 0.75,
                }
            ]
        }
    )

    assert plan.operations[0].source_type == "inferred"
    assert plan.operations[0].confidence == 0.75


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_profile_operation_rejects_out_of_range_confidence(confidence):
    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate(
            {
                "operations": [
                    {
                        "operation": "set",
                        "attribute_key": "analysis_role",
                        "value": "financial_analyst",
                        "source_type": "inferred",
                        "confidence": confidence,
                    }
                ]
            }
        )


def test_profile_operation_rejects_unknown_source_type():
    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate(
            {
                "operations": [
                    {
                        "operation": "set",
                        "attribute_key": "analysis_role",
                        "value": "financial_analyst",
                        "source_type": "guessed",
                        "confidence": 0.5,
                    }
                ]
            }
        )


@pytest.mark.parametrize("missing_field", ["source_type", "confidence"])
def test_delete_profile_operation_requires_source_metadata(missing_field):
    operation = {
        "operation": "delete",
        "attribute_key": "analysis_role",
        "source_type": "correction",
        "confidence": 1.0,
    }
    operation.pop(missing_field)

    with pytest.raises(ValidationError):
        ProfileUpdatePlan.model_validate({"operations": [operation]})


def test_replace_list_attribute_rejects_append_unique(db):
    manager = ProfileManager(db, UserProfileConfig(allow_dynamic_attributes=True))
    manager.create_attribute(_attribute_definition("watch_list", value_type="string_list", merge_policy="replace"))

    with pytest.raises(ValueError, match="watch_list.*append_unique"):
        _apply(
            manager,
            "user-1",
            {
                "operation": "append_unique",
                "attribute_key": "watch_list",
                "items": ["ETF"],
                "source_type": "explicit",
                "confidence": 1.0,
            },
        )


def test_profile_is_shared_across_runs_and_isolated_by_user(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "financial_analyst",
            "source_type": "explicit",
            "confidence": 1.0,
        },
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
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "financial_analyst",
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "fp_and_a",
            "source_type": "explicit",
            "confidence": 1.0,
        },
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
            "attribute_key": "recurring_responsibilities",
            "items": ["月度经营分析", "预算差异分析", "月度经营分析"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "recurring_responsibilities",
            "items": ["月度经营分析", "管理层报告准备"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "remove_items",
            "attribute_key": "recurring_responsibilities",
            "items": ["预算差异分析"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    assert profile_manager.get_profile("user-1")["profile"]["recurring_responsibilities"] == [
        "月度经营分析",
        "管理层报告准备",
    ]


def test_append_existing_items_does_not_increment_version(profile_manager):
    operation = {
        "operation": "append_unique",
        "attribute_key": "recurring_responsibilities",
        "items": ["月度经营分析"],
        "source_type": "explicit",
        "confidence": 1.0,
    }
    _apply(profile_manager, "user-1", operation)
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["recurring_responsibilities"]

    _apply(profile_manager, "user-1", operation)

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["recurring_responsibilities"]
    assert after["value_version"] == before["value_version"] == 1


def test_empty_append_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "recurring_responsibilities",
            "items": [],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_remove_items_from_missing_list_does_not_create_profile_value(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "remove_items",
            "attribute_key": "recurring_responsibilities",
            "items": ["月度经营分析"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    assert profile_manager.get_profile("user-1")["profile"] == {}


def test_removing_missing_item_does_not_increment_version(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "append_unique",
            "attribute_key": "recurring_responsibilities",
            "items": ["月度经营分析"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    before = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["recurring_responsibilities"]

    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "remove_items",
            "attribute_key": "recurring_responsibilities",
            "items": ["投融资分析"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    after = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["recurring_responsibilities"]
    assert after["value_version"] == before["value_version"] == 1


def test_setting_same_value_does_not_change_version_or_timestamp(profile_manager, db):
    operation = {
        "operation": "set",
        "attribute_key": "analysis_role",
        "value": "fp_and_a",
        "source_type": "explicit",
        "confidence": 1.0,
    }
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
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "fp_and_a",
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    assert profile_manager.delete_value("user-1", "analysis_role") is True
    assert "analysis_role" not in profile_manager.get_profile("user-1")["profile"]
    assert db.get_profile_attribute("analysis_role") is not None


@pytest.mark.parametrize(
    "operation",
    [
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": 1,
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "unsupported",
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "set",
            "attribute_key": "financial_analysis_expertise_level",
            "value": "master",
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"conclusion_first": "yes"},
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "append_unique",
            "attribute_key": "analysis_role",
            "items": ["fp_and_a"],
            "source_type": "explicit",
            "confidence": 1.0,
        },
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
        {
            "operation": "set",
            "attribute_key": "report_templates",
            "value": templates,
            "source_type": "explicit",
            "confidence": 1.0,
        },
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
            {
                "operation": "set",
                "attribute_key": "report_templates",
                "value": [template],
                "source_type": "explicit",
                "confidence": 1.0,
            },
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
                "source_type": "explicit",
                "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
            "attribute_key": "evidence_requirements",
            "updates": {
                "require_calculation_details": True,
                "require_reconciliation": True,
            },
            "remove_keys": [],
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    assert profile["profile"]["evidence_requirements"] == {
        "require_calculation_details": True,
        "require_reconciliation": True,
    }


def test_patch_object_remove_keys_deletes_optional_field(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "zh-CN", "expression_style": "business"},
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": ["expression_style"],
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
                "source_type": "explicit",
                "confidence": 1.0,
            },
        )

    assert manager.get_profile("user-1")["profile"] == {}


def test_patch_object_rejects_non_object_attribute(profile_manager):
    with pytest.raises(ValueError, match="recurring_responsibilities.*patch_object"):
        _apply(
            profile_manager,
            "user-1",
            {
                "operation": "patch_object",
                "attribute_key": "recurring_responsibilities",
                "updates": {"item": "月度经营分析"},
                "remove_keys": [],
                "source_type": "explicit",
                "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"include_charts": "yes"},
            "remove_keys": [],
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {},
            "remove_keys": ["unexpected"],
            "source_type": "explicit",
            "confidence": 1.0,
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
                "updates": {"expression_style": "business"},
                "remove_keys": ["expression_style"],
                "source_type": "explicit",
                "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
                "source_type": "explicit",
                "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    assert profile_manager.get_profile("user-1")["profile"] == {}

    operation = {
        "operation": "patch_object",
        "attribute_key": "response_preferences",
        "updates": {"include_charts": True},
        "remove_keys": [],
        "source_type": "explicit",
        "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )

    profile = _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"language": "en-US"},
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "patch_object",
            "attribute_key": "response_preferences",
            "updates": {"include_charts": True},
            "remove_keys": [],
            "source_type": "explicit",
            "confidence": 1.0,
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
            "source_type": "explicit",
            "confidence": 1.0,
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


def test_profile_response_with_completion_metadata_parses_normally():
    response = LLMResponse(
        content='{"operations":[],"unmapped_facts":[]}',
        finish_reason="stop",
        prompt_tokens=120,
        completion_tokens=12,
        reasoning_tokens=300,
        model="deepseek-v4-flash",
    )

    assert ProfileUpdater._parse_response(response) == {"operations": [], "unmapped_facts": []}


def test_profile_length_finish_reason_raises_truncated_error():
    response = LLMResponse(
        content="",
        finish_reason="length",
        prompt_tokens=130,
        completion_tokens=4096,
        reasoning_tokens=4000,
        model="deepseek-v4-flash",
    )

    with pytest.raises(ProfileLLMOutputTruncatedError) as raised:
        ProfileUpdater._parse_response(response)

    assert raised.value.finish_reason == "length"
    assert raised.value.completion_tokens == 4096
    assert raised.value.reasoning_tokens == 4000


def test_profile_stop_with_empty_body_raises_empty_response_error():
    response = LLMResponse(content="", finish_reason="stop", model="deepseek-v4-flash")

    with pytest.raises(ProfileLLMEmptyResponseError):
        ProfileUpdater._parse_response(response)


def test_profile_invalid_json_raises_explicit_error():
    response = LLMResponse(content="{invalid", finish_reason="stop", model="deepseek-v4-flash")

    with pytest.raises(ProfileLLMInvalidJSONError):
        ProfileUpdater._parse_response(response)


def test_profile_model_operation_missing_source_metadata_raises_schema_error():
    updater = ProfileUpdater(_RecordingLLM(), UserProfileConfig())
    response = {
        "operations": [
            {
                "operation": "set",
                "attribute_key": "analysis_role",
                "value": "financial_analyst",
                "confidence": 0.8,
            }
        ],
        "unmapped_facts": [],
    }

    with pytest.raises(ProfileLLMSchemaValidationError):
        updater._parse_update_plan(response, {"user_id": "user-1", "profile": {}})


def test_profile_model_request_uses_compact_catalog_and_json():
    updater = ProfileUpdater(_RecordingLLM(), UserProfileConfig())
    current_profile = {"user_id": "用户-1", "profile": {"关注": ["现金流", "毛利率"]}}
    attributes = [
        {
            "attribute_id": 42,
            "attribute_key": "focus",
            "attribute_name": "关注点",
            "attribute_category": "internal",
            "description": "稳定关注点",
            "value_type": "string_list",
            "value_schema": {
                "type": "array",
                "items": {"type": "string", "maxLength": 40},
                "maxItems": 4,
                "uniqueItems": True,
            },
            "merge_policy": "append_unique",
            "is_predefined": True,
            "is_active": True,
            "created_at": "2026-01-01",
            "updated_at": "2026-01-02",
        }
    ]
    user_messages = ['第一行\n第二行，含引号 "、反斜杠 \\ 和 emoji 😀']
    payload = {
        "current_profile": current_profile,
        "attribute_catalog": build_profile_prompt_catalog(attributes),
        "user_messages": user_messages,
    }

    request = updater._build_request(current_profile, attributes, user_messages)
    content = request["messages"][1]["content"]

    assert content == json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert json.loads(content) == payload
    assert "\n" not in content
    assert "\\u" not in content
    assert request["max_tokens"] == 4096
    assert set(payload["attribute_catalog"][0]) == {
        "key",
        "description",
        "type",
        "merge_policy",
        "operations",
        "constraints",
    }
    for omitted in ("attribute_id", "created_at", "updated_at", "is_predefined", "is_active"):
        assert omitted not in content


def test_user_profile_llm_configuration_defaults_and_loading():
    defaults = UserProfileConfig()
    configured = UserProfileConfig.model_validate(
        {
            "enabled": True,
            "llm_max_tokens": 6144,
            "llm_request_options": {"extra_body": {"thinking": {"type": "disabled"}}},
        }
    )

    assert defaults.llm_max_tokens == 4096
    assert defaults.llm_request_options == {}
    assert defaults.extraction_mode == "explicit_and_inferred"
    assert configured.llm_max_tokens == 6144
    assert configured.llm_request_options["extra_body"]["thinking"]["type"] == "disabled"


@pytest.mark.parametrize(
    "reserved_key",
    ["messages", "response_format", "tools", "tool_choice", "max_tokens", "_return_metadata"],
)
def test_profile_llm_request_options_reject_reserved_request_fields(reserved_key):
    with pytest.raises(ValidationError, match="cannot override reserved profile request fields"):
        UserProfileConfig(llm_request_options={reserved_key: "not-allowed"})


def test_profile_request_uses_dedicated_tokens_without_adding_thinking(profile_manager):
    llm = _RecordingLLM()
    llm.config = SimpleNamespace(model="deepseek-v4-flash", max_tokens=512)
    llm.supports_response_metadata = True
    updater = ProfileUpdater(llm, UserProfileConfig(llm_max_tokens=4096))

    request = updater._build_request(
        profile_manager.get_profile("user-1"),
        profile_manager.list_attributes(),
        ["我长期负责集团月度经营分析。"],
    )

    assert request["max_tokens"] == 4096
    assert "extra_body" not in request
    assert request["_return_metadata"] is True
    assert llm.config.max_tokens == 512


@pytest.mark.parametrize("thinking_type", ["enabled", "disabled"])
def test_profile_request_preserves_explicit_thinking_mode(profile_manager, thinking_type):
    llm = _RecordingLLM()
    llm.config = SimpleNamespace(model="deepseek-v4-flash", max_tokens=512)
    llm.supports_response_metadata = True
    configured_options = {"extra_body": {"thinking": {"type": thinking_type}}}
    config = UserProfileConfig(llm_max_tokens=4096, llm_request_options=configured_options)
    updater = ProfileUpdater(llm, config)

    request = updater._build_request(
        profile_manager.get_profile("user-1"),
        profile_manager.list_attributes(),
        ["我长期负责集团月度经营分析。"],
    )

    assert request["max_tokens"] == 4096
    assert request["extra_body"] == {"thinking": {"type": thinking_type}}
    assert request["_return_metadata"] is True


def test_profile_llm_options_deep_copy_request_options():
    configured_options = {
        "extra_body": {"thinking": {"type": "enabled"}},
        "seed": 7,
    }
    config = UserProfileConfig(llm_request_options=configured_options)
    updater = ProfileUpdater(_RecordingLLM(), config)

    options = updater._profile_llm_options()
    options["extra_body"]["thinking"]["type"] = "disabled"
    options["seed"] = 8

    assert config.llm_request_options == configured_options


def test_full_prompt_catalog_is_materially_smaller_than_database_catalog(profile_manager):
    attributes = profile_manager.list_attributes()
    current_profile = profile_manager.get_profile("user-1")
    messages = ["我长期负责集团月度经营分析，回答时直接给结论并附关键计算过程。"]
    updater = ProfileUpdater(_RecordingLLM(), UserProfileConfig())
    request = updater._build_request(current_profile, attributes, messages)
    compact_user_prompt = request["messages"][1]["content"]
    previous_user_prompt = json.dumps(
        {
            "current_profile": current_profile,
            "available_attributes": attributes,
            "user_messages": messages,
        },
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )

    assert len(compact_user_prompt) < len(previous_user_prompt) * 0.7
    assert len(request["messages"][0]["content"] + compact_user_prompt) < 10000


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

    assert plans[0]["operations"][0]["attribute_key"] == "recurring_responsibilities"
    assert plans[1] == {"operations": [], "unmapped_facts": []}
    assert plans[2]["operations"][0]["updates"]["skip_basic_background"] is True
    assert plans[3]["operations"][0]["attribute_key"] == "stable_analysis_preferences"
    assert plans[4]["operations"][0]["operation"] == "delete"
    assert plans[4]["operations"][1]["attribute_key"] == "recurring_responsibilities"


def test_stable_analysis_preferences_are_bounded(db):
    definition = db.get_profile_attribute("stable_analysis_preferences")

    assert definition["value_schema"]["maxItems"] == 8
    assert definition["value_schema"]["items"]["maxLength"] == 120
    assert definition["merge_policy"] == "append_unique"


def test_assistant_messages_do_not_enter_profile_update():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=True))

    memory._update_profile_after_add(
        "user-1",
        [{"role": "assistant", "content": "The user prefers stocks."}],
    )

    memory._profile_updater.generate_update_plan.assert_not_called()


def test_single_company_analysis_does_not_update_profile(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["分析华辰智能装备有限公司本季度收入结构，重点看工业机器人控制器毛利率，并评估本次授信风险。"],
    )

    assert plan == ProfileUpdatePlan()
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "用户画像描述用户本人，而不是用户当前正在处理的任务" in system_prompt
    assert "公司、产品、指标、数值、时间范围" in system_prompt
    assert "不能仅根据当前问题主题推断具体岗位" in system_prompt


def test_single_format_requirement_does_not_update_profile(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["这次结果用表格展示，结论放在前面。"],
    )

    assert plan == ProfileUpdatePlan()
    assert "单次格式要求默认不更新" in llm.calls[0]["messages"][0]["content"]


def test_budget_analysis_task_does_not_infer_fp_and_a_role(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["分析本月预算差异。"],
    )
    assert plan == ProfileUpdatePlan()
    assert "不能仅根据当前问题主题推断具体岗位" in llm.calls[0]["messages"][0]["content"]


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
    assert "不能把当前任务中的实体、时间、数值" in system_prompt


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


def test_current_kpi_focus_does_not_create_stable_preference(profile_manager):
    llm = _RecordingLLM({"operations": [], "unmapped_facts": []})
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["这次经营分析重点看毛利率。"],
    )
    assert plan == ProfileUpdatePlan()


def test_llm_plan_operations_persist_source_and_confidence(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "set",
                    "attribute_key": "analysis_role",
                    "value": "fp_and_a",
                    "source_type": "inferred",
                    "confidence": 0.78,
                },
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

    profile_manager.apply_update_plan("user-1", plan)
    stored = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["analysis_role"]
    assert stored["source_type"] == "inferred"
    assert stored["confidence"] == 0.78
    system_prompt = llm.calls[0]["messages"][0]["content"]
    assert "source_type" in system_prompt
    assert "confidence" in system_prompt


def test_llm_patch_object_operation_is_parsed(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "patch_object",
                    "attribute_key": "response_preferences",
                    "updates": {"conclusion_first": True},
                    "remove_keys": [],
                    "source_type": "explicit",
                    "confidence": 0.99,
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
        "source_type": "explicit",
        "confidence": 0.99,
    }
    assert "updates 仅含变化字段" in llm.calls[0]["messages"][0]["content"]


def test_inferred_extraction_mode_is_supported(profile_manager):
    llm = _RecordingLLM()
    updater = ProfileUpdater(llm, UserProfileConfig(extraction_mode="explicit_and_inferred"))

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["我通常负责财务规划与分析。"],
    )

    assert plan == ProfileUpdatePlan()
    assert len(llm.calls) == 1
    assert "合理推断" in llm.calls[0]["messages"][0]["content"]


def test_direct_response_feedback_updates_preferences(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "patch_object",
                    "attribute_key": "response_preferences",
                    "updates": {
                        "skip_basic_background": True,
                        "conclusion_first": True,
                        "prefer_actionable_output": True,
                        "detail_level": "concise",
                    },
                    "remove_keys": [],
                    "source_type": "explicit",
                    "confidence": 0.99,
                },
                {
                    "operation": "patch_object",
                    "attribute_key": "recommendation_preferences",
                    "updates": {
                        "include_actionable_recommendations": True,
                        "include_validation_steps": True,
                    },
                    "remove_keys": [],
                    "source_type": "explicit",
                    "confidence": 0.99,
                },
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["不要再解释基础背景，直接告诉我问题、修改方案和验证方法。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["response_preferences"]["skip_basic_background"] is True
    assert profile["profile"]["recommendation_preferences"]["include_validation_steps"] is True
    assert all(operation.source_type == "explicit" for operation in plan.operations)


def test_explicit_recurring_responsibility_and_inferred_audience(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "recurring_responsibilities",
                    "items": ["集团月度预算差异分析和经营分析会材料准备"],
                    "source_type": "explicit",
                    "confidence": 0.99,
                },
                {
                    "operation": "set",
                    "attribute_key": "default_report_audience",
                    "value": "business_management",
                    "source_type": "inferred",
                    "confidence": 0.68,
                },
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["我每个月负责集团预算差异分析和经营分析会材料。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["recurring_responsibilities"] == ["集团月度预算差异分析和经营分析会材料准备"]
    assert profile["profile"]["default_report_audience"] == "business_management"


def test_strong_work_context_does_not_assert_specific_role(profile_manager):
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "recurring_responsibilities",
                    "items": ["经营分析相关材料准备"],
                    "source_type": "inferred",
                    "confidence": 0.64,
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["我要给管理层准备下周经营分析会材料，重点解释预算差异和现金流偏差。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert "analysis_role" not in profile["profile"]
    assert profile["profile"]["recurring_responsibilities"] == ["经营分析相关材料准备"]
    assert "下周" not in json.dumps(profile, ensure_ascii=False)


def test_repeated_behavior_is_abstracted_into_stable_analysis_preference(profile_manager):
    preference = "经营分析时重视盈利质量及利润与现金流的匹配关系"
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "append_unique",
                    "attribute_key": "stable_analysis_preferences",
                    "items": [preference],
                    "source_type": "repeated",
                    "confidence": 0.9,
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["不要只看利润，还要看经营现金流。", "检查利润和现金流是否匹配。", "这类分析还要判断盈利质量。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"]["stable_analysis_preferences"] == [preference]
    assert profile_manager.get_profile("user-1", include_metadata=True)["profile"]["stable_analysis_preferences"][
        "source_type"
    ] == "repeated"


def test_user_correction_removes_conflicting_role(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "investment_analyst",
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "delete",
                    "attribute_key": "analysis_role",
                    "source_type": "correction",
                    "confidence": 1.0,
                },
                {
                    "operation": "append_unique",
                    "attribute_key": "recurring_responsibilities",
                    "items": ["内部经营分析"],
                    "source_type": "correction",
                    "confidence": 0.99,
                },
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["我不是投资分析师，我现在主要负责内部经营分析。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert "analysis_role" not in profile["profile"]
    metadata = profile_manager.get_profile("user-1", include_metadata=True)["profile"]["recurring_responsibilities"]
    assert metadata["source_type"] == "correction"


def test_user_can_forget_report_format_preferences(profile_manager):
    _apply(
        profile_manager,
        "user-1",
        {
            "operation": "set",
            "attribute_key": "analysis_role",
            "value": "financial_analyst",
            "source_type": "explicit",
            "confidence": 1.0,
        },
        {
            "operation": "set",
            "attribute_key": "response_preferences",
            "value": {"include_tables": True, "conclusion_first": True},
            "source_type": "explicit",
            "confidence": 1.0,
        },
    )
    llm = _RecordingLLM(
        {
            "operations": [
                {
                    "operation": "delete",
                    "attribute_key": "response_preferences",
                    "source_type": "correction",
                    "confidence": 1.0,
                }
            ],
            "unmapped_facts": [],
        }
    )
    updater = ProfileUpdater(llm, UserProfileConfig())

    plan = updater.generate_update_plan(
        current_profile=profile_manager.get_profile("user-1"),
        attribute_catalog=profile_manager.list_attributes(),
        messages=["忘掉我之前关于报告格式的偏好。"],
    )
    profile = profile_manager.apply_update_plan("user-1", plan)

    assert profile["profile"] == {"analysis_role": "financial_analyst"}


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
                    "attribute_key": "recurring_responsibilities",
                    "items": ["月度经营分析"],
                    "source_type": "explicit",
                    "confidence": 1.0,
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
                        "source_type": "explicit",
                        "confidence": 1.0,
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
    assert memory.get_profile("user-1")["profile"]["recurring_responsibilities"] == ["月度经营分析"]
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
    assert memory.get_profile("user-1")["profile"]["recurring_responsibilities"] == ["月度经营分析"]
    memory.close()


def test_update_and_delete_profile_normalize_user_id(db):
    memory = _memory_with_profile_storage(db)

    updated = memory.update_profile(" user-1 ", [{"role": "user", "content": "以后优先看毛利率"}])

    assert updated["user_id"] == "user-1"
    assert memory.get_profile("user-1")["profile"]["recurring_responsibilities"] == ["月度经营分析"]
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
    assert memory.get_profile("user-1")["profile"]["recurring_responsibilities"] == ["月度经营分析"]
    memory.close()


def test_explicit_update_is_rejected_without_initializing_updater_when_disabled():
    memory = _memory_for_automatic_update(UserProfileConfig(enabled=False))
    memory._profile_updater = None
    memory.llm = MagicMock()

    with pytest.raises(ValueError, match="profile updates are disabled"):
        memory.update_profile("user-1", [{"role": "user", "content": "以后优先看毛利率"}])

    assert memory._profile_updater is None
    memory.llm.generate_response.assert_not_called()


def test_automatic_update_persists_supplied_explicit_metadata(db):
    config = UserProfileConfig(enabled=True)
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(profile=config)
    memory._profile_manager = ProfileManager(db, config)
    memory._profile_updater = MagicMock()
    memory._profile_updater.generate_update_plan.return_value = ProfileUpdatePlan.model_validate(
        {
            "operations": [
                {
                    "operation": "set",
                    "attribute_key": "analysis_role",
                    "value": "fp_and_a",
                    "source_type": "explicit",
                    "confidence": 1.0,
                },
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

    def append(responsibility):
        try:
            barrier.wait()
            _apply(
                profile_manager,
                "user-1",
                {
                    "operation": "append_unique",
                    "attribute_key": "recurring_responsibilities",
                    "items": [responsibility],
                    "source_type": "explicit",
                    "confidence": 1.0,
                },
            )
        except Exception as exc:
            errors.append(exc)

    responsibilities = ("月度经营分析", "预算差异分析")
    threads = [threading.Thread(target=append, args=(item,)) for item in responsibilities]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(profile_manager.get_profile("user-1")["profile"]["recurring_responsibilities"]) == set(
        responsibilities
    )


def test_concurrent_appends_across_sqlite_connections_do_not_lose_updates(tmp_path):
    db_path = str(tmp_path / "shared-profile.db")
    first_db = SQLiteManager(db_path)
    second_db = SQLiteManager(db_path)
    first_manager = ProfileManager(first_db, UserProfileConfig())
    second_manager = ProfileManager(second_db, UserProfileConfig())
    barrier = threading.Barrier(3)
    errors = []

    def append(manager, responsibility):
        try:
            barrier.wait()
            _apply(
                manager,
                "user-1",
                {
                    "operation": "append_unique",
                    "attribute_key": "recurring_responsibilities",
                    "items": [responsibility],
                    "source_type": "explicit",
                    "confidence": 1.0,
                },
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=append, args=(first_manager, "月度经营分析")),
        threading.Thread(target=append, args=(second_manager, "预算差异分析")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(first_manager.get_profile("user-1")["profile"]["recurring_responsibilities"]) == {
        "月度经营分析",
        "预算差异分析",
    }
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
    assert len(rebuilt.list_profile_attributes()) == 8
    assert rebuilt.get_user_profile_values("user-1") == []
    rebuilt.close()
