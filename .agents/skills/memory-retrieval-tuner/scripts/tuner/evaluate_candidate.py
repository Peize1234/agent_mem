from __future__ import annotations

import copy
import statistics
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agentic_retrieval_artifacts import (
    agentic_supplement_rows,
    build_agentic_parent_retrieval_identity,
    load_production_agentic_trace,
)
from .artifact_registry import ArtifactRegistry
from .fact_evaluator import FactRequirement, fact_member_hit, parse_required_context, uses_context_gold
from .io_utils import atomic_write_json, iter_jsonl, load_jsonl, stable_hash
from .models import Candidate, CandidateResult, Dataset, Requirement, Turn
from .parameter_schema import production_parameter_metadata, validate_candidate_config
from .production_midterm_adapter import (
    PRODUCTION_BACKEND,
    ProductionMidtermAdapter,
    checkpoint_paths_by_session,
)

_DEFAULT_MAX_TOTAL_PAGES = int(production_parameter_metadata("max_total_pages").default)
_DEFAULT_AGENTIC_MAX_TOTAL_RESULTS = int(production_parameter_metadata("max_total_results").default)
_DEFAULT_LONGTERM_TOP_K = int(production_parameter_metadata("longterm_top_k").default)
_EVALUATION_SCHEMA = 3


def candidate_hash(dataset_sha256: str, candidate: Candidate) -> str:
    return stable_hash({"dataset_sha256": dataset_sha256, "config": _ranking_identity_config(candidate.config)})


def _ranking_identity_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Remove mutable cache paths while retaining their immutable content identities."""
    value = dict(config)
    manifests = value.pop("manifest_sha256", {}) or {}
    value.pop("manifest_paths", None)
    value["manifest_content_sha256"] = sorted(str(item) for item in manifests.values())
    source_spec = value.pop("source_generation_spec", None)
    if isinstance(source_spec, Mapping):
        value["source_generation_identity"] = source_spec.get("source_identity")
    for path_key, hash_key in (
        ("derived_artifact_path", "derived_artifact_sha256"),
        ("query_artifact_path", "query_artifact_sha256"),
        ("embedding_model_path", "embedding_model_revision"),
        ("reranker_model_path", "reranker_model_revision"),
    ):
        if value.get(path_key):
            value.pop(path_key, None)
            value[f"{path_key}_identity"] = value.get(hash_key)
    for key in ("experiment_branch", "branch_cost_level", "parent_candidate_hash", "applied_branches"):
        value.pop(key, None)
    return value


def _eligible_requirements(
    turn: Turn, session_turns: Sequence[Turn], target: str, shortterm_window: int
) -> list[Requirement]:
    dependency_type = str(turn.dependency_type or "").lower()
    if any(marker in dependency_type for marker in ("cross", "temporal", "promotion")):
        return []
    local_ids = {item.query_id for item in session_turns}
    local_requirements = [
        requirement for requirement in turn.requirements if all(member in local_ids for member in requirement.members)
    ]
    # Reconstructed workbooks use ``关联前序对话`` as deterministic Gold
    # source IDs.  Text ``required_context`` remains supported only for
    # ID-less legacy/context-only fixtures, whose denominator is fixed.
    if uses_context_gold(turn):
        return [Requirement((), str(turn.required_context))]
    if target == "all_memory":
        return local_requirements
    shortterm_ids = {
        item.query_id for item in session_turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]
    }
    return [
        requirement
        for requirement in local_requirements
        if not any(member in shortterm_ids for member in requirement.members)
    ]


def _ranking_layers(rankings: Mapping[str, Any], query_id: str) -> dict[str, list[dict[str, Any]]]:
    raw = rankings.get(query_id, [])
    if isinstance(raw, Mapping):
        layers = {}
        for key, value in raw.items():
            if key in {
                "shortterm",
                "sessions",
                "midterm",
                "agentic",
                "session_longterm",
                "cross_session_longterm",
                "all_memory",
                "rows",
            }:
                layers[key] = [dict(item) for item in (value or [])]
        if layers:
            return layers
    rows = [dict(item) for item in (raw or [])]
    sessions, midterm, agentic, longterm, cross = [], [], [], [], []
    for row in rows:
        source = str(row.get("layer") or row.get("memory_layer") or row.get("source") or "").lower()
        if "cross_session" in source:
            cross.append(row)
        elif source == "production_agentic_supplement":
            agentic.append(row)
        elif source == "mid_term_session":
            sessions.append(row)
        elif "long" in source:
            longterm.append(row)
        elif "short" in source:
            # ShortTerm rows are generally supplied separately, but accepting
            # them here makes the evaluator usable with full trace artifacts.
            rows_short = [row]
            return {
                "shortterm": rows_short,
                "sessions": sessions,
                "midterm": midterm,
                "agentic": agentic,
                "session_longterm": longterm,
                "cross_session_longterm": cross,
            }
        else:
            midterm.append(row)
    return {
        "sessions": sessions,
        "midterm": midterm,
        "agentic": agentic,
        "session_longterm": longterm,
        "cross_session_longterm": cross,
    }


def _row_text(row: Mapping[str, Any]) -> str:
    identifiers = " ".join(str(row.get(key) or "") for key in ("source_turn_id", "turn_id", "page_id", "id"))
    content = " ".join(
        str(row.get(key) or "") for key in ("raw_dialogue", "content", "memory", "summary", "text", "data")
    )
    return f"{identifiers} {content}".strip()


def _visible_midterm_rows(rows: Sequence[Mapping[str, Any]], context_budget: int) -> list[dict[str, Any]]:
    """Select the production-visible Mid-term Pages from a diagnostic trace."""
    budget = min(5, max(1, int(context_budget)))
    diagnostic_rows = [row for row in rows if "final_visible" in row or "threshold_passed" in row]
    if diagnostic_rows:
        return [
            dict(row)
            for row in diagnostic_rows
            if row.get("final_visible") is True and row.get("threshold_filtered") is not True
        ][:budget]
    return [dict(row) for row in rows[:budget]]


def _fact_rows_for_visible(
    turn: Turn,
    visible_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    shortterm_rows: Sequence[Mapping[str, Any]],
    *,
    k: int,
    midterm_rows: Sequence[Mapping[str, Any]] = (),
    agentic_rows: Sequence[Mapping[str, Any]] = (),
    session_longterm_rows: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[FactRequirement]]:
    requirements = list(parse_required_context(turn.required_context))
    if not requirements:
        return [], []
    retrieval_candidate_rows = [
        row for row in candidate_rows if str(row.get("source") or "").lower() != "production_agentic_supplement"
    ]
    pre_threshold_rows = [
        row
        for row in retrieval_candidate_rows
        if row.get("in_candidate_pool", True) is not False
        and str(row.get("source") or "").lower()
        not in {"long_term", "cross_session_long_term", "shortterm", "mid_term_session"}
    ]
    candidate_text = "\n".join(_row_text(row) for row in pre_threshold_rows)
    post_threshold_rows = [row for row in pre_threshold_rows if row.get("threshold_filtered") is not True]
    post_threshold_text = "\n".join(_row_text(row) for row in post_threshold_rows)
    short_text = "\n".join(_row_text(row) for row in shortterm_rows)
    # ``visible_rows`` has already been clipped to the actual configured
    # context budgets by the caller.  Never substitute evaluation K for the
    # final Mid-term Page budget.
    final_text = "\n".join(_row_text(row) for row in visible_rows)
    output = []
    for index, requirement in enumerate(requirements, start=1):

        def hit(text: str) -> bool:
            return any(fact_member_hit(member, text) for member in requirement.members)

        rank = None
        matched_row: Mapping[str, Any] | None = None
        for row_index, row in enumerate(pre_threshold_rows, start=1):
            if hit(_row_text(row)):
                rank = int(row.get("rank_before_threshold") or row_index)
                matched_row = row
                break
        routed_hit = any(
            hit(_row_text(row))
            and bool(row.get("in_routed_pool", row.get("routed_candidate", not row.get("global_supplement"))))
            for row in retrieval_candidate_rows
            if str(row.get("source") or "").lower() not in {"mid_term_session", "long_term", "cross_session_long_term"}
        )
        final_hit = hit(final_text)
        midterm_final_hit = hit("\n".join(_row_text(row) for row in midterm_rows))
        candidate_hit = hit(candidate_text)
        post_threshold_hit = hit(post_threshold_text)
        if final_hit:
            failure_class = None
        elif not candidate_hit and not retrieval_candidate_rows:
            failure_class = "Source Generation Loss"
        elif not candidate_hit and not routed_hit:
            failure_class = "Session Routing Loss"
        elif not candidate_hit:
            failure_class = "Candidate Coverage Loss"
        elif matched_row and matched_row.get("threshold_filtered"):
            failure_class = "Threshold Loss"
        elif matched_row and not any(matched_row.get(key) for key in ("raw_dialogue", "memory", "summary", "content")):
            failure_class = "Representation Loss"
        elif matched_row and matched_row.get("ranking_loss"):
            failure_class = "Ranking Loss"
        elif matched_row and (
            matched_row.get("context_budget_filtered")
            or matched_row.get("final_visible") is False
            or matched_row.get("final_rank") is not None
        ):
            failure_class = "Context Budget Loss"
        else:
            failure_class = "Ranking Loss"
        output.append(
            {
                "requirement_id": f"{turn.query_id}::CONTEXT{index}",
                "session_id": turn.session_id,
                "query_id": turn.query_id,
                "turn_index": turn.turn_index,
                "gold_members": list(requirement.members),
                "is_or": requirement.is_or,
                "best_rank": rank,
                "hit_at_k": final_hit,
                "hit_at_2k": final_hit
                or hit("\n".join(_row_text(row) for row in retrieval_candidate_rows[: 2 * max(k, 1)])),
                "hit_at_4k": final_hit
                or hit("\n".join(_row_text(row) for row in retrieval_candidate_rows[: 4 * max(k, 1)])),
                "candidate_pool_hit": candidate_hit,
                "post_threshold_hit": post_threshold_hit,
                "final_context_hit": final_hit,
                "midterm_final_context_hit": midterm_final_hit,
                "shortterm_hit": hit(short_text),
                "midterm_hit": hit("\n".join(_row_text(row) for row in midterm_rows)),
                "agentic_hit": hit("\n".join(_row_text(row) for row in agentic_rows)),
                "session_longterm_hit": hit("\n".join(_row_text(row) for row in session_longterm_rows[:30])),
                "reciprocal_rank": 1.0 / rank if rank else 0.0,
                "failure_class": failure_class,
                "diagnostics": {
                    "routed_pool": routed_hit,
                    "global_supplement": any(
                        hit(_row_text(row)) and bool(row.get("global_supplement")) for row in midterm_rows
                    ),
                    "candidate_pool": hit(candidate_text),
                    "post_threshold": post_threshold_hit,
                    "final_context": hit(final_text),
                    "raw_rag_score": matched_row.get("raw_rag_score") if matched_row else None,
                    "forgetting_factor": matched_row.get("forgetting_factor") if matched_row else None,
                    "heat_modulation": matched_row.get("heat_modulation") if matched_row else None,
                    "final_score": matched_row.get("final_score") if matched_row else None,
                    "threshold_filtered": bool(matched_row.get("threshold_filtered")) if matched_row else None,
                    "threshold_passed": bool(matched_row.get("threshold_passed")) if matched_row else None,
                    "rank_before_threshold": rank,
                    "final_rank": matched_row.get("final_rank") if matched_row else None,
                    "final_visible": matched_row.get("final_visible") if matched_row else None,
                    "context_budget_filtered": bool(matched_row.get("context_budget_filtered"))
                    if matched_row
                    else None,
                },
            }
        )
    return output, requirements


def _apply_retrieval_controls(
    ranking: Sequence[Mapping[str, Any]], config: Mapping[str, Any], ranking_depth: int
) -> list[dict[str, Any]]:
    rows = [dict(row) for row in ranking]
    all_midterm = [
        row for row in rows if str(row.get("source") or "").lower() not in {"long_term", "cross_session_long_term"}
    ]
    # A production diagnostic ranking contains the complete deduplicated
    # pre-threshold pool. Preserve it for candidate-pool recall; final context
    # selection is performed from explicit ``final_visible`` flags below.
    has_diagnostic_pool = any(row.get("in_candidate_pool") is True or "final_visible" in row for row in all_midterm)
    midterm = all_midterm if has_diagnostic_pool else all_midterm[:ranking_depth]
    session_longterm = [row for row in rows if str(row.get("source") or "").lower() == "long_term"][
        : int(config.get("longterm_top_k", _DEFAULT_LONGTERM_TOP_K))
    ]
    cross_session = [row for row in rows if str(row.get("source") or "").lower() == "cross_session_long_term"]
    rows = [*midterm, *session_longterm, *cross_session]
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
                **{
                    key: row[key]
                    for key in (
                        "source",
                        "layer",
                        "memory_layer",
                        "memory",
                        "summary",
                        "raw_dialogue",
                        "content",
                        "text",
                    )
                    if key in row
                },
            }
        )
    for query_id in grouped:
        grouped[query_id].sort(key=lambda row: (int(row["rank"]), str(row["page_id"])))
    return grouped


def _load_production_trace_rankings(config: Mapping[str, Any], target: str) -> dict[str, list[dict[str, Any]]]:
    field = {
        "midterm": "mid_retrieved_turn_ids",
        "longterm": "fine_grained_longterm_retrieved_turn_ids",
        "all_memory": "all_retrieved_turn_ids",
    }[target]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw_path in config.get("trace_paths") or []:
        for row in load_jsonl(Path(str(raw_path))):
            query_id = str(row.get("turn_id") or row.get("query_id") or "").upper()
            if not query_id or row.get("error"):
                continue
            layered = [item for item in row.get("all_memory_results") or [] if isinstance(item, Mapping)]
            if target == "midterm":
                layered = [
                    item
                    for item in layered
                    if str(item.get("source") or "") in {"mid_term_page", "mid_term_session", "midterm"}
                ]
            elif target == "longterm":
                layered = [item for item in layered if str(item.get("source") or "") == "long_term"]
            else:
                layered = [item for item in layered if "cross_session" not in str(item.get("source") or "").lower()]
            if layered:
                grouped[query_id] = [
                    {
                        "page_id": str(item.get("id") or item.get("source_turn_id") or rank),
                        "source_turn_id": str(item.get("source_turn_id") or item.get("id") or rank).upper(),
                        "rank": rank,
                        "score": float(item.get("score") or 0.0),
                        "source": str(item.get("source") or ""),
                        "memory": item.get("memory"),
                        "summary": item.get("summary"),
                        "raw_dialogue": item.get("raw_dialogue"),
                    }
                    for rank, item in enumerate(layered, start=1)
                ]
                continue
            retrieved = [str(value).upper() for value in row.get(field) or []]
            grouped[query_id] = []
            for rank, source_turn_id in enumerate(dict.fromkeys(retrieved), start=1):
                grouped[query_id].append(
                    {
                        "page_id": source_turn_id,
                        "source_turn_id": source_turn_id,
                        "rank": rank,
                        "score": 0.0,
                        "source": "mid_term_page" if field == "mid_retrieved_turn_ids" else "long_term",
                    }
                )
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
    agentic: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    checkpoint_path: Path | None,
    run_dir: Path,
    candidate_id: str,
) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    ranking_config = _ranking_identity_config(candidate.config)
    raw_depth = ranking_depth + int(candidate.config.get("longterm_top_k", _DEFAULT_LONGTERM_TOP_K))
    identity = {
        "schema": _EVALUATION_SCHEMA,
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
            ranking_depth=ranking_depth,
        )
        if candidate.config.get("backend") == PRODUCTION_BACKEND
        else None
    )
    checkpoint_iterator = iter_jsonl(checkpoint_path) if checkpoint_path is not None else None
    for turn in session_turns:
        checkpoint = None
        if checkpoint_iterator is not None:
            try:
                checkpoint = next(checkpoint_iterator)
            except StopIteration as exc:
                raise ValueError(f"Production checkpoint is incomplete for Query {turn.query_id}") from exc
            checkpoint_query_id = str(checkpoint.get("query_id") or "").upper()
            if checkpoint_query_id != turn.query_id:
                raise ValueError(
                    f"Production checkpoint order mismatch for {session_id}: "
                    f"expected {turn.query_id}, got {checkpoint_query_id or '<missing>'}"
                )
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
            if checkpoint is None:
                raise ValueError(f"Production checkpoint is incomplete for eligible Query {turn.query_id}")
            ranking = adapter.rank(checkpoint, candidate.config) if adapter is not None else []
        else:
            raise ValueError(
                f"Candidate {candidate.name} does not use a supported production/frozen backend: {backend!r}"
            )
        if candidate.config.get("agentic_trace_enabled") is True:
            if turn.query_id not in (agentic or {}):
                raise ValueError(f"Production Agentic trace is incomplete for Query {turn.query_id}")
            ranking = [*ranking, *(dict(row) for row in (agentic or {})[turn.query_id])]
        grouped[turn.query_id] = _apply_retrieval_controls(ranking, candidate.config, ranking_depth)
        flat.extend({"query_id": turn.query_id, **row} for row in ranking)
    if checkpoint_iterator is not None:
        try:
            extra_checkpoint = next(checkpoint_iterator)
        except StopIteration:
            pass
        else:
            raise ValueError(
                f"Production checkpoint contains extra Query for {session_id}: "
                f"{str(extra_checkpoint.get('query_id') or '<missing>').upper()}"
            )
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
    max_total_pages: int = _DEFAULT_MAX_TOTAL_PAGES,
    agentic_max_total_results: int = _DEFAULT_AGENTIC_MAX_TOTAL_RESULTS,
    longterm_top_k: int = _DEFAULT_LONGTERM_TOP_K,
) -> dict[str, Any]:
    validated_limits = validate_candidate_config(
        {
            "max_total_pages": max_total_pages,
            "max_total_results": agentic_max_total_results,
            "longterm_top_k": longterm_top_k,
        }
    )
    max_total_pages = int(validated_limits["max_total_pages"])
    agentic_max_total_results = int(validated_limits["max_total_results"])
    longterm_top_k = int(validated_limits["longterm_top_k"])
    session_turns = dataset.sessions[session_id]
    requirement_rows: list[dict[str, Any]] = []
    shortterm_total = 0
    shortterm_hits = 0
    context_mode = any(uses_context_gold(turn) for turn in session_turns)
    returned_page_counts: list[int] = []
    precision_values: list[float] = []
    contribution_counts = {"midterm": 0, "agentic": 0, "session_longterm": 0, "shortterm": 0}
    for turn in session_turns:
        shortterm_ids = [
            item.query_id for item in session_turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]
        ]
        short_rows = [
            {"source_turn_id": item.query_id, "memory": f"{item.question}\n{item.answer}", "source": "shortterm"}
            for item in session_turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]
        ]
        local_requirements = _eligible_requirements(turn, session_turns, "all_memory", 0)
        if uses_context_gold(turn):
            shortterm_requirements = parse_required_context(turn.required_context)
            shortterm_text = "\n".join(_row_text(row) for row in short_rows)
            shortterm_total += len(shortterm_requirements)
            shortterm_hits += sum(
                any(fact_member_hit(member, shortterm_text) for member in requirement.members)
                for requirement in shortterm_requirements
            )
        else:
            shortterm_total += len(local_requirements)
            shortterm_hits += sum(
                any(member in shortterm_ids for member in requirement.members) for requirement in local_requirements
            )
        eligible = _eligible_requirements(turn, session_turns, target, shortterm_window)
        if not eligible:
            continue
        layers = _ranking_layers(rankings, turn.query_id)
        session_rows = layers.get("sessions", [])
        midterm_rows = layers.get("midterm", [])
        agentic_rows = layers.get("agentic", [])
        longterm_rows = layers.get("session_longterm", [])
        # Cross-session memory has a separate Temporal Replay/Gold contract.
        # It is never counted in an ordinary run_id-scoped Session benchmark.
        candidate_rows = [*session_rows, *midterm_rows, *agentic_rows, *longterm_rows]
        candidate_page_ids = {
            str(item.get("page_id") or item.get("id") or "")
            for item in midterm_rows
            if str(item.get("source") or "").lower()
            not in {"mid_term_session", "long_term", "cross_session_longterm", "cross_session_long_term"}
            and item.get("in_candidate_pool", True) is not False
            and (item.get("page_id") or item.get("id"))
        }
        context_budget = min(5, max_total_pages)
        # Candidate configs are not part of the evaluator API; callers may
        # pass a synthetic ``max_total_pages`` on the ranking mapping.
        if isinstance(rankings.get("__meta__"), Mapping):
            configured_budget = validate_candidate_config(
                {"max_total_pages": rankings["__meta__"].get("max_total_pages", context_budget)}
            )["max_total_pages"]
            context_budget = min(5, int(configured_budget))
        visible_midterm = _visible_midterm_rows(midterm_rows, context_budget)
        remaining_agentic_budget = max(0, 5 - len(visible_midterm))
        visible_agentic = agentic_rows[: min(5, agentic_max_total_results, remaining_agentic_budget)]
        visible_longterm = longterm_rows[:longterm_top_k]
        visible_rows = [*short_rows, *session_rows, *visible_midterm, *visible_agentic, *visible_longterm]
        returned_page_counts.append(len(visible_midterm) + len(visible_agentic) + len(visible_longterm))
        if context_mode and uses_context_gold(turn):
            context_rows, context_requirements = _fact_rows_for_visible(
                turn,
                visible_rows,
                candidate_rows,
                short_rows,
                k=k,
                midterm_rows=[*session_rows, *visible_midterm],
                agentic_rows=visible_agentic,
                session_longterm_rows=visible_longterm,
            )
            for row in context_rows:
                row.update(
                    {
                        "selected_session_count": len(session_rows),
                        "session_routed_page_count": max(
                            [int(item.get("session_routed_page_count") or 0) for item in midterm_rows]
                            or [len(midterm_rows)]
                        ),
                        "global_supplement_page_count": sum(
                            1 for item in midterm_rows if item.get("global_supplement")
                        ),
                        "dedup_candidate_count": len(candidate_page_ids),
                        "candidate_pool_count": len(candidate_page_ids),
                        "returned_page_count": len(visible_midterm),
                        "returned_agentic_count": len(visible_agentic),
                    }
                )
            # Context requirements, rather than source IDs, are the fixed Gold
            # denominator.  Preserve the legacy ID rows only for ID-only data.
            requirement_rows.extend(context_rows)
            for row in context_rows:
                contribution_counts["shortterm"] += int(bool(row["shortterm_hit"]))
                contribution_counts["midterm"] += int(bool(row["midterm_hit"]))
                contribution_counts["agentic"] += int(bool(row["agentic_hit"]))
                contribution_counts["session_longterm"] += int(bool(row["session_longterm_hit"]))
            returned_memory_rows = [*session_rows, *visible_midterm, *visible_agentic, *visible_longterm]
            relevant_pages = sum(
                any(
                    any(fact_member_hit(member, _row_text(page)) for member in requirement.members)
                    for requirement in context_requirements
                )
                for page in returned_memory_rows
            )
            precision_values.append(relevant_pages / max(len(returned_memory_rows), 1))
            continue
        ranked_ids = [str(row.get("source_turn_id") or row.get("page_id")) for row in midterm_rows]
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
                    "selected_session_count": len(layers.get("sessions", [])),
                    "session_routed_page_count": len(midterm_rows),
                    "global_supplement_page_count": sum(
                        1 for item in midterm_rows if bool(item.get("global_supplement"))
                    ),
                    "dedup_candidate_count": len(candidate_page_ids),
                    "candidate_pool_count": len(candidate_page_ids),
                    "returned_page_count": len(_visible_midterm_rows(midterm_rows, context_budget)),
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
        "candidate_pool_recall": sum(
            bool(row.get("candidate_pool_hit", row.get("best_rank") is not None)) for row in requirement_rows
        )
        / total
        if total
        else 0.0,
        "post_threshold_recall": sum(bool(row.get("post_threshold_hit")) for row in requirement_rows) / total
        if total
        else 0.0,
        "midterm_final_context_recall": sum(bool(row.get("midterm_final_context_hit")) for row in requirement_rows)
        / total
        if total
        else 0.0,
        "final_context_recall": sum(bool(row.get("final_context_hit", row.get("hit_at_k"))) for row in requirement_rows)
        / total
        if total
        else 0.0,
        "context_precision": statistics.fmean(precision_values) if precision_values else 0.0,
        "mean_returned_pages": statistics.fmean(returned_page_counts) if returned_page_counts else 0.0,
        "shortterm_contribution": contribution_counts["shortterm"] / total if total else 0.0,
        "midterm_contribution": contribution_counts["midterm"] / total if total else 0.0,
        "agentic_contribution": contribution_counts["agentic"] / total if total else 0.0,
        "session_longterm_contribution": contribution_counts["session_longterm"] / total if total else 0.0,
        "short_mid_session_longterm_union": sum(
            bool(row.get("final_context_hit", row.get("hit_at_k"))) for row in requirement_rows
        )
        / total
        if total
        else 0.0,
        "required_context_evaluation": context_mode,
        "selected_session_count": statistics.fmean(
            [float(row.get("selected_session_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
        "session_routed_page_count": statistics.fmean(
            [float(row.get("session_routed_page_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
        "global_supplement_page_count": statistics.fmean(
            [float(row.get("global_supplement_page_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
        "dedup_candidate_count": statistics.fmean(
            [float(row.get("dedup_candidate_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
        "candidate_pool_count": statistics.fmean(
            [float(row.get("candidate_pool_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
        "returned_page_count": statistics.fmean(
            [float(row.get("returned_page_count") or 0) for row in requirement_rows]
        )
        if requirement_rows
        else 0.0,
    }
    metrics["target_layer_union"] = (shortterm_hits + sum(row["hit_at_k"] for row in requirement_rows)) / max(
        shortterm_total, 1
    )
    if not context_mode:
        # ID-backed Gold keeps ShortTerm coverage separate from the eligible
        # MidTerm rows.  These are marginal layer rates over the same fixed
        # Gold denominator, not additive components of R@K.
        metrics["shortterm_contribution"] = shortterm_hits / max(shortterm_total, 1)
        metrics["midterm_contribution"] = sum(bool(row["hit_at_k"]) for row in requirement_rows) / max(
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
    for name in (
        "candidate_pool_recall",
        "post_threshold_recall",
        "midterm_final_context_recall",
        "final_context_recall",
        "context_precision",
        "mean_returned_pages",
        "candidate_pool_count",
        "returned_page_count",
        "shortterm_contribution",
        "midterm_contribution",
        "agentic_contribution",
        "session_longterm_contribution",
        "short_mid_session_longterm_union",
    ):
        values = [float(row.get(name) or 0.0) for row in session_rows]
        metrics[name] = statistics.fmean(values) if values else 0.0
    metrics["query_completion"] = metrics["final_context_recall"]
    if any(bool(row.get("required_context_evaluation")) for row in session_rows):
        metrics["target_layer_union"] = metrics["final_context_recall"]
        metrics["all_memory_union"] = metrics["final_context_recall"]
        metrics["query_completion"] = metrics["final_context_recall"]
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
    try:
        # Legacy frozen ID-ranking fixtures predate the production context
        # budget and are retained only for cache regression tests.  Live
        # production/source candidates always take the strict path.
        if candidate.config.get("backend") != "frozen_ranking":
            validate_candidate_config(candidate.config)
        if candidate.config.get("agentic_trace_enabled") is True:
            expected_parent_identity = build_agentic_parent_retrieval_identity(candidate.config)
            trace = load_production_agentic_trace(
                candidate.config,
                dataset_sha256=dataset.sha256,
                query_ids_by_session={
                    session_id: [turn.query_id for turn in turns] for session_id, turns in dataset.sessions.items()
                },
                expected_parent_retrieval_identity=expected_parent_identity,
            )
            agentic = agentic_supplement_rows(
                trace,
                max_queries=int(candidate.config["max_queries"]),
                max_total_results=int(candidate.config["max_total_results"]),
            )
        else:
            agentic = None
    except ValueError as exc:
        return CandidateResult(
            name=candidate.name,
            candidate_hash=candidate_hash(dataset.sha256, candidate),
            stage=candidate.stage,
            config=copy.deepcopy(candidate.config),
            metrics={"recall_at_k": 0.0, "invalid_reason": str(exc)},
            requirement_rows=[],
            session_rows=[],
            runtime_seconds=0.0,
            work_seconds=0.0,
            cache_hits=0,
            cache_misses=0,
            complexity=candidate.complexity,
            status="INVALID",
        )
    backend = candidate.config.get("backend")
    if backend == "frozen_ranking":
        frozen = _load_frozen_rankings(candidate.config)
    elif backend == "production_trace":
        frozen = _load_production_trace_rankings(candidate.config, target)
    else:
        frozen = None
    checkpoint_paths = (
        checkpoint_paths_by_session(candidate.config.get("manifest_paths") or [])
        if backend == PRODUCTION_BACKEND
        else None
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
            "evaluation_schema": _EVALUATION_SCHEMA,
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
            agentic=agentic,
            checkpoint_path=(checkpoint_paths or {}).get(session_id),
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
            max_total_pages=int(candidate.config.get("max_total_pages", _DEFAULT_MAX_TOTAL_PAGES)),
            agentic_max_total_results=int(
                candidate.config.get("max_total_results", _DEFAULT_AGENTIC_MAX_TOTAL_RESULTS)
            ),
            longterm_top_k=int(candidate.config.get("longterm_top_k", _DEFAULT_LONGTERM_TOP_K)),
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
    metrics["tuning_llm_calls"] = int(candidate.provenance.get("tuning_llm_calls") or 0)
    metrics["tuning_embedding_calls"] = int(candidate.provenance.get("tuning_embedding_calls") or 0)
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
    generation_stats = candidate.provenance.get("deferred_generation_stats") or {}
    reused_artifacts.extend(str(value) for value in candidate.provenance.get("reused_artifacts") or [])
    return CandidateResult(
        name=candidate.name,
        candidate_hash=candidate_id,
        stage=candidate.stage,
        config=copy.deepcopy(candidate.config),
        metrics=metrics,
        requirement_rows=requirements,
        session_rows=session_rows,
        runtime_seconds=time.perf_counter() - started,
        work_seconds=work_seconds,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        llm_calls=int(generation_stats.get("llm_calls") or 0),
        embedding_calls=int(generation_stats.get("embedding_calls") or 0),
        reused_artifacts=reused_artifacts,
        complexity=candidate.complexity,
    )
