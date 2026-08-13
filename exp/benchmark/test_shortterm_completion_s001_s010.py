from exp.benchmark.audit_shortterm_completion_s001_s010 import aggregate_shortterm


def test_shortterm_audit_aggregates_query_completion() -> None:
    dataset_stats = {
        "query_count": 3,
        "gold_requirement_count": 3,
        "or_requirement_count": 0,
        "shortterm_requirement_count": 2,
    }
    requirements = [
        {"is_or": False, "shortterm_hit": True},
        {"is_or": False, "shortterm_hit": False},
        {"is_or": False, "shortterm_hit": True},
    ]
    queries = [
        {"shortterm_completion": 0.5},
        {"shortterm_completion": 1.0},
    ]

    result = aggregate_shortterm(dataset_stats, requirements, queries)

    assert result["query_count"] == 2
    assert result["no_gold_query_count"] == 1
    assert result["requirement_hit_count"] == 2
    assert result["requirement_recall"] == 2 / 3
    assert result["mean"] == 0.75
    assert result["full_complete_query_count"] == 1
    assert result["zero_complete_query_count"] == 0
