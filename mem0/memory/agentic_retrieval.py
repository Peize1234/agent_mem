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
    "唯一一次中期记忆检索机会已经结束。请仅根据当前对话、用户画像和已经返回的记忆结果直接回答。"
    "不得再次请求任何工具；信息不足时明确说明不确定。"
)
_FALLBACK_ANSWER = "根据当前对话和已检索到的记忆，仍无法确定足够可靠的答案。"


def _response_content(response: Any) -> str:
    if isinstance(response, str):
        return response.strip()
    if not isinstance(response, dict):
        content = getattr(response, "content", None)
        return str(content or "").strip()

    content = response.get("content") or response.get("text") or response.get("message")
    if isinstance(content, dict):
        content = content.get("content")
    return str(content or "").strip()


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
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


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
    if result.get("error"):
        summary["error"] = result["error"]
    return summary


def _limit_result() -> dict[str, Any]:
    return {
        "ok": False,
        "error": "ToolCallLimitExceeded",
        "message": "每轮只允许一次记忆工具调用；请直接回答",
    }


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
        first_response = self.llm.generate_response(
            messages=deepcopy(working_messages),
            tools=MEMORY_TOOLS,
            tool_choice="auto",
            **self.generation_kwargs,
        )
        calls = _tool_calls(first_response)
        content = _response_content(first_response)
        if not calls:
            if content:
                return self._result(content, 1, 0, "model_answered", [])
            return self._final_call(working_messages, 0, "empty_response", [])

        working_messages.append(_assistant_tool_message(content, calls))
        tool_trace = []
        for index, call in enumerate(calls):
            if index == 0:
                try:
                    result = self.tool_executor.execute(call["name"], call["arguments"])
                except Exception:
                    logger.exception("Unexpected agentic memory tool failure for tool=%s", call["name"])
                    result = {
                        "ok": False,
                        "error": "ToolExecutionError",
                        "message": "记忆检索暂时不可用",
                    }
            else:
                result = _limit_result()

            result, serialized = _safe_tool_result(result, self.config.max_tool_result_chars)
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": serialized,
                }
            )
            tool_trace.append(
                {
                    "iteration": 1,
                    "name": call["name"],
                    "arguments": deepcopy(call["arguments"]),
                    "result_summary": _result_summary(result),
                }
            )

        stop_reason = "max_tool_calls" if len(calls) > 1 else "model_answered"
        return self._final_call(working_messages, 1, stop_reason, tool_trace)

    def _final_call(
        self,
        messages: list[dict[str, Any]],
        tool_call_count: int,
        stop_reason: str,
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.config.max_iterations < 2:
            return self._result(_FALLBACK_ANSWER, 1, tool_call_count, "max_iterations", tool_trace)

        final_messages = deepcopy(messages)
        prompt = _FINAL_ANSWER_PROMPT if self.config.force_final_answer else "不得请求工具；请直接给出最终回答。"
        final_messages.append({"role": "system", "content": prompt})
        response = self.llm.generate_response(messages=final_messages, **self.generation_kwargs)
        attempted_calls = _tool_calls(response)
        answer = _response_content(response) or _FALLBACK_ANSWER
        if attempted_calls:
            stop_reason = "model_tool_call_blocked"
        return self._result(answer, 2, tool_call_count, stop_reason, tool_trace)

    @staticmethod
    def _result(
        answer: str,
        iterations: int,
        tool_call_count: int,
        stop_reason: str,
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "answer": answer,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "stop_reason": stop_reason,
            "tool_trace": deepcopy(tool_trace),
        }


class AsyncAgenticMemoryRunner(AgenticMemoryRunner):
    """Async runner with the same two-call protocol as the sync runner."""

    async def run(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        working_messages = deepcopy(messages)
        first_response = await _run_sync(
            self.llm.generate_response,
            messages=deepcopy(working_messages),
            tools=MEMORY_TOOLS,
            tool_choice="auto",
            **self.generation_kwargs,
        )
        calls = _tool_calls(first_response)
        content = _response_content(first_response)
        if not calls:
            if content:
                return self._result(content, 1, 0, "model_answered", [])
            return await self._final_call_async(working_messages, 0, "empty_response", [])

        working_messages.append(_assistant_tool_message(content, calls))
        tool_trace = []
        for index, call in enumerate(calls):
            if index == 0:
                try:
                    result = await self.tool_executor.execute(call["name"], call["arguments"])
                except Exception:
                    logger.exception("Unexpected async agentic memory tool failure for tool=%s", call["name"])
                    result = {
                        "ok": False,
                        "error": "ToolExecutionError",
                        "message": "记忆检索暂时不可用",
                    }
            else:
                result = _limit_result()

            result, serialized = _safe_tool_result(result, self.config.max_tool_result_chars)
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": serialized,
                }
            )
            tool_trace.append(
                {
                    "iteration": 1,
                    "name": call["name"],
                    "arguments": deepcopy(call["arguments"]),
                    "result_summary": _result_summary(result),
                }
            )

        stop_reason = "max_tool_calls" if len(calls) > 1 else "model_answered"
        return await self._final_call_async(working_messages, 1, stop_reason, tool_trace)

    async def _final_call_async(
        self,
        messages: list[dict[str, Any]],
        tool_call_count: int,
        stop_reason: str,
        tool_trace: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self.config.max_iterations < 2:
            return self._result(_FALLBACK_ANSWER, 1, tool_call_count, "max_iterations", tool_trace)

        final_messages = deepcopy(messages)
        prompt = _FINAL_ANSWER_PROMPT if self.config.force_final_answer else "不得请求工具；请直接给出最终回答。"
        final_messages.append({"role": "system", "content": prompt})
        response = await _run_sync(
            self.llm.generate_response,
            messages=final_messages,
            **self.generation_kwargs,
        )
        attempted_calls = _tool_calls(response)
        answer = _response_content(response) or _FALLBACK_ANSWER
        if attempted_calls:
            stop_reason = "model_tool_call_blocked"
        return self._result(answer, 2, tool_call_count, stop_reason, tool_trace)
