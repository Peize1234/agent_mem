import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mem0.configs.base import AgenticRetrievalConfig
from mem0.memory.main import build_answer_prompt_messages_from_context
from memory_monitor.models import PipelineStep
from memory_monitor.runtime import DemoBackgroundCoordinator
from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository


class _AgenticDemoMemory:
    _normalize_agentic_supplement_result = DemoMemory._normalize_agentic_supplement_result
    _normalize_agentic_answer_result = DemoMemory._normalize_agentic_answer_result

    def __init__(self):
        self.agentic_enabled = True
        self.agentic_calls = []
        self.normal_generation_calls = []
        self.commit_calls = []
        self.agentic_error = None
        self.final_generation_error = None
        self.agentic_result = {
            "status": "supplemented",
            "supplement": "historical memory supplement",
            "answer": "historical memory supplement",
            "iterations": 2,
            "tool_call_count": 1,
            "stop_reason": "supplemented",
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
        }
        if self.agentic_enabled:
            context["agentic_retrieval"] = True
        context["context_hash"] = self.context_hash(context)
        return context

    def build_prompt_from_context(self, context, *, agentic_memory_supplement=None, agentic_answer=None):
        supplement = agentic_memory_supplement or agentic_answer or ""
        return [{"role": "system", "content": f"final prompt supplement: {supplement}"}]

    def generate_agentic_response_for_demo(self, context, **kwargs):
        self.agentic_calls.append(
            {
                "context": deepcopy(context),
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
    agentic_input = pipeline._step_input(turn, PipelineStep.AGENTIC_RETRIEVAL)
    agentic_output, _ = pipeline._execute(turn, PipelineStep.AGENTIC_RETRIEVAL, agentic_input)
    pipeline._completed_step_output = lambda _turn_id, step: {
        PipelineStep.AGENTIC_RETRIEVAL: agentic_output,
    }[step]
    prompt_output, _ = pipeline._execute(turn, PipelineStep.BUILD_PROMPT, {})
    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.BUILD_PROMPT: prompt_output,
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    generation, updates = pipeline._execute(turn, PipelineStep.GENERATE_RESPONSE, {})
    return agentic_output, generation, updates


def test_demo_memory_reuses_core_agentic_flow_and_final_prompt_builder():
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))
    memory.llm = MagicMock()
    memory._run_agentic_retrieval_from_context = MagicMock(
        return_value={
            "status": "supplemented",
            "supplement": "historical supplement",
            "answer": "historical supplement",
            "iterations": 2,
            "tool_call_count": 1,
            "stop_reason": "supplemented",
            "tool_trace": [
                {
                    "iteration": 1,
                    "name": "search_memory",
                    "arguments": {"queries": ["historical context"]},
                    "result_summary": {"ok": True, "item_count": 1},
                }
            ],
        }
    )
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
    agentic_result = memory.generate_agentic_response_for_demo(context, temperature=0.2)
    answer_prompt = memory.build_prompt_from_context(context, agentic_memory_supplement="historical supplement")

    assert context["agentic_retrieval"] is True
    assert len(context["mid_term"]) == len(context["long_term"]) == 1
    assert agentic_result["answer"] == agentic_result["supplement"] == "historical supplement"
    assert (
        "<agentic_memory_supplement>\nhistorical supplement\n</agentic_memory_supplement>"
        in answer_prompt[0]["content"]
    )
    core_context = memory._run_agentic_retrieval_from_context.call_args.args[0]
    assert "context_hash" not in core_context
    assert core_context["retrieved_memories"] == context["retrieved_memories"]
    memory._run_agentic_retrieval_from_context.assert_called_once_with(
        core_context,
        reference_information=None,
        generation_kwargs={"temperature": 0.2},
        record_midterm_visits=False,
    )
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


def test_frozen_demo_context_builds_exactly_the_core_final_prompt(monkeypatch):
    monkeypatch.setattr("mem0.memory.main.beijing_now_iso", lambda: "2026-08-03T12:00:00+08:00")
    memory = DemoMemory.__new__(DemoMemory)
    context = {
        "query": "中文问题",
        "user_id": "user-1",
        "session_id": "run-1",
        "profile": {"风险偏好": "稳健"},
        "short_term_messages": [{"role": "user", "content": "保留中文"}],
        "retrieved_memories": [
            {
                "source": "mid_term_page",
                "raw_dialogue": "User: 历史问题\nAssistant: 历史回答",
                "created_at": "2026-08-01T10:00:00+08:00",
                "score": 0.9,
            }
        ],
        "agentic_retrieval": True,
    }
    context["context_hash"] = memory.context_hash(context)
    core_context = deepcopy(context)
    core_context.pop("context_hash")

    demo_messages = memory.build_prompt_from_context(context, agentic_memory_supplement="历史口径补充")
    core_messages = build_answer_prompt_messages_from_context(
        core_context,
        agentic_memory_supplement="历史口径补充",
    )

    assert demo_messages == core_messages
    prompt = demo_messages[0]["content"]
    assert '<short_term_memory>\n[\n  {\n    "role": "user",\n    "content": "保留中文"' in prompt
    assert '<mid_term_memories>\n[\n  {\n    "score": 0.9,\n    "created_at": "2026-08-01T10:00:00+08:00"' in prompt
    assert "<fine_grained_longterm_memories>\n[]\n</fine_grained_longterm_memories>" in prompt
    assert "<promoted_longterm_memories>\n[]\n</promoted_longterm_memories>" in prompt
    assert '<user_profile>\n{\n  "风险偏好": "稳健"\n}\n</user_profile>' in prompt
    assert "<reference_information>\n[]\n</reference_information>" in prompt
    assert "\\u4e2d" not in prompt
    assert "<agentic_memory_supplement>\n历史口径补充\n</agentic_memory_supplement>" in prompt


def test_agentic_loop_runs_in_its_own_node_and_commit_receives_only_final_turn(tmp_path):
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
    agentic_input = pipeline._step_input(turn, PipelineStep.AGENTIC_RETRIEVAL)
    agentic, _ = pipeline._execute(turn, PipelineStep.AGENTIC_RETRIEVAL, agentic_input)
    pipeline._completed_step_output = lambda _turn_id, step: {
        PipelineStep.AGENTIC_RETRIEVAL: agentic,
    }[step]
    prompt_output, _ = pipeline._execute(turn, PipelineStep.BUILD_PROMPT, {})

    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.BUILD_PROMPT: prompt_output,
        PipelineStep.RETRIEVE_CONTEXT: context,
    }[step]
    generation, updates = pipeline._execute(turn, PipelineStep.GENERATE_RESPONSE, {})

    assert generation["assistant_message"] == "externally verified answer"
    assert agentic["iterations"] == 2
    assert agentic["tool_call_count"] == 1
    assert agentic["tool_trace"][0]["arguments"]["queries"] == ["private search term"]
    assert agentic["agentic_status"] == "supplemented"
    assert agentic["agentic_memory_supplement"] == "historical memory supplement"
    assert agentic["agentic_answer"] == agentic["agentic_memory_supplement"]
    assert "iterations" not in generation
    assert "tool_trace" not in generation
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [
        {"role": "system", "content": "final prompt supplement: historical memory supplement"}
    ]
    assert memory.agentic_calls[0]["context"] == context

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
            "status": "degraded",
            "supplement": "",
            "answer": "",
            "iterations": 1,
            "tool_call_count": 0,
            "stop_reason": "degraded",
            "tool_trace": [],
        },
        {
            "status": "not_needed",
            "supplement": "",
            "answer": "",
            "iterations": 1,
            "tool_call_count": 0,
            "stop_reason": "not_needed",
            "tool_trace": [],
        },
        {
            "status": "degraded",
            "supplement": "",
            "answer": "",
            "iterations": 1,
            "tool_call_count": 1,
            "stop_reason": "degraded",
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
def test_demo_non_supplemented_agentic_result_uses_empty_supplement_and_continues(tmp_path, agentic_result):
    memory = _AgenticDemoMemory()
    memory.agentic_result = agentic_result

    agentic, generation, updates = _execute_demo_generation(tmp_path, memory)

    assert agentic["agentic_status"] in {"not_needed", "no_relevant_memory", "degraded"}
    assert agentic["agentic_memory_supplement"] == agentic["agentic_answer"] == ""
    assert generation["assistant_message"] == "externally verified answer"
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [{"role": "system", "content": "final prompt supplement: "}]


def test_demo_agentic_exception_degrades_to_core_non_agentic_prompt(tmp_path):
    memory = _AgenticDemoMemory()
    memory.agentic_error = RuntimeError("agentic unavailable")

    agentic, generation, updates = _execute_demo_generation(tmp_path, memory)

    assert agentic["agentic_status"] == "degraded"
    assert agentic["agentic_memory_supplement"] == ""
    assert agentic["agentic_answer"] == ""
    assert agentic["error_message"] == "agentic unavailable"
    assert generation["assistant_message"] == "externally verified answer"
    assert updates["assistant_message"] == "externally verified answer"
    assert memory.normal_generation_calls[0][0] == [{"role": "system", "content": "final prompt supplement: "}]


def test_demo_final_answer_model_exception_still_fails_generation_step(tmp_path):
    memory = _AgenticDemoMemory()
    memory.final_generation_error = RuntimeError("final answer unavailable")

    with pytest.raises(RuntimeError, match="final answer unavailable"):
        _execute_demo_generation(tmp_path, memory)

    assert len(memory.agentic_calls) == 1
    assert len(memory.normal_generation_calls) == 1


@pytest.mark.parametrize(
    "enabled,tool_call_count,expected_status,expected_agentic_status",
    [
        (False, 0, "skipped", "disabled"),
        (True, 0, "succeeded", "not_needed"),
        (True, 1, "succeeded", "supplemented"),
        (True, 1, "succeeded", "no_relevant_memory"),
    ],
)
def test_real_agentic_step_persists_disabled_no_retrieval_and_retrieval_states(
    tmp_path,
    enabled,
    tool_call_count,
    expected_status,
    expected_agentic_status,
):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    memory = _AgenticDemoMemory()
    memory.agentic_enabled = enabled
    if expected_agentic_status == "not_needed":
        memory.agentic_result.update(
            status="not_needed",
            supplement="",
            answer="",
            iterations=1,
            tool_call_count=0,
            stop_reason="not_needed",
            tool_trace=[],
        )
    elif expected_agentic_status == "no_relevant_memory":
        memory.agentic_result.update(
            status="no_relevant_memory",
            supplement="",
            answer="",
            iterations=1,
            tool_call_count=1,
            stop_reason="no_relevant_memory",
        )
    pipeline = DemoPipelineService(memory, repository, state_service=object())
    coordinator = DemoBackgroundCoordinator("agentic-states", repository)
    pipeline.coordinator = coordinator
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="question",
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        agentic = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        assert agentic["status"] == expected_status
        assert agentic["output"]["agentic_status"] == expected_agentic_status
        assert agentic["attempts"] == 1
        assert agentic["duration_ms"] is not None
        assert repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["status"] == "succeeded"
        assert len(memory.agentic_calls) == int(enabled)
    finally:
        coordinator.shutdown(wait=True)


def test_agentic_failure_is_persisted_as_degraded_and_downstream_continues(tmp_path):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("simulation-1", "user-1", "run-1")
    memory = _AgenticDemoMemory()
    memory.agentic_error = RuntimeError("agentic unavailable")
    pipeline = DemoPipelineService(memory, repository, state_service=object())
    coordinator = DemoBackgroundCoordinator("agentic-retry", repository)
    pipeline.coordinator = coordinator
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="question",
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        degraded = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        assert degraded["status"] == "succeeded"
        assert degraded["attempts"] == 1
        assert degraded["error_message"] is None
        assert degraded["output"]["agentic_status"] == "degraded"
        assert degraded["output"]["error_message"] == "agentic unavailable"
        assert repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["status"] == "succeeded"
        assert repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["status"] == "succeeded"
        assert len(memory.normal_generation_calls) == 1
    finally:
        coordinator.shutdown(wait=True)
