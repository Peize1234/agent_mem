from __future__ import annotations

from pathlib import Path

from exp.benchmark.run_midterm_small_reranker_ablation import (
    BATCH_SIZE,
    MAX_LENGTH,
    MODEL_SPECS,
    QWEN_INSTRUCTION,
    TOP_K,
    ModelSpec,
    ScoreCache,
    cache_identity,
    failure_category,
    rerank_top20,
    sha256_text,
    validate_reranked_scope,
    write_frozen_instruction,
)


def dense_ranking(count: int = 25) -> dict[str, list[dict[str, object]]]:
    return {
        "Q1": [
            {
                "page_id": f"P{index:02d}",
                "source_turn_id": f"S-Q{index:02d}",
                "rank": index,
                "score": 1.0 - index / 100,
            }
            for index in range(1, count + 1)
        ]
    }


def score_rows(count: int = 20) -> dict[str, dict[str, float]]:
    return {
        f"Q1::P{index:02d}": {"reranker_score": float(index)}
        for index in range(1, count + 1)
    }


def test_rerank_only_reorders_top20_and_keeps_tail() -> None:
    dense = dense_ranking()
    reranked = rerank_top20(dense, score_rows())

    assert [row["page_id"] for row in reranked["Q1"][:20]] == [f"P{index:02d}" for index in range(20, 0, -1)]
    assert [row["page_id"] for row in reranked["Q1"][20:]] == ["P21", "P22", "P23", "P24", "P25"]
    assert [row["rank"] for row in reranked["Q1"]] == list(range(1, 26))
    assert validate_reranked_scope(dense, reranked)["status"] == "PASS"


def test_scope_validation_detects_candidate_leakage() -> None:
    dense = dense_ranking()
    reranked = rerank_top20(dense, score_rows())
    reranked["Q1"][0]["page_id"] = "FUTURE"

    result = validate_reranked_scope(dense, reranked)
    assert result["status"] == "FAIL"
    assert result["candidate_set_mismatch_query_ids"] == ["Q1"]


def test_cache_identity_freezes_contract_and_text_hashes() -> None:
    spec = ModelSpec("X", "example/model", "Example", "generic")
    pair = {
        "query_id": "Q1",
        "page_id": "P1",
        "dense_rank": 7,
        "query_text": "查询",
        "page_text": "页面",
    }
    identity = cache_identity(spec, pair, "revision", "unused")

    assert identity["query_sha256"] == sha256_text("查询")
    assert identity["page_sha256"] == sha256_text("页面")
    assert identity["candidate_k"] == TOP_K
    assert identity["max_length"] == MAX_LENGTH
    assert identity["batch_size"] == BATCH_SIZE
    assert identity["dtype"] == "float16"
    assert identity["quantization"] == "none"
    assert identity["cpu_offload"] is False


def test_cache_key_changes_for_qwen_instruction() -> None:
    spec = next(spec for spec in MODEL_SPECS if spec.key == "QWEN")
    pair = {
        "query_id": "Q1",
        "page_id": "P1",
        "dense_rank": 1,
        "query_text": "查询",
        "page_text": "页面",
    }
    left = cache_identity(spec, pair, "revision", "instruction-a")
    right = cache_identity(spec, pair, "revision", "instruction-b")
    assert left != right
    assert left["instruction_sha256"] == "instruction-a"


def test_incremental_score_cache_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "scores.jsonl"
    cache = ScoreCache(path)
    assert cache.get("key") is None
    cache.append({"cache_key": "key", "reranker_score": 0.5, "status": "SUCCESS"})

    reloaded = ScoreCache(path)
    assert reloaded.get("key") == {"cache_key": "key", "reranker_score": 0.5, "status": "SUCCESS"}


def test_qwen_instruction_is_frozen_before_inference(tmp_path: Path) -> None:
    metadata = write_frozen_instruction(tmp_path)
    path = Path(metadata["path"])
    assert path.read_text(encoding="utf-8") == QWEN_INSTRUCTION + "\n"
    assert write_frozen_instruction(tmp_path) == metadata


def test_failure_category_uses_fixed_non_gold_diagnostics() -> None:
    assert failure_category("前面的判断需要修订吗", "总资产", "总资产") == "历史依赖关系未识别"
    assert (
        failure_category("比较指标", "总资产与归母股东权益", "总资产与归母股东权益")
        == "Page 内容高度同质"
    )
    assert failure_category("经营活动现金流如何", "总资产", "经营活动现金流") == "普通语义相似误判"


def test_model_matrix_has_fixed_resource_contract() -> None:
    assert [spec.key for spec in MODEL_SPECS] == ["GTE", "BGE", "QWEN"]
    assert next(spec for spec in MODEL_SPECS if spec.key == "QWEN").prompt_name == "memory_dependency"
    assert MAX_LENGTH == 512
    assert BATCH_SIZE == 1
