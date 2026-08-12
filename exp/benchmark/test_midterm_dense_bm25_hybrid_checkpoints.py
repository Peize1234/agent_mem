from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import (
    production_hybrid_ranking,
    query_categories,
)


def dense_rows(scores):
    return [
        {
            "page_id": page_id,
            "source_turn_id": f"turn-{page_id}",
            "score": score,
            "rank": rank,
        }
        for rank, (page_id, score) in enumerate(scores, start=1)
    ]


def test_production_hybrid_ranking_uses_production_additive_score():
    dense = dense_rows([("p1", 0.90), ("p2", 0.80), ("p3", 0.70)])
    bm25 = [{"page_id": "p3", "raw_bm25_score": 20.0, "bm25_rank": 1}]

    ranked = production_hybrid_ranking(dense, bm25, "归母净利润", "归母净利润", 5)

    assert [row["page_id"] for row in ranked] == ["p3", "p1", "p2"]
    assert ranked[0]["semantic_score"] == 0.70
    assert ranked[0]["raw_bm25_score"] == 20.0
    assert ranked[0]["bm25_score"] > 0.99
    assert ranked[0]["final_score"] == ranked[0]["score"]


def test_production_hybrid_ranking_preserves_semantic_threshold_gate():
    dense = dense_rows([("p1", 0.90), ("p2", 0.80), ("p3", 0.05)])
    bm25 = [{"page_id": "p3", "raw_bm25_score": 100.0, "bm25_rank": 1}]

    ranked = production_hybrid_ranking(dense, bm25, "归母净利润", "归母净利润", 5)

    assert [row["page_id"] for row in ranked] == ["p1", "p2", "p3"]
    assert ranked[-1]["semantic_candidate"] is False
    assert ranked[-1]["final_score"] is None


def test_query_categories_detects_financial_and_task_anchors():
    categories = query_categories("2024年贵州茅台的归母净利润能否作为前面结论的反证？")

    assert "指标名" in categories
    assert "年份/数字" in categories
    assert "实体" in categories
    assert "反证/反例" in categories
