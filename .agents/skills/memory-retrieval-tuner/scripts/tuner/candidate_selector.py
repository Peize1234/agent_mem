from __future__ import annotations

from typing import Any, Sequence

from .models import CandidateResult


def _metric(result: CandidateResult, name: str) -> float:
    return float(result.metrics.get(name) or 0.0)


def _cost(result: CandidateResult, name: str, fallback: int) -> int:
    value = result.metrics.get(name)
    return int(value) if value is not None else fallback


def tune_frontier(results: Sequence[CandidateResult], *, tolerance_pp: float, limit: int) -> list[CandidateResult]:
    valid = [result for result in results if result.status == "VALID"]
    if not valid:
        return []
    best = max(_metric(result, "recall_at_k") for result in valid)
    tolerance = tolerance_pp / 100.0
    frontier = [result for result in valid if best - _metric(result, "recall_at_k") <= tolerance]
    frontier.sort(
        key=lambda result: (
            -_metric(result, "recall_at_k"),
            -_metric(result, "macro_session_recall_at_k"),
            _metric(result, "session_stddev"),
            -_metric(result, "mrr"),
            -_metric(result, "recall_at_2k"),
            -_metric(result, "recall_at_4k"),
            _cost(result, "tuning_llm_calls", result.llm_calls),
            _cost(result, "tuning_embedding_calls", result.embedding_calls),
            result.runtime_seconds,
            result.complexity,
            result.name,
        )
    )
    return frontier[:limit]


def classify_overfit(
    tune: CandidateResult,
    validation: CandidateResult,
    baseline_tune: CandidateResult,
    baseline_validation: CandidateResult,
    *,
    validation_regression_pp: float,
) -> bool:
    tune_delta = (_metric(tune, "recall_at_k") - _metric(baseline_tune, "recall_at_k")) * 100.0
    validation_delta = (_metric(validation, "recall_at_k") - _metric(baseline_validation, "recall_at_k")) * 100.0
    return tune_delta > 0 and validation_delta < validation_regression_pp


def select_best(
    tune_by_name: dict[str, CandidateResult],
    validation_by_name: dict[str, CandidateResult],
    *,
    baseline_name: str,
    tie_tolerance_pp: float,
    overfit_regression_pp: float,
) -> tuple[CandidateResult, list[str], dict[str, Any]]:
    baseline_tune = tune_by_name[baseline_name]
    baseline_validation = validation_by_name.get(baseline_name, baseline_tune)
    overfit: list[str] = []
    eligible: list[CandidateResult] = []
    for name, validation in validation_by_name.items():
        tune = tune_by_name[name]
        if classify_overfit(
            tune,
            validation,
            baseline_tune,
            baseline_validation,
            validation_regression_pp=overfit_regression_pp,
        ):
            validation.status = "OVERFIT"
            overfit.append(name)
        else:
            eligible.append(validation)
    if not eligible:
        eligible = [baseline_validation]
    best_primary = max(_metric(result, "recall_at_k") for result in eligible)
    tolerance = tie_tolerance_pp / 100.0
    tied = [result for result in eligible if best_primary - _metric(result, "recall_at_k") <= tolerance]
    tied.sort(
        key=lambda result: (
            -_metric(result, "macro_session_recall_at_k"),
            _metric(result, "session_stddev"),
            -_metric(result, "mrr"),
            -_metric(result, "recall_at_2k"),
            -_metric(result, "recall_at_4k"),
            _cost(result, "tuning_llm_calls", result.llm_calls),
            _cost(result, "tuning_embedding_calls", result.embedding_calls),
            result.runtime_seconds,
            result.complexity,
            result.name,
        )
    )
    best = tied[0]
    explanation = {
        "primary": "validation_requirement_recall_at_k",
        "tie_tolerance_pp": tie_tolerance_pp,
        "near_tied_candidates": [result.name for result in tied],
        "tie_breakers": [
            "macro_session_recall_at_k",
            "session_stability",
            "mrr",
            "recall_at_2k",
            "recall_at_4k",
            "lower_cost",
            "lower_complexity",
        ],
    }
    return best, overfit, explanation
