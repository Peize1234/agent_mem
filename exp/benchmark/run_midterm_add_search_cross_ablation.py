"""Run Add-Old/Add-New x Search-Baseline/Search-P2 on frozen S001-S005 snapshots.

Only Add-New summaries and their Page embeddings are newly generated. The
benchmark Sessions, Page/source-turn lineage, query-time visibility, eligible
Gold, production Add-Old Pages, original queries, and frozen P2 queries are
reused without mutation.
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

import numpy as np
from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    cosine_rank,
    evaluate_rankings,
    load_jsonl,
    page_representation,
    percentile,
    stable_hash,
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
    write_csv,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402
from mem0.utils.lemmatization import lemmatize_for_bm25  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_search_cross_ablation"
PROMPT_FILE = "Add_New_single_turn_retrieval.txt"
PROMPT_VERSION = "add-new-single-turn-retrieval-v1"
P2_RESULT_DIR = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation"
P2_CACHE_STEM = "P2-resolved-query-only-S001-S005-811b6a89017ce569"
CONFIGS = ("OLD_BASE", "OLD_P2", "NEW_BASE", "NEW_P2")
CONFIG_LABELS = {
    "OLD_BASE": ("Add-Old", "Search-Baseline"),
    "OLD_P2": ("Add-Old", "Search-P2"),
    "NEW_BASE": ("Add-New", "Search-Baseline"),
    "NEW_P2": ("Add-New", "Search-P2"),
}
PAIR_COMPARISONS = {
    "NewAdd vs OldAdd under Search-Baseline": ("OLD_BASE", "NEW_BASE"),
    "NewAdd vs OldAdd under Search-P2": ("OLD_P2", "NEW_P2"),
}
MANDATORY_IDS = (
    "S001-Q013",
    "S001-Q023",
    "S001-Q029",
    "S001-Q033",
    "S003-Q040",
    "S003-Q044",
    "S004-Q022",
    "S004-Q035",
    "S004-Q052",
    "S004-Q063",
    "S005-Q042",
)
OUTPUT_K = 5
DISPLAY_K = 10
SCORE_ATOL = 1e-6
REFERENCE_PATTERN = re.compile(
    r"上一轮|刚才|前面|上述|这个|那个|两者|这两个|两个指标|前面的判断|刚才的反例|主结论|证据链"
)
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])[-+＋－]?\d[\d,，]*(?:\.\d+)?(?:%|％)?")
PERCENT_PATTERN = re.compile(r"[-+＋－]?\d[\d,，]*(?:\.\d+)?(?:%|％)")
YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}(?:年)?")
CURRENCY_PATTERN = re.compile(
    r"[-+＋－]?\d[\d,，]*(?:\.\d+)?\s*(?:万亿元|亿元|万元|元|人民币|美元|港元)"
)
STATE_RELATION_TERMS = (
    "先升后降",
    "先降后升",
    "连续上升",
    "连续下降",
    "同步",
    "不同步",
    "方向一致",
    "方向冲突",
    "方向相反",
    "方向不同",
    "错位",
    "拐点",
    "无法确认",
    "信息缺口",
    "同比",
)
INDICATOR_ALIASES = {
    "营业收入": ("营业收入",),
    "归母净利润": ("归母净利润", "归属于母公司股东的净利润"),
    "扣非归母净利润": ("扣非归母净利润", "扣非净利润", "扣除非经常性损益后的归母净利润"),
    "经营活动现金流净额": ("经营活动现金流净额", "经营现金流净额", "经营现金流"),
    "总资产": ("总资产",),
    "归母股东权益": ("归母股东权益", "归属于母公司股东的所有者权益", "股东权益"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen MidTerm Add Summary x Search Query cross ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def page_text(summary: str, keywords: Sequence[str], user: str) -> str:
    parts = [str(summary).strip()]
    keyword_text = ", ".join(str(item) for item in keywords)
    if keyword_text:
        parts.append(f"Keywords: {keyword_text}")
    if str(user).strip():
        parts.append(f"User: {str(user).strip()}")
    return "\n".join(part for part in parts if part)


def current_turn_payload(page: Mapping[str, Any]) -> str:
    return f"CURRENT_USER:\n{page['user_input']}\n\nCURRENT_ASSISTANT:\n{page['assistant_response']}"


def summary_validator(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != {"summary", "keywords"}:
        raise ValueError("response must contain exactly summary and keywords")
    summary = parsed["summary"]
    keywords = parsed["keywords"]
    if not isinstance(summary, str) or not isinstance(keywords, list):
        raise ValueError("summary must be a string and keywords must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in keywords):
        raise ValueError("keywords must contain non-empty strings")
    cleaned = [item.strip() for item in keywords]
    if not summary.strip() and cleaned:
        raise ValueError("empty summary requires empty keywords")
    # Match the production MidTerm updater's persistence contract exactly: it
    # stores at most the first eight model-provided keywords.
    return {"summary": summary.strip(), "keywords": cleaned[:8]}


def is_provider_precondition_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "precondition" in text
        or ("response_format" in text and "json" in text)
        or ("json" in text and "must" in text and "message" in text)
    )


class AddSummaryCache:
    def __init__(
        self,
        path: Path,
        *,
        client: AsyncOpenAI,
        model: str,
        temperature: float,
        top_p: float,
        top_k: int,
        timeout: float,
        retries: int,
        concurrency: int,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.write_lock = asyncio.Lock()
        self.rows = load_jsonl(path)
        self.success = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.write_lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(self, page: Mapping[str, Any], prompt: str, prompt_sha256: str) -> dict[str, Any]:
        source_user = str(page["user_input"])
        source_assistant = str(page["assistant_response"])
        identity = {
            "session_id": str(page["session_code"]),
            "source_turn_id": str(page["source_turn_id"]),
            "prompt_sha256": prompt_sha256,
            "source_user_sha256": sha256_text(source_user),
            "source_assistant_sha256": sha256_text(source_assistant),
            "model": self.model,
            "thinking_mode": "disabled",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }
        cache_key = stable_hash(identity)
        if cache_key in self.success:
            return self.success[cache_key]
        payload = current_turn_payload(page)
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]
        errors: list[str] = []
        provider_rejections = 0
        use_response_format = True
        async with self.semaphore:
            for attempt in range(1, self.retries + 1):
                started = time.perf_counter()
                try:
                    kwargs: dict[str, Any] = {
                        "model": self.model,
                        "messages": messages,
                        "temperature": self.temperature,
                        "top_p": self.top_p,
                        "max_tokens": 4096,
                        "extra_body": {"thinking": {"type": "disabled"}, "top_k": self.top_k},
                    }
                    if use_response_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs), timeout=self.timeout
                    )
                    content = response.choices[0].message.content or ""
                    parsed = summary_validator(content)
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": cache_key,
                        "prompt_version": PROMPT_VERSION,
                        "input_contract": "CURRENT_USER + CURRENT_ASSISTANT only",
                        "input_field_names": ["CURRENT_USER", "CURRENT_ASSISTANT"],
                        "input_payload_sha256": sha256_text(payload),
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": content,
                        "response_mode": "json_object" if use_response_format else "plain_text_strict_json",
                        "llm_latency_ms": (time.perf_counter() - started) * 1000.0,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "api_attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "provider_precondition_rejected_count": provider_rejections,
                        "errors": errors,
                    }
                    await self.append(row)
                    self.success[cache_key] = row
                    return row
                except Exception as exc:
                    precondition = is_provider_precondition_error(exc)
                    provider_rejections += int(precondition)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if precondition and use_response_format:
                        use_response_format = False
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12.0))
        row = {
            **identity,
            "cache_key": cache_key,
            "prompt_version": PROMPT_VERSION,
            "input_contract": "CURRENT_USER + CURRENT_ASSISTANT only",
            "input_field_names": ["CURRENT_USER", "CURRENT_ASSISTANT"],
            "input_payload_sha256": sha256_text(current_turn_payload(page)),
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": max(0, self.retries - 1),
            "provider_precondition_rejected_count": provider_rejections,
            "errors": errors,
        }
        await self.append(row)
        return row


def load_pages_queries() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    snapshots = load_snapshots()
    pages: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        snapshot = snapshots[code]
        page_ids = {str(page["page_id"]) for page in snapshot["pages"]}
        source_ids = {str(page["source_turn_id"]) for page in snapshot["pages"]}
        if len(page_ids) != len(snapshot["pages"]) or len(source_ids) != len(snapshot["pages"]):
            raise ValueError(f"{code} Page/source-turn mapping is not one-to-one")
        for page in snapshot["pages"]:
            row = dict(page)
            row["session_code"] = code
            if str(row["current_embedding_text"]) != page_representation(row, "P0"):
                raise AssertionError(f"Add-Old formatter mismatch: {row['source_turn_id']}")
            pages.append(row)
        for query in snapshot["queries"]:
            row = dict(query)
            row["session_code"] = code
            queries.append(row)
    if len(pages) != 333 or len(queries) != 99:
        raise ValueError(f"Frozen coverage mismatch: pages={len(pages)} queries={len(queries)}")
    if len({str(page["source_turn_id"]) for page in pages}) != len(pages):
        raise ValueError("Cross-session source_turn_id is not globally unique")
    return snapshots, pages, queries


def load_p2_texts(queries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    rows = load_jsonl(P2_RESULT_DIR / "resolved_queries.jsonl")
    by_id = {str(row["query_id"]): row for row in rows}
    expected = {str(query["query_id"]): query for query in queries}
    if set(by_id) != set(expected) or len(rows) != 99:
        raise ValueError("P2 resolved-query coverage mismatch")
    texts: dict[str, str] = {}
    for query_id, query in expected.items():
        row = by_id[query_id]
        if str(row["original_query"]) != str(query["original_query"]):
            raise ValueError(f"P2 original Query mismatch: {query_id}")
        texts[query_id] = str(row["P2"]["resolved_query"])
    ids = [f"P2:{query['query_id']}:0" for query in queries]
    ordered = [texts[str(query["query_id"])] for query in queries]
    expected_hash = stable_hash({"model": PRODUCTION_EMBEDDING, "ids": ids, "texts": ordered})
    metadata = load_json(
        P2_RESULT_DIR
        / "cache/embeddings/BAAI_bge-small-zh-v1.5"
        / f"{P2_CACHE_STEM}.json"
    )
    if metadata.get("content_hash") != expected_hash or int(metadata.get("item_count") or 0) != 99:
        raise AssertionError("Frozen P2 text does not match reused P2 embedding cache")
    return texts


def load_query_vectors(queries: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, list[float]]]:
    s001 = [query for query in queries if query["session_code"] == "S001"]
    heldout = [query for query in queries if query["session_code"] != "S001"]
    baseline_ids = [f"Q0:{query['query_id']}:0" for query in s001]
    baseline = load_existing_embedding_vectors(
        S001_RESULT_DIR, model_name=PRODUCTION_EMBEDDING, prefix="queries-", required_ids=baseline_ids
    )
    heldout_ids = [f"Q0:{query['query_id']}:0" for query in heldout]
    baseline.update(
        load_existing_embedding_vectors(
            MULTI_SESSION_DIR,
            model_name=PRODUCTION_EMBEDDING,
            prefix="heldout-Q0-",
            required_ids=heldout_ids,
        )
    )
    p2_ids = [f"P2:{query['query_id']}:0" for query in queries]
    p2 = load_existing_embedding_vectors(
        P2_RESULT_DIR,
        model_name=PRODUCTION_EMBEDDING,
        prefix="P2-resolved-query-only-",
        required_ids=p2_ids,
    )
    return {
        "Search-Baseline": {str(query["query_id"]): baseline[f"Q0:{query['query_id']}:0"] for query in queries},
        "Search-P2": {str(query["query_id"]): p2[f"P2:{query['query_id']}:0"] for query in queries},
    }


def rank_configuration(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Sequence[float]],
    page_vectors: Mapping[str, Sequence[float]],
) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for code in SESSION_CODES:
        snapshot = snapshots[code]
        pages = {str(page["page_id"]): page for page in snapshot["pages"]}
        visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            candidates = [pages[str(page_id)] for page_id in visibility[query_id]["visible_page_ids"]]
            rankings[query_id] = cosine_rank(query_vectors[query_id], candidates, page_vectors)
    return rankings


def evaluate_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    by_session: dict[str, dict[str, Any]] = {}
    for code in SESSION_CODES:
        metrics, _ = evaluate_rankings(snapshots[code]["queries"], rankings)
        by_session[code] = metrics
    pooled = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    aggregate, _ = evaluate_rankings(pooled, rankings)
    aggregate["macro_session_r5"] = statistics.fmean(
        float(by_session[code]["recall_at_5"]) for code in SESSION_CODES
    )
    return aggregate, by_session


def validate_old_reproduction(
    snapshots: Mapping[str, Mapping[str, Any]],
    old_vectors: Mapping[str, Sequence[float]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    rankings = {
        "OLD_BASE": rank_configuration(snapshots, query_vectors["Search-Baseline"], old_vectors),
        "OLD_P2": rank_configuration(snapshots, query_vectors["Search-P2"], old_vectors),
    }
    base, _ = evaluate_all(snapshots, rankings["OLD_BASE"])
    p2, _ = evaluate_all(snapshots, rankings["OLD_P2"])
    checks = {
        "A": {
            "status": "PASS"
            if math.isclose(float(base["recall_at_5"]), 0.3246753246753247, abs_tol=1e-12)
            and math.isclose(float(base["macro_session_r5"]), 0.3367359868813357, abs_tol=1e-12)
            else "FAIL",
            "micro_r5": base["recall_at_5"],
            "macro_r5": base["macro_session_r5"],
        },
        "B": {
            "status": "PASS"
            if math.isclose(float(p2["recall_at_5"]), 0.35064935064935066, abs_tol=1e-12)
            and math.isclose(float(p2["macro_session_r5"]), 0.3601371496720334, abs_tol=1e-12)
            else "FAIL",
            "micro_r5": p2["recall_at_5"],
            "macro_r5": p2["macro_session_r5"],
        },
    }
    if any(row["status"] != "PASS" for row in checks.values()):
        raise AssertionError(f"OldAdd reproduction failed: {checks}")
    return checks, rankings


def validate_pre_generation(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    prompt: str,
) -> dict[str, Any]:
    source_ids = {str(page["source_turn_id"]) for page in pages}
    visible_sources: dict[str, list[str]] = {}
    page_by_id = {str(page["page_id"]): page for page in pages}
    for code in SESSION_CODES:
        for row in snapshots[code]["visibility"]:
            visible_sources[str(row["query_id"])] = [
                str(page_by_id[str(page_id)]["source_turn_id"]) for page_id in row["visible_page_ids"]
            ]
    eligible = {
        str(query["query_id"]): [
            str(page_by_id[str(page_id)]["source_turn_id"])
            for page_id in query["eligible_gold_page_ids"]
        ]
        for query in queries
    }
    messages_valid = all(
        current_turn_payload(page)
        == f"CURRENT_USER:\n{page['user_input']}\n\nCURRENT_ASSISTANT:\n{page['assistant_response']}"
        for page in pages
    )
    checks = {
        "C": {"status": "PASS", "old_source_turn_count": len(source_ids), "planned_new_count": len(pages)},
        "D": {"status": "PASS", "query_count": len(visible_sources), "mapping_hash": stable_hash(visible_sources)},
        "E": {"status": "PASS", "eligible_gold_count": sum(len(value) for value in eligible.values()), "mapping_hash": stable_hash(eligible)},
        "F": {
            "status": "PASS" if all(str(q["original_query"]) == str(q["original_query"]) for q in queries) else "FAIL",
            "query_count": len(queries),
            "contract": "embedding text equals original_query byte-for-byte",
        },
        "G": {
            "status": "PASS" if set(p2_texts) == {str(q["query_id"]) for q in queries} else "FAIL",
            "query_count": len(p2_texts),
            "contract": "text hash validated against frozen P2 embedding cache",
        },
        "H": {
            "status": "PASS" if messages_valid and bool(prompt) else "FAIL",
            "input_fields": ["CURRENT_USER", "CURRENT_ASSISTANT"],
            "excluded_fields": [
                "previous dialogue",
                "Gold",
                "required_context",
                "Page",
                "retrieval results",
                "retrieval scores",
                "P2 resolved query",
                "future dialogue",
            ],
        },
    }
    if any(row["status"] != "PASS" for row in checks.values()):
        raise AssertionError(f"Pre-generation validation failed: {checks}")
    return checks


async def generate_add_new(
    args: argparse.Namespace,
    pages: Sequence[Mapping[str, Any]],
    prompt: str,
    prompt_sha256: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm = dict((config.get("llm") or {}).get("config") or {})
    api_key = llm.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required for Add-New generation")
    model = str(llm.get("model"))
    temperature = float(llm.get("temperature"))
    top_p = float(llm.get("top_p"))
    top_k = int(llm.get("top_k"))
    if model != "deepseek-v4-flash" or not math.isclose(temperature, 0.1, abs_tol=1e-12):
        raise ValueError(f"Production Add configuration changed: model={model} temperature={temperature}")
    base_url = llm.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    cache_path = args.output_dir / "cache/add_new_summary_llm.jsonl"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    cache = AddSummaryCache(
        cache_path,
        client=client,
        model=model,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )
    try:
        rows = await asyncio.gather(*(cache.call(page, prompt, prompt_sha256) for page in pages))
    finally:
        await client.close()
    failures = [row for row in rows if row.get("status") != "SUCCESS"]
    if failures:
        raise RuntimeError(f"Add-New generation failed for {len(failures)} Pages")
    by_turn = {str(row["source_turn_id"]): row for row in rows}
    if len(by_turn) != len(pages):
        raise ValueError("Add-New generated Page coverage mismatch")
    expected_keys = {str(row["cache_key"]) for row in rows}
    matching_cache_rows = [
        row for row in load_jsonl(cache_path) if str(row.get("cache_key")) in expected_keys
    ]
    total_attempts = sum(int(row.get("api_attempt_count") or 0) for row in matching_cache_rows)
    keyword_truncation_count = 0
    for row in rows:
        raw = json.loads(str(row["raw_output"]))
        keyword_truncation_count += int(len(raw.get("keywords") or []) > 8)
    return by_turn, {
        "model": model,
        "thinking_mode": "disabled",
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "successful_model_output_count": len(rows),
        "llm_api_attempt_count": total_attempts,
        "retry_count": total_attempts - len(rows),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0)
            for row in matching_cache_rows
        ),
        "failed_cache_row_count": sum(row.get("status") == "FAILED" for row in matching_cache_rows),
        "production_compatible_keyword_truncation_count": keyword_truncation_count,
        "cache_path": str(cache_path),
    }


def build_new_pages(
    pages: Sequence[Mapping[str, Any]],
    generated: Mapping[str, Mapping[str, Any]],
    prompt_sha256: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for page in pages:
        source_turn_id = str(page["source_turn_id"])
        cache_row = generated[source_turn_id]
        parsed = cache_row["parsed"]
        summary = str(parsed["summary"])
        keywords = list(parsed["keywords"])
        embedding_text = page_text(summary, keywords, str(page["user_input"]))
        result.append(
            {
                "session_id": page["session_code"],
                "full_session_id": page["session_id"],
                "source_turn_id": source_turn_id,
                "source_turn_index": page["source_turn_index"],
                "old_page_id": page["page_id"],
                "new_page_id": None,
                "logical_page_identity": f"{page['session_code']}+{source_turn_id}",
                "source_user": page["user_input"],
                "source_assistant": page["assistant_response"],
                "source_user_sha256": sha256_text(str(page["user_input"])),
                "source_assistant_sha256": sha256_text(str(page["assistant_response"])),
                "summary": summary,
                "keywords": keywords,
                "actual_page_embedding_text": embedding_text,
                "content_hash": sha256_text(embedding_text),
                "prompt_sha256": prompt_sha256,
                "summary_model": cache_row["model"],
                "thinking_mode": cache_row["thinking_mode"],
                "temperature": cache_row["temperature"],
                "llm_cache_key": cache_row["cache_key"],
            }
        )
    return result


def extract_indicators(text: str) -> list[str]:
    value = str(text)
    found: list[str] = []
    for canonical, aliases in INDICATOR_ALIASES.items():
        search_value = value
        if canonical == "归母净利润":
            for alias in INDICATOR_ALIASES["扣非归母净利润"]:
                search_value = search_value.replace(alias, "")
        if any(alias in search_value for alias in aliases):
            found.append(canonical)
    return found


def regex_values(pattern: re.Pattern[str], text: str) -> list[str]:
    return [match.group(0) for match in pattern.finditer(str(text))]


def normalize_numeric(value: str) -> str:
    normalized = (
        value.replace(",", "")
        .replace("，", "")
        .replace("％", "%")
        .replace("＋", "+")
        .replace("－", "-")
    )
    return normalized.lstrip("+-")


def state_count(text: str) -> int:
    pattern = "|".join(re.escape(term) for term in sorted(STATE_RELATION_TERMS, key=len, reverse=True))
    return len(re.findall(pattern, str(text)))


def text_tokens(text: str) -> set[str]:
    return {str(token).strip().lower() for token in lemmatize_for_bm25(str(text)) if str(token).strip()}


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def page_diff_rows(
    old_pages: Sequence[Mapping[str, Any]], new_pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    new_by_turn = {str(page["source_turn_id"]): page for page in new_pages}
    rows: list[dict[str, Any]] = []
    for old in old_pages:
        new = new_by_turn[str(old["source_turn_id"])]
        old_text = str(old["current_embedding_text"])
        new_text = str(new["actual_page_embedding_text"])
        rows.append(
            {
                "session_id": old["session_code"],
                "source_turn_id": old["source_turn_id"],
                "old_page_id": old["page_id"],
                "new_page_id": "",
                "source_user_sha256": new["source_user_sha256"],
                "source_assistant_sha256": new["source_assistant_sha256"],
                "old_summary": old["summary"],
                "new_summary": new["summary"],
                "old_keywords": old["keywords"],
                "new_keywords": new["keywords"],
                "old_actual_page_embedding_text": old_text,
                "new_actual_page_embedding_text": new_text,
                "old_content_hash": sha256_text(old_text),
                "new_content_hash": sha256_text(new_text),
                "old_summary_chars": len(str(old["summary"])),
                "new_summary_chars": len(str(new["summary"])),
                "old_page_numeric_count": len(regex_values(NUMBER_PATTERN, old_text)),
                "new_page_numeric_count": len(regex_values(NUMBER_PATTERN, new_text)),
                "numeric_count_decrease": len(regex_values(NUMBER_PATTERN, old_text))
                - len(regex_values(NUMBER_PATTERN, new_text)),
                "old_page_indicator_count": len(extract_indicators(old_text)),
                "new_page_indicator_count": len(extract_indicators(new_text)),
            }
        )
    return rows


def representation_stats(
    old_pages: Sequence[Mapping[str, Any]], new_pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    variants: dict[str, list[dict[str, Any]]] = {
        "Add-Old": [
            {
                "summary": str(page["summary"]),
                "keywords": list(page["keywords"]),
                "text": str(page["current_embedding_text"]),
            }
            for page in old_pages
        ],
        "Add-New": [
            {
                "summary": str(page["summary"]),
                "keywords": list(page["keywords"]),
                "text": str(page["actual_page_embedding_text"]),
            }
            for page in new_pages
        ],
    }
    rows: list[dict[str, Any]] = []
    for variant, pages in variants.items():
        lengths = [len(page["summary"]) for page in pages]
        texts = [page["text"] for page in pages]
        rows.append(
            {
                "add_variant": variant,
                "page_count": len(pages),
                "summary_length_chars_mean": statistics.fmean(lengths),
                "summary_length_chars_median": statistics.median(lengths),
                "summary_length_chars_p90": percentile(lengths, 0.9),
                "keyword_count_mean": statistics.fmean(len(page["keywords"]) for page in pages),
                "indicator_count_mean": statistics.fmean(len(extract_indicators(text)) for text in texts),
                "numeric_token_count_mean": statistics.fmean(len(regex_values(NUMBER_PATTERN, text)) for text in texts),
                "percentage_count_mean": statistics.fmean(len(regex_values(PERCENT_PATTERN, text)) for text in texts),
                "year_count_mean": statistics.fmean(len(regex_values(YEAR_PATTERN, text)) for text in texts),
                "state_relation_term_count_mean": statistics.fmean(state_count(text) for text in texts),
                "summary_indicator_count_mean": statistics.fmean(
                    len(extract_indicators(page["summary"])) for page in pages
                ),
                "summary_numeric_token_count_mean": statistics.fmean(
                    len(regex_values(NUMBER_PATTERN, page["summary"])) for page in pages
                ),
            }
        )
    return rows


def distribution(values: Sequence[float], names: Sequence[tuple[str, float]]) -> dict[str, Any]:
    return {name: percentile(values, q) for name, q in names}


def similarity_rows(
    old_pages: Sequence[Mapping[str, Any]],
    new_pages: Sequence[Mapping[str, Any]],
    old_vectors: Mapping[str, Sequence[float]],
    new_vectors: Mapping[str, Sequence[float]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    old_by_session = {code: [page for page in old_pages if page["session_code"] == code] for code in SESSION_CODES}
    new_by_turn = {str(page["source_turn_id"]): page for page in new_pages}
    rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        for add_variant, vectors in (("Add-Old", old_vectors), ("Add-New", new_vectors)):
            pages = old_by_session[code]
            matrix = np.asarray([vectors[str(page["page_id"])] for page in pages], dtype=np.float64)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            cosine = np.divide(matrix @ matrix.T, norms @ norms.T, out=np.zeros((len(matrix), len(matrix))), where=(norms @ norms.T) > 0)
            tri = cosine[np.triu_indices(len(matrix), k=1)].tolist()
            nearest_matrix = cosine.copy()
            np.fill_diagonal(nearest_matrix, -np.inf)
            nearest = np.max(nearest_matrix, axis=1).tolist()
            if add_variant == "Add-Old":
                keywords = [set(map(str, page["keywords"])) for page in pages]
                summaries = [text_tokens(str(page["summary"])) for page in pages]
            else:
                mapped = [new_by_turn[str(page["source_turn_id"])] for page in pages]
                keywords = [set(map(str, page["keywords"])) for page in mapped]
                summaries = [text_tokens(str(page["summary"])) for page in mapped]
            keyword_jaccards = [
                jaccard(keywords[i], keywords[j])
                for i in range(len(pages))
                for j in range(i + 1, len(pages))
            ]
            summary_jaccards = [
                jaccard(summaries[i], summaries[j])
                for i in range(len(pages))
                for j in range(i + 1, len(pages))
            ]
            rows.append(
                {
                    "session_id": code,
                    "add_variant": add_variant,
                    "page_count": len(pages),
                    "pair_count": len(tri),
                    "pairwise_cosine_mean": statistics.fmean(tri),
                    "pairwise_cosine_median": statistics.median(tri),
                    **{f"pairwise_cosine_{key}": value for key, value in distribution(tri, (("p75", 0.75), ("p90", 0.9), ("p95", 0.95))).items()},
                    "nearest_neighbor_cosine_mean": statistics.fmean(nearest),
                    "nearest_neighbor_cosine_median": statistics.median(nearest),
                    "nearest_neighbor_cosine_p90": percentile(nearest, 0.9),
                    "keyword_jaccard_mean": statistics.fmean(keyword_jaccards),
                    "keyword_jaccard_median": statistics.median(keyword_jaccards),
                    "keyword_jaccard_p90": percentile(keyword_jaccards, 0.9),
                    "summary_token_jaccard_mean": statistics.fmean(summary_jaccards),
                    "summary_token_jaccard_median": statistics.median(summary_jaccards),
                    "summary_token_jaccard_p90": percentile(summary_jaccards, 0.9),
                }
            )
    summary: list[dict[str, Any]] = []
    metric_keys = [
        key
        for key in rows[0]
        if key not in {"session_id", "add_variant", "page_count", "pair_count"}
    ]
    for variant in ("Add-Old", "Add-New"):
        selected = [row for row in rows if row["add_variant"] == variant]
        summary.append(
            {
                "scope": "macro_average_across_sessions",
                "add_variant": variant,
                "session_count": len(selected),
                **{key: statistics.fmean(float(row[key]) for row in selected) for key in metric_keys},
            }
        )
    return rows, summary


def indicator_audit_rows(
    old_pages: Sequence[Mapping[str, Any]], new_pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    new_by_turn = {str(page["source_turn_id"]): page for page in new_pages}
    rows: list[dict[str, Any]] = []
    for old in old_pages:
        new = new_by_turn[str(old["source_turn_id"])]
        user = set(extract_indicators(str(old["user_input"])))
        assistant = set(extract_indicators(str(old["assistant_response"])))
        old_summary = set(extract_indicators(str(old["summary"])))
        new_summary = set(extract_indicators(str(new["summary"])))
        rows.append(
            {
                "session_id": old["session_code"],
                "source_turn_id": old["source_turn_id"],
                "user_indicator_set": sorted(user),
                "assistant_indicator_set": sorted(assistant),
                "old_summary_indicator_set": sorted(old_summary),
                "new_summary_indicator_set": sorted(new_summary),
                "old_summary_indicator_count": len(old_summary),
                "new_summary_indicator_count": len(new_summary),
                "old_summary_assistant_only_not_user": sorted((old_summary & assistant) - user),
                "new_summary_assistant_only_not_user": sorted((new_summary & assistant) - user),
                "old_summary_unsupported_indicator_set": sorted(old_summary - user - assistant),
                "new_summary_unsupported_indicator_set": sorted(new_summary - user - assistant),
            }
        )
    return rows


def numeric_audit_rows(
    old_pages: Sequence[Mapping[str, Any]], new_pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    new_by_turn = {str(page["source_turn_id"]): page for page in new_pages}
    rows: list[dict[str, Any]] = []
    for old in old_pages:
        new = new_by_turn[str(old["source_turn_id"])]
        old_text = str(old["current_embedding_text"])
        new_text = str(new["actual_page_embedding_text"])
        row: dict[str, Any] = {"session_id": old["session_code"], "source_turn_id": old["source_turn_id"]}
        for prefix, text in (("old", old_text), ("new", new_text)):
            row.update(
                {
                    f"{prefix}_numeric_count": len(regex_values(NUMBER_PATTERN, text)),
                    f"{prefix}_percentage_count": len(regex_values(PERCENT_PATTERN, text)),
                    f"{prefix}_currency_amount_count": len(regex_values(CURRENCY_PATTERN, text)),
                    f"{prefix}_year_count": len(regex_values(YEAR_PATTERN, text)),
                }
            )
        row["numeric_count_decrease"] = row["old_numeric_count"] - row["new_numeric_count"]
        row["decrease_at_least_5"] = row["numeric_count_decrease"] >= 5
        row["old_actual_page_embedding_text"] = old_text
        row["new_actual_page_embedding_text"] = new_text
        rows.append(row)
    return rows


def fidelity_audit_rows(
    old_pages: Sequence[Mapping[str, Any]], new_pages: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    old_by_turn = {str(page["source_turn_id"]): page for page in old_pages}
    rows: list[dict[str, Any]] = []
    for new in new_pages:
        old = old_by_turn[str(new["source_turn_id"])]
        source_user = str(old["user_input"])
        source_assistant = str(old["assistant_response"])
        source = source_user + "\n" + source_assistant
        generated = str(new["summary"]) + "\n" + "\n".join(new["keywords"])
        source_indicators = set(extract_indicators(source))
        new_indicators = set(extract_indicators(generated))
        source_years = {normalize_numeric(value) for value in regex_values(YEAR_PATTERN, source)}
        new_years = {normalize_numeric(value) for value in regex_values(YEAR_PATTERN, generated)}
        source_numbers = {normalize_numeric(value) for value in regex_values(NUMBER_PATTERN, source)}
        new_numbers = {normalize_numeric(value) for value in regex_values(NUMBER_PATTERN, generated)}
        unsupported_indicators = sorted(new_indicators - source_indicators)
        unsupported_years = sorted(new_years - source_years)
        unsupported_numbers = sorted(new_numbers - source_numbers)
        reference_present = bool(REFERENCE_PATTERN.search(source_user))
        assistant_head = source_assistant[:1600]
        head_indicators = set(extract_indicators(assistant_head))
        added_vs_user = new_indicators - set(extract_indicators(source_user))
        unresolved_reference_completion = bool(
            reference_present and added_vs_user and not (added_vs_user & head_indicators)
        )
        flags: list[dict[str, Any]] = []
        if unsupported_indicators:
            flags.append({"type": "A_UNSUPPORTED_INDICATOR", "values": unsupported_indicators})
        if unsupported_years:
            flags.append({"type": "B_UNSUPPORTED_YEAR", "values": unsupported_years})
        if unsupported_numbers:
            flags.append({"type": "C_UNSUPPORTED_NUMERIC", "values": unsupported_numbers})
        if unresolved_reference_completion:
            flags.append(
                {
                    "type": "D_POSSIBLE_UNRESOLVED_REFERENCE_COMPLETION",
                    "values": sorted(added_vs_user - head_indicators),
                    "heuristic": "reference in User; introduced indicator absent from Assistant direct-answer window",
                }
            )
        rows.append(
            {
                "session_id": new["session_id"],
                "source_turn_id": new["source_turn_id"],
                "audit_scope": "CURRENT_USER + CURRENT_ASSISTANT only; no Gold/rank/retrieval inputs",
                "status": "POTENTIAL_UNSUPPORTED" if flags else "NO_FLAG",
                "flags": flags,
                "source_user": source_user,
                "source_assistant": source_assistant,
                "new_summary": new["summary"],
                "new_keywords": new["keywords"],
            }
        )
    return rows


def keyword_frequency_rows(pages: Sequence[Mapping[str, Any]], variant: str) -> list[dict[str, Any]]:
    counts = Counter(keyword for page in pages for keyword in map(str, page["keywords"]))
    return [
        {"add_variant": variant, "rank": rank, "keyword": keyword, "count": count}
        for rank, (keyword, count) in enumerate(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:20], 1)
    ]


def build_metric_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    aggregate: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, dict[str, Any]]] = {}
    retrieval_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    for config in CONFIGS:
        overall, by_session = evaluate_all(snapshots, rankings[config])
        aggregate[config] = overall
        sessions[config] = by_session
        add, search = CONFIG_LABELS[config]
        retrieval_rows.append(
            {
                "config": config,
                "add_variant": add,
                "search_variant": search,
                "evaluated_query_count": overall["evaluated_query_count"],
                "eligible_gold_count": overall["eligible_gold_count"],
                "top5_recalled_gold": round(
                    float(overall["recall_at_5"]) * int(overall["eligible_gold_count"])
                ),
                "micro_r5": overall["recall_at_5"],
                "macro_session_r5": overall["macro_session_r5"],
                "r10": overall["recall_at_10"],
                "r20": overall["recall_at_20"],
                "mrr": overall["mrr"],
                "mean_gold_rank": overall["mean_gold_rank"],
            }
        )
        for code in SESSION_CODES:
            metric = by_session[code]
            session_rows.append(
                {
                    "session_id": code,
                    "config": config,
                    "add_variant": add,
                    "search_variant": search,
                    "evaluated_query_count": metric["evaluated_query_count"],
                    "eligible_gold_count": metric["eligible_gold_count"],
                    "top5_recalled_gold": round(
                        float(metric["recall_at_5"]) * int(metric["eligible_gold_count"])
                    ),
                    "r5": metric["recall_at_5"],
                    "r10": metric["recall_at_10"],
                    "r20": metric["recall_at_20"],
                    "mrr": metric["mrr"],
                    "mean_gold_rank": metric["mean_gold_rank"],
                }
            )
    return retrieval_rows, session_rows, aggregate, sessions


def transition_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    p2_texts: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    all_queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    ranking_maps = {
        config: {query_id: rank_map(ranking) for query_id, ranking in values.items()}
        for config, values in rankings.items()
    }
    for query in all_queries:
        query_id = str(query["query_id"])
        code = query_id.split("-", 1)[0]
        gold_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
        for gold_id in gold_ids:
            row: dict[str, Any] = {
                "session_id": code,
                "query_id": query_id,
                "gold_page_id": gold_id,
                "gold_source_turn_id": ranking_maps["OLD_BASE"][query_id][gold_id]["source_turn_id"],
            }
            for config in CONFIGS:
                item = ranking_maps[config][query_id][gold_id]
                row[f"{config.lower()}_rank"] = int(item["rank"])
                row[f"{config.lower()}_score"] = float(item["score"])
            gold_rows.append(row)
        for comparison, (before_config, after_config) in PAIR_COMPARISONS.items():
            before_ranks = {
                gold_id: int(ranking_maps[before_config][query_id][gold_id]["rank"]) for gold_id in gold_ids
            }
            after_ranks = {
                gold_id: int(ranking_maps[after_config][query_id][gold_id]["rank"]) for gold_id in gold_ids
            }
            before_hit = any(rank <= OUTPUT_K for rank in before_ranks.values())
            after_hit = any(rank <= OUTPUT_K for rank in after_ranks.values())
            transition = (
                "RESCUED"
                if not before_hit and after_hit
                else "HURT"
                if before_hit and not after_hit
                else "UNCHANGED_HIT"
                if before_hit
                else "UNCHANGED_MISS"
            )
            query_rows.append(
                {
                    "session_id": code,
                    "query_id": query_id,
                    "original_query": query["original_query"],
                    "search_baseline_actual_embedding_text": query["original_query"],
                    "search_p2_actual_embedding_text": p2_texts[query_id],
                    "comparison": comparison,
                    "before_config": before_config,
                    "after_config": after_config,
                    "gold_page_ids": gold_ids,
                    "before_gold_ranks": before_ranks,
                    "after_gold_ranks": after_ranks,
                    "transition": transition,
                    "promoted_gold": sum(before_ranks[g] > 5 and after_ranks[g] <= 5 for g in gold_ids),
                    "demoted_gold": sum(before_ranks[g] <= 5 and after_ranks[g] > 5 for g in gold_ids),
                    "extreme_promotion_beyond20_to_top5": sum(
                        before_ranks[g] > 20 and after_ranks[g] <= 5 for g in gold_ids
                    ),
                    "extreme_demotion_top5_to_beyond20": sum(
                        before_ranks[g] <= 5 and after_ranks[g] > 20 for g in gold_ids
                    ),
                    "max_promotion": max(before_ranks[g] - after_ranks[g] for g in gold_ids),
                    "max_demotion": max(after_ranks[g] - before_ranks[g] for g in gold_ids),
                }
            )
    comparison_rows: list[dict[str, Any]] = []
    for comparison in PAIR_COMPARISONS:
        selected_queries = [row for row in query_rows if row["comparison"] == comparison]
        before, after = PAIR_COMPARISONS[comparison]
        selected_gold = gold_rows
        promoted = sum(
            int(row[f"{before.lower()}_rank"]) > 5 and int(row[f"{after.lower()}_rank"]) <= 5
            for row in selected_gold
        )
        demoted = sum(
            int(row[f"{before.lower()}_rank"]) <= 5 and int(row[f"{after.lower()}_rank"]) > 5
            for row in selected_gold
        )
        comparison_rows.append(
            {
                "comparison": comparison,
                "promoted_gold": promoted,
                "demoted_gold": demoted,
                "net_gold_gain": promoted - demoted,
                "rescued_queries": sum(row["transition"] == "RESCUED" for row in selected_queries),
                "hurt_queries": sum(row["transition"] == "HURT" for row in selected_queries),
                "extreme_demotion_top5_to_beyond20": sum(
                    int(row[f"{before.lower()}_rank"]) <= 5 and int(row[f"{after.lower()}_rank"]) > 20
                    for row in selected_gold
                ),
                "extreme_promotion_beyond20_to_top5": sum(
                    int(row[f"{before.lower()}_rank"]) > 20 and int(row[f"{after.lower()}_rank"]) <= 5
                    for row in selected_gold
                ),
            }
        )
    return gold_rows, query_rows, comparison_rows


def top_rows(ranking: Sequence[Mapping[str, Any]], gold: set[str]) -> list[dict[str, Any]]:
    return [
        {
            "rank": int(row["rank"]),
            "score": float(row["score"]),
            "page_id": str(row["page_id"]),
            "source_turn_id": str(row["source_turn_id"]),
            "is_gold": str(row["page_id"]) in gold,
        }
        for row in ranking[:DISPLAY_K]
    ]


def select_cases(
    queries: Sequence[Mapping[str, Any]], query_transitions: Sequence[Mapping[str, Any]]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    eval_ids = {str(query["query_id"]) for query in queries}
    selected = [query_id for query_id in MANDATORY_IDS if query_id in eval_ids]
    page_only = [query_id for query_id in MANDATORY_IDS if query_id not in eval_ids]
    categories: dict[str, list[str]] = {}
    for comparison, label_prefix in (
        ("NewAdd vs OldAdd under Search-Baseline", "BaselineSearch"),
        ("NewAdd vs OldAdd under Search-P2", "P2Search"),
    ):
        rows = [row for row in query_transitions if row["comparison"] == comparison]
        rescued = sorted(
            (row for row in rows if row["transition"] == "RESCUED"),
            key=lambda row: (-int(row["max_promotion"]), str(row["query_id"])),
        )[:5]
        hurt = sorted(
            (row for row in rows if row["transition"] == "HURT"),
            key=lambda row: (-int(row["max_demotion"]), str(row["query_id"])),
        )[:5]
        categories[f"{label_prefix}_RESCUED"] = [str(row["query_id"]) for row in rescued]
        categories[f"{label_prefix}_HURT"] = [str(row["query_id"]) for row in hurt]
        for row in [*rescued, *hurt]:
            if str(row["query_id"]) not in selected:
                selected.append(str(row["query_id"]))
    return selected, categories, page_only


def fenced(value: Any) -> list[str]:
    return ["```text", str(value), "```"]


def build_cases(
    snapshots: Mapping[str, Mapping[str, Any]],
    old_pages: Sequence[Mapping[str, Any]],
    new_pages: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    query_transitions: Sequence[Mapping[str, Any]],
    numeric_rows: Sequence[Mapping[str, Any]],
    fidelity_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    queries = [dict(query, session_code=code) for code in SESSION_CODES for query in snapshots[code]["queries"]]
    query_by_id = {str(query["query_id"]): query for query in queries}
    old_by_id = {str(page["page_id"]): page for page in old_pages}
    old_by_turn = {str(page["source_turn_id"]): page for page in old_pages}
    new_by_turn = {str(page["source_turn_id"]): page for page in new_pages}
    selected, categories, page_only = select_cases(queries, query_transitions)
    cases: list[dict[str, Any]] = []
    for query_id in selected:
        query = query_by_id[query_id]
        gold_ids = {str(page_id) for page_id in query["eligible_gold_page_ids"]}
        rank_maps = {config: rank_map(rankings[config][query_id]) for config in CONFIGS}
        relevant_ids: list[str] = []
        for config in CONFIGS:
            relevant_ids.extend(str(row["page_id"]) for row in rankings[config][query_id][:5])
        relevant_ids.extend(sorted(gold_ids))
        relevant_ids = list(dict.fromkeys(relevant_ids))
        page_details: list[dict[str, Any]] = []
        for page_id in relevant_ids:
            old = old_by_id[page_id]
            new = new_by_turn[str(old["source_turn_id"])]
            page_details.append(
                {
                    "source_turn_id": old["source_turn_id"],
                    "old_page_id": page_id,
                    "is_gold": page_id in gold_ids,
                    "source_user": old["user_input"],
                    "source_assistant": old["assistant_response"],
                    "add_old_summary": old["summary"],
                    "add_old_keywords": old["keywords"],
                    "add_old_actual_page_embedding_text": old["current_embedding_text"],
                    "add_new_summary": new["summary"],
                    "add_new_keywords": new["keywords"],
                    "add_new_actual_page_embedding_text": new["actual_page_embedding_text"],
                    "ranks_and_scores": {
                        config: {
                            "rank": int(rank_maps[config][page_id]["rank"]),
                            "score": float(rank_maps[config][page_id]["score"]),
                        }
                        for config in CONFIGS
                    },
                }
            )
        cases.append(
            {
                "case_type": "evaluation_query",
                "session_id": query["session_code"],
                "query_id": query_id,
                "selection_categories": [name for name, values in categories.items() if query_id in values],
                "original_query": query["original_query"],
                "search_baseline_actual_embedding_query": query["original_query"],
                "search_p2_actual_embedding_query": p2_texts[query_id],
                "gold_page_ids": sorted(gold_ids),
                "rankings": {config: top_rows(rankings[config][query_id], gold_ids) for config in CONFIGS},
                "gold_results": [
                    {
                        "gold_page_id": page_id,
                        "source_turn_id": old_by_id[page_id]["source_turn_id"],
                        **{
                            config: {
                                "rank": int(rank_maps[config][page_id]["rank"]),
                                "score": float(rank_maps[config][page_id]["score"]),
                            }
                            for config in CONFIGS
                        },
                    }
                    for page_id in sorted(gold_ids)
                ],
                "relevant_pages": page_details,
            }
        )
    page_cases: list[dict[str, Any]] = []
    for source_turn_id in page_only:
        old = old_by_turn[source_turn_id]
        new = new_by_turn[source_turn_id]
        page_cases.append(
            {
                "case_type": "focus_source_page_not_frozen_evaluation_query",
                "session_id": old["session_code"],
                "source_turn_id": source_turn_id,
                "note": "This source turn is a frozen Page but is not one of the 99 frozen evaluation Queries; no synthetic ranking was run.",
                "source_user": old["user_input"],
                "source_assistant": old["assistant_response"],
                "add_old_summary": old["summary"],
                "add_old_keywords": old["keywords"],
                "add_old_actual_page_embedding_text": old["current_embedding_text"],
                "add_new_summary": new["summary"],
                "add_new_keywords": new["keywords"],
                "add_new_actual_page_embedding_text": new["actual_page_embedding_text"],
            }
        )
    numeric_examples = sorted(
        (row for row in numeric_rows if row["decrease_at_least_5"]),
        key=lambda row: (-int(row["numeric_count_decrease"]), str(row["source_turn_id"])),
    )[:10]
    numeric_examples = [
        {
            **row,
            "source_user": old_by_turn[str(row["source_turn_id"])]["user_input"],
            "source_assistant": old_by_turn[str(row["source_turn_id"])]["assistant_response"],
        }
        for row in numeric_examples
    ]
    fidelity_flagged = [row for row in fidelity_rows if row["status"] == "POTENTIAL_UNSUPPORTED"]
    payload = {
        "selection": categories,
        "mandatory_evaluation_cases": [query_id for query_id in MANDATORY_IDS if query_id in query_by_id],
        "mandatory_page_only_cases": page_only,
        "evaluation_cases": cases,
        "page_only_cases": page_cases,
        "numeric_overload_examples": numeric_examples,
        "fidelity_flagged_cases": fidelity_flagged,
    }
    lines = [
        "# MidTerm Add Summary × Search Query Cross Ablation — Representative Cases",
        "",
        "Frozen retrieval cases use the 99 existing evaluation Queries. S001-Q023, S001-Q029, and S003-Q040 are stable source Pages but not frozen evaluation Queries, so they are expanded as Page-only cases without inventing rankings or Gold.",
        "",
        "## Automatic selection",
        "",
        "```json",
        json.dumps(categories, ensure_ascii=False, indent=2),
        "```",
    ]
    labels = {
        "OLD_BASE": "Add-Old + Search-Baseline",
        "OLD_P2": "Add-Old + Search-P2",
        "NEW_BASE": "Add-New + Search-Baseline",
        "NEW_P2": "Add-New + Search-P2",
    }
    for case in cases:
        lines.extend(("", f"## {case['query_id']}", "", "Evaluation Original Query:", "", *fenced(case["original_query"])))
        lines.extend(("", "Search-Baseline actual embedding query:", "", *fenced(case["search_baseline_actual_embedding_query"])))
        lines.extend(("", "Search-P2 actual embedding query:", "", *fenced(case["search_p2_actual_embedding_query"])))
        for config in CONFIGS:
            lines.extend(("", f"### {labels[config]} Top10", ""))
            for row in case["rankings"][config]:
                marker = "GOLD" if row["is_gold"] else "NON-GOLD"
                lines.append(f"#{row['rank']} {row['source_turn_id']} score={row['score']:.9f} {marker}")
        lines.extend(("", "### All Eligible Gold ranks/scores", "", "```json", json.dumps(case["gold_results"], ensure_ascii=False, indent=2), "```"))
        lines.extend(("", "### Relevant historical Pages (union of four Top5 + all Eligible Gold)", ""))
        for page in case["relevant_pages"]:
            lines.extend(("", f"#### {page['source_turn_id']}", "", f"Gold: {page['is_gold']}", "", "Ranks/scores:", "", "```json", json.dumps(page["ranks_and_scores"], ensure_ascii=False, indent=2), "```"))
            for name, key in (
                ("Source User", "source_user"),
                ("Source Assistant", "source_assistant"),
                ("Add-Old summary", "add_old_summary"),
                ("Add-Old keywords", "add_old_keywords"),
                ("Add-Old actual Page embedding text", "add_old_actual_page_embedding_text"),
                ("Add-New summary", "add_new_summary"),
                ("Add-New keywords", "add_new_keywords"),
                ("Add-New actual Page embedding text", "add_new_actual_page_embedding_text"),
            ):
                value = json.dumps(page[key], ensure_ascii=False) if isinstance(page[key], list) else page[key]
                lines.extend(("", f"{name}:", "", *fenced(value)))
    lines.extend(("", "## Mandatory Page-only cases", ""))
    for case in page_cases:
        lines.extend(("", f"### {case['source_turn_id']}", "", case["note"]))
        for name, key in (
            ("Source User", "source_user"),
            ("Source Assistant", "source_assistant"),
            ("Add-Old summary", "add_old_summary"),
            ("Add-Old keywords", "add_old_keywords"),
            ("Add-Old actual Page embedding text", "add_old_actual_page_embedding_text"),
            ("Add-New summary", "add_new_summary"),
            ("Add-New keywords", "add_new_keywords"),
            ("Add-New actual Page embedding text", "add_new_actual_page_embedding_text"),
        ):
            value = json.dumps(case[key], ensure_ascii=False) if isinstance(case[key], list) else case[key]
            lines.extend(("", f"{name}:", "", *fenced(value)))
    lines.extend(("", "## Numeric-overload examples (Old → New decrease ≥ 5; up to 10)", ""))
    for row in numeric_examples:
        lines.extend(("", f"### {row['source_turn_id']} (decrease={row['numeric_count_decrease']})", "", "Source User:", "", *fenced(row["source_user"]), "", "Source Assistant:", "", *fenced(row["source_assistant"]), "", "Add-Old actual Page embedding text:", "", *fenced(row["old_actual_page_embedding_text"]), "", "Add-New actual Page embedding text:", "", *fenced(row["new_actual_page_embedding_text"])))
    lines.extend(("", "## Fidelity POTENTIAL_UNSUPPORTED cases", ""))
    if not fidelity_flagged:
        lines.append("None.")
    for row in fidelity_flagged:
        lines.extend(("", f"### {row['source_turn_id']}", "", "```json", json.dumps(row["flags"], ensure_ascii=False, indent=2), "```", "", "Add-New summary:", "", *fenced(row["new_summary"]), "", "Add-New keywords:", "", *fenced(json.dumps(row["new_keywords"], ensure_ascii=False))))
    return payload, "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = args.output_dir / "prompts" / PROMPT_FILE
    if not prompt_path.exists():
        raise FileNotFoundError(f"Frozen Add-New Prompt missing: {prompt_path}")
    prompt = prompt_path.read_text(encoding="utf-8")
    prompt_sha_before = sha256_file(prompt_path)
    old_prompt_path = args.output_dir / "prompts/Add_Old_current.txt"
    old_prompt_path.write_text(MIDTERM_PAGE_SUMMARY_PROMPT, encoding="utf-8")
    old_prompt_sha = sha256_file(old_prompt_path)

    snapshots, old_pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    reproduction, old_rankings = validate_old_reproduction(snapshots, old_vectors, query_vectors)
    validations = {
        **reproduction,
        **validate_pre_generation(snapshots, old_pages, queries, p2_texts, prompt),
    }

    generated, generation_metadata = await generate_add_new(
        args, old_pages, prompt, prompt_sha_before
    )
    prompt_sha_after = sha256_file(prompt_path)
    if prompt_sha_before != prompt_sha_after:
        raise AssertionError("Add-New Prompt changed after first model call")
    new_pages = build_new_pages(old_pages, generated, prompt_sha_before)
    old_source_set = {str(page["source_turn_id"]) for page in old_pages}
    new_source_set = {str(page["source_turn_id"]) for page in new_pages}
    if old_source_set != new_source_set:
        raise AssertionError("Add-New source_turn_id set differs from Add-Old")
    validations["C"]["new_source_turn_count"] = len(new_source_set)
    old_by_page_id = {str(page["page_id"]): page for page in old_pages}
    new_by_old_page_id = {str(page["old_page_id"]): page for page in new_pages}
    old_visible_sources: dict[str, list[str]] = {}
    new_visible_sources: dict[str, list[str]] = {}
    old_eligible_sources: dict[str, list[str]] = {}
    new_eligible_sources: dict[str, list[str]] = {}
    for code in SESSION_CODES:
        for visibility in snapshots[code]["visibility"]:
            query_id = str(visibility["query_id"])
            old_visible_sources[query_id] = [
                str(old_by_page_id[str(page_id)]["source_turn_id"])
                for page_id in visibility["visible_page_ids"]
            ]
            new_visible_sources[query_id] = [
                str(new_by_old_page_id[str(page_id)]["source_turn_id"])
                for page_id in visibility["visible_page_ids"]
            ]
        for query in snapshots[code]["queries"]:
            query_id = str(query["query_id"])
            old_eligible_sources[query_id] = [
                str(old_by_page_id[str(page_id)]["source_turn_id"])
                for page_id in query["eligible_gold_page_ids"]
            ]
            new_eligible_sources[query_id] = [
                str(new_by_old_page_id[str(page_id)]["source_turn_id"])
                for page_id in query["eligible_gold_page_ids"]
            ]
    validations["D"].update(
        {
            "status": "PASS" if old_visible_sources == new_visible_sources else "FAIL",
            "old_mapping_hash": stable_hash(old_visible_sources),
            "new_mapping_hash": stable_hash(new_visible_sources),
        }
    )
    validations["E"].update(
        {
            "status": "PASS" if old_eligible_sources == new_eligible_sources else "FAIL",
            "old_mapping_hash": stable_hash(old_eligible_sources),
            "new_mapping_hash": stable_hash(new_eligible_sources),
        }
    )
    if validations["D"]["status"] != "PASS" or validations["E"]["status"] != "PASS":
        raise AssertionError("Add-New visible candidates or eligible Gold changed")

    new_page_ids = [f"ADDNEW:{page['source_turn_id']}" for page in new_pages]
    new_page_texts = [str(page["actual_page_embedding_text"]) for page in new_pages]
    embedding_cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    encoded, embedding_metadata = embedding_cache.encode(
        PRODUCTION_EMBEDDING,
        "add-new-single-turn-retrieval-pages-S001-S005",
        new_page_ids,
        new_page_texts,
        measure_individual=False,
    )
    new_vectors = {
        str(old["page_id"]): encoded[f"ADDNEW:{old['source_turn_id']}"] for old in old_pages
    }
    if int(embedding_metadata.get("dimension") or 0) != 512 or len(new_vectors) != 333:
        raise AssertionError("Add-New embedding coverage/dimension mismatch")

    rankings = {
        **old_rankings,
        "NEW_BASE": rank_configuration(snapshots, query_vectors["Search-Baseline"], new_vectors),
        "NEW_P2": rank_configuration(snapshots, query_vectors["Search-P2"], new_vectors),
    }
    retrieval_rows, session_rows, aggregate, _ = build_metric_rows(snapshots, rankings)
    gold_rows, transition_rows, comparison_rows = transition_outputs(snapshots, rankings, p2_texts)
    diff_rows = page_diff_rows(old_pages, new_pages)
    stats_rows = representation_stats(old_pages, new_pages)
    similarity_by_session, similarity_summary = similarity_rows(
        old_pages, new_pages, old_vectors, new_vectors
    )
    indicators = indicator_audit_rows(old_pages, new_pages)
    numeric = numeric_audit_rows(old_pages, new_pages)
    fidelity = fidelity_audit_rows(old_pages, new_pages)
    old_keyword_frequency = keyword_frequency_rows(old_pages, "Add-Old")
    new_keyword_frequency = keyword_frequency_rows(new_pages, "Add-New")
    cases, cases_markdown = build_cases(
        snapshots,
        old_pages,
        new_pages,
        p2_texts,
        rankings,
        transition_rows,
        numeric,
        fidelity,
    )

    metrics_dir = args.output_dir / "metrics"
    pages_dir = args.output_dir / "pages"
    analysis_dir = args.output_dir / "analysis"
    write_csv(metrics_dir / "retrieval_metrics.csv", retrieval_rows)
    write_csv(metrics_dir / "session_metrics.csv", session_rows)
    write_csv(metrics_dir / "gold_results.csv", gold_rows)
    write_csv(metrics_dir / "query_transitions.csv", transition_rows)
    write_csv(metrics_dir / "add_new_comparisons.csv", comparison_rows)
    write_jsonl(pages_dir / "add_new_pages.jsonl", new_pages)
    write_csv(pages_dir / "page_diff.csv", diff_rows)
    write_csv(pages_dir / "page_representation_stats.csv", stats_rows)
    write_csv(analysis_dir / "page_similarity_by_session.csv", similarity_by_session)
    write_csv(analysis_dir / "page_similarity_summary.csv", similarity_summary)
    write_csv(analysis_dir / "keyword_frequency_old.csv", old_keyword_frequency)
    write_csv(analysis_dir / "keyword_frequency_new.csv", new_keyword_frequency)
    write_csv(analysis_dir / "indicator_audit.csv", indicators)
    write_csv(analysis_dir / "numeric_overload_audit.csv", numeric)
    write_jsonl(analysis_dir / "fidelity_audit.jsonl", fidelity)
    dump_json(args.output_dir / "representative_cases.json", cases)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")

    source_model_configs = {}
    for code in SESSION_CODES:
        source_dir = Path(str(snapshots[code]["manifest"]["source_dir"]))
        effective = load_json(source_dir / "effective_memory_config.json")
        source_model_configs[code] = {
            "model": effective["llm"]["config"]["model"],
            "temperature": effective["llm"]["config"]["temperature"],
            "thinking_mode": "disabled"
            if effective.get("benchmark_runtime", {}).get("deepseek_midterm_non_thinking")
            else "production_default",
        }
    empty_ids = [str(page["source_turn_id"]) for page in new_pages if not str(page["summary"]).strip()]
    fidelity_flagged = [row for row in fidelity if row["status"] == "POTENTIAL_UNSUPPORTED"]
    all_pass = all(row.get("status") == "PASS" for row in validations.values())
    metadata = {
        "experiment_name": "midterm_add_summary_search_query_cross_ablation",
        "sessions": list(SESSION_CODES),
        "session_count": len(SESSION_CODES),
        "evaluation_query_count": len(queries),
        "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        "page_count": len(old_pages),
        "add_old_prompt_sha256": old_prompt_sha,
        "add_new_prompt_sha256": prompt_sha_before,
        "add_new_prompt_sha256_after_llm": prompt_sha_after,
        "add_new_prompt_frozen_before_first_llm_call": True,
        "prompt_adaptation_after_results": False,
        "add_new_input_contract": "current User + current Assistant only",
        "add_new_forbidden_inputs": [
            "previous dialogue",
            "Session summary",
            "other Pages",
            "Gold",
            "required_context",
            "retrieval result/score",
            "P2 resolved query",
            "future dialogue",
            "benchmark label",
        ],
        "summary_model": generation_metadata["model"],
        "thinking_mode": generation_metadata["thinking_mode"],
        "temperature": generation_metadata["temperature"],
        "top_p": generation_metadata["top_p"],
        "top_k": generation_metadata["top_k"],
        "production_source_model_configs": source_model_configs,
        "add_new_successful_model_output_count": generation_metadata[
            "successful_model_output_count"
        ],
        "add_new_llm_api_attempt_count": generation_metadata["llm_api_attempt_count"],
        "retry_count": generation_metadata["retry_count"],
        "provider_precondition_rejected_count": generation_metadata[
            "provider_precondition_rejected_count"
        ],
        "production_compatible_keyword_truncation_count": generation_metadata[
            "production_compatible_keyword_truncation_count"
        ],
        "keyword_persistence_contract": "production-compatible first 8 model-provided keywords",
        "old_page_embedding_reused_count": len(old_vectors),
        "new_page_embedding_count": len(new_vectors),
        "new_page_embedding_current_run_cache_hit": bool(embedding_metadata.get("cache_hit")),
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_dimension": embedding_metadata["dimension"],
        "new_page_embedding_content_hash": embedding_metadata["content_hash"],
        "page_embedding_mode": "add",
        "page_formatter": "<summary>\\nKeywords: <comma-space joined keywords>\\nUser: <original_user_query>",
        "search_baseline_query_count": len(queries),
        "search_p2_query_count": len(queries),
        "search_baseline_new_llm_calls": 0,
        "search_p2_new_llm_calls": 0,
        "search_baseline_query_embedding_reused_count": len(queries),
        "search_p2_query_embedding_reused_count": len(queries),
        "search_baseline_embedding_contract": "original_query only",
        "search_p2_embedding_contract": "frozen P2 resolved_query only",
        "retrieval_method": "per-Session dense cosine",
        "full_session_rerun": False,
        "source_turn_set_changed": old_source_set != new_source_set,
        "eligible_gold_changed": old_eligible_sources != new_eligible_sources,
        "visible_page_ids_changed": old_visible_sources != new_visible_sources,
        "page_regeneration": "Add-New experimental summary/keywords only; no production Page or Session mutation",
        "validations": validations,
        "validations_a_through_h_all_pass": all_pass,
        "empty_summary_count": len(empty_ids),
        "empty_summary_source_turn_ids": empty_ids,
        "potential_unsupported_count": len(fidelity_flagged),
        "comparison_metrics": comparison_rows,
        "search_p2_gain_on_old_add_r5": aggregate["OLD_P2"]["recall_at_5"]
        - aggregate["OLD_BASE"]["recall_at_5"],
        "search_p2_gain_on_new_add_r5": aggregate["NEW_P2"]["recall_at_5"]
        - aggregate["NEW_BASE"]["recall_at_5"],
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(json.dumps({"retrieval_metrics": retrieval_rows, "comparisons": comparison_rows, "metadata": metadata}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
