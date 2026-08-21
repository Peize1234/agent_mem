from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from tuner.evaluate_candidate import _eligible_requirements, _evaluate_session
from tuner.fact_evaluator import deterministic_fact_match, fact_member_hit, parse_required_context
from tuner.models import Dataset, Requirement, Turn
from tuner.experiment_branches import MidtermEvolutionBranch
from tuner.parameter_schema import (
    dynamic_turn_distance_candidates,
    parameter_class,
    promotion_threshold_candidates,
    validate_candidate_config,
)
from tuner.production_midterm_adapter import ProductionMidtermAdapter, _valid_recalled_page_ids
from tuner.stateful_replay import WithinSessionStatefulReplay
from tuner.temporal_replay import CrossSessionTemporalReplay, UNVALIDATED_NO_CROSS_SESSION_GOLD


def _dataset(required_context: str, *, index: int = 1) -> Dataset:
    first = Turn("S001", "S001", 0, "S001-Q001", "旧问题", "旧答案", "", ())
    requirement = Requirement(("S001-Q001",), "S001-Q001")
    second = Turn("S001", "S001", index, "S001-Q002", "当前", "答案", "", (requirement,), required_context)
    return Dataset("synthetic", "d" * 64, {"S001": (first, second)})


def test_required_context_denominator_is_fixed_and_layer_union() -> None:
    dataset = _dataset("2024年收入100万元 AND (增长12% OR 增长0%)")
    eligible = _eligible_requirements(dataset.sessions["S001"][1], dataset.sessions["S001"], "all_memory", 0)
    assert [item.raw_text for item in eligible] == ["2024年收入100万元 AND (增长12% OR 增长0%)"]
    ranking = {
        "S001-Q002": {
            "midterm": [{"source_turn_id": "S001-Q001", "memory": "2024年收入100万元 增长12%"}],
            "session_longterm": [],
        }
    }
    one = _evaluate_session(dataset, "S001", ranking, k=1, target="midterm", shortterm_window=0)
    two = _evaluate_session(dataset, "S001", ranking, k=1, target="midterm", shortterm_window=1)
    assert one["metrics"]["eligible_requirement_count"] == two["metrics"]["eligible_requirement_count"] == 2
    assert one["metrics"]["final_context_recall"] == 1.0
    assert one["metrics"]["short_mid_session_longterm_union"] == 1.0


def test_fact_parser_and_deterministic_types() -> None:
    parsed = parse_required_context("(A OR B) AND C")
    assert [item.members for item in parsed] == [("A", "B"), ("C",)]
    assert deterministic_fact_match("12%", "增长12%")
    assert deterministic_fact_match("2024年", "截至2024年")
    assert deterministic_fact_match("100万元", "收入100万元")
    assert not deterministic_fact_match("100万元", "收入100美元")
    assert fact_member_hit("华辰智能装备有限公司", "授信对象是华辰智能装备有限公司")
    assert not fact_member_hit("华辰智能装备有限公司", "授信对象是另一家公司")
    assert [item.members for item in parse_required_context("A；(B OR C)")] == [("A",), ("B", "C")]


def test_core_branches_have_required_minimum_screening() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    rules = space["search"]["branch_coverage"]
    applicable = {
        "RetrievalControl": ["candidate_coverage_bottleneck", "ranking_bottleneck"],
        "HybridRetrieval": ["candidate_coverage_bottleneck", "ranking_bottleneck"],
        "Embedding": ["candidate_coverage_bottleneck"],
        "Reranking": ["ranking_bottleneck"],
    }
    for branch, regimes in applicable.items():
        for regime in regimes:
            rule = rules[regime]["relevant"][branch]
            assert rule["minimum_attempts"] == 1
            assert rule["coverage_class"] == "required"


def test_final_context_uses_page_budget_not_candidate_depth() -> None:
    dataset = _dataset("必须命中目标事实")
    ranking = {
        "S001-Q002": {
            "midterm": [
                {"memory": "无关内容", "source_turn_id": "A"},
                {"memory": "必须命中目标事实", "source_turn_id": "B"},
            ]
        }
    }
    result = _evaluate_session(
        dataset,
        "S001",
        ranking,
        k=5,
        target="midterm",
        shortterm_window=0,
        max_total_pages=1,
    )
    assert result["metrics"]["candidate_pool_recall"] == 1.0
    assert result["metrics"]["final_context_recall"] == 0.0


def test_max_total_pages_searches_every_legal_final_budget() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    config = space["search"]["stages"]["cheap"]["retrieval"]["max_total_pages"]
    assert config["values"] == [1, 2, 3, 4, 5]


def test_hard_constraints_and_parameter_classes() -> None:
    validate_candidate_config({"max_total_pages": 5, "longterm_top_k": 30, "midterm_candidate_pool_multiplier": 8})
    with pytest.raises(ValueError):
        validate_candidate_config({"max_total_pages": 6})
    with pytest.raises(ValueError):
        validate_candidate_config({"short_term_capacity": 5})
    assert parameter_class("top_k_sessions") == "source-changing"
    assert parameter_class("retention_half_life_turns") == "within-session-stateful"
    assert parameter_class("cross_session_retention_half_life_hours") == "cross-session-temporal-stateful"
    assert parameter_class("max_total_pages") == "query-time"
    assert parameter_class("query_rewrite_prompt") == "query-time"


def test_dynamic_turn_and_heat_threshold_candidates() -> None:
    values = dynamic_turn_distance_candidates([2, 4, 7, 12, 30])
    assert values and all(value > 0 for value in values)
    assert promotion_threshold_candidates([1.0, 2.0, 5.0]) == [2.0]


def test_evolution_and_longterm_branches_are_staged_not_cartesian() -> None:
    source = Path(MidtermEvolutionBranch.generate.__code__.co_filename).read_text(encoding="utf-8")
    evolution = source[source.index("class MidtermEvolutionBranch") : source.index("class PromotionBranch")]
    assert "cartesian_grid\": False" in evolution
    assert "for tau in" not in evolution
    assert "longterm_hybrid_preset" in source
    assert "entity_similarity_threshold" in source


def test_stateful_replay_search_before_add_and_turn_index() -> None:
    clock = {"value": 0}
    calls: list[str] = []

    def search(turn, index):
        calls.append(f"search:{turn}:{index}")
        return [{"id": "page-1"}]

    def add(turn):
        calls.append(f"add:{turn}")
        clock["value"] += 1

    replay = WithinSessionStatefulReplay(search=search, add=add, current_turn_index=lambda: clock["value"])
    result = replay.replay(["Q1", "Q2"])
    assert result.search_before_add
    assert calls == ["search:Q1:0", "add:Q1", "search:Q2:1", "add:Q2"]
    assert [step.turn_index_before_search for step in result.steps] == [0, 1]


def test_stateful_replay_rejects_add_without_turn_progress() -> None:
    replay = WithinSessionStatefulReplay(search=lambda turn, index: [], add=lambda turn: None, current_turn_index=lambda: 0)
    with pytest.raises(RuntimeError, match="did not advance"):
        replay.replay(["Q1"])


def test_stateful_production_replay_confirms_only_gold_visible_pages() -> None:
    turn = _dataset("华辰公司授信额度100万元").sessions["S001"][1]
    checkpoint = {
        "retrieved_results": [
            {"id": "hit", "source": "mid_term_page", "raw_dialogue": "华辰公司授信额度100万元"},
            {"id": "miss", "source": "mid_term_page", "raw_dialogue": "另一家公司授信额度100万元"},
        ]
    }
    assert _valid_recalled_page_ids(turn, checkpoint) == ["hit"]


def test_cross_session_temporal_without_gold_is_not_a_winner() -> None:
    now = datetime.now(timezone.utc)
    replay = CrossSessionTemporalReplay()
    result = replay.replay(
        [
            {"session_id": "A", "user_id": "u", "at": now, "promotions": [{"id": "m"}]},
            {"session_id": "B", "user_id": "u", "at": now + timedelta(hours=24), "promotions": []},
        ],
        retrieve=lambda session, states, at: ["m"] if session["session_id"] == "B" else [],
    )
    assert result.status == UNVALIDATED_NO_CROSS_SESSION_GOLD
    assert result.winner_selection_enabled is False
    assert any(item["event"] == "decay" for item in result.transitions)
    events = [item["event"] for item in result.transitions if item["session_id"] == "B"]
    assert events == ["decay", "valid_recall", "reinforcement"]


def test_session_longterm_replay_honors_all_query_time_controls() -> None:
    checkpoint = {
        "longterm_candidate_pool": {
            "semantic_candidates": [
                {"id": "a", "score": 0.9, "payload": {"data": "A"}},
                {"id": "b", "score": 0.6, "payload": {"data": "B"}},
            ],
            "bm25_scores": {"a": 0.0, "b": 1.0},
            "entity_boosts_by_threshold": {"0.5": {"a": 0.0, "b": 0.5}},
        }
    }
    rows = ProductionMidtermAdapter._rank_session_longterm(
        checkpoint,
        {
            "longterm_top_k": 1,
            "longterm_candidate_pool_multiplier": 2,
            "longterm_rag_threshold": 0.5,
            "longterm_hybrid_preset": "keyword-heavy",
            "entity_similarity_threshold": 0.5,
        },
    )
    assert len(rows) == 1
    assert rows[0]["page_id"] == "b"
    assert rows[0]["candidate_pool_count"] == 2
