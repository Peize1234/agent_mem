import asyncio
import copy
import inspect
import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from mem0.configs.profile_prompts import PROFILE_UPDATE_SYSTEM_PROMPT
from mem0.llms.base import LLMResponse
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.utils import extract_json, remove_code_blocks

logger = logging.getLogger(__name__)


class ProfileLLMOutputError(RuntimeError):
    """Base class for profile extraction failures with safe completion metadata."""

    retryable = False

    def __init__(self, message: str, response: Optional[LLMResponse] = None):
        self.finish_reason = response.finish_reason if response else None
        self.prompt_tokens = response.prompt_tokens if response else None
        self.completion_tokens = response.completion_tokens if response else None
        self.reasoning_tokens = response.reasoning_tokens if response else None
        self.model = response.model if response else None
        details = [
            f"finish_reason={self.finish_reason or 'unknown'}",
            f"model={self.model or 'unknown'}",
            f"prompt_tokens={self.prompt_tokens if self.prompt_tokens is not None else 'unknown'}",
            f"completion_tokens={self.completion_tokens if self.completion_tokens is not None else 'unknown'}",
            f"reasoning_tokens={self.reasoning_tokens if self.reasoning_tokens is not None else 'unknown'}",
        ]
        super().__init__(f"{message} ({', '.join(details)})")


class ProfileLLMOutputTruncatedError(ProfileLLMOutputError):
    """The provider stopped because the profile output token budget was exhausted."""


class ProfileLLMEmptyResponseError(ProfileLLMOutputError):
    """The provider completed without a usable profile response body."""

    retryable = True


class ProfileLLMInvalidJSONError(ProfileLLMOutputError):
    """The provider returned a non-empty body that was not valid JSON."""

    retryable = True


class ProfileLLMSchemaValidationError(ProfileLLMOutputError):
    """The provider returned JSON that did not match the profile plan contract."""


def _catalog_operations(value_type: str, merge_policy: str) -> List[str]:
    if value_type == "object":
        return ["set", "patch_object", "delete"]
    if value_type in {"string_list", "number_list"} and merge_policy == "append_unique":
        return ["set", "append_unique", "remove_items", "delete"]
    return ["set", "delete"]


def _compact_schema_field(schema: Dict[str, Any]) -> Dict[str, Any]:
    field = {"type": schema.get("type")}
    if "enum" in schema:
        field["allowed_values"] = schema["enum"]
    for source, target in (
        ("minimum", "minimum"),
        ("maximum", "maximum"),
        ("minLength", "min_length"),
        ("maxLength", "max_length"),
    ):
        if source in schema:
            field[target] = schema[source]
    return field


def build_profile_prompt_catalog(attribute_catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the minimal attribute contract needed by the profile extraction model."""
    prompt_catalog = []
    for attribute in attribute_catalog:
        schema = attribute.get("value_schema") or {}
        value_type = attribute["value_type"]
        merge_policy = attribute.get("merge_policy", "replace")
        item = {
            "key": attribute["attribute_key"],
            "description": attribute["description"],
            "type": value_type,
            "merge_policy": merge_policy,
            "operations": _catalog_operations(value_type, merge_policy),
        }
        if value_type == "string" and "enum" in schema:
            item["allowed_values"] = schema["enum"]
        elif value_type in {"string_list", "number_list"}:
            item_schema = schema.get("items") or {}
            if "enum" in item_schema:
                item["allowed_values"] = item_schema["enum"]
            constraints = {}
            if "maxItems" in schema:
                constraints["max_items"] = schema["maxItems"]
            if "minLength" in item_schema:
                constraints["item_min_length"] = item_schema["minLength"]
            if "maxLength" in item_schema:
                constraints["item_max_length"] = item_schema["maxLength"]
            if constraints:
                item["constraints"] = constraints
        elif value_type == "object":
            item["fields"] = {
                key: _compact_schema_field(field_schema)
                for key, field_schema in (schema.get("properties") or {}).items()
            }
        prompt_catalog.append(item)
    return prompt_catalog


def _serialize_profile_prompt(payload: Dict[str, Any]) -> str:
    """Serialize only the compact LLM payload without changing canonical storage JSON."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ProfileUpdater:
    def __init__(self, llm, config):
        self.llm = llm
        self.config = config

    @staticmethod
    def _parse_response(response: Any) -> Dict[str, Any]:
        metadata_response = response if isinstance(response, LLMResponse) else None
        if metadata_response and metadata_response.finish_reason == "length":
            raise ProfileLLMOutputTruncatedError("profile LLM output was truncated", metadata_response)

        content = metadata_response.content if metadata_response else response
        if isinstance(content, dict):
            return content
        if content is None or (isinstance(content, str) and not content.strip()):
            raise ProfileLLMEmptyResponseError("profile LLM returned an empty response", metadata_response)
        if not isinstance(content, str):
            raise ProfileLLMSchemaValidationError("profile LLM response must be a JSON object", metadata_response)

        cleaned = remove_code_blocks(content)
        if not cleaned:
            raise ProfileLLMEmptyResponseError("profile LLM returned an empty response", metadata_response)
        try:
            parsed = json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            try:
                parsed = json.loads(extract_json(content), strict=False)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ProfileLLMInvalidJSONError(
                    "profile LLM returned invalid JSON",
                    metadata_response,
                ) from exc
        if not isinstance(parsed, dict):
            raise ProfileLLMSchemaValidationError("profile LLM response must be a JSON object", metadata_response)
        return parsed

    def _profile_llm_options(self) -> Dict[str, Any]:
        options = copy.deepcopy(self.config.llm_request_options)
        options["max_tokens"] = self.config.llm_max_tokens

        if getattr(self.llm, "supports_response_metadata", None) is True:
            options["_return_metadata"] = True
        return options

    def _build_request(
        self,
        current_profile: Dict[str, Any],
        attribute_catalog: List[Dict[str, Any]],
        messages: List[str],
    ) -> Dict[str, Any]:
        if not isinstance(messages, list) or not all(isinstance(message, str) for message in messages):
            raise ValueError("profile updater messages must be a list of user message strings")

        system_prompt = PROFILE_UPDATE_SYSTEM_PROMPT
        if self.config.extraction_mode == "explicit_only":
            system_prompt += "\n\n配置限制：本次仅提取用户明确陈述、直接反馈、纠正或删除，不创建 inferred/repeated 操作。"
        payload = {
            "current_profile": current_profile,
            "attribute_catalog": build_profile_prompt_catalog(attribute_catalog),
            "user_messages": messages,
        }
        return {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": _serialize_profile_prompt(payload)},
            ],
            "response_format": {"type": "json_object"},
            **self._profile_llm_options(),
        }

    def _parse_update_plan(self, response: Any, current_profile: Dict[str, Any]) -> ProfileUpdatePlan:
        metadata_response = response if isinstance(response, LLMResponse) else None
        try:
            plan = ProfileUpdatePlan.model_validate(self._parse_response(response))
        except ValidationError as exc:
            raise ProfileLLMSchemaValidationError(
                f"profile LLM response failed schema validation with {exc.error_count()} error(s)",
                metadata_response,
            ) from exc
        if plan.unmapped_facts:
            user_id = current_profile.get("user_id", "unknown")
            logger.info("Profile extraction produced %s unmapped fact(s) for user %s", len(plan.unmapped_facts), user_id)
        return plan

    def generate_update_plan(
        self,
        *,
        current_profile: Dict[str, Any],
        attribute_catalog: List[Dict[str, Any]],
        messages: List[str],
    ) -> ProfileUpdatePlan:
        """Generate a profile update plan without writing to the database."""
        if not messages:
            return ProfileUpdatePlan()
        request = self._build_request(current_profile, attribute_catalog, messages)
        response = self.llm.generate_response(**request)
        return self._parse_update_plan(response, current_profile)

    async def generate_update_plan_async(
        self,
        *,
        current_profile: Dict[str, Any],
        attribute_catalog: List[Dict[str, Any]],
        messages: List[str],
    ) -> ProfileUpdatePlan:
        """Generate a profile update plan without blocking the event loop."""
        if not messages:
            return ProfileUpdatePlan()

        async_generate = None
        for method_name in ("generate_response_async", "agenerate_response"):
            candidate = getattr(self.llm, method_name, None)
            if inspect.iscoroutinefunction(candidate):
                async_generate = candidate
                break
        if async_generate is None:
            return await asyncio.to_thread(
                self.generate_update_plan,
                current_profile=current_profile,
                attribute_catalog=attribute_catalog,
                messages=messages,
            )

        request = self._build_request(current_profile, attribute_catalog, messages)
        response = await async_generate(**request)
        return self._parse_update_plan(response, current_profile)
