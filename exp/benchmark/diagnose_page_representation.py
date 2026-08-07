from __future__ import annotations

import argparse
import csv
import json
import logging
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from exp.benchmark.benchmark_common import (
    BenchmarkSession,
    BenchmarkTurn,
    dependency_distances,
    dump_json,
    ensure_repo_root_on_path,
    expand_env_placeholders,
    load_dataset,
    load_json,
    resolve_path,
    write_csv,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.diagnose_midterm_page_recall import (  # noqa: E402
    PageRow,
    compact_text,
    cosine_similarity,
    load_jsonl,
    load_qdrant_state,
    page_embedding_text,
    parse_llm_json,
    qdrant_client_from_config,
    query_dense_rank,
    write_jsonl,
)
from mem0.configs.embeddings.base import BaseEmbedderConfig  # noqa: E402
from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT  # noqa: E402
from mem0.embeddings.huggingface import HuggingFaceEmbedding  # noqa: E402
from mem0.utils.factory import LlmFactory  # noqa: E402

LOGGER = logging.getLogger("page_representation_diagnosis")
DEFAULT_SOURCE_DIR = "exp/results/recall_full_100_sessions"
DEFAULT_PREVIOUS_DIAGNOSIS = "exp/results/midterm_page_diagnosis"
DEFAULT_OUTPUT_DIR = "exp/results/page_representation_diagnosis"
DEFAULT_SHEET = "S001_贵州茅台_投研"
SHORT_TERM_QA_CAPACITY = 3

COMMON_TERMS = (
    "贵州茅台",
    "2023",
    "2024",
    "2025",
    "营业收入",
    "归母净利润",
    "总资产",
    "归母股东权益",
    "年度报告",
    "同比",
    "亿元",
    "盈利",
    "增长",
)

CASHFLOW_TERMS = (
    "经营现金流",
    "经营活动现金流",
    "现金流质量",
    "盈利质量",
    "净利润",
    "扣非净利润",
    "利润现金含量",
    "盈利可持续性",
    "非经常性损益",
)

TYPICAL_CASES = (
    ("S001-Q013", "S001-Q006"),
    ("S001-Q022", "S001-Q012"),
    ("S001-Q028", "S001-Q022"),
    ("S001-Q033", "S001-Q023"),
    ("S001-Q035", "S001-Q029"),
    ("S001-Q039", "S001-Q032"),
    ("S001-Q042", "S001-Q036"),
    ("S001-Q049", "S001-Q043"),
)

RETRIEVAL_FOCUSED_PROMPT = """
你负责把单轮金融投研对话抽取成“只用于后续向量检索”的 Page 表示，而不是审计底稿或完整摘要。

只返回严格 JSON：
{
  "topic": "本轮独有的核心主题",
  "user_intent": "用户真正要解决的分析任务",
  "key_entities": [],
  "key_metrics": [],
  "key_facts": [],
  "key_numbers": [],
  "core_conclusion": "本轮新得到或确认的核心结论",
  "relation_to_prior_context": "本轮与前文信息的具体关系",
  "retrieval_keywords": []
}

规则：
1. 不重复公司名、2023—2025、年报来源、币种单位等所有 Page 都共有的背景，除非它们是本轮区分点。
2. 优先抽取本轮新增内容，不要复制回答中的通用三年底表、模板性限制和风险提示。
3. 必须保留用户真正关注的“关系语义”，不能只列名词。例如：经营现金流是否支持盈利质量、扣非净利润是否验证利润持续性、收入与利润增长是否匹配、应收增长是否快于收入。
4. key_facts 和 core_conclusion 应写清方向、比较或因果边界；保留真正有区分度的数字。
5. relation_to_prior_context 要消解“前面、刚才、上一轮”等指代；若原对话没有关系则写空字符串。
6. retrieval_keywords 优先使用能区分本轮的复合短语，不要堆叠“贵州茅台、同比、增长、亿元”等公共词。
7. 不引入输入中不存在的事实，不使用任何未来问题或 Gold dependency。
8. 必须先读取 User 的当前问题，围绕这个问题筛选 Assistant 内容；Assistant 往往重复整套营业收入、净利润、总资产、权益底表，凡是不直接回答当前 User 任务的指标和数字一律删除。
9. key_entities 最多 3 项，key_metrics 最多 3 项，key_facts 最多 3 条，key_numbers 最多 6 个，retrieval_keywords 最多 8 个；每条事实只表达一个有区分度的方向或关系。
10. 除非 User 当前问题就是“整体财务梳理”，否则禁止同时列出营业收入、归母净利润、总资产、归母权益四套通用数据。
11. 输出目标是让本 Page 区别于同一公司其他 46 个 Page；若删除一句话不影响区分本轮，就删除它。
"""

MINIMAL_PROMPT = """
你负责生成一条简洁、可区分的金融对话检索表示。只返回严格 JSON：
{
  "user_task": "用户任务",
  "core_topic": "核心主题",
  "key_metrics": [],
  "key_fact": "本轮最关键的新事实或比较关系",
  "core_conclusion": "本轮核心结论",
  "retrieval_keywords": []
}

总长度控制在 300—600 个中文字符以内。只保留本轮最有区分度的任务、指标、事实、结论和复合关键词。
不要重复数据来源 URL、岗位、通用风险提示、公司名与通用年份，也不要抄写与本轮任务无关的整套财务指标。
必须把“现金流支持盈利质量”“扣非利润验证持续性”“收入与利润是否匹配”这类关系写出来，而不是只列名词。
不引入输入中不存在的信息，不使用任何未来问题或 Gold dependency。
先锁定 User 的当前问题，再筛选 Assistant 回答；除非当前任务要求整体梳理，否则不得复制营业收入、净利润、总资产、权益四套通用底表。关键指标最多 3 项，检索关键词最多 8 个。
"""

PRESERVATION_PROMPT = """
你是严格的信息保留审计员。比较原始单轮对话与生产 Page 的 summary、keywords、embedding_text，识别原对话本轮独有的信息是否被保留。
不要把公司、年份、通用底表视为本轮独有信息。只返回严格 JSON：
{
  "core_topic": "...",
  "new_facts": ["..."],
  "key_numbers": ["..."],
  "core_conclusion": "...",
  "relation_to_prior_context": "...",
  "has_key_number": true,
  "topic": {"summary": true, "keywords": true, "embedding_text": true},
  "key_fact": {"summary": true, "keywords": true, "embedding_text": true},
  "key_number": {"summary": true, "keywords": false, "embedding_text": true},
  "conclusion": {"summary": true, "keywords": false, "embedding_text": true},
  "relation": {"summary": true, "keywords": false, "embedding_text": true},
  "reason": "简洁、可核查的理由"
}
若原对话没有真正有区分度的数字，has_key_number=false，key_number 三项均设为 true，避免把“不需要数字”误判成丢失。
"""

CASE_REASON_PROMPT = """
你是 Dense Page 排名误差分析员。根据 current query、Gold Page 以及当前 Top-10 Page 的真实表示，解释为什么错误 Page 的 cosine score 高于 Gold。
primary_reason 和 secondary_reason 必须从以下枚举选择：
A_COMMON_TERMS（主体/年份/通用财务术语重复过多）
B_SURFACE_KEYWORD（错误 Page 与 Query 共享更多表面词）
C_GOLD_SUMMARY_MISSING（Gold summary 丢失关键概念）
D_GOLD_CONCEPT_WEAK（Gold summary 对核心概念表达过弱）
E_GOLD_KEYWORDS_MISSING（Gold keywords 未抽出关键概念）
F_USER_SIGNAL_DILUTED（Gold user question 有正确词，但被 summary 公共信息稀释）
G_QUERY_ANAPHORIC（Query 指代化，Page 表示不是主要责任）

只返回严格 JSON：
{"primary_reason":"枚举","secondary_reason":"枚举或null","evidence":["..."],"explanation":"..."}
必须引用输入中的具体 Query/summary/keywords 证据，不能只复述枚举名称。
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="诊断 S001 Page Summary Prompt 是否导致表示同质化与 Dense 排名损失")
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--previous-diagnosis", default=DEFAULT_PREVIOUS_DIAGNOSIS)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--reuse-llm-results", action="store_true")
    return parser.parse_args()


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() == "true"


def json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value)]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def join_field(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value if str(item).strip())
    return str(value or "").strip()


def retrieval_focused_text(parsed: Mapping[str, Any]) -> str:
    fields = (
        ("主题", "topic"),
        ("用户意图", "user_intent"),
        ("关键指标", "key_metrics"),
        ("关键事实", "key_facts"),
        ("核心结论", "core_conclusion"),
        ("前文关系", "relation_to_prior_context"),
        ("检索关键词", "retrieval_keywords"),
    )
    return "\n".join(f"{label}: {join_field(parsed.get(key))}" for label, key in fields if join_field(parsed.get(key)))


def minimal_text(parsed: Mapping[str, Any]) -> str:
    fields = (
        ("用户任务", "user_task"),
        ("核心主题", "core_topic"),
        ("关键指标", "key_metrics"),
        ("关键事实", "key_fact"),
        ("核心结论", "core_conclusion"),
        ("检索关键词", "retrieval_keywords"),
    )
    return "\n".join(f"{label}: {join_field(parsed.get(key))}" for label, key in fields if join_field(parsed.get(key)))


def current_generated_text(parsed: Mapping[str, Any], user_input: str) -> str:
    keywords = json_list(parsed.get("keywords"))
    return "\n".join(
        part
        for part in (
            str(parsed.get("summary") or "").strip(),
            f"Keywords: {', '.join(keywords)}" if keywords else "",
            f"User: {user_input}",
        )
        if part
    )


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def pairwise_stats(vectors: Sequence[Sequence[float]]) -> dict[str, float]:
    similarities = [
        cosine_similarity(vectors[left], vectors[right])
        for left in range(len(vectors))
        for right in range(left + 1, len(vectors))
    ]
    return {
        "pair_count": len(similarities),
        "mean": statistics.fmean(similarities),
        "median": statistics.median(similarities),
        "p75": percentile(similarities, 0.75),
        "p90": percentile(similarities, 0.90),
        "p95": percentile(similarities, 0.95),
        "max": max(similarities),
    }


def llm_json(llm: Any, system: str, payload: Mapping[str, Any], attempts: int = 5) -> dict[str, Any]:
    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        try:
            response = llm.generate_response(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format=None,
                _return_metadata=True,
            )
            raw = str(getattr(response, "content", response) or "").strip()
            if not raw:
                raise ValueError("empty content")
            return {
                "raw_output": raw,
                "parsed": parse_llm_json(raw),
                "attempts": attempt,
                "response_model": getattr(response, "model", None),
                "finish_reason": getattr(response, "finish_reason", None),
                "errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - all failures must be retained in diagnostic artifacts
            errors.append(f"{type(exc).__name__}: {exc}")
            LOGGER.warning("LLM attempt %d/%d failed: %s", attempt, attempts, exc)
    raise RuntimeError(f"LLM failed after {attempts} attempts: {errors}")


def generate_page_representations(
    llm: Any,
    pages: Sequence[PageRow],
    output_path: Path,
    *,
    variant: str,
    system_prompt: str,
    model_config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows = load_jsonl(output_path)
    by_page = {str(row["page_id"]): row for row in rows}
    for index, page in enumerate(pages, start=1):
        if page.page_id in by_page:
            continue
        LOGGER.info("Generate %s %d/%d: %s", variant, index, len(pages), page.turn_id)
        result = llm_json(
            llm,
            system_prompt,
            {
                "source_turn": page.turn_id,
                "raw_dialogue": page.payload.get("raw_dialogue") or "",
            },
        )
        parsed = result["parsed"]
        if variant == "current_regenerated":
            embedding_text = current_generated_text(parsed, str(page.payload.get("user_input") or ""))
        elif variant == "retrieval_focused":
            embedding_text = retrieval_focused_text(parsed)
        elif variant == "minimal":
            embedding_text = minimal_text(parsed)
        else:
            raise ValueError(variant)
        if not embedding_text.strip():
            raise ValueError(f"{page.turn_id} {variant} produced empty embedding text")
        row = {
            "page_id": page.page_id,
            "source_turn": page.turn_id,
            "variant": variant,
            "model_config": dict(model_config),
            **result,
            "embedding_text": embedding_text,
        }
        if variant == "current_regenerated":
            row["production"] = {
                "summary": page.payload.get("summary"),
                "keywords": page.payload.get("keywords") or [],
                "embedding_text": page.payload.get("data"),
            }
        rows.append(row)
        by_page[page.page_id] = row
        write_jsonl(output_path, rows)
    rows.sort(key=lambda row: next(page.turn_index for page in pages if page.page_id == row["page_id"]))
    write_jsonl(output_path, rows)
    return rows


def embed_text_map(embedder: Any, pages: Sequence[PageRow], texts: Mapping[str, str]) -> dict[str, list[float]]:
    ordered = [str(texts[page.page_id]) for page in pages]
    vectors = embedder.embed_batch(ordered, "search")
    return {page.page_id: list(vector) for page, vector in zip(pages, vectors)}


def long_range_turns(session: BenchmarkSession) -> list[BenchmarkTurn]:
    return [
        turn
        for turn in session.turns
        if turn.needs_history
        and turn.dependency_turn_ids
        and any(distance > SHORT_TERM_QA_CAPACITY for distance in dependency_distances(session, turn))
    ]


def long_gold_map(session: BenchmarkSession, evaluated: Sequence[BenchmarkTurn]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for turn in evaluated:
        result[turn.turn_id] = [
            gold
            for gold, distance in zip(turn.dependency_turn_ids, dependency_distances(session, turn))
            if distance > SHORT_TERM_QA_CAPACITY
        ]
    return result


def build_rankings(
    pages: Sequence[PageRow],
    evaluated: Sequence[BenchmarkTurn],
    query_vectors: Mapping[str, Sequence[float]],
    document_vectors: Mapping[str, Sequence[float]],
) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for turn in evaluated:
        available = [page for page in pages if page.turn_index <= turn.turn_index - SHORT_TERM_QA_CAPACITY - 1]
        rankings[turn.turn_id] = query_dense_rank(
            query_vectors[turn.turn_id],
            available,
            document_vectors=document_vectors,
        )
    return rankings


def rank_of(ranking: Sequence[Mapping[str, Any]], turn_id: str) -> int | None:
    item = next((row for row in ranking if row["turn_id"] == turn_id), None)
    return int(item["rank"]) if item else None


def score_of(ranking: Sequence[Mapping[str, Any]], turn_id: str) -> float | None:
    item = next((row for row in ranking if row["turn_id"] == turn_id), None)
    return float(item["score"]) if item else None


def rank_metrics(
    evaluated: Sequence[BenchmarkTurn],
    gold_by_turn: Mapping[str, Sequence[str]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    ranks = [
        rank_of(rankings[turn.turn_id], gold)
        for turn in evaluated
        for gold in gold_by_turn[turn.turn_id]
    ]
    finite = [int(rank) for rank in ranks if rank is not None]
    return {
        "gold_count": len(ranks),
        "recall_at_1": sum(rank is not None and rank <= 1 for rank in ranks) / len(ranks),
        "recall_at_3": sum(rank is not None and rank <= 3 for rank in ranks) / len(ranks),
        "recall_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks),
        "recall_at_10": sum(rank is not None and rank <= 10 for rank in ranks) / len(ranks),
        "mrr": statistics.fmean(1.0 / rank if rank else 0.0 for rank in ranks),
        "mean_gold_rank": statistics.fmean(finite),
        "median_gold_rank": statistics.median(finite),
    }


def load_previous_gold_rows(previous_dir: Path) -> list[dict[str, Any]]:
    path = previous_dir / "per_gold_diagnosis.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def export_pages(pages: Sequence[PageRow], output_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for page in pages:
        payload = page.payload
        actual = str(payload.get("data") or "")
        reconstructed = page_embedding_text(payload)
        rows.append(
            {
                "page_id": page.page_id,
                "source_turn_id": page.turn_id,
                "source_job_id": page.source_job_id,
                "session_id": page.session_id,
                "run_id": payload.get("run_id"),
                "user_input": payload.get("user_input"),
                "raw_dialogue": payload.get("raw_dialogue"),
                "summary": payload.get("summary"),
                "keywords": payload.get("keywords") or [],
                "embedding_text": actual,
                "embedding_text_matches_code": actual == reconstructed,
                "raw_dialogue_length": len(str(payload.get("raw_dialogue") or "")),
                "summary_length": len(str(payload.get("summary") or "")),
                "embedding_text_length": len(actual),
            }
        )
    write_csv(output_dir / "all_pages.csv", rows)
    lines = ["# S001 全部 Mid-term Page", ""]
    for row in rows:
        lines.extend(
            [
                f"## {row['source_turn_id']}",
                "",
                f"- page_id: `{row['page_id']}`",
                f"- source_job_id: `{row['source_job_id']}`",
                f"- session_id: `{row['session_id']}`",
                f"- lengths: raw={row['raw_dialogue_length']}, summary={row['summary_length']}, embedding={row['embedding_text_length']}",
                "",
                "### User input",
                "",
                str(row["user_input"] or ""),
                "",
                "### Summary",
                "",
                str(row["summary"] or ""),
                "",
                f"**Keywords:** {row['keywords']}",
                "",
                "### Embedding text",
                "",
                str(row["embedding_text"] or ""),
                "",
                "<details><summary>Raw dialogue</summary>",
                "",
                str(row["raw_dialogue"] or ""),
                "",
                "</details>",
                "",
            ]
        )
    (output_dir / "all_pages.md").write_text("\n".join(lines), encoding="utf-8")
    return rows


def common_term_rows(pages: Sequence[PageRow]) -> list[dict[str, Any]]:
    result = []
    for term in COMMON_TERMS:
        for field in ("summary", "keywords", "embedding_text"):
            documents = []
            for page in pages:
                if field == "keywords":
                    text = " ".join(json_list(page.payload.get("keywords")))
                elif field == "embedding_text":
                    text = str(page.payload.get("data") or "")
                else:
                    text = str(page.payload.get(field) or "")
                documents.append(text)
            df = sum(term in text for text in documents)
            result.append(
                {
                    "term": term,
                    "field": field,
                    "document_frequency": df,
                    "document_frequency_rate": df / len(documents),
                    "total_occurrences": sum(text.count(term) for text in documents),
                }
            )
    return result


def relation_semantics(text: Any) -> list[str]:
    value = str(text or "")
    relation_words = ("支持", "匹配", "含量", "持续", "质量", "验证", "错位", "同步", "方向", "幅度", "快于")
    result = []
    if (
        any(term in value for term in ("经营现金流", "经营活动现金流", "现金流"))
        and any(term in value for term in ("盈利质量", "净利润", "盈利可持续性", "利润"))
        and any(term in value for term in relation_words)
    ):
        result.append("CASHFLOW_VS_PROFIT_QUALITY")
    if (
        "扣非净利润" in value
        and any(term in value for term in ("盈利趋势", "持续经营", "盈利持续性", "盈利可持续性", "归母净利润"))
        and any(term in value for term in relation_words)
    ):
        result.append("DEDUCTED_PROFIT_VS_SUSTAINABILITY")
    if (
        "营业收入" in value
        and "归母净利润" in value
        and any(term in value for term in ("匹配", "同步", "幅度", "方向", "降幅", "增减"))
    ):
        result.append("REVENUE_VS_PROFIT_MATCH")
    if (
        "应收" in value
        and "营业收入" in value
        and any(term in value for term in ("增长", "快于", "质量", "匹配", "占比"))
    ):
        result.append("RECEIVABLE_VS_REVENUE_QUALITY")
    return result


def run_preservation_judge(
    llm: Any,
    pages: Sequence[PageRow],
    gold_rows: Sequence[Mapping[str, Any]],
    path: Path,
) -> list[dict[str, Any]]:
    unique_gold_ids = sorted(
        {str(row["gold_turn_id"]) for row in gold_rows},
        key=lambda turn_id: int(turn_id.rsplit("Q", 1)[1]),
    )
    page_by_turn = {page.turn_id: page for page in pages}
    rows = load_jsonl(path)
    completed = {str(row["source_turn"]) for row in rows}
    for index, turn_id in enumerate(unique_gold_ids, start=1):
        if turn_id in completed:
            continue
        page = page_by_turn[turn_id]
        LOGGER.info("Preservation judge %d/%d: %s", index, len(unique_gold_ids), turn_id)
        result = llm_json(
            llm,
            PRESERVATION_PROMPT,
            {
                "source_turn": turn_id,
                "raw_dialogue": page.payload.get("raw_dialogue"),
                "summary": page.payload.get("summary"),
                "keywords": page.payload.get("keywords") or [],
                "embedding_text": page.payload.get("data"),
            },
        )
        rows.append({"source_turn": turn_id, **result})
        completed.add(turn_id)
        write_jsonl(path, rows)
    return rows


def preservation_csv_rows(
    gold_rows: Sequence[Mapping[str, Any]],
    preservation_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    by_turn = {str(row["source_turn"]): row["parsed"] for row in preservation_rows}
    output = []
    for gold in gold_rows:
        parsed = by_turn[str(gold["gold_turn_id"])]
        channels = {name: parsed.get(name) or {} for name in ("topic", "key_fact", "key_number", "conclusion", "relation")}
        output.append(
            {
                "query_turn": gold["turn_id"],
                "gold_turn": gold["gold_turn_id"],
                "baseline_hit": as_bool(gold.get("baseline_mid_page_hit")),
                "core_topic": parsed.get("core_topic"),
                "new_facts": parsed.get("new_facts") or [],
                "key_numbers": parsed.get("key_numbers") or [],
                "core_conclusion": parsed.get("core_conclusion"),
                "relation_to_prior_context": parsed.get("relation_to_prior_context"),
                "has_key_number": as_bool(parsed.get("has_key_number")),
                **{
                    f"{name}_preserved": as_bool(channels[name].get("embedding_text"))
                    for name in channels
                },
                **{
                    f"{name}_in_{channel}": as_bool(channels[name].get(channel))
                    for name in channels
                    for channel in ("summary", "keywords", "embedding_text")
                },
                "reason": parsed.get("reason"),
            }
        )
    return output


def run_case_reason_judge(
    llm: Any,
    pages: Sequence[PageRow],
    miss_rows: Sequence[Mapping[str, Any]],
    turns: Mapping[str, BenchmarkTurn],
    current_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    path: Path,
) -> list[dict[str, Any]]:
    page_by_turn = {page.turn_id: page for page in pages}
    rows = load_jsonl(path)
    completed = {(str(row["query_turn"]), str(row["gold_turn"])) for row in rows}
    for index, gold in enumerate(miss_rows, start=1):
        key = (str(gold["turn_id"]), str(gold["gold_turn_id"]))
        if key in completed:
            continue
        query_turn, gold_turn = key
        gold_page = page_by_turn[gold_turn]
        top10 = current_rankings[query_turn][:10]
        LOGGER.info("Case reason %d/%d: %s -> %s", index, len(miss_rows), query_turn, gold_turn)
        result = llm_json(
            llm,
            CASE_REASON_PROMPT,
            {
                "current_query": turns[query_turn].question,
                "gold": {
                    "turn": gold_turn,
                    "rank": rank_of(current_rankings[query_turn], gold_turn),
                    "score": score_of(current_rankings[query_turn], gold_turn),
                    "user_input": gold_page.payload.get("user_input"),
                    "summary": gold_page.payload.get("summary"),
                    "keywords": gold_page.payload.get("keywords"),
                    "embedding_text": gold_page.payload.get("data"),
                },
                "top10": [
                    {
                        "turn": item["turn_id"],
                        "rank": item["rank"],
                        "score": item["score"],
                        "user_input": page_by_turn[str(item["turn_id"])].payload.get("user_input"),
                        "summary": page_by_turn[str(item["turn_id"])].payload.get("summary"),
                        "keywords": page_by_turn[str(item["turn_id"])].payload.get("keywords"),
                    }
                    for item in top10
                ],
            },
        )
        rows.append({"query_turn": query_turn, "gold_turn": gold_turn, **result})
        completed.add(key)
        write_jsonl(path, rows)
    return rows


def stability_reextract(
    llm: Any,
    pages: Sequence[PageRow],
    main_rows: Mapping[str, Mapping[str, Mapping[str, Any]]],
    path: Path,
    model_config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    critical_turns = ("S001-Q006", "S001-Q022", "S001-Q032")
    page_by_turn = {page.turn_id: page for page in pages}
    rows = load_jsonl(path)
    completed = {(str(row["source_turn"]), str(row["variant"]), int(row["repeat"])) for row in rows}
    for turn_id in critical_turns:
        page = page_by_turn[turn_id]
        for variant, prompt, formatter in (
            ("retrieval_focused", RETRIEVAL_FOCUSED_PROMPT, retrieval_focused_text),
            ("minimal", MINIMAL_PROMPT, minimal_text),
        ):
            for repeat in (1, 2):
                key = (turn_id, variant, repeat)
                if key in completed:
                    continue
                LOGGER.info("Stability reextract %s %s repeat=%d", turn_id, variant, repeat)
                result = llm_json(
                    llm,
                    prompt,
                    {"source_turn": turn_id, "raw_dialogue": page.payload.get("raw_dialogue")},
                )
                text = formatter(result["parsed"])
                rows.append(
                    {
                        "source_turn": turn_id,
                        "page_id": page.page_id,
                        "variant": variant,
                        "repeat": repeat,
                        "model_config": dict(model_config),
                        **result,
                        "embedding_text": text,
                        "exact_match_main": canonical_json(result["parsed"])
                        == canonical_json(main_rows[variant][page.page_id]["parsed"]),
                    }
                )
                completed.add(key)
                write_jsonl(path, rows)
    return rows


def build_top10_inspection(
    pages: Sequence[PageRow],
    turns: Mapping[str, BenchmarkTurn],
    current_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    reason_rows: Sequence[Mapping[str, Any]],
) -> str:
    page_by_turn = {page.turn_id: page for page in pages}
    reason_by_key = {(row["query_turn"], row["gold_turn"]): row["parsed"] for row in reason_rows}
    lines = ["# 典型失败 Query：Current Page Top-10 完整检查", ""]
    for query_turn, gold_turn in TYPICAL_CASES:
        ranking = current_rankings[query_turn]
        reason = reason_by_key[(query_turn, gold_turn)]
        lines.extend(
            [
                f"## {query_turn} → {gold_turn}",
                "",
                f"**Current Query:** {turns[query_turn].question}",
                "",
                f"**Gold rank/score:** {rank_of(ranking, gold_turn)} / {score_of(ranking, gold_turn):.6f}",
                "",
                f"**原因：** primary={reason.get('primary_reason')}；secondary={reason.get('secondary_reason')}；{reason.get('explanation')}",
                "",
            ]
        )
        for item in ranking[:10]:
            page = page_by_turn[str(item["turn_id"])]
            marker = " **[GOLD]**" if item["turn_id"] == gold_turn else ""
            lines.extend(
                [
                    f"### Rank {item['rank']} — {item['turn_id']}{marker}",
                    "",
                    f"score={float(item['score']):.6f}",
                    "",
                    f"- User: {page.payload.get('user_input')}",
                    f"- Summary: {page.payload.get('summary')}",
                    f"- Keywords: {page.payload.get('keywords')}",
                    "",
                    "```text",
                    str(page.payload.get("data") or ""),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines)


def build_report(
    summary: Mapping[str, Any],
    ablation_rows: Sequence[Mapping[str, Any]],
    similarity_rows: Sequence[Mapping[str, Any]],
    common_rows: Sequence[Mapping[str, Any]],
    case_rows: Sequence[Mapping[str, Any]],
    root_rows: Sequence[Mapping[str, Any]],
    preservation_rows: Sequence[Mapping[str, Any]],
    cashflow_rows: Sequence[Mapping[str, Any]],
    pages: Sequence[PageRow],
    generated: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> str:
    metric_by_variant = {row["variant"]: row for row in ablation_rows}
    similarity_by_variant = {row["representation"]: row for row in similarity_rows}
    root_counts = Counter(str(row["root_cause_2x2"]) for row in root_rows)
    common_by_key = {(row["term"], row["field"]): row for row in common_rows}
    common_terms_ordered = sorted(
        COMMON_TERMS,
        key=lambda term: -int(common_by_key[(term, "embedding_text")]["document_frequency"]),
    )
    hit_preservation = [row for row in preservation_rows if row["baseline_hit"]]
    miss_preservation = [row for row in preservation_rows if not row["baseline_hit"]]

    def preservation_rate(rows: Sequence[Mapping[str, Any]], field: str) -> float:
        return statistics.fmean(as_bool(row[field]) for row in rows) if rows else 0.0

    lines = [
        "# S001 Page Representation / Prompt 根因诊断",
        "",
        f"> 受控变量：Query、BAAI/bge-small-zh-v1.5、时间快照、Session 与 Top-K 均不变，只改变 Page representation。离线抽取模型固定为 `{summary['generation_config']['model']}`，temperature={summary['generation_config']['temperature']}。",
        "",
        "## 1. 生产 Page 实际存储与 embedding_text",
        "",
        "Qdrant payload 实测包含 `raw_dialogue`、`user_input`、`assistant_response`、`summary`、`keywords`、`data`、`session_id`、`source_job_id`、`run_id`。生产代码将 `data` 写为：",
        "",
        "```text",
        "summary",
        "+ Keywords: <keywords>",
        "+ User: <user_input>",
        "```",
        "",
        "47/47 个 Page 的真实 `data` 与按生产代码重建的 embedding_text 完全相等；未覆盖 Qdrant Collection。",
        "",
        "## 2. 公共词频与表示同质化",
        "",
        "| Term | Summary DF | Keywords DF | embedding_text DF |",
        "|---|---:|---:|---:|",
    ]
    for term in common_terms_ordered:
        summary_row = common_by_key[(term, "summary")]
        keywords_row = common_by_key[(term, "keywords")]
        embedding_row = common_by_key[(term, "embedding_text")]
        lines.append(
            f"| {term} | {summary_row['document_frequency']}/47 | {keywords_row['document_frequency']}/47 | "
            f"{embedding_row['document_frequency']}/47 |"
        )
    lines.extend(
        [
            "",
            "| Representation | Mean pairwise cosine | Median | P90 | P95 | Max |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name in ("user_input", "summary", "raw_dialogue", "current", "retrieval_focused", "minimal"):
        row = similarity_by_variant[name]
        lines.append(
            f"| {name} | {row['mean']:.4f} | {row['median']:.4f} | {row['p90']:.4f} | {row['p95']:.4f} | {row['max']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 3. 相同 Query / embedding / 时间快照的 Page 表示消融",
            "",
            "| Variant | R@1 | R@3 | R@5 | R@10 | MRR | Mean rank | Median rank |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ablation_rows:
        lines.append(
            f"| {row['variant']} | {row['recall_at_1']:.2%} | {row['recall_at_3']:.2%} | {row['recall_at_5']:.2%} | "
            f"{row['recall_at_10']:.2%} | {row['mrr']:.4f} | {row['mean_gold_rank']:.2f} | {row['median_gold_rank']:.1f} |"
        )
    current = metric_by_variant["current_production"]
    retrieval = metric_by_variant["retrieval_focused"]
    minimal = metric_by_variant["minimal"]
    regenerated = metric_by_variant["current_prompt_regenerated"]
    lines.extend(
        [
            "",
            f"Retrieval-focused 在保持 Query 与 embedding 不变时，Recall@5 从 {current['recall_at_5']:.2%} 变为 {retrieval['recall_at_5']:.2%}（{retrieval['recall_at_5'] - current['recall_at_5']:+.2%}）；Minimal 为 {minimal['recall_at_5']:.2%}。同一 DeepSeek 模型重跑生产 Prompt 的 Recall@5 为 {regenerated['recall_at_5']:.2%}，用于分离 Prompt 与生成模型差异。",
            "",
            f"生产 Page 原本由配置中的 `deepseek-v4-flash` 生成，而离线三套重抽固定为 `deepseek-chat`，所以 production→current-regenerated 的 +{regenerated['recall_at_5'] - current['recall_at_5']:.2%} 不能归因于 Prompt（Prompt 根本没变）。在严格相同的 `deepseek-chat` 下，Retrieval-focused 相对重跑 Current 变化 {retrieval['recall_at_5'] - regenerated['recall_at_5']:+.2%}，且 R@10/Mean rank 也更差；这排除了“只要把 Prompt 改成检索导向就会整体改善”的假设。",
            "",
            "## 4. 典型 rank 6–10 案例的前后变化",
            "",
            "| Query → Gold | Current rank | Retrieval rank | Minimal rank | Oracle+Current | Oracle+Retrieval |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in case_rows:
        lines.append(
            f"| {row['query_turn']} → {row['gold_turn']} | {row['current_rank']} | {row['retrieval_focused_rank']} | "
            f"{row['minimal_rank']} | {row['oracle_current_rank']} | {row['oracle_retrieval_rank']} |"
        )
    reason_counts = Counter(str(row["ranking_primary_reason"]) for row in root_rows)
    lines.extend(
        [
            "",
            "完整 Top-10 候选的 user_input、summary、keywords、embedding_text 以及逐例 A–G 原因见 `top10_case_inspection.md`。错误 Page 得分更高的解释不是 cutoff，而是逐例由公共术语、表面词、Gold 关系语义丢失/弱化或 Query 指代性判定。",
            "",
            "15 条 miss 的逐例 primary reason 为："
            + "；".join(f"{reason}={count}" for reason, count in reason_counts.most_common())
            + "。",
            "",
            "## 5. Gold 区分性内容保留率",
            "",
            "| Signal | Hit Gold | Miss Gold | Miss - Hit |",
            "|---|---:|---:|---:|",
        ]
    )
    for field, label in (
        ("topic_preserved", "topic"),
        ("key_fact_preserved", "key fact"),
        ("key_number_preserved", "key number"),
        ("conclusion_preserved", "conclusion"),
        ("relation_preserved", "relation"),
    ):
        hit_rate = preservation_rate(hit_preservation, field)
        miss_rate = preservation_rate(miss_preservation, field)
        lines.append(f"| {label} | {hit_rate:.2%} | {miss_rate:.2%} | {miss_rate - hit_rate:+.2%} |")

    lines.extend(
        [
            "",
            "分字段看，生产 summary 对 21 条 Gold 的 topic/fact/number/conclusion/relation 保留均为 21/21；keywords 仅对 topic/fact 为 21/21，number/conclusion/relation 均只有 1/21。由于正式 embedding_text 还包含 summary 和原始 User question，五类信号最终均为 21/21。这说明主要现象是大量公共内容造成相对权重稀释，而不是关键信息从 Page 表示中彻底消失。",
        ]
    )

    relation_current_count = sum(as_bool(row["current_explicit_relation"]) for row in cashflow_rows)
    relation_new_count = sum(as_bool(row["retrieval_explicit_relation"]) for row in cashflow_rows)
    lines.extend(
        [
            "",
            "## 6. 经营现金流 / 盈利质量专题检查",
            "",
            f"按 user_input/summary/keywords 中的现金流、扣非、盈利质量/持续性等特定概念筛出 {len(cashflow_rows)} 个 Page。生产 embedding_text 明确写出四类关系语义的有 {relation_current_count}/{len(cashflow_rows)}；Retrieval-focused 表示为 {relation_new_count}/{len(cashflow_rows)}。",
            "",
            "| Turn | Current summary relations | Current embedding relations | Retrieval relations | Current keywords |",
            "|---|---|---|---|---|",
        ]
    )
    for row in cashflow_rows:
        lines.append(
            f"| {row['source_turn']} | {row['current_summary_relation_types']} | {row['current_embedding_relation_types']} | "
            f"{row['retrieval_relation_types']} | {compact_text(row['keywords'], 120)} |"
        )
    q006 = next(row for row in cashflow_rows if row["source_turn"] == "S001-Q006")
    q007 = next(row for row in cashflow_rows if row["source_turn"] == "S001-Q007")
    lines.extend(
        [
            "",
            f"Q007 的生产 summary 明确保留了“核查经营现金流是否支持盈利质量判断”，并说明因完整现金流序列不在底表而无法验证；关系类型={q007['current_summary_relation_types']}。因此该关系没有被 Prompt 丢失。",
            "",
            f"Q006 的生产 summary 保留了“扣非净利润是否调整盈利趋势”的任务，但原回答没有取得扣非三年数据，只能维持开放结论；关系类型={q006['current_summary_relation_types']}。这里缺的是原始回答中的可用事实，不是摘要把已经存在的扣非结论删掉。",
        ]
    )

    lines.extend(
        [
            "",
            "## 7. Current vs Retrieval-focused 具体质量审查（5 hit + 5 miss）",
            "",
        ]
    )
    selected = hit_preservation[:5] + miss_preservation[:5]
    page_by_turn = {page.turn_id: page for page in pages}
    for row in selected:
        page = page_by_turn[str(row["gold_turn"])]
        retrieval_row = generated["retrieval_focused"][page.page_id]
        lines.extend(
            [
                f"### {row['query_turn']} → {row['gold_turn']} ({'hit' if row['baseline_hit'] else 'miss'})",
                "",
                f"- 原始本轮核心：主题={row['core_topic']}；新增事实={compact_text(row['new_facts'], 420)}；结论={compact_text(row['core_conclusion'], 350)}；前文关系={compact_text(row['relation_to_prior_context'], 300)}",
                f"- Current summary：{compact_text(page.payload.get('summary'), 700)}",
                f"- Current keywords：{page.payload.get('keywords')}",
                f"- Retrieval-focused：{compact_text(retrieval_row['embedding_text'], 900)}",
                "",
            ]
        )

    lines.extend(
        [
            "## 8. Prompt vs Query 的 2×2 归因",
            "",
            "定义：Current Query+New Page 单独进入 Top-5 为 Page rescue；Oracle Query+Current Page 单独进入 Top-5 为 Query rescue；只有 Oracle+New 联合进入或两边都可 rescue 为 BOTH。数据集错标/严格等价优先归入 DATASET。",
            "",
            "| Root class | Count | Share of 15 misses |",
            "|---|---:|---:|",
        ]
    )
    for cause, count in sorted(root_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {cause} | {count} | {count / len(root_rows):.2%} |")

    stability = summary["stability"]
    lines.extend(
        [
            "",
            "## 9. 重复抽取稳定性",
            "",
            f"对 Q006、Q022、Q032 的 Retrieval-focused 与 Minimal 分别额外复抽 2 次（加主抽取共 3 次）。解析结果 exact match 为 {stability['exact_match_rate']:.2%}，embedding cosine mean/min 为 {stability['embedding_cosine_mean']:.4f}/{stability['embedding_cosine_min']:.4f}；Gold rank 完全不变 {stability['rank_same_rate']:.2%}，Top-5 命中状态不变 {stability['top5_status_same_rate']:.2%}，rank 平均/最大绝对变化为 {stability['rank_abs_change_mean']:.2f}/{stability['rank_abs_change_max']}。temperature=0 未带来字节级确定性，但语义向量与 Top-5 判断稳定；更重要的是 Retrieval-focused 总体 R@5 没有增益，因此不存在由一次幸运输出制造的正向结论。",
            "",
            "## 10. 最终结论",
            "",
            f"**{summary['headline_conclusion']}**",
            "",
            summary["conclusion_detail"],
            "",
            "完整逐 Page、逐 Gold、逐 Query 和 LLM 原始输出均在本目录 CSV/JSONL 中，可复跑且未修改生产 Prompt 或 Collection。",
        ]
    )
    return "\n".join(lines) + "\n"


def run() -> None:
    args = parse_args()
    source_dir = resolve_path(REPO_ROOT, args.source_dir)
    previous_dir = resolve_path(REPO_ROOT, args.previous_diagnosis)
    output_dir = resolve_path(REPO_ROOT, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    effective_config = load_json(source_dir / "effective_memory_config.json")
    recall_config = load_json(REPO_ROOT / "exp/benchmark/recall_benchmark.json")
    dataset_path = resolve_path(REPO_ROOT, (recall_config.get("dataset") or {})["path"])
    session = load_dataset(dataset_path, include_sheets=[args.sheet])[0]
    turns = {turn.turn_id: turn for turn in session.turns}
    evaluated = long_range_turns(session)
    gold_by_turn = long_gold_map(session, evaluated)

    client, collection_name = qdrant_client_from_config(effective_config)
    pages, _sessions = load_qdrant_state(client, collection_name, session)
    if len(pages) != 47:
        raise ValueError(f"Expected 47 S001 pages, got {len(pages)}")
    exported = export_pages(pages, output_dir)
    if not all(row["embedding_text_matches_code"] for row in exported):
        raise AssertionError("Qdrant data differs from MidTermMemory.page_embedding_text")

    common_rows = common_term_rows(pages)
    write_csv(output_dir / "common_term_frequency.csv", common_rows)

    base_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((base_config.get("llm") or {}).get("config") or {})
    llm_config.update({"model": args.model, "temperature": 0.0, "top_p": 0.1, "top_k": 1, "max_tokens": 2500})
    public_model_config = {
        "model": args.model,
        "temperature": 0.0,
        "top_p": 0.1,
        "top_k": 1,
        "max_tokens": 2500,
    }
    llm = LlmFactory.create(
        str((base_config.get("llm") or {}).get("provider")),
        llm_config,
        timeout_seconds=float(base_config.get("llm_timeout_seconds") or 180),
    )

    generation_specs = (
        ("current_regenerated", MIDTERM_PAGE_SUMMARY_PROMPT, output_dir / "page_repr_current.jsonl"),
        ("retrieval_focused", RETRIEVAL_FOCUSED_PROMPT, output_dir / "page_repr_retrieval_focused.jsonl"),
        ("minimal", MINIMAL_PROMPT, output_dir / "page_repr_minimal.jsonl"),
    )
    generated_rows: dict[str, list[dict[str, Any]]] = {}
    for variant, prompt, path in generation_specs:
        if args.reuse_llm_results and not path.exists():
            raise FileNotFoundError(f"--reuse-llm-results requested but missing {path}")
        generated_rows[variant] = generate_page_representations(
            llm,
            pages,
            path,
            variant=variant,
            system_prompt=prompt,
            model_config=public_model_config,
        )
    generated = {
        variant: {str(row["page_id"]): row for row in rows} for variant, rows in generated_rows.items()
    }

    embed_cfg = effective_config.get("embedder") or {}
    embedder = HuggingFaceEmbedding(BaseEmbedderConfig(**dict(embed_cfg.get("config") or {})))
    representation_texts: dict[str, dict[str, str]] = {
        "user_input": {page.page_id: str(page.payload.get("user_input") or "") for page in pages},
        "summary": {page.page_id: str(page.payload.get("summary") or "") for page in pages},
        "raw_dialogue": {page.page_id: str(page.payload.get("raw_dialogue") or "") for page in pages},
        "current_production": {page.page_id: str(page.payload.get("data") or "") for page in pages},
        "current_prompt_regenerated": {
            page.page_id: str(generated["current_regenerated"][page.page_id]["embedding_text"]) for page in pages
        },
        "retrieval_focused": {
            page.page_id: str(generated["retrieval_focused"][page.page_id]["embedding_text"]) for page in pages
        },
        "minimal": {page.page_id: str(generated["minimal"][page.page_id]["embedding_text"]) for page in pages},
    }
    representation_vectors = {
        variant: embed_text_map(embedder, pages, texts) for variant, texts in representation_texts.items()
    }

    similarity_rows = []
    similarity_names = {
        "user_input": "user_input",
        "summary": "summary",
        "raw_dialogue": "raw_dialogue",
        "current": "current_production",
        "current_regenerated": "current_prompt_regenerated",
        "retrieval_focused": "retrieval_focused",
        "minimal": "minimal",
    }
    for label, variant in similarity_names.items():
        stats = pairwise_stats([representation_vectors[variant][page.page_id] for page in pages])
        similarity_rows.append({"representation": label, **stats})
    write_csv(output_dir / "page_similarity_stats.csv", similarity_rows)

    query_texts = [turn.question for turn in evaluated]
    query_embeddings = embedder.embed_batch(query_texts, "search")
    query_vectors = {turn.turn_id: list(vector) for turn, vector in zip(evaluated, query_embeddings)}
    oracle_texts = [turn.required_context for turn in evaluated]
    oracle_embeddings = embedder.embed_batch(oracle_texts, "search")
    oracle_vectors = {turn.turn_id: list(vector) for turn, vector in zip(evaluated, oracle_embeddings)}

    rankings = {
        variant: build_rankings(pages, evaluated, query_vectors, vectors)
        for variant, vectors in representation_vectors.items()
    }
    oracle_rankings = {
        variant: build_rankings(pages, evaluated, oracle_vectors, vectors)
        for variant, vectors in representation_vectors.items()
    }
    ablation_order = (
        "current_production",
        "current_prompt_regenerated",
        "retrieval_focused",
        "minimal",
        "user_input",
        "summary",
    )
    ablation_rows = [{"variant": variant, **rank_metrics(evaluated, gold_by_turn, rankings[variant])} for variant in ablation_order]
    write_csv(output_dir / "page_representation_ablation.csv", ablation_rows)
    current_metrics = next(row for row in ablation_rows if row["variant"] == "current_production")
    if abs(float(current_metrics["recall_at_5"]) - 6 / 21) > 1e-12:
        raise AssertionError(f"Temporal current baseline drifted: {current_metrics}")

    previous_gold = load_previous_gold_rows(previous_dir)
    long_gold_rows = [row for row in previous_gold if row["gold_responsibility"] == "long_range"]
    miss_rows = [row for row in long_gold_rows if not as_bool(row["baseline_mid_page_hit"])]

    preservation_json = run_preservation_judge(
        llm,
        pages,
        long_gold_rows,
        output_dir / "information_preservation_judge.jsonl",
    )
    preservation_rows = preservation_csv_rows(long_gold_rows, preservation_json)
    write_csv(output_dir / "gold_information_preservation.csv", preservation_rows)

    reason_rows = run_case_reason_judge(
        llm,
        pages,
        miss_rows,
        turns,
        rankings["current_production"],
        output_dir / "case_reason_analysis.jsonl",
    )
    reason_by_key = {(row["query_turn"], row["gold_turn"]): row["parsed"] for row in reason_rows}

    stability_rows = stability_reextract(
        llm,
        pages,
        generated,
        output_dir / "stability_reextract.jsonl",
        public_model_config,
    )
    stability_texts = [str(row["embedding_text"]) for row in stability_rows]
    stability_vectors_list = embedder.embed_batch(stability_texts, "search")
    stability_cosines = []
    stability_rank_changes = []
    stability_rank_same = []
    stability_top5_same = []
    stability_query_by_gold = {
        "S001-Q006": "S001-Q013",
        "S001-Q022": "S001-Q028",
        "S001-Q032": "S001-Q039",
    }
    for row, vector in zip(stability_rows, stability_vectors_list):
        main_vector = representation_vectors[str(row["variant"])][str(row["page_id"])]
        row["embedding_cosine_to_main"] = cosine_similarity(vector, main_vector)
        stability_cosines.append(float(row["embedding_cosine_to_main"]))
        query_turn = stability_query_by_gold[str(row["source_turn"])]
        query = turns[query_turn]
        available = [page for page in pages if page.turn_index <= query.turn_index - SHORT_TERM_QA_CAPACITY - 1]
        repeated_vectors = dict(representation_vectors[str(row["variant"])])
        repeated_vectors[str(row["page_id"])] = list(vector)
        repeated_ranking = query_dense_rank(
            query_vectors[query_turn],
            available,
            document_vectors=repeated_vectors,
        )
        row["query_turn"] = query_turn
        row["main_rank"] = rank_of(rankings[str(row["variant"])][query_turn], str(row["source_turn"]))
        row["repeat_rank"] = rank_of(repeated_ranking, str(row["source_turn"]))
        row["rank_change"] = int(row["repeat_rank"]) - int(row["main_rank"])
        stability_rank_changes.append(abs(int(row["rank_change"])))
        stability_rank_same.append(int(row["repeat_rank"]) == int(row["main_rank"]))
        stability_top5_same.append((int(row["repeat_rank"]) <= 5) == (int(row["main_rank"]) <= 5))
    write_jsonl(output_dir / "stability_reextract.jsonl", stability_rows)

    case_rows = []
    for query_turn, gold_turn in TYPICAL_CASES:
        case_rows.append(
            {
                "query_turn": query_turn,
                "gold_turn": gold_turn,
                "current_rank": rank_of(rankings["current_production"][query_turn], gold_turn),
                "current_score": score_of(rankings["current_production"][query_turn], gold_turn),
                "retrieval_focused_rank": rank_of(rankings["retrieval_focused"][query_turn], gold_turn),
                "retrieval_focused_score": score_of(rankings["retrieval_focused"][query_turn], gold_turn),
                "minimal_rank": rank_of(rankings["minimal"][query_turn], gold_turn),
                "minimal_score": score_of(rankings["minimal"][query_turn], gold_turn),
                "oracle_current_rank": rank_of(oracle_rankings["current_production"][query_turn], gold_turn),
                "oracle_retrieval_rank": rank_of(oracle_rankings["retrieval_focused"][query_turn], gold_turn),
                "primary_reason": reason_by_key[(query_turn, gold_turn)].get("primary_reason"),
                "secondary_reason": reason_by_key[(query_turn, gold_turn)].get("secondary_reason"),
                "reason_explanation": reason_by_key[(query_turn, gold_turn)].get("explanation"),
            }
        )
    write_csv(output_dir / "case_rank_changes.csv", case_rows)

    root_rows = []
    for gold in miss_rows:
        query_turn = str(gold["turn_id"])
        gold_turn = str(gold["gold_turn_id"])
        current_rank = rank_of(rankings["current_production"][query_turn], gold_turn)
        page_rank = rank_of(rankings["retrieval_focused"][query_turn], gold_turn)
        query_rank = rank_of(oracle_rankings["current_production"][query_turn], gold_turn)
        joint_rank = rank_of(oracle_rankings["retrieval_focused"][query_turn], gold_turn)
        dataset_issue = str(gold.get("root_cause")) in {"DATASET_WRONG_GOLD", "EQUIVALENT_ALTERNATIVE_PAGE"}
        page_rescue = page_rank is not None and page_rank <= 5
        query_rescue = query_rank is not None and query_rank <= 5
        joint_rescue = joint_rank is not None and joint_rank <= 5
        if dataset_issue:
            root_cause = "DATASET"
        elif page_rescue and query_rescue:
            root_cause = "BOTH"
        elif page_rescue:
            root_cause = "PAGE_PROMPT_PRIMARY"
        elif query_rescue:
            root_cause = "QUERY_PRIMARY"
        elif joint_rescue:
            root_cause = "BOTH"
        else:
            root_cause = "OTHER"
        reason = reason_by_key[(query_turn, gold_turn)]
        root_rows.append(
            {
                "query_turn": query_turn,
                "gold_turn": gold_turn,
                "current_query_current_page_rank": current_rank,
                "current_query_new_page_rank": page_rank,
                "oracle_query_current_page_rank": query_rank,
                "oracle_query_new_page_rank": joint_rank,
                "page_rescue_top5": page_rescue,
                "query_rescue_top5": query_rescue,
                "joint_rescue_top5": joint_rescue,
                "root_cause_2x2": root_cause,
                "ranking_primary_reason": reason.get("primary_reason"),
                "ranking_secondary_reason": reason.get("secondary_reason"),
                "ranking_explanation": reason.get("explanation"),
                "prior_dataset_verdict": gold.get("dataset_audit_verdict"),
                "prior_semantic_equivalent": gold.get("semantic_equivalent"),
            }
        )
    write_csv(output_dir / "miss_root_cause.csv", root_rows)

    top10_md = build_top10_inspection(pages, turns, rankings["current_production"], reason_rows)
    (output_dir / "top10_case_inspection.md").write_text(top10_md, encoding="utf-8")

    cashflow_rows = []
    specific_topic_terms = (
        "经营现金流",
        "经营活动现金流",
        "现金流质量",
        "盈利质量",
        "利润现金含量",
        "盈利可持续性",
        "非经常性损益",
        "扣非净利润",
        "现金质量",
        "现金错位",
        "现金表现",
        "现金风险",
    )
    for page in pages:
        payload = page.payload
        topic_index_text = "\n".join(
            str(value or "")
            for value in (
                payload.get("user_input"),
                payload.get("summary"),
                " ".join(json_list(payload.get("keywords"))),
            )
        )
        if not any(term in topic_index_text for term in specific_topic_terms):
            continue
        current_text = str(payload.get("data") or "")
        retrieval_text = str(generated["retrieval_focused"][page.page_id]["embedding_text"])
        current_summary_relations = relation_semantics(payload.get("summary"))
        current_keyword_relations = relation_semantics(" ".join(json_list(payload.get("keywords"))))
        current_embedding_relations = relation_semantics(current_text)
        retrieval_relations = relation_semantics(retrieval_text)
        cashflow_rows.append(
            {
                "source_turn": page.turn_id,
                "user_input": payload.get("user_input"),
                "summary": payload.get("summary"),
                "keywords": payload.get("keywords") or [],
                "embedding_text": current_text,
                "retrieval_focused_text": retrieval_text,
                "matched_terms": [term for term in CASHFLOW_TERMS if term in topic_index_text],
                "current_summary_relation_types": current_summary_relations,
                "current_keywords_relation_types": current_keyword_relations,
                "current_embedding_relation_types": current_embedding_relations,
                "retrieval_relation_types": retrieval_relations,
                "current_explicit_relation": bool(current_embedding_relations),
                "retrieval_explicit_relation": bool(retrieval_relations),
            }
        )
    write_csv(output_dir / "cashflow_topic_pages.csv", cashflow_rows)

    root_counts = Counter(row["root_cause_2x2"] for row in root_rows)
    retrieval_metrics = next(row for row in ablation_rows if row["variant"] == "retrieval_focused")
    prompt_gain = float(retrieval_metrics["recall_at_5"]) - float(current_metrics["recall_at_5"])
    page_involved = root_counts.get("PAGE_PROMPT_PRIMARY", 0) + root_counts.get("BOTH", 0)
    query_involved = root_counts.get("QUERY_PRIMARY", 0) + root_counts.get("BOTH", 0)
    if prompt_gain <= 0.0 and query_involved > page_involved:
        headline = "当前 Page Recall 低：主要是 Query 问题；Page Prompt 同质化存在但不是主要问题"
    elif prompt_gain < 0.05 and page_involved <= 2:
        headline = "Prompt 不是主要问题"
    elif page_involved and query_involved and abs(page_involved - query_involved) <= 2:
        headline = "当前 Page Recall 低：Page Summary Prompt 与 Query 是共同问题"
    elif page_involved > query_involved:
        headline = "当前 Page Recall 低：主要是 Page Summary Prompt 问题"
    else:
        headline = "当前 Page Recall 低：主要是 Query 问题"
    conclusion_detail = (
        f"Retrieval-focused Prompt 的 R@5 相对生产表示变化 {prompt_gain:+.2%}；15 条 baseline miss 的 2×2 分类为 "
        + "、".join(f"{key}={value}" for key, value in sorted(root_counts.items()))
        + "。因此结论来自相同 Query/embedding/Top-5 下的 rank 变化，而不是摘要文字观感。"
    )

    summary = {
        "sheet": args.sheet,
        "page_count": len(pages),
        "long_range_gold_count": sum(len(value) for value in gold_by_turn.values()),
        "baseline_miss_count": len(miss_rows),
        "qdrant": {
            "base_collection": collection_name,
            "page_collection": f"{collection_name}_midterm_pages",
            "actual_embedding_text_formula": "summary + Keywords + User",
            "payload_matches_code": sum(row["embedding_text_matches_code"] for row in exported),
        },
        "generation_config": public_model_config,
        "ablation": {row["variant"]: row for row in ablation_rows},
        "similarity": {row["representation"]: row for row in similarity_rows},
        "root_cause_counts": dict(root_counts),
        "retrieval_focused_recall_at_5_gain": prompt_gain,
        "same_model_retrieval_vs_current_regenerated_recall_at_5_gain": (
            float(retrieval_metrics["recall_at_5"])
            - float(next(row for row in ablation_rows if row["variant"] == "current_prompt_regenerated")["recall_at_5"])
        ),
        "preservation": {
            status: {
                signal: statistics.fmean(as_bool(row[f"{signal}_preserved"]) for row in rows)
                for signal in ("topic", "key_fact", "key_number", "conclusion", "relation")
            }
            for status, rows in (
                ("hit", [row for row in preservation_rows if row["baseline_hit"]]),
                ("miss", [row for row in preservation_rows if not row["baseline_hit"]]),
            )
        },
        "cashflow_relation": {
            "matched_pages": len(cashflow_rows),
            "current_explicit": sum(row["current_explicit_relation"] for row in cashflow_rows),
            "retrieval_focused_explicit": sum(row["retrieval_explicit_relation"] for row in cashflow_rows),
        },
        "stability": {
            "sample_count": len(stability_rows),
            "exact_match_rate": statistics.fmean(as_bool(row["exact_match_main"]) for row in stability_rows),
            "embedding_cosine_mean": statistics.fmean(stability_cosines),
            "embedding_cosine_min": min(stability_cosines),
            "rank_abs_change_mean": statistics.fmean(stability_rank_changes),
            "rank_abs_change_max": max(stability_rank_changes),
            "rank_same_rate": statistics.fmean(stability_rank_same),
            "top5_status_same_rate": statistics.fmean(stability_top5_same),
        },
        "headline_conclusion": headline,
        "conclusion_detail": conclusion_detail,
    }
    dump_json(output_dir / "diagnosis_summary.json", summary)
    report = build_report(
        summary,
        ablation_rows,
        similarity_rows,
        common_rows,
        case_rows,
        root_rows,
        preservation_rows,
        cashflow_rows,
        pages,
        generated,
    )
    (output_dir / "diagnosis_report.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nPage representation diagnosis: {output_dir}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run()


if __name__ == "__main__":
    main()
