#!/usr/bin/env python3
"""Run an isolated concurrency benchmark against the OSS AsyncMemory pipeline."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import hashlib
import io
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
import warnings
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from unittest.mock import patch

# Keep benchmark traffic and first-run notices off the network.
os.environ["MEM0_TELEMETRY"] = "False"
warnings.filterwarnings("ignore", category=DeprecationWarning)

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
    warnings.simplefilter("ignore")
    from mem0 import AsyncMemory  # noqa: E402
    from mem0.configs.base import BackgroundTaskConfig, MemoryConfig  # noqa: E402
    from mem0.configs.midterm_prompts import (  # noqa: E402
        MIDTERM_PAGE_SUMMARY_PROMPT,
        MIDTERM_SESSION_MERGE_PROMPT,
    )
    from mem0.configs.profile_prompts import PROFILE_UPDATE_SYSTEM_PROMPT  # noqa: E402
    from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT  # noqa: E402
    from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT  # noqa: E402
    from mem0.memory import main as memory_main  # noqa: E402


DEFAULT_CONCURRENCY = 500
DEFAULT_USERS = 50
DEFAULT_WORKER_COUNT = 8
DEFAULT_BACKGROUND_CONCURRENCY = 128
DEFAULT_ENTITY_EXTRACTION_WORKERS = 2
DEFAULT_ENTITY_EXTRACTION_PENDING_CAPACITY = 8
DEFAULT_TARGET_RPS = 5.0
DEFAULT_SUSTAINED_DURATION_SECONDS = 180.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0
DEFAULT_BACKGROUND_TIMEOUT_SECONDS = 600.0
LOCAL_EMBEDDING_DIMENSIONS = 64
SUMMARY_WIDTH = 68
TOKEN_PATTERN = re.compile(r"BENCH_U(?P<user>\d+)_S(?P<session>[A-Z0-9]+)_R(?P<request>\d+)")
MEASURED_RETRIEVAL_SCOPE = "measured_retrieval"
ENTITY_EXTRACTION_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar(
    "benchmark_entity_extraction_scope",
    default="other",
)


@dataclass(frozen=True)
class RequestSpec:
    request_id: int
    user_id: str
    session_id: str
    marker: str
    query: str
    user_message: str
    assistant_response: str
    idempotency_key: str
    warmup: bool = False


@dataclass
class RequestResult:
    spec: RequestSpec
    retrieval_latency_seconds: float | None
    add_latency_seconds: float | None
    end_to_end_latency_seconds: float
    succeeded: bool
    migration_job_id: str | None = None
    profile_job_id: str | None = None
    error: str | None = None


@dataclass
class SafetyReport:
    user_isolation_errors: int = 0
    session_isolation_errors: int = 0
    duplicate_conflict_errors: int = 0
    ordering_violations: int = 0
    background_task_failures: int = 0
    retrieval_verification_errors: int = 0
    cleanup_errors: int = 0
    job_counts: dict[str, int] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)

    def add_detail(self, detail: str) -> None:
        if len(self.details) < 8:
            self.details.append(detail)

    @property
    def passed(self) -> bool:
        return not any(
            (
                self.user_isolation_errors,
                self.session_isolation_errors,
                self.duplicate_conflict_errors,
                self.ordering_violations,
                self.background_task_failures,
                self.retrieval_verification_errors,
                self.cleanup_errors,
            )
        )


@dataclass
class BenchmarkResult:
    request_results: list[RequestResult]
    warmup_requests: int
    peak_in_flight_requests: int
    foreground_duration_seconds: float
    background_drain_seconds: float
    total_duration_seconds: float
    background_drained: bool
    load: LoadMetrics
    entity_extraction: EntityExtractionMetrics
    retrieval_stages: dict[str, tuple[float, ...]]
    resources: ForegroundResourceUsage
    safety: SafetyReport


@dataclass(frozen=True)
class EntityExtractionMetrics:
    all_attempts: int
    measured_attempts: int
    measured_executed: int
    measured_failures: int
    measured_failed_before_execution: int
    measured_call_seconds: tuple[float, ...]
    measured_queue_wait_seconds: tuple[float, ...]
    measured_execution_seconds: tuple[float, ...]
    peak_executing_all: int
    peak_executing_measured: int


@dataclass(frozen=True)
class ForegroundResourceUsage:
    average_process_cpu_percent: float
    peak_sampled_process_cpu_percent: float
    average_threads: float
    min_threads: int
    max_threads: int
    logical_cpus: int
    asyncio_default_executor_workers: int


@dataclass(frozen=True)
class LoadMetrics:
    mode: str
    target_rps: float | None
    configured_arrival_seconds: float
    actual_arrival_seconds: float
    achieved_arrival_rps: float
    schedule_lag_seconds: tuple[float, ...]
    completion_tail_seconds: float
    in_flight_at_arrival_end: int
    background_backlog_at_arrival_end: dict[str, int]
    background_backlog_at_foreground_end: dict[str, int]
    peak_sampled_background_backlog: int


class EntityExtractionProbe:
    """Measure executor queueing and actual entity extraction execution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._attempts: Counter[str] = Counter()
            self._executed: Counter[str] = Counter()
            self._failures: Counter[str] = Counter()
            self._failed_before_execution: Counter[str] = Counter()
            self._call_seconds: dict[str, list[float]] = defaultdict(list)
            self._queue_wait_seconds: dict[str, list[float]] = defaultdict(list)
            self._execution_seconds: dict[str, list[float]] = defaultdict(list)
            self._active_all = 0
            self._active_by_scope: Counter[str] = Counter()
            self._peak_executing_all = 0
            self._peak_executing_by_scope: Counter[str] = Counter()

    def run(self, delegate, function, args: Sequence[Any], kwargs: dict[str, Any], scope: str):
        submitted_at = time.perf_counter()
        started = threading.Event()
        with self._lock:
            self._attempts[scope] += 1

        def observed_function(*function_args: Any, **function_kwargs: Any):
            execution_started_at = time.perf_counter()
            started.set()
            with self._lock:
                self._executed[scope] += 1
                self._queue_wait_seconds[scope].append(execution_started_at - submitted_at)
                self._active_all += 1
                self._active_by_scope[scope] += 1
                self._peak_executing_all = max(self._peak_executing_all, self._active_all)
                self._peak_executing_by_scope[scope] = max(
                    self._peak_executing_by_scope[scope],
                    self._active_by_scope[scope],
                )
            try:
                return function(*function_args, **function_kwargs)
            finally:
                execution_seconds = time.perf_counter() - execution_started_at
                with self._lock:
                    self._execution_seconds[scope].append(execution_seconds)
                    self._active_all -= 1
                    self._active_by_scope[scope] -= 1

        try:
            return delegate(observed_function, *args, **kwargs)
        except BaseException:
            with self._lock:
                self._failures[scope] += 1
                if not started.is_set():
                    self._failed_before_execution[scope] += 1
            raise
        finally:
            with self._lock:
                self._call_seconds[scope].append(time.perf_counter() - submitted_at)

    def snapshot(self) -> EntityExtractionMetrics:
        scope = MEASURED_RETRIEVAL_SCOPE
        with self._lock:
            return EntityExtractionMetrics(
                all_attempts=sum(self._attempts.values()),
                measured_attempts=self._attempts[scope],
                measured_executed=self._executed[scope],
                measured_failures=self._failures[scope],
                measured_failed_before_execution=self._failed_before_execution[scope],
                measured_call_seconds=tuple(self._call_seconds[scope]),
                measured_queue_wait_seconds=tuple(self._queue_wait_seconds[scope]),
                measured_execution_seconds=tuple(self._execution_seconds[scope]),
                peak_executing_all=self._peak_executing_all,
                peak_executing_measured=self._peak_executing_by_scope[scope],
            )


class RetrievalStageProbe:
    """Record benchmark-local timings at stable retrieval method boundaries."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seconds: dict[str, list[float]] = defaultdict(list)

    def record(self, stage: str, started_at: float) -> None:
        if ENTITY_EXTRACTION_SCOPE.get() != MEASURED_RETRIEVAL_SCOPE:
            return
        with self._lock:
            self._seconds[stage].append(time.perf_counter() - started_at)

    def snapshot(self) -> dict[str, tuple[float, ...]]:
        with self._lock:
            return {stage: tuple(values) for stage, values in self._seconds.items()}


def _process_thread_count() -> int:
    try:
        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


class ForegroundResourceSampler:
    """Sample process CPU and native thread counts during the measured wave."""

    def __init__(self, asyncio_default_executor_workers: int, interval_seconds: float = 0.25):
        self.asyncio_default_executor_workers = asyncio_default_executor_workers
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._cpu_samples: list[float] = []
        self._thread_samples: list[int] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._started_wall = time.perf_counter()
        self._started_cpu = time.process_time()
        self._thread_samples.append(_process_thread_count())
        self._thread = threading.Thread(
            target=self._sample,
            name="benchmark-resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def _sample(self) -> None:
        previous_wall = self._started_wall
        previous_cpu = self._started_cpu
        while not self._stop_event.wait(self.interval_seconds):
            current_wall = time.perf_counter()
            current_cpu = time.process_time()
            elapsed = current_wall - previous_wall
            if elapsed > 0:
                self._cpu_samples.append((current_cpu - previous_cpu) / elapsed * 100.0)
            self._thread_samples.append(_process_thread_count())
            previous_wall = current_wall
            previous_cpu = current_cpu

    def stop(self) -> ForegroundResourceUsage:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        stopped_wall = time.perf_counter()
        stopped_cpu = time.process_time()
        self._thread_samples.append(_process_thread_count())
        elapsed = max(stopped_wall - self._started_wall, sys.float_info.epsilon)
        average_cpu = (stopped_cpu - self._started_cpu) / elapsed * 100.0
        return ForegroundResourceUsage(
            average_process_cpu_percent=average_cpu,
            peak_sampled_process_cpu_percent=max(self._cpu_samples, default=average_cpu),
            average_threads=sum(self._thread_samples) / len(self._thread_samples),
            min_threads=min(self._thread_samples),
            max_threads=max(self._thread_samples),
            logical_cpus=os.cpu_count() or 1,
            asyncio_default_executor_workers=self.asyncio_default_executor_workers,
        )


class LocalEmbedding:
    """Deterministic local embedding substitute used only by benchmark local mode."""

    def __init__(self, dimensions: int = LOCAL_EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def embed(self, text: Any, memory_action: str | None = None) -> list[float]:
        del memory_action
        serialized = text if isinstance(text, str) else json.dumps(text, ensure_ascii=True, sort_keys=True)
        tokens = re.findall(r"[A-Za-z0-9_]+", serialized.lower())
        vector = [0.0] * self.dimensions
        for token in tokens or [serialized]:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            vector[int.from_bytes(digest[:4], "big") % self.dimensions] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_batch(self, texts: Iterable[Any], memory_action: str | None = None) -> list[list[float]]:
        return [self.embed(text, memory_action) for text in texts]

    def close(self) -> None:
        return None


class LocalLLM:
    """Return deterministic schema-valid responses without external model calls."""

    supports_response_metadata = False

    @staticmethod
    def _content(messages: Sequence[dict[str, Any]], index: int) -> str:
        if not messages:
            return ""
        value = messages[index].get("content", "")
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _marker(text: str) -> str:
        match = TOKEN_PATTERN.search(text)
        return match.group(0) if match else "BENCH_U000_SUNKNOWN_R0000"

    @staticmethod
    def _user_tag(marker: str) -> str:
        match = TOKEN_PATTERN.fullmatch(marker)
        return f"USER_U{int(match.group('user')):03d}" if match else "USER_UNKNOWN"

    def generate_response(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> Any:
        del tool_choice, kwargs
        if tools:
            # A no-tool decision is a valid Agentic result: existing context is sufficient.
            return ""

        system = self._content(messages, 0)
        user = self._content(messages, -1)
        marker = self._marker(user)

        if system.strip() == QUERY_REFERENCE_RESOLUTION_PROMPT.strip():
            payload = json.loads(user)
            return {"resolved_query": payload["current_query"]}

        if system.startswith(PROFILE_UPDATE_SYSTEM_PROMPT):
            return {"operations": [], "unmapped_facts": []}

        if system.strip() == MIDTERM_PAGE_SUMMARY_PROMPT.strip():
            return {
                "summary": f"Local benchmark summary for {marker}",
                "keywords": [self._user_tag(marker), marker],
            }

        if system.strip() == MIDTERM_SESSION_MERGE_PROMPT.strip():
            payload = json.loads(user)
            existing = str(payload.get("existing_session", {}).get("summary") or "").strip()
            new_page = str(payload.get("new_page", {}).get("summary") or "").strip()
            summary = " ".join(part for part in (existing, new_page) if part)
            return {
                "summary": summary or f"Local benchmark session for {marker}",
                "keywords": [self._user_tag(marker), marker],
            }

        if system.strip() == ADDITIVE_EXTRACTION_PROMPT.strip():
            return {
                "memory": [
                    {
                        "text": (f"Reporting currency is CNY for {self._user_tag(marker)}; source marker {marker}"),
                        "linked_memory_ids": [],
                    }
                ]
            }

        return ""

    async def generate_response_async(self, *args: Any, **kwargs: Any) -> Any:
        return self.generate_response(*args, **kwargs)

    def close(self) -> None:
        return None


class InFlightRequestCounter:
    """Coordinate a simultaneous wave and record coroutine-level in-flight requests."""

    def __init__(self, expected: int):
        self.expected = expected
        self.active = 0
        self.peak = 0
        self.ready = 0
        self.all_ready = asyncio.Event()
        self.release = asyncio.Event()

    async def enter(self) -> None:
        self.active += 1
        self.ready += 1
        self.peak = max(self.peak, self.active)
        if self.ready == self.expected:
            self.all_ready.set()
        await self.release.wait()

    def leave(self) -> None:
        self.active -= 1


class SustainedInFlightCounter:
    """Track coroutine-level in-flight requests without gating their start."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        self.active -= 1


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark AsyncMemory with either a synchronized burst or sustained open-loop arrivals.",
    )
    parser.add_argument(
        "--load-mode",
        choices=("burst", "sustained"),
        default="burst",
        help="Arrival model: synchronized burst or open-loop sustained traffic (default: burst).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concurrent requests and total measured requests (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--target-rps",
        type=float,
        default=DEFAULT_TARGET_RPS,
        help=f"Target open-loop arrival rate in sustained mode (default: {DEFAULT_TARGET_RPS:g}).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_SUSTAINED_DURATION_SECONDS,
        help=f"Sustained arrival duration in seconds (default: {DEFAULT_SUSTAINED_DURATION_SECONDS:g}).",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=DEFAULT_USERS,
        help=f"Number of isolated users in the workload (default: {DEFAULT_USERS}).",
    )
    parser.add_argument(
        "--worker-count",
        type=int,
        default=DEFAULT_WORKER_COUNT,
        help=f"Configured worker threads for each background queue (default: {DEFAULT_WORKER_COUNT}).",
    )
    parser.add_argument(
        "--background-concurrency",
        type=int,
        default=DEFAULT_BACKGROUND_CONCURRENCY,
        help=f"In-flight task cap per background worker (default: {DEFAULT_BACKGROUND_CONCURRENCY}).",
    )
    parser.add_argument(
        "--entity-extraction-workers",
        type=int,
        default=DEFAULT_ENTITY_EXTRACTION_WORKERS,
        help=f"Entity extraction executor workers (default: {DEFAULT_ENTITY_EXTRACTION_WORKERS}).",
    )
    parser.add_argument(
        "--entity-extraction-pending-capacity",
        type=int,
        default=DEFAULT_ENTITY_EXTRACTION_PENDING_CAPACITY,
        help=(f"Entity extraction executor pending capacity (default: {DEFAULT_ENTITY_EXTRACTION_PENDING_CAPACITY})."),
    )
    parser.add_argument(
        "--disable-entity-extraction",
        action="store_true",
        help="Benchmark only: replace entity extraction with an empty-result function; all retrieval routes still run.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help=f"Timeout for one request in seconds (default: {DEFAULT_REQUEST_TIMEOUT_SECONDS:g}).",
    )
    parser.add_argument(
        "--background-timeout",
        type=float,
        default=DEFAULT_BACKGROUND_TIMEOUT_SECONDS,
        help=f"Timeout for draining background jobs in seconds (default: {DEFAULT_BACKGROUND_TIMEOUT_SECONDS:g}).",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Use the configured default external LLM and embedding providers instead of local substitutes.",
    )
    parser.add_argument(
        "--agentic",
        action="store_true",
        help="Enable optional Agentic retrieval calls inside build_agent_answer_messages.",
    )
    args = parser.parse_args()

    for name in (
        "concurrency",
        "users",
        "worker_count",
        "background_concurrency",
        "entity_extraction_workers",
        "entity_extraction_pending_capacity",
    ):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than 0")
    for name in ("target_rps", "duration", "request_timeout", "background_timeout"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be a finite value greater than 0")
    if args.background_concurrency > 1024:
        parser.error("--background-concurrency cannot exceed 1024")
    if args.worker_count > 16:
        parser.error("--worker-count cannot exceed 16")
    if args.entity_extraction_workers > 16:
        parser.error("--entity-extraction-workers cannot exceed 16")
    if args.entity_extraction_pending_capacity > 128:
        parser.error("--entity-extraction-pending-capacity cannot exceed 128")
    return args


def _measured_request_count(args: argparse.Namespace) -> int:
    if args.load_mode == "burst":
        return args.concurrency
    return max(1, math.floor(args.target_rps * args.duration + 1e-9))


def _make_workload(concurrency: int, requested_users: int) -> tuple[list[RequestSpec], list[RequestSpec]]:
    session_count = math.ceil(concurrency / 2)
    user_count = min(requested_users, session_count)
    warmups = []
    for user_index in range(user_count):
        user_id = f"bench-user-{user_index:03d}"
        session_id = f"bench-u{user_index:03d}-seed"
        marker = f"BENCH_U{user_index:03d}_SSEED_R{user_index:04d}"
        warmups.append(
            RequestSpec(
                request_id=user_index,
                user_id=user_id,
                session_id=session_id,
                marker=marker,
                query="Remember that this user's management reporting currency is CNY.",
                user_message=(
                    f"{marker} USER_U{user_index:03d}: remember that this user's management reporting currency is CNY."
                ),
                assistant_response=f"Stored reporting context for {marker}.",
                idempotency_key=f"memory-concurrency:warmup:{user_index:04d}",
                warmup=True,
            )
        )

    requests = []
    for request_id in range(concurrency):
        session_slot = request_id // 2
        user_index = session_slot % user_count
        session_index = session_slot // user_count
        user_id = f"bench-user-{user_index:03d}"
        session_id = f"bench-u{user_index:03d}-s{session_index:03d}"
        marker = f"BENCH_U{user_index:03d}_S{session_index:03d}_R{request_id:04d}"
        requests.append(
            RequestSpec(
                request_id=request_id,
                user_id=user_id,
                session_id=session_id,
                marker=marker,
                query="Use the remembered reporting currency context for this analysis request.",
                user_message=(
                    f"{marker} USER_U{user_index:03d}: use the remembered reporting currency "
                    "context for this analysis request."
                ),
                assistant_response=f"Completed isolated analysis request {marker}.",
                idempotency_key=f"memory-concurrency:request:{request_id:04d}",
            )
        )
    return warmups, requests


def _memory_config(args: argparse.Namespace, root: Path) -> MemoryConfig:
    dimensions = 1536 if args.real else LOCAL_EMBEDDING_DIMENSIONS
    background = BackgroundTaskConfig(
        enabled=True,
        midterm_worker_count=args.worker_count,
        longterm_worker_count=args.worker_count,
        profile_worker_count=args.worker_count,
        promotion_worker_count=args.worker_count,
        midterm_worker_concurrency=args.background_concurrency,
        longterm_worker_concurrency=args.background_concurrency,
        profile_worker_concurrency=args.background_concurrency,
        promotion_worker_concurrency=args.background_concurrency,
        entity_extraction_worker_count=args.entity_extraction_workers,
        entity_extraction_pending_capacity=args.entity_extraction_pending_capacity,
        max_retries=0,
        retry_delays_seconds=(),
        poll_interval_seconds=0.01,
        lease_timeout_seconds=60.0,
        heartbeat_interval_seconds=10.0,
        watchdog_interval_seconds=5.0,
        shutdown_timeout_seconds=30.0,
    )
    return MemoryConfig(
        vector_store={
            "provider": "qdrant",
            "config": {
                "collection_name": "memory_concurrency_benchmark",
                "embedding_model_dims": dimensions,
                "path": str(root / "qdrant"),
                "on_disk": False,
                "bm25_language": "zh",
            },
        },
        history_db_path=str(root / "history.db"),
        background=background,
        midterm={
            "short_term_capacity": 2,
            "promotion_min_recall_count": 1000,
        },
        agentic_retrieval={"enabled": args.agentic},
    )


def _create_memory(args: argparse.Namespace, root: Path) -> AsyncMemory:
    config = _memory_config(args, root)
    if args.real:
        return AsyncMemory(config)
    with (
        patch.object(memory_main.EmbedderFactory, "create", return_value=LocalEmbedding()),
        patch.object(memory_main.LlmFactory, "create", return_value=LocalLLM()),
    ):
        return AsyncMemory(config)


def _install_entity_extraction_probe(
    memory: AsyncMemory,
    *,
    disabled: bool = False,
) -> EntityExtractionProbe:
    probe = EntityExtractionProbe()
    original_delegate = memory._run_entity_extraction

    def disabled_delegate(function, *args: Any, **kwargs: Any):
        del function, args, kwargs
        return []

    delegate = disabled_delegate if disabled else original_delegate

    def instrumented(function, *args: Any, **kwargs: Any):
        return probe.run(
            delegate,
            function,
            args,
            kwargs,
            ENTITY_EXTRACTION_SCOPE.get(),
        )

    memory._run_entity_extraction = instrumented
    return probe


def _install_retrieval_stage_probe(memory: AsyncMemory) -> RetrievalStageProbe:
    """Wrap stable instance boundaries without changing the production retrieval implementation."""
    probe = RetrievalStageProbe()

    def wrap_sync(instance: Any, method_name: str, stage: str) -> None:
        original = getattr(instance, method_name)

        def instrumented(*args: Any, **kwargs: Any):
            started_at = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                probe.record(stage, started_at)

        setattr(instance, method_name, instrumented)

    def wrap_async(instance: Any, method_name: str, stage: str) -> None:
        original = getattr(instance, method_name)

        async def instrumented(*args: Any, **kwargs: Any):
            started_at = time.perf_counter()
            try:
                return await original(*args, **kwargs)
            finally:
                probe.record(stage, started_at)

        setattr(instance, method_name, instrumented)

    wrap_async(memory, "_retrieve_base_context", "Profile / Short-term Context")
    wrap_sync(memory, "_with_midterm_search_results", "Mid-term / Promoted Retrieval")
    wrap_async(memory, "_search_vector_store", "Fine-grained LongTerm Total")
    wrap_async(memory.llm, "generate_response_async", "Query Rewrite LLM Call")
    wrap_sync(memory.vector_store, "search", "Qdrant Dense Search Call")
    wrap_sync(memory.vector_store, "keyword_search", "BM25 / Qdrant Keyword Call")

    retriever = memory.fine_grained_longterm_retriever
    wrap_sync(retriever, "_coarse_rank", "Coarse Merge / Ranking")
    wrap_async(retriever, "_rerank_async", "Rerank")
    wrap_sync(retriever, "_format_results", "Final Result Formatting")
    return probe


async def _submit_qa(memory: AsyncMemory, spec: RequestSpec) -> RequestResult:
    started = time.perf_counter()
    retrieval_latency: float | None = None
    add_latency: float | None = None
    try:
        if not spec.warmup:
            retrieval_started = time.perf_counter()
            entity_scope_token = ENTITY_EXTRACTION_SCOPE.set(MEASURED_RETRIEVAL_SCOPE)
            try:
                answer_messages = await memory.build_agent_answer_messages(
                    spec.query,
                    user_id=spec.user_id,
                    session_id=spec.session_id,
                    top_k=20,
                )
            finally:
                ENTITY_EXTRACTION_SCOPE.reset(entity_scope_token)
                retrieval_latency = time.perf_counter() - retrieval_started
            if not answer_messages or not all(isinstance(message, dict) for message in answer_messages):
                raise RuntimeError("build_agent_answer_messages returned no prompt messages")

        add_started = time.perf_counter()
        try:
            result = await memory.add(
                [
                    {"role": "user", "content": spec.user_message},
                    {"role": "assistant", "content": spec.assistant_response},
                ],
                user_id=spec.user_id,
                run_id=spec.session_id,
                idempotency_key=spec.idempotency_key,
            )
        finally:
            add_latency = time.perf_counter() - add_started
        background = result.get("background", {}) if isinstance(result, dict) else {}
        return RequestResult(
            spec=spec,
            retrieval_latency_seconds=retrieval_latency,
            add_latency_seconds=add_latency,
            end_to_end_latency_seconds=time.perf_counter() - started,
            succeeded=True,
            migration_job_id=background.get("migration_job_id"),
            profile_job_id=background.get("profile_job_id"),
        )
    except Exception as exc:
        return RequestResult(
            spec=spec,
            retrieval_latency_seconds=retrieval_latency,
            add_latency_seconds=add_latency,
            end_to_end_latency_seconds=time.perf_counter() - started,
            succeeded=False,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _run_warmup(
    memory: AsyncMemory,
    specs: Sequence[RequestSpec],
    request_timeout: float,
    background_timeout: float,
) -> list[RequestResult]:
    results = await asyncio.gather(
        *(asyncio.wait_for(_submit_qa(memory, spec), timeout=request_timeout) for spec in specs)
    )
    failures = [result for result in results if not result.succeeded]
    if failures:
        raise RuntimeError(f"warm-up failed: {failures[0].error}")
    if not await memory.flush_background_tasks(timeout=background_timeout):
        raise TimeoutError("warm-up background jobs did not drain before the timeout")
    first = specs[0]
    await asyncio.wait_for(
        memory.build_agent_answer_messages(
            "Recall the reporting currency context.",
            user_id=first.user_id,
            session_id=first.session_id,
            top_k=20,
        ),
        timeout=request_timeout,
    )
    return list(results)


async def _run_request_wave(
    memory: AsyncMemory,
    specs: Sequence[RequestSpec],
    request_timeout: float,
) -> tuple[list[RequestResult], int, float, ForegroundResourceUsage]:
    counter = InFlightRequestCounter(len(specs))

    async def run_one(spec: RequestSpec) -> RequestResult:
        await counter.enter()
        try:
            return await asyncio.wait_for(_submit_qa(memory, spec), timeout=request_timeout)
        except Exception as exc:
            return RequestResult(
                spec=spec,
                retrieval_latency_seconds=None,
                add_latency_seconds=None,
                end_to_end_latency_seconds=request_timeout,
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            counter.leave()

    tasks = [asyncio.create_task(run_one(spec), name=f"memory-benchmark-{spec.request_id}") for spec in specs]
    await asyncio.wait_for(counter.all_ready.wait(), timeout=request_timeout)
    loop = asyncio.get_running_loop()
    default_executor = getattr(loop, "_default_executor", None)
    default_executor_workers = int(getattr(default_executor, "_max_workers", min(32, (os.cpu_count() or 1) + 4)))
    resource_sampler = ForegroundResourceSampler(default_executor_workers)
    resource_sampler.start()
    started = time.perf_counter()
    counter.release.set()
    try:
        results = await asyncio.gather(*tasks)
    finally:
        resource_usage = resource_sampler.stop()
    return list(results), counter.peak, time.perf_counter() - started, resource_usage


async def _run_sustained_load(
    memory: AsyncMemory,
    specs: Sequence[RequestSpec],
    request_timeout: float,
    target_rps: float,
    duration_seconds: float,
) -> tuple[list[RequestResult], int, float, ForegroundResourceUsage, LoadMetrics]:
    """Submit requests on a monotonic open-loop schedule without waiting for completion."""
    counter = SustainedInFlightCounter()
    tasks: list[asyncio.Task[RequestResult]] = []
    schedule_lags: list[float] = []
    release_times: list[float] = []
    backlog_samples: list[int] = []
    stop_backlog_sampler = asyncio.Event()

    async def run_one(spec: RequestSpec) -> RequestResult:
        counter.enter()
        try:
            return await asyncio.wait_for(_submit_qa(memory, spec), timeout=request_timeout)
        except Exception as exc:
            return RequestResult(
                spec=spec,
                retrieval_latency_seconds=None,
                add_latency_seconds=None,
                end_to_end_latency_seconds=request_timeout,
                succeeded=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            counter.leave()

    async def sample_background_backlog() -> None:
        while not stop_backlog_sampler.is_set():
            snapshot = await asyncio.to_thread(_background_backlog_snapshot, memory)
            backlog_samples.append(sum(snapshot.values()))
            try:
                await asyncio.wait_for(stop_backlog_sampler.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    loop = asyncio.get_running_loop()
    default_executor = getattr(loop, "_default_executor", None)
    default_executor_workers = int(getattr(default_executor, "_max_workers", min(32, (os.cpu_count() or 1) + 4)))
    resource_sampler = ForegroundResourceSampler(default_executor_workers)
    resource_sampler.start()
    started = time.perf_counter()
    backlog_sampler_task = asyncio.create_task(sample_background_backlog(), name="memory-backlog-sampler")
    try:
        for index, spec in enumerate(specs):
            scheduled_at = started + index / target_rps
            wait_seconds = scheduled_at - time.perf_counter()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            released_at = time.perf_counter()
            schedule_lags.append(max(0.0, released_at - scheduled_at))
            release_times.append(released_at)
            tasks.append(asyncio.create_task(run_one(spec), name=f"memory-benchmark-{spec.request_id}"))

        configured_arrival_end = started + duration_seconds
        wait_seconds = configured_arrival_end - time.perf_counter()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)
        actual_arrival_end = max(configured_arrival_end, release_times[-1] if release_times else started)
        actual_arrival_seconds = actual_arrival_end - started
        in_flight_at_arrival_end = counter.active
        backlog_at_arrival_end = await asyncio.to_thread(_background_backlog_snapshot, memory)
        backlog_samples.append(sum(backlog_at_arrival_end.values()))

        results = await asyncio.gather(*tasks)
        foreground_duration = time.perf_counter() - started
        backlog_at_foreground_end = await asyncio.to_thread(_background_backlog_snapshot, memory)
        backlog_samples.append(sum(backlog_at_foreground_end.values()))
    finally:
        stop_backlog_sampler.set()
        await backlog_sampler_task
        resource_usage = resource_sampler.stop()

    load = LoadMetrics(
        mode="sustained",
        target_rps=target_rps,
        configured_arrival_seconds=duration_seconds,
        actual_arrival_seconds=actual_arrival_seconds,
        achieved_arrival_rps=len(specs) / max(actual_arrival_seconds, sys.float_info.epsilon),
        schedule_lag_seconds=tuple(schedule_lags),
        completion_tail_seconds=max(0.0, foreground_duration - actual_arrival_seconds),
        in_flight_at_arrival_end=in_flight_at_arrival_end,
        background_backlog_at_arrival_end=backlog_at_arrival_end,
        background_backlog_at_foreground_end=backlog_at_foreground_end,
        peak_sampled_background_backlog=max(backlog_samples, default=0),
    )
    return list(results), counter.peak, foreground_duration, resource_usage, load


def _fetchall(memory: AsyncMemory, statement: str, parameters: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
    db = memory.db
    if db is None or db.connection is None:
        raise RuntimeError("SQLite database is already closed")
    with db._lock:
        return list(db.connection.execute(statement, tuple(parameters)).fetchall())


def _background_backlog_snapshot(memory: AsyncMemory) -> dict[str, int]:
    tables = {
        "migration": "memory_migration_jobs",
        "longterm": "longterm_extraction_jobs",
        "profile": "profile_update_jobs",
        "promotion": "memory_promotion_jobs",
    }
    return {
        label: int(
            _fetchall(
                memory,
                f"SELECT COUNT(*) FROM {table} WHERE status IN ('pending', 'running', 'retry')",
            )[0][0]
        )
        for label, table in tables.items()
    }


def _markers(text: Any) -> list[str]:
    return [match.group(0) for match in TOKEN_PATTERN.finditer(str(text or ""))]


def _expected_scope(spec: RequestSpec) -> str:
    return memory_main._build_session_scope({"user_id": spec.user_id, "run_id": spec.session_id})


def _record_scope_errors(
    report: SafetyReport,
    text: Any,
    actual_user_id: Any,
    actual_session_id: Any,
    expected_by_marker: dict[str, RequestSpec],
    source: str,
) -> None:
    for marker in set(_markers(text)):
        expected = expected_by_marker.get(marker)
        if expected is None:
            continue
        if str(actual_user_id or "") != expected.user_id:
            report.user_isolation_errors += 1
            report.add_detail(f"user scope mismatch in {source}: {marker}")
        if str(actual_session_id or "") != expected.session_id:
            report.session_isolation_errors += 1
            report.add_detail(f"session scope mismatch in {source}: {marker}")


def _inspect_background_jobs(memory: AsyncMemory, report: SafetyReport) -> None:
    table_rules = {
        "memory_migration_jobs": {"succeeded", "succeeded_degraded"},
        "longterm_extraction_jobs": {"succeeded"},
        "profile_update_jobs": {"succeeded"},
        "memory_promotion_jobs": {"succeeded"},
    }
    for table, successful_statuses in table_rules.items():
        rows = _fetchall(memory, f"SELECT job_id, status FROM {table}")
        label = table.removeprefix("memory_").removesuffix("_jobs")
        report.job_counts[label] = len(rows)
        for job_id, status in rows:
            if status not in successful_statuses:
                report.background_task_failures += 1
                report.add_detail(f"background job {job_id} ended as {status}")

    migration_rows = _fetchall(
        memory,
        "SELECT job_id, midterm_status, longterm_status FROM memory_migration_jobs",
    )
    successful_stage_statuses = {"succeeded", "succeeded_degraded"}
    for job_id, midterm_status, longterm_status in migration_rows:
        if midterm_status not in successful_stage_statuses or longterm_status not in successful_stage_statuses:
            # The parent row was already counted when terminal state represented loss.
            if (midterm_status, longterm_status) != ("succeeded", "succeeded"):
                report.add_detail(f"migration stages {job_id}: midterm={midterm_status}, longterm={longterm_status}")


def _inspect_job_scopes(
    memory: AsyncMemory,
    report: SafetyReport,
    expected_by_marker: dict[str, RequestSpec],
) -> None:
    rows = _fetchall(
        memory,
        "SELECT messages_json, filters_json FROM longterm_extraction_jobs",
    )
    for messages_json, filters_json in rows:
        filters = json.loads(filters_json)
        _record_scope_errors(
            report,
            messages_json,
            filters.get("user_id"),
            filters.get("run_id"),
            expected_by_marker,
            "long-term job",
        )

    rows = _fetchall(memory, "SELECT messages_json, user_id FROM profile_update_jobs")
    for messages_json, user_id in rows:
        for marker in set(_markers(messages_json)):
            expected = expected_by_marker.get(marker)
            if expected is not None and str(user_id) != expected.user_id:
                report.user_isolation_errors += 1
                report.add_detail(f"profile job user mismatch: {marker}")

    rows = _fetchall(
        memory,
        "SELECT session_scope, filters_json FROM memory_migration_jobs",
    )
    for session_scope, filters_json in rows:
        filters = json.loads(filters_json)
        expected_scope = memory_main._build_session_scope(filters)
        if session_scope != expected_scope:
            report.session_isolation_errors += 1
            report.add_detail(f"migration filter mismatch: {session_scope}")


def _inspect_vector_scopes(
    memory: AsyncMemory,
    report: SafetyReport,
    expected_by_marker: dict[str, RequestSpec],
) -> None:
    listed = memory.vector_store.list(top_k=max(len(expected_by_marker) * 4, 1000))
    primary_rows = listed[0] if isinstance(listed, (tuple, list)) and listed else []
    for row in primary_rows:
        payload = dict(getattr(row, "payload", None) or {})
        _record_scope_errors(
            report,
            payload.get("data"),
            payload.get("user_id"),
            payload.get("run_id"),
            expected_by_marker,
            "long-term vector",
        )

    for row in memory.midterm_memory.list_pages(top_k=max(len(expected_by_marker) * 2, 1000)):
        payload = dict(getattr(row, "payload", None) or {})
        text = " ".join(
            str(payload.get(key) or "") for key in ("raw_dialogue", "summary", "user_input", "assistant_response")
        )
        _record_scope_errors(
            report,
            text,
            payload.get("user_id"),
            payload.get("run_id"),
            expected_by_marker,
            "mid-term vector",
        )


def _inspect_ordering_and_duplicates(
    memory: AsyncMemory,
    report: SafetyReport,
    expected_specs: Sequence[RequestSpec],
    request_results: Sequence[RequestResult],
) -> None:
    successful_specs = [result.spec for result in request_results if result.succeeded]
    expected_per_scope = Counter(_expected_scope(spec) for spec in [*expected_specs, *successful_specs])
    states = {
        scope: (int(current_turn), open_turn)
        for scope, current_turn, open_turn in _fetchall(
            memory,
            "SELECT session_scope, current_turn_index, open_turn_index FROM conversation_turns",
        )
    }
    for scope, expected_turns in expected_per_scope.items():
        current_turn, open_turn = states.get(scope, (0, None))
        if current_turn != expected_turns or open_turn is not None:
            report.ordering_violations += 1
            report.add_detail(
                f"turn state mismatch {scope}: expected={expected_turns}, current={current_turn}, open={open_turn}"
            )

    jobs_by_scope: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for scope, turn_index, messages_json in _fetchall(
        memory,
        "SELECT session_scope, turn_index, messages_json FROM longterm_extraction_jobs",
    ):
        jobs_by_scope[scope].append((int(turn_index), messages_json))
    for scope, expected_turns in expected_per_scope.items():
        jobs = jobs_by_scope.get(scope, [])
        indices = sorted(turn_index for turn_index, _ in jobs)
        if indices != list(range(1, expected_turns + 1)):
            report.ordering_violations += 1
            report.add_detail(f"long-term sequence mismatch {scope}: {indices}")
        for _, messages_json in jobs:
            roles = [message.get("role") for message in json.loads(messages_json)]
            if roles.count("user") != 1 or roles.count("assistant") != 1:
                report.ordering_violations += 1
                report.add_detail(f"incomplete QA ordering in {scope}: {roles}")

    for table in ("longterm_extraction_jobs", "profile_update_jobs", "memory_migration_jobs"):
        rows = _fetchall(
            memory,
            f"""
            SELECT source_operation_key, COUNT(*)
            FROM {table}
            WHERE source_operation_key LIKE 'memory-concurrency:%'
            GROUP BY source_operation_key
            HAVING COUNT(*) > 1
            """,
        )
        report.duplicate_conflict_errors += sum(int(count) - 1 for _, count in rows)

    idempotency_failures = _fetchall(
        memory,
        """
        SELECT idempotency_key, status
        FROM memory_idempotency_operations
        WHERE idempotency_key LIKE 'memory-concurrency:%' AND status != 'succeeded'
        """,
    )
    report.duplicate_conflict_errors += len(idempotency_failures)
    for key, status in idempotency_failures:
        report.add_detail(f"idempotency operation {key} ended as {status}")

    for result in request_results:
        if result.error and any(word in result.error.lower() for word in ("duplicate", "conflict", "idempotency")):
            report.duplicate_conflict_errors += 1


async def _verify_public_retrieval(
    memory: AsyncMemory,
    report: SafetyReport,
    request_specs: Sequence[RequestSpec],
) -> None:
    by_scope: dict[tuple[str, str], RequestSpec] = {}
    for spec in request_specs:
        by_scope.setdefault((spec.user_id, spec.session_id), spec)
    semaphore = asyncio.Semaphore(32)

    async def verify(spec: RequestSpec) -> tuple[RequestSpec, str | None, str | None]:
        try:
            async with semaphore:
                messages = await memory.build_agent_answer_messages(
                    "Recall the management reporting currency context.",
                    user_id=spec.user_id,
                    session_id=spec.session_id,
                    top_k=20,
                )
            content = "\n".join(str(message.get("content") or "") for message in messages)
            return spec, content, None
        except Exception as exc:
            return spec, None, f"{type(exc).__name__}: {exc}"

    results = await asyncio.gather(*(verify(spec) for spec in by_scope.values()))
    for spec, content, error in results:
        if error is not None:
            report.retrieval_verification_errors += 1
            report.add_detail(f"retrieval verification failed for {spec.session_id}: {error}")
            continue
        markers = set(_markers(content))
        expected_user_number = int(spec.user_id.rsplit("-", 1)[-1])
        cross_session_markers = {
            marker
            for marker in markers
            if (match := TOKEN_PATTERN.fullmatch(marker)) is not None
            and int(match.group("user")) == expected_user_number
            and marker != spec.marker
            and f"_S{spec.session_id.rsplit('-s', 1)[-1]}_" not in marker
        }
        if not cross_session_markers:
            report.retrieval_verification_errors += 1
            report.add_detail(f"no same-user cross-session memory was retrieved for {spec.session_id}")
        for marker in markers:
            match = TOKEN_PATTERN.fullmatch(marker)
            if match is not None and int(match.group("user")) != expected_user_number:
                report.user_isolation_errors += 1
                report.add_detail(f"public retrieval leaked {marker} into {spec.session_id}")


async def _inspect_safety(
    memory: AsyncMemory,
    warmup_specs: Sequence[RequestSpec],
    request_specs: Sequence[RequestSpec],
    request_results: Sequence[RequestResult],
    background_timeout: float,
) -> SafetyReport:
    report = SafetyReport()
    all_specs = [*warmup_specs, *request_specs]
    expected_by_marker = {spec.marker: spec for spec in all_specs}

    await _verify_public_retrieval(memory, report, request_specs)
    if not await memory.flush_background_tasks(timeout=background_timeout):
        report.background_task_failures += 1
        report.add_detail("background jobs were pending after retrieval verification")

    _inspect_background_jobs(memory, report)
    _inspect_job_scopes(memory, report, expected_by_marker)
    _inspect_vector_scopes(memory, report, expected_by_marker)
    _inspect_ordering_and_duplicates(memory, report, warmup_specs, request_results)
    return report


async def _run_benchmark(args: argparse.Namespace, root: Path) -> BenchmarkResult:
    measured_request_count = _measured_request_count(args)
    warmup_specs, request_specs = _make_workload(measured_request_count, args.users)
    memory = _create_memory(args, root)
    entity_probe = _install_entity_extraction_probe(memory, disabled=args.disable_entity_extraction)
    retrieval_stage_probe = _install_retrieval_stage_probe(memory)
    report: SafetyReport | None = None
    closed = False
    try:
        print(f"Preparing {len(warmup_specs)} isolated user memories...", flush=True)
        await _run_warmup(
            memory,
            warmup_specs,
            args.request_timeout,
            args.background_timeout,
        )
        entity_probe.reset()

        if args.load_mode == "sustained":
            print(
                f"Launching {measured_request_count} AsyncMemory requests at {args.target_rps:g} req/s "
                f"for {args.duration:g} seconds...",
                flush=True,
            )
            request_results, peak_in_flight, foreground_duration, resource_usage, load = await _run_sustained_load(
                memory,
                request_specs,
                args.request_timeout,
                args.target_rps,
                args.duration,
            )
        else:
            print(f"Launching {args.concurrency} synchronized AsyncMemory requests...", flush=True)
            request_results, peak_in_flight, foreground_duration, resource_usage = await _run_request_wave(
                memory,
                request_specs,
                args.request_timeout,
            )
            foreground_backlog = await asyncio.to_thread(_background_backlog_snapshot, memory)
            load = LoadMetrics(
                mode="burst",
                target_rps=None,
                configured_arrival_seconds=0.0,
                actual_arrival_seconds=0.0,
                achieved_arrival_rps=0.0,
                schedule_lag_seconds=(),
                completion_tail_seconds=0.0,
                in_flight_at_arrival_end=peak_in_flight,
                background_backlog_at_arrival_end=dict(foreground_backlog),
                background_backlog_at_foreground_end=dict(foreground_backlog),
                peak_sampled_background_backlog=sum(foreground_backlog.values()),
            )
        entity_extraction = entity_probe.snapshot()
        retrieval_stages = retrieval_stage_probe.snapshot()

        print("Draining persistent background memory jobs...", flush=True)
        drain_started = time.perf_counter()
        background_drained = await memory.flush_background_tasks(timeout=args.background_timeout)
        drain_duration = time.perf_counter() - drain_started
        total_duration = foreground_duration + drain_duration

        print("Verifying user/session isolation and durable job ordering...", flush=True)
        report = await _inspect_safety(
            memory,
            warmup_specs,
            request_specs,
            request_results,
            args.background_timeout,
        )
        if not background_drained:
            report.background_task_failures += 1
            report.add_detail("measured background drain timed out")

        closed = await asyncio.to_thread(memory.close)
        if not closed:
            report.cleanup_errors += 1
            report.add_detail("AsyncMemory.close() timed out")
        leaked_workers = [
            thread.name for thread in threading.enumerate() if thread.name.startswith("mem0-") and thread.is_alive()
        ]
        if leaked_workers:
            report.cleanup_errors += len(leaked_workers)
            report.add_detail(f"worker threads still alive: {', '.join(leaked_workers[:3])}")

        return BenchmarkResult(
            request_results=request_results,
            warmup_requests=len(warmup_specs),
            peak_in_flight_requests=peak_in_flight,
            foreground_duration_seconds=foreground_duration,
            background_drain_seconds=drain_duration,
            total_duration_seconds=total_duration,
            background_drained=background_drained,
            load=load,
            entity_extraction=entity_extraction,
            retrieval_stages=retrieval_stages,
            resources=resource_usage,
            safety=report,
        )
    finally:
        if not closed and memory.db is not None:
            await asyncio.to_thread(memory.close)


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(math.ceil((percentile / 100.0) * len(ordered)) - 1, 0)
    return ordered[index]


def _line(label: str, value: str) -> str:
    return f"{label:<25}: {value}"


def _print_latency_summary(label: str, values_seconds: Sequence[float]) -> None:
    values_ms = [value * 1000 for value in values_seconds]
    print(label)
    print(_line("  Samples", str(len(values_ms))))
    print(_line("  Average", f"{sum(values_ms) / len(values_ms) if values_ms else 0.0:.2f} ms"))
    print(_line("  P50", f"{_percentile(values_ms, 50):.2f} ms"))
    print(_line("  P95", f"{_percentile(values_ms, 95):.2f} ms"))
    print(_line("  P99", f"{_percentile(values_ms, 99):.2f} ms"))
    print(_line("  Min / Max", f"{min(values_ms, default=0.0):.2f} / {max(values_ms, default=0.0):.2f} ms"))


def _print_latency_trend(label: str, values_seconds: Sequence[float]) -> None:
    if not values_seconds:
        print(_line(label, "n/a"))
        return
    window = max(1, math.ceil(len(values_seconds) * 0.2))
    first = values_seconds[:window]
    last = values_seconds[-window:]
    first_average = sum(first) / len(first) * 1000
    last_average = sum(last) / len(last) * 1000
    ratio = last_average / first_average if first_average > 0 else 0.0
    print(_line(label, f"{first_average:.2f} -> {last_average:.2f} ms ({ratio:.2f}×)"))


def _format_backlog(backlog: dict[str, int]) -> str:
    return (
        f"{sum(backlog.values())} total "
        f"({backlog.get('migration', 0)} migration / {backlog.get('longterm', 0)} longterm / "
        f"{backlog.get('profile', 0)} profile / {backlog.get('promotion', 0)} promotion)"
    )


def _print_summary(args: argparse.Namespace, result: BenchmarkResult) -> None:
    successful = sum(request.succeeded for request in result.request_results)
    failed = len(result.request_results) - successful
    success_rate = successful / len(result.request_results) * 100 if result.request_results else 0.0
    retrieval_latencies = [
        request.retrieval_latency_seconds
        for request in result.request_results
        if request.retrieval_latency_seconds is not None
    ]
    add_latencies = [
        request.add_latency_seconds for request in result.request_results if request.add_latency_seconds is not None
    ]
    end_to_end_latencies = [request.end_to_end_latency_seconds for request in result.request_results]
    foreground_throughput = (
        len(result.request_results) / result.foreground_duration_seconds
        if result.foreground_duration_seconds > 0
        else 0.0
    )
    settled_throughput = (
        len(result.request_results) / result.total_duration_seconds if result.total_duration_seconds > 0 else 0.0
    )
    safety = result.safety
    passed = (
        failed == 0
        and (
            (args.load_mode == "burst" and result.peak_in_flight_requests == args.concurrency)
            or (args.load_mode == "sustained" and 0 < result.peak_in_flight_requests <= len(result.request_results))
        )
        and result.background_drained
        and safety.passed
    )
    mode = "Real (external LLM + embedding)" if args.real else "Local (LLM + embedding substituted)"
    job_counts = safety.job_counts

    print()
    print("=" * SUMMARY_WIDTH)
    print("MEMORY CONCURRENCY BENCHMARK".center(SUMMARY_WIDTH))
    print("=" * SUMMARY_WIDTH)
    print(_line("Mode", mode))
    print(_line("Load Mode", result.load.mode))
    if result.load.mode == "burst":
        print(_line("Request Concurrency", str(args.concurrency)))
    else:
        print(_line("Target Arrival Rate", f"{result.load.target_rps:.2f} req/s"))
        print(_line("Configured Arrival", f"{result.load.configured_arrival_seconds:.3f} s"))
        print(_line("Actual Arrival Window", f"{result.load.actual_arrival_seconds:.3f} s"))
        print(_line("Achieved Arrival Rate", f"{result.load.achieved_arrival_rps:.2f} req/s"))
        _print_latency_summary("Arrival Schedule Lag", result.load.schedule_lag_seconds)
        print(_line("In-flight At Arrival End", str(result.load.in_flight_at_arrival_end)))
        print(_line("Completion Tail", f"{result.load.completion_tail_seconds:.3f} s"))
    print(_line("Warm-up Requests", str(result.warmup_requests)))
    print(_line("Measured Requests", str(len(result.request_results))))
    print(_line("Successful", str(successful)))
    print(_line("Failed", str(failed)))
    print(_line("Success Rate", f"{success_rate:.2f} %"))
    if failed:
        print(_line("Latency Note", "failed requests include time-to-failure"))
    print()
    print(_line("Foreground Duration", f"{result.foreground_duration_seconds:.3f} s"))
    print(_line("Foreground Throughput", f"{foreground_throughput:.2f} req/s"))
    print(_line("Background Drain", f"{result.background_drain_seconds:.3f} s"))
    print(_line("Total Duration", f"{result.total_duration_seconds:.3f} s"))
    print(_line("Settled Throughput", f"{settled_throughput:.2f} req/s"))
    print()
    _print_latency_summary("Retrieval Latency", retrieval_latencies)
    _print_latency_summary("Add Latency", add_latencies)
    _print_latency_summary("End-to-End Latency", end_to_end_latencies)
    if result.load.mode == "sustained" and successful == len(result.request_results):
        print("Latency Trend (First 20% -> Last 20%)")
        _print_latency_trend("  Retrieval Average", retrieval_latencies)
        _print_latency_trend("  Add Average", add_latencies)
        _print_latency_trend("  End-to-End Average", end_to_end_latencies)
    print(_line("Peak In-flight Requests", str(result.peak_in_flight_requests)))
    if result.load.mode == "sustained":
        print(_line("Backlog At Arrival End", _format_backlog(result.load.background_backlog_at_arrival_end)))
        print(_line("Backlog At Foreground End", _format_backlog(result.load.background_backlog_at_foreground_end)))
        print(_line("Peak Sampled Backlog", str(result.load.peak_sampled_background_backlog)))
    print(
        _line(
            "Background Jobs",
            (
                f"{job_counts.get('migration', 0)} migration / "
                f"{job_counts.get('longterm_extraction', 0)} longterm / "
                f"{job_counts.get('profile_update', 0)} profile / "
                f"{job_counts.get('promotion', 0)} promotion"
            ),
        )
    )
    print()
    print("Retrieval Stage Probe (Measured Foreground)")
    for stage in (
        "Query Rewrite LLM Call",
        "Profile / Short-term Context",
        "Fine-grained LongTerm Total",
        "Qdrant Dense Search Call",
        "BM25 / Qdrant Keyword Call",
        "Mid-term / Promoted Retrieval",
        "Coarse Merge / Ranking",
        "Rerank",
        "Final Result Formatting",
    ):
        _print_latency_summary(f"  {stage}", result.retrieval_stages.get(stage, ()))
    print("  Fine-grained child timings overlap their parent and must not be added together.")
    print("  Qdrant/BM25 call timing excludes upstream asyncio default-executor queueing.")
    if not result.retrieval_stages.get("Query Rewrite LLM Call"):
        print("  Zero query-rewrite samples means the resolver made no LLM call; the retrieval path still ran.")
    print()
    entity = result.entity_extraction
    print("Entity Extraction Probe (Measured Foreground)")
    print(_line("  Mode", "DISABLED (benchmark only)" if args.disable_entity_extraction else "ENABLED"))
    print(_line("  Configured Workers", str(args.entity_extraction_workers)))
    print(_line("  Pending Capacity", str(args.entity_extraction_pending_capacity)))
    print(_line("  All Attempts", str(entity.all_attempts)))
    print(_line("  Retrieval Attempts", str(entity.measured_attempts)))
    print(_line("  Retrieval Executed", str(entity.measured_executed)))
    print(_line("  Retrieval Failures", str(entity.measured_failures)))
    print(_line("  Failed Before Execute", str(entity.measured_failed_before_execution)))
    _print_latency_summary("  Entity Call Latency", entity.measured_call_seconds)
    _print_latency_summary("  Executor Queue Wait", entity.measured_queue_wait_seconds)
    _print_latency_summary("  Entity Execution", entity.measured_execution_seconds)
    print(_line("  Peak Executing (All)", str(entity.peak_executing_all)))
    print(_line("  Peak Executing Retrieval", str(entity.peak_executing_measured)))
    print("  Queue wait excludes upstream asyncio default-executor queueing.")
    print()
    resources = result.resources
    print("Foreground Process Resources")
    print(_line("  Process CPU Average", f"{resources.average_process_cpu_percent:.2f} %"))
    print(_line("  Process CPU Peak", f"{resources.peak_sampled_process_cpu_percent:.2f} %"))
    print(
        _line(
            "  Process Threads Avg",
            f"{resources.average_threads:.1f}",
        )
    )
    print(_line("  Process Threads Min/Max", f"{resources.min_threads} / {resources.max_threads}"))
    print(_line("  Logical CPUs", str(resources.logical_cpus)))
    print(_line("  Asyncio Default Workers", str(resources.asyncio_default_executor_workers)))
    print("  CPU percentages use 100% per fully utilized logical CPU.")
    print()
    print("-" * SUMMARY_WIDTH)
    print("Concurrency Safety")
    print("-" * SUMMARY_WIDTH)
    print(_line("User Isolation Errors", str(safety.user_isolation_errors)))
    print(_line("Session Isolation Errors", str(safety.session_isolation_errors)))
    print(_line("Duplicate/Conflict Errors", str(safety.duplicate_conflict_errors)))
    print(_line("Ordering Violations", str(safety.ordering_violations)))
    print(_line("Background Task Failures", str(safety.background_task_failures)))
    print(_line("Retrieval Verify Errors", str(safety.retrieval_verification_errors)))
    print(_line("Cleanup Errors", str(safety.cleanup_errors)))
    print("=" * SUMMARY_WIDTH)
    print(_line("Concurrency Safety", "PASS" if passed else "FAIL"))
    print(_line("Performance", "MEASURED"))
    print("=" * SUMMARY_WIDTH)

    if not passed:
        errors = [request.error for request in result.request_results if request.error]
        for detail in [*errors[:3], *safety.details[:5]]:
            print(f"Failure detail: {detail}")


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    logging.disable(logging.CRITICAL)

    mode_description = "REAL external providers" if args.real else "LOCAL model substitutes"
    print("Memory concurrency benchmark")
    print(f"Mode: {mode_description}; all storage is isolated in a temporary directory.")
    print(_line("Load Mode", args.load_mode))
    if args.load_mode == "burst":
        print(_line("Request Concurrency", str(args.concurrency)))
    else:
        print(_line("Target Arrival Rate", f"{args.target_rps:g} req/s"))
        print(_line("Arrival Duration", f"{args.duration:g} s"))
        print(_line("Measured Requests", str(_measured_request_count(args))))
    print(_line("Midterm Workers", f"{args.worker_count} × {args.background_concurrency}"))
    print(_line("Longterm Workers", f"{args.worker_count} × {args.background_concurrency}"))
    print(_line("Profile Workers", f"{args.worker_count} × {args.background_concurrency}"))
    print(_line("Promotion Workers", f"{args.worker_count} × {args.background_concurrency}"))
    entity_default_marker = " (production default)" if args.entity_extraction_workers == 2 else ""
    print(_line("Entity Extraction Workers", f"{args.entity_extraction_workers}{entity_default_marker}"))
    print(_line("Entity Pending Capacity", str(args.entity_extraction_pending_capacity)))
    print(
        _line(
            "Entity Extraction",
            "DISABLED (benchmark-only -> [])" if args.disable_entity_extraction else "ENABLED",
        )
    )
    print("Worker concurrency is an in-flight cap per worker, not a sustained-throughput guarantee.")
    if args.real:
        print("WARNING: --real performs external LLM and embedding calls for the full workload.")

    try:
        with tempfile.TemporaryDirectory(prefix="mem0-concurrency-") as temporary_directory:
            result = asyncio.run(_run_benchmark(args, Path(temporary_directory)))
        _print_summary(args, result)
    except KeyboardInterrupt:
        print("Benchmark interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Benchmark failed before summary: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    successful = sum(request.succeeded for request in result.request_results)
    passed = (
        successful == len(result.request_results)
        and (
            (args.load_mode == "burst" and result.peak_in_flight_requests == args.concurrency)
            or (args.load_mode == "sustained" and 0 < result.peak_in_flight_requests <= len(result.request_results))
        )
        and result.background_drained
        and result.safety.passed
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
