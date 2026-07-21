import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from mem0.memory.profile_schema import ProfileAttributeDefinition, ProfileOperation

_VALUE_TYPE_TO_SCHEMA_TYPE = {
    "string": "string",
    "number": "number",
    "boolean": "boolean",
    "string_list": "array",
    "number_list": "array",
    "object": "object",
    "object_list": "array",
}
_LIST_VALUE_TYPES = {"string_list", "number_list"}


def normalize_memory_identifier(value: Any, name: str, *, allow_none: bool = True) -> Optional[str]:
    """Validate and trim a memory scope identifier."""
    if value is None:
        if allow_none:
            return None
        raise ValueError(f"Invalid {name}: must be a string.")
    if not isinstance(value, str):
        raise ValueError(f"Invalid {name}: must be a string.")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"Invalid {name}: cannot be empty or whitespace-only. Provide a valid identifier.")
    if any(character.isspace() for character in trimmed):
        raise ValueError(f"Invalid {name}: cannot contain whitespace. Provide a valid identifier without spaces.")
    return trimmed


def normalize_profile_user_id(user_id: str) -> str:
    """Validate and normalize a user ID used by profile APIs."""
    return normalize_memory_identifier(user_id, "user_id", allow_none=False)


def serialize_profile_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _load_schema(value_schema_json: Any) -> Dict[str, Any]:
    if isinstance(value_schema_json, str):
        try:
            schema = json.loads(value_schema_json)
        except json.JSONDecodeError as exc:
            raise ValueError("value_schema_json must contain valid JSON") from exc
    else:
        schema = value_schema_json
    if not isinstance(schema, dict):
        raise ValueError("value schema must be a JSON object")
    return schema


def _validate_json_type(value: Any, expected_type: Any, path: str) -> str:
    validators = {
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "array": lambda item: isinstance(item, list),
        "object": lambda item: isinstance(item, dict),
        "null": lambda item: item is None,
    }
    expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
    if not expected_types or any(item not in validators for item in expected_types):
        raise ValueError(f"Unsupported JSON schema type: {expected_type}")

    matched_type = next((item for item in expected_types if validators[item](value)), None)
    if matched_type is None:
        type_label = ", ".join(expected_types)
        raise ValueError(f"{path} must be of type {type_label}")
    if matched_type in {"number", "integer"} and not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    return matched_type


def _validate_schema_node(value: Any, schema: Dict[str, Any], path: str = "value") -> None:
    expected_type = schema.get("type")
    matched_type = None
    if expected_type:
        matched_type = _validate_json_type(value, expected_type, path)

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not one of the allowed enum values")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not match the required constant")

    if matched_type in {"number", "integer"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} must be <= {schema['maximum']}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"{path} must be > {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            raise ValueError(f"{path} must be < {schema['exclusiveMaximum']}")

    if matched_type == "string":
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{path} is shorter than minLength")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} is longer than maxLength")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match the required pattern")

    if matched_type == "array":
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{path} has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{path} has more than maxItems")
        if schema.get("uniqueItems"):
            serialized = [serialize_profile_value(item) for item in value]
            if len(serialized) != len(set(serialized)):
                raise ValueError(f"{path} must contain unique items")
        item_schema = schema.get("items")
        if item_schema is not None:
            if not isinstance(item_schema, dict):
                raise ValueError("array items schema must be an object")
            for index, item in enumerate(value):
                _validate_schema_node(item, item_schema, f"{path}[{index}]")

    if matched_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for key in required:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        for key, item in value.items():
            if key in properties:
                _validate_schema_node(item, properties[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{path}.{key} is not allowed")


def validate_attribute_definition(definition: Any) -> ProfileAttributeDefinition:
    if isinstance(definition, ProfileAttributeDefinition):
        model = definition
    else:
        raw = dict(definition)
        if "value_schema" not in raw and "value_schema_json" in raw:
            raw["value_schema"] = _load_schema(raw.pop("value_schema_json"))
        model = ProfileAttributeDefinition.model_validate(raw)

    schema = _load_schema(model.value_schema)
    expected_schema_type = _VALUE_TYPE_TO_SCHEMA_TYPE[model.value_type]
    if schema.get("type") != expected_schema_type:
        raise ValueError(f"value_schema.type must be {expected_schema_type} for {model.value_type}")
    if model.value_type == "string_list" and schema.get("items", {}).get("type") != "string":
        raise ValueError("string_list schema items must have type string")
    if model.value_type == "number_list" and schema.get("items", {}).get("type") != "number":
        raise ValueError("number_list schema items must have type number")
    if model.value_type == "object_list" and schema.get("items", {}).get("type") != "object":
        raise ValueError("object_list schema items must have type object")
    if model.merge_policy == "append_unique" and model.value_type not in _LIST_VALUE_TYPES:
        raise ValueError("append_unique merge policy is only valid for basic list attributes")
    serialize_profile_value(schema)
    return model


def validate_value_against_schema(
    value: Any,
    value_schema_json: Any,
    value_type: Optional[str] = None,
    attribute_key: Optional[str] = None,
) -> None:
    schema = _load_schema(value_schema_json)
    if value_type is not None:
        expected_type = _VALUE_TYPE_TO_SCHEMA_TYPE.get(value_type)
        if expected_type is None:
            raise ValueError(f"Unsupported profile value_type: {value_type}")
        _validate_json_type(value, expected_type, "value")
        if value_type == "string_list" and not all(isinstance(item, str) for item in value):
            raise ValueError("string_list values must contain only strings")
        if value_type == "number_list" and not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) for item in value
        ):
            raise ValueError("number_list values must contain only finite numbers")
        if value_type == "object_list" and not all(isinstance(item, dict) for item in value):
            raise ValueError("object_list values must contain only objects")
    _validate_schema_node(value, schema)
    if attribute_key == "max_acceptable_loss_ratio" and not 0 <= value <= 1:
        raise ValueError("max_acceptable_loss_ratio must be between 0 and 1")
    serialize_profile_value(value)


def normalize_profile_value(value: Any, definition: Any) -> Any:
    model = validate_attribute_definition(definition)
    validate_value_against_schema(value, model.value_schema, model.value_type, model.attribute_key)
    return json.loads(serialize_profile_value(value))


def validate_operation(definition: Any, operation: ProfileOperation) -> None:
    model = validate_attribute_definition(definition)
    if operation.attribute_key != model.attribute_key:
        raise ValueError(
            f"Profile attribute '{model.attribute_key}' does not support operation '{operation.operation}': "
            "operation key does not match the definition"
        )
    if operation.operation == "set":
        normalize_profile_value(operation.value, model)
        return
    if operation.operation == "delete":
        return
    if model.value_type not in _LIST_VALUE_TYPES:
        raise ValueError(
            f"Profile attribute '{model.attribute_key}' does not support operation '{operation.operation}'"
        )
    if operation.operation == "append_unique" and model.merge_policy != "append_unique":
        raise ValueError(
            f"Profile attribute '{model.attribute_key}' does not support operation 'append_unique' "
            f"with merge_policy '{model.merge_policy}'"
        )
    item_schema = model.value_schema["items"]
    for item in operation.items:
        _validate_schema_node(item, item_schema, "item")


def merge_profile_value(
    current_value: Any,
    operation: ProfileOperation,
    definition: Any,
) -> Tuple[Any, bool]:
    """Return the normalized merged value and whether persistence is required."""
    model = validate_attribute_definition(definition)
    validate_operation(model, operation)
    if operation.operation == "set":
        merged = normalize_profile_value(operation.value, model)
        if current_value is None:
            return merged, True
        current = normalize_profile_value(current_value, model)
        return merged, merged != current
    elif operation.operation == "append_unique":
        current_items = [] if current_value is None else normalize_profile_value(current_value, model)
        merged = list(current_items)
        serialized = {serialize_profile_value(item) for item in merged}
        for item in operation.items:
            item_key = serialize_profile_value(item)
            if item_key not in serialized:
                serialized.add(item_key)
                merged.append(item)
    elif operation.operation == "remove_items":
        if current_value is None:
            return None, False
        current_items = normalize_profile_value(current_value, model)
        removed = {serialize_profile_value(item) for item in operation.items}
        merged = [item for item in current_items if serialize_profile_value(item) not in removed]
    else:
        raise ValueError(
            f"Profile attribute '{model.attribute_key}' does not produce a value for operation '{operation.operation}'"
        )
    normalized = normalize_profile_value(merged, model)
    return normalized, normalized != current_items


def _profile_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"].strip())
    return "\n".join(part for part in parts if part)


def select_profile_user_messages(messages: Any, max_messages: int) -> List[str]:
    """Normalize input and return only the final textual user messages."""
    if isinstance(messages, str):
        candidates = [{"role": "user", "content": messages}]
    elif isinstance(messages, dict):
        candidates = [messages]
    elif isinstance(messages, list):
        candidates = messages
    else:
        raise ValueError("messages must be a string, dictionary, or list")

    user_messages = []
    for message in candidates:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = _profile_message_text(message.get("content"))
        if content:
            user_messages.append(content)
    return user_messages[-max_messages:]
