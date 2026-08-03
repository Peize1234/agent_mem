import hashlib
import html
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

from mem0.configs.base import AgenticRetrievalConfig
from memory_monitor.components import pipeline_graph, styles
from memory_monitor.models import PIPELINE_STEPS, BackgroundStepConfig, PipelineStep, StepStatus
from memory_monitor.runtime import DemoBackgroundCoordinator, DemoMemory
from memory_monitor.runtime.llm_trace import TracedToolExecutor
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository
from tests.memory_monitor.test_demo_pipeline import _FakeDemoMemory, _FakeStateService


class _ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.requests = []
        self.lock = threading.Lock()

    def generate_response(self, **kwargs):
        with self.lock:
            index = self.calls
            self.calls += 1
            self.requests.append(deepcopy(kwargs))
        response = self.script[index]
        if isinstance(response, BaseException):
            raise response
        return deepcopy(response)


class _ToolExecutor:
    def execute(self, name, arguments):
        return {
            "ok": True,
            "items": [{"memory": "完整中文检索结果", "score": 0.93}],
            "query": arguments["queries"][0],
        }


class _TracingAgenticMemory(DemoMemory):
    def __init__(self, llm):
        self.llm = llm
        self.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))

    def retrieve_context_for_demo(self, query, *, user_id, session_id):
        context = {
            "query": query,
            "user_id": user_id,
            "session_id": session_id,
            "short_term_messages": [],
            "retrieved_memories": [],
            "profile": {},
            "agentic_retrieval": True,
        }
        context["context_hash"] = self.context_hash(context)
        return context

    def _create_agentic_tool_executor(
        self,
        *,
        user_id,
        session_id,
        record_midterm_visits,
    ):
        return TracedToolExecutor(_ToolExecutor())

    @staticmethod
    def context_hash(context):
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


def _agentic_pipeline(tmp_path, script):
    repository = DemoRepository(tmp_path / "demo.db")
    session = repository.create_session("trace", "user-1", "run-1")
    memory = _TracingAgenticMemory(_ScriptedLLM(script))
    pipeline = DemoPipelineService(
        memory,
        repository,
        state_service=object(),
        generation_kwargs={"temperature": 0.2},
    )
    coordinator = DemoBackgroundCoordinator("trace", repository)
    pipeline.coordinator = coordinator
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="question",
    )
    return pipeline, coordinator, repository, session, turn


def test_multiple_llm_calls_and_complete_tool_result_are_persisted_in_order(tmp_path):
    first_response = {
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search_memory", "arguments": '{"queries":["private query"]}'},
            }
        ],
    }
    pipeline, coordinator, repository, session, turn = _agentic_pipeline(
        tmp_path,
        [first_response, "candidate answer", "final answer"],
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        agentic = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        calls = agentic["output"]["llm_calls"]
        assert [call["sequence"] for call in calls] == [1, 2]
        assert [call["status"] for call in calls] == ["succeeded", "succeeded"]
        assert calls[0]["messages"][-1]["content"] == "question"
        assert calls[0]["tools"][0]["function"]["name"] == "search_memory"
        assert calls[0]["parameters"] == {"temperature": 0.2, "tool_choice": "auto"}
        assert calls[0]["response"] == first_response
        assert calls[1]["response"] == "candidate answer"
        assert calls[0]["messages"] == pipeline.memory.llm._wrapped.requests[0]["messages"]
        assert calls[1]["messages"] == pipeline.memory.llm._wrapped.requests[1]["messages"]
        second_messages = calls[1]["messages"]
        assert [message["role"] for message in second_messages[-3:]] == ["assistant", "tool", "system"]
        assert second_messages[-3]["tool_calls"][0]["function"]["name"] == "search_memory"
        assert second_messages[-3]["tool_calls"][0]["function"]["arguments"] == (
            '{\n  "queries": [\n    "private query"\n  ]\n}'
        )
        assert second_messages[-2]["name"] == "search_memory"
        assert second_messages[-2]["content"].startswith('{\n  "ok": true,\n  "items": [')
        assert "完整中文检索结果" in second_messages[-2]["content"]
        assert "\\u" not in second_messages[-2]["content"]
        assert "不得再次请求任何工具" in second_messages[-1]["content"]
        assert all(call["duration_ms"] is not None for call in calls)
        assert all(call["purpose"] == "Agentic 检索" for call in calls)

        tools = agentic["output"]["tool_calls"]
        assert len(tools) == 1
        assert tools[0]["name"] == "search_memory"
        assert tools[0]["arguments"] == {"queries": ["private query"]}
        assert tools[0]["result"]["items"][0]["memory"] == "完整中文检索结果"

        build = repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)
        generation = repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)
        assert len(generation["output"]["llm_calls"]) == 1
        assert generation["output"]["llm_calls"][0]["purpose"] == "生成最终回答"
        assert generation["output"]["llm_calls"][0]["messages"] == pipeline.memory.llm._wrapped.requests[2]["messages"]
        assert generation["output"]["llm_calls"][0]["messages"] == build["output"]["messages"]
        final_prompt = build["output"]["messages"][0]["content"]
        assert "<agentic_answer>\ncandidate answer\n</agentic_answer>" in final_prompt
        assert "外部记忆工具判断是否需要补充检索" not in final_prompt
    finally:
        coordinator.shutdown(wait=True)


def test_failed_agentic_call_keeps_trace_and_degrades_to_final_answer(tmp_path):
    first_response = {
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "search_memory", "arguments": '{"queries":["private query"]}'},
            }
        ],
    }
    pipeline, coordinator, repository, session, turn = _agentic_pipeline(
        tmp_path,
        [first_response, RuntimeError("second call failed"), "final"],
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)
        degraded = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        assert degraded["status"] == "succeeded"
        assert degraded["output"]["agentic_status"] == "failed_degraded"
        assert degraded["output"]["error_message"] == "second call failed"
        assert [call["status"] for call in degraded["output"]["llm_calls"]] == ["succeeded", "failed"]
        assert degraded["output"]["llm_calls"][1]["error_message"] == "second call failed"
        assert len(degraded["output"]["tool_calls"]) == 1
        assert repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["status"] == "succeeded"
        generation = repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)
        assert generation["status"] == "succeeded"
        assert generation["output"]["assistant_message"] == "final"
    finally:
        coordinator.shutdown(wait=True)


class _ParallelLLM:
    def __init__(self):
        self.barrier = threading.Barrier(3)

    def generate_response(self, **kwargs):
        self.barrier.wait(timeout=2)
        return {"content": kwargs["messages"][0]["content"]}


def test_parallel_memory_branch_llm_traces_are_isolated_by_step(tmp_path):
    memory = _FakeDemoMemory()
    memory.llm = _ParallelLLM()
    original_methods = {
        "midterm": memory.demo_background_worker.process_midterm_job,
        "longterm": memory.demo_background_worker.process_longterm_job,
        "profile": memory.demo_background_worker.process_profile_job,
    }

    def traced_processor(label):
        def process(job_id):
            memory.llm.generate_response(messages=[{"role": "user", "content": label}], branch=label)
            return original_methods[label](job_id)

        return process

    memory.demo_background_worker.process_midterm_job = traced_processor("midterm")
    memory.demo_background_worker.process_longterm_job = traced_processor("longterm")
    memory.demo_background_worker.process_profile_job = traced_processor("profile")
    repository = DemoRepository(tmp_path / "parallel.db")
    session = repository.create_session("parallel", "user-1", "run-1")
    pipeline = DemoPipelineService(memory, repository, _FakeStateService(memory))
    coordinator = DemoBackgroundCoordinator("parallel", repository)
    pipeline.coordinator = coordinator
    turn = pipeline.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="parallel",
    )
    try:
        pipeline.run_all(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(4)
        for step, label in (
            (PipelineStep.RUN_MIDTERM, "midterm"),
            (PipelineStep.RUN_LONGTERM, "longterm"),
            (PipelineStep.RUN_PROFILE, "profile"),
        ):
            calls = repository.get_step(turn["turn_id"], step)["output"]["llm_calls"]
            assert len(calls) == 1
            assert calls[0]["messages"] == [{"role": "user", "content": label}]
            assert calls[0]["parameters"] == {"branch": label}
    finally:
        coordinator.shutdown(wait=True)


def test_legacy_completed_turn_gets_persisted_compatibility_step_without_timing(tmp_path):
    db_path = tmp_path / "legacy.db"
    repository = DemoRepository(db_path)
    session = repository.create_session("legacy", "user-1", "run-1")
    turn = repository.create_turn(
        session["session_id"],
        user_id="user-1",
        run_id="run-1",
        user_message="historical question",
    )
    with repository._connection() as connection:
        connection.execute(
            "DELETE FROM demo_step_runs WHERE turn_id = ? AND step = ?",
            (turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL.value),
        )
        connection.execute(
            "UPDATE demo_step_runs SET status = 'succeeded' WHERE turn_id = ? AND step = ?",
            (turn["turn_id"], PipelineStep.GENERATE_RESPONSE.value),
        )
        connection.execute(
            """
            UPDATE demo_turns
            SET assistant_message = 'historical answer', completed_at = '2026-01-01T00:00:00+00:00'
            WHERE turn_id = ?
            """,
            (turn["turn_id"],),
        )
        connection.commit()

    reopened = DemoRepository(db_path)
    agentic = reopened.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
    assert agentic["status"] == "skipped"
    assert agentic["attempts"] == 0
    assert agentic["started_at"] is None
    assert agentic["ended_at"] is None
    assert agentic["duration_ms"] is None
    assert agentic["output"]["agentic_status"] == "legacy_compatible"
    assert reopened.get_turn(turn["turn_id"])["assistant_message"] == "historical answer"


def test_pipeline_hover_trace_renders_only_readable_prompt_and_answer():
    steps = [{"step": step.value, "status": StepStatus.PENDING.value, "attempts": 0} for step in PIPELINE_STEPS]
    agentic = next(step for step in steps if step["step"] == PipelineStep.AGENTIC_RETRIEVAL.value)
    dangerous_prompt = '</div><script data-x="1">alert(1)</script>\n\n- **保留 Markdown**'
    tool_only_danger = "<img src=x onerror=alert(2)>"
    agentic.update(
        status=StepStatus.SUCCEEDED.value,
        attempts=1,
        duration_ms=12.4,
        output={
            "agentic_status": "retrieved",
            "llm_calls": [
                {
                    "sequence": 1,
                    "purpose": "Agentic 判断",
                    "messages": [
                        {
                            "role": "system",
                            "content": '规则\n\n<short_term_memory>\n[\n  {"content": "中文"}\n]\n</short_term_memory>',
                        },
                        {"role": "user", "content": dangerous_prompt},
                    ],
                    "tools": [{"name": tool_only_danger}],
                    "parameters": {"tool_choice": "auto", "unsafe": tool_only_danger},
                    "response": {
                        "choices": [{"message": {"content": "第一轮回答\n\n- 中文内容"}}],
                        "usage": {"tokens": 12},
                    },
                    "duration_ms": 4.2,
                    "status": "succeeded",
                },
                {
                    "sequence": 2,
                    "purpose": "生成候选回答",
                    "messages": [
                        {"role": "assistant", "content": "已有候选"},
                        {"role": "tool", "content": "检索内容\n```text\n安全代码块\n```"},
                    ],
                    "tools": [{"name": tool_only_danger}],
                    "parameters": {"temperature": 0.2},
                    "response": {
                        "content": [
                            {"type": "text", "text": "第二轮回答"},
                            {"type": "text", "text": "最终内容"},
                        ]
                    },
                    "duration_ms": 7.1,
                    "status": "succeeded",
                },
            ],
            "tool_calls": [
                {
                    "sequence": 1,
                    "name": tool_only_danger,
                    "arguments": {"query": tool_only_danger},
                    "result": {"memory": tool_only_danger},
                }
            ],
        },
    )

    rendered = pipeline_graph.render_html(steps, BackgroundStepConfig())

    assert dangerous_prompt not in rendered
    assert "&lt;script data-x=&quot;1&quot;&gt;alert(1)&lt;/script&gt;" in rendered
    assert tool_only_danger not in rendered
    assert html.escape(tool_only_danger) not in rendered
    assert "LLM ×2" in rendered
    assert "工具 ×1" in rendered
    assert "已补充检索" in rendered
    assert 'class="demo-node-popover"' in rendered
    assert rendered.index("模型调用 1") < rendered.index("模型调用 2")
    assert rendered.count('class="demo-call-heading">Prompt</div>') == 2
    assert rendered.count('class="demo-call-heading">模型回答</div>') == 2
    assert all(role in rendered for role in ("System", "User", "Assistant", "Tool"))
    assert "规则\n\n&lt;short_term_memory&gt;\n[\n  {&quot;content&quot;: &quot;中文&quot;}" in rendered
    assert "<short_term_memory>" in html.unescape(rendered)
    assert "&amp;lt;short_term_memory&amp;gt;" not in rendered
    assert "第一轮回答\n\n- 中文内容" in rendered
    assert "第二轮回答\n\n最终内容" in rendered
    assert "\\n" not in rendered
    assert '"role"' not in rendered
    assert '"content"' not in rendered
    assert "Prompt / messages" not in rendered
    assert ">Tools<" not in rendered
    assert "调用参数" not in rendered
    assert "tool_choice" not in rendered
    assert "temperature" not in rendered
    assert "position: fixed" in styles._DEMO_LAB_CSS
    assert "max-height: min(72vh, 720px)" in styles._DEMO_LAB_CSS
    assert "white-space: pre-wrap" in styles._DEMO_LAB_CSS
    assert ".demo-popover-call + .demo-popover-call" in styles._DEMO_LAB_CSS
    assert "overflow: auto" in styles._DEMO_LAB_CSS


def test_hover_panel_uses_a_centralized_readable_type_scale():
    css = styles._DEMO_LAB_CSS

    assert "--demo-popover-body-font-size: 0.9rem" in css
    assert "--demo-popover-body-line-height: 1.55" in css
    assert "--demo-popover-title-font-size: 0.875rem" in css
    assert "font-size: var(--demo-popover-body-font-size)" in css
    assert "line-height: var(--demo-popover-body-line-height)" in css
    assert ".demo-markdown-text :where(p, ul, ol, li, pre, code)" in css
