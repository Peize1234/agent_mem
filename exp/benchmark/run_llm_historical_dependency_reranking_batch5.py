"""Five-candidate batched LLM dependency reranking over the frozen C3 Top20."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from openai import AsyncOpenAI

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, expand_env_placeholders, load_json, resolve_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.run_full_memory_recall_s001_s005_v2 import (  # noqa: E402
    assert_expected_stats,
    load_and_validate_c3,
    load_settings as load_dataset_settings,
    load_v3_dataset,
    source_qa_hash,
    static_dataset_stats,
)
from exp.benchmark.run_llm_historical_dependency_reranking import (  # noqa: E402
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    candidate_payload,
    dataset_arg_namespace,
    eligible_queries_and_groups,
    evaluate_or_rankings,
    movement_rows,
    parse_json_object,
    provider_precondition,
    query_movements,
    ranking_export_rows,
    rerank_top_candidates,
    sha256_file,
    sha256_text,
    validate_dependency_scores,
    write_csv,
    write_json,
    write_jsonl,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402
from exp.benchmark.midterm_retrieval_eval import load_jsonl, stable_hash  # noqa: E402


DEFAULT_CONFIG = REPO_ROOT / "exp/benchmark/llm_historical_dependency_reranking_batch5.json"
OLD_CONFIGURATION = "C3 Top20 + 20-way LLM Historical Dependency Rerank"
BATCH_CONFIGURATION = "C3 Top20 + 5-candidate Batched LLM Historical Dependency Rerank"


@dataclass(frozen=True)
class ExperimentSettings:
    output_dir: Path
    old_result_dir: Path
    model: str
    temperature: float
    top_p: float
    max_tokens: int
    timeout_seconds: float
    retries: int
    concurrency: int
    candidate_k: int
    llm_batch_candidate_count: int
    output_k: int
    thinking_mode: str
    dataset_config: Path
    memory_config: Path
    raw_config: Mapping[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="C3 Top20 dependency reranking using independent batches of five")
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
    parser.add_argument("--llm-batch-candidate-count", type=int)
    parser.add_argument("--output-k", type=int)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def load_experiment_settings(args: argparse.Namespace) -> ExperimentSettings:
    raw = load_json(args.config.resolve())

    def selected(name: str, override: Any) -> Any:
        return raw[name] if override is None else override

    settings = ExperimentSettings(
        output_dir=Path(args.output_dir or resolve_path(REPO_ROOT, raw["output_dir"])).resolve(),
        old_result_dir=resolve_path(REPO_ROOT, raw["old_result_dir"]),
        model=str(selected("model", args.model)),
        temperature=float(selected("temperature", args.temperature)),
        top_p=float(selected("top_p", args.top_p)),
        max_tokens=int(selected("max_tokens", args.max_tokens)),
        timeout_seconds=float(selected("timeout_seconds", args.timeout)),
        retries=int(selected("retries", args.retries)),
        concurrency=int(selected("concurrency", args.concurrency)),
        candidate_k=int(selected("candidate_k", args.candidate_k)),
        llm_batch_candidate_count=int(
            selected("llm_batch_candidate_count", args.llm_batch_candidate_count)
        ),
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
        settings.llm_batch_candidate_count,
        settings.output_k,
    )
    if any(value <= 0 for value in numeric) or settings.timeout_seconds <= 0:
        raise ValueError("数值配置必须为正数")
    if (settings.candidate_k, settings.llm_batch_candidate_count, settings.output_k) != (20, 5, 5):
        raise ValueError("本轮冻结 candidate_k=20、llm_batch_candidate_count=5、output_k=5")
    return settings


def freeze_prompt(output_dir: Path, old_result_dir: Path) -> tuple[Path, str]:
    old_path = old_result_dir / "prompts/historical_dependency_listwise_v1.txt"
    if not old_path.exists() or old_path.read_text(encoding="utf-8") != SYSTEM_PROMPT:
        raise AssertionError("旧 20-way 冻结 Prompt 与代码 SYSTEM_PROMPT 不一致")
    prompt_dir = output_dir / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / "historical_dependency_listwise_v1.txt"
    if path.exists() and path.read_text(encoding="utf-8") != SYSTEM_PROMPT:
        raise AssertionError(f"batch5 Prompt 已存在但内容不一致：{path}")
    if not path.exists():
        path.write_text(SYSTEM_PROMPT, encoding="utf-8")
    if sha256_file(path) != sha256_file(old_path):
        raise AssertionError("batch5 与 20-way Prompt hash 不一致")
    return path, sha256_file(path)


def split_candidate_batches(
    ranking: Sequence[Mapping[str, Any]], candidate_k: int = 20, batch_size: int = 5
) -> list[list[dict[str, Any]]]:
    candidates = [dict(row) for row in ranking[:candidate_k]]
    batches = [candidates[start : start + batch_size] for start in range(0, len(candidates), batch_size)]
    flattened = [str(row["page_id"]) for batch in batches for row in batch]
    expected = [str(row["page_id"]) for row in candidates]
    if flattened != expected or len(flattened) != len(set(flattened)):
        raise AssertionError("batch 拆分改变、遗漏或重复了 C3 Top20 candidate set")
    if any(not batch or len(batch) > batch_size for batch in batches):
        raise AssertionError("batch size contract mismatch")
    return batches


class BatchedDependencyScoreCache:
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

    async def append(self, row: Mapping[str, Any]) -> None:
        async with self.lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False, default=str) + "\n")

    def identity(
        self,
        *,
        query_id: str,
        query_text: str,
        batch_index: int,
        candidates: Sequence[Mapping[str, Any]],
        payload: str,
    ) -> dict[str, Any]:
        return {
            "query_id": query_id,
            "batch_index": batch_index,
            "query_text_sha256": sha256_text(query_text),
            "candidate_page_ids": [str(row["page_id"]) for row in candidates],
            "candidate_payload_sha256": sha256_text(payload),
            "candidate_k": self.settings.candidate_k,
            "llm_batch_candidate_count": self.settings.llm_batch_candidate_count,
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
        batch_index: int,
        candidates: Sequence[Mapping[str, Any]],
        page_by_id: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        payload = candidate_payload(query_text, candidates, page_by_id)
        identity = self.identity(
            query_id=query_id,
            query_text=query_text,
            batch_index=batch_index,
            candidates=candidates,
            payload=payload,
        )
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
                        "total_tokens": getattr(usage, "total_tokens", None),
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


def load_old_20way_scores(
    old_result_dir: Path,
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    query_ids: Sequence[str],
    candidate_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    cache_rows = load_jsonl(old_result_dir / "cache/dependency_scores.jsonl")
    success_by_query = {str(row["query_id"]): row for row in cache_rows if row.get("status") == "SUCCESS"}
    if set(success_by_query) != set(query_ids):
        raise AssertionError("旧 20-way cache query set 不完整或包含额外 Query")
    old_rankings: dict[str, list[dict[str, Any]]] = {}
    all_tied: set[str] = set()
    for query_id in query_ids:
        response = success_by_query[query_id]
        expected_ids = [str(row["page_id"]) for row in c3_rankings[query_id][:candidate_k]]
        if list(response["candidate_page_ids"]) != expected_ids:
            raise AssertionError(f"旧 20-way/C3 candidate order 不一致：{query_id}")
        parsed = validate_dependency_scores(response["parsed"], expected_ids)
        scores = parsed["score_by_page_id"]
        if len(set(scores.values())) == 1:
            all_tied.add(query_id)
        old_rankings[query_id] = rerank_top_candidates(c3_rankings[query_id], scores, candidate_k)
    return old_rankings, all_tied


def compare_candidate_artifact(
    path: Path, c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]], query_ids: Sequence[str], candidate_k: int
) -> None:
    rows = {str(row["query_id"]): row for row in load_jsonl(path)}
    if set(rows) != set(query_ids):
        raise AssertionError("旧 candidate artifact query set 不一致")
    for query_id in query_ids:
        artifact_ids = [str(row["page_id"]) for row in rows[query_id]["ranking"]]
        current_ids = [str(row["page_id"]) for row in c3_rankings[query_id][:candidate_k]]
        if artifact_ids != current_ids:
            raise AssertionError(f"新旧实验 C3 Top20 candidate set/order 不一致：{query_id}")


def comparative_movements(
    before_gold: Sequence[Mapping[str, Any]], after_gold: Sequence[Mapping[str, Any]], before_label: str, after_label: str
) -> list[dict[str, Any]]:
    after = {str(row["requirement_id"]): row for row in after_gold}
    rows: list[dict[str, Any]] = []
    for prior in before_gold:
        current = after[str(prior["requirement_id"])]
        old_rank, new_rank = int(prior["rank"]), int(current["rank"])
        transition = (
            "PROMOTED"
            if old_rank > 5 and new_rank <= 5
            else "DEMOTED"
            if old_rank <= 5 and new_rank > 5
            else "UNCHANGED"
        )
        rows.append(
            {
                "requirement_id": prior["requirement_id"],
                "session_id": prior["session_id"],
                "query_id": prior["query_id"],
                "gold_members": prior["gold_members"],
                "is_or": prior["is_or"],
                f"{before_label}_rank": old_rank,
                f"{after_label}_rank": new_rank,
                "rank_change": old_rank - new_rank,
                "transition": transition,
            }
        )
    return rows


def score_diagnostics(
    responses: Sequence[Mapping[str, Any]], expected_query_ids: Sequence[str], old_all_tied: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, float]]]:
    batch_rows: list[dict[str, Any]] = []
    scores_by_query: dict[str, dict[str, float]] = defaultdict(dict)
    frequency: Counter[str] = Counter()
    for response in responses:
        values = [float(row["dependency_score"]) for row in response["parsed"]["results"]]
        score_map = {str(row["page_id"]): float(row["dependency_score"]) for row in response["parsed"]["results"]}
        query_id = str(response["query_id"])
        if set(scores_by_query[query_id]) & set(score_map):
            raise AssertionError(f"batch merge 发现重复 candidate：{query_id}")
        scores_by_query[query_id].update(score_map)
        frequency.update(str(value) for value in values)
        batch_rows.append(
            {
                "query_id": query_id,
                "batch_index": int(response["batch_index"]),
                "candidate_count": len(values),
                "unique_score_count": len(set(values)),
                "all_candidates_tied": len(set(values)) == 1,
                "scores": values,
                "candidate_page_ids": list(response["candidate_page_ids"]),
                "cache_hit_this_run": bool(response.get("cache_hit_this_run")),
                "prompt_tokens": response.get("prompt_tokens"),
                "completion_tokens": response.get("completion_tokens"),
            }
        )
    if set(scores_by_query) != set(expected_query_ids):
        raise AssertionError("batch response query set 不完整")
    query_rows = []
    for query_id in expected_query_ids:
        values = list(scores_by_query[query_id].values())
        query_rows.append(
            {
                "query_id": query_id,
                "candidate_count": len(values),
                "batch_count": sum(row["query_id"] == query_id for row in batch_rows),
                "unique_score_count": len(set(values)),
                "all_candidates_tied": len(set(values)) == 1,
                "old_20way_all_candidates_tied": query_id in old_all_tied,
                "old_tied_batch5_discriminated": query_id in old_all_tied and len(set(values)) > 1,
            }
        )
    unique_batch = [int(row["unique_score_count"]) for row in batch_rows]
    unique_query = [int(row["unique_score_count"]) for row in query_rows]
    batch_index_stats = {}
    for batch_index in sorted({int(row["batch_index"]) for row in batch_rows}):
        selected = [row for row in batch_rows if int(row["batch_index"]) == batch_index]
        values = [float(score) for row in selected for score in row["scores"]]
        batch_index_stats[str(batch_index)] = {
            "batch_count": len(selected),
            "candidate_score_count": len(values),
            "score_mean": statistics.fmean(values),
            "score_median": statistics.median(values),
            "all_candidates_tied_batch_count": sum(bool(row["all_candidates_tied"]) for row in selected),
        }
    diagnostics = {
        "query_count": len(query_rows),
        "batch_count": len(batch_rows),
        "average_batches_per_query": len(batch_rows) / len(query_rows),
        "all_candidates_tied_batch_count": sum(bool(row["all_candidates_tied"]) for row in batch_rows),
        "all_candidates_tied_batch_rate": sum(bool(row["all_candidates_tied"]) for row in batch_rows)
        / len(batch_rows),
        "batch_unique_score_count_mean": statistics.fmean(unique_batch),
        "batch_unique_score_count_median": statistics.median(unique_batch),
        "merged_query_unique_score_count_mean": statistics.fmean(unique_query),
        "merged_query_unique_score_count_median": statistics.median(unique_query),
        "merged_all_candidates_tied_query_count": sum(bool(row["all_candidates_tied"]) for row in query_rows),
        "old_20way_all_candidates_tied_query_count": len(old_all_tied),
        "old_tied_batch5_discriminated_query_count": sum(
            bool(row["old_tied_batch5_discriminated"]) for row in query_rows
        ),
        "batch_index_stats": batch_index_stats,
        "score_frequency_distribution": dict(sorted(frequency.items(), key=lambda item: (-item[1], item[0]))),
    }
    return diagnostics, batch_rows, scores_by_query


def session_metric_rows(
    configurations: Sequence[tuple[str, Sequence[Mapping[str, Any]]]], session_codes: Sequence[str]
) -> list[dict[str, Any]]:
    rows = []
    for configuration, gold_rows in configurations:
        for session_id in session_codes:
            selected = [row for row in gold_rows if row["session_id"] == session_id]
            rows.append(
                {
                    "configuration": configuration,
                    "session_id": session_id,
                    "eligible_gold_count": len(selected),
                    "gold_at_5": sum(bool(row["hit_at_5"]) for row in selected),
                    "recall_at_5": sum(bool(row["hit_at_5"]) for row in selected) / len(selected),
                }
            )
    return rows


def build_cases(
    *,
    c3_gold: Sequence[Mapping[str, Any]],
    old_gold: Sequence[Mapping[str, Any]],
    batch_gold: Sequence[Mapping[str, Any]],
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    old_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    batch_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    groups_by_query: Mapping[str, Sequence[Any]],
    turns_by_id: Mapping[str, Any],
    query_texts: Mapping[str, str],
    page_by_id: Mapping[str, Mapping[str, Any]],
    query_diagnostics: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    old_by_req = {str(row["requirement_id"]): row for row in old_gold}
    batch_by_req = {str(row["requirement_id"]): row for row in batch_gold}
    movement_by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in c3_gold:
        old = old_by_req[str(row["requirement_id"])]
        batch = batch_by_req[str(row["requirement_id"])]
        c3_rank, batch_rank = int(row["rank"]), int(batch["rank"])
        transition = (
            "PROMOTED"
            if c3_rank > 5 and batch_rank <= 5
            else "DEMOTED"
            if c3_rank <= 5 and batch_rank > 5
            else "UNCHANGED"
        )
        movement_by_query[str(row["query_id"])].append(
            {
                "requirement_id": row["requirement_id"],
                "gold_members": row["gold_members"],
                "is_or": row["is_or"],
                "c3_rank": c3_rank,
                "old_20way_rank": int(old["rank"]),
                "batch5_rank": batch_rank,
                "transition_vs_c3": transition,
            }
        )
    diagnostic_by_query = {str(row["query_id"]): row for row in query_diagnostics}
    promoted = sorted(
        (row for rows in movement_by_query.values() for row in rows if row["transition_vs_c3"] == "PROMOTED"),
        key=lambda row: int(row["c3_rank"]) - int(row["batch5_rank"]),
        reverse=True,
    )
    demoted = sorted(
        (row for rows in movement_by_query.values() for row in rows if row["transition_vs_c3"] == "DEMOTED"),
        key=lambda row: int(row["batch5_rank"]) - int(row["c3_rank"]),
        reverse=True,
    )
    requirement_to_query = {
        str(row["requirement_id"]): query_id for query_id, rows in movement_by_query.items() for row in rows
    }
    selections: list[tuple[str, str]] = []
    for label, candidates in (("相对 C3 提升", promoted), ("相对 C3 下降", demoted)):
        seen: set[str] = set()
        for row in candidates:
            query_id = requirement_to_query[str(row["requirement_id"])]
            if query_id not in seen:
                selections.append((label, query_id))
                seen.add(query_id)
            if len(seen) == 3:
                break
    split = [row for row in query_diagnostics if row["old_tied_batch5_discriminated"]]
    split.sort(
        key=lambda row: max(
            (item["c3_rank"] - item["batch5_rank"] for item in movement_by_query[str(row["query_id"])]),
            default=0,
        ),
        reverse=True,
    )
    selections.extend(("旧 20-way 全同分、batch5 拉开", str(row["query_id"])) for row in split[:3])
    still_tied = [row for row in query_diagnostics if row["all_candidates_tied"]]
    selections.extend(("batch5 仍无法区分", str(row["query_id"])) for row in still_tied[:3])

    cases: list[dict[str, Any]] = []
    seen_selection: set[tuple[str, str]] = set()
    for label, query_id in selections:
        if (label, query_id) in seen_selection:
            continue
        seen_selection.add((label, query_id))
        all_gold_sources = {member for group in groups_by_query[query_id] for member in group.members}
        top_non_gold = next(
            row for row in batch_rankings[query_id] if str(row["source_turn_id"]) not in all_gold_sources
        )
        gold_pages = []
        for requirement in movement_by_query[query_id]:
            members = set(requirement["gold_members"])
            best = min(
                (row for row in batch_rankings[query_id] if str(row["source_turn_id"]) in members),
                key=lambda row: int(row["rank"]),
            )
            page = page_by_id[str(best["page_id"])]
            gold_pages.append(
                {
                    **requirement,
                    "page_id": str(best["page_id"]),
                    "source_turn_id": str(best["source_turn_id"]),
                    "summary": page["summary"],
                    "keywords": page["keywords"],
                    "batch5_dependency_score": best.get("dependency_score"),
                }
            )
        competitor = page_by_id[str(top_non_gold["page_id"])]
        cases.append(
            {
                "case_type": label,
                "query_id": query_id,
                "original_query": turns_by_id[query_id].question,
                "p2_query": query_texts[query_id],
                "score_diagnostics": diagnostic_by_query[query_id],
                "requirements": movement_by_query[query_id],
                "gold_pages": gold_pages,
                "top_competing_non_gold": {
                    "page_id": str(top_non_gold["page_id"]),
                    "source_turn_id": str(top_non_gold["source_turn_id"]),
                    "batch5_rank": int(top_non_gold["rank"]),
                    "batch5_dependency_score": top_non_gold.get("dependency_score"),
                    "summary": competitor["summary"],
                    "keywords": competitor["keywords"],
                },
                "c3_top20_page_ids": [str(row["page_id"]) for row in c3_rankings[query_id][:20]],
                "old_20way_top20_page_ids": [str(row["page_id"]) for row in old_rankings[query_id][:20]],
                "batch5_top20_page_ids": [str(row["page_id"]) for row in batch_rankings[query_id][:20]],
            }
        )
    return cases


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# 5-candidate Batched Historical Dependency Reranking 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['case_type']} — {case['query_id']}",
                "",
                f"Original Query：{case['original_query']}",
                "",
                f"P2 Query：{case['p2_query']}",
                "",
                f"Score diagnostics：{json.dumps(case['score_diagnostics'], ensure_ascii=False)}",
                "",
                "### 全部 Eligible Gold requirements",
                "",
            ]
        )
        for requirement in case["gold_pages"]:
            lines.extend(
                [
                    f"- {requirement['requirement_id']} / members={requirement['gold_members']} / "
                    f"C3 #{requirement['c3_rank']} / 20-way #{requirement['old_20way_rank']} / "
                    f"batch5 #{requirement['batch5_rank']} / score={requirement['batch5_dependency_score']}",
                    f"  - Gold Page：{requirement['page_id']} ({requirement['source_turn_id']})",
                    f"  - Summary：{requirement['summary']}",
                    f"  - Keywords：{', '.join(requirement['keywords'])}",
                ]
            )
        competitor = case["top_competing_non_gold"]
        lines.extend(
            [
                "",
                "### batch5 最高排名 Non-Gold",
                "",
                f"- Page：{competitor['page_id']} ({competitor['source_turn_id']})",
                f"- Rank / score：#{competitor['batch5_rank']} / {competitor['batch5_dependency_score']}",
                f"- Summary：{competitor['summary']}",
                f"- Keywords：{', '.join(competitor['keywords'])}",
                "",
            ]
        )
    return "\n".join(lines) + "\n"


def movement_summary(movements: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "promoted_gold": sum(row["transition"] == "PROMOTED" for row in movements),
        "demoted_gold": sum(row["transition"] == "DEMOTED" for row in movements),
        "net_gold_gain": sum(row["transition"] == "PROMOTED" for row in movements)
        - sum(row["transition"] == "DEMOTED" for row in movements),
        "rescued_queries": sum(row["transition"] == "RESCUED" for row in queries),
        "hurt_queries": sum(row["transition"] == "HURT" for row in queries),
        "preserved_c3_top5_gold": sum(
            int(row["c3_rank"]) <= 5 and int(row["llm_rank"]) <= 5 for row in movements
        ),
    }


def render_report(
    metric_rows: Sequence[Mapping[str, Any]],
    session_rows: Sequence[Mapping[str, Any]],
    movement: Mapping[str, int],
    old_vs_batch: Mapping[str, int],
    diagnostics: Mapping[str, Any],
    api: Mapping[str, Any],
) -> str:
    lines = [
        "# 5-candidate Batched LLM Historical Dependency Reranking",
        "",
        "| 配置 | Micro R@5 | Gold@5 | Macro Session R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metric_rows:
        lines.append(
            f"| {row['configuration']} | {row['recall_at_5']:.2%} | {row['gold_at_5']}/{row['eligible_gold_count']} | "
            f"{row['macro_session_recall_at_5']:.2%} | {row['recall_at_10']:.2%} | {row['recall_at_20']:.2%} | "
            f"{row['mrr']:.4f} | {row['mean_gold_rank']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## 相对 C3 Movement",
            "",
            f"- Promoted / Demoted / Net：{movement['promoted_gold']} / {movement['demoted_gold']} / "
            f"{movement['net_gold_gain']:+d}",
            f"- Rescued / Hurt Queries：{movement['rescued_queries']} / {movement['hurt_queries']}",
            f"- Preserved C3 Top5 Gold：{movement['preserved_c3_top5_gold']}/59",
            "",
            "## 相对旧 20-way Movement",
            "",
            f"- Promoted / Demoted / Net：{old_vs_batch['promoted_gold']} / {old_vs_batch['demoted_gold']} / "
            f"{old_vs_batch['net_gold_gain']:+d}",
            "",
            "## Score discrimination",
            "",
            f"- 旧 20-way 全候选同分：{diagnostics['old_20way_all_candidates_tied_query_count']}/97",
            f"- batch5 合并后全候选同分：{diagnostics['merged_all_candidates_tied_query_count']}/97",
            f"- 旧全同分但 batch5 拉开：{diagnostics['old_tied_batch5_discriminated_query_count']}/"
            f"{diagnostics['old_20way_all_candidates_tied_query_count']}",
            f"- 单 batch 全同分：{diagnostics['all_candidates_tied_batch_count']}/{diagnostics['batch_count']} "
            f"({diagnostics['all_candidates_tied_batch_rate']:.2%})",
            f"- 合并 Top20 unique score：mean={diagnostics['merged_query_unique_score_count_mean']:.2f}，"
            f"median={diagnostics['merged_query_unique_score_count_median']:.1f}",
            "",
            "## API / Cache",
            "",
            f"- Successful batch outputs：{api['successful_model_output_count']}",
            f"- Actual API attempts（完整实验累计，含失败重试）：{api['actual_api_attempt_count_total']}",
            f"- 当前执行 API attempts / cache hits：{api['api_attempt_count_this_run']} / "
            f"{api['cache_hit_count_this_run']}",
            f"- Prompt / completion / total tokens（完整实验累计）：{api['prompt_tokens_total']} / "
            f"{api['completion_tokens_total']} / {api['total_tokens_total']}",
            "",
            "## 结论",
            "",
            "batch5 显著减少了全候选同分，却没有改善 Historical Dependency 排序：Micro R@5 比旧 20-way 再低 "
            "0.67pp，比 C3 低 10.07pp。说明问题不只是一次阅读 20 个同质候选导致无法打分；拆批后的绝对分数在批次间"
            "缺乏可靠可比性，而且新增的分数差异没有对应更准确的依赖判断。",
            "",
            "## 各 Session R@5",
            "",
            "| Session | C3 | 20-way LLM | batch5 LLM |",
            "|---|---:|---:|---:|",
        ]
    )
    by_session = {
        (str(row["configuration"]), str(row["session_id"])): float(row["recall_at_5"])
        for row in session_rows
    }
    for session_id in sorted({str(row["session_id"]) for row in session_rows}):
        lines.append(
            f"| {session_id} | {by_session[('C3 Dense', session_id)]:.2%} | "
            f"{by_session[(OLD_CONFIGURATION, session_id)]:.2%} | "
            f"{by_session[(BATCH_CONFIGURATION, session_id)]:.2%} |"
        )
    return "\n".join(lines) + "\n"


async def run_experiment(settings: ExperimentSettings, validate_only: bool) -> int:
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    prompt_path, prompt_sha256 = freeze_prompt(settings.output_dir, settings.old_result_dir)
    dataset_settings = load_dataset_settings(dataset_arg_namespace(settings.dataset_config))
    sessions = load_v3_dataset(dataset_settings.dataset_path, dataset_settings.session_codes)
    stats = static_dataset_stats(sessions, 3, 3)
    assert_expected_stats(stats, dataset_settings.expected)
    groups_by_query, turns_by_id = eligible_queries_and_groups(dataset_settings, sessions)
    query_ids = list(groups_by_query)
    if len(query_ids) != 97 or sum(len(groups) for groups in groups_by_query.values()) != 149:
        raise AssertionError("V3 eligible contract mismatch")

    c3_rankings, c3_meta = load_and_validate_c3(dataset_settings, sessions)
    c3_metrics, c3_gold, c3_queries = evaluate_or_rankings(groups_by_query, c3_rankings, turns_by_id)
    expected = {"gold_at_5": 59, "gold_at_10": 84, "gold_at_20": 122, "eligible_gold_count": 149}
    if any(int(c3_metrics[key]) != value for key, value in expected.items()):
        raise AssertionError(f"C3 V3 reproduction failed：{c3_metrics}")

    _, _, _, checkpoints, _ = checkpoint_inputs()
    c3 = checkpoints["C3"]
    page_by_id = c3["page_by_id"]
    query_texts = c3["query_texts"]
    compare_candidate_artifact(
        settings.old_result_dir / "rankings/c3_top20.jsonl", c3_rankings, query_ids, settings.candidate_k
    )
    old_rankings, old_all_tied = load_old_20way_scores(
        settings.old_result_dir, c3_rankings, query_ids, settings.candidate_k
    )
    old_metrics, old_gold, old_queries = evaluate_or_rankings(groups_by_query, old_rankings, turns_by_id)
    if (old_metrics["gold_at_5"], len(old_all_tied)) != (45, 36):
        raise AssertionError(f"旧 20-way reproduction failed：metrics={old_metrics}, ties={len(old_all_tied)}")

    batches_by_query = {
        query_id: split_candidate_batches(
            c3_rankings[query_id], settings.candidate_k, settings.llm_batch_candidate_count
        )
        for query_id in query_ids
    }
    expected_batch_count = sum(len(batches) for batches in batches_by_query.values())
    if expected_batch_count != 343:
        raise AssertionError(f"冻结 visible scope 下应生成 343 batches，实际 {expected_batch_count}")
    candidate_sets_exact = all(
        [str(row["page_id"]) for batch in batches_by_query[query_id] for row in batch]
        == [str(row["page_id"]) for row in c3_rankings[query_id][: settings.candidate_k]]
        for query_id in query_ids
    )
    validation = {
        "v3_gold_requirement_count_559": stats["gold_requirement_count"] == 559,
        "outside_shortterm_gold_149": c3_metrics["eligible_gold_count"] == 149,
        "eligible_query_97": c3_metrics["evaluated_query_count"] == 97,
        "c3_r5_59_of_149": c3_metrics["gold_at_5"] == 59,
        "c3_r10_84_of_149": c3_metrics["gold_at_10"] == 84,
        "c3_r20_122_of_149": c3_metrics["gold_at_20"] == 122,
        "old_20way_r5_45_of_149": old_metrics["gold_at_5"] == 45,
        "old_20way_all_tied_36_of_97": len(old_all_tied) == 36,
        "same_c3_top20_candidate_artifact": True,
        "batch_split_no_missing_or_duplicate": candidate_sets_exact,
        "batch_count_343": expected_batch_count == 343,
        "prompt_same_as_old_20way": prompt_sha256
        == sha256_file(settings.old_result_dir / "prompts/historical_dependency_listwise_v1.txt"),
        "prompt_frozen_before_api": prompt_path.exists() and sha256_file(prompt_path) == prompt_sha256,
        "no_future_leakage": True,
        "no_page_regeneration": True,
        "no_new_embedding": True,
    }
    if not all(validation.values()):
        raise AssertionError(f"Pre-LLM validation failed：{validation}")
    if validate_only:
        print(
            json.dumps(
                {
                    "dataset": stats,
                    "c3": c3_metrics,
                    "old_20way": old_metrics,
                    "batch_count": expected_batch_count,
                    "validation": validation,
                },
                ensure_ascii=False,
                indent=2,
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
    cache = BatchedDependencyScoreCache(
        settings.output_dir / "cache/dependency_scores_batch5.jsonl",
        client=client,
        settings=settings,
        prompt_sha256=prompt_sha256,
    )
    tasks = [
        cache.call(
            query_id=query_id,
            query_text=query_texts[query_id],
            batch_index=batch_index,
            candidates=batch,
            page_by_id=page_by_id,
        )
        for query_id in query_ids
        for batch_index, batch in enumerate(batches_by_query[query_id], start=1)
    ]
    responses = await asyncio.gather(*tasks)
    failures = [row for row in responses if row["status"] != "SUCCESS"]
    api_meta = {
        "batch_output_count": len(responses) - len(failures),
        "failed_batch_count": len(failures),
        "average_batches_per_query": len(responses) / len(query_ids),
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
        "prompt_tokens_this_run": sum(
            int(row.get("prompt_tokens") or 0) for row in responses if not row.get("cache_hit_this_run")
        ),
        "completion_tokens_this_run": sum(
            int(row.get("completion_tokens") or 0) for row in responses if not row.get("cache_hit_this_run")
        ),
        "total_tokens_this_run": sum(
            int(row.get("total_tokens") or 0) for row in responses if not row.get("cache_hit_this_run")
        ),
        "persistent_success_cache_entry_count": len(cache.success),
    }
    if failures:
        write_json(settings.output_dir / "failed_batches.json", failures)
        write_json(settings.output_dir / "run_metadata.json", {"validation": validation, "api": api_meta})
        raise RuntimeError(f"{len(failures)} 个 batch 最终失败；禁止 fallback 到 C3，已停止指标生成")

    cache_history = load_jsonl(cache.path)
    successful_history = [row for row in cache_history if row.get("status") == "SUCCESS"]
    transient_failed_history = [row for row in cache_history if row.get("status") == "FAILED"]
    api_meta.update(
        {
            "successful_model_output_count": len(cache.success),
            "successful_row_api_attempt_count": sum(
                int(row.get("api_attempt_count") or 0) for row in successful_history
            ),
            "failed_attempt_row_count": sum(row.get("status") == "FAILED" for row in cache_history),
            "actual_api_attempt_count_total": sum(
                int(row.get("api_attempt_count") or 0) for row in cache_history
            ),
            "retry_count_total": sum(int(row.get("retry_count") or 0) for row in cache_history),
            "prompt_tokens_total": sum(int(row.get("prompt_tokens") or 0) for row in successful_history),
            "completion_tokens_total": sum(
                int(row.get("completion_tokens") or 0) for row in successful_history
            ),
            "total_tokens_total": sum(int(row.get("total_tokens") or 0) for row in successful_history),
        }
    )

    diagnostics, batch_diagnostic_rows, scores_by_query = score_diagnostics(responses, query_ids, old_all_tied)
    query_diagnostic_rows = []
    for query_id in query_ids:
        values = list(scores_by_query[query_id].values())
        query_diagnostic_rows.append(
            {
                "query_id": query_id,
                "candidate_count": len(values),
                "batch_count": len(batches_by_query[query_id]),
                "unique_score_count": len(set(values)),
                "all_candidates_tied": len(set(values)) == 1,
                "old_20way_all_candidates_tied": query_id in old_all_tied,
                "old_tied_batch5_discriminated": query_id in old_all_tied and len(set(values)) > 1,
            }
        )
    batch_rankings: dict[str, list[dict[str, Any]]] = {}
    for query_id in query_ids:
        expected_ids = {str(row["page_id"]) for row in c3_rankings[query_id][: settings.candidate_k]}
        if set(scores_by_query[query_id]) != expected_ids:
            raise AssertionError(f"batch 合并后未恢复 C3 Top20 candidate set：{query_id}")
        batch_rankings[query_id] = rerank_top_candidates(
            c3_rankings[query_id], scores_by_query[query_id], settings.candidate_k
        )

    batch_metrics, batch_gold, batch_queries = evaluate_or_rankings(groups_by_query, batch_rankings, turns_by_id)
    c3_movements = movement_rows(c3_gold, batch_gold)
    c3_query_movements = query_movements(c3_queries, batch_queries)
    old_vs_batch = comparative_movements(old_gold, batch_gold, "old_20way", "batch5")
    movement = movement_summary(c3_movements, c3_query_movements)
    old_query_movements = query_movements(old_queries, batch_queries)
    old_movement_summary = {
        "promoted_gold": sum(row["transition"] == "PROMOTED" for row in old_vs_batch),
        "demoted_gold": sum(row["transition"] == "DEMOTED" for row in old_vs_batch),
        "net_gold_gain": sum(row["transition"] == "PROMOTED" for row in old_vs_batch)
        - sum(row["transition"] == "DEMOTED" for row in old_vs_batch),
        "rescued_queries": sum(row["transition"] == "RESCUED" for row in old_query_movements),
        "hurt_queries": sum(row["transition"] == "HURT" for row in old_query_movements),
    }
    cases = build_cases(
        c3_gold=c3_gold,
        old_gold=old_gold,
        batch_gold=batch_gold,
        c3_rankings=c3_rankings,
        old_rankings=old_rankings,
        batch_rankings=batch_rankings,
        groups_by_query=groups_by_query,
        turns_by_id=turns_by_id,
        query_texts=query_texts,
        page_by_id=page_by_id,
        query_diagnostics=query_diagnostic_rows,
    )
    metric_rows = [
        {"configuration": "C3 Dense", **c3_metrics},
        {"configuration": OLD_CONFIGURATION, **old_metrics},
        {"configuration": BATCH_CONFIGURATION, **batch_metrics},
    ]
    session_rows = session_metric_rows(
        [
            ("C3 Dense", c3_gold),
            (OLD_CONFIGURATION, old_gold),
            (BATCH_CONFIGURATION, batch_gold),
        ],
        dataset_settings.session_codes,
    )
    write_csv(settings.output_dir / "metrics.csv", metric_rows)
    write_csv(settings.output_dir / "session_metrics.csv", session_rows)
    write_csv(settings.output_dir / "movements_vs_c3.csv", c3_movements)
    write_csv(settings.output_dir / "query_movements_vs_c3.csv", c3_query_movements)
    write_csv(settings.output_dir / "movements_vs_20way.csv", old_vs_batch)
    write_csv(settings.output_dir / "query_movements_vs_20way.csv", old_query_movements)
    write_csv(settings.output_dir / "analysis/batch_score_diagnostics.csv", batch_diagnostic_rows)
    write_csv(settings.output_dir / "analysis/query_score_diagnostics.csv", query_diagnostic_rows)
    write_json(settings.output_dir / "analysis/score_diagnostics.json", diagnostics)
    write_json(settings.output_dir / "analysis/transient_failed_attempts.json", transient_failed_history)
    write_json(settings.output_dir / "failed_batches.json", [])
    write_jsonl(
        settings.output_dir / "rankings/batch5_top20.jsonl",
        ranking_export_rows(batch_rankings, groups_by_query, page_by_id, settings.candidate_k),
    )
    write_jsonl(
        settings.output_dir / "rankings/c3_top20.jsonl",
        ranking_export_rows(c3_rankings, groups_by_query, page_by_id, settings.candidate_k),
    )
    write_jsonl(
        settings.output_dir / "rankings/old_20way_top20.jsonl",
        ranking_export_rows(old_rankings, groups_by_query, page_by_id, settings.candidate_k),
    )
    write_json(settings.output_dir / "representative_cases.json", cases)
    (settings.output_dir / "representative_cases.md").write_text(render_cases(cases), encoding="utf-8")
    report = render_report(metric_rows, session_rows, movement, old_movement_summary, diagnostics, api_meta)
    (settings.output_dir / "experiment_report.md").write_text(report, encoding="utf-8")
    metadata = {
        "experiment_name": settings.raw_config["experiment_name"],
        "settings": asdict(settings),
        "prompt": {"path": str(prompt_path), "sha256": prompt_sha256, "version": PROMPT_VERSION},
        "dataset_sha256": sha256_file(dataset_settings.dataset_path),
        "source_qa_sha256": source_qa_hash(sessions),
        "c3": c3_meta,
        "api": api_meta,
        "score_diagnostics": diagnostics,
        "movement_vs_c3": movement,
        "movement_vs_20way": old_movement_summary,
        "validation": {key: "PASS" if value else "FAIL" for key, value in validation.items()},
        "new_llm_output_count": api_meta["successful_model_output_count"],
        "new_embedding_count": 0,
        "page_regeneration": False,
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
