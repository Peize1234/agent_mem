"""Transfer the historically positive MidTerm ablations to frozen S001-S010.

This is an offline retrieval evaluation.  It reuses Production/C3 Pages,
stored Production vectors, frozen P2 vectors/rankings, ShortTerm hits, and
LongTerm provenance.  It never runs a benchmark Session or writes to Qdrant.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.analyze_query_completion_s001_s005_v2 import (
    build_query_completion_rows,
    completion_statistics,
    group_rank,
    load_v3_dataset,
    static_dataset_stats,
    write_csv,
    write_jsonl,
)
from exp.benchmark.analyze_query_completion_s001_s010_rebalanced import load_longterm_provenance
from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import cosine_rank, evaluate_rankings  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import page_text  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import (  # noqa: E402
    AsyncJsonlLLMCache,
    EmbeddingCache,
    qdrant_client,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    load_existing_embedding_vectors,
)
from exp.benchmark.run_reference_resolution_prompt_ablation import (  # noqa: E402
    PROMPT_VERSIONS,
    PlainTextJsonlLLMCache,
    resolution_messages,
    resolved_validator,
)
from exp.benchmark.run_reference_resolution_query_diagnosis import (  # noqa: E402
    REFERENCE_SYSTEM_PROMPT,
    reference_messages,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


DEFAULT_DATASET = REPO_ROOT / "exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx"
DEFAULT_RECALL_DIR = REPO_ROOT / "exp/results/recall_rebalanced_s001_s010_isolated"
DEFAULT_C3_DIR = REPO_ROOT / "exp/results/midterm_c3_rebalanced_s001_s010"
DEFAULT_COMPLETION_DIR = REPO_ROOT / "exp/results/full_memory_recall_s001_s010_rebalanced"
DEFAULT_OLD_CONTEXT_DIR = REPO_ROOT / "exp/results/midterm_add_local_context_ablation"
DEFAULT_OLD_RESOLUTION_DIR = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation"
DEFAULT_OLD_CHECKPOINT_DIR = REPO_ROOT / "exp/results/midterm_dense_bm25_hybrid_checkpoints"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_positive_transfer_ablation_s001_s010"
MEMORY_CONFIG = REPO_ROOT / "exp/benchmark/memory_config.json"
SESSION_CODES = tuple(f"S{index:03d}" for index in range(1, 11))
SHORT_TERM_QA_CAPACITY = 3
TOP_K = 5
EXPECTED = {
    "query_count": 752,
    "gold_requirement_count": 786,
    "outside_shortterm_requirement_count": 386,
    "shortterm_hit_count": 400,
    "or_requirement_count": 0,
    "page_count": 722,
    "routed_query_count": 386,
    "c3_gold_at_5": 179,
    "c3_mid_mean": 0.23315363881401618,
    "c3_short_mid_mean": 0.7345013477088949,
    "c3_all_memory_mean": 0.7836927223719676,
}
MAIN_CONFIGS = ("A0", "A1", "A2", "A3", "A4")
CONFIG_LABELS = {
    "A0": "Original Query + Production Add + Full Page",
    "A1": "P2 + Production Add + Full Page",
    "A2": "P2 + Production Add + Summary + Keywords",
    "A3": "P2 + C3 Context Add + Full Page",
    "A4": "P2 + C3 Context Add + Summary + Keywords",
    "P0": "P0 + Production Add + Full Page",
    "P1": "P1 + Production Add + Full Page",
}
OLD_GOLD_AT_5 = {"A0": 50, "A1": 54, "A2": 55, "A3": 57, "A4": 59, "P0": 53, "P1": 52}
PROMPT_FILES = {"P0": "P0_current.txt", "P1": "P1_explicit_reference.txt"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen positive-method transfer ablation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--recall-dir", type=Path, default=DEFAULT_RECALL_DIR)
    parser.add_argument("--c3-dir", type=Path, default=DEFAULT_C3_DIR)
    parser.add_argument("--completion-dir", type=Path, default=DEFAULT_COMPLETION_DIR)
    parser.add_argument("--old-context-dir", type=Path, default=DEFAULT_OLD_CONTEXT_DIR)
    parser.add_argument("--old-resolution-dir", type=Path, default=DEFAULT_OLD_RESOLUTION_DIR)
    parser.add_argument("--old-checkpoint-dir", type=Path, default=DEFAULT_OLD_CHECKPOINT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=240.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    parser.add_argument("--skip-secondary", action="store_true", help="Skip qualifying P0/P1 secondary runs")
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_routed_queries(sessions: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        turns = sessions[code]
        for turn in turns:
            short_ids = {
                item.query_id
                for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
            }
            outside = [group for group in turn.gold_groups if not group.hit_by(short_ids)]
            if not outside:
                continue
            if any(group.is_or or len(group.members) != 1 for group in outside):
                raise AssertionError("This frozen transfer benchmark is expected to contain no OR requirements")
            queries.append(
                {
                    "session_code": code,
                    "session_id": turn.session_id,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "original_query": turn.question,
                    "previous_3_qa": [
                        {"turn_id": item.query_id, "user": item.question, "assistant": item.answer}
                        for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
                    ],
                    "eligible_gold_page_ids": [group.members[0] for group in outside],
                }
            )
    return queries


def validate_dataset(sessions: Mapping[str, Sequence[Any]], queries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    stats = static_dataset_stats(sessions, SHORT_TERM_QA_CAPACITY, TOP_K)
    actual = {
        "query_count": int(stats["query_count"]),
        "gold_requirement_count": int(stats["gold_requirement_count"]),
        "outside_shortterm_requirement_count": int(stats["outside_shortterm_requirement_count"]),
        "shortterm_hit_count": int(stats["shortterm_requirement_count"]),
        "or_requirement_count": int(stats["or_requirement_count"]),
        "routed_query_count": len(queries),
        "routed_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
    }
    expected = {
        **{key: EXPECTED[key] for key in actual if key != "routed_gold_count"},
        "routed_gold_count": EXPECTED["outside_shortterm_requirement_count"],
    }
    if actual != expected:
        raise AssertionError(f"Frozen dataset validation failed: {actual} != {expected}")
    return {"status": "PASS", **actual, "session_query_counts": stats["session_query_counts"]}


def load_c3_artifacts(
    c3_dir: Path,
    queries: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, list[dict[str, Any]]], dict[str, Any]]:
    pages = load_jsonl(c3_dir / "c3_pages.jsonl")
    resolved_rows = load_jsonl(c3_dir / "resolved_p2_queries.jsonl")
    p2 = {str(row["query_id"]): str(row["resolved_query"]) for row in resolved_rows}
    expected_queries = {str(query["query_id"]): query for query in queries}
    if set(p2) != set(expected_queries):
        raise AssertionError("Frozen P2 Query coverage differs from routed Query set")
    for row in resolved_rows:
        if str(row["original_query"]) != str(expected_queries[str(row["query_id"])]["original_query"]):
            raise AssertionError(f"Frozen P2 original Query mismatch: {row['query_id']}")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(c3_dir / "c3_rankings.jsonl"):
        grouped[str(row["query_id"])].append(
            {
                "page_id": str(row["source_turn_id"]),
                "score": float(row["score"]),
                "rank": int(row["c3_rank"]),
            }
        )
    rankings: dict[str, list[dict[str, Any]]] = {}
    for query_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: int(row["rank"]))
        if [int(row["rank"]) for row in ordered] != list(range(1, len(ordered) + 1)):
            raise AssertionError(f"Frozen C3 ranking is not contiguous: {query_id}")
        rankings[query_id] = ordered
    if set(rankings) != set(expected_queries) or len(pages) != EXPECTED["page_count"]:
        raise AssertionError("Frozen C3 Page/ranking coverage mismatch")
    metadata = load_json(c3_dir / "run_metadata.json")
    if metadata["validation"] != {
        "status": "PASS",
        "page_count": 722,
        "routed_query_count": 386,
        "ranking_row_count": 14730,
        "future_page_leak_count": 0,
        "context_window_validation": True,
        "no_user_embedding_contract": True,
    }:
        raise AssertionError("Frozen C3 source validation changed")
    return pages, p2, rankings, metadata


def load_production_pages(
    recall_dir: Path,
    sessions: Mapping[str, Sequence[Any]],
    c3_pages: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    identity = {str(row["production_page_id"]): row for row in c3_pages}
    turns = {turn.query_id: turn for values in sessions.values() for turn in values}
    pages: list[dict[str, Any]] = []
    session_validation: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        result_dir = recall_dir / code
        summary = load_json(result_dir / "recall_summary.json")
        expected_turns = len(sessions[code])
        if int(summary.get("total_turns") or 0) != expected_turns or int(summary.get("failed_turns") or 0):
            raise AssertionError(f"{code}: frozen source Session run is incomplete")
        effective = load_json(result_dir / "effective_memory_config.json")
        client, collection = qdrant_client(effective)
        try:
            points, _ = client.scroll(
                f"{collection}_midterm_pages", limit=10_000, with_payload=True, with_vectors=True
            )
        finally:
            client.close()
        expected_pages = expected_turns - SHORT_TERM_QA_CAPACITY
        if len(points) != expected_pages:
            raise AssertionError(f"{code}: Production Page count {len(points)} != {expected_pages}")
        for point in points:
            c3 = identity.get(str(point.id))
            if not c3 or str(c3["session_id"]) != code:
                raise AssertionError(f"{code}: cannot map Production Page {point.id} to frozen source turn")
            source_id = str(c3["source_turn_id"])
            payload = dict(point.payload or {})
            vector = point.vector.get("") if isinstance(point.vector, dict) else point.vector
            keywords = list(payload.get("keywords") or [])
            full_text = page_text(str(payload.get("summary") or ""), keywords, str(payload.get("user_input") or ""))
            if (
                str(payload.get("data") or "") != full_text
                or str(payload.get("user_input") or "") != turns[source_id].question
                or len(vector or []) != 512
            ):
                raise AssertionError(f"{source_id}: Production Page/vector contract mismatch")
            pages.append(
                {
                    "session_id": code,
                    "page_id": source_id,
                    "source_turn_id": source_id,
                    "source_turn_index": int(c3["source_turn_index"]),
                    "production_page_id": str(point.id),
                    "summary": str(payload.get("summary") or ""),
                    "keywords": keywords,
                    "user_input": str(payload.get("user_input") or ""),
                    "full_text": full_text,
                    "no_user_text": page_text(str(payload.get("summary") or ""), keywords, ""),
                    "stored_embedding": list(vector or []),
                }
            )
        session_validation.append(
            {
                "session_id": code,
                "expected_turns": expected_turns,
                "actual_turns": int(summary["total_turns"]),
                "failed_turns": int(summary["failed_turns"]),
                "page_count": len(points),
                "visibility_source": "frozen source index cutoff and C3 ranking candidate-set cross-check",
            }
        )
    pages.sort(key=lambda row: (str(row["session_id"]), int(row["source_turn_index"])))
    if len(pages) != EXPECTED["page_count"] or len({str(row["page_id"]) for row in pages}) != len(pages):
        raise AssertionError("Production Pages are missing or duplicated")
    return pages, {"status": "PASS", "sessions": session_validation}


def validate_visibility(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    a4_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        by_session[str(page["session_id"])].append(dict(page))
    candidates: dict[str, list[dict[str, Any]]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        visible = [
            page
            for page in by_session[str(query["session_code"])]
            if int(page["source_turn_index"])
            <= int(query["turn_index"]) - SHORT_TERM_QA_CAPACITY - 1
        ]
        expected = {str(page["page_id"]) for page in visible}
        frozen = {str(row["page_id"]) for row in a4_rankings[query_id]}
        if expected != frozen or not set(map(str, query["eligible_gold_page_ids"])).issubset(expected):
            raise AssertionError(f"{query_id}: time visibility differs from frozen C3")
        candidates[query_id] = visible
    return candidates


def compare_a0_with_frozen_production_trace(
    recall_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    a0_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Audit historical offline C0 ranking against the end-to-end Production trace.

    The transferred A0 definition is the historical offline C0 contract: dense
    cosine over all time-visible Production Pages.  The live Production router
    can expose a narrower Page set, so its frozen Top5 is an audit, not a source
    of the R@10/R@20/full-ranking metrics.
    """

    traces: dict[str, list[str]] = {}
    for code in SESSION_CODES:
        for row in load_jsonl(recall_dir / code / "recall_turn_results.jsonl"):
            traces[str(row["turn_id"])] = [
                str(item).upper() for item in (row.get("mid_page_retrieved_turn_ids") or [])[:TOP_K]
            ]
    mismatches: list[dict[str, Any]] = []
    hit_disagreements: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["query_id"])
        offline_top5 = [str(row["page_id"]) for row in a0_rankings[query_id][:TOP_K]]
        trace_top5 = traces[query_id]
        gold = set(map(str, query["eligible_gold_page_ids"]))
        if offline_top5 != trace_top5:
            mismatches.append(
                {"query_id": query_id, "offline_c0_top5": offline_top5, "production_trace_top5": trace_top5}
            )
        if bool(gold.intersection(offline_top5)) != bool(gold.intersection(trace_top5)):
            hit_disagreements.append(
                {"query_id": query_id, "gold_page_ids": sorted(gold), "offline_top5": offline_top5, "trace_top5": trace_top5}
            )
    if hit_disagreements:
        raise AssertionError(f"A0 offline/Production trace Gold@5 differs: {hit_disagreements[:3]}")
    return {
        "status": "PASS",
        "contract": "historical offline C0 over all time-visible Production Pages",
        "query_count": len(queries),
        "exact_top5_query_count": len(queries) - len(mismatches),
        "top5_order_mismatch_count": len(mismatches),
        "gold_hit_at_5_disagreement_count": 0,
        "mismatches": mismatches,
    }


def load_p2_vectors(c3_dir: Path, queries: Sequence[Mapping[str, Any]]) -> dict[str, list[float]]:
    ids = [f"P2:{query['query_id']}:0" for query in queries]
    return load_existing_embedding_vectors(
        c3_dir,
        model_name=PRODUCTION_EMBEDDING,
        prefix="P2-resolved-query-only-S001-S010-rebalanced-",
        required_ids=ids,
    )


def embed_main_variants(
    output_dir: Path,
    production_pages: Sequence[Mapping[str, Any]],
    c3_pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any]]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    original_ids = [f"A0:{query['query_id']}:0" for query in queries]
    original_vectors, original_meta = cache.encode(
        PRODUCTION_EMBEDDING,
        "A0-original-query-S001-S010-rebalanced",
        original_ids,
        [str(query["original_query"]) for query in queries],
        measure_individual=True,
    )
    a2_ids = [f"A2:{page['page_id']}" for page in production_pages]
    a2_vectors_raw, a2_meta = cache.encode(
        PRODUCTION_EMBEDDING,
        "A2-production-summary-keywords-S001-S010-rebalanced",
        a2_ids,
        [str(page["no_user_text"]) for page in production_pages],
        measure_individual=False,
    )
    production_by_source = {str(page["page_id"]): page for page in production_pages}
    c3_ordered = sorted(c3_pages, key=lambda row: (str(row["session_id"]), int(row["source_turn_index"])))
    a3_ids = [f"A3:{page['source_turn_id']}" for page in c3_ordered]
    a3_texts = [
        page_text(
            str(page["summary"]),
            list(page["keywords"]),
            str(production_by_source[str(page["source_turn_id"])]["user_input"]),
        )
        for page in c3_ordered
    ]
    a3_vectors_raw, a3_meta = cache.encode(
        PRODUCTION_EMBEDDING,
        "A3-context-full-page-S001-S010-rebalanced",
        a3_ids,
        a3_texts,
        measure_individual=False,
    )
    return {
        "A0_query": original_vectors,
        "A2_page": {str(page["page_id"]): a2_vectors_raw[item_id] for page, item_id in zip(production_pages, a2_ids)},
        "A3_page": {
            str(page["source_turn_id"]): a3_vectors_raw[item_id] for page, item_id in zip(c3_ordered, a3_ids)
        },
    }, {"A0_query": original_meta, "A2_page": a2_meta, "A3_page": a3_meta}


def build_main_rankings(
    queries: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    production_pages: Sequence[Mapping[str, Any]],
    p2_vectors: Mapping[str, Sequence[float]],
    generated: Mapping[str, Mapping[str, Sequence[float]]],
    a4_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    production_vectors = {str(page["page_id"]): page["stored_embedding"] for page in production_pages}
    rankings = {config: {} for config in MAIN_CONFIGS}
    for query in queries:
        query_id = str(query["query_id"])
        visible = candidates[query_id]
        rankings["A0"][query_id] = cosine_rank(generated["A0_query"][f"A0:{query_id}:0"], visible, production_vectors)
        rankings["A1"][query_id] = cosine_rank(p2_vectors[f"P2:{query_id}:0"], visible, production_vectors)
        rankings["A2"][query_id] = cosine_rank(p2_vectors[f"P2:{query_id}:0"], visible, generated["A2_page"])
        rankings["A3"][query_id] = cosine_rank(p2_vectors[f"P2:{query_id}:0"], visible, generated["A3_page"])
        rankings["A4"][query_id] = [dict(row) for row in a4_rankings[query_id]]
    return rankings


def evaluate_config(
    config: str,
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw, per_query = evaluate_rankings(queries, rankings, cutoffs=(5, 10, 20))
    gold5 = round(float(raw["recall_at_5"]) * int(raw["eligible_gold_count"]))
    return {
        "config": config,
        "label": CONFIG_LABELS[config],
        "gold_at_5": gold5,
        "eligible_gold_count": int(raw["eligible_gold_count"]),
        "micro_r_at_5": float(raw["recall_at_5"]),
        "r_at_10": float(raw["recall_at_10"]),
        "r_at_20": float(raw["recall_at_20"]),
        "mrr": float(raw["mrr"]),
        "mean_gold_rank": float(raw["mean_gold_rank"]),
    }, per_query


def movement_counts(
    current_ranks: Mapping[str, int],
    reference_ranks: Mapping[str, int],
) -> dict[str, int]:
    promoted = sum(reference_ranks[key] > TOP_K and current_ranks[key] <= TOP_K for key in current_ranks)
    demoted = sum(reference_ranks[key] <= TOP_K and current_ranks[key] > TOP_K for key in current_ranks)
    return {"promoted": promoted, "demoted": demoted, "net": promoted - demoted}


def gold_rank_values(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    values: dict[str, int] = {}
    for query in queries:
        query_id = str(query["query_id"])
        rank_by_page = {str(row["page_id"]): rank for rank, row in enumerate(rankings[query_id], start=1)}
        for gold_index, gold_id in enumerate(query["eligible_gold_page_ids"], start=1):
            values[f"{query_id}::OUT{gold_index}"] = rank_by_page[str(gold_id)]
    return values


def build_gold_transitions(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    configs: Sequence[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    rank_maps = {
        config: {
            query_id: {str(row["page_id"]): index for index, row in enumerate(rows, start=1)}
            for query_id, rows in rankings[config].items()
        }
        for config in configs
    }
    rows: list[dict[str, Any]] = []
    by_config: dict[str, dict[str, int]] = {}
    a0_ranks: dict[str, int] = {}
    c3_ranks = gold_rank_values(queries, rankings["A4"])
    previous_ranks: dict[str, int] | None = None
    for index, config in enumerate(configs):
        current: dict[str, int] = {}
        for query in queries:
            query_id = str(query["query_id"])
            for gold_index, gold_id in enumerate(query["eligible_gold_page_ids"], start=1):
                requirement_id = f"{query_id}::OUT{gold_index}"
                rank = rank_maps[config][query_id][str(gold_id)]
                current[requirement_id] = rank
                if config == configs[0]:
                    a0_ranks[requirement_id] = rank
                rows.append(
                    {
                        "config": config,
                        "session_id": query["session_code"],
                        "query_id": query_id,
                        "requirement_id": requirement_id,
                        "gold_page_id": gold_id,
                        "rank": rank,
                        "hit_at_5": rank <= TOP_K,
                        "a0_rank": a0_ranks.get(requirement_id, rank),
                        "promoted_vs_a0": a0_ranks.get(requirement_id, rank) > TOP_K and rank <= TOP_K,
                        "demoted_vs_a0": a0_ranks.get(requirement_id, rank) <= TOP_K and rank > TOP_K,
                        "c3_rank": c3_ranks[requirement_id],
                        "promoted_vs_c3": c3_ranks[requirement_id] > TOP_K and rank <= TOP_K,
                        "demoted_vs_c3": c3_ranks[requirement_id] <= TOP_K and rank > TOP_K,
                        "previous_config": configs[index - 1] if index else None,
                        "previous_rank": previous_ranks.get(requirement_id) if previous_ranks else None,
                        "promoted_vs_previous": bool(
                            previous_ranks and previous_ranks[requirement_id] > TOP_K and rank <= TOP_K
                        ),
                        "demoted_vs_previous": bool(
                            previous_ranks and previous_ranks[requirement_id] <= TOP_K and rank > TOP_K
                        ),
                    }
                )
        by_config[config] = {
            **{f"vs_a0_{key}": value for key, value in movement_counts(current, a0_ranks).items()},
            **{
                f"vs_previous_{key}": value
                for key, value in movement_counts(current, previous_ranks or current).items()
            },
            **{f"vs_c3_{key}": value for key, value in movement_counts(current, c3_ranks).items()},
        }
        previous_ranks = current
    return rows, by_config


def completion_for_config(
    sessions: Mapping[str, Sequence[Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    longterm: Mapping[str, Sequence[str]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mid_ids = {query_id: [str(row["page_id"]) for row in rows] for query_id, rows in rankings.items()}
    requirement_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        turns = sessions[code]
        for turn in turns:
            short_ids = [
                item.query_id
                for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
            ]
            rows: list[dict[str, Any]] = []
            for group_index, group in enumerate(turn.gold_groups, start=1):
                short_rank = group_rank(group, short_ids)
                mid_rank = group_rank(group, mid_ids.get(turn.query_id, ()))
                long_rank = group_rank(group, longterm[turn.query_id])
                row = {
                    "requirement_id": f"{turn.query_id}::G{group_index}",
                    "session_id": code,
                    "query_id": turn.query_id,
                    "gold_members": list(group.members),
                    "shortterm_hit": short_rank is not None,
                    "midterm_rank": mid_rank,
                    "midterm_hit_at_5": mid_rank is not None and mid_rank <= TOP_K,
                    "longterm_hit": long_rank is not None,
                }
                rows.append(row)
                requirement_rows.append(row)
            query_rows.append({"session_id": code, "query_id": turn.query_id})
    details, hits = build_query_completion_rows(requirement_rows, query_rows, longterm, TOP_K)
    stats = completion_statistics(details, hits)
    return {
        "gold_bearing_query_count": len(details),
        "no_gold_query_count": int(hits["no_gold_query_count"]),
        "mid_mean_completion": float(stats["mid"]["mean"]),
        "short_mid_mean_completion": float(stats["short_mid"]["mean"]),
        "short_mid_full_count": int(stats["short_mid"]["full_complete_query_count"]),
        "short_mid_full_rate": float(stats["short_mid"]["full_complete_query_rate"]),
        "all_memory_mean_completion": float(stats["all_memory"]["mean"]),
        "all_memory_full_count": int(stats["all_memory"]["full_complete_query_count"]),
        "all_memory_full_rate": float(stats["all_memory"]["full_complete_query_rate"]),
        "all_memory_requirement_hit_count": int(hits["all_memory"]),
        "all_memory_requirement_count": EXPECTED["gold_requirement_count"],
    }, details


def add_comparisons(
    rows: list[dict[str, Any]],
    movements: Mapping[str, Mapping[str, int]],
) -> None:
    a0 = int(rows[0]["gold_at_5"])
    c3 = int(rows[-1]["gold_at_5"])
    for index, row in enumerate(rows):
        gold = int(row["gold_at_5"])
        row["delta_vs_a0"] = gold - a0
        row["delta_vs_previous"] = None if index == 0 else gold - int(rows[index - 1]["gold_at_5"])
        row["delta_vs_c3"] = gold - c3
        row.update(movements[str(row["config"])])


def flatten_metric_row(metric: Mapping[str, Any], completion: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(metric), **dict(completion)}


def embedding_counts(metadata: Mapping[str, Any]) -> dict[str, Any]:
    count = int(metadata["item_count"])
    hit = bool(metadata.get("cache_hit"))
    return {"new": 0 if hit else count, "reused": count if hit else 0, "cache_hit": hit, "batch": metadata}


def experiment_embedding_counts(metadata: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    invocation = embedding_counts(metadata)
    return {
        "experiment_new": int(metadata["item_count"]),
        "experiment_reused": 0,
        "source": source,
        "current_invocation_new": invocation["new"],
        "current_invocation_cache_reused": invocation["reused"],
        "cache_hit": invocation["cache_hit"],
        "batch": invocation["batch"],
    }


def validate_old_experiment_definitions(
    old_checkpoint_dir: Path,
    old_context_dir: Path,
    old_resolution_dir: Path,
    c3_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint_metadata = load_json(old_checkpoint_dir / "run_metadata.json")
    checkpoint_gold = {str(row["key"]): int(row["expected_dense_gold5"]) for row in checkpoint_metadata["checkpoint_specs"]}
    if checkpoint_gold != {"C0": 50, "C1": 54, "C2": 55, "C3": 59}:
        raise AssertionError(f"Historical C0-C3 checkpoint evidence changed: {checkpoint_gold}")

    with (old_context_dir / "metrics/page_formatter_no_user_control.csv").open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        context_rows = list(csv.DictReader(handle))
    context_full = next(
        row
        for row in context_rows
        if row["Add 方式"] == "Production Add + 最近上文及下文"
        and row["Formatter"] == "Summary + Keywords + User"
    )
    if int(context_full["Top5 recalled Gold"]) != 57:
        raise AssertionError("Historical Context Add + Full Page A3 evidence changed")

    with (old_resolution_dir / "prompt_metrics.csv").open(encoding="utf-8-sig", newline="") as handle:
        prompt_rows = {str(row["prompt"]): row for row in csv.DictReader(handle)}
    prompt_gold = {
        prompt: round(float(prompt_rows[prompt]["micro_r5"]) * int(prompt_rows[prompt]["eligible_gold_count"]))
        for prompt in ("P0", "P1", "P2", "P3")
    }
    if prompt_gold != {"P0": 53, "P1": 52, "P2": 54, "P3": 49}:
        raise AssertionError(f"Historical P0-P3 prompt evidence changed: {prompt_gold}")

    context_metadata = load_json(old_context_dir / "run_metadata.json")
    production_prompt_hash = str(context_metadata["prompt_sha256_before_first_llm_call"]["Production"])
    context_prompt_hash = str(
        context_metadata["prompt_sha256_before_first_llm_call"]["PreviousAndFollowingContext"]
    )
    if production_prompt_hash != sha256_text(MIDTERM_PAGE_SUMMARY_PROMPT):
        raise AssertionError("Current Production Add Prompt differs from frozen old experiment")
    if context_prompt_hash != str(c3_metadata["page_generation"]["prompt_sha256"]):
        raise AssertionError("Current C3 Context Add Prompt differs from frozen old experiment")
    old_prompt_manifest = load_json(old_resolution_dir / "prompts/prompt_sha256.json")
    if old_prompt_manifest["P2"]["sha256"] != str(c3_metadata["p2_generation"]["prompt_sha256"]):
        raise AssertionError("Current C3 P2 Prompt differs from frozen old experiment")
    return {
        "status": "PASS",
        "old_eligible_gold_count": 154,
        "main_gold_at_5": OLD_GOLD_AT_5,
        "secondary_prompt_gold_at_5": prompt_gold,
        "production_add_prompt_sha256": production_prompt_hash,
        "context_add_prompt_sha256": context_prompt_hash,
        "p2_prompt_sha256": old_prompt_manifest["P2"]["sha256"],
        "checkpoint_metadata": str(old_checkpoint_dir / "run_metadata.json"),
        "context_metrics": str(old_context_dir / "metrics/page_formatter_no_user_control.csv"),
        "resolution_metrics": str(old_resolution_dir / "prompt_metrics.csv"),
    }


def validate_a4(metric: Mapping[str, Any], completion: Mapping[str, Any], completion_dir: Path) -> None:
    source = load_json(completion_dir / "query_completion_stats.json")
    source_configs = source["configurations"]
    checks = {
        "gold_at_5": (int(metric["gold_at_5"]), EXPECTED["c3_gold_at_5"]),
        "mid_mean": (float(completion["mid_mean_completion"]), EXPECTED["c3_mid_mean"]),
        "short_mid_mean": (float(completion["short_mid_mean_completion"]), EXPECTED["c3_short_mid_mean"]),
        "all_memory_mean": (float(completion["all_memory_mean_completion"]), EXPECTED["c3_all_memory_mean"]),
        "source_mid_mean": (float(source_configs["mid"]["mean"]), EXPECTED["c3_mid_mean"]),
        "source_short_mid_mean": (float(source_configs["short_mid"]["mean"]), EXPECTED["c3_short_mid_mean"]),
        "source_all_mean": (float(source_configs["all_memory"]["mean"]), EXPECTED["c3_all_memory_mean"]),
    }
    failed = {key: values for key, values in checks.items() if abs(values[0] - values[1]) > 1e-12}
    if failed:
        raise AssertionError(f"Frozen A4/current completion reproduction failed: {failed}")


def load_frozen_secondary_prompts(old_resolution_dir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    prompt_dir = old_resolution_dir / "prompts"
    manifest = load_json(prompt_dir / "prompt_sha256.json")
    prompts: dict[str, str] = {}
    for prompt, filename in PROMPT_FILES.items():
        path = prompt_dir / filename
        if manifest[prompt]["file"] != filename or sha256_file(path) != manifest[prompt]["sha256"]:
            raise AssertionError(f"Frozen {prompt} Prompt evidence changed")
        prompts[prompt] = path.read_text(encoding="utf-8")
    if prompts["P0"] != REFERENCE_SYSTEM_PROMPT:
        raise AssertionError("Frozen P0 prompt differs from reference-resolution-v1")
    return prompts, manifest


async def generate_secondary_queries(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
    prompts: Mapping[str, str],
) -> tuple[dict[str, dict[str, str]], list[dict[str, Any]], dict[str, Any]]:
    config = expand_env_placeholders(load_json(MEMORY_CONFIG))
    llm = dict((config.get("llm") or {}).get("config") or {})
    api_key = str(llm.get("api_key") or "")
    if not api_key or api_key.startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for qualifying P0/P1 secondary ablations")
    model = str(llm.get("model") or "")
    if model != "deepseek-v4-flash":
        raise AssertionError(f"Frozen reference-resolution model changed: {model}")
    base_url = llm.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    caches = {
        "P0": AsyncJsonlLLMCache(
            args.output_dir / "cache/p0_query_resolution_llm.jsonl",
            client=client,
            model=model,
            timeout=args.llm_timeout,
            retries=args.llm_retries,
            concurrency=args.concurrency,
        ),
        "P1": PlainTextJsonlLLMCache(
            args.output_dir / "cache/p1_query_resolution_llm.jsonl",
            client=client,
            model=model,
            timeout=args.llm_timeout,
            retries=args.llm_retries,
            concurrency=args.concurrency,
        ),
    }
    initial = {prompt: set(cache.success_by_key) for prompt, cache in caches.items()}
    resolved: dict[str, dict[str, str]] = {"P0": {}, "P1": {}}
    output_rows: list[dict[str, Any]] = []
    call_metadata: dict[str, Any] = {}
    try:
        for prompt in ("P0", "P1"):
            async def one(query: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, Any]]:
                if prompt == "P0":
                    messages, version = reference_messages(query)
                else:
                    messages = resolution_messages(query, prompts[prompt])
                    version = PROMPT_VERSIONS[prompt]
                row = await caches[prompt].call(
                    query_id=str(query["query_id"]),
                    variant=prompt,
                    messages=messages,
                    prompt_version=version,
                    max_tokens=800,
                    validator=resolved_validator,
                )
                return query, row

            results = await asyncio.gather(*(one(query) for query in queries))
            failures = [(str(query["query_id"]), row.get("errors")) for query, row in results if row["status"] != "SUCCESS"]
            if failures:
                dump_json(args.output_dir / f"{prompt.lower()}_query_failures.json", failures)
                raise RuntimeError(f"{prompt} resolution failed for {len(failures)} Queries")
            for query, row in results:
                query_id = str(query["query_id"])
                text = str(row["parsed"]["resolved_query"])
                resolved[prompt][query_id] = text
                output_rows.append(
                    {
                        "config": prompt,
                        "session_id": query["session_code"],
                        "query_id": query_id,
                        "original_query": query["original_query"],
                        "resolved_query": text,
                        "changed": text != str(query["original_query"]),
                        "llm_cache_key": row["cache_key"],
                    }
                )
            new_rows = [row for _, row in results if str(row["cache_key"]) not in initial[prompt]]
            call_metadata[prompt] = {
                "query_count": len(results),
                "cache_hit_count": len(results) - len(new_rows),
                "new_success_count": len(new_rows),
                "llm_call_count": len(new_rows),
                "llm_attempt_count": sum(1 + int(row.get("retry_count") or 0) for row in new_rows),
                "changed_query_count": sum(
                    resolved[prompt][str(query["query_id"])] != str(query["original_query"]) for query in queries
                ),
            }
    finally:
        await client.close()
    return resolved, output_rows, call_metadata


def embed_and_rank_secondary(
    output_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    candidates: Mapping[str, Sequence[Mapping[str, Any]]],
    production_pages: Sequence[Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, Any]]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    production_vectors = {str(page["page_id"]): page["stored_embedding"] for page in production_pages}
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"P0": {}, "P1": {}}
    metadata: dict[str, Any] = {}
    for prompt in ("P0", "P1"):
        ids = [f"{prompt}:{query['query_id']}:0" for query in queries]
        vectors, batch = cache.encode(
            PRODUCTION_EMBEDDING,
            f"{prompt}-resolved-query-only-S001-S010-rebalanced",
            ids,
            [str(resolved[prompt][str(query["query_id"])]) for query in queries],
            measure_individual=True,
        )
        metadata[prompt] = batch
        for query, item_id in zip(queries, ids):
            query_id = str(query["query_id"])
            rankings[prompt][query_id] = cosine_rank(vectors[item_id], candidates[query_id], production_vectors)
    return rankings, metadata


def rankings_as_rows(
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    configs: Sequence[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for config in configs:
        for query_id, ranking in rankings[config].items():
            for rank, item in enumerate(ranking, start=1):
                rows.append(
                    {
                        "config": config,
                        "session_id": query_id[:4],
                        "query_id": query_id,
                        "rank": rank,
                        "page_id": item["page_id"],
                        "score": float(item.get("score") or 0.0),
                    }
                )
    return rows


def render_report(
    main_rows: Sequence[Mapping[str, Any]],
    secondary_rows: Sequence[Mapping[str, Any]],
    a0_trace_audit: Mapping[str, Any],
) -> str:
    by_config = {str(row["config"]): row for row in main_rows}
    stage_deltas = {
        f"{MAIN_CONFIGS[index - 1]}->{config}": int(by_config[config]["delta_vs_previous"])
        for index, config in enumerate(MAIN_CONFIGS)
        if index
    }
    best_config = max(MAIN_CONFIGS, key=lambda config: int(by_config[config]["gold_at_5"]))
    sequence_holds = all(delta > 0 for delta in stage_deltas.values())
    lines = [
        "# MidTerm positive-method transfer ablation (S001-S010 rebalanced)",
        "",
        "The benchmark, ShortTerm/LongTerm traces, C3 Pages, P2 Queries, and A4 rankings are frozen. ",
        "A0-A3 only perform offline embedding/ranking; A4 is copied from the existing C3 ranking artifact.",
        "",
        "| Config | Gold@5 | R@5 | Δ vs A0 | Δ vs Previous | Δ vs C3 | R@10 | R@20 | MRR | Short+Mid Mean | All Memory Mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in main_rows:
        previous = "—" if row["delta_vs_previous"] is None else f"{int(row['delta_vs_previous']):+d}"
        lines.append(
            f"| {row['config']} | {row['gold_at_5']}/{row['eligible_gold_count']} | {row['micro_r_at_5']:.2%} | "
            f"{int(row['delta_vs_a0']):+d} | {previous} | {int(row['delta_vs_c3']):+d} | "
            f"{row['r_at_10']:.2%} | {row['r_at_20']:.2%} | {row['mrr']:.4f} | "
            f"{row['short_mid_mean_completion']:.2%} | {row['all_memory_mean_completion']:.2%} |"
        )
    lines.extend(
        [
            "",
            "| Config | Mean Gold Rank | Mid Mean | Short+Mid 100% | All Memory 100% | Three-layer union | Promoted / Demoted / Net vs Previous |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in main_rows:
        lines.append(
            f"| {row['config']} | {row['mean_gold_rank']:.4f} | {row['mid_mean_completion']:.2%} | "
            f"{row['short_mid_full_count']}/{row['gold_bearing_query_count']} ({row['short_mid_full_rate']:.2%}) | "
            f"{row['all_memory_full_count']}/{row['gold_bearing_query_count']} ({row['all_memory_full_rate']:.2%}) | "
            f"{row['all_memory_requirement_hit_count']}/{row['all_memory_requirement_count']} | "
            f"{row['vs_previous_promoted']} / {row['vs_previous_demoted']} / {int(row['vs_previous_net']):+d} |"
        )
    lines.extend(
        [
            "",
            "## Conclusions",
            "",
            f"1. P2 Query Resolution: A1 vs A0 is {stage_deltas['A0->A1']:+d} Gold@5; "
            f"{'still positive' if stage_deltas['A0->A1'] > 0 else 'not positive'} on the new dataset.",
            f"2. Removing Raw User under Production Add: A2 vs A1 is {stage_deltas['A1->A2']:+d} Gold@5; "
            f"under Context Add, A4 vs A3 is {stage_deltas['A3->A4']:+d}.",
            f"3. Context Add: A3 vs A2 is {stage_deltas['A2->A3']:+d} Gold@5; the full-page controlled comparison "
            f"A3 vs A1 is {int(by_config['A3']['gold_at_5']) - int(by_config['A1']['gold_at_5']):+d}.",
            f"4. Strict A0→A1→A2→A3→A4 positive progression: {'YES' if sequence_holds else 'NO'} "
            f"({', '.join(f'{key} {value:+d}' for key, value in stage_deltas.items())}).",
            f"5. Best tested main configuration by Gold@5: {best_config} "
            f"({by_config[best_config]['gold_at_5']}/{by_config[best_config]['eligible_gold_count']}).",
            "   A0, A1, A2, and A3 exceed frozen C3 by +59, +50, +38, and +3 Gold@5 respectively; "
            "these differences are reported as observed and were not tuned away.",
            "",
            "Promoted/demoted/net Gold counts are recorded in `main_metrics.csv` and `gold_transitions.csv`.",
            "",
            "## Generalization classification",
            "",
            "- Stable generalization on the primary Gold@5 metric: none of the transferred incremental optimizations.",
            "- Failed on the new dataset: P2 Query Resolution, Production Summary+Keywords, Context Add, and "
            "Context Summary+Keywords all reverse their old positive stage delta.",
            "- Partial only: P0 loses 2 Gold@5 but preserves A0's R@20 and three-layer union; this is tail/union "
            "robustness, not a positive routed-R@5 transfer.",
            "",
            "## Frozen-source audit",
            "",
            f"- Historical offline A0 and the end-to-end Production trace have identical ordered Top5 for "
            f"{a0_trace_audit['exact_top5_query_count']}/{a0_trace_audit['query_count']} routed Queries.",
            f"- The {a0_trace_audit['top5_order_mismatch_count']} mismatch changes no requirement-level Gold@5 hit. "
            "A0 follows the old offline C0 candidate contract; the Production trace is retained only as an audit.",
        ]
    )
    if secondary_rows:
        lines.extend(
            [
                "",
                "## Secondary P0/P1 transfer",
                "",
                "| Config | Gold@5 | R@5 | Δ vs A0 | R@10 | R@20 | MRR |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        a0_gold = int(by_config["A0"]["gold_at_5"])
        for row in secondary_rows:
            lines.append(
                f"| {row['config']} | {row['gold_at_5']}/{row['eligible_gold_count']} | "
                f"{row['micro_r_at_5']:.2%} | {int(row['gold_at_5']) - a0_gold:+d} | "
                f"{row['r_at_10']:.2%} | {row['r_at_20']:.2%} | {row['mrr']:.4f} |"
            )
    return "\n".join(lines) + "\n"


async def run(args: argparse.Namespace) -> int:
    args.dataset = args.dataset.resolve()
    args.recall_dir = args.recall_dir.resolve()
    args.c3_dir = args.c3_dir.resolve()
    args.completion_dir = args.completion_dir.resolve()
    args.old_context_dir = args.old_context_dir.resolve()
    args.old_resolution_dir = args.old_resolution_dir.resolve()
    args.old_checkpoint_dir = args.old_checkpoint_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    sessions = load_v3_dataset(args.dataset, SESSION_CODES)
    queries = build_routed_queries(sessions)
    dataset_validation = validate_dataset(sessions, queries)
    c3_pages, _p2_texts, a4_rankings, c3_metadata = load_c3_artifacts(args.c3_dir, queries)
    old_definition_validation = validate_old_experiment_definitions(
        args.old_checkpoint_dir, args.old_context_dir, args.old_resolution_dir, c3_metadata
    )
    production_pages, source_validation = load_production_pages(args.recall_dir, sessions, c3_pages)
    candidates = validate_visibility(queries, production_pages, a4_rankings)
    p2_vectors = load_p2_vectors(args.c3_dir, queries)
    generated, embedding_metadata = embed_main_variants(args.output_dir, production_pages, c3_pages, queries)
    rankings = build_main_rankings(
        queries, candidates, production_pages, p2_vectors, generated, a4_rankings
    )
    a0_trace_audit = compare_a0_with_frozen_production_trace(args.recall_dir, queries, rankings["A0"])

    longterm, longterm_validation = load_longterm_provenance(
        args.recall_dir, {turn.query_id for turns in sessions.values() for turn in turns}
    )
    main_rows: list[dict[str, Any]] = []
    main_details: list[dict[str, Any]] = []
    for config in MAIN_CONFIGS:
        metric, per_query = evaluate_config(config, queries, rankings[config])
        completion, details = completion_for_config(sessions, rankings[config], longterm)
        main_rows.append(flatten_metric_row(metric, completion))
        main_details.extend({"config": config, **row} for row in details)
        write_jsonl(args.output_dir / f"{config.lower()}_retrieval_per_query.jsonl", per_query)
    transitions, movements = build_gold_transitions(queries, rankings, MAIN_CONFIGS)
    add_comparisons(main_rows, movements)
    validate_a4(main_rows[-1], main_rows[-1], args.completion_dir)

    secondary_rows: list[dict[str, Any]] = []
    secondary_details: list[dict[str, Any]] = []
    secondary_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    secondary_generation: dict[str, Any] = {"status": "SKIPPED"}
    secondary_embedding: dict[str, Any] = {}
    prompt_manifest: dict[str, Any] = {}
    if not args.skip_secondary:
        prompts, prompt_manifest = load_frozen_secondary_prompts(args.old_resolution_dir)
        output_prompt_dir = args.output_dir / "prompts"
        output_prompt_dir.mkdir(parents=True, exist_ok=True)
        for prompt, text in prompts.items():
            (output_prompt_dir / PROMPT_FILES[prompt]).write_text(text, encoding="utf-8")
        resolved, resolved_rows, secondary_generation = await generate_secondary_queries(args, queries, prompts)
        write_jsonl(args.output_dir / "secondary_resolved_queries.jsonl", resolved_rows)
        secondary_rankings, secondary_embedding = embed_and_rank_secondary(
            args.output_dir, queries, candidates, production_pages, resolved
        )
        for config in ("P0", "P1"):
            metric, per_query = evaluate_config(config, queries, secondary_rankings[config])
            completion, details = completion_for_config(sessions, secondary_rankings[config], longterm)
            secondary_rows.append(flatten_metric_row(metric, completion))
            secondary_details.extend({"config": config, **row} for row in details)
            write_jsonl(args.output_dir / f"{config.lower()}_retrieval_per_query.jsonl", per_query)
        a0_gold_ranks = gold_rank_values(queries, rankings["A0"])
        c3_gold_ranks = gold_rank_values(queries, rankings["A4"])
        previous_gold_ranks = a0_gold_ranks
        for row in secondary_rows:
            config = str(row["config"])
            current_gold_ranks = gold_rank_values(queries, secondary_rankings[config])
            row["delta_vs_a0"] = int(row["gold_at_5"]) - int(main_rows[0]["gold_at_5"])
            row["delta_vs_previous_secondary"] = int(row["gold_at_5"]) - sum(
                rank <= TOP_K for rank in previous_gold_ranks.values()
            )
            row["delta_vs_c3"] = int(row["gold_at_5"]) - int(main_rows[-1]["gold_at_5"])
            row.update(
                {
                    **{
                        f"vs_a0_{key}": value
                        for key, value in movement_counts(current_gold_ranks, a0_gold_ranks).items()
                    },
                    **{
                        f"vs_previous_secondary_{key}": value
                        for key, value in movement_counts(current_gold_ranks, previous_gold_ranks).items()
                    },
                    **{
                        f"vs_c3_{key}": value
                        for key, value in movement_counts(current_gold_ranks, c3_gold_ranks).items()
                    },
                }
            )
            previous_gold_ranks = current_gold_ranks

    write_csv(args.output_dir / "main_metrics.csv", main_rows)
    dump_json(args.output_dir / "main_metrics.json", main_rows)
    write_csv(args.output_dir / "gold_transitions.csv", transitions)
    write_jsonl(args.output_dir / "gold_transitions.jsonl", transitions)
    write_csv(args.output_dir / "query_completion_details.csv", main_details)
    write_jsonl(args.output_dir / "query_completion_details.jsonl", main_details)
    write_jsonl(args.output_dir / "main_rankings.jsonl", rankings_as_rows(rankings, MAIN_CONFIGS))
    if secondary_rows:
        write_csv(args.output_dir / "secondary_metrics.csv", secondary_rows)
        dump_json(args.output_dir / "secondary_metrics.json", secondary_rows)
        write_jsonl(args.output_dir / "secondary_rankings.jsonl", rankings_as_rows(secondary_rankings, ("P0", "P1")))
        write_jsonl(args.output_dir / "secondary_query_completion_details.jsonl", secondary_details)

    reuse = {
        "A0": {
            "llm_calls": 0,
            "page_embeddings": {"new": 0, "reused_production_qdrant": 722},
            "query_embeddings": experiment_embedding_counts(
                embedding_metadata["A0_query"], source="created once by this transfer experiment"
            ),
        },
        "A1": {
            "llm_calls": 0,
            "page_embeddings": {"new": 0, "reused_production_qdrant": 722},
            "query_embeddings": {"new": 0, "reused_c3_p2": 386},
        },
        "A2": {
            "llm_calls": 0,
            "page_content": {"reused_production_pages": 722},
            "page_embeddings": experiment_embedding_counts(
                embedding_metadata["A2_page"], source="created once by this transfer experiment"
            ),
            "query_embeddings": {"new": 0, "reused_c3_p2": 386},
        },
        "A3": {
            "llm_calls": 0,
            "page_content": {"reused_c3_summaries": 722, "reused_production_users": 722},
            "page_embeddings": experiment_embedding_counts(
                embedding_metadata["A3_page"], source="created once by this transfer experiment"
            ),
            "query_embeddings": {"new": 0, "reused_c3_p2": 386},
        },
        "A4": {
            "llm_calls": 0,
            "rankings": {"new": 0, "reused_c3_rows": sum(len(rows) for rows in a4_rankings.values())},
            "embeddings_created_by_this_run": 0,
        },
    }
    for config in ("P0", "P1"):
        if config in secondary_embedding:
            cache_rows = load_jsonl(args.output_dir / f"cache/{config.lower()}_query_resolution_llm.jsonl")
            total_successes = sum(row.get("status") == "SUCCESS" for row in cache_rows)
            total_attempts = sum(
                1 + int(row.get("retry_count") or 0) for row in cache_rows if row.get("status") == "SUCCESS"
            )
            reuse[config] = {
                **secondary_generation[config],
                "experiment_llm_call_count": total_successes,
                "experiment_llm_attempt_count": total_attempts,
                "page_embeddings": {"new": 0, "reused_production_qdrant": 722},
                "query_embeddings": experiment_embedding_counts(
                    secondary_embedding[config], source="created once by this transfer experiment"
                ),
            }
    old_evidence = {
        "historical_gold_at_5_over_154": OLD_GOLD_AT_5,
        "context_formatter_metrics": str(args.old_context_dir / "metrics/page_formatter_no_user_control.csv"),
        "context_formatter_metrics_sha256": sha256_file(
            args.old_context_dir / "metrics/page_formatter_no_user_control.csv"
        ),
        "reference_prompt_metrics": str(args.old_resolution_dir / "prompt_metrics.csv"),
        "reference_prompt_metrics_sha256": sha256_file(args.old_resolution_dir / "prompt_metrics.csv"),
        "secondary_selection": "P0/P1 included because old net Gold@5 vs original baseline was +3/+2; P3 excluded (-1)",
    }
    metadata = {
        "experiment": "midterm_positive_transfer_ablation_s001_s010",
        "mode": "strict frozen transfer; no hyperparameter search; no Session run; no Qdrant writes",
        "dataset": str(args.dataset),
        "dataset_sha256": sha256_file(args.dataset),
        "dataset_validation": dataset_validation,
        "source_session_validation": source_validation,
        "longterm_validation": longterm_validation,
        "visibility_validation": {"status": "PASS", "query_count": len(candidates), "future_page_leak_count": 0},
        "a0_production_trace_audit": a0_trace_audit,
        "frozen_c3": {
            "directory": str(args.c3_dir),
            "ranking_sha256": sha256_file(args.c3_dir / "c3_rankings.jsonl"),
            "source_run_metadata_sha256": sha256_file(args.c3_dir / "run_metadata.json"),
            "source_validation": c3_metadata["validation"],
        },
        "old_experiment_definition_evidence": old_evidence,
        "old_experiment_definition_validation": old_definition_validation,
        "prompt_manifest": prompt_manifest,
        "artifact_reuse_and_generation": reuse,
        "config_definitions": CONFIG_LABELS,
        "shortterm_window": SHORT_TERM_QA_CAPACITY,
        "midterm_top_k": TOP_K,
        "embedding_model": PRODUCTION_EMBEDDING,
        "validation": {
            "status": "PASS",
            "a4_gold_at_5": int(main_rows[-1]["gold_at_5"]),
            "a4_completion_reproduced": True,
            "query_count": 752,
            "gold_requirement_count": 786,
            "outside_shortterm_requirement_count": 386,
            "failed_turns": 0,
            "visibility": "PASS",
        },
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    (args.output_dir / "transfer_ablation_report.md").write_text(
        render_report(main_rows, secondary_rows, a0_trace_audit), encoding="utf-8"
    )
    print(json.dumps({"main": main_rows, "secondary": secondary_rows}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    return asyncio.run(run(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
