from exp.benchmark.run_midterm_rank_fusion_tuning import (
    fusion_movement_category,
    minmax_normalize,
    protected_order,
    rank_fusion_order,
    score_fusion_order,
)


def sample_pool():
    return [
        {"page_id": "A", "score": 0.9},
        {"page_id": "B", "score": 0.5},
        {"page_id": "C", "score": 0.1},
    ]


def test_rank_fusion_respects_explicit_signal_weights():
    reranker_scores = {"A": 0.1, "B": 0.5, "C": 0.9}

    equal = rank_fusion_order(
        sample_pool(),
        reranker_scores,
        candidate_weight=1.0,
        reranker_weight=1.0,
        rank_constant=60,
    )
    reranker_weighted = rank_fusion_order(
        sample_pool(),
        reranker_scores,
        candidate_weight=1.0,
        reranker_weight=2.0,
        rank_constant=60,
    )

    assert equal == ["A", "C", "B"]
    assert reranker_weighted == ["C", "B", "A"]


def test_score_fusion_normalizes_each_signal_before_combining():
    reranker_scores = {"A": 0.1, "B": 0.8, "C": 0.9}

    assert score_fusion_order(sample_pool(), reranker_scores, candidate_weight=0.2) == ["B", "C", "A"]
    assert score_fusion_order(sample_pool(), reranker_scores, candidate_weight=0.5) == ["B", "A", "C"]


def test_minmax_normalize_handles_constant_pool_without_inventing_signal():
    assert minmax_normalize({"A": 4.0, "B": 4.0}) == {"A": 0.0, "B": 0.0}


def test_candidate_top_n_protection_is_stable_and_deduplicated():
    reranker_scores = {"A": 0.1, "B": 0.2, "C": 0.9}

    order = protected_order(sample_pool(), reranker_scores, protected_count=2)

    assert order == ["A", "B", "C"]
    assert len(order) == len(set(order))


def test_fusion_movement_categories_cover_recovery_and_regression():
    assert fusion_movement_category(2, 9, 4) == "RECOVERED_DEMOTED_GOLD"
    assert fusion_movement_category(9, 3, 7) == "NEWLY_DEMOTED"
    assert fusion_movement_category(9, 8, 4) == "NEWLY_PROMOTED"
    assert fusion_movement_category(9, 8, 7) == "STILL_MISS"
    assert fusion_movement_category(2, 3, 4) == "PRESERVED_HIT"
