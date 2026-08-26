from __future__ import annotations

import hashlib
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .agentic_retrieval_artifacts import (
    AGENTIC_FIXED_MAX_ITERATIONS,
    AGENTIC_FIXED_MAX_TOOL_CALLS,
    agentic_parent_retrieval_identity_sha256,
    build_agentic_parent_retrieval_identity,
    load_production_agentic_trace,
)
from .artifact_registry import ArtifactRegistry
from .derived_artifacts import DerivedArtifactBuilder
from .io_utils import load_json, load_jsonl, sha256_file, stable_hash
from .low_consumption import low_consumption_enabled
from .model_discovery import ModelDiscovery
from .models import Candidate, CandidateResult, Dataset
from .parameter_schema import (
    production_integer_candidates,
    production_literal_candidates,
    production_parameter_metadata,
    production_overrides_from_candidate,
    production_strategy_candidates,
    validate_candidate_config,
)
from .prompt_artifacts import (
    QueryPromptArtifactGenerator,
    controlled_query_prompt_variants,
)
from .source_prompt_variants import (
    controlled_fine_grained_longterm_prompt_variants,
    controlled_page_prompt_variants,
    controlled_session_merge_prompt_variants,
)

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
    requires_source_regeneration: bool

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
    if "query_prompt_text" in changes and "query_rewrite_prompt" not in changes:
        config["query_rewrite_prompt"] = changes["query_prompt_text"]
    config["experiment_branch"] = branch
    config["branch_cost_level"] = cost_level
    config["parent_candidate_hash"] = stable_hash(source.config)
    config["applied_branches"] = list(dict.fromkeys([*(source.config.get("applied_branches") or []), branch]))
    config.setdefault("ablation_from_baseline", False)
    config["production_overrides"] = production_overrides_from_candidate(config)
    config["experiment_metadata"] = {
        **dict(source.config.get("experiment_metadata") or {}),
        "branch": branch,
        "cost_level": cost_level,
        "parent_candidate_hash": config["parent_candidate_hash"],
        "applied_branches": config["applied_branches"],
    }
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

    unsupported = {"page_representation_name", "include_field_vectors"} & overrides.keys()
    if unsupported:
        raise ValueError("Page and field vectors must be regenerated by Production, not as derived artifacts")

    query_path = anchor.config.get("query_artifact_path")
    dimensions: dict[str, Any] = {
        "query_representation": str(anchor.config.get("query_representation") or "original"),
        "query_artifact_path": Path(str(query_path)) if query_path else None,
        "query_artifact_variant": anchor.config.get("query_artifact_variant"),
        "embedding_model_id": str(anchor.config.get("embedding_model_id") or "production"),
        "embedding_revision": anchor.config.get("embedding_model_revision"),
        "embedding_local_path": anchor.config.get("embedding_model_path"),
        "encoding_contract": anchor.config.get("encoding_contract"),
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


def _deep_merge_mapping(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge_mapping(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


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
    base_spec = dict(context.anchor.config.get("source_generation_spec") or {})
    merged_overrides = _deep_merge_mapping(dict(base_spec.get("config_overrides") or {}), overrides)
    identity = {
        "schema": 3,
        "kind": kind,
        "dataset_sha256": context.dataset.sha256,
        "base_manifest": sha256_file(manifests[0]),
        "parent_source_identity": base_spec.get("source_identity"),
        "overrides": merged_overrides,
        "stateful_replay": True,
    }
    if not base_spec:
        base_spec = {
            "dataset_path": context.dataset.path,
            "dataset_sha256": context.dataset.sha256,
            "session_turn_counts": {key: len(turns) for key, turns in context.dataset.sessions.items()},
            "memory_config_path": manifest.get("memory_config_path"),
            "llm_mode": manifest.get("llm_mode") or "real",
            "page_summary_prompt": None,
            "page_context_contract": "production_previous_current_following_raw_v1",
            "source_variant": kind,
            "session_order": sorted(context.dataset.sessions),
        }
    spec = {
        **base_spec,
        "config_overrides": merged_overrides,
        "source_identity": identity,
        "source_variant": kind,
        "stateful_replay": True,
        "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
    }
    return spec, identity


class BaseBranch:
    spec: BranchSpec
    requires_source_regeneration = False

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


def _coverage_first_numeric_values(values: Iterable[Any], baseline: int | float) -> list[Any]:
    """Order a bounded numeric sample so a truncated prefix still spans the axis.

    Stage-level candidate budgets are intentionally finite.  Sorting numeric
    candidates by distance from the production/anchor value makes the first
    few points a coarse coverage sample instead of an arbitrary low-value
    prefix.  Stable numeric ordering resolves equal-distance ties.
    """

    return sorted(values, key=lambda value: (-abs(float(value) - float(baseline)), float(value)))


class RetrievalControlBranch(BaseBranch):
    spec = BranchSpec(
        name="RetrievalControl",
        diagnostic_regimes=ALL_REGIMES,
        cost_level="cheap",
        required_artifacts=("production_midterm_checkpoints",),
        execution_adapter="ProductionMidtermAdapter",
        provenance_contract=("dataset_sha256", "manifest_sha256", "production_config"),
        resource_requirements={"llm": False, "embedding": False, "gpu": "optional"},
        priority=10,
        initial_stage=True,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        retrieval = (((context.search_space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
            "retrieval"
        ) or {}
        candidates: list[Candidate] = []
        axes = ("top_k_pages", "top_k_sessions", "max_total_pages", "midterm_candidate_pool_multiplier")
        for axis in axes:
            metadata = production_parameter_metadata(axis)
            minimum = math.ceil(metadata.ge) if metadata.ge is not None else math.floor(metadata.gt) + 1
            if axis == "top_k_pages":
                minimum = max(minimum, context.k)
            baseline = int(context.anchor.config.get(axis) or minimum)
            axis_config = retrieval.get(axis) or {}
            if context.generation_round == 1:
                values = _relative_values(axis_config, baseline, minimum=minimum)
            else:
                step = max(1, int(axis_config.get("refine_step") or 1))
                values = sorted({max(minimum, baseline - step), baseline, baseline + step})
            if axis == "max_total_pages":
                context_budget = int(
                    (context.anchor.config.get("benchmark_constraints") or {}).get("context_budget")
                    or max(context.k, baseline)
                )
                values = list(range(minimum, context_budget + 1))
            if axis == "midterm_candidate_pool_multiplier":
                values = production_integer_candidates(axis)
            else:
                values = production_strategy_candidates(axis, values)
            values = _coverage_first_numeric_values(values, baseline)
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
                        provenance={"search_axis": axis},
                        **{axis: value},
                    )
                )
        threshold_config = retrieval.get("midterm_rag_threshold") or {}
        threshold_values = threshold_config.get("coarse") or []
        baseline_threshold = float(
            context.anchor.config.get(
                "midterm_rag_threshold", production_parameter_metadata("midterm_rag_threshold").default
            )
        )
        ordered_thresholds = _coverage_first_numeric_values(
            production_strategy_candidates("midterm_rag_threshold", threshold_values),
            baseline_threshold,
        )
        for raw_threshold in ordered_thresholds:
            threshold = float(raw_threshold)
            if threshold == baseline_threshold:
                continue
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"midterm_rag_threshold={threshold:.2f}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    provenance={"search_axis": "midterm_rag_threshold"},
                    midterm_rag_threshold=threshold,
                )
            )
        return BranchOutcome(self.spec.name, "READY", candidates=candidates)


class AgenticRetrievalBranch(BaseBranch):
    spec = BranchSpec(
        name="AgenticRetrieval",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="cheap",
        required_artifacts=("production_midterm_checkpoints", "production_agentic_trace"),
        execution_adapter="ProductionAgenticTraceAdapter",
        provenance_contract=(
            "dataset_sha256",
            "manifest_sha256",
            "production_agentic_trace_sha256",
            "parent_retrieval_identity_sha256",
            "exact_agentic_parameter_variant",
            "agentic_fixed_max_tool_result_chars",
        ),
        resource_requirements={"llm": False, "embedding": False, "gpu": False},
        priority=25,
    )

    @staticmethod
    def _query_ids(context: BranchContext) -> dict[str, tuple[str, ...]]:
        return {
            session_id: tuple(turn.query_id for turn in turns) for session_id, turns in context.dataset.sessions.items()
        }

    def _load_trace(self, candidate: Candidate, context: BranchContext):
        expected_parent = build_agentic_parent_retrieval_identity(candidate.config)
        return load_production_agentic_trace(
            candidate.config,
            dataset_sha256=context.dataset.sha256,
            query_ids_by_session=self._query_ids(context),
            expected_parent_retrieval_identity=expected_parent,
        )

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, str | None]:
        valid, reason = super().validate_provenance(candidate, context)
        if not valid:
            return valid, reason
        try:
            trace = self._load_trace(candidate, context)
        except (OSError, ValueError) as exc:
            return False, str(exc)
        variant = (int(candidate.config["max_queries"]), int(candidate.config["max_total_results"]))
        if variant not in trace.variants:
            return False, "production Agentic trace does not contain the Candidate's exact parameter variant"
        return True, None

    def generate(self, context: BranchContext) -> BranchOutcome:
        retrieval = (((context.search_space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
            "retrieval"
        ) or {}
        settings = retrieval.get("agentic_retrieval")
        if not isinstance(settings, Mapping):
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE",
                reason="search_space.yaml has no agentic_retrieval search definition",
            )
        raw_max_tool_result_chars = context.anchor.config.get("agentic_fixed_max_tool_result_chars")
        try:
            max_tool_result_chars = int(
                validate_candidate_config({"max_tool_result_chars": raw_max_tool_result_chars})["max_tool_result_chars"]
            )
        except (TypeError, ValueError):
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE",
                reason="current Anchor is missing valid fixed Agentic max_tool_result_chars provenance",
            )
        parent_identity = build_agentic_parent_retrieval_identity(context.anchor.config)
        parent_identity_sha256 = agentic_parent_retrieval_identity_sha256(parent_identity)
        trace_error: OSError | ValueError | None = None
        try:
            trace = self._load_trace(context.anchor, context)
        except (OSError, ValueError) as exc:
            trace_error = exc
            try:
                discovered = context.registry.discover_production_agentic_trace(
                    dataset_sha256=context.dataset.sha256,
                    query_ids_by_session=self._query_ids(context),
                    expected_parent_retrieval_identity=parent_identity,
                    expected_max_tool_result_chars=max_tool_result_chars,
                )
            except ValueError as discovery_exc:
                trace_error = discovery_exc
                discovered = None
            if discovered is None:
                return BranchOutcome(
                    self.spec.name,
                    "UNAVAILABLE",
                    reason=f"production_agentic_trace unavailable: {trace_error}",
                    provenance={
                        "required_artifact": "production_agentic_trace",
                        "expected_parent_retrieval_identity_sha256": parent_identity_sha256,
                    },
                )
            discovered_config = {
                **context.anchor.config,
                "production_agentic_trace_paths": discovered["paths"],
                "production_agentic_trace_sha256": discovered["sha256"],
            }
            trace = self._load_trace(
                Candidate(
                    name=context.anchor.name,
                    stage=context.anchor.stage,
                    config=discovered_config,
                    provenance=context.anchor.provenance,
                    complexity=context.anchor.complexity,
                ),
                context,
            )

        query_values = production_integer_candidates("max_queries")
        result_values = production_integer_candidates("max_total_results")
        baseline_queries = int(
            context.anchor.config.get("max_queries") or production_parameter_metadata("max_queries").default
        )
        baseline_results = int(
            context.anchor.config.get("max_total_results") or production_parameter_metadata("max_total_results").default
        )
        available = set(trace.variants)
        variants = {
            *((value, baseline_results) for value in query_values if value != baseline_queries),
            *((baseline_queries, value) for value in result_values if value != baseline_results),
        }
        candidates: list[Candidate] = []
        for max_queries, max_total_results in sorted(variants):
            if (max_queries, max_total_results) not in available:
                continue
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"max_queries={max_queries},max_total_results={max_total_results}",
                    cost_level=self.spec.cost_level,
                    complexity=1,
                    provenance={
                        "production_agentic_trace_sha256": trace.sha256,
                        "production_agentic_execution_contract": "Memory.run_agentic_retrieval",
                        "parent_retrieval_identity": parent_identity,
                        "parent_retrieval_identity_sha256": parent_identity_sha256,
                        "agentic_fixed_max_tool_result_chars": trace.max_tool_result_chars,
                        "provenance_validated": True,
                    },
                    agentic_trace_enabled=True,
                    max_queries=max_queries,
                    max_total_results=max_total_results,
                    agentic_fixed_max_iterations=AGENTIC_FIXED_MAX_ITERATIONS,
                    agentic_fixed_max_tool_calls=AGENTIC_FIXED_MAX_TOOL_CALLS,
                    agentic_fixed_max_tool_result_chars=trace.max_tool_result_chars,
                    production_agentic_trace_paths=[str(path) for path in trace.paths],
                    production_agentic_trace_sha256=trace.sha256,
                    production_agentic_parent_retrieval_identity=parent_identity,
                    production_agentic_parent_retrieval_identity_sha256=parent_identity_sha256,
                )
            )
        missing_axes = []
        if not any(candidate.config["max_queries"] != baseline_queries for candidate in candidates):
            missing_axes.append("max_queries")
        if not any(candidate.config["max_total_results"] != baseline_results for candidate in candidates):
            missing_axes.append("max_total_results")
        if missing_axes:
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE",
                reason=(
                    "production_agentic_trace has no complete exact Candidate variants for axes: "
                    + ", ".join(missing_axes)
                ),
                provenance={
                    "required_artifact": "production_agentic_trace",
                    "available_variants": [list(value) for value in trace.variants],
                },
            )
        return BranchOutcome(
            self.spec.name,
            "READY",
            candidates=candidates,
            provenance={
                "required_artifact": "production_agentic_trace",
                "trace_sha256": trace.sha256,
                "parent_retrieval_identity_sha256": parent_identity_sha256,
                "agentic_fixed_max_tool_result_chars": trace.max_tool_result_chars,
                "available_variants": [list(value) for value in trace.variants],
            },
            reused_artifacts=sorted(trace.sha256.values()),
        )


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
    requires_source_regeneration = True
    spec = BranchSpec(
        name="PageRepresentation",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "session_instability", "balanced_or_plateau"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "production_page_payloads"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "page_representation", "embedding_model", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": True, "gpu": False},
        priority=30,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        from mem0.memory.midterm import PAGE_REPRESENTATION_ALIASES

        variants = list(
            dict.fromkeys(
                PAGE_REPRESENTATION_ALIASES.get(str(value), str(value))
                for value in production_literal_candidates("page_representation")
            )
        )
        production_default = str(production_parameter_metadata("page_representation").default)
        limit = 1 if context.budget == "quick" else 2 if context.budget == "standard" else len(variants)
        limit = min(limit, int(context.execution_settings.get("remaining_expensive_candidates") or limit))
        candidates: list[Candidate] = []
        failures: list[str] = []
        for variant in variants[:limit]:
            if variant == production_default:
                continue
            try:
                source = _stateful_source_spec(
                    context,
                    overrides={"midterm": {"page_representation": str(variant)}},
                    kind=f"production_page_representation_{variant}",
                )
                if source is None:
                    raise ValueError("production source manifest is missing")
                spec, identity = source
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=str(variant),
                        cost_level=self.spec.cost_level,
                        complexity=2,
                        provenance={
                            "source_identity": identity,
                            "provenance_validated": True,
                        },
                        page_representation=str(variant),
                        source_generation_spec=spec,
                        source_config_overrides={"midterm": {"page_representation": str(variant)}},
                        derived_artifact_path=None,
                        derived_artifact_sha256=None,
                    )
                )
            except Exception as exc:
                failures.append(f"{variant}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(failures) if failures else None,
        )


class HybridRetrievalBranch(BaseBranch):
    spec = BranchSpec(
        name="HybridRetrieval",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="medium",
        required_artifacts=("production_midterm_checkpoints", "qdrant_bm25_payload"),
        execution_adapter="ProductionMidtermAdapter",
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
        fusion_methods = production_literal_candidates("fusion_method")
        weight_config = config.get("dense_weight") or {}
        production_weight = float(production_parameter_metadata("dense_weight").default)
        if context.generation_round == 1:
            weights = weight_config.get("coarse") or [production_weight]
        else:
            center = float(context.anchor.config.get("dense_weight") or production_weight)
            step = float(weight_config.get("refine_step") or 0.05)
            weights = [center - 2 * step, center - step, center, center + step, center + 2 * step]
        anchor_retrieval_method = str(
            context.anchor.config.get("retrieval_method") or production_parameter_metadata("retrieval_method").default
        )
        anchor_fusion_method = str(
            context.anchor.config.get("fusion_method") or production_parameter_metadata("fusion_method").default
        )
        anchor_dense_weight = float(
            context.anchor.config.get("dense_weight") or production_parameter_metadata("dense_weight").default
        )
        valid_weights = []
        for raw_weight in production_strategy_candidates("dense_weight", weights):
            weight = float(raw_weight)
            if (
                anchor_retrieval_method == "dense_bm25_fusion"
                and anchor_fusion_method == "normalized_score"
                and math.isclose(weight, anchor_dense_weight)
            ):
                continue
            valid_weights.append(weight)
        weight_limit = 1 if context.budget == "quick" else 2 if context.budget == "standard" else len(valid_weights)
        candidates: list[Candidate] = []
        if "normalized_score" in fusion_methods:
            candidates.extend(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label=f"fusion=normalized_score,dense_weight={weight:.2f}",
                    cost_level=self.spec.cost_level,
                    complexity=2,
                    retrieval_method="dense_bm25_fusion",
                    fusion_method="normalized_score",
                    dense_weight=weight,
                )
                for weight in valid_weights[:weight_limit]
            )
        if "rrf" in fusion_methods and not (
            anchor_retrieval_method == "dense_bm25_fusion" and anchor_fusion_method == "rrf"
        ):
            candidates.append(
                _candidate(
                    context,
                    branch=self.spec.name,
                    label="fusion=rrf",
                    cost_level=self.spec.cost_level,
                    complexity=2,
                    retrieval_method="dense_bm25_fusion",
                    fusion_method="rrf",
                )
            )
        return BranchOutcome(self.spec.name, "READY", candidates=candidates)


class RerankingBranch(BaseBranch):
    spec = BranchSpec(
        name="Reranking",
        diagnostic_regimes=frozenset({"ranking_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "deep_candidate_pool"),
        execution_adapter="ProductionMidtermAdapter",
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
        methods = set(rerank_config.get("methods") or ["auto_discovered_cross_encoder"])
        candidates = []
        unavailable: list[str] = []
        method_limit = int(rerank_config.get("max_methods_standard") or 1) if context.budget == "standard" else 99
        if context.budget not in {"standard", "deep"}:
            return BranchOutcome(self.spec.name, "BUDGET_BLOCKED", reason="cross-encoder reranking requires standard")
        if method_limit <= 0 or "auto_discovered_cross_encoder" not in methods:
            return BranchOutcome(self.spec.name, "UNAVAILABLE", reason="cross-encoder reranking is disabled")

        from mem0.configs.base import MidTermMemoryConfig

        anchor_reranker = dict(context.anchor.config.get("reranker") or {})
        reference_depth = int(
            context.anchor.config.get("rerank_depth")
            or anchor_reranker.get("rerank_depth")
            or MidTermMemoryConfig().reranker.rerank_depth
        )
        configured_depths = rerank_config.get("rerank_depth") or [reference_depth]
        depths = sorted(int(value) for value in production_strategy_candidates("rerank_depth", configured_depths))
        if not depths:
            return BranchOutcome(
                self.spec.name,
                "UNAVAILABLE",
                reason="rerank_depth has no values accepted by Production",
            )

        if context.generation_round == 1:
            allow_network = context.budget == "deep"
            model_limit = int(rerank_config.get("max_models_deep" if allow_network else "max_models_standard") or 2)
            models = context.model_discovery.discover(
                model_type="reranker",
                allow_network=allow_network,
                general_limit=model_limit,
                finance_limit=1 if allow_network else 0,
            )
            for model in models[: max(0, model_limit)]:
                model = context.model_discovery.ensure_available(model, allow_download=allow_network)
                model = context.model_discovery.smoke_test(
                    model, device=str(model.resource_usage.get("preferred_device") or "cpu")
                )
                if model.status != "SMOKE_PASSED":
                    unavailable.append(f"{model.model_id}: {model.status}")
                    continue
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"cross_encoder={model.model_id.replace('/', '--')},depth={reference_depth}",
                        cost_level=self.spec.cost_level,
                        complexity=3,
                        provenance={"model_discovery": model.serializable(), "provenance_validated": True},
                        reranker_method="cross_encoder",
                        reranker_model_id=model.model_id,
                        reranker_model_revision=model.revision,
                        reranker_model_path=model.local_path,
                        reranker_inference_device=str(model.resource_usage.get("preferred_device") or "cuda"),
                        reranker_inference_precision=model.resource_usage.get("inference_precision"),
                        reranker_inference_batch_size=int(model.resource_usage.get("inference_batch_size") or 1),
                        rerank_depth=reference_depth,
                    )
                )
        else:
            applied_branches = set(context.anchor.config.get("applied_branches") or [])
            has_reranking_lineage = (
                context.anchor.config.get("experiment_branch") == self.spec.name or self.spec.name in applied_branches
            )
            prior_frontier_winner = any(bool(record.get("frontier_winner")) for record in context.branch_history)
            model_id = context.anchor.config.get("reranker_model_id")
            model_revision = context.anchor.config.get("reranker_model_revision")
            model_path = context.anchor.config.get("reranker_model_path")
            if (
                not has_reranking_lineage
                or not prior_frontier_winner
                or context.anchor.config.get("reranker_method") != "cross_encoder"
                or not model_id
                or not model_revision
                or not model_path
            ):
                return BranchOutcome(
                    self.spec.name,
                    "NOT_TRIGGERED",
                    reason="depth refinement requires the evaluated Reranking frontier winner as current anchor",
                )
            current_depth = int(context.anchor.config.get("rerank_depth") or reference_depth)
            for depth in depths:
                if depth == current_depth:
                    continue
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"cross_encoder={str(model_id).replace('/', '--')},depth={depth}",
                        cost_level=self.spec.cost_level,
                        complexity=1,
                        reranker_method="cross_encoder",
                        reranker_model_id=model_id,
                        reranker_model_revision=model_revision,
                        reranker_model_path=model_path,
                        rerank_depth=depth,
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
    requires_source_regeneration = True
    spec = BranchSpec(
        name="Embedding",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_source_dataset", "production_runtime", "model_weights"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=(
            "dataset_sha256",
            "model_id",
            "model_revision",
            "encoding_contract",
            "source_identity",
            "manifest_sha256",
        ),
        resource_requirements={"llm": True, "embedding": True, "gpu": "optional", "network": "deep_only"},
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
        candidates: list[Candidate] = []
        unavailable: list[str] = []
        for model in models:
            model = context.model_discovery.ensure_available(model, allow_download=allow_network)
            model = context.model_discovery.smoke_test(
                model,
                device=str(model.resource_usage.get("preferred_device") or "cpu"),
            )
            if model.status != "SMOKE_PASSED":
                unavailable.append(f"{model.model_id}: {model.status}")
                continue
            try:
                if not model.revision or not model.local_path or not Path(model.local_path).exists():
                    raise ValueError("embedding source replay requires an immutable local model snapshot")
                if not model.encoding_contract:
                    raise ValueError("embedding source replay requires a validated encoding contract")
                embedding_dimension = int(model.resource_usage.get("embedding_dimension") or 0)
                if embedding_dimension <= 0:
                    raise ValueError("embedding smoke test did not record a valid embedding dimension")
                manifests = [Path(str(value)) for value in context.anchor.config.get("manifest_paths") or []]
                if not manifests:
                    raise ValueError("embedding source replay requires production MidTerm manifests")
                manifest = load_json(manifests[0])
                effective_config = dict(manifest.get("effective_memory_config") or {})
                base_embedder = deepcopy(dict(effective_config.get("embedder") or {}))
                embedder_config = deepcopy(dict(base_embedder.get("config") or {}))
                inference_device = str(model.resource_usage.get("preferred_device") or "cpu")
                inference_precision = str(model.resource_usage.get("inference_precision") or "")
                runtime_model_kwargs: dict[str, Any] = {
                    "local_files_only": True,
                    "device": inference_device,
                }
                if inference_device.startswith("cuda") and inference_precision:
                    runtime_model_kwargs["model_kwargs"] = _deep_merge_mapping(
                        dict((embedder_config.get("model_kwargs") or {}).get("model_kwargs") or {}),
                        {"torch_dtype": inference_precision},
                    )
                embedder_config.update(
                    {
                        "model": str(Path(model.local_path).resolve()),
                        "revision": model.revision,
                        "embedding_dims": embedding_dimension,
                        "encoding_contract": dict(model.encoding_contract),
                        "model_kwargs": _deep_merge_mapping(
                            dict(embedder_config.get("model_kwargs") or {}),
                            runtime_model_kwargs,
                        ),
                    }
                )
                embedding_overrides = {
                    "embedder": {"provider": "huggingface", "config": embedder_config},
                    "vector_store": {"config": {"embedding_model_dims": embedding_dimension}},
                }
                base_spec = dict(context.anchor.config.get("source_generation_spec") or {})
                base_overrides = dict(base_spec.get("config_overrides") or {})
                config_overrides = _deep_merge_mapping(base_overrides, embedding_overrides)
                if not base_spec:
                    base_spec = {
                        "dataset_path": context.dataset.path,
                        "dataset_sha256": context.dataset.sha256,
                        "session_turn_counts": {key: len(turns) for key, turns in context.dataset.sessions.items()},
                        "memory_config_path": manifest.get("memory_config_path"),
                        "llm_mode": manifest.get("llm_mode") or "real",
                        "page_summary_prompt": None,
                        "page_context_contract": "production_previous_current_following_raw_v1",
                        "session_order": sorted(context.dataset.sessions),
                    }
                identity = {
                    "schema": 3,
                    "kind": "embedding_production_source_replay",
                    "dataset_sha256": context.dataset.sha256,
                    "base_manifest": sha256_file(manifests[0]),
                    "model_id": model.model_id,
                    "model_revision": model.revision,
                    "model_local_path": str(Path(model.local_path).resolve()),
                    "embedding_dimension": embedding_dimension,
                    "encoding_contract": dict(model.encoding_contract),
                    "config_overrides": config_overrides,
                    "real_add_replay": True,
                }
                source_variant = f"embedding:{model.model_id}@{model.revision}"
                source_spec = {
                    **base_spec,
                    "config_overrides": config_overrides,
                    "embedding_encoding_contract": dict(model.encoding_contract),
                    "embedding_model_id": model.model_id,
                    "embedding_model_revision": model.revision,
                    "embedding_model_path": str(Path(model.local_path).resolve()),
                    "embedding_dimension": embedding_dimension,
                    "source_identity": identity,
                    "source_variant": source_variant,
                    "stateful_replay": False,
                    "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
                }
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=model.model_id.replace("/", "--"),
                        cost_level=self.spec.cost_level,
                        complexity=3,
                        provenance={
                            "model_discovery": model.serializable(),
                            "source_identity": identity,
                            "provenance_validated": True,
                        },
                        embedding_model_id=model.model_id,
                        embedding_model_revision=model.revision,
                        embedding_model_path=str(Path(model.local_path).resolve()),
                        encoding_contract=model.encoding_contract,
                        embedding_dimension=embedding_dimension,
                        embedding_inference_device=inference_device,
                        embedding_inference_precision=inference_precision or None,
                        embedding_inference_batch_size=int(model.resource_usage.get("inference_batch_size") or 4),
                        embedding_source_regenerated=True,
                        source_generation_spec=source_spec,
                        source_config_overrides=config_overrides,
                        source_variant=source_variant,
                        derived_artifact_path=None,
                        derived_artifact_sha256=None,
                    )
                )
            except Exception as exc:
                unavailable.append(f"{model.model_id}: {type(exc).__name__}: {exc}")
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates,
            reason="; ".join(unavailable) if unavailable else None,
        )


class FieldAwareMultiVectorBranch(BaseBranch):
    requires_source_regeneration = True
    spec = BranchSpec(
        name="FieldAwareMultiVector",
        diagnostic_regimes=frozenset({"ranking_bottleneck", "candidate_coverage_bottleneck"}),
        cost_level="high",
        required_artifacts=("production_midterm_checkpoints", "page_fields"),
        execution_adapter="ProductionMidtermAdapter",
        provenance_contract=("dataset_sha256", "manifest_sha256"),
        resource_requirements={"llm": False, "embedding": "multi_vector_only", "gpu": False},
        priority=20,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        settings = (((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}).get(
            "advanced_representation"
        ) or {}
        methods = set(settings.get("methods") or ["multi_vector_maxsim"])
        candidates = []
        embeddings = 0
        reused: list[str] = []
        reason = None
        if context.budget == "deep" and "multi_vector_maxsim" in methods:
            try:
                source = _stateful_source_spec(
                    context,
                    overrides={
                        "midterm": {
                            "reranker": {
                                "method": "multi_vector_maxsim",
                                "rerank_depth": context.ranking_depth,
                            }
                        }
                    },
                    kind="production_midterm_multi_vector_maxsim",
                )
                if source is None:
                    raise ValueError("production source manifest is missing")
                spec, identity = source
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label="maxsim_fields",
                        cost_level=self.spec.cost_level,
                        complexity=4,
                        provenance={
                            "source_identity": identity,
                            "provenance_validated": True,
                        },
                        reranker_method="multi_vector_maxsim",
                        source_generation_spec=spec,
                        source_config_overrides={
                            "midterm": {
                                "reranker": {
                                    "method": "multi_vector_maxsim",
                                    "rerank_depth": context.ranking_depth,
                                }
                            }
                        },
                        derived_artifact_path=None,
                        derived_artifact_sha256=None,
                        rerank_depth=context.ranking_depth,
                    )
                )
            except Exception as exc:
                reason = f"multi-vector unavailable: {type(exc).__name__}: {exc}"
            if low_consumption_enabled():
                # A low-consumption run still permits online model discovery,
                # download, and local inference. Compose the field-aware method
                # with discovered embedding candidates so the experiment does
                # not silently assume the repository's baseline embedder.
                embedding_outcome = EmbeddingBranch().generate(context)
                unavailable = [embedding_outcome.reason] if embedding_outcome.reason else []
                reranker_override = {
                    "midterm": {
                        "reranker": {
                            "method": "multi_vector_maxsim",
                            "rerank_depth": context.ranking_depth,
                        }
                    }
                }
                for embedding_candidate in embedding_outcome.candidates:
                    embedding_spec = dict(embedding_candidate.config.get("source_generation_spec") or {})
                    embedding_overrides = dict(embedding_candidate.config.get("source_config_overrides") or {})
                    combined_overrides = _deep_merge_mapping(embedding_overrides, reranker_override)
                    identity = {
                        "schema": 1,
                        "kind": "low_consumption_discovered_embedding_multi_vector",
                        "dataset_sha256": context.dataset.sha256,
                        "embedding_source_identity": embedding_spec.get("source_identity"),
                        "config_overrides": combined_overrides,
                    }
                    combined_spec = {
                        **embedding_spec,
                        "config_overrides": combined_overrides,
                        "source_identity": identity,
                        "source_variant": (
                            f"field-aware:{embedding_candidate.config.get('embedding_model_id') or 'unknown'}"
                        ),
                        "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
                    }
                    candidates.append(
                        _candidate(
                            context,
                            base=embedding_candidate,
                            branch=self.spec.name,
                            label=(
                                "maxsim_fields+"
                                + str(embedding_candidate.config.get("embedding_model_id") or "embedding").replace(
                                    "/", "--"
                                )
                            ),
                            cost_level=self.spec.cost_level,
                            complexity=4,
                            provenance={
                                "source_identity": identity,
                                "provenance_validated": True,
                                "field_embedding_model_discovered_online": True,
                            },
                            reranker_method="multi_vector_maxsim",
                            source_generation_spec=combined_spec,
                            source_config_overrides=combined_overrides,
                            derived_artifact_path=None,
                            derived_artifact_sha256=None,
                            rerank_depth=context.ranking_depth,
                        )
                    )
                if unavailable:
                    reason = "; ".join(value for value in [reason, *unavailable] if value)
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
    requires_source_regeneration = True
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
        base_source_spec = dict(context.anchor.config.get("source_generation_spec") or {})
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
                "page_context_contract": variant["context_contract"],
                "llm_mode": llm_mode,
                "artifact_schema": 1,
                "parent_source_identity": base_source_spec.get("source_identity"),
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
                        "page_context_contract": variant["context_contract"],
                        "generation_deferred_until_screening": True,
                        "provenance_validated": True,
                    },
                    source_generation_spec={
                        **base_source_spec,
                        "dataset_path": context.dataset.path,
                        "dataset_sha256": context.dataset.sha256,
                        "session_turn_counts": session_turn_counts,
                        "memory_config_path": str(memory_config_path.resolve()),
                        "llm_mode": llm_mode,
                        "page_summary_prompt": prompt,
                        "page_context_contract": str(variant["context_contract"]),
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
                        "page_context_contract": value["context_contract"],
                        "kind": value["kind"],
                    }
                    for label, value in variants.items()
                ],
            },
        )


class MidtermEvolutionBranch(BaseBranch):
    requires_source_regeneration = True
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
        from .parameter_schema import (
            dynamic_turn_distance_candidates,
            heat_modulation_candidates,
            heat_preset_candidates,
        )

        distances = []
        positions = {turn.query_id: turn.turn_index for turns in context.dataset.sessions.values() for turn in turns}
        for turns in context.dataset.sessions.values():
            for turn in turns:
                for requirement in turn.requirements:
                    for member in requirement.members:
                        if member in positions and positions[member] < turn.turn_index:
                            distances.append(turn.turn_index - positions[member])
        retrieval_strategy = (((context.search_space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
            "retrieval"
        ) or {}
        evolution_strategy = retrieval_strategy.get("midterm_evolution") or {}
        half_lives = production_strategy_candidates(
            "retention_half_life_turns", dynamic_turn_distance_candidates(distances)
        )
        tau_values = production_strategy_candidates(
            "heat_recency_tau_turns", dynamic_turn_distance_candidates(distances)
        )
        retention_floors = production_strategy_candidates(
            "retention_floor", evolution_strategy.get("retention_floor") or []
        )
        groups: list[list[tuple[str, dict[str, float]]]] = [
            [(f"half-life={value}", {"retention_half_life_turns": value}) for value in half_lives[:5]],
            [(f"recency-tau={value}", {"heat_recency_tau_turns": value}) for value in tau_values[:5]],
            [(f"retention-floor={value:.2f}", {"retention_floor": value}) for value in retention_floors],
            [(f"heat-preset={index + 1}", preset) for index, preset in enumerate(heat_preset_candidates())],
            [(f"heat-modulation={index + 1}", preset) for index, preset in enumerate(heat_modulation_candidates())],
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
            try:
                changes = validate_candidate_config(changes, allow_unknown=False)
            except (TypeError, ValueError):
                continue
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

    requires_source_regeneration = True

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

    requires_source_regeneration = True

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
            return BranchOutcome(
                self.spec.name, "BUDGET_BLOCKED", reason="source-changing candidates require deep budget"
            )
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
                "page_context_contract": "production_previous_current_following_raw_v1",
                "source_variant": "source-config",
                "source_root": str(context.registry.cache_root / "production_variants"),
                "session_order": sorted(context.dataset.sessions),
            }
        baseline_midterm = dict(manifest.get("production_config") or {})
        retrieval_strategy = (((context.search_space.get("search") or {}).get("stages") or {}).get("cheap") or {}).get(
            "retrieval"
        ) or {}
        axes = [
            ("short_term_capacity", (retrieval_strategy.get("short_term_capacity") or {}).get("values") or []),
            (
                "session_similarity_threshold",
                (retrieval_strategy.get("session_similarity_threshold") or {}).get("values") or [],
            ),
        ]
        candidates = []
        for axis, values in axes:
            for value in production_strategy_candidates(axis, values):
                if value == baseline_midterm.get(axis):
                    continue
                overrides = {"midterm": {axis: value}}
                merged_overrides = _deep_merge_mapping(dict(base_spec.get("config_overrides") or {}), overrides)
                identity = {
                    "schema": 3,
                    "kind": "production_midterm_source_config",
                    "dataset_sha256": context.dataset.sha256,
                    "base_manifest": sha256_file(manifests[0]),
                    "parent_source_identity": base_spec.get("source_identity"),
                    "overrides": merged_overrides,
                }
                spec = {
                    **base_spec,
                    "config_overrides": merged_overrides,
                    "source_identity": identity,
                    "source_root": str(context.registry.cache_root / "production_variants" / stable_hash(identity)),
                }
                candidates.append(
                    _candidate(
                        context,
                        branch=self.spec.name,
                        label=f"{axis}={value}",
                        cost_level=self.spec.cost_level,
                        complexity=5,
                        provenance={
                            "source_identity": identity,
                        },
                        source_generation_spec=spec,
                        source_config_overrides=overrides,
                        **{axis: value},
                    )
                )
        # Session assignment weights are a paired preset, never an
        # independent Cartesian grid.  Each pair changes the generated
        # Session source and therefore receives its own source identity.
        assignment_presets = (retrieval_strategy.get("session_assignment_weight_presets") or {}).get("values") or []
        default_embedding_weight = production_parameter_metadata("embedding_similarity_weight").default
        default_keyword_weight = production_parameter_metadata("keyword_overlap_weight").default
        for raw_embedding_weight, raw_keyword_weight in assignment_presets:
            embedding_values = production_strategy_candidates("embedding_similarity_weight", [raw_embedding_weight])
            keyword_values = production_strategy_candidates("keyword_overlap_weight", [raw_keyword_weight])
            if not embedding_values or not keyword_values:
                continue
            embedding_weight = float(embedding_values[0])
            keyword_weight = float(keyword_values[0])
            if math.isclose(
                float(baseline_midterm.get("embedding_similarity_weight", default_embedding_weight)), embedding_weight
            ) and math.isclose(
                float(baseline_midterm.get("keyword_overlap_weight", default_keyword_weight)), keyword_weight
            ):
                continue
            overrides = {
                "midterm": {
                    "embedding_similarity_weight": embedding_weight,
                    "keyword_overlap_weight": keyword_weight,
                }
            }
            merged_overrides = _deep_merge_mapping(dict(base_spec.get("config_overrides") or {}), overrides)
            identity = {
                "schema": 3,
                "kind": "production_midterm_source_weight_preset",
                "dataset_sha256": context.dataset.sha256,
                "base_manifest": sha256_file(manifests[0]),
                "parent_source_identity": base_spec.get("source_identity"),
                "overrides": merged_overrides,
            }
            spec = {
                **base_spec,
                "config_overrides": merged_overrides,
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
                    provenance={
                        "source_identity": identity,
                    },
                    source_generation_spec=spec,
                    source_config_overrides=overrides,
                    embedding_similarity_weight=embedding_weight,
                    keyword_overlap_weight=keyword_weight,
                )
            )
        return BranchOutcome(
            self.spec.name,
            "READY" if candidates else "UNAVAILABLE",
            candidates=candidates[: max(1, int(context.execution_settings.get("remaining_expensive_candidates") or 8))],
        )


class FineGrainedLongtermRetrievalBranch(BaseBranch):
    spec = BranchSpec(
        name="FineGrainedLongtermRetrieval",
        diagnostic_regimes=ALL_REGIMES,
        cost_level="medium",
        required_artifacts=("production_full_memory_trace",),
        execution_adapter="ProductionMidtermAdapter",
        provenance_contract=("dataset_sha256", "longterm_config"),
        resource_requirements={"llm": False, "embedding": False, "gpu": False},
        priority=18,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        secondary = ((context.search_space.get("search") or {}).get("stages") or {}).get("secondary") or {}
        settings = secondary.get("fine_grained_longterm") or secondary.get("session_longterm") or {}
        groups: list[list[Candidate]] = []
        top_k_candidates = []
        for top_k in production_integer_candidates("longterm_top_k"):
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
        for threshold in production_strategy_candidates(
            "longterm_rag_threshold", settings.get("longterm_rag_threshold") or []
        ):
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
        for multiplier in production_integer_candidates("longterm_candidate_pool_multiplier"):
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
            try:
                validate_candidate_config({"longterm_hybrid_preset": preset})
            except (TypeError, ValueError):
                continue
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
        for threshold in production_strategy_candidates(
            "entity_similarity_threshold", settings.get("entity_similarity_threshold") or []
        ):
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

        fine_reranker_candidates = []
        if context.budget in {"standard", "deep"} and "auto_discovered_cross_encoder" in set(
            settings.get("reranker_methods") or []
        ):
            from mem0.configs.base import FineGrainedLongTermConfig

            anchor_fine = dict(context.anchor.config.get("fine_grained_longterm") or {})
            production_overrides = dict(context.anchor.config.get("production_overrides") or {})
            override_fine = dict(production_overrides.get("fine_grained_longterm") or {})
            nested_reranker = dict(anchor_fine.get("reranker") or override_fine.get("reranker") or {})

            def legal_depth(value: Any) -> int | None:
                values = production_strategy_candidates("longterm_rerank_depth", [] if value is None else [value])
                return int(values[0]) if values else None

            reference_depth = FineGrainedLongTermConfig().reranker.rerank_depth
            for value in (context.anchor.config.get("longterm_rerank_depth"), nested_reranker.get("rerank_depth")):
                candidate_depth = legal_depth(value)
                if candidate_depth is not None:
                    reference_depth = candidate_depth
                    break
            depths = sorted(
                int(value)
                for value in production_strategy_candidates(
                    "longterm_rerank_depth", settings.get("rerank_depth") or [reference_depth]
                )
            )

            if context.generation_round == 1:
                allow_network = context.budget == "deep"
                model_limit = int(
                    settings.get("max_reranker_models_deep" if allow_network else "max_reranker_models_standard") or 1
                )
                models = context.model_discovery.discover(
                    model_type="reranker",
                    allow_network=allow_network,
                    general_limit=model_limit,
                    finance_limit=1 if allow_network else 0,
                )
                for model in models[:model_limit]:
                    model = context.model_discovery.ensure_available(model, allow_download=allow_network)
                    model = context.model_discovery.smoke_test(
                        model,
                        device=str(model.resource_usage.get("preferred_device") or "cpu"),
                    )
                    if model.status != "SMOKE_PASSED":
                        continue
                    fine_reranker_candidates.append(
                        _candidate(
                            context,
                            branch=self.spec.name,
                            label=f"reranker={model.model_id.replace('/', '--')}@{reference_depth}",
                            cost_level="high",
                            complexity=3,
                            provenance={
                                "model_discovery": model.serializable(),
                                "provenance_validated": True,
                            },
                            longterm_reranker_method="cross_encoder",
                            longterm_reranker_model_id=model.model_id,
                            longterm_reranker_model_revision=model.revision,
                            longterm_reranker_model_path=model.local_path,
                            longterm_reranker_inference_device=str(
                                model.resource_usage.get("preferred_device") or "cuda"
                            ),
                            longterm_reranker_inference_precision=model.resource_usage.get("inference_precision"),
                            longterm_reranker_inference_batch_size=int(
                                model.resource_usage.get("inference_batch_size") or 1
                            ),
                            longterm_rerank_depth=reference_depth,
                        )
                    )
            else:
                previous_round = context.branch_history[-1] if context.branch_history else {}
                model_id = context.anchor.config.get("longterm_reranker_model_id")
                model_revision = context.anchor.config.get("longterm_reranker_model_revision")
                model_path = context.anchor.config.get("longterm_reranker_model_path")
                model_provenance = context.anchor.provenance.get("model_discovery")
                current_depth = legal_depth(context.anchor.config.get("longterm_rerank_depth"))
                valid_winner = (
                    context.anchor.config.get("experiment_branch") == self.spec.name
                    and previous_round.get("generation_round") == context.generation_round - 1
                    and bool(previous_round.get("frontier_winner"))
                    and previous_round.get("best_candidate") == context.anchor.name
                    and context.anchor.config.get("longterm_reranker_method") == "cross_encoder"
                    and bool(model_id)
                    and bool(model_revision)
                    and bool(model_path)
                    and current_depth is not None
                    and isinstance(model_provenance, Mapping)
                    and model_provenance.get("model_id") == model_id
                    and model_provenance.get("revision") == model_revision
                    and model_provenance.get("local_path") == model_path
                )
                if valid_winner:
                    for depth in depths:
                        if depth == current_depth:
                            continue
                        fine_reranker_candidates.append(
                            _candidate(
                                context,
                                branch=self.spec.name,
                                label=f"reranker={str(model_id).replace('/', '--')}@{depth}",
                                cost_level="high",
                                complexity=1,
                                longterm_rerank_depth=depth,
                            )
                        )
        if fine_reranker_candidates:
            # Reranker model/depth screening must survive the branch-wide
            # candidate cap while the other Long-term axes remain available.
            groups.insert(0, fine_reranker_candidates)

        # Put one candidate from every high-level Long-term axis into the
        # minimum screen before spending depth on any one axis.
        candidates = []
        for offset in range(max(len(group) for group in groups)):
            for group in groups:
                if offset < len(group):
                    candidate = group[offset]
                    changed = {
                        key: value for key, value in candidate.config.items() if context.anchor.config.get(key) != value
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
    base_source_spec = dict(context.anchor.config.get("source_generation_spec") or {})
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
            "parent_source_identity": base_source_spec.get("source_identity"),
            "invalidates": {
                "session_merge_prompt": ["midterm_sessions", "promotion", "cross_session_longterm"],
                "fine_grained_longterm_extraction_prompt": ["fine_grained_longterm", "all_memory_context"],
                "session_longterm_extraction_prompt": ["session_longterm", "all_memory_context"],
            }.get(prompt_field, ["midterm_pages", "midterm_sessions", "downstream_memory"]),
        }
        source_root = context.registry.cache_root / "production_variants" / stable_hash(identity)
        source_spec = {
            **base_source_spec,
            "dataset_path": context.dataset.path,
            "dataset_sha256": context.dataset.sha256,
            "session_turn_counts": session_turn_counts,
            "memory_config_path": str(memory_config_path.resolve()),
            "llm_mode": str(manifest.get("llm_mode") or "real"),
            "page_summary_prompt": None,
            "session_merge_prompt": None,
            "fine_grained_longterm_extraction_prompt": None,
            "session_longterm_extraction_prompt": None,
            "page_context_contract": "production_previous_current_following_raw_v1",
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
            "parent": "production/P0-reference-resolution",
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


class FineGrainedLongtermExtractionPromptBranch(SourcePromptBranch):
    spec = BranchSpec(
        name="FineGrainedLongtermExtractionPrompt",
        diagnostic_regimes=frozenset({"candidate_coverage_bottleneck", "balanced_or_plateau"}),
        cost_level="expensive",
        required_artifacts=("production_longterm_outputs", "fine_grained_longterm_prompt"),
        execution_adapter="ProductionGeneratedSourceAdapter",
        provenance_contract=("dataset_sha256", "fine_grained_longterm_prompt_hash", "manifest_sha256"),
        resource_requirements={"llm": True, "embedding": True, "network": True},
        priority=33,
    )

    def generate(self, context: BranchContext) -> BranchOutcome:
        return _split_source_prompt_outcome(
            context,
            branch_spec=self.spec,
            prompt_field="fine_grained_longterm_extraction_prompt",
            variants=controlled_fine_grained_longterm_prompt_variants(),
        )


# Historical imports remain valid, but the default registry and new artifacts
# use the Fine-grained contract names above.
SessionLongtermRetrievalBranch = FineGrainedLongtermRetrievalBranch
SessionLongtermExtractionPromptBranch = FineGrainedLongtermExtractionPromptBranch


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
        return [
            {
                **branch.spec.serializable(),
                "requires_source_regeneration": bool(getattr(branch, "requires_source_regeneration", False)),
            }
            for branch in self.ordered()
        ]

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
        needs_embedding = bool(branch.spec.resource_requirements.get("embedding"))
        needs_reranker = branch.spec.name == "Reranking"
        if (needs_embedding or needs_reranker) and int(context.execution_settings.get("gpu_count") or 0) <= 0:
            return BranchOutcome(
                branch.spec.name,
                "UNAVAILABLE_NO_GPU",
                reason="GPU_REQUIRED_NO_GPU: skipped embedding/reranker Branch; CPU fallback is disabled",
            )
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
            requires_source_regeneration = bool(getattr(branch, "requires_source_regeneration", False))
            candidate.provenance["requires_source_regeneration"] = requires_source_regeneration
            if requires_source_regeneration:
                source_spec = candidate.config.get("source_generation_spec")
                if not isinstance(source_spec, Mapping) or not isinstance(source_spec.get("source_identity"), Mapping):
                    invalid.append(f"{candidate.name}: source-regenerating Branch did not create a source identity")
                    continue
                anchor_spec = context.anchor.config.get("source_generation_spec")
                anchor_identity = anchor_spec.get("source_identity") if isinstance(anchor_spec, Mapping) else None
                if anchor_identity is not None and stable_hash(source_spec["source_identity"]) == stable_hash(
                    anchor_identity
                ):
                    invalid.append(f"{candidate.name}: source-regenerating Branch reused its parent source identity")
                    continue
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
        AgenticRetrievalBranch(),
        HybridRetrievalBranch(),
        QueryRepresentationBranch(),
        RerankingBranch(),
        EmbeddingBranch(),
        FieldAwareMultiVectorBranch(),
        PageRepresentationBranch(),
        MidtermEvolutionBranch(),
        MidtermSourceConfigBranch(),
        FineGrainedLongtermRetrievalBranch(),
        QueryRewritePromptBranch(),
        MidtermPageSummaryPromptBranch(),
        MidtermSessionMergePromptBranch(),
        FineGrainedLongtermExtractionPromptBranch(),
    ]
