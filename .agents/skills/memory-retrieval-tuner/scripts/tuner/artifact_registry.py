from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .io_utils import atomic_write_json, load_json, load_jsonl, sha256_file, stable_hash
from .models import Candidate


def _contains_value(value: Any, expected: str) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_value(item, expected) for item in value.values())
    if isinstance(value, list):
        return any(_contains_value(item, expected) for item in value)
    return str(value) == expected


def _string_values(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def _query_artifact_variants(rows: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Extract common repository query-rewrite layouts without guessing a winner."""
    variants: dict[str, dict[str, str]] = {}
    direct_fields = (
        "conservative_reference_resolution",
        "old_standalone_rewrite",
        "rewritten_query",
        "standalone_query",
    )
    for row in rows:
        query_id = str(row.get("query_id") or row.get("turn_id") or "").upper()
        if not query_id:
            continue
        resolved = row.get("resolved_query")
        if isinstance(resolved, str) and resolved.strip():
            discriminator = next(
                (key for key in ("config", "prompt", "variant") if row.get(key) not in (None, "")),
                None,
            )
            label = f"row:{discriminator}:{row[discriminator]}" if discriminator else "field:resolved_query"
            variants.setdefault(label, {})[query_id] = resolved.strip()
        for field in direct_fields:
            value = row.get(field)
            if isinstance(value, str) and value.strip():
                variants.setdefault(f"field:{field}", {})[query_id] = value.strip()
        for key, value in row.items():
            if isinstance(value, Mapping):
                nested = value.get("resolved_query")
                if isinstance(nested, str) and nested.strip():
                    variants.setdefault(f"nested:{key}", {})[query_id] = nested.strip()
    return variants


def _production_config_fingerprint(config: Mapping[str, Any]) -> str:
    llm = dict(config.get("llm") or {})
    llm_config = {
        key: value for key, value in dict(llm.get("config") or {}).items() if key not in {"api_key", "base_url"}
    }
    return stable_hash(
        {
            "llm": {"provider": llm.get("provider"), "config": llm_config},
            "embedder": config.get("embedder"),
            "midterm": config.get("midterm"),
            "benchmark_runtime": config.get("benchmark_runtime"),
        }
    )


def load_frozen_query_overrides(config: Mapping[str, Any]) -> dict[str, str]:
    path = Path(str(config["query_artifact_path"]))
    if path.suffix.lower() == ".json":
        value = load_json(path)
        payload = value.get("payload") if value.get("status") == "COMPLETE" else value
        rows = list((payload or {}).get("rows") or []) if isinstance(payload, Mapping) else []
    else:
        rows = load_jsonl(path)
    variants = _query_artifact_variants(rows)
    variant = str(config["query_artifact_variant"])
    if variant not in variants:
        raise ValueError(f"Query artifact {path} has no variant {variant!r}")
    return variants[variant]


class ArtifactRegistry:
    """Content-addressed, atomic cache plus validated frozen-artifact discovery."""

    def __init__(self, cache_root: Path, results_root: Path):
        self.cache_root = cache_root
        self.results_root = results_root
        self.cache_root.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def lock(self, key: str, *, timeout_seconds: float = 120.0) -> Iterator[None]:
        lock_path = self.cache_root / "locks" / f"{key}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(descriptor, f"pid={os.getpid()}\n".encode())
            except FileExistsError:
                if time.monotonic() - started >= timeout_seconds:
                    # A stale lock from an interrupted worker is safe to remove after the timeout.
                    lock_path.unlink(missing_ok=True)
                    continue
                time.sleep(0.05)
        try:
            yield
        finally:
            if descriptor is not None:
                os.close(descriptor)
            lock_path.unlink(missing_ok=True)

    def ranking_cache_path(self, cache_key: str) -> Path:
        return self.cache_root / "rankings" / cache_key[:2] / f"{cache_key}.json"

    def get_ranking(self, identity: Mapping[str, Any], required_depth: int) -> dict[str, Any] | None:
        cache_key = stable_hash(identity)
        path = self.ranking_cache_path(cache_key)
        if not path.exists():
            return None
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        if value.get("status") != "COMPLETE" or value.get("identity") != dict(identity):
            return None
        if int(value.get("stored_depth") or 0) < required_depth:
            return None
        return value

    def store_ranking(self, identity: Mapping[str, Any], rows: list[dict[str, Any]], depth: int) -> Path:
        cache_key = stable_hash(identity)
        path = self.ranking_cache_path(cache_key)
        with self.lock(cache_key):
            current = self.get_ranking(identity, depth)
            if current is None:
                atomic_write_json(
                    path,
                    {
                        "status": "COMPLETE",
                        "identity": dict(identity),
                        "stored_depth": depth,
                        "row_count": len(rows),
                        "rows": rows,
                    },
                )
        return path

    def worker_path(self, run_dir: Path, candidate_hash: str, scope: str, session_id: str) -> Path:
        safe_session = session_id.replace("/", "_").replace(" ", "_")
        return run_dir / "workers" / candidate_hash / scope / f"{safe_session}.json"

    @staticmethod
    def valid_worker(path: Path, *, expected_queries: int, identity: Mapping[str, Any]) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError):
            return None
        if value.get("status") != "COMPLETE" or value.get("identity") != dict(identity):
            return None
        if int(value.get("evaluated_query_count") or 0) != expected_queries:
            return None
        if int(value.get("failed_turns") or 0) != 0:
            return None
        return value

    def discover_frozen_rankings(self, dataset_sha256: str, *, limit: int = 24) -> list[Candidate]:
        candidates: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        for metadata_path in sorted(self.results_root.glob("**/run_metadata.json")):
            try:
                metadata = load_json(metadata_path)
            except (OSError, json.JSONDecodeError):
                continue
            if not _contains_value(metadata, dataset_sha256):
                continue
            if not _contains_value(metadata, "production_midterm_v1"):
                # Legacy flat-Page rankings do not prove that production
                # Session routing and Page retrieval were both applied.
                continue
            local_rankings = {
                path.resolve()
                for path in metadata_path.parent.glob("**/*.jsonl")
                if any("rank" in part.lower() for part in path.relative_to(metadata_path.parent).parts)
            }
            referenced_rankings = {
                Path(value).expanduser().resolve()
                for value in _string_values(metadata)
                if value.lower().endswith(".jsonl") and "rank" in value.lower() and Path(value).expanduser().exists()
            }
            for ranking_path in sorted(local_rankings | referenced_rankings):
                try:
                    rows = load_jsonl(ranking_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if not rows or not all("query_id" in row for row in rows[: min(20, len(rows))]):
                    continue
                variant_key = next(
                    (key for key in ("variant", "checkpoint", "method", "Candidate") if key in rows[0]), None
                )
                variants = (
                    sorted({str(row.get(variant_key) or "default") for row in rows}) if variant_key else ["default"]
                )
                for variant in variants[:4]:
                    identity = (str(ranking_path.resolve()), variant)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    label = f"frozen:{ranking_path.parent.name}:{variant}"
                    metadata_sha256 = sha256_file(metadata_path)
                    ranking_sha256 = sha256_file(ranking_path)
                    candidates.append(
                        Candidate(
                            name=label,
                            stage="frozen_replay",
                            config={
                                "backend": "frozen_ranking",
                                "ranking_path": str(ranking_path.resolve()),
                                "variant_key": variant_key,
                                "variant": variant,
                                "metadata_sha256": metadata_sha256,
                                "ranking_sha256": ranking_sha256,
                            },
                            provenance={
                                "dataset_sha256": dataset_sha256,
                                "metadata_path": str(metadata_path.resolve()),
                                "metadata_sha256": metadata_sha256,
                                "ranking_sha256": ranking_sha256,
                                "provenance_validated": True,
                            },
                            complexity=2,
                        )
                    )
                    if len(candidates) >= limit:
                        return candidates
        return candidates

    def discover_frozen_queries(
        self,
        dataset_sha256: str,
        *,
        query_text_by_id: Mapping[str, str],
        base_candidate: Candidate,
        limit: int = 4,
    ) -> list[Candidate]:
        """Find exact-dataset query artifacts and validate their original-query identity."""
        candidates: list[Candidate] = []
        seen: set[tuple[str, str]] = set()
        seen_override_hashes: set[str] = set()
        for metadata_path in sorted(self.results_root.glob("**/run_metadata.json")):
            try:
                metadata = load_json(metadata_path)
            except (OSError, json.JSONDecodeError):
                continue
            if not _contains_value(metadata, dataset_sha256):
                continue
            for artifact_path in sorted(metadata_path.parent.glob("**/*.jsonl")):
                lowered = artifact_path.name.lower()
                if not any(marker in lowered for marker in ("query", "resolv", "rewrite")):
                    continue
                try:
                    rows = load_jsonl(artifact_path)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                variants = _query_artifact_variants(rows)
                if not variants:
                    continue
                mismatched = {
                    str(row.get("query_id") or row.get("turn_id") or "").upper()
                    for row in rows
                    if str(row.get("query_id") or row.get("turn_id") or "").upper() in query_text_by_id
                    and row.get("original_query") not in (None, "")
                    and str(row["original_query"]).strip()
                    != query_text_by_id[str(row.get("query_id") or row.get("turn_id") or "").upper()].strip()
                }
                if mismatched:
                    continue
                metadata_sha256 = sha256_file(metadata_path)
                query_sha256 = sha256_file(artifact_path)
                for variant, overrides in sorted(variants.items()):
                    identity = (str(artifact_path.resolve()), variant)
                    matched = set(overrides) & set(query_text_by_id)
                    if identity in seen or not matched:
                        continue
                    seen.add(identity)
                    matched_overrides = {query_id: overrides[query_id] for query_id in sorted(matched)}
                    if all(
                        matched_overrides[query_id].strip() == query_text_by_id[query_id].strip()
                        for query_id in matched
                    ):
                        continue
                    override_hash = stable_hash(matched_overrides)
                    if override_hash in seen_override_hashes:
                        continue
                    seen_override_hashes.add(override_hash)
                    safe_variant = variant.replace(":", "-")
                    candidates.append(
                        Candidate(
                            name=f"query:frozen:{artifact_path.stem}:{safe_variant}",
                            stage="cheap_query_representation",
                            config={
                                **base_candidate.config,
                                "query_representation": "bounded_reference_resolution",
                                "query_artifact_path": str(artifact_path.resolve()),
                                "query_artifact_variant": variant,
                                "metadata_sha256": metadata_sha256,
                                "query_artifact_sha256": query_sha256,
                            },
                            provenance={
                                "dataset_sha256": dataset_sha256,
                                "metadata_path": str(metadata_path.resolve()),
                                "metadata_sha256": metadata_sha256,
                                "query_artifact_sha256": query_sha256,
                                "matched_query_count": len(matched),
                                "provenance_validated": True,
                            },
                            complexity=1,
                        )
                    )
                    if len(candidates) >= limit:
                        return candidates
        return candidates

    def discover_production_trace(
        self,
        *,
        dataset_path: Path,
        dataset_sha256: str,
        session_query_counts: Mapping[str, int],
        memory_config_path: Path | None = None,
    ) -> Candidate | None:
        """Find a complete production recall trace with exact dataset provenance."""
        trace_paths: set[Path] = set()
        for summary_path in self.results_root.glob("**/isolated_run_summary.json"):
            try:
                summary = load_json(summary_path)
                source_dataset = Path(str(summary.get("dataset") or "")).expanduser()
                if not source_dataset.exists() or sha256_file(source_dataset) != dataset_sha256:
                    continue
                for session in summary.get("sessions") or []:
                    result_dir = Path(str(session.get("result_dir") or ""))
                    trace = result_dir / "recall_turn_results.jsonl"
                    if trace.exists() and int(session.get("failed_turns") or 0) == 0:
                        trace_paths.add(trace.resolve())
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        for metadata_path in self.results_root.glob("**/run_metadata.json"):
            try:
                metadata = load_json(metadata_path)
            except (OSError, json.JSONDecodeError):
                continue
            if not _contains_value(metadata, dataset_sha256):
                continue
            for value in _string_values(metadata):
                path = Path(value).expanduser()
                if path.name == "recall_turn_results.jsonl" and path.exists():
                    trace_paths.add(path.resolve())

        rows_by_session: dict[str, list[dict[str, Any]]] = {}
        selected_path_by_session: dict[str, Path] = {}
        config_fingerprints: dict[str, str] = {}
        expected_config_fingerprint = (
            _production_config_fingerprint(load_json(memory_config_path)) if memory_config_path is not None else None
        )
        for trace_path in sorted(trace_paths):
            try:
                rows = load_jsonl(trace_path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if expected_config_fingerprint is not None:
                effective_path = trace_path.parent / "effective_memory_config.json"
                if not effective_path.exists():
                    continue
                effective_fingerprint = _production_config_fingerprint(load_json(effective_path))
                if effective_fingerprint != expected_config_fingerprint:
                    continue
                config_fingerprints[str(trace_path)] = effective_fingerprint
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                session_id = str(row.get("session_id") or row.get("sheet_name") or "")
                if session_id in session_query_counts:
                    grouped.setdefault(session_id, []).append(row)
            for session_id, expected_count in session_query_counts.items():
                session_rows = grouped.get(session_id) or []
                query_ids = [str(row.get("turn_id") or row.get("query_id") or "") for row in session_rows]
                complete = (
                    len(session_rows) == expected_count
                    and len(set(query_ids)) == expected_count
                    and not any(row.get("error") for row in session_rows)
                )
                if complete and session_id not in rows_by_session:
                    rows_by_session[session_id] = session_rows
                    selected_path_by_session[session_id] = trace_path
        if set(rows_by_session) != set(session_query_counts):
            return None
        selected_paths = sorted(set(selected_path_by_session.values()))
        trace_sha256 = {str(path): sha256_file(path) for path in selected_paths}
        production_midterm_config: dict[str, Any] = {}
        first_effective = selected_paths[0].parent / "effective_memory_config.json"
        if first_effective.exists():
            production_midterm_config = dict(load_json(first_effective).get("midterm") or {})
        return Candidate(
            name="baseline",
            stage="baseline",
            config={
                **production_midterm_config,
                "backend": "production_trace",
                "retrieval_contract": "production_midterm_v1",
                "retrieval_method": "dense",
                "query_representation": "original",
                "page_representation": "production",
                "trace_paths": [str(path) for path in selected_paths],
                "trace_sha256": trace_sha256,
                "dataset_sha256": dataset_sha256,
            },
            provenance={
                "dataset": str(dataset_path.resolve()),
                "dataset_sha256": dataset_sha256,
                "trace_sha256": trace_sha256,
                "session_trace_paths": {
                    session_id: str(path) for session_id, path in sorted(selected_path_by_session.items())
                },
                "provenance_validated": True,
                "retrieval_contract": "production_midterm_v1",
                "failed_turns": 0,
                "config_fingerprints": {
                    str(path): config_fingerprints[str(path)]
                    for path in selected_paths
                    if str(path) in config_fingerprints
                },
                "prompt_provenance": "legacy source git commit; explicit prompt hashes unavailable",
            },
            complexity=0,
        )

    def discover_production_midterm(
        self,
        *,
        dataset_sha256: str,
        session_turn_counts: Mapping[str, int],
        ranking_depth: int,
        memory_config_path: Path,
        llm_mode: str,
        source_run: Path | None = None,
    ) -> Candidate | None:
        """Find complete replayable checkpoints created from the production MidTerm pipeline."""
        from .production_midterm_adapter import production_candidate_from_manifests

        roots = [self.results_root]
        if source_run is not None:
            roots.insert(0, source_run if source_run.is_dir() else source_run.parent)
        candidates: dict[str, Path] = {}
        expected_memory_config_sha256 = sha256_file(memory_config_path)
        expected_prompt_hashes: dict[str, str] | None = None
        for root in roots:
            for manifest_path in root.glob("**/production_midterm_manifest.json"):
                try:
                    manifest = load_json(manifest_path)
                except (OSError, json.JSONDecodeError):
                    continue
                session_id = str(manifest.get("session_id") or "")
                if (
                    manifest.get("status") != "COMPLETE"
                    or manifest.get("dataset_sha256") != dataset_sha256
                    or session_id not in session_turn_counts
                    or int(manifest.get("turn_count") or 0) != int(session_turn_counts[session_id])
                    or int(manifest.get("failed_turns") or 0) != 0
                    or int(manifest.get("ranking_depth") or 0) < ranking_depth
                    or manifest.get("memory_config_sha256") != expected_memory_config_sha256
                    or str(manifest.get("llm_mode") or "real") != llm_mode
                ):
                    continue
                checkpoints = Path(str(manifest.get("checkpoints_path") or ""))
                if not checkpoints.exists() or manifest.get("checkpoints_sha256") != sha256_file(checkpoints):
                    continue
                if expected_prompt_hashes is None:
                    from .production_midterm_adapter import production_prompt_hashes

                    expected_prompt_hashes = production_prompt_hashes()
                if manifest.get("prompt_hashes") != expected_prompt_hashes:
                    continue
                candidates.setdefault(session_id, manifest_path.resolve())
        if set(candidates) != set(session_turn_counts):
            return None
        paths = [candidates[session_id] for session_id in session_turn_counts]
        config, provenance = production_candidate_from_manifests(paths)
        return Candidate(
            name="baseline",
            stage="baseline",
            config=config,
            provenance=provenance,
            complexity=0,
        )

    def materialize_once(
        self, identity: Mapping[str, Any], producer: Callable[[], dict[str, Any]]
    ) -> tuple[dict[str, Any], bool]:
        """Generic resume primitive used by tests and future artifact branches."""
        key = stable_hash(identity)
        path = self.artifact_path(identity)
        if path.exists():
            value = load_json(path)
            if value.get("status") == "COMPLETE" and value.get("identity") == dict(identity):
                return value, True
        with self.lock(key):
            if path.exists():
                value = load_json(path)
                if value.get("status") == "COMPLETE" and value.get("identity") == dict(identity):
                    return value, True
            payload = producer()
            value = {"status": "COMPLETE", "identity": dict(identity), "payload": payload}
            atomic_write_json(path, value)
            return value, False

    def artifact_path(self, identity: Mapping[str, Any]) -> Path:
        key = stable_hash(identity)
        return self.cache_root / "artifacts" / key[:2] / f"{key}.json"
