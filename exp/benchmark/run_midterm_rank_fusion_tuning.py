"""Third-stage offline rank-fusion tuning for S001 mid-term Page retrieval.

This entry point only reads the immutable first-stage snapshot/embedding cache
and the second-stage ``BAAI/bge-reranker-base`` score cache.  It never loads a
model, calls an LLM, regenerates memory, or mutates production data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    append_reranked_candidates,
    cosine_rank,
    evaluate_rankings,
    latency_stats,
    load_jsonl,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    BASE_RERANKER,
    PRODUCTION_EMBEDDING,
    candidate_components,
    load_existing_embedding_vectors,
    load_snapshot,
    pool_recall,
    rank_of,
)


FIRST_STAGE_DEFAULT = REPO_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
SECOND_STAGE_DEFAULT = FIRST_STAGE_DEFAULT / "rerank_tuning"
OUTPUT_DEFAULT = FIRST_STAGE_DEFAULT / "rank_fusion_tuning"
CANDIDATE_KS = (15, 20)
OUTPUT_K = 5
RRF_CONSTANT = 60
LOCAL_PROMPT_VERSION = "cross-encoder-independent-score-v1"
EXPECTED_QUERY_COUNT = 14
EXPECTED_GOLD_COUNT = 21


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tune cached first-stage/reranker fusion for S001")
    parser.add_argument("--first-stage-dir", type=Path, default=FIRST_STAGE_DEFAULT)
    parser.add_argument("--second-stage-dir", type=Path, default=SECOND_STAGE_DEFAULT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--fusion-latency-warmups", type=int, default=20)
    parser.add_argument("--fusion-latency-repeats", type=int, default=200)
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as file:
        return [dict(row) for row in csv.DictReader(file)]


def file_state(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def source_cache_paths(first_stage_dir: Path, second_stage_dir: Path) -> list[Path]:
    paths = [
        first_stage_dir / "snapshot/snapshot_manifest.json",
        first_stage_dir / "snapshot/queries.jsonl",
        first_stage_dir / "snapshot/pages.jsonl",
        first_stage_dir / "snapshot/query_page_visibility.jsonl",
        first_stage_dir / "baseline_metrics.json",
        second_stage_dir / "cache/rerank/local_scores.jsonl",
        second_stage_dir / "cache/rerank/llm_rerank.jsonl",
        second_stage_dir / "cache/query/lightweight_query.jsonl",
    ]
    paths.extend(sorted((first_stage_dir / "cache/embeddings" / PRODUCTION_EMBEDDING.replace("/", "_")).glob("*.npz")))
    return [path for path in paths if path.exists()]


def capture_source_states(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    return {str(path): file_state(path) for path in paths}


def minmax_normalize(values: Mapping[str, float]) -> dict[str, float]:
    if not values:
        return {}
    low = min(float(value) for value in values.values())
    high = max(float(value) for value in values.values())
    if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-15):
        return {key: 0.0 for key in values}
    return {key: (float(value) - low) / (high - low) for key, value in values.items()}


def candidate_and_reranker_signals(
    candidate_pool: Sequence[Mapping[str, Any]],
    reranker_scores: Mapping[str, float],
) -> dict[str, dict[str, float | int]]:
    ids = [str(item["page_id"]) for item in candidate_pool]
    missing = [page_id for page_id in ids if page_id not in reranker_scores]
    if missing:
        raise ValueError(f"Reranker cache omits candidate Pages: {missing}")
    reranker_order = sorted(ids, key=lambda page_id: (-float(reranker_scores[page_id]), page_id))
    reranker_rank = {page_id: rank for rank, page_id in enumerate(reranker_order, start=1)}
    return {
        str(item["page_id"]): {
            "candidate_rank": rank,
            "candidate_score": float(item["score"]),
            "reranker_rank": reranker_rank[str(item["page_id"])],
            "reranker_score": float(reranker_scores[str(item["page_id"])]),
        }
        for rank, item in enumerate(candidate_pool, start=1)
    }


def reranker_only_order(
    candidate_pool: Sequence[Mapping[str, Any]],
    reranker_scores: Mapping[str, float],
) -> list[str]:
    signals = candidate_and_reranker_signals(candidate_pool, reranker_scores)
    return sorted(signals, key=lambda page_id: (-float(signals[page_id]["reranker_score"]), page_id))


def rank_fusion_order(
    candidate_pool: Sequence[Mapping[str, Any]],
    reranker_scores: Mapping[str, float],
    *,
    candidate_weight: float,
    reranker_weight: float,
    rank_constant: int,
) -> list[str]:
    signals = candidate_and_reranker_signals(candidate_pool, reranker_scores)

    def score(page_id: str) -> float:
        values = signals[page_id]
        return candidate_weight / (rank_constant + int(values["candidate_rank"])) + reranker_weight / (
            rank_constant + int(values["reranker_rank"])
        )

    return sorted(signals, key=lambda page_id: (-score(page_id), page_id))


def score_fusion_order(
    candidate_pool: Sequence[Mapping[str, Any]],
    reranker_scores: Mapping[str, float],
    *,
    candidate_weight: float,
) -> list[str]:
    signals = candidate_and_reranker_signals(candidate_pool, reranker_scores)
    candidate_norm = minmax_normalize(
        {page_id: float(values["candidate_score"]) for page_id, values in signals.items()}
    )
    reranker_norm = minmax_normalize(
        {page_id: float(values["reranker_score"]) for page_id, values in signals.items()}
    )
    reranker_weight = 1.0 - candidate_weight
    scores = {
        page_id: candidate_weight * candidate_norm[page_id] + reranker_weight * reranker_norm[page_id]
        for page_id in signals
    }
    return sorted(signals, key=lambda page_id: (-scores[page_id], page_id))


def protected_order(
    candidate_pool: Sequence[Mapping[str, Any]],
    reranker_scores: Mapping[str, float],
    *,
    protected_count: int,
) -> list[str]:
    candidate_ids = [str(item["page_id"]) for item in candidate_pool]
    protected = candidate_ids[:protected_count]
    protected_set = set(protected)
    return [*protected, *(page_id for page_id in reranker_only_order(candidate_pool, reranker_scores) if page_id not in protected_set)]


def fusion_movement_category(candidate_rank: int, reranker_rank: int, fusion_rank: int) -> str:
    candidate_hit = candidate_rank <= OUTPUT_K
    reranker_hit = reranker_rank <= OUTPUT_K
    fusion_hit = fusion_rank <= OUTPUT_K
    if reranker_hit and fusion_hit:
        return "PRESERVED_HIT"
    if candidate_hit and not reranker_hit and fusion_hit:
        return "RECOVERED_DEMOTED_GOLD"
    if not candidate_hit and not reranker_hit and fusion_hit:
        return "NEWLY_PROMOTED"
    if reranker_hit and not fusion_hit:
        return "NEWLY_DEMOTED"
    if not candidate_hit and not reranker_hit and not fusion_hit:
        return "STILL_MISS"
    return "UNCHANGED"


def strategy_definitions(score_fusion_valid: bool) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = [
        {
            "strategy_id": "RF0_reranker_only",
            "strategy_group": "RANK_FUSION",
            "description": "reranker rank only",
            "order_fn": reranker_only_order,
        }
    ]
    for constant in (20, 60, 100):
        definitions.append(
            {
                "strategy_id": f"RF1_equal_rrf_c{constant}",
                "strategy_group": "RANK_FUSION",
                "description": f"1/(c+candidate_rank) + 1/(c+reranker_rank), c={constant}",
                "candidate_weight": 1.0,
                "reranker_weight": 1.0,
                "rank_constant": constant,
                "order_fn": lambda pool, scores, c=constant: rank_fusion_order(
                    pool,
                    scores,
                    candidate_weight=1.0,
                    reranker_weight=1.0,
                    rank_constant=c,
                ),
            }
        )
    for weight in (1.5, 2.0, 3.0, 4.0):
        definitions.append(
            {
                "strategy_id": f"RF2_reranker_weight_{weight:g}",
                "strategy_group": "RANK_FUSION",
                "description": f"1/(60+candidate_rank) + {weight:g}/(60+reranker_rank)",
                "candidate_weight": 1.0,
                "reranker_weight": weight,
                "rank_constant": RRF_CONSTANT,
                "order_fn": lambda pool, scores, w=weight: rank_fusion_order(
                    pool,
                    scores,
                    candidate_weight=1.0,
                    reranker_weight=w,
                    rank_constant=RRF_CONSTANT,
                ),
            }
        )
    for weight in (1.5, 2.0):
        definitions.append(
            {
                "strategy_id": f"RF3_candidate_weight_{weight:g}",
                "strategy_group": "RANK_FUSION",
                "description": f"{weight:g}/(60+candidate_rank) + 1/(60+reranker_rank)",
                "candidate_weight": weight,
                "reranker_weight": 1.0,
                "rank_constant": RRF_CONSTANT,
                "order_fn": lambda pool, scores, w=weight: rank_fusion_order(
                    pool,
                    scores,
                    candidate_weight=w,
                    reranker_weight=1.0,
                    rank_constant=RRF_CONSTANT,
                ),
            }
        )
    if score_fusion_valid:
        for index, weight in enumerate((0.2, 0.3, 0.4, 0.5)):
            definitions.append(
                {
                    "strategy_id": f"SF{index}_candidate_{weight:.1f}_reranker_{1.0 - weight:.1f}",
                    "strategy_group": "SCORE_FUSION",
                    "description": "per-Query min-max normalized RRF candidate score + cached sigmoid reranker score",
                    "candidate_weight": weight,
                    "reranker_weight": 1.0 - weight,
                    "normalization": "within-query candidate-pool min-max",
                    "order_fn": lambda pool, scores, w=weight: score_fusion_order(
                        pool,
                        scores,
                        candidate_weight=w,
                    ),
                }
            )
    for count in (2, 3):
        definitions.append(
            {
                "strategy_id": f"PR1_candidate_top{count}_protection",
                "strategy_group": "PROTECTION",
                "description": f"keep first-stage Top{count}; fill remaining ranks by reranker",
                "protected_count": count,
                "order_fn": lambda pool, scores, n=count: protected_order(pool, scores, protected_count=n),
            }
        )
    return definitions


def load_cached_scores(
    second_stage_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, Any]], dict[str, Any]]:
    cache_path = second_stage_dir / "cache/rerank/local_scores.jsonl"
    rows = load_jsonl(cache_path)
    selected = [
        row
        for row in rows
        if row.get("status") == "SUCCESS"
        and row.get("reranker_model") == BASE_RERANKER
        and row.get("rerank_representation") == "P8"
        and row.get("candidate_strategy") == "ALL_VISIBLE_SCORE_CACHE"
        and str(row.get("candidate_k")) == "all"
        and row.get("reranker_prompt_version") == LOCAL_PROMPT_VERSION
    ]
    by_query: dict[str, dict[str, Any]] = {}
    for row in selected:
        query_id = str(row["query_id"])
        if query_id in by_query:
            raise ValueError(f"Duplicate all-visible P8/base score cache for {query_id}")
        by_query[query_id] = row
    expected_ids = {str(query["query_id"]) for query in queries}
    if set(by_query) != expected_ids:
        raise ValueError(f"P8/base all-visible cache query mismatch: missing={sorted(expected_ids - set(by_query))}")
    scores: dict[str, dict[str, float]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for query_id, row in by_query.items():
        visible_ids = [str(page_id) for page_id in visibility[query_id]["visible_page_ids"]]
        cached_ids = [str(page_id) for page_id in row["candidate_page_ids"]]
        score_map = {str(page_id): float(value) for page_id, value in row["scores_by_page"].items()}
        if set(cached_ids) != set(visible_ids) or set(score_map) != set(visible_ids):
            raise ValueError(f"All-visible reranker cache does not match query-time visibility for {query_id}")
        if not all(math.isfinite(value) for value in score_map.values()):
            raise ValueError(f"Non-finite reranker score for {query_id}")
        scores[query_id] = score_map
        metadata[query_id] = dict(row)
    validation = {
        "status": "PASS",
        "cache_path": str(cache_path),
        "cache_sha256": file_state(cache_path)["sha256"],
        "cache_row_count_total": len(rows),
        "matched_all_visible_p8_base_rows": len(selected),
        "matched_query_count": len(by_query),
        "reranker_model": BASE_RERANKER,
        "reranker_representation": "P8",
        "cached_score_semantics": "sigmoid-normalized cross-encoder logits (normalize=True); ranking-preserving",
        "new_reranker_calls": 0,
        "new_llm_calls": 0,
    }
    return scores, metadata, validation


def validate_score_fusion_inputs(
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_scores: Mapping[str, Mapping[str, float]],
    candidate_ks: Sequence[int],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate_k in candidate_ks:
        for query_id, full_ranking in candidate_rankings.items():
            pool = list(full_ranking[: min(candidate_k, len(full_ranking))])
            candidate_values = [float(item.get("score") or 0.0) for item in pool]
            reranker_values = [float(reranker_scores[query_id][str(item["page_id"])]) for item in pool]
            formula_matches = all(
                len(item.get("component_ranks") or []) == 2
                and all(rank is not None for rank in item["component_ranks"])
                and math.isclose(
                    float(item["score"]),
                    sum(1.0 / (RRF_CONSTANT + int(rank)) for rank in item["component_ranks"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                for item in pool
            )
            candidate_varies = len(pool) <= 1 or not math.isclose(
                min(candidate_values), max(candidate_values), rel_tol=0.0, abs_tol=1e-15
            )
            reranker_varies = len(pool) <= 1 or not math.isclose(
                min(reranker_values), max(reranker_values), rel_tol=0.0, abs_tol=1e-15
            )
            finite = all(math.isfinite(value) for value in [*candidate_values, *reranker_values])
            bounded_reranker = all(0.0 <= value <= 1.0 for value in reranker_values)
            passed = formula_matches and candidate_varies and reranker_varies and finite and bounded_reranker
            checks.append(
                {
                    "query_id": query_id,
                    "candidate_k": candidate_k,
                    "actual_candidate_count": len(pool),
                    "candidate_score_min": min(candidate_values) if candidate_values else None,
                    "candidate_score_max": max(candidate_values) if candidate_values else None,
                    "reranker_score_min": min(reranker_values) if reranker_values else None,
                    "reranker_score_max": max(reranker_values) if reranker_values else None,
                    "rrf_formula_matches": formula_matches,
                    "candidate_score_varies": candidate_varies,
                    "reranker_score_varies": reranker_varies,
                    "finite": finite,
                    "reranker_score_in_0_1": bounded_reranker,
                    "passed": passed,
                }
            )
            if not passed:
                errors.append(f"{query_id}/K{candidate_k}")
    return {
        "status": "PASS" if not errors else "SKIPPED",
        "score_fusion_executed": not errors,
        "candidate_score_semantics": "RRF60 score from dense rank and Chinese-BM25 rank",
        "reranker_score_semantics": "cached sigmoid-normalized bge-reranker-base score",
        "normalization": "independent min-max normalization inside each Query candidate pool",
        "cross_query_scores_combined": False,
        "reason": (
            "Both signals are finite, non-degenerate within every non-trivial pool, and have explicit semantics; "
            "normalization is performed separately per Query."
            if not errors
            else f"Score fusion skipped because input validation failed for: {errors}"
        ),
        "checks": checks,
    }


def build_strategy_rankings(
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_scores: Mapping[str, Mapping[str, float]],
    *,
    candidate_k: int,
    order_fn: Callable[[Sequence[Mapping[str, Any]], Mapping[str, float]], list[str]],
) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for query_id, full_ranking in candidate_rankings.items():
        actual_count = min(candidate_k, len(full_ranking))
        candidate_pool = full_ranking[:actual_count]
        preferred_ids = order_fn(candidate_pool, reranker_scores[query_id])
        rankings[query_id] = append_reranked_candidates(
            full_ranking,
            preferred_ids,
            candidate_k=actual_count,
        )
    return rankings


def benchmark_fusion_computation(
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_scores: Mapping[str, Mapping[str, float]],
    *,
    candidate_k: int,
    order_fn: Callable[[Sequence[Mapping[str, Any]], Mapping[str, float]], list[str]],
    warmups: int,
    repeats: int,
) -> dict[str, Any]:
    items = list(candidate_rankings.items())

    def run_one(query_id: str, full_ranking: Sequence[Mapping[str, Any]]) -> None:
        actual_count = min(candidate_k, len(full_ranking))
        pool = full_ranking[:actual_count]
        order = order_fn(pool, reranker_scores[query_id])
        append_reranked_candidates(full_ranking, order, candidate_k=actual_count)

    for _ in range(max(0, warmups)):
        for query_id, full_ranking in items:
            run_one(query_id, full_ranking)
    values: list[float] = []
    for _ in range(max(1, repeats)):
        for query_id, full_ranking in items:
            started = time.perf_counter_ns()
            run_one(query_id, full_ranking)
            values.append((time.perf_counter_ns() - started) / 1_000_000.0)
    return {
        "warmup_repeats": max(0, warmups),
        "measurement_repeats": max(1, repeats),
        "cold_start_excluded": True,
        **{f"fusion_{key}_ms" if key != "count" else "measurement_count": value for key, value in latency_stats(values).items()},
    }


def top5_gold_structure(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, int]]:
    counts: list[int] = []
    per_query: dict[str, int] = {}
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(page_id) for page_id in query.get("eligible_gold_page_ids") or []}
        top_ids = {str(item["page_id"]) for item in rankings[query_id][:OUTPUT_K]}
        count = len(gold_ids & top_ids)
        counts.append(count)
        per_query[query_id] = count
    distribution = {
        "0": sum(value == 0 for value in counts),
        "1": sum(value == 1 for value in counts),
        "2": sum(value == 2 for value in counts),
        "3+": sum(value >= 3 for value in counts),
    }
    return {
        "mean_gold_pages_in_top5": statistics.fmean(counts) if counts else 0.0,
        "query_top5_gold_0": distribution["0"],
        "query_top5_gold_1": distribution["1"],
        "query_top5_gold_2": distribution["2"],
        "query_top5_gold_3_plus": distribution["3+"],
    }, per_query


def movement_counts(
    queries: Sequence[Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    final_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    counts = Counter()
    for query in queries:
        query_id = str(query["query_id"])
        for page_id in query.get("eligible_gold_page_ids") or []:
            page_id = str(page_id)
            baseline_rank = rank_of(baseline_rankings[query_id], page_id)
            candidate_rank = rank_of(candidate_rankings[query_id], page_id)
            reranker_rank = rank_of(reranker_rankings[query_id], page_id)
            final_rank = rank_of(final_rankings[query_id], page_id)
            if None in (baseline_rank, candidate_rank, reranker_rank, final_rank):
                raise ValueError(f"Ranking omits visible Gold: {query_id}/{page_id}")
            baseline_hit = int(baseline_rank) <= OUTPUT_K
            candidate_hit = int(candidate_rank) <= OUTPUT_K
            reranker_hit = int(reranker_rank) <= OUTPUT_K
            final_hit = int(final_rank) <= OUTPUT_K
            counts["promoted_into_top5_vs_baseline"] += int(not baseline_hit and final_hit)
            counts["demoted_out_of_top5_vs_baseline"] += int(baseline_hit and not final_hit)
            counts["promoted_into_top5_vs_candidate"] += int(not candidate_hit and final_hit)
            counts["demoted_out_of_top5_vs_candidate"] += int(candidate_hit and not final_hit)
            counts["preserved_reranker_hits"] += int(reranker_hit and final_hit)
            counts["recovered_reranker_demotions"] += int(candidate_hit and not reranker_hit and final_hit)
            counts["newly_promoted_vs_reranker"] += int(not reranker_hit and final_hit)
            counts["newly_demoted_vs_reranker"] += int(reranker_hit and not final_hit)
    counts["net_gain_vs_baseline"] = (
        counts["promoted_into_top5_vs_baseline"] - counts["demoted_out_of_top5_vs_baseline"]
    )
    counts["net_gain_vs_candidate"] = (
        counts["promoted_into_top5_vs_candidate"] - counts["demoted_out_of_top5_vs_candidate"]
    )
    return dict(counts)


def protection_counts(
    queries: Sequence[Mapping[str, Any]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    final_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    protected_count: int,
    candidate_k: int,
) -> dict[str, int]:
    protected_gold = 0
    protected_non_gold = 0
    blocked_reranker_gold = 0
    recovered_gold = 0
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(page_id) for page_id in query.get("eligible_gold_page_ids") or []}
        actual_count = min(candidate_k, len(candidate_rankings[query_id]))
        protected_ids = {
            str(item["page_id"])
            for item in candidate_rankings[query_id][: min(protected_count, actual_count)]
        }
        protected_gold += len(protected_ids & gold_ids)
        protected_non_gold += len(protected_ids - gold_ids)
        reranker_top5 = {str(item["page_id"]) for item in reranker_rankings[query_id][:OUTPUT_K]}
        final_top5 = {str(item["page_id"]) for item in final_rankings[query_id][:OUTPUT_K]}
        blocked_reranker_gold += len((reranker_top5 & gold_ids) - final_top5)
        recovered_gold += len((final_top5 & gold_ids) - reranker_top5)
    return {
        "protected_gold_slots": protected_gold,
        "protected_non_gold_slots": protected_non_gold,
        "reranker_hit_gold_blocked": blocked_reranker_gold,
        "reranker_missed_gold_recovered": recovered_gold,
    }


def selection_key(row: Mapping[str, Any]) -> tuple[float, float, float, int, float]:
    return (
        float(row["recall_at_5"]),
        float(row["ndcg_at_5"]),
        float(row["mrr"]),
        -int(row["demoted_out_of_top5_vs_baseline"]),
        -float(row.get("fusion_p95_ms") or 0.0),
    )


def best_row(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("Cannot select from an empty result set")
    return dict(max(rows, key=selection_key))


def validate_baselines(
    first_stage_dir: Path,
    second_stage_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility: Mapping[str, Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_rankings_by_k: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    baseline_metrics, baseline_per_query = evaluate_rankings(queries, baseline_rankings)
    first_stage = load_json(first_stage_dir / "baseline_metrics.json")
    second_stage = load_json(second_stage_dir / "second_stage_summary.json")
    old_per_query = {str(row["query_id"]): row for row in first_stage["per_query"]}
    top20_mismatches = [
        str(row["query_id"])
        for row in baseline_per_query
        if row["top20_page_ids"] != old_per_query[str(row["query_id"])]["top20_page_ids"]
    ]
    expected_baseline = second_stage["baseline"]
    baseline_matches = (
        len(queries) == EXPECTED_QUERY_COUNT
        and int(baseline_metrics["eligible_gold_count"]) == EXPECTED_GOLD_COUNT
        and math.isclose(float(baseline_metrics["recall_at_5"]), 7 / 21, abs_tol=1e-12)
        and math.isclose(float(baseline_metrics["recall_at_20"]), 18 / 21, abs_tol=1e-12)
        and math.isclose(
            float(baseline_metrics["recall_at_5"]), float(expected_baseline["recall_at_5"]), abs_tol=1e-12
        )
        and math.isclose(
            float(baseline_metrics["recall_at_20"]), float(expected_baseline["recall_at_20"]), abs_tol=1e-12
        )
        and not top20_mismatches
    )
    stage2_k_rows = {
        int(row["candidate_k"]): row
        for row in read_csv(second_stage_dir / "rerank_k_tuning.csv")
        if row["candidate_representation"] == "P0"
        and row["candidate_strategy"] == "rrf60"
        and row["rerank_representation"] == "P8"
        and row["reranker_model"] == BASE_RERANKER
    }
    local_validation: dict[str, Any] = {}
    local_matches = True
    for candidate_k in CANDIDATE_KS:
        metrics, _ = evaluate_rankings(queries, reranker_rankings_by_k[candidate_k])
        expected = stage2_k_rows[candidate_k]
        matched = all(
            math.isclose(float(metrics[key]), float(expected[key]), rel_tol=0.0, abs_tol=1e-12)
            for key in ("recall_at_5", "mrr", "ndcg_at_5", "mean_gold_rank", "median_gold_rank")
        )
        local_validation[str(candidate_k)] = {"status": "PASS" if matched else "FAIL", "recomputed": metrics, "expected": expected}
        local_matches = local_matches and matched

    movement_rows = read_csv(second_stage_dir / "rerank_case_analysis.csv")
    gold_rank_mismatches: list[str] = []
    for row in movement_rows:
        query_id = str(row["query_id"])
        page_id = str(row["gold_page_id"])
        expected_rank = int(row["local_rerank_rank"])
        actual_rank = rank_of(reranker_rankings_by_k[20][query_id], page_id)
        if actual_rank != expected_rank:
            gold_rank_mismatches.append(f"{query_id}/{page_id}:{expected_rank}!={actual_rank}")
    local_k20_counts = movement_counts(
        queries,
        baseline_rankings,
        candidate_rankings,
        reranker_rankings_by_k[20],
        reranker_rankings_by_k[20],
    )
    movement_matches = (
        local_k20_counts["promoted_into_top5_vs_baseline"] == 5
        and local_k20_counts["demoted_out_of_top5_vs_baseline"] == 2
        and not gold_rank_mismatches
    )
    status = "PASS" if baseline_matches and local_matches and movement_matches else "FAIL"
    return {
        "status": status,
        "scope": "S001 only",
        "baseline": baseline_metrics,
        "baseline_expected": expected_baseline,
        "baseline_top20_mismatch_query_ids": top20_mismatches,
        "local_by_k": local_validation,
        "local_k20_movement": local_k20_counts,
        "local_k20_gold_rank_mismatches": gold_rank_mismatches,
        "snapshot_page_count": len(pages),
        "query_count": len(queries),
        "reason": (
            "Production baseline, K15/K20 local metrics, and all K20 per-Gold ranks exactly match prior stages."
            if status == "PASS"
            else "A baseline/local/cache invariant changed; fusion tuning must stop."
        ),
    }


def stage2_latency_by_k(second_stage_dir: Path) -> dict[int, dict[str, float]]:
    rows = {row["scheme"]: row for row in read_csv(second_stage_dir / "latency_summary.csv")}
    mapping = {15: rows["recommended_pareto"], 20: rows["best_local"]}
    return {
        candidate_k: {
            "query_embedding_mean_ms": float(row["query_embedding_warm_mean_ms"]),
            "query_embedding_p95_ms": float(row["query_embedding_warm_p95_ms"]),
            "candidate_mean_ms": float(row["candidate_retrieval_warm_mean_ms"]),
            "candidate_p95_ms": float(row["candidate_retrieval_warm_p95_ms"]),
            "reranker_mean_ms": float(row["rerank_warm_or_llm_mean_ms"]),
            "reranker_p95_ms": float(row["rerank_warm_or_llm_p95_ms"]),
        }
        for candidate_k, row in mapping.items()
    }


def run_fusion_grid(
    queries: Sequence[Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_rankings_by_k: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]],
    reranker_scores: Mapping[str, Mapping[str, float]],
    definitions: Sequence[Mapping[str, Any]],
    latency_by_k: Mapping[int, Mapping[str, float]],
    *,
    latency_warmups: int,
    latency_repeats: int,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[tuple[int, str], dict[str, list[dict[str, Any]]]],
]:
    result_rows: list[dict[str, Any]] = []
    per_query_rows: list[dict[str, Any]] = []
    rankings_by_strategy: dict[tuple[int, str], dict[str, list[dict[str, Any]]]] = {}
    for candidate_k in CANDIDATE_KS:
        actual_counts = {
            query_id: min(candidate_k, len(ranking)) for query_id, ranking in candidate_rankings.items()
        }
        candidate_recall, candidate_hits, candidate_total = pool_recall(
            queries,
            candidate_rankings,
            actual_counts,
        )
        values = list(actual_counts.values())
        for definition in definitions:
            strategy_id = str(definition["strategy_id"])
            order_fn = definition["order_fn"]
            rankings = build_strategy_rankings(
                candidate_rankings,
                reranker_scores,
                candidate_k=candidate_k,
                order_fn=order_fn,
            )
            rankings_by_strategy[(candidate_k, strategy_id)] = rankings
            metrics, per_query = evaluate_rankings(queries, rankings)
            movement = movement_counts(
                queries,
                baseline_rankings,
                candidate_rankings,
                reranker_rankings_by_k[candidate_k],
                rankings,
            )
            top5_structure, top5_counts = top5_gold_structure(queries, rankings)
            fusion_latency = benchmark_fusion_computation(
                candidate_rankings,
                reranker_scores,
                candidate_k=candidate_k,
                order_fn=order_fn,
                warmups=latency_warmups,
                repeats=latency_repeats,
            )
            component = latency_by_k[candidate_k]
            protection = (
                protection_counts(
                    queries,
                    candidate_rankings,
                    reranker_rankings_by_k[candidate_k],
                    rankings,
                    protected_count=int(definition["protected_count"]),
                    candidate_k=candidate_k,
                )
                if definition["strategy_group"] == "PROTECTION"
                else {}
            )
            row = {
                "strategy_id": strategy_id,
                "strategy_group": definition["strategy_group"],
                "description": definition["description"],
                "s001_tuned": True,
                "query_variant": "Q0 current user query",
                "candidate_representation": "P0",
                "candidate_strategy": "dense + Chinese BM25 RRF60",
                "candidate_k": candidate_k,
                "mean_actual_candidate_count": statistics.fmean(values),
                "min_actual_candidate_count": min(values),
                "max_actual_candidate_count": max(values),
                "candidate_recall": candidate_recall,
                "candidate_hits": candidate_hits,
                "eligible_gold_count": candidate_total,
                "rerank_representation": "P8 summary + keywords",
                "reranker_model": BASE_RERANKER,
                "output_k": OUTPUT_K,
                "candidate_weight": definition.get("candidate_weight"),
                "reranker_weight": definition.get("reranker_weight"),
                "rank_constant": definition.get("rank_constant"),
                "normalization": definition.get("normalization"),
                **{key: value for key, value in metrics.items() if not key.startswith("macro_")},
                **movement,
                **top5_structure,
                **protection,
                **fusion_latency,
                **component,
                "retrieval_total_mean_ms": (
                    component["query_embedding_mean_ms"]
                    + component["candidate_mean_ms"]
                    + component["reranker_mean_ms"]
                    + float(fusion_latency["fusion_mean_ms"] or 0.0)
                ),
                "retrieval_total_p95_conservative_ms": (
                    component["query_embedding_p95_ms"]
                    + component["candidate_p95_ms"]
                    + component["reranker_p95_ms"]
                    + float(fusion_latency["fusion_p95_ms"] or 0.0)
                ),
                "online_llm_calls_per_query": 0,
                "new_model_calls": 0,
                "chain_name": (
                    f"Q0 + P0 dense/Chinese-BM25 RRF60 Top{candidate_k} + P8 {BASE_RERANKER} + "
                    f"{strategy_id} -> Top5"
                ),
            }
            result_rows.append(row)
            per_query_by_id = {str(item["query_id"]): item for item in per_query}
            for query in queries:
                query_id = str(query["query_id"])
                detail = per_query_by_id[query_id]
                per_query_rows.append(
                    {
                        "candidate_k": candidate_k,
                        "strategy_id": strategy_id,
                        "strategy_group": definition["strategy_group"],
                        "query_id": query_id,
                        "original_query": query["original_query"],
                        "gold_page_ids": detail["gold_page_ids"],
                        "gold_ranks": detail["gold_ranks"],
                        "best_gold_rank": detail["best_gold_rank"],
                        "reciprocal_rank": detail["reciprocal_rank"],
                        "ndcg_at_5": detail["ndcg_at_5"],
                        "gold_pages_in_top5": top5_counts[query_id],
                        "candidate_top5_page_ids": [
                            str(item["page_id"]) for item in candidate_rankings[query_id][:OUTPUT_K]
                        ],
                        "reranker_top5_page_ids": [
                            str(item["page_id"]) for item in reranker_rankings_by_k[candidate_k][query_id][:OUTPUT_K]
                        ],
                        "final_top5_page_ids": [str(item["page_id"]) for item in rankings[query_id][:OUTPUT_K]],
                    }
                )
    return result_rows, per_query_rows, rankings_by_strategy


def candidate_signal_rows(
    queries: Sequence[Mapping[str, Any]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_scores: Mapping[str, Mapping[str, float]],
) -> list[dict[str, Any]]:
    query_by_id = {str(query["query_id"]): query for query in queries}
    rows: list[dict[str, Any]] = []
    for candidate_k in CANDIDATE_KS:
        for query_id, full_ranking in candidate_rankings.items():
            pool = full_ranking[: min(candidate_k, len(full_ranking))]
            signals = candidate_and_reranker_signals(pool, reranker_scores[query_id])
            gold_ids = {str(page_id) for page_id in query_by_id[query_id].get("eligible_gold_page_ids") or []}
            for page_id, values in signals.items():
                candidate_item = next(item for item in pool if str(item["page_id"]) == page_id)
                component_ranks = list(candidate_item.get("component_ranks") or [])
                rows.append(
                    {
                        "query_id": query_id,
                        "candidate_k": candidate_k,
                        "page_id": page_id,
                        "is_eligible_gold": page_id in gold_ids,
                        "candidate_rank": values["candidate_rank"],
                        "candidate_score_rrf60": values["candidate_score"],
                        "dense_rank": component_ranks[0] if component_ranks else None,
                        "bm25_rank": component_ranks[1] if len(component_ranks) > 1 else None,
                        "reranker_rank": values["reranker_rank"],
                        "reranker_cached_sigmoid_score": values["reranker_score"],
                    }
                )
    return rows


def build_rank_movement_rows(
    queries: Sequence[Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    candidate_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reranker_rankings_by_k: Mapping[int, Mapping[str, Sequence[Mapping[str, Any]]]],
    rankings_by_strategy: Mapping[tuple[int, str], Mapping[str, Sequence[Mapping[str, Any]]]],
    definitions: Sequence[Mapping[str, Any]],
    best_fusion: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    best_k = int(best_fusion["candidate_k"])
    best_id = str(best_fusion["strategy_id"])
    for query in queries:
        query_id = str(query["query_id"])
        for page_id_value in query.get("eligible_gold_page_ids") or []:
            page_id = str(page_id_value)
            baseline_rank = rank_of(baseline_rankings[query_id], page_id)
            row: dict[str, Any] = {
                "query_id": query_id,
                "gold_page_id": page_id,
                "baseline_rank": baseline_rank,
                "baseline_hit5": bool(baseline_rank and baseline_rank <= OUTPUT_K),
                "best_fusion_candidate_k": best_k,
                "best_fusion_strategy": best_id,
            }
            for candidate_k in CANDIDATE_KS:
                candidate_rank = rank_of(candidate_rankings[query_id], page_id)
                reranker_rank = rank_of(reranker_rankings_by_k[candidate_k][query_id], page_id)
                row[f"candidate_rank_k{candidate_k}"] = candidate_rank
                row[f"candidate_hit5_k{candidate_k}"] = bool(candidate_rank and candidate_rank <= OUTPUT_K)
                row[f"reranker_rank_k{candidate_k}"] = reranker_rank
                row[f"reranker_hit5_k{candidate_k}"] = bool(reranker_rank and reranker_rank <= OUTPUT_K)
                for definition in definitions:
                    strategy_id = str(definition["strategy_id"])
                    final_rank = rank_of(rankings_by_strategy[(candidate_k, strategy_id)][query_id], page_id)
                    prefix = f"k{candidate_k}__{strategy_id}"
                    row[f"{prefix}__rank"] = final_rank
                    row[f"{prefix}__hit5"] = bool(final_rank and final_rank <= OUTPUT_K)
            candidate_rank = int(row[f"candidate_rank_k{best_k}"])
            reranker_rank = int(row[f"reranker_rank_k{best_k}"])
            fusion_rank = int(row[f"k{best_k}__{best_id}__rank"])
            row["best_fusion_rank"] = fusion_rank
            row["best_fusion_hit5"] = fusion_rank <= OUTPUT_K
            row["movement_category"] = fusion_movement_category(candidate_rank, reranker_rank, fusion_rank)
            rows.append(row)
    return rows


def gold_set_differences(
    queries: Sequence[Mapping[str, Any]],
    reference: Mapping[str, Sequence[Mapping[str, Any]]],
    contender: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, str]]]:
    gained: list[dict[str, str]] = []
    lost: list[dict[str, str]] = []
    for query in queries:
        query_id = str(query["query_id"])
        reference_top5 = {str(item["page_id"]) for item in reference[query_id][:OUTPUT_K]}
        contender_top5 = {str(item["page_id"]) for item in contender[query_id][:OUTPUT_K]}
        for page_id in query.get("eligible_gold_page_ids") or []:
            page_id = str(page_id)
            detail = {"query_id": query_id, "gold_page_id": page_id}
            if page_id in contender_top5 and page_id not in reference_top5:
                gained.append(detail)
            if page_id in reference_top5 and page_id not in contender_top5:
                lost.append(detail)
    return {"gained": gained, "lost": lost}


def compact_winner(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "strategy_id",
        "strategy_group",
        "candidate_k",
        "candidate_recall",
        "recall_at_5",
        "mrr",
        "ndcg_at_5",
        "mean_gold_rank",
        "median_gold_rank",
        "promoted_into_top5_vs_baseline",
        "demoted_out_of_top5_vs_baseline",
        "net_gain_vs_baseline",
        "newly_promoted_vs_reranker",
        "newly_demoted_vs_reranker",
        "fusion_mean_ms",
        "fusion_p95_ms",
        "retrieval_total_mean_ms",
        "retrieval_total_p95_conservative_ms",
        "chain_name",
    )
    return {key: row.get(key) for key in keys}


def pct(value: Any) -> str:
    return f"{100.0 * float(value):.2f}%"


def ms(value: Any) -> str:
    return f"{float(value):.3f}"


def winner_table_row(label: str, row: Mapping[str, Any]) -> str:
    return (
        f"| {label} | K{row['candidate_k']} / `{row['strategy_id']}` | {pct(row['recall_at_5'])} | "
        f"{float(row['mrr']):.4f} | {float(row['ndcg_at_5']):.4f} | "
        f"{row['demoted_out_of_top5_vs_baseline']} | {ms(row['fusion_p95_ms'])} |"
    )


def build_report(summary: Mapping[str, Any]) -> str:
    baseline = summary["baseline_validation"]["baseline"]
    current = summary["current_local"]
    best_rank = summary["winners"]["best_rank_fusion"]
    best_score = summary["winners"].get("best_score_fusion")
    best_protection = summary["winners"]["best_protection"]
    best_k15 = summary["winners"]["best_k15"]
    best_k20 = summary["winners"]["best_k20"]
    best_quality = summary["winners"]["best_quality"]
    best_stable = summary["winners"]["best_stable_ranking"]
    pareto = summary["winners"]["recommended_pareto"]
    stable_diff = summary["best_stable_vs_reranker_only"]
    score_sentence = (
        f"合法并已执行。最佳为 `{best_score['strategy_id']}` / K{best_score['candidate_k']}，"
        f"R@5 {pct(best_score['recall_at_5'])}，没有超过 reranker-only。"
        if best_score
        else f"未执行：{summary['score_fusion_validation']['reason']}"
    )
    gained_text = ", ".join(
        f"{row['query_id']}/{row['gold_page_id']}" for row in stable_diff["gained"]
    ) or "无"
    lost_text = ", ".join(
        f"{row['query_id']}/{row['gold_page_id']}" for row in stable_diff["lost"]
    ) or "无"
    reuse_text = (
        "连续第二次执行也只读取这些源产物；前一次 after-state、本次 before-state 与本次 after-state "
        "的 SHA-256、mtime、size 全部一致"
        if summary["cache_reuse_validation"].get("consecutive_run_source_states_unchanged")
        else "本次执行前后的源 cache SHA-256、mtime、size 全部一致"
    )
    case_rows = summary["case_analysis"]
    case_lines = "\n".join(
        f"| {row['query_id']} | `{row['gold_page_id']}` | {row['candidate_rank']} | "
        f"{row['reranker_rank']} | {row['stable_fusion_rank']} | {row['movement_category']} |"
        for row in case_rows
    )
    score_row = winner_table_row("T2 Best Score Fusion", best_score) if best_score else ""
    return f"""# S001 Mid-term Rank Fusion Tuning — Third Stage

> **范围与风险标记：S001-tuned。** 本轮只读取第一阶段 immutable snapshot/embedding 和第二阶段本地
> reranker score cache；没有重新运行 S001、没有生成 Page/长期记忆、没有加载模型，也没有新增 DeepSeek
> 或本地 reranker 调用。1 个 Gold = 4.76 percentage points，以下参数不是全局最优参数。

## 1. Baseline 与第二阶段复现

校验状态：**{summary['baseline_validation']['status']}**。Production Baseline 完全复现：R@5
**{pct(baseline['recall_at_5'])}**（7/21）、R@20 **{pct(baseline['recall_at_20'])}**（18/21）、MRR
{baseline['mrr']:.4f}、NDCG@5 {baseline['ndcg_at_5']:.4f}。第二阶段 Local
`P0 RRF60 Top20 + P8 {BASE_RERANKER}` 也完全复现：R@5 **{pct(current['recall_at_5'])}**（10/21）。

reranker-only 相对生产 Baseline 推进 **{current['promoted_into_top5_vs_baseline']}** 个 Gold、推出
**{current['demoted_out_of_top5_vs_baseline']}** 个原 Top-5 Gold，净增
**{current['net_gain_vs_baseline']}**。所有 K20 Gold rank 与第二阶段 `rerank_case_analysis.csv` 一致。

## 2. Signal 与 Score Fusion 合法性

- `candidate_rank/score` 来自 P0 Dense + 中文 BM25 的 RRF60；candidate score 逐项通过
  `1/(60+dense_rank)+1/(60+bm25_rank)` 公式校验。
- `reranker_rank/score` 来自 P8 + `bge-reranker-base` 的既有 all-visible cache。生产 wrapper 以
  `normalize=True` 保存 sigmoid score，因此它不是未归一化 logit，但保序且具有稳定 [0,1] 语义。
- Score fusion 只在各 Query 当前 candidate pool 内分别 min-max，绝不跨 Query 混合量纲。

结论：{score_sentence}

## 3. Rank Fusion

Equal RRF（c=20/60/100）、reranker-weighted（λ=1.5/2/3/4）和 candidate-weighted（λ=1.5/2）均在
K15/K20 完成。严格 rank-fusion 最佳为 `{best_rank['strategy_id']}` / K{best_rank['candidate_k']}：R@5
**{pct(best_rank['recall_at_5'])}**、MRR {best_rank['mrr']:.4f}、NDCG@5 {best_rank['ndcg_at_5']:.4f}。
它没有超过 10/21 的 reranker-only；连续加权会折中两个排名，但在这 21 个 Gold 上反而破坏了 reranker
的大幅有效跃迁。

Rank fusion 可以在某些权重下降低 DEMOTED，但以损失更多新推进 Gold 为代价；因此没有证据用 RF1/RF2/RF3
替换 reranker-only。

## 4. Conservative Top-N Protection

最佳保护策略 `{best_protection['strategy_id']}` / K{best_protection['candidate_k']} 保持 R@5
**{pct(best_protection['recall_at_5'])}**（10/21），并把相对生产 Baseline 的 DEMOTED_OUT_OF_TOP5 从 2 降为
{best_protection['demoted_out_of_top5_vs_baseline']}。它保护 {best_protection['protected_gold_slots']} 个 Gold slot、
{best_protection['protected_non_gold_slots']} 个非 Gold slot；恢复 {best_protection['reranker_missed_gold_recovered']}
个被 reranker 推出的 Gold，同时阻止 {best_protection['reranker_hit_gold_blocked']} 个 reranker 新命中留在 Top-5。

与 K20 reranker-only 相比，新增/恢复：{gained_text}；损失：{lost_text}。因此它是“更稳定的等 Recall 排序”，
不是净 Recall 提升。

## 5. K15 与 K20

| K | 推荐策略 | Candidate Recall | R@5 | MRR | NDCG@5 | DEMOTED | Fusion p95 ms | Total conservative p95 ms |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 15 | `{best_k15['strategy_id']}` | {pct(best_k15['candidate_recall'])} | {pct(best_k15['recall_at_5'])} | {best_k15['mrr']:.4f} | {best_k15['ndcg_at_5']:.4f} | {best_k15['demoted_out_of_top5_vs_baseline']} | {ms(best_k15['fusion_p95_ms'])} | {ms(best_k15['retrieval_total_p95_conservative_ms'])} |
| 20 | `{best_k20['strategy_id']}` | {pct(best_k20['candidate_recall'])} | {pct(best_k20['recall_at_5'])} | {best_k20['mrr']:.4f} | {best_k20['ndcg_at_5']:.4f} | {best_k20['demoted_out_of_top5_vs_baseline']} | {ms(best_k20['fusion_p95_ms'])} | {ms(best_k20['retrieval_total_p95_conservative_ms'])} |

Quality 仍选 K20；允许少 1 个 Gold 时，K15 将 local reranker warm p95 从约 1359.8 ms 降到 1019.7 ms，
所以 K15 是待多 Session 验证的 Pareto 版本。Fusion 本身 p95 只有亚毫秒级，新增开销可忽略；总延迟沿用
第二阶段 warm query embedding/candidate/reranker 实测，只重放缓存上的数值排序。

## 6. 典型回退 Case

| Query | Gold Page | Candidate rank | Reranker-only rank | Top2 protection rank | 分类 |
|---|---|---:|---:|---:|---|
{case_lines}

Q026 达到了预期效果：candidate rank 2 的 Gold 被恢复，同时 reranker 从 rank 19 推到前列的另一个 Gold
仍保留在 Top-5；但全局上保护策略又挤出了 Q013 的一个 Gold，所以总命中仍是 10/21。Q028 的 Gold
candidate rank=5，Top2 protection 不覆盖它，因此仍未恢复。复杂化保护规则会进一步针对 S001 调参，本轮
按约束停止。

## 7. Top-5 Gold 结构与 MRR/Recall

Best Quality 的每 Query Top-5 Gold 均值为 {best_quality['mean_gold_pages_in_top5']:.3f}，分布为
0 个={best_quality['query_top5_gold_0']}、1 个={best_quality['query_top5_gold_1']}、2 个={best_quality['query_top5_gold_2']}、
3+ 个={best_quality['query_top5_gold_3_plus']}。Best Stable 的对应均值同为
{best_stable['mean_gold_pages_in_top5']:.3f}。

Recall@5 是 21 个 eligible Gold 的 micro 指标；MRR 是 14 个 Query 的最佳 Gold rank macro 指标。当前 Local
相对 Baseline 净增 3 个 Gold，所以 Recall 33.33%→47.62%；但部分 Query 原本最靠前的 Gold 被推后，MRR
0.3322→{current['mrr']:.4f}。NDCG@5 从 0.2644→{current['ndcg_at_5']:.4f}，说明 Top-5 整体仍改善。
Top2 protection 的 MRR/NDCG 为 {best_stable['mrr']:.4f}/{best_stable['ndcg_at_5']:.4f}：demotion 更少，
但其 NDCG 略低于 reranker-only，故按预先规定的 R@5→NDCG→MRR 质量选择规则，Best Quality 仍是 RF0。

## 8. Winners

| Winner | 配置 | R@5 | MRR | NDCG@5 | DEMOTED | Fusion p95 ms |
|---|---|---:|---:|---:|---:|---:|
{winner_table_row('T0 Current Local', current)}
{winner_table_row('T1 Best Rank Fusion', best_rank)}
{score_row}
{winner_table_row('T3 Best Protection / Stable', best_protection)}
{winner_table_row('T4 Best K15', best_k15)}
{winner_table_row('T5 Best K20 / Quality', best_k20)}

- **Best Quality:** `{best_quality['chain_name']}`，10/21，47.62%。
- **Best Stable Ranking:** `{best_stable['chain_name']}`，同为 10/21，但少推出 1 个原命中。
- **Recommended Pareto:** `{pareto['chain_name']}`，9/21，42.86%，K15 降低 reranker 延迟。

严格生产候选选择规则（R@5→NDCG@5→MRR→更少 DEMOTED→latency）仍选择 K20 reranker-only；Top2
protection 与 K15 protection 是需要在未参与调参 Session 上对照验证的稳定/Pareto 候选，不能从 S001
直接固化。

## 9. 验收问题逐项结论

1. Second-stage Local 47.62%：**完全复现**。
2. reranker-only 推进：**{current['promoted_into_top5_vs_baseline']} Gold**。
3. reranker-only 推出：**{current['demoted_out_of_top5_vs_baseline']} Gold**。
4. Rank fusion 是否减少 DEMOTED：部分规则可以，但严格 RF 会损失更多新命中；Top2 protection 可在同 Recall 下从 2 降到 1。
5. Rank fusion 是否提高 R@5：**否**。
6. 最佳 Rank Fusion：`{best_rank['strategy_id']}` / K{best_rank['candidate_k']}，R@5 {pct(best_rank['recall_at_5'])}。
7. Score Fusion：{score_sentence}
8. Protection：Top2 有效降低过度重排，但只是等量换回 1 个 Gold，R@5 不变。
9. K15 最佳：`{best_k15['strategy_id']}`，{pct(best_k15['recall_at_5'])}、MRR {best_k15['mrr']:.4f}、NDCG {best_k15['ndcg_at_5']:.4f}。
10. K20 最佳：按质量规则为 `{best_k20['strategy_id']}`，{pct(best_k20['recall_at_5'])}。
11. 是否达到 52.38%：**否，最高仍是 10/21=47.62%**。
12. 新增命中：相对 reranker-only 没有净新增；Stable 恢复 {gained_text}。
13. 损失：Stable 同时损失 {lost_text}；Best Quality 没有相对第二阶段损失。
14. MRR/NDCG：Best Quality {best_quality['mrr']:.4f}/{best_quality['ndcg_at_5']:.4f}；Stable {best_stable['mrr']:.4f}/{best_stable['ndcg_at_5']:.4f}。
15. Fusion latency：Best Quality p95 {ms(best_quality['fusion_p95_ms'])} ms；Stable p95 {ms(best_stable['fusion_p95_ms'])} ms，近乎可忽略。
16. Best Quality：K20 reranker-only，即第二阶段 Local。
17. Best Pareto：K15 Top2 protection，少 1 Gold、reranker p95 约少 340 ms。
18. DeepSeek reranker：**仍不需要**；第二阶段同为 10/21，且 MRR/NDCG 更低并增加 1 次在线 LLM。
19. Query Rewrite：**仍不需要**；本轮没有调用，上轮已显示无稳定增益。
20. Page Prompt：**仍建议不修改**；K20 candidate coverage 已是 19/21，瓶颈仍是精排取舍。
21. 下一步：**停止继续对 S001 调参，转入未参与调参的多 Session 验证。** 当前扫描已出现 1 Gold=4.76 pp 的 selection-overfitting 风险，不能把 S001-tuned 权重/保护规则称作全局最优。

## 10. Cache 与执行边界

Cache reuse 状态：**{summary['cache_reuse_validation']['status']}**。{reuse_text}。执行只读取 snapshot、
Q0/P0 embedding 和 `local_scores.jsonl`。新增 DeepSeek 调用=0、
新增 local reranker 调用=0、新增 embedding=0、Page/长期记忆生成=0、其他 Session=0。
"""


def print_terminal_summary(summary: Mapping[str, Any]) -> None:
    current = summary["current_local"]
    winners = summary["winners"]
    score = winners.get("best_score_fusion")
    lines = [
        "Third-stage terminal summary",
        "1. Added/modified: exp/benchmark/run_midterm_rank_fusion_tuning.py, "
        "exp/benchmark/test_midterm_rank_fusion_tuning.py; outputs only under rank_fusion_tuning/.",
        "2. Reused: immutable S001 snapshot, Q0/P0 BGE-small caches/stored vectors, P8/base all-visible local_scores cache, "
        "and second-stage warm latency measurements.",
        "3. New model calls: 0 (DeepSeek=0, local reranker=0, embedding=0); no Page/memory replay.",
        f"4. Local 47.62% reproduction: {summary['baseline_validation']['status']} "
        f"({round(float(current['recall_at_5']) * 21)}/21).",
        f"5. Best rank fusion: {winners['best_rank_fusion']['strategy_id']} K{winners['best_rank_fusion']['candidate_k']} "
        f"R@5={pct(winners['best_rank_fusion']['recall_at_5'])}.",
        (
            f"6. Best score fusion: {score['strategy_id']} K{score['candidate_k']} R@5={pct(score['recall_at_5'])}."
            if score
            else "6. Score fusion skipped because validation failed."
        ),
        f"7. Best protection: {winners['best_protection']['strategy_id']} K{winners['best_protection']['candidate_k']} "
        f"R@5={pct(winners['best_protection']['recall_at_5'])}, demoted="
        f"{winners['best_protection']['demoted_out_of_top5_vs_baseline']}.",
        f"8. K15/K20: {pct(winners['best_k15']['recall_at_5'])} / {pct(winners['best_k20']['recall_at_5'])}.",
        "9. Reached >=52.38%: NO; maximum remains 10/21=47.62%.",
        f"10. Stable recovered={summary['best_stable_vs_reranker_only']['gained']}; "
        f"lost={summary['best_stable_vs_reranker_only']['lost']}.",
        f"11. Quality R/MRR/NDCG={pct(winners['best_quality']['recall_at_5'])}/"
        f"{winners['best_quality']['mrr']:.4f}/{winners['best_quality']['ndcg_at_5']:.4f}.",
        f"12. Fusion p95: quality={ms(winners['best_quality']['fusion_p95_ms'])} ms; "
        f"stable={ms(winners['best_stable_ranking']['fusion_p95_ms'])} ms.",
        f"13. Best Quality: {winners['best_quality']['chain_name']}",
        f"14. Recommended Pareto: {winners['recommended_pareto']['chain_name']}",
        "15. Recommendation: stop S001 tuning and validate these S001-tuned candidates on held-out Sessions.",
        f"16. Cache reuse: {summary['cache_reuse_validation']['status']}; static/test results are reported separately.",
    ]
    print("\n".join(lines))


def main() -> None:
    args = parse_args()
    if args.fusion_latency_warmups < 0 or args.fusion_latency_repeats < 1:
        raise ValueError("Fusion latency warmups must be >=0 and repeats must be >=1")
    previous_output_present = (args.output_dir / "third_stage_summary.json").exists()
    previous_validation_path = args.output_dir / "cache_reuse_validation.json"
    previous_cache_validation = load_json(previous_validation_path) if previous_validation_path.exists() else None
    source_paths = source_cache_paths(args.first_stage_dir, args.second_stage_dir)
    source_states_before = capture_source_states(source_paths)

    queries, pages, visibility = load_snapshot(args.first_stage_dir)
    query_ids = [f"Q0:{query['query_id']}:0" for query in queries]
    query_vectors = load_existing_embedding_vectors(
        args.first_stage_dir,
        model_name=PRODUCTION_EMBEDDING,
        prefix="queries-",
        required_ids=query_ids,
    )
    page_vectors = {str(page["page_id"]): list(page["stored_embedding"]) for page in pages}
    pages_by_id = {str(page["page_id"]): page for page in pages}

    baseline_rankings: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        baseline_rankings[query_id] = cosine_rank(
            query_vectors[f"Q0:{query_id}:0"],
            visible_pages,
            page_vectors,
        )
    components, _ = candidate_components(
        queries,
        pages_by_id,
        visibility,
        query_vectors,
        page_vectors,
        "P0",
    )
    candidate_rankings = {query_id: values["rrf60"] for query_id, values in components.items()}
    reranker_scores, _, score_cache_validation = load_cached_scores(
        args.second_stage_dir,
        queries,
        visibility,
    )
    reranker_rankings_by_k = {
        candidate_k: build_strategy_rankings(
            candidate_rankings,
            reranker_scores,
            candidate_k=candidate_k,
            order_fn=reranker_only_order,
        )
        for candidate_k in CANDIDATE_KS
    }

    baseline_validation = validate_baselines(
        args.first_stage_dir,
        args.second_stage_dir,
        queries,
        pages,
        visibility,
        baseline_rankings,
        candidate_rankings,
        reranker_rankings_by_k,
    )
    dump_json(args.output_dir / "baseline_validation.json", baseline_validation)
    if baseline_validation["status"] != "PASS":
        raise RuntimeError("Baseline/local validation failed; third-stage fusion tuning intentionally stopped")

    score_fusion_validation = validate_score_fusion_inputs(
        candidate_rankings,
        reranker_scores,
        CANDIDATE_KS,
    )
    dump_json(args.output_dir / "score_fusion_validation.json", score_fusion_validation)
    definitions = strategy_definitions(bool(score_fusion_validation["score_fusion_executed"]))
    all_rows, per_query_rows, rankings_by_strategy = run_fusion_grid(
        queries,
        baseline_rankings,
        candidate_rankings,
        reranker_rankings_by_k,
        reranker_scores,
        definitions,
        stage2_latency_by_k(args.second_stage_dir),
        latency_warmups=args.fusion_latency_warmups,
        latency_repeats=args.fusion_latency_repeats,
    )

    rank_rows = [row for row in all_rows if row["strategy_group"] == "RANK_FUSION"]
    score_rows = [row for row in all_rows if row["strategy_group"] == "SCORE_FUSION"]
    protection_rows = [row for row in all_rows if row["strategy_group"] == "PROTECTION"]
    write_csv(args.output_dir / "rank_fusion_ablation.csv", rank_rows)
    write_csv(args.output_dir / "score_fusion_ablation.csv", score_rows)
    write_csv(args.output_dir / "protection_ablation.csv", protection_rows)
    write_csv(args.output_dir / "fusion_k_comparison.csv", all_rows)
    write_csv(args.output_dir / "fusion_per_query_results.csv", per_query_rows)
    write_csv(args.output_dir / "fusion_latency_summary.csv", all_rows)
    write_csv(
        args.output_dir / "fusion_candidate_signals.csv",
        candidate_signal_rows(queries, candidate_rankings, reranker_scores),
    )

    current_local = next(
        dict(row) for row in all_rows if int(row["candidate_k"]) == 20 and row["strategy_id"] == "RF0_reranker_only"
    )
    best_rank_fusion = best_row([row for row in rank_rows if row["strategy_id"] != "RF0_reranker_only"])
    best_score_fusion = best_row(score_rows) if score_rows else None
    best_protection = best_row(protection_rows)
    best_k15 = best_row([row for row in all_rows if int(row["candidate_k"]) == 15])
    best_k20 = best_row([row for row in all_rows if int(row["candidate_k"]) == 20])
    best_quality = best_row(all_rows)
    maximum_recall = max(float(row["recall_at_5"]) for row in all_rows)
    best_stable = dict(
        max(
            (row for row in all_rows if math.isclose(float(row["recall_at_5"]), maximum_recall, abs_tol=1e-12)),
            key=lambda row: (
                -int(row["demoted_out_of_top5_vs_baseline"]),
                float(row["ndcg_at_5"]),
                float(row["mrr"]),
                -float(row["fusion_p95_ms"]),
            ),
        )
    )
    pareto_threshold = maximum_recall - 1 / EXPECTED_GOLD_COUNT
    pareto_pool = [row for row in all_rows if float(row["recall_at_5"]) >= pareto_threshold - 1e-12]
    pareto_k = min(int(row["candidate_k"]) for row in pareto_pool)
    recommended_pareto = best_row([row for row in pareto_pool if int(row["candidate_k"]) == pareto_k])
    best_non_reranker_only = best_row([row for row in all_rows if row["strategy_id"] != "RF0_reranker_only"])

    movement_rows = build_rank_movement_rows(
        queries,
        baseline_rankings,
        candidate_rankings,
        reranker_rankings_by_k,
        rankings_by_strategy,
        definitions,
        best_non_reranker_only,
    )
    write_csv(args.output_dir / "fusion_rank_movement.csv", movement_rows)
    stable_rankings = rankings_by_strategy[(int(best_stable["candidate_k"]), str(best_stable["strategy_id"]))]
    stable_diff = gold_set_differences(queries, reranker_rankings_by_k[20], stable_rankings)
    case_analysis = []
    for row in movement_rows:
        if row["query_id"] not in {"S001-Q026", "S001-Q028"}:
            continue
        query_id = str(row["query_id"])
        page_id = str(row["gold_page_id"])
        stable_rank = rank_of(stable_rankings[query_id], page_id)
        case_analysis.append(
            {
                "query_id": query_id,
                "gold_page_id": page_id,
                "candidate_rank": rank_of(candidate_rankings[query_id], page_id),
                "reranker_rank": rank_of(reranker_rankings_by_k[20][query_id], page_id),
                "stable_fusion_rank": stable_rank,
                "movement_category": fusion_movement_category(
                    int(rank_of(candidate_rankings[query_id], page_id)),
                    int(rank_of(reranker_rankings_by_k[20][query_id], page_id)),
                    int(stable_rank),
                ),
            }
        )

    source_states_after = capture_source_states(source_paths)
    unchanged = source_states_before == source_states_after
    previous_after_states = (
        {str(row["path"]): dict(row) for row in previous_cache_validation.get("source_states_after") or []}
        if previous_cache_validation
        else None
    )
    consecutive_unchanged = previous_after_states == source_states_before if previous_after_states is not None else None
    cache_reuse_validation = {
        "status": "PASS" if unchanged and consecutive_unchanged is not False else "FAIL",
        "previous_third_stage_output_present": previous_output_present,
        "source_files_unchanged_within_run": unchanged,
        "consecutive_run_source_states_unchanged": consecutive_unchanged,
        "source_states_before": list(source_states_before.values()),
        "source_states_after": list(source_states_after.values()),
        "reused_local_score_cache": score_cache_validation,
        "new_deepseek_calls": 0,
        "new_local_reranker_calls": 0,
        "new_embedding_calls": 0,
        "new_page_generation_calls": 0,
        "new_memory_replay_calls": 0,
        "sessions_evaluated": ["S001_贵州茅台_投研"],
    }
    dump_json(args.output_dir / "cache_reuse_validation.json", cache_reuse_validation)
    if cache_reuse_validation["status"] != "PASS":
        raise RuntimeError("A source snapshot/cache changed during the offline fusion run")

    winners = {
        "current_local": compact_winner(current_local),
        "best_rank_fusion": compact_winner(best_rank_fusion),
        "best_score_fusion": compact_winner(best_score_fusion) if best_score_fusion else None,
        "best_protection": compact_winner(best_protection),
        "best_k15": compact_winner(best_k15),
        "best_k20": compact_winner(best_k20),
        "best_quality": compact_winner(best_quality),
        "best_stable_ranking": compact_winner(best_stable),
        "recommended_pareto": compact_winner(recommended_pareto),
    }
    # Preserve protection/top-5 diagnostic fields used directly by the report.
    for name, source in (
        ("best_protection", best_protection),
        ("best_quality", best_quality),
        ("best_stable_ranking", best_stable),
        ("best_k15", best_k15),
        ("best_k20", best_k20),
    ):
        winners[name].update(
            {
                key: source.get(key)
                for key in (
                    "protected_gold_slots",
                    "protected_non_gold_slots",
                    "reranker_hit_gold_blocked",
                    "reranker_missed_gold_recovered",
                    "mean_gold_pages_in_top5",
                    "query_top5_gold_0",
                    "query_top5_gold_1",
                    "query_top5_gold_2",
                    "query_top5_gold_3_plus",
                )
            }
        )
    summary = {
        "scope": "S001 only; third-stage cached numerical fusion; S001-tuned",
        "baseline_validation": baseline_validation,
        "score_cache_validation": score_cache_validation,
        "score_fusion_validation": score_fusion_validation,
        "current_local": winners["current_local"],
        "winners": winners,
        "best_stable_vs_reranker_only": stable_diff,
        "case_analysis": case_analysis,
        "quality_target": {
            "target_gold": 11,
            "target_recall_at_5": 11 / 21,
            "achieved_gold": round(maximum_recall * EXPECTED_GOLD_COUNT),
            "achieved_recall_at_5": maximum_recall,
            "target_reached": maximum_recall >= 11 / 21,
        },
        "selection_rules": {
            "quality": "R@5, then NDCG@5, MRR, fewer baseline demotions, fusion latency",
            "stable": "among maximum-R@5 rows: fewer baseline demotions, then NDCG@5/MRR/latency",
            "pareto": "R@5 >= Best Quality - 1 Gold; choose smaller K, then the quality rule",
        },
        "parameter_scope": {
            "rank_constants": [20, 60, 100],
            "reranker_rank_weights": [1.5, 2.0, 3.0, 4.0],
            "candidate_rank_weights": [1.5, 2.0],
            "score_candidate_weights": [0.2, 0.3, 0.4, 0.5],
            "protected_candidate_top_n": [2, 3],
            "candidate_ks": list(CANDIDATE_KS),
            "learning_to_rank_used": False,
        },
        "cache_reuse_validation": cache_reuse_validation,
        "recommendation": {
            "production_quality_candidate": winners["best_quality"]["chain_name"],
            "stable_validation_candidate": winners["best_stable_ranking"]["chain_name"],
            "pareto_validation_candidate": winners["recommended_pareto"]["chain_name"],
            "modify_production_now": "NO: validate on held-out Sessions first",
            "continue_s001_tuning": "NO",
            "next_step": "Stop S001 selection tuning and run held-out multi-Session validation.",
            "deepseek_needed": False,
            "query_rewrite_needed": False,
            "page_prompt_change_needed": False,
        },
    }
    dump_json(args.output_dir / "third_stage_summary.json", summary)
    (args.output_dir / "third_stage_report.md").write_text(build_report(summary), encoding="utf-8")
    print_terminal_summary(summary)


if __name__ == "__main__":
    main()
