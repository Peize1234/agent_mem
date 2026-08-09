"""Evaluate conservative reference resolution against Q0 and old Q5.

The experiment is offline and time-safe: it reuses the immutable S001-S005
query-time snapshots, stored production P0 Page embeddings, and cached Q0/Q5
query embeddings. It never replays a benchmark Session or mutates production
retrieval code/data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from openai import AsyncOpenAI
from transformers import AutoTokenizer

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    cosine_rank,
    evaluate_rankings,
    load_jsonl,
    stable_hash,
    write_jsonl,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    load_existing_embedding_vectors,
)
from exp.benchmark.run_midterm_retrieval_experiments import (  # noqa: E402
    AsyncJsonlLLMCache,
    EmbeddingCache,
)
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


PREVIOUS_RESULT_DIR = REPO_ROOT / "exp/results/query_rewrite_cross_session_diagnosis"
OUTPUT_DIR = REPO_ROOT / "exp/results/reference_resolution_query_diagnosis"
OUTPUT_K = 5
DISPLAY_K = 10
REFERENCE_PROMPT_VERSION = "reference-resolution-v1"
REFERENCE_VARIANT = "QREF"
MANDATORY_CASE_IDS = ("S003-Q044", "S004-Q052", "S004-Q063", "S005-Q042")
CLASSIFICATIONS = (
    "REFERENCE_REQUIRED",
    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED",
    "UNSUPPORTED",
)

REFERENCE_SYSTEM_PROMPT = """You are a conservative reference resolution component for a memory retrieval system.

Your task is NOT to rewrite, summarize, expand, improve, or answer the user's question.
Your only task is to resolve references or necessary omissions in the CURRENT query by using the VISIBLE previous conversation.

You may edit only spans whose meaning cannot be understood outside the conversation, including pronouns, omitted subjects, and explicit references such as:
- this / that / it / they
- previous / above / just now
- the previous conclusion or judgment
- these two indicators
- this evidence or evidence chain
- this change / the previous cash performance

Absolute rules:
1. Copy and preserve the original query wording, clauses, scope, intent, requested action, and constraints as much as possible.
2. Insert or replace only an entity or concept that is explicitly and uniquely referred to by the current query.
3. Do not add a related indicator, fact, year, conclusion, comparison, task, action, or analysis dimension merely because it appears in context.
4. Do not summarize the conversation, broaden the topic, infer hidden intent, improve the task, or make the query more comprehensive.
5. Do not replace one local question with a complete standalone research question.
6. Preserve the original level of detail and granularity. Necessary context is allowed; potentially useful context is forbidden.
7. If a reference has two or more reasonable antecedents, leave that reference unchanged. Resolve only the unambiguous parts.
8. Prefer under-resolution over over-resolution.
9. Do not remove, paraphrase away, strengthen, or add user constraints.
10. Do not use information outside the supplied visible conversation and current query.
11. Do not answer or explain the query.

Example: if "two indicators" uniquely means operating cash flow and adjusted net profit, name only those two. Never add revenue, total assets, equity, or other context indicators.
Counterexample: never turn "Where does the previous counterexample conflict with the main conclusion?" into a comprehensive analysis of all company financial indicators.

Return strict JSON only: {"resolved_query":"the minimally edited current query"}.
"""


SUBJECT_PATTERNS = {
    "COMPANY_GUIZHOU_MOUTAI": ("贵州茅台",),
    "COMPANY_BYD": ("比亚迪",),
    "COMPANY_CATL": ("宁德时代",),
}

INDICATOR_PATTERNS = {
    "OPERATING_CASH_FLOW_NET": ("经营活动现金流净额", "经营现金流净额", "经营活动现金流", "经营现金流"),
    "ADJUSTED_PARENT_NET_PROFIT": ("扣非归母净利润", "扣非净利润"),
    "PARENT_NET_PROFIT": ("归母净利润",),
    "PARENT_EQUITY": ("归母股东权益",),
    "TOTAL_ASSETS": ("总资产",),
    "REVENUE": ("营业收入",),
    "ASSET_EQUITY_RATIO": ("总资产/归母股东权益", "资产权益比"),
    "CASH_PROFIT_RATIO": ("现金利润比",),
    "ASSET_TURNOVER": ("资产周转",),
    "PROFIT_MARGIN": ("利润率",),
    "CASH_GENERIC": ("现金表现", "现金证据", "现金流"),
    "PROFIT_GENERIC": ("盈利", "利润"),
    "ASSETS_GENERIC": ("资产",),
    "EQUITY_GENERIC": ("股东权益", "权益"),
    "INDICATORS_GENERIC": ("两个指标", "两项指标", "两者", "二者", "这个指标", "该指标", "这项指标"),
}

HISTORICAL_PATTERNS = {
    "CONTINUOUS_RISE": ("连续上升",),
    "CONTINUOUS_DECLINE": ("连续下降",),
    "RISE_THEN_FALL": ("先升后降",),
    "FALL_THEN_RISE": ("先降后升",),
    "GROWTH_SLOWED": ("增速放缓",),
    "TURNING_POINT": ("出现拐点", "中途反转", "拐点"),
    "MISMATCH": ("存在错位", "错位"),
    "EFFICIENCY_ISSUE": ("效率问题",),
    "MAIN_CONCLUSION": ("主结论",),
    "COUNTEREXAMPLE": ("反例",),
    "CONFLICT": ("冲突",),
    "NOT_SYNCHRONIZED": ("不同步",),
}

TASK_PATTERNS = {
    "EXPLAIN": ("解释", "说明", "回答"),
    "COMPARE": ("比较", "对比", "同步", "匹配", "同向", "方向相反"),
    "ASSESS": ("判断", "评估"),
    "REVISE": ("修订", "修正", "调整", "降级"),
    "AUDIT": ("检查", "核对", "确认", "追溯"),
    "SUMMARIZE": ("汇总", "总结", "小结", "综合"),
    "MANAGEMENT_QUESTION": ("追问", "问询"),
    "SEPARATE": ("区分", "分开"),
    "IDENTIFY": ("找出", "指出"),
}

CONSTRAINT_PATTERNS = {
    "NO_ASSUMPTIONS": ("不要另作假设", "不另作假设", "不作假设", "不要补估计值"),
    "FULL_FISCAL_YEARS": ("完整财年",),
    "NO_QUARTER_DATA": ("不加入季度信息", "不要加入季度信息", "不混入季度"),
    "RAW_VS_CALCULATED": ("哪些是年报原数、哪些是计算结果", "年报原数", "计算结果"),
    "KEEP_2024": ("保留2024年", "2024年这个中间点", "2024年作为中间"),
    "PRESERVE_CONFLICT": ("保留这种冲突", "保留冲突"),
    "SEPARATE_YOY_PERIODS": ("两段同比分开", "不要合成一个趋势词"),
    "SOURCE_REPRODUCIBLE": ("回到公开来源复算", "来源清楚", "可复现"),
    "SCOPE_IN_CONCLUSION": ("把口径限制放在结论里", "明确口径限制", "口径限制"),
    "NO_FORECAST": ("不加入预测", "不包含预测"),
}

TIME_PATTERNS = {
    "THREE_YEAR_PERIOD": ("三年", "三个完整财年"),
    "LATEST_YEAR": ("最新一年", "最新完整财年"),
    "TWO_YOY_PERIODS": ("两段同比",),
    "INTERMEDIATE_POINT": ("中间点",),
    "ANNUAL_PATH": ("年度路径", "年度变化", "各年度"),
    "QUARTER_PERIOD": ("季度",),
}

SPECIFIC_INDICATORS = {
    "OPERATING_CASH_FLOW_NET",
    "ADJUSTED_PARENT_NET_PROFIT",
    "PARENT_NET_PROFIT",
    "PARENT_EQUITY",
    "TOTAL_ASSETS",
    "REVENUE",
    "ASSET_EQUITY_RATIO",
    "CASH_PROFIT_RATIO",
    "ASSET_TURNOVER",
    "PROFIT_MARGIN",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Conservative Query reference-resolution diagnosis")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=1)
    return parser.parse_args()


def resolved_validator(value: Mapping[str, Any]) -> dict[str, Any]:
    text = str(value.get("resolved_query") or "").strip()
    if not text:
        raise ValueError("resolved_query is empty")
    return {"resolved_query": text}


def reference_messages(query: Mapping[str, Any]) -> tuple[list[dict[str, str]], str]:
    history = query.get("previous_3_qa") or []
    context = "\n\n".join(
        f"[{item['turn_id']}] User: {item['user']}\nAssistant: {item['assistant']}" for item in history
    )
    user = f"Visible previous conversation (chronological, at most 3 QAs):\n{context}\n\nCURRENT query:\n{query['original_query']}"
    return [
        {"role": "system", "content": REFERENCE_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ], REFERENCE_PROMPT_VERSION


def load_old_rewrites(snapshots: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    rows = [
        row
        for row in load_jsonl(PREVIOUS_RESULT_DIR / "cache/query_rewrites.jsonl")
        if row.get("variant") == "Q4" and row.get("status") == "SUCCESS"
    ]
    by_query = {str(row["query_id"]): str(row["parsed"]["standalone_query"]) for row in rows}
    expected = {str(query["query_id"]) for code in SESSION_CODES for query in snapshots[code]["queries"]}
    if set(by_query) != expected:
        raise ValueError(f"Old rewrite cache coverage mismatch: {len(by_query)} != {len(expected)}")
    return by_query


async def load_or_generate_resolved_queries(
    args: argparse.Namespace,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    cache_path = args.output_dir / "cache/reference_resolution_llm.jsonl"
    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for reference resolution")
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
    initial_keys = set(cache.success_by_key)

    async def one(query: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        messages, version = reference_messages(query)
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant=REFERENCE_VARIANT,
            messages=messages,
            prompt_version=version,
            max_tokens=800,
            validator=resolved_validator,
        )
        return str(query["query_id"]), row

    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    try:
        results = await asyncio.gather(*(one(query) for query in all_queries))
    finally:
        await client.close()
    failures = {query_id: row.get("errors") for query_id, row in results if row.get("status") != "SUCCESS"}
    if failures:
        dump_json(args.output_dir / "reference_resolution_failures.json", failures)
        raise RuntimeError(f"Reference resolution failed for {len(failures)} Queries")
    resolved = {query_id: str(row["parsed"]["resolved_query"]) for query_id, row in results}
    rows_by_query = {query_id: row for query_id, row in results}
    durable_rows = {
        str(row["query_id"]): row
        for row in load_jsonl(cache_path)
        if row.get("variant") == REFERENCE_VARIANT
        and row.get("status") == "SUCCESS"
        and row.get("prompt_version") == REFERENCE_PROMPT_VERSION
    }
    metadata = {
        "model": model,
        "thinking_mode": "disabled",
        "prompt_version": REFERENCE_PROMPT_VERSION,
        "prompt_sha256": stable_hash(REFERENCE_SYSTEM_PROMPT),
        "frozen_system_prompt": REFERENCE_SYSTEM_PROMPT,
        "query_count": len(all_queries),
        "current_run_cache_hits": sum(str(row["cache_key"]) in initial_keys for _, row in results),
        "current_run_cache_misses": sum(str(row["cache_key"]) not in initial_keys for _, row in results),
        "new_llm_query_count": len(durable_rows),
        "new_llm_api_attempt_count": sum(1 + int(row.get("retry_count") or 0) for row in durable_rows.values()),
        "cache_path": str(cache_path),
    }
    return resolved, rows_by_query, metadata


def old_embedding_text(original: str, rewrite: str) -> str:
    return f"Original query: {original}\nStandalone query: {rewrite}"


def ref_embedding_text(original: str, resolved: str) -> str:
    return f"Original query: {original}\nResolved query: {resolved}"


def load_query_vectors(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, str],
) -> tuple[dict[str, list[float]], EmbeddingCache, dict[str, Any]]:
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
    q0_ids = [f"Q0:{query['query_id']}:0" for query in heldout_queries]
    vectors.update(
        load_existing_embedding_vectors(
            MULTI_SESSION_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="heldout-Q0-",
            required_ids=q0_ids,
        )
    )
    old_ids = [f"Q5:{query['query_id']}:0" for query in heldout_queries]
    vectors.update(
        load_existing_embedding_vectors(
            PREVIOUS_RESULT_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="heldout-Q5-",
            required_ids=old_ids,
        )
    )
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    ref_ids = [f"{REFERENCE_VARIANT}:{query['query_id']}:0" for query in all_queries]
    ref_texts = [
        ref_embedding_text(str(query["original_query"]), resolved[str(query["query_id"])]) for query in all_queries
    ]
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    ref_vectors, metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "reference-resolution-S001-S005-resolved-label",
        ref_ids,
        ref_texts,
        measure_individual=True,
    )
    vectors.update(ref_vectors)
    return vectors, cache, {
        "baseline_embedding_cache_reused": len(all_queries),
        "old_rewrite_embedding_cache_reused": len(all_queries),
        "new_embedding_count": len(ref_ids),
        "reference_embedding_cache_hit": bool(metadata.get("cache_hit")),
        "reference_embedding_metadata": metadata,
    }


def rank_snapshot(
    snapshot: Mapping[str, Any],
    vectors: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
    page_vectors = {page_id: page["stored_embedding"] for page_id, page in pages_by_id.items()}
    visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
    result = {"baseline": {}, "old_rewrite": {}, "reference_resolution": {}}
    for query in snapshot["queries"]:
        query_id = str(query["query_id"])
        visible_pages = [pages_by_id[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
        result["baseline"][query_id] = cosine_rank(vectors[f"Q0:{query_id}:0"], visible_pages, page_vectors)
        result["old_rewrite"][query_id] = cosine_rank(vectors[f"Q5:{query_id}:0"], visible_pages, page_vectors)
        result["reference_resolution"][query_id] = cosine_rank(
            vectors[f"{REFERENCE_VARIANT}:{query_id}:0"], visible_pages, page_vectors
        )
    return result


def validate_reused_metrics(
    metrics_by_session: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    previous = {row["session_id"]: row for row in read_csv(PREVIOUS_RESULT_DIR / "session_summary.csv")}
    checks: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for variant, prefix in (("baseline", "baseline"), ("old_rewrite", "rewrite")):
            current = metrics_by_session[code][variant]
            for suffix, current_key in (
                ("r5", "recall_at_5"),
                ("r10", "recall_at_10"),
                ("r20", "recall_at_20"),
                ("mrr", "mrr"),
                ("mean_gold_rank", "mean_gold_rank"),
            ):
                expected = float(previous[code][f"{prefix}_{suffix}"])
                actual = float(current[current_key])
                if not math.isclose(actual, expected, abs_tol=1e-12):
                    raise AssertionError(f"{code}/{variant}/{current_key}: {actual} != previous {expected}")
        checks.append({"session_id": code, "baseline": "MATCHED", "old_rewrite": "MATCHED"})
    return {"status": "PASS", "checks": checks}


def query_transition(baseline_hit: bool, contender_hit: bool) -> str:
    if not baseline_hit and contender_hit:
        return "BASE_MISS_REF_HIT"
    if baseline_hit and not contender_hit:
        return "BASE_HIT_REF_MISS"
    if baseline_hit:
        return "BASE_HIT_REF_HIT"
    return "BASE_MISS_REF_MISS"


def old_transition(baseline_hit: bool, old_hit: bool) -> str:
    if not baseline_hit and old_hit:
        return "BASE_MISS_OLD_HIT"
    if baseline_hit and not old_hit:
        return "BASE_HIT_OLD_MISS"
    if baseline_hit:
        return "BASE_HIT_OLD_HIT"
    return "BASE_MISS_OLD_MISS"


def build_gold_and_query_transitions(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            gold_ids = [str(item) for item in query["eligible_gold_page_ids"]]
            maps = {
                variant: rank_map(rankings[code][variant][query_id])
                for variant in ("baseline", "old_rewrite", "reference_resolution")
            }
            ranks = {
                variant: {page_id: int(maps[variant][page_id]["rank"]) for page_id in gold_ids}
                for variant in maps
            }
            hits = {variant: any(rank <= OUTPUT_K for rank in ranks[variant].values()) for variant in ranks}
            ref_transition = query_transition(hits["baseline"], hits["reference_resolution"])
            previous_transition = old_transition(hits["baseline"], hits["old_rewrite"])
            old_hurt_ref_recovered = hits["baseline"] and not hits["old_rewrite"] and hits["reference_resolution"]
            old_rescue_ref_lost = not hits["baseline"] and hits["old_rewrite"] and not hits["reference_resolution"]
            query_rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "baseline_hit5": hits["baseline"],
                    "old_rewrite_hit5": hits["old_rewrite"],
                    "reference_resolution_hit5": hits["reference_resolution"],
                    "reference_transition": ref_transition,
                    "old_rewrite_transition": previous_transition,
                    "old_hurt_ref_recovered": old_hurt_ref_recovered,
                    "old_rescue_ref_lost": old_rescue_ref_lost,
                    "best_baseline_gold_rank": min(ranks["baseline"].values()),
                    "best_old_rewrite_gold_rank": min(ranks["old_rewrite"].values()),
                    "best_reference_resolution_gold_rank": min(ranks["reference_resolution"].values()),
                }
            )
            for page_id in gold_ids:
                baseline_rank = ranks["baseline"][page_id]
                old_rank = ranks["old_rewrite"][page_id]
                ref_rank = ranks["reference_resolution"][page_id]
                gold_rows.append(
                    {
                        "session_id": code,
                        "query_id": query_id,
                        "gold_page_id": page_id,
                        "baseline_rank": baseline_rank,
                        "old_rewrite_rank": old_rank,
                        "reference_resolution_rank": ref_rank,
                        "baseline_score": float(maps["baseline"][page_id]["score"]),
                        "old_rewrite_score": float(maps["old_rewrite"][page_id]["score"]),
                        "reference_resolution_score": float(maps["reference_resolution"][page_id]["score"]),
                        "reference_transition": ref_transition,
                        "old_rewrite_transition": previous_transition,
                        "promoted_by_old": baseline_rank > OUTPUT_K and old_rank <= OUTPUT_K,
                        "demoted_by_old": baseline_rank <= OUTPUT_K and old_rank > OUTPUT_K,
                        "promoted_by_reference": baseline_rank > OUTPUT_K and ref_rank <= OUTPUT_K,
                        "demoted_by_reference": baseline_rank <= OUTPUT_K and ref_rank > OUTPUT_K,
                        "recovered_old_rewrite_demotion": (
                            baseline_rank <= OUTPUT_K and old_rank > OUTPUT_K and ref_rank <= OUTPUT_K
                        ),
                        "lost_old_rewrite_rescue": (
                            baseline_rank > OUTPUT_K and old_rank <= OUTPUT_K and ref_rank > OUTPUT_K
                        ),
                        "new_rescue": baseline_rank > OUTPUT_K and old_rank > OUTPUT_K and ref_rank <= OUTPUT_K,
                        "new_demotion": baseline_rank <= OUTPUT_K and old_rank <= OUTPUT_K and ref_rank > OUTPUT_K,
                        "old_rank_movement": baseline_rank - old_rank,
                        "reference_rank_movement": baseline_rank - ref_rank,
                    }
                )
    return gold_rows, query_rows


def transition_counts(rows: Sequence[Mapping[str, Any]], prefix: str) -> dict[str, int]:
    promoted = sum(bool(row[f"promoted_by_{prefix}"]) for row in rows)
    demoted = sum(bool(row[f"demoted_by_{prefix}"]) for row in rows)
    return {"promoted_gold": promoted, "demoted_gold": demoted, "net_gain": promoted - demoted}


def build_session_summary(
    snapshots: Mapping[str, Mapping[str, Any]],
    metrics_by_session: Mapping[str, Mapping[str, Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        metrics = metrics_by_session[code]
        session_gold = [row for row in gold_rows if row["session_id"] == code]
        session_queries = [row for row in query_rows if row["session_id"] == code]
        old_counts = transition_counts(session_gold, "old")
        ref_counts = transition_counts(session_gold, "reference")
        result.append(
            {
                "session_id": code,
                "eligible_gold_count": metrics["baseline"]["eligible_gold_count"],
                "baseline_r5": metrics["baseline"]["recall_at_5"],
                "old_rewrite_r5": metrics["old_rewrite"]["recall_at_5"],
                "reference_resolution_r5": metrics["reference_resolution"]["recall_at_5"],
                "ref_vs_base_r5": float(metrics["reference_resolution"]["recall_at_5"])
                - float(metrics["baseline"]["recall_at_5"]),
                "ref_vs_old_r5": float(metrics["reference_resolution"]["recall_at_5"])
                - float(metrics["old_rewrite"]["recall_at_5"]),
                "baseline_r10": metrics["baseline"]["recall_at_10"],
                "old_rewrite_r10": metrics["old_rewrite"]["recall_at_10"],
                "reference_resolution_r10": metrics["reference_resolution"]["recall_at_10"],
                "baseline_r20": metrics["baseline"]["recall_at_20"],
                "old_rewrite_r20": metrics["old_rewrite"]["recall_at_20"],
                "reference_resolution_r20": metrics["reference_resolution"]["recall_at_20"],
                "baseline_mrr": metrics["baseline"]["mrr"],
                "old_rewrite_mrr": metrics["old_rewrite"]["mrr"],
                "reference_resolution_mrr": metrics["reference_resolution"]["mrr"],
                "baseline_mean_gold_rank": metrics["baseline"]["mean_gold_rank"],
                "old_rewrite_mean_gold_rank": metrics["old_rewrite"]["mean_gold_rank"],
                "reference_resolution_mean_gold_rank": metrics["reference_resolution"]["mean_gold_rank"],
                "old_promoted_gold": old_counts["promoted_gold"],
                "old_demoted_gold": old_counts["demoted_gold"],
                "old_net_gain": old_counts["net_gain"],
                "reference_promoted_gold": ref_counts["promoted_gold"],
                "reference_demoted_gold": ref_counts["demoted_gold"],
                "reference_net_gain": ref_counts["net_gain"],
                "recovered_old_rewrite_demotions": sum(
                    bool(row["recovered_old_rewrite_demotion"]) for row in session_gold
                ),
                "lost_old_rewrite_rescues": sum(bool(row["lost_old_rewrite_rescue"]) for row in session_gold),
                "new_rescues": sum(bool(row["new_rescue"]) for row in session_gold),
                "new_demotions": sum(bool(row["new_demotion"]) for row in session_gold),
                "reference_rescued_queries": sum(
                    row["reference_transition"] == "BASE_MISS_REF_HIT" for row in session_queries
                ),
                "reference_hurt_queries": sum(
                    row["reference_transition"] == "BASE_HIT_REF_MISS" for row in session_queries
                ),
                "old_hurt_queries_recovered": sum(bool(row["old_hurt_ref_recovered"]) for row in session_queries),
                "old_rescue_queries_lost": sum(bool(row["old_rescue_ref_lost"]) for row in session_queries),
            }
        )

    pooled_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    pooled_metrics: dict[str, dict[str, Any]] = {}
    for variant in ("baseline", "old_rewrite", "reference_resolution"):
        pooled_rankings = {
            query_id: ranking
            for code in SESSION_CODES
            for query_id, ranking in metrics_by_session[code][f"{variant}_rankings"].items()
        }
        pooled_metrics[variant], _ = evaluate_rankings(pooled_queries, pooled_rankings)
    old_counts = transition_counts(gold_rows, "old")
    ref_counts = transition_counts(gold_rows, "reference")
    aggregate = {
        "session_id": "Aggregate",
        "eligible_gold_count": pooled_metrics["baseline"]["eligible_gold_count"],
        "baseline_r5": pooled_metrics["baseline"]["recall_at_5"],
        "old_rewrite_r5": pooled_metrics["old_rewrite"]["recall_at_5"],
        "reference_resolution_r5": pooled_metrics["reference_resolution"]["recall_at_5"],
        "ref_vs_base_r5": float(pooled_metrics["reference_resolution"]["recall_at_5"])
        - float(pooled_metrics["baseline"]["recall_at_5"]),
        "ref_vs_old_r5": float(pooled_metrics["reference_resolution"]["recall_at_5"])
        - float(pooled_metrics["old_rewrite"]["recall_at_5"]),
        "baseline_macro_session_r5": statistics.fmean(float(row["baseline_r5"]) for row in result),
        "old_rewrite_macro_session_r5": statistics.fmean(float(row["old_rewrite_r5"]) for row in result),
        "reference_resolution_macro_session_r5": statistics.fmean(
            float(row["reference_resolution_r5"]) for row in result
        ),
        "baseline_r10": pooled_metrics["baseline"]["recall_at_10"],
        "old_rewrite_r10": pooled_metrics["old_rewrite"]["recall_at_10"],
        "reference_resolution_r10": pooled_metrics["reference_resolution"]["recall_at_10"],
        "baseline_r20": pooled_metrics["baseline"]["recall_at_20"],
        "old_rewrite_r20": pooled_metrics["old_rewrite"]["recall_at_20"],
        "reference_resolution_r20": pooled_metrics["reference_resolution"]["recall_at_20"],
        "baseline_mrr": pooled_metrics["baseline"]["mrr"],
        "old_rewrite_mrr": pooled_metrics["old_rewrite"]["mrr"],
        "reference_resolution_mrr": pooled_metrics["reference_resolution"]["mrr"],
        "baseline_mean_gold_rank": pooled_metrics["baseline"]["mean_gold_rank"],
        "old_rewrite_mean_gold_rank": pooled_metrics["old_rewrite"]["mean_gold_rank"],
        "reference_resolution_mean_gold_rank": pooled_metrics["reference_resolution"]["mean_gold_rank"],
        "old_promoted_gold": old_counts["promoted_gold"],
        "old_demoted_gold": old_counts["demoted_gold"],
        "old_net_gain": old_counts["net_gain"],
        "reference_promoted_gold": ref_counts["promoted_gold"],
        "reference_demoted_gold": ref_counts["demoted_gold"],
        "reference_net_gain": ref_counts["net_gain"],
        "recovered_old_rewrite_demotions": sum(bool(row["recovered_old_rewrite_demotion"]) for row in gold_rows),
        "lost_old_rewrite_rescues": sum(bool(row["lost_old_rewrite_rescue"]) for row in gold_rows),
        "new_rescues": sum(bool(row["new_rescue"]) for row in gold_rows),
        "new_demotions": sum(bool(row["new_demotion"]) for row in gold_rows),
        "reference_rescued_queries": sum(row["reference_transition"] == "BASE_MISS_REF_HIT" for row in query_rows),
        "reference_hurt_queries": sum(row["reference_transition"] == "BASE_HIT_REF_MISS" for row in query_rows),
        "old_hurt_queries_recovered": sum(bool(row["old_hurt_ref_recovered"]) for row in query_rows),
        "old_rescue_queries_lost": sum(bool(row["old_rescue_ref_lost"]) for row in query_rows),
    }
    return [*result, aggregate]


def find_non_overlapping(text: str, patterns: Mapping[str, Sequence[str]]) -> dict[str, str]:
    candidates: list[tuple[int, int, int, str, str]] = []
    for canonical, surfaces in patterns.items():
        for surface in surfaces:
            for match in re.finditer(re.escape(surface), text):
                candidates.append((match.start(), match.end(), -len(surface), canonical, surface))
    candidates.sort(key=lambda item: (item[2], item[0], item[3]))
    selected: dict[str, str] = {}
    occupied: list[tuple[int, int]] = []
    for start, end, _, canonical, surface in candidates:
        if canonical in selected or any(not (end <= left or start >= right) for left, right in occupied):
            continue
        selected[canonical] = surface
        occupied.append((start, end))
    return selected


def extract_content(text: str) -> dict[str, dict[str, str]]:
    content = {
        "subject": find_non_overlapping(text, SUBJECT_PATTERNS),
        "time": find_non_overlapping(text, TIME_PATTERNS),
        "indicator": find_non_overlapping(text, INDICATOR_PATTERNS),
        "historical_conclusion": find_non_overlapping(text, HISTORICAL_PATTERNS),
        "task_action": find_non_overlapping(text, TASK_PATTERNS),
        "constraint": find_non_overlapping(text, CONSTRAINT_PATTERNS),
        "comparison_object": {},
    }
    for year in sorted(set(re.findall(r"20\d{2}", text))):
        content["time"][f"YEAR_{year}"] = year
    specific = [canonical for canonical in content["indicator"] if canonical in SPECIFIC_INDICATORS]
    if len(specific) >= 2 and re.search(r"与|和|以及|相比|差异|匹配|同步|同向|方向相反|冲突", text):
        canonical = "COMPARE[" + "|".join(specific) + "]"
        content["comparison_object"][canonical] = " / ".join(content["indicator"][item] for item in specific)
    return content


def visible_context(query: Mapping[str, Any]) -> str:
    return "\n\n".join(
        [str(query["original_query"])]
        + [
            f"[{item['turn_id']}] User: {item['user']}\nAssistant: {item['assistant']}"
            for item in query.get("previous_3_qa") or []
        ]
    )


def assistant_head_metric_sets(query: Mapping[str, Any]) -> list[set[str]]:
    result: list[set[str]] = []
    for item in query.get("previous_3_qa") or []:
        head = str(item["assistant"]).split("三年关键数据", 1)[0][:1200]
        metrics = set(extract_content(head)["indicator"]) & SPECIFIC_INDICATORS
        if metrics:
            result.append(metrics)
    return result


def required_indicator_targets(query: Mapping[str, Any]) -> set[str]:
    original = str(query["original_query"])
    targets: set[str] = set()
    direct_rules = (
        (r"(?<!总)资产", "TOTAL_ASSETS"),
        (r"(?<!归母)股东权益|(?<!股东)权益", "PARENT_EQUITY"),
        (r"现金表现|现金流|现金证据", "OPERATING_CASH_FLOW_NET"),
        (r"扣非", "ADJUSTED_PARENT_NET_PROFIT"),
        (r"归母净利润", "PARENT_NET_PROFIT"),
        (r"营业收入", "REVENUE"),
    )
    for pattern, canonical in direct_rules:
        if re.search(pattern, original):
            targets.add(canonical)

    metric_sets = assistant_head_metric_sets(query)
    if re.search(r"两个指标|两项指标|两者|二者|这两个", original):
        unique_sets = {frozenset(items) for items in metric_sets if len(items) == 2}
        if len(unique_sets) == 1:
            targets.update(next(iter(unique_sets)))
    elif re.search(r"这个指标|该指标|这项指标", original):
        latest = metric_sets[-1] if metric_sets else set()
        if len(latest) == 1:
            targets.update(latest)
    elif re.search(r"刚才的反例|刚才的结论|上一轮的结论|前面的结论|前面的判断", original):
        latest = metric_sets[-1] if metric_sets else set()
        if len(latest) <= 2:
            targets.update(latest)
    return targets


def snippet_for_surface(text: str, surface: str, radius: int = 70) -> str | None:
    index = text.find(surface)
    if index < 0:
        return None
    return text[max(0, index - radius) : min(len(text), index + len(surface) + radius)]


def audit_variant(query: Mapping[str, Any], variant_name: str, variant_text: str) -> dict[str, Any]:
    original = str(query["original_query"])
    original_content = extract_content(original)
    variant_content = extract_content(variant_text)
    visible_text = visible_context(query)
    visible_content = extract_content(visible_text)
    required_metrics = required_indicator_targets(query)
    original_reference = bool(
        re.search(r"刚才|前面|上一轮|这条|这个|这组|这些|两者|二者|两个指标|它|其", original)
    )
    added: list[dict[str, Any]] = []
    for category in (
        "subject",
        "time",
        "indicator",
        "comparison_object",
        "historical_conclusion",
        "task_action",
        "constraint",
    ):
        for canonical, surface in variant_content[category].items():
            if canonical in original_content[category]:
                continue
            if category == "comparison_object":
                components = canonical.removeprefix("COMPARE[").removesuffix("]").split("|")
                supported = all(component in visible_content["indicator"] for component in components)
                required = bool(components) and all(component in required_metrics for component in components)
                evidence = [
                    snippet_for_surface(visible_text, visible_content["indicator"].get(component, ""))
                    for component in components
                    if visible_content["indicator"].get(component)
                ]
            else:
                supported = canonical in visible_content[category]
                evidence_surface = visible_content[category].get(canonical, "")
                evidence = [snippet_for_surface(visible_text, evidence_surface)] if evidence_surface else []
                if category == "subject":
                    required = not original_content["subject"] and supported
                elif category == "time":
                    required = bool(
                        re.search(r"三年|两段同比|最新一年|年度路径|年度变化|这几年", original)
                    ) and supported
                elif category == "indicator":
                    required = canonical in required_metrics
                elif category == "historical_conclusion":
                    required = original_reference and canonical in extract_content(
                        str((query.get("previous_3_qa") or [{}])[-1].get("assistant") or "")[:1600]
                    )["historical_conclusion"]
                else:
                    required = False
            classification = (
                "REFERENCE_REQUIRED"
                if supported and required
                else "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                if supported
                else "UNSUPPORTED"
            )
            added.append(
                {
                    "category": category,
                    "canonical": canonical,
                    "surface_text": surface,
                    "classification": classification,
                    "context_supported": supported,
                    "reference_required": required,
                    "evidence_snippets": [item for item in evidence if item],
                }
            )
    return {
        "variant": variant_name,
        "text": variant_text,
        "added_count": len(added),
        "classification_counts": dict(Counter(item["classification"] for item in added)),
        "added_content": added,
        "required_indicator_targets": sorted(required_metrics),
    }


def build_audits(
    snapshots: Mapping[str, Mapping[str, Any]],
    old_rewrites: Mapping[str, str],
    resolved: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "original_query": query["original_query"],
                    "visible_context_turn_ids": [str(item["turn_id"]) for item in query.get("previous_3_qa") or []],
                    "audit_scope": "current query + previous_3_qa only",
                    "old_rewrite": audit_variant(query, "old_rewrite", old_rewrites[query_id]),
                    "reference_resolution": audit_variant(
                        query, "reference_resolution", resolved[query_id]
                    ),
                }
            )
    return rows


def descriptive(values: Sequence[float]) -> dict[str, float | int | None]:
    clean = [float(value) for value in values]
    if not clean:
        return {"count": 0, "mean": None, "median": None, "p25": None, "p75": None, "p95": None}
    return {
        "count": len(clean),
        "mean": statistics.fmean(clean),
        "median": statistics.median(clean),
        "p25": float(np.percentile(clean, 25)),
        "p75": float(np.percentile(clean, 75)),
        "p95": float(np.percentile(clean, 95)),
    }


def query_length_summary(
    snapshots: Mapping[str, Mapping[str, Any]],
    old_rewrites: Mapping[str, str],
    resolved: Mapping[str, str],
    tokenizer: Any,
) -> dict[str, Any]:
    queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    raw = {
        "original": [str(query["original_query"]) for query in queries],
        "old_standalone_rewrite": [old_rewrites[str(query["query_id"])] for query in queries],
        "reference_resolution": [resolved[str(query["query_id"])] for query in queries],
    }
    embedding = {
        "baseline": raw["original"],
        "old_rewrite": [
            old_embedding_text(str(query["original_query"]), old_rewrites[str(query["query_id"])]) for query in queries
        ],
        "reference_resolution": [
            ref_embedding_text(str(query["original_query"]), resolved[str(query["query_id"])]) for query in queries
        ],
    }

    def one(texts: Sequence[str]) -> dict[str, Any]:
        chars = [len(text) for text in texts]
        token_counts = [len(tokenizer.encode(text, add_special_tokens=True)) for text in texts]
        return {
            "count": len(texts),
            "mean_chars": statistics.fmean(chars),
            "median_chars": statistics.median(chars),
            "p95_chars": float(np.percentile(chars, 95)),
            "mean_tokens": statistics.fmean(token_counts),
            "tokenizer": PRODUCTION_EMBEDDING,
            "token_count_includes_special_tokens": True,
        }

    ratios = [
        len(resolved[str(query["query_id"])]) / len(str(query["original_query"])) for query in queries
    ]
    old_ratios = [
        len(old_rewrites[str(query["query_id"])]) / len(str(query["original_query"])) for query in queries
    ]
    return {
        "raw_query_text": {key: one(value) for key, value in raw.items()},
        "actual_embedding_input": {key: one(value) for key, value in embedding.items()},
        "old_rewrite_to_original_char_ratio": descriptive(old_ratios),
        "resolved_to_original_char_ratio": descriptive(ratios),
    }


def over_resolution_summary(
    audits: Sequence[Mapping[str, Any]],
    gold_rows: Sequence[Mapping[str, Any]],
    length_summary: Mapping[str, Any],
) -> dict[str, Any]:
    variants: dict[str, Any] = {}
    for variant in ("old_rewrite", "reference_resolution"):
        added = [item for row in audits for item in row[variant]["added_content"]]
        classifications = Counter(item["classification"] for item in added)
        categories = Counter(item["category"] for item in added)
        variants[variant] = {
            "query_count": len(audits),
            "total_added_entities": len(added),
            "average_added_entities_per_query": len(added) / len(audits),
            "REFERENCE_REQUIRED_added_count": classifications["REFERENCE_REQUIRED"],
            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED_count": classifications[
                "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            ],
            "UNSUPPORTED_count": classifications["UNSUPPORTED"],
            "added_by_category": dict(categories),
        }
    movements: dict[str, Any] = {}
    for variant, rank_key, movement_key in (
        ("old_rewrite", "old_rewrite_rank", "old_rank_movement"),
        ("reference_resolution", "reference_resolution_rank", "reference_rank_movement"),
    ):
        movements[variant] = {
            "rank_movement_baseline_minus_variant": descriptive([row[movement_key] for row in gold_rows]),
            "extreme_demotion_top5_to_beyond20": sum(
                int(row["baseline_rank"]) <= 5 and int(row[rank_key]) > 20 for row in gold_rows
            ),
            "extreme_promotion_beyond20_to_top5": sum(
                int(row["baseline_rank"]) > 20 and int(row[rank_key]) <= 5 for row in gold_rows
            ),
        }
    return {
        "audit_method": (
            "Deterministic canonical-content diff across subject/time/indicator/comparison/historical-conclusion/"
            "task/constraint. Context support is checked only against current query + previous_3_qa. Reference-required "
            "uses explicit reference cues and unique antecedents; ambiguous multi-indicator antecedents remain not required."
        ),
        "classifications": list(CLASSIFICATIONS),
        "variants": variants,
        "length_statistics": length_summary,
        "gold_rank_movement": movements,
    }


def diverse_select(
    rows: Sequence[Mapping[str, Any]], count: int, sort_key,
) -> list[Mapping[str, Any]]:
    ordered = sorted(rows, key=sort_key)
    if len(ordered) <= count:
        return ordered
    selected: list[Mapping[str, Any]] = []
    sessions: set[str] = set()
    for row in ordered:
        if str(row["session_id"]) not in sessions:
            selected.append(row)
            sessions.add(str(row["session_id"]))
            if len(selected) == count:
                return selected
    for row in ordered:
        if row not in selected:
            selected.append(row)
            if len(selected) == count:
                return selected
    return selected


def select_cases(query_rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, Any]]:
    categories = {
        "CONSERVATIVE_RESCUED": (
            [row for row in query_rows if row["reference_transition"] == "BASE_MISS_REF_HIT"],
            3,
            lambda row: (
                int(row["best_reference_resolution_gold_rank"]) - int(row["best_baseline_gold_rank"]),
                str(row["query_id"]),
            ),
        ),
        "CONSERVATIVE_HURT": (
            [row for row in query_rows if row["reference_transition"] == "BASE_HIT_REF_MISS"],
            3,
            lambda row: (
                int(row["best_baseline_gold_rank"]) - int(row["best_reference_resolution_gold_rank"]),
                str(row["query_id"]),
            ),
        ),
        "OLD_HURT_REF_RECOVERED": (
            [row for row in query_rows if row["old_hurt_ref_recovered"]],
            3,
            lambda row: (
                int(row["best_reference_resolution_gold_rank"]) - int(row["best_old_rewrite_gold_rank"]),
                str(row["query_id"]),
            ),
        ),
        "OLD_RESCUE_REF_FAILED": (
            [row for row in query_rows if row["old_rescue_ref_lost"]],
            2,
            lambda row: (
                int(row["best_old_rewrite_gold_rank"]) - int(row["best_reference_resolution_gold_rank"]),
                str(row["query_id"]),
            ),
        ),
    }
    selected_by_category = {
        category: diverse_select(candidates, requested, sort_key)
        for category, (candidates, requested, sort_key) in categories.items()
    }
    ordered_ids = list(MANDATORY_CASE_IDS)
    for selected in selected_by_category.values():
        for row in selected:
            if str(row["query_id"]) not in ordered_ids:
                ordered_ids.append(str(row["query_id"]))
    metadata = {
        "mandatory_case_ids": list(MANDATORY_CASE_IDS),
        "requested_counts": {category: requested for category, (_, requested, _) in categories.items()},
        "available_counts": {category: len(candidates) for category, (candidates, _, _) in categories.items()},
        "selected_by_category": {
            category: [str(row["query_id"]) for row in selected]
            for category, selected in selected_by_category.items()
        },
    }
    return ordered_ids, metadata


def build_representative_cases(
    case_ids: Sequence[str],
    selection_metadata: Mapping[str, Any],
    snapshots: Mapping[str, Mapping[str, Any]],
    old_rewrites: Mapping[str, str],
    resolved: Mapping[str, str],
    audits: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> list[dict[str, Any]]:
    query_lookup = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    audit_lookup = {str(row["query_id"]): row for row in audits}
    category_by_query: dict[str, list[str]] = defaultdict(list)
    for category, ids in selection_metadata["selected_by_category"].items():
        for query_id in ids:
            category_by_query[query_id].append(category)
    cases: list[dict[str, Any]] = []
    for query_id in case_ids:
        query = query_lookup[query_id]
        code = query_id[:4]
        pages = {str(page["page_id"]): page for page in snapshots[code]["pages"]}
        gold_ids = [str(item) for item in query["eligible_gold_page_ids"]]
        gold_set = set(gold_ids)
        variant_rankings = {variant: list(rankings[code][variant][query_id]) for variant in rankings[code]}
        maps = {variant: rank_map(ranking) for variant, ranking in variant_rankings.items()}
        detail_ids: list[str] = []
        for page_id in [
            *(str(item["page_id"]) for variant in variant_rankings.values() for item in variant[:OUTPUT_K]),
            *gold_ids,
        ]:
            if page_id not in detail_ids:
                detail_ids.append(page_id)
        cases.append(
            {
                "session_id": code,
                "query_id": query_id,
                "selection_categories": category_by_query[query_id],
                "mandatory_case": query_id in MANDATORY_CASE_IDS,
                "original_query": query["original_query"],
                "old_standalone_rewrite": old_rewrites[query_id],
                "conservative_reference_resolution": resolved[query_id],
                "baseline_embedding_text": query["original_query"],
                "old_rewrite_embedding_text": old_embedding_text(
                    str(query["original_query"]), old_rewrites[query_id]
                ),
                "reference_resolution_embedding_text": ref_embedding_text(
                    str(query["original_query"]), resolved[query_id]
                ),
                "gold_page_ids": gold_ids,
                "baseline_top10": top_rows(variant_rankings["baseline"], gold_set, DISPLAY_K),
                "old_rewrite_top10": top_rows(variant_rankings["old_rewrite"], gold_set, DISPLAY_K),
                "reference_resolution_top10": top_rows(
                    variant_rankings["reference_resolution"], gold_set, DISPLAY_K
                ),
                "gold_comparison": [
                    {
                        "gold_page_id": page_id,
                        "baseline_rank": int(maps["baseline"][page_id]["rank"]),
                        "baseline_score": float(maps["baseline"][page_id]["score"]),
                        "old_rewrite_rank": int(maps["old_rewrite"][page_id]["rank"]),
                        "old_rewrite_score": float(maps["old_rewrite"][page_id]["score"]),
                        "reference_resolution_rank": int(maps["reference_resolution"][page_id]["rank"]),
                        "reference_resolution_score": float(maps["reference_resolution"][page_id]["score"]),
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
                        "old_rewrite_rank": int(maps["old_rewrite"][page_id]["rank"]),
                        "old_rewrite_score": float(maps["old_rewrite"][page_id]["score"]),
                        "reference_resolution_rank": int(maps["reference_resolution"][page_id]["rank"]),
                        "reference_resolution_score": float(maps["reference_resolution"][page_id]["score"]),
                        "actual_production_p0_embedding_text": pages[page_id]["current_embedding_text"],
                    }
                    for page_id in detail_ids
                ],
                "over_resolution_audit": audit_lookup[query_id],
            }
        )
    return cases


def fenced(text: Any) -> list[str]:
    return ["```text", str(text), "```", ""]


def ranking_markdown(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = [f"### {title}", "", "```text"]
    for row in rows:
        marker = "✅ GOLD" if row["is_gold"] else "❌ NON-GOLD"
        lines.append(
            f"#{row['rank']} {row['page_id']} score={float(row['score']):.9f} "
            f"source_turn={row['source_turn_id']} {marker}"
        )
    lines.extend(("```", ""))
    return lines


def audit_markdown(title: str, audit: Mapping[str, Any]) -> list[str]:
    lines = [f"### {title}", "", "| Category | Added content | Classification |", "|---|---|---|"]
    if not audit["added_content"]:
        lines.append("| — | No canonical content added | — |")
    for item in audit["added_content"]:
        lines.append(f"| {item['category']} | {item['surface_text']} | {item['classification']} |")
    lines.append("")
    return lines


def write_report(
    path: Path,
    session_summary: Sequence[Mapping[str, Any]],
    selection_metadata: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    over_summary: Mapping[str, Any],
) -> None:
    lines = [
        "# Conservative Reference Resolution Query Diagnosis",
        "",
        "## Frozen contract",
        "",
        "- Baseline: exact original query.",
        "- Old Rewrite: exact cached `Original query` + `Standalone query` Q5 input.",
        "- Reference Resolution: exact `Original query` + `Resolved query` input.",
        "- Context: current query + previous_3_qa only.",
        f"- Page: production P0; embedding: `{PRODUCTION_EMBEDDING}`; retrieval: per-Session dense cosine Top5.",
        "",
        "## Session R@5",
        "",
        "| Session | Baseline | Old Rewrite | Ref Resolution | Ref vs Base | Ref vs Old |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in session_summary:
        lines.append(
            f"| {row['session_id']} | {float(row['baseline_r5']):.9f} | {float(row['old_rewrite_r5']):.9f} | "
            f"{float(row['reference_resolution_r5']):.9f} | {float(row['ref_vs_base_r5']):+.9f} | "
            f"{float(row['ref_vs_old_r5']):+.9f} |"
        )
    aggregate = next(row for row in session_summary if row["session_id"] == "Aggregate")
    lines.extend(
        (
            "",
            f"Micro R@5: `{aggregate['baseline_r5']}` / `{aggregate['old_rewrite_r5']}` / "
            f"`{aggregate['reference_resolution_r5']}`.",
            "",
            f"Macro Session R@5: `{aggregate['baseline_macro_session_r5']}` / "
            f"`{aggregate['old_rewrite_macro_session_r5']}` / "
            f"`{aggregate['reference_resolution_macro_session_r5']}`.",
            "",
            "## Stability and over-resolution",
            "",
            f"Old Rewrite promoted/demoted/net: `{aggregate['old_promoted_gold']}` / "
            f"`{aggregate['old_demoted_gold']}` / `{aggregate['old_net_gain']}`.",
            "",
            f"Reference Resolution promoted/demoted/net: `{aggregate['reference_promoted_gold']}` / "
            f"`{aggregate['reference_demoted_gold']}` / `{aggregate['reference_net_gain']}`.",
            "",
            f"Old Rewrite CONTEXT_SUPPORTED_BUT_NOT_REQUIRED: "
            f"`{over_summary['variants']['old_rewrite']['CONTEXT_SUPPORTED_BUT_NOT_REQUIRED_count']}`.",
            "",
            f"Reference Resolution CONTEXT_SUPPORTED_BUT_NOT_REQUIRED: "
            f"`{over_summary['variants']['reference_resolution']['CONTEXT_SUPPORTED_BUT_NOT_REQUIRED_count']}`.",
            "",
            "## Selection metadata",
            "",
            "```json",
            json.dumps(selection_metadata, ensure_ascii=False, indent=2),
            "```",
            "",
        )
    )
    for case in cases:
        lines.extend((f"# {case['query_id']}", "", f"Selection: `{case['selection_categories']}`", ""))
        lines.extend(("### Original Query", "", *fenced(case["original_query"])))
        lines.extend(("### Old Standalone Rewrite", "", *fenced(case["old_standalone_rewrite"])))
        lines.extend(
            (
                "### Conservative Reference Resolution",
                "",
                *fenced(case["conservative_reference_resolution"]),
            )
        )
        lines.extend(("### Baseline embedding text", "", *fenced(case["baseline_embedding_text"])))
        lines.extend(("### Old Rewrite embedding text", "", *fenced(case["old_rewrite_embedding_text"])))
        lines.extend(
            (
                "### Reference Resolution embedding text",
                "",
                *fenced(case["reference_resolution_embedding_text"]),
            )
        )
        lines.extend(ranking_markdown("Baseline Top10", case["baseline_top10"]))
        lines.extend(ranking_markdown("Old Rewrite Top10", case["old_rewrite_top10"]))
        lines.extend(ranking_markdown("Reference Resolution Top10", case["reference_resolution_top10"]))
        lines.extend(("### All Gold ranks", "", "```text"))
        for gold in case["gold_comparison"]:
            lines.extend(
                (
                    f"Gold {gold['gold_page_id']}",
                    f"Baseline #{gold['baseline_rank']} score={float(gold['baseline_score']):.9f}",
                    f"Old Rewrite #{gold['old_rewrite_rank']} score={float(gold['old_rewrite_score']):.9f}",
                    f"Reference Resolution #{gold['reference_resolution_rank']} "
                    f"score={float(gold['reference_resolution_score']):.9f}",
                    "",
                )
            )
        lines.extend(("```", ""))
        lines.extend(audit_markdown("Old Rewrite Over-Resolution Audit", case["over_resolution_audit"]["old_rewrite"]))
        lines.extend(
            audit_markdown(
                "Reference Resolution Over-Resolution Audit",
                case["over_resolution_audit"]["reference_resolution"],
            )
        )
        lines.extend(("### Production P0 Page texts (three Top5 union + all Gold)", ""))
        for page in case["page_details"]:
            lines.extend(
                (
                    f"#### Page {page['page_id']}",
                    "",
                    f"- Source Turn ID: `{page['source_turn_id']}`",
                    f"- Gold: `{'YES' if page['is_gold'] else 'NO'}`",
                    f"- Baseline: `#{page['baseline_rank']} / {float(page['baseline_score']):.9f}`",
                    f"- Old Rewrite: `#{page['old_rewrite_rank']} / {float(page['old_rewrite_score']):.9f}`",
                    f"- Reference Resolution: `#{page['reference_resolution_rank']} / "
                    f"{float(page['reference_resolution_score']):.9f}`",
                    "",
                    "实际生产 P0 embedding text：",
                    "",
                    *fenced(page["actual_production_p0_embedding_text"]),
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


async def async_main(args: argparse.Namespace) -> None:
    args.output_dir = args.output_dir.resolve()
    snapshots = load_snapshots()
    old_rewrites = load_old_rewrites(snapshots)
    resolved, llm_rows, reference_metadata = await load_or_generate_resolved_queries(args, snapshots)
    vectors, embedding_cache, embedding_metadata = load_query_vectors(args.output_dir, snapshots, resolved)

    rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    metrics_by_session: dict[str, dict[str, Any]] = {}
    for code in SESSION_CODES:
        rankings[code] = rank_snapshot(snapshots[code], vectors)
        metrics_by_session[code] = {}
        for variant in ("baseline", "old_rewrite", "reference_resolution"):
            metrics, _ = evaluate_rankings(snapshots[code]["queries"], rankings[code][variant])
            metrics_by_session[code][variant] = metrics
            metrics_by_session[code][f"{variant}_rankings"] = rankings[code][variant]
    reuse_validation = validate_reused_metrics(metrics_by_session)
    gold_rows, query_rows = build_gold_and_query_transitions(snapshots, rankings)
    summary = build_session_summary(snapshots, metrics_by_session, gold_rows, query_rows)

    audits = build_audits(snapshots, old_rewrites, resolved)
    tokenizer = AutoTokenizer.from_pretrained(PRODUCTION_EMBEDDING)
    length_summary = query_length_summary(snapshots, old_rewrites, resolved, tokenizer)
    over_summary = over_resolution_summary(audits, gold_rows, length_summary)
    case_ids, selection_metadata = select_cases(query_rows)
    cases = build_representative_cases(
        case_ids,
        selection_metadata,
        snapshots,
        old_rewrites,
        resolved,
        audits,
        rankings,
    )

    query_output = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            llm_row = llm_rows[query_id]
            query_output.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "original_query": query["original_query"],
                    "old_standalone_rewrite": old_rewrites[query_id],
                    "conservative_reference_resolution": resolved[query_id],
                    "baseline_embedding_text": query["original_query"],
                    "old_rewrite_embedding_text": old_embedding_text(
                        str(query["original_query"]), old_rewrites[query_id]
                    ),
                    "reference_resolution_embedding_text": ref_embedding_text(
                        str(query["original_query"]), resolved[query_id]
                    ),
                    "visible_context_turn_ids": [str(item["turn_id"]) for item in query.get("previous_3_qa") or []],
                    "prompt_version": llm_row["prompt_version"],
                    "prompt_hash": llm_row["prompt_hash"],
                    "model": llm_row["model"],
                    "thinking_mode": llm_row["thinking_mode"],
                }
            )

    write_csv(args.output_dir / "session_summary.csv", summary)
    write_csv(args.output_dir / "query_transition.csv", gold_rows)
    write_jsonl(args.output_dir / "reference_resolution_queries.jsonl", query_output)
    write_jsonl(args.output_dir / "reference_resolution_audit.jsonl", audits)
    dump_json(args.output_dir / "over_resolution_summary.json", over_summary)
    dump_json(
        args.output_dir / "representative_cases.json",
        {"selection_metadata": selection_metadata, "cases": cases},
    )
    write_report(
        args.output_dir / "representative_cases.md",
        summary,
        selection_metadata,
        cases,
        over_summary,
    )
    run_metadata = {
        "full_session_rerun": False,
        "page_representation": "production P0: summary + Keywords + User",
        "embedding_model": PRODUCTION_EMBEDDING,
        "retrieval_method": "per-Session dense cosine",
        "output_k": OUTPUT_K,
        "reference_resolution_model": reference_metadata["model"],
        "thinking_mode": reference_metadata["thinking_mode"],
        "prompt_version": reference_metadata["prompt_version"],
        "prompt_sha256": reference_metadata["prompt_sha256"],
        "frozen_system_prompt": REFERENCE_SYSTEM_PROMPT,
        "context_contract": "current query + previous_3_qa only",
        "reference_embedding_format": "Original query: <original>\\nResolved query: <resolved>",
        "old_rewrite_cache_reused": len(old_rewrites),
        "baseline_embedding_cache_reused": embedding_metadata["baseline_embedding_cache_reused"],
        "old_rewrite_embedding_cache_reused": embedding_metadata["old_rewrite_embedding_cache_reused"],
        "page_embedding_reused": sum(len(snapshots[code]["pages"]) for code in SESSION_CODES),
        "new_llm_api_attempt_count": reference_metadata["new_llm_api_attempt_count"],
        "new_embedding_count": embedding_metadata["new_embedding_count"],
        "reference_generation": reference_metadata,
        "embedding_cache": embedding_metadata,
        "reuse_validation": reuse_validation,
        "snapshot_paths": {code: snapshots[code]["path"] for code in SESSION_CODES},
    }
    dump_json(args.output_dir / "run_metadata.json", run_metadata)
    embedding_cache.release(PRODUCTION_EMBEDDING)


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
