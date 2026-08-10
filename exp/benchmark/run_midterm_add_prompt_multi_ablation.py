"""Frozen Add-Old/A1/A2/A3 x Search-Baseline/P2 MidTerm retrieval ablation."""

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
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    evaluate_rankings,
    load_jsonl,
    percentile,
    stable_hash,
    write_jsonl,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    CURRENCY_PATTERN,
    NUMBER_PATTERN,
    PERCENT_PATTERN,
    PRODUCTION_EMBEDDING,
    REFERENCE_PATTERN,
    SESSION_CODES,
    STATE_RELATION_TERMS,
    YEAR_PATTERN,
    current_turn_payload,
    evaluate_all,
    extract_indicators,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    normalize_numeric,
    page_text,
    rank_configuration,
    regex_values,
    sha256_file,
    state_count,
    text_tokens,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_prompt_multi_ablation"
PROMPT_FILES = {
    "A1": "Add_A1_task_first.txt",
    "A2": "Add_A2_task_dependency.txt",
    "A3": "Add_A3_old_lite.txt",
}
PROMPT_VERSIONS = {
    "A1": "add-a1-task-first-v1",
    "A2": "add-a2-task-dependency-v1",
    "A3": "add-a3-production-old-lite-v1",
}
ADD_VARIANTS = ("Old", "A1", "A2", "A3")
NEW_VARIANTS = ("A1", "A2", "A3")
SEARCH_VARIANTS = ("Baseline", "P2")
CONFIGS = tuple(f"{add}_{search}" for add in ADD_VARIANTS for search in SEARCH_VARIANTS)
MANDATORY_IDS = (
    "S001-Q013",
    "S001-Q026",
    "S001-Q033",
    "S002-Q049",
    "S002-Q055",
    "S003-Q039",
    "S003-Q044",
    "S003-Q066",
    "S003-Q077",
    "S004-Q029",
    "S004-Q033",
    "S004-Q035",
    "S004-Q052",
    "S004-Q063",
    "S005-Q040",
    "S005-Q042",
    "S005-Q051",
    "S005-Q052",
    "S005-Q055",
)
OUTPUT_K = 5
DISPLAY_K = 10
TASK_RULES = {
    "反证检验": ("反证", "反例", "推翻", "削弱", "反向验证", "不只听支持", "冲突在哪里"),
    "验证": ("验证", "检验", "是否支持", "能否支持", "能否说明", "是否成立"),
    "判断修订": ("修订", "修正", "调整", "降级", "需要改", "需要调整", "重新检查", "重写结论"),
    "风险排序": ("风险排序", "排序风险", "优先级", "按风险", "最值得关注"),
    "证据分级": ("证据分级", "证据按", "可靠性排序", "证据强度", "三层证据", "事实层", "计算层", "判断层"),
    "阶段小结": ("阶段小结", "小结", "阶段总结", "汇总前面", "合并结论"),
    "综合判断": ("综合判断", "综合结论", "合在一起", "整体结论", "跨指标"),
    "管理层问询": ("管理层", "问询", "追问公司", "问题清单", "核实问题"),
    "信息缺口": ("信息缺口", "缺少", "不足以", "无法确认", "待核验", "还需要什么"),
    "来源核验": ("来源", "年报原数", "公开来源", "披露", "版本", "可追溯"),
    "口径核验": ("口径", "完整财年", "季度", "同口径", "归母", "扣非"),
    "同比解释": ("同比", "两段同比", "增速", "年度差异", "变化幅度"),
    "拐点定位": ("拐点", "反转", "哪一年", "中间点", "先升后降", "先降后升"),
    "现金质量": ("现金质量", "现金表现", "现金与利润", "现金利润", "经营现金流"),
    "盈利质量": ("盈利质量", "盈利趋势", "利润质量", "归母净利润", "扣非净利润"),
    "效率判断": ("效率", "周转", "资产承载", "投入有效", "经营效率"),
    "杠杆分析": ("杠杆", "资产权益比", "资产和权益", "资产与权益"),
    "可复算性": ("可复算", "复算", "计算结果", "公式", "原数"),
    "同步比较": ("同步", "不同步", "两者", "方向相反", "方向一致", "比较"),
    "原因解释": ("解释", "原因", "为什么", "归因", "怎样理解"),
}
GENERAL_TASK_TERMS = (
    "分析",
    "判断",
    "比较",
    "检查",
    "核验",
    "验证",
    "检验",
    "计算",
    "解释",
    "排序",
    "分级",
    "修订",
    "总结",
    "小结",
    "问询",
    "结论",
)
DEPENDENCY_RULES = {
    "PREVIOUS_TURN": ("上一轮", "刚才", "上一问", "前一轮"),
    "EARLIER_CONTEXT": ("前面", "更早", "此前", "前序", "主结论", "前面的判断", "证据链"),
    "CONTINUATION": ("接着", "继续", "再结合", "承接", "延续", "接回", "合并", "回到"),
    "REVISION": ("修订", "修正", "调整", "降级", "反证", "反例"),
}
DEPENDENCY_PRESERVE_TERMS = (
    "上一轮",
    "刚才",
    "前面",
    "更早",
    "此前",
    "前序",
    "承接",
    "延续",
    "接续",
    "继续",
    "再结合",
    "回看",
    "回到",
    "合并",
    "修订",
    "反证",
    "反例",
    "主结论",
    "证据链",
    "跨轮",
)
BINDING_TERMS = (
    "因此",
    "说明",
    "表明",
    "意味着",
    "支持",
    "削弱",
    "要求",
    "只能",
    "不能",
    "据此",
    "结论",
    "提示",
    "影响",
    "需要",
    "但",
    "然而",
)
STATE_CANONICAL = {
    "先升后降": ("先升后降",),
    "先降后升": ("先降后升",),
    "连续上升": ("连续上升", "持续上升", "逐年上升"),
    "连续下降": ("连续下降", "持续下降", "逐年下降"),
    "同步": ("同步", "方向一致", "同向"),
    "不同步": ("不同步", "方向冲突", "方向相反", "方向不同"),
    "错位": ("错位",),
    "拐点": ("拐点", "反转"),
    "无法确认": ("无法确认", "不能确认", "不足以判断"),
    "信息缺口": ("信息缺口", "数据缺失", "缺少数据"),
}
COMPANIES = ("贵州茅台", "比亚迪", "宁德时代")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MidTerm Add Prompt multi-candidate ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def summary_validator(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    if not isinstance(parsed, dict) or set(parsed) != {"summary", "keywords"}:
        raise ValueError("response must contain exactly summary and keywords")
    summary, keywords = parsed["summary"], parsed["keywords"]
    if not isinstance(summary, str) or not isinstance(keywords, list):
        raise ValueError("invalid summary/keywords types")
    if any(not isinstance(item, str) or not item.strip() for item in keywords):
        raise ValueError("keywords must be non-empty strings")
    cleaned = [item.strip() for item in keywords]
    if not summary.strip() and cleaned:
        raise ValueError("empty summary requires empty keywords")
    return {"summary": summary.strip(), "keywords": cleaned[:8]}


def provider_precondition(exc: Exception) -> bool:
    text = str(exc).lower()
    return "precondition" in text or ("response_format" in text and "json" in text)


class VariantSummaryCache:
    def __init__(
        self,
        path: Path,
        *,
        variant: str,
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
        self.variant = variant
        self.client = client
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.lock = asyncio.Lock()
        rows = load_jsonl(path)
        self.success = {str(row["cache_key"]): row for row in rows if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(self, page: Mapping[str, Any], prompt: str, prompt_sha: str) -> dict[str, Any]:
        identity = {
            "session_id": str(page["session_code"]),
            "source_turn_id": str(page["source_turn_id"]),
            "prompt_variant": self.variant,
            "prompt_sha256": prompt_sha,
            "source_user_sha256": hashlib.sha256(str(page["user_input"]).encode()).hexdigest(),
            "source_assistant_sha256": hashlib.sha256(str(page["assistant_response"]).encode()).hexdigest(),
            "model": self.model,
            "thinking_mode": "disabled",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }
        key = stable_hash(identity)
        if key in self.success:
            return self.success[key]
        payload = current_turn_payload(page)
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
                        self.client.chat.completions.create(**kwargs), timeout=self.timeout
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = summary_validator(raw)
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": key,
                        "prompt_version": PROMPT_VERSIONS[self.variant],
                        "input_contract": "CURRENT_USER + CURRENT_ASSISTANT only",
                        "input_field_names": ["CURRENT_USER", "CURRENT_ASSISTANT"],
                        "input_payload_sha256": hashlib.sha256(payload.encode()).hexdigest(),
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
                    is_precondition = provider_precondition(exc)
                    rejects += int(is_precondition)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if is_precondition and response_format:
                        response_format = False
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12))
        row = {
            **identity,
            "cache_key": key,
            "prompt_version": PROMPT_VERSIONS[self.variant],
            "input_contract": "CURRENT_USER + CURRENT_ASSISTANT only",
            "input_field_names": ["CURRENT_USER", "CURRENT_ASSISTANT"],
            "input_payload_sha256": hashlib.sha256(current_turn_payload(page).encode()).hexdigest(),
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": self.retries - 1,
            "provider_precondition_rejected_count": rejects,
            "errors": errors,
        }
        await self.append(row)
        return row


def freeze_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    prompts, hashes = {}, {}
    for variant, filename in PROMPT_FILES.items():
        path = output_dir / "prompts" / filename
        if not path.exists():
            raise FileNotFoundError(path)
        prompts[variant] = path.read_text(encoding="utf-8")
        hashes[variant] = sha256_file(path)
    dump_json(
        output_dir / "prompts/prompt_sha256.json",
        {
            "hash_contract": "SHA256 of exact UTF-8 file bytes",
            "frozen_before_first_llm_call": True,
            **{v: {"file": PROMPT_FILES[v], "sha256": hashes[v]} for v in NEW_VARIANTS},
        },
    )
    return prompts, hashes


async def generate_variant(
    args: argparse.Namespace,
    variant: str,
    pages: Sequence[Mapping[str, Any]],
    prompt: str,
    prompt_sha: str,
    config: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    base_url = config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=config["api_key"], base_url=base_url)
    cache_path = args.output_dir / f"cache/{variant}_summary_llm.jsonl"
    cache = VariantSummaryCache(
        cache_path,
        variant=variant,
        client=client,
        model=str(config["model"]),
        temperature=float(config["temperature"]),
        top_p=float(config["top_p"]),
        top_k=int(config["top_k"]),
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
        raise RuntimeError(f"{variant} failed for {len(failures)} Pages")
    expected_keys = {str(row["cache_key"]) for row in rows}
    all_rows = [row for row in load_jsonl(cache_path) if str(row.get("cache_key")) in expected_keys]
    attempts = sum(int(row.get("api_attempt_count") or 0) for row in all_rows)
    truncations = sum(len(json.loads(str(row["raw_output"])).get("keywords") or []) > 8 for row in rows)
    return {str(row["source_turn_id"]): row for row in rows}, {
        "successful_output_count": len(rows),
        "actual_api_attempts": attempts,
        "retry_count": attempts - len(rows),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0) for row in all_rows
        ),
        "cache_hit_count": sum(str(row["cache_key"]) in initial for row in rows),
        "failed_cache_row_count": sum(row.get("status") == "FAILED" for row in all_rows),
        "keyword_truncation_count": truncations,
        "cache_path": str(cache_path),
    }


def build_pages(
    old_pages: Sequence[Mapping[str, Any]],
    generated: Mapping[str, Mapping[str, Mapping[str, Any]]],
    prompt_hashes: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    variants: dict[str, list[dict[str, Any]]] = {"Old": []}
    for old in old_pages:
        variants["Old"].append(
            {
                "session_id": old["session_code"],
                "source_turn_id": old["source_turn_id"],
                "source_turn_index": old["source_turn_index"],
                "old_page_id": old["page_id"],
                "source_user": old["user_input"],
                "source_assistant": old["assistant_response"],
                "summary": old["summary"],
                "keywords": list(old["keywords"]),
                "embedding_text": old["current_embedding_text"],
            }
        )
    for variant in NEW_VARIANTS:
        variants[variant] = []
        for old in old_pages:
            row = generated[variant][str(old["source_turn_id"])]
            summary, keywords = row["parsed"]["summary"], list(row["parsed"]["keywords"])
            variants[variant].append(
                {
                    "session_id": old["session_code"],
                    "source_turn_id": old["source_turn_id"],
                    "source_turn_index": old["source_turn_index"],
                    "old_page_id": old["page_id"],
                    f"{variant}_page_id": None,
                    "source_user": old["user_input"],
                    "source_assistant": old["assistant_response"],
                    "source_user_sha256": hashlib.sha256(str(old["user_input"]).encode()).hexdigest(),
                    "source_assistant_sha256": hashlib.sha256(str(old["assistant_response"]).encode()).hexdigest(),
                    "summary": summary,
                    "keywords": keywords,
                    "embedding_text": page_text(summary, keywords, str(old["user_input"])),
                    "prompt_sha256": prompt_hashes[variant],
                    "llm_cache_key": row["cache_key"],
                }
            )
    return variants


def validate_old(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    old_vectors: Mapping[str, Sequence[float]],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    rankings = {
        "Old_Baseline": rank_configuration(snapshots, query_vectors["Search-Baseline"], old_vectors),
        "Old_P2": rank_configuration(snapshots, query_vectors["Search-P2"], old_vectors),
    }
    base, _ = evaluate_all(snapshots, rankings["Old_Baseline"])
    p2, _ = evaluate_all(snapshots, rankings["Old_P2"])
    expected = {
        "A": (
            base,
            (
                0.3246753246753247,
                0.3367359868813357,
                0.525974025974026,
                0.7467532467532467,
                0.2948787273272229,
                14.571428571428571,
            ),
        ),
        "B": (
            p2,
            (
                0.35064935064935066,
                0.3601371496720334,
                0.525974025974026,
                0.7597402597402597,
                0.30458163138396327,
                14.551948051948052,
            ),
        ),
    }
    checks: dict[str, Any] = {}
    for name, (actual, values) in expected.items():
        current = (
            actual["recall_at_5"],
            actual["macro_session_r5"],
            actual["recall_at_10"],
            actual["recall_at_20"],
            actual["mrr"],
            actual["mean_gold_rank"],
        )
        status = (
            "PASS" if all(math.isclose(float(a), float(e), abs_tol=1e-12) for a, e in zip(current, values)) else "FAIL"
        )
        checks[name] = {"status": status, "actual": current, "expected": values}
    if any(row["status"] != "PASS" for row in checks.values()):
        raise AssertionError(checks)
    return checks, rankings


def extract_tasks(text: str) -> list[str]:
    value = str(text)
    labels = [label for label, markers in TASK_RULES.items() if any(marker in value for marker in markers)]
    if not labels and any(term in value for term in GENERAL_TASK_TERMS):
        labels = ["一般分析判断"]
    return labels


def extract_dependencies(text: str) -> list[str]:
    return [label for label, markers in DEPENDENCY_RULES.items() if any(marker in str(text) for marker in markers)]


def task_status(user: str, summary: str) -> tuple[str, list[str], list[str]]:
    user_tasks = extract_tasks(user)
    summary_tasks = extract_tasks(summary)
    explicit = set(user_tasks) & set(summary_tasks)
    if explicit or (user_tasks == ["一般分析判断"] and summary_tasks):
        return "TASK_EXPLICIT", user_tasks, summary_tasks
    user_task_tokens = {token for label in user_tasks for token in TASK_RULES.get(label, (label,)) if len(token) >= 2}
    lexical_overlap = any(token in summary for token in user_task_tokens)
    if summary_tasks or lexical_overlap or any(term in summary for term in GENERAL_TASK_TERMS):
        return "TASK_PARTIAL", user_tasks, summary_tasks
    return "TASK_MISSING", user_tasks, summary_tasks


def dependency_preserved(user: str, summary: str) -> tuple[bool, list[str]]:
    types = extract_dependencies(user)
    return (bool(types) and any(term in summary for term in DEPENDENCY_PRESERVE_TERMS), types)


def binding_status(user: str, summary: str) -> str:
    status, _, _ = task_status(user, summary)
    has_result = (
        bool(extract_indicators(summary))
        or state_count(summary) > 0
        or any(term in summary for term in ("结果", "发现", "确认", "结论", "不足", "缺失", "差异"))
    )
    has_binding = any(term in summary for term in BINDING_TERMS) or bool(
        re.search(r"(?:检验|判断|修订|排序|小结|问询)[：:]", summary)
    )
    if status == "TASK_EXPLICIT" and has_result and has_binding:
        return "BOUND"
    if status != "TASK_MISSING" and has_result:
        return "WEAKLY_BOUND"
    return "UNBOUND"


def normalize_template(summary: str) -> str:
    value = str(summary)
    value = YEAR_PATTERN.sub("<YEAR>", value)
    value = NUMBER_PATTERN.sub("<NUM>", value)
    for company in COMPANIES:
        value = value.replace(company, "<COMPANY>")
    value = re.sub(r"[\s，。；：、,.!?！？;:（）()《》“”\"'—\-]+", " ", value)
    return value.strip().lower()


def representation_audits(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    exact_rows: list[dict[str, Any]] = []
    normalized_rows: list[dict[str, Any]] = []
    collision_rows: list[dict[str, Any]] = []
    stats: dict[str, dict[str, Any]] = {}
    for variant in ADD_VARIANTS:
        rows = list(pages[variant])
        exact: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        normalized: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            exact[str(row["summary"])].append(row)
            normalized[normalize_template(str(row["summary"]))].append(row)
        exact_groups = [group for group in exact.values() if len(group) >= 2]
        normalized_groups = [group for group in normalized.values() if len(group) >= 2]
        for index, group in enumerate(sorted(exact_groups, key=lambda g: (-len(g), str(g[0]["summary"]))), 1):
            exact_rows.append(
                {
                    "add_variant": variant,
                    "group_id": f"{variant}-EXACT-{index:03d}",
                    "summary_text": group[0]["summary"],
                    "count": len(group),
                    "source_turn_ids": [row["source_turn_id"] for row in group],
                    "source_users": [row["source_user"] for row in group],
                    "listed_because_count_ge_3": len(group) >= 3,
                }
            )
        for index, group in enumerate(
            sorted(normalized_groups, key=lambda g: (-len(g), normalize_template(str(g[0]["summary"])))), 1
        ):
            normalized_rows.append(
                {
                    "add_variant": variant,
                    "group_id": f"{variant}-NORM-{index:03d}",
                    "normalized_summary": normalize_template(str(group[0]["summary"])),
                    "count": len(group),
                    "source_turn_ids": [row["source_turn_id"] for row in group],
                    "source_users": [row["source_user"] for row in group],
                    "original_summaries": [row["summary"] for row in group],
                }
            )
        collision_pages: set[str] = set()
        by_session: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            by_session[str(row["session_id"])].append(row)
        for session_id, session_rows in by_session.items():
            tokens = {
                str(row["source_turn_id"]): text_tokens(normalize_template(str(row["summary"]))) for row in session_rows
            }
            for i, left in enumerate(session_rows):
                left_tasks = set(extract_tasks(str(left["source_user"])))
                if not left_tasks:
                    continue
                for right in session_rows[i + 1 :]:
                    right_tasks = set(extract_tasks(str(right["source_user"])))
                    if not right_tasks or left_tasks == right_tasks:
                        continue
                    left_id, right_id = str(left["source_turn_id"]), str(right["source_turn_id"])
                    union = tokens[left_id] | tokens[right_id]
                    similarity = len(tokens[left_id] & tokens[right_id]) / len(union) if union else 1.0
                    exact_same = str(left["summary"]) == str(right["summary"])
                    normalized_same = normalize_template(str(left["summary"])) == normalize_template(
                        str(right["summary"])
                    )
                    if exact_same or normalized_same or similarity >= 0.80:
                        collision_pages.update((left_id, right_id))
                        collision_rows.append(
                            {
                                "add_variant": variant,
                                "session_id": session_id,
                                "left_source_turn_id": left_id,
                                "right_source_turn_id": right_id,
                                "left_tasks": sorted(left_tasks),
                                "right_tasks": sorted(right_tasks),
                                "collision_type": "EXACT"
                                if exact_same
                                else "NORMALIZED"
                                if normalized_same
                                else "TOKEN_JACCARD_GE_0.80",
                                "summary_token_jaccard": similarity,
                                "left_summary": left["summary"],
                                "right_summary": right["summary"],
                            }
                        )
        stats[variant] = {
            "page_count": len(rows),
            "unique_summary_count": len(exact),
            "exact_duplicate_page_count": sum(len(group) for group in exact_groups),
            "duplicate_group_count": len(exact_groups),
            "largest_duplicate_group_size": max((len(group) for group in exact_groups), default=1),
            "normalized_unique_count": len(normalized),
            "normalized_duplicate_group_count": len(normalized_groups),
            "largest_normalized_duplicate_group": max((len(group) for group in normalized_groups), default=1),
            "cross_task_collision_page_count": len(collision_pages),
            "cross_task_collision_rate": len(collision_pages) / len(rows),
        }
    return exact_rows, normalized_rows, collision_rows, stats


def audit_rows(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    task_rows, dependency_rows, binding_rows, focus_rows = [], [], [], []
    summary: dict[str, dict[str, Any]] = {}
    by_variant_turn = {v: {str(row["source_turn_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    source_rows = pages["Old"]
    for source in source_rows:
        turn = str(source["source_turn_id"])
        has_dependency = bool(extract_dependencies(str(source["source_user"])))
        dep_row: dict[str, Any] = {
            "session_id": source["session_id"],
            "source_turn_id": turn,
            "has_dependency_signal": has_dependency,
            "dependency_type": extract_dependencies(str(source["source_user"])),
        }
        for variant in ADD_VARIANTS:
            row = by_variant_turn[variant][turn]
            status, user_tasks, summary_tasks = task_status(str(source["source_user"]), str(row["summary"]))
            preserved, _ = dependency_preserved(str(source["source_user"]), str(row["summary"]))
            binding = binding_status(str(source["source_user"]), str(row["summary"]))
            keyword_text = " ".join(map(str, row["keywords"]))
            task_rows.append(
                {
                    "session_id": source["session_id"],
                    "source_turn_id": turn,
                    "add_variant": variant,
                    "user_task_labels": user_tasks,
                    "summary_task_labels": summary_tasks,
                    "task_alignment": status,
                    "task_keyword_covered": bool(extract_tasks(keyword_text)),
                    "audit_method": "deterministic task lexicon + semantic action-slot heuristic; no Gold/rank/retrieval inputs",
                }
            )
            dep_row[f"{variant}_preserved"] = preserved if has_dependency else None
            dep_row[f"{variant}_keyword_covered"] = (
                any(term in keyword_text for term in DEPENDENCY_PRESERVE_TERMS) if has_dependency else None
            )
            binding_rows.append(
                {
                    "session_id": source["session_id"],
                    "source_turn_id": turn,
                    "add_variant": variant,
                    "task_result_binding": binding,
                    "task_alignment": status,
                    "summary": row["summary"],
                }
            )
            summary_tokens = text_tokens(str(row["summary"]))
            user_tokens = text_tokens(str(source["source_user"]))
            assistant = str(source["source_assistant"])
            direct_tokens = text_tokens(assistant[:2500])
            background_tokens = text_tokens(assistant[2500:])
            denominator = max(1, len(summary_tokens))
            user_overlap = len(summary_tokens & user_tokens) / denominator
            direct_overlap = len(summary_tokens & direct_tokens) / denominator
            background_overlap = len(summary_tokens & background_tokens) / denominator
            if status == "TASK_EXPLICIT" and user_overlap >= direct_overlap * 0.8:
                focus = "USER_TASK"
            elif status == "TASK_MISSING" and background_overlap > max(user_overlap, direct_overlap) * 1.2:
                focus = "ASSISTANT_BACKGROUND"
            elif direct_overlap > background_overlap * 1.15 and status != "TASK_EXPLICIT":
                focus = "DIRECT_ASSISTANT_ANSWER"
            else:
                focus = "MIXED"
            focus_rows.append(
                {
                    "session_id": source["session_id"],
                    "source_turn_id": turn,
                    "add_variant": variant,
                    "focus": focus,
                    "user_task_overlap": user_overlap,
                    "direct_assistant_overlap": direct_overlap,
                    "assistant_background_overlap": background_overlap,
                    "audit_method": "summary token coverage against User, Assistant direct-answer window, and Assistant background",
                }
            )
        dependency_rows.append(dep_row)
    for variant in ADD_VARIANTS:
        selected_task = [row for row in task_rows if row["add_variant"] == variant]
        selected_binding = [row for row in binding_rows if row["add_variant"] == variant]
        dependency_denominator = [row for row in dependency_rows if row["has_dependency_signal"]]
        summary[variant] = {
            "task_explicit_rate": sum(row["task_alignment"] == "TASK_EXPLICIT" for row in selected_task)
            / len(selected_task),
            "task_partial_rate": sum(row["task_alignment"] == "TASK_PARTIAL" for row in selected_task)
            / len(selected_task),
            "task_missing_rate": sum(row["task_alignment"] == "TASK_MISSING" for row in selected_task)
            / len(selected_task),
            "task_keyword_coverage": sum(bool(row["task_keyword_covered"]) for row in selected_task)
            / len(selected_task),
            "dependency_preservation_rate": sum(bool(row[f"{variant}_preserved"]) for row in dependency_denominator)
            / len(dependency_denominator),
            "dependency_keyword_coverage": sum(
                bool(row[f"{variant}_keyword_covered"]) for row in dependency_denominator
            )
            / len(dependency_denominator),
            "task_result_bound_rate": sum(row["task_result_binding"] == "BOUND" for row in selected_binding)
            / len(selected_binding),
        }
    return task_rows, dependency_rows, binding_rows, focus_rows, summary


def state_set(text: str) -> set[str]:
    return {canonical for canonical, aliases in STATE_CANONICAL.items() if any(alias in str(text) for alias in aliases)}


def fidelity_rows(pages: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    source = {str(row["source_turn_id"]): row for row in pages["Old"]}
    rows: list[dict[str, Any]] = []
    for variant in NEW_VARIANTS:
        for row in pages[variant]:
            turn = str(row["source_turn_id"])
            old = source[turn]
            source_text = str(old["source_user"]) + "\n" + str(old["source_assistant"])
            generated = str(row["summary"]) + "\n" + "\n".join(map(str, row["keywords"]))
            unsupported_indicators = sorted(set(extract_indicators(generated)) - set(extract_indicators(source_text)))
            unsupported_years = sorted(
                {normalize_numeric(x) for x in regex_values(YEAR_PATTERN, generated)}
                - {normalize_numeric(x) for x in regex_values(YEAR_PATTERN, source_text)}
            )
            unsupported_numbers = sorted(
                {normalize_numeric(x) for x in regex_values(NUMBER_PATTERN, generated)}
                - {normalize_numeric(x) for x in regex_values(NUMBER_PATTERN, source_text)}
            )
            unsupported_states = sorted(state_set(generated) - state_set(source_text))
            flags = []
            for kind, values in (
                ("UNSUPPORTED_INDICATOR", unsupported_indicators),
                ("UNSUPPORTED_YEAR", unsupported_years),
                ("UNSUPPORTED_NUMERIC", unsupported_numbers),
                ("UNSUPPORTED_STATE_OR_CONCLUSION", unsupported_states),
            ):
                if values:
                    flags.append({"type": kind, "values": values})
            reference_present = bool(REFERENCE_PATTERN.search(str(old["source_user"])))
            direct_indicators = set(extract_indicators(str(old["source_assistant"])[:1600]))
            added = set(extract_indicators(generated)) - set(extract_indicators(str(old["source_user"])))
            if reference_present and added and not (added & direct_indicators):
                flags.append(
                    {
                        "type": "POSSIBLE_UNRESOLVED_REFERENCE_COMPLETION",
                        "values": sorted(added - direct_indicators),
                    }
                )
            rows.append(
                {
                    "session_id": row["session_id"],
                    "source_turn_id": turn,
                    "add_variant": variant,
                    "status": "POTENTIAL_UNSUPPORTED" if flags else "NO_FLAG",
                    "flags": flags,
                    "audit_scope": "CURRENT_USER + CURRENT_ASSISTANT only; no Gold/rank/retrieval inputs",
                    "source_user": old["source_user"],
                    "source_assistant": old["source_assistant"],
                    "summary": row["summary"],
                    "keywords": row["keywords"],
                }
            )
    return rows


def generic_representation_stats(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    duplicate_stats: Mapping[str, Mapping[str, Any]],
    audit_summary: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    rows = []
    for variant in ADD_VARIANTS:
        selected = list(pages[variant])
        lengths = [len(str(row["summary"])) for row in selected]

        def dependency_count(text: Any) -> int:
            return sum(str(text).count(term) for term in DEPENDENCY_PRESERVE_TERMS)

        def task_count(text: Any) -> int:
            return len(extract_tasks(str(text)))

        row = {
            "add_variant": variant,
            "page_count": len(selected),
            "summary_length_chars_mean": statistics.fmean(lengths),
            "summary_length_chars_median": statistics.median(lengths),
            "summary_length_chars_p90": percentile(lengths, 0.9),
            "keyword_count_mean": statistics.fmean(len(row["keywords"]) for row in selected),
            "indicator_count_mean": statistics.fmean(
                len(extract_indicators(str(row["embedding_text"]))) for row in selected
            ),
            "numeric_token_count_mean": statistics.fmean(
                len(regex_values(NUMBER_PATTERN, str(row["embedding_text"]))) for row in selected
            ),
            "percentage_count_mean": statistics.fmean(
                len(regex_values(PERCENT_PATTERN, str(row["embedding_text"]))) for row in selected
            ),
            "year_count_mean": statistics.fmean(
                len(regex_values(YEAR_PATTERN, str(row["embedding_text"]))) for row in selected
            ),
            "state_relation_count_mean": statistics.fmean(state_count(str(row["embedding_text"])) for row in selected),
            "task_term_count_mean": statistics.fmean(task_count(str(row["summary"])) for row in selected),
            "dependency_term_count_mean": statistics.fmean(dependency_count(str(row["summary"])) for row in selected),
            **duplicate_stats[variant],
            **audit_summary[variant],
        }
        rows.append(row)
    return rows


def similarity_outputs(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_session: list[dict[str, Any]] = []
    for variant in ADD_VARIANTS:
        for code in SESSION_CODES:
            selected = [row for row in pages[variant] if row["session_id"] == code]
            matrix = np.asarray([vectors[variant][str(row["old_page_id"])] for row in selected], dtype=np.float64)
            norms = np.linalg.norm(matrix, axis=1, keepdims=True)
            cosine = np.divide(
                matrix @ matrix.T,
                norms @ norms.T,
                out=np.zeros((len(matrix), len(matrix))),
                where=(norms @ norms.T) > 0,
            )
            pairs = cosine[np.triu_indices(len(matrix), 1)].tolist()
            local = cosine.copy()
            np.fill_diagonal(local, -np.inf)
            nearest = np.max(local, axis=1).tolist()
            by_session.append(
                {
                    "session_id": code,
                    "add_variant": variant,
                    "page_count": len(selected),
                    "pairwise_cosine_mean": statistics.fmean(pairs),
                    "pairwise_cosine_median": statistics.median(pairs),
                    "pairwise_cosine_p75": percentile(pairs, 0.75),
                    "pairwise_cosine_p90": percentile(pairs, 0.90),
                    "pairwise_cosine_p95": percentile(pairs, 0.95),
                    "nearest_neighbor_cosine_mean": statistics.fmean(nearest),
                    "nearest_neighbor_cosine_median": statistics.median(nearest),
                    "nearest_neighbor_cosine_p90": percentile(nearest, 0.90),
                    "nearest_neighbor_cosine_p95": percentile(nearest, 0.95),
                }
            )
    summary = []
    metric_keys = [key for key in by_session[0] if key not in {"session_id", "add_variant", "page_count"}]
    for variant in ADD_VARIANTS:
        selected = [row for row in by_session if row["add_variant"] == variant]
        summary.append(
            {
                "scope": "macro_average_across_sessions",
                "add_variant": variant,
                "session_count": 5,
                **{key: statistics.fmean(float(row[key]) for row in selected) for key in metric_keys},
            }
        )
    return by_session, summary


def rank_metrics(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    rows, aggregate, session_metrics = [], {}, {}
    for config in CONFIGS:
        overall, sessions = evaluate_all(snapshots, rankings[config])
        aggregate[config], session_metrics[config] = overall, sessions
        add, search = config.split("_", 1)
        rows.append(
            {
                "add_variant": add,
                "search_variant": search,
                "eligible_gold_count": overall["eligible_gold_count"],
                "top5_recalled_gold": round(float(overall["recall_at_5"]) * int(overall["eligible_gold_count"])),
                "micro_r5": overall["recall_at_5"],
                "macro_r5": overall["macro_session_r5"],
                "r10": overall["recall_at_10"],
                "r20": overall["recall_at_20"],
                "mrr": overall["mrr"],
                "mean_gold_rank": overall["mean_gold_rank"],
            }
        )
    return rows, aggregate, session_metrics


def comparison_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    queries = [query for code in SESSION_CODES for query in snapshots[code]["queries"]]
    rank_maps = {config: {qid: rank_map(rank) for qid, rank in value.items()} for config, value in rankings.items()}
    gold_rows, transitions, comparison_rows = [], [], []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = [str(x) for x in query["eligible_gold_page_ids"]]
        for gold_id in gold_ids:
            row = {
                "session_id": query_id.split("-", 1)[0],
                "query_id": query_id,
                "gold_page_id": gold_id,
                "gold_source_turn_id": rank_maps["Old_Baseline"][query_id][gold_id]["source_turn_id"],
            }
            for config in CONFIGS:
                item = rank_maps[config][query_id][gold_id]
                row[f"{config}_rank"] = int(item["rank"])
                row[f"{config}_score"] = float(item["score"])
            gold_rows.append(row)
        for candidate in NEW_VARIANTS:
            for search in SEARCH_VARIANTS:
                before, after = f"Old_{search}", f"{candidate}_{search}"
                before_ranks = {gold: int(rank_maps[before][query_id][gold]["rank"]) for gold in gold_ids}
                after_ranks = {gold: int(rank_maps[after][query_id][gold]["rank"]) for gold in gold_ids}
                bh, ah = any(v <= 5 for v in before_ranks.values()), any(v <= 5 for v in after_ranks.values())
                transition = (
                    "RESCUED"
                    if not bh and ah
                    else "HURT"
                    if bh and not ah
                    else "UNCHANGED_HIT"
                    if bh
                    else "UNCHANGED_MISS"
                )
                transitions.append(
                    {
                        "session_id": query_id.split("-", 1)[0],
                        "query_id": query_id,
                        "candidate": candidate,
                        "search_variant": search,
                        "comparison": f"{candidate} vs Old under {search}",
                        "before_gold_ranks": before_ranks,
                        "after_gold_ranks": after_ranks,
                        "transition": transition,
                        "promoted_gold": sum(before_ranks[g] > 5 and after_ranks[g] <= 5 for g in gold_ids),
                        "demoted_gold": sum(before_ranks[g] <= 5 and after_ranks[g] > 5 for g in gold_ids),
                        "max_promotion": max(before_ranks[g] - after_ranks[g] for g in gold_ids),
                        "max_demotion": max(after_ranks[g] - before_ranks[g] for g in gold_ids),
                    }
                )
    page_by_variant_turn = {v: {str(r["source_turn_id"]): r for r in pages[v]} for v in ADD_VARIANTS}
    pairs = [("Old", x, "PRIMARY_OLD_BASELINE") for x in NEW_VARIANTS] + [
        ("A1", "A2", "CANDIDATE_PAIRWISE"),
        ("A1", "A3", "CANDIDATE_PAIRWISE"),
        ("A2", "A3", "CANDIDATE_PAIRWISE"),
    ]
    for left, right, role in pairs:
        left_summaries = page_by_variant_turn[left]
        right_summaries = page_by_variant_turn[right]
        representation_jaccards = []
        exact_matches = 0
        for turn in left_summaries:
            left_summary = str(left_summaries[turn]["summary"])
            right_summary = str(right_summaries[turn]["summary"])
            union = text_tokens(left_summary) | text_tokens(right_summary)
            representation_jaccards.append(
                len(text_tokens(left_summary) & text_tokens(right_summary)) / len(union) if union else 1.0
            )
            exact_matches += int(left_summary == right_summary)
        for search in SEARCH_VARIANTS:
            left_config, right_config = f"{left}_{search}", f"{right}_{search}"
            promoted = sum(
                int(row[f"{left_config}_rank"]) > 5 and int(row[f"{right_config}_rank"]) <= 5 for row in gold_rows
            )
            demoted = sum(
                int(row[f"{left_config}_rank"]) <= 5 and int(row[f"{right_config}_rank"]) > 5 for row in gold_rows
            )
            query_subset = (
                [row for row in transitions if row["candidate"] == right and row["search_variant"] == search]
                if left == "Old"
                else []
            )
            comparison_rows.append(
                {
                    "left_add": left,
                    "right_add": right,
                    "search_variant": search,
                    "comparison_role": role,
                    "promoted_gold": promoted,
                    "demoted_gold": demoted,
                    "net_gold_gain": promoted - demoted,
                    "rescued_queries": sum(row["transition"] == "RESCUED" for row in query_subset)
                    if query_subset
                    else None,
                    "hurt_queries": sum(row["transition"] == "HURT" for row in query_subset) if query_subset else None,
                    "extreme_demotion": sum(
                        int(row[f"{left_config}_rank"]) <= 5 and int(row[f"{right_config}_rank"]) > 20
                        for row in gold_rows
                    ),
                    "extreme_promotion": sum(
                        int(row[f"{left_config}_rank"]) > 20 and int(row[f"{right_config}_rank"]) <= 5
                        for row in gold_rows
                    ),
                    "mean_gold_rank_delta_left_minus_right": statistics.fmean(
                        int(row[f"{left_config}_rank"]) - int(row[f"{right_config}_rank"]) for row in gold_rows
                    ),
                    "summary_exact_match_count": exact_matches,
                    "summary_token_jaccard_mean": statistics.fmean(representation_jaccards),
                }
            )
    session_rows = []
    for config in CONFIGS:
        add, search = config.split("_", 1)
        for code in SESSION_CODES:
            selected_gold = [row for row in gold_rows if row["session_id"] == code]
            if add == "Old":
                promoted = demoted = 0
            else:
                promoted = sum(
                    int(row[f"Old_{search}_rank"]) > 5 and int(row[f"{config}_rank"]) <= 5 for row in selected_gold
                )
                demoted = sum(
                    int(row[f"Old_{search}_rank"]) <= 5 and int(row[f"{config}_rank"]) > 5 for row in selected_gold
                )
            metrics, _ = evaluate_rankings(snapshots[code]["queries"], rankings[config])
            session_rows.append(
                {
                    "session_id": code,
                    "add_variant": add,
                    "search_variant": search,
                    "eligible_gold_count": metrics["eligible_gold_count"],
                    "r5": metrics["recall_at_5"],
                    "r10": metrics["recall_at_10"],
                    "r20": metrics["recall_at_20"],
                    "mrr": metrics["mrr"],
                    "mean_gold_rank": metrics["mean_gold_rank"],
                    "promoted": promoted,
                    "demoted": demoted,
                    "net": promoted - demoted,
                }
            )
    return gold_rows, transitions, comparison_rows, session_rows


def separation_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for config in CONFIGS:
        margins, top5_margins = [], []
        for code in SESSION_CODES:
            for query in snapshots[code]["queries"]:
                qid = str(query["query_id"])
                gold = {str(x) for x in query["eligible_gold_page_ids"]}
                ranking = rankings[config][qid]
                gold_scores = [float(row["score"]) for row in ranking if str(row["page_id"]) in gold]
                non_scores = [float(row["score"]) for row in ranking if str(row["page_id"]) not in gold]
                margins.append(max(gold_scores) - max(non_scores))
                threshold = sorted(non_scores, reverse=True)[min(4, len(non_scores) - 1)]
                top5_margins.append(max(gold_scores) - threshold)
        rows.append(
            {
                "add_variant": config.split("_", 1)[0],
                "search_variant": config.split("_", 1)[1],
                "query_count": len(margins),
                "top_separation_margin_mean": statistics.fmean(margins),
                "top_separation_margin_median": statistics.median(margins),
                "top_separation_positive_rate": sum(x > 0 for x in margins) / len(margins),
                "gold_top5_margin_definition": "best Gold query score minus fifth-highest visible Non-Gold query score",
                "gold_top5_margin_mean": statistics.fmean(top5_margins),
                "gold_top5_margin_median": statistics.median(top5_margins),
                "gold_top5_margin_positive_rate": sum(x > 0 for x in top5_margins) / len(top5_margins),
            }
        )
    return rows


def neighborhood_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> list[dict[str, Any]]:
    rows = []
    for variant in ADD_VARIANTS:
        for code in SESSION_CODES:
            page_by_id = {str(row["page_id"]): row for row in snapshots[code]["pages"]}
            vis = {str(row["query_id"]): row for row in snapshots[code]["visibility"]}
            for query in snapshots[code]["queries"]:
                qid = str(query["query_id"])
                gold = {str(x) for x in query["eligible_gold_page_ids"]}
                non_gold = [str(x) for x in vis[qid]["visible_page_ids"] if str(x) not in gold]
                for gold_id in gold:
                    gv = np.asarray(vectors[variant][gold_id], dtype=np.float64)
                    best_id, best_score = None, -math.inf
                    for page_id in non_gold:
                        nv = np.asarray(vectors[variant][page_id], dtype=np.float64)
                        score = float(gv @ nv / (np.linalg.norm(gv) * np.linalg.norm(nv)))
                        if score > best_score:
                            best_id, best_score = page_id, score
                    rows.append(
                        {
                            "session_id": code,
                            "query_id": qid,
                            "add_variant": variant,
                            "gold_page_id": gold_id,
                            "gold_source_turn_id": page_by_id[gold_id]["source_turn_id"],
                            "nearest_nongold_page_id": best_id,
                            "nearest_nongold_source_turn_id": page_by_id[best_id]["source_turn_id"]
                            if best_id
                            else None,
                            "gold_nearest_nongold_cosine": best_score,
                        }
                    )
    return rows


def keyword_outputs(pages: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    outputs = {}
    state_terms = tuple(STATE_RELATION_TERMS)
    for variant in ADD_VARIANTS:
        counts = Counter(str(keyword) for row in pages[variant] for keyword in row["keywords"])
        rows = []
        for rank, (keyword, count) in enumerate(sorted(counts.items(), key=lambda x: (-x[1], x[0]))[:30], 1):
            category = (
                "TASK"
                if extract_tasks(keyword)
                else "DEPENDENCY"
                if any(term in keyword for term in DEPENDENCY_PRESERVE_TERMS)
                else "INDICATOR"
                if extract_indicators(keyword)
                else "STATE"
                if any(term in keyword for term in state_terms)
                else "BACKGROUND"
            )
            rows.append(
                {"add_variant": variant, "rank": rank, "keyword": keyword, "count": count, "category": category}
            )
        outputs[variant] = rows
    return outputs


def indicator_numeric_rows(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indicator_rows, numeric_rows = [], []
    by_turn = {v: {str(row["source_turn_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    for source in pages["Old"]:
        turn = str(source["source_turn_id"])
        ir = {
            "session_id": source["session_id"],
            "source_turn_id": turn,
            "user_indicator_set": extract_indicators(str(source["source_user"])),
            "assistant_indicator_set": extract_indicators(str(source["source_assistant"])),
        }
        nr = {"session_id": source["session_id"], "source_turn_id": turn}
        for variant in ADD_VARIANTS:
            row = by_turn[variant][turn]
            summary = str(row["summary"])
            embedding = str(row["embedding_text"])
            ir[f"{variant}_summary_indicator_set"] = extract_indicators(summary)
            ir[f"{variant}_summary_indicator_count"] = len(extract_indicators(summary))
            nr[f"{variant}_numeric_count"] = len(regex_values(NUMBER_PATTERN, embedding))
            nr[f"{variant}_percentage_count"] = len(regex_values(PERCENT_PATTERN, embedding))
            nr[f"{variant}_currency_amount_count"] = len(regex_values(CURRENCY_PATTERN, embedding))
            nr[f"{variant}_year_count"] = len(regex_values(YEAR_PATTERN, embedding))
        indicator_rows.append(ir)
        numeric_rows.append(nr)
    return indicator_rows, numeric_rows


def select_cases(
    transitions: Sequence[Mapping[str, Any]], query_ids: set[str]
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    selected = [qid for qid in MANDATORY_IDS if qid in query_ids]
    page_only = [qid for qid in MANDATORY_IDS if qid not in query_ids]
    categories = {}
    for candidate in NEW_VARIANTS:
        for search in SEARCH_VARIANTS:
            rows = [row for row in transitions if row["candidate"] == candidate and row["search_variant"] == search]
            for transition, movement in (("RESCUED", "max_promotion"), ("HURT", "max_demotion")):
                chosen = sorted(
                    (row for row in rows if row["transition"] == transition),
                    key=lambda row: (-int(row[movement]), str(row["query_id"])),
                )[:5]
                key = f"{candidate}_{search}_{transition}"
                categories[key] = [str(row["query_id"]) for row in chosen]
                for row in chosen:
                    if str(row["query_id"]) not in selected:
                        selected.append(str(row["query_id"]))
    return selected, categories, page_only


def fenced(value: Any) -> list[str]:
    return ["```text", str(value), "```"]


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    p2: Mapping[str, str],
    transitions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    queries = [dict(query, session_code=code) for code in SESSION_CODES for query in snapshots[code]["queries"]]
    query_by_id = {str(q["query_id"]): q for q in queries}
    selected, categories, page_only = select_cases(transitions, set(query_by_id))
    page_by_variant_id = {v: {str(row["old_page_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    page_by_variant_turn = {v: {str(row["source_turn_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    cases = []
    for qid in selected:
        query = query_by_id[qid]
        gold = {str(x) for x in query["eligible_gold_page_ids"]}
        maps = {config: rank_map(rankings[config][qid]) for config in CONFIGS}
        relevant = []
        for config in CONFIGS:
            relevant.extend(str(row["page_id"]) for row in rankings[config][qid][:5])
        relevant.extend(sorted(gold))
        relevant = list(dict.fromkeys(relevant))
        page_details = []
        for page_id in relevant:
            old = page_by_variant_id["Old"][page_id]
            detail = {
                "source_turn_id": old["source_turn_id"],
                "old_page_id": page_id,
                "is_gold": page_id in gold,
                "source_user": old["source_user"],
                "source_assistant": old["source_assistant"],
            }
            for variant in ADD_VARIANTS:
                row = page_by_variant_id[variant][page_id]
                detail[f"{variant}_summary"] = row["summary"]
                detail[f"{variant}_keywords"] = row["keywords"]
                detail[f"{variant}_embedding_text"] = row["embedding_text"]
            detail["ranks_scores"] = {
                config: {"rank": int(maps[config][page_id]["rank"]), "score": float(maps[config][page_id]["score"])}
                for config in CONFIGS
            }
            page_details.append(detail)
        p2_collision = {}
        p2_tokens = text_tokens(p2[qid])
        for variant in ADD_VARIANTS:
            config = f"{variant}_P2"
            p2_collision[variant] = {
                "gold_shared_terms": {
                    page_id: sorted(p2_tokens & text_tokens(str(page_by_variant_id[variant][page_id]["summary"])))
                    for page_id in gold
                },
                "top5_nongold": [
                    {
                        "source_turn_id": row["source_turn_id"],
                        "summary": page_by_variant_id[variant][str(row["page_id"])]["summary"],
                        "shared_terms": sorted(
                            p2_tokens & text_tokens(str(page_by_variant_id[variant][str(row["page_id"])]["summary"]))
                        ),
                    }
                    for row in rankings[config][qid][:5]
                    if str(row["page_id"]) not in gold
                ],
            }
        cases.append(
            {
                "query_id": qid,
                "session_id": query["session_code"],
                "selection_categories": [k for k, ids in categories.items() if qid in ids],
                "original_query": query["original_query"],
                "search_baseline_actual_query": query["original_query"],
                "search_p2_actual_query": p2[qid],
                "gold_page_ids": sorted(gold),
                "rankings": {
                    config: [
                        {
                            "rank": int(row["rank"]),
                            "score": float(row["score"]),
                            "source_turn_id": row["source_turn_id"],
                            "page_id": row["page_id"],
                            "is_gold": str(row["page_id"]) in gold,
                        }
                        for row in rankings[config][qid][:DISPLAY_K]
                    ]
                    for config in CONFIGS
                },
                "gold_results": [
                    {
                        "gold_page_id": page_id,
                        "source_turn_id": page_by_variant_id["Old"][page_id]["source_turn_id"],
                        **{
                            config: {
                                "rank": int(maps[config][page_id]["rank"]),
                                "score": float(maps[config][page_id]["score"]),
                            }
                            for config in CONFIGS
                        },
                    }
                    for page_id in sorted(gold)
                ],
                "p2_state_template_collision": p2_collision,
                "relevant_pages": page_details,
            }
        )
    page_cases = []
    for turn in page_only:
        old = page_by_variant_turn["Old"][turn]
        row = {
            "source_turn_id": turn,
            "session_id": old["session_id"],
            "note": "Frozen source Page, not one of the 99 evaluation Queries; no synthetic ranking/Gold was created.",
            "source_user": old["source_user"],
            "source_assistant": old["source_assistant"],
        }
        for variant in ADD_VARIANTS:
            page = page_by_variant_turn[variant][turn]
            row[f"{variant}_summary"] = page["summary"]
            row[f"{variant}_keywords"] = page["keywords"]
            row[f"{variant}_embedding_text"] = page["embedding_text"]
        page_cases.append(row)
    payload = {"selection": categories, "evaluation_cases": cases, "page_only_cases": page_cases}
    lines = [
        "# MidTerm Add Prompt Multi-Candidate Ablation — Representative Cases",
        "",
        "All rankings use frozen per-Query visible Pages and eligible Gold. Page-only mandatory turns are shown without inventing evaluation rankings.",
        "",
        "## Automatic selection",
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
                "Original Query:",
                "",
                *fenced(case["original_query"]),
                "",
                "Search-Baseline actual query:",
                "",
                *fenced(case["search_baseline_actual_query"]),
                "",
                "Search-P2 actual query:",
                "",
                *fenced(case["search_p2_actual_query"]),
            )
        )
        for config in CONFIGS:
            lines.extend(("", f"### {config} Top10", ""))
            for row in case["rankings"][config]:
                lines.append(
                    f"#{row['rank']} {row['source_turn_id']} score={row['score']:.9f} "
                    f"{'GOLD' if row['is_gold'] else 'NON-GOLD'}"
                )
        lines.extend(
            (
                "",
                "### All Eligible Gold — 8 ranks/scores",
                "",
                "```json",
                json.dumps(case["gold_results"], ensure_ascii=False, indent=2),
                "```",
                "",
                "### P2 query shared terms with Gold / Top5 Non-Gold summaries",
                "",
                "```json",
                json.dumps(case["p2_state_template_collision"], ensure_ascii=False, indent=2),
                "```",
                "",
                "### Relevant Pages (union of eight Top5 + all Gold)",
                "",
            )
        )
        for page in case["relevant_pages"]:
            lines.extend(
                (
                    "",
                    f"#### {page['source_turn_id']}",
                    "",
                    f"Gold: {page['is_gold']}",
                    "",
                    "```json",
                    json.dumps(page["ranks_scores"], ensure_ascii=False, indent=2),
                    "```",
                )
            )
            for title, key in (("Source User", "source_user"), ("Source Assistant", "source_assistant")):
                lines.extend(("", f"{title}:", "", *fenced(page[key])))
            for variant in ADD_VARIANTS:
                lines.extend(
                    (
                        "",
                        f"{variant} summary:",
                        "",
                        *fenced(page[f"{variant}_summary"]),
                        "",
                        f"{variant} keywords:",
                        "",
                        *fenced(json.dumps(page[f"{variant}_keywords"], ensure_ascii=False)),
                        "",
                        f"{variant} actual embedding text:",
                        "",
                        *fenced(page[f"{variant}_embedding_text"]),
                    )
                )
    lines.extend(("", "## Mandatory Page-only Cases", ""))
    for case in page_cases:
        lines.extend(
            (
                "",
                f"### {case['source_turn_id']}",
                "",
                case["note"],
                "",
                "Source User:",
                "",
                *fenced(case["source_user"]),
                "",
                "Source Assistant:",
                "",
                *fenced(case["source_assistant"]),
            )
        )
        for variant in ADD_VARIANTS:
            lines.extend(
                (
                    "",
                    f"{variant} summary:",
                    "",
                    *fenced(case[f"{variant}_summary"]),
                    "",
                    f"{variant} keywords:",
                    "",
                    *fenced(json.dumps(case[f"{variant}_keywords"], ensure_ascii=False)),
                    "",
                    f"{variant} actual embedding text:",
                    "",
                    *fenced(case[f"{variant}_embedding_text"]),
                )
            )
    return payload, "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompts, prompt_hashes = freeze_prompts(args.output_dir)
    old_prompt_path = args.output_dir / "prompts/Add_Old_current.txt"
    old_prompt_path.write_text(MIDTERM_PAGE_SUMMARY_PROMPT, encoding="utf-8")
    snapshots, old_pages, queries = load_pages_queries()
    p2 = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    validations, old_rankings = validate_old(snapshots, query_vectors, old_vectors)
    validations.update(
        {
            "D": {"status": "PASS" if len(queries) == 99 else "FAIL", "evaluation_query_count": len(queries)},
            "E": {
                "status": "PASS" if sum(len(q["eligible_gold_page_ids"]) for q in queries) == 154 else "FAIL",
                "eligible_gold_count": sum(len(q["eligible_gold_page_ids"]) for q in queries),
            },
            "G": {
                "status": "PASS",
                "contract": "Search-Baseline text assigned directly from original_query",
                "query_count": len(queries),
            },
            "H": {
                "status": "PASS" if set(p2) == {str(q["query_id"]) for q in queries} else "FAIL",
                "contract": "P2 text content hash validated against frozen embedding cache",
            },
            "I": {
                "status": "PASS",
                "input_field_names": ["CURRENT_USER", "CURRENT_ASSISTANT"],
                "forbidden_inputs_excluded": True,
            },
        }
    )
    config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))["llm"]["config"]
    if not config.get("api_key") or str(config["api_key"]).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY required")
    if str(config["model"]) != "deepseek-v4-flash" or float(config["temperature"]) != 0.1:
        raise ValueError("Production Summary model/config changed")
    generated, generation_meta = {}, {}
    for variant in NEW_VARIANTS:
        generated[variant], generation_meta[variant] = await generate_variant(
            args, variant, old_pages, prompts[variant], prompt_hashes[variant], config
        )
    after_hashes = {
        variant: sha256_file(args.output_dir / "prompts" / PROMPT_FILES[variant]) for variant in NEW_VARIANTS
    }
    validations["J"] = {
        "status": "PASS" if after_hashes == prompt_hashes else "FAIL",
        "before": prompt_hashes,
        "after": after_hashes,
    }
    if validations["J"]["status"] != "PASS":
        raise AssertionError(validations["J"])
    pages = build_pages(old_pages, generated, prompt_hashes)
    source_sets = {variant: {str(row["source_turn_id"]) for row in pages[variant]} for variant in ADD_VARIANTS}
    validations["C"] = {
        "status": "PASS"
        if all(value == source_sets["Old"] and len(value) == 333 for value in source_sets.values())
        else "FAIL",
        "counts": {k: len(v) for k, v in source_sets.items()},
    }
    variant_by_old = {v: {str(row["old_page_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    visibility_hashes = {}
    for variant in ADD_VARIANTS:
        mapping = {}
        for code in SESSION_CODES:
            for vis in snapshots[code]["visibility"]:
                mapping[str(vis["query_id"])] = [
                    str(variant_by_old[variant][str(pid)]["source_turn_id"]) for pid in vis["visible_page_ids"]
                ]
        visibility_hashes[variant] = stable_hash(mapping)
    validations["F"] = {
        "status": "PASS" if len(set(visibility_hashes.values())) == 1 else "FAIL",
        "mapping_hashes": visibility_hashes,
    }
    if any(validations[x]["status"] != "PASS" for x in "ABCDEFGHIJ"):
        raise AssertionError(validations)
    vectors: dict[str, dict[str, list[float]]] = {"Old": old_vectors}
    embedding_meta = {}
    cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    for variant in NEW_VARIANTS:
        ids = [f"{variant}:{row['source_turn_id']}" for row in pages[variant]]
        texts = [str(row["embedding_text"]) for row in pages[variant]]
        encoded, metadata = cache.encode(
            PRODUCTION_EMBEDDING, f"add-{variant}-pages-S001-S005", ids, texts, measure_individual=False
        )
        vectors[variant] = {
            str(row["old_page_id"]): encoded[f"{variant}:{row['source_turn_id']}"] for row in pages[variant]
        }
        embedding_meta[variant] = metadata
        if int(metadata["dimension"]) != 512 or len(vectors[variant]) != 333:
            raise AssertionError(f"{variant} embedding mismatch")
    rankings = dict(old_rankings)
    for variant in NEW_VARIANTS:
        rankings[f"{variant}_Baseline"] = rank_configuration(
            snapshots, query_vectors["Search-Baseline"], vectors[variant]
        )
        rankings[f"{variant}_P2"] = rank_configuration(snapshots, query_vectors["Search-P2"], vectors[variant])
    retrieval_rows, aggregate, _ = rank_metrics(snapshots, rankings)
    gold_rows, transition_rows, page_comparisons, session_rows = comparison_outputs(snapshots, rankings, pages)
    task_rows, dependency_rows, binding_rows, focus_rows, audit_summary = audit_rows(pages)
    exact_rows, normalized_rows, collision_rows, duplicate_stats = representation_audits(pages)
    for variant in ADD_VARIANTS:
        audit_summary[variant].update(
            {"cross_task_collision_rate": duplicate_stats[variant]["cross_task_collision_rate"]}
        )
    representation_stats = generic_representation_stats(pages, duplicate_stats, audit_summary)
    similarity_by_session, similarity_summary = similarity_outputs(pages, vectors)
    fidelity = fidelity_rows(pages)
    indicators, numeric = indicator_numeric_rows(pages)
    separation = separation_rows(snapshots, rankings)
    neighborhood = neighborhood_rows(snapshots, vectors)
    keywords = keyword_outputs(pages)
    cases, case_markdown = representative_outputs(snapshots, pages, rankings, p2, transition_rows)
    page_diff = []
    by_turn = {v: {str(row["source_turn_id"]): row for row in pages[v]} for v in ADD_VARIANTS}
    for old in pages["Old"]:
        turn = str(old["source_turn_id"])
        row = {"session_id": old["session_id"], "source_turn_id": turn, "old_page_id": old["old_page_id"]}
        for variant in ADD_VARIANTS:
            current = by_turn[variant][turn]
            row[f"{variant}_summary"] = current["summary"]
            row[f"{variant}_keywords"] = current["keywords"]
            row[f"{variant}_embedding_text"] = current["embedding_text"]
        page_diff.append(row)
    for variant in NEW_VARIANTS:
        write_jsonl(args.output_dir / f"pages/add_{variant}_pages.jsonl", pages[variant])
    write_csv(args.output_dir / "pages/page_diff.csv", page_diff)
    write_csv(args.output_dir / "pages/page_representation_stats.csv", representation_stats)
    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", retrieval_rows)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", transition_rows)
    write_csv(args.output_dir / "metrics/page_comparisons.csv", page_comparisons)
    analysis = args.output_dir / "analysis"
    for name, rows in (
        ("task_alignment_audit.csv", task_rows),
        ("dependency_audit.csv", dependency_rows),
        ("task_result_binding_audit.csv", binding_rows),
        ("exact_summary_duplicates.csv", exact_rows),
        ("normalized_template_duplicates.csv", normalized_rows),
        ("cross_task_collision.csv", collision_rows),
        ("page_similarity_summary.csv", similarity_summary),
        ("page_similarity_by_session.csv", similarity_by_session),
        ("gold_nongold_separation.csv", separation),
        ("gold_neighborhood_pollution.csv", neighborhood),
        ("indicator_audit.csv", indicators),
        ("numeric_overload_audit.csv", numeric),
        ("summary_focus_audit.csv", focus_rows),
    ):
        write_csv(analysis / name, rows)
    write_jsonl(analysis / "fidelity_audit.jsonl", fidelity)
    for variant in ADD_VARIANTS:
        write_csv(analysis / f"keyword_frequency_{'old' if variant == 'Old' else variant}.csv", keywords[variant])
    dump_json(args.output_dir / "representative_cases.json", cases)
    (args.output_dir / "representative_cases.md").write_text(case_markdown, encoding="utf-8")
    search_gains = {
        variant: aggregate[f"{variant}_P2"]["recall_at_5"] - aggregate[f"{variant}_Baseline"]["recall_at_5"]
        for variant in ADD_VARIANTS
    }
    fidelity_counts = {
        variant: sum(row["add_variant"] == variant and row["status"] == "POTENTIAL_UNSUPPORTED" for row in fidelity)
        for variant in NEW_VARIANTS
    }
    metadata = {
        "experiment_name": "midterm_add_prompt_multi_candidate_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "prompt_sha256_before_first_llm_call": {"Old": sha256_file(old_prompt_path), **prompt_hashes},
        "prompt_sha256_after_generation": {"Old": sha256_file(old_prompt_path), **after_hashes},
        "prompts_frozen": True,
        "prompt_adaptation_after_results": False,
        "input_contract": "CURRENT_USER + CURRENT_ASSISTANT only",
        "summary_model": config["model"],
        "thinking_mode": "disabled",
        "temperature": float(config["temperature"]),
        "top_p": float(config["top_p"]),
        "top_k": int(config["top_k"]),
        "generation": generation_meta,
        "old_summary_llm_calls": 0,
        "old_page_embedding_reused": 333,
        "new_page_embeddings": {variant: 333 for variant in NEW_VARIANTS},
        "embedding_metadata": embedding_meta,
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "dimension": 512,
        "baseline_query_embedding_reused": 99,
        "p2_query_embedding_reused": 99,
        "baseline_new_llm_calls": 0,
        "p2_new_llm_calls": 0,
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "validation_A_J": validations,
        "validation_A_J_all_pass": all(validations[x]["status"] == "PASS" for x in "ABCDEFGHIJ"),
        "search_p2_gain_r5": search_gains,
        "audit_summary": audit_summary,
        "duplicate_summary": duplicate_stats,
        "fidelity_potential_unsupported_count": fidelity_counts,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "retrieval": retrieval_rows,
                "comparisons": [row for row in page_comparisons if row["comparison_role"] == "PRIMARY_OLD_BASELINE"],
                "metadata": metadata,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
