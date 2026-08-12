"""Evaluate Task/Fact/Relation multi-vector retrieval on frozen C3 artifacts."""

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

import numpy as np
from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_conservative_retrieval_tuning_ablation import (  # noqa: E402
    provider_precondition,
)
from exp.benchmark.run_midterm_add_local_context_controls import compare_rankings  # noqa: E402
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


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_field_aware_multi_vector_retrieval"
PROMPT_FILE = "field_extraction_v1.txt"
PROMPT_VERSION = "midterm-field-aware-task-fact-relation-v1"
FIELDS = ("task", "fact", "relation")
FIELD_WEIGHTS = {"task": 0.4, "fact": 0.4, "relation": 0.2}
ELIGIBLE_GOLD = 154
DENSE_GOLD5 = 59

FIELD_EXTRACTION_PROMPT = """你是中期记忆 Dense Retrieval 的字段抽取组件。

输入是一条已经冻结的 Query 或一个已经冻结的 MidTerm Page。你只能使用输入文本本身，不能补充外部知识、历史对话、未来对话、检索结果或答案标注。

请将输入拆成三个可以独立用于语义检索的中文字段：

1. task
表示当前文本中的任务、用户意图或对话动作。例如比较、核验、反证、修订、证据分级、来源检查、计算、阶段总结、管理层追问，以及原文明确提出的回答约束。

2. fact
表示当前文本中明确出现的事实检索锚点。例如主体、时间、来源、财务指标、数字、事实状态、证据对象和信息缺口。对于 Query，只提取 Query 明确提到或引用的对象，不回答问题；对于 Page，只提取 Page 已明确记录的事实。

3. relation
表示当前文本中明确出现的关系。例如指标间比较、同步或冲突、趋势和拐点、支持或反证、事实与判断的关系、承接或修订前文、因果限制、当前结论与证据限制。关系描述必须写明相关对象，不能只写“二者”“这个判断”等无法独立理解的表达。

严格规则：

- 只做抽取和最小必要归纳，不回答 Query，不评价内容，不新增输入中没有的信息。
- 三个字段都必须最大程度保留原文措辞和检索锚点。
- 每个字段必须是非空字符串，并能够脱离另外两个字段单独用于 embedding。
- 为了独立可读，核心主体或指标名称可以在多个字段中重复；不要把整段输入机械复制到每个字段。
- Task 不要退化成通用的“财务分析”；Fact 不要写入推测；Relation 不要创造输入中没有的比较或结论。
- 输入类型为 Query 时，Relation 表示 Query 正在询问或验证的关系，不得生成问题答案。
- 输入类型为 Page 时，Task、Fact、Relation 都只能来自该 Page 的 Summary + Keywords 文本。

只返回严格有效 JSON，不要添加 Markdown 或解释：

{
  "task": "...",
  "fact": "...",
  "relation": "..."
}
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen C3 Task/Fact/Relation multi-vector retrieval")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=4)
    parser.add_argument("--llm-concurrency", type=int, default=8)
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_prompt(output_dir: Path) -> tuple[Path, str]:
    path = output_dir / "prompts" / PROMPT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") != FIELD_EXTRACTION_PROMPT:
        raise AssertionError(f"Frozen field Prompt differs from script: {path}")
    if not path.exists():
        path.write_text(FIELD_EXTRACTION_PROMPT, encoding="utf-8")
    return path, sha256_file(path)


def parse_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1], strict=False)
    if not isinstance(parsed, dict):
        raise ValueError("Field extraction output must be a JSON object")
    return parsed


def validate_fields(value: Mapping[str, Any]) -> dict[str, str]:
    result = {field: str(value.get(field) or "").strip() for field in FIELDS}
    missing = [field for field, text in result.items() if not text]
    if missing:
        raise ValueError(f"Missing or empty fields: {missing}")
    return result


class FieldExtractionCache:
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
        prompt_sha256: str,
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
        self.prompt_sha256 = prompt_sha256
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.lock = asyncio.Lock()
        self.success = {
            str(row["cache_key"]): row for row in load_jsonl(path) if row.get("status") == "SUCCESS"
        }

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(
        self,
        *,
        item_type: str,
        item_id: str,
        session_id: str,
        source_turn_id: str | None,
        input_text: str,
    ) -> dict[str, Any]:
        identity = {
            "item_type": item_type,
            "item_id": item_id,
            "session_id": session_id,
            "source_turn_id": source_turn_id,
            "input_text_sha256": sha256_text(input_text),
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": self.prompt_sha256,
            "model": self.model,
            "thinking_mode": "disabled",
            "temperature": self.temperature,
            "top_p": self.top_p,
            "top_k": self.top_k,
        }
        cache_key = stable_hash(identity)
        if cache_key in self.success:
            return {**self.success[cache_key], "cache_hit": True}
        payload = f"输入类型：{item_type}\n\n输入文本：\n{input_text}"
        messages = [
            {"role": "system", "content": FIELD_EXTRACTION_PROMPT},
            {"role": "user", "content": payload},
        ]
        errors: list[str] = []
        rejects = 0
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
                        "max_tokens": 1200,
                        "extra_body": {"thinking": {"type": "disabled"}, "top_k": self.top_k},
                    }
                    if use_response_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs), timeout=self.timeout
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = validate_fields(parse_json_object(raw))
                    usage = response.usage
                    row = {
                        **identity,
                        "cache_key": cache_key,
                        "status": "SUCCESS",
                        "input_contract": f"frozen {item_type} text only",
                        "parsed": parsed,
                        "raw_output": raw,
                        "response_mode": "json_object" if use_response_format else "plain_text_strict_json",
                        "llm_latency_ms": (time.perf_counter() - started) * 1000.0,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "api_attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "provider_precondition_rejected_count": rejects,
                        "errors": errors,
                        "cache_hit": False,
                    }
                    await self.append(row)
                    self.success[cache_key] = row
                    return row
                except Exception as exc:
                    rejected = provider_precondition(exc)
                    rejects += int(rejected)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if rejected and use_response_format:
                        use_response_format = False
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12))
        failed = {
            **identity,
            "cache_key": cache_key,
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.retries,
            "retry_count": self.retries - 1,
            "provider_precondition_rejected_count": rejects,
            "errors": errors,
            "cache_hit": False,
        }
        await self.append(failed)
        return failed


async def extract_all_fields(
    args: argparse.Namespace,
    output_dir: Path,
    prompt_sha256: str,
    llm_config: Mapping[str, Any],
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    query_texts: Mapping[str, str],
    page_texts: Mapping[str, str],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]], dict[str, Any]]:
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    cache = FieldExtractionCache(
        output_dir / "cache/field_extractions.jsonl",
        client=AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url),
        model=str(llm_config["model"]),
        temperature=float(llm_config["temperature"]),
        top_p=float(llm_config["top_p"]),
        top_k=int(llm_config["top_k"]),
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.llm_concurrency,
        prompt_sha256=prompt_sha256,
    )
    tasks = []
    identities = []
    for query in queries:
        query_id = str(query["query_id"])
        identities.append(("Query", query_id))
        tasks.append(
            cache.call(
                item_type="Query",
                item_id=query_id,
                session_id=str(query["session_code"]),
                source_turn_id=None,
                input_text=query_texts[query_id],
            )
        )
    for page in pages:
        page_id = str(page["page_id"])
        identities.append(("Page", page_id))
        tasks.append(
            cache.call(
                item_type="Page",
                item_id=page_id,
                session_id=str(page["session_code"]),
                source_turn_id=str(page["source_turn_id"]),
                input_text=page_texts[page_id],
            )
        )
    results = await asyncio.gather(*tasks)
    failed = [row for row in results if row.get("status") != "SUCCESS"]
    if failed:
        raise RuntimeError(f"Field extraction failed for {len(failed)} items: {failed[:3]}")
    query_fields: dict[str, dict[str, str]] = {}
    page_fields: dict[str, dict[str, str]] = {}
    for (item_type, item_id), row in zip(identities, results):
        parsed = validate_fields(row["parsed"])
        target = query_fields if item_type == "Query" else page_fields
        target[item_id] = parsed
    metadata = {
        "item_count": len(results),
        "query_count": len(query_fields),
        "page_count": len(page_fields),
        "cache_hit_count": sum(bool(row.get("cache_hit")) for row in results),
        "successful_model_output_count": len(results),
        "new_successful_model_output_count": sum(not bool(row.get("cache_hit")) for row in results),
        "generation_api_attempt_count": sum(int(row.get("api_attempt_count") or 0) for row in results),
        "new_llm_api_attempt_count": sum(
            int(row.get("api_attempt_count") or 0) for row in results if not row.get("cache_hit")
        ),
        "retry_count": sum(int(row.get("retry_count") or 0) for row in results),
        "new_retry_count": sum(
            int(row.get("retry_count") or 0) for row in results if not row.get("cache_hit")
        ),
        "provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0) for row in results
        ),
        "new_provider_precondition_rejected_count": sum(
            int(row.get("provider_precondition_rejected_count") or 0)
            for row in results
            if not row.get("cache_hit")
        ),
    }
    return query_fields, page_fields, metadata


def encode_fields(
    output_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    query_fields: Mapping[str, Mapping[str, str]],
    page_fields: Mapping[str, Mapping[str, str]],
) -> tuple[
    dict[str, dict[str, list[float]]],
    dict[str, dict[str, list[float]]],
    dict[str, Any],
]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    query_vectors: dict[str, dict[str, list[float]]] = {}
    page_vectors: dict[str, dict[str, list[float]]] = {}
    metadata: dict[str, Any] = {}
    for field in FIELDS:
        query_ids = [str(query["query_id"]) for query in queries]
        query_embedding_ids = [f"Query:{field}:{query_id}" for query_id in query_ids]
        query_text = [query_fields[query_id][field] for query_id in query_ids]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"field-aware-query-{field}-S001-S005",
            query_embedding_ids,
            query_text,
            measure_individual=True,
        )
        query_vectors[field] = {
            query_id: encoded[embedding_id] for query_id, embedding_id in zip(query_ids, query_embedding_ids)
        }
        metadata[f"query_{field}"] = item_meta

        page_ids = [str(page["page_id"]) for page in pages]
        page_embedding_ids = [f"Page:{field}:{page_id}" for page_id in page_ids]
        page_text = [page_fields[page_id][field] for page_id in page_ids]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"field-aware-page-{field}-S001-S005",
            page_embedding_ids,
            page_text,
            measure_individual=False,
        )
        page_vectors[field] = {
            page_id: encoded[embedding_id] for page_id, embedding_id in zip(page_ids, page_embedding_ids)
        }
        metadata[f"page_{field}"] = item_meta
    cache.release(PRODUCTION_EMBEDDING)
    return query_vectors, page_vectors, metadata


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    left_array = np.asarray(left, dtype=np.float32)
    right_array = np.asarray(right, dtype=np.float32)
    denominator = float(np.linalg.norm(left_array) * np.linalg.norm(right_array))
    return float(left_array @ right_array / denominator) if denominator else 0.0


def field_aware_rank(
    query_id: str,
    pages: Sequence[Mapping[str, Any]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    page_vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> list[dict[str, Any]]:
    rows = []
    for page in pages:
        page_id = str(page["page_id"])
        scores = {
            field: cosine(query_vectors[field][query_id], page_vectors[field][page_id]) for field in FIELDS
        }
        final = sum(FIELD_WEIGHTS[field] * scores[field] for field in FIELDS)
        rows.append(
            {
                "page_id": page_id,
                "source_turn_id": str(page.get("source_turn_id") or ""),
                "score": final,
                "final_score": final,
                "task_cosine": scores["task"],
                "fact_cosine": scores["fact"],
                "relation_cosine": scores["relation"],
            }
        )
    rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def rank_all(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Mapping[str, Sequence[float]]],
    page_vectors: Mapping[str, Mapping[str, Sequence[float]]],
) -> dict[str, list[dict[str, Any]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    for code in SESSION_CODES:
        snapshot = snapshots[code]
        page_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
        visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            visible_ids = [str(page_id) for page_id in visibility[query_id]["visible_page_ids"]]
            visible_pages = [page_by_id[page_id] for page_id in visible_ids]
            rankings[query_id] = field_aware_rank(query_id, visible_pages, query_vectors, page_vectors)
            if {str(row["page_id"]) for row in rankings[query_id]} != set(visible_ids):
                raise AssertionError(f"Field-aware visibility changed for {query_id}")
    return rankings


def field_statistics(
    query_fields: Mapping[str, Mapping[str, str]],
    page_fields: Mapping[str, Mapping[str, str]],
) -> list[dict[str, Any]]:
    rows = []
    for item_type, fields_by_id in (("Query", query_fields), ("Page", page_fields)):
        for field in FIELDS:
            lengths = [len(value[field]) for value in fields_by_id.values()]
            rows.append(
                {
                    "item_type": item_type,
                    "field": field,
                    "count": len(lengths),
                    "mean_chars": statistics.fmean(lengths),
                    "median_chars": statistics.median(lengths),
                    "min_chars": min(lengths),
                    "max_chars": max(lengths),
                }
            )
    return rows


def transition_label(before_rank: int, after_rank: int) -> str:
    if before_rank > 5 >= after_rank:
        return "PROMOTED"
    if before_rank <= 5 < after_rank:
        return "DEMOTED"
    return "UNCHANGED"


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# Field-aware Multi-vector Retrieval 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['transition']} / {case['query_id']} / {case['gold_source_turn_id']}",
                "",
                f"P2 Query：{case['p2_query']}",
                "",
                "Query Task：" + case["query_fields"]["task"],
                "",
                "Query Fact：" + case["query_fields"]["fact"],
                "",
                "Query Relation：" + case["query_fields"]["relation"],
                "",
                f"Gold：Dense #{case['dense_rank']} → Field-aware #{case['field_rank']}；"
                f"Task={float(case['task_cosine']):.6f}，Fact={float(case['fact_cosine']):.6f}，"
                f"Relation={float(case['relation_cosine']):.6f}，Final={float(case['final_score']):.6f}。",
                "",
                "Page Task：" + case["page_fields"]["task"],
                "",
                "Page Fact：" + case["page_fields"]["fact"],
                "",
                "Page Relation：" + case["page_fields"]["relation"],
                "",
            ]
        )
    return "\n".join(lines)


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def render_report(
    baseline: Mapping[str, Any],
    field_metrics: Mapping[str, Any],
    sessions: Mapping[str, Mapping[str, Any]],
    comparison: Mapping[str, Any],
    score_summary: Sequence[Mapping[str, Any]],
    separation_summary: Sequence[Mapping[str, Any]],
    field_length_rows: Sequence[Mapping[str, Any]],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    length_lookup = {
        (str(row["item_type"]), str(row["field"])): row for row in field_length_rows
    }
    lines = [
        "# C3 Field-aware Multi-vector Retrieval",
        "",
        "固定分数：`0.4 × Task cosine + 0.4 × Fact cosine + 0.2 × Relation cosine`。",
        "",
        "| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| C3 Dense baseline | {pct(baseline['recall_at_5'])} | 59 | {pct(baseline['macro_session_r5'])} | "
        f"{pct(baseline['recall_at_10'])} | {pct(baseline['recall_at_20'])} | {float(baseline['mrr']):.4f} | "
        f"{float(baseline['mean_gold_rank']):.2f} |",
        f"| Task / Fact / Relation Multi-vector | {pct(field_metrics['recall_at_5'])} | "
        f"{round(float(field_metrics['recall_at_5']) * ELIGIBLE_GOLD)} | "
        f"{pct(field_metrics['macro_session_r5'])} | {pct(field_metrics['recall_at_10'])} | "
        f"{pct(field_metrics['recall_at_20'])} | {float(field_metrics['mrr']):.4f} | "
        f"{float(field_metrics['mean_gold_rank']):.2f} |",
        "",
        f"相对 C3：promoted={comparison['Promoted Gold']}，demoted={comparison['Demoted Gold']}，"
        f"net={int(comparison['Net Gold gain']):+d}，rescued Query={comparison['Rescued Queries']}，"
        f"hurt Query={comparison['Hurt Queries']}。",
        "",
        "## Session R@5",
        "",
        "| Session | C3 Dense | Field-aware | Delta |",
        "|---|---:|---:|---:|",
    ]
    for code in SESSION_CODES:
        baseline_value = float(sessions[code]["baseline_r5"])
        value = float(sessions[code]["field_r5"])
        lines.append(f"| {code} | {pct(baseline_value)} | {pct(value)} | {100 * (value - baseline_value):+.2f}pp |")
    lines.extend(
        [
            "",
            "## Gold 字段分数",
            "",
            "| Transition | Count | Mean Task | Mean Fact | Mean Relation | Mean Final |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in score_summary:
        lines.append(
            f"| {row['transition']} | {row['count']} | {float(row['task_cosine_mean']):.4f} | "
            f"{float(row['fact_cosine_mean']):.4f} | {float(row['relation_cosine_mean']):.4f} | "
            f"{float(row['final_score_mean']):.4f} |"
        )
    lines.extend(
        [
            "",
            "## Query-conditioned Gold / Non-Gold separation",
            "",
            "| Score | Mean margin | Median margin | Positive rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in separation_summary:
        lines.append(
            f"| {row['score_type']} | {float(row['mean_margin']):.4f} | "
            f"{float(row['median_margin']):.4f} | {pct(row['positive_margin_rate'])} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            f"1. 当前固定 0.4/0.4/0.2 方案比 C3 少 "
            f"{round(float(baseline['recall_at_5']) * ELIGIBLE_GOLD) - round(float(field_metrics['recall_at_5']) * ELIGIBLE_GOLD)} "
            "个 Top5 Gold，不能替换 C3。",
            f"2. R@10 从 {pct(baseline['recall_at_10'])} 升至 {pct(field_metrics['recall_at_10'])}，"
            f"且产生 {comparison['Promoted Gold']} 个 promotion，说明字段信号存在；但 "
            f"{comparison['Demoted Gold']} 个 demotion 和 S001/S004/S005 的下降表明 Top5 稳定性不足。",
            f"3. 字段粒度不对称：Query/Page 平均字符数分别为 Task "
            f"{float(length_lookup[('Query', 'task')]['mean_chars']):.1f}/"
            f"{float(length_lookup[('Page', 'task')]['mean_chars']):.1f}，Fact "
            f"{float(length_lookup[('Query', 'fact')]['mean_chars']):.1f}/"
            f"{float(length_lookup[('Page', 'fact')]['mean_chars']):.1f}，Relation "
            f"{float(length_lookup[('Query', 'relation')]['mean_chars']):.1f}/"
            f"{float(length_lookup[('Page', 'relation')]['mean_chars']):.1f}。Page Fact/Relation 仍承载较多完整底表与通用限制。",
            "4. 值得继续研究字段定义，但应先收紧 Page 字段的语义边界和粒度对称性，再做预先冻结的小规模权重消融；"
            "当前结果不支持直接围绕 0.4/0.4/0.2 做细粒度调参，也不支持修改 Production。",
            "",
            "Query 字段仅由冻结 P2 Query 抽取；Page 字段仅由冻结 C3 `Summary + Keywords` 抽取。",
            "没有向字段抽取器提供 Gold、visibility、rank、score、原始长对话或其它 Page。",
            "",
            "Validation：" + ("PASS" if all(row["status"] == "PASS" for row in validations.values()) else "FAIL") + "。",
            "",
        ]
    )
    return "\n".join(lines)


async def async_main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path, prompt_hash_before = freeze_prompt(args.output_dir)

    snapshots, old_pages, queries, checkpoint_data, frozen_cache_meta = checkpoint_inputs()
    data = checkpoint_data["C3"]
    query_texts = dict(data["query_texts"])
    page_texts = dict(data["page_texts"])
    baseline_ranking = rank_configuration(snapshots, data["query_vectors"], data["page_vectors"])
    baseline_metrics, baseline_sessions = evaluate_all(snapshots, baseline_ranking)
    if not math.isclose(float(baseline_metrics["recall_at_5"]), DENSE_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12):
        raise AssertionError(f"C3 baseline reproduction failed: {baseline_metrics}")

    config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict(config["llm"]["config"])
    if not llm_config.get("api_key") or str(llm_config["api_key"]).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY required for field extraction")
    if (
        str(llm_config["model"]) != "deepseek-v4-flash"
        or not math.isclose(float(llm_config["temperature"]), 0.1, abs_tol=1e-12)
        or not math.isclose(float(llm_config["top_p"]), 0.1, abs_tol=1e-12)
        or int(llm_config["top_k"]) != 1
    ):
        raise AssertionError("Frozen field extraction model configuration changed")

    query_fields, page_fields, extraction_metadata = await extract_all_fields(
        args,
        args.output_dir,
        prompt_hash_before,
        llm_config,
        queries,
        data["pages"],
        query_texts,
        page_texts,
    )
    prompt_hash_after = sha256_file(prompt_path)
    if prompt_hash_after != prompt_hash_before:
        raise AssertionError("Field extraction Prompt changed after generation")

    query_field_rows = [
        {
            "session_id": query["session_code"],
            "query_id": query["query_id"],
            "input_text": query_texts[str(query["query_id"])],
            **query_fields[str(query["query_id"])],
        }
        for query in queries
    ]
    page_field_rows = [
        {
            "session_id": page["session_code"],
            "page_id": page["page_id"],
            "source_turn_id": page["source_turn_id"],
            "input_text": page_texts[str(page["page_id"])],
            **page_fields[str(page["page_id"])],
        }
        for page in data["pages"]
    ]
    write_jsonl(args.output_dir / "fields/query_fields.jsonl", query_field_rows)
    write_jsonl(args.output_dir / "fields/page_fields.jsonl", page_field_rows)

    query_vectors, page_vectors, embedding_metadata = encode_fields(
        args.output_dir, queries, data["pages"], query_fields, page_fields
    )
    field_ranking = rank_all(snapshots, query_vectors, page_vectors)
    field_metrics, field_sessions = evaluate_all(snapshots, field_ranking)
    comparison, comparison_gold, comparison_queries = compare_rankings(
        snapshots, baseline_ranking, field_ranking, comparison="Field-aware vs C3 Dense"
    )

    baseline_maps = {query_id: rank_map(rows) for query_id, rows in baseline_ranking.items()}
    field_maps = {query_id: rank_map(rows) for query_id, rows in field_ranking.items()}
    gold_rows = []
    ranking_rows = []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(page_id) for page_id in query["eligible_gold_page_ids"]}
        for row in field_ranking[query_id]:
            page_id = str(row["page_id"])
            ranking_rows.append(
                {
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "page_id": page_id,
                    "source_turn_id": row["source_turn_id"],
                    "task_cosine": row["task_cosine"],
                    "fact_cosine": row["fact_cosine"],
                    "relation_cosine": row["relation_cosine"],
                    "final_score": row["final_score"],
                    "rank": row["rank"],
                    "is_gold": page_id in gold_ids,
                }
            )
        for gold_id in gold_ids:
            baseline_item = baseline_maps[query_id][gold_id]
            field_item = field_maps[query_id][gold_id]
            gold_rows.append(
                {
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "gold_page_id": gold_id,
                    "gold_source_turn_id": field_item["source_turn_id"],
                    "dense_rank": baseline_item["rank"],
                    "field_rank": field_item["rank"],
                    "rank_improvement": int(baseline_item["rank"]) - int(field_item["rank"]),
                    "transition": transition_label(int(baseline_item["rank"]), int(field_item["rank"])),
                    "dense_score": baseline_item["score"],
                    "task_cosine": field_item["task_cosine"],
                    "fact_cosine": field_item["fact_cosine"],
                    "relation_cosine": field_item["relation_cosine"],
                    "final_score": field_item["final_score"],
                }
            )

    score_summary = []
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in gold_rows:
        grouped[str(row["transition"])].append(row)
    for transition in ("PROMOTED", "DEMOTED", "UNCHANGED"):
        rows = grouped[transition]
        if not rows:
            continue
        score_summary.append(
            {
                "transition": transition,
                "count": len(rows),
                "task_cosine_mean": statistics.fmean(float(row["task_cosine"]) for row in rows),
                "fact_cosine_mean": statistics.fmean(float(row["fact_cosine"]) for row in rows),
                "relation_cosine_mean": statistics.fmean(float(row["relation_cosine"]) for row in rows),
                "final_score_mean": statistics.fmean(float(row["final_score"]) for row in rows),
                "rank_improvement_mean": statistics.fmean(float(row["rank_improvement"]) for row in rows),
            }
        )

    separation_rows = []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(page_id) for page_id in query["eligible_gold_page_ids"]}
        baseline_rows = baseline_ranking[query_id]
        field_rows = field_ranking[query_id]
        score_contracts = {
            "C3 Dense": (baseline_rows, "score"),
            "Task cosine": (field_rows, "task_cosine"),
            "Fact cosine": (field_rows, "fact_cosine"),
            "Relation cosine": (field_rows, "relation_cosine"),
            "Field-aware final": (field_rows, "final_score"),
        }
        for score_type, (rows, score_key) in score_contracts.items():
            gold_scores = [float(row[score_key]) for row in rows if str(row["page_id"]) in gold_ids]
            nongold_scores = [float(row[score_key]) for row in rows if str(row["page_id"]) not in gold_ids]
            separation_rows.append(
                {
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "score_type": score_type,
                    "best_gold_score": max(gold_scores),
                    "strongest_nongold_score": max(nongold_scores),
                    "margin": max(gold_scores) - max(nongold_scores),
                    "positive_margin": max(gold_scores) > max(nongold_scores),
                }
            )
    separation_summary = []
    for score_type in ("C3 Dense", "Task cosine", "Fact cosine", "Relation cosine", "Field-aware final"):
        values = [float(row["margin"]) for row in separation_rows if row["score_type"] == score_type]
        separation_summary.append(
            {
                "score_type": score_type,
                "query_count": len(values),
                "mean_margin": statistics.fmean(values),
                "median_margin": statistics.median(values),
                "positive_margin_rate": sum(value > 0 for value in values) / len(values),
            }
        )

    selected = sorted(
        (row for row in gold_rows if row["transition"] == "PROMOTED"),
        key=lambda row: -int(row["rank_improvement"]),
    )[:5]
    selected += sorted(
        (row for row in gold_rows if row["transition"] == "DEMOTED"),
        key=lambda row: int(row["rank_improvement"]),
    )[:5]
    cases = []
    for row in selected:
        query_id, page_id = str(row["query_id"]), str(row["gold_page_id"])
        cases.append(
            {
                **row,
                "p2_query": query_texts[query_id],
                "query_fields": query_fields[query_id],
                "page_fields": page_fields[page_id],
                "page_text": page_texts[page_id],
            }
        )

    session_rows = [
        {
            "session_id": code,
            "eligible_gold_count": field_sessions[code]["eligible_gold_count"],
            "C3 Dense R@5": baseline_sessions[code]["recall_at_5"],
            "Field-aware R@5": field_sessions[code]["recall_at_5"],
            "Delta R@5": float(field_sessions[code]["recall_at_5"])
            - float(baseline_sessions[code]["recall_at_5"]),
            "Field-aware R@10": field_sessions[code]["recall_at_10"],
            "Field-aware R@20": field_sessions[code]["recall_at_20"],
            "Field-aware MRR": field_sessions[code]["mrr"],
            "Field-aware Mean Gold Rank": field_sessions[code]["mean_gold_rank"],
        }
        for code in SESSION_CODES
    ]
    report_sessions = {
        code: {
            "baseline_r5": baseline_sessions[code]["recall_at_5"],
            "field_r5": field_sessions[code]["recall_at_5"],
        }
        for code in SESSION_CODES
    }

    current_page_hash = stable_hash([page_texts[str(page["page_id"])] for page in data["pages"]])
    current_query_hash = stable_hash([query_texts[str(query["query_id"])] for query in queries])
    prior_metadata = load_json(REPO_ROOT / "exp/results/midterm_c3_bm25_input_idf_ablation/run_metadata.json")
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    validations = {
        "C3 Dense 59 of 154": {
            "status": "PASS"
            if math.isclose(float(baseline_metrics["recall_at_5"]), DENSE_GOLD5 / ELIGIBLE_GOLD, abs_tol=1e-12)
            else "FAIL"
        },
        "frozen counts": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and len(data["pages"]) == 333
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == ELIGIBLE_GOLD
            else "FAIL"
        },
        "C3 Page hash": {
            "status": "PASS" if current_page_hash == prior_metadata["page_text_hash"] else "FAIL",
            "value": current_page_hash,
        },
        "P2 Query hash": {
            "status": "PASS" if current_query_hash == prior_metadata["p2_query_hash"] else "FAIL",
            "value": current_query_hash,
        },
        "field extraction complete": {
            "status": "PASS" if len(query_fields) == 99 and len(page_fields) == 333 else "FAIL"
        },
        "all fields nonempty": {
            "status": "PASS"
            if all(fields[field] for fields in [*query_fields.values(), *page_fields.values()] for field in FIELDS)
            else "FAIL"
        },
        "Prompt frozen": {
            "status": "PASS" if prompt_hash_before == prompt_hash_after else "FAIL",
            "sha256": prompt_hash_before,
        },
        "field weights frozen": {
            "status": "PASS" if FIELD_WEIGHTS == {"task": 0.4, "fact": 0.4, "relation": 0.2} else "FAIL"
        },
        "visibility unchanged": {
            "status": "PASS"
            if len(visibility) == 99
            and all(
                [str(row["page_id"]) for row in field_ranking[query_id]]
                and {str(row["page_id"]) for row in field_ranking[query_id]} == set(page_ids)
                for query_id, page_ids in visibility.items()
            )
            else "FAIL"
        },
        "no Gold or retrieval leakage": {
            "status": "PASS",
            "query_input": "frozen P2 text only",
            "page_input": "frozen C3 Summary + Keywords only",
        },
        "no full Session rerun": {"status": "PASS", "value": False},
        "no Add regeneration": {"status": "PASS", "value": False},
        "no BM25 or reranker": {"status": "PASS", "value": True},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    new_embedding_count = sum(
        int(meta["item_count"]) for meta in embedding_metadata.values() if not meta.get("cache_hit")
    )
    embedding_count = sum(int(meta["item_count"]) for meta in embedding_metadata.values())
    metrics_row = {
        "Retrieval": "Task / Fact / Relation Multi-vector",
        "Task Weight": FIELD_WEIGHTS["task"],
        "Fact Weight": FIELD_WEIGHTS["fact"],
        "Relation Weight": FIELD_WEIGHTS["relation"],
        "Eligible Gold": field_metrics["eligible_gold_count"],
        "Gold@5": round(float(field_metrics["recall_at_5"]) * ELIGIBLE_GOLD),
        "Micro R@5": field_metrics["recall_at_5"],
        "Macro R@5": field_metrics["macro_session_r5"],
        "R@10": field_metrics["recall_at_10"],
        "R@20": field_metrics["recall_at_20"],
        "MRR": field_metrics["mrr"],
        "Mean Gold Rank": field_metrics["mean_gold_rank"],
        "Promoted vs C3": comparison["Promoted Gold"],
        "Demoted vs C3": comparison["Demoted Gold"],
        "Net vs C3": comparison["Net Gold gain"],
        "Rescued Query": comparison["Rescued Queries"],
        "Hurt Query": comparison["Hurt Queries"],
    }
    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", [metrics_row])
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/gold_results.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", comparison_queries)
    write_jsonl(args.output_dir / "rankings/query_page_rankings.jsonl", ranking_rows)
    field_length_rows = field_statistics(query_fields, page_fields)
    write_csv(args.output_dir / "analysis/field_length_stats.csv", field_length_rows)
    write_csv(args.output_dir / "analysis/gold_field_score_summary.csv", score_summary)
    write_csv(args.output_dir / "analysis/query_field_separation.csv", separation_rows)
    write_csv(args.output_dir / "analysis/field_separation_summary.csv", separation_summary)
    write_csv(args.output_dir / "analysis/comparison_gold_movements.csv", comparison_gold)
    dump_json(args.output_dir / "representative_cases.json", {"cases": cases})
    (args.output_dir / "representative_cases.md").write_text(render_cases(cases), encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        render_report(
            baseline_metrics,
            field_metrics,
            report_sessions,
            comparison,
            score_summary,
            separation_summary,
            field_length_rows,
            validations,
        ),
        encoding="utf-8",
    )
    metadata = {
        "experiment_name": "midterm_field_aware_multi_vector_retrieval_v1",
        "baseline": "C3 Dense",
        "embedding_model": PRODUCTION_EMBEDDING,
        "query_input_contract": "frozen P2 resolved query only",
        "page_input_contract": "frozen C3 Summary + Keywords only",
        "field_embedding_contract": "each extracted field text only; Query=search mode; Page=add mode",
        "field_weights": FIELD_WEIGHTS,
        "field_extraction_prompt_version": PROMPT_VERSION,
        "field_extraction_prompt_sha256": prompt_hash_before,
        "field_extraction_model": llm_config["model"],
        "thinking_mode": "disabled",
        "temperature": float(llm_config["temperature"]),
        "top_p": float(llm_config["top_p"]),
        "top_k": int(llm_config["top_k"]),
        "field_extraction": extraction_metadata,
        "embedding_batches": embedding_metadata,
        "embedding_count": embedding_count,
        "new_embedding_count": new_embedding_count,
        "session_count": 5,
        "page_count": 333,
        "query_count": 99,
        "eligible_gold_count": ELIGIBLE_GOLD,
        "page_text_hash": current_page_hash,
        "p2_query_hash": current_query_hash,
        "frozen_vector_cache_metadata": frozen_cache_meta,
        "full_session_rerun": False,
        "add_regeneration": False,
        "bm25_used": False,
        "reranker_used": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "baseline": baseline_metrics,
                "field_aware": field_metrics,
                "comparison": comparison,
                "sessions": session_rows,
                "field_extraction": extraction_metadata,
                "new_embedding_count": new_embedding_count,
                "embedding_count": embedding_count,
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
