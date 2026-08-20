"""Regression coverage for the current retrieve/add trace implementation.

The former finance-package trace runner was removed with its fixture package.
Tracing now lives in ``memory_monitor.runtime`` and is isolated from production
Memory, so this module verifies that active implementation instead.
"""

import asyncio
from copy import deepcopy
from unittest.mock import MagicMock

import pytest

from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.runtime.llm_trace import TracedLLM, trace_llm_calls


class RecordingLLM:
    def __init__(self):
        self.requests = []

    def generate_response(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        return {"content": "answer"}


class AsyncRecordingLLM(RecordingLLM):
    async def generate_response_async(self, **kwargs):
        await asyncio.sleep(0)
        self.requests.append(deepcopy(kwargs))
        return {"content": "async-answer"}


def test_current_trace_records_complete_llm_request_and_response():
    wrapped = RecordingLLM()
    traced = TracedLLM(wrapped)
    messages = [{"role": "user", "content": "What changed?"}]

    with trace_llm_calls("generate", "answer_generation", attempt=1) as trace:
        response = traced.generate_response(
            messages=messages,
            response_format={"type": "json_object"},
            temperature=0.2,
        )

    assert response == {"content": "answer"}
    assert wrapped.requests == [
        {
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
    ]
    assert trace.llm_calls == [
        {
            "sequence": 1,
            "attempt": 1,
            "purpose": "answer_generation",
            "messages": messages,
            "tools": None,
            "parameters": {
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
            "response": {"content": "answer"},
            "duration_ms": trace.llm_calls[0]["duration_ms"],
            "status": "succeeded",
            "error_type": None,
            "error_message": None,
        }
    ]
    assert trace.llm_calls[0]["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_current_trace_records_native_async_llm_calls():
    wrapped = AsyncRecordingLLM()
    traced = TracedLLM(wrapped)

    with trace_llm_calls("add", "long_term_memory", attempt=2) as trace:
        response = await traced.generate_response_async(
            messages=[{"role": "user", "content": "remember this"}],
        )

    assert response == {"content": "async-answer"}
    assert trace.llm_calls[0]["attempt"] == 2
    assert trace.llm_calls[0]["purpose"] == "long_term_memory"
    assert trace.llm_calls[0]["status"] == "succeeded"


def test_current_demo_retrieve_context_freezes_layers_without_answer_llm_call():
    memory = DemoMemory.__new__(DemoMemory)
    memory.llm = MagicMock()
    memory._retrieve_context = MagicMock(
        return_value={
            "user_id": "user-1",
            "session_id": "run-1",
            "query": "What changed?",
            "profile": {"risk_level": "balanced"},
            "short_term_messages": [{"role": "user", "content": "recent"}],
            "retrieved_memories": [
                {"id": "mid-1", "source": "mid_term_page", "raw_dialogue": "older"},
                {"id": "long-1", "source": "long_term", "memory": "durable"},
            ],
        }
    )

    context = memory.retrieve_context_for_demo(
        "What changed?",
        user_id="user-1",
        session_id="run-1",
    )

    assert context["short_term"] == [{"role": "user", "content": "recent"}]
    assert [item["id"] for item in context["mid_term"]] == ["mid-1"]
    assert [item["id"] for item in context["long_term"]] == ["long-1"]
    assert context["user_profile"] == {"risk_level": "balanced"}
    assert len(context["context_hash"]) == 64
    memory.llm.generate_response.assert_not_called()
