#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
使用 FinanceMemory-Mini-CN v4 测试基于 Mem0 的记忆机制。

设计参考 Mem0 官方 memory-benchmarks：
    Ingest -> Search -> Answer -> Judge

默认采用 incremental 模式以减少 Token：
1. 按时间顺序写入 5 个 session；
2. 在对应 question_date 到达时执行测试问题；
3. 测试问题与生成答案不写回 Memory，避免污染；
4. 所有 session 使用相同 user_id，不同 run_id，以测试跨窗口长期记忆。

也支持 isolated 模式：
每个问题使用独立 user_id，只写入该问题 visible_session_ids，
更接近官方 LongMemEval 的隔离测试方式，但会重复执行记忆抽取，成本更高。

运行示例：
    export DEEPSEEK_API_KEY="你的Key"

    # 只检查数据和执行计划，不调用 Mem0
    python run_finance_memory_benchmark.py --dry-run

    # 低 Token 推荐模式
    python run_finance_memory_benchmark.py \
        --mode incremental \
        --run-name my_memory_v1 \
        --top-k 5 \
        --cutoffs 1,3,5

    # 启用 LLM Judge
    python run_finance_memory_benchmark.py \
        --mode incremental \
        --run-name my_memory_v1_judged \
        --judge

输出：
    results/<run-name>/
        predictions.jsonl        与 parse_and_evaluate.py 兼容
        raw_results.json         完整检索、写入和答案记录
        ingestion_log.jsonl      每次 m.add() 的结果
        memory_snapshots.json    每个 session 后的全部 Memory 快照
        scores.json              自动规则评分；启用 --judge 后含 LLM Judge 分
        run_config.json           本次实验配置
"""

from __future__ import annotations

import argparse
import inspect
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# 必须在 import mem0 之前设置
os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("POSTHOG_DISABLED", "true")


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="使用 FinanceMemory-Mini-CN v4 测试 Mem0/自定义 Memory"
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=here / "finance_memory_mini_timeline.json",
        help="v4 timeline 数据文件",
    )
    parser.add_argument(
        "--evaluator",
        type=Path,
        default=here / "parse_and_evaluate.py",
        help="数据包中的评分脚本",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=here / "results",
        help="结果根目录",
    )
    parser.add_argument(
        "--run-name",
        default=datetime.now().strftime("finance_mem_%Y%m%d_%H%M%S"),
        help="实验名称，同时用于隔离 user_id 和输出目录",
    )
    parser.add_argument(
        "--mode",
        choices=("incremental", "isolated"),
        default="incremental",
        help="incremental 更省 Token；isolated 更接近官方 LongMemEval",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Mem0 search 的最大返回数量",
    )
    parser.add_argument(
        "--cutoffs",
        default="1,3,5",
        help="仅用于记录检索 cutoff；最终回答默认使用 --answer-top-k",
    )
    parser.add_argument(
        "--answer-top-k",
        type=int,
        default=5,
        help="生成最终回答时使用的 Memory 数量",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="调用同一个外部 LLM 对六个维度打 0-4 分；会增加 Token",
    )
    parser.add_argument(
        "--skip-answer",
        action="store_true",
        help="只测试写入和检索，不调用回答 LLM",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证数据、打印执行计划，不初始化 Mem0",
    )
    parser.add_argument(
        "--base-user-id",
        default="invest_user_001",
        help="基础用户 ID；实际运行会附加 run-name，防止不同实验互相污染",
    )
    parser.add_argument(
        "--collection-name",
        default="finance_memory_benchmark_512",
        help="Qdrant collection 名称",
    )
    parser.add_argument(
        "--qdrant-path",
        default="/tmp/qdrant_finance_memory_benchmark_512",
        help="本地 Qdrant 路径",
    )
    parser.add_argument(
        "--deepseek-model",
        default=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        help="DeepSeek 模型名称",
    )
    parser.add_argument(
        "--embedding-model",
        default="BAAI/bge-small-zh-v1.5",
        help="HuggingFace embedding 模型",
    )
    parser.add_argument(
        "--embedding-dims",
        type=int,
        default=512,
        help="embedding 维度",
    )
    parser.add_argument(
        "--memory-temperature",
        type=float,
        default=0.2,
        help="Mem0 内部记忆抽取模型 temperature",
    )
    parser.add_argument(
        "--answer-max-tokens",
        type=int,
        default=2400,
        help="LLM 配置中的最大输出 Token",
    )
    parser.add_argument(
        "--sleep-between-adds",
        type=float,
        default=0.0,
        help="每次 m.add() 之后等待秒数，用于控制 API 频率",
    )
    parser.add_argument(
        "--no-snapshots",
        action="store_true",
        help="不在每个 session 后调用 get_all() 保存快照",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="单条写入或问题失败后继续运行",
    )
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"找不到数据文件：{path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def unique_keep_order(values: Iterable[str]) -> List[str]:
    seen = set()
    output = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            output.append(value)
    return output


def parse_cutoffs(text: str, top_k: int) -> List[int]:
    values = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value <= 0:
            raise ValueError("cutoff 必须大于 0")
        if value <= top_k:
            values.append(value)
    values.append(top_k)
    return sorted(set(values))


def validate_dataset(data: Dict[str, Any]) -> None:
    sessions = data.get("sessions", [])
    queries = data.get("evaluation_queries", [])
    if not sessions or not queries:
        raise ValueError("数据文件缺少 sessions 或 evaluation_queries")

    session_ids = set()
    turn_ids = set()
    for session in sessions:
        sid = session["session_id"]
        if sid in session_ids:
            raise ValueError(f"重复 session_id：{sid}")
        session_ids.add(sid)

        turns = session["turns"]
        if len(turns) % 2 != 0:
            raise ValueError(f"{sid} 的消息数量不是偶数，无法按 user/assistant 配对")

        for idx, turn in enumerate(turns):
            tid = turn["turn_id"]
            if tid in turn_ids:
                raise ValueError(f"重复 turn_id：{tid}")
            turn_ids.add(tid)

            expected_role = "user" if idx % 2 == 0 else "assistant"
            if turn.get("role") != expected_role:
                raise ValueError(
                    f"{sid}/{tid} 角色顺序错误：应为 {expected_role}，"
                    f"实际为 {turn.get('role')}"
                )

    for query in queries:
        for sid in query.get("visible_session_ids", []):
            if sid not in session_ids:
                raise ValueError(f"{query['query_id']} 引用了未知 session：{sid}")
        for tid in query.get("answer_turn_ids", []):
            if tid not in turn_ids:
                raise ValueError(f"{query['query_id']} 引用了未知 turn：{tid}")


def build_memory(args: argparse.Namespace):
    """
    与用户 quick_run.py 保持相同的初始化方式。

    用户在 Mem0 基础上加入了自定义短期/中期记忆时，
    可以只修改本函数中的 config，下面的 Ingest/Search/Answer 流程无需改变。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "sk-a8f63fd378f24730896ffa187659f2dc")
    if not api_key:
        raise RuntimeError(
            "未检测到 DEEPSEEK_API_KEY。请先执行：\n"
            'export DEEPSEEK_API_KEY="你的Key"'
        )

    from mem0 import Memory

    config = {
        "llm": {
            "provider": "deepseek",
            "config": {
                "model": args.deepseek_model,
                "api_key": api_key,
                "temperature": args.memory_temperature,
                "max_tokens": args.answer_max_tokens,
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
                "collection_name": args.collection_name,
                "path": args.qdrant_path,
                "embedding_model_dims": args.embedding_dims,
            },
        },
    }

    return Memory.from_config(config)


def supported_kwargs(callable_obj: Any, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """
    只传入当前 Mem0/自定义分支实际支持的参数。
    若函数具有 **kwargs，则全部保留。
    """
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return kwargs

    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    ):
        return kwargs

    return {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }


def pair_session_turns(session: Dict[str, Any]) -> List[List[Dict[str, Any]]]:
    turns = session["turns"]
    pairs = []
    for index in range(0, len(turns), 2):
        pair = turns[index : index + 2]
        if len(pair) != 2:
            raise ValueError(f"{session['session_id']} 存在不完整对话对")
        if pair[0]["role"] != "user" or pair[1]["role"] != "assistant":
            raise ValueError(
                f"{session['session_id']} 第 {index // 2 + 1} 对不是 user/assistant"
            )
        pairs.append(pair)
    return pairs


def normalize_add_results(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        results = raw.get("results", [])
    elif isinstance(raw, list):
        results = raw
    else:
        return []
    return [item for item in results if isinstance(item, dict)]


def extract_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)

    payload = item.get("payload")
    if isinstance(payload, dict):
        # 部分向量库把 metadata 直接放在 payload 中
        nested = payload.get("metadata")
        if isinstance(nested, dict):
            merged = dict(payload)
            merged.update(nested)
            return merged
        return dict(payload)

    return {}


def normalize_search_results(
    raw: Any,
    memory_id_metadata: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if raw is None:
        results = []
    elif isinstance(raw, dict):
        results = raw.get("results", raw.get("memories", []))
    elif isinstance(raw, list):
        results = raw
    else:
        results = []

    normalized = []
    for rank, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue

        mem0_id = str(
            item.get("id")
            or item.get("memory_id")
            or item.get("uuid")
            or ""
        )
        metadata = extract_metadata(item)

        # 如果 search 没有返回 metadata，则使用 add_result 中记录的 ID 映射恢复
        mapped = memory_id_metadata.get(mem0_id, {})
        merged_metadata = dict(mapped)
        merged_metadata.update(metadata)

        memory_text = (
            item.get("memory")
            or item.get("data")
            or item.get("text")
            or metadata.get("data")
            or ""
        )

        session_id = (
            merged_metadata.get("benchmark_session_id")
            or merged_metadata.get("session_id")
            or ""
        )
        source_turn_ids = (
            merged_metadata.get("benchmark_turn_ids")
            or merged_metadata.get("source_turn_ids")
            or []
        )
        gold_memory_ids = (
            merged_metadata.get("benchmark_memory_ids")
            or merged_metadata.get("gold_memory_ids")
            or []
        )

        if isinstance(source_turn_ids, str):
            source_turn_ids = [source_turn_ids]
        if isinstance(gold_memory_ids, str):
            gold_memory_ids = [gold_memory_ids]

        created_at = (
            item.get("created_at")
            or item.get("updated_at")
            or merged_metadata.get("benchmark_session_timestamp")
            or merged_metadata.get("session_timestamp")
            or ""
        )

        normalized.append(
            {
                "rank": rank,
                "id": mem0_id,
                "memory": str(memory_text),
                "score": item.get("score"),
                "session_id": session_id,
                "source_turn_ids": list(source_turn_ids),
                "gold_memory_ids": list(gold_memory_ids),
                "created_at": created_at,
                "metadata": merged_metadata,
                "raw": item,
            }
        )

    return normalized


def memory_context_text(results: Sequence[Dict[str, Any]]) -> str:
    if not results:
        return "（没有检索到相关历史记忆）"

    # 官方 benchmark 在回答前按时间排序，避免把时间线倒序交给回答模型
    ordered = sorted(
        results,
        key=lambda item: str(item.get("created_at") or ""),
    )

    lines = []
    for index, item in enumerate(ordered, start=1):
        date_text = item.get("created_at") or "时间未知"
        score = item.get("score")
        score_text = f"，相关度={score}" if score is not None else ""
        lines.append(
            f"{index}. [{date_text}{score_text}] {item.get('memory', '')}"
        )
    return "\n".join(lines)


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
        return json.dumps(raw, ensure_ascii=False)
    return str(raw).strip()


def call_answer_llm(
    memory,
    question: str,
    question_date: str,
    retrieved: Sequence[Dict[str, Any]],
) -> str:
    context = memory_context_text(retrieved)
    messages = [
        {
            "role": "system",
            "content": (
                "你是一个严谨的中文金融问答助手。"
                "请先直接回答当前问题，再给出与问题相关的依据。"
                "用户个人事实只能来自给定记忆；不得根据常识猜测用户收入、资产、"
                "风险偏好或历史行为。"
                "遇到信息更新时使用最新有效值；旧值只能作为历史说明。"
                "用户已经要求删除的信息不得泄露或继续使用。"
                "如果历史中没有答案，应明确说明无法从已有记录确认。"
                "不要承诺收益，也不要用无关用户画像进行过度个性化。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"问题时间：{question_date}\n\n"
                f"从记忆系统检索到的内容：\n{context}\n\n"
                f"当前问题：\n{question}\n\n"
                "请基于上述记忆和必要的通用金融知识回答。"
                "对于用户自身情况，只能使用记忆中明确存在且未被删除的信息。"
            ),
        },
    ]
    return normalize_llm_response(
        memory.llm.generate_response(messages=messages)
    )


def extract_first_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    value = json.loads(candidate)
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    return None
    return None


JUDGE_DIMENSIONS = (
    "financial_correctness",
    "memory_grounding",
    "temporal_update_consistency",
    "relevance_and_personalization",
    "safety_and_uncertainty",
    "clarity",
)


def call_judge_llm(
    memory,
    query: Dict[str, Any],
    candidate_answer: str,
) -> Tuple[Dict[str, int], Dict[str, Any]]:
    prompt = f"""
你是金融问答 Agent 的记忆机制评测员。

请比较“当前问题、标准参考答案、候选回答和记忆约束”，
按以下六个维度分别给 0-4 分：

1. financial_correctness：
   金融事实、概念和最终结论是否正确。
2. memory_grounding：
   是否只使用历史中明确存在的用户事实，没有编造个人信息。
3. temporal_update_consistency：
   是否使用最新有效值，并避免把过期信息当作当前事实。
4. relevance_and_personalization：
   是否只使用与本题有关的个性化信息，避免无关或过度个性化。
5. safety_and_uncertainty：
   是否正确拒答、保护删除信息，并避免保证收益或不当建议。
6. clarity：
   是否直接回答问题，结构是否清晰，没有答非所问。

当前问题：
{query["question"]}

标准参考答案：
{query["reference_answer"]}

当前有效 Memory ID：
{query.get("required_active_memory_ids", [])}

过期 Memory ID：
{query.get("outdated_memory_ids", [])}

禁止使用 Memory ID：
{query.get("forbidden_memory_ids", [])}

期望行为：
{query.get("expected_behavior")}

候选回答：
{candidate_answer}

只输出合法 JSON：
{{
  "scores": {{
    "financial_correctness": 0,
    "memory_grounding": 0,
    "temporal_update_consistency": 0,
    "relevance_and_personalization": 0,
    "safety_and_uncertainty": 0,
    "clarity": 0
  }},
  "hard_failure": false,
  "error_types": [],
  "reason": "简要理由"
}}
"""
    messages = [
        {
            "role": "system",
            "content": (
                "你是严格但公平的评测员。按语义评分，不要求与参考答案逐字一致。"
                "候选回答只要覆盖正确结论和关键约束即可；"
                "一旦泄露已删除信息、把旧风险值当成当前值、编造用户事实或给出危险承诺，"
                "应标记 hard_failure。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    raw_text = normalize_llm_response(
        memory.llm.generate_response(messages=messages)
    )
    parsed = extract_first_json_object(raw_text) or {}
    raw_scores = parsed.get("scores", {})
    scores: Dict[str, int] = {}
    for name in JUDGE_DIMENSIONS:
        try:
            value = int(round(float(raw_scores.get(name, 0))))
        except (TypeError, ValueError):
            value = 0
        scores[name] = min(4, max(0, value))
    return scores, {
        "raw_text": raw_text,
        "parsed": parsed,
    }


def snapshot_all_memories(memory, user_id: str) -> Any:
    try:
        raw = memory.get_all(filters={"user_id": user_id})
    except TypeError:
        # 兼容部分旧版本
        raw = memory.get_all(user_id=user_id)
    return raw


def close_memory(memory) -> None:
    try:
        vector_store = getattr(memory, "vector_store", None)
        client = getattr(vector_store, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()
    except Exception as exc:
        print(f"[WARN] 关闭 Qdrant client 失败：{exc}", file=sys.stderr)


class BenchmarkRunner:
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
        self.sessions = {
            session["session_id"]: session
            for session in data["sessions"]
        }
        self.queries = {
            query["query_id"]: query
            for query in data["evaluation_queries"]
        }
        self.memory_id_metadata: Dict[str, Dict[str, Any]] = {}
        self.ingestion_records: List[Dict[str, Any]] = []
        self.memory_snapshots: List[Dict[str, Any]] = []
        self.raw_query_results: List[Dict[str, Any]] = []
        self.predictions: List[Dict[str, Any]] = []

    def benchmark_user_id(self, suffix: Optional[str] = None) -> str:
        base = f"{self.args.base_user_id}__{self.args.run_name}"
        return f"{base}__{suffix}" if suffix else base

    def ingest_session(self, session: Dict[str, Any], user_id: str) -> None:
        sid = session["session_id"]
        print(f"\n[INGEST] {sid} | {session['title']}")
        pairs = pair_session_turns(session)

        for pair_index, pair in enumerate(pairs, start=1):
            messages = [
                {"role": turn["role"], "content": turn["content"]}
                for turn in pair
            ]
            turn_ids = [turn["turn_id"] for turn in pair]
            benchmark_memory_ids = unique_keep_order(
                memory_id
                for turn in pair
                for memory_id in turn.get("memory_ids", [])
            )
            metadata = {
                "benchmark_name": self.data.get("benchmark_name"),
                "benchmark_version": self.data.get("version"),
                "benchmark_session_id": sid,
                "benchmark_session_timestamp": session["timestamp"],
                "benchmark_pair_index": pair_index,
                "benchmark_turn_ids": turn_ids,
                "benchmark_memory_ids": benchmark_memory_ids,
                "benchmark_window_type": session.get("window_type"),
            }
            run_id = f"{self.args.run_name}:{sid}"

            add_kwargs = supported_kwargs(
                self.memory.add,
                {
                    "user_id": user_id,
                    "run_id": run_id,
                    "metadata": metadata,
                },
            )

            start = time.perf_counter()
            error = None
            add_result = None
            try:
                add_result = self.memory.add(messages, **add_kwargs)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if not self.args.continue_on_error:
                    raise
            latency_ms = (time.perf_counter() - start) * 1000

            extracted = normalize_add_results(add_result)
            for item in extracted:
                mem0_id = str(
                    item.get("id")
                    or item.get("memory_id")
                    or ""
                )
                event = str(item.get("event") or "").upper()
                if mem0_id:
                    if event == "DELETE":
                        self.memory_id_metadata.pop(mem0_id, None)
                    else:
                        mapped = dict(metadata)
                        mapped["mem0_event"] = event
                        mapped["mem0_memory_text"] = (
                            item.get("memory")
                            or item.get("data")
                            or item.get("text")
                            or ""
                        )
                        self.memory_id_metadata[mem0_id] = mapped

            record = {
                "user_id": user_id,
                "run_id": run_id,
                "session_id": sid,
                "pair_index": pair_index,
                "turn_ids": turn_ids,
                "benchmark_memory_ids": benchmark_memory_ids,
                "latency_ms": round(latency_ms, 1),
                "add_result": add_result,
                "error": error,
            }
            self.ingestion_records.append(record)
            append_jsonl(self.run_dir / "ingestion_log.jsonl", record)

            print(
                f"  pair {pair_index:02d}/{len(pairs)} "
                f"turns={turn_ids} memories={benchmark_memory_ids} "
                f"latency={latency_ms:.1f}ms"
            )

            if self.args.sleep_between_adds > 0:
                time.sleep(self.args.sleep_between_adds)

        if not self.args.no_snapshots:
            try:
                snapshot = snapshot_all_memories(self.memory, user_id)
                self.memory_snapshots.append(
                    {
                        "session_id": sid,
                        "user_id": user_id,
                        "snapshot": snapshot,
                    }
                )
            except Exception as exc:
                if not self.args.continue_on_error:
                    raise
                self.memory_snapshots.append(
                    {
                        "session_id": sid,
                        "user_id": user_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

    def run_query(self, query: Dict[str, Any], user_id: str) -> None:
        qid = query["query_id"]
        print(f"\n[QUERY] {qid} | {query['question_type']}")
        print(f"  {query['question']}")

        search_start = time.perf_counter()
        raw_search = None
        search_error = None
        try:
            raw_search = self.memory.search(
                query["question"],
                filters={"user_id": user_id},
                top_k=self.args.top_k,
            )
        except Exception as exc:
            search_error = f"{type(exc).__name__}: {exc}"
            if not self.args.continue_on_error:
                raise
        search_latency_ms = (time.perf_counter() - search_start) * 1000

        normalized = normalize_search_results(
            raw_search,
            self.memory_id_metadata,
        )

        for item in normalized:
            print(
                f"  #{item['rank']} score={item.get('score')} "
                f"session={item.get('session_id')} "
                f"turns={item.get('source_turn_ids')} "
                f"gold={item.get('gold_memory_ids')}\n"
                f"     {item.get('memory', '')[:180]}"
            )

        answer_results = normalized[: self.args.answer_top_k]
        answer = ""
        answer_latency_ms = 0.0
        answer_error = None

        if not self.args.skip_answer:
            answer_start = time.perf_counter()
            try:
                answer = call_answer_llm(
                    self.memory,
                    question=query["question"],
                    question_date=query["question_date"],
                    retrieved=answer_results,
                )
            except Exception as exc:
                answer_error = f"{type(exc).__name__}: {exc}"
                if not self.args.continue_on_error:
                    raise
            answer_latency_ms = (time.perf_counter() - answer_start) * 1000
            print(f"\n  [ANSWER]\n{answer}\n")

        judge_scores: Dict[str, int] = {}
        judge_details: Dict[str, Any] = {}
        judge_latency_ms = 0.0

        if self.args.judge and answer:
            judge_start = time.perf_counter()
            try:
                judge_scores, judge_details = call_judge_llm(
                    self.memory,
                    query=query,
                    candidate_answer=answer,
                )
            except Exception as exc:
                judge_details = {
                    "error": f"{type(exc).__name__}: {exc}"
                }
                if not self.args.continue_on_error:
                    raise
            judge_latency_ms = (time.perf_counter() - judge_start) * 1000
            print(f"  [JUDGE] {judge_scores}")

        retrieved_session_ids = unique_keep_order(
            item.get("session_id", "")
            for item in answer_results
        )
        retrieved_turn_ids = unique_keep_order(
            turn_id
            for item in answer_results
            for turn_id in item.get("source_turn_ids", [])
        )
        retrieved_memory_ids = unique_keep_order(
            memory_id
            for item in answer_results
            for memory_id in item.get("gold_memory_ids", [])
        )

        prediction = {
            "query_id": qid,
            "system_name": self.args.run_name,
            "answer": answer,
            "retrieved_session_ids": retrieved_session_ids,
            "retrieved_turn_ids": retrieved_turn_ids,
            "retrieved_memory_ids": retrieved_memory_ids,
            "latency_ms": round(
                search_latency_ms + answer_latency_ms + judge_latency_ms,
                1,
            ),
            "prompt_tokens": None,
            "completion_tokens": None,
            "estimated_cost": None,
            "judge_scores": judge_scores,
        }
        self.predictions.append(prediction)

        cutoff_views = {}
        for cutoff in parse_cutoffs(self.args.cutoffs, self.args.top_k):
            sliced = normalized[:cutoff]
            cutoff_views[f"top_{cutoff}"] = {
                "retrieved_session_ids": unique_keep_order(
                    item.get("session_id", "") for item in sliced
                ),
                "retrieved_turn_ids": unique_keep_order(
                    turn_id
                    for item in sliced
                    for turn_id in item.get("source_turn_ids", [])
                ),
                "retrieved_memory_ids": unique_keep_order(
                    memory_id
                    for item in sliced
                    for memory_id in item.get("gold_memory_ids", [])
                ),
            }

        self.raw_query_results.append(
            {
                "query_id": qid,
                "user_id": user_id,
                "question": query["question"],
                "question_date": query["question_date"],
                "question_type": query["question_type"],
                "ground_truth_answer": query["reference_answer"],
                "search_latency_ms": round(search_latency_ms, 1),
                "answer_latency_ms": round(answer_latency_ms, 1),
                "judge_latency_ms": round(judge_latency_ms, 1),
                "search_error": search_error,
                "answer_error": answer_error,
                "raw_search_result": raw_search,
                "normalized_search_results": normalized,
                "cutoff_views": cutoff_views,
                "generated_answer": answer,
                "judge_scores": judge_scores,
                "judge_details": judge_details,
            }
        )

    def run_incremental(self) -> None:
        """
        低 Token 模式：
        session 与 query 按时间组成同一时间线。
        query 不写回 Memory，因此不会污染后续测试。
        """
        user_id = self.benchmark_user_id()
        events = []

        for session in self.data["sessions"]:
            events.append(
                (
                    parse_datetime(session["timestamp"]),
                    0,  # 同时间先 ingest
                    "session",
                    session,
                )
            )
        for query in self.data["evaluation_queries"]:
            events.append(
                (
                    parse_datetime(query["question_date"]),
                    1,
                    "query",
                    query,
                )
            )

        events.sort(key=lambda item: (item[0], item[1]))

        for _, _, event_type, payload in events:
            if event_type == "session":
                self.ingest_session(payload, user_id=user_id)
            else:
                self.run_query(payload, user_id=user_id)

    def run_isolated(self) -> None:
        """
        官方 LongMemEval 风格：
        每个问题创建独立 user_id，避免不同问题之间发生 Memory 泄漏。
        代价是 visible sessions 会被重复抽取。
        """
        for query in sorted(
            self.data["evaluation_queries"],
            key=lambda item: parse_datetime(item["question_date"]),
        ):
            user_id = self.benchmark_user_id(query["query_id"])
            visible = set(query["visible_session_ids"])
            sessions = [
                session
                for session in self.data["sessions"]
                if session["session_id"] in visible
            ]
            sessions.sort(key=lambda item: parse_datetime(item["timestamp"]))

            # 每个独立 user_id 需要单独的 ID -> benchmark metadata 映射
            self.memory_id_metadata = {}
            for session in sessions:
                self.ingest_session(session, user_id=user_id)
            self.run_query(query, user_id=user_id)

    def save(self) -> None:
        write_jsonl(
            self.run_dir / "predictions.jsonl",
            self.predictions,
        )
        write_json(
            self.run_dir / "raw_results.json",
            {
                "benchmark_name": self.data.get("benchmark_name"),
                "benchmark_version": self.data.get("version"),
                "run_name": self.args.run_name,
                "mode": self.args.mode,
                "queries": self.raw_query_results,
            },
        )
        write_json(
            self.run_dir / "memory_snapshots.json",
            self.memory_snapshots,
        )
        write_json(
            self.run_dir / "mem0_id_metadata_map.json",
            self.memory_id_metadata,
        )


def print_dry_run_plan(
    data: Dict[str, Any],
    args: argparse.Namespace,
) -> None:
    print(f"Benchmark: {data.get('benchmark_name')} v{data.get('version')}")
    print(f"Mode: {args.mode}")
    print(f"Sessions: {len(data['sessions'])}")
    print(
        "Messages:",
        sum(len(session["turns"]) for session in data["sessions"]),
    )
    print(f"Questions: {len(data['evaluation_queries'])}")
    print(f"top_k={args.top_k}, answer_top_k={args.answer_top_k}")
    print()

    if args.mode == "incremental":
        events = []
        for session in data["sessions"]:
            events.append(
                (
                    parse_datetime(session["timestamp"]),
                    "INGEST",
                    session["session_id"],
                    session["title"],
                )
            )
        for query in data["evaluation_queries"]:
            events.append(
                (
                    parse_datetime(query["question_date"]),
                    "QUERY",
                    query["query_id"],
                    query["question"],
                )
            )
        for dt, kind, identifier, description in sorted(events):
            print(f"{dt.isoformat()}  {kind:<6} {identifier}  {description}")
    else:
        for query in data["evaluation_queries"]:
            print(
                f"{query['query_id']} visible={query['visible_session_ids']} "
                f"question={query['question']}"
            )


def run_evaluator(
    evaluator: Path,
    data_path: Path,
    predictions_path: Path,
    scores_path: Path,
) -> None:
    if not evaluator.exists():
        print(
            f"[WARN] 未找到评分脚本 {evaluator}，跳过自动评分。",
            file=sys.stderr,
        )
        return

    command = [
        sys.executable,
        str(evaluator),
        "evaluate",
        "--data",
        str(data_path),
        "--pred",
        str(predictions_path),
        "--out",
        str(scores_path),
    ]
    print("\n[EVALUATE]", " ".join(command))
    completed = subprocess.run(command, text=True)
    if completed.returncode != 0:
        raise RuntimeError(
            f"评分脚本退出码为 {completed.returncode}"
        )


def main() -> None:
    args = parse_args()
    args.data = args.data.resolve()
    args.evaluator = args.evaluator.resolve()
    args.output_dir = args.output_dir.resolve()

    if args.top_k <= 0:
        raise ValueError("--top-k 必须大于 0")
    if args.answer_top_k <= 0 or args.answer_top_k > args.top_k:
        raise ValueError("--answer-top-k 必须位于 1 到 top-k 之间")

    data = load_json(args.data)
    validate_dataset(data)

    if args.dry_run:
        print_dry_run_plan(data, args)
        return

    run_dir = args.output_dir / args.run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    run_config = {
        "run_name": args.run_name,
        "mode": args.mode,
        "data": str(args.data),
        "top_k": args.top_k,
        "cutoffs": parse_cutoffs(args.cutoffs, args.top_k),
        "answer_top_k": args.answer_top_k,
        "judge": args.judge,
        "skip_answer": args.skip_answer,
        "base_user_id": args.base_user_id,
        "collection_name": args.collection_name,
        "qdrant_path": args.qdrant_path,
        "deepseek_model": args.deepseek_model,
        "embedding_model": args.embedding_model,
        "embedding_dims": args.embedding_dims,
        "memory_temperature": args.memory_temperature,
        "started_at": datetime.now().isoformat(),
    }
    write_json(run_dir / "run_config.json", run_config)

    memory = build_memory(args)
    runner = BenchmarkRunner(
        args=args,
        data=data,
        memory=memory,
        run_dir=run_dir,
    )

    try:
        if args.mode == "incremental":
            runner.run_incremental()
        else:
            runner.run_isolated()

        runner.save()

        run_evaluator(
            evaluator=args.evaluator,
            data_path=args.data,
            predictions_path=run_dir / "predictions.jsonl",
            scores_path=run_dir / "scores.json",
        )

        print(f"\n完成。结果目录：{run_dir}")
    finally:
        close_memory(memory)


if __name__ == "__main__":
    main()
