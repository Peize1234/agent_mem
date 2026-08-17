from pathlib import Path

from exp.benchmark.analyze_query_completion_s001_s005_v2 import load_v3_dataset
from exp.benchmark.run_midterm_positive_transfer_ablation_s001_s010 import (
    MAIN_CONFIGS,
    SESSION_CODES,
    add_comparisons,
    build_gold_transitions,
    build_routed_queries,
    render_report,
    validate_dataset,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_frozen_rebalanced_dataset_has_expected_transfer_scope() -> None:
    sessions = load_v3_dataset(
        REPO_ROOT / "exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx",
        SESSION_CODES,
    )
    queries = build_routed_queries(sessions)

    result = validate_dataset(sessions, queries)

    assert result["status"] == "PASS"
    assert result["query_count"] == 752
    assert result["gold_requirement_count"] == 786
    assert result["outside_shortterm_requirement_count"] == 386
    assert result["routed_query_count"] == 386
    assert result["routed_gold_count"] == 386
    assert result["or_requirement_count"] == 0


def test_gold_transitions_use_requirement_level_top5_boundaries() -> None:
    queries = [
        {
            "session_code": "S001",
            "query_id": "S001-Q010",
            "eligible_gold_page_ids": ["S001-Q001", "S001-Q002"],
        }
    ]
    rankings = {}
    rank_orders = {
        "A0": ["S001-Q001", "X1", "X2", "X3", "X4", "S001-Q002"],
        "A1": ["S001-Q002", "X1", "X2", "X3", "X4", "S001-Q001"],
        "A2": ["S001-Q002", "S001-Q001", "X1", "X2", "X3", "X4"],
        "A3": ["S001-Q002", "S001-Q001", "X1", "X2", "X3", "X4"],
        "A4": ["S001-Q002", "X1", "X2", "X3", "X4", "S001-Q001"],
    }
    for config, page_ids in rank_orders.items():
        rankings[config] = {
            "S001-Q010": [{"page_id": page_id, "score": 1 / rank} for rank, page_id in enumerate(page_ids, 1)]
        }

    rows, movements = build_gold_transitions(queries, rankings, MAIN_CONFIGS)

    assert len(rows) == 10
    assert movements["A1"]["vs_previous_promoted"] == 1
    assert movements["A1"]["vs_previous_demoted"] == 1
    assert movements["A1"]["vs_previous_net"] == 0
    assert movements["A2"]["vs_previous_promoted"] == 1
    assert movements["A4"]["vs_previous_demoted"] == 1


def test_metric_deltas_are_gold_counts_not_summed_recall() -> None:
    rows = [{"config": config, "gold_at_5": gold} for config, gold in zip(MAIN_CONFIGS, (100, 103, 102, 110, 108))]
    movements = {
        config: {
            "vs_a0_promoted": 0,
            "vs_a0_demoted": 0,
            "vs_a0_net": 0,
            "vs_previous_promoted": 0,
            "vs_previous_demoted": 0,
            "vs_previous_net": 0,
            "vs_c3_promoted": 0,
            "vs_c3_demoted": 0,
            "vs_c3_net": 0,
        }
        for config in MAIN_CONFIGS
    }

    add_comparisons(rows, movements)

    assert rows[0]["delta_vs_previous"] is None
    assert rows[1]["delta_vs_a0"] == 3
    assert rows[2]["delta_vs_previous"] == -1
    assert rows[3]["delta_vs_c3"] == 2
    assert rows[4]["delta_vs_c3"] == 0


def test_report_does_not_hide_a_config_that_beats_c3() -> None:
    rows = []
    for config, gold, previous in zip(MAIN_CONFIGS, (238, 229, 217, 182, 179), (None, -9, -12, -35, -3)):
        rows.append(
            {
                "config": config,
                "gold_at_5": gold,
                "eligible_gold_count": 386,
                "micro_r_at_5": gold / 386,
                "delta_vs_a0": gold - 238,
                "delta_vs_previous": previous,
                "delta_vs_c3": gold - 179,
                "r_at_10": 0.7,
                "r_at_20": 0.8,
                "mrr": 0.4,
                "mean_gold_rank": 10.0,
                "mid_mean_completion": 0.3,
                "short_mid_mean_completion": 0.7,
                "short_mid_full_count": 500,
                "short_mid_full_rate": 500 / 742,
                "all_memory_mean_completion": 0.8,
                "all_memory_full_count": 550,
                "all_memory_full_rate": 550 / 742,
                "gold_bearing_query_count": 742,
                "all_memory_requirement_hit_count": 600,
                "all_memory_requirement_count": 786,
                "vs_previous_promoted": 1,
                "vs_previous_demoted": 2,
                "vs_previous_net": -1,
            }
        )

    report = render_report(
        rows,
        [],
        {"exact_top5_query_count": 385, "query_count": 386, "top5_order_mismatch_count": 1},
    )

    assert "Best tested main configuration by Gold@5: A0" in report
    assert "exceed frozen C3 by +59, +50, +38, and +3" in report
    assert "Stable generalization on the primary Gold@5 metric: none" in report
