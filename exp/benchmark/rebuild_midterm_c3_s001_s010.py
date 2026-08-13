"""Rebuild the historical MidTerm C3 contract for the rebalanced S001-S010 workbook.

C3 is Search-P2 dense cosine over Page summaries generated with the latest
three prior Pages plus the exact three-QA eviction window.  Its embedding text
is Summary + Keywords, with the source User intentionally omitted.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from exp.benchmark.analyze_query_completion_s001_s005_v2 import load_v3_dataset
from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_dataset, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import cosine_rank, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_ablation import (  # noqa: E402
    ContextSummaryCache,
    context_layout,
    format_context_input,
    prompt_variants,
    sha256_text,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import page_text  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache, qdrant_client  # noqa: E402
from exp.benchmark.run_midterm_rerank_tuning import PRODUCTION_EMBEDDING  # noqa: E402
from exp.benchmark.run_reference_resolution_prompt_ablation import (  # noqa: E402
    PROMPT_VERSIONS as RESOLUTION_PROMPT_VERSIONS,
    PlainTextJsonlLLMCache,
    resolution_messages,
    resolved_validator,
)

DEFAULT_DATASET = REPO_ROOT / "exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx"
DEFAULT_SOURCE_DIR = REPO_ROOT / "exp/results/recall_rebalanced_s001_s010_isolated"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_c3_rebalanced_s001_s010"
MEMORY_CONFIG = REPO_ROOT / "exp/benchmark/memory_config.json"
P2_PROMPT = REPO_ROOT / "exp/results/reference_resolution_prompt_ablation/prompts/P2_slot_bounded.txt"
SESSION_CODES = tuple(f"S{index:03d}" for index in range(1, 11))
SHORT_TERM_QA_CAPACITY = 3
MIDTERM_TOP_K = 5
C3_VARIANT = "PreviousAndFollowingContext"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild C3 MidTerm rankings for rebalanced S001-S010")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--llm-timeout", type=float, default=240.0)
    parser.add_argument("--llm-retries", type=int, default=3)
    return parser.parse_args()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jobs(path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in connection.execute("SELECT * FROM memory_migration_jobs ORDER BY sequence_no")]
    finally:
        connection.close()
    for row in rows:
        row["metadata"] = json.loads(row.get("metadata_json") or "{}")
    return rows


def validate_source_runs(
    source_dir: Path,
    benchmark_sessions: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    page_identity: dict[str, dict[str, Any]] = {}
    session_rows: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        session = benchmark_sessions[code]
        result_dir = source_dir / code
        summary = load_json(result_dir / "recall_summary.json")
        if int(summary.get("total_turns") or 0) != len(session.turns) or int(summary.get("failed_turns") or 0):
            raise AssertionError(f"{code}: source recall run is incomplete")
        effective = load_json(result_dir / "effective_memory_config.json")
        history_db = Path(str(effective["history_db_path"]))
        jobs = read_jobs(history_db)
        expected_pages = len(session.turns) - SHORT_TERM_QA_CAPACITY
        if len(jobs) != expected_pages:
            raise AssertionError(f"{code}: expected {expected_pages} migration jobs, got {len(jobs)}")
        midterm_statuses = Counter(str(row.get("midterm_status") or "") for row in jobs)
        longterm_statuses = Counter(str(row.get("longterm_status") or "") for row in jobs)
        if set(midterm_statuses) - {"succeeded", "succeeded_degraded"}:
            raise AssertionError(f"{code}: incomplete MidTerm jobs: {dict(midterm_statuses)}")
        jobs_by_id = {str(row["job_id"]): row for row in jobs}
        client, collection = qdrant_client(effective)
        try:
            points, _ = client.scroll(
                f"{collection}_midterm_pages", limit=10_000, with_payload=True, with_vectors=False
            )
        finally:
            client.close()
        if len(points) != expected_pages:
            raise AssertionError(f"{code}: expected {expected_pages} committed Pages, got {len(points)}")
        for point in points:
            payload = dict(point.payload or {})
            job_id = str(payload.get("source_job_id") or "")
            job = jobs_by_id.get(job_id)
            if not job:
                raise AssertionError(f"{code}: Page {point.id} has no source migration job")
            trigger_id = str(job["metadata"].get("dataset_turn_id") or "")
            trigger_index = next(
                (turn.turn_index for turn in session.turns if turn.turn_id == trigger_id), None
            )
            if trigger_index is None or trigger_index < SHORT_TERM_QA_CAPACITY:
                raise AssertionError(f"{code}: invalid eviction trigger {trigger_id}")
            source_turn = session.turns[trigger_index - SHORT_TERM_QA_CAPACITY]
            if (
                str(payload.get("user_input") or "") != source_turn.question
                or str(payload.get("assistant_response") or "") != source_turn.answer
            ):
                raise AssertionError(f"{source_turn.turn_id}: production Page source QA mismatch")
            page_identity[source_turn.turn_id] = {
                "production_page_id": str(point.id),
                "source_job_id": job_id,
                "source_job_trigger_turn_id": trigger_id,
            }
        session_rows.append(
            {
                "session_id": code,
                "turn_count": len(session.turns),
                "page_count": expected_pages,
                "failed_turns": 0,
                "midterm_job_statuses": dict(midterm_statuses),
                "longterm_job_statuses": dict(longterm_statuses),
            }
        )
    expected_total = sum(len(session.turns) - SHORT_TERM_QA_CAPACITY for session in benchmark_sessions.values())
    if len(page_identity) != expected_total:
        raise AssertionError(f"Production Page identity count mismatch: {len(page_identity)} != {expected_total}")
    return {"pages": page_identity, "sessions": session_rows}


def routed_queries(dataset_sessions: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    queries: list[dict[str, Any]] = []
    for code, turns in dataset_sessions.items():
        for turn in turns:
            visible_short = {
                item.query_id
                for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
            }
            outside_short = [group for group in turn.gold_groups if not group.hit_by(visible_short)]
            if not outside_short:
                continue
            previous = [
                {"turn_id": item.query_id, "user": item.question, "assistant": item.answer}
                for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
            ]
            queries.append(
                {
                    "session_code": code,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "original_query": turn.question,
                    "previous_3_qa": previous,
                    "outside_shortterm_requirement_count": len(outside_short),
                }
            )
    return queries


async def generate_c3_pages(
    args: argparse.Namespace,
    benchmark_sessions: Mapping[str, Any],
    source_pages: Mapping[str, Mapping[str, Any]],
    llm_config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    output_dir = args.output_dir.resolve()
    prompt = prompt_variants()[C3_VARIANT]
    prompt_hash = sha256_text(prompt)
    prompt_path = output_dir / "prompts/Production_Add_With_Previous_And_Following_Context.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    if prompt_path.exists() and prompt_path.read_text(encoding="utf-8") != prompt:
        raise AssertionError("Existing frozen C3 Page prompt changed")
    prompt_path.write_text(prompt, encoding="utf-8")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache = ContextSummaryCache(
        output_dir / "cache/c3_page_summary_llm.jsonl",
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
    page_rows: list[dict[str, Any]] = []
    lock = asyncio.Lock()

    async def one_session(code: str) -> None:
        session = benchmark_sessions[code]
        generated_previous: list[dict[str, Any]] = []
        local_rows: list[dict[str, Any]] = []
        for source_turn in session.turns[:-SHORT_TERM_QA_CAPACITY]:
            identity = source_pages[source_turn.turn_id]
            page = {
                "session_code": code,
                "source_turn_id": source_turn.turn_id,
                "source_turn_index": source_turn.turn_index,
                "source_job_trigger_turn_id": identity["source_job_trigger_turn_id"],
                "user_input": source_turn.question,
                "assistant_response": source_turn.answer,
            }
            layout = context_layout(page, session, generated_previous, include_following=True)
            payload = format_context_input(page, layout, include_following=True)
            row = await cache.call(
                page=page,
                variant=C3_VARIANT,
                prompt=prompt,
                prompt_sha256=prompt_hash,
                payload=payload,
                layout=layout,
            )
            if row.get("status") != "SUCCESS":
                raise RuntimeError(f"C3 Page generation failed at {source_turn.turn_id}: {row.get('errors')}")
            summary = str(row["parsed"]["summary"])
            keywords = list(row["parsed"]["keywords"])
            generated_previous.append(
                {
                    "source_turn_id": source_turn.turn_id,
                    "source_turn_index": source_turn.turn_index,
                    "summary": summary,
                    "keywords": keywords,
                }
            )
            local_rows.append(
                {
                    "session_id": code,
                    "page_id": source_turn.turn_id,
                    "production_page_id": identity["production_page_id"],
                    "source_turn_id": source_turn.turn_id,
                    "source_turn_index": source_turn.turn_index,
                    "eviction_trigger_turn_id": identity["source_job_trigger_turn_id"],
                    "summary": summary,
                    "keywords": keywords,
                    "no_user_text": page_text(summary, keywords, ""),
                    "previous_context_turn_ids": [item["source_turn_id"] for item in layout["previous_context"]],
                    "following_context_turn_ids": [item["turn_id"] for item in layout["following_context"]],
                    "llm_cache_key": row["cache_key"],
                }
            )
        async with lock:
            page_rows.extend(local_rows)

    try:
        await asyncio.gather(*(one_session(code) for code in SESSION_CODES))
    finally:
        await client.close()
    page_rows.sort(key=lambda row: (str(row["session_id"]), int(row["source_turn_index"])))
    return page_rows, {
        "page_count": len(page_rows),
        "cache_hit_count": sum(str(row["llm_cache_key"]) in initial_keys for row in page_rows),
        "new_success_count": sum(str(row["llm_cache_key"]) not in initial_keys for row in page_rows),
        "prompt_sha256": prompt_hash,
        "prompt_path": str(prompt_path),
    }


async def generate_p2_queries(
    args: argparse.Namespace,
    queries: Sequence[Mapping[str, Any]],
    llm_config: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    prompt = P2_PROMPT.read_text(encoding="utf-8")
    output_prompt = args.output_dir.resolve() / "prompts/P2_slot_bounded.txt"
    output_prompt.parent.mkdir(parents=True, exist_ok=True)
    if output_prompt.exists() and output_prompt.read_text(encoding="utf-8") != prompt:
        raise AssertionError("Existing frozen P2 prompt changed")
    output_prompt.write_text(prompt, encoding="utf-8")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=llm_config["api_key"], base_url=base_url)
    cache = PlainTextJsonlLLMCache(
        args.output_dir.resolve() / "cache/p2_query_resolution_llm.jsonl",
        client=client,
        model=str(llm_config["model"]),
        timeout=args.llm_timeout,
        retries=args.llm_retries,
        concurrency=args.concurrency,
    )
    initial_keys = set(cache.success_by_key)

    async def one(query: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        row = await cache.call(
            query_id=str(query["query_id"]),
            variant="P2",
            messages=resolution_messages(query, prompt),
            prompt_version=RESOLUTION_PROMPT_VERSIONS["P2"],
            max_tokens=800,
            validator=resolved_validator,
        )
        return str(query["query_id"]), row

    try:
        results = await asyncio.gather(*(one(query) for query in queries))
    finally:
        await client.close()
    failures = {query_id: row.get("errors") for query_id, row in results if row.get("status") != "SUCCESS"}
    if failures:
        dump_json(args.output_dir.resolve() / "p2_query_failures.json", failures)
        raise RuntimeError(f"P2 generation failed for {len(failures)} Queries")
    texts = {query_id: str(row["parsed"]["resolved_query"]) for query_id, row in results}
    rows = [
        {
            "session_id": str(query["session_code"]),
            "query_id": str(query["query_id"]),
            "original_query": str(query["original_query"]),
            "resolved_query": texts[str(query["query_id"])],
        }
        for query in queries
    ]
    write_jsonl(args.output_dir.resolve() / "resolved_p2_queries.jsonl", rows)
    return texts, {
        "query_count": len(texts),
        "cache_hit_count": sum(str(row["cache_key"]) in initial_keys for _, row in results),
        "new_success_count": sum(str(row["cache_key"]) not in initial_keys for _, row in results),
        "prompt_sha256": sha256_file(P2_PROMPT),
        "prompt_path": str(output_prompt),
    }


def embed_and_rank(
    output_dir: Path,
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    page_ids = [f"C3:{row['source_turn_id']}" for row in pages]
    page_texts = [str(row["no_user_text"]) for row in pages]
    page_vectors_raw, page_metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "c3-context-no-user-S001-S010-rebalanced",
        page_ids,
        page_texts,
        measure_individual=False,
    )
    query_ids = [f"P2:{row['query_id']}:0" for row in queries]
    query_texts = [str(p2_texts[str(row["query_id"])]) for row in queries]
    query_vectors_raw, query_metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "P2-resolved-query-only-S001-S010-rebalanced",
        query_ids,
        query_texts,
        measure_individual=True,
    )
    page_vectors = {
        str(row["page_id"]): page_vectors_raw[f"C3:{row['source_turn_id']}"] for row in pages
    }
    pages_by_session: dict[str, list[Mapping[str, Any]]] = {code: [] for code in SESSION_CODES}
    for row in pages:
        pages_by_session[str(row["session_id"])].append(row)
    rankings: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["query_id"])
        candidates = [
            row
            for row in pages_by_session[str(query["session_code"])]
            if int(row["source_turn_index"]) <= int(query["turn_index"]) - SHORT_TERM_QA_CAPACITY - 1
        ]
        ranked = cosine_rank(query_vectors_raw[f"P2:{query_id}:0"], candidates, page_vectors)
        for item in ranked:
            rankings.append(
                {
                    "session_id": str(query["session_code"]),
                    "query_id": query_id,
                    "c3_rank": int(item["rank"]),
                    "page_id": str(item["page_id"]),
                    "source_turn_id": str(item["source_turn_id"]),
                    "score": float(item["score"]),
                }
            )
    write_jsonl(output_dir / "c3_rankings.jsonl", rankings)
    return rankings, {
        "embedding_model": PRODUCTION_EMBEDDING,
        "dimension": int(page_metadata["dimension"]),
        "page_embeddings": page_metadata,
        "query_embeddings": query_metadata,
        "ranking_row_count": len(rankings),
    }


def validate_c3(
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    rankings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    page_by_id = {str(row["page_id"]): row for row in pages}
    rankings_by_query: dict[str, list[Mapping[str, Any]]] = {}
    for row in rankings:
        rankings_by_query.setdefault(str(row["query_id"]), []).append(row)
    if len(page_by_id) != len(pages):
        raise AssertionError("C3 logical Page IDs are duplicated")
    for page in pages:
        if len(page["following_context_turn_ids"]) != SHORT_TERM_QA_CAPACITY:
            raise AssertionError(f"{page['source_turn_id']}: incorrect C3 following context")
        if len(page["previous_context_turn_ids"]) > SHORT_TERM_QA_CAPACITY:
            raise AssertionError(f"{page['source_turn_id']}: incorrect C3 previous context")
        if str(page["no_user_text"]) != page_text(str(page["summary"]), list(page["keywords"]), ""):
            raise AssertionError(f"{page['source_turn_id']}: C3 no-User formatter mismatch")
        if str(page["following_context_turn_ids"][-1]) != str(page["eviction_trigger_turn_id"]):
            raise AssertionError(f"{page['source_turn_id']}: eviction trigger does not match following context")
    for query in queries:
        query_id = str(query["query_id"])
        rows = sorted(rankings_by_query.get(query_id, []), key=lambda row: int(row["c3_rank"]))
        expected_count = max(0, int(query["turn_index"]) - SHORT_TERM_QA_CAPACITY)
        if len(rows) != expected_count or [int(row["c3_rank"]) for row in rows] != list(
            range(1, expected_count + 1)
        ):
            raise AssertionError(f"{query_id}: incomplete C3 ranking")
        if any(int(page_by_id[str(row["page_id"])]["source_turn_index"]) > int(query["turn_index"]) - 4 for row in rows):
            raise AssertionError(f"{query_id}: future Page leakage")
    return {
        "status": "PASS",
        "page_count": len(pages),
        "routed_query_count": len(queries),
        "ranking_row_count": len(rankings),
        "future_page_leak_count": 0,
        "context_window_validation": True,
        "no_user_embedding_contract": True,
    }


async def main() -> None:
    args = parse_args()
    args.dataset = args.dataset.resolve()
    args.source_dir = args.source_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    dataset_sessions = load_v3_dataset(args.dataset, SESSION_CODES)
    loaded_sessions = load_dataset(
        args.dataset,
        include_sheets=[turns[0].session_id for turns in dataset_sessions.values()],
    )
    benchmark_sessions = {session.turns[0].turn_id.split("-", 1)[0]: session for session in loaded_sessions}
    if set(benchmark_sessions) != set(SESSION_CODES):
        raise AssertionError("Benchmark Session set mismatch")
    source_validation = validate_source_runs(args.source_dir, benchmark_sessions)
    queries = routed_queries(dataset_sessions)
    memory_config = expand_env_placeholders(load_json(MEMORY_CONFIG))
    llm_config = dict(memory_config["llm"]["config"])
    if not llm_config.get("api_key") or str(llm_config["api_key"]).startswith("${"):
        raise RuntimeError("DEEPSEEK_API_KEY is required")
    if (
        str(llm_config["model"]) != "deepseek-v4-flash"
        or not math.isclose(float(llm_config["temperature"]), 0.1, abs_tol=1e-12)
        or not math.isclose(float(llm_config["top_p"]), 0.1, abs_tol=1e-12)
        or int(llm_config["top_k"]) != 1
    ):
        raise AssertionError("Historical C3 summary model contract changed")

    pages, page_generation = await generate_c3_pages(
        args, benchmark_sessions, source_validation["pages"], llm_config
    )
    p2_texts, p2_generation = await generate_p2_queries(args, queries, llm_config)
    write_jsonl(args.output_dir / "c3_pages.jsonl", pages)
    rankings, embedding = embed_and_rank(args.output_dir, pages, queries, p2_texts)
    validation = validate_c3(pages, queries, rankings)
    metadata = {
        "experiment": "midterm_c3_rebalanced_s001_s010",
        "dataset": str(args.dataset),
        "dataset_sha256": sha256_file(args.dataset),
        "source_dir": str(args.source_dir),
        "source_validation": source_validation["sessions"],
        "c3_contract": {
            "query": "P2 slot-bounded reference resolution; resolved_query only",
            "page": "latest 3 prior same-variant Pages + current evicted QA + exact following 3 QA window",
            "page_embedding": "Summary + Keywords; User omitted",
            "retrieval": "per-Session dense cosine over time-visible Pages",
            "top_k": MIDTERM_TOP_K,
        },
        "page_generation": page_generation,
        "p2_generation": p2_generation,
        "embedding": embedding,
        "validation": validation,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
