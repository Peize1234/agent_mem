from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .candidate_selector import tune_frontier
from .experiment_branches import BranchContext, BranchRegistry
from .io_utils import stable_hash
from .models import Candidate, CandidateResult, Dataset
from .research_decision import ResearchDecisionEngine
from .research_evidence import build_research_evidence
from .research_policy import build_legal_actions, deterministic_plan_actions
from .parameter_schema import validate_candidate_config

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
    coverage_audit: dict[str, Any] = field(default_factory=dict)
    research_decisions: list[dict[str, Any]] = field(default_factory=list)
    research_stats: dict[str, int] = field(default_factory=dict)
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
    research_decider: ResearchDecisionEngine | None = None,
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
    exhaustion_reasons: dict[str, str] = {}
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
    coverage_policy = (search_space.get("search") or {}).get("branch_coverage") or {}
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
    research_decisions: list[dict[str, Any]] = []
    deprioritized_history: list[dict[str, Any]] = []

    def mark_exhausted(name: str, reason: str) -> None:
        exhausted.add(name)
        exhaustion_reasons[name] = reason

    def coverage_snapshot(regime: str) -> dict[str, Any]:
        return registry.coverage(
            regime=regime,
            max_cost_level=max_cost,
            attempt_counts=attempt_counts,
            exhausted=exhausted,
            exhaustion_reasons=exhaustion_reasons,
            enabled=enabled_branches,
            branch_settings=branch_settings,
            coverage_policy=coverage_policy,
            remaining_expensive_candidates=max(0, max_expensive_candidates - expensive_candidates_used),
            branch_history=branch_history,
        )

    def terminal_reason(snapshot: Mapping[str, Any]) -> str:
        if snapshot.get("blocked_branches"):
            return "resource_budget_exhausted"
        if any(
            str(row.get("reason") or "").startswith(("UNAVAILABLE:", "BUDGET_BLOCKED:"))
            for row in snapshot.get("exhausted_branches") or []
        ):
            return "resource_budget_exhausted"
        if snapshot.get("relevant_branches"):
            return "converged_after_relevant_branch_coverage"
        return "no_applicable_branch"

    def record_policy_event(
        *,
        stage_index: int,
        status: str,
        reason: str,
        snapshot: Mapping[str, Any],
    ) -> None:
        branch_events.append(
            {
                "stage_index": stage_index,
                "branch": "__search_policy__",
                "diagnostic_regime": snapshot.get("diagnostic_regime"),
                "status": status,
                "reason": reason,
                "candidate_names": [],
                "candidate_count": 0,
                "provenance": {"coverage": dict(snapshot)},
                "llm_calls": 0,
                "embedding_calls": 0,
                "reused_artifacts": [],
            }
        )

    diagnostic = diagnose(baseline_result)
    diagnostics.append({"after_stage": 0, **diagnostic})

    for stage_index in range(1, max_stages + 1):
        initial = stage_index == 1
        coverage_before = coverage_snapshot(str(diagnostic["regime"]))
        deterministic_selected = registry.select(
            regime=str(diagnostic["regime"]),
            max_cost_level=max_cost,
            attempt_counts=attempt_counts,
            exhausted=exhausted,
            initial_stage=initial,
            limit=max_branches,
            enabled=enabled_branches,
            branch_settings=branch_settings,
            coverage_policy=coverage_policy,
            remaining_expensive_candidates=max(0, max_expensive_candidates - expensive_candidates_used),
            exhaustion_reasons=exhaustion_reasons,
            branch_history=branch_history,
        )
        if not deterministic_selected:
            stop_reason = "no_applicable_branch" if initial else terminal_reason(coverage_before)
            break
        anchor_result = frontier[0] if frontier else _best(tune_results)
        anchor = candidate_by_name[anchor_result.name]
        selected = deterministic_selected
        stage_research_decision: dict[str, Any] | None = None
        if not initial and research_decider is not None:
            patience_triggered = no_improvement_stages >= patience
            convergence_triggered = frontier_convergence_stages >= patience
            legal_actions = build_legal_actions(
                registry=registry,
                coverage=coverage_before,
                deterministic_branches=deterministic_selected,
                attempt_counts=attempt_counts,
                max_branches_per_stage=max_branches,
                patience_triggered=patience_triggered,
                frontier_converged=convergence_triggered,
            )
            deterministic_plan = deterministic_plan_actions(deterministic_selected, legal_actions)
            evidence = build_research_evidence(
                dataset_sha256=dataset.sha256,
                tune_sessions=tune_sessions,
                k=k,
                stage_index=stage_index,
                baseline=baseline,
                baseline_result=baseline_result,
                anchor=anchor,
                anchor_result=anchor_result,
                candidates=candidate_by_name,
                tune_results=tune_results,
                stage_history=history,
                diagnostic=diagnostic,
                coverage=coverage_before,
                deterministic_plan=deterministic_plan,
                legal_actions=[action.serializable() for action in legal_actions],
                budget_state={
                    "budget": budget,
                    "stage_index": stage_index,
                    "max_stages": max_stages,
                    "max_cost_level": max_cost,
                    "max_branches_per_stage": max_branches,
                    "max_candidates_per_stage": max_candidates,
                    "remaining_expensive_candidates": max(0, max_expensive_candidates - expensive_candidates_used),
                    "no_improvement_stages": no_improvement_stages,
                    "patience_stages": patience,
                    "frontier_convergence_stages": frontier_convergence_stages,
                    "branch_registry": registry.describe(),
                    "branch_settings": branch_settings,
                },
                deprioritized_history=deprioritized_history,
            )
            decision = research_decider.decide(
                stage_index=stage_index,
                evidence=evidence,
                legal_actions=legal_actions,
                deterministic_plan=deterministic_plan,
                max_actions=max_branches,
                identity_context={
                    "dataset_sha256": dataset.sha256,
                    "tune_scope": sorted(tune_sessions),
                    "anchor_candidate_hash": anchor_result.candidate_hash,
                    "anchor_config_hash": candidate_config_hash(anchor.config),
                    "branch_registry_state_hash": stable_hash(
                        {
                            "registry": registry.describe(),
                            "settings": branch_settings,
                            "coverage_policy": coverage_policy,
                            "attempt_counts": attempt_counts,
                            "exhausted": sorted(exhausted),
                        }
                    ),
                },
            )
            stage_research_decision = decision.serializable()
            research_decisions.append(stage_research_decision)
            selected = [
                registry.get(str(action.branch))
                for action in decision.selected_actions
                if action.action_type == "branch" and action.branch is not None
            ]
            legal_by_id = {action.action_id: action for action in legal_actions}
            stage_deprioritized: list[dict[str, Any]] = []
            for item in decision.deprioritized:
                action = legal_by_id[str(item["action_id"])]
                if action.action_type != "branch" or action.branch is None:
                    continue
                record = {
                    "stage_index": stage_index,
                    "decision_id": decision.decision_id,
                    "branch": action.branch,
                    "generation_round": action.generation_round,
                    "status": "DEPRIORITIZED",
                    "reason": item["reason"],
                    "evidence_hash": decision.evidence_hash,
                }
                stage_deprioritized.append(record)
                deprioritized_history.append(record)
                skipped.append(record)
            branch_events.append(
                {
                    "stage_index": stage_index,
                    "branch": "__research_decision__",
                    "diagnostic_regime": str(diagnostic["regime"]),
                    "status": "DETERMINISTIC_FALLBACK" if decision.fallback_used else "VALIDATED",
                    "reason": decision.rationale,
                    "candidate_names": [],
                    "candidate_count": 0,
                    "provenance": {
                        "decision": stage_research_decision,
                        "deprioritized": stage_deprioritized,
                        "coverage": coverage_before,
                    },
                    "llm_calls": 0 if decision.cache_hit else decision.attempt_count,
                    "embedding_calls": 0,
                    "reused_artifacts": [decision.decision_id] if decision.cache_hit else [],
                }
            )
            if decision.stop_action is not None:
                stop_reason = str(decision.stop_action.stop_reason or "frontier_converged")
                history.append(
                    {
                        "stage_index": stage_index,
                        "diagnostic_before": dict(diagnostic),
                        "branches": [],
                        "outcomes": [],
                        "candidate_count": 0,
                        "improvement_pp": 0.0,
                        "status": "RESEARCH_SELECTED_STOP",
                        "stop_reason": stop_reason,
                        "coverage_before": coverage_before,
                        "research_decision": stage_research_decision,
                    }
                )
                break
        stage_candidates: list[Candidate] = []
        stage_outcomes: list[dict[str, Any]] = []
        candidate_branches: dict[str, str] = {}
        stage_expensive_generated = 0
        executed_branches: list[str] = []
        for branch in selected:
            next_round = int(attempt_counts.get(branch.spec.name, 0)) + 1
            remaining_expensive = max(
                0, max_expensive_candidates - expensive_candidates_used - stage_expensive_generated
            )
            if branch.spec.cost_level in {"high", "expensive"} and remaining_expensive == 0:
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
                    "generation_round": next_round,
                }
                branch_events.append(event)
                stage_outcomes.append(event)
                skipped.append({"branch": branch.spec.name, "reason": event["reason"], "status": event["status"]})
                continue
            attempt_counts[branch.spec.name] = next_round
            executed_branches.append(branch.spec.name)
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
            legal_candidates: list[Candidate] = []
            rejected_candidates: list[str] = []
            for candidate in outcome.candidates:
                try:
                    validate_candidate_config(candidate.config)
                except ValueError as exc:
                    rejected_candidates.append(f"{candidate.name}: {exc}")
                    continue
                legal_candidates.append(candidate)
            outcome.candidates = legal_candidates
            if rejected_candidates:
                outcome.reason = "; ".join(filter(None, [outcome.reason, *rejected_candidates]))
                if not outcome.candidates:
                    outcome.status = "INVALID"
            event = outcome.event(stage_index=stage_index, diagnostic_regime=str(diagnostic["regime"]))
            event["generation_round"] = attempt_counts[branch.spec.name]
            event["effective_config_hashes"] = [
                candidate_config_hash(candidate.config) for candidate in outcome.candidates
            ]
            branch_events.append(event)
            stage_outcomes.append(event)
            total_llm_calls += outcome.llm_calls
            total_embedding_calls += outcome.embedding_calls
            reused_artifacts.update(outcome.reused_artifacts)
            if outcome.status != "READY":
                skipped.append({"branch": branch.spec.name, "reason": outcome.reason, "status": outcome.status})
                mark_exhausted(
                    branch.spec.name,
                    f"{outcome.status}: {outcome.reason or 'branch produced no executable Candidate'}",
                )
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
                    "coverage_before": coverage_before,
                    "research_decision": stage_research_decision,
                }
            )
            no_improvement_stages += 1
            for name in executed_branches:
                mark_exhausted(name, "no unique executable Candidate was generated")
            coverage_after = coverage_snapshot(str(diagnostic["regime"]))
            history[-1]["coverage_after"] = coverage_after
            if no_improvement_stages >= patience:
                if coverage_after["remaining_branches"]:
                    history[-1]["patience_decision"] = "patience_soft_exhausted"
                    record_policy_event(
                        stage_index=stage_index,
                        status=(
                            "PATIENCE_DEFERRED_TO_RESEARCH"
                            if research_decider is not None
                            else "PATIENCE_SOFT_EXHAUSTED"
                        ),
                        reason="no executable Candidate, but relevant Branch coverage remains",
                        snapshot=coverage_after,
                    )
                    if research_decider is None:
                        no_improvement_stages = 0
                        frontier_convergence_stages = 0
                else:
                    stop_reason = terminal_reason(coverage_after)
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
                    "coverage_before": coverage_before,
                    "research_decision": stage_research_decision,
                }
            )
            no_improvement_stages += 1
            for name in executed_branches:
                mark_exhausted(name, "all generated Candidates were pruned by Tune screening")
            coverage_after = coverage_snapshot(str(diagnostic["regime"]))
            history[-1]["coverage_after"] = coverage_after
            if no_improvement_stages >= patience:
                if coverage_after["remaining_branches"]:
                    history[-1]["patience_decision"] = "patience_soft_exhausted"
                    record_policy_event(
                        stage_index=stage_index,
                        status=(
                            "PATIENCE_DEFERRED_TO_RESEARCH"
                            if research_decider is not None
                            else "PATIENCE_SOFT_EXHAUSTED"
                        ),
                        reason="screening found no survivor, but relevant Branch coverage remains",
                        snapshot=coverage_after,
                    )
                    if research_decider is None:
                        no_improvement_stages = 0
                        frontier_convergence_stages = 0
                else:
                    stop_reason = terminal_reason(coverage_after)
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
                "coverage_before": coverage_before,
                "research_decision": stage_research_decision,
            }
        )
        valid_by_name = {result.name: result for result in stage_results if result.status == "VALID"}
        for branch in selected:
            if branch.spec.name not in executed_branches:
                continue
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
                "effective_config_hashes": sorted(
                    {
                        candidate_config_hash(candidate_by_name[name].config)
                        for name, branch_name in candidate_branches.items()
                        if branch_name == branch.spec.name and name in candidate_by_name
                    }
                ),
                "frontier_winner": best_branch is not None and frontier and best_branch.name == frontier[0].name,
            }
            branch_history.setdefault(branch.spec.name, []).append(round_record)
            if not branch_results or branch_improvement_pp < min_improvement_pp:
                mark_exhausted(
                    branch.spec.name,
                    "no valid Candidate met min_improvement_pp "
                    f"({branch_improvement_pp:+.3f} < {min_improvement_pp:+.3f})",
                )
            if branch.spec.name in {"QueryRepresentation", "QueryRewritePrompt"} and (
                best_branch is None
                or branch_improvement_pp < min_improvement_pp
                or not any(result.name == best_branch.name for result in frontier)
            ):
                mark_exhausted(
                    branch.spec.name,
                    "Query round had no qualifying frontier improvement; later rounds are not meaningful",
                )
        previous_best = current_best
        previous_frontier_signature = signature
        coverage_after = coverage_snapshot(str(diagnostic["regime"]))
        history[-1]["coverage_after"] = coverage_after
        if diagnostic["regime"] == "data_artifact_suspicion":
            stop_reason = "data_artifact_suspicion"
            break
        convergence_triggered = frontier_convergence_stages >= patience
        patience_triggered = no_improvement_stages >= patience
        if convergence_triggered or patience_triggered:
            if coverage_after["remaining_branches"]:
                trigger = "frontier convergence" if convergence_triggered else "global patience"
                history[-1]["patience_decision"] = "patience_soft_exhausted"
                record_policy_event(
                    stage_index=stage_index,
                    status=(
                        "PATIENCE_DEFERRED_TO_RESEARCH" if research_decider is not None else "PATIENCE_SOFT_EXHAUSTED"
                    ),
                    reason=f"{trigger} reached, but meaningful relevant Branch work remains",
                    snapshot=coverage_after,
                )
                if research_decider is None:
                    no_improvement_stages = 0
                    frontier_convergence_stages = 0
            elif convergence_triggered:
                history[-1]["patience_decision"] = "hard_stop_after_coverage"
                stop_reason = "frontier_converged"
                break
            else:
                history[-1]["patience_decision"] = "hard_stop_after_coverage"
                stop_reason = terminal_reason(coverage_after)
                break
    else:
        stop_reason = "stage_budget_exhausted"

    final_coverage = coverage_snapshot(str(diagnostic["regime"]))
    final_coverage["deprioritized_events"] = deprioritized_history
    record_policy_event(
        stage_index=len(history),
        status="SEARCH_STOP",
        reason=stop_reason,
        snapshot=final_coverage,
    )
    attempted_specs = {branch.spec.name: branch.spec for branch in registry.ordered()}
    final_relevant = set(final_coverage["relevant_branches"])
    final_blocked = {row["branch"]: row["reason"] for row in final_coverage["blocked_branches"]}
    for name in attempted_specs:
        if not attempt_counts.get(name):
            if name not in final_relevant:
                reason = f"not relevant to final diagnostic={diagnostic['regime']}"
                status = "NOT_RELEVANT"
            elif name in final_blocked:
                reason = final_blocked[name]
                status = "BUDGET_OR_RESOURCE_BLOCKED"
            else:
                reason = f"remaining when search stopped: {stop_reason}"
                status = "NOT_ATTEMPTED"
            skipped.append(
                {
                    "branch": name,
                    "status": status,
                    "reason": reason,
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
        coverage_audit=final_coverage,
        research_decisions=research_decisions,
        research_stats=(research_decider.stats.serializable() if research_decider is not None else {}),
        llm_calls=total_llm_calls,
        embedding_calls=total_embedding_calls,
        reused_artifacts=sorted(reused_artifacts),
    )
