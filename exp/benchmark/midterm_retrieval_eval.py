"""Shared offline evaluation helpers for the S001 mid-term Page experiments.

The helpers in this module are deliberately independent of the production
retriever.  They operate on a time-filtered snapshot and never mutate Qdrant or
the runtime SQLite database.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from mem0.utils.lemmatization import lemmatize_for_bm25


DEFAULT_CUTOFFS = (1, 3, 5, 10, 15, 20, 30)


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 hash for JSON-compatible data."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must be a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")


def percentile(values: Sequence[float], quantile: float) -> float | None:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not clean:
        return None
    return float(np.percentile(np.asarray(clean, dtype=float), quantile * 100.0))


def latency_stats(values: Iterable[float | None]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    if not clean:
        return {"count": 0, "mean": None, "p50": None, "p90": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "mean": statistics.fmean(clean),
        "p50": percentile(clean, 0.50),
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "max": max(clean),
    }


def page_representation(page: Mapping[str, Any], variant: str) -> str:
    """Build one of the controlled P0-P8 Page text representations."""
    summary = str(page.get("summary") or "").strip()
    user_input = str(page.get("user_input") or "").strip()
    assistant_response = str(page.get("assistant_response") or "").strip()
    raw_dialogue = str(page.get("raw_dialogue") or "").strip()
    keywords = page.get("keywords") or []
    keyword_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)

    def join(parts: Iterable[str]) -> str:
        return "\n".join(part for part in parts if part)

    if variant == "P0":
        # Exact production order/prefixes from MidTermMemory.page_embedding_text.
        return join((summary, f"Keywords: {keyword_text}" if keyword_text else "", f"User: {user_input}"))
    if variant == "P1":
        return summary
    if variant == "P2":
        return user_input
    if variant == "P3":
        return raw_dialogue
    if variant == "P4":
        return join((f"User: {user_input}", f"Assistant: {assistant_response}"))
    if variant == "P5":
        return join((f"User: {user_input}", summary))
    if variant == "P6":
        return join((summary, raw_dialogue))
    if variant == "P7":
        return join((f"User: {user_input}", f"Keywords: {keyword_text}" if keyword_text else ""))
    if variant == "P8":
        return join((summary, f"Keywords: {keyword_text}" if keyword_text else ""))
    raise ValueError(f"Unknown Page representation: {variant}")


def cosine_rank(
    query_vector: Sequence[float],
    pages: Sequence[Mapping[str, Any]],
    page_vectors: Mapping[str, Sequence[float]],
) -> list[dict[str, Any]]:
    """Rank every supplied Page by cosine similarity with deterministic ties."""
    if not pages:
        return []
    page_ids = [str(page["page_id"]) for page in pages]
    matrix = np.asarray([page_vectors[page_id] for page_id in page_ids], dtype=np.float32)
    query = np.asarray(query_vector, dtype=np.float32)
    if matrix.ndim != 2 or query.ndim != 1 or matrix.shape[1] != query.shape[0]:
        raise ValueError(f"Embedding shape mismatch: pages={matrix.shape}, query={query.shape}")
    matrix_norm = np.linalg.norm(matrix, axis=1)
    query_norm = float(np.linalg.norm(query))
    denominator = matrix_norm * query_norm
    scores = np.divide(matrix @ query, denominator, out=np.zeros(len(matrix), dtype=np.float32), where=denominator > 0)
    rows = [
        {
            "page_id": page_id,
            "source_turn_id": str(page.get("source_turn_id") or ""),
            "score": float(score),
        }
        for page_id, page, score in zip(page_ids, pages, scores)
    ]
    rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


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
                {
                    "page_id": page_id,
                    "source_turn_id": str(item.get("source_turn_id") or ""),
                    "score": 0.0,
                    "component_ranks": [None] * len(rankings),
                },
            )
            row["component_ranks"][ranking_index] = rank
            row["score"] += 1.0 / (rank_constant + rank)
    rows = sorted(by_page.values(), key=lambda row: (-float(row["score"]), str(row["page_id"])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def normalized_score_fuse(
    dense: Sequence[Mapping[str, Any]],
    sparse: Sequence[Mapping[str, Any]],
    *,
    dense_weight: float,
) -> list[dict[str, Any]]:
    def normalized(ranking: Sequence[Mapping[str, Any]]) -> dict[str, float]:
        values = [float(item.get("score") or 0.0) for item in ranking]
        if not values:
            return {}
        low, high = min(values), max(values)
        if math.isclose(low, high):
            return {str(item["page_id"]): 1.0 if high > 0 else 0.0 for item in ranking}
        return {str(item["page_id"]): (float(item.get("score") or 0.0) - low) / (high - low) for item in ranking}

    dense_scores = normalized(dense)
    sparse_scores = normalized(sparse)
    metadata = {
        str(item["page_id"]): str(item.get("source_turn_id") or "") for item in [*dense, *sparse]
    }
    rows = [
        {
            "page_id": page_id,
            "source_turn_id": metadata[page_id],
            "score": dense_weight * dense_scores.get(page_id, 0.0)
            + (1.0 - dense_weight) * sparse_scores.get(page_id, 0.0),
            "dense_score_normalized": dense_scores.get(page_id, 0.0),
            "bm25_score_normalized": sparse_scores.get(page_id, 0.0),
        }
        for page_id in metadata
    ]
    rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


class ChineseBM25Index:
    """Small offline BM25 index using the repository's Chinese tokenizer."""

    def __init__(self, pages: Sequence[Mapping[str, Any]], texts: Mapping[str, str], *, k1: float = 1.5, b: float = 0.75):
        self.pages = list(pages)
        self.k1 = k1
        self.b = b
        self.tokens: dict[str, list[str]] = {
            str(page["page_id"]): self.tokenize(texts[str(page["page_id"])]) for page in self.pages
        }
        lengths = [len(tokens) for tokens in self.tokens.values()]
        self.avg_length = statistics.fmean(lengths) if lengths else 1.0
        document_frequency: Counter[str] = Counter()
        for tokens in self.tokens.values():
            document_frequency.update(set(tokens))
        count = len(self.pages)
        self.idf = {
            token: math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
            for token, frequency in document_frequency.items()
        }

    @staticmethod
    def tokenize(text: str) -> list[str]:
        return [token for token in lemmatize_for_bm25(text, language="zh").split() if token]

    def rank(self, query: str) -> list[dict[str, Any]]:
        query_tokens = set(self.tokenize(query))
        rows: list[dict[str, Any]] = []
        for page in self.pages:
            page_id = str(page["page_id"])
            tokens = self.tokens[page_id]
            frequencies = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for token in query_tokens:
                frequency = frequencies.get(token, 0)
                if not frequency:
                    continue
                denominator = frequency + self.k1 * (1.0 - self.b + self.b * length / max(self.avg_length, 1e-9))
                score += self.idf.get(token, 0.0) * frequency * (self.k1 + 1.0) / denominator
            rows.append(
                {
                    "page_id": page_id,
                    "source_turn_id": str(page.get("source_turn_id") or ""),
                    "score": score,
                }
            )
        rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        return rows


def bm25_sanity_cases() -> list[dict[str, Any]]:
    cases = [
        ("经营现金流是否支持盈利质量", "经营活动现金流净额与净利润匹配，盈利质量改善", "股东权益和总资产连续增长"),
        ("贵州茅台营业收入同比变化", "贵州茅台营业收入2025年同比下降", "资产负债率与现金分红"),
        ("扣非净利润和归母净利润差异", "扣非净利润与归母净利润口径差异", "营业收入与总资产周转率"),
    ]
    results: list[dict[str, Any]] = []
    for index, (query, positive, negative) in enumerate(cases, start=1):
        pages = [
            {"page_id": f"positive-{index}", "source_turn_id": "positive"},
            {"page_id": f"negative-{index}", "source_turn_id": "negative"},
        ]
        texts = {pages[0]["page_id"]: positive, pages[1]["page_id"]: negative}
        ranking = ChineseBM25Index(pages, texts).rank(query)
        scores = {str(row["page_id"]): float(row["score"]) for row in ranking}
        passed = scores[pages[0]["page_id"]] > 0 and scores[pages[0]["page_id"]] > scores[pages[1]["page_id"]]
        results.append(
            {
                "case_id": index,
                "query": query,
                "positive_score": scores[pages[0]["page_id"]],
                "negative_score": scores[pages[1]["page_id"]],
                "passed": passed,
            }
        )
    return results


def append_reranked_candidates(
    full_ranking: Sequence[Mapping[str, Any]],
    reranked_candidate_ids: Sequence[str],
    *,
    candidate_k: int,
) -> list[dict[str, Any]]:
    """Replace the candidate prefix while retaining a full rank for diagnostics."""
    original = {str(item["page_id"]): dict(item) for item in full_ranking}
    candidate_ids = [str(item["page_id"]) for item in full_ranking[:candidate_k]]
    allowed = set(candidate_ids)
    seen: set[str] = set()
    ordered: list[str] = []
    for page_id in reranked_candidate_ids:
        page_id = str(page_id)
        if page_id in allowed and page_id not in seen:
            ordered.append(page_id)
            seen.add(page_id)
    ordered.extend(page_id for page_id in candidate_ids if page_id not in seen)
    ordered.extend(str(item["page_id"]) for item in full_ranking[candidate_k:])
    rows: list[dict[str, Any]] = []
    for rank, page_id in enumerate(ordered, start=1):
        row = dict(original[page_id])
        row["rank"] = rank
        rows.append(row)
    return rows


def evaluate_rankings(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    cutoffs: Sequence[int] = DEFAULT_CUTOFFS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Evaluate exact Page recall on Gold Pages available at query time.

    Recall is micro-averaged over eligible Gold Page IDs.  MRR and NDCG are
    macro-averaged per Query.  All rankings are expected to contain every Page
    visible to the Query, including the tail after candidate reranking.
    """
    gold_ranks: list[int] = []
    reciprocal_ranks: list[float] = []
    ndcg_at_5: list[float] = []
    recalls_by_k: dict[int, list[float]] = {int(k): [] for k in cutoffs}
    matched_by_k: Counter[int] = Counter()
    total_gold = 0
    per_query: list[dict[str, Any]] = []

    for query in queries:
        query_id = str(query["query_id"])
        ranking = list(rankings.get(query_id) or [])
        rank_by_page = {str(item["page_id"]): rank for rank, item in enumerate(ranking, start=1)}
        gold_ids = [str(item) for item in query.get("eligible_gold_page_ids") or []]
        if not gold_ids:
            continue
        missing = [page_id for page_id in gold_ids if page_id not in rank_by_page]
        if missing:
            raise ValueError(f"{query_id} ranking omits visible Gold Pages: {missing}")
        ranks = [rank_by_page[page_id] for page_id in gold_ids]
        gold_ranks.extend(ranks)
        total_gold += len(gold_ids)
        first_rank = min(ranks)
        reciprocal_ranks.append(1.0 / first_rank)
        for cutoff in cutoffs:
            matched = sum(rank <= cutoff for rank in ranks)
            matched_by_k[int(cutoff)] += matched
            recalls_by_k[int(cutoff)].append(matched / len(ranks))

        dcg = sum(1.0 / math.log2(rank + 1.0) for rank in ranks if rank <= 5)
        ideal_count = min(len(ranks), 5)
        ideal = sum(1.0 / math.log2(rank + 1.0) for rank in range(1, ideal_count + 1))
        ndcg = dcg / ideal if ideal else 0.0
        ndcg_at_5.append(ndcg)
        per_query.append(
            {
                "query_id": query_id,
                "gold_page_ids": gold_ids,
                "gold_ranks": ranks,
                "best_gold_rank": first_rank,
                "reciprocal_rank": 1.0 / first_rank,
                "ndcg_at_5": ndcg,
                **{f"recall_at_{cutoff}": sum(rank <= cutoff for rank in ranks) / len(ranks) for cutoff in cutoffs},
                "top20_page_ids": [str(item["page_id"]) for item in ranking[:20]],
                "top20_scores": [float(item.get("score") or 0.0) for item in ranking[:20]],
            }
        )

    metrics: dict[str, Any] = {
        "evaluated_query_count": len(per_query),
        "eligible_gold_count": total_gold,
        "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "ndcg_at_5": statistics.fmean(ndcg_at_5) if ndcg_at_5 else 0.0,
        "mean_gold_rank": statistics.fmean(gold_ranks) if gold_ranks else None,
        "median_gold_rank": statistics.median(gold_ranks) if gold_ranks else None,
        "max_gold_rank": max(gold_ranks) if gold_ranks else None,
    }
    for cutoff in cutoffs:
        metrics[f"recall_at_{cutoff}"] = matched_by_k[int(cutoff)] / total_gold if total_gold else 0.0
        metrics[f"macro_recall_at_{cutoff}"] = (
            statistics.fmean(recalls_by_k[int(cutoff)]) if recalls_by_k[int(cutoff)] else 0.0
        )
    return metrics, per_query


def gold_rank_buckets(per_query: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    buckets = {"1-5": 0, "6-10": 0, "11-20": 0, "21-30": 0, ">30 / not found": 0}
    for row in per_query:
        for rank in row.get("gold_ranks") or []:
            if rank <= 5:
                buckets["1-5"] += 1
            elif rank <= 10:
                buckets["6-10"] += 1
            elif rank <= 20:
                buckets["11-20"] += 1
            elif rank <= 30:
                buckets["21-30"] += 1
            else:
                buckets[">30 / not found"] += 1
    return buckets


def pairwise_cosine_stats(vectors: Sequence[Sequence[float]]) -> dict[str, float | int | None]:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or len(matrix) < 2:
        return {"pair_count": 0, "mean_pairwise_cosine": None, "p90_pairwise_cosine": None}
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    normalized = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)
    similarity = normalized @ normalized.T
    values = similarity[np.triu_indices(len(matrix), k=1)].astype(float).tolist()
    return {
        "pair_count": len(values),
        "mean_pairwise_cosine": statistics.fmean(values),
        "p90_pairwise_cosine": percentile(values, 0.90),
    }


class StageTimer:
    """Tiny context manager for component latency bookkeeping."""

    def __init__(self) -> None:
        self.elapsed_ms = 0.0

    def __enter__(self) -> StageTimer:
        self.started = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed_ms = (time.perf_counter() - self.started) * 1000.0
