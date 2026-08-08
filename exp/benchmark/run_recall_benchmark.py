from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import (
    AsyncJsonlWriter,
    LineageTracker,
    dependency_distances,
    dump_json,
    ensure_repo_root_on_path,
    extract_retrieval_layers,
    is_long_range_turn,
    latency_summary,
    load_dataset,
    load_json,
    mean_or_none,
    prepare_runtime_config,
    redact_secrets,
    resolve_path,
    retrieval_metrics,
    safe_git_commit,
    wait_for_migration_jobs,
    write_csv,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))
from exp.benchmark.benchmark_memory import create_benchmark_memory  # noqa: E402

LOGGER = logging.getLogger("recall_benchmark")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="异步短中长期记忆召回率评测")
    parser.add_argument(
        "--config",
        default="mem0/exp/benchmark/recall_benchmark.json",
        help="评测配置 JSON，路径相对于仓库根目录",
    )
    return parser.parse_args()


def flatten_metric_record(prefix: str, metrics: dict[str, Any], target: dict[str, Any]) -> None:
    for key, value in metrics.items():
        target[f"{prefix}_{key}"] = value


def compact_retrieved_memories(context: dict[str, Any], *, text_limit: int = 2000) -> list[dict[str, Any]]:
    """保留失败案例诊断所需字段，避免把超长回答完整复制到结果目录。"""
    compact: list[dict[str, Any]] = []
    for item in context.get("retrieved_memories") or []:
        if not isinstance(item, dict):
            continue
        row = {
            key: item.get(key)
            for key in (
                "id",
                "source",
                "score",
                "created_at",
                "source_job_id",
                "source_job_ids",
            )
            if item.get(key) not in (None, "", [])
        }
        metadata = item.get("metadata")
        if isinstance(metadata, dict):
            row["metadata"] = {
                key: metadata.get(key)
                for key in ("source_job_id", "source_job_ids", "source_stage", "run_id", "user_id")
                if metadata.get(key) not in (None, "", [])
            }
        for key in ("summary", "memory", "raw_dialogue"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                row[key] = text if len(text) <= text_limit else f"{text[:text_limit]}...[truncated]"
        compact.append(row)
    return compact


def build_summary(records: list[dict[str, Any]], config: dict[str, Any], effective: dict[str, Any]) -> dict[str, Any]:
    evaluated = [record for record in records if record.get("evaluated")]
    successful = [record for record in records if not record.get("error")]
    evaluated_successful = [record for record in evaluated if not record.get("error")]
    summary: dict[str, Any] = {
        "total_turns": len(records),
        "successful_turns": len(successful),
        "failed_turns": len(records) - len(successful),
        "evaluated_turns": len(evaluated),
        "evaluated_successful_turns": len(evaluated_successful),
        "effective": effective,
        "build_total_ms": latency_summary([record.get("build_total_ms") for record in successful]),
        "retrieve_context_ms": latency_summary([record.get("retrieve_context_ms") for record in successful]),
        "add_submit_ms": latency_summary([record.get("add_submit_ms") for record in successful]),
    }
    for layer in ("short", "mid_page", "mid_session", "mid", "long", "mid_long", "all"):
        layer_rows = [record for record in evaluated_successful if record.get(f"{layer}_recall") is not None]
        summary[layer] = {
            "recall": mean_or_none([record[f"{layer}_recall"] for record in layer_rows]),
            "precision": mean_or_none([record[f"{layer}_precision"] for record in layer_rows]),
            "hit_rate": mean_or_none([record[f"{layer}_hit"] for record in layer_rows]),
            "full_dependency_recall": mean_or_none(
                [record[f"{layer}_full_recall"] for record in layer_rows]
            ),
            "mrr": mean_or_none([record[f"{layer}_mrr"] for record in layer_rows]),
        }

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in evaluated_successful:
        grouped[str(record.get("dependency_type") or "未分类")].append(record)
    summary["by_dependency_type"] = {
        key: {
            "count": len(rows),
            "mid_long_recall": mean_or_none([row["mid_long_recall"] for row in rows]),
            "mid_long_precision": mean_or_none([row["mid_long_precision"] for row in rows]),
            "all_recall": mean_or_none([row["all_recall"] for row in rows]),
        }
        for key, rows in sorted(grouped.items())
    }
    summary["benchmark_config"] = redact_secrets(config)
    return summary


async def run() -> None:
    args = parse_args()
    config_path = resolve_path(REPO_ROOT, args.config)
    config = load_json(config_path)

    benchmark_cfg = config.get("benchmark") or {}
    dataset_cfg = config.get("dataset") or {}
    storage_cfg = config.get("storage") or {}
    execution_cfg = config.get("execution") or {}
    memory_cfg = config.get("memory") or {}
    retrieval_cfg = config.get("retrieval") or {}
    evaluation_cfg = config.get("evaluation") or {}

    output_dir = resolve_path(REPO_ROOT, benchmark_cfg.get("output_dir", "mem0/exp/results/recall_benchmark"))
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = resolve_path(REPO_ROOT, storage_cfg.get("runtime_dir", "mem0/exp/runtime/recall_benchmark"))
    base_memory_config_path = resolve_path(REPO_ROOT, memory_cfg["config_path"])
    dataset_path = resolve_path(REPO_ROOT, dataset_cfg["path"])

    sessions = load_dataset(
        dataset_path,
        include_sheets=dataset_cfg.get("include_sheets") or None,
        max_sessions=dataset_cfg.get("max_sessions"),
        max_turns_per_session=dataset_cfg.get("max_turns_per_session"),
    )
    base_memory_config = load_json(base_memory_config_path)
    effective_memory_config = prepare_runtime_config(
        base_memory_config,
        runtime_dir=runtime_dir,
        collection_name=str(storage_cfg.get("collection_name", "recall_benchmark")),
        reset_storage=bool(benchmark_cfg.get("reset_storage", True)),
        agentic_retrieval_enabled=False,
        profile_enabled=bool(memory_cfg.get("profile_enabled", False)),
        profile_update_on_add=bool(memory_cfg.get("profile_update_on_add", False)),
    )

    memory = create_benchmark_memory(
        effective_memory_config,
        llm_mode=str(memory_cfg.get("llm_mode", "real")),
    )
    short_term_message_capacity = int(memory._short_term_capacity())
    short_term_qa_capacity = short_term_message_capacity // 2
    lineage = LineageTracker(short_term_qa_capacity)

    session_concurrency = int(execution_cfg.get("session_concurrency", 50))
    retrieval_timeout = float(execution_cfg.get("retrieval_timeout_seconds", 120))
    add_timeout = float(execution_cfg.get("add_timeout_seconds", 120))
    job_timeout = float(execution_cfg.get("background_job_timeout_seconds", 900))
    poll_interval = float(execution_cfg.get("background_poll_interval_seconds", 0.2))
    wait_strategy = str(execution_cfg.get("wait_strategy", "before_evaluation"))
    if wait_strategy not in {"every_turn", "before_evaluation", "session_end"}:
        raise ValueError("execution.wait_strategy 只能是 every_turn、before_evaluation 或 session_end")

    evaluate_only_long_range = bool(evaluation_cfg.get("evaluate_only_long_range", True))
    infer = bool(memory_cfg.get("infer", True))
    top_k = int(retrieval_cfg.get("top_k", 20))
    threshold = float(retrieval_cfg.get("threshold", 0.1))
    rerank = bool(retrieval_cfg.get("rerank", False))
    semaphore = asyncio.Semaphore(session_concurrency)
    records: list[dict[str, Any]] = []
    records_lock = asyncio.Lock()
    session_wall_clock_seconds: dict[str, float] = {}
    raw_writer = AsyncJsonlWriter(output_dir / "recall_turn_results.jsonl")
    failure_writer = AsyncJsonlWriter(output_dir / "recall_failures.jsonl")
    started_at = time.time()

    async def wait_pending(job_ids: list[str]) -> None:
        if not job_ids:
            return
        await wait_for_migration_jobs(
            memory,
            job_ids,
            timeout_seconds=job_timeout,
            poll_interval_seconds=poll_interval,
        )
        job_ids.clear()

    async def run_session(session) -> None:
        async with semaphore:
            session_started = time.perf_counter()
            pending_migration_jobs: list[str] = []
            user_id = f"recall::{session.session_id}"
            try:
                for turn in session.turns:
                    long_range = is_long_range_turn(session, turn, short_term_qa_capacity)
                    evaluated = bool(
                        turn.needs_history
                        and turn.dependency_turn_ids
                        and (long_range or not evaluate_only_long_range)
                    )
                    if evaluated and wait_strategy == "before_evaluation":
                        await wait_pending(pending_migration_jobs)

                    record: dict[str, Any] = {
                        "session_id": session.session_id,
                        "sheet_name": session.sheet_name,
                        "turn_id": turn.turn_id,
                        "turn_index": turn.turn_index,
                        "needs_history": turn.needs_history,
                        "evaluated": evaluated,
                        "long_range": long_range,
                        "dependency_type": turn.dependency_type,
                        "max_lookback": turn.max_lookback,
                        "dependency_distances": dependency_distances(session, turn),
                        "gold_turn_ids": list(turn.dependency_turn_ids),
                        "required_context": turn.required_context,
                        "error": None,
                    }
                    try:
                        trace = await asyncio.wait_for(
                            memory.build_agent_answer_messages_with_trace(
                                turn.question,
                                user_id=user_id,
                                session_id=session.session_id,
                                top_k=top_k,
                                threshold=threshold,
                                rerank=rerank,
                                explain=False,
                                include_profile_metadata=False,
                            ),
                            timeout=retrieval_timeout,
                        )
                        record["build_total_ms"] = trace["build_total_ms"]
                        record["retrieve_context_ms"] = trace["retrieve_context_ms"]

                        layers = extract_retrieval_layers(trace["retrieved_context"], lineage)
                        current_prefix = turn.turn_id.split("-", 1)[0]
                        current_number = int(turn.turn_id.rsplit("Q", 1)[-1])
                        all_retrieved = layers["all"]
                        record["cross_session_leak_turn_ids"] = [
                            item for item in all_retrieved if item.split("-", 1)[0] != current_prefix
                        ]
                        record["future_turn_leak_turn_ids"] = [
                            item
                            for item in all_retrieved
                            if item.split("-", 1)[0] == current_prefix
                            and int(item.rsplit("Q", 1)[-1]) >= current_number
                        ]
                        record["cross_session_leak_count"] = len(record["cross_session_leak_turn_ids"])
                        record["future_turn_leak_count"] = len(record["future_turn_leak_turn_ids"])
                        for layer_name, ids in layers.items():
                            record[f"{layer_name}_retrieved_turn_ids"] = ids
                            if evaluated:
                                flatten_metric_record(
                                    layer_name,
                                    retrieval_metrics(turn.dependency_turn_ids, ids),
                                    record,
                                )

                        if evaluated and record.get("mid_long_full_recall") != 1:
                            await failure_writer.write(
                                {
                                    "session_id": session.session_id,
                                    "turn_id": turn.turn_id,
                                    "turn_index": turn.turn_index,
                                    "dependency_type": turn.dependency_type,
                                    "gold_turn_ids": list(turn.dependency_turn_ids),
                                    "required_context": turn.required_context,
                                    "retrieved_turn_ids": {
                                        layer_name: ids for layer_name, ids in layers.items()
                                    },
                                    "retrieved_memories": compact_retrieved_memories(
                                        trace["retrieved_context"]
                                    ),
                                }
                            )

                        add_started = time.perf_counter()
                        add_result = await asyncio.wait_for(
                            memory.add(
                                [
                                    {
                                        "role": "user",
                                        "content": turn.question,
                                        "name": f"{turn.turn_id}:user",
                                    },
                                    {
                                        "role": "assistant",
                                        "content": turn.answer,
                                        "name": f"{turn.turn_id}:assistant",
                                    },
                                ],
                                user_id=user_id,
                                run_id=session.session_id,
                                metadata={
                                    "benchmark_run_name": benchmark_cfg.get("run_name", "recall_benchmark"),
                                    "dataset_session_id": session.session_id,
                                    "dataset_turn_id": turn.turn_id,
                                    "dataset_turn_index": turn.turn_index,
                                },
                                infer=infer,
                            ),
                            timeout=add_timeout,
                        )
                        record["add_submit_ms"] = (time.perf_counter() - add_started) * 1000.0
                        background = add_result.get("background") or {}
                        migration_job_id = background.get("migration_job_id")
                        profile_job_id = background.get("profile_job_id")
                        record["migration_job_id"] = migration_job_id
                        record["profile_job_id"] = profile_job_id
                        evicted_turn_ids = lineage.register_add(
                            session.session_id,
                            turn.turn_id,
                            migration_job_id,
                        )
                        record["evicted_turn_ids"] = list(evicted_turn_ids)
                        if migration_job_id:
                            pending_migration_jobs.append(str(migration_job_id))
                        if wait_strategy == "every_turn":
                            await wait_pending(pending_migration_jobs)
                    except Exception as exc:
                        record["error"] = f"{type(exc).__name__}: {exc}"
                        record["traceback"] = traceback.format_exc()
                        LOGGER.exception("Session %s turn %s 执行失败", session.session_id, turn.turn_id)
                    async with records_lock:
                        records.append(record)
                    await raw_writer.write(record)
                    if record.get("error"):
                        break

                await wait_pending(pending_migration_jobs)
            except Exception:
                LOGGER.exception("Session %s 终止", session.session_id)
            finally:
                session_wall_clock_seconds[session.session_id] = time.perf_counter() - session_started

    try:
        await asyncio.gather(*(run_session(session) for session in sessions))
        await memory.flush_background_tasks(timeout=job_timeout)
    finally:
        await raw_writer.close()
        await failure_writer.close()
        memory.close()

    records.sort(key=lambda item: (item["session_id"], item["turn_index"]))
    write_csv(output_dir / "recall_turn_results.csv", records)

    session_rows: list[dict[str, Any]] = []
    by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_session[record["session_id"]].append(record)
    for session_id, rows in sorted(by_session.items()):
        evaluated_rows = [row for row in rows if row.get("evaluated") and not row.get("error")]
        session_rows.append(
            {
                "session_id": session_id,
                "turn_count": len(rows),
                "evaluated_count": len(evaluated_rows),
                "error_count": sum(1 for row in rows if row.get("error")),
                "wall_clock_seconds": session_wall_clock_seconds.get(session_id),
                "migration_job_count": sum(bool(row.get("migration_job_id")) for row in rows),
                "mid_long_recall": mean_or_none([row["mid_long_recall"] for row in evaluated_rows]),
                "mid_long_precision": mean_or_none([row["mid_long_precision"] for row in evaluated_rows]),
                "all_recall": mean_or_none([row["all_recall"] for row in evaluated_rows]),
                "full_dependency_recall": mean_or_none(
                    [row["mid_long_full_recall"] for row in evaluated_rows]
                ),
            }
        )
    write_csv(output_dir / "recall_session_summary.csv", session_rows)

    actual_elapsed_seconds = time.time() - started_at
    sequential_estimated_seconds = sum(session_wall_clock_seconds.values())
    effective = {
        "session_count": len(sessions),
        "session_concurrency": session_concurrency,
        "short_term_message_capacity": short_term_message_capacity,
        "short_term_qa_capacity": short_term_qa_capacity,
        "wait_strategy": wait_strategy,
        "evaluate_only_long_range": evaluate_only_long_range,
        "llm_mode": memory_cfg.get("llm_mode", "real"),
        "infer": infer,
        "profile_enabled": memory_cfg.get("profile_enabled", False),
        "runtime_dir": str(runtime_dir),
        "dataset_path": str(dataset_path),
        "git_commit": safe_git_commit(REPO_ROOT),
        "elapsed_seconds": actual_elapsed_seconds,
        "session_wall_clock_seconds": session_wall_clock_seconds,
        "sequential_estimated_seconds": sequential_estimated_seconds,
        "effective_session_speedup": (
            sequential_estimated_seconds / actual_elapsed_seconds if actual_elapsed_seconds > 0 else None
        ),
    }
    summary = build_summary(records, config, effective)
    dump_json(output_dir / "recall_summary.json", summary)
    dump_json(output_dir / "effective_memory_config.json", redact_secrets(effective_memory_config))

    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    print(f"\n结果目录：{output_dir}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run())


if __name__ == "__main__":
    main()
