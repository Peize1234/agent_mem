from __future__ import annotations

import json
import logging
import re
import time
from copy import deepcopy
from contextvars import ContextVar
from typing import Any
from unittest.mock import patch

from mem0 import AsyncMemory
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT
from mem0.utils.factory import VectorStoreFactory, _configure_native_timeout


_ORIGINAL_VECTOR_STORE_CREATE = VectorStoreFactory.create
LOGGER = logging.getLogger("benchmark_memory")


def _config_to_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, dict):
        return dict(config)
    model_dump = getattr(config, "model_dump", None)
    if callable(model_dump):
        value = model_dump()
        return dict(value) if isinstance(value, dict) else {}
    legacy_dict = getattr(config, "dict", None)
    if callable(legacy_dict):
        value = legacy_dict()
        return dict(value) if isinstance(value, dict) else {}
    return {}


def _uses_local_qdrant(config: Any) -> bool:
    values = _config_to_dict(config)
    has_path = bool(values.get("path"))
    has_remote_endpoint = bool(
        values.get("client")
        or values.get("url")
        or (values.get("host") and values.get("port"))
        or values.get("api_key")
    )
    return has_path and not has_remote_endpoint


def _create_vector_store_for_benchmark(
    provider_name: str,
    config: Any,
    *,
    timeout_seconds: float | None = None,
):
    """兼容当前仓库中本地 Qdrant path 与 timeout_seconds 的冲突。

    原实现会把 timeout_seconds 传入 Qdrant 构造函数。Qdrant 构造函数只要
    收到任意连接参数就不会使用 path，导致本地模式误连 localhost:6333。
    Benchmark 仅在明确配置为本地 Qdrant 时延后应用 timeout，不修改业务代码。
    """
    if str(provider_name).strip().lower() == "qdrant" and _uses_local_qdrant(config):
        instance = _ORIGINAL_VECTOR_STORE_CREATE(
            provider_name,
            config,
            timeout_seconds=None,
        )
        return _configure_native_timeout(instance, timeout_seconds)

    return _ORIGINAL_VECTOR_STORE_CREATE(
        provider_name,
        config,
        timeout_seconds=timeout_seconds,
    )


def _value_char_count(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    content = getattr(value, "content", None)
    if content is not None and content is not value:
        return _value_char_count(content)
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


class BenchmarkObservedLLM:
    """实验专用 LLM 包装器：记录耗时，并可关闭 DeepSeek 记忆提取思考。"""

    def __init__(
        self,
        delegate: Any,
        *,
        call_context: ContextVar[dict[str, Any] | None],
        observability_enabled: bool,
        deepseek_midterm_non_thinking: bool,
        deepseek_longterm_non_thinking: bool,
    ):
        self._delegate = delegate
        self._call_context = call_context
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

    @staticmethod
    def _disable_thinking(kwargs: dict[str, Any]) -> dict[str, Any]:
        updated = dict(kwargs)
        extra_body = deepcopy(updated.get("extra_body") or {})
        thinking = deepcopy(extra_body.get("thinking") or {})
        thinking["type"] = "disabled"
        extra_body["thinking"] = thinking
        updated["extra_body"] = extra_body
        return updated

    def generate_response(self, messages, response_format=None, **kwargs):
        messages = messages or []
        operation = self._operation(messages)
        request_kwargs = dict(kwargs)
        if self._deepseek_midterm_non_thinking and operation in {
            "midterm_page_summary",
            "midterm_session_merge",
        }:
            request_kwargs = self._disable_thinking(request_kwargs)
        if self._deepseek_longterm_non_thinking and operation == "longterm_extraction":
            request_kwargs = self._disable_thinking(request_kwargs)

        if operation is None or not self._observability_enabled:
            return self._delegate.generate_response(
                messages,
                response_format=response_format,
                **request_kwargs,
            )

        context = self._call_context.get() or {}
        source_job_id = str(context.get("source_job_id") or "-")
        stage = str(context.get("stage") or "-")
        model = getattr(getattr(self._delegate, "config", None), "model", None) or type(self._delegate).__name__
        prompt_chars = sum(_value_char_count(message.get("content")) for message in messages)
        thinking = (
            ((request_kwargs.get("extra_body") or {}).get("thinking") or {}).get("type")
            if isinstance(request_kwargs.get("extra_body"), dict)
            else None
        )
        started = time.perf_counter()
        try:
            response = self._delegate.generate_response(
                messages,
                response_format=response_format,
                **request_kwargs,
            )
        except Exception as exc:
            LOGGER.warning(
                "Benchmark LLM call failed operation=%s source_job_id=%s stage=%s model=%s thinking=%s "
                "prompt_chars=%s elapsed_ms=%.1f error_type=%s",
                operation,
                source_job_id,
                stage,
                model,
                thinking or "default",
                prompt_chars,
                (time.perf_counter() - started) * 1000.0,
                type(exc).__name__,
            )
            raise

        LOGGER.info(
            "Benchmark LLM call completed operation=%s source_job_id=%s stage=%s model=%s thinking=%s "
            "prompt_chars=%s response_chars=%s elapsed_ms=%.1f",
            operation,
            source_job_id,
            stage,
            model,
            thinking or "default",
            prompt_chars,
            _value_char_count(response),
            (time.perf_counter() - started) * 1000.0,
        )
        return response


class BenchmarkAsyncMemory(AsyncMemory):
    """实验专用 AsyncMemory 子类，只捕获原始召回上下文，不改变正式逻辑。"""

    def __init__(self, config):
        self._benchmark_trace: ContextVar[dict[str, Any] | None] = ContextVar(
            f"benchmark_trace_{id(self)}",
            default=None,
        )
        self._benchmark_llm_context: ContextVar[dict[str, Any] | None] = ContextVar(
            f"benchmark_llm_context_{id(self)}",
            default=None,
        )
        super().__init__(config)

    def _run_background_stage_with_context(self, stage: str, job: dict[str, Any], callback, *args) -> Any:
        token = self._benchmark_llm_context.set(
            {
                "source_job_id": job.get("job_id"),
                "stage": stage,
            }
        )
        try:
            return callback(job, *args)
        finally:
            self._benchmark_llm_context.reset(token)

    def _background_process_midterm(self, job, messages, degraded: bool) -> None:
        return self._run_background_stage_with_context(
            "midterm",
            job,
            super()._background_process_midterm,
            messages,
            degraded,
        )

    def _background_process_longterm(self, job, messages, degraded: bool) -> None:
        return self._run_background_stage_with_context(
            "longterm",
            job,
            super()._background_process_longterm,
            messages,
            degraded,
        )

    async def _retrieve_context(self, *args, **kwargs):
        started = time.perf_counter()
        context = await super()._retrieve_context(*args, **kwargs)
        self._benchmark_trace.set(
            {
                "retrieved_context": context,
                "retrieve_context_ms": (time.perf_counter() - started) * 1000.0,
            }
        )
        return context

    def close(self) -> bool:
        try:
            return super().close()
        finally:
            for patcher in reversed(getattr(self, "_benchmark_patchers", [])):
                patcher.stop()
            self._benchmark_patchers = []

    async def build_agent_answer_messages_with_trace(self, query: str, **kwargs) -> dict[str, Any]:
        token = self._benchmark_trace.set(None)
        started = time.perf_counter()
        try:
            messages = await super().build_agent_answer_messages(query, **kwargs)
            trace = self._benchmark_trace.get()
            if trace is None:
                raise RuntimeError("build_agent_answer_messages 完成后没有捕获到 retrieved_context")
            return {
                "messages": messages,
                "retrieved_context": trace["retrieved_context"],
                "retrieve_context_ms": trace["retrieve_context_ms"],
                "build_total_ms": (time.perf_counter() - started) * 1000.0,
            }
        finally:
            self._benchmark_trace.reset(token)


class DeterministicBenchmarkLLM:
    """用于纯基础设施压测的确定性 LLM，不代表真实记忆提取准确率。"""

    supports_response_metadata = False

    @staticmethod
    def _keywords(text: str, limit: int = 8) -> list[str]:
        values = re.findall(r"[A-Za-z0-9_\-%.]+|[\u4e00-\u9fff]{2,8}", text)
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            value = value.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _safe_json(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def generate_response(self, messages, response_format=None, **kwargs):
        messages = messages or []
        system = str(messages[0].get("content", "")) if messages else ""
        user = str(messages[-1].get("content", "")) if messages else ""
        system_lower = system.lower()

        if "attribute_catalog" in user and "current_profile" in user:
            return json.dumps({"operations": [], "unmapped_facts": []}, ensure_ascii=False)

        payload = self._safe_json(user)
        if "existing_session" in payload and "new_page" in payload:
            existing = payload.get("existing_session") or {}
            new_page = payload.get("new_page") or {}
            existing_summary = str(existing.get("summary") or "").strip()
            page_summary = str(new_page.get("summary") or "").strip()
            summary = existing_summary
            if page_summary and page_summary not in existing_summary:
                summary = f"{existing_summary} {page_summary}".strip()
            keywords: list[str] = []
            for value in [*(existing.get("keywords") or []), *(new_page.get("keywords") or [])]:
                text = str(value).strip()
                if text and text not in keywords:
                    keywords.append(text)
            return json.dumps({"summary": summary[:1000], "keywords": keywords[:12]}, ensure_ascii=False)

        if "summary" in system_lower and "keyword" in system_lower:
            summary = user.strip()[:240]
            return json.dumps(
                {"summary": summary, "keywords": self._keywords(user)},
                ensure_ascii=False,
            )

        memory_text = user.strip()
        if len(memory_text) > 800:
            memory_text = memory_text[:800]
        if not memory_text:
            return json.dumps({"memory": []}, ensure_ascii=False)
        return json.dumps({"memory": [{"text": memory_text}]}, ensure_ascii=False)

    async def generate_response_async(self, messages, response_format=None, **kwargs):
        return self.generate_response(messages, response_format=response_format, **kwargs)

    async def agenerate_response(self, messages, response_format=None, **kwargs):
        return self.generate_response(messages, response_format=response_format, **kwargs)


def create_benchmark_memory(config: dict[str, Any], *, llm_mode: str) -> BenchmarkAsyncMemory:
    mode = str(llm_mode or "real").strip().lower()
    if mode not in {"real", "mock"}:
        raise ValueError("llm_mode 只能是 real 或 mock")

    runtime_config = deepcopy(config)
    benchmark_runtime = runtime_config.pop("benchmark_runtime", {}) or {}
    observability_enabled = bool(benchmark_runtime.get("llm_observability", False))
    deepseek_midterm_non_thinking = bool(benchmark_runtime.get("deepseek_midterm_non_thinking", False))
    deepseek_longterm_non_thinking = bool(benchmark_runtime.get("deepseek_longterm_non_thinking", False))
    llm_provider = str((runtime_config.get("llm") or {}).get("provider") or "").strip().lower()
    if (deepseek_midterm_non_thinking or deepseek_longterm_non_thinking) and llm_provider != "deepseek":
        raise ValueError("DeepSeek 非思考模式仅支持 llm.provider=deepseek")

    patchers = [
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch(
            "mem0.utils.factory.VectorStoreFactory.create",
            side_effect=_create_vector_store_for_benchmark,
        ),
    ]
    if mode == "mock":
        patchers.append(
            patch(
                "mem0.memory.main.LlmFactory.create",
                return_value=DeterministicBenchmarkLLM(),
            )
        )
    for patcher in patchers:
        patcher.start()
    try:
        memory = BenchmarkAsyncMemory.from_config(runtime_config)
    except Exception:
        for patcher in reversed(patchers):
            patcher.stop()
        raise
    observed_llm = BenchmarkObservedLLM(
        memory.llm,
        call_context=memory._benchmark_llm_context,
        observability_enabled=observability_enabled,
        deepseek_midterm_non_thinking=deepseek_midterm_non_thinking,
        deepseek_longterm_non_thinking=deepseek_longterm_non_thinking,
    )
    memory.llm = observed_llm
    if getattr(memory, "_midterm_updater", None) is not None:
        memory._midterm_updater.llm = observed_llm
    if getattr(memory, "_profile_updater", None) is not None:
        memory._profile_updater.llm = observed_llm
    memory._benchmark_patchers = patchers
    return memory
