import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

from mem0.configs.base import AgenticRetrievalConfig
from memory_monitor.models import PipelineStep
from memory_monitor.runtime.demo_memory import DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository


class _AgenticDemoMemory:
    def __init__(self):
        self.agentic_calls = []
        self.normal_generation_calls = []
        self.commit_calls = []

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

    def build_prompt_from_context(self, context):
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
        return {
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

    def generate_response_for_demo(self, messages, **kwargs):
        self.normal_generation_calls.append((deepcopy(messages), deepcopy(kwargs)))
        raise AssertionError("agentic mode must not use the one-shot generation method")

    def commit_demo_turn(self, **kwargs):
        self.commit_calls.append(deepcopy(kwargs))
        return {"results": [], "background": {"migration_job_id": "m1", "profile_job_id": "p1"}}

    @staticmethod
    def context_hash(context):
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def test_demo_memory_switch_uses_base_context_and_agentic_prompt_without_generation():
    memory = DemoMemory.__new__(DemoMemory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))
    memory.llm = MagicMock()
    memory._retrieve_context = MagicMock(side_effect=AssertionError("one-shot retrieval must stay disabled"))
    memory._retrieve_base_context = MagicMock(
        return_value={
            "query": "historical question",
            "user_id": "user-1",
            "session_id": "run-1",
            "profile": {"risk_level": "balanced"},
            "short_term_messages": [{"role": "user", "content": "recent"}],
            "retrieved_memories": [],
        }
    )

    context = memory.retrieve_context_for_demo("historical question", user_id="user-1", session_id="run-1")
    prompt = memory.build_prompt_from_context(context)

    assert context["agentic_retrieval"] is True
    assert context["mid_term"] == context["long_term"] == []
    assert prompt[-1] == {"role": "user", "content": "historical question"}
    assert "外部记忆工具" in prompt[0]["content"]
    memory._retrieve_context.assert_not_called()
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
    }[step]
    generation, updates = pipeline._execute(turn, PipelineStep.GENERATE_RESPONSE, {})

    assert generation["assistant_message"] == "agentic final answer"
    assert generation["iterations"] == 2
    assert generation["tool_call_count"] == 1
    assert generation["tool_trace"][0]["arguments"]["queries"] == ["private search term"]
    assert updates["assistant_message"] == "agentic final answer"
    assert memory.normal_generation_calls == []
    assert memory.agentic_calls[0]["user_id"] == "user-1"
    assert memory.agentic_calls[0]["session_id"] == "run-1"

    pipeline._step_output = lambda _turn_id, step: {
        PipelineStep.GENERATE_RESPONSE: generation,
    }[step]
    pipeline._execute(turn, PipelineStep.RUN_SHORTTERM, {})

    assert len(memory.commit_calls) == 1
    commit = memory.commit_calls[0]
    assert commit["user_message"] == "original question"
    assert commit["assistant_message"] == "agentic final answer"
    assert "tool_trace" not in commit
    assert "prompt_messages" not in commit
    assert "raw_response" not in commit
