from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import (
    AsyncJsonlWriter,
    dump_json,
    ensure_repo_root_on_path,
    latency_summary,
    load_dataset,
    load_json,
    parse_iso_timestamp,
    prepare_runtime_config,
    redact_secrets,
    query_queue_snapshot,
    resolve_path,
    safe_git_commit,
    write_csv,
)

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))
from exp.benchmark.benchmark_memory import create_benchmark_memory  # noqa: E402

LOGGER = logging.getLogger("concurrency_benchmark")

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    psutil = None


@dataclass
class SessionCursor:
    session: Any
    next_index: int

    def has_next(self) -> bool:
        return self.next_index < len(self.session.turns)

    def take_next(self):
        if not self.has_next():
            raise IndexError("Session 已没有剩余轮次")
        turn = self.session.turns[self.next_index]
        self.next_index += 1
        return turn


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="异步记忆系统 50 RPS 并发稳定性测试")
    parser.add_argument(
        "--config",
        default="mem0/exp/benchmark/concurrency_benchmark.json",
        help="压测配置 JSON，路径相对于仓库根目录",
    )
    return parser.parse_args()


def scheduled_offset(index: int, *, rps: float, model: str, rng: random.Random, state: dict[str, float]) -> float:
    if model == "uniform":
        return index / rps
    if model == "burst":
        batch_size = max(int(round(rps)), 1)
        return float(index // batch_size)
    if model == "poisson":
        if index == 0:
            state["poisson"] = 0.0
        else:
            state["poisson"] += rng.expovariate(rps)
        return state["poisson"]
    raise ValueError("traffic_model 只能是 uniform、burst 或 poisson")


async def run() -> None:
    args = parse_args()
    config_path = resolve_path(REPO_ROOT, args.config)
    config = load_json(config_path)

    benchmark_cfg = config.get("benchmark") or {}
    dataset_cfg = config.get("dataset") or {}
    storage_cfg = config.get("storage") or {}
    memory_cfg = config.get("memory") or {}
    load_cfg = config.get("load") or {}
    prefill_cfg = config.get("prefill") or {}
    retrieval_cfg = config.get("retrieval") or {}
    output_cfg = config.get("output") or {}

    output_dir = resolve_path(
        REPO_ROOT,
        benchmark_cfg.get("output_dir", "mem0/exp/results/concurrency_benchmark"),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = resolve_path(
        REPO_ROOT,
        storage_cfg.get("runtime_dir", "mem0/exp/runtime/concurrency_benchmark"),
    )
    dataset_path = resolve_path(REPO_ROOT, dataset_cfg["path"])
    base_memory_config_path = resolve_path(REPO_ROOT, memory_cfg["config_path"])

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
        collection_name=str(storage_cfg.get("collection_name", "concurrency_benchmark")),
        reset_storage=bool(benchmark_cfg.get("reset_storage", True)),
        agentic_retrieval_enabled=False,
        profile_enabled=bool(memory_cfg.get("profile_enabled", True)),
        profile_update_on_add=bool(memory_cfg.get("profile_update_on_add", True)),
    )
    memory = create_benchmark_memory(
        effective_memory_config,
        llm_mode=str(memory_cfg.get("llm_mode", "mock")),
    )

    infer = bool(memory_cfg.get("infer", True))
    workload = str(load_cfg.get("workload", "mixed"))
    if workload not in {"mixed", "retrieval_only", "add_only"}:
        raise ValueError("load.workload 只能是 mixed、retrieval_only 或 add_only")
    target_rps = float(load_cfg.get("target_rps", 50))
    if target_rps <= 0:
        raise ValueError("load.target_rps 必须大于 0")
    max_in_flight = int(load_cfg.get("max_in_flight", 100))
    request_timeout = float(load_cfg.get("request_timeout_seconds", 120))
    duration_seconds = load_cfg.get("duration_seconds")
    duration_seconds = float(duration_seconds) if duration_seconds is not None else None
    max_requests = load_cfg.get("max_requests")
    max_requests = int(max_requests) if max_requests is not None else None
    warmup_requests = int(load_cfg.get("warmup_requests", 100))
    traffic_model = str(load_cfg.get("traffic_model", "uniform"))
    sample_interval = float(load_cfg.get("system_sample_interval_seconds", 1.0))
    drain_timeout = float(load_cfg.get("background_drain_timeout_seconds", 1800))
    random_seed = int(benchmark_cfg.get("random_seed", 42))
    rng = random.Random(random_seed)

    top_k = int(retrieval_cfg.get("top_k", 20))
    threshold = float(retrieval_cfg.get("threshold", 0.1))
    rerank = bool(retrieval_cfg.get("rerank", False))

    request_writer = AsyncJsonlWriter(output_dir / "concurrency_requests.jsonl")
    queue_writer = AsyncJsonlWriter(output_dir / "concurrency_queue_samples.jsonl")
    job_writer = AsyncJsonlWriter(output_dir / "concurrency_background_jobs.jsonl")
    error_writer = AsyncJsonlWriter(output_dir / "concurrency_errors.jsonl")

    request_records: list[dict[str, Any]] = []
    request_records_lock = asyncio.Lock()
    queue_samples: list[dict[str, Any]] = []
    queue_samples_lock = asyncio.Lock()
    job_records: list[dict[str, Any]] = []
    in_flight = 0
    max_observed_in_flight = 0
    in_flight_lock = asyncio.Lock()
    stop_monitor = asyncio.Event()
    db_path = runtime_dir / "history.db"
    process = psutil.Process() if psutil is not None else None

    prefill_turns = int(prefill_cfg.get("turns_per_session", 0)) if prefill_cfg.get("enabled", True) else 0
    prefill_concurrency = int(prefill_cfg.get("session_concurrency", 25))
    prefill_sem = asyncio.Semaphore(max(prefill_concurrency, 1))

    async def prefill_session(session) -> None:
        async with prefill_sem:
            user_id = f"load::{session.session_id}"
            for turn in session.turns[:prefill_turns]:
                await asyncio.wait_for(
                    memory.add(
                        [
                            {"role": "user", "content": turn.question, "name": f"{turn.turn_id}:user"},
                            {"role": "assistant", "content": turn.answer, "name": f"{turn.turn_id}:assistant"},
                        ],
                        user_id=user_id,
                        run_id=session.session_id,
                        metadata={
                            "benchmark_phase": "prefill",
                            "dataset_session_id": session.session_id,
                            "dataset_turn_id": turn.turn_id,
                        },
                        infer=infer,
                    ),
                    timeout=request_timeout,
                )

    if prefill_turns > 0:
        LOGGER.info("开始预填充：%s 个 Session，每个 %s 轮", len(sessions), prefill_turns)
        await asyncio.gather(*(prefill_session(session) for session in sessions))
        if bool(prefill_cfg.get("wait_for_background", True)):
            flushed = await memory.flush_background_tasks(timeout=drain_timeout)
            if not flushed:
                LOGGER.warning("预填充后台任务在超时时间内未完全排空")
        LOGGER.info("预填充完成")

    cursors = [
        SessionCursor(session=session, next_index=min(prefill_turns, len(session.turns)))
        for session in sessions
        if min(prefill_turns, len(session.turns)) < len(session.turns)
    ]
    total_available_requests = sum(len(cursor.session.turns) - cursor.next_index for cursor in cursors)
    request_limit = total_available_requests
    if max_requests is not None:
        request_limit = min(request_limit, max_requests)
    if request_limit <= 0:
        raise RuntimeError("预填充后没有可用于正式压测的对话轮次")

    available_sessions: asyncio.Queue[SessionCursor] = asyncio.Queue()
    for cursor in cursors:
        available_sessions.put_nowait(cursor)
    capacity = asyncio.Semaphore(max(max_in_flight, 1))
    tasks: set[asyncio.Task] = set()
    jobs_by_request: list[dict[str, Any]] = []

    async def monitor() -> None:
        expected = time.monotonic()
        while not stop_monitor.is_set():
            now = time.monotonic()
            sample: dict[str, Any] = {
                "sampled_at": datetime.now(timezone.utc).isoformat(),
                "event_loop_lag_ms": max((now - expected) * 1000.0, 0.0),
                "in_flight": in_flight,
            }
            try:
                sample.update(await asyncio.to_thread(query_queue_snapshot, db_path))
            except Exception as exc:
                sample["queue_sample_error"] = f"{type(exc).__name__}: {exc}"
            if process is not None:
                try:
                    sample["cpu_percent"] = process.cpu_percent(interval=None)
                    sample["rss_bytes"] = process.memory_info().rss
                    sample["thread_count"] = process.num_threads()
                except Exception as exc:
                    sample["process_sample_error"] = f"{type(exc).__name__}: {exc}"
            async with queue_samples_lock:
                queue_samples.append(sample)
            await queue_writer.write(sample)
            expected = now + sample_interval
            try:
                await asyncio.wait_for(stop_monitor.wait(), timeout=sample_interval)
            except asyncio.TimeoutError:
                pass

    async def execute_request(
        request_index: int,
        cursor: SessionCursor,
        scheduled_at_monotonic: float,
        benchmark_started_monotonic: float,
    ) -> None:
        nonlocal in_flight, max_observed_in_flight
        turn = cursor.take_next()
        actual_started_monotonic = time.monotonic()
        actual_started_wall = time.time()
        phase = "warmup" if request_index < warmup_requests else "measured"
        record: dict[str, Any] = {
            "request_index": request_index,
            "request_id": f"request-{request_index:06d}",
            "phase": phase,
            "workload": workload,
            "session_id": cursor.session.session_id,
            "turn_id": turn.turn_id,
            "turn_index": turn.turn_index,
            "scheduled_offset_ms": (scheduled_at_monotonic - benchmark_started_monotonic) * 1000.0,
            "schedule_lag_ms": max((actual_started_monotonic - scheduled_at_monotonic) * 1000.0, 0.0),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "build_success": None,
            "add_success": None,
            "error": None,
        }
        async with in_flight_lock:
            in_flight += 1
            max_observed_in_flight = max(max_observed_in_flight, in_flight)
        try:
            user_id = f"load::{cursor.session.session_id}"
            if workload in {"mixed", "retrieval_only"}:
                build_started = time.perf_counter()
                await asyncio.wait_for(
                    memory.build_agent_answer_messages(
                        turn.question,
                        user_id=user_id,
                        session_id=cursor.session.session_id,
                        top_k=top_k,
                        threshold=threshold,
                        rerank=rerank,
                        explain=False,
                        include_profile_metadata=False,
                    ),
                    timeout=request_timeout,
                )
                record["build_total_ms"] = (time.perf_counter() - build_started) * 1000.0
                record["build_success"] = True

            if workload in {"mixed", "add_only"}:
                add_started_monotonic = time.perf_counter()
                add_started_wall = time.time()
                add_result = await asyncio.wait_for(
                    memory.add(
                        [
                            {"role": "user", "content": turn.question, "name": f"{turn.turn_id}:user"},
                            {"role": "assistant", "content": turn.answer, "name": f"{turn.turn_id}:assistant"},
                        ],
                        user_id=user_id,
                        run_id=cursor.session.session_id,
                        metadata={
                            "benchmark_run_name": benchmark_cfg.get("run_name", "concurrency_benchmark"),
                            "benchmark_phase": phase,
                            "dataset_session_id": cursor.session.session_id,
                            "dataset_turn_id": turn.turn_id,
                            "dataset_turn_index": turn.turn_index,
                        },
                        infer=infer,
                    ),
                    timeout=request_timeout,
                )
                record["add_submit_ms"] = (time.perf_counter() - add_started_monotonic) * 1000.0
                record["add_success"] = True
                background = add_result.get("background") or {}
                migration_job_id = background.get("migration_job_id")
                profile_job_id = background.get("profile_job_id")
                record["migration_job_id"] = migration_job_id
                record["profile_job_id"] = profile_job_id
                jobs_by_request.append(
                    {
                        "request_id": record["request_id"],
                        "phase": phase,
                        "session_id": cursor.session.session_id,
                        "turn_id": turn.turn_id,
                        "add_started_wall": add_started_wall,
                        "migration_job_id": migration_job_id,
                        "profile_job_id": profile_job_id,
                    }
                )
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["traceback"] = traceback.format_exc()
            if record.get("build_success") is None and workload in {"mixed", "retrieval_only"}:
                record["build_success"] = False
            if record.get("add_success") is None and workload in {"mixed", "add_only"}:
                record["add_success"] = False
            await error_writer.write(record)
            LOGGER.error("请求 %s 失败：%s", record["request_id"], record["error"])
        finally:
            record["request_total_ms"] = (time.monotonic() - actual_started_monotonic) * 1000.0
            finished_wall_epoch = time.time()
            record["finished_at"] = datetime.fromtimestamp(finished_wall_epoch, timezone.utc).isoformat()
            record["started_wall_epoch"] = actual_started_wall
            record["finished_wall_epoch"] = finished_wall_epoch
            async with request_records_lock:
                request_records.append(record)
            await request_writer.write(record)
            async with in_flight_lock:
                in_flight -= 1
            if cursor.has_next():
                available_sessions.put_nowait(cursor)
            capacity.release()

    monitor_task = asyncio.create_task(monitor())
    benchmark_started_monotonic = time.monotonic()
    benchmark_started_wall = time.time()
    schedule_state: dict[str, float] = {"poisson": 0.0}
    scheduled_count = 0
    request_generation_finished_wall = benchmark_started_wall
    flushed = False
    background_drain_seconds: float | None = None

    try:
        for request_index in range(request_limit):
            offset = scheduled_offset(
                request_index,
                rps=target_rps,
                model=traffic_model,
                rng=rng,
                state=schedule_state,
            )
            if duration_seconds is not None and offset >= duration_seconds:
                break
            scheduled_at = benchmark_started_monotonic + offset
            sleep_seconds = scheduled_at - time.monotonic()
            if sleep_seconds > 0:
                await asyncio.sleep(sleep_seconds)
            await capacity.acquire()
            cursor = await available_sessions.get()
            task = asyncio.create_task(
                execute_request(
                    request_index,
                    cursor,
                    scheduled_at,
                    benchmark_started_monotonic,
                )
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            scheduled_count += 1

        if tasks:
            await asyncio.gather(*tuple(tasks))

        request_generation_finished_wall = time.time()
        LOGGER.info("前台请求结束，开始等待后台任务排空")
        drain_started = time.monotonic()
        flushed = await memory.flush_background_tasks(timeout=drain_timeout)
        background_drain_seconds = time.monotonic() - drain_started
        if not flushed:
            LOGGER.warning("后台任务在 %.1f 秒内未完全排空", drain_timeout)
    finally:
        stop_monitor.set()
        await monitor_task

    for item in jobs_by_request:
        job_record = dict(item)
        completion_epochs: list[float] = []
        migration_id = item.get("migration_job_id")
        profile_id = item.get("profile_job_id")
        if migration_id:
            migration = await asyncio.to_thread(memory.db.get_background_job, migration_id, "migration")
            job_record["migration"] = migration
            if migration:
                completed = parse_iso_timestamp(migration.get("finalized_at") or migration.get("updated_at"))
                if completed is not None:
                    completion_epochs.append(completed)
                    job_record["migration_complete_ms"] = max(
                        (completed - item["add_started_wall"]) * 1000.0,
                        0.0,
                    )
        if profile_id:
            profile = await asyncio.to_thread(memory.db.get_background_job, profile_id, "profile")
            job_record["profile"] = profile
            if profile:
                completed = parse_iso_timestamp(profile.get("updated_at"))
                if completed is not None:
                    completion_epochs.append(completed)
                    job_record["profile_complete_ms"] = max(
                        (completed - item["add_started_wall"]) * 1000.0,
                        0.0,
                    )
        if completion_epochs:
            job_record["add_complete_ms"] = max(
                (max(completion_epochs) - item["add_started_wall"]) * 1000.0,
                0.0,
            )
        job_records.append(job_record)
        await job_writer.write(job_record)

    await request_writer.close()
    await queue_writer.close()
    await job_writer.close()
    await error_writer.close()
    memory.close()

    request_records.sort(key=lambda item: item["request_index"])
    measured = [record for record in request_records if record.get("phase") == "measured"]
    successful = [record for record in measured if not record.get("error")]
    measured_jobs = [record for record in job_records if record.get("phase") == "measured"]

    total_elapsed_seconds = max(time.time() - benchmark_started_wall, 1e-9)
    generation_elapsed_seconds = max(request_generation_finished_wall - benchmark_started_wall, 1e-9)
    last_frontend_finished_wall = max(
        (record.get("finished_wall_epoch", benchmark_started_wall) for record in request_records),
        default=benchmark_started_wall,
    )
    frontend_completion_elapsed_seconds = max(last_frontend_finished_wall - benchmark_started_wall, 1e-9)
    summary = {
        "run_name": benchmark_cfg.get("run_name", "concurrency_benchmark"),
        "workload": workload,
        "llm_mode": memory_cfg.get("llm_mode", "mock"),
        "infer": infer,
        "target_rps": target_rps,
        "traffic_model": traffic_model,
        "scheduled_requests": scheduled_count,
        "warmup_requests": sum(1 for record in request_records if record.get("phase") == "warmup"),
        "measured_requests": len(measured),
        "successful_measured_requests": len(successful),
        "failed_measured_requests": len(measured) - len(successful),
        "error_rate": (len(measured) - len(successful)) / len(measured) if measured else None,
        "actual_schedule_rps": scheduled_count / generation_elapsed_seconds,
        "actual_frontend_completion_rps": len(request_records) / frontend_completion_elapsed_seconds,
        "actual_measured_completion_rps": len(measured) / frontend_completion_elapsed_seconds,
        "max_in_flight_config": max_in_flight,
        "max_in_flight_observed": max_observed_in_flight,
        "background_flushed": flushed,
        "background_drain_seconds": background_drain_seconds,
        "schedule_lag_ms": latency_summary([record.get("schedule_lag_ms") for record in measured]),
        "request_total_ms": latency_summary([record.get("request_total_ms") for record in measured]),
        "build_total_ms": latency_summary([record.get("build_total_ms") for record in successful]),
        "add_submit_ms": latency_summary([record.get("add_submit_ms") for record in successful]),
        "migration_complete_ms": latency_summary(
            [record.get("migration_complete_ms") for record in measured_jobs]
        ),
        "profile_complete_ms": latency_summary(
            [record.get("profile_complete_ms") for record in measured_jobs]
        ),
        "add_complete_ms": latency_summary([record.get("add_complete_ms") for record in measured_jobs]),
        "runtime_dir": str(runtime_dir),
        "dataset_path": str(dataset_path),
        "session_count": len(sessions),
        "prefill_turns_per_session": prefill_turns,
        "git_commit": safe_git_commit(REPO_ROOT),
        "elapsed_seconds": total_elapsed_seconds,
        "benchmark_config": redact_secrets(config),
    }

    if queue_samples:
        numeric_keys = {
            key
            for sample in queue_samples
            for key, value in sample.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        summary["queue_and_system_max"] = {
            key: max(float(sample[key]) for sample in queue_samples if key in sample)
            for key in sorted(numeric_keys)
        }

    write_csv(output_dir / "concurrency_requests.csv", request_records)
    write_csv(output_dir / "concurrency_queue_samples.csv", queue_samples)
    write_csv(
        output_dir / "concurrency_background_jobs.csv",
        [
            {
                **{key: value for key, value in record.items() if key not in {"migration", "profile"}},
                "migration_status": (record.get("migration") or {}).get("status"),
                "midterm_status": (record.get("migration") or {}).get("midterm_status"),
                "longterm_status": (record.get("migration") or {}).get("longterm_status"),
                "profile_status": (record.get("profile") or {}).get("status"),
            }
            for record in job_records
        ],
    )
    dump_json(output_dir / "concurrency_summary.json", summary)
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
