import hashlib
import json
import threading
from copy import deepcopy
from html.parser import HTMLParser
from types import SimpleNamespace

from mem0.configs.base import AgenticRetrievalConfig
from memory_monitor.components import pipeline_graph, styles
from memory_monitor.models import PIPELINE_STEPS, BackgroundStepConfig, PipelineStep, StepStatus
from memory_monitor.runtime import DemoBackgroundCoordinator, DemoMemory
from memory_monitor.runtime.llm_trace import TracedToolExecutor
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository
from tests.memory_monitor.test_demo_pipeline import _FakeDemoMemory, _FakeStateService


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        del attrs
        self.tags.append(tag)

    def handle_data(self, data):
        self.text.append(data)


def _css_rule(css, selector):
    marker = f"{selector} {{"
    return css.split(marker, 1)[1].split("}", 1)[0]


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
        exclude_midterm_page_ids=None,
    ):
        del exclude_midterm_page_ids
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
        [first_response, "historical memory supplement", "final answer"],
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        agentic = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        calls = agentic["output"]["llm_calls"]
        assert [call["sequence"] for call in calls] == [1, 2]
        assert [call["status"] for call in calls] == ["succeeded", "succeeded"]
        assert len(calls[0]["messages"]) == 1
        assert "<user_query>\nquestion\n</user_query>" in calls[0]["messages"][0]["content"]
        assert calls[0]["tools"][0]["function"]["name"] == "search_memory"
        assert calls[0]["parameters"] == {"temperature": 0.2, "tool_choice": "auto"}
        assert calls[0]["response"] == first_response
        assert calls[1]["response"] == "historical memory supplement"
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
        assert "<agentic_memory_supplement>\nhistorical memory supplement\n</agentic_memory_supplement>" in final_prompt
        assert "外部记忆工具判断是否需要补充检索" not in final_prompt
    finally:
        coordinator.shutdown(wait=True)


def test_not_needed_hides_accidental_agentic_answer_and_keeps_final_generation(tmp_path):
    accidental_answer = "华辰智能装备的业务结构以机器人控制器为主、智能仓储设备为辅。"
    pipeline, coordinator, repository, session, turn = _agentic_pipeline(
        tmp_path,
        [accidental_answer, "最终模型根据当前问题生成业务结构概括。"],
    )
    try:
        pipeline.run_to_answer(turn["turn_id"], session_id=session["session_id"])
        assert coordinator.wait_for_idle(3)

        agentic = repository.get_step(turn["turn_id"], PipelineStep.AGENTIC_RETRIEVAL)
        assert agentic["output"]["agentic_status"] == "not_needed"
        assert agentic["output"]["agentic_memory_supplement"] == agentic["output"]["agentic_answer"] == ""
        assert agentic["output"]["tool_call_count"] == 0
        assert agentic["output"]["llm_calls"][0]["response"] is None
        assert agentic["output"]["llm_calls"][0]["response_suppressed"] is True
        assert accidental_answer not in json.dumps(agentic["output"], ensure_ascii=False)

        final_prompt = repository.get_step(turn["turn_id"], PipelineStep.BUILD_PROMPT)["output"]["messages"][0][
            "content"
        ]
        assert accidental_answer not in final_prompt
        assert "<agentic_memory_supplement>\n\n</agentic_memory_supplement>" in final_prompt
        assert repository.get_step(turn["turn_id"], PipelineStep.GENERATE_RESPONSE)["output"]["assistant_message"] == (
            "最终模型根据当前问题生成业务结构概括。"
        )
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
        assert degraded["output"]["agentic_status"] == "degraded"
        assert degraded["output"]["agentic_memory_supplement"] == ""
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
        self.requests = []
        self.lock = threading.Lock()

    def generate_response(self, **kwargs):
        with self.lock:
            self.requests.append(deepcopy(kwargs))
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
        sent_by_branch = {
            request["branch"]: request["messages"] for request in memory.llm._wrapped.requests
        }
        for step, label in (
            (PipelineStep.RUN_MIDTERM, "midterm"),
            (PipelineStep.RUN_LONGTERM, "longterm"),
            (PipelineStep.RUN_PROFILE, "profile"),
        ):
            calls = repository.get_step(turn["turn_id"], step)["output"]["llm_calls"]
            assert len(calls) == 1
            assert calls[0]["messages"] == [{"role": "user", "content": label}]
            assert calls[0]["messages"] == sent_by_branch[label]
            assert calls[0]["parameters"] == {"branch": label}
    finally:
        coordinator.shutdown(wait=True)


def test_midterm_longterm_and_profile_answers_share_pretty_json_renderer():
    steps = [{"step": step.value, "status": StepStatus.PENDING.value, "attempts": 0} for step in PIPELINE_STEPS]
    branch_steps = (
        (PipelineStep.RUN_MIDTERM, "midterm", "中期摘要"),
        (PipelineStep.RUN_LONGTERM, "longterm", "长期事实"),
        (PipelineStep.RUN_PROFILE, "profile", "用户画像"),
    )
    for pipeline_step, branch, value in branch_steps:
        step = next(item for item in steps if item["step"] == pipeline_step.value)
        step.update(
            status=StepStatus.SUCCEEDED.value,
            attempts=1,
            output={
                "llm_calls": [
                    {
                        "sequence": 1,
                        "purpose": branch,
                        "messages": [{"role": "user", "content": f"{branch} input"}],
                        "response": json.dumps({"branch": branch, "items": [value]}, ensure_ascii=False),
                        "status": "succeeded",
                    }
                ]
            },
        )

    rendered = pipeline_graph.render_html(steps, BackgroundStepConfig())

    assert rendered.count('class="demo-markdown-text demo-json-block"') == 3
    for _, branch, value in branch_steps:
        assert f'  "branch": "{branch}"' in rendered
        assert f'    "{value}"' in rendered
        assert f'{{"branch": "{branch}"' not in rendered
    assert "\\u" not in rendered


def test_mixed_prompt_json_blocks_render_separately_without_changing_trace_content():
    prompt = (
        "说明 <script>alert('plain')</script>\n"
        "<short_term_memory>\n"
        '[{"content":"用户问题","nested":{"brackets":"{中文} [内容]"}}]\n'
        "</short_term_memory>\n"
        "参考信息：\n"
        '{"items":[{"text":"</div><script>alert(1)</script>"}]}\n'
        "结束。"
    )
    messages = [{"role": "system", "content": prompt}]
    original_messages = deepcopy(messages)

    rendered = pipeline_graph._prompt_html(messages)

    assert rendered.count('class="demo-markdown-text demo-json-block"') == 2
    assert rendered.count("```json") == 2
    assert '  "content": "用户问题"' in rendered
    assert '  "items": [' in rendered
    assert '[{"content":"用户问题"' not in rendered
    assert '{"items":[{"text"' not in rendered
    assert "说明 &lt;script&gt;alert('plain')&lt;/script&gt;" in rendered
    assert "&lt;short_term_memory&gt;" in rendered
    assert "&lt;/short_term_memory&gt;" in rendered
    assert rendered.index("说明") < rendered.index("用户问题") < rendered.index("参考信息")
    assert rendered.index("参考信息") < rendered.index("items") < rendered.index("结束。")
    assert messages == original_messages


def test_existing_fenced_json_prompt_remains_one_markdown_block():
    prompt = '已有代码块：\n```json\n{"items":[{"nested":true}]}\n```\n结束。'

    rendered = pipeline_graph._render_call_text_html(prompt)

    assert rendered.count('class="demo-markdown-text"') == 1
    assert 'class="demo-markdown-text demo-json-block"' not in rendered
    assert rendered.count("```json") == 1
    assert prompt in rendered


def test_complete_compact_json_prompt_uses_existing_pretty_json_block():
    prompt = '{"answer":{"items":["中文"]}}'

    rendered = pipeline_graph._render_call_text_html(prompt)

    assert rendered.count('class="demo-markdown-text demo-json-block"') == 1
    assert '  "answer": {' in rendered
    assert '    "items": [' in rendered
    assert prompt not in rendered


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
            "agentic_status": "supplemented",
            "llm_calls": [
                {
                    "sequence": 1,
                    "purpose": "Agentic 判断",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "规则\n\n```text\n<current_time>\n2026-08-03\n</current_time>\n\n"
                                '<short_term_memory>\n[\n  {\n    "content": "中文"\n  }\n]\n'
                                "</short_term_memory>\n```"
                            ),
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
                    "purpose": "整理记忆补充",
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
    safe_user_prompt = pipeline_graph._escape_markdown_html(dangerous_prompt)
    parser = _VisibleTextParser()
    parser.feed(safe_user_prompt)
    visible_text = "".join(parser.text)

    assert dangerous_prompt not in rendered
    assert '<script data-x="1">' not in rendered
    assert 'data-x="1"' in rendered
    assert tool_only_danger not in rendered
    assert "LLM ×2" in rendered
    assert "工具 ×1" in rendered
    assert "已补充" in rendered
    assert 'class="demo-node-popover"' in rendered
    assert rendered.index("模型调用 1") < rendered.index("模型调用 2")
    assert rendered.count('class="demo-call-heading">Prompt</div>') == 2
    assert rendered.count('class="demo-call-heading">模型回答</div>') == 2
    assert all(role in rendered for role in ("System", "User", "Assistant", "Tool"))
    assert '<div class="demo-markdown-text">\n\n规则\n\n```text\n<current_time>' in rendered
    assert "&lt;current_time&gt;" not in rendered
    assert "<short_term_memory>" in rendered
    assert "&lt;short_term_memory&gt;" not in rendered
    assert "&quot;content&quot;" not in rendered
    assert '"content": "中文"' in rendered
    assert dangerous_prompt in visible_text
    assert all(entity not in visible_text for entity in ("&lt;", "&gt;", "&quot;"))
    assert "script" not in parser.tags
    assert "img" not in parser.tags
    assert "第一轮回答\n\n- 中文内容" in rendered
    assert "第二轮回答\n\n最终内容" in rendered
    assert "\\n" not in rendered
    assert '"role"' not in rendered
    assert "Prompt / messages" not in rendered
    assert ">Tools<" not in rendered
    assert "调用参数" not in rendered
    assert "tool_choice" not in rendered
    assert "temperature" not in rendered
    assert "position: fixed" in styles._DEMO_LAB_CSS
    assert ".demo-popover-call + .demo-popover-call" in styles._DEMO_LAB_CSS
    assert "<br" not in rendered


def test_hover_panel_stays_interactive_during_delayed_hide_and_fits_viewport():
    css = styles._DEMO_LAB_CSS
    popover_rule = _css_rule(css, ".demo-node-popover")
    trigger_selector = ".demo-node:hover .demo-node-popover,\n.demo-node:focus-within .demo-node-popover"
    trigger_rule = _css_rule(css, trigger_selector)
    popover_hover_rule = _css_rule(css, ".demo-node-popover:hover")

    assert "display: none" not in popover_rule
    assert "display: block" not in trigger_rule
    assert "visibility: hidden" in popover_rule
    assert "opacity: 0" in popover_rule
    assert "opacity 100ms ease 240ms" in popover_rule
    assert "visibility 0s linear 340ms" in popover_rule
    assert "pointer-events" not in popover_rule

    for visible_rule in (trigger_rule, popover_hover_rule):
        assert "visibility: visible" in visible_rule
        assert "opacity: 1" in visible_rule
        assert "transition-delay: 0s" in visible_rule

    assert "position: fixed" in popover_rule
    assert "right: 1.25rem" in popover_rule
    assert "top: 6rem" in popover_rule
    assert "top: 4.75rem" not in popover_rule
    assert "max-height: min(72vh, 720px, calc(100vh - 7.25rem))" in popover_rule
    assert "overflow: auto" in popover_rule
    assert "overscroll-behavior: contain" in popover_rule
    assert "scrollbar-gutter: stable" in popover_rule
    assert "z-index: 100000" in popover_rule


def test_hover_panel_uses_a_centralized_readable_type_scale():
    css = styles._DEMO_LAB_CSS

    assert "--demo-popover-body-font-size: 0.95rem" in css
    assert "--demo-popover-body-line-height: 1.5" in css
    assert "--demo-popover-title-font-size: 0.95rem" in css
    assert "font-size: var(--demo-popover-body-font-size)" in css
    assert "line-height: var(--demo-popover-body-line-height)" in css
    assert ".demo-node-popover .demo-markdown-text :where(h1, h2, h3, h4, h5, h6)" in css
    assert ".demo-node-popover .demo-markdown-text pre" in css
    assert "font-family: ui-monospace" in css


def test_hover_panel_scopes_compact_markdown_layout_and_readable_code_colors():
    css = styles._DEMO_LAB_CSS
    root_selector = ".demo-node-popover .demo-markdown-text"
    list_selector = ".demo-node-popover .demo-markdown-text :where(ul, ol)"
    item_selector = ".demo-node-popover .demo-markdown-text li"
    item_paragraph_selector = ".demo-node-popover .demo-markdown-text li > p"
    paragraph_selector = ".demo-node-popover .demo-markdown-text p"
    pre_selector = ".demo-node-popover .demo-markdown-text pre"
    code_selector = ".demo-node-popover .demo-markdown-text :where(pre, code)"
    nested_code_selector = ".demo-node-popover .demo-markdown-text pre code span"

    assert "white-space: normal" in _css_rule(css, root_selector)
    assert "pre-wrap" not in _css_rule(css, root_selector)
    assert "margin: 0.2rem 0" in _css_rule(css, list_selector)
    assert "white-space: normal" in _css_rule(css, list_selector)
    assert "margin: 0.06rem 0" in _css_rule(css, item_selector)
    assert "white-space: normal" in _css_rule(css, item_selector)
    assert "white-space: pre-wrap" in _css_rule(css, paragraph_selector)
    assert "margin: 0" in _css_rule(css, item_paragraph_selector)
    assert "white-space: normal" in _css_rule(css, item_paragraph_selector)
    assert "color: #e5e7eb !important" in _css_rule(css, pre_selector)
    assert "white-space: pre" in _css_rule(css, pre_selector)
    assert "overflow-x: auto" in _css_rule(css, pre_selector)
    assert "color: #e5e7eb !important" in _css_rule(css, code_selector)
    assert "color: inherit !important" in _css_rule(css, nested_code_selector)
    assert "text-shadow: none !important" in _css_rule(css, nested_code_selector)
