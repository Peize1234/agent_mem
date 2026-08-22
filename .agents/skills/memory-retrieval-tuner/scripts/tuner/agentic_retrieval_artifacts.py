from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .io_utils import load_jsonl, sha256_file, stable_hash
from .parameter_schema import validate_candidate_config

PRODUCTION_AGENTIC_TRACE_SCHEMA = "production_agentic_trace_v2"
PRODUCTION_AGENTIC_EXECUTION_CONTRACT = "Memory.run_agentic_retrieval"
AGENTIC_PARENT_RETRIEVAL_IDENTITY_SCHEMA = "agentic_parent_retrieval_identity_v1"
AGENTIC_FIXED_MAX_ITERATIONS = 2
AGENTIC_FIXED_MAX_TOOL_CALLS = 1
_VALID_STATUSES = frozenset({"supplemented", "not_needed", "no_relevant_memory", "degraded"})

_MIDTERM_RETRIEVAL_FIELDS = (
    "retrieval_contract",
    "production_memory_contract",
    "top_k_sessions",
    "top_k_pages",
    "max_total_pages",
    "midterm_candidate_pool_multiplier",
    "midterm_rag_threshold",
    "retrieval_method",
    "dense_weight",
    "bm25_language",
    "reranker_method",
    "reranker_dense_weight",
    "rerank_depth",
    "candidate_depth",
    "field_weights",
    "reranker_model_id",
    "reranker_model_revision",
    "retention_half_life_turns",
    "retention_floor",
    "heat_recency_tau_turns",
    "heat_modulation_min",
    "heat_modulation_max",
    "heat_alpha",
    "heat_beta",
    "heat_gamma",
)
_QUERY_REPRESENTATION_FIELDS = (
    "query_representation",
    "query_prompt_hash",
    "query_artifact_sha256",
    "query_artifact_variant",
)
_SOURCE_FIELDS = (
    "short_term_capacity",
    "session_similarity_threshold",
    "embedding_similarity_weight",
    "keyword_overlap_weight",
    "embedding_model_id",
    "embedding_model_revision",
    "embedding_dimension",
    "encoding_contract",
    "page_summary_prompt_hash",
    "session_merge_prompt_hash",
    "fine_grained_longterm_extraction_prompt_hash",
    "session_longterm_extraction_prompt_hash",
)
_LONGTERM_FIELDS = (
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
)
_NON_SEMANTIC_SOURCE_KEYS = frozenset(
    {
        "cache_root",
        "dataset_path",
        "history_db_path",
        "manifest_paths",
        "memory_config_path",
        "model_local_path",
        "output_dir",
        "run_dir",
        "runtime_dir",
        "source_root",
        "trace_paths",
    }
)


def _identity_values(config: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: config.get(field) for field in fields}


def _semantic_source_identity(value: Any) -> Any:
    """Remove machine-local locations from a content/source identity."""

    if isinstance(value, Mapping):
        normalized = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _NON_SEMANTIC_SOURCE_KEYS or key.endswith(("_cache_path", "_runtime_path")):
                continue
            if key == "path" or (key == "model" and isinstance(item, str) and item.startswith("/")):
                continue
            normalized[key] = _semantic_source_identity(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_semantic_source_identity(item) for item in value]
    if isinstance(value, Path):
        return None
    return value


def build_agentic_parent_retrieval_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the stable, semantic identity of context seen by Agentic fallback.

    Agentic parameters and experiment bookkeeping are deliberately absent.
    Artifact paths are represented only by immutable content/source identities.
    """

    manifest_hashes = config.get("manifest_sha256") or {}
    if isinstance(manifest_hashes, Mapping):
        manifest_content_sha256 = sorted({str(value) for value in manifest_hashes.values() if value})
    else:
        manifest_content_sha256 = []
    source_spec = config.get("source_generation_spec")
    raw_source_identity = source_spec.get("source_identity") if isinstance(source_spec, Mapping) else None
    source_identity = _semantic_source_identity(raw_source_identity) if raw_source_identity is not None else None
    return {
        "schema": AGENTIC_PARENT_RETRIEVAL_IDENTITY_SCHEMA,
        "midterm_retrieval": _identity_values(config, _MIDTERM_RETRIEVAL_FIELDS),
        "query_representation": {
            **_identity_values(config, _QUERY_REPRESENTATION_FIELDS),
            "derived_artifact_sha256": config.get("derived_artifact_sha256"),
        },
        "page_representation": {
            "page_representation": config.get("page_representation"),
            "derived_artifact_sha256": config.get("derived_artifact_sha256"),
        },
        "production_source": {
            **_identity_values(config, _SOURCE_FIELDS),
            "manifest_content_sha256": manifest_content_sha256,
            "source_identity": source_identity,
            "source_identity_sha256": stable_hash(source_identity) if source_identity is not None else None,
        },
        "fine_grained_longterm": _identity_values(config, _LONGTERM_FIELDS),
    }


def agentic_parent_retrieval_identity_sha256(identity: Mapping[str, Any]) -> str:
    return stable_hash(dict(identity))


@dataclass(frozen=True)
class ProductionAgenticTrace:
    paths: tuple[Path, ...]
    sha256: dict[str, str]
    parent_retrieval_identity: dict[str, Any]
    parent_retrieval_identity_sha256: str
    variants: tuple[tuple[int, int], ...]
    rows: dict[tuple[int, int], dict[str, dict[str, Any]]]

    def serializable(self) -> dict[str, Any]:
        return {
            "schema": PRODUCTION_AGENTIC_TRACE_SCHEMA,
            "execution_contract": PRODUCTION_AGENTIC_EXECUTION_CONTRACT,
            "paths": [str(path.resolve()) for path in self.paths],
            "sha256": dict(self.sha256),
            "parent_retrieval_identity": dict(self.parent_retrieval_identity),
            "parent_retrieval_identity_sha256": self.parent_retrieval_identity_sha256,
            "variants": [
                {"max_queries": max_queries, "max_total_results": max_total_results}
                for max_queries, max_total_results in self.variants
            ],
            "fixed": {
                "max_iterations": AGENTIC_FIXED_MAX_ITERATIONS,
                "max_tool_calls": AGENTIC_FIXED_MAX_TOOL_CALLS,
            },
        }


def _trace_paths(config: Mapping[str, Any]) -> tuple[Path, ...]:
    raw_paths = config.get("production_agentic_trace_paths")
    if raw_paths is None:
        raw_paths = config.get("production_agentic_trace")
    if isinstance(raw_paths, (str, Path)):
        raw_paths = [raw_paths]
    if not isinstance(raw_paths, Sequence):
        return ()
    return tuple(dict.fromkeys(Path(str(value)).expanduser().resolve() for value in raw_paths))


def _agentic_result(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("agentic_result")
    result = (
        dict(raw)
        if isinstance(raw, Mapping)
        else {
            key: row.get(key)
            for key in ("status", "supplement", "answer", "iterations", "tool_call_count", "tool_trace")
            if key in row
        }
    )
    status = str(result.get("status") or "")
    if status not in _VALID_STATUSES:
        raise ValueError(f"production Agentic trace has unsupported status={status!r}")
    supplement = result.get("supplement", result.get("answer", ""))
    if not isinstance(supplement, str):
        raise ValueError("production Agentic trace supplement must be text")
    tool_trace = result.get("tool_trace")
    if not isinstance(tool_trace, list):
        raise ValueError("production Agentic trace must contain the production tool_trace list")
    iterations = int(result.get("iterations") or 0)
    tool_call_count = int(result.get("tool_call_count") or 0)
    if iterations < 1 or iterations > AGENTIC_FIXED_MAX_ITERATIONS:
        raise ValueError("production Agentic trace has invalid iteration count")
    if tool_call_count < 0 or tool_call_count > AGENTIC_FIXED_MAX_TOOL_CALLS:
        raise ValueError("production Agentic trace has invalid tool-call count")
    if status == "supplemented":
        if not supplement.strip() or iterations != AGENTIC_FIXED_MAX_ITERATIONS or tool_call_count != 1:
            raise ValueError("supplemented Agentic trace does not match the production two-call contract")
        if len(tool_trace) != 1 or str((tool_trace[0] or {}).get("name") or "") != "search_memory":
            raise ValueError("supplemented Agentic trace is missing its production search_memory tool call")
    return {
        **result,
        "status": status,
        "supplement": supplement,
        "iterations": iterations,
        "tool_call_count": tool_call_count,
        "tool_trace": tool_trace,
    }


def load_production_agentic_trace(
    config: Mapping[str, Any],
    *,
    dataset_sha256: str,
    query_ids_by_session: Mapping[str, Sequence[str]],
    expected_parent_retrieval_identity: Mapping[str, Any],
) -> ProductionAgenticTrace:
    """Load only complete, exact-config traces produced by the real Agentic fallback.

    A trace contains a separate production execution for every evaluated
    ``(max_queries, max_total_results)`` variant, all bound to the expected
    retrieval/source/query/LongTerm parent identity. The tuner never derives
    an Agentic result from ordinary MidTerm rankings and never truncates one
    parameter variant to impersonate another.
    """

    expected_parent = dict(expected_parent_retrieval_identity)
    if expected_parent.get("schema") != AGENTIC_PARENT_RETRIEVAL_IDENTITY_SCHEMA:
        raise ValueError("expected Agentic parent retrieval identity has an unsupported schema")
    expected_source = expected_parent.get("production_source")
    if not isinstance(expected_source, Mapping) or not (
        expected_source.get("manifest_content_sha256") or expected_source.get("source_identity_sha256")
    ):
        raise ValueError("expected Agentic parent retrieval identity has no immutable Production source artifact")
    expected_parent_sha256 = agentic_parent_retrieval_identity_sha256(expected_parent)
    paths = _trace_paths(config)
    if not paths:
        raise ValueError("required production_agentic_trace is missing")
    expected_hashes = config.get("production_agentic_trace_sha256") or {}
    hashes: dict[str, str] = {}
    rows: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    sessions_by_variant: dict[tuple[int, int], dict[str, set[str]]] = {}
    for path in paths:
        if not path.exists():
            raise ValueError(f"production Agentic trace does not exist: {path}")
        digest = sha256_file(path)
        hashes[str(path)] = digest
        expected = expected_hashes.get(str(path)) or expected_hashes.get(str(path.resolve()))
        if expected is None and len(expected_hashes) == 1 and len(paths) == 1:
            expected = next(iter(expected_hashes.values()))
        if expected_hashes and expected != digest:
            raise ValueError(f"production Agentic trace hash mismatch: {path}")
        for raw_row in load_jsonl(path):
            row = dict(raw_row)
            if row.get("schema") != PRODUCTION_AGENTIC_TRACE_SCHEMA:
                raise ValueError(f"unsupported production Agentic trace schema in {path}")
            if row.get("dataset_sha256") != dataset_sha256:
                raise ValueError(f"production Agentic trace dataset mismatch in {path}")
            if row.get("production_agentic_execution") is not True:
                raise ValueError("Agentic trace row does not prove a production Agentic execution")
            if row.get("execution_contract") != PRODUCTION_AGENTIC_EXECUTION_CONTRACT:
                raise ValueError("Agentic trace row has the wrong execution contract")
            row_parent = row.get("parent_retrieval_identity")
            if not isinstance(row_parent, Mapping) or dict(row_parent) != expected_parent:
                raise ValueError("production Agentic trace parent retrieval identity mismatch")
            if row.get("parent_retrieval_identity_sha256") != expected_parent_sha256:
                raise ValueError("production Agentic trace parent retrieval identity hash mismatch")
            max_queries = int(row.get("max_queries") or 0)
            max_total_results = int(row.get("max_total_results") or 0)
            validate_candidate_config({"max_queries": max_queries, "max_total_results": max_total_results})
            if int(row.get("max_iterations") or 0) != AGENTIC_FIXED_MAX_ITERATIONS:
                raise ValueError("production Agentic trace must keep max_iterations=2")
            if int(row.get("max_tool_calls") or 0) != AGENTIC_FIXED_MAX_TOOL_CALLS:
                raise ValueError("production Agentic trace must keep max_tool_calls=1")
            session_id = str(row.get("session_id") or "")
            query_id = str(row.get("query_id") or row.get("turn_id") or "").upper()
            expected_queries = {str(value).upper() for value in query_ids_by_session.get(session_id, ())}
            if not session_id or query_id not in expected_queries:
                raise ValueError(f"production Agentic trace contains an unexpected Query: {session_id}/{query_id}")
            if row.get("error") not in (None, ""):
                raise ValueError(f"production Agentic trace contains a failed Query: {session_id}/{query_id}")
            variant = (max_queries, max_total_results)
            if query_id in rows.setdefault(variant, {}):
                raise ValueError(f"duplicate/conflicting production Agentic trace row for {variant}/{query_id}")
            row["agentic_result"] = _agentic_result(row)
            rows[variant][query_id] = row
            sessions_by_variant.setdefault(variant, {}).setdefault(session_id, set()).add(query_id)

    expected = {
        session_id: {str(value).upper() for value in query_ids}
        for session_id, query_ids in query_ids_by_session.items()
    }
    complete_variants = tuple(
        sorted(
            variant
            for variant, sessions in sessions_by_variant.items()
            if set(sessions) == set(expected)
            and all(sessions[session_id] == expected[session_id] for session_id in expected)
        )
    )
    if not complete_variants:
        raise ValueError("production Agentic trace has no complete exact-config variant for this dataset")
    return ProductionAgenticTrace(
        paths=paths,
        sha256=hashes,
        parent_retrieval_identity=expected_parent,
        parent_retrieval_identity_sha256=expected_parent_sha256,
        variants=complete_variants,
        rows={variant: rows[variant] for variant in complete_variants},
    )


def agentic_supplement_rows(
    trace: ProductionAgenticTrace,
    *,
    max_queries: int,
    max_total_results: int,
) -> dict[str, list[dict[str, Any]]]:
    variant = (int(max_queries), int(max_total_results))
    if variant not in trace.rows:
        raise ValueError(
            f"production Agentic trace has no exact variant max_queries={variant[0]}, max_total_results={variant[1]}"
        )
    output: dict[str, list[dict[str, Any]]] = {}
    for query_id, row in trace.rows[variant].items():
        result = dict(row["agentic_result"])
        supplement = str(result.get("supplement") or "").strip()
        output[query_id] = []
        if supplement:
            output[query_id].append(
                {
                    "page_id": f"agentic:{query_id}:{max_queries}:{max_total_results}",
                    "source_turn_id": "",
                    "rank": 1,
                    "score": 1.0,
                    "source": "production_agentic_supplement",
                    "memory": supplement,
                    "agentic_status": result["status"],
                    "agentic_iterations": result["iterations"],
                    "agentic_tool_call_count": result["tool_call_count"],
                    "production_agentic_trace": True,
                }
            )
    return output
