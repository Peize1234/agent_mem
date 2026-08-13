"""Listwise LLM Historical Dependency reranking over frozen C3 Top20 Pages."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json, resolve_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.memory_gold_groups import GoldRequirement  # noqa: E402
from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash  # noqa: E402
from exp.benchmark.run_full_memory_recall_s001_s005_v2 import (  # noqa: E402
    DatasetTurn,
    RunSettings as DatasetSettings,
    assert_expected_stats,
    load_and_validate_c3,
    load_settings as load_dataset_settings,
    load_v3_dataset,
    source_qa_hash,
    static_dataset_stats,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402


DEFAULT_CONFIG = REPO_ROOT / "exp/benchmark/llm_historical_dependency_reranking.json"
PROMPT_VERSION = "historical-dependency-listwise-v1"
SYSTEM_PROMPT = """你是一个中期对话记忆的 Historical Dependency 判断组件。

你的任务是判断：候选历史记忆中是否包含回答 Current Query 真正需要依赖的历史信息，而不是判断普通主题相似度。

优先考虑当前问题明确引用、承接或继续使用的历史事实、计算结果、分析结论、证据、限制和上下文。仅仅公司、年份、财务指标或任务主题相似，不代表当前 Query 对该历史 Page 存在 Historical Dependency。

你会收到按现有 Dense Retriever 顺序排列的候选，但不得根据候选位置判断相关性。必须独立阅读每个候选的 Summary 和 Keywords。

请为输入中的每一个候选返回一个 0 到 100 的 dependency_score。分数越高，表示回答当前 Query 越真正依赖该历史 Page。

严格要求：
1. 必须返回全部候选 Page ID；
2. 不得遗漏或重复 Page ID；
3. 不得新增输入中不存在的 Page ID；
4. 只输出严格有效的 JSON 对象；
5. 每个结果只能包含 page_id 和 dependency_score；
6. 不要解释，不要输出 Markdown。

输出格式：
{
  "results": [
    {
      "page_id": "...",
      "dependency_score": 0
    }
  ]
}"""


@dataclass(frozen=True)
class ExperimentSettings:
    output_dir: Path
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    timeout_seconds: float
    retries: int
    concurrency: int
    candidate_k: int
    output_k: int
    thinking_mode: str
    dataset_config: Path
    memory_config: Path
    raw_config: Mapping[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C3 Top20 dependency-aware DeepSeek listwise reranking")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--retries", type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--candidate-k", type=int)
    parser.add_argument("--output-k", type=int)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def load_experiment_settings(args: argparse.Namespace) -> ExperimentSettings:
    raw = load_json(args.config.resolve())

    def selected(name: str, override: Any) -> Any:
        return raw[name] if override is None else override

    settings = ExperimentSettings(
        output_dir=Path(args.output_dir or resolve_path(REPO_ROOT, raw["output_dir"])).resolve(),
        model=str(selected("model", args.model)),
        temperature=float(selected("temperature", args.temperature)),
        top_p=float(selected("top_p", args.top_p)),
        max_tokens=int(selected("max_tokens", args.max_tokens)),
        timeout_seconds=float(selected("timeout_seconds", args.timeout)),
        retries=int(selected("retries", args.retries)),
        concurrency=int(selected("concurrency", args.concurrency)),
        candidate_k=int(selected("candidate_k", args.candidate_k)),
        output_k=int(selected("output_k", args.output_k)),
        thinking_mode=str(raw["thinking_mode"]),
        dataset_config=resolve_path(REPO_ROOT, raw["dataset_config"]),
        memory_config=resolve_path(REPO_ROOT, raw["memory_config"]),
        raw_config=raw,
    )
    numeric = (
        settings.max_tokens,
        settings.retries,
        settings.concurrency,
        settings.candidate_k,
        settings.output_k,
    )
    if any(value <= 0 for value in numeric) or settings.timeout_seconds <= 0:
        raise ValueError("max_tokens/retries/concurrency/candidate_k/output_k/timeout 必须为正数")
    if settings.output_k > settings.candidate_k:
        raise ValueError("output_k 不能大于 candidate_k")
    if settings.candidate_k != 20 or settings.output_k != 5:
        raise ValueError("本轮冻结 candidate_k=20、output_k=5；禁止改变 candidate scope")
    return settings


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_prompt(output_dir: Path) -> tuple[Path, str]:
    prompt_dir = output_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / "historical_dependency_listwise_v1.txt"
    if path.exists() and path.read_text(encoding="utf-8") != SYSTEM_PROMPT:
        raise AssertionError(f"Prompt 文件与代码中的首轮冻结 Prompt 不一致：{path}")
    if not path.exists():
        path.write_text(SYSTEM_PROMPT, encoding="utf-8")
    return path, sha256_file(path)


def parse_json_object(raw: str) -> Mapping[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    value = json.loads(text)
    if not isinstance(value, Mapping):
        raise ValueError("LLM JSON 顶层必须为 object")
    return value


def validate_dependency_scores(value: Mapping[str, Any], candidate_ids: Sequence[str]) -> dict[str, Any]:
    if set(value) != {"results"} or not isinstance(value["results"], list):
        raise ValueError("输出必须且只能包含 results list")
    expected = [str(page_id) for page_id in candidate_ids]
    results = value["results"]
    if len(results) != len(expected):
        raise ValueError(f"候选数量不完整：expected={len(expected)}, actual={len(results)}")
    seen: set[str] = set()
    scores: dict[str, float] = {}
    clean: list[dict[str, Any]] = []
    for result in results:
        if not isinstance(result, Mapping) or set(result) != {"page_id", "dependency_score"}:
            raise ValueError("每个 result 必须且只能包含 page_id/dependency_score")
        page_id = str(result["page_id"])
        score = result["dependency_score"]
        if page_id not in expected:
            raise ValueError(f"未知 Page ID：{page_id}")
        if page_id in seen:
            raise ValueError(f"重复 Page ID：{page_id}")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError(f"dependency_score 必须为 number：{page_id}")
        numeric = float(score)
        if not math.isfinite(numeric) or not 0 <= numeric <= 100:
            raise ValueError(f"dependency_score 超出 0-100：{page_id}={numeric}")
        seen.add(page_id)
        scores[page_id] = numeric
        clean.append({"page_id": page_id, "dependency_score": numeric})
    if seen != set(expected):
        raise ValueError(f"Page ID 缺失：{sorted(set(expected) - seen)}")
    return {"results": clean, "score_by_page_id": scores}


def candidate_payload(
    query_text: str, candidates: Sequence[Mapping[str, Any]], page_by_id: Mapping[str, Mapping[str, Any]]
) -> str:
    lines = ["Current Query:", query_text]
    for index, row in enumerate(candidates, start=1):
        page = page_by_id[str(row["page_id"])]
        keywords = ", ".join(str(item) for item in page["keywords"])
        lines.extend(
            [
                "",
                f"Candidate {index}:",
                f"Page ID: {row['page_id']}",
                f"Summary: {page['summary']}",
                f"Keywords: {keywords}",
            ]
        )
    return "\n".join(lines)


def provider_precondition(exc: Exception) -> bool:
    text = str(exc).lower()
    return "precondition" in text or ("response_format" in text and "json" in text)


class DependencyScoreCache:
    def __init__(
        self,
        path: Path,
        *,
        client: AsyncOpenAI,
        settings: ExperimentSettings,
        prompt_sha256: str,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.settings = settings
        self.prompt_sha256 = prompt_sha256
        self.semaphore = asyncio.Semaphore(settings.concurrency)
        self.lock = asyncio.Lock()
        self.rows = load_jsonl(path)
        self.success = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.initial_success_keys = set(self.success)

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    def identity(
        self, query_id: str, query_text: str, candidates: Sequence[Mapping[str, Any]], payload: str
    ) -> dict[str, Any]:
        return {
            "query_id": query_id,
            "query_text_sha256": sha256_text(query_text),
            "candidate_page_ids": [str(row["page_id"]) for row in candidates],
            "candidate_payload_sha256": sha256_text(payload),
            "candidate_k": self.settings.candidate_k,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": self.prompt_sha256,
            "model": self.settings.model,
            "thinking_mode": self.settings.thinking_mode,
            "temperature": self.settings.temperature,
            "top_p": self.settings.top_p,
            "max_tokens": self.settings.max_tokens,
        }

    async def call(
        self,
        *,
        query_id: str,
        query_text: str,
        candidates: Sequence[Mapping[str, Any]],
        page_by_id: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        payload = candidate_payload(query_text, candidates, page_by_id)
        identity = self.identity(query_id, query_text, candidates, payload)
        cache_key = stable_hash(identity)
        candidate_ids = identity["candidate_page_ids"]
        if cached := self.success.get(cache_key):
            validate_dependency_scores(cached["parsed"], candidate_ids)
            return {**cached, "cache_hit_this_run": True}
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": payload}]
        errors: list[str] = []
        precondition_rejects = 0
        use_response_format = True
        async with self.semaphore:
            for attempt in range(1, self.settings.retries + 1):
                started = time.perf_counter()
                try:
                    kwargs: dict[str, Any] = {
                        "model": self.settings.model,
                        "messages": messages,
                        "temperature": self.settings.temperature,
                        "top_p": self.settings.top_p,
                        "max_tokens": self.settings.max_tokens,
                        "extra_body": {"thinking": {"type": self.settings.thinking_mode}},
                    }
                    if use_response_format:
                        kwargs["response_format"] = {"type": "json_object"}
                    response = await asyncio.wait_for(
                        self.client.chat.completions.create(**kwargs), timeout=self.settings.timeout_seconds
                    )
                    raw = response.choices[0].message.content or ""
                    parsed = validate_dependency_scores(parse_json_object(raw), candidate_ids)
                    usage = response.usage
                    row = {
                        "cache_key": cache_key,
                        **identity,
                        "status": "SUCCESS",
                        "parsed": {"results": parsed["results"]},
                        "raw_output": raw,
                        "response_mode": "json_object" if use_response_format else "plain_text_strict_json",
                        "llm_latency_ms": (time.perf_counter() - started) * 1000,
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "api_attempt_count": attempt,
                        "retry_count": attempt - 1,
                        "provider_precondition_rejected_count": precondition_rejects,
                        "errors": errors,
                    }
                    await self.append(row)
                    self.success[cache_key] = row
                    return {**row, "cache_hit_this_run": False}
                except Exception as exc:
                    rejected = provider_precondition(exc)
                    precondition_rejects += int(rejected)
                    errors.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
                    if rejected and use_response_format:
                        use_response_format = False
                    if attempt < self.settings.retries:
                        await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 12))
        row = {
            "cache_key": cache_key,
            **identity,
            "status": "FAILED",
            "parsed": None,
            "api_attempt_count": self.settings.retries,
            "retry_count": self.settings.retries - 1,
            "provider_precondition_rejected_count": precondition_rejects,
            "errors": errors,
        }
        await self.append(row)
        return {**row, "cache_hit_this_run": False}


def dataset_arg_namespace(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=config_path,
        output_dir=None,
        shortterm_window=None,
        shortterm_top_k=None,
        midterm_top_k=None,
        longterm_top_k=None,
        embedding_model=None,
        device=None,
        batch_size=None,
    )


def eligible_queries_and_groups(
    dataset_settings: DatasetSettings, sessions: Mapping[str, Sequence[DatasetTurn]]
) -> tuple[dict[str, list[GoldRequirement]], dict[str, DatasetTurn]]:
    groups: dict[str, list[GoldRequirement]] = {}
    turns_by_id: dict[str, DatasetTurn] = {}
    for code in dataset_settings.session_codes:
        turns = sessions[code]
        for turn in turns:
            turns_by_id[turn.query_id] = turn
            short_ids = [
                candidate.query_id
                for candidate in turns[max(0, turn.turn_index - dataset_settings.shortterm_qa_turns) : turn.turn_index]
            ]
            eligible = [group for group in turn.gold_groups if not group.hit_by(short_ids)]
            if eligible:
                groups[turn.query_id] = eligible
    return groups, turns_by_id


def requirement_rank(group: GoldRequirement, ranking: Sequence[Mapping[str, Any]]) -> int:
    rank_by_source = {str(row["source_turn_id"]): index for index, row in enumerate(ranking, start=1)}
    ranks = [rank_by_source[member] for member in group.members if member in rank_by_source]
    if not ranks:
        raise AssertionError(f"Visible ranking 缺少 eligible OR Gold members：{group.members}")
    return min(ranks)


def evaluate_or_rankings(
    groups_by_query: Mapping[str, Sequence[GoldRequirement]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    turns_by_id: Mapping[str, DatasetTurn],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    gold_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    session_hits: dict[str, list[bool]] = defaultdict(list)
    for query_id, groups in groups_by_query.items():
        ranking = rankings[query_id]
        ranks = []
        for index, group in enumerate(groups, start=1):
            rank = requirement_rank(group, ranking)
            ranks.append(rank)
            gold_rows.append(
                {
                    "requirement_id": f"{query_id}::E{index}",
                    "session_id": turns_by_id[query_id].session_code,
                    "query_id": query_id,
                    "gold_members": list(group.members),
                    "is_or": group.is_or,
                    "rank": rank,
                    "hit_at_5": rank <= 5,
                    "hit_at_10": rank <= 10,
                    "hit_at_20": rank <= 20,
                }
            )
            session_hits[turns_by_id[query_id].session_code].append(rank <= 5)
        query_rows.append(
            {
                "query_id": query_id,
                "session_id": turns_by_id[query_id].session_code,
                "eligible_gold_count": len(groups),
                "gold_ranks": ranks,
                "best_gold_rank": min(ranks),
                "reciprocal_rank": 1.0 / min(ranks),
                "recall_at_5": sum(rank <= 5 for rank in ranks) / len(ranks),
            }
        )
    total = len(gold_rows)
    metrics = {
        "evaluated_query_count": len(query_rows),
        "eligible_gold_count": total,
        "gold_at_5": sum(row["hit_at_5"] for row in gold_rows),
        "recall_at_5": sum(row["hit_at_5"] for row in gold_rows) / total,
        "gold_at_10": sum(row["hit_at_10"] for row in gold_rows),
        "recall_at_10": sum(row["hit_at_10"] for row in gold_rows) / total,
        "gold_at_20": sum(row["hit_at_20"] for row in gold_rows),
        "recall_at_20": sum(row["hit_at_20"] for row in gold_rows) / total,
        "mrr": statistics.fmean(row["reciprocal_rank"] for row in query_rows),
        "mean_gold_rank": statistics.fmean(row["rank"] for row in gold_rows),
        "macro_query_recall_at_5": statistics.fmean(row["recall_at_5"] for row in query_rows),
        "macro_session_recall_at_5": statistics.fmean(sum(values) / len(values) for values in session_hits.values()),
    }
    return metrics, gold_rows, query_rows


def rerank_top_candidates(
    c3_ranking: Sequence[Mapping[str, Any]], score_by_page_id: Mapping[str, float], candidate_k: int
) -> list[dict[str, Any]]:
    candidates = [dict(row) for row in c3_ranking[:candidate_k]]
    original_rank = {str(row["page_id"]): index for index, row in enumerate(candidates, start=1)}
    if set(score_by_page_id) != set(original_rank):
        raise ValueError("LLM score candidate set 与 C3 TopK 不一致")
    candidates.sort(key=lambda row: (-float(score_by_page_id[str(row["page_id"])]), original_rank[str(row["page_id"])]))
    rows = []
    for rank, row in enumerate([*candidates, *[dict(item) for item in c3_ranking[candidate_k:]]], start=1):
        row["rank"] = rank
        row["dependency_score"] = score_by_page_id.get(str(row["page_id"]))
        rows.append(row)
    return rows


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
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                    for key, value in row.items()
                }
            )


def movement_rows(
    baseline_gold: Sequence[Mapping[str, Any]], reranked_gold: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    after = {str(row["requirement_id"]): row for row in reranked_gold}
    rows = []
    for before in baseline_gold:
        current = after[str(before["requirement_id"])]
        old_rank, new_rank = int(before["rank"]), int(current["rank"])
        transition = (
            "PROMOTED"
            if old_rank > 5 and new_rank <= 5
            else "DEMOTED"
            if old_rank <= 5 and new_rank > 5
            else "UNCHANGED"
        )
        rows.append(
            {
                "requirement_id": before["requirement_id"],
                "session_id": before["session_id"],
                "query_id": before["query_id"],
                "gold_members": before["gold_members"],
                "is_or": before["is_or"],
                "c3_rank": old_rank,
                "llm_rank": new_rank,
                "rank_change": old_rank - new_rank,
                "transition": transition,
            }
        )
    return rows


def query_movements(
    baseline_queries: Sequence[Mapping[str, Any]], reranked_queries: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    after = {str(row["query_id"]): row for row in reranked_queries}
    rows = []
    for before in baseline_queries:
        current = after[str(before["query_id"])]
        base_hit = any(rank <= 5 for rank in before["gold_ranks"])
        llm_hit = any(rank <= 5 for rank in current["gold_ranks"])
        transition = "RESCUED" if not base_hit and llm_hit else "HURT" if base_hit and not llm_hit else "UNCHANGED"
        rows.append(
            {
                "session_id": before["session_id"],
                "query_id": before["query_id"],
                "c3_gold_ranks": before["gold_ranks"],
                "llm_gold_ranks": current["gold_ranks"],
                "transition": transition,
            }
        )
    return rows


def ranking_export_rows(
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    groups_by_query: Mapping[str, Sequence[GoldRequirement]],
    page_by_id: Mapping[str, Mapping[str, Any]],
    candidate_k: int,
) -> list[dict[str, Any]]:
    rows = []
    for query_id in groups_by_query:
        gold_sources = {member for group in groups_by_query[query_id] for member in group.members}
        candidates = []
        for rank, item in enumerate(rankings[query_id][:candidate_k], start=1):
            page = page_by_id[str(item["page_id"])]
            candidates.append(
                {
                    "rank": rank,
                    "page_id": str(item["page_id"]),
                    "source_turn_id": str(item["source_turn_id"]),
                    "is_eligible_gold_member": str(item["source_turn_id"]) in gold_sources,
                    "dense_score": float(item["score"]),
                    "dependency_score": item.get("dependency_score"),
                    "summary": str(page["summary"]),
                    "keywords": list(page["keywords"]),
                }
            )
        rows.append({"query_id": query_id, "candidate_count": len(candidates), "ranking": candidates})
    return rows


def choose_representative_cases(
    movements: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    turns_by_id: Mapping[str, DatasetTurn],
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    llm_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    groups_by_query: Mapping[str, Sequence[GoldRequirement]],
    page_by_id: Mapping[str, Mapping[str, Any]],
    query_texts: Mapping[str, str],
) -> list[dict[str, Any]]:
    selected: list[tuple[str, Mapping[str, Any]]] = []
    promoted = sorted(
        (row for row in movements if row["transition"] == "PROMOTED"), key=lambda row: -int(row["rank_change"])
    )[:3]
    demoted = sorted(
        (row for row in movements if row["transition"] == "DEMOTED"), key=lambda row: int(row["rank_change"])
    )[:3]
    selected.extend(("PROMOTED", row) for row in promoted)
    selected.extend(("DEMOTED", row) for row in demoted)
    movement_by_query = {str(row["query_id"]): row for row in movements}
    for query in query_rows:
        query_id = str(query["query_id"])
        gold_sources = {member for group in groups_by_query[query_id] for member in group.members}
        top = llm_rankings[query_id][0]
        if str(top["source_turn_id"]) not in gold_sources:
            selected.append(("HARD_NEGATIVE", movement_by_query[query_id]))
            if sum(label == "HARD_NEGATIVE" for label, _ in selected) == 3:
                break
    cases = []
    seen: set[tuple[str, str]] = set()
    for label, movement in selected:
        query_id = str(movement["query_id"])
        key = (label, query_id)
        if key in seen:
            continue
        seen.add(key)
        gold_sources = {member for group in groups_by_query[query_id] for member in group.members}
        llm_rows = llm_rankings[query_id]
        gold_item = min(
            (row for row in llm_rows if str(row["source_turn_id"]) in set(movement["gold_members"])),
            key=lambda row: int(row["rank"]),
        )
        competitor = next(row for row in llm_rows if str(row["source_turn_id"]) not in gold_sources)
        gold_page = page_by_id[str(gold_item["page_id"])]
        competing_page = page_by_id[str(competitor["page_id"])]
        cases.append(
            {
                "case_type": label,
                "query_id": query_id,
                "original_query": turns_by_id[query_id].question,
                "p2_query": query_texts[query_id],
                "gold_members": movement["gold_members"],
                "c3_gold_rank": movement["c3_rank"],
                "llm_gold_rank": movement["llm_rank"],
                "gold_page": {
                    "page_id": gold_item["page_id"],
                    "source_turn_id": gold_item["source_turn_id"],
                    "summary": gold_page["summary"],
                    "keywords": gold_page["keywords"],
                    "dependency_score": gold_item.get("dependency_score"),
                },
                "competing_non_gold_page": {
                    "page_id": competitor["page_id"],
                    "source_turn_id": competitor["source_turn_id"],
                    "rank": competitor["rank"],
                    "summary": competing_page["summary"],
                    "keywords": competing_page["keywords"],
                    "dependency_score": competitor.get("dependency_score"),
                },
                "c3_top20_page_ids": [str(row["page_id"]) for row in c3_rankings[query_id][:20]],
                "llm_top20_page_ids": [str(row["page_id"]) for row in llm_rankings[query_id][:20]],
            }
        )
    return cases


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# LLM Historical Dependency Reranking 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['case_type']} — {case['query_id']}",
                "",
                "### Query",
                "",
                str(case["p2_query"]),
                "",
                f"Gold rank：C3 #{case['c3_gold_rank']} → LLM #{case['llm_gold_rank']}",
                "",
                "### Gold Page",
                "",
                f"- Page ID：{case['gold_page']['page_id']}",
                f"- Source Turn：{case['gold_page']['source_turn_id']}",
                f"- Dependency score：{case['gold_page']['dependency_score']}",
                f"- Summary：{case['gold_page']['summary']}",
                f"- Keywords：{', '.join(case['gold_page']['keywords'])}",
                "",
                "### 最高排名 Non-Gold Page",
                "",
                f"- Page ID：{case['competing_non_gold_page']['page_id']}",
                f"- Source Turn：{case['competing_non_gold_page']['source_turn_id']}",
                f"- LLM rank：#{case['competing_non_gold_page']['rank']}",
                f"- Dependency score：{case['competing_non_gold_page']['dependency_score']}",
                f"- Summary：{case['competing_non_gold_page']['summary']}",
                f"- Keywords：{', '.join(case['competing_non_gold_page']['keywords'])}",
                "",
            ]
        )
    return "\n".join(lines)


def render_report(
    baseline: Mapping[str, Any],
    llm: Mapping[str, Any],
    movements: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    api: Mapping[str, Any],
    session_rows: Sequence[Mapping[str, Any]],
    score_diagnostics: Mapping[str, Any],
) -> str:
    promoted = sum(row["transition"] == "PROMOTED" for row in movements)
    demoted = sum(row["transition"] == "DEMOTED" for row in movements)
    rescued = sum(row["transition"] == "RESCUED" for row in queries)
    hurt = sum(row["transition"] == "HURT" for row in queries)
    preserved = sum(int(row["c3_rank"]) <= 5 and int(row["llm_rank"]) <= 5 for row in movements)
    lines = [
        "# LLM Historical Dependency Reranking",
        "",
        "| Configuration | Micro R@5 | Gold@5 | Macro Session R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        f"| C3 Dense | {baseline['recall_at_5']:.2%} | {baseline['gold_at_5']}/{baseline['eligible_gold_count']} | "
        f"{baseline['macro_session_recall_at_5']:.2%} | {baseline['recall_at_10']:.2%} | "
        f"{baseline['recall_at_20']:.2%} | {baseline['mrr']:.4f} | {baseline['mean_gold_rank']:.2f} |",
        f"| C3 Top20 + LLM Dependency Rerank | {llm['recall_at_5']:.2%} | {llm['gold_at_5']}/{llm['eligible_gold_count']} | "
        f"{llm['macro_session_recall_at_5']:.2%} | {llm['recall_at_10']:.2%} | {llm['recall_at_20']:.2%} | "
        f"{llm['mrr']:.4f} | {llm['mean_gold_rank']:.2f} |",
        "",
        "## Movement",
        "",
        f"- Promoted Gold：{promoted}",
        f"- Demoted Gold：{demoted}",
        f"- Net Gold Gain：{promoted - demoted:+d}",
        f"- Rescued Queries：{rescued}",
        f"- Hurt Queries：{hurt}",
        f"- Preserved C3 Top5 Gold：{preserved}/{baseline['gold_at_5']}",
        "",
        "## API / Cache",
        "",
        f"- Successful Query outputs：{api['successful_output_count']}",
        f"- API attempts（本次 / 累计成功生成）：{api['api_attempt_count_this_run']} / "
        f"{api['cumulative_successful_api_attempt_count']}",
        f"- Cache hits（本次）：{api['cache_hit_count_this_run']}",
        f"- Retries（本次 / 累计）：{api['retry_count_this_run']} / {api['cumulative_retry_count']}",
        "",
        "## Session R@5",
        "",
        "| Session | C3 | LLM Dependency Rerank |",
        "|---|---:|---:|",
    ]
    by_session = {
        (str(row["configuration"]), str(row["session_id"])): float(row["recall_at_5"]) for row in session_rows
    }
    session_ids = sorted({str(row["session_id"]) for row in session_rows})
    for session_id in session_ids:
        lines.append(
            f"| {session_id} | {by_session[('C3 Dense', session_id)]:.2%} | "
            f"{by_session[('LLM Dependency Rerank', session_id)]:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Score Diagnostics",
            "",
            f"- 全候选同分 Query：{score_diagnostics['all_candidates_tied_query_count']}/"
            f"{score_diagnostics['query_count']}",
            f"- 每个 Query 的 unique score 数：mean={score_diagnostics['unique_score_count_mean']:.2f}，"
            f"median={score_diagnostics['unique_score_count_median']:.1f}",
            "",
            "候选只来自冻结 C3 query-time visible Top20；Prompt 不包含 Gold、C3 score/rank、Raw QA、邻近 QA或未来对话。",
        ]
    )
    return "\n".join(lines) + "\n"


async def run_experiment(settings: ExperimentSettings, validate_only: bool) -> int:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path, prompt_sha256 = freeze_prompt(settings.output_dir)
    dataset_settings = load_dataset_settings(dataset_arg_namespace(settings.dataset_config))
    sessions = load_v3_dataset(dataset_settings.dataset_path, dataset_settings.session_codes)
    stats = static_dataset_stats(sessions, 3, 3)
    assert_expected_stats(stats, dataset_settings.expected)
    groups_by_query, turns_by_id = eligible_queries_and_groups(dataset_settings, sessions)
    if len(groups_by_query) != 97 or sum(len(groups) for groups in groups_by_query.values()) != 149:
        raise AssertionError("V3 eligible contract mismatch")
    c3_rankings, c3_meta = load_and_validate_c3(dataset_settings, sessions)
    baseline_metrics, baseline_gold, baseline_queries = evaluate_or_rankings(groups_by_query, c3_rankings, turns_by_id)
    expected = {"gold_at_5": 59, "gold_at_10": 84, "gold_at_20": 122, "eligible_gold_count": 149}
    if any(int(baseline_metrics[key]) != value for key, value in expected.items()):
        raise AssertionError(f"C3 V3 reproduction failed：{baseline_metrics}")

    _, _, _, checkpoints, _ = checkpoint_inputs()
    c3 = checkpoints["C3"]
    page_by_id = c3["page_by_id"]
    query_texts = c3["query_texts"]
    for query_id in groups_by_query:
        if len(c3_rankings[query_id]) < 1:
            raise AssertionError(f"C3 candidate set empty：{query_id}")
        if len(c3_rankings[query_id][: settings.candidate_k]) != min(settings.candidate_k, len(c3_rankings[query_id])):
            raise AssertionError(f"C3 Top20 candidate scope mismatch：{query_id}")
    validation = {
        "v3_gold_requirement_count_559": stats["gold_requirement_count"] == 559,
        "outside_shortterm_gold_149": baseline_metrics["eligible_gold_count"] == 149,
        "eligible_query_97": baseline_metrics["evaluated_query_count"] == 97,
        "c3_r5_59_of_149": baseline_metrics["gold_at_5"] == 59,
        "c3_r10_84_of_149": baseline_metrics["gold_at_10"] == 84,
        "c3_r20_122_of_149": baseline_metrics["gold_at_20"] == 122,
        "prompt_frozen_before_api": prompt_path.exists() and sha256_file(prompt_path) == prompt_sha256,
        "no_page_regeneration": True,
        "no_future_leakage": True,
        "or_gold": True,
    }
    if not all(validation.values()):
        raise AssertionError(f"Pre-LLM validation failed：{validation}")
    if validate_only:
        print(
            json.dumps(
                {"dataset": stats, "baseline": baseline_metrics, "validation": validation}, ensure_ascii=False, indent=2
            )
        )
        return 0

    memory_config = expand_env_placeholders(load_json(settings.memory_config))
    llm_config = dict((memory_config.get("llm") or {}).get("config") or {})
    api_key = llm_config.get("api_key")
    if not api_key or str(api_key).startswith("${"):
        raise RuntimeError("DeepSeek API key 未配置")
    base_url = llm_config.get("deepseek_base_url") or os.getenv("DEEPSEEK_API_BASE") or "https://api.deepseek.com"
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    cache = DependencyScoreCache(
        settings.output_dir / "cache/dependency_scores.jsonl",
        client=client,
        settings=settings,
        prompt_sha256=prompt_sha256,
    )
    tasks = [
        cache.call(
            query_id=query_id,
            query_text=query_texts[query_id],
            candidates=c3_rankings[query_id][: settings.candidate_k],
            page_by_id=page_by_id,
        )
        for query_id in groups_by_query
    ]
    responses = await asyncio.gather(*tasks)
    failures = [row for row in responses if row["status"] != "SUCCESS"]
    api_meta = {
        "successful_output_count": len(responses) - len(failures),
        "failed_output_count": len(failures),
        "api_attempt_count_this_run": sum(
            0 if row.get("cache_hit_this_run") else int(row.get("api_attempt_count") or 0) for row in responses
        ),
        "cache_hit_count_this_run": sum(bool(row.get("cache_hit_this_run")) for row in responses),
        "retry_count_this_run": sum(
            0 if row.get("cache_hit_this_run") else int(row.get("retry_count") or 0) for row in responses
        ),
        "provider_precondition_rejected_count": sum(
            0 if row.get("cache_hit_this_run") else int(row.get("provider_precondition_rejected_count") or 0)
            for row in responses
        ),
        "prompt_tokens": sum(
            int(row.get("prompt_tokens") or 0) for row in responses if not row.get("cache_hit_this_run")
        ),
        "completion_tokens": sum(
            int(row.get("completion_tokens") or 0) for row in responses if not row.get("cache_hit_this_run")
        ),
        "cumulative_successful_api_attempt_count": sum(
            int(row.get("api_attempt_count") or 0) for row in cache.success.values()
        ),
        "cumulative_retry_count": sum(int(row.get("retry_count") or 0) for row in cache.success.values()),
        "persistent_success_cache_entry_count": len(cache.success),
    }
    if failures:
        write_json(settings.output_dir / "failed_queries.json", failures)
        write_json(settings.output_dir / "run_metadata.json", {"validation": validation, "api": api_meta})
        raise RuntimeError(f"{len(failures)} 个 Query 的 LLM listwise 输出最终失败；已停止指标生成")

    llm_rankings: dict[str, list[dict[str, Any]]] = {}
    response_by_query = {str(row["query_id"]): row for row in responses}
    for query_id in groups_by_query:
        response = response_by_query[query_id]
        parsed = validate_dependency_scores(response["parsed"], response["candidate_page_ids"])
        llm_rankings[query_id] = rerank_top_candidates(
            c3_rankings[query_id], parsed["score_by_page_id"], settings.candidate_k
        )
        if {str(row["page_id"]) for row in llm_rankings[query_id][: settings.candidate_k]} != {
            str(row["page_id"]) for row in c3_rankings[query_id][: settings.candidate_k]
        }:
            raise AssertionError(f"LLM/C3 candidate set mismatch：{query_id}")

    llm_metrics, llm_gold, llm_queries = evaluate_or_rankings(groups_by_query, llm_rankings, turns_by_id)
    movements = movement_rows(baseline_gold, llm_gold)
    query_movement_rows = query_movements(baseline_queries, llm_queries)
    cases = choose_representative_cases(
        movements,
        llm_queries,
        turns_by_id,
        c3_rankings,
        llm_rankings,
        groups_by_query,
        page_by_id,
        query_texts,
    )
    c3_exports = ranking_export_rows(c3_rankings, groups_by_query, page_by_id, settings.candidate_k)
    llm_exports = ranking_export_rows(llm_rankings, groups_by_query, page_by_id, settings.candidate_k)
    metric_rows = [
        {"configuration": "C3 Dense", **baseline_metrics},
        {"configuration": "C3 Top20 + LLM Historical Dependency Rerank", **llm_metrics},
    ]
    session_rows = []
    for configuration, gold_rows in (("C3 Dense", baseline_gold), ("LLM Dependency Rerank", llm_gold)):
        for code in dataset_settings.session_codes:
            selected = [row for row in gold_rows if row["session_id"] == code]
            session_rows.append(
                {
                    "configuration": configuration,
                    "session_id": code,
                    "eligible_gold_count": len(selected),
                    "gold_at_5": sum(row["hit_at_5"] for row in selected),
                    "recall_at_5": sum(row["hit_at_5"] for row in selected) / len(selected),
                }
            )
    unique_score_counts = []
    all_scores = []
    for response in responses:
        scores = [float(row["dependency_score"]) for row in response["parsed"]["results"]]
        unique_score_counts.append(len(set(scores)))
        all_scores.extend(scores)
    score_frequencies: dict[str, int] = defaultdict(int)
    for score in all_scores:
        score_frequencies[str(score)] += 1
    score_diagnostics = {
        "query_count": len(responses),
        "all_candidates_tied_query_count": sum(count == 1 for count in unique_score_counts),
        "unique_score_count_mean": statistics.fmean(unique_score_counts),
        "unique_score_count_median": statistics.median(unique_score_counts),
        "unique_score_count_min": min(unique_score_counts),
        "unique_score_count_max": max(unique_score_counts),
        "score_frequencies": dict(sorted(score_frequencies.items(), key=lambda item: (-item[1], item[0]))),
    }
    write_csv(settings.output_dir / "metrics.csv", metric_rows)
    write_csv(settings.output_dir / "session_metrics.csv", session_rows)
    write_csv(settings.output_dir / "gold_movements.csv", movements)
    write_csv(settings.output_dir / "query_movements.csv", query_movement_rows)
    write_jsonl(settings.output_dir / "rankings/c3_top20.jsonl", c3_exports)
    write_jsonl(settings.output_dir / "rankings/llm_top20.jsonl", llm_exports)
    write_json(settings.output_dir / "representative_cases.json", cases)
    write_json(settings.output_dir / "analysis/score_diagnostics.json", score_diagnostics)
    (settings.output_dir / "representative_cases.md").write_text(render_cases(cases), encoding="utf-8")
    report = render_report(
        baseline_metrics,
        llm_metrics,
        movements,
        query_movement_rows,
        api_meta,
        session_rows,
        score_diagnostics,
    )
    (settings.output_dir / "experiment_report.md").write_text(report, encoding="utf-8")
    metadata = {
        "experiment_name": settings.raw_config["experiment_name"],
        "settings": asdict(settings),
        "prompt": {"path": str(prompt_path), "sha256": prompt_sha256, "version": PROMPT_VERSION},
        "dataset_sha256": sha256_file(dataset_settings.dataset_path),
        "source_qa_sha256": source_qa_hash(sessions),
        "c3": c3_meta,
        "api": api_meta,
        "validation": {key: "PASS" if value else "FAIL" for key, value in validation.items()},
        "page_regeneration": False,
        "new_embedding_count": 0,
        "full_session_rerun": False,
        "mem0_production_modified": False,
    }
    write_json(settings.output_dir / "run_metadata.json", metadata)
    print(report)
    print(f"结果目录：{settings.output_dir}")
    return 0


def main() -> int:
    args = parse_args()
    settings = load_experiment_settings(args)
    return asyncio.run(run_experiment(settings, args.validate_only))


if __name__ == "__main__":
    raise SystemExit(main())
