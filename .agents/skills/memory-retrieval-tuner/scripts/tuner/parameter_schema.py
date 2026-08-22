"""Single source of truth for tuner parameter semantics and hard limits.

The production config remains authoritative for formulas.  This module only
describes which knobs are safe to search and validates candidates before a
Research decision or an adapter can execute them.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

SOURCE_CHANGING = frozenset(
    {
        "short_term_capacity",
        "session_similarity_threshold",
        "embedding_similarity_weight",
        "keyword_overlap_weight",
        "top_k_sessions",
        "page_summary_prompt",
        "session_merge_prompt",
        "fine_grained_longterm_extraction_prompt",
        "session_longterm_extraction_prompt",
        "embedding_model_id",
        "embedding_model_revision",
    }
)
WITHIN_SESSION_STATEFUL = frozenset(
    {
        "retention_half_life_turns",
        "retention_floor",
        "heat_alpha",
        "heat_beta",
        "heat_gamma",
        "heat_recency_tau_turns",
        "heat_modulation_min",
        "heat_modulation_max",
        "promotion_min_recall_count",
        "promotion_heat_threshold",
    }
)
CROSS_SESSION_TEMPORAL_STATEFUL = frozenset(
    {
        "cross_session_longterm_rag_threshold",
        "cross_session_retention_half_life_hours",
        "cross_session_retention_floor",
        "cross_session_reinforcement_gain",
    }
)
RETRIEVAL_ONLY = frozenset(
    {
        "top_k_pages",
        "max_total_pages",
        "midterm_candidate_pool_multiplier",
        "midterm_rag_threshold",
        "longterm_top_k",
        "longterm_rag_threshold",
        "longterm_candidate_pool_multiplier",
        "longterm_hybrid_preset",
        "entity_similarity_threshold",
        "dense_weight",
        "rerank_depth",
        "candidate_depth",
        "max_queries",
        "max_total_results",
        "query_rewrite_prompt",
    }
)
PRODUCTION_FIXED = frozenset({"longterm_other_session_weight"})

HEAT_PRESETS: dict[str, tuple[float, float, float]] = {
    "recall-heavy": (1.0, 0.25, 0.5),
    "balanced": (1.0, 0.5, 1.0),
    "interaction-heavy": (0.75, 1.0, 0.5),
    "recency-heavy": (0.75, 0.25, 1.5),
}
HYBRID_PRESETS: dict[str, tuple[float, float, float]] = {
    "semantic-heavy": (0.70, 0.20, 0.10),
    "balanced": (0.40, 0.40, 0.20),
    "keyword-heavy": (0.25, 0.65, 0.10),
    "entity-aware": (0.50, 0.15, 0.35),
}


def parameter_class(name: str) -> str:
    if name in SOURCE_CHANGING:
        return "source-changing"
    if name in WITHIN_SESSION_STATEFUL:
        return "within-session-stateful"
    if name in CROSS_SESSION_TEMPORAL_STATEFUL:
        return "cross-session-temporal-stateful"
    if name in PRODUCTION_FIXED:
        return "production-fixed"
    return "query-time" if name in RETRIEVAL_ONLY else "unknown"


def validate_candidate_config(config: Mapping[str, Any], *, allow_unknown: bool = True) -> dict[str, Any]:
    """Validate a candidate and return a normalized copy.

    This is deliberately stricter than YAML parsing so an LLM cannot bypass a
    hard maximum by returning a hand-written JSON configuration.
    """
    value = dict(config)
    if not allow_unknown:
        unknown = sorted(
            set(value)
            - (SOURCE_CHANGING | WITHIN_SESSION_STATEFUL | CROSS_SESSION_TEMPORAL_STATEFUL | RETRIEVAL_ONLY | PRODUCTION_FIXED)
        )
        if unknown:
            raise ValueError(f"unknown tuner parameters: {', '.join(unknown)}")

    def integer(name: str, minimum: int | None = None, maximum: int | None = None) -> None:
        if name not in value:
            return
        item = value[name]
        if isinstance(item, bool) or int(item) != item:
            raise ValueError(f"{name} must be an integer")
        item = int(item)
        if minimum is not None and item < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        if maximum is not None and item > maximum:
            raise ValueError(f"{name} must be <= {maximum}")
        value[name] = item

    def unit(name: str) -> None:
        if name in value:
            item = float(value[name])
            if not math.isfinite(item) or not 0 <= item <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
            value[name] = item

    integer("max_total_pages", 1, 5)
    integer("longterm_top_k", 1, 30)
    integer("midterm_candidate_pool_multiplier", 1, 8)
    integer("longterm_candidate_pool_multiplier", 1, 6)
    integer("top_k_sessions", 1)
    integer("top_k_pages", 1)
    integer("short_term_capacity", 2)
    if "short_term_capacity" in value and value["short_term_capacity"] % 2:
        raise ValueError("short_term_capacity must be a positive even number")
    integer("promotion_min_recall_count", 1)
    integer("max_queries", 1, 3)
    integer("max_total_results", 1, 5)
    integer("agentic_fixed_max_iterations", 2, 2)
    integer("agentic_fixed_max_tool_calls", 1, 1)
    integer("candidate_depth", 1, 100)
    integer("rerank_depth", 1, 100)
    for name in (
        "session_similarity_threshold", "midterm_rag_threshold", "longterm_rag_threshold",
        "cross_session_longterm_rag_threshold", "retention_floor", "cross_session_retention_floor",
        "entity_similarity_threshold", "dense_weight", "embedding_similarity_weight", "keyword_overlap_weight",
        "longterm_other_session_weight",
    ):
        unit(name)
    for name in ("retention_half_life_turns", "heat_recency_tau_turns", "cross_session_retention_half_life_hours"):
        if name in value and (not math.isfinite(float(value[name])) or float(value[name]) <= 0):
            raise ValueError(f"{name} must be > 0")
    if "embedding_similarity_weight" in value or "keyword_overlap_weight" in value:
        embedding = float(value.get("embedding_similarity_weight", 0.7))
        keyword = float(value.get("keyword_overlap_weight", 0.3))
        if not math.isclose(embedding + keyword, 1.0, abs_tol=1e-9):
            raise ValueError("embedding_similarity_weight + keyword_overlap_weight must equal 1")
    minimum = value.get("heat_modulation_min")
    maximum = value.get("heat_modulation_max")
    if minimum is not None or maximum is not None:
        minimum = float(0.9 if minimum is None else minimum)
        maximum = float(1.1 if maximum is None else maximum)
        if not 0 < minimum < 1 < maximum or minimum >= maximum:
            raise ValueError("heat modulation must satisfy 0 < min < 1 < max")
    for name in (
        "heat_alpha",
        "heat_beta",
        "heat_gamma",
        "cross_session_reinforcement_gain",
        "promotion_heat_threshold",
    ):
        if name in value and (not math.isfinite(float(value[name])) or float(value[name]) < 0):
            raise ValueError(f"{name} must be >= 0")
    if "longterm_hybrid_preset" in value and value["longterm_hybrid_preset"] not in HYBRID_PRESETS:
        raise ValueError("longterm_hybrid_preset must be a Python-defined preset")
    return value


def dynamic_turn_distance_candidates(distances: list[int] | tuple[int, ...]) -> list[int]:
    """Generate a small data-derived turn search set, never copied from hours."""
    values = sorted(max(int(item), 1) for item in distances if int(item) > 0)
    if not values:
        return [1]
    n = len(values)
    quantiles = [values[int((n - 1) * fraction)] for fraction in (0.25, 0.50, 0.75, 0.90)]
    return sorted(set(quantiles + [max(values)]))


def heat_preset_candidates() -> list[dict[str, float]]:
    return [
        {"heat_alpha": alpha, "heat_beta": beta, "heat_gamma": gamma}
        for alpha, beta, gamma in HEAT_PRESETS.values()
    ]


def heat_modulation_candidates() -> list[dict[str, float]]:
    return [
        {"heat_modulation_min": minimum, "heat_modulation_max": maximum}
        for minimum, maximum in ((0.80, 1.20), (0.90, 1.10), (0.95, 1.05))
    ]


def promotion_threshold_candidates(heat_values: list[float] | tuple[float, ...]) -> list[float]:
    values = sorted(float(value) for value in heat_values if math.isfinite(float(value)) and float(value) >= 0)
    if not values:
        return []
    n = len(values)
    return sorted(set(round(values[int((n - 1) * q)], 6) for q in (0.50, 0.75, 0.90)))
