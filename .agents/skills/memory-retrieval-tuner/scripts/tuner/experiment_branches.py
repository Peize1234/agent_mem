from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .artifact_registry import ArtifactRegistry
from .derived_artifacts import DerivedArtifactBuilder
from .io_utils import load_json, sha256_file, stable_hash
from .model_discovery import ModelDiscovery
from .models import Candidate, CandidateResult, Dataset


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
    anchor_suffix = stable_hash(source.config)[:6]
    return Candidate(
        name=f"{branch}:{label}:from-{anchor_suffix}",
        stage=f"stage_{context.stage_index}_{branch}",
        config=config,
        provenance={**source.provenance, **dict(provenance or {})},
        complexity=source.complexity + complexity,
    )


def _baseline_provenance_valid(context: BranchContext) -> tuple[bool, str | None]:
    if context.anchor.config.get("backend") != "production_midterm":
        return False, "branch anchor is not a production_midterm Candidate"
    manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
    if not manifests or any(not path.exists() for path in manifests):
        return False, "production MidTerm manifests are missing"
    expected = context.anchor.config.get("manifest_sha256") or {}
    if any(expected.get(str(path.resolve())) != sha256_file(path) for path in manifests):
        return False, "production MidTerm manifest hash mismatch"
    return True, None


class BaseBranch:
    spec: BranchSpec

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, str | None]:
        valid, reason = _baseline_provenance_valid(context)
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
            ("top_k_sessions", 1),
            ("top_k_pages", context.k),
            ("max_total_pages", context.k),
        )
        for axis, minimum in axes:
            baseline = int(context.anchor.config.get(axis) or minimum)
            axis_config = retrieval.get(axis) or {}
            values = _relative_values(axis_config, baseline, minimum=minimum)
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
        return BranchOutcome(self.spec.name, "READY", candidates=candidates)


class QueryRepresentationBranch(BaseBranch):
    spec = BranchSpec(
        name="QueryRepresentation",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="medium",
        required_artifacts=("production_midterm_checkpoints", "frozen_query_text"),
        execution_adapter="DerivedQueryVectorAdapter",
        provenance_contract=("dataset_sha256", "query_artifact_sha256", "embedding_model", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": True, "gpu": False},
        priority=20,
        initial_stage=False,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        query_texts = {turn.query_id: turn.question for turns in context.dataset.sessions.values() for turn in turns}
        frozen = context.registry.discover_frozen_queries(
            context.dataset.sha256,
            query_text_by_id=query_texts,
            base_candidate=context.anchor,
            limit=4 if context.budget == "deep" else 2,
        )
        if not frozen:
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE",
                reason="no exact-dataset frozen query representation with validated original-query identity",
            )
        builder = DerivedArtifactBuilder(context.registry)
        candidates: list[Candidate] = []
        embeddings = 0
        reused: list[str] = []
        failures: list[str] = []
        for item in frozen:
            try:
                result = builder.build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.baseline,
                    query_representation="bounded_reference_resolution",
                    query_artifact_path=Path(str(item.config["query_artifact_path"])),
                    query_artifact_variant=str(item.config["query_artifact_variant"]),
                )
                embeddings += result.embedding_calls
                if result.reused:
                    reused.append(result.sha256)
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=str(item.config["query_artifact_variant"]).replace(":", "-"),
                        cost_level=self.spec.cost_level,
                        complexity=1,
                        base=context.baseline,
                        provenance={
                            **item.provenance,
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                        },
                        query_representation="bounded_reference_resolution",
                        query_artifact_path=item.config["query_artifact_path"],
                        query_artifact_variant=item.config["query_artifact_variant"],
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                    )
                )
            except Exception as exc:
                failures.append(f"{item.name}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(failures) if failures else None,
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
                    baseline=context.baseline,
                    page_representation_name=str(variant),
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
                        base=context.baseline,
                        provenance={"derived_artifact_sha256": result.sha256, "provenance_validated": True},
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
        weights = (config.get("dense_weight") or {}).get("coarse") or [0.7, 0.85]
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
            if float(weight) < 1.0
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
        candidates = [
            _candidate(
                context,
                branch=self.spec.name,
                label="field_lexical",
                cost_level=self.spec.cost_level,
                complexity=2,
                reranker_method="field_lexical",
                reranker_dense_weight=0.75,
                max_total_pages=max(
                    context.ranking_depth, int(context.anchor.config.get("max_total_pages") or context.k)
                ),
            )
        ]
        unavailable: list[str] = []
        if context.budget == "deep":
            models = context.model_discovery.discover(
                model_type="reranker", allow_network=True, general_limit=2, finance_limit=2
            )
            for model in models:
                model = context.model_discovery.ensure_available(model, allow_download=True)
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
                        base=context.baseline,
                        provenance={"model_discovery": model.serializable(), "provenance_validated": True},
                        reranker_method="cross_encoder",
                        reranker_model_id=model.model_id,
                        reranker_model_revision=model.revision,
                        reranker_model_path=model.local_path,
                        max_total_pages=max(
                            context.ranking_depth,
                            int(context.anchor.config.get("max_total_pages") or context.k),
                        ),
                    )
                )
        return BranchOutcome(
            self.spec.name,
            "READY",
            candidates=candidates,
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
        models = context.model_discovery.discover(
            model_type="embedding",
            allow_network=allow_network,
            general_limit=2,
            finance_limit=2,
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
                    baseline=context.baseline,
                    embedding_model_id=model.model_id,
                    embedding_revision=model.revision,
                    embedding_local_path=model.local_path,
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
                        base=context.baseline,
                        provenance={
                            "model_discovery": model.serializable(),
                            "derived_artifact_sha256": result.sha256,
                            "provenance_validated": True,
                        },
                        embedding_model_id=model.model_id,
                        embedding_model_revision=model.revision,
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
        candidates = [
            _candidate(
                context,
                branch=self.spec.name,
                label="lexical_fields",
                cost_level=self.spec.cost_level,
                complexity=3,
                reranker_method="field_lexical",
                field_weights={"summary": 0.5, "keywords": 0.3, "user_input": 0.2},
                reranker_dense_weight=0.7,
                max_total_pages=max(
                    context.ranking_depth, int(context.anchor.config.get("max_total_pages") or context.k)
                ),
            )
        ]
        embeddings = 0
        reused: list[str] = []
        reason = None
        if context.budget == "deep":
            try:
                result = DerivedArtifactBuilder(context.registry).build(
                    dataset_sha256=context.dataset.sha256,
                    session_scope=tuple(sorted(context.dataset.sessions)),
                    baseline=context.baseline,
                    include_field_vectors=True,
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
                        base=context.baseline,
                        provenance={"derived_artifact_sha256": result.sha256, "provenance_validated": True},
                        reranker_method="multi_vector_maxsim",
                        derived_artifact_path=str(result.path),
                        derived_artifact_sha256=result.sha256,
                        max_total_pages=max(
                            context.ranking_depth,
                            int(context.anchor.config.get("max_total_pages") or context.k),
                        ),
                    )
                )
            except Exception as exc:
                reason = f"multi-vector unavailable: {type(exc).__name__}: {exc}"
        return BranchOutcome(
            self.spec.name,
            "READY",
            candidates=candidates,
            reason=reason,
            embedding_calls=embeddings,
            reused_artifacts=reused,
        )


def _prompt_hashes(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if "prompt" in str(key).lower() and "hash" in str(key).lower() and isinstance(item, str):
                result.add(item)
            result.update(_prompt_hashes(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_prompt_hashes(item))
    return result


class MemoryWriteAddPromptBranch(BaseBranch):
    spec = BranchSpec(
        name="MemoryWriteAddPrompt",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck"}),
        cost_level="expensive",
        required_artifacts=("production_generated_pages", "prompt_hash", "model_config"),
        execution_adapter="FrozenGeneratedRankingAdapter",
        provenance_contract=("dataset_sha256", "prompt_hash", "model_config", "ranking_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=30,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        if context.budget != "deep":
            return BranchOutcome(
                self.spec.name, "BUDGET_BLOCKED", reason="only budget=deep permits Add/Prompt branches"
            )
        candidates: list[Candidate] = []
        unavailable: list[str] = []
        for item in context.registry.discover_frozen_rankings(context.dataset.sha256, limit=16):
            metadata_path = Path(str(item.provenance.get("metadata_path") or ""))
            if not metadata_path.exists():
                continue
            try:
                hashes = sorted(_prompt_hashes(load_json(metadata_path)))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not hashes:
                unavailable.append(f"{item.name}: prompt hash absent")
                continue
            candidates.append(
                Candidate(
                    name=f"{self.spec.name}:{item.name}",
                    stage=f"stage_{context.stage_index}_{self.spec.name}",
                    config={
                        **item.config,
                        "experiment_branch": self.spec.name,
                        "branch_cost_level": self.spec.cost_level,
                    },
                    provenance={**item.provenance, "prompt_hashes": hashes, "provenance_validated": True},
                    complexity=context.anchor.complexity + 4,
                )
            )
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason=(
                "; ".join(unavailable[:5])
                if unavailable
                else "no exact-dataset frozen Add/Page prompt artifact with prompt hash; generation requires configured variants"
            ),
        )

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, str | None]:
        del context
        path = Path(str(candidate.config.get("ranking_path") or ""))
        if not path.exists() or candidate.config.get("ranking_sha256") != sha256_file(path):
            return False, "frozen generated ranking hash mismatch"
        if not candidate.provenance.get("prompt_hashes"):
            return False, "prompt hash is absent"
        return True, None


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

    def select(
        self,
        *,
        regime: str,
        max_cost_level: str,
        attempted: set[str],
        initial_stage: bool,
        limit: int,
        enabled: set[str] | None = None,
    ) -> list[ExperimentBranch]:
        max_cost = COST_RANK[max_cost_level]
        eligible = [
            branch
            for branch in self.ordered()
            if branch.spec.name not in attempted
            and (enabled is None or branch.spec.name in enabled)
            and COST_RANK[branch.spec.cost_level] <= max_cost
            and (branch.spec.initial_stage if initial_stage else regime in branch.spec.diagnostic_regimes)
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
        MemoryWriteAddPromptBranch(),
    ]
