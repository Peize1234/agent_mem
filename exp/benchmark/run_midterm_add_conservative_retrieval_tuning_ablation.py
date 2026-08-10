"""Compare frozen Production Add Pages with a conservatively tuned Add Prompt.

Only the tuned summary/keywords and their Page embeddings are newly generated.
The formatter, source turns, visibility, Gold, query texts/vectors, embedding
model, and dense cosine retrieval remain frozen.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import (
    ensure_repo_root_on_path,
    expand_env_placeholders,
    load_json,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_prompt_multi_ablation import (  # noqa: E402
    COMPANIES,
    extract_dependencies,
    extract_tasks,
    state_set,
)
from exp.benchmark.run_midterm_page_representation_decomposition import tokenizer_audit  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    NUMBER_PATTERN,
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    YEAR_PATTERN,
    evaluate_all,
    extract_indicators,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    page_text,
    rank_configuration,
    regex_values,
    sha256_file,
    validate_old_reproduction,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_conservative_retrieval_tuning_ablation"
ORIGINAL_PROMPT_FILE = "Original_Production_Add_Prompt.txt"
TUNED_PROMPT_FILE = "Conservative_Retrieval_Tuned_Add_Prompt.txt"
PROMPT_VERSION = "production-conservative-retrieval-tuning-v1"
VARIANTS = ("Production", "ConservativeTuned")
SEARCHES = ("OriginalQuery", "ResolvedQuery")
CONFIGS = tuple(f"{variant}_{search}" for variant in VARIANTS for search in SEARCHES)
VARIANT_LABELS = {
    "Production": "原 Production Add Prompt",
    "ConservativeTuned": "保守调优 Add Prompt",
}
SEARCH_LABELS = {
    "OriginalQuery": "原始问题直接检索",
    "ResolvedQuery": "上下文引用解析后检索",
}
CONFIG_LABELS = {
    f"{variant}_{search}": f"{VARIANT_LABELS[variant]} + {SEARCH_LABELS[search]}"
    for variant in VARIANTS
    for search in SEARCHES
}
DECISION_RULE = (
    "建议替换仅当调优 Prompt 在两种 Search 下的 Micro R@5 和 Macro R@5 均不低于 Production，"
    "且至少一种 Search 的 Micro R@5 严格提升，并且两种 Search 的 net Gold gain 均不为负。"
)
EVIDENCE_LIMIT_TERMS = {
    "证据限制": ("证据限制", "证据强度", "不足以", "不能据此", "仅能确认", "无法确认"),
    "信息缺口": ("信息缺口", "数据缺失", "缺少", "未披露", "待核验", "需核验"),
    "口径限制": ("口径", "完整财年", "季度信息", "同比口径", "可复算"),
    "来源限制": ("来源", "年报原数", "公开披露", "正式披露", "派生计算"),
}
RELATION_TERMS = {
    "同步": ("同步", "同向", "方向一致"),
    "不同步": ("不同步", "方向相反", "方向冲突", "方向不同"),
    "错位": ("错位",),
    "比较": ("比较", "对比", "差异"),
    "同比": ("同比",),
    "反证": ("反证", "反例", "削弱"),
    "修订": ("修订", "修正", "降级", "缩小结论"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production vs conservative retrieval-oriented Add Prompt")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def production_dialogue(page: Mapping[str, Any]) -> str:
    user = str(page.get("user_input") or "")
    assistant = str(page.get("assistant_response") or "")
    return f"User:{' ' if user else ''}{user}\n\nAssistant:{' ' if assistant else ''}{assistant}"


def summary_validator(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != {"summary", "keywords"}:
        raise ValueError("response must contain exactly summary and keywords")
    summary = parsed["summary"]
    keywords = parsed["keywords"]
    if not isinstance(summary, str) or not isinstance(keywords, list):
        raise ValueError("invalid summary/keywords types")
    if any(not isinstance(item, str) or not item.strip() for item in keywords):
        raise ValueError("keywords must be non-empty strings")
    cleaned = [item.strip() for item in keywords]
    if not summary.strip() and cleaned:
        raise ValueError("empty summary requires empty keywords")
    return {"summary": summary.strip(), "keywords": cleaned[:8]}


def provider_precondition(exc: Exception) -> bool:
    value = str(exc).lower()
    return "precondition" in value or ("response_format" in value and "json" in value)


class TunedSummaryCache:
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
        prompt_version: str = PROMPT_VERSION,
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
        self.prompt_version = prompt_version
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.lock = asyncio.Lock()
        rows = load_jsonl(path)
        self.success = {str(row["cache_key"]): row for row in rows if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(self, page: Mapping[str, Any], prompt: str, prompt_sha: str) -> dict[str, Any]:
        payload = production_dialogue(page)
        frozen_raw = str(page.get("raw_dialogue") or "")
        if payload != frozen_raw:
            raise AssertionError(f"Production dialogue mismatch: {page['source_turn_id']}")
        identity = {
            "session_id": str(page["session_code"]),
            "source_turn_id": str(page["source_turn_id"]),
            "prompt_version": self.prompt_version,
            "prompt_sha256": prompt_sha,
            "source_user_sha256": sha256_text(str(page["user_input"])),
            "source_assistant_sha256": sha256_text(str(page["assistant_response"])),
            "raw_dialogue_sha256": sha256_text(payload),
            "model": self.model,
            "thinking_mode": "disabled",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }
        key = stable_hash(identity)
        if key in self.success:
            return self.success[key]
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": payload}]
        errors: list[str] = []
        rejects = 0
        response_format = True
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
                    if response_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs),
                        timeout=self.timeout,
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = summary_validator(raw)
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": key,
                        "input_contract": "exact production raw dialogue: User + Assistant only",
                        "input_payload_sha256": sha256_text(payload),
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": raw,
                        "response_mode": "json_object" if response_format else "plain_text_strict_json",
                        "llm_latency_ms": (time.perf_counter() - started) * 1000,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "api_attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "provider_precondition_rejected_count": rejects,
                        "errors": errors,
                    }
                    await self.append(row)
                    self.success[key] = row
                    return row
                except Exception as exc:
                    rejected = provider_precondition(exc)
                    rejects += int(rejected)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if rejected and response_format:
                        response_format = False
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12))
        row = {
            **identity,
            "cache_key": key,
            "input_contract": "exact production raw dialogue: User + Assistant only",
            "input_payload_sha256": sha256_text(payload),
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": self.retries - 1,
            "provider_precondition_rejected_count": rejects,
            "errors": errors,
        }
        await self.append(row)
        return row


def freeze_prompts(output_dir: Path) -> tuple[str, str, dict[str, str]]:
    prompt_dir = output_dir / "prompts"
    old_path = prompt_dir / ORIGINAL_PROMPT_FILE
    tuned_path = prompt_dir / TUNED_PROMPT_FILE
    if not old_path.exists() or not tuned_path.exists():
        raise FileNotFoundError("Frozen prompt files are required before generation")
    old_prompt = old_path.read_text(encoding="utf-8")
    tuned_prompt = tuned_path.read_text(encoding="utf-8")
    if old_prompt.strip() != MIDTERM_PAGE_SUMMARY_PROMPT.strip():
        raise AssertionError("Saved Production Prompt does not match repository constant")
    hashes = {"Production": sha256_file(old_path), "ConservativeTuned": sha256_file(tuned_path)}
    diff = "\n".join(
        difflib.unified_diff(
            old_prompt.splitlines(),
            tuned_prompt.splitlines(),
            fromfile=ORIGINAL_PROMPT_FILE,
            tofile=TUNED_PROMPT_FILE,
            lineterm="",
        )
    )
    (prompt_dir / "prompt_diff.patch").write_text(diff + "\n", encoding="utf-8")
    dump_json(
        prompt_dir / "prompt_sha256.json",
        {
            "frozen_before_first_llm_call": True,
            "hash_contract": "SHA256 of exact UTF-8 file bytes",
            "Production": {"file": ORIGINAL_PROMPT_FILE, "sha256": hashes["Production"]},
            "ConservativeTuned": {
                "file": TUNED_PROMPT_FILE,
                "sha256": hashes["ConservativeTuned"],
            },
        },
    )
    return old_prompt, tuned_prompt, hashes


def validate_frozen_before_generation(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    prompt_hashes: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in pages}
    reproduction, old_rankings = validate_old_reproduction(snapshots, old_vectors, query_vectors)
    page_ids = {str(page["page_id"]) for page in pages}
    source_turns = {str(page["source_turn_id"]) for page in pages}
    query_ids = {str(query["query_id"]) for query in queries}
    visible = {
        str(row["query_id"]): [str(value) for value in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    exact_dialogue = all(production_dialogue(page) == str(page["raw_dialogue"]) for page in pages)
    checks = {
        "production_original_query_reproduction": reproduction["A"],
        "production_resolved_query_reproduction": reproduction["B"],
        "frozen_coverage": {
            "status": "PASS"
            if len(pages) == len(page_ids) == len(source_turns) == 333
            and len(queries) == len(query_ids) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == 154
            else "FAIL",
            "page_count": len(pages),
            "query_count": len(queries),
            "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        },
        "visible_page_ids": {
            "status": "PASS"
            if len(visible) == 99 and all(set(values) <= page_ids for values in visible.values())
            else "FAIL",
            "mapping_sha256": stable_hash(visible),
        },
        "query_texts_and_vectors": {
            "status": "PASS"
            if set(p2_texts) == query_ids
            and len(query_vectors["Search-Baseline"]) == 99
            and len(query_vectors["Search-P2"]) == 99
            else "FAIL",
            "original_query_contract": "original_query exactly",
            "resolved_query_contract": "frozen P2 resolved_query only",
        },
        "production_input_contract": {
            "status": "PASS" if exact_dialogue else "FAIL",
            "contract": "User:<user_input>\\n\\nAssistant:<assistant_response>",
            "previous_dialogue_included": False,
        },
        "prompt_frozen": {
            "status": "PASS" if len(prompt_hashes) == 2 else "FAIL",
            "sha256": dict(prompt_hashes),
        },
        "new_llm_scope": {
            "status": "PASS",
            "Production_calls": 0,
            "Search_calls": 0,
            "ConservativeTuned_expected_outputs": 333,
        },
    }
    if any(row["status"] != "PASS" for row in checks.values()):
        raise AssertionError(checks)
    return checks, old_rankings


async def generate_tuned(
    args: argparse.Namespace,
    pages: Sequence[Mapping[str, Any]],
    prompt: str,
    prompt_sha: str,
    llm_config: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache_path = args.output_dir / "cache/conservative_tuned_summary_llm.jsonl"
    cache = TunedSummaryCache(
        cache_path,
        client=client,
        model=str(llm_config["model"]),
        temperature=float(llm_config["temperature"]),
        top_p=float(llm_config["top_p"]),
        top_k=int(llm_config["top_k"]),
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )
    initial = set(cache.success)
    try:
        rows = await asyncio.gather(*(cache.call(page, prompt, prompt_sha) for page in pages))
    finally:
        await client.close()
    failures = [row for row in rows if row.get("status") != "SUCCESS"]
    if failures:
        raise RuntimeError(f"Tuned Summary generation failed for {len(failures)} Pages")
    expected_keys = {str(row["cache_key"]) for row in rows}
    cache_rows = [row for row in load_jsonl(cache_path) if str(row.get("cache_key")) in expected_keys]
    attempts = sum(int(row.get("api_attempt_count") or 0) for row in cache_rows)
    raw_keyword_truncations = sum(len(json.loads(str(row["raw_output"])).get("keywords") or []) > 8 for row in rows)
    return {str(row["source_turn_id"]): row for row in rows}, {
        "successful_output_count": len(rows),
        "actual_api_attempts": attempts,
        "retry_count": attempts - len(rows),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0) for row in cache_rows
        ),
        "cache_hit_count": sum(str(row["cache_key"]) in initial for row in rows),
        "raw_keyword_truncation_count": raw_keyword_truncations,
        "cache_path": str(cache_path),
    }


def build_pages(
    old_pages: Sequence[Mapping[str, Any]],
    tuned: Mapping[str, Mapping[str, Any]],
    prompt_hashes: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {variant: [] for variant in VARIANTS}
    for old in old_pages:
        common = {
            "session_id": old["session_code"],
            "full_session_id": old["session_id"],
            "source_turn_id": old["source_turn_id"],
            "source_turn_index": old["source_turn_index"],
            "old_page_id": old["page_id"],
            "source_user": old["user_input"],
            "source_assistant": old["assistant_response"],
            "raw_dialogue": old["raw_dialogue"],
        }
        result["Production"].append(
            {
                **common,
                "summary": old["summary"],
                "keywords": old["keywords"],
                "embedding_text": old["current_embedding_text"],
                "prompt_sha256": prompt_hashes["Production"],
            }
        )
        generated = tuned[str(old["source_turn_id"])]
        parsed = generated["parsed"]
        summary = str(parsed["summary"])
        keywords = list(parsed["keywords"])
        result["ConservativeTuned"].append(
            {
                **common,
                "summary": summary,
                "keywords": keywords,
                "embedding_text": page_text(summary, keywords, str(old["user_input"])),
                "prompt_sha256": prompt_hashes["ConservativeTuned"],
                "llm_cache_key": generated["cache_key"],
            }
        )
    return result


def canonical_matches(text: str, rules: Mapping[str, Sequence[str]]) -> list[str]:
    return sorted(label for label, aliases in rules.items() if any(alias in str(text) for alias in aliases))


def anchors(summary: str, keywords: Sequence[str]) -> dict[str, list[str]]:
    keyword_text = " ".join(str(value) for value in keywords)
    text = f"{summary}\n{keyword_text}"
    return {
        "TASK": sorted(extract_tasks(text)),
        "SUBJECT": sorted(company for company in COMPANIES if company in text),
        "TIME": sorted(set(regex_values(YEAR_PATTERN, text))),
        "INDICATOR": sorted(extract_indicators(text)),
        "NUMBER": sorted(set(regex_values(NUMBER_PATTERN, text))),
        "STATE": sorted(state_set(text)),
        "RELATION": canonical_matches(text, RELATION_TERMS),
        "DEPENDENCY": sorted(extract_dependencies(text)),
        "EVIDENCE_LIMIT": canonical_matches(text, EVIDENCE_LIMIT_TERMS),
        "KEYWORD": sorted(set(str(value) for value in keywords)),
    }


def anchor_diff(old: Mapping[str, Sequence[str]], new: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for category in old:
        old_values = set(old[category])
        new_values = set(new[category])
        result[f"{category}_added"] = sorted(new_values - old_values)
        result[f"{category}_lost"] = sorted(old_values - new_values)
        result[f"{category}_preserved"] = sorted(old_values & new_values)
    return result


def representation_analysis(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    stats: list[dict[str, Any]] = []
    for variant in VARIANTS:
        selected = pages[variant]
        stats.append(
            {
                "Add Prompt": VARIANT_LABELS[variant],
                "page_count": len(selected),
                "summary_length_chars_mean": statistics.fmean(len(str(row["summary"])) for row in selected),
                "summary_length_chars_median": statistics.median(len(str(row["summary"])) for row in selected),
                "summary_indicator_count_mean": statistics.fmean(
                    len(extract_indicators(str(row["summary"]))) for row in selected
                ),
                "summary_numeric_count_mean": statistics.fmean(
                    len(regex_values(NUMBER_PATTERN, str(row["summary"]))) for row in selected
                ),
                "keyword_count_mean": statistics.fmean(len(row["keywords"]) for row in selected),
            }
        )
    page_diff_rows: list[dict[str, Any]] = []
    keyword_rows: list[dict[str, Any]] = []
    anchor_rows: list[dict[str, Any]] = []
    fidelity_rows: list[dict[str, Any]] = []
    keyword_added = Counter()
    keyword_lost = Counter()
    exact_keyword_matches = 0
    jaccards: list[float] = []
    for source in pages["Production"]:
        turn = str(source["source_turn_id"])
        tuned = by_turn["ConservativeTuned"][turn]
        old_keywords = [str(value) for value in source["keywords"]]
        new_keywords = [str(value) for value in tuned["keywords"]]
        old_set, new_set = set(old_keywords), set(new_keywords)
        added_keywords = sorted(new_set - old_set)
        lost_keywords = sorted(old_set - new_set)
        keyword_added.update(added_keywords)
        keyword_lost.update(lost_keywords)
        exact_keyword_matches += int(old_keywords == new_keywords)
        union = old_set | new_set
        jaccard = len(old_set & new_set) / len(union) if union else 1.0
        jaccards.append(jaccard)
        old_anchors = anchors(str(source["summary"]), old_keywords)
        new_anchors = anchors(str(tuned["summary"]), new_keywords)
        diff = anchor_diff(old_anchors, new_anchors)
        page_diff_rows.append(
            {
                "session_id": source["session_id"],
                "source_turn_id": turn,
                "old_summary": source["summary"],
                "new_summary": tuned["summary"],
                "old_keywords": old_keywords,
                "new_keywords": new_keywords,
                "old_embedding_text": source["embedding_text"],
                "new_embedding_text": tuned["embedding_text"],
                "summary_char_delta": len(str(tuned["summary"])) - len(str(source["summary"])),
                "indicator_count_delta": len(extract_indicators(str(tuned["summary"])))
                - len(extract_indicators(str(source["summary"]))),
                "numeric_count_delta": len(regex_values(NUMBER_PATTERN, str(tuned["summary"])))
                - len(regex_values(NUMBER_PATTERN, str(source["summary"]))),
            }
        )
        keyword_rows.append(
            {
                "session_id": source["session_id"],
                "source_turn_id": turn,
                "old_keywords": old_keywords,
                "new_keywords": new_keywords,
                "added_keywords": added_keywords,
                "lost_keywords": lost_keywords,
                "preserved_keywords": sorted(old_set & new_set),
                "keyword_jaccard": jaccard,
                "exact_ordered_match": old_keywords == new_keywords,
            }
        )
        anchor_rows.append(
            {
                "session_id": source["session_id"],
                "source_turn_id": turn,
                "old_anchors": old_anchors,
                "new_anchors": new_anchors,
                **diff,
            }
        )
        source_text = f"{source['source_user']}\n{source['source_assistant']}"
        source_indicators = set(extract_indicators(source_text))
        source_years = set(regex_values(YEAR_PATTERN, source_text))
        source_numbers = set(regex_values(NUMBER_PATTERN, source_text))
        new_text = f"{tuned['summary']}\n{' '.join(new_keywords)}"
        unsupported = {
            "indicators": sorted(set(extract_indicators(new_text)) - source_indicators),
            "years": sorted(set(regex_values(YEAR_PATTERN, new_text)) - source_years),
            "numbers": sorted(set(regex_values(NUMBER_PATTERN, new_text)) - source_numbers),
        }
        fidelity_rows.append(
            {
                "session_id": source["session_id"],
                "source_turn_id": turn,
                "status": "POTENTIAL_UNSUPPORTED" if any(unsupported.values()) else "NO_FLAG",
                "unsupported": unsupported,
                "audit_scope": "deterministic comparison against current User + Assistant only; no Gold/rank",
            }
        )
    keyword_summary = {
        "Production": {"mean_keyword_count": statistics.fmean(len(row["keywords"]) for row in pages["Production"])},
        "ConservativeTuned": {
            "mean_keyword_count": statistics.fmean(len(row["keywords"]) for row in pages["ConservativeTuned"])
        },
        "exact_ordered_keyword_match_count": exact_keyword_matches,
        "mean_keyword_jaccard": statistics.fmean(jaccards),
        "total_added_keyword_instances": sum(keyword_added.values()),
        "total_lost_keyword_instances": sum(keyword_lost.values()),
        "top_added_keywords": keyword_added.most_common(30),
        "top_lost_keywords": keyword_lost.most_common(30),
    }
    return stats, page_diff_rows, keyword_rows, anchor_rows, fidelity_rows, keyword_summary


def build_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    page_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    old_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    rankings = {
        "Production_OriginalQuery": dict(old_rankings["OLD_BASE"]),
        "Production_ResolvedQuery": dict(old_rankings["OLD_P2"]),
    }
    rankings["ConservativeTuned_OriginalQuery"] = rank_configuration(
        snapshots,
        query_vectors["Search-Baseline"],
        page_vectors["ConservativeTuned"],
    )
    rankings["ConservativeTuned_ResolvedQuery"] = rank_configuration(
        snapshots,
        query_vectors["Search-P2"],
        page_vectors["ConservativeTuned"],
    )
    return rankings


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row, session_code=code) for code in SESSION_CODES for row in snapshots[code]["queries"]]


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
    maps = {
        config: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for config, by_query in rankings.items()
    }
    retrieval_rows: list[dict[str, Any]] = []
    session_rows: list[dict[str, Any]] = []
    gold_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []
    aggregate: dict[str, dict[str, Any]] = {}
    for config in CONFIGS:
        variant, search = config.split("_", 1)
        overall, sessions = evaluate_all(snapshots, rankings[config])
        aggregate[config] = overall
        base_config = f"Production_{search}"
        promoted = demoted = rescued = hurt = 0
        for query in queries:
            query_id = str(query["query_id"])
            gold_ids = [str(value) for value in query["eligible_gold_page_ids"]]
            base_ranks = {gold_id: int(maps[base_config][query_id][gold_id]["rank"]) for gold_id in gold_ids}
            ranks = {gold_id: int(maps[config][query_id][gold_id]["rank"]) for gold_id in gold_ids}
            base_hit = any(value <= 5 for value in base_ranks.values())
            hit = any(value <= 5 for value in ranks.values())
            query_promoted = sum(base_ranks[gold_id] > 5 and ranks[gold_id] <= 5 for gold_id in gold_ids)
            query_demoted = sum(base_ranks[gold_id] <= 5 and ranks[gold_id] > 5 for gold_id in gold_ids)
            promoted += query_promoted
            demoted += query_demoted
            transition = (
                "RESCUED"
                if not base_hit and hit
                else "HURT"
                if base_hit and not hit
                else "UNCHANGED_HIT"
                if base_hit
                else "UNCHANGED_MISS"
            )
            rescued += int(transition == "RESCUED")
            hurt += int(transition == "HURT")
            if variant == "ConservativeTuned":
                transition_rows.append(
                    {
                        "Session": query["session_code"],
                        "Query ID": query_id,
                        "Search 方案": SEARCH_LABELS[search],
                        "对照": f"{VARIANT_LABELS[variant]} 相对 {VARIANT_LABELS['Production']}",
                        "Transition": transition,
                        "原 Production Gold ranks": base_ranks,
                        "保守调优 Gold ranks": ranks,
                        "promoted_gold": query_promoted,
                        "demoted_gold": query_demoted,
                        "max_promotion": max(base_ranks[gold] - ranks[gold] for gold in gold_ids),
                        "max_demotion": max(ranks[gold] - base_ranks[gold] for gold in gold_ids),
                    }
                )
            for gold_id in gold_ids:
                item = maps[config][query_id][gold_id]
                gold_rows.append(
                    {
                        "Session": query["session_code"],
                        "Query ID": query_id,
                        "Gold Page ID": gold_id,
                        "Gold source_turn_id": item["source_turn_id"],
                        "完整实验配置": CONFIG_LABELS[config],
                        "Add Prompt": VARIANT_LABELS[variant],
                        "Search 方案": SEARCH_LABELS[search],
                        "Gold rank": int(item["rank"]),
                        "Gold score": float(item["score"]),
                        "Gold hit Top5": int(item["rank"]) <= 5,
                    }
                )
        retrieval_rows.append(
            {
                "完整实验配置": CONFIG_LABELS[config],
                "Add Prompt": VARIANT_LABELS[variant],
                "Search 方案": SEARCH_LABELS[search],
                "Eligible Gold": overall["eligible_gold_count"],
                "Top5 recalled Gold": round(float(overall["recall_at_5"]) * int(overall["eligible_gold_count"])),
                "Micro R@5": overall["recall_at_5"],
                "Macro R@5": overall["macro_session_r5"],
                "R@10": overall["recall_at_10"],
                "R@20": overall["recall_at_20"],
                "MRR": overall["mrr"],
                "Mean Gold Rank": overall["mean_gold_rank"],
                "Promoted Gold vs Production": promoted,
                "Demoted Gold vs Production": demoted,
                "Net Gold gain vs Production": promoted - demoted,
                "RESCUED Queries vs Production": rescued,
                "HURT Queries vs Production": hurt,
            }
        )
        for code in SESSION_CODES:
            value = sessions[code]
            session_rows.append(
                {
                    "Session": code,
                    "完整实验配置": CONFIG_LABELS[config],
                    "Add Prompt": VARIANT_LABELS[variant],
                    "Search 方案": SEARCH_LABELS[search],
                    "R@5": value["recall_at_5"],
                    "R@10": value["recall_at_10"],
                    "R@20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    return retrieval_rows, session_rows, gold_rows, transition_rows, aggregate


def separation_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for config in CONFIGS:
        variant, search = config.split("_", 1)
        margins: list[float] = []
        gold_scores: list[float] = []
        nongold_scores: list[float] = []
        for query in all_queries(snapshots):
            query_id = str(query["query_id"])
            gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
            ranking = rankings[config][query_id]
            best_gold = max(float(item["score"]) for item in ranking if str(item["page_id"]) in gold_ids)
            best_nongold = max(float(item["score"]) for item in ranking if str(item["page_id"]) not in gold_ids)
            gold_scores.append(best_gold)
            nongold_scores.append(best_nongold)
            margins.append(best_gold - best_nongold)
        rows.append(
            {
                "完整实验配置": CONFIG_LABELS[config],
                "Add Prompt": VARIANT_LABELS[variant],
                "Search 方案": SEARCH_LABELS[search],
                "query_count": len(margins),
                "best_Gold_score_mean": statistics.fmean(gold_scores),
                "best_NonGold_score_mean": statistics.fmean(nongold_scores),
                "separation_margin_mean": statistics.fmean(margins),
                "separation_margin_median": statistics.median(margins),
                "positive_margin_rate": sum(value > 0 for value in margins) / len(margins),
                "margin_definition": "best visible eligible Gold score - best visible Non-Gold score",
            }
        )
    return rows


def transition_anchor_outputs(
    transition_rows: Sequence[Mapping[str, Any]],
    anchor_rows: Sequence[Mapping[str, Any]],
    snapshots: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    anchors_by_turn = {str(row["source_turn_id"]): row for row in anchor_rows}
    query_by_id = {str(row["query_id"]): row for row in all_queries(snapshots)}
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for transition in transition_rows:
        query = query_by_id[str(transition["Query ID"])]
        for gold_id in query["eligible_gold_page_ids"]:
            before = transition["原 Production Gold ranks"][str(gold_id)]
            after = transition["保守调优 Gold ranks"][str(gold_id)]
            movement = "PROMOTED" if before > 5 and after <= 5 else "DEMOTED" if before <= 5 and after > 5 else "OTHER"
            if movement == "OTHER":
                continue
            page = next(
                page
                for code in SESSION_CODES
                for page in snapshots[code]["pages"]
                if str(page["page_id"]) == str(gold_id)
            )
            grouped[(str(transition["Search 方案"]), movement)].append(anchors_by_turn[str(page["source_turn_id"])])
    rows = []
    for (search, movement), selected in sorted(grouped.items()):
        row: dict[str, Any] = {
            "Search 方案": search,
            "Gold movement": movement,
            "Gold count": len(selected),
        }
        for category in anchors("", []):
            added = Counter(value for item in selected for value in item[f"{category}_added"])
            lost = Counter(value for item in selected for value in item[f"{category}_lost"])
            row[f"{category} added instances"] = sum(added.values())
            row[f"{category} lost instances"] = sum(lost.values())
            row[f"{category} top added"] = added.most_common(10)
            row[f"{category} top lost"] = lost.most_common(10)
        rows.append(row)
    return rows


def select_cases(transition_rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    selected: list[str] = []
    categories: dict[str, list[str]] = {}
    for search in SEARCH_LABELS.values():
        for transition, movement in (("RESCUED", "max_promotion"), ("HURT", "max_demotion")):
            candidates = sorted(
                (row for row in transition_rows if row["Search 方案"] == search and row["Transition"] == transition),
                key=lambda row: (-int(row[movement]), str(row["Query ID"])),
            )[:5]
            category = f"{search} / {'调优救回' if transition == 'RESCUED' else '调优掉出 Top5'}"
            categories[category] = [str(row["Query ID"]) for row in candidates]
            for row in candidates:
                query_id = str(row["Query ID"])
                if query_id not in selected:
                    selected.append(query_id)
    return selected, categories


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    p2_texts: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    transition_rows: Sequence[Mapping[str, Any]],
    anchor_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selected, categories = select_cases(transition_rows)
    queries = {str(row["query_id"]): row for row in all_queries(snapshots)}
    by_variant_page = {variant: {str(row["old_page_id"]): row for row in pages[variant]} for variant in VARIANTS}
    anchor_by_turn = {str(row["source_turn_id"]): row for row in anchor_rows}
    maps = {
        config: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for config, by_query in rankings.items()
    }
    cases = []
    for query_id in selected:
        query = queries[query_id]
        gold_pages = []
        for gold_id_value in query["eligible_gold_page_ids"]:
            gold_id = str(gold_id_value)
            old = by_variant_page["Production"][gold_id]
            tuned = by_variant_page["ConservativeTuned"][gold_id]
            gold_pages.append(
                {
                    "gold_page_id": gold_id,
                    "gold_source_turn_id": old["source_turn_id"],
                    "old_summary": old["summary"],
                    "old_keywords": old["keywords"],
                    "new_summary": tuned["summary"],
                    "new_keywords": tuned["keywords"],
                    "retrieval_anchor_changes": {
                        key: value
                        for key, value in anchor_by_turn[str(old["source_turn_id"])].items()
                        if key.endswith("_added") or key.endswith("_lost")
                    },
                    "rank_score": {
                        CONFIG_LABELS[config]: {
                            "rank": int(maps[config][query_id][gold_id]["rank"]),
                            "score": float(maps[config][query_id][gold_id]["score"]),
                        }
                        for config in CONFIGS
                    },
                }
            )
        cases.append(
            {
                "query_id": query_id,
                "session_id": query["session_code"],
                "selection_categories": [name for name, ids in categories.items() if query_id in ids],
                "original_query": query["original_query"],
                "resolved_query": p2_texts[query_id],
                "gold_pages": gold_pages,
            }
        )
    payload = {"selection": categories, "cases": cases}
    lines = [
        "# Production Add Prompt vs 保守调优 Add Prompt — Representative Cases",
        "",
        "全部排名使用冻结的 query-time visible Pages 和 eligible Gold。",
        "",
        "## 自动选择",
        "",
        "```json",
        json.dumps(categories, ensure_ascii=False, indent=2),
        "```",
    ]
    for case in cases:
        lines.extend(
            (
                "",
                f"## {case['query_id']}",
                "",
                f"选择类别：{', '.join(case['selection_categories'])}",
                "",
                "### 当前原始 Query",
                "",
                "```text",
                str(case["original_query"]),
                "```",
                "",
                "### 上下文引用解析后 Query",
                "",
                "```text",
                str(case["resolved_query"]),
                "```",
            )
        )
        for gold in case["gold_pages"]:
            lines.extend(
                (
                    "",
                    f"### Gold Page：{gold['gold_source_turn_id']} ({gold['gold_page_id']})",
                    "",
                    "#### 原 Production summary",
                    "",
                    "```text",
                    str(gold["old_summary"]),
                    "```",
                    "",
                    "#### 原 Production keywords",
                    "",
                    "```text",
                    json.dumps(gold["old_keywords"], ensure_ascii=False),
                    "```",
                    "",
                    "#### 保守调优 summary",
                    "",
                    "```text",
                    str(gold["new_summary"]),
                    "```",
                    "",
                    "#### 保守调优 keywords",
                    "",
                    "```text",
                    json.dumps(gold["new_keywords"], ensure_ascii=False),
                    "```",
                    "",
                    "#### Gold rank / score",
                    "",
                    "| 完整实验配置 | Rank | Score |",
                    "|---|---:|---:|",
                )
            )
            for label, value in gold["rank_score"].items():
                lines.append(f"| {label} | #{value['rank']} | {value['score']:.9f} |")
            lines.extend(
                (
                    "",
                    "#### 增加或丢失的 retrieval anchors",
                    "",
                    "```json",
                    json.dumps(gold["retrieval_anchor_changes"], ensure_ascii=False, indent=2),
                    "```",
                )
            )
    return payload, "\n".join(lines) + "\n"


def replacement_decision(
    retrieval_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    by_config = {str(row["完整实验配置"]): row for row in retrieval_rows}
    conditions: dict[str, bool] = {}
    strict_micro_improvement = False
    for search in SEARCHES:
        old = by_config[CONFIG_LABELS[f"Production_{search}"]]
        tuned = by_config[CONFIG_LABELS[f"ConservativeTuned_{search}"]]
        conditions[f"{SEARCH_LABELS[search]} Micro R@5 non-decreasing"] = float(tuned["Micro R@5"]) >= float(
            old["Micro R@5"]
        )
        conditions[f"{SEARCH_LABELS[search]} Macro R@5 non-decreasing"] = float(tuned["Macro R@5"]) >= float(
            old["Macro R@5"]
        )
        conditions[f"{SEARCH_LABELS[search]} net Gold gain non-negative"] = (
            int(tuned["Net Gold gain vs Production"]) >= 0
        )
        strict_micro_improvement |= float(tuned["Micro R@5"]) > float(old["Micro R@5"])
    conditions["at least one strict Micro R@5 improvement"] = strict_micro_improvement
    recommend = all(conditions.values())
    return {
        "decision_rule_frozen_before_results": DECISION_RULE,
        "conditions": conditions,
        "recommend_replace_production": recommend,
        "conclusion": "值得替换 Production" if recommend else "不值得替换 Production",
    }


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    old_prompt, tuned_prompt, prompt_hashes = freeze_prompts(args.output_dir)
    snapshots, old_pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    validations, old_rankings = validate_frozen_before_generation(
        snapshots,
        old_pages,
        queries,
        p2_texts,
        query_vectors,
        prompt_hashes,
    )
    config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict(config["llm"]["config"])
    if not llm_config.get("api_key") or str(llm_config["api_key"]).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY required")
    if (
        str(llm_config["model"]) != "deepseek-v4-flash"
        or not math.isclose(float(llm_config["temperature"]), 0.1, abs_tol=1e-12)
        or not math.isclose(float(llm_config["top_p"]), 0.1, abs_tol=1e-12)
        or int(llm_config["top_k"]) != 1
    ):
        raise AssertionError("Production Summary model configuration changed")
    tuned, generation_metadata = await generate_tuned(
        args,
        old_pages,
        tuned_prompt,
        prompt_hashes["ConservativeTuned"],
        llm_config,
    )
    after_hashes = {
        "Production": sha256_file(args.output_dir / "prompts" / ORIGINAL_PROMPT_FILE),
        "ConservativeTuned": sha256_file(args.output_dir / "prompts" / TUNED_PROMPT_FILE),
    }
    validations["prompt_hash_after_generation"] = {
        "status": "PASS" if after_hashes == prompt_hashes else "FAIL",
        "before": prompt_hashes,
        "after": after_hashes,
    }
    pages = build_pages(old_pages, tuned, prompt_hashes)
    page_sets = {variant: {str(row["source_turn_id"]) for row in values} for variant, values in pages.items()}
    formatter_valid = all(
        str(row["embedding_text"]) == page_text(str(row["summary"]), row["keywords"], str(row["source_user"]))
        for row in pages["ConservativeTuned"]
    )
    validations["post_generation_page_identity_and_formatter"] = {
        "status": "PASS"
        if page_sets["Production"] == page_sets["ConservativeTuned"]
        and len(page_sets["Production"]) == 333
        and formatter_valid
        else "FAIL",
        "source_turn_counts": {key: len(value) for key, value in page_sets.items()},
        "formatter": "<summary>\\nKeywords: <keywords>\\nUser: <original_user>",
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    embedding_cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    ids = [f"ConservativeTuned:{row['source_turn_id']}" for row in pages["ConservativeTuned"]]
    texts = [str(row["embedding_text"]) for row in pages["ConservativeTuned"]]
    encoded, embedding_metadata = embedding_cache.encode(
        PRODUCTION_EMBEDDING,
        "conservative-tuned-pages-S001-S005",
        ids,
        texts,
        measure_individual=False,
    )
    tuned_vectors = {
        str(row["old_page_id"]): encoded[f"ConservativeTuned:{row['source_turn_id']}"]
        for row in pages["ConservativeTuned"]
    }
    if len(tuned_vectors) != 333 or int(embedding_metadata["dimension"]) != 512:
        raise AssertionError("Tuned Page embedding mismatch")
    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    tokenizer = embedder.model.tokenizer
    max_length = int(embedder.model.max_seq_length)
    tokenizer_rows: list[dict[str, Any]] = []
    tokenizer_summaries: dict[str, dict[str, Any]] = {}
    for variant in VARIANTS:
        audit_pages = [
            {
                "session_code": row["session_id"],
                "source_turn_id": row["source_turn_id"],
                "page_id": row["old_page_id"],
                "summary": row["summary"],
                "keywords": row["keywords"],
                "user_input": row["source_user"],
                "current_embedding_text": row["embedding_text"],
            }
            for row in pages[variant]
        ]
        variant_rows, variant_summary = tokenizer_audit(audit_pages, tokenizer, max_length)
        for row in variant_rows:
            tokenizer_rows.append({"Add Prompt": VARIANT_LABELS[variant], **row})
        tokenizer_summaries[VARIANT_LABELS[variant]] = variant_summary
    vectors = {"Production": old_vectors, "ConservativeTuned": tuned_vectors}
    rankings = build_rankings(snapshots, query_vectors, vectors, old_rankings)
    retrieval, sessions, gold, transitions, aggregate = metric_outputs(snapshots, rankings)
    separation = separation_outputs(snapshots, rankings)
    stats, page_diffs, keyword_diffs, anchor_diffs, fidelity, keyword_summary = representation_analysis(pages)
    transition_anchors = transition_anchor_outputs(transitions, anchor_diffs, snapshots)
    cases_json, cases_markdown = representative_outputs(
        snapshots,
        pages,
        p2_texts,
        rankings,
        transitions,
        anchor_diffs,
    )
    decision = replacement_decision(retrieval)
    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", retrieval)
    write_csv(args.output_dir / "metrics/session_metrics.csv", sessions)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold)
    write_csv(args.output_dir / "metrics/query_transitions.csv", transitions)
    write_csv(args.output_dir / "metrics/gold_nongold_separation.csv", separation)
    write_csv(args.output_dir / "pages/page_diff.csv", page_diffs)
    write_csv(args.output_dir / "pages/representation_stats.csv", stats)
    write_csv(args.output_dir / "analysis/keyword_diff.csv", keyword_diffs)
    write_csv(args.output_dir / "analysis/retrieval_anchor_diff.csv", anchor_diffs)
    write_csv(args.output_dir / "analysis/transition_anchor_summary.csv", transition_anchors)
    write_csv(args.output_dir / "analysis/fidelity_audit.csv", fidelity)
    write_csv(args.output_dir / "analysis/tokenizer_truncation_comparison.csv", tokenizer_rows)
    from exp.benchmark.midterm_retrieval_eval import write_jsonl

    write_jsonl(args.output_dir / "pages/conservative_tuned_pages.jsonl", pages["ConservativeTuned"])
    dump_json(args.output_dir / "analysis/keyword_summary.json", keyword_summary)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    metadata = {
        "experiment_name": "midterm_add_conservative_retrieval_tuning_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "human_facing_config_labels": CONFIG_LABELS,
        "Production_prompt_sha256_before": prompt_hashes["Production"],
        "Conservative_Tuned_prompt_sha256_before": prompt_hashes["ConservativeTuned"],
        "prompt_sha256_after_generation": after_hashes,
        "prompt_frozen": after_hashes == prompt_hashes,
        "prompt_changed_after_results": False,
        "input_contract": "exact production User + Assistant raw dialogue only",
        "output_contract": {"summary": "string", "keywords": "list persisted at max 8"},
        "page_formatter": "<summary>\\nKeywords: <keywords>\\nUser: <original_user>",
        "summary_model": llm_config["model"],
        "thinking_mode": "disabled",
        "temperature": float(llm_config["temperature"]),
        "top_p": float(llm_config["top_p"]),
        "top_k": int(llm_config["top_k"]),
        "generation": generation_metadata,
        "Production_new_LLM_calls": 0,
        "Search_new_LLM_calls": 0,
        "Conservative_Tuned_successful_outputs": generation_metadata["successful_output_count"],
        "Production_Page_embeddings_reused": 333,
        "Conservative_Tuned_Page_embedding_items": 333,
        "Conservative_Tuned_Page_embedding_computed_this_run": 0 if embedding_metadata.get("cache_hit") else 333,
        "embedding_metadata": embedding_metadata,
        "tokenizer_truncation_summary": tokenizer_summaries,
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "dimension": 512,
        "Baseline_Query_embeddings_reused": 99,
        "Resolved_Query_embeddings_reused": 99,
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "query_text_changed": False,
        "validation": validations,
        "validation_all_pass": all(row["status"] == "PASS" for row in validations.values()),
        "aggregate_metrics": aggregate,
        "keyword_summary": keyword_summary,
        "fidelity_potential_unsupported_count": sum(row["status"] == "POTENTIAL_UNSUPPORTED" for row in fidelity),
        "replacement_decision": decision,
        "production_prompt_char_count": len(old_prompt),
        "tuned_prompt_char_count": len(tuned_prompt),
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "retrieval_metrics": retrieval,
                "representation_stats": stats,
                "separation": separation,
                "generation": generation_metadata,
                "decision": decision,
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
