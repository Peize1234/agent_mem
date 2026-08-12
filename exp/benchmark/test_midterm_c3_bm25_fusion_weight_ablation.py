import math

from exp.benchmark.run_midterm_c3_bm25_fusion_weight_ablation import CONFIGS, weighted_ranking


def dense_rows():
    return [
        {"page_id": "a", "source_turn_id": "Q1", "score": 0.8},
        {"page_id": "b", "source_turn_id": "Q2", "score": 0.7},
        {"page_id": "c", "source_turn_id": "Q3", "score": 0.6},
    ]


def sparse_row():
    return {
        "query_lemmatized": "现金 流 判断",
        "ranking": [{"page_id": "b", "raw_bm25_score": 20.0, "bm25_rank": 1}],
    }


def test_weight_grid_is_frozen_and_sums_to_one():
    assert [config.key for config in CONFIGS] == [
        "H5-W50",
        "H5-W60",
        "H5-W70",
        "H5-W80",
        "H5-W90",
        "H5-W95",
        "H5-W100",
        "H1-W80",
        "H1-W90",
        "H1-W95",
        "H5-W90-ZeroFallback",
    ]
    assert all(abs(config.dense_weight + config.bm25_weight - 1.0) < 1e-12 for config in CONFIGS)


def test_w100_preserves_dense_ranking_and_scores():
    result = weighted_ranking(
        dense_rows(), sparse_row(), dense_weight=1.0, bm25_weight=0.0, candidate_limit=3, zero_fallback=False
    )
    assert [row["page_id"] for row in result] == ["a", "b", "c"]
    assert [row["score"] for row in result] == [0.8, 0.7, 0.6]


def test_zero_fallback_keeps_unmatched_dense_score():
    plain = weighted_ranking(
        dense_rows(), sparse_row(), dense_weight=0.9, bm25_weight=0.1, candidate_limit=3, zero_fallback=False
    )
    fallback = weighted_ranking(
        dense_rows(), sparse_row(), dense_weight=0.9, bm25_weight=0.1, candidate_limit=3, zero_fallback=True
    )
    plain_a = next(row for row in plain if row["page_id"] == "a")
    fallback_a = next(row for row in fallback if row["page_id"] == "a")
    assert math.isclose(plain_a["score"], 0.72, abs_tol=1e-12)
    assert fallback_a["score"] == 0.8
    assert fallback_a["zero_fallback_applied"] is True


def test_zero_fallback_does_not_change_matched_formula():
    plain = weighted_ranking(
        dense_rows(), sparse_row(), dense_weight=0.9, bm25_weight=0.1, candidate_limit=3, zero_fallback=False
    )
    fallback = weighted_ranking(
        dense_rows(), sparse_row(), dense_weight=0.9, bm25_weight=0.1, candidate_limit=3, zero_fallback=True
    )
    plain_b = next(row for row in plain if row["page_id"] == "b")
    fallback_b = next(row for row in fallback if row["page_id"] == "b")
    assert plain_b["score"] == fallback_b["score"]
    assert fallback_b["zero_fallback_applied"] is False
