import asyncio
import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from mem0.configs.base import AgenticRetrievalConfig, MemoryConfig
from mem0.memory import retrieval_tools
from mem0.memory.agentic_retrieval import AgenticMemoryRunner, AsyncAgenticMemoryRunner
from mem0.memory.main import Memory
from mem0.memory.retrieval_tools import (
    MEMORY_TOOLS,
    SEARCH_MEMORY_TOOL,
    AsyncMemoryToolExecutor,
    MemoryToolExecutor,
)


def _tool_call(call_id, arguments, *, name="search_memory"):
    return {
        "content": None,
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
    }


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_response(self, **kwargs):
        self.calls.append(deepcopy(kwargs))
        return self.responses.pop(0)


class _FakeMidtermRetriever:
    def __init__(self, results_by_query=None, delays_by_query=None):
        self.results_by_query = results_by_query or {}
        self.delays_by_query = delays_by_query or {}
        self.calls = []
        self._lock = threading.Lock()
        self.active_searches = 0
        self.max_active_searches = 0

    def search(self, query, filters, *, record_visits=True, candidate_pool_size=None):
        with self._lock:
            self.calls.append((query, deepcopy(filters), record_visits, candidate_pool_size))
            self.active_searches += 1
            self.max_active_searches = max(self.max_active_searches, self.active_searches)
        try:
            delay = self.delays_by_query.get(query, 0)
            if delay:
                time.sleep(delay)
            result = self.results_by_query.get(query, [])
            if isinstance(result, Exception):
                raise result
            return deepcopy(result)
        finally:
            with self._lock:
                self.active_searches -= 1


class _FakeMidtermMemory:
    def __init__(self, visit_error=None):
        self.visits = []
        self.visit_error = visit_error

    def record_session_visit(self, session_id):
        self.visits.append(session_id)
        if self.visit_error is not None:
            raise self.visit_error


class _FakeMemory:
    def __init__(
        self,
        results_by_query=None,
        *,
        delays_by_query=None,
        midterm_enabled=True,
        visit_error=None,
    ):
        self.midterm_retriever = _FakeMidtermRetriever(results_by_query, delays_by_query)
        self.midterm_memory = _FakeMidtermMemory(visit_error)
        self.midterm_enabled = midterm_enabled
        self.longterm_calls = []
        self.search_calls = []

    def _midterm_enabled(self):
        return self.midterm_enabled

    def _search_vector_store(self, *args, **kwargs):
        self.longterm_calls.append((deepcopy(args), deepcopy(kwargs)))
        raise AssertionError("agentic retrieval must not query the long-term vector store")

    def search(self, *args, **kwargs):
        self.search_calls.append((deepcopy(args), deepcopy(kwargs)))
        raise AssertionError("agentic retrieval must not call Memory.search")


def _session(session_id="session-1", summary="用户讨论过投资组合风险控制"):
    return {
        "id": session_id,
        "session_id": session_id,
        "source": "mid_term_session",
        "summary": summary,
        "memory": summary,
        "score": 0.95,
    }


def _page(
    page_id,
    score,
    *,
    session_id="session-1",
    summary="用户明确说明了最大可接受亏损比例",
    raw_dialogue="用户表示最多能够接受本金亏损10%，投资期限不少于三年。",
):
    return {
        "id": page_id,
        "session_id": session_id,
        "source": "mid_term_page",
        "summary": summary,
        "raw_dialogue": raw_dialogue,
        "memory": summary,
        "score": score,
        "created_at": "2026-07-30T10:00:00+08:00",
    }


def _default_results():
    return [_session(), _page("page-1", 0.82)]


def _executor(memory=None, *, record_midterm_visits=False, **overrides):
    return MemoryToolExecutor(
        memory or _FakeMemory({"风险偏好": _default_results()}),
        user_id="user-1",
        run_id="run-1",
        config=AgenticRetrievalConfig(**overrides),
        record_midterm_visits=record_midterm_visits,
    )


def test_agentic_config_defaults_off_and_validates_low_latency_limits():
    config = AgenticRetrievalConfig()

    assert MemoryConfig().agentic_retrieval.enabled is False
    assert config.max_iterations == 2
    assert config.max_tool_calls == 1
    assert config.max_queries == 3
    assert config.candidate_pool_size == 20
    assert config.max_total_results == 6
    assert "max_chars_per_result" not in AgenticRetrievalConfig.model_fields
    with pytest.raises(ValidationError):
        AgenticRetrievalConfig(max_iterations=3)
    with pytest.raises(ValidationError):
        AgenticRetrievalConfig(max_tool_calls=2)
    with pytest.raises(ValidationError):
        AgenticRetrievalConfig(max_queries=4)
    with pytest.raises(ValidationError):
        AgenticRetrievalConfig(default_threshold=1.1)


def test_only_search_memory_tool_with_queries_is_exposed():
    properties = SEARCH_MEMORY_TOOL["function"]["parameters"]["properties"]

    assert MEMORY_TOOLS == [SEARCH_MEMORY_TOOL]
    assert set(properties) == {"queries"}
    assert properties["queries"]["minItems"] == 1
    assert properties["queries"]["maxItems"] == 3
    assert not hasattr(retrieval_tools, "READ_MEMORY_RESULT_TOOL")


def test_current_context_sufficient_uses_one_llm_call_and_no_retrieval():
    memory = _FakeMemory()
    llm = _ScriptedLLM([{"content": "直接回答", "tool_calls": []}])
    result = AgenticMemoryRunner(llm, _executor(memory), AgenticRetrievalConfig()).run(
        [{"role": "user", "content": "你好"}]
    )

    assert result["answer"] == "直接回答"
    assert result["iterations"] == 1
    assert result["tool_call_count"] == 0
    assert memory.midterm_retriever.calls == []
    assert memory.longterm_calls == []
    assert len(llm.calls) == 1


def test_one_tool_call_with_multiple_queries_uses_two_llm_calls():
    memory = _FakeMemory(
        {
            "最大可接受亏损比例": _default_results(),
            "投资期限": [_session(), _page("page-2", 0.75, summary="用户说明投资期限")],
        }
    )
    llm = _ScriptedLLM(
        [
            _tool_call("call-search", {"queries": ["最大可接受亏损比例", "投资期限"]}),
            {"content": "检索后的回答", "tool_calls": []},
        ]
    )

    result = AgenticMemoryRunner(llm, _executor(memory), AgenticRetrievalConfig()).run(
        [{"role": "user", "content": "我的风险约束是什么？"}]
    )

    assert result["answer"] == "检索后的回答"
    assert result["iterations"] == 2
    assert result["tool_call_count"] == 1
    assert len(llm.calls) == 2
    assert {call[0] for call in memory.midterm_retriever.calls} == {"最大可接受亏损比例", "投资期限"}
    assert all(call[1] == {"user_id": "user-1", "run_id": "run-1"} for call in memory.midterm_retriever.calls)
    assert all(call[2:] == (False, 20) for call in memory.midterm_retriever.calls)
    assert memory.longterm_calls == []
    assert memory.search_calls == []

    second_messages = llm.calls[1]["messages"]
    assert second_messages[-3]["tool_calls"][0]["id"] == "call-search"
    assert second_messages[-2]["tool_call_id"] == "call-search"
    assert json.loads(second_messages[-2]["content"])["ok"] is True
    assert second_messages[-1]["role"] == "system"
    assert "tools" not in llm.calls[1]
    assert "tool_choice" not in llm.calls[1]


@pytest.mark.parametrize("query_count", [1, 2, 3])
def test_tool_accepts_one_to_three_queries(query_count):
    queries = [f"query-{index}" for index in range(query_count)]
    memory = _FakeMemory({query: [] for query in queries})

    result = _executor(memory).execute("search_memory", {"queries": queries})

    assert result == {"ok": True, "items": []}
    assert len(memory.midterm_retriever.calls) == query_count


@pytest.mark.parametrize("arguments", [{"queries": []}, {"queries": ["1", "2", "3", "4"]}])
def test_tool_rejects_query_counts_outside_schema(arguments):
    result = _executor().execute("search_memory", arguments)

    assert result["error"] == "InvalidArguments"


def test_server_max_queries_can_be_stricter_than_tool_schema():
    result = _executor(max_queries=1).execute("search_memory", {"queries": ["one", "two"]})

    assert result["error"] == "InvalidArguments"
    assert result["fields"] == ["queries"]


def test_multi_query_pages_are_deduplicated_by_id_and_keep_highest_score():
    memory = _FakeMemory(
        {
            "风险": [_session("s1", "风险主题"), _page("shared", 0.55, session_id="s1"), _page("only-1", 0.7)],
            "亏损": [
                _session("s2", "亏损主题"),
                _page("shared", 0.91, session_id="s2", raw_dialogue="高分命中的完整对话"),
                _page("only-2", 0.8),
            ],
        }
    )

    result = _executor(memory).execute("search_memory", {"queries": ["风险", "亏损"]})

    assert [item["result_id"] for item in result["items"]] == [
        "mid_term_page:shared",
        "mid_term_page:only-2",
        "mid_term_page:only-1",
    ]
    assert result["items"][0]["score"] == 0.91
    assert result["items"][0]["content"] == "高分命中的完整对话"
    assert result["items"][0]["session_summary"] == "亏损主题"


def test_multi_query_records_each_hit_session_once_after_retrieval():
    queries = ["风险偏好", "最大亏损", "投资限制"]
    memory = _FakeMemory({query: _default_results() for query in queries})

    result = _executor(memory, record_midterm_visits=True).execute(
        "search_memory",
        {"queries": queries},
    )

    assert result["ok"] is True
    assert memory.midterm_memory.visits == ["session-1"]
    assert all(call[2] is False for call in memory.midterm_retriever.calls)


def test_record_midterm_visits_false_never_updates_session_statistics():
    queries = ["风险偏好", "最大亏损", "投资限制"]
    memory = _FakeMemory({query: _default_results() for query in queries})

    result = _executor(memory, record_midterm_visits=False).execute(
        "search_memory",
        {"queries": queries},
    )

    assert result["ok"] is True
    assert memory.midterm_memory.visits == []
    assert all(call[2] is False for call in memory.midterm_retriever.calls)


def test_session_visit_failure_does_not_fail_retrieval():
    memory = _FakeMemory(
        {"风险偏好": _default_results()},
        visit_error=RuntimeError("visit update failed"),
    )

    result = _executor(memory, record_midterm_visits=True).execute(
        "search_memory",
        {"queries": ["风险偏好"]},
    )

    assert result["ok"] is True
    assert [item["result_id"] for item in result["items"]] == ["mid_term_page:page-1"]
    assert memory.midterm_memory.visits == ["session-1"]


def test_only_midterm_pages_are_returned_with_complete_content():
    memory = _FakeMemory({"风险偏好": _default_results()})

    result = _executor(memory).execute("search_memory", {"queries": ["风险偏好"]})

    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item == {
        "result_id": "mid_term_page:page-1",
        "score": 0.82,
        "created_at": "2026-07-30T10:00:00+08:00",
        "session_summary": "用户讨论过投资组合风险控制",
        "summary": "用户明确说明了最大可接受亏损比例",
        "content": "用户表示最多能够接受本金亏损10%，投资期限不少于三年。",
    }
    assert all(not item["result_id"].startswith("mid_term_session:") for item in result["items"])


def test_content_falls_back_to_summary_then_memory():
    summary_page = _page("summary", 0.8, raw_dialogue="")
    memory_page = _page("memory", 0.7, raw_dialogue="", summary="")
    memory_page["memory"] = "仅有 memory 字段"
    memory = _FakeMemory({"fallback": [_session(), summary_page, memory_page]})

    result = _executor(memory).execute("search_memory", {"queries": ["fallback"]})

    assert [item["content"] for item in result["items"]] == [
        "用户明确说明了最大可接受亏损比例",
        "仅有 memory 字段",
    ]
    assert all("summary" not in item for item in result["items"])


def test_complete_page_text_is_not_truncated():
    session_summary = "S" * 1300
    summary = "M" * 1500
    raw_dialogue = "C" * 2400
    memory = _FakeMemory(
        {
            "完整内容": [
                _session(summary=session_summary),
                _page("full-page", 0.9, summary=summary, raw_dialogue=raw_dialogue),
            ]
        }
    )

    result = _executor(memory).execute("search_memory", {"queries": ["完整内容"]})
    item = result["items"][0]

    assert item["session_summary"] == session_summary
    assert item["summary"] == summary
    assert item["content"] == raw_dialogue
    assert len(item["content"]) == 2400


def test_configurable_max_total_results_limits_complete_pages():
    pages = [_page(f"page-{index}", 0.9 - index * 0.1) for index in range(5)]
    memory = _FakeMemory({"top-k": [_session(), *pages]})

    result = _executor(memory, max_total_results=2).execute(
        "search_memory",
        {"queries": ["top-k"]},
    )

    assert [item["result_id"] for item in result["items"]] == [
        "mid_term_page:page-0",
        "mid_term_page:page-1",
    ]
    assert len(result["items"]) == 2


def test_global_tool_limit_drops_whole_low_score_pages_without_truncation():
    high_content = "H" * 650
    low_content = "L" * 650
    memory = _FakeMemory(
        {
            "large": [
                _session(summary="session"),
                _page("high", 0.9, summary="high summary", raw_dialogue=high_content),
                _page("low", 0.5, summary="low summary", raw_dialogue=low_content),
            ]
        }
    )

    result = _executor(memory, max_total_results=2, max_tool_result_chars=1000).execute(
        "search_memory",
        {"queries": ["large"]},
    )
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    assert result["ok"] is True
    assert [item["result_id"] for item in result["items"]] == ["mid_term_page:high"]
    assert result["items"][0]["content"] == high_content
    assert len(serialized) <= 1000


def test_single_complete_page_over_global_limit_returns_error_without_truncation():
    raw_dialogue = "C" * 2000
    memory = _FakeMemory(
        {
            "oversized": [
                _session(summary="session"),
                _page("oversized", 0.9, summary="summary", raw_dialogue=raw_dialogue),
            ]
        }
    )

    result = _executor(memory, max_tool_result_chars=1000).execute(
        "search_memory",
        {"queries": ["oversized"]},
    )

    assert result == {
        "ok": False,
        "error": "ToolResultTooLarge",
        "message": "单条完整中期记忆超过工具消息长度限制",
    }
    assert not hasattr(_executor(memory), "_shrink_text_field")


def test_scope_and_retrieval_controls_cannot_be_supplied_by_model():
    memory = _FakeMemory({"valid": []})
    executor = _executor(memory)
    forbidden = {
        "user_id": "other-user",
        "run_id": "other-run",
        "session_id": "other-session",
        "filters": {"user_id": "other-user"},
        "top_k": 100,
        "threshold": 0,
        "cursor": "forged",
        "layers": ["long_term"],
        "page_size": 20,
    }

    for field, value in forbidden.items():
        result = executor.execute("search_memory", {"queries": ["valid"], field: value})
        assert result["error"] == "InvalidArguments"

    valid = executor.execute("search_memory", {"queries": ["valid"]})
    assert valid["ok"] is True
    assert memory.midterm_retriever.calls == [("valid", {"user_id": "user-1", "run_id": "run-1"}, False, 20)]


def test_no_cursor_pagination_or_result_reading_state_exists():
    executor = _executor()

    assert not hasattr(executor, "_cursors")
    assert not hasattr(executor, "_search_states")
    assert not hasattr(executor, "_result_items")
    assert executor.execute("read_memory_result", {"result_id": "page-1"})["error"] == "UnknownTool"
    assert executor.execute("search_memory", {"queries": ["风险偏好"], "cursor": "x"})["error"] == "InvalidArguments"


def test_multiple_tool_calls_execute_only_first_and_return_limit_for_others():
    memory = _FakeMemory({"first": _default_results(), "second": _default_results()})
    first_response = {
        "content": None,
        "tool_calls": [
            {"id": "call-1", "name": "search_memory", "arguments": {"queries": ["first"]}},
            {"id": "call-2", "name": "search_memory", "arguments": {"queries": ["second"]}},
        ],
    }
    llm = _ScriptedLLM([first_response, {"content": "最终回答", "tool_calls": []}])

    result = AgenticMemoryRunner(llm, _executor(memory), AgenticRetrievalConfig()).run([])
    tool_messages = [message for message in llm.calls[1]["messages"] if message["role"] == "tool"]

    assert [call[0] for call in memory.midterm_retriever.calls] == ["first"]
    assert len(tool_messages) == 2
    assert json.loads(tool_messages[0]["content"])["ok"] is True
    assert json.loads(tool_messages[1]["content"])["error"] == "ToolCallLimitExceeded"
    assert result["answer"] == "最终回答"
    assert result["tool_call_count"] == 1
    assert result["stop_reason"] == "max_tool_calls"
    assert len(result["tool_trace"]) == 2
    assert len(llm.calls) == 2


def test_second_model_call_cannot_execute_another_tool():
    memory = _FakeMemory({"first": _default_results(), "second": _default_results()})
    llm = _ScriptedLLM(
        [
            _tool_call("call-1", {"queries": ["first"]}),
            _tool_call("call-2", {"queries": ["second"]}),
        ]
    )

    result = AgenticMemoryRunner(llm, _executor(memory), AgenticRetrievalConfig()).run([])

    assert [call[0] for call in memory.midterm_retriever.calls] == ["first"]
    assert len(llm.calls) == 2
    assert "tools" not in llm.calls[1]
    assert result["stop_reason"] == "model_tool_call_blocked"
    assert result["answer"] == "根据当前对话和已检索到的记忆，仍无法确定足够可靠的答案。"


def test_retrieval_exception_becomes_tool_result_and_second_call_can_answer():
    memory = _FakeMemory({"timeout": TimeoutError("embedding timeout")})
    llm = _ScriptedLLM(
        [
            _tool_call("call-1", {"queries": ["timeout"]}),
            {"content": "根据现有信息给出有边界的回答", "tool_calls": []},
        ]
    )

    result = AgenticMemoryRunner(llm, _executor(memory), AgenticRetrievalConfig()).run([])
    tool_payload = json.loads(llm.calls[1]["messages"][-2]["content"])

    assert tool_payload["ok"] is False
    assert tool_payload["error"] == "RetrievalUnavailable"
    assert tool_payload["errors"][0]["error"] == "TimeoutError"
    assert "embedding timeout" not in tool_payload["errors"][0]["message"]
    assert result["answer"] == "根据现有信息给出有边界的回答"
    assert len(llm.calls) == 2


def test_malformed_arguments_still_lead_to_bounded_final_answer():
    llm = _ScriptedLLM(
        [
            {
                "content": None,
                "tool_calls": [{"id": "bad", "name": "search_memory", "arguments": "{not-json"}],
            },
            {"content": "参数错误后的回答", "tool_calls": []},
        ]
    )

    result = AgenticMemoryRunner(llm, _executor(), AgenticRetrievalConfig()).run([])
    payload = json.loads(llm.calls[1]["messages"][-2]["content"])

    assert payload["error"] == "InvalidArguments"
    assert result["answer"] == "参数错误后的回答"
    assert len(llm.calls) == 2


def test_base_context_does_not_search_and_disabled_public_runner_rejects_use():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        midterm=SimpleNamespace(short_term_capacity=4),
        agentic_retrieval=AgenticRetrievalConfig(enabled=False),
    )
    memory.get_profile = MagicMock(return_value={"profile": {"risk_level": "balanced"}})
    memory.db = SimpleNamespace(get_last_messages=MagicMock(return_value=[]))
    memory.search = MagicMock(side_effect=AssertionError("base context must not search"))

    context = memory._retrieve_base_context(" question ", user_id=" user-1 ", session_id=" run-1 ")

    assert context["retrieved_memories"] == []
    memory.search.assert_not_called()
    with pytest.raises(ValueError, match="disabled"):
        memory.run_agentic_retrieval("question", user_id="user-1", session_id="run-1")


def test_enabled_public_memory_runner_uses_base_context_and_returns_trace_shape():
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(agentic_retrieval=AgenticRetrievalConfig(enabled=True))
    memory.llm = _ScriptedLLM([{"content": "public answer", "tool_calls": []}])
    memory._retrieve_base_context = MagicMock(
        return_value={
            "query": "question",
            "user_id": "user-1",
            "session_id": "run-1",
            "profile": {},
            "short_term_messages": [],
            "retrieved_memories": [],
        }
    )

    result = memory.run_agentic_retrieval("question", user_id="user-1", session_id="run-1")

    assert result == {
        "answer": "public answer",
        "iterations": 1,
        "tool_call_count": 0,
        "stop_reason": "model_answered",
        "tool_trace": [],
    }
    memory._retrieve_base_context.assert_called_once_with(
        "question",
        user_id="user-1",
        session_id="run-1",
        include_profile_metadata=False,
    )


def test_sync_three_queries_run_in_parallel():
    queries = ["query-1", "query-2", "query-3"]
    memory = _FakeMemory(
        {query: [] for query in queries},
        delays_by_query={query: 0.2 for query in queries},
    )

    started_at = time.perf_counter()
    result = _executor(memory).execute("search_memory", {"queries": queries})
    elapsed = time.perf_counter() - started_at

    assert result == {"ok": True, "items": []}
    assert memory.midterm_retriever.max_active_searches == 3
    assert elapsed < 0.45


@pytest.mark.asyncio
async def test_async_three_queries_run_in_parallel_without_blocking_event_loop():
    queries = ["query-1", "query-2", "query-3"]
    memory = _FakeMemory(
        {query: [] for query in queries},
        delays_by_query={query: 0.2 for query in queries},
    )
    executor = AsyncMemoryToolExecutor(
        memory,
        user_id="user-1",
        run_id="run-1",
        config=AgenticRetrievalConfig(),
        record_midterm_visits=False,
    )
    loop_ticks = 0

    async def count_loop_ticks():
        nonlocal loop_ticks
        for _ in range(5):
            await asyncio.sleep(0.02)
            loop_ticks += 1

    started_at = time.perf_counter()
    result, _ = await asyncio.gather(
        executor.execute("search_memory", {"queries": queries}),
        count_loop_ticks(),
    )
    elapsed = time.perf_counter() - started_at

    assert result == {"ok": True, "items": []}
    assert memory.midterm_retriever.max_active_searches == 3
    assert loop_ticks == 5
    assert elapsed < 0.45


@pytest.mark.asyncio
async def test_sync_and_async_multi_query_results_and_visits_match():
    queries = ["风险", "亏损", "限制"]
    results_by_query = {
        "风险": [_session(), _page("shared", 0.5), _page("risk-only", 0.7)],
        "亏损": [_session(), _page("shared", 0.9)],
        "限制": [_session(), _page("limit-only", 0.8)],
    }
    sync_memory = _FakeMemory(results_by_query)
    async_memory = _FakeMemory(results_by_query)
    config = AgenticRetrievalConfig()
    sync_executor = MemoryToolExecutor(
        sync_memory,
        user_id="user-1",
        run_id="run-1",
        config=config,
        record_midterm_visits=True,
    )
    async_executor = AsyncMemoryToolExecutor(
        async_memory,
        user_id="user-1",
        run_id="run-1",
        config=config,
        record_midterm_visits=True,
    )

    sync_result = sync_executor.execute("search_memory", {"queries": queries})
    async_result = await async_executor.execute("search_memory", {"queries": queries})

    assert async_result == sync_result
    assert sync_memory.midterm_memory.visits == ["session-1"]
    assert async_memory.midterm_memory.visits == ["session-1"]
    assert all(call[2] is False for call in async_memory.midterm_retriever.calls)


@pytest.mark.asyncio
async def test_async_query_failure_preserves_other_query_results():
    memory = _FakeMemory(
        {
            "失败": TimeoutError("embedding timeout"),
            "成功": [_session(), _page("success", 0.9)],
        }
    )
    executor = AsyncMemoryToolExecutor(
        memory,
        user_id="user-1",
        run_id="run-1",
        config=AgenticRetrievalConfig(),
        record_midterm_visits=False,
    )

    result = await executor.execute("search_memory", {"queries": ["失败", "成功"]})

    assert result["ok"] is True
    assert [item["result_id"] for item in result["items"]] == ["mid_term_page:success"]
    assert result["errors"] == [{"error": "TimeoutError", "message": "记忆检索暂时不可用"}]


@pytest.mark.asyncio
async def test_async_runner_matches_sync_bounds_and_only_searches_midterm():
    memory = _FakeMemory(
        {
            "风险": [_session(), _page("shared", 0.5)],
            "亏损": [_session(), _page("shared", 0.9)],
        }
    )
    config = AgenticRetrievalConfig(enabled=True)
    executor = AsyncMemoryToolExecutor(
        memory,
        user_id="user-1",
        run_id="run-1",
        config=config,
        record_midterm_visits=False,
    )
    llm = _ScriptedLLM(
        [
            _tool_call("call-1", {"queries": ["风险", "亏损"]}),
            {"content": "async answer", "tool_calls": []},
        ]
    )

    result = await AsyncAgenticMemoryRunner(llm, executor, config).run([])

    assert result["answer"] == "async answer"
    assert result["iterations"] == 2
    assert result["tool_call_count"] == 1
    assert len(llm.calls) == 2
    assert "tools" not in llm.calls[1]
    assert {call[0] for call in memory.midterm_retriever.calls} == {"风险", "亏损"}
    assert memory.longterm_calls == []
    tool_payload = json.loads(llm.calls[1]["messages"][-2]["content"])
    assert tool_payload["items"][0]["score"] == 0.9
