from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook


SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").exists())
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

from tuner.artifact_registry import ArtifactRegistry  # noqa: E402
from tuner.benchmark_support import load_dataset, parse_gold_requirements  # noqa: E402
from tuner.build_report import write_outputs  # noqa: E402
from tuner.candidate_selector import classify_overfit, select_best  # noqa: E402
from tuner.dataset_audit import DatasetAuditFailed, audit_dataset  # noqa: E402
from tuner.evaluate_candidate import (  # noqa: E402
    _eligible_requirements,
    _evaluate_session,
    candidate_hash,
    evaluate_candidate,
)
from tuner.experiment_branches import (  # noqa: E402
    ALL_REGIMES,
    BranchContext,
    BranchOutcome,
    BranchRegistry,
    BranchSpec,
    MemoryWriteAddPromptBranch,
    QueryRepresentationBranch,
    RerankingBranch,
    _derived_dimensions,
)
from tuner.generated_source_artifacts import prepare_generated_source_candidate  # noqa: E402
from tuner.encoding_contract import EncodingContract, SentenceTransformerEncodingAdapter  # noqa: E402
from tuner.io_utils import sha256_file  # noqa: E402
from tuner.model_discovery import (  # noqa: E402
    ModelCandidate,
    ModelDiscovery,
    ResourceEnvelope,
    _annotate_relative_benchmark_scores,
    _metadata_evidence,
)
from tuner.models import Candidate, CandidateResult, Dataset, Requirement, Turn  # noqa: E402
import tuner.orchestrator as orchestrator  # noqa: E402
import tuner.production_midterm_adapter as production_adapter  # noqa: E402
from tuner.orchestrator import (  # noqa: E402
    TunerConfig,
    _artifact_cache_root,
    _evaluate_loso_many,
    _resolve_shortterm_window,
)
from tuner.production_midterm_adapter import (  # noqa: E402
    ProductionMidtermAdapter,
    generate_production_sources,
    isolated_runtime_layout,
    production_candidate_from_manifests,
)
from tuner.split_sessions import create_or_load_split  # noqa: E402
from tuner.prompt_artifacts import controlled_query_prompt_variants  # noqa: E402
from tuner.staged_search import candidate_config_hash, run_staged_search  # noqa: E402


def make_turn(session: str, index: int, gold: tuple[Requirement, ...] = ()) -> Turn:
    code = session.split("_", 1)[0]
    return Turn(
        session_id=session,
        session_code=code,
        turn_index=index,
        query_id=f"{code}-Q{index + 1:03d}",
        question=f"问题 {index + 1} 指标 {index % 2}",
        answer=f"答案 {index + 1} 财务 指标 {index % 2}",
        gold_raw="",
        requirements=gold,
    )


def make_dataset(tmp_path: Path, session_count: int = 2) -> Dataset:
    sessions = {}
    for session_index in range(session_count):
        session = f"S{session_index + 1:03d}_测试"
        code = session.split("_", 1)[0]
        turns = [make_turn(session, index) for index in range(4)]
        turns.append(make_turn(session, 4, (Requirement((f"{code}-Q001",), f"{code}-Q001"),)))
        sessions[session] = tuple(turns)
    return Dataset(path=str(tmp_path / "synthetic.xlsx"), sha256="a" * 64, sessions=sessions)


def result(name: str, recall: float, *, macro: float | None = None, stddev: float = 0.0) -> CandidateResult:
    return CandidateResult(
        name=name,
        candidate_hash=name,
        stage="test",
        config={"name": name},
        metrics={
            "recall_at_k": recall,
            "macro_session_recall_at_k": recall if macro is None else macro,
            "session_stddev": stddev,
            "mrr": recall,
            "recall_at_2k": recall,
            "recall_at_4k": recall,
        },
        requirement_rows=[],
        session_rows=[],
        runtime_seconds=0.1,
        work_seconds=0.1,
        cache_hits=0,
        cache_misses=1,
    )


def write_workbook(path: Path, sessions: dict[str, list[tuple[str, str]]]) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sessions.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(["编号", "当前问题", "最终回答", "是否需要前文", "关联前序对话", "所需前文信息", "依赖类型"])
        for query_id, gold in rows:
            sheet.append([query_id, f"问题 {query_id}", f"答案 {query_id}", bool(gold), gold, "", ""])
    workbook.save(path)


def test_gold_and_or_semantics() -> None:
    requirements = parse_gold_requirements("S001-Q001；（S001-Q002；S001-Q003）")
    assert len(requirements) == 2
    assert not requirements[0].is_or
    assert requirements[1].is_or
    assert requirements[1].hit_by(["S001-Q003"])


@pytest.mark.parametrize(("k", "expected"), [(5, False), (10, True)])
def test_evaluator_parameterizes_k(tmp_path: Path, k: int, expected: bool) -> None:
    dataset = make_dataset(tmp_path, 1)
    session = next(iter(dataset.sessions))
    target = dataset.sessions[session][-1]
    ranking = [
        {"source_turn_id": f"S001-Q{index:03d}", "page_id": f"S001-Q{index:03d}"} for index in (2, 3, 4, 8, 9, 10, 1)
    ]
    evaluated = _evaluate_session(
        dataset, session, {target.query_id: ranking}, k=k, target="midterm", shortterm_window=3
    )
    assert evaluated["requirements"][0]["hit_at_k"] is expected


@pytest.mark.parametrize(
    ("count", "method", "validation_count"),
    [
        (8, "deterministic_stratified_holdout", 2),
        (6, "deterministic_holdout", 2),
        (4, "leave_one_session_out", 0),
        (2, "exploratory_only", 0),
    ],
)
def test_session_split_policy(tmp_path: Path, count: int, method: str, validation_count: int) -> None:
    dataset = make_dataset(tmp_path, count)
    output = tmp_path / f"split-{count}"
    split = create_or_load_split(dataset, output_dir=output, seed=42, shortterm_window=3)
    assert split["method"] == method
    assert len(split["validation_sessions"]) == validation_count
    assert not set(split["tune_sessions"]) & set(split["validation_sessions"])
    if method == "leave_one_session_out":
        assert len(split["folds"]) == count
        assert sorted(fold["validation_sessions"][0] for fold in split["folds"]) == sorted(dataset.sessions)
        assert all(len(fold["tune_sessions"]) == count - 1 for fold in split["folds"])
    assert create_or_load_split(dataset, output_dir=output, seed=42, shortterm_window=3) == split


def test_shortterm_window_is_derived_and_mismatch_is_detected() -> None:
    actual, validation = _resolve_shortterm_window(
        {"dataset": {"shortterm_qa_turns": 2}},
        {"midterm": {"short_term_capacity": 6}},
    )
    assert actual == 3
    assert validation == {
        "status": "OVERRIDDEN_BY_PRODUCTION_CONFIG",
        "dataset_config_qa_turns": 2,
        "production_capacity_messages": 6,
        "actual_qa_turns": 3,
        "source": "memory_config.midterm.short_term_capacity / 2",
    }
    with pytest.raises(ValueError, match="positive even message count"):
        _resolve_shortterm_window(
            {"dataset": {"shortterm_qa_turns": 3}},
            {"midterm": {"short_term_capacity": 5}},
        )


def test_resume_derives_cache_from_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "custom-output" / "run-id"
    resumed = TunerConfig(
        dataset=tmp_path / "dataset.xlsx",
        output_root=tmp_path / "unrelated-default",
        resume=run_dir,
    )
    fresh = TunerConfig(dataset=tmp_path / "dataset.xlsx", output_root=tmp_path / "fresh-output")
    assert _artifact_cache_root(resumed, run_dir) == tmp_path / "custom-output" / ".cache"
    assert (
        _artifact_cache_root(fresh, tmp_path / "fresh-output" / "run")
        == (tmp_path / "fresh-output" / ".cache").resolve()
    )


def test_loso_executes_every_frozen_fold(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 3)
    split = create_or_load_split(dataset, output_dir=tmp_path / "split", seed=42, shortterm_window=3)
    ranking_path = tmp_path / "rankings.jsonl"
    ranking_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "query_id": turns[-1].query_id,
                    "source_turn_id": turns[0].query_id,
                    "rank": 1,
                    "score": 1.0,
                }
            )
            for turns in dataset.sessions.values()
        )
        + "\n",
        encoding="utf-8",
    )
    candidate = Candidate(
        name="baseline",
        stage="baseline",
        config={"backend": "frozen_ranking", "ranking_path": str(ranking_path)},
    )
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    trace_path = tmp_path / "search_trace.jsonl"
    trace_path.touch()
    common = {
        "dataset": dataset,
        "candidates": [candidate],
        "folds": split["folds"],
        "config": TunerConfig(dataset=Path(dataset.path), k=5),
        "shortterm_window": 3,
        "ranking_depth": 20,
        "registry": registry,
        "run_dir": tmp_path / "run",
        "execution": {"max_parallel_candidates": 2, "max_parallel_sessions": 2, "adaptive_reductions": []},
        "trace_path": trace_path,
    }
    tune = _evaluate_loso_many(
        partition="tune_sessions",
        scope="tune_loso",
        **common,
    )[0]
    validation = _evaluate_loso_many(
        partition="validation_sessions",
        scope="validation_loso",
        **common,
    )[0]

    assert len(tune.session_rows) == 6
    assert len(validation.session_rows) == 3
    assert sorted(row["session_id"] for row in validation.session_rows) == sorted(dataset.sessions)
    scopes = [json.loads(line)["scope"] for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert {scope for scope in scopes if scope.startswith("validation_loso")} == {
        "validation_loso_fold_1",
        "validation_loso_fold_2",
        "validation_loso_fold_3",
    }


def test_loso_prunes_invalid_candidate_without_aborting(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 3)
    split = create_or_load_split(dataset, output_dir=tmp_path / "split-invalid", seed=42, shortterm_window=3)
    candidate = Candidate(name="invalid", stage="test", config={"backend": "not-supported"})
    trace = tmp_path / "invalid-trace.jsonl"
    trace.touch()
    combined = _evaluate_loso_many(
        dataset,
        [candidate],
        split["folds"],
        partition="tune_sessions",
        scope="invalid_loso",
        config=TunerConfig(dataset=Path(dataset.path), k=5),
        shortterm_window=3,
        ranking_depth=20,
        registry=ArtifactRegistry(tmp_path / "invalid-cache", tmp_path / "results"),
        run_dir=tmp_path / "invalid-run",
        execution={"max_parallel_candidates": 1, "max_parallel_sessions": 2, "adaptive_reductions": []},
        trace_path=trace,
    )
    assert len(combined) == 1
    assert combined[0].status == "INVALID"
    assert combined[0].metrics["recall_at_k"] == 0.0


def test_audit_future_dependency_and_cross_session(tmp_path: Path) -> None:
    future = tmp_path / "future.xlsx"
    write_workbook(future, {"S001_x": [("S001-Q001", "S001-Q002"), ("S001-Q002", "")]})
    with pytest.raises(DatasetAuditFailed):
        audit_dataset(
            future,
            output_dir=tmp_path / "future-output",
            shortterm_qa_turns=3,
            warning_config={},
        )
    audit = json.loads((tmp_path / "future-output/dataset_audit.json").read_text(encoding="utf-8"))
    assert audit["status"] == "DATASET_AUDIT_FAILED"
    assert any(error["code"] in {"FUTURE_DEPENDENCY", "REPOSITORY_SCHEMA_VALIDATION"} for error in audit["hard_errors"])

    cross = tmp_path / "cross.xlsx"
    write_workbook(
        cross,
        {
            "S001_x": [("S001-Q001", ""), ("S001-Q002", "S002-Q001")],
            "S002_x": [("S002-Q001", "")],
        },
    )
    with pytest.raises(DatasetAuditFailed):
        audit_dataset(
            cross,
            output_dir=tmp_path / "cross-output",
            shortterm_qa_turns=3,
            warning_config={},
        )
    audit = json.loads((tmp_path / "cross-output/dataset_audit.json").read_text(encoding="utf-8"))
    assert audit["cross_session_dependency_count"] == 1


def test_auxiliary_sheet_is_not_a_session(tmp_path: Path) -> None:
    dataset_path = tmp_path / "with-helper.xlsx"
    write_workbook(dataset_path, {"S001_x": [("S001-Q001", ""), ("S001-Q002", "S001-Q001")]})
    workbook = load_workbook(dataset_path)
    helper = workbook.create_sheet("Dataset_Quality_Audit")
    helper.append(["编号", "当前问题", "最终回答", "是否需要前文", "关联前序对话"])
    helper.append(["CHECK-1", "检查项", "这不是 Session", False, ""])
    workbook.save(dataset_path)
    workbook.close()

    repository_sessions = load_dataset(dataset_path)
    assert [session.session_id for session in repository_sessions] == ["S001_x"]
    dataset, audit = audit_dataset(
        dataset_path,
        output_dir=tmp_path / "audit-helper",
        shortterm_qa_turns=3,
        warning_config={},
    )
    assert list(dataset.sessions) == ["S001_x"]
    assert audit["session_count"] == 1


def test_audit_accepts_complete_production_midterm_checkpoint_run(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.xlsx"
    write_workbook(
        dataset_path,
        {
            "S001_x": [
                ("S001-Q001", ""),
                ("S001-Q002", ""),
                ("S001-Q003", ""),
                ("S001-Q004", ""),
                ("S001-Q005", "S001-Q001"),
            ]
        },
    )
    source = tmp_path / "source/S001"
    source.mkdir(parents=True)
    checkpoints = source / "production_midterm_checkpoints.jsonl"
    checkpoints.write_text(json.dumps({"query_id": "S001-Q005"}) + "\n", encoding="utf-8")
    (source / "production_midterm_manifest.json").write_text(
        json.dumps(
            {
                "status": "COMPLETE",
                "dataset_sha256": sha256_file(dataset_path),
                "session_id": "S001_x",
                "turn_count": 5,
                "failed_turns": 0,
                "checkpoints_path": str(checkpoints),
                "checkpoints_sha256": sha256_file(checkpoints),
            }
        ),
        encoding="utf-8",
    )
    _, audit = audit_dataset(
        dataset_path,
        output_dir=tmp_path / "audit",
        shortterm_qa_turns=3,
        warning_config={
            "explicit_history_target_leakage_ratio": 1.1,
            "single_distance_concentration_ratio": 1.1,
            "repeated_question_template_ratio": 1.1,
        },
        source_run=source.parent,
    )
    assert audit["source_run"]["status"] == "COMPLETE"
    assert audit["source_run"]["kind"] == "production_midterm_checkpoints"


def test_true_midterm_requirement_eligibility_for_or_gold(tmp_path: Path) -> None:
    session = "S001_test"
    turns = [make_turn(session, index) for index in range(5)]
    target = make_turn(
        session,
        5,
        (
            Requirement(("S001-Q001",), "S001-Q001"),
            Requirement(("S001-Q001", "S001-Q004"), "(S001-Q001；S001-Q004)"),
        ),
    )
    turns.append(target)
    eligible = _eligible_requirements(target, turns, "midterm", shortterm_window=3)
    assert eligible == [Requirement(("S001-Q001",), "S001-Q001")]


def test_production_adapter_manifest_and_runtime_isolation(tmp_path: Path) -> None:
    manifests = []
    for index, session in enumerate(("S001_test", "S002_test"), start=1):
        checkpoints = tmp_path / session / "checkpoints.jsonl"
        checkpoints.parent.mkdir(parents=True)
        checkpoints.write_text("", encoding="utf-8")
        manifest = checkpoints.parent / "production_midterm_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "COMPLETE",
                    "backend": "production_midterm",
                    "session_id": session,
                    "production_config": {
                        "top_k_sessions": 5,
                        "top_k_pages": 5,
                        "max_total_pages": 5,
                    },
                    "effective_memory_config": {
                        "vector_store": {"config": {"bm25_language": "zh"}},
                    },
                    "checkpoints_path": str(checkpoints),
                    "failed_turns": 0,
                    "llm_calls": index,
                    "embedding_calls": index * 10,
                }
            ),
            encoding="utf-8",
        )
        manifests.append(manifest)
    config, provenance = production_candidate_from_manifests(manifests)
    assert config["backend"] == "production_midterm"
    assert config["retrieval_method"] == "dense"
    assert config["bm25_language"] == "zh"
    assert provenance["source"] == "real AsyncMemory Add/MidTerm pipeline"
    assert provenance["llm_calls"] == 3
    assert provenance["embedding_calls"] == 30
    ProductionMidtermAdapter.supported(config)
    with pytest.raises(ValueError, match="regenerated production artifacts"):
        ProductionMidtermAdapter.supported({**config, "page_representation": "summary"})

    first = isolated_runtime_layout(tmp_path, candidate_hash="candidate-a", session_id="S001")
    second = isolated_runtime_layout(tmp_path, candidate_hash="candidate-b", session_id="S001")
    third = isolated_runtime_layout(tmp_path, candidate_hash="candidate-a", session_id="S002")
    assert len({first.root, second.root, third.root}) == 3
    assert all(layout.sqlite_path.exists() for layout in (first, second, third))


def test_production_source_workers_respect_llm_concurrency_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_path = tmp_path / "dataset.xlsx"
    dataset_path.write_bytes(b"dataset")
    memory_config = tmp_path / "memory_config.json"
    memory_config.write_text("{}", encoding="utf-8")
    lock = threading.Lock()
    active = 0
    max_active = 0

    def fake_run(command: list[str], **_: object) -> object:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            spec = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
            output_dir = Path(spec["output_dir"])
            checkpoints = output_dir / "production_midterm_checkpoints.jsonl"
            checkpoints.write_text("", encoding="utf-8")
            (output_dir / "production_midterm_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETE",
                        "turn_count": 1,
                        "failed_turns": 0,
                        "checkpoints_path": str(checkpoints),
                    }
                ),
                encoding="utf-8",
            )
        finally:
            with lock:
                active -= 1
        return production_adapter.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(production_adapter.subprocess, "run", fake_run)
    session_ids = [f"S{index:03d}_test" for index in range(1, 5)]
    paths = generate_production_sources(
        dataset_path=dataset_path,
        dataset_sha256="dataset-sha",
        session_ids=session_ids,
        session_turn_counts={session_id: 1 for session_id in session_ids},
        memory_config_path=memory_config,
        run_dir=tmp_path / "run",
        ranking_depth=20,
        llm_mode="real",
        max_parallel_sessions=4,
        max_parallel_llm_calls=2,
    )
    assert len(paths) == 4
    assert 1 < max_active <= 2


def test_production_adapter_calls_real_midterm_retriever(tmp_path: Path) -> None:
    session_point_id = "11111111-1111-1111-1111-111111111111"
    page_point_id = "22222222-2222-2222-2222-222222222222"
    scope = {"user_id": "recall::S001_test", "run_id": "S001_test"}
    checkpoint = {
        "query_id": "S001-Q005",
        "query": "真实生产检索查询",
        "filters": scope,
        "query_vector": [1.0, 0.0],
        "sessions": [
            {
                "id": session_point_id,
                "vector": [1.0, 0.0],
                "payload": {
                    **scope,
                    "summary": "生产 Session 摘要",
                    "summary_keywords": ["生产"],
                    "data": "生产 Session 摘要\nKeywords: 生产",
                    "output_state": "committed",
                    "page_ids": [page_point_id],
                },
            }
        ],
        "pages": [
            {
                "id": page_point_id,
                "vector": [1.0, 0.0],
                "payload": {
                    **scope,
                    "session_id": session_point_id,
                    "source_job_id": "job-1",
                    "summary": "真实生产检索查询的 Page prompt 摘要",
                    "keywords": ["生产", "摘要"],
                    "user_input": "源问题",
                    "data": "真实生产检索查询的 Page prompt 摘要\nKeywords: 生产, 摘要\nUser: 源问题",
                    "text_lemmatized": "真实生产检索查询 生产 page 摘要",
                    "output_state": "committed",
                },
            }
        ],
        "source_turn_ids_by_job": {"job-1": ["S001-Q001"]},
    }
    config = {
        "backend": "production_midterm",
        "retrieval_method": "dense",
        "query_representation": "original",
        "page_representation": "production",
        "top_k_sessions": 1,
        "top_k_pages": 1,
        "max_total_pages": 1,
    }
    adapter = ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="candidate",
        session_id="S001_test",
        ranking_depth=20,
    )
    assert adapter.rank(checkpoint, config) == [
        {
            "page_id": page_point_id,
            "source_turn_id": "S001-Q001",
            "source": "mid_term_page",
            "score": pytest.approx(1.0),
            "rank": 1,
        }
    ]

    hybrid = ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="candidate-hybrid",
        session_id="S001_test",
        ranking_depth=20,
    )
    hybrid_ranking = hybrid.rank(
        checkpoint,
        {
            **config,
            "retrieval_method": "dense_bm25_fusion",
            "dense_weight": 0.7,
            "bm25_language": "zh",
        },
    )
    assert hybrid_ranking[0]["source_turn_id"] == "S001-Q001"
    assert hybrid_ranking[0]["source"] == "mid_term_page"


def test_candidate_selection_near_tie_and_overfit() -> None:
    baseline_tune = result("baseline", 0.50)
    baseline_validation = result("baseline", 0.50)
    complex_tune = result("complex", 0.60)
    complex_validation = result("complex", 0.49)
    assert classify_overfit(
        complex_tune,
        complex_validation,
        baseline_tune,
        baseline_validation,
        validation_regression_pp=-0.5,
    )
    stable_tune = result("stable", 0.505, macro=0.51)
    stable_validation = result("stable", 0.501, macro=0.51)
    stable_validation.complexity = 0
    best, overfit, _ = select_best(
        {"baseline": baseline_tune, "complex": complex_tune, "stable": stable_tune},
        {"baseline": baseline_validation, "complex": complex_validation, "stable": stable_validation},
        baseline_name="baseline",
        tie_tolerance_pp=0.25,
        overfit_regression_pp=-0.5,
    )
    assert best.name == "stable"
    assert overfit == ["complex"]


def test_report_marks_missing_full_memory_trace_as_na(tmp_path: Path) -> None:
    baseline = result("baseline", 0.5)
    baseline.metrics["midterm_recall_at_k"] = 0.5
    reason = "No complete production trace is available"
    write_outputs(
        run_dir=tmp_path,
        dataset_audit={
            "dataset": "dataset.xlsx",
            "dataset_sha256": "sha",
            "session_count": 1,
            "query_count": 1,
            "gold_requirement_count": 1,
            "status": "OK",
            "warnings": [],
        },
        split={"method": "exploratory_only", "confidence": "exploratory", "tune_sessions": ["S001"]},
        k=5,
        baseline_tune=baseline,
        baseline_validation=baseline,
        tune_results=[baseline],
        validation_by_name={"baseline": baseline},
        best=baseline,
        selection={},
        overfit=[],
        skipped_branches=[],
        diagnostics={},
        stop_reason="test",
        run_metadata={
            "shortterm_qa_turns": 3,
            "shortterm_window_validation": {"status": "MATCH"},
            "full_memory_regression": {
                "status": "SKIPPED",
                "backend": None,
                "reason": reason,
                "metrics": None,
            },
            "midterm_baseline_backend": "production_midterm",
            "full_memory_regression_baseline_backend": None,
        },
    )
    report = (tmp_path / "final_report.md").read_text(encoding="utf-8")
    assert "Regression ShortTerm coverage: N/A" in report
    assert "Regression LongTerm R@5: N/A" in report
    assert reason in report


def test_ranking_cache_reuse_across_k(tmp_path: Path) -> None:
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    identity = {"dataset": "x", "candidate": "y"}
    registry.store_ranking(identity, [{"query_id": "q", "page_id": "p"}], depth=40)
    assert registry.get_ranking(identity, required_depth=20) is not None
    assert registry.get_ranking(identity, required_depth=40) is not None
    assert registry.get_ranking(identity, required_depth=41) is None


def test_production_trace_discovery_checks_completeness(tmp_path: Path) -> None:
    dataset_path = tmp_path / "dataset.xlsx"
    dataset_path.write_bytes(b"dataset")
    from tuner.io_utils import sha256_file

    dataset_sha256 = sha256_file(dataset_path)
    result_dir = tmp_path / "results/run/S001"
    result_dir.mkdir(parents=True)
    trace_path = result_dir / "recall_turn_results.jsonl"
    trace_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "session_id": "S001_test",
                    "turn_id": f"S001-Q{index:03d}",
                    "error": None,
                    "mid_retrieved_turn_ids": [],
                }
            )
            for index in (1, 2)
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "dataset": str(dataset_path),
        "sessions": [{"result_dir": str(result_dir), "failed_turns": 0}],
    }
    (tmp_path / "results/run/isolated_run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    candidate = registry.discover_production_trace(
        dataset_path=dataset_path,
        dataset_sha256=dataset_sha256,
        session_query_counts={"S001_test": 2},
    )
    assert candidate is not None
    assert candidate.config["backend"] == "production_trace"
    assert candidate.provenance["failed_turns"] == 0
    assert (
        registry.discover_production_trace(
            dataset_path=dataset_path,
            dataset_sha256=dataset_sha256,
            session_query_counts={"S001_test": 3},
        )
        is None
    )


def test_trace_does_not_replace_midterm_checkpoints_and_regression_is_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset_path = tmp_path / "dataset.xlsx"
    sessions = {}
    for index in range(1, 9):
        code = f"S{index:03d}"
        sessions[f"{code}_test"] = [
            (f"{code}-Q001", ""),
            (f"{code}-Q002", ""),
            (f"{code}-Q003", ""),
            (f"{code}-Q004", ""),
            (f"{code}-Q005", f"{code}-Q001"),
        ]
    write_workbook(dataset_path, sessions)
    memory_config = tmp_path / "memory_config.json"
    memory_config.write_text(
        json.dumps(
            {
                "midterm": {
                    "enabled": True,
                    "short_term_capacity": 6,
                    "top_k_sessions": 5,
                    "top_k_pages": 5,
                    "max_total_pages": 5,
                }
            }
        ),
        encoding="utf-8",
    )
    trace_candidate = Candidate(
        name="baseline",
        stage="baseline",
        config={"backend": "production_trace", "trace_paths": ["trace.jsonl"]},
        provenance={"trace_sha256": {"trace.jsonl": "trace-sha"}},
    )
    generated = False
    observed: list[tuple[str, str, str, dict[str, object]]] = []
    manifest_path = tmp_path / "manifest.json"

    monkeypatch.setattr(ArtifactRegistry, "discover_production_midterm", lambda self, **kwargs: None)
    monkeypatch.setattr(
        ArtifactRegistry,
        "discover_production_trace",
        lambda self, **kwargs: trace_candidate,
    )
    monkeypatch.setattr(ArtifactRegistry, "discover_frozen_rankings", lambda self, *args, **kwargs: [])

    def fake_generate(**_: object) -> list[Path]:
        nonlocal generated
        generated = True
        manifest_path.write_text("{}", encoding="utf-8")
        return [manifest_path]

    monkeypatch.setattr(orchestrator, "generate_production_sources", fake_generate)
    monkeypatch.setattr(
        orchestrator,
        "production_candidate_from_manifests",
        lambda paths: (
            {
                "backend": "production_midterm",
                "retrieval_method": "dense",
                "top_k_sessions": 5,
                "top_k_pages": 5,
                "max_total_pages": 5,
                "manifest_paths": [str(manifest_path.resolve())],
                "manifest_sha256": {str(manifest_path.resolve()): sha256_file(manifest_path)},
            },
            {"llm_calls": 2, "embedding_calls": 11, "source": "test production source"},
        ),
    )
    monkeypatch.setattr(orchestrator, "_gpu_count", lambda: 0)
    monkeypatch.setattr(orchestrator, "_memory_gib", lambda: 16.0)

    def fake_evaluate_many(
        dataset: Dataset,
        candidates: list[Candidate] | tuple[Candidate, ...],
        session_ids: list[str] | tuple[str, ...],
        *,
        scope: str,
        **_: object,
    ) -> list[CandidateResult]:
        values = []
        for candidate in candidates:
            backend = str(candidate.config.get("backend"))
            observed.append((scope, candidate.name, backend, dict(candidate.config)))
            if backend == "production_trace":
                recall = 0.99
            elif candidate.config.get("top_k_sessions") == 6:
                recall = 0.70
            else:
                recall = 0.50
            metrics = {
                "evaluated_query_count": len(session_ids),
                "eligible_requirement_count": len(session_ids),
                "recall_at_k": recall,
                "recall_at_2k": recall,
                "recall_at_4k": recall,
                "macro_session_recall_at_k": recall,
                "session_stddev": 0.0,
                "worst_session_recall_at_k": recall,
                "mrr": recall,
                "midterm_recall_at_k": recall,
                "shortterm_coverage": 0.25,
                "target_layer_union": 0.60,
                "all_memory_union": None,
                "query_completion": None,
            }
            if backend == "production_trace":
                metrics.update(
                    {
                        "longterm_recall_at_k": 0.40,
                        "all_memory_recall_at_k": 0.80,
                        "all_memory_union": 0.80,
                        "query_completion": 0.80,
                    }
                )
            values.append(
                CandidateResult(
                    name=candidate.name,
                    candidate_hash=candidate.name,
                    stage=candidate.stage,
                    config=candidate.config,
                    metrics=metrics,
                    requirement_rows=[],
                    session_rows=[],
                    runtime_seconds=0.01,
                    work_seconds=0.01,
                    cache_hits=0,
                    cache_misses=len(session_ids),
                    complexity=candidate.complexity,
                )
            )
        return values

    monkeypatch.setattr(orchestrator, "_evaluate_many", fake_evaluate_many)
    run_dir = orchestrator.run_tuning(
        TunerConfig(
            dataset=dataset_path,
            budget="quick",
            output_root=tmp_path / "results",
            memory_config=memory_config,
            max_parallel_sessions=4,
            max_parallel_llm_calls=2,
        ),
        skill_root=SCRIPTS.parent,
    )

    metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
    best = json.loads((run_dir / "best_config.json").read_text(encoding="utf-8"))
    report = (run_dir / "final_report.md").read_text(encoding="utf-8")
    assert generated
    assert metadata["midterm_baseline_backend"] == "production_midterm"
    assert metadata["full_memory_regression_baseline_backend"] == "production_trace"
    assert metadata["source_generation_llm_calls"] == 2
    assert metadata["source_generation_embedding_calls"] == 11
    assert metadata["llm_calls"] == 2
    assert metadata["embedding_calls"] == 11
    assert metadata["execution"]["source_worker_parallelism"] == 2
    assert metadata["shortterm_qa_turns"] == 3
    assert best["candidate"].startswith("RetrievalControl:top_k_sessions=6")
    assert best["config"]["top_k_sessions"] == 6
    assert best["validation_metrics"]["recall_at_k"] == pytest.approx(0.70)
    cheap_configs = [value for scope, _, _, value in observed if scope == "stage_1_tune"]
    assert any(value.get("top_k_sessions") != 5 for value in cheap_configs)
    assert any(value.get("top_k_pages") != 5 for value in cheap_configs)
    assert any(value.get("max_total_pages") != 5 for value in cheap_configs)
    assert all(scope == "full_memory_regression" for scope, _, backend, _ in observed if backend == "production_trace")
    assert metadata["full_memory_regression"]["metrics"]["longterm_recall_at_k"] == 0.40
    assert "Regression LongTerm R@5: 0.4000" in report
    assert "Regression All-memory R@5 / query completion: 0.8000 / 0.8000" in report


def test_frozen_query_artifact_discovery_validates_dataset_and_original_query(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 1)
    results = tmp_path / "results/query_run"
    results.mkdir(parents=True)
    (results / "run_metadata.json").write_text(
        json.dumps({"dataset_sha256": dataset.sha256}),
        encoding="utf-8",
    )
    target = dataset.sessions["S001_测试"][-1]
    artifact = results / "resolved_queries.jsonl"
    artifact.write_text(
        json.dumps(
            {
                "query_id": target.query_id,
                "original_query": target.question,
                "resolved_query": f"已消解：{target.question}",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    base = Candidate(
        name="baseline",
        stage="baseline",
        config={"backend": "production_midterm", "query_representation": "original"},
    )
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    discovered = registry.discover_frozen_queries(
        dataset.sha256,
        query_text_by_id={turn.query_id: turn.question for turns in dataset.sessions.values() for turn in turns},
        base_candidate=base,
    )
    assert len(discovered) == 1
    assert discovered[0].config["query_representation"] == "bounded_reference_resolution"
    assert discovered[0].provenance["matched_query_count"] == 1

    artifact.write_text(
        json.dumps(
            {
                "query_id": target.query_id,
                "original_query": "不属于当前数据集的问题",
                "resolved_query": target.question,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    assert not registry.discover_frozen_queries(
        dataset.sha256,
        query_text_by_id={target.query_id: target.question},
        base_candidate=base,
    )


def test_parallel_session_candidate_isolation_and_resume(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 2)
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    run_dir = tmp_path / "run"
    ranking_path = tmp_path / "frozen_rankings.jsonl"
    ranking_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "query_id": turns[-1].query_id,
                    "source_turn_id": turns[0].query_id,
                    "rank": 1,
                    "score": 1.0,
                }
            )
            for turns in dataset.sessions.values()
        )
        + "\n",
        encoding="utf-8",
    )
    baseline = Candidate(
        name="baseline",
        stage="baseline",
        config={
            "backend": "frozen_ranking",
            "ranking_path": str(ranking_path),
            "max_total_pages": 20,
        },
    )
    alternative = Candidate(
        name="alternative",
        stage="cheap",
        config={**baseline.config, "max_total_pages": 5},
    )
    evaluate_candidate(
        dataset,
        baseline,
        list(dataset.sessions),
        scope="tune",
        k=5,
        target="midterm",
        shortterm_window=3,
        ranking_depth=20,
        max_parallel_sessions=2,
        registry=registry,
        run_dir=run_dir,
    )
    second = evaluate_candidate(
        dataset,
        baseline,
        list(dataset.sessions),
        scope="tune",
        k=5,
        target="midterm",
        shortterm_window=3,
        ranking_depth=20,
        max_parallel_sessions=2,
        registry=registry,
        run_dir=run_dir,
    )
    assert second.cache_hits == 2
    baseline_hash = candidate_hash(dataset.sha256, baseline)
    alternative_hash = candidate_hash(dataset.sha256, alternative)
    assert baseline_hash != alternative_hash
    worker_scope = "tune__k5__midterm__short3"
    assert len(list((run_dir / "workers" / baseline_hash / worker_scope).glob("*.json"))) == 2

    interrupted = registry.worker_path(run_dir, alternative_hash, worker_scope, next(iter(dataset.sessions)))
    interrupted.parent.mkdir(parents=True, exist_ok=True)
    interrupted.write_text('{"status": "RUNNING"}', encoding="utf-8")
    evaluated = evaluate_candidate(
        dataset,
        alternative,
        list(dataset.sessions),
        scope="tune",
        k=5,
        target="midterm",
        shortterm_window=3,
        ranking_depth=20,
        max_parallel_sessions=2,
        registry=registry,
        run_dir=run_dir,
    )
    assert evaluated.metrics["eligible_requirement_count"] == 2
    assert json.loads(interrupted.read_text(encoding="utf-8"))["status"] == "COMPLETE"


def test_k_change_reuses_deep_raw_ranking_cache(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 1)
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    turns = next(iter(dataset.sessions.values()))
    ranking_path = tmp_path / "frozen_rankings.jsonl"
    ranking_path.write_text(
        json.dumps(
            {
                "query_id": turns[-1].query_id,
                "source_turn_id": turns[0].query_id,
                "rank": 1,
                "score": 1.0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    base_config = {
        "backend": "frozen_ranking",
        "ranking_path": str(ranking_path),
        "max_total_pages": 20,
    }
    baseline = Candidate(name="baseline", stage="baseline", config=base_config)
    same_ranking = Candidate(name="same-ranking", stage="cheap", config=base_config)
    common = {
        "dataset": dataset,
        "sessions": list(dataset.sessions),
        "scope": "tune",
        "k": 5,
        "target": "midterm",
        "shortterm_window": 3,
        "ranking_depth": 20,
        "max_parallel_sessions": 1,
        "registry": registry,
        "run_dir": tmp_path / "run",
    }
    evaluate_candidate(candidate=baseline, **common)
    reused = evaluate_candidate(candidate=same_ranking, **{**common, "k": 10})
    assert reused.cache_hits == 1
    assert reused.cache_misses == 0


def test_branch_registry_exposes_required_experiment_contracts() -> None:
    registry = BranchRegistry()
    descriptions = {item["name"]: item for item in registry.describe()}
    assert {
        "RetrievalControl",
        "QueryRepresentation",
        "PageRepresentation",
        "HybridRetrieval",
        "Reranking",
        "Embedding",
        "FieldAwareMultiVector",
        "MemoryWriteAddPrompt",
    } <= set(descriptions)
    for item in descriptions.values():
        assert item["diagnostic_regimes"]
        assert item["cost_level"] in {"cheap", "medium", "high", "expensive"}
        assert item["required_artifacts"]
        assert item["execution_adapter"]
        assert item["provenance_contract"]
        assert isinstance(item["resource_requirements"], dict)


class _StaticBranch:
    def __init__(self, name: str, *, initial: bool, priority: int, gain: float):
        self.gain = gain
        self.spec = BranchSpec(
            name=name,
            diagnostic_regimes=ALL_REGIMES,
            cost_level="cheap" if initial else "medium",
            required_artifacts=("checkpoint",),
            execution_adapter="test",
            provenance_contract=("dataset_sha256",),
            resource_requirements={},
            priority=priority,
            initial_stage=initial,
        )

    def generate(self, context: Any) -> BranchOutcome:
        candidate = Candidate(
            name=self.spec.name.lower(),
            stage=f"stage_{context.stage_index}",
            config={**context.anchor.config, "gain": self.gain, "branch_cost_level": self.spec.cost_level},
        )
        return BranchOutcome(self.spec.name, "READY", [candidate])

    def validate_provenance(self, candidate: Candidate, context: Any) -> tuple[bool, None]:
        del candidate, context
        return True, None


def test_staged_loop_rediagnoses_and_never_uses_validation(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 3)
    baseline = Candidate(name="baseline", stage="baseline", config={"backend": "production_midterm", "gain": 0.0})

    def measured(candidate: Candidate) -> CandidateResult:
        recall = 0.40 + float(candidate.config.get("gain") or 0.0)
        value = result(candidate.name, recall)
        value.candidate_hash = candidate.name
        value.stage = candidate.stage
        value.config = candidate.config
        value.metrics["recall_at_4k"] = min(1.0, recall + 0.30)
        return value

    baseline_result = measured(baseline)
    scopes: list[str] = []

    def evaluate(
        candidates: list[Candidate] | tuple[Candidate, ...], sessions: Any, scope: str
    ) -> list[CandidateResult]:
        del sessions
        scopes.append(scope)
        return [measured(candidate) for candidate in candidates]

    diagnoses: list[str] = []

    def diagnose(candidate_result: CandidateResult) -> dict[str, Any]:
        diagnoses.append(candidate_result.name)
        return {
            "regime": "ranking_bottleneck",
            "recall_4k_minus_k_pp": 30.0,
            "recall_4k_percent": 80.0,
            "stddev_pp": 0.0,
        }

    registry = BranchRegistry(
        [
            _StaticBranch("Cheap", initial=True, priority=1, gain=0.10),
            _StaticBranch("Secondary", initial=False, priority=2, gain=0.15),
        ]
    )
    search = run_staged_search(
        dataset=dataset,
        baseline=baseline,
        baseline_result=baseline_result,
        tune_sessions=tuple(dataset.sessions),
        registry=registry,
        artifact_registry=None,
        model_discovery=None,
        run_dir=tmp_path,
        search_space={"selection": {"tie_tolerance_pp": 0.25, "min_improvement_pp": 0.25, "patience_stages": 2}},
        budget="quick",
        profile={
            "max_stages": 4,
            "max_cost_level": "medium",
            "max_branches_per_stage": 1,
            "max_candidates_per_stage": 3,
            "validation_frontier": 2,
        },
        k=5,
        ranking_depth=20,
        evaluate=evaluate,
        diagnose=diagnose,
    )
    assert [stage["branches"] for stage in search.stage_history] == [["Cheap"], ["Secondary"]]
    assert len(search.diagnostics) == 3
    assert diagnoses == ["baseline", "cheap", "secondary"]
    assert scopes == ["stage_1_tune", "stage_2_tune"]
    assert search.stop_reason == "no_applicable_branch_within_resource_budget"
    assert search.frontier[0].name == "secondary"


def _model_index(*, name: str, task: str, dataset: str, score: float) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "results": [
                {
                    "task": {"type": task},
                    "dataset": {"name": dataset},
                    "metrics": [{"type": "ndcg_at_10", "value": score}],
                }
            ],
        }
    ]


@pytest.mark.parametrize(
    ("model_type", "local_id", "online_id", "task", "dataset", "tags"),
    [
        (
            "embedding",
            "local/zh-embedding",
            "dynamic/high-zh-embedding",
            "Retrieval",
            "T2Retrieval",
            ["sentence-similarity", "zh"],
        ),
        (
            "reranker",
            "local/zh-reranker",
            "dynamic/high-zh-reranker",
            "Reranking",
            "T2Reranking",
            ["text-ranking", "zh", "reranker"],
        ),
    ],
)
def test_deep_model_discovery_unifies_cached_and_online_quality_ranking(
    tmp_path: Path,
    model_type: str,
    local_id: str,
    online_id: str,
    task: str,
    dataset: str,
    tags: list[str],
) -> None:
    cache = tmp_path / "hf" / "hub"
    snapshot = cache / f"models--{local_id.replace('/', '--')}" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "README.md").write_text(
        "---\n"
        + json.dumps(
            {
                "license": "apache-2.0",
                "tags": tags,
                "model-index": _model_index(name="C-MTEB", task=task, dataset=dataset, score=0.3),
            },
            ensure_ascii=False,
        )
        + "\n---\n",
        encoding="utf-8",
    )

    class FakeApi:
        def list_models(self, **kwargs: Any) -> list[Any]:
            return [
                SimpleNamespace(
                    modelId=online_id,
                    tags=tags,
                    sha="online-revision",
                    downloads=100,
                    cardData={
                        "license": "mit",
                        "model-index": _model_index(name="C-MTEB", task=task, dataset=dataset, score=0.8),
                    },
                    safetensors={"total": 20_000_000},
                )
            ]

    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 16.0, 100.0),
        cache_root=cache,
        api=FakeApi(),
    )
    candidates = discovery.discover(
        model_type=model_type,
        allow_network=True,
        general_limit=1,
        finance_limit=0,
    )
    assert [candidate.model_id for candidate in candidates] == [online_id]
    assert candidates[0].cache_status == "NOT_CHECKED"
    assert "cache affects download cost only" in candidates[0].selection_reason
    assert candidates[0].metadata_evidence["benchmark_scores"] == [
        {
            "benchmark": "C-MTEB",
            "task": task,
            "dataset": dataset,
            "metric": "ndcg_at_10",
            "score": 0.8,
        }
    ]


def test_standard_model_discovery_is_cache_only(tmp_path: Path) -> None:
    cache = tmp_path / "hf" / "hub"
    snapshot = cache / "models--local--zh-embedding" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    class OfflineApi:
        def list_models(self, **kwargs: Any) -> list[Any]:
            raise AssertionError(f"standard discovery must not call the network: {kwargs}")

    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 16.0, 100.0),
        cache_root=cache,
        api=OfflineApi(),
    )
    candidates = discovery.discover(
        model_type="embedding",
        allow_network=False,
        general_limit=1,
        finance_limit=0,
    )
    assert [candidate.model_id for candidate in candidates] == ["local/zh-embedding"]
    assert candidates[0].cache_status == "CACHED"


def test_model_discovery_deduplicates_exact_cached_online_revision(tmp_path: Path) -> None:
    cache = tmp_path / "hf" / "hub"
    snapshot = cache / "models--shared--zh-embedding" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)

    class FakeApi:
        def list_models(self, **_: Any) -> list[Any]:
            return [
                SimpleNamespace(
                    modelId="shared/zh-embedding",
                    tags=["sentence-similarity", "zh"],
                    sha="abc123",
                    downloads=10,
                    cardData={
                        "license": "apache-2.0",
                        "model-index": _model_index(
                            name="C-MTEB",
                            task="Retrieval",
                            dataset="T2Retrieval",
                            score=0.7,
                        ),
                    },
                )
            ]

    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 16.0, 100.0),
        cache_root=cache,
        api=FakeApi(),
    )
    candidates = discovery.discover(
        model_type="embedding",
        allow_network=True,
        general_limit=2,
        finance_limit=0,
    )
    assert len(candidates) == 1
    assert candidates[0].revision == "abc123"
    assert candidates[0].cache_status == "CACHED"
    assert set(candidates[0].source.split("+")) == {"huggingface_search", "local_huggingface_cache"}


def test_model_benchmark_scores_are_structured_and_compared_only_like_for_like() -> None:
    def candidate(model_id: str, benchmark: str, dataset: str, score: float) -> ModelCandidate:
        evidence = _metadata_evidence(
            {
                "tags": ["sentence-similarity", "zh"],
                "cardData": {
                    "model-index": _model_index(
                        name=benchmark,
                        task="Retrieval",
                        dataset=dataset,
                        score=score,
                    )
                },
            }
        )
        return ModelCandidate(model_id, "embedding", "test", revision="revision", metadata_evidence=evidence)

    low = candidate("test/c-mteb-low", "C-MTEB", "T2Retrieval", 0.6)
    high = candidate("test/c-mteb-high", "C-MTEB", "T2Retrieval", 75.0)
    finance = candidate("test/finmteb", "FinMTEB", "FinQA", 99.0)
    _annotate_relative_benchmark_scores([low, high, finance])

    assert low.metadata_evidence["benchmark_scores"][0] == {
        "benchmark": "C-MTEB",
        "task": "Retrieval",
        "dataset": "T2Retrieval",
        "metric": "ndcg_at_10",
        "score": 0.6,
    }
    assert low.metadata_evidence["benchmark_comparisons"][0]["normalized_score"] == pytest.approx(0.6)
    assert high.metadata_evidence["benchmark_comparisons"][0]["normalized_score"] == pytest.approx(0.75)
    assert high.metadata_evidence["benchmark_comparisons"][0]["relative_percentile"] == pytest.approx(1.0)
    assert finance.metadata_evidence["benchmark_comparisons"] == []


def test_exact_cached_revision_reuses_snapshot_without_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "hf" / "hub" / "models--cached--embedding" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 16.0, 100.0),
        cache_root=tmp_path / "hf" / "hub",
    )

    def unexpected_download(**_: Any) -> str:
        raise AssertionError("an exact cached model_id + revision must not call snapshot_download")

    monkeypatch.setattr("huggingface_hub.snapshot_download", unexpected_download)
    available = discovery.ensure_available(
        ModelCandidate(
            "cached/embedding",
            "embedding",
            "local_huggingface_cache",
            revision="abc123",
            local_path=str(snapshot),
            cache_status="CACHED",
        ),
        allow_download=True,
    )
    assert available.status == "AVAILABLE"
    assert available.resource_usage["cache_reused_without_download"] is True


def test_model_download_failure_is_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 16.0, 100.0),
        cache_root=tmp_path / "hf" / "hub",
    )

    def unavailable(**_: Any) -> str:
        raise PermissionError("gated model")

    monkeypatch.setattr("huggingface_hub.snapshot_download", unavailable)
    failed = discovery.ensure_available(
        ModelCandidate("gated/model", "embedding", "huggingface_search"),
        allow_download=True,
    )
    assert failed.status == "UNAVAILABLE"
    assert "gated model" in str(failed.error)
    payload = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert any(item.get("status") == "UNAVAILABLE" for item in payload["models"])


def test_budget_profiles_gate_real_cost_levels() -> None:
    import yaml

    space = yaml.safe_load((SCRIPTS.parent / "search_space.yaml").read_text(encoding="utf-8"))
    profiles = space["budget"]["profiles"]
    assert profiles["quick"]["max_cost_level"] == "medium"
    assert profiles["standard"]["max_cost_level"] == "high"
    assert profiles["deep"]["max_cost_level"] == "expensive"
    assert profiles["quick"]["max_stages"] < profiles["deep"]["max_stages"]


def _production_branch_inputs(tmp_path: Path, dataset: Dataset) -> tuple[Candidate, Path]:
    memory_config = tmp_path / "memory.json"
    memory_config.write_text(
        json.dumps(
            {
                "llm": {"provider": "mock", "config": {"model": "test-model"}},
                "midterm": {"enabled": True, "short_term_capacity": 6},
            }
        ),
        encoding="utf-8",
    )
    manifests: list[Path] = []
    for session_id, turns in dataset.sessions.items():
        source_dir = tmp_path / "production" / session_id
        source_dir.mkdir(parents=True, exist_ok=True)
        checkpoints = source_dir / "production_midterm_checkpoints.jsonl"
        checkpoint_rows = [
            {
                "query_id": turn.query_id,
                "query": turn.question,
                "query_vector": [1.0, 0.0],
                "filters": {"run_id": session_id},
                "pages": [],
                "sessions": [],
                "source_turn_ids_by_job": {},
            }
            for turn in turns
        ]
        checkpoints.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in checkpoint_rows),
            encoding="utf-8",
        )
        manifest = source_dir / "production_midterm_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "status": "COMPLETE",
                    "dataset_sha256": dataset.sha256,
                    "session_id": session_id,
                    "memory_config_path": str(memory_config),
                    "memory_config_sha256": sha256_file(memory_config),
                    "llm_mode": "mock",
                    "checkpoints_path": str(checkpoints),
                    "checkpoints_sha256": sha256_file(checkpoints),
                    "prompt_hashes": {"page_summary": "production"},
                    "production_config": {"top_k_sessions": 5, "top_k_pages": 5, "max_total_pages": 5},
                    "effective_memory_config": {"vector_store": {"config": {"bm25_language": "en"}}},
                    "failed_turns": 0,
                    "llm_calls": 0,
                    "embedding_calls": 0,
                }
            ),
            encoding="utf-8",
        )
        manifests.append(manifest)
    candidate = Candidate(
        name="baseline",
        stage="baseline",
        config={
            "backend": "production_midterm",
            "retrieval_contract": "production_midterm_v1",
            "retrieval_method": "dense",
            "query_representation": "original",
            "page_representation": "production",
            "top_k_sessions": 5,
            "top_k_pages": 5,
            "max_total_pages": 5,
            "manifest_paths": [str(manifest) for manifest in manifests],
            "manifest_sha256": {str(manifest.resolve()): sha256_file(manifest) for manifest in manifests},
        },
    )
    return candidate, manifests[0]


def _branch_context(
    tmp_path: Path,
    dataset: Dataset,
    baseline: Candidate,
    *,
    budget: str = "standard",
    generation_round: int = 1,
    model_discovery: Any = None,
    search_space: dict[str, Any] | None = None,
) -> BranchContext:
    return BranchContext(
        dataset=dataset,
        baseline=baseline,
        anchor=baseline,
        anchor_result=result("baseline", 0.4),
        diagnostic={
            "regime": "candidate_coverage_bottleneck",
            "recall_4k_minus_k_pp": 30.0,
        },
        search_space=search_space
        or {
            "search": {
                "stages": {
                    "secondary": {
                        "query_representation": {"max_rounds": 3, "variants_per_round": 3},
                        "reranking": {"min_deep_recall_gap_pp": 15.0},
                    },
                    "expensive": {"require_frontier_candidate": True, "max_prompt_candidates": 4},
                }
            }
        },
        budget=budget,
        k=5,
        ranking_depth=20,
        tune_sessions=(next(iter(dataset.sessions)),),
        registry=ArtifactRegistry(tmp_path / "cache", tmp_path / "results"),
        run_dir=tmp_path / "run",
        model_discovery=model_discovery,
        stage_index=generation_round,
        generation_round=generation_round,
        execution_settings={"max_parallel_sessions": 2, "max_parallel_llm_calls": 2},
    )


def test_new_dataset_generates_three_query_variants_and_resumes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tuner.derived_artifacts as derived_artifacts
    from tuner.prompt_artifacts import QueryPromptArtifactGenerator

    dataset = make_dataset(tmp_path, 2)
    baseline, _ = _production_branch_inputs(tmp_path, dataset)

    class FakeEncoder:
        def __init__(self, manifest_path: Path):
            del manifest_path

        def encode(self, texts: list[str], *, action: str) -> list[list[float]]:
            del action
            return [[float(len(text)), 1.0] for text in texts]

    class FakeLLM:
        def __init__(self) -> None:
            self.calls = 0
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def generate_response(self, messages: list[dict[str, str]], **_: Any) -> str:
            payload = json.loads(messages[-1]["content"])
            with self.lock:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.002)
            with self.lock:
                self.active -= 1
            return json.dumps({"resolved_query": f"{payload['current_query']} 已消解"}, ensure_ascii=False)

    llm = FakeLLM()
    monkeypatch.setattr(derived_artifacts, "_ProductionEncoder", FakeEncoder)
    monkeypatch.setattr(QueryPromptArtifactGenerator, "_create_llm", lambda self, config, mode: llm)
    context = _branch_context(tmp_path, dataset, baseline)
    branch = QueryRepresentationBranch()
    first = BranchRegistry([branch]).generate(branch, context)
    assert first.status == "READY"
    assert len(first.candidates) == 3
    assert {item.config["query_prompt_generation_round"] for item in first.candidates} == {1}
    assert first.provenance["analysis_session_ids"] == [context.tune_sessions[0]]
    assert all(Path(item.config["query_artifact_path"]).exists() for item in first.candidates)
    assert llm.max_active <= 2
    calls_after_first = llm.calls

    second = BranchRegistry([branch]).generate(branch, context)
    assert second.status == "READY"
    assert len(second.candidates) == 3
    assert second.llm_calls == 0
    assert llm.calls == calls_after_first
    payload = json.loads(Path(first.candidates[0].config["query_artifact_path"]).read_text(encoding="utf-8"))["payload"]
    assert payload["analysis_session_ids"] == [context.tune_sessions[0]]
    assert set(payload["generated_session_ids"]) == set(dataset.sessions)


def test_query_prompt_variants_are_capped_at_three_rounds_and_three_per_round(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 2)
    baseline = Candidate(name="baseline", stage="baseline", config={"query_representation": "original"})
    baseline_result = result("baseline", 0.4)
    for generation_round in (1, 2, 3):
        variants = controlled_query_prompt_variants(
            dataset=dataset,
            anchor=baseline,
            anchor_result=baseline_result,
            tune_sessions=(next(iter(dataset.sessions)),),
            generation_round=generation_round,
            variants_per_round=99,
        )
        assert len(variants) == 3
        assert {variant.generation_round for variant in variants} == {generation_round}
    assert not controlled_query_prompt_variants(
        dataset=dataset,
        anchor=baseline,
        anchor_result=baseline_result,
        tune_sessions=tuple(dataset.sessions),
        generation_round=4,
        variants_per_round=3,
    )


class _RoundBranch:
    def __init__(self, name: str = "CoarseRefine", *, improve: bool = True):
        self.improve = improve
        self.spec = BranchSpec(
            name=name,
            diagnostic_regimes=ALL_REGIMES,
            cost_level="cheap",
            required_artifacts=("checkpoint",),
            execution_adapter="test",
            provenance_contract=("dataset",),
            resource_requirements={},
            priority=1,
            initial_stage=True,
        )

    def generate(self, context: BranchContext) -> BranchOutcome:
        gain = 0.05 * context.generation_round if self.improve else 0.0
        candidate = Candidate(
            name=f"{self.spec.name}-round-{context.generation_round}",
            stage=f"stage-{context.stage_index}",
            config={
                **context.anchor.config,
                "coarse_refine_round": context.generation_round,
                "gain": gain,
                "branch_cost_level": "cheap",
            },
        )
        return BranchOutcome(self.spec.name, "READY", [candidate])

    def validate_provenance(self, candidate: Candidate, context: BranchContext) -> tuple[bool, None]:
        del candidate, context
        return True, None


def _run_round_search(tmp_path: Path, branch: Any, *, max_rounds: int) -> Any:
    dataset = make_dataset(tmp_path, 3)
    baseline = Candidate(name="baseline", stage="baseline", config={"gain": 0.0})

    def evaluate(candidates: Any, sessions: Any, scope: str) -> list[CandidateResult]:
        del sessions, scope
        return [result(candidate.name, 0.4 + float(candidate.config.get("gain") or 0.0)) for candidate in candidates]

    return run_staged_search(
        dataset=dataset,
        baseline=baseline,
        baseline_result=result("baseline", 0.4),
        tune_sessions=tuple(dataset.sessions),
        registry=BranchRegistry([branch]),
        artifact_registry=None,
        model_discovery=None,
        run_dir=tmp_path,
        search_space={
            "selection": {"min_improvement_pp": 0.25, "patience_stages": 2},
            "search": {"branch_registry": {branch.spec.name: {"enabled": True, "max_rounds": max_rounds}}},
        },
        budget="standard",
        profile={"max_stages": 5, "max_cost_level": "high", "max_candidates_per_stage": 4},
        k=5,
        ranking_depth=20,
        evaluate=evaluate,
        diagnose=lambda _: {"regime": "ranking_bottleneck"},
    )


def test_same_branch_can_reenter_for_coarse_refine(tmp_path: Path) -> None:
    search = _run_round_search(tmp_path, _RoundBranch(), max_rounds=2)
    assert [event["generation_round"] for event in search.branch_events] == [1, 2]
    assert search.frontier[0].name == "CoarseRefine-round-2"


def test_query_branch_stops_early_without_improvement(tmp_path: Path) -> None:
    search = _run_round_search(tmp_path, _RoundBranch("QueryRepresentation", improve=False), max_rounds=3)
    assert [event["generation_round"] for event in search.branch_events] == [1]


def test_query_branch_never_exceeds_three_rounds(tmp_path: Path) -> None:
    search = _run_round_search(tmp_path, _RoundBranch("QueryRepresentation", improve=True), max_rounds=3)
    assert [event["generation_round"] for event in search.branch_events] == [1, 2, 3]


def test_candidate_config_hash_deduplicates_branch_bookkeeping() -> None:
    left = {
        "top_k_pages": 10,
        "experiment_branch": "A",
        "branch_cost_level": "cheap",
        "parent_candidate_hash": "one",
        "applied_branches": ["A"],
    }
    right = {
        "top_k_pages": 10,
        "experiment_branch": "B",
        "branch_cost_level": "high",
        "parent_candidate_hash": "two",
        "applied_branches": ["A", "B"],
    }
    assert candidate_config_hash(left) == candidate_config_hash(right)


def test_deep_memory_write_generates_current_dataset_artifacts_without_frozen_rankings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tuner.generated_source_artifacts as generated_sources

    dataset = make_dataset(tmp_path, 2)
    baseline, _ = _production_branch_inputs(tmp_path, dataset)
    generated_session_sets: list[tuple[str, ...]] = []

    def fake_generate(**kwargs: Any) -> list[Path]:
        generated_session_sets.append(tuple(kwargs["session_ids"]))
        root = Path(kwargs["source_root"])
        paths = []
        for session_id in kwargs["session_ids"]:
            path = root / session_id / "production_midterm_manifest.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "session_id": session_id,
                        "production_config": {"top_k_sessions": 5, "top_k_pages": 5, "max_total_pages": 5},
                        "effective_memory_config": {"vector_store": {"config": {"bm25_language": "en"}}},
                        "failed_turns": 0,
                        "llm_calls": 2,
                        "embedding_calls": 5,
                    }
                ),
                encoding="utf-8",
            )
            paths.append(path)
        kwargs["generation_stats"].update(
            {"generated_sessions": len(paths), "reused_sessions": 0, "llm_calls": 2, "embedding_calls": 5}
        )
        return paths

    monkeypatch.setattr(generated_sources, "generate_production_sources", fake_generate)
    discovery = SimpleNamespace(resources=SimpleNamespace(gpu_count=0))
    context = _branch_context(tmp_path, dataset, baseline, budget="deep", model_discovery=discovery)
    branch = MemoryWriteAddPromptBranch()
    outcome = BranchRegistry([branch]).generate(branch, context)
    assert outcome.status == "READY"
    assert len(outcome.candidates) == 4
    assert {candidate.config["source_variant"] for candidate in outcome.candidates} == {
        "conservative_add",
        "context_aware_add",
        "evidence_focused_summary",
        "diagnostic_controlled_summary",
    }
    assert outcome.llm_calls == 0
    assert outcome.embedding_calls == 0
    assert generated_session_sets == []

    screening_session = next(iter(dataset.sessions))
    prepared = prepare_generated_source_candidate(
        outcome.candidates[0],
        sessions=[screening_session],
        registry=context.registry,
        run_dir=context.run_dir,
        ranking_depth=context.ranking_depth,
        max_parallel_sessions=2,
        max_parallel_llm_calls=2,
        gpu_count=0,
    )
    assert generated_session_sets == [(screening_session,)]
    assert prepared.config["source_variant"] == "conservative_add"
    assert prepared.provenance["deferred_generation_stats"]["llm_calls"] == 2
    assert prepared.provenance["deferred_generation_stats"]["embedding_calls"] == 5
    selected_manifest = next(
        Path(path)
        for path in prepared.config["manifest_paths"]
        if json.loads(Path(path).read_text(encoding="utf-8"))["session_id"] == screening_session
    )
    assert "production_variants" in str(selected_manifest)


def test_later_branch_combines_with_frontier_anchor(tmp_path: Path) -> None:
    class Retrieval(_StaticBranch):
        def generate(self, context: Any) -> BranchOutcome:
            return BranchOutcome(
                self.spec.name,
                "READY",
                [
                    Candidate(
                        name="retrieval-winner",
                        stage="stage-1",
                        config={**context.anchor.config, "top_k_sessions": 6, "gain": 0.1},
                    )
                ],
            )

    class Embedding(_StaticBranch):
        def generate(self, context: Any) -> BranchOutcome:
            return BranchOutcome(
                self.spec.name,
                "READY",
                [
                    Candidate(
                        name="combined-winner",
                        stage="stage-2",
                        config={**context.anchor.config, "embedding_model_id": "local/model", "gain": 0.15},
                    )
                ],
            )

    dataset = make_dataset(tmp_path, 3)
    baseline = Candidate(name="baseline", stage="baseline", config={"top_k_sessions": 5, "gain": 0.0})
    registry = BranchRegistry(
        [
            Retrieval("Retrieval", initial=True, priority=1, gain=0.1),
            Embedding("Embedding", initial=False, priority=2, gain=0.15),
        ]
    )

    def evaluate(candidates: Any, sessions: Any, scope: str) -> list[CandidateResult]:
        del sessions, scope
        return [result(candidate.name, 0.4 + candidate.config["gain"]) for candidate in candidates]

    search = run_staged_search(
        dataset=dataset,
        baseline=baseline,
        baseline_result=result("baseline", 0.4),
        tune_sessions=tuple(dataset.sessions),
        registry=registry,
        artifact_registry=None,
        model_discovery=None,
        run_dir=tmp_path,
        search_space={"selection": {"min_improvement_pp": 0.25, "patience_stages": 2}},
        budget="standard",
        profile={"max_stages": 3, "max_cost_level": "medium", "max_candidates_per_stage": 3},
        k=5,
        ranking_depth=20,
        evaluate=evaluate,
        diagnose=lambda _: {"regime": "ranking_bottleneck"},
    )
    assert search.candidates["combined-winner"].config["top_k_sessions"] == 6


def test_derived_artifact_rebuild_preserves_frontier_dimensions(tmp_path: Path) -> None:
    query_artifact = tmp_path / "queries.json"
    query_artifact.write_text("{}", encoding="utf-8")
    anchor = Candidate(
        name="combined",
        stage="tune",
        config={
            "query_representation": "bounded_reference_resolution",
            "query_artifact_path": str(query_artifact),
            "query_artifact_variant": "generated:v1",
            "page_representation": "summary_keywords",
            "embedding_model_id": "local/embedding",
            "embedding_model_revision": "immutable-revision",
            "embedding_model_path": "/models/embedding",
            "encoding_contract": {"query_prefix": "query: ", "document_prefix": "passage: "},
            "reranker_method": "multi_vector_maxsim",
        },
    )
    dimensions = _derived_dimensions(anchor, page_representation_name="user_summary")
    assert dimensions == {
        "query_representation": "bounded_reference_resolution",
        "query_artifact_path": query_artifact,
        "query_artifact_variant": "generated:v1",
        "page_representation_name": "user_summary",
        "embedding_model_id": "local/embedding",
        "embedding_revision": "immutable-revision",
        "embedding_local_path": "/models/embedding",
        "encoding_contract": {"query_prefix": "query: ", "document_prefix": "passage: "},
        "include_field_vectors": True,
    }


@pytest.mark.parametrize(("budget", "expected_network"), [("standard", False), ("deep", True)])
def test_reranker_budget_uses_local_first_and_network_only_for_deep(
    tmp_path: Path,
    budget: str,
    expected_network: bool,
) -> None:
    dataset = make_dataset(tmp_path, 1)
    baseline, _ = _production_branch_inputs(tmp_path, dataset)

    class FakeDiscovery:
        resources = SimpleNamespace(gpu_count=0)

        def __init__(self) -> None:
            self.allow_network: list[bool] = []

        def discover(self, *, allow_network: bool, **_: Any) -> list[ModelCandidate]:
            self.allow_network.append(allow_network)
            return [ModelCandidate("local/zh-reranker", "reranker", "local_huggingface_cache")]

        def ensure_available(self, candidate: ModelCandidate, *, allow_download: bool) -> ModelCandidate:
            assert allow_download is expected_network
            candidate.status = "AVAILABLE"
            candidate.local_path = "/tmp/local-reranker"
            return candidate

        def smoke_test(self, candidate: ModelCandidate, *, device: str) -> ModelCandidate:
            del device
            candidate.status = "SMOKE_PASSED"
            return candidate

    discovery = FakeDiscovery()
    context = _branch_context(tmp_path, dataset, baseline, budget=budget, model_discovery=discovery)
    outcome = RerankingBranch().generate(context)
    assert outcome.status == "READY"
    assert discovery.allow_network == [expected_network]
    assert any(candidate.config.get("reranker_method") == "cross_encoder" for candidate in outcome.candidates)


def test_model_specific_encoding_contract_is_applied() -> None:
    class FakeModel:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], dict[str, Any]]] = []

        def encode(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
            self.calls.append((texts, kwargs))
            return [[1.0, 0.0] for _ in texts]

    model = FakeModel()
    contract = EncodingContract(
        query_prefix="query: ",
        document_prefix="passage: ",
        query_prompt_name="query",
        normalize_embeddings=True,
        pooling="mean",
        source="test-model-card",
    )
    adapter = SentenceTransformerEncodingAdapter(model, contract)
    adapter.encode(["现金流"], action="search")
    adapter.encode(["经营现金流改善"], action="add")
    assert model.calls[0][0] == ["query: 现金流"]
    assert model.calls[0][1]["prompt_name"] == "query"
    assert model.calls[0][1]["normalize_embeddings"] is True
    assert model.calls[1][0] == ["passage: 经营现金流改善"]
    assert "prompt_name" not in model.calls[1][1]
