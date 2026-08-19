from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .benchmark_support import redact_secrets
from .io_utils import stable_hash
from .models import Candidate, CandidateResult


RESEARCH_EVIDENCE_SCHEMA = "research_tune_evidence_v1"
_FORBIDDEN_KEY_MARKERS = ("validation", "gold", "answer", "future", "required_context")
# Candidate config keys the Research LLM may see. Execution, cache, path, and
# provenance-only fields stay out of the Tune evidence contract.
RESEARCH_CONFIG_KEYS = frozenset(
    {
        "retrieval_method",
        "retrieval_contract",
        "top_k_sessions",
        "top_k_pages",
        "max_total_pages",
        "bm25_language",
        "dense_weight",
        "query_representation",
        "query_prompt_text",
        "query_prompt_hash",
        "query_prompt_parent_hash",
        "query_prompt_generation_round",
        "query_optimization_direction",
        "query_artifact_variant",
        "page_representation",
        "embedding_model_id",
        "embedding_model_revision",
        "encoding_contract",
        "reranker_method",
        "reranker_dense_weight",
        "reranker_model_id",
        "reranker_model_revision",
        "field_weights",
        "page_summary_prompt_hash",
        "source_variant",
        "context_mode",
        "ablation_from_baseline",
    }
)


@dataclass(frozen=True)
class ResearchEvidence:
    payload: dict[str, Any]
    evidence_hash: str


def _safe(value: Any) -> Any:
    """Remove validation/label fields defensively before evidence reaches an LLM."""

    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if any(marker in lowered for marker in _FORBIDDEN_KEY_MARKERS):
                continue
            clean[key] = _safe(item)
        return redact_secrets(clean)
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return redact_secrets(value)


def _metric_summary(result: CandidateResult) -> dict[str, Any]:
    metrics = result.metrics
    return {
        "recall_at_k": metrics.get("recall_at_k"),
        "recall_at_2k": metrics.get("recall_at_2k"),
        "recall_at_4k": metrics.get("recall_at_4k"),
        "mrr": metrics.get("mrr"),
        "macro_session_recall_at_k": metrics.get("macro_session_recall_at_k"),
        "session_stddev": metrics.get("session_stddev"),
        "worst_session_recall_at_k": metrics.get("worst_session_recall_at_k"),
        "eligible_requirement_count": metrics.get("eligible_requirement_count"),
        "evaluated_query_count": metrics.get("evaluated_query_count"),
    }


def _research_config_view(config: Mapping[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in RESEARCH_CONFIG_KEYS if key in config}


def _config_diff(parent: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    parent_view = _research_config_view(parent)
    current_view = _research_config_view(current)
    keys = sorted(set(parent_view) | set(current_view))
    return _safe(
        {
            key: {"from": parent_view.get(key), "to": current_view.get(key)}
            for key in keys
            if parent_view.get(key) != current_view.get(key)
        }
    )


def _failure_distribution(result: CandidateResult, tune_sessions: Sequence[str]) -> dict[str, Any]:
    tune_scope = set(tune_sessions)
    rows = [row for row in result.requirement_rows if str(row.get("session_id") or "") in tune_scope]
    misses = [row for row in rows if not bool(row.get("hit_at_k"))]
    by_session: dict[str, dict[str, int]] = {}
    for session_id in tune_sessions:
        session_rows = [row for row in rows if str(row.get("session_id") or "") == session_id]
        session_misses = [row for row in session_rows if not bool(row.get("hit_at_k"))]
        by_session[str(session_id)] = {
            "eligible_requirements": len(session_rows),
            "missed_at_k": len(session_misses),
            "present_at_2k": sum(bool(row.get("hit_at_2k")) for row in session_misses),
            "present_at_4k": sum(bool(row.get("hit_at_4k")) for row in session_misses),
            "absent_at_4k": sum(not bool(row.get("hit_at_4k")) for row in session_misses),
        }
    return {
        "eligible_requirements": len(rows),
        "missed_at_k": len(misses),
        "present_at_2k": sum(bool(row.get("hit_at_2k")) for row in misses),
        "present_at_4k": sum(bool(row.get("hit_at_4k")) for row in misses),
        "absent_at_4k": sum(not bool(row.get("hit_at_4k")) for row in misses),
        "per_session": by_session,
    }


def _coverage_summary(value: Any) -> dict[str, Any]:
    coverage = value if isinstance(value, Mapping) else {}
    return {
        "diagnostic_regime": coverage.get("diagnostic_regime"),
        "relevant": coverage.get("relevant_branches") or [],
        "attempted": coverage.get("attempted_branches") or {},
        "exhausted": coverage.get("exhausted_branches") or [],
        "remaining": coverage.get("remaining_branches") or [],
        "revisitable": coverage.get("revisitable_branches") or [],
        "blocked": coverage.get("blocked_branches") or [],
    }


def _stage_summary(stage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage_index": stage.get("stage_index"),
        "status": stage.get("status"),
        "branches": stage.get("branches") or [],
        "candidate_count": stage.get("candidate_count"),
        "evaluated_candidates": stage.get("evaluated_candidates") or [],
        "best_candidate": stage.get("best_candidate"),
        "best_recall_at_k": stage.get("best_recall_at_k"),
        "improvement_pp": stage.get("improvement_pp"),
        # Anchor identity is carried separately. Canonicalize the remaining
        # near-tied frontier so parallel timing does not invalidate an otherwise
        # identical Research Decision cache entry.
        "frontier": sorted(str(item) for item in stage.get("frontier") or []),
        "diagnostic_before": stage.get("diagnostic_before"),
        "diagnostic_after": stage.get("diagnostic_after"),
        "screening": stage.get("screening"),
        "branch_rounds": [
            {
                "branch": outcome.get("branch"),
                "generation_round": outcome.get("generation_round"),
                "status": outcome.get("status"),
                "reason": outcome.get("reason"),
                "candidate_names": outcome.get("candidate_names") or [],
            }
            for outcome in stage.get("outcomes") or []
            if isinstance(outcome, Mapping)
        ],
        "coverage_before": _coverage_summary(stage.get("coverage_before")),
        "coverage_after": _coverage_summary(stage.get("coverage_after")),
        "patience_decision": stage.get("patience_decision"),
        "research_decision": stage.get("research_decision"),
    }


def build_research_evidence(
    *,
    dataset_sha256: str,
    tune_sessions: Sequence[str],
    k: int,
    stage_index: int,
    baseline: Candidate,
    baseline_result: CandidateResult,
    anchor: Candidate,
    anchor_result: CandidateResult,
    candidates: Mapping[str, Candidate],
    tune_results: Sequence[CandidateResult],
    stage_history: Sequence[Mapping[str, Any]],
    diagnostic: Mapping[str, Any],
    coverage: Mapping[str, Any],
    deterministic_plan: Sequence[Mapping[str, Any]],
    legal_actions: Sequence[Mapping[str, Any]],
    budget_state: Mapping[str, Any],
    deprioritized_history: Sequence[Mapping[str, Any]],
) -> ResearchEvidence:
    """Build exhaustive aggregate evidence without accepting any Validation input."""

    candidate_rows: list[dict[str, Any]] = []
    parent_by_hash = {stable_hash(candidate.config): candidate for candidate in candidates.values()}
    result_by_name = {result.name: result for result in tune_results}
    stage_summaries = [_stage_summary(stage) for stage in stage_history]
    round_by_candidate = {
        str(candidate_name): outcome.get("generation_round")
        for stage in stage_history
        for outcome in stage.get("outcomes") or []
        if isinstance(outcome, Mapping)
        for candidate_name in outcome.get("candidate_names") or []
    }
    for result in tune_results:
        candidate = candidates.get(result.name)
        if candidate is None:
            continue
        parent = parent_by_hash.get(str(candidate.config.get("parent_candidate_hash") or ""), baseline)
        parent_result = result_by_name.get(parent.name, baseline_result)
        candidate_rows.append(
            {
                "candidate": result.name,
                "stage": result.stage,
                "branch": candidate.config.get("experiment_branch"),
                "generation_round": (
                    candidate.config.get("query_prompt_generation_round") or round_by_candidate.get(result.name)
                ),
                "config_diff_from_parent": _config_diff(parent.config, candidate.config),
                "metrics": _metric_summary(result),
                "relative_anchor_improvement_pp": (
                    (
                        float(result.metrics.get("recall_at_k") or 0.0)
                        - float(parent_result.metrics.get("recall_at_k") or 0.0)
                    )
                    * 100.0
                ),
                "status": result.status,
                "complexity": result.complexity,
            }
        )
    payload = _safe(
        {
            "schema": RESEARCH_EVIDENCE_SCHEMA,
            "data_boundary": {
                "scope": "tune_sessions_only",
                "dataset_sha256": dataset_sha256,
                "tune_session_ids": sorted(str(item) for item in tune_sessions),
                "prohibited_inputs": [
                    "held_out_validation",
                    "future_turns",
                    "gold_dependencies",
                    "benchmark_answers",
                ],
            },
            "metric_contract": {"k": k, "primary": f"R@{k}", "deeper": [f"R@{2 * k}", f"R@{4 * k}"]},
            "decision_boundary": {"stage_index": stage_index, "trigger": "next_stage_branch_selection"},
            "baseline": {
                "candidate": baseline.name,
                "metrics": _metric_summary(baseline_result),
            },
            "experiment_history": candidate_rows,
            "stage_history": stage_summaries,
            "current_stage_results": stage_summaries[-1:],
            "frontier_anchor": {
                "candidate": anchor.name,
                "config_diff_from_baseline": _config_diff(baseline.config, anchor.config),
                "metrics": _metric_summary(anchor_result),
                "failure_distribution": _failure_distribution(anchor_result, tune_sessions),
            },
            "diagnosis": dict(diagnostic),
            "branch_state": {
                "relevant": coverage.get("relevant_branches") or [],
                "required": coverage.get("required_branches") or [],
                "selectable": coverage.get("selectable_branches") or [],
                "expensive_gated": coverage.get("expensive_gated_branches") or [],
                "attempted": coverage.get("attempted_branches") or {},
                "exhausted": coverage.get("exhausted_branches") or [],
                "revisitable": coverage.get("revisitable_branches") or [],
                "blocked": coverage.get("blocked_branches") or [],
                "unexplored": coverage.get("unexplored_branches") or [],
                "prior_deprioritized_events": list(deprioritized_history),
            },
            "deterministic_plan": list(deterministic_plan),
            "budget_and_hard_constraints": dict(budget_state),
            "legal_actions": list(legal_actions),
        }
    )
    return ResearchEvidence(payload=payload, evidence_hash=stable_hash(payload))
