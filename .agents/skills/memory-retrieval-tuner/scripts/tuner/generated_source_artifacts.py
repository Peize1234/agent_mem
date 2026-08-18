from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_registry import ArtifactRegistry
from .benchmark_support import load_json
from .derived_artifacts import DerivedArtifactBuilder
from .io_utils import sha256_file, stable_hash
from .models import Candidate
from .production_midterm_adapter import generate_production_sources


def prepare_generated_source_candidate(
    candidate: Candidate,
    *,
    sessions: Sequence[str],
    registry: ArtifactRegistry,
    run_dir: Path,
    ranking_depth: int,
    max_parallel_sessions: int,
    max_parallel_llm_calls: int,
    gpu_count: int,
) -> Candidate:
    """Materialize only the prompt-variant Sessions required by the next evaluation.

    This is the execution half of ``MemoryWriteAddPromptBranch``. Candidate
    generation is cheap; Tune-subset screening triggers the first isolated
    source workers, and only promoted Candidates are completed for full Tune
    and later held-out Validation.
    """

    spec = candidate.config.get("source_generation_spec")
    if not isinstance(spec, Mapping):
        return candidate
    requested = sorted(set(str(value) for value in sessions))
    if not requested:
        return candidate
    identity = dict(spec["source_identity"])
    stats: dict[str, Any] = {}
    with registry.lock(f"source-{stable_hash(identity)}", timeout_seconds=7200.0):
        generated = generate_production_sources(
            dataset_path=Path(str(spec["dataset_path"])),
            dataset_sha256=str(spec["dataset_sha256"]),
            session_ids=requested,
            session_turn_counts={key: int(value) for key, value in dict(spec["session_turn_counts"]).items()},
            memory_config_path=Path(str(spec["memory_config_path"])),
            run_dir=run_dir,
            ranking_depth=ranking_depth,
            llm_mode=str(spec["llm_mode"]),
            max_parallel_sessions=max_parallel_sessions,
            max_parallel_llm_calls=max_parallel_llm_calls,
            page_summary_prompt=str(spec["page_summary_prompt"]),
            context_mode=str(spec["context_mode"]),
            source_variant=str(spec["source_variant"]),
            source_identity=identity,
            source_root=Path(str(spec["source_root"])),
            generation_stats=stats,
        )

    manifests_by_session: dict[str, Path] = {}
    for raw_path in candidate.config.get("manifest_paths") or []:
        path = Path(str(raw_path))
        session_id = str(load_json(path).get("session_id") or "")
        if session_id:
            manifests_by_session[session_id] = path
    for path in generated:
        manifests_by_session[str(load_json(path)["session_id"])] = path
    session_order = [str(value) for value in spec["session_order"]]
    manifest_paths = [manifests_by_session[session_id] for session_id in session_order]
    config = {
        **candidate.config,
        "manifest_paths": [str(path.resolve()) for path in manifest_paths],
        "manifest_sha256": {str(path.resolve()): sha256_file(path) for path in manifest_paths},
    }
    embedding_calls = int(stats.get("embedding_calls") or 0)
    reused_artifacts = [sha256_file(path) for path in generated] if stats.get("reused_sessions") else []
    needs_derived = any(
        (
            config.get("query_representation") not in (None, "original"),
            config.get("page_representation") not in (None, "production"),
            config.get("embedding_model_id") not in (None, "production"),
            config.get("reranker_method") == "multi_vector_maxsim",
        )
    )
    if needs_derived:
        temporary = Candidate(candidate.name, candidate.stage, config, candidate.provenance, candidate.complexity)
        derived = DerivedArtifactBuilder(registry).build(
            dataset_sha256=str(spec["dataset_sha256"]),
            session_scope=requested,
            baseline=temporary,
            query_representation=str(config.get("query_representation") or "original"),
            query_artifact_path=(
                Path(str(config["query_artifact_path"])) if config.get("query_artifact_path") else None
            ),
            query_artifact_variant=config.get("query_artifact_variant"),
            page_representation_name=str(config.get("page_representation") or "production"),
            embedding_model_id=str(config.get("embedding_model_id") or "production"),
            embedding_revision=config.get("embedding_model_revision"),
            embedding_local_path=config.get("embedding_model_path"),
            encoding_contract=config.get("encoding_contract"),
            include_field_vectors=config.get("reranker_method") == "multi_vector_maxsim",
            device="cuda" if gpu_count else "cpu",
        )
        config["derived_artifact_path"] = str(derived.path)
        config["derived_artifact_sha256"] = derived.sha256
        embedding_calls += derived.embedding_calls
        if derived.reused:
            reused_artifacts.append(derived.sha256)
    generation_stats = {
        **stats,
        "embedding_calls": embedding_calls,
        "requested_sessions": requested,
    }
    cumulative_llm_calls = int(candidate.provenance.get("tuning_llm_calls") or 0) + int(
        generation_stats.get("llm_calls") or 0
    )
    cumulative_embedding_calls = int(candidate.provenance.get("tuning_embedding_calls") or 0) + int(
        generation_stats.get("embedding_calls") or 0
    )
    provenance = {
        **candidate.provenance,
        "deferred_generation_stats": generation_stats,
        "tuning_llm_calls": cumulative_llm_calls,
        "tuning_embedding_calls": cumulative_embedding_calls,
        "reused_artifacts": sorted(set(reused_artifacts)),
        "manifests": config["manifest_sha256"],
    }
    return Candidate(candidate.name, candidate.stage, config, provenance, candidate.complexity)
