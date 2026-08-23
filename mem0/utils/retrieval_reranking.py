"""Lightweight production reranking functions without model ownership."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from mem0.utils.lemmatization import lemmatize_for_bm25


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def lexical_overlap(query: str, text: str, *, language: str | None = None) -> float:
    query_tokens = set(lemmatize_for_bm25(query, language=language).split())
    if not query_tokens:
        return 0.0
    text_tokens = set(lemmatize_for_bm25(text, language=language).split())
    return len(query_tokens & text_tokens) / len(query_tokens)


def field_lexical_score(
    query: str,
    payload: Mapping[str, Any],
    *,
    field_weights: Mapping[str, float],
    language: str | None = None,
) -> float:
    keywords = payload.get("keywords") or []
    values = {
        "summary": str(payload.get("summary") or ""),
        "keywords": " ".join(str(value) for value in keywords) if isinstance(keywords, list) else str(keywords),
        "user_input": str(payload.get("user_input") or ""),
        "raw_dialogue": str(payload.get("raw_dialogue") or ""),
    }
    total = sum(max(0.0, float(weight)) for weight in field_weights.values()) or 1.0
    return (
        sum(
            max(0.0, float(weight)) * lexical_overlap(query, values.get(field, ""), language=language)
            for field, weight in field_weights.items()
        )
        / total
    )


def blend_reranker_scores(
    rows: Sequence[Mapping[str, Any]],
    secondary_scores: Mapping[str, float],
    *,
    dense_weight: float,
) -> list[dict[str, Any]]:
    from mem0.utils.retrieval_fusion import normalize_score_map

    dense_weight = float(dense_weight)
    if not 0 <= dense_weight <= 1:
        raise ValueError("dense_weight must be in [0, 1]")
    dense = normalize_score_map(rows)
    normalized_secondary = normalize_score_map(
        [{"id": item_id, "score": score} for item_id, score in secondary_scores.items()]
    )
    ranked = []
    for row in rows:
        item = dict(row)
        item_id = str(item.get("id") or "")
        score = dense_weight * dense.get(item_id, 0.0) + (1 - dense_weight) * normalized_secondary.get(item_id, 0.0)
        item["first_stage_score"] = float(item.get("score") or 0.0)
        item["rerank_score"] = secondary_scores.get(item_id, 0.0)
        item["score"] = score
        item["final_score"] = score
        ranked.append(item)
    return sorted(ranked, key=lambda item: (-float(item.get("score") or 0.0), str(item.get("id") or "")))
