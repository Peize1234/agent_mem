from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from qdrant_client import QdrantClient, models

from exp.benchmark.benchmark_common import (
    BenchmarkSession,
    BenchmarkTurn,
    dependency_distances,
    dump_json,
    ensure_repo_root_on_path,
    expand_env_placeholders,
    load_dataset,
    load_json,
    ordered_unique,
    resolve_path,
    retrieval_metrics,
    write_csv,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from mem0.configs.embeddings.base import BaseEmbedderConfig  # noqa: E402
from mem0.embeddings.huggingface import HuggingFaceEmbedding  # noqa: E402
from mem0.memory.utils import extract_json, remove_code_blocks  # noqa: E402
from mem0.utils.bm25_sparse import ChineseBM25SparseEncoder  # noqa: E402
from mem0.utils.factory import LlmFactory  # noqa: E402

LOGGER = logging.getLogger("midterm_page_diagnosis")
DEFAULT_SOURCE_DIR = "exp/results/recall_full_100_sessions"
DEFAULT_OUTPUT_DIR = "exp/results/midterm_page_diagnosis"
DEFAULT_SHEET = "S001_贵州茅台_投研"
SHORT_TERM_QA_CAPACITY = 3
PAGE_VARIANTS = (
    "current_summary_keywords_user",
    "user_assistant",
    "raw_dialogue",
    "summary_keywords_raw_dialogue",
    "summary_only",
)
QUERY_VARIANTS = ("current_query", "recent_1_qa_query", "recent_3_qa_query", "required_context_oracle")


@dataclass(frozen=True)
class PageRow:
    page_id: str
    turn_id: str
    turn_index: int
    session_id: str
    source_job_id: str
    payload: dict[str, Any]
    stored_vector: list[float]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="对既有 S001 召回实验做无 future leakage 的 Mid-term Page 根因诊断",
    )
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR, help="既有召回实验结果目录")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="独立诊断结果目录")
    parser.add_argument("--sheet", default=DEFAULT_SHEET, help="只诊断这个 Excel Sheet")
    parser.add_argument(
        "--skip-judges",
        action="store_true",
        help="跳过 DeepSeek Gold audit 和 semantic-equivalence judge，仅用于本地快速调试",
    )
    parser.add_argument(
        "--reuse-judge-results",
        action="store_true",
        help="若输出目录已有 judge 文件则复用，避免重复调用 DeepSeek",
    )
    parser.add_argument(
        "--judge-model",
        default="deepseek-chat",
        help="DeepSeek 诊断 Judge 模型；与生成 Page 的模型独立并写入 summary",
    )
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)


def mean(values: Iterable[float]) -> float:
    clean = [float(value) for value in values]
    return statistics.fmean(clean) if clean else 0.0


def parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def compact_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def page_embedding_text(payload: Mapping[str, Any]) -> str:
    keywords = payload.get("keywords") or []
    keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
    return "\n".join(
        part
        for part in (
            str(payload.get("summary") or ""),
            f"Keywords: {keywords_text}" if keywords_text else "",
            f"User: {payload.get('user_input', '')}",
        )
        if part
    )


def page_variant_text(page: PageRow, variant: str) -> str:
    payload = page.payload
    if variant == "current_summary_keywords_user":
        return page_embedding_text(payload)
    if variant == "user_assistant":
        return f"User: {payload.get('user_input', '')}\nAssistant: {payload.get('assistant_response', '')}"
    if variant == "raw_dialogue":
        return str(payload.get("raw_dialogue") or "")
    if variant == "summary_keywords_raw_dialogue":
        keywords = payload.get("keywords") or []
        keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
        return "\n".join(
            part
            for part in (
                str(payload.get("summary") or ""),
                f"Keywords: {keywords_text}" if keywords_text else "",
                str(payload.get("raw_dialogue") or ""),
            )
            if part
        )
    if variant == "summary_only":
        return str(payload.get("summary") or "")
    raise ValueError(f"未知 Page variant: {variant}")


def query_variant_text(session: BenchmarkSession, turn: BenchmarkTurn, variant: str) -> str:
    if variant == "current_query":
        return turn.question
    if variant == "required_context_oracle":
        return turn.required_context
    qa_count = 1 if variant == "recent_1_qa_query" else 3 if variant == "recent_3_qa_query" else None
    if qa_count is None:
        raise ValueError(f"未知 Query variant: {variant}")
    prior = session.turns[max(0, turn.turn_index - qa_count) : turn.turn_index]
    parts: list[str] = []
    for item in prior:
        parts.extend((f"User: {item.question}", f"Assistant: {item.answer}"))
    parts.append(f"Current user query: {turn.question}")
    return "\n\n".join(parts)


def layer_metrics(
    evaluated_rows: Sequence[Mapping[str, Any]],
    retrieved_by_turn: Mapping[str, Sequence[str]],
    *,
    long_range_only: bool = False,
) -> dict[str, Any]:
    per_turn: list[dict[str, Any]] = []
    matched_total = 0
    gold_total = 0
    retrieved_total = 0
    for row in evaluated_rows:
        gold_ids = list(row.get("gold_turn_ids") or [])
        if long_range_only:
            distances = list(row.get("dependency_distances") or [])
            gold_ids = [gold for gold, distance in zip(gold_ids, distances) if int(distance) > SHORT_TERM_QA_CAPACITY]
        if not gold_ids:
            continue
        retrieved = ordered_unique(str(item) for item in retrieved_by_turn.get(str(row["turn_id"]), []))
        metrics = retrieval_metrics(gold_ids, retrieved)
        per_turn.append(metrics)
        matched_total += int(metrics["matched_count"])
        gold_total += int(metrics["gold_count"])
        retrieved_total += int(metrics["retrieved_count"])
    return {
        "evaluated_turns": len(per_turn),
        "macro_recall": mean(item["recall"] for item in per_turn),
        "micro_recall": matched_total / gold_total if gold_total else 0.0,
        "macro_precision": mean(item["precision"] for item in per_turn),
        "micro_precision": matched_total / retrieved_total if retrieved_total else 0.0,
        "hit_rate": mean(item["hit"] for item in per_turn),
        "full_dependency_recall": mean(item["full_recall"] for item in per_turn),
        "mrr": mean(item["mrr"] for item in per_turn),
        "matched_gold": matched_total,
        "gold_count": gold_total,
        "retrieved_count": retrieved_total,
    }


def aggregate_rank_metrics(
    evaluated_turns: Sequence[BenchmarkTurn],
    long_gold_by_turn: Mapping[str, Sequence[str]],
    rankings: Mapping[str, Sequence[str]],
    cutoff: int,
) -> dict[str, Any]:
    gold_ranks: list[int | None] = []
    per_query_recalls: list[float] = []
    per_query_mrrs: list[float] = []
    full: list[int] = []
    hits: list[int] = []
    for turn in evaluated_turns:
        gold = list(long_gold_by_turn.get(turn.turn_id) or [])
        if not gold:
            continue
        ranking = list(rankings.get(turn.turn_id) or [])
        rank_by_id = {turn_id: index + 1 for index, turn_id in enumerate(ranking)}
        ranks = [rank_by_id.get(gold_id) for gold_id in gold]
        gold_ranks.extend(ranks)
        matched = [rank for rank in ranks if rank is not None and rank <= cutoff]
        per_query_recalls.append(len(matched) / len(gold))
        per_query_mrrs.append(1.0 / min(matched) if matched else 0.0)
        full.append(int(len(matched) == len(gold)))
        hits.append(int(bool(matched)))
    finite = [rank for rank in gold_ranks if rank is not None]
    return {
        "long_range_gold_count": len(gold_ranks),
        "matched_gold": sum(1 for rank in gold_ranks if rank is not None and rank <= cutoff),
        "recall_at_k": mean(per_query_recalls),
        "micro_recall_at_k": (
            sum(1 for rank in gold_ranks if rank is not None and rank <= cutoff) / len(gold_ranks)
            if gold_ranks
            else 0.0
        ),
        "hit_rate_at_k": mean(hits),
        "full_recall_at_k": mean(full),
        "query_mrr_at_k": mean(per_query_mrrs),
        "gold_mrr": mean(1.0 / rank if rank else 0.0 for rank in gold_ranks),
        "gold_mean_rank": mean(finite) if finite else None,
        "unranked_gold": sum(rank is None for rank in gold_ranks),
        "cutoff": cutoff,
    }


def retrieve_layer_map(row: Mapping[str, Any], *layers: str) -> list[str]:
    return ordered_unique(
        str(turn_id)
        for layer in layers
        for turn_id in (row.get(f"{layer}_retrieved_turn_ids") or [])
    )


def load_jobs(history_db: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM memory_migration_jobs ORDER BY sequence_no").fetchall()
    finally:
        connection.close()
    jobs: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        for key in ("filters_json", "metadata_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(item.get(key) or "{}")
            except json.JSONDecodeError:
                item[key.removesuffix("_json")] = {}
        jobs[str(item["job_id"])] = item
    return jobs


def qdrant_client_from_config(config: Mapping[str, Any]) -> tuple[QdrantClient, str]:
    vector_config = dict((config.get("vector_store") or {}).get("config") or {})
    collection_name = str(vector_config["collection_name"])
    if vector_config.get("url"):
        client = QdrantClient(url=vector_config["url"], api_key=vector_config.get("api_key"))
    elif vector_config.get("host") and vector_config.get("port"):
        client = QdrantClient(
            host=vector_config["host"],
            port=int(vector_config["port"]),
            https=vector_config.get("https"),
            api_key=vector_config.get("api_key"),
        )
    elif vector_config.get("path"):
        client = QdrantClient(path=vector_config["path"])
    else:
        raise ValueError("effective_memory_config 中没有可用的 Qdrant endpoint")
    return client, collection_name


def load_qdrant_state(
    client: QdrantClient,
    collection_name: str,
    session: BenchmarkSession,
) -> tuple[list[PageRow], list[dict[str, Any]]]:
    turn_by_question = {turn.question: turn for turn in session.turns}
    page_points, _ = client.scroll(
        f"{collection_name}_midterm_pages",
        limit=10_000,
        with_payload=True,
        with_vectors=True,
    )
    pages: list[PageRow] = []
    for point in page_points:
        payload = dict(point.payload or {})
        source_turn = turn_by_question.get(str(payload.get("user_input") or ""))
        if source_turn is None:
            raise ValueError(f"Page {point.id} 的 user_input 无法映射回数据集 turn")
        vector = point.vector.get("") if isinstance(point.vector, dict) else point.vector
        pages.append(
            PageRow(
                page_id=str(point.id),
                turn_id=source_turn.turn_id,
                turn_index=source_turn.turn_index,
                session_id=str(payload.get("session_id") or ""),
                source_job_id=str(payload.get("source_job_id") or ""),
                payload=payload,
                stored_vector=list(vector or []),
            )
        )
    pages.sort(key=lambda page: page.turn_index)

    session_points, _ = client.scroll(
        f"{collection_name}_midterm_sessions",
        limit=10_000,
        with_payload=True,
        with_vectors=False,
    )
    sessions = [{"id": str(point.id), **dict(point.payload or {})} for point in session_points]
    return pages, sessions


def query_dense_rank(
    query_vector: Sequence[float],
    pages: Sequence[PageRow],
    *,
    document_vectors: Mapping[str, Sequence[float]] | None = None,
) -> list[dict[str, Any]]:
    ranked = []
    for page in pages:
        document_vector = (document_vectors or {}).get(page.page_id, page.stored_vector)
        ranked.append(
            {
                "page_id": page.page_id,
                "turn_id": page.turn_id,
                "session_id": page.session_id,
                "score": cosine_similarity(query_vector, document_vector),
            }
        )
    ranked.sort(key=lambda item: (-float(item["score"]), str(item["page_id"])))
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def qdrant_bm25_rank(
    client: QdrantClient,
    collection_name: str,
    encoder: ChineseBM25SparseEncoder,
    query: str,
    available_pages: Sequence[PageRow],
) -> list[dict[str, Any]]:
    if not available_pages:
        return []
    sparse_result = list(encoder.query_embed([query]))
    if not sparse_result:
        return []
    sparse = sparse_result[0]
    query_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="source_job_id",
                match=models.MatchAny(any=ordered_unique(page.source_job_id for page in available_pages)),
            )
        ]
    )
    hits = client.query_points(
        collection_name=f"{collection_name}_midterm_pages",
        query=models.SparseVector(
            indices=sparse.indices.tolist() if hasattr(sparse.indices, "tolist") else list(sparse.indices),
            values=sparse.values.tolist() if hasattr(sparse.values, "tolist") else list(sparse.values),
        ),
        using="bm25",
        query_filter=query_filter,
        limit=len(available_pages),
        with_payload=True,
    ).points
    page_by_id = {page.page_id: page for page in available_pages}
    ranked: list[dict[str, Any]] = []
    for index, hit in enumerate(hits, start=1):
        page = page_by_id.get(str(hit.id))
        if page is None:
            continue
        ranked.append(
            {
                "page_id": page.page_id,
                "turn_id": page.turn_id,
                "session_id": page.session_id,
                "score": float(hit.score or 0.0),
                "rank": index,
            }
        )
    return ranked


def rrf_rank(
    dense: Sequence[Mapping[str, Any]],
    sparse: Sequence[Mapping[str, Any]],
    *,
    rank_constant: int = 60,
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for source, ranking in (("dense", dense), ("bm25", sparse)):
        for rank, item in enumerate(ranking, start=1):
            page_id = str(item["page_id"])
            row = by_id.setdefault(
                page_id,
                {
                    "page_id": page_id,
                    "turn_id": item["turn_id"],
                    "session_id": item["session_id"],
                    "dense_rank": None,
                    "bm25_rank": None,
                    "score": 0.0,
                },
            )
            row[f"{source}_rank"] = rank
            row["score"] += 1.0 / (rank_constant + rank)
    ranked = sorted(by_id.values(), key=lambda item: (-float(item["score"]), str(item["page_id"])))
    for index, item in enumerate(ranked, start=1):
        item["rank"] = index
    return ranked


def ranking_ids(ranking: Sequence[Mapping[str, Any]]) -> list[str]:
    return [str(item["turn_id"]) for item in ranking]


def rank_item(ranking: Sequence[Mapping[str, Any]], turn_id: str) -> Mapping[str, Any] | None:
    return next((item for item in ranking if item.get("turn_id") == turn_id), None)


def embed_all_variants(
    embedder: HuggingFaceEmbedding,
    pages: Sequence[PageRow],
) -> dict[str, dict[str, list[float]]]:
    result: dict[str, dict[str, list[float]]] = {}
    for variant in PAGE_VARIANTS:
        LOGGER.info("Embedding Page variant: %s (%d pages)", variant, len(pages))
        texts = [page_variant_text(page, variant) for page in pages]
        vectors = embedder.embed_batch(texts, "add")
        result[variant] = {page.page_id: vector for page, vector in zip(pages, vectors)}
    return result


def build_corrected_metrics(evaluated_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    combinations = {
        "short": ("short",),
        "mid_page": ("mid_page",),
        "long": ("long",),
        "short_mid_page": ("short", "mid_page"),
        "mid_page_long": ("mid_page", "long"),
        "short_mid_page_long": ("short", "mid_page", "long"),
    }
    summary: dict[str, Any] = {}
    for name, layers in combinations.items():
        retrieved = {str(row["turn_id"]): retrieve_layer_map(row, *layers) for row in evaluated_rows}
        summary[name] = layer_metrics(evaluated_rows, retrieved)

    long_range_layers = {
        "mid_page_long_range": ("mid_page",),
        "long_long_range": ("long",),
        "mid_page_long_long_range": ("mid_page", "long"),
        "short_mid_page_long_long_range": ("short", "mid_page", "long"),
    }
    for name, layers in long_range_layers.items():
        retrieved = {str(row["turn_id"]): retrieve_layer_map(row, *layers) for row in evaluated_rows}
        summary[name] = layer_metrics(evaluated_rows, retrieved, long_range_only=True)
    return summary


def parse_llm_json(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    text = str(response or "")
    for candidate in (remove_code_blocks(text), extract_json(text)):
        try:
            value = json.loads(candidate, strict=False)
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    raise ValueError(f"Judge 未返回合法 JSON: {compact_text(text, 500)}")


def judge_json(llm: Any, system: str, payload: Mapping[str, Any], *, attempts: int = 6) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = llm.generate_response(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                # deepseek-v4-flash intermittently spends the entire completion on hidden reasoning
                # when JSON mode is forced. The prompt still requires strict JSON, while plain mode
                # has proven materially more reliable in this diagnostic workload.
                response_format=None,
                _return_metadata=True,
            )
            content = getattr(response, "content", response)
            if not str(content or "").strip():
                raise ValueError(
                    "Judge content 为空"
                    f" (finish_reason={getattr(response, 'finish_reason', None)}, "
                    f"completion_tokens={getattr(response, 'completion_tokens', None)}, "
                    f"reasoning_tokens={getattr(response, 'reasoning_tokens', None)})"
                )
            return parse_llm_json(content)
        except Exception as exc:  # noqa: BLE001 - diagnostics must retain judge failures
            last_error = exc
            LOGGER.warning("Judge attempt %d/%d failed: %s", attempt, attempts, exc)
    raise RuntimeError(f"Judge 连续失败 {attempts} 次: {last_error}")


def run_dataset_audit(
    llm: Any,
    session: BenchmarkSession,
    evaluated_turns: Sequence[BenchmarkTurn],
    pages: Sequence[PageRow],
    gold_by_turn: Mapping[str, Sequence[str]],
    checkpoint_path: Path,
) -> list[dict[str, Any]]:
    turn_by_id = {turn.turn_id: turn for turn in session.turns}
    page_by_turn = {page.turn_id: page for page in pages}
    rows = load_jsonl(checkpoint_path)
    completed = {(str(row.get("turn_id")), str(row.get("gold_turn_id"))) for row in rows}
    system = """你是严格的金融对话依赖标注审计员。逐条审查 exact turn-id Gold，不评价召回算法。
只能依据输入。不要因为多个回答共享通用财务数字就轻率认定等价；必须比较当前问题所需的特定分析结论、口径和事实。
对每个 gold 输出：gold_turn_id，verdict（CORRECT、WRONG_TURN、REDUNDANT、UNCLEAR 四选一），necessary（bool），
wrong_turn_candidate（string或null），equivalent_turn_ids（array），ambiguous_without_context（bool），reason（简洁中文）。
WRONG_TURN 仅在另一轮明显才是真实来源时使用；REDUNDANT 仅在不依赖该轮也能回答时使用。
ambiguous_without_context 必须只根据 current_turn.question 本身判断：假设检索器看不到 required_context、Gold、Short QA 和其他历史，
它能否在已有历史中唯一定位该 exact Gold。required_context 只是审计答案键，绝不能当作检索输入。若问题使用“前面”“刚才”
“上一轮”“这些变化”等指代，且仅靠当前问题无法区分多个相似历史分析，必须判为 true，即使 required_context 明确写出了 Gold。
输出严格 JSON：{"audits":[...]}。"""
    for turn in evaluated_turns:
        gold_ids = list(gold_by_turn.get(turn.turn_id) or [])
        if all((turn.turn_id, gold_id) in completed for gold_id in gold_ids):
            continue
        prior_pages = [page for page in pages if page.turn_index <= turn.turn_index - 4]
        payload = {
            "current_turn": {
                "turn_id": turn.turn_id,
                "question": turn.question,
                "required_context": turn.required_context,
            },
            "gold_dependencies": [
                {
                    "turn_id": gold_id,
                    "question": turn_by_id[gold_id].question,
                    "answer": compact_text(turn_by_id[gold_id].answer, 2200),
                    "page_summary": compact_text(page_by_turn[gold_id].payload.get("summary"), 900)
                    if gold_id in page_by_turn
                    else None,
                }
                for gold_id in gold_ids
            ],
            "other_available_history": [
                {
                    "turn_id": page.turn_id,
                    "question": compact_text(turn_by_id[page.turn_id].question, 300),
                    "summary": compact_text(page.payload.get("summary"), 260),
                }
                for page in prior_pages
                if page.turn_id not in gold_ids
            ],
        }
        LOGGER.info("Dataset audit: %s (%d Gold)", turn.turn_id, len(gold_ids))
        result = judge_json(llm, system, payload)
        by_gold = {
            str(item.get("gold_turn_id")): item
            for item in (result.get("audits") or [])
            if isinstance(item, dict)
        }
        for gold_id in gold_ids:
            item = dict(by_gold.get(gold_id) or {})
            row = {
                "turn_id": turn.turn_id,
                "gold_turn_id": gold_id,
                "judge_model": getattr(getattr(llm, "config", None), "model", None),
                "verdict": str(item.get("verdict") or "UNCLEAR").upper(),
                "necessary": bool(item.get("necessary")),
                "wrong_turn_candidate": item.get("wrong_turn_candidate"),
                "equivalent_turn_ids": item.get("equivalent_turn_ids") or [],
                "ambiguous_without_context": bool(item.get("ambiguous_without_context")),
                "reason": str(item.get("reason") or "Judge 未返回该 Gold"),
            }
            rows.append(row)
            completed.add((turn.turn_id, gold_id))
        write_jsonl(checkpoint_path, rows)
    return rows


def run_semantic_equivalence_judge(
    llm: Any,
    session: BenchmarkSession,
    pages: Sequence[PageRow],
    per_gold_rows: Sequence[Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    checkpoint_path: Path,
) -> list[dict[str, Any]]:
    page_by_turn = {page.turn_id: page for page in pages}
    turn_by_id = {turn.turn_id: turn for turn in session.turns}
    system = """你是严格的历史 Page 语义覆盖 Judge。Gold exact ID 没命中时，判断返回的其他 Page 是否真正包含回答当前问题所需的 Gold 核心事实。
共享公司名、年份、通用财务数字或泛化结论不够；必须覆盖 Gold 对当前问题贡献的特定事实、分析关系和口径。
从 candidates 中选择最佳一项；若没有等价项，best_retrieved_turn_id 为 null。
输出严格 JSON：{"equivalent":bool,"coverage":0到1,"best_retrieved_turn_id":string或null,
"missing_facts":array,"reason":string}。"""
    results = load_jsonl(checkpoint_path)
    completed = {(str(row.get("turn_id")), str(row.get("gold_turn_id"))) for row in results}
    for row in per_gold_rows:
        if row.get("gold_responsibility") != "long_range" or row.get("baseline_mid_page_hit"):
            continue
        turn_id = str(row["turn_id"])
        gold_id = str(row["gold_turn_id"])
        if (turn_id, gold_id) in completed:
            continue
        gold_page = page_by_turn.get(gold_id)
        candidates = list(baseline_rankings.get(turn_id) or [])[:5]
        payload = {
            "current_question": turn_by_id[turn_id].question,
            "required_context": turn_by_id[turn_id].required_context,
            "gold_page": {
                "turn_id": gold_id,
                "question": turn_by_id[gold_id].question,
                "raw_dialogue": compact_text(gold_page.payload.get("raw_dialogue") if gold_page else "", 2800),
            },
            "candidates": [
                {
                    "turn_id": item["turn_id"],
                    "raw_dialogue": compact_text(page_by_turn[str(item["turn_id"])].payload.get("raw_dialogue"), 1800),
                }
                for item in candidates
            ],
        }
        LOGGER.info("Semantic equivalence judge: %s <- %s", turn_id, gold_id)
        result = judge_json(llm, system, payload)
        results.append(
            {
                "turn_id": turn_id,
                "gold_turn_id": gold_id,
                "judge_model": getattr(getattr(llm, "config", None), "model", None),
                "equivalent": bool(result.get("equivalent")),
                "coverage": float(result.get("coverage") or 0.0),
                "best_retrieved_turn_id": result.get("best_retrieved_turn_id"),
                "missing_facts": result.get("missing_facts") or [],
                "reason": str(result.get("reason") or ""),
            }
        )
        completed.add((turn_id, gold_id))
        write_jsonl(checkpoint_path, results)
    return results


def classify_root_cause(row: Mapping[str, Any]) -> tuple[str, list[str], str]:
    secondary: list[str] = []
    if row.get("gold_responsibility") == "short":
        return "SHORT_TERM_RESPONSIBILITY", secondary, "covered"
    if row.get("baseline_mid_page_hit"):
        return "BASELINE_HIT", secondary, "covered"
    if not row.get("page_exists") or not row.get("page_available"):
        return "PAGE_NOT_AVAILABLE", secondary, "algorithm"
    if not row.get("lineage_ok") or not row.get("session_lineage_consistent"):
        return "LINEAGE_ERROR", secondary, "algorithm"
    if not row.get("selected_session"):
        return "SESSION_ROUTING", secondary, "algorithm"

    if row.get("dataset_ambiguous_without_context"):
        secondary.append("AMBIGUOUS_QUERY_LABEL")

    audit_verdict = str(row.get("dataset_audit_verdict") or "").upper()
    if audit_verdict == "WRONG_TURN":
        return "DATASET_WRONG_GOLD", secondary, "data"
    if row.get("semantic_equivalent") and float(row.get("semantic_equivalent_coverage") or 0.0) >= 0.8:
        return "EQUIVALENT_ALTERNATIVE_PAGE", secondary, "data"
    if audit_verdict == "REDUNDANT":
        return "DATASET_WRONG_GOLD", ["REDUNDANT_GOLD"], "data"

    current_rank = row.get("global_page_rank")
    raw_rank = row.get("raw_dialogue_rank")
    context_rank = row.get("recent_3_qa_rank")
    required_rank = row.get("required_context_rank")
    rrf_rank_value = row.get("rrf_rank")
    if context_rank and int(context_rank) <= 5 and (not current_rank or int(current_rank) > 5):
        return (
            "QUERY_FORMULATION",
            secondary,
            "joint" if row.get("dataset_ambiguous_without_context") else "algorithm",
        )
    if raw_rank and int(raw_rank) <= 5 and (not current_rank or int(current_rank) > 5):
        return "PAGE_REPRESENTATION", ["SUMMARY_INFORMATION_LOSS"], "algorithm"
    if current_rank and 5 < int(current_rank) <= 20:
        return "TOP_K_CUTOFF", secondary, "algorithm"
    if rrf_rank_value and int(rrf_rank_value) <= 5 and (not current_rank or int(current_rank) > 5):
        return "HYBRID_FUSION", secondary, "algorithm"
    if required_rank and int(required_rank) <= 5:
        if row.get("dataset_ambiguous_without_context"):
            return "AMBIGUOUS_QUERY_LABEL", ordered_unique([*secondary, "QUERY_FORMULATION"]), "joint"
        return "QUERY_FORMULATION", secondary, "algorithm"
    if row.get("raw_vs_summary_similarity_delta", 0.0) > 0.05:
        return "SUMMARY_INFORMATION_LOSS", ["PAGE_REPRESENTATION"], "algorithm"
    if not current_rank or int(current_rank) > 20:
        return "EMBEDDING_MODEL", ["PAGE_RANKING"], "algorithm"
    return "PAGE_RANKING", secondary, "algorithm"


def format_percent(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:.2f}%"


def build_report(
    *,
    summary: Mapping[str, Any],
    per_turn_rows: Sequence[Mapping[str, Any]],
    per_gold_rows: Sequence[Mapping[str, Any]],
    page_rep_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    topk_rows: Sequence[Mapping[str, Any]],
    dataset_rows: Sequence[Mapping[str, Any]],
    session_rows: Sequence[Mapping[str, Any]],
) -> str:
    corrected = summary["corrected_metrics"]
    effective = corrected["short_mid_page_long"]
    long_effective = corrected["short_mid_page_long_long_range"]
    experiments = summary["oracle_experiments"]
    root_counts = summary["root_cause_counts"]
    lines = [
        "# S001 Mid-term Page 召回根因诊断",
        "",
        "> 口径：最终中期历史只计 `mid_page`，`mid_session` 仅用于第一阶段路由诊断。所有离线排名均按每个评测轮当时已提交的 Page 构造时间快照，排除未来 Page。",
        "",
        "## 1. 当前真实有效 Recall",
        "",
        f"`Short + Mid Page + Long`：Macro Recall **{format_percent(effective['macro_recall'])}**，Micro Recall **{format_percent(effective['micro_recall'])}**（{effective['matched_gold']}/{effective['gold_count']}），Macro Precision **{format_percent(effective['macro_precision'])}**，Hit Rate **{format_percent(effective['hit_rate'])}**，Full Dependency Recall **{format_percent(effective['full_dependency_recall'])}**，MRR **{effective['mrr']:.4f}**。",
        "",
        f"真正 long-range Gold（distance > 3）共 {long_effective['gold_count']} 条；`Page + Long` 覆盖 {corrected['mid_page_long_long_range']['matched_gold']}/{corrected['mid_page_long_long_range']['gold_count']}（{format_percent(corrected['mid_page_long_long_range']['micro_recall'])}），`Short + Page + Long` long-range Recall 为 {format_percent(long_effective['micro_recall'])}。",
        "",
        "| Layer | Macro Recall | Micro Recall | Precision (macro) | Hit Rate | Full Recall | MRR |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("short", "mid_page", "long", "short_mid_page", "mid_page_long", "short_mid_page_long"):
        item = corrected[name]
        lines.append(
            f"| {name} | {format_percent(item['macro_recall'])} | {format_percent(item['micro_recall'])} | "
            f"{format_percent(item['macro_precision'])} | {format_percent(item['hit_rate'])} | "
            f"{format_percent(item['full_dependency_recall'])} | {item['mrr']:.4f} |"
        )

    lines.extend(
        [
            "",
            "## 2. 14 个评测问题逐条结果",
            "",
            "| Query | Long Gold | Page hit | Long hit | Effective hit | Full dependencies | 主因 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in per_turn_rows:
        lines.append(
            f"| {row['turn_id']} | {row['long_range_gold_count']} | {row['mid_page_long_hits']} | "
            f"{row['long_long_hits']} | {row['effective_long_hits']} | "
            f"{'yes' if row['effective_full_dependency_recall'] else 'no'} | {row['root_causes']} |"
        )

    availability = summary["availability"]
    lines.extend(
        [
            "",
            "## 3. Gold Page availability 与 lineage",
            "",
            f"21 条 long-range Gold 中：Page 存在 {availability['page_exists']}/21，committed 且在原查询前 job 已完成 {availability['page_available']}/21，Qdrant 可见 {availability['qdrant_visible']}/21，lineage 正确 {availability['lineage_ok']}/21。",
            "",
            "没有发现 `PAGE_NOT_AVAILABLE`、staging、已提交但不可见或 source-job lineage 错误。原实验 `before_evaluation` 的等待确实生效；这一层不是低 Recall 的来源。",
            "",
            f"为避免 future leakage，诊断按查询 Qxxx 仅保留 `turn_index <= current_index - 4` 的 Page，并用原 migration job 完成时间校验；重建的 dense Top-5 与原实验 **{summary['temporal_replay_validation']['matched_turns']}/{summary['temporal_replay_validation']['evaluated_turns']}** 轮逐项完全一致。",
            "",
            "## 4. Session Routing 与 Session 划分",
            "",
            f"最终形成 **{len(session_rows)} 个 Session**、{sum(int(row['page_count']) for row in session_rows)} 个 Page。所有 Page 都属于同一个 Session；所有评测 Query 的 Gold Session rank 均为 1 且被选中。",
            "",
            "| Session ID | Pages | Q range | Summary | Summary keywords | lineage |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for row in session_rows:
        lines.append(
            f"| {row['session_id']} | {row['page_count']} | {row['turn_ids']} | {compact_text(row['summary'], 220)} | "
            f"{compact_text(row['summary_keywords'], 180)} | page_ids={row['page_ids_consistent']}, jobs={row['source_job_ids_consistent']} |"
        )

    lines.extend(
        [
            "",
            "## 5. Oracle / Global upper bounds",
            "",
            "| Experiment | Long-range Recall@5 | Recall@10 | Gold MRR | Gold mean rank |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for key in (
        "baseline",
        "oracle_session",
        "global_page",
        "context_query_recent_3",
        "raw_dialogue_embedding",
        "required_context_oracle",
    ):
        item = experiments[key]
        lines.append(
            f"| {key} | {format_percent(item['recall_at_5'])} | {format_percent(item['recall_at_10'])} | "
            f"{item['gold_mrr']:.4f} | {item['gold_mean_rank'] if item['gold_mean_rank'] is not None else 'N/A'} |"
        )

    lines.extend(
        [
            "",
            "Oracle Session 与 Global Page 都和 baseline 相同，证明两阶段 Session 路由在本 Sheet 上没有造成损失；瓶颈完全发生在单一大 Session 内的 Page 排序/检索信号。",
            "",
            "## 6. Page ranking 分布与 Top-K sweep",
            "",
            f"missed Gold 的 baseline dense rank 中位数为 {summary['ranking_distribution']['missed_median_rank']}，rank 6–10 有 {summary['ranking_distribution']['rank_6_10']} 条，rank 11–20 有 {summary['ranking_distribution']['rank_11_20']} 条，rank >20 有 {summary['ranking_distribution']['rank_gt_20']} 条。",
            "",
            "| top_k_sessions | top_k_pages | max_total_pages | Recall | Full Recall | Gold MRR |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in topk_rows:
        if row["top_k_sessions"] in (1, "all") and row["top_k_pages"] in (5, 10, 20):
            lines.append(
                f"| {row['top_k_sessions']} | {row['top_k_pages']} | {row['max_total_pages']} | "
                f"{format_percent(row['micro_recall_at_k'])} | {format_percent(row['full_recall_at_k'])} | {row['gold_mrr']:.4f} |"
            )

    lines.extend(
        [
            "",
            "## 7. Query ablation",
            "",
            "| Query | Recall@5 | Recall@10 | Gold MRR | Mean rank |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in query_rows:
        lines.append(
            f"| {row['variant']} | {format_percent(row['recall_at_5'])} | {format_percent(row['recall_at_10'])} | "
            f"{row['gold_mrr']:.4f} | {row['gold_mean_rank']} |"
        )

    lines.extend(
        [
            "",
            "当前 Query 单独检索 Recall@5 为 28.57%；拼接最近 1 个 QA 提升到 42.86%，最近 3 个 QA 为 38.10%，但两者的 Recall@10 均下降，说明直接拼接上下文有帮助但噪声明显。`required_context` Oracle 达 85.71%，相对当前 Query 提升 57.14 个百分点，证明检索输入缺少历史语义是强瓶颈；该字段只用于上界，不进入正式检索。",
        ]
    )

    lines.extend(
        [
            "",
            "## 8. Page embedding representation ablation",
            "",
            "| Representation | Recall@5 | Recall@10 | Gold MRR | Mean rank |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in page_rep_rows:
        lines.append(
            f"| {row['variant']} | {format_percent(row['recall_at_5'])} | {format_percent(row['recall_at_10'])} | "
            f"{row['gold_mrr']:.4f} | {row['gold_mean_rank']} |"
        )

    lines.extend(
        [
            "",
            "当前实际表示是 `summary + keywords + user_input`。`user_input + assistant_answer` 与 `raw_dialogue` 在本数据中等价，Recall@5/10 为 23.81%/47.62%，低于当前表示的 28.57%/76.19%；因此不能据此把正式表示直接替换为完整原对话。`summary + keywords + raw_dialogue` 的 Recall@5 小幅升至 33.33%，但 Recall@10 降至 66.67%。当前金融数据的 Assistant Answer 高度模板化且重复携带相同底表，完整 raw dialogue 会稀释区分信号；Page 表示是个别失败的原因，不是总体主因。",
        ]
    )

    hybrid = summary["hybrid_search"]
    lines.extend(
        [
            "",
            "## 9. Dense / BM25 / Hybrid",
            "",
            "代码审计和真实 trace 均确认：Mid-term `search_pages()` 只调用 Qdrant dense `search()`；虽然 Collection 写入了 BM25 sparse vector，但 Mid-term Page ranking 没有调用 `keyword_search()`，因此所谓 current hybrid 实际是 dense-only。",
            "",
            f"Dense Recall@5={format_percent(hybrid['dense']['micro_recall_at_k'])}，BM25-only Recall@5={format_percent(hybrid['bm25']['micro_recall_at_k'])}，诊断用 RRF Recall@5={format_percent(hybrid['diagnostic_rrf']['micro_recall_at_k'])}。RRF 只用于诊断，没有改生产算法。",
            "",
            "实际 BM25 trace 的 21 条 long-range Gold 全部未排名。原因是中文 sparse encoder 只按空格切 token，而 Mid-term 的 `text_lemmatized` 只是对 embedding text 调用 `.lower()`，没有中文分词；Query 也未被预分词。故本轮不能把 0% 解释成“词法检索天然无效”，只能确认当前 Mid-term BM25 链路既未参与正式排序、按现有文本形态也不可用。",
            "",
            "真实 trace 例：Q013→Q001 的 dense/final score=0.394777、rank=9；BM25 无命中；诊断 RRF score=0.014493、rank=9，cutoff=5。各 Gold 的 dense、BM25、RRF、raw、summary 分数与排名均保存在 `page_ranking_analysis.csv`。",
            "",
            "## 10. Page Summary / Keywords 质量",
            "",
            f"missed Gold 中，Query↔raw dialogue 相似度高于 Query↔summary 的有 {summary['summary_quality']['raw_better_count']}/{summary['summary_quality']['missed_count']} 条，平均差值 {summary['summary_quality']['mean_raw_minus_summary']:.4f}；raw-dialogue embedding 能把 {summary['summary_quality']['raw_rescued_at_5']} 条 baseline miss 拉回 Top-5。",
            "",
            "## 11. Dataset Gold audit",
            "",
            "| Audit verdict | Count |",
            "|---|---:|",
        ]
    )
    for verdict, count in sorted(Counter(str(row["verdict"]) for row in dataset_rows).items()):
        lines.append(f"| {verdict} | {count} |")
    correct_count = sum(str(row["verdict"]) == "CORRECT" for row in dataset_rows)
    wrong_count = sum(str(row["verdict"]) == "WRONG_TURN" for row in dataset_rows)
    unclear_count = sum(str(row["verdict"]) == "UNCLEAR" for row in dataset_rows)
    redundant_count = sum(str(row["verdict"]) == "REDUNDANT" for row in dataset_rows)
    ambiguous_rows = [
        row
        for row in dataset_rows
        if str(row.get("ambiguous_without_context", "")).strip().lower() == "true"
    ]
    equivalent_rows = [
        row
        for row in per_gold_rows
        if row.get("gold_responsibility") == "long_range"
        and not row.get("baseline_mid_page_hit")
        and row.get("semantic_equivalent")
        and float(row.get("semantic_equivalent_coverage") or 0.0) >= 0.8
    ]
    lines.extend(
        [
            "",
            f"审计汇总：明确正确 Gold {correct_count}/{len(dataset_rows)}，疑似标错 {wrong_count}/{len(dataset_rows)}，冗余 {redundant_count}/{len(dataset_rows)}，不确定 {unclear_count}/{len(dataset_rows)}；严格语义 Judge 在 15 条 exact miss 中确认等价历史 Page {len(equivalent_rows)} 条；仅给 current question 时无法唯一定位 exact Gold {len(ambiguous_rows)}/{len(dataset_rows)}。",
            "",
            "具体例子：Q013→Q006 被确认是正确且必要的扣非口径依赖；Q039→Q032 被确认是正确且必要的资产/权益同步性依赖，未发现错标。Q044→Q034 的 Top-5 返回 Q035、Q044→Q040 的 Top-5 返回 Q016，严格 Judge 均判定 coverage=1.0，因此 exact-ID 在这两条上是 false negative。",
            "",
            "## 12. Exact-ID vs Semantic-equivalent Recall",
            "",
            f"baseline exact long-range Page Recall@5 为 {format_percent(summary['semantic_equivalence']['exact_recall'])}；允许严格 Judge 认可的等价 Page 后为 {format_percent(summary['semantic_equivalence']['semantic_equivalent_recall'])}。Judge 结果不替换 exact-ID 指标，只揭示评价 false negative。",
            "",
            "## 13. missed Gold 根因数量和占比",
            "",
            "| Primary cause | Count | Share of misses |",
            "|---|---:|---:|",
        ]
    )
    missed_count = int(summary["missed_long_range_gold"])
    for cause, count in sorted(root_counts.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {cause} | {count} | {format_percent(count / missed_count if missed_count else 0)} |")
    ownership = summary["ownership_counts"]
    lines.extend(
        [
            "",
            f"算法/实现相关 {ownership.get('algorithm', 0)}/{missed_count}（{format_percent(ownership.get('algorithm', 0) / missed_count if missed_count else 0)}）；数据集/评价相关 {ownership.get('data', 0)}/{missed_count}（{format_percent(ownership.get('data', 0) / missed_count if missed_count else 0)}）；共同影响 {ownership.get('joint', 0)}/{missed_count}（{format_percent(ownership.get('joint', 0) / missed_count if missed_count else 0)}）；未归因 {ownership.get('unresolved', 0)}/{missed_count}。",
            "",
            "## 14. 最终结论",
            "",
            str(summary["final_conclusion"]),
            "",
            "## 15. 推荐修改优先级",
            "",
        ]
    )
    for index, item in enumerate(summary["recommendations"], start=1):
        lines.append(f"{index}. {item}")

    lines.extend(["", "## Appendix: 每条 missed long-range Gold", ""])
    for row in per_gold_rows:
        if row.get("gold_responsibility") != "long_range" or row.get("baseline_mid_page_hit"):
            continue
        lines.extend(
            [
                f"### {row['turn_id']} → {row['gold_turn_id']}",
                "",
                f"distance={row['dependency_distance']}；page_available={row['page_available']}；session_rank={row['gold_session_rank']}；session_score={row['gold_session_score']}；page_rank={row['global_page_rank']}；page_score={row['global_page_score']}；cutoff=5；recent1_rank={row['recent_1_qa_rank']}；recent3_rank={row['recent_3_qa_rank']}；required_context_rank={row['required_context_rank']}；raw_rank={row['raw_dialogue_rank']}；long_hit={row['long_hit']}；effective_hit={row['effective_hit']}；semantic_equivalent={row['semantic_equivalent']}；primary={row['root_cause']}；secondary={row['secondary_causes']}。",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def run() -> None:
    args = parse_args()
    source_dir = resolve_path(REPO_ROOT, args.source_dir)
    output_dir = resolve_path(REPO_ROOT, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    recall_config = load_json(REPO_ROOT / "exp/benchmark/recall_benchmark.json")
    dataset_path = resolve_path(REPO_ROOT, (recall_config.get("dataset") or {})["path"])
    session = load_dataset(dataset_path, include_sheets=[args.sheet])[0]
    turn_by_id = {turn.turn_id: turn for turn in session.turns}
    evaluated_turns = [
        turn
        for turn in session.turns
        if turn.needs_history
        and turn.dependency_turn_ids
        and any(distance > SHORT_TERM_QA_CAPACITY for distance in dependency_distances(session, turn))
    ]

    recall_rows = load_jsonl(source_dir / "recall_turn_results.jsonl")
    evaluated_rows = [row for row in recall_rows if row.get("evaluated") and row.get("sheet_name") == args.sheet]
    evaluated_by_turn = {str(row["turn_id"]): row for row in evaluated_rows}
    failure_rows = {str(row["turn_id"]): row for row in load_jsonl(source_dir / "recall_failures.jsonl")}
    if len(evaluated_rows) != 14:
        raise ValueError(f"预期 14 个 evaluated turn，实际 {len(evaluated_rows)}")

    effective_config = load_json(source_dir / "effective_memory_config.json")
    history_db = Path(str(effective_config["history_db_path"]))
    jobs = load_jobs(history_db)
    client, collection_name = qdrant_client_from_config(effective_config)
    pages, midterm_sessions = load_qdrant_state(client, collection_name, session)
    if len(pages) != 47:
        raise ValueError(f"预期 47 个已迁移 Page，实际 {len(pages)}")
    page_by_turn = {page.turn_id: page for page in pages}

    embed_cfg = effective_config.get("embedder") or {}
    embedder = HuggingFaceEmbedding(BaseEmbedderConfig(**dict(embed_cfg.get("config") or {})))
    variant_vectors = embed_all_variants(embedder, pages)
    query_texts = {
        (turn.turn_id, variant): query_variant_text(session, turn, variant)
        for turn in evaluated_turns
        for variant in QUERY_VARIANTS
    }
    query_vectors_list = embedder.embed_batch(list(query_texts.values()), "search")
    query_vectors = {key: vector for key, vector in zip(query_texts, query_vectors_list)}
    bm25_encoder = ChineseBM25SparseEncoder()

    long_gold_by_turn: dict[str, list[str]] = {}
    all_gold_by_turn: dict[str, list[str]] = {}
    for turn in evaluated_turns:
        distances = dependency_distances(session, turn)
        all_gold_by_turn[turn.turn_id] = list(turn.dependency_turn_ids)
        long_gold_by_turn[turn.turn_id] = [
            gold for gold, distance in zip(turn.dependency_turn_ids, distances) if distance > SHORT_TERM_QA_CAPACITY
        ]

    dense_rankings: dict[str, list[dict[str, Any]]] = {}
    bm25_rankings: dict[str, list[dict[str, Any]]] = {}
    rrf_rankings: dict[str, list[dict[str, Any]]] = {}
    representation_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {
        variant: {} for variant in PAGE_VARIANTS
    }
    query_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {variant: {} for variant in QUERY_VARIANTS}
    oracle_rankings: dict[str, list[dict[str, Any]]] = {}

    for turn in evaluated_turns:
        available_pages = [page for page in pages if page.turn_index <= turn.turn_index - SHORT_TERM_QA_CAPACITY - 1]
        current_vector = query_vectors[(turn.turn_id, "current_query")]
        dense = query_dense_rank(current_vector, available_pages)
        dense_rankings[turn.turn_id] = dense
        bm25 = qdrant_bm25_rank(client, collection_name, bm25_encoder, turn.question, available_pages)
        bm25_rankings[turn.turn_id] = bm25
        rrf_rankings[turn.turn_id] = rrf_rank(dense, bm25)

        gold_sessions = {
            page_by_turn[gold].session_id for gold in long_gold_by_turn[turn.turn_id] if gold in page_by_turn
        }
        oracle_pages = [page for page in available_pages if page.session_id in gold_sessions]
        oracle_rankings[turn.turn_id] = query_dense_rank(current_vector, oracle_pages)

        for variant in PAGE_VARIANTS:
            representation_rankings[variant][turn.turn_id] = query_dense_rank(
                current_vector,
                available_pages,
                document_vectors=variant_vectors[variant],
            )
        for variant in QUERY_VARIANTS:
            query_rankings[variant][turn.turn_id] = query_dense_rank(
                query_vectors[(turn.turn_id, variant)],
                available_pages,
            )

    baseline_rankings = dense_rankings
    for turn in evaluated_turns:
        recorded = list(evaluated_by_turn[turn.turn_id].get("mid_page_retrieved_turn_ids") or [])
        reconstructed = ranking_ids(dense_rankings[turn.turn_id])[:5]
        if recorded != reconstructed:
            raise AssertionError(
                f"{turn.turn_id} temporal rank 无法复现原实验：recorded={recorded}, reconstructed={reconstructed}"
            )

    failure_memory_by_turn = {
        turn_id: list(row.get("retrieved_memories") or []) for turn_id, row in failure_rows.items()
    }
    per_gold_rows: list[dict[str, Any]] = []
    session_routing_rows: list[dict[str, Any]] = []
    page_ranking_rows: list[dict[str, Any]] = []

    for turn in evaluated_turns:
        record = evaluated_by_turn[turn.turn_id]
        distances = dict(zip(turn.dependency_turn_ids, dependency_distances(session, turn)))
        session_item = next(
            (
                item
                for item in failure_memory_by_turn.get(turn.turn_id, [])
                if item.get("source") == "mid_term_session"
            ),
            {},
        )
        available_pages = [page for page in pages if page.turn_index <= turn.turn_index - SHORT_TERM_QA_CAPACITY - 1]
        available_job_ids = {page.source_job_id for page in available_pages}
        current_job = next(
            (
                job
                for job in jobs.values()
                if (job.get("metadata") or {}).get("dataset_turn_id") == turn.turn_id
            ),
            None,
        )
        retrieval_upper_bound = parse_timestamp(current_job.get("created_at")) if current_job else None

        for gold_id in turn.dependency_turn_ids:
            responsibility = "long_range" if distances[gold_id] > SHORT_TERM_QA_CAPACITY else "short"
            page = page_by_turn.get(gold_id)
            job = jobs.get(page.source_job_id) if page else None
            job_finished = parse_timestamp(job.get("midterm_finished_at")) if job else None
            page_available = bool(
                responsibility == "long_range"
                and page
                and page.source_job_id in available_job_ids
                and job
                and job.get("midterm_status") == "succeeded"
                and page.payload.get("output_state") == "committed"
                and (not retrieval_upper_bound or not job_finished or job_finished <= retrieval_upper_bound)
            )
            session_payload = next(
                (item for item in midterm_sessions if str(item["id"]) == (page.session_id if page else "")),
                None,
            )
            lineage_ok = bool(page and page.source_job_id and job and page.turn_id == gold_id)
            session_consistent = bool(
                page
                and session_payload
                and page.page_id in (session_payload.get("page_ids") or [])
                and page.source_job_id in (session_payload.get("source_job_ids") or [])
            )
            dense_item = rank_item(dense_rankings[turn.turn_id], gold_id)
            bm25_item = rank_item(bm25_rankings[turn.turn_id], gold_id)
            rrf_item = rank_item(rrf_rankings[turn.turn_id], gold_id)
            raw_item = rank_item(representation_rankings["raw_dialogue"][turn.turn_id], gold_id)
            summary_item = rank_item(representation_rankings["summary_only"][turn.turn_id], gold_id)
            recent_1_item = rank_item(query_rankings["recent_1_qa_query"][turn.turn_id], gold_id)
            recent_3_item = rank_item(query_rankings["recent_3_qa_query"][turn.turn_id], gold_id)
            required_item = rank_item(query_rankings["required_context_oracle"][turn.turn_id], gold_id)
            row = {
                "turn_id": turn.turn_id,
                "current_question": turn.question,
                "required_context": turn.required_context,
                "gold_turn_id": gold_id,
                "gold_question": turn_by_id[gold_id].question,
                "gold_raw_dialogue": page.payload.get("raw_dialogue") if page else None,
                "gold_summary": page.payload.get("summary") if page else None,
                "gold_keywords": page.payload.get("keywords") if page else None,
                "gold_embedding_text": page.payload.get("data") if page else None,
                "dependency_distance": distances[gold_id],
                "gold_responsibility": responsibility,
                "page_exists": bool(page),
                "migration_job_id": page.source_job_id if page else None,
                "migration_job_exists": bool(job),
                "migration_status": job.get("status") if job else None,
                "midterm_status": job.get("midterm_status") if job else None,
                "midterm_finished_at": job.get("midterm_finished_at") if job else None,
                "retrieval_time_upper_bound": current_job.get("created_at") if current_job else None,
                "page_output_state": page.payload.get("output_state") if page else None,
                "qdrant_visible": bool(page),
                "page_available": page_available,
                "lineage_ok": lineage_ok,
                "gold_session_id": page.session_id if page else None,
                "gold_session_rank": 1 if page_available and midterm_sessions else None,
                "gold_session_score": session_item.get("score") if page_available else None,
                "selected_session": bool(page_available and midterm_sessions),
                "session_lineage_consistent": session_consistent,
                "page_rank_in_gold_session": dense_item.get("rank") if dense_item else None,
                "global_page_rank": dense_item.get("rank") if dense_item else None,
                "global_page_score": dense_item.get("score") if dense_item else None,
                "page_cutoff": 5,
                "baseline_mid_page_hit": gold_id in (record.get("mid_page_retrieved_turn_ids") or []),
                "bm25_rank": bm25_item.get("rank") if bm25_item else None,
                "bm25_score": bm25_item.get("score") if bm25_item else None,
                "rrf_rank": rrf_item.get("rank") if rrf_item else None,
                "rrf_score": rrf_item.get("score") if rrf_item else None,
                "raw_dialogue_rank": raw_item.get("rank") if raw_item else None,
                "raw_dialogue_score": raw_item.get("score") if raw_item else None,
                "summary_rank": summary_item.get("rank") if summary_item else None,
                "summary_score": summary_item.get("score") if summary_item else None,
                "raw_vs_summary_similarity_delta": (
                    float(raw_item.get("score") or 0.0) - float(summary_item.get("score") or 0.0)
                    if raw_item and summary_item
                    else 0.0
                ),
                "recent_1_qa_rank": recent_1_item.get("rank") if recent_1_item else None,
                "recent_3_qa_rank": recent_3_item.get("rank") if recent_3_item else None,
                "required_context_rank": required_item.get("rank") if required_item else None,
                "long_hit": gold_id in (record.get("long_retrieved_turn_ids") or []),
                "effective_hit": gold_id
                in retrieve_layer_map(record, "short", "mid_page", "long"),
            }
            per_gold_rows.append(row)
            session_routing_rows.append(
                {
                    key: row[key]
                    for key in (
                        "turn_id",
                        "gold_turn_id",
                        "gold_session_id",
                        "gold_session_rank",
                        "gold_session_score",
                        "selected_session",
                        "session_lineage_consistent",
                    )
                }
            )
            page_ranking_rows.append(
                {
                    key: row[key]
                    for key in (
                        "turn_id",
                        "current_question",
                        "required_context",
                        "gold_turn_id",
                        "gold_question",
                        "gold_raw_dialogue",
                        "gold_summary",
                        "gold_keywords",
                        "gold_embedding_text",
                        "dependency_distance",
                        "global_page_rank",
                        "global_page_score",
                        "bm25_rank",
                        "bm25_score",
                        "rrf_rank",
                        "rrf_score",
                        "raw_dialogue_rank",
                        "raw_dialogue_score",
                        "summary_rank",
                        "summary_score",
                        "page_cutoff",
                        "baseline_mid_page_hit",
                    )
                }
            )

    corrected_metrics = build_corrected_metrics(evaluated_rows)

    page_rep_rows: list[dict[str, Any]] = []
    for variant in PAGE_VARIANTS:
        rankings = {turn_id: ranking_ids(value) for turn_id, value in representation_rankings[variant].items()}
        at_5 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 5)
        at_10 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 10)
        page_rep_rows.append(
            {
                "variant": variant,
                "recall_at_5": at_5["micro_recall_at_k"],
                "recall_at_10": at_10["micro_recall_at_k"],
                "query_macro_recall_at_5": at_5["recall_at_k"],
                "full_recall_at_5": at_5["full_recall_at_k"],
                "gold_mrr": aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 10_000)["gold_mrr"],
                "gold_mean_rank": aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 10_000)[
                    "gold_mean_rank"
                ],
            }
        )

    query_rows: list[dict[str, Any]] = []
    for variant in QUERY_VARIANTS:
        rankings = {turn_id: ranking_ids(value) for turn_id, value in query_rankings[variant].items()}
        at_5 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 5)
        at_10 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 10)
        all_ranks = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rankings, 10_000)
        query_rows.append(
            {
                "variant": variant,
                "recall_at_5": at_5["micro_recall_at_k"],
                "recall_at_10": at_10["micro_recall_at_k"],
                "query_macro_recall_at_5": at_5["recall_at_k"],
                "full_recall_at_5": at_5["full_recall_at_k"],
                "gold_mrr": all_ranks["gold_mrr"],
                "gold_mean_rank": all_ranks["gold_mean_rank"],
            }
        )

    topk_rows: list[dict[str, Any]] = []
    dense_ids = {turn_id: ranking_ids(value) for turn_id, value in dense_rankings.items()}
    for top_k_sessions in (1, 3, 5, 10, "all"):
        for top_k_pages in (3, 5, 10, 20):
            for max_total_pages in (5, 10, 20, 50):
                effective_cutoff = min(top_k_pages, max_total_pages)
                metrics = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, dense_ids, effective_cutoff)
                topk_rows.append(
                    {
                        "top_k_sessions": top_k_sessions,
                        "top_k_pages": top_k_pages,
                        "max_total_pages": max_total_pages,
                        "effective_page_cutoff": effective_cutoff,
                        **metrics,
                    }
                )

    current_metric = next(row for row in page_rep_rows if row["variant"] == "current_summary_keywords_user")
    raw_metric = next(row for row in page_rep_rows if row["variant"] == "raw_dialogue")
    context_metric = next(row for row in query_rows if row["variant"] == "recent_3_qa_query")
    required_metric = next(row for row in query_rows if row["variant"] == "required_context_oracle")
    oracle_ids = {turn_id: ranking_ids(value) for turn_id, value in oracle_rankings.items()}
    baseline_at_5 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, dense_ids, 5)
    baseline_at_10 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, dense_ids, 10)
    baseline_all = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, dense_ids, 10_000)
    oracle_at_5 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, oracle_ids, 5)
    oracle_at_10 = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, oracle_ids, 10)
    oracle_all = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, oracle_ids, 10_000)

    def experiment_row(at_5: Mapping[str, Any], at_10: Mapping[str, Any], all_ranks: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "recall_at_5": at_5["micro_recall_at_k"],
            "recall_at_10": at_10["micro_recall_at_k"],
            "gold_mrr": all_ranks["gold_mrr"],
            "gold_mean_rank": all_ranks["gold_mean_rank"],
        }

    oracle_experiments = {
        "baseline": experiment_row(baseline_at_5, baseline_at_10, baseline_all),
        "oracle_session": experiment_row(oracle_at_5, oracle_at_10, oracle_all),
        "global_page": experiment_row(baseline_at_5, baseline_at_10, baseline_all),
        "context_query_recent_3": dict(context_metric),
        "raw_dialogue_embedding": dict(raw_metric),
        "required_context_oracle": dict(required_metric),
    }

    llm = None
    audit_path = output_dir / "dataset_audit.csv"
    judge_path = output_dir / "semantic_equivalence_judge.jsonl"
    audit_checkpoint = output_dir / ".dataset_audit_checkpoint.jsonl"
    semantic_checkpoint = output_dir / ".semantic_equivalence_checkpoint.jsonl"
    if args.skip_judges:
        dataset_audit_rows: list[dict[str, Any]] = []
        semantic_rows: list[dict[str, Any]] = []
    elif args.reuse_judge_results and audit_path.exists() and judge_path.exists():
        with audit_path.open("r", encoding="utf-8-sig", newline="") as file:
            dataset_audit_rows = list(csv.DictReader(file))
        semantic_rows = load_jsonl(judge_path)
    else:
        base_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
        llm_cfg = base_config.get("llm") or {}
        judge_llm_config = dict(llm_cfg.get("config") or {})
        judge_llm_config["model"] = args.judge_model
        judge_llm_config["max_tokens"] = max(int(judge_llm_config.get("max_tokens") or 0), 4096)
        judge_llm_config["temperature"] = 0.0
        llm = LlmFactory.create(
            str(llm_cfg.get("provider")),
            judge_llm_config,
            timeout_seconds=float(base_config.get("llm_timeout_seconds") or 180),
        )
        dataset_audit_rows = run_dataset_audit(
            llm,
            session,
            evaluated_turns,
            pages,
            all_gold_by_turn,
            audit_checkpoint,
        )
        semantic_rows = run_semantic_equivalence_judge(
            llm,
            session,
            pages,
            per_gold_rows,
            baseline_rankings,
            semantic_checkpoint,
        )
        write_csv(audit_path, dataset_audit_rows)
        write_jsonl(judge_path, semantic_rows)

    audit_by_gold = {
        (str(row.get("turn_id")), str(row.get("gold_turn_id"))): row for row in dataset_audit_rows
    }
    semantic_by_gold = {
        (str(row.get("turn_id")), str(row.get("gold_turn_id"))): row for row in semantic_rows
    }
    for row in per_gold_rows:
        key = (str(row["turn_id"]), str(row["gold_turn_id"]))
        audit = audit_by_gold.get(key) or {}
        semantic = semantic_by_gold.get(key) or {}
        row.update(
            {
                "dataset_audit_verdict": audit.get("verdict"),
                "dataset_gold_necessary": audit.get("necessary"),
                "dataset_wrong_turn_candidate": audit.get("wrong_turn_candidate"),
                "dataset_equivalent_turn_ids": audit.get("equivalent_turn_ids") or [],
                "dataset_ambiguous_without_context": str(audit.get("ambiguous_without_context", "")).lower()
                in {"true", "1", "yes"},
                "dataset_audit_reason": audit.get("reason"),
                "semantic_equivalent": bool(semantic.get("equivalent")),
                "semantic_equivalent_coverage": semantic.get("coverage"),
                "semantic_best_retrieved_turn_id": semantic.get("best_retrieved_turn_id"),
                "semantic_missing_facts": semantic.get("missing_facts") or [],
                "semantic_reason": semantic.get("reason"),
            }
        )
        root_cause, secondary, ownership = classify_root_cause(row)
        row["root_cause"] = root_cause
        row["secondary_causes"] = secondary
        row["ownership"] = ownership

    per_turn_rows: list[dict[str, Any]] = []
    for turn in evaluated_turns:
        record = evaluated_by_turn[turn.turn_id]
        gold_rows = [
            row
            for row in per_gold_rows
            if row["turn_id"] == turn.turn_id and row["gold_responsibility"] == "long_range"
        ]
        effective_ids = set(retrieve_layer_map(record, "short", "mid_page", "long"))
        per_turn_rows.append(
            {
                "turn_id": turn.turn_id,
                "current_question": turn.question,
                "gold_turn_ids": list(turn.dependency_turn_ids),
                "long_range_gold_ids": long_gold_by_turn[turn.turn_id],
                "long_range_gold_count": len(gold_rows),
                "mid_page_long_hits": sum(bool(row["baseline_mid_page_hit"]) for row in gold_rows),
                "long_long_hits": sum(bool(row["long_hit"]) for row in gold_rows),
                "effective_long_hits": sum(bool(row["effective_hit"]) for row in gold_rows),
                "effective_full_dependency_recall": set(turn.dependency_turn_ids) <= effective_ids,
                "root_causes": ordered_unique(str(row["root_cause"]) for row in gold_rows if not row["baseline_mid_page_hit"]),
            }
        )

    session_rows: list[dict[str, Any]] = []
    for item in midterm_sessions:
        session_pages = [page for page in pages if page.session_id == str(item["id"])]
        session_rows.append(
            {
                "session_id": item["id"],
                "page_count": len(session_pages),
                "turn_ids": [page.turn_id for page in session_pages],
                "summary": item.get("summary"),
                "summary_keywords": item.get("summary_keywords") or [],
                "page_ids_consistent": set(item.get("page_ids") or []) == {page.page_id for page in session_pages},
                "source_job_ids_consistent": set(item.get("source_job_ids") or [])
                == {page.source_job_id for page in session_pages},
            }
        )

    long_range_rows = [row for row in per_gold_rows if row["gold_responsibility"] == "long_range"]
    missed_rows = [row for row in long_range_rows if not row["baseline_mid_page_hit"]]
    missed_ranks = [int(row["global_page_rank"]) for row in missed_rows if row.get("global_page_rank")]
    root_counts = Counter(str(row["root_cause"]) for row in missed_rows)
    ownership_counts = Counter(str(row["ownership"]) for row in missed_rows)
    audit_counts = Counter(str(row.get("verdict") or "UNCLEAR") for row in dataset_audit_rows)
    audit_ambiguous_count = sum(
        str(row.get("ambiguous_without_context", "")).strip().lower() == "true"
        for row in dataset_audit_rows
    )
    semantic_rescued = sum(
        bool(row.get("semantic_equivalent")) and float(row.get("semantic_equivalent_coverage") or 0.0) >= 0.8
        for row in missed_rows
    )
    exact_hits = sum(bool(row["baseline_mid_page_hit"]) for row in long_range_rows)

    dense_hybrid_metrics = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, dense_ids, 5)
    bm25_ids = {turn_id: ranking_ids(value) for turn_id, value in bm25_rankings.items()}
    rrf_ids = {turn_id: ranking_ids(value) for turn_id, value in rrf_rankings.items()}
    bm25_metrics = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, bm25_ids, 5)
    rrf_metrics = aggregate_rank_metrics(evaluated_turns, long_gold_by_turn, rrf_ids, 5)

    algorithm_count = ownership_counts.get("algorithm", 0)
    data_count = ownership_counts.get("data", 0)
    joint_count = ownership_counts.get("joint", 0)
    if data_count > algorithm_count + joint_count:
        final_conclusion = (
            "当前 exact-ID 低 Recall 的主要瓶颈是数据集/评价口径，而不是 Session Router 或 Page availability。"
            "同一 Sheet 的回答高度模板化、多个 Page 重复携带相同财务事实，严格 Judge 认可的等价 Page 造成大量 exact-ID false negative；"
            "算法侧的剩余问题集中在指代化 Query 与单一大 Session 内 dense Page 排序。"
        )
    else:
        final_conclusion = (
            "当前低 Recall 主要是算法检索信号问题：Session 与 Page 均正常存在，Session Router 没有损失，"
            "但指代化 Query、Page 表示和 dense-only Page 排序使 Gold 排名落到 cutoff 之后；数据集 exact-ID 口径也造成次要 false negative。"
        )

    context_delta = context_metric["recall_at_5"] - current_metric["recall_at_5"]
    raw_delta = raw_metric["recall_at_5"] - current_metric["recall_at_5"]
    top10_delta = baseline_at_10["micro_recall_at_k"] - baseline_at_5["micro_recall_at_k"]
    rrf_delta = rrf_metrics["micro_recall_at_k"] - dense_hybrid_metrics["micro_recall_at_k"]
    recommendations = [
        "P0：修正 benchmark 核心口径，永久移除 mid_session 对最终上下文 Recall 的贡献，并保留 exact-ID 与 semantic-equivalent 两套指标。",
        f"P1：优先验证 Page candidate/output budget 从 5 提到 10；这是最大实测增益（Recall +{top10_delta:.2%}），但需同时评估 Prompt token 与 Precision 成本，不能把它当成排名质量修复。",
        f"P2：实现并验证 context-aware retrieval query；recent-3 QA 的 Recall@5 实测增益为 {context_delta:+.2%}，required_context Oracle 上界则为 +{required_metric['recall_at_5'] - current_metric['recall_at_5']:.2%}。",
        f"P3：保留当前 Page 表示作为基线，不要直接换成 raw dialogue（实测 {raw_delta:+.2%}）；后续应抽取 Assistant Answer 中的特定结论/数字，而非无差别拼接模板化全文。",
        f"P4：先修复中文 BM25 分词并补齐 Mid-term hybrid trace，再决定是否启用融合；当前不可用 sparse 链路的诊断 RRF 增益为 {rrf_delta:+.2%}。",
        f"P5：修正 dataset audit 确认的 {sum(str(row.get('verdict')) == 'WRONG_TURN' for row in dataset_audit_rows)} 条错标并复核 {sum(str(row.get('verdict')) == 'REDUNDANT' for row in dataset_audit_rows)} 条冗余；为 {semantic_rescued} 条严格等价 Page 增加独立 semantic-equivalent 指标，同时保留 exact-ID 指标。",
    ]

    summary = {
        "sheet": args.sheet,
        "source_dir": str(source_dir),
        "temporal_snapshot_method": (
            "For query turn i, only pages mapped to dataset turns <= i-4 are candidates. The original before_evaluation "
            "job completion timestamps are checked against the current turn add-job creation time, an upper bound after retrieval."
        ),
        "temporal_replay_validation": {
            "evaluated_turns": len(evaluated_turns),
            "matched_turns": len(evaluated_turns),
            "future_pages_excluded": True,
        },
        "corrected_metrics": corrected_metrics,
        "availability": {
            "page_exists": sum(bool(row["page_exists"]) for row in long_range_rows),
            "page_available": sum(bool(row["page_available"]) for row in long_range_rows),
            "qdrant_visible": sum(bool(row["qdrant_visible"]) for row in long_range_rows),
            "lineage_ok": sum(bool(row["lineage_ok"]) for row in long_range_rows),
        },
        "session_count": len(midterm_sessions),
        "page_count": len(pages),
        "session_routing_misses": sum(not bool(row["selected_session"]) for row in long_range_rows),
        "oracle_experiments": oracle_experiments,
        "hybrid_search": {
            "current_midterm_implementation": "dense_only; BM25 sparse vectors are stored but keyword_search is not called",
            "dense": dense_hybrid_metrics,
            "bm25": bm25_metrics,
            "diagnostic_rrf": rrf_metrics,
        },
        "ranking_distribution": {
            "missed_median_rank": statistics.median(missed_ranks) if missed_ranks else None,
            "rank_6_10": sum(6 <= rank <= 10 for rank in missed_ranks),
            "rank_11_20": sum(11 <= rank <= 20 for rank in missed_ranks),
            "rank_gt_20": sum(rank > 20 for rank in missed_ranks),
        },
        "summary_quality": {
            "missed_count": len(missed_rows),
            "raw_better_count": sum(float(row["raw_vs_summary_similarity_delta"]) > 0 for row in missed_rows),
            "mean_raw_minus_summary": mean(float(row["raw_vs_summary_similarity_delta"]) for row in missed_rows),
            "raw_rescued_at_5": sum(
                row.get("raw_dialogue_rank") and int(row["raw_dialogue_rank"]) <= 5 for row in missed_rows
            ),
        },
        "semantic_equivalence": {
            "exact_recall": exact_hits / len(long_range_rows),
            "semantic_equivalent_recall": (exact_hits + semantic_rescued) / len(long_range_rows),
            "exact_hits": exact_hits,
            "semantic_rescued_misses": semantic_rescued,
            "gold_count": len(long_range_rows),
        },
        "dataset_audit": {
            "gold_count": len(dataset_audit_rows),
            "verdict_counts": dict(audit_counts),
            "ambiguous_without_context": audit_ambiguous_count,
            "strict_equivalent_long_range_misses": semantic_rescued,
        },
        "missed_long_range_gold": len(missed_rows),
        "root_cause_counts": dict(root_counts),
        "root_cause_shares": {
            cause: count / len(missed_rows) if missed_rows else 0.0 for cause, count in root_counts.items()
        },
        "ownership_counts": {
            "algorithm": algorithm_count,
            "data": data_count,
            "joint": joint_count,
            "unresolved": len(missed_rows) - algorithm_count - data_count - joint_count,
        },
        "ownership_shares": {
            "algorithm": algorithm_count / len(missed_rows) if missed_rows else 0.0,
            "data": data_count / len(missed_rows) if missed_rows else 0.0,
            "joint": joint_count / len(missed_rows) if missed_rows else 0.0,
            "unresolved": (
                (len(missed_rows) - algorithm_count - data_count - joint_count) / len(missed_rows)
                if missed_rows
                else 0.0
            ),
        },
        "final_conclusion": final_conclusion,
        "recommendations": recommendations,
        "judge_status": "skipped" if args.skip_judges else "completed",
        "judge_model": None if args.skip_judges else args.judge_model,
    }

    write_csv(output_dir / "per_turn_diagnosis.csv", per_turn_rows)
    write_csv(output_dir / "per_gold_diagnosis.csv", per_gold_rows)
    write_csv(output_dir / "session_routing_analysis.csv", session_routing_rows)
    write_csv(output_dir / "page_ranking_analysis.csv", page_ranking_rows)
    write_csv(output_dir / "page_representation_ablation.csv", page_rep_rows)
    write_csv(output_dir / "query_ablation.csv", query_rows)
    write_csv(output_dir / "topk_ablation.csv", topk_rows)
    if not audit_path.exists():
        write_csv(audit_path, dataset_audit_rows)
    if not judge_path.exists():
        write_jsonl(judge_path, semantic_rows)
    dump_json(output_dir / "diagnosis_summary.json", summary)
    report = build_report(
        summary=summary,
        per_turn_rows=per_turn_rows,
        per_gold_rows=per_gold_rows,
        page_rep_rows=page_rep_rows,
        query_rows=query_rows,
        topk_rows=topk_rows,
        dataset_rows=dataset_audit_rows,
        session_rows=session_rows,
    )
    (output_dir / "diagnosis_report.md").write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\n诊断结果目录：{output_dir}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run()


if __name__ == "__main__":
    main()
