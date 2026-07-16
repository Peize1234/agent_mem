#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
针对 Peize1234/agent_mem 的五 Session 金融记忆测试脚本。

核心流程
========
普通历史对话：
    用户原始问题
        -> memory.search()
        -> 读取当前短期 / 中期 / 长期记忆状态
        -> 构造最终 Prompt
        -> 调用外部 LLM 生成回答
        -> memory.add([user, generated_assistant])
        -> 再次读取三层记忆状态

评测问题：
    memory.search()
        -> 构造 Prompt
        -> 调用外部 LLM
        -> 不写回 Memory
        -> 保存 predictions.jsonl
        -> 调用 parse_and_evaluate.py
        -> 额外生成 layered_scores.json

输出目录
========
results/<run_name>/
├── full_trace.txt                 # 控制台完整镜像
├── interactions.jsonl             # 36 个历史交互的结构化记录
├── evaluation_predictions.jsonl   # 9 个评测问题
├── evaluation_details.json        # 每题的三层检索详情
├── scores.json                    # 原数据集规则 / Judge 综合评分
├── layered_scores.json            # 长期事实 / 中期页面 / 短期上下文分层指标
├── final_memory_state.json         # 最终三层记忆完整状态
├── run_config.json
└── errors.jsonl

重要说明
========
1. 必须从你修改后的 agent_mem 仓库根目录运行，脚本会优先导入当前仓库中的 mem0。
2. 标准 Agent 顺序是 search -> LLM answer -> add，而不是先 add 再回答。
3. 历史数据中的 assistant 文本仅作为参考答案；真正写入 Memory 的是模型本次生成的回答。
4. 中期页的 Turn 召回通过 raw_dialogue / user_input / assistant_response 与实际运行文本匹配；
   长期记忆只评价事实 Memory ID，不要求返回原始 Turn。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# 必须在导入 mem0 前关闭遥测
os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("POSTHOG_DISABLED", "true")


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="严格单Session隔离测试 Peize1234/agent_mem 的短期、中期、长期记忆机制"
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=repo_root / "finance_memory_single_session_timeline.json",
        help="FinanceMemory-Mini-CN v4 timeline 数据",
    )
    parser.add_argument(
        "--evaluator",
        type=Path,
        default=repo_root / "parse_and_evaluate.py",
        help="已有规则评测脚本",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=repo_root / "results",
    )
    parser.add_argument(
        "--run-name",
        default=datetime.now().strftime("agent_mem_%Y%m%d_%H%M%S"),
    )
    parser.add_argument("--user-id", default="invest_user_001")
    parser.add_argument("--top-k-long", type=int, default=5)
    parser.add_argument("--long-term-all-limit", type=int, default=1000)
    parser.add_argument("--midterm-all-limit", type=int, default=1000)
    parser.add_argument("--short-term-capacity", type=int, default=4)
    parser.add_argument("--top-k-mid-topic", type=int, default=5)
    parser.add_argument("--top-k-mid-pages", type=int, default=5)
    parser.add_argument("--max-total-pages", type=int, default=5)
    parser.add_argument("--session-similarity-threshold", type=float, default=0.50)
    parser.add_argument("--deepseek-model", default=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"))
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--embedding-dims", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=2400)
    parser.add_argument(
        "--qdrant-path",
        default=None,
        help="默认写入 results/<run_name>/qdrant",
    )
    parser.add_argument(
        "--history-db-path",
        default=None,
        help="默认写入 results/<run_name>/history.db",
    )
    parser.add_argument(
        "--collection-name",
        default=None,
        help="默认使用 agent_mem_<run_name>",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="对 9 个评测问题额外调用 LLM Judge；会增加 API 成本",
    )
    parser.add_argument(
        "--midterm-enabled",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="开启或关闭中期记忆；使用 --no-midterm-enabled 关闭",
    )
    parser.add_argument(
        "--history-write-mode",
        choices=("generated", "reference"),
        default="generated",
        help=(
            "generated：写入模型实际回答，符合端到端Agent流程；"
            "reference：写入数据集标准回答，适合严格消融对照。"
        ),
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="结果目录存在时先删除，保证不会混入上次运行数据",
    )
    parser.add_argument(
        "--disable-fair-short-window-patch",
        action="store_true",
        help="关闭测试脚本对短期窗口的公平性修补；通常不应使用",
    )
    parser.add_argument(
        "--reset-before-run",
        action="store_true",
        help="Memory 初始化后调用 reset；只建议使用独立新路径时开启",
    )
    parser.add_argument(
        "--state-timing",
        choices=("before", "after", "both"),
        default="both",
        help="控制台 / 文本记录中输出回答前、回答后的完整三层状态",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="不初始化 Memory，只检查数据和执行顺序",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
    )
    return parser.parse_args()


class Tee(io.TextIOBase):
    def __init__(self, *streams: io.TextIOBase):
        self.streams = streams

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
            stream.flush()
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def normalize_llm_response(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        for key in ("content", "response", "answer", "text", "message"):
            value = raw.get(key)
            if isinstance(value, str):
                return value.strip()
        return json.dumps(raw, ensure_ascii=False, default=str)
    return str(raw).strip()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    output: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def public_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    payload = getattr(value, "payload", None)
    if isinstance(payload, dict):
        return dict(payload)
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump()
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {"value": str(value)}


def vector_row_to_dict(row: Any) -> Dict[str, Any]:
    payload = dict(getattr(row, "payload", None) or {})
    return {
        "id": str(getattr(row, "id", payload.get("id", ""))),
        "score": getattr(row, "score", None),
        "payload": payload,
    }


def build_session_scope(user_id: str, run_id: str) -> str:
    # 与 agent_mem/mem0/memory/main.py 的 _build_session_scope 一致
    values = {"user_id": user_id, "run_id": run_id}
    return "&".join(
        f"{key}={values[key]}"
        for key in sorted(values)
        if values.get(key)
    )


def pair_turns(session: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    turns = session["turns"]
    if len(turns) % 2 != 0:
        raise ValueError(f"{session['session_id']} 消息数不是偶数")
    pairs = []
    for index in range(0, len(turns), 2):
        user_turn, assistant_turn = turns[index], turns[index + 1]
        if user_turn["role"] != "user" or assistant_turn["role"] != "assistant":
            raise ValueError(
                f"{session['session_id']} 第 {index // 2 + 1} 对角色顺序错误"
            )
        pairs.append((user_turn, assistant_turn))
    return pairs


def validate_data(data: Dict[str, Any]) -> None:
    if not data.get("sessions") or not data.get("evaluation_queries"):
        raise ValueError("数据缺少 sessions 或 evaluation_queries")
    try:
        from parse_and_evaluate import validate_dataset
    except ImportError:
        validate_dataset = None
    if validate_dataset is not None:
        errors = validate_dataset(data)
        if errors:
            raise ValueError("数据校验失败:\n" + "\n".join(errors))
    seen_turns = set()
    for session in data["sessions"]:
        for user_turn, assistant_turn in pair_turns(session):
            for turn in (user_turn, assistant_turn):
                tid = turn["turn_id"]
                if tid in seen_turns:
                    raise ValueError(f"重复 turn_id：{tid}")
                seen_turns.add(tid)


# ---------------------------------------------------------------------------
# Memory 初始化
# ---------------------------------------------------------------------------

def build_memory(args: argparse.Namespace, run_dir: Path):
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY。\n"
            'Linux/WSL: export DEEPSEEK_API_KEY="你的Key"\n'
            'PowerShell: $env:DEEPSEEK_API_KEY="你的Key"'
        )

    # 强制优先导入当前仓库
    repo_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(repo_root))

    from mem0 import Memory

    qdrant_path = Path(args.qdrant_path) if args.qdrant_path else run_dir / "qdrant"
    history_path = Path(args.history_db_path) if args.history_db_path else run_dir / "history.db"
    collection_name = args.collection_name or f"agent_mem_{args.run_name}"

    config = {
        "llm": {
            "provider": "deepseek",
            "config": {
                "model": args.deepseek_model,
                "api_key": api_key,
                "temperature": args.temperature,
                "max_tokens": args.max_tokens,
            },
        },
        "embedder": {
            "provider": "huggingface",
            "config": {
                "model": args.embedding_model,
            },
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection_name,
                "path": str(qdrant_path),
                "embedding_model_dims": args.embedding_dims,
            },
        },
        "history_db_path": str(history_path),
        "midterm": {
            "enabled": args.midterm_enabled,
            "short_term_capacity": args.short_term_capacity,
            "session_similarity_threshold": args.session_similarity_threshold,
            "top_k_sessions": args.top_k_mid_topic,
            "top_k_pages": args.top_k_mid_pages,
            "max_total_pages": args.max_total_pages,
        },
    }
    memory = Memory.from_config(config)
    return memory, config



def apply_fair_short_window_patch(memory, capacity: int) -> None:
    """
    让short_term_capacity与midterm.enabled解耦。

    原仓库在中期关闭时会走SQLiteManager.save_messages()的默认max_messages=10，
    导致消融实验的短期窗口从4变成10。此补丁只作用于当前测试进程：
    - 中期开启：保留capacity条，淘汰QA进入中期；
    - 中期关闭：仍保留capacity条，但淘汰内容直接丢弃。
    """
    from types import MethodType

    capacity = max(int(capacity), 0)
    if capacity % 2 != 0:
        capacity += 1

    def fixed_capacity(self):
        return capacity

    def fixed_save_short_term(self, messages, session_scope):
        enabled = bool(getattr(self, "_midterm_enabled", lambda: False)())
        evicted = self.db.save_messages(
            messages,
            session_scope,
            max_messages=capacity,
            return_evicted=enabled,
        )
        if enabled:
            evicted = evicted or []
            expand = getattr(self, "_expand_evicted_messages_for_qa_pairs", None)
            if callable(expand):
                evicted = expand(evicted, session_scope)
            self._last_evicted_messages = evicted
            return evicted

        self._last_evicted_messages = []
        return []

    memory._short_term_capacity = MethodType(fixed_capacity, memory)
    memory._save_short_term_messages = MethodType(fixed_save_short_term, memory)


# ---------------------------------------------------------------------------
# 三层状态读取
# ---------------------------------------------------------------------------

def get_all_long_term(memory, user_id: str, run_id: str, limit: int) -> List[Dict[str, Any]]:
    filters = {"user_id": user_id, "run_id": run_id}
    try:
        raw = memory.get_all(filters=filters)
    except TypeError:
        raw = memory.get_all(user_id=user_id, run_id=run_id)

    if isinstance(raw, dict):
        rows = raw.get("results", raw.get("memories", []))
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    output = []
    for item in rows[:limit]:
        if not isinstance(item, dict):
            item = public_dict(item)
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        output.append({
            "id": str(item.get("id") or item.get("memory_id") or ""),
            "memory": item.get("memory") or item.get("data") or item.get("text") or "",
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "metadata": metadata,
            "raw": item,
        })
    return output


def get_all_midterm(memory, user_id: str, run_id: str, limit: int) -> Dict[str, List[Dict[str, Any]]]:
    if not getattr(memory, "_midterm_enabled", lambda: False)():
        return {"sessions": [], "pages": []}

    filters = {"user_id": user_id, "run_id": run_id}
    session_rows = memory.midterm_memory.list_sessions(filters=filters, top_k=limit)
    page_rows = memory.midterm_memory.list_pages(filters=filters, top_k=limit)

    sessions = []
    for row in session_rows:
        item = vector_row_to_dict(row)
        payload = item["payload"]
        sessions.append({
            "id": item["id"],
            "summary": payload.get("summary", ""),
            "summary_keywords": payload.get("summary_keywords", []),
            "page_ids": payload.get("page_ids", []),
            "N_visit": payload.get("N_visit"),
            "L_interaction": payload.get("L_interaction"),
            "R_recency": payload.get("R_recency"),
            "H_segment": payload.get("H_segment"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "user_id": payload.get("user_id"),
            "run_id": payload.get("run_id"),
            "raw": item,
        })

    pages = []
    for row in page_rows:
        item = vector_row_to_dict(row)
        payload = item["payload"]
        pages.append({
            "id": item["id"],
            "session_id": payload.get("session_id"),
            "user_input": payload.get("user_input", ""),
            "assistant_response": payload.get("assistant_response", ""),
            "raw_dialogue": payload.get("raw_dialogue", ""),
            "summary": payload.get("summary", ""),
            "keywords": payload.get("keywords", []),
            "pre_page": payload.get("pre_page"),
            "next_page": payload.get("next_page"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "user_id": payload.get("user_id"),
            "run_id": payload.get("run_id"),
            "raw": item,
        })

    return {"sessions": sessions, "pages": pages}


def get_short_term(memory, user_id: str, run_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
    scope = build_session_scope(user_id, run_id)
    rows = memory.db.get_last_messages(scope, limit=limit)
    return [
        {
            "role": row.get("role"),
            "content": row.get("content"),
            "name": row.get("name"),
            "created_at": row.get("created_at"),
            "session_scope": scope,
        }
        for row in rows
    ]


def snapshot_memory_state(
    memory,
    user_id: str,
    run_id: str,
    long_limit: int,
    mid_limit: int,
) -> Dict[str, Any]:
    return {
        "long_term": get_all_long_term(memory, user_id, run_id, long_limit),
        "mid_term": get_all_midterm(memory, user_id, run_id, mid_limit),
        "short_term": get_short_term(memory, user_id, run_id),
    }


# ---------------------------------------------------------------------------
# Search 结果分类和 Prompt 构建
# ---------------------------------------------------------------------------

def normalize_search_results(raw: Any) -> Dict[str, List[Dict[str, Any]]]:
    if isinstance(raw, dict):
        rows = raw.get("results", [])
    elif isinstance(raw, list):
        rows = raw
    else:
        rows = []

    grouped = {
        "long_term": [],
        "mid_term_sessions": [],
        "mid_term_pages": [],
        "unknown": [],
    }

    for rank, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            item = public_dict(item)
        source = item.get("source")
        normalized = {
            "rank": rank,
            "id": str(item.get("id") or ""),
            "source": source or "long_term",
            "memory": item.get("memory") or "",
            "score": item.get("score"),
            "session_id": item.get("session_id"),
            "summary": item.get("summary"),
            "raw_dialogue": item.get("raw_dialogue"),
            "keywords": item.get("keywords") or item.get("summary_keywords") or [],
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "user_id": item.get("user_id"),
            "run_id": item.get("run_id"),
            "metadata": item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            "raw": item,
        }
        if source == "mid_term_session":
            grouped["mid_term_sessions"].append(normalized)
        elif source == "mid_term_page":
            grouped["mid_term_pages"].append(normalized)
        elif source in (None, "long_term"):
            grouped["long_term"].append(normalized)
        else:
            grouped["unknown"].append(normalized)
    return grouped


def get_midterm_retrieval_stats(memory) -> Dict[str, int]:
    stats = getattr(getattr(memory, "_midterm_retriever", None), "last_search_stats", None) or {}
    return {
        "retrieved_sessions": int(stats.get("retrieved_sessions", 0) or 0),
        "candidate_pages_before_dedupe": int(stats.get("candidate_pages_before_dedupe", 0) or 0),
        "candidate_pages_after_dedupe": int(stats.get("candidate_pages_after_dedupe", 0) or 0),
        "returned_pages": int(stats.get("returned_pages", 0) or 0),
    }


def print_midterm_retrieval_stats(stats: Dict[str, int]) -> None:
    print_separator("中期检索统计", "-")
    print(f"召回的主题数量：{stats['retrieved_sessions']}")
    print(f"去重前候选 Page 数量：{stats['candidate_pages_before_dedupe']}")
    print(f"去重后数量：{stats['candidate_pages_after_dedupe']}")
    print(f"最终返回 Page 数量：{stats['returned_pages']}")


def format_short_term_for_prompt(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "（当前 Session 暂无短期消息）"
    return "\n".join(
        f"{index}. {row.get('role')}: {row.get('content')}"
        for index, row in enumerate(rows, start=1)
    )


def format_long_term_for_prompt(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "（未检索到相关长期记忆）"
    return "\n".join(
        f"{index}. {row.get('memory')}（score={row.get('score')}）"
        for index, row in enumerate(rows, start=1)
    )


def format_midterm_for_prompt(grouped: Dict[str, List[Dict[str, Any]]]) -> str:
    sessions = grouped["mid_term_sessions"]
    pages = grouped["mid_term_pages"]
    if not sessions and not pages:
        return "（未检索到相关中期记忆）"

    parts = []
    if sessions:
        parts.append("【主题 Session 摘要】")
        for index, item in enumerate(sessions, start=1):
            parts.append(
                f"{index}. {item.get('summary') or item.get('memory')}"
                f"（session_id={item.get('session_id')}, score={item.get('score')}）"
            )

    if pages:
        parts.append("【相关原始对话页】")
        for index, item in enumerate(pages, start=1):
            dialogue = item.get("raw_dialogue") or item.get("memory") or ""
            parts.append(
                f"{index}. {dialogue}"
                f"\n   page_id={item.get('id')}, session_id={item.get('session_id')}, "
                f"score={item.get('score')}"
            )
    return "\n".join(parts)


def build_prompt_messages(
    question: str,
    question_date: str,
    short_term: Sequence[Dict[str, Any]],
    search_grouped: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    short_text = format_short_term_for_prompt(short_term)
    long_text = format_long_term_for_prompt(search_grouped["long_term"])
    mid_text = format_midterm_for_prompt(search_grouped)

    system_prompt = (
        "你是一个严谨的中文金融投资问答助手。\n"
        "请按照以下优先级使用上下文：\n"
        "当前测试采用严格Session隔离，只允许使用当前run_id中的内容。\n"
        "1. 当前Session的短期原始对话；\n"
        "2. 当前Session的中期主题摘要和原始对话页；\n"
        "3. 当前Session的长期抽象事实。\n\n"
        "规则：\n"
        "- 先直接回答用户当前问题，再解释原因；\n"
        "- 用户个人信息只能来自给定记忆，不得猜测；\n"
        "- 新信息覆盖旧信息，不能把过期值当作当前事实；\n"
        "- 已删除或禁止使用的信息不能泄露；\n"
        "- 只使用与当前问题相关的记忆，避免过度个性化；\n"
        "- 信息不足时明确说明无法确认；\n"
        "- 不承诺金融收益。"
    )

    user_prompt = (
        f"当前时间：{question_date}\n\n"
        f"===== 当前 Session 短期记忆 =====\n{short_text}\n\n"
        f"===== Search 命中的中期记忆 =====\n{mid_text}\n\n"
        f"===== Search 命中的长期记忆 =====\n{long_text}\n\n"
        f"===== 用户当前问题 =====\n{question}\n\n"
        "请基于上述信息回答。"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def prompt_to_text(messages: Sequence[Dict[str, str]]) -> str:
    return "\n\n".join(
        f"[{item['role'].upper()}]\n{item['content']}"
        for item in messages
    )


# ---------------------------------------------------------------------------
# 输出格式
# ---------------------------------------------------------------------------

def print_separator(title: str, char: str = "=") -> None:
    print("\n" + char * 110)
    print(title)
    print(char * 110)


def print_json_section(title: str, value: Any) -> None:
    print_separator(title, "-")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def print_memory_state(title: str, state: Dict[str, Any]) -> None:
    print_separator(title)
    print_json_section("当前长期记忆：全部内容", state["long_term"])
    print_json_section("当前中期记忆：主题 Session 全部内容", state["mid_term"]["sessions"])
    print_json_section("当前中期记忆：原始对话 Page 全部内容", state["mid_term"]["pages"])
    print_json_section("当前短期记忆：当前 run_id 全部内容", state["short_term"])


# ---------------------------------------------------------------------------
# Turn / Memory 来源映射
# ---------------------------------------------------------------------------

@dataclass
class TextSource:
    session_id: str
    user_turn_id: str
    assistant_turn_id: str
    benchmark_memory_ids: List[str]


class SourceRegistry:
    def __init__(self) -> None:
        self.user_text: Dict[str, TextSource] = {}
        self.assistant_text: Dict[str, TextSource] = {}

    def register(
        self,
        session_id: str,
        user_turn_id: str,
        assistant_turn_id: str,
        benchmark_memory_ids: List[str],
        user_text: str,
        assistant_text: str,
    ) -> None:
        source = TextSource(
            session_id=session_id,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            benchmark_memory_ids=benchmark_memory_ids,
        )
        self.user_text[user_text.strip()] = source
        self.assistant_text[assistant_text.strip()] = source

    def source_from_page(self, page: Dict[str, Any]) -> Optional[TextSource]:
        raw_item = page.get("raw") if isinstance(page.get("raw"), dict) else {}
        user_input = str(
            raw_item.get("user_input")
            or page.get("user_input")
            or ""
        ).strip()
        assistant_response = str(
            raw_item.get("assistant_response")
            or page.get("assistant_response")
            or ""
        ).strip()

        direct = self.user_text.get(user_input) or self.assistant_text.get(assistant_response)
        if direct:
            return direct

        # agent_mem 的 MidTermRetriever._format_page() 一定返回 raw_dialogue，
        # 但默认不直接返回 user_input / assistant_response。
        dialogue = str(
            page.get("raw_dialogue")
            or raw_item.get("raw_dialogue")
            or page.get("memory")
            or ""
        )
        for text, source in self.user_text.items():
            if text and text in dialogue:
                return source
        for text, source in self.assistant_text.items():
            if text and text in dialogue:
                return source
        return None

    def source_from_long_term(self, item: Dict[str, Any]) -> Optional[TextSource]:
        metadata = item.get("metadata") or {}
        user_tid = metadata.get("benchmark_user_turn_id")
        assistant_tid = metadata.get("benchmark_assistant_turn_id")
        session_id = metadata.get("benchmark_session_id")
        memory_ids = metadata.get("benchmark_memory_ids") or []
        if isinstance(memory_ids, str):
            memory_ids = [memory_ids]
        if session_id and (user_tid or assistant_tid):
            return TextSource(
                session_id=str(session_id),
                user_turn_id=str(user_tid or ""),
                assistant_turn_id=str(assistant_tid or ""),
                benchmark_memory_ids=list(memory_ids),
            )
        return None


def extract_retrieval_evidence(
    grouped: Dict[str, List[Dict[str, Any]]],
    registry: SourceRegistry,
) -> Dict[str, Any]:
    long_sessions: List[str] = []
    long_turns: List[str] = []
    long_memories: List[str] = []

    for item in grouped["long_term"]:
        source = registry.source_from_long_term(item)
        if source:
            long_sessions.append(source.session_id)
            long_turns.extend([source.user_turn_id, source.assistant_turn_id])
            long_memories.extend(source.benchmark_memory_ids)

    mid_sessions: List[str] = []
    mid_turns: List[str] = []
    mid_memories: List[str] = []

    for page in grouped["mid_term_pages"]:
        source = registry.source_from_page(page)
        if source:
            mid_sessions.append(source.session_id)
            mid_turns.extend([source.user_turn_id, source.assistant_turn_id])
            mid_memories.extend(source.benchmark_memory_ids)

    return {
        "long_term": {
            "session_ids": unique_keep_order(long_sessions),
            "turn_ids": unique_keep_order(long_turns),
            "memory_ids": unique_keep_order(long_memories),
        },
        "mid_term": {
            "session_ids": unique_keep_order(mid_sessions),
            "turn_ids": unique_keep_order(mid_turns),
            "memory_ids": unique_keep_order(mid_memories),
        },
        "combined": {
            "session_ids": unique_keep_order(long_sessions + mid_sessions),
            "turn_ids": unique_keep_order(long_turns + mid_turns),
            "memory_ids": unique_keep_order(long_memories + mid_memories),
        },
    }



def find_isolation_violations(
    grouped: Dict[str, List[Dict[str, Any]]],
    expected_run_id: str,
) -> List[Dict[str, Any]]:
    violations = []
    for layer, rows in grouped.items():
        for item in rows:
            result_run_id = item.get("run_id")
            # 中期结果应带run_id；长期结果也应带run_id。
            if result_run_id and result_run_id != expected_run_id:
                violations.append({
                    "layer": layer,
                    "id": item.get("id"),
                    "result_run_id": result_run_id,
                    "expected_run_id": expected_run_id,
                    "memory": item.get("memory") or item.get("summary"),
                })
    return violations


# ---------------------------------------------------------------------------
# LLM Judge
# ---------------------------------------------------------------------------

JUDGE_KEYS = (
    "financial_correctness",
    "memory_grounding",
    "temporal_update_consistency",
    "relevance_and_personalization",
    "safety_and_uncertainty",
    "clarity",
)


def extract_json_object(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(cleaned[start : end + 1])
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def judge_answer(memory, query: Dict[str, Any], answer: str) -> Tuple[Dict[str, int], Dict[str, Any]]:
    messages = [
        {
            "role": "system",
            "content": (
                "你是金融记忆 Agent 评测员。"
                "请按语义判断，不要求候选答案与参考答案逐字一致。"
                "泄露删除信息、把旧值当当前值、编造用户事实或给出危险承诺属于严重错误。"
            ),
        },
        {
            "role": "user",
            "content": f"""
问题：
{query["question"]}

参考答案：
{query["reference_answer"]}

候选答案：
{answer}

当前有效 Memory ID：
{query.get("required_active_memory_ids", [])}

过期 Memory ID：
{query.get("outdated_memory_ids", [])}

禁止使用 Memory ID：
{query.get("forbidden_memory_ids", [])}

请按 0-4 分评价：
- financial_correctness
- memory_grounding
- temporal_update_consistency
- relevance_and_personalization
- safety_and_uncertainty
- clarity

只输出 JSON：
{{
  "scores": {{
    "financial_correctness": 0,
    "memory_grounding": 0,
    "temporal_update_consistency": 0,
    "relevance_and_personalization": 0,
    "safety_and_uncertainty": 0,
    "clarity": 0
  }},
  "reason": "简要理由"
}}
""",
        },
    ]
    raw = normalize_llm_response(memory.llm.generate_response(messages=messages))
    parsed = extract_json_object(raw)
    scores_raw = parsed.get("scores", {})
    scores: Dict[str, int] = {}
    for key in JUDGE_KEYS:
        try:
            value = int(round(float(scores_raw.get(key, 0))))
        except (TypeError, ValueError):
            value = 0
        scores[key] = min(4, max(0, value))
    return scores, {"raw": raw, "parsed": parsed}


# ---------------------------------------------------------------------------
# 分层指标
# ---------------------------------------------------------------------------

def recall(retrieved: Sequence[str], relevant: Sequence[str]) -> Optional[float]:
    relevant_set = set(relevant)
    if not relevant_set:
        return None
    return len(set(retrieved) & relevant_set) / len(relevant_set)


def precision(retrieved: Sequence[str], relevant: Sequence[str]) -> Optional[float]:
    retrieved_set = set(retrieved)
    if not retrieved_set:
        return 1.0 if not relevant else 0.0
    return len(retrieved_set & set(relevant)) / len(retrieved_set)


def build_layered_scores(
    queries: Dict[str, Dict[str, Any]],
    details: List[Dict[str, Any]],
) -> Dict[str, Any]:
    per_query = []
    for item in details:
        query = queries[item["query_id"]]
        if "retrieval" not in query.get("metric_groups", []):
            per_query.append({
                "query_id": item["query_id"],
                "excluded_from_memory_recall": True,
                "reason": "metric_groups does not include retrieval",
                "long_term_fact_recall": None,
                "long_term_fact_precision": None,
                "mid_term_turn_recall": None,
                "mid_term_session_recall": None,
                "combined_fact_recall": None,
                "combined_turn_recall": None,
                "combined_session_recall": None,
                "retrieved_long_term_count": len(item["search_results"]["long_term"]),
                "retrieved_mid_term_session_count": len(item["search_results"]["mid_term_sessions"]),
                "retrieved_mid_term_page_count": len(item["search_results"]["mid_term_pages"]),
                "short_term_message_count": len(item["short_term_before"]),
                "session_isolation_violation_count": len(
                    item.get("session_isolation_violations", [])
                ),
            })
            continue
        evidence = item["retrieval_evidence"]
        required_memories = query.get("required_active_memory_ids", [])
        answer_turns = query.get("answer_turn_ids", [])
        answer_sessions = query.get(
            "required_active_evidence_session_ids",
            query.get("answer_session_ids", []),
        )

        long_data = evidence["long_term"]
        mid_data = evidence["mid_term"]
        combined = evidence["combined"]

        row = {
            "query_id": item["query_id"],
            # 长期：只测抽象事实，不把来源 Turn 当硬指标
            "long_term_fact_recall": recall(long_data["memory_ids"], required_memories),
            "long_term_fact_precision": precision(long_data["memory_ids"], required_memories),
            # 中期：实际返回原始页面，因此测 Turn / Page 证据
            "mid_term_turn_recall": recall(mid_data["turn_ids"], answer_turns),
            "mid_term_session_recall": recall(mid_data["session_ids"], answer_sessions),
            # 最终提供给模型的联合证据
            "combined_fact_recall": recall(combined["memory_ids"], required_memories),
            "combined_turn_recall": recall(combined["turn_ids"], answer_turns),
            "combined_session_recall": recall(combined["session_ids"], answer_sessions),
            "retrieved_long_term_count": len(item["search_results"]["long_term"]),
            "retrieved_mid_term_session_count": len(item["search_results"]["mid_term_sessions"]),
            "retrieved_mid_term_page_count": len(item["search_results"]["mid_term_pages"]),
            "short_term_message_count": len(item["short_term_before"]),
            "session_isolation_violation_count": len(
                item.get("session_isolation_violations", [])
            ),
        }
        per_query.append(row)

    def avg(key: str) -> Optional[float]:
        values = [row[key] for row in per_query if row.get(key) is not None]
        return sum(values) / len(values) if values else None

    summary = {
        "query_count": len(per_query),
        "retrieval_query_count": sum(
            1 for row in per_query if not row.get("excluded_from_memory_recall")
        ),
        "memory_recall_excluded_query_ids": [
            row["query_id"]
            for row in per_query
            if row.get("excluded_from_memory_recall")
        ],
        "long_term_fact_recall_mean": avg("long_term_fact_recall"),
        "long_term_fact_precision_mean": avg("long_term_fact_precision"),
        "mid_term_turn_recall_mean": avg("mid_term_turn_recall"),
        "mid_term_session_recall_mean": avg("mid_term_session_recall"),
        "combined_fact_recall_mean": avg("combined_fact_recall"),
        "combined_turn_recall_mean": avg("combined_turn_recall"),
        "combined_session_recall_mean": avg("combined_session_recall"),
        "session_isolation_violation_total": sum(
            row["session_isolation_violation_count"]
            for row in per_query
        ),
    }
    return {"summary": summary, "per_query": per_query}


# ---------------------------------------------------------------------------
# 主 Runner
# ---------------------------------------------------------------------------

class Runner:
    def __init__(
        self,
        args: argparse.Namespace,
        data: Dict[str, Any],
        memory,
        run_dir: Path,
    ) -> None:
        self.args = args
        self.data = data
        self.memory = memory
        self.run_dir = run_dir
        self.registry = SourceRegistry()
        self.interactions: List[Dict[str, Any]] = []
        self.evaluation_predictions: List[Dict[str, Any]] = []
        self.evaluation_details: List[Dict[str, Any]] = []
        self.errors_path = run_dir / "errors.jsonl"
        self.query_map = {
            query["query_id"]: query
            for query in data["evaluation_queries"]
        }

    def search(self, question: str, run_id: str) -> Tuple[Any, Dict[str, List[Dict[str, Any]]], float, Dict[str, int]]:
        start = time.perf_counter()
        raw = self.memory.search(
            question,
            filters={"user_id": self.args.user_id, "run_id": run_id},
            top_k=self.args.top_k_long,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        grouped = normalize_search_results(raw)
        midterm_stats = get_midterm_retrieval_stats(self.memory)
        return raw, grouped, latency_ms, midterm_stats

    def answer(
        self,
        question: str,
        question_date: str,
        run_id: str,
        grouped: Dict[str, List[Dict[str, Any]]],
    ) -> Tuple[str, List[Dict[str, str]], List[Dict[str, Any]], float]:
        short_term = get_short_term(
            self.memory,
            self.args.user_id,
            run_id,
        )[-self.args.short_term_capacity:]
        prompt_messages = build_prompt_messages(
            question=question,
            question_date=question_date,
            short_term=short_term,
            search_grouped=grouped,
        )
        start = time.perf_counter()
        answer = normalize_llm_response(
            self.memory.llm.generate_response(messages=prompt_messages)
        )
        latency_ms = (time.perf_counter() - start) * 1000
        return answer, prompt_messages, short_term, latency_ms

    def run_history_pair(
        self,
        session: Dict[str, Any],
        pair_index: int,
        user_turn: Dict[str, Any],
        reference_assistant_turn: Dict[str, Any],
    ) -> None:
        session_id = session["session_id"]
        run_id = session_id
        event_time = (
            parse_iso(session["timestamp"]) + timedelta(minutes=pair_index)
        ).isoformat()
        question = user_turn["content"]

        print_separator(
            f"历史交互 | {session_id} | pair {pair_index:02d} | "
            f"{user_turn['turn_id']} -> {reference_assistant_turn['turn_id']}"
        )
        print("【用户原始问题】")
        print(question)

        raw_search, grouped, search_latency, midterm_stats = self.search(question, run_id)
        state_before = snapshot_memory_state(
            self.memory,
            self.args.user_id,
            run_id,
            self.args.long_term_all_limit,
            self.args.midterm_all_limit,
        )

        answer, prompt_messages, short_before, answer_latency = self.answer(
            question=question,
            question_date=event_time,
            run_id=run_id,
            grouped=grouped,
        )

        print_separator("系统处理完成后的完整 Prompt", "-")
        print(prompt_to_text(prompt_messages))

        print_json_section("本轮 Search：长期记忆命中", grouped["long_term"])
        print_json_section("本轮 Search：中期主题 Session 命中", grouped["mid_term_sessions"])
        print_json_section("本轮 Search：中期原始 Page 命中", grouped["mid_term_pages"])
        print_midterm_retrieval_stats(midterm_stats)

        if self.args.state_timing in ("before", "both"):
            print_memory_state("回答前：三层记忆完整状态", state_before)

        print_separator("大模型生成回答", "-")
        print(answer)

        benchmark_memory_ids = unique_keep_order(
            list(user_turn.get("memory_ids", []))
            + list(reference_assistant_turn.get("memory_ids", []))
        )
        metadata = {
            "benchmark_session_id": session_id,
            "benchmark_user_turn_id": user_turn["turn_id"],
            "benchmark_assistant_turn_id": reference_assistant_turn["turn_id"],
            "benchmark_memory_ids": benchmark_memory_ids,
            "benchmark_pair_index": pair_index,
            "benchmark_session_timestamp": session["timestamp"],
        }
        assistant_text_to_store = (
            answer
            if self.args.history_write_mode == "generated"
            else reference_assistant_turn["content"]
        )
        messages_to_add = [
            {
                "role": "user",
                "content": question,
                "created_at": event_time,
            },
            {
                "role": "assistant",
                "content": assistant_text_to_store,
                "created_at": (
                    parse_iso(event_time) + timedelta(seconds=1)
                ).isoformat(),
            },
        ]

        start = time.perf_counter()
        add_result = self.memory.add(
            messages_to_add,
            user_id=self.args.user_id,
            run_id=run_id,
            metadata=metadata,
        )
        add_latency = (time.perf_counter() - start) * 1000

        self.registry.register(
            session_id=session_id,
            user_turn_id=user_turn["turn_id"],
            assistant_turn_id=reference_assistant_turn["turn_id"],
            benchmark_memory_ids=benchmark_memory_ids,
            user_text=question,
            assistant_text=assistant_text_to_store,
        )

        state_after = snapshot_memory_state(
            self.memory,
            self.args.user_id,
            run_id,
            self.args.long_term_all_limit,
            self.args.midterm_all_limit,
        )

        print_json_section("本轮 add() 返回结果", add_result)
        if self.args.state_timing in ("after", "both"):
            print_memory_state("回答后 / add 后：三层记忆完整状态", state_after)

        record = {
            "type": "history_interaction",
            "session_id": session_id,
            "run_id": run_id,
            "pair_index": pair_index,
            "user_turn_id": user_turn["turn_id"],
            "reference_assistant_turn_id": reference_assistant_turn["turn_id"],
            "question": question,
            "reference_answer": reference_assistant_turn["content"],
            "generated_answer": answer,
            "stored_assistant_answer": assistant_text_to_store,
            "history_write_mode": self.args.history_write_mode,
            "prompt_messages": prompt_messages,
            "search_raw": raw_search,
            "search_results": grouped,
            "midterm_retrieval_stats": midterm_stats,
            "short_term_before": short_before,
            "state_before": state_before,
            "add_result": add_result,
            "state_after": state_after,
            "latency_ms": {
                "search": round(search_latency, 1),
                "answer": round(answer_latency, 1),
                "add": round(add_latency, 1),
            },
        }
        self.interactions.append(record)
        append_jsonl(self.run_dir / "interactions.jsonl", record)

    def run_evaluation_query(self, query: Dict[str, Any], current_run_id: str) -> None:
        qid = query["query_id"]
        question = query["question"]
        target_session_id = query.get("target_session_id")
        if target_session_id != current_run_id:
            raise ValueError(
                f"{qid} target_session_id={target_session_id} "
                f"但当前run_id={current_run_id}"
            )

        print_separator(f"正式评测问题 | {qid} | {query['question_type']}")
        print("【用户原始问题】")
        print(question)

        raw_search, grouped, search_latency, midterm_stats = self.search(question, current_run_id)
        state_before = snapshot_memory_state(
            self.memory,
            self.args.user_id,
            current_run_id,
            self.args.long_term_all_limit,
            self.args.midterm_all_limit,
        )
        answer, prompt_messages, short_before, answer_latency = self.answer(
            question=question,
            question_date=query["question_date"],
            run_id=current_run_id,
            grouped=grouped,
        )

        print_separator("系统处理完成后的完整 Prompt", "-")
        print(prompt_to_text(prompt_messages))
        print_json_section("评测 Search：长期记忆命中", grouped["long_term"])
        print_json_section("评测 Search：中期主题 Session 命中", grouped["mid_term_sessions"])
        print_json_section("评测 Search：中期原始 Page 命中", grouped["mid_term_pages"])
        print_midterm_retrieval_stats(midterm_stats)
        print_memory_state("评测时：三层记忆完整状态", state_before)
        print_separator("大模型生成回答", "-")
        print(answer)

        evidence = extract_retrieval_evidence(grouped, self.registry)
        isolation_violations = find_isolation_violations(
            grouped,
            current_run_id,
        )
        if isolation_violations:
            print_json_section(
                "Session隔离违规结果",
                isolation_violations,
            )

        judge_scores: Dict[str, int] = {}
        judge_details: Dict[str, Any] = {}
        judge_latency = 0.0
        if self.args.judge:
            start = time.perf_counter()
            judge_scores, judge_details = judge_answer(
                self.memory,
                query,
                answer,
            )
            judge_latency = (time.perf_counter() - start) * 1000
            print_json_section("LLM Judge 评分", {
                "scores": judge_scores,
                "details": judge_details,
            })

        prediction = {
            "query_id": qid,
            "system_name": self.args.run_name,
            "answer": answer,
            "retrieved_session_ids": evidence["combined"]["session_ids"],
            "retrieved_turn_ids": evidence["combined"]["turn_ids"],
            "retrieved_memory_ids": evidence["combined"]["memory_ids"],
            "latency_ms": round(
                search_latency + answer_latency + judge_latency,
                1,
            ),
            "prompt_tokens": None,
            "completion_tokens": None,
            "estimated_cost": None,
            "judge_scores": judge_scores,
            "session_isolation_violation_count": len(isolation_violations),
        }
        self.evaluation_predictions.append(prediction)

        detail = {
            "query_id": qid,
            "question": question,
            "reference_answer": query["reference_answer"],
            "generated_answer": answer,
            # "stored_assistant_answer": assistant_text_to_store,
            # "history_write_mode": self.args.history_write_mode,
            "prompt_messages": prompt_messages,
            "search_raw": raw_search,
            "search_results": grouped,
            "midterm_retrieval_stats": midterm_stats,
            "retrieval_evidence": evidence,
            "session_isolation_violations": isolation_violations,
            "short_term_before": short_before,
            "memory_state": state_before,
            "judge_scores": judge_scores,
            "judge_details": judge_details,
            "latency_ms": {
                "search": round(search_latency, 1),
                "answer": round(answer_latency, 1),
                "judge": round(judge_latency, 1),
            },
        }
        self.evaluation_details.append(detail)

    def run(self) -> None:
        sessions = sorted(
            self.data["sessions"],
            key=lambda item: parse_iso(item["timestamp"]),
        )
        queries_by_session: Dict[str, List[Dict[str, Any]]] = {}
        for query in self.data["evaluation_queries"]:
            sid = query.get("target_session_id")
            if not sid:
                raise ValueError(f"{query['query_id']} 缺少 target_session_id")
            if query.get("visible_session_ids") != [sid]:
                raise ValueError(
                    f"{query['query_id']} visible_session_ids必须严格等于[{sid}]"
                )
            if query.get("answer_session_ids") != [sid]:
                raise ValueError(
                    f"{query['query_id']} answer_session_ids必须严格等于[{sid}]"
                )
            queries_by_session.setdefault(sid, []).append(query)

        completed_queries = set()
        for session in sessions:
            session_id = session["session_id"]
            print_separator(
                f"开始独立 Session {session_id} | {session['title']}"
            )
            print("隔离范围："
                  f"user_id={self.args.user_id}, run_id={session_id}")
            print(f"session_goal: {session.get('session_goal')}")

            for pair_index, (user_turn, assistant_turn) in enumerate(
                pair_turns(session), start=1
            ):
                try:
                    self.run_history_pair(
                        session,
                        pair_index,
                        user_turn,
                        assistant_turn,
                    )
                except Exception as exc:
                    self.handle_error(
                        "history",
                        f"{session_id}:pair{pair_index}",
                        exc,
                    )

            for query in sorted(
                queries_by_session.get(session_id, []),
                key=lambda item: parse_iso(item["question_date"]),
            ):
                try:
                    self.run_evaluation_query(
                        query,
                        current_run_id=session_id,
                    )
                    completed_queries.add(query["query_id"])
                except Exception as exc:
                    self.handle_error("evaluation", query["query_id"], exc)

        expected = {q["query_id"] for q in self.data["evaluation_queries"]}
        missing = sorted(expected - completed_queries)
        if missing:
            raise RuntimeError(f"存在未执行评测问题：{missing}")

    def handle_error(self, stage: str, identifier: str, exc: Exception) -> None:
        row = {
            "stage": stage,
            "identifier": identifier,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        append_jsonl(self.errors_path, row)
        print_json_section("发生错误", row)
        if not self.args.continue_on_error:
            raise exc

    def save_results(self) -> None:
        write_jsonl(
            self.run_dir / "evaluation_predictions.jsonl",
            self.evaluation_predictions,
        )
        write_json(
            self.run_dir / "evaluation_details.json",
            self.evaluation_details,
        )
        layered = build_layered_scores(
            self.query_map,
            self.evaluation_details,
        )
        write_json(self.run_dir / "layered_scores.json", layered)

        final_state = {}
        for session in self.data["sessions"]:
            sid = session["session_id"]
            final_state[sid] = snapshot_memory_state(
                self.memory,
                self.args.user_id,
                sid,
                self.args.long_term_all_limit,
                self.args.midterm_all_limit,
            )
        write_json(self.run_dir / "final_memory_state_by_session.json", final_state)

        print_json_section("分层评测汇总", layered["summary"])

        if self.args.evaluator.exists():
            command = [
                sys.executable,
                str(self.args.evaluator),
                "evaluate",
                "--data",
                str(self.args.data),
                "--pred",
                str(self.run_dir / "evaluation_predictions.jsonl"),
                "--out",
                str(self.run_dir / "scores.json"),
            ]
            print_separator("运行原数据集评测脚本")
            print(" ".join(command))
            subprocess.run(command, check=True)
        else:
            print(f"[WARN] 找不到 evaluator：{self.args.evaluator}")

    def close(self) -> None:
        try:
            client = getattr(
                getattr(self.memory, "vector_store", None),
                "client",
                None,
            )
            close = getattr(client, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            print(f"[WARN] 关闭 Qdrant 失败：{exc}")

        try:
            db = getattr(self.memory, "db", None)
            close = getattr(db, "close", None)
            if callable(close):
                close()
        except Exception as exc:
            print(f"[WARN] 关闭 SQLite 失败：{exc}")


# ---------------------------------------------------------------------------
# Dry-run 和入口
# ---------------------------------------------------------------------------

def print_dry_run(data: Dict[str, Any]) -> None:
    print(f"Benchmark: {data.get('benchmark_name')} v{data.get('version')}")
    print(f"Sessions: {len(data['sessions'])}")
    print(f"Messages: {sum(len(s['turns']) for s in data['sessions'])}")
    print(f"Evaluation queries: {len(data['evaluation_queries'])}")
    print("Isolation: strict user_id + run_id; cross-session retrieval disabled")
    print()
    by_session: Dict[str, List[Dict[str, Any]]] = {}
    for query in data["evaluation_queries"]:
        by_session.setdefault(query["target_session_id"], []).append(query)
    for session in data["sessions"]:
        sid = session["session_id"]
        print(
            f"{session['timestamp']} SESSION {sid} "
            f"pairs={len(pair_turns(session))} {session['title']}"
        )
        for query in by_session.get(sid, []):
            print(f"  EVAL {query['query_id']}: {query['question']}")


def main() -> None:
    args = parse_args()
    args.data = args.data.resolve()
    args.evaluator = args.evaluator.resolve()
    args.output_root = args.output_root.resolve()

    if args.short_term_capacity < 0:
        raise ValueError("--short-term-capacity 不能小于 0")
    if args.max_total_pages < 0:
        raise ValueError("--max-total-pages 不能小于 0")
    if args.short_term_capacity % 2 != 0:
        print(
            f"[WARN] short_term_capacity={args.short_term_capacity} 为奇数，"
            f"agent_mem 会自动上调为 {args.short_term_capacity + 1}"
        )

    data = load_json(args.data)
    validate_data(data)

    if args.dry_run:
        print_dry_run(data)
        return

    run_dir = args.output_root / args.run_name
    if run_dir.exists():
        if args.overwrite:
            import shutil
            shutil.rmtree(run_dir)
        else:
            raise FileExistsError(
                f"结果目录已存在：{run_dir}\n"
                "请更换--run-name，或明确使用--overwrite。"
            )
    run_dir.mkdir(parents=True, exist_ok=False)
    trace_path = run_dir / "full_trace.txt"

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    with trace_path.open("w", encoding="utf-8") as trace_file:
        tee = Tee(original_stdout, trace_file)
        tee_err = Tee(original_stderr, trace_file)
        with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee_err):
            memory = None
            runner = None
            try:
                memory, memory_config = build_memory(args, run_dir)
                if not args.disable_fair_short_window_patch:
                    apply_fair_short_window_patch(
                        memory,
                        args.short_term_capacity,
                    )
                write_json(
                    run_dir / "run_config.json",
                    {
                        "run_name": args.run_name,
                        "user_id": args.user_id,
                        "data": str(args.data),
                        "evaluator": str(args.evaluator),
                        "top_k": args.top_k_long,
                        "max_total_pages": args.max_total_pages,
                        "judge": args.judge,
                        "state_timing": args.state_timing,
                        "midterm_enabled": args.midterm_enabled,
                        "history_write_mode": args.history_write_mode,
                        "session_isolation": "strict_user_id_and_run_id",
                        "fair_short_window_patch": not args.disable_fair_short_window_patch,
                        "memory_config_without_api_key": {
                            **memory_config,
                            "llm": {
                                **memory_config["llm"],
                                "config": {
                                    **memory_config["llm"]["config"],
                                    "api_key": "***REDACTED***",
                                },
                            },
                        },
                        "started_at": datetime.now().isoformat(),
                    },
                )

                if args.reset_before_run:
                    print("[INIT] 调用 memory.reset()")
                    memory.reset()

                runner = Runner(args, data, memory, run_dir)
                runner.run()
                runner.save_results()
                print_separator("测试完成")
                print(f"结果目录：{run_dir}")
                print(f"完整文本记录：{trace_path}")
            finally:
                if runner is not None:
                    runner.close()
                elif memory is not None:
                    try:
                        client = getattr(
                            getattr(memory, "vector_store", None),
                            "client",
                            None,
                        )
                        if callable(getattr(client, "close", None)):
                            client.close()
                    except Exception:
                        pass


if __name__ == "__main__":
    main()
