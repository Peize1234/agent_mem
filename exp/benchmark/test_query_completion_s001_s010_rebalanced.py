import pytest

from exp.benchmark.analyze_query_completion_s001_s010_rebalanced import (
    pairwise_improvements,
    validate,
)


def test_validate_accepts_requirement_level_monotonic_completion() -> None:
    dataset_stats = {
        "query_count": 752,
        "gold_requirement_count": 786,
        "or_requirement_count": 0,
    }
    requirements = [
        {
            "query_id": f"Q{index}",
            "is_or": False,
            "shortterm_hit": index < 400,
            "midterm_hit_at_5": 400 <= index < 500,
        }
        for index in range(786)
    ]
    queries = [{"query_id": f"Q{index}"} for index in range(752)]
    details = [
        {
            "query_id": "Q0",
            "gold_requirement_count": 786,
            "shortterm_completion": 400 / 786,
            "midterm_completion": 100 / 786,
            "longterm_completion": 200 / 786,
            "short_mid_completion": 500 / 786,
            "short_long_completion": 600 / 786,
            "mid_long_completion": 300 / 786,
            "all_memory_completion": 700 / 786,
        }
    ]
    hits = {
        "short": 400,
        "mid": 100,
        "long": 200,
        "all_memory": 700,
        "no_gold_query_count": 751,
    }

    result = validate(dataset_stats, requirements, queries, details, hits)

    assert result["status"] == "PASS"
    assert result["outside_shortterm_requirement_count"] == 386
    assert result["midterm_requirement_hit_at_5_all_gold"] == 100
    assert result["midterm_outside_shortterm_hit_at_5"] == 100
    assert result["all_memory_requirement_hit_count"] == 700


def test_pairwise_gain_compares_against_better_constituent() -> None:
    stats = {
        "short": {"mean": 0.5},
        "mid": {"mean": 0.2},
        "long": {"mean": 0.4},
        "short_mid": {"label": "Short + Mid", "mean": 0.65},
        "short_long": {"label": "Short + Long", "mean": 0.7},
        "mid_long": {"label": "Mid + Long", "mean": 0.55},
    }

    result = pairwise_improvements(stats)

    assert result[0]["configuration"] == "Short + Long"
    assert result[0]["gain_over_best_constituent"] == pytest.approx(0.2)
