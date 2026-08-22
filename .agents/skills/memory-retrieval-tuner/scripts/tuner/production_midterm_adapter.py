from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mem0.memory.midterm_updater import PRODUCTION_PAGE_CONTEXT_CONTRACT
from mem0.memory.query_resolver import QueryResolver

from .benchmark_support import (
    LineageTracker,
    load_dataset,
    load_json,
    prepare_runtime_config,
    redact_secrets,
    safe_git_commit,
    wait_for_migration_jobs,
)
from .diagnostic_midterm_retriever import DiagnosticMidTermRetriever
from .encoding_contract import EncodingContract, SentenceTransformerEncodingAdapter
from .fact_evaluator import fact_member_hit, parse_required_context
from .io_utils import (
    atomic_write_json,
    load_jsonl,
    sha256_file,
    stable_hash,
    write_jsonl,
)
from .production_runtime import create_production_memory
from .retrieval_primitives import (
    HYBRID_PRESET_WEIGHTS,
    cosine,
    field_aware_score,
    normalize_scores,
    normalized_score_fuse,
    page_representation,
    tuner_score_and_rank,
)
from .source_prompt_variants import PromptOverrideLLM

ADAPTER_SCHEMA = 7  # isolates the per-QA LongTerm and fixed Page-context production contract
PRODUCTION_BACKEND = "production_midterm"
PRODUCTION_MEMORY_CONTRACT = "agent_memory_p0_query_fixed_page_context_per_qa_cross_session_longterm_v2"
SUPPORTED_RETRIEVAL_METHODS = {"dense", "dense_bm25_fusion"}
logger = logging.getLogger(__name__)


def production_prompt_hashes(
    *,
    page_summary_prompt: str | None = None,
    session_merge_prompt: str | None = None,
    fine_grained_longterm_extraction_prompt: str | None = None,
    session_longterm_extraction_prompt: str | None = None,
) -> dict[str, str]:
    from mem0.configs.midterm_prompts import (
        MIDTERM_PAGE_SUMMARY_PROMPT,
        MIDTERM_SESSION_MERGE_PROMPT,
    )
    from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT

    return {
        "page_summary": hashlib.sha256((page_summary_prompt or MIDTERM_PAGE_SUMMARY_PROMPT).encode()).hexdigest(),
        "session_merge": hashlib.sha256((session_merge_prompt or MIDTERM_SESSION_MERGE_PROMPT).encode()).hexdigest(),
        "fine_grained_longterm_extraction": hashlib.sha256(
            (
                fine_grained_longterm_extraction_prompt
                or session_longterm_extraction_prompt
                or ADDITIVE_EXTRACTION_PROMPT
            ).encode()
        ).hexdigest(),
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


def _diagnostic_source_ids(result: Mapping[str, Any], source_turn_ids_by_job: Mapping[str, Any]) -> list[str]:
    """Resolve production diagnostic Page lineage without evaluator guesses."""
    values: list[str] = []
    for job_id in [result.get("source_job_id"), *(result.get("source_job_ids") or [])]:
        values.extend(str(value).upper() for value in source_turn_ids_by_job.get(str(job_id), []) if value)
    explicit = result.get("source_turn_id")
    if explicit:
        values.append(str(explicit).upper())
    return list(dict.fromkeys(values))


def _diagnostic_rows_from_pages(
    pages: Sequence[Mapping[str, Any]],
    source_turn_ids_by_job: Mapping[str, Any],
    *,
    ranking_depth: int | None = None,
) -> list[dict[str, Any]]:
    """Expand the production Page-level trace to evaluator source-turn rows.

    Every row still carries the Page-stage fields.  This is deliberately kept
    in the production adapter so candidate-pool recall cannot be inferred from
    the final <=5 return list.
    """
    rows: list[dict[str, Any]] = []
    for page in pages:
        source_ids = _diagnostic_source_ids(page, source_turn_ids_by_job)
        if not source_ids:
            source_ids = [str(page.get("id") or page.get("page_id") or "").upper()]
        for source_turn_id in source_ids:
            if not source_turn_id:
                continue
            rows.append(
                {
                    "page_id": str(page.get("id") or page.get("page_id") or ""),
                    "source_turn_id": source_turn_id,
                    "source": str(page.get("source") or "mid_term_page"),
                    "score": float(page.get("score") or page.get("final_score") or 0.0),
                    "memory": page.get("memory"),
                    "summary": page.get("summary"),
                    "raw_dialogue": page.get("raw_dialogue"),
                    "raw_rag_score": page.get("raw_rag_score"),
                    "forgetting_factor": page.get("forgetting_factor"),
                    "heat_modulation": page.get("heat_factor", page.get("heat_modulation")),
                    "final_score": page.get("final_score"),
                    "routed_candidate": bool(page.get("routed_candidate")),
                    "in_routed_pool": bool(page.get("routed_candidate")),
                    "global_supplement": bool(page.get("global_supplement")),
                    "selected_session_count": page.get("selected_session_count"),
                    "session_routed_page_count": page.get("session_routed_page_count"),
                    "global_supplement_page_count": page.get("global_supplement_page_count"),
                    "dedup_candidate_count": page.get("dedup_candidate_count"),
                    "candidate_pool_count": page.get("candidate_pool_count"),
                    "in_candidate_pool": True,
                    "rank_before_threshold": page.get("rank_before_threshold"),
                    "threshold_passed": page.get("threshold_passed") is True
                    if "threshold_passed" in page
                    else not bool(page.get("threshold_filtered")),
                    "threshold_filtered": bool(page.get("threshold_filtered")),
                    "final_rank": page.get("final_rank"),
                    "final_visible": page.get("final_visible"),
                    "context_budget_filtered": page.get("final_visible") is False,
                    "ranking_loss": bool(page.get("ranking_loss"))
                    or (
                        ranking_depth is not None
                        and page.get("rank_before_threshold") is not None
                        and int(page.get("rank_before_threshold") or 0) > int(ranking_depth)
                    ),
                }
            )
    return rows


def _unselected_page_diagnostic_rows(
    checkpoint: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    source_turn_ids_by_job: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Expose source Pages omitted before candidate pooling for loss diagnosis."""
    pool_ids = {
        str(row.get("id") or "")
        for row in diagnostics.get("deduplicated_candidate_pool") or []
        if row.get("id")
    }
    routed_session_ids = {
        str(row.get("session_id") or row.get("id") or "")
        for row in diagnostics.get("selected_sessions") or []
        if row.get("session_id") or row.get("id")
    }
    rows: list[dict[str, Any]] = []
    for point in checkpoint.get("pages") or []:
        page_id = str(point.get("id") or "")
        if not page_id or page_id in pool_ids:
            continue
        payload = dict(point.get("payload") or {})
        routed = str(payload.get("session_id") or "") in routed_session_ids
        source_ids = _diagnostic_source_ids(payload, source_turn_ids_by_job)
        if not source_ids:
            source_ids = [page_id.upper()]
        for source_turn_id in source_ids:
            rows.append(
                {
                    "page_id": page_id,
                    "source_turn_id": source_turn_id,
                    "source": "mid_term_page",
                    "memory": payload.get("summary") or payload.get("raw_dialogue") or "",
                    "summary": payload.get("summary"),
                    "raw_dialogue": payload.get("raw_dialogue"),
                    "in_candidate_pool": False,
                    "routed_candidate": routed,
                    "in_routed_pool": routed,
                    "global_supplement": False,
                    "candidate_pool_count": len(pool_ids),
                    "threshold_passed": None,
                    "threshold_filtered": None,
                    "final_rank": None,
                    "final_visible": False,
                    "diagnostic_only": True,
                }
            )
    return rows


def _longterm_candidate_pool(
    memory: Any,
    *,
    query: str,
    query_vector: Sequence[float],
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze production Long-term signals deeply enough for query-only replay."""
    from mem0.memory.main import _payload_is_expired
    from mem0.utils.entity_extraction import extract_entities
    from mem0.utils.lemmatization import lemmatize_for_bm25
    from mem0.utils.scoring import get_bm25_params, normalize_bm25

    pool_limit = max(30 * 6, 60)
    current_filters = dict(filters)
    all_session_filters = {key: value for key, value in current_filters.items() if key != "run_id"}
    semantic_routes = [
        memory.vector_store.search(
            query=query,
            vectors=list(query_vector),
            top_k=pool_limit,
            filters=route_filters,
        )
        for route_filters in (current_filters, all_session_filters)
    ]
    current_candidate_ids = {str(row.id) for row in semantic_routes[0]}
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for row in [item for route in semantic_routes for item in route]:
        payload = dict(getattr(row, "payload", None) or {})
        if not memory._stage_output_is_visible(payload, "longterm") or _payload_is_expired(payload):
            continue
        candidate = {
            "id": str(row.id),
            "score": float(getattr(row, "score", 0.0) or 0.0),
            "payload": payload,
        }
        existing = candidates_by_id.get(candidate["id"])
        if existing is None or candidate["score"] > existing["score"]:
            candidates_by_id[candidate["id"]] = candidate
    candidates = sorted(candidates_by_id.values(), key=lambda item: float(item["score"]), reverse=True)

    query_lemmatized = lemmatize_for_bm25(query, language=getattr(memory, "_bm25_language", None))
    keyword_rows = [
        item
        for route_filters in (current_filters, all_session_filters)
        for item in (
            memory.vector_store.keyword_search(
                query=query_lemmatized,
                top_k=pool_limit,
                filters=route_filters,
            )
            or []
        )
    ]
    midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
    bm25_scores: dict[str, float] = {}
    for row in keyword_rows or []:
        raw_score = float(getattr(row, "score", 0.0) or 0.0)
        if raw_score > 0:
            bm25_scores[str(row.id)] = normalize_bm25(raw_score, midpoint, steepness)

    query_entities = memory._run_entity_extraction(extract_entities, query)
    entity_boosts: dict[str, dict[str, float]] = {}
    for threshold in (0.4, 0.5, 0.6, 0.7, 0.8):
        if query_entities:
            compute = getattr(memory, "_compute_entity_boosts_async", None)
            boosts = (
                asyncio.run(compute(query_entities, dict(filters), threshold=threshold))
                if callable(compute)
                else {}
            )
        else:
            boosts = {}
        entity_boosts[f"{threshold:.1f}"] = {
            str(memory_id): float(score) for memory_id, score in boosts.items()
        }

    return {
        "schema": 2,
        "pool_limit": pool_limit,
        "current_run_id": current_filters.get("run_id"),
        "semantic_candidates": candidates,
        "current_session_candidate_ids": sorted(current_candidate_ids),
        "bm25_scores": bm25_scores,
        "entity_boosts_by_threshold": entity_boosts,
    }


def _session_heat_states(
    memory: Any,
    *,
    user_id: str,
    session_id: str,
    current_turn_index: int,
) -> list[dict[str, Any]]:
    """Snapshot production-computed state after Search/recall mutations.

    The tuner records production fields and calls the production retriever's
    evolution helper; it does not duplicate the Heat formula.
    """
    rows = _scroll_points(memory.midterm_memory.sessions_store)
    payloads = {
        str(row["id"]): dict(row.get("payload") or {})
        for row in rows
        if str((row.get("payload") or {}).get("user_id") or "") == user_id
        and str((row.get("payload") or {}).get("run_id") or "") == session_id
    }
    if not payloads:
        return []
    recencies, heats = memory.midterm_retriever._session_evolution(payloads, int(current_turn_index))
    minimum_recalls = int(memory.config.midterm.promotion_min_recall_count)
    minimum_heat = float(memory.config.midterm.promotion_heat_threshold)
    states = []
    for memory_session_id, payload in payloads.items():
        heat = float(heats.get(memory_session_id, payload.get("H_segment") or 0.0))
        recalls = int(payload.get("valid_recall_count") or 0)
        states.append(
            {
                "session_id": memory_session_id,
                "H_segment": heat,
                "N_visit": int(payload.get("N_visit") or 0),
                "L_interaction": float(payload.get("L_interaction") or 0.0),
                "R_recency": float(recencies.get(memory_session_id, payload.get("R_recency") or 0.0)),
                "valid_recall_count": recalls,
                "current_turn_index": int(current_turn_index),
                "last_visit_turn_index": payload.get("last_visit_turn_index"),
                "promotion_eligible": recalls >= minimum_recalls and heat >= minimum_heat,
            }
        )
    return states


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
    base_context = asyncio.run(
        memory._retrieve_base_context(
            query,
            user_id=user_id,
            session_id=session_id,
        )
    )
    retrieval_query = asyncio.run(
        QueryResolver(memory.llm).resolve_async(query, base_context.get("short_term_messages") or [])
    )
    query_vector = memory.embedding_model.embed(retrieval_query, "search")
    pages = _scroll_points(memory.midterm_memory.pages_store)
    sessions = _scroll_points(memory.midterm_memory.sessions_store)
    diagnostic_retriever = DiagnosticMidTermRetriever(memory.midterm_memory, memory.config.midterm)
    results = diagnostic_retriever.search(
        retrieval_query,
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
    # Keep a complete layered trace alongside the Mid-term checkpoint.  This
    # is used only for final Short+Mid+Session-Longterm regression; it never
    # enters static Mid-term winner selection.
    layered_results: list[dict[str, Any]] = []
    try:
        from mem0.memory.main import _filter_shortterm_duplicate_longterm

        raw = memory.search(
            retrieval_query,
            top_k=min(int(getattr(memory.config, "longterm_top_k", 30)), 30),
            filters={"user_id": user_id, "run_id": session_id},
            threshold=None,
        )
        if inspect.isawaitable(raw):
            raw = asyncio.run(raw)
        layered_results = list(raw.get("results") if isinstance(raw, dict) else raw or [])
        layered_context = {**base_context, "retrieved_memories": layered_results}
        _filter_shortterm_duplicate_longterm(layered_context)
        layered_results = list(layered_context["retrieved_memories"])
    except Exception:
        logger.debug("Layered production trace unavailable for %s", query_id, exc_info=True)
    longterm_pool: dict[str, Any] = {}
    try:
        longterm_pool = _longterm_candidate_pool(
            memory,
            query=retrieval_query,
            query_vector=query_vector,
            filters={"user_id": user_id, "run_id": session_id},
        )
    except Exception:
        logger.debug("Long-term candidate pool unavailable for %s", query_id, exc_info=True)
    current_turn_index = int(
        memory.midterm_memory.current_turn_index({"user_id": user_id, "run_id": session_id})
    )
    return {
        "query_id": query_id,
        "query": query,
        "retrieval_query": retrieval_query,
        "filters": {"user_id": user_id, "run_id": session_id},
        "query_vector": list(query_vector),
        # Search is performed before Add for this turn.  Persist the exact
        # production clock so replay cannot substitute page_sequence.
        "current_turn_index": current_turn_index,
        "heat_states": _session_heat_states(
            memory,
            user_id=user_id,
            session_id=session_id,
            current_turn_index=current_turn_index,
        ),
        "pages": pages,
        "sessions": sessions,
        "source_turn_ids_by_job": {
            job_id: list(turn_ids) for job_id, turn_ids in sorted(lineage.job_to_turn_ids.items())
        },
        "baseline_ranking": baseline_ranking,
        # This is the complete production retrieval chain, not just the
        # thresholded/capped user-visible result.
        "retrieval_diagnostics": deepcopy(diagnostic_retriever.last_search_diagnostics),
        "retrieved_results": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "source",
                    "raw_dialogue",
                    "summary",
                    "memory",
                    "source_job_id",
                    "source_job_ids",
                    "raw_rag_score",
                    "forgetting_factor",
                    "heat_factor",
                    "final_score",
                )
            }
            for item in results
            if isinstance(item, Mapping)
        ],
        "layered_results": [
            {
                "id": str(item.get("id") or ""),
                "source": str(item.get("source") or "long_term"),
                "memory": item.get("memory") or item.get("data") or item.get("raw_dialogue") or "",
                "summary": item.get("summary"),
                "raw_dialogue": item.get("raw_dialogue"),
                "source_turn_id": (
                    item.get("source_turn_id")
                    or (
                        item.get("metadata", {}).get("dataset_turn_id")
                        if isinstance(item.get("metadata"), Mapping)
                        else None
                    )
                ),
                "score": float(item.get("score") or 0.0),
            }
            for item in layered_results
            if isinstance(item, Mapping)
        ],
        "longterm_candidate_pool": longterm_pool,
    }


def _valid_recalled_page_ids(turn: Any, checkpoint: Mapping[str, Any]) -> list[str]:
    """Return only visible production Pages that satisfy this turn's Gold."""
    pages = [
        row
        for row in checkpoint.get("retrieved_results") or []
        if isinstance(row, Mapping) and row.get("source") in {"mid_term_page", "midterm"} and row.get("id")
    ]
    required_context = str(getattr(turn, "required_context", "") or "").strip()
    if required_context:
        requirements = parse_required_context(required_context)
        return [
            str(row["id"])
            for row in pages
            if any(
                any(
                    fact_member_hit(
                        member,
                        " ".join(str(row.get(key) or "") for key in ("raw_dialogue", "summary", "memory")),
                    )
                    for member in requirement.members
                )
                for requirement in requirements
            )
        ]

    required_ids = {
        str(member).upper()
        for requirement in getattr(turn, "requirements", ())
        for member in requirement.members
    }
    job_map = {
        str(job_id): {str(turn_id).upper() for turn_id in turn_ids}
        for job_id, turn_ids in (checkpoint.get("source_turn_ids_by_job") or {}).items()
    }
    valid = []
    for row in pages:
        source_ids: set[str] = set()
        for job_id in [row.get("source_job_id"), *(row.get("source_job_ids") or [])]:
            source_ids.update(job_map.get(str(job_id), set()))
        if required_ids & source_ids:
            valid.append(str(row["id"]))
    return valid


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


class EncodingContractEmbedding:
    """Apply the model-owned encoding contract inside an isolated source runtime."""

    def __init__(self, delegate: Any, contract: Mapping[str, Any]):
        model = getattr(delegate, "model", None)
        if model is None:
            raise ValueError("Candidate encoding contracts require a local SentenceTransformer model")
        self.delegate = delegate
        self.adapter = SentenceTransformerEncodingAdapter(model, EncodingContract(**dict(contract)))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @staticmethod
    def _action(memory_action: str | None) -> str:
        return "search" if memory_action == "search" else "add"

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        return self.adapter.encode([text], action=self._action(memory_action))[0]

    def embed_batch(self, texts: Sequence[str], memory_action: str | None = "add") -> list[list[float]]:
        return self.adapter.encode(texts, action=self._action(memory_action))


def _deep_merge_config(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge_config(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


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
    # Source-changing candidates carry explicit production-config overrides;
    # query-time candidates never reach this path.  Merge only declared config
    # objects and let Pydantic enforce the production schema/ranges.
    overrides = spec.get("config_overrides") or {}
    if isinstance(overrides, Mapping) and overrides:
        runtime_vector_config = dict((config.get("vector_store") or {}).get("config") or {})
        isolated_vector_config = {
            key: runtime_vector_config[key]
            for key in ("path", "collection_name")
            if key in runtime_vector_config
        }
        isolated_history_db_path = config.get("history_db_path")
        config = _deep_merge_config(config, overrides)
        config.setdefault("vector_store", {}).setdefault("config", {}).update(isolated_vector_config)
        if isolated_history_db_path is not None:
            config["history_db_path"] = isolated_history_db_path
    # Persist the effective production config beside the trace.  Artifact
    # discovery uses it to validate complete-memory reuse without trusting
    # mutable runtime paths or a stale manifest.
    atomic_write_json(output_dir / "effective_memory_config.json", redact_secrets(config))
    config.setdefault("background", {})["midterm_worker_count"] = 1
    config["background"]["longterm_worker_count"] = 1
    page_summary_prompt = str(spec.get("page_summary_prompt") or "") or None
    session_merge_prompt = str(spec.get("session_merge_prompt") or "") or None
    fine_grained_longterm_extraction_prompt = str(
        spec.get("fine_grained_longterm_extraction_prompt")
        or spec.get("session_longterm_extraction_prompt")
        or ""
    ) or None
    memory = create_production_memory(config, llm_mode=str(spec.get("llm_mode") or "real"))
    prompt_kwargs = {
        "page_summary_prompt": page_summary_prompt,
        "session_merge_prompt": session_merge_prompt,
        "fine_grained_longterm_extraction_prompt": fine_grained_longterm_extraction_prompt,
    }
    if hasattr(memory.llm, "_delegate"):
        # Keep TunerPolicyLLM outside the override so it still recognizes the
        # production operation before the instance-scoped delegate replaces it.
        memory.llm._delegate = PromptOverrideLLM(memory.llm._delegate, **prompt_kwargs)
        counted = CountingLLM(memory.llm)
    else:
        counted = CountingLLM(PromptOverrideLLM(memory.llm, **prompt_kwargs))
    encoding_contract = dict(spec.get("embedding_encoding_contract") or {})
    source_embedding = (
        EncodingContractEmbedding(memory.embedding_model, encoding_contract)
        if encoding_contract
        else memory.embedding_model
    )
    counted_embedding = CountingEmbedding(source_embedding)
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
            # Production now creates Fine-grained LongTerm for every complete
            # QA.  Freeze each checkpoint only after all preceding source jobs
            # (LongTerm, MidTerm, Profile) have reached a terminal state.
            await memory.flush_background_tasks(timeout=job_timeout)
            pending.clear()
            checkpoint = await asyncio.to_thread(
                _checkpoint,
                memory,
                query_id=turn.turn_id,
                query=turn.question,
                session_id=session.session_id,
                user_id=user_id,
                lineage=lineage,
                ranking_depth=int(spec["ranking_depth"]),
            )
            checkpoints.append(checkpoint)
            valid_recalled_page_ids: list[str] = []
            if bool(spec.get("stateful_replay")):
                valid_recalled_page_ids = _valid_recalled_page_ids(turn, checkpoint)
                if valid_recalled_page_ids:
                    await asyncio.to_thread(
                        memory._confirm_valid_midterm_page_ids,
                        valid_recalled_page_ids,
                        current_turn_index=int(checkpoint["current_turn_index"]),
                    )
                checkpoint["post_recall_heat_states"] = await asyncio.to_thread(
                    _session_heat_states,
                    memory,
                    user_id=user_id,
                    session_id=session.session_id,
                    current_turn_index=int(checkpoint["current_turn_index"]),
                )
                checkpoint["promotion_events"] = [
                    {
                        "memory_id": state["session_id"],
                        "H_segment": state["H_segment"],
                        "valid_recall_count": state["valid_recall_count"],
                        "current_turn_index": state["current_turn_index"],
                    }
                    for state in checkpoint["post_recall_heat_states"]
                    if state["promotion_eligible"]
                ]
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
                infer=True,
            )
            migration_job_id = (add_result.get("background") or {}).get("migration_job_id")
            evicted = lineage.register_add(session.session_id, turn.turn_id, migration_job_id)
            if migration_job_id:
                pending.append(str(migration_job_id))
            layered = list(checkpoint.get("layered_results") or [])
            all_retrieved_turn_ids = [
                str(item.get("source_turn_id") or "").upper()
                for item in layered
                if isinstance(item, Mapping) and item.get("source_turn_id")
            ]
            long_retrieved_turn_ids = [
                str(item.get("source_turn_id") or "").upper()
                for item in layered
                if isinstance(item, Mapping)
                and item.get("source_turn_id")
                and str(item.get("source") or "") not in {"mid_term_page", "mid_term_session", "midterm"}
            ]
            turn_rows.append(
                {
                    "session_id": session.session_id,
                    "turn_id": turn.turn_id,
                    "turn_index": turn.turn_index,
                    "migration_job_id": migration_job_id,
                    "evicted_turn_ids": list(evicted),
                    "error": None,
                    "mid_retrieved_turn_ids": [
                        str(item.get("source_turn_id") or "").upper()
                        for item in checkpoint.get("baseline_ranking") or []
                        if item.get("source_turn_id")
                    ],
                    "all_memory_results": layered,
                    "long_retrieved_turn_ids": list(dict.fromkeys(long_retrieved_turn_ids)),
                    "all_retrieved_turn_ids": list(dict.fromkeys(all_retrieved_turn_ids)),
                    "valid_recalled_page_ids": valid_recalled_page_ids,
                    "heat_states": list(checkpoint.get("post_recall_heat_states") or checkpoint.get("heat_states") or []),
                    "promotion_events": list(checkpoint.get("promotion_events") or []),
                    "current_turn_index": int(checkpoint["current_turn_index"]),
                    "stateful_replay": bool(spec.get("stateful_replay")),
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
    prompt_hashes = production_prompt_hashes(
        page_summary_prompt=page_summary_prompt,
        session_merge_prompt=session_merge_prompt,
        fine_grained_longterm_extraction_prompt=fine_grained_longterm_extraction_prompt,
    )
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
        "config_overrides": redact_secrets(dict(spec.get("config_overrides") or {})),
        "effective_config_hash": stable_hash(redact_secrets(config)),
        "prompt_hashes": prompt_hashes,
        "source_variant": str(spec.get("source_variant") or "production"),
        "page_context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
        "production_memory_contract": PRODUCTION_MEMORY_CONTRACT,
        "source_identity": dict(spec.get("source_identity") or {}),
        "stateful_replay": bool(spec.get("stateful_replay")),
        "llm_mode": str(spec.get("llm_mode") or "real"),
        "llm_calls": counted.calls,
        "embedding_calls": counted_embedding.calls,
        "embedding_model": deepcopy(config.get("embedder") or {}),
        "embedding_model_id": spec.get("embedding_model_id"),
        "embedding_model_revision": spec.get("embedding_model_revision"),
        "embedding_encoding_contract": encoding_contract,
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
    page_summary_prompt: str | None = None,
    session_merge_prompt: str | None = None,
    fine_grained_longterm_extraction_prompt: str | None = None,
    session_longterm_extraction_prompt: str | None = None,
    source_variant: str = "production",
    source_identity: Mapping[str, Any] | None = None,
    source_root: Path | None = None,
    generation_stats: dict[str, Any] | None = None,
    config_overrides: Mapping[str, Any] | None = None,
    stateful_replay: bool = False,
    embedding_encoding_contract: Mapping[str, Any] | None = None,
    embedding_model_id: str | None = None,
    embedding_model_revision: str | None = None,
) -> list[Path]:
    """Generate missing production artifacts in isolated subprocesses."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    source_root = source_root or run_dir / "production_source"
    source_root.mkdir(parents=True, exist_ok=True)
    memory_config_sha256 = sha256_file(memory_config_path)
    prompt_hashes = production_prompt_hashes(
        page_summary_prompt=page_summary_prompt,
        session_merge_prompt=session_merge_prompt,
        fine_grained_longterm_extraction_prompt=fine_grained_longterm_extraction_prompt,
        session_longterm_extraction_prompt=session_longterm_extraction_prompt,
    )
    expected_source_identity = dict(source_identity or {})
    config_overrides_hash = stable_hash(dict(config_overrides or {}))
    embedding_contract_hash = stable_hash(dict(embedding_encoding_contract or {}))
    generated_paths: list[Path] = []
    generated_lock = threading.Lock()

    def run_session(session_id: str) -> Path:
        code = _safe_component(session_id)
        result_dir = source_root / code
        manifest_path = result_dir / "production_midterm_manifest.json"
        if manifest_path.exists():
            value = load_json(manifest_path)
            if (
                value.get("schema") == ADAPTER_SCHEMA
                and value.get("status") == "COMPLETE"
                and value.get("dataset_sha256") == dataset_sha256
                and int(value.get("turn_count") or 0) == int(session_turn_counts[session_id])
                and int(value.get("failed_turns") or 0) == 0
                and int(value.get("ranking_depth") or 0) >= ranking_depth
                and value.get("memory_config_sha256") == memory_config_sha256
                and value.get("prompt_hashes") == prompt_hashes
                and str(value.get("source_variant") or "production") == source_variant
                and str(value.get("page_context_contract") or "") == PRODUCTION_PAGE_CONTEXT_CONTRACT
                and dict(value.get("source_identity") or {}) == expected_source_identity
                and stable_hash(value.get("config_overrides") or {}) == config_overrides_hash
                and stable_hash(value.get("embedding_encoding_contract") or {}) == embedding_contract_hash
                and value.get("embedding_model_id") == embedding_model_id
                and value.get("embedding_model_revision") == embedding_model_revision
                and bool(value.get("stateful_replay")) is bool(stateful_replay)
                and str(value.get("llm_mode") or "real") == llm_mode
                and Path(str(value.get("checkpoints_path") or "")).exists()
                and value.get("checkpoints_sha256") == sha256_file(Path(str(value["checkpoints_path"])))
            ):
                return manifest_path
        runtime = isolated_runtime_layout(
            run_dir,
            candidate_hash=f"production-source-{stable_hash(expected_source_identity)[:12]}",
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
            "page_summary_prompt": page_summary_prompt,
            "session_merge_prompt": session_merge_prompt,
            "fine_grained_longterm_extraction_prompt": (
                fine_grained_longterm_extraction_prompt or session_longterm_extraction_prompt
            ),
            "page_context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
            "source_variant": source_variant,
            "source_identity": expected_source_identity,
            "config_overrides": dict(config_overrides or {}),
            "stateful_replay": bool(stateful_replay),
            "embedding_encoding_contract": dict(embedding_encoding_contract or {}),
            "embedding_model_id": embedding_model_id,
            "embedding_model_revision": embedding_model_revision,
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
        with generated_lock:
            generated_paths.append(manifest_path)
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
    ordered = [paths[session_id] for session_id in session_ids]
    if generation_stats is not None:
        generated = [load_json(path) for path in generated_paths]
        generation_stats.update(
            {
                "generated_sessions": len(generated_paths),
                "reused_sessions": len(ordered) - len(generated_paths),
                "llm_calls": sum(int(item.get("llm_calls") or 0) for item in generated),
                "embedding_calls": sum(int(item.get("embedding_calls") or 0) for item in generated),
            }
        )
    return ordered


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
    from mem0.configs.query_prompts import QUERY_REFERENCE_RESOLUTION_PROMPT

    manifests = [load_json(path) for path in manifest_paths]
    if not manifests:
        raise ValueError("No production MidTerm manifests")
    config = deepcopy(manifests[0].get("production_config") or {})
    effective_memory_config = manifests[0].get("effective_memory_config") or {}
    vector_config = (effective_memory_config.get("vector_store") or {}).get("config") or {}
    for key in (
        "longterm_top_k",
        "longterm_rag_threshold",
        "longterm_candidate_pool_multiplier",
        "longterm_hybrid_preset",
        "entity_similarity_threshold",
        "longterm_other_session_weight",
        "cross_session_longterm_rag_threshold",
        "cross_session_retention_half_life_hours",
        "cross_session_retention_floor",
        "cross_session_reinforcement_gain",
    ):
        if key in effective_memory_config:
            config[key] = deepcopy(effective_memory_config[key])
    agentic = effective_memory_config.get("agentic_retrieval") or {}
    if agentic:
        config["max_queries"] = int(agentic.get("max_queries", 3))
        config["max_total_results"] = min(5, int(agentic.get("max_total_results", 6)))
        config["agentic_fixed_max_iterations"] = int(agentic.get("max_iterations", 2))
        config["agentic_fixed_max_tool_calls"] = int(agentic.get("max_tool_calls", 1))
    config.update(
        {
            "backend": PRODUCTION_BACKEND,
            "retrieval_contract": "production_midterm_v1",
            "production_memory_contract": PRODUCTION_MEMORY_CONTRACT,
            "retrieval_method": "dense",
            "query_representation": "original",
            "query_prompt_text": QUERY_REFERENCE_RESOLUTION_PROMPT,
            "query_prompt_hash": hashlib.sha256(QUERY_REFERENCE_RESOLUTION_PROMPT.encode()).hexdigest(),
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
        "production_memory_contract": PRODUCTION_MEMORY_CONTRACT,
        "manifests": config["manifest_sha256"],
        "failed_turns": sum(int(item.get("failed_turns") or 0) for item in manifests),
        "llm_calls": sum(int(item.get("llm_calls") or 0) for item in manifests),
        "embedding_calls": sum(int(item.get("embedding_calls") or 0) for item in manifests),
        "candidate_name": name,
        "production_agentic_max_total_results": int(agentic.get("max_total_results", 6)) if agentic else None,
        "tuner_agentic_context_cap": 5,
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

    @staticmethod
    def _rank_fine_grained_longterm(
        checkpoint: Mapping[str, Any],
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Replay per-QA Long-term with Production cross-session ranking semantics."""
        pool = checkpoint.get("longterm_candidate_pool") or {}
        if not pool:
            return []
        top_k = min(30, max(1, int(config.get("longterm_top_k", 20))))
        multiplier = min(6, max(1, int(config.get("longterm_candidate_pool_multiplier", 4))))
        internal_limit = max(top_k * multiplier, 60)
        all_semantic_candidates = [dict(row) for row in (pool.get("semantic_candidates") or [])]
        current_candidate_ids = {str(value) for value in pool.get("current_session_candidate_ids") or []}
        current_candidates = [
            row for row in all_semantic_candidates if str(row.get("id") or "") in current_candidate_ids
        ][:internal_limit]
        routed_candidates = [*current_candidates, *all_semantic_candidates[:internal_limit]]
        semantic_candidates_by_id: dict[str, dict[str, Any]] = {}
        for row in routed_candidates:
            memory_id = str(row.get("id") or "")
            existing = semantic_candidates_by_id.get(memory_id)
            if memory_id and (existing is None or float(row.get("score") or 0.0) > float(existing.get("score") or 0.0)):
                semantic_candidates_by_id[memory_id] = row
        semantic_candidates = list(semantic_candidates_by_id.values())
        threshold = float(config.get("longterm_rag_threshold", 0.1))
        entity_threshold = float(config.get("entity_similarity_threshold", 0.5))
        entity_maps = pool.get("entity_boosts_by_threshold") or {}
        entity_boosts = entity_maps.get(f"{entity_threshold:.1f}") or {}
        preset = str(config.get("longterm_hybrid_preset") or "balanced")
        current_run_id = pool.get("current_run_id")
        other_session_weight = float(config.get("longterm_other_session_weight", 0.7))
        session_weights = {
            str(row.get("id") or ""): (
                1.0 if (row.get("payload") or {}).get("run_id") == current_run_id else other_session_weight
            )
            for row in semantic_candidates
        }
        scored = tuner_score_and_rank(
            semantic_results=semantic_candidates,
            bm25_scores={str(key): float(value) for key, value in (pool.get("bm25_scores") or {}).items()},
            entity_boosts={str(key): float(value) for key, value in entity_boosts.items()},
            threshold=threshold,
            top_k=top_k,
            explain=True,
            weights=HYBRID_PRESET_WEIGHTS[preset],
            session_weights=session_weights,
        )
        rows = []
        for rank, item in enumerate(scored, start=1):
            payload = dict(item.get("payload") or {})
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
            source_turn_id = str(
                payload.get("dataset_turn_id")
                or metadata.get("dataset_turn_id")
                or payload.get("source_turn_id")
                or ""
            ).upper()
            details = item.get("score_details") or {}
            rows.append(
                {
                    "page_id": str(item.get("id") or ""),
                    "source_turn_id": source_turn_id,
                    "source": "long_term",
                    "score": float(item.get("score") or 0.0),
                    "memory": payload.get("data") or "",
                    "raw_rag_score": details.get("semantic_score"),
                    "bm25_score": details.get("bm25_score"),
                    "entity_boost": details.get("entity_boost"),
                    "hybrid_score": details.get("hybrid_score"),
                    "session_weight": details.get("session_weight"),
                    "final_score": details.get("final_score"),
                    "candidate_pool_count": len(semantic_candidates),
                    "final_rank": rank,
                }
            )
        return rows

    _rank_session_longterm = _rank_fine_grained_longterm

    def rank(self, checkpoint: Mapping[str, Any], config: Mapping[str, Any]) -> list[dict[str, Any]]:
        from qdrant_client import QdrantClient

        from mem0.configs.base import MidTermMemoryConfig
        from mem0.memory.midterm import MidTermMemory

        self.supported(config)
        derived = self._derived_payload(config)
        query_id = str(checkpoint["query_id"])
        query = str(
            (derived or {}).get("query_texts", {}).get(query_id)
            or checkpoint.get("retrieval_query")
            or checkpoint["query"]
        )
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
            current_turn_index_provider=lambda filters: int(
                checkpoint.get("current_turn_index", max(
                    (int((point.get("payload") or {}).get("turn_index"))
                     for point in checkpoint.get("pages") or []
                     if (point.get("payload") or {}).get("turn_index") is not None),
                    default=0,
                ))
            ),
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
                payloads = []
                for point in points:
                    payload = dict(point["payload"])
                    if key == "pages":
                        # Checkpoints emitted before the turn-clock contract
                        # are still replayable at the current query clock.
                        payload.setdefault("turn_index", int(checkpoint.get("current_turn_index", 0)))
                    payloads.append(payload)
                store.insert(
                        vectors=[
                            list(replacements.get(point_fingerprint(point)) or point["vector"]) for point in points
                        ],
                        ids=[str(point["id"]) for point in points],
                    payloads=payloads,
                )
            retriever = DiagnosticMidTermRetriever(memory, midterm_config)
            results = retriever.search(
                query,
                dict(checkpoint["filters"]),
                record_visits=False,
            )
            job_map = {
                str(job_id): [str(turn_id).upper() for turn_id in turn_ids]
                for job_id, turn_ids in (checkpoint.get("source_turn_ids_by_job") or {}).items()
            }
            retrieval_diagnostics = retriever.last_search_diagnostics
            diagnostic_pages = list(retrieval_diagnostics.get("pre_threshold_ranking") or [])
            if diagnostic_pages:
                # Reranking is allowed to inspect a diagnostic candidate depth;
                # the final_visible flags are recomputed below and still obey
                # production max_total_pages.
                page_results = self._rerank_pages(
                    [dict(row) for row in diagnostic_pages],
                    query=query,
                    query_vector=query_vector,
                    point_by_id=point_by_kind_and_id["pages"],
                    derived=derived,
                    config=config,
                )
                max_total_pages = min(5, max(1, int(config.get("max_total_pages", 5))))
                for rank, row in enumerate(page_results, start=1):
                    row["rank_before_threshold"] = rank
                thresholded = [row for row in page_results if not bool(row.get("threshold_filtered"))]
                for rank, row in enumerate(thresholded, start=1):
                    row["final_rank"] = rank
                    row["final_visible"] = rank <= max_total_pages
                ranking = _diagnostic_rows_from_pages(page_results, job_map, ranking_depth=self.ranking_depth)
                ranking.extend(_unselected_page_diagnostic_rows(checkpoint, retrieval_diagnostics, job_map))
            else:
                # Checkpoints produced before the diagnostic contract remain
                # replayable, but are explicitly a degraded trace.
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
                ranking = _diagnostic_rows_from_pages(ordered_results, job_map)
                for rank, row in enumerate(ranking, start=1):
                    row["rank_before_threshold"] = row.get("rank_before_threshold") or rank
                    row["final_rank"] = row.get("final_rank") or rank
                    max_total_pages = min(5, max(1, int(config.get("max_total_pages", 5))))
                    row["final_visible"] = row.get("final_visible", rank <= max_total_pages)
            longterm_ranking = self._rank_fine_grained_longterm(checkpoint, config)
            combined = [*ranking, *longterm_ranking]
            return [{**row, "rank": rank} for rank, row in enumerate(combined, start=1)]
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
