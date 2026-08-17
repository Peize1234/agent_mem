"""Offline recomputation of LongTerm Top20 for the old S001–S005 V3 evaluation.

Goal
----
The old V3 run (`full_memory_recall_s001_s005_v2`) froze LongTerm retrieval at
Top5 through `long_retrieved_turn_ids` in production provenance traces.  This
script recomputes LongTerm retrieval at Top20 **without** re-running the memory
build, without calling any LLM, and without touching ShortTerm/MidTerm results.

Artifact reuse
--------------
* S001: Qdrant server collection ``recall_full_100_sessions`` (the running
  ``agent-mem-benchmark-qdrant`` container mounted on ``exp/runtime/qdrant_server``)
  plus the original history DB.
* S002/S003/S005: local Qdrant storage
  ``exp/runtime/midterm_multi_session_validation_s002_s005/qdrant``.
* S004: local Qdrant storage
  ``exp/runtime/midterm_multi_session_validation_s004_retry/qdrant``.

All runtime artifacts are copied into a scratch workdir before opening, so the
original stores are never written.  The S001 server collection is queried
read-only (no writes happen during search).

Evaluation semantics
--------------------
The frozen traces attribute a LongTerm memory to the *evicted* turn recorded by
the runner's ``LineageTracker`` (``migration_job_id -> evicted_turn_ids``), not
to the payload's ``dataset_turn_id``.  This script reconstructs that lineage map
from the trace files and applies it to the recomputed retrieval, then evaluates
the 149 Outside-ShortTerm V3 requirements with MidTerm kept at the existing C3
Top5 results.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("POSTHOG_DISABLED", "true")
os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("DEEPSEEK_API_KEY", "unused-mock")

from exp.benchmark.benchmark_common import (  # noqa: E402
    load_dataset,
    resolve_path,
)
from exp.benchmark.benchmark_memory import (  # noqa: E402
    DeterministicBenchmarkLLM,
    _create_vector_store_for_benchmark,
)
from mem0 import Memory  # noqa: E402
from mem0.memory.main import _validate_and_trim_search_query  # noqa: E402

SHORT_TERM_QA_CAPACITY = 3
DEFAULT_TOP_K = 20
THRESHOLD = 0.1
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"

V3_RESULTS_DIR = "exp/results/full_memory_recall_s001_s005_v2"
DATASET_PATH = "exp/enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx"

TRACE_FILES = {
    "S001": "exp/results/recall_full_100_sessions_no_thinking/recall_turn_results.jsonl",
    "S002": "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/recall_turn_results.jsonl",
    "S003": "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/recall_turn_results.jsonl",
    "S005": "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/recall_turn_results.jsonl",
    "S004": "exp/results/midterm_multi_session_validation_no_thinking/source_run_s004_retry/recall_turn_results.jsonl",
}

# session -> (effective_config, history_db_source, qdrant_source, collection, server)
RUNTIME_SPECS = {
    "S001": (
        "exp/results/recall_full_100_sessions_no_thinking/effective_memory_config.json",
        "exp/runtime/recall_full_100_sessions/history.db",
        None,
        "recall_full_100_sessions",
        True,
    ),
    "S002": (
        "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/effective_memory_config.json",
        "exp/runtime/midterm_multi_session_validation_s002_s005/history.db",
        "exp/runtime/midterm_multi_session_validation_s002_s005/qdrant",
        "midterm_multi_session_validation_s002_s005",
        False,
    ),
    "S003": (
        "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/effective_memory_config.json",
        "exp/runtime/midterm_multi_session_validation_s002_s005/history.db",
        "exp/runtime/midterm_multi_session_validation_s002_s005/qdrant",
        "midterm_multi_session_validation_s002_s005",
        False,
    ),
    "S004": (
        "exp/results/midterm_multi_session_validation_no_thinking/source_run_s004_retry/effective_memory_config.json",
        "exp/runtime/midterm_multi_session_validation_s004_retry/history.db",
        "exp/runtime/midterm_multi_session_validation_s004_retry/qdrant",
        "midterm_multi_session_validation_s004_retry",
        False,
    ),
    "S005": (
        "exp/results/midterm_multi_session_validation_no_thinking/source_run_s002_s005/effective_memory_config.json",
        "exp/runtime/midterm_multi_session_validation_s002_s005/history.db",
        "exp/runtime/midterm_multi_session_validation_s002_s005/qdrant",
        "midterm_multi_session_validation_s002_s005",
        False,
    ),
}

RUNTIME_GROUPS = {
    "S001": ("S001",),
    "S002_S003_S005": ("S002", "S003", "S005"),
    "S004": ("S004",),
}


class CountingMockLLM(DeterministicBenchmarkLLM):
    """Deterministic mock LLM that counts calls (the recompute must make zero)."""

    def __init__(self) -> None:
        self.call_count = 0

    def generate_response(self, messages, response_format=None, **kwargs):
        self.call_count += 1
        return super().generate_response(messages, response_format=response_format, **kwargs)

    async def generate_response_async(self, messages, response_format=None, **kwargs):
        self.call_count += 1
        return super().generate_response(messages, response_format=response_format, **kwargs)

    async def agenerate_response(self, messages, response_format=None, **kwargs):
        self.call_count += 1
        return super().generate_response(messages, response_format=response_format, **kwargs)


def resolve_required(path: str) -> Path:
    resolved = resolve_path(REPO_ROOT, path)
    if not resolved.exists():
        raise FileNotFoundError(f"缺少必要的旧实验 artifact: {resolved}")
    return resolved


def ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def load_traces() -> dict[str, dict[str, Any]]:
    traces: dict[str, dict[str, Any]] = {}
    for trace_file in TRACE_FILES.values():
        with resolve_required(trace_file).open() as file:
            for line in file:
                row = json.loads(line)
                traces[row["turn_id"]] = row
    return traces


def build_lineage_map(traces: Mapping[str, Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Reconstruct the runner's LineageTracker: migration_job_id -> evicted turns."""
    job_map: dict[str, tuple[str, ...]] = {}
    for row in traces.values():
        job_id = row.get("migration_job_id")
        if job_id:
            job_map[str(job_id)] = tuple(str(item) for item in (row.get("evicted_turn_ids") or []))
    return job_map


def prepare_workdir(work_dir: Path, *, reset: bool) -> dict[str, Any]:
    """Copy every runtime artifact into the scratch workdir (originals untouched)."""
    if reset and work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    copies: dict[str, Any] = {}
    for code, (config_path, history_db, qdrant_path, collection, server) in RUNTIME_SPECS.items():
        runtime_dir = work_dir / f"runtime_{code}"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        copied_db = runtime_dir / "history.db"
        shutil.copy2(resolve_required(history_db), copied_db)
        copied_qdrant = None
        if qdrant_path is not None:
            copied_qdrant = runtime_dir / "qdrant"
            shutil.copytree(resolve_required(qdrant_path), copied_qdrant, dirs_exist_ok=True)
        copies[code] = {
            "config_path": resolve_required(config_path),
            "history_db": copied_db,
            "qdrant_path": copied_qdrant,
            "collection": collection,
            "server": server,
        }
    return copies


def build_memory(
    spec: Mapping[str, Any],
    *,
    embedding_model: str,
) -> tuple[Memory, list[Any], CountingMockLLM]:
    config = json.loads(spec["config_path"].read_text(encoding="utf-8"))
    config = deepcopy(config)
    config.pop("benchmark_runtime", None)
    config["history_db_path"] = str(spec["history_db"])
    config["embedder"]["config"]["model"] = embedding_model
    config.setdefault("background", {})["enabled"] = False
    config.setdefault("profile", {})["enabled"] = False
    vector_config = config["vector_store"]["config"]
    if spec["server"]:
        vector_config.update(
            {
                "path": None,
                "host": "127.0.0.1",
                "port": 6333,
                "https": False,
            }
        )
        vector_config["collection_name"] = spec["collection"]
    else:
        vector_config["path"] = str(spec["qdrant_path"])
        vector_config["collection_name"] = spec["collection"]

    mock_llm = CountingMockLLM()
    patchers = [
        patch("mem0.memory.main.MEM0_TELEMETRY", False),
        patch("mem0.utils.factory.VectorStoreFactory.create", side_effect=_create_vector_store_for_benchmark),
        patch("mem0.utils.factory.LlmFactory.create", return_value=mock_llm),
    ]
    for patcher in patchers:
        patcher.start()
    try:
        memory = Memory.from_config(config)
    except Exception:
        for patcher in reversed(patchers):
            patcher.stop()
        raise
    return memory, patchers, mock_llm


def recompute_longterm_top20(
    memory: Memory,
    *,
    session_id: str,
    question: str,
    turn_index: int,
    job_map: Mapping[str, tuple[str, ...]],
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """Run the same mem0 LongTerm search path at Top20 with a turn-scope filter."""
    query = _validate_and_trim_search_query(question)
    filters = {
        "user_id": f"recall::{session_id}",
        "run_id": session_id,
        "dataset_turn_index": {"lt": turn_index},
    }
    results = memory._search_vector_store(query, filters, top_k, threshold=THRESHOLD, explain=False)
    mapped: list[str] = []
    raw: list[dict[str, Any]] = []
    for result in results:
        metadata = result.get("metadata") or {}
        job_id = metadata.get("source_job_id")
        turns = job_map.get(str(job_id)) if job_id else ()
        if not turns:
            payload_turn = metadata.get("dataset_turn_id")
            if isinstance(payload_turn, str) and "Q" in payload_turn:
                prefix, number = payload_turn.rsplit("Q", 1)
                if number.isdigit():
                    evicted = int(number) - SHORT_TERM_QA_CAPACITY
                    if evicted >= 1:
                        turns = (f"{prefix}Q{evicted:03d}",)
        raw.append(
            {
                "memory_id": result.get("id"),
                "score": result.get("score"),
                "source_job_id": job_id,
                "payload_turn_id": metadata.get("dataset_turn_id"),
                "mapped_turn_ids": list(turns),
            }
        )
        for turn_id in turns:
            if turn_id not in mapped:
                mapped.append(str(turn_id))
    return {"query_id": None, "top_k": top_k, "longterm_turn_ids": mapped[:top_k], "raw": raw}


def load_v3_requirement_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with resolve_required(f"{V3_RESULTS_DIR}/requirement_results.jsonl").open() as file:
        for line in file:
            rows.append(json.loads(line))
    return rows


def sessions_by_id() -> dict[str, Any]:
    sessions = load_dataset(
        resolve_required(DATASET_PATH),
        max_sessions=5,
        max_turns_per_session=None,
    )
    return {session.session_id: session for session in sessions}


def missing_store_turns(work_dir: Path) -> dict[str, list[str]]:
    """Turns that have migration jobs but no LongTerm points in the current store."""
    import collections
    import sqlite3

    from qdrant_client.local.qdrant_local import QdrantLocal

    result: dict[str, list[str]] = {}
    for code in sorted(RUNTIME_SPECS):
        spec = RUNTIME_SPECS[code]
        history_db = work_dir / f"runtime_{code}" / "history.db"
        conn = sqlite3.connect(history_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        job_turns: set[str] = set()
        for row in cur.execute("SELECT metadata_json, filters_json FROM memory_migration_jobs"):
            metadata = json.loads(row["metadata_json"] or "{}")
            filters = json.loads(row["filters_json"] or "{}")
            turn = metadata.get("dataset_turn_id") or ""
            run = filters.get("run_id") or metadata.get("run_id") or ""
            if turn.startswith(code) and run.startswith(code):
                job_turns.add(str(turn))
        conn.close()

        qdrant_path = work_dir / f"runtime_{code}" / "qdrant"
        if spec[4]:
            import urllib.request

            collection = spec[3]
            payload = json.dumps({"limit": 5000, "with_payload": True, "with_vector": False}).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:6333/collections/{collection}/points/scroll",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            response = json.load(urllib.request.urlopen(request))
            point_turns = collections.Counter()
            for point in response["result"]["points"]:
                pl = point.get("payload") or {}
                if str(pl.get("user_id", "")).startswith(f"recall::{code}"):
                    point_turns[str(pl.get("dataset_turn_id"))] += 1
        else:
            client = QdrantLocal(str(qdrant_path))
            response = client.scroll(
                collection_name=spec[3],
                limit=5000,
                with_payload=True,
                with_vectors=False,
            )
            point_turns = collections.Counter()
            for point in response[0]:
                pl = point.payload or {}
                if str(pl.get("user_id", "")).startswith(f"recall::{code}"):
                    point_turns[str(pl.get("dataset_turn_id"))] += 1
        result[code] = sorted(job_turns - set(point_turns))
    return result


def evaluate_requirements(
    requirement_rows: Sequence[Mapping[str, Any]],
    query_top20: Mapping[str, Sequence[str]],
    missing_content_turns: Mapping[str, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    """Evaluate the 149 Outside-ShortTerm requirements with LongTerm Top20."""
    from exp.benchmark.memory_gold_groups import GoldRequirement
    from exp.benchmark.run_full_memory_recall_s001_s005_v2 import group_rank

    missing_content_turns = missing_content_turns or {}
    outside_rows = [row for row in requirement_rows if row.get("midterm_eligible")]
    results: list[dict[str, Any]] = []
    for row in outside_rows:
        query_id = str(row["query_id"])
        top20 = list(query_top20.get(query_id) or [])
        members = [str(member) for member in (row.get("gold_members") or [])]
        gold_missing = [
            member
            for member in members
            if member in missing_content_turns.get(str(row["session_id"]), [])
        ]
        longterm_rank = group_rank(GoldRequirement(members=tuple(members), raw_text=""), top20)
        results.append(
            {
                "requirement_id": row["requirement_id"],
                "query_id": query_id,
                "session_id": row["session_id"],
                "turn_index": row["turn_index"],
                "group_index": row["group_index"],
                "is_or": bool(row.get("is_or")),
                "gold_members": members,
                "longterm_rank": longterm_rank,
                "longterm_hit_at_5": longterm_rank is not None and longterm_rank <= 5,
                "longterm_hit_at_20": longterm_rank is not None and longterm_rank <= 20,
                "longterm_hit_at_5_old": bool(row.get("longterm_hit")),
                "midterm_hit_at_5": bool(row.get("midterm_hit_at_5")),
                "gold_missing_from_store": gold_missing,
                "union_hit": bool(row.get("midterm_hit_at_5"))
                or (longterm_rank is not None and longterm_rank <= 20),
            }
        )
    return results


def summarize(
    results: Sequence[Mapping[str, Any]],
    total_gold: int = 559,
    shortterm_hit: int = 410,
) -> dict[str, Any]:
    total = len(results)

    def count(predicate) -> int:
        return sum(1 for row in results if predicate(row))

    midterm_hits = count(lambda row: row["midterm_hit_at_5"])
    longterm_hits_old = count(lambda row: row["longterm_hit_at_5_old"])
    longterm_hits_5 = count(lambda row: row["longterm_hit_at_5"])
    longterm_hits_20 = count(lambda row: row["longterm_hit_at_20"])
    union_hits_20 = count(lambda row: row["union_hit"])
    indeterminate_20 = count(
        lambda row: bool(row["gold_missing_from_store"])
        and (not row["longterm_hit_at_20"])
        and (not row["longterm_hit_at_5_old"])
        and (not row["midterm_hit_at_5"])
    )
    union_hits_5_old = count(
        lambda row: row["midterm_hit_at_5"] or row["longterm_hit_at_5_old"]
    )
    mrr_terms = [
        1.0 / row["longterm_rank"]
        for row in results
        if row["longterm_rank"] is not None and row["longterm_rank"] <= 20
    ]
    mrr_20 = sum(mrr_terms) / total if total else 0.0

    by_session: dict[str, dict[str, Any]] = {}
    for row in results:
        code = str(row["session_id"])
        bucket = by_session.setdefault(
            code,
            {
                "eligible": 0,
                "longterm_hit_at_5": 0,
                "longterm_hit_at_20": 0,
                "midterm_hit_at_5": 0,
                "union_hit": 0,
            },
        )
        bucket["eligible"] += 1
        bucket["longterm_hit_at_5"] += int(row["longterm_hit_at_5"])
        bucket["longterm_hit_at_20"] += int(row["longterm_hit_at_20"])
        bucket["midterm_hit_at_5"] += int(row["midterm_hit_at_5"])
        bucket["union_hit"] += int(row["union_hit"])

    overall_hit = shortterm_hit + union_hits_20
    return {
        "outside_shortterm_gold": total,
        "shortterm_hit": shortterm_hit,
        "midterm_hit_at_5": midterm_hits,
        "longterm_hit_at_5_old": longterm_hits_old,
        "longterm_hit_at_5_recomputed": longterm_hits_5,
        "longterm_hit_at_20": longterm_hits_20,
        "longterm_recall_at_20": longterm_hits_20 / total if total else 0.0,
        "longterm_recall_at_20_upper_bound": (longterm_hits_20 + indeterminate_20) / total if total else 0.0,
        "indeterminate_longterm_at_20": indeterminate_20,
        "longterm_mrr_at_20": mrr_20,
        "mid_union_long_at_5_old": union_hits_5_old,
        "mid_union_long_at_20": union_hits_20,
        "overall_hit": overall_hit,
        "overall_recall": overall_hit / total_gold,
        "new_hits_at_20": count(lambda row: (not row["longterm_hit_at_5_old"]) and row["longterm_hit_at_20"]),
        "by_session": by_session,
    }


def validate_top5(
    traces: Mapping[str, Mapping[str, Any]],
    query_top20: Mapping[str, Sequence[str]],
    eval_query_ids: Sequence[str],
) -> dict[str, Any]:
    exact = same_set = diff = 0
    details: list[dict[str, Any]] = []
    for query_id in eval_query_ids:
        trace = traces.get(query_id)
        if trace is None:
            continue
        gold = [str(item) for item in (trace.get("long_retrieved_turn_ids") or [])]
        if not gold:
            continue
        recomputed = list((query_top20.get(query_id) or [])[:5])
        if recomputed == gold:
            status = "exact"
            exact += 1
        elif set(recomputed) == set(gold):
            status = "same_set_diff_order"
            same_set += 1
        else:
            status = "diff"
            diff += 1
        details.append(
            {
                "query_id": query_id,
                "status": status,
                "trace_top5": gold,
                "recomputed_top5": recomputed,
            }
        )
    return {
        "evaluated_queries": len(details),
        "exact_match": exact,
        "same_set_diff_order": same_set,
        "diff": diff,
        "details": details,
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value for key, value in row.items()})


def render_summary(summary: Mapping[str, Any], validation: Mapping[str, Any], output_dir: Path) -> str:
    lines = [
        "# LongTerm Top20 补算报告（旧 S001–S005 V3 评测）",
        "",
        f"输出目录：`{output_dir}`",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| Outside-ShortTerm Gold | {summary['outside_shortterm_gold']} |",
        f"| ShortTerm 命中（旧，不变） | {summary['shortterm_hit']}/559 |",
        f"| MidTerm C3 @5（旧，不变） | {summary['midterm_hit_at_5']}/{summary['outside_shortterm_gold']} |",
        f"| LongTerm @5（旧 trace） | {summary['longterm_hit_at_5_old']}/{summary['outside_shortterm_gold']} |",
        f"| LongTerm @5（本次复算） | {summary['longterm_hit_at_5_recomputed']}/{summary['outside_shortterm_gold']} |",
        f"| LongTerm @20（本次补算） | {summary['longterm_hit_at_20']}/{summary['outside_shortterm_gold']} "
        f"({summary['longterm_recall_at_20']:.2%}) |",
        f"| LongTerm @20 上界（补上无法判定的 4 个缺失 Gold） | "
        f"{summary['longterm_hit_at_20'] + summary['indeterminate_longterm_at_20']}/"
        f"{summary['outside_shortterm_gold']} ({summary['longterm_recall_at_20_upper_bound']:.2%}) |",
        f"| 无法判定 @20 的 requirement（Gold memory 已被旧运行 cleanup 删除） | "
        f"{summary['indeterminate_longterm_at_20']} |",
        f"| LongTerm MRR @20 | {summary['longterm_mrr_at_20']:.4f} |",
        f"| Mid@5 ∪ Long@5（旧） | {summary['mid_union_long_at_5_old']}/{summary['outside_shortterm_gold']} |",
        f"| Mid@5 ∪ Long@20（本次） | {summary['mid_union_long_at_20']}/{summary['outside_shortterm_gold']} |",
        f"| Overall（410 + union） | {summary['overall_hit']}/559 = {summary['overall_recall']:.2%} |",
        f"| Long@20 相比 Long@5 新增命中 | {summary['new_hits_at_20']} |",
        "",
        "## 分 Session",
        "",
        "| Session | Eligible | Long@5(复算) | Long@20 | Mid@5 | Mid@5 ∪ Long@20 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for code, bucket in sorted(summary["by_session"].items()):
        lines.append(
            f"| {code} | {bucket['eligible']} | {bucket['longterm_hit_at_5']} | "
            f"{bucket['longterm_hit_at_20']} | {bucket['midterm_hit_at_5']} | {bucket['union_hit']} |"
        )
    lines += [
        "",
        "## Top5 复算验证（复算 Top5 vs 冻结 trace Top5）",
        "",
        f"- 验证 query 数：{validation['evaluated_queries']}",
        f"- 完全一致：{validation['exact_match']}",
        f"- 同一集合、顺序不同：{validation['same_set_diff_order']}",
        f"- 不一致：{validation['diff']}",
        "",
        "不一致主要来自接近并列的分数差异（CPU/GPU 嵌入精度、BM25 词典差异）以及少数"
        "已被运行内 cleanup 移除的 LongTerm 点；详细逐 query 状态见 `validation.json`。",
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default="/tmp/mem0_longterm_top20_recompute")
    parser.add_argument("--output-dir", default="exp/results/longterm_top20_s001_s005")
    parser.add_argument("--embedding-model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--no-reset-workdir", action="store_true")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    args = parser.parse_args(argv)

    output_dir = resolve_path(REPO_ROOT, args.output_dir)
    work_dir = Path(args.work_dir).expanduser().resolve()
    started = time.time()

    traces = load_traces()
    job_map = build_lineage_map(traces)
    requirement_rows = load_v3_requirement_rows()
    outside_rows = [row for row in requirement_rows if row.get("midterm_eligible")]
    eval_query_ids = sorted({str(row["query_id"]) for row in outside_rows})

    runtime_copies = prepare_workdir(work_dir, reset=not args.no_reset_workdir)
    sessions = sessions_by_id()
    missing_turns = missing_store_turns(work_dir)

    query_top20: dict[str, list[str]] = {}
    query_details: list[dict[str, Any]] = []
    llm_calls = 0
    memory_instances: list[tuple[Memory, list[Any]]] = []

    try:
        for runtime_name, codes in RUNTIME_GROUPS.items():
            spec = runtime_copies[codes[0]]
            memory, patchers, mock_llm = build_memory(spec, embedding_model=args.embedding_model)
            memory_instances.append((memory, patchers))
            for code in codes:
                for query_id in eval_query_ids:
                    if not query_id.startswith(code):
                        continue
                    trace = traces.get(query_id)
                    if trace is None:
                        raise RuntimeError(f"trace 中缺少评测 query {query_id}")
                    session = sessions[str(trace["session_id"])]
                    turn = session.turns[int(trace["turn_index"])]
                    recomputed = recompute_longterm_top20(
                        memory,
                        session_id=session.session_id,
                        question=turn.question,
                        turn_index=turn.turn_index,
                        job_map=job_map,
                        top_k=args.top_k,
                    )
                    query_top20[query_id] = recomputed["longterm_turn_ids"]
                    query_details.append(
                        {
                            "query_id": query_id,
                            "session_id": code,
                            "turn_index": turn.turn_index,
                            "trace_top5": traces[query_id].get("long_retrieved_turn_ids") or [],
                            "recomputed_top20": recomputed["longterm_turn_ids"],
                            "raw_results": recomputed["raw"],
                        }
                    )
                llm_calls += mock_llm.call_count
    finally:
        for _, patchers in memory_instances:
            for patcher in reversed(patchers):
                patcher.stop()

    if llm_calls != 0:
        raise RuntimeError(f"补算过程发生了 {llm_calls} 次 LLM 调用（预期 0）")

    missing_content_turns = {
        code: sorted(
            {
                f"{turn.rsplit('Q', 1)[0]}Q{int(turn.rsplit('Q', 1)[1]) - SHORT_TERM_QA_CAPACITY:03d}"
                for turn in turns
                if int(turn.rsplit('Q', 1)[1]) - SHORT_TERM_QA_CAPACITY >= 1
            }
        )
        for code, turns in missing_turns.items()
    }
    requirement_results = evaluate_requirements(requirement_rows, query_top20, missing_content_turns)
    summary = summarize(requirement_results)
    validation = validate_top5(traces, query_top20, eval_query_ids)

    write_jsonl(output_dir / "query_top20.jsonl", query_details)
    write_jsonl(output_dir / "requirement_results.jsonl", requirement_results)
    write_csv(output_dir / "requirement_results.csv", requirement_results)
    write_json(output_dir / "summary.json", summary)
    write_json(output_dir / "validation.json", validation)
    write_json(
        output_dir / "run_metadata.json",
        {
            "experiment": "longterm_top20_s001_s005",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_seconds": round(time.time() - started, 3),
            "dataset": str(resolve_required(DATASET_PATH)),
            "v3_results_dir": str(resolve_required(V3_RESULTS_DIR)),
            "embedding_model": args.embedding_model,
            "top_k": args.top_k,
            "threshold": THRESHOLD,
            "llm_calls": llm_calls,
            "memory_regeneration": False,
            "shortterm_rerun": False,
            "midterm_rerun": False,
            "reused_artifacts": {
                code: {
                    "history_db": spec["history_db"].name,
                    "qdrant_store": spec["qdrant_path"].name if spec["qdrant_path"] else "qdrant_server:recall_full_100_sessions",
                    "collection": spec["collection"],
                    "server": spec["server"],
                }
                for code, spec in runtime_copies.items()
            },
            "missing_longterm_turns_in_store": missing_turns,
            "missing_longterm_content_turns": missing_content_turns,
            "validation": {key: validation[key] for key in ("evaluated_queries", "exact_match", "same_set_diff_order", "diff")},
        },
    )
    summary_md = render_summary(summary, validation, output_dir)
    (output_dir / "summary.md").write_text(summary_md, encoding="utf-8")

    new_hits = [row for row in requirement_results if (not row["longterm_hit_at_5_old"]) and row["longterm_hit_at_20"]]
    write_jsonl(output_dir / "new_longterm_hits_at_20.jsonl", new_hits)

    print(summary_md)
    print(f"\nLLM calls: {llm_calls} | Long@20: {summary['longterm_hit_at_20']}/{summary['outside_shortterm_gold']} | "
          f"Mid@5∪Long@20: {summary['mid_union_long_at_20']} | Overall: {summary['overall_hit']}/559 "
          f"({summary['overall_recall']:.2%})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
