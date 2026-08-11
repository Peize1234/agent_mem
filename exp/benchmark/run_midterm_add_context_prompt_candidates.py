"""Compare three local-context usage instructions for frozen MidTerm Add Pages.

All candidates retain the Production Add Prompt body, receive the same runtime-
visible eviction context, use Summary + Keywords only, and search with frozen P2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import percentile, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_conservative_retrieval_tuning_ablation import anchor_diff  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_ablation import (  # noqa: E402
    OUTPUT_DIR as LOCAL_CONTEXT_DIR,
    ContextSummaryCache,
    add_context_instructions,
    context_layout,
    format_context_input,
    load_frozen_sessions,
    sha256_text,
)
from exp.benchmark.run_midterm_add_local_context_controls import (  # noqa: E402
    compare_rankings,
    load_existing_add_pages,
    metric_row,
    separation_row,
    summary_keywords_text,
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
    rank_configuration,
    sha256_file,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_context_prompt_candidates"
VARIANTS = ("ProductionReference", "CurrentContext", "Candidate1", "Candidate2", "Candidate3")
GENERATED_VARIANTS = VARIANTS[2:]
VARIANT_LABELS = {
    "ProductionReference": "原 Production Add（参考）",
    "CurrentContext": "当前前后文 Prompt（主要基线）",
    "Candidate1": "候选一：关系解析优先，禁止上下文事实扩写",
    "Candidate2": "候选二：当前轮信息优先 + 一句关系补充",
    "Candidate3": "候选三：区分性信息优先，主动抑制重复内容",
}
PROMPT_FILES = {
    "CurrentContext": "当前前后文_Prompt_基线.txt",
    "Candidate1": "候选一_关系解析优先.txt",
    "Candidate2": "候选二_当前轮信息优先.txt",
    "Candidate3": "候选三_区分性信息优先.txt",
}
PROMPT_VERSIONS = {
    "Candidate1": "local-context-relation-only-v1",
    "Candidate2": "local-context-current-first-one-relation-v1",
    "Candidate3": "local-context-distinctive-information-v1",
}
SEARCH_LABEL = "上下文引用解析后检索（P2）"

CANDIDATE_1 = """## 上下文使用说明

输入中的上文和下文只用于帮助理解当前待总结对话在连续对话中的具体含义和位置。

摘要始终以“当前待总结对话”实际讨论的内容为主体。

使用上文时，重点解决：
- “前面”“刚才”“上一轮”“这个判断”等指代具体指什么；
- 当前轮是否属于延续、比较、验证、反证、补充、修订或阶段总结；
- 当前轮是在前文哪个判断或问题基础上继续推进。

使用下文时，只用于判断当前轮形成的信息后来承担了什么作用，例如：
- 是否成为后续验证、反证或修订的对象；
- 是否成为后续分析继续引用的基础；
- 当前轮在局部分析链条中承担什么作用。

上下文中的独立事实、数字、指标和结论，不属于当前轮内容时，不要写入当前摘要。

尤其不要为了体现“结合上下文”而重新展开上下文中的完整财务数据、三年指标、同比、来源或其它背景。

如果当前轮本身已经包含某项事实或数字，则按照原有规则保留；如果该信息只存在于上文或下文，则不要复制到当前摘要。

下文不得改变当前轮发生时的事实和判断状态，也不得把后续新结论写成当前轮自己的结论。

上下文关系应尽量具体，例如：
“本轮修订前文的效率判断”
优于：
“本轮结合前文进一步分析”。

如果上下文没有帮助，则忽略上下文，按照原有方式总结。"""

CANDIDATE_2 = """## 上下文使用说明

首先完全按照原有规则总结当前待总结对话本身。

上文和下文只作为辅助信息，用于解决当前对话中无法仅凭本轮确定的指代或关系。

摘要中的主体、指标、数字、事实、趋势和结论，原则上都必须来自当前待总结对话本身。

在完成当前轮摘要后，如果上下文能够明确当前轮与其它轮次之间存在重要关系，可以额外用一句简洁的话说明这种关系，例如：

- 承接前文某项判断；
- 对前文结论进行修订；
- 引入反证重新验证前文判断；
- 补充此前的信息缺口；
- 当前判断随后成为进一步验证或修订的对象。

这类关系说明原则上只保留最重要的一项或少数几项，不展开对应上下文中的完整事实。

不要重复上下文中的数字、指标序列、来源和结论。

不要为了体现上下文而增加与当前轮检索身份无关的信息。

如果当前轮没有明显的前后文依赖，则完全按照原 Production Prompt 的方式生成摘要。"""

CANDIDATE_3 = """## 上下文使用说明

上文和下文用于帮助判断当前待总结对话的具体任务、指代对象、与前文的关系以及后续实际作用。

生成摘要时，应优先保留能够区分当前这一轮与附近其它对话的信息，包括：

- 当前轮具体要解决的问题；
- 当前轮新增的分析动作或判断动作；
- 当前轮重点涉及的指标或事实；
- 当前轮得到的核心判断；
- 与前文之间明确的延续、比较、反证、修订、验证或补充关系；
- 如果确有必要，当前轮在后续对话中的实际作用。

对于在附近多轮中反复出现、且不能帮助区分当前轮的信息，应减少重复展开。

例如，同一公司、同一三年期间、相同来源、相同完整指标底表或相同通用限制，如果已经属于稳定背景，不需要因为上下文再次出现就全部复制到当前摘要。

但当前问题直接依赖的数字、指标、趋势、拐点或判断仍应保留，不能为了缩短摘要删除当前任务真正需要的信息。

不要将摘要扩展成多轮综合总结，也不要罗列上下文中所有可能相关的信息。

上下文的作用是帮助当前摘要更准确、更有区分度，而不是让摘要包含更多内容。"""

INSTRUCTIONS = {"Candidate1": CANDIDATE_1, "Candidate2": CANDIDATE_2, "Candidate3": CANDIDATE_3}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen front/back-context Add Prompt candidates")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--llm-timeout", type=float, default=240.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def freeze_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    current_path = LOCAL_CONTEXT_DIR / "prompts/Production_Add_With_Previous_And_Following_Context.txt"
    prompts = {
        "CurrentContext": current_path.read_text(encoding="utf-8"),
        **{
            variant: add_context_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, INSTRUCTIONS[variant])
            for variant in GENERATED_VARIANTS
        },
    }
    directory = output_dir / "prompts"
    directory.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for variant in ("CurrentContext", *GENERATED_VARIANTS):
        path = directory / PROMPT_FILES[variant]
        if path.exists() and path.read_text(encoding="utf-8") != prompts[variant]:
            raise AssertionError(f"Frozen Prompt content changed: {path}")
        if not path.exists():
            path.write_text(prompts[variant], encoding="utf-8")
        hashes[variant] = sha256_file(path)
        if (
            variant in GENERATED_VARIANTS
            and prompts[variant].replace(f"{INSTRUCTIONS[variant]}\n\n", "") != MIDTERM_PAGE_SUMMARY_PROMPT
        ):
            raise AssertionError(f"{variant}: changed Production Prompt outside context instructions")
    dump_json(
        directory / "prompt_sha256.json",
        {
            "all_candidates_frozen_before_first_llm_call": True,
            **{
                VARIANT_LABELS[variant]: {"file": PROMPT_FILES[variant], "sha256": hashes[variant]}
                for variant in ("CurrentContext", *GENERATED_VARIANTS)
            },
        },
    )
    return prompts, hashes


async def generate_candidate(
    args: argparse.Namespace,
    variant: str,
    prompt: str,
    prompt_sha256: str,
    old_pages: Sequence[Mapping[str, Any]],
    sessions: Mapping[str, Any],
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
    lock = asyncio.Lock()

    async def process_session(code: str) -> None:
        session = sessions[code]
        selected = sorted(
            (page for page in old_pages if page["session_code"] == code),
            key=lambda page: int(page["source_turn_index"]),
        )
        generated_previous: list[dict[str, Any]] = []
        for page in selected:
            layout = context_layout(page, session, generated_previous, include_following=True)
            payload = format_context_input(page, layout, include_following=True)
            row = await cache.call(
                page=page,
                variant=variant,
                prompt_version=PROMPT_VERSIONS[variant],
                prompt=prompt,
                prompt_sha256=prompt_sha256,
                payload=payload,
                layout=layout,
            )
            if row.get("status") != "SUCCESS":
                raise RuntimeError(f"{variant} failed at {page['source_turn_id']}: {row.get('errors')}")
            generated_page = {
                "session_code": code,
                "source_turn_id": page["source_turn_id"],
                "source_turn_index": page["source_turn_index"],
                "page_id": page["page_id"],
                "summary": row["parsed"]["summary"],
                "keywords": list(row["parsed"]["keywords"]),
                "user_input": page["user_input"],
            }
            generated_previous.append(generated_page)
            context_row = {
                "候选 Add Prompt": VARIANT_LABELS[variant],
                "session_id": code,
                "source_turn_id": page["source_turn_id"],
                "source_turn_index": page["source_turn_index"],
                "eviction_trigger_turn_id": layout["eviction_trigger_turn_id"],
                "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
                "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
                "previous_context_count": len(layout["previous_context"]),
                "following_context_count": len(layout["following_context"]),
                "input_payload_sha256": sha256_text(payload),
                "input_char_count": len(payload),
                "no_future_beyond_eviction_trigger": all(
                    int(item["turn_index"]) <= int(layout["eviction_trigger_turn_index"])
                    for item in layout["following_context"]
                ),
                "cache_key": row["cache_key"],
            }
            async with lock:
                results[str(page["source_turn_id"])] = row
                context_rows.append(context_row)

    try:
        await asyncio.gather(*(process_session(code) for code in SESSION_CODES))
    finally:
        await client.close()
    if len(results) != 333:
        raise AssertionError(f"{variant}: expected 333 summaries, got {len(results)}")
    rows = list(results.values())
    return (
        results,
        sorted(context_rows, key=lambda row: (row["session_id"], int(row["source_turn_index"]))),
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
    existing = load_existing_add_pages(old_pages)
    pages: dict[str, list[dict[str, Any]]] = {
        "ProductionReference": existing["Production"],
        "CurrentContext": existing["PreviousAndFollowingContext"],
    }
    for variant in GENERATED_VARIANTS:
        pages[variant] = []
        for old in old_pages:
            response = generated[variant][str(old["source_turn_id"])]
            summary = str(response["parsed"]["summary"])
            keywords = list(response["parsed"]["keywords"])
            pages[variant].append(
                {
                    "session_code": old["session_code"],
                    "source_turn_id": old["source_turn_id"],
                    "source_turn_index": old["source_turn_index"],
                    "page_id": old["page_id"],
                    "summary": summary,
                    "keywords": keywords,
                    "user_input": old["user_input"],
                    "no_user_text": summary_keywords_text(summary, keywords),
                    "llm_cache_key": response["cache_key"],
                }
            )
    for variant in VARIANTS:
        pages[variant].sort(key=lambda row: (str(row["session_code"]), int(row["source_turn_index"])))
    return pages


def load_reference_vectors(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any]]:
    cache = EmbeddingCache(LOCAL_CONTEXT_DIR / "controls/cache/embeddings")
    references = {
        "ProductionReference": "Production",
        "CurrentContext": "PreviousAndFollowingContext",
    }
    vectors, metadata = {}, {}
    for variant, parent_variant in references.items():
        selected = pages[variant]
        ids = [f"NoUser:{parent_variant}:{row['page_id']}" for row in selected]
        texts = [str(row["no_user_text"]) for row in selected]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"no-user-{parent_variant}-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        if not item_meta.get("cache_hit"):
            raise AssertionError(f"{variant}: expected frozen no-User embedding cache")
        vectors[variant] = {
            str(row["page_id"]): encoded[f"NoUser:{parent_variant}:{row['page_id']}"] for row in selected
        }
        metadata[variant] = item_meta
    return vectors, metadata


def encode_candidates(
    output_dir: Path,
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any], EmbeddingCache]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    vectors, metadata = {}, {}
    for variant in GENERATED_VARIANTS:
        selected = pages[variant]
        ids = [f"{variant}:{row['page_id']}" for row in selected]
        texts = [str(row["no_user_text"]) for row in selected]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"context-prompt-{variant}-summary-keywords-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        vectors[variant] = {str(row["page_id"]): encoded[f"{variant}:{row['page_id']}"] for row in selected}
        metadata[variant] = item_meta
        if len(vectors[variant]) != 333 or int(item_meta["dimension"]) != 512:
            raise AssertionError(f"{variant}: embedding coverage/dimension mismatch")
    return vectors, metadata, cache


def tokenizer_audit(
    pages: Mapping[str, Sequence[Mapping[str, Any]]], tokenizer: Any, max_length: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    special = int(tokenizer.num_special_tokens_to_add(pair=False))
    budget = max_length - special
    detail, summaries = [], []
    for variant in VARIANTS:
        for page in pages[variant]:
            summary_text = str(page["summary"]).strip()
            keywords_text = "Keywords: " + ", ".join(str(value) for value in page["keywords"])
            full_text = str(page["no_user_text"])
            summary_ids = tokenizer.encode(summary_text, add_special_tokens=False)
            keyword_ids = tokenizer.encode(keywords_text, add_special_tokens=False)
            full_ids = tokenizer.encode(full_text, add_special_tokens=False)
            if list(summary_ids) + list(keyword_ids) != list(full_ids):
                raise AssertionError(f"Tokenizer boundary mismatch: {variant}/{page['source_turn_id']}")
            summary_kept = min(len(summary_ids), budget)
            remaining = budget - summary_kept
            keyword_kept = min(len(keyword_ids), remaining)
            keyword_status = (
                "FULLY_KEPT"
                if keyword_kept == len(keyword_ids)
                else "FULLY_TRUNCATED"
                if keyword_kept == 0
                else "PARTIALLY_TRUNCATED"
            )
            detail.append(
                {
                    "session_id": page["session_code"],
                    "source_turn_id": page["source_turn_id"],
                    "page_id": page["page_id"],
                    "Add Prompt": VARIANT_LABELS[variant],
                    "summary_chars": len(summary_text),
                    "summary_tokens": len(summary_ids),
                    "summary_tokens_kept": summary_kept,
                    "summary_truncated": summary_kept < len(summary_ids),
                    "keywords_count": len(page["keywords"]),
                    "keywords_tokens": len(keyword_ids),
                    "keywords_tokens_kept": keyword_kept,
                    "keywords_truncation_status": keyword_status,
                    "raw_page_tokens_including_special": len(full_ids) + special,
                    "page_truncated": len(full_ids) + special > max_length,
                }
            )
        current = [row for row in detail if row["Add Prompt"] == VARIANT_LABELS[variant]]
        summaries.append(
            {
                "Add Prompt": VARIANT_LABELS[variant],
                "Page count": len(current),
                "Mean Summary chars": statistics.fmean(int(row["summary_chars"]) for row in current),
                "Mean Summary tokens": statistics.fmean(int(row["summary_tokens"]) for row in current),
                "Median Summary tokens": statistics.median(int(row["summary_tokens"]) for row in current),
                "P90 Summary tokens": percentile([int(row["summary_tokens"]) for row in current], 0.90),
                "Summary truncated Pages": sum(bool(row["summary_truncated"]) for row in current),
                "Keywords fully kept Pages": sum(row["keywords_truncation_status"] == "FULLY_KEPT" for row in current),
                "Keywords partially truncated Pages": sum(
                    row["keywords_truncation_status"] == "PARTIALLY_TRUNCATED" for row in current
                ),
                "Keywords fully truncated Pages": sum(
                    row["keywords_truncation_status"] == "FULLY_TRUNCATED" for row in current
                ),
                "Page truncated Pages": sum(bool(row["page_truncated"]) for row in current),
            }
        )
    return detail, summaries


def retrieval_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    metrics, sessions, comparisons, gold_rows, query_rows = [], [], [], [], []
    for variant in VARIANTS:
        row = metric_row(
            snapshots,
            rankings[variant],
            add_label=VARIANT_LABELS[variant],
            formatter_label="Summary + Keywords",
        )
        metrics.append(row)
        _, session_values = evaluate_all(snapshots, rankings[variant])
        for code in SESSION_CODES:
            value = session_values[code]
            sessions.append(
                {
                    "Session": code,
                    "Add Prompt": VARIANT_LABELS[variant],
                    "Top5": value["recall_at_5"],
                    "Top10": value["recall_at_10"],
                    "Top20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    for variant in GENERATED_VARIANTS:
        label = f"{VARIANT_LABELS[variant]} vs {VARIANT_LABELS['CurrentContext']}"
        aggregate, gold, queries = compare_rankings(
            snapshots,
            rankings["CurrentContext"],
            rankings[variant],
            comparison=label,
        )
        aggregate["候选 Add Prompt"] = VARIANT_LABELS[variant]
        comparisons.append(aggregate)
        for row in gold:
            row["候选 Add Prompt"] = VARIANT_LABELS[variant]
        for row in queries:
            row["候选 Add Prompt"] = VARIANT_LABELS[variant]
        gold_rows.extend(gold)
        query_rows.extend(queries)
    s002 = [row for row in query_rows if row["session_id"] == "S002"]
    return metrics, sessions, comparisons, gold_rows, query_rows, s002


def movement_anchor_outputs(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    rows = []
    counts: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for gold in gold_rows:
        before, after = int(gold["before_rank"]), int(gold["after_rank"])
        movement = "PROMOTED" if before > 5 and after <= 5 else "DEMOTED" if before <= 5 and after > 5 else None
        if movement is None:
            continue
        variant = next(key for key in GENERATED_VARIANTS if VARIANT_LABELS[key] == gold["候选 Add Prompt"])
        turn = str(gold["gold_source_turn_id"])
        diff = anchor_diff(
            extended_anchors(
                str(by_turn["CurrentContext"][turn]["summary"]), by_turn["CurrentContext"][turn]["keywords"]
            ),
            extended_anchors(str(by_turn[variant][turn]["summary"]), by_turn[variant][turn]["keywords"]),
        )
        row = {**gold, "Gold movement": movement, **diff}
        rows.append(row)
        for key, values in diff.items():
            if not key.endswith(("_added", "_lost")):
                continue
            category, direction = key.rsplit("_", 1)
            counts[(VARIANT_LABELS[variant], movement, direction.upper(), category)] += len(values)
    summary = [
        {
            "候选 Add Prompt": variant,
            "Gold movement": movement,
            "Anchor direction": direction,
            "Anchor category": category,
            "Anchor instances": count,
        }
        for (variant, movement, direction, category), count in sorted(counts.items())
    ]
    return rows, summary


def normalized_bigrams(value: str) -> set[str]:
    normalized = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value)).lower()
    return {normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))}


def lexical_coverage(source: str, target: str) -> float:
    source_items = normalized_bigrams(source)
    return len(source_items & normalized_bigrams(target)) / len(source_items) if source_items else 0.0


def current_turn_focus_audit(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    sessions: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flag summaries whose opening task description lexically matches a following User more than the current User."""
    rows = []
    session_turns = {code: {turn.turn_id: turn for turn in session.turns} for code, session in sessions.items()}
    for variant in VARIANTS[1:]:
        for page in pages[variant]:
            code = str(page["session_code"])
            index = int(page["source_turn_index"])
            focus_text = str(page["summary"])[:220]
            current_user = str(page["user_input"])
            current_coverage = lexical_coverage(current_user, focus_text)
            following = [
                sessions[code].turns[item] for item in range(index + 1, min(index + 4, len(sessions[code].turns)))
            ]
            following_scores = [(turn.turn_id, lexical_coverage(turn.question, focus_text)) for turn in following]
            best_turn, best_score = max(following_scores, key=lambda item: item[1])
            current_tasks = set(extended_anchors(current_user, [])["TASK"])
            output_tasks = set(extended_anchors(focus_text, [])["TASK"])
            following_tasks = {task for turn in following for task in extended_anchors(str(turn.question), [])["TASK"]}
            rows.append(
                {
                    "Session": code,
                    "source_turn_id": page["source_turn_id"],
                    "Add Prompt": VARIANT_LABELS[variant],
                    "Current User": current_user,
                    "Summary opening": focus_text,
                    "current_user_bigram_coverage": current_coverage,
                    "best_following_turn_id": best_turn,
                    "best_following_user_bigram_coverage": best_score,
                    "following_minus_current_coverage": best_score - current_coverage,
                    "potential_following_task_dominance": best_score > current_coverage + 0.12,
                    "current_user_tasks": sorted(current_tasks),
                    "summary_opening_tasks": sorted(output_tasks),
                    "following_only_tasks_in_summary": sorted((output_tasks & following_tasks) - current_tasks),
                    "current_tasks_preserved": sorted(output_tasks & current_tasks),
                    "current_user_from_frozen_session_matches": (
                        session_turns[code][str(page["source_turn_id"])].question == current_user
                    ),
                }
            )
    summary = []
    for variant in VARIANTS[1:]:
        selected = [row for row in rows if row["Add Prompt"] == VARIANT_LABELS[variant]]
        summary.append(
            {
                "Add Prompt": VARIANT_LABELS[variant],
                "Page count": len(selected),
                "Potential following-task dominance Pages": sum(
                    bool(row["potential_following_task_dominance"]) for row in selected
                ),
                "Mean current-user coverage": statistics.fmean(
                    float(row["current_user_bigram_coverage"]) for row in selected
                ),
                "Mean best-following-user coverage": statistics.fmean(
                    float(row["best_following_user_bigram_coverage"]) for row in selected
                ),
                "Pages with following-only task marker": sum(
                    bool(row["following_only_tasks_in_summary"]) for row in selected
                ),
            }
        )
    return rows, summary


def s002_summary_outputs(
    gold_rows: Sequence[Mapping[str, Any]], query_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    output = []
    for variant in GENERATED_VARIANTS:
        label = VARIANT_LABELS[variant]
        gold = [row for row in gold_rows if row["session_id"] == "S002" and row["候选 Add Prompt"] == label]
        queries = [row for row in query_rows if row["session_id"] == "S002" and row["候选 Add Prompt"] == label]
        promoted = [
            f"{row['query_id']}->{row['gold_source_turn_id']} ({row['before_rank']}→{row['after_rank']})"
            for row in gold
            if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5
        ]
        demoted = [
            f"{row['query_id']}->{row['gold_source_turn_id']} ({row['before_rank']}→{row['after_rank']})"
            for row in gold
            if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5
        ]
        output.append(
            {
                "候选 Add Prompt": label,
                "Promoted Gold": len(promoted),
                "Demoted Gold": len(demoted),
                "Net": len(promoted) - len(demoted),
                "Rescued Query": sum(row["transition"] == "RESCUED" for row in queries),
                "Hurt Query": sum(row["transition"] == "HURT" for row in queries),
                "Promoted Gold details": promoted,
                "Demoted Gold details": demoted,
            }
        )
    return output


def select_cases(query_rows: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    categories: dict[str, list[str]] = {}
    selected = []
    for variant in GENERATED_VARIANTS:
        label = VARIANT_LABELS[variant]
        rows = [row for row in query_rows if row["候选 Add Prompt"] == label]
        hurt = sorted(
            (row for row in rows if row["transition"] == "HURT"),
            key=lambda row: max(row["after_gold_ranks"].values()) - min(row["before_gold_ranks"].values()),
            reverse=True,
        )
        rescued = sorted(
            (row for row in rows if row["transition"] == "RESCUED"),
            key=lambda row: max(row["before_gold_ranks"].values()) - min(row["after_gold_ranks"].values()),
            reverse=True,
        )
        categories[f"当前前后文成功、{label}失败"] = [str(row["query_id"]) for row in hurt[:3]]
        categories[f"当前前后文失败、{label}救回"] = [str(row["query_id"]) for row in rescued[:3]]
    for values in categories.values():
        for query_id in values:
            if query_id not in selected:
                selected.append(query_id)
    return selected, categories


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    query_rows: Sequence[Mapping[str, Any]],
    token_rows: Sequence[Mapping[str, Any]],
    context_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selected, categories = select_cases(query_rows)
    queries = {
        str(query["query_id"]): dict(query, session_code=code)
        for code in SESSION_CODES
        for query in snapshots[code]["queries"]
    }
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in VARIANTS}
    maps = {
        variant: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()}
        for variant, by_query in rankings.items()
    }
    token_by = {(str(row["Add Prompt"]), str(row["source_turn_id"])): row for row in token_rows}
    context_by = {(str(row["候选 Add Prompt"]), str(row["source_turn_id"])): row for row in context_rows}
    cases = []
    lines = [
        "# Add 前后文 Prompt 候选 — 代表案例",
        "",
        "所有方案统一使用 Summary + Keywords Page 与冻结 P2 Search。",
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
            "session_id": query["session_code"],
            "query_id": query_id,
            "original_query": query["original_query"],
            "p2_query": p2_texts[query_id],
            "selection_categories": [name for name, values in categories.items() if query_id in values],
            "gold_pages": [],
        }
        lines.extend(
            [
                "",
                f"## {query_id}",
                "",
                "原始 Query：",
                "",
                "```text",
                str(query["original_query"]),
                "```",
                "",
                "P2 Query：",
                "",
                "```text",
                p2_texts[query_id],
                "```",
            ]
        )
        for gold_id in query["eligible_gold_page_ids"]:
            gold = str(gold_id)
            source_turn = str(maps["CurrentContext"][query_id][gold]["source_turn_id"])
            gold_case = {"gold_page_id": gold, "source_turn_id": source_turn, "variants": {}}
            lines.extend(["", f"### Gold Page {source_turn}", "", f"Page ID：`{gold}`"])
            for variant in VARIANTS:
                page = by_turn[variant][source_turn]
                item = maps[variant][query_id][gold]
                payload = {
                    "summary": page["summary"],
                    "keywords": page["keywords"],
                    "rank": int(item["rank"]),
                    "score": float(item["score"]),
                    "tokenizer": token_by[(VARIANT_LABELS[variant], source_turn)],
                }
                if variant in GENERATED_VARIANTS:
                    payload["context_turn_ids"] = context_by[(VARIANT_LABELS[variant], source_turn)]
                    diff = anchor_diff(
                        extended_anchors(
                            str(by_turn["CurrentContext"][source_turn]["summary"]),
                            by_turn["CurrentContext"][source_turn]["keywords"],
                        ),
                        extended_anchors(str(page["summary"]), page["keywords"]),
                    )
                    payload["anchors_vs_current_context"] = diff
                gold_case["variants"][VARIANT_LABELS[variant]] = payload
                lines.extend(
                    [
                        "",
                        f"#### {VARIANT_LABELS[variant]}",
                        "",
                        f"Gold rank / score：#{int(item['rank'])} / {float(item['score']):.9f}",
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
                        f"Summary truncated：{payload['tokenizer']['summary_truncated']}；"
                        f"Keywords：{payload['tokenizer']['keywords_truncation_status']}",
                    ]
                )
                if variant in GENERATED_VARIANTS:
                    lines.extend(
                        [
                            "",
                            "相对当前前后文基线的 anchors：",
                            "",
                            "```json",
                            json.dumps(payload["anchors_vs_current_context"], ensure_ascii=False, indent=2),
                            "```",
                        ]
                    )
            case["gold_pages"].append(gold_case)
        cases.append(case)
    return {"selection": categories, "cases": cases}, "\n".join(lines) + "\n"


def build_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    token_summary: Sequence[Mapping[str, Any]],
    anchor_summary: Sequence[Mapping[str, Any]],
    focus_summary: Sequence[Mapping[str, Any]],
    s002_summary: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# Add 前后文 Prompt 候选对照实验",
        "",
        "所有方案使用相同 eviction 前后文、Summary + Keywords Page formatter 和冻结 P2 Search。",
        "",
        "## 主结果（Top5 优先）",
        "",
        "| Add Prompt | Top5 | Macro Top5 | Top5 Gold 数 | MRR | Mean Gold Rank | Top10 | Top20 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Add 方式']} | {float(row['Top5']):.2%} | {float(row['Macro Top5']):.2%} | "
            f"{row['Top5 recalled Gold']} | {float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} | "
            f"{float(row['Top10']):.2%} | {float(row['Top20']):.2%} |"
        )
    lines.extend(
        [
            "",
            "## 相对当前前后文 Prompt 的 Top5 movement",
            "",
            "| 候选 Add Prompt | Promoted | Demoted | Net | Rescued Query | Hurt Query |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in comparisons:
        lines.append(
            f"| {row['候选 Add Prompt']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    session_lookup = {(str(row["Session"]), str(row["Add Prompt"])): float(row["Top5"]) for row in sessions}
    lines.extend(
        [
            "",
            "## Session Top5",
            "",
            "| Session | 当前前后文 | 候选一 | 候选二 | 候选三 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for code in SESSION_CODES:
        values = [session_lookup[(code, VARIANT_LABELS[variant])] for variant in VARIANTS[1:]]
        lines.append(f"| {code} | " + " | ".join(f"{value:.2%}" for value in values) + " |")
    lines.extend(
        [
            "",
            "### S002 Top5 movement",
            "",
            "| 候选 Add Prompt | Promoted | Demoted | Net | Rescued Query | Hurt Query |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in s002_summary:
        lines.append(
            f"| {row['候选 Add Prompt']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net']):+d} | {row['Rescued Query']} | {row['Hurt Query']} |"
        )
    lines.extend(
        [
            "",
            "## Summary / Keywords token budget",
            "",
            "| Add Prompt | Mean chars | Mean Summary tokens | Summary truncated | Keywords fully kept | Keywords partial | Keywords fully lost |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in token_summary:
        lines.append(
            f"| {row['Add Prompt']} | {float(row['Mean Summary chars']):.2f} | "
            f"{float(row['Mean Summary tokens']):.2f} | {row['Summary truncated Pages']} | "
            f"{row['Keywords fully kept Pages']} | {row['Keywords partially truncated Pages']} | "
            f"{row['Keywords fully truncated Pages']} |"
        )
    lines.extend(
        [
            "",
            "## Current-turn focus audit（不使用 Gold）",
            "",
            "该审计只比较 Summary 开头与当前 User/三个真实下文 User 的字符 bigram，并辅以 task marker；"
            "它用于定位人工案例，不等同于语义正确性判定。",
            "",
            "| Add Prompt | Potential following-task dominance Pages | Mean current coverage | Mean best-following coverage | Pages with following-only task marker |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in focus_summary:
        lines.append(
            f"| {row['Add Prompt']} | {row['Potential following-task dominance Pages']} | "
            f"{float(row['Mean current-user coverage']):.3f} | "
            f"{float(row['Mean best-following-user coverage']):.3f} | "
            f"{row['Pages with following-only task marker']} |"
        )
    metrics_by = {str(row["Add 方式"]): row for row in metrics}
    current = metrics_by[VARIANT_LABELS["CurrentContext"]]
    candidates = [metrics_by[VARIANT_LABELS[variant]] for variant in GENERATED_VARIANTS]
    best = max(candidates, key=lambda row: (float(row["Top5"]), float(row["Macro Top5"])))
    best_exceeds = float(best["Top5"]) > float(current["Top5"])
    lines.extend(
        [
            "",
            "## 客观判断",
            "",
            f"- 当前前后文基线 Top5：{float(current['Top5']):.2%}。",
            f"- 三个候选中 Top5 最高：{best['Add 方式']}，{float(best['Top5']):.2%}。",
            f"- 是否超过 38.31% 基线：{'是' if best_exceeds else '否'}。",
        ]
    )
    if not best_exceeds:
        lines.append("- 三个候选均未超过当前前后文方案；按预先规则保留当前前后文 Prompt 为最佳结果，不生成第四个候选。")
    else:
        better_sessions = [
            code
            for code in SESSION_CODES
            if session_lookup[(code, str(best["Add 方式"]))] > session_lookup[(code, VARIANT_LABELS["CurrentContext"])]
        ]
        lines.append(f"- 最佳候选发生 Session-level Top5 提升的 Session：{', '.join(better_sessions) or '无'}。")
    demotion_anchor = [
        row for row in anchor_summary if row["Gold movement"] == "DEMOTED" and row["Anchor direction"] == "LOST"
    ]
    if demotion_anchor:
        leading = sorted(demotion_anchor, key=lambda row: int(row["Anchor instances"]), reverse=True)[:8]
        lines.extend(
            [
                "- Demoted Gold Page 中候选相对当前基线丢失最多的 anchor 类别（按实例数）："
                + "；".join(
                    f"{row['候选 Add Prompt']} / {row['Anchor category']}={row['Anchor instances']}" for row in leading
                )
                + "。",
                "- Anchor audit 为 deterministic canonical diff，只用于定位人工案例，不将同义改写直接判为事实丢失。",
            ]
        )
    focus_by = {str(row["Add Prompt"]): row for row in focus_summary}
    lines.append(
        "- Potential following-task dominance Page 数："
        + "；".join(
            f"{VARIANT_LABELS[variant]}={focus_by[VARIANT_LABELS[variant]]['Potential following-task dominance Pages']}"
            for variant in VARIANTS[1:]
        )
        + "。"
    )
    lines.append(
        "- 代表案例 S003-Q078 的 Gold S003-Q066 中，候选二/三将当前的“表述审查”任务写成下文的“风险议题卡”；"
        "S004-Q056 中三个候选均删除了当前基线用于定位综合证据链的多指标/后续用途信息，Gold 从 #3 降至 #23/#38/#46。"
    )
    lines.append("- 本轮没有修改正式 Production Prompt、P2、Page formatter 或 benchmark，也没有自动生成下一版 Prompt。")
    return "\n".join(lines) + "\n"


async def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompts, prompt_hashes = freeze_prompts(args.output_dir)
    snapshots, old_pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    sessions_data = load_frozen_sessions(snapshots, old_pages)

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

    generated, context_rows, generation_metadata = {}, [], {}
    for variant in GENERATED_VARIANTS:
        generated[variant], rows, generation_metadata[variant] = await generate_candidate(
            args,
            variant,
            prompts[variant],
            prompt_hashes[variant],
            old_pages,
            sessions_data,
            llm_config,
        )
        context_rows.extend(rows)
    after_hashes = {
        variant: sha256_file(args.output_dir / "prompts" / PROMPT_FILES[variant])
        for variant in ("CurrentContext", *GENERATED_VARIANTS)
    }
    if after_hashes != prompt_hashes:
        raise AssertionError("Candidate Prompt SHA256 changed during generation")

    pages = build_pages(old_pages, generated)
    reference_vectors, reference_embedding_metadata = load_reference_vectors(pages)
    candidate_vectors, candidate_embedding_metadata, embedding_cache = encode_candidates(args.output_dir, pages)
    vectors = {**reference_vectors, **candidate_vectors}
    rankings = {
        variant: rank_configuration(snapshots, query_vectors["Search-P2"], vectors[variant]) for variant in VARIANTS
    }
    metrics, session_metrics, comparisons, gold_rows, query_rows, s002_rows = retrieval_outputs(snapshots, rankings)
    separations = [
        separation_row(
            snapshots,
            rankings[variant],
            add_label=VARIANT_LABELS[variant],
            formatter_label="Summary + Keywords",
        )
        for variant in VARIANTS
    ]
    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    token_rows, token_summary = tokenizer_audit(pages, embedder.model.tokenizer, int(embedder.model.max_seq_length))
    movement_anchors, anchor_summary = movement_anchor_outputs(pages, gold_rows)
    focus_rows, focus_summary = current_turn_focus_audit(pages, sessions_data)
    s002_summary = s002_summary_outputs(gold_rows, query_rows)
    cases_json, cases_markdown = representative_outputs(
        snapshots, p2_texts, pages, rankings, query_rows, token_rows, context_rows
    )

    metrics_by = {str(row["Add 方式"]): row for row in metrics}
    page_ids = {str(page["page_id"]) for page in old_pages}
    context_identity = {
        variant: {
            str(row["source_turn_id"]): (
                tuple(row["previous_context_turn_ids"]),
                tuple(row["following_context_turn_ids"]),
                str(row["eviction_trigger_turn_id"]),
            )
            for row in context_rows
            if row["候选 Add Prompt"] == VARIANT_LABELS[variant]
        }
        for variant in GENERATED_VARIANTS
    }
    validations = {
        "Current front/back no-User Top5 reproduction": {
            "status": "PASS"
            if math.isclose(float(metrics_by[VARIANT_LABELS["CurrentContext"]]["Top5"]), 0.38311688311688313)
            else "FAIL",
            "top5": metrics_by[VARIANT_LABELS["CurrentContext"]]["Top5"],
        },
        "Production no-User reference reproduction": {
            "status": "PASS"
            if math.isclose(float(metrics_by[VARIANT_LABELS["ProductionReference"]]["Top5"]), 0.35714285714285715)
            else "FAIL",
            "top5": metrics_by[VARIANT_LABELS["ProductionReference"]]["Top5"],
        },
        "Frozen counts and identity": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == 154
            and all({str(row["page_id"]) for row in pages[variant]} == page_ids for variant in VARIANTS)
            else "FAIL",
            "page_count": len(old_pages),
            "query_count": len(queries),
            "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        },
        "Frozen P2": {"status": "PASS" if len(p2_texts) == len(query_vectors["Search-P2"]) == 99 else "FAIL"},
        "Identical temporal context IDs": {
            "status": "PASS"
            if len(context_rows) == 999
            and all(value == context_identity[GENERATED_VARIANTS[0]] for value in context_identity.values())
            else "FAIL",
            "context_rows": len(context_rows),
        },
        "Eviction leakage": {
            "status": "PASS"
            if all(
                row["no_future_beyond_eviction_trigger"]
                and int(row["previous_context_count"]) <= 3
                and int(row["following_context_count"]) == 3
                for row in context_rows
            )
            else "FAIL"
        },
        "Prompt frozen": {"status": "PASS" if after_hashes == prompt_hashes else "FAIL"},
        "Summary Keywords formatter": {
            "status": "PASS"
            if all("\nUser:" not in str(row["no_user_text"]) for variant in VARIANTS for row in pages[variant])
            else "FAIL"
        },
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_metrics)
    write_csv(args.output_dir / "metrics/prompt_comparisons.csv", comparisons)
    write_csv(args.output_dir / "metrics/query_separation.csv", separations)
    write_csv(args.output_dir / "analysis/gold_rank_transitions.csv", gold_rows)
    write_csv(args.output_dir / "analysis/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/s002_transitions.csv", s002_rows)
    write_csv(args.output_dir / "analysis/token_truncation.csv", token_rows)
    write_csv(args.output_dir / "analysis/token_truncation_summary.csv", token_summary)
    write_csv(args.output_dir / "analysis/movement_anchor_audit.csv", movement_anchors)
    write_csv(args.output_dir / "analysis/movement_anchor_summary.csv", anchor_summary)
    write_csv(args.output_dir / "analysis/current_turn_focus_audit.csv", focus_rows)
    write_csv(args.output_dir / "analysis/current_turn_focus_summary.csv", focus_summary)
    write_csv(args.output_dir / "analysis/s002_summary.csv", s002_summary)
    write_jsonl(args.output_dir / "context/context_mapping.jsonl", context_rows)
    for variant in GENERATED_VARIANTS:
        write_jsonl(args.output_dir / f"pages/{variant}_pages.jsonl", pages[variant])
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        build_report(
            metrics,
            session_metrics,
            comparisons,
            token_summary,
            anchor_summary,
            focus_summary,
            s002_summary,
        ),
        encoding="utf-8",
    )

    metadata = {
        "experiment_name": "midterm_add_context_prompt_candidates",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "formatter": "<summary>\\nKeywords: <comma-space joined keywords>",
        "search": SEARCH_LABEL,
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "dimension": 512,
        "summary_model": llm_config["model"],
        "thinking_mode": "disabled",
        "temperature": float(llm_config["temperature"]),
        "top_p": float(llm_config["top_p"]),
        "top_k": int(llm_config["top_k"]),
        "prompt_sha256_before_first_llm_call": prompt_hashes,
        "prompt_sha256_after_generation": after_hashes,
        "all_prompts_frozen_before_first_llm_call": True,
        "generation": {VARIANT_LABELS[key]: value for key, value in generation_metadata.items()},
        "current_context_baseline_llm_calls": 0,
        "production_reference_llm_calls": 0,
        "search_p2_llm_calls": 0,
        "new_candidate_page_embeddings": {VARIANT_LABELS[key]: 333 for key in GENERATED_VARIANTS},
        "reference_embedding_metadata": reference_embedding_metadata,
        "candidate_embedding_metadata": candidate_embedding_metadata,
        "current_turn_focus_summary": focus_summary,
        "s002_summary": s002_summary,
        "input_contract": {
            "previous": "candidate-own latest up to 3 generated Page summaries + keywords",
            "current": "evicted source User + Assistant",
            "following": "exact next 3 QA turns active at eviction",
            "semantic_context_retrieval": False,
        },
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "query_changed": False,
        "production_prompt_changed": False,
        "validation": validations,
        "validation_all_pass": all(row["status"] == "PASS" for row in validations.values()),
        "automatic_fourth_candidate_generated": False,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics,
                "comparisons": comparisons,
                "token_summary": token_summary,
                "generation": metadata["generation"],
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
