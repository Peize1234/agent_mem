"""Unit tests for the offline LongTerm Top20 recomputation helpers."""

from __future__ import annotations

from exp.benchmark.recompute_longterm_top20_s001_s005 import (
    build_lineage_map,
    evaluate_requirements,
    ordered_unique,
    summarize,
    validate_top5,
)


def test_ordered_unique_preserves_first_occurrence():
    assert ordered_unique(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]
    assert ordered_unique([]) == []


def test_build_lineage_map_uses_evicted_turns():
    traces = {
        "S001-Q004": {"turn_id": "S001-Q004", "migration_job_id": "job-4", "evicted_turn_ids": ["S001-Q001"]},
        "S001-Q005": {"turn_id": "S001-Q005", "migration_job_id": "job-5", "evicted_turn_ids": ["S001-Q002"]},
        "S001-Q001": {"turn_id": "S001-Q001", "migration_job_id": None, "evicted_turn_ids": []},
    }
    assert build_lineage_map(traces) == {
        "job-4": ("S001-Q001",),
        "job-5": ("S001-Q002",),
    }


def _requirement_row(requirement_id, query_id, gold_members, midterm_hit=False, long_hit_old=False):
    return {
        "requirement_id": requirement_id,
        "query_id": query_id,
        "session_id": query_id.rsplit("-", 1)[0],
        "turn_index": 6,
        "group_index": 1,
        "is_or": False,
        "gold_members": gold_members,
        "midterm_eligible": True,
        "midterm_hit_at_5": midterm_hit,
        "longterm_hit": long_hit_old,
    }


def test_evaluate_requirements_hits_at_5_and_20():
    rows = [
        _requirement_row("S001-Q007::G1", "S001-Q007", ["S001-Q001"]),
        _requirement_row("S001-Q021::G1", "S001-Q021", ["S001-Q017"]),
        _requirement_row("S001-Q021::G2", "S001-Q021", ["S001-Q030"]),
    ]
    top20 = {
        "S001-Q007": ["S001-Q001", "S001-Q002"],
        "S001-Q021": ["S001-Q010", "S001-Q017", "S001-Q020"],
    }
    results = evaluate_requirements(rows, top20)
    by_id = {row["requirement_id"]: row for row in results}
    assert by_id["S001-Q007::G1"]["longterm_rank"] == 1
    assert by_id["S001-Q007::G1"]["longterm_hit_at_5"] is True
    assert by_id["S001-Q021::G1"]["longterm_rank"] == 2
    assert by_id["S001-Q021::G1"]["longterm_hit_at_20"] is True
    assert by_id["S001-Q021::G2"]["longterm_rank"] is None
    assert by_id["S001-Q021::G2"]["longterm_hit_at_20"] is False


def test_evaluate_requirements_or_group_min_rank():
    rows = [
        _requirement_row("S001-Q021::G1", "S001-Q021", ["S001-Q017", "S001-Q030"]),
    ]
    results = evaluate_requirements(
        rows,
        {"S001-Q021": ["S001-Q010", "S001-Q030", "S001-Q017"]},
    )
    assert results[0]["longterm_rank"] == 2


def test_evaluate_requirements_marks_missing_gold():
    rows = [
        _requirement_row("S001-Q022::G2", "S001-Q022", ["S001-Q018"]),
    ]
    results = evaluate_requirements(
        rows,
        {"S001-Q022": ["S001-Q010"]},
        missing_content_turns={"S001": ["S001-Q018"]},
    )
    assert results[0]["gold_missing_from_store"] == ["S001-Q018"]
    assert results[0]["longterm_hit_at_20"] is False


def test_evaluate_requirements_union_hit():
    rows = [
        _requirement_row("S001-Q007::G1", "S001-Q007", ["S001-Q001"], midterm_hit=True),
        _requirement_row("S001-Q021::G1", "S001-Q021", ["S001-Q030"]),
    ]
    results = evaluate_requirements(
        rows,
        {"S001-Q007": ["S001-Q001"], "S001-Q021": ["S001-Q010", "S001-Q030"]},
    )
    by_id = {row["requirement_id"]: row for row in results}
    assert by_id["S001-Q007::G1"]["union_hit"] is True
    assert by_id["S001-Q021::G1"]["union_hit"] is True


def test_summarize_metrics_math():
    rows = [
        {
            "longterm_rank": 1,
            "longterm_hit_at_5": True,
            "longterm_hit_at_20": True,
            "longterm_hit_at_5_old": True,
            "midterm_hit_at_5": False,
            "gold_missing_from_store": [],
            "union_hit": True,
            "session_id": "S001",
        },
        {
            "longterm_rank": 12,
            "longterm_hit_at_5": False,
            "longterm_hit_at_20": True,
            "longterm_hit_at_5_old": False,
            "midterm_hit_at_5": False,
            "gold_missing_from_store": [],
            "union_hit": True,
            "session_id": "S001",
        },
        {
            "longterm_rank": None,
            "longterm_hit_at_5": False,
            "longterm_hit_at_20": False,
            "longterm_hit_at_5_old": False,
            "midterm_hit_at_5": False,
            "gold_missing_from_store": ["S002-Q018"],
            "union_hit": False,
            "session_id": "S002",
        },
    ]
    summary = summarize(rows)
    assert summary["outside_shortterm_gold"] == 3
    assert summary["longterm_hit_at_5_old"] == 1
    assert summary["longterm_hit_at_20"] == 2
    assert summary["indeterminate_longterm_at_20"] == 1
    assert summary["longterm_recall_at_20_upper_bound"] == 1.0
    assert summary["mid_union_long_at_20"] == 2
    assert summary["overall_hit"] == 412


def test_validate_top5_classification():
    traces = {
        "S001-Q007": {"long_retrieved_turn_ids": ["S001-Q001", "S001-Q002"]},
        "S001-Q008": {"long_retrieved_turn_ids": ["S001-Q003", "S001-Q004"]},
        "S001-Q009": {"long_retrieved_turn_ids": ["S001-Q005", "S001-Q006"]},
        "S001-Q010": {"long_retrieved_turn_ids": []},
    }
    top20 = {
        "S001-Q007": ["S001-Q001", "S001-Q002", "S001-Q003"],
        "S001-Q008": ["S001-Q004", "S001-Q003"],
        "S001-Q009": ["S001-Q005", "S001-Q099"],
    }
    validation = validate_top5(traces, top20, ["S001-Q007", "S001-Q008", "S001-Q009", "S001-Q010"])
    assert validation["evaluated_queries"] == 3
    # Q008 is the same set in a different order; Q007/Q009 are list mismatches.
    assert validation["exact_match"] == 0
    assert validation["same_set_diff_order"] == 1
    assert validation["diff"] == 2
