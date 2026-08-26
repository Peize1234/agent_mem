from __future__ import annotations

import ctypes
import gc
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .io_utils import atomic_write_json, load_json, sha256_file, stable_hash
from .models import Candidate
from .parameter_schema import production_overrides_from_candidate

LOW_CONSUMPTION_ENV = "MEMORY_RETRIEVAL_TUNER_LOW_CONSUMPTION"
CUDA_MIN_FREE_GIB = 0.5
CUDA_MIN_FREE_FRACTION = 0.12

# These branches can replay their real query-time or local-model logic while
# freezing only the text that the baseline LLM produced. Prompt/source-layout
# branches are disabled by run_tuner because their defining output cannot be
# changed without another generative LLM call.
LOCAL_VECTOR_REPLAY_BRANCHES = frozenset({"Embedding", "FieldAwareMultiVector", "PageRepresentation"})


def low_consumption_enabled() -> bool:
    value = str(os.environ.get(LOW_CONSUMPTION_ENV, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def set_low_consumption_mode(enabled: bool) -> None:
    if enabled:
        os.environ[LOW_CONSUMPTION_ENV] = "1"
    else:
        os.environ.pop(LOW_CONSUMPTION_ENV, None)


def release_local_model_memory() -> None:
    """Release cyclic model objects and return free CPU/GPU pages promptly.

    Deep low-consumption search evaluates several large local models in one
    process.  PyTorch/transformers objects may participate in reference cycles,
    while glibc can retain their freed CPU arenas.  Without an explicit cleanup
    boundary, sequential smoke tests and Candidate replays can accumulate close
    to the sum of every model's resident memory and trigger the OOM killer.

    Cleanup is deliberately best-effort: model selection must not fail merely
    because an allocator-specific trimming hook is unavailable.
    """

    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except (ImportError, RuntimeError):
        pass
    try:
        malloc_trim = getattr(ctypes.CDLL(None), "malloc_trim")
        malloc_trim.argtypes = [ctypes.c_size_t]
        malloc_trim.restype = ctypes.c_int
        malloc_trim(0)
    except (AttributeError, OSError):
        pass


def cuda_memory_snapshot(device: str = "cuda") -> dict[str, float]:
    """Return allocator and device headroom for a CUDA execution boundary."""

    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA_REQUIRED_UNAVAILABLE: PyTorch cannot initialize CUDA")
    torch.cuda.synchronize(device)
    free_bytes, total_bytes = torch.cuda.mem_get_info(device)
    gib = float(1024**3)
    return {
        "free_gib": free_bytes / gib,
        "total_gib": total_bytes / gib,
        "allocated_gib": torch.cuda.memory_allocated(device) / gib,
        "reserved_gib": torch.cuda.memory_reserved(device) / gib,
    }


def required_cuda_free_gib(total_gib: float) -> float:
    """Keep both an absolute and proportional safety margin on every GPU."""

    return max(CUDA_MIN_FREE_GIB, float(total_gib) * CUDA_MIN_FREE_FRACTION)


def assert_cuda_headroom(
    device: str = "cuda",
    *,
    context: str,
    reclaim_inactive_cache: bool = False,
) -> dict[str, float]:
    """Fail a local-model operation that leaves unsafe CUDA headroom.

    Runtime checks may first return PyTorch's unused cached blocks to CUDA. A
    model is rejected only when its live tensors and the current workload still
    violate the same device-relative envelope. Smoke tests deliberately disable
    reclamation so a model that relies on filling the card with transient cache
    is excluded before a real Session starts.
    """

    snapshot = cuda_memory_snapshot(device)
    required_free = required_cuda_free_gib(snapshot["total_gib"])
    reclaimed = False
    if snapshot["free_gib"] < required_free and reclaim_inactive_cache:
        import torch

        torch.cuda.empty_cache()
        snapshot = cuda_memory_snapshot(device)
        required_free = required_cuda_free_gib(snapshot["total_gib"])
        reclaimed = True
    if snapshot["free_gib"] < required_free:
        raise RuntimeError(
            f"CUDA_HEADROOM_INSUFFICIENT: {context} left {snapshot['free_gib']:.2f} GiB free on {device}; "
            f"at least {required_free:.2f} GiB is required"
        )
    return {
        **snapshot,
        "required_free_gib": required_free,
        "inactive_cache_reclaimed": float(reclaimed),
    }


def assert_cuda_runtime_headroom(device: str = "cuda", *, context: str) -> dict[str, float]:
    """Enforce CUDA safety after each real local-model execution boundary."""

    return assert_cuda_headroom(device, context=context, reclaim_inactive_cache=True)


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
    """Replay a Candidate on baseline LLM text without more generative calls.

    Baseline Page/Session/LongTerm text and source topology stay frozen. Local
    embeddings, Page representations, field vectors, indexes, rerankers, and
    retrieval/evolution math are still allowed and are recomputed by the replay
    adapter when the Candidate changes them.
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
    branch = str(config.get("experiment_branch") or "")
    replay_modes: set[str] = set()
    first_manifest = load_json(baseline_paths[0])
    baseline_effective = _load_effective_memory_config(first_manifest, baseline_paths[0])
    baseline_midterm = dict(baseline_effective.get("midterm") or {})
    candidate_effective = _deep_merge(baseline_effective, overrides)
    candidate_midterm = dict(candidate_effective.get("midterm") or {})
    if dict(candidate_effective.get("embedder") or {}) != dict(baseline_effective.get("embedder") or {}):
        replay_modes.add("query_page_session_embeddings")
    if candidate_midterm.get("page_representation") != baseline_midterm.get("page_representation"):
        replay_modes.add("page_representation_embedding")
    if str((candidate_midterm.get("reranker") or {}).get("method") or "none") == "multi_vector_maxsim":
        replay_modes.add("page_field_embeddings")
    if branch in LOCAL_VECTOR_REPLAY_BRANCHES and not replay_modes:
        # FieldAware may inherit an already materialized multi-vector config.
        # Keep an explicit policy marker so a supposedly local-model branch can
        # never silently degrade into a no-op.
        replay_modes.add("candidate_local_vector_replay")

    approximation_identity = {
        "schema": 2,
        "kind": "low_consumption_baseline_source_reuse",
        "candidate": candidate.name,
        "branch": branch,
        "source_identity": source_identity,
        "baseline_manifest_sha256": [sha256_file(path) for path in baseline_paths],
        "production_overrides": overrides,
        "replay_modes": sorted(replay_modes),
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
                    "frozen_artifacts": ["llm_generated_text", "source_topology"],
                    "local_replay_modes": sorted(replay_modes),
                    "baseline_manifest_path": str(baseline_path.resolve()),
                    "baseline_manifest_sha256": sha256_file(baseline_path),
                    "requested_for_current_evaluation": session_id in requested,
                    "note": (
                        "baseline LLM text/source topology reused; Candidate local embeddings, indexes, rerankers, "
                        "and retrieval/evolution logic remain executable"
                    ),
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
    config["low_consumption_frozen_artifacts"] = ["llm_generated_text", "source_topology"]
    config["low_consumption_local_replay_modes"] = sorted(replay_modes)

    provenance = {
        **candidate.provenance,
        "low_consumption_mode": True,
        "low_consumption_approximate": True,
        "low_consumption_baseline_manifest_paths": [str(path.resolve()) for path in baseline_paths],
        "low_consumption_reused_sessions": sorted(reused_sessions),
        "low_consumption_ignored_generation_identity": source_identity,
        "low_consumption_frozen_artifacts": ["llm_generated_text", "source_topology"],
        "low_consumption_local_replay_modes": sorted(replay_modes),
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
