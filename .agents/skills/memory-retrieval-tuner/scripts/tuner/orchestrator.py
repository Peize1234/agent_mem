from __future__ import annotations

import copy
import os
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
from .candidate_selector import select_best, tune_frontier
from .dataset_audit import audit_dataset
from .evaluate_candidate import BM25_BACKEND, evaluate_candidate
from .io_utils import append_jsonl, load_json, stable_hash
from .models import Candidate, CandidateResult, Dataset
from .split_sessions import create_or_load_split


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


def _baseline(k: int, ranking_depth: int) -> Candidate:
    values = {
        "backend": "offline_repository_adapter",
        "query_representation": "original",
        "page_representation": "production",
        "retrieval_method": "bm25",
        "top_k_pages": ranking_depth,
        "page_similarity_threshold": 0.0,
    }
    if BM25_BACKEND.startswith("compatibility_fallback"):
        values["backend_implementation"] = BM25_BACKEND
    return Candidate(
        name="baseline",
        stage="baseline",
        config=values,
        provenance={"adapter": "exp.benchmark.midterm_retrieval_eval.ChineseBM25Index", "k": k},
        complexity=0,
    )


def _candidate(name: str, stage: str, baseline: Candidate, complexity: int = 1, **changes: Any) -> Candidate:
    values = dict(baseline.config)
    values.update(changes)
    return Candidate(name=name, stage=stage, config=values, complexity=complexity)


def _cheap_candidates(
    baseline: Candidate,
    space: Mapping[str, Any],
    maximum: int,
    *,
    k: int,
    ranking_depth: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    page_values = (((space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
        "page_representation", {}
    ).get("values", [])
    for representation in page_values:
        if representation == "production":
            continue
        candidates.append(
            _candidate(
                f"page:{representation}",
                "cheap_page_representation",
                baseline,
                page_representation=representation,
            )
        )
    candidates.extend(
        [
            _candidate("retrieval:question_bm25", "cheap_retrieval", baseline, retrieval_method="question_bm25"),
            _candidate(
                "retrieval:hybrid_w0.50",
                "cheap_retrieval",
                baseline,
                retrieval_method="hybrid_bm25",
                embedding_similarity_weight=0.50,
                complexity=2,
            ),
            _candidate(
                "retrieval:hybrid_w0.75",
                "cheap_retrieval",
                baseline,
                retrieval_method="hybrid_bm25",
                embedding_similarity_weight=0.75,
                complexity=2,
            ),
            *[
                _candidate(
                    f"candidate_pool:{pool}",
                    "cheap_candidate_pool",
                    baseline,
                    top_k_pages=pool,
                )
                for pool in sorted({k, 2 * k, max(k, ranking_depth - 5)})
                if pool != int(baseline.config.get("top_k_pages") or ranking_depth)
            ],
            _candidate("threshold:0.20", "cheap_threshold", baseline, page_similarity_threshold=0.20),
            _candidate("threshold:0.40", "cheap_threshold", baseline, page_similarity_threshold=0.40),
        ]
    )
    # This is coordinate search: every candidate changes one axis from baseline.
    return candidates[:maximum]


def _diagnose(best: CandidateResult, audit: Mapping[str, Any], space: Mapping[str, Any]) -> dict[str, Any]:
    metrics = best.metrics
    gap_pp = (float(metrics.get("recall_at_4k") or 0) - float(metrics.get("recall_at_k") or 0)) * 100.0
    deep_recall = float(metrics.get("recall_at_4k") or 0) * 100.0
    stddev_pp = float(metrics.get("session_stddev") or 0) * 100.0
    settings = space.get("diagnostics") or {}
    if audit.get("warnings"):
        regime = "data_artifact_suspicion"
    elif stddev_pp >= float((settings.get("session_instability") or {}).get("max_stddev_pp", 12.0)):
        regime = "session_instability"
    elif gap_pp >= float((settings.get("ranking_bottleneck") or {}).get("min_recall_4k_minus_k_pp", 20.0)):
        regime = "ranking_bottleneck"
    elif deep_recall <= float((settings.get("candidate_coverage_bottleneck") or {}).get("max_recall_4k_percent", 65.0)):
        regime = "candidate_coverage_bottleneck"
    else:
        regime = "balanced_or_plateau"
    return {"regime": regime, "recall_4k_minus_k_pp": gap_pp, "recall_4k_percent": deep_recall, "stddev_pp": stddev_pp}


def _secondary_candidates(baseline: Candidate, regime: str) -> list[Candidate]:
    if regime == "ranking_bottleneck":
        return [
            _candidate(
                f"secondary:hybrid_w{weight:.2f}",
                "secondary_ranking",
                baseline,
                retrieval_method="hybrid_bm25",
                embedding_similarity_weight=weight,
                complexity=2,
            )
            for weight in (0.60, 0.70, 0.85)
        ]
    if regime == "candidate_coverage_bottleneck":
        return [
            _candidate("secondary:full", "secondary_coverage", baseline, page_representation="full"),
            _candidate("secondary:summary_keywords", "secondary_coverage", baseline, page_representation="summary_keywords"),
        ]
    if regime == "session_instability":
        return [
            _candidate("secondary:simple_summary", "secondary_stability", baseline, page_representation="summary"),
            _candidate("secondary:original_simple", "secondary_stability", baseline),
        ]
    return []


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
    candidate_parallelism = min(execution["max_parallel_candidates"], len(candidates) or 1)
    session_parallelism = execution["max_parallel_sessions"] if candidate_parallelism == 1 else 1

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

    with ThreadPoolExecutor(max_workers=max(1, candidate_parallelism)) as executor:
        futures = {executor.submit(run, candidate, session_parallelism): candidate for candidate in candidates}
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
                        continue
                else:
                    append_jsonl(
                        trace_path,
                        {"scope": scope, "candidate": candidate.name, "status": "INVALID", "error": message},
                    )
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


def _load_space(skill_root: Path, overrides: Mapping[str, Any]) -> dict[str, Any]:
    with (skill_root / "search_space.yaml").open(encoding="utf-8") as handle:
        values = yaml.safe_load(handle)
    return _deep_merge(values, overrides)


def run_tuning(config: TunerConfig, *, skill_root: Path) -> Path:
    started = time.perf_counter()
    if config.k < 1:
        raise ValueError("k must be >= 1")
    space = _load_space(skill_root, config.overrides)
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

    shortterm_window = int((space.get("dataset") or {}).get("shortterm_qa_turns", 3))
    dataset, audit = audit_dataset(
        config.dataset,
        output_dir=run_dir,
        shortterm_qa_turns=shortterm_window,
        warning_config=(space.get("dataset") or {}).get("quality_warnings", {}),
        sessions=config.sessions,
        source_run=config.source_run,
    )
    split = create_or_load_split(dataset, output_dir=run_dir, seed=seed, shortterm_window=shortterm_window)
    profile = (space.get("budget") or {}).get("profiles", {})[config.budget]
    registry = ArtifactRegistry(config.output_root.resolve() / ".cache", Path("exp/results").resolve())
    offline_anchor = _baseline(config.k, ranking_depth)
    production_baseline = registry.discover_production_trace(
        dataset_path=Path(dataset.path),
        dataset_sha256=dataset.sha256,
        session_query_counts={session_id: len(turns) for session_id, turns in dataset.sessions.items()},
    )
    baseline = production_baseline or offline_anchor

    baseline_tune = _evaluate_many(
        dataset,
        [baseline],
        split["tune_sessions"],
        scope="tune",
        config=config,
        shortterm_window=shortterm_window,
        ranking_depth=ranking_depth,
        registry=registry,
        run_dir=run_dir,
        execution=execution,
        trace_path=trace_path,
    )[0]
    maximum_cheap = int(profile["max_cheap_candidates"])
    generic_cheap = _cheap_candidates(
        offline_anchor,
        space,
        maximum_cheap,
        k=config.k,
        ranking_depth=ranking_depth,
    )
    frozen_queries = registry.discover_frozen_queries(
        dataset.sha256,
        query_text_by_id={
            turn.query_id: turn.question
            for session_turns in dataset.sessions.values()
            for turn in session_turns
        },
        base_candidate=offline_anchor,
        limit=min(4, maximum_cheap),
    )
    cheap = [*frozen_queries, *generic_cheap][:maximum_cheap]
    if production_baseline is not None and maximum_cheap > 0:
        cheap = [
            Candidate(
                name="offline:original_bm25",
                stage="cheap_offline_adapter",
                config=offline_anchor.config,
                provenance=offline_anchor.provenance,
                complexity=offline_anchor.complexity,
            ),
            *cheap,
        ][:maximum_cheap]
    cheap_results = _evaluate_many(
        dataset,
        cheap,
        split["tune_sessions"],
        scope="tune",
        config=config,
        shortterm_window=shortterm_window,
        ranking_depth=ranking_depth,
        registry=registry,
        run_dir=run_dir,
        execution=execution,
        trace_path=trace_path,
    )
    tune_results = [baseline_tune, *cheap_results]
    tolerance = float((space.get("selection") or {}).get("tie_tolerance_pp", 0.25))
    provisional = tune_frontier(tune_results, tolerance_pp=tolerance, limit=max(2, int(profile["validation_frontier"])))
    best_tune = provisional[0] if provisional else baseline_tune
    diagnostics = _diagnose(best_tune, audit, space)
    skipped: list[dict[str, Any]] = []

    secondary = _secondary_candidates(offline_anchor, str(diagnostics["regime"]))
    max_branches = int(profile["max_secondary_branches"])
    secondary = secondary[: max_branches * 3]
    if secondary:
        secondary_results = _evaluate_many(
            dataset,
            secondary,
            split["tune_sessions"],
            scope="tune",
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )
        tune_results.extend(secondary_results)
    else:
        skipped.append({"branch": "secondary", "reason": f"diagnostic regime {diagnostics['regime']} did not justify it"})
    skipped.append(
        {
            "branch": "top_k_sessions",
            "reason": "generic source-turn adapter has no Session router; production trace is read-only",
        }
    )

    frozen_limit = max(2, int(profile["max_expensive_candidates"]))
    frozen = registry.discover_frozen_rankings(dataset.sha256, limit=frozen_limit)
    if frozen:
        replay_results = _evaluate_many(
            dataset,
            frozen,
            split["tune_sessions"],
            scope="tune",
            config=config,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            run_dir=run_dir,
            execution=execution,
            trace_path=trace_path,
        )
        tune_results.extend(replay_results)
    else:
        skipped.append({"branch": "frozen_replay", "reason": "no complete exact-provenance frozen ranking found"})

    if int(profile["max_expensive_candidates"]) == 0:
        skipped.append({"branch": "expensive_llm", "reason": "quick budget disables new LLM generation"})
    elif diagnostics["regime"] == "data_artifact_suspicion":
        skipped.append({"branch": "expensive_llm", "reason": "dataset artifact warning; new LLM tuning suppressed"})
    elif not frozen:
        skipped.append(
            {
                "branch": "expensive_llm",
                "reason": "no exact-provenance frozen candidate; no repository-native no-call generator available",
            }
        )

    frontier = tune_frontier(tune_results, tolerance_pp=tolerance, limit=int(profile["validation_frontier"]))
    if baseline_tune.name not in {result.name for result in frontier}:
        frontier.append(baseline_tune)
    validation_sessions = split["validation_sessions"] or split["tune_sessions"]
    validation_scope = "validation" if split["validation_sessions"] else "exploratory"
    if split["method"] == "leave_one_session_out":
        validation_sessions = [fold["validation_sessions"][0] for fold in split["folds"]]
        validation_scope = "validation_loso"
    candidate_by_name = {candidate.name: candidate for candidate in [baseline, *cheap, *secondary, *frozen]}
    validation_candidates = [candidate_by_name[result.name] for result in frontier if result.name in candidate_by_name]
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

    baseline_full = _evaluate_many(
        dataset,
        [baseline],
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
    if config.target == "midterm" and baseline_full.metrics.get("longterm_recall_at_k") is not None:
        best.metrics["longterm_recall_at_k"] = baseline_full.metrics["longterm_recall_at_k"]
        best.metrics["longterm_metric_source"] = "unchanged_production_baseline"

    total_work = sum(result.work_seconds for result in [*tune_results, *validation_results, baseline_full, final_result])
    wall = time.perf_counter() - started
    reused = sorted(
        {
            artifact
            for result in [*tune_results, *validation_results, baseline_full, final_result]
            for artifact in result.reused_artifacts
            if artifact
        }
    )
    stop_reason = "validation_frontier_selected"
    previous_runtime = float(previous_metadata.get("cumulative_runtime_seconds") or previous_metadata.get("runtime_seconds") or 0)
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
    run_metadata = {
        "status": "COMPLETE",
        "dataset": dataset.path,
        "dataset_sha256": dataset.sha256,
        "k": config.k,
        "secondary_cutoffs": [2 * config.k, 4 * config.k],
        "ranking_cache_depth": ranking_depth,
        "budget": config.budget,
        "target": config.target,
        "seed": seed,
        "run_dir": str(run_dir),
        "execution": execution,
        "runtime_seconds": cumulative_runtime,
        "attempt_runtime_seconds": wall,
        "estimated_serial_work_seconds": cumulative_work,
        "cumulative_runtime_seconds": cumulative_runtime,
        "cumulative_serial_work_seconds": cumulative_work,
        "parallel_time_saved_seconds": max(0.0, cumulative_work - cumulative_runtime),
        "llm_calls": int(previous_metadata.get("llm_calls") or 0)
        + sum(result.llm_calls for result in tune_results),
        "embedding_calls": int(previous_metadata.get("embedding_calls") or 0)
        + sum(result.embedding_calls for result in tune_results),
        "reused_artifacts": sorted(set(previous_metadata.get("reused_artifacts") or []) | set(reused)),
        "failed_turns": 0,
        "stop_reason": stop_reason,
        "resume_supported": True,
        "source_run": str(config.source_run) if config.source_run else None,
        "baseline_backend": baseline.config.get("backend"),
        "baseline_provenance": baseline.provenance,
        "baseline_full_metrics": baseline_full.metrics,
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
