from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT
from mem0.memory.midterm_updater import MidTermUpdater
from mem0.memory.storage import SQLiteManager


def _qa(turn_index: int) -> list[dict]:
    return [
        {"role": "user", "content": f"Q{turn_index}", "turn_index": turn_index},
        {"role": "assistant", "content": f"A{turn_index}", "turn_index": turn_index},
    ]


class _PageStore:
    def __init__(self):
        self.pages = {
            "p1": SimpleNamespace(
                id="p1",
                payload={"raw_dialogue": "User: P1Q\n\nAssistant: P1A", "pre_page": None, "page_sequence": 1},
            ),
            "p2": SimpleNamespace(
                id="p2",
                payload={"raw_dialogue": "User: P2Q\n\nAssistant: P2A", "pre_page": "p1", "page_sequence": 2},
            ),
        }

    def list_pages(self, **_):
        return list(self.pages.values())

    def get_page(self, page_id):
        return self.pages.get(page_id)

    def reserve_page_sequences(self, _filters, count):
        return list(range(3, 3 + count))

    def insert_page(self, page_id, payload):
        self.pages[page_id] = SimpleNamespace(id=page_id, payload=dict(payload))

    def current_turn_index(self, _filters):
        return 6


def _updater(*, async_llm: bool = False):
    store = _PageStore()
    llm = MagicMock()
    response = '{"summary":"Q1 summary","keywords":["Q1"]}'
    if async_llm:
        llm.generate_response_async = AsyncMock(return_value=response)
    else:
        llm.generate_response.return_value = response
    calls = []

    def following(filters, after_turn_index, qa_limit):
        calls.append((filters, after_turn_index, qa_limit))
        return [message for turn in range(after_turn_index + 1, after_turn_index + qa_limit + 1) for message in _qa(turn)]

    updater = MidTermUpdater(
        store,
        llm,
        SimpleNamespace(short_term_capacity=6),
        following_qa_provider=following,
    )
    return updater, store, llm, calls


def test_page_summary_uses_previous_chain_and_nearest_following_but_persists_only_current_qa():
    updater, _, llm, calls = _updater()

    pages = updater.process_evicted_messages(
        _qa(1),
        {"user_id": "u1", "run_id": "r1"},
        source_job_id="migration-1",
        lease_token="lease-1",
        lease_is_current=lambda: True,
    )

    prompt_input = llm.generate_response.call_args.kwargs["messages"][1]["content"]
    assert prompt_input.index("P1Q") < prompt_input.index("P2Q") < prompt_input.index("Q1")
    assert prompt_input.index("Q1") < prompt_input.index("Q2") < prompt_input.index("Q3") < prompt_input.index("Q4")
    assert calls == [({"user_id": "u1", "run_id": "r1"}, 1, 3)]
    assert pages[0]["raw_dialogue"] == "User: Q1\n\nAssistant: A1"
    assert pages[0]["user_input"] == "Q1"
    assert pages[0]["assistant_response"] == "A1"
    assert "P1Q" not in pages[0]["raw_dialogue"]
    assert "Q2" not in pages[0]["raw_dialogue"]


def test_following_context_is_based_on_source_turn_even_when_worker_runs_after_q6():
    db = SQLiteManager(":memory:")
    scope = "run_id=r1&user_id=u1"
    messages = [
        {"role": role, "content": content}
        for turn in range(1, 7)
        for role, content in (("user", f"Q{turn}"), ("assistant", f"A{turn}"))
    ]
    try:
        db.save_messages(messages, scope, max_messages=100)
        db.connection.execute("UPDATE messages SET status = 'pending' WHERE turn_index = 2")
        db.connection.execute("UPDATE messages SET status = 'processing' WHERE turn_index = 3")
        db.connection.commit()

        q1 = db.get_following_qa_messages(scope, after_turn_index=1, qa_limit=3)
        q2 = db.get_following_qa_messages(scope, after_turn_index=2, qa_limit=3)

        assert [row["content"] for row in q1] == ["Q2", "A2", "Q3", "A3", "Q4", "A4"]
        assert [row["content"] for row in q2] == ["Q3", "A3", "Q4", "A4", "Q5", "A5"]
        assert {row["status"] for row in q1} >= {"pending", "processing"}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_midterm_context_contract_sync_async_parity():
    sync_updater, _, sync_llm, _ = _updater()
    async_updater, _, async_llm, _ = _updater(async_llm=True)

    sync_pages = sync_updater.process_evicted_messages(
        _qa(1),
        {"user_id": "u1", "run_id": "r1"},
        source_job_id="same-job",
        lease_token="lease",
        lease_is_current=lambda: True,
    )
    async_pages = await async_updater.process_evicted_messages_async(
        _qa(1),
        {"user_id": "u1", "run_id": "r1"},
        source_job_id="same-job",
        lease_token="lease",
        lease_is_current=lambda: True,
    )

    assert async_pages[0]["raw_dialogue"] == sync_pages[0]["raw_dialogue"]
    assert async_pages[0]["summary"] == sync_pages[0]["summary"]
    assert async_llm.generate_response_async.await_args.kwargs["messages"][1]["content"] == (
        sync_llm.generate_response.call_args.kwargs["messages"][1]["content"]
    )


def test_production_prompt_forbids_following_fact_contamination():
    assert "禁止把下文中新出现的数字、指标、证据、任务或结论写成当前轮已经包含的内容" in MIDTERM_PAGE_SUMMARY_PROMPT
    assert "不要把多轮对话整理成综合总结" in MIDTERM_PAGE_SUMMARY_PROMPT
