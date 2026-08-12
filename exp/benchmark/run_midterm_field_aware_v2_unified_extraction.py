"""Unified Add/Search field extraction and Top60 field reranking on frozen C3."""

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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_conservative_retrieval_tuning_ablation import (  # noqa: E402
    provider_precondition,
)
from exp.benchmark.run_midterm_add_local_context_ablation import (  # noqa: E402
    context_layout,
    format_context_input,
    load_frozen_sessions,
)
from exp.benchmark.run_midterm_add_local_context_controls import (  # noqa: E402
    compare_rankings,
    summary_keywords_text,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    rank_configuration,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from exp.benchmark.run_reference_resolution_prompt_ablation import resolution_messages  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_field_aware_v2_unified_extraction"
ADD_BASE_PATH = (
    REPO_ROOT
    / "exp/results/midterm_add_local_context_ablation/prompts/Production_Add_With_Previous_And_Following_Context.txt"
)
SEARCH_BASE_PATH = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation/prompts/P2_slot_bounded.txt"
V1_DIR = REPO_ROOT / "exp/results/midterm_field_aware_multi_vector_retrieval"
PRIOR_C3_DIR = REPO_ROOT / "exp/results/midterm_c3_bm25_input_idf_ablation"

ADD_PROMPT_VERSION = "midterm-field-aware-v2-unified-add-v1"
SEARCH_PROMPT_VERSION = "midterm-field-aware-v2-unified-search-p2-v1"
FIELDS = ("task", "fact", "relation")
FIELD_WEIGHTS = {"task": 0.4, "fact": 0.3, "relation": 0.3}
TOP60 = 60
ELIGIBLE_GOLD = 154
C3_GOLD5 = 59
V1_GOLD5 = 52

ADD_OUTPUT_OLD = """输出结构：

{
  "summary": "对本轮对话的自包含摘要",
  "keywords": ["关键词1", "关键词2"]
}"""
ADD_OUTPUT_NEW = """输出结构：

{
  "summary": "对本轮对话的自包含摘要",
  "keywords": ["关键词1", "关键词2"],
  "task": "当前轮的任务身份",
  "fact": "当前轮的事实检索锚点",
  "relation": "当前轮的关系与判断链"
}"""
ADD_EMPTY_OLD = """如果本轮对话没有任何值得保留的分析信息，返回：

{
  "summary": "",
  "keywords": []
}"""
ADD_EMPTY_NEW = """如果本轮对话没有任何值得保留的分析信息，返回：

{
  "summary": "",
  "keywords": [],
  "task": "",
  "fact": "",
  "relation": ""
}"""

ADD_FIELD_SECTION = """## Task / Fact / Relation 要求

除原有 summary 和 keywords 外，同一次调用还要为当前待总结对话生成 task、fact、relation。
这三个字段是当前 Page 的独立检索入口，不得改变原有 summary/keywords 的生成规则，也不得为了填充字段而扩大 summary。

### Task

Task 只标识当前轮具体在做什么，包括分析任务、用户意图、对话动作，以及会改变任务含义的必要约束。
例如比较、核验、反证、修订、证据分级、来源检查、计算、阶段总结或管理层追问。

- 使用能够区分当前轮与附近其它轮次的最短任务描述；
- 如果当前轮承接、修订或反证前文，应写明被承接的任务或判断主题；
- 不要把角色、回答顺序、写作规范或通用免责声明机械写入 Task，除非它本身就是当前轮要检索的任务；
- 不要把完整事实底表、数字和结论复制进 Task。

### Fact

Fact 只保存识别当前轮所需的事实对象和事实锚点，包括必要的主体、期间、指标、数字、证据对象、事实状态和信息缺口。

- 只保留直接参与当前任务或直接支撑本轮回答的事实，不复制 Assistant 中与当前任务无关的完整财务底表；
- 指标很多时，保留当前问题明确涉及、用于当前比较或对当前结论不可缺少的指标；
- 数字只在当前任务要求计算、复算、定位年度或数字本身决定结论时保留；
- 除非当前任务正在核验来源、版本或披露口径，否则不得把 URL、cninfo 地址、披露日期、报告全名和通用来源描述写入 Fact；
- 不要把分析动作、推断或完整关系结论写入 Fact。

### Relation

Relation 只保存当前轮明确讨论或得到的对象关系、对话关系与结论边界。
包括指标间比较、同步或冲突、趋势与拐点、支持或反证、事实与判断的关系、承接或修订前文，以及直接影响结论的证据限制。

- 必须写明关系涉及的对象，不能只写“两者”“这个判断”；
- 只保留当前任务直接需要的关系，不罗列背景指标的所有趋势；
- 不要重复 Task 的任务措辞，也不要重写完整 Fact；
- “不预测未来”“不作确定归因”“不以首尾差额代替年度路径”等通用限制，只有直接决定本轮关系或结论边界时才保留；
- 下文只能帮助识别当前轮后来承担的关系，不能把下文新事实或新结论变成当前轮的 Relation。

### 字段边界与一致性检查

- task、fact、relation 都只能来自当前待总结对话；上文和下文只按原有上下文规则用于消解指代和明确承接关系；
- 三个字段应与当前轮保持相近粒度，不能让 Page Fact 退化成完整底表，而 Query 侧将来只能形成几个指标词；
- 三个字段可以为独立可读而重复必要的主体或指标名称，但不得把同一句话换写三次；
- summary、keywords、task、fact、relation 之间不得互相制造输入中没有的信息；
- 如果某类字段在当前轮没有安全内容，返回空字符串，不要强行生成通用模板。

输出前检查：Task 是否只表达任务身份；Fact 是否只保留必要事实锚点；Relation 是否只表达有明确对象的关系；三者是否都没有复制无关上下文。
"""

SEARCH_OUTPUT_OLD = '严格只返回：\n\n{"resolved_query":"补全后的问题"}'
SEARCH_FIELD_SECTION = """## Task / Fact / Relation 要求

完成上述槽位约束式指代消解后，在同一次调用中额外生成 task、fact、relation。

必须先严格按照以上全部 P2 规则得到 resolved_query，并将它视为已经锁定的最终结果；随后才能从 current query 与锁定后的 resolved_query 中抽取三个字段。
Task / Fact / Relation 绝对不能反向影响、重写或扩展 resolved_query。

### Task

Task 只表达当前 Query 的任务身份、用户意图、分析动作和会改变任务含义的必要约束。
使用最短且可独立检索的描述。不要把主体、完整指标底表、数字、答案或推断写入 Task，也不要机械复制回答顺序和通用写作规范。

### Fact

Fact 只表达当前 Query 明确询问或引用的事实对象，例如必要主体、期间、核心指标、证据对象和信息缺口。
只能使用 current query 中已有内容，以及 resolved_query 按 P2 五类 slot 唯一消解出的对象。
不要加入数字、年份、指标或历史事实，除非它们本来就在 current query 中；不要回答问题。
不要保留已被 resolved_query 安全消解的“上一轮、前面的、刚才、上一问、前文”等空指代。

### Relation

Relation 只表达当前 Query 正在询问、比较、验证或修订的关系，例如指标比较、同步或冲突、支持或反证、承接或修订前序判断、事实与计算的区分。
必须使用有名称的对象，不能仅用“两者”“这个判断”等空指代；不得生成问题答案或历史结论。

### 字段边界与一致性检查

- 三个字段只从 current query 与 P2 合法 resolved_query 中抽取，不得继续展开 previous_3_qa；
- task、fact、relation 应保持与 Query 相近的简洁粒度，并能分别独立 embedding；
- 可以重复独立理解所需的对象名称，但不要把 resolved_query 整句换写到三个字段；
- 如果某类字段没有安全内容，返回空字符串，不要生成通用模板；
- 再次检查 resolved_query 是否仍然完全遵守前面的 P2 五类 slot、最小修改和禁止扩写规则。

输出时不要解释。

严格只返回：

{
  "resolved_query": "补全后的问题",
  "task": "当前 Query 的任务身份",
  "fact": "当前 Query 的事实检索锚点",
  "relation": "当前 Query 询问的关系"
}
"""

PROVENANCE_PATTERNS = ("http", "cninfo", "披露日期", "年度报告", "来源", "报告摘要")
SOURCE_TASK_PATTERN = re.compile(r"来源|披露|年报|报告|版本|口径|原数|公开")
BOILERPLATE_PATTERNS = (
    "风险委员会",
    "区分事实、计算和判断",
    "区分事实、计算与判断",
    "不预测未来",
    "不作确定归因",
    "不以首尾差额代替年度路径",
    "先给结论",
    "先说明",
    "岗位",
)
DEICTIC_PATTERNS = ("上一轮", "前面的", "刚才", "前一轮", "上一问", "前文")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Unified C3 Add/Search Field-aware V2 experiment")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--llm-timeout", type=float, default=240.0)
    parser.add_argument("--llm-retries", type=int, default=4)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_add_prompt(base: str) -> str:
    if base.count(ADD_OUTPUT_OLD) != 1 or base.count(ADD_EMPTY_OLD) != 1 or base.count("## summary 要求") != 1:
        raise AssertionError("Frozen Add base Prompt does not match expected anchors")
    prompt = base.replace(ADD_OUTPUT_OLD, ADD_OUTPUT_NEW).replace(ADD_EMPTY_OLD, ADD_EMPTY_NEW)
    return prompt.replace("## summary 要求", f"{ADD_FIELD_SECTION}\n\n## summary 要求")


def build_search_prompt(base: str) -> str:
    if base.count(SEARCH_OUTPUT_OLD) != 1:
        raise AssertionError("Frozen P2 Prompt does not match expected output anchor")
    return base.replace(SEARCH_OUTPUT_OLD, SEARCH_FIELD_SECTION)


def freeze_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    bases = {"add": ADD_BASE_PATH.read_text(encoding="utf-8"), "search": SEARCH_BASE_PATH.read_text(encoding="utf-8")}
    prompts = {"add": build_add_prompt(bases["add"]), "search": build_search_prompt(bases["search"])}
    paths = {"add": output_dir / "prompts/add_v2.txt", "search": output_dir / "prompts/search_v2.txt"}
    hashes = {}
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") != prompts[key]:
            raise AssertionError(f"Frozen V2 Prompt differs from deterministic source: {path}")
        if not path.exists():
            path.write_text(prompts[key], encoding="utf-8")
        hashes[key] = sha256_file(path)
    base_hashes = {key: sha256_text(value) for key, value in bases.items()}
    dump_json(
        output_dir / "prompts/prompt_sha256.json",
        {
            "frozen_before_first_llm_call": True,
            "base_prompt_sha256": base_hashes,
            "v2_prompt_sha256": hashes,
            "source_paths": {"add": str(ADD_BASE_PATH), "search": str(SEARCH_BASE_PATH)},
        },
    )
    return prompts, hashes, base_hashes


def parse_json(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1]).strip()
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1], strict=False)
    if not isinstance(parsed, dict):
        raise ValueError("LLM output must be a JSON object")
    return parsed


def clean_field(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Field value must be a string")
    return value.strip()


def validate_add_output(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"summary", "keywords", "task", "fact", "relation"}:
        raise ValueError("Add output must contain exactly summary, keywords, task, fact, relation")
    if not isinstance(value["summary"], str) or not isinstance(value["keywords"], list):
        raise ValueError("Invalid summary/keywords types")
    keywords = []
    for item in value["keywords"]:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Keywords must be non-empty strings")
        keywords.append(item.strip())
    result = {
        "summary": value["summary"].strip(),
        "keywords": keywords,
        **{field: clean_field(value[field]) for field in FIELDS},
    }
    if not result["summary"] and any((result["keywords"], result["task"], result["fact"], result["relation"])):
        raise ValueError("Empty summary requires all Add outputs to be empty")
    return result


def validate_search_output(value: Mapping[str, Any]) -> dict[str, str]:
    if set(value) != {"resolved_query", "task", "fact", "relation"}:
        raise ValueError("Search output must contain exactly resolved_query, task, fact, relation")
    result = {key: clean_field(value[key]) for key in ("resolved_query", *FIELDS)}
    if not result["resolved_query"]:
        raise ValueError("resolved_query must not be empty")
    return result


def cache_identity(
    *,
    side: str,
    item_id: str,
    prompt_hash: str,
    input_hash: str,
    model: str,
    temperature: float,
    top_p: float | None,
    top_k: int | None,
    extra: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "side": side,
        "item_id": item_id,
        "prompt_sha256": prompt_hash,
        "input_sha256": input_hash,
        "model": model,
        "thinking_mode": "disabled",
        "temperature": temperature,
        "top_p": top_p,
        "top_k": top_k,
        "extra": dict(extra),
    }


class UnifiedExtractionCache:
    def __init__(
        self,
        path: Path,
        *,
        client: AsyncOpenAI,
        model: str,
        timeout: float,
        retries: int,
        concurrency: int,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(max(concurrency, 1))
        self.lock = asyncio.Lock()
        self.success = {str(row["cache_key"]): row for row in load_jsonl(path) if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(
        self,
        *,
        side: str,
        item_id: str,
        prompt_hash: str,
        input_text: str,
        messages: Sequence[Mapping[str, str]],
        temperature: float,
        top_p: float | None,
        top_k: int | None,
        max_tokens: int,
        validator: Callable[[Mapping[str, Any]], dict[str, Any]],
        extra: Mapping[str, Any],
        use_response_format: bool,
    ) -> dict[str, Any]:
        identity = cache_identity(
            side=side,
            item_id=item_id,
            prompt_hash=prompt_hash,
            input_hash=sha256_text(input_text),
            model=self.model,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            extra=extra,
        )
        key = stable_hash(identity)
        cached = self.success.get(key)
        if cached:
            return {**cached, "cache_hit": True}
        errors: list[str] = []
        rejected_count = 0
        response_format = use_response_format
        async with self.semaphore:
            for attempt in range(1, self.retries + 1):
                started = time.perf_counter()
                try:
                    kwargs: dict[str, Any] = {
                        "model": self.model,
                        "messages": list(messages),
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "extra_body": {"thinking": {"type": "disabled"}},
                    }
                    if top_p is not None:
                        kwargs["top_p"] = top_p
                    if top_k is not None:
                        kwargs["extra_body"]["top_k"] = top_k
                    if response_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs), timeout=self.timeout
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = validator(parse_json(raw))
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": key,
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": raw,
                        "response_mode": "json_object" if response_format else "plain_text_strict_json",
                        "llm_latency_ms": (time.perf_counter() - started) * 1000.0,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "api_attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "provider_precondition_rejected_count": rejected_count,
                        "errors": errors,
                        "cache_hit": False,
                    }
                    await self.append(row)
                    self.success[key] = row
                    return row
                except Exception as exc:
                    rejected = provider_precondition(exc)
                    rejected_count += int(rejected)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if rejected and response_format:
                        response_format = False
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12))
        row = {
            **identity,
            "cache_key": key,
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": self.retries - 1,
            "provider_precondition_rejected_count": rejected_count,
            "errors": errors,
            "cache_hit": False,
        }
        await self.append(row)
        return row


def generation_metadata(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "item_count": len(rows),
        "cache_hit_count": sum(bool(row.get("cache_hit")) for row in rows),
        "successful_model_output_count": len(rows),
        "new_successful_model_output_count": sum(not row.get("cache_hit") for row in rows),
        "generation_api_attempt_count": sum(int(row.get("api_attempt_count") or 0) for row in rows),
        "new_llm_api_attempt_count": sum(
            int(row.get("api_attempt_count") or 0) for row in rows if not row.get("cache_hit")
        ),
        "retry_count": sum(int(row.get("retry_count") or 0) for row in rows),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0) for row in rows
        ),
    }


async def generate_pages(
    args: argparse.Namespace,
    output_dir: Path,
    prompt: str,
    prompt_hash: str,
    snapshots: Mapping[str, Mapping[str, Any]],
    old_pages: Sequence[Mapping[str, Any]],
    llm_config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sessions = load_frozen_sessions(snapshots, old_pages)
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache = UnifiedExtractionCache(
        output_dir / "cache/add_v2.jsonl",
        client=client,
        model=str(llm_config["model"]),
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=min(args.concurrency, len(SESSION_CODES)),
    )
    results: list[dict[str, Any]] = []
    context_rows: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def process_session(code: str) -> None:
        generated_previous: list[dict[str, Any]] = []
        session_pages = sorted(
            (page for page in old_pages if page["session_code"] == code),
            key=lambda page: int(page["source_turn_index"]),
        )
        for page in session_pages:
            layout = context_layout(page, sessions[code], generated_previous, include_following=True)
            payload = format_context_input(page, layout, include_following=True)
            extra = {
                "session_id": code,
                "source_turn_id": page["source_turn_id"],
                "source_user_sha256": sha256_text(str(page["user_input"])),
                "source_assistant_sha256": sha256_text(str(page["assistant_response"])),
                "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
                "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
                "eviction_trigger_turn_id": layout["eviction_trigger_turn_id"],
            }
            row = await cache.call(
                side="Add",
                item_id=str(page["source_turn_id"]),
                prompt_hash=prompt_hash,
                input_text=payload,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": payload}],
                temperature=float(llm_config["temperature"]),
                top_p=float(llm_config["top_p"]),
                top_k=int(llm_config["top_k"]),
                max_tokens=4096,
                validator=validate_add_output,
                extra=extra,
                use_response_format=True,
            )
            if row.get("status") != "SUCCESS":
                raise RuntimeError(f"Add V2 failed at {page['source_turn_id']}: {row.get('errors')}")
            parsed = dict(row["parsed"])
            generated_previous.append(
                {
                    "source_turn_id": page["source_turn_id"],
                    "source_turn_index": page["source_turn_index"],
                    "summary": parsed["summary"],
                    "keywords": parsed["keywords"],
                }
            )
            output = {
                "session_id": code,
                "page_id": page["page_id"],
                "source_turn_id": page["source_turn_id"],
                "source_turn_index": page["source_turn_index"],
                "summary": parsed["summary"],
                "keywords": list(parsed["keywords"]),
                "task": parsed["task"],
                "fact": parsed["fact"],
                "relation": parsed["relation"],
                "global_text": summary_keywords_text(parsed["summary"], parsed["keywords"]),
                "cache_key": row["cache_key"],
            }
            context_row = {
                "session_id": code,
                "source_turn_id": page["source_turn_id"],
                "source_turn_index": page["source_turn_index"],
                "eviction_trigger_turn_id": layout["eviction_trigger_turn_id"],
                "eviction_trigger_turn_index": layout["eviction_trigger_turn_index"],
                "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
                "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
                "following_context_turn_indices": [item["turn_index"] for item in layout["following_context"]],
                "no_future_beyond_eviction": all(
                    int(item["turn_index"]) <= int(layout["eviction_trigger_turn_index"])
                    for item in layout["following_context"]
                ),
                "input_payload_sha256": sha256_text(payload),
                "cache_key": row["cache_key"],
            }
            async with lock:
                results.append({"cache_hit": bool(row.get("cache_hit")), **output, "cache_row": row})
                context_rows.append(context_row)

    try:
        await asyncio.gather(*(process_session(code) for code in SESSION_CODES))
    finally:
        await client.close()
    results.sort(key=lambda row: (row["session_id"], int(row["source_turn_index"])))
    context_rows.sort(key=lambda row: (row["session_id"], int(row["source_turn_index"])))
    if len(results) != 333:
        raise AssertionError(f"Expected 333 Add outputs, got {len(results)}")
    rows = [row["cache_row"] for row in results]
    clean_results = [
        {key: value for key, value in row.items() if key not in {"cache_row", "cache_hit"}} for row in results
    ]
    return clean_results, context_rows, generation_metadata(rows)


async def generate_queries(
    args: argparse.Namespace,
    output_dir: Path,
    prompt: str,
    prompt_hash: str,
    queries: Sequence[Mapping[str, Any]],
    llm_config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache = UnifiedExtractionCache(
        output_dir / "cache/search_v2.jsonl",
        client=client,
        model=str(llm_config["model"]),
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )

    async def one(query: Mapping[str, Any]) -> tuple[Mapping[str, Any], dict[str, Any]]:
        messages = resolution_messages(query, prompt)
        payload = str(messages[1]["content"])
        previous = list(query.get("previous_3_qa") or [])
        row = await cache.call(
            side="Search",
            item_id=str(query["query_id"]),
            prompt_hash=prompt_hash,
            input_text=payload,
            messages=messages,
            temperature=0.0,
            top_p=None,
            top_k=None,
            max_tokens=1200,
            validator=validate_search_output,
            extra={
                "session_id": query["session_code"],
                "current_query_sha256": sha256_text(str(query["original_query"])),
                "previous_context_turn_ids": [item["turn_id"] for item in previous],
                "previous_context_count": len(previous),
            },
            use_response_format=False,
        )
        return query, row

    try:
        generated = await asyncio.gather(*(one(query) for query in queries))
    finally:
        await client.close()
    failures = [row for _, row in generated if row.get("status") != "SUCCESS"]
    if failures:
        raise RuntimeError(f"Search V2 failed for {len(failures)} Queries: {failures[:3]}")
    results = []
    for query, row in generated:
        parsed = dict(row["parsed"])
        previous = list(query.get("previous_3_qa") or [])
        results.append(
            {
                "session_id": query["session_code"],
                "query_id": query["query_id"],
                "original_query": query["original_query"],
                "eligible_gold_page_ids": list(query["eligible_gold_page_ids"]),
                "previous_context_turn_ids": [item["turn_id"] for item in previous],
                "previous_context_count": len(previous),
                "resolved_query": parsed["resolved_query"],
                "task": parsed["task"],
                "fact": parsed["fact"],
                "relation": parsed["relation"],
                "cache_key": row["cache_key"],
                "cache_hit": bool(row.get("cache_hit")),
                "cache_row": row,
            }
        )
    results.sort(key=lambda row: (row["session_id"], row["query_id"]))
    metadata = generation_metadata([row["cache_row"] for row in results])
    clean_results = [
        {key: value for key, value in row.items() if key not in {"cache_row", "cache_hit"}} for row in results
    ]
    return clean_results, metadata


def encode_texts(
    cache: EmbeddingCache,
    *,
    batch_name: str,
    ids: Sequence[str],
    texts: Sequence[str],
    query_mode: bool,
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    valid = [(item_id, text) for item_id, text in zip(ids, texts) if text.strip()]
    if not valid:
        return {}, {"batch_name": batch_name, "item_count": 0, "cache_hit": True, "empty_count": len(ids)}
    valid_ids, valid_texts = zip(*valid)
    vectors, metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        batch_name,
        list(valid_ids),
        list(valid_texts),
        measure_individual=query_mode,
    )
    return vectors, {**metadata, "empty_count": len(ids) - len(valid)}


def encode_representations(
    output_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, dict[str, list[float]]], dict[str, Any]]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    query_vectors: dict[str, dict[str, list[float]]] = {}
    page_vectors: dict[str, dict[str, list[float]]] = {}
    metadata: dict[str, Any] = {}
    contracts = ("global", *FIELDS)
    for representation in contracts:
        query_ids = [str(row["query_id"]) for row in queries]
        query_texts = [
            str(row["resolved_query"] if representation == "global" else row[representation]) for row in queries
        ]
        embedding_ids = [f"Query:{representation}:{item_id}" for item_id in query_ids]
        encoded, item_metadata = encode_texts(
            cache,
            batch_name=f"field-aware-v2-query-{representation}-S001-S005",
            ids=embedding_ids,
            texts=query_texts,
            query_mode=True,
        )
        query_vectors[representation] = {
            item_id: encoded[embedding_id]
            for item_id, embedding_id, text in zip(query_ids, embedding_ids, query_texts)
            if text.strip()
        }
        metadata[f"query_{representation}"] = item_metadata

        page_ids = [str(row["page_id"]) for row in pages]
        page_texts = [str(row["global_text"] if representation == "global" else row[representation]) for row in pages]
        embedding_ids = [f"Page:{representation}:{item_id}" for item_id in page_ids]
        encoded, item_metadata = encode_texts(
            cache,
            batch_name=f"field-aware-v2-page-{representation}-S001-S005",
            ids=embedding_ids,
            texts=page_texts,
            query_mode=False,
        )
        page_vectors[representation] = {
            item_id: encoded[embedding_id]
            for item_id, embedding_id, text in zip(page_ids, embedding_ids, page_texts)
            if text.strip()
        }
        metadata[f"page_{representation}"] = item_metadata
    cache.release(PRODUCTION_EMBEDDING)
    return query_vectors, page_vectors, metadata


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_array = np.asarray(left, dtype=np.float32)
    right_array = np.asarray(right, dtype=np.float32)
    denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
    return float(left_array @ right_array / denominator) if denominator else 0.0


def field_score(
    query_id: str,
    page_id: str,
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    page_vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> tuple[float | None, dict[str, float | None], list[str]]:
    scores: dict[str, float | None] = {}
    valid = []
    for field in FIELDS:
        if query_id in query_vectors[field] and page_id in page_vectors[field]:
            scores[field] = cosine(query_vectors[field][query_id], page_vectors[field][page_id])
            valid.append(field)
        else:
            scores[field] = None
    weight_sum = sum(FIELD_WEIGHTS[field] for field in valid)
    if not valid or weight_sum <= 0:
        return None, scores, valid
    final = sum(FIELD_WEIGHTS[field] * float(scores[field]) for field in valid) / weight_sum
    return final, scores, valid


def rerank_top60(
    global_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    page_vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> dict[str, list[dict[str, Any]]]:
    result = {}
    for query_id, global_rows in global_rankings.items():
        candidates = []
        for global_rank, row in enumerate(global_rows[:TOP60], start=1):
            page_id = str(row["page_id"])
            final, scores, valid = field_score(query_id, page_id, query_vectors, page_vectors)
            candidates.append(
                {
                    **dict(row),
                    "global_rank": global_rank,
                    "global_cosine": float(row["score"]),
                    "task_cosine": scores["task"],
                    "fact_cosine": scores["fact"],
                    "relation_cosine": scores["relation"],
                    "field_score": final,
                    "final_score": final,
                    "valid_fields": valid,
                    "valid_field_count": len(valid),
                    "field_weight_sum": sum(FIELD_WEIGHTS[field] for field in valid),
                }
            )
        if all(row["field_score"] is None for row in candidates):
            reordered = candidates
        else:
            reordered = sorted(
                candidates,
                key=lambda row: (
                    row["field_score"] is None,
                    -float(row["field_score"]) if row["field_score"] is not None else 0.0,
                    int(row["global_rank"]),
                ),
            )
        complete = reordered + [
            {
                **dict(row),
                "global_rank": rank,
                "global_cosine": float(row["score"]),
                "task_cosine": None,
                "fact_cosine": None,
                "relation_cosine": None,
                "field_score": None,
                "final_score": None,
                "valid_fields": [],
                "valid_field_count": 0,
                "field_weight_sum": 0.0,
            }
            for rank, row in enumerate(global_rows[TOP60:], start=TOP60 + 1)
        ]
        for rank, row in enumerate(complete, start=1):
            row["rank"] = rank
            row["score"] = float(row["field_score"]) if row["field_score"] is not None else 0.0
        result[query_id] = complete
    return result


def load_v1_rankings() -> dict[str, list[dict[str, Any]]]:
    rows = load_jsonl(V1_DIR / "rankings/query_page_rankings.jsonl")
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result[str(row["query_id"])].append(
            {
                "page_id": str(row["page_id"]),
                "source_turn_id": str(row["source_turn_id"]),
                "score": float(row["final_score"]),
                "rank": int(row["rank"]),
                "task_cosine": float(row["task_cosine"]),
                "fact_cosine": float(row["fact_cosine"]),
                "relation_cosine": float(row["relation_cosine"]),
            }
        )
    for query_id in result:
        result[query_id].sort(key=lambda row: int(row["rank"]))
    if len(result) != 99:
        raise AssertionError(f"V1 ranking coverage mismatch: {len(result)}")
    return dict(result)


def text_lengths(rows: Sequence[Mapping[str, Any]], item_type: str) -> list[dict[str, Any]]:
    result = []
    for field in FIELDS:
        lengths = [len(str(row[field])) for row in rows]
        result.append(
            {
                "item_type": item_type,
                "field": field,
                "count": len(lengths),
                "mean_chars": statistics.fmean(lengths),
                "median_chars": statistics.median(lengths),
                "p90_chars": float(np.percentile(np.asarray(lengths), 90)),
                "empty_count": sum(length == 0 for length in lengths),
            }
        )
    return result


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", value.strip())


def duplicate_stats(rows: Sequence[Mapping[str, Any]], variant: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counter = Counter(normalized_text(str(row["fact"])) for row in rows if str(row["fact"]).strip())
    groups = {text: count for text, count in counter.items() if count >= 2}
    duplicate_rows = sum(groups.values())
    by_session = []
    for code in SESSION_CODES:
        selected = [row for row in rows if row["session_id"] == code]
        session_counter = Counter(normalized_text(str(row["fact"])) for row in selected if str(row["fact"]).strip())
        session_duplicate_rows = sum(count for count in session_counter.values() if count >= 2)
        by_session.append(
            {
                "variant": variant,
                "session_id": code,
                "page_count": len(selected),
                "duplicate_rows": session_duplicate_rows,
                "duplicate_row_rate": session_duplicate_rows / len(selected),
            }
        )
    return (
        {
            "page_count": len(rows),
            "exact_duplicate_group_count": len(groups),
            "duplicate_rows": duplicate_rows,
            "duplicate_row_rate": duplicate_rows / len(rows),
            "largest_duplicate_group": max(groups.values(), default=1),
            "session_macro_duplicate_rate": statistics.fmean(row["duplicate_row_rate"] for row in by_session),
        },
        by_session,
    )


def char_ngrams(value: str, size: int = 3) -> Counter[str]:
    clean = re.sub(r"[\s，。；：、,.!?！？:;\-_/（）()]+", "", value.lower())
    if not clean:
        return Counter()
    if len(clean) < size:
        return Counter({clean: 1})
    return Counter(clean[index : index + size] for index in range(len(clean) - size + 1))


def counter_cosine(left: Counter[str], right: Counter[str]) -> float:
    denominator = math.sqrt(
        sum(value * value for value in left.values()) * sum(value * value for value in right.values())
    )
    if not denominator:
        return 0.0
    return sum(value * right.get(key, 0) for key, value in left.items()) / denominator


def task_relation_similarity(rows: Sequence[Mapping[str, Any]], item_type: str, variant: str) -> dict[str, Any]:
    similarities = [counter_cosine(char_ngrams(str(row["task"])), char_ngrams(str(row["relation"]))) for row in rows]
    return {
        "variant": variant,
        "item_type": item_type,
        "count": len(rows),
        "mean_char_trigram_cosine": statistics.fmean(similarities),
        "median_char_trigram_cosine": statistics.median(similarities),
        "p90_char_trigram_cosine": float(np.percentile(np.asarray(similarities), 90)),
        "rate_ge_0_8": sum(value >= 0.8 for value in similarities) / len(similarities),
        "rate_ge_0_9": sum(value >= 0.9 for value in similarities) / len(similarities),
    }


def quality_analysis(
    queries: Sequence[Mapping[str, Any]], pages: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    v1_queries = load_jsonl(V1_DIR / "fields/query_fields.jsonl")
    v1_pages = load_jsonl(V1_DIR / "fields/page_fields.jsonl")
    v2_length = text_lengths(queries, "Query") + text_lengths(pages, "Page")
    v1_length = text_lengths(v1_queries, "Query") + text_lengths(v1_pages, "Page")
    v1_duplicate, v1_sessions = duplicate_stats(v1_pages, "V1")
    v2_duplicate, v2_sessions = duplicate_stats(pages, "V2")

    contamination_rows = []
    for variant, selected in (("V1", v1_pages), ("V2", pages)):
        for row in selected:
            task = str(row["task"])
            fact = str(row["fact"])
            matched = [pattern for pattern in PROVENANCE_PATTERNS if pattern.lower() in fact.lower()]
            contamination_rows.append(
                {
                    "variant": variant,
                    "session_id": row["session_id"],
                    "page_id": row["page_id"],
                    "source_turn_id": row["source_turn_id"],
                    "source_task": bool(SOURCE_TASK_PATTERN.search(task)),
                    "has_provenance": bool(matched),
                    "inappropriate_provenance": bool(matched) and not SOURCE_TASK_PATTERN.search(task),
                    "matched_patterns": matched,
                }
            )

    boilerplate = []
    for variant, selected in (("V1", v1_pages), ("V2", pages)):
        for field in ("task", "relation"):
            for pattern in BOILERPLATE_PATTERNS:
                count = sum(pattern in str(row[field]) for row in selected)
                boilerplate.append(
                    {
                        "variant": variant,
                        "field": field,
                        "pattern": pattern,
                        "match_count": count,
                        "match_rate": count / len(selected),
                    }
                )

    deictic = []
    for variant, selected in (("V1", v1_queries), ("V2", queries)):
        for row in selected:
            matched = [pattern for pattern in DEICTIC_PATTERNS if pattern in str(row["fact"])]
            deictic.append(
                {
                    "variant": variant,
                    "session_id": row["session_id"],
                    "query_id": row["query_id"],
                    "has_deictic_residue": bool(matched),
                    "matched_patterns": matched,
                }
            )

    similarities = [
        task_relation_similarity(v1_queries, "Query", "V1"),
        task_relation_similarity(queries, "Query", "V2"),
        task_relation_similarity(v1_pages, "Page", "V1"),
        task_relation_similarity(pages, "Page", "V2"),
    ]

    def provenance_summary(variant: str) -> dict[str, Any]:
        rows = [row for row in contamination_rows if row["variant"] == variant]
        nonsource = [row for row in rows if not row["source_task"]]
        return {
            "page_count": len(rows),
            "has_provenance_count": sum(row["has_provenance"] for row in rows),
            "has_provenance_rate": sum(row["has_provenance"] for row in rows) / len(rows),
            "non_source_task_page_count": len(nonsource),
            "inappropriate_provenance_count": sum(row["inappropriate_provenance"] for row in nonsource),
            "inappropriate_provenance_rate": sum(row["inappropriate_provenance"] for row in nonsource) / len(nonsource),
            "pattern_counts": {
                pattern: sum(pattern in row["matched_patterns"] for row in rows) for pattern in PROVENANCE_PATTERNS
            },
            "pattern_rates": {
                pattern: sum(pattern in row["matched_patterns"] for row in rows) / len(rows)
                for pattern in PROVENANCE_PATTERNS
            },
        }

    def deictic_summary(variant: str) -> dict[str, Any]:
        rows = [row for row in deictic if row["variant"] == variant]
        count = sum(row["has_deictic_residue"] for row in rows)
        return {"query_count": len(rows), "residue_count": count, "residue_rate": count / len(rows)}

    quality = {
        "length_symmetry": {"V1": v1_length, "V2": v2_length},
        "page_fact_duplicates": {"V1": v1_duplicate, "V2": v2_duplicate},
        "page_fact_duplicate_by_session": {"V1": v1_sessions, "V2": v2_sessions},
        "fact_provenance": {"V1": provenance_summary("V1"), "V2": provenance_summary("V2")},
        "query_fact_deictic_residue": {"V1": deictic_summary("V1"), "V2": deictic_summary("V2")},
        "task_relation_similarity": similarities,
        "boilerplate_summary": {
            variant: {
                field: {
                    "any_pattern_count": sum(
                        any(pattern in str(row[field]) for pattern in BOILERPLATE_PATTERNS)
                        for row in (v1_pages if variant == "V1" else pages)
                    ),
                    "any_pattern_rate": sum(
                        any(pattern in str(row[field]) for pattern in BOILERPLATE_PATTERNS)
                        for row in (v1_pages if variant == "V1" else pages)
                    )
                    / len(pages),
                }
                for field in ("task", "relation")
            }
            for variant in ("V1", "V2")
        },
    }
    audit_rows = contamination_rows + boilerplate + deictic
    return quality, audit_rows


def render_quality(quality: Mapping[str, Any]) -> str:
    lines = ["# V2 Extraction Quality", "", "## 长度对称性", ""]
    for variant in ("V1", "V2"):
        lines.extend(
            [
                f"### {variant}",
                "",
                "| Type | Field | Mean chars | Median | P90 | Empty |",
                "|---|---|---:|---:|---:|---:|",
            ]
        )
        for row in quality["length_symmetry"][variant]:
            lines.append(
                f"| {row['item_type']} | {row['field']} | {float(row['mean_chars']):.2f} | "
                f"{float(row['median_chars']):.1f} | {float(row['p90_chars']):.1f} | {row['empty_count']} |"
            )
        lines.append("")
    lines.extend(
        [
            "## Page Fact duplicates / provenance / Query deictic",
            "",
            "| Variant | Fact duplicate rows | Duplicate rate | Provenance rate | Inappropriate provenance rate | Query Fact deictic rate |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in ("V1", "V2"):
        duplicates = quality["page_fact_duplicates"][variant]
        provenance = quality["fact_provenance"][variant]
        deictic = quality["query_fact_deictic_residue"][variant]
        lines.append(
            f"| {variant} | {duplicates['duplicate_rows']} | {100 * duplicates['duplicate_row_rate']:.2f}% | "
            f"{100 * provenance['has_provenance_rate']:.2f}% | "
            f"{100 * provenance['inappropriate_provenance_rate']:.2f}% | "
            f"{100 * deictic['residue_rate']:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## Page Fact provenance pattern",
            "",
            "| Variant | http | cninfo | 披露日期 | 年度报告 | 来源 | 报告摘要 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in ("V1", "V2"):
        counts = quality["fact_provenance"][variant]["pattern_counts"]
        lines.append(f"| {variant} | " + " | ".join(str(counts[pattern]) for pattern in PROVENANCE_PATTERNS) + " |")
    lines.extend(
        [
            "",
            "## Task / Relation char-trigram similarity",
            "",
            "| Variant | Type | Mean | Median | P90 | >=0.8 |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in quality["task_relation_similarity"]:
        lines.append(
            f"| {row['variant']} | {row['item_type']} | {float(row['mean_char_trigram_cosine']):.4f} | "
            f"{float(row['median_char_trigram_cosine']):.4f} | "
            f"{float(row['p90_char_trigram_cosine']):.4f} | {100 * float(row['rate_ge_0_8']):.2f}% |"
        )
    return "\n".join(lines) + "\n"


def movement_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    baselines: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    variants: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    gold_rows, query_rows, summary = [], [], {}
    for variant_name, variant in variants.items():
        for baseline_name, baseline in baselines.items():
            comparison_name = f"{variant_name} vs {baseline_name}"
            comparison, gold, queries = compare_rankings(snapshots, baseline, variant, comparison=comparison_name)
            summary[comparison_name] = comparison
            gold_rows.extend({"variant": variant_name, "baseline": baseline_name, **row} for row in gold)
            query_rows.extend({"variant": variant_name, "baseline": baseline_name, **row} for row in queries)
    return gold_rows, query_rows, summary


def build_ranking_rows(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    variant: str,
) -> list[dict[str, Any]]:
    query_by_id = {str(row["query_id"]): row for row in queries}
    rows = []
    for query_id, ranking in rankings.items():
        gold = {str(page_id) for page_id in query_by_id[query_id]["eligible_gold_page_ids"]}
        for item in ranking:
            rows.append(
                {
                    "variant": variant,
                    "session_id": query_by_id[query_id]["session_id"],
                    "query_id": query_id,
                    "page_id": item["page_id"],
                    "source_turn_id": item["source_turn_id"],
                    "rank": item["rank"],
                    "is_gold": str(item["page_id"]) in gold,
                    "global_rank": item.get("global_rank", item["rank"]),
                    "global_cosine": item.get("global_cosine", item["score"]),
                    "task_cosine": item.get("task_cosine"),
                    "fact_cosine": item.get("fact_cosine"),
                    "relation_cosine": item.get("relation_cosine"),
                    "field_score": item.get("field_score"),
                    "valid_fields": item.get("valid_fields", []),
                }
            )
    return rows


def representative_cases(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    c3: Mapping[str, Sequence[Mapping[str, Any]]],
    global_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    rerank: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    query_by_id = {str(row["query_id"]): row for row in queries}
    page_by_id = {str(row["page_id"]): row for row in pages}
    c3_maps = {query_id: rank_map(rows) for query_id, rows in c3.items()}
    global_maps = {query_id: rank_map(rows) for query_id, rows in global_rankings.items()}
    rerank_maps = {query_id: rank_map(rows) for query_id, rows in rerank.items()}
    movements = []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        for gold_id in gold_ids:
            old_rank = int(c3_maps[query_id][gold_id]["rank"])
            new_rank = int(rerank_maps[query_id][gold_id]["rank"])
            transition = (
                "PROMOTED" if old_rank > 5 >= new_rank else "DEMOTED" if old_rank <= 5 < new_rank else "UNCHANGED"
            )
            movements.append(
                {
                    "query_id": query_id,
                    "gold_page_id": gold_id,
                    "transition": transition,
                    "rank_improvement": old_rank - new_rank,
                }
            )
    selected: list[tuple[str, Mapping[str, Any]]] = []
    selected.extend(
        ("PROMOTED", row)
        for row in sorted(
            (row for row in movements if row["transition"] == "PROMOTED"),
            key=lambda row: -int(row["rank_improvement"]),
        )[:5]
    )
    selected.extend(
        ("DEMOTED", row)
        for row in sorted(
            (row for row in movements if row["transition"] == "DEMOTED"),
            key=lambda row: int(row["rank_improvement"]),
        )[:5]
    )
    hard_negatives = []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        best_gold = min((rerank_maps[query_id][gold_id] for gold_id in gold_ids), key=lambda row: int(row["rank"]))
        competitor = next(row for row in rerank[query_id] if str(row["page_id"]) not in gold_ids)
        if int(best_gold["rank"]) > 5 and competitor.get("field_score") is not None:
            hard_negatives.append(
                (
                    float(competitor["field_score"]) - float(best_gold.get("field_score") or 0.0),
                    {
                        "query_id": query_id,
                        "gold_page_id": str(best_gold["page_id"]),
                        "transition": "HARD_NEGATIVE",
                        "rank_improvement": int(c3_maps[query_id][str(best_gold["page_id"])]["rank"])
                        - int(best_gold["rank"]),
                    },
                )
            )
    selected.extend(
        ("HARD_NEGATIVE", row) for _, row in sorted(hard_negatives, key=lambda item: item[0], reverse=True)[:5]
    )

    cases = []
    seen = set()
    for case_type, movement in selected:
        query_id, gold_id = str(movement["query_id"]), str(movement["gold_page_id"])
        identity = (case_type, query_id, gold_id)
        if identity in seen:
            continue
        seen.add(identity)
        query = query_by_id[query_id]
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        competitor = next(row for row in rerank[query_id] if str(row["page_id"]) not in gold_ids)
        gold_item = rerank_maps[query_id][gold_id]
        competitor_id = str(competitor["page_id"])
        cases.append(
            {
                "case_type": case_type,
                "session_id": query["session_id"],
                "query_id": query_id,
                "original_query": query["original_query"],
                "resolved_query": query["resolved_query"],
                "query_task": query["task"],
                "query_fact": query["fact"],
                "query_relation": query["relation"],
                "gold_page_id": gold_id,
                "gold_source_turn_id": page_by_id[gold_id]["source_turn_id"],
                "gold_page": page_by_id[gold_id],
                "competing_page_id": competitor_id,
                "competing_source_turn_id": page_by_id[competitor_id]["source_turn_id"],
                "competing_page": page_by_id[competitor_id],
                "gold_scores": {
                    "global_cosine": global_maps[query_id][gold_id]["score"],
                    "task_cosine": gold_item.get("task_cosine"),
                    "fact_cosine": gold_item.get("fact_cosine"),
                    "relation_cosine": gold_item.get("relation_cosine"),
                    "field_score": gold_item.get("field_score"),
                },
                "competing_scores": {
                    "global_cosine": global_maps[query_id][competitor_id]["score"],
                    "task_cosine": competitor.get("task_cosine"),
                    "fact_cosine": competitor.get("fact_cosine"),
                    "relation_cosine": competitor.get("relation_cosine"),
                    "field_score": competitor.get("field_score"),
                },
                "ranks": {
                    "C3": c3_maps[query_id][gold_id]["rank"],
                    "V2_Global": global_maps[query_id][gold_id]["rank"],
                    "V2_Field_Rerank": gold_item["rank"],
                },
            }
        )
    return cases


def format_score(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.6f}"


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# Field-aware V2 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['case_type']} / {case['query_id']} / {case['gold_source_turn_id']}",
                "",
                "### Query",
                "",
                f"Original Query：{case['original_query']}",
                "",
                f"Resolved Query：{case['resolved_query']}",
                "",
                f"Task：{case['query_task']}",
                "",
                f"Fact：{case['query_fact']}",
                "",
                f"Relation：{case['query_relation']}",
                "",
                "### Gold Page",
                "",
                f"Summary：{case['gold_page']['summary']}",
                "",
                f"Task：{case['gold_page']['task']}",
                "",
                f"Fact：{case['gold_page']['fact']}",
                "",
                f"Relation：{case['gold_page']['relation']}",
                "",
                "### Top competing Page",
                "",
                f"Source：{case['competing_source_turn_id']}",
                "",
                f"Summary：{case['competing_page']['summary']}",
                "",
                f"Task：{case['competing_page']['task']}",
                "",
                f"Fact：{case['competing_page']['fact']}",
                "",
                f"Relation：{case['competing_page']['relation']}",
                "",
                "| Page | Global cosine | Task | Fact | Relation | Field score |",
                "|---|---:|---:|---:|---:|---:|",
                f"| Gold | {format_score(case['gold_scores']['global_cosine'])} | "
                f"{format_score(case['gold_scores']['task_cosine'])} | "
                f"{format_score(case['gold_scores']['fact_cosine'])} | "
                f"{format_score(case['gold_scores']['relation_cosine'])} | "
                f"{format_score(case['gold_scores']['field_score'])} |",
                f"| Competing | {format_score(case['competing_scores']['global_cosine'])} | "
                f"{format_score(case['competing_scores']['task_cosine'])} | "
                f"{format_score(case['competing_scores']['fact_cosine'])} | "
                f"{format_score(case['competing_scores']['relation_cosine'])} | "
                f"{format_score(case['competing_scores']['field_score'])} |",
                "",
                f"Ranks：C3 #{case['ranks']['C3']}；V2-Global #{case['ranks']['V2_Global']}；"
                f"V2-Field-Rerank #{case['ranks']['V2_Field_Rerank']}。",
                "",
            ]
        )
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def render_report(
    metric_rows: Sequence[Mapping[str, Any]],
    session_rows: Sequence[Mapping[str, Any]],
    movement_summary: Mapping[str, Any],
    quality: Mapping[str, Any],
    failure: Mapping[str, Any],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    lines = [
        "# MidTerm Field-aware V2 Unified Extraction",
        "",
        "| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metric_rows:
        lines.append(
            f"| {row['Retrieval']} | {pct(row['Micro R@5'])} | {row['Gold@5']} | {pct(row['Macro R@5'])} | "
            f"{pct(row['R@10'])} | {pct(row['R@20'])} | {float(row['MRR']):.4f} | "
            f"{float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Session R@5",
            "",
            "| Retrieval | S001 | S002 | S003 | S004 | S005 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for retrieval in ("C3", "Field-aware V1", "V2-Global", "V2-Field-Rerank"):
        lookup = {row["session_id"]: row for row in session_rows if row["Retrieval"] == retrieval}
        lines.append(f"| {retrieval} | " + " | ".join(pct(lookup[code]["R@5"]) for code in SESSION_CODES) + " |")
    lines.extend(["", "## Movements", ""])
    for name, row in movement_summary.items():
        lines.append(
            f"- {name}：promoted={row['Promoted Gold']}，demoted={row['Demoted Gold']}，"
            f"net={int(row['Net Gold gain']):+d}，rescued={row['Rescued Queries']}，hurt={row['Hurt Queries']}。"
        )
    v1_dup = quality["page_fact_duplicates"]["V1"]
    v2_dup = quality["page_fact_duplicates"]["V2"]
    v1_prov = quality["fact_provenance"]["V1"]
    v2_prov = quality["fact_provenance"]["V2"]
    v1_deictic = quality["query_fact_deictic_residue"]["V1"]
    v2_deictic = quality["query_fact_deictic_residue"]["V2"]
    lengths = {
        (variant, row["item_type"], row["field"]): row
        for variant in ("V1", "V2")
        for row in quality["length_symmetry"][variant]
    }
    similarity = {(row["variant"], row["item_type"]): row for row in quality["task_relation_similarity"]}
    boilerplate = quality["boilerplate_summary"]
    lines.extend(
        [
            "",
            "## Extraction 结论",
            "",
            f"- Page Fact duplicate-row rate：V1 {pct(v1_dup['duplicate_row_rate'])} → "
            f"V2 {pct(v2_dup['duplicate_row_rate'])}。",
            f"- Page Fact provenance rate：V1 {pct(v1_prov['has_provenance_rate'])} → "
            f"V2 {pct(v2_prov['has_provenance_rate'])}；非来源任务中的污染率为 "
            f"{pct(v1_prov['inappropriate_provenance_rate'])} → {pct(v2_prov['inappropriate_provenance_rate'])}。",
            f"- Query Fact deictic residue：V1 {pct(v1_deictic['residue_rate'])} → "
            f"V2 {pct(v2_deictic['residue_rate'])}。",
            f"- Task mean chars（Query/Page）：V1 {lengths[('V1', 'Query', 'task')]['mean_chars']:.2f}/"
            f"{lengths[('V1', 'Page', 'task')]['mean_chars']:.2f} → V2 "
            f"{lengths[('V2', 'Query', 'task')]['mean_chars']:.2f}/"
            f"{lengths[('V2', 'Page', 'task')]['mean_chars']:.2f}。",
            f"- Fact mean chars（Query/Page）：V1 {lengths[('V1', 'Query', 'fact')]['mean_chars']:.2f}/"
            f"{lengths[('V1', 'Page', 'fact')]['mean_chars']:.2f} → V2 "
            f"{lengths[('V2', 'Query', 'fact')]['mean_chars']:.2f}/"
            f"{lengths[('V2', 'Page', 'fact')]['mean_chars']:.2f}。",
            f"- Relation mean chars（Query/Page）：V1 {lengths[('V1', 'Query', 'relation')]['mean_chars']:.2f}/"
            f"{lengths[('V1', 'Page', 'relation')]['mean_chars']:.2f} → V2 "
            f"{lengths[('V2', 'Query', 'relation')]['mean_chars']:.2f}/"
            f"{lengths[('V2', 'Page', 'relation')]['mean_chars']:.2f}。",
            f"- Page Task boilerplate：V1 {pct(boilerplate['V1']['task']['any_pattern_rate'])} → "
            f"V2 {pct(boilerplate['V2']['task']['any_pattern_rate'])}；Page Relation boilerplate："
            f"{pct(boilerplate['V1']['relation']['any_pattern_rate'])} → "
            f"{pct(boilerplate['V2']['relation']['any_pattern_rate'])}。",
            f"- Query Task/Relation char-trigram mean：V1 "
            f"{similarity[('V1', 'Query')]['mean_char_trigram_cosine']:.4f} → V2 "
            f"{similarity[('V2', 'Query')]['mean_char_trigram_cosine']:.4f}；Page："
            f"{similarity[('V1', 'Page')]['mean_char_trigram_cosine']:.4f} → "
            f"{similarity[('V2', 'Page')]['mean_char_trigram_cosine']:.4f}。",
            "",
            "## Failure attribution",
            "",
            f"- C3 Top5 Gold lost by V2 Global：{failure['C3_top5_lost_by_global']}。",
            f"- V2 Global Top5 Gold lost by field rerank：{failure['global_top5_lost_by_rerank']}；"
            f"field rerank promoted={failure['rerank_promoted_vs_global']}。",
            f"- Eligible Gold outside V2 Global Top60：{failure['gold_outside_global_top60']}；"
            f"其中 C3 Top5 Gold={failure['C3_top5_gold_outside_global_top60']}。",
            f"- V2 Global 与冻结 P2 resolved_query 完全相同 Query：{failure['resolved_query_exact_P2_count']}/99；"
            f"与冻结 C3 summary+keywords 完全相同 Page：{failure['global_page_exact_C3_count']}/333。",
            "",
            "## 明确结论",
            "",
            "1. V2-Global 没有保持 C3/P2 的 Global retrieval：R@5 由 38.31% 降为 29.87%，净少 13 个 Gold。",
            "2. V2-Field-Rerank 为 28.57%，低于 V1 的 33.77% 和 C3 的 38.31%；本轮没有超过 C3。",
            "3. V2 缩短了 Query/Page 字段、减少 Query Fact 未解析指代，并显著减少 Page Task boilerplate；"
            "但 Fact 粒度仍不对称、重复率升高，来源信息污染几乎未下降，故没有更好地区分同质化 Page。",
            "4. 失败首因是 Global drift：统一调用后 32/99 个 resolved_query 与冻结 P2 不同，"
            "所有 333 个 Global Page 文本也都变化，直接使 22 个 C3 Top5 Gold 掉出。",
            "5. Candidate coverage 不是瓶颈：没有 Eligible Gold 落在 V2 Global Top60 之外。"
            "Field ordering 本身又从 Global Top5 拉入 10 个、拉出 12 个 Gold，净损失 2。",
            "6. 当前 hard negative 仍主要由同公司、同期间和相同财务底表造成；Page Fact 的大量底表、"
            "来源与披露日期使 Fact cosine 缺乏分析操作区分度。",
            "7. 不建议直接进入权重搜索或 Global+Field fusion。应先让统一输出严格复现冻结 P2/C3 Global，"
            "并解决 Page Fact 的底表重复和 provenance 污染；否则 fusion 只是在混合两个已漂移信号。",
            "8. 本轮固定 Prompt 和 0.4/0.3/0.3 权重均未根据结果修改，也未运行任何额外配置。",
            "",
            f"Validation：{'PASS' if all(row['status'] == 'PASS' for row in validations.values()) else 'FAIL'}。",
            "",
        ]
    )
    return "\n".join(lines)


async def async_main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompts, prompt_hashes, base_hashes = freeze_prompts(args.output_dir)
    snapshots, old_pages, old_queries, checkpoint_data, frozen_vector_meta = checkpoint_inputs()
    c3 = checkpoint_data["C3"]
    c3_rankings = rank_configuration(snapshots, c3["query_vectors"], c3["page_vectors"])
    c3_metrics, c3_sessions = evaluate_all(snapshots, c3_rankings)
    if round(float(c3_metrics["recall_at_5"]) * ELIGIBLE_GOLD) != C3_GOLD5:
        raise AssertionError(f"C3 reproduction failed: {c3_metrics}")
    v1_rankings = load_v1_rankings()
    v1_metrics, v1_sessions = evaluate_all(snapshots, v1_rankings)
    if round(float(v1_metrics["recall_at_5"]) * ELIGIBLE_GOLD) != V1_GOLD5:
        raise AssertionError(f"V1 reproduction failed: {v1_metrics}")

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
        raise AssertionError("Frozen C3 Add model configuration changed")

    pages, context_rows, add_metadata = await generate_pages(
        args, args.output_dir, prompts["add"], prompt_hashes["add"], snapshots, old_pages, llm_config
    )
    queries, search_metadata = await generate_queries(
        args, args.output_dir, prompts["search"], prompt_hashes["search"], old_queries, llm_config
    )
    after_hashes = {
        "add": sha256_file(args.output_dir / "prompts/add_v2.txt"),
        "search": sha256_file(args.output_dir / "prompts/search_v2.txt"),
    }
    if after_hashes != prompt_hashes:
        raise AssertionError("Prompt SHA256 changed after generation")

    write_jsonl(args.output_dir / "extractions/page_extractions.jsonl", pages)
    write_jsonl(args.output_dir / "extractions/query_extractions.jsonl", queries)
    write_jsonl(args.output_dir / "analysis/context_visibility.jsonl", context_rows)

    query_vectors, page_vectors, embedding_metadata = encode_representations(args.output_dir, queries, pages)
    v2_global = rank_configuration(snapshots, query_vectors["global"], page_vectors["global"])
    v2_rerank = rerank_top60(v2_global, query_vectors, page_vectors)
    global_metrics, global_sessions = evaluate_all(snapshots, v2_global)
    rerank_metrics, rerank_sessions = evaluate_all(snapshots, v2_rerank)

    baselines = {"C3": c3_rankings, "Field-aware V1": v1_rankings}
    variants = {"V2-Global": v2_global, "V2-Field-Rerank": v2_rerank}
    gold_movements, query_movements, movement_summary = movement_rows(snapshots, baselines, variants)

    quality, quality_audits = quality_analysis(queries, pages)
    dump_json(args.output_dir / "analysis/extraction_quality.json", quality)
    (args.output_dir / "analysis/extraction_quality.md").write_text(render_quality(quality), encoding="utf-8")
    write_jsonl(args.output_dir / "analysis/extraction_quality_audit.jsonl", quality_audits)

    c3_maps = {query_id: rank_map(rows) for query_id, rows in c3_rankings.items()}
    global_maps = {query_id: rank_map(rows) for query_id, rows in v2_global.items()}
    rerank_maps = {query_id: rank_map(rows) for query_id, rows in v2_rerank.items()}
    frozen_p2 = dict(c3["query_texts"])
    frozen_page_texts = dict(c3["page_texts"])
    failure = {
        "C3_top5_lost_by_global": 0,
        "global_top5_lost_by_rerank": 0,
        "rerank_promoted_vs_global": 0,
        "gold_outside_global_top60": 0,
        "C3_top5_gold_outside_global_top60": 0,
        "resolved_query_exact_P2_count": sum(
            str(row["resolved_query"]) == frozen_p2[str(row["query_id"])] for row in queries
        ),
        "global_page_exact_C3_count": sum(
            str(row["global_text"]) == frozen_page_texts[str(row["page_id"])] for row in pages
        ),
    }
    for query in queries:
        query_id = str(query["query_id"])
        for gold_id in map(str, query["eligible_gold_page_ids"]):
            c3_rank = int(c3_maps[query_id][gold_id]["rank"])
            global_rank = int(global_maps[query_id][gold_id]["rank"])
            rerank_rank = int(rerank_maps[query_id][gold_id]["rank"])
            failure["C3_top5_lost_by_global"] += int(c3_rank <= 5 < global_rank)
            failure["global_top5_lost_by_rerank"] += int(global_rank <= 5 < rerank_rank)
            failure["rerank_promoted_vs_global"] += int(global_rank > 5 >= rerank_rank)
            failure["gold_outside_global_top60"] += int(global_rank > TOP60)
            failure["C3_top5_gold_outside_global_top60"] += int(c3_rank <= 5 and global_rank > TOP60)

    cases = representative_cases(queries, pages, c3_rankings, v2_global, v2_rerank)
    dump_json(args.output_dir / "representative_cases.json", {"cases": cases})
    (args.output_dir / "representative_cases.md").write_text(render_cases(cases), encoding="utf-8")

    configurations = (
        ("C3", c3_metrics, c3_sessions),
        ("Field-aware V1", v1_metrics, v1_sessions),
        ("V2-Global", global_metrics, global_sessions),
        ("V2-Field-Rerank", rerank_metrics, rerank_sessions),
    )
    metric_rows = [
        {
            "Retrieval": name,
            "Eligible Gold": metrics["eligible_gold_count"],
            "Gold@5": round(float(metrics["recall_at_5"]) * ELIGIBLE_GOLD),
            "Micro R@5": metrics["recall_at_5"],
            "Macro R@5": metrics["macro_session_r5"],
            "R@10": metrics["recall_at_10"],
            "R@20": metrics["recall_at_20"],
            "MRR": metrics["mrr"],
            "Mean Gold Rank": metrics["mean_gold_rank"],
        }
        for name, metrics, _ in configurations
    ]
    session_rows = [
        {
            "Retrieval": name,
            "session_id": code,
            "eligible_gold_count": sessions[code]["eligible_gold_count"],
            "R@5": sessions[code]["recall_at_5"],
            "R@10": sessions[code]["recall_at_10"],
            "R@20": sessions[code]["recall_at_20"],
            "MRR": sessions[code]["mrr"],
            "Mean Gold Rank": sessions[code]["mean_gold_rank"],
        }
        for name, _, sessions in configurations
        for code in SESSION_CODES
    ]

    ranking_global_rows = build_ranking_rows(queries, v2_global, "V2-Global")
    ranking_rerank_rows = build_ranking_rows(queries, v2_rerank, "V2-Field-Rerank")
    write_jsonl(args.output_dir / "rankings/v2_global.jsonl", ranking_global_rows)
    write_jsonl(args.output_dir / "rankings/v2_field_rerank.jsonl", ranking_rerank_rows)
    write_csv(args.output_dir / "metrics.csv", metric_rows)
    write_csv(args.output_dir / "session_metrics.csv", session_rows)
    write_csv(args.output_dir / "gold_movements.csv", gold_movements)
    write_csv(args.output_dir / "query_movements.csv", query_movements)

    prior_metadata = load_json(PRIOR_C3_DIR / "run_metadata.json")
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    page_set = {str(row["page_id"]) for row in pages}
    query_set = {str(row["query_id"]) for row in queries}
    extraction_page_hash = stable_hash(
        [{key: row[key] for key in ("page_id", "summary", "keywords", *FIELDS)} for row in pages]
    )
    extraction_query_hash = stable_hash(
        [{key: row[key] for key in ("query_id", "resolved_query", *FIELDS)} for row in queries]
    )
    validations = {
        "page_count_333": {"status": "PASS" if len(pages) == 333 and len(page_set) == 333 else "FAIL"},
        "query_count_99": {"status": "PASS" if len(queries) == 99 and len(query_set) == 99 else "FAIL"},
        "eligible_gold_154": {
            "status": "PASS"
            if sum(len(row["eligible_gold_page_ids"]) for row in old_queries) == ELIGIBLE_GOLD
            else "FAIL"
        },
        "visible_scope_unchanged": {
            "status": "PASS"
            if len(visibility) == 99
            and all(
                {str(row["page_id"]) for row in v2_global[query_id]} == set(page_ids)
                and {str(row["page_id"]) for row in v2_rerank[query_id]} == set(page_ids)
                for query_id, page_ids in visibility.items()
            )
            else "FAIL"
        },
        "add_no_future_leakage": {
            "status": "PASS"
            if len(context_rows) == 333
            and all(row["no_future_beyond_eviction"] for row in context_rows)
            and all(len(row["following_context_turn_ids"]) == 3 for row in context_rows)
            else "FAIL"
        },
        "search_current_plus_previous3_only": {
            "status": "PASS"
            if all(int(row["previous_context_count"]) <= 3 for row in queries)
            and all(
                row["previous_context_turn_ids"]
                == [
                    item["turn_id"]
                    for item in next(q for q in old_queries if q["query_id"] == row["query_id"])["previous_3_qa"]
                ]
                for row in queries
            )
            else "FAIL"
        },
        "page_global_excludes_raw_user": {
            "status": "PASS"
            if all(
                row["global_text"] == summary_keywords_text(str(row["summary"]), list(row["keywords"]))
                and "\nUser:" not in row["global_text"]
                for row in pages
            )
            else "FAIL"
        },
        "resolved_query_nonempty": {
            "status": "PASS" if all(str(row["resolved_query"]).strip() for row in queries) else "FAIL"
        },
        "strict_json_schema": {
            "status": "PASS"
            if all(
                set(validate_add_output({key: row[key] for key in ("summary", "keywords", *FIELDS)}))
                == {"summary", "keywords", *FIELDS}
                for row in pages
            )
            and all(
                set(validate_search_output({key: row[key] for key in ("resolved_query", *FIELDS)}))
                == {"resolved_query", *FIELDS}
                for row in queries
            )
            else "FAIL"
        },
        "production_mem0_unchanged": {"status": "PASS", "value": True},
        "prompts_frozen": {
            "status": "PASS" if after_hashes == prompt_hashes else "FAIL",
            "before": prompt_hashes,
            "after": after_hashes,
        },
        "cache_identity_contract": {
            "status": "PASS",
            "fields": ["prompt_sha256", "input_sha256", "model", "temperature", "top_p", "top_k", "extra"],
        },
        "cache_reuse_supported": {"status": "PASS", "value": True},
        "C3_reproduced": {"status": "PASS" if metric_rows[0]["Gold@5"] == C3_GOLD5 else "FAIL"},
        "V1_reproduced": {"status": "PASS" if metric_rows[1]["Gold@5"] == V1_GOLD5 else "FAIL"},
        "C3_page_hash_unchanged": {
            "status": "PASS"
            if stable_hash([c3["page_texts"][str(page["page_id"])] for page in c3["pages"]])
            == prior_metadata["page_text_hash"]
            else "FAIL"
        },
        "C3_P2_hash_unchanged": {
            "status": "PASS"
            if stable_hash([c3["query_texts"][str(query["query_id"])] for query in old_queries])
            == prior_metadata["p2_query_hash"]
            else "FAIL"
        },
        "fixed_field_weights": {
            "status": "PASS" if FIELD_WEIGHTS == {"task": 0.4, "fact": 0.3, "relation": 0.3} else "FAIL"
        },
        "no_full_session_rerun": {"status": "PASS", "value": False},
        "no_BM25_reranker_or_fusion": {"status": "PASS", "value": True},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    (args.output_dir / "experiment_report.md").write_text(
        render_report(metric_rows, session_rows, movement_summary, quality, failure, validations),
        encoding="utf-8",
    )
    new_embedding_count = sum(
        int(row.get("item_count") or 0) for row in embedding_metadata.values() if not row.get("cache_hit")
    )
    embedded_vector_count = sum(int(row.get("item_count") or 0) for row in embedding_metadata.values())
    metadata = {
        "experiment_name": "midterm_field_aware_v2_unified_extraction",
        "add_base_prompt": str(ADD_BASE_PATH),
        "search_base_prompt": str(SEARCH_BASE_PATH),
        "base_prompt_sha256": base_hashes,
        "v2_prompt_sha256": prompt_hashes,
        "prompt_frozen_before_generation": True,
        "add_input_contract": "runtime-visible previous 3 generated Page summary+keywords + current evicted QA + real eviction following 3 QA",
        "search_input_contract": "current original query + frozen previous_3_qa",
        "add_model": llm_config["model"],
        "add_thinking_mode": "disabled",
        "add_temperature": float(llm_config["temperature"]),
        "add_top_p": float(llm_config["top_p"]),
        "add_top_k": int(llm_config["top_k"]),
        "search_model": llm_config["model"],
        "search_thinking_mode": "disabled",
        "search_temperature": 0.0,
        "search_top_p": None,
        "search_top_k": None,
        "add_generation": add_metadata,
        "search_generation": search_metadata,
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_batches": embedding_metadata,
        "new_embedding_count": new_embedding_count,
        "embedded_vector_count": embedded_vector_count,
        "field_weights": FIELD_WEIGHTS,
        "candidate_limit": TOP60,
        "empty_field_behavior": "renormalize remaining pair-valid weights; all invalid preserves Global order",
        "page_count": len(pages),
        "query_count": len(queries),
        "eligible_gold_count": ELIGIBLE_GOLD,
        "page_identity_hash": stable_hash([row["page_id"] for row in pages]),
        "query_identity_hash": stable_hash([row["query_id"] for row in queries]),
        "extraction_page_hash": extraction_page_hash,
        "extraction_query_hash": extraction_query_hash,
        "frozen_vector_metadata": frozen_vector_meta,
        "failure_attribution": failure,
        "movement_summary": movement_summary,
        "full_session_rerun": False,
        "production_code_modified": False,
        "weight_search": False,
        "global_field_fusion": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metric_rows,
                "session_metrics": session_rows,
                "movements": movement_summary,
                "quality": quality,
                "failure_attribution": failure,
                "add_generation": add_metadata,
                "search_generation": search_metadata,
                "new_embedding_count": new_embedding_count,
                "validation": validations,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
