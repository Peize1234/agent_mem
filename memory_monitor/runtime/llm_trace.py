from __future__ import annotations

import inspect
import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Iterator


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "dict"):
        return value.dict()
    if hasattr(value, "__dict__"):
        return vars(value)
    return str(value)


def serializable_copy(value: Any) -> Any:
    """Freeze provider inputs and outputs without retaining mutable SDK objects."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    except Exception:
        try:
            return deepcopy(value)
        except Exception:
            return str(value)


@dataclass
class LLMTrace:
    step: str
    purpose: str
    attempt: int
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next_llm_sequence: int = field(init=False, repr=False)
    _next_tool_sequence: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._next_llm_sequence = max(
            (int(call.get("sequence") or 0) for call in self.llm_calls),
            default=0,
        ) + 1
        self._next_tool_sequence = max(
            (int(call.get("sequence") or 0) for call in self.tool_calls),
            default=0,
        ) + 1

    def begin_llm_call(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        request = dict(kwargs)
        messages = request.pop("messages", None)
        tools = request.pop("tools", None)
        if args:
            if messages is None:
                messages = args[0]
                args = args[1:]
            if args:
                request["positional_args"] = args
        with self._lock:
            sequence = self._next_llm_sequence
            self._next_llm_sequence += 1
        call = {
            "sequence": sequence,
            "attempt": self.attempt,
            "purpose": self.purpose,
            "messages": serializable_copy(messages),
            "tools": serializable_copy(tools),
            "parameters": serializable_copy(request),
            "response": None,
            "duration_ms": None,
            "status": "running",
            "error_type": None,
            "error_message": None,
        }
        return sequence, call

    def finish_llm_call(
        self,
        sequence: int,
        call: dict[str, Any],
        *,
        started_at: float,
        response: Any = None,
        error: BaseException | None = None,
    ) -> None:
        call["duration_ms"] = max((time.perf_counter() - started_at) * 1000, 0.0)
        if error is None:
            call["status"] = "succeeded"
            call["response"] = serializable_copy(response)
        else:
            call["status"] = "failed"
            call["error_type"] = type(error).__name__
            call["error_message"] = str(error)
        with self._lock:
            # Calls are normally sequential. Sorting preserves actual invocation
            # order if one node ever issues overlapping requests.
            self.llm_calls.append(call)
            self.llm_calls.sort(key=lambda item: int(item.get("sequence") or sequence))

    def record_tool_call(
        self,
        name: str,
        arguments: Any,
        *,
        started_at: float,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            sequence = self._next_tool_sequence
            self._next_tool_sequence += 1
        call = {
            "sequence": sequence,
            "attempt": self.attempt,
            "name": str(name),
            "arguments": serializable_copy(arguments),
            "result": serializable_copy(result) if error is None else None,
            "duration_ms": max((time.perf_counter() - started_at) * 1000, 0.0),
            "status": "succeeded" if error is None else "failed",
            "error_type": type(error).__name__ if error is not None else None,
            "error_message": str(error) if error is not None else None,
        }
        with self._lock:
            self.tool_calls.append(call)
            self.tool_calls.sort(key=lambda item: int(item.get("sequence") or sequence))


_CURRENT_TRACE: ContextVar[LLMTrace | None] = ContextVar("memory_monitor_llm_trace", default=None)


@contextmanager
def trace_llm_calls(
    step: str,
    purpose: str,
    *,
    attempt: int,
    existing_llm_calls: list[dict[str, Any]] | None = None,
    existing_tool_calls: list[dict[str, Any]] | None = None,
) -> Iterator[LLMTrace]:
    trace = LLMTrace(
        step=step,
        purpose=purpose,
        attempt=attempt,
        llm_calls=serializable_copy(existing_llm_calls or []),
        tool_calls=serializable_copy(existing_tool_calls or []),
    )
    token = _CURRENT_TRACE.set(trace)
    try:
        yield trace
    finally:
        _CURRENT_TRACE.reset(token)


class TracedLLM:
    """Demo-local LLM decorator that records calls in the active step context."""

    def __init__(self, wrapped: Any):
        self._wrapped = wrapped

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        trace = _CURRENT_TRACE.get()
        if trace is None:
            return self._wrapped.generate_response(*args, **kwargs)
        sequence, call = trace.begin_llm_call(args, kwargs)
        started_at = time.perf_counter()
        try:
            response = self._wrapped.generate_response(*args, **kwargs)
        except Exception as exc:
            trace.finish_llm_call(sequence, call, started_at=started_at, error=exc)
            raise
        trace.finish_llm_call(sequence, call, started_at=started_at, response=response)
        return response

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._wrapped, name)
        if name not in {"generate_response_async", "agenerate_response"} or not inspect.iscoroutinefunction(attribute):
            return attribute

        async def traced_async(*args: Any, **kwargs: Any) -> Any:
            trace = _CURRENT_TRACE.get()
            if trace is None:
                return await attribute(*args, **kwargs)
            sequence, call = trace.begin_llm_call(args, kwargs)
            started_at = time.perf_counter()
            try:
                response = await attribute(*args, **kwargs)
            except Exception as exc:
                trace.finish_llm_call(sequence, call, started_at=started_at, error=exc)
                raise
            trace.finish_llm_call(sequence, call, started_at=started_at, response=response)
            return response

        return traced_async


class TracedToolExecutor:
    """Record Agentic memory tool inputs and complete results for the active step."""

    def __init__(self, wrapped: Any):
        self._wrapped = wrapped

    def execute(self, name: str, arguments: Any) -> Any:
        trace = _CURRENT_TRACE.get()
        started_at = time.perf_counter()
        try:
            result = self._wrapped.execute(name, arguments)
        except Exception as exc:
            if trace is not None:
                trace.record_tool_call(name, arguments, started_at=started_at, error=exc)
            raise
        if trace is not None:
            trace.record_tool_call(name, arguments, started_at=started_at, result=result)
        return result


def ensure_traced_llm(memory: Any) -> None:
    """Decorate only this Demo memory instance; production/global LLMs are untouched."""
    llm = getattr(memory, "llm", None)
    if llm is not None and not isinstance(llm, TracedLLM):
        memory.llm = TracedLLM(llm)
    for component_name in ("reranker", "_midterm_updater", "_profile_updater"):
        component = getattr(memory, component_name, None)
        component_llm = getattr(component, "llm", None)
        if component_llm is not None and not isinstance(component_llm, TracedLLM):
            component.llm = TracedLLM(component_llm)
