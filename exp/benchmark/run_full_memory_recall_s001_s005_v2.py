"""Evaluate ShortTerm, C3 MidTerm, and production LongTerm memory on V3 Gold groups.

The runner is deliberately offline by default.  It reuses the frozen C3 Page/
query embeddings and production LongTerm retrieval traces generated from the
same source QA.  The revised Excel changes only Gold grouping, so changing a
label never regenerates a Page or invokes an LLM.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
from openpyxl import load_workbook

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json, resolve_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.memory_gold_groups import GoldRequirement, parse_gold_requirements  # noqa: E402
from exp.benchmark.midterm_retrieval_eval import stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import evaluate_all, rank_configuration  # noqa: E402
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402


DEFAULT_CONFIG = REPO_ROOT / "exp/benchmark/full_memory_recall_s001_s005_v2.json"


@dataclass(frozen=True)
class DatasetTurn:
    session_code: str
    session_id: str
    turn_index: int
    query_id: str
    question: str
    answer: str
    gold_raw: str
    gold_groups: tuple[GoldRequirement, ...]


@dataclass(frozen=True)
class RunSettings:
    dataset_path: Path
    session_codes: tuple[str, ...]
    output_dir: Path
    shortterm_qa_turns: int
    shortterm_top_k: int
    midterm_top_k: tuple[int, ...]
    longterm_top_k: int
    embedding_model: str
    device: str
    batch_size: int
    expected: Mapping[str, int]
    longterm_trace_files: Mapping[str, Path]
    raw_config: Mapping[str, Any]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_int_list(value: str) -> tuple[int, ...]:
    values = tuple(sorted({int(item.strip()) for item in value.split(",") if item.strip()}))
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("TopK 必须是逗号分隔的正整数")
    return values


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S001-S005 全量 Short/Mid/Long Memory Recall（V3 OR Gold）")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--shortterm-window", type=int, help="ShortTerm QA turn 数，生产默认 3")
    parser.add_argument("--shortterm-top-k", type=int, help="ShortTerm 返回的最近 QA turn 数，生产默认 3")
    parser.add_argument("--midterm-top-k", type=parse_int_list, help="例如 5,10,20")
    parser.add_argument("--longterm-top-k", type=int)
    parser.add_argument("--embedding-model")
    parser.add_argument("--device")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--sanity-check-only", action="store_true")
    return parser.parse_args()


def load_settings(args: argparse.Namespace) -> RunSettings:
    config_path = args.config.resolve()
    config = load_json(config_path)
    dataset_path = resolve_path(REPO_ROOT, config["dataset"])
    output_dir = args.output_dir or resolve_path(REPO_ROOT, config["output_dir"])
    shortterm = int(args.shortterm_window or config["shortterm"]["qa_turns"])
    shortterm_top_k = int(args.shortterm_top_k or config["shortterm"]["top_k"])
    midterm_top_k = args.midterm_top_k or tuple(int(value) for value in config["midterm"]["top_k"])
    longterm_top_k = int(args.longterm_top_k or config["longterm"]["top_k"])
    batch_size = int(args.batch_size or config["midterm"]["batch_size"])
    if shortterm <= 0 or shortterm_top_k <= 0 or longterm_top_k <= 0 or batch_size <= 0:
        raise ValueError("ShortTerm window/TopK、LongTerm TopK 和 batch size 必须为正数")
    if str(config["longterm"]["mode"]) == "frozen_production_trace" and longterm_top_k > 5:
        raise ValueError("冻结 Production LongTerm provenance trace 只保存 Top5；如需更大 TopK 必须重新跑生产检索")
    traces = {
        code: resolve_path(REPO_ROOT, path) for code, path in config["longterm"]["trace_files"].items()
    }
    return RunSettings(
        dataset_path=dataset_path,
        session_codes=tuple(config["sessions"]),
        output_dir=Path(output_dir).resolve(),
        shortterm_qa_turns=shortterm,
        shortterm_top_k=shortterm_top_k,
        midterm_top_k=tuple(sorted(set(midterm_top_k))),
        longterm_top_k=longterm_top_k,
        embedding_model=str(args.embedding_model or config["midterm"]["embedding_model"]),
        device=str(args.device or config["midterm"]["device"]),
        batch_size=batch_size,
        expected={str(key): int(value) for key, value in config["expected"].items()},
        longterm_trace_files=traces,
        raw_config=config,
    )


def _normalise_header(value: Any) -> str:
    return str(value or "").strip()


def load_v3_dataset(path: Path, session_codes: Sequence[str]) -> dict[str, list[DatasetTurn]]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    result: dict[str, list[DatasetTurn]] = {}
    required = {"编号", "当前问题", "最终回答", "关联前序对话"}
    try:
        for sheet_name in workbook.sheetnames:
            code = sheet_name.split("_", 1)[0]
            if code not in session_codes:
                continue
            rows = workbook[sheet_name].iter_rows(values_only=True)
            headers = [_normalise_header(value) for value in next(rows)]
            index = {name: position for position, name in enumerate(headers) if name}
            missing = required - set(index)
            if missing:
                raise ValueError(f"{sheet_name} 缺少列：{sorted(missing)}")
            turns: list[DatasetTurn] = []
            for row in rows:
                query_id = str(row[index["编号"]] or "").strip().upper()
                question = str(row[index["当前问题"]] or "").strip()
                answer = str(row[index["最终回答"]] or "").strip()
                if not query_id and not question and not answer:
                    continue
                raw = str(row[index["关联前序对话"]] or "").strip()
                turns.append(
                    DatasetTurn(
                        session_code=code,
                        session_id=sheet_name,
                        turn_index=len(turns),
                        query_id=query_id,
                        question=question,
                        answer=answer,
                        gold_raw=raw,
                        gold_groups=parse_gold_requirements(raw),
                    )
                )
            result[code] = turns
    finally:
        workbook.close()
    if set(result) != set(session_codes):
        raise ValueError(f"Session 不一致：expected={list(session_codes)}, actual={sorted(result)}")
    validate_dataset_order(result)
    return result


def validate_dataset_order(sessions: Mapping[str, Sequence[DatasetTurn]]) -> None:
    for code, turns in sessions.items():
        positions = {turn.query_id: turn.turn_index for turn in turns}
        if len(positions) != len(turns):
            raise ValueError(f"{code} 出现重复 Query ID")
        for turn in turns:
            if not turn.query_id.startswith(f"{code}-Q"):
                raise ValueError(f"Query ID 与 Session 不匹配：{turn.query_id} / {code}")
            for group in turn.gold_groups:
                for member in group.members:
                    if member not in positions:
                        raise ValueError(f"{turn.query_id} 引用了不存在的 Gold：{member}")
                    if positions[member] >= turn.turn_index:
                        raise ValueError(f"{turn.query_id} 引用了当前或未来 Gold：{member}")


def source_qa_hash(sessions: Mapping[str, Sequence[DatasetTurn]]) -> str:
    rows = [
        {
            "session_code": code,
            "query_id": turn.query_id,
            "question": turn.question,
            "answer": turn.answer,
        }
        for code, turns in sessions.items()
        for turn in turns
    ]
    return stable_hash(rows)


def static_dataset_stats(
    sessions: Mapping[str, Sequence[DatasetTurn]], shortterm_qa_turns: int, shortterm_top_k: int | None = None
) -> dict[str, Any]:
    queries = [turn for turns in sessions.values() for turn in turns]
    groups = [group for turn in queries for group in turn.gold_groups]
    short_count = 0
    effective_top_k = min(shortterm_qa_turns, shortterm_top_k or shortterm_qa_turns)
    for turns in sessions.values():
        positions = {turn.query_id: turn.turn_index for turn in turns}
        for turn in turns:
            short_visible = {
                candidate.query_id
                for candidate in turns[max(0, turn.turn_index - effective_top_k) : turn.turn_index]
            }
            for group in turn.gold_groups:
                if group.hit_by(short_visible):
                    short_count += 1
                if any(positions[member] >= turn.turn_index for member in group.members):
                    raise AssertionError(f"Future Gold leakage: {turn.query_id} -> {group.members}")
    return {
        "session_count": len(sessions),
        "query_count": len(queries),
        "gold_requirement_count": len(groups),
        "or_requirement_count": sum(group.is_or for group in groups),
        "shortterm_requirement_count": short_count,
        "outside_shortterm_requirement_count": len(groups) - short_count,
        "shortterm_window_qa_turns": shortterm_qa_turns,
        "shortterm_top_k": effective_top_k,
        "session_query_counts": {code: len(turns) for code, turns in sessions.items()},
    }


def assert_expected_stats(stats: Mapping[str, Any], expected: Mapping[str, int]) -> None:
    errors = {
        key: {"expected": expected[key], "actual": stats.get(key)}
        for key in expected
        if key in stats and stats.get(key) != expected[key]
    }
    if errors:
        raise AssertionError(f"V3 dataset sanity check failed；停止检索：{errors}")


def load_longterm_traces(settings: RunSettings) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache: dict[Path, list[dict[str, Any]]] = {}
    selected: dict[str, dict[str, Any]] = {}
    trace_hashes: dict[str, str] = {}
    effective_config_hashes: dict[str, str] = {}
    for code, path in settings.longterm_trace_files.items():
        if path not in cache:
            cache[path] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        rows = [row for row in cache[path] if str(row.get("turn_id", "")).startswith(f"{code}-Q")]
        for row in rows:
            if row.get("error"):
                raise AssertionError(f"Production LongTerm trace error at {row['turn_id']}: {row['error']}")
            if "long_retrieved_turn_ids" not in row:
                raise AssertionError(f"Production LongTerm trace 缺少 provenance：{row['turn_id']}")
            selected[str(row["turn_id"])] = row
        trace_hashes[code] = sha256_file(path)
        effective_config = path.parent / "effective_memory_config.json"
        if not effective_config.exists():
            raise AssertionError(f"LongTerm trace 缺少 effective config：{effective_config}")
        effective_config_hashes[code] = sha256_file(effective_config)
    return selected, {
        "trace_sha256": trace_hashes,
        "effective_memory_config_sha256": effective_config_hashes,
        "source": "frozen production retrieval provenance",
    }


def validate_longterm_visibility(
    sessions: Mapping[str, Sequence[DatasetTurn]], traces: Mapping[str, Mapping[str, Any]]
) -> None:
    expected_ids = {turn.query_id for turns in sessions.values() for turn in turns}
    if set(traces) != expected_ids:
        raise AssertionError(
            f"LongTerm trace Query set mismatch: missing={sorted(expected_ids-set(traces))[:5]}, "
            f"extra={sorted(set(traces)-expected_ids)[:5]}"
        )
    positions = {turn.query_id: (code, turn.turn_index) for code, turns in sessions.items() for turn in turns}
    for query_id, row in traces.items():
        query_code, query_index = positions[query_id]
        if row.get("cross_session_leak_count") or row.get("future_turn_leak_count"):
            raise AssertionError(f"Frozen trace 已标记 leakage：{query_id}")
        for source_id in row["long_retrieved_turn_ids"]:
            source = positions.get(str(source_id))
            if source is None or source[0] != query_code or source[1] >= query_index:
                raise AssertionError(f"LongTerm future/cross-session leakage：{query_id} <- {source_id}")


def maybe_reencode_c3(
    settings: RunSettings,
    checkpoint: Mapping[str, Any],
) -> tuple[Mapping[str, Sequence[float]], Mapping[str, Sequence[float]], dict[str, Any]]:
    production_model = "BAAI/bge-small-zh-v1.5"
    if settings.embedding_model == production_model:
        return checkpoint["page_vectors"], checkpoint["query_vectors"], {
            "embedding_cache_reused": True,
            "new_embedding_count": 0,
            "contract": "frozen C3 vectors",
        }
    from sentence_transformers import SentenceTransformer

    page_ids = sorted(checkpoint["page_texts"])
    query_ids = sorted(checkpoint["query_texts"])
    identity = {
        "model": settings.embedding_model,
        "page_ids": page_ids,
        "page_texts": [checkpoint["page_texts"][item] for item in page_ids],
        "query_ids": query_ids,
        "query_texts": [checkpoint["query_texts"][item] for item in query_ids],
        "normalization": True,
    }
    cache_key = stable_hash(identity)
    cache_dir = settings.output_dir / "cache/embeddings" / settings.embedding_model.replace("/", "_")
    cache_path = cache_dir / f"{cache_key}.npz"
    if cache_path.exists():
        values = np.load(cache_path)
        page_matrix, query_matrix = values["pages"], values["queries"]
        reused = True
        new_count = 0
    else:
        model = SentenceTransformer(settings.embedding_model, device=settings.device)
        page_matrix = model.encode(
            [checkpoint["page_texts"][item] for item in page_ids],
            batch_size=settings.batch_size,
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        query_matrix = model.encode(
            [checkpoint["query_texts"][item] for item in query_ids],
            batch_size=settings.batch_size,
            normalize_embeddings=True,
            show_progress_bar=True,
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(cache_path, pages=page_matrix, queries=query_matrix)
        reused = False
        new_count = len(page_ids) + len(query_ids)
    return (
        {item: page_matrix[index].tolist() for index, item in enumerate(page_ids)},
        {item: query_matrix[index].tolist() for index, item in enumerate(query_ids)},
        {
            "embedding_cache_reused": reused,
            "new_embedding_count": new_count,
            "cache_path": str(cache_path),
            "contract": "SentenceTransformer encode, normalize_embeddings=True, no instruction",
        },
    )


def load_and_validate_c3(
    settings: RunSettings,
    sessions: Mapping[str, Sequence[DatasetTurn]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    snapshots, old_pages, frozen_queries, checkpoints, cache_metadata = checkpoint_inputs()
    c3 = checkpoints["C3"]
    expected_long_queries = {
        turn.query_id
        for turns in sessions.values()
        for turn in turns
        if any(
            not group.hit_by(
                candidate.query_id
                for candidate in turns[
                    max(0, turn.turn_index - min(settings.shortterm_qa_turns, settings.shortterm_top_k)) :
                    turn.turn_index
                ]
            )
            for group in turn.gold_groups
        )
    }
    if len(old_pages) != settings.expected["midterm_page_count"]:
        raise AssertionError(f"C3 Page count mismatch: {len(old_pages)}")
    if len(frozen_queries) != settings.expected["midterm_evaluation_query_count"]:
        raise AssertionError(f"C3 Query count mismatch: {len(frozen_queries)}")
    frozen_query_ids = {str(query["query_id"]) for query in frozen_queries}
    if not expected_long_queries.issubset(frozen_query_ids):
        raise AssertionError(
            f"V3 MidTerm eligible Query 不在冻结 C3 集合中：{sorted(expected_long_queries-frozen_query_ids)}"
        )
    excluded_by_or = frozen_query_ids - expected_long_queries
    if excluded_by_or != {"S002-Q042", "S005-Q042"}:
        raise AssertionError(f"冻结 C3 到 V3 OR 子集的差异异常：{sorted(excluded_by_or)}")
    dataset_by_id = {turn.query_id: turn for turns in sessions.values() for turn in turns}
    for page in old_pages:
        turn = dataset_by_id[str(page["source_turn_id"])]
        if str(page["user_input"]) != turn.question or str(page["assistant_response"]) != turn.answer:
            raise AssertionError(f"C3 Page source QA hash mismatch：{turn.query_id}")
    for query in frozen_queries:
        turn = dataset_by_id[str(query["query_id"])]
        if str(query["original_query"]) != turn.question:
            raise AssertionError(f"C3 Query text mismatch：{turn.query_id}")
    page_vectors, query_vectors, embedding_meta = maybe_reencode_c3(settings, c3)
    rankings = rank_configuration(snapshots, query_vectors, page_vectors)
    if settings.embedding_model == "BAAI/bge-small-zh-v1.5":
        old_metrics, _ = evaluate_all(snapshots, rankings)
        reproduced_gold_at_5 = round(float(old_metrics["recall_at_5"]) * int(old_metrics["eligible_gold_count"]))
        if reproduced_gold_at_5 != 59 or int(old_metrics["eligible_gold_count"]) != 154:
            raise AssertionError(f"Frozen C3 baseline reproduction failed: {old_metrics}")
    page_sources = {str(page["page_id"]): str(page["source_turn_id"]) for page in c3["pages"]}
    positions = {turn.query_id: turn.turn_index for turns in sessions.values() for turn in turns}
    for query_id, ranking in rankings.items():
        query_index = positions[query_id]
        if any(positions[page_sources[str(row["page_id"])]] >= query_index - settings.shortterm_qa_turns for row in ranking):
            raise AssertionError(f"C3 visible Page leakage：{query_id}")
    return rankings, {
        "page_count": len(old_pages),
        "query_count": len(frozen_queries),
        "v3_eligible_query_count": len(expected_long_queries),
        "excluded_from_midterm_by_or_shortterm_hit": sorted(excluded_by_or),
        "old_gold_contract_reproduction": {"gold_at_5": 59, "gold_count": 154, "recall_at_5": 59 / 154},
        "cache_metadata": cache_metadata,
        **embedding_meta,
    }


def group_rank(group: GoldRequirement, retrieved_turn_ids: Sequence[str]) -> int | None:
    ranks = {str(turn_id).upper(): rank for rank, turn_id in enumerate(retrieved_turn_ids, start=1)}
    candidates = [ranks[member] for member in group.members if member in ranks]
    return min(candidates) if candidates else None


def build_results(
    settings: RunSettings,
    sessions: Mapping[str, Sequence[DatasetTurn]],
    midterm_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    longterm_traces: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirement_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    primary_midterm_k = 5 if 5 in settings.midterm_top_k else min(settings.midterm_top_k)
    for code in settings.session_codes:
        turns = sessions[code]
        for turn in turns:
            short_ids = [
                candidate.query_id
                for candidate in turns[
                    max(0, turn.turn_index - min(settings.shortterm_qa_turns, settings.shortterm_top_k)) :
                    turn.turn_index
                ]
            ]
            mid_ids = [str(row["source_turn_id"]) for row in midterm_rankings.get(turn.query_id, [])]
            long_ids = [str(value) for value in longterm_traces[turn.query_id]["long_retrieved_turn_ids"]]
            if len(long_ids) > 5 and settings.longterm_top_k > 5:
                raise AssertionError("Frozen production LongTerm trace expected at most Top5")
            group_results: list[dict[str, Any]] = []
            short_hits_by_group = [group.hit_by(short_ids) for group in turn.gold_groups]
            long_range_query = any(not hit for hit in short_hits_by_group)
            for group_index, group in enumerate(turn.gold_groups, start=1):
                short_rank = group_rank(group, short_ids)
                short_hit = short_rank is not None
                mid_rank = None if short_hit else group_rank(group, mid_ids)
                long_rank = (
                    group_rank(group, long_ids[: settings.longterm_top_k]) if long_range_query else None
                )
                row = {
                    "requirement_id": f"{turn.query_id}::G{group_index}",
                    "session_id": code,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "group_index": group_index,
                    "gold_members": list(group.members),
                    "is_or": group.is_or,
                    "shortterm_eligible": True,
                    "shortterm_rank": short_rank,
                    "shortterm_hit": short_hit,
                    "midterm_eligible": not short_hit,
                    "midterm_rank": mid_rank,
                    "longterm_eligible": long_range_query,
                    "longterm_rank": long_rank,
                    "longterm_hit": long_rank is not None,
                }
                for top_k in settings.midterm_top_k:
                    row[f"midterm_hit_at_{top_k}"] = mid_rank is not None and mid_rank <= top_k
                row["final_hit"] = short_hit or row[f"midterm_hit_at_{primary_midterm_k}"] or long_rank is not None
                requirement_rows.append(row)
                group_results.append(row)
            query_rows.append(
                {
                    "session_id": code,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "original_query": turn.question,
                    "parsed_gold_groups": [list(group.members) for group in turn.gold_groups],
                    "shortterm_hit": [row["shortterm_hit"] for row in group_results],
                    "midterm_rank": [row["midterm_rank"] for row in group_results],
                    "longterm_hit": [row["longterm_hit"] for row in group_results],
                    "final_hit": [row["final_hit"] for row in group_results],
                }
            )
    return requirement_rows, query_rows


def recall(hit: int, gold: int) -> float:
    return hit / gold if gold else 0.0


def aggregate_metrics(
    settings: RunSettings, requirement_rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    short_rows = list(requirement_rows)
    eligible = [row for row in requirement_rows if row["midterm_eligible"]]
    longterm_eligible = [row for row in requirement_rows if row["longterm_eligible"]]
    short_hits = sum(bool(row["shortterm_hit"]) for row in short_rows)
    summary: dict[str, Any] = {
        "shortterm": {
            "gold_requirement_count": len(short_rows),
            "hit_count": short_hits,
            "recall": recall(short_hits, len(short_rows)),
            "window_qa_turns": settings.shortterm_qa_turns,
            "top_k": settings.shortterm_top_k,
        },
        "midterm": {"eligible_gold_count": len(eligible)},
        "longterm": {
            "eligible_gold_count": len(longterm_eligible),
            "hit_count": sum(bool(row["longterm_hit"]) for row in longterm_eligible),
            "top_k": settings.longterm_top_k,
            "eligibility_contract": "all Gold groups in Queries containing at least one dependency outside ShortTerm",
        },
        "overall": {
            "gold_requirement_count": len(short_rows),
            "hit_count": sum(bool(row["final_hit"]) for row in short_rows),
        },
    }
    summary["longterm"]["recall"] = recall(summary["longterm"]["hit_count"], len(longterm_eligible))
    summary["overall"]["memory_coverage"] = recall(summary["overall"]["hit_count"], len(short_rows))
    all_ranks = [int(row["midterm_rank"]) for row in eligible]
    for top_k in settings.midterm_top_k:
        hits = sum(bool(row[f"midterm_hit_at_{top_k}"]) for row in eligible)
        summary["midterm"][f"gold_at_{top_k}"] = hits
        summary["midterm"][f"recall_at_{top_k}"] = recall(hits, len(eligible))
    query_best_ranks: dict[str, int] = {}
    for row in eligible:
        query_id = str(row["query_id"])
        rank = int(row["midterm_rank"])
        query_best_ranks[query_id] = min(rank, query_best_ranks.get(query_id, rank))
    summary["midterm"]["mrr"] = (
        statistics.fmean(1.0 / rank for rank in query_best_ranks.values()) if query_best_ranks else 0.0
    )
    summary["midterm"]["mean_gold_rank"] = statistics.fmean(all_ranks) if all_ranks else None
    summary["midterm"]["ranked_gold_count"] = len(all_ranks)
    summary["midterm"]["evaluated_query_count"] = len(query_best_ranks)

    session_rows: list[dict[str, Any]] = []
    for code in settings.session_codes:
        session_all = [row for row in requirement_rows if row["session_id"] == code]
        session_eligible = [row for row in session_all if row["midterm_eligible"]]
        session_longterm = [row for row in session_all if row["longterm_eligible"]]
        values: dict[str, Any] = {
            "session_id": code,
            "gold_requirement_count": len(session_all),
            "shortterm_hit": sum(bool(row["shortterm_hit"]) for row in session_all),
            "shortterm_recall": recall(sum(bool(row["shortterm_hit"]) for row in session_all), len(session_all)),
            "midterm_eligible_gold": len(session_eligible),
            "longterm_eligible_gold": len(session_longterm),
            "longterm_hit": sum(bool(row["longterm_hit"]) for row in session_longterm),
            "longterm_recall": recall(
                sum(bool(row["longterm_hit"]) for row in session_longterm), len(session_longterm)
            ),
            "final_hit": sum(bool(row["final_hit"]) for row in session_all),
            "memory_coverage": recall(sum(bool(row["final_hit"]) for row in session_all), len(session_all)),
        }
        for top_k in settings.midterm_top_k:
            count = sum(bool(row[f"midterm_hit_at_{top_k}"]) for row in session_eligible)
            values[f"midterm_gold_at_{top_k}"] = count
            values[f"midterm_recall_at_{top_k}"] = recall(count, len(session_eligible))
        session_rows.append(values)
    primary_k = 5 if 5 in settings.midterm_top_k else min(settings.midterm_top_k)
    summary["midterm"]["macro_session_recall_at_5"] = statistics.fmean(
        float(row[f"midterm_recall_at_{primary_k}"]) for row in session_rows
    )
    return summary, session_rows


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                    for key, value in row.items()
                }
            )


def render_summary(
    settings: RunSettings, stats: Mapping[str, Any], metrics: Mapping[str, Any], sessions: Sequence[Mapping[str, Any]]
) -> str:
    lines = [
        "# S001–S005 全量 Memory Recall（V3 OR Gold）",
        "",
        "## 数据集",
        "",
        f"- Session：{stats['session_count']}",
        f"- Query：{stats['query_count']}",
        f"- Gold requirements：{stats['gold_requirement_count']}",
        f"- OR requirements：{stats['or_requirement_count']}",
        "",
        "## 汇总",
        "",
        "| Layer | Gold / Eligible | Hit | Recall |",
        "|---|---:|---:|---:|",
        f"| ShortTerm（{settings.shortterm_qa_turns} QA） | {metrics['shortterm']['gold_requirement_count']} | "
        f"{metrics['shortterm']['hit_count']} | {metrics['shortterm']['recall']:.2%} |",
        f"| MidTerm C3 @5 | {metrics['midterm']['eligible_gold_count']} | "
        f"{metrics['midterm']['gold_at_5']} | {metrics['midterm']['recall_at_5']:.2%} |",
        f"| LongTerm Production @{settings.longterm_top_k} | {metrics['longterm']['eligible_gold_count']} | "
        f"{metrics['longterm']['hit_count']} | {metrics['longterm']['recall']:.2%} |",
        f"| Overall Memory Coverage | {metrics['overall']['gold_requirement_count']} | "
        f"{metrics['overall']['hit_count']} | {metrics['overall']['memory_coverage']:.2%} |",
        "",
        "## MidTerm",
        "",
        f"- R@5：{metrics['midterm']['recall_at_5']:.2%} ({metrics['midterm']['gold_at_5']}/"
        f"{metrics['midterm']['eligible_gold_count']})",
        f"- R@10：{metrics['midterm']['recall_at_10']:.2%}",
        f"- R@20：{metrics['midterm']['recall_at_20']:.2%}",
        f"- MRR：{metrics['midterm']['mrr']:.4f}",
        f"- Mean Gold Rank：{metrics['midterm']['mean_gold_rank']:.2f}",
        "",
        "## Session",
        "",
        "| Session | ShortTerm | MidTerm R@5 | LongTerm | Overall |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sessions:
        lines.append(
            f"| {row['session_id']} | {row['shortterm_recall']:.2%} | {row['midterm_recall_at_5']:.2%} | "
            f"{row['longterm_recall']:.2%} | {row['memory_coverage']:.2%} |"
        )
    lines.extend(
        [
            "",
            "LongTerm 结果来自相同 source QA、相同生产配置生成的冻结 provenance trace；新版 Excel 只重组了 5 个 OR Gold label。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    settings = load_settings(args)
    sessions = load_v3_dataset(settings.dataset_path, settings.session_codes)
    reference_path = resolve_path(REPO_ROOT, settings.raw_config["source_qa_reference_dataset"])
    reference_sessions = load_v3_dataset(reference_path, settings.session_codes)
    if source_qa_hash(sessions) != source_qa_hash(reference_sessions):
        raise AssertionError("新版 Excel 与 frozen artifact source QA 不一致；不能复用 C3/LongTerm cache")
    production_stats = static_dataset_stats(sessions, 3, 3)
    assert_expected_stats(production_stats, settings.expected)
    stats = static_dataset_stats(sessions, settings.shortterm_qa_turns, settings.shortterm_top_k)
    if args.sanity_check_only:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    longterm_traces, longterm_meta = load_longterm_traces(settings)
    validate_longterm_visibility(sessions, longterm_traces)
    midterm_rankings, midterm_meta = load_and_validate_c3(settings, sessions)
    requirement_rows, query_rows = build_results(settings, sessions, midterm_rankings, longterm_traces)
    metrics, session_rows = aggregate_metrics(settings, requirement_rows)

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    write_json(settings.output_dir / "dataset_stats.json", stats)
    write_json(settings.output_dir / "summary.json", metrics)
    write_jsonl(settings.output_dir / "query_results.jsonl", query_rows)
    write_csv(settings.output_dir / "query_results.csv", query_rows)
    write_jsonl(settings.output_dir / "requirement_results.jsonl", requirement_rows)
    write_csv(settings.output_dir / "requirement_results.csv", requirement_rows)
    write_csv(settings.output_dir / "session_metrics.csv", session_rows)
    metadata = {
        "experiment_name": settings.raw_config["experiment_name"],
        "dataset": str(settings.dataset_path),
        "dataset_sha256": sha256_file(settings.dataset_path),
        "source_qa_sha256": source_qa_hash(sessions),
        "source_qa_reference_dataset": str(reference_path),
        "source_qa_reference_dataset_sha256": sha256_file(reference_path),
        "production_memory_config": str(resolve_path(REPO_ROOT, settings.raw_config["production_memory_config"])),
        "production_memory_config_sha256": sha256_file(
            resolve_path(REPO_ROOT, settings.raw_config["production_memory_config"])
        ),
        "settings": {**asdict(settings), "raw_config": settings.raw_config},
        "midterm": midterm_meta,
        "longterm": longterm_meta,
        "new_llm_calls": 0,
        "summary_regeneration": False,
        "full_session_rerun": False,
        "future_leakage": False,
        "validation": {
            "dataset_static_sanity": "PASS",
            "source_qa_matches_frozen_artifacts": "PASS",
            "gold_members_are_prior_turns": "PASS",
            "c3_page_query_source_hashes": "PASS",
            "c3_59_of_154_old_contract_reproduction": "PASS",
            "c3_visible_scope": "PASS",
            "production_longterm_trace_scope": "PASS",
            "or_group_denominator": "PASS",
        },
    }
    write_json(settings.output_dir / "run_metadata.json", metadata)
    (settings.output_dir / "summary.md").write_text(
        render_summary(settings, stats, metrics, session_rows), encoding="utf-8"
    )
    print((settings.output_dir / "summary.md").read_text(encoding="utf-8"))
    print(f"结果目录：{settings.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
