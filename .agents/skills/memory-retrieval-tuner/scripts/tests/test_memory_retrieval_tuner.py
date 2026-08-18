from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook


SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").exists())
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

from exp.benchmark.memory_gold_groups import parse_gold_requirements  # noqa: E402
from tuner.artifact_registry import ArtifactRegistry  # noqa: E402
from tuner.candidate_selector import classify_overfit, select_best  # noqa: E402
from tuner.dataset_audit import DatasetAuditFailed, audit_dataset  # noqa: E402
from tuner.evaluate_candidate import _evaluate_session, candidate_hash, evaluate_candidate  # noqa: E402
from tuner.models import Candidate, CandidateResult, Dataset, Requirement, Turn  # noqa: E402
from tuner.split_sessions import create_or_load_split  # noqa: E402


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
        {"source_turn_id": f"S001-Q{index:03d}", "page_id": f"S001-Q{index:03d}"}
        for index in (2, 3, 4, 8, 9, 10, 1)
    ]
    evaluated = _evaluate_session(dataset, session, {target.query_id: ranking}, k=k, target="midterm", shortterm_window=3)
    assert evaluated["requirements"][0]["hit_at_k"] is expected


@pytest.mark.parametrize(
    ("count", "method", "validation_count"),
    [
        (8, "deterministic_stratified_holdout", 2),
        (6, "deterministic_holdout", 2),
        (4, "leave_one_session_out", 1),
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
    assert create_or_load_split(dataset, output_dir=output, seed=42, shortterm_window=3) == split


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
        config={"backend": "offline_repository_adapter", "query_representation": "original"},
    )
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    discovered = registry.discover_frozen_queries(
        dataset.sha256,
        query_text_by_id={
            turn.query_id: turn.question for turns in dataset.sessions.values() for turn in turns
        },
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
    baseline = Candidate(
        name="baseline",
        stage="baseline",
        config={
            "backend": "offline_repository_adapter",
            "query_representation": "original",
            "page_representation": "production",
            "retrieval_method": "bm25",
            "top_k_pages": 20,
        },
    )
    alternative = Candidate(
        name="alternative",
        stage="cheap",
        config={**baseline.config, "page_representation": "summary"},
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


def test_numeric_candidates_reuse_raw_ranking_cache(tmp_path: Path) -> None:
    dataset = make_dataset(tmp_path, 1)
    registry = ArtifactRegistry(tmp_path / "cache", tmp_path / "results")
    base_config = {
        "backend": "offline_repository_adapter",
        "query_representation": "original",
        "page_representation": "production",
        "retrieval_method": "bm25",
        "top_k_pages": 20,
        "page_similarity_threshold": 0.0,
    }
    baseline = Candidate(name="baseline", stage="baseline", config=base_config)
    pool = Candidate(name="pool", stage="cheap", config={**base_config, "top_k_pages": 5})
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
    reused = evaluate_candidate(candidate=pool, **common)
    assert reused.cache_hits == 1
    assert reused.cache_misses == 0
