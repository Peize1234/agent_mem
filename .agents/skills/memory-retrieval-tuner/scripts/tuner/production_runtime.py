from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from mem0 import AsyncMemory
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT
from mem0.utils.factory import VectorStoreFactory, _configure_native_timeout


_ORIGINAL_VECTOR_STORE_CREATE = VectorStoreFactory.create
LOGGER = logging.getLogger("memory_retrieval_tuner.production_runtime")


def _config_to_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    for method_name in ("model_dump", "dict"):
        method = getattr(config, method_name, None)
        if callable(method):
            value = method()
            return dict(value) if isinstance(value, dict) else {}
    return {}


def _uses_local_qdrant(config: Any) -> bool:
    values = _config_to_dict(config)
    remote = values.get("client") or values.get("url") or (values.get("host") and values.get("port"))
    return bool(values.get("path")) and not bool(remote or values.get("api_key"))


def _create_vector_store_for_tuner(
    provider_name: str,
    config: Any,
    *,
    timeout_seconds: float | None = None,
) -> Any:
    """Keep local Qdrant path mode when the production factory receives a timeout."""
    if str(provider_name).strip().lower() == "qdrant" and _uses_local_qdrant(config):
        instance = _ORIGINAL_VECTOR_STORE_CREATE(provider_name, config, timeout_seconds=None)
        return _configure_native_timeout(instance, timeout_seconds)
    return _ORIGINAL_VECTOR_STORE_CREATE(provider_name, config, timeout_seconds=timeout_seconds)


class TunerAsyncMemory(AsyncMemory):
    """Production AsyncMemory with lifecycle cleanup for tuner-only factory patches."""

    def close(self) -> bool:
        try:
            return super().close()
        finally:
            for patcher in reversed(getattr(self, "_tuner_patchers", [])):
                patcher.stop()
            self._tuner_patchers = []


class DeterministicTunerLLM:
    """Infrastructure-only LLM used by smoke tests; it is never an accuracy baseline."""

    supports_response_metadata = False

    @staticmethod
    def _keywords(text: str, limit: int = 8) -> list[str]:
        values = re.findall(r"[A-Za-z0-9_\-%.]+|[\u4e00-\u9fff]{2,8}", text)
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))[:limit]

    @staticmethod
    def _safe_json(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def generate_response(self, messages: Any, response_format: Any = None, **kwargs: Any) -> str:
        del response_format, kwargs
        messages = messages or []
        system = str(messages[0].get("content", "")) if messages else ""
        user = str(messages[-1].get("content", "")) if messages else ""
        payload = self._safe_json(user)
        if payload.get("research_decision_schema") and payload.get("legal_actions"):
            deterministic = list(payload.get("deterministic_plan") or [])
            selected = [str(item.get("action_id")) for item in deterministic if item.get("action_id")]
            if not selected:
                selected = [
                    str(item["action_id"]) for item in payload["legal_actions"] if item.get("action_type") == "branch"
                ][:1]
            selected_ids = set(selected)
            deprioritized = [
                {
                    "action_id": str(item["action_id"]),
                    "reason": "mock runtime follows the deterministic plan",
                }
                for item in payload["legal_actions"]
                if item.get("action_type") == "branch" and str(item.get("action_id")) not in selected_ids
            ]
            return json.dumps(
                {
                    "action_ids": selected,
                    "rationale": "mock runtime follows the deterministic plan",
                    "deprioritized": deprioritized,
                },
                ensure_ascii=False,
            )
        if "resolved_query" in system and payload.get("current_query"):
            return json.dumps({"resolved_query": str(payload["current_query"])}, ensure_ascii=False)
        if "attribute_catalog" in user and "current_profile" in user:
            return json.dumps({"operations": [], "unmapped_facts": []}, ensure_ascii=False)
        if "existing_session" in payload and "new_page" in payload:
            existing = payload.get("existing_session") or {}
            page = payload.get("new_page") or {}
            summary = " ".join(
                dict.fromkeys(
                    value
                    for value in (str(existing.get("summary") or "").strip(), str(page.get("summary") or "").strip())
                    if value
                )
            )
            keywords = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in [*(existing.get("keywords") or []), *(page.get("keywords") or [])]
                    if str(value).strip()
                )
            )
            return json.dumps({"summary": summary[:1000], "keywords": keywords[:12]}, ensure_ascii=False)
        if "summary" in system.lower() and "keyword" in system.lower():
            return json.dumps({"summary": user.strip()[:240], "keywords": self._keywords(user)}, ensure_ascii=False)
        memory_text = user.strip()[:800]
        return json.dumps({"memory": [{"text": memory_text}]} if memory_text else {"memory": []}, ensure_ascii=False)

    async def generate_response_async(self, *args: Any, **kwargs: Any) -> str:
        return self.generate_response(*args, **kwargs)

    async def agenerate_response(self, *args: Any, **kwargs: Any) -> str:
        return self.generate_response(*args, **kwargs)


class TunerPolicyLLM:
    """Apply explicit experiment request policy without changing production code."""

    def __init__(
        self,
        delegate: Any,
        *,
        observability_enabled: bool,
        deepseek_midterm_non_thinking: bool,
        deepseek_longterm_non_thinking: bool,
    ):
        self._delegate = delegate
        self._observability_enabled = observability_enabled
        self._deepseek_midterm_non_thinking = deepseek_midterm_non_thinking
        self._deepseek_longterm_non_thinking = deepseek_longterm_non_thinking

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    @staticmethod
    def _operation(messages: list[dict[str, Any]]) -> str | None:
        system = str(messages[0].get("content", "")) if messages else ""
        if system.strip() == MIDTERM_PAGE_SUMMARY_PROMPT.strip():
            return "midterm_page_summary"
        if system.strip() == MIDTERM_SESSION_MERGE_PROMPT.strip():
            return "midterm_session_merge"
        if system.startswith(ADDITIVE_EXTRACTION_PROMPT):
            return "longterm_extraction"
        return None

    def _request_kwargs(self, operation: str | None, kwargs: dict[str, Any]) -> dict[str, Any]:
        disable = (
            self._deepseek_midterm_non_thinking and operation in {"midterm_page_summary", "midterm_session_merge"}
        ) or (self._deepseek_longterm_non_thinking and operation == "longterm_extraction")
        if not disable:
            return kwargs
        updated = dict(kwargs)
        extra_body = deepcopy(updated.get("extra_body") or {})
        thinking = deepcopy(extra_body.get("thinking") or {})
        thinking["type"] = "disabled"
        extra_body["thinking"] = thinking
        updated["extra_body"] = extra_body
        return updated

    def generate_response(self, messages: Any, response_format: Any = None, **kwargs: Any) -> Any:
        messages = messages or []
        operation = self._operation(messages)
        request_kwargs = self._request_kwargs(operation, kwargs)
        started = time.perf_counter()
        try:
            return self._delegate.generate_response(messages, response_format=response_format, **request_kwargs)
        finally:
            if operation and self._observability_enabled:
                LOGGER.info("LLM operation=%s elapsed_ms=%.1f", operation, (time.perf_counter() - started) * 1000.0)


def create_tuner_policy_llm(config: dict[str, Any], *, llm_mode: str) -> Any:
    """Create the configured LLM without constructing a Memory/vector-store runtime."""

    mode = str(llm_mode or "real").strip().lower()
    if mode not in {"real", "mock"}:
        raise ValueError("llm_mode must be real or mock")
    if mode == "mock":
        return DeterministicTunerLLM()
    from mem0.configs.base import MemoryConfig
    from mem0.utils.factory import LlmFactory

    from .benchmark_support import expand_env_placeholders

    parsed = MemoryConfig(**expand_env_placeholders(deepcopy(config)))
    delegate = LlmFactory.create(parsed.llm.provider, parsed.llm.config)
    runtime = dict(config.get("benchmark_runtime") or {})
    return TunerPolicyLLM(
        delegate,
        observability_enabled=bool(runtime.get("llm_observability", False)),
        deepseek_midterm_non_thinking=bool(runtime.get("deepseek_midterm_non_thinking", False)),
        deepseek_longterm_non_thinking=bool(runtime.get("deepseek_longterm_non_thinking", False)),
    )


def create_production_memory(config: dict[str, Any], *, llm_mode: str) -> TunerAsyncMemory:
    """Construct the real production memory graph with tuner-only isolation hooks."""
    mode = str(llm_mode or "real").strip().lower()
    if mode not in {"real", "mock"}:
        raise ValueError("llm_mode must be real or mock")
    runtime_config = deepcopy(config)
    benchmark_runtime = runtime_config.pop("benchmark_runtime", {}) or {}
    observability_enabled = bool(benchmark_runtime.get("llm_observability", False))
    deepseek_midterm_non_thinking = bool(benchmark_runtime.get("deepseek_midterm_non_thinking", False))
    deepseek_longterm_non_thinking = bool(benchmark_runtime.get("deepseek_longterm_non_thinking", False))
    llm_provider = str((runtime_config.get("llm") or {}).get("provider") or "").strip().lower()
    if (deepseek_midterm_non_thinking or deepseek_longterm_non_thinking) and llm_provider != "deepseek":
        raise ValueError("DeepSeek non-thinking policy requires llm.provider=deepseek")
    patchers = [
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.VectorStoreFactory.create", side_effect=_create_vector_store_for_tuner),
    ]
    if mode == "mock":
        patchers.append(patch("mem0.memory.main.LlmFactory.create", return_value=DeterministicTunerLLM()))
    for patcher in patchers:
        patcher.start()
    try:
        memory = TunerAsyncMemory.from_config(runtime_config)
    except Exception:
        for patcher in reversed(patchers):
            patcher.stop()
        raise
    memory.llm = TunerPolicyLLM(
        memory.llm,
        observability_enabled=observability_enabled,
        deepseek_midterm_non_thinking=deepseek_midterm_non_thinking,
        deepseek_longterm_non_thinking=deepseek_longterm_non_thinking,
    )
    if getattr(memory, "_midterm_updater", None) is not None:
        memory._midterm_updater.llm = memory.llm
    if getattr(memory, "_profile_updater", None) is not None:
        memory._profile_updater.llm = memory.llm
    memory._tuner_patchers = patchers
    return memory
