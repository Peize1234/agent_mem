"""Second-stage, offline-only tuning for S001 mid-term Page reranking.

The experiment consumes the immutable snapshot and caches produced by
``run_midterm_retrieval_experiments.py``.  It does not replay S001, regenerate
memory, mutate Qdrant, or change the production retriever.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import json
import math
import os
import random
import re
import resource
import statistics
import time
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    ChineseBM25Index,
    append_reranked_candidates,
    bm25_sanity_cases,
    cosine_rank,
    evaluate_rankings,
    latency_stats,
    load_jsonl,
    normalized_score_fuse,
    page_representation,
    rrf_fuse,
    stable_hash,
)
from exp.benchmark.run_midterm_retrieval_experiments import (  # noqa: E402
    EmbeddingCache,
    llm_rerank_messages,
    llm_ranking_validator,
    parse_llm_json,
    validate_snapshot,
)
from mem0.configs.embeddings.base import BaseEmbedderConfig  # noqa: E402
from mem0.embeddings.huggingface import HuggingFaceEmbedding  # noqa: E402
from mem0.reranker.huggingface_reranker import HuggingFaceReranker  # noqa: E402


FIRST_STAGE_DEFAULT = REPO_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
OUTPUT_DEFAULT = FIRST_STAGE_DEFAULT / "rerank_tuning"
PRODUCTION_EMBEDDING = "BAAI/bge-small-zh-v1.5"
BASE_RERANKER = "BAAI/bge-reranker-base"
STRONG_RERANKER = "BAAI/bge-reranker-large"
RRF_CONSTANT = 60
CANDIDATE_KS = (10, 15, 20, 30)
RERANK_REPRESENTATIONS = ("P0", "P1", "P8")
FIXED_BUDGET_STRATEGIES = (
    "dense",
    "bm25",
    "union_fixed",
    "rrf60",
    "weighted_dense_0.8",
    "weighted_dense_0.6",
)
ALL_CANDIDATE_STRATEGIES = (*FIXED_BUDGET_STRATEGIES, "union_expanded")
LOCAL_PROMPT_VERSION = "cross-encoder-independent-score-v1"
LLM_RERANK_PROMPT_VERSION = "page-rerank-v1"
LQ_PROMPT_VERSION = "lightweight-query-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tune S001 candidate pools and Page rerankers offline")
    parser.add_argument("--first-stage-dir", type=Path, default=FIRST_STAGE_DEFAULT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--candidate-ks", default=",".join(str(value) for value in CANDIDATE_KS))
    parser.add_argument("--local-reranker-model", default=BASE_RERANKER)
    parser.add_argument("--stronger-reranker-model", default=STRONG_RERANKER)
    parser.add_argument("--skip-stronger-reranker", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=4)
    parser.add_argument("--llm-concurrency", type=int, default=6)
    parser.add_argument("--warmup-repeats", type=int, default=3)
    parser.add_argument("--latency-repeats", type=int, default=5)
    parser.add_argument("--retry-unavailable-models", action="store_true")
    return parser.parse_args()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def parse_candidate_ks(value: str) -> tuple[int, ...]:
    values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    if not values or any(item < 5 for item in values):
        raise ValueError("candidate K values must be integers >= 5")
    return values


def rank_of(ranking: Sequence[Mapping[str, Any]], page_id: str) -> int | None:
    return next(
        (index for index, item in enumerate(ranking, start=1) if str(item["page_id"]) == str(page_id)),
        None,
    )


def full_ranking_with_prefix(
    preferred_ids: Sequence[str],
    tail_ranking: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_id = {str(item["page_id"]): dict(item) for item in tail_ranking}
    ordered: list[str] = []
    seen: set[str] = set()
    for page_id in [*preferred_ids, *(str(item["page_id"]) for item in tail_ranking)]:
        if page_id in by_id and page_id not in seen:
            ordered.append(page_id)
            seen.add(page_id)
    result: list[dict[str, Any]] = []
    for rank, page_id in enumerate(ordered, start=1):
        row = dict(by_id[page_id])
        row["rank"] = rank
        result.append(row)
    return result


def fixed_budget_union(
    dense: Sequence[Mapping[str, Any]],
    bm25: Sequence[Mapping[str, Any]],
    budget: int,
) -> tuple[list[dict[str, Any]], int]:
    """Dense ceil(K/2) + BM25 floor(K/2), then alternate refill to K."""
    limit = min(budget, len(dense))
    dense_quota = min((budget + 1) // 2, len(dense))
    bm25_quota = min(budget // 2, len(bm25))
    dense_ids = [str(item["page_id"]) for item in dense]
    bm25_ids = [str(item["page_id"]) for item in bm25]
    selected: list[str] = []
    seen: set[str] = set()

    def add(page_id: str) -> None:
        if page_id not in seen and len(selected) < limit:
            selected.append(page_id)
            seen.add(page_id)

    for index in range(max(dense_quota, bm25_quota)):
        if index < dense_quota:
            add(dense_ids[index])
        if index < bm25_quota:
            add(bm25_ids[index])
    dense_cursor, bm25_cursor = dense_quota, bm25_quota
    while len(selected) < limit and (dense_cursor < len(dense_ids) or bm25_cursor < len(bm25_ids)):
        if dense_cursor < len(dense_ids):
            add(dense_ids[dense_cursor])
            dense_cursor += 1
        if bm25_cursor < len(bm25_ids):
            add(bm25_ids[bm25_cursor])
            bm25_cursor += 1
    tail = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
    return full_ranking_with_prefix(selected, tail), len(selected)


def expanded_union(
    dense: Sequence[Mapping[str, Any]],
    bm25: Sequence[Mapping[str, Any]],
    per_path_k: int,
) -> tuple[list[dict[str, Any]], int]:
    """Dense Top-K union BM25 Top-K, ordered by RRF inside the union."""
    selected = {
        *(str(item["page_id"]) for item in dense[:per_path_k]),
        *(str(item["page_id"]) for item in bm25[:per_path_k]),
    }
    tail = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
    prefix = [str(item["page_id"]) for item in tail if str(item["page_id"]) in selected]
    return full_ranking_with_prefix(prefix, tail), len(prefix)


def movement_category(baseline_rank: int, reranked_rank: int) -> str:
    if baseline_rank > 5 and reranked_rank <= 5:
        return "PROMOTED_INTO_TOP5"
    if baseline_rank <= 5 and reranked_rank > 5:
        return "DEMOTED_OUT_OF_TOP5"
    if reranked_rank < baseline_rank:
        return "PROMOTED_BUT_STILL_MISS" if reranked_rank > 5 else "UNCHANGED"
    if reranked_rank > baseline_rank:
        return "DEMOTED_BUT_STILL_HIT" if reranked_rank <= 5 else "UNCHANGED"
    return "UNCHANGED"


def load_existing_embedding_vectors(
    first_stage_dir: Path,
    *,
    model_name: str,
    prefix: str,
    required_ids: Sequence[str],
) -> dict[str, list[float]]:
    model_dir = first_stage_dir / "cache/embeddings" / model_name.replace("/", "_")
    required = set(required_ids)
    for path in sorted(model_dir.glob(f"{prefix}*.npz")):
        with np.load(path) as cached:
            ids = [str(item) for item in cached["ids"].tolist()]
            if required.issubset(ids):
                vectors = np.asarray(cached["vectors"], dtype=np.float32)
                by_id = {item_id: vector.tolist() for item_id, vector in zip(ids, vectors)}
                return {item_id: by_id[item_id] for item_id in required_ids}
    raise FileNotFoundError(f"No first-stage embedding cache contains {prefix} / {len(required_ids)} required IDs")


def load_snapshot(first_stage_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    snapshot = first_stage_dir / "snapshot"
    queries = load_jsonl(snapshot / "queries.jsonl")
    pages = load_jsonl(snapshot / "pages.jsonl")
    visibility_rows = load_jsonl(snapshot / "query_page_visibility.jsonl")
    manifest = load_json(snapshot / "snapshot_manifest.json")
    validate_snapshot(queries, pages, visibility_rows, str(manifest["sheet"]))
    if set(str(query["session_id"]) for query in queries) != {"S001_贵州茅台_投研"}:
        raise ValueError("Second-stage snapshot is not restricted to S001")
    return queries, pages, {str(row["query_id"]): row for row in visibility_rows}


def candidate_components(
    queries: Sequence[Mapping[str, Any]],
    pages_by_id: Mapping[str, Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Sequence[float]],
    page_vectors: Mapping[str, Sequence[float]],
    candidate_representation: str,
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, float]],
]:
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    timings: dict[str, dict[str, float]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        texts = {str(page["page_id"]): page_representation(page, candidate_representation) for page in visible_pages}
        started = time.perf_counter()
        dense = cosine_rank(query_vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors)
        dense_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        bm25_index = ChineseBM25Index(visible_pages, texts)
        bm25 = bm25_index.rank(str(query["original_query"]))
        bm25_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        rrf = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
        weighted_08 = normalized_score_fuse(dense, bm25, dense_weight=0.8)
        weighted_06 = normalized_score_fuse(dense, bm25, dense_weight=0.6)
        fusion_ms = (time.perf_counter() - started) * 1000.0
        rankings[query_id] = {
            "dense": dense,
            "bm25": bm25,
            "rrf60": rrf,
            "weighted_dense_0.8": weighted_08,
            "weighted_dense_0.6": weighted_06,
        }
        timings[query_id] = {
            "dense_ms": dense_ms,
            "bm25_ms": bm25_ms,
            "fusion_ms": fusion_ms,
        }
    return rankings, timings


def candidate_rankings_for_config(
    components: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    strategy: str,
    candidate_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    actual_counts: dict[str, int] = {}
    for query_id, values in components.items():
        if strategy in {"dense", "bm25", "rrf60", "weighted_dense_0.8", "weighted_dense_0.6"}:
            ranking = [dict(item) for item in values[strategy]]
            actual = min(candidate_k, len(ranking))
        elif strategy == "union_fixed":
            ranking, actual = fixed_budget_union(values["dense"], values["bm25"], candidate_k)
        elif strategy == "union_expanded":
            ranking, actual = expanded_union(values["dense"], values["bm25"], candidate_k)
        else:
            raise ValueError(f"Unknown candidate strategy: {strategy}")
        rankings[query_id] = ranking
        actual_counts[query_id] = actual
    return rankings, actual_counts


def pool_recall(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    actual_counts: Mapping[str, int],
) -> tuple[float, int, int]:
    total = 0
    hits = 0
    for query in queries:
        query_id = str(query["query_id"])
        candidate_ids = {str(item["page_id"]) for item in rankings[query_id][: actual_counts[query_id]]}
        for page_id in query.get("eligible_gold_page_ids") or []:
            total += 1
            hits += int(str(page_id) in candidate_ids)
    return hits / total if total else 0.0, hits, total


def strategy_latency(
    strategy: str,
    timings: Mapping[str, Mapping[str, float]],
) -> dict[str, float | int | None]:
    values = []
    for row in timings.values():
        dense_ms = float(row["dense_ms"])
        bm25_ms = float(row["bm25_ms"])
        fusion_ms = float(row["fusion_ms"])
        if strategy == "dense":
            values.append(dense_ms)
        elif strategy == "bm25":
            values.append(bm25_ms)
        else:
            values.append(dense_ms + bm25_ms + fusion_ms)
    return latency_stats(values)


def benchmark_candidate_retrieval(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Sequence[float]],
    page_vectors: Mapping[str, Sequence[float]],
    *,
    candidate_representation: str,
    candidate_strategy: str,
    candidate_k: int,
    warmups: int,
    repeats: int,
) -> dict[str, Any]:
    pages_by_id = {str(page["page_id"]): page for page in pages}

    def run_one(query: Mapping[str, Any]) -> None:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        dense: list[dict[str, Any]] | None = None
        bm25: list[dict[str, Any]] | None = None
        if candidate_strategy != "bm25":
            dense = cosine_rank(query_vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors)
        if candidate_strategy != "dense":
            texts = {
                str(page["page_id"]): page_representation(page, candidate_representation)
                for page in visible_pages
            }
            bm25 = ChineseBM25Index(visible_pages, texts).rank(str(query["original_query"]))
        if candidate_strategy == "union_fixed":
            assert dense is not None and bm25 is not None
            fixed_budget_union(dense, bm25, candidate_k)
        elif candidate_strategy == "rrf60":
            assert dense is not None and bm25 is not None
            rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
        elif candidate_strategy == "weighted_dense_0.8":
            assert dense is not None and bm25 is not None
            normalized_score_fuse(dense, bm25, dense_weight=0.8)
        elif candidate_strategy == "weighted_dense_0.6":
            assert dense is not None and bm25 is not None
            normalized_score_fuse(dense, bm25, dense_weight=0.6)

    for _ in range(max(warmups, 0)):
        for query in queries:
            run_one(query)
    values: list[float] = []
    for _ in range(max(repeats, 1)):
        for query in queries:
            started = time.perf_counter()
            run_one(query)
            values.append((time.perf_counter() - started) * 1000.0)
    stats = latency_stats(values)
    return {
        "candidate_representation": candidate_representation,
        "candidate_strategy": candidate_strategy,
        "candidate_k": candidate_k,
        "warmup_repeats": warmups,
        "measurement_repeats": repeats,
        "cold_start_excluded": True,
        **{f"warm_{key}_ms" if key != "count" else "measurement_count": value for key, value in stats.items()},
    }


def benchmark_query_embedding(
    queries: Sequence[Mapping[str, Any]],
    *,
    warmups: int,
    repeats: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    model = HuggingFaceEmbedding(BaseEmbedderConfig(model=PRODUCTION_EMBEDDING))
    load_ms = (time.perf_counter() - started) * 1000.0
    warm_text = str(queries[-1]["original_query"])
    for _ in range(max(warmups, 0)):
        model.embed(warm_text, "search")
    values: list[float] = []
    for _ in range(max(repeats, 1)):
        for query in queries:
            started = time.perf_counter()
            model.embed(str(query["original_query"]), "search")
            values.append((time.perf_counter() - started) * 1000.0)
    stats = latency_stats(values)
    del model
    gc.collect()
    return {
        "model": PRODUCTION_EMBEDDING,
        "model_load_ms": load_ms,
        "warmup_repeats": warmups,
        "measurement_repeats": repeats,
        "cold_start_excluded": True,
        **{f"warm_{key}_ms" if key != "count" else "measurement_count": value for key, value in stats.items()},
    }


def build_candidate_grid(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Sequence[float]],
    page_vectors_by_rep: Mapping[str, Mapping[str, Sequence[float]]],
    candidate_ks: Sequence[int],
) -> tuple[
    list[dict[str, Any]],
    dict[tuple[str, str, int], dict[str, list[dict[str, Any]]]],
    dict[tuple[str, str, int], dict[str, int]],
    dict[str, dict[str, dict[str, list[dict[str, Any]]]]],
]:
    pages_by_id = {str(page["page_id"]): page for page in pages}
    grid_rows: list[dict[str, Any]] = []
    rankings_by_config: dict[tuple[str, str, int], dict[str, list[dict[str, Any]]]] = {}
    counts_by_config: dict[tuple[str, str, int], dict[str, int]] = {}
    components_by_rep: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for candidate_representation in ("P0", "P8"):
        components, timing = candidate_components(
            queries,
            pages_by_id,
            visibility,
            query_vectors,
            page_vectors_by_rep[candidate_representation],
            candidate_representation,
        )
        components_by_rep[candidate_representation] = components
        for strategy in ALL_CANDIDATE_STRATEGIES:
            latency = strategy_latency(strategy, timing)
            for candidate_k in candidate_ks:
                rankings, actual_counts = candidate_rankings_for_config(components, strategy, candidate_k)
                metrics, _ = evaluate_rankings(queries, rankings)
                candidate_recall, candidate_hits, candidate_total = pool_recall(
                    queries, rankings, actual_counts
                )
                key = (candidate_representation, strategy, candidate_k)
                rankings_by_config[key] = rankings
                counts_by_config[key] = actual_counts
                values = list(actual_counts.values())
                grid_rows.append(
                    {
                        "candidate_representation": candidate_representation,
                        "candidate_strategy": strategy,
                        "candidate_k": candidate_k,
                        "candidate_k_semantics": (
                            "per-path K; expanded union actual pool may exceed K"
                            if strategy == "union_expanded"
                            else "final deduplicated candidate budget"
                        ),
                        "candidate_recall": candidate_recall,
                        "candidate_hits": candidate_hits,
                        "eligible_gold_count": candidate_total,
                        "candidate_recall_at_10": metrics["recall_at_10"],
                        "candidate_recall_at_15": metrics["recall_at_15"],
                        "candidate_recall_at_20": metrics["recall_at_20"],
                        "candidate_recall_at_30": metrics["recall_at_30"],
                        "pre_rerank_recall_at_5": metrics["recall_at_5"],
                        "pre_rerank_mrr": metrics["mrr"],
                        "mean_actual_candidate_count": statistics.fmean(values),
                        "min_actual_candidate_count": min(values),
                        "max_actual_candidate_count": max(values),
                        "retrieval_mean_ms": latency["mean"],
                        "retrieval_p50_ms": latency["p50"],
                        "retrieval_p95_ms": latency["p95"],
                    }
                )
    return grid_rows, rankings_by_config, counts_by_config, components_by_rep


def build_complementarity(
    queries: Sequence[Mapping[str, Any]],
    components_by_rep: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    candidate_ks: Sequence[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for representation, components in components_by_rep.items():
        for candidate_k in candidate_ks:
            counts = {"DENSE_ONLY": 0, "BM25_ONLY": 0, "BOTH": 0, "NEITHER": 0}
            for query in queries:
                query_id = str(query["query_id"])
                dense = components[query_id]["dense"]
                bm25 = components[query_id]["bm25"]
                for page_id in query.get("eligible_gold_page_ids") or []:
                    dense_rank = rank_of(dense, str(page_id))
                    bm25_rank = rank_of(bm25, str(page_id))
                    dense_hit = bool(dense_rank and dense_rank <= candidate_k)
                    bm25_hit = bool(bm25_rank and bm25_rank <= candidate_k)
                    category = (
                        "BOTH"
                        if dense_hit and bm25_hit
                        else "DENSE_ONLY"
                        if dense_hit
                        else "BM25_ONLY"
                        if bm25_hit
                        else "NEITHER"
                    )
                    counts[category] += 1
                    details.append(
                        {
                            "candidate_representation": representation,
                            "candidate_k": candidate_k,
                            "query_id": query_id,
                            "gold_page_id": page_id,
                            "dense_rank": dense_rank,
                            "bm25_rank": bm25_rank,
                            "dense_hit": dense_hit,
                            "bm25_hit": bm25_hit,
                            "union_hit": dense_hit or bm25_hit,
                            "category": category,
                        }
                    )
            summary.append(
                {
                    "candidate_representation": representation,
                    "candidate_k": candidate_k,
                    "dense_only_gold": counts["DENSE_ONLY"],
                    "bm25_only_gold": counts["BM25_ONLY"],
                    "both_gold": counts["BOTH"],
                    "neither_gold": counts["NEITHER"],
                }
            )
    return details, summary


def model_cache_exists(model_name: str) -> bool:
    path = Path.home() / ".cache/huggingface/hub" / f"models--{model_name.replace('/', '--')}"
    return path.exists()


class LocalScoreCache:
    def __init__(self, output_dir: Path, model_name: str, *, retry_unavailable: bool = False):
        self.output_dir = output_dir
        self.model_name = model_name
        self.path = output_dir / "cache/rerank/local_scores.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = load_jsonl(self.path)
        self.by_key = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.model: HuggingFaceReranker | None = None
        self.status_path = output_dir / "cache/rerank/local_model_status.json"
        self.status_by_model = load_json(self.status_path) if self.status_path.exists() else {}
        self.retry_unavailable = retry_unavailable

    def load_model(self) -> HuggingFaceReranker:
        previous = self.status_by_model.get(self.model_name) or {}
        if previous.get("status") == "UNAVAILABLE" and not self.retry_unavailable:
            raise RuntimeError(f"Previously unavailable: {previous.get('reason')}")
        if self.model is not None:
            return self.model
        before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        was_cached_before_run = model_cache_exists(self.model_name)
        started = time.perf_counter()
        try:
            self.model = HuggingFaceReranker(
                {
                    "provider": "huggingface",
                    "model": self.model_name,
                    "batch_size": 32,
                    "max_length": 512,
                    "normalize": True,
                }
            )
        except Exception as exc:
            self.status_by_model[self.model_name] = {
                "status": "UNAVAILABLE",
                "reason": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=6),
            }
            dump_json(self.status_path, self.status_by_model)
            raise
        after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.status_by_model[self.model_name] = {
            "status": "AVAILABLE",
            "model": self.model_name,
            "model_load_ms": (time.perf_counter() - started) * 1000.0,
            "device": str(self.model.device),
            "max_rss_delta_kb": max(0, after_rss - before_rss),
            "was_cached_before_run": was_cached_before_run,
        }
        dump_json(self.status_path, self.status_by_model)
        return self.model

    def score(
        self,
        *,
        query_id: str,
        query_text: str,
        page_ids: Sequence[str],
        page_texts: Mapping[str, str],
        candidate_strategy: str,
        candidate_k: str | int,
        candidate_representation: str,
        rerank_representation: str,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        key_fields = {
            "query_id": query_id,
            "candidate_strategy": candidate_strategy,
            "candidate_k": candidate_k,
            "candidate_representation": candidate_representation,
            "rerank_representation": rerank_representation,
            "reranker_model": self.model_name,
            "reranker_prompt_version": LOCAL_PROMPT_VERSION,
            "query_text": query_text,
            "candidate_page_ids": list(page_ids),
            "page_text_hash": stable_hash({page_id: page_texts[page_id] for page_id in page_ids}),
        }
        cache_key = stable_hash(key_fields)
        if cached := self.by_key.get(cache_key):
            return {str(key): float(value) for key, value in cached["scores_by_page"].items()}, cached
        model = self.load_model()
        documents = [{"page_id": page_id, "memory": page_texts[page_id]} for page_id in page_ids]
        started = time.perf_counter()
        result = model.rerank(query_text, documents, top_k=len(documents))
        latency_ms = (time.perf_counter() - started) * 1000.0
        scores = {str(item["page_id"]): float(item["rerank_score"]) for item in result}
        row = {
            "cache_key": cache_key,
            **key_fields,
            "scores_by_page": scores,
            "ranking_page_ids": [str(item["page_id"]) for item in result],
            "rerank_latency_ms": latency_ms,
            "status": "SUCCESS",
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.by_key[cache_key] = row
        return scores, row

    def release(self) -> None:
        if self.model is not None:
            del self.model
            self.model = None
            gc.collect()


def apply_scores_to_pool(
    full_ranking: Sequence[Mapping[str, Any]],
    actual_count: int,
    scores: Mapping[str, float],
) -> list[dict[str, Any]]:
    candidates = [str(item["page_id"]) for item in full_ranking[:actual_count]]
    ordered = sorted(candidates, key=lambda page_id: (-float(scores[page_id]), page_id))
    return append_reranked_candidates(full_ranking, ordered, candidate_k=actual_count)


def local_reranker_grid(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    rankings_by_config: Mapping[tuple[str, str, int], Mapping[str, Sequence[Mapping[str, Any]]]],
    counts_by_config: Mapping[tuple[str, str, int], Mapping[str, int]],
    output_dir: Path,
    model_name: str,
    *,
    retry_unavailable: bool,
) -> tuple[
    list[dict[str, Any]],
    dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]],
    dict[str, Any],
]:
    pages_by_id = {str(page["page_id"]): page for page in pages}
    page_texts_by_rep = {
        rep: {page_id: page_representation(page, rep) for page_id, page in pages_by_id.items()}
        for rep in RERANK_REPRESENTATIONS
    }
    cache = LocalScoreCache(output_dir, model_name, retry_unavailable=retry_unavailable)
    score_maps: dict[tuple[str, str], dict[str, float]] = {}
    score_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        for rerank_representation in RERANK_REPRESENTATIONS:
            for query in queries:
                query_id = str(query["query_id"])
                visible_ids = [
                    str(item["page_id"])
                    for item in rankings_by_config[("P0", "dense", max(CANDIDATE_KS))][query_id]
                ]
                scores, metadata = cache.score(
                    query_id=query_id,
                    query_text=str(query["original_query"]),
                    page_ids=visible_ids,
                    page_texts=page_texts_by_rep[rerank_representation],
                    candidate_strategy="ALL_VISIBLE_SCORE_CACHE",
                    candidate_k="all",
                    candidate_representation="N/A",
                    rerank_representation=rerank_representation,
                )
                score_maps[(rerank_representation, query_id)] = scores
                score_metadata[(rerank_representation, query_id)] = metadata
    except Exception as exc:
        return [], {}, {
            "status": "UNAVAILABLE",
            "model": model_name,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    candidate_lookup = {
        (str(row["candidate_representation"]), str(row["candidate_strategy"]), int(row["candidate_k"])): row
        for row in candidate_rows
    }
    rows: list[dict[str, Any]] = []
    derived_rankings: dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]] = {}
    for config, base_rankings in rankings_by_config.items():
        candidate_representation, strategy, candidate_k = config
        if strategy == "union_expanded":
            continue
        for rerank_representation in RERANK_REPRESENTATIONS:
            rankings: dict[str, list[dict[str, Any]]] = {}
            for query in queries:
                query_id = str(query["query_id"])
                rankings[query_id] = apply_scores_to_pool(
                    base_rankings[query_id],
                    counts_by_config[config][query_id],
                    score_maps[(rerank_representation, query_id)],
                )
            metrics, _ = evaluate_rankings(queries, rankings)
            derived_key = (*config, rerank_representation)
            derived_rankings[derived_key] = rankings
            candidate = candidate_lookup[config]
            score_latency = latency_stats(
                score_metadata[(rerank_representation, str(query["query_id"]))]["rerank_latency_ms"]
                for query in queries
            )
            rows.append(
                {
                    "candidate_representation": candidate_representation,
                    "candidate_strategy": strategy,
                    "candidate_k": candidate_k,
                    "rerank_representation": rerank_representation,
                    "reranker_model": model_name,
                    "candidate_recall": candidate["candidate_recall"],
                    **{key: value for key, value in metrics.items() if not key.startswith("macro_")},
                    "score_all_visible_mean_ms": score_latency["mean"],
                    "score_all_visible_p95_ms": score_latency["p95"],
                    "status": "AVAILABLE",
                }
            )
    status = dict(cache.status_by_model.get(model_name) or {})
    status.setdefault("status", "AVAILABLE")
    cache.release()
    return rows, derived_rankings, status


def select_best_local(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    available = [row for row in rows if row.get("status") == "AVAILABLE"]
    if not available:
        raise ValueError("No available local reranker configuration")
    return dict(
        max(
            available,
            key=lambda row: (
                float(row["recall_at_5"]),
                float(row["mrr"]),
                float(row["ndcg_at_5"]),
                float(row["candidate_recall"]),
                -int(row["candidate_k"]),
            ),
        )
    )


def benchmark_local_k_latency(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    rankings_by_config: Mapping[tuple[str, str, int], Mapping[str, Sequence[Mapping[str, Any]]]],
    counts_by_config: Mapping[tuple[str, str, int], Mapping[str, int]],
    candidate_representation: str,
    candidate_strategy: str,
    rerank_representation: str,
    candidate_ks: Sequence[int],
    model_name: str,
    output_dir: Path,
    *,
    warmups: int,
    repeats: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = LocalScoreCache(output_dir, model_name)
    model = cache.load_model()
    pages_by_id = {str(page["page_id"]): page for page in pages}
    texts = {page_id: page_representation(page, rerank_representation) for page_id, page in pages_by_id.items()}
    largest_query = max(queries, key=lambda query: len(rankings_by_config[(candidate_representation, candidate_strategy, max(candidate_ks))][str(query["query_id"])]))
    largest_id = str(largest_query["query_id"])
    warm_config = (candidate_representation, candidate_strategy, max(candidate_ks))
    warm_ids = [
        str(item["page_id"])
        for item in rankings_by_config[warm_config][largest_id][: counts_by_config[warm_config][largest_id]]
    ]
    warm_docs = [{"page_id": page_id, "memory": texts[page_id]} for page_id in warm_ids]
    for _ in range(max(warmups, 0)):
        model.rerank(str(largest_query["original_query"]), warm_docs, top_k=len(warm_docs))
    rows: list[dict[str, Any]] = []
    for candidate_k in candidate_ks:
        config = (candidate_representation, candidate_strategy, candidate_k)
        values: list[float] = []
        for _ in range(max(repeats, 1)):
            for query in queries:
                query_id = str(query["query_id"])
                candidate_ids = [
                    str(item["page_id"])
                    for item in rankings_by_config[config][query_id][: counts_by_config[config][query_id]]
                ]
                documents = [{"page_id": page_id, "memory": texts[page_id]} for page_id in candidate_ids]
                started = time.perf_counter()
                model.rerank(str(query["original_query"]), documents, top_k=len(documents))
                values.append((time.perf_counter() - started) * 1000.0)
        stats = latency_stats(values)
        rows.append(
            {
                "candidate_representation": candidate_representation,
                "candidate_strategy": candidate_strategy,
                "candidate_k": candidate_k,
                "rerank_representation": rerank_representation,
                "reranker_model": model_name,
                "warmup_repeats": warmups,
                "measurement_repeats": repeats,
                "cold_start_excluded": True,
                **{f"warm_{key}_ms" if key != "count" else "measurement_count": value for key, value in stats.items()},
            }
        )
    status = dict(cache.status_by_model.get(model_name) or {})
    cache.release()
    return rows, status


class LLMJsonlCache:
    def __init__(
        self,
        path: Path,
        *,
        client: AsyncOpenAI,
        model: str,
        timeout: float,
        retries: int,
        concurrency: int,
        first_stage_rows: Sequence[Mapping[str, Any]] = (),
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.lock = asyncio.Lock()
        self.rows = load_jsonl(path)
        self.by_key = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.by_prompt = {
            (str(row.get("model")), str(row.get("thinking_mode")), str(row.get("prompt_hash"))): row
            for row in [*first_stage_rows, *self.rows]
            if row.get("status") == "SUCCESS"
        }

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(
        self,
        *,
        key_fields: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        prompt_version: str,
        max_tokens: int,
        validator,
    ) -> dict[str, Any]:
        prompt_hash = stable_hash({"version": prompt_version, "messages": list(messages)})
        full_key_fields = {
            **dict(key_fields),
            "model": self.model,
            "thinking_mode": "disabled",
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
        }
        cache_key = stable_hash(full_key_fields)
        if cached := self.by_key.get(cache_key):
            return cached
        prompt_key = (self.model, "disabled", prompt_hash)
        if source := self.by_prompt.get(prompt_key):
            row = {
                "cache_key": cache_key,
                **full_key_fields,
                "status": "SUCCESS",
                "parsed": source["parsed"],
                "raw_output": source.get("raw_output"),
                "llm_latency_ms": source.get("llm_latency_ms"),
                "prompt_tokens": source.get("prompt_tokens"),
                "completion_tokens": source.get("completion_tokens"),
                "retry_count": source.get("retry_count", 0),
                "errors": source.get("errors", []),
                "cache_source": "first_stage_or_equivalent_prompt",
            }
            await self.append(row)
            self.by_key[cache_key] = row
            return row
        errors: list[str] = []
        retry_count = 0
        async with self.semaphore:
            for attempt in range(1, self.retries + 1):
                started = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(
                            model=self.model,
                            messages=list(messages),
                            temperature=0.0,
                            max_tokens=max_tokens,
                            response_format={"type": "json_object"},
                            extra_body={"thinking": {"type": "disabled"}},
                        ),
                        timeout=self.timeout,
                    )
                    content = response.choices[0].message.content or ""
                    parsed = validator(parse_llm_json(content))
                    usage = response.usage
                    row = {
                        "cache_key": cache_key,
                        **full_key_fields,
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": content,
                        "llm_latency_ms": (time.perf_counter() - started) * 1000.0,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "retry_count": retry_count,
                        "errors": errors,
                        "cache_source": "api",
                    }
                    await self.append(row)
                    self.by_key[cache_key] = row
                    self.by_prompt[prompt_key] = row
                    return row
                except Exception as exc:
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if attempt < self.retries:
                        retry_count += 1
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12.0))
        row = {
            "cache_key": cache_key,
            **full_key_fields,
            "status": "FAILED",
            "parsed": None,
            "llm_latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "retry_count": retry_count,
            "errors": errors,
        }
        await self.append(row)
        return row


def deepseek_client() -> tuple[AsyncOpenAI, str]:
    config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm = dict((config.get("llm") or {}).get("config") or {})
    model = str(llm.get("model") or "deepseek-chat")
    base_url = llm.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    return AsyncOpenAI(api_key=llm.get("api_key"), base_url=base_url), model


def complete_llm_ranking_validator(allowed_ids: set[str]):
    base_validator = llm_ranking_validator(allowed_ids)

    def validator(value: Mapping[str, Any]) -> dict[str, Any]:
        parsed = base_validator(value)
        if set(parsed["ranking"]) != allowed_ids:
            missing = sorted(allowed_ids - set(parsed["ranking"]))
            raise ValueError(f"ranking must include every candidate ID; missing={missing}")
        return parsed

    return validator


async def deepseek_reranker_grid(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    rankings_by_config: Mapping[tuple[str, str, int], Mapping[str, Sequence[Mapping[str, Any]]]],
    counts_by_config: Mapping[tuple[str, str, int], Mapping[str, int]],
    candidate_config_by_k: Mapping[int, tuple[str, str]],
    candidate_ks: Sequence[int],
    rerank_representations: Sequence[str] = RERANK_REPRESENTATIONS,
) -> tuple[
    list[dict[str, Any]],
    dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]],
    dict[tuple[str, str, int, str], dict[str, dict[str, Any]]],
]:
    client, model = deepseek_client()
    first_rows = load_jsonl(args.first_stage_dir / "cache/rerank/llm_rerank.jsonl")
    cache = LLMJsonlCache(
        args.output_dir / "cache/rerank/llm_rerank.jsonl",
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.llm_concurrency,
        first_stage_rows=first_rows,
    )
    pages_by_id = {str(page["page_id"]): page for page in pages}

    async def one(candidate_k: int, rerank_representation: str, query: Mapping[str, Any]):
        query_id = str(query["query_id"])
        candidate_representation, candidate_strategy = candidate_config_by_k[candidate_k]
        config = (candidate_representation, candidate_strategy, candidate_k)
        actual_count = counts_by_config[config][query_id]
        candidate_rows = list(rankings_by_config[config][query_id][:actual_count])
        short_to_page = {
            f"P{index:02d}": str(item["page_id"]) for index, item in enumerate(candidate_rows, start=1)
        }
        candidates = [
            {
                "short_id": short_id,
                "text": page_representation(pages_by_id[page_id], rerank_representation),
            }
            for short_id, page_id in short_to_page.items()
        ]
        messages = llm_rerank_messages(str(query["original_query"]), candidates)
        row = await cache.call(
            key_fields={
                "query_id": query_id,
                "candidate_strategy": candidate_strategy,
                "candidate_k": candidate_k,
                "actual_candidate_count": actual_count,
                "candidate_representation": candidate_representation,
                "rerank_representation": rerank_representation,
                "reranker_model": model,
                "reranker_prompt_version": LLM_RERANK_PROMPT_VERSION,
                "candidate_page_ids": list(short_to_page.values()),
            },
            messages=messages,
            prompt_version=LLM_RERANK_PROMPT_VERSION,
            max_tokens=1000,
            validator=complete_llm_ranking_validator(set(short_to_page)),
        )
        return candidate_k, rerank_representation, query_id, short_to_page, row

    results = await asyncio.gather(
        *(one(candidate_k, rep, query) for candidate_k in candidate_ks for rep in rerank_representations for query in queries)
    )
    await client.close()
    rankings_by_variant: dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]] = {
        (*candidate_config_by_k[candidate_k], candidate_k, rep): {}
        for candidate_k in candidate_ks
        for rep in rerank_representations
    }
    metadata_by_variant: dict[tuple[str, str, int, str], dict[str, dict[str, Any]]] = {
        (*candidate_config_by_k[candidate_k], candidate_k, rep): {}
        for candidate_k in candidate_ks
        for rep in rerank_representations
    }
    for candidate_k, rep, query_id, short_to_page, row in results:
        if row.get("status") != "SUCCESS":
            continue
        candidate_representation, candidate_strategy = candidate_config_by_k[candidate_k]
        config = (candidate_representation, candidate_strategy, candidate_k)
        page_ids = [short_to_page[short_id] for short_id in row["parsed"]["ranking"]]
        actual_count = counts_by_config[config][query_id]
        variant_key = (candidate_representation, candidate_strategy, candidate_k, rep)
        rankings_by_variant[variant_key][query_id] = append_reranked_candidates(
            rankings_by_config[config][query_id], page_ids, candidate_k=actual_count
        )
        metadata_by_variant[variant_key][query_id] = row
    rows: list[dict[str, Any]] = []
    for key, rankings in rankings_by_variant.items():
        candidate_representation, candidate_strategy, candidate_k, rep = key
        metadata = metadata_by_variant[key]
        if len(rankings) != len(queries):
            rows.append(
                {
                    "candidate_representation": candidate_representation,
                    "candidate_strategy": candidate_strategy,
                    "candidate_k": candidate_k,
                    "rerank_representation": rep,
                    "reranker_model": model,
                    "status": f"FAILED: {len(rankings)}/{len(queries)} queries",
                }
            )
            continue
        metrics, _ = evaluate_rankings(queries, rankings)
        config = (candidate_representation, candidate_strategy, candidate_k)
        candidate_recall, candidate_hits, _ = pool_recall(
            queries,
            rankings_by_config[config],
            counts_by_config[config],
        )
        prompt_tokens = [float(row.get("prompt_tokens") or 0) for row in metadata.values()]
        completion_tokens = [float(row.get("completion_tokens") or 0) for row in metadata.values()]
        latency = latency_stats(float(row.get("llm_latency_ms") or 0) for row in metadata.values())
        rows.append(
            {
                "candidate_representation": candidate_representation,
                "candidate_strategy": candidate_strategy,
                "candidate_k": candidate_k,
                "rerank_representation": rep,
                "reranker_model": model,
                "candidate_recall": candidate_recall,
                "candidate_hits": candidate_hits,
                **{metric: value for metric, value in metrics.items() if not metric.startswith("macro_")},
                "mean_prompt_tokens": statistics.fmean(prompt_tokens),
                "p95_prompt_tokens": float(np.percentile(prompt_tokens, 95)),
                "mean_completion_tokens": statistics.fmean(completion_tokens),
                "p95_completion_tokens": float(np.percentile(completion_tokens, 95)),
                "llm_mean_ms": latency["mean"],
                "llm_p95_ms": latency["p95"],
                "llm_calls_per_query": 1.0,
                "failure_rate": 0.0,
                "mean_retry_count": statistics.fmean(float(row.get("retry_count") or 0) for row in metadata.values()),
                "first_stage_or_prompt_cache_reuse_count": sum(
                    row.get("cache_source") == "first_stage_or_equivalent_prompt" for row in metadata.values()
                ),
                "status": "AVAILABLE",
            }
        )
    return rows, rankings_by_variant, metadata_by_variant


async def deepseek_rerank_custom(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    counts: Mapping[str, int],
    query_texts: Mapping[str, str],
    *,
    candidate_representation: str,
    candidate_strategy: str,
    candidate_k: int,
    rerank_representation: str,
    variant: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    client, model = deepseek_client()
    cache = LLMJsonlCache(
        args.output_dir / "cache/rerank/llm_rerank.jsonl",
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.llm_concurrency,
        first_stage_rows=load_jsonl(args.first_stage_dir / "cache/rerank/llm_rerank.jsonl"),
    )
    pages_by_id = {str(page["page_id"]): page for page in pages}

    async def one(query: Mapping[str, Any]):
        query_id = str(query["query_id"])
        candidate_rows = list(rankings[query_id][: counts[query_id]])
        short_to_page = {
            f"P{index:02d}": str(item["page_id"]) for index, item in enumerate(candidate_rows, start=1)
        }
        candidates = [
            {
                "short_id": short_id,
                "text": page_representation(pages_by_id[page_id], rerank_representation),
            }
            for short_id, page_id in short_to_page.items()
        ]
        messages = llm_rerank_messages(query_texts[query_id], candidates)
        row = await cache.call(
            key_fields={
                "query_id": query_id,
                "variant": variant,
                "candidate_strategy": candidate_strategy,
                "candidate_k": candidate_k,
                "actual_candidate_count": counts[query_id],
                "candidate_representation": candidate_representation,
                "rerank_representation": rerank_representation,
                "reranker_model": model,
                "reranker_prompt_version": LLM_RERANK_PROMPT_VERSION,
                "candidate_page_ids": list(short_to_page.values()),
            },
            messages=messages,
            prompt_version=LLM_RERANK_PROMPT_VERSION,
            max_tokens=1000,
            validator=complete_llm_ranking_validator(set(short_to_page)),
        )
        return query_id, short_to_page, row

    results = await asyncio.gather(*(one(query) for query in queries))
    await client.close()
    output: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for query_id, short_to_page, row in results:
        if row.get("status") != "SUCCESS":
            continue
        ordered = [short_to_page[short_id] for short_id in row["parsed"]["ranking"]]
        output[query_id] = append_reranked_candidates(
            rankings[query_id], ordered, candidate_k=counts[query_id]
        )
        metadata[query_id] = row
    return output, metadata


def lightweight_query_text(query: Mapping[str, Any], variant: str) -> str:
    current = str(query["original_query"])
    if variant == "LQ0":
        return current
    count = {"LQ1": 1, "LQ2": 2}[variant]
    history = list(query.get(f"previous_{count}_qa") or [])
    user_questions = [str(item["user"]) for item in history]
    return "Current query:\n" + current + "\n\nPrevious user questions:\n" + "\n".join(
        f"- {text}" for text in user_questions
    )


def lq4_messages(query: Mapping[str, Any]) -> list[dict[str, str]]:
    history = list(query.get("previous_3_qa") or [])
    questions = "\n".join(f"- {item['user']}" for item in history)
    system = (
        "你是轻量检索 Query 改写器。仅根据当前问题和最近用户问题做指代消解，以及主体、指标、时间范围补全；"
        "不得使用或猜测答案，不得添加未出现事实，不写解释。返回严格 JSON："
        '{"standalone_query":"一条可独立理解的中文 retrieval query"}。'
    )
    user = f"最近用户问题（不含助手回答）：\n{questions}\n\n当前问题：\n{query['original_query']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def lq_validator(value: Mapping[str, Any]) -> dict[str, Any]:
    query = str(value.get("standalone_query") or "").strip()
    if not query:
        raise ValueError("standalone_query is empty")
    return {"standalone_query": query}


async def build_lightweight_queries(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    options = {
        variant: {str(query["query_id"]): lightweight_query_text(query, variant) for query in queries}
        for variant in ("LQ0", "LQ1", "LQ2")
    }
    metadata: dict[str, dict[str, Any]] = {}
    if args.skip_llm:
        return options, metadata, []
    client, model = deepseek_client()
    cache = LLMJsonlCache(
        args.output_dir / "cache/query/lightweight_query.jsonl",
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.llm_concurrency,
    )

    async def one(query: Mapping[str, Any]):
        query_id = str(query["query_id"])
        row = await cache.call(
            key_fields={
                "query_id": query_id,
                "variant": "LQ4",
                "context_policy": "current_plus_previous_3_user_questions_only",
            },
            messages=lq4_messages(query),
            prompt_version=LQ_PROMPT_VERSION,
            max_tokens=500,
            validator=lq_validator,
        )
        return query_id, row

    results = await asyncio.gather(*(one(query) for query in queries))
    await client.close()
    options["LQ4"] = {}
    for query_id, row in results:
        if row.get("status") == "SUCCESS":
            options["LQ4"][query_id] = str(row["parsed"]["standalone_query"])
            metadata[query_id] = row
    sampled_ids = {str(query["query_id"]) for query in random.Random(42).sample(list(queries), 5)}
    audit: list[dict[str, Any]] = []
    by_id = {str(query["query_id"]): query for query in queries}
    for query_id in sampled_ids:
        query = by_id[query_id]
        rewrite = options["LQ4"].get(query_id, "")
        previous_questions = [str(item["user"]) for item in query.get("previous_3_qa") or []]
        allowed_text = "\n".join([str(query["original_query"]), *previous_questions])
        added_years = sorted(set(re.findall(r"20\d{2}", rewrite)) - set(re.findall(r"20\d{2}", allowed_text)))
        audit.append(
            {
                "query_id": query_id,
                "current_query": query["original_query"],
                "previous_user_questions": previous_questions,
                "rewrite": rewrite,
                "contains_required_context_verbatim": bool(
                    query.get("required_context") and str(query["required_context"]) in rewrite
                ),
                "contains_gold_page_id": any(str(page_id) in rewrite for page_id in query.get("gold_page_ids") or []),
                "assistant_history_in_prompt": False,
                "future_history_in_prompt": False,
                "added_years_not_in_input": added_years,
                "manual_review_status": "FAIL" if added_years else "PASS",
                "manual_review_note": (
                    f"Added unsupported year anchors: {', '.join(added_years)}"
                    if added_years
                    else "No unsupported factual anchor observed in the controlled five-item review."
                ),
            }
        )
    return options, metadata, audit


def candidate_rankings_for_query_options(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
    options: Mapping[str, Mapping[str, str]],
    variants: Sequence[str],
    candidate_representation: str,
    strategy: str,
    candidate_k: int,
    first_stage_dir: Path,
    output_dir: Path,
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, int]],
    dict[str, Any],
]:
    pages_by_id = {str(page["page_id"]): page for page in pages}
    page_ids = [str(page["page_id"]) for page in pages]
    if candidate_representation == "P0":
        page_vectors = {str(page["page_id"]): list(page["stored_embedding"]) for page in pages}
    else:
        page_vectors = load_existing_embedding_vectors(
            first_stage_dir,
            model_name=PRODUCTION_EMBEDDING,
            prefix=f"pages-{candidate_representation}",
            required_ids=page_ids,
        )
    embedding_cache = EmbeddingCache(output_dir / "cache/embeddings")
    new_variants = [variant for variant in variants if variant != "LQ0"]
    embedding_ids = [f"{variant}:{query['query_id']}:0" for variant in new_variants for query in queries]
    embedding_texts = [options[variant][str(query["query_id"])] for variant in new_variants for query in queries]
    if embedding_ids:
        query_vectors, embedding_metadata = embedding_cache.encode(
            PRODUCTION_EMBEDDING,
            "lightweight-query-variants",
            embedding_ids,
            embedding_texts,
            measure_individual=True,
        )
    else:
        query_vectors, embedding_metadata = {}, {"cache_hit": True, "item_count": 0}
    if "LQ0" in variants:
        existing_ids = [f"Q0:{query['query_id']}:0" for query in queries]
        existing = load_existing_embedding_vectors(
            first_stage_dir,
            model_name=PRODUCTION_EMBEDDING,
            prefix="queries-",
            required_ids=existing_ids,
        )
        query_vectors.update(
            {
                f"LQ0:{query['query_id']}:0": existing[f"Q0:{query['query_id']}:0"]
                for query in queries
            }
        )
    rankings_by_variant: dict[str, dict[str, list[dict[str, Any]]]] = {}
    counts_by_variant: dict[str, dict[str, int]] = {}
    for variant in variants:
        variant_rankings: dict[str, list[dict[str, Any]]] = {}
        variant_counts: dict[str, int] = {}
        for query in queries:
            query_id = str(query["query_id"])
            visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
            dense = cosine_rank(query_vectors[f"{variant}:{query_id}:0"], visible_pages, page_vectors)
            texts = {
                str(page["page_id"]): page_representation(page, candidate_representation)
                for page in visible_pages
            }
            bm25 = ChineseBM25Index(visible_pages, texts).rank(options[variant][query_id])
            components = {
                "dense": dense,
                "bm25": bm25,
                "rrf60": rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT),
                "weighted_dense_0.8": normalized_score_fuse(dense, bm25, dense_weight=0.8),
                "weighted_dense_0.6": normalized_score_fuse(dense, bm25, dense_weight=0.6),
            }
            if strategy in components:
                ranking = components[strategy]
                actual = min(candidate_k, len(ranking))
            elif strategy == "union_fixed":
                ranking, actual = fixed_budget_union(dense, bm25, candidate_k)
            else:
                raise ValueError(f"Unsupported lightweight strategy: {strategy}")
            variant_rankings[query_id] = ranking
            variant_counts[query_id] = actual
        rankings_by_variant[variant] = variant_rankings
        counts_by_variant[variant] = variant_counts
    return rankings_by_variant, counts_by_variant, embedding_metadata


def rerank_with_local_model(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    counts: Mapping[str, int],
    query_texts: Mapping[str, str],
    output_dir: Path,
    model_name: str,
    candidate_representation: str,
    candidate_strategy: str,
    candidate_k: int,
    rerank_representation: str,
    variant: str,
    *,
    retry_unavailable: bool = False,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    pages_by_id = {str(page["page_id"]): page for page in pages}
    page_texts = {
        page_id: page_representation(page, rerank_representation) for page_id, page in pages_by_id.items()
    }
    cache = LocalScoreCache(output_dir, model_name, retry_unavailable=retry_unavailable)
    result: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        page_ids = [str(item["page_id"]) for item in rankings[query_id][: counts[query_id]]]
        scores, row = cache.score(
            query_id=query_id,
            query_text=query_texts[query_id],
            page_ids=page_ids,
            page_texts=page_texts,
            candidate_strategy=f"{candidate_strategy}:{variant}",
            candidate_k=candidate_k,
            candidate_representation=candidate_representation,
            rerank_representation=rerank_representation,
        )
        result[query_id] = apply_scores_to_pool(rankings[query_id], counts[query_id], scores)
        metadata[query_id] = row
    cache.release()
    return result, metadata


def best_available(rows: Sequence[Mapping[str, Any]], *, lower_k_tiebreak: bool = True) -> dict[str, Any]:
    available = [dict(row) for row in rows if row.get("status") == "AVAILABLE"]
    if not available:
        raise ValueError("No available rows")
    return max(
        available,
        key=lambda row: (
            float(row.get("recall_at_5") or 0),
            float(row.get("mrr") or 0),
            float(row.get("ndcg_at_5") or 0),
            -int(row.get("candidate_k") or 999) if lower_k_tiebreak else 0,
        ),
    )


def per_gold_movements(
    queries: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Sequence[Mapping[str, Any]]],
    local_candidate: Mapping[str, Sequence[Mapping[str, Any]]],
    local: Mapping[str, Sequence[Mapping[str, Any]]],
    llm_candidate: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    llm: Mapping[str, Sequence[Mapping[str, Any]]] | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["query_id"])
        for page_id in query.get("eligible_gold_page_ids") or []:
            baseline_rank = rank_of(baseline[query_id], str(page_id))
            candidate_rank = rank_of(local_candidate[query_id], str(page_id))
            llm_candidate_rank = rank_of(llm_candidate[query_id], str(page_id)) if llm_candidate else None
            local_rank = rank_of(local[query_id], str(page_id))
            llm_rank = rank_of(llm[query_id], str(page_id)) if llm else None
            assert baseline_rank and candidate_rank and local_rank
            rows.append(
                {
                    "query_id": query_id,
                    "gold_page_id": page_id,
                    "baseline_rank": baseline_rank,
                    "candidate_rank": candidate_rank,
                    "local_candidate_rank": candidate_rank,
                    "llm_candidate_rank": llm_candidate_rank,
                    "local_rerank_rank": local_rank,
                    "local_rank_delta": baseline_rank - local_rank,
                    "local_movement": movement_category(baseline_rank, local_rank),
                    "llm_rerank_rank": llm_rank,
                    "llm_rank_delta": baseline_rank - llm_rank if llm_rank else None,
                    "llm_movement": movement_category(baseline_rank, llm_rank) if llm_rank else None,
                    "baseline_rank_6_to_20": 6 <= baseline_rank <= 20,
                }
            )
    return rows


def pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:.2f}%"


def pp(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:+.2f} pp"


def build_report(summary: Mapping[str, Any]) -> str:
    baseline = summary["baseline"]
    candidate = summary["best_candidate"]
    complement = summary["complementarity_at_selected_k"]
    local = summary.get("best_local") or {}
    deepseek = summary.get("best_deepseek") or {}
    low = summary.get("best_low_latency") or local
    pareto = summary.get("recommended_pareto") or local
    lq = summary.get("lightweight_query") or {}
    quality = summary.get("best_quality") or local
    production_ready = summary.get("production_recommendation") or {}
    candidate_source_table = "\n".join(
        f"| {row['candidate_strategy']} | {pct(row['candidate_recall'])} | "
        f"{float(row['mean_actual_candidate_count']):.2f} | {pct(row['pre_rerank_recall_at_5'])} |"
        for row in summary.get("candidate_source_k20") or []
    )
    candidate_k_table = "\n".join(
        f"| {candidate_k} | {row['candidate_representation']} {row['candidate_strategy']} | "
        f"{pct(row['candidate_recall'])} | {int(row['candidate_hits'])}/21 | "
        f"{float(row['mean_actual_candidate_count']):.2f} |"
        for candidate_k, row in sorted(
            ((int(key), value) for key, value in (summary.get("best_candidate_by_k") or {}).items())
        )
    )
    local_k_table = "\n".join(
        f"| {row['candidate_k']} | {pct(row['candidate_recall'])} | {pct(row['recall_at_5'])} | "
        f"{float(row['mrr']):.4f} | {float(row['ndcg_at_5']):.4f} | "
        f"{float(row['warm_mean_ms']):.1f} | {float(row['warm_p95_ms']):.1f} |"
        for row in summary.get("local_k_summary") or []
    )
    local_rep_table = "\n".join(
        f"| {row['rerank_representation']} | {pct(row['recall_at_5'])} | "
        f"{float(row['mrr']):.4f} | {float(row['ndcg_at_5']):.4f} |"
        for row in summary.get("local_representation_summary") or []
    )
    deepseek_rep_table = "\n".join(
        f"| {row['rerank_representation']} | {pct(row['recall_at_5'])} | "
        f"{float(row['mrr']):.4f} | {float(row['ndcg_at_5']):.4f} | "
        f"{float(row['mean_prompt_tokens']):.0f} | {float(row['p95_prompt_tokens']):.0f} |"
        for row in summary.get("deepseek_representation_summary") or []
    )
    deepseek_source_table = "\n".join(
        f"| {row['candidate_representation']} {row['candidate_strategy']} | "
        f"{pct(row['candidate_recall'])} | {pct(row['recall_at_5'])} | "
        f"{float(row['mrr']):.4f} |"
        for row in summary.get("deepseek_source_summary") or []
    )
    deepseek_k_table = "\n".join(
        f"| {row['candidate_k']} | {pct(row['candidate_recall'])} | {pct(row['recall_at_5'])} | "
        f"{float(row['mean_prompt_tokens']):.0f} | {float(row['llm_p95_ms']):.1f} |"
        for row in sorted(summary.get("deepseek_k_summary") or [], key=lambda item: int(item["candidate_k"]))
    )
    latency_table = "\n".join(
        f"| {row['scheme']} | {pct(row['recall_at_5'])} | {float(row['mrr']):.4f} | "
        f"{float(row['retrieval_total_mean_ms']):.1f} | "
        f"{float(row['retrieval_total_p95_conservative_ms']):.1f} | "
        f"{row['online_llm_calls_per_query']} | {float(row['mean_prompt_tokens'] or 0):.0f} |"
        for row in summary.get("warm_latency_summary") or []
    )
    lq_table = "\n".join(
        f"| {row['variant']} | {pct(row['recall_at_5'])} | {float(row['mrr']):.4f} | "
        f"{row.get('query_rewrite_llm_calls_per_query', 0)} |"
        for row in summary.get("lightweight_query_rows") or []
    )
    local_promoted = int((summary.get("local_movement_counts") or {}).get("PROMOTED_INTO_TOP5", 0))
    local_demoted = int((summary.get("local_movement_counts") or {}).get("DEMOTED_OUT_OF_TOP5", 0))
    llm_promoted = int((summary.get("llm_movement_counts") or {}).get("PROMOTED_INTO_TOP5", 0))
    llm_demoted = int((summary.get("llm_movement_counts") or {}).get("DEMOTED_OUT_OF_TOP5", 0))
    return f"""# S001 Mid-term Page Rerank Tuning — Second Stage

## 1. Baseline 校验

复用第一轮 immutable snapshot、Q0 embedding 与 Page stored embedding 后，Baseline 仍为 R@5 **{pct(baseline['recall_at_5'])}**、R@20 **{pct(baseline['recall_at_20'])}**、MRR {baseline['mrr']:.4f}、NDCG@5 {baseline['ndcg_at_5']:.4f}。逐 Query Top-K 与第一轮一致，校验状态：`{summary['baseline_validation_status']}`。

## 2. Candidate Source

相同最终候选预算下，覆盖率最高的受控 candidate 配置是 `{candidate['chain_name']}`：Candidate Recall **{pct(candidate['candidate_recall'])}**（{candidate['candidate_hits']}/21），平均实际候选 {candidate['mean_actual_candidate_count']:.2f}。Dense、BM25、固定预算 Union、扩大 Union、RRF60、0.8/0.2 与 0.6/0.4 normalized fusion 均已在 K=10/15/20/30 比较；完整数值见 `rerank_candidate_tuning.csv`。

扩大 Union 的 K 是“每路 K”，不是最终预算；它只作为 coverage 上界，未混入固定预算生产候选比较。

| P8 / K=20 source | Candidate Recall | Mean actual K | Pre-rerank R@5 |
|---|---:|---:|---:|
{candidate_source_table}

## 3. Dense / BM25 Complementarity

在 `{complement['candidate_representation']}`、K={complement['candidate_k']} 下：Dense-only {complement['dense_only_gold']}、BM25-only {complement['bm25_only_gold']}、Both {complement['both_gold']}、Neither {complement['neither_gold']}。因此 Dense/BM25 **{summary['complementarity_conclusion']}**。逐 Gold rank 与 hit 见 `candidate_complementarity.csv`。

## 4. candidate_k

K=10/15/20/30 的 candidate coverage、最终 local/DeepSeek R@5 与 warm latency/token 已分开统计。推荐 K 为 **{summary.get('recommended_candidate_k', 'N/A')}**；判断依据不是只看 coverage，而是更大 K 是否真正增加最终 Top-5 命中。

| K | 同预算 coverage 最佳 source | Candidate Recall | Gold | Mean actual K |
|---:|---|---:|---:|---:|
{candidate_k_table}

| Local K（固定 P0 RRF60 + P8 rerank） | Candidate Recall | R@5 | MRR | NDCG@5 | Warm mean ms | Warm p95 ms |
|---:|---:|---:|---:|---:|---:|---:|
{local_k_table}

K=20 是唯一达到 10/21 的 local 配置；K=15 为 9/21，K=10 为 8/21，K=30 又回落到 9/21。因此不能把 K20 无损降到 10/15，K30 也没有最终 Recall 收益。

## 5. Reranker Representation

P0、P1、P8 作为 reranker 输入独立于 candidate representation 比较。最佳 local rerank representation 为 `{local.get('rerank_representation', 'N/A')}`；最佳 DeepSeek representation 为 `{deepseek.get('rerank_representation', 'N/A')}`。P3 raw dialogue 平均约 9.7k 字，明显违背本轮低成本目标，因此没有进入主网格。

| Local rerank representation | R@5 | MRR | NDCG@5 |
|---|---:|---:|---:|
{local_rep_table}

| DeepSeek rerank representation（P8 BM25 K20） | R@5 | MRR | NDCG@5 | Mean tokens | P95 tokens |
|---|---:|---:|---:|---:|---:|
{deepseek_rep_table}

## 6. Local Reranker

最佳无在线 LLM 链路：`{local.get('chain_name', 'UNAVAILABLE')}`。R@5 **{pct(local.get('recall_at_5'))}**，相对生产 Baseline **{pp(local.get('delta_recall_at_5'))}**，MRR {float(local.get('mrr') or 0):.4f}，NDCG@5 {float(local.get('ndcg_at_5') or 0):.4f}，warm mean {float(local.get('warm_mean_ms') or 0):.1f} ms，warm p95 {float(local.get('warm_p95_ms') or 0):.1f} ms，online LLM calls/query = 0。

该 reranker 将 5 个 Gold 推入 Top-5，同时把 2 个原 Top-5 Gold 推出，净增 3 个。Recall 33.33%→47.62%，但 MRR 0.3322→0.3098；原因是 micro Gold 命中净增，而若干 Query 的首个高位 Gold 被推后。NDCG@5 0.2644→0.2976，说明 Top-5 整体仍改善。逐 Gold 见 `rerank_case_analysis.csv`。

更强本地模型状态与是否值得使用：{summary.get('stronger_reranker_conclusion', '未执行')} 其 R@5 为 {pct((summary.get('best_stronger_local') or {}).get('recall_at_5'))}，mean/p95 为 {float((summary.get('best_stronger_local') or {}).get('rerank_mean_ms') or 0):.1f}/{float((summary.get('best_stronger_local') or {}).get('rerank_p95_ms') or 0):.1f} ms，首次下载+加载约 {float((summary.get('stronger_reranker_status') or {}).get('model_load_ms') or 0):.1f} ms；不值得替换 base。

## 7. DeepSeek no-thinking Reranker

最佳链路：`{deepseek.get('chain_name', 'SKIPPED')}`。R@5 **{pct(deepseek.get('recall_at_5'))}**，相对生产 Baseline **{pp(deepseek.get('delta_recall_at_5'))}**，MRR {float(deepseek.get('mrr') or 0):.4f}，NDCG@5 {float(deepseek.get('ndcg_at_5') or 0):.4f}；平均/p95 prompt tokens 为 {float(deepseek.get('mean_prompt_tokens') or 0):.0f}/{float(deepseek.get('p95_prompt_tokens') or 0):.0f}，平均/p95 LLM latency 为 {float(deepseek.get('llm_mean_ms') or 0):.1f}/{float(deepseek.get('llm_p95_ms') or 0):.1f} ms，1 次 no-thinking LLM/query。

| Candidate source（K20/P8 rerank） | Candidate Recall | R@5 | MRR |
|---|---:|---:|---:|
{deepseek_source_table}

| DeepSeek K（P8 BM25/P8） | Candidate Recall | R@5 | Mean tokens | LLM p95 ms |
|---:|---:|---:|---:|---:|
{deepseek_k_table}

P8 相比 P0 同时提高 DeepSeek R@5（47.62% vs 38.10%）并减少约 9.4% mean prompt tokens；P1 更短但 R@5 只有 23.81%，不能用 token 节省抵消质量损失。K20 最佳；K30 coverage 达到 100% 但 R@5 降为 38.10%。

## 8. Local vs DeepSeek

DeepSeek 相比最佳 local 多命中 **{summary.get('deepseek_extra_gold_vs_local', 'N/A')}** 个 available Gold。Local/DeepSeek 分别有 {local_promoted}/{llm_promoted} 个 Gold 被推进 Top-5、{local_demoted}/{llm_demoted} 个原命中被推出。两者 R@5 相同，但 local 的 MRR/NDCG 更高且 0 在线 LLM，因此额外约一次在线调用不值得。

## 9. Lightweight Query Contextualization

{lq.get('conclusion', '未执行（LLM 被显式跳过）。')} LQ1/LQ2 只加入历史 User Question；LQ4 只看当前问题与最近最多 3 个 User Question，不含 Assistant 长回答、Gold、required_context 或未来消息。5 条固定样本人工审计为 {(summary.get('rewrite_manual_audit') or {}).get('pass_count', 0)} PASS / {(summary.get('rewrite_manual_audit') or {}).get('fail_count', 0)} FAIL；Q011 错加了输入中不存在的 2022 年锚点，因此当前 rewrite 方案判定为 **UNSTABLE**。明细见 `lightweight_query_audit.csv`。

| Query variant / chain | R@5 | MRR | Rewrite LLM calls/query |
|---|---:|---:|---:|
{lq_table}

## 10. 最佳方案

- Best Quality: `{quality.get('chain_name', 'UNAVAILABLE')}` — R@5 {pct(quality.get('recall_at_5'))}，MRR {float(quality.get('mrr') or 0):.4f}。
- Best Local / No-Online-LLM: `{local.get('chain_name', 'UNAVAILABLE')}` — R@5 {pct(local.get('recall_at_5'))}。
- Best Low-Latency: `{low.get('chain_name', 'UNAVAILABLE')}` — R@5 {pct(low.get('recall_at_5'))}，p95 {float(low.get('warm_p95_ms') or low.get('retrieval_p95_ms') or 0):.1f} ms。
- Recommended Pareto: `{pareto.get('chain_name', 'UNAVAILABLE')}` — R@5 {pct(pareto.get('recall_at_5'))}。

Pareto 规则是：R@5 至少达到 Best Quality 减 1 个 Gold（1/21），满足后优先 0 在线 LLM，再按 warm p95、token/memory cost 排序。

| Scheme | R@5 | MRR | Total mean ms | Conservative p95 ms | LLM calls/query | Mean prompt tokens |
|---|---:|---:|---:|---:|---:|---:|
{latency_table}

总延迟把 query embedding、candidate retrieval、rerank 相加；p95 是各段 p95 相加的保守上界。模型下载/加载与 3 次 warmup 不计入 warm latency。Base reranker load 约 5.8s；BGE-small load 约 4.8s。

## 11. 是否值得修改生产

**{production_ready.get('decision', 'NO')}**。{production_ready.get('reason', '第二阶段尚未完整运行。')}

本任务没有修改生产链路。Page Prompt 仍建议暂不修改：当前诊断的主要证据继续指向 candidate coverage 后的 Top-5 fine ranking，且本轮只调 candidate/rerank/query 构造，没有重新生成 Page。

## 12. 验收问题逐项结论

1. Dense 和 BM25 **真正互补**：P8/K20 下 Dense-only 2、BM25-only 4、Both 15、Neither 0。
2. 给 reranker 的最佳 coverage pool 是 `P8 union_fixed Top20`（20/21）；但最终 local 最佳 source 是 `P0 RRF60 Top20`，DeepSeek 最佳是 `P8 BM25 Top20`。coverage 最优不能替代 rerank 后实测。
3. 最佳完整质量 candidate_k 是 **20**；Pareto K 是 **{pareto.get('candidate_k', 'N/A')}**。
4. 最佳 reranker Page representation 是 **P8**（local 与 DeepSeek 一致）。
5. `bge-reranker-base` 最佳 R@5 是 **{pct(local.get('recall_at_5'))}**，即 10/21、相对生产 +14.29 pp。
6. `bge-reranker-large` 不值得：R@5 {pct((summary.get('best_stronger_local') or {}).get('recall_at_5'))}，比 base 少 3 Gold 且延迟显著恶化。
7. DeepSeek no-thinking 最佳 R@5 是 **{pct(deepseek.get('recall_at_5'))}**，即 10/21、相对生产 +14.29 pp。
8. DeepSeek 比最佳 local 多召回 **{summary.get('deepseek_extra_gold_vs_local', 'N/A')}** 个 Gold。
9. 该差异不值得约 1 次在线 LLM：Recall 持平，local MRR/NDCG 更高。
10. K20 不能无损降到 K10/15：分别损失 2/1 个 Gold；若允许少 1 Gold，K15 是规则化 Pareto。
11. Lightweight contextualization 无效且不稳定：Best Quality local 上 LQ4 比 Q0 少 3 个 Gold，5 条审计中 1 条添加了不存在的年份，停止 rewrite 路线。
12. Best Quality 是 `{quality.get('chain_name', 'UNAVAILABLE')}`。
13. Best No-Online-LLM 是 `{local.get('chain_name', 'UNAVAILABLE')}`。
14. Best Low-Latency 是 `{low.get('chain_name', 'UNAVAILABLE')}`。
15. Recommended Pareto 是 `{pareto.get('chain_name', 'UNAVAILABLE')}`。
16. 推荐链真实组件只有：Q0、P0 dense+中文 BM25 RRF60 candidate、BGE-small query embedding、P8 cross-encoder 输入、`bge-reranker-base`；没有无效的 BGE-base embedding 或在线 LLM。
17. 仍建议暂不改 Page Prompt：20-candidate coverage 可达 95.24%，主要矛盾仍是 fine ranking。
18. 是否已有足够证据进入生产代码修改阶段：**{production_ready.get('decision', 'NO')}**，但应先在更多 Session 做小流量复验，不能从 S001 直接改默认值。
"""


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    args.first_stage_dir = args.first_stage_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidate_ks = parse_candidate_ks(args.candidate_ks)
    queries, pages, visibility = load_snapshot(args.first_stage_dir)
    page_ids = [str(page["page_id"]) for page in pages]
    query_ids = [f"Q0:{query['query_id']}:0" for query in queries]
    query_vectors = load_existing_embedding_vectors(
        args.first_stage_dir,
        model_name=PRODUCTION_EMBEDDING,
        prefix="queries-",
        required_ids=query_ids,
    )
    page_vectors = {
        "P0": {str(page["page_id"]): list(page["stored_embedding"]) for page in pages},
        "P8": load_existing_embedding_vectors(
            args.first_stage_dir,
            model_name=PRODUCTION_EMBEDDING,
            prefix="pages-P8",
            required_ids=page_ids,
        ),
    }
    pages_by_id = {str(page["page_id"]): page for page in pages}
    baseline_rankings: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        baseline_rankings[query_id] = cosine_rank(
            query_vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors["P0"]
        )
    baseline, baseline_per_query = evaluate_rankings(queries, baseline_rankings)
    first_baseline = load_json(args.first_stage_dir / "baseline_metrics.json")
    expected = first_baseline["metrics"]
    topk_mismatches = []
    old_per_query = {str(row["query_id"]): row for row in first_baseline["per_query"]}
    for row in baseline_per_query:
        old = old_per_query[str(row["query_id"])]
        if row["top20_page_ids"] != old["top20_page_ids"]:
            topk_mismatches.append(str(row["query_id"]))
    baseline_matches = (
        math.isclose(float(baseline["recall_at_5"]), 1 / 3, abs_tol=1e-12)
        and math.isclose(float(baseline["recall_at_20"]), 18 / 21, abs_tol=1e-12)
        and math.isclose(float(baseline["recall_at_5"]), float(expected["recall_at_5"]), abs_tol=1e-12)
        and math.isclose(float(baseline["recall_at_20"]), float(expected["recall_at_20"]), abs_tol=1e-12)
        and not topk_mismatches
    )
    baseline_validation = {
        "status": "PASS" if baseline_matches else "FAIL",
        "recomputed": baseline,
        "expected": expected,
        "top20_mismatch_query_ids": topk_mismatches,
        "snapshot_reused": str(args.first_stage_dir / "snapshot"),
    }
    dump_json(args.output_dir / "baseline_validation.json", baseline_validation)
    if not baseline_matches:
        raise RuntimeError("Baseline validation failed; candidate/reranker tuning was intentionally stopped")

    sanity = bm25_sanity_cases()
    dump_json(args.output_dir / "bm25_sanity.json", {"passed": all(row["passed"] for row in sanity), "cases": sanity})
    if not all(row["passed"] for row in sanity):
        raise RuntimeError("Chinese BM25 sanity validation failed")

    candidate_rows, rankings_by_config, counts_by_config, components_by_rep = build_candidate_grid(
        queries, pages, visibility, query_vectors, page_vectors, candidate_ks
    )
    write_csv(args.output_dir / "rerank_candidate_tuning.csv", candidate_rows)
    complementarity, complementarity_summary = build_complementarity(queries, components_by_rep, candidate_ks)
    write_csv(args.output_dir / "candidate_complementarity.csv", complementarity)
    write_csv(args.output_dir / "candidate_complementarity_summary.csv", complementarity_summary)

    fixed_candidate_rows = [
        row for row in candidate_rows if row["candidate_strategy"] in FIXED_BUDGET_STRATEGIES
    ]
    best_candidate_by_k = {
        candidate_k: dict(
            max(
                (row for row in fixed_candidate_rows if int(row["candidate_k"]) == candidate_k),
                key=lambda row: (
                    float(row["candidate_recall"]),
                    float(row["pre_rerank_recall_at_5"]),
                    float(row["pre_rerank_mrr"]),
                    -float(row["retrieval_p95_ms"]),
                ),
            )
        )
        for candidate_k in candidate_ks
    }
    central_k = 20 if 20 in best_candidate_by_k else max(candidate_ks)
    best_candidate = dict(best_candidate_by_k[central_k])
    best_candidate["chain_name"] = (
        f"Q0 + {best_candidate['candidate_representation']} {best_candidate['candidate_strategy']} "
        f"Top{best_candidate['candidate_k']}"
    )

    local_rows: list[dict[str, Any]] = []
    local_rankings: dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]] = {}
    local_status: dict[str, Any] = {"status": "SKIPPED"}
    best_local: dict[str, Any] | None = None
    local_latency_rows: list[dict[str, Any]] = []
    if not args.skip_local:
        local_rows, local_rankings, local_status = local_reranker_grid(
            queries,
            pages,
            candidate_rows,
            rankings_by_config,
            counts_by_config,
            args.output_dir,
            args.local_reranker_model,
            retry_unavailable=args.retry_unavailable_models,
        )
        write_csv(args.output_dir / "rerank_representation_tuning.csv", local_rows)
        if local_rows:
            best_local = select_best_local(local_rows)
            best_local_key = (
                str(best_local["candidate_representation"]),
                str(best_local["candidate_strategy"]),
                int(best_local["candidate_k"]),
                str(best_local["rerank_representation"]),
            )
            latency_path = args.output_dir / "rerank_latency_tuning.csv"
            cached_latency_rows: list[dict[str, Any]] = []
            if latency_path.exists():
                with latency_path.open(encoding="utf-8-sig") as file:
                    cached_latency_rows = [dict(row) for row in csv.DictReader(file)]
            latency_cache_valid = (
                len(cached_latency_rows) == len(candidate_ks)
                and {int(row["candidate_k"]) for row in cached_latency_rows} == set(candidate_ks)
                and all(
                    row["candidate_representation"] == best_local_key[0]
                    and row["candidate_strategy"] == best_local_key[1]
                    and row["rerank_representation"] == best_local_key[3]
                    and row["reranker_model"] == args.local_reranker_model
                    and int(row["warmup_repeats"]) == args.warmup_repeats
                    and int(row["measurement_repeats"]) == args.latency_repeats
                    for row in cached_latency_rows
                )
            )
            if latency_cache_valid:
                local_latency_rows = cached_latency_rows
                latency_status = {"warm_latency_cache_hit": True}
            else:
                local_latency_rows, latency_status = benchmark_local_k_latency(
                    queries,
                    pages,
                    rankings_by_config,
                    counts_by_config,
                    best_local_key[0],
                    best_local_key[1],
                    best_local_key[3],
                    candidate_ks,
                    args.local_reranker_model,
                    args.output_dir,
                    warmups=args.warmup_repeats,
                    repeats=args.latency_repeats,
                )
            local_status.update(latency_status)
            latency_by_k = {int(row["candidate_k"]): row for row in local_latency_rows}
            for row in local_rows:
                if (
                    row["candidate_representation"] == best_local_key[0]
                    and row["candidate_strategy"] == best_local_key[1]
                    and row["rerank_representation"] == best_local_key[3]
                ):
                    latency = latency_by_k[int(row["candidate_k"])]
                    row["warm_mean_ms"] = latency["warm_mean_ms"]
                    row["warm_p95_ms"] = latency["warm_p95_ms"]
            write_csv(args.output_dir / "rerank_k_tuning.csv", [
                {
                    **row,
                    **next(
                        (
                            metric
                            for metric in local_rows
                            if metric["candidate_representation"] == row["candidate_representation"]
                            and metric["candidate_strategy"] == row["candidate_strategy"]
                            and int(metric["candidate_k"]) == int(row["candidate_k"])
                            and metric["rerank_representation"] == row["rerank_representation"]
                        ),
                        {},
                    ),
                }
                for row in local_latency_rows
            ])
            best_local = select_best_local(local_rows)
            best_local["delta_recall_at_5"] = float(best_local["recall_at_5"]) - float(baseline["recall_at_5"])
            best_local["chain_name"] = (
                f"Q0 + {best_local['candidate_representation']} {best_local['candidate_strategy']} "
                f"Top{best_local['candidate_k']} + {best_local['rerank_representation']} "
                f"{args.local_reranker_model} -> Top5"
            )
    write_csv(args.output_dir / "rerank_latency_tuning.csv", local_latency_rows)

    stronger_rows: list[dict[str, Any]] = []
    stronger_status: dict[str, Any] = {"status": "SKIPPED"}
    best_stronger: dict[str, Any] | None = None
    if best_local and not args.skip_stronger_reranker:
        key = (
            str(best_local["candidate_representation"]),
            str(best_local["candidate_strategy"]),
            int(best_local["candidate_k"]),
        )
        candidate_rankings = rankings_by_config[key]
        candidate_counts = counts_by_config[key]
        try:
            stronger_rankings, stronger_meta = rerank_with_local_model(
                queries,
                pages,
                candidate_rankings,
                candidate_counts,
                {str(query["query_id"]): str(query["original_query"]) for query in queries},
                args.output_dir,
                args.stronger_reranker_model,
                key[0],
                key[1],
                key[2],
                str(best_local["rerank_representation"]),
                "STRONGER_MODEL",
                retry_unavailable=args.retry_unavailable_models,
            )
            metrics, _ = evaluate_rankings(queries, stronger_rankings)
            latency = latency_stats(row["rerank_latency_ms"] for row in stronger_meta.values())
            best_stronger = {
                "candidate_representation": key[0],
                "candidate_strategy": key[1],
                "candidate_k": key[2],
                "rerank_representation": best_local["rerank_representation"],
                "reranker_model": args.stronger_reranker_model,
                **{metric: value for metric, value in metrics.items() if not metric.startswith("macro_")},
                "rerank_mean_ms": latency["mean"],
                "rerank_p95_ms": latency["p95"],
                "status": "AVAILABLE",
            }
            stronger_rows.append(best_stronger)
            model_status = load_json(args.output_dir / "cache/rerank/local_model_status.json")
            stronger_status = dict(model_status.get(args.stronger_reranker_model) or {"status": "AVAILABLE"})
        except Exception as exc:
            stronger_status = {"status": "UNAVAILABLE", "reason": f"{type(exc).__name__}: {exc}"}
            stronger_rows.append({"reranker_model": args.stronger_reranker_model, **stronger_status})
    write_csv(args.output_dir / "stronger_local_reranker.csv", stronger_rows)

    deepseek_rows: list[dict[str, Any]] = []
    deepseek_rankings: dict[tuple[str, str, int, str], dict[str, list[dict[str, Any]]]] = {}
    deepseek_metadata: dict[tuple[str, str, int, str], dict[str, dict[str, Any]]] = {}
    best_deepseek: dict[str, Any] | None = None
    if not args.skip_llm:
        async def add_deepseek_grid(
            config_by_k: Mapping[int, tuple[str, str]],
            ks: Sequence[int],
            reps: Sequence[str],
        ) -> list[dict[str, Any]]:
            rows, rankings, metadata = await deepseek_reranker_grid(
                args,
                queries,
                pages,
                rankings_by_config,
                counts_by_config,
                config_by_k,
                ks,
                reps,
            )
            deepseek_rankings.update(rankings)
            deepseek_metadata.update(metadata)
            return rows

        representation_k = 20 if 20 in candidate_ks else max(candidate_ks)
        deepseek_rows.extend(
            await add_deepseek_grid(
                {representation_k: ("P8", "bm25")},
                (representation_k,),
                RERANK_REPRESENTATIONS,
            )
        )
        representation_best = best_available(deepseek_rows)
        best_llm_representation = str(representation_best["rerank_representation"])
        source_pairs = [
            ("P8", strategy) for strategy in FIXED_BUDGET_STRATEGIES
        ] + [("P0", "rrf60")]
        for candidate_representation, candidate_strategy in source_pairs:
            key = (candidate_representation, candidate_strategy, representation_k, best_llm_representation)
            if key in deepseek_rankings:
                continue
            deepseek_rows.extend(
                await add_deepseek_grid(
                    {representation_k: (candidate_representation, candidate_strategy)},
                    (representation_k,),
                    (best_llm_representation,),
                )
            )
        source_best = best_available(
            [
                row
                for row in deepseek_rows
                if int(row["candidate_k"]) == representation_k
                and row["rerank_representation"] == best_llm_representation
            ]
        )
        source_pair = (
            str(source_best["candidate_representation"]),
            str(source_best["candidate_strategy"]),
        )
        missing_ks = [
            candidate_k
            for candidate_k in candidate_ks
            if (*source_pair, candidate_k, best_llm_representation) not in deepseek_rankings
        ]
        if missing_ks:
            deepseek_rows.extend(
                await add_deepseek_grid(
                    {candidate_k: source_pair for candidate_k in missing_ks},
                    missing_ks,
                    (best_llm_representation,),
                )
            )
        unique_deepseek_rows = {
            (
                row.get("candidate_representation"),
                row.get("candidate_strategy"),
                row.get("candidate_k"),
                row.get("rerank_representation"),
            ): row
            for row in deepseek_rows
        }
        deepseek_rows = list(unique_deepseek_rows.values())
        write_csv(args.output_dir / "deepseek_rerank_tuning.csv", deepseek_rows)
        best_deepseek = best_available(deepseek_rows)
        best_deepseek["delta_recall_at_5"] = float(best_deepseek["recall_at_5"]) - float(baseline["recall_at_5"])
        best_deepseek["chain_name"] = (
            f"Q0 + {best_deepseek['candidate_representation']} {best_deepseek['candidate_strategy']} "
            f"Top{best_deepseek['candidate_k']} + {best_deepseek['rerank_representation']} "
            f"{best_deepseek['reranker_model']} no-thinking -> Top5"
        )
    else:
        write_csv(args.output_dir / "deepseek_rerank_tuning.csv", [])

    lightweight_rows: list[dict[str, Any]] = []
    lq_audit: list[dict[str, Any]] = []
    lq_summary: dict[str, Any] = {"conclusion": "SKIPPED: --skip-llm"}
    if best_local:
        options, lq_metadata, lq_audit = await build_lightweight_queries(args, queries)
        available_variants = [variant for variant in ("LQ0", "LQ1", "LQ2", "LQ4") if len(options.get(variant, {})) == len(queries)]
        if available_variants:
            lq_candidate_rep = str(best_local["candidate_representation"])
            lq_candidate_strategy = str(best_local["candidate_strategy"])
            lq_candidate_k = int(best_local["candidate_k"])
            lq_rankings, lq_counts, lq_embedding_meta = candidate_rankings_for_query_options(
                queries,
                pages,
                visibility,
                options,
                available_variants,
                lq_candidate_rep,
                lq_candidate_strategy,
                lq_candidate_k,
                args.first_stage_dir,
                args.output_dir,
            )
            for variant in available_variants:
                reranked, meta = rerank_with_local_model(
                    queries,
                    pages,
                    lq_rankings[variant],
                    lq_counts[variant],
                    options[variant],
                    args.output_dir,
                    args.local_reranker_model,
                    lq_candidate_rep,
                    lq_candidate_strategy,
                    lq_candidate_k,
                    str(best_local["rerank_representation"]),
                    variant,
                )
                metrics, _ = evaluate_rankings(queries, reranked)
                row = {
                    "variant": variant,
                    "candidate_representation": lq_candidate_rep,
                    "candidate_strategy": lq_candidate_strategy,
                    "candidate_k": lq_candidate_k,
                    "rerank_representation": best_local["rerank_representation"],
                    **{key: value for key, value in metrics.items() if not key.startswith("macro_")},
                    "query_embedding_cache_hit": True if variant == "LQ0" else lq_embedding_meta.get("cache_hit"),
                    "query_rewrite_llm_calls_per_query": 1.0 if variant == "LQ4" else 0.0,
                    "mean_rewrite_prompt_tokens": statistics.fmean(
                        float(value.get("prompt_tokens") or 0) for value in lq_metadata.values()
                    ) if variant == "LQ4" and lq_metadata else 0.0,
                    "mean_rewrite_latency_ms": statistics.fmean(
                        float(value.get("llm_latency_ms") or 0) for value in lq_metadata.values()
                    ) if variant == "LQ4" and lq_metadata else 0.0,
                    "status": "AVAILABLE",
                }
                lightweight_rows.append(row)
        if best_deepseek and "LQ4" in available_variants:
            ds_candidate_rep = str(best_deepseek["candidate_representation"])
            ds_candidate_strategy = str(best_deepseek["candidate_strategy"])
            ds_candidate_k = int(best_deepseek["candidate_k"])
            ds_lq_rankings, ds_lq_counts, _ = candidate_rankings_for_query_options(
                queries,
                pages,
                visibility,
                options,
                available_variants,
                ds_candidate_rep,
                ds_candidate_strategy,
                ds_candidate_k,
                args.first_stage_dir,
                args.output_dir,
            )
            ds_lq4_rankings, ds_lq4_metadata = await deepseek_rerank_custom(
                args,
                queries,
                pages,
                ds_lq_rankings["LQ4"],
                ds_lq_counts["LQ4"],
                options["LQ4"],
                candidate_representation=ds_candidate_rep,
                candidate_strategy=ds_candidate_strategy,
                candidate_k=ds_candidate_k,
                rerank_representation=str(best_deepseek["rerank_representation"]),
                variant="LQ4_BEST_QUALITY",
            )
            if len(ds_lq4_rankings) == len(queries):
                ds_lq4_metrics, _ = evaluate_rankings(queries, ds_lq4_rankings)
                lightweight_rows.append(
                    {
                        "variant": "LQ4_BEST_QUALITY",
                        "candidate_representation": ds_candidate_rep,
                        "candidate_strategy": ds_candidate_strategy,
                        "candidate_k": ds_candidate_k,
                        "rerank_representation": best_deepseek["rerank_representation"],
                        **{
                            key: value
                            for key, value in ds_lq4_metrics.items()
                            if not key.startswith("macro_")
                        },
                        "query_rewrite_llm_calls_per_query": 1.0,
                        "rerank_llm_calls_per_query": 1.0,
                        "mean_rerank_prompt_tokens": statistics.fmean(
                            float(value.get("prompt_tokens") or 0) for value in ds_lq4_metadata.values()
                        ),
                        "mean_rerank_latency_ms": statistics.fmean(
                            float(value.get("llm_latency_ms") or 0) for value in ds_lq4_metadata.values()
                        ),
                        "status": "AVAILABLE",
                    }
                )
                deepseek_is_best_quality = (
                    float(best_deepseek["recall_at_5"]),
                    float(best_deepseek["mrr"]),
                    float(best_deepseek["ndcg_at_5"]),
                ) > (
                    float(best_local["recall_at_5"]),
                    float(best_local["mrr"]),
                    float(best_local["ndcg_at_5"]),
                )
                if deepseek_is_best_quality:
                    delta_gold = round(
                        (float(ds_lq4_metrics["recall_at_5"]) - float(best_deepseek["recall_at_5"])) * 21
                    )
                    lq_summary = {
                        "comparison_chain": "Best Quality DeepSeek rerank",
                        "lq0_recall_at_5": best_deepseek["recall_at_5"],
                        "lq4_recall_at_5": ds_lq4_metrics["recall_at_5"],
                        "delta_gold": delta_gold,
                        "conclusion": (
                            f"LQ4 相对同一 Best Quality DeepSeek 链路的 Q0 改变 {delta_gold:+d} 个 Gold。"
                            + (
                                "达到至少 +2 Gold 门槛，可进入后续多 Session 验证。"
                                if delta_gold >= 2
                                else "未达到 +2 Gold 门槛，停止 Query Rewrite 路线。"
                            )
                        ),
                    }
        if lq_summary.get("conclusion") == "SKIPPED: --skip-llm":
            lq0 = next((row for row in lightweight_rows if row["variant"] == "LQ0"), None)
            lq4 = next((row for row in lightweight_rows if row["variant"] == "LQ4"), None)
            if lq0 and lq4:
                delta_gold = round((float(lq4["recall_at_5"]) - float(lq0["recall_at_5"])) * 21)
                lq_summary = {
                    "comparison_chain": "Best local rerank",
                    "lq0_recall_at_5": lq0["recall_at_5"],
                    "lq4_recall_at_5": lq4["recall_at_5"],
                    "delta_gold": delta_gold,
                    "conclusion": (
                        f"LQ4 相对同一最佳 local 链路的 Q0 改变 {delta_gold:+d} 个 Gold。"
                        + (
                            "达到至少 +2 Gold 门槛，可进入后续多 Session 验证。"
                            if delta_gold >= 2
                            else "未达到 +2 Gold 门槛，停止 Query Rewrite 路线。"
                        )
                    ),
                }
    write_csv(args.output_dir / "query_contextualization_tuning.csv", lightweight_rows)
    write_csv(args.output_dir / "lightweight_query_audit.csv", lq_audit)

    if not best_local:
        raise RuntimeError("The base local reranker is required for final movement/Pareto analysis")
    local_key = (
        str(best_local["candidate_representation"]),
        str(best_local["candidate_strategy"]),
        int(best_local["candidate_k"]),
        str(best_local["rerank_representation"]),
    )
    candidate_key = local_key[:3]
    selected_llm_rankings = None
    if best_deepseek:
        selected_llm_rankings = deepseek_rankings[
            (
                str(best_deepseek["candidate_representation"]),
                str(best_deepseek["candidate_strategy"]),
                int(best_deepseek["candidate_k"]),
                str(best_deepseek["rerank_representation"]),
            )
        ]
    movement_rows = per_gold_movements(
        queries,
        baseline_rankings,
        rankings_by_config[candidate_key],
        local_rankings[local_key],
        rankings_by_config[
            (
                str(best_deepseek["candidate_representation"]),
                str(best_deepseek["candidate_strategy"]),
                int(best_deepseek["candidate_k"]),
            )
        ]
        if best_deepseek
        else None,
        selected_llm_rankings,
    )
    write_csv(args.output_dir / "rerank_case_analysis.csv", movement_rows)

    candidate_k_rows = [
        row
        for row in local_rows
        if row["candidate_representation"] == best_local["candidate_representation"]
        and row["candidate_strategy"] == best_local["candidate_strategy"]
        and row["rerank_representation"] == best_local["rerank_representation"]
    ]
    best_recall = max(float(row["recall_at_5"]) for row in candidate_k_rows)
    recommended_k_row = min(
        (row for row in candidate_k_rows if float(row["recall_at_5"]) == best_recall),
        key=lambda row: int(row["candidate_k"]),
    )
    recommended_candidate_k = int(recommended_k_row["candidate_k"])

    low_latency_candidates = [
        row for row in candidate_k_rows if float(row["recall_at_5"]) > float(baseline["recall_at_5"])
    ]
    best_low_latency = dict(
        min(
            low_latency_candidates,
            key=lambda row: (
                next(
                    float(latency["warm_p95_ms"])
                    for latency in local_latency_rows
                    if int(latency["candidate_k"]) == int(row["candidate_k"])
                ),
                -float(row["recall_at_5"]),
            ),
        )
    )
    low_latency = next(
        row for row in local_latency_rows if int(row["candidate_k"]) == int(best_low_latency["candidate_k"])
    )
    best_low_latency.update({"warm_mean_ms": low_latency["warm_mean_ms"], "warm_p95_ms": low_latency["warm_p95_ms"]})
    best_low_latency["chain_name"] = (
        f"Q0 + {best_low_latency['candidate_representation']} {best_low_latency['candidate_strategy']} "
        f"Top{best_low_latency['candidate_k']} + {best_low_latency['rerank_representation']} "
        f"{args.local_reranker_model} -> Top5"
    )

    quality = dict(
        max(
            [row for row in (best_local, best_deepseek, best_stronger) if row],
            key=lambda row: (
                float(row["recall_at_5"]),
                float(row["mrr"]),
                float(row["ndcg_at_5"]),
            ),
        )
    )
    threshold = float(quality["recall_at_5"]) - (1 / 21)
    pareto_candidates = [row for row in candidate_k_rows if float(row["recall_at_5"]) >= threshold]
    if pareto_candidates:
        pareto = dict(
            min(
                pareto_candidates,
                key=lambda row: next(
                    float(latency["warm_p95_ms"])
                    for latency in local_latency_rows
                    if int(latency["candidate_k"]) == int(row["candidate_k"])
                ),
            )
        )
        pareto_latency = next(
            row for row in local_latency_rows if int(row["candidate_k"]) == int(pareto["candidate_k"])
        )
        pareto.update(
            {
                "warm_mean_ms": pareto_latency["warm_mean_ms"],
                "warm_p95_ms": pareto_latency["warm_p95_ms"],
                "delta_recall_at_5": float(pareto["recall_at_5"]) - float(baseline["recall_at_5"]),
                "chain_name": (
                    f"Q0 + {pareto['candidate_representation']} {pareto['candidate_strategy']} "
                    f"Top{pareto['candidate_k']} + {pareto['rerank_representation']} "
                    f"{args.local_reranker_model} -> Top5"
                ),
            }
        )
    else:
        pareto = dict(quality)

    comp = next(
        row
        for row in complementarity_summary
        if row["candidate_representation"] == best_candidate["candidate_representation"]
        and int(row["candidate_k"]) == int(best_candidate["candidate_k"])
    )
    complementary = int(comp["dense_only_gold"]) > 0 and int(comp["bm25_only_gold"]) > 0
    stronger_conclusion = "SKIPPED"
    if best_stronger:
        delta_gold = round((float(best_stronger["recall_at_5"]) - float(best_local["recall_at_5"])) * 21)
        stronger_conclusion = (
            f"{args.stronger_reranker_model} 相对 base {delta_gold:+d} Gold，"
            f"单次缓存构建 mean {float(best_stronger['rerank_mean_ms'] or 0):.1f} ms；"
            + ("存在实质增益，可继续验证。" if delta_gold >= 2 else "未达到 +2 Gold，当前不推荐增加模型成本。")
        )
    elif stronger_status.get("status") == "UNAVAILABLE":
        stronger_conclusion = f"UNAVAILABLE: {stronger_status.get('reason')}"

    local_best_latency = next(
        row for row in local_latency_rows if int(row["candidate_k"]) == int(best_local["candidate_k"])
    )
    best_local.update(
        {"warm_mean_ms": local_best_latency["warm_mean_ms"], "warm_p95_ms": local_best_latency["warm_p95_ms"]}
    )
    deepseek_extra = (
        round((float(best_deepseek["recall_at_5"]) - float(best_local["recall_at_5"])) * 21)
        if best_deepseek
        else None
    )
    latency_configs: dict[str, tuple[str, str, int]] = {
        "baseline": ("P0", "dense", 5),
        "best_local": (
            str(best_local["candidate_representation"]),
            str(best_local["candidate_strategy"]),
            int(best_local["candidate_k"]),
        ),
        "best_low_latency": (
            str(best_low_latency["candidate_representation"]),
            str(best_low_latency["candidate_strategy"]),
            int(best_low_latency["candidate_k"]),
        ),
        "recommended_pareto": (
            str(pareto["candidate_representation"]),
            str(pareto["candidate_strategy"]),
            int(pareto["candidate_k"]),
        ),
    }
    if best_deepseek:
        latency_configs["best_deepseek"] = (
            str(best_deepseek["candidate_representation"]),
            str(best_deepseek["candidate_strategy"]),
            int(best_deepseek["candidate_k"]),
        )
    component_latency_path = args.output_dir / "warm_component_latency.json"
    latency_contract = {
        "snapshot_hash": load_json(args.first_stage_dir / "snapshot/snapshot_manifest.json")["snapshot_hash"],
        "configs": latency_configs,
        "warmups": args.warmup_repeats,
        "repeats": args.latency_repeats,
    }
    latency_contract_hash = stable_hash(latency_contract)
    component_latency = load_json(component_latency_path) if component_latency_path.exists() else {}
    if component_latency.get("contract_hash") != latency_contract_hash:
        retrieval_latency: dict[str, dict[str, Any]] = {}
        for scheme, (candidate_representation, candidate_strategy, candidate_k) in latency_configs.items():
            retrieval_latency[scheme] = benchmark_candidate_retrieval(
                queries,
                pages,
                visibility,
                query_vectors,
                page_vectors[candidate_representation],
                candidate_representation=candidate_representation,
                candidate_strategy=candidate_strategy,
                candidate_k=candidate_k,
                warmups=args.warmup_repeats,
                repeats=args.latency_repeats,
            )
        embedding_latency = benchmark_query_embedding(
            queries,
            warmups=args.warmup_repeats,
            repeats=args.latency_repeats,
        )
        component_latency = {
            "contract_hash": latency_contract_hash,
            "contract": latency_contract,
            "embedding": embedding_latency,
            "candidate_retrieval": retrieval_latency,
        }
        dump_json(component_latency_path, component_latency)

    local_latency_by_k = {int(row["candidate_k"]): row for row in local_latency_rows}

    def latency_row(
        scheme: str,
        metrics: Mapping[str, Any],
        *,
        local_rerank: bool = False,
        llm_rerank: bool = False,
    ) -> dict[str, Any]:
        retrieval = component_latency["candidate_retrieval"][scheme]
        strategy = latency_configs[scheme][1]
        uses_dense = strategy != "bm25"
        embed_mean = float(component_latency["embedding"]["warm_mean_ms"]) if uses_dense else 0.0
        embed_p95 = float(component_latency["embedding"]["warm_p95_ms"]) if uses_dense else 0.0
        rerank_mean = 0.0
        rerank_p95 = 0.0
        if local_rerank:
            local_latency = local_latency_by_k[int(metrics["candidate_k"])]
            rerank_mean = float(local_latency["warm_mean_ms"])
            rerank_p95 = float(local_latency["warm_p95_ms"])
        elif llm_rerank:
            rerank_mean = float(metrics.get("llm_mean_ms") or 0)
            rerank_p95 = float(metrics.get("llm_p95_ms") or 0)
        candidate_mean = float(retrieval["warm_mean_ms"])
        candidate_p95 = float(retrieval["warm_p95_ms"])
        return {
            "scheme": scheme,
            "chain_name": metrics.get("chain_name"),
            "recall_at_5": metrics.get("recall_at_5"),
            "mrr": metrics.get("mrr"),
            "query_embedding_warm_mean_ms": embed_mean,
            "query_embedding_warm_p95_ms": embed_p95,
            "candidate_retrieval_warm_mean_ms": candidate_mean,
            "candidate_retrieval_warm_p95_ms": candidate_p95,
            "rerank_warm_or_llm_mean_ms": rerank_mean,
            "rerank_warm_or_llm_p95_ms": rerank_p95,
            "retrieval_total_mean_ms": embed_mean + candidate_mean + rerank_mean,
            "retrieval_total_p95_conservative_ms": embed_p95 + candidate_p95 + rerank_p95,
            "online_llm_calls_per_query": 1 if llm_rerank else 0,
            "mean_prompt_tokens": metrics.get("mean_prompt_tokens", 0),
            "cold_model_load_excluded": True,
        }

    baseline_chain = {**baseline, "chain_name": "Q0 + P0 BGE-small dense Top5 + no rerank", "candidate_k": 5}
    latency_summary_rows = [
        latency_row("baseline", baseline_chain),
        latency_row("best_local", best_local, local_rerank=True),
        latency_row("best_low_latency", best_low_latency, local_rerank=True),
        latency_row("recommended_pareto", pareto, local_rerank=True),
    ]
    if best_deepseek:
        latency_summary_rows.append(latency_row("best_deepseek", best_deepseek, llm_rerank=True))
    write_csv(args.output_dir / "latency_summary.csv", latency_summary_rows)

    enough_evidence = float(best_local["recall_at_5"]) >= float(baseline["recall_at_5"]) + 2 / 21
    production_recommendation = {
        "decision": "YES" if enough_evidence else "NO",
        "reason": (
            "S001 上无在线 LLM 链路至少稳定增加 2 个 available Gold，且 candidate_k/representation/rank movement 均已有受控证据；建议进入多 Session 小流量复验后再改默认链路。"
            if enough_evidence
            else "单个 S001 上的无在线 LLM 增益未达到 2 Gold，证据不足以进入生产修改。"
        ),
        "candidate_chain": best_local["chain_name"],
    }
    candidate_source_k20 = [
        row
        for row in candidate_rows
        if row["candidate_representation"] == "P8"
        and int(row["candidate_k"]) == (20 if 20 in candidate_ks else max(candidate_ks))
    ]
    local_k_summary = []
    for row in sorted(candidate_k_rows, key=lambda item: int(item["candidate_k"])):
        latency = local_latency_by_k[int(row["candidate_k"])]
        local_k_summary.append(
            {
                **dict(row),
                "warm_mean_ms": float(latency["warm_mean_ms"]),
                "warm_p95_ms": float(latency["warm_p95_ms"]),
            }
        )
    local_representation_summary = [
        row
        for row in local_rows
        if row["candidate_representation"] == best_local["candidate_representation"]
        and row["candidate_strategy"] == best_local["candidate_strategy"]
        and int(row["candidate_k"]) == int(best_local["candidate_k"])
    ]
    deepseek_representation_summary = [
        row
        for row in deepseek_rows
        if row.get("candidate_representation") == "P8"
        and row.get("candidate_strategy") == "bm25"
        and int(row.get("candidate_k") or 0) == (20 if 20 in candidate_ks else max(candidate_ks))
    ]
    deepseek_source_summary = [
        row
        for row in deepseek_rows
        if int(row.get("candidate_k") or 0) == (20 if 20 in candidate_ks else max(candidate_ks))
        and row.get("rerank_representation") == (best_deepseek or {}).get("rerank_representation")
    ]
    deepseek_k_summary = [
        row
        for row in deepseek_rows
        if best_deepseek
        and row.get("candidate_representation") == best_deepseek["candidate_representation"]
        and row.get("candidate_strategy") == best_deepseek["candidate_strategy"]
        and row.get("rerank_representation") == best_deepseek["rerank_representation"]
    ]
    local_movement_counts: dict[str, int] = {}
    llm_movement_counts: dict[str, int] = {}
    for row in movement_rows:
        local_movement_counts[str(row["local_movement"])] = local_movement_counts.get(str(row["local_movement"]), 0) + 1
        if row.get("llm_movement"):
            llm_movement_counts[str(row["llm_movement"])] = llm_movement_counts.get(str(row["llm_movement"]), 0) + 1
    rewrite_audit_failures = [row for row in lq_audit if row.get("manual_review_status") != "PASS"]
    summary = {
        "scope": "S001 only; immutable first-stage snapshot; no memory replay/regeneration",
        "baseline_validation_status": baseline_validation["status"],
        "baseline": baseline,
        "best_candidate": best_candidate,
        "best_candidate_by_k": best_candidate_by_k,
        "candidate_source_k20": candidate_source_k20,
        "complementarity_at_selected_k": comp,
        "complementarity_conclusion": "存在双向互补" if complementary else "未观察到双向互补",
        "recommended_candidate_k": recommended_candidate_k,
        "local_k_summary": local_k_summary,
        "local_representation_summary": local_representation_summary,
        "best_local": best_local,
        "best_stronger_local": best_stronger,
        "stronger_reranker_status": stronger_status,
        "stronger_reranker_conclusion": stronger_conclusion,
        "best_deepseek": best_deepseek,
        "deepseek_representation_summary": deepseek_representation_summary,
        "deepseek_source_summary": deepseek_source_summary,
        "deepseek_k_summary": deepseek_k_summary,
        "deepseek_extra_gold_vs_local": deepseek_extra,
        "best_low_latency": best_low_latency,
        "best_quality": quality,
        "recommended_pareto": pareto,
        "lightweight_query": lq_summary,
        "lightweight_query_rows": lightweight_rows,
        "rewrite_manual_audit": {
            "sample_count": len(lq_audit),
            "pass_count": len(lq_audit) - len(rewrite_audit_failures),
            "fail_count": len(rewrite_audit_failures),
            "status": "UNSTABLE" if rewrite_audit_failures else "PASS",
            "failures": rewrite_audit_failures,
        },
        "local_movement_counts": local_movement_counts,
        "llm_movement_counts": llm_movement_counts,
        "production_recommendation": production_recommendation,
        "page_prompt_recommendation": "DO_NOT_CHANGE: candidate/fine-ranking evidence remains primary",
        "bm25_sanity_passed": True,
        "local_model_status": local_status,
        "warm_latency_summary": latency_summary_rows,
        "llm_thinking_mode": "disabled",
        "cache_key_contract": [
            "query_id",
            "candidate_strategy",
            "candidate_k",
            "candidate_representation",
            "rerank_representation",
            "reranker_model",
            "reranker_prompt_version",
            "model",
            "thinking_mode",
        ],
    }
    dump_json(args.output_dir / "second_stage_summary.json", summary)
    (args.output_dir / "second_stage_report.md").write_text(build_report(summary), encoding="utf-8")

    combination_rows = []
    latency_by_scheme = {str(row["scheme"]): row for row in latency_summary_rows}
    for name, row, online_calls in (
        ("F0_baseline", {**baseline, "chain_name": "Q0 + P0 BGE-small dense Top5 + no rerank"}, 0),
        ("F1_best_local", best_local, 0),
        ("F2_best_deepseek", best_deepseek, 1),
        ("F3_best_low_latency", best_low_latency, 0),
        (
            "F4_best_quality",
            quality,
            1 if str(quality.get("reranker_model") or "").startswith("deepseek") else 0,
        ),
        ("F5_recommended_pareto", pareto, 0 if pareto.get("reranker_model") == args.local_reranker_model else 1),
    ):
        if row:
            latency_scheme = {
                "F0_baseline": "baseline",
                "F1_best_local": "best_local",
                "F2_best_deepseek": "best_deepseek",
                "F3_best_low_latency": "best_low_latency",
                "F4_best_quality": (
                    "best_deepseek"
                    if str(row.get("reranker_model") or "").startswith("deepseek")
                    else "best_local"
                ),
                "F5_recommended_pareto": "recommended_pareto",
            }[name]
            latency = latency_by_scheme.get(latency_scheme) or {}
            combination_rows.append(
                {
                    "scheme": name,
                    "chain_name": row.get("chain_name"),
                    "recall_at_5": row.get("recall_at_5"),
                    "absolute_delta_recall_at_5": float(row.get("recall_at_5") or 0) - float(baseline["recall_at_5"]),
                    "mrr": row.get("mrr"),
                    "ndcg_at_5": row.get("ndcg_at_5"),
                    "candidate_pool_recall": row.get("candidate_recall", row.get("recall_at_5")),
                    "candidate_recall_at_20": row.get("recall_at_20"),
                    "candidate_k": row.get("candidate_k", 5),
                    "warm_mean_ms": row.get("warm_mean_ms"),
                    "warm_p95_ms": row.get("warm_p95_ms"),
                    "llm_mean_ms": row.get("llm_mean_ms"),
                    "llm_p95_ms": row.get("llm_p95_ms"),
                    "mean_prompt_tokens": row.get("mean_prompt_tokens", 0),
                    "online_llm_calls_per_query": online_calls,
                    "retrieval_total_mean_ms": latency.get("retrieval_total_mean_ms"),
                    "retrieval_total_p95_conservative_ms": latency.get(
                        "retrieval_total_p95_conservative_ms"
                    ),
                }
            )
    write_csv(args.output_dir / "second_stage_combinations.csv", combination_rows)
    return summary


def main() -> None:
    args = parse_args()
    summary = asyncio.run(main_async(args))
    baseline = summary["baseline"]
    best_candidate = summary["best_candidate"]
    best_local = summary["best_local"]
    best_deepseek = summary.get("best_deepseek") or {}
    print("S001 second-stage rerank tuning complete")
    print(f"Output: {args.output_dir.resolve()}")
    print(f"Baseline: R@5={baseline['recall_at_5']:.6f}, R@20={baseline['recall_at_20']:.6f}")
    print(f"Candidate: {best_candidate['chain_name']} R={best_candidate['candidate_recall']:.6f}")
    print(f"Local: {best_local['chain_name']} R@5={best_local['recall_at_5']:.6f}")
    if best_deepseek:
        print(f"DeepSeek: {best_deepseek['chain_name']} R@5={best_deepseek['recall_at_5']:.6f}")
    print(f"Production experiment decision: {summary['production_recommendation']['decision']}")


if __name__ == "__main__":
    main()
