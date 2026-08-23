from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml
from mem0.configs.base import MidTermMemoryConfig
from tuner.artifact_registry import ArtifactRegistry
from tuner.evaluate_candidate import _eligible_requirements, _evaluate_session, _fact_rows_for_visible
from tuner.experiment_branches import (
    BranchContext,
    BranchRegistry,
    MidtermEvolutionBranch,
    MidtermSourceConfigBranch,
    PromotionBranch,
)
from tuner.fact_evaluator import deterministic_fact_match, fact_member_hit, parse_required_context
from tuner.io_utils import sha256_file
from tuner.models import Candidate, CandidateResult, Dataset, Requirement, Turn
from tuner.parameter_schema import (
    dynamic_turn_distance_candidates,
    production_integer_candidates,
    production_literal_candidates,
    production_parameter_metadata,
    promotion_threshold_candidates,
    validate_candidate_config,
)
from tuner.production_midterm_adapter import (
    ProductionMidtermAdapter,
    _diagnostic_rows_from_pages,
    _unselected_page_diagnostic_rows,
    _valid_recalled_page_ids,
)
from tuner.stateful_replay import WithinSessionStatefulReplay
from tuner.temporal_replay import (
    CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD,
    STRUCTURAL_ONLY_NOT_EVALUATED,
    UNSUPPORTED_TEMPORAL_GOLD_SCHEMA,
    UNVALIDATED_NO_CROSS_SESSION_GOLD,
    CrossSessionTemporalReplay,
    run_cross_session_temporal_replay,
)


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


def test_entity_matching_rejects_shared_generic_tokens() -> None:
    assert not fact_member_hit("华辰智能装备有限公司", "授信对象为华辰智能设备有限公司")
    assert not fact_member_hit("中证新能源指数", "报告讨论中证消费指数")
    assert fact_member_hit("阿里巴巴（别名：阿里）", "证券名称：阿里")


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


def test_skill_document_matches_current_run_only_research_boundary() -> None:
    text = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text(encoding="utf-8")
    assert "跨 run winner" in text
    assert "不得作为搜索先验" in text
    assert "references/experiment_lessons.md" not in text


def test_new_branches_are_reachable_through_real_coverage_policy() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    registry = BranchRegistry()
    policy = space["search"]["branch_coverage"]

    def next_branch(regime: str, attempts: dict[str, int]) -> str:
        selected = registry.select(
            regime=regime,
            max_cost_level="expensive",
            attempt_counts=attempts,
            exhausted=set(),
            initial_stage=False,
            limit=1,
            branch_settings=space["search"]["branch_registry"],
            coverage_policy=policy,
            remaining_expensive_candidates=100,
        )
        assert selected
        return selected[0].spec.name

    assert (
        next_branch("session_instability", {"RetrievalControl": 1, "FineGrainedLongtermRetrieval": 1})
        == "MidtermEvolution"
    )
    ranking_selected = registry.select(
        regime="ranking_bottleneck",
        max_cost_level="expensive",
        attempt_counts={"RetrievalControl": 1, "HybridRetrieval": 1, "Reranking": 1, "QueryRewritePrompt": 1},
        exhausted=set(),
        initial_stage=False,
        limit=5,
        branch_settings=space["search"]["branch_registry"],
        coverage_policy=policy,
        remaining_expensive_candidates=100,
    )
    assert any(branch.spec.name == "MidtermEvolution" for branch in ranking_selected)
    assert (
        next_branch(
            "session_instability",
            {"RetrievalControl": 1, "FineGrainedLongtermRetrieval": 1, "MidtermEvolution": 1},
        )
        == "PageRepresentation"
    )
    assert "Promotion" not in policy["session_instability"]["relevant"]
    assert "Promotion" not in policy["balanced_or_plateau"]["relevant"]
    assert "Promotion" not in {branch.spec.name for branch in registry.ordered()}
    cross_session = space["search"]["stages"]["secondary"]["cross_session_temporal"]
    assert cross_session["tuning"] == "disabled"
    assert cross_session["winner_selection"] == "disabled"
    assert cross_session["production_config_policy"] == "unchanged_defaults"
    assert "promotion_min_recall_count" not in space["search"]["stages"]["cheap"]["retrieval"]["midterm_evolution"]
    assert (
        next_branch(
            "candidate_coverage_bottleneck",
            {
                "RetrievalControl": 1,
                "HybridRetrieval": 1,
                "Embedding": 1,
                "FineGrainedLongtermRetrieval": 1,
            },
        )
        == "AgenticRetrieval"
    )
    assert (
        next_branch(
            "candidate_coverage_bottleneck",
            {
                "RetrievalControl": 1,
                "HybridRetrieval": 1,
                "AgenticRetrieval": 1,
                "Embedding": 1,
                "FineGrainedLongtermRetrieval": 1,
            },
        )
        == "QueryRewritePrompt"
    )
    candidate_relevant = policy["candidate_coverage_bottleneck"]["relevant"]
    assert candidate_relevant["MidtermSourceConfig"]["minimum_attempts"] == 1
    assert candidate_relevant["MidtermSourceConfig"]["coverage_class"] == "expensive_gated"
    assert "QueryRepresentation" not in candidate_relevant


def test_yaml_and_branch_specs_agree_on_midterm_evolution_regimes() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    policy = space["search"]["branch_coverage"]
    spec = BranchRegistry().get("MidtermEvolution").spec
    configured = {regime for regime, value in policy.items() if "MidtermEvolution" in (value.get("relevant") or {})}
    assert configured <= set(spec.diagnostic_regimes)
    assert "ranking_bottleneck" in spec.diagnostic_regimes


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
    assert result["metrics"]["midterm_final_context_recall"] == 0.0
    assert result["metrics"]["final_context_recall"] == 0.0


def test_adapter_diagnostic_trace_drives_all_failure_classes() -> None:
    dataset = _dataset("目标事实")
    turn = dataset.sessions["S001"][1]

    def evaluate(rows: list[dict[str, object]]) -> str:
        ranking = {turn.query_id: {"midterm": rows, "session_longterm": []}}
        result = _evaluate_session(
            dataset,
            "S001",
            ranking,
            k=1,
            target="all_memory",
            shortterm_window=0,
            max_total_pages=1,
            longterm_top_k=30,
        )
        return str(result["requirements"][0]["failure_class"])

    def page(**overrides: object) -> dict[str, object]:
        return {
            "id": "page",
            "source": "mid_term_page",
            "source_job_id": "job",
            "memory": "目标事实",
            "summary": "目标事实",
            "raw_dialogue": "目标事实",
            "raw_rag_score": 0.9,
            "final_score": 0.9,
            "rank_before_threshold": 1,
            "threshold_passed": True,
            "threshold_filtered": False,
            "final_rank": 1,
            "final_visible": True,
            "routed_candidate": True,
            **overrides,
        }

    routed = {"selected_sessions": [{"id": "session", "session_id": "session"}]}
    checkpoint = {
        "sessions": [{"id": "session", "payload": {"session_id": "session"}}],
        "pages": [
            {
                "id": "page",
                "payload": {"session_id": "session", "source_job_id": "job", "summary": "目标事实"},
            }
        ],
    }
    routing_rows = _unselected_page_diagnostic_rows(
        {**checkpoint, "sessions": [{"id": "other", "payload": {"session_id": "other"}}]},
        {"selected_sessions": [{"id": "other", "session_id": "other"}]},
        {"job": ["S001-Q001"]},
    )
    assert evaluate(routing_rows) == "Session Routing Loss"

    coverage_rows = _unselected_page_diagnostic_rows(checkpoint, routed, {"job": ["S001-Q001"]})
    assert evaluate(coverage_rows) == "Candidate Coverage Loss"
    assert (
        evaluate(
            _diagnostic_rows_from_pages(
                [page(threshold_filtered=True, threshold_passed=False, final_visible=False)], {"job": ["S001-Q001"]}
            )
        )
        == "Threshold Loss"
    )
    assert (
        evaluate(_diagnostic_rows_from_pages([page(ranking_loss=True, final_visible=None)], {"job": ["S001-Q001"]}))
        == "Ranking Loss"
    )
    assert (
        evaluate(
            _diagnostic_rows_from_pages(
                [page(final_visible=False, context_budget_filtered=True)], {"job": ["S001-Q001"]}
            )
        )
        == "Context Budget Loss"
    )


def test_adapter_candidate_pool_recall_is_pre_threshold_and_capped_final() -> None:
    pages = [
        {
            "id": f"p{index}",
            "source": "mid_term_page",
            "source_job_id": f"job{index}",
            "memory": "目标事实" if index == 5 else "无关",
            "raw_rag_score": 0.9 if index != 5 else 0.8,
            "final_score": 1.0 - index / 10,
            "rank_before_threshold": index + 1,
            "threshold_passed": index != 5,
            "threshold_filtered": index == 5,
            "final_rank": index + 1 if index != 5 else None,
            "final_visible": index < 2,
            "routed_candidate": True,
        }
        for index in range(6)
    ]
    rows = _diagnostic_rows_from_pages(pages, {f"job{index}": [f"T{index}"] for index in range(6)})
    assert len({row["page_id"] for row in rows}) == 6
    assert sum(bool(row["threshold_filtered"]) for row in rows) == 1
    assert sum(bool(row["final_visible"]) for row in rows) == 2
    assert max(int(row["rank_before_threshold"] or 0) for row in rows) == 6
    result = _evaluate_session(
        dataset=_dataset("目标事实"),
        session_id="S001",
        rankings={"S001-Q002": {"midterm": rows, "session_longterm": []}},
        k=1,
        target="all_memory",
        shortterm_window=0,
        max_total_pages=2,
        longterm_top_k=30,
    )
    metrics = result["metrics"]
    assert metrics["candidate_pool_recall"] >= metrics["post_threshold_recall"] >= metrics["final_context_recall"]
    assert metrics["candidate_pool_recall"] == 1.0


def test_diagnostic_only_pages_do_not_inflate_candidate_pool_count() -> None:
    result = _evaluate_session(
        dataset=_dataset("目标事实"),
        session_id="S001",
        rankings={
            "S001-Q002": {
                "midterm": [
                    {
                        "page_id": "in-pool",
                        "memory": "目标事实",
                        "in_candidate_pool": True,
                        "threshold_passed": True,
                        "final_visible": True,
                    },
                    {
                        "page_id": "diagnostic-only",
                        "memory": "其他事实",
                        "in_candidate_pool": False,
                        "diagnostic_only": True,
                        "final_visible": False,
                    },
                ],
                "session_longterm": [],
            }
        },
        k=1,
        target="all_memory",
        shortterm_window=0,
        max_total_pages=1,
        longterm_top_k=30,
    )
    assert result["metrics"]["candidate_pool_count"] == 1
    assert result["requirements"][0]["candidate_pool_count"] == 1


def test_ranking_loss_uses_diagnostic_depth_not_cache_depth() -> None:
    pages = [
        {
            "id": f"p{rank}",
            "source": "mid_term_page",
            "source_job_id": f"job-{rank}",
            "memory": "目标事实" if rank == 25 else "无关",
            "rank_before_threshold": rank,
            "threshold_passed": True,
            "threshold_filtered": False,
            "final_rank": rank,
            "final_visible": False,
            "routed_candidate": True,
        }
        for rank in range(1, 41)
    ]
    rows = _diagnostic_rows_from_pages(
        pages,
        {f"job-{rank}": [f"T{rank}"] for rank in range(1, 41)},
        ranking_depth=20,
    )
    gold = next(row for row in rows if row["page_id"] == "p25")
    assert gold["ranking_loss"] is True
    turn = _dataset("目标事实").sessions["S001"][1]
    facts, _ = _fact_rows_for_visible(
        turn,
        visible_rows=[{"memory": "无关可见内容"}],
        candidate_rows=rows,
        shortterm_rows=[],
        k=5,
        midterm_rows=rows,
    )
    assert facts[0]["failure_class"] == "Ranking Loss"


def test_max_total_pages_search_uses_benchmark_budget_not_a_fake_production_range() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    config = space["search"]["stages"]["cheap"]["retrieval"]["max_total_pages"]
    assert config == {"mode": "benchmark_context_budget", "refine_step": 1}


def test_production_config_is_the_only_parameter_authority() -> None:
    space = yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text())
    assert "parameters" not in space
    metadata = production_parameter_metadata("max_total_results")
    assert metadata.annotation is int
    assert metadata.default == 5
    assert metadata.ge == 1
    assert metadata.le == 5
    assert production_integer_candidates("max_total_results") == [1, 2, 3, 4, 5]

    page_values = production_literal_candidates("page_representation")
    assert page_values == list(get_args(MidTermMemoryConfig.model_fields["page_representation"].annotation))
    assert "production" in page_values
    assert "P8" in page_values

    rag_metadata = production_parameter_metadata("midterm_rag_threshold")
    assert rag_metadata.annotation is float
    assert rag_metadata.default == 0.1
    assert rag_metadata.ge == 0
    assert rag_metadata.le == 1
    assert validate_candidate_config({"midterm_rag_threshold": 0.05})["midterm_rag_threshold"] == 0.05
    with pytest.raises(ValueError):
        validate_candidate_config({"midterm_rag_threshold": 1.01})
    assert space["search"]["stages"]["cheap"]["retrieval"]["midterm_rag_threshold"]["coarse"]

    validate_candidate_config({"max_total_pages": 6})
    for name in ("top_k_sessions", "top_k_pages", "max_total_pages"):
        validate_candidate_config({name: 1})
        with pytest.raises(ValueError):
            validate_candidate_config({name: 0})
    with pytest.raises(ValueError):
        validate_candidate_config({"fusion_method": "invented"})


def test_dynamic_turn_and_heat_threshold_candidates() -> None:
    values = dynamic_turn_distance_candidates([2, 4, 7, 12, 30])
    assert values and all(value > 0 for value in values)
    assert promotion_threshold_candidates([1.0, 2.0, 5.0]) == [2.0]


def test_evolution_and_longterm_branches_are_staged_not_cartesian() -> None:
    source = Path(MidtermEvolutionBranch.generate.__code__.co_filename).read_text(encoding="utf-8")
    evolution = source[source.index("class MidtermEvolutionBranch") : source.index("class PromotionBranch")]
    assert 'cartesian_grid": False' in evolution
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


def test_stateful_replay_records_real_heat_distribution_fields() -> None:
    clock = {"value": 0}

    def add(_turn: str) -> None:
        clock["value"] += 1

    replay = WithinSessionStatefulReplay(
        search=lambda _turn, _index: [{"id": "page"}],
        add=add,
        current_turn_index=lambda: clock["value"],
        state_snapshot=lambda index: [
            {
                "session_id": "segment",
                "H_segment": 2.5 + index,
                "N_visit": 2,
                "L_interaction": 1.0,
                "R_recency": 0.8,
                "valid_recall_count": 3,
                "current_turn_index": index,
                "promotion_eligible": True,
            }
        ],
    )
    result = replay.replay(["Q1", "Q2"])
    assert [step.heat_states[0]["H_segment"] for step in result.steps] == [2.5, 3.5]
    assert all(step.promotion_events for step in result.steps)


def _promotion_context(tmp_path: Path, heat_values: list[float]) -> BranchContext:
    config_path = tmp_path / "memory_config.json"
    config_path.write_text(json.dumps({"midterm": {"short_term_capacity": 6}}), encoding="utf-8")
    checkpoints_path = tmp_path / "checkpoints.jsonl"
    checkpoints_path.write_text(
        "".join(
            json.dumps(
                {
                    "query_id": f"Q{index}",
                    "post_recall_heat_states": [
                        {
                            "session_id": "segment",
                            "H_segment": heat,
                            "N_visit": index,
                            "L_interaction": 1.0,
                            "R_recency": 0.5,
                            "valid_recall_count": 3,
                            "current_turn_index": index,
                            "promotion_eligible": True,
                        }
                    ],
                }
            )
            + "\n"
            for index, heat in enumerate(heat_values, start=1)
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "stateful_replay": True,
                "checkpoints_path": str(checkpoints_path),
                "memory_config_path": str(config_path),
                "llm_mode": "mock",
                "production_config": {"top_k_sessions": 5},
            }
        ),
        encoding="utf-8",
    )
    baseline = Candidate(
        "stateful",
        "tune",
        {
            "backend": "production_midterm",
            "manifest_paths": [str(manifest_path)],
            "manifest_sha256": {str(manifest_path.resolve()): sha256_file(manifest_path)},
            "promotion_min_recall_count": 3,
            "promotion_heat_threshold": 5.0,
        },
    )
    result = CandidateResult(
        name=baseline.name,
        candidate_hash="hash",
        stage=baseline.stage,
        config=baseline.config,
        metrics={"recall_at_k": 0.5},
        requirement_rows=[],
        session_rows=[],
        runtime_seconds=0,
        work_seconds=0,
        cache_hits=0,
        cache_misses=0,
    )
    return BranchContext(
        dataset=_dataset("目标事实"),
        baseline=baseline,
        anchor=baseline,
        anchor_result=result,
        diagnostic={"regime": "session_instability"},
        search_space=yaml.safe_load((Path(__file__).resolve().parents[2] / "search_space.yaml").read_text()),
        budget="deep",
        k=5,
        ranking_depth=20,
        tune_sessions=("S001",),
        registry=ArtifactRegistry(tmp_path / "cache", tmp_path / "legacy"),
        run_dir=tmp_path / "run",
        model_discovery=None,
        stage_index=2,
        execution_settings={"max_candidates_per_stage": 20, "remaining_expensive_candidates": 20},
    )


def test_promotion_uses_stateful_heat_quantiles_and_count_screening(tmp_path: Path) -> None:
    context = _promotion_context(tmp_path, [float(value) for value in range(1, 11)])
    outcome = PromotionBranch().generate(context)
    assert outcome.status == "READY"
    assert outcome.provenance["heat_thresholds"] == [5.0, 7.0, 9.0]
    labels = {candidate.name for candidate in outcome.candidates}
    assert any("recalls=2" in label for label in labels)
    assert any("heat=7.000" in label for label in labels)


def test_promotion_without_heat_does_not_create_zero_threshold(tmp_path: Path) -> None:
    outcome = PromotionBranch().generate(_promotion_context(tmp_path, []))
    assert outcome.status == "UNAVAILABLE_NO_HEAT_DISTRIBUTION"
    assert outcome.candidates == []


def test_midterm_source_config_deep_generates_real_source_specs(tmp_path: Path) -> None:
    context = _promotion_context(tmp_path, [2.0])
    branch = MidtermSourceConfigBranch()
    outcome = BranchRegistry([branch]).generate(branch, context)
    assert outcome.status == "READY"
    changed = {
        key
        for candidate in outcome.candidates
        for key in candidate.config.get("source_config_overrides", {}).get("midterm", {})
    }
    assert {"short_term_capacity", "session_similarity_threshold"} <= changed
    assert "top_k_sessions" not in changed
    assert all(candidate.config.get("source_generation_spec") for candidate in outcome.candidates)
    assert all(candidate.provenance.get("requires_source_regeneration") for candidate in outcome.candidates)


def test_stateful_replay_rejects_add_without_turn_progress() -> None:
    replay = WithinSessionStatefulReplay(
        search=lambda turn, index: [], add=lambda turn: None, current_turn_index=lambda: 0
    )
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


def test_orchestrator_executes_temporal_replay_without_gold(tmp_path: Path) -> None:
    baseline = Candidate("baseline", "baseline", {"backend": "production_midterm", "manifest_paths": []})
    result = run_cross_session_temporal_replay(
        dataset=_dataset("目标事实"),
        audit={"cross_session_gold_available": False},
        baseline=baseline,
        memory_config={
            "cross_session_retention_half_life_hours": 720.0,
            "cross_session_retention_floor": 0.2,
            "cross_session_reinforcement_gain": 0.25,
        },
        run_dir=tmp_path,
    )
    assert result["executed"] is True
    assert result["status"] == CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD
    assert result["structural_replay_status"] == STRUCTURAL_ONLY_NOT_EVALUATED
    assert result["production_defaults_unchanged"] is True
    assert result["winner_selection_enabled"] is False
    assert result["provenance"]["structural_probe"] is True
    assert {row["event"] for row in result["transitions"]} >= {
        "promotion",
        "decay",
        "valid_recall",
        "reinforcement",
    }
    assert (tmp_path / "temporal_replay.json").exists()


def test_cross_session_marker_without_temporal_schema_is_not_validated(tmp_path: Path) -> None:
    dataset = _dataset("目标事实")
    turns = list(dataset.sessions["S001"])
    turns[1] = Turn(
        **{
            **turns[1].__dict__,
            "dependency_type": "cross_session",
        }
    )
    dataset = Dataset(dataset.path, dataset.sha256, {"S001": tuple(turns)})
    result = run_cross_session_temporal_replay(
        dataset=dataset,
        audit={"cross_session_gold_available": True},
        baseline=Candidate("baseline", "baseline", {"backend": "production_midterm", "manifest_paths": []}),
        memory_config={},
        run_dir=tmp_path,
    )
    assert result["status"] == UNSUPPORTED_TEMPORAL_GOLD_SCHEMA
    assert result["winner_selection_enabled"] is False


@pytest.mark.parametrize(
    ("candidate_rows", "expected"),
    [
        ([{"memory": "无关内容", "routed_candidate": True}], "Session Routing Loss"),
        (
            [{"memory": "目标事实", "in_routed_pool": True, "in_candidate_pool": False}],
            "Candidate Coverage Loss",
        ),
        ([{"memory": "目标事实", "threshold_filtered": True}], "Threshold Loss"),
        ([{"memory": "目标事实", "rank_before_threshold": 9}], "Ranking Loss"),
        ([{"memory": "目标事实", "rank_before_threshold": 2, "final_rank": 2}], "Context Budget Loss"),
    ],
)
def test_failure_classes_are_distinct(candidate_rows: list[dict[str, object]], expected: str) -> None:
    turn = _dataset("目标事实").sessions["S001"][1]
    rows, _ = _fact_rows_for_visible(
        turn,
        visible_rows=[{"memory": "无关可见内容"}],
        candidate_rows=candidate_rows,
        shortterm_rows=[],
        k=5,
        midterm_rows=candidate_rows,
    )
    assert rows[0]["failure_class"] == expected


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


def test_tuner_fine_grained_replay_matches_production_retriever() -> None:
    from mem0.configs.base import FineGrainedLongTermConfig
    from mem0.memory.fine_grained_longterm import FineGrainedLongTermRetriever

    pool = {
        "semantic_candidates": [
            {"id": "a", "score": 0.9, "payload": {"data": "A", "run_id": "r1"}},
            {"id": "b", "score": 0.7, "payload": {"data": "B", "run_id": "r2"}},
        ],
        "current_session_candidate_ids": ["a"],
        "current_run_id": "r1",
        "bm25_scores": {"b": 1.0},
        "entity_boosts_by_threshold": {"0.5": {}},
    }
    checkpoint = {"query": "query", "longterm_candidate_pool": pool}
    config = {
        "longterm_top_k": 2,
        "longterm_rag_threshold": 0.1,
        "longterm_candidate_pool_multiplier": 4,
        "longterm_other_session_weight": 0.7,
        "longterm_hybrid_preset": "balanced",
        "entity_similarity_threshold": 0.5,
    }
    tuner_rows = ProductionMidtermAdapter._rank_session_longterm(checkpoint, config)

    production_config = FineGrainedLongTermConfig(
        top_k=2,
        semantic_weight=0.4,
        bm25_weight=0.4,
        entity_weight=0.2,
    )
    production = FineGrainedLongTermRetriever(
        vector_store=None,
        embedding_model=None,
        entity_store_provider=lambda: None,
        config=production_config,
    ).rank_frozen(
        "query",
        pool["semantic_candidates"],
        bm25_scores=pool["bm25_scores"],
        current_run_id="r1",
        current_session_candidate_ids=["a"],
        top_k=2,
        threshold=0.1,
    )

    assert [row["page_id"] for row in tuner_rows] == [row["id"] for row in production]
    assert [row["score"] for row in tuner_rows] == pytest.approx([row["score"] for row in production])
