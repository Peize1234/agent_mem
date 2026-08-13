import pytest
from pathlib import Path

from exp.benchmark.analyze_query_completion_s001_s005_v2 import (
    build_query_completion_rows,
    completion_statistics,
    load_v3_dataset,
    source_qa_hash,
    static_dataset_stats,
    validate_baseline_and_completion,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_requirement_level_union_and_or_group_are_counted_once() -> None:
    requirements = [
        {
            "requirement_id": "S001-Q002::G1",
            "session_id": "S001",
            "query_id": "S001-Q002",
            "gold_members": ["S001-Q001", "S001-Q000"],
            "is_or": True,
            "shortterm_hit": False,
            "midterm_rank": 3,
            "midterm_hit_at_5": True,
            "longterm_hit": False,
        },
        {
            "requirement_id": "S001-Q002::G2",
            "session_id": "S001",
            "query_id": "S001-Q002",
            "gold_members": ["S001-Q003"],
            "is_or": False,
            "shortterm_hit": True,
            "midterm_rank": None,
            "midterm_hit_at_5": False,
            "longterm_hit": False,
        },
        {
            "requirement_id": "S001-Q004::G1",
            "session_id": "S001",
            "query_id": "S001-Q004",
            "gold_members": ["S001-Q001"],
            "is_or": False,
            "shortterm_hit": False,
            "midterm_rank": None,
            "midterm_hit_at_5": False,
            "longterm_hit": False,
        },
    ]
    queries = [
        {"session_id": "S001", "query_id": "S001-Q001"},
        {"session_id": "S001", "query_id": "S001-Q002"},
        {"session_id": "S001", "query_id": "S001-Q004"},
    ]
    longterm = {
        "S001-Q001": (),
        "S001-Q002": ("S001-Q000",),
        "S001-Q004": (),
    }

    details, hits = build_query_completion_rows(requirements, queries, longterm)

    assert len(details) == 2
    assert hits["no_gold_query_count"] == 1
    assert hits["mid"] == 1
    assert hits["long"] == 1
    assert hits["longterm_scope_difference_requirement_ids"] == ["S001-Q002::G1"]
    assert details[0]["gold_requirement_count"] == 2
    assert details[0]["shortterm_completion"] == 0.5
    assert details[0]["midterm_completion"] == 0.5
    assert details[0]["longterm_completion"] == 0.5
    assert details[0]["all_memory_completion"] == 1.0
    assert details[1]["all_memory_completion"] == 0.0


def test_completion_statistics_use_query_denominators() -> None:
    details = [
        {"shortterm_completion": 0.0},
        {"shortterm_completion": 0.5},
        {"shortterm_completion": 1.0},
    ]
    complete_rows = []
    for row in details:
        value = row["shortterm_completion"]
        complete_rows.append(
            {
                **row,
                "midterm_completion": value,
                "longterm_completion": value,
                "short_mid_completion": value,
                "short_long_completion": value,
                "mid_long_completion": value,
                "all_memory_completion": value,
            }
        )
    hits = {key: 0 for key in ("short", "mid", "long", "short_mid", "short_long", "mid_long", "all_memory")}

    stats = completion_statistics(complete_rows, hits)["short"]

    assert stats["query_count"] == 3
    assert stats["mean"] == 0.5
    assert stats["median"] == 0.5
    assert stats["p25"] == 0.25
    assert stats["p75"] == 0.75
    assert stats["zero_complete_query_count"] == 1
    assert stats["full_complete_query_count"] == 1
    assert stats["completion_ge_50_rate"] == pytest.approx(2 / 3)


def test_monotonicity_validation_rejects_invalid_union() -> None:
    dataset_stats = {"query_count": 348, "gold_requirement_count": 559, "or_requirement_count": 5}
    summary = {
        "shortterm": {"hit_count": 410},
        "midterm": {"eligible_gold_count": 149, "gold_at_5": 59},
        "longterm": {"hit_count": 37},
        "overall": {"hit_count": 484},
    }
    requirements = [
        {"requirement_id": f"Q::G{index}", "is_or": index <= 5} for index in range(1, 560)
    ]
    queries = [{"query_id": f"Q{index}"} for index in range(348)]
    details = [
        {
            "query_id": "Q1",
            "gold_requirement_count": 559,
            "shortterm_completion": 1.0,
            "midterm_completion": 0.0,
            "longterm_completion": 0.0,
            "short_mid_completion": 0.5,
            "short_long_completion": 1.0,
            "mid_long_completion": 0.0,
            "all_memory_completion": 1.0,
        }
    ]
    hits = {"short": 410, "mid": 59, "long": 38, "all_memory": 484, "no_gold_query_count": 347}
    hits["longterm_scope_difference_requirement_ids"] = []

    with pytest.raises(AssertionError, match="Pairwise completion monotonicity"):
        validate_baseline_and_completion(dataset_stats, summary, requirements, queries, details, hits)


def test_rechecked_dependency_workbook_matches_frozen_source_qa() -> None:
    session_codes = ("S001", "S002", "S003", "S004", "S005")
    frozen = load_v3_dataset(
        REPO_ROOT / "exp/enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx", session_codes
    )
    rechecked = load_v3_dataset(REPO_ROOT / "exp/金融分析数据集_必要依赖重新核验版.xlsx", session_codes)

    assert source_qa_hash(rechecked) == source_qa_hash(frozen)
    assert static_dataset_stats(rechecked, 3, 3) == {
        "session_count": 5,
        "query_count": 348,
        "gold_requirement_count": 384,
        "or_requirement_count": 0,
        "shortterm_requirement_count": 350,
        "outside_shortterm_requirement_count": 34,
        "shortterm_window_qa_turns": 3,
        "shortterm_top_k": 3,
        "session_query_counts": {"S001": 50, "S002": 73, "S003": 93, "S004": 74, "S005": 58},
    }
