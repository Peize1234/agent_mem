from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").exists())
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

from tuner.artifact_registry import ArtifactRegistry  # noqa: E402
from tuner.experiment_branches import (  # noqa: E402
    ALL_REGIMES,
    BranchContext,
    BranchOutcome,
    BranchRegistry,
    BranchSpec,
)
from tuner.models import Candidate, CandidateResult, Dataset, Turn  # noqa: E402
from tuner.research_decision import ResearchDecisionEngine  # noqa: E402
from tuner.research_evidence import ResearchEvidence, build_research_evidence  # noqa: E402
from tuner.research_policy import LegalAction  # noqa: E402
from tuner.staged_search import run_staged_search  # noqa: E402


class FakeRuntime:
    model_config = {"provider": "fake", "api_key": "must-redact"}
    model_config_hash = "fake-model-config"
    secrets = ("must-redact",)

    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    def generate_response(self, messages: list[dict[str, str]], **_: Any) -> Any:
        self.calls += 1
        self.messages.append(messages)
        if not self.responses:
            raise AssertionError("unexpected Research LLM call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def action(action_id: str, branch: str, *, required: bool = False) -> LegalAction:
    return LegalAction(
        action_id=action_id,
        action_type="branch",
        branch=branch,
        generation_round=1,
        coverage_class="required" if required else "selectable",
        cost_level="medium",
        reason="test",
        required_now=required,
    )


def evidence(value: str = "one") -> ResearchEvidence:
    payload = {"schema": "research_tune_evidence_v1", "tune_marker": value}
    return ResearchEvidence(payload=payload, evidence_hash=f"evidence-{value}")


def engine(tmp_path: Path, runtime: FakeRuntime, *, cache: Path | None = None) -> ResearchDecisionEngine:
    registry = ArtifactRegistry(cache or tmp_path / "cache", tmp_path / "legacy")
    return ResearchDecisionEngine(
        runtime=runtime,
        artifact_registry=registry,
        trace_path=tmp_path / "research_trace.jsonl",
        max_attempts=3,
    )


def decide(
    decision_engine: ResearchDecisionEngine,
    *,
    evidence_value: str = "one",
    actions: list[LegalAction] | None = None,
) -> Any:
    legal = actions or [action("A01", "BranchA"), action("A02", "BranchB")]
    return decision_engine.decide(
        stage_index=2,
        evidence=evidence(evidence_value),
        legal_actions=legal,
        deterministic_plan=[legal[0].serializable()],
        max_actions=1,
        identity_context={
            "dataset_sha256": "d" * 64,
            "tune_scope": ["S001"],
            "anchor_candidate_hash": "anchor",
            "branch_registry_state_hash": "registry",
        },
    )


def test_invalid_json_and_illegal_action_retry_then_succeed_with_complete_trace(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        [
            "not-json",
            json.dumps({"action_ids": ["A99"], "rationale": "bad", "deprioritized": []}),
            json.dumps(
                {
                    "action_ids": ["A02"],
                    "rationale": "deep recall suggests BranchB",
                    "deprioritized": [{"action_id": "A01", "reason": "lower expected information gain"}],
                }
            ),
        ]
    )
    decision_engine = engine(tmp_path, runtime)
    result = decide(decision_engine)

    assert [item.action_id for item in result.selected_actions] == ["A02"]
    assert runtime.calls == 3
    assert decision_engine.stats.serializable() == {
        "llm_calls": 3,
        "successful_calls": 1,
        "failed_calls": 2,
        "decisions": 1,
        "fallback_decisions": 0,
        "cache_hits": 0,
    }
    trace = [json.loads(line) for line in (tmp_path / "research_trace.jsonl").read_text().splitlines()]
    assert [row["event"] for row in trace] == ["REQUEST", "RESPONSE"] * 3
    assert all(row["system_prompt"] for row in trace)
    assert all(row["user_evidence_prompt"] for row in trace)
    assert trace[1]["raw_response"] == "not-json"
    assert trace[1]["error"].startswith("ResearchDecisionValidationError: invalid JSON")
    assert trace[3]["parsed_response"]["action_ids"] == ["A99"]
    assert trace[-1]["final_validated_actions"][0]["action_id"] == "A02"
    assert "previous_validation_error" in runtime.messages[1][-1]["content"]
    assert "previous_validation_error" in runtime.messages[2][-1]["content"]
    assert "must-redact" not in json.dumps(trace)


def test_three_failures_fall_back_to_exact_deterministic_plan(tmp_path: Path) -> None:
    runtime = FakeRuntime([TimeoutError("timeout must-redact"), RuntimeError("rate limit"), ""])
    decision_engine = engine(tmp_path, runtime)
    result = decide(decision_engine)

    assert result.fallback_used is True
    assert [item.action_id for item in result.selected_actions] == ["A01"]
    assert result.rationale.endswith("registry.select() unchanged.")
    assert decision_engine.stats.llm_calls == 3
    assert decision_engine.stats.failed_calls == 3
    assert decision_engine.stats.fallback_decisions == 1
    trace = [json.loads(line) for line in (tmp_path / "research_trace.jsonl").read_text().splitlines()]
    assert trace[-1]["event"] == "FALLBACK"
    assert trace[-1]["status"] == "DETERMINISTIC_FALLBACK"
    assert trace[-1]["final_validated_actions"][0]["branch"] == "BranchA"
    assert "must-redact" not in json.dumps(trace)


def test_first_attempt_api_failure_second_attempt_succeeds(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        [
            TimeoutError("first attempt timeout"),
            json.dumps({"action_ids": ["A01"], "rationale": "corrected", "deprioritized": []}),
        ]
    )
    decision_engine = engine(tmp_path, runtime)
    result = decide(decision_engine)
    assert result.attempt_count == 2
    assert result.fallback_used is False
    assert decision_engine.stats.llm_calls == 2
    assert decision_engine.stats.failed_calls == 1
    assert decision_engine.stats.successful_calls == 1


def test_required_action_cannot_be_deprioritized(tmp_path: Path) -> None:
    legal = [action("A01", "RetrievalControl", required=True), action("A02", "BranchB")]
    runtime = FakeRuntime(
        [
            json.dumps(
                {
                    "action_ids": ["A02"],
                    "rationale": "try to skip required",
                    "deprioritized": [{"action_id": "A01", "reason": "skip"}],
                }
            ),
            json.dumps(
                {
                    "action_ids": ["A01"],
                    "rationale": "required action retained",
                    "deprioritized": [{"action_id": "A02", "reason": "later"}],
                }
            ),
        ]
    )
    decision_engine = engine(tmp_path, runtime)
    result = decide(decision_engine, actions=legal)
    assert [item.branch for item in result.selected_actions] == ["RetrievalControl"]
    assert runtime.calls == 2
    assert "required actions were omitted" in runtime.messages[1][-1]["content"]


def test_python_legal_stop_action_must_be_selected_alone(tmp_path: Path) -> None:
    stop = LegalAction(
        action_id="A02",
        action_type="stop",
        branch=None,
        generation_round=None,
        coverage_class="policy_stop",
        cost_level=None,
        reason="patience reached after required coverage",
        stop_reason="frontier_converged",
    )
    legal = [action("A01", "BranchA"), stop]
    runtime = FakeRuntime(
        [
            json.dumps(
                {
                    "action_ids": ["A01", "A02"],
                    "rationale": "invalid mixed stop",
                    "deprioritized": [],
                }
            ),
            json.dumps(
                {
                    "action_ids": ["A02"],
                    "rationale": "frontier is converged",
                    "deprioritized": [{"action_id": "A01", "reason": "no expected gain"}],
                }
            ),
        ]
    )
    result = decide(engine(tmp_path, runtime), actions=legal)
    assert result.stop_action == stop
    assert runtime.calls == 2


def test_identical_decision_cache_hit_does_not_call_llm(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    first_runtime = FakeRuntime(
        [json.dumps({"action_ids": ["A01"], "rationale": "cached choice", "deprioritized": []})]
    )
    first = decide(engine(tmp_path, first_runtime, cache=cache))
    second_runtime = FakeRuntime([])
    second_engine = engine(tmp_path, second_runtime, cache=cache)
    second = decide(second_engine)

    assert first.decision_id == second.decision_id
    assert second.cache_hit is True
    assert second_runtime.calls == 0
    assert second_engine.stats.cache_hits == 1
    assert second_engine.stats.llm_calls == 0
    trace = [json.loads(line) for line in (tmp_path / "research_trace.jsonl").read_text().splitlines()]
    assert trace[-1]["event"] == "CACHE_HIT"


def test_evidence_change_recomputes_decision(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        [
            json.dumps({"action_ids": ["A01"], "rationale": "first", "deprioritized": []}),
            json.dumps({"action_ids": ["A02"], "rationale": "new evidence", "deprioritized": []}),
        ]
    )
    decision_engine = engine(tmp_path, runtime)
    first = decide(decision_engine, evidence_value="one")
    second = decide(decision_engine, evidence_value="two")
    assert first.decision_id != second.decision_id
    assert runtime.calls == 2
    assert second.cache_hit is False


def candidate_result(name: str, recall: float, *, config: dict[str, Any] | None = None) -> CandidateResult:
    return CandidateResult(
        name=name,
        candidate_hash=name,
        stage="tune",
        config=config or {},
        metrics={
            "recall_at_k": recall,
            "recall_at_2k": recall,
            "recall_at_4k": recall,
            "mrr": recall,
            "macro_session_recall_at_k": recall,
            "session_stddev": 0.0,
            "worst_session_recall_at_k": recall,
        },
        requirement_rows=[],
        session_rows=[],
        runtime_seconds=0.0,
        work_seconds=0.0,
        cache_hits=0,
        cache_misses=0,
    )


def test_research_evidence_excludes_validation_gold_answers_and_future_data(tmp_path: Path) -> None:
    del tmp_path
    baseline = Candidate(
        "baseline",
        "baseline",
        {"backend": "production_midterm", "validation_secret": "DO_NOT_LEAK"},
    )
    baseline_result = candidate_result("baseline", 0.4)
    baseline_result.requirement_rows = [
        {"session_id": "S001", "hit_at_k": False, "hit_at_2k": True, "hit_at_4k": True},
        {
            "session_id": "S999_VALIDATION",
            "hit_at_k": False,
            "gold": "VALIDATION_GOLD_SECRET",
            "answer": "VALIDATION_ANSWER_SECRET",
        },
    ]
    built = build_research_evidence(
        dataset_sha256="d" * 64,
        tune_sessions=["S001"],
        k=5,
        stage_index=2,
        baseline=baseline,
        baseline_result=baseline_result,
        anchor=baseline,
        anchor_result=baseline_result,
        candidates={"baseline": baseline},
        tune_results=[baseline_result],
        stage_history=[{"stage_index": 1, "validation_failure": "VALIDATION_STAGE_SECRET"}],
        diagnostic={"regime": "candidate_coverage_bottleneck"},
        coverage={"relevant_branches": ["BranchA"]},
        deterministic_plan=[{"action_id": "A01", "branch": "BranchA"}],
        legal_actions=[{"action_id": "A01", "branch": "BranchA"}],
        budget_state={"budget": "deep"},
        deprioritized_history=[],
    )
    serialized = json.dumps(built.payload, ensure_ascii=False)
    assert "DO_NOT_LEAK" not in serialized
    assert "VALIDATION_GOLD_SECRET" not in serialized
    assert "VALIDATION_ANSWER_SECRET" not in serialized
    assert "VALIDATION_STAGE_SECRET" not in serialized
    assert "S999_VALIDATION" not in serialized
    assert built.payload["data_boundary"]["tune_session_ids"] == ["S001"]


class SearchBranch:
    def __init__(self, name: str, *, priority: int, gain: float, initial: bool = False):
        self.gain = gain
        self.spec = BranchSpec(
            name=name,
            diagnostic_regimes=ALL_REGIMES,
            cost_level="cheap" if initial else "medium",
            required_artifacts=("checkpoint",),
            execution_adapter="test",
            provenance_contract=("dataset",),
            resource_requirements={},
            priority=priority,
            initial_stage=initial,
        )

    def generate(self, context: BranchContext) -> BranchOutcome:
        candidate = Candidate(
            name=f"{self.spec.name}-round-{context.generation_round}",
            stage=f"stage-{context.stage_index}",
            config={
                **context.anchor.config,
                "gain": self.gain,
                "experiment_branch": self.spec.name,
                "branch_cost_level": self.spec.cost_level,
            },
        )
        return BranchOutcome(self.spec.name, "READY", [candidate])

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, None]:
        del candidate, context
        return True, None


def search_dataset(tmp_path: Path) -> Dataset:
    session = "S001_test"
    turns = tuple(
        Turn(session, "S001", index, f"S001-Q{index + 1:03d}", f"q{index}", f"a{index}", "", ()) for index in range(4)
    )
    return Dataset(str(tmp_path / "dataset.xlsx"), "d" * 64, {session: turns})


def run_research_search(tmp_path: Path, runtime: FakeRuntime, *, max_stages: int = 3) -> Any:
    dataset = search_dataset(tmp_path)
    baseline = Candidate("baseline", "baseline", {"gain": 0.0})
    branches = [
        SearchBranch("RetrievalControl", priority=10, gain=0.05, initial=True),
        SearchBranch("BranchA", priority=20, gain=0.12),
        SearchBranch("BranchB", priority=30, gain=0.10),
    ]
    registry = BranchRegistry(branches)
    search_space = {
        "selection": {"min_improvement_pp": 0.25, "patience_stages": 2},
        "search": {
            "branch_registry": {
                "RetrievalControl": {"max_rounds": 1},
                "BranchA": {"max_rounds": 1},
                "BranchB": {"max_rounds": 1},
            },
            "branch_coverage": {
                "ranking_bottleneck": {
                    "relevant": {
                        "RetrievalControl": {
                            "priority": 10,
                            "minimum_attempts": 1,
                            "coverage_class": "required",
                        },
                        "BranchA": {
                            "priority": 20,
                            "minimum_attempts": 0,
                            "coverage_class": "selectable",
                        },
                        "BranchB": {
                            "priority": 30,
                            "minimum_attempts": 0,
                            "coverage_class": "selectable",
                        },
                    }
                }
            },
        },
    }

    def evaluate(candidates: Any, sessions: Any, scope: str) -> list[CandidateResult]:
        del sessions, scope
        return [candidate_result(candidate.name, 0.4 + float(candidate.config["gain"])) for candidate in candidates]

    decision_engine = engine(tmp_path, runtime)
    return run_staged_search(
        dataset=dataset,
        baseline=baseline,
        baseline_result=candidate_result("baseline", 0.4),
        tune_sessions=("S001_test",),
        registry=registry,
        artifact_registry=None,
        model_discovery=None,
        run_dir=tmp_path,
        search_space=search_space,
        budget="standard",
        profile={
            "max_stages": max_stages,
            "max_cost_level": "medium",
            "max_branches_per_stage": 1,
            "max_candidates_per_stage": 4,
            "tune_frontier": 4,
            "max_expensive_candidates": 0,
        },
        k=5,
        ranking_depth=20,
        evaluate=evaluate,
        diagnose=lambda _: {"regime": "ranking_bottleneck"},
        research_decider=decision_engine,
    )


def test_stage_one_is_deterministic_and_stage_two_uses_research_llm(tmp_path: Path) -> None:
    runtime = FakeRuntime(
        [
            json.dumps(
                {
                    "action_ids": ["A02"],
                    "rationale": "BranchB is more diagnostic",
                    "deprioritized": [{"action_id": "A01", "reason": "try after evidence changes"}],
                }
            ),
            json.dumps({"action_ids": ["A01"], "rationale": "now revisit BranchA", "deprioritized": []}),
        ]
    )
    search = run_research_search(tmp_path, runtime)
    assert search.stage_history[0]["branches"] == ["RetrievalControl"]
    assert search.stage_history[0]["research_decision"] is None
    assert search.stage_history[1]["branches"] == ["BranchB"]
    assert search.stage_history[2]["branches"] == ["BranchA"]
    assert runtime.calls == 2
    assert any(item["branch"] == "BranchA" and item["status"] == "DEPRIORITIZED" for item in search.skipped_branches)
    assert not any(
        item["branch"] == "BranchA"
        for item in search.coverage_audit["exhausted_branches"]
        if item["reason"].startswith("DEPRIORITIZED")
    )
    assert [record["stage_index"] for record in search.research_decisions] == [2, 3]


def test_staged_research_three_failures_use_registry_select_result(tmp_path: Path) -> None:
    runtime = FakeRuntime([RuntimeError("api") for _ in range(3)])
    search = run_research_search(tmp_path, runtime, max_stages=2)
    assert search.stage_history[0]["branches"] == ["RetrievalControl"]
    assert search.stage_history[1]["branches"] == ["BranchA"]
    assert runtime.calls == 3
    assert search.research_stats["fallback_decisions"] == 1
    research_event = next(event for event in search.branch_events if event["branch"] == "__research_decision__")
    assert research_event["status"] == "DETERMINISTIC_FALLBACK"


def test_deterministic_mode_does_not_require_research_runtime(tmp_path: Path) -> None:
    dataset = search_dataset(tmp_path)
    baseline = Candidate("baseline", "baseline", {"gain": 0.0})
    branch = SearchBranch("RetrievalControl", priority=1, gain=0.05, initial=True)

    def evaluate(candidates: Any, sessions: Any, scope: str) -> list[CandidateResult]:
        del sessions, scope
        return [candidate_result(candidate.name, 0.45) for candidate in candidates]

    search = run_staged_search(
        dataset=dataset,
        baseline=baseline,
        baseline_result=candidate_result("baseline", 0.4),
        tune_sessions=("S001_test",),
        registry=BranchRegistry([branch]),
        artifact_registry=None,
        model_discovery=None,
        run_dir=tmp_path,
        search_space={"selection": {"patience_stages": 1}},
        budget="quick",
        profile={"max_stages": 1, "max_cost_level": "medium", "max_candidates_per_stage": 2},
        k=5,
        ranking_depth=20,
        evaluate=evaluate,
        diagnose=lambda _: {"regime": "balanced_or_plateau"},
    )
    assert search.research_decisions == []
    assert search.research_stats == {}
