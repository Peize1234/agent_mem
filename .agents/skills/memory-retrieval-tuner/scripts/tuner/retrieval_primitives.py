from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from mem0.utils.lemmatization import lemmatize_for_bm25

HYBRID_PRESET_WEIGHTS = {
    "semantic-heavy": {"semantic": 0.70, "bm25": 0.20, "entity": 0.10},
    "balanced": {"semantic": 0.40, "bm25": 0.40, "entity": 0.20},
    "keyword-heavy": {"semantic": 0.25, "bm25": 0.65, "entity": 0.10},
    "entity-aware": {"semantic": 0.50, "bm25": 0.15, "entity": 0.35},
}


def tuner_score_and_rank(
    semantic_results: Sequence[Mapping[str, Any]],
    bm25_scores: Mapping[str, float],
    entity_boosts: Mapping[str, float],
    *,
    threshold: float,
    top_k: int,
    weights: Mapping[str, float],
    explain: bool = False,
) -> list[dict[str, Any]]:
    """Tuner-only high-level weighting over frozen production signals."""
    if set(weights) != {"semantic", "bm25", "entity"}:
        raise ValueError("hybrid weights must define semantic, bm25, and entity")
    normalized = {key: float(value) for key, value in weights.items()}
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in normalized.values()):
        raise ValueError("hybrid weights must be finite values between 0 and 1")
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-9):
        raise ValueError("hybrid weights must sum to 1")

    rows = []
    for result in semantic_results:
        memory_id = str(result.get("id") or "")
        semantic_score = float(result.get("score") or 0.0)
        if not memory_id or semantic_score < threshold:
            continue
        bm25_score = float(bm25_scores.get(memory_id, 0.0))
        # Production entity boosts are capped at 0.5; normalize that existing
        # signal before applying a tuner-only preset.
        entity_boost = float(entity_boosts.get(memory_id, 0.0))
        normalized_entity = entity_boost / 0.5
        final_score = min(
            normalized["semantic"] * semantic_score
            + normalized["bm25"] * bm25_score
            + normalized["entity"] * normalized_entity,
            1.0,
        )
        row = {"id": memory_id, "score": final_score, "payload": result.get("payload")}
        if explain:
            row["score_details"] = {
                "semantic_score": semantic_score,
                "bm25_score": bm25_score,
                "entity_boost": entity_boost,
                "final_score": final_score,
                "threshold": threshold,
                "weights": normalized,
            }
        rows.append(row)
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    return rows[:top_k]


def page_representation(payload: Mapping[str, Any], variant: str) -> str:
    """Controlled Page representations extracted from the historical P0-P8 ablations."""
    summary = str(payload.get("summary") or "").strip()
    user = str(payload.get("user_input") or "").strip()
    assistant = str(payload.get("assistant_response") or "").strip()
    raw = str(payload.get("raw_dialogue") or "").strip()
    keywords = payload.get("keywords") or []
    keyword_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)

    def join(parts: Iterable[str]) -> str:
        return "\n".join(part for part in parts if part)

    variants = {
        "production": lambda: join((summary, f"Keywords: {keyword_text}" if keyword_text else "", f"User: {user}")),
        "P0": lambda: join((summary, f"Keywords: {keyword_text}" if keyword_text else "", f"User: {user}")),
        "summary": lambda: summary,
        "P1": lambda: summary,
        "user": lambda: user,
        "P2": lambda: user,
        "raw_dialogue": lambda: raw,
        "P3": lambda: raw,
        "user_assistant": lambda: join((f"User: {user}", f"Assistant: {assistant}")),
        "P4": lambda: join((f"User: {user}", f"Assistant: {assistant}")),
        "user_summary": lambda: join((f"User: {user}", summary)),
        "P5": lambda: join((f"User: {user}", summary)),
        "summary_raw": lambda: join((summary, raw)),
        "P6": lambda: join((summary, raw)),
        "user_keywords": lambda: join((f"User: {user}", f"Keywords: {keyword_text}" if keyword_text else "")),
        "P7": lambda: join((f"User: {user}", f"Keywords: {keyword_text}" if keyword_text else "")),
        "summary_keywords": lambda: join((summary, f"Keywords: {keyword_text}" if keyword_text else "")),
        "P8": lambda: join((summary, f"Keywords: {keyword_text}" if keyword_text else "")),
    }
    try:
        return variants[variant]()
    except KeyError as exc:
        raise ValueError(f"Unknown Page representation: {variant}") from exc


def session_representation(payload: Mapping[str, Any]) -> str:
    keywords = payload.get("summary_keywords") or []
    keyword_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
    return "\n".join(
        part
        for part in (str(payload.get("summary") or "").strip(), f"Keywords: {keyword_text}" if keyword_text else "")
        if part
    )


def normalize_scores(ranking: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    values = [float(item.get("score") or 0.0) for item in ranking]
    if not values:
        return {}
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return {str(item["page_id"]): 1.0 if high > 0 else 0.0 for item in ranking}
    return {str(item["page_id"]): (float(item.get("score") or 0.0) - low) / (high - low) for item in ranking}


def normalized_score_fuse(
    dense: Sequence[Mapping[str, Any]],
    sparse: Sequence[Mapping[str, Any]],
    *,
    dense_weight: float,
) -> list[dict[str, Any]]:
    if not 0.0 <= dense_weight <= 1.0:
        raise ValueError("dense_weight must be in [0, 1]")
    dense_scores = normalize_scores(dense)
    sparse_scores = normalize_scores(sparse)
    metadata = {str(item["page_id"]): dict(item) for item in [*dense, *sparse]}
    rows = [
        {
            **metadata[page_id],
            "page_id": page_id,
            "score": dense_weight * dense_scores.get(page_id, 0.0)
            + (1.0 - dense_weight) * sparse_scores.get(page_id, 0.0),
            "dense_score_normalized": dense_scores.get(page_id, 0.0),
            "bm25_score_normalized": sparse_scores.get(page_id, 0.0),
        }
        for page_id in metadata
    ]
    rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
    return [{**row, "rank": rank} for rank, row in enumerate(rows, start=1)]


def rrf_fuse(
    rankings: Sequence[Sequence[Mapping[str, Any]]],
    *,
    rank_constant: int = 60,
) -> list[dict[str, Any]]:
    by_page: dict[str, dict[str, Any]] = {}
    for ranking_index, ranking in enumerate(rankings):
        for rank, item in enumerate(ranking, start=1):
            page_id = str(item["page_id"])
            row = by_page.setdefault(
                page_id,
                {**dict(item), "page_id": page_id, "score": 0.0, "component_ranks": [None] * len(rankings)},
            )
            row["component_ranks"][ranking_index] = rank
            row["score"] += 1.0 / (rank_constant + rank)
    rows = sorted(by_page.values(), key=lambda row: (-float(row["score"]), str(row["page_id"])))
    return [{**row, "rank": rank} for rank, row in enumerate(rows, start=1)]


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0


def tokenize(text: str, *, language: str = "zh") -> set[str]:
    return {token for token in lemmatize_for_bm25(text, language=language).split() if token}


def lexical_overlap(query: str, text: str, *, language: str = "zh") -> float:
    query_tokens = tokenize(query, language=language)
    if not query_tokens:
        return 0.0
    return len(query_tokens & tokenize(text, language=language)) / len(query_tokens)


def field_aware_score(
    query: str,
    payload: Mapping[str, Any],
    *,
    field_weights: Mapping[str, float],
    language: str = "zh",
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
