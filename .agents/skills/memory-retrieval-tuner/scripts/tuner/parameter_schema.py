"""Read tuner parameter facts from the production Pydantic configuration.

Search policy remains tuner-owned, but defaults, types, constraints, and
Literal/Enum values are always discovered from the production config models.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Annotated, Any, Literal, Mapping, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from mem0.configs.base import (
    AgenticRetrievalConfig,
    FineGrainedLongTermConfig,
    MemoryConfig,
    MidTermMemoryConfig,
    PromotedLongTermConfig,
    RetrievalRerankerConfig,
)

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


@dataclass(frozen=True)
class ProductionParameterMetadata:
    """Production-owned facts for one tuner-visible parameter."""

    name: str
    model: type[BaseModel]
    field_name: str
    annotation: Any
    default: Any
    ge: int | float | None
    gt: int | float | None
    le: int | float | None
    lt: int | float | None
    literal_values: tuple[Any, ...]


# These routes describe how tuner labels are deployed; the referenced field
# remains the sole owner of its type, default, constraints, and legal values.
_PRODUCTION_FIELD_ROUTES: dict[str, tuple[type[BaseModel], str]] = {
    **{
        name: (MidTermMemoryConfig, name)
        for name in (
            "top_k_sessions",
            "top_k_pages",
            "max_total_pages",
            "midterm_candidate_pool_multiplier",
            "midterm_rag_threshold",
            "short_term_capacity",
            "session_similarity_threshold",
            "embedding_similarity_weight",
            "keyword_overlap_weight",
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
            "page_representation",
            "retrieval_method",
            "fusion_method",
            "dense_weight",
            "rrf_rank_constant",
            "page_summary_prompt",
            "session_merge_prompt",
            "page_summary_request_options",
            "session_merge_request_options",
        )
    },
    "longterm_top_k": (FineGrainedLongTermConfig, "top_k"),
    "longterm_rag_threshold": (FineGrainedLongTermConfig, "rag_threshold"),
    "longterm_candidate_pool_multiplier": (FineGrainedLongTermConfig, "candidate_pool_multiplier"),
    "longterm_other_session_weight": (FineGrainedLongTermConfig, "other_session_weight"),
    "entity_similarity_threshold": (FineGrainedLongTermConfig, "entity_similarity_threshold"),
    "semantic_weight": (FineGrainedLongTermConfig, "semantic_weight"),
    "bm25_weight": (FineGrainedLongTermConfig, "bm25_weight"),
    "entity_weight": (FineGrainedLongTermConfig, "entity_weight"),
    "candidate_depth": (RetrievalRerankerConfig, "rerank_depth"),
    "rerank_depth": (RetrievalRerankerConfig, "rerank_depth"),
    "reranker_method": (RetrievalRerankerConfig, "method"),
    "longterm_rerank_depth": (RetrievalRerankerConfig, "rerank_depth"),
    "longterm_reranker_method": (RetrievalRerankerConfig, "method"),
    "fine_grained_longterm_extraction_prompt": (FineGrainedLongTermConfig, "extraction_prompt"),
    "fine_grained_longterm_extraction_request_options": (
        FineGrainedLongTermConfig,
        "extraction_request_options",
    ),
    "session_longterm_extraction_prompt": (FineGrainedLongTermConfig, "extraction_prompt"),
    "query_rewrite_prompt": (MemoryConfig, "query_rewrite_prompt"),
    "query_prompt_text": (MemoryConfig, "query_rewrite_prompt"),
    "max_iterations": (AgenticRetrievalConfig, "max_iterations"),
    "max_tool_calls": (AgenticRetrievalConfig, "max_tool_calls"),
    "max_queries": (AgenticRetrievalConfig, "max_queries"),
    "max_total_results": (AgenticRetrievalConfig, "max_total_results"),
    "max_tool_result_chars": (AgenticRetrievalConfig, "max_tool_result_chars"),
    "cross_session_longterm_rag_threshold": (PromotedLongTermConfig, "rag_threshold"),
    "cross_session_retention_half_life_hours": (PromotedLongTermConfig, "retention_half_life_hours"),
    "cross_session_retention_floor": (PromotedLongTermConfig, "retention_floor"),
    "cross_session_reinforcement_gain": (PromotedLongTermConfig, "reinforcement_gain"),
    "promoted_longterm_top_k": (PromotedLongTermConfig, "top_k"),
    "promoted_longterm_rag_threshold": (PromotedLongTermConfig, "rag_threshold"),
}


def _production_field(name: str) -> tuple[type[BaseModel], str]:
    try:
        return _PRODUCTION_FIELD_ROUTES[name]
    except KeyError as exc:
        raise KeyError(f"{name!r} is not backed by a tuner-routed production MemoryConfig field") from exc


def _constraint(field: Any, attribute: str) -> int | float | None:
    for item in field.metadata:
        value = getattr(item, attribute, None)
        if value is not None:
            return value
    return None


def _literal_values(annotation: Any) -> tuple[Any, ...]:
    if get_origin(annotation) is Literal:
        return tuple(get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return tuple(item.value for item in annotation)
    return ()


def production_parameter_metadata(name: str) -> ProductionParameterMetadata:
    """Return type/default/bounds/Literal values directly from Production."""

    model, field_name = _production_field(name)
    field = model.model_fields[field_name]
    return ProductionParameterMetadata(
        name=name,
        model=model,
        field_name=field_name,
        annotation=field.annotation,
        default=field.get_default(call_default_factory=True),
        ge=_constraint(field, "ge"),
        gt=_constraint(field, "gt"),
        le=_constraint(field, "le"),
        lt=_constraint(field, "lt"),
        literal_values=_literal_values(field.annotation),
    )


def production_integer_candidates(name: str) -> list[int]:
    """Enumerate a finite integer field range declared by Production."""

    metadata = production_parameter_metadata(name)
    if metadata.annotation is not int:
        raise TypeError(f"{name} is not a production integer field")
    if metadata.ge is not None:
        lower = math.ceil(metadata.ge)
    elif metadata.gt is not None:
        lower = math.floor(metadata.gt) + 1
    else:
        raise ValueError(f"{name} has no production lower bound")
    if metadata.le is not None:
        upper = math.floor(metadata.le)
    elif metadata.lt is not None:
        upper = math.ceil(metadata.lt) - 1
    else:
        raise ValueError(f"{name} has no production upper bound")
    return list(range(lower, upper + 1))


def production_literal_candidates(name: str) -> list[Any]:
    """Return Literal/Enum candidates declared by the production field type."""

    values = production_parameter_metadata(name).literal_values
    if not values:
        raise TypeError(f"{name} is not a production Literal/Enum field")
    return list(values)


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def production_overrides_from_candidate(config: Mapping[str, Any]) -> dict[str, Any]:
    """Translate tuner labels into a validated Production MemoryConfig override."""
    overrides = _deep_merge(
        dict(config.get("production_overrides") or {}),
        dict(config.get("source_config_overrides") or {}),
    )
    midterm = dict(overrides.get("midterm") or {})
    fine = dict(overrides.get("fine_grained_longterm") or {})
    agentic = dict(overrides.get("agentic_retrieval") or {})

    for name in (
        "top_k_sessions",
        "top_k_pages",
        "max_total_pages",
        "midterm_candidate_pool_multiplier",
        "midterm_rag_threshold",
        "short_term_capacity",
        "session_similarity_threshold",
        "embedding_similarity_weight",
        "keyword_overlap_weight",
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
        "page_representation",
        "retrieval_method",
        "fusion_method",
        "dense_weight",
        "page_summary_prompt",
        "session_merge_prompt",
        "page_summary_request_options",
        "session_merge_request_options",
    ):
        if name in config:
            midterm[name] = deepcopy(config[name])

    reranker_method = config.get("reranker_method")
    if reranker_method is not None:
        midterm_reranker = dict(midterm.get("reranker") or {})
        midterm_reranker.update(
            {
                "method": str(reranker_method),
                "rerank_depth": int(config.get("rerank_depth") or config.get("candidate_depth") or 30),
            }
        )
        if str(reranker_method) == "cross_encoder":
            model = config.get("reranker_model_path") or config.get("reranker_model_id")
            if model:
                midterm_reranker["backend"] = {
                    "provider": "sentence_transformer",
                    "config": {
                        "model": str(model),
                        "revision": config.get("reranker_model_revision"),
                        "local_files_only": bool(config.get("reranker_model_path")),
                    },
                    "max_concurrency": int(
                        config.get("midterm_reranker_max_concurrency") or config.get("reranker_max_concurrency") or 1
                    ),
                }
        else:
            midterm_reranker.pop("backend", None)
        midterm["reranker"] = midterm_reranker

    fine_names = {
        "longterm_top_k": "top_k",
        "longterm_rag_threshold": "rag_threshold",
        "longterm_candidate_pool_multiplier": "candidate_pool_multiplier",
        "longterm_other_session_weight": "other_session_weight",
        "entity_similarity_threshold": "entity_similarity_threshold",
        "semantic_weight": "semantic_weight",
        "bm25_weight": "bm25_weight",
        "entity_weight": "entity_weight",
        "fine_grained_longterm_extraction_prompt": "extraction_prompt",
        "fine_grained_longterm_extraction_request_options": "extraction_request_options",
    }
    for source, target in fine_names.items():
        if source in config:
            fine[target] = deepcopy(config[source])
    preset = config.get("longterm_hybrid_preset")
    if preset is not None:
        semantic, bm25, entity = HYBRID_PRESETS[str(preset)]
        fine.update({"semantic_weight": semantic, "bm25_weight": bm25, "entity_weight": entity})

    fine_reranker_method = config.get("longterm_reranker_method")
    if fine_reranker_method is not None:
        fine_reranker = dict(fine.get("reranker") or {})
        fine_reranker.update(
            {
                "method": str(fine_reranker_method),
                "rerank_depth": int(config.get("longterm_rerank_depth") or 30),
            }
        )
        if str(fine_reranker_method) == "cross_encoder":
            model = config.get("longterm_reranker_model_path") or config.get("longterm_reranker_model_id")
            if model:
                fine_reranker["backend"] = {
                    "provider": "sentence_transformer",
                    "config": {
                        "model": str(model),
                        "revision": config.get("longterm_reranker_model_revision"),
                        "local_files_only": bool(config.get("longterm_reranker_model_path")),
                    },
                    "max_concurrency": int(
                        config.get("longterm_reranker_max_concurrency") or config.get("reranker_max_concurrency") or 1
                    ),
                }
        else:
            fine_reranker.pop("backend", None)
        fine["reranker"] = fine_reranker

    if config.get("query_rewrite_prompt"):
        overrides["query_rewrite_prompt"] = str(config["query_rewrite_prompt"])
    elif "query_rewrite_prompt" not in overrides and config.get("query_prompt_text"):
        # ``query_prompt_text`` is retained as legacy experiment metadata.  It
        # must not overwrite an explicit production override supplied by a
        # caller or loaded from a source manifest.
        overrides["query_rewrite_prompt"] = str(config["query_prompt_text"])
    for name in ("max_queries", "max_total_results"):
        if name in config:
            agentic[name] = int(config[name])
    if midterm:
        overrides["midterm"] = midterm
    if fine:
        overrides["fine_grained_longterm"] = fine
    if agentic:
        overrides["agentic_retrieval"] = agentic
    return overrides


def validate_candidate_config(config: Mapping[str, Any], *, allow_unknown: bool = True) -> dict[str, Any]:
    """Validate production-backed values with Production's Pydantic fields."""

    value = dict(config)
    unknown: list[str] = []
    for name, raw_value in value.items():
        try:
            metadata = production_parameter_metadata(name)
        except KeyError:
            unknown.append(name)
            continue
        field_metadata = metadata.model.model_fields[metadata.field_name].metadata
        annotation = (
            Annotated.__class_getitem__((metadata.annotation, *field_metadata))
            if field_metadata
            else metadata.annotation
        )
        normalized = TypeAdapter(annotation).validate_python(raw_value)
        value[name] = normalized
    if not allow_unknown and unknown:
        raise ValueError(f"unknown tuner parameters: {', '.join(sorted(unknown))}")

    # Hybrid presets are tuner-owned search strategies, not production field
    # values. The selected weights are still validated by MemoryConfig below.
    if "longterm_hybrid_preset" in value and value["longterm_hybrid_preset"] not in HYBRID_PRESETS:
        raise ValueError("longterm_hybrid_preset must be a Python-defined preset")

    # Model-level invariants also stay in Production. Build only the minimal
    # override represented by this Candidate and let MemoryConfig validate it.
    production_overrides = production_overrides_from_candidate(value)
    if production_overrides:
        resolved = _deep_merge(MemoryConfig().model_dump(mode="python", warnings=False), production_overrides)
        MemoryConfig.model_validate(resolved)
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
        {"heat_alpha": alpha, "heat_beta": beta, "heat_gamma": gamma} for alpha, beta, gamma in HEAT_PRESETS.values()
    ]


def heat_modulation_candidates() -> list[dict[str, float]]:
    return [
        {"heat_modulation_min": minimum, "heat_modulation_max": maximum}
        for minimum, maximum in ((0.80, 1.20), (0.90, 1.10), (0.95, 1.05))
    ]


def promotion_threshold_candidates(heat_values: list[float] | tuple[float, ...]) -> list[float]:
    values = []
    for raw_value in heat_values:
        value = float(raw_value)
        if not math.isfinite(value):
            continue
        try:
            value = float(validate_candidate_config({"promotion_heat_threshold": value})["promotion_heat_threshold"])
        except ValueError:
            continue
        values.append(value)
    values.sort()
    if not values:
        return []
    n = len(values)
    return sorted(set(round(values[int((n - 1) * q)], 6) for q in (0.50, 0.75, 0.90)))
