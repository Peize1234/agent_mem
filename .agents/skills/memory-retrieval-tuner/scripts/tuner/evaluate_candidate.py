from __future__ import annotations

import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_registry import ArtifactRegistry
from .io_utils import atomic_write_json, load_jsonl, stable_hash
from .models import Candidate, CandidateResult, Dataset, Requirement, Turn
from .production_midterm_adapter import PRODUCTION_BACKEND, ProductionMidtermAdapter, load_checkpoints


def candidate_hash(dataset_sha256: str, candidate: Candidate) -> str:
    return stable_hash({"dataset_sha256": dataset_sha256, "config": candidate.config})


def _eligible_requirements(
    turn: Turn, session_turns: Sequence[Turn], target: str, shortterm_window: int
) -> list[Requirement]:
    if target == "all_memory":
        return list(turn.requirements)
    shortterm_ids = {
        item.query_id for item in session_turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]
    }
    return [
        requirement
        for requirement in turn.requirements
        if not any(member in shortterm_ids for member in requirement.members)
    ]


def _apply_retrieval_controls(
    ranking: Sequence[Mapping[str, Any]], config: Mapping[str, Any], ranking_depth: int
) -> list[dict[str, Any]]:
    del config
    rows = [dict(row) for row in ranking]
    rows = rows[:ranking_depth]
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _load_frozen_rankings(config: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows = load_jsonl(Path(str(config["ranking_path"])))
    variant_key = config.get("variant_key")
    variant = str(config.get("variant") or "default")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if variant_key and str(row.get(str(variant_key)) or "default") != variant:
            continue
        query_id = str(row.get("query_id") or "").upper()
        source_turn_id = str(row.get("source_turn_id") or row.get("page_id") or "").upper()
        if not query_id or not source_turn_id:
            continue
        grouped[query_id].append(
            {
                "page_id": source_turn_id,
                "source_turn_id": source_turn_id,
                "rank": int(row.get("rank") or row.get("c3_rank") or len(grouped[query_id]) + 1),
                "score": float(row.get("score") or 0.0),
            }
        )
    for query_id in grouped:
        grouped[query_id].sort(key=lambda row: (int(row["rank"]), str(row["page_id"])))
    return grouped


def _load_production_trace_rankings(config: Mapping[str, Any], target: str) -> dict[str, list[dict[str, Any]]]:
    field = {
        "midterm": "mid_retrieved_turn_ids",
        "longterm": "long_retrieved_turn_ids",
        "all_memory": "all_retrieved_turn_ids",
    }[target]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_path in config.get("trace_paths") or []:
        for row in load_jsonl(Path(str(raw_path))):
            query_id = str(row.get("turn_id") or row.get("query_id") or "").upper()
            if not query_id or row.get("error"):
                continue
            retrieved = [str(value).upper() for value in row.get(field) or []]
            grouped[query_id] = [
                {"page_id": source_turn_id, "source_turn_id": source_turn_id, "rank": rank, "score": 0.0}
                for rank, source_turn_id in enumerate(dict.fromkeys(retrieved), start=1)
            ]
    return grouped


def _rank_session(
    dataset: Dataset,
    session_id: str,
    candidate: Candidate,
    *,
    k: int,
    target: str,
    shortterm_window: int,
    ranking_depth: int,
    registry: ArtifactRegistry,
    frozen: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    checkpoints: Mapping[str, Mapping[str, Any]] | None,
    run_dir: Path,
    candidate_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    ranking_config = dict(candidate.config)
    raw_depth = ranking_depth
    identity = {
        "schema": 1,
        "dataset_sha256": dataset.sha256,
        "session_id": session_id,
        "target": target,
        "shortterm_window": shortterm_window,
        "ranking_config": ranking_config,
    }
    cached = registry.get_ranking(identity, raw_depth)
    if cached is not None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cached["rows"]:
            grouped[str(row["query_id"])].append({key: value for key, value in row.items() if key != "query_id"})
        return {
            query_id: _apply_retrieval_controls(ranking, candidate.config, ranking_depth)
            for query_id, ranking in grouped.items()
        }, True

    session_turns = dataset.sessions[session_id]
    grouped = {}
    flat: list[dict[str, Any]] = []
    adapter = (
        ProductionMidtermAdapter(
            run_dir=run_dir,
            candidate_hash=candidate_id,
            session_id=session_id,
            ranking_depth=raw_depth,
        )
        if candidate.config.get("backend") == PRODUCTION_BACKEND
        else None
    )
    for turn in session_turns:
        if not _eligible_requirements(turn, session_turns, target, shortterm_window):
            continue
        backend = candidate.config.get("backend")
        if backend in {"frozen_ranking", "production_trace"}:
            if backend == "production_trace" and turn.query_id not in (frozen or {}):
                raise ValueError(f"Production trace is incomplete for eligible Query {turn.query_id}")
            ranking = [dict(row) for row in (frozen or {}).get(turn.query_id, [])]
            if backend == "frozen_ranking" and not ranking:
                raise ValueError(f"Frozen ranking is incomplete for eligible Query {turn.query_id}")
        elif backend == PRODUCTION_BACKEND:
            checkpoint = (checkpoints or {}).get(turn.query_id)
            if checkpoint is None:
                raise ValueError(f"Production checkpoint is incomplete for eligible Query {turn.query_id}")
            ranking = adapter.rank(checkpoint, candidate.config) if adapter is not None else []
        else:
            raise ValueError(
                f"Candidate {candidate.name} does not use a supported production/frozen backend: {backend!r}"
            )
        raw_ranking = ranking[:raw_depth]
        grouped[turn.query_id] = _apply_retrieval_controls(raw_ranking, candidate.config, ranking_depth)
        flat.extend({"query_id": turn.query_id, **row} for row in raw_ranking)
    registry.store_ranking(identity, flat, raw_depth)
    return grouped, False


def _evaluate_session(
    dataset: Dataset,
    session_id: str,
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    k: int,
    target: str,
    shortterm_window: int,
) -> dict[str, Any]:
    session_turns = dataset.sessions[session_id]
    requirement_rows: list[dict[str, Any]] = []
    shortterm_total = 0
    shortterm_hits = 0
    for turn in session_turns:
        shortterm_ids = [
            item.query_id for item in session_turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]
        ]
        shortterm_total += len(turn.requirements)
        shortterm_hits += sum(
            any(member in shortterm_ids for member in requirement.members) for requirement in turn.requirements
        )
        eligible = _eligible_requirements(turn, session_turns, target, shortterm_window)
        if not eligible:
            continue
        ranked_ids = [str(row.get("source_turn_id") or row.get("page_id")) for row in rankings.get(turn.query_id, [])]
        rank_by_id = {page_id: rank for rank, page_id in enumerate(ranked_ids, start=1)}
        for group_index, requirement in enumerate(eligible, start=1):
            member_ranks = [rank_by_id[member] for member in requirement.members if member in rank_by_id]
            best_rank = min(member_ranks) if member_ranks else None
            requirement_rows.append(
                {
                    "requirement_id": f"{turn.query_id}::G{group_index}",
                    "session_id": session_id,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "gold_members": list(requirement.members),
                    "is_or": requirement.is_or,
                    "best_rank": best_rank,
                    "hit_at_k": bool(best_rank is not None and best_rank <= k),
                    "hit_at_2k": bool(best_rank is not None and best_rank <= 2 * k),
                    "hit_at_4k": bool(best_rank is not None and best_rank <= 4 * k),
                    "reciprocal_rank": 1.0 / best_rank if best_rank else 0.0,
                }
            )
    total = len(requirement_rows)

    def recall(field: str) -> float:
        return sum(bool(row[field]) for row in requirement_rows) / total if total else 0.0

    ranks = [int(row["best_rank"]) for row in requirement_rows if row["best_rank"] is not None]
    metrics = {
        "session_id": session_id,
        "evaluated_query_count": len({row["query_id"] for row in requirement_rows}),
        "eligible_requirement_count": total,
        "recall_at_k": recall("hit_at_k"),
        "recall_at_2k": recall("hit_at_2k"),
        "recall_at_4k": recall("hit_at_4k"),
        "mrr": statistics.fmean(row["reciprocal_rank"] for row in requirement_rows) if total else 0.0,
        "mean_gold_rank": statistics.fmean(ranks) if ranks else None,
        "median_gold_rank": statistics.median(ranks) if ranks else None,
        "shortterm_coverage": shortterm_hits / shortterm_total if shortterm_total else 0.0,
        "shortterm_requirement_count": shortterm_hits,
        "total_gold_requirement_count": shortterm_total,
    }
    metrics["target_layer_union"] = (shortterm_hits + sum(row["hit_at_k"] for row in requirement_rows)) / max(
        shortterm_total, 1
    )
    metrics["all_memory_union"] = None
    metrics["query_completion"] = None
    return {"metrics": metrics, "requirements": requirement_rows}


def _aggregate(
    session_results: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    session_rows = [dict(result["metrics"]) for result in session_results]
    requirement_rows = [dict(row) for result in session_results for row in result["requirements"]]
    total = len(requirement_rows)

    def hit(field: str) -> float:
        return sum(bool(row[field]) for row in requirement_rows) / total if total else 0.0

    ranks = [int(row["best_rank"]) for row in requirement_rows if row["best_rank"] is not None]
    recalls = [float(row["recall_at_k"]) for row in session_rows]
    total_gold = sum(int(row["total_gold_requirement_count"]) for row in session_rows)
    short_hits = sum(int(row["shortterm_requirement_count"]) for row in session_rows)
    target_hits = sum(bool(row["hit_at_k"]) for row in requirement_rows)
    metrics = {
        "evaluated_query_count": len({row["query_id"] for row in requirement_rows}),
        "eligible_requirement_count": total,
        "recall_at_k": hit("hit_at_k"),
        "recall_at_2k": hit("hit_at_2k"),
        "recall_at_4k": hit("hit_at_4k"),
        "macro_session_recall_at_k": statistics.fmean(recalls) if recalls else 0.0,
        "session_stddev": statistics.pstdev(recalls) if len(recalls) > 1 else 0.0,
        "worst_session_recall_at_k": min(recalls) if recalls else 0.0,
        "mrr": statistics.fmean(row["reciprocal_rank"] for row in requirement_rows) if total else 0.0,
        "mean_gold_rank": statistics.fmean(ranks) if ranks else None,
        "median_gold_rank": statistics.median(ranks) if ranks else None,
        "shortterm_coverage": short_hits / total_gold if total_gold else 0.0,
        "target_layer_union": (short_hits + target_hits) / total_gold if total_gold else 0.0,
        "all_memory_union": None,
        "query_completion": None,
    }
    return metrics, requirement_rows, session_rows


def combine_candidate_results(
    results: Sequence[CandidateResult],
    *,
    target: str,
) -> CandidateResult:
    """Combine independently evaluated LOSO folds without hiding fold boundaries."""
    if not results:
        raise ValueError("Cannot combine an empty Candidate result set")
    first = results[0]
    if any(result.candidate_hash != first.candidate_hash for result in results):
        raise ValueError("LOSO fold results belong to different Candidates")

    session_results: list[dict[str, Any]] = []
    for result in results:
        requirements_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in result.requirement_rows:
            requirements_by_session[str(row["session_id"])].append(dict(row))
        for session_row in result.session_rows:
            session_id = str(session_row["session_id"])
            session_results.append(
                {
                    "metrics": dict(session_row),
                    "requirements": requirements_by_session.get(session_id, []),
                }
            )

    metrics, requirement_rows, session_rows = _aggregate(session_results)
    metrics[f"{target}_recall_at_k"] = metrics["recall_at_k"]
    return CandidateResult(
        name=first.name,
        candidate_hash=first.candidate_hash,
        stage=first.stage,
        config=first.config,
        metrics=metrics,
        requirement_rows=requirement_rows,
        session_rows=session_rows,
        runtime_seconds=sum(result.runtime_seconds for result in results),
        work_seconds=sum(result.work_seconds for result in results),
        cache_hits=sum(result.cache_hits for result in results),
        cache_misses=sum(result.cache_misses for result in results),
        llm_calls=sum(result.llm_calls for result in results),
        embedding_calls=sum(result.embedding_calls for result in results),
        reused_artifacts=sorted({value for result in results for value in result.reused_artifacts}),
        complexity=first.complexity,
        status="VALID" if all(result.status == "VALID" for result in results) else "INVALID",
    )


def _production_layer_metrics(
    dataset: Dataset,
    sessions: Sequence[str],
    config: Mapping[str, Any],
    *,
    k: int,
    shortterm_window: int,
) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for layer in ("midterm", "longterm", "all_memory"):
        rankings = _load_production_trace_rankings(config, layer)
        evaluated = [
            _evaluate_session(
                dataset,
                session_id,
                rankings,
                k=k,
                target=layer,
                shortterm_window=shortterm_window,
            )
            for session_id in sessions
        ]
        layer_metrics, _, _ = _aggregate(evaluated)
        values[f"{layer}_recall_at_k"] = layer_metrics["recall_at_k"]
    values["all_memory_union"] = values["all_memory_recall_at_k"]
    values["query_completion"] = values["all_memory_recall_at_k"]
    return values


def evaluate_candidate(
    dataset: Dataset,
    candidate: Candidate,
    sessions: Sequence[str],
    *,
    scope: str,
    k: int,
    target: str,
    shortterm_window: int,
    ranking_depth: int,
    max_parallel_sessions: int,
    registry: ArtifactRegistry,
    run_dir: Path,
) -> CandidateResult:
    started = time.perf_counter()
    backend = candidate.config.get("backend")
    if backend == "frozen_ranking":
        frozen = _load_frozen_rankings(candidate.config)
    elif backend == "production_trace":
        frozen = _load_production_trace_rankings(candidate.config, target)
    else:
        frozen = None
    checkpoints = (
        load_checkpoints(candidate.config.get("manifest_paths") or []) if backend == PRODUCTION_BACKEND else None
    )
    candidate_id = candidate_hash(dataset.sha256, candidate)
    cache_hits = 0
    cache_misses = 0
    work_seconds = 0.0

    def run_session(session_id: str) -> tuple[dict[str, Any], bool, float]:
        session_started = time.perf_counter()
        session_turns = dataset.sessions[session_id]
        expected_queries = sum(
            bool(_eligible_requirements(turn, session_turns, target, shortterm_window)) for turn in session_turns
        )
        identity = {
            "dataset_sha256": dataset.sha256,
            "candidate_hash": candidate_id,
            "scope": scope,
            "session_id": session_id,
            "k": k,
            "target": target,
            "shortterm_window": shortterm_window,
        }
        evaluation_scope = f"{scope}__k{k}__{target}__short{shortterm_window}"
        worker_path = registry.worker_path(run_dir, candidate_id, evaluation_scope, session_id)
        resumed = registry.valid_worker(worker_path, expected_queries=expected_queries, identity=identity)
        if resumed is not None:
            return resumed["result"], True, time.perf_counter() - session_started
        rankings, ranking_cache_hit = _rank_session(
            dataset,
            session_id,
            candidate,
            k=k,
            target=target,
            shortterm_window=shortterm_window,
            ranking_depth=ranking_depth,
            registry=registry,
            frozen=frozen,
            checkpoints=checkpoints,
            run_dir=run_dir,
            candidate_id=candidate_id,
        )
        result = _evaluate_session(
            dataset,
            session_id,
            rankings,
            k=k,
            target=target,
            shortterm_window=shortterm_window,
        )
        if int(result["metrics"]["evaluated_query_count"]) != expected_queries:
            raise RuntimeError(
                f"Incomplete Session evaluation {session_id}: "
                f"{result['metrics']['evaluated_query_count']}/{expected_queries} Queries"
            )
        atomic_write_json(
            worker_path,
            {
                "status": "COMPLETE",
                "identity": identity,
                "evaluated_query_count": expected_queries,
                "failed_turns": 0,
                "ranking_cache_hit": ranking_cache_hit,
                "result": result,
            },
        )
        return result, ranking_cache_hit, time.perf_counter() - session_started

    completed: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel_sessions, len(sessions) or 1))) as executor:
        futures = {executor.submit(run_session, session_id): session_id for session_id in sessions}
        for future in as_completed(futures):
            session_id = futures[future]
            result, hit_cache, elapsed = future.result()
            completed[session_id] = result
            cache_hits += int(hit_cache)
            cache_misses += int(not hit_cache)
            work_seconds += elapsed
    ordered = [completed[session_id] for session_id in sessions]
    metrics, requirements, session_rows = _aggregate(ordered)
    if backend == "production_trace":
        metrics.update(
            _production_layer_metrics(
                dataset,
                sessions,
                candidate.config,
                k=k,
                shortterm_window=shortterm_window,
            )
        )
    else:
        metrics[f"{target}_recall_at_k"] = metrics["recall_at_k"]
    reused_artifacts: list[str] = []
    if candidate.provenance.get("ranking_sha256"):
        reused_artifacts.append(str(candidate.provenance["ranking_sha256"]))
    if candidate.provenance.get("query_artifact_sha256"):
        reused_artifacts.append(str(candidate.provenance["query_artifact_sha256"]))
    trace_hashes = candidate.provenance.get("trace_sha256") or {}
    if isinstance(trace_hashes, Mapping):
        reused_artifacts.extend(str(value) for value in trace_hashes.values())
    manifest_hashes = candidate.provenance.get("manifests") or {}
    if isinstance(manifest_hashes, Mapping):
        reused_artifacts.extend(str(value) for value in manifest_hashes.values())
    return CandidateResult(
        name=candidate.name,
        candidate_hash=candidate_id,
        stage=candidate.stage,
        config=candidate.config,
        metrics=metrics,
        requirement_rows=requirements,
        session_rows=session_rows,
        runtime_seconds=time.perf_counter() - started,
        work_seconds=work_seconds,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        llm_calls=0,
        embedding_calls=0,
        reused_artifacts=reused_artifacts,
        complexity=candidate.complexity,
    )
