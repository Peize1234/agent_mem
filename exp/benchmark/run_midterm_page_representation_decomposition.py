"""Decompose the frozen production MidTerm Page representation without LLM calls.

The experiment keeps the benchmark, Page identity, query-time visibility, Gold,
query texts/vectors, embedding model, and dense cosine retrieval fixed. Only the
Page embedding text is changed deterministically.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import percentile, stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    rank_configuration,
    validate_old_reproduction,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_page_representation_decomposition"
VARIANTS = (
    "E0",
    "E1",
    "E2",
    "E3",
    "E4",
    "E5",
    "E6-100",
    "E6-200",
    "E6-300",
    "E7-100",
    "E7-200",
    "E7-300",
)
NON_E0_VARIANTS = VARIANTS[1:]
SEARCHES = ("Baseline", "P2")
DISPLAY_VARIANT_ORDER = VARIANTS
PREFIX_LENGTHS = (100, 200, 300)
FULL_CLOSE_GOLD_TOLERANCE = 1
MANDATORY_CASES = (
    "S001-Q013",
    "S001-Q026",
    "S001-Q033",
    "S002-Q049",
    "S002-Q055",
    "S003-Q039",
    "S003-Q066",
    "S004-Q029",
    "S004-Q035",
    "S005-Q040",
    "S005-Q051",
    "S005-Q052",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen Old Page representation decomposition")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    return parser.parse_args()


def join_parts(parts: Sequence[str]) -> str:
    return "\n".join(part for part in parts if part)


def keyword_line(page: Mapping[str, Any]) -> str:
    keywords = page.get("keywords") or []
    joined = ", ".join(str(item) for item in keywords)
    return f"Keywords: {joined}" if joined else ""


def user_line(page: Mapping[str, Any]) -> str:
    user = str(page.get("user_input") or "").strip()
    return f"User: {user}" if user else ""


def representation_text(page: Mapping[str, Any], variant: str) -> str:
    summary = str(page.get("summary") or "").strip()
    keywords = keyword_line(page)
    user = user_line(page)
    if variant == "E0":
        return str(page["current_embedding_text"])
    if variant == "E1":
        return summary
    if variant == "E2":
        return join_parts((summary, keywords))
    if variant == "E3":
        return join_parts((summary, user))
    if variant == "E4":
        return user
    if variant == "E5":
        return join_parts((keywords, user))
    if variant.startswith("E6-"):
        length = int(variant.split("-", 1)[1])
        return join_parts((summary[:length], keywords, user))
    if variant.startswith("E7-"):
        length = int(variant.split("-", 1)[1])
        return join_parts((summary[-length:], keywords, user))
    raise ValueError(f"Unknown Page representation: {variant}")


def build_texts(pages: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    result = {
        variant: {str(page["page_id"]): representation_text(page, variant) for page in pages} for variant in VARIANTS
    }
    if any(len(values) != 333 for values in result.values()):
        raise AssertionError("Page representation coverage mismatch")
    return result


def validate_frozen_inputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    texts: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in pages}
    reproduction, old_rankings = validate_old_reproduction(snapshots, old_vectors, query_vectors)
    page_ids = {str(page["page_id"]) for page in pages}
    source_turn_ids = {str(page["source_turn_id"]) for page in pages}
    query_ids = {str(query["query_id"]) for query in queries}
    eligible_gold_count = sum(len(query["eligible_gold_page_ids"]) for query in queries)
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    visibility_hash = stable_hash(visibility)
    representation_page_sets = {variant: set(values) for variant, values in texts.items()}
    baseline_texts = {str(query["query_id"]): str(query["original_query"]) for query in queries}
    checks = {
        "1_E0_Baseline_reproduction": reproduction["A"],
        "2_E0_P2_reproduction": reproduction["B"],
        "3_source_turn_set": {
            "status": "PASS"
            if len(source_turn_ids) == 333
            and page_ids == representation_page_sets["E0"]
            and all(value == page_ids for value in representation_page_sets.values())
            else "FAIL",
            "page_count": len(page_ids),
            "source_turn_count": len(source_turn_ids),
        },
        "4_evaluation_queries": {
            "status": "PASS" if len(queries) == len(query_ids) == 99 else "FAIL",
            "query_count": len(queries),
        },
        "5_eligible_gold": {
            "status": "PASS" if eligible_gold_count == 154 else "FAIL",
            "eligible_gold_count": eligible_gold_count,
        },
        "6_visible_page_ids": {
            "status": "PASS"
            if len(visibility) == 99 and all(set(ids) <= page_ids for ids in visibility.values())
            else "FAIL",
            "query_count": len(visibility),
            "mapping_sha256": visibility_hash,
            "changed_across_variants": False,
        },
        "7_baseline_query_text": {
            "status": "PASS"
            if set(baseline_texts) == query_ids
            and all(baseline_texts[str(query["query_id"])] == str(query["original_query"]) for query in queries)
            else "FAIL",
            "contract": "original_query exactly",
            "query_count": len(baseline_texts),
        },
        "8_p2_query_text": {
            "status": "PASS" if set(p2_texts) == query_ids and len(query_vectors["Search-P2"]) == 99 else "FAIL",
            "contract": "frozen P2 resolved_query only; content hash checked by loader",
            "query_count": len(p2_texts),
        },
        "9_new_llm_calls": {"status": "PASS", "new_llm_calls": 0},
    }
    if any(row["status"] != "PASS" for row in checks.values()):
        raise AssertionError(checks)
    return checks, old_rankings


def token_ids(tokenizer: Any, text: str) -> list[int]:
    return list(tokenizer.encode(str(text), add_special_tokens=False))


def tokenizer_audit(
    pages: Sequence[Mapping[str, Any]],
    tokenizer: Any,
    max_length: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    special_tokens = int(tokenizer.num_special_tokens_to_add(pair=False))
    content_budget = max_length - special_tokens
    for page in pages:
        summary = str(page.get("summary") or "").strip()
        keywords = keyword_line(page)
        user = user_line(page)
        full_text = str(page["current_embedding_text"])
        summary_ids = token_ids(tokenizer, summary)
        keyword_ids = token_ids(tokenizer, keywords)
        user_ids = token_ids(tokenizer, user)
        full_content_ids = token_ids(tokenizer, full_text)
        concatenated = summary_ids + keyword_ids + user_ids
        if concatenated != full_content_ids:
            raise AssertionError(f"Tokenizer component boundary mismatch: {page['source_turn_id']}")
        remaining = content_budget
        summary_kept = min(len(summary_ids), max(0, remaining))
        remaining -= summary_kept
        keywords_kept = min(len(keyword_ids), max(0, remaining))
        remaining -= keywords_kept
        user_kept = min(len(user_ids), max(0, remaining))
        raw_count = len(full_content_ids) + special_tokens
        encoded = tokenizer(full_text, truncation=True, max_length=max_length, add_special_tokens=True)
        effective_count = len(encoded["input_ids"])
        if effective_count != min(raw_count, max_length):
            raise AssertionError(f"Tokenizer effective length mismatch: {page['source_turn_id']}")
        if len(user_ids) == user_kept:
            user_status = "FULLY_KEPT"
        elif user_kept == 0:
            user_status = "FULLY_TRUNCATED"
        else:
            user_status = "PARTIALLY_TRUNCATED"
        rows.append(
            {
                "session_id": page["session_code"],
                "source_turn_id": page["source_turn_id"],
                "page_id": page["page_id"],
                "raw_token_count_including_special": raw_count,
                "effective_token_count": effective_count,
                "truncated": raw_count > max_length,
                "summary_tokens_total": len(summary_ids),
                "summary_tokens_kept": summary_kept,
                "keywords_tokens_total": len(keyword_ids),
                "keywords_tokens_kept": keywords_kept,
                "user_tokens_total": len(user_ids),
                "user_tokens_kept": user_kept,
                "user_truncation_status": user_status,
            }
        )
    counts = [int(row["raw_token_count_including_special"]) for row in rows]
    truncated_count = sum(bool(row["truncated"]) for row in rows)
    partial_user = sum(row["user_truncation_status"] == "PARTIALLY_TRUNCATED" for row in rows)
    full_user = sum(row["user_truncation_status"] == "FULLY_TRUNCATED" for row in rows)
    summary = {
        "tokenizer_class": type(tokenizer).__name__,
        "tokenizer_model_max_length": int(tokenizer.model_max_length),
        "sentence_transformer_max_seq_length": max_length,
        "effective_max_length": max_length,
        "special_tokens_single": special_tokens,
        "content_token_budget": content_budget,
        "truncation_enabled_by_sentence_transformer": True,
        "truncation_strategy": "longest_first (single sequence)",
        "truncation_side": str(tokenizer.truncation_side),
        "padding_side": str(tokenizer.padding_side),
        "token_count_mean": statistics.fmean(counts),
        "token_count_median": statistics.median(counts),
        "token_count_p90": percentile(counts, 0.90),
        "token_count_p95": percentile(counts, 0.95),
        "token_count_max": max(counts),
        "truncated_page_count": truncated_count,
        "truncated_page_rate": truncated_count / len(rows),
        "user_partially_truncated_page_count": partial_user,
        "user_fully_truncated_page_count": full_user,
        "user_partially_or_fully_truncated_page_count": partial_user + full_user,
        "component_token_definition": "token counts include each field label (Keywords:/User:) but exclude special tokens",
    }
    return rows, summary


def representation_length_stats(
    pages: Sequence[Mapping[str, Any]],
    texts: Mapping[str, Mapping[str, str]],
    tokenizer: Any,
    max_length: int,
) -> list[dict[str, Any]]:
    page_ids = [str(page["page_id"]) for page in pages]
    rows = []
    for variant in VARIANTS:
        values = [texts[variant][page_id] for page_id in page_ids]
        chars = [len(value) for value in values]
        tokens = [len(tokenizer.encode(value, add_special_tokens=True)) for value in values]
        rows.append(
            {
                "page_variant": variant,
                "page_count": len(values),
                "char_count_mean": statistics.fmean(chars),
                "char_count_median": statistics.median(chars),
                "char_count_p90": percentile(chars, 0.90),
                "char_count_max": max(chars),
                "raw_token_count_mean": statistics.fmean(tokens),
                "raw_token_count_median": statistics.median(tokens),
                "raw_token_count_p90": percentile(tokens, 0.90),
                "raw_token_count_p95": percentile(tokens, 0.95),
                "raw_token_count_max": max(tokens),
                "truncated_page_count": sum(value > max_length for value in tokens),
                "truncated_page_rate": sum(value > max_length for value in tokens) / len(tokens),
            }
        )
    return rows


def embed_variants(
    output_dir: Path,
    pages: Sequence[Mapping[str, Any]],
    texts: Mapping[str, Mapping[str, str]],
    old_vectors: Mapping[str, Sequence[float]],
    cache: EmbeddingCache,
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any]]:
    vectors: dict[str, dict[str, list[float]]] = {
        "E0": {page_id: list(value) for page_id, value in old_vectors.items()}
    }
    metadata: dict[str, Any] = {
        "E0": {
            "item_count": 333,
            "cache_hit": True,
            "source": "frozen production stored_embedding",
            "dimension": 512,
        }
    }
    page_ids = [str(page["page_id"]) for page in pages]
    for variant in NON_E0_VARIANTS:
        item_ids = [f"{variant}:{page_id}" for page_id in page_ids]
        values = [texts[variant][page_id] for page_id in page_ids]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"page-representation-{variant}-S001-S005",
            item_ids,
            values,
            measure_individual=False,
        )
        vectors[variant] = {page_id: encoded[f"{variant}:{page_id}"] for page_id in page_ids}
        metadata[variant] = item_meta
        if len(vectors[variant]) != 333 or int(item_meta["dimension"]) != 512:
            raise AssertionError(f"Embedding coverage/dimension mismatch: {variant}")
    return vectors, metadata


def build_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    vectors: Mapping[str, Mapping[str, Sequence[float]]],
    old_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {
        "E0_Baseline": dict(old_rankings["OLD_BASE"]),
        "E0_P2": dict(old_rankings["OLD_P2"]),
    }
    for variant in NON_E0_VARIANTS:
        for search in SEARCHES:
            rankings[f"{variant}_{search}"] = rank_configuration(
                snapshots,
                query_vectors[f"Search-{search}"],
                vectors[variant],
            )
    return rankings


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(query, session_code=code) for code in SESSION_CODES for query in snapshots[code]["queries"]]


def metric_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    queries = all_queries(snapshots)
    rank_maps = {
        config: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for config, by_query in rankings.items()
    }
    retrieval_rows: list[dict[str, Any]] = []
    gold_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    distribution_rows: list[dict[str, Any]] = []
    aggregate: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        for search in SEARCHES:
            config = f"{variant}_{search}"
            metrics, _ = evaluate_all(snapshots, rankings[config])
            aggregate[config] = metrics
            base_config = f"E0_{search}"
            promoted = demoted = 0
            rescued = hurt = 0
            gold_ranks: list[int] = []
            for query in queries:
                query_id = str(query["query_id"])
                gold_ids = [str(value) for value in query["eligible_gold_page_ids"]]
                base_ranks = {gold_id: int(rank_maps[base_config][query_id][gold_id]["rank"]) for gold_id in gold_ids}
                variant_ranks = {gold_id: int(rank_maps[config][query_id][gold_id]["rank"]) for gold_id in gold_ids}
                base_hit = any(rank <= 5 for rank in base_ranks.values())
                variant_hit = any(rank <= 5 for rank in variant_ranks.values())
                promoted_query_gold = sum(
                    base_ranks[gold_id] > 5 and variant_ranks[gold_id] <= 5 for gold_id in gold_ids
                )
                demoted_query_gold = sum(
                    base_ranks[gold_id] <= 5 and variant_ranks[gold_id] > 5 for gold_id in gold_ids
                )
                promoted += promoted_query_gold
                demoted += demoted_query_gold
                transition = (
                    "RESCUED"
                    if not base_hit and variant_hit
                    else "HURT"
                    if base_hit and not variant_hit
                    else "UNCHANGED_HIT"
                    if base_hit
                    else "UNCHANGED_MISS"
                )
                rescued += int(transition == "RESCUED")
                hurt += int(transition == "HURT")
                transition_rows.append(
                    {
                        "session_id": query["session_code"],
                        "query_id": query_id,
                        "page_variant": variant,
                        "search_variant": search,
                        "comparison": f"{variant} vs E0 under {search}",
                        "transition": transition,
                        "e0_gold_ranks": base_ranks,
                        "variant_gold_ranks": variant_ranks,
                        "promoted_gold": promoted_query_gold,
                        "demoted_gold": demoted_query_gold,
                    }
                )
                for gold_id in gold_ids:
                    item = rank_maps[config][query_id][gold_id]
                    gold_ranks.append(int(item["rank"]))
                    gold_rows.append(
                        {
                            "session_id": query["session_code"],
                            "query_id": query_id,
                            "gold_page_id": gold_id,
                            "gold_source_turn_id": item["source_turn_id"],
                            "page_variant": variant,
                            "search_variant": search,
                            "rank": int(item["rank"]),
                            "score": float(item["score"]),
                            "hit_at_5": int(item["rank"]) <= 5,
                        }
                    )
            retrieval_rows.append(
                {
                    "page_variant": variant,
                    "search_variant": search,
                    "eligible_gold_count": metrics["eligible_gold_count"],
                    "top5_recalled_gold": round(float(metrics["recall_at_5"]) * int(metrics["eligible_gold_count"])),
                    "micro_r5": metrics["recall_at_5"],
                    "macro_r5": metrics["macro_session_r5"],
                    "r10": metrics["recall_at_10"],
                    "r20": metrics["recall_at_20"],
                    "mrr": metrics["mrr"],
                    "mean_gold_rank": metrics["mean_gold_rank"],
                    "promoted_gold_vs_e0": promoted,
                    "demoted_gold_vs_e0": demoted,
                    "net_gold_gain_vs_e0": promoted - demoted,
                    "rescued_queries_vs_e0": rescued,
                    "hurt_queries_vs_e0": hurt,
                }
            )
            distribution_rows.append(
                {
                    "page_variant": variant,
                    "search_variant": search,
                    "eligible_gold_count": len(gold_ranks),
                    "gold_at_1": sum(rank <= 1 for rank in gold_ranks),
                    "gold_at_3": sum(rank <= 3 for rank in gold_ranks),
                    "gold_at_5": sum(rank <= 5 for rank in gold_ranks),
                    "gold_at_10": sum(rank <= 10 for rank in gold_ranks),
                    "gold_at_20": sum(rank <= 20 for rank in gold_ranks),
                    "gold_gt_20": sum(rank > 20 for rank in gold_ranks),
                }
            )
    return retrieval_rows, gold_rows, transition_rows, distribution_rows, aggregate


def separation_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for variant in VARIANTS:
        for search in SEARCHES:
            config = f"{variant}_{search}"
            margins: list[float] = []
            best_gold_scores: list[float] = []
            best_nongold_scores: list[float] = []
            for query in all_queries(snapshots):
                query_id = str(query["query_id"])
                gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
                ranking = rankings[config][query_id]
                gold_scores = [float(item["score"]) for item in ranking if str(item["page_id"]) in gold_ids]
                nongold_scores = [float(item["score"]) for item in ranking if str(item["page_id"]) not in gold_ids]
                best_gold = max(gold_scores)
                best_nongold = max(nongold_scores)
                best_gold_scores.append(best_gold)
                best_nongold_scores.append(best_nongold)
                margins.append(best_gold - best_nongold)
            rows.append(
                {
                    "page_variant": variant,
                    "search_variant": search,
                    "query_count": len(margins),
                    "best_gold_score_mean": statistics.fmean(best_gold_scores),
                    "best_nongold_score_mean": statistics.fmean(best_nongold_scores),
                    "margin_mean": statistics.fmean(margins),
                    "margin_median": statistics.median(margins),
                    "positive_margin_rate": sum(value > 0 for value in margins) / len(margins),
                    "margin_definition": "best visible eligible Gold score - best visible Non-Gold score",
                }
            )
    return rows


def component_summary(
    retrieval_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    by_config = {(str(row["page_variant"]), str(row["search_variant"])): row for row in retrieval_rows}
    comparisons = (
        ("Summary Only vs Full", "E0", "E1"),
        ("User Only vs Full", "E0", "E4"),
        ("Remove Keywords from Full", "E0", "E3"),
        ("Remove User from Full", "E0", "E2"),
        ("Add Keywords to Summary", "E1", "E2"),
        ("Add Keywords to Summary+User", "E3", "E0"),
        ("Add Keywords to User", "E4", "E5"),
        ("Add User to Summary", "E1", "E3"),
        ("Add User to Summary+Keywords", "E2", "E0"),
        ("Add Summary to User", "E4", "E3"),
        ("Add Summary to Keywords+User", "E5", "E0"),
    )
    rows: list[dict[str, Any]] = []
    for search in SEARCHES:
        for label, left, right in comparisons:
            left_row = by_config[(left, search)]
            right_row = by_config[(right, search)]
            rows.append(
                {
                    "comparison": label,
                    "search_variant": search,
                    "left_variant": left,
                    "right_variant": right,
                    "left_micro_r5": left_row["micro_r5"],
                    "right_micro_r5": right_row["micro_r5"],
                    "delta_right_minus_left": float(right_row["micro_r5"]) - float(left_row["micro_r5"]),
                    "left_top5_gold": left_row["top5_recalled_gold"],
                    "right_top5_gold": right_row["top5_recalled_gold"],
                }
            )
        for length in PREFIX_LENGTHS:
            for side in ("E6", "E7"):
                variant = f"{side}-{length}"
                full = by_config[("E0", search)]
                candidate = by_config[(variant, search)]
                rows.append(
                    {
                        "comparison": f"{variant} vs Full",
                        "search_variant": search,
                        "left_variant": "E0",
                        "right_variant": variant,
                        "left_micro_r5": full["micro_r5"],
                        "right_micro_r5": candidate["micro_r5"],
                        "delta_right_minus_left": float(candidate["micro_r5"]) - float(full["micro_r5"]),
                        "left_top5_gold": full["top5_recalled_gold"],
                        "right_top5_gold": candidate["top5_recalled_gold"],
                    }
                )
            prefix = by_config[(f"E6-{length}", search)]
            tail = by_config[(f"E7-{length}", search)]
            rows.append(
                {
                    "comparison": f"Prefix vs Tail {length}",
                    "search_variant": search,
                    "left_variant": f"E7-{length}",
                    "right_variant": f"E6-{length}",
                    "left_micro_r5": tail["micro_r5"],
                    "right_micro_r5": prefix["micro_r5"],
                    "delta_right_minus_left": float(prefix["micro_r5"]) - float(tail["micro_r5"]),
                    "left_top5_gold": tail["top5_recalled_gold"],
                    "right_top5_gold": prefix["top5_recalled_gold"],
                }
            )
    shortest_close: int | None = None
    close_details: dict[str, Any] = {}
    for length in PREFIX_LENGTHS:
        gaps = {
            search: abs(
                int(by_config[(f"E6-{length}", search)]["top5_recalled_gold"])
                - int(by_config[("E0", search)]["top5_recalled_gold"])
            )
            for search in SEARCHES
        }
        close_details[str(length)] = gaps
        if shortest_close is None and all(value <= FULL_CLOSE_GOLD_TOLERANCE for value in gaps.values()):
            shortest_close = length
    prefix_wins = tail_wins = ties = 0
    deltas = []
    for length in PREFIX_LENGTHS:
        for search in SEARCHES:
            prefix = float(by_config[(f"E6-{length}", search)]["micro_r5"])
            tail = float(by_config[(f"E7-{length}", search)]["micro_r5"])
            deltas.append(prefix - tail)
            prefix_wins += int(prefix > tail)
            tail_wins += int(tail > prefix)
            ties += int(math.isclose(prefix, tail, abs_tol=1e-12))
    summary = {
        "shortest_prefix_close_to_full": shortest_close,
        "close_definition": ("absolute Top5 recalled-Gold gap <= 1 versus E0 under both Baseline and P2"),
        "prefix_top5_gold_gaps": close_details,
        "prefix_vs_tail_pair_count": len(deltas),
        "prefix_wins": prefix_wins,
        "tail_wins": tail_wins,
        "ties": ties,
        "prefix_minus_tail_micro_r5_mean": statistics.fmean(deltas),
    }
    return rows, summary


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, Any], str]:
    queries = {str(query["query_id"]): query for query in all_queries(snapshots)}
    pages_by_id = {str(page["page_id"]): page for page in pages}
    pages_by_turn = {str(page["source_turn_id"]): page for page in pages}
    rank_maps = {
        config: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for config, by_query in rankings.items()
    }
    evaluated_cases: list[dict[str, Any]] = []
    page_only_cases: list[dict[str, Any]] = []
    for case_id in MANDATORY_CASES:
        if case_id not in queries:
            page = pages_by_turn[case_id]
            page_only_cases.append(
                {
                    "source_turn_id": case_id,
                    "session_id": page["session_code"],
                    "note": "Frozen source Page is not one of the 99 evaluation Queries; no ranking or Gold was invented.",
                    "old_summary": page["summary"],
                    "old_keywords": page["keywords"],
                    "old_user": page["user_input"],
                }
            )
            continue
        query = queries[case_id]
        gold_pages = []
        for gold_id_value in query["eligible_gold_page_ids"]:
            gold_id = str(gold_id_value)
            page = pages_by_id[gold_id]
            results = []
            for variant in DISPLAY_VARIANT_ORDER:
                results.append(
                    {
                        "page_variant": variant,
                        "baseline_rank": int(rank_maps[f"{variant}_Baseline"][case_id][gold_id]["rank"]),
                        "baseline_score": float(rank_maps[f"{variant}_Baseline"][case_id][gold_id]["score"]),
                        "p2_rank": int(rank_maps[f"{variant}_P2"][case_id][gold_id]["rank"]),
                        "p2_score": float(rank_maps[f"{variant}_P2"][case_id][gold_id]["score"]),
                    }
                )
            gold_pages.append(
                {
                    "gold_page_id": gold_id,
                    "gold_source_turn_id": page["source_turn_id"],
                    "old_summary": page["summary"],
                    "old_keywords": page["keywords"],
                    "old_user": page["user_input"],
                    "rank_score_by_variant": results,
                }
            )
        evaluated_cases.append(
            {
                "query_id": case_id,
                "session_id": query["session_code"],
                "original_query": query["original_query"],
                "p2_query": p2_texts[case_id],
                "gold_pages": gold_pages,
            }
        )
    payload = {"evaluated_cases": evaluated_cases, "page_only_cases": page_only_cases}
    lines = [
        "# Old Page Representation Decomposition — Representative Cases",
        "",
        "All scores use frozen per-Query visibility and eligible Gold. The three mandatory source turns that are not evaluation Queries are shown without synthetic ranks.",
    ]
    for case in evaluated_cases:
        lines.extend(
            (
                "",
                f"## {case['query_id']}",
                "",
                "### Original Query",
                "",
                "```text",
                str(case["original_query"]),
                "```",
                "",
                "### P2 Query",
                "",
                "```text",
                str(case["p2_query"]),
                "```",
            )
        )
        for gold in case["gold_pages"]:
            lines.extend(
                (
                    "",
                    f"### Gold {gold['gold_source_turn_id']} ({gold['gold_page_id']})",
                    "",
                    "Old Summary:",
                    "",
                    "```text",
                    str(gold["old_summary"]),
                    "```",
                    "",
                    "Old Keywords:",
                    "",
                    "```text",
                    json.dumps(gold["old_keywords"], ensure_ascii=False),
                    "```",
                    "",
                    "Old User:",
                    "",
                    "```text",
                    str(gold["old_user"]),
                    "```",
                    "",
                    "| Page Variant | Baseline rank | Baseline score | P2 rank | P2 score |",
                    "|---|---:|---:|---:|---:|",
                )
            )
            for result in gold["rank_score_by_variant"]:
                lines.append(
                    f"| {result['page_variant']} | #{result['baseline_rank']} | "
                    f"{result['baseline_score']:.9f} | #{result['p2_rank']} | {result['p2_score']:.9f} |"
                )
    if page_only_cases:
        lines.extend(("", "## Mandatory Page-only source turns"))
    for case in page_only_cases:
        lines.extend(
            (
                "",
                f"### {case['source_turn_id']}",
                "",
                str(case["note"]),
                "",
                "Old Summary:",
                "",
                "```text",
                str(case["old_summary"]),
                "```",
                "",
                "Old Keywords:",
                "",
                "```text",
                json.dumps(case["old_keywords"], ensure_ascii=False),
                "```",
                "",
                "Old User:",
                "",
                "```text",
                str(case["old_user"]),
                "```",
            )
        )
    return payload, "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    texts = build_texts(pages)
    validations, old_rankings = validate_frozen_inputs(
        snapshots,
        pages,
        queries,
        p2_texts,
        query_vectors,
        texts,
    )
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in pages}
    embedding_cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    sentence_transformer = embedder.model
    tokenizer = sentence_transformer.tokenizer
    max_length = int(sentence_transformer.max_seq_length)
    tokenizer_rows, tokenizer_summary = tokenizer_audit(pages, tokenizer, max_length)
    length_rows = representation_length_stats(pages, texts, tokenizer, max_length)
    vectors, embedding_metadata = embed_variants(
        args.output_dir,
        pages,
        texts,
        old_vectors,
        embedding_cache,
    )
    rankings = build_rankings(snapshots, query_vectors, vectors, old_rankings)
    retrieval_rows, gold_rows, transitions, distributions, aggregate = metric_outputs(
        snapshots,
        rankings,
    )
    separation = separation_outputs(snapshots, rankings)
    component_rows, component_interpretation = component_summary(retrieval_rows)
    representative_json, representative_markdown = representative_outputs(
        snapshots,
        pages,
        p2_texts,
        rankings,
    )
    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", retrieval_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", transitions)
    write_csv(args.output_dir / "metrics/gold_rank_distribution.csv", distributions)
    write_csv(args.output_dir / "metrics/gold_nongold_separation.csv", separation)
    write_csv(args.output_dir / "analysis/tokenizer_truncation_audit.csv", tokenizer_rows)
    write_csv(args.output_dir / "analysis/representation_length_stats.csv", length_rows)
    write_csv(args.output_dir / "analysis/component_ablation_summary.csv", component_rows)
    dump_json(args.output_dir / "representative_cases.json", representative_json)
    (args.output_dir / "representative_cases.md").write_text(
        representative_markdown,
        encoding="utf-8",
    )
    embedding_items = sum(int(embedding_metadata[variant]["item_count"]) for variant in NON_E0_VARIANTS)
    computed_this_run = sum(
        int(embedding_metadata[variant]["item_count"])
        for variant in NON_E0_VARIANTS
        if not bool(embedding_metadata[variant].get("cache_hit"))
    )
    metadata = {
        "experiment_name": "old_page_representation_decomposition_ablation",
        "session_count": len(SESSION_CODES),
        "page_count": len(pages),
        "evaluation_query_count": len(queries),
        "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        "page_variants": list(VARIANTS),
        "search_variants": ["Search-Baseline", "Search-P2"],
        "page_text_contracts": {
            "E0": "<summary>\\nKeywords: <keywords>\\nUser: <original_user>",
            "E1": "<summary>",
            "E2": "<summary>\\nKeywords: <keywords>",
            "E3": "<summary>\\nUser: <original_user>",
            "E4": "User: <original_user>",
            "E5": "Keywords: <keywords>\\nUser: <original_user>",
            "E6": "Old Summary first N characters + Keywords + User",
            "E7": "Old Summary last N characters + Keywords + User",
        },
        "prefix_tail_truncation": "deterministic Python character slicing; no sentence-boundary rewrite",
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "embedding_dimension": 512,
        "normalization": "SentenceTransformer encode normalize_embeddings=False; dense retrieval applies cosine normalization",
        "tokenizer_contract": tokenizer_summary,
        "old_page_embedding_reused_count": 333,
        "non_e0_page_embedding_item_count": embedding_items,
        "new_page_embedding_count_this_run": computed_this_run,
        "embedding_metadata": embedding_metadata,
        "baseline_query_embedding_reused_count": 99,
        "p2_query_embedding_reused_count": 99,
        "new_query_embedding_count": 0,
        "new_query_llm_calls": 0,
        "new_page_summary_llm_calls": 0,
        "total_new_llm_calls": 0,
        "full_session_rerun": False,
        "summary_regeneration": False,
        "gold_changed": False,
        "visible_page_ids_changed": False,
        "query_prompt_changed": False,
        "validation": validations,
        "validation_all_pass": all(row["status"] == "PASS" for row in validations.values()),
        "component_interpretation_rule": component_interpretation,
        "aggregate_metrics": aggregate,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "retrieval_metrics": retrieval_rows,
                "tokenizer": tokenizer_summary,
                "component_interpretation": component_interpretation,
                "new_llm_calls": 0,
                "new_page_embedding_count": embedding_items,
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
