from __future__ import annotations

import pytest

from exp.benchmark.midterm_retrieval_eval import (
    append_reranked_candidates,
    bm25_sanity_cases,
    evaluate_rankings,
    page_representation,
    rrf_fuse,
)
from exp.benchmark.run_midterm_retrieval_experiments import contextual_query


def test_production_page_representation_order_and_prefixes() -> None:
    page = {
        "summary": "summary",
        "keywords": ["现金流", "净利润"],
        "user_input": "question",
        "assistant_response": "answer",
        "raw_dialogue": "raw",
    }

    assert page_representation(page, "P0") == "summary\nKeywords: 现金流, 净利润\nUser: question"
    assert page_representation(page, "P4") == "User: question\nAssistant: answer"
    assert page_representation(page, "P8") == "summary\nKeywords: 现金流, 净利润"


def test_evaluator_preserves_multiple_gold_pages() -> None:
    queries = [{"query_id": "Q1", "eligible_gold_page_ids": ["P1", "P3"]}]
    rankings = {
        "Q1": [
            {"page_id": "P1", "score": 0.9},
            {"page_id": "P2", "score": 0.8},
            {"page_id": "P3", "score": 0.7},
        ]
    }

    metrics, per_query = evaluate_rankings(queries, rankings)

    assert metrics["eligible_gold_count"] == 2
    assert metrics["recall_at_1"] == pytest.approx(0.5)
    assert metrics["recall_at_3"] == pytest.approx(1.0)
    assert metrics["mrr"] == pytest.approx(1.0)
    assert metrics["mean_gold_rank"] == pytest.approx(2.0)
    assert per_query[0]["gold_ranks"] == [1, 3]


def test_rrf_and_rerank_keep_a_full_diagnostic_ranking() -> None:
    dense = [
        {"page_id": "P1", "source_turn_id": "T1", "score": 0.9},
        {"page_id": "P2", "source_turn_id": "T2", "score": 0.8},
        {"page_id": "P3", "source_turn_id": "T3", "score": 0.7},
    ]
    sparse = [dense[2], dense[1], dense[0]]

    fused = rrf_fuse((dense, sparse), rank_constant=60)
    reranked = append_reranked_candidates(fused, ["P3", "P1"], candidate_k=2)

    assert len(fused) == 3
    assert [row["page_id"] for row in reranked] == ["P3", "P1", "P2"]
    assert [row["rank"] for row in reranked] == [1, 2, 3]


def test_chinese_bm25_sanity_cases_are_discriminative() -> None:
    cases = bm25_sanity_cases()

    assert len(cases) >= 3
    assert all(case["passed"] for case in cases)
    assert all(case["positive_score"] > case["negative_score"] for case in cases)


def test_contextual_query_keeps_current_and_recent_turn_first() -> None:
    query = {
        "original_query": "current",
        "previous_2_qa": [
            {"user": "older-user", "assistant": "older-answer"},
            {"user": "recent-user", "assistant": "recent-answer"},
        ],
    }

    text = contextual_query(query, 2)

    assert text.startswith("Current user query: current")
    assert text.index("recent-user") < text.index("older-user")
