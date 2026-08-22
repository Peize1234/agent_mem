from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .io_utils import load_jsonl, sha256_file
from .parameter_schema import validate_candidate_config

PRODUCTION_AGENTIC_TRACE_SCHEMA = "production_agentic_trace_v1"
PRODUCTION_AGENTIC_EXECUTION_CONTRACT = "Memory.run_agentic_retrieval"
AGENTIC_FIXED_MAX_ITERATIONS = 2
AGENTIC_FIXED_MAX_TOOL_CALLS = 1
_VALID_STATUSES = frozenset({"supplemented", "not_needed", "no_relevant_memory", "degraded"})


@dataclass(frozen=True)
class ProductionAgenticTrace:
    paths: tuple[Path, ...]
    sha256: dict[str, str]
    variants: tuple[tuple[int, int], ...]
    rows: dict[tuple[int, int], dict[str, dict[str, Any]]]

    def serializable(self) -> dict[str, Any]:
        return {
            "schema": PRODUCTION_AGENTIC_TRACE_SCHEMA,
            "execution_contract": PRODUCTION_AGENTIC_EXECUTION_CONTRACT,
            "paths": [str(path.resolve()) for path in self.paths],
            "sha256": dict(self.sha256),
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
    return tuple(Path(str(value)).expanduser().resolve() for value in raw_paths)


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
) -> ProductionAgenticTrace:
    """Load only complete, exact-config traces produced by the real Agentic fallback.

    A trace contains a separate production execution for every evaluated
    ``(max_queries, max_total_results)`` variant. The tuner never derives an
    Agentic result from ordinary MidTerm rankings and never truncates one
    parameter variant to impersonate another.
    """

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
                raise ValueError(f"duplicate production Agentic trace row for {variant}/{query_id}")
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
