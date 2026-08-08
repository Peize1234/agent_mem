from __future__ import annotations

from types import SimpleNamespace

import pytest

from exp.benchmark.run_midterm_multi_session_validation import (
    bootstrap_query_delta,
    expected_frozen_config,
    movement_category,
    validate_session_snapshot,
    win_tie_loss,
)


def test_frozen_config_uses_predeclared_s001_parameters() -> None:
    sessions = [SimpleNamespace(session_id=f"S00{index}_session") for index in range(1, 6)]

    config = expected_frozen_config(sessions)

    assert config["development_session"] == "S001_session"
    assert config["heldout_sessions"] == [f"S00{index}_session" for index in range(2, 6)]
    assert config["quality"] == {
        "candidate_representation": "P0",
        "retrieval": "dense+chinese_bm25",
        "fusion": "RRF",
        "rrf_constant": 60,
        "candidate_k": 20,
        "rerank_representation": "P8",
        "reranker": "BAAI/bge-reranker-base",
        "final_ranking": "reranker_only",
        "output_k": 5,
    }
    assert config["pareto"]["candidate_k"] == 15
    assert config["pareto"]["candidate_top_n_protection"] == 2
    assert config["parameter_tuning_on_heldout_allowed"] is False


def test_query_bootstrap_is_deterministic_and_micro_weighted() -> None:
    queries = [
        {"query_id": "Q1", "eligible_gold_page_ids": ["P1", "P2"]},
        {"query_id": "Q2", "eligible_gold_page_ids": ["P3"]},
    ]
    baseline = {
        "Q1": [{"page_id": "P1"}, {"page_id": "X"}, {"page_id": "P2"}],
        "Q2": [{"page_id": "X"}, {"page_id": "P3"}],
    }
    contender = {
        "Q1": [{"page_id": "P1"}, {"page_id": "P2"}],
        "Q2": [{"page_id": "P3"}],
    }

    first = bootstrap_query_delta(queries, baseline, contender, iterations=500, seed=42)
    second = bootstrap_query_delta(queries, baseline, contender, iterations=500, seed=42)

    assert first == second
    assert first["observed_delta_recall_at_5"] == 0.0
    assert first["unit"] == "query"


@pytest.mark.parametrize(
    ("baseline_rank", "quality_rank", "expected"),
    [
        (8, 3, "PROMOTED_INTO_TOP5"),
        (3, 8, "DEMOTED_OUT_OF_TOP5"),
        (3, 2, "PRESERVED_HIT"),
        (8, 9, "STILL_MISS"),
    ],
)
def test_movement_categories(baseline_rank: int, quality_rank: int, expected: str) -> None:
    assert movement_category(baseline_rank, quality_rank) == expected


def test_win_tie_loss_uses_only_heldout_rows() -> None:
    rows = [
        {"role": "development", "baseline_recall_at_5": 0.0, "quality_recall_at_5": 1.0},
        {"role": "heldout", "baseline_recall_at_5": 0.2, "quality_recall_at_5": 0.3},
        {"role": "heldout", "baseline_recall_at_5": 0.3, "quality_recall_at_5": 0.3},
        {"role": "heldout", "baseline_recall_at_5": 0.4, "quality_recall_at_5": 0.2},
    ]

    assert win_tie_loss(rows, "quality", heldout_only=True) == {"win": 1, "tie": 1, "loss": 1}


def test_snapshot_validation_rejects_future_page_leak() -> None:
    session = SimpleNamespace(session_id="S002_session")
    queries = [
        {
            "query_id": "S002-Q010",
            "session_id": "S002_session",
            "turn_index": 9,
            "eligible_gold_page_ids": [],
        }
    ]
    pages = [{"page_id": "P1", "source_turn_id": "S002-Q008", "source_turn_index": 7}]
    visibility = [
        {
            "query_id": "S002-Q010",
            "visible_page_ids": ["P1"],
            "future_page_leak_count": 1,
        }
    ]

    with pytest.raises(ValueError, match="future Page leakage"):
        validate_session_snapshot(session, queries, pages, visibility)
