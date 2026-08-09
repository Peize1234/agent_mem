"""Run the clean single-query replacement ablation for S001-S005.

No LLM or Session is run. The only new computation is embedding the cached old
standalone rewrite and cached conservative resolved query as single, unlabeled
query strings. Production P0 Page vectors and query-time visibility are reused.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from exp.benchmark.benchmark_common import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    cosine_rank,
    evaluate_rankings,
    load_jsonl,
    stable_hash,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    load_existing_embedding_vectors,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    MULTI_SESSION_DIR,
    S001_RESULT_DIR,
    SESSION_CODES,
    dump_json,
    load_snapshots,
    rank_map,
    read_csv,
    top_rows,
    write_csv,
)


SOURCE_RESULT_DIR = REPO_ROOT / "exp/results/reference_resolution_query_diagnosis"
OUTPUT_DIR = REPO_ROOT / "exp/results/query_rewrite_clean_replacement_ablation"
OUTPUT_K = 5
DISPLAY_K = 10
CASE_IDS = ("S001-Q033", "S003-Q035", "S003-Q044", "S004-Q052", "S004-Q063", "S005-Q042")
VARIANTS = ("baseline", "old_rewrite_only", "reference_resolution_only")
VECTOR_ATOL = 1e-6
VECTOR_RTOL = 1e-5
SCORE_ATOL = 1e-6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean single-query replacement ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def load_query_records(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    rows = load_jsonl(SOURCE_RESULT_DIR / "reference_resolution_queries.jsonl")
    by_query = {str(row["query_id"]): row for row in rows}
    expected = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    if len(rows) != 99 or set(by_query) != set(expected):
        raise ValueError(f"Reference-resolution Query coverage mismatch: {len(rows)} / {len(by_query)}")
    for query_id, row in by_query.items():
        if str(row["original_query"]) != str(expected[query_id]["original_query"]):
            raise ValueError(f"Original Query differs from immutable snapshot: {query_id}")
        if not str(row.get("old_standalone_rewrite") or "").strip():
            raise ValueError(f"Old standalone rewrite is missing: {query_id}")
        if not str(row.get("conservative_reference_resolution") or "").strip():
            raise ValueError(f"Resolved Query is missing: {query_id}")
    return by_query


def load_query_vectors(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    query_records: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[float]], EmbeddingCache, dict[str, Any]]:
    vectors: dict[str, list[float]] = {}
    s001_queries = snapshots["S001"]["queries"]
    s001_q0_ids = [f"Q0:{query['query_id']}:0" for query in s001_queries]
    vectors.update(
        load_existing_embedding_vectors(
            S001_RESULT_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="queries-",
            required_ids=s001_q0_ids,
        )
    )
    heldout_queries = [query for code in SESSION_CODES[1:] for query in snapshots[code]["queries"]]
    heldout_q0_ids = [f"Q0:{query['query_id']}:0" for query in heldout_queries]
    vectors.update(
        load_existing_embedding_vectors(
            MULTI_SESSION_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="heldout-Q0-",
            required_ids=heldout_q0_ids,
        )
    )

    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    old_ids = [f"QOLDONLY:{query['query_id']}:0" for query in all_queries]
    old_texts = [str(query_records[str(query["query_id"])]["old_standalone_rewrite"]) for query in all_queries]
    ref_ids = [f"QREFONLY:{query['query_id']}:0" for query in all_queries]
    ref_texts = [
        str(query_records[str(query["query_id"])]["conservative_reference_resolution"])
        for query in all_queries
    ]
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    old_vectors, old_metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "old-standalone-rewrite-only-S001-S005",
        old_ids,
        old_texts,
        measure_individual=True,
    )
    ref_vectors, ref_metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "conservative-reference-resolution-only-S001-S005",
        ref_ids,
        ref_texts,
        measure_individual=True,
    )
    vectors.update(old_vectors)
    vectors.update(ref_vectors)
    return vectors, cache, {
        "baseline_embedding_cache_reused": len(all_queries),
        "old_rewrite_only_new_embedding_count": len(old_ids),
        "reference_resolution_only_new_embedding_count": len(ref_ids),
        "old_rewrite_only_cache_hit": bool(old_metadata.get("cache_hit")),
        "reference_resolution_only_cache_hit": bool(ref_metadata.get("cache_hit")),
        "old_rewrite_only_batch": old_metadata,
        "reference_resolution_only_batch": ref_metadata,
        "old_rewrite_text_sha256": stable_hash(old_texts),
        "reference_resolution_text_sha256": stable_hash(ref_texts),
    }


def rank_snapshot(
    snapshot: Mapping[str, Any],
    vectors: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    pages = {str(page["page_id"]): page for page in snapshot["pages"]}
    page_vectors = {page_id: page["stored_embedding"] for page_id, page in pages.items()}
    visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
    rankings = {variant: {} for variant in VARIANTS}
    for query in snapshot["queries"]:
        query_id = str(query["query_id"])
        visible_pages = [pages[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        rankings["baseline"][query_id] = cosine_rank(vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors)
        rankings["old_rewrite_only"][query_id] = cosine_rank(
            vectors[f"QOLDONLY:{query_id}:0"], visible_pages, page_vectors
        )
        rankings["reference_resolution_only"][query_id] = cosine_rank(
            vectors[f"QREFONLY:{query_id}:0"], visible_pages, page_vectors
        )
    return rankings


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
    return float(left_array @ right_array / denominator) if denominator else 0.0


def validate_unchanged_queries(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_records: Mapping[str, Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    unchanged = [
        query
        for code in SESSION_CODES
        for query in snapshots[code]["queries"]
        if query_records[str(query["query_id"])]["conservative_reference_resolution"] == query["original_query"]
    ]
    if len(unchanged) != 86:
        raise AssertionError(f"Expected 86 UNCHANGED Queries from frozen source, got {len(unchanged)}")
    text_failures: list[str] = []
    vector_failures: list[dict[str, Any]] = []
    ranking_failures: list[dict[str, Any]] = []
    gold_failures: list[dict[str, Any]] = []
    cosines: list[float] = []
    max_vector_difference = 0.0
    max_ranking_score_difference = 0.0
    exact_vector_equal = 0
    exact_ranking_score_equal = 0
    for query in unchanged:
        query_id = str(query["query_id"])
        code = query_id[:4]
        original = str(query["original_query"])
        resolved = str(query_records[query_id]["conservative_reference_resolution"])
        if original != resolved:
            text_failures.append(query_id)
        baseline_vector = np.asarray(vectors[f"Q0:{query_id}:0"], dtype=np.float32)
        ref_vector = np.asarray(vectors[f"QREFONLY:{query_id}:0"], dtype=np.float32)
        similarity = cosine(baseline_vector, ref_vector)
        cosines.append(similarity)
        difference = float(np.max(np.abs(baseline_vector - ref_vector)))
        max_vector_difference = max(max_vector_difference, difference)
        exact_vector_equal += int(np.array_equal(baseline_vector, ref_vector))
        if not np.allclose(baseline_vector, ref_vector, rtol=VECTOR_RTOL, atol=VECTOR_ATOL) or not math.isclose(
            similarity, 1.0, rel_tol=1e-6, abs_tol=1e-6
        ):
            vector_failures.append(
                {"query_id": query_id, "cosine": similarity, "max_abs_difference": difference}
            )

        baseline = list(rankings[code]["baseline"][query_id])
        reference = list(rankings[code]["reference_resolution_only"][query_id])
        baseline_ids = [str(row["page_id"]) for row in baseline]
        reference_ids = [str(row["page_id"]) for row in reference]
        score_differences = [
            abs(float(before["score"]) - float(after["score"])) for before, after in zip(baseline, reference)
        ]
        score_difference = max(score_differences, default=0.0)
        max_ranking_score_difference = max(max_ranking_score_difference, score_difference)
        exact_ranking_score_equal += int(all(value == 0.0 for value in score_differences))
        if baseline_ids != reference_ids or any(value > SCORE_ATOL for value in score_differences):
            ranking_failures.append(
                {
                    "query_id": query_id,
                    "same_page_order": baseline_ids == reference_ids,
                    "max_abs_score_difference": score_difference,
                }
            )

        baseline_map = rank_map(baseline)
        reference_map = rank_map(reference)
        for page_id in map(str, query["eligible_gold_page_ids"]):
            before = baseline_map[page_id]
            after = reference_map[page_id]
            if int(before["rank"]) != int(after["rank"]) or not math.isclose(
                float(before["score"]), float(after["score"]), rel_tol=1e-6, abs_tol=SCORE_ATOL
            ):
                gold_failures.append(
                    {
                        "query_id": query_id,
                        "gold_page_id": page_id,
                        "baseline_rank": before["rank"],
                        "reference_rank": after["rank"],
                        "baseline_score": before["score"],
                        "reference_score": after["score"],
                    }
                )
    validation = {
        "unchanged_query_count": len(unchanged),
        "tolerances": {
            "vector_rtol": VECTOR_RTOL,
            "vector_atol": VECTOR_ATOL,
            "score_atol": SCORE_ATOL,
        },
        "validation_a_text_identity": "PASS" if not text_failures else "FAIL",
        "validation_b_vector_identity": "PASS" if not vector_failures else "FAIL",
        "validation_c_full_ranking_identity": "PASS" if not ranking_failures else "FAIL",
        "validation_d_gold_rank_score_identity": "PASS" if not gold_failures else "FAIL",
        "all_pass": not any((text_failures, vector_failures, ranking_failures, gold_failures)),
        "exact_vector_equal_count": exact_vector_equal,
        "exact_ranking_score_equal_count": exact_ranking_score_equal,
        "minimum_vector_cosine": min(cosines) if cosines else None,
        "maximum_vector_abs_difference": max_vector_difference,
        "maximum_ranking_score_abs_difference": max_ranking_score_difference,
        "failures": {
            "text": text_failures,
            "vector": vector_failures,
            "ranking": ranking_failures,
            "gold": gold_failures,
        },
    }
    if not validation["all_pass"]:
        raise AssertionError(f"UNCHANGED validation failed: {validation}")
    return validation


def variant_transition(baseline_hit: bool, variant_hit: bool) -> str:
    if not baseline_hit and variant_hit:
        return "RESCUED"
    if baseline_hit and not variant_hit:
        return "HURT"
    if baseline_hit:
        return "UNCHANGED_HIT"
    return "UNCHANGED_MISS"


def build_transitions(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_records: Mapping[str, Mapping[str, Any]],
    audits: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            changed = (
                str(query_records[query_id]["conservative_reference_resolution"])
                != str(query["original_query"])
            )
            gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
            maps = {variant: rank_map(rankings[code][variant][query_id]) for variant in VARIANTS}
            ranks = {
                variant: {page_id: int(maps[variant][page_id]["rank"]) for page_id in gold_ids}
                for variant in VARIANTS
            }
            hits = {variant: any(rank <= OUTPUT_K for rank in ranks[variant].values()) for variant in VARIANTS}
            old_transition = variant_transition(hits["baseline"], hits["old_rewrite_only"])
            ref_transition = variant_transition(hits["baseline"], hits["reference_resolution_only"])
            old_audit_counts = Counter(
                item["classification"] for item in audits[query_id]["old_rewrite"]["added_content"]
            )
            ref_audit_counts = Counter(
                item["classification"]
                for item in audits[query_id]["reference_resolution"]["added_content"]
            )
            query_rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "reference_group": "CHANGED" if changed else "UNCHANGED",
                    "baseline_hit5": hits["baseline"],
                    "old_rewrite_only_hit5": hits["old_rewrite_only"],
                    "reference_resolution_only_hit5": hits["reference_resolution_only"],
                    "old_rewrite_only_transition": old_transition,
                    "reference_resolution_only_transition": ref_transition,
                    "best_baseline_gold_rank": min(ranks["baseline"].values()),
                    "best_old_rewrite_only_gold_rank": min(ranks["old_rewrite_only"].values()),
                    "best_reference_resolution_only_gold_rank": min(
                        ranks["reference_resolution_only"].values()
                    ),
                    "old_reference_required_additions": old_audit_counts["REFERENCE_REQUIRED"],
                    "old_context_supported_not_required_additions": old_audit_counts[
                        "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                    ],
                    "ref_reference_required_additions": ref_audit_counts["REFERENCE_REQUIRED"],
                    "ref_context_supported_not_required_additions": ref_audit_counts[
                        "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                    ],
                }
            )
            for page_id in gold_ids:
                baseline_rank = ranks["baseline"][page_id]
                old_rank = ranks["old_rewrite_only"][page_id]
                ref_rank = ranks["reference_resolution_only"][page_id]
                gold_rows.append(
                    {
                        "session_id": code,
                        "query_id": query_id,
                        "reference_group": "CHANGED" if changed else "UNCHANGED",
                        "gold_page_id": page_id,
                        "baseline_rank": baseline_rank,
                        "old_rewrite_only_rank": old_rank,
                        "reference_resolution_only_rank": ref_rank,
                        "baseline_score": float(maps["baseline"][page_id]["score"]),
                        "old_rewrite_only_score": float(maps["old_rewrite_only"][page_id]["score"]),
                        "reference_resolution_only_score": float(
                            maps["reference_resolution_only"][page_id]["score"]
                        ),
                        "old_rewrite_only_transition": old_transition,
                        "reference_resolution_only_transition": ref_transition,
                        "old_promoted": baseline_rank > OUTPUT_K and old_rank <= OUTPUT_K,
                        "old_demoted": baseline_rank <= OUTPUT_K and old_rank > OUTPUT_K,
                        "ref_promoted": baseline_rank > OUTPUT_K and ref_rank <= OUTPUT_K,
                        "ref_demoted": baseline_rank <= OUTPUT_K and ref_rank > OUTPUT_K,
                        "old_rank_movement": baseline_rank - old_rank,
                        "ref_rank_movement": baseline_rank - ref_rank,
                        "old_reference_required_additions": old_audit_counts["REFERENCE_REQUIRED"],
                        "old_context_supported_not_required_additions": old_audit_counts[
                            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                        ],
                        "ref_reference_required_additions": ref_audit_counts["REFERENCE_REQUIRED"],
                        "ref_context_supported_not_required_additions": ref_audit_counts[
                            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                        ],
                    }
                )
    return gold_rows, query_rows


def movement_counts(rows: Sequence[Mapping[str, Any]], prefix: str) -> dict[str, int]:
    promoted = sum(bool(row[f"{prefix}_promoted"]) for row in rows)
    demoted = sum(bool(row[f"{prefix}_demoted"]) for row in rows)
    return {"promoted_gold": promoted, "demoted_gold": demoted, "net_gold_gain": promoted - demoted}


def evaluate_variant_subset(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    metrics, _ = evaluate_rankings(queries, rankings)
    return metrics


def build_metrics(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    for code in SESSION_CODES:
        metrics[code] = {
            variant: evaluate_variant_subset(snapshots[code]["queries"], rankings[code][variant])
            for variant in VARIANTS
        }
    pooled_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    metrics["Aggregate"] = {}
    for variant in VARIANTS:
        pooled_rankings = {
            query_id: ranking
            for code in SESSION_CODES
            for query_id, ranking in rankings[code][variant].items()
        }
        metrics["Aggregate"][variant] = evaluate_variant_subset(pooled_queries, pooled_rankings)
        metrics["Aggregate"][variant]["macro_session_recall_at_5"] = statistics.fmean(
            float(metrics[code][variant]["recall_at_5"]) for code in SESSION_CODES
        )
    return metrics


def build_session_summary(
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in (*SESSION_CODES, "Aggregate"):
        scoped_gold = list(gold_rows) if code == "Aggregate" else [row for row in gold_rows if row["session_id"] == code]
        scoped_queries = (
            list(query_rows) if code == "Aggregate" else [row for row in query_rows if row["session_id"] == code]
        )
        old_counts = movement_counts(scoped_gold, "old")
        ref_counts = movement_counts(scoped_gold, "ref")
        baseline = metrics[code]["baseline"]
        old = metrics[code]["old_rewrite_only"]
        ref = metrics[code]["reference_resolution_only"]
        rows.append(
            {
                "session_id": code,
                "eligible_gold_count": baseline["eligible_gold_count"],
                "baseline_r5": baseline["recall_at_5"],
                "old_rewrite_only_r5": old["recall_at_5"],
                "reference_resolution_only_r5": ref["recall_at_5"],
                "ref_vs_baseline_r5": float(ref["recall_at_5"]) - float(baseline["recall_at_5"]),
                "old_vs_baseline_r5": float(old["recall_at_5"]) - float(baseline["recall_at_5"]),
                "ref_vs_old_r5": float(ref["recall_at_5"]) - float(old["recall_at_5"]),
                "baseline_r10": baseline["recall_at_10"],
                "old_rewrite_only_r10": old["recall_at_10"],
                "reference_resolution_only_r10": ref["recall_at_10"],
                "baseline_r20": baseline["recall_at_20"],
                "old_rewrite_only_r20": old["recall_at_20"],
                "reference_resolution_only_r20": ref["recall_at_20"],
                "baseline_mrr": baseline["mrr"],
                "old_rewrite_only_mrr": old["mrr"],
                "reference_resolution_only_mrr": ref["mrr"],
                "baseline_mean_gold_rank": baseline["mean_gold_rank"],
                "old_rewrite_only_mean_gold_rank": old["mean_gold_rank"],
                "reference_resolution_only_mean_gold_rank": ref["mean_gold_rank"],
                "old_promoted_gold": old_counts["promoted_gold"],
                "old_demoted_gold": old_counts["demoted_gold"],
                "old_net_gold_gain": old_counts["net_gold_gain"],
                "ref_promoted_gold": ref_counts["promoted_gold"],
                "ref_demoted_gold": ref_counts["demoted_gold"],
                "ref_net_gold_gain": ref_counts["net_gold_gain"],
                "old_rescued_queries": sum(
                    row["old_rewrite_only_transition"] == "RESCUED" for row in scoped_queries
                ),
                "old_hurt_queries": sum(row["old_rewrite_only_transition"] == "HURT" for row in scoped_queries),
                "ref_rescued_queries": sum(
                    row["reference_resolution_only_transition"] == "RESCUED" for row in scoped_queries
                ),
                "ref_hurt_queries": sum(
                    row["reference_resolution_only_transition"] == "HURT" for row in scoped_queries
                ),
                "baseline_macro_session_r5": baseline.get("macro_session_recall_at_5"),
                "old_rewrite_only_macro_session_r5": old.get("macro_session_recall_at_5"),
                "reference_resolution_only_macro_session_r5": ref.get("macro_session_recall_at_5"),
            }
        )
    return rows


def group_summary(
    group_name: str,
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    query_ids = {str(query["query_id"]) for query in queries}
    scoped_gold = [row for row in gold_rows if row["query_id"] in query_ids]
    baseline_rankings = {
        query_id: rankings[query_id[:4]]["baseline"][query_id] for query_id in query_ids
    }
    ref_rankings = {
        query_id: rankings[query_id[:4]]["reference_resolution_only"][query_id] for query_id in query_ids
    }
    baseline, _ = evaluate_rankings(queries, baseline_rankings)
    ref, _ = evaluate_rankings(queries, ref_rankings)
    ref_counts = movement_counts(scoped_gold, "ref")
    return {
        "group": group_name,
        "query_count": len(queries),
        "eligible_gold_count": baseline["eligible_gold_count"],
        "baseline_r5": baseline["recall_at_5"],
        "reference_resolution_only_r5": ref["recall_at_5"],
        "delta_r5": float(ref["recall_at_5"]) - float(baseline["recall_at_5"]),
        **ref_counts,
    }


def rank_association(
    query_rows: Sequence[Mapping[str, Any]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    definitions = {
        "REFERENCE_REQUIRED_positive": lambda row, prefix: int(row[f"{prefix}_reference_required_additions"]) > 0,
        "REFERENCE_REQUIRED_ge_2": lambda row, prefix: int(row[f"{prefix}_reference_required_additions"]) >= 2,
        "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED_positive": lambda row, prefix: int(
            row[f"{prefix}_context_supported_not_required_additions"]
        )
        > 0,
    }
    for variant, prefix, movement_key, rank_key in (
        ("old_rewrite_only", "old", "old_rank_movement", "old_rewrite_only_rank"),
        ("reference_resolution_only", "ref", "ref_rank_movement", "reference_resolution_only_rank"),
    ):
        result[variant] = {}
        for cohort, predicate in definitions.items():
            ids = {str(row["query_id"]) for row in query_rows if predicate(row, prefix)}
            scoped = [row for row in gold_rows if str(row["query_id"]) in ids]
            result[variant][cohort] = {
                "query_count": len(ids),
                "eligible_gold_count": len(scoped),
                "mean_gold_rank_movement_baseline_minus_variant": (
                    statistics.fmean(float(row[movement_key]) for row in scoped) if scoped else None
                ),
                "baseline_r5": (
                    sum(int(row["baseline_rank"]) <= 5 for row in scoped) / len(scoped) if scoped else None
                ),
                "variant_r5": (
                    sum(int(row[rank_key]) <= 5 for row in scoped) / len(scoped) if scoped else None
                ),
            }
    return result


def movement_summary(gold_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for variant, rank_key, movement_key in (
        ("old_rewrite_only", "old_rewrite_only_rank", "old_rank_movement"),
        ("reference_resolution_only", "reference_resolution_only_rank", "ref_rank_movement"),
    ):
        values = [float(row[movement_key]) for row in gold_rows]
        result[variant] = {
            "rank_movement_baseline_minus_variant": {
                "count": len(values),
                "mean": statistics.fmean(values),
                "median": statistics.median(values),
                "p25": float(np.percentile(values, 25)),
                "p75": float(np.percentile(values, 75)),
            },
            "extreme_demotion_top5_to_beyond20": sum(
                int(row["baseline_rank"]) <= 5 and int(row[rank_key]) > 20 for row in gold_rows
            ),
            "extreme_promotion_beyond20_to_top5": sum(
                int(row["baseline_rank"]) > 20 and int(row[rank_key]) <= 5 for row in gold_rows
            ),
        }
    return result


def build_changed_summary(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_records: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    validation: Mapping[str, Any],
) -> dict[str, Any]:
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    changed = [
        query
        for query in all_queries
        if query_records[str(query["query_id"])]["conservative_reference_resolution"] != query["original_query"]
    ]
    unchanged = [query for query in all_queries if query not in changed]
    groups = {
        "CHANGED": group_summary("CHANGED", changed, rankings, gold_rows),
        "UNCHANGED": group_summary("UNCHANGED", unchanged, rankings, gold_rows),
    }
    unchanged_summary = groups["UNCHANGED"]
    if not (
        math.isclose(
            float(unchanged_summary["baseline_r5"]),
            float(unchanged_summary["reference_resolution_only_r5"]),
            abs_tol=1e-12,
        )
        and unchanged_summary["promoted_gold"] == 0
        and unchanged_summary["demoted_gold"] == 0
        and unchanged_summary["net_gold_gain"] == 0
    ):
        raise AssertionError(f"UNCHANGED aggregate invariant failed: {unchanged_summary}")
    return {
        "groups": groups,
        "unchanged_invariant": "PASS",
        "unchanged_validation": validation,
        "gold_rank_movement": movement_summary(gold_rows),
        "over_resolution_rank_association": rank_association(query_rows, gold_rows),
        "audit_sources_reused": {
            "reference_resolution_audit": str(SOURCE_RESULT_DIR / "reference_resolution_audit.jsonl"),
            "over_resolution_summary": str(SOURCE_RESULT_DIR / "over_resolution_summary.json"),
        },
    }


def build_cases(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_records: Mapping[str, Mapping[str, Any]],
    audits: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> list[dict[str, Any]]:
    query_lookup = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    cases: list[dict[str, Any]] = []
    for query_id in CASE_IDS:
        query = query_lookup[query_id]
        code = query_id[:4]
        pages = {str(page["page_id"]): page for page in snapshots[code]["pages"]}
        gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
        gold_set = set(gold_ids)
        variant_rankings = {variant: list(rankings[code][variant][query_id]) for variant in VARIANTS}
        maps = {variant: rank_map(ranking) for variant, ranking in variant_rankings.items()}
        detail_ids: list[str] = []
        for page_id in [
            *(str(item["page_id"]) for variant in VARIANTS for item in variant_rankings[variant][:OUTPUT_K]),
            *gold_ids,
        ]:
            if page_id not in detail_ids:
                detail_ids.append(page_id)
        record = query_records[query_id]
        cases.append(
            {
                "session_id": code,
                "query_id": query_id,
                "reference_group": (
                    "CHANGED"
                    if record["conservative_reference_resolution"] != record["original_query"]
                    else "UNCHANGED"
                ),
                "original_query": record["original_query"],
                "old_standalone_rewrite": record["old_standalone_rewrite"],
                "conservative_reference_resolution": record["conservative_reference_resolution"],
                "baseline_actual_embedding_text": record["original_query"],
                "old_rewrite_only_actual_embedding_text": record["old_standalone_rewrite"],
                "reference_resolution_only_actual_embedding_text": record[
                    "conservative_reference_resolution"
                ],
                "gold_page_ids": gold_ids,
                "baseline_top10": top_rows(variant_rankings["baseline"], gold_set, DISPLAY_K),
                "old_rewrite_only_top10": top_rows(
                    variant_rankings["old_rewrite_only"], gold_set, DISPLAY_K
                ),
                "reference_resolution_only_top10": top_rows(
                    variant_rankings["reference_resolution_only"], gold_set, DISPLAY_K
                ),
                "gold_comparison": [
                    {
                        "gold_page_id": page_id,
                        "baseline_rank": int(maps["baseline"][page_id]["rank"]),
                        "baseline_score": float(maps["baseline"][page_id]["score"]),
                        "old_rewrite_only_rank": int(maps["old_rewrite_only"][page_id]["rank"]),
                        "old_rewrite_only_score": float(maps["old_rewrite_only"][page_id]["score"]),
                        "reference_resolution_only_rank": int(
                            maps["reference_resolution_only"][page_id]["rank"]
                        ),
                        "reference_resolution_only_score": float(
                            maps["reference_resolution_only"][page_id]["score"]
                        ),
                    }
                    for page_id in gold_ids
                ],
                "page_details": [
                    {
                        "page_id": page_id,
                        "source_turn_id": pages[page_id]["source_turn_id"],
                        "is_gold": page_id in gold_set,
                        "baseline_rank": int(maps["baseline"][page_id]["rank"]),
                        "baseline_score": float(maps["baseline"][page_id]["score"]),
                        "old_rewrite_only_rank": int(maps["old_rewrite_only"][page_id]["rank"]),
                        "old_rewrite_only_score": float(maps["old_rewrite_only"][page_id]["score"]),
                        "reference_resolution_only_rank": int(
                            maps["reference_resolution_only"][page_id]["rank"]
                        ),
                        "reference_resolution_only_score": float(
                            maps["reference_resolution_only"][page_id]["score"]
                        ),
                        "production_p0_embedding_text": pages[page_id]["current_embedding_text"],
                    }
                    for page_id in detail_ids
                ],
                "reused_over_resolution_audit": audits[query_id],
            }
        )
    return cases


def fenced(text: Any) -> list[str]:
    return ["```text", str(text), "```", ""]


def ranking_lines(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [f"### {title}", "", "```text"]
    for row in rows:
        marker = "✅ GOLD" if row["is_gold"] else "❌ NON-GOLD"
        lines.append(
            f"#{row['rank']} {row['page_id']} score={float(row['score']):.9f} "
            f"source_turn={row['source_turn_id']} {marker}"
        )
    lines.extend(("```", ""))
    return lines


def write_report(
    path: Path,
    session_summary: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
    changed_summary: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> None:
    lines = [
        "# Clean Query Replacement Ablation",
        "",
        "## Embedding contract",
        "",
        "- Baseline: `original_query only`.",
        "- Old Rewrite Only: `old_standalone_rewrite only`.",
        "- Reference Resolution Only: `resolved_query only`.",
        "- Page: stored production P0; retrieval: per-Session dense cosine; TopK=5.",
        "",
        "## UNCHANGED validation",
        "",
        "```json",
        json.dumps(validation, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Session R@5",
        "",
        "| Session | Baseline | Old Rewrite Only | Ref Resolution Only | Ref vs Base | Old vs Base | Ref vs Old |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in session_summary:
        lines.append(
            f"| {row['session_id']} | {float(row['baseline_r5']):.9f} | "
            f"{float(row['old_rewrite_only_r5']):.9f} | "
            f"{float(row['reference_resolution_only_r5']):.9f} | "
            f"{float(row['ref_vs_baseline_r5']):+.9f} | {float(row['old_vs_baseline_r5']):+.9f} | "
            f"{float(row['ref_vs_old_r5']):+.9f} |"
        )
    lines.extend(
        (
            "",
            "## CHANGED vs UNCHANGED",
            "",
            "```json",
            json.dumps(changed_summary["groups"], ensure_ascii=False, indent=2),
            "```",
            "",
        )
    )
    for case in cases:
        lines.extend((f"# {case['query_id']}", "", f"Group: `{case['reference_group']}`", ""))
        lines.extend(("### Original Query", "", *fenced(case["original_query"])))
        lines.extend(("### Old Standalone Rewrite", "", *fenced(case["old_standalone_rewrite"])))
        lines.extend(
            (
                "### Conservative Reference Resolution",
                "",
                *fenced(case["conservative_reference_resolution"]),
            )
        )
        lines.extend(
            (
                "### Baseline actual embedding text",
                "",
                *fenced(case["baseline_actual_embedding_text"]),
            )
        )
        lines.extend(
            (
                "### Old Rewrite Only actual embedding text",
                "",
                *fenced(case["old_rewrite_only_actual_embedding_text"]),
            )
        )
        lines.extend(
            (
                "### Ref Resolution Only actual embedding text",
                "",
                *fenced(case["reference_resolution_only_actual_embedding_text"]),
            )
        )
        lines.extend(ranking_lines("Baseline Top10", case["baseline_top10"]))
        lines.extend(ranking_lines("Old Rewrite Only Top10", case["old_rewrite_only_top10"]))
        lines.extend(
            ranking_lines("Ref Resolution Only Top10", case["reference_resolution_only_top10"])
        )
        lines.extend(("### All Gold ranks", "", "```text"))
        for gold in case["gold_comparison"]:
            lines.extend(
                (
                    f"Gold Page: {gold['gold_page_id']}",
                    f"Baseline #{gold['baseline_rank']} score={float(gold['baseline_score']):.9f}",
                    f"Old Rewrite Only #{gold['old_rewrite_only_rank']} "
                    f"score={float(gold['old_rewrite_only_score']):.9f}",
                    f"Ref Resolution Only #{gold['reference_resolution_only_rank']} "
                    f"score={float(gold['reference_resolution_only_score']):.9f}",
                    "",
                )
            )
        lines.extend(("```", "", "### Production P0 Page texts (three Top5 union + all Gold)", ""))
        for page in case["page_details"]:
            lines.extend(
                (
                    f"#### Page {page['page_id']}",
                    "",
                    f"- Source Turn ID: `{page['source_turn_id']}`",
                    f"- Gold: `{'YES' if page['is_gold'] else 'NO'}`",
                    f"- Baseline: `#{page['baseline_rank']} / {float(page['baseline_score']):.9f}`",
                    f"- Old Rewrite Only: `#{page['old_rewrite_only_rank']} / "
                    f"{float(page['old_rewrite_only_score']):.9f}`",
                    f"- Ref Resolution Only: `#{page['reference_resolution_only_rank']} / "
                    f"{float(page['reference_resolution_only_score']):.9f}`",
                    "",
                    "完整 production P0 embedding text：",
                    "",
                    *fenced(page["production_p0_embedding_text"]),
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.output_dir = args.output_dir.resolve()
    snapshots = load_snapshots()
    query_records = load_query_records(snapshots)
    audits = {str(row["query_id"]): row for row in load_jsonl(SOURCE_RESULT_DIR / "reference_resolution_audit.jsonl")}
    if len(audits) != 99:
        raise ValueError(f"Reused audit coverage is not 99: {len(audits)}")
    vectors, embedding_cache, embedding_metadata = load_query_vectors(
        args.output_dir, snapshots, query_records
    )
    rankings = {code: rank_snapshot(snapshots[code], vectors) for code in SESSION_CODES}

    # Required ordering: do not compute or write formal metrics until A/B/C/D pass.
    validation = validate_unchanged_queries(snapshots, query_records, vectors, rankings)

    metrics = build_metrics(snapshots, rankings)
    gold_rows, query_rows = build_transitions(snapshots, query_records, audits, rankings)
    session_summary = build_session_summary(metrics, gold_rows, query_rows)
    changed_summary = build_changed_summary(
        snapshots, query_records, rankings, gold_rows, query_rows, validation
    )
    cases = build_cases(snapshots, query_records, audits, rankings)

    # Recompute sanity: S001 standalone-only must match the immutable old Q4 result.
    old_q4 = next(
        row
        for row in read_csv(S001_RESULT_DIR / "query_ablation.csv")
        if row["variant"] == "Q4"
    )
    s001_old_validation = {
        key: math.isclose(
            float(metrics["S001"]["old_rewrite_only"][metric_key]), float(old_q4[key]), abs_tol=1e-12
        )
        for key, metric_key in (
            ("recall_at_5", "recall_at_5"),
            ("recall_at_10", "recall_at_10"),
            ("recall_at_20", "recall_at_20"),
            ("mrr", "mrr"),
            ("mean_gold_rank", "mean_gold_rank"),
        )
    }
    if not all(s001_old_validation.values()):
        raise AssertionError(f"S001 old standalone-only does not reproduce old Q4: {s001_old_validation}")

    write_csv(args.output_dir / "session_summary.csv", session_summary)
    write_csv(args.output_dir / "query_transition.csv", gold_rows)
    dump_json(args.output_dir / "changed_vs_unchanged_summary.json", changed_summary)
    dump_json(args.output_dir / "representative_cases.json", {"case_ids": list(CASE_IDS), "cases": cases})
    write_report(
        args.output_dir / "representative_cases.md",
        session_summary,
        cases,
        changed_summary,
        validation,
    )
    run_metadata = {
        "baseline_embedding_contract": "original_query only",
        "old_rewrite_embedding_contract": "old_standalone_rewrite only",
        "reference_resolution_embedding_contract": "resolved_query only",
        "new_llm_calls": 0,
        "full_session_rerun": False,
        "page_regeneration": False,
        "page_representation": "production P0: summary + Keywords + User",
        "page_embedding_source": "reused immutable snapshot stored_embedding",
        "page_embedding_reused_count": sum(len(snapshots[code]["pages"]) for code in SESSION_CODES),
        "embedding_model": PRODUCTION_EMBEDDING,
        "retrieval_method": "per-Session dense cosine",
        "top_k": OUTPUT_K,
        "query_source": str(SOURCE_RESULT_DIR / "reference_resolution_queries.jsonl"),
        "query_source_sha256": stable_hash(load_jsonl(SOURCE_RESULT_DIR / "reference_resolution_queries.jsonl")),
        "audit_reused": str(SOURCE_RESULT_DIR / "reference_resolution_audit.jsonl"),
        "over_resolution_summary_reused": str(SOURCE_RESULT_DIR / "over_resolution_summary.json"),
        "embedding": embedding_metadata,
        "validation": validation,
        "s001_old_q4_reproduction": s001_old_validation,
        "snapshot_paths": {code: snapshots[code]["path"] for code in SESSION_CODES},
    }
    dump_json(args.output_dir / "run_metadata.json", run_metadata)
    embedding_cache.release(PRODUCTION_EMBEDDING)


if __name__ == "__main__":
    main()
