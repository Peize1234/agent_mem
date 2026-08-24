import asyncio
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
import time

from mem0.configs.base import BackgroundTaskConfig
from scripts import benchmark_memory_concurrency as benchmark


def _args(**overrides):
    values = {
        "load_mode": "burst",
        "concurrency": 2,
        "target_rps": 5.0,
        "duration": 180.0,
        "users": 1,
        "worker_count": 8,
        "background_concurrency": 128,
        "entity_extraction_workers": 2,
        "entity_extraction_pending_capacity": 8,
        "disable_entity_extraction": False,
        "request_timeout": 10.0,
        "background_timeout": 10.0,
        "real": False,
        "agentic": False,
    }
    values.update(overrides)
    return Namespace(**values)


def test_benchmark_worker_settings_do_not_change_production_defaults(tmp_path):
    production_defaults = BackgroundTaskConfig()
    assert production_defaults.midterm_worker_count == 1
    assert production_defaults.longterm_worker_count == 1
    assert production_defaults.profile_worker_count == 1
    assert production_defaults.promotion_worker_count == 1
    assert production_defaults.midterm_worker_concurrency == 1
    assert production_defaults.longterm_worker_concurrency == 1
    assert production_defaults.profile_worker_concurrency == 1
    assert production_defaults.promotion_worker_concurrency == 2

    config = benchmark._memory_config(_args(), tmp_path).background
    assert config.midterm_worker_count == 8
    assert config.longterm_worker_count == 8
    assert config.profile_worker_count == 8
    assert config.promotion_worker_count == 8
    assert config.midterm_worker_concurrency == 128
    assert config.longterm_worker_concurrency == 128
    assert config.profile_worker_concurrency == 128
    assert config.promotion_worker_concurrency == 128
    assert config.entity_extraction_worker_count == 2
    assert config.entity_extraction_pending_capacity == 8


def test_sustained_request_count_uses_target_rate_and_duration():
    assert benchmark._measured_request_count(_args(load_mode="sustained", target_rps=2.5, duration=4.0)) == 10
    assert benchmark._measured_request_count(_args(load_mode="sustained", target_rps=0.1, duration=1.0)) == 1
    assert benchmark._measured_request_count(_args(load_mode="burst", concurrency=17)) == 17


def test_entity_extraction_probe_tracks_actual_executor_execution():
    probe = benchmark.EntityExtractionProbe()

    with ThreadPoolExecutor(max_workers=1) as entity_executor:

        def delegate(function, *args):
            return entity_executor.submit(function, *args).result()

        def extraction(value):
            time.sleep(0.01)
            return value

        with ThreadPoolExecutor(max_workers=2) as callers:
            futures = [
                callers.submit(
                    probe.run,
                    delegate,
                    extraction,
                    (index,),
                    {},
                    benchmark.MEASURED_RETRIEVAL_SCOPE,
                )
                for index in range(2)
            ]
            assert [future.result() for future in futures] == [0, 1]

    metrics = probe.snapshot()
    assert metrics.all_attempts == 2
    assert metrics.measured_attempts == 2
    assert metrics.measured_executed == 2
    assert metrics.measured_failures == 0
    assert len(metrics.measured_queue_wait_seconds) == 2
    assert len(metrics.measured_execution_seconds) == 2
    assert metrics.peak_executing_all == 1
    assert metrics.peak_executing_measured == 1


def test_disabled_entity_extraction_returns_empty_without_executing_extractor():
    class FakeMemory:
        def _run_entity_extraction(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    memory = FakeMemory()
    probe = benchmark._install_entity_extraction_probe(memory, disabled=True)
    token = benchmark.ENTITY_EXTRACTION_SCOPE.set(benchmark.MEASURED_RETRIEVAL_SCOPE)
    try:
        assert memory._run_entity_extraction(lambda: (_ for _ in ()).throw(AssertionError("must not run"))) == []
    finally:
        benchmark.ENTITY_EXTRACTION_SCOPE.reset(token)

    metrics = probe.snapshot()
    assert metrics.measured_attempts == 1
    assert metrics.measured_executed == 0
    assert metrics.measured_failures == 0


def test_submit_qa_records_retrieval_add_and_end_to_end_latency():
    class FakeMemory:
        async def build_agent_answer_messages(self, *args, **kwargs):
            return [{"role": "system", "content": "context"}]

        async def add(self, *args, **kwargs):
            return {"background": {"migration_job_id": "migration", "profile_job_id": "profile"}}

    spec = benchmark.RequestSpec(
        request_id=1,
        user_id="user",
        session_id="session",
        marker="BENCH_U000_S000_R0001",
        query="query",
        user_message="user message",
        assistant_response="assistant response",
        idempotency_key="key",
    )

    result = asyncio.run(benchmark._submit_qa(FakeMemory(), spec))

    assert result.succeeded
    assert result.retrieval_latency_seconds is not None
    assert result.add_latency_seconds is not None
    assert result.end_to_end_latency_seconds >= result.retrieval_latency_seconds + result.add_latency_seconds
    assert result.migration_job_id == "migration"
    assert result.profile_job_id == "profile"


def test_summary_separates_foreground_and_settled_throughput(capsys):
    specs = [
        benchmark.RequestSpec(
            request_id=index,
            user_id="user",
            session_id=f"session-{index}",
            marker=f"BENCH_U000_S000_R{index:04d}",
            query="query",
            user_message="user message",
            assistant_response="assistant response",
            idempotency_key=f"key-{index}",
        )
        for index in range(2)
    ]
    request_results = [
        benchmark.RequestResult(
            spec=spec,
            retrieval_latency_seconds=0.4,
            add_latency_seconds=0.1,
            end_to_end_latency_seconds=0.5,
            succeeded=True,
        )
        for spec in specs
    ]
    result = benchmark.BenchmarkResult(
        request_results=request_results,
        warmup_requests=1,
        peak_in_flight_requests=2,
        foreground_duration_seconds=2.0,
        background_drain_seconds=1.0,
        total_duration_seconds=3.0,
        background_drained=True,
        load=benchmark.LoadMetrics(
            mode="burst",
            target_rps=None,
            configured_arrival_seconds=0.0,
            actual_arrival_seconds=0.0,
            achieved_arrival_rps=0.0,
            schedule_lag_seconds=(),
            completion_tail_seconds=0.0,
            in_flight_at_arrival_end=2,
            background_backlog_at_arrival_end={},
            background_backlog_at_foreground_end={},
            peak_sampled_background_backlog=0,
        ),
        entity_extraction=benchmark.EntityExtractionMetrics(
            all_attempts=2,
            measured_attempts=2,
            measured_executed=2,
            measured_failures=0,
            measured_failed_before_execution=0,
            measured_call_seconds=(0.2, 0.3),
            measured_queue_wait_seconds=(0.01, 0.02),
            measured_execution_seconds=(0.19, 0.28),
            peak_executing_all=2,
            peak_executing_measured=2,
        ),
        retrieval_stages={"Query Rewrite LLM Call": (0.01, 0.02)},
        resources=benchmark.ForegroundResourceUsage(
            average_process_cpu_percent=100.0,
            peak_sampled_process_cpu_percent=120.0,
            average_threads=10.0,
            min_threads=9,
            max_threads=11,
            logical_cpus=8,
            asyncio_default_executor_workers=12,
        ),
        safety=benchmark.SafetyReport(),
    )

    benchmark._print_summary(_args(), result)

    output = capsys.readouterr().out
    assert "Warm-up Requests         : 1" in output
    assert "Measured Requests        : 2" in output
    assert "Foreground Throughput    : 1.00 req/s" in output
    assert "Settled Throughput       : 0.67 req/s" in output
    assert "Peak In-flight Requests  : 2" in output
    assert "  Retrieval Attempts     : 2" in output
    assert "  Peak Executing Retrieval: 2" in output
    assert "  Asyncio Default Workers: 12" in output
    assert "Concurrency Safety       : PASS" in output
    assert "Performance              : MEASURED" in output
    assert "RESULT" not in output
