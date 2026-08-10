"""Run the frozen P0-P3 Reference Resolution prompt ablation on S001-S005.

P0 is reused from the completed conservative experiment. P1-P3 are generated
once from current Query + previous_3_qa only. Retrieval is an offline clean
replacement evaluation over immutable query-time snapshots and stored P0 Page
embeddings; no benchmark Session or Page is regenerated.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import re
import statistics
import time
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
    parse_llm_json,
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
from exp.benchmark.run_reference_resolution_query_diagnosis import (  # noqa: E402
    audit_variant as base_audit_variant,
)


SOURCE_RESULT_DIR = REPO_ROOT / "exp/results/reference_resolution_query_diagnosis"
CLEAN_RESULT_DIR = REPO_ROOT / "exp/results/query_rewrite_clean_replacement_ablation"
OUTPUT_DIR = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation"
PROMPT_DIR_NAME = "prompts"
PROMPTS = ("P0", "P1", "P2", "P3")
GENERATED_PROMPTS = ("P1", "P2", "P3")
PROMPT_FILES = {
    "P0": "P0_current.txt",
    "P1": "P1_explicit_reference.txt",
    "P2": "P2_slot_bounded.txt",
    "P3": "P3_one_hop_dependency.txt",
}
PROMPT_VERSIONS = {
    "P0": "reference-resolution-v1",
    "P1": "reference-resolution-prompt-ablation-p1-v1",
    "P2": "reference-resolution-prompt-ablation-p2-v1",
    "P3": "reference-resolution-prompt-ablation-p3-v1",
}
MANDATORY_CASE_IDS = ("S001-Q033", "S003-Q044", "S004-Q052", "S004-Q063", "S005-Q042")
RESOLUTION_TYPES = (
    "SUBJECT",
    "INDICATOR",
    "COMPARISON_OBJECT",
    "PRIOR_JUDGMENT",
    "EVIDENCE_OBJECT",
    "TIME",
    "OTHER",
)
CLASSIFICATIONS = (
    "REFERENCE_REQUIRED",
    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED",
    "UNSUPPORTED",
)
SEMANTIC_OBJECT_PATTERNS = (
    ("EVIDENCE_RAW_DISCLOSURE", "原始披露", "EVIDENCE_OBJECT", r"三层证据|证据分级|披露"),
    ("EVIDENCE_DERIVED_CALCULATION", "派生计算", "EVIDENCE_OBJECT", r"三层证据|证据分级|计算"),
    ("EVIDENCE_ANALYTICAL_JUDGMENT", "分析判断", "EVIDENCE_OBJECT", r"三层证据|证据分级|判断"),
    ("EVIDENCE_FACT_LAYER", "事实层", "EVIDENCE_OBJECT", r"三层证据|证据分级|证据"),
    ("EVIDENCE_CALCULATION_LAYER", "计算层", "EVIDENCE_OBJECT", r"三层证据|证据分级|证据"),
    ("EVIDENCE_JUDGMENT_LAYER", "判断层", "EVIDENCE_OBJECT", r"三层证据|证据分级|证据"),
    ("PRIOR_PROFIT_QUALITY_JUDGMENT", "盈利质量判断", "PRIOR_JUDGMENT", r"判断|结论"),
    ("PRIOR_EFFICIENCY_JUDGMENT", "效率判断", "PRIOR_JUDGMENT", r"判断|结论"),
    ("PRIOR_CASH_MISMATCH_JUDGMENT", "现金错位判断", "PRIOR_JUDGMENT", r"判断|结论"),
    ("PRIOR_RISK_RANKING", "风险排序", "PRIOR_JUDGMENT", r"风险|排序|判断|结论"),
    ("EVIDENCE_GRADING", "证据分级", "EVIDENCE_OBJECT", r"证据|三层"),
    ("EVIDENCE_RISK_CARD", "风险议题卡核心内容", "EVIDENCE_OBJECT", r"风险点|证据|这条|前面"),
    (
        "EVIDENCE_INVESTOR_QA_DRAFT",
        "投资者问答底稿核心内容",
        "EVIDENCE_OBJECT",
        r"证据|这条|前面|底稿",
    ),
)
OUTPUT_K = 5
DISPLAY_K = 10
SCORE_ATOL = 1e-6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen Reference Resolution prompt ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, Any]]:
    prompt_dir = output_dir / PROMPT_DIR_NAME
    manifest = load_json(prompt_dir / "prompt_sha256.json")
    prompts: dict[str, str] = {}
    for prompt in PROMPTS:
        expected_file = PROMPT_FILES[prompt]
        manifest_row = manifest.get(prompt) or {}
        if manifest_row.get("file") != expected_file:
            raise ValueError(f"Frozen Prompt file mismatch: {prompt}")
        path = prompt_dir / expected_file
        actual_hash = sha256_bytes(path)
        if actual_hash != manifest_row.get("sha256"):
            raise ValueError(f"Frozen Prompt SHA256 mismatch: {prompt}")
        prompts[prompt] = path.read_text(encoding="utf-8")
    prior_p0 = load_json(SOURCE_RESULT_DIR / "run_metadata.json")["frozen_system_prompt"]
    if prompts["P0"] != prior_p0:
        raise ValueError("P0 Prompt is not byte/text identical to reference-resolution-v1")
    if not manifest.get("frozen_before_first_llm_call"):
        raise ValueError("Prompt manifest does not assert pre-call freezing")
    return prompts, manifest


def load_p0_queries(
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    rows = load_jsonl(SOURCE_RESULT_DIR / "reference_resolution_queries.jsonl")
    by_query = {str(row["query_id"]): row for row in rows}
    expected = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    if len(rows) != 99 or set(by_query) != set(expected):
        raise ValueError(f"P0 coverage mismatch: rows={len(rows)} unique={len(by_query)}")
    for query_id, query in expected.items():
        if str(by_query[query_id]["original_query"]) != str(query["original_query"]):
            raise ValueError(f"P0 source original Query mismatch: {query_id}")
    resolved = {
        query_id: str(row["conservative_reference_resolution"]) for query_id, row in by_query.items()
    }
    return resolved, by_query


def resolved_validator(value: Mapping[str, Any]) -> dict[str, str]:
    text = str(value.get("resolved_query") or "").strip()
    if not text:
        raise ValueError("resolved_query is empty")
    return {"resolved_query": text}


class PlainTextJsonlLLMCache(AsyncJsonlLLMCache):
    """Keep the frozen messages unchanged and validate ordinary text as strict JSON.

    DeepSeek's OpenAI-compatible endpoint rejects ``json_object`` mode unless a
    literal Latin ``json`` token appears in the messages. The frozen Chinese
    P1-P3 Prompts specify the JSON object structurally but intentionally are not
    mutated to satisfy that transport-level precondition.
    """

    async def call(
        self,
        *,
        query_id: str,
        variant: str,
        messages: Sequence[Mapping[str, str]],
        prompt_version: str,
        max_tokens: int,
        validator,
        extra_key: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt_hash = stable_hash({"version": prompt_version, "messages": list(messages)})
        cache_key = stable_hash(
            {
                "query_id": query_id,
                "variant": variant,
                "model": self.model,
                "thinking_mode": "disabled",
                "prompt_hash": prompt_hash,
                "extra": dict(extra_key or {}),
            }
        )
        cached = self.success_by_key.get(cache_key)
        if cached:
            return cached
        errors: list[str] = []
        retries_used = 0
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
                            extra_body={"thinking": {"type": "disabled"}},
                        ),
                        timeout=self.timeout,
                    )
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    content = response.choices[0].message.content or ""
                    parsed = validator(parse_llm_json(content))
                    usage = response.usage
                    row = {
                        "cache_key": cache_key,
                        "query_id": query_id,
                        "variant": variant,
                        "model": self.model,
                        "thinking_mode": "disabled",
                        "prompt_version": prompt_version,
                        "prompt_hash": prompt_hash,
                        "response_mode": "plain_text_parsed_as_json",
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": content,
                        "llm_latency_ms": latency_ms,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "retry_count": retries_used,
                        "errors": errors,
                    }
                    await self.append(row)
                    self.success_by_key[cache_key] = row
                    return row
                except Exception as exc:
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    retries_used += int(attempt < self.retries)
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12.0))
        row = {
            "cache_key": cache_key,
            "query_id": query_id,
            "variant": variant,
            "model": self.model,
            "thinking_mode": "disabled",
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "response_mode": "plain_text_parsed_as_json",
            "status": "FAILED",
            "parsed": None,
            "llm_latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "retry_count": retries_used,
            "errors": errors,
        }
        await self.append(row)
        return row


def resolution_messages(query: Mapping[str, Any], system_prompt: str) -> list[dict[str, str]]:
    history = query.get("previous_3_qa") or []
    context = "\n\n".join(
        f"[{item['turn_id']}] User: {item['user']}\nAssistant: {item['assistant']}" for item in history
    )
    user = (
        "Visible previous conversation (chronological, at most 3 QAs):\n"
        f"{context}\n\nCURRENT query:\n{query['original_query']}"
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


async def load_or_generate_prompt_queries(
    args: argparse.Namespace,
    snapshots: Mapping[str, Mapping[str, Any]],
    prompts: Mapping[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for P1/P2/P3")
    model = str(llm_config.get("model") or "deepseek-chat")
    if model != "deepseek-v4-flash":
        raise ValueError(f"Frozen model must be deepseek-v4-flash, got {model}")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    cache_path = args.output_dir / "cache/prompt_resolution_llm.jsonl"
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
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]

    async def one(prompt: str, query: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant=prompt,
            messages=resolution_messages(query, prompts[prompt]),
            prompt_version=PROMPT_VERSIONS[prompt],
            max_tokens=800,
            validator=resolved_validator,
        )
        return prompt, str(query["query_id"]), row

    try:
        results = await asyncio.gather(
            *(one(prompt, query) for prompt in GENERATED_PROMPTS for query in all_queries)
        )
    finally:
        await client.close()
    failures = [
        {"prompt": prompt, "query_id": query_id, "errors": row.get("errors")}
        for prompt, query_id, row in results
        if row.get("status") != "SUCCESS"
    ]
    if failures:
        dump_json(args.output_dir / "prompt_resolution_failures.json", failures)
        raise RuntimeError(f"P1/P2/P3 generation failed for {len(failures)} Prompt-Queries")

    resolved: dict[str, dict[str, str]] = {prompt: {} for prompt in GENERATED_PROMPTS}
    rows_by_prompt: dict[str, dict[str, dict[str, Any]]] = {
        prompt: {} for prompt in GENERATED_PROMPTS
    }
    for prompt, query_id, row in results:
        resolved[prompt][query_id] = str(row["parsed"]["resolved_query"])
        rows_by_prompt[prompt][query_id] = row
    for prompt in GENERATED_PROMPTS:
        if len(resolved[prompt]) != 99:
            raise ValueError(f"{prompt} successful coverage is {len(resolved[prompt])}, expected 99")

    durable_rows = load_jsonl(cache_path)
    generation_metadata: dict[str, Any] = {
        "model": model,
        "thinking_mode": "disabled",
        "temperature": 0.0,
        "response_mode": "plain_text parsed and strictly validated as a JSON object",
        "cache_path": str(cache_path),
        "per_prompt": {
            "P0": {
                "query_count": 99,
                "successful_model_output_count": 0,
                "successful_row_api_attempt_count": 0,
                "new_llm_api_attempt_count": 0,
                "provider_precondition_rejected_attempt_count": 0,
            }
        },
    }
    for prompt in GENERATED_PROMPTS:
        matching = [
            row
            for row in durable_rows
            if row.get("variant") == prompt
            and row.get("prompt_version") == PROMPT_VERSIONS[prompt]
            and row.get("model") == model
            and row.get("thinking_mode") == "disabled"
        ]
        successful = [row for row in matching if row.get("status") == "SUCCESS"]
        attempts = sum(1 + int(row.get("retry_count") or 0) for row in matching)
        successful_row_attempts = sum(
            1 + int(row.get("retry_count") or 0) for row in successful
        )
        rejected_rows = [
            row
            for row in matching
            if row.get("status") == "FAILED"
            and any(
                "Prompt must contain the word 'json'" in str(error)
                for error in row.get("errors") or []
            )
        ]
        precondition_rejected_attempts = sum(
            1 + int(row.get("retry_count") or 0) for row in rejected_rows
        )
        current = list(rows_by_prompt[prompt].values())
        generation_metadata["per_prompt"][prompt] = {
            "query_count": len(successful),
            "successful_model_output_count": len(successful),
            "successful_row_api_attempt_count": successful_row_attempts,
            "new_llm_api_attempt_count": attempts,
            "provider_precondition_rejected_attempt_count": precondition_rejected_attempts,
            "durable_success_rows": len(successful),
            "durable_failed_rows": len(matching) - len(successful),
            "current_run_cache_hits": sum(str(row["cache_key"]) in initial_keys for row in current),
            "current_run_cache_misses": sum(str(row["cache_key"]) not in initial_keys for row in current),
            "prompt_version": PROMPT_VERSIONS[prompt],
        }
    generation_metadata["new_llm_api_attempt_count"] = sum(
        generation_metadata["per_prompt"][prompt]["new_llm_api_attempt_count"]
        for prompt in GENERATED_PROMPTS
    )
    return resolved, rows_by_prompt, generation_metadata


def load_query_vectors(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, list[float]], EmbeddingCache, dict[str, Any]]:
    vectors: dict[str, list[float]] = {}
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
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
    for query in all_queries:
        query_id = str(query["query_id"])
        vectors[f"BASE:{query_id}"] = baseline[f"Q0:{query_id}:0"]

    p0_ids = [f"QREFONLY:{query['query_id']}:0" for query in all_queries]
    p0_vectors = load_existing_embedding_vectors(
        CLEAN_RESULT_DIR,
        model_name=PRODUCTION_EMBEDDING,
        prefix="conservative-reference-resolution-only-",
        required_ids=p0_ids,
    )
    for query in all_queries:
        query_id = str(query["query_id"])
        vectors[f"P0:{query_id}"] = p0_vectors[f"QREFONLY:{query_id}:0"]

    cache = EmbeddingCache(output_dir / "cache/embeddings")
    embedding_metadata: dict[str, Any] = {
        "baseline_embedding_cache_reused": 99,
        "p0_embedding_cache_reused": 99,
        "per_prompt": {"P0": {"cache_reused": 99, "new_embedding_count": 0}},
    }
    for prompt in GENERATED_PROMPTS:
        ids = [f"{prompt}:{query['query_id']}:0" for query in all_queries]
        texts = [resolved[prompt][str(query["query_id"])] for query in all_queries]
        encoded, metadata = cache.encode(
            PRODUCTION_EMBEDDING,
            f"{prompt}-resolved-query-only-S001-S005",
            ids,
            texts,
            measure_individual=True,
        )
        vectors.update(encoded)
        embedding_metadata["per_prompt"][prompt] = {
            "cache_hit": bool(metadata.get("cache_hit")),
            "new_embedding_count": 0 if metadata.get("cache_hit") else len(ids),
            "batch": metadata,
        }
    embedding_metadata["new_embedding_count"] = sum(
        int(embedding_metadata["per_prompt"][prompt]["new_embedding_count"])
        for prompt in GENERATED_PROMPTS
    )
    return vectors, cache, embedding_metadata


def rank_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    vectors: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    result: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for code in SESSION_CODES:
        snapshot = snapshots[code]
        pages = {str(page["page_id"]): page for page in snapshot["pages"]}
        page_vectors = {page_id: page["stored_embedding"] for page_id, page in pages.items()}
        visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
        result[code] = {variant: {} for variant in ("Baseline", *PROMPTS)}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            visible_pages = [pages[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
            result[code]["Baseline"][query_id] = cosine_rank(
                vectors[f"BASE:{query_id}"], visible_pages, page_vectors
            )
            for prompt in PROMPTS:
                vector_key = f"{prompt}:{query_id}" if prompt == "P0" else f"{prompt}:{query_id}:0"
                result[code][prompt][query_id] = cosine_rank(
                    vectors[vector_key], visible_pages, page_vectors
                )
    return result


def validate_clean_reuse(
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    clean_rows = read_csv(CLEAN_RESULT_DIR / "query_transition.csv")
    by_gold = {
        (str(row["query_id"]), str(row["gold_page_id"])): row for row in clean_rows
    }
    rank_failures: list[dict[str, Any]] = []
    score_failures: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        clean_session = next(
            row for row in read_csv(CLEAN_RESULT_DIR / "session_summary.csv") if row["session_id"] == code
        )
        metrics_by_variant = {}
        snapshot_queries = load_snapshots()[code]["queries"]
        for variant in ("Baseline", "P0"):
            metrics_by_variant[variant], _ = evaluate_rankings(
                snapshot_queries, rankings[code][variant]
            )
        if not math.isclose(
            float(metrics_by_variant["Baseline"]["recall_at_5"]),
            float(clean_session["baseline_r5"]),
            abs_tol=1e-12,
        ):
            rank_failures.append({"session_id": code, "variant": "Baseline", "metric": "R@5"})
        if not math.isclose(
            float(metrics_by_variant["P0"]["recall_at_5"]),
            float(clean_session["reference_resolution_only_r5"]),
            abs_tol=1e-12,
        ):
            rank_failures.append({"session_id": code, "variant": "P0", "metric": "R@5"})
        for query in snapshot_queries:
            query_id = str(query["query_id"])
            maps = {variant: rank_map(rankings[code][variant][query_id]) for variant in ("Baseline", "P0")}
            for page_id in map(str, query["eligible_gold_page_ids"]):
                expected = by_gold[(query_id, page_id)]
                for variant, rank_column, score_column in (
                    ("Baseline", "baseline_rank", "baseline_score"),
                    ("P0", "reference_resolution_only_rank", "reference_resolution_only_score"),
                ):
                    actual = maps[variant][page_id]
                    if int(actual["rank"]) != int(expected[rank_column]):
                        rank_failures.append(
                            {
                                "query_id": query_id,
                                "page_id": page_id,
                                "variant": variant,
                                "actual": actual["rank"],
                                "expected": expected[rank_column],
                            }
                        )
                    if not math.isclose(
                        float(actual["score"]), float(expected[score_column]), abs_tol=SCORE_ATOL
                    ):
                        score_failures.append(
                            {
                                "query_id": query_id,
                                "page_id": page_id,
                                "variant": variant,
                                "actual": actual["score"],
                                "expected": expected[score_column],
                            }
                        )
    validation = {
        "baseline_and_p0_clean_metric_reproduction": "PASS" if not rank_failures else "FAIL",
        "baseline_and_p0_gold_score_reproduction": "PASS" if not score_failures else "FAIL",
        "all_pass": not rank_failures and not score_failures,
        "rank_failures": rank_failures,
        "score_failures": score_failures,
    }
    if not validation["all_pass"]:
        raise AssertionError(f"Clean ablation reuse validation failed: {validation}")
    return validation


def resolution_type(original: str, raw_category: str) -> str:
    evidence_reference = bool(re.search(r"反例|证据|证据链|三层证据|风险点", original))
    judgment_reference = bool(re.search(r"前面的判断|刚才的判断|上一轮.*结论|前面的结论|主结论", original))
    comparison_reference = bool(re.search(r"两个指标|两项指标|这两个|这两项|两者|二者", original))
    if raw_category == "subject":
        return "SUBJECT"
    if raw_category == "time":
        return "TIME"
    if raw_category in {"task_action", "constraint"}:
        return "OTHER"
    if evidence_reference and raw_category in {"indicator", "comparison_object", "historical_conclusion"}:
        return "EVIDENCE_OBJECT"
    if judgment_reference and raw_category in {"indicator", "comparison_object", "historical_conclusion"}:
        return "PRIOR_JUDGMENT"
    if raw_category == "comparison_object" or (
        comparison_reference and raw_category == "indicator"
    ):
        return "COMPARISON_OBJECT"
    if raw_category == "historical_conclusion":
        return "PRIOR_JUDGMENT"
    if raw_category == "indicator":
        return "INDICATOR"
    return "OTHER"


def normalized_classification(
    original: str,
    item: Mapping[str, Any],
) -> str:
    classification = str(item["classification"])
    if classification != "REFERENCE_REQUIRED":
        return classification
    if item["category"] == "subject" and not re.search(
        r"它|该公司|这家公司|这个主体|该主体|其公司", original
    ):
        return "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
    if item["category"] == "time" and str(item["canonical"]).startswith("YEAR_") and not re.search(
        r"当年|那一年|这个年度|该年度|上一年|前一年|后一年度", original
    ):
        return "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
    return classification


def context_text(query: Mapping[str, Any]) -> str:
    return "\n\n".join(
        f"[{item['turn_id']}] User: {item['user']}\nAssistant: {item['assistant']}"
        for item in query.get("previous_3_qa") or []
    )


def semantic_object_additions(
    query: Mapping[str, Any],
    resolved_query: str,
) -> list[dict[str, Any]]:
    original = str(query["original_query"])
    visible = context_text(query)
    additions: list[dict[str, Any]] = []
    for canonical, surface, item_type, required_cue in SEMANTIC_OBJECT_PATTERNS:
        if surface in original or surface not in resolved_query:
            continue
        supported = surface in visible
        required = supported and bool(re.search(required_cue, original))
        classification = (
            "REFERENCE_REQUIRED"
            if required
            else "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            if supported
            else "UNSUPPORTED"
        )
        index = visible.find(surface)
        evidence = (
            visible[max(0, index - 70) : min(len(visible), index + len(surface) + 70)]
            if index >= 0
            else None
        )
        additions.append(
            {
                "category": "semantic_object",
                "canonical": canonical,
                "surface_text": surface,
                "classification": classification,
                "context_supported": supported,
                "reference_required": required,
                "evidence_snippets": [evidence] if evidence else [],
                "raw_category": "semantic_object",
                "resolution_type": item_type,
            }
        )
    return additions


def build_audits(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            original = str(query["original_query"])
            for prompt in PROMPTS:
                base = base_audit_variant(query, prompt, resolved[prompt][query_id])
                semantic_additions = semantic_object_additions(query, resolved[prompt][query_id])
                semantic_surfaces = {item["surface_text"] for item in semantic_additions}
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
                additions.extend(semantic_additions)
                rows.append(
                    {
                        "session_id": code,
                        "query_id": query_id,
                        "prompt": prompt,
                        "original_query": original,
                        "resolved_query": resolved[prompt][query_id],
                        "changed": resolved[prompt][query_id] != original,
                        "visible_context_turn_ids": [
                            str(item["turn_id"]) for item in query.get("previous_3_qa") or []
                        ],
                        "audit_scope": "current query + previous_3_qa only; no Gold/Page/ranking",
                        "total_added_canonical_content": len(additions),
                        "classification_counts": dict(
                            Counter(item["classification"] for item in additions)
                        ),
                        "resolution_type_counts": dict(
                            Counter(item["resolution_type"] for item in additions)
                        ),
                        "added_content": additions,
                    }
                )
    return rows


def build_resolution_summaries(
    audits: Sequence[Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    snapshots: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    csv_rows: list[dict[str, Any]] = []
    prompt_json: dict[str, Any] = {}
    for prompt in PROMPTS:
        prompt_audits = [row for row in audits if row["prompt"] == prompt]
        additions = [item for row in prompt_audits for item in row["added_content"]]
        classifications = Counter(item["classification"] for item in additions)
        types = Counter(item["resolution_type"] for item in additions)
        by_type: dict[str, Any] = {}
        for item_type in RESOLUTION_TYPES:
            scoped = [item for item in additions if item["resolution_type"] == item_type]
            scoped_classifications = Counter(item["classification"] for item in scoped)
            row = {
                "prompt": prompt,
                "resolution_type": item_type,
                "total_added_canonical_content": len(scoped),
                "reference_required": scoped_classifications["REFERENCE_REQUIRED"],
                "context_supported_but_not_required": scoped_classifications[
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                ],
                "unsupported": scoped_classifications["UNSUPPORTED"],
            }
            csv_rows.append(row)
            by_type[item_type] = row
        csv_rows.append(
            {
                "prompt": prompt,
                "resolution_type": "ALL",
                "total_added_canonical_content": len(additions),
                "reference_required": classifications["REFERENCE_REQUIRED"],
                "context_supported_but_not_required": classifications[
                    "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
                ],
                "unsupported": classifications["UNSUPPORTED"],
            }
        )
        changed_count = sum(
            resolved[prompt][str(query["query_id"])] != str(query["original_query"])
            for query in all_queries
        )
        prompt_json[prompt] = {
            "query_count": len(all_queries),
            "changed_query_count": changed_count,
            "unchanged_query_count": len(all_queries) - changed_count,
            "total_added_canonical_content": len(additions),
            "average_added_canonical_content_per_query": len(additions) / len(all_queries),
            "REFERENCE_REQUIRED": classifications["REFERENCE_REQUIRED"],
            "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED": classifications[
                "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED"
            ],
            "UNSUPPORTED": classifications["UNSUPPORTED"],
            "by_resolution_type": dict(types),
            "by_resolution_type_and_classification": by_type,
        }
    prior = load_json(SOURCE_RESULT_DIR / "over_resolution_summary.json")["variants"][
        "reference_resolution"
    ]
    p0 = prompt_json["P0"]
    expected = {
        "total_added_canonical_content": prior["total_added_entities"],
        "REFERENCE_REQUIRED": prior["REFERENCE_REQUIRED_added_count"],
        "CONTEXT_SUPPORTED_BUT_NOT_REQUIRED": prior["CONTEXT_SUPPORTED_BUT_NOT_REQUIRED_count"],
        "UNSUPPORTED": prior["UNSUPPORTED_count"],
    }
    actual = {key: p0[key] for key in expected}
    if actual != expected:
        raise AssertionError(f"P0 deterministic audit did not reproduce: actual={actual}, expected={expected}")
    summary = {
        "audit_method": (
            "Deterministic canonical-content diff using the frozen current-query + previous_3_qa audit. "
            "No Gold, Page text, retrieval result, score, future dialogue, or benchmark label is used."
        ),
        "resolution_types": list(RESOLUTION_TYPES),
        "classifications": list(CLASSIFICATIONS),
        "prompts": prompt_json,
        "p0_prior_audit_reproduction": "PASS",
    }
    return csv_rows, summary


def evaluate_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for code in SESSION_CODES:
        result[code] = {}
        for variant in ("Baseline", *PROMPTS):
            metrics, _ = evaluate_rankings(snapshots[code]["queries"], rankings[code][variant])
            result[code][variant] = metrics
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    result["Aggregate"] = {}
    for variant in ("Baseline", *PROMPTS):
        pooled = {
            query_id: ranking
            for code in SESSION_CODES
            for query_id, ranking in rankings[code][variant].items()
        }
        metrics, _ = evaluate_rankings(all_queries, pooled)
        metrics["macro_session_r5"] = statistics.fmean(
            float(result[code][variant]["recall_at_5"]) for code in SESSION_CODES
        )
        result["Aggregate"][variant] = metrics
    return result


def transition(baseline_hit: bool, prompt_hit: bool) -> str:
    if not baseline_hit and prompt_hit:
        return "RESCUED"
    if baseline_hit and not prompt_hit:
        return "HURT"
    if baseline_hit:
        return "UNCHANGED_HIT"
    return "UNCHANGED_MISS"


def build_gold_and_query_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    audits: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    audit_lookup = {(str(row["prompt"]), str(row["query_id"])): row for row in audits}
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    change_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            original = str(query["original_query"])
            gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
            maps = {
                variant: rank_map(rankings[code][variant][query_id])
                for variant in ("Baseline", *PROMPTS)
            }
            hits = {
                variant: any(int(maps[variant][page_id]["rank"]) <= OUTPUT_K for page_id in gold_ids)
                for variant in ("Baseline", *PROMPTS)
            }
            change_rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    **{f"{prompt}_changed": resolved[prompt][query_id] != original for prompt in PROMPTS},
                }
            )
            query_row: dict[str, Any] = {
                "session_id": code,
                "query_id": query_id,
                "original_query": original,
                "gold_page_ids": json.dumps(gold_ids, ensure_ascii=False),
                "baseline_hit5": hits["Baseline"],
                "baseline_gold_ranks": json.dumps(
                    [int(maps["Baseline"][page_id]["rank"]) for page_id in gold_ids]
                ),
                "baseline_gold_scores": json.dumps(
                    [float(maps["Baseline"][page_id]["score"]) for page_id in gold_ids]
                ),
            }
            for prompt in PROMPTS:
                prompt_ranks = [int(maps[prompt][page_id]["rank"]) for page_id in gold_ids]
                prompt_scores = [float(maps[prompt][page_id]["score"]) for page_id in gold_ids]
                audit = audit_lookup[(prompt, query_id)]
                query_row.update(
                    {
                        f"{prompt}_resolved_query": resolved[prompt][query_id],
                        f"{prompt}_actual_embedding_text": resolved[prompt][query_id],
                        f"{prompt}_changed": resolved[prompt][query_id] != original,
                        f"{prompt}_hit5": hits[prompt],
                        f"{prompt}_transition": transition(hits["Baseline"], hits[prompt]),
                        f"{prompt}_gold_ranks": json.dumps(prompt_ranks),
                        f"{prompt}_gold_scores": json.dumps(prompt_scores),
                        f"{prompt}_added_canonical_content": audit["total_added_canonical_content"],
                        f"{prompt}_reference_required": audit["classification_counts"].get(
                            "REFERENCE_REQUIRED", 0
                        ),
                        f"{prompt}_context_supported_but_not_required": audit[
                            "classification_counts"
                        ].get("CONTEXT_SUPPORTED_BUT_NOT_REQUIRED", 0),
                        f"{prompt}_unsupported": audit["classification_counts"].get("UNSUPPORTED", 0),
                    }
                )
            query_rows.append(query_row)
            for page_id in gold_ids:
                row: dict[str, Any] = {
                    "session_id": code,
                    "query_id": query_id,
                    "gold_page_id": page_id,
                    "baseline_rank": int(maps["Baseline"][page_id]["rank"]),
                    "baseline_score": float(maps["Baseline"][page_id]["score"]),
                }
                for prompt in PROMPTS:
                    prompt_rank = int(maps[prompt][page_id]["rank"])
                    baseline_rank = int(row["baseline_rank"])
                    row.update(
                        {
                            f"{prompt}_rank": prompt_rank,
                            f"{prompt}_score": float(maps[prompt][page_id]["score"]),
                            f"{prompt}_promoted": baseline_rank > OUTPUT_K and prompt_rank <= OUTPUT_K,
                            f"{prompt}_demoted": baseline_rank <= OUTPUT_K and prompt_rank > OUTPUT_K,
                            f"{prompt}_rank_movement": baseline_rank - prompt_rank,
                        }
                    )
                gold_rows.append(row)
    return gold_rows, query_rows, change_rows


def prompt_movement_counts(
    prompt: str,
    gold_rows: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    promoted = sum(bool(row[f"{prompt}_promoted"]) for row in gold_rows)
    demoted = sum(bool(row[f"{prompt}_demoted"]) for row in gold_rows)
    return {"promoted_gold": promoted, "demoted_gold": demoted, "net_gold_gain": promoted - demoted}


def build_prompt_metrics(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    metrics: Mapping[str, Mapping[str, Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    result: list[dict[str, Any]] = []
    for prompt in PROMPTS:
        changed_queries = [
            query
            for query in all_queries
            if resolved[prompt][str(query["query_id"])] != str(query["original_query"])
        ]
        changed_ids = {str(query["query_id"]) for query in changed_queries}
        changed_gold = [row for row in gold_rows if str(row["query_id"]) in changed_ids]
        changed_baseline_rankings = {
            query_id: rankings[query_id[:4]]["Baseline"][query_id] for query_id in changed_ids
        }
        changed_prompt_rankings = {
            query_id: rankings[query_id[:4]][prompt][query_id] for query_id in changed_ids
        }
        baseline_changed, _ = evaluate_rankings(changed_queries, changed_baseline_rankings)
        prompt_changed, _ = evaluate_rankings(changed_queries, changed_prompt_rankings)
        counts = prompt_movement_counts(prompt, gold_rows)
        changed_counts = prompt_movement_counts(prompt, changed_gold)
        useful = 0
        harmful = 0
        for query_id in changed_ids:
            scoped = [row for row in changed_gold if row["query_id"] == query_id]
            has_promotion = any(bool(row[f"{prompt}_promoted"]) for row in scoped)
            has_demotion = any(bool(row[f"{prompt}_demoted"]) for row in scoped)
            useful += int(has_promotion and not has_demotion)
            harmful += int(has_demotion)
        aggregate = metrics["Aggregate"][prompt]
        aggregate_baseline = metrics["Aggregate"]["Baseline"]
        result.append(
            {
                "prompt": prompt,
                "changed_query_count": len(changed_queries),
                "unchanged_query_count": len(all_queries) - len(changed_queries),
                "eligible_gold_count": aggregate["eligible_gold_count"],
                "baseline_micro_r5": aggregate_baseline["recall_at_5"],
                "micro_r5": aggregate["recall_at_5"],
                "macro_session_r5": aggregate["macro_session_r5"],
                "r10": aggregate["recall_at_10"],
                "r20": aggregate["recall_at_20"],
                "mrr": aggregate["mrr"],
                "mean_gold_rank": aggregate["mean_gold_rank"],
                **counts,
                "rescued_query_count": sum(row[f"{prompt}_transition"] == "RESCUED" for row in query_rows),
                "hurt_query_count": sum(row[f"{prompt}_transition"] == "HURT" for row in query_rows),
                "extreme_demotion_top5_to_beyond20": sum(
                    int(row["baseline_rank"]) <= 5 and int(row[f"{prompt}_rank"]) > 20
                    for row in gold_rows
                ),
                "extreme_promotion_beyond20_to_top5": sum(
                    int(row["baseline_rank"]) > 20 and int(row[f"{prompt}_rank"]) <= 5
                    for row in gold_rows
                ),
                "changed_eligible_gold_count": baseline_changed["eligible_gold_count"],
                "changed_baseline_r5": baseline_changed["recall_at_5"],
                "changed_variant_r5": prompt_changed["recall_at_5"],
                "changed_delta_r5": float(prompt_changed["recall_at_5"])
                - float(baseline_changed["recall_at_5"]),
                "changed_promoted_gold": changed_counts["promoted_gold"],
                "changed_demoted_gold": changed_counts["demoted_gold"],
                "changed_net_gold_gain": changed_counts["net_gold_gain"],
                "useful_changed_query_count": useful,
                "useful_change_rate": useful / len(changed_queries),
                "harmful_changed_query_count": harmful,
                "harmful_change_rate": harmful / len(changed_queries),
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
        snapshot_queries = snapshots[code]["queries"]
        for prompt in PROMPTS:
            counts = prompt_movement_counts(prompt, scoped_gold)
            variant = metrics[code][prompt]
            baseline = metrics[code]["Baseline"]
            changed = sum(
                resolved[prompt][str(query["query_id"])] != str(query["original_query"])
                for query in snapshot_queries
            )
            result.append(
                {
                    "session_id": code,
                    "prompt": prompt,
                    "query_count": len(snapshot_queries),
                    "changed_query_count": changed,
                    "eligible_gold_count": baseline["eligible_gold_count"],
                    "baseline_r5": baseline["recall_at_5"],
                    "variant_r5": variant["recall_at_5"],
                    "delta_r5": float(variant["recall_at_5"]) - float(baseline["recall_at_5"]),
                    "r10": variant["recall_at_10"],
                    "r20": variant["recall_at_20"],
                    "mrr": variant["mrr"],
                    "mean_gold_rank": variant["mean_gold_rank"],
                    **counts,
                    "rescued_query_count": sum(
                        row[f"{prompt}_transition"] == "RESCUED" for row in scoped_queries
                    ),
                    "hurt_query_count": sum(
                        row[f"{prompt}_transition"] == "HURT" for row in scoped_queries
                    ),
                }
            )
    return result


def build_cases(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    audits: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    query_lookup = {
        str(query["query_id"]): query for code in SESSION_CODES for query in snapshots[code]["queries"]
    }
    audit_lookup = {(str(row["prompt"]), str(row["query_id"])): row for row in audits}
    cases: list[dict[str, Any]] = []
    for query_id in MANDATORY_CASE_IDS:
        query = query_lookup[query_id]
        code = query_id[:4]
        pages = {str(page["page_id"]): page for page in snapshots[code]["pages"]}
        gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
        gold_set = set(gold_ids)
        variant_rankings = {
            variant: list(rankings[code][variant][query_id]) for variant in ("Baseline", *PROMPTS)
        }
        maps = {variant: rank_map(ranking) for variant, ranking in variant_rankings.items()}
        detail_ids: list[str] = []
        for page_id in [
            *(
                str(item["page_id"])
                for variant in ("Baseline", *PROMPTS)
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
                "actual_embedding_texts": {prompt: resolved[prompt][query_id] for prompt in PROMPTS},
                "rankings_top10": {
                    variant: top_rows(variant_rankings[variant], gold_set, DISPLAY_K)
                    for variant in ("Baseline", *PROMPTS)
                },
                "gold_comparison": [
                    {
                        "gold_page_id": page_id,
                        **{
                            variant: {
                                "rank": int(maps[variant][page_id]["rank"]),
                                "score": float(maps[variant][page_id]["score"]),
                            }
                            for variant in ("Baseline", *PROMPTS)
                        },
                    }
                    for page_id in gold_ids
                ],
                "resolution_audits": {
                    prompt: audit_lookup[(prompt, query_id)] for prompt in PROMPTS
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
                            for variant in ("Baseline", *PROMPTS)
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
    lines = [f"### {title} Top10", "", "```text"]
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
    prompt_metrics: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> None:
    lines = [
        "# Reference Resolution Prompt Engineering Ablation",
        "",
        "## Clean replacement contract",
        "",
        "- Baseline embedding input: `original_query only`.",
        "- P0/P1/P2/P3 embedding input: each Prompt's `resolved_query only`.",
        "- Context: current Query + `previous_3_qa` only.",
        "- Retrieval: per-Session dense cosine over frozen production P0 Page vectors.",
        "",
        "## Aggregate metrics",
        "",
        "| Prompt | Changed | Micro R@5 | Macro R@5 | Promoted | Demoted | Net | Extreme demotion | Useful rate | Harmful rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in prompt_metrics:
        lines.append(
            f"| {row['prompt']} | {row['changed_query_count']} | {float(row['micro_r5']):.9f} | "
            f"{float(row['macro_session_r5']):.9f} | {row['promoted_gold']} | "
            f"{row['demoted_gold']} | {int(row['net_gold_gain']):+d} | "
            f"{row['extreme_demotion_top5_to_beyond20']} | "
            f"{float(row['useful_change_rate']):.9f} | {float(row['harmful_change_rate']):.9f} |"
        )
    lines.append("")
    for case in cases:
        lines.extend((f"# {case['query_id']}", "", "### Original Query", "", *fenced(case["original_query"])))
        for prompt in PROMPTS:
            lines.extend(
                (
                    f"### {prompt} Resolved Query",
                    "",
                    *fenced(case["resolved_queries"][prompt]),
                    f"### {prompt} actual embedding text",
                    "",
                    *fenced(case["actual_embedding_texts"][prompt]),
                )
            )
        for variant in ("Baseline", *PROMPTS):
            lines.extend(ranking_lines(variant, case["rankings_top10"][variant]))
        lines.extend(("### All Gold ranks and scores", "", "```text"))
        for gold in case["gold_comparison"]:
            lines.append(f"Gold Page: {gold['gold_page_id']}")
            for variant in ("Baseline", *PROMPTS):
                lines.append(
                    f"{variant}: #{gold[variant]['rank']} score={float(gold[variant]['score']):.9f}"
                )
            lines.append("")
        lines.extend(("```", "", "### Resolution audits", ""))
        for prompt in PROMPTS:
            audit = case["resolution_audits"][prompt]
            lines.extend((f"#### {prompt}", "", "```json"))
            lines.append(json.dumps(audit["added_content"], ensure_ascii=False, indent=2))
            lines.extend(("```", ""))
        lines.extend(("### Production P0 Page texts (Baseline/P0/P1/P2/P3 Top5 union + all Gold)", ""))
        for page in case["page_details"]:
            lines.extend(
                (
                    f"#### Page {page['page_id']}",
                    "",
                    f"- Source Turn ID: `{page['source_turn_id']}`",
                    f"- Gold: `{'YES' if page['is_gold'] else 'NO'}`",
                )
            )
            for variant in ("Baseline", *PROMPTS):
                value = page["ranks_and_scores"][variant]
                lines.append(f"- {variant}: `#{value['rank']} / {float(value['score']):.9f}`")
            lines.extend(("", "完整 production P0 embedding text：", "", *fenced(page["production_p0_embedding_text"])))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def validate_unchanged_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, Mapping[str, str]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for prompt in PROMPTS:
        count = 0
        for code in SESSION_CODES:
            for query in snapshots[code]["queries"]:
                query_id = str(query["query_id"])
                if resolved[prompt][query_id] != str(query["original_query"]):
                    continue
                count += 1
                baseline = list(rankings[code]["Baseline"][query_id])
                variant = list(rankings[code][prompt][query_id])
                baseline_ids = [str(row["page_id"]) for row in baseline]
                variant_ids = [str(row["page_id"]) for row in variant]
                max_score_delta = max(
                    (
                        abs(float(before["score"]) - float(after["score"]))
                        for before, after in zip(baseline, variant)
                    ),
                    default=0.0,
                )
                if baseline_ids != variant_ids or max_score_delta > SCORE_ATOL:
                    failures.append(
                        {
                            "prompt": prompt,
                            "query_id": query_id,
                            "same_order": baseline_ids == variant_ids,
                            "max_score_delta": max_score_delta,
                        }
                    )
        counts[prompt] = count
    validation = {
        "unchanged_query_counts": counts,
        "full_ranking_and_score_tolerance": SCORE_ATOL,
        "unchanged_text_implies_same_ranking": "PASS" if not failures else "FAIL",
        "all_pass": not failures,
        "failures": failures,
    }
    if failures:
        raise AssertionError(f"Unchanged clean-replacement validation failed: {validation}")
    return validation


async def async_main(args: argparse.Namespace) -> None:
    args.output_dir = args.output_dir.resolve()
    prompts, prompt_manifest = load_frozen_prompts(args.output_dir)
    prompt_hashes_before = {prompt: sha256_bytes(args.output_dir / PROMPT_DIR_NAME / PROMPT_FILES[prompt]) for prompt in PROMPTS}
    snapshots = load_snapshots()
    p0_resolved, p0_rows = load_p0_queries(snapshots)
    generated, llm_rows, llm_metadata = await load_or_generate_prompt_queries(
        args, snapshots, prompts
    )
    prompt_hashes_after = {prompt: sha256_bytes(args.output_dir / PROMPT_DIR_NAME / PROMPT_FILES[prompt]) for prompt in PROMPTS}
    if prompt_hashes_before != prompt_hashes_after:
        raise AssertionError("A frozen Prompt changed while P1/P2/P3 were running")
    resolved: dict[str, dict[str, str]] = {"P0": p0_resolved, **generated}

    audits = build_audits(snapshots, resolved)
    resolution_rows, over_summary = build_resolution_summaries(audits, resolved, snapshots)
    vectors, embedding_cache, embedding_metadata = load_query_vectors(
        args.output_dir, snapshots, resolved
    )
    rankings = rank_all(snapshots, vectors)
    clean_reuse_validation = validate_clean_reuse(rankings)
    unchanged_validation = validate_unchanged_rankings(snapshots, resolved, rankings)
    metrics = evaluate_all(snapshots, rankings)
    gold_rows, query_rows, change_rows = build_gold_and_query_rows(
        snapshots, resolved, audits, rankings
    )
    prompt_metrics = build_prompt_metrics(
        snapshots, resolved, rankings, metrics, gold_rows, query_rows
    )
    session_metrics = build_session_metrics(
        snapshots, resolved, metrics, gold_rows, query_rows
    )
    cases = build_cases(snapshots, resolved, rankings, audits)

    write_csv(args.output_dir / "prompt_metrics.csv", prompt_metrics)
    write_csv(args.output_dir / "session_metrics.csv", session_metrics)
    write_csv(args.output_dir / "query_results.csv", query_rows)
    write_csv(args.output_dir / "gold_results.csv", gold_rows)
    write_csv(args.output_dir / "prompt_change_matrix.csv", change_rows)
    write_csv(args.output_dir / "resolution_type_summary.csv", resolution_rows)
    write_jsonl(args.output_dir / "resolution_audit.jsonl", audits)
    dump_json(args.output_dir / "over_resolution_summary.json", over_summary)
    dump_json(
        args.output_dir / "representative_cases.json",
        {"mandatory_case_ids": list(MANDATORY_CASE_IDS), "cases": cases},
    )
    write_report(args.output_dir / "representative_cases.md", prompt_metrics, cases)

    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    query_outputs = []
    for query in all_queries:
        query_id = str(query["query_id"])
        row: dict[str, Any] = {
            "session_id": query_id[:4],
            "query_id": query_id,
            "original_query": query["original_query"],
            "visible_context_turn_ids": [str(item["turn_id"]) for item in query.get("previous_3_qa") or []],
            "P0": {
                "resolved_query": resolved["P0"][query_id],
                "source": "reused reference-resolution-v1",
                "prompt_version": PROMPT_VERSIONS["P0"],
                "prompt_hash": p0_rows[query_id]["prompt_hash"],
            },
        }
        for prompt in GENERATED_PROMPTS:
            llm_row = llm_rows[prompt][query_id]
            row[prompt] = {
                "resolved_query": resolved[prompt][query_id],
                "prompt_version": llm_row["prompt_version"],
                "prompt_hash": llm_row["prompt_hash"],
                "model": llm_row["model"],
                "thinking_mode": llm_row["thinking_mode"],
            }
        query_outputs.append(row)
    write_jsonl(args.output_dir / "resolved_queries.jsonl", query_outputs)

    run_metadata = {
        "experiment": "reference_resolution_prompt_engineering_ablation",
        "full_session_rerun": False,
        "page_regeneration": False,
        "page_representation": "production P0: summary + Keywords + User",
        "page_embedding_source": "reused immutable snapshot stored_embedding",
        "page_embedding_reused": sum(len(snapshots[code]["pages"]) for code in SESSION_CODES),
        "embedding_model": PRODUCTION_EMBEDDING,
        "retrieval_method": "per-Session dense cosine",
        "embedding_contract": {
            "Baseline": "original_query only",
            "P0": "P0 resolved_query only",
            "P1": "P1 resolved_query only",
            "P2": "P2 resolved_query only",
            "P3": "P3 resolved_query only",
        },
        "context_contract": "current query + previous_3_qa only",
        "forbidden_generation_inputs": [
            "Gold",
            "required_context",
            "Gold Page",
            "Page content",
            "retrieval result",
            "retrieval score",
            "future dialogue",
            "benchmark label",
        ],
        "prompt_manifest": prompt_manifest,
        "prompt_sha256_before_llm": prompt_hashes_before,
        "prompt_sha256_after_llm": prompt_hashes_after,
        "prompts_frozen_before_first_llm_call": True,
        "prompt_adaptation_after_results": False,
        "prompt_count": 4,
        "reference_resolution_model": "deepseek-v4-flash",
        "thinking_mode": "disabled",
        "p0_llm_calls": 0,
        "p0_query_cache_reused": 99,
        "generation": llm_metadata,
        "embedding": embedding_metadata,
        "clean_reuse_validation": clean_reuse_validation,
        "unchanged_clean_replacement_validation": unchanged_validation,
        "snapshot_paths": {code: snapshots[code]["path"] for code in SESSION_CODES},
    }
    dump_json(args.output_dir / "run_metadata.json", run_metadata)
    embedding_cache.release(PRODUCTION_EMBEDDING)


def main() -> None:
    asyncio.run(async_main(parse_args()))


if __name__ == "__main__":
    main()
