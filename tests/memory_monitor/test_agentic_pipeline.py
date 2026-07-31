import hashlib
import json
import logging
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import AgenticRetrievalConfig
from memory_monitor.models import PipelineStep
from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository


class _AgenticDemoMemory:
    _normalize_agentic_answer_result = DemoMemory._normalize_agentic_answer_result

    def __init__(self):
        self.agentic_calls = []
        self.normal_generation_calls = []
        self.commit_calls = []
        self.agentic_error = None
        self.final_generation_error = None
        self.agentic_result = {
            "answer": "agentic final answer",
            "iterations": 2,
            "tool_call_count": 1,
            "stop_reason": "model_answered",
            "tool_trace": [
                {
                    "iteration": 1,
                    "name": "search_memory",
                    "arguments": {"queries": ["private search term"]},
                    "result_summary": {"ok": True, "item_count": 1},
                }
            ],
        }

    def retrieve_context_for_demo(self, query, *, user_id, session_id):
        context = {
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "profile": {"risk_level": "balanced"},
            "short_term_messages": [{"role": "user", "content": "recent context"}],
            "retrieved_memories": [],
            "short_term": [{"role": "user", "content": "recent context"}],
            "mid_term": [],
            "long_term": [],
            "user_profile": {"risk_level": "balanced"},
            "agentic_retrieval": True,
        }
        context["context_hash"] = self.context_hash(context)
        return context

    def build_prompt_from_context(self, context, *, agentic_answer=None):
        if agentic_answer is not None:
            return [{"role": "system", "content": f"final prompt candidate: {agentic_answer}"}]
        return [
            {"role": "system", "content": "agentic base context"},
            {"role": "user", "content": context["query"]},
        ]

    def generate_agentic_response_for_demo(self, messages, *, user_id, session_id, **kwargs):
        self.agentic_calls.append(
            {
                "messages": deepcopy(messages),
                "user_id": user_id,
                "session_id": session_id,
                "kwargs": deepcopy(kwargs),
            }
        )
        if self.agentic_error is not None:
            raise self.agentic_error
        return deepcopy(self.agentic_result)

    def generate_response_for_demo(self, messages, **kwargs):
        self.normal_generation_calls.append((deepcopy(messages), deepcopy(kwargs)))
        if self.final_generation_error is not None:
            raise self.final_generation_error
        return "externally verified answer"

    def commit_demo_turn(self, **kwargs):
        self.commit_calls.append(deepcopy(kwargs))
        return {"results": [], "background": {"migration_job_id": "m1", "profile_job_id": "p1"}}

    @staticmethod
    def context_hash(context):
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _execute_demo_generation(tmp_path, memory):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    pipeline = DemoPipelineService(
        memory,
        repository,
        state_service=object(),
        generation_kwargs={"temperature": 0.2},
    )
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="original question",
    )
    context, _ = pipeline._execute(turn, PipelineStep.RETRIEVE_CONTEXT, {})
    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    prompt_output, _ = pipeline._execute(turn, PipelineStep.BUILD_PROMPT, {})
    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.BUILD_PROMPT: prompt_output,
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    return pipeline._execute(turn, PipelineStep.GENERATE_RESPONSE, {})


def test_demo_memory_switch_uses_complete_context_and_agentic_prompt_without_generation():
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))
    memory.llm = MagicMock()
    memory._retrieve_base_context = MagicMock(side_effect=AssertionError("base-only retrieval must stay disabled"))
    memory._retrieve_context = MagicMock(
        return_value={
            "query": "historical question",
            "user_id": "user-1",
            "session_id": "run-1",
            "profile": {"risk_level": "balanced"},
            "short_term_messages": [{"role": "user", "content": "recent"}],
            "retrieved_memories": [
                {
                    "source": "mid_term_page",
                    "raw_dialogue": "ordinary mid-term context",
                    "created_at": "2026-07-30T10:00:00+08:00",
                },
                {
                    "source": "long_term",
                    "memory": "ordinary long-term context",
                    "created_at": "2026-07-29T10:00:00+08:00",
                },
            ],
        }
    )

    context = memory.retrieve_context_for_demo("historical question", user_id="user-1", session_id="run-1")
    prompt = memory.build_prompt_from_context(context)
    answer_prompt = memory.build_prompt_from_context(context, agentic_answer="candidate answer")

    assert context["agentic_retrieval"] is True
    assert len(context["mid_term"]) == len(context["long_term"]) == 1
    assert prompt[-1] == {"role": "user", "content": "historical question"}
    assert "外部记忆工具" in prompt[0]["content"]
    assert "ordinary mid-term context" in prompt[0]["content"]
    assert "ordinary long-term context" in prompt[0]["content"]
    assert "<agentic_answer>\ncandidate answer\n</agentic_answer>" in answer_prompt[0]["content"]
    memory._retrieve_context.assert_called_once_with(
        "historical question",
        user_id="user-1",
        session_id="run-1",
    )
    memory._retrieve_base_context.assert_not_called()
    memory.llm.generate_response.assert_not_called()


def test_demo_memory_disabled_agentic_mode_keeps_existing_one_shot_retrieval():
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=False))
    memory._retrieve_base_context = MagicMock(side_effect=AssertionError("base-only retrieval is agentic-only"))
    memory._retrieve_context = MagicMock(
        return_value={
            "query": "historical question",
            "user_id": "user-1",
            "session_id": "run-1",
            "profile": {},
            "short_term_messages": [],
            "retrieved_memories": [
                {"id": "mid-1", "source": "mid_term_page", "memory": "mid-term"},
                {"id": "long-1", "source": "long_term", "memory": "long-term"},
            ],
        }
    )

    context = memory.retrieve_context_for_demo("historical question", user_id="user-1", session_id="run-1")

    assert "agentic_retrieval" not in context
    assert [item["id"] for item in context["mid_term"]] == ["mid-1"]
    assert [item["id"] for item in context["long_term"]] == ["long-1"]
    memory._retrieve_context.assert_called_once()
    memory._retrieve_base_context.assert_not_called()


def test_agentic_loop_stays_inside_generation_node_and_commit_receives_only_final_turn(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    memory = _AgenticDemoMemory()
    pipeline = DemoPipelineService(
        memory,
        repository,
        state_service=object(),
        generation_kwargs={"temperature": 0.2},
    )
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="original question",
    )

    context, _ = pipeline._execute(turn, PipelineStep.RETRIEVE_CONTEXT, {})
    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    prompt_output, _ = pipeline._execute(turn, PipelineStep.BUILD_PROMPT, {})

    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.BUILD_PROMPT: prompt_output,
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    generation, updates = pipeline._execute(turn, PipelineStep.GENERATE_RESPONSE, {})

    assert generation["assistant_message"] == "externally verified answer"
    assert generation["iterations"] == 2
    assert generation["tool_call_count"] == 1
    assert generation["tool_trace"][0]["arguments"]["queries"] == ["private search term"]
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [
        {"role": "system", "content": "final prompt candidate: agentic final answer"}
    ]
    assert memory.agentic_calls[0]["user_id"] == "user-1"
    assert memory.agentic_calls[0]["session_id"] == "run-1"

    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.GENERATE_RESPONSE: generation,
    }[step]
    pipeline._execute(turn, PipelineStep.RUN_SHORTTERM, {})

    assert len(memory.commit_calls) == 1
    commit = memory.commit_calls[0]
    assert commit["user_message"] == "original question"
    assert commit["assistant_message"] == "externally verified answer"
    assert "tool_trace" not in commit
    assert "prompt_messages" not in commit
    assert "raw_response" not in commit


@pytest.mark.parametrize(
    "agentic_result",
    [
        {
            "answer": "must be ignored",
            "iterations": 1,
            "tool_call_count": 0,
            "stop_reason": "max_iterations",
            "tool_trace": [],
        },
        {
            "answer": "",
            "iterations": 1,
            "tool_call_count": 0,
            "stop_reason": "model_answered",
            "tool_trace": [],
        },
        {
            "answer": "must be ignored",
            "iterations": 2,
            "tool_call_count": 1,
            "stop_reason": "model_answered",
            "tool_trace": [
                {
                    "iteration": 1,
                    "name": "search_memory",
                    "arguments": {"queries": ["risk limit"]},
                    "result_summary": {"ok": False, "error": "RetrievalUnavailable"},
                }
            ],
        },
    ],
)
def test_demo_invalid_agentic_result_uses_empty_candidate_and_continues(tmp_path, agentic_result):
    memory = _AgenticDemoMemory()
    memory.agentic_result = agentic_result

    generation, updates = _execute_demo_generation(tmp_path, memory)

    assert generation["assistant_message"] == "externally verified answer"
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [{"role": "system", "content": "final prompt candidate: "}]


def test_demo_agentic_exception_uses_empty_candidate_and_continues(tmp_path, caplog):
    memory = _AgenticDemoMemory()
    memory.agentic_error = RuntimeError("agentic unavailable")

    with caplog.at_level(logging.WARNING):
        generation, updates = _execute_demo_generation(tmp_path, memory)

    assert generation["assistant_message"] == "externally verified answer"
    assert generation["stop_reason"] == "agentic_error"
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [{"role": "system", "content": "final prompt candidate: "}]
    assert any(
        record.message == "Agentic retrieval failed in demo; using normal answer prompt" for record in caplog.records
    )


def test_demo_final_answer_model_exception_still_fails_generation_step(tmp_path):
    memory = _AgenticDemoMemory()
    memory.final_generation_error = RuntimeError("final answer unavailable")

    with pytest.raises(RuntimeError, match="final answer unavailable"):
        _execute_demo_generation(tmp_path, memory)

    assert len(memory.agentic_calls) == 1
    assert len(memory.normal_generation_calls) == 1
