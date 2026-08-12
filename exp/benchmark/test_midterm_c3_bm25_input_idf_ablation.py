from exp.benchmark.run_midterm_c3_bm25_input_idf_ablation import (
    BM25_FILTER_TOKENS,
    CONFIGS,
    filtered_bm25_text,
    local_top60_hybrid_ranking,
)


def test_filter_contract_is_frozen_and_keeps_financial_and_task_terms():
    assert BM25_FILTER_TOKENS == (
        "的",
        "和",
        "与",
        "在",
        "了",
        "是",
        "把",
        "也",
        "中",
        "请",
        "回答",
        "前面",
        "当前",
        "刚才",
        "结合",
        "如果",
        "具体",
        "体现",
        "怎么",
        "说明",
    )
    filtered = filtered_bm25_text("请结合前面的归母净利润，回答这个反证与近似周转问题。")

    assert "请" not in filtered.split()
    assert "结合" not in filtered.split()
    assert "前面" not in filtered.split()
    assert "回答" not in filtered.split()
    assert "归母" in filtered.split()
    assert "净利润" in filtered.split()
    assert "反证" in filtered.split()
    assert "周转" in filtered.split()


def test_local_idf_ranking_never_promotes_a_page_outside_dense_top60():
    dense = [
        {
            "page_id": f"p{index:02d}",
            "source_turn_id": f"turn-{index:02d}",
            "score": 1.0 - index / 100.0,
            "rank": index + 1,
        }
        for index in range(70)
    ]
    bm25 = [
        {"page_id": "p59", "raw_bm25_score": 30.0, "bm25_rank": 1},
        {"page_id": "p60", "raw_bm25_score": 100.0, "bm25_rank": 2},
    ]

    ranked = local_top60_hybrid_ranking(dense, bm25, "近似周转", "近似 周转")

    assert len(ranked) == 70
    assert ranked[0]["page_id"] == "p59"
    assert next(row for row in ranked if row["page_id"] == "p60")["semantic_candidate"] is False
    assert next(row for row in ranked if row["page_id"] == "p60")["rank"] > 60


def test_ablation_contains_only_the_six_frozen_configs():
    assert [config.key for config in CONFIGS] == ["H0", "H1", "H2", "H3", "H4", "H5"]
