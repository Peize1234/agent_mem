#!/usr/bin/env python3
"""Export exact S001 dense-retrieval inputs from existing experiment artifacts.

This utility performs no model inference. It reads the existing snapshot, saved
baseline rankings, cached Q0 query vectors, and Qdrant Page vectors captured in
the snapshot. The cached vectors are used only to validate saved Top-20 scores
and to recover a score for an eligible Gold Page outside the saved Top-20.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULT_ROOT = PROJECT_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
SELECTED_CASES = (
    ("S001-Q014", "DENSE_TOP5_HIT"),
    ("S001-Q028", "DENSE_TOP5_HIT"),
    ("S001-Q013", "DENSE_TOP5_MISS"),
    ("S001-Q026", "DENSE_TOP5_MISS"),
    ("S001-Q035", "DENSE_TOP5_MISS"),
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def production_page_embedding_text(page: dict[str, Any]) -> str:
    """Mirror MidTermMemory.page_embedding_text without altering whitespace."""
    keywords = page.get("keywords") or []
    keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
    return "\n".join(
        part
        for part in (
            page.get("summary", ""),
            f"Keywords: {keywords_text}" if keywords_text else "",
            f"User: {page.get('user_input', '')}",
        )
        if part
    )


def experiment_p0(page: dict[str, Any]) -> str:
    """Mirror the experiment's P0 helper, including its field stripping."""
    summary = str(page.get("summary") or "").strip()
    user_input = str(page.get("user_input") or "").strip()
    keywords = page.get("keywords") or []
    keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
    return "\n".join(
        part for part in (summary, f"Keywords: {keywords_text}" if keywords_text else "", f"User: {user_input}") if part
    )


def cosine_rank(
    query_vector: np.ndarray,
    visible_page_ids: list[str],
    page_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reproduce the existing evaluator's float32 cosine ranking."""
    matrix = np.asarray([page_by_id[page_id]["stored_embedding"] for page_id in visible_page_ids], dtype=np.float32)
    query = np.asarray(query_vector, dtype=np.float32)
    denominator = np.linalg.norm(matrix, axis=1) * float(np.linalg.norm(query))
    scores = np.divide(matrix @ query, denominator, out=np.zeros(len(matrix), dtype=np.float32), where=denominator > 0)
    rows = [
        {"page_id": page_id, "score": float(score)} for page_id, score in zip(visible_page_ids, scores, strict=True)
    ]
    rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def load_q0_vectors(cache_dir: Path, query_ids: list[str]) -> tuple[dict[str, np.ndarray], list[Path]]:
    cache_files = sorted(cache_dir.glob("queries-Q0-Q1-Q2-Q3-Q4-Q5-Q6-QO-*.npz"))
    if not cache_files:
        raise FileNotFoundError(f"No existing Q0 query embedding cache under {cache_dir}")

    loaded: list[dict[str, np.ndarray]] = []
    for cache_file in cache_files:
        with np.load(cache_file) as payload:
            loaded.append({str(item_id): vector.copy() for item_id, vector in zip(payload["ids"], payload["vectors"], strict=True)})

    selected: dict[str, np.ndarray] = {}
    for query_id in query_ids:
        cache_key = f"Q0:{query_id}:0"
        vectors = [payload[cache_key] for payload in loaded if cache_key in payload]
        if not vectors:
            raise KeyError(f"Q0 vector is absent from all caches: {query_id}")
        if any(not np.array_equal(vectors[0], vector) for vector in vectors[1:]):
            raise AssertionError(f"Duplicate Q0 caches disagree for {query_id}")
        selected[query_id] = vectors[0]
    return selected, cache_files


def exact_fence(text: str) -> str:
    """Return a Markdown code fence that cannot collide with the exact text."""
    longest_run = 0
    current_run = 0
    for char in text:
        if char == "`":
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 0
    marker = "`" * max(3, longest_run + 1)
    return f"{marker}text\n{text}\n{marker}"


def format_score(score: float) -> str:
    return format(float(score), ".17g")


def build_case(
    *,
    query_id: str,
    case_type: str,
    query: dict[str, Any],
    visibility: dict[str, Any],
    baseline: dict[str, Any],
    query_vector: np.ndarray,
    page_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], float]:
    visible_page_ids = [str(page_id) for page_id in visibility["visible_page_ids"]]
    if int(visibility.get("future_page_leak_count") or 0) != 0:
        raise AssertionError(f"Future Page leak detected for {query_id}")

    ranking = cosine_rank(query_vector, visible_page_ids, page_by_id)
    saved_top20_ids = [str(page_id) for page_id in baseline["top20_page_ids"]]
    saved_top20_scores = [float(score) for score in baseline["top20_scores"]]
    if [str(row["page_id"]) for row in ranking[:20]] != saved_top20_ids:
        raise AssertionError(f"Cached vectors do not reproduce saved Top-20 ordering for {query_id}")
    score_differences = [
        abs(float(row["score"]) - saved_score) for row, saved_score in zip(ranking[:20], saved_top20_scores, strict=True)
    ]
    max_score_difference = max(score_differences, default=0.0)
    if max_score_difference > 1e-6:
        raise AssertionError(f"Cached vectors do not reproduce saved scores for {query_id}: {max_score_difference}")

    eligible_gold_page_ids = [str(page_id) for page_id in query["eligible_gold_page_ids"]]
    actual_gold_ranks = [
        next(int(row["rank"]) for row in ranking if str(row["page_id"]) == gold_page_id)
        for gold_page_id in eligible_gold_page_ids
    ]
    if actual_gold_ranks != [int(rank) for rank in baseline["gold_ranks"]]:
        raise AssertionError(f"Cached vectors do not reproduce Gold ranks for {query_id}")

    ranks_to_export = set(range(1, min(20, len(ranking)) + 1))
    if len(ranking) <= 20:
        ranks_to_export = set(range(1, len(ranking) + 1))
        output_policy = "ALL_QUERY_TIME_VISIBLE_PAGES"
    else:
        ranks_to_export.update(actual_gold_ranks)
        output_policy = "DENSE_TOP20_PLUS_ELIGIBLE_GOLD_OUTSIDE_TOP20"

    dense_results: list[dict[str, Any]] = []
    for row in ranking:
        rank = int(row["rank"])
        if rank not in ranks_to_export:
            continue
        page_id = str(row["page_id"])
        page = page_by_id[page_id]
        if rank <= len(saved_top20_scores):
            score = saved_top20_scores[rank - 1]
            score_source = "saved_baseline_top20_score"
        else:
            score = float(row["score"])
            score_source = "derived_from_existing_cached_query_and_qdrant_vectors"
        dense_results.append(
            {
                "rank": rank,
                "page_id": page_id,
                "source_turn_id": str(page["source_turn_id"]),
                "score": score,
                "score_source": score_source,
                "is_gold": page_id in eligible_gold_page_ids,
                "page_embedding_text": str(page["current_embedding_text"]),
            }
        )

    rank_by_page = {str(row["page_id"]): row for row in ranking}
    gold_pages = [
        {
            "page_id": page_id,
            "source_turn_id": str(page_by_id[page_id]["source_turn_id"]),
            "dense_rank": int(rank_by_page[page_id]["rank"]),
            "dense_score": (
                saved_top20_scores[int(rank_by_page[page_id]["rank"]) - 1]
                if int(rank_by_page[page_id]["rank"]) <= len(saved_top20_scores)
                else float(rank_by_page[page_id]["score"])
            ),
        }
        for page_id in eligible_gold_page_ids
    ]
    dense_top5_page_ids = [str(row["page_id"]) for row in ranking[:5]]
    hit_all_gold = all(page_id in dense_top5_page_ids for page_id in eligible_gold_page_ids)
    expected_hit = case_type == "DENSE_TOP5_HIT"
    if hit_all_gold != expected_hit:
        raise AssertionError(f"Case type does not match exact eligible-Gold Top-5 result for {query_id}")

    return (
        {
            "query_id": query_id,
            "session_id": str(query["session_id"]),
            "case_type": case_type,
            "original_query": str(query["original_query"]),
            "query_embedding_text": str(query["original_query"]),
            "visible_page_count": len(visible_page_ids),
            "output_policy": output_policy,
            "gold_page_ids": eligible_gold_page_ids,
            "gold_pages": gold_pages,
            "dense_results": dense_results,
            "dense_top5_page_ids": dense_top5_page_ids,
            "dense_hit_all_gold": hit_all_gold,
            "gold_dense_ranks": {page_id: int(rank_by_page[page_id]["rank"]) for page_id in eligible_gold_page_ids},
        },
        max_score_difference,
    )


def render_case(case: dict[str, Any]) -> list[str]:
    lines = [
        f"## {case['query_id']} — {case['case_type']}",
        "",
        "### Query",
        "",
        f"query_id: `{case['query_id']}`",
        "",
        "current user query:",
        "",
        exact_fence(case["original_query"]),
        "",
        "实际送入 Query Embedding Model 的文本：",
        "",
        exact_fence(case["query_embedding_text"]),
        "",
        "### 正确依赖 Gold",
        "",
    ]
    for gold in case["gold_pages"]:
        lines.extend(
            [
                "✅ GOLD",
                "",
                f"Gold Page ID: `{gold['page_id']}`  ",
                f"source_turn_id: `{gold['source_turn_id']}`  ",
                f"Dense Rank: `#{gold['dense_rank']}`  ",
                f"Dense Similarity Score: `{format_score(gold['dense_score'])}`",
                "",
            ]
        )

    lines.extend(
        [
            "### Dense Retrieval",
            "",
            f"可见 Page 数：`{case['visible_page_count']}`  ",
            f"输出范围：`{case['output_policy']}`",
            "",
        ]
    )
    for result in case["dense_results"]:
        marker = "✅ GOLD" if result["is_gold"] else "❌ NON-GOLD"
        lines.append(
            f"#{result['rank']}  `{result['page_id']}`  score=`{format_score(result['score'])}`  {marker}"
        )

    lines.extend(["", "### Page Embedding Inputs", ""])
    for result in case["dense_results"]:
        marker = "✅ GOLD" if result["is_gold"] else "❌ NON-GOLD"
        lines.extend(
            [
                f"#### Dense Rank #{result['rank']}",
                "",
                f"Dense Similarity Score: `{format_score(result['score'])}`  ",
                f"Score Source: `{result['score_source']}`",
                "",
                f"Page ID: `{result['page_id']}`  ",
                f"Source Turn ID: `{result['source_turn_id']}`",
                "",
                f"是否 Gold: {marker}",
                "",
                "实际送入 Page Embedding Model 的完整文本：",
                "",
                exact_fence(result["page_embedding_text"]),
                "",
            ]
        )

    lines.extend(
        [
            "### 事实汇总",
            "",
            "Gold Page：",
            "",
            exact_fence(json.dumps(case["gold_page_ids"], ensure_ascii=False)),
            "",
            "Dense Top5：",
            "",
            exact_fence(json.dumps(case["dense_top5_page_ids"], ensure_ascii=False)),
            "",
            f"Dense 是否命中全部 Gold：`{'YES' if case['dense_hit_all_gold'] else 'NO'}`",
            "",
            "Gold 的 Dense Rank：",
            "",
        ]
    )
    for page_id, rank in case["gold_dense_ranks"].items():
        lines.append(f"- `{page_id}` -> `#{rank}`")
    lines.append("")
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    contract = payload["production_embedding_contract"]
    sources = payload["source_artifacts"]
    validation = payload["validation"]
    lines = [
        "# S001 Page Dense Embedding 人工检查",
        "",
        "本文件只包含现有 S001 snapshot、基线 Dense 结果和既有 embedding 向量中的事实。未运行 benchmark，未生成 Page，未调用任何 LLM，未执行 reranker。",
        "",
        "## 生产 Dense Embedding 文本路径",
        "",
        "Query：`MidTermRetriever.search(query)` 将同一个 `query` 传给 `MidTermMemory.search_pages(query)`；后者直接执行 "
        "`embedding_model.embed(query, \"search\")`。本文件所列 Q0 的输入因此就是 snapshot 中完整的 `original_query`。",
        "",
        "Page：`MidTermMemory.page_embedding_text(payload)` 按以下顺序构造文本，`insert_page` 用该字符串执行 "
        "`embedding_model.embed(embedding_text, \"add\")`，并由 `_stored_page_payload` 将同一个字符串保存到 Qdrant payload `data`：",
        "",
        "```text",
        "<summary>",
        "Keywords: <keywords 以逗号+空格连接>",
        "User: <user_input>",
        "```",
        "",
        f"Snapshot Page 校验：`current_embedding_text == Qdrant payload data == production page_embedding_text == P0`："
        f"`{validation['page_embedding_text_exact_match_count']}/{validation['page_count']}`。",
        "",
        "代码路径：",
        "",
        *[f"- `{path}`" for path in contract["code_paths"]],
        "",
        "Snapshot 原始数据血缘：",
        "",
        f"- benchmark：`{sources['original_benchmark_result']}`",
        f"- runtime SQLite：`{sources['runtime_history_db']}`",
        f"- Qdrant collection：`{sources['qdrant_collection']}`",
        "",
        "## Dense 排序与分数来源校验",
        "",
        f"选定案例的 saved Top20 排序复现：`{str(validation['selected_top20_order_reproduced']).upper()}`。  ",
        f"选定案例的 Gold rank 复现：`{str(validation['selected_gold_ranks_reproduced']).upper()}`。  ",
        f"保存 Top20 score 与既有向量余弦的最大绝对差：`{format_score(validation['max_abs_score_difference'])}`。  ",
        "Top20 内 score 直接取自 `baseline_metrics.json`；Top20 外 eligible Gold 的 score 只由已有 Q0 cache 向量和 "
        "snapshot 中已有 Qdrant Page 向量计算。没有运行 embedding model。",
        "",
    ]
    for case in payload["cases"]:
        lines.extend(render_case(case))
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-root", type=Path, default=DEFAULT_RESULT_ROOT)
    args = parser.parse_args()

    result_root = args.result_root.resolve()
    snapshot_dir = result_root / "snapshot"
    pages = load_jsonl(snapshot_dir / "pages.jsonl")
    queries = load_jsonl(snapshot_dir / "queries.jsonl")
    visibility_rows = load_jsonl(snapshot_dir / "query_page_visibility.jsonl")
    with (result_root / "baseline_metrics.json").open(encoding="utf-8") as handle:
        baseline_payload = json.load(handle)
    with (snapshot_dir / "snapshot_manifest.json").open(encoding="utf-8") as handle:
        snapshot_manifest = json.load(handle)

    page_by_id = {str(page["page_id"]): page for page in pages}
    query_by_id = {str(query["query_id"]): query for query in queries}
    visibility_by_id = {str(row["query_id"]): row for row in visibility_rows}
    baseline_by_id = {str(row["query_id"]): row for row in baseline_payload["per_query"]}
    selected_query_ids = [query_id for query_id, _ in SELECTED_CASES]
    query_vectors, query_cache_files = load_q0_vectors(
        result_root / "cache/embeddings/BAAI_bge-small-zh-v1.5", selected_query_ids
    )

    exact_page_matches = sum(
        str(page["current_embedding_text"]) == production_page_embedding_text(page) == experiment_p0(page)
        for page in pages
    )
    if exact_page_matches != len(pages):
        raise AssertionError(f"P0/current_embedding_text mismatch: {exact_page_matches}/{len(pages)}")

    cases: list[dict[str, Any]] = []
    max_abs_score_difference = 0.0
    for query_id, case_type in SELECTED_CASES:
        case, score_difference = build_case(
            query_id=query_id,
            case_type=case_type,
            query=query_by_id[query_id],
            visibility=visibility_by_id[query_id],
            baseline=baseline_by_id[query_id],
            query_vector=query_vectors[query_id],
            page_by_id=page_by_id,
        )
        cases.append(case)
        max_abs_score_difference = max(max_abs_score_difference, score_difference)

    payload = {
        "scope": "S001 production Page dense candidate retrieval only",
        "generated_from_existing_data_only": True,
        "benchmark_runs": 0,
        "page_generation_calls": 0,
        "embedding_model_inference_calls": 0,
        "llm_calls": 0,
        "reranker_calls": 0,
        "production_embedding_contract": {
            "query_embedding_text": "The query argument passed unchanged to MidTermMemory.search_pages; Q0 uses original_query.",
            "page_embedding_text": "summary + optional 'Keywords: ' line + 'User: ' user_input line, joined by newline",
            "qdrant_payload_field": "data",
            "snapshot_field": "current_embedding_text",
            "experiment_representation": "P0",
            "code_paths": [
                "mem0/memory/retrieval_tools.py::_search_query",
                "mem0/memory/main.py::_with_midterm_search_results",
                "mem0/memory/midterm_retriever.py::MidTermRetriever.search",
                "mem0/memory/midterm.py::MidTermMemory.search_pages",
                "mem0/memory/midterm.py::MidTermMemory.page_embedding_text",
                "mem0/memory/midterm.py::MidTermMemory.insert_page",
                "mem0/memory/midterm.py::MidTermMemory._stored_page_payload",
                "exp/benchmark/midterm_retrieval_eval.py::page_representation(P0)",
                "exp/benchmark/run_midterm_retrieval_experiments.py::build_snapshot",
            ],
        },
        "source_artifacts": {
            "original_benchmark_result": str(snapshot_manifest["source_dir"]),
            "runtime_history_db": str(snapshot_manifest["history_db_path"]),
            "qdrant_collection": str(snapshot_manifest["qdrant_collection"]),
            "queries": str((snapshot_dir / "queries.jsonl").relative_to(PROJECT_ROOT)),
            "pages": str((snapshot_dir / "pages.jsonl").relative_to(PROJECT_ROOT)),
            "visibility": str((snapshot_dir / "query_page_visibility.jsonl").relative_to(PROJECT_ROOT)),
            "saved_dense_results": str((result_root / "baseline_metrics.json").relative_to(PROJECT_ROOT)),
            "query_embedding_caches": [str(path.relative_to(PROJECT_ROOT)) for path in query_cache_files],
            "page_vectors": "snapshot/pages.jsonl stored_embedding (captured from the run Qdrant collection)",
        },
        "validation": {
            "page_count": len(pages),
            "page_embedding_text_exact_match_count": exact_page_matches,
            "selected_top20_order_reproduced": True,
            "selected_gold_ranks_reproduced": True,
            "max_abs_score_difference": max_abs_score_difference,
            "selected_future_page_leak_count": sum(
                int(visibility_by_id[query_id].get("future_page_leak_count") or 0) for query_id in selected_query_ids
            ),
        },
        "case_selection": {
            "dense_top5_hits": [query_id for query_id, case_type in SELECTED_CASES if case_type == "DENSE_TOP5_HIT"],
            "dense_top5_misses": [query_id for query_id, case_type in SELECTED_CASES if case_type == "DENSE_TOP5_MISS"],
            "definition": "HIT means Dense Top5 contains every query-time eligible Gold Page; MISS means it does not.",
        },
        "cases": cases,
    }

    json_path = result_root / "manual_embedding_inspection.json"
    markdown_path = result_root / "manual_embedding_inspection.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print(f"Wrote {markdown_path}")
    print(f"Wrote {json_path}")
    print(f"Cases: {len(cases)}; full P0/current_embedding_text matches: {exact_page_matches}/{len(pages)}")
    print(f"Saved Top20 max score delta from existing vectors: {max_abs_score_difference:.3g}")


if __name__ == "__main__":
    main()
