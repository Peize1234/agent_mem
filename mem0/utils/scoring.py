"""
Scoring utilities for hybrid retrieval.

Provides:
- **BM25 normalization**: Sigmoid normalization of raw BM25 scores to [0, 1].
- **BM25 parameter selection**: Query-length-adaptive sigmoid parameters.
- **Additive scoring**: Combined scoring with semantic + BM25 + entity boost.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def get_bm25_params(query: str, *, lemmatized: Optional[str] = None) -> tuple:
    """Get BM25 sigmoid parameters based on query length.

    Longer queries tend to have higher raw BM25 scores, so we adjust
    the sigmoid midpoint and steepness accordingly.

    Returns:
        (midpoint, steepness) for sigmoid normalization.
    """
    if lemmatized is None:
        from mem0.utils.lemmatization import lemmatize_for_bm25

        lemmatized = lemmatize_for_bm25(query)
    num_terms = len(lemmatized.split()) if lemmatized else 1

    if num_terms <= 3:
        return 5.0, 0.7
    elif num_terms <= 6:
        return 7.0, 0.6
    elif num_terms <= 9:
        return 9.0, 0.5
    elif num_terms <= 15:
        return 10.0, 0.5
    else:
        return 12.0, 0.5


def normalize_bm25(raw_score: float, midpoint: float, steepness: float) -> float:
    """Normalize BM25 score to [0, 1] using logistic sigmoid.

    Args:
        raw_score: Raw BM25 score (unbounded, typically 0-20+).
        midpoint: Score at which sigmoid outputs 0.5.
        steepness: Controls how quickly sigmoid transitions.

    Returns:
        Normalized score in range [0, 1].
    """
    return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))


ENTITY_BOOST_WEIGHT = 0.5
HYBRID_PRESET_WEIGHTS = {
    "semantic-heavy": {"semantic": 0.70, "bm25": 0.20, "entity": 0.10},
    "balanced": {"semantic": 0.40, "bm25": 0.40, "entity": 0.20},
    "keyword-heavy": {"semantic": 0.25, "bm25": 0.65, "entity": 0.10},
    "entity-aware": {"semantic": 0.50, "bm25": 0.15, "entity": 0.35},
}


def validate_hybrid_weights(
    semantic_weight: float,
    bm25_weight: float,
    entity_weight: float,
) -> tuple[float, float, float]:
    weights = tuple(float(value) for value in (semantic_weight, bm25_weight, entity_weight))
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in weights):
        raise ValueError("hybrid weights must be finite values between 0 and 1")
    if not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
        raise ValueError("hybrid weights must sum to 1")
    return weights


def score_and_rank(
    semantic_results: List[Dict[str, Any]],
    bm25_scores: Dict[str, float],
    entity_boosts: Dict[str, float],
    threshold: float,
    top_k: int,
    explain: bool = False,
    session_weights: Optional[Dict[str, float]] = None,
    semantic_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
    entity_weight: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Score candidates additively and return top-k results.

    For each candidate:
        semantic_score is taken from the result's score field.
        combined = (semantic + bm25 + entity_boost) / max_possible

    Threshold gates the semantic score BEFORE combining -- candidates
    below the threshold are excluded even if BM25/entity would boost them.

    The divisor adapts based on which signals are active:
        - Semantic only: max_possible = 1.0
        - Semantic + BM25: max_possible = 2.0
        - Semantic + BM25 + entity: max_possible = 2.5
        - Semantic + entity (no BM25): max_possible = 1.5

    Args:
        semantic_results: Candidate memories from vector search.
        bm25_scores: Normalized keyword scores keyed by memory ID.
        entity_boosts: Entity-link boosts keyed by memory ID.
        threshold: Minimum semantic score required before hybrid scoring.
        top_k: Maximum number of results to return.
        explain: Include score_details in each result when true.

    Returns:
        List of scored result dicts sorted by combined score descending.
    """
    configured_weights = (semantic_weight, bm25_weight, entity_weight)
    weighted = not all(value is None for value in configured_weights)
    if weighted:
        if any(value is None for value in configured_weights):
            raise ValueError("semantic_weight, bm25_weight, and entity_weight must be configured together")
        semantic_weight, bm25_weight, entity_weight = validate_hybrid_weights(
            semantic_weight,
            bm25_weight,
            entity_weight,
        )

    has_bm25 = bool(bm25_scores)
    has_entity = bool(entity_boosts)

    max_possible = 1.0
    if has_bm25:
        max_possible += 1.0
    if has_entity:
        max_possible += ENTITY_BOOST_WEIGHT

    scored: List[Dict[str, Any]] = []

    for result in semantic_results:
        mem_id = result.get("id")
        if mem_id is None:
            continue

        semantic_score = result.get("score") or 0.0
        if semantic_score < threshold:
            continue

        mem_id_str = str(mem_id)
        bm25_score = bm25_scores.get(mem_id_str, 0.0)
        entity_boost = entity_boosts.get(mem_id_str, 0.0)

        if weighted:
            normalized_entity = min(max(entity_boost / ENTITY_BOOST_WEIGHT, 0.0), 1.0)
            raw_combined = (
                semantic_weight * semantic_score + bm25_weight * bm25_score + entity_weight * normalized_entity
            )
            hybrid_score = min(max(raw_combined, 0.0), 1.0)
        else:
            # Compatibility contract: the historical production formula and
            # its dynamic divisor remain byte-for-byte equivalent by default.
            raw_combined = semantic_score + bm25_score + entity_boost
            hybrid_score = min(raw_combined / max_possible, 1.0)
        session_weight = float((session_weights or {}).get(mem_id_str, 1.0))
        combined = hybrid_score * session_weight

        scored_result = {
            "id": mem_id_str,
            "score": combined,
            "payload": result.get("payload"),
        }
        if explain:
            scored_result["score_details"] = {
                "semantic_score": semantic_score,
                "bm25_score": bm25_score,
                "entity_boost": entity_boost,
                "raw_score": raw_combined,
                "max_possible_score": max_possible,
                "hybrid_score": hybrid_score,
                "session_weight": session_weight,
                "final_score": combined,
                "threshold": threshold,
            }
            if weighted:
                scored_result["score_details"]["weights"] = {
                    "semantic": semantic_weight,
                    "bm25": bm25_weight,
                    "entity": entity_weight,
                }
        scored.append(scored_result)

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
