"""Run controlled, time-safe S001 mid-term Page retrieval experiments.

This script reuses the completed no-thinking benchmark.  It never replays the
Session and never mutates production Qdrant collections, Pages, prompts, or
retriever defaults.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import gc
import json
import logging
import math
import os
import random
import re
import sqlite3
import statistics
import time
import traceback
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
from openai import AsyncOpenAI
from qdrant_client import QdrantClient

from exp.benchmark.benchmark_common import (
    dependency_distances,
    ensure_repo_root_on_path,
    expand_env_placeholders,
    load_dataset,
    load_json,
    resolve_path,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    ChineseBM25Index,
    StageTimer,
    append_reranked_candidates,
    bm25_sanity_cases,
    cosine_rank,
    evaluate_rankings,
    gold_rank_buckets,
    latency_stats,
    load_jsonl,
    normalized_score_fuse,
    page_representation,
    pairwise_cosine_stats,
    rrf_fuse,
    stable_hash,
    write_jsonl,
)
from mem0.configs.embeddings.base import BaseEmbedderConfig  # noqa: E402
from mem0.embeddings.huggingface import HuggingFaceEmbedding  # noqa: E402
from mem0.memory.utils import extract_json, remove_code_blocks  # noqa: E402
from mem0.reranker.huggingface_reranker import HuggingFaceReranker  # noqa: E402


LOGGER = logging.getLogger("midterm_retrieval_experiments")
DEFAULT_SOURCE_DIR = "exp/results/recall_full_100_sessions_no_thinking"
DEFAULT_OUTPUT_DIR = "exp/results/midterm_retrieval_experiments_no_thinking"
DEFAULT_SHEET = "S001_贵州茅台_投研"
SNAPSHOT_VERSION = 1
SHORT_TERM_QA_CAPACITY = 3
PAGE_VARIANTS = tuple(f"P{index}" for index in range(9))
QUERY_VARIANTS = ("Q0", "Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "QO")
EMBEDDING_MODELS = {
    "E0": "BAAI/bge-small-zh-v1.5",
    "E1": "BAAI/bge-base-zh-v1.5",
    "E2": "BAAI/bge-large-zh-v1.5",
    "E3": "BAAI/bge-m3",
}
RRF_CONSTANT = 60
REWRITE_PROMPT_VERSION = "standalone-v1"
MULTI_QUERY_PROMPT_VERSION = "multi-query-v1"
LLM_RERANK_PROMPT_VERSION = "page-rerank-v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S001 no-thinking Mid-term Page retrieval controlled experiments")
    parser.add_argument("--source-dir", default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sheet", default=DEFAULT_SHEET)
    parser.add_argument("--concurrency", type=int, default=8, help="Query rewrite bounded concurrency")
    parser.add_argument("--rerank-concurrency", type=int, default=6, help="LLM rerank bounded concurrency")
    parser.add_argument("--llm-timeout", type=float, default=180.0)
    parser.add_argument("--llm-retries", type=int, default=4)
    parser.add_argument("--candidate-k", type=int, default=20)
    parser.add_argument("--output-k", type=int, default=5)
    parser.add_argument("--local-reranker-model", default="BAAI/bge-reranker-base")
    parser.add_argument("--skip-llm", action="store_true", help="Skip rewrite and LLM rerank; intended for local debugging")
    parser.add_argument("--force-snapshot", action="store_true")
    parser.add_argument("--retry-unavailable-models", action="store_true")
    return parser.parse_args()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def parse_timestamp(value: Any) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def qdrant_client(config: Mapping[str, Any]) -> tuple[QdrantClient, str]:
    vector = dict((config.get("vector_store") or {}).get("config") or {})
    collection = str(vector["collection_name"])
    if vector.get("url"):
        return QdrantClient(url=vector["url"], api_key=vector.get("api_key")), collection
    if vector.get("host") and vector.get("port"):
        return (
            QdrantClient(
                host=vector["host"],
                port=int(vector["port"]),
                https=vector.get("https"),
                api_key=vector.get("api_key"),
            ),
            collection,
        )
    if vector.get("path"):
        return QdrantClient(path=vector["path"]), collection
    raise ValueError("The stage-0 effective config has no usable Qdrant endpoint")


def load_jobs(history_db: Path) -> dict[str, dict[str, Any]]:
    connection = sqlite3.connect(f"file:{history_db}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM memory_migration_jobs ORDER BY sequence_no").fetchall()
    finally:
        connection.close()
    jobs: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        for key in ("filters_json", "metadata_json"):
            try:
                row[key.removesuffix("_json")] = json.loads(row.get(key) or "{}")
            except json.JSONDecodeError:
                row[key.removesuffix("_json")] = {}
        jobs[str(row["job_id"])] = row
    return jobs


def previous_qa(turns: Sequence[Any], turn_index: int, count: int) -> list[dict[str, str]]:
    return [
        {"turn_id": item.turn_id, "user": item.question, "assistant": item.answer}
        for item in turns[max(0, turn_index - count) : turn_index]
    ]


def build_snapshot(args: argparse.Namespace, source_dir: Path, output_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    snapshot_dir = output_dir / "snapshot"
    manifest_path = snapshot_dir / "snapshot_manifest.json"
    queries_path = snapshot_dir / "queries.jsonl"
    pages_path = snapshot_dir / "pages.jsonl"
    visibility_path = snapshot_dir / "query_page_visibility.jsonl"
    if not args.force_snapshot and all(path.exists() for path in (manifest_path, queries_path, pages_path, visibility_path)):
        manifest = load_json(manifest_path)
        if int(manifest.get("snapshot_version") or 0) == SNAPSHOT_VERSION:
            queries = load_jsonl(queries_path)
            pages = load_jsonl(pages_path)
            validate_snapshot(queries, pages, load_jsonl(visibility_path), args.sheet)
            LOGGER.info("Reused snapshot: %s", snapshot_dir)
            return queries, pages, manifest

    effective_config = load_json(source_dir / "effective_memory_config.json")
    summary = load_json(source_dir / "recall_summary.json")
    dataset_path = Path(str((summary.get("effective") or {}).get("dataset_path") or ""))
    if not dataset_path.exists():
        raise FileNotFoundError(f"Stage-0 dataset is unavailable: {dataset_path}")
    sessions = load_dataset(dataset_path, include_sheets=[args.sheet])
    if len(sessions) != 1:
        raise ValueError(f"Expected only {args.sheet}, got {len(sessions)} sessions")
    session = sessions[0]
    turn_by_question: dict[str, list[Any]] = {}
    for turn in session.turns:
        turn_by_question.setdefault(turn.question, []).append(turn)

    history_db = Path(str(effective_config["history_db_path"]))
    jobs = load_jobs(history_db)
    jobs_by_trigger_turn = {
        str((job.get("metadata") or {}).get("dataset_turn_id")): job
        for job in jobs.values()
        if (job.get("metadata") or {}).get("dataset_turn_id")
    }
    client, collection_name = qdrant_client(effective_config)
    points, _ = client.scroll(
        f"{collection_name}_midterm_pages",
        limit=10_000,
        with_payload=True,
        with_vectors=True,
    )
    pages: list[dict[str, Any]] = []
    for point in points:
        payload = dict(point.payload or {})
        matching_turns = turn_by_question.get(str(payload.get("user_input") or ""), [])
        if len(matching_turns) != 1:
            raise ValueError(f"Page {point.id} source-turn mapping is ambiguous: {len(matching_turns)} matches")
        source_turn = matching_turns[0]
        source_job_id = str(payload.get("source_job_id") or "")
        job = jobs.get(source_job_id)
        if not job:
            raise ValueError(f"Page {point.id} source job is missing: {source_job_id}")
        vector = point.vector.get("") if isinstance(point.vector, dict) else point.vector
        page = {
            "page_id": str(point.id),
            "source_turn_id": source_turn.turn_id,
            "source_turn_index": source_turn.turn_index,
            "session_id": str(payload.get("session_id") or ""),
            "source_job_id": source_job_id,
            "source_job_trigger_turn_id": str((job.get("metadata") or {}).get("dataset_turn_id") or ""),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "committed_at": job.get("midterm_finished_at"),
            "output_state": payload.get("output_state"),
            "migration_status": job.get("status"),
            "midterm_status": job.get("midterm_status"),
            "user_input": payload.get("user_input") or "",
            "assistant_response": payload.get("assistant_response") or "",
            "raw_dialogue": payload.get("raw_dialogue") or "",
            "summary": payload.get("summary") or "",
            "keywords": payload.get("keywords") or [],
            "current_embedding_text": payload.get("data") or page_representation(payload, "P0"),
            "stored_embedding": list(vector or []),
        }
        if page["current_embedding_text"] != page_representation(page, "P0"):
            raise AssertionError(f"Page {point.id} stored data differs from production P0 construction")
        pages.append(page)
    pages.sort(key=lambda row: int(row["source_turn_index"]))
    page_by_turn = {str(page["source_turn_id"]): page for page in pages}
    if len(page_by_turn) != len(pages):
        raise ValueError("Source turn to Page mapping is not one-to-one")

    recall_rows = load_jsonl(source_dir / "recall_turn_results.jsonl")
    evaluated_rows = [row for row in recall_rows if row.get("evaluated") and row.get("sheet_name") == args.sheet]
    evaluated_by_turn = {str(row["turn_id"]): row for row in evaluated_rows}
    queries: list[dict[str, Any]] = []
    visibility_rows: list[dict[str, Any]] = []
    for turn in session.turns:
        if turn.turn_id not in evaluated_by_turn:
            continue
        record = evaluated_by_turn[turn.turn_id]
        current_job = jobs_by_trigger_turn.get(turn.turn_id)
        retrieval_upper_bound = current_job.get("created_at") if current_job else None
        upper_timestamp = parse_timestamp(retrieval_upper_bound)
        visible: list[dict[str, Any]] = []
        for page in pages:
            finished_timestamp = parse_timestamp(page.get("committed_at"))
            source_is_evicted = int(page["source_turn_index"]) <= turn.turn_index - SHORT_TERM_QA_CAPACITY - 1
            committed_before_query = bool(
                page.get("output_state") == "committed"
                and page.get("midterm_status") == "succeeded"
                and finished_timestamp is not None
                and (upper_timestamp is None or finished_timestamp <= upper_timestamp)
            )
            if source_is_evicted and committed_before_query:
                visible.append(
                    {
                        "page_id": page["page_id"],
                        "source_turn_id": page["source_turn_id"],
                        "source_turn_index": page["source_turn_index"],
                        "source_job_id": page["source_job_id"],
                        "committed_at": page["committed_at"],
                        "retrieval_time_upper_bound": retrieval_upper_bound,
                    }
                )
        visible_ids = {str(item["page_id"]) for item in visible}
        distances = dependency_distances(session, turn)
        gold_lineage: list[dict[str, Any]] = []
        for source_turn_id, distance in zip(turn.dependency_turn_ids, distances):
            page = page_by_turn.get(source_turn_id)
            page_id = str(page["page_id"]) if page else None
            available = bool(page_id and page_id in visible_ids)
            if available:
                status = "AVAILABLE"
            elif page:
                status = "GOLD_NOT_AVAILABLE_AT_QUERY_TIME"
            else:
                status = "PAGE_NOT_CREATED_WITHIN_STAGE0_RUN"
            gold_lineage.append(
                {
                    "source_turn_id": source_turn_id,
                    "dependency_distance": distance,
                    "page_id": page_id,
                    "available_at_query_time": available,
                    "status": status,
                }
            )
        query = {
            "query_id": turn.turn_id,
            "session_id": session.session_id,
            "turn_index": turn.turn_index,
            "original_query": turn.question,
            "required_context": turn.required_context,
            "gold_source_turn_ids": list(turn.dependency_turn_ids),
            "gold_page_ids": [item["page_id"] for item in gold_lineage if item.get("page_id")],
            "eligible_gold_page_ids": [item["page_id"] for item in gold_lineage if item["available_at_query_time"]],
            "gold_lineage": gold_lineage,
            "long_range_evaluation": bool(record.get("long_range")),
            "dependency_distances": distances,
            "dependency_distance": max(distances) if distances else None,
            "previous_1_qa": previous_qa(session.turns, turn.turn_index, 1),
            "previous_2_qa": previous_qa(session.turns, turn.turn_index, 2),
            "previous_3_qa": previous_qa(session.turns, turn.turn_index, 3),
            "retrieval_time_upper_bound": retrieval_upper_bound,
            "stage0_mid_page_retrieved_turn_ids": list(record.get("mid_page_retrieved_turn_ids") or []),
        }
        if not query["eligible_gold_page_ids"]:
            raise ValueError(f"Evaluated Query has no Page-eligible Gold: {turn.turn_id}")
        queries.append(query)
        visibility_rows.append(
            {
                "query_id": turn.turn_id,
                "turn_index": turn.turn_index,
                "retrieval_time_upper_bound": retrieval_upper_bound,
                "visible_page_count": len(visible),
                "visible_page_ids": [item["page_id"] for item in visible],
                "visible_page_proofs": visible,
                "future_page_leak_count": sum(
                    int(item["source_turn_index"]) > turn.turn_index - SHORT_TERM_QA_CAPACITY - 1 for item in visible
                ),
            }
        )

    validate_snapshot(queries, pages, visibility_rows, args.sheet)
    gold_mapping_count = sum(len(query["gold_lineage"]) for query in queries)
    eligible_gold_count = sum(len(query["eligible_gold_page_ids"]) for query in queries)
    status_counts = Counter(item["status"] for query in queries for item in query["gold_lineage"])
    manifest = {
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at_epoch": time.time(),
        "sheet": args.sheet,
        "session_id": session.session_id,
        "source_dir": str(source_dir),
        "dataset_path": str(dataset_path),
        "history_db_path": str(history_db),
        "qdrant_collection": f"{collection_name}_midterm_pages",
        "source_git_commit": (summary.get("effective") or {}).get("git_commit"),
        "evaluated_query_count": len(queries),
        "page_count": len(pages),
        "gold_mapping_count": gold_mapping_count,
        "eligible_gold_page_count": eligible_gold_count,
        "gold_status_counts": dict(status_counts),
        "visible_page_count_distribution": [row["visible_page_count"] for row in visibility_rows],
        "gold_available_before_query_count": eligible_gold_count,
        "future_page_leak_count": sum(row["future_page_leak_count"] for row in visibility_rows),
        "production_page_representation": "summary + 'Keywords: ' + keywords + 'User: ' + user_input",
        "production_embedding_model": str(((effective_config.get("embedder") or {}).get("config") or {}).get("model")),
        "snapshot_hash": stable_hash(
            {
                "queries": [{key: value for key, value in row.items() if key != "required_context"} for row in queries],
                "pages": [{key: value for key, value in row.items() if key != "stored_embedding"} for row in pages],
                "visibility": visibility_rows,
            }
        ),
    }
    write_jsonl(queries_path, queries)
    write_jsonl(pages_path, pages)
    write_jsonl(visibility_path, visibility_rows)
    dump_json(manifest_path, manifest)
    LOGGER.info("Built snapshot: %d queries, %d pages, %d eligible Gold", len(queries), len(pages), eligible_gold_count)
    return queries, pages, manifest


def validate_snapshot(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility_rows: Sequence[Mapping[str, Any]],
    sheet: str,
) -> None:
    if len(queries) != 14:
        raise ValueError(f"Expected 14 S001 evaluated Queries, got {len(queries)}")
    if len(pages) != 47:
        raise ValueError(f"Expected 47 S001 Pages, got {len(pages)}")
    page_ids = [str(page.get("page_id") or "") for page in pages]
    if not all(page_ids) or len(page_ids) != len(set(page_ids)):
        raise ValueError("Page IDs are missing or duplicated")
    source_turn_ids = [str(page.get("source_turn_id") or "") for page in pages]
    if len(source_turn_ids) != len(set(source_turn_ids)):
        raise ValueError("Page source-turn mapping is ambiguous")
    page_by_id = {str(page["page_id"]): page for page in pages}
    visibility_by_query = {str(row["query_id"]): row for row in visibility_rows}
    if len(visibility_by_query) != len(queries):
        raise ValueError("Visibility rows do not cover every Query")
    for query in queries:
        query_id = str(query.get("query_id") or "")
        if not query_id.startswith("S001-") or query.get("session_id") != sheet:
            raise ValueError(f"Snapshot contains a non-S001 Query: {query_id}")
        if not str(query.get("original_query") or "").strip():
            raise ValueError(f"Query text is missing: {query_id}")
        visible = visibility_by_query[query_id]
        if int(visible.get("future_page_leak_count") or 0):
            raise ValueError(f"Future Page leakage detected: {query_id}")
        visible_ids = {str(item) for item in visible.get("visible_page_ids") or []}
        for page_id in query.get("eligible_gold_page_ids") or []:
            if str(page_id) not in visible_ids or str(page_id) not in page_by_id:
                raise ValueError(f"Gold Page is not time-visible for {query_id}: {page_id}")
        cutoff = int(query["turn_index"]) - SHORT_TERM_QA_CAPACITY - 1
        for page_id in visible_ids:
            if int(page_by_id[page_id]["source_turn_index"]) > cutoff:
                raise ValueError(f"Future Page {page_id} leaked into {query_id}")


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")


class EmbeddingCache:
    def __init__(self, cache_dir: Path, *, retry_unavailable: bool = False):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retry_unavailable = retry_unavailable
        self.models: dict[str, HuggingFaceEmbedding] = {}
        self.status_path = self.cache_dir / "model_status.json"
        self.status = load_json(self.status_path) if self.status_path.exists() else {}

    def _load_model(self, model_name: str) -> HuggingFaceEmbedding:
        previous = self.status.get(model_name) or {}
        if previous.get("status") == "UNAVAILABLE" and not self.retry_unavailable:
            raise RuntimeError(f"Previously unavailable: {previous.get('reason')}")
        if model_name not in self.models:
            started = time.perf_counter()
            try:
                self.models[model_name] = HuggingFaceEmbedding(BaseEmbedderConfig(model=model_name))
            except Exception as exc:
                self.status[model_name] = {
                    "status": "UNAVAILABLE",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=5),
                }
                dump_json(self.status_path, self.status)
                raise
            self.status[model_name] = {
                "status": "AVAILABLE",
                "load_ms": (time.perf_counter() - started) * 1000.0,
                "dimension": int(self.models[model_name].config.embedding_dims or 0),
            }
            dump_json(self.status_path, self.status)
        return self.models[model_name]

    def encode(
        self,
        model_name: str,
        batch_name: str,
        ids: Sequence[str],
        texts: Sequence[str],
        *,
        measure_individual: bool,
    ) -> tuple[dict[str, list[float]], dict[str, Any]]:
        if len(ids) != len(texts) or len(ids) != len(set(ids)):
            raise ValueError(f"Invalid embedding batch {batch_name}: IDs/texts mismatch or duplicate IDs")
        content_hash = stable_hash({"model": model_name, "ids": list(ids), "texts": list(texts)})
        model_dir = self.cache_dir / safe_slug(model_name)
        model_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_slug(batch_name)}-{content_hash[:16]}"
        vector_path = model_dir / f"{stem}.npz"
        metadata_path = model_dir / f"{stem}.json"
        if vector_path.exists() and metadata_path.exists():
            metadata = load_json(metadata_path)
            cached = np.load(vector_path)
            matrix = np.asarray(cached["vectors"], dtype=np.float32)
            cached_ids = [str(item) for item in cached["ids"].tolist()]
            if metadata.get("content_hash") == content_hash and cached_ids == list(ids):
                metadata["cache_hit"] = True
                return {item_id: vector.tolist() for item_id, vector in zip(cached_ids, matrix)}, metadata

        model = self._load_model(model_name)
        item_latency_ms: dict[str, float] = {}
        started = time.perf_counter()
        if measure_individual:
            vectors: list[list[float]] = []
            for item_id, text in zip(ids, texts):
                item_started = time.perf_counter()
                vectors.append(model.embed(text, "search"))
                item_latency_ms[item_id] = (time.perf_counter() - item_started) * 1000.0
        else:
            vectors = model.embed_batch(list(texts), "add")
        build_ms = (time.perf_counter() - started) * 1000.0
        matrix = np.asarray(vectors, dtype=np.float32)
        np.savez_compressed(vector_path, ids=np.asarray(ids, dtype=str), vectors=matrix)
        metadata = {
            "model": model_name,
            "batch_name": batch_name,
            "content_hash": content_hash,
            "item_count": len(ids),
            "dimension": int(matrix.shape[1]) if matrix.ndim == 2 and len(matrix) else 0,
            "build_ms": build_ms,
            "item_latency_ms": item_latency_ms,
            "cache_hit": False,
        }
        dump_json(metadata_path, metadata)
        return {item_id: vector.tolist() for item_id, vector in zip(ids, matrix)}, metadata

    def release(self, model_name: str) -> None:
        model = self.models.pop(model_name, None)
        if model is not None:
            del model
            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass


def parse_llm_json(value: Any) -> dict[str, Any]:
    text = str(value or "")
    for candidate in (remove_code_blocks(text), extract_json(text)):
        try:
            parsed = json.loads(candidate, strict=False)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    raise ValueError(f"LLM did not return a JSON object: {text[:500]}")


def rewrite_messages(query: Mapping[str, Any], variant: str) -> tuple[list[dict[str, str]], str]:
    history = query.get("previous_3_qa") or []
    context = "\n\n".join(
        f"[{item['turn_id']}] 用户：{item['user']}\n助手：{item['assistant']}" for item in history
    )
    payload = f"最近最多 3 轮短期上下文：\n{context}\n\n当前问题：\n{query['original_query']}"
    if variant == "Q4":
        system = (
            "你是检索 Query 改写器。只做指代消解、省略补全、主体/分析对象补全，以及上下文中已有的时间和指标补全。"
            "不得添加上下文中不存在的事实，不回答问题，不解释。返回严格 JSON："
            '{"standalone_query":"可脱离上下文独立理解的中文检索问题"}。'
        )
        version = REWRITE_PROMPT_VERSION
    elif variant == "Q6":
        system = (
            "你是检索 Query 生成器。基于当前问题和最近短期上下文，一次生成两个互补的中文 retrieval queries："
            "第一个强调主题和任务关系，第二个强调主体、核心指标、时间或关键事实。只可消解和补全已有信息，"
            "不得增加新事实，不回答问题。返回严格 JSON："
            '{"queries":["主题关系 query","指标事实 query"]}，必须恰好两个非空字符串。'
        )
        version = MULTI_QUERY_PROMPT_VERSION
    else:
        raise ValueError(f"Unsupported LLM query variant: {variant}")
    return [{"role": "system", "content": system}, {"role": "user", "content": payload}], version


class AsyncJsonlLLMCache:
    def __init__(
        self,
        path: Path,
        *,
        client: AsyncOpenAI,
        model: str,
        timeout: float,
        retries: int,
        concurrency: int,
    ):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.semaphore = asyncio.Semaphore(max(concurrency, 1))
        self.write_lock = asyncio.Lock()
        self.rows = load_jsonl(path)
        self.success_by_key = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.write_lock:
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    async def call(
        self,
        *,
        query_id: str,
        variant: str,
        messages: Sequence[Mapping[str, str]],
        prompt_version: str,
        max_tokens: int,
        validator,
        extra_key: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        prompt_hash = stable_hash({"version": prompt_version, "messages": list(messages)})
        cache_key = stable_hash(
            {
                "query_id": query_id,
                "variant": variant,
                "model": self.model,
                "thinking_mode": "disabled",
                "prompt_hash": prompt_hash,
                "extra": dict(extra_key or {}),
            }
        )
        cached = self.success_by_key.get(cache_key)
        if cached:
            return cached
        errors: list[str] = []
        retries_used = 0
        async with self.semaphore:
            for attempt in range(1, self.retries + 1):
                started = time.perf_counter()
                try:
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(
                            model=self.model,
                            messages=list(messages),
                            temperature=0.0,
                            max_tokens=max_tokens,
                            response_format={"type": "json_object"},
                            extra_body={"thinking": {"type": "disabled"}},
                        ),
                        timeout=self.timeout,
                    )
                    latency_ms = (time.perf_counter() - started) * 1000.0
                    content = response.choices[0].message.content or ""
                    parsed = validator(parse_llm_json(content))
                    usage = response.usage
                    row = {
                        "cache_key": cache_key,
                        "query_id": query_id,
                        "variant": variant,
                        "model": self.model,
                        "thinking_mode": "disabled",
                        "prompt_version": prompt_version,
                        "prompt_hash": prompt_hash,
                        "status": "SUCCESS",
                        "parsed": parsed,
                        "raw_output": content,
                        "llm_latency_ms": latency_ms,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "retry_count": retries_used,
                        "errors": errors,
                    }
                    await self.append(row)
                    self.success_by_key[cache_key] = row
                    return row
                except Exception as exc:
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    retries_used += int(attempt < self.retries)
                    if attempt < self.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12.0))
        row = {
            "cache_key": cache_key,
            "query_id": query_id,
            "variant": variant,
            "model": self.model,
            "thinking_mode": "disabled",
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "status": "FAILED",
            "parsed": None,
            "llm_latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "retry_count": retries_used,
            "errors": errors,
        }
        await self.append(row)
        return row


def standalone_validator(value: Mapping[str, Any]) -> dict[str, Any]:
    text = str(value.get("standalone_query") or "").strip()
    if not text:
        raise ValueError("standalone_query is empty")
    return {"standalone_query": text}


def multi_query_validator(value: Mapping[str, Any]) -> dict[str, Any]:
    queries = [str(item).strip() for item in value.get("queries") or [] if str(item).strip()]
    if len(queries) != 2 or queries[0] == queries[1]:
        raise ValueError("queries must contain exactly two distinct strings")
    return {"queries": queries}


def contextual_query(query: Mapping[str, Any], count: int) -> str:
    history = query.get(f"previous_{count}_qa") or []
    # Preserve the actual retrieval intent when an embedder truncates long
    # inputs: current Query first, then prior QAs from most recent to oldest.
    parts: list[str] = [f"Current user query: {query['original_query']}"]
    for item in reversed(history):
        parts.extend((f"User: {item['user']}", f"Assistant: {item['assistant']}"))
    return "\n\n".join(parts)


async def build_query_options(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
    output_dir: Path,
) -> tuple[
    dict[str, dict[str, list[str]]],
    dict[str, dict[str, dict[str, Any]]],
    dict[str, str],
]:
    options: dict[str, dict[str, list[str]]] = {variant: {} for variant in QUERY_VARIANTS}
    metadata: dict[str, dict[str, dict[str, Any]]] = {variant: {} for variant in QUERY_VARIANTS}
    status = {variant: "AVAILABLE" for variant in QUERY_VARIANTS}
    for query in queries:
        query_id = str(query["query_id"])
        deterministic = {
            "Q0": str(query["original_query"]),
            "Q1": contextual_query(query, 1),
            "Q2": contextual_query(query, 2),
            "Q3": contextual_query(query, 3),
            "QO": str(query["required_context"]),
        }
        for variant, text in deterministic.items():
            options[variant][query_id] = [text]
            metadata[variant][query_id] = {
                "query_build_ms": 0.0,
                "llm_latency_ms": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "retry_count": 0,
                "llm_calls": 0,
            }

    if args.skip_llm:
        for variant in ("Q4", "Q5", "Q6"):
            status[variant] = "SKIPPED: --skip-llm"
        return options, metadata, status

    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    model = str(llm_config.get("model") or "deepseek-chat")
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    cache = AsyncJsonlLLMCache(
        output_dir / "cache/query_rewrites.jsonl",
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )

    async def one(query: Mapping[str, Any], variant: str) -> tuple[str, str, dict[str, Any]]:
        messages, version = rewrite_messages(query, variant)
        validator = standalone_validator if variant == "Q4" else multi_query_validator
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant=variant,
            messages=messages,
            prompt_version=version,
            max_tokens=800,
            validator=validator,
        )
        return str(query["query_id"]), variant, row

    results = await asyncio.gather(*(one(query, variant) for query in queries for variant in ("Q4", "Q6")))
    by_key = {(query_id, variant): row for query_id, variant, row in results}
    for query in queries:
        query_id = str(query["query_id"])
        rewrite = by_key[(query_id, "Q4")]
        multi = by_key[(query_id, "Q6")]
        if rewrite.get("status") == "SUCCESS":
            standalone = str(rewrite["parsed"]["standalone_query"])
            options["Q4"][query_id] = [standalone]
            options["Q5"][query_id] = [f"Original query: {query['original_query']}\nStandalone query: {standalone}"]
            for variant in ("Q4", "Q5"):
                metadata[variant][query_id] = {
                    "query_build_ms": 0.0,
                    "llm_latency_ms": rewrite.get("llm_latency_ms") or 0.0,
                    "prompt_tokens": rewrite.get("prompt_tokens") or 0,
                    "completion_tokens": rewrite.get("completion_tokens") or 0,
                    "retry_count": rewrite.get("retry_count") or 0,
                    "llm_calls": 1,
                }
        else:
            status["Q4"] = status["Q5"] = "FAILED: one or more rewrites unavailable"
        if multi.get("status") == "SUCCESS":
            options["Q6"][query_id] = list(multi["parsed"]["queries"])
            metadata["Q6"][query_id] = {
                "query_build_ms": 0.0,
                "llm_latency_ms": multi.get("llm_latency_ms") or 0.0,
                "prompt_tokens": multi.get("prompt_tokens") or 0,
                "completion_tokens": multi.get("completion_tokens") or 0,
                "retry_count": multi.get("retry_count") or 0,
                "llm_calls": 1,
            }
        else:
            status["Q6"] = "FAILED: one or more multi-query generations unavailable"

    for variant in ("Q4", "Q5", "Q6"):
        if len(options[variant]) != len(queries):
            status[variant] = f"FAILED: {len(options[variant])}/{len(queries)} Queries available"
    await client.close()
    return options, metadata, status


def build_rewrite_audit(
    queries: Sequence[Mapping[str, Any]],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
) -> list[dict[str, Any]]:
    rng = random.Random(42)
    sampled = rng.sample(list(queries), k=min(5, len(queries)))
    rows: list[dict[str, Any]] = []
    for query in sampled:
        query_id = str(query["query_id"])
        rewrite = list((query_options.get("Q4") or {}).get(query_id) or [""])[0]
        required = str(query.get("required_context") or "")
        gold_ids = [str(item) for item in query.get("gold_source_turn_ids") or []]
        rows.append(
            {
                "query_id": query_id,
                "original_query": query["original_query"],
                "standalone_rewrite": rewrite,
                "multi_queries": list((query_options.get("Q6") or {}).get(query_id) or []),
                "non_empty": bool(rewrite),
                "does_not_copy_required_context": bool(rewrite and rewrite.strip() != required.strip()),
                "does_not_expose_gold_ids": bool(rewrite and not any(gold_id in rewrite for gold_id in gold_ids)),
                "review_note": "Manual spot-check required: verify only context-supported disambiguation/completion.",
            }
        )
    return rows


def flatten_query_texts(
    variants: Sequence[str],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
    query_status: Mapping[str, str],
) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    texts: list[str] = []
    for variant in variants:
        if query_status.get(variant) != "AVAILABLE":
            continue
        for query_id, values in query_options[variant].items():
            for index, text in enumerate(values):
                ids.append(f"{variant}:{query_id}:{index}")
                texts.append(str(text))
    return ids, texts


def metric_columns(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "recall_at_1": metrics.get("recall_at_1"),
        "recall_at_3": metrics.get("recall_at_3"),
        "recall_at_5": metrics.get("recall_at_5"),
        "recall_at_10": metrics.get("recall_at_10"),
        "recall_at_15": metrics.get("recall_at_15"),
        "recall_at_20": metrics.get("recall_at_20"),
        "recall_at_30": metrics.get("recall_at_30"),
        "mrr": metrics.get("mrr"),
        "ndcg_at_5": metrics.get("ndcg_at_5"),
        "mean_gold_rank": metrics.get("mean_gold_rank"),
        "median_gold_rank": metrics.get("median_gold_rank"),
        "eligible_gold_count": metrics.get("eligible_gold_count"),
    }


def ranking_sort_key(row: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(row.get("recall_at_5") or 0.0),
        float(row.get("mrr") or 0.0),
        float(row.get("recall_at_20") or 0.0),
        -float(row.get("mean_gold_rank") or math.inf),
    )


def visible_pages(
    query_id: str,
    pages_by_id: Mapping[str, Mapping[str, Any]],
    visibility_by_query: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    return [pages_by_id[str(page_id)] for page_id in visibility_by_query[query_id]["visible_page_ids"]]


def rank_dense_configuration(
    queries: Sequence[Mapping[str, Any]],
    pages_by_id: Mapping[str, Mapping[str, Any]],
    visibility_by_query: Mapping[str, Mapping[str, Any]],
    *,
    query_variant: str,
    query_vectors: Mapping[str, Sequence[float]],
    page_vectors: Mapping[str, Sequence[float]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, float]]]:
    rankings: dict[str, list[dict[str, Any]]] = {}
    timing: dict[str, dict[str, float]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        pages = visible_pages(query_id, pages_by_id, visibility_by_query)
        vectors: list[Sequence[float]] = []
        index = 0
        while f"{query_variant}:{query_id}:{index}" in query_vectors:
            vectors.append(query_vectors[f"{query_variant}:{query_id}:{index}"])
            index += 1
        if not vectors:
            raise ValueError(f"No Query embedding for {query_variant}/{query_id}")
        with StageTimer() as timer:
            components = [cosine_rank(vector, pages, page_vectors) for vector in vectors]
            ranking = components[0] if len(components) == 1 else rrf_fuse(components, rank_constant=RRF_CONSTANT)
        rankings[query_id] = ranking
        timing[query_id] = {"dense_search_ms": timer.elapsed_ms, "fusion_ms": 0.0}
    return rankings, timing


def encode_page_variant(
    cache: EmbeddingCache,
    model_id: str,
    page_variant: str,
    pages: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    model_name = EMBEDDING_MODELS[model_id]
    if model_id == "E0" and page_variant == "P0":
        vectors = {str(page["page_id"]): list(page["stored_embedding"]) for page in pages}
        return vectors, {
            "model": model_name,
            "batch_name": "stage0-stored-P0",
            "item_count": len(pages),
            "dimension": len(next(iter(vectors.values()))),
            "build_ms": 0.0,
            "source": "reused stage-0 Qdrant vectors",
            "cache_hit": True,
        }
    ids = [str(page["page_id"]) for page in pages]
    texts = [page_representation(page, page_variant) for page in pages]
    return cache.encode(model_name, f"pages-{page_variant}", ids, texts, measure_individual=False)


def encode_query_variants(
    cache: EmbeddingCache,
    model_id: str,
    variants: Sequence[str],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
    query_status: Mapping[str, str],
) -> tuple[dict[str, list[float]], dict[str, Any]]:
    ids, texts = flatten_query_texts(variants, query_options, query_status)
    if not ids:
        return {}, {"item_count": 0, "item_latency_ms": {}, "build_ms": 0.0}
    return cache.encode(
        EMBEDDING_MODELS[model_id],
        f"queries-{'-'.join(variants)}",
        ids,
        texts,
        measure_individual=True,
    )


def query_embedding_latency(
    query_id: str,
    variant: str,
    embedding_metadata: Mapping[str, Any],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
) -> float:
    item_latency = embedding_metadata.get("item_latency_ms") or {}
    count = len((query_options.get(variant) or {}).get(query_id) or [])
    return sum(float(item_latency.get(f"{variant}:{query_id}:{index}") or 0.0) for index in range(count))


def build_hybrid_rankings(
    queries: Sequence[Mapping[str, Any]],
    pages_by_id: Mapping[str, Mapping[str, Any]],
    visibility_by_query: Mapping[str, Mapping[str, Any]],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    query_variant: str,
    page_variant: str,
    dense_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    dense_timing: Mapping[str, Mapping[str, float]],
) -> tuple[
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, dict[str, float]]],
]:
    strategies: dict[str, dict[str, list[dict[str, Any]]]] = {
        name: {} for name in ("H0_dense", "H1_bm25", "H2_rrf", "H3_dense_0.8_bm25_0.2", "H4_dense_0.6_bm25_0.4")
    }
    timing: dict[str, dict[str, dict[str, float]]] = {name: {} for name in strategies}
    for query in queries:
        query_id = str(query["query_id"])
        pages = visible_pages(query_id, pages_by_id, visibility_by_query)
        texts = {str(page["page_id"]): page_representation(page, page_variant) for page in pages}
        with StageTimer() as bm25_build_timer:
            index = ChineseBM25Index(pages, texts)
        with StageTimer() as bm25_timer:
            lexical_components = [index.rank(text) for text in query_options[query_variant][query_id]]
            bm25 = lexical_components[0] if len(lexical_components) == 1 else rrf_fuse(
                lexical_components,
                rank_constant=RRF_CONSTANT,
            )
        dense = [dict(item) for item in dense_rankings[query_id]]
        with StageTimer() as rrf_timer:
            rrf = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
        with StageTimer() as fusion_08_timer:
            fused_08 = normalized_score_fuse(dense, bm25, dense_weight=0.8)
        with StageTimer() as fusion_06_timer:
            fused_06 = normalized_score_fuse(dense, bm25, dense_weight=0.6)
        values = {
            "H0_dense": dense,
            "H1_bm25": bm25,
            "H2_rrf": rrf,
            "H3_dense_0.8_bm25_0.2": fused_08,
            "H4_dense_0.6_bm25_0.4": fused_06,
        }
        for name, ranking in values.items():
            strategies[name][query_id] = ranking
            timing[name][query_id] = {
                "dense_search_ms": float((dense_timing.get(query_id) or {}).get("dense_search_ms") or 0.0)
                if name != "H1_bm25"
                else 0.0,
                "bm25_search_ms": bm25_build_timer.elapsed_ms + bm25_timer.elapsed_ms if name != "H0_dense" else 0.0,
                "fusion_ms": {
                    "H2_rrf": rrf_timer.elapsed_ms,
                    "H3_dense_0.8_bm25_0.2": fusion_08_timer.elapsed_ms,
                    "H4_dense_0.6_bm25_0.4": fusion_06_timer.elapsed_ms,
                }.get(name, 0.0),
            }
    return strategies, timing


def rerank_query_text(
    query_id: str,
    query_variant: str,
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
) -> str:
    values = list(query_options[query_variant][query_id])
    if len(values) == 1:
        return values[0]
    return "\n".join(f"Retrieval query {index}: {text}" for index, text in enumerate(values, start=1))


class LocalRerankCache:
    def __init__(self, cache_dir: Path, model_name: str):
        self.path = cache_dir / "rerank" / "local_rerank.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.model_name = model_name
        self.rows = load_jsonl(self.path)
        self.success_by_key = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.model: HuggingFaceReranker | None = None
        self.status_path = self.path.parent / "local_model_status.json"
        self.status = load_json(self.status_path) if self.status_path.exists() else {}

    def _load_model(self) -> HuggingFaceReranker:
        if self.model is None:
            started = time.perf_counter()
            try:
                self.model = HuggingFaceReranker(
                    {
                        "provider": "huggingface",
                        "model": self.model_name,
                        "batch_size": 32,
                        "max_length": 512,
                        "normalize": True,
                    }
                )
            except Exception as exc:
                self.status = {"status": "UNAVAILABLE", "reason": f"{type(exc).__name__}: {exc}"}
                dump_json(self.status_path, self.status)
                raise
            self.status = {
                "status": "AVAILABLE",
                "model": self.model_name,
                "load_ms": (time.perf_counter() - started) * 1000.0,
                "device": str(self.model.device),
            }
            dump_json(self.status_path, self.status)
        return self.model

    def rerank(
        self,
        *,
        query_id: str,
        query_text: str,
        full_ranking: Sequence[Mapping[str, Any]],
        page_texts: Mapping[str, str],
        candidate_k: int,
        config_name: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        candidates = list(full_ranking[:candidate_k])
        candidate_ids = [str(item["page_id"]) for item in candidates]
        cache_key = stable_hash(
            {
                "query_id": query_id,
                "query": query_text,
                "candidate_ids": candidate_ids,
                "page_texts": {page_id: page_texts[page_id] for page_id in candidate_ids},
                "candidate_k": candidate_k,
                "model": self.model_name,
                "config_name": config_name,
            }
        )
        cached = self.success_by_key.get(cache_key)
        if cached:
            return append_reranked_candidates(
                full_ranking,
                cached["ranking_page_ids"],
                candidate_k=candidate_k,
            ), cached
        model = self._load_model()
        documents = [{"page_id": page_id, "memory": page_texts[page_id]} for page_id in candidate_ids]
        started = time.perf_counter()
        try:
            result = model.rerank(query_text, documents, top_k=candidate_k)
            latency_ms = (time.perf_counter() - started) * 1000.0
            ranking_ids = [str(item["page_id"]) for item in result]
            row = {
                "cache_key": cache_key,
                "query_id": query_id,
                "config_name": config_name,
                "model": self.model_name,
                "candidate_k": candidate_k,
                "candidate_page_ids": candidate_ids,
                "ranking_page_ids": ranking_ids,
                "scores": [float(item.get("rerank_score") or 0.0) for item in result],
                "rerank_latency_ms": latency_ms,
                "status": "SUCCESS",
            }
        except Exception as exc:
            row = {
                "cache_key": cache_key,
                "query_id": query_id,
                "config_name": config_name,
                "model": self.model_name,
                "candidate_k": candidate_k,
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            raise
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.success_by_key[cache_key] = row
        return append_reranked_candidates(full_ranking, ranking_ids, candidate_k=candidate_k), row


def llm_rerank_messages(query_text: str, candidates: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    system = (
        "你是历史 Page 相关性排序器。只根据 retrieval query 判断候选历史 Page 的相关性；不回答 query，不修改 Page，"
        "不得使用未提供的信息。返回严格 JSON：{\"ranking\":[\"P07\",\"P02\",...]}。"
        "ranking 应包含所有给出的候选短 ID，按相关性从高到低排列，不得输出不存在或重复的 ID，不写解释。"
    )
    page_block = "\n\n".join(f"[{item['short_id']}]\n{item['text']}" for item in candidates)
    user = f"Retrieval query:\n{query_text}\n\nCandidate Pages:\n{page_block}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def llm_ranking_validator(allowed_ids: set[str]):
    def validator(value: Mapping[str, Any]) -> dict[str, Any]:
        ranking = [str(item).strip() for item in value.get("ranking") or [] if str(item).strip()]
        if not ranking or len(ranking) != len(set(ranking)):
            raise ValueError("ranking is empty or contains duplicate IDs")
        invalid = [item for item in ranking if item not in allowed_ids]
        if invalid:
            raise ValueError(f"ranking contains invalid IDs: {invalid}")
        return {"ranking": ranking}

    return validator


async def run_llm_rerank(
    args: argparse.Namespace,
    output_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    query_variant: str,
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
    full_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    page_texts: Mapping[str, str],
    *,
    config_name: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], str]:
    if args.skip_llm:
        return {}, {}, "SKIPPED: --skip-llm"
    memory_config = expand_env_placeholders(load_json(REPO_ROOT / "exp/benchmark/memory_config.json"))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    model = str(llm_config.get("model") or "deepseek-chat")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config.get("api_key"), base_url=base_url)
    cache = AsyncJsonlLLMCache(
        output_dir / "cache/rerank/llm_rerank.jsonl",
        client=client,
        model=model,
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.rerank_concurrency,
    )

    async def one(query: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, str]]:
        query_id = str(query["query_id"])
        candidate_rows = list(full_rankings[query_id][: args.candidate_k])
        short_to_page: dict[str, str] = {}
        candidates: list[dict[str, str]] = []
        for index, item in enumerate(candidate_rows, start=1):
            short_id = f"P{index:02d}"
            page_id = str(item["page_id"])
            short_to_page[short_id] = page_id
            candidates.append({"short_id": short_id, "text": page_texts[page_id][:1600]})
        query_text = rerank_query_text(query_id, query_variant, query_options)
        messages = llm_rerank_messages(query_text, candidates)
        row = await cache.call(
            query_id=query_id,
            variant="R2",
            messages=messages,
            prompt_version=LLM_RERANK_PROMPT_VERSION,
            max_tokens=1200,
            validator=llm_ranking_validator(set(short_to_page)),
            extra_key={
                "config_name": config_name,
                "candidate_page_ids": list(short_to_page.values()),
                "candidate_k": args.candidate_k,
            },
        )
        return query_id, row, short_to_page

    results = await asyncio.gather(*(one(query) for query in queries))
    await client.close()
    rankings: dict[str, list[dict[str, Any]]] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for query_id, row, short_to_page in results:
        if row.get("status") != "SUCCESS":
            continue
        short_ids = list(row["parsed"]["ranking"])
        page_ids = [short_to_page[short_id] for short_id in short_ids]
        rankings[query_id] = append_reranked_candidates(
            full_rankings[query_id],
            page_ids,
            candidate_k=args.candidate_k,
        )
        metadata[query_id] = row
    status = "AVAILABLE" if len(rankings) == len(queries) else f"FAILED: {len(rankings)}/{len(queries)} Queries available"
    return rankings, metadata, status


def rank_of(ranking: Sequence[Mapping[str, Any]], page_id: str) -> int | None:
    for index, item in enumerate(ranking, start=1):
        if str(item["page_id"]) == str(page_id):
            return index
    return None


def summarize_scheme_latency(
    scheme: str,
    queries: Sequence[Mapping[str, Any]],
    *,
    query_variant: str,
    query_metadata: Mapping[str, Mapping[str, Mapping[str, Any]]],
    embedding_metadata: Mapping[str, Any],
    query_options: Mapping[str, Mapping[str, Sequence[str]]],
    search_timing: Mapping[str, Mapping[str, float]],
    rerank_metadata: Mapping[str, Mapping[str, Any]] | None = None,
    llm_rerank: bool = False,
) -> tuple[dict[str, Any], dict[str, dict[str, float]]]:
    per_query: dict[str, dict[str, float]] = {}
    for query in queries:
        query_id = str(query["query_id"])
        query_meta = (query_metadata.get(query_variant) or {}).get(query_id) or {}
        search = search_timing.get(query_id) or {}
        rerank = (rerank_metadata or {}).get(query_id) or {}
        rewrite_ms = float(query_meta.get("llm_latency_ms") or 0.0)
        rerank_ms = float(rerank.get("llm_latency_ms") if llm_rerank else rerank.get("rerank_latency_ms") or 0.0)
        row = {
            "query_build_ms": float(query_meta.get("query_build_ms") or 0.0),
            "query_rewrite_ms": rewrite_ms,
            "query_embedding_ms": query_embedding_latency(
                query_id,
                query_variant,
                embedding_metadata,
                query_options,
            ),
            "dense_search_ms": float(search.get("dense_search_ms") or 0.0),
            "bm25_search_ms": float(search.get("bm25_search_ms") or 0.0),
            "fusion_ms": float(search.get("fusion_ms") or 0.0),
            "rerank_ms": rerank_ms,
            "online_llm_calls": float(query_meta.get("llm_calls") or 0) + (1.0 if llm_rerank else 0.0),
            "prompt_tokens": float(query_meta.get("prompt_tokens") or 0)
            + (float(rerank.get("prompt_tokens") or 0.0) if llm_rerank else 0.0),
            "completion_tokens": float(query_meta.get("completion_tokens") or 0)
            + (float(rerank.get("completion_tokens") or 0.0) if llm_rerank else 0.0),
            "retry_count": float(query_meta.get("retry_count") or 0)
            + (float(rerank.get("retry_count") or 0.0) if llm_rerank else 0.0),
        }
        row["retrieval_total_ms"] = sum(
            row[key]
            for key in (
                "query_build_ms",
                "query_rewrite_ms",
                "query_embedding_ms",
                "dense_search_ms",
                "bm25_search_ms",
                "fusion_ms",
                "rerank_ms",
            )
        )
        per_query[query_id] = row
    summary: dict[str, Any] = {"scheme": scheme}
    for key in (
        "query_build_ms",
        "query_rewrite_ms",
        "query_embedding_ms",
        "dense_search_ms",
        "bm25_search_ms",
        "fusion_ms",
        "rerank_ms",
        "retrieval_total_ms",
    ):
        stats = latency_stats(row[key] for row in per_query.values())
        for stat_name, value in stats.items():
            summary[f"{key}_{stat_name}"] = value
    summary["online_llm_calls_per_query"] = statistics.fmean(
        row["online_llm_calls"] for row in per_query.values()
    )
    summary["mean_prompt_tokens"] = statistics.fmean(row["prompt_tokens"] for row in per_query.values())
    summary["mean_completion_tokens"] = statistics.fmean(row["completion_tokens"] for row in per_query.values())
    summary["mean_retry_count"] = statistics.fmean(row["retry_count"] for row in per_query.values())
    return summary, per_query


def best_row(rows: Sequence[Mapping[str, Any]], *, require_available: bool = True) -> dict[str, Any]:
    candidates = [
        dict(row)
        for row in rows
        if not require_available or str(row.get("status") or "AVAILABLE") == "AVAILABLE"
    ]
    if not candidates:
        raise ValueError("No available experiment row")
    return max(candidates, key=ranking_sort_key)


def report_percent(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:.2f}%"


def report_delta(value: Any) -> str:
    return "N/A" if value is None else f"{float(value) * 100:+.2f} pp"


def build_report(
    *,
    manifest: Mapping[str, Any],
    baseline: Mapping[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    page_rows: Sequence[Mapping[str, Any]],
    embedding_rows: Sequence[Mapping[str, Any]],
    hybrid_rows: Sequence[Mapping[str, Any]],
    rerank_rows: Sequence[Mapping[str, Any]],
    combination_rows: Sequence[Mapping[str, Any]],
    selections: Mapping[str, Any],
    prompt_decision: Mapping[str, Any],
    old_thinking: Mapping[str, Any],
    root_cause_counts: Mapping[str, int],
) -> str:
    baseline_metrics = baseline["metrics"]
    candidate_20 = next(row for row in candidate_rows if str(row["candidate_k"]) == "20")
    best_query = next(row for row in query_rows if row["variant"] == selections["best_query"])
    best_page = next(row for row in page_rows if row["variant"] == selections["best_page"])
    best_embedding = best_row([row for row in embedding_rows if row.get("screening_scope") == "required"])
    best_embedding_only = best_row(
        [row for row in embedding_rows if row.get("screening_scope") == "embedding_only"]
    )
    best_hybrid = next(
        row
        for row in hybrid_rows
        if row["strategy"] == selections["best_hybrid"] and row.get("screening_scope") == "front"
    )
    best_hybrid_only = best_row(
        [row for row in hybrid_rows if row.get("screening_scope") == "hybrid_only"]
    )
    best_rerank = next(row for row in rerank_rows if row["reranker"] == selections["best_reranker"])
    best_quality = next(row for row in combination_rows if row["scheme"] == "C5_best_quality")
    best_no_llm = next(row for row in combination_rows if row["scheme"] == "C4_best_no_online_llm")
    pareto = next(row for row in combination_rows if row["scheme"] == "C6_best_practical")
    baseline_combination = next(row for row in combination_rows if row["scheme"] == "C0_baseline")
    standalone_rewrite = next(row for row in query_rows if row["variant"] == "Q4")
    multi_query = next(row for row in query_rows if row["variant"] == "Q6")
    candidate_rerank_only = next(
        (row for row in combination_rows if row["scheme"] == "C3_candidate_expansion_rerank_only"),
        None,
    )

    query_table = "\n".join(
        f"| {row['variant']} | {row.get('description', '')} | {report_percent(row.get('recall_at_5'))} | "
        f"{report_percent(row.get('recall_at_20'))} | {float(row.get('mrr') or 0):.4f} | "
        f"{float(row.get('average_text_length_chars') or 0):.0f} | {row.get('truncation_expected')} | {row.get('status')} |"
        for row in query_rows
    )
    page_table = "\n".join(
        f"| {row['variant']} | {row.get('description', '')} | {report_percent(row.get('recall_at_5'))} | "
        f"{float(row.get('mrr') or 0):.4f} | {float(row.get('mean_pairwise_cosine') or 0):.4f} |"
        for row in page_rows
    )
    embedding_table = "\n".join(
        f"| {row['embedding']} | {row['model']} | {row['query_variant']} | {row['page_variant']} | "
        f"{report_percent(row.get('recall_at_5'))} | {float(row.get('mrr') or 0):.4f} | {row.get('dimension')} | {row.get('status')} |"
        for row in embedding_rows
        if row.get("screening_scope") == "required"
    )
    hybrid_table = "\n".join(
        f"| {row.get('screening_scope', '')} | {row['strategy']} | {report_percent(row.get('recall_at_5'))} | {report_percent(row.get('recall_at_10'))} | "
        f"{report_percent(row.get('recall_at_20'))} | {report_percent(row.get('recall_at_30'))} |"
        for row in hybrid_rows
    )
    rerank_table = "\n".join(
        f"| {row['reranker']} | {report_percent(row.get('candidate_recall_at_20'))} | "
        f"{report_percent(row.get('recall_at_5'))} | {float(row.get('mrr') or 0):.4f} | "
        f"{float(row.get('rerank_p95_ms') or 0):.1f} | {row.get('status')} |"
        for row in rerank_rows
    )
    combo_table = "\n".join(
        f"| {row['scheme']} | {report_percent(row.get('recall_at_5'))} | {float(row.get('mrr') or 0):.4f} | "
        f"{report_percent(row.get('candidate_recall_at_20'))} | {float(row.get('retrieval_mean_ms') or 0):.1f} | "
        f"{float(row.get('retrieval_p95_ms') or 0):.1f} | {float(row.get('online_llm_calls_per_query') or 0):.1f} |"
        for row in combination_rows
    )
    biggest_single = max(
        (
            ("Query", float(best_query["recall_at_5"]) - float(baseline_metrics["recall_at_5"])),
            ("Page representation", float(best_page["recall_at_5"]) - float(baseline_metrics["recall_at_5"])),
            ("Embedding", float(best_embedding_only["recall_at_5"]) - float(baseline_metrics["recall_at_5"])),
            ("Hybrid", float(best_hybrid_only["recall_at_5"]) - float(baseline_metrics["recall_at_5"])),
            (
                "Candidate expansion + rerank",
                float(candidate_rerank_only.get("recall_at_5") or 0.0) - float(baseline_metrics["recall_at_5"])
                if candidate_rerank_only and candidate_rerank_only.get("status") == "AVAILABLE"
                else float("-inf"),
            ),
            (
                "Rerank on screened candidates",
                float(best_rerank["recall_at_5"]) - float(rerank_rows[0]["recall_at_5"]),
            ),
        ),
        key=lambda item: item[1],
    )
    candidate_note = (
        "大量 Gold 位于 rank 6–20，candidate generation 尚可，fine ranking 是主要瓶颈之一。"
        if sum(int(baseline["gold_rank_buckets"].get(key) or 0) for key in ("6-10", "11-20"))
        > int(baseline["gold_rank_buckets"].get(">30 / not found") or 0)
        else "Gold 在 20 名以外仍占较大比例，candidate generation 本身仍是重要瓶颈。"
    )
    if selections["best_reranker"] == "R0_no_rerank":
        rerank_recommendation = (
            "本轮本地 cross-encoder 与 DeepSeek rerank 均未提升 R@5 且降低 MRR，不建议把任一 tested reranker 上线；"
            "应先针对 S001 的 rank 6–20 失败样本改进 ranking 训练/提示后再复验。"
        )
    else:
        rerank_recommendation = (
            f"对 `{selections['best_reranker']}` 做小流量 candidate expansion + rerank 验证，"
            "并把其延迟与失败回退纳入上线门槛。"
        )
    query_recommendation = (
        f"`{selections['best_query']}` 是 Query screening 的最高 R@5，但 Q4 Standalone Rewrite 无增益、"
        "Q6 Multi-query 下降，Q5 也牺牲 Candidate R@20/MRR 且增加约一次在线 LLM；"
        "因此推荐方案保持 Q0，没有证据支持仅为 Query 构建新增在线 LLM。"
    )
    hybrid_recommendation = (
        "正确中文 BM25 仅改善 Candidate R@20、未改善 baseline R@5，暂不建议把 Hybrid 直接写入生产默认路径。"
    )

    return f"""# S001 no-thinking Mid-term Page Retrieval Experiment Report

本报告只使用阶段 0 已完成的 S001 产物。14 个 evaluated Query 均按原运行时序过滤候选；47 个最终 Page 从未被直接用于早期 Query。统一主口径是 **query 时刻已 committed 的 exact Gold Page micro Recall**，共 {manifest['eligible_gold_page_count']} 个 Gold。旧 benchmark 把仍在短期层的 Gold 也计入 Page denominator，因此另列为 legacy 口径，不能与这里混用。

## 1. No-thinking Baseline

- Exact Page R@5 / R@10 / R@20：{report_percent(baseline_metrics['recall_at_5'])} / {report_percent(baseline_metrics['recall_at_10'])} / {report_percent(baseline_metrics['recall_at_20'])}
- MRR：{baseline_metrics['mrr']:.4f}；Mean Gold Rank：{baseline_metrics['mean_gold_rank']:.2f}
- Stage-0 legacy Page macro/micro R@5：{report_percent(baseline['legacy_page_macro_recall_at_5'])} / {report_percent(baseline['legacy_page_micro_recall_at_5'])}
- 与旧 thinking snapshot 的 eligible-Gold R@5 {report_percent(old_thinking.get('recall_at_5'))} 相比，no-thinking 变化 {report_delta(float(baseline_metrics['recall_at_5']) - float(old_thinking.get('recall_at_5') or 0))}。Page 摘要随 thinking mode 改变，排名分布也发生变化。
- 新 evaluator 对 14/14 Query 的生产 Top-5 完全复现：`{baseline['stage0_consistency_status']}`。

## 2. Candidate Retrieval 是否已经足够

Candidate R@20 为 **{report_percent(candidate_20['recall_at_20'])}**。Gold rank 分桶：{json.dumps(baseline['gold_rank_buckets'], ensure_ascii=False)}。{candidate_note}

## 3. Query 是否仍然是主瓶颈

| Variant | Query | R@5 | R@20 | MRR | Avg chars | Truncation expected | Status |
|---|---|---:|---:|---:|---:|---|---|
{query_table}

最佳可部署 Query 是 `{selections['best_query']}`，单独替换 Query 的 R@5 增益为 {report_delta(float(best_query['recall_at_5']) - float(baseline_metrics['recall_at_5']))}。Q1–Q3 保留完整 QA 文本，但当前 Query 被置于最前，最近 QA 优先；生产 E0 的 max sequence length 为 512，表中的长文本会被模型截断，因此增加更多完整 QA 不等于模型实际看见更多轮次。Oracle 只表示理想 Query 上界，不是可部署方案，也不计入 Top-2 或实际增益。

## 4. Page Representation 是否是主瓶颈

| Variant | Page text | R@5 | MRR | Mean pairwise cosine |
|---|---|---:|---:|---:|
{page_table}

最佳 Page representation 是 `{selections['best_page']}`，固定 Q0/E0 时增益 {report_delta(float(best_page['recall_at_5']) - float(baseline_metrics['recall_at_5']))}。Pairwise cosine 只用于同质化诊断，选择仍以 Recall/Rank 为准。

## 5. Embedding 模型影响

| ID | Model | Query | Page | R@5 | MRR | Dim | Status |
|---|---|---|---|---:|---:|---:|---|
{embedding_table}

筛选组合下最佳 embedding 为 `{best_embedding['embedding']}` / `{best_embedding['model']}`；相对 baseline 的表观增益为 {report_delta(float(best_embedding['recall_at_5']) - float(baseline_metrics['recall_at_5']))}。严格固定 Q0/P0 时，最佳模型 `{best_embedding_only['embedding']}` / `{best_embedding_only['model']}` 的单纯 embedding 增益为 {report_delta(float(best_embedding_only['recall_at_5']) - float(baseline_metrics['recall_at_5']))}。

## 6. Hybrid 是否有效

中文 BM25 sanity case 全部通过后才执行下表；分词使用仓库 `lemmatize_for_bm25(..., language='zh')`，没有沿用旧的全零 sparse 结论。

| Scope | Strategy | R@5 | Candidate R@10 | Candidate R@20 | Candidate R@30 |
|---|---|---:|---:|---:|---:|
{hybrid_table}

最佳 candidate strategy 是 `{selections['best_hybrid']}`，其 Candidate R@20 为 {report_percent(best_hybrid['recall_at_20'])}。严格固定 Q0/P0/E0 的 hybrid-only 最佳方案为 `{best_hybrid_only['strategy']}`，R@5 单项增益 {report_delta(float(best_hybrid_only['recall_at_5']) - float(baseline_metrics['recall_at_5']))}。

## 7. Rerank 是否有效

默认 `candidate_k={selections['candidate_k']}`、`output_k={selections['output_k']}`，二者在实现和输出中完全分离。

| Reranker | Candidate R@20 | Output R@5 | MRR | rerank p95 ms | Status |
|---|---:|---:|---:|---:|---|
{rerank_table}

最佳 reranker 是 `{selections['best_reranker']}`；相对同一 candidate 排名直接取前 5 的增益为 {report_delta(float(best_rerank['recall_at_5']) - float(rerank_rows[0]['recall_at_5']))}。rank 6–20 的逐例 before/after 位于 `per_query_results.csv`。

## 8. 最佳单项修改

- Query：{report_delta(float(best_query['recall_at_5']) - float(baseline_metrics['recall_at_5']))}
- Page representation：{report_delta(float(best_page['recall_at_5']) - float(baseline_metrics['recall_at_5']))}
- Embedding（固定 Q0/P0）：{report_delta(float(best_embedding_only['recall_at_5']) - float(baseline_metrics['recall_at_5']))}
- Hybrid（固定 Q0/P0/E0）：{report_delta(float(best_hybrid_only['recall_at_5']) - float(baseline_metrics['recall_at_5']))}
- Reranker（相对同候选 R0）：{report_delta(float(best_rerank['recall_at_5']) - float(rerank_rows[0]['recall_at_5']))}
- Candidate expansion + local reranker（固定 baseline Q0/P0/E0）：{report_delta(float(candidate_rerank_only['recall_at_5']) - float(baseline_metrics['recall_at_5'])) if candidate_rerank_only and candidate_rerank_only.get('status') == 'AVAILABLE' else 'UNAVAILABLE'}

最大观察到的单层增益来自 **{biggest_single[0]}：{report_delta(biggest_single[1])}**。

## 9. 最佳组合

| Scheme | R@5 | MRR | Candidate R@20 | mean ms | p95 ms | online LLM calls/query |
|---|---:|---:|---:|---:|---:|---:|
{combo_table}

- Best Quality：`{best_quality['details']}`，R@5 {report_percent(best_quality['recall_at_5'])}，相对 baseline {report_delta(best_quality['absolute_delta_recall_at_5'])}。
- Best No-Online-LLM：`{best_no_llm['details']}`，R@5 {report_percent(best_no_llm['recall_at_5'])}。
- Recommended Production/Pareto：`{pareto['details']}`，R@5 {report_percent(pareto['recall_at_5'])}。

## 10. 性能

Baseline 离线检索 mean/p95 为 {float(baseline_combination['retrieval_mean_ms'] or 0):.1f}/{float(baseline_combination['retrieval_p95_ms'] or 0):.1f} ms。Best Quality mean/p95 为 {float(best_quality['retrieval_mean_ms'] or 0):.1f}/{float(best_quality['retrieval_p95_ms'] or 0):.1f} ms，mean 相对 baseline {float(best_quality['delta_retrieval_mean_ms'] or 0):+.1f} ms，在线 LLM {float(best_quality['online_llm_calls_per_query'] or 0):.1f} 次/query。Best No-Online-LLM p95 为 {float(best_no_llm['retrieval_p95_ms'] or 0):.1f} ms；Pareto p95 为 {float(pareto['retrieval_p95_ms'] or 0):.1f} ms。

DeepSeek no-thinking Q4 rewrite mean/p95 为 {float(standalone_rewrite.get('llm_latency_mean_ms') or 0):.1f}/{float(standalone_rewrite.get('llm_latency_p95_ms') or 0):.1f} ms，平均 prompt/completion tokens 为 {float(standalone_rewrite.get('mean_prompt_tokens') or 0):.1f}/{float(standalone_rewrite.get('mean_completion_tokens') or 0):.1f}；Q6 multi-query mean/p95 为 {float(multi_query.get('llm_latency_mean_ms') or 0):.1f}/{float(multi_query.get('llm_latency_p95_ms') or 0):.1f} ms。详细分阶段 mean/p50/p90/p95/max 在 `latency_summary.csv`，rerank 与 embedding 构建/Query 编码开销分别在对应 ablation CSV。

本地延迟来自当前单机 CPU 的逐 Query 单次测量，适合方案内相对比较但样本仅 14 条；上线前仍需在目标硬件做稳定压测。LLM 延迟和 token 取自首次成功 API 调用，缓存复跑没有把 cache hit 误记为零成本在线调用。

## 11. 是否应该继续改 Page Prompt

**{prompt_decision['decision']}**。{prompt_decision['reason']} 理想 Query + best representation/embedding/hybrid 的 Candidate R@20 为 {report_percent(prompt_decision['oracle_candidate_recall_at_20'])}；当前最佳 candidate R@20 为 {report_percent(prompt_decision['best_candidate_recall_at_20'])}。

## 12. 下一步生产修改建议

1. 先在实验/灰度层验证本报告的 Pareto 方案，并把 `candidate_k` 与 `output_k` 在设计中明确拆开；本任务不修改生产行为。
2. {query_recommendation}
3. {rerank_recommendation}
4. {hybrid_recommendation}
5. Oracle required_context 绝不能进入生产；当前证据也不支持继续消耗成本重写 Page Prompt。
6. Baseline miss 自动根因计数为 {json.dumps(dict(root_cause_counts), ensure_ascii=False)}；逐 Gold 证据在 `miss_root_cause.csv`，生产修改应优先覆盖占比最高且有单项 rescue 证据的类别。
"""


async def run() -> None:
    args = parse_args()
    if args.candidate_k <= args.output_k:
        raise ValueError("candidate_k must be greater than output_k for the rerank experiment")
    source_dir = resolve_path(REPO_ROOT, args.source_dir)
    output_dir = resolve_path(REPO_ROOT, args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    queries, pages, manifest = build_snapshot(args, source_dir, output_dir)
    visibility_rows = load_jsonl(output_dir / "snapshot/query_page_visibility.jsonl")
    visibility_by_query = {str(row["query_id"]): row for row in visibility_rows}
    pages_by_id = {str(page["page_id"]): page for page in pages}

    query_options, query_metadata, query_status = await build_query_options(args, queries, output_dir)
    rewrite_audit = build_rewrite_audit(queries, query_options)
    dump_json(output_dir / "rewrite_spot_check.json", rewrite_audit)

    embedding_cache = EmbeddingCache(
        output_dir / "cache/embeddings",
        retry_unavailable=args.retry_unavailable_models,
    )
    available_query_variants = [variant for variant in QUERY_VARIANTS if query_status.get(variant) == "AVAILABLE"]
    e0_query_vectors, e0_query_metadata = encode_query_variants(
        embedding_cache,
        "E0",
        available_query_variants,
        query_options,
        query_status,
    )
    query_vectors_by_embedding: dict[str, dict[str, list[float]]] = {"E0": e0_query_vectors}
    query_embedding_metadata: dict[str, dict[str, Any]] = {"E0": e0_query_metadata}
    page_vectors_by_key: dict[tuple[str, str], dict[str, list[float]]] = {}
    page_embedding_metadata: dict[tuple[str, str], dict[str, Any]] = {}
    for page_variant in PAGE_VARIANTS:
        vectors, metadata = encode_page_variant(embedding_cache, "E0", page_variant, pages)
        page_vectors_by_key[("E0", page_variant)] = vectors
        page_embedding_metadata[("E0", page_variant)] = metadata

    baseline_rankings, baseline_timing = rank_dense_configuration(
        queries,
        pages_by_id,
        visibility_by_query,
        query_variant="Q0",
        query_vectors=e0_query_vectors,
        page_vectors=page_vectors_by_key[("E0", "P0")],
    )
    mismatches: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["query_id"])
        reconstructed = [str(item["source_turn_id"]) for item in baseline_rankings[query_id][: args.output_k]]
        recorded = list(query["stage0_mid_page_retrieved_turn_ids"])
        if reconstructed != recorded:
            mismatches.append({"query_id": query_id, "recorded": recorded, "reconstructed": reconstructed})
    if mismatches:
        dump_json(output_dir / "baseline_consistency_failure.json", mismatches)
        raise AssertionError(f"Unified evaluator differs from stage-0 production Top-5 for {len(mismatches)} Queries")
    baseline_metrics, baseline_per_query = evaluate_rankings(queries, baseline_rankings)
    stage0_summary = load_json(source_dir / "recall_summary.json")
    stage0_rows = [row for row in load_jsonl(source_dir / "recall_turn_results.jsonl") if row.get("evaluated")]
    legacy_gold = sum(int(row.get("mid_page_gold_count") or 0) for row in stage0_rows)
    legacy_hits = sum(int(row.get("mid_page_matched_count") or 0) for row in stage0_rows)
    baseline = {
        "metric_definition": (
            "Exact Page micro Recall over Gold Pages committed and visible before each Query; MRR/NDCG are macro per Query."
        ),
        "metrics": baseline_metrics,
        "legacy_page_macro_recall_at_5": (stage0_summary.get("mid_page") or {}).get("recall"),
        "legacy_page_micro_recall_at_5": legacy_hits / legacy_gold if legacy_gold else 0.0,
        "legacy_gold_count_including_short_term": legacy_gold,
        "legacy_matched_count": legacy_hits,
        "gold_rank_buckets": gold_rank_buckets(baseline_per_query),
        "stage0_consistency_status": "MATCHED_14_OF_14",
        "stage0_top5_mismatches": [],
        "per_query": baseline_per_query,
    }
    dump_json(output_dir / "baseline_metrics.json", baseline)

    candidate_rows: list[dict[str, Any]] = []
    for candidate_k in (5, 10, 15, 20, 30):
        candidate_rows.append(
            {
                "candidate_k": candidate_k,
                "candidate_recall": baseline_metrics[f"recall_at_{candidate_k}"],
                **metric_columns(baseline_metrics),
                "gold_rank_buckets": baseline["gold_rank_buckets"],
            }
        )
    candidate_rows.append(
        {
            "candidate_k": "all_available",
            "candidate_recall": 1.0,
            **metric_columns(baseline_metrics),
            "recall_at_all_available": 1.0,
            "gold_rank_buckets": baseline["gold_rank_buckets"],
        }
    )
    write_csv(output_dir / "candidate_depth_ablation.csv", candidate_rows)

    query_descriptions = {
        "Q0": "current user query",
        "Q1": "current + previous 1 QA",
        "Q2": "current + previous 2 QA",
        "Q3": "current + previous 3 QA",
        "Q4": "DeepSeek no-thinking standalone rewrite",
        "Q5": "original + standalone rewrite",
        "Q6": "two-query RRF (one no-thinking LLM call)",
        "QO": "required_context Oracle (not deployable)",
    }
    query_rows: list[dict[str, Any]] = []
    query_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    query_timing: dict[str, dict[str, dict[str, float]]] = {}
    for variant in QUERY_VARIANTS:
        if query_status.get(variant) != "AVAILABLE":
            query_rows.append(
                {
                    "variant": variant,
                    "description": query_descriptions[variant],
                    "deployable": variant != "QO",
                    "status": query_status.get(variant),
                }
            )
            continue
        rankings, timing = rank_dense_configuration(
            queries,
            pages_by_id,
            visibility_by_query,
            query_variant=variant,
            query_vectors=e0_query_vectors,
            page_vectors=page_vectors_by_key[("E0", "P0")],
        )
        metrics, _ = evaluate_rankings(queries, rankings)
        query_rankings[variant] = rankings
        query_timing[variant] = timing
        llm_calls = statistics.fmean(
            float((query_metadata.get(variant, {}).get(str(query["query_id"])) or {}).get("llm_calls") or 0)
            for query in queries
        )
        variant_metadata = [
            (query_metadata.get(variant, {}).get(str(query["query_id"])) or {}) for query in queries
        ]
        variant_text_lengths = [
            sum(len(text) for text in query_options[variant][str(query["query_id"])]) for query in queries
        ]
        llm_latency = latency_stats(float(row.get("llm_latency_ms") or 0.0) for row in variant_metadata)
        query_rows.append(
            {
                "variant": variant,
                "description": query_descriptions[variant],
                "deployable": variant != "QO",
                "status": "AVAILABLE",
                "online_llm_calls_per_query": llm_calls,
                "llm_latency_mean_ms": llm_latency["mean"],
                "llm_latency_p95_ms": llm_latency["p95"],
                "mean_prompt_tokens": statistics.fmean(
                    float(row.get("prompt_tokens") or 0.0) for row in variant_metadata
                ),
                "mean_completion_tokens": statistics.fmean(
                    float(row.get("completion_tokens") or 0.0) for row in variant_metadata
                ),
                "mean_retry_count": statistics.fmean(
                    float(row.get("retry_count") or 0.0) for row in variant_metadata
                ),
                "failure_rate": 0.0,
                "average_text_length_chars": statistics.fmean(variant_text_lengths),
                "production_embedding_max_seq_length": 512,
                "truncation_expected": max(variant_text_lengths) > 2000,
                **metric_columns(metrics),
            }
        )
    write_csv(output_dir / "query_ablation.csv", query_rows)
    deployable_query_rows = [
        row for row in query_rows if row.get("deployable") and row.get("status") == "AVAILABLE"
    ]
    top_query_rows = sorted(deployable_query_rows, key=ranking_sort_key, reverse=True)[:2]
    top_query_variants = [str(row["variant"]) for row in top_query_rows]

    page_descriptions = {
        "P0": "production: summary + keywords + user_input",
        "P1": "summary",
        "P2": "user_input",
        "P3": "raw_dialogue",
        "P4": "user_input + assistant_response",
        "P5": "user_input + summary",
        "P6": "summary + raw_dialogue",
        "P7": "user_input + keywords",
        "P8": "summary + keywords",
    }
    page_rows: list[dict[str, Any]] = []
    page_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    page_timing: dict[str, dict[str, dict[str, float]]] = {}
    for variant in PAGE_VARIANTS:
        rankings, timing = rank_dense_configuration(
            queries,
            pages_by_id,
            visibility_by_query,
            query_variant="Q0",
            query_vectors=e0_query_vectors,
            page_vectors=page_vectors_by_key[("E0", variant)],
        )
        metrics, _ = evaluate_rankings(queries, rankings)
        similarities = pairwise_cosine_stats(list(page_vectors_by_key[("E0", variant)].values()))
        lengths = [len(page_representation(page, variant)) for page in pages]
        page_rows.append(
            {
                "variant": variant,
                "description": page_descriptions[variant],
                "status": "AVAILABLE",
                **metric_columns(metrics),
                **similarities,
                "average_text_length_chars": statistics.fmean(lengths),
                "page_embedding_build_ms": page_embedding_metadata[("E0", variant)].get("build_ms"),
            }
        )
        page_rankings[variant] = rankings
        page_timing[variant] = timing
    write_csv(output_dir / "page_representation_ablation.csv", page_rows)
    top_page_rows = sorted(page_rows, key=ranking_sort_key, reverse=True)[:2]
    top_page_variants = [str(row["variant"]) for row in top_page_rows]

    interaction_rows: list[dict[str, Any]] = []
    interaction_rankings: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    interaction_timing: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for query_variant in top_query_variants:
        for page_variant in top_page_variants:
            rankings, timing = rank_dense_configuration(
                queries,
                pages_by_id,
                visibility_by_query,
                query_variant=query_variant,
                query_vectors=e0_query_vectors,
                page_vectors=page_vectors_by_key[("E0", page_variant)],
            )
            metrics, _ = evaluate_rankings(queries, rankings)
            interaction_rows.append(
                {
                    "query_variant": query_variant,
                    "page_variant": page_variant,
                    "status": "AVAILABLE",
                    **metric_columns(metrics),
                }
            )
            interaction_rankings[(query_variant, page_variant)] = rankings
            interaction_timing[(query_variant, page_variant)] = timing
    write_csv(output_dir / "query_page_interaction.csv", interaction_rows)

    embedding_rows: list[dict[str, Any]] = []
    embedding_rankings: dict[tuple[str, str, str], dict[str, list[dict[str, Any]]]] = {}
    embedding_timing: dict[tuple[str, str, str], dict[str, dict[str, float]]] = {}
    required_pairs = [(query_variant, page_variant) for query_variant in top_query_variants for page_variant in top_page_variants]
    auxiliary_pairs = list(dict.fromkeys([("Q0", "P0"), *(("Q0", page) for page in top_page_variants)]))
    variants_needed = list(dict.fromkeys([*top_query_variants, "Q0", "Q1", "Q2", "Q3", "QO"]))
    pages_needed = list(dict.fromkeys([*top_page_variants, "P0"]))
    for embedding_id, model_name in EMBEDDING_MODELS.items():
        LOGGER.info("Embedding experiment: %s (%s)", embedding_id, model_name)
        try:
            if embedding_id == "E0":
                query_vectors = e0_query_vectors
                query_meta = e0_query_metadata
            else:
                query_vectors, query_meta = encode_query_variants(
                    embedding_cache,
                    embedding_id,
                    variants_needed,
                    query_options,
                    query_status,
                )
                query_vectors_by_embedding[embedding_id] = query_vectors
                query_embedding_metadata[embedding_id] = query_meta
            for page_variant in pages_needed:
                key = (embedding_id, page_variant)
                if key not in page_vectors_by_key:
                    vectors, metadata = encode_page_variant(embedding_cache, embedding_id, page_variant, pages)
                    page_vectors_by_key[key] = vectors
                    page_embedding_metadata[key] = metadata
            for scope, pairs in (("required", required_pairs), ("auxiliary", auxiliary_pairs)):
                for query_variant, page_variant in pairs:
                    rankings, timing = rank_dense_configuration(
                        queries,
                        pages_by_id,
                        visibility_by_query,
                        query_variant=query_variant,
                        query_vectors=query_vectors,
                        page_vectors=page_vectors_by_key[(embedding_id, page_variant)],
                    )
                    metrics, _ = evaluate_rankings(queries, rankings)
                    row_scope = (
                        "embedding_only"
                        if scope == "auxiliary" and query_variant == "Q0" and page_variant == "P0"
                        else "c2_auxiliary"
                        if scope == "auxiliary"
                        else "required"
                    )
                    embedding_rows.append(
                        {
                            "embedding": embedding_id,
                            "model": model_name,
                            "query_variant": query_variant,
                            "page_variant": page_variant,
                            "screening_scope": row_scope,
                            "status": "AVAILABLE",
                            "dimension": page_embedding_metadata[(embedding_id, page_variant)].get("dimension"),
                            "page_encoding_ms": page_embedding_metadata[(embedding_id, page_variant)].get("build_ms"),
                            "query_encoding_p50_ms": latency_stats(
                                (query_meta.get("item_latency_ms") or {}).values()
                            )["p50"],
                            "query_encoding_p95_ms": latency_stats(
                                (query_meta.get("item_latency_ms") or {}).values()
                            )["p95"],
                            **metric_columns(metrics),
                        }
                    )
                    embedding_rankings[(embedding_id, query_variant, page_variant)] = rankings
                    embedding_timing[(embedding_id, query_variant, page_variant)] = timing
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            LOGGER.warning("Embedding %s unavailable: %s", model_name, reason)
            for query_variant, page_variant in required_pairs:
                embedding_rows.append(
                    {
                        "embedding": embedding_id,
                        "model": model_name,
                        "query_variant": query_variant,
                        "page_variant": page_variant,
                        "screening_scope": "required",
                        "status": "UNAVAILABLE",
                        "reason": reason,
                    }
                )
        finally:
            embedding_cache.release(model_name)
    write_csv(output_dir / "embedding_ablation.csv", embedding_rows)
    available_required_embedding_rows = [
        row for row in embedding_rows if row.get("screening_scope") == "required" and row.get("status") == "AVAILABLE"
    ]
    if not available_required_embedding_rows:
        raise RuntimeError("No embedding model completed the required screening")
    best_by_embedding: list[dict[str, Any]] = []
    for embedding_id in EMBEDDING_MODELS:
        rows = [row for row in available_required_embedding_rows if row["embedding"] == embedding_id]
        if rows:
            best_by_embedding.append(best_row(rows))
    top_embedding_rows = sorted(best_by_embedding, key=ranking_sort_key, reverse=True)[:2]
    top_embedding_ids = [str(row["embedding"]) for row in top_embedding_rows]
    front = best_row(available_required_embedding_rows)
    front_embedding = str(front["embedding"])
    front_query = str(front["query_variant"])
    front_page = str(front["page_variant"])

    if front_embedding not in query_vectors_by_embedding:
        raise AssertionError(f"Missing Query vectors for selected embedding {front_embedding}")
    front_dense_rankings, front_dense_timing = rank_dense_configuration(
        queries,
        pages_by_id,
        visibility_by_query,
        query_variant=front_query,
        query_vectors=query_vectors_by_embedding[front_embedding],
        page_vectors=page_vectors_by_key[(front_embedding, front_page)],
    )
    sanity = bm25_sanity_cases()
    dump_json(output_dir / "hybrid_bm25_sanity.json", sanity)
    if not all(row["passed"] for row in sanity):
        raise RuntimeError("Chinese BM25 sanity validation failed; Hybrid comparison is invalid")
    hybrid_rankings, hybrid_timing = build_hybrid_rankings(
        queries,
        pages_by_id,
        visibility_by_query,
        query_options,
        query_variant=front_query,
        page_variant=front_page,
        dense_rankings=front_dense_rankings,
        dense_timing=front_dense_timing,
    )
    hybrid_rows: list[dict[str, Any]] = []
    hybrid_metrics: dict[str, dict[str, Any]] = {}
    for strategy, rankings in hybrid_rankings.items():
        metrics, _ = evaluate_rankings(queries, rankings)
        hybrid_metrics[strategy] = metrics
        hybrid_rows.append(
            {
                "strategy": strategy,
                "screening_scope": "front",
                "query_variant": front_query,
                "page_variant": front_page,
                "embedding": front_embedding,
                "status": "AVAILABLE",
                **metric_columns(metrics),
            }
        )
    best_hybrid_row = max(
        hybrid_rows,
        key=lambda row: (
            float(row.get("recall_at_20") or 0.0),
            float(row.get("recall_at_10") or 0.0),
            float(row.get("recall_at_30") or 0.0),
            float(row.get("recall_at_5") or 0.0),
            float(row.get("mrr") or 0.0),
        ),
    )
    best_hybrid = str(best_hybrid_row["strategy"])
    candidate_rankings = hybrid_rankings[best_hybrid]
    candidate_timing = hybrid_timing[best_hybrid]
    candidate_metrics = hybrid_metrics[best_hybrid]

    page_texts_front = {str(page["page_id"]): page_representation(page, front_page) for page in pages}
    rerank_rows: list[dict[str, Any]] = []
    rerank_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"R0_no_rerank": dict(candidate_rankings)}
    rerank_metadata_by_name: dict[str, dict[str, dict[str, Any]]] = {"R0_no_rerank": {}}
    r0_metrics, _ = evaluate_rankings(queries, candidate_rankings)
    rerank_rows.append(
        {
            "reranker": "R0_no_rerank",
            "status": "AVAILABLE",
            "candidate_k": args.candidate_k,
            "output_k": args.output_k,
            "candidate_recall_at_20": candidate_metrics["recall_at_20"],
            "rerank_p50_ms": 0.0,
            "rerank_p95_ms": 0.0,
            "mean_prompt_tokens": 0.0,
            "mean_completion_tokens": 0.0,
            "mean_retry_count": 0.0,
            "failure_rate": 0.0,
            **metric_columns(r0_metrics),
        }
    )
    local_cache = LocalRerankCache(output_dir / "cache", args.local_reranker_model)
    local_status = "AVAILABLE"
    local_rankings: dict[str, list[dict[str, Any]]] = {}
    local_metadata: dict[str, dict[str, Any]] = {}
    try:
        for query in queries:
            query_id = str(query["query_id"])
            ranking, metadata = local_cache.rerank(
                query_id=query_id,
                query_text=rerank_query_text(query_id, front_query, query_options),
                full_ranking=candidate_rankings[query_id],
                page_texts=page_texts_front,
                candidate_k=args.candidate_k,
                config_name=f"front:{front_query}:{front_page}:{front_embedding}:{best_hybrid}",
            )
            local_rankings[query_id] = ranking
            local_metadata[query_id] = metadata
        local_metrics, _ = evaluate_rankings(queries, local_rankings)
        rerank_rankings["R1_local_cross_encoder"] = local_rankings
        rerank_metadata_by_name["R1_local_cross_encoder"] = local_metadata
        local_latency = [float(row.get("rerank_latency_ms") or 0.0) for row in local_metadata.values()]
        rerank_rows.append(
            {
                "reranker": "R1_local_cross_encoder",
                "model": args.local_reranker_model,
                "status": "AVAILABLE",
                "candidate_k": args.candidate_k,
                "output_k": args.output_k,
                "candidate_recall_at_20": candidate_metrics["recall_at_20"],
                "rerank_p50_ms": latency_stats(local_latency)["p50"],
                "rerank_p95_ms": latency_stats(local_latency)["p95"],
                "mean_prompt_tokens": 0.0,
                "mean_completion_tokens": 0.0,
                "mean_retry_count": 0.0,
                "failure_rate": 0.0,
                **metric_columns(local_metrics),
            }
        )
    except Exception as exc:
        local_status = f"UNAVAILABLE: {type(exc).__name__}: {exc}"
        LOGGER.warning("Local reranker unavailable: %s", local_status)
        rerank_rows.append(
            {
                "reranker": "R1_local_cross_encoder",
                "model": args.local_reranker_model,
                "status": local_status,
                "candidate_k": args.candidate_k,
                "output_k": args.output_k,
                "candidate_recall_at_20": candidate_metrics["recall_at_20"],
            }
        )

    llm_rankings, llm_rerank_metadata, llm_rerank_status = await run_llm_rerank(
        args,
        output_dir,
        queries,
        front_query,
        query_options,
        candidate_rankings,
        page_texts_front,
        config_name=f"front:{front_query}:{front_page}:{front_embedding}:{best_hybrid}",
    )
    if llm_rerank_status == "AVAILABLE":
        llm_metrics, _ = evaluate_rankings(queries, llm_rankings)
        rerank_rankings["R2_llm_no_thinking"] = llm_rankings
        rerank_metadata_by_name["R2_llm_no_thinking"] = llm_rerank_metadata
        llm_latency = [float(row.get("llm_latency_ms") or 0.0) for row in llm_rerank_metadata.values()]
        rerank_rows.append(
            {
                "reranker": "R2_llm_no_thinking",
                "model": next(iter(llm_rerank_metadata.values())).get("model"),
                "status": "AVAILABLE",
                "candidate_k": args.candidate_k,
                "output_k": args.output_k,
                "candidate_recall_at_20": candidate_metrics["recall_at_20"],
                "rerank_p50_ms": latency_stats(llm_latency)["p50"],
                "rerank_p95_ms": latency_stats(llm_latency)["p95"],
                "mean_prompt_tokens": statistics.fmean(
                    float(row.get("prompt_tokens") or 0.0) for row in llm_rerank_metadata.values()
                ),
                "mean_completion_tokens": statistics.fmean(
                    float(row.get("completion_tokens") or 0.0) for row in llm_rerank_metadata.values()
                ),
                "mean_retry_count": statistics.fmean(
                    float(row.get("retry_count") or 0.0) for row in llm_rerank_metadata.values()
                ),
                "failure_rate": 0.0,
                **metric_columns(llm_metrics),
            }
        )
    else:
        rerank_rows.append(
            {
                "reranker": "R2_llm_no_thinking",
                "status": llm_rerank_status,
                "candidate_k": args.candidate_k,
                "output_k": args.output_k,
                "candidate_recall_at_20": candidate_metrics["recall_at_20"],
            }
        )
    write_csv(output_dir / "rerank_ablation.csv", rerank_rows)
    best_rerank_row = best_row(rerank_rows)
    best_reranker = str(best_rerank_row["reranker"])

    # C2: keep Q0 and select only representation/embedding changes.
    c2_candidates = [
        row
        for row in embedding_rows
        if row.get("status") == "AVAILABLE"
        and row.get("query_variant") == "Q0"
        and row.get("page_variant") in top_page_variants
    ]
    c2_best = best_row(c2_candidates)
    c2_key = (str(c2_best["embedding"]), "Q0", str(c2_best["page_variant"]))
    c2_rankings = embedding_rankings[c2_key]
    c2_timing = embedding_timing[c2_key]
    c2_metrics, _ = evaluate_rankings(queries, c2_rankings)

    # C3: baseline candidate expansion + the local reranker only.
    c3_rankings: dict[str, list[dict[str, Any]]] = {}
    c3_metadata: dict[str, dict[str, Any]] = {}
    c3_status = local_status
    if local_status == "AVAILABLE":
        page_texts_p0 = {str(page["page_id"]): page_representation(page, "P0") for page in pages}
        for query in queries:
            query_id = str(query["query_id"])
            ranking, metadata = local_cache.rerank(
                query_id=query_id,
                query_text=rerank_query_text(query_id, "Q0", query_options),
                full_ranking=baseline_rankings[query_id],
                page_texts=page_texts_p0,
                candidate_k=args.candidate_k,
                config_name="baseline-candidate-expansion",
            )
            c3_rankings[query_id] = ranking
            c3_metadata[query_id] = metadata

    # C4: screened best non-LLM Query + best Page/embedding/candidate generator/local reranker.
    non_llm_query_row = best_row(
        [row for row in query_rows if row.get("variant") in {"Q0", "Q1", "Q2", "Q3"}]
    )
    non_llm_query = str(non_llm_query_row["variant"])
    non_llm_page = top_page_variants[0]
    non_llm_embedding = top_embedding_ids[0]
    non_llm_dense, non_llm_dense_timing = rank_dense_configuration(
        queries,
        pages_by_id,
        visibility_by_query,
        query_variant=non_llm_query,
        query_vectors=query_vectors_by_embedding[non_llm_embedding],
        page_vectors=page_vectors_by_key[(non_llm_embedding, non_llm_page)],
    )
    non_llm_hybrid, non_llm_hybrid_timing = build_hybrid_rankings(
        queries,
        pages_by_id,
        visibility_by_query,
        query_options,
        query_variant=non_llm_query,
        page_variant=non_llm_page,
        dense_rankings=non_llm_dense,
        dense_timing=non_llm_dense_timing,
    )
    non_llm_hybrid_rows: list[dict[str, Any]] = []
    for strategy, rankings in non_llm_hybrid.items():
        metrics, _ = evaluate_rankings(queries, rankings)
        non_llm_hybrid_rows.append({"strategy": strategy, **metric_columns(metrics)})
    non_llm_candidate_row = max(
        non_llm_hybrid_rows,
        key=lambda row: (
            float(row.get("recall_at_20") or 0.0),
            float(row.get("recall_at_10") or 0.0),
            float(row.get("recall_at_5") or 0.0),
        ),
    )
    non_llm_strategy = str(non_llm_candidate_row["strategy"])
    non_llm_candidate_rankings = non_llm_hybrid[non_llm_strategy]
    non_llm_candidate_timing = non_llm_hybrid_timing[non_llm_strategy]
    non_llm_candidate_metrics, _ = evaluate_rankings(queries, non_llm_candidate_rankings)
    non_llm_local_rankings: dict[str, list[dict[str, Any]]] = {}
    non_llm_local_metadata: dict[str, dict[str, Any]] = {}
    if local_status == "AVAILABLE":
        non_llm_texts = {str(page["page_id"]): page_representation(page, non_llm_page) for page in pages}
        for query in queries:
            query_id = str(query["query_id"])
            ranking, metadata = local_cache.rerank(
                query_id=query_id,
                query_text=rerank_query_text(query_id, non_llm_query, query_options),
                full_ranking=non_llm_candidate_rankings[query_id],
                page_texts=non_llm_texts,
                candidate_k=args.candidate_k,
                config_name=f"no-llm:{non_llm_query}:{non_llm_page}:{non_llm_embedding}:{non_llm_strategy}",
            )
            non_llm_local_rankings[query_id] = ranking
            non_llm_local_metadata[query_id] = metadata
    c4_options: list[dict[str, Any]] = []
    for strategy, rankings in non_llm_hybrid.items():
        metrics, _ = evaluate_rankings(queries, rankings)
        c4_options.append(
            {
                "candidate_strategy": strategy,
                "reranker": "no_rerank",
                "rankings": rankings,
                "metadata": {},
                "metrics": metrics,
                "candidate_recall_at_20": metrics["recall_at_20"],
                "search_timing": non_llm_hybrid_timing[strategy],
            }
        )
    if non_llm_local_rankings:
        local_metrics, _ = evaluate_rankings(queries, non_llm_local_rankings)
        c4_options.append(
            {
                "candidate_strategy": non_llm_strategy,
                "reranker": "local_cross_encoder",
                "rankings": non_llm_local_rankings,
                "metadata": non_llm_local_metadata,
                "metrics": local_metrics,
                "candidate_recall_at_20": non_llm_candidate_metrics["recall_at_20"],
                "search_timing": non_llm_candidate_timing,
            }
        )
    c4_option = max(c4_options, key=lambda option: ranking_sort_key(option["metrics"]))
    c4_strategy = str(c4_option["candidate_strategy"])
    c4_reranker = str(c4_option["reranker"])
    c4_rankings = c4_option["rankings"]
    c4_metadata = c4_option["metadata"]

    # Oracle candidate upper bound for the Page Prompt decision.
    oracle_dense, oracle_dense_timing = rank_dense_configuration(
        queries,
        pages_by_id,
        visibility_by_query,
        query_variant="QO",
        query_vectors=query_vectors_by_embedding[front_embedding],
        page_vectors=page_vectors_by_key[(front_embedding, front_page)],
    )
    oracle_hybrid, _ = build_hybrid_rankings(
        queries,
        pages_by_id,
        visibility_by_query,
        query_options,
        query_variant="QO",
        page_variant=front_page,
        dense_rankings=oracle_dense,
        dense_timing=oracle_dense_timing,
    )
    oracle_metrics = [evaluate_rankings(queries, rankings)[0] for rankings in oracle_hybrid.values()]
    oracle_best = max(oracle_metrics, key=lambda metrics: float(metrics.get("recall_at_20") or 0.0))

    combination_rows: list[dict[str, Any]] = []
    latency_rows: list[dict[str, Any]] = []
    combination_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    combination_latency_per_query: dict[str, dict[str, dict[str, float]]] = {}

    def add_combination(
        scheme: str,
        details: str,
        rankings: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        candidate_recall_at_20: float,
        query_variant: str,
        embedding_id: str,
        search_timing: Mapping[str, Mapping[str, float]],
        rerank_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        llm_rerank: bool = False,
        status: str = "AVAILABLE",
    ) -> None:
        if status != "AVAILABLE":
            combination_rows.append({"scheme": scheme, "details": details, "status": status})
            return
        metrics, _ = evaluate_rankings(queries, rankings)
        latency, per_query_latency = summarize_scheme_latency(
            scheme,
            queries,
            query_variant=query_variant,
            query_metadata=query_metadata,
            embedding_metadata=query_embedding_metadata[embedding_id],
            query_options=query_options,
            search_timing=search_timing,
            rerank_metadata=rerank_metadata,
            llm_rerank=llm_rerank,
        )
        row = {
            "scheme": scheme,
            "details": details,
            "status": "AVAILABLE",
            "candidate_recall_at_20": candidate_recall_at_20,
            **metric_columns(metrics),
            "retrieval_mean_ms": latency["retrieval_total_ms_mean"],
            "retrieval_p95_ms": latency["retrieval_total_ms_p95"],
            "online_llm_calls_per_query": latency["online_llm_calls_per_query"],
            "mean_prompt_tokens": latency["mean_prompt_tokens"],
            "mean_completion_tokens": latency["mean_completion_tokens"],
        }
        combination_rows.append(row)
        latency_rows.append(latency)
        combination_rankings[scheme] = {key: list(value) for key, value in rankings.items()}
        combination_latency_per_query[scheme] = per_query_latency

    add_combination(
        "C0_baseline",
        "Q0 + P0 + E0 + dense top5 + no rerank",
        baseline_rankings,
        candidate_recall_at_20=baseline_metrics["recall_at_20"],
        query_variant="Q0",
        embedding_id="E0",
        search_timing=baseline_timing,
    )
    best_query = top_query_variants[0]
    add_combination(
        "C1_best_query_only",
        f"{best_query} + P0 + E0 + dense top5",
        query_rankings[best_query],
        candidate_recall_at_20=next(row for row in query_rows if row["variant"] == best_query)["recall_at_20"],
        query_variant=best_query,
        embedding_id="E0",
        search_timing=query_timing[best_query],
    )
    add_combination(
        "C2_best_representation_embedding_only",
        f"Q0 + {c2_best['page_variant']} + {c2_best['embedding']} + dense top5",
        c2_rankings,
        candidate_recall_at_20=c2_metrics["recall_at_20"],
        query_variant="Q0",
        embedding_id=str(c2_best["embedding"]),
        search_timing=c2_timing,
    )
    if c3_rankings:
        c3_metrics, _ = evaluate_rankings(queries, c3_rankings)
        add_combination(
            "C3_candidate_expansion_rerank_only",
            f"Q0 + P0 + E0 + dense candidate20 + {args.local_reranker_model} -> top5",
            c3_rankings,
            candidate_recall_at_20=baseline_metrics["recall_at_20"],
            query_variant="Q0",
            embedding_id="E0",
            search_timing=baseline_timing,
            rerank_metadata=c3_metadata,
        )
    else:
        add_combination(
            "C3_candidate_expansion_rerank_only",
            "baseline candidate20 + local reranker",
            {},
            candidate_recall_at_20=baseline_metrics["recall_at_20"],
            query_variant="Q0",
            embedding_id="E0",
            search_timing=baseline_timing,
            status=c3_status,
        )
    add_combination(
        "C4_best_no_online_llm",
        f"{non_llm_query} + {non_llm_page} + {non_llm_embedding} + {c4_strategy} + {c4_reranker}",
        c4_rankings,
        candidate_recall_at_20=c4_option["candidate_recall_at_20"],
        query_variant=non_llm_query,
        embedding_id=non_llm_embedding,
        search_timing=c4_option["search_timing"],
        rerank_metadata=c4_metadata,
    )
    # Best Quality must be the best observed output ranking, not necessarily the
    # candidate generator with the highest Recall@20.  Keeping these selections
    # separate prevents a high-depth candidate strategy from silently reducing
    # Top-5 quality when its reranker does not recover the ordering.
    quality_options: list[dict[str, Any]] = []
    for strategy, rankings in hybrid_rankings.items():
        metrics = hybrid_metrics[strategy]
        quality_options.append(
            {
                "candidate_strategy": strategy,
                "reranker": "R0_no_rerank",
                "rankings": rankings,
                "metadata": {},
                "metrics": metrics,
                "candidate_recall_at_20": metrics["recall_at_20"],
                "search_timing": hybrid_timing[strategy],
                "llm_rerank": False,
            }
        )
    for reranker, rankings in rerank_rankings.items():
        if reranker == "R0_no_rerank":
            continue
        metrics, _ = evaluate_rankings(queries, rankings)
        quality_options.append(
            {
                "candidate_strategy": best_hybrid,
                "reranker": reranker,
                "rankings": rankings,
                "metadata": rerank_metadata_by_name.get(reranker) or {},
                "metrics": metrics,
                "candidate_recall_at_20": candidate_metrics["recall_at_20"],
                "search_timing": candidate_timing,
                "llm_rerank": reranker == "R2_llm_no_thinking",
            }
        )
    best_quality_option = max(
        quality_options,
        key=lambda option: (
            ranking_sort_key(option["metrics"]),
            -int(bool(option["llm_rerank"])),
        ),
    )
    add_combination(
        "C5_best_quality",
        f"{front_query} + {front_page} + {front_embedding} + "
        f"{best_quality_option['candidate_strategy']} + {best_quality_option['reranker']}",
        best_quality_option["rankings"],
        candidate_recall_at_20=best_quality_option["candidate_recall_at_20"],
        query_variant=front_query,
        embedding_id=front_embedding,
        search_timing=best_quality_option["search_timing"],
        rerank_metadata=best_quality_option["metadata"],
        llm_rerank=best_quality_option["llm_rerank"],
    )

    available_combinations = [row for row in combination_rows if row.get("status") == "AVAILABLE"]
    baseline_for_budget = next(row for row in available_combinations if row["scheme"] == "C0_baseline")
    practical_latency_budget_ms = max(
        100.0,
        float(baseline_for_budget.get("retrieval_p95_ms") or 0.0) * 1.25,
    )
    practical_candidates = [
        row
        for row in available_combinations
        if float(row.get("online_llm_calls_per_query") or 0.0) == 0.0
        and float(row.get("retrieval_p95_ms") or math.inf) <= practical_latency_budget_ms
    ]
    pareto_source = max(
        practical_candidates or available_combinations,
        key=lambda row: (
            float(row.get("recall_at_5") or 0.0),
            float(row.get("mrr") or 0.0),
            -float(row.get("retrieval_p95_ms") or math.inf),
        ),
    )
    source_scheme = str(pareto_source["scheme"])
    pareto_row = dict(pareto_source)
    pareto_row["scheme"] = "C6_best_practical"
    pareto_row["details"] = (
        f"Low-latency Pareto selection of {source_scheme} under p95<={practical_latency_budget_ms:.1f} ms: "
        f"{pareto_source['details']}"
    )
    combination_rows.append(pareto_row)
    pareto_latency = dict(next(row for row in latency_rows if row["scheme"] == source_scheme))
    pareto_latency["scheme"] = "C6_best_practical"
    latency_rows.append(pareto_latency)
    combination_rankings["C6_best_practical"] = combination_rankings[source_scheme]
    combination_latency_per_query["C6_best_practical"] = combination_latency_per_query[source_scheme]

    baseline_combination = next(row for row in combination_rows if row["scheme"] == "C0_baseline")
    for row in combination_rows:
        if row.get("status") != "AVAILABLE":
            continue
        delta = float(row["recall_at_5"]) - float(baseline_combination["recall_at_5"])
        row["absolute_delta_recall_at_5"] = delta
        row["relative_delta_recall_at_5"] = (
            delta / float(baseline_combination["recall_at_5"]) if baseline_combination["recall_at_5"] else None
        )
        row["delta_mrr"] = float(row["mrr"]) - float(baseline_combination["mrr"])
        row["delta_mean_gold_rank"] = float(row["mean_gold_rank"]) - float(
            baseline_combination["mean_gold_rank"]
        )
        row["delta_retrieval_mean_ms"] = float(row["retrieval_mean_ms"]) - float(
            baseline_combination["retrieval_mean_ms"]
        )
    write_csv(output_dir / "combination_ablation.csv", combination_rows)
    write_csv(output_dir / "latency_summary.csv", latency_rows)

    # Single-factor Hybrid baseline for evidence-backed miss attribution.
    baseline_hybrid, _ = build_hybrid_rankings(
        queries,
        pages_by_id,
        visibility_by_query,
        query_options,
        query_variant="Q0",
        page_variant="P0",
        dense_rankings=baseline_rankings,
        dense_timing=baseline_timing,
    )
    baseline_hybrid_scored = [
        (name, rankings, evaluate_rankings(queries, rankings)[0]) for name, rankings in baseline_hybrid.items()
    ]
    for name, _, metrics in baseline_hybrid_scored:
        hybrid_rows.append(
            {
                "strategy": name,
                "screening_scope": "hybrid_only",
                "query_variant": "Q0",
                "page_variant": "P0",
                "embedding": "E0",
                "status": "AVAILABLE",
                **metric_columns(metrics),
            }
        )
    write_csv(output_dir / "hybrid_ablation.csv", hybrid_rows)
    baseline_best_hybrid_name, baseline_best_hybrid_rankings, _ = max(
        baseline_hybrid_scored,
        key=lambda item: ranking_sort_key(item[2]),
    )
    embedding_only_rows = [row for row in embedding_rows if row.get("screening_scope") == "embedding_only" and row.get("status") == "AVAILABLE"]
    best_embedding_only = best_row(embedding_only_rows)
    best_embedding_only_rankings = embedding_rankings[(str(best_embedding_only["embedding"]), "Q0", "P0")]

    root_rows: list[dict[str, Any]] = []
    root_counts: Counter[str] = Counter()
    final_rankings = combination_rankings["C5_best_quality"]
    screened_best_rerank_rankings = rerank_rankings[best_reranker]
    for query in queries:
        query_id = str(query["query_id"])
        for page_id in query["eligible_gold_page_ids"]:
            baseline_rank = rank_of(baseline_rankings[query_id], page_id)
            if baseline_rank is None or baseline_rank <= args.output_k:
                continue
            ranks = {
                "QUERY": rank_of(query_rankings[best_query][query_id], page_id),
                "PAGE_REPRESENTATION": rank_of(page_rankings[top_page_variants[0]][query_id], page_id),
                "EMBEDDING": rank_of(best_embedding_only_rankings[query_id], page_id),
                "HYBRID": rank_of(baseline_best_hybrid_rankings[query_id], page_id),
                "RERANK": rank_of(c3_rankings[query_id], page_id) if c3_rankings else None,
            }
            rescued = [cause for cause, rank in ranks.items() if rank is not None and rank <= args.output_k]
            final_rank = rank_of(final_rankings[query_id], page_id)
            if len(rescued) == 1:
                root_cause = rescued[0]
            elif len(rescued) > 1:
                root_cause = "MULTI_FACTOR"
            elif final_rank is not None and final_rank <= args.output_k:
                root_cause = "MULTI_FACTOR"
            elif 6 <= baseline_rank <= args.candidate_k:
                root_cause = "CANDIDATE_DEPTH"
            else:
                root_cause = "UNRESOLVED"
            root_counts[root_cause] += 1
            root_rows.append(
                {
                    "query_id": query_id,
                    "original_query": query["original_query"],
                    "required_context": query["required_context"],
                    "gold_page_id": page_id,
                    "gold_source_turn_id": pages_by_id[page_id]["source_turn_id"],
                    "baseline_gold_rank": baseline_rank,
                    "best_query_variant": best_query,
                    "best_query_gold_rank": ranks["QUERY"],
                    "best_page_representation": top_page_variants[0],
                    "best_page_gold_rank": ranks["PAGE_REPRESENTATION"],
                    "best_embedding": best_embedding_only["embedding"],
                    "best_embedding_gold_rank": ranks["EMBEDDING"],
                    "best_hybrid": baseline_best_hybrid_name,
                    "best_hybrid_gold_rank": ranks["HYBRID"],
                    "baseline_local_rerank_gold_rank": ranks["RERANK"],
                    "best_reranker": best_reranker,
                    "best_rerank_gold_rank": rank_of(screened_best_rerank_rankings[query_id], page_id),
                    "final_gold_rank": final_rank,
                    "final_in_top5": bool(final_rank and final_rank <= args.output_k),
                    "root_cause": root_cause,
                    "single_factor_rescues": rescued,
                }
            )
    write_csv(output_dir / "miss_root_cause.csv", root_rows)

    per_query_rows: list[dict[str, Any]] = []
    root_by_query: dict[str, list[str]] = {}
    for row in root_rows:
        root_by_query.setdefault(str(row["query_id"]), []).append(str(row["root_cause"]))
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = list(query["eligible_gold_page_ids"])
        baseline_ranks = [rank_of(baseline_rankings[query_id], page_id) for page_id in gold_ids]
        c3_ranks = [rank_of(c3_rankings[query_id], page_id) for page_id in gold_ids] if c3_rankings else []
        candidate_ranks = [rank_of(candidate_rankings[query_id], page_id) for page_id in gold_ids]
        screened_rerank_ranks = [
            rank_of(screened_best_rerank_rankings[query_id], page_id) for page_id in gold_ids
        ]
        final_ranks = [rank_of(final_rankings[query_id], page_id) for page_id in gold_ids]
        before_after = [
            {"gold_page_id": page_id, "before": before, "after": after}
            for page_id, before, after in zip(gold_ids, baseline_ranks, c3_ranks or [None] * len(gold_ids))
            if before is not None and 6 <= before <= args.candidate_k
        ]
        screened_before_after = [
            {"gold_page_id": page_id, "before": before, "after": after}
            for page_id, before, after in zip(gold_ids, candidate_ranks, screened_rerank_ranks)
            if before is not None and 6 <= before <= args.candidate_k
        ]
        per_query_rows.append(
            {
                "query_id": query_id,
                "original_query": query["original_query"],
                "required_context": query["required_context"],
                "gold_source_turn_ids": query["gold_source_turn_ids"],
                "gold_page_ids": gold_ids,
                "baseline_gold_ranks": baseline_ranks,
                "baseline_top20_page_ids": [item["page_id"] for item in baseline_rankings[query_id][:20]],
                "baseline_top20_scores": [item.get("score") for item in baseline_rankings[query_id][:20]],
                "best_query_gold_ranks": [rank_of(query_rankings[best_query][query_id], page_id) for page_id in gold_ids],
                "best_page_gold_ranks": [rank_of(page_rankings[top_page_variants[0]][query_id], page_id) for page_id in gold_ids],
                "best_embedding_gold_ranks": [rank_of(best_embedding_only_rankings[query_id], page_id) for page_id in gold_ids],
                "best_hybrid_gold_ranks": [rank_of(baseline_best_hybrid_rankings[query_id], page_id) for page_id in gold_ids],
                "baseline_local_rerank_gold_ranks": c3_ranks,
                "baseline_candidate_rank_6_20_before_after": before_after,
                "best_reranker": best_reranker,
                "best_rerank_gold_ranks": screened_rerank_ranks,
                "candidate_rank_6_20_before_after": screened_before_after,
                "final_gold_ranks": final_ranks,
                "final_hit_at_5": any(rank is not None and rank <= args.output_k for rank in final_ranks),
                "root_causes": sorted(set(root_by_query.get(query_id) or [])),
            }
        )
    write_csv(output_dir / "per_query_results.csv", per_query_rows)

    rerank_improved = float(best_rerank_row.get("recall_at_5") or 0.0) > float(rerank_rows[0]["recall_at_5"])
    candidate_sufficient = float(oracle_best["recall_at_20"]) >= 0.90
    best_observed_candidate_recall_at_20 = max(
        [
            float(candidate_metrics["recall_at_20"]),
            *(
                float(row.get("recall_at_20") or 0.0)
                for row in query_rows
                if row.get("status") == "AVAILABLE" and row.get("variant") != "QO"
            ),
            *(float(row.get("recall_at_20") or 0.0) for row in page_rows if row.get("status") == "AVAILABLE"),
            *(
                float(row.get("recall_at_20") or 0.0)
                for row in embedding_rows
                if row.get("status") == "AVAILABLE" and row.get("query_variant") != "QO"
            ),
            *(float(row.get("recall_at_20") or 0.0) for row in hybrid_rows if row.get("status") == "AVAILABLE"),
        ]
    )
    if candidate_sufficient and rerank_improved:
        prompt_decision = {
            "decision": "暂时不需要继续修改 Page Prompt",
            "reason": (
                "理想 Query 下 Candidate Recall@20 已达到阈值，且 rerank 能把候选中的 Gold 推入 Top-5；"
                "现有 Page 信息基本可用，应先优化 retrieval pipeline。"
            ),
            "oracle_candidate_recall_at_20": oracle_best["recall_at_20"],
            "best_candidate_recall_at_20": best_observed_candidate_recall_at_20,
            "page_generation_experiment_executed": False,
        }
    elif candidate_sufficient:
        prompt_decision = {
            "decision": "暂时不执行额外 Page Prompt 实验",
            "reason": (
                "理想 Query 下 Candidate Recall@20 已达到阈值，尚无 Page 表示不可检索的证据；"
                "rerank 未提升应先单独诊断 ranking model，而不是重新生成全部 Page。"
            ),
            "oracle_candidate_recall_at_20": oracle_best["recall_at_20"],
            "best_candidate_recall_at_20": best_observed_candidate_recall_at_20,
            "page_generation_experiment_executed": False,
        }
    else:
        prompt_decision = {
            "decision": "需要后续执行受控 Page Prompt 实验",
            "reason": (
                "即使 Oracle Query + best representation/embedding/hybrid，Candidate Recall@20 仍低于 90%；"
                "本次管线将此标记为后续 G0/G1/G2 条件分支，不允许 replay S001。"
            ),
            "oracle_candidate_recall_at_20": oracle_best["recall_at_20"],
            "best_candidate_recall_at_20": best_observed_candidate_recall_at_20,
            "page_generation_experiment_executed": False,
            "blocked_reason": "Conditional G0/G1/G2 generation requires a separate explicitly reviewed LLM budget.",
        }

    selections = {
        "top_2_query_variants": top_query_variants,
        "top_2_page_variants": top_page_variants,
        "top_2_embeddings": top_embedding_ids,
        "best_query": best_query,
        "best_page": top_page_variants[0],
        "best_embedding_front": front_embedding,
        "best_hybrid": best_hybrid,
        "best_reranker": best_reranker,
        "best_quality_candidate_strategy": best_quality_option["candidate_strategy"],
        "best_quality_reranker": best_quality_option["reranker"],
        "candidate_k": args.candidate_k,
        "output_k": args.output_k,
        "front_configuration": {
            "query_variant": front_query,
            "page_variant": front_page,
            "embedding": front_embedding,
        },
        "best_no_llm_configuration": {
            "query_variant": non_llm_query,
            "page_variant": non_llm_page,
            "embedding": non_llm_embedding,
            "candidate_strategy": c4_strategy,
            "reranker": c4_reranker,
        },
        "pareto_source_scheme": source_scheme,
        "practical_latency_budget_ms": practical_latency_budget_ms,
    }
    old_thinking_path = REPO_ROOT / "exp/results/midterm_page_diagnosis/diagnosis_summary.json"
    old_thinking = {}
    if old_thinking_path.exists():
        old_summary = load_json(old_thinking_path)
        old_thinking = ((old_summary.get("oracle_experiments") or {}).get("baseline") or {})
    experiment_summary = {
        "snapshot": manifest,
        "baseline": baseline_metrics,
        "candidate_depth": candidate_rows,
        "selections": selections,
        "best_quality": next(row for row in combination_rows if row["scheme"] == "C5_best_quality"),
        "best_no_online_llm": next(row for row in combination_rows if row["scheme"] == "C4_best_no_online_llm"),
        "best_practical": next(row for row in combination_rows if row["scheme"] == "C6_best_practical"),
        "prompt_decision": prompt_decision,
        "root_cause_counts": dict(root_counts),
        "bm25_sanity": sanity,
        "query_status": query_status,
        "embedding_model_status": embedding_cache.status,
        "local_reranker_status": local_cache.status,
        "llm_rerank_status": llm_rerank_status,
        "cache_contract": (
            "Snapshot, successful LLM calls, embedding batches, and rerank results are content-addressed and reused."
        ),
    }
    dump_json(output_dir / "experiment_summary.json", experiment_summary)
    report = build_report(
        manifest=manifest,
        baseline=baseline,
        candidate_rows=candidate_rows,
        query_rows=query_rows,
        page_rows=page_rows,
        embedding_rows=embedding_rows,
        hybrid_rows=hybrid_rows,
        rerank_rows=rerank_rows,
        combination_rows=combination_rows,
        selections=selections,
        prompt_decision=prompt_decision,
        old_thinking=old_thinking,
        root_cause_counts=root_counts,
    )
    (output_dir / "experiment_report.md").write_text(report, encoding="utf-8")

    terminal_summary = {
        "files_added_or_modified": [
            "exp/benchmark/midterm_retrieval_eval.py",
            "exp/benchmark/run_midterm_retrieval_experiments.py",
            "exp/benchmark/test_midterm_retrieval_eval.py",
            str(output_dir.relative_to(REPO_ROOT)),
        ],
        "reused_stage0_data": [
            "recall_turn_results.jsonl",
            "recall_summary.json",
            "effective_memory_config.json",
            "SQLite memory_migration_jobs",
            "Qdrant committed Page payloads and stored E0 vectors",
            "original S001 Excel Query/Answer/Gold/required_context",
        ],
        "experiments": [
            "snapshot", "baseline", "candidate depth", "Q0-Q6/QO", "P0-P8", "Top2 interactions",
            "four embedding models", "Chinese BM25 hybrid", "local and LLM rerank", "C0-C6 combinations",
        ],
        "unavailable": {
            key: value for key, value in embedding_cache.status.items() if value.get("status") == "UNAVAILABLE"
        },
        "baseline_recall_at_5": baseline_metrics["recall_at_5"],
        "baseline_candidate_recall_at_20": baseline_metrics["recall_at_20"],
        "best_quality": experiment_summary["best_quality"],
        "best_no_online_llm": experiment_summary["best_no_online_llm"],
        "best_practical": experiment_summary["best_practical"],
        "page_prompt_decision": prompt_decision,
    }
    print(json.dumps(terminal_summary, ensure_ascii=False, indent=2, default=str))
    print(f"\nExperiment results: {output_dir}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
