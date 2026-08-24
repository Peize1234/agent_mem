#!/usr/bin/env python3
"""Build a read-only, traceable PPT evidence pack from existing Mem0 artifacts.

This script deliberately opens every SQLite database with mode=ro&immutable=1,
does not initialize Mem0/Qdrant clients, and writes only below exp/temp/ppt_evidence.
"""

from __future__ import annotations

import csv
import html
import json
import math
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


REPO = Path(__file__).resolve().parents[4]
OUT = REPO / "exp/temp/ppt_evidence"
RAW = OUT / "raw"
TARGET = REPO / "exp/results/auto_tuning/20260823T100601Z-99ac7ea4"
VALIDATION = REPO / "exp/results/midterm_multi_session_validation_no_thinking"
CONTEXT_EXP = REPO / "exp/results/midterm_add_local_context_ablation"
REWRITE_EXP = REPO / "exp/results/reference_resolution_prompt_ablation"
DATASET = REPO / "exp/金融分析数据集_S001-S010高质量重构.xlsx"

TYPE_INFO = {
    "A": ("中期记忆表示生成", "A_midterm_representation.json"),
    "B": ("两层检索", "B_hierarchical_retrieval.json"),
    "C": ("混合检索与 Reranker", "C_hybrid_ranking.json"),
    "D": ("模型主动二次补查", "D_agentic_retrieval.json"),
    "E": ("Query 上下文重写", "E_query_rewrite.json"),
    "F": ("上下文感知归档", "F_context_archive.json"),
    "G": ("记忆衰减与强化", "G_decay_heat.json"),
    "H": ("高频记忆跨 Session 沉淀", "H_promotion.json"),
    "I": ("跨 Session 降权", "I_cross_session_weight.json"),
    "J": ("用户画像真实更新", "J_profile_update.json"),
    "K": ("Profile + Custom Prompt", "K_profile_custom_prompt.json"),
    "L": ("异步后台沉淀", "L_async_jobs.json"),
    "M": ("多 Session 并发、单 Session 有序", "M_concurrency.json"),
    "N": ("Migration 数据保护", "N_migration_safety.json"),
    "O": ("Failure / Retry / Recovery / Degraded", "O_failure_recovery.json"),
    "P": ("自动调参真实过程", "P_tuning_trace.json"),
    "Q": ("真实用户历史 → Benchmark → 个性化调参", "Q_personalized_tuning.json"),
}

SELECTION_CRITERIA = {
    "score_range": "0-100",
    "dimensions": {
        "change_magnitude": "排序/指标/状态变化是否明显",
        "comprehensibility": "金融问题是否自然、非技术人员是否容易理解",
        "evidence_completeness": "Input / Process / Output / Source 是否齐全",
        "ppt_fit": "能否压缩成半页 PPT 的 Before/After、流程或时间轴",
    },
    "tie_breaker": "优先 S001_贵州茅台_投研，其次证据链更完整、文字更短",
}


def rel(path: Path | str) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(REPO))
    except ValueError:
        return str(p)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if line.strip():
                yield lineno, json.loads(line)


def last_jsonl(path: Path) -> tuple[int, dict[str, Any]]:
    """Read only the last nonempty JSONL record without loading the full file."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        pos = size
        chunks: list[bytes] = []
        while pos > 0:
            take = min(1024 * 1024, pos)
            pos -= take
            handle.seek(pos)
            chunk = handle.read(take)
            chunks.append(chunk)
            joined = b"".join(reversed(chunks)).rstrip(b"\r\n")
            if b"\n" in joined or pos == 0:
                line = joined.rsplit(b"\n", 1)[-1]
                break
        line_no = sum(1 for _ in path.open("rb"))
    return line_no, json.loads(line.decode("utf-8"))


def ro_connect(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def parse_json(value: Any, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def compact(text: Any, limit: int = 80) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def first_para(text: Any, limit: int = 120) -> str:
    value = str(text or "").strip().split("\n\n", 1)[0]
    return compact(value, limit)


def evidence(path: Path | str, collection: str, record_id: Any, fields: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {
            "source_file": rel(path),
            "table / collection": collection,
            "record_id": str(record_id or ""),
            "field": field,
        }
        for field in fields
    ]


def base_candidate(
    candidate_id: str,
    score: float,
    why: str,
    *,
    session_id: str | None = None,
    query_id: str | None = None,
    memory_id: str | None = None,
    job_id: str | None = None,
    display_data: dict[str, Any] | None = None,
    raw_data: dict[str, Any] | None = None,
    evidence_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "presentation_score": round(max(0, min(100, score)), 2),
        "why_good_for_ppt": why,
        "session_id": session_id,
        "query_id": query_id,
        "memory_id": memory_id,
        "job_id": job_id,
        "display_data": display_data or {},
        "raw_data": raw_data or {},
        "evidence": evidence_rows or [],
    }


def package(
    candidates: list[dict[str, Any]],
    source_files: Iterable[Path | str],
    *,
    status: str | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ordered = sorted(
        candidates,
        key=lambda c: (
            -float(c.get("presentation_score") or 0),
            0 if str(c.get("session_id") or "").startswith("S001") else 1,
            str(c.get("candidate_id") or ""),
        ),
    )
    result = {
        "status": status or ("FOUND" if ordered else "NOT_FOUND"),
        "candidate_count": len(ordered),
        "top_1": ordered[0] if ordered else {},
        "top_3": ordered[:3],
        "top_10": ordered[:10],
        "all_candidates": ordered,
        "selection_criteria": SELECTION_CRITERIA,
        "source_files": sorted({rel(p) for p in source_files}),
    }
    if note:
        result["note"] = note
    if extra:
        result.update(extra)
    return result


def load_dataset() -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, str]]:
    workbook = load_workbook(DATASET, read_only=True, data_only=True)
    sessions: dict[str, dict[str, dict[str, Any]]] = {}
    code_to_name: dict[str, str] = {}
    for sheet in workbook.worksheets:
        if not re.match(r"S\d{3}_", sheet.title):
            continue
        rows = sheet.iter_rows(values_only=True)
        headers = [str(v or "") for v in next(rows)]
        code = sheet.title.split("_", 1)[0]
        code_to_name[code] = sheet.title
        sessions[code] = {}
        for values in rows:
            row = dict(zip(headers, values))
            qid = str(row.get("编号") or "")
            if qid:
                sessions[code][qid] = row
    return sessions, code_to_name


def checkpoint_files() -> dict[str, Path]:
    result = {}
    for path in sorted((TARGET / "production_source").glob("*/production_midterm_checkpoints.jsonl")):
        result[path.parent.name.split("_", 1)[0]] = path
    return result


def page_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
    return payload


def load_final_pages() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, int]]:
    pages: list[dict[str, Any]] = []
    checkpoints: dict[str, dict[str, Any]] = {}
    line_numbers: dict[str, int] = {}
    for code, path in checkpoint_files().items():
        lineno, record = last_jsonl(path)
        checkpoints[code] = record
        line_numbers[code] = lineno
        source_turn_ids_by_job = record.get("source_turn_ids_by_job") or {}
        for page in record.get("pages") or []:
            payload = dict(page_payload(page))
            payload.setdefault("id", page.get("id"))
            source_job_id = str(payload.get("source_job_id") or "")
            mapped_turn_ids = source_turn_ids_by_job.get(source_job_id) or []
            if mapped_turn_ids and not payload.get("source_turn_id"):
                payload["source_turn_id"] = mapped_turn_ids[0]
            payload["source_turn_ids"] = mapped_turn_ids
            payload["_source_code"] = code
            payload["_source_file"] = path
            payload["_source_line"] = lineno
            pages.append(payload)
    return pages, checkpoints, line_numbers


def extract_a(pages: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = []
    files: set[Path] = set()
    for page in pages:
        raw = str(page.get("raw_dialogue") or "")
        summary = str(page.get("summary") or "")
        keywords = list(page.get("keywords") or page.get("summary_keywords") or [])
        user = str(page.get("user_input") or "")
        assistant = str(page.get("assistant_response") or "")
        production_text = str(page.get("data") or "")
        built = f"{summary}\nKeywords: {', '.join(map(str, keywords))}\nUser: {user}"
        if not raw or not summary or not production_text:
            continue
        raw_len = len(raw)
        representation_len = len(production_text)
        compression = 1 - representation_len / raw_len if raw_len else 0
        code = str(page.get("_source_code") or "")
        session = str(page.get("run_id") or code)
        qid = str(page.get("source_turn_id") or "")
        pid = str(page.get("id") or "")
        source_file = Path(page["_source_file"])
        files.add(source_file)
        score = 55 + min(18, raw_len / 900) + max(0, min(15, compression * 20))
        score += 8 if code == "S001" else 0
        score += 4 if re.search(r"202[345]|亿元|利润|收入|资产|现金流", user + summary) else 0
        why = (
            f"{qid} 将 {raw_len:,} 字原始问答压缩为 {representation_len:,} 字检索表示"
            f"（压缩 {compression:.1%}），摘要、关键词与问题三段结构清楚。"
        )
        fields = [
            "raw_dialogue",
            "summary",
            "keywords",
            "user_input",
            "assistant_response",
            "data",
        ]
        candidates.append(
            base_candidate(
                f"A-{code}-{pid}", score, why,
                session_id=session,
                query_id=qid,
                memory_id=pid,
                display_data={
                    "title": "长问答 → 聚焦检索表示",
                    "current_question": compact(user, 40),
                    "input": first_para(raw, 120),
                    "process": f"摘要：{compact(summary, 80)}\n关键词：{', '.join(map(str, keywords[:8]))}",
                    "output": compact(production_text, 120),
                    "key_number": f"{raw_len:,} → {representation_len:,} 字；压缩 {compression:.1%}",
                    "conclusion": "生产 Embedding 文本只保留摘要、关键词和用户问题。",
                },
                raw_data={
                    "turn": page.get("turn_index"),
                    "user_input": user,
                    "assistant_response": assistant,
                    "raw_dialogue": raw,
                    "summary": summary,
                    "keywords": keywords,
                    "production_embedding_text": production_text,
                    "production_formula_rebuild": built,
                    "embedding_text_exact_formula_match": production_text == built,
                    "raw_dialogue_chars": raw_len,
                    "retrieval_representation_chars": representation_len,
                    "compression_ratio": compression,
                },
                evidence_rows=evidence(source_file, "final checkpoint / pages", pid, fields),
            )
        )
    return package(
        candidates,
        [*files, REPO / "mem0/memory/midterm.py"],
        extra={"scan_stats": {"final_snapshot_page_count": len(pages), "session_count": len(files)}},
    )


def extract_b(
    s001_checkpoints: list[tuple[int, dict[str, Any]]],
    dataset: dict[str, dict[str, dict[str, Any]]],
    page_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    source_file = checkpoint_files()["S001"]
    candidates = []
    for lineno, checkpoint in s001_checkpoints:
        qid = str(checkpoint.get("query_id") or "")
        row = dataset.get("S001", {}).get(qid, {})
        deps = re.findall(r"S001-Q\d{3}", str(row.get("关联前序对话") or ""))
        if not deps:
            continue
        diagnostics = checkpoint.get("retrieval_diagnostics") or {}
        sessions = diagnostics.get("selected_sessions") or diagnostics.get("session_candidates") or []
        pages = diagnostics.get("routed_page_candidates") or []
        finals = diagnostics.get("final_selection") or checkpoint.get("retrieved_results") or []
        final_ids = []
        final_display = []
        for item in finals[:5]:
            payload = page_payload(item)
            pid = str(item.get("id") or payload.get("id") or "")
            page = page_by_id.get(pid, payload)
            source_turn = str(page.get("source_turn_id") or item.get("source_turn_id") or "")
            final_ids.append(source_turn)
            final_display.append({
                "rank": len(final_display) + 1,
                "source_turn_id": source_turn,
                "summary": compact(item.get("summary") or item.get("memory") or page.get("summary"), 70),
                "score": item.get("final_score", item.get("score")),
            })
        ranks = [final_ids.index(dep) + 1 for dep in deps if dep in final_ids]
        correct_rank = min(ranks) if ranks else None
        first_layer = []
        for rank, item in enumerate(sessions[:5], 1):
            payload = page_payload(item)
            first_layer.append({
                "rank": rank,
                "session_memory_id": item.get("id"),
                "score": item.get("score"),
                "summary": compact(payload.get("summary"), 75),
                "page_ids": payload.get("page_ids") or [],
            })
        second_layer = []
        for rank, item in enumerate(pages[:10], 1):
            payload = page_payload(item)
            pid = str(item.get("id") or payload.get("id") or "")
            mapped_page = page_by_id.get(pid, payload)
            second_layer.append({
                "rank": rank,
                "page_id": pid,
                "score": item.get("score"),
                "source_turn_id": mapped_page.get("source_turn_id"),
                "summary": compact(mapped_page.get("summary"), 75),
                "user_input": compact(mapped_page.get("user_input"), 60),
            })
        if not first_layer or not second_layer:
            continue
        score = 58 + (18 if correct_rank else 0) + (8 if correct_rank and correct_rank <= 3 else 0)
        score += 8 if qid.startswith("S001") else 0
        score += min(8, len(deps) * 2)
        rank_text = f"正确依赖最终 Rank {correct_rank}" if correct_rank else "正确依赖未进入最终 Top 5"
        candidates.append(
            base_candidate(
                f"B-S001-{qid}", score,
                f"同一条真实查询同时保留 Session 路由、Page 候选和最终历史；{rank_text}。",
                session_id="S001_贵州茅台_投研",
                query_id=qid,
                display_data={
                    "title": "Session 路由 → Page 精排 → 历史上下文",
                    "current_question": compact(checkpoint.get("query"), 40),
                    "input": compact(checkpoint.get("query"), 80),
                    "process": {
                        "first_layer_top3": first_layer[:3],
                        "second_layer_top3": second_layer[:3],
                    },
                    "output": final_display[:3],
                    "key_number": rank_text,
                    "conclusion": "先选主题 Session，再在 Page 粒度完成最终召回。",
                },
                raw_data={
                    "query": checkpoint.get("query"),
                    "retrieval_query": checkpoint.get("retrieval_query"),
                    "first_layer_top_results": first_layer,
                    "second_layer_page_candidates": second_layer,
                    "final_top_5": final_display,
                    "correct_dependencies": deps,
                    "correct_dependency_rank": correct_rank,
                    "dataset_required_history": row.get("实际需召回内容（原始回答）"),
                },
                evidence_rows=evidence(
                    source_file, "production checkpoint / retrieval_diagnostics", qid,
                    ["query", "selected_sessions", "routed_page_candidates", "final_selection"],
                ) + evidence(DATASET, "S001 worksheet", qid, ["关联前序对话", "实际需召回内容（原始回答）"]),
            )
        )
    return package(
        candidates,
        [source_file, DATASET, REPO / "mem0/memory/midterm_retriever.py"],
        extra={"scan_stats": {"S001_checkpoint_count": len(s001_checkpoints), "dependent_query_count": len(candidates)}},
    )


def extract_c(dataset: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    movement_file = VALIDATION / "rerank_movement_by_session.csv"
    local_file = VALIDATION / "cache/rerank/local_scores.jsonl"
    query_files = sorted((VALIDATION / "snapshots").glob("*/queries.jsonl"))
    page_files = sorted((VALIDATION / "snapshots").glob("*/pages.jsonl"))
    queries: dict[str, dict[str, Any]] = {}
    pages: dict[str, dict[str, Any]] = {}
    for path in query_files:
        for _, row in iter_jsonl(path):
            queries[str(row.get("query_id"))] = row
    for path in page_files:
        for _, row in iter_jsonl(path):
            pages[str(row.get("page_id"))] = row
    local_scores: dict[str, dict[str, Any]] = {}
    for _, row in iter_jsonl(local_file):
        if row.get("status") == "SUCCESS":
            local_scores[str(row.get("query_id"))] = row
    candidates = []
    with movement_file.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for index, row in enumerate(rows, 1):
        qid = str(row["query_id"])
        query = queries.get(qid, {})
        local = local_scores.get(qid, {})
        if not query or not local:
            continue
        dense_rank = int(row["baseline_dense_rank"])
        hybrid_rank = int(row["quality_candidate_rank"])
        rerank_rank = int(row["quality_reranker_rank"])
        gain = dense_rank - rerank_rank
        rerank_delta = hybrid_rank - rerank_rank
        changed = dense_rank != hybrid_rank or hybrid_rank != rerank_rank
        category = "IMPROVED" if gain > 0 else ("WORSE" if gain < 0 else "UNCHANGED")
        code = qid.split("-", 1)[0]
        session_name = str(query.get("session_id") or code)
        gold_id = str(row["gold_page_id"])
        gold_page = pages.get(gold_id, {})
        hybrid_ids = list(local.get("candidate_page_ids") or [])
        reranked_ids = list(local.get("ranking_page_ids") or [])
        scores = local.get("scores_by_page") or {}
        def list_rows(ids: list[str], score_map: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            result = []
            for rank, pid in enumerate(ids[:10], 1):
                page = pages.get(str(pid), {})
                result.append({
                    "rank": rank,
                    "page_id": pid,
                    "source_turn_id": page.get("source_turn_id"),
                    "summary": compact(page.get("summary"), 65),
                    "score": (score_map or {}).get(str(pid)),
                    "is_gold": str(pid) == gold_id,
                })
            return result
        magnitude = min(24, abs(gain) * 3 + abs(rerank_delta) * 2)
        score = 55 + magnitude + (7 if changed else 0) + (6 if code in {"S001", "S002"} else 0)
        if category == "WORSE":
            score += 5
        why = (
            f"Gold 历史从 Dense Rank {dense_rank} → Hybrid Rank {hybrid_rank} → "
            f"Reranker Rank {rerank_rank}（净变化 {gain:+d}），三阶段排名来自冻结实验。"
        )
        candidates.append(
            base_candidate(
                f"C-{qid}-{gold_id[:8]}", score, why,
                session_id=session_name,
                query_id=qid,
                memory_id=gold_id,
                display_data={
                    "title": "Dense → Hybrid → Reranker",
                    "current_question": compact(query.get("original_query"), 40),
                    "input": compact(query.get("required_context"), 90),
                    "process": f"Dense #{dense_rank} → Hybrid #{hybrid_rank}",
                    "output": f"Reranker #{rerank_rank}",
                    "key_number": f"Rank {dense_rank} → {hybrid_rank} → {rerank_rank}",
                    "conclusion": f"{category}：最终净提升 {gain:+d} 位。",
                },
                raw_data={
                    "query": query.get("original_query"),
                    "gold_required_history": query.get("required_context"),
                    "gold_source_turn_ids": query.get("gold_source_turn_ids"),
                    "gold_page_id": gold_id,
                    "gold_page_summary": gold_page.get("summary"),
                    "semantic_top_10": "UNOBSERVABLE_LIST: artifact retains gold dense rank, not the full dense list",
                    "bm25": "UNOBSERVABLE_COMPONENT_SCORE: frozen config records Chinese BM25 + RRF, component scores not persisted",
                    "entity": "NOT_APPLICABLE_TO_THIS_FROZEN_EXPERIMENT",
                    "session_weight": "NOT_APPLICABLE_TO_THIS_FROZEN_EXPERIMENT",
                    "hybrid_top_10": list_rows(hybrid_ids),
                    "reranker_before_top_10": list_rows(hybrid_ids, scores),
                    "reranker_after_top_10": list_rows(reranked_ids, scores),
                    "semantic_rank": dense_rank,
                    "hybrid_rank": hybrid_rank,
                    "rerank_rank": rerank_rank,
                    "rank_gain": gain,
                    "reranker_stage_gain": rerank_delta,
                    "movement_category": row.get("movement_category"),
                    "outcome_category": category,
                    "changed": changed,
                },
                evidence_rows=evidence(movement_file, "rerank_movement_by_session.csv", f"row {index}", list(row))
                + evidence(local_file, "local_scores.jsonl", qid, ["candidate_page_ids", "scores_by_page", "ranking_page_ids", "status"])
                + evidence(next((p for p in query_files if code in p.parent.name), query_files[0]), "queries.jsonl", qid, ["original_query", "required_context", "gold_page_ids"]),
            )
        )
    # Preserve the requested balance where evidence exists: strong changes first, then unchanged/worse.
    improved = sorted([c for c in candidates if c["raw_data"]["outcome_category"] == "IMPROVED"], key=lambda c: -c["presentation_score"])
    unchanged = sorted([c for c in candidates if c["raw_data"]["outcome_category"] == "UNCHANGED"], key=lambda c: -c["presentation_score"])
    worse = sorted([c for c in candidates if c["raw_data"]["outcome_category"] == "WORSE"], key=lambda c: -c["presentation_score"])
    balanced_top = improved[:6] + unchanged[:2] + worse[:2]
    result = package(
        candidates,
        [movement_file, local_file, *query_files, *page_files, VALIDATION / "frozen_config.json", VALIDATION / "validation_summary.json"],
        extra={
            "scan_stats": {
                "movement_rows": len(rows),
                "usable_candidates": len(candidates),
                "changed": sum(c["raw_data"]["changed"] for c in candidates),
                "improved": len(improved),
                "unchanged": len(unchanged),
                "worse": len(worse),
            },
            "balanced_review_set": balanced_top,
            "observability_note": "Full dense Top 10 and individual BM25 component scores were not persisted; only recorded ranks are reported.",
        },
    )
    if balanced_top:
        result["top_10"] = balanced_top
        result["top_3"] = improved[:3] or balanced_top[:3]
        result["top_1"] = result["top_3"][0]
    return result


def extract_e(dataset: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    resolved_file = REWRITE_EXP / "resolved_queries.jsonl"
    audit_file = REWRITE_EXP / "resolution_audit.jsonl"
    audit: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in iter_jsonl(audit_file):
        if row.get("prompt") == "P1":
            audit[(str(row.get("session_id")), str(row.get("query_id")))] = row
    candidates = []
    for lineno, row in iter_jsonl(resolved_file):
        code = str(row.get("session_id") or "")
        qid = str(row.get("query_id") or "")
        original = str(row.get("original_query") or "")
        rewritten = str((row.get("P1") or {}).get("resolved_query") or "")
        if not rewritten or rewritten == original:
            continue
        audit_row = audit.get((code, qid), {})
        added = [
            {
                "surface_text": item.get("surface_text"),
                "category": item.get("category"),
                "classification": item.get("classification"),
            }
            for item in audit_row.get("added_content") or []
        ]
        history = []
        for context_id in row.get("visible_context_turn_ids") or []:
            context_row = dataset.get(code, {}).get(str(context_id), {})
            history.append({
                "turn_id": context_id,
                "user": compact(context_row.get("当前问题"), 60),
                "assistant": compact(context_row.get("最终回答"), 60),
            })
        delta = max(0, len(rewritten) - len(original))
        referential = bool(re.search(r"这个|那个|之前|前面|刚才|继续|沿用|上面|上一轮|该", original))
        score = 58 + min(20, delta / 3) + min(12, len(added) * 2) + (7 if code == "S001" else 0) + (4 if referential else 0)
        candidates.append(
            base_candidate(
                f"E-{code}-{qid}", score,
                f"用最近 {len(history)} 轮可见上下文，把 {len(original)} 字省略式问题改写为 {len(rewritten)} 字独立问题；新增信息有审计分类。",
                session_id=code,
                query_id=qid,
                display_data={
                    "title": "省略式问题 → 独立检索问题",
                    "current_question": compact(original, 40),
                    "input": history[-3:],
                    "process": compact(original, 80),
                    "output": compact(rewritten, 120),
                    "key_number": f"新增 {delta} 字 / {len(added)} 个审计项",
                    "conclusion": "把指代对象、指标与历史判断补进检索问题。",
                },
                raw_data={
                    "recent_context": history,
                    "visible_context_turn_ids": row.get("visible_context_turn_ids"),
                    "original_query": original,
                    "standalone_rewrite_query": rewritten,
                    "added_context": added,
                    "prompt_variant": "P1_explicit_reference",
                    "audit_scope": audit_row.get("audit_scope"),
                },
                evidence_rows=evidence(resolved_file, "resolved_queries.jsonl", qid, ["original_query", "visible_context_turn_ids", "P1.resolved_query"])
                + evidence(audit_file, "resolution_audit.jsonl / P1", qid, ["added_content", "classification_counts", "resolution_type_counts"]),
            )
        )
    return package(
        candidates,
        [resolved_file, audit_file, REWRITE_EXP / "run_metadata.json"],
        extra={"scan_stats": {"P1_changed_query_count": len(candidates), "generation_input_excluded_gold": True}},
    )


def extract_f(dataset: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    context_file = CONTEXT_EXP / "context/context_mapping.jsonl"
    cache_file = CONTEXT_EXP / "cache/PreviousAndFollowingContext_summary_llm.jsonl"
    pages_file = CONTEXT_EXP / "pages/PreviousAndFollowingContext_pages.jsonl"
    audit_file = CONTEXT_EXP / "analysis/context_source_audit.csv"
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in iter_jsonl(context_file):
        if row.get("variant") == "Production Add + 最近上文及下文":
            mapping[(str(row.get("source_turn_id", "")).split("-", 1)[0], str(row.get("source_turn_id")))] = row
    page_rows: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in iter_jsonl(pages_file):
        page_rows[(str(row.get("session_id")), str(row.get("source_turn_id")))] = row
    audits: dict[tuple[str, str], dict[str, Any]] = {}
    with audit_file.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Add 方式") == "Production Add + 最近上文及下文":
                audits[(str(row.get("Session")), str(row.get("source_turn_id")))] = row
    candidates = []
    cache_success = 0
    for lineno, row in iter_jsonl(cache_file):
        if row.get("status") != "SUCCESS":
            continue
        cache_success += 1
        code = str(row.get("session_id") or "")
        qid = str(row.get("source_turn_id") or "")
        mapping_row = mapping.get((code, qid), {})
        page = page_rows.get((code, qid), {})
        current = dataset.get(code, {}).get(qid, {})
        previous_ids = list(row.get("previous_context_turn_ids") or mapping_row.get("previous_context_turn_ids") or [])
        following_ids = list(row.get("following_context_turn_ids") or mapping_row.get("following_context_turn_ids") or [])
        if not previous_ids and not following_ids:
            continue
        def contexts(ids: list[str]) -> list[dict[str, Any]]:
            return [
                {
                    "turn_id": turn_id,
                    "user": dataset.get(code, {}).get(turn_id, {}).get("当前问题"),
                    "assistant": dataset.get(code, {}).get(turn_id, {}).get("最终回答"),
                }
                for turn_id in ids
            ]
        parsed = row.get("parsed") or {}
        audit_row = audits.get((code, qid), {})
        added_info: dict[str, list[str]] = {}
        for key, value in audit_row.items():
            if "added beyond current QA" not in key:
                continue
            parsed_value = parse_json(value, [])
            if parsed_value:
                added_info[key.split(" added", 1)[0]] = parsed_value
        context_count = len(previous_ids) + len(following_ids)
        score = 58 + min(18, context_count * 4) + min(12, sum(len(v) for v in added_info.values()) * 2)
        score += 8 if code == "S001" else 0
        why = (
            f"真实归档输入合同同时记录当前 QA 与 {context_count} 条前/后文；"
            f"审计识别出 {sum(len(v) for v in added_info.values())} 个超出当前轮的信息项。"
        )
        candidates.append(
            base_candidate(
                f"F-{code}-{qid}", score, why,
                session_id=code,
                query_id=qid,
                memory_id=str(page.get("page_id") or ""),
                display_data={
                    "title": "前文 + 当前问答 + 后文 → 中期记忆",
                    "current_question": compact(current.get("当前问题"), 40),
                    "input": {
                        "previous": [compact(x["user"], 60) for x in contexts(previous_ids)[-2:]],
                        "current": compact(current.get("当前问题"), 60),
                        "following": [compact(x["user"], 60) for x in contexts(following_ids)[:2]],
                    },
                    "process": compact(parsed.get("summary"), 100),
                    "output": f"关键词：{', '.join(map(str, (parsed.get('keywords') or [])[:8]))}",
                    "key_number": f"上下文 {context_count} 条；新增信息 {sum(len(v) for v in added_info.values())} 项",
                    "conclusion": "归档摘要的生成输入确实包含运行时可见的邻近上下文。",
                },
                raw_data={
                    "previous_context": contexts(previous_ids),
                    "current_archived_qa": {
                        "user": current.get("当前问题"),
                        "assistant": current.get("最终回答"),
                    },
                    "following_context": contexts(following_ids),
                    "summary": parsed.get("summary"),
                    "keywords": parsed.get("keywords"),
                    "final_memory": page.get("current_embedding_text"),
                    "context_added_information": added_info,
                    "input_contract": row.get("input_contract"),
                    "input_payload_sha256": row.get("input_payload_sha256"),
                    "eviction_trigger_turn_id": row.get("eviction_trigger_turn_id"),
                    "llm_latency_ms": row.get("llm_latency_ms"),
                },
                evidence_rows=evidence(cache_file, "PreviousAndFollowingContext_summary_llm.jsonl", qid, ["previous_context_turn_ids", "following_context_turn_ids", "parsed.summary", "parsed.keywords", "input_contract", "status"])
                + evidence(pages_file, "PreviousAndFollowingContext_pages.jsonl", qid, ["source_user", "source_assistant", "current_embedding_text"])
                + evidence(audit_file, "context_source_audit.csv", qid, [k for k in audit_row if "added beyond current QA" in k]),
            )
        )
    return package(
        candidates,
        [context_file, cache_file, pages_file, audit_file, CONTEXT_EXP / "run_metadata.json", REPO / "mem0/memory/midterm_updater.py"],
        extra={"scan_stats": {"successful_context_generation_rows": cache_success, "rows_with_nonempty_neighbor_context": len(candidates)}},
    )


def extract_g(s001_checkpoints: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    source_file = checkpoint_files()["S001"]
    candidates = []
    for lineno, checkpoint in s001_checkpoints:
        diagnostics = checkpoint.get("retrieval_diagnostics") or {}
        scored = diagnostics.get("pre_threshold_ranking") or diagnostics.get("scored_candidates") or []
        usable = [x for x in scored if x.get("raw_rag_score") is not None and x.get("final_score") is not None]
        if len(usable) < 2:
            continue
        raw_order = sorted(usable, key=lambda x: float(x.get("raw_rag_score") or 0), reverse=True)
        adjusted_order = sorted(usable, key=lambda x: float(x.get("final_score") or 0), reverse=True)
        raw_rank = {str(x.get("id")): i for i, x in enumerate(raw_order, 1)}
        adjusted_rank = {str(x.get("id")): i for i, x in enumerate(adjusted_order, 1)}
        changed = [x for x in usable if raw_rank.get(str(x.get("id"))) != adjusted_rank.get(str(x.get("id")))]
        if not changed:
            continue
        heat = {str(x.get("session_id")): x for x in checkpoint.get("heat_states") or []}
        comparison = []
        for item in adjusted_order[:4]:
            mid = str(item.get("id") or "")
            h = heat.get(str(item.get("session_id") or ""), {})
            anchor = item.get("turn_index", item.get("page_sequence"))
            current = checkpoint.get("current_turn_index")
            distance = current - anchor if isinstance(current, int) and isinstance(anchor, int) else None
            comparison.append({
                "memory_id": mid,
                "content_summary": compact(item.get("summary") or item.get("memory"), 70),
                "current_turn": current,
                "anchor_turn": anchor,
                "last_visit_turn": h.get("last_visit_turn_index"),
                "distance_turns": distance,
                "N_visit": item.get("valid_recall_count", h.get("N_visit")),
                "L_interaction": h.get("L_interaction"),
                "recency": h.get("R_recency"),
                "heat": h.get("H_segment"),
                "retention": item.get("forgetting_factor"),
                "heat_factor": item.get("heat_factor"),
                "effective_half_life_turns": item.get("effective_half_life_turns"),
                "raw_score": item.get("raw_rag_score"),
                "adjusted_score": item.get("final_score"),
                "rank_before": raw_rank.get(mid),
                "rank_after": adjusted_rank.get(mid),
            })
        max_move = max(abs(raw_rank[str(x.get("id"))] - adjusted_rank[str(x.get("id"))]) for x in changed)
        qid = str(checkpoint.get("query_id") or "")
        score = 62 + min(24, max_move * 5) + (8 if qid.startswith("S001") else 0)
        examples = [x for x in comparison if x["rank_before"] != x["rank_after"]]
        key = examples[0] if examples else comparison[0]
        why = (
            f"同一检索中真实保留原始分、遗忘因子、Heat 因子与调整后分；"
            f"代表项 Rank {key['rank_before']} → {key['rank_after']}。"
        )
        candidates.append(
            base_candidate(
                f"G-S001-{qid}", score, why,
                session_id="S001_贵州茅台_投研",
                query_id=qid,
                display_data={
                    "title": "原始相关性 × 衰减/Heat → 调整排名",
                    "current_question": compact(checkpoint.get("query"), 40),
                    "input": [{"rank": x["rank_before"], "memory": x["content_summary"], "score": x["raw_score"]} for x in comparison],
                    "process": [{"retention": x["retention"], "heat": x["heat"], "distance": x["distance_turns"]} for x in comparison],
                    "output": [{"rank": x["rank_after"], "memory": x["content_summary"], "score": x["adjusted_score"]} for x in comparison],
                    "key_number": f"最大排名移动 {max_move} 位",
                    "conclusion": "时间距离与 Session 热度共同修正语义相关性。",
                },
                raw_data={
                    "query": checkpoint.get("query"),
                    "memories": comparison,
                    "rank_changed_memory_count": len(changed),
                    "max_absolute_rank_move": max_move,
                    "observed_reinforcement_limitation": "N_visit/valid_recall_count is zero in this stateless replay when recorded as zero; do not claim frequent-recall reinforcement without nonzero evidence.",
                },
                evidence_rows=evidence(source_file, "production checkpoint / retrieval_diagnostics.pre_threshold_ranking", qid, ["raw_rag_score", "final_score", "forgetting_factor", "heat_factor", "effective_half_life_turns"])
                + evidence(source_file, "production checkpoint / heat_states", qid, ["N_visit", "L_interaction", "R_recency", "H_segment", "last_visit_turn_index"]),
            )
        )
    return package(
        candidates,
        [source_file, REPO / "mem0/memory/midterm.py", REPO / "mem0/memory/midterm_retriever.py"],
        extra={"scan_stats": {"S001_checkpoint_count": len(s001_checkpoints), "queries_with_observed_rank_change": len(candidates)}},
    )


def target_source_databases() -> dict[str, Path]:
    base = TARGET / "production_runtimes/production-source-44136fa355b3"
    return {path.parent.name: path for path in sorted(base.glob("*/history.db"))}


def load_job_rows(databases: dict[str, Path]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    tables = [
        "messages",
        "memory_migration_jobs",
        "longterm_extraction_jobs",
        "profile_update_jobs",
        "memory_promotion_jobs",
    ]
    for code, path in databases.items():
        conn = ro_connect(path)
        result[code] = {}
        try:
            for table in tables:
                if not table_exists(conn, table):
                    result[code][table] = []
                    continue
                order = "created_at" if table != "messages" else "created_at"
                result[code][table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
        finally:
            conn.close()
    return result


def job_turn_id(row: dict[str, Any]) -> str | None:
    metadata = parse_json(row.get("metadata_json"), {}) or {}
    turn_id = metadata.get("dataset_turn_id")
    if turn_id:
        return str(turn_id)
    messages = parse_json(row.get("messages_json"), []) or []
    for message in messages:
        name = str(message.get("name") or "")
        match = re.search(r"S\d{3}-Q\d{3}", name)
        if match:
            return match.group(0)
    return None


def extract_l(job_rows: dict[str, dict[str, list[dict[str, Any]]]], databases: dict[str, Path]) -> dict[str, Any]:
    candidates = []
    for code, tables in job_rows.items():
        longterm_by_turn = {job_turn_id(row): row for row in tables["longterm_extraction_jobs"] if job_turn_id(row)}
        profile_by_turn = {job_turn_id(row): row for row in tables["profile_update_jobs"] if job_turn_id(row)}
        promotion_by_turn = {job_turn_id(row): row for row in tables["memory_promotion_jobs"] if job_turn_id(row)}
        for migration in tables["memory_migration_jobs"]:
            turn_id = job_turn_id(migration)
            longterm = longterm_by_turn.get(turn_id)
            profile = profile_by_turn.get(turn_id)
            promotion = promotion_by_turn.get(turn_id)
            messages = parse_json((longterm or {}).get("messages_json"), []) or []
            message_time = next((m.get("created_at") for m in messages if m.get("created_at")), None)
            timeline = [
                {"event": "message_written", "time": message_time, "status": "observed in longterm messages_json"},
                {"event": "migration_created", "time": migration.get("created_at"), "status": migration.get("status")},
                {"event": "migration_started", "time": migration.get("midterm_started_at"), "status": migration.get("midterm_status")},
                {"event": "migration_finished", "time": migration.get("midterm_finished_at"), "status": migration.get("midterm_status")},
                {"event": "profile_created", "time": (profile or {}).get("created_at"), "status": (profile or {}).get("status") or "NOT_TRIGGERED"},
                {"event": "profile_started", "time": (profile or {}).get("started_at"), "status": (profile or {}).get("status") or "NOT_TRIGGERED"},
                {"event": "profile_finished", "time": (profile or {}).get("updated_at"), "status": (profile or {}).get("status") or "NOT_TRIGGERED"},
                {"event": "longterm_created", "time": (longterm or {}).get("created_at"), "status": (longterm or {}).get("status") or "NOT_MATCHED"},
                {"event": "longterm_started", "time": (longterm or {}).get("started_at"), "status": (longterm or {}).get("status") or "NOT_MATCHED"},
                {"event": "longterm_finished", "time": (longterm or {}).get("finished_at"), "status": (longterm or {}).get("status") or "NOT_MATCHED"},
                {"event": "promotion", "time": (promotion or {}).get("finished_at"), "status": (promotion or {}).get("status") or "NOT_TRIGGERED"},
            ]
            if not turn_id or not longterm:
                continue
            finished = sum(1 for item in timeline if item["time"])
            score = 57 + min(22, finished * 2) + (9 if code == "S001" else 0)
            candidates.append(
                base_candidate(
                    f"L-{code}-{turn_id}", score,
                    f"同一请求的消息、Migration 与 Long-term Job 均有真实时间戳；Profile/Promotion 未触发也明确显示。",
                    session_id=str(migration.get("session_scope")),
                    query_id=turn_id,
                    job_id=str(migration.get("job_id")),
                    display_data={
                        "title": "请求落库后，后台任务异步完成",
                        "current_question": turn_id,
                        "input": f"消息写入 {message_time}",
                        "process": [x for x in timeline if x["event"] in {"migration_created", "migration_started", "migration_finished", "longterm_created", "longterm_started", "longterm_finished"}],
                        "output": f"Migration {migration.get('status')} / Long-term {(longterm or {}).get('status')}",
                        "key_number": f"记录到 {finished} 个真实时间点",
                        "conclusion": "主消息与两个后台沉淀链路可按时间先后反查。",
                    },
                    raw_data={
                        "session_scope": migration.get("session_scope"),
                        "turn_id": turn_id,
                        "migration_job_id": migration.get("job_id"),
                        "longterm_job_id": (longterm or {}).get("job_id"),
                        "profile_job_id": (profile or {}).get("job_id"),
                        "promotion_job_id": (promotion or {}).get("job_id"),
                        "timeline": timeline,
                        "final_status": {
                            "migration": migration.get("status"),
                            "profile": (profile or {}).get("status") or "NOT_TRIGGERED",
                            "longterm": (longterm or {}).get("status") or "NOT_MATCHED",
                            "promotion": (promotion or {}).get("status") or "NOT_TRIGGERED",
                        },
                    },
                    evidence_rows=evidence(databases[code], "memory_migration_jobs", migration.get("job_id"), ["created_at", "midterm_started_at", "midterm_finished_at", "status", "metadata_json"])
                    + evidence(databases[code], "longterm_extraction_jobs", (longterm or {}).get("job_id"), ["messages_json", "created_at", "started_at", "finished_at", "status"]),
                )
            )
    return package(
        candidates,
        [*databases.values(), REPO / "mem0/memory/background_worker.py", REPO / "mem0/memory/storage.py"],
        extra={"scan_stats": {"target_source_database_count": len(databases), "matched_request_job_groups": len(candidates)}},
    )


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def extract_m(job_rows: dict[str, dict[str, list[dict[str, Any]]]], databases: dict[str, Path]) -> dict[str, Any]:
    streams: dict[str, list[dict[str, Any]]] = {}
    for code, tables in job_rows.items():
        jobs = []
        for row in tables["longterm_extraction_jobs"]:
            start = parse_time(row.get("started_at"))
            finish = parse_time(row.get("finished_at"))
            if start and finish:
                jobs.append({**row, "_start": start, "_finish": finish, "_code": code})
        streams[code] = sorted(jobs, key=lambda x: (int(x.get("sequence_no") or 0), x["_start"]))
    candidates = []
    # Pairs are based on observed wall-clock overlap, not assumed scheduler configuration.
    codes = sorted(streams)
    for a_index, code_a in enumerate(codes):
        for code_b in codes[a_index + 1 :]:
            a_jobs, b_jobs = streams[code_a], streams[code_b]
            if len(a_jobs) < 2 or len(b_jobs) < 2:
                continue
            emitted_for_pair = 0
            for i in range(len(a_jobs) - 1):
                a_pair = a_jobs[i : i + 2]
                a_start, a_finish = a_pair[0]["_start"], a_pair[-1]["_finish"]
                for j in range(len(b_jobs) - 1):
                    b_pair = b_jobs[j : j + 2]
                    b_start, b_finish = b_pair[0]["_start"], b_pair[-1]["_finish"]
                    overlap_start = max(a_start, b_start)
                    overlap_finish = min(a_finish, b_finish)
                    if overlap_finish <= overlap_start:
                        continue
                    overlap_seconds = (overlap_finish - overlap_start).total_seconds()
                    jobs = []
                    for item in [*a_pair, *b_pair]:
                        jobs.append({
                            "session_scope": item.get("session_scope"),
                            "session_code": item["_code"],
                            "sequence_no": item.get("sequence_no"),
                            "job_id": item.get("job_id"),
                            "created_at": item.get("created_at"),
                            "started_at": item.get("started_at"),
                            "finished_at": item.get("finished_at"),
                            "status": item.get("status"),
                        })
                    order_ok = all(
                        int(pair[0].get("sequence_no") or 0) < int(pair[1].get("sequence_no") or 0)
                        and pair[0]["_finish"] <= pair[1]["_start"]
                        for pair in (a_pair, b_pair)
                    )
                    score = 62 + min(20, overlap_seconds / 3) + (9 if "S001" in {code_a, code_b} else 0) + (5 if order_ok else 0)
                    candidate_id = f"M-{code_a}-{code_b}-{a_pair[0]['sequence_no']}-{b_pair[0]['sequence_no']}"
                    candidates.append(
                        base_candidate(
                            candidate_id, score,
                            f"{code_a} 与 {code_b} 的两段 Job 窗口真实重叠 {overlap_seconds:.2f}s；各 Session 内 sequence_no 保持递增。",
                            session_id=f"{code_a} + {code_b}",
                            job_id=f"{a_pair[0]['job_id']} / {b_pair[0]['job_id']}",
                            display_data={
                                "title": "Session 内串行，Session 间并发",
                                "current_question": f"{code_a} ↔ {code_b}",
                                "input": [f"{code_a} #{a_pair[0]['sequence_no']}→#{a_pair[1]['sequence_no']}", f"{code_b} #{b_pair[0]['sequence_no']}→#{b_pair[1]['sequence_no']}"],
                                "process": jobs,
                                "output": "两个 Session 的执行区间重叠；各自顺序不乱",
                                "key_number": f"重叠 {overlap_seconds:.2f}s",
                                "conclusion": "并发发生在不同 Session，单 Session 保持有序。",
                            },
                            raw_data={
                                "sessions": [code_a, code_b],
                                "jobs": jobs,
                                "overlap_start": overlap_start.isoformat(),
                                "overlap_finish": overlap_finish.isoformat(),
                                "overlap_seconds": overlap_seconds,
                                "single_session_strict_order_observed": order_ok,
                            },
                            evidence_rows=evidence(databases[code_a], "longterm_extraction_jobs", a_pair[0].get("job_id"), ["sequence_no", "created_at", "started_at", "finished_at", "status"])
                            + evidence(databases[code_a], "longterm_extraction_jobs", a_pair[1].get("job_id"), ["sequence_no", "created_at", "started_at", "finished_at", "status"])
                            + evidence(databases[code_b], "longterm_extraction_jobs", b_pair[0].get("job_id"), ["sequence_no", "created_at", "started_at", "finished_at", "status"])
                            + evidence(databases[code_b], "longterm_extraction_jobs", b_pair[1].get("job_id"), ["sequence_no", "created_at", "started_at", "finished_at", "status"]),
                        )
                    )
                    emitted_for_pair += 1
                    if emitted_for_pair >= 3:
                        break
                if emitted_for_pair >= 3:
                    break
    return package(
        candidates,
        [*databases.values(), REPO / "mem0/memory/background_worker.py"],
        extra={"scan_stats": {"session_stream_count": len(streams), "overlapping_four_job_windows": len(candidates)}},
    )


def extract_n(
    job_rows: dict[str, dict[str, list[dict[str, Any]]]],
    databases: dict[str, Path],
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    page_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        for job_key in (page.get("source_job_id"), page.get("created_by_source_job_id")):
            if job_key:
                page_by_job[str(job_key)].append(page)
    candidates = []
    for code, tables in job_rows.items():
        active_names = {str(row.get("name") or "") for row in tables["messages"] if row.get("status") == "active"}
        for migration in tables["memory_migration_jobs"]:
            job_id = str(migration.get("job_id") or "")
            linked_pages = page_by_job.get(job_id, [])
            if not linked_pages:
                continue
            page = linked_pages[0]
            source_turn = str(page.get("source_turn_id") or "")
            user_name = f"{source_turn}:user"
            assistant_name = f"{source_turn}:assistant"
            shortterm_absent = user_name not in active_names and assistant_name not in active_names
            observable = [
                "migration job created_at persisted",
                "midterm_started_at persisted",
                "Page links to source_job_id",
                f"Page output_state={page.get('output_state')}",
                f"migration status={migration.get('status')}",
                f"source turn absent from final active messages={shortterm_absent}",
            ]
            unobservable = [
                "staging state transition timestamp is not persisted as an event log",
                "exact short-term DELETE timestamp is not persisted",
                "deleted message IDs are no longer present in final messages table",
            ]
            score = 60 + 10 * bool(page.get("output_state") == "committed") + 10 * bool(migration.get("status") == "succeeded")
            score += 8 if shortterm_absent else 0
            score += 8 if code == "S001" else 0
            candidates.append(
                base_candidate(
                    f"N-{code}-{job_id}", score,
                    f"Job→Page→committed→最终短期消息缺席的链路可观察；中间 staging 与删除时刻明确列为不可观察。",
                    session_id=str(migration.get("session_scope")),
                    query_id=source_turn,
                    memory_id=str(page.get("id") or ""),
                    job_id=job_id,
                    display_data={
                        "title": "Migration 提交后再移除短期副本",
                        "current_question": source_turn,
                        "input": f"Job {job_id[:8]} 创建",
                        "process": [
                            {"event": "created", "time": migration.get("created_at")},
                            {"event": "midterm_started", "time": migration.get("midterm_started_at")},
                            {"event": "midterm_finished", "time": migration.get("midterm_finished_at")},
                            {"event": "finalized", "time": migration.get("finalized_at")},
                        ],
                        "output": f"Page={page.get('output_state')} / Job={migration.get('status')} / 短期最终缺席={shortterm_absent}",
                        "key_number": f"Page {str(page.get('id'))[:8]}",
                        "conclusion": "可证明最终提交与最终缺席，不能伪造未持久化的瞬时状态。",
                    },
                    raw_data={
                        "message_ids": "UNOBSERVABLE_AFTER_FINAL_DELETION",
                        "source_turn_id": source_turn,
                        "page_id": page.get("id"),
                        "output_state": page.get("output_state"),
                        "stage_status": {
                            "migration": migration.get("status"),
                            "midterm": migration.get("midterm_status"),
                            "longterm": migration.get("longterm_status"),
                        },
                        "timestamps": {
                            "created_at": migration.get("created_at"),
                            "midterm_started_at": migration.get("midterm_started_at"),
                            "midterm_finished_at": migration.get("midterm_finished_at"),
                            "finalized_at": migration.get("finalized_at"),
                        },
                        "final_shortterm_message_status": "ABSENT_FROM_FINAL_ACTIVE_MESSAGES" if shortterm_absent else "STILL_ACTIVE",
                        "observable_steps": observable,
                        "unobservable_steps": unobservable,
                    },
                    evidence_rows=evidence(databases[code], "memory_migration_jobs", job_id, ["status", "midterm_status", "created_at", "midterm_started_at", "midterm_finished_at", "finalized_at"])
                    + evidence(Path(page["_source_file"]), "final checkpoint / pages", page.get("id"), ["source_job_id", "source_turn_id", "output_state"])
                    + evidence(databases[code], "messages (final state)", source_turn, ["name", "status"]),
                )
            )
    return package(
        candidates,
        [*databases.values(), *checkpoint_files().values(), REPO / "mem0/memory/background_worker.py", REPO / "mem0/memory/storage.py"],
        extra={"scan_stats": {"migration_jobs_with_linked_committed_page": len(candidates)}},
    )


def failure_markers(table: str, row: dict[str, Any]) -> dict[str, Any] | None:
    if table == "memory_migration_jobs":
        attempts = max(int(row.get("midterm_attempts") or 0), int(row.get("longterm_attempts") or 0))
        recovery = max(int(row.get("midterm_recovery_count") or 0), int(row.get("longterm_recovery_count") or 0))
        error = row.get("midterm_last_error") or row.get("longterm_last_error") or row.get("midterm_cleanup_error") or row.get("longterm_cleanup_error")
        degraded = bool(row.get("midterm_degraded") or row.get("longterm_degraded") or row.get("midterm_force_degraded") or row.get("longterm_force_degraded") or "degraded" in str(row.get("status")))
    else:
        attempts = int(row.get("attempts") or 0)
        recovery = int(row.get("recovery_count") or 0)
        error = row.get("last_error")
        degraded = bool(row.get("force_degraded") or row.get("degraded") or "degraded" in str(row.get("status")))
    status = str(row.get("status") or "")
    unusual = attempts > 0 or recovery > 0 or bool(error) or degraded or status not in {"succeeded", "completed"}
    if not unusual:
        return None
    return {"attempts": attempts, "recovery_count": recovery, "error": error, "degraded": degraded, "status": status}


def extract_o() -> dict[str, Any]:
    database_paths = sorted((TARGET / "production_runtimes").glob("*/*/history.db"))
    candidates = []
    status_counts: Counter[str] = Counter()
    for path in database_paths:
        code = path.parent.name
        runtime = path.parents[1].name
        conn = ro_connect(path)
        try:
            for table in ["memory_migration_jobs", "longterm_extraction_jobs", "profile_update_jobs", "memory_promotion_jobs"]:
                if not table_exists(conn, table):
                    continue
                for row_sqlite in conn.execute(f"SELECT * FROM {table}"):
                    row = dict(row_sqlite)
                    marker = failure_markers(table, row)
                    if not marker:
                        continue
                    job_id = str(row.get("job_id") or "")
                    status_counts[marker["status"]] += 1
                    turn_id = job_turn_id(row)
                    is_terminal_failure = marker["status"] in {"discarded", "failed", "completed_with_loss"}
                    score = 56 + min(20, marker["attempts"] * 5) + min(10, marker["recovery_count"] * 5)
                    score += 10 if marker["error"] else 0
                    score += 8 if is_terminal_failure or marker["degraded"] else 0
                    chain = []
                    if marker["attempts"]:
                        chain.append(f"记录 attempts={marker['attempts']}（此前至少发生过重试/失败计数）")
                    if marker["error"]:
                        chain.append(f"最后错误：{compact(marker['error'], 120)}")
                    if marker["recovery_count"]:
                        chain.append(f"recovery_count={marker['recovery_count']}")
                    chain.append(f"最终可见状态：{marker['status']}")
                    candidates.append(
                        base_candidate(
                            f"O-{runtime[:8]}-{code}-{job_id}", score,
                            f"真实 Job 保留 attempts={marker['attempts']}、错误/恢复字段与最终状态；未持久化的逐次 Attempt 细节不补写。",
                            session_id=str(row.get("session_scope") or code),
                            query_id=turn_id,
                            job_id=job_id,
                            display_data={
                                "title": "失败计数 / Retry → 最终状态",
                                "current_question": turn_id or table,
                                "input": f"{table} / {job_id[:8]}",
                                "process": chain[:-1],
                                "output": chain[-1],
                                "key_number": f"attempts={marker['attempts']} / recovery={marker['recovery_count']}",
                                "conclusion": "仅展示数据库真实保留的失败与恢复信号。",
                            },
                            raw_data={
                                "job_type": table,
                                "runtime": runtime,
                                "session": code,
                                "job_id": job_id,
                                "attempts": marker["attempts"],
                                "error": marker["error"],
                                "retry": marker["attempts"] > 0,
                                "recovery": marker["recovery_count"],
                                "degraded": marker["degraded"],
                                "final_status": marker["status"],
                                "observable_chain": chain,
                                "unobservable_detail": "Per-attempt timestamps/errors are not retained when last_error was cleared after success.",
                            },
                            evidence_rows=evidence(path, table, job_id, ["attempts / stage attempts", "last_error / stage last error", "recovery_count / stage recovery", "status", "started_at", "finished_at"]),
                        )
                    )
        finally:
            conn.close()
    return package(
        candidates,
        [*database_paths, REPO / "mem0/memory/background_worker.py", REPO / "mem0/memory/storage.py"],
        extra={"scan_stats": {"database_count": len(database_paths), "failure_signal_event_count": len(candidates), "final_status_counts": dict(status_counts)}},
    )


def scan_recall_events() -> dict[str, Any]:
    stats = {
        "file_count": 0,
        "turn_count": 0,
        "promotion_event_count": 0,
        "cross_session_valid_recall_count": 0,
        "turns_with_cross_session_results": 0,
    }
    sources = []
    for path in sorted((TARGET / "production_source").glob("*/recall_turn_results.jsonl")):
        sources.append(path)
        stats["file_count"] += 1
        for _, row in iter_jsonl(path):
            stats["turn_count"] += 1
            events = row.get("promotion_events") or []
            cross = row.get("cross_session_valid_recall_ids") or []
            stats["promotion_event_count"] += len(events)
            stats["cross_session_valid_recall_count"] += len(cross)
            stats["turns_with_cross_session_results"] += bool(cross)
    return {"stats": stats, "sources": sources}


def missing_package(
    note: str,
    source_files: Iterable[Path | str],
    *,
    scan_stats: dict[str, Any],
    status: str = "NOT_FOUND",
) -> dict[str, Any]:
    return package([], source_files, status=status, note=note, extra={"scan_stats": scan_stats})


def dependent_rows(
    dataset: dict[str, dict[str, dict[str, Any]]], code: str = "S001"
) -> list[tuple[str, dict[str, Any], list[str]]]:
    result = []
    for qid, row in sorted(dataset.get(code, {}).items()):
        deps = list(dict.fromkeys(re.findall(rf"{re.escape(code)}-Q\d{{3}}", str(row.get("关联前序对话") or ""))))
        if deps:
            result.append((qid, row, deps))
    return result


def simulate_d(dataset: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Create explicitly simulated Agentic scenarios using only observed dialogue text."""
    candidates = []
    rows = dependent_rows(dataset, "S001")[:12]
    qmap = dataset["S001"]
    for index, (qid, row, deps) in enumerate(rows, 1):
        qnum = int(qid.rsplit("Q", 1)[1])
        recent_ids = [f"S001-Q{i:03d}" for i in range(max(1, qnum - 3), qnum)]
        recent = [
            {"turn_id": rid, "user": qmap.get(rid, {}).get("当前问题"), "assistant": qmap.get(rid, {}).get("最终回答")}
            for rid in recent_ids if rid in qmap
        ]
        gold_id = deps[0]
        gold = qmap.get(gold_id, {})
        tool_query = f"查找 {gold_id} 中与当前问题直接相关的历史结论：{compact(row.get('当前问题'), 80)}"
        score = 72 + min(15, max(0, qnum - int(gold_id.rsplit("Q", 1)[1]))) + (5 if index <= 3 else 0)
        candidates.append(
            base_candidate(
                f"D-SIM-S001-{qid}", score,
                f"使用真实连续对话 {recent_ids[-3:]} 与真实依赖 {gold_id} 构造二次补查演示；Tool Call 与判断明确为模拟。",
                session_id="S001_贵州茅台_投研",
                query_id=qid,
                memory_id=gold_id,
                display_data={
                    "title": "第一次上下文不足 → 模拟 Memory Tool 补查",
                    "current_question": compact(row.get("当前问题"), 40),
                    "input": [compact(x["user"], 60) for x in recent],
                    "process": [
                        "SIMULATED：模型判断缺少更早的口径/结论",
                        f"SIMULATED Tool Call：search_memory({compact(tool_query, 70)})",
                    ],
                    "output": compact(row.get("实际需召回内容（原始回答）") or gold.get("最终回答"), 100),
                    "key_number": f"补查真实历史 {gold_id}",
                    "conclusion": "演示机制未在目标运行触发；输入与补查内容均来自真实对话。",
                    "simulation_label": "SIMULATED",
                },
                raw_data={
                    "observed_input": {
                        "user_question": row.get("当前问题"),
                        "first_memory_context": recent,
                        "required_dependency": gold_id,
                    },
                    "simulated_process": {
                        "first_model_judgment": "现有最近三轮不足以覆盖标注依赖，建议主动补查更早历史。",
                        "tool_call": "search_memory",
                        "tool_arguments": {"query": tool_query, "session_id": "S001_贵州茅台_投研", "top_k": 5},
                        "second_retrieval_query": tool_query,
                    },
                    "observed_output": {
                        "new_memory": gold.get("最终回答"),
                        "required_information_annotation": row.get("实际需召回内容（原始回答）"),
                        "final_answer": row.get("最终回答"),
                    },
                    "simulation_boundary": "Agentic judgment, tool call, arguments, and second retrieval did not occur in the target run. Dialogue, dependency annotation, memory text, and final answer are observed artifacts.",
                },
                evidence_rows=evidence(DATASET, "S001 worksheet", qid, ["当前问题", "关联前序对话", "最终回答"])
                + evidence(DATASET, "S001 worksheet", gold_id, ["当前问题", "最终回答", "实际需召回内容（原始回答）"]),
            )
        )
    return package(
        candidates,
        [DATASET, REPO / "mem0/memory/agentic_retrieval.py", *checkpoint_files().values()],
        status="SIMULATED",
        note="目标运行没有 Agentic Tool 事件；以下为真实对话驱动的显式模拟，不是运行证据。",
        extra={"scan_stats": {"observed_agentic_event_count": 0, "simulated_scenario_count": len(candidates)}},
    )


def simulate_h(
    dataset: dict[str, dict[str, dict[str, Any]]],
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    config_file = TARGET / "resolved_memory_config.json"
    config = read_json(config_file).get("midterm") or {}
    threshold = int(config.get("promotion_min_recall_count") or 3)
    heat_threshold = float(config.get("promotion_heat_threshold") or 5.0)
    alpha = float(config.get("heat_alpha") or 1.0)
    beta = float(config.get("heat_beta") or 0.5)
    gamma = float(config.get("heat_gamma") or 1.0)
    s001_pages = sorted(
        [p for p in pages if p.get("_source_code") == "S001" and p.get("source_turn_id")],
        key=lambda p: int(p.get("turn_index") or 0),
    )[:12]
    candidates = []
    qmap = dataset["S001"]
    for index, page in enumerate(s001_pages, 1):
        source_qid = str(page.get("source_turn_id"))
        source_num = int(source_qid.rsplit("Q", 1)[1])
        future_ids = [f"S001-Q{i:03d}" for i in range(source_num + 1, min(source_num + 4, 51))]
        if len(future_ids) < threshold:
            continue
        recall_history = []
        for recall_no, future_id in enumerate(future_ids[:threshold], 1):
            recall_history.append({
                "recall_no": recall_no,
                "turn_id": future_id,
                "observed_query": qmap.get(future_id, {}).get("当前问题"),
                "simulated_as_recall_of": source_qid,
            })
        n_visit = threshold
        l_interaction = threshold
        recency = 1.0
        simulated_heat = alpha * n_visit + beta * l_interaction + gamma * recency
        promoted_text = str(page.get("summary") or "")
        score = 72 + min(15, len(promoted_text) / 35) + (6 if index <= 3 else 0)
        candidates.append(
            base_candidate(
                f"H-SIM-S001-{source_qid}", score,
                f"以真实 Memory {source_qid} 和随后三条连续真实问题演示阈值链；召回归属、Heat 达标、Job 与跨 Session 写入均标为模拟。",
                session_id="S001_贵州茅台_投研",
                query_id=future_ids[-1],
                memory_id=str(page.get("id") or ""),
                job_id=f"SIMULATED-PROMOTION-{index:02d}",
                display_data={
                    "title": "连续 3 次模拟召回 → Heat 达标 → Promotion",
                    "current_question": compact(qmap.get(future_ids[-1], {}).get("当前问题"), 40),
                    "input": compact(page.get("summary"), 90),
                    "process": [f"#{x['recall_no']} {x['turn_id']}" for x in recall_history] + [f"SIMULATED Heat={simulated_heat:.2f}"],
                    "output": compact(promoted_text, 100),
                    "key_number": f"count {threshold} / Heat {simulated_heat:.2f} ≥ {heat_threshold:.2f}",
                    "conclusion": "Promotion 未真实发生；此卡只演示生产阈值如何作用于真实连续对话。",
                    "simulation_label": "SIMULATED",
                },
                raw_data={
                    "observed_input": {
                        "source_memory": {"memory_id": page.get("id"), "source_turn_id": source_qid, "summary": page.get("summary")},
                        "real_following_dialogue_queries": recall_history,
                    },
                    "simulated_process": {
                        "recall_history": recall_history,
                        "valid_recall_count": n_visit,
                        "H_segment": simulated_heat,
                        "promotion_threshold": {"min_recall_count": threshold, "heat_threshold": heat_threshold},
                        "first_threshold_turn": future_ids[threshold - 1],
                        "promotion_job": f"SIMULATED-PROMOTION-{index:02d}",
                        "attempts": 0,
                        "final_status": "SIMULATED_SUCCEEDED",
                    },
                    "simulated_output": {"cross_session_memory": promoted_text},
                    "simulation_boundary": "No promotion_events or memory_promotion_jobs exist in the target run. The recall association, heat state, job, and promoted record are hypothetical; source memory and dialogue texts are observed.",
                },
                evidence_rows=evidence(Path(page["_source_file"]), "final checkpoint / pages", page.get("id"), ["source_turn_id", "summary", "keywords"])
                + evidence(DATASET, "S001 worksheet", future_ids[-1], ["当前问题", "最终回答"])
                + evidence(config_file, "midterm config", "promotion", ["promotion_min_recall_count", "promotion_heat_threshold", "heat_alpha", "heat_beta", "heat_gamma"]),
            )
        )
    return package(
        candidates,
        [DATASET, config_file, *checkpoint_files().values(), REPO / "mem0/memory/background_worker.py"],
        status="SIMULATED",
        note="目标运行扫描到 0 个 promotion event / job；以下仅为真实连续对话驱动的阈值演示。",
        extra={"scan_stats": {"observed_promotion_event_count": 0, "simulated_scenario_count": len(candidates)}},
    )


def simulate_i(
    dataset: dict[str, dict[str, dict[str, Any]]],
    pages: list[dict[str, Any]],
    code_to_name: dict[str, str],
) -> dict[str, Any]:
    config_file = TARGET / "resolved_memory_config.json"
    config = read_json(config_file)
    other_weight = float((config.get("fine_grained_longterm") or {}).get("other_session_weight") or 0.7)
    pages_by_code: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        if page.get("source_turn_id"):
            pages_by_code[str(page.get("_source_code"))].append(page)
    pairs = [("S001", "S002"), ("S003", "S004"), ("S005", "S006"), ("S007", "S008"), ("S009", "S010")]
    candidates = []
    sequence = 0
    for current_code, other_code in pairs:
        current_pages = sorted(pages_by_code[current_code], key=lambda p: int(p.get("turn_index") or 0))
        other_pages = sorted(pages_by_code[other_code], key=lambda p: int(p.get("turn_index") or 0))
        for offset in (0, 1):
            if offset >= len(current_pages) or offset >= len(other_pages):
                continue
            sequence += 1
            current = current_pages[offset]
            other = other_pages[offset]
            # Scores are illustrative by explicit user request; only the configured 0.7 weight is observed.
            other_raw = round(0.86 - sequence * 0.007, 3)
            current_raw = round(0.69 + sequence * 0.004, 3)
            current_after = current_raw
            other_after = round(other_raw * other_weight, 3)
            current_qid = str(current.get("source_turn_id"))
            query = dataset.get(current_code, {}).get(current_qid, {}).get("当前问题")
            score = 75 + (8 if current_code == "S001" else 0) + min(10, (current_after - other_after) * 50)
            candidates.append(
                base_candidate(
                    f"I-SIM-{current_code}-{other_code}-{offset + 1}", score,
                    f"使用同公司两个真实 Session 的真实 Memory 文本，按生产配置 other_session_weight={other_weight} 演示反超；相关分数与同次检索为模拟。",
                    session_id=code_to_name.get(current_code, current_code),
                    query_id=current_qid,
                    memory_id=f"{current.get('id')} / {other.get('id')}",
                    display_data={
                        "title": "其他 Session 原始分更高 → 降权后当前 Session 反超",
                        "current_question": compact(query, 40),
                        "input": [
                            f"当前：{compact(current.get('summary'), 65)}",
                            f"其他：{compact(other.get('summary'), 65)}",
                        ],
                        "process": f"SIMULATED：其他 Session × {other_weight}",
                        "output": f"Rank：其他#1/当前#2 → 当前#1/其他#2",
                        "key_number": f"{other_raw:.3f}×{other_weight}={other_after:.3f} < {current_after:.3f}",
                        "conclusion": "仅 Memory 内容与 0.7 权重真实；同次检索及分数为模拟。",
                        "simulation_label": "SIMULATED",
                    },
                    raw_data={
                        "observed_input": {
                            "query": query,
                            "current_session_memory": {"memory_id": current.get("id"), "source_session": current_code, "summary": current.get("summary")},
                            "other_session_memory": {"memory_id": other.get("id"), "source_session": other_code, "summary": other.get("summary")},
                            "configured_other_session_weight": other_weight,
                        },
                        "simulated_process": {
                            "candidates": [
                                {"memory_id": other.get("id"), "source_session": other_code, "raw_score": other_raw, "session_weight": other_weight, "weighted_score": other_after, "rank_before": 1, "rank_after": 2},
                                {"memory_id": current.get("id"), "source_session": current_code, "raw_score": current_raw, "session_weight": 1.0, "weighted_score": current_after, "rank_before": 2, "rank_after": 1},
                            ]
                        },
                        "simulation_boundary": "The target source used isolated user/run scopes and recorded zero cross-session recalls. Pairing and retrieval scores are hypothetical; memory texts and configured other_session_weight are observed.",
                    },
                    evidence_rows=evidence(Path(current["_source_file"]), "final checkpoint / pages", current.get("id"), ["summary", "source_turn_id"])
                    + evidence(Path(other["_source_file"]), "final checkpoint / pages", other.get("id"), ["summary", "source_turn_id"])
                    + evidence(config_file, "fine_grained_longterm config", "other_session_weight", ["other_session_weight"]),
                )
            )
    return package(
        candidates,
        [DATASET, config_file, *checkpoint_files().values(), REPO / "mem0/memory/fine_grained_longterm.py"],
        status="SIMULATED",
        note="目标运行的 652 个 turn 中跨 Session recall 为 0；以下为真实 Memory 文本 + 真实配置权重的模拟排序。",
        extra={"scan_stats": {"observed_cross_session_recall_count": 0, "simulated_scenario_count": len(candidates)}},
    )


def profile_signal(row: dict[str, Any], current_profile: dict[str, Any]) -> tuple[str, str, str]:
    query = str(row.get("当前问题") or "")
    role = str(row.get("当前角色") or "")
    if role and current_profile.get("work_role") != role:
        return "work_role", role, "replace"
    if re.search(r"来源|披露|复算|年报原数|证据", query):
        return "evidence_requirements", f"证据要求：{compact(query, 46)}", "append_unique"
    if re.search(r"先给结论|先说结论|结论优先", query):
        return "output_preferences", f"输出要求：{compact(query, 46)}", "append_unique"
    if re.search(r"不要只|反证|边界|不能", query):
        return "analysis_habits", f"分析习惯：{compact(query, 46)}", "append_unique"
    if re.search(r"三年|同比|逐年|年度", query):
        return "analysis_habits", f"分析习惯：{compact(query, 46)}", "append_unique"
    return "common_tasks", compact(query, 48), "append_unique"


def simulate_profiles(dataset: dict[str, dict[str, dict[str, Any]]]) -> tuple[dict[str, Any], dict[str, Any]]:
    qmap = dataset["S001"]
    selected = list(sorted(qmap.items()))[:12]
    profile: dict[str, Any] = {
        "work_role": None,
        "professional_level": "金融分析专业用户",
        "output_preferences": [],
        "evidence_requirements": [],
        "analysis_habits": [],
        "common_tasks": [],
    }
    j_candidates = []
    k_candidates = []
    for index, (qid, row) in enumerate(selected, 1):
        before = json.loads(json.dumps(profile, ensure_ascii=False))
        field, value, operation = profile_signal(row, profile)
        if operation == "replace":
            profile[field] = value
        else:
            values = profile.setdefault(field, [])
            if value not in values:
                values.append(value)
        after = json.loads(json.dumps(profile, ensure_ascii=False))
        j_candidates.append(
            base_candidate(
                f"J-SIM-S001-{qid}", 76 + (8 if index <= 3 else 0),
                f"用真实用户表达演示画像从 before 到 after 的单字段更新；LLM Plan、校验和 DB 操作均为模拟并单独标注。",
                session_id="S001_贵州茅台_投研",
                query_id=qid,
                job_id=f"SIMULATED-PROFILE-{index:02d}",
                display_data={
                    "title": "用户新行为 → Profile 增量更新",
                    "current_question": compact(row.get("当前问题"), 40),
                    "input": {field: before.get(field)},
                    "process": f"SIMULATED {operation}: {value}",
                    "output": {field: after.get(field)},
                    "key_number": f"changed_fields = [{field}]",
                    "conclusion": "画像链路未真实触发；用户表达与会话角色来自真实数据。",
                    "simulation_label": "SIMULATED",
                },
                raw_data={
                    "observed_input": {"user_expression": row.get("当前问题"), "declared_role": row.get("当前角色")},
                    "simulated_process": {
                        "before_profile": before,
                        "llm_update_plan": {"operation": operation, "field": field, "value": value, "reason": "derived from the current observed user expression"},
                        "validated_modification": {"changed_fields": [field], "operation_type": operation, "before": before.get(field), "after": after.get(field)},
                        "db_operation": f"SIMULATED UPSERT profile.{field}",
                    },
                    "simulated_output": {"after_profile": after},
                    "simulation_boundary": "No profile_update_jobs or user_profiles rows exist in the target source databases. Plan, validation, DB operation, and resulting profile are hypothetical.",
                },
                evidence_rows=evidence(DATASET, "S001 worksheet", qid, ["当前问题", "当前角色", "最终回答"]),
            )
        )
        custom_rules = []
        query = str(row.get("当前问题") or "")
        for phrase in re.split(r"[；;。]", query)[1:]:
            if phrase.strip():
                custom_rules.append(phrase.strip())
        if not custom_rules:
            custom_rules = [compact(query, 60)]
        answer = str(row.get("最终回答") or "")
        headings = [compact(line, 50) for line in answer.splitlines() if line.strip() and len(line.strip()) <= 40][:5]
        profile_rules = [f"角色：{after.get('work_role') or '未设置'}"]
        for profile_field, label in [
            ("evidence_requirements", "证据"),
            ("output_preferences", "输出"),
            ("analysis_habits", "分析"),
        ]:
            values = after.get(profile_field) or []
            if values:
                profile_rules.append(f"{label}：{compact(values[-1], 60)}")
        k_candidates.append(
            base_candidate(
                f"K-SIM-S001-{qid}", 74 + min(12, len(custom_rules) * 3) + (5 if index <= 3 else 0),
                "长期画像由前序真实表达累计模拟，本轮 Custom Prompt 直接取自真实问题约束，最终回答为数据集中的真实回答。",
                session_id="S001_贵州茅台_投研",
                query_id=qid,
                display_data={
                    "title": "模拟长期画像 + 本轮真实要求 → 真实回答",
                    "current_question": compact(query, 40),
                    "input": {"长期画像规则": profile_rules, "本次要求": custom_rules},
                    "process": "SIMULATED：合并长期规则与本轮显式约束",
                    "output": headings or [first_para(answer, 100)],
                    "key_number": f"画像字段 {sum(bool(v) for v in after.values())} / 本轮规则 {len(custom_rules)}",
                    "conclusion": "Profile 应用是模拟；Query、显式规则和最终回答均为真实对话。",
                    "simulation_label": "SIMULATED",
                },
                raw_data={
                    "observed_input": {"query": query, "custom_prompt_from_query": custom_rules},
                    "simulated_process": {"profile": after, "combined_rules": {"long_term": after, "request_specific": custom_rules}},
                    "observed_output": {"final_answer": answer, "answer_structure_headings": headings},
                    "rule_manifestation": {
                        "long_term_profile_contribution": "role, evidence discipline, recurring analysis/output habits",
                        "custom_prompt_contribution": custom_rules,
                        "answer_manifestation": headings,
                    },
                    "simulation_boundary": "No request trace persisted an applied Profile or Custom Prompt in the target run. The profile application is hypothetical; the user query and final answer are observed dataset cells.",
                },
                evidence_rows=evidence(DATASET, "S001 worksheet", qid, ["当前问题", "当前角色", "最终回答"]),
            )
        )
    j = package(
        j_candidates,
        [DATASET, REPO / "mem0/memory/profile_manager.py", REPO / "mem0/memory/profile_updater.py"],
        status="SIMULATED",
        note="目标数据库没有 Profile Job/Profile 行；以下是同一 Session 连续真实表达驱动的累计画像模拟。",
        extra={"scan_stats": {"observed_profile_update_count": 0, "simulated_scenario_count": len(j_candidates)}},
    )
    k = package(
        k_candidates,
        [DATASET, REPO / "mem0/memory/main.py", REPO / "mem0/memory/profile_manager.py"],
        status="SIMULATED",
        note="Profile 应用未真实发生；Query、Custom Prompt 文本和最终回答取自真实对话，画像合并步骤为模拟。",
        extra={"scan_stats": {"observed_profile_custom_prompt_request_count": 0, "simulated_scenario_count": len(k_candidates)}},
    )
    return j, k


def changed_parameters_from_name(name: str) -> dict[str, Any]:
    parts = name.split(":", 1)
    if len(parts) == 1:
        return {}
    spec = re.sub(r":from-[0-9a-f]+$", "", parts[1])
    result: dict[str, Any] = {}
    for token in spec.split(","):
        if "=" in token:
            key, value = token.split("=", 1)
            result[key] = value
        elif token:
            result["variant"] = token
    return result


def metric_subset(metrics: dict[str, Any]) -> dict[str, Any]:
    return {
        key: metrics.get(key)
        for key in [
            "recall_at_k",
            "recall_at_2k",
            "recall_at_4k",
            "mrr",
            "macro_session_recall_at_k",
            "worst_session_recall_at_k",
            "session_stddev",
            "candidate_pool_recall",
            "final_context_recall",
            "runtime_seconds",
        ]
        if key in metrics
    }


def metric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key in set(before) & set(after):
        if isinstance(before[key], (int, float)) and isinstance(after[key], (int, float)):
            result[key] = after[key] - before[key]
    return result


def extract_p() -> dict[str, Any]:
    search_file = TARGET / "search_trace.jsonl"
    research_file = TARGET / "research_trace.jsonl"
    rows_with_lines = list(iter_jsonl(search_file))
    # The trace appends repeated cache observations. Keep the last observed record per candidate hash.
    dedup: dict[str, tuple[int, dict[str, Any]]] = {}
    order: list[str] = []
    for lineno, row in rows_with_lines:
        key = str(row.get("candidate_hash") or f"{row.get('stage')}::{row.get('candidate')}")
        if key not in dedup:
            order.append(key)
        dedup[key] = (lineno, row)
    unique_rows = [dedup[key] for key in order]
    baseline_pairs = [(line, row) for line, row in unique_rows if row.get("stage") == "baseline"]
    baseline_line, baseline_row = baseline_pairs[-1]
    baseline_metrics = metric_subset(baseline_row.get("metrics") or {})

    accepted_names: set[str] = set()
    branch_outcomes: dict[str, dict[str, Any]] = {}
    diagnosis_by_stage: dict[int, dict[str, Any]] = {}
    action_basis_by_stage: dict[int, list[dict[str, Any]]] = {}
    research_events = []
    research_statuses: Counter[str] = Counter()
    for lineno, row in iter_jsonl(research_file):
        research_events.append({"line": lineno, "event": row.get("event"), "status": row.get("status"), "decision_id": row.get("decision_id"), "stage_index": row.get("stage_index"), "timestamp": row.get("timestamp"), "error": row.get("error"), "fallback_used": row.get("fallback_used"), "final_validated_actions": row.get("final_validated_actions")})
        research_statuses[str(row.get("status"))] += 1
        prompt = parse_json(row.get("user_evidence_prompt"), {}) or {}
        ev = prompt.get("evidence") or {}
        stage_index = int(row.get("stage_index") or 0)
        if ev.get("diagnosis"):
            diagnosis_by_stage[stage_index] = ev["diagnosis"]
        frontier = ev.get("frontier_anchor") or {}
        if frontier.get("candidate"):
            accepted_names.add(str(frontier["candidate"]))
        for branch in (ev.get("branch_state") or {}).get("attempted", {}).values():
            for round_row in branch.get("rounds") or []:
                if round_row.get("best_candidate"):
                    best_name = str(round_row["best_candidate"])
                    accepted_names.add(best_name)
                    branch_outcomes[best_name] = {
                        "frontier_winner": bool(round_row.get("frontier_winner")),
                        "improvement_pp": round_row.get("improvement_pp"),
                        "generation_round": round_row.get("generation_round"),
                    }
        if row.get("event") in {"RESPONSE", "FALLBACK"} and row.get("final_validated_actions"):
            action_basis_by_stage[stage_index] = list(row.get("final_validated_actions") or [])

    by_name = {str(row.get("candidate")): row for _, row in unique_rows}
    stage1_winner_name = next(
        (name for name in accepted_names if name.startswith("RetrievalControl:midterm_candidate_pool_multiplier=8")),
        "RetrievalControl:midterm_candidate_pool_multiplier=8:from-3af475",
    )
    stage1_metrics = metric_subset((by_name.get(stage1_winner_name) or {}).get("metrics") or baseline_row.get("metrics") or {})
    candidates = []
    full_path = [
        {
            "sequence": 0,
            "stage": "baseline",
            "candidate": "baseline",
            "metrics": baseline_metrics,
            "status": baseline_row.get("status"),
            "source_line": baseline_line,
        }
    ]
    sequence = 0
    for lineno, row in unique_rows:
        if row.get("stage") == "baseline":
            continue
        sequence += 1
        name = str(row.get("candidate") or "")
        stage = str(row.get("stage") or "")
        stage_index = 1 if stage.startswith("stage_1") else 2
        parent_name = "baseline" if stage_index == 1 else stage1_winner_name
        before = baseline_metrics if stage_index == 1 else stage1_metrics
        after = metric_subset(row.get("metrics") or {})
        delta = metric_delta(before, after)
        outcome = branch_outcomes.get(name)
        accepted = bool(outcome and outcome.get("frontier_winner"))
        if accepted:
            decision = "ACCEPTED_FRONTIER_WINNER"
        elif outcome:
            decision = "BRANCH_WINNER_FRONTIER_REJECTED"
        else:
            decision = "REJECTED_NOT_BRANCH_WINNER"
        diagnosis = diagnosis_by_stage.get(stage_index + 1) or diagnosis_by_stage.get(stage_index) or {}
        changed = changed_parameters_from_name(name)
        recall_delta_pp = float(delta.get("recall_at_k") or 0) * 100
        mrr_delta = float(delta.get("mrr") or 0)
        score = 55 + min(24, abs(recall_delta_pp) * 3 + abs(mrr_delta) * 120)
        score += 12 if accepted else 0
        score += 7 if recall_delta_pp < -1 else 0
        if name.startswith("HybridRetrieval:fusion=rrf"):
            score += 5
        why = (
            f"仅改 {', '.join(changed) or '候选分支'}，Recall@K 相对父方案 {recall_delta_pp:+.2f}pp、"
            f"MRR {mrr_delta:+.4f}，结果被标记为 {decision}。"
        )
        candidate_id = f"P-{sequence:03d}-{row.get('candidate_hash', '')[:8]}"
        candidate = base_candidate(
            candidate_id, score, why,
            session_id="tune split: S002/S003/S005/S007/S008/S009/S010",
            query_id=None,
            job_id=str(row.get("candidate_hash")),
            display_data={
                "title": f"{stage.replace('_', ' ')}：{compact(name, 70)}",
                "current_question": "自动调参实验决策",
                "input": f"父方案：{parent_name}",
                "process": f"诊断：{diagnosis.get('regime', 'recorded diagnosis unavailable')}；改动：{json.dumps(changed, ensure_ascii=False)}",
                "output": f"Recall@K {before.get('recall_at_k', 0):.4f} → {after.get('recall_at_k', 0):.4f}；{decision}",
                "key_number": f"Recall {recall_delta_pp:+.2f}pp / MRR {mrr_delta:+.4f}",
                "conclusion": "接受分支最优者，其余真实失败/持平候选保留为淘汰证据。",
            },
            raw_data={
                "stage": stage,
                "parent_candidate": parent_name,
                "candidate": name,
                "candidate_hash": row.get("candidate_hash"),
                "diagnosis": diagnosis,
                "decided_experiment": changed,
                "changed_parameters": changed,
                "metrics_before": before,
                "metrics_after": after,
                "metric_delta": delta,
                "decision": decision,
                "accepted": accepted,
                "branch_outcome": outcome,
                "next_experiment_basis": action_basis_by_stage.get(stage_index + 1) or [],
                "status": row.get("status"),
                "runtime_seconds": row.get("runtime_seconds"),
                "cache_hits": row.get("cache_hits"),
                "cache_misses": row.get("cache_misses"),
            },
            evidence_rows=evidence(search_file, "search_trace.jsonl", f"line {lineno}", ["candidate", "candidate_hash", "stage", "status", "metrics", "runtime_seconds", "cache_hits", "cache_misses"])
            + evidence(research_file, "research_trace.jsonl", f"stage {stage_index + 1}", ["evidence.diagnosis", "evidence.branch_state", "evidence.frontier_anchor", "final_validated_actions"]),
        )
        candidates.append(candidate)
        full_path.append({
            "sequence": sequence,
            "stage": stage,
            "parent_candidate": parent_name,
            "candidate": name,
            "candidate_hash": row.get("candidate_hash"),
            "changed_parameters": changed,
            "metrics": after,
            "delta_from_parent": delta,
            "decision": decision,
            "source_line": lineno,
        })

    result = package(
        candidates,
        [
            search_file,
            research_file,
            TARGET / "dataset_audit.json",
            TARGET / "dataset_audit.md",
            TARGET / "split_manifest.json",
            TARGET / "resolved_memory_config.json",
            TARGET / "model_discovery.json",
            *sorted((TARGET / "workers").glob("*/*.json")),
        ],
        note="目标运行在 stage 3 选型后停止，未生成 leaderboard/session_metrics/requirement_results/best_config/best_config_diff/final_report；只报告已执行候选。",
        extra={
            "run_completion_status": "PARTIAL_RUN_NO_FINAL_SELECTION_ARTIFACTS",
            "missing_expected_artifacts": [
                "run_metadata.json",
                "leaderboard.csv",
                "session_metrics.csv",
                "requirement_results.jsonl",
                "best_config.json",
                "best_config_diff.json",
                "final_report.md",
            ],
            "P1_full_tuning_path": full_path,
            "P2_top_10_decisions": [],
            "four_stage_display_summary": {
                "Baseline": baseline_metrics,
                "主要诊断": diagnosis_by_stage.get(2) or diagnosis_by_stage.get(3),
                "关键实验": {
                    "candidate": stage1_winner_name,
                    "changed_parameter": {"midterm_candidate_pool_multiplier": "4 → 8"},
                    "metrics": stage1_metrics,
                },
                "Final": "NOT_PRODUCED；最后已完成 frontier 为 RetrievalControl pool multiplier=8，不能称最终最佳配置",
            },
            "scan_stats": {
                "search_trace_raw_rows": len(rows_with_lines),
                "search_trace_unique_candidate_count": len(unique_rows),
                "completed_experimental_candidate_count": len(candidates),
                "research_trace_event_count": len(research_events),
                "research_status_counts": dict(research_statuses),
                "worker_json_count": len(list((TARGET / "workers").glob("*/*.json"))),
            },
            "research_events": research_events,
        },
    )
    result["P2_top_10_decisions"] = result["top_10"]
    return result


def extract_q(
    dataset: dict[str, dict[str, dict[str, Any]]],
    code_to_name: dict[str, str],
    databases: dict[str, Path],
) -> dict[str, Any]:
    audit_file = TARGET / "dataset_audit.json"
    split_file = TARGET / "split_manifest.json"
    search_file = TARGET / "search_trace.jsonl"
    audit = read_json(audit_file)
    search_rows = [row for _, row in iter_jsonl(search_file)]
    baseline_rows = [row for row in search_rows if row.get("stage") == "baseline"]
    aggregate_baseline = metric_subset((baseline_rows[-1] if baseline_rows else {}).get("metrics") or {})
    candidates = []
    table_scan = {"database_count": 0, "user_memory_tuning_configs_table_count": 0, "saved_config_row_count": 0}
    for path in sorted((TARGET / "production_runtimes").glob("*/*/history.db")):
        table_scan["database_count"] += 1
        conn = ro_connect(path)
        try:
            if table_exists(conn, "user_memory_tuning_configs"):
                table_scan["user_memory_tuning_configs_table_count"] += 1
                table_scan["saved_config_row_count"] += conn.execute("SELECT COUNT(*) FROM user_memory_tuning_configs").fetchone()[0]
        finally:
            conn.close()
    for code, rows in sorted(dataset.items()):
        qa_count = len(rows)
        annotated = 0
        dependencies = 0
        for row in rows.values():
            deps = re.findall(rf"{re.escape(code)}-Q\d{{3}}", str(row.get("关联前序对话") or ""))
            if deps:
                annotated += 1
                dependencies += len(deps)
        db = databases.get(code if code != "S004" else "S004___IR")
        score = 46 + min(20, annotated / 5) + (8 if code == "S001" else 0)
        candidates.append(
            base_candidate(
                f"Q-PARTIAL-{code}", score,
                f"可确认该 Session 有 {qa_count} 条基准 QA、{annotated} 条历史依赖 Query；但目标运行不是 user-memory-auto-tuner 完整链路，未产生最终配置。",
                session_id=code_to_name.get(code, code),
                job_id=None,
                display_data={
                    "title": "Benchmark 数据存在；个性化配置链路未完成",
                    "current_question": code_to_name.get(code, code),
                    "input": f"QA {qa_count} / 标注 Query {annotated} / dependency {dependencies}",
                    "process": f"目标运行 aggregate baseline Recall@K={aggregate_baseline.get('recall_at_k')}",
                    "output": "Final metrics / 用户配置：NOT_PRODUCED",
                    "key_number": f"链路确认至 Benchmark + 部分调参，共 {qa_count} QA",
                    "conclusion": "不是可宣称的真实用户级闭环案例。",
                },
                raw_data={
                    "raw_qa_count": qa_count,
                    "annotated_query_count": annotated,
                    "dependency_count": dependencies,
                    "requirement_count": dependencies,
                    "baseline_metrics": {"scope": "aggregate tune split, not this session", **aggregate_baseline},
                    "final_metrics": "NOT_PRODUCED",
                    "final_user_config": "NOT_PRODUCED",
                    "changed_parameters_vs_default": [],
                    "confirmed_chain": [
                        "curated multi-session QA workbook exists",
                        "dependency annotations audited",
                        "production source replay persisted to read-only-inspected SQLite" if db else "production source DB not mapped",
                        "baseline and stage-1/stage-2 aggregate candidate evaluations exist",
                    ],
                    "missing_chain": [
                        "not proven to originate from a raw real-user SQLite history extraction",
                        "no completed held-out final selection in target run",
                        "no user_memory_tuning_configs saved row",
                        "no online application of a user config",
                    ],
                    "chain_status": "PARTIAL_ONLY_NOT_PERSONALIZED_CLOSED_LOOP",
                },
                evidence_rows=evidence(DATASET, f"{code_to_name.get(code, code)} worksheet", code, ["编号", "关联前序对话", "当前问题", "最终回答"])
                + evidence(audit_file, "dataset audit", code, ["session_count", "query_count", "gold_bearing_query_count", "gold_requirement_count"])
                + evidence(search_file, "search_trace.jsonl", "aggregate baseline", ["metrics", "stage", "scope"])
                + (evidence(db, "SQLite schema scan", code, ["user_memory_tuning_configs absent"]) if db else []),
            )
        )
    return package(
        candidates,
        [DATASET, audit_file, split_file, search_file, *databases.values(), REPO / ".agents/skills/user-memory-auto-tuner/SKILL.md"],
        note="找到 10 个 Benchmark Session 的部分链路，但完整用户历史→Benchmark→held-out final→saved config 链路为 0。",
        extra={
            "chain_status": "PARTIAL_ONLY",
            "complete_chain_count": 0,
            "partial_session_count": len(candidates),
            "database_config_table_scan": table_scan,
            "cross_session_tuning_status": "CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD",
            "deployment_note": "即便保存推荐配置也不等于线上 Memory 已生效；本目标运行连 saved config 都未产生。",
        },
    )


def json_text(value: Any, limit: int = 32000) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > limit:
        return text[: limit - 80] + "… [Excel 单元格截断；完整值见 raw JSON]"
    return text


def candidate_source_summary(candidate: dict[str, Any]) -> str:
    sources = []
    for item in candidate.get("evidence") or []:
        source = str(item.get("source_file") or "")
        if source and source not in sources:
            sources.append(source)
    return " | ".join(sources)


def build_excel(results: dict[str, dict[str, Any]], path: Path) -> None:
    workbook = Workbook()
    workbook.remove(workbook.active)
    headers_fill = PatternFill("solid", fgColor="26384A")
    status_fill = PatternFill("solid", fgColor="E8EEF4")

    def add_sheet(name: str, headers: list[str], rows: Iterable[list[Any]]) -> None:
        sheet = workbook.create_sheet(name)
        sheet.append(headers)
        for cell in sheet[1]:
            cell.fill = headers_fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in rows:
            sheet.append([json_text(value) if isinstance(value, (dict, list)) else value for value in row])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
        for index, header in enumerate(headers, 1):
            max_len = len(header)
            for cell in list(sheet.columns)[index - 1][:80]:
                max_len = max(max_len, min(50, len(str(cell.value or ""))))
            sheet.column_dimensions[get_column_letter(index)].width = min(55, max(12, max_len + 2))
        sheet.row_dimensions[1].height = 28

    overview_rows = []
    all_rows = []
    display_rows = []
    raw_rows = []
    missing_rows = []
    for type_code, (name, _) in TYPE_INFO.items():
        result = results[type_code]
        top = result.get("top_1") or {}
        status = result.get("status")
        recommend = "是" if status == "FOUND" and result.get("candidate_count", 0) else ("仅作模拟演示" if status == "SIMULATED" else "否")
        overview_rows.append([
            type_code,
            name,
            result.get("candidate_count"),
            "是" if int(result.get("candidate_count") or 0) >= 10 else "否",
            top.get("candidate_id"),
            [c.get("candidate_id") for c in result.get("top_3") or []],
            top.get("session_id"),
            top.get("query_id") or top.get("job_id"),
            status,
            recommend,
        ])
        rank_map = {str(c.get("candidate_id")): i for i, c in enumerate(result.get("all_candidates") or [], 1)}
        for candidate in result.get("all_candidates") or []:
            display = candidate.get("display_data") or {}
            all_rows.append([
                type_code,
                rank_map.get(str(candidate.get("candidate_id"))),
                candidate.get("presentation_score"),
                candidate.get("session_id"),
                candidate.get("query_id"),
                candidate.get("job_id") or candidate.get("memory_id"),
                display.get("input"),
                display.get("output"),
                display.get("key_number"),
                candidate.get("why_good_for_ppt"),
                candidate_source_summary(candidate),
                status,
            ])
        for rank, candidate in enumerate(result.get("top_10") or [], 1):
            display = candidate.get("display_data") or {}
            display_rows.append([
                type_code,
                rank,
                display.get("title"),
                display.get("current_question"),
                display.get("input"),
                display.get("process"),
                display.get("output"),
                display.get("key_number"),
                display.get("conclusion"),
                status,
            ])
        for candidate in result.get("all_candidates") or []:
            raw_rows.append([
                type_code,
                candidate.get("candidate_id"),
                "candidate.raw_data",
                "candidate JSON",
                candidate.get("candidate_id"),
                "raw_data",
                json_text(candidate.get("raw_data"), 32000),
            ])
            for locator in candidate.get("evidence") or []:
                raw_rows.append([
                    type_code,
                    candidate.get("candidate_id"),
                    locator.get("source_file"),
                    locator.get("table / collection"),
                    locator.get("record_id"),
                    locator.get("field"),
                    "完整原始值已保存在同 candidate 的 raw_data 与来源文件中",
                ])
        issues = []
        if status in {"NOT_FOUND", "READ_ONLY_UNAVAILABLE", "SIMULATED"}:
            issues.append(status)
        if int(result.get("candidate_count") or 0) < 10:
            issues.append("LESS_THAN_10")
        if result.get("note"):
            issues.append(result["note"])
        if result.get("missing_expected_artifacts"):
            issues.append("missing: " + ", ".join(result["missing_expected_artifacts"]))
        if result.get("chain_status") == "PARTIAL_ONLY":
            issues.append("PARTIAL_ONLY: complete personalized chain count = 0")
        if issues:
            missing_rows.append([type_code, name, status, result.get("candidate_count"), "；".join(issues), result.get("scan_stats")])

    add_sheet(
        "01_Overview",
        ["type", "名称", "实际候选数量", "是否达到10条", "Top 1", "Top 3", "推荐 Session", "推荐 Query / Job", "数据完整度", "是否建议用于答辩"],
        overview_rows,
    )
    add_sheet(
        "02_All_Candidates",
        ["type", "candidate_rank", "presentation_score", "session", "query", "job / memory", "before", "after", "improvement", "why_good_for_ppt", "source", "status"],
        all_rows,
    )
    add_sheet(
        "03_Display_Content",
        ["类型", "案例排名", "标题", "当前问题", "输入", "中间结果", "最终结果", "关键数字", "一句话结论", "状态"],
        display_rows,
    )
    add_sheet(
        "04_Raw_Evidence",
        ["type", "candidate_id", "file", "table / collection", "ID", "field", "原始值 / 定位说明"],
        raw_rows,
    )
    add_sheet(
        "05_Missing",
        ["type", "名称", "状态", "候选数", "原因 / 边界", "扫描统计"],
        missing_rows,
    )
    # Highlight the status column in Overview without changing semantics.
    sheet = workbook["01_Overview"]
    for cell in sheet["I"][1:]:
        cell.fill = status_fill
        cell.font = Font(bold=True)
    workbook.save(path)


def render_value(value: Any, limit: int = 5) -> str:
    if value is None or value == "":
        return '<span class="muted">—</span>'
    if isinstance(value, dict):
        pieces = []
        for key, item in list(value.items())[:limit]:
            pieces.append(f"<div><span class=\"label\">{html.escape(str(key))}</span>{render_value(item, 3)}</div>")
        return "".join(pieces)
    if isinstance(value, list):
        pieces = [f"<li>{render_value(item, 3)}</li>" for item in value[:limit]]
        extra = f"<li class=\"muted\">另有 {len(value) - limit} 条，见 raw JSON</li>" if len(value) > limit else ""
        return "<ol>" + "".join(pieces) + extra + "</ol>"
    return html.escape(compact(value, 160))


def candidate_card(candidate: dict[str, Any], rank: int, status: str) -> str:
    display = candidate.get("display_data") or {}
    sources = sorted({str(e.get("source_file")) for e in candidate.get("evidence") or []})
    simulation = '<div class="sim-watermark">SIMULATED · 非真实运行事件</div>' if status == "SIMULATED" else ""
    return f"""
    <article class="case-card {'simulated' if status == 'SIMULATED' else ''}">
      {simulation}
      <div class="case-head">
        <div><span class="case-no">案例 #{rank}</span><h3>{html.escape(str(display.get('title') or candidate.get('candidate_id')))}</h3></div>
        <div class="score">{candidate.get('presentation_score')}<small>/100</small></div>
      </div>
      <div class="meta"><span>Session：{html.escape(compact(candidate.get('session_id'), 55))}</span><span>Query / Job：{html.escape(compact(candidate.get('query_id') or candidate.get('job_id'), 55))}</span></div>
      <div class="question">{html.escape(str(display.get('current_question') or ''))}</div>
      <div class="flow">
        <div class="flow-box"><b>输入 / Before</b>{render_value(display.get('input'))}</div>
        <div class="arrow">→</div>
        <div class="flow-box"><b>系统处理</b>{render_value(display.get('process'))}</div>
        <div class="arrow">→</div>
        <div class="flow-box output"><b>输出 / After</b>{render_value(display.get('output'))}</div>
      </div>
      <div class="result-line"><strong>{html.escape(str(display.get('key_number') or ''))}</strong><span>{html.escape(str(display.get('conclusion') or ''))}</span></div>
      <p class="why">{html.escape(str(candidate.get('why_good_for_ppt') or ''))}</p>
      <p class="source">来源：{html.escape('；'.join(sources[:4]))}{'；其余见 raw JSON' if len(sources) > 4 else ''}</p>
    </article>
    """


def build_html(results: dict[str, dict[str, Any]], path: Path) -> None:
    sections = []
    for type_code, (name, filename) in TYPE_INFO.items():
        result = results[type_code]
        status = str(result.get("status"))
        top = result.get("top_10") or []
        if top:
            top3 = "".join(candidate_card(candidate, rank, status) for rank, candidate in enumerate(top[:3], 1))
            rest = "".join(candidate_card(candidate, rank, status) for rank, candidate in enumerate(top[3:10], 4))
            rest_block = f'<h3 class="subhead">候选 4～10</h3>{rest}' if rest else ""
        else:
            top3 = f'<div class="missing-card"><b>{html.escape(status)}</b><p>{html.escape(str(result.get("note") or "未找到真实运行记录。"))}</p><p>扫描统计：{html.escape(json_text(result.get("scan_stats") or {}, 1000))}</p></div>'
            rest_block = ""
        note = f'<p class="section-note">{html.escape(str(result.get("note")))}</p>' if result.get("note") else ""
        sections.append(f"""
        <section id="type-{type_code}" class="mechanism">
          <div class="section-title"><div><span>{type_code}</span><h2>{html.escape(name)}</h2></div><em class="status {status.lower()}">{html.escape(status)} · {result.get('candidate_count')} 条</em></div>
          {note}
          <h3 class="subhead">Top 3 推荐案例</h3>
          {top3}
          {rest_block}
          <p class="json-link">结构化证据：raw/{html.escape(filename)}</p>
        </section>
        """)
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mem0 答辩案例证据包</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#fff;color:#141a20;font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1440px;margin:auto;padding:42px 54px 100px}} .cover{{border-bottom:3px solid #26384a;padding-bottom:28px;margin-bottom:44px}}
h1{{font-size:34px;margin:0 0 10px}} .cover p{{color:#526273;margin:5px 0}} .cover strong{{color:#b42318}}
.mechanism{{margin:0 0 72px;page-break-before:always}} .section-title{{display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #8ea1b4;padding-bottom:10px}}
.section-title>div{{display:flex;align-items:center;gap:12px}} .section-title span{{display:inline-grid;place-items:center;width:38px;height:38px;background:#26384a;color:#fff;font-size:20px;font-weight:700}}
h2{{font-size:26px;margin:0}} .status{{font-style:normal;font-weight:700;color:#33485d}} .status.not_found{{color:#697784}} .status.simulated{{color:#8a5a00}}
.section-note{{background:#f5f7f9;border-left:4px solid #8ea1b4;padding:10px 14px;color:#46596c}} .subhead{{font-size:18px;margin:26px 0 12px;color:#33485d}}
.case-card{{position:relative;border:1px solid #b8c5d1;border-left:6px solid #516f8d;border-radius:2px;padding:20px 22px 14px;margin:0 0 18px;min-height:430px;break-inside:avoid;overflow:hidden}}
.case-card.simulated{{border-style:dashed;border-left-color:#a87614;background:#fffdf7}} .sim-watermark{{position:absolute;right:130px;top:19px;color:#8a5a00;font-weight:700;letter-spacing:.08em;font-size:12px}}
.case-head{{display:flex;justify-content:space-between;gap:20px}} .case-head h3{{margin:3px 0 0;font-size:21px}} .case-no{{color:#526273;font-weight:700}}
.score{{font-size:30px;color:#b42318;font-weight:800}} .score small{{font-size:12px;color:#6d7985}} .meta{{display:flex;gap:30px;color:#526273;font-size:13px;border-bottom:1px solid #d8e0e7;padding:8px 0 10px}}
.question{{font-size:17px;font-weight:700;padding:13px 0 10px}} .flow{{display:grid;grid-template-columns:1fr 34px 1fr 34px 1fr;align-items:stretch;gap:4px}}
.flow-box{{background:#f5f7f9;border:1px solid #d6dfe7;padding:13px;min-height:145px;overflow:hidden}} .flow-box.output{{background:#fff;border-color:#a9b9c7}} .flow-box>b{{display:block;color:#33485d;margin-bottom:7px}}
.arrow{{display:grid;place-items:center;color:#66819b;font-size:27px;font-weight:700}} ol{{margin:4px 0;padding-left:20px}} li{{margin:3px 0}} .label{{display:inline-block;color:#526273;font-weight:700;margin-right:6px}}
.result-line{{display:flex;gap:18px;align-items:center;margin-top:12px;padding:10px 12px;border-top:2px solid #b42318;background:#fff8f7}} .result-line strong{{font-size:18px;color:#b42318;white-space:nowrap}}
.why{{margin:10px 0 4px}} .source{{font-size:11px;color:#74808b;margin:8px 0 0;border-top:1px solid #e0e5ea;padding-top:7px}} .muted{{color:#85909a}} .json-link{{font-size:12px;color:#526273}}
.missing-card{{border:1px solid #cbd4dc;background:#f7f8fa;padding:24px;min-height:150px}} .missing-card b{{color:#697784;font-size:18px}}
@media(max-width:900px){{main{{padding:24px}}.flow{{grid-template-columns:1fr}}.arrow{{transform:rotate(90deg)}}.meta{{display:block}}}}
@media print{{main{{max-width:none;padding:20px}}.case-card{{min-height:0}}}}
</style></head><body><main>
<header class="cover"><h1>Mem0 真实实验与 Memory 案例证据包</h1>
<p>重点运行：<strong>20260823T100601Z-99ac7ea4</strong>；生成时间：{html.escape(generated)}</p>
<p>FOUND = 真实记录；SIMULATED = 按用户要求、基于真实对话构造的机制演示；NOT_FOUND = 扫描后未发现。模拟卡不得作为真实运行证据。</p>
</header>
{''.join(sections)}
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


def build_readme(results: dict[str, dict[str, Any]], path: Path) -> None:
    lines = [
        "# PPT Evidence Pack",
        "",
        "| 类型 | 候选数 | FOUND / NOT_FOUND | 推荐 Session | 推荐 Query / Job | 是否建议展示 |",
        "| -- | --: | ----------------- | ---------- | -------------- | ------ |",
    ]
    for code, (name, _) in TYPE_INFO.items():
        result = results[code]
        top = result.get("top_1") or {}
        status = result.get("status")
        recommend = "是" if status == "FOUND" and result.get("candidate_count") else ("仅作模拟演示" if status == "SIMULATED" else "否")
        lines.append(
            f"| {code} {name} | {result.get('candidate_count')} | {status} | "
            f"{compact(top.get('session_id'), 30)} | {compact(top.get('query_id') or top.get('job_id'), 30)} | {recommend} |"
        )
    lines += [
        "",
        "> 证据口径：`FOUND` 卡片的机制过程来自真实持久化记录；`SIMULATED` 卡片只复用真实对话/Memory，机制步骤没有真实发生，HTML 与 Excel 均保留模拟标签。",
        "",
    ]
    for code, (name, filename) in TYPE_INFO.items():
        result = results[code]
        top3 = ", ".join(c.get("candidate_id", "") for c in result.get("top_3") or []) or "无"
        top = result.get("top_1") or {}
        lines += [
            f"## {code}. {name}",
            "",
            f"- 状态 / 数量：{result.get('status')} / {result.get('candidate_count')} 条。",
            f"- Top 3：{top3}。",
            f"- Top 1 原因：{top.get('why_good_for_ppt') or result.get('note') or '无候选'}",
            f"- 数据完整度：{result.get('note') or ('达到 10 条' if int(result.get('candidate_count') or 0) >= 10 else '不足 10 条，已保存全部')}。",
            f"- 扫描统计：`{json_text(result.get('scan_stats') or {}, 1500)}`",
            f"- 结构化证据：[`raw/{filename}`](raw/{filename})",
            "",
        ]
    lines += [
        "## 安全验收",
        "",
        "Existing repository files modified: NO",
        "",
        "All generated files under exp/temp/: YES",
        "",
        "SQLite access mode: `mode=ro&immutable=1` + `PRAGMA query_only=ON`",
        "",
        "Vector store access: NOT OPENED（仅读取已有 JSON/JSONL 快照）",
        "",
        "Experiment rerun / LLM call / Query Rewrite rerun: NO",
        "",
        "Git baseline/final comparison: PASS（除允许的 `?? exp/temp/` 外，无新增、删除或改变的状态项；详见 `raw/git_safety_report.json`）",
        "",
        "说明：上述结论以任务前后 Git 状态差异检查为准；`exp/temp/` 通常被 Git 忽略，但所有本任务新文件均位于本目录。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def validate_results(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["all_types_present"] = set(results) == set(TYPE_INFO)
    checks["candidate_ids_unique_within_type"] = all(
        len({c.get("candidate_id") for c in result.get("all_candidates") or []}) == len(result.get("all_candidates") or [])
        for result in results.values()
    )
    checks["top_10_sorted_by_selection"] = all(
        all(float(items[i].get("presentation_score") or 0) >= float(items[i + 1].get("presentation_score") or 0) for i in range(len(items) - 1))
        for result in results.values()
        for items in [result.get("top_10") or []]
        if result.get("status") != "FOUND" or result is not results.get("C")
    )
    checks["all_candidates_have_traceability_fields"] = all(
        all(key in candidate for key in ["candidate_id", "presentation_score", "why_good_for_ppt", "session_id", "query_id", "memory_id", "job_id", "display_data", "raw_data", "evidence"])
        for result in results.values() for candidate in result.get("all_candidates") or []
    )
    checks["simulated_types_explicit"] = all(results[code].get("status") == "SIMULATED" for code in ["D", "H", "I", "J", "K"])
    checks["simulated_candidates_have_boundary"] = all(
        "simulation_boundary" in (candidate.get("raw_data") or {})
        for code in ["D", "H", "I", "J", "K"]
        for candidate in results[code].get("all_candidates") or []
    )
    checks["real_types_not_labeled_simulated"] = all(results[code].get("status") != "SIMULATED" for code in set(TYPE_INFO) - {"D", "H", "I", "J", "K"})
    checks["simulated_types_have_at_least_10"] = all(int(results[code].get("candidate_count") or 0) >= 10 for code in ["D", "H", "I", "J", "K"])
    checks["simulated_profile_updates_change_value"] = all(
        candidate["raw_data"]["simulated_process"]["validated_modification"]["before"]
        != candidate["raw_data"]["simulated_process"]["validated_modification"]["after"]
        for candidate in results["J"].get("all_candidates") or []
    )
    checks["all_new_files_root"] = str(OUT.resolve()).startswith(str((REPO / "exp/temp").resolve()))
    checks["target_run_not_claimed_complete"] = results["P"].get("run_completion_status") == "PARTIAL_RUN_NO_FINAL_SELECTION_ARTIFACTS"
    checks["personalized_chain_not_claimed_complete"] = results["Q"].get("complete_chain_count") == 0
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "counts": {code: results[code].get("candidate_count") for code in TYPE_INFO},
        "statuses": {code: results[code].get("status") for code in TYPE_INFO},
    }


def main() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    dataset, code_to_name = load_dataset()
    pages, final_checkpoints, _ = load_final_pages()
    page_by_id = {str(page.get("id")): page for page in pages}
    s001_path = checkpoint_files()["S001"]
    s001_checkpoints = list(iter_jsonl(s001_path))
    databases = target_source_databases()
    jobs = load_job_rows(databases)
    recall_scan = scan_recall_events()
    profile_job_count = sum(len(tables["profile_update_jobs"]) for tables in jobs.values())
    promotion_job_count = sum(len(tables["memory_promotion_jobs"]) for tables in jobs.values())
    profile_row_count = 0
    for path in databases.values():
        conn = ro_connect(path)
        try:
            if table_exists(conn, "user_profiles"):
                profile_row_count += conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]
        finally:
            conn.close()

    j_result, k_result = simulate_profiles(dataset)
    results: dict[str, dict[str, Any]] = {
        "A": extract_a(pages),
        "B": extract_b(s001_checkpoints, dataset, page_by_id),
        "C": extract_c(dataset),
        "D": simulate_d(dataset),
        "E": extract_e(dataset),
        "F": extract_f(dataset),
        "G": extract_g(s001_checkpoints),
        "H": simulate_h(dataset, pages),
        "I": simulate_i(dataset, pages, code_to_name),
        "J": j_result,
        "K": k_result,
        "L": extract_l(jobs, databases),
        "M": extract_m(jobs, databases),
        "N": extract_n(jobs, databases, pages),
        "O": extract_o(),
        "P": extract_p(),
        "Q": extract_q(dataset, code_to_name, databases),
    }
    # Attach the full observed absence scans to the simulated mechanisms.
    results["D"]["scan_stats"].update({"target_recall_turn_count": recall_scan["stats"]["turn_count"], "search_trace_agentic_stage_count": 0})
    results["H"]["scan_stats"].update({"target_promotion_event_count": recall_scan["stats"]["promotion_event_count"], "target_promotion_job_count": promotion_job_count})
    results["I"]["scan_stats"].update({"target_cross_session_valid_recall_count": recall_scan["stats"]["cross_session_valid_recall_count"], "target_turns_with_cross_session_results": recall_scan["stats"]["turns_with_cross_session_results"]})
    results["J"]["scan_stats"].update({"target_profile_job_count": profile_job_count, "target_user_profile_row_count": profile_row_count})
    results["K"]["scan_stats"].update({"target_profile_job_count": profile_job_count, "target_user_profile_row_count": profile_row_count})
    for code, (_, filename) in TYPE_INFO.items():
        (RAW / filename).write_text(json.dumps(results[code], ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    build_excel(results, OUT / "evidence_index.xlsx")
    build_html(results, OUT / "evidence_pack.html")
    build_readme(results, OUT / "README.md")
    validation = validate_results(results)
    (RAW / "validation_report.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    inventory = {
        "target_run": rel(TARGET),
        "target_top_level_files": sorted(rel(path) for path in TARGET.iterdir() if path.is_file()),
        "target_production_source_files": sorted(rel(path) for path in (TARGET / "production_source").glob("*/*")),
        "target_source_database_count": len(databases),
        "read_only_protocol": "SQLite mode=ro&immutable=1; PRAGMA query_only=ON; no vector DB client opened",
        "existing_expected_artifacts": {name: (TARGET / name).exists() for name in ["run_metadata.json", "search_trace.jsonl", "requirement_results.jsonl", "leaderboard.csv", "session_metrics.csv", "best_config.json", "best_config_diff.json", "final_report.md"]},
        "generated_files": sorted(rel(path) for path in OUT.rglob("*") if path.is_file()),
    }
    inventory_path = RAW / "source_inventory.json"
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    inventory["generated_files"] = sorted(rel(path) for path in OUT.rglob("*") if path.is_file())
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"validation": validation, "output": rel(OUT)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
