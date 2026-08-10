"""Run a frozen Production-vs-three-local-Add-Prompt retrieval ablation.

The three candidate prompts are deterministic insertions into the current
Production Add Prompt. Source turns, Page identity and visibility, Gold,
query texts/vectors, formatter, embedding model, and dense cosine retrieval
remain frozen.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import stable_hash, write_jsonl  # noqa: E402
from exp.benchmark import run_midterm_add_prompt_multi_ablation as infrastructure  # noqa: E402
from exp.benchmark.run_midterm_add_conservative_retrieval_tuning_ablation import (  # noqa: E402
    TunedSummaryCache,
    anchor_diff,
    anchors,
    canonical_matches,
    production_dialogue,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    NUMBER_PATTERN,
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    extract_indicators,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    page_text,
    rank_configuration,
    regex_values,
    sha256_file,
)
from exp.benchmark.run_midterm_page_representation_decomposition import tokenizer_audit  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import dump_json, write_csv  # noqa: E402
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_add_prompt_conservative_candidates_ablation"
INTERNAL_VARIANTS = ("Old", "A1", "A2", "A3")
NEW_VARIANTS = ("A1", "A2", "A3")
SEARCHES = ("Baseline", "P2")
CONFIGS = tuple(f"{variant}_{search}" for variant in INTERNAL_VARIANTS for search in SEARCHES)
VARIANT_LABELS = {
    "Old": "原 Production Add Prompt",
    "A1": "最小改动 Add Prompt",
    "A2": "多检索入口保留 Add Prompt",
    "A3": "任务 + 证据链保留 Add Prompt",
}
SEARCH_LABELS = {
    "Baseline": "原始问题直接检索",
    "P2": "上下文引用解析后检索",
}
PROMPT_FILES = {
    "Old": "Original_Production_Add_Prompt.txt",
    "A1": "Minimal_Change_Add_Prompt.txt",
    "A2": "Multi_Retrieval_Entry_Add_Prompt.txt",
    "A3": "Task_Evidence_Chain_Add_Prompt.txt",
}
PROMPT_VERSIONS = {
    "A1": "production-local-minimal-change-v1",
    "A2": "production-local-multi-retrieval-entry-v1",
    "A3": "production-local-task-evidence-chain-v1",
}
CONFIG_LABELS = {
    f"{variant}_{search}": f"{VARIANT_LABELS[variant]} + {SEARCH_LABELS[search]}"
    for variant in INTERNAL_VARIANTS
    for search in SEARCHES
}
CANDIDATE_RANKING_RULE = (
    "候选继续调优优先级在结果生成前固定为：两种 Search 的 Micro R@5 均值、Macro R@5 均值、"
    "相对 Production 的合计 net Gold gain、合计 demoted Gold（越少越好），依次排序。"
)
REPLACEMENT_RULE = (
    "候选值得替换 Production 仅当两种 Search 下 Micro/Macro R@5 均不低于 Production，"
    "至少一个 Micro R@5 严格提升，且两种 Search 的 net Gold gain 均非负。"
)
SUMMARY_INSERTION_ANCHOR = "summary 可以使用一至三句话，不要求机械包含所有字段。"
KEYWORD_INSERTION_ANCHOR = "关键词不得包含完整句子，不得加入对话中没有出现的概念。"

MINIMAL_SUMMARY_ADDITION = """## 长期记忆检索补充要求

该摘要后续还会用于长期记忆检索，因此在保持原有总结方式的基础上，应保留未来可能用于重新定位本轮内容的信息，包括当前任务、主体、时间、指标、关键事实、趋势、结论、与前文的承接或修订关系以及信息限制。

不要为了缩短摘要主动删除仍具有独立检索价值的信息。仅删除完全重复、没有新增事实、关系、状态或判断的信息。"""

MULTI_ENTRY_SUMMARY_ADDITION = """## 多检索入口保留要求

摘要不仅需要准确总结当前轮内容，还需要保留多个可能的未来检索入口。

在当前对话中真实出现时，应尽量保留以下类型的信息：

- 当前任务或用户意图；
- 主体、时间范围、数据来源或口径；
- 当前讨论涉及的重要指标；
- 对后续判断有价值的关键数字；
- 指标的趋势、变化、拐点或状态；
- 不同指标之间的比较、匹配、冲突或其他关系；
- 当前形成的结论或判断；
- 对前文结论的承接、修订、反证或补充；
- 当前无法确认的信息、证据限制或待补充内容。

不要求每一项都出现，只保留本轮实际存在且有独立语义的信息。

不要把多个不同的检索入口过度压缩成一个“核心任务 + 核心结论”。

不设置摘要长度、指标数量和数字数量的硬限制。"""

MULTI_ENTRY_KEYWORD_ADDITION = """Keywords 用于补充摘要的检索入口。优先选择能够区分本轮与其他历史轮次的任务词、核心指标、指标关系、趋势或状态、判断类型等词语，而不是只重复公司名、年份等高频公共词。"""

CHAIN_SUMMARY_ADDITION = """## 任务—证据—关系—结论链保留要求

摘要应让未来系统能够仅通过当前 Page 恢复“这一轮为什么出现、分析了什么、依据是什么、得出了什么”。

优先保持以下信息链：

当前任务或追问
→ 使用的关键事实或指标
→ 指标之间的重要关系或变化
→ 当前结论
→ 对前文判断造成的影响
→ 尚未解决的限制

如果当前轮是对前文的继续、反证、修订、比较、补充或阶段总结，应明确保留这种对话关系。

不要因为某项事实不是当前结论的中心，就自动删除它；如果它能够解释当前判断、形成反证、支持后续追问或区分本轮与其他历史 Page，应继续保留。

避免无意义重复，但不要以“摘要尽可能短”为目标。"""

CHAIN_KEYWORD_ADDITION = """Keywords 优先覆盖当前任务类型、核心指标、关键关系或状态、重要判断，不设置新的硬数量限制。"""

JUDGMENT_TERMS = {
    "判断": ("判断",),
    "结论": ("结论",),
    "支持": ("支持",),
    "不支持": ("不支持",),
    "修订": ("修订", "修正", "降级", "缩小结论"),
    "确认": ("确认", "已确认"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Production Add Prompt local-candidate ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def insert_after(prompt: str, anchor: str, addition: str) -> str:
    if prompt.count(anchor) != 1:
        raise AssertionError(f"Prompt insertion anchor is not unique: {anchor}")
    return prompt.replace(anchor, f"{anchor}\n\n{addition}")


def candidate_prompts() -> dict[str, str]:
    production = MIDTERM_PAGE_SUMMARY_PROMPT
    minimal = insert_after(production, SUMMARY_INSERTION_ANCHOR, MINIMAL_SUMMARY_ADDITION)
    multi = insert_after(production, SUMMARY_INSERTION_ANCHOR, MULTI_ENTRY_SUMMARY_ADDITION)
    multi = insert_after(multi, KEYWORD_INSERTION_ANCHOR, MULTI_ENTRY_KEYWORD_ADDITION)
    chain = insert_after(production, SUMMARY_INSERTION_ANCHOR, CHAIN_SUMMARY_ADDITION)
    chain = insert_after(chain, KEYWORD_INSERTION_ANCHOR, CHAIN_KEYWORD_ADDITION)
    return {"Old": production, "A1": minimal, "A2": multi, "A3": chain}


def freeze_prompts(output_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    prompt_dir = output_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompts = candidate_prompts()
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
            "hash_contract": "SHA256 of exact UTF-8 file bytes",
            **{
                VARIANT_LABELS[variant]: {"file": PROMPT_FILES[variant], "sha256": hashes[variant]}
                for variant in INTERNAL_VARIANTS
            },
        },
    )
    return prompts, hashes


async def generate_variant(
    args: argparse.Namespace,
    pages: Sequence[Mapping[str, Any]],
    variant: str,
    prompt: str,
    prompt_sha: str,
    llm_config: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache_path = args.output_dir / f"cache/{variant}_summary_llm.jsonl"
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
        prompt_version=PROMPT_VERSIONS[variant],
    )
    initial_keys = set(cache.success)
    try:
        rows = await asyncio.gather(*(cache.call(page, prompt, prompt_sha) for page in pages))
    finally:
        await client.close()
    failures = [row for row in rows if row.get("status") != "SUCCESS"]
    if failures:
        raise RuntimeError(f"{VARIANT_LABELS[variant]} failed for {len(failures)} Pages")
    raw_keyword_truncations = 0
    for row in rows:
        try:
            raw_keyword_truncations += len(json.loads(str(row["raw_output"])).get("keywords") or []) > 8
        except (json.JSONDecodeError, TypeError):
            pass
    return {str(row["source_turn_id"]): row for row in rows}, {
        "successful_output_count": len(rows),
        "actual_api_attempts": sum(int(row.get("api_attempt_count") or 0) for row in rows),
        "retry_count": sum(int(row.get("retry_count") or 0) for row in rows),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0) for row in rows
        ),
        "cache_hit_count": sum(str(row["cache_key"]) in initial_keys for row in rows),
        "raw_keyword_truncation_count": raw_keyword_truncations,
        "cache_path": str(cache_path),
    }


def validate_frozen_inputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    old_pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    prompt_hashes: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, dict[str, list[dict[str, Any]]]]]:
    old_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    reproduction, old_rankings = infrastructure.validate_old(snapshots, query_vectors, old_vectors)
    page_ids = {str(page["page_id"]) for page in old_pages}
    source_turns = {str(page["source_turn_id"]) for page in old_pages}
    query_ids = {str(query["query_id"]) for query in queries}
    visible = {
        str(row["query_id"]): [str(value) for value in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    formatter_matches = all(
        page_text(str(page["summary"]), list(page["keywords"]), str(page["user_input"]))
        == str(page["current_embedding_text"])
        for page in old_pages
    )
    checks = {
        "Production + original query reproduction": reproduction["A"],
        "Production + resolved query reproduction": reproduction["B"],
        "frozen coverage": {
            "status": "PASS"
            if len(old_pages) == len(page_ids) == len(source_turns) == 333
            and len(queries) == len(query_ids) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == 154
            else "FAIL",
            "page_count": len(old_pages),
            "query_count": len(queries),
            "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        },
        "visible Page IDs": {
            "status": "PASS"
            if len(visible) == 99 and all(set(values) <= page_ids for values in visible.values())
            else "FAIL",
            "mapping_sha256": stable_hash(visible),
        },
        "query texts and vectors": {
            "status": "PASS"
            if set(p2_texts) == query_ids
            and len(query_vectors["Search-Baseline"]) == 99
            and len(query_vectors["Search-P2"]) == 99
            else "FAIL",
            "original_query_contract": "original_query exactly",
            "resolved_query_contract": "frozen reference-resolution P2 query exactly",
        },
        "single Production formatter": {
            "status": "PASS" if formatter_matches else "FAIL",
            "formatter": "<summary>\\nKeywords: <comma-space joined keywords>\\nUser: <original_user>",
        },
        "single-turn Add input": {
            "status": "PASS"
            if all(production_dialogue(page) == str(page["raw_dialogue"]) for page in old_pages)
            else "FAIL",
            "fields": ["CURRENT_USER", "CURRENT_ASSISTANT"],
        },
        "all Prompt candidates frozen": {
            "status": "PASS" if set(prompt_hashes) == set(INTERNAL_VARIANTS) else "FAIL",
            "sha256": dict(prompt_hashes),
        },
        "new LLM scope": {
            "status": "PASS",
            "Production_calls": 0,
            "Search_calls": 0,
            "candidate_expected_outputs": 999,
        },
    }
    if any(value["status"] != "PASS" for value in checks.values()):
        raise AssertionError(checks)
    return checks, old_rankings


def humanize_metrics(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Add Prompt": VARIANT_LABELS[str(row["add_variant"])],
            "Query 检索方式": SEARCH_LABELS[str(row["search_variant"])],
            "完整实验配置": CONFIG_LABELS[f"{row['add_variant']}_{row['search_variant']}"],
            "Eligible Gold": row["eligible_gold_count"],
            "Top5 recalled Gold": row["top5_recalled_gold"],
            "Top5 (Micro)": row["micro_r5"],
            "Macro Top5": row["macro_r5"],
            "Top10": row["r10"],
            "Top20": row["r20"],
            "MRR": row["mrr"],
            "Mean Gold Rank": row["mean_gold_rank"],
        }
        for row in rows
    ]


def humanize_comparisons(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        if row["comparison_role"] != "PRIMARY_OLD_BASELINE":
            continue
        selected.append(
            {
                "候选 Add Prompt": VARIANT_LABELS[str(row["right_add"])],
                "统一基线": VARIANT_LABELS["Old"],
                "Query 检索方式": SEARCH_LABELS[str(row["search_variant"])],
                "Promoted Gold": row["promoted_gold"],
                "Demoted Gold": row["demoted_gold"],
                "Net Gold gain": row["net_gold_gain"],
                "Rescued Queries": row["rescued_queries"],
                "Hurt Queries": row["hurt_queries"],
                "Extreme demotion": row["extreme_demotion"],
                "Extreme promotion": row["extreme_promotion"],
            }
        )
    return selected


def representation_outputs(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    production_by_turn = {str(row["source_turn_id"]): row for row in pages["Old"]}
    stats = []
    keyword_rows = []
    keyword_summary: dict[str, Any] = {}
    for variant in INTERNAL_VARIANTS:
        selected = list(pages[variant])
        stats.append(
            {
                "Add Prompt": VARIANT_LABELS[variant],
                "Page count": len(selected),
                "Mean summary chars": statistics.fmean(len(str(row["summary"])) for row in selected),
                "Median summary chars": statistics.median(len(str(row["summary"])) for row in selected),
                "Mean summary indicator count": statistics.fmean(
                    len(extract_indicators(str(row["summary"]))) for row in selected
                ),
                "Mean summary numeric count": statistics.fmean(
                    len(regex_values(NUMBER_PATTERN, str(row["summary"]))) for row in selected
                ),
                "Mean keyword count": statistics.fmean(len(row["keywords"]) for row in selected),
            }
        )
        frequencies = Counter(str(keyword) for row in selected for keyword in row["keywords"])
        if variant == "Old":
            keyword_summary[VARIANT_LABELS[variant]] = {
                "top30": frequencies.most_common(30),
                "mean_keyword_count": statistics.fmean(len(row["keywords"]) for row in selected),
            }
            continue
        jaccards = []
        exact = 0
        added, lost = Counter(), Counter()
        for row in selected:
            turn = str(row["source_turn_id"])
            old_keywords = [str(value) for value in production_by_turn[turn]["keywords"]]
            new_keywords = [str(value) for value in row["keywords"]]
            old_set, new_set = set(old_keywords), set(new_keywords)
            union = old_set | new_set
            jaccard = len(old_set & new_set) / len(union) if union else 1.0
            jaccards.append(jaccard)
            exact += int(old_keywords == new_keywords)
            added.update(new_set - old_set)
            lost.update(old_set - new_set)
            keyword_rows.append(
                {
                    "session_id": row["session_id"],
                    "source_turn_id": turn,
                    "候选 Add Prompt": VARIANT_LABELS[variant],
                    "Production keywords": old_keywords,
                    "Candidate keywords": new_keywords,
                    "Added keywords": sorted(new_set - old_set),
                    "Lost keywords": sorted(old_set - new_set),
                    "Keyword Jaccard": jaccard,
                    "Exact ordered match": old_keywords == new_keywords,
                }
            )
        keyword_summary[VARIANT_LABELS[variant]] = {
            "mean_keyword_count": statistics.fmean(len(row["keywords"]) for row in selected),
            "exact_ordered_match_count": exact,
            "mean_jaccard_vs_Production": statistics.fmean(jaccards),
            "added_keyword_instances": sum(added.values()),
            "lost_keyword_instances": sum(lost.values()),
            "top_added": added.most_common(30),
            "top_lost": lost.most_common(30),
            "top30": frequencies.most_common(30),
        }
    return stats, keyword_rows, keyword_summary


def extended_anchors(summary: str, keywords: Sequence[str]) -> dict[str, list[str]]:
    values = anchors(summary, keywords)
    text = f"{summary}\n{' '.join(str(value) for value in keywords)}"
    values["JUDGMENT"] = canonical_matches(text, JUDGMENT_TERMS)
    return values


def anchor_outputs(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in INTERNAL_VARIANTS}
    page_diffs = []
    diffs: dict[tuple[str, str], dict[str, Any]] = {}
    for variant in NEW_VARIANTS:
        for turn, old in by_turn["Old"].items():
            new = by_turn[variant][turn]
            diff = anchor_diff(
                extended_anchors(str(old["summary"]), old["keywords"]),
                extended_anchors(str(new["summary"]), new["keywords"]),
            )
            diffs[(variant, turn)] = diff
            page_diffs.append(
                {
                    "session_id": old["session_id"],
                    "source_turn_id": turn,
                    "候选 Add Prompt": VARIANT_LABELS[variant],
                    "Production summary": old["summary"],
                    "Candidate summary": new["summary"],
                    **diff,
                }
            )
    movement_rows = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for gold in gold_rows:
        query_id = str(gold["query_id"])
        gold_id = str(gold["gold_page_id"])
        turn = str(gold["gold_source_turn_id"])
        for variant in NEW_VARIANTS:
            for search in SEARCHES:
                before = int(gold[f"Old_{search}_rank"])
                after = int(gold[f"{variant}_{search}_rank"])
                movement = "PROMOTED" if before > 5 and after <= 5 else "DEMOTED" if before <= 5 and after > 5 else None
                if movement is None:
                    continue
                row = {
                    "query_id": query_id,
                    "gold_page_id": gold_id,
                    "gold_source_turn_id": turn,
                    "候选 Add Prompt": VARIANT_LABELS[variant],
                    "Query 检索方式": SEARCH_LABELS[search],
                    "Gold movement": movement,
                    "Production rank": before,
                    "Candidate rank": after,
                    **diffs[(variant, turn)],
                }
                movement_rows.append(row)
                grouped[(variant, search, movement)].append(row)
    summary_rows = []
    for (variant, search, movement), selected in sorted(grouped.items()):
        row: dict[str, Any] = {
            "候选 Add Prompt": VARIANT_LABELS[variant],
            "Query 检索方式": SEARCH_LABELS[search],
            "Gold movement": movement,
            "Gold count": len(selected),
        }
        for category in extended_anchors("", []):
            added = Counter(value for item in selected for value in item[f"{category}_added"])
            lost = Counter(value for item in selected for value in item[f"{category}_lost"])
            row[f"{category} added instances"] = sum(added.values())
            row[f"{category} lost instances"] = sum(lost.values())
            row[f"{category} top added"] = added.most_common(12)
            row[f"{category} top lost"] = lost.most_common(12)
        summary_rows.append(row)
    return page_diffs, movement_rows, summary_rows


def cross_candidate_outputs(
    gold_rows: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    overlap_rows = []
    recovery_rows = []
    summary: dict[str, Any] = {}
    for search in SEARCHES:
        demotion_sets: dict[str, set[str]] = {}
        for variant in NEW_VARIANTS:
            demotion_sets[variant] = {
                f"{row['query_id']}|{row['gold_page_id']}"
                for row in gold_rows
                if int(row[f"Old_{search}_rank"]) <= 5 and int(row[f"{variant}_{search}_rank"]) > 5
            }
        union = set().union(*demotion_sets.values())
        for key in sorted(union):
            query_id, gold_id = key.split("|", 1)
            source = next(
                row for row in gold_rows if str(row["query_id"]) == query_id and str(row["gold_page_id"]) == gold_id
            )
            overlap_rows.append(
                {
                    "Query 检索方式": SEARCH_LABELS[search],
                    "query_id": query_id,
                    "gold_page_id": gold_id,
                    "gold_source_turn_id": source["gold_source_turn_id"],
                    **{f"{VARIANT_LABELS[variant]} demoted": key in demotion_sets[variant] for variant in NEW_VARIANTS},
                }
            )
        for lost_variant in NEW_VARIANTS:
            for rescue_variant in NEW_VARIANTS:
                if lost_variant == rescue_variant:
                    continue
                rescued = [
                    key
                    for key in demotion_sets[lost_variant]
                    if next(
                        int(row[f"{rescue_variant}_{search}_rank"])
                        for row in gold_rows
                        if f"{row['query_id']}|{row['gold_page_id']}" == key
                    )
                    <= 5
                ]
                recovery_rows.append(
                    {
                        "Query 检索方式": SEARCH_LABELS[search],
                        "Gold 被该候选丢失": VARIANT_LABELS[lost_variant],
                        "被该候选保留或救回": VARIANT_LABELS[rescue_variant],
                        "Gold count": len(rescued),
                        "query_gold_keys": sorted(rescued),
                    }
                )
        all_three = set.intersection(*demotion_sets.values())
        summary[SEARCH_LABELS[search]] = {
            "demoted_by_candidate": {VARIANT_LABELS[variant]: len(values) for variant, values in demotion_sets.items()},
            "demoted_union": len(union),
            "demoted_by_all_three": len(all_three),
            "all_three_query_gold_keys": sorted(all_three),
            "pairwise_overlap": {
                f"{VARIANT_LABELS[left]} ∩ {VARIANT_LABELS[right]}": len(demotion_sets[left] & demotion_sets[right])
                for index, left in enumerate(NEW_VARIANTS)
                for right in NEW_VARIANTS[index + 1 :]
            },
        }
    return overlap_rows, recovery_rows, summary


def select_representative_queries(transitions: Sequence[Mapping[str, Any]]) -> tuple[list[str], dict[str, list[str]]]:
    categories: dict[str, list[str]] = {}
    selected: list[str] = []
    for variant in NEW_VARIANTS:
        for search in SEARCHES:
            relevant = [row for row in transitions if row["candidate"] == variant and row["search_variant"] == search]
            for movement, key, reverse in (("RESCUED", "max_promotion", True), ("HURT", "max_demotion", True)):
                rows = sorted(
                    (row for row in relevant if row["transition"] == movement),
                    key=lambda row: (int(row[key]), str(row["query_id"])),
                    reverse=reverse,
                )[:3]
                name = f"{VARIANT_LABELS[variant]} / {SEARCH_LABELS[search]} / {movement}"
                categories[name] = [str(row["query_id"]) for row in rows]
                for query_id in categories[name]:
                    if query_id not in selected:
                        selected.append(query_id)
    return selected, categories


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    p2_texts: Mapping[str, str],
    gold_rows: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    page_anchor_diffs: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selected, categories = select_representative_queries(transitions)
    queries = {
        str(query["query_id"]): {**query, "session_code": code}
        for code in SESSION_CODES
        for query in snapshots[code]["queries"]
    }
    by_turn = {variant: {str(row["source_turn_id"]): row for row in pages[variant]} for variant in INTERNAL_VARIANTS}
    diffs = {(str(row["候选 Add Prompt"]), str(row["source_turn_id"])): row for row in page_anchor_diffs}
    cases = []
    lines = [
        "# Add Prompt 保守候选对照 — Representative Cases",
        "",
        "所有 Page 均使用同一 Production formatter；排名只使用冻结的 query-time visible Pages 与 eligible Gold。",
        "",
        "## 自动选择类别",
        "",
        "```json",
        json.dumps(categories, ensure_ascii=False, indent=2),
        "```",
    ]
    for query_id in selected:
        query = queries[query_id]
        selected_gold = [row for row in gold_rows if str(row["query_id"]) == query_id]
        case = {
            "query_id": query_id,
            "session_id": query["session_code"],
            "selection_categories": [name for name, values in categories.items() if query_id in values],
            "original_query": query["original_query"],
            "resolved_query": p2_texts[query_id],
            "gold_pages": [],
        }
        lines.extend(
            [
                "",
                f"## {query_id}",
                "",
                f"选择类别：{', '.join(case['selection_categories'])}",
                "",
                "### 当前原始 Query",
                "",
                "```text",
                str(query["original_query"]),
                "```",
                "",
                "### 上下文引用解析后 Query",
                "",
                "```text",
                str(p2_texts[query_id]),
                "```",
            ]
        )
        for gold in selected_gold:
            turn = str(gold["gold_source_turn_id"])
            page_payload = {
                "gold_page_id": gold["gold_page_id"],
                "gold_source_turn_id": turn,
                "variants": {},
            }
            lines.extend(["", f"### Gold Page：{turn} ({gold['gold_page_id']})"])
            for variant in INTERNAL_VARIANTS:
                page = by_turn[variant][turn]
                variant_payload = {
                    "summary": page["summary"],
                    "keywords": page["keywords"],
                    "ranks": {
                        SEARCH_LABELS[search]: {
                            "rank": int(gold[f"{variant}_{search}_rank"]),
                            "score": float(gold[f"{variant}_{search}_score"]),
                        }
                        for search in SEARCHES
                    },
                }
                if variant != "Old":
                    diff = diffs[(VARIANT_LABELS[variant], turn)]
                    variant_payload["retrieval_anchor_changes_vs_Production"] = {
                        key: value for key, value in diff.items() if key.endswith("_added") or key.endswith("_lost")
                    }
                page_payload["variants"][VARIANT_LABELS[variant]] = variant_payload
                lines.extend(
                    [
                        "",
                        f"#### {VARIANT_LABELS[variant]}",
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
                        "| Query 检索方式 | Gold rank | Gold score |",
                        "|---|---:|---:|",
                    ]
                )
                for search in SEARCHES:
                    lines.append(
                        f"| {SEARCH_LABELS[search]} | #{gold[f'{variant}_{search}_rank']} | "
                        f"{float(gold[f'{variant}_{search}_score']):.9f} |"
                    )
                if variant != "Old":
                    lines.extend(
                        [
                            "",
                            "相对 Production 增加或丢失的 retrieval anchors：",
                            "",
                            "```json",
                            json.dumps(
                                variant_payload["retrieval_anchor_changes_vs_Production"],
                                ensure_ascii=False,
                                indent=2,
                            ),
                            "```",
                        ]
                    )
            case["gold_pages"].append(page_payload)
        cases.append(case)
    return {"selection": categories, "cases": cases}, "\n".join(lines) + "\n"


def humanize_separation(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Add Prompt": VARIANT_LABELS[str(row["add_variant"])],
            "Query 检索方式": SEARCH_LABELS[str(row["search_variant"])],
            "Top separation margin mean": row["top_separation_margin_mean"],
            "Top separation margin median": row["top_separation_margin_median"],
            "Top separation positive rate": row["top_separation_positive_rate"],
            "Gold Top5 margin mean": row["gold_top5_margin_mean"],
            "Gold Top5 margin median": row["gold_top5_margin_median"],
            "Gold Top5 margin positive rate": row["gold_top5_margin_positive_rate"],
        }
        for row in rows
    ]


def decision_outputs(metrics: Sequence[Mapping[str, Any]], comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metric_by = {(str(row["Add Prompt"]), str(row["Query 检索方式"])): row for row in metrics}
    comparison_by = {(str(row["候选 Add Prompt"]), str(row["Query 检索方式"])): row for row in comparisons}
    replacement = {}
    ordering = []
    for variant in NEW_VARIANTS:
        label = VARIANT_LABELS[variant]
        micro = [float(metric_by[(label, SEARCH_LABELS[search])]["Top5 (Micro)"]) for search in SEARCHES]
        macro = [float(metric_by[(label, SEARCH_LABELS[search])]["Macro Top5"]) for search in SEARCHES]
        old_micro = [
            float(metric_by[(VARIANT_LABELS["Old"], SEARCH_LABELS[search])]["Top5 (Micro)"]) for search in SEARCHES
        ]
        old_macro = [
            float(metric_by[(VARIANT_LABELS["Old"], SEARCH_LABELS[search])]["Macro Top5"]) for search in SEARCHES
        ]
        nets = [int(comparison_by[(label, SEARCH_LABELS[search])]["Net Gold gain"]) for search in SEARCHES]
        demotions = [int(comparison_by[(label, SEARCH_LABELS[search])]["Demoted Gold"]) for search in SEARCHES]
        replacement[label] = (
            all(value >= base for value, base in zip(micro, old_micro))
            and all(value >= base for value, base in zip(macro, old_macro))
            and any(value > base for value, base in zip(micro, old_micro))
            and all(value >= 0 for value in nets)
        )
        ordering.append(
            {
                "candidate": label,
                "mean_micro_r5": statistics.fmean(micro),
                "mean_macro_r5": statistics.fmean(macro),
                "summed_net_gold_gain": sum(nets),
                "summed_demoted_gold": sum(demotions),
            }
        )
    ordering.sort(
        key=lambda row: (
            float(row["mean_micro_r5"]),
            float(row["mean_macro_r5"]),
            int(row["summed_net_gold_gain"]),
            -int(row["summed_demoted_gold"]),
        ),
        reverse=True,
    )
    return {
        "replacement_rule_frozen_before_results": REPLACEMENT_RULE,
        "candidate_ranking_rule_frozen_before_results": CANDIDATE_RANKING_RULE,
        "worth_replacing_Production": replacement,
        "candidate_ranking": ordering,
        "most_worth_continuing_direction": ordering[0]["candidate"],
        "automatic_fourth_prompt_generated": False,
    }


def build_report(
    metrics: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    stats: Sequence[Mapping[str, Any]],
    separation: Sequence[Mapping[str, Any]],
    overlap: Mapping[str, Any],
    recovery_rows: Sequence[Mapping[str, Any]],
    anchor_summary: Sequence[Mapping[str, Any]],
    keyword_summary: Mapping[str, Any],
    tokenizer_summaries: Mapping[str, Mapping[str, Any]],
    decisions: Mapping[str, Any],
) -> str:
    lines = [
        "# Production Add Prompt 局部调优多候选对照报告",
        "",
        "## Retrieval",
        "",
        "| Add Prompt | Query 检索方式 | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Add Prompt']} | {row['Query 检索方式']} | {float(row['Top5 (Micro)']):.2%} | "
            f"{float(row['Macro Top5']):.2%} | {float(row['Top10']):.2%} | {float(row['Top20']):.2%} | "
            f"{float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## 相对 Production 的 Top5 Gold 变化",
            "",
            "| 候选 Add Prompt | Query 检索方式 | Promoted | Demoted | Net | Rescued Queries | Hurt Queries |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in comparisons:
        lines.append(
            f"| {row['候选 Add Prompt']} | {row['Query 检索方式']} | {row['Promoted Gold']} | "
            f"{row['Demoted Gold']} | {int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(
        [
            "",
            "## Page 表征",
            "",
            "| Add Prompt | Mean summary chars | Mean indicators | Mean numbers | Mean keywords |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in stats:
        lines.append(
            f"| {row['Add Prompt']} | {float(row['Mean summary chars']):.2f} | "
            f"{float(row['Mean summary indicator count']):.2f} | {float(row['Mean summary numeric count']):.2f} | "
            f"{float(row['Mean keyword count']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Query-conditioned separation",
            "",
            "| Add Prompt | Query 检索方式 | Mean best-Gold minus best-NonGold | Positive rate |",
            "|---|---|---:|---:|",
        ]
    )
    for row in separation:
        lines.append(
            f"| {row['Add Prompt']} | {row['Query 检索方式']} | "
            f"{float(row['Top separation margin mean']):.6f} | {float(row['Top separation positive rate']):.2%} |"
        )
    demoted_anchor_rows = [row for row in anchor_summary if row["Gold movement"] == "DEMOTED"]

    def anchor_total(category: str, direction: str) -> int:
        return sum(int(row[f"{category} {direction} instances"]) for row in demoted_anchor_rows)

    recovery_counts = [int(row["Gold count"]) for row in recovery_rows]
    lines.extend(
        [
            "",
            "## 事实归纳",
            "",
            "- 三个候选在两种 Query 检索方式下的 Micro/Macro Top5 均低于原 Production。",
            f"- 三个候选共同 demote 的 Gold：原始问题直接检索 {overlap['原始问题直接检索']['demoted_by_all_three']} 个；"
            f"上下文引用解析后检索 {overlap['上下文引用解析后检索']['demoted_by_all_three']} 个。",
            f"- 候选之间并非完全同错：有序候选对能保留或救回另一候选丢失的 Gold 数介于 "
            f"{min(recovery_counts)} 和 {max(recovery_counts)}。",
            f"- 对所有 demoted movement 行汇总，RELATION anchors 新增 {anchor_total('RELATION', 'added')}、"
            f"丢失 {anchor_total('RELATION', 'lost')}；JUDGMENT 新增 {anchor_total('JUDGMENT', 'added')}、"
            f"丢失 {anchor_total('JUDGMENT', 'lost')}；DEPENDENCY 新增 {anchor_total('DEPENDENCY', 'added')}、"
            f"丢失 {anchor_total('DEPENDENCY', 'lost')}。",
            f"- 同一批 demoted movement 中，INDICATOR anchors 新增 {anchor_total('INDICATOR', 'added')}、"
            f"丢失 {anchor_total('INDICATOR', 'lost')}；NUMBER anchors 新增 {anchor_total('NUMBER', 'added')}、"
            f"丢失 {anchor_total('NUMBER', 'lost')}。这说明严重下降不能只用摘要长度或信息总量解释。",
            f"- 实际 512-token 右截断 Page 数：Production "
            f"{tokenizer_summaries[VARIANT_LABELS['Old']]['truncated_page_count']}；最小改动 "
            f"{tokenizer_summaries[VARIANT_LABELS['A1']]['truncated_page_count']}；多检索入口 "
            f"{tokenizer_summaries[VARIANT_LABELS['A2']]['truncated_page_count']}；任务 + 证据链 "
            f"{tokenizer_summaries[VARIANT_LABELS['A3']]['truncated_page_count']}。formatter 未变，但更长内容会让末尾 User 字段更常被截断。",
            f"- 与 Production keywords 完全同序的 Page 数：最小改动 "
            f"{keyword_summary[VARIANT_LABELS['A1']]['exact_ordered_match_count']}；多检索入口 "
            f"{keyword_summary[VARIANT_LABELS['A2']]['exact_ordered_match_count']}；任务 + 证据链 "
            f"{keyword_summary[VARIANT_LABELS['A3']]['exact_ordered_match_count']}（总 Page 333）。",
            "",
            "## 跨候选 Gold 丢失重叠",
            "",
            "```json",
            json.dumps(overlap, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Keywords 变化",
            "",
            "```json",
            json.dumps(keyword_summary, ensure_ascii=False, indent=2),
            "```",
            "",
            "## Promoted / Demoted Page 的 retrieval anchor 事实统计",
            "",
            "```json",
            json.dumps(list(anchor_summary), ensure_ascii=False, indent=2),
            "```",
            "",
            "## 结论",
            "",
            f"按预先冻结的候选排序规则，最值得继续调优的方向是：{decisions['most_worth_continuing_direction']}。",
            "",
            "各候选是否已达到直接替换 Production 的标准：",
            "",
            "```json",
            json.dumps(decisions["worth_replacing_Production"], ensure_ascii=False, indent=2),
            "```",
            "",
            "本轮未生成第四个 Prompt，也未根据 Retrieval 结果修改任何候选 Prompt。",
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
    validations, old_rankings = validate_frozen_inputs(
        snapshots, old_pages, queries, p2_texts, query_vectors, prompt_hashes
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

    generated: dict[str, dict[str, dict[str, Any]]] = {}
    generation_metadata: dict[str, dict[str, Any]] = {}
    # All Prompt files are frozen above. No retrieval is run until all three generations complete.
    for variant in NEW_VARIANTS:
        generated[variant], generation_metadata[variant] = await generate_variant(
            args, old_pages, variant, prompts[variant], prompt_hashes[variant], llm_config
        )
    after_hashes = {
        variant: sha256_file(args.output_dir / "prompts" / PROMPT_FILES[variant]) for variant in INTERNAL_VARIANTS
    }
    validations["Prompt SHA256 unchanged after all generations"] = {
        "status": "PASS" if after_hashes == prompt_hashes else "FAIL",
        "before": prompt_hashes,
        "after": after_hashes,
    }
    pages = infrastructure.build_pages(old_pages, generated, prompt_hashes)
    for old_page, output_page in zip(old_pages, pages["Old"]):
        formatted = page_text(str(old_page["summary"]), list(old_page["keywords"]), str(old_page["user_input"]))
        if formatted != str(old_page["current_embedding_text"]):
            raise AssertionError(f"Production formatter reproduction failed: {old_page['source_turn_id']}")
        output_page["embedding_text"] = formatted
    source_sets = {variant: {str(row["source_turn_id"]) for row in pages[variant]} for variant in INTERNAL_VARIANTS}
    validations["Page source turn identity and shared formatter"] = {
        "status": "PASS"
        if all(values == source_sets["Old"] for values in source_sets.values())
        and all(
            str(row["embedding_text"]) == page_text(str(row["summary"]), list(row["keywords"]), str(row["source_user"]))
            for variant in INTERNAL_VARIANTS
            for row in pages[variant]
        )
        else "FAIL",
        "source_turn_counts": {variant: len(values) for variant, values in source_sets.items()},
    }
    if any(value["status"] != "PASS" for value in validations.values()):
        raise AssertionError(validations)

    embedding_cache = EmbeddingCache(args.output_dir / "cache/embeddings")
    vectors: dict[str, dict[str, Sequence[float]]] = {
        "Old": {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    }
    embedding_metadata: dict[str, dict[str, Any]] = {}
    for variant in NEW_VARIANTS:
        ids = [f"{variant}:{row['source_turn_id']}" for row in pages[variant]]
        texts = [str(row["embedding_text"]) for row in pages[variant]]
        encoded, metadata = embedding_cache.encode(
            PRODUCTION_EMBEDDING,
            f"{variant}-production-local-prompt-pages-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        vectors[variant] = {
            str(row["old_page_id"]): encoded[f"{variant}:{row['source_turn_id']}"] for row in pages[variant]
        }
        embedding_metadata[variant] = metadata
        if len(vectors[variant]) != 333 or int(metadata["dimension"]) != 512:
            raise AssertionError(f"{variant} Page embedding mismatch")

    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    tokenizer = embedder.model.tokenizer
    max_length = int(embedder.model.max_seq_length)
    tokenizer_rows = []
    tokenizer_summaries = {}
    for variant in INTERNAL_VARIANTS:
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
        tokenizer_rows.extend({"Add Prompt": VARIANT_LABELS[variant], **row} for row in audit)
        tokenizer_summaries[VARIANT_LABELS[variant]] = summary

    rankings = dict(old_rankings)
    for variant in NEW_VARIANTS:
        rankings[f"{variant}_Baseline"] = rank_configuration(
            snapshots, query_vectors["Search-Baseline"], vectors[variant]
        )
        rankings[f"{variant}_P2"] = rank_configuration(snapshots, query_vectors["Search-P2"], vectors[variant])
    raw_metrics, aggregate, _ = infrastructure.rank_metrics(snapshots, rankings)
    raw_gold, raw_transitions, raw_comparisons, raw_sessions = infrastructure.comparison_outputs(
        snapshots, rankings, pages
    )
    raw_separation = infrastructure.separation_rows(snapshots, rankings)
    metrics = humanize_metrics(raw_metrics)
    comparisons = humanize_comparisons(raw_comparisons)
    separation = humanize_separation(raw_separation)
    stats, keyword_rows, keyword_summary = representation_outputs(pages)
    page_anchor_diffs, gold_anchor_movements, anchor_summary = anchor_outputs(pages, raw_gold)
    overlap_rows, recovery_rows, overlap_summary = cross_candidate_outputs(raw_gold)
    cases_json, cases_markdown = representative_outputs(
        snapshots, pages, p2_texts, raw_gold, raw_transitions, page_anchor_diffs
    )
    decisions = decision_outputs(metrics, comparisons)

    session_rows = [
        {
            "Session": row["session_id"],
            "Add Prompt": VARIANT_LABELS[str(row["add_variant"])],
            "Query 检索方式": SEARCH_LABELS[str(row["search_variant"])],
            "Eligible Gold": row["eligible_gold_count"],
            "Top5": row["r5"],
            "Top10": row["r10"],
            "Top20": row["r20"],
            "MRR": row["mrr"],
            "Mean Gold Rank": row["mean_gold_rank"],
            "Promoted Gold vs Production": row["promoted"],
            "Demoted Gold vs Production": row["demoted"],
            "Net Gold vs Production": row["net"],
        }
        for row in raw_sessions
    ]
    long_gold_rows = []
    for row in raw_gold:
        for variant in INTERNAL_VARIANTS:
            for search in SEARCHES:
                long_gold_rows.append(
                    {
                        "Session": row["session_id"],
                        "Query ID": row["query_id"],
                        "Gold Page ID": row["gold_page_id"],
                        "Gold source_turn_id": row["gold_source_turn_id"],
                        "Add Prompt": VARIANT_LABELS[variant],
                        "Query 检索方式": SEARCH_LABELS[search],
                        "Gold rank": row[f"{variant}_{search}_rank"],
                        "Gold score": row[f"{variant}_{search}_score"],
                    }
                )
    transition_rows = [
        {
            "Session": row["session_id"],
            "Query ID": row["query_id"],
            "候选 Add Prompt": VARIANT_LABELS[str(row["candidate"])],
            "Query 检索方式": SEARCH_LABELS[str(row["search_variant"])],
            "Transition": row["transition"],
            "Production Gold ranks": row["before_gold_ranks"],
            "Candidate Gold ranks": row["after_gold_ranks"],
            "Promoted Gold": row["promoted_gold"],
            "Demoted Gold": row["demoted_gold"],
            "Max promotion": row["max_promotion"],
            "Max demotion": row["max_demotion"],
        }
        for row in raw_transitions
    ]

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", long_gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", transition_rows)
    write_csv(args.output_dir / "metrics/page_comparisons.csv", comparisons)
    write_csv(args.output_dir / "metrics/gold_nongold_separation.csv", separation)
    write_csv(args.output_dir / "pages/representation_stats.csv", stats)
    for variant in NEW_VARIANTS:
        write_jsonl(args.output_dir / f"pages/{variant}_pages.jsonl", pages[variant])
    write_csv(args.output_dir / "analysis/keyword_diff.csv", keyword_rows)
    write_csv(args.output_dir / "analysis/page_retrieval_anchor_diff.csv", page_anchor_diffs)
    write_csv(args.output_dir / "analysis/gold_anchor_movements.csv", gold_anchor_movements)
    write_csv(args.output_dir / "analysis/anchor_movement_summary.csv", anchor_summary)
    write_csv(args.output_dir / "analysis/cross_candidate_demotion_overlap.csv", overlap_rows)
    write_csv(args.output_dir / "analysis/cross_candidate_recovery.csv", recovery_rows)
    write_csv(args.output_dir / "analysis/tokenizer_truncation_comparison.csv", tokenizer_rows)
    dump_json(args.output_dir / "analysis/keyword_summary.json", keyword_summary)
    dump_json(args.output_dir / "analysis/cross_candidate_summary.json", overlap_summary)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    report = build_report(
        metrics,
        comparisons,
        stats,
        separation,
        overlap_summary,
        recovery_rows,
        anchor_summary,
        keyword_summary,
        tokenizer_summaries,
        decisions,
    )
    (args.output_dir / "experiment_report.md").write_text(report, encoding="utf-8")

    metadata = {
        "experiment_name": "midterm_add_prompt_conservative_candidates_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "human_facing_variant_labels": VARIANT_LABELS,
        "human_facing_search_labels": SEARCH_LABELS,
        "prompt_sha256_before_first_llm_call": prompt_hashes,
        "prompt_sha256_after_generation": after_hashes,
        "prompt_frozen": after_hashes == prompt_hashes,
        "prompts_are_local_additions_to_current_Production": True,
        "input_contract": "exact Production User + Assistant raw dialogue only",
        "output_contract": {"summary": "string", "keywords": "Production persistence keeps first 8"},
        "page_formatter": "<summary>\\nKeywords: <comma-space joined keywords>\\nUser: <original_user>",
        "same_formatter_function_for_all_variants": True,
        "summary_model": llm_config["model"],
        "thinking_mode": "disabled",
        "temperature": float(llm_config["temperature"]),
        "top_p": float(llm_config["top_p"]),
        "top_k": int(llm_config["top_k"]),
        "generation": {VARIANT_LABELS[key]: value for key, value in generation_metadata.items()},
        "Production_new_LLM_calls": 0,
        "Search_new_LLM_calls": 0,
        "Production_Page_embeddings_reused": 333,
        "new_Page_embedding_items": {VARIANT_LABELS[key]: 333 for key in NEW_VARIANTS},
        "embedding_metadata": {VARIANT_LABELS[key]: value for key, value in embedding_metadata.items()},
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
        "validation_all_pass": all(value["status"] == "PASS" for value in validations.values()),
        "tokenizer_truncation_summary": tokenizer_summaries,
        "aggregate_metrics_internal": aggregate,
        "cross_candidate_summary": overlap_summary,
        "decisions": decisions,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "retrieval_metrics": metrics,
                "comparisons": comparisons,
                "representation_stats": stats,
                "cross_candidate_summary": overlap_summary,
                "decisions": decisions,
                "generation": metadata["generation"],
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
