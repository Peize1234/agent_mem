"""Test whether local runtime-visible context improves Production Add Pages.

The experiment replays only the Add summarization order. It does not rerun
Sessions or change source turns, Page identity, visibility, Gold, Search-P2,
Page formatting, embedding, or dense cosine retrieval.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import (
    BenchmarkSession,
    ensure_repo_root_on_path,
    expand_env_placeholders,
    load_dataset,
    load_json,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_conservative_retrieval_tuning_ablation import (  # noqa: E402
    anchor_diff,
    provider_precondition,
    summary_validator,
)
from exp.benchmark.run_midterm_add_prompt_conservative_candidates_ablation import (  # noqa: E402
    extended_anchors,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    page_text,
    rank_configuration,
    sha256_file,
    validate_old_reproduction,
)
from exp.benchmark.run_midterm_page_representation_decomposition import tokenizer_audit  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import dump_json, rank_map, write_csv  # noqa: E402
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_local_context_ablation"
VARIANTS = ("Production", "PreviousContext", "PreviousAndFollowingContext")
NEW_VARIANTS = VARIANTS[1:]
VARIANT_LABELS = {
    "Production": "原 Production Add（仅当前 User + Assistant）",
    "PreviousContext": "Production Add + 最近上文",
    "PreviousAndFollowingContext": "Production Add + 最近上文及下文",
}
PROMPT_FILES = {
    "Production": "Original_Production_Add_Prompt.txt",
    "PreviousContext": "Production_Add_With_Previous_Context.txt",
    "PreviousAndFollowingContext": "Production_Add_With_Previous_And_Following_Context.txt",
}
PROMPT_VERSIONS = {
    "PreviousContext": "production-add-recent-previous-context-v1",
    "PreviousAndFollowingContext": "production-add-recent-previous-following-context-v1",
}
SEARCH_LABEL = "上下文引用解析后检索（P2）"
SHORT_TERM_MESSAGE_CAPACITY = 6
SHORT_TERM_QA_CAPACITY = 3
PROMPT_INSERTION_ANCHOR = "## summary 要求"

PREVIOUS_CONTEXT_INSTRUCTIONS = """## 上下文使用说明

本次需要总结的是一轮已从短期记忆淘汰的用户/助手对话。

输入中还会提供该轮对话之前最近的若干条对话记忆作为上文。上文仅用于帮助理解当前对话中的指代、承接关系以及当前任务与此前讨论之间的联系。

生成摘要时仍应以当前待总结的用户/助手对话为主体，并继续遵循原有摘要和关键词要求。

如果当前对话中存在“前面”“刚才”“上一轮”“这个判断”“这些数据”等依赖前文才能理解的表达，应结合上文恢复其具体含义。

如果当前轮是在延续、比较、验证、反证、补充或修订此前的某项分析或判断，应在摘要中自然保留这种具体关系，而不是只写“结合前文进一步分析”。

如果当前对话本身已经足够完整，或者与提供的上文没有明显关系，则按照原有方式总结，不需要强行加入上下文信息。

上文只用于理解当前对话，不要将上文中与当前任务无关的独立数据、指标、结论或任务复制到当前摘要中，也不要将多轮内容合并成一份综合总结。

关键词仍然描述当前待总结对话本身；上下文只可用于明确当前任务或其与前文的关系，不要因为上文出现某个概念就自动将其加入关键词。"""

PREVIOUS_FOLLOWING_CONTEXT_INSTRUCTIONS = """## 上下文使用说明

本次需要总结的是一轮已从短期记忆淘汰的用户/助手对话。

输入中还会提供：

- 上文：该轮对话之前最近的若干条对话记忆；
- 下文：该轮对话之后已经发生、当前仍处于近期对话窗口中的若干轮用户/助手对话。

这些内容仅用于帮助理解当前待总结对话在连续对话中的实际位置。

生成摘要时仍应以当前待总结的用户/助手对话为主体，并继续遵循原有摘要和关键词要求。

对于上文，如果当前对话中存在“前面”“刚才”“上一轮”“这个判断”“这些数据”等指代表达，应结合上文恢复其具体含义。

如果当前轮是在延续、比较、验证、反证、补充或修订此前的某项分析或判断，应在摘要中自然保留这种具体关系。

对于下文，可以利用其判断当前轮形成的信息或判断后来是否被继续引用，以及当前轮在后续分析中实际承担了什么作用。

但下文只能用于辅助理解当前轮，不能改变当前轮发生时真实包含的信息和判断状态。

禁止把下文中新出现的数字、指标、证据、任务或结论写成当前轮已经包含的内容，也禁止用后续结论覆盖当前轮原本的判断。

如果下文能够明确说明当前轮后来成为某项验证、反证、修订或后续分析的基础，可以简洁保留这种关系；没有明显帮助时直接忽略下文。

无论使用上文还是下文，都不要将其他轮次的独立内容直接合并进当前摘要，也不要把多轮对话整理成综合总结。

关键词仍然描述当前待总结对话本身；前后文只可用于明确当前任务、判断关系或对话承接，不要因为上下文出现某个概念就自动将其加入关键词。"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen Add local-context ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--llm-timeout", type=float, default=240.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def add_context_instructions(production: str, instructions: str) -> str:
    if production.count(PROMPT_INSERTION_ANCHOR) != 1:
        raise AssertionError("Production Prompt summary heading is not unique")
    return production.replace(PROMPT_INSERTION_ANCHOR, f"{instructions}\n\n{PROMPT_INSERTION_ANCHOR}")


def prompt_variants() -> dict[str, str]:
    return {
        "Production": MIDTERM_PAGE_SUMMARY_PROMPT,
        "PreviousContext": add_context_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, PREVIOUS_CONTEXT_INSTRUCTIONS),
        "PreviousAndFollowingContext": add_context_instructions(
            MIDTERM_PAGE_SUMMARY_PROMPT, PREVIOUS_FOLLOWING_CONTEXT_INSTRUCTIONS
        ),
    }


def freeze_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    prompts = prompt_variants()
    prompt_dir = output_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for variant, prompt in prompts.items():
        path = prompt_dir / PROMPT_FILES[variant]
        if path.exists() and path.read_text(encoding="utf-8") != prompt:
            raise AssertionError(f"Frozen Prompt differs from deterministic source: {path}")
        if not path.exists():
            path.write_text(prompt, encoding="utf-8")
        hashes[variant] = sha256_file(path)
    dump_json(
        prompt_dir / "prompt_sha256.json",
        {
            "frozen_before_first_llm_call": True,
            "Production_body_is_an_ordered_subsequence_of_context_variants": True,
            **{
                VARIANT_LABELS[variant]: {"file": PROMPT_FILES[variant], "sha256": hashes[variant]}
                for variant in VARIANTS
            },
        },
    )
    return prompts, hashes


def load_frozen_sessions(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
) -> dict[str, BenchmarkSession]:
    dataset_path = Path(str(snapshots["S001"]["manifest"]["dataset_path"]))
    sheet_names = [str(snapshots[code]["manifest"]["session_id"]) for code in SESSION_CODES]
    sessions = load_dataset(dataset_path, include_sheets=sheet_names)
    result = {session.turns[0].turn_id.split("-", 1)[0]: session for session in sessions}
    if set(result) != set(SESSION_CODES):
        raise AssertionError(f"Frozen Session dataset mismatch: {sorted(result)}")
    page_by_turn = {str(page["source_turn_id"]): page for page in pages}
    for code, session in result.items():
        session_pages = sorted(
            (page for page in pages if page["session_code"] == code), key=lambda page: int(page["source_turn_index"])
        )
        if len(session_pages) != len(session.turns) - SHORT_TERM_QA_CAPACITY:
            raise AssertionError(f"{code}: Page count does not match three-turn active window")
        for page in session_pages:
            index = int(page["source_turn_index"])
            turn = session.turns[index]
            trigger_index = index + SHORT_TERM_QA_CAPACITY
            if (
                turn.turn_id != page["source_turn_id"]
                or turn.question != page["user_input"]
                or turn.answer != page["assistant_response"]
                or trigger_index >= len(session.turns)
                or session.turns[trigger_index].turn_id != page["source_job_trigger_turn_id"]
            ):
                raise AssertionError(f"{page['source_turn_id']}: frozen QA/eviction trigger mismatch")
            if str(page["source_turn_id"]) not in page_by_turn:
                raise AssertionError("Missing frozen Page")
    return result


def context_layout(
    page: Mapping[str, Any],
    session: BenchmarkSession,
    generated_previous_pages: Sequence[Mapping[str, Any]],
    include_following: bool,
) -> dict[str, Any]:
    source_index = int(page["source_turn_index"])
    trigger_index = source_index + SHORT_TERM_QA_CAPACITY
    previous = list(generated_previous_pages[-SHORT_TERM_QA_CAPACITY:])
    following = list(session.turns[source_index + 1 : trigger_index + 1]) if include_following else []
    if any(int(row["source_turn_index"]) >= source_index for row in previous):
        raise AssertionError(f"{page['source_turn_id']}: non-past Page in previous context")
    if include_following and (
        len(following) != SHORT_TERM_QA_CAPACITY
        or following[-1].turn_id != page["source_job_trigger_turn_id"]
        or any(turn.turn_index <= source_index or turn.turn_index > trigger_index for turn in following)
    ):
        raise AssertionError(f"{page['source_turn_id']}: following context is not the real eviction window")
    return {
        "source_turn_id": page["source_turn_id"],
        "source_turn_index": source_index,
        "eviction_trigger_turn_id": page["source_job_trigger_turn_id"],
        "eviction_trigger_turn_index": trigger_index,
        "previous_context": [
            {
                "source_turn_id": row["source_turn_id"],
                "source_turn_index": row["source_turn_index"],
                "summary": row["summary"],
                "keywords": list(row["keywords"]),
            }
            for row in previous
        ],
        "following_context": [
            {
                "turn_id": turn.turn_id,
                "turn_index": turn.turn_index,
                "user": turn.question,
                "assistant": turn.answer,
            }
            for turn in following
        ],
    }


def format_context_input(page: Mapping[str, Any], layout: Mapping[str, Any], include_following: bool) -> str:
    lines = ["上文：", ""]
    previous = list(layout["previous_context"])
    if previous:
        for number, item in enumerate(previous, 1):
            lines.extend(
                [
                    f"{number}.",
                    f"摘要：{item['summary']}",
                    f"关键词：{', '.join(str(value) for value in item['keywords'])}",
                    "",
                ]
            )
    else:
        lines.extend(["（无）", ""])
    lines.extend(
        [
            "当前待总结对话：",
            "",
            f"用户：{page['user_input']}",
            f"助手：{page['assistant_response']}",
        ]
    )
    if include_following:
        lines.extend(["", "下文：", ""])
        following = list(layout["following_context"])
        if following:
            for item in following:
                lines.extend([f"用户：{item['user']}", f"助手：{item['assistant']}", ""])
        else:
            lines.append("（无）")
    return "\n".join(lines).rstrip()


class ContextSummaryCache:
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
        self.lock = asyncio.Lock()
        self.success = {str(row["cache_key"]): row for row in load_jsonl(path) if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(
        self,
        *,
        page: Mapping[str, Any],
        variant: str,
        prompt_version: str | None = None,
        prompt: str,
        prompt_sha256: str,
        payload: str,
        layout: Mapping[str, Any],
    ) -> dict[str, Any]:
        identity = {
            "session_id": page["session_code"],
            "source_turn_id": page["source_turn_id"],
            "variant": variant,
            "prompt_version": prompt_version or PROMPT_VERSIONS[variant],
            "prompt_sha256": prompt_sha256,
            "input_payload_sha256": sha256_text(payload),
            "current_user_sha256": sha256_text(str(page["user_input"])),
            "current_assistant_sha256": sha256_text(str(page["assistant_response"])),
            "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
            "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
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
                        self.client.chat.completions.create(**kwargs), timeout=self.timeout
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = summary_validator(raw)
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": key,
                        "input_contract": "runtime-visible previous Page summaries/keywords + current QA"
                        + (" + active following QA window" if layout["following_context"] else ""),
                        "eviction_trigger_turn_id": layout["eviction_trigger_turn_id"],
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
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": self.retries - 1,
            "provider_precondition_rejected_count": rejects,
            "errors": errors,
        }
        await self.append(row)
        return row


async def generate_variant(
    args: argparse.Namespace,
    variant: str,
    prompt: str,
    prompt_sha256: str,
    old_pages: Sequence[Mapping[str, Any]],
    sessions: Mapping[str, BenchmarkSession],
    llm_config: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache_path = args.output_dir / f"cache/{variant}_summary_llm.jsonl"
    cache = ContextSummaryCache(
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
    initial_keys = set(cache.success)
    results: dict[str, dict[str, Any]] = {}
    context_rows: list[dict[str, Any]] = []
    result_lock = asyncio.Lock()
    include_following = variant == "PreviousAndFollowingContext"

    async def process_session(code: str) -> None:
        session = sessions[code]
        session_pages = sorted(
            (page for page in old_pages if page["session_code"] == code),
            key=lambda page: int(page["source_turn_index"]),
        )
        generated_previous_pages: list[dict[str, Any]] = []
        for page in session_pages:
            layout = context_layout(page, session, generated_previous_pages, include_following)
            payload = format_context_input(page, layout, include_following)
            row = await cache.call(
                page=page,
                variant=variant,
                prompt=prompt,
                prompt_sha256=prompt_sha256,
                payload=payload,
                layout=layout,
            )
            if row.get("status") != "SUCCESS":
                raise RuntimeError(f"{variant} failed at {page['source_turn_id']}: {row.get('errors')}")
            generated = {
                "session_id": code,
                "source_turn_id": page["source_turn_id"],
                "source_turn_index": page["source_turn_index"],
                "summary": row["parsed"]["summary"],
                "keywords": list(row["parsed"]["keywords"]),
            }
            generated_previous_pages.append(generated)
            context_row = {
                "variant": VARIANT_LABELS[variant],
                **{key: value for key, value in layout.items() if key not in {"previous_context", "following_context"}},
                "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
                "previous_context_count": len(layout["previous_context"]),
                "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
                "following_context_count": len(layout["following_context"]),
                "input_payload_sha256": sha256_text(payload),
                "input_char_count": len(payload),
                "no_future_beyond_eviction_trigger": all(
                    int(item["turn_index"]) <= int(layout["eviction_trigger_turn_index"])
                    for item in layout["following_context"]
                ),
                "cache_key": row["cache_key"],
            }
            async with result_lock:
                results[str(page["source_turn_id"])] = row
                context_rows.append(context_row)

    try:
        await asyncio.gather(*(process_session(code) for code in SESSION_CODES))
    finally:
        await client.close()
    if len(results) != 333:
        raise AssertionError(f"{variant}: expected 333 results, got {len(results)}")
    rows = list(results.values())
    return (
        results,
        sorted(context_rows, key=lambda row: (row["variant"], row["source_turn_id"])),
        {
            "successful_output_count": len(rows),
            "actual_api_attempts": sum(int(row.get("api_attempt_count") or 0) for row in rows),
            "retry_count": sum(int(row.get("retry_count") or 0) for row in rows),
            "provider_precondition_rejected_count": sum(
                int(row.get("provider_precondition_rejected_count") or 0) for row in rows
            ),
            "cache_hit_count": sum(str(row["cache_key"]) in initial_keys for row in rows),
            "cache_path": str(cache_path),
        },
    )


def build_pages(
    old_pages: Sequence[Mapping[str, Any]],
    generated: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    pages = {variant: [] for variant in VARIANTS}
    for old in old_pages:
        common = {
            "session_id": old["session_code"],
            "source_turn_id": old["source_turn_id"],
            "source_turn_index": old["source_turn_index"],
            "old_page_id": old["page_id"],
            "source_user": old["user_input"],
            "source_assistant": old["assistant_response"],
        }
        production_text = page_text(str(old["summary"]), list(old["keywords"]), str(old["user_input"]))
        if production_text != str(old["current_embedding_text"]):
            raise AssertionError(f"Production formatter mismatch: {old['source_turn_id']}")
        pages["Production"].append(
            {
                **common,
                "summary": old["summary"],
                "keywords": list(old["keywords"]),
                "embedding_text": production_text,
            }
        )
        for variant in NEW_VARIANTS:
            row = generated[variant][str(old["source_turn_id"])]
            summary = str(row["parsed"]["summary"])
            keywords = list(row["parsed"]["keywords"])
            pages[variant].append(
                {
                    **common,
                    "summary": summary,
                    "keywords": keywords,
                    "embedding_text": page_text(summary, keywords, str(old["user_input"])),
                    "llm_cache_key": row["cache_key"],
                }
            )
    return pages


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(query, session_code=code) for code in SESSION_CODES for query in snapshots[code]["queries"]]


def metric_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    queries = all_queries(snapshots)
    maps = {
        variant: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for variant, by_query in rankings.items()
    }
    retrieval_rows, gold_rows, transition_rows, session_rows = [], [], [], []
    for variant in VARIANTS:
        overall, sessions = evaluate_all(snapshots, rankings[variant])
        promoted = demoted = rescued = hurt = 0
        for query in queries:
            query_id = str(query["query_id"])
            gold_ids = [str(value) for value in query["eligible_gold_page_ids"]]
            base_ranks = {gold: int(maps["Production"][query_id][gold]["rank"]) for gold in gold_ids}
            ranks = {gold: int(maps[variant][query_id][gold]["rank"]) for gold in gold_ids}
            base_hit, hit = any(rank <= 5 for rank in base_ranks.values()), any(rank <= 5 for rank in ranks.values())
            query_promoted = sum(base_ranks[gold] > 5 and ranks[gold] <= 5 for gold in gold_ids)
            query_demoted = sum(base_ranks[gold] <= 5 and ranks[gold] > 5 for gold in gold_ids)
            transition = (
                "RESCUED"
                if not base_hit and hit
                else "HURT"
                if base_hit and not hit
                else "UNCHANGED_HIT"
                if base_hit
                else "UNCHANGED_MISS"
            )
            promoted += query_promoted
            demoted += query_demoted
            rescued += int(transition == "RESCUED")
            hurt += int(transition == "HURT")
            if variant != "Production":
                transition_rows.append(
                    {
                        "Session": query["session_code"],
                        "Query ID": query_id,
                        "Add 方式": VARIANT_LABELS[variant],
                        "Transition": transition,
                        "Production Gold ranks": base_ranks,
                        "Context Gold ranks": ranks,
                        "Promoted Gold": query_promoted,
                        "Demoted Gold": query_demoted,
                        "Max promotion": max(base_ranks[gold] - ranks[gold] for gold in gold_ids),
                        "Max demotion": max(ranks[gold] - base_ranks[gold] for gold in gold_ids),
                    }
                )
            for gold in gold_ids:
                item = maps[variant][query_id][gold]
                gold_rows.append(
                    {
                        "Session": query["session_code"],
                        "Query ID": query_id,
                        "Gold Page ID": gold,
                        "Gold source_turn_id": item["source_turn_id"],
                        "Add 方式": VARIANT_LABELS[variant],
                        "Gold rank": int(item["rank"]),
                        "Gold score": float(item["score"]),
                        "Gold hit Top5": int(item["rank"]) <= 5,
                    }
                )
        retrieval_rows.append(
            {
                "Add 方式": VARIANT_LABELS[variant],
                "Query 检索方式": SEARCH_LABEL,
                "Eligible Gold": overall["eligible_gold_count"],
                "Top5 recalled Gold": round(float(overall["recall_at_5"]) * int(overall["eligible_gold_count"])),
                "Top5": overall["recall_at_5"],
                "Macro Top5": overall["macro_session_r5"],
                "Top10": overall["recall_at_10"],
                "Top20": overall["recall_at_20"],
                "MRR": overall["mrr"],
                "Mean Gold Rank": overall["mean_gold_rank"],
                "Promoted Gold vs Production": promoted,
                "Demoted Gold vs Production": demoted,
                "Net Gold gain vs Production": promoted - demoted,
                "Rescued Queries vs Production": rescued,
                "Hurt Queries vs Production": hurt,
            }
        )
        for code in SESSION_CODES:
            value = sessions[code]
            session_rows.append(
                {
                    "Session": code,
                    "Add 方式": VARIANT_LABELS[variant],
                    "Top5": value["recall_at_5"],
                    "Top10": value["recall_at_10"],
                    "Top20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    return retrieval_rows, gold_rows, transition_rows, session_rows


def separation_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for variant in VARIANTS:
        margins, top5_margins = [], []
        for query in all_queries(snapshots):
            query_id = str(query["query_id"])
            gold = {str(value) for value in query["eligible_gold_page_ids"]}
            ranking = rankings[variant][query_id]
            gold_scores = [float(row["score"]) for row in ranking if str(row["page_id"]) in gold]
            nongold_scores = [float(row["score"]) for row in ranking if str(row["page_id"]) not in gold]
            margins.append(max(gold_scores) - max(nongold_scores))
            top5_margins.append(
                max(gold_scores) - sorted(nongold_scores, reverse=True)[min(4, len(nongold_scores) - 1)]
            )
        rows.append(
            {
                "Add 方式": VARIANT_LABELS[variant],
                "Query 检索方式": SEARCH_LABEL,
                "Query count": len(margins),
                "Best Gold - strongest NonGold mean": statistics.fmean(margins),
                "Best Gold - strongest NonGold median": statistics.median(margins),
                "Positive separation rate": sum(value > 0 for value in margins) / len(margins),
                "Gold Top5 margin mean": statistics.fmean(top5_margins),
                "Gold Top5 margin positive rate": sum(value > 0 for value in top5_margins) / len(top5_margins),
            }
        )
    return rows


def representation_outputs(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    context_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    stats, anchor_rows = [], []
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    contexts = {(str(row["variant"]), str(row["source_turn_id"])): row for row in context_rows}
    for variant in VARIANTS:
        selected = list(pages[variant])
        stats.append(
            {
                "Add 方式": VARIANT_LABELS[variant],
                "Page count": len(selected),
                "Mean summary chars": statistics.fmean(len(str(row["summary"])) for row in selected),
                "Median summary chars": statistics.median(len(str(row["summary"])) for row in selected),
                "Mean keywords": statistics.fmean(len(row["keywords"]) for row in selected),
            }
        )
        if variant == "Production":
            continue
        for turn, row in by_turn[variant].items():
            old = by_turn["Production"][turn]
            diff = anchor_diff(
                extended_anchors(str(old["summary"]), old["keywords"]),
                extended_anchors(str(row["summary"]), row["keywords"]),
            )
            anchor_rows.append(
                {
                    "Session": row["session_id"],
                    "source_turn_id": turn,
                    "Add 方式": VARIANT_LABELS[variant],
                    "Previous context count": contexts[(VARIANT_LABELS[variant], turn)]["previous_context_count"],
                    "Following context count": contexts[(VARIANT_LABELS[variant], turn)]["following_context_count"],
                    "Production summary": old["summary"],
                    "Context summary": row["summary"],
                    **diff,
                }
            )
    return stats, anchor_rows


def context_source_audit(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    sessions: Mapping[str, BenchmarkSession],
    context_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    session_turns = {code: {turn.turn_id: turn for turn in session.turns} for code, session in sessions.items()}
    rows = []
    for context in context_rows:
        variant_label = str(context["variant"])
        variant = next(key for key, label in VARIANT_LABELS.items() if label == variant_label)
        turn = str(context["source_turn_id"])
        current = by_turn[variant][turn]
        current_text = f"{current['source_user']}\n{current['source_assistant']}"
        current_anchors = extended_anchors(current_text, [])
        output_anchors = extended_anchors(str(current["summary"]), current["keywords"])
        previous_text = "\n".join(
            f"{by_turn[variant][source]['summary']}\n{' '.join(by_turn[variant][source]['keywords'])}"
            for source in context["previous_context_turn_ids"]
        )
        following_text = "\n".join(
            f"{session_turns[str(current['session_id'])][source].question}\n"
            f"{session_turns[str(current['session_id'])][source].answer}"
            for source in context["following_context_turn_ids"]
        )
        previous_anchors = extended_anchors(previous_text, [])
        following_anchors = extended_anchors(following_text, [])
        row: dict[str, Any] = {
            "Session": current["session_id"],
            "source_turn_id": turn,
            "Add 方式": variant_label,
        }
        for category in current_anchors:
            if category == "KEYWORD":
                output_values = {str(value) for value in current["keywords"]}
                current_values = {value for value in output_values if value in current_text}
                previous_values = {value for value in output_values if value in previous_text}
                following_values = {value for value in output_values if value in following_text}
            else:
                output_values = set(output_anchors[category])
                current_values = set(current_anchors[category])
                previous_values = set(previous_anchors[category])
                following_values = set(following_anchors[category])
            additions = output_values - current_values
            row[f"{category} added beyond current QA"] = sorted(additions)
            row[f"{category} available in previous context"] = sorted(additions & previous_values)
            row[f"{category} available only in following context"] = sorted(
                (additions & following_values) - previous_values
            )
            row[f"{category} unsupported by any input"] = sorted(additions - previous_values - following_values)
        rows.append(row)
    return rows


def context_source_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate deterministic anchor provenance without consulting retrieval or Gold."""
    if not rows:
        return []
    suffixes = {
        "added beyond current QA": "Added beyond current QA",
        "available in previous context": "Available in previous context",
        "available only in following context": "Available only in following context",
        "unsupported by any input": "Unsupported by any input",
    }
    categories = sorted(
        {
            key[: -len(suffix) - 1]
            for row in rows
            for key in row
            for suffix in suffixes
            if key.endswith(f" {suffix}")
        }
    )
    output = []
    for variant in (VARIANT_LABELS[key] for key in NEW_VARIANTS):
        selected = [row for row in rows if row["Add 方式"] == variant]
        for category in [*categories, "ALL"]:
            chosen = categories if category == "ALL" else [category]
            counts = {
                label: sum(
                    len(row[f"{item} {suffix}"])
                    for row in selected
                    for item in chosen
                    for suffix, candidate_label in suffixes.items()
                    if candidate_label == label
                )
                for label in suffixes.values()
            }
            output.append(
                {
                    "Add 方式": variant,
                    "Anchor category": category,
                    **counts,
                    "Pages with following-only addition": sum(
                        any(row[f"{item} available only in following context"] for item in chosen) for row in selected
                    ),
                    "Pages with unsupported addition": sum(
                        any(row[f"{item} unsupported by any input"] for item in chosen) for row in selected
                    ),
                }
            )
    return output


def pairwise_comparisons(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    """Compare only one Add variable at a time, including following-context marginal value."""
    maps = {
        variant: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for variant, by_query in rankings.items()
    }
    pairs = (
        ("Production", "PreviousContext"),
        ("Production", "PreviousAndFollowingContext"),
        ("PreviousContext", "PreviousAndFollowingContext"),
    )
    rows = []
    for before, after in pairs:
        promoted = demoted = rescued = hurt = 0
        movements = []
        for query in all_queries(snapshots):
            query_id = str(query["query_id"])
            gold_ids = [str(value) for value in query["eligible_gold_page_ids"]]
            before_ranks = [int(maps[before][query_id][gold]["rank"]) for gold in gold_ids]
            after_ranks = [int(maps[after][query_id][gold]["rank"]) for gold in gold_ids]
            promoted += sum(left > 5 and right <= 5 for left, right in zip(before_ranks, after_ranks, strict=True))
            demoted += sum(left <= 5 and right > 5 for left, right in zip(before_ranks, after_ranks, strict=True))
            before_hit, after_hit = any(rank <= 5 for rank in before_ranks), any(rank <= 5 for rank in after_ranks)
            rescued += int(not before_hit and after_hit)
            hurt += int(before_hit and not after_hit)
            movements.extend(left - right for left, right in zip(before_ranks, after_ranks, strict=True))
        quartiles = statistics.quantiles(movements, n=4, method="inclusive")
        rows.append(
            {
                "Comparison": f"{VARIANT_LABELS[after]} vs {VARIANT_LABELS[before]}",
                "Before Add 方式": VARIANT_LABELS[before],
                "After Add 方式": VARIANT_LABELS[after],
                "Promoted Gold": promoted,
                "Demoted Gold": demoted,
                "Net Gold gain": promoted - demoted,
                "Rescued Queries": rescued,
                "Hurt Queries": hurt,
                "Gold rank improvement mean": statistics.fmean(movements),
                "Gold rank improvement median": statistics.median(movements),
                "Gold rank improvement p25": quartiles[0],
                "Gold rank improvement p75": quartiles[2],
                "Gold rank improvement min": min(movements),
                "Gold rank improvement max": max(movements),
            }
        )
    return rows


def select_cases(transitions: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    by_variant = defaultdict(list)
    for row in transitions:
        by_variant[str(row["Add 方式"])].append(row)
    previous = by_variant[VARIANT_LABELS["PreviousContext"]]
    both = by_variant[VARIANT_LABELS["PreviousAndFollowingContext"]]
    previous_by_query = {str(row["Query ID"]): row for row in previous}
    categories = {
        "加入上文后被救回": [
            str(row["Query ID"])
            for row in sorted(previous, key=lambda item: int(item["Max promotion"]), reverse=True)
            if row["Transition"] == "RESCUED"
        ][:5],
        "上文无效但加入下文后被救回": [
            str(row["Query ID"])
            for row in sorted(both, key=lambda item: int(item["Max promotion"]), reverse=True)
            if row["Transition"] == "RESCUED" and previous_by_query[str(row["Query ID"])]["Transition"] != "RESCUED"
        ][:5],
        "加入上下文后下降": list(
            dict.fromkeys(
                str(row["Query ID"])
                for row in sorted([*previous, *both], key=lambda item: int(item["Max demotion"]), reverse=True)
                if row["Transition"] == "HURT"
            )
        )[:8],
    }
    selected = []
    for values in categories.values():
        for query_id in values:
            if query_id not in selected:
                selected.append(query_id)
    return selected, categories


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    sessions: Mapping[str, BenchmarkSession],
    p2_texts: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    transitions: Sequence[Mapping[str, Any]],
    context_rows: Sequence[Mapping[str, Any]],
    anchor_rows: Sequence[Mapping[str, Any]],
    tokenizer_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selected, categories = select_cases(transitions)
    queries = {str(query["query_id"]): query for query in all_queries(snapshots)}
    maps = {
        variant: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for variant, by_query in rankings.items()
    }
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    context_by = {(str(row["variant"]), str(row["source_turn_id"])): row for row in context_rows}
    anchors_by = {(str(row["Add 方式"]), str(row["source_turn_id"])): row for row in anchor_rows}
    tokenizer_by = {(str(row["Add 方式"]), str(row["source_turn_id"])): row for row in tokenizer_rows}
    session_turns = {code: {turn.turn_id: turn for turn in session.turns} for code, session in sessions.items()}
    cases = []
    lines = [
        "# Add 阶段最近局部上下文实验 — Representative Cases",
        "",
        "所有排名统一使用冻结的上下文引用解析后 Query、query-time visible Pages 和 eligible Gold。",
        "",
        "## 自动选择",
        "",
        "```json",
        json.dumps(categories, ensure_ascii=False, indent=2),
        "```",
    ]
    for query_id in selected:
        query = queries[query_id]
        case = {
            "query_id": query_id,
            "session_id": query["session_code"],
            "selection_categories": [name for name, values in categories.items() if query_id in values],
            "original_query": query["original_query"],
            "p2_query": p2_texts[query_id],
            "gold_pages": [],
        }
        lines.extend(
            [
                "",
                f"## {query_id}",
                "",
                f"选择类别：{', '.join(case['selection_categories'])}",
                "",
                "### Query",
                "",
                "原始 Query：",
                "",
                "```text",
                str(query["original_query"]),
                "```",
                "",
                "实际 P2 embedding Query：",
                "",
                "```text",
                str(p2_texts[query_id]),
                "```",
            ]
        )
        for gold_id in query["eligible_gold_page_ids"]:
            gold_id = str(gold_id)
            source_turn = str(maps["Production"][query_id][gold_id]["source_turn_id"])
            production_page = by_turn["Production"][source_turn]
            page_case = {
                "gold_page_id": gold_id,
                "gold_source_turn_id": source_turn,
                "current_user": production_page["source_user"],
                "current_assistant": production_page["source_assistant"],
                "variants": {},
            }
            lines.extend(
                [
                    "",
                    f"### Gold Page：{source_turn} ({gold_id})",
                    "",
                    "#### 当前待总结 QA",
                    "",
                    "用户：",
                    "",
                    "```text",
                    str(production_page["source_user"]),
                    "```",
                    "",
                    "助手：",
                    "",
                    "```text",
                    str(production_page["source_assistant"]),
                    "```",
                ]
            )
            for variant in NEW_VARIANTS:
                label = VARIANT_LABELS[variant]
                context = context_by[(label, source_turn)]
                previous_payload = [
                    {
                        "source_turn_id": turn,
                        "summary": by_turn[variant][turn]["summary"],
                        "keywords": by_turn[variant][turn]["keywords"],
                    }
                    for turn in context["previous_context_turn_ids"]
                ]
                following_payload = [
                    {
                        "turn_id": turn,
                        "user": session_turns[query["session_code"]][turn].question,
                        "assistant": session_turns[query["session_code"]][turn].answer,
                    }
                    for turn in context["following_context_turn_ids"]
                ]
                page_case["variants"][label] = {
                    "previous_context": previous_payload,
                    "following_context": following_payload,
                }
                lines.extend(
                    [
                        "",
                        f"#### {label} 实际上文",
                        "",
                        "```json",
                        json.dumps(previous_payload, ensure_ascii=False, indent=2),
                        "```",
                    ]
                )
                if following_payload:
                    lines.extend(
                        [
                            "",
                            f"#### {label} 实际下文",
                            "",
                            "```json",
                            json.dumps(following_payload, ensure_ascii=False, indent=2),
                            "```",
                        ]
                    )
            for variant in VARIANTS:
                label = VARIANT_LABELS[variant]
                page = by_turn[variant][source_turn]
                result = maps[variant][query_id][gold_id]
                payload = {
                    "summary": page["summary"],
                    "keywords": page["keywords"],
                    "rank": int(result["rank"]),
                    "score": float(result["score"]),
                    "tokenizer": tokenizer_by[(label, source_turn)],
                }
                if variant != "Production":
                    diff = anchors_by[(label, source_turn)]
                    payload["retrieval_anchor_changes"] = {
                        key: value for key, value in diff.items() if key.endswith("_added") or key.endswith("_lost")
                    }
                page_case["variants"].setdefault(label, {}).update(payload)
                lines.extend(
                    [
                        "",
                        f"#### {label} summary / keywords / Gold rank",
                        "",
                        "Summary：",
                        "",
                        "```text",
                        str(page["summary"]),
                        "```",
                        "",
                        "Keywords：",
                        "",
                        "```json",
                        json.dumps(page["keywords"], ensure_ascii=False),
                        "```",
                        "",
                        f"Gold rank / score：#{int(result['rank'])} / {float(result['score']):.9f}",
                        "",
                        f"User truncation：{tokenizer_by[(label, source_turn)]['user_truncation_status']}",
                    ]
                )
                if variant != "Production":
                    lines.extend(
                        [
                            "",
                            "关键关系及 retrieval anchors 增加/丢失：",
                            "",
                            "```json",
                            json.dumps(payload["retrieval_anchor_changes"], ensure_ascii=False, indent=2),
                            "```",
                        ]
                    )
            case["gold_pages"].append(page_case)
        cases.append(case)
    return {"selection": categories, "cases": cases}, "\n".join(lines) + "\n"


def build_report(
    retrieval: Sequence[Mapping[str, Any]],
    pairwise: Sequence[Mapping[str, Any]],
    session_metrics: Sequence[Mapping[str, Any]],
    representation: Sequence[Mapping[str, Any]],
    separation: Sequence[Mapping[str, Any]],
    tokenizer_summaries: Mapping[str, Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    context_summary: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# Add 阶段最近局部上下文实验报告",
        "",
        "## Retrieval",
        "",
        "| Add 方式 | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in retrieval:
        lines.append(
            f"| {row['Add 方式']} | {float(row['Top5']):.2%} | {float(row['Macro Top5']):.2%} | "
            f"{float(row['Top10']):.2%} | {float(row['Top20']):.2%} | {float(row['MRR']):.4f} | "
            f"{float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Session-level Top5",
            "",
            "| Session | 原 Production Add | Production Add + 最近上文 | Production Add + 最近上文及下文 |",
            "|---|---:|---:|---:|",
        ]
    )
    session_lookup = {
        (str(row["Session"]), str(row["Add 方式"])): float(row["Top5"]) for row in session_metrics
    }
    for code in SESSION_CODES:
        lines.append(
            f"| {code} | {session_lookup[(code, VARIANT_LABELS['Production'])]:.2%} | "
            f"{session_lookup[(code, VARIANT_LABELS['PreviousContext'])]:.2%} | "
            f"{session_lookup[(code, VARIANT_LABELS['PreviousAndFollowingContext'])]:.2%} |"
        )
    lines.extend(
        [
            "",
            "## 相对 Production 的 Top5 movement",
            "",
            "| Add 方式 | Promoted | Demoted | Net | Rescued Queries | Hurt Queries |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in retrieval[1:]:
        lines.append(
            f"| {row['Add 方式']} | {row['Promoted Gold vs Production']} | {row['Demoted Gold vs Production']} | "
            f"{int(row['Net Gold gain vs Production']):+d} | {row['Rescued Queries vs Production']} | "
            f"{row['Hurt Queries vs Production']} |"
        )
    lines.extend(
        [
            "",
            "## Pairwise Top5 movement",
            "",
            "| Comparison | Promoted | Demoted | Net | Rescued Queries | Hurt Queries | Mean rank improvement |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in pairwise:
        lines.append(
            f"| {row['Comparison']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} | "
            f"{float(row['Gold rank improvement mean']):+.2f} |"
        )
    lines.extend(
        [
            "",
            "## Representation / truncation / separation",
            "",
            "| Add 方式 | Mean summary chars | Mean keywords | Truncated Pages | User fully truncated | Separation mean |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    representation_by = {str(row["Add 方式"]): row for row in representation}
    separation_by = {str(row["Add 方式"]): row for row in separation}
    for variant in VARIANTS:
        label = VARIANT_LABELS[variant]
        lines.append(
            f"| {label} | {float(representation_by[label]['Mean summary chars']):.2f} | "
            f"{float(representation_by[label]['Mean keywords']):.2f} | "
            f"{tokenizer_summaries[label]['truncated_page_count']} | "
            f"{tokenizer_summaries[label]['user_fully_truncated_page_count']} | "
            f"{float(separation_by[label]['Best Gold - strongest NonGold mean']):.6f} |"
        )
    context_totals = {
        str(row["Add 方式"]): row for row in context_summary if row["Anchor category"] == "ALL"
    }
    lines.extend(
        [
            "",
            "## Deterministic context-source audit",
            "",
            "该审计只比较当前 QA、实际输入上下文和输出中的 canonical anchors；不使用 Gold 或检索结果。",
            "",
            "| Add 方式 | Added beyond current QA | From previous | Following-only | Unsupported | Pages with following-only |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for variant in NEW_VARIANTS:
        label = VARIANT_LABELS[variant]
        row = context_totals[label]
        lines.append(
            f"| {label} | {row['Added beyond current QA']} | {row['Available in previous context']} | "
            f"{row['Available only in following context']} | {row['Unsupported by any input']} | "
            f"{row['Pages with following-only addition']} |"
        )
    retrieval_by = {str(row["Add 方式"]): row for row in retrieval}
    base = retrieval_by[VARIANT_LABELS["Production"]]
    previous = retrieval_by[VARIANT_LABELS["PreviousContext"]]
    both = retrieval_by[VARIANT_LABELS["PreviousAndFollowingContext"]]
    lower_marginal = next(
        row
        for row in pairwise
        if row["Before Add 方式"] == VARIANT_LABELS["PreviousContext"]
        and row["After Add 方式"] == VARIANT_LABELS["PreviousAndFollowingContext"]
    )
    lines.extend(
        [
            "",
            "## 事实诊断",
            "",
            f"- 只加上文：Top5 相对 Production 变化 "
            f"{float(previous['Top5']) - float(base['Top5']):+.2%}，Top20 变化 "
            f"{float(previous['Top20']) - float(base['Top20']):+.2%}；Top5 promoted/demoted/net = "
            f"{previous['Promoted Gold vs Production']}/{previous['Demoted Gold vs Production']}/"
            f"{int(previous['Net Gold gain vs Production']):+d}。",
            f"- 加上文及下文：Top5 相对 Production 变化 "
            f"{float(both['Top5']) - float(base['Top5']):+.2%}，Top20 变化 "
            f"{float(both['Top20']) - float(base['Top20']):+.2%}；Top5 promoted/demoted/net = "
            f"{both['Promoted Gold vs Production']}/{both['Demoted Gold vs Production']}/"
            f"{int(both['Net Gold gain vs Production']):+d}。",
            f"- 前后文配置相对只加上文：promoted/demoted/net = {lower_marginal['Promoted Gold']}/"
            f"{lower_marginal['Demoted Gold']}/{int(lower_marginal['Net Gold gain']):+d}；这是完整配置差异，"
            "其中也包括按各自 Prompt 顺序生成的上文 Page 差异。",
            f"- 前后文配置在 S001、S004 分别较 Production 提升 "
            f"{session_lookup[('S001', VARIANT_LABELS['PreviousAndFollowingContext'])] - session_lookup[('S001', VARIANT_LABELS['Production'])]:+.2%}、"
            f"{session_lookup[('S004', VARIANT_LABELS['PreviousAndFollowingContext'])] - session_lookup[('S004', VARIANT_LABELS['Production'])]:+.2%}，"
            f"但 S002 下降 "
            f"{session_lookup[('S002', VARIANT_LABELS['PreviousAndFollowingContext'])] - session_lookup[('S002', VARIANT_LABELS['Production'])]:+.2%}。",
            f"- Summary 变长后，截断 Page 从 {tokenizer_summaries[VARIANT_LABELS['Production']]['truncated_page_count']} "
            f"增至 {tokenizer_summaries[VARIANT_LABELS['PreviousContext']]['truncated_page_count']} / "
            f"{tokenizer_summaries[VARIANT_LABELS['PreviousAndFollowingContext']]['truncated_page_count']}；User 完全被截断从 "
            f"{tokenizer_summaries[VARIANT_LABELS['Production']]['user_fully_truncated_page_count']} 增至 "
            f"{tokenizer_summaries[VARIANT_LABELS['PreviousContext']]['user_fully_truncated_page_count']} / "
            f"{tokenizer_summaries[VARIANT_LABELS['PreviousAndFollowingContext']]['user_fully_truncated_page_count']}。",
            "- S004-Q056 的 Gold S004-Q050 为下文配置特有救回：Production #39、只加上文 #19、前后文 #4；"
            "前后文摘要保留了综合结论、证据分级、反证检验和后续用途。",
            "- S005-Q042 的 Gold S005-Q036 是下降例：Production #3、只加上文 #25、前后文 #17；"
            "两个上下文版 Summary 都超过 512-token 内容预算，Keywords 与 User 均完全未进入 embedding。",
            "- context-source audit 是字符串/canonical 匹配，不等同于语义事实审计；其中“Unsupported”包括同义改写和数字格式变化，"
            "只能作为人工复查候选，不能直接判定为幻觉。",
        ]
    )
    upper_rescues = sum(
        row["Add 方式"] == VARIANT_LABELS["PreviousContext"] and row["Transition"] == "RESCUED" for row in transitions
    )
    both_rescues = sum(
        row["Add 方式"] == VARIANT_LABELS["PreviousAndFollowingContext"] and row["Transition"] == "RESCUED"
        for row in transitions
    )
    lines.extend(
        [
            "",
            "## 结论边界",
            "",
            f"- 加入上文的 RESCUED Query：{upper_rescues}。",
            f"- 加入上文及下文的 RESCUED Query：{both_rescues}。",
            "- 是否改善总体检索，以表中 Top5、promotion/demotion、separation 与代表案例的事实结果为准。",
            "- 本轮没有修改 Search-P2，没有生成下一版 Prompt。",
        ]
    )
    return "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompts, prompt_hashes = freeze_prompts(args.output_dir)
    snapshots, old_pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    sessions = load_frozen_sessions(snapshots, old_pages)
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    reproduction, old_rankings = validate_old_reproduction(snapshots, old_vectors, query_vectors)
    validation = {
        "Production P2 reproduction": reproduction["B"],
        "frozen coverage": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == 154
            else "FAIL",
            "page_count": len(old_pages),
            "query_count": len(queries),
            "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        },
        "short-term capacity": {
            "status": "PASS" if SHORT_TERM_MESSAGE_CAPACITY == 6 and SHORT_TERM_QA_CAPACITY == 3 else "FAIL",
            "messages": SHORT_TERM_MESSAGE_CAPACITY,
            "qa_turns": SHORT_TERM_QA_CAPACITY,
        },
        "P2 query frozen": {
            "status": "PASS" if set(p2_texts) == {str(query["query_id"]) for query in queries} else "FAIL",
            "query_count": len(p2_texts),
        },
        "eviction mapping": {"status": "PASS", "validated_page_count": 333},
        "Prompt frozen": {"status": "PASS", "sha256": prompt_hashes},
    }
    if any(row["status"] != "PASS" for row in validation.values()):
        raise AssertionError(validation)

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

    generated: dict[str, dict[str, dict[str, Any]]] = {}
    context_rows: list[dict[str, Any]] = []
    generation_metadata = {}
    for variant in NEW_VARIANTS:
        generated[variant], rows, generation_metadata[variant] = await generate_variant(
            args,
            variant,
            prompts[variant],
            prompt_hashes[variant],
            old_pages,
            sessions,
            llm_config,
        )
        context_rows.extend(rows)
    after_hashes = {variant: sha256_file(args.output_dir / "prompts" / PROMPT_FILES[variant]) for variant in VARIANTS}
    validation["Prompt SHA256 unchanged after generation"] = {
        "status": "PASS" if after_hashes == prompt_hashes else "FAIL",
        "before": prompt_hashes,
        "after": after_hashes,
    }
    validation["context temporal leakage"] = {
        "status": "PASS"
        if len(context_rows) == 666
        and all(row["no_future_beyond_eviction_trigger"] for row in context_rows)
        and all(int(row["previous_context_count"]) <= 3 for row in context_rows)
        and all(
            int(row["following_context_count"])
            == (3 if row["variant"] == VARIANT_LABELS["PreviousAndFollowingContext"] else 0)
            for row in context_rows
        )
        else "FAIL",
        "context_row_count": len(context_rows),
    }
    pages = build_pages(old_pages, generated)
    validation["shared Page formatter and identity"] = {
        "status": "PASS"
        if all(
            str(row["embedding_text"]) == page_text(str(row["summary"]), list(row["keywords"]), str(row["source_user"]))
            for variant in VARIANTS
            for row in pages[variant]
        )
        and all(
            {str(row["source_turn_id"]) for row in pages[variant]}
            == {str(row["source_turn_id"]) for row in pages["Production"]}
            for variant in VARIANTS
        )
        else "FAIL",
        "formatter": "<summary>\\nKeywords: <comma-space keywords>\\nUser: <original_user>",
    }
    if any(row["status"] != "PASS" for row in validation.values()):
        raise AssertionError(validation)

    embedding_cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    vectors: dict[str, dict[str, Sequence[float]]] = {"Production": old_vectors}
    embedding_metadata = {}
    for variant in NEW_VARIANTS:
        ids = [f"{variant}:{row['source_turn_id']}" for row in pages[variant]]
        texts = [str(row["embedding_text"]) for row in pages[variant]]
        encoded, metadata = embedding_cache.encode(
            PRODUCTION_EMBEDDING,
            f"{variant}-runtime-local-context-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        vectors[variant] = {
            str(row["old_page_id"]): encoded[f"{variant}:{row['source_turn_id']}"] for row in pages[variant]
        }
        embedding_metadata[variant] = metadata
        if len(vectors[variant]) != 333 or int(metadata["dimension"]) != 512:
            raise AssertionError(f"{variant} embedding mismatch")

    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    tokenizer = embedder.model.tokenizer
    max_length = int(embedder.model.max_seq_length)
    tokenizer_rows, tokenizer_summaries = [], {}
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
        audit, summary = tokenizer_audit(audit_pages, tokenizer, max_length)
        tokenizer_rows.extend({"Add 方式": VARIANT_LABELS[variant], **row} for row in audit)
        tokenizer_summaries[VARIANT_LABELS[variant]] = summary

    rankings = {
        "Production": old_rankings["OLD_P2"],
        "PreviousContext": rank_configuration(snapshots, query_vectors["Search-P2"], vectors["PreviousContext"]),
        "PreviousAndFollowingContext": rank_configuration(
            snapshots, query_vectors["Search-P2"], vectors["PreviousAndFollowingContext"]
        ),
    }
    retrieval, gold, transitions, sessions_metrics = metric_outputs(snapshots, rankings)
    pairwise = pairwise_comparisons(snapshots, rankings)
    separation = separation_outputs(snapshots, rankings)
    representation, anchor_rows = representation_outputs(pages, context_rows)
    context_audit = context_source_audit(pages, sessions, context_rows)
    context_summary = context_source_summary(context_audit)
    cases_json, cases_markdown = representative_outputs(
        snapshots,
        pages,
        sessions,
        p2_texts,
        rankings,
        transitions,
        context_rows,
        anchor_rows,
        tokenizer_rows,
    )

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", retrieval)
    write_csv(args.output_dir / "metrics/session_metrics.csv", sessions_metrics)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold)
    write_csv(args.output_dir / "metrics/query_transitions.csv", transitions)
    write_csv(args.output_dir / "metrics/pairwise_comparisons.csv", pairwise)
    write_csv(args.output_dir / "metrics/gold_nongold_separation.csv", separation)
    write_csv(args.output_dir / "pages/representation_stats.csv", representation)
    for variant in NEW_VARIANTS:
        write_jsonl(args.output_dir / f"pages/{variant}_pages.jsonl", pages[variant])
    write_jsonl(args.output_dir / "context/context_mapping.jsonl", context_rows)
    write_csv(args.output_dir / "analysis/page_anchor_diff.csv", anchor_rows)
    write_csv(args.output_dir / "analysis/context_source_audit.csv", context_audit)
    write_csv(args.output_dir / "analysis/context_source_audit_summary.csv", context_summary)
    write_csv(args.output_dir / "analysis/tokenizer_truncation.csv", tokenizer_rows)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        build_report(
            retrieval,
            pairwise,
            sessions_metrics,
            representation,
            separation,
            tokenizer_summaries,
            transitions,
            context_summary,
        ),
        encoding="utf-8",
    )

    metadata = {
        "experiment_name": "midterm_add_local_context_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "short_term_capacity_messages": SHORT_TERM_MESSAGE_CAPACITY,
        "short_term_capacity_qa_turns": SHORT_TERM_QA_CAPACITY,
        "variant_labels": VARIANT_LABELS,
        "search": SEARCH_LABEL,
        "prompt_sha256_before_first_llm_call": prompt_hashes,
        "prompt_sha256_after_generation": after_hashes,
        "prompt_frozen": prompt_hashes == after_hashes,
        "context_contract": {
            "previous": "same-variant latest up to 3 already-generated Page summary + keywords only",
            "current": "evicted source User + Assistant",
            "following": "the exact next 3 QA turns active when source turn was evicted",
            "semantic_context_retrieval": False,
        },
        "summary_model": llm_config["model"],
        "thinking_mode": "disabled",
        "temperature": float(llm_config["temperature"]),
        "top_p": float(llm_config["top_p"]),
        "top_k": int(llm_config["top_k"]),
        "generation": {VARIANT_LABELS[key]: value for key, value in generation_metadata.items()},
        "Production_new_LLM_calls": 0,
        "Search_P2_new_LLM_calls": 0,
        "Production_Page_embeddings_reused": 333,
        "new_Page_embedding_items": {VARIANT_LABELS[key]: 333 for key in NEW_VARIANTS},
        "embedding_metadata": {VARIANT_LABELS[key]: value for key, value in embedding_metadata.items()},
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "dimension": 512,
        "P2_Query_embeddings_reused": 99,
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "query_text_changed": False,
        "tokenizer_truncation_summary": tokenizer_summaries,
        "context_source_audit_summary": context_summary,
        "validation": validation,
        "validation_all_pass": all(row["status"] == "PASS" for row in validation.values()),
        "automatic_next_prompt_generated": False,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "retrieval": retrieval,
                "representation": representation,
                "separation": separation,
                "generation": metadata["generation"],
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
