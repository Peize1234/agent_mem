from __future__ import annotations

import copy
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .artifact_registry import ArtifactRegistry
from .build_report import write_outputs
from .candidate_selector import select_best
from .dataset_audit import audit_dataset
from .evaluate_candidate import candidate_hash, combine_candidate_results, evaluate_candidate
from .experiment_branches import BranchRegistry
from .generated_source_artifacts import prepare_generated_source_candidate
from .io_utils import append_jsonl, load_json, stable_hash, write_jsonl
from .model_discovery import ModelDiscovery, ResourceEnvelope
from .models import Candidate, CandidateResult, Dataset
from .production_midterm_adapter import (
    generate_production_sources,
    production_candidate_from_manifests,
    source_worker_parallelism,
)
from .research_decision import ResearchDecisionEngine
from .research_runtime import ResearchLLMRuntime
from .split_sessions import create_or_load_split
from .staged_search import run_staged_search
from .temporal_replay import run_cross_session_temporal_replay


@dataclass(frozen=True)
class TunerConfig:
    dataset: Path
    k: int = 5
    budget: str = "standard"
    target: str = "midterm"
    sessions: tuple[str, ...] = ()
    seed: int | None = None
    output_root: Path = Path("exp/results/auto_tuning")
    resume: Path | None = None
    source_run: Path | None = None
    memory_config: Path = Path(".agents/skills/memory-retrieval-tuner/memory_config.json")
    llm_mode: str = "real"
    max_parallel_sessions: int | None = None
    max_parallel_candidates: int | None = None
    max_parallel_llm_calls: int | None = None
    overrides: dict[str, Any] = field(default_factory=dict)


def _deep_merge(base: dict[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _gpu_count() -> int:
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        return len([line for line in output.splitlines() if line.strip()])
    except (FileNotFoundError, subprocess.SubprocessError):
        return 0


def _gpu_memory_gib() -> float | None:
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout
        values = [float(line.strip()) / 1024 for line in output.splitlines() if line.strip()]
        return min(values) if values else None
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        return None


def _memory_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _execution_settings(config: TunerConfig, space: Mapping[str, Any]) -> dict[str, Any]:
    cpu = max(1, os.cpu_count() or 1)
    memory_gib = _memory_gib()
    configured = space.get("execution") or {}
    conservative_cap = max(1, min(4, cpu // 2))
    if memory_gib is not None and memory_gib < 8:
        conservative_cap = min(conservative_cap, 2)
    sessions = min(int(config.max_parallel_sessions or configured.get("max_parallel_sessions") or 4), conservative_cap)
    candidates = min(
        int(config.max_parallel_candidates or configured.get("max_parallel_candidates") or 4),
        max(1, min(4, cpu // max(sessions, 1))),
    )
    llm = min(int(config.max_parallel_llm_calls or configured.get("max_parallel_llm_calls") or 4), 4)
    return {
        "cpu_count": cpu,
        "available_memory_gib": memory_gib,
        "gpu_count": _gpu_count(),
        "max_parallel_sessions": max(1, sessions),
        "max_parallel_candidates": max(1, candidates),
        "max_parallel_llm_calls": max(1, llm),
        "adaptive_reductions": [],
    }


def _diagnose(best: CandidateResult, audit: Mapping[str, Any], space: Mapping[str, Any]) -> dict[str, Any]:
    metrics = best.metrics
    gap_pp = (float(metrics.get("recall_at_4k") or 0) - float(metrics.get("recall_at_k") or 0)) * 100.0
    deep_recall = float(metrics.get("recall_at_4k") or 0) * 100.0
    stddev_pp = float(metrics.get("session_stddev") or 0) * 100.0
    worst_gap_pp = (
        float(metrics.get("macro_session_recall_at_k") or 0) - float(metrics.get("worst_session_recall_at_k") or 0)
    ) * 100.0
    settings = space.get("diagnostics") or {}
    instability = settings.get("session_instability") or {}
    if audit.get("warnings"):
        regime = "data_artifact_suspicion"
    elif stddev_pp >= float(instability.get("max_stddev_pp", 12.0)) or worst_gap_pp >= float(
        instability.get("max_worst_session_gap_from_macro_pp", 20.0)
    ):
        regime = "session_instability"
    elif gap_pp >= float((settings.get("ranking_bottleneck") or {}).get("min_recall_4k_minus_k_pp", 20.0)):
        regime = "ranking_bottleneck"
    elif deep_recall <= float((settings.get("candidate_coverage_bottleneck") or {}).get("max_recall_4k_percent", 65.0)):
        regime = "candidate_coverage_bottleneck"
    else:
        regime = "balanced_or_plateau"
    return {
        "regime": regime,
        "recall_4k_minus_k_pp": gap_pp,
        "recall_4k_percent": deep_recall,
        "stddev_pp": stddev_pp,
        "worst_session_gap_from_macro_pp": worst_gap_pp,
    }


def _evaluate_many(
    dataset: Dataset,
    candidates: Sequence[Candidate],
    sessions: Sequence[str],
    *,
    scope: str,
    config: TunerConfig,
    shortterm_window: int,
    ranking_depth: int,
    registry: ArtifactRegistry,
    run_dir: Path,
    execution: dict[str, Any],
    trace_path: Path,
) -> list[CandidateResult]:
    results: list[CandidateResult] = []

    def run(candidate: Candidate, sessions_limit: int) -> CandidateResult:
        return evaluate_candidate(
            dataset,
            candidate,
            sessions,
            scope=scope,
            k=config.k,
            target=config.target,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            max_parallel_sessions=sessions_limit,
            registry=registry,
            run_dir=run_dir,
        )

    def invalid_result(candidate: Candidate) -> CandidateResult:
        return CandidateResult(
            name=candidate.name,
            candidate_hash=candidate_hash(dataset.sha256, candidate),
            stage=candidate.stage,
            config=copy.deepcopy(candidate.config),
            metrics={},
            requirement_rows=[],
            session_rows=[],
            runtime_seconds=0.0,
            work_seconds=0.0,
            cache_hits=0,
            cache_misses=0,
            complexity=candidate.complexity,
            status="INVALID",
        )

    prepared_candidates: list[Candidate] = []
    for candidate in candidates:
        try:
            prepared = prepare_generated_source_candidate(
                candidate,
                sessions=sessions,
                registry=registry,
                run_dir=run_dir,
                ranking_depth=ranking_depth,
                max_parallel_sessions=int(execution.get("max_parallel_sessions") or 1),
                max_parallel_llm_calls=int(execution.get("max_parallel_llm_calls") or 1),
                gpu_count=int(execution.get("gpu_count") or 0),
            )
        except Exception as exc:
            append_jsonl(
                trace_path,
                {
                    "scope": scope,
                    "candidate": candidate.name,
                    "status": "INVALID",
                    "error": f"source generation failed: {type(exc).__name__}: {exc}",
                },
            )
            results.append(invalid_result(candidate))
            continue
        if prepared is not candidate:
            # Candidate is frozen, but these dictionaries intentionally carry its
            # progressively materialized, content-addressed artifacts across
            # screening, full Tune, Validation, and final evaluation.
            candidate.config.clear()
            candidate.config.update(prepared.config)
            candidate.provenance.clear()
            candidate.provenance.update(prepared.provenance)
        prepared_candidates.append(candidate)

    candidate_parallelism = min(execution["max_parallel_candidates"], len(prepared_candidates) or 1)
    session_parallelism = execution["max_parallel_sessions"] if candidate_parallelism == 1 else 1

    with ThreadPoolExecutor(max_workers=max(1, candidate_parallelism)) as executor:
        futures = {executor.submit(run, candidate, session_parallelism): candidate for candidate in prepared_candidates}
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                if any(marker in message.lower() for marker in ("out of memory", "oom", "rate limit", "contention")):
                    execution["adaptive_reductions"].append(
                        {"candidate": candidate.name, "reason": message, "retry_parallel_sessions": 1}
                    )
                    try:
                        result = run(candidate, 1)
                    except Exception as retry_exc:
                        append_jsonl(
                            trace_path,
                            {"scope": scope, "candidate": candidate.name, "status": "INVALID", "error": str(retry_exc)},
                        )
                        results.append(invalid_result(candidate))
                        continue
                else:
                    append_jsonl(
                        trace_path,
                        {"scope": scope, "candidate": candidate.name, "status": "INVALID", "error": message},
                    )
                    results.append(invalid_result(candidate))
                    continue
            results.append(result)
            append_jsonl(
                trace_path,
                {
                    "scope": scope,
                    "candidate": result.name,
                    "candidate_hash": result.candidate_hash,
                    "stage": result.stage,
                    "status": result.status,
                    "metrics": result.metrics,
                    "runtime_seconds": result.runtime_seconds,
                    "cache_hits": result.cache_hits,
                    "cache_misses": result.cache_misses,
                },
            )
    return sorted(results, key=lambda result: result.name)


def _evaluate_loso_many(
    dataset: Dataset,
    candidates: Sequence[Candidate],
    folds: Sequence[Mapping[str, Any]],
    *,
    partition: str,
    scope: str,
    config: TunerConfig,
    shortterm_window: int,
    ranking_depth: int,
    registry: ArtifactRegistry,
    run_dir: Path,
    execution: dict[str, Any],
    trace_path: Path,
) -> list[CandidateResult]:
    """Evaluate every LOSO fold explicitly, then aggregate the frozen fold results."""
    if partition not in {"tune_sessions", "validation_sessions"}:
        raise ValueError(f"Unknown LOSO partition: {partition}")
    by_candidate: dict[str, list[CandidateResult]] = {candidate.name: [] for candidate in candidates}
    for fold in folds:
        fold_number = int(fold["fold"])
        fold_results = _evaluate_many(
            dataset,
            candidates,
            list(fold[partition]),
            scope=f"{scope}_fold_{fold_number}",
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )
        for result in fold_results:
            by_candidate[result.name].append(result)
    return sorted(
        [combine_candidate_results(by_candidate[candidate.name], target=config.target) for candidate in candidates],
        key=lambda result: result.name,
    )


def _load_space(skill_root: Path, overrides: Mapping[str, Any]) -> dict[str, Any]:
    with (skill_root / "search_space.yaml").open(encoding="utf-8") as handle:
        values = yaml.safe_load(handle)
    return _deep_merge(values, overrides)


def _resolve_shortterm_window(
    space: Mapping[str, Any],
    memory_config: Mapping[str, Any],
) -> tuple[int, dict[str, Any]]:
    configured_turns = int((space.get("dataset") or {}).get("shortterm_qa_turns", 3))
    capacity = (memory_config.get("midterm") or {}).get("short_term_capacity")
    if capacity is None:
        raise ValueError("memory_config.midterm.short_term_capacity is required")
    capacity_messages = int(capacity)
    if capacity_messages <= 0 or capacity_messages % 2:
        raise ValueError("memory_config.midterm.short_term_capacity must be a positive even message count")
    production_turns = capacity_messages // 2
    status = "MATCH" if configured_turns == production_turns else "OVERRIDDEN_BY_PRODUCTION_CONFIG"
    return production_turns, {
        "status": status,
        "dataset_config_qa_turns": configured_turns,
        "production_capacity_messages": capacity_messages,
        "actual_qa_turns": production_turns,
        "source": "memory_config.midterm.short_term_capacity / 2",
    }


def _artifact_cache_root(config: TunerConfig, run_dir: Path) -> Path:
    # The resume directory is the durable run identity. Derive its sibling
    # cache so callers need not repeat a custom output_dir on every resume.
    return run_dir.parent / ".cache" if config.resume else config.output_root.resolve() / ".cache"


def run_tuning(config: TunerConfig, *, skill_root: Path) -> Path:
    started = time.perf_counter()
    space = _load_space(skill_root, config.overrides)
    min_k = int((space.get("evaluation") or {}).get("min_k", 1))
    if config.k < min_k:
        raise ValueError(f"k must be >= {min_k}")
    if config.budget not in (space.get("budget") or {}).get("profiles", {}):
        raise ValueError(f"Unknown budget: {config.budget}")
    if config.target not in (space.get("target") or {}).get("allowed", []):
        raise ValueError(f"Unknown target: {config.target}")
    seed = int(config.seed if config.seed is not None else (space.get("split") or {}).get("seed", 42))
    ranking_depth = max(
        int(((space.get("execution") or {}).get("cache_rankings_depth") or {}).get("minimum", 20)),
        int(((space.get("execution") or {}).get("cache_rankings_depth") or {}).get("multiplier_of_k", 4)) * config.k,
    )
    execution = _execution_settings(config, space)
    if config.resume:
        run_dir = config.resume.resolve()
        run_dir.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_key = stable_hash({"dataset": str(config.dataset.resolve()), "k": config.k, "seed": seed})[:8]
        run_dir = (config.output_root / f"{stamp}-{run_key}").resolve()
        run_dir.mkdir(parents=True, exist_ok=False)
    previous_metadata: dict[str, Any] = {}
    if config.resume and (run_dir / "run_metadata.json").exists():
        loaded_metadata = load_json(run_dir / "run_metadata.json")
        if isinstance(loaded_metadata, dict):
            previous_metadata = loaded_metadata
    trace_path = run_dir / "search_trace.jsonl"
    if not trace_path.exists():
        trace_path.touch()
    research_trace_path = run_dir / "research_trace.jsonl"
    if not research_trace_path.exists():
        research_trace_path.touch()

    memory_config_values = load_json(config.memory_config)
    shortterm_window, shortterm_window_validation = _resolve_shortterm_window(space, memory_config_values)
    dataset, audit = audit_dataset(
        config.dataset,
        output_dir=run_dir,
        shortterm_qa_turns=shortterm_window,
        warning_config=(space.get("dataset") or {}).get("quality_warnings", {}),
        sessions=config.sessions,
        source_run=config.source_run,
    )
    split = create_or_load_split(dataset, output_dir=run_dir, seed=seed, shortterm_window=shortterm_window)
    loso = split["method"] == "leave_one_session_out"
    tune_sessions = sorted(dataset.sessions) if loso else split["tune_sessions"]
    profile = (space.get("budget") or {}).get("profiles", {})[config.budget]
    registry = ArtifactRegistry(_artifact_cache_root(config, run_dir), Path("exp/results").resolve())
    session_turn_counts = {session_id: len(turns) for session_id, turns in dataset.sessions.items()}
    generated_source = False
    source_generation_stats: dict[str, Any] = {}
    generated_trace_paths: list[Path] = []
    execution["source_worker_parallelism"] = 0
    midterm_baseline = registry.discover_production_midterm(
        dataset_sha256=dataset.sha256,
        session_turn_counts=session_turn_counts,
        ranking_depth=ranking_depth,
        memory_config_path=config.memory_config,
        llm_mode=config.llm_mode,
        source_run=config.source_run,
    )
    full_memory_regression_baseline = registry.discover_production_trace(
        dataset_path=Path(dataset.path),
        dataset_sha256=dataset.sha256,
        session_query_counts=session_turn_counts,
        memory_config_path=config.memory_config,
    )
    if full_memory_regression_baseline is not None:
        full_memory_regression_baseline = Candidate(
            name="full_memory_regression_baseline",
            stage="regression_baseline",
            config=full_memory_regression_baseline.config,
            provenance=full_memory_regression_baseline.provenance,
            complexity=full_memory_regression_baseline.complexity,
        )
    if midterm_baseline is None:
        generated_source = True
        execution["source_worker_parallelism"] = source_worker_parallelism(
            session_count=len(dataset.sessions),
            max_parallel_sessions=execution["max_parallel_sessions"],
            max_parallel_llm_calls=execution["max_parallel_llm_calls"],
        )
        manifest_paths = generate_production_sources(
            dataset_path=Path(dataset.path),
            dataset_sha256=dataset.sha256,
            session_ids=sorted(dataset.sessions),
            session_turn_counts=session_turn_counts,
            memory_config_path=config.memory_config,
            run_dir=run_dir,
            ranking_depth=ranking_depth,
            llm_mode=config.llm_mode,
            max_parallel_sessions=execution["max_parallel_sessions"],
            max_parallel_llm_calls=execution["max_parallel_llm_calls"],
            generation_stats=source_generation_stats,
        )
        generated_source = bool(source_generation_stats.get("generated_sessions", True))
        generated_trace_paths = [Path(path).parent / "recall_turn_results.jsonl" for path in manifest_paths]
        production_config, production_provenance = production_candidate_from_manifests(manifest_paths)
        midterm_baseline = Candidate(
            name="baseline",
            stage="baseline",
            config=production_config,
            provenance=production_provenance,
            complexity=0,
        )
    if midterm_baseline is None or midterm_baseline.config.get("backend") != "production_midterm":
        raise RuntimeError("Unable to establish a replayable production MidTerm baseline")

    # Source generation is intentionally completed before searching.  Re-scan
    # the newly generated exact traces so the baseline can also be evaluated as
    # Short + Mid + Session-Longterm, when the trace carries all layers.
    if full_memory_regression_baseline is None:
        full_memory_regression_baseline = registry.discover_production_trace(
            dataset_path=Path(dataset.path),
            dataset_sha256=dataset.sha256,
            session_query_counts=session_turn_counts,
            memory_config_path=config.memory_config,
            extra_trace_paths=generated_trace_paths,
        )
        if full_memory_regression_baseline is not None:
            full_memory_regression_baseline = Candidate(
                name="full_memory_regression_baseline",
                stage="regression_baseline",
                config=full_memory_regression_baseline.config,
                provenance=full_memory_regression_baseline.provenance,
                complexity=full_memory_regression_baseline.complexity,
            )

    def evaluate_tune_candidates(
        candidates: Sequence[Candidate],
        sessions_override: Sequence[str] | None = None,
        scope: str = "tune",
    ) -> list[CandidateResult]:
        common = {
            "config": config,
            "shortterm_window": shortterm_window,
            "ranking_depth": ranking_depth,
            "registry": registry,
            "run_dir": run_dir,
            "execution": execution,
            "trace_path": trace_path,
        }
        if loso and sessions_override is None:
            return _evaluate_loso_many(
                dataset,
                candidates,
                split["folds"],
                partition="tune_sessions",
                scope=f"{scope}_loso",
                **common,
            )
        sessions_to_use = list(sessions_override) if sessions_override is not None else tune_sessions
        return _evaluate_many(dataset, candidates, sessions_to_use, scope=scope, **common)

    baseline_tune = evaluate_tune_candidates([midterm_baseline], None, "baseline_tune")[0]
    tolerance = float((space.get("selection") or {}).get("tie_tolerance_pp", 0.25))
    branch_registry = BranchRegistry()
    try:
        free_disk_gib = shutil.disk_usage(config.output_root.resolve().parent).free / 1024**3
    except OSError:
        free_disk_gib = None
    resources = ResourceEnvelope(
        gpu_count=int(execution["gpu_count"]),
        gpu_memory_gib=_gpu_memory_gib(),
        available_memory_gib=execution.get("available_memory_gib"),
        free_disk_gib=free_disk_gib,
    )
    model_discovery = ModelDiscovery(
        output_path=run_dir / "model_discovery.json",
        resources=resources,
    )
    model_discovery.flush()
    research_settings = (space.get("search") or {}).get("research") or {}
    research_decider: ResearchDecisionEngine | None = None
    research_runtime: ResearchLLMRuntime | None = None
    if research_settings.get("enabled", True):
        research_runtime = ResearchLLMRuntime(memory_config=dict(memory_config_values), llm_mode=config.llm_mode)
        research_decider = ResearchDecisionEngine(
            runtime=research_runtime,
            artifact_registry=registry,
            trace_path=research_trace_path,
            max_attempts=int(research_settings.get("max_attempts") or 3),
        )
    search_result = run_staged_search(
        dataset=dataset,
        baseline=midterm_baseline,
        baseline_result=baseline_tune,
        tune_sessions=tune_sessions,
        registry=branch_registry,
        artifact_registry=registry,
        model_discovery=model_discovery,
        run_dir=run_dir,
        search_space=space,
        budget=config.budget,
        profile=profile,
        k=config.k,
        ranking_depth=ranking_depth,
        evaluate=evaluate_tune_candidates,
        diagnose=lambda result: _diagnose(result, audit, space),
        execution_settings=execution,
        research_decider=research_decider,
    )
    write_jsonl(run_dir / "branch_trace.jsonl", search_result.branch_events)
    for stage in search_result.stage_history:
        append_jsonl(
            trace_path,
            {
                "scope": "search_policy",
                "status": stage.get("status"),
                "stage_index": stage.get("stage_index"),
                "diagnostic_before": stage.get("diagnostic_before"),
                "diagnostic_after": stage.get("diagnostic_after"),
                "branches": stage.get("branches"),
                "coverage_before": stage.get("coverage_before"),
                "coverage_after": stage.get("coverage_after"),
                "patience_decision": stage.get("patience_decision"),
                "research_decision": stage.get("research_decision"),
            },
        )
    tune_results = search_result.tune_results
    diagnostics = search_result.diagnostics[-1]
    skipped = list(search_result.skipped_branches)

    if full_memory_regression_baseline is None:
        skipped.append(
            {
                "branch": "final_longterm_union_regression",
                "reason": "no exact complete production full-memory trace; MidTerm source generation intentionally avoided extra LongTerm LLM extraction",
            }
        )

    frontier = list(search_result.frontier[: int(profile["validation_frontier"])])
    if baseline_tune.name not in {result.name for result in frontier}:
        frontier.append(baseline_tune)
    validation_sessions = split["validation_sessions"] or split["tune_sessions"]
    validation_scope = "validation" if split["validation_sessions"] else "exploratory"
    if loso:
        validation_sessions = [fold["validation_sessions"][0] for fold in split["folds"]]
        validation_scope = "validation_loso"
    candidate_by_name = search_result.candidates
    validation_candidates = [candidate_by_name[result.name] for result in frontier if result.name in candidate_by_name]
    if loso:
        validation_results = _evaluate_loso_many(
            dataset,
            validation_candidates,
            split["folds"],
            partition="validation_sessions",
            scope=validation_scope,
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )
    else:
        validation_results = _evaluate_many(
            dataset,
            validation_candidates,
            validation_sessions,
            scope=validation_scope,
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )
    tune_by_name = {result.name: result for result in tune_results}
    validation_by_name = {result.name: result for result in validation_results}
    baseline_validation = validation_by_name["baseline"]
    # Temporal Replay is an independent phase. Execute it before the selection
    # boundary so status/provenance are available there; without Gold it remains
    # explicitly excluded and the ordinary Session winner is unchanged.
    temporal_replay = run_cross_session_temporal_replay(
        dataset=dataset,
        audit=audit,
        baseline=midterm_baseline,
        memory_config=memory_config_values,
        run_dir=run_dir,
    )
    best, overfit, selection = select_best(
        tune_by_name,
        validation_by_name,
        baseline_name="baseline",
        tie_tolerance_pp=tolerance,
        overfit_regression_pp=float(
            ((space.get("selection") or {}).get("overfit") or {}).get(
                "mark_if_tune_improves_and_validation_delta_pp_below", -0.5
            )
        ),
    )
    if loso:
        selection["validation_protocol"] = "leave_one_session_out_out_of_fold"
        selection["fold_count"] = len(split["folds"])

    midterm_baseline_full = _evaluate_many(
        dataset,
        [midterm_baseline],
        sorted(dataset.sessions),
        scope="baseline_final",
        config=config,
        shortterm_window=shortterm_window,
        ranking_depth=ranking_depth,
        registry=registry,
        run_dir=run_dir,
        execution=execution,
        trace_path=trace_path,
    )[0]
    final_candidate = candidate_by_name[best.name]
    final_result = _evaluate_many(
        dataset,
        [final_candidate],
        sorted(dataset.sessions),
        scope="final",
        config=config,
        shortterm_window=shortterm_window,
        ranking_depth=ranking_depth,
        registry=registry,
        run_dir=run_dir,
        execution=execution,
        trace_path=trace_path,
    )[0]
    # Selection metrics remain held-out; result files contain the complete selected-candidate evaluation.
    best.requirement_rows = final_result.requirement_rows
    best.session_rows = final_result.session_rows

    full_memory_regression_result: CandidateResult | None = None
    if full_memory_regression_baseline is not None:
        full_memory_regression_result = _evaluate_many(
            dataset,
            [full_memory_regression_baseline],
            sorted(dataset.sessions),
            scope="full_memory_regression",
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )[0]

    evaluation_results = [
        *tune_results,
        *validation_results,
        midterm_baseline_full,
        final_result,
        *([full_memory_regression_result] if full_memory_regression_result is not None else []),
    ]
    total_work = sum(result.work_seconds for result in evaluation_results)
    wall = time.perf_counter() - started
    reused = sorted(
        {artifact for result in evaluation_results for artifact in result.reused_artifacts if artifact}
        | set(search_result.reused_artifacts)
    )
    stop_reason = search_result.stop_reason
    previous_runtime = float(
        previous_metadata.get("cumulative_runtime_seconds") or previous_metadata.get("runtime_seconds") or 0
    )
    previous_work = float(
        previous_metadata.get("cumulative_serial_work_seconds")
        or previous_metadata.get("estimated_serial_work_seconds")
        or 0
    )
    cumulative_runtime = previous_runtime + wall
    cumulative_work = previous_work + total_work
    attempts = list(previous_metadata.get("attempts") or [])
    attempts.append(
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "runtime_seconds": wall,
            "serial_work_seconds": total_work,
            "resumed": bool(config.resume),
        }
    )
    source_llm_calls = (
        int(source_generation_stats.get("llm_calls") or 0)
        if "llm_calls" in source_generation_stats
        else int(midterm_baseline.provenance.get("llm_calls") or 0)
        if generated_source
        else 0
    )
    source_embedding_calls = (
        int(source_generation_stats.get("embedding_calls") or 0)
        if "embedding_calls" in source_generation_stats
        else int(midterm_baseline.provenance.get("embedding_calls") or 0)
        if generated_source
        else 0
    )
    evaluation_llm_calls = sum(result.llm_calls for result in evaluation_results)
    evaluation_embedding_calls = sum(result.embedding_calls for result in evaluation_results)
    current_research_stats = dict(search_result.research_stats or {})
    research_llm_calls = int(current_research_stats.get("llm_calls") or 0)
    if full_memory_regression_result is None:
        full_memory_regression = {
            "status": "SKIPPED",
            "backend": None,
            "reason": (
                "No complete production trace is available; ShortTerm/LongTerm/All-memory/Union "
                "regression was not inferred from MidTerm checkpoints."
            ),
            "metrics": None,
        }
    else:
        full_memory_regression = {
            "status": "AVAILABLE",
            "backend": full_memory_regression_baseline.config.get("backend"),
            "reason": None,
            "metrics": full_memory_regression_result.metrics,
        }

    stateful_results = [
        result for result in tune_results if bool(result.config.get("requires_within_session_replay"))
    ]
    stateful_status = "COMPLETE" if stateful_results else "NOT_RUN_BUDGET_OR_DIAGNOSTIC_GATE"
    run_metadata = {
        "status": "COMPLETE",
        "dataset": dataset.path,
        "dataset_sha256": dataset.sha256,
        "k": config.k,
        "secondary_cutoffs": [2 * config.k, 4 * config.k],
        "ranking_cache_depth": ranking_depth,
        "budget": config.budget,
        "budget_profile": dict(profile),
        "resolved_search_config": space.get("search") or {},
        "target": config.target,
        "seed": seed,
        "shortterm_qa_turns": shortterm_window,
        "shortterm_window_validation": shortterm_window_validation,
        "cross_session_gold_available": bool(audit.get("cross_session_gold_available", False)),
        "cross_session_temporal_status": temporal_replay["status"],
        "cross_session_temporal_replay": temporal_replay,
        "cross_session_winner_selection_enabled": temporal_replay["winner_selection_enabled"],
        "stateful_replay_status": stateful_status,
        "stateful_replay_candidates": [result.name for result in stateful_results],
        "run_dir": str(run_dir),
        "execution": execution,
        "runtime_seconds": cumulative_runtime,
        "attempt_runtime_seconds": wall,
        "estimated_serial_work_seconds": cumulative_work,
        "cumulative_runtime_seconds": cumulative_runtime,
        "cumulative_serial_work_seconds": cumulative_work,
        "parallel_time_saved_seconds": max(0.0, cumulative_work - cumulative_runtime),
        "llm_calls": int(previous_metadata.get("llm_calls") or 0)
        + source_llm_calls
        + search_result.llm_calls
        + research_llm_calls
        + evaluation_llm_calls,
        "embedding_calls": int(previous_metadata.get("embedding_calls") or 0)
        + source_embedding_calls
        + search_result.embedding_calls
        + evaluation_embedding_calls,
        "source_generation_llm_calls": source_llm_calls,
        "source_generation_embedding_calls": source_embedding_calls,
        "evaluation_llm_calls": evaluation_llm_calls,
        "evaluation_embedding_calls": evaluation_embedding_calls,
        "branch_generation_llm_calls": search_result.llm_calls,
        "branch_generation_embedding_calls": search_result.embedding_calls,
        "research_llm_calls": int(previous_metadata.get("research_llm_calls") or 0) + research_llm_calls,
        "research_llm_successful_calls": int(previous_metadata.get("research_llm_successful_calls") or 0)
        + int(current_research_stats.get("successful_calls") or 0),
        "research_llm_failed_calls": int(previous_metadata.get("research_llm_failed_calls") or 0)
        + int(current_research_stats.get("failed_calls") or 0),
        "research_decisions": int(previous_metadata.get("research_decisions") or 0)
        + int(current_research_stats.get("decisions") or 0),
        "research_fallback_decisions": int(previous_metadata.get("research_fallback_decisions") or 0)
        + int(current_research_stats.get("fallback_decisions") or 0),
        "research_cache_hits": int(previous_metadata.get("research_cache_hits") or 0)
        + int(current_research_stats.get("cache_hits") or 0),
        "research_decision_records": search_result.research_decisions,
        "research_trace_path": str(research_trace_path),
        "research_enabled": research_decider is not None,
        "research_model_config": research_runtime.model_config if research_runtime is not None else None,
        "reused_artifacts": sorted(set(previous_metadata.get("reused_artifacts") or []) | set(reused)),
        "failed_turns": 0,
        "stop_reason": stop_reason,
        "resume_supported": True,
        "source_run": str(config.source_run) if config.source_run else None,
        "baseline_backend": midterm_baseline.config.get("backend"),
        "baseline_provenance": midterm_baseline.provenance,
        "midterm_baseline_backend": midterm_baseline.config.get("backend"),
        "midterm_baseline_provenance": midterm_baseline.provenance,
        "full_memory_regression_baseline_backend": full_memory_regression.get("backend"),
        "full_memory_regression_baseline_provenance": (
            full_memory_regression_baseline.provenance if full_memory_regression_baseline is not None else None
        ),
        "production_source_generated": generated_source,
        "production_source_generation_stats": source_generation_stats,
        "midterm_baseline_full_metrics": midterm_baseline_full.metrics,
        "full_memory_regression": full_memory_regression,
        "branch_registry": branch_registry.describe(),
        "stage_history": search_result.stage_history,
        "diagnostics_history": search_result.diagnostics,
        "branch_events": search_result.branch_events,
        "branch_coverage": search_result.coverage_audit,
        "model_discovery_path": str(run_dir / "model_discovery.json"),
        "resource_envelope": {
            "gpu_count": resources.gpu_count,
            "gpu_memory_gib": resources.gpu_memory_gib,
            "available_memory_gib": resources.available_memory_gib,
            "free_disk_gib": resources.free_disk_gib,
        },
        "attempts": attempts,
    }
    write_outputs(
        run_dir=run_dir,
        dataset_audit=audit,
        split=split,
        k=config.k,
        baseline_tune=baseline_tune,
        baseline_validation=baseline_validation,
        tune_results=tune_results,
        validation_by_name=validation_by_name,
        best=best,
        selection=selection,
        overfit=overfit,
        skipped_branches=skipped,
        diagnostics=diagnostics,
        stop_reason=stop_reason,
        run_metadata=run_metadata,
    )
    return run_dir
