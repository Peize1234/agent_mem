from types import SimpleNamespace

from exp.benchmark.run_midterm_add_local_context_ablation import (
    PREVIOUS_CONTEXT_INSTRUCTIONS,
    PREVIOUS_FOLLOWING_CONTEXT_INSTRUCTIONS,
    add_context_instructions,
    context_layout,
    format_context_input,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT


def test_context_prompts_only_insert_the_frozen_usage_instructions():
    previous = add_context_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, PREVIOUS_CONTEXT_INSTRUCTIONS)
    both = add_context_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, PREVIOUS_FOLLOWING_CONTEXT_INSTRUCTIONS)

    assert previous.replace(f"{PREVIOUS_CONTEXT_INSTRUCTIONS}\n\n", "") == MIDTERM_PAGE_SUMMARY_PROMPT
    assert both.replace(f"{PREVIOUS_FOLLOWING_CONTEXT_INSTRUCTIONS}\n\n", "") == MIDTERM_PAGE_SUMMARY_PROMPT


def test_context_layout_matches_the_real_three_turn_eviction_window():
    turns = [
        SimpleNamespace(turn_id=f"S000-Q{index:03d}", turn_index=index, question=f"u{index}", answer=f"a{index}")
        for index in range(8)
    ]
    session = SimpleNamespace(turns=turns)
    page = {
        "source_turn_id": "S000-Q004",
        "source_turn_index": 4,
        "source_job_trigger_turn_id": "S000-Q007",
        "user_input": "current user",
        "assistant_response": "current assistant",
    }
    previous_pages = [
        {
            "source_turn_id": f"S000-Q{index:03d}",
            "source_turn_index": index,
            "summary": f"summary {index}",
            "keywords": [f"keyword {index}"],
        }
        for index in range(4)
    ]

    layout = context_layout(page, session, previous_pages, include_following=True)

    assert [item["source_turn_id"] for item in layout["previous_context"]] == [
        "S000-Q001",
        "S000-Q002",
        "S000-Q003",
    ]
    assert [item["turn_id"] for item in layout["following_context"]] == [
        "S000-Q005",
        "S000-Q006",
        "S000-Q007",
    ]
    assert layout["eviction_trigger_turn_id"] == "S000-Q007"

    payload = format_context_input(page, layout, include_following=True)
    assert "summary 0" not in payload
    assert "summary 1" in payload
    assert "用户：current user" in payload
    assert "用户：u5" in payload
    assert "用户：u7" in payload


def test_previous_only_layout_contains_no_following_turns():
    turns = [
        SimpleNamespace(turn_id=f"S000-Q{index:03d}", turn_index=index, question=f"u{index}", answer=f"a{index}")
        for index in range(4)
    ]
    session = SimpleNamespace(turns=turns)
    page = {
        "source_turn_id": "S000-Q000",
        "source_turn_index": 0,
        "source_job_trigger_turn_id": "S000-Q003",
        "user_input": "current user",
        "assistant_response": "current assistant",
    }

    layout = context_layout(page, session, [], include_following=False)
    payload = format_context_input(page, layout, include_following=False)

    assert layout["previous_context"] == []
    assert layout["following_context"] == []
    assert "上文：\n\n（无）" in payload
    assert "下文：" not in payload
