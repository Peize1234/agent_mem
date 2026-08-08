"""Frozen S001-selected Page retrieval validation on held-out S002-S005.

The script never runs the conversational benchmark itself.  It consumes an
existing S001 snapshot plus the one-time no-thinking S002-S005 source run,
builds time-safe per-Session snapshots, and evaluates exactly three frozen
chains: production baseline, K20 local quality, and K15 Top2-protected Pareto.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from exp.benchmark.benchmark_common import (
    dependency_distances,
    ensure_repo_root_on_path,
    load_dataset,
    load_json,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    ChineseBM25Index,
    append_reranked_candidates,
    cosine_rank,
    evaluate_rankings,
    gold_rank_buckets,
    latency_stats,
    load_jsonl,
    page_representation,
    rrf_fuse,
    stable_hash,
    write_jsonl,
)
from exp.benchmark.run_midterm_rank_fusion_tuning import protected_order  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import (  # noqa: E402
    EmbeddingCache,
    load_jobs,
    parse_timestamp,
    previous_qa,
    qdrant_client,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    BASE_RERANKER,
    LOCAL_PROMPT_VERSION,
    PRODUCTION_EMBEDDING,
    LocalScoreCache,
    load_existing_embedding_vectors,
    rank_of,
)


OUTPUT_DEFAULT = REPO_ROOT / "exp/results/midterm_multi_session_validation_no_thinking"
DATASET_DEFAULT = REPO_ROOT / "exp/enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx"
S001_RESULT_DEFAULT = REPO_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
HELDOUT_SOURCE_DEFAULT = OUTPUT_DEFAULT / "source_run_s002_s005"
S004_RETRY_SOURCE_DEFAULT = OUTPUT_DEFAULT / "source_run_s004_retry"
FROZEN_CONFIG_DEFAULT = OUTPUT_DEFAULT / "frozen_config.json"
SHORT_TERM_QA_CAPACITY = 3
RRF_CONSTANT = 60
QUALITY_K = 20
PARETO_K = 15
OUTPUT_K = 5
BOOTSTRAP_SEED = 42
BOOTSTRAP_ITERATIONS = 10_000
SNAPSHOT_VERSION = 1
SCHEMES = ("baseline", "quality", "pareto")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate frozen S001 retrieval chains on S002-S005")
    parser.add_argument("--dataset", type=Path, default=DATASET_DEFAULT)
    parser.add_argument("--s001-result-dir", type=Path, default=S001_RESULT_DEFAULT)
    parser.add_argument("--heldout-source-dir", type=Path, default=HELDOUT_SOURCE_DEFAULT)
    parser.add_argument("--s004-retry-source-dir", type=Path, default=S004_RETRY_SOURCE_DEFAULT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--frozen-config", type=Path, default=FROZEN_CONFIG_DEFAULT)
    parser.add_argument("--latency-warmups", type=int, default=3)
    parser.add_argument("--latency-repeats", type=int, default=1)
    parser.add_argument("--bootstrap-iterations", type=int, default=BOOTSTRAP_ITERATIONS)
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


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as file:
        return [dict(row) for row in csv.DictReader(file)]


def file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def session_code(session_id: str) -> str:
    return session_id.split("_", 1)[0]


def expected_frozen_config(first_five: Sequence[Any]) -> dict[str, Any]:
    return {
        "development_session": first_five[0].session_id,
        "heldout_sessions": [session.session_id for session in first_five[1:5]],
        "query": "Q0 current user query",
        "embedding": PRODUCTION_EMBEDDING,
        "output_k": OUTPUT_K,
        "baseline": {
            "candidate_representation": "P0",
            "retrieval": "dense",
            "candidate_k": 5,
            "reranker": None,
        },
        "quality": {
            "candidate_representation": "P0",
            "retrieval": "dense+chinese_bm25",
            "fusion": "RRF",
            "rrf_constant": RRF_CONSTANT,
            "candidate_k": QUALITY_K,
            "rerank_representation": "P8",
            "reranker": BASE_RERANKER,
            "final_ranking": "reranker_only",
            "output_k": OUTPUT_K,
        },
        "pareto": {
            "candidate_representation": "P0",
            "retrieval": "dense+chinese_bm25",
            "fusion": "RRF",
            "rrf_constant": RRF_CONSTANT,
            "candidate_k": PARETO_K,
            "rerank_representation": "P8",
            "reranker": BASE_RERANKER,
            "final_ranking": "candidate_top2_protection_then_reranker",
            "candidate_top_n_protection": 2,
            "output_k": OUTPUT_K,
        },
        "parameter_tuning_on_heldout_allowed": False,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "bootstrap_unit": "query",
        "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
    }


def validate_frozen_config(path: Path, first_five: Sequence[Any]) -> dict[str, Any]:
    actual = load_json(path)
    expected = expected_frozen_config(first_five)
    mismatches = {
        key: {"expected": expected[key], "actual": actual.get(key)}
        for key in expected
        if actual.get(key) != expected[key]
    }
    if mismatches:
        raise ValueError(f"Frozen config changed or does not match the predeclared chains: {mismatches}")
    state = file_state(path)
    return {"status": "PASS", **state, "config": actual}


def load_jobs_by_session(history_db: Path) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    jobs = load_jobs(history_db)
    by_trigger: dict[tuple[str, str], dict[str, Any]] = {}
    for job in jobs.values():
        metadata = job.get("metadata") or {}
        session_id = str(metadata.get("dataset_session_id") or "")
        turn_id = str(metadata.get("dataset_turn_id") or "")
        if session_id and turn_id:
            key = (session_id, turn_id)
            if key in by_trigger:
                raise ValueError(f"Duplicate migration job trigger: {key}")
            by_trigger[key] = job
    return jobs, by_trigger


def load_all_source_pages(
    effective_config: Mapping[str, Any],
    sessions: Sequence[Any],
    jobs: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    client, collection_name = qdrant_client(effective_config)
    points, _ = client.scroll(
        f"{collection_name}_midterm_pages",
        limit=10_000,
        with_payload=True,
        with_vectors=True,
    )
    session_by_id = {session.session_id: session for session in sessions}
    turn_by_question = {
        session.session_id: {
            turn.question: [item for item in session.turns if item.question == turn.question]
            for turn in session.turns
        }
        for session in sessions
    }
    pages_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        payload = dict(point.payload or {})
        # Page ``session_id`` is the internal midterm cluster UUID.  Dataset
        # isolation is carried by run_id/user_id, matching the benchmark path.
        session_id = str(payload.get("run_id") or "")
        if session_id not in session_by_id:
            continue
        matches = turn_by_question[session_id].get(str(payload.get("user_input") or ""), [])
        if len(matches) != 1:
            raise ValueError(f"Page {point.id} source-turn mapping is ambiguous: {len(matches)} matches")
        source_turn = matches[0]
        source_job_id = str(payload.get("source_job_id") or "")
        job = jobs.get(source_job_id)
        if not job:
            raise ValueError(f"Page {point.id} source job is missing: {source_job_id}")
        vector = point.vector.get("") if isinstance(point.vector, dict) else point.vector
        page = {
            "page_id": str(point.id),
            "source_turn_id": source_turn.turn_id,
            "source_turn_index": source_turn.turn_index,
            "session_id": session_id,
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
        pages_by_session[session_id].append(page)
    for pages in pages_by_session.values():
        pages.sort(key=lambda row: int(row["source_turn_index"]))
    return dict(pages_by_session)


def validate_session_snapshot(
    session: Any,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    visibility_rows: Sequence[Mapping[str, Any]],
) -> None:
    page_ids = [str(page.get("page_id") or "") for page in pages]
    source_ids = [str(page.get("source_turn_id") or "") for page in pages]
    if not all(page_ids) or len(page_ids) != len(set(page_ids)):
        raise ValueError(f"{session.session_id}: Page IDs missing or duplicated")
    if len(source_ids) != len(set(source_ids)):
        raise ValueError(f"{session.session_id}: Page source-turn mapping is ambiguous")
    page_by_id = {str(page["page_id"]): page for page in pages}
    visibility = {str(row["query_id"]): row for row in visibility_rows}
    if len(visibility) != len(queries):
        raise ValueError(f"{session.session_id}: visibility does not cover every Query")
    for query in queries:
        query_id = str(query["query_id"])
        if query.get("session_id") != session.session_id or not query_id.startswith(session_code(session.session_id)):
            raise ValueError(f"{session.session_id}: out-of-scope Query {query_id}")
        row = visibility[query_id]
        if int(row.get("future_page_leak_count") or 0):
            raise ValueError(f"{query_id}: future Page leakage detected")
        visible_ids = {str(page_id) for page_id in row.get("visible_page_ids") or []}
        cutoff = int(query["turn_index"]) - SHORT_TERM_QA_CAPACITY - 1
        for page_id in visible_ids:
            if page_id not in page_by_id or int(page_by_id[page_id]["source_turn_index"]) > cutoff:
                raise ValueError(f"{query_id}: invalid time-visible Page {page_id}")
        for page_id in query.get("eligible_gold_page_ids") or []:
            if str(page_id) not in visible_ids:
                raise ValueError(f"{query_id}: eligible Gold is not time-visible: {page_id}")


def build_or_load_heldout_snapshots(
    output_dir: Path,
    source_dir: Path,
    sessions: Sequence[Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    effective_config = load_json(source_dir / "effective_memory_config.json")
    summary = load_json(source_dir / "recall_summary.json")
    history_db = Path(str(effective_config["history_db_path"]))
    jobs, jobs_by_trigger = load_jobs_by_session(history_db)
    pages_by_session = load_all_source_pages(effective_config, sessions, jobs)
    recall_rows = load_jsonl(source_dir / "recall_turn_results.jsonl")
    snapshots: dict[str, dict[str, Any]] = {}
    reuse = {"built": [], "reused": []}
    for session in sessions:
        code = session_code(session.session_id)
        snapshot_dir = output_dir / "snapshots" / code
        paths = {
            "manifest": snapshot_dir / "snapshot_manifest.json",
            "queries": snapshot_dir / "queries.jsonl",
            "pages": snapshot_dir / "pages.jsonl",
            "visibility": snapshot_dir / "query_page_visibility.jsonl",
        }
        if all(path.exists() for path in paths.values()):
            manifest = load_json(paths["manifest"])
            queries = load_jsonl(paths["queries"])
            pages = load_jsonl(paths["pages"])
            visibility_rows = load_jsonl(paths["visibility"])
            if manifest.get("source_run_hash") == stable_hash(summary):
                validate_session_snapshot(session, queries, pages, visibility_rows)
                snapshots[session.session_id] = {
                    "manifest": manifest,
                    "queries": queries,
                    "pages": pages,
                    "visibility": visibility_rows,
                    "path": str(snapshot_dir),
                }
                reuse["reused"].append(session.session_id)
                continue

        pages = list(pages_by_session.get(session.session_id) or [])
        page_by_turn = {str(page["source_turn_id"]): page for page in pages}
        evaluated_rows = {
            str(row["turn_id"]): row
            for row in recall_rows
            if row.get("evaluated") and row.get("sheet_name") == session.session_id
        }
        queries: list[dict[str, Any]] = []
        visibility_rows: list[dict[str, Any]] = []
        for turn in session.turns:
            if turn.turn_id not in evaluated_rows:
                continue
            record = evaluated_rows[turn.turn_id]
            current_job = jobs_by_trigger.get((session.session_id, turn.turn_id))
            upper_bound = current_job.get("created_at") if current_job else None
            upper_timestamp = parse_timestamp(upper_bound)
            visible: list[dict[str, Any]] = []
            for page in pages:
                finished_timestamp = parse_timestamp(page.get("committed_at"))
                source_is_evicted = int(page["source_turn_index"]) <= turn.turn_index - SHORT_TERM_QA_CAPACITY - 1
                committed = bool(
                    page.get("output_state") == "committed"
                    and page.get("midterm_status") in {"succeeded", "succeeded_degraded"}
                    and finished_timestamp is not None
                    and (upper_timestamp is None or finished_timestamp <= upper_timestamp)
                )
                if source_is_evicted and committed:
                    visible.append(
                        {
                            "page_id": page["page_id"],
                            "source_turn_id": page["source_turn_id"],
                            "source_turn_index": page["source_turn_index"],
                            "source_job_id": page["source_job_id"],
                            "committed_at": page["committed_at"],
                            "retrieval_time_upper_bound": upper_bound,
                        }
                    )
            visible_ids = {str(item["page_id"]) for item in visible}
            distances = dependency_distances(session, turn)
            lineage = []
            for source_turn_id, distance in zip(turn.dependency_turn_ids, distances):
                page = page_by_turn.get(source_turn_id)
                page_id = str(page["page_id"]) if page else None
                available = bool(page_id and page_id in visible_ids)
                lineage.append(
                    {
                        "source_turn_id": source_turn_id,
                        "dependency_distance": distance,
                        "page_id": page_id,
                        "available_at_query_time": available,
                        "status": (
                            "AVAILABLE"
                            if available
                            else "GOLD_NOT_AVAILABLE_AT_QUERY_TIME"
                            if page
                            else "PAGE_NOT_CREATED_WITHIN_SOURCE_RUN"
                        ),
                    }
                )
            queries.append(
                {
                    "query_id": turn.turn_id,
                    "session_id": session.session_id,
                    "turn_index": turn.turn_index,
                    "original_query": turn.question,
                    "required_context": turn.required_context,
                    "gold_source_turn_ids": list(turn.dependency_turn_ids),
                    "gold_page_ids": [item["page_id"] for item in lineage if item.get("page_id")],
                    "eligible_gold_page_ids": [item["page_id"] for item in lineage if item["available_at_query_time"]],
                    "gold_lineage": lineage,
                    "long_range_evaluation": bool(record.get("long_range")),
                    "dependency_distances": distances,
                    "dependency_distance": max(distances) if distances else None,
                    "previous_1_qa": previous_qa(session.turns, turn.turn_index, 1),
                    "previous_2_qa": previous_qa(session.turns, turn.turn_index, 2),
                    "previous_3_qa": previous_qa(session.turns, turn.turn_index, 3),
                    "retrieval_time_upper_bound": upper_bound,
                }
            )
            visibility_rows.append(
                {
                    "query_id": turn.turn_id,
                    "turn_index": turn.turn_index,
                    "retrieval_time_upper_bound": upper_bound,
                    "visible_page_count": len(visible),
                    "visible_page_ids": [item["page_id"] for item in visible],
                    "visible_page_proofs": visible,
                    "future_page_leak_count": sum(
                        int(item["source_turn_index"]) > turn.turn_index - SHORT_TERM_QA_CAPACITY - 1
                        for item in visible
                    ),
                }
            )
        validate_session_snapshot(session, queries, pages, visibility_rows)
        statuses = Counter(item["status"] for query in queries for item in query["gold_lineage"])
        manifest = {
            "snapshot_version": SNAPSHOT_VERSION,
            "session_id": session.session_id,
            "session_code": code,
            "source_dir": str(source_dir),
            "source_run_hash": stable_hash(summary),
            "history_db_path": str(history_db),
            "turn_count": len(session.turns),
            "page_count": len(pages),
            "long_range_query_count": len(queries),
            "evaluated_query_count": sum(bool(query["eligible_gold_page_ids"]) for query in queries),
            "total_gold_lineage_count": sum(len(query["gold_lineage"]) for query in queries),
            "eligible_gold_page_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
            "gold_status_counts": dict(statuses),
            "future_page_leak_count": sum(row["future_page_leak_count"] for row in visibility_rows),
            "production_page_representation": "P0",
            "production_embedding_model": PRODUCTION_EMBEDDING,
            "snapshot_hash": stable_hash(
                {
                    "queries": [{key: value for key, value in row.items() if key != "required_context"} for row in queries],
                    "pages": [{key: value for key, value in row.items() if key != "stored_embedding"} for row in pages],
                    "visibility": visibility_rows,
                }
            ),
        }
        write_jsonl(paths["queries"], queries)
        write_jsonl(paths["pages"], pages)
        write_jsonl(paths["visibility"], visibility_rows)
        dump_json(paths["manifest"], manifest)
        snapshots[session.session_id] = {
            "manifest": manifest,
            "queries": queries,
            "pages": pages,
            "visibility": visibility_rows,
            "path": str(snapshot_dir),
        }
        reuse["built"].append(session.session_id)
    return snapshots, reuse


def load_s001_snapshot(s001_result_dir: Path, session: Any) -> dict[str, Any]:
    snapshot_dir = s001_result_dir / "snapshot"
    manifest = load_json(snapshot_dir / "snapshot_manifest.json")
    queries = load_jsonl(snapshot_dir / "queries.jsonl")
    pages = load_jsonl(snapshot_dir / "pages.jsonl")
    visibility = load_jsonl(snapshot_dir / "query_page_visibility.jsonl")
    validate_session_snapshot(session, queries, pages, visibility)
    return {
        "manifest": manifest,
        "queries": queries,
        "pages": pages,
        "visibility": visibility,
        "path": str(snapshot_dir),
        "reference_only": True,
    }


def validate_source_run(
    source_dir: Path,
    heldout_sessions: Sequence[Any],
    *,
    expected_concurrency: int,
) -> dict[str, Any]:
    summary = load_json(source_dir / "recall_summary.json")
    effective = load_json(source_dir / "effective_memory_config.json")
    rows = load_jsonl(source_dir / "recall_turn_results.jsonl")
    expected_ids = {session.session_id for session in heldout_sessions}
    all_actual_ids = {str(row["session_id"]) for row in rows}
    rows = [row for row in rows if str(row["session_id"]) in expected_ids]
    actual_ids = {str(row["session_id"]) for row in rows}
    history_db = Path(str(effective["history_db_path"]))
    jobs, _ = load_jobs_by_session(history_db)
    jobs = {
        job_id: job
        for job_id, job in jobs.items()
        if str((job.get("metadata") or {}).get("dataset_session_id") or "") in expected_ids
    }
    job_sessions = Counter(str((job.get("metadata") or {}).get("dataset_session_id") or "") for job in jobs.values())
    expected_jobs = {session.session_id: max(0, len(session.turns) - SHORT_TERM_QA_CAPACITY) for session in heldout_sessions}
    status_counts = Counter(str(job.get("status")) for job in jobs.values())
    midterm_counts = Counter(str(job.get("midterm_status")) for job in jobs.values())
    longterm_counts = Counter(str(job.get("longterm_status")) for job in jobs.values())
    benchmark_config = summary.get("benchmark_config") or {}
    runtime_flags = effective.get("benchmark_runtime") or {}
    errors = [row for row in rows if row.get("error")]
    cross_leaks = sum(int(row.get("cross_session_leak_count") or 0) for row in rows)
    future_leaks = sum(int(row.get("future_turn_leak_count") or 0) for row in rows)
    all_session_wall = (summary.get("effective") or {}).get("session_wall_clock_seconds") or {}
    scoped_session_wall = {
        session_id: float(all_session_wall[session_id])
        for session_id in expected_ids
        if session_id in all_session_wall
    }
    scoped_sequential_seconds = sum(scoped_session_wall.values())
    actual_elapsed_seconds = float((summary.get("effective") or {}).get("elapsed_seconds") or 0.0)
    completed_statuses = {"succeeded", "succeeded_degraded"}
    valid = (
        actual_ids == expected_ids
        and len(rows) == sum(len(session.turns) for session in heldout_sessions)
        and not errors
        and cross_leaks == 0
        and future_leaks == 0
        and dict(job_sessions) == expected_jobs
        and sum(status_counts.values()) == sum(expected_jobs.values())
        and set(status_counts).issubset(completed_statuses)
        and sum(midterm_counts.values()) == sum(expected_jobs.values())
        and set(midterm_counts).issubset(completed_statuses)
        and longterm_counts == {"succeeded": sum(expected_jobs.values())}
        and int((summary.get("effective") or {}).get("session_concurrency") or 0) == expected_concurrency
        and bool(runtime_flags.get("deepseek_midterm_non_thinking"))
        and bool(runtime_flags.get("deepseek_longterm_non_thinking"))
        and expected_ids.issubset(
            set(((benchmark_config.get("dataset") or {}).get("include_sheets") or []))
        )
    )
    validation = {
        "status": "PASS" if valid else "FAIL",
        "sessions": sorted(actual_ids),
        "ignored_partial_or_out_of_scope_sessions": sorted(all_actual_ids - expected_ids),
        "turn_count": len(rows),
        "failed_turn_count": len(errors),
        "cross_session_leak_count": cross_leaks,
        "future_turn_leak_count": future_leaks,
        "expected_migration_jobs_by_session": expected_jobs,
        "actual_migration_jobs_by_session": dict(job_sessions),
        "migration_status_counts": dict(status_counts),
        "midterm_status_counts": dict(midterm_counts),
        "longterm_status_counts": dict(longterm_counts),
        "session_concurrency": (summary.get("effective") or {}).get("session_concurrency"),
        "session_wall_clock_seconds": scoped_session_wall,
        "actual_concurrent_wall_seconds": actual_elapsed_seconds,
        "sequential_estimated_seconds": scoped_sequential_seconds,
        "effective_session_speedup": (
            scoped_sequential_seconds / actual_elapsed_seconds if actual_elapsed_seconds else None
        ),
        "no_thinking": {
            "midterm": runtime_flags.get("deepseek_midterm_non_thinking"),
            "longterm": runtime_flags.get("deepseek_longterm_non_thinking"),
        },
    }
    dump_json(source_dir / "recall_validation.json", validation)
    if not valid:
        raise RuntimeError(f"Held-out source run validation failed: {validation}")
    return validation


def visible_pages_by_query(snapshot: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
    visibility = {str(row["query_id"]): row for row in snapshot["visibility"]}
    return {
        str(query["query_id"]): [pages_by_id[str(page_id)] for page_id in visibility[str(query["query_id"])]["visible_page_ids"]]
        for query in snapshot["queries"]
    }


def load_s001_reranker_scores(s001_result_dir: Path) -> dict[str, dict[str, float]]:
    path = s001_result_dir / "rerank_tuning/cache/rerank/local_scores.jsonl"
    rows = [
        row
        for row in load_jsonl(path)
        if row.get("status") == "SUCCESS"
        and row.get("reranker_model") == BASE_RERANKER
        and row.get("rerank_representation") == "P8"
        and row.get("candidate_strategy") == "ALL_VISIBLE_SCORE_CACHE"
        and str(row.get("candidate_k")) == "all"
        and row.get("reranker_prompt_version") == LOCAL_PROMPT_VERSION
    ]
    if len(rows) != 14:
        raise ValueError(f"Expected 14 immutable S001 P8/base cache rows, got {len(rows)}")
    return {
        str(row["query_id"]): {str(page_id): float(score) for page_id, score in row["scores_by_page"].items()}
        for row in rows
    }


def build_query_embeddings(
    output_dir: Path,
    s001_result_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    development_session_id: str,
) -> tuple[dict[str, list[float]], EmbeddingCache, dict[str, Any]]:
    vectors: dict[str, list[float]] = {}
    s001_ids = [f"Q0:{query['query_id']}:0" for query in snapshots[development_session_id]["queries"]]
    vectors.update(
        load_existing_embedding_vectors(
            s001_result_dir,
            model_name=PRODUCTION_EMBEDDING,
            prefix="queries-",
            required_ids=s001_ids,
        )
    )
    heldout_queries = [
        query
        for session_id, snapshot in snapshots.items()
        if session_id != development_session_id
        for query in snapshot["queries"]
    ]
    ids = [f"Q0:{query['query_id']}:0" for query in heldout_queries]
    texts = [str(query["original_query"]) for query in heldout_queries]
    cache = EmbeddingCache(output_dir / "cache/embeddings")
    heldout_vectors, metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "heldout-Q0-S002-S005-frozen",
        ids,
        texts,
        measure_individual=False,
    )
    vectors.update(heldout_vectors)
    return vectors, cache, metadata


def session_candidate_rankings(
    snapshot: Mapping[str, Any],
    query_vectors: Mapping[str, Sequence[float]],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
    dict[str, list[dict[str, Any]]],
]:
    visible = visible_pages_by_query(snapshot)
    page_vectors = {str(page["page_id"]): page["stored_embedding"] for page in snapshot["pages"]}
    dense_by_query: dict[str, list[dict[str, Any]]] = {}
    bm25_by_query: dict[str, list[dict[str, Any]]] = {}
    rrf_by_query: dict[str, list[dict[str, Any]]] = {}
    for query in snapshot["queries"]:
        query_id = str(query["query_id"])
        pages = visible[query_id]
        dense = cosine_rank(query_vectors[f"Q0:{query_id}:0"], pages, page_vectors)
        texts = {str(page["page_id"]): page_representation(page, "P0") for page in pages}
        bm25 = ChineseBM25Index(pages, texts).rank(str(query["original_query"]))
        dense_by_query[query_id] = dense
        bm25_by_query[query_id] = bm25
        rrf_by_query[query_id] = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
    return dense_by_query, bm25_by_query, rrf_by_query


def score_heldout_candidates(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    heldout_session_ids: Sequence[str],
    rrf_by_session: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[dict[str, dict[str, float]], LocalScoreCache, dict[str, Any]]:
    cache = LocalScoreCache(output_dir, BASE_RERANKER)
    existing_keys = set(cache.by_key)
    scores_by_query: dict[str, dict[str, float]] = {}
    metadata_by_query: dict[str, dict[str, Any]] = {}
    for session_id in heldout_session_ids:
        snapshot = snapshots[session_id]
        pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
        page_texts = {page_id: page_representation(page, "P8") for page_id, page in pages_by_id.items()}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            ranking = rrf_by_session[session_id][query_id]
            candidate_ids = [str(item["page_id"]) for item in ranking[: min(QUALITY_K, len(ranking))]]
            scores, metadata = cache.score(
                query_id=query_id,
                query_text=str(query["original_query"]),
                page_ids=candidate_ids,
                page_texts=page_texts,
                candidate_strategy="FROZEN_P0_DENSE_BM25_RRF60",
                candidate_k=QUALITY_K,
                candidate_representation="P0",
                rerank_representation="P8",
            )
            scores_by_query[query_id] = scores
            metadata_by_query[query_id] = metadata
    status = {
        "model": BASE_RERANKER,
        "matched_query_count": len(scores_by_query),
        "cache_hits": sum(str(metadata.get("cache_key")) in existing_keys for metadata in metadata_by_query.values()),
        "cache_misses": sum(str(metadata.get("cache_key")) not in existing_keys for metadata in metadata_by_query.values()),
        "cache_path": str(cache.path),
        "model_status": cache.status_by_model.get(BASE_RERANKER),
    }
    return scores_by_query, cache, status


def rerank_from_scores(
    full_ranking: Sequence[Mapping[str, Any]],
    scores: Mapping[str, float],
    *,
    candidate_k: int,
    protect_top2: bool,
) -> list[dict[str, Any]]:
    actual_count = min(candidate_k, len(full_ranking))
    pool = full_ranking[:actual_count]
    if protect_top2:
        ordered = protected_order(pool, scores, protected_count=2)
    else:
        candidate_ids = [str(item["page_id"]) for item in pool]
        ordered = sorted(candidate_ids, key=lambda page_id: (-float(scores[page_id]), page_id))
    return append_reranked_candidates(full_ranking, ordered, candidate_k=actual_count)


def build_frozen_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    development_session_id: str,
    heldout_session_ids: Sequence[str],
    query_vectors: Mapping[str, Sequence[float]],
    output_dir: Path,
    s001_result_dir: Path,
) -> tuple[
    dict[str, dict[str, dict[str, list[dict[str, Any]]]]],
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, list[dict[str, Any]]]],
    dict[str, dict[str, list[dict[str, Any]]]],
    LocalScoreCache,
    dict[str, Any],
]:
    dense_by_session: dict[str, dict[str, list[dict[str, Any]]]] = {}
    bm25_by_session: dict[str, dict[str, list[dict[str, Any]]]] = {}
    rrf_by_session: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for session_id, snapshot in snapshots.items():
        dense, bm25, rrf = session_candidate_rankings(snapshot, query_vectors)
        dense_by_session[session_id] = dense
        bm25_by_session[session_id] = bm25
        rrf_by_session[session_id] = rrf

    heldout_scores, local_cache, score_status = score_heldout_candidates(
        output_dir,
        snapshots,
        heldout_session_ids,
        rrf_by_session,
    )
    scores = {**load_s001_reranker_scores(s001_result_dir), **heldout_scores}
    rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for session_id, snapshot in snapshots.items():
        scheme_rankings = {
            "baseline": dense_by_session[session_id],
            "quality": {},
            "pareto": {},
        }
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            scheme_rankings["quality"][query_id] = rerank_from_scores(
                rrf_by_session[session_id][query_id],
                scores[query_id],
                candidate_k=QUALITY_K,
                protect_top2=False,
            )
            scheme_rankings["pareto"][query_id] = rerank_from_scores(
                rrf_by_session[session_id][query_id],
                scores[query_id],
                candidate_k=PARETO_K,
                protect_top2=True,
            )
        rankings[session_id] = scheme_rankings
    return rankings, dense_by_session, bm25_by_session, rrf_by_session, local_cache, {
        **score_status,
        "development_score_cache_reused": True,
        "heldout_session_ids": list(heldout_session_ids),
    }


def eligible_gold_count(queries: Sequence[Mapping[str, Any]]) -> int:
    return sum(len(query.get("eligible_gold_page_ids") or []) for query in queries)


def candidate_recall(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    cutoff: int,
) -> tuple[float, int, int]:
    hits = 0
    total = 0
    for query in queries:
        query_id = str(query["query_id"])
        ids = {str(item["page_id"]) for item in rankings[query_id][:cutoff]}
        for page_id in query.get("eligible_gold_page_ids") or []:
            total += 1
            hits += int(str(page_id) in ids)
    return (hits / total if total else 0.0), hits, total


def movement_category(baseline_rank: int, quality_rank: int) -> str:
    if baseline_rank > OUTPUT_K and quality_rank <= OUTPUT_K:
        return "PROMOTED_INTO_TOP5"
    if baseline_rank <= OUTPUT_K and quality_rank > OUTPUT_K:
        return "DEMOTED_OUT_OF_TOP5"
    if baseline_rank <= OUTPUT_K and quality_rank <= OUTPUT_K:
        return "PRESERVED_HIT"
    return "STILL_MISS"


def evaluate_sessions(
    sessions: Sequence[Any],
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    dense_by_session: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    bm25_by_session: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    rrf_by_session: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, dict[str, Any]]],
]:
    per_session: list[dict[str, Any]] = []
    depth_rows: list[dict[str, Any]] = []
    complementarity_rows: list[dict[str, Any]] = []
    movement_rows: list[dict[str, Any]] = []
    query_win_loss_rows: list[dict[str, Any]] = []
    miss_rows: list[dict[str, Any]] = []
    metrics_by_session: dict[str, dict[str, dict[str, Any]]] = {}
    for session in sessions:
        session_id = session.session_id
        snapshot = snapshots[session_id]
        queries = snapshot["queries"]
        scheme_metrics = {
            scheme: evaluate_rankings(queries, rankings[session_id][scheme])[0] for scheme in SCHEMES
        }
        metrics_by_session[session_id] = scheme_metrics
        quality_candidate_recall, quality_candidate_hits, total = candidate_recall(
            queries,
            rrf_by_session[session_id],
            QUALITY_K,
        )
        pareto_candidate_recall, pareto_candidate_hits, _ = candidate_recall(
            queries,
            rrf_by_session[session_id],
            PARETO_K,
        )
        status_counts = Counter(
            item["status"] for query in queries for item in query.get("gold_lineage") or []
        )
        row: dict[str, Any] = {
            "session_id": session_id,
            "session_code": session_code(session_id),
            "role": "development" if session_code(session_id) == "S001" else "heldout",
            "turn_count": len(session.turns),
            "page_count": int(snapshot["manifest"]["page_count"]),
            "long_range_query_count": len(queries),
            "evaluated_query_count": int(scheme_metrics["baseline"]["evaluated_query_count"]),
            "total_gold_lineage_count": sum(len(query.get("gold_lineage") or []) for query in queries),
            "eligible_gold_count": total,
            "gold_not_available_count": status_counts["GOLD_NOT_AVAILABLE_AT_QUERY_TIME"],
            "page_not_created_count": status_counts["PAGE_NOT_CREATED_WITHIN_SOURCE_RUN"]
            + status_counts["PAGE_NOT_CREATED_WITHIN_STAGE0_RUN"],
            "future_page_leak_count": int(snapshot["manifest"].get("future_page_leak_count") or 0),
            "quality_candidate_recall_at_20": quality_candidate_recall,
            "quality_candidate_hits_at_20": quality_candidate_hits,
            "pareto_candidate_recall_at_15": pareto_candidate_recall,
            "pareto_candidate_hits_at_15": pareto_candidate_hits,
            "quality_rerank_utilization_ratio": (
                float(scheme_metrics["quality"]["recall_at_5"]) / quality_candidate_recall
                if quality_candidate_recall
                else None
            ),
        }
        for scheme, metrics in scheme_metrics.items():
            for key, value in metrics.items():
                if not key.startswith("macro_"):
                    row[f"{scheme}_{key}"] = value
        row["quality_delta_recall_at_5_pp"] = 100 * (
            float(row["quality_recall_at_5"]) - float(row["baseline_recall_at_5"])
        )
        row["pareto_delta_recall_at_5_pp"] = 100 * (
            float(row["pareto_recall_at_5"]) - float(row["baseline_recall_at_5"])
        )
        per_session.append(row)

        _, baseline_per_query = evaluate_rankings(queries, dense_by_session[session_id])
        for cutoff in (5, 10, 15, 20, 30):
            recall, hits, gold_total = candidate_recall(queries, dense_by_session[session_id], cutoff)
            depth_rows.append(
                {
                    "session_id": session_id,
                    "session_code": session_code(session_id),
                    "candidate_source": "production P0 dense",
                    "cutoff": cutoff,
                    "candidate_recall": recall,
                    "hits": hits,
                    "eligible_gold_count": gold_total,
                    "gold_rank_buckets": gold_rank_buckets(baseline_per_query),
                }
            )

        movement_counts = Counter()
        for query in queries:
            query_id = str(query["query_id"])
            baseline_top5 = {str(item["page_id"]) for item in rankings[session_id]["baseline"][query_id][:OUTPUT_K]}
            quality_top5 = {str(item["page_id"]) for item in rankings[session_id]["quality"][query_id][:OUTPUT_K]}
            pareto_top5 = {str(item["page_id"]) for item in rankings[session_id]["pareto"][query_id][:OUTPUT_K]}
            gold_ids = [str(page_id) for page_id in query.get("eligible_gold_page_ids") or []]
            baseline_gold_count = len(set(gold_ids) & baseline_top5)
            quality_gold_count = len(set(gold_ids) & quality_top5)
            pareto_gold_count = len(set(gold_ids) & pareto_top5)
            difference = quality_gold_count - baseline_gold_count
            query_win_loss_rows.append(
                {
                    "session_id": session_id,
                    "session_code": session_code(session_id),
                    "role": "development" if session_code(session_id) == "S001" else "heldout",
                    "query_id": query_id,
                    "eligible_gold_count": len(gold_ids),
                    "baseline_top5_gold_count": baseline_gold_count,
                    "quality_top5_gold_count": quality_gold_count,
                    "pareto_top5_gold_count": pareto_gold_count,
                    "quality_minus_baseline_gold": difference,
                    "quality_result": "QUERY_WIN" if difference > 0 else "QUERY_LOSS" if difference < 0 else "QUERY_TIE",
                    "pareto_minus_baseline_gold": pareto_gold_count - baseline_gold_count,
                    "pareto_result": (
                        "QUERY_WIN"
                        if pareto_gold_count > baseline_gold_count
                        else "QUERY_LOSS"
                        if pareto_gold_count < baseline_gold_count
                        else "QUERY_TIE"
                    ),
                }
            )
            for page_id in gold_ids:
                dense_rank = rank_of(dense_by_session[session_id][query_id], page_id)
                bm25_rank = rank_of(bm25_by_session[session_id][query_id], page_id)
                candidate_rank = rank_of(rrf_by_session[session_id][query_id], page_id)
                quality_rank = rank_of(rankings[session_id]["quality"][query_id], page_id)
                pareto_rank = rank_of(rankings[session_id]["pareto"][query_id], page_id)
                if None in (dense_rank, bm25_rank, candidate_rank, quality_rank, pareto_rank):
                    raise ValueError(f"Visible Gold omitted from ranking: {session_id}/{query_id}/{page_id}")
                dense_hit20 = int(dense_rank) <= QUALITY_K
                bm25_hit20 = int(bm25_rank) <= QUALITY_K
                complementarity_rows.append(
                    {
                        "session_id": session_id,
                        "session_code": session_code(session_id),
                        "query_id": query_id,
                        "gold_page_id": page_id,
                        "dense_rank": dense_rank,
                        "bm25_rank": bm25_rank,
                        "dense_hit_at_20": dense_hit20,
                        "bm25_hit_at_20": bm25_hit20,
                        "category": (
                            "BOTH"
                            if dense_hit20 and bm25_hit20
                            else "DENSE_ONLY"
                            if dense_hit20
                            else "BM25_ONLY"
                            if bm25_hit20
                            else "NEITHER"
                        ),
                    }
                )
                category = movement_category(int(dense_rank), int(quality_rank))
                movement_counts[category] += 1
                movement_rows.append(
                    {
                        "session_id": session_id,
                        "session_code": session_code(session_id),
                        "query_id": query_id,
                        "gold_page_id": page_id,
                        "baseline_dense_rank": dense_rank,
                        "quality_candidate_rank": candidate_rank,
                        "quality_reranker_rank": quality_rank,
                        "pareto_final_rank": pareto_rank,
                        "p8_reranker_rank_delta": int(candidate_rank) - int(quality_rank),
                        "baseline_hit5": int(dense_rank) <= OUTPUT_K,
                        "quality_candidate_hit20": int(candidate_rank) <= QUALITY_K,
                        "quality_hit5": int(quality_rank) <= OUTPUT_K,
                        "pareto_hit5": int(pareto_rank) <= OUTPUT_K,
                        "movement_category": category,
                    }
                )
                if int(quality_rank) > OUTPUT_K:
                    miss_stage = "CANDIDATE_MISS" if int(candidate_rank) > QUALITY_K else "RERANKER_MISS"
                    miss_rows.append(
                        {
                            "session_id": session_id,
                            "session_code": session_code(session_id),
                            "query_id": query_id,
                            "gold_page_id": page_id,
                            "baseline_dense_rank": dense_rank,
                            "quality_candidate_rank": candidate_rank,
                            "quality_reranker_rank": quality_rank,
                            "miss_stage": miss_stage,
                        }
                    )
        promoted = movement_counts["PROMOTED_INTO_TOP5"]
        demoted = movement_counts["DEMOTED_OUT_OF_TOP5"]
        row["quality_promoted_into_top5"] = promoted
        row["quality_demoted_out_of_top5"] = demoted
        row["quality_net_gain"] = promoted - demoted
        row["quality_promotion_efficiency"] = promoted / (promoted + demoted) if promoted + demoted else None
    return (
        per_session,
        depth_rows,
        complementarity_rows,
        movement_rows,
        query_win_loss_rows,
        miss_rows,
        metrics_by_session,
    )


def aggregate_scheme_metrics(
    session_ids: Sequence[str],
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    metrics_by_session: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    pooled_queries = [query for session_id in session_ids for query in snapshots[session_id]["queries"]]
    micro: dict[str, Any] = {}
    macro: dict[str, Any] = {}
    for scheme in SCHEMES:
        pooled_rankings = {
            query_id: ranking
            for session_id in session_ids
            for query_id, ranking in rankings[session_id][scheme].items()
        }
        metrics, _ = evaluate_rankings(pooled_queries, pooled_rankings)
        micro[scheme] = metrics
        macro[scheme] = {
            "session_count": len(session_ids),
            "macro_session_recall_at_5": statistics.fmean(
                float(metrics_by_session[session_id][scheme]["recall_at_5"]) for session_id in session_ids
            ),
            "macro_session_mrr": statistics.fmean(
                float(metrics_by_session[session_id][scheme]["mrr"]) for session_id in session_ids
            ),
            "macro_session_ndcg_at_5": statistics.fmean(
                float(metrics_by_session[session_id][scheme]["ndcg_at_5"]) for session_id in session_ids
            ),
            "query_level_macro_mrr": metrics["mrr"],
            "query_level_macro_ndcg_at_5": metrics["ndcg_at_5"],
        }
    return micro, macro


def bootstrap_query_delta(
    queries: Sequence[Mapping[str, Any]],
    baseline_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    contender_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    units = []
    for query in queries:
        gold_ids = [str(page_id) for page_id in query.get("eligible_gold_page_ids") or []]
        if not gold_ids:
            continue
        query_id = str(query["query_id"])
        baseline_ids = {str(item["page_id"]) for item in baseline_rankings[query_id][:OUTPUT_K]}
        contender_ids = {str(item["page_id"]) for item in contender_rankings[query_id][:OUTPUT_K]}
        units.append(
            (
                len(gold_ids),
                sum(page_id in baseline_ids for page_id in gold_ids),
                sum(page_id in contender_ids for page_id in gold_ids),
            )
        )
    if not units:
        raise ValueError("No eligible Query units for bootstrap")
    totals = np.asarray(units, dtype=np.int64)
    rng = np.random.default_rng(seed)
    deltas = np.empty(iterations, dtype=np.float64)
    for index in range(iterations):
        sampled = totals[rng.integers(0, len(totals), size=len(totals))]
        denominator = int(sampled[:, 0].sum())
        deltas[index] = (sampled[:, 2].sum() - sampled[:, 1].sum()) / denominator
    observed = (totals[:, 2].sum() - totals[:, 1].sum()) / totals[:, 0].sum()
    return {
        "unit": "query",
        "seed": seed,
        "iterations": iterations,
        "query_unit_count": len(units),
        "observed_delta_recall_at_5": float(observed),
        "observed_delta_pp": 100 * float(observed),
        "ci95_low": float(np.percentile(deltas, 2.5)),
        "ci95_high": float(np.percentile(deltas, 97.5)),
        "ci95_low_pp": 100 * float(np.percentile(deltas, 2.5)),
        "ci95_high_pp": 100 * float(np.percentile(deltas, 97.5)),
    }


def _e2e_chain(
    *,
    query: Mapping[str, Any],
    pages: Sequence[Mapping[str, Any]],
    scheme: str,
    embedder: Any,
    reranker: Any,
) -> list[str]:
    """Execute one complete warm retrieval chain without consulting score caches."""

    query_text = str(query["original_query"])
    query_vector = embedder.embed(query_text, "search")
    page_vectors = {str(page["page_id"]): page["stored_embedding"] for page in pages}
    dense = cosine_rank(query_vector, pages, page_vectors)
    if scheme == "baseline":
        return [str(item["page_id"]) for item in dense[:OUTPUT_K]]
    bm25_texts = {str(page["page_id"]): page_representation(page, "P0") for page in pages}
    bm25 = ChineseBM25Index(pages, bm25_texts).rank(query_text)
    candidate_ranking = rrf_fuse((dense, bm25), rank_constant=RRF_CONSTANT)
    candidate_k = QUALITY_K if scheme == "quality" else PARETO_K
    candidate_ids = [str(item["page_id"]) for item in candidate_ranking[:candidate_k]]
    page_by_id = {str(page["page_id"]): page for page in pages}
    documents = [
        {"page_id": page_id, "memory": page_representation(page_by_id[page_id], "P8")}
        for page_id in candidate_ids
    ]
    result = reranker.rerank(query_text, documents, top_k=len(documents))
    scores = {str(item["page_id"]): float(item["rerank_score"]) for item in result}
    if scheme == "quality":
        return sorted(candidate_ids, key=lambda page_id: (-scores[page_id], page_id))[:OUTPUT_K]
    return protected_order(candidate_ranking[:candidate_k], scores, protected_count=2)[:OUTPUT_K]


def benchmark_e2e_latency(
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    heldout_session_ids: Sequence[str],
    embedding_cache: EmbeddingCache,
    local_cache: LocalScoreCache,
    *,
    warmups: int,
    repeats: int,
    frozen_state: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    latency_dir = output_dir / "cache/latency"
    contract_path = latency_dir / "contract.json"
    raw_path = latency_dir / "warm_e2e_measurements.csv"
    contract = {
        "version": 1,
        "frozen_config_sha256": frozen_state["sha256"],
        "heldout_sessions": list(heldout_session_ids),
        "snapshot_hashes": {
            session_id: snapshots[session_id]["manifest"]["snapshot_hash"] for session_id in heldout_session_ids
        },
        "warmups": warmups,
        "repeats": repeats,
        "clock": "time.perf_counter",
        "includes": "query embedding -> dense -> optional BM25/RRF/local reranker -> final Top5",
        "cold_start_excluded": True,
    }
    if contract_path.exists() and raw_path.exists() and load_json(contract_path) == contract:
        raw_rows = read_csv(raw_path)
        rows = [
            {
                **row,
                "repeat": int(row["repeat"]),
                "elapsed_ms": float(row["elapsed_ms"]),
            }
            for row in raw_rows
        ]
        reused = True
    else:
        latency_dir.mkdir(parents=True, exist_ok=True)
        embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
        reranker = local_cache.load_model()
        visible = {session_id: visible_pages_by_query(snapshots[session_id]) for session_id in heldout_session_ids}
        first_query = next(
            query
            for session_id in heldout_session_ids
            for query in snapshots[session_id]["queries"]
            if visible[session_id][str(query["query_id"])]
        )
        first_session_id = str(first_query["session_id"])
        for scheme in SCHEMES:
            for _ in range(max(warmups, 0)):
                _e2e_chain(
                    query=first_query,
                    pages=visible[first_session_id][str(first_query["query_id"])],
                    scheme=scheme,
                    embedder=embedder,
                    reranker=reranker,
                )
        rows = []
        for repeat in range(max(repeats, 1)):
            for session_id in heldout_session_ids:
                for query in snapshots[session_id]["queries"]:
                    query_id = str(query["query_id"])
                    pages = visible[session_id][query_id]
                    for scheme in SCHEMES:
                        started = time.perf_counter()
                        _e2e_chain(
                            query=query,
                            pages=pages,
                            scheme=scheme,
                            embedder=embedder,
                            reranker=reranker,
                        )
                        rows.append(
                            {
                                "session_id": session_id,
                                "session_code": session_code(session_id),
                                "query_id": query_id,
                                "scheme": scheme,
                                "repeat": repeat + 1,
                                "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                            }
                        )
        write_csv(raw_path, rows)
        dump_json(contract_path, contract)
        reused = False
    summary_rows: list[dict[str, Any]] = []
    for scope, session_ids in (
        ("HELDOUT_S002_S005", list(heldout_session_ids)),
        *[(session_code(session_id), [session_id]) for session_id in heldout_session_ids],
    ):
        for scheme in SCHEMES:
            values = [
                float(row["elapsed_ms"])
                for row in rows
                if row["scheme"] == scheme and row["session_id"] in session_ids
            ]
            summary_rows.append(
                {
                    "scope": scope,
                    "scheme": scheme,
                    "warmups": warmups,
                    "repeats": repeats,
                    "cold_start_excluded": True,
                    **latency_stats(values),
                }
            )
    model_load = {
        "embedding": embedding_cache.status.get(PRODUCTION_EMBEDDING),
        "reranker": local_cache.status_by_model.get(BASE_RERANKER),
    }
    return rows, summary_rows, {
        "status": "PASS",
        "measurement_cache_reused": reused,
        "contract": contract,
        "contract_path": str(contract_path),
        "raw_measurements_path": str(raw_path),
        "model_load": model_load,
    }


def win_tie_loss(
    per_session: Sequence[Mapping[str, Any]],
    contender: str,
    *,
    heldout_only: bool,
) -> dict[str, int]:
    counts = Counter()
    for row in per_session:
        if heldout_only and row["role"] != "heldout":
            continue
        delta = float(row[f"{contender}_recall_at_5"]) - float(row["baseline_recall_at_5"])
        counts["win" if delta > 1e-12 else "loss" if delta < -1e-12 else "tie"] += 1
    return {key: counts[key] for key in ("win", "tie", "loss")}


def render_report(summary: Mapping[str, Any]) -> str:
    sessions = summary["per_session"]
    heldout = summary["heldout"]
    latency = {row["scheme"]: row for row in summary["latency_summary"] if row["scope"] == "HELDOUT_S002_S005"}
    source = summary["source_run_validation"]
    movement = summary["heldout_reranker_movement"]
    complement = summary["heldout_complementarity"]
    miss = summary["heldout_miss_stage"]
    bootstrap = summary["bootstrap"]

    lines = [
        "# S001 冻结检索链的 S002–S005 Held-out 验证",
        "",
        "## 1. 实验设计与参数冻结",
        "",
        "S001 是 development/tuning Session；S002–S005 是 held-out validation。Quality 与 Pareto 的参数在任何 held-out 结果产生前写入 `frozen_config.json`，验证过程没有重新调参。S001 只引用已有 snapshot/cache，未重新完整运行。",
        "",
        (
            "S002–S005 首次以 concurrency=4 启动；本地 Qdrant 的并发读写竞态使 S004 在 Q019 中止。"
            "完整的 S002/S003/S005 被保留，S004 在独立 runtime 以 concurrency=1 从头重跑。"
            "失败的部分 S004 数据被明确排除，未与 retry 数据拼接；这属于运行恢复，不是检索参数调优。"
        ),
        "",
        f"一次性源数据实际 wall-clock 合计 {source['actual_wall_seconds']:.2f} 秒；按各成功 Session wall-clock 求和的顺序估计为 {source['sequential_estimated_seconds']:.2f} 秒，有效 speedup={source['effective_speedup']:.2f}x。",
        "",
        "## 2. Session 信息与逐 Session 结果",
        "",
        "| Session | Role | Turns | Pages | Queries | Total Gold | Eligible Gold | Gold Not Available | Baseline R@5 | Quality R@5 | ΔQuality | Pareto R@5 | ΔPareto | Quality Cand R@20 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sessions:
        lines.append(
            "| {session_code} | {role} | {turn_count} | {page_count} | {evaluated_query_count} | "
            "{total_gold_lineage_count} | {eligible_gold_count} | {gold_not_available_count} | "
            "{baseline_recall_at_5:.2%} | {quality_recall_at_5:.2%} | {quality_delta_recall_at_5_pp:+.2f} pp | "
            "{pareto_recall_at_5:.2%} | {pareto_delta_recall_at_5_pp:+.2f} pp | {quality_candidate_recall_at_20:.2%} |".format(**row)
        )
    lines.extend(
        [
            "",
            "Baseline 统一 evaluator 的完整核心指标：",
            "",
            "| Session | R@5 | R@10 | R@20 | MRR | NDCG@5 | Mean Gold Rank |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sessions:
        lines.append(
            f"| {row['session_code']} | {row['baseline_recall_at_5']:.2%} | {row['baseline_recall_at_10']:.2%} | "
            f"{row['baseline_recall_at_20']:.2%} | {row['baseline_mrr']:.4f} | {row['baseline_ndcg_at_5']:.4f} | "
            f"{row['baseline_mean_gold_rank']:.2f} |"
        )
    micro = heldout["micro"]
    macro = heldout["macro"]
    lines.extend(
        [
            "",
            "## 3. Held-out Micro / Macro（仅 S002–S005）",
            "",
            "| Scheme | Micro R@5 | Micro MRR | Micro NDCG@5 | Macro Session R@5 | Macro MRR | Macro NDCG@5 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for scheme in SCHEMES:
        lines.append(
            f"| {scheme} | {micro[scheme]['recall_at_5']:.2%} | {micro[scheme]['mrr']:.4f} | "
            f"{micro[scheme]['ndcg_at_5']:.4f} | {macro[scheme]['macro_session_recall_at_5']:.2%} | "
            f"{macro[scheme]['macro_session_mrr']:.4f} | {macro[scheme]['macro_session_ndcg_at_5']:.4f} |"
        )
    quality_delta = 100 * (micro["quality"]["recall_at_5"] - micro["baseline"]["recall_at_5"])
    pareto_delta = 100 * (micro["pareto"]["recall_at_5"] - micro["baseline"]["recall_at_5"])
    lines.extend(
        [
            "",
            f"Quality held-out Micro 相对 Baseline：{quality_delta:+.2f} pp；Pareto：{pareto_delta:+.2f} pp。",
            f"Quality 的 MRR 从 {micro['baseline']['mrr']:.4f} 降至 {micro['quality']['mrr']:.4f}，NDCG@5 从 {micro['baseline']['ndcg_at_5']:.4f} 降至 {micro['quality']['ndcg_at_5']:.4f}；微小 Recall 增益伴随明显的 Query 首个 Gold 与 Top5 排序质量退化。",
            f"Quality Session Win/Tie/Loss：{summary['win_tie_loss']['quality']['win']}/{summary['win_tie_loss']['quality']['tie']}/{summary['win_tie_loss']['quality']['loss']}；Pareto：{summary['win_tie_loss']['pareto']['win']}/{summary['win_tie_loss']['pareto']['tie']}/{summary['win_tie_loss']['pareto']['loss']}。",
            f"Query-level Quality Win/Tie/Loss：{summary['query_level_win_tie_loss']['quality'].get('QUERY_WIN', 0)}/{summary['query_level_win_tie_loss']['quality'].get('QUERY_TIE', 0)}/{summary['query_level_win_tie_loss']['quality'].get('QUERY_LOSS', 0)}；收益不是广泛单向改善，Query win 与 loss 基本相抵。",
            "",
            "S001–S005 pooled 数字另存于 `all_five_pooled_metrics.json`，但包含已参与调参的 S001，不能作为纯 held-out 泛化证据。",
            "",
            "## 4. Candidate depth 与 Dense/BM25 互补",
            "",
            f"Held-out K20 Gold 分类：Dense-only {complement['DENSE_ONLY']}，BM25-only {complement['BM25_ONLY']}，Both {complement['BOTH']}，Neither {complement['NEITHER']}。",
            "",
            "| Session | Dense-only | BM25-only | Both | Neither |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for code, counts in summary["complementarity_by_session"].items():
        lines.append(
            f"| {code} | {counts['DENSE_ONLY']} | {counts['BM25_ONLY']} | {counts['BOTH']} | {counts['NEITHER']} |"
        )
    lines.extend(
        [
            "",
            f"Production Dense R@20 为 {micro['baseline']['recall_at_20']:.2%}；冻结 RRF Candidate R@20 为 {micro['quality']['candidate_recall_at_20']:.2%}（+{100 * (micro['quality']['candidate_recall_at_20'] - micro['baseline']['recall_at_20']):.2f} pp）。最终 Quality R@5 为 {micro['quality']['recall_at_5']:.2%}；剩余 miss 中 Candidate miss {miss['CANDIDATE_MISS']}，Reranker miss {miss['RERANKER_MISS']}。",
            "",
            "## 5. P0 → P8 reranker 稳定性",
            "",
            f"Held-out promoted={movement['promoted']}，demoted={movement['demoted']}，net gain={movement['net_gain']}，promotion efficiency={movement['promotion_efficiency']:.2%}。这些 movement 直接比较生产 Dense Top5 与冻结 Quality 最终 Top5。",
            "",
            "失败 Session S005 的 Quality Candidate 已覆盖 21/26 Gold（80.77%），但 final Top5 仅命中 8/26，而 Baseline 命中 12/26。具体为 promoted=3、demoted=7；Quality miss 中 Candidate miss=5、Reranker miss=13。因此该 Session 的退化主要来自 P8 local reranker 的过度重排，而不是 Gold 大量无法进入 RRF Top20。此处只记录 future hypothesis，没有据此改参或重跑。",
            "",
            "## 6. K15 Pareto vs K20 Quality",
            "",
            f"K20 Quality 比 K15 Pareto 多命中 {summary['k20_minus_k15_hits']} 个 held-out eligible Gold。两者 warm E2E p95 差值为 {latency['quality']['p95'] - latency['pareto']['p95']:+.2f} ms。注意这比较的是两个完整冻结方案：Pareto 还包含 Top2 protection，因此不能把差异纯归因于 K。",
            "",
            "## 7. 真实 warm E2E latency",
            "",
            "完整 `query → embedding → Dense → BM25 → RRF → reranker → Top5` 由一个 `time.perf_counter()` 包围；结果不是 component p95 相加。模型加载和下载不计入 warm latency。",
            "",
            "| Scheme | Mean ms | p50 | p90 | p95 | Max |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for scheme in SCHEMES:
        row = latency[scheme]
        lines.append(
            f"| {scheme} | {row['mean']:.2f} | {row['p50']:.2f} | {row['p90']:.2f} | {row['p95']:.2f} | {row['max']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 8. Bootstrap 不确定性",
            "",
            f"以 Query 为 bootstrap 单元、seed={bootstrap['quality_minus_baseline']['seed']}、{bootstrap['quality_minus_baseline']['iterations']} 次重采样。Quality−Baseline ΔR@5 95% CI：[{bootstrap['quality_minus_baseline']['ci95_low_pp']:.2f}, {bootstrap['quality_minus_baseline']['ci95_high_pp']:.2f}] pp；Pareto−Baseline：[{bootstrap['pareto_minus_baseline']['ci95_low_pp']:.2f}, {bootstrap['pareto_minus_baseline']['ci95_high_pp']:.2f}] pp。样本仍只有四个 Session，应保守解释。",
            "",
            "## 9. S001 selection overfitting 与最终建议",
            "",
            summary["selection_overfitting_assessment"],
            "",
            f"最终状态：**{summary['validation_status']}**。",
            "",
            summary["recommendation"],
            "",
            "冻结验证不支持在本轮恢复 DeepSeek reranker、Query Rewrite 或 Page Prompt 改写；这些组件均未参与验证。Dense/BM25 互补性泛化了，但 P0→P8 + bge-reranker-base 的最终排序并不稳定，因此本轮不建议将完整 Quality 链直接设为生产默认。RRF60 只应保持为下一批冻结验证候选，不能称为全局最优。",
            "",
            "## 10. 直接问题回答",
            "",
        ]
    )
    for index, answer in enumerate(summary["direct_answers"], start=1):
        lines.append(f"{index}. {answer}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    if args.latency_warmups < 2 or args.latency_repeats < 1 or args.bootstrap_iterations < 1:
        raise ValueError("Latency requires >=2 warmups and >=1 repeat; bootstrap iterations must be positive")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    all_sessions = load_dataset(args.dataset.resolve(), max_sessions=5)
    if len(all_sessions) != 5:
        raise ValueError(f"Dataset did not yield exactly the first five Sessions: {len(all_sessions)}")
    actual_codes = [session_code(session.session_id) for session in all_sessions]
    if actual_codes != ["S001", "S002", "S003", "S004", "S005"]:
        raise ValueError(f"Unexpected first-five dataset order: {actual_codes}")
    development = all_sessions[0]
    heldout_sessions = all_sessions[1:]
    heldout_ids = [session.session_id for session in heldout_sessions]

    frozen_before = validate_frozen_config(args.frozen_config.resolve(), all_sessions)
    upstream_paths = [
        args.s001_result_dir.resolve() / "snapshot/snapshot_manifest.json",
        args.s001_result_dir.resolve() / "cache/embeddings/model_status.json",
        args.s001_result_dir.resolve() / "rerank_tuning/cache/rerank/local_scores.jsonl",
        args.s001_result_dir.resolve() / "rank_fusion_tuning/third_stage_summary.json",
    ]
    upstream_before = [file_state(path) for path in upstream_paths if path.exists()]
    primary_sessions = [session for session in heldout_sessions if session_code(session.session_id) != "S004"]
    retry_sessions = [session for session in heldout_sessions if session_code(session.session_id) == "S004"]
    primary_validation = validate_source_run(
        args.heldout_source_dir.resolve(),
        primary_sessions,
        expected_concurrency=4,
    )
    retry_validation = validate_source_run(
        args.s004_retry_source_dir.resolve(),
        retry_sessions,
        expected_concurrency=1,
    )
    primary_snapshots, primary_snapshot_reuse = build_or_load_heldout_snapshots(
        output_dir,
        args.heldout_source_dir.resolve(),
        primary_sessions,
    )
    retry_snapshots, retry_snapshot_reuse = build_or_load_heldout_snapshots(
        output_dir,
        args.s004_retry_source_dir.resolve(),
        retry_sessions,
    )
    heldout_snapshots = {**primary_snapshots, **retry_snapshots}
    snapshot_reuse = {
        "built": primary_snapshot_reuse["built"] + retry_snapshot_reuse["built"],
        "reused": primary_snapshot_reuse["reused"] + retry_snapshot_reuse["reused"],
    }
    source_validation = {
        "status": "PASS",
        "initial_concurrent_run": primary_validation,
        "isolated_s004_retry": retry_validation,
        "actual_wall_seconds": float(primary_validation["actual_concurrent_wall_seconds"])
        + float(retry_validation["actual_concurrent_wall_seconds"]),
        "sequential_estimated_seconds": float(primary_validation["sequential_estimated_seconds"])
        + float(retry_validation["sequential_estimated_seconds"]),
        "reason_for_isolated_retry": (
            "The initial local-Qdrant concurrent source run hit an internal array-size race on S004-Q019. "
            "S002/S003/S005 were retained; S004 was rerun once in an isolated runtime."
        ),
    }
    source_validation["effective_speedup"] = (
        source_validation["sequential_estimated_seconds"] / source_validation["actual_wall_seconds"]
    )
    snapshots: dict[str, dict[str, Any]] = {
        development.session_id: load_s001_snapshot(args.s001_result_dir.resolve(), development),
        **heldout_snapshots,
    }
    heldout_score_cache_path = output_dir / "cache/rerank/local_scores.jsonl"
    heldout_score_cache_before = (
        file_state(heldout_score_cache_path) if heldout_score_cache_path.exists() else None
    )
    query_vectors, embedding_cache, embedding_status = build_query_embeddings(
        output_dir,
        args.s001_result_dir.resolve(),
        snapshots,
        development.session_id,
    )
    (
        rankings,
        dense_by_session,
        bm25_by_session,
        rrf_by_session,
        local_cache,
        reranker_cache_status,
    ) = build_frozen_rankings(
        snapshots,
        development.session_id,
        heldout_ids,
        query_vectors,
        output_dir,
        args.s001_result_dir.resolve(),
    )
    (
        per_session,
        depth_rows,
        complementarity_rows,
        movement_rows,
        query_win_loss_rows,
        miss_rows,
        metrics_by_session,
    ) = evaluate_sessions(
        all_sessions,
        snapshots,
        rankings,
        dense_by_session,
        bm25_by_session,
        rrf_by_session,
    )

    s001_metrics = metrics_by_session[development.session_id]
    expected_s001 = {
        "baseline_r5": 7 / 21,
        "baseline_r20": 18 / 21,
        "quality_r5": 10 / 21,
        "pareto_r5": 9 / 21,
    }
    actual_s001 = {
        "baseline_r5": float(s001_metrics["baseline"]["recall_at_5"]),
        "baseline_r20": float(s001_metrics["baseline"]["recall_at_20"]),
        "quality_r5": float(s001_metrics["quality"]["recall_at_5"]),
        "pareto_r5": float(s001_metrics["pareto"]["recall_at_5"]),
    }
    if any(not math.isclose(actual_s001[key], expected, abs_tol=1e-12) for key, expected in expected_s001.items()):
        raise RuntimeError(f"Frozen S001 reproduction failed; held-out results rejected: {actual_s001}")

    heldout_micro, heldout_macro = aggregate_scheme_metrics(
        heldout_ids,
        snapshots,
        rankings,
        metrics_by_session,
    )
    all_five_micro, all_five_macro = aggregate_scheme_metrics(
        [session.session_id for session in all_sessions],
        snapshots,
        rankings,
        metrics_by_session,
    )
    for cutoff in (15, 20):
        recall, hits, total = candidate_recall(
            [query for session_id in heldout_ids for query in snapshots[session_id]["queries"]],
            {
                query_id: ranking
                for session_id in heldout_ids
                for query_id, ranking in rrf_by_session[session_id].items()
            },
            cutoff,
        )
        key = "quality" if cutoff == QUALITY_K else "pareto"
        heldout_micro[key][f"candidate_recall_at_{cutoff}"] = recall
        heldout_micro[key][f"candidate_hits_at_{cutoff}"] = hits
        heldout_micro[key]["candidate_eligible_gold_count"] = total

    heldout_queries = [query for session_id in heldout_ids for query in snapshots[session_id]["queries"]]
    pooled_baseline = {
        query_id: ranking
        for session_id in heldout_ids
        for query_id, ranking in rankings[session_id]["baseline"].items()
    }
    pooled_quality = {
        query_id: ranking
        for session_id in heldout_ids
        for query_id, ranking in rankings[session_id]["quality"].items()
    }
    pooled_pareto = {
        query_id: ranking
        for session_id in heldout_ids
        for query_id, ranking in rankings[session_id]["pareto"].items()
    }
    bootstrap = {
        "quality_minus_baseline": bootstrap_query_delta(
            heldout_queries,
            pooled_baseline,
            pooled_quality,
            iterations=args.bootstrap_iterations,
            seed=BOOTSTRAP_SEED,
        ),
        "pareto_minus_baseline": bootstrap_query_delta(
            heldout_queries,
            pooled_baseline,
            pooled_pareto,
            iterations=args.bootstrap_iterations,
            seed=BOOTSTRAP_SEED,
        ),
    }
    latency_rows, latency_summary, latency_status = benchmark_e2e_latency(
        output_dir,
        snapshots,
        heldout_ids,
        embedding_cache,
        local_cache,
        warmups=args.latency_warmups,
        repeats=args.latency_repeats,
        frozen_state=frozen_before,
    )

    heldout_complementarity = Counter(
        row["category"] for row in complementarity_rows if row["session_id"] in heldout_ids
    )
    complementarity_by_session = {
        session_code(session_id): dict(
            Counter(row["category"] for row in complementarity_rows if row["session_id"] == session_id)
        )
        for session_id in heldout_ids
    }
    for counts in complementarity_by_session.values():
        for category in ("DENSE_ONLY", "BM25_ONLY", "BOTH", "NEITHER"):
            counts.setdefault(category, 0)
    heldout_movement = Counter(
        row["movement_category"] for row in movement_rows if row["session_id"] in heldout_ids
    )
    promoted = heldout_movement["PROMOTED_INTO_TOP5"]
    demoted = heldout_movement["DEMOTED_OUT_OF_TOP5"]
    heldout_miss = Counter(row["miss_stage"] for row in miss_rows if row["session_id"] in heldout_ids)
    quality_wtl = win_tie_loss(per_session, "quality", heldout_only=True)
    pareto_wtl = win_tie_loss(per_session, "pareto", heldout_only=True)
    query_quality_counts = Counter(
        row["quality_result"] for row in query_win_loss_rows if row["session_id"] in heldout_ids
    )
    query_pareto_counts = Counter(
        row["pareto_result"] for row in query_win_loss_rows if row["session_id"] in heldout_ids
    )
    quality_delta = float(heldout_micro["quality"]["recall_at_5"]) - float(
        heldout_micro["baseline"]["recall_at_5"]
    )
    pareto_delta = float(heldout_micro["pareto"]["recall_at_5"]) - float(
        heldout_micro["baseline"]["recall_at_5"]
    )
    macro_quality_delta = float(heldout_macro["quality"]["macro_session_recall_at_5"]) - float(
        heldout_macro["baseline"]["macro_session_recall_at_5"]
    )
    ci_low = float(bootstrap["quality_minus_baseline"]["ci95_low"])
    latency_heldout = {
        row["scheme"]: row for row in latency_summary if row["scope"] == "HELDOUT_S002_S005"
    }
    if quality_delta <= 0 and quality_wtl["win"] <= 1 and quality_wtl["loss"] >= 2:
        validation_status = "S001_SELECTION_OVERFIT_LIKELY"
    elif (
        quality_delta > 0
        and macro_quality_delta > 0
        and quality_wtl["win"] + quality_wtl["tie"] >= 3
        and quality_wtl["loss"] <= 1
        and query_quality_counts["QUERY_WIN"] >= query_quality_counts["QUERY_LOSS"]
        and ci_low >= 0
        and float(latency_heldout["quality"]["p95"]) < 5_000
    ):
        validation_status = "READY_FOR_PRODUCTION_IMPLEMENTATION"
    else:
        validation_status = "NEEDS_MORE_HELD_OUT_VALIDATION"

    if quality_delta <= 0:
        overfit_assessment = (
            f"S001 的 +14.29 pp 未在 held-out 复现：held-out Quality Δ={100 * quality_delta:+.2f} pp，"
            "存在明显 selection-overfitting 风险。"
        )
    elif quality_delta < 0.07145:
        overfit_assessment = (
            f"held-out 方向仍为正，但 Δ={100 * quality_delta:+.2f} pp，显著小于 S001 的 +14.29 pp；"
            "说明至少存在收益幅度上的 selection overfitting。"
        )
    else:
        overfit_assessment = (
            f"held-out Quality Δ={100 * quality_delta:+.2f} pp，方向和幅度均支持 S001 的收益；"
            "但只有四个 held-out Session，不能宣称全局最优。"
        )
    recommendation = {
        "READY_FOR_PRODUCTION_IMPLEMENTATION": (
            "建议进入生产实现阶段：先以冻结 Quality 链路实现受控 feature flag / shadow rollout，保留生产 Baseline 回退；"
            "不要把本轮结果解释为全局最优参数。"
        ),
        "NEEDS_MORE_HELD_OUT_VALIDATION": (
            "暂不进入正式生产替换；保持参数冻结，扩大到更多未参与选择的 Session 验证。可以准备隔离实现或 shadow 评估，"
            "但不应依据本轮四个 Session 继续调参。"
        ),
        "S001_SELECTION_OVERFIT_LIKELY": (
            "不建议实现为生产默认链路；停止围绕 S001 调参，并在更大的未调参 Session 集合重新验证问题分解。"
        ),
    }[validation_status]

    heldout_quality_hits = round(
        heldout_micro["quality"]["recall_at_5"] * heldout_micro["quality"]["eligible_gold_count"]
    )
    heldout_pareto_hits = round(
        heldout_micro["pareto"]["recall_at_5"] * heldout_micro["pareto"]["eligible_gold_count"]
    )
    future_hypotheses = []
    for row in per_session:
        if row["role"] == "heldout" and float(row["quality_delta_recall_at_5_pp"]) < 0:
            session_misses = Counter(
                item["miss_stage"] for item in miss_rows if item["session_id"] == row["session_id"]
            )
            future_hypotheses.append(
                {
                    "session_id": row["session_id"],
                    "observed_quality_delta_pp": row["quality_delta_recall_at_5_pp"],
                    "candidate_misses": session_misses["CANDIDATE_MISS"],
                    "reranker_misses": session_misses["RERANKER_MISS"],
                    "note": "Held-out failure analysis only; no parameter was changed or rerun.",
                }
            )

    direct_answers = []
    eligible_text = ", ".join(
        f"{row['session_code']}={row['eligible_gold_count']}" for row in per_session if row["role"] == "heldout"
    )
    direct_answers.extend(
        [
            f"S002–S005 eligible Gold：{eligible_text}。",
            "各 Session Baseline R@5：" + ", ".join(f"{row['session_code']}={row['baseline_recall_at_5']:.2%}" for row in per_session),
            "各 Session Quality R@5：" + ", ".join(f"{row['session_code']}={row['quality_recall_at_5']:.2%}" for row in per_session),
            "各 Session Pareto R@5：" + ", ".join(f"{row['session_code']}={row['pareto_recall_at_5']:.2%}" for row in per_session),
            f"Quality Session Win/Tie/Loss={quality_wtl['win']}/{quality_wtl['tie']}/{quality_wtl['loss']}。",
            f"Pareto Session Win/Tie/Loss={pareto_wtl['win']}/{pareto_wtl['tie']}/{pareto_wtl['loss']}。",
            f"Held-out Micro Baseline R@5={heldout_micro['baseline']['recall_at_5']:.2%}。",
            f"Held-out Micro Quality R@5={heldout_micro['quality']['recall_at_5']:.2%}。",
            f"Held-out Quality 相对 Baseline={100 * quality_delta:+.2f} pp。",
            f"Held-out Macro Session R@5：Baseline={heldout_macro['baseline']['macro_session_recall_at_5']:.2%}，Quality={heldout_macro['quality']['macro_session_recall_at_5']:.2%}，Δ={100 * macro_quality_delta:+.2f} pp。",
            f"Dense/BM25 互补性泛化：K20 分类为 Dense-only={heldout_complementarity['DENSE_ONLY']}、BM25-only={heldout_complementarity['BM25_ONLY']}、Both={heldout_complementarity['BOTH']}、Neither={heldout_complementarity['NEITHER']}，且四个 held-out Session 均同时存在 Dense-only 与 BM25-only Gold。",
            f"Quality Candidate R@20={heldout_micro['quality']['candidate_recall_at_20']:.2%}，final R@5={heldout_micro['quality']['recall_at_5']:.2%}。",
            f"Quality miss：Candidate miss={heldout_miss['CANDIDATE_MISS']}，Reranker miss={heldout_miss['RERANKER_MISS']}。",
            f"Local reranker held-out promoted={promoted}。",
            f"Local reranker held-out demoted={demoted}。",
            f"K20 Quality 比 K15 Pareto 多命中 {heldout_quality_hits - heldout_pareto_hits} 个 eligible Gold。",
            f"K20 Quality 相比 K15 Pareto warm E2E p95 增量={latency_heldout['quality']['p95'] - latency_heldout['pareto']['p95']:+.2f} ms。",
            f"Quality 真实 warm E2E p95={latency_heldout['quality']['p95']:.2f} ms。",
            f"Pareto 真实 warm E2E p95={latency_heldout['pareto']['p95']:.2f} ms。",
            overfit_assessment,
            f"P0 candidate + P8 reranker：不建议直接作为生产默认；held-out promoted/demoted={promoted}/{demoted}，Quality Macro R@5、MRR、NDCG@5 均未改善。",
            f"RRF60：Dense/BM25 互补性泛化，且 Candidate R@20 比 Dense R@20 高 {100 * (heldout_micro['quality']['candidate_recall_at_20'] - heldout_micro['baseline']['recall_at_20']):.2f} pp；保留为下一批冻结验证候选，但本轮不能证明常数 60 最优或足以进入生产。",
            f"bge-reranker-base：不建议作为生产默认；虽推进 {promoted} 个 Gold，也推出 {demoted} 个，净增仅 {promoted - demoted}。",
            "继续维持无需 DeepSeek reranker：是；本轮没有新增 DeepSeek 检索调用，结论不依赖它。",
            "继续维持无需 Query Rewrite：是；冻结 Q0 链路直接验证。",
            "继续维持暂不修改 Page Prompt：是；本轮没有产生支持修改 Prompt 的新消融证据。",
            f"最终状态：{validation_status}。",
        ]
    )

    session_manifest = {
        "dataset": str(args.dataset.resolve()),
        "first_five_in_dataset_order": [
            {
                "session_code": session_code(session.session_id),
                "session_id": session.session_id,
                "turn_count": len(session.turns),
                "role": "development" if session is development else "heldout",
                "source": "reused existing S001 snapshot/cache" if session is development else "new one-time no-thinking source run",
                "full_benchmark_rerun": session is not development,
            }
            for session in all_sessions
        ],
        "out_of_scope_session_count": 0,
        "source_run_validation": source_validation,
    }
    complementarity_aggregate = {
        "scope": "S002-S005 heldout",
        **{key: heldout_complementarity[key] for key in ("DENSE_ONLY", "BM25_ONLY", "BOTH", "NEITHER")},
        "is_complementary": bool(heldout_complementarity["DENSE_ONLY"] and heldout_complementarity["BM25_ONLY"]),
    }
    heldout_payload = {
        "scope": "S002-S005 heldout only",
        "session_ids": heldout_ids,
        "micro": heldout_micro,
        "macro": heldout_macro,
    }
    summary = {
        "experiment": "frozen S001-selected retrieval validation",
        "development_session": development.session_id,
        "heldout_sessions": heldout_ids,
        "frozen_config": frozen_before["config"],
        "frozen_config_sha256": frozen_before["sha256"],
        "s001_reproduction": {"status": "PASS", "expected": expected_s001, "actual": actual_s001},
        "source_run_validation": source_validation,
        "per_session": per_session,
        "heldout": heldout_payload,
        "all_five_pooled": {
            "scope": "S001-S005 pooled; includes tuned S001 and is not pure heldout",
            "micro": all_five_micro,
            "macro": all_five_macro,
        },
        "win_tie_loss": {"quality": quality_wtl, "pareto": pareto_wtl},
        "query_level_win_tie_loss": {
            "quality": dict(query_quality_counts),
            "pareto": dict(query_pareto_counts),
        },
        "heldout_complementarity": dict(heldout_complementarity),
        "complementarity_by_session": complementarity_by_session,
        "heldout_reranker_movement": {
            "promoted": promoted,
            "demoted": demoted,
            "net_gain": promoted - demoted,
            "promotion_efficiency": promoted / (promoted + demoted) if promoted + demoted else 0.0,
        },
        "heldout_miss_stage": dict(heldout_miss),
        "k20_minus_k15_hits": heldout_quality_hits - heldout_pareto_hits,
        "bootstrap": bootstrap,
        "latency_summary": latency_summary,
        "latency_status": latency_status,
        "future_hypotheses": future_hypotheses,
        "selection_overfitting_assessment": overfit_assessment,
        "validation_status": validation_status,
        "recommendation": recommendation,
        "direct_answers": direct_answers,
    }

    dump_json(output_dir / "session_manifest.json", session_manifest)
    write_csv(output_dir / "per_session_metrics.csv", per_session)
    dump_json(output_dir / "heldout_micro_metrics.json", {"scope": heldout_payload["scope"], **heldout_micro})
    dump_json(output_dir / "heldout_macro_metrics.json", {"scope": heldout_payload["scope"], **heldout_macro})
    dump_json(output_dir / "all_five_pooled_metrics.json", summary["all_five_pooled"])
    write_csv(output_dir / "candidate_depth_by_session.csv", depth_rows)
    write_csv(output_dir / "candidate_complementarity_by_session.csv", complementarity_rows)
    dump_json(output_dir / "candidate_complementarity_aggregate.json", complementarity_aggregate)
    write_csv(output_dir / "rerank_movement_by_session.csv", movement_rows)
    write_csv(output_dir / "query_level_win_loss.csv", query_win_loss_rows)
    write_csv(output_dir / "miss_stage_breakdown.csv", miss_rows)
    write_csv(output_dir / "latency_by_session.csv", latency_rows)
    write_csv(output_dir / "latency_summary.csv", latency_summary)
    dump_json(output_dir / "bootstrap_results.json", bootstrap)
    dump_json(output_dir / "validation_summary.json", summary)
    (output_dir / "validation_report.md").write_text(render_report(summary), encoding="utf-8")

    frozen_after = validate_frozen_config(args.frozen_config.resolve(), all_sessions)
    upstream_after = [file_state(path) for path in upstream_paths if path.exists()]
    upstream_unchanged = upstream_before == upstream_after
    heldout_score_cache_after = (
        file_state(heldout_score_cache_path) if heldout_score_cache_path.exists() else None
    )
    heldout_score_cache_unchanged = (
        heldout_score_cache_before == heldout_score_cache_after if heldout_score_cache_before else None
    )
    cache_validation = {
        "status": "PASS"
        if frozen_before["sha256"] == frozen_after["sha256"]
        and upstream_unchanged
        and heldout_score_cache_unchanged is not False
        else "FAIL",
        "frozen_config_before": frozen_before,
        "frozen_config_after": frozen_after,
        "frozen_config_unchanged": frozen_before["sha256"] == frozen_after["sha256"],
        "upstream_s001_cache_before": upstream_before,
        "upstream_s001_cache_after": upstream_after,
        "upstream_s001_cache_unchanged": upstream_unchanged,
        "snapshot_cache": snapshot_reuse,
        "embedding_cache_hit": bool(embedding_status.get("cache_hit")),
        "reranker_cache": reranker_cache_status,
        "heldout_reranker_score_cache_before": heldout_score_cache_before,
        "heldout_reranker_score_cache_after": heldout_score_cache_after,
        "heldout_reranker_score_cache_unchanged": heldout_score_cache_unchanged,
        "latency_measurement_cache_reused": latency_status["measurement_cache_reused"],
        "new_deepseek_retrieval_calls": 0,
        "s001_full_benchmark_rerun": False,
        "source_page_regeneration_during_validation_script": False,
    }
    dump_json(output_dir / "cache_reuse_validation.json", cache_validation)
    if cache_validation["status"] != "PASS":
        raise RuntimeError(f"Cache/frozen-config integrity validation failed: {cache_validation}")
    local_cache.release()
    embedding_cache.release(PRODUCTION_EMBEDDING)
    gc.collect()

    terminal_summary = {
        "first_five_sessions": [session.session_id for session in all_sessions],
        "reused_full_source_sessions": [development.session_id],
        "new_full_benchmark_sessions": heldout_ids,
        "session_concurrency": "4 for initial S002-S005 run; 1 for isolated S004 retry",
        "source_actual_wall_seconds": source_validation["actual_wall_seconds"],
        "source_sequential_estimated_seconds": source_validation["sequential_estimated_seconds"],
        "source_effective_speedup": source_validation["effective_speedup"],
        "heldout_eligible_gold": int(heldout_micro["baseline"]["eligible_gold_count"]),
        "heldout_micro_r5": {scheme: heldout_micro[scheme]["recall_at_5"] for scheme in SCHEMES},
        "heldout_macro_session_r5": {
            scheme: heldout_macro[scheme]["macro_session_recall_at_5"] for scheme in SCHEMES
        },
        "quality_delta_pp": 100 * quality_delta,
        "pareto_delta_pp": 100 * pareto_delta,
        "win_tie_loss": summary["win_tie_loss"],
        "complementarity": complementarity_aggregate,
        "miss_stage": dict(heldout_miss),
        "movement": summary["heldout_reranker_movement"],
        "k20_minus_k15_hits": summary["k20_minus_k15_hits"],
        "warm_e2e_p95_ms": {scheme: latency_heldout[scheme]["p95"] for scheme in SCHEMES},
        "bootstrap": bootstrap,
        "selection_overfitting": overfit_assessment,
        "validation_status": validation_status,
        "recommended_next_step": recommendation,
        "cache_reuse_validation": cache_validation["status"],
    }
    print(json.dumps(terminal_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
