from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .candidate_selector import tune_frontier
from .experiment_branches import BranchContext, BranchRegistry
from .io_utils import stable_hash
from .models import Candidate, CandidateResult, Dataset


EvaluateCandidates = Callable[[Sequence[Candidate], Sequence[str] | None, str], list[CandidateResult]]
Diagnose = Callable[[CandidateResult], dict[str, Any]]


@dataclass
class StageSearchResult:
    candidates: dict[str, Candidate]
    tune_results: list[CandidateResult]
    frontier: list[CandidateResult]
    diagnostics: list[dict[str, Any]]
    stage_history: list[dict[str, Any]]
    branch_events: list[dict[str, Any]]
    skipped_branches: list[dict[str, Any]]
    stop_reason: str
    llm_calls: int = 0
    embedding_calls: int = 0
    reused_artifacts: list[str] = field(default_factory=list)


def _metric(result: CandidateResult) -> float:
    return float(result.metrics.get("recall_at_k") or 0.0)


def _best(results: Sequence[CandidateResult]) -> CandidateResult:
    valid = [result for result in results if result.status == "VALID"]
    if not valid:
        raise RuntimeError("Search stage produced no valid Candidate result")
    return max(
        valid,
        key=lambda result: (
            _metric(result),
            float(result.metrics.get("macro_session_recall_at_k") or 0.0),
            -float(result.metrics.get("session_stddev") or 0.0),
            float(result.metrics.get("mrr") or 0.0),
            -result.complexity,
        ),
    )


def _dedupe(candidates: Sequence[Candidate], seen_hashes: set[str]) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in candidates:
        key = stable_hash(candidate.config)
        if key not in seen_hashes:
            seen_hashes.add(key)
            result.append(candidate)
    return result


def _screen_expensive(
    candidates: Sequence[Candidate],
    *,
    baseline: Candidate,
    tune_sessions: Sequence[str],
    screening_session_count: int,
    tolerance_pp: float,
    evaluate: EvaluateCandidates,
    stage_index: int,
) -> tuple[list[Candidate], dict[str, Any]]:
    screening = tuple(tune_sessions[: max(1, min(screening_session_count, len(tune_sessions)))])
    if not candidates or not screening:
        return list(candidates), {"screening": "NOT_REQUIRED"}
    results = evaluate(
        [baseline, *candidates],
        screening,
        f"stage_{stage_index}_screening",
    )
    baseline_result = next(result for result in results if result.name == baseline.name)
    floor = _metric(baseline_result) - tolerance_pp / 100.0
    promoted_names = {
        result.name
        for result in results
        if result.name != baseline.name and result.status == "VALID" and _metric(result) >= floor
    }
    promoted = [candidate for candidate in candidates if candidate.name in promoted_names]
    return promoted, {
        "screening": "COMPLETE",
        "sessions": list(screening),
        "baseline_recall_at_k": _metric(baseline_result),
        "evaluated": [result.name for result in results if result.name != baseline.name],
        "promoted": [candidate.name for candidate in promoted],
        "eliminated": [candidate.name for candidate in candidates if candidate.name not in promoted_names],
    }


def run_staged_search(
    *,
    dataset: Dataset,
    baseline: Candidate,
    baseline_result: CandidateResult,
    tune_sessions: Sequence[str],
    registry: BranchRegistry,
    artifact_registry: Any,
    model_discovery: Any,
    run_dir: Any,
    search_space: Mapping[str, Any],
    budget: str,
    profile: Mapping[str, Any],
    k: int,
    ranking_depth: int,
    evaluate: EvaluateCandidates,
    diagnose: Diagnose,
) -> StageSearchResult:
    """Run staged successive filtering strictly on Tune Sessions."""
    max_stages = max(1, int(profile.get("max_stages", 3)))
    max_cost = str(profile.get("max_cost_level") or "medium")
    max_branches = max(1, int(profile.get("max_branches_per_stage", 1)))
    max_candidates = max(1, int(profile.get("max_candidates_per_stage") or profile.get("max_cheap_candidates") or 12))
    screening_sessions = max(1, int(profile.get("screening_sessions", 1)))
    tolerance_pp = float((search_space.get("selection") or {}).get("tie_tolerance_pp", 0.25))
    min_improvement_pp = float((search_space.get("selection") or {}).get("min_improvement_pp", 0.25))
    patience = max(1, int((search_space.get("selection") or {}).get("patience_stages", 2)))
    frontier_limit = max(2, int(profile.get("tune_frontier", profile.get("validation_frontier", 3))))

    candidate_by_name = {baseline.name: baseline}
    tune_results = [baseline_result]
    frontier = [baseline_result]
    diagnostics: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    branch_events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    attempted: set[str] = set()
    seen_hashes = {stable_hash(baseline.config)}
    no_improvement_stages = 0
    previous_best = _metric(baseline_result)
    previous_frontier_signature: tuple[str, ...] = ()
    total_llm_calls = 0
    total_embedding_calls = 0
    reused_artifacts: set[str] = set()
    stop_reason = "stage_budget_exhausted"
    branch_settings = (search_space.get("search") or {}).get("branch_registry") or {}
    enabled_branches = (
        {
            str(name)
            for name, settings in branch_settings.items()
            if not isinstance(settings, Mapping) or settings.get("enabled", True) is not False
        }
        if branch_settings
        else None
    )

    diagnostic = diagnose(baseline_result)
    diagnostics.append({"after_stage": 0, **diagnostic})

    for stage_index in range(1, max_stages + 1):
        initial = stage_index == 1
        selected = registry.select(
            regime=str(diagnostic["regime"]),
            max_cost_level=max_cost,
            attempted=attempted,
            initial_stage=initial,
            limit=max_branches,
            enabled=enabled_branches,
        )
        if not selected:
            stop_reason = (
                "no_budget_eligible_initial_branch" if initial else "no_applicable_branch_within_resource_budget"
            )
            break
        anchor_result = frontier[0] if frontier else _best(tune_results)
        anchor = candidate_by_name[anchor_result.name]
        stage_candidates: list[Candidate] = []
        stage_outcomes: list[dict[str, Any]] = []
        for branch in selected:
            attempted.add(branch.spec.name)
            context = BranchContext(
                dataset=dataset,
                baseline=baseline,
                anchor=anchor,
                anchor_result=anchor_result,
                diagnostic=diagnostic,
                search_space=search_space,
                budget=budget,
                k=k,
                ranking_depth=ranking_depth,
                tune_sessions=tuple(tune_sessions),
                registry=artifact_registry,
                run_dir=run_dir,
                model_discovery=model_discovery,
                stage_index=stage_index,
            )
            outcome = registry.generate(branch, context)
            event = outcome.event(stage_index=stage_index, diagnostic_regime=str(diagnostic["regime"]))
            branch_events.append(event)
            stage_outcomes.append(event)
            total_llm_calls += outcome.llm_calls
            total_embedding_calls += outcome.embedding_calls
            reused_artifacts.update(outcome.reused_artifacts)
            if outcome.status != "READY":
                skipped.append({"branch": branch.spec.name, "reason": outcome.reason, "status": outcome.status})
            stage_candidates.extend(outcome.candidates)
        stage_candidates = _dedupe(stage_candidates, seen_hashes)[:max_candidates]
        if not stage_candidates:
            history.append(
                {
                    "stage_index": stage_index,
                    "diagnostic_before": dict(diagnostic),
                    "branches": [branch.spec.name for branch in selected],
                    "outcomes": stage_outcomes,
                    "candidate_count": 0,
                    "improvement_pp": 0.0,
                    "status": "NO_EXECUTABLE_CANDIDATE",
                }
            )
            no_improvement_stages += 1
            if no_improvement_stages >= patience:
                stop_reason = "patience_exhausted_no_executable_candidates"
                break
            continue

        costly = [
            candidate
            for candidate in stage_candidates
            if str(candidate.config.get("branch_cost_level")) in {"high", "expensive"}
        ]
        cheap = [candidate for candidate in stage_candidates if candidate not in costly]
        screening_metadata: dict[str, Any] = {"screening": "NOT_REQUIRED"}
        if costly:
            promoted, screening_metadata = _screen_expensive(
                costly,
                baseline=baseline,
                tune_sessions=tune_sessions,
                screening_session_count=screening_sessions,
                tolerance_pp=tolerance_pp,
                evaluate=evaluate,
                stage_index=stage_index,
            )
            stage_candidates = [*cheap, *promoted]
        if not stage_candidates:
            history.append(
                {
                    "stage_index": stage_index,
                    "diagnostic_before": dict(diagnostic),
                    "branches": [branch.spec.name for branch in selected],
                    "outcomes": stage_outcomes,
                    "candidate_count": 0,
                    "screening": screening_metadata,
                    "improvement_pp": 0.0,
                    "status": "ALL_CANDIDATES_PRUNED_BY_SCREENING",
                }
            )
            no_improvement_stages += 1
            if no_improvement_stages >= patience:
                stop_reason = "patience_exhausted_after_screening"
                break
            continue

        for candidate in stage_candidates:
            candidate_by_name[candidate.name] = candidate
        stage_results = evaluate(stage_candidates, None, f"stage_{stage_index}_tune")
        tune_results.extend(stage_results)
        frontier = tune_frontier(tune_results, tolerance_pp=tolerance_pp, limit=frontier_limit)
        current_best = _metric(_best(tune_results))
        improvement_pp = (current_best - previous_best) * 100.0
        signature = tuple(result.name for result in frontier)
        converged = bool(signature and signature == previous_frontier_signature)
        no_improvement_stages = no_improvement_stages + 1 if improvement_pp < min_improvement_pp else 0
        best_stage = _best(tune_results)
        diagnostic = diagnose(best_stage)
        diagnostics.append({"after_stage": stage_index, **diagnostic})
        history.append(
            {
                "stage_index": stage_index,
                "diagnostic_before": diagnostics[-2],
                "branches": [branch.spec.name for branch in selected],
                "outcomes": stage_outcomes,
                "candidate_count": len(stage_candidates),
                "evaluated_candidates": [candidate.name for candidate in stage_candidates],
                "screening": screening_metadata,
                "best_candidate": best_stage.name,
                "best_recall_at_k": current_best,
                "improvement_pp": improvement_pp,
                "frontier": list(signature),
                "diagnostic_after": dict(diagnostic),
                "status": "COMPLETE",
            }
        )
        previous_best = current_best
        previous_frontier_signature = signature
        if diagnostic["regime"] == "data_artifact_suspicion":
            stop_reason = "dataset_quality_diagnostic_suppressed_further_search"
            break
        if converged:
            stop_reason = "frontier_converged"
            break
        if no_improvement_stages >= patience:
            stop_reason = "min_improvement_patience_exhausted"
            break
    else:
        stop_reason = "stage_budget_exhausted"

    attempted_specs = {branch.spec.name: branch.spec for branch in registry.ordered()}
    for name, spec in attempted_specs.items():
        if name not in attempted:
            skipped.append(
                {
                    "branch": name,
                    "status": "NOT_SELECTED",
                    "reason": (
                        f"not selected before stop={stop_reason}; cost={spec.cost_level}; "
                        f"regimes={sorted(spec.diagnostic_regimes)}"
                    ),
                }
            )
    return StageSearchResult(
        candidates=candidate_by_name,
        tune_results=tune_results,
        frontier=frontier,
        diagnostics=diagnostics,
        stage_history=history,
        branch_events=branch_events,
        skipped_branches=skipped,
        stop_reason=stop_reason,
        llm_calls=total_llm_calls,
        embedding_calls=total_embedding_calls,
        reused_artifacts=sorted(reused_artifacts),
    )
