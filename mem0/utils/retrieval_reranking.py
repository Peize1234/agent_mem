"""Lightweight production reranking functions without model ownership."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


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
