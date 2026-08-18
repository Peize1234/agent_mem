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


def candidate_config_hash(config: Mapping[str, Any]) -> str:
    bookkeeping = {"experiment_branch", "branch_cost_level", "parent_candidate_hash", "applied_branches"}
    return stable_hash({key: value for key, value in config.items() if key not in bookkeeping})


def _dedupe(candidates: Sequence[Candidate], seen_hashes: set[str]) -> list[Candidate]:
    result: list[Candidate] = []
    for candidate in candidates:
        key = candidate_config_hash(candidate.config)
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
        "llm_calls": sum(result.llm_calls for result in results),
        "embedding_calls": sum(result.embedding_calls for result in results),
        "reused_artifacts": sorted(
            {artifact for result in results for artifact in result.reused_artifacts if artifact}
        ),
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
    execution_settings: Mapping[str, Any] | None = None,
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
    configured_expensive_limit = profile.get("max_expensive_candidates")
    max_expensive_candidates = (
        max_candidates if configured_expensive_limit is None else max(0, int(configured_expensive_limit))
    )

    candidate_by_name = {baseline.name: baseline}
    tune_results = [baseline_result]
    frontier = [baseline_result]
    diagnostics: list[dict[str, Any]] = []
    history: list[dict[str, Any]] = []
    branch_events: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    attempt_counts: dict[str, int] = {}
    exhausted: set[str] = set()
    branch_history: dict[str, list[dict[str, Any]]] = {}
    seen_hashes = {candidate_config_hash(baseline.config)}
    no_improvement_stages = 0
    previous_best = _metric(baseline_result)
    previous_frontier_signature: tuple[str, ...] = ()
    frontier_convergence_stages = 0
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
    expensive_candidates_used = 0

    diagnostic = diagnose(baseline_result)
    diagnostics.append({"after_stage": 0, **diagnostic})

    for stage_index in range(1, max_stages + 1):
        initial = stage_index == 1
        selected = registry.select(
            regime=str(diagnostic["regime"]),
            max_cost_level=max_cost,
            attempt_counts=attempt_counts,
            exhausted=exhausted,
            initial_stage=initial,
            limit=max_branches,
            enabled=enabled_branches,
            branch_settings=branch_settings,
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
        candidate_branches: dict[str, str] = {}
        stage_expensive_generated = 0
        for branch in selected:
            attempt_counts[branch.spec.name] = int(attempt_counts.get(branch.spec.name, 0)) + 1
            remaining_expensive = max(
                0, max_expensive_candidates - expensive_candidates_used - stage_expensive_generated
            )
            if branch.spec.cost_level in {"high", "expensive"} and remaining_expensive == 0:
                exhausted.add(branch.spec.name)
                event = {
                    "stage_index": stage_index,
                    "branch": branch.spec.name,
                    "diagnostic_regime": str(diagnostic["regime"]),
                    "status": "BUDGET_BLOCKED",
                    "reason": "max_expensive_candidates exhausted",
                    "candidate_names": [],
                    "candidate_count": 0,
                    "provenance": {},
                    "llm_calls": 0,
                    "embedding_calls": 0,
                    "reused_artifacts": [],
                    "generation_round": attempt_counts[branch.spec.name],
                }
                branch_events.append(event)
                stage_outcomes.append(event)
                skipped.append({"branch": branch.spec.name, "reason": event["reason"], "status": event["status"]})
                continue
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
                generation_round=attempt_counts[branch.spec.name],
                branch_history=tuple(branch_history.get(branch.spec.name) or []),
                execution_settings={
                    **dict(execution_settings or {}),
                    "remaining_expensive_candidates": remaining_expensive,
                },
            )
            outcome = registry.generate(branch, context)
            event = outcome.event(stage_index=stage_index, diagnostic_regime=str(diagnostic["regime"]))
            event["generation_round"] = attempt_counts[branch.spec.name]
            branch_events.append(event)
            stage_outcomes.append(event)
            total_llm_calls += outcome.llm_calls
            total_embedding_calls += outcome.embedding_calls
            reused_artifacts.update(outcome.reused_artifacts)
            if outcome.status != "READY":
                skipped.append({"branch": branch.spec.name, "reason": outcome.reason, "status": outcome.status})
                if outcome.status in {"EXHAUSTED", "BUDGET_BLOCKED", "NOT_TRIGGERED"}:
                    exhausted.add(branch.spec.name)
            for candidate in outcome.candidates:
                candidate_branches[candidate.name] = branch.spec.name
            if branch.spec.cost_level in {"high", "expensive"}:
                stage_expensive_generated += len(outcome.candidates)
            stage_candidates.extend(outcome.candidates)
        stage_candidates = _dedupe(stage_candidates, seen_hashes)[:max_candidates]
        if max_expensive_candidates >= 0:
            inexpensive = [
                candidate
                for candidate in stage_candidates
                if str(candidate.config.get("branch_cost_level")) not in {"high", "expensive"}
            ]
            expensive = [candidate for candidate in stage_candidates if candidate not in inexpensive]
            allowance = max(0, max_expensive_candidates - expensive_candidates_used)
            stage_candidates = [*inexpensive, *expensive[:allowance]]
            expensive_candidates_used += min(len(expensive), allowance)
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
            exhausted.update(branch.spec.name for branch in selected)
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
                baseline=anchor,
                tune_sessions=tune_sessions,
                screening_session_count=screening_sessions,
                tolerance_pp=tolerance_pp,
                evaluate=evaluate,
                stage_index=stage_index,
            )
            total_llm_calls += int(screening_metadata.get("llm_calls") or 0)
            total_embedding_calls += int(screening_metadata.get("embedding_calls") or 0)
            reused_artifacts.update(screening_metadata.get("reused_artifacts") or [])
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
            exhausted.update(branch.spec.name for branch in selected)
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
        frontier_convergence_stages = frontier_convergence_stages + 1 if converged else 0
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
        valid_by_name = {result.name: result for result in stage_results if result.status == "VALID"}
        for branch in selected:
            branch_results = [
                valid_by_name[name]
                for name, branch_name in candidate_branches.items()
                if branch_name == branch.spec.name and name in valid_by_name
            ]
            best_branch = _best(branch_results) if branch_results else None
            branch_improvement_pp = (
                (_metric(best_branch) - _metric(anchor_result)) * 100.0 if best_branch is not None else 0.0
            )
            round_record = {
                "generation_round": attempt_counts[branch.spec.name],
                "anchor": anchor.name,
                "best_candidate": best_branch.name if best_branch is not None else None,
                "improvement_pp": branch_improvement_pp,
                "candidate_count": len(branch_results),
                "frontier_winner": best_branch is not None and frontier and best_branch.name == frontier[0].name,
            }
            branch_history.setdefault(branch.spec.name, []).append(round_record)
            if not branch_results or branch_improvement_pp < min_improvement_pp:
                exhausted.add(branch.spec.name)
            if branch.spec.name == "QueryRepresentation" and (
                best_branch is None
                or branch_improvement_pp < min_improvement_pp
                or not any(result.name == best_branch.name for result in frontier)
            ):
                exhausted.add(branch.spec.name)
        previous_best = current_best
        previous_frontier_signature = signature
        if diagnostic["regime"] == "data_artifact_suspicion":
            stop_reason = "dataset_quality_diagnostic_suppressed_further_search"
            break
        if frontier_convergence_stages >= patience:
            stop_reason = "frontier_converged"
            break
        if no_improvement_stages >= patience:
            stop_reason = "min_improvement_patience_exhausted"
            break
    else:
        stop_reason = "stage_budget_exhausted"

    attempted_specs = {branch.spec.name: branch.spec for branch in registry.ordered()}
    for name, spec in attempted_specs.items():
        if not attempt_counts.get(name):
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
