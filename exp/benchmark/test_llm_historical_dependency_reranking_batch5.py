from pathlib import Path

from exp.benchmark.run_llm_historical_dependency_reranking_batch5 import (
    BatchedDependencyScoreCache,
    ExperimentSettings,
    comparative_movements,
    score_diagnostics,
    split_candidate_batches,
)
from exp.benchmark.run_llm_historical_dependency_reranking import rerank_top_candidates


def candidates(count: int = 20) -> list[dict]:
    return [
        {
            "page_id": f"p{index:02d}",
            "source_turn_id": f"S001-Q{index:03d}",
            "score": 1.0 - index / 100,
            "rank": index,
        }
        for index in range(1, count + 1)
    ]


def settings(tmp_path: Path) -> ExperimentSettings:
    return ExperimentSettings(
        output_dir=tmp_path,
        old_result_dir=tmp_path / "old",
        model="deepseek-v4-flash",
        temperature=0.0,
        top_p=0.1,
        max_tokens=4096,
        timeout_seconds=240,
        retries=3,
        concurrency=5,
        candidate_k=20,
        llm_batch_candidate_count=5,
        output_k=5,
        thinking_mode="disabled",
        dataset_config=tmp_path / "dataset.json",
        memory_config=tmp_path / "memory.json",
        raw_config={},
    )


def test_top20_is_split_into_four_ordered_batches_without_loss() -> None:
    source = candidates()
    batches = split_candidate_batches(source)
    assert [len(batch) for batch in batches] == [5, 5, 5, 5]
    assert [row["page_id"] for batch in batches for row in batch] == [row["page_id"] for row in source]


def test_short_visible_candidate_pool_keeps_a_partial_final_batch() -> None:
    source = candidates(7)
    batches = split_candidate_batches(source)
    assert [len(batch) for batch in batches] == [5, 2]
    assert [row["page_id"] for batch in batches for row in batch] == [row["page_id"] for row in source]


def test_global_merge_uses_absolute_score_then_c3_rank() -> None:
    source = candidates(7)
    scores = {"p01": 50, "p02": 90, "p03": 50, "p04": 20, "p05": 40, "p06": 100, "p07": 50}
    result = rerank_top_candidates(source, scores, 20)
    assert [row["page_id"] for row in result] == ["p06", "p02", "p01", "p03", "p07", "p05", "p04"]


def test_cache_identity_includes_batch_scope(tmp_path: Path) -> None:
    cache = BatchedDependencyScoreCache(
        tmp_path / "cache.jsonl", client=object(), settings=settings(tmp_path), prompt_sha256="prompt-hash"
    )
    first = candidates(5)
    identity = cache.identity(
        query_id="S001-Q010",
        query_text="query",
        batch_index=2,
        candidates=first,
        payload="payload",
    )
    assert identity["batch_index"] == 2
    assert identity["llm_batch_candidate_count"] == 5
    assert identity["candidate_page_ids"] == [row["page_id"] for row in first]
    assert identity["candidate_payload_sha256"]


def test_score_diagnostics_tracks_batch_and_merged_ties() -> None:
    responses = [
        {
            "query_id": "q1",
            "batch_index": 1,
            "candidate_page_ids": ["a", "b"],
            "parsed": {
                "results": [
                    {"page_id": "a", "dependency_score": 50},
                    {"page_id": "b", "dependency_score": 50},
                ]
            },
        },
        {
            "query_id": "q1",
            "batch_index": 2,
            "candidate_page_ids": ["c"],
            "parsed": {"results": [{"page_id": "c", "dependency_score": 80}]},
        },
        {
            "query_id": "q2",
            "batch_index": 1,
            "candidate_page_ids": ["d", "e"],
            "parsed": {
                "results": [
                    {"page_id": "d", "dependency_score": 0},
                    {"page_id": "e", "dependency_score": 0},
                ]
            },
        },
    ]
    diagnostics, batch_rows, score_maps = score_diagnostics(responses, ["q1", "q2"], {"q1"})
    assert diagnostics["all_candidates_tied_batch_count"] == 3
    assert diagnostics["merged_all_candidates_tied_query_count"] == 1
    assert diagnostics["old_tied_batch5_discriminated_query_count"] == 1
    assert len(batch_rows) == 3
    assert score_maps["q1"] == {"a": 50.0, "b": 50.0, "c": 80.0}


def test_comparative_movements_keeps_every_requirement_of_same_query() -> None:
    before = [
        {"requirement_id": "q::E1", "session_id": "S001", "query_id": "q", "gold_members": ["a"], "is_or": False, "rank": 7},
        {"requirement_id": "q::E2", "session_id": "S001", "query_id": "q", "gold_members": ["b", "c"], "is_or": True, "rank": 3},
    ]
    after = [
        {**before[0], "rank": 4},
        {**before[1], "rank": 8},
    ]
    rows = comparative_movements(before, after, "old", "new")
    assert [row["requirement_id"] for row in rows] == ["q::E1", "q::E2"]
    assert [row["transition"] for row in rows] == ["PROMOTED", "DEMOTED"]
