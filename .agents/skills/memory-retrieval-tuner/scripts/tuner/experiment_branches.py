from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .artifact_registry import ArtifactRegistry
from .derived_artifacts import DerivedArtifactBuilder
from .io_utils import load_json, load_jsonl, sha256_file, stable_hash
from .model_discovery import ModelDiscovery
from .models import Candidate, CandidateResult, Dataset
from .prompt_artifacts import QueryPromptArtifactGenerator, controlled_query_prompt_variants
from .source_prompt_variants import (
    controlled_page_prompt_variants,
    controlled_session_longterm_prompt_variants,
    controlled_session_merge_prompt_variants,
)
from .parameter_schema import validate_candidate_config

COST_RANK = {"cheap": 0, "medium": 1, "high": 2, "expensive": 3}
ALL_REGIMES = frozenset(
    {
        "ranking_bottleneck",
        "candidate_coverage_bottleneck",
        "session_instability",
        "data_artifact_suspicion",
        "balanced_or_plateau",
    }
)


@dataclass(frozen=True)
class BranchSpec:
    name: str
    diagnostic_regimes: frozenset[str]
    cost_level: str
    required_artifacts: tuple[str, ...]
    execution_adapter: str
    provenance_contract: tuple[str, ...]
    resource_requirements: dict[str, Any]
    priority: int
    initial_stage: bool = False

    def serializable(self) -> dict[str, Any]:
        value = asdict(self)
        value["diagnostic_regimes"] = sorted(self.diagnostic_regimes)
        return value


@dataclass(frozen=True)
class BranchCoverageRule:
    branch_name: str
    priority: int
    minimum_attempts: int = 1
    coverage_class: str = "selectable"


@dataclass
class BranchOutcome:
    branch_name: str
    status: str
    candidates: list[Candidate] = field(default_factory=list)
    reason: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    llm_calls: int = 0
    embedding_calls: int = 0
    reused_artifacts: list[str] = field(default_factory=list)

    def event(self, *, stage_index: int, diagnostic_regime: str) -> dict[str, Any]:
        return {
            "stage_index": stage_index,
            "branch": self.branch_name,
            "diagnostic_regime": diagnostic_regime,
            "status": self.status,
            "reason": self.reason,
            "candidate_names": [candidate.name for candidate in self.candidates],
            "candidate_count": len(self.candidates),
            "provenance": self.provenance,
            "llm_calls": self.llm_calls,
            "embedding_calls": self.embedding_calls,
            "reused_artifacts": self.reused_artifacts,
        }


@dataclass(frozen=True)
class BranchContext:
    dataset: Dataset
    baseline: Candidate
    anchor: Candidate
    anchor_result: CandidateResult
    diagnostic: Mapping[str, Any]
    search_space: Mapping[str, Any]
    budget: str
    k: int
    ranking_depth: int
    tune_sessions: tuple[str, ...]
    registry: ArtifactRegistry
    run_dir: Path
    model_discovery: ModelDiscovery
    stage_index: int
    generation_round: int = 1
    branch_history: tuple[Mapping[str, Any], ...] = ()
    execution_settings: Mapping[str, Any] = field(default_factory=dict)


class ExperimentBranch(Protocol):
    spec: BranchSpec

    def generate(self, context: BranchContext) -> BranchOutcome: ...

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, str | None]: ...


def _candidate(
    context: BranchContext,
    *,
    branch: str,
    label: str,
    cost_level: str,
    complexity: int,
    base: Candidate | None = None,
    provenance: Mapping[str, Any] | None = None,
    **changes: Any,
) -> Candidate:
    source = base or context.anchor
    config = dict(source.config)
    config.update(changes)
    config["experiment_branch"] = branch
    config["branch_cost_level"] = cost_level
    config["parent_candidate_hash"] = stable_hash(source.config)
    config["applied_branches"] = list(dict.fromkeys([*(source.config.get("applied_branches") or []), branch]))
    config.setdefault("ablation_from_baseline", False)
    # Validate at generation time as well as at Research-decision time.
    validate_candidate_config(config)
    anchor_suffix = stable_hash(source.config)[:6]
    return Candidate(
        name=f"{branch}:{label}:from-{anchor_suffix}",
        stage=f"stage_{context.stage_index}_{branch}",
        config=config,
        provenance={**source.provenance, **dict(provenance or {})},
        complexity=source.complexity + complexity,
    )


def _candidate_provenance_valid(candidate: Candidate) -> tuple[bool, str | None]:
    if candidate.config.get("backend") != "production_midterm":
        return False, "branch anchor is not a production_midterm Candidate"
    manifests = [Path(str(value)) for value in candidate.config.get("manifest_paths") or []]
    if not manifests or any(not path.exists() for path in manifests):
        return False, "production MidTerm manifests are missing"
    expected = candidate.config.get("manifest_sha256") or {}
    if any(expected.get(str(path.resolve())) != sha256_file(path) for path in manifests):
        return False, "production MidTerm manifest hash mismatch"
    return True, None


def _derived_dimensions(anchor: Candidate, **overrides: Any) -> dict[str, Any]:
    """Carry every already-won representation dimension into a rebuilt artifact."""

    query_path = anchor.config.get("query_artifact_path")
    dimensions: dict[str, Any] = {
        "query_representation": str(anchor.config.get("query_representation") or "original"),
        "query_artifact_path": Path(str(query_path)) if query_path else None,
        "query_artifact_variant": anchor.config.get("query_artifact_variant"),
        "page_representation_name": str(anchor.config.get("page_representation") or "production"),
        "embedding_model_id": str(anchor.config.get("embedding_model_id") or "production"),
        "embedding_revision": anchor.config.get("embedding_model_revision"),
        "embedding_local_path": anchor.config.get("embedding_model_path"),
        "encoding_contract": anchor.config.get("encoding_contract"),
        "include_field_vectors": anchor.config.get("reranker_method") == "multi_vector_maxsim",
    }
    dimensions.update(overrides)
    return dimensions


def _tuning_cost_provenance(
    context: BranchContext,
    *,
    llm_calls: int = 0,
    embedding_calls: int = 0,
) -> dict[str, int]:
    return {
        "tuning_llm_calls": int(context.anchor.provenance.get("tuning_llm_calls") or 0) + llm_calls,
        "tuning_embedding_calls": int(context.anchor.provenance.get("tuning_embedding_calls") or 0) + embedding_calls,
    }


def _stateful_source_spec(
    context: BranchContext,
    *,
    overrides: Mapping[str, Any],
    kind: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Build an immutable production replay spec for a stateful candidate."""
    manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
    if not manifests:
        return None
    manifest = load_json(manifests[0])
    identity = {
        "schema": 3,
        "kind": kind,
        "dataset_sha256": context.dataset.sha256,
        "base_manifest": sha256_file(manifests[0]),
        "overrides": dict(overrides),
        "stateful_replay": True,
    }
    base_spec = dict(context.anchor.config.get("source_generation_spec") or {})
    if not base_spec:
        base_spec = {
            "dataset_path": context.dataset.path,
            "dataset_sha256": context.dataset.sha256,
            "session_turn_counts": {key: len(turns) for key, turns in context.dataset.sessions.items()},
            "memory_config_path": manifest.get("memory_config_path"),
            "llm_mode": manifest.get("llm_mode") or "real",
            "page_summary_prompt": None,
            "context_mode": "none",
            "source_variant": kind,
            "session_order": sorted(context.dataset.sessions),
        }
    spec = {
        **base_spec,
        "config_overrides": dict(overrides),
        "source_identity": identity,
        "source_variant": kind,
        "stateful_replay": True,
        "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
    }
    return spec, identity


class BaseBranch:
    spec: BranchSpec

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, str | None]:
        del context
        valid, reason = _candidate_provenance_valid(candidate)
        if not valid:
            return valid, reason
        artifact_path = candidate.config.get("derived_artifact_path")
        artifact_sha = candidate.config.get("derived_artifact_sha256")
        if artifact_path:
            path = Path(str(artifact_path))
            if not path.exists() or sha256_file(path) != artifact_sha:
                return False, "derived artifact hash mismatch"
        return True, None


def _relative_values(config: Mapping[str, Any], baseline: int, *, minimum: int) -> list[int]:
    values = config.get("relative_values") or []
    return sorted({max(minimum, baseline + int(value)) for value in values})


class RetrievalControlBranch(BaseBranch):
    spec = BranchSpec(
        name="RetrievalControl",
        diagnostic_regimes=ALL_REGIMES,
        cost_level="cheap",
        required_artifacts=("production_midterm_checkpoints",),
        execution_adapter="ProductionMidtermAdapter",
        provenance_contract=("dataset_sha256", "manifest_sha256", "production_config"),
        resource_requirements={"llm": False, "embedding": False, "gpu": False},
        priority=10,
        initial_stage=True,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        retrieval = (((context.search_space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
            "retrieval"
        ) or {}
        candidates: list[Candidate] = []
        axes = (
            ("top_k_pages", context.k),
            ("max_total_pages", 1),
            ("midterm_candidate_pool_multiplier", 1),
        )
        for axis, minimum in axes:
            baseline = int(context.anchor.config.get(axis) or minimum)
            axis_config = retrieval.get(axis) or {}
            if context.generation_round == 1:
                values = _relative_values(axis_config, baseline, minimum=minimum)
            else:
                step = max(1, int(axis_config.get("refine_step") or 1))
                values = sorted({max(minimum, baseline - step), baseline, baseline + step})
            if axis == "max_total_pages":
                configured = axis_config.get("values") or [1, 2, 3, 4, 5]
                values = [int(value) for value in configured if 1 <= int(value) <= 5]
            if axis == "midterm_candidate_pool_multiplier":
                configured = axis_config.get("values") or [2, 4, 6, 8]
                values = [int(value) for value in configured if 1 <= int(value) <= 8]
            for value in values:
                if value == baseline:
                    continue
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"{axis}={value}",
                        cost_level=self.spec.cost_level,
                        complexity=1,
                        **{axis: value},
                )
            )
        threshold_config = retrieval.get("midterm_rag_threshold") or {}
        threshold_values = threshold_config.get("coarse") or [0.00, 0.05, 0.10, 0.15, 0.20, 0.30]
        baseline_threshold = float(context.anchor.config.get("midterm_rag_threshold", 0.1))
        for threshold in sorted({float(value) for value in threshold_values if 0 <= float(value) <= 1}):
            if threshold == baseline_threshold:
                continue
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"midterm_rag_threshold={threshold:.2f}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    midterm_rag_threshold=threshold,
                )
            )
        return BranchOutcome(self.spec.name, "READY", candidates=candidates)


class QueryRepresentationBranch(BaseBranch):
    spec = BranchSpec(
        name="QueryRepresentation",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "production_llm_config"),
        execution_adapter="DerivedQueryVectorAdapter",
        provenance_contract=("dataset_sha256", "query_artifact_sha256", "embedding_model", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "gpu": False},
        priority=20,
        initial_stage=False,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        if context.budget == "quick":
            return BranchOutcome(
                self.spec.name,
                "BUDGET_BLOCKED",
                reason="quick budget does not generate new Query Prompt LLM artifacts",
            )
        settings = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "query_representation"
        ) or {}
        max_rounds = max(1, min(3, int(settings.get("max_rounds") or 3)))
        if context.generation_round > max_rounds:
            return BranchOutcome(self.spec.name, "EXHAUSTED", reason=f"max_rounds={max_rounds} reached")
        variants = controlled_query_prompt_variants(
            dataset=context.dataset,
            anchor=context.anchor,
            anchor_result=context.anchor_result,
            tune_sessions=context.tune_sessions,
            generation_round=context.generation_round,
            variants_per_round=min(
                int(settings.get("variants_per_round") or 3),
                int(context.execution_settings.get("remaining_expensive_candidates") or 3),
            ),
        )
        generator = QueryPromptArtifactGenerator(context.registry)
        builder = DerivedArtifactBuilder(context.registry)
        candidates: list[Candidate] = []
        embeddings = 0
        reused: list[str] = []
        failures: list[str] = []
        llm_calls = 0
        for variant in variants:
            try:
                artifact = generator.generate(
                    dataset=context.dataset,
                    anchor=context.anchor,
                    variant=variant,
                    tune_sessions=context.tune_sessions,
                    max_parallel_llm_calls=int(context.execution_settings.get("max_parallel_llm_calls") or 1),
                )
                llm_calls += artifact.llm_calls
                result = builder.build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.anchor,
                    **_derived_dimensions(
                        context.anchor,
                        query_representation="bounded_reference_resolution",
                        query_artifact_path=artifact.path,
                        query_artifact_variant=artifact.variant,
                    ),
                )
                embeddings += result.embedding_calls
                if result.reused:
                    reused.append(result.sha256)
                if artifact.reused:
                    reused.append(artifact.sha256)
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"round{context.generation_round}-{variant.optimization_direction}",
                        cost_level=self.spec.cost_level,
                        complexity=1,
                        provenance={
                            "dataset_sha256": context.dataset.sha256,
                            "query_artifact_sha256": artifact.sha256,
                            "parent_prompt_hash": variant.parent_prompt_hash,
                            "prompt_hash": variant.prompt_hash,
                            "prompt_text": variant.prompt_text,
                            "generation_round": variant.generation_round,
                            "optimization_direction": variant.optimization_direction,
                            "analysis_session_ids": list(context.tune_sessions),
                            "shortterm_capacity_messages": artifact.identity["shortterm_capacity_messages"],
                            "shortterm_qa_turns": artifact.identity["shortterm_qa_turns"],
                            "history_policy": artifact.identity["history_policy"],
                            "production_config_hash": artifact.identity["production_config_hash"],
                            "llm_calls": artifact.llm_calls,
                            **_tuning_cost_provenance(
                                context,
                                llm_calls=artifact.llm_calls,
                                embedding_calls=result.embedding_calls,
                            ),
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                        },
                        query_representation="bounded_reference_resolution",
                        query_prompt_text=variant.prompt_text,
                        query_prompt_hash=variant.prompt_hash,
                        query_prompt_parent_hash=variant.parent_prompt_hash,
                        query_prompt_generation_round=variant.generation_round,
                        query_optimization_direction=variant.optimization_direction,
                        query_artifact_path=str(artifact.path),
                        query_artifact_sha256=artifact.sha256,
                        query_artifact_variant=artifact.variant,
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                    )
                )
            except Exception as exc:
                failures.append(f"{variant.optimization_direction}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(failures) if failures else None,
            provenance={
                "generation_round": context.generation_round,
                "analysis_session_ids": list(context.tune_sessions),
                "variants": [
                    {
                        "parent_prompt_hash": variant.parent_prompt_hash,
                        "prompt_text": variant.prompt_text,
                        "prompt_hash": variant.prompt_hash,
                        "generation_round": variant.generation_round,
                        "optimization_direction": variant.optimization_direction,
                    }
                    for variant in variants
                ],
            },
            llm_calls=llm_calls,
            embedding_calls=embeddings,
            reused_artifacts=reused,
        )


class PageRepresentationBranch(BaseBranch):
    spec = BranchSpec(
        name="PageRepresentation",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "session_instability", "balanced_or_plateau"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "production_page_payloads"),
        execution_adapter="DerivedPageVectorAdapter",
        provenance_contract=("dataset_sha256", "page_representation", "embedding_model", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": True, "gpu": False},
        priority=30,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        config = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "page_representation"
        ) or {}
        variants = list(config.get("values") or ["summary", "summary_keywords", "user_summary"])
        limit = 1 if context.budget == "quick" else 2 if context.budget == "standard" else len(variants)
        limit = min(limit, int(context.execution_settings.get("remaining_expensive_candidates") or limit))
        builder = DerivedArtifactBuilder(context.registry)
        candidates: list[Candidate] = []
        embeddings = 0
        reused: list[str] = []
        failures: list[str] = []
        for variant in variants[:limit]:
            if variant == "production":
                continue
            try:
                result = builder.build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.anchor,
                    **_derived_dimensions(context.anchor, page_representation_name=str(variant)),
                )
                embeddings += result.embedding_calls
                if result.reused:
                    reused.append(result.sha256)
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=str(variant),
                        cost_level=self.spec.cost_level,
                        complexity=2,
                        provenance={
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                            **_tuning_cost_provenance(context, embedding_calls=result.embedding_calls),
                        },
                        page_representation=str(variant),
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                    )
                )
            except Exception as exc:
                failures.append(f"{variant}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(failures) if failures else None,
            embedding_calls=embeddings,
            reused_artifacts=reused,
        )


class HybridRetrievalBranch(BaseBranch):
    spec = BranchSpec(
        name="HybridRetrieval",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="medium",
        required_artifacts=("production_midterm_checkpoints", "qdrant_bm25_payload"),
        execution_adapter="ProductionQdrantHybridAdapter",
        provenance_contract=("dataset_sha256", "manifest_sha256", "bm25_language"),
        resource_requirements={"llm": False, "embedding": False, "gpu": False},
        priority=15,
        initial_stage=False,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        config = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "lexical_hybrid"
        ) or {}
        if "dense_bm25_fusion" not in set(config.get("methods") or ["dense_bm25_fusion"]):
            return BranchOutcome(self.spec.name, "UNAVAILABLE", reason="dense_bm25_fusion is disabled")
        weight_config = config.get("dense_weight") or {}
        if context.generation_round == 1:
            weights = weight_config.get("coarse") or [0.7, 0.85]
        else:
            center = float(context.anchor.config.get("dense_weight") or 0.7)
            step = float(weight_config.get("refine_step") or 0.05)
            weights = [center - 2 * step, center - step, center, center + step, center + 2 * step]
        limit = 1 if context.budget == "quick" else 2 if context.budget == "standard" else len(weights)
        candidates = [
            _candidate(
                context,
                branch=self.spec.name,
                label=f"dense_weight={float(weight):.2f}",
                cost_level=self.spec.cost_level,
                complexity=2,
                retrieval_method="dense_bm25_fusion",
                dense_weight=float(weight),
            )
            for weight in weights[:limit]
            if 0.0 <= float(weight) <= 1.0 and float(weight) != float(context.anchor.config.get("dense_weight") or -1)
        ]
        return BranchOutcome(self.spec.name, "READY", candidates=candidates)


class RerankingBranch(BaseBranch):
    spec = BranchSpec(
        name="Reranking",
        diagnostic_regimes=frozenset({"ranking_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "deep_candidate_pool"),
        execution_adapter="ProductionPoolRerankerAdapter",
        provenance_contract=("dataset_sha256", "reranker_model_revision", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": False, "gpu": "optional"},
        priority=5,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        rerank_config = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "reranking"
        ) or {}
        minimum_gap = float(rerank_config.get("min_deep_recall_gap_pp") or 0.0)
        if float(context.diagnostic.get("recall_4k_minus_k_pp") or 0.0) < minimum_gap:
            return BranchOutcome(
                self.spec.name,
                "NOT_TRIGGERED",
                reason=f"deep recall gap is below min_deep_recall_gap_pp={minimum_gap}",
            )
        methods = set(rerank_config.get("methods") or ["field_lexical", "auto_discovered_cross_encoder"])
        candidates = []
        if "field_lexical" in methods:
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label="field_lexical",
                    cost_level=self.spec.cost_level,
                    complexity=2,
                    reranker_method="field_lexical",
                    reranker_dense_weight=0.75,
                        # Reranking may inspect a deep candidate pool, but the
                        # final raw Mid-term context budget remains <= 5.
                        rerank_depth=context.ranking_depth,
                        max_total_pages=min(5, max(1, int(context.anchor.config.get("max_total_pages") or 1))),
                )
            )
        unavailable: list[str] = []
        method_limit = int(rerank_config.get("max_methods_standard") or 2) if context.budget == "standard" else 99
        if context.budget in {"standard", "deep"} and "auto_discovered_cross_encoder" in methods:
            allow_network = context.budget == "deep"
            model_limit = int(rerank_config.get("max_models_deep" if allow_network else "max_models_standard") or 2)
            models = context.model_discovery.discover(
                model_type="reranker",
                allow_network=allow_network,
                general_limit=model_limit,
                finance_limit=1 if allow_network else 0,
            )
            for model in models[: max(0, min(model_limit, method_limit - len(candidates)))]:
                model = context.model_discovery.ensure_available(model, allow_download=allow_network)
                model = context.model_discovery.smoke_test(
                    model, device="cuda" if context.model_discovery.resources.gpu_count else "cpu"
                )
                if model.status != "SMOKE_PASSED":
                    unavailable.append(f"{model.model_id}: {model.status}")
                    continue
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"cross_encoder={model.model_id.replace('/', '--')}",
                        cost_level=self.spec.cost_level,
                        complexity=3,
                        provenance={"model_discovery": model.serializable(), "provenance_validated": True},
                        reranker_method="cross_encoder",
                        reranker_model_id=model.model_id,
                        reranker_model_revision=model.revision,
                        reranker_model_path=model.local_path,
                        rerank_depth=context.ranking_depth,
                        max_total_pages=min(5, max(1, int(context.anchor.config.get("max_total_pages") or 1))),
                    )
                )
        return BranchOutcome(
            self.spec.name,
            "READY",
            candidates=candidates[
                : int(context.execution_settings.get("remaining_expensive_candidates") or len(candidates))
            ],
            reason="; ".join(unavailable) if unavailable else None,
        )


class EmbeddingBranch(BaseBranch):
    spec = BranchSpec(
        name="Embedding",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "model_weights"),
        execution_adapter="DerivedEmbeddingCheckpointAdapter",
        provenance_contract=("dataset_sha256", "model_id", "model_revision", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": True, "gpu": "optional", "network": "deep_only"},
        priority=10,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        allow_network = context.budget == "deep"
        settings = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "embedding_models"
        ) or {}
        max_models = int(
            settings.get("max_models_deep" if allow_network else "max_models_standard") or (4 if allow_network else 2)
        )
        max_models = min(
            max_models, int(context.execution_settings.get("remaining_expensive_candidates") or max_models)
        )
        discovery = settings.get("discovery") or {}
        general_limit = min(max_models, int(discovery.get("general_semantic_limit") or 2))
        finance_limit = min(max_models - general_limit, int(discovery.get("finance_domain_limit") or 2))
        models = context.model_discovery.discover(
            model_type="embedding",
            allow_network=allow_network,
            general_limit=general_limit,
            finance_limit=max(0, finance_limit),
        )
        if not models:
            return BranchOutcome(self.spec.name, "UNAVAILABLE", reason="no resource-compatible local/discovered model")
        builder = DerivedArtifactBuilder(context.registry)
        candidates: list[Candidate] = []
        embeddings = 0
        reused: list[str] = []
        unavailable: list[str] = []
        for model in models:
            model = context.model_discovery.ensure_available(model, allow_download=allow_network)
            model = context.model_discovery.smoke_test(
                model,
                device="cuda" if context.model_discovery.resources.gpu_count else "cpu",
            )
            if model.status != "SMOKE_PASSED":
                unavailable.append(f"{model.model_id}: {model.status}")
                continue
            try:
                result = builder.build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.anchor,
                    **_derived_dimensions(
                        context.anchor,
                        embedding_model_id=model.model_id,
                        embedding_revision=model.revision,
                        embedding_local_path=model.local_path,
                        encoding_contract=model.encoding_contract,
                    ),
                    device="cuda" if context.model_discovery.resources.gpu_count else "cpu",
                )
                embeddings += result.embedding_calls
                if result.reused:
                    reused.append(result.sha256)
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=model.model_id.replace("/", "--"),
                        cost_level=self.spec.cost_level,
                        complexity=3,
                        provenance={
                            "model_discovery": model.serializable(),
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                            **_tuning_cost_provenance(context, embedding_calls=result.embedding_calls),
                        },
                        embedding_model_id=model.model_id,
                        embedding_model_revision=model.revision,
                        embedding_model_path=model.local_path,
                        encoding_contract=model.encoding_contract,
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                    )
                )
            except Exception as exc:
                unavailable.append(f"{model.model_id}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(unavailable) if unavailable else None,
            embedding_calls=embeddings,
            reused_artifacts=reused,
        )


class FieldAwareMultiVectorBranch(BaseBranch):
    spec = BranchSpec(
        name="FieldAwareMultiVector",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "page_fields"),
        execution_adapter="FieldAwareRerankAdapter",
        provenance_contract=("dataset_sha256", "field_weights", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": "multi_vector_only", "gpu": False},
        priority=20,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        settings = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "advanced_representation"
        ) or {}
        methods = set(settings.get("methods") or ["field_aware_lexical", "multi_vector_maxsim"])
        candidates = []
        if "field_aware_lexical" in methods:
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label="lexical_fields",
                    cost_level=self.spec.cost_level,
                    complexity=3,
                    reranker_method="field_lexical",
                    field_weights={"summary": 0.5, "keywords": 0.3, "user_input": 0.2},
                    reranker_dense_weight=0.7,
                        rerank_depth=context.ranking_depth,
                        max_total_pages=min(5, max(1, int(context.anchor.config.get("max_total_pages") or 1))),
                )
            )
        embeddings = 0
        reused: list[str] = []
        reason = None
        if context.budget == "deep" and "multi_vector_maxsim" in methods:
            try:
                result = DerivedArtifactBuilder(context.registry).build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.anchor,
                    **_derived_dimensions(context.anchor, include_field_vectors=True),
                )
                embeddings += result.embedding_calls
                if result.reused:
                    reused.append(result.sha256)
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label="maxsim_fields",
                        cost_level=self.spec.cost_level,
                        complexity=4,
                        provenance={
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                            **_tuning_cost_provenance(context, embedding_calls=result.embedding_calls),
                        },
                        reranker_method="multi_vector_maxsim",
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                        rerank_depth=context.ranking_depth,
                        max_total_pages=min(5, max(1, int(context.anchor.config.get("max_total_pages") or 1))),
                    )
                )
            except Exception as exc:
                reason = f"multi-vector unavailable: {type(exc).__name__}: {exc}"
        if context.budget == "standard":
            candidates = candidates[: max(1, int(settings.get("max_methods_standard") or 1))]
        candidates = candidates[
            : int(context.execution_settings.get("remaining_expensive_candidates") or len(candidates))
        ]
        return BranchOutcome(
            self.spec.name,
            "READY",
            candidates=candidates,
            reason=reason,
            embedding_calls=embeddings,
            reused_artifacts=reused,
        )


class SourcePromptBranch(BaseBranch):
    spec = BranchSpec(
        name="SourcePrompt",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "session_instability"}),
        cost_level="expensive",
        required_artifacts=("production_generated_pages", "prompt_hash", "model_config"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "prompt_hash", "model_config", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=30,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        if context.budget != "deep":
            return BranchOutcome(
                self.spec.name, "BUDGET_BLOCKED", reason="only budget=deep permits Add/Prompt branches"
            )
        expensive = ((context.search_space.get("search") or {}).get("stages") or {}).get("expensive") or {}
        if expensive.get("require_frontier_candidate", True) and not context.anchor_result:
            return BranchOutcome(self.spec.name, "NOT_TRIGGERED", reason="a Tune frontier anchor is required")
        tune_scope = set(context.tune_sessions)
        tune_failures = [
            row
            for row in context.anchor_result.requirement_rows
            if str(row.get("session_id") or "") in tune_scope and not bool(row.get("hit_at_k"))
        ]
        variants = controlled_page_prompt_variants(
            context.diagnostic,
            {
                "missed_at_k": len(tune_failures),
                "present_at_2k": sum(bool(row.get("hit_at_2k")) for row in tune_failures),
                "present_at_4k": sum(bool(row.get("hit_at_4k")) for row in tune_failures),
                "missing_at_4k": sum(not bool(row.get("hit_at_4k")) for row in tune_failures),
            },
        )
        configured_variants = {
            str(value)
            for section in ("memory_write_prompt", "page_summary_prompt")
            for value in ((expensive.get(section) or {}).get("variants") or [])
            if str(value) not in {"production_add", "production_summary"}
        }
        if configured_variants:
            variants = {name: value for name, value in variants.items() if name in configured_variants}
        max_candidates = max(1, int(expensive.get("max_prompt_candidates") or 4))
        max_candidates = min(
            max_candidates,
            int(context.execution_settings.get("remaining_expensive_candidates") or max_candidates),
        )
        base_manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
        if not base_manifests:
            return BranchOutcome(self.spec.name, "UNAVAILABLE", reason="production source manifest is missing")
        source_manifest = load_json(base_manifests[0])
        memory_config_path = Path(str(source_manifest["memory_config_path"]))
        llm_mode = str(source_manifest.get("llm_mode") or "real")
        session_turn_counts = {session_id: len(turns) for session_id, turns in context.dataset.sessions.items()}
        candidates: list[Candidate] = []
        for label, variant in list(variants.items())[:max_candidates]:
            prompt = str(variant["prompt"])
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            identity = {
                "schema": 2,
                "kind": "production_midterm_prompt_variant",
                "dataset_sha256": context.dataset.sha256,
                "memory_config_sha256": sha256_file(memory_config_path),
                "production_prompt_hashes": source_manifest.get("prompt_hashes") or {},
                "page_summary_prompt_hash": prompt_hash,
                "context_mode": variant["context_mode"],
                "llm_mode": llm_mode,
                "artifact_schema": 1,
            }
            source_root = context.registry.cache_root / "production_variants" / stable_hash(identity)
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=label,
                    cost_level=self.spec.cost_level,
                    complexity=4,
                    provenance={
                        "dataset_sha256": context.dataset.sha256,
                        "prompt_hash": prompt_hash,
                        "prompt_text": prompt,
                        "prompt_variant": label,
                        "prompt_kind": variant["kind"],
                        "context_mode": variant["context_mode"],
                        "generation_deferred_until_screening": True,
                        "provenance_validated": True,
                    },
                    source_generation_spec={
                        "dataset_path": context.dataset.path,
                        "dataset_sha256": context.dataset.sha256,
                        "session_turn_counts": session_turn_counts,
                        "memory_config_path": str(memory_config_path.resolve()),
                        "llm_mode": llm_mode,
                        "page_summary_prompt": prompt,
                        "context_mode": str(variant["context_mode"]),
                        "source_variant": label,
                        "source_identity": identity,
                        "source_root": str(source_root.resolve()),
                        "session_order": sorted(context.dataset.sessions),
                    },
                    page_summary_prompt=prompt,
                    page_summary_prompt_hash=prompt_hash,
                    source_variant=label,
                )
            )
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            provenance={
                "production_reference": context.anchor.name,
                "generation_policy": "screening_sessions_then_promoted_tune_then_validation",
                "generated_variants": [
                    {
                        "name": label,
                        "prompt_hash": hashlib.sha256(str(value["prompt"]).encode()).hexdigest(),
                        "prompt_text": value["prompt"],
                        "context_mode": value["context_mode"],
                        "kind": value["kind"],
                    }
                    for label, value in variants.items()
                ],
            },
        )


class MidtermEvolutionBranch(BaseBranch):
    """Generate data-derived Evolution candidates for stateful replay."""

    spec = BranchSpec(
        name="MidtermEvolution",
        diagnostic_regimes=frozenset({"session_instability", "balanced_or_plateau", "ranking_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "stateful_replay_contract"),
        execution_adapter="WithinSessionStatefulReplay",
        provenance_contract=("dataset_sha256", "turn_distance_distribution", "replay_order"),
        resource_requirements={"llm": True, "embedding": True, "gpu": False},
        priority=35,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        from .parameter_schema import dynamic_turn_distance_candidates, heat_modulation_candidates, heat_preset_candidates

        distances = []
        positions = {turn.query_id: turn.turn_index for turns in context.dataset.sessions.values() for turn in turns}
        for turns in context.dataset.sessions.values():
            for turn in turns:
                for requirement in turn.requirements:
                    for member in requirement.members:
                        if member in positions and positions[member] < turn.turn_index:
                            distances.append(turn.turn_index - positions[member])
        half_lives = dynamic_turn_distance_candidates(distances)
        tau_values = dynamic_turn_distance_candidates(distances)
        groups: list[list[tuple[str, dict[str, float]]]] = [
            [(f"half-life={value}", {"retention_half_life_turns": value}) for value in half_lives[:5]],
            [(f"recency-tau={value}", {"heat_recency_tau_turns": value}) for value in tau_values[:5]],
            [
                (f"retention-floor={value:.2f}", {"retention_floor": value})
                for value in (0.05, 0.10, 0.20, 0.30, 0.40)
            ],
            [
                (f"heat-preset={index + 1}", preset)
                for index, preset in enumerate(heat_preset_candidates())
            ],
            [
                (f"heat-modulation={index + 1}", preset)
                for index, preset in enumerate(heat_modulation_candidates())
            ],
        ]
        # Search each semantic axis independently first.  This preserves the
        # staged-search design and avoids a half-life × tau × heat Cartesian
        # grid while still letting later rounds refine a winning parent.
        changesets: list[tuple[str, dict[str, float]]] = []
        for offset in range(max(len(group) for group in groups)):
            for group in groups:
                if offset < len(group):
                    changesets.append(group[offset])

        candidates = []
        for label, changes in changesets:
            if all(context.anchor.config.get(key) == value for key, value in changes.items()):
                continue
            source = _stateful_source_spec(
                context,
                overrides={"midterm": changes},
                kind="within-session-evolution",
            )
            if source is None:
                continue
            spec, identity = source
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=label,
                    cost_level=self.spec.cost_level,
                    complexity=3,
                    provenance={
                        "stateful_replay": True,
                        "source_identity": identity,
                        "requires_source_regeneration": True,
                        "cartesian_grid": False,
                    },
                    source_generation_spec=spec,
                    source_config_overrides={"midterm": changes},
                    requires_within_session_replay=True,
                    **changes,
                )
            )
        limit = max(1, int(context.execution_settings.get("max_candidates_per_stage") or 8))
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates[:limit],
        )


class PromotionBranch(BaseBranch):
    """Tune promotion thresholds only after a Heat preset replay."""

    spec = BranchSpec(
        name="Promotion",
        diagnostic_regimes=frozenset({"session_instability", "balanced_or_plateau"}),
        cost_level="high",
        required_artifacts=("stateful_replay_contract", "heat_distribution"),
        execution_adapter="WithinSessionStatefulReplay",
        provenance_contract=("dataset_sha256", "heat_preset", "heat_distribution"),
        resource_requirements={"llm": True, "embedding": True, "gpu": False},
        priority=40,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        from .parameter_schema import promotion_threshold_candidates

        heat_values: list[float] = []
        state_rows: list[dict[str, Any]] = []
        for raw_manifest in context.anchor.config.get("manifest_paths") or []:
            manifest_path = Path(str(raw_manifest))
            if not manifest_path.is_file():
                continue
            manifest = load_json(manifest_path)
            if not bool(manifest.get("stateful_replay")):
                continue
            checkpoint_path = Path(str(manifest.get("checkpoints_path") or ""))
            if not checkpoint_path.is_file():
                continue
            for checkpoint in load_jsonl(checkpoint_path):
                for row in checkpoint.get("post_recall_heat_states") or checkpoint.get("heat_states") or []:
                    try:
                        heat = float(row["H_segment"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if math.isfinite(heat):
                        heat_values.append(heat)
                        state_rows.append(dict(row))
        if not heat_values:
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE_NO_HEAT_DISTRIBUTION",
                reason="Stateful Replay produced no production H_segment samples",
                provenance={"heat_sample_count": 0},
            )
        thresholds = promotion_threshold_candidates(heat_values)
        baseline_count = int(context.anchor.config.get("promotion_min_recall_count", 3))
        baseline_threshold = float(context.anchor.config.get("promotion_heat_threshold", 5.0))
        changesets = [
            (f"recalls={count}", {"promotion_min_recall_count": count})
            for count in (2, 3, 4, 5)
            if count != baseline_count
        ]
        changesets.extend(
            (
                f"heat={threshold:.3f}",
                {"promotion_heat_threshold": threshold},
            )
            for threshold in thresholds
            if not math.isclose(threshold, baseline_threshold)
        )
        candidates = []
        for label, changes in changesets:
            source = _stateful_source_spec(
                context,
                overrides={"midterm": changes},
                kind="within-session-promotion",
            )
            if source is None:
                continue
            spec, identity = source
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=label,
                    cost_level=self.spec.cost_level,
                    complexity=2,
                    provenance={
                        "stateful_replay": True,
                        "source_identity": identity,
                        "requires_source_regeneration": True,
                    },
                    source_generation_spec=spec,
                    source_config_overrides={"midterm": changes},
                    requires_within_session_replay=True,
                    **changes,
                )
            )
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates[: max(1, int(context.execution_settings.get("max_candidates_per_stage") or 8))],
            provenance={
                "heat_sample_count": len(heat_values),
                "heat_thresholds": thresholds,
                "heat_state_fields": sorted({key for row in state_rows for key in row}),
                "source": "production_stateful_replay",
            },
        )


class MidtermSourceConfigBranch(BaseBranch):
    """Screen source-changing Mid-term config candidates with real Add replay."""

    spec = BranchSpec(
        name="MidtermSourceConfig",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "session_instability", "balanced_or_plateau"}),
        cost_level="expensive",
        required_artifacts=("production_midterm_checkpoints", "source_generation_contract"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "effective_source_config_hash", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": False},
        priority=25,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        if context.budget != "deep":
            return BranchOutcome(self.spec.name, "BUDGET_BLOCKED", reason="source-changing candidates require deep budget")
        manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
        if not manifests:
            return BranchOutcome(self.spec.name, "UNAVAILABLE", reason="source manifest is missing")
        manifest = load_json(manifests[0])
        base_spec = dict(context.anchor.config.get("source_generation_spec") or {})
        if not base_spec:
            base_spec = {
                "dataset_path": context.dataset.path,
                "dataset_sha256": context.dataset.sha256,
                "session_turn_counts": {key: len(turns) for key, turns in context.dataset.sessions.items()},
                "memory_config_path": manifest.get("memory_config_path"),
                "llm_mode": manifest.get("llm_mode") or "real",
                "page_summary_prompt": None,
                "context_mode": "none",
                "source_variant": "source-config",
                "source_root": str(context.registry.cache_root / "production_variants"),
                "session_order": sorted(context.dataset.sessions),
            }
        baseline_midterm = dict(manifest.get("production_config") or {})
        axes = [
            ("short_term_capacity", [4, 6, 8]),
            ("session_similarity_threshold", [0.5, 0.6, 0.7, 0.8, 0.9]),
            ("top_k_sessions", [max(1, int(baseline_midterm.get("top_k_sessions", 5)) - 1), int(baseline_midterm.get("top_k_sessions", 5)), int(baseline_midterm.get("top_k_sessions", 5)) + 1, int(baseline_midterm.get("top_k_sessions", 5)) + 2]),
        ]
        candidates = []
        for axis, values in axes:
            for value in dict.fromkeys(values):
                if value == baseline_midterm.get(axis):
                    continue
                overrides = {"midterm": {axis: value}}
                identity = {
                    "schema": 3,
                    "kind": "production_midterm_source_config",
                    "dataset_sha256": context.dataset.sha256,
                    "base_manifest": sha256_file(manifests[0]),
                    "overrides": overrides,
                }
                spec = {**base_spec, "config_overrides": overrides, "source_identity": identity, "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity))}
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"{axis}={value}",
                        cost_level=self.spec.cost_level,
                        complexity=5,
                        provenance={"source_changing": True, "source_identity": identity, "requires_source_regeneration": True},
                        source_generation_spec=spec,
                        source_config_overrides=overrides,
                        **{axis: value},
                    )
                )
        # Session assignment weights are a paired preset, never an
        # independent Cartesian grid.  Each pair changes the generated
        # Session source and therefore receives its own source identity.
        for embedding_weight, keyword_weight in ((0.5, 0.5), (0.6, 0.4), (0.7, 0.3), (0.8, 0.2), (0.9, 0.1)):
            if (
                math.isclose(float(baseline_midterm.get("embedding_similarity_weight", 0.7)), embedding_weight)
                and math.isclose(float(baseline_midterm.get("keyword_overlap_weight", 0.3)), keyword_weight)
            ):
                continue
            overrides = {
                "midterm": {
                    "embedding_similarity_weight": embedding_weight,
                    "keyword_overlap_weight": keyword_weight,
                }
            }
            identity = {
                "schema": 3,
                "kind": "production_midterm_source_weight_preset",
                "dataset_sha256": context.dataset.sha256,
                "base_manifest": sha256_file(manifests[0]),
                "overrides": overrides,
            }
            spec = {
                **base_spec,
                "config_overrides": overrides,
                "source_identity": identity,
                "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
            }
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"assignment_weights={embedding_weight:.1f}/{keyword_weight:.1f}",
                    cost_level=self.spec.cost_level,
                    complexity=5,
                    provenance={"source_changing": True, "source_identity": identity, "requires_source_regeneration": True},
                    source_generation_spec=spec,
                    source_config_overrides=overrides,
                    embedding_similarity_weight=embedding_weight,
                    keyword_overlap_weight=keyword_weight,
                )
            )
        return BranchOutcome(self.spec.name, "READY" if candidates else "UNAVAILABLE", candidates=candidates[: max(1, int(context.execution_settings.get("remaining_expensive_candidates") or 8))])


class SessionLongtermRetrievalBranch(BaseBranch):
    spec = BranchSpec(
        name="SessionLongtermRetrieval",
        diagnostic_regimes=ALL_REGIMES,
        cost_level="medium",
        required_artifacts=("production_full_memory_trace",),
        execution_adapter="ProductionLongtermAdapter",
        provenance_contract=("dataset_sha256", "longterm_config"),
        resource_requirements={"llm": False, "embedding": False, "gpu": False},
        priority=18,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        settings = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get("session_longterm") or {}
        groups: list[list[Candidate]] = []
        top_k_candidates = []
        for top_k in settings.get("longterm_top_k") or [5, 10, 15, 20, 25, 30]:
            top_k_candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"top_k={top_k}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    longterm_top_k=int(top_k),
                )
            )
        groups.append(top_k_candidates)
        threshold_candidates = []
        for threshold in settings.get("longterm_rag_threshold") or [0.05, 0.1, 0.2, 0.3, 0.4, 0.5]:
            threshold_candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"threshold={float(threshold):.2f}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    longterm_rag_threshold=float(threshold),
                )
            )
        groups.append(threshold_candidates)
        multiplier_candidates = []
        for multiplier in settings.get("longterm_candidate_pool_multiplier") or [2, 4, 6]:
            multiplier_candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"candidate_multiplier={multiplier}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    longterm_candidate_pool_multiplier=int(multiplier),
                )
            )
        groups.append(multiplier_candidates)
        preset_candidates = []
        for preset in settings.get("hybrid_presets") or [
            "semantic-heavy",
            "balanced",
            "keyword-heavy",
            "entity-aware",
        ]:
            preset_candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"hybrid={preset}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    longterm_hybrid_preset=str(preset),
                )
            )
        groups.append(preset_candidates)
        entity_candidates = []
        for threshold in settings.get("entity_similarity_threshold") or [0.4, 0.5, 0.6, 0.7, 0.8]:
            entity_candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"entity-threshold={float(threshold):.1f}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    entity_similarity_threshold=float(threshold),
                )
            )
        groups.append(entity_candidates)

        # Put one candidate from every high-level Long-term axis into the
        # minimum screen before spending depth on any one axis.
        candidates = []
        for offset in range(max(len(group) for group in groups)):
            for group in groups:
                if offset < len(group):
                    candidate = group[offset]
                    changed = {
                        key: value
                        for key, value in candidate.config.items()
                        if context.anchor.config.get(key) != value
                    }
                    if changed:
                        candidates.append(candidate)
        limit = max(1, int(context.execution_settings.get("max_candidates_per_stage") or 8))
        return BranchOutcome(self.spec.name, "READY", candidates=candidates[:limit])


class QueryRewritePromptBranch(QueryRepresentationBranch):
    spec = BranchSpec(
        name="QueryRewritePrompt",
        diagnostic_regimes=QueryRepresentationBranch.spec.diagnostic_regimes,
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "query_prompt_artifact"),
        execution_adapter="DerivedQueryVectorAdapter",
        provenance_contract=("dataset_sha256", "parent_prompt_hash", "prompt_hash", "query_artifact_sha256"),
        resource_requirements={"llm": True, "embedding": True, "gpu": False},
        priority=21,
    )


def _split_source_prompt_outcome(
    context: BranchContext,
    *,
    branch_spec: BranchSpec,
    prompt_field: str,
    variants: Mapping[str, str],
) -> BranchOutcome:
    if context.budget != "deep":
        return BranchOutcome(branch_spec.name, "BUDGET_BLOCKED", reason="source Prompt candidates require deep budget")
    manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
    if not manifests:
        return BranchOutcome(branch_spec.name, "UNAVAILABLE", reason="production source manifest is missing")
    manifest = load_json(manifests[0])
    memory_config_path = Path(str(manifest["memory_config_path"]))
    session_turn_counts = {session_id: len(turns) for session_id, turns in context.dataset.sessions.items()}
    tune_scope = set(context.tune_sessions)
    missed = sum(
        str(row.get("session_id") or "") in tune_scope and not bool(row.get("hit_at_k"))
        for row in context.anchor_result.requirement_rows
    )
    candidates = []
    for label, base_prompt in variants.items():
        prompt = (
            f"{base_prompt}\n\nTune-only aggregate diagnostics: regime="
            f"{context.diagnostic.get('regime', 'balanced_or_plateau')}, missed_requirements={missed}."
        )
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        identity = {
            "schema": 3,
            "kind": prompt_field,
            "dataset_sha256": context.dataset.sha256,
            "memory_config_sha256": sha256_file(memory_config_path),
            "production_prompt_hashes": manifest.get("prompt_hashes") or {},
            "prompt_hash": prompt_hash,
            "invalidates": {
                "session_merge_prompt": ["midterm_sessions", "promotion", "cross_session_longterm"],
                "session_longterm_extraction_prompt": ["session_longterm", "all_memory_context"],
            }.get(prompt_field, ["midterm_pages", "midterm_sessions", "downstream_memory"]),
        }
        source_root = context.registry.cache_root / "production_variants" / stable_hash(identity)
        source_spec = {
            "dataset_path": context.dataset.path,
            "dataset_sha256": context.dataset.sha256,
            "session_turn_counts": session_turn_counts,
            "memory_config_path": str(memory_config_path.resolve()),
            "llm_mode": str(manifest.get("llm_mode") or "real"),
            "page_summary_prompt": None,
            "session_merge_prompt": None,
            "session_longterm_extraction_prompt": None,
            "context_mode": "none",
            "source_variant": label,
            "source_identity": identity,
            "source_root": str(source_root.resolve()),
            "session_order": sorted(context.dataset.sessions),
        }
        source_spec[prompt_field] = prompt
        candidates.append(
            _candidate(
                context,
                branch=branch_spec.name,
                label=label,
                cost_level=branch_spec.cost_level,
                complexity=4,
                provenance={
                    "dataset_sha256": context.dataset.sha256,
                    "prompt_hash": prompt_hash,
                    "prompt_kind": prompt_field,
                    "source_identity": identity,
                    "requires_source_regeneration": True,
                    "tune_aggregate_only": True,
                    "provenance_validated": True,
                },
                source_generation_spec=source_spec,
                source_variant=label,
                **{prompt_field: prompt, f"{prompt_field}_hash": prompt_hash},
            )
        )
    return BranchOutcome(
        branch_spec.name,
        "READY" if candidates else "UNAVAILABLE",
        candidates=candidates,
        provenance={
            "prompt_kind": prompt_field,
            "parent": "production/original",
            "analysis_session_ids": list(context.tune_sessions),
        },
    )


class MidtermPageSummaryPromptBranch(SourcePromptBranch):
    spec = BranchSpec(
        name="MidtermPageSummaryPrompt",
        diagnostic_regimes=SourcePromptBranch.spec.diagnostic_regimes,
        cost_level="expensive",
        required_artifacts=("production_generated_pages", "page_summary_prompt"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "page_summary_prompt_hash", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=31,
    )


class MidtermSessionMergePromptBranch(SourcePromptBranch):
    spec = BranchSpec(
        name="MidtermSessionMergePrompt",
        diagnostic_regimes=frozenset({"session_instability", "candidate_coverage_bottleneck"}),
        cost_level="expensive",
        required_artifacts=("production_generated_sessions", "session_merge_prompt"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "session_merge_prompt_hash", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=32,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        return _split_source_prompt_outcome(
            context,
            branch_spec=self.spec,
            prompt_field="session_merge_prompt",
            variants=controlled_session_merge_prompt_variants(),
        )


class SessionLongtermExtractionPromptBranch(SourcePromptBranch):
    spec = BranchSpec(
        name="SessionLongtermExtractionPrompt",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="expensive",
        required_artifacts=("production_longterm_outputs", "session_longterm_prompt"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "session_longterm_prompt_hash", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=33,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        return _split_source_prompt_outcome(
            context,
            branch_spec=self.spec,
            prompt_field="session_longterm_extraction_prompt",
            variants=controlled_session_longterm_prompt_variants(),
        )


class BranchRegistry:
    def __init__(self, branches: Iterable[ExperimentBranch] | None = None):
        self._branches: dict[str, ExperimentBranch] = {}
        for branch in branches or default_branches():
            self.register(branch)

    def register(self, branch: ExperimentBranch) -> None:
        if branch.spec.name in self._branches:
            raise ValueError(f"Duplicate experiment branch: {branch.spec.name}")
        if branch.spec.cost_level not in COST_RANK:
            raise ValueError(f"Unknown cost level for {branch.spec.name}: {branch.spec.cost_level}")
        self._branches[branch.spec.name] = branch

    def describe(self) -> list[dict[str, Any]]:
        return [branch.spec.serializable() for branch in self.ordered()]

    def ordered(self) -> list[ExperimentBranch]:
        return sorted(
            self._branches.values(),
            key=lambda branch: (COST_RANK[branch.spec.cost_level], branch.spec.priority, branch.spec.name),
        )

    def get(self, name: str) -> ExperimentBranch:
        try:
            return self._branches[name]
        except KeyError as exc:
            raise KeyError(f"Unknown experiment Branch: {name}") from exc

    @staticmethod
    def _max_rounds(branch: ExperimentBranch, branch_settings: Mapping[str, Any]) -> int:
        value = branch_settings.get(branch.spec.name) or {}
        return max(1, int(value.get("max_rounds") or 1)) if isinstance(value, Mapping) else 1

    def _coverage_rules(
        self,
        *,
        regime: str,
        coverage_policy: Mapping[str, Any] | None,
    ) -> list[BranchCoverageRule]:
        configured = (coverage_policy or {}).get(regime) or {}
        relevant = configured.get("relevant") if isinstance(configured, Mapping) else None
        rules: list[BranchCoverageRule] = []
        if isinstance(relevant, Mapping):
            for name, raw_rule in relevant.items():
                resolved_name = str(name)
                # QueryRewritePrompt replaced the old prompt-search role of
                # QueryRepresentation.  The fallback keeps custom/legacy test
                # registries usable without making both paths executable in a
                # normal registry.
                if resolved_name not in self._branches and resolved_name == "QueryRewritePrompt":
                    resolved_name = "QueryRepresentation"
                if resolved_name not in self._branches:
                    continue
                values = raw_rule if isinstance(raw_rule, Mapping) else {}
                rules.append(
                    BranchCoverageRule(
                        branch_name=resolved_name,
                        priority=int(values.get("priority") or self._branches[resolved_name].spec.priority),
                        minimum_attempts=max(0, int(values.get("minimum_attempts", 1))),
                        coverage_class=str(
                            values.get("coverage_class")
                            or values.get("policy")
                            or (
                                "required"
                                if self._branches[resolved_name].spec.initial_stage
                                else "expensive_gated"
                                if self._branches[resolved_name].spec.cost_level == "expensive"
                                else "selectable"
                            )
                        ),
                    )
                )
        if rules:
            return rules
        return [
            BranchCoverageRule(
                branch.spec.name,
                branch.spec.priority,
                1 if branch.spec.initial_stage else 0,
                "required"
                if branch.spec.initial_stage
                else "expensive_gated"
                if branch.spec.cost_level == "expensive"
                else "selectable",
            )
            for branch in self.ordered()
            if regime in branch.spec.diagnostic_regimes
        ]

    def coverage(
        self,
        *,
        regime: str,
        max_cost_level: str,
        attempt_counts: Mapping[str, int],
        exhausted: set[str],
        exhaustion_reasons: Mapping[str, str],
        enabled: set[str] | None,
        branch_settings: Mapping[str, Any] | None,
        coverage_policy: Mapping[str, Any] | None,
        remaining_expensive_candidates: int,
        branch_history: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        settings = branch_settings or {}
        rules = self._coverage_rules(regime=regime, coverage_policy=coverage_policy)
        max_cost = COST_RANK[max_cost_level]
        relevant: list[str] = []
        attempted: dict[str, Any] = {}
        exhausted_rows: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        remaining: list[tuple[tuple[int, int, int, str], str]] = []
        revisitable: list[str] = []
        coverage_classes: dict[str, str] = {}
        required: list[str] = []
        selectable: list[str] = []
        expensive_gated: list[str] = []
        unexplored: list[str] = []
        remaining_required: list[str] = []
        for rule in rules:
            branch = self._branches[rule.branch_name]
            name = branch.spec.name
            relevant.append(name)
            if rule.coverage_class not in {"required", "selectable", "expensive_gated"}:
                raise ValueError(f"Unknown coverage class for {name}: {rule.coverage_class}")
            coverage_classes[name] = rule.coverage_class
            if rule.coverage_class == "required":
                required.append(name)
            elif rule.coverage_class == "expensive_gated":
                expensive_gated.append(name)
            else:
                selectable.append(name)
            attempts = int(attempt_counts.get(name, 0))
            max_rounds = self._max_rounds(branch, settings)
            if not attempts:
                unexplored.append(name)
            if rule.minimum_attempts > 0 and attempts < rule.minimum_attempts:
                remaining_required.append(name)
            if attempts:
                attempted[name] = {
                    "attempts": attempts,
                    "max_rounds": max_rounds,
                    "minimum_attempts": rule.minimum_attempts,
                    "rounds": list((branch_history or {}).get(name) or []),
                }
            reason: str | None = None
            configured_enabled = None
            raw_settings = settings.get(name)
            if isinstance(raw_settings, Mapping) and "enabled" in raw_settings:
                configured_enabled = bool(raw_settings.get("enabled"))
            if (enabled is not None and name not in enabled) or configured_enabled is False:
                reason = "disabled by search.branch_registry"
            elif regime not in branch.spec.diagnostic_regimes:
                reason = f"not relevant to adapter regime={regime}"
            elif COST_RANK[branch.spec.cost_level] > max_cost:
                reason = f"budget blocks cost={branch.spec.cost_level} above max_cost={max_cost_level}"
            elif name in exhausted:
                exhausted_rows.append(
                    {
                        "branch": name,
                        "attempts": attempts,
                        "reason": exhaustion_reasons.get(name, "branch declared exhausted"),
                        "coverage_class": rule.coverage_class,
                    }
                )
                continue
            elif attempts >= max_rounds:
                exhausted_rows.append(
                    {
                        "branch": name,
                        "attempts": attempts,
                        "reason": f"max_rounds={max_rounds} reached",
                        "coverage_class": rule.coverage_class,
                    }
                )
                continue
            elif branch.spec.cost_level in {"high", "expensive"} and remaining_expensive_candidates <= 0:
                reason = "max_expensive_candidates exhausted"
            if reason:
                blocked.append(
                    {
                        "branch": name,
                        "attempts": attempts,
                        "reason": reason,
                        "coverage_class": rule.coverage_class,
                    }
                )
                continue
            if attempts:
                revisitable.append(name)
            minimum_unmet = 0 if attempts < rule.minimum_attempts else 1
            remaining.append(
                (
                    (minimum_unmet, COST_RANK[branch.spec.cost_level], rule.priority, name),
                    name,
                )
            )
        remaining_names = [name for _, name in sorted(remaining)]
        return {
            "diagnostic_regime": regime,
            "relevant_branches": relevant,
            "coverage_classes": coverage_classes,
            "required_branches": required,
            "selectable_branches": selectable,
            "expensive_gated_branches": expensive_gated,
            "attempted_branches": attempted,
            "exhausted_branches": exhausted_rows,
            "remaining_branches": remaining_names,
            "revisitable_branches": [name for name in remaining_names if name in revisitable],
            "blocked_branches": blocked,
            "unexplored_branches": unexplored,
            "remaining_required_branches": remaining_required,
            "required_coverage_complete": not remaining_required,
            "relevant_coverage_complete": not remaining_names,
        }

    def select(
        self,
        *,
        regime: str,
        max_cost_level: str,
        attempt_counts: Mapping[str, int],
        exhausted: set[str],
        initial_stage: bool,
        limit: int,
        enabled: set[str] | None = None,
        branch_settings: Mapping[str, Any] | None = None,
        coverage_policy: Mapping[str, Any] | None = None,
        remaining_expensive_candidates: int = 0,
        exhaustion_reasons: Mapping[str, str] | None = None,
        branch_history: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> list[ExperimentBranch]:
        snapshot = self.coverage(
            regime=regime,
            max_cost_level=max_cost_level,
            attempt_counts=attempt_counts,
            exhausted=exhausted,
            exhaustion_reasons=exhaustion_reasons or {},
            enabled=enabled,
            branch_settings=branch_settings,
            coverage_policy=coverage_policy,
            remaining_expensive_candidates=remaining_expensive_candidates,
            branch_history=branch_history,
        )
        eligible = [
            self._branches[name]
            for name in snapshot["remaining_branches"]
            if not initial_stage or self._branches[name].spec.initial_stage
        ]
        return eligible[:limit]

    def generate(self, branch: ExperimentBranch, context: BranchContext) -> BranchOutcome:
        try:
            outcome = branch.generate(context)
        except Exception as exc:
            return BranchOutcome(
                branch.spec.name,
                "UNAVAILABLE",
                reason=f"{type(exc).__name__}: {exc}",
            )
        valid: list[Candidate] = []
        invalid: list[str] = []
        for candidate in outcome.candidates:
            is_valid, reason = branch.validate_provenance(candidate, context)
            if is_valid:
                valid.append(candidate)
            else:
                invalid.append(f"{candidate.name}: {reason}")
        outcome.candidates = valid
        if invalid:
            outcome.reason = "; ".join(filter(None, [outcome.reason, *invalid]))
        if not valid and outcome.status == "READY":
            outcome.status = "UNAVAILABLE"
        return outcome


def default_branches() -> list[ExperimentBranch]:
    return [
        RetrievalControlBranch(),
        HybridRetrievalBranch(),
        QueryRepresentationBranch(),
        RerankingBranch(),
        EmbeddingBranch(),
        FieldAwareMultiVectorBranch(),
        PageRepresentationBranch(),
        MidtermEvolutionBranch(),
        PromotionBranch(),
        MidtermSourceConfigBranch(),
        SessionLongtermRetrievalBranch(),
        QueryRewritePromptBranch(),
        MidtermPageSummaryPromptBranch(),
        MidtermSessionMergePromptBranch(),
        SessionLongtermExtractionPromptBranch(),
    ]
