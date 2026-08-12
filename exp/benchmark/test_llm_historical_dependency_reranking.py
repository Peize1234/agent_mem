import copy

import pytest

from exp.benchmark.run_llm_historical_dependency_reranking import (
    SYSTEM_PROMPT,
    candidate_payload,
    rerank_top_candidates,
    validate_dependency_scores,
)


def candidates() -> list[dict]:
    return [
        {"page_id": "p1", "source_turn_id": "S001-Q001", "score": 0.8, "rank": 1},
        {"page_id": "p2", "source_turn_id": "S001-Q002", "score": 0.7, "rank": 2},
        {"page_id": "p3", "source_turn_id": "S001-Q003", "score": 0.6, "rank": 3},
    ]


def test_dependency_score_schema_requires_every_candidate_exactly_once() -> None:
    value = {
        "results": [
            {"page_id": "p1", "dependency_score": 10},
            {"page_id": "p2", "dependency_score": 20.5},
            {"page_id": "p3", "dependency_score": 0},
        ]
    }
    parsed = validate_dependency_scores(value, ["p1", "p2", "p3"])
    assert parsed["score_by_page_id"] == {"p1": 10.0, "p2": 20.5, "p3": 0.0}


@pytest.mark.parametrize(
    "value",
    [
        {"results": [{"page_id": "p1", "dependency_score": 10}]},
        {
            "results": [
                {"page_id": "p1", "dependency_score": 10},
                {"page_id": "p1", "dependency_score": 20},
                {"page_id": "p3", "dependency_score": 30},
            ]
        },
        {
            "results": [
                {"page_id": "p1", "dependency_score": 10},
                {"page_id": "p2", "dependency_score": 20},
                {"page_id": "unknown", "dependency_score": 30},
            ]
        },
        {
            "results": [
                {"page_id": "p1", "dependency_score": -1},
                {"page_id": "p2", "dependency_score": 20},
                {"page_id": "p3", "dependency_score": 30},
            ]
        },
    ],
)
def test_dependency_score_schema_rejects_invalid_outputs(value: dict) -> None:
    with pytest.raises(ValueError):
        validate_dependency_scores(value, ["p1", "p2", "p3"])


def test_ties_keep_original_c3_order_and_candidate_set() -> None:
    original = candidates()
    result = rerank_top_candidates(original, {"p1": 50, "p2": 90, "p3": 50}, 3)
    assert [row["page_id"] for row in result] == ["p2", "p1", "p3"]
    assert {row["page_id"] for row in result} == {row["page_id"] for row in original}
    assert original == candidates()


def test_payload_has_only_allowed_page_fields_and_no_dense_signals() -> None:
    page_by_id = {
        "p1": {"summary": "summary one", "keywords": ["k1"]},
        "p2": {"summary": "summary two", "keywords": ["k2"]},
        "p3": {"summary": "summary three", "keywords": ["k3"]},
    }
    payload = candidate_payload("resolved query", candidates(), page_by_id)
    assert "Current Query:\nresolved query" in payload
    assert "Summary: summary one" in payload
    assert "Keywords: k1" in payload
    assert "Dense" not in payload
    assert "C3 score" not in payload
    assert "source_turn" not in payload
    assert "Raw" not in payload
    assert "Gold" not in payload


def test_prompt_explicitly_forbids_position_heuristic() -> None:
    assert "不得根据候选位置" in SYSTEM_PROMPT
