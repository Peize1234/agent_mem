import logging
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.base import AgenticRetrievalConfig
from mem0.memory.main import AsyncMemory, Memory
from mem0.memory.retrieval_tools import MEMORY_TOOLS, MemoryToolExecutor


def _tool_call(arguments):
    return {
        "content": None,
        "tool_calls": [{"id": "call-search", "name": "search_memory", "arguments": arguments}],
    }


class _ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self._lock = threading.Lock()

    def generate_response(self, **kwargs):
        with self._lock:
            self.calls.append(deepcopy(kwargs))
            response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _MidtermRetriever:
    def __init__(self, result=None):
        self.result = result if result is not None else []
        self.calls = []

    def search(self, query, filters, *, record_visits=True, candidate_pool_size=None):
        self.calls.append((query, deepcopy(filters), record_visits, candidate_pool_size))
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


class _MidtermMemory:
    def __init__(self):
        self.visits = []

    def record_session_visit(self, session_id):
        self.visits.append(session_id)

    def record_valid_recalls(self, page_ids):
        self.visits.extend(page_ids)
        return []


def _retrieved_context():
    return {
        "user_id": "user-1",
        "session_id": "run-1",
        "query": "What risk limit did I choose?",
        "profile": {"risk_level": "balanced"},
        "short_term_messages": [
            {
                "role": "user",
                "content": "Use my existing plan.",
                "created_at": "2026-07-31T09:00:00+08:00",
            }
        ],
        "retrieved_memories": [
            {
                "id": "mid-ordinary",
                "source": "mid_term_page",
                "score": 0.82,
                "created_at": "2026-07-29T10:00:00+08:00",
                "raw_dialogue": "ordinary mid-term context",
            },
            {
                "id": "long-ordinary",
                "source": "long_term",
                "score": 0.74,
                "created_at": "2026-07-28T10:00:00+08:00",
                "memory": "ordinary long-term context",
            },
        ],
    }


def _supplemental_result():
    return [
        {
            "id": "session-1",
            "session_id": "session-1",
            "source": "mid_term_session",
            "summary": "risk discussion",
            "memory": "risk discussion",
            "score": 0.95,
        },
        {
            "id": "page-1",
            "session_id": "session-1",
            "source": "mid_term_page",
            "summary": "chosen loss limit",
            "raw_dialogue": "The user chose a 10% maximum loss.",
            "memory": "chosen loss limit",
            "score": 0.9,
            "created_at": "2026-07-30T10:00:00+08:00",
        },
    ]


def _build_sync_memory(llm, *, config=None, retriever_result=None):
    memory = Memory.__new__(Memory)
    memory.config = SimpleNamespace(
        agentic_retrieval=config or AgenticRetrievalConfig(enabled=True),
        midterm=SimpleNamespace(promotion_min_recall_count=3, promotion_heat_threshold=5.0),
    )
    memory.llm = llm
    memory._retrieve_context = MagicMock(return_value=_retrieved_context())
    memory._midterm_enabled = MagicMock(return_value=True)
    memory._midterm_retriever = _MidtermRetriever(retriever_result)
    memory._midterm_memory = _MidtermMemory()
    return memory


def _build_async_memory(llm, *, config=None, retriever_result=None):
    memory = AsyncMemory.__new__(AsyncMemory)
    memory.config = SimpleNamespace(
        agentic_retrieval=config or AgenticRetrievalConfig(enabled=True),
        midterm=SimpleNamespace(promotion_min_recall_count=3, promotion_heat_threshold=5.0),
    )
    memory.llm = llm
    memory._retrieve_context = AsyncMock(return_value=_retrieved_context())
    memory._midterm_enabled = MagicMock(return_value=True)
    memory._midterm_retriever = _MidtermRetriever(retriever_result)
    memory._midterm_memory = _MidtermMemory()
    return memory


def _assert_empty_agentic_supplement(messages):
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    assert "<agentic_memory_supplement>\n\n</agentic_memory_supplement>" in messages[0]["content"]
    assert "ordinary mid-term context" in messages[0]["content"]
    assert "ordinary long-term context" in messages[0]["content"]


def test_disabled_agentic_retrieval_keeps_original_message_flow():
    llm = MagicMock()
    memory = _build_sync_memory(llm, config=AgenticRetrievalConfig(enabled=False))
    memory._run_agentic_retrieval_from_context = MagicMock(
        side_effect=AssertionError("disabled Agentic retrieval must not run")
    )

    messages = memory.build_agent_answer_messages(
        "question",
        user_id="user-1",
        session_id="run-1",
        reference_information={"source": "reference detail"},
    )

    _assert_empty_agentic_supplement(messages)
    assert "reference detail" in messages[0]["content"]
    memory._retrieve_context.assert_called_once()
    memory._run_agentic_retrieval_from_context.assert_not_called()
    llm.generate_response.assert_not_called()
    assert memory._midterm_retriever.calls == []


def test_enabled_agentic_retrieval_ignores_accidental_answer_without_tool_call():
    accidental_answer = "华辰智能装备的业务结构以机器人控制器为主、智能仓储设备为辅。"
    llm = _ScriptedLLM([{"content": accidental_answer, "tool_calls": []}])
    memory = _build_sync_memory(llm)
    agentic_generation_kwargs = {
        "temperature": 0.2,
        "metadata": {"request_id": "agentic-only"},
        "messages": [{"role": "user", "content": "must be ignored"}],
        "tools": [{"type": "invalid"}],
        "tool_choice": "none",
    }
    original_generation_kwargs = deepcopy(agentic_generation_kwargs)

    messages = memory.build_agent_answer_messages(
        "question",
        user_id="user-1",
        session_id="run-1",
        reference_information={"source": "reference detail"},
        agentic_generation_kwargs=agentic_generation_kwargs,
    )

    memory._retrieve_context.assert_called_once()
    assert len(llm.calls) == 1
    assert memory._midterm_retriever.calls == []
    assert agentic_generation_kwargs == original_generation_kwargs
    assert llm.calls[0]["temperature"] == 0.2
    assert llm.calls[0]["metadata"] == {"request_id": "agentic-only"}
    assert llm.calls[0]["tools"] == MEMORY_TOOLS
    assert llm.calls[0]["tool_choice"] == "auto"
    assert llm.calls[0]["messages"] != agentic_generation_kwargs["messages"]
    assert len(llm.calls[0]["messages"]) == 1
    agentic_prompt = llm.calls[0]["messages"][0]["content"]
    for expected in (
        "What risk limit did I choose?",
        "Use my existing plan.",
        "ordinary mid-term context",
        "ordinary long-term context",
        "balanced",
        "reference detail",
    ):
        assert expected in agentic_prompt
    assert len(messages) == 1
    assert messages[0]["role"] == "system"
    _assert_empty_agentic_supplement(messages)
    assert accidental_answer not in messages[0]["content"]


def test_sufficient_huachen_context_reaches_only_the_final_answer_prompt():
    query = (
        "我们正在评估是否向“华辰智能装备有限公司”提供一笔三年期授信。公司主要生产工业机器人控制器和"
        "智能仓储设备，2025年收入约60%来自机器人控制器，40%来自智能仓储设备。请先根据这些信息概括"
        "公司的业务结构。"
    )
    accidental_answer = "机器人控制器是核心业务，智能仓储设备是第二业务板块。"
    llm = _ScriptedLLM([{"content": accidental_answer, "tool_calls": []}])
    memory = _build_sync_memory(llm)
    context = _retrieved_context()
    context.update(query=query, short_term_messages=[], retrieved_memories=[])
    memory._retrieve_context.return_value = context

    messages = memory.build_agent_answer_messages(query, user_id="user-1", session_id="run-1")

    assert len(llm.calls) == 1
    assert len(llm.calls[0]["messages"]) == 1
    assert memory._midterm_retriever.calls == []
    assert "<agentic_memory_supplement>\n\n</agentic_memory_supplement>" in messages[0]["content"]
    assert "华辰智能装备有限公司" in messages[0]["content"]
    assert "60%" in messages[0]["content"] and "40%" in messages[0]["content"]
    assert accidental_answer not in messages[0]["content"]


def test_enabled_agentic_retrieval_supplements_context_without_repeating_complete_retrieval():
    llm = _ScriptedLLM(
        [
            _tool_call({"queries": ["maximum loss"]}),
            {
                "content": "Historical policy: the 10% maximum-loss limit applies to the existing plan.",
                "tool_calls": [],
            },
        ]
    )
    memory = _build_sync_memory(llm, retriever_result=_supplemental_result())

    messages = memory.build_agent_answer_messages("question", user_id="user-1", session_id="run-1")

    memory._retrieve_context.assert_called_once()
    assert len(llm.calls) == 2
    assert set(llm.calls[0]) == {"messages", "tools", "tool_choice"}
    assert memory._midterm_retriever.calls == [("maximum loss", {"user_id": "user-1", "run_id": "run-1"}, False, 20)]
    assert "ordinary mid-term context" in llm.calls[1]["messages"][0]["content"]
    assert "The user chose a 10% maximum loss." in llm.calls[1]["messages"][-2]["content"]
    assert (
        "<agentic_memory_supplement>\nHistorical policy: the 10% maximum-loss limit applies to the existing plan.\n"
        "</agentic_memory_supplement>"
    ) in messages[0]["content"]
    assert all(message["role"] != "assistant" for message in messages)


@pytest.mark.parametrize(
    "scenario",
    ["model_error", "tool_error", "invalid_arguments", "empty_supplement", "max_iterations"],
)
def test_agentic_failures_degrade_to_normal_answer_prompt(scenario, caplog):
    config = AgenticRetrievalConfig(enabled=True)
    retriever_result = _supplemental_result()
    if scenario == "model_error":
        responses = [RuntimeError("model unavailable")]
    elif scenario == "tool_error":
        responses = [_tool_call({"queries": ["maximum loss"]}), {"content": "candidate", "tool_calls": []}]
        retriever_result = RuntimeError("tool unavailable")
    elif scenario == "invalid_arguments":
        responses = [_tool_call("{not-json"), {"content": "candidate", "tool_calls": []}]
    elif scenario == "empty_supplement":
        responses = [_tool_call({"queries": ["maximum loss"]}), {"content": "", "tool_calls": []}]
    else:
        config = AgenticRetrievalConfig(enabled=True, max_iterations=1)
        responses = [_tool_call({"queries": ["maximum loss"]})]

    memory = _build_sync_memory(_ScriptedLLM(responses), config=config, retriever_result=retriever_result)

    with caplog.at_level(logging.WARNING):
        messages = memory.build_agent_answer_messages("question", user_id="user-1", session_id="run-1")

    _assert_empty_agentic_supplement(messages)
    memory._retrieve_context.assert_called_once()


def test_invalid_tool_result_degrades_to_normal_answer_prompt(monkeypatch, caplog):
    llm = _ScriptedLLM(
        [
            _tool_call({"queries": ["maximum loss"]}),
            {"content": "candidate", "tool_calls": []},
        ]
    )
    memory = _build_sync_memory(llm)
    monkeypatch.setattr(MemoryToolExecutor, "execute", lambda *_args, **_kwargs: ["invalid result"])

    with caplog.at_level(logging.WARNING):
        messages = memory.build_agent_answer_messages("question", user_id="user-1", session_id="run-1")

    _assert_empty_agentic_supplement(messages)
    memory._retrieve_context.assert_called_once()


@pytest.mark.asyncio
async def test_async_agentic_retrieval_matches_sync_context_and_supplement_injection():
    llm = _ScriptedLLM(
        [
            _tool_call({"queries": ["maximum loss"]}),
            {"content": "Async historical supplement.", "tool_calls": []},
        ]
    )
    memory = _build_async_memory(llm, retriever_result=_supplemental_result())
    agentic_generation_kwargs = {
        "temperature": 0.3,
        "metadata": {"request_id": "async-agentic-only"},
        "messages": [{"role": "user", "content": "must be ignored"}],
        "tools": [{"type": "invalid"}],
        "tool_choice": "none",
    }
    original_generation_kwargs = deepcopy(agentic_generation_kwargs)

    messages = await memory.build_agent_answer_messages(
        "question",
        user_id="user-1",
        session_id="run-1",
        agentic_generation_kwargs=agentic_generation_kwargs,
    )

    memory._retrieve_context.assert_awaited_once()
    assert len(llm.calls) == 2
    assert agentic_generation_kwargs == original_generation_kwargs
    assert llm.calls[0]["temperature"] == 0.3
    assert llm.calls[0]["metadata"] == {"request_id": "async-agentic-only"}
    assert llm.calls[0]["tools"] == MEMORY_TOOLS
    assert llm.calls[0]["tool_choice"] == "auto"
    assert llm.calls[0]["messages"] != agentic_generation_kwargs["messages"]
    assert llm.calls[1]["temperature"] == 0.3
    assert llm.calls[1]["metadata"] == {"request_id": "async-agentic-only"}
    assert "tools" not in llm.calls[1]
    assert "tool_choice" not in llm.calls[1]
    assert "ordinary mid-term context" in llm.calls[0]["messages"][0]["content"]
    assert memory._midterm_retriever.calls == [("maximum loss", {"user_id": "user-1", "run_id": "run-1"}, False, 20)]
    assert (
        "<agentic_memory_supplement>\nAsync historical supplement.\n</agentic_memory_supplement>"
        in messages[0]["content"]
    )
    assert all(message["role"] != "assistant" for message in messages)


@pytest.mark.asyncio
async def test_async_agentic_model_failure_degrades_to_normal_answer_prompt(caplog):
    llm = _ScriptedLLM([RuntimeError("model unavailable")])
    memory = _build_async_memory(llm)

    with caplog.at_level(logging.WARNING):
        messages = await memory.build_agent_answer_messages("question", user_id="user-1", session_id="run-1")

    _assert_empty_agentic_supplement(messages)
    memory._retrieve_context.assert_awaited_once()
    assert set(llm.calls[0]) == {"messages", "tools", "tool_choice"}
    assert caplog.records
