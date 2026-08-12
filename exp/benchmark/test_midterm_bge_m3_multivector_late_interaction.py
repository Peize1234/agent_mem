import math

import numpy as np

from exp.benchmark.run_midterm_bge_m3_multivector_late_interaction import (
    cache_identity,
    maxsim_score,
    official_colbert_score,
    rerank_c3_top60,
)


def test_maxsim_uses_mean_of_query_token_max_cosines():
    query = np.asarray([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    page = np.asarray([[1.0, 0.0], [1.0, 1.0]], dtype=np.float32)
    expected = (1.0 + 1.0 / math.sqrt(2.0)) / 2.0
    assert math.isclose(maxsim_score(query, page), expected, abs_tol=1e-6)


def test_custom_maxsim_matches_flagembedding_official_colbert_score():
    generator = np.random.default_rng(7)
    query = generator.normal(size=(5, 16)).astype(np.float32)
    page = generator.normal(size=(11, 16)).astype(np.float32)
    custom = maxsim_score(query, page)
    official = official_colbert_score(query, page)
    assert math.isclose(custom, official, abs_tol=1e-6)


def test_top60_rerank_only_reorders_fixed_candidates():
    c3 = {
        "Q": [
            {"page_id": "A", "source_turn_id": "A", "rank": 1, "score": 0.9},
            {"page_id": "B", "source_turn_id": "B", "rank": 2, "score": 0.8},
            {"page_id": "C", "source_turn_id": "C", "rank": 3, "score": 0.7},
        ]
    }
    pure = {
        "Q": [
            {"page_id": "C", "rank": 1, "maxsim_score": 0.9},
            {"page_id": "B", "rank": 2, "maxsim_score": 0.5},
            {"page_id": "A", "rank": 3, "maxsim_score": 0.1},
        ]
    }
    reranked = rerank_c3_top60(c3, pure)["Q"]
    assert [row["page_id"] for row in reranked] == ["C", "B", "A"]
    assert all(row["reranked_by_multivector"] for row in reranked)


def test_top60_rerank_preserves_c3_tail_order():
    c3_rows = [
        {"page_id": f"P{index:02d}", "source_turn_id": f"P{index:02d}", "rank": index, "score": 1 - index / 100}
        for index in range(1, 63)
    ]
    pure_rows = [
        {"page_id": row["page_id"], "rank": row["rank"], "maxsim_score": float(row["rank"])} for row in c3_rows
    ]
    reranked = rerank_c3_top60({"Q": c3_rows}, {"Q": pure_rows})["Q"]
    assert {row["page_id"] for row in reranked[:60]} == {row["page_id"] for row in c3_rows[:60]}
    assert [row["page_id"] for row in reranked[60:]] == ["P61", "P62"]
    assert all(not row["reranked_by_multivector"] for row in reranked[60:])


def test_cache_identity_includes_revision_text_and_scoring_contract():
    first = cache_identity(kind="pages", ids=["P"], texts=["text"], model_revision="rev1", max_length=1024)
    same = cache_identity(kind="pages", ids=["P"], texts=["text"], model_revision="rev1", max_length=1024)
    changed = cache_identity(kind="pages", ids=["P"], texts=["changed"], model_revision="rev1", max_length=1024)
    assert first == same
    assert first != changed
    assert first["representation"] == "colbert"
    assert first["model_revision"] == "rev1"
    assert "scoring" in first
