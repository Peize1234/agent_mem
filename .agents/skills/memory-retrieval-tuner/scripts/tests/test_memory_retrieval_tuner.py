from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook


SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").exists())
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

from exp.benchmark.benchmark_common import load_dataset  # noqa: E402
from exp.benchmark.memory_gold_groups import parse_gold_requirements  # noqa: E402
from tuner.artifact_registry import ArtifactRegistry  # noqa: E402
from tuner.candidate_selector import classify_overfit, select_best  # noqa: E402
from tuner.dataset_audit import DatasetAuditFailed, audit_dataset  # noqa: E402
from tuner.evaluate_candidate import (  # noqa: E402
    _eligible_requirements,
    _evaluate_session,
    candidate_hash,
    evaluate_candidate,
)
from tuner.models import Candidate, CandidateResult, Dataset, Requirement, Turn  # noqa: E402
from tuner.orchestrator import TunerConfig, _evaluate_loso_many  # noqa: E402
from tuner.production_midterm_adapter import (  # noqa: E402
    ProductionMidtermAdapter,
    isolated_runtime_layout,
    production_candidate_from_manifests,
)
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
    ProductionMidtermAdapter.supported(config)
    with pytest.raises(ValueError, match="regenerated production artifacts"):
        ProductionMidtermAdapter.supported({**config, "page_representation": "summary"})

    first = isolated_runtime_layout(tmp_path, candidate_hash="candidate-a", session_id="S001")
    second = isolated_runtime_layout(tmp_path, candidate_hash="candidate-b", session_id="S001")
    third = isolated_runtime_layout(tmp_path, candidate_hash="candidate-a", session_id="S002")
    assert len({first.root, second.root, third.root}) == 3
    assert all(layout.sqlite_path.exists() for layout in (first, second, third))


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
        config={"backend": "production_midterm", "query_representation": "original"},
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
