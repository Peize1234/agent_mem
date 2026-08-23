"""Production score-fusion primitives used by retrieval layers and tuning."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


def _item_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("page_id") or "")


def normalize_score_map(ranking: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    values = [float(item.get("score") or 0.0) for item in ranking if _item_id(item)]
    if not values:
        return {}
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return {_item_id(item): 1.0 if high > 0 else 0.0 for item in ranking if _item_id(item)}
    return {
        _item_id(item): (float(item.get("score") or 0.0) - low) / (high - low) for item in ranking if _item_id(item)
    }


def normalized_score_fuse(
    dense: Sequence[Mapping[str, Any]],
    sparse: Sequence[Mapping[str, Any]],
    *,
    dense_weight: float,
) -> list[dict[str, Any]]:
    """Min-max normalize dense and sparse rankings, then compute a weighted sum."""
    dense_weight = float(dense_weight)
    if not 0 <= dense_weight <= 1:
        raise ValueError("dense_weight must be in [0, 1]")
    dense_scores = normalize_score_map(dense)
    sparse_scores = normalize_score_map(sparse)
    metadata = {_item_id(item): dict(item) for item in [*dense, *sparse] if _item_id(item)}
    rows = [
        {
            **metadata[item_id],
            "id": item_id,
            "score": dense_weight * dense_scores.get(item_id, 0.0)
            + (1.0 - dense_weight) * sparse_scores.get(item_id, 0.0),
            "dense_score_normalized": dense_scores.get(item_id, 0.0),
            "bm25_score_normalized": sparse_scores.get(item_id, 0.0),
        }
        for item_id in metadata
    ]
    rows.sort(key=lambda row: (-float(row["score"]), str(row["id"])))
    return [{**row, "rank": rank} for rank, row in enumerate(rows, start=1)]


def rrf_fuse(
    rankings: Sequence[Sequence[Mapping[str, Any]]],
    *,
    rank_constant: int = 60,
) -> list[dict[str, Any]]:
    """Fuse ordered rankings with reciprocal-rank fusion."""
    rank_constant = int(rank_constant)
    if rank_constant < 1:
        raise ValueError("rank_constant must be positive")
    by_id: dict[str, dict[str, Any]] = {}
    for ranking_index, ranking in enumerate(rankings):
        for rank, item in enumerate(ranking, start=1):
            item_id = _item_id(item)
            if not item_id:
                continue
            row = by_id.setdefault(
                item_id,
                {
                    **dict(item),
                    "id": item_id,
                    "score": 0.0,
                    "component_ranks": [None] * len(rankings),
                },
            )
            row["component_ranks"][ranking_index] = rank
            row["score"] += 1.0 / (rank_constant + rank)
    rows = sorted(by_id.values(), key=lambda row: (-float(row["score"]), str(row["id"])))
    return [{**row, "rank": rank} for rank, row in enumerate(rows, start=1)]
