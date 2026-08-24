from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .io_utils import atomic_write_json, load_json, sha256_file, stable_hash
from .models import Candidate
from .parameter_schema import production_overrides_from_candidate

LOW_CONSUMPTION_ENV = "MEMORY_RETRIEVAL_TUNER_LOW_CONSUMPTION"


def low_consumption_enabled() -> bool:
    value = str(os.environ.get(LOW_CONSUMPTION_ENV, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_low_consumption_mode(enabled: bool) -> None:
    if enabled:
        os.environ[LOW_CONSUMPTION_ENV] = "1"
    else:
        os.environ.pop(LOW_CONSUMPTION_ENV, None)


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(dict(result[key]), value)
        else:
            result[key] = deepcopy(value)
    return result


def _baseline_manifest_paths(candidate: Candidate) -> list[Path]:
    recorded = candidate.provenance.get("low_consumption_baseline_manifest_paths")
    raw_paths = recorded if isinstance(recorded, list) and recorded else candidate.config.get("manifest_paths") or []
    return [Path(str(value)) for value in raw_paths]


def _load_effective_memory_config(manifest: Mapping[str, Any], manifest_path: Path) -> dict[str, Any]:
    effective = manifest.get("effective_memory_config")
    if isinstance(effective, Mapping) and effective:
        return deepcopy(dict(effective))
    raw_path = manifest.get("memory_config_path")
    if not raw_path:
        return {}
    memory_config_path = Path(str(raw_path))
    if not memory_config_path.is_absolute():
        memory_config_path = (manifest_path.parent / memory_config_path).resolve()
    loaded = load_json(memory_config_path)
    return deepcopy(dict(loaded)) if isinstance(loaded, Mapping) else {}


def reuse_baseline_source_candidate(
    candidate: Candidate,
    *,
    sessions: Sequence[str],
    run_dir: Path,
) -> Candidate:
    """Approximate a source-changing Candidate on the frozen baseline source.

    Low-consumption mode deliberately keeps the baseline checkpoints/vectors and
    changes only the effective Candidate configuration used by replay.  No Add,
    source prompt, Query LLM, or embedding regeneration is performed here.
    """

    baseline_paths = _baseline_manifest_paths(candidate)
    if not baseline_paths:
        raise ValueError("low-consumption source reuse requires baseline production manifests")
    if any(not path.is_file() for path in baseline_paths):
        missing = [str(path) for path in baseline_paths if not path.is_file()]
        raise ValueError(f"low-consumption baseline manifest is missing: {missing}")

    config = deepcopy(candidate.config)
    overrides = production_overrides_from_candidate(config)
    source_spec = config.get("source_generation_spec")
    source_identity = (
        deepcopy(dict(source_spec.get("source_identity") or {})) if isinstance(source_spec, Mapping) else {}
    )
    approximation_identity = {
        "schema": 1,
        "kind": "low_consumption_baseline_source_reuse",
        "candidate": candidate.name,
        "source_identity": source_identity,
        "baseline_manifest_sha256": [sha256_file(path) for path in baseline_paths],
        "production_overrides": overrides,
    }
    root = run_dir / "low_consumption_sources" / stable_hash(approximation_identity)
    root.mkdir(parents=True, exist_ok=True)

    requested = {str(value) for value in sessions}
    synthetic_paths: list[Path] = []
    reused_sessions: list[str] = []
    for baseline_path in baseline_paths:
        manifest = load_json(baseline_path)
        session_id = str(manifest.get("session_id") or "")
        if not session_id:
            raise ValueError(f"baseline manifest has no session_id: {baseline_path}")
        effective = _deep_merge(_load_effective_memory_config(manifest, baseline_path), overrides)
        synthetic = deepcopy(dict(manifest))
        synthetic.update(
            {
                "effective_memory_config": effective,
                "production_config": deepcopy(effective.get("midterm") or manifest.get("production_config") or {}),
                "config_overrides": deepcopy(overrides),
                "source_variant": str(config.get("source_variant") or manifest.get("source_variant") or "production"),
                "source_identity": source_identity,
                "stateful_replay": bool(
                    (source_spec or {}).get("stateful_replay") if isinstance(source_spec, Mapping) else False
                ),
                "low_consumption_reuse": {
                    "enabled": True,
                    "approximate": True,
                    "baseline_manifest_path": str(baseline_path.resolve()),
                    "baseline_manifest_sha256": sha256_file(baseline_path),
                    "requested_for_current_evaluation": session_id in requested,
                    "note": "baseline checkpoints/vectors reused; candidate config replayed without source regeneration",
                },
            }
        )
        output_dir = root / session_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "production_midterm_manifest.json"
        atomic_write_json(output_path, synthetic)
        synthetic_paths.append(output_path)
        reused_sessions.append(session_id)

    config["manifest_paths"] = [str(path.resolve()) for path in synthetic_paths]
    config["manifest_sha256"] = {str(path.resolve()): sha256_file(path) for path in synthetic_paths}
    config["low_consumption_mode"] = True
    config["low_consumption_source_reuse"] = True
    config["low_consumption_approximation_identity"] = stable_hash(approximation_identity)

    provenance = {
        **candidate.provenance,
        "low_consumption_mode": True,
        "low_consumption_approximate": True,
        "low_consumption_baseline_manifest_paths": [str(path.resolve()) for path in baseline_paths],
        "low_consumption_reused_sessions": sorted(reused_sessions),
        "low_consumption_ignored_generation_identity": source_identity,
        "deferred_generation_stats": {
            "low_consumption": True,
            "generated_sessions": 0,
            "reused_sessions": len(synthetic_paths),
            "requested_sessions": sorted(requested),
            "llm_calls": 0,
            "embedding_calls": 0,
        },
        "manifests": config["manifest_sha256"],
    }
    return Candidate(candidate.name, candidate.stage, config, provenance, candidate.complexity)
