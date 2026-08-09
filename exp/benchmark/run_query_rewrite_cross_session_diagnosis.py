"""Diagnose Original Query vs Original + Standalone Rewrite across S001-S005.

This is an offline, time-safe experiment. It reuses immutable query-time
snapshots and stored production P0 Page vectors, and never replays a Session or
mutates production retrieval code/data.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    cosine_rank,
    evaluate_rankings,
    load_jsonl,
    page_representation,
    write_jsonl,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    load_existing_embedding_vectors,
)
from exp.benchmark.run_midterm_retrieval_experiments import (  # noqa: E402
    AsyncJsonlLLMCache,
    EmbeddingCache,
    rewrite_messages,
    standalone_validator,
)


S001_RESULT_DIR = REPO_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
MULTI_SESSION_DIR = REPO_ROOT / "exp/results/midterm_multi_session_validation_no_thinking"
OUTPUT_DIR = REPO_ROOT / "exp/results/query_rewrite_cross_session_diagnosis"
SESSION_CODES = ("S001", "S002", "S003", "S004", "S005")
REWRITE_VARIANT = "Q5"
OUTPUT_K = 5
DISPLAY_K = 10
TRANSITION_TOP_K = 20

REFERENCE_PATTERN = re.compile(r"上一轮|刚才|前面|前面的|这个|这组|这些|接着|继续|沿着|再结合|该变化")
SUBJECT_TERMS = ("贵州茅台", "比亚迪", "宁德时代")
METRIC_TERMS = (
    "营业收入",
    "收入",
    "归母净利润",
    "扣非归母净利润",
    "扣非净利润",
    "经营活动现金流净额",
    "经营现金流净额",
    "经营现金流",
    "现金流",
    "总资产",
    "归母股东权益",
    "股东权益",
    "权益",
    "资产权益比",
    "总资产/归母股东权益",
    "杠杆",
    "资产周转",
    "现金利润比",
    "利润率",
)
CONCLUSION_TERMS = (
    "连续上升",
    "连续下降",
    "先升后降",
    "先降后升",
    "增速放缓",
    "出现拐点",
    "中途反转",
    "存在错位",
    "效率问题",
    "现金质量结论",
    "风险排序结论",
    "反例",
    "主结论",
    "冲突",
    "不同步",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cross-session Query Rewrite Page-recall diagnosis")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=1)
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


def session_code(session_id: str) -> str:
    return session_id.split("_", 1)[0]


def query_session_code(query_id: str) -> str:
    return query_id.split("-", 1)[0]


def load_snapshots() -> dict[str, dict[str, Any]]:
    locations = {
        "S001": S001_RESULT_DIR / "snapshot",
        **{code: MULTI_SESSION_DIR / "snapshots" / code for code in SESSION_CODES[1:]},
    }
    snapshots: dict[str, dict[str, Any]] = {}
    all_page_ids: dict[str, str] = {}
    for code, directory in locations.items():
        manifest = load_json(directory / "snapshot_manifest.json")
        queries = load_jsonl(directory / "queries.jsonl")
        pages = load_jsonl(directory / "pages.jsonl")
        visibility_rows = load_jsonl(directory / "query_page_visibility.jsonl")
        if session_code(str(manifest["session_id"])) != code:
            raise ValueError(f"{code}: wrong snapshot session: {manifest['session_id']}")
        if manifest.get("production_embedding_model") != PRODUCTION_EMBEDDING:
            raise ValueError(f"{code}: embedding model is not frozen to {PRODUCTION_EMBEDDING}")
        if int(manifest.get("future_page_leak_count") or 0):
            raise ValueError(f"{code}: manifest reports future Page leakage")
        if int(manifest.get("evaluated_query_count") or 0) != len(queries):
            raise ValueError(f"{code}: incomplete Query snapshot")
        page_by_id = {str(page["page_id"]): page for page in pages}
        visibility = {str(row["query_id"]): row for row in visibility_rows}
        if len(page_by_id) != len(pages) or len(visibility) != len(queries):
            raise ValueError(f"{code}: duplicate Page IDs or incomplete visibility")
        for page_id, page in page_by_id.items():
            if page["current_embedding_text"] != page_representation(page, "P0"):
                raise ValueError(f"{code}: Page {page_id} does not contain exact production P0 text")
            if len(page.get("stored_embedding") or []) != 512:
                raise ValueError(f"{code}: Page {page_id} does not contain a 512-dimensional stored vector")
            previous_session = all_page_ids.setdefault(page_id, code)
            if previous_session != code:
                raise ValueError(f"Page ID is shared across Sessions: {page_id}")
        for query in queries:
            query_id = str(query["query_id"])
            if not query_id.startswith(code) or session_code(str(query["session_id"])) != code:
                raise ValueError(f"{code}: cross-Session Query {query_id}")
            visible = visibility[query_id]
            if int(visible.get("future_page_leak_count") or 0):
                raise ValueError(f"{query_id}: future Page leakage")
            visible_ids = {str(item) for item in visible.get("visible_page_ids") or []}
            if not visible_ids.issubset(page_by_id):
                raise ValueError(f"{query_id}: visibility contains a foreign Page")
            if not set(map(str, query.get("eligible_gold_page_ids") or [])).issubset(visible_ids):
                raise ValueError(f"{query_id}: eligible Gold is not visible at Query time")
        snapshots[code] = {
            "manifest": manifest,
            "queries": queries,
            "pages": pages,
            "visibility": visibility_rows,
            "path": str(directory),
        }
    return snapshots


def seed_s001_rewrite_cache(target_path: Path) -> int:
    source_rows = [
        row
        for row in load_jsonl(S001_RESULT_DIR / "cache/query_rewrites.jsonl")
        if row.get("variant") == "Q4" and row.get("status") == "SUCCESS"
    ]
    source_by_query = {str(row["query_id"]): row for row in source_rows}
    if len(source_by_query) != 14:
        raise ValueError(f"Expected 14 reusable S001 standalone rewrites, got {len(source_by_query)}")
    existing = load_jsonl(target_path)
    existing_keys = {str(row.get("cache_key")) for row in existing}
    added = [row for row in source_by_query.values() if str(row.get("cache_key")) not in existing_keys]
    if added:
        write_jsonl(target_path, [*existing, *added])
    return len(source_by_query)


async def load_or_generate_rewrites(
    args: argparse.Namespace,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, Any]]:
    cache_path = args.output_dir / "cache/query_rewrites.jsonl"
    reused_s001 = seed_s001_rewrite_cache(cache_path)
    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for missing S002-S005 rewrites")
    model = str(llm_config.get("model") or "deepseek-chat")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    cache = AsyncJsonlLLMCache(
        cache_path,
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )
    initial_success_keys = set(cache.success_by_key)

    async def one(query: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        messages, version = rewrite_messages(query, "Q4")
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant="Q4",
            messages=messages,
            prompt_version=version,
            max_tokens=800,
            validator=standalone_validator,
        )
        return str(query["query_id"]), row

    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    try:
        results = await asyncio.gather(*(one(query) for query in all_queries))
    finally:
        await client.close()
    failures = {query_id: row.get("errors") for query_id, row in results if row.get("status") != "SUCCESS"}
    if failures:
        dump_json(args.output_dir / "rewrite_failures.json", failures)
        raise RuntimeError(f"Standalone rewrite failed for {len(failures)} Queries")
    rewrites = {query_id: str(row["parsed"]["standalone_query"]) for query_id, row in results}
    new_rows = [row for _, row in results if str(row["cache_key"]) not in initial_success_keys]
    reused_rows = [row for _, row in results if str(row["cache_key"]) in initial_success_keys]
    # This output directory is the durable experiment cache. S001 rows are the
    # only seeded old rows; every successful S002-S005 row records one rewrite
    # generated for this experiment, even when a later reproducibility rerun is
    # a cache hit.
    generated_rows_by_query = {
        str(row["query_id"]): row
        for row in load_jsonl(cache_path)
        if row.get("variant") == "Q4"
        and row.get("status") == "SUCCESS"
        and query_session_code(str(row.get("query_id") or "")) in SESSION_CODES[1:]
    }
    metadata = {
        "model": model,
        "thinking_mode": "disabled",
        "prompt_version": "standalone-v1",
        "query_count": len(all_queries),
        "s001_rewrite_source_cache_reused": reused_s001,
        "current_run_cache_hit_count": len(reused_rows),
        "current_run_cache_miss_count": len(new_rows),
        "new_rewrite_query_count": len(generated_rows_by_query),
        "new_llm_api_attempt_count": sum(
            1 + int(row.get("retry_count") or 0) for row in generated_rows_by_query.values()
        ),
        "cache_path": str(cache_path),
    }
    return rewrites, metadata


def actual_rewrite_text(query: Mapping[str, Any], standalone: str) -> str:
    return f"Original query: {query['original_query']}\nStandalone query: {standalone}"


def load_query_vectors(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    rewrites: Mapping[str, str],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    vectors: dict[str, list[float]] = {}
    s001_queries = snapshots["S001"]["queries"]
    s001_ids = [
        item_id
        for query in s001_queries
        for item_id in (f"Q0:{query['query_id']}:0", f"Q5:{query['query_id']}:0")
    ]
    vectors.update(
        load_existing_embedding_vectors(
            S001_RESULT_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="queries-",
            required_ids=s001_ids,
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
    rewrite_ids = [f"Q5:{query['query_id']}:0" for query in heldout_queries]
    rewrite_texts = [actual_rewrite_text(query, rewrites[str(query["query_id"])]) for query in heldout_queries]
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    rewrite_vectors, rewrite_metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "heldout-Q5-S002-S005-original-plus-standalone",
        rewrite_ids,
        rewrite_texts,
        measure_individual=True,
    )
    vectors.update(rewrite_vectors)
    return vectors, {
        "s001_q0_embedding_cache_reused": len(s001_queries),
        "s001_q5_embedding_cache_reused": len(s001_queries),
        "heldout_q0_embedding_cache_reused": len(heldout_queries),
        "heldout_q5_embedding_cache_hit": bool(rewrite_metadata.get("cache_hit")),
        "heldout_q5_embedding_count": len(rewrite_ids),
        "heldout_q5_embedding_metadata": rewrite_metadata,
    }


def rank_snapshot(
    snapshot: Mapping[str, Any],
    vectors: Mapping[str, Sequence[float]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
    page_vectors = {page_id: page["stored_embedding"] for page_id, page in pages_by_id.items()}
    visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
    baseline: dict[str, list[dict[str, Any]]] = {}
    rewrite: dict[str, list[dict[str, Any]]] = {}
    for query in snapshot["queries"]:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        baseline[query_id] = cosine_rank(vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors)
        rewrite[query_id] = cosine_rank(vectors[f"Q5:{query_id}:0"], visible_pages, page_vectors)
    return baseline, rewrite


def validate_reused_baselines(
    metrics_by_session: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    old_s001 = {row["variant"]: row for row in read_csv(S001_RESULT_DIR / "query_ablation.csv")}
    old_multi = {row["session_code"]: row for row in read_csv(MULTI_SESSION_DIR / "per_session_metrics.csv")}
    checks: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        current = metrics_by_session[code]["baseline"]
        expected = old_s001["Q0"] if code == "S001" else old_multi[code]
        prefix = "" if code == "S001" else "baseline_"
        for cutoff in (5, 10, 20):
            key = f"recall_at_{cutoff}"
            expected_value = float(expected[f"{prefix}{key}"])
            actual_value = float(current[key])
            if not math.isclose(actual_value, expected_value, abs_tol=1e-12):
                raise AssertionError(f"{code} reused Q0 mismatch for {key}: {actual_value} != {expected_value}")
        checks.append({"session_id": code, "status": "MATCHED", "baseline_r5": current["recall_at_5"]})
    s001_rewrite = metrics_by_session["S001"]["rewrite"]
    for key in ("recall_at_5", "recall_at_10", "recall_at_20", "mrr", "mean_gold_rank"):
        actual_value = float(s001_rewrite[key])
        expected_value = float(old_s001["Q5"][key])
        if not math.isclose(actual_value, expected_value, abs_tol=1e-12):
            raise AssertionError(f"S001 reused Q5 mismatch for {key}: {actual_value} != {expected_value}")
    return {"status": "PASS", "baseline_checks": checks, "s001_q5_status": "MATCHED_OLD_Q5"}


def rank_map(ranking: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {str(row["page_id"]): row for row in ranking}


def top_rows(ranking: Sequence[Mapping[str, Any]], gold_ids: set[str], count: int) -> list[dict[str, Any]]:
    return [
        {
            "rank": int(row["rank"]),
            "page_id": str(row["page_id"]),
            "source_turn_id": str(row.get("source_turn_id") or ""),
            "score": float(row["score"]),
            "is_gold": str(row["page_id"]) in gold_ids,
        }
        for row in ranking[:count]
    ]


def transition_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rewrites: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            gold_ids = [str(item) for item in query["eligible_gold_page_ids"]]
            gold_set = set(gold_ids)
            baseline = list(rankings[code]["baseline"][query_id])
            rewrite = list(rankings[code]["rewrite"][query_id])
            baseline_by_id = rank_map(baseline)
            rewrite_by_id = rank_map(rewrite)
            baseline_gold_ranks = {page_id: int(baseline_by_id[page_id]["rank"]) for page_id in gold_ids}
            rewrite_gold_ranks = {page_id: int(rewrite_by_id[page_id]["rank"]) for page_id in gold_ids}
            baseline_hit5 = any(rank <= OUTPUT_K for rank in baseline_gold_ranks.values())
            rewrite_hit5 = any(rank <= OUTPUT_K for rank in rewrite_gold_ranks.values())
            if not baseline_hit5 and rewrite_hit5:
                transition = "RESCUED"
            elif baseline_hit5 and not rewrite_hit5:
                transition = "HURT"
            elif baseline_hit5 and rewrite_hit5:
                transition = "UNCHANGED_HIT"
            else:
                transition = "UNCHANGED_MISS"
            rows.append(
                {
                    "session_id": code,
                    "full_session_id": query["session_id"],
                    "query_id": query_id,
                    "original_query": query["original_query"],
                    "standalone_rewrite": rewrites[query_id],
                    "actual_baseline_embedding_text": query["original_query"],
                    "actual_rewrite_embedding_text": actual_rewrite_text(query, rewrites[query_id]),
                    "gold_page_ids": gold_ids,
                    "gold_scope": "eligible Gold Pages committed and visible before this Query",
                    "all_labeled_gold_page_ids": [str(item) for item in query.get("gold_page_ids") or []],
                    "ineligible_gold_page_ids": [
                        str(item) for item in query.get("gold_page_ids") or [] if str(item) not in gold_set
                    ],
                    "baseline_top20": top_rows(baseline, gold_set, TRANSITION_TOP_K),
                    "rewrite_top20": top_rows(rewrite, gold_set, TRANSITION_TOP_K),
                    "baseline_gold_ranks": baseline_gold_ranks,
                    "rewrite_gold_ranks": rewrite_gold_ranks,
                    "baseline_gold_scores": {
                        page_id: float(baseline_by_id[page_id]["score"]) for page_id in gold_ids
                    },
                    "rewrite_gold_scores": {
                        page_id: float(rewrite_by_id[page_id]["score"]) for page_id in gold_ids
                    },
                    "baseline_hit5": baseline_hit5,
                    "rewrite_hit5": rewrite_hit5,
                    "transition": transition,
                    "best_baseline_gold_rank": min(baseline_gold_ranks.values()),
                    "best_rewrite_gold_rank": min(rewrite_gold_ranks.values()),
                    "best_gold_rank_change": min(baseline_gold_ranks.values()) - min(rewrite_gold_ranks.values()),
                    "max_abs_gold_rank_change": max(
                        abs(baseline_gold_ranks[page_id] - rewrite_gold_ranks[page_id]) for page_id in gold_ids
                    ),
                    "has_obvious_reference": bool(REFERENCE_PATTERN.search(str(query["original_query"]))),
                }
            )
    return rows


def build_session_summary(
    snapshots: Mapping[str, Mapping[str, Any]],
    metrics_by_session: Mapping[str, Mapping[str, Mapping[str, Any]]],
    transitions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        baseline = metrics_by_session[code]["baseline"]
        rewrite = metrics_by_session[code]["rewrite"]
        session_transitions = [row for row in transitions if row["session_id"] == code]
        promoted_gold = sum(
            before > OUTPUT_K and after <= OUTPUT_K
            for row in session_transitions
            for page_id, before in row["baseline_gold_ranks"].items()
            for after in [row["rewrite_gold_ranks"][page_id]]
        )
        demoted_gold = sum(
            before <= OUTPUT_K and after > OUTPUT_K
            for row in session_transitions
            for page_id, before in row["baseline_gold_ranks"].items()
            for after in [row["rewrite_gold_ranks"][page_id]]
        )
        counts = Counter(str(row["transition"]) for row in session_transitions)
        eligible = int(baseline["eligible_gold_count"])
        baseline_hits = round(float(baseline["recall_at_5"]) * eligible)
        rewrite_hits = round(float(rewrite["recall_at_5"]) * eligible)
        rows.append(
            {
                "session_id": code,
                "eligible_gold_count": eligible,
                "baseline_gold_hits": baseline_hits,
                "rewrite_gold_hits": rewrite_hits,
                "baseline_r5": baseline["recall_at_5"],
                "rewrite_r5": rewrite["recall_at_5"],
                "delta_r5": float(rewrite["recall_at_5"]) - float(baseline["recall_at_5"]),
                "baseline_r10": baseline["recall_at_10"],
                "rewrite_r10": rewrite["recall_at_10"],
                "baseline_r20": baseline["recall_at_20"],
                "rewrite_r20": rewrite["recall_at_20"],
                "baseline_mrr": baseline["mrr"],
                "rewrite_mrr": rewrite["mrr"],
                "baseline_mean_gold_rank": baseline["mean_gold_rank"],
                "rewrite_mean_gold_rank": rewrite["mean_gold_rank"],
                "promoted_gold": promoted_gold,
                "demoted_gold": demoted_gold,
                "net_gold_gain": promoted_gold - demoted_gold,
                "rescued_queries": counts["RESCUED"],
                "hurt_queries": counts["HURT"],
                "unchanged_hit_queries": counts["UNCHANGED_HIT"],
                "unchanged_miss_queries": counts["UNCHANGED_MISS"],
            }
        )
    pooled_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    pooled_baseline = {
        query_id: ranking
        for code in SESSION_CODES
        for query_id, ranking in metrics_by_session[code]["baseline_rankings"].items()
    }
    pooled_rewrite = {
        query_id: ranking
        for code in SESSION_CODES
        for query_id, ranking in metrics_by_session[code]["rewrite_rankings"].items()
    }
    baseline_micro, _ = evaluate_rankings(pooled_queries, pooled_baseline)
    rewrite_micro, _ = evaluate_rankings(pooled_queries, pooled_rewrite)
    macro_baseline_r5 = statistics.fmean(float(row["baseline_r5"]) for row in rows)
    macro_rewrite_r5 = statistics.fmean(float(row["rewrite_r5"]) for row in rows)
    counts = Counter(str(row["transition"]) for row in transitions)
    aggregate = {
        "session_id": "Aggregate",
        "eligible_gold_count": baseline_micro["eligible_gold_count"],
        "baseline_gold_hits": sum(int(row["baseline_gold_hits"]) for row in rows),
        "rewrite_gold_hits": sum(int(row["rewrite_gold_hits"]) for row in rows),
        "baseline_r5": baseline_micro["recall_at_5"],
        "rewrite_r5": rewrite_micro["recall_at_5"],
        "delta_r5": float(rewrite_micro["recall_at_5"]) - float(baseline_micro["recall_at_5"]),
        "baseline_macro_r5": macro_baseline_r5,
        "rewrite_macro_r5": macro_rewrite_r5,
        "macro_delta_r5": macro_rewrite_r5 - macro_baseline_r5,
        "baseline_r10": baseline_micro["recall_at_10"],
        "rewrite_r10": rewrite_micro["recall_at_10"],
        "baseline_r20": baseline_micro["recall_at_20"],
        "rewrite_r20": rewrite_micro["recall_at_20"],
        "baseline_mrr": baseline_micro["mrr"],
        "rewrite_mrr": rewrite_micro["mrr"],
        "baseline_mean_gold_rank": baseline_micro["mean_gold_rank"],
        "rewrite_mean_gold_rank": rewrite_micro["mean_gold_rank"],
        "promoted_gold": sum(int(row["promoted_gold"]) for row in rows),
        "demoted_gold": sum(int(row["demoted_gold"]) for row in rows),
        "net_gold_gain": sum(int(row["net_gold_gain"]) for row in rows),
        "rescued_queries": counts["RESCUED"],
        "hurt_queries": counts["HURT"],
        "unchanged_hit_queries": counts["UNCHANGED_HIT"],
        "unchanged_miss_queries": counts["UNCHANGED_MISS"],
    }
    return [*rows, aggregate]


def evidence_snippet(text: str, term: str, radius: int = 80) -> str | None:
    index = text.find(term)
    if index < 0:
        return None
    return text[max(0, index - radius) : min(len(text), index + len(term) + radius)]


def unique_terms(text: str, terms: Sequence[str]) -> list[str]:
    # Longer terms win so "归母净利润" is not duplicated as "净利润"-style subterms.
    selected: list[str] = []
    occupied: list[tuple[int, int]] = []
    for term in sorted(terms, key=len, reverse=True):
        for match in re.finditer(re.escape(term), text):
            span = match.span()
            if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
                continue
            selected.append(term)
            occupied.append(span)
            break
    return selected


def audit_category(values: Sequence[str], visible_text: str) -> dict[str, Any]:
    evidence = {value: evidence_snippet(visible_text, value) for value in values}
    unsupported = [value for value, snippet in evidence.items() if snippet is None]
    return {
        "values": list(values),
        "status": "NOT_SUPPORTED_BY_VISIBLE_CONTEXT" if unsupported else "SUPPORTED_BY_VISIBLE_CONTEXT",
        "unsupported_values": unsupported,
        "evidence_snippets": evidence,
    }


def audit_comparison_segments(values: Sequence[str], visible_text: str) -> dict[str, Any]:
    evidence_terms: list[str] = []
    for value in values:
        evidence_terms.extend(unique_terms(value, SUBJECT_TERMS))
        evidence_terms.extend(sorted(set(re.findall(r"20\d{2}", value))))
        evidence_terms.extend(unique_terms(value, METRIC_TERMS))
        evidence_terms.extend(unique_terms(value, CONCLUSION_TERMS))
    evidence_terms = list(dict.fromkeys(evidence_terms))
    evidence = {term: evidence_snippet(visible_text, term) for term in evidence_terms}
    unsupported = [term for term, snippet in evidence.items() if snippet is None]
    return {
        "values": list(values),
        "status": "NOT_SUPPORTED_BY_VISIBLE_CONTEXT" if unsupported else "SUPPORTED_BY_VISIBLE_CONTEXT",
        "unsupported_values": unsupported,
        "evidence_snippets": evidence,
        "support_rule": "Every subject/year/indicator/historical-conclusion component in the comparison is visible.",
    }


def audit_rewrite(query: Mapping[str, Any], rewrite: str) -> dict[str, Any]:
    history = query.get("previous_3_qa") or []
    visible_text = "\n\n".join(
        [str(query["original_query"])]
        + [f"[{item['turn_id']}] 用户：{item['user']}\n助手：{item['assistant']}" for item in history]
    )
    subjects = unique_terms(rewrite, SUBJECT_TERMS)
    years = sorted(set(re.findall(r"20\d{2}", rewrite)))
    time_terms = [*years]
    for term in ("三年", "完整财年", "最新一年", "最新完整财年"):
        if term in rewrite:
            time_terms.append(term)
    indicators = unique_terms(rewrite, METRIC_TERMS)
    comparison_segments = [
        segment.strip()
        for segment in re.split(r"[；;。？?]", rewrite)
        if segment.strip() and re.search(r"与|和|以及|相比|差异|匹配|同步|同向|方向不一致", segment)
    ]
    conclusions = unique_terms(rewrite, CONCLUSION_TERMS)
    return {
        "query_id": query["query_id"],
        "visible_context_turn_ids": [str(item["turn_id"]) for item in history],
        "audit_scope": "current query + previous_3_qa only; no required_context/Gold/future dialogue",
        "subject": audit_category(subjects, visible_text),
        "time": audit_category(time_terms, visible_text),
        "indicator": audit_category(indicators, visible_text),
        "comparison_object": audit_comparison_segments(comparison_segments, visible_text),
        "historical_conclusion": audit_category(conclusions, visible_text),
    }


def diverse_select(candidates: Sequence[Mapping[str, Any]], count: int, *, category: str) -> list[Mapping[str, Any]]:
    if category == "RESCUED":
        ordered = sorted(
            candidates,
            key=lambda row: (
                -int(row["best_gold_rank_change"]),
                -int(row["max_abs_gold_rank_change"]),
                -int(bool(row["has_obvious_reference"])),
                str(row["query_id"]),
            ),
        )
    elif category == "HURT":
        ordered = sorted(
            candidates,
            key=lambda row: (
                int(row["best_gold_rank_change"]),
                -int(row["max_abs_gold_rank_change"]),
                -int(bool(row["has_obvious_reference"])),
                str(row["query_id"]),
            ),
        )
    else:
        ordered = sorted(
            candidates,
            key=lambda row: (
                -int(row["max_abs_gold_rank_change"]),
                -int(bool(row["has_obvious_reference"])),
                str(row["query_id"]),
            ),
        )
    if len(ordered) <= count:
        return ordered
    selected: list[Mapping[str, Any]] = []
    seen_sessions: set[str] = set()
    for row in ordered:
        if str(row["session_id"]) not in seen_sessions:
            selected.append(row)
            seen_sessions.add(str(row["session_id"]))
            if len(selected) == count:
                return selected
    for row in ordered:
        if row not in selected:
            selected.append(row)
            if len(selected) == count:
                break
    return selected


def representative_cases(
    snapshots: Mapping[str, Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected_rows = [
        *diverse_select([row for row in transitions if row["transition"] == "RESCUED"], 3, category="RESCUED"),
        *diverse_select(
            [row for row in transitions if row["transition"] == "UNCHANGED_MISS"],
            3,
            category="UNCHANGED_MISS",
        ),
        *diverse_select([row for row in transitions if row["transition"] == "HURT"], 2, category="HURT"),
    ]
    cases: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    query_lookup = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    for transition in selected_rows:
        code = str(transition["session_id"])
        query_id = str(transition["query_id"])
        query = query_lookup[query_id]
        pages_by_id = {str(page["page_id"]): page for page in snapshots[code]["pages"]}
        baseline = list(rankings[code]["baseline"][query_id])
        rewrite = list(rankings[code]["rewrite"][query_id])
        baseline_by_id = rank_map(baseline)
        rewrite_by_id = rank_map(rewrite)
        gold_ids = [str(item) for item in query["eligible_gold_page_ids"]]
        gold_set = set(gold_ids)
        detail_ids: list[str] = []
        for page_id in [
            *(str(item["page_id"]) for item in baseline[:OUTPUT_K]),
            *(str(item["page_id"]) for item in rewrite[:OUTPUT_K]),
            *gold_ids,
        ]:
            if page_id not in detail_ids:
                detail_ids.append(page_id)
        audit = audit_rewrite(query, str(transition["standalone_rewrite"]))
        audit["transition"] = transition["transition"]
        audits.append(audit)
        cases.append(
            {
                "selection_category": transition["transition"],
                "session_id": code,
                "full_session_id": query["session_id"],
                "query_id": query_id,
                "original_query": query["original_query"],
                "standalone_rewrite": transition["standalone_rewrite"],
                "actual_baseline_embedding_text": transition["actual_baseline_embedding_text"],
                "actual_rewrite_embedding_text": transition["actual_rewrite_embedding_text"],
                "visible_short_term_context": query.get("previous_3_qa") or [],
                "gold_page_ids": gold_ids,
                "baseline_top10": top_rows(baseline, gold_set, DISPLAY_K),
                "rewrite_top10": top_rows(rewrite, gold_set, DISPLAY_K),
                "baseline_all_gold": [
                    {
                        "page_id": page_id,
                        "rank": int(baseline_by_id[page_id]["rank"]),
                        "score": float(baseline_by_id[page_id]["score"]),
                    }
                    for page_id in gold_ids
                ],
                "rewrite_all_gold": [
                    {
                        "page_id": page_id,
                        "rank": int(rewrite_by_id[page_id]["rank"]),
                        "score": float(rewrite_by_id[page_id]["score"]),
                    }
                    for page_id in gold_ids
                ],
                "entered_top5": [
                    str(item["page_id"])
                    for item in rewrite[:OUTPUT_K]
                    if str(item["page_id"]) not in {str(row["page_id"]) for row in baseline[:OUTPUT_K]}
                ],
                "dropped_from_top5": [
                    str(item["page_id"])
                    for item in baseline[:OUTPUT_K]
                    if str(item["page_id"]) not in {str(row["page_id"]) for row in rewrite[:OUTPUT_K]}
                ],
                "page_details": [
                    {
                        "page_id": page_id,
                        "source_turn_id": pages_by_id[page_id]["source_turn_id"],
                        "is_gold": page_id in gold_set,
                        "baseline_rank": int(baseline_by_id[page_id]["rank"]),
                        "baseline_score": float(baseline_by_id[page_id]["score"]),
                        "rewrite_rank": int(rewrite_by_id[page_id]["rank"]),
                        "rewrite_score": float(rewrite_by_id[page_id]["score"]),
                        "actual_page_embedding_text": pages_by_id[page_id]["current_embedding_text"],
                    }
                    for page_id in detail_ids
                ],
                "fact_comparison": [
                    {
                        "gold_page_id": page_id,
                        "baseline_rank": int(baseline_by_id[page_id]["rank"]),
                        "baseline_score": float(baseline_by_id[page_id]["score"]),
                        "rewrite_rank": int(rewrite_by_id[page_id]["rank"]),
                        "rewrite_score": float(rewrite_by_id[page_id]["score"]),
                        "rank_change": f"#{baseline_by_id[page_id]['rank']} → #{rewrite_by_id[page_id]['rank']}",
                    }
                    for page_id in gold_ids
                ],
                "rewrite_audit": audit,
            }
        )
    return cases, audits


def fenced(text: Any) -> list[str]:
    return ["```text", str(text), "```", ""]


def ranking_lines(title: str, rows: Sequence[Mapping[str, Any]], gold_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [f"### {title}", "", "```text"]
    for row in rows:
        marker = "✅ GOLD" if row["is_gold"] else "❌ NON-GOLD"
        lines.append(
            f"#{row['rank']} {row['page_id']} score={float(row['score']):.9f} "
            f"source_turn={row['source_turn_id']} {marker}"
        )
    lines.append("```")
    lines.append("")
    lines.append("All Gold ranks:")
    lines.append("")
    lines.append("```text")
    for row in gold_rows:
        lines.append(f"Gold {row['page_id']} -> #{row['rank']} score={float(row['score']):.9f}")
    lines.extend(("```", ""))
    return lines


def write_representative_markdown(
    path: Path,
    cases: Sequence[Mapping[str, Any]],
    session_summary: Sequence[Mapping[str, Any]],
    run_metadata: Mapping[str, Any],
) -> None:
    lines = [
        "# Query Rewrite Cross-Session Diagnosis",
        "",
        "## Frozen experiment contract",
        "",
        "- Baseline query: exact original Query.",
        "- Rewrite query: exact old Q5 format `Original query: ...\\nStandalone query: ...`.",
        "- Page representation: stored production P0 (`summary`, `Keywords: ...`, `User: ...`).",
        f"- Embedding: `{PRODUCTION_EMBEDDING}`; retrieval: per-Session dense cosine only.",
        "- Candidate visibility: each Query's immutable query-time `visible_page_ids`; no cross-Session candidates.",
        "- Rewrite context: current Query + preceding `previous_3_qa`; no required_context, Gold, or future dialogue.",
        "",
        "## Session summary",
        "",
        "| Session | Eligible Gold | Baseline R@5 | Rewrite R@5 | Delta | Promoted | Demoted | Net |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in session_summary:
        lines.append(
            f"| {row['session_id']} | {row['eligible_gold_count']} | {float(row['baseline_r5']):.6f} | "
            f"{float(row['rewrite_r5']):.6f} | {float(row['delta_r5']):+.6f} | "
            f"{row['promoted_gold']} | {row['demoted_gold']} | {int(row['net_gold_gain']):+d} |"
        )
    aggregate = next(row for row in session_summary if row["session_id"] == "Aggregate")
    lines.extend(
        (
            "",
            f"Aggregate Micro R@5: `{float(aggregate['baseline_r5']):.9f} → "
            f"{float(aggregate['rewrite_r5']):.9f}` "
            f"(delta `{float(aggregate['delta_r5']):+.9f}`).",
            "",
            f"Aggregate Macro Session R@5: `{float(aggregate['baseline_macro_r5']):.9f} → "
            f"{float(aggregate['rewrite_macro_r5']):.9f}` "
            f"(delta `{float(aggregate['macro_delta_r5']):+.9f}`).",
            "",
        )
    )
    lines.extend(
        (
            "",
            "## Automatic case selection",
            "",
            "Within each transition type, cases are ordered by Gold rank movement, then explicit-reference flag, "
            "then query_id. A deterministic distinct-Session first pass is applied before filling remaining slots.",
            "",
        )
    )
    category_titles = {
        "RESCUED": "RESCUED — Baseline miss → Rewrite hit",
        "UNCHANGED_MISS": "UNCHANGED_MISS — both miss",
        "HURT": "HURT — Baseline hit → Rewrite miss",
    }
    current_category = None
    for case in cases:
        category = str(case["selection_category"])
        if category != current_category:
            lines.extend((f"# {category_titles[category]}", ""))
            current_category = category
        lines.extend((f"## {case['query_id']}", ""))
        lines.extend(("### 当前原始问题", "", *fenced(case["original_query"])))
        lines.extend(("### Standalone Rewrite", "", *fenced(case["standalone_rewrite"])))
        lines.extend(("### Baseline 实际 Embedding Query", "", *fenced(case["actual_baseline_embedding_text"])))
        lines.extend(("### Rewrite 方案实际 Embedding Query", "", *fenced(case["actual_rewrite_embedding_text"])))
        lines.extend(ranking_lines("Baseline Dense", case["baseline_top10"], case["baseline_all_gold"]))
        lines.extend(ranking_lines("Rewrite Dense", case["rewrite_top10"], case["rewrite_all_gold"]))
        lines.extend(("### Rewrite factual audit", "", "| Field | Status | Values | Unsupported |", "|---|---|---|---|"))
        for key in ("subject", "time", "indicator", "comparison_object", "historical_conclusion"):
            audit = case["rewrite_audit"][key]
            lines.append(
                f"| {key} | {audit['status']} | {json.dumps(audit['values'], ensure_ascii=False)} | "
                f"{json.dumps(audit['unsupported_values'], ensure_ascii=False)} |"
            )
        lines.extend(("", "### Pages actually embedded (Baseline Top5 + Rewrite Top5 + all Gold, deduplicated)", ""))
        for page in case["page_details"]:
            lines.extend(
                (
                    f"#### Page {page['page_id']}",
                    "",
                    f"- Source Turn ID: `{page['source_turn_id']}`",
                    f"- 是否 Gold: `{'YES' if page['is_gold'] else 'NO'}`",
                    f"- Baseline rank / score: `#{page['baseline_rank']} / {float(page['baseline_score']):.9f}`",
                    f"- Rewrite rank / score: `#{page['rewrite_rank']} / {float(page['rewrite_score']):.9f}`",
                    "",
                    "实际 Page Embedding Text：",
                    "",
                    *fenced(page["actual_page_embedding_text"]),
                )
            )
        lines.extend(("### Fact comparison", ""))
        for comparison in case["fact_comparison"]:
            lines.extend(
                (
                    "```text",
                    f"Gold Page: {comparison['gold_page_id']}",
                    "",
                    "Baseline:",
                    f"rank #{comparison['baseline_rank']}",
                    f"score {float(comparison['baseline_score']):.9f}",
                    "",
                    "Rewrite:",
                    f"rank #{comparison['rewrite_rank']}",
                    f"score {float(comparison['rewrite_score']):.9f}",
                    "",
                    "Rank change:",
                    comparison["rank_change"],
                    "```",
                    "",
                )
            )
        lines.extend(("Entered Top5:", "", *fenced("\n".join(case["entered_top5"]) or "NONE")))
        lines.extend(("Dropped from Top5:", "", *fenced("\n".join(case["dropped_from_top5"]) or "NONE")))
    lines.extend(("## Run metadata", "", "```json", json.dumps(run_metadata, ensure_ascii=False, indent=2), "```", ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


async def async_main(args: argparse.Namespace) -> None:
    args.output_dir = args.output_dir.resolve()
    snapshots = load_snapshots()
    rewrites, rewrite_metadata = await load_or_generate_rewrites(args, snapshots)
    vectors, embedding_metadata = load_query_vectors(args.output_dir, snapshots, rewrites)
    rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    metrics_by_session: dict[str, dict[str, Any]] = {}
    for code in SESSION_CODES:
        baseline_rankings, rewrite_rankings = rank_snapshot(snapshots[code], vectors)
        baseline_metrics, _ = evaluate_rankings(snapshots[code]["queries"], baseline_rankings)
        rewrite_metrics, _ = evaluate_rankings(snapshots[code]["queries"], rewrite_rankings)
        rankings[code] = {"baseline": baseline_rankings, "rewrite": rewrite_rankings}
        metrics_by_session[code] = {
            "baseline": baseline_metrics,
            "rewrite": rewrite_metrics,
            "baseline_rankings": baseline_rankings,
            "rewrite_rankings": rewrite_rankings,
        }
    reuse_validation = validate_reused_baselines(metrics_by_session)
    transitions = transition_rows(snapshots, rewrites, rankings)
    summary = build_session_summary(snapshots, metrics_by_session, transitions)
    cases, audits = representative_cases(snapshots, transitions, rankings)

    output_transition_rows = [
        {key: value for key, value in row.items() if key not in {"max_abs_gold_rank_change", "has_obvious_reference"}}
        for row in transitions
    ]
    write_csv(args.output_dir / "session_summary.csv", summary)
    write_csv(args.output_dir / "query_transition.csv", output_transition_rows)
    dump_json(
        args.output_dir / "representative_cases.json",
        {
            "selection_rule": (
                "Rank movement descending (HURT: damage descending), explicit-reference flag, query_id; "
                "distinct-Session first pass before fill."
            ),
            "requested_counts": {"RESCUED": 3, "UNCHANGED_MISS": 3, "HURT": 2},
            "available_counts": dict(Counter(str(row["transition"]) for row in transitions)),
            "selected_counts": dict(Counter(str(row["selection_category"]) for row in cases)),
            "cases": cases,
        },
    )
    write_jsonl(args.output_dir / "rewrite_audit.jsonl", audits)
    run_metadata = {
        "sessions": list(SESSION_CODES),
        "full_session_rerun": False,
        "page_representation": "production P0: summary + Keywords + User",
        "embedding_model": PRODUCTION_EMBEDDING,
        "retrieval": "per-Session dense cosine",
        "rewrite_embedding_format": "Original query: <original>\\nStandalone query: <rewrite>",
        "rewrite": rewrite_metadata,
        "embedding_cache": embedding_metadata,
        "reuse_validation": reuse_validation,
    }
    dump_json(args.output_dir / "run_metadata.json", run_metadata)
    write_representative_markdown(args.output_dir / "representative_cases.md", cases, summary, run_metadata)


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
