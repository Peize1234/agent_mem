import asyncio
import inspect
import json
import logging
from typing import Any, Dict, List

from mem0.configs.profile_prompts import PROFILE_UPDATE_SYSTEM_PROMPT
from mem0.memory.profile_schema import ProfileUpdatePlan
from mem0.memory.profile_validator import serialize_profile_value
from mem0.memory.utils import extract_json, remove_code_blocks

logger = logging.getLogger(__name__)


class ProfileUpdater:
    def __init__(self, llm, config):
        self.llm = llm
        self.config = config

    @staticmethod
    def _parse_response(response: Any) -> Dict[str, Any]:
        if isinstance(response, dict):
            return response
        if not isinstance(response, str):
            raise ValueError("profile LLM response must be a JSON object")
        cleaned = remove_code_blocks(response)
        try:
            parsed = json.loads(cleaned, strict=False)
        except json.JSONDecodeError:
            parsed = json.loads(extract_json(response), strict=False)
        if not isinstance(parsed, dict):
            raise ValueError("profile LLM response must be a JSON object")
        return parsed

    def _build_request(
        self,
        current_profile: Dict[str, Any],
        attribute_catalog: List[Dict[str, Any]],
        messages: List[str],
    ) -> Dict[str, Any]:
        if self.config.extraction_mode != "explicit_only":
            raise NotImplementedError("profile extraction_mode='explicit_and_inferred' is not implemented")
        if not isinstance(messages, list) or not all(isinstance(message, str) for message in messages):
            raise ValueError("profile updater messages must be a list of user message strings")

        payload = {
            "current_profile": current_profile,
            "available_attributes": attribute_catalog,
            "user_messages": messages,
        }
        return {
            "messages": [
                {"role": "system", "content": PROFILE_UPDATE_SYSTEM_PROMPT},
                {"role": "user", "content": serialize_profile_value(payload)},
            ],
            "response_format": {"type": "json_object"},
        }

    def _parse_update_plan(self, response: Any, current_profile: Dict[str, Any]) -> ProfileUpdatePlan:
        plan = ProfileUpdatePlan.model_validate(self._parse_response(response))
        user_id = current_profile.get("user_id", "unknown")
        for fact in plan.unmapped_facts:
            logger.info("Unmapped profile fact for user %s: %s", user_id, fact.model_dump())
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
