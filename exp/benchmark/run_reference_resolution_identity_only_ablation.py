"""Evaluate frozen identity-only Reference Resolution P4 against Baseline/P0/P2.

Only P4 is newly generated. Baseline, P0, P2, immutable query-time snapshots,
eligible Gold, visible Page IDs, and production P0 Page vectors are reused.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
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
    write_jsonl,
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
from exp.benchmark.run_reference_resolution_prompt_ablation import (  # noqa: E402
    PlainTextJsonlLLMCache,
    base_audit_variant,
    normalized_classification,
    resolution_messages,
    resolution_type,
    resolved_validator,
    semantic_object_additions,
)


PREVIOUS_RESULT_DIR = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation"
CLEAN_RESULT_DIR = REPO_ROOT / "exp/results/query_rewrite_clean_replacement_ablation"
OUTPUT_DIR = REPO_ROOT / "exp/results/reference_resolution_identity_only_ablation"
P4_PROMPT_FILE = "P4_identity_only.txt"
P4_PROMPT_VERSION = "reference-resolution-identity-only-p4-v1"
VARIANTS = ("Baseline", "P0", "P2", "P4")
PROMPTS = ("P0", "P2", "P4")
MANDATORY_CASE_IDS = ("S001-Q033", "S003-Q044", "S004-Q052", "S004-Q063", "S005-Q042")
RESOLUTION_TYPES = (
    "INDICATOR",
    "COMPARISON_OBJECT",
    "PRIOR_JUDGMENT",
    "EVIDENCE_OBJECT",
    "SUBJECT",
    "TIME",
    "OTHER",
)
CLASSIFICATIONS = (
    "REFERENCE_REQUIRED",
    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED",
    "UNSUPPORTED",
)
STATE_TERMS = (
    "增减方向和幅度不匹配",
    "增减方向和幅度匹配",
    "只能确认存在错位",
    "不能判断效率问题",
    "先升后降",
    "先降后升",
    "连续上升",
    "连续下降",
    "持续上升",
    "持续下降",
    "方向相反",
    "方向一致",
    "方向不同",
    "方向相同",
    "不支持",
    "不支撑",
    "不匹配",
    "不同步",
    "增速放缓",
    "边际改善",
    "边际恶化",
    "出现拐点",
    "发生反转",
    "连续改善",
    "连续恶化",
    "扩大",
    "收缩",
    "扩张",
    "反转",
    "拐点",
    "改善",
    "恶化",
    "承压",
    "回落",
    "错位",
    "支持",
    "支撑",
    "匹配",
    "同步",
    "上升",
    "下降",
    "增加",
    "减少",
    "增长",
    "缩小",
    "增强",
    "减弱",
    "稳定",
)
OUTPUT_K = 5
DISPLAY_K = 10
SCORE_ATOL = 1e-6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Identity-only Reference Resolution ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen_p4(output_dir: Path) -> tuple[str, dict[str, Any]]:
    prompt_dir = output_dir / "prompts"
    manifest = load_json(prompt_dir / "prompt_sha256.json")
    row = manifest.get("P4") or {}
    if row.get("file") != P4_PROMPT_FILE or not manifest.get("frozen_before_first_llm_call"):
        raise ValueError("P4 freeze manifest is invalid")
    path = prompt_dir / P4_PROMPT_FILE
    actual = sha256_bytes(path)
    if actual != row.get("sha256"):
        raise ValueError(f"P4 SHA256 mismatch: {actual} != {row.get('sha256')}")
    return path.read_text(encoding="utf-8"), manifest


def load_prior_queries(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, Any]]]:
    rows = load_jsonl(PREVIOUS_RESULT_DIR / "resolved_queries.jsonl")
    by_query = {str(row["query_id"]): row for row in rows}
    expected = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    if len(rows) != 99 or set(by_query) != set(expected):
        raise ValueError(f"Previous P0/P2 Query coverage mismatch: {len(rows)}")
    resolved = {"P0": {}, "P2": {}}
    for query_id, query in expected.items():
        row = by_query[query_id]
        if str(row["original_query"]) != str(query["original_query"]):
            raise ValueError(f"Previous original Query mismatch: {query_id}")
        resolved["P0"][query_id] = str(row["P0"]["resolved_query"])
        resolved["P2"][query_id] = str(row["P2"]["resolved_query"])
    return resolved, by_query


async def load_or_generate_p4(
    args: argparse.Namespace,
    snapshots: Mapping[str, Mapping[str, Any]],
    system_prompt: str,
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for P4")
    model = str(llm_config.get("model") or "deepseek-chat")
    if model != "deepseek-v4-flash":
        raise ValueError(f"P4 model must remain deepseek-v4-flash, got {model}")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    cache_path = args.output_dir / "cache/p4_resolution_llm.jsonl"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    cache = PlainTextJsonlLLMCache(
        cache_path,
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )
    initial_keys = set(cache.success_by_key)
    queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]

    async def one(query: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant="P4",
            messages=resolution_messages(query, system_prompt),
            prompt_version=P4_PROMPT_VERSION,
            max_tokens=800,
            validator=resolved_validator,
        )
        return str(query["query_id"]), row

    try:
        results = await asyncio.gather(*(one(query) for query in queries))
    finally:
        await client.close()
    failures = [
        {"query_id": query_id, "errors": row.get("errors")}
        for query_id, row in results
        if row.get("status") != "SUCCESS"
    ]
    if failures:
        dump_json(args.output_dir / "p4_resolution_failures.json", failures)
        raise RuntimeError(f"P4 generation failed for {len(failures)} Queries")
    resolved = {query_id: str(row["parsed"]["resolved_query"]) for query_id, row in results}
    rows_by_query = {query_id: row for query_id, row in results}
    if len(resolved) != 99:
        raise ValueError(f"P4 successful coverage is {len(resolved)}, expected 99")

    durable = [
        row
        for row in load_jsonl(cache_path)
        if row.get("variant") == "P4"
        and row.get("prompt_version") == P4_PROMPT_VERSION
        and row.get("model") == model
        and row.get("thinking_mode") == "disabled"
    ]
    successful = [row for row in durable if row.get("status") == "SUCCESS"]
    rejected = [
        row
        for row in durable
        if row.get("status") == "FAILED"
        and any(
            "Prompt must contain the word 'json'" in str(error)
            for error in row.get("errors") or []
        )
    ]
    metadata = {
        "model": model,
        "thinking_mode": "disabled",
        "temperature": 0.0,
        "response_mode": "plain_text parsed and strictly validated as a JSON object",
        "prompt_version": P4_PROMPT_VERSION,
        "query_count": len(successful),
        "successful_model_output_count": len(successful),
        "successful_row_api_attempt_count": sum(
            1 + int(row.get("retry_count") or 0) for row in successful
        ),
        "new_llm_api_attempt_count": sum(
            1 + int(row.get("retry_count") or 0) for row in durable
        ),
        "provider_precondition_rejected_attempt_count": sum(
            1 + int(row.get("retry_count") or 0) for row in rejected
        ),
        "durable_success_rows": len(successful),
        "durable_failed_rows": len(durable) - len(successful),
        "current_run_cache_hits": sum(str(row["cache_key"]) in initial_keys for _, row in results),
        "current_run_cache_misses": sum(str(row["cache_key"]) not in initial_keys for _, row in results),
        "cache_path": str(cache_path),
    }
    return resolved, rows_by_query, metadata


def load_query_vectors(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    p4_resolved: Mapping[str, str],
) -> tuple[dict[str, list[float]], EmbeddingCache, dict[str, Any]]:
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    vectors: dict[str, list[float]] = {}
    s001_ids = [f"Q0:{query['query_id']}:0" for query in snapshots["S001"]["queries"]]
    baseline = load_existing_embedding_vectors(
        S001_RESULT_DIR,
        model_name=PRODUCTION_EMBEDDING,
        prefix="queries-",
        required_ids=s001_ids,
    )
    heldout = [query for code in SESSION_CODES[1:] for query in snapshots[code]["queries"]]
    heldout_ids = [f"Q0:{query['query_id']}:0" for query in heldout]
    baseline.update(
        load_existing_embedding_vectors(
            MULTI_SESSION_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="heldout-Q0-",
            required_ids=heldout_ids,
        )
    )
    p0_ids = [f"QREFONLY:{query['query_id']}:0" for query in all_queries]
    p0_vectors = load_existing_embedding_vectors(
        CLEAN_RESULT_DIR,
        model_name=PRODUCTION_EMBEDDING,
        prefix="conservative-reference-resolution-only-",
        required_ids=p0_ids,
    )
    p2_ids = [f"P2:{query['query_id']}:0" for query in all_queries]
    p2_vectors = load_existing_embedding_vectors(
        PREVIOUS_RESULT_DIR,
        model_name=PRODUCTION_EMBEDDING,
        prefix="P2-resolved-query-only-",
        required_ids=p2_ids,
    )
    for query in all_queries:
        query_id = str(query["query_id"])
        vectors[f"Baseline:{query_id}"] = baseline[f"Q0:{query_id}:0"]
        vectors[f"P0:{query_id}"] = p0_vectors[f"QREFONLY:{query_id}:0"]
        vectors[f"P2:{query_id}"] = p2_vectors[f"P2:{query_id}:0"]

    cache = EmbeddingCache(output_dir / "cache/embeddings")
    p4_ids = [f"P4:{query['query_id']}:0" for query in all_queries]
    p4_texts = [p4_resolved[str(query["query_id"])] for query in all_queries]
    encoded, metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "P4-identity-only-resolved-query-S001-S005",
        p4_ids,
        p4_texts,
        measure_individual=True,
    )
    for query in all_queries:
        query_id = str(query["query_id"])
        vectors[f"P4:{query_id}"] = encoded[f"P4:{query_id}:0"]
    return vectors, cache, {
        "baseline_embedding_cache_reused": 99,
        "p0_embedding_cache_reused": 99,
        "p2_embedding_cache_reused": 99,
        "p4_query_embedding_count": 99,
        "p4_current_run_new_embedding_count": 0 if metadata.get("cache_hit") else 99,
        "p4_embedding_cache_hit": bool(metadata.get("cache_hit")),
        "p4_embedding_batch": metadata,
    }


def rank_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for code in SESSION_CODES:
        snapshot = snapshots[code]
        pages = {str(page["page_id"]): page for page in snapshot["pages"]}
        page_vectors = {page_id: page["stored_embedding"] for page_id, page in pages.items()}
        visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
        rankings[code] = {variant: {} for variant in VARIANTS}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            visible_pages = [pages[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
            for variant in VARIANTS:
                rankings[code][variant][query_id] = cosine_rank(
                    vectors[f"{variant}:{query_id}"], visible_pages, page_vectors
                )
    return rankings


def build_p4_audits(
    snapshots: Mapping[str, Mapping[str, Any]],
    p4_resolved: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            original = str(query["original_query"])
            base = base_audit_variant(query, "P4", p4_resolved[query_id])
            semantic = semantic_object_additions(query, p4_resolved[query_id])
            semantic_surfaces = {item["surface_text"] for item in semantic}
            additions = []
            for item in base["added_content"]:
                if item["category"] == "task_action" and any(
                    str(item["surface_text"]) in surface for surface in semantic_surfaces
                ):
                    continue
                additions.append(
                    {
                        **item,
                        "raw_category": item["category"],
                        "resolution_type": resolution_type(original, str(item["category"])),
                        "classification": normalized_classification(original, item),
                    }
                )
            additions.extend(semantic)
            rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "prompt": "P4",
                    "original_query": original,
                    "resolved_query": p4_resolved[query_id],
                    "changed": p4_resolved[query_id] != original,
                    "visible_context_turn_ids": [
                        str(item["turn_id"]) for item in query.get("previous_3_qa") or []
                    ],
                    "audit_scope": "current query + previous_3_qa only; no Gold/Page/ranking/audit labels",
                    "total_added_canonical_content": len(additions),
                    "classification_counts": dict(Counter(item["classification"] for item in additions)),
                    "resolution_type_counts": dict(Counter(item["resolution_type"] for item in additions)),
                    "added_content": additions,
                }
            )
    return rows


def load_reused_audits() -> dict[tuple[str, str], dict[str, Any]]:
    rows = load_jsonl(PREVIOUS_RESULT_DIR / "resolution_audit.jsonl")
    selected = [row for row in rows if row.get("prompt") in {"P0", "P2"}]
    lookup = {(str(row["prompt"]), str(row["query_id"])): row for row in selected}
    if len(lookup) != 198:
        raise ValueError(f"Reused P0/P2 audit coverage is {len(lookup)}, expected 198")
    return lookup


def added_spans(original: str, resolved: str) -> list[dict[str, Any]]:
    matcher = difflib.SequenceMatcher(a=original, b=resolved, autojunk=False)
    return [
        {"tag": tag, "original_span": original[i1:i2], "resolved_span": resolved[j1:j2], "start": j1, "end": j2}
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag in {"insert", "replace"} and resolved[j1:j2]
    ]


def state_content_items(
    original: str,
    resolved: str,
    audit: Mapping[str, Any],
) -> list[dict[str, Any]]:
    spans = added_spans(original, resolved)
    items: dict[str, dict[str, Any]] = {}
    for addition in audit["added_content"]:
        if addition.get("raw_category") != "historical_conclusion":
            continue
        surface = str(addition["surface_text"])
        items[surface] = {
            "surface_text": surface,
            "canonical": addition["canonical"],
            "detection_methods": ["canonical_historical_conclusion_diff"],
            "classification": addition["classification"],
        }

    occupied: list[tuple[int, int]] = []
    candidates: list[tuple[int, int, str]] = []
    for term in sorted(STATE_TERMS, key=len, reverse=True):
        original_count = original.count(term)
        matches = list(re.finditer(re.escape(term), resolved))
        excess = max(0, len(matches) - original_count)
        if not excess:
            continue
        for match in matches[-excess:]:
            candidates.append((match.start(), match.end(), term))
    candidates.sort(key=lambda item: (-(item[1] - item[0]), item[0], item[2]))
    for start, end, term in candidates:
        if any(not (end <= left or start >= right) for left, right in occupied):
            continue
        occupied.append((start, end))
        overlaps_added_span = any(not (end <= span["start"] or start >= span["end"]) for span in spans)
        row = items.setdefault(
            term,
            {
                "surface_text": term,
                "canonical": None,
                "detection_methods": [],
                "classification": None,
            },
        )
        row["detection_methods"].append("state_term_occurrence_delta")
        if overlaps_added_span:
            row["detection_methods"].append("added_span_alignment")
    return list(items.values())


def build_state_audit(
    query_lookup: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    audit_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for prompt in ("P2", "P4"):
        for query_id, query in query_lookup.items():
            original = str(query["original_query"])
            resolved_query = resolved[prompt][query_id]
            items = state_content_items(original, resolved_query, audit_lookup[(prompt, query_id)])
            row = {
                "session_id": query_id[:4],
                "query_id": query_id,
                "prompt": prompt,
                "original_query": original,
                "resolved_query": resolved_query,
                "state_content_additions": len(items),
                "state_content_items": json.dumps(items, ensure_ascii=False),
                "added_spans": json.dumps(added_spans(original, resolved_query), ensure_ascii=False),
            }
            rows.append(row)
            lookup[(prompt, query_id)] = {**row, "items": items}
    return rows, lookup


def summarize_audits(
    audit_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    state_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prompts: dict[str, Any] = {}
    for prompt in PROMPTS:
        audits = [row for (name, _), row in audit_lookup.items() if name == prompt]
        additions = [item for audit in audits for item in audit["added_content"]]
        classifications = Counter(item["classification"] for item in additions)
        by_type: dict[str, Any] = {}
        for item_type in RESOLUTION_TYPES:
            scoped = [item for item in additions if item["resolution_type"] == item_type]
            counts = Counter(item["classification"] for item in scoped)
            precision = counts["REFERENCE_REQUIRED"] / len(scoped) if scoped else None
            row = {
                "prompt": prompt,
                "resolution_type": item_type,
                "total": len(scoped),
                "reference_required": counts["REFERENCE_REQUIRED"],
                "context_supported_but_not_required": counts[
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                ],
                "unsupported": counts["UNSUPPORTED"],
                "precision": precision,
            }
            rows.append(row)
            by_type[item_type] = row
        total = len(additions)
        all_row = {
            "prompt": prompt,
            "resolution_type": "ALL",
            "total": total,
            "reference_required": classifications["REFERENCE_REQUIRED"],
            "context_supported_but_not_required": classifications[
                "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            ],
            "unsupported": classifications["UNSUPPORTED"],
            "precision": classifications["REFERENCE_REQUIRED"] / total if total else None,
        }
        rows.append(all_row)
        prompts[prompt] = {
            "total_added_canonical_content": total,
            "REFERENCE_REQUIRED": classifications["REFERENCE_REQUIRED"],
            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED": classifications[
                "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            ],
            "UNSUPPORTED": classifications["UNSUPPORTED"],
            "added_content_precision": classifications["REFERENCE_REQUIRED"] / total if total else None,
            "unnecessary_addition_rate": classifications[
                "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            ]
            / total
            if total
            else None,
            "state_content_additions": sum(
                int(row["state_content_additions"])
                for (name, _), row in state_lookup.items()
                if name == prompt
            )
            if prompt in {"P2", "P4"}
            else None,
            "by_resolution_type": by_type,
        }
    previous = load_json(PREVIOUS_RESULT_DIR / "over_resolution_summary.json")["prompts"]
    for prompt in ("P0", "P2"):
        for key in (
            "total_added_canonical_content",
            "REFERENCE_REQUIRED",
            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED",
            "UNSUPPORTED",
        ):
            if prompts[prompt][key] != previous[prompt][key]:
                raise AssertionError(f"Reused {prompt} audit differs for {key}")
    return rows, {
        "audit_method": (
            "Reused P0/P2 deterministic canonical audit; P4 uses the identical canonical/semantic-object method. "
            "State audit combines canonical historical-conclusion diff, state-term occurrence delta, and "
            "SequenceMatcher added-span alignment. No Gold/Page/ranking enters either audit."
        ),
        "prompts": prompts,
        "p0_p2_prior_audit_reproduction": "PASS",
    }


def evaluate_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    metrics: dict[str, dict[str, dict[str, Any]]] = {}
    for code in SESSION_CODES:
        metrics[code] = {}
        for variant in VARIANTS:
            metrics[code][variant], _ = evaluate_rankings(
                snapshots[code]["queries"], rankings[code][variant]
            )
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    metrics["Aggregate"] = {}
    for variant in VARIANTS:
        pooled = {
            query_id: ranking
            for code in SESSION_CODES
            for query_id, ranking in rankings[code][variant].items()
        }
        metrics["Aggregate"][variant], _ = evaluate_rankings(all_queries, pooled)
        metrics["Aggregate"][variant]["macro_session_r5"] = statistics.fmean(
            float(metrics[code][variant]["recall_at_5"]) for code in SESSION_CODES
        )
    return metrics


def transition(baseline_hit: bool, variant_hit: bool) -> str:
    if not baseline_hit and variant_hit:
        return "RESCUED"
    if baseline_hit and not variant_hit:
        return "HURT"
    if baseline_hit:
        return "UNCHANGED_HIT"
    return "UNCHANGED_MISS"


def build_gold_and_query_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            original = str(query["original_query"])
            gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
            maps = {
                variant: rank_map(rankings[code][variant][query_id]) for variant in VARIANTS
            }
            hits = {
                variant: any(int(maps[variant][page_id]["rank"]) <= OUTPUT_K for page_id in gold_ids)
                for variant in VARIANTS
            }
            row: dict[str, Any] = {
                "session_id": code,
                "query_id": query_id,
                "original_query": original,
                "gold_page_ids": json.dumps(gold_ids, ensure_ascii=False),
                "baseline_actual_embedding_text": original,
                "baseline_hit5": hits["Baseline"],
                "baseline_gold_ranks": json.dumps(
                    [int(maps["Baseline"][page_id]["rank"]) for page_id in gold_ids]
                ),
                "baseline_gold_scores": json.dumps(
                    [float(maps["Baseline"][page_id]["score"]) for page_id in gold_ids]
                ),
            }
            for prompt in PROMPTS:
                row.update(
                    {
                        f"{prompt}_resolved_query": resolved[prompt][query_id],
                        f"{prompt}_actual_embedding_text": resolved[prompt][query_id],
                        f"{prompt}_changed": resolved[prompt][query_id] != original,
                        f"{prompt}_hit5": hits[prompt],
                        f"{prompt}_transition": transition(hits["Baseline"], hits[prompt]),
                        f"{prompt}_gold_ranks": json.dumps(
                            [int(maps[prompt][page_id]["rank"]) for page_id in gold_ids]
                        ),
                        f"{prompt}_gold_scores": json.dumps(
                            [float(maps[prompt][page_id]["score"]) for page_id in gold_ids]
                        ),
                    }
                )
            query_rows.append(row)
            for page_id in gold_ids:
                gold_row: dict[str, Any] = {
                    "session_id": code,
                    "query_id": query_id,
                    "gold_page_id": page_id,
                    "baseline_rank": int(maps["Baseline"][page_id]["rank"]),
                    "baseline_score": float(maps["Baseline"][page_id]["score"]),
                }
                for prompt in PROMPTS:
                    prompt_rank = int(maps[prompt][page_id]["rank"])
                    baseline_rank = int(gold_row["baseline_rank"])
                    gold_row.update(
                        {
                            f"{prompt}_rank": prompt_rank,
                            f"{prompt}_score": float(maps[prompt][page_id]["score"]),
                            f"{prompt}_promoted": baseline_rank > OUTPUT_K and prompt_rank <= OUTPUT_K,
                            f"{prompt}_demoted": baseline_rank <= OUTPUT_K and prompt_rank > OUTPUT_K,
                            f"{prompt}_rank_movement": baseline_rank - prompt_rank,
                        }
                    )
                gold_rows.append(gold_row)
    return gold_rows, query_rows


def movement_counts(prompt: str, rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    if prompt == "Baseline":
        return {"promoted_gold": 0, "demoted_gold": 0, "net_gold_gain": 0}
    promoted = sum(bool(row[f"{prompt}_promoted"]) for row in rows)
    demoted = sum(bool(row[f"{prompt}_demoted"]) for row in rows)
    return {"promoted_gold": promoted, "demoted_gold": demoted, "net_gold_gain": promoted - demoted}


def build_prompt_metrics(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    audit_summary: Mapping[str, Any],
) -> list[dict[str, Any]]:
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    result: list[dict[str, Any]] = []
    for variant in VARIANTS:
        if variant == "Baseline":
            changed_queries: list[Mapping[str, Any]] = []
        else:
            changed_queries = [
                query
                for query in all_queries
                if resolved[variant][str(query["query_id"])] != str(query["original_query"])
            ]
        changed_ids = {str(query["query_id"]) for query in changed_queries}
        scoped_gold = [row for row in gold_rows if row["query_id"] in changed_ids]
        counts = movement_counts(variant, gold_rows)
        useful = 0
        harmful = 0
        for query_id in changed_ids:
            query_gold = [row for row in scoped_gold if row["query_id"] == query_id]
            has_promotion = any(bool(row[f"{variant}_promoted"]) for row in query_gold)
            has_demotion = any(bool(row[f"{variant}_demoted"]) for row in query_gold)
            useful += int(has_promotion and not has_demotion)
            harmful += int(has_demotion)
        aggregate = metrics["Aggregate"][variant]
        audit = audit_summary.get(variant) or {}
        result.append(
            {
                "prompt": variant,
                "changed_query_count": len(changed_queries),
                "unchanged_query_count": len(all_queries) - len(changed_queries),
                "eligible_gold_count": aggregate["eligible_gold_count"],
                "micro_r5": aggregate["recall_at_5"],
                "macro_session_r5": aggregate["macro_session_r5"],
                "r10": aggregate["recall_at_10"],
                "r20": aggregate["recall_at_20"],
                "mrr": aggregate["mrr"],
                "mean_gold_rank": aggregate["mean_gold_rank"],
                **counts,
                "rescued_query_count": 0
                if variant == "Baseline"
                else sum(row[f"{variant}_transition"] == "RESCUED" for row in query_rows),
                "hurt_query_count": 0
                if variant == "Baseline"
                else sum(row[f"{variant}_transition"] == "HURT" for row in query_rows),
                "extreme_demotion_top5_to_beyond20": 0
                if variant == "Baseline"
                else sum(
                    int(row["baseline_rank"]) <= 5 and int(row[f"{variant}_rank"]) > 20
                    for row in gold_rows
                ),
                "extreme_promotion_beyond20_to_top5": 0
                if variant == "Baseline"
                else sum(
                    int(row["baseline_rank"]) > 20 and int(row[f"{variant}_rank"]) <= 5
                    for row in gold_rows
                ),
                "total_added_canonical_content": audit.get("total_added_canonical_content", 0),
                "reference_required": audit.get("REFERENCE_REQUIRED", 0),
                "context_supported_but_not_required": audit.get(
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED", 0
                ),
                "unsupported": audit.get("UNSUPPORTED", 0),
                "added_content_precision": audit.get("added_content_precision"),
                "unnecessary_addition_rate": audit.get("unnecessary_addition_rate"),
                "useful_changed_query_count": useful,
                "useful_change_rate": useful / len(changed_queries) if changed_queries else None,
                "harmful_changed_query_count": harmful,
                "harmful_change_rate": harmful / len(changed_queries) if changed_queries else None,
            }
        )
    return result


def build_session_metrics(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        scoped_gold = [row for row in gold_rows if row["session_id"] == code]
        scoped_queries = [row for row in query_rows if row["session_id"] == code]
        for variant in VARIANTS:
            changed = 0
            if variant != "Baseline":
                changed = sum(
                    resolved[variant][str(query["query_id"])] != str(query["original_query"])
                    for query in snapshots[code]["queries"]
                )
            counts = movement_counts(variant, scoped_gold)
            value = metrics[code][variant]
            baseline = metrics[code]["Baseline"]
            result.append(
                {
                    "session_id": code,
                    "prompt": variant,
                    "query_count": len(snapshots[code]["queries"]),
                    "changed_query_count": changed,
                    "eligible_gold_count": value["eligible_gold_count"],
                    "r5": value["recall_at_5"],
                    "delta_vs_baseline_r5": float(value["recall_at_5"])
                    - float(baseline["recall_at_5"]),
                    "r10": value["recall_at_10"],
                    "r20": value["recall_at_20"],
                    "mrr": value["mrr"],
                    "mean_gold_rank": value["mean_gold_rank"],
                    **counts,
                    "rescued_query_count": 0
                    if variant == "Baseline"
                    else sum(row[f"{variant}_transition"] == "RESCUED" for row in scoped_queries),
                    "hurt_query_count": 0
                    if variant == "Baseline"
                    else sum(row[f"{variant}_transition"] == "HURT" for row in scoped_queries),
                }
            )
    return result


def build_tradeoff(gold_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    p2_promotions = [row for row in gold_rows if row["P2_promoted"]]
    p2_demotions = [row for row in gold_rows if row["P2_demoted"]]
    preserved = [row for row in p2_promotions if int(row["P4_rank"]) <= OUTPUT_K]
    lost = [row for row in p2_promotions if int(row["P4_rank"]) > OUTPUT_K]
    new = [row for row in gold_rows if row["P4_promoted"] and not row["P2_promoted"]]
    recovered = [row for row in p2_demotions if int(row["P4_rank"]) <= OUTPUT_K]

    def keys(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        return [
            {
                "session_id": str(row["session_id"]),
                "query_id": str(row["query_id"]),
                "gold_page_id": str(row["gold_page_id"]),
            }
            for row in rows
        ]

    result = {
        "p2_promoted_gold_total": len(p2_promotions),
        "p4_preserved_p2_promotions": len(preserved),
        "p4_lost_p2_promotions": len(lost),
        "p4_new_promotions": len(new),
        "p2_demoted_gold_total": len(p2_demotions),
        "p4_recovered_p2_demotions": len(recovered),
        "preserved_p2_promotions": keys(preserved),
        "lost_p2_promotions": keys(lost),
        "new_p4_promotions": keys(new),
        "recovered_p2_demotions": keys(recovered),
    }
    if result["p2_promoted_gold_total"] != 7 or result["p2_demoted_gold_total"] != 3:
        raise AssertionError(f"P2 movement counts do not reproduce prior experiment: {result}")
    return result


def build_p2_p4_diff(
    query_rows: Sequence[Mapping[str, Any]],
    audit_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    state_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for query in query_rows:
        query_id = str(query["query_id"])
        p2_audit = audit_lookup[("P2", query_id)]
        p4_audit = audit_lookup[("P4", query_id)]
        rows.append(
            {
                "session_id": query["session_id"],
                "query_id": query_id,
                "original_query": query["original_query"],
                "p2_resolved_query": query["P2_resolved_query"],
                "p4_resolved_query": query["P4_resolved_query"],
                "p2_changed": query["P2_changed"],
                "p4_changed": query["P4_changed"],
                "p2_added_content_count": p2_audit["total_added_canonical_content"],
                "p4_added_content_count": p4_audit["total_added_canonical_content"],
                "p2_reference_required": p2_audit["classification_counts"].get(
                    "REFERENCE_REQUIRED", 0
                ),
                "p4_reference_required": p4_audit["classification_counts"].get(
                    "REFERENCE_REQUIRED", 0
                ),
                "p2_context_supported_not_required": p2_audit["classification_counts"].get(
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED", 0
                ),
                "p4_context_supported_not_required": p4_audit["classification_counts"].get(
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED", 0
                ),
                "p2_state_content_additions": state_lookup[("P2", query_id)][
                    "state_content_additions"
                ],
                "p4_state_content_additions": state_lookup[("P4", query_id)][
                    "state_content_additions"
                ],
                "p2_gold_transition": query["P2_transition"],
                "p4_gold_transition": query["P4_transition"],
            }
        )
    return rows


def select_cases(
    query_rows: Sequence[Mapping[str, Any]],
    state_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> tuple[list[str], dict[str, Any]]:
    p2_hurt = [str(row["query_id"]) for row in query_rows if row["P2_transition"] == "HURT"]
    p4_hurt = [str(row["query_id"]) for row in query_rows if row["P4_transition"] == "HURT"]
    p4_rescued_rows = [row for row in query_rows if row["P4_transition"] == "RESCUED"]
    p4_rescued_rows.sort(
        key=lambda row: (
            min(json.loads(str(row["P4_gold_ranks"]))) - min(json.loads(str(row["baseline_gold_ranks"]))),
            str(row["query_id"]),
        )
    )
    p4_rescued = [str(row["query_id"]) for row in p4_rescued_rows[:5]]
    p4_state = [
        query_id
        for (prompt, query_id), row in state_lookup.items()
        if prompt == "P4" and int(row["state_content_additions"]) > 0
    ]
    selected: list[str] = []
    for query_id in (*MANDATORY_CASE_IDS, *p2_hurt, *p4_hurt, *p4_rescued, *p4_state):
        if query_id not in selected:
            selected.append(query_id)
    metadata = {
        "mandatory": list(MANDATORY_CASE_IDS),
        "all_p2_hurt": p2_hurt,
        "all_p4_hurt": p4_hurt,
        "p4_rescued_max_5": p4_rescued,
        "all_p4_state_content_addition_cases": p4_state,
        "selected_case_ids": selected,
    }
    return selected, metadata


def build_cases(
    case_ids: Sequence[str],
    query_lookup: Mapping[str, Mapping[str, Any]],
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    audit_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
    state_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for query_id in case_ids:
        query = query_lookup[query_id]
        code = query_id[:4]
        pages = {str(page["page_id"]): page for page in snapshots[code]["pages"]}
        gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
        gold_set = set(gold_ids)
        variant_rankings = {
            variant: list(rankings[code][variant][query_id]) for variant in VARIANTS
        }
        maps = {variant: rank_map(rows) for variant, rows in variant_rankings.items()}
        detail_ids: list[str] = []
        for page_id in [
            *(
                str(item["page_id"])
                for variant in VARIANTS
                for item in variant_rankings[variant][:OUTPUT_K]
            ),
            *gold_ids,
        ]:
            if page_id not in detail_ids:
                detail_ids.append(page_id)
        cases.append(
            {
                "session_id": code,
                "query_id": query_id,
                "original_query": query["original_query"],
                "resolved_queries": {prompt: resolved[prompt][query_id] for prompt in PROMPTS},
                "actual_embedding_texts": {
                    "Baseline": query["original_query"],
                    **{prompt: resolved[prompt][query_id] for prompt in PROMPTS},
                },
                "rankings_top10": {
                    variant: top_rows(variant_rankings[variant], gold_set, DISPLAY_K)
                    for variant in VARIANTS
                },
                "gold_comparison": [
                    {
                        "gold_page_id": page_id,
                        **{
                            variant: {
                                "rank": int(maps[variant][page_id]["rank"]),
                                "score": float(maps[variant][page_id]["score"]),
                            }
                            for variant in VARIANTS
                        },
                    }
                    for page_id in gold_ids
                ],
                "resolution_audits": {
                    prompt: audit_lookup[(prompt, query_id)] for prompt in PROMPTS
                },
                "state_content_audits": {
                    prompt: state_lookup[(prompt, query_id)] for prompt in ("P2", "P4")
                },
                "page_details": [
                    {
                        "page_id": page_id,
                        "source_turn_id": pages[page_id]["source_turn_id"],
                        "is_gold": page_id in gold_set,
                        "ranks_and_scores": {
                            variant: {
                                "rank": int(maps[variant][page_id]["rank"]),
                                "score": float(maps[variant][page_id]["score"]),
                            }
                            for variant in VARIANTS
                        },
                        "production_p0_embedding_text": pages[page_id]["current_embedding_text"],
                    }
                    for page_id in detail_ids
                ],
            }
        )
    return cases


def fenced(value: Any, language: str = "text") -> list[str]:
    return [f"```{language}", str(value), "```", ""]


def ranking_lines(title: str, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    availability = f" (only {len(rows)} query-time visible Pages)" if len(rows) < DISPLAY_K else ""
    lines = [f"### {title} Top10{availability}", "", "```text"]
    for row in rows:
        marker = "✅ GOLD" if row["is_gold"] else "❌ NON-GOLD"
        lines.append(
            f"#{row['rank']} {row['page_id']} score={float(row['score']):.9f} "
            f"source_turn={row['source_turn_id']} {marker}"
        )
    lines.extend(("```", ""))
    return lines


def metric_cell(value: Any) -> str:
    return "N/A" if value is None or value == "" else f"{float(value):.9f}"


def write_report(
    path: Path,
    prompt_metrics: Sequence[Mapping[str, Any]],
    tradeoff: Mapping[str, Any],
    selection: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
) -> None:
    lines = [
        "# Identity-Only Slot-Bounded Reference Resolution Ablation",
        "",
        "## Frozen retrieval contract",
        "",
        "- Baseline/P0/P2/P4 each embeds exactly one final Query string.",
        "- P4 generation input: current Query + previous_3_qa only.",
        "- Page: frozen production P0 stored embedding; retrieval: per-Session dense cosine.",
        "",
        "## Aggregate metrics",
        "",
        "| Prompt | Changed | Micro R@5 | Macro R@5 | Promoted | Demoted | Net | Added precision | Unnecessary rate | Useful rate | Harmful rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in prompt_metrics:
        lines.append(
            f"| {row['prompt']} | {row['changed_query_count']} | {float(row['micro_r5']):.9f} | "
            f"{float(row['macro_session_r5']):.9f} | {row['promoted_gold']} | {row['demoted_gold']} | "
            f"{int(row['net_gold_gain']):+d} | {metric_cell(row['added_content_precision'])} | "
            f"{metric_cell(row['unnecessary_addition_rate'])} | {metric_cell(row['useful_change_rate'])} | "
            f"{metric_cell(row['harmful_change_rate'])} |"
        )
    lines.extend(("", "## P2 to P4 Gold trade-off", "", "```json", json.dumps(tradeoff, ensure_ascii=False, indent=2), "```", ""))
    lines.extend(("## Case selection", "", "```json", json.dumps(selection, ensure_ascii=False, indent=2), "```", ""))
    for case in cases:
        lines.extend((f"# {case['query_id']}", "", "### Original Query", "", *fenced(case["original_query"])))
        for prompt in PROMPTS:
            lines.extend((f"### {prompt} Resolved Query", "", *fenced(case["resolved_queries"][prompt])))
        for variant in VARIANTS:
            lines.extend((f"### {variant} actual embedding text", "", *fenced(case["actual_embedding_texts"][variant])))
        for variant in VARIANTS:
            lines.extend(ranking_lines(variant, case["rankings_top10"][variant]))
        lines.extend(("### All Gold ranks and scores", "", "```text"))
        for gold in case["gold_comparison"]:
            lines.append(f"Gold Page: {gold['gold_page_id']}")
            for variant in VARIANTS:
                lines.append(f"{variant}: #{gold[variant]['rank']} score={float(gold[variant]['score']):.9f}")
            lines.append("")
        lines.extend(("```", "", "### P2/P4 deterministic audits", ""))
        for prompt in ("P2", "P4"):
            lines.extend((f"#### {prompt} added canonical content", "", "```json"))
            lines.append(
                json.dumps(case["resolution_audits"][prompt]["added_content"], ensure_ascii=False, indent=2)
            )
            lines.extend(("```", "", f"#### {prompt} state content additions", "", "```json"))
            lines.append(
                json.dumps(case["state_content_audits"][prompt]["items"], ensure_ascii=False, indent=2)
            )
            lines.extend(("```", ""))
        lines.extend(("### Production P0 Page texts (Baseline/P0/P2/P4 Top5 union + all Gold)", ""))
        for page in case["page_details"]:
            lines.extend(
                (
                    f"#### Page {page['page_id']}",
                    "",
                    f"- Source Turn ID: `{page['source_turn_id']}`",
                    f"- Gold: `{'YES' if page['is_gold'] else 'NO'}`",
                )
            )
            for variant in VARIANTS:
                value = page["ranks_and_scores"][variant]
                lines.append(f"- {variant}: `#{value['rank']} / {float(value['score']):.9f}`")
            lines.extend(("", "完整 production P0 embedding text：", "", *fenced(page["production_p0_embedding_text"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def validate_reuse(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    prior = read_csv(PREVIOUS_RESULT_DIR / "gold_results.csv")
    prior_lookup = {(str(row["query_id"]), str(row["gold_page_id"])): row for row in prior}
    failures: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            maps = {variant: rank_map(rankings[code][variant][query_id]) for variant in ("Baseline", "P0", "P2")}
            for page_id in map(str, query["eligible_gold_page_ids"]):
                expected = prior_lookup[(query_id, page_id)]
                for variant, rank_column, score_column in (
                    ("Baseline", "baseline_rank", "baseline_score"),
                    ("P0", "P0_rank", "P0_score"),
                    ("P2", "P2_rank", "P2_score"),
                ):
                    actual = maps[variant][page_id]
                    if int(actual["rank"]) != int(expected[rank_column]) or not math.isclose(
                        float(actual["score"]), float(expected[score_column]), abs_tol=SCORE_ATOL
                    ):
                        failures.append(
                            {
                                "query_id": query_id,
                                "gold_page_id": page_id,
                                "variant": variant,
                                "actual_rank": actual["rank"],
                                "expected_rank": expected[rank_column],
                                "actual_score": actual["score"],
                                "expected_score": expected[score_column],
                            }
                        )
    validation = {
        "baseline_p0_p2_gold_rank_score_reuse": "PASS" if not failures else "FAIL",
        "all_pass": not failures,
        "score_atol": SCORE_ATOL,
        "failures": failures,
    }
    if failures:
        raise AssertionError(f"Baseline/P0/P2 reuse validation failed: {validation}")
    return validation


def validate_p4_unchanged(
    snapshots: Mapping[str, Mapping[str, Any]],
    p4_resolved: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    unchanged = 0
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            if p4_resolved[query_id] != str(query["original_query"]):
                continue
            unchanged += 1
            baseline = list(rankings[code]["Baseline"][query_id])
            p4 = list(rankings[code]["P4"][query_id])
            baseline_ids = [str(row["page_id"]) for row in baseline]
            p4_ids = [str(row["page_id"]) for row in p4]
            max_delta = max(
                (abs(float(left["score"]) - float(right["score"])) for left, right in zip(baseline, p4)),
                default=0.0,
            )
            if baseline_ids != p4_ids or max_delta > SCORE_ATOL:
                failures.append(
                    {
                        "query_id": query_id,
                        "same_page_order": baseline_ids == p4_ids,
                        "max_score_delta": max_delta,
                    }
                )
    validation = {
        "unchanged_query_count": unchanged,
        "p4_unchanged_full_ranking_identity": "PASS" if not failures else "FAIL",
        "all_pass": not failures,
        "score_atol": SCORE_ATOL,
        "failures": failures,
    }
    if failures:
        raise AssertionError(f"P4 unchanged ranking validation failed: {validation}")
    return validation


async def async_main(args: argparse.Namespace) -> None:
    args.output_dir = args.output_dir.resolve()
    p4_prompt, prompt_manifest = load_frozen_p4(args.output_dir)
    prompt_hash_before = sha256_bytes(args.output_dir / "prompts" / P4_PROMPT_FILE)
    snapshots = load_snapshots()
    prior_resolved, prior_query_rows = load_prior_queries(snapshots)
    p4_resolved, p4_llm_rows, p4_generation = await load_or_generate_p4(
        args, snapshots, p4_prompt
    )
    prompt_hash_after = sha256_bytes(args.output_dir / "prompts" / P4_PROMPT_FILE)
    if prompt_hash_before != prompt_hash_after:
        raise AssertionError("P4 Prompt changed during generation")
    resolved: dict[str, dict[str, str]] = {**prior_resolved, "P4": p4_resolved}
    query_lookup = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }

    reused_audits = load_reused_audits()
    p4_audits = build_p4_audits(snapshots, p4_resolved)
    audit_lookup: dict[tuple[str, str], dict[str, Any]] = {
        **reused_audits,
        **{("P4", str(row["query_id"])): row for row in p4_audits},
    }
    state_rows, state_lookup = build_state_audit(query_lookup, resolved, audit_lookup)
    resolution_rows, over_summary = summarize_audits(audit_lookup, state_lookup)

    vectors, embedding_cache, embedding_metadata = load_query_vectors(
        args.output_dir, snapshots, p4_resolved
    )
    rankings = rank_all(snapshots, vectors)
    reuse_validation = validate_reuse(snapshots, rankings)
    p4_unchanged_validation = validate_p4_unchanged(snapshots, p4_resolved, rankings)
    metrics = evaluate_all(snapshots, rankings)
    gold_rows, query_rows = build_gold_and_query_rows(snapshots, resolved, rankings)
    tradeoff = build_tradeoff(gold_rows)
    over_summary["p2_to_p4_gold_tradeoff"] = tradeoff
    prompt_metrics = build_prompt_metrics(
        snapshots,
        resolved,
        rankings,
        metrics,
        gold_rows,
        query_rows,
        over_summary["prompts"],
    )
    session_metrics = build_session_metrics(
        snapshots, resolved, metrics, gold_rows, query_rows
    )
    diff_rows = build_p2_p4_diff(query_rows, audit_lookup, state_lookup)
    case_ids, selection = select_cases(query_rows, state_lookup)
    cases = build_cases(
        case_ids,
        query_lookup,
        snapshots,
        resolved,
        rankings,
        audit_lookup,
        state_lookup,
    )

    write_csv(args.output_dir / "prompt_metrics.csv", prompt_metrics)
    write_csv(args.output_dir / "session_metrics.csv", session_metrics)
    write_csv(args.output_dir / "query_results.csv", query_rows)
    write_csv(args.output_dir / "gold_results.csv", gold_rows)
    write_csv(args.output_dir / "p2_vs_p4_query_diff.csv", diff_rows)
    write_csv(args.output_dir / "resolution_type_summary.csv", resolution_rows)
    write_csv(args.output_dir / "state_content_audit.csv", state_rows)
    write_jsonl(args.output_dir / "p4_resolution_audit.jsonl", p4_audits)
    dump_json(args.output_dir / "over_resolution_summary.json", over_summary)
    dump_json(
        args.output_dir / "representative_cases.json",
        {"selection": selection, "cases": cases},
    )
    write_report(
        args.output_dir / "representative_cases.md",
        prompt_metrics,
        tradeoff,
        selection,
        cases,
    )

    p4_query_output = []
    for query_id, query in query_lookup.items():
        llm_row = p4_llm_rows[query_id]
        p4_query_output.append(
            {
                "session_id": query_id[:4],
                "query_id": query_id,
                "original_query": query["original_query"],
                "p4_resolved_query": p4_resolved[query_id],
                "visible_context_turn_ids": [
                    str(item["turn_id"]) for item in query.get("previous_3_qa") or []
                ],
                "prompt_version": llm_row["prompt_version"],
                "prompt_hash": llm_row["prompt_hash"],
                "model": llm_row["model"],
                "thinking_mode": llm_row["thinking_mode"],
            }
        )
    write_jsonl(args.output_dir / "p4_resolved_queries.jsonl", p4_query_output)

    run_metadata = {
        "experiment": "reference_resolution_identity_only_ablation",
        "compared_variants": list(VARIANTS),
        "p1_rerun": False,
        "p3_rerun": False,
        "p5_generated": False,
        "full_session_rerun": False,
        "page_regeneration": False,
        "page_representation": "production P0: summary + Keywords + User",
        "page_embedding_source": "reused immutable snapshot stored_embedding",
        "page_embedding_reused": sum(len(snapshots[code]["pages"]) for code in SESSION_CODES),
        "embedding_model": PRODUCTION_EMBEDDING,
        "retrieval_method": "per-Session dense cosine",
        "embedding_contract": {
            "Baseline": "original_query only",
            "P0": "P0_resolved_query only",
            "P2": "P2_resolved_query only",
            "P4": "P4_resolved_query only",
        },
        "context_contract": "current query + previous_3_qa only",
        "forbidden_p4_generation_inputs": [
            "Gold",
            "required_context",
            "Gold Page",
            "Page text",
            "retrieval result",
            "retrieval score",
            "future dialogue",
            "benchmark label",
            "P0/P2 retrieval outcome",
            "audit classification",
        ],
        "p4_prompt_manifest": prompt_manifest,
        "p4_prompt_sha256_before_llm": prompt_hash_before,
        "p4_prompt_sha256_after_llm": prompt_hash_after,
        "p4_prompt_frozen_before_first_llm_call": True,
        "p4_prompt_adaptation_after_results": False,
        "reference_resolution_model": "deepseek-v4-flash",
        "thinking_mode": "disabled",
        "baseline_p0_p2_llm_calls": 0,
        "p0_query_cache_reused": len(prior_resolved["P0"]),
        "p2_query_cache_reused": len(prior_resolved["P2"]),
        "p4_generation": p4_generation,
        "embedding": embedding_metadata,
        "reuse_validation": reuse_validation,
        "p4_unchanged_validation": p4_unchanged_validation,
        "tradeoff": tradeoff,
        "snapshot_paths": {code: snapshots[code]["path"] for code in SESSION_CODES},
        "prior_query_artifact": str(PREVIOUS_RESULT_DIR / "resolved_queries.jsonl"),
        "prior_query_artifact_rows": len(prior_query_rows),
    }
    dump_json(args.output_dir / "run_metadata.json", run_metadata)
    embedding_cache.release(PRODUCTION_EMBEDDING)


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
