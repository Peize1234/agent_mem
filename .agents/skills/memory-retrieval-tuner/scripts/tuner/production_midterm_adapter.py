from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .benchmark_support import (
    LineageTracker,
    load_dataset,
    load_json,
    prepare_runtime_config,
    redact_secrets,
    safe_git_commit,
    wait_for_migration_jobs,
)
from .io_utils import atomic_write_json, load_jsonl, sha256_file, stable_hash, write_jsonl
from .production_runtime import create_production_memory
from .retrieval_primitives import (
    cosine,
    field_aware_score,
    normalize_scores,
    normalized_score_fuse,
    page_representation,
)


ADAPTER_SCHEMA = 1
PRODUCTION_BACKEND = "production_midterm"
SUPPORTED_RETRIEVAL_METHODS = {"dense", "dense_bm25_fusion"}


def production_prompt_hashes() -> dict[str, str]:
    from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT

    return {
        "page_summary": hashlib.sha256(MIDTERM_PAGE_SUMMARY_PROMPT.encode()).hexdigest(),
        "session_merge": hashlib.sha256(MIDTERM_SESSION_MERGE_PROMPT.encode()).hexdigest(),
    }


def _safe_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "session"


@dataclass(frozen=True)
class RuntimeLayout:
    root: Path
    qdrant_path: Path
    sqlite_path: Path
    cache_path: Path
    collection_name: str


def isolated_runtime_layout(
    run_dir: Path,
    *,
    candidate_hash: str,
    session_id: str,
    purpose: str = "candidate",
) -> RuntimeLayout:
    """Return a collision-free runtime for one Candidate/Session pair."""
    namespace = stable_hash({"candidate_hash": candidate_hash, "session_id": session_id, "purpose": purpose})[:16]
    root = run_dir / "production_runtimes" / _safe_component(candidate_hash) / _safe_component(session_id)
    root.mkdir(parents=True, exist_ok=True)
    cache = root / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    sqlite_path = root / "history.db"
    with sqlite3.connect(sqlite_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS tuner_runtime (namespace TEXT PRIMARY KEY, created_at REAL NOT NULL)"
        )
        connection.execute(
            "INSERT OR IGNORE INTO tuner_runtime(namespace, created_at) VALUES (?, ?)",
            (namespace, time.time()),
        )
    return RuntimeLayout(
        root=root,
        qdrant_path=root / "qdrant",
        sqlite_path=sqlite_path,
        cache_path=cache,
        collection_name=f"mrt_{purpose}_{namespace}",
    )


class FrozenQueryEmbedding:
    """Read-only query embedder used when replaying production checkpoints."""

    def __init__(self, vectors: Mapping[str, Sequence[float]]):
        self._vectors = {str(text): list(vector) for text, vector in vectors.items()}

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        del memory_action
        try:
            return list(self._vectors[str(text)])
        except KeyError as exc:
            raise ValueError(f"No frozen production embedding for query text: {text!r}") from exc

    def embed_batch(self, texts: Sequence[str], memory_action: str | None = None) -> list[list[float]]:
        return [self.embed(text, memory_action) for text in texts]


@dataclass
class AdapterPoint:
    id: str
    payload: dict[str, Any]
    score: float


def _hybrid_memory_class() -> type[Any]:
    from mem0.memory.midterm import MidTermMemory, derived_output_is_visible

    class HybridMidTermMemory(MidTermMemory):
        """Benchmark-only Page-search extension; Session routing remains production code."""

        def __init__(self, *args: Any, dense_weight: float, **kwargs: Any):
            self._dense_weight = dense_weight
            super().__init__(*args, **kwargs)

        def search_pages(
            self,
            query: str,
            filters: dict[str, Any] | None = None,
            top_k: int = 5,
        ) -> list[Any]:
            dense = super().search_pages(query=query, filters=filters, top_k=top_k)
            sparse_result = self.pages_store.keyword_search(query, top_k=top_k, filters=filters)
            if sparse_result is None and (
                not getattr(self.pages_store, "_has_bm25_slot", False) or self.pages_store._get_bm25_encoder() is None
            ):
                raise RuntimeError("Qdrant BM25 sparse slot is unavailable for dense_bm25_fusion")
            # A valid sparse query may simply have no term overlap.  That is a
            # real hybrid result (dense-only for this Query), not an unavailable Branch.
            sparse = list(sparse_result or [])

            dense_rows = [{"page_id": str(row.id), "score": float(getattr(row, "score", 0.0) or 0.0)} for row in dense]
            sparse_rows = [
                {"page_id": str(row.id), "score": float(getattr(row, "score", 0.0) or 0.0)} for row in sparse
            ]
            fused = normalized_score_fuse(dense_rows, sparse_rows, dense_weight=self._dense_weight)
            payload_by_id = {str(row.id): dict(getattr(row, "payload", None) or {}) for row in [*dense, *sparse]}
            return [
                AdapterPoint(
                    id=str(row["page_id"]),
                    payload=payload_by_id[str(row["page_id"])],
                    score=float(row["score"]),
                )
                for row in fused[:top_k]
                if derived_output_is_visible(payload_by_id[str(row["page_id"])])
            ]

    return HybridMidTermMemory


def _dense_vector(value: Any) -> list[float]:
    if isinstance(value, Mapping):
        value = value.get("")
    return [float(item) for item in (value or [])]


def _scroll_points(store: Any) -> list[dict[str, Any]]:
    from mem0.memory.midterm import derived_output_is_visible

    points: list[Any] = []
    offset = None
    while True:
        batch, offset = store.client.scroll(
            collection_name=store.collection_name,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        points.extend(batch)
        if offset is None:
            break
    rows = []
    for point in points:
        payload = dict(point.payload or {})
        if not derived_output_is_visible(payload):
            continue
        vector = _dense_vector(point.vector)
        if not vector:
            raise RuntimeError(f"Production point {point.id} has no dense vector")
        rows.append({"id": str(point.id), "payload": payload, "vector": vector})
    return rows


def _source_ids(result: Mapping[str, Any], lineage: LineageTracker) -> list[str]:
    values: list[str] = []
    source_job_ids = [result.get("source_job_id"), *(result.get("source_job_ids") or [])]
    for job_id in source_job_ids:
        values.extend(lineage.turn_ids_for_job(str(job_id) if job_id else None))
    return list(dict.fromkeys(values))


def _checkpoint(
    memory: Any,
    *,
    query_id: str,
    query: str,
    session_id: str,
    user_id: str,
    lineage: LineageTracker,
    ranking_depth: int,
) -> dict[str, Any]:
    query_vector = memory.embedding_model.embed(query, "search")
    pages = _scroll_points(memory.midterm_memory.pages_store)
    sessions = _scroll_points(memory.midterm_memory.sessions_store)
    results = memory.midterm_retriever.search(
        query,
        {"user_id": user_id, "run_id": session_id},
        record_visits=False,
    )
    baseline_ranking = []
    ordered_results = [
        *[row for row in results if row.get("source") == "mid_term_page"],
        *[row for row in results if row.get("source") == "mid_term_session"],
    ]
    seen_source_ids: set[str] = set()
    for row in ordered_results:
        for source_turn_id in _source_ids(row, lineage):
            if source_turn_id in seen_source_ids:
                continue
            seen_source_ids.add(source_turn_id)
            baseline_ranking.append(
                {
                    "page_id": str(row.get("id") or ""),
                    "source_turn_id": source_turn_id,
                    "source": str(row.get("source") or ""),
                    "score": float(row.get("score") or 0.0),
                }
            )
            if len(baseline_ranking) >= ranking_depth:
                break
        if len(baseline_ranking) >= ranking_depth:
            break
    return {
        "query_id": query_id,
        "query": query,
        "filters": {"user_id": user_id, "run_id": session_id},
        "query_vector": list(query_vector),
        "pages": pages,
        "sessions": sessions,
        "source_turn_ids_by_job": {
            job_id: list(turn_ids) for job_id, turn_ids in sorted(lineage.job_to_turn_ids.items())
        },
        "baseline_ranking": baseline_ranking,
    }


class CountingLLM:
    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.delegate.generate_response(*args, **kwargs)

    async def generate_response_async(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        method = getattr(self.delegate, "generate_response_async", None)
        if method is not None:
            return await method(*args, **kwargs)
        return await asyncio.to_thread(self.delegate.generate_response, *args, **kwargs)

    async def agenerate_response(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        method = getattr(self.delegate, "agenerate_response", None)
        if method is not None:
            return await method(*args, **kwargs)
        return await asyncio.to_thread(self.delegate.generate_response, *args, **kwargs)


class CountingEmbedding:
    def __init__(self, delegate: Any):
        self.delegate = delegate
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def embed(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        return self.delegate.embed(*args, **kwargs)

    def embed_batch(self, texts: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        self.calls += len(texts)
        return self.delegate.embed_batch(texts, *args, **kwargs)


async def build_production_source(spec: Mapping[str, Any]) -> dict[str, Any]:
    """Run the actual AsyncMemory Add -> MidTerm pipeline for one isolated Session."""
    dataset_path = Path(str(spec["dataset_path"])).resolve()
    sheet_name = str(spec["session_id"])
    output_dir = Path(str(spec["output_dir"])).resolve()
    runtime_dir = Path(str(spec["runtime_dir"])).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    max_turns = spec.get("max_turns_per_session")
    sessions = load_dataset(
        dataset_path,
        include_sheets=[sheet_name],
        max_turns_per_session=int(max_turns) if max_turns is not None else None,
    )
    if len(sessions) != 1:
        raise ValueError(f"Expected exactly one benchmark Session, got {len(sessions)}")
    session = sessions[0]

    base_config = load_json(Path(str(spec["memory_config_path"])).resolve())
    config = prepare_runtime_config(
        base_config,
        runtime_dir=runtime_dir,
        collection_name=str(spec["collection_name"]),
        reset_storage=True,
        agentic_retrieval_enabled=False,
        profile_enabled=False,
        profile_update_on_add=False,
    )
    config.setdefault("background", {})["midterm_worker_count"] = 1
    config["background"]["longterm_worker_count"] = 1
    memory = create_production_memory(config, llm_mode=str(spec.get("llm_mode") or "real"))
    counted = CountingLLM(memory.llm)
    counted_embedding = CountingEmbedding(memory.embedding_model)
    memory.llm = counted
    memory.embedding_model = counted_embedding
    if getattr(memory, "_midterm_updater", None) is not None:
        memory._midterm_updater.llm = counted
    if getattr(memory, "_midterm_memory", None) is not None:
        memory._midterm_memory.embedding_model = counted_embedding
    shortterm_window = memory._short_term_capacity() // 2
    lineage = LineageTracker(shortterm_window)
    pending: list[str] = []
    checkpoints: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    user_id = f"recall::{session.session_id}"
    job_timeout = float(spec.get("job_timeout_seconds") or 900.0)

    try:
        for turn in session.turns:
            if turn.dependency_turn_ids:
                await wait_for_migration_jobs(
                    memory,
                    pending,
                    timeout_seconds=job_timeout,
                    poll_interval_seconds=0.2,
                )
                pending.clear()
                checkpoints.append(
                    await asyncio.to_thread(
                        _checkpoint,
                        memory,
                        query_id=turn.turn_id,
                        query=turn.question,
                        session_id=session.session_id,
                        user_id=user_id,
                        lineage=lineage,
                        ranking_depth=int(spec["ranking_depth"]),
                    )
                )
            add_result = await memory.add(
                [
                    {"role": "user", "content": turn.question, "name": f"{turn.turn_id}:user"},
                    {"role": "assistant", "content": turn.answer, "name": f"{turn.turn_id}:assistant"},
                ],
                user_id=user_id,
                run_id=session.session_id,
                metadata={
                    "benchmark_run_name": "memory_retrieval_tuner_source",
                    "dataset_session_id": session.session_id,
                    "dataset_turn_id": turn.turn_id,
                    "dataset_turn_index": turn.turn_index,
                },
                infer=False,
            )
            migration_job_id = (add_result.get("background") or {}).get("migration_job_id")
            evicted = lineage.register_add(session.session_id, turn.turn_id, migration_job_id)
            if migration_job_id:
                pending.append(str(migration_job_id))
            turn_rows.append(
                {
                    "session_id": session.session_id,
                    "turn_id": turn.turn_id,
                    "turn_index": turn.turn_index,
                    "migration_job_id": migration_job_id,
                    "evicted_turn_ids": list(evicted),
                    "error": None,
                }
            )
        await wait_for_migration_jobs(
            memory,
            pending,
            timeout_seconds=job_timeout,
            poll_interval_seconds=0.2,
        )
        await memory.flush_background_tasks(timeout=job_timeout)
    finally:
        memory.close()

    checkpoints_path = output_dir / "production_midterm_checkpoints.jsonl"
    trace_path = output_dir / "recall_turn_results.jsonl"
    write_jsonl(checkpoints_path, checkpoints)
    write_jsonl(trace_path, turn_rows)
    prompt_hashes = production_prompt_hashes()
    repo_root = Path(__file__).resolve().parents[5]
    manifest = {
        "schema": ADAPTER_SCHEMA,
        "status": "COMPLETE",
        "backend": PRODUCTION_BACKEND,
        "dataset_path": str(dataset_path),
        "dataset_sha256": sha256_file(dataset_path),
        "session_id": session.session_id,
        "turn_count": len(turn_rows),
        "checkpoint_count": len(checkpoints),
        "failed_turns": 0,
        "ranking_depth": int(spec["ranking_depth"]),
        "shortterm_qa_turns": shortterm_window,
        "checkpoints_path": str(checkpoints_path),
        "checkpoints_sha256": sha256_file(checkpoints_path),
        "trace_path": str(trace_path),
        "trace_sha256": sha256_file(trace_path),
        "memory_config_path": str(Path(str(spec["memory_config_path"])).resolve()),
        "memory_config_sha256": sha256_file(Path(str(spec["memory_config_path"])).resolve()),
        "effective_memory_config": redact_secrets(config),
        "production_config": deepcopy(config.get("midterm") or {}),
        "prompt_hashes": prompt_hashes,
        "llm_mode": str(spec.get("llm_mode") or "real"),
        "llm_calls": counted.calls,
        "embedding_calls": counted_embedding.calls,
        "embedding_model": deepcopy(config.get("embedder") or {}),
        "git_commit": safe_git_commit(repo_root),
    }
    atomic_write_json(output_dir / "production_midterm_manifest.json", manifest)
    return manifest


def generate_production_sources(
    *,
    dataset_path: Path,
    dataset_sha256: str,
    session_ids: Sequence[str],
    session_turn_counts: Mapping[str, int],
    memory_config_path: Path,
    run_dir: Path,
    ranking_depth: int,
    llm_mode: str,
    max_parallel_sessions: int,
    max_parallel_llm_calls: int,
) -> list[Path]:
    """Generate missing production artifacts in isolated subprocesses."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    source_root = run_dir / "production_source"
    source_root.mkdir(parents=True, exist_ok=True)
    memory_config_sha256 = sha256_file(memory_config_path)
    prompt_hashes = production_prompt_hashes()

    def run_session(session_id: str) -> Path:
        code = _safe_component(session_id)
        result_dir = source_root / code
        manifest_path = result_dir / "production_midterm_manifest.json"
        if manifest_path.exists():
            value = load_json(manifest_path)
            if (
                value.get("status") == "COMPLETE"
                and value.get("dataset_sha256") == dataset_sha256
                and int(value.get("turn_count") or 0) == int(session_turn_counts[session_id])
                and int(value.get("failed_turns") or 0) == 0
                and int(value.get("ranking_depth") or 0) >= ranking_depth
                and value.get("memory_config_sha256") == memory_config_sha256
                and value.get("prompt_hashes") == prompt_hashes
                and str(value.get("llm_mode") or "real") == llm_mode
                and Path(str(value.get("checkpoints_path") or "")).exists()
                and value.get("checkpoints_sha256") == sha256_file(Path(str(value["checkpoints_path"])))
            ):
                return manifest_path
        runtime = isolated_runtime_layout(
            run_dir,
            candidate_hash="production-source",
            session_id=session_id,
            purpose="source",
        )
        spec = {
            "dataset_path": str(dataset_path.resolve()),
            "session_id": session_id,
            "output_dir": str(result_dir),
            "runtime_dir": str(runtime.root),
            "collection_name": runtime.collection_name,
            "memory_config_path": str(memory_config_path.resolve()),
            "ranking_depth": ranking_depth,
            "llm_mode": llm_mode,
        }
        spec_path = result_dir / "source_spec.json"
        atomic_write_json(spec_path, spec)
        log_path = result_dir / "source_worker.log"
        repo_root = Path(__file__).resolve().parents[5]
        scripts_root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(repo_root), str(scripts_root), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        environment["MEM0_TELEMETRY"] = "False"
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(
                [sys.executable, "-m", "tuner.production_midterm_adapter", "source-worker", "--spec", str(spec_path)],
                cwd=repo_root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        if completed.returncode:
            raise RuntimeError(f"Production source worker failed for {session_id}; see {log_path}")
        manifest = load_json(manifest_path)
        if int(manifest.get("turn_count") or 0) != int(session_turn_counts[session_id]):
            raise RuntimeError(f"Incomplete production source for {session_id}")
        return manifest_path

    paths: dict[str, Path] = {}
    worker_count = source_worker_parallelism(
        session_count=len(session_ids),
        max_parallel_sessions=max_parallel_sessions,
        max_parallel_llm_calls=max_parallel_llm_calls,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(run_session, session_id): session_id for session_id in session_ids}
        for future in as_completed(futures):
            paths[futures[future]] = future.result()
    return [paths[session_id] for session_id in session_ids]


def source_worker_parallelism(
    *,
    session_count: int,
    max_parallel_sessions: int,
    max_parallel_llm_calls: int,
) -> int:
    """Cap isolated workers so their single MidTerm LLM lane cannot exceed the LLM limit."""
    return max(1, min(session_count, max_parallel_sessions, max_parallel_llm_calls))


def load_checkpoints(manifest_paths: Sequence[str | Path]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for raw_path in manifest_paths:
        manifest = load_json(Path(raw_path))
        if manifest.get("schema") != ADAPTER_SCHEMA or manifest.get("status") != "COMPLETE":
            raise ValueError(f"Invalid production MidTerm manifest: {raw_path}")
        for checkpoint in load_jsonl(Path(str(manifest["checkpoints_path"]))):
            query_id = str(checkpoint.get("query_id") or "").upper()
            if not query_id or query_id in result:
                raise ValueError(f"Missing or duplicate production checkpoint: {query_id}")
            result[query_id] = checkpoint
    return result


def production_candidate_from_manifests(
    manifest_paths: Sequence[Path],
    *,
    name: str = "baseline",
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifests = [load_json(path) for path in manifest_paths]
    if not manifests:
        raise ValueError("No production MidTerm manifests")
    config = deepcopy(manifests[0].get("production_config") or {})
    vector_config = ((manifests[0].get("effective_memory_config") or {}).get("vector_store") or {}).get("config") or {}
    config.update(
        {
            "backend": PRODUCTION_BACKEND,
            "retrieval_contract": "production_midterm_v1",
            "retrieval_method": "dense",
            "query_representation": "original",
            "page_representation": "production",
            "bm25_language": str(vector_config.get("bm25_language") or "en"),
            "manifest_paths": [str(path.resolve()) for path in manifest_paths],
            "manifest_sha256": {str(path.resolve()): sha256_file(path) for path in manifest_paths},
        }
    )
    provenance = {
        "adapter": "tuner.production_midterm_adapter.ProductionMidtermAdapter",
        "production_classes": [
            "mem0.memory.midterm.MidTermMemory",
            "mem0.memory.midterm_retriever.MidTermRetriever",
        ],
        "source": "real AsyncMemory Add/MidTerm pipeline",
        "retrieval_contract": "production_midterm_v1",
        "manifests": config["manifest_sha256"],
        "failed_turns": sum(int(item.get("failed_turns") or 0) for item in manifests),
        "llm_calls": sum(int(item.get("llm_calls") or 0) for item in manifests),
        "embedding_calls": sum(int(item.get("embedding_calls") or 0) for item in manifests),
        "candidate_name": name,
    }
    return config, provenance


class ProductionMidtermAdapter:
    def __init__(self, *, run_dir: Path, candidate_hash: str, session_id: str, ranking_depth: int):
        self.layout = isolated_runtime_layout(
            run_dir,
            candidate_hash=candidate_hash,
            session_id=session_id,
            purpose="replay",
        )
        self.ranking_depth = ranking_depth
        self._cross_encoder: Any | None = None

    @staticmethod
    def supported(config: Mapping[str, Any]) -> None:
        method = str(config.get("retrieval_method") or "dense")
        if method not in SUPPORTED_RETRIEVAL_METHODS:
            raise ValueError(f"Unsupported production MidTerm retrieval method: {method}")
        has_derived = bool(config.get("derived_artifact_path"))
        if str(config.get("page_representation") or "production") != "production" and not has_derived:
            raise ValueError("Page representation changes require regenerated production artifacts")
        if str(config.get("query_representation") or "original") != "original" and not has_derived:
            raise ValueError("Query representation requires a matching frozen query embedding artifact")

    @staticmethod
    def _derived_payload(config: Mapping[str, Any]) -> dict[str, Any] | None:
        raw_path = config.get("derived_artifact_path")
        if not raw_path:
            return None
        from .derived_artifacts import load_derived_payload

        path = Path(str(raw_path))
        if not path.exists() or sha256_file(path) != config.get("derived_artifact_sha256"):
            raise ValueError("Derived artifact is missing or its SHA-256 does not match")
        return load_derived_payload(path)

    def _rerank_pages(
        self,
        rows: Sequence[Mapping[str, Any]],
        *,
        query: str,
        query_vector: Sequence[float],
        point_by_id: Mapping[str, Mapping[str, Any]],
        derived: Mapping[str, Any] | None,
        config: Mapping[str, Any],
    ) -> list[Mapping[str, Any]]:
        method = str(config.get("reranker_method") or "none")
        if method == "none" or not rows:
            return list(rows)
        dense_rows = [{"page_id": str(row.get("id") or ""), "score": float(row.get("score") or 0.0)} for row in rows]
        dense_scores = normalize_scores(dense_rows)
        secondary: dict[str, float] = {}
        language = str(config.get("bm25_language") or "zh")
        if method == "field_lexical":
            weights = config.get("field_weights") or {"summary": 0.5, "keywords": 0.3, "user_input": 0.2}
            secondary = {
                page_id: field_aware_score(
                    query,
                    point.get("payload") or {},
                    field_weights=weights,
                    language=language,
                )
                for page_id, point in point_by_id.items()
            }
        elif method == "multi_vector_maxsim":
            if derived is None:
                raise ValueError("multi_vector_maxsim requires a derived field-vector artifact")
            from .derived_artifacts import point_fingerprint

            field_vectors = derived.get("field_vectors") or {}
            secondary = {
                page_id: max(
                    [
                        cosine(query_vector, vector)
                        for vector in field_vectors.get(point_fingerprint(point), {}).values()
                    ]
                    or [0.0]
                )
                for page_id, point in point_by_id.items()
            }
        elif method == "cross_encoder":
            from sentence_transformers import CrossEncoder

            if self._cross_encoder is None:
                model_path = str(config.get("reranker_model_path") or config.get("reranker_model_id") or "")
                if not model_path:
                    raise ValueError("cross_encoder requires reranker_model_path or reranker_model_id")
                self._cross_encoder = CrossEncoder(model_path)
            page_ids = [str(row.get("id") or "") for row in rows]
            texts = [
                page_representation(point_by_id[page_id].get("payload") or {}, "production") for page_id in page_ids
            ]
            scores = self._cross_encoder.predict([[query, text] for text in texts])
            secondary = {page_id: float(score) for page_id, score in zip(page_ids, scores)}
            low, high = min(secondary.values()), max(secondary.values())
            if not math.isclose(low, high):
                secondary = {key: (value - low) / (high - low) for key, value in secondary.items()}
        else:
            raise ValueError(f"Unsupported reranker method: {method}")
        dense_weight = float(config.get("reranker_dense_weight", 0.7))
        reranked = sorted(
            rows,
            key=lambda row: (
                -(
                    dense_weight * dense_scores.get(str(row.get("id") or ""), 0.0)
                    + (1.0 - dense_weight) * secondary.get(str(row.get("id") or ""), 0.0)
                ),
                str(row.get("id") or ""),
            ),
        )
        return list(reranked)

    def rank(self, checkpoint: Mapping[str, Any], config: Mapping[str, Any]) -> list[dict[str, Any]]:
        from mem0.configs.base import MidTermMemoryConfig
        from mem0.memory.midterm import MidTermMemory
        from mem0.memory.midterm_retriever import MidTermRetriever
        from qdrant_client import QdrantClient

        self.supported(config)
        derived = self._derived_payload(config)
        query_id = str(checkpoint["query_id"])
        query = str((derived or {}).get("query_texts", {}).get(query_id) or checkpoint["query"])
        query_vector = list((derived or {}).get("query_vectors", {}).get(query_id) or checkpoint["query_vector"])
        dimensions = len(query_vector)
        if not dimensions:
            raise ValueError("Production checkpoint has no query vector")
        checkpoint_id = stable_hash(
            {"query_id": checkpoint["query_id"], "candidate": dict(config), "schema": ADAPTER_SCHEMA}
        )[:16]
        base_collection = f"{self.layout.collection_name}_{checkpoint_id}"
        vector_config = {
            "collection_name": base_collection,
            "path": str(self.layout.qdrant_path),
            "embedding_model_dims": dimensions,
            "bm25_language": str(config.get("bm25_language") or "en"),
            "on_disk": False,
        }
        midterm_config = MidTermMemoryConfig(
            **{key: value for key, value in config.items() if key in MidTermMemoryConfig.model_fields}
        )
        memory_class = (
            _hybrid_memory_class() if config.get("retrieval_method") == "dense_bm25_fusion" else MidTermMemory
        )
        extra = (
            {"dense_weight": float(config.get("dense_weight", 0.7))}
            if config.get("retrieval_method") == "dense_bm25_fusion"
            else {}
        )
        qdrant_client = QdrantClient(path=str(self.layout.qdrant_path))

        class PrimaryStore:
            client = qdrant_client

        memory = memory_class(
            provider="qdrant",
            base_vector_config=vector_config,
            base_collection_name=base_collection,
            embedding_model=FrozenQueryEmbedding({query: query_vector}),
            config=midterm_config,
            primary_vector_store=PrimaryStore(),
            **extra,
        )
        try:
            point_by_kind_and_id: dict[str, dict[str, Mapping[str, Any]]] = {"pages": {}, "sessions": {}}
            for key, store in (("pages", memory.pages_store), ("sessions", memory.sessions_store)):
                points = list(checkpoint.get(key) or [])
                point_by_kind_and_id[key] = {str(point["id"]): point for point in points}
                if points:
                    from .derived_artifacts import point_fingerprint

                    replacements = (derived or {}).get(f"{key[:-1]}_vectors") or {}
                    store.insert(
                        vectors=[
                            list(replacements.get(point_fingerprint(point)) or point["vector"]) for point in points
                        ],
                        ids=[str(point["id"]) for point in points],
                        payloads=[dict(point["payload"]) for point in points],
                    )
            retriever = MidTermRetriever(memory, midterm_config)
            results = retriever.search(
                query,
                dict(checkpoint["filters"]),
                record_visits=False,
            )
            job_map = {
                str(job_id): [str(turn_id).upper() for turn_id in turn_ids]
                for job_id, turn_ids in (checkpoint.get("source_turn_ids_by_job") or {}).items()
            }
            ranking: list[dict[str, Any]] = []
            page_results = [row for row in results if row.get("source") == "mid_term_page"]
            page_results = self._rerank_pages(
                page_results,
                query=query,
                query_vector=query_vector,
                point_by_id=point_by_kind_and_id["pages"],
                derived=derived,
                config=config,
            )
            ordered_results = [
                *page_results,
                *[row for row in results if row.get("source") == "mid_term_session"],
            ]
            seen_source_ids: set[str] = set()
            for row in ordered_results:
                source_ids: list[str] = []
                for job_id in [row.get("source_job_id"), *(row.get("source_job_ids") or [])]:
                    source_ids.extend(job_map.get(str(job_id), []))
                for source_turn_id in dict.fromkeys(source_ids):
                    if source_turn_id in seen_source_ids:
                        continue
                    seen_source_ids.add(source_turn_id)
                    ranking.append(
                        {
                            "page_id": str(row.get("id") or ""),
                            "source_turn_id": source_turn_id,
                            "source": str(row.get("source") or ""),
                            "score": float(row.get("score") or 0.0),
                        }
                    )
                    if len(ranking) >= self.ranking_depth:
                        break
                if len(ranking) >= self.ranking_depth:
                    break
            return [{**row, "rank": rank} for rank, row in enumerate(ranking, start=1)]
        finally:
            try:
                memory.reset()
            finally:
                qdrant_client.close()


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    worker = subparsers.add_parser("source-worker")
    worker.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "source-worker":
        asyncio.run(build_production_source(json.loads(args.spec.read_text(encoding="utf-8"))))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
