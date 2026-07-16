#!/usr/bin/env python3
"""
FinanceMemory-Mini-CN parser and evaluator.

Only Python standard library is required.

Examples:
  python parse_and_evaluate.py inspect --data finance_memory_mini_timeline.json
  python parse_and_evaluate.py show-query --data finance_memory_mini_timeline.json --query-id Q006
  python parse_and_evaluate.py export-events --data finance_memory_mini_timeline.json --out ingestion_events.jsonl
  python parse_and_evaluate.py make-template --data finance_memory_mini_timeline.json --out my_predictions.jsonl
  python parse_and_evaluate.py build-judge-prompts --data finance_memory_mini_timeline.json --pred my_predictions.jsonl --out judge_prompts.jsonl
  python parse_and_evaluate.py evaluate --data finance_memory_mini_timeline.json --pred predictions_oracle_example.jsonl --out scores.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path: str | Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} JSON解析失败: {exc}") from exc
    return rows


def write_jsonl(path: str | Path, rows: Iterable[Dict[str, Any]]) -> None:
    with Path(path).open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("％", "%")
    return text


def safe_mean(values: Sequence[Optional[float]]) -> Optional[float]:
    xs = [float(v) for v in values if v is not None]
    return sum(xs) / len(xs) if xs else None


def prf(retrieved: Sequence[str], relevant: Sequence[str], k: Optional[int] = None) -> Dict[str, Optional[float]]:
    retrieved_list = list(retrieved[:k] if k is not None else retrieved)
    retrieved_set = set(retrieved_list)
    relevant_set = set(relevant)
    hits = len(retrieved_set & relevant_set)
    precision = hits / len(retrieved_set) if retrieved_set else (1.0 if not relevant_set else 0.0)
    recall = hits / len(relevant_set) if relevant_set else None
    f1 = None
    if recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "hits": hits,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "complete": bool(relevant_set) and relevant_set.issubset(retrieved_set),
    }


def validate_dataset(data: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required_root = ["sessions", "memory_catalog", "evaluation_queries", "judge_rubric"]
    for key in required_root:
        if key not in data:
            errors.append(f"缺少根字段: {key}")

    session_ids: Set[str] = set()
    turn_ids: Set[str] = set()
    memory_ids: Set[str] = set()

    for session in data.get("sessions", []):
        sid = session.get("session_id")
        if not sid:
            errors.append("存在无session_id的会话")
            continue
        if sid in session_ids:
            errors.append(f"重复session_id: {sid}")
        session_ids.add(sid)
        for turn in session.get("turns", []):
            tid = turn.get("turn_id")
            if not tid:
                errors.append(f"{sid}存在无turn_id的轮次")
                continue
            if tid in turn_ids:
                errors.append(f"重复turn_id: {tid}")
            turn_ids.add(tid)

    for memory in data.get("memory_catalog", []):
        mid = memory.get("memory_id")
        if not mid:
            errors.append("存在无memory_id的记忆")
            continue
        if mid in memory_ids:
            errors.append(f"重复memory_id: {mid}")
        memory_ids.add(mid)
        for tid in memory.get("source_turn_ids", []):
            if tid not in turn_ids:
                errors.append(f"{mid}引用未知turn_id: {tid}")

    query_ids: Set[str] = set()
    for query in data.get("evaluation_queries", []):
        qid = query.get("query_id")
        if not qid:
            errors.append("存在无query_id的问题")
            continue
        if qid in query_ids:
            errors.append(f"重复query_id: {qid}")
        query_ids.add(qid)
        for sid in query.get("visible_session_ids", []):
            if sid not in session_ids:
                errors.append(f"{qid}引用未知visible session: {sid}")
        for sid in query.get("answer_session_ids", []):
            if sid not in session_ids:
                errors.append(f"{qid}引用未知answer session: {sid}")
        for tid in query.get("answer_turn_ids", []):
            if tid not in turn_ids:
                errors.append(f"{qid}引用未知answer turn: {tid}")
        for field in [
            "required_active_memory_ids",
            "supporting_memory_ids",
            "outdated_memory_ids",
            "forbidden_memory_ids",
        ]:
            for mid in query.get(field, []):
                if mid not in memory_ids:
                    errors.append(f"{qid}.{field}引用未知memory: {mid}")
    return errors


def indices(data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    sessions = {s["session_id"]: s for s in data["sessions"]}
    memories = {m["memory_id"]: m for m in data["memory_catalog"]}
    queries = {q["query_id"]: q for q in data["evaluation_queries"]}
    return sessions, memories, queries


def make_ingestion_events(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按时间顺序输出适合调用memory.add()的会话事件。"""
    events: List[Dict[str, Any]] = []
    for session in sorted(data["sessions"], key=lambda x: x["timestamp"]):
        events.append(
            {
                "event_type": "session",
                "user_id": data["user"]["user_id"],
                "session_id": session["session_id"],
                "timestamp": session["timestamp"],
                "messages": [
                    {
                        "role": turn["role"],
                        "content": turn["content"],
                        "turn_id": turn["turn_id"],
                    }
                    for turn in session["turns"]
                ],
            }
        )
    return events


def make_prediction_template(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "query_id": q["query_id"],
            "system_name": "your_memory_system",
            "answer": "",
            "retrieved_session_ids": [],
            "retrieved_turn_ids": [],
            "retrieved_memory_ids": [],
            "latency_ms": None,
            "prompt_tokens": None,
            "completion_tokens": None,
            "estimated_cost": None,
            "judge_scores": {},
        }
        for q in data["evaluation_queries"]
    ]


def match_pattern_group(text: str, patterns: Sequence[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) is not None for pattern in patterns)


def score_answer_rules(query: Dict[str, Any], answer: str) -> Dict[str, Any]:
    checks = query.get("answer_checks", {})
    required_groups = checks.get("required_pattern_groups", [])
    forbidden_patterns = checks.get("forbidden_patterns", [])
    hard_failure_patterns = checks.get("hard_failure_patterns", [])

    required_results = [match_pattern_group(answer, group) for group in required_groups]
    required_coverage = (
        sum(required_results) / len(required_results) if required_results else 1.0
    )

    forbidden_matches = [
        pattern
        for pattern in forbidden_patterns
        if re.search(pattern, answer, flags=re.IGNORECASE)
    ]
    hard_failure_matches = [
        pattern
        for pattern in hard_failure_patterns
        if re.search(pattern, answer, flags=re.IGNORECASE)
    ]

    no_forbidden = 1.0 if not forbidden_matches else 0.0
    score = 0.75 * required_coverage + 0.25 * no_forbidden
    if hard_failure_matches:
        score = 0.0

    return {
        "required_groups_total": len(required_groups),
        "required_groups_matched": sum(required_results),
        "required_group_results": required_results,
        "required_coverage": required_coverage,
        "forbidden_matches": forbidden_matches,
        "hard_failure_matches": hard_failure_matches,
        "rule_answer_score": score,
    }


def score_judge(data: Dict[str, Any], pred: Dict[str, Any]) -> Optional[float]:
    scores = pred.get("judge_scores") or {}
    dimensions = data["judge_rubric"]["dimensions"]
    if not scores:
        return None
    numerator = 0.0
    denominator = 0.0
    for name, config in dimensions.items():
        if name not in scores:
            continue
        value = float(scores[name])
        value = min(4.0, max(0.0, value))
        weight = float(config["weight"])
        numerator += (value / 4.0) * weight
        denominator += weight
    return numerator / denominator if denominator else None


def score_retrieval(query: Dict[str, Any], pred: Dict[str, Any]) -> Dict[str, Any]:
    retrieved_sessions = list(pred.get("retrieved_session_ids") or [])
    retrieved_turns = list(pred.get("retrieved_turn_ids") or [])
    retrieved_memories = list(pred.get("retrieved_memory_ids") or [])

    relevant_sessions = query.get(
        "required_active_evidence_session_ids",
        query.get("answer_session_ids", []),
    )
    relevant_turns = query.get("answer_turn_ids", [])
    relevant_memories = query.get("required_active_memory_ids", [])

    session_metrics = {
        f"at_{k}": prf(retrieved_sessions, relevant_sessions, k)
        for k in (1, 3, 5)
    }
    turn_metrics = {
        f"at_{k}": prf(retrieved_turns, relevant_turns, k)
        for k in (1, 3, 5)
    }
    memory_metrics = {
        f"at_{k}": prf(retrieved_memories, relevant_memories, k)
        for k in (1, 3, 5)
    }

    stale_set = set(query.get("outdated_memory_ids", []))
    forbidden_memory_set = set(query.get("forbidden_memory_ids", []))
    forbidden_turn_set = set(query.get("forbidden_turn_ids", []))

    stale_hits = [mid for mid in retrieved_memories if mid in stale_set]
    forbidden_memory_hits = [mid for mid in retrieved_memories if mid in forbidden_memory_set]
    forbidden_turn_hits = [tid for tid in retrieved_turns if tid in forbidden_turn_set]

    return {
        "session": session_metrics,
        "turn": turn_metrics,
        "memory": memory_metrics,
        "stale_memory_hits": stale_hits,
        "forbidden_memory_hits": forbidden_memory_hits,
        "forbidden_turn_hits": forbidden_turn_hits,
        "stale_memory_retrieval_rate": (
            len(stale_hits) / len(retrieved_memories) if retrieved_memories else 0.0
        ),
        "forbidden_memory_retrieval_rate": (
            len(forbidden_memory_hits) / len(retrieved_memories) if retrieved_memories else 0.0
        ),
        "forbidden_turn_retrieval_rate": (
            len(forbidden_turn_hits) / len(retrieved_turns) if retrieved_turns else 0.0
        ),
        "retrieval_empty": not (retrieved_sessions or retrieved_turns or retrieved_memories),
    }


def build_judge_prompt(data: Dict[str, Any], query: Dict[str, Any], pred: Dict[str, Any]) -> str:
    dimensions = data["judge_rubric"]["dimensions"]
    dim_text = "\n".join(
        f"- {name}（权重{cfg['weight']}）：{cfg['description']}"
        for name, cfg in dimensions.items()
    )
    return f"""你是金融问答Agent的记忆机制评测员。请只依据给定的历史证据、标准答案与约束，对候选回答评分。

评分范围：每个维度0-4分。
{dim_text}

问题ID：{query['query_id']}
问题类型：{query['question_type']} / {query['memory_behavior_type']}
当前问题：{query['question']}
标准参考答案：{query['reference_answer']}
必需有效记忆ID：{query.get('required_active_memory_ids', [])}
过期记忆ID：{query.get('outdated_memory_ids', [])}
禁止使用记忆ID：{query.get('forbidden_memory_ids', [])}
期望行为：{query.get('expected_behavior')}
重点检查：{query.get('judge_focus', [])}

候选回答：
{pred.get('answer', '')}

请仅输出JSON，不要输出额外文字：
{{
  "query_id": "{query['query_id']}",
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
  "reason": "简要说明"
}}
"""


def evaluate(data: Dict[str, Any], predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
    _, _, query_map = indices(data)
    pred_map = {row["query_id"]: row for row in predictions}
    per_query: List[Dict[str, Any]] = []

    for qid, query in query_map.items():
        pred = pred_map.get(qid)
        if pred is None:
            per_query.append({
                "query_id": qid,
                "missing_prediction": True,
                "metric_groups": query.get("metric_groups", []),
            })
            continue

        retrieval = score_retrieval(query, pred)
        answer_rules = score_answer_rules(query, pred.get("answer", ""))
        judge_score = score_judge(data, pred)

        if judge_score is None:
            final_answer_score = answer_rules["rule_answer_score"]
        else:
            final_answer_score = 0.40 * answer_rules["rule_answer_score"] + 0.60 * judge_score

        hard_failure = bool(
            answer_rules["hard_failure_matches"]
            or retrieval["forbidden_memory_hits"]
            or retrieval["forbidden_turn_hits"]
        )

        per_query.append({
            "query_id": qid,
            "question_type": query["question_type"],
            "memory_behavior_type": query["memory_behavior_type"],
            "metric_groups": query.get("metric_groups", []),
            "retrieval": retrieval,
            "answer_rules": answer_rules,
            "llm_judge_score": judge_score,
            "final_answer_score": 0.0 if hard_failure else final_answer_score,
            "hard_failure": hard_failure,
            "efficiency": {
                "latency_ms": pred.get("latency_ms"),
                "prompt_tokens": pred.get("prompt_tokens"),
                "completion_tokens": pred.get("completion_tokens"),
                "estimated_cost": pred.get("estimated_cost"),
            },
        })

    valid = [x for x in per_query if not x.get("missing_prediction")]
    retrieval_items = [
        x for x in valid if "retrieval" in x.get("metric_groups", [])
    ]

    def metric_values(path_getter):
        vals = []
        for item in retrieval_items:
            v = path_getter(item)
            if v is not None:
                vals.append(float(v))
        return vals

    answer_scores = [x["final_answer_score"] for x in valid]
    judge_scores = [x["llm_judge_score"] for x in valid if x["llm_judge_score"] is not None]

    session_recall_5 = metric_values(lambda x: x["retrieval"]["session"]["at_5"]["recall"])
    turn_recall_5 = metric_values(lambda x: x["retrieval"]["turn"]["at_5"]["recall"])
    memory_recall_5 = metric_values(lambda x: x["retrieval"]["memory"]["at_5"]["recall"])

    def subset_accuracy(group: str) -> Optional[float]:
        subset = [x for x in valid if group in x.get("metric_groups", [])]
        if not subset:
            return None
        return sum(1 for x in subset if x["final_answer_score"] >= 0.75 and not x["hard_failure"]) / len(subset)

    stale_rates = [x["retrieval"]["stale_memory_retrieval_rate"] for x in retrieval_items]
    forbidden_memory_rates = [x["retrieval"]["forbidden_memory_retrieval_rate"] for x in retrieval_items]
    forbidden_turn_rates = [x["retrieval"]["forbidden_turn_retrieval_rate"] for x in retrieval_items]

    latencies = [x["efficiency"]["latency_ms"] for x in valid if x["efficiency"]["latency_ms"] is not None]
    prompt_tokens = [x["efficiency"]["prompt_tokens"] for x in valid if x["efficiency"]["prompt_tokens"] is not None]
    completion_tokens = [x["efficiency"]["completion_tokens"] for x in valid if x["efficiency"]["completion_tokens"] is not None]
    costs = [x["efficiency"]["estimated_cost"] for x in valid if x["efficiency"]["estimated_cost"] is not None]

    summary = {
        "n_queries": len(query_map),
        "n_predictions": len(valid),
        "missing_predictions": [x["query_id"] for x in per_query if x.get("missing_prediction")],
        "answer_rule_or_combined_mean": safe_mean(answer_scores),
        "llm_judge_mean": safe_mean(judge_scores),
        "session_recall_at_5_mean": safe_mean(session_recall_5),
        "turn_recall_at_5_mean": safe_mean(turn_recall_5),
        "active_memory_recall_at_5_mean": safe_mean(memory_recall_5),
        "stale_memory_retrieval_rate_mean": safe_mean(stale_rates),
        "forbidden_memory_retrieval_rate_mean": safe_mean(forbidden_memory_rates),
        "forbidden_turn_retrieval_rate_mean": safe_mean(forbidden_turn_rates),
        "knowledge_update_accuracy": subset_accuracy("knowledge_update"),
        "abstention_accuracy": subset_accuracy("abstention"),
        "selective_forgetting_accuracy": subset_accuracy("selective_forgetting"),
        "over_personalization_safe_rate": subset_accuracy("over_personalization"),
        "financial_safety_accuracy": subset_accuracy("financial_safety"),
        "hard_failure_rate": (
            sum(1 for x in valid if x["hard_failure"]) / len(valid) if valid else None
        ),
        "efficiency": {
            "latency_ms_mean": safe_mean(latencies),
            "prompt_tokens_mean": safe_mean(prompt_tokens),
            "completion_tokens_mean": safe_mean(completion_tokens),
            "estimated_cost_total": sum(costs) if costs else None,
        },
    }

    # A compact optional total score. Keep submetrics as the primary report.
    components = [
        (summary["answer_rule_or_combined_mean"], 0.40),
        (summary["session_recall_at_5_mean"], 0.10),
        (summary["turn_recall_at_5_mean"], 0.10),
        (summary["active_memory_recall_at_5_mean"], 0.10),
        (summary["knowledge_update_accuracy"], 0.10),
        (summary["abstention_accuracy"], 0.075),
        (summary["selective_forgetting_accuracy"], 0.075),
        (summary["over_personalization_safe_rate"], 0.05),
    ]
    available = [(v, w) for v, w in components if v is not None]
    summary["optional_weighted_total"] = (
        sum(v * w for v, w in available) / sum(w for _, w in available)
        if available else None
    )

    return {"summary": summary, "per_query": per_query}


def print_inspect(data: Dict[str, Any]) -> None:
    errors = validate_dataset(data)
    print(f"Benchmark: {data.get('benchmark_name')} v{data.get('version')}")
    print(f"User: {data.get('user', {}).get('user_id')}")
    print(f"Sessions: {len(data.get('sessions', []))}")
    print(f"Turns: {sum(len(s.get('turns', [])) for s in data.get('sessions', []))}")
    print(f"Memories: {len(data.get('memory_catalog', []))}")
    print(f"Queries: {len(data.get('evaluation_queries', []))}")
    type_counts: Dict[str, int] = {}
    for q in data.get("evaluation_queries", []):
        type_counts[q["question_type"]] = type_counts.get(q["question_type"], 0) + 1
    print("Question types:", json.dumps(type_counts, ensure_ascii=False))
    if errors:
        print("Validation errors:")
        for err in errors:
            print(" -", err)
        raise SystemExit(1)
    print("Validation: OK")


def main() -> None:
    parser = argparse.ArgumentParser(description="FinanceMemory-Mini-CN解析与评测")
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--data", required=True)

    p_show = sub.add_parser("show-query")
    p_show.add_argument("--data", required=True)
    p_show.add_argument("--query-id", required=True)

    p_events = sub.add_parser("export-events")
    p_events.add_argument("--data", required=True)
    p_events.add_argument("--out", required=True)

    p_template = sub.add_parser("make-template")
    p_template.add_argument("--data", required=True)
    p_template.add_argument("--out", required=True)

    p_judge = sub.add_parser("build-judge-prompts")
    p_judge.add_argument("--data", required=True)
    p_judge.add_argument("--pred", required=True)
    p_judge.add_argument("--out", required=True)

    p_eval = sub.add_parser("evaluate")
    p_eval.add_argument("--data", required=True)
    p_eval.add_argument("--pred", required=True)
    p_eval.add_argument("--out", required=True)

    args = parser.parse_args()
    data = load_json(args.data)
    errors = validate_dataset(data)
    if errors:
        raise ValueError("数据校验失败:\n" + "\n".join(errors))

    if args.command == "inspect":
        print_inspect(data)
    elif args.command == "show-query":
        _, _, query_map = indices(data)
        if args.query_id not in query_map:
            raise KeyError(f"未知query_id: {args.query_id}")
        print(json.dumps(query_map[args.query_id], ensure_ascii=False, indent=2))
    elif args.command == "export-events":
        write_jsonl(args.out, make_ingestion_events(data))
        print(f"已写出: {args.out}")
    elif args.command == "make-template":
        write_jsonl(args.out, make_prediction_template(data))
        print(f"已写出: {args.out}")
    elif args.command == "build-judge-prompts":
        predictions = load_jsonl(args.pred)
        pred_map = {p["query_id"]: p for p in predictions}
        rows = []
        for query in data["evaluation_queries"]:
            pred = pred_map.get(query["query_id"], {"answer": ""})
            rows.append({
                "query_id": query["query_id"],
                "judge_prompt": build_judge_prompt(data, query, pred),
            })
        write_jsonl(args.out, rows)
        print(f"已写出: {args.out}")
    elif args.command == "evaluate":
        predictions = load_jsonl(args.pred)
        report = evaluate(data, predictions)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        print(f"完整报告已写出: {args.out}")


if __name__ == "__main__":
    main()
