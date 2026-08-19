from __future__ import annotations

import asyncio
import json
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
from typing import Any

from mem0.configs.base import AgenticRetrievalConfig
from mem0.memory.retrieval_tools import MEMORY_TOOLS, serialize_tool_result

logger = logging.getLogger(__name__)

_FINAL_ANSWER_PROMPT = (
    "唯一一次中期记忆检索机会已经结束。你不是最终回答节点，只能整理中期记忆补充。"
    "请仅根据原始上下文识别待补充的历史信息，并从工具返回的历史记忆中提取与该缺口直接相关的内容。"
    "删除无关、重复、过时、冲突严重或不可信的内容，保留必要的主体、期间、版本、指标口径、历史修正和适用条件。"
    "不得回答用户当前问题，不得重新进行业务或财务分析，不得给出授信建议，不得添加工具结果中不存在的内容，"
    "也不得暴露工具名称、检索词、内部 ID、记忆层级或实现细节。直接输出历史上下文本身，不得添加“中期记忆补充”"
    "等内部标题或前缀。没有可用补充时返回空内容。不得再次请求任何工具。"
)


def _supplement_content(response: Any) -> str | None:
    """Return normalized supplement text, or None for an invalid response shape."""
    if isinstance(response, str):
        return response.strip()
    if isinstance(response, dict):
        if "content" in response:
            content = response.get("content")
        elif "text" in response:
            content = response.get("text")
        elif "message" in response:
            content = response.get("message")
        else:
            return None
        if isinstance(content, dict):
            content = content.get("content")
    else:
        if not hasattr(response, "content"):
            return None
        content = getattr(response, "content", None)
    if content is None:
        return ""
    if not isinstance(content, str):
        return None
    return content.strip()


def _tool_calls(response: Any) -> list[dict[str, Any]]:
    if not isinstance(response, dict) or not isinstance(response.get("tool_calls"), list):
        return []

    normalized = []
    for raw_call in response["tool_calls"]:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function") if isinstance(raw_call.get("function"), dict) else {}
        name = raw_call.get("name") or function.get("name")
        arguments = raw_call.get("arguments", function.get("arguments", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{uuid.uuid4().hex}"
        normalized.append({"id": call_id, "name": str(name or ""), "arguments": arguments})
    return normalized


def _arguments_json(arguments: Any) -> str:
    if isinstance(arguments, str):
        return arguments
    return json.dumps(
        arguments,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    )


def _assistant_tool_message(content: str, calls: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["name"],
                    "arguments": _arguments_json(call["arguments"]),
                },
            }
            for call in calls
        ],
    }


def _safe_tool_result(result: Any, max_chars: int) -> tuple[dict[str, Any], str]:
    if not isinstance(result, dict):
        result = {"ok": False, "error": "InvalidToolResult", "message": "记忆工具返回了无效结果"}
    serialized = serialize_tool_result(result)
    if len(serialized) > max_chars:
        result = {"ok": False, "error": "ToolResultTooLarge", "message": "工具结果超过长度限制"}
        serialized = serialize_tool_result(result)
    return result, serialized


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    summary = {"ok": bool(result.get("ok"))}
    if isinstance(result.get("items"), list):
        summary["item_count"] = len(result["items"])
    if isinstance(result.get("errors"), list):
        summary["error_count"] = len(result["errors"])
    if result.get("error"):
        summary["error"] = result["error"]
    return summary


def _tool_result_state(result: dict[str, Any]) -> str:
    """Classify a serialized tool payload without interpreting retrieval relevance."""
    if result.get("ok") is not True:
        return "degraded"
    items = result.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return "degraded"
    errors = result.get("errors")
    if errors not in (None, []):
        return "degraded"
    return "has_items" if items else "no_relevant_memory"


async def _run_sync(function: Any, /, **kwargs: Any) -> Any:
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, partial(function, **kwargs))


class AgenticMemoryRunner:
    """Run one optional mid-term search using at most two model calls."""

    def __init__(
        self,
        llm: Any,
        tool_executor: Any,
        config: AgenticRetrievalConfig,
        *,
        generation_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.llm = llm
        self.tool_executor = tool_executor
        self.config = config
        self.generation_kwargs = deepcopy(generation_kwargs or {})
        for reserved in ("messages", "tools", "tool_choice"):
            self.generation_kwargs.pop(reserved, None)

    def run(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        working_messages = deepcopy(messages)
        try:
            first_response = self.llm.generate_response(
                messages=deepcopy(working_messages),
                tools=MEMORY_TOOLS,
                tool_choice="auto",
                **self.generation_kwargs,
            )
        except Exception:
            logger.exception("Agentic memory retrieval decision model failed")
            return self._result("", 1, 0, "degraded", [])
        calls = _tool_calls(first_response)
        if not calls:
            # A no-tool decision means the current context is sufficient. Any
            # ordinary text emitted by the decision model must never reach the
            # final answer prompt as memory context.
            return self._result("", 1, 0, "not_needed", [])

        call = calls[0]
        working_messages.append(_assistant_tool_message("", [call]))
        try:
            raw_result = self.tool_executor.execute(call["name"], call["arguments"])
        except Exception:
            logger.exception("Unexpected agentic memory tool failure for tool=%s", call["name"])
            raw_result = {
                "ok": False,
                "error": "ToolExecutionError",
                "message": "记忆检索暂时不可用",
            }

        result, serialized = _safe_tool_result(raw_result, self.config.max_tool_result_chars)
        working_messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": serialized,
            }
        )
        tool_trace = [
            {
                "iteration": 1,
                "name": call["name"],
                "arguments": deepcopy(call["arguments"]),
                "result_summary": _result_summary(result),
            }
        ]

        if len(calls) > 1:
            logger.warning("Agentic retrieval model requested more than one tool call")
            return self._result("", 1, 1, "degraded", tool_trace)

        result_state = _tool_result_state(result)
        if result_state == "degraded":
            return self._result("", 1, 1, "degraded", tool_trace)
        if result_state == "no_relevant_memory":
            return self._result("", 1, 1, "no_relevant_memory", tool_trace)
        return self._final_call(working_messages, tool_trace)

    def _final_call(
        self,
        messages: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.config.max_iterations < 2:
            return self._result("", 1, 1, "degraded", tool_trace)

        final_messages = deepcopy(messages)
        # ``force_final_answer`` remains a public config field for compatibility,
        # but it can no longer switch this node back to user-facing answer output.
        final_messages.append({"role": "system", "content": _FINAL_ANSWER_PROMPT})
        self._confirm_tool_results()
        try:
            response = self.llm.generate_response(messages=final_messages, **self.generation_kwargs)
        except Exception:
            logger.exception("Agentic memory supplement model failed")
            return self._result("", 2, 1, "degraded", tool_trace)
        attempted_calls = _tool_calls(response)
        if attempted_calls:
            return self._result("", 2, 1, "degraded", tool_trace)
        supplement = _supplement_content(response)
        if supplement is None:
            return self._result("", 2, 1, "degraded", tool_trace)
        if not supplement:
            return self._result("", 2, 1, "no_relevant_memory", tool_trace)
        return self._result(supplement, 2, 1, "supplemented", tool_trace)

    def _confirm_tool_results(self) -> None:
        confirm = getattr(self.tool_executor, "confirm_last_results", None)
        if not callable(confirm):
            return
        try:
            confirm()
        except Exception:
            logger.exception("Failed to confirm valid Agentic memory recalls")

    @staticmethod
    def _result(
        supplement: str,
        iterations: int,
        tool_call_count: int,
        status: str,
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "status": status,
            "supplement": supplement,
            # Compatibility alias: answer now has exactly the same memory-
            # supplement semantics and never represents a user-facing answer.
            "answer": supplement,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            # Compatibility field retained for existing result consumers.
            "stop_reason": status,
            "tool_trace": deepcopy(tool_trace),
        }


class AsyncAgenticMemoryRunner(AgenticMemoryRunner):
    """Async runner with the same two-call protocol as the sync runner."""

    async def run(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        working_messages = deepcopy(messages)
        try:
            first_response = await _run_sync(
                self.llm.generate_response,
                messages=deepcopy(working_messages),
                tools=MEMORY_TOOLS,
                tool_choice="auto",
                **self.generation_kwargs,
            )
        except Exception:
            logger.exception("Async Agentic memory retrieval decision model failed")
            return self._result("", 1, 0, "degraded", [])
        calls = _tool_calls(first_response)
        if not calls:
            return self._result("", 1, 0, "not_needed", [])

        call = calls[0]
        working_messages.append(_assistant_tool_message("", [call]))
        try:
            raw_result = await self.tool_executor.execute(call["name"], call["arguments"])
        except Exception:
            logger.exception("Unexpected async agentic memory tool failure for tool=%s", call["name"])
            raw_result = {
                "ok": False,
                "error": "ToolExecutionError",
                "message": "记忆检索暂时不可用",
            }

        result, serialized = _safe_tool_result(raw_result, self.config.max_tool_result_chars)
        working_messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": serialized,
            }
        )
        tool_trace = [
            {
                "iteration": 1,
                "name": call["name"],
                "arguments": deepcopy(call["arguments"]),
                "result_summary": _result_summary(result),
            }
        ]

        if len(calls) > 1:
            logger.warning("Async Agentic retrieval model requested more than one tool call")
            return self._result("", 1, 1, "degraded", tool_trace)

        result_state = _tool_result_state(result)
        if result_state == "degraded":
            return self._result("", 1, 1, "degraded", tool_trace)
        if result_state == "no_relevant_memory":
            return self._result("", 1, 1, "no_relevant_memory", tool_trace)
        return await self._final_call_async(working_messages, tool_trace)

    async def _final_call_async(
        self,
        messages: list[dict[str, Any]],
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.config.max_iterations < 2:
            return self._result("", 1, 1, "degraded", tool_trace)

        final_messages = deepcopy(messages)
        # Keep the async protocol identical to the sync supplement-only path.
        final_messages.append({"role": "system", "content": _FINAL_ANSWER_PROMPT})
        confirm = getattr(self.tool_executor, "confirm_last_results", None)
        if callable(confirm):
            try:
                await _run_sync(confirm)
            except Exception:
                logger.exception("Failed to confirm valid async Agentic memory recalls")
        try:
            response = await _run_sync(
                self.llm.generate_response,
                messages=final_messages,
                **self.generation_kwargs,
            )
        except Exception:
            logger.exception("Async Agentic memory supplement model failed")
            return self._result("", 2, 1, "degraded", tool_trace)
        attempted_calls = _tool_calls(response)
        if attempted_calls:
            return self._result("", 2, 1, "degraded", tool_trace)
        supplement = _supplement_content(response)
        if supplement is None:
            return self._result("", 2, 1, "degraded", tool_trace)
        if not supplement:
            return self._result("", 2, 1, "no_relevant_memory", tool_trace)
        return self._result(supplement, 2, 1, "supplemented", tool_trace)
