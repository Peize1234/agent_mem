"""Compare three small zero-shot rerankers on the frozen C3 Top20.

The experiment is intentionally narrow: frozen P2 queries and C3 Page texts are
scored by one cross encoder at a time, with batch size 1 and max length 512.
Only the C3 Top20 is reordered. No LLM, embedding, BM25, fusion, or production
code path is invoked.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import re
import subprocess
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch

from exp.benchmark.benchmark_common import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import stable_hash, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import compare_rankings  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    SESSION_CODES,
    evaluate_all,
    rank_configuration,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_small_reranker_ablation"
TOP_K = 20
MAX_LENGTH = 512
BATCH_SIZE = 1
EXPECTED_QUERY_COUNT = 99
EXPECTED_PAGE_COUNT = 333
EXPECTED_ELIGIBLE_GOLD = 154
EXPECTED_C3_GOLD5 = 59
EXPECTED_C3_R5 = EXPECTED_C3_GOLD5 / EXPECTED_ELIGIBLE_GOLD
QWEN_INSTRUCTION = (
    "Determine whether the candidate historical memory contains information actually needed to answer the current "
    "query. Prioritize historical dependency, referenced prior conclusions, evidence continuity, and required "
    "context over general topical similarity."
)
DEPENDENCY_PATTERN = re.compile(r"上一轮|前面|刚才|前序|前文|上一问|反证|反例|修订|承接|证据链|主结论")
INDICATORS = (
    "营业收入",
    "归母净利润",
    "扣非归母净利润",
    "经营活动现金流",
    "现金流",
    "总资产",
    "归母股东权益",
    "股东权益",
    "利润",
    "杠杆",
    "周转",
)


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model_id: str
    label: str
    family: str
    trust_remote_code: bool = False
    prompt_name: str | None = None


MODEL_SPECS = (
    ModelSpec(
        key="GTE",
        model_id="Alibaba-NLP/gte-multilingual-reranker-base",
        label="Alibaba-NLP/gte-multilingual-reranker-base",
        family="generic_cross_encoder",
        trust_remote_code=True,
    ),
    ModelSpec(
        key="BGE",
        model_id="BAAI/bge-reranker-v2-m3",
        label="BAAI/bge-reranker-v2-m3",
        family="generic_cross_encoder",
    ),
    ModelSpec(
        key="QWEN",
        model_id="Qwen/Qwen3-Reranker-0.6B",
        label="Qwen/Qwen3-Reranker-0.6B",
        family="instruction_aware_cross_encoder",
        prompt_name="memory_dependency",
    ),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen C3 small zero-shot reranker ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--require-cache-hit",
        action="store_true",
        help="Fail if any successful model pair score would require model inference.",
    )
    parser.add_argument("--models", nargs="*", choices=[spec.key for spec in MODEL_SPECS])
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_path_status(pathspec: str) -> list[str]:
    process = subprocess.run(
        ["git", "status", "--short", "--", pathspec],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in process.stdout.splitlines() if line.strip()]


def write_frozen_instruction(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "prompts/Qwen3_memory_dependency_instruction.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = QWEN_INSTRUCTION + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != expected:
        raise AssertionError(f"Frozen Qwen instruction differs: {path}")
    path.write_text(expected, encoding="utf-8")
    return {"path": str(path), "sha256_before_first_inference": sha256_file(path)}


def all_queries(snapshots: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(query) for code in SESSION_CODES for query in snapshots[code]["queries"]]


def expected_pairs(
    queries: Sequence[Mapping[str, Any]],
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    query_texts: Mapping[str, str],
    page_texts: Mapping[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["query_id"])
        for candidate in dense[query_id][:TOP_K]:
            page_id = str(candidate["page_id"])
            rows.append(
                {
                    "session_id": str(query["session_code"]),
                    "query_id": query_id,
                    "page_id": page_id,
                    "source_turn_id": str(candidate["source_turn_id"]),
                    "dense_rank": int(candidate["rank"]),
                    "dense_score": float(candidate["score"]),
                    "query_text": str(query_texts[query_id]),
                    "page_text": str(page_texts[page_id]),
                }
            )
    return rows


def cache_identity(spec: ModelSpec, pair: Mapping[str, Any], revision: str, instruction_sha: str) -> dict[str, Any]:
    return {
        "model": spec.model_id,
        "model_revision": revision,
        "query_id": pair["query_id"],
        "page_id": pair["page_id"],
        "query_sha256": sha256_text(str(pair["query_text"])),
        "page_sha256": sha256_text(str(pair["page_text"])),
        "candidate_dense_rank": int(pair["dense_rank"]),
        "candidate_k": TOP_K,
        "max_length": MAX_LENGTH,
        "batch_size": BATCH_SIZE,
        "device": "cuda",
        "dtype": "float16",
        "quantization": "none",
        "cpu_offload": False,
        "family": spec.family,
        "prompt_name": spec.prompt_name,
        "instruction_sha256": instruction_sha if spec.prompt_name else None,
        "score_contract": "sentence-transformers CrossEncoder official predict output; descending relevance",
    }


class ScoreCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if row.get("status") == "SUCCESS":
                        self.rows[str(row["cache_key"])] = row
        self.hit_count = 0
        self.miss_count = 0

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.rows.get(key)
        if row is not None:
            self.hit_count += 1
        else:
            self.miss_count += 1
        return row

    def append(self, row: Mapping[str, Any]) -> None:
        value = dict(row)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.rows[str(value["cache_key"])] = value


def load_status(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_status(path: Path, value: Mapping[str, Any]) -> None:
    dump_json(path, dict(value))


def snapshot_revision(snapshot_path: Path) -> str:
    if snapshot_path.parent.name == "snapshots":
        return snapshot_path.name
    return "unresolved"


def download_snapshot(model_id: str) -> Path:
    from huggingface_hub import snapshot_download

    # This workstation exports HF_ENDPOINT to a mirror that may lag model-card
    # metadata. Pin the official endpoint explicitly so the official model
    # contract and already-downloaded blobs are assembled into one snapshot.
    return Path(snapshot_download(repo_id=model_id, endpoint="https://huggingface.co"))


def cuda_hardware() -> dict[str, Any]:
    if not torch.cuda.is_available():
        return {"available": False}
    properties = torch.cuda.get_device_properties(0)
    return {
        "available": True,
        "device_name": properties.name,
        "total_memory_bytes": int(properties.total_memory),
        "total_memory_gib": properties.total_memory / 1024**3,
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
    }


def is_oom(error: BaseException) -> bool:
    return isinstance(error, torch.cuda.OutOfMemoryError) or "out of memory" in str(error).lower()


def unload_model(model: Any | None) -> None:
    if model is not None:
        del model
    gc.collect()
    if torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except torch.AcceleratorError:
            # A device-side assertion invalidates the process CUDA context.
            # Model invocations are isolated by the experiment runner.
            pass


def load_cross_encoder(spec: ModelSpec, snapshot: Path, instruction: str) -> Any:
    from sentence_transformers import CrossEncoder

    kwargs: dict[str, Any] = {
        "model_name_or_path": str(snapshot),
        "device": "cuda",
        "trust_remote_code": spec.trust_remote_code,
        "model_kwargs": {"torch_dtype": torch.float16},
        "max_length": MAX_LENGTH,
    }
    if spec.prompt_name:
        kwargs.update(
            {
                "prompts": {spec.prompt_name: instruction},
                "default_prompt_name": spec.prompt_name,
            }
        )
    model = CrossEncoder(**kwargs)
    if spec.key == "GTE":
        # transformers 5 does not initialize this non-persistent remote-code
        # buffer when loading the transformers-4.39 checkpoint. Restore the
        # exact arange buffer defined by the official model implementation.
        embeddings = model.model.new.embeddings
        device = embeddings.word_embeddings.weight.device
        embeddings.register_buffer(
            "position_ids",
            torch.arange(model.model.config.max_position_embeddings, device=device),
            persistent=False,
        )
    return model


def score_pair(model: Any, spec: ModelSpec, query: str, page: str) -> float:
    kwargs: dict[str, Any] = {
        "batch_size": BATCH_SIZE,
        "show_progress_bar": False,
        "convert_to_numpy": True,
    }
    if spec.prompt_name:
        kwargs["prompt_name"] = spec.prompt_name
    values = model.predict([(query, page)], **kwargs)
    return float(np.asarray(values).reshape(-1)[0])


def run_model(
    spec: ModelSpec,
    pairs: Sequence[Mapping[str, Any]],
    output_dir: Path,
    instruction: str,
    instruction_sha: str,
    *,
    retry_failed: bool,
    require_cache_hit: bool,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    cache_path = output_dir / f"cache/scores/{spec.key.lower()}.jsonl"
    status_path = output_dir / f"cache/model_status/{spec.key.lower()}.json"
    prior = load_status(status_path)
    if prior and prior.get("status") in {"OOM", "ERROR"} and not retry_failed:
        return {}, {**prior, "failure_status_reused": True}

    revision = str((prior or {}).get("model_revision") or "")
    cache = ScoreCache(cache_path)
    cached_scores: dict[str, dict[str, Any]] = {}
    missing: list[tuple[Mapping[str, Any], dict[str, Any], str]] = []
    if revision:
        for pair in pairs:
            identity = cache_identity(spec, pair, revision, instruction_sha)
            key = stable_hash(identity)
            row = cache.get(key)
            if row is None:
                missing.append((pair, identity, key))
            else:
                cached_scores[f"{pair['query_id']}::{pair['page_id']}"] = row
        if not missing:
            return cached_scores, {
                **(prior or {}),
                "status": "SUCCESS",
                "pair_count": len(pairs),
                "cache_hit_count_this_run": len(pairs),
                "new_score_count_this_run": 0,
                "model_loaded_this_run": False,
                "all_scores_reused": True,
            }
    if require_cache_hit:
        raise AssertionError(f"{spec.model_id}: missing cached scores")
    if not torch.cuda.is_available():
        status = {
            "model": spec.model_id,
            "status": "ERROR",
            "failure_stage": "precondition",
            "error": "CUDA is unavailable; CPU execution is forbidden by this experiment.",
        }
        save_status(status_path, status)
        return {}, status

    model = None
    started = time.perf_counter()
    try:
        snapshot = download_snapshot(spec.model_id)
        revision = snapshot_revision(snapshot)
        # Rebuild cache identities now that the immutable revision is known.
        cached_scores = {}
        missing = []
        for pair in pairs:
            identity = cache_identity(spec, pair, revision, instruction_sha)
            key = stable_hash(identity)
            row = cache.get(key)
            if row is None:
                missing.append((pair, identity, key))
            else:
                cached_scores[f"{pair['query_id']}::{pair['page_id']}"] = row
        if not missing:
            status = {
                "model": spec.model_id,
                "model_revision": revision,
                "status": "SUCCESS",
                "pair_count": len(pairs),
                "cache_hit_count_this_run": len(pairs),
                "new_score_count_this_run": 0,
                "model_loaded_this_run": False,
                "all_scores_reused": True,
            }
            save_status(status_path, status)
            return cached_scores, status

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
        load_started = time.perf_counter()
        model = load_cross_encoder(spec, snapshot, instruction)
        load_seconds = time.perf_counter() - load_started
        for index, (pair, identity, key) in enumerate(missing, start=1):
            score_started = time.perf_counter()
            score = score_pair(model, spec, str(pair["query_text"]), str(pair["page_text"]))
            row = {
                "cache_key": key,
                "identity": identity,
                "query_id": pair["query_id"],
                "page_id": pair["page_id"],
                "dense_rank": pair["dense_rank"],
                "reranker_score": score,
                "inference_seconds": time.perf_counter() - score_started,
                "status": "SUCCESS",
            }
            cache.append(row)
            cached_scores[f"{pair['query_id']}::{pair['page_id']}"] = row
            if index % 100 == 0:
                print(f"[{spec.key}] scored {index}/{len(missing)} new pairs", flush=True)
        status = {
            "model": spec.model_id,
            "model_revision": revision,
            "snapshot_path": str(snapshot),
            "status": "SUCCESS",
            "family": spec.family,
            "official_contract": "sentence-transformers CrossEncoder query-document relevance scoring",
            "prompt_name": spec.prompt_name,
            "instruction_sha256": instruction_sha if spec.prompt_name else None,
            "device": "cuda",
            "dtype": "float16",
            "batch_size": BATCH_SIZE,
            "max_length": MAX_LENGTH,
            "quantization": "none",
            "cpu_offload": False,
            "pair_count": len(pairs),
            "cache_hit_count_this_run": len(pairs) - len(missing),
            "new_score_count_this_run": len(missing),
            "model_loaded_this_run": True,
            "all_scores_reused": False,
            "load_seconds": load_seconds,
            "run_seconds": time.perf_counter() - started,
            "peak_cuda_memory_bytes": int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else None,
        }
        save_status(status_path, status)
        return cached_scores, status
    except BaseException as error:
        status_name = "OOM" if is_oom(error) else "ERROR"
        status = {
            "model": spec.model_id,
            "model_revision": revision or None,
            "status": status_name,
            "failure_stage": "load_or_inference",
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
            "device": "cuda",
            "dtype": "float16",
            "batch_size": BATCH_SIZE,
            "max_length": MAX_LENGTH,
            "quantization": "none",
            "cpu_offload": False,
            "run_seconds": time.perf_counter() - started,
            "peak_cuda_memory_bytes": int(torch.cuda.max_memory_allocated()) if torch.cuda.is_available() else None,
        }
        save_status(status_path, status)
        return {}, status
    finally:
        unload_model(model)


def rerank_top20(
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    scores: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for query_id, dense_ranking in dense.items():
        head = []
        for item in dense_ranking[:TOP_K]:
            page_id = str(item["page_id"])
            cache_row = scores[f"{query_id}::{page_id}"]
            head.append(
                {
                    **dict(item),
                    "dense_rank": int(item["rank"]),
                    "dense_score": float(item["score"]),
                    "reranker_score": float(cache_row["reranker_score"]),
                }
            )
        head.sort(key=lambda row: (-float(row["reranker_score"]), int(row["dense_rank"]), str(row["page_id"])))
        tail = [
            {
                **dict(item),
                "dense_rank": int(item["rank"]),
                "dense_score": float(item["score"]),
                "reranker_score": None,
            }
            for item in dense_ranking[TOP_K:]
        ]
        ranking = head + tail
        for rank, item in enumerate(ranking, start=1):
            item["rank"] = rank
            item["score"] = item["reranker_score"] if item["reranker_score"] is not None else item["dense_score"]
        result[query_id] = ranking
    return result


def validate_reranked_scope(
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    reranked: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    candidate_set_mismatches = []
    tail_order_mismatches = []
    for query_id, dense_ranking in dense.items():
        dense_top = {str(row["page_id"]) for row in dense_ranking[:TOP_K]}
        reranked_top = {str(row["page_id"]) for row in reranked[query_id][:TOP_K]}
        if dense_top != reranked_top:
            candidate_set_mismatches.append(query_id)
        dense_tail = [str(row["page_id"]) for row in dense_ranking[TOP_K:]]
        reranked_tail = [str(row["page_id"]) for row in reranked[query_id][TOP_K:]]
        if dense_tail != reranked_tail:
            tail_order_mismatches.append(query_id)
    return {
        "status": "PASS" if not candidate_set_mismatches and not tail_order_mismatches else "FAIL",
        "candidate_k": TOP_K,
        "candidate_set_mismatch_query_ids": candidate_set_mismatches,
        "tail_order_mismatch_query_ids": tail_order_mismatches,
    }


def metric_row(label: str, metrics: Mapping[str, Any], movement: Mapping[str, Any] | None = None) -> dict[str, Any]:
    eligible = int(metrics["eligible_gold_count"])
    value = {
        "Model": label,
        "Status": "SUCCESS",
        "Micro R@5": float(metrics["recall_at_5"]),
        "Gold@5": round(float(metrics["recall_at_5"]) * eligible),
        "Eligible Gold": eligible,
        "Macro R@5": float(metrics["macro_session_r5"]),
        "R@10": float(metrics["recall_at_10"]),
        "R@20": float(metrics["recall_at_20"]),
        "MRR": float(metrics["mrr"]),
        "Mean Gold Rank": float(metrics["mean_gold_rank"]),
    }
    if movement:
        value.update(
            {
                "Promoted Gold": int(movement["Promoted Gold"]),
                "Demoted Gold": int(movement["Demoted Gold"]),
                "Net Gold gain": int(movement["Net Gold gain"]),
                "Rescued Queries": int(movement["Rescued Queries"]),
                "Hurt Queries": int(movement["Hurt Queries"]),
                "Preserved C3 Top5 Gold": EXPECTED_C3_GOLD5 - int(movement["Demoted Gold"]),
                "C3 rank 6-20 -> reranker Top5 Gold": int(movement["Promoted Gold"]),
                "C3 rank <=5 -> reranker >5 Gold": int(movement["Demoted Gold"]),
            }
        )
    else:
        value.update(
            {
                "Promoted Gold": 0,
                "Demoted Gold": 0,
                "Net Gold gain": 0,
                "Rescued Queries": 0,
                "Hurt Queries": 0,
                "Preserved C3 Top5 Gold": EXPECTED_C3_GOLD5,
                "C3 rank 6-20 -> reranker Top5 Gold": 0,
                "C3 rank <=5 -> reranker >5 Gold": 0,
            }
        )
    return value


def session_rows(label: str, sessions: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Model": label,
            "Session": code,
            "Eligible Gold": int(sessions[code]["eligible_gold_count"]),
            "R@5": float(sessions[code]["recall_at_5"]),
            "R@10": float(sessions[code]["recall_at_10"]),
            "R@20": float(sessions[code]["recall_at_20"]),
            "MRR": float(sessions[code]["mrr"]),
            "Mean Gold Rank": float(sessions[code]["mean_gold_rank"]),
        }
        for code in SESSION_CODES
    ]


def dependency_subset_rows(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    selected = [query for query in queries if DEPENDENCY_PATTERN.search(str(query["original_query"]))]
    rows = []
    for label, ranking in rankings.items():
        gold_count = hit_count = 0
        for query in selected:
            query_id = str(query["query_id"])
            rank_by_page = rank_map(ranking[query_id])
            for gold in query["eligible_gold_page_ids"]:
                gold_count += 1
                hit_count += int(int(rank_by_page[str(gold)]["rank"]) <= 5)
        rows.append(
            {
                "Model": label,
                "Dependency Query Count": len(selected),
                "Eligible Gold": gold_count,
                "Gold@5": hit_count,
                "R@5": hit_count / gold_count,
                "selection_contract": "deterministic original-query discourse/dependency marker regex; no Gold/rank used",
            }
        )
    return rows


def text_indicators(text: str) -> set[str]:
    return {term for term in INDICATORS if term in text}


def failure_category(query_text: str, gold_text: str, competitor_text: str) -> str:
    if DEPENDENCY_PATTERN.search(query_text):
        return "历史依赖关系未识别"
    gold_indicators = text_indicators(gold_text)
    competitor_indicators = text_indicators(competitor_text)
    overlap = gold_indicators & competitor_indicators
    union = gold_indicators | competitor_indicators
    if len(overlap) >= 2 or (union and len(overlap) / len(union) >= 0.5):
        return "Page 内容高度同质"
    if text_indicators(query_text) & competitor_indicators:
        return "普通语义相似误判"
    return "其他原因"


def case_payload(
    *,
    case_type: str,
    query: Mapping[str, Any],
    gold_page_id: str,
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    reranked: Mapping[str, Sequence[Mapping[str, Any]]],
    page_by_id: Mapping[str, Mapping[str, Any]],
    query_text: str,
) -> dict[str, Any]:
    query_id = str(query["query_id"])
    dense_map = rank_map(dense[query_id])
    rerank_map = rank_map(reranked[query_id])
    gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
    competitor = next(row for row in reranked[query_id] if str(row["page_id"]) not in gold_ids)
    competitor_id = str(competitor["page_id"])
    gold_page = page_by_id[gold_page_id]
    competitor_page = page_by_id[competitor_id]
    return {
        "case_type": case_type,
        "session_id": query["session_code"],
        "query_id": query_id,
        "original_query": query["original_query"],
        "p2_query": query_text,
        "gold_page": {
            "page_id": gold_page_id,
            "source_turn_id": gold_page["source_turn_id"],
            "summary": gold_page["summary"],
            "keywords": gold_page["keywords"],
            "c3_rank": int(dense_map[gold_page_id]["rank"]),
            "reranker_rank": int(rerank_map[gold_page_id]["rank"]),
            "reranker_score": rerank_map[gold_page_id].get("reranker_score"),
        },
        "highest_ranked_non_gold_page": {
            "page_id": competitor_id,
            "source_turn_id": competitor_page["source_turn_id"],
            "summary": competitor_page["summary"],
            "keywords": competitor_page["keywords"],
            "c3_rank": int(dense_map[competitor_id]["rank"]),
            "reranker_rank": int(rerank_map[competitor_id]["rank"]),
            "reranker_score": rerank_map[competitor_id].get("reranker_score"),
        },
        "failure_category": failure_category(query_text, str(gold_page["no_user_text"]), str(competitor_page["no_user_text"])),
    }


def representative_cases(
    queries: Sequence[Mapping[str, Any]],
    dense: Mapping[str, Sequence[Mapping[str, Any]]],
    reranked: Mapping[str, Sequence[Mapping[str, Any]]],
    page_by_id: Mapping[str, Mapping[str, Any]],
    query_texts: Mapping[str, str],
) -> list[dict[str, Any]]:
    query_by_id = {str(row["query_id"]): row for row in queries}
    movements = []
    for query in queries:
        query_id = str(query["query_id"])
        dense_map = rank_map(dense[query_id])
        rerank_map = rank_map(reranked[query_id])
        for gold in query["eligible_gold_page_ids"]:
            gold_id = str(gold)
            before = int(dense_map[gold_id]["rank"])
            after = int(rerank_map[gold_id]["rank"])
            movements.append((query_id, gold_id, before, after))
    promoted = sorted(
        (row for row in movements if row[2] > 5 and row[3] <= 5),
        key=lambda row: (-(row[2] - row[3]), row[0], row[1]),
    )[:3]
    demoted = sorted(
        (row for row in movements if row[2] <= 5 and row[3] > 5),
        key=lambda row: (-(row[3] - row[2]), row[0], row[1]),
    )[:3]
    failed = sorted(
        (row for row in movements if row[3] > 5),
        key=lambda row: (row[3], row[0], row[1]),
    )[:3]
    result = []
    for case_type, rows in (("PROMOTED", promoted), ("DEMOTED", demoted), ("STILL_FAILED_HARD_NEGATIVE", failed)):
        for query_id, gold_id, _, _ in rows:
            result.append(
                case_payload(
                    case_type=case_type,
                    query=query_by_id[query_id],
                    gold_page_id=gold_id,
                    dense=dense,
                    reranked=reranked,
                    page_by_id=page_by_id,
                    query_text=query_texts[query_id],
                )
            )
    return result


def render_cases(cases_by_model: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    lines = ["# 小型 Zero-shot Reranker 代表案例", ""]
    for model, cases in cases_by_model.items():
        lines.extend([f"## {model}", ""])
        if not cases:
            lines.extend(["模型未成功完成，因此没有案例。", ""])
            continue
        for case in cases:
            gold = case["gold_page"]
            competitor = case["highest_ranked_non_gold_page"]
            lines.extend(
                [
                    f"### {case['case_type']} · {case['query_id']} · Gold {gold['source_turn_id']}",
                    "",
                    "Query：",
                    "",
                    str(case["p2_query"]),
                    "",
                    f"事实型诊断标签：{case['failure_category']}。",
                    "",
                    f"Gold Page（C3 #{gold['c3_rank']} → Reranker #{gold['reranker_rank']}，score={gold['reranker_score']}）：",
                    "",
                    str(gold["summary"]),
                    "",
                    f"Keywords: {', '.join(gold['keywords'])}",
                    "",
                    f"最高 Non-Gold（C3 #{competitor['c3_rank']} → Reranker #{competitor['reranker_rank']}，score={competitor['reranker_score']}）：",
                    "",
                    str(competitor["summary"]),
                    "",
                    f"Keywords: {', '.join(competitor['keywords'])}",
                    "",
                ]
            )
    return "\n".join(lines)


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    statuses: Mapping[str, Mapping[str, Any]],
    dependency: Sequence[Mapping[str, Any]],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    lines = ["# MidTerm 小型 Zero-shot Reranker 对比实验", "", "## 主结果", ""]
    lines.append("| 模型 | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Promoted | Demoted | Net |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in metrics:
        if row["Status"] != "SUCCESS":
            lines.append(f"| {row['Model']} | {row['Status']} |  |  |  |  |  |  |  |  |")
            continue
        lines.append(
            f"| {row['Model']} | {100*float(row['Micro R@5']):.2f}% | {row['Gold@5']}/154 | "
            f"{100*float(row['Macro R@5']):.2f}% | {100*float(row['R@10']):.2f}% | "
            f"{100*float(row['R@20']):.2f}% | {float(row['MRR']):.4f} | "
            f"{row['Promoted Gold']} | {row['Demoted Gold']} | {int(row['Net Gold gain']):+d} |"
        )
    lines.extend(["", "## Session R@5", ""])
    for row in sessions:
        lines.append(f"- {row['Model']} / {row['Session']}：{100*float(row['R@5']):.2f}%")
    lines.extend(["", "## Memory Dependency 子集", ""])
    for row in dependency:
        lines.append(
            f"- {row['Model']}：{row['Gold@5']}/{row['Eligible Gold']}（{100*float(row['R@5']):.2f}%）"
        )
    lines.extend(["", "## 运行状态", ""])
    for key, status in statuses.items():
        lines.append(
            f"- {key}：{status.get('status')}；new_scores={status.get('new_score_count_this_run', 0)}；"
            f"cache_hits={status.get('cache_hit_count_this_run', 0)}；peak_cuda_bytes={status.get('peak_cuda_memory_bytes')}."
        )
        if status.get("status") != "SUCCESS":
            lines.append(f"  - {status.get('error')}")
    lines.extend(["", "## Validation", ""])
    for name, value in validations.items():
        lines.append(f"- {name}: {value['status']}")
    lines.extend(
        [
            "",
            "## 实验边界",
            "",
            "- 仅重排冻结 C3 Dense Top20；Top20 外 Page 顺序保持 C3 不变。",
            "- batch_size=1、max_length=512、FP16；无 CPU offload、量化、BM25、融合或参数搜索。",
            "- Qwen instruction 在推理前冻结；P2 Query、C3 Page、Gold 与 visibility 均来自原 artifact。",
            "",
            "详细案例见 `representative_cases.md`。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    instruction_meta = write_frozen_instruction(args.output_dir)
    instruction_sha = str(instruction_meta["sha256_before_first_inference"])
    mem0_status_before = git_path_status("mem0")

    snapshots, old_pages, queries, checkpoints, checkpoint_cache = checkpoint_inputs()
    c3 = checkpoints["C3"]
    dense = rank_configuration(snapshots, c3["query_vectors"], c3["page_vectors"])
    dense_metrics, dense_sessions = evaluate_all(snapshots, dense)
    eligible_gold = sum(len(query["eligible_gold_page_ids"]) for query in queries)
    page_ids = {str(page["page_id"]) for page in old_pages}
    query_ids = {str(query["query_id"]) for query in queries}
    pairs = expected_pairs(queries, dense, c3["query_texts"], c3["page_texts"])
    visible_by_query = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    query_by_id = {str(query["query_id"]): query for query in queries}
    prior_frozen = json.loads(
        (REPO_ROOT / "exp/results/midterm_bge_m3_multivector_late_interaction/run_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    # The earlier frozen C3 experiment hashes text lists in benchmark order.
    frozen_query_hash = stable_hash([str(c3["query_texts"][str(query["query_id"])]) for query in queries])
    frozen_page_hash = stable_hash([str(c3["page_texts"][str(page["page_id"])]) for page in old_pages])

    validations: dict[str, dict[str, Any]] = {
        "query_count": {"status": "PASS" if len(queries) == EXPECTED_QUERY_COUNT else "FAIL", "actual": len(queries)},
        "page_count": {"status": "PASS" if len(old_pages) == EXPECTED_PAGE_COUNT else "FAIL", "actual": len(old_pages)},
        "eligible_gold": {
            "status": "PASS" if eligible_gold == EXPECTED_ELIGIBLE_GOLD else "FAIL",
            "actual": eligible_gold,
        },
        "c3_baseline": {
            "status": "PASS"
            if math.isclose(float(dense_metrics["recall_at_5"]), EXPECTED_C3_R5, abs_tol=1e-12)
            else "FAIL",
            "micro_r5": dense_metrics["recall_at_5"],
            "gold_at_5": round(float(dense_metrics["recall_at_5"]) * eligible_gold),
        },
        "p2_query_coverage": {
            "status": "PASS"
            if set(c3["query_texts"]) == query_ids and frozen_query_hash == prior_frozen["query_text_hash"]
            else "FAIL",
            "query_texts_sha256": frozen_query_hash,
            "expected_sha256": prior_frozen["query_text_hash"],
        },
        "c3_page_coverage": {
            "status": "PASS"
            if set(c3["page_texts"]) == page_ids and frozen_page_hash == prior_frozen["page_text_hash"]
            else "FAIL",
            "page_texts_sha256": frozen_page_hash,
            "expected_sha256": prior_frozen["page_text_hash"],
            "raw_user_in_page": False,
        },
        "top20_candidate_source": {
            "status": "PASS"
            if all(
                len(dense[query_id][:TOP_K]) == min(TOP_K, len(visible_by_query[query_id]))
                and {str(row["page_id"]) for row in dense[query_id][:TOP_K]}.issubset(set(visible_by_query[query_id]))
                for query_id in query_ids
            )
            else "FAIL",
            "pair_count": len(pairs),
            "candidate_k": TOP_K,
            "candidate_hash": stable_hash(
                {query_id: [str(row["page_id"]) for row in dense[query_id][:TOP_K]] for query_id in sorted(query_ids)}
            ),
        },
        "visibility": {
            "status": "PASS"
            if all(
                [str(row["page_id"]) for row in dense[query_id]] == visible_by_query[query_id]
                or {str(row["page_id"]) for row in dense[query_id]} == set(visible_by_query[query_id])
                for query_id in query_ids
            )
            else "FAIL",
            "mapping_sha256": stable_hash(visible_by_query),
        },
        "future_leakage": {
            "status": "PASS"
            if all(
                int(row.get("future_page_leak_count") or 0) == 0
                for code in SESSION_CODES
                for row in snapshots[code]["visibility"]
            )
            else "FAIL",
        },
        "no_llm": {"status": "PASS", "new_llm_calls": 0},
        "no_new_embedding": {"status": "PASS", "new_embedding_count": 0},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(f"Pre-inference validation failed: {validations}")

    selected_specs = [spec for spec in MODEL_SPECS if not args.models or spec.key in args.models]
    metric_rows = [metric_row("C3 BGE-small Dense", dense_metrics)]
    all_session_rows = session_rows("C3 BGE-small Dense", dense_sessions)
    rankings: dict[str, dict[str, list[dict[str, Any]]]] = {"C3 BGE-small Dense": dense}
    statuses: dict[str, dict[str, Any]] = {}
    movements: list[dict[str, Any]] = []
    gold_movements: list[dict[str, Any]] = []
    cases_by_model: dict[str, list[dict[str, Any]]] = {}

    for spec in selected_specs:
        print(f"Starting {spec.model_id}", flush=True)
        scores, status = run_model(
            spec,
            pairs,
            args.output_dir,
            QWEN_INSTRUCTION,
            instruction_sha,
            retry_failed=args.retry_failed,
            require_cache_hit=args.require_cache_hit,
        )
        statuses[spec.label] = status
        if status.get("status") != "SUCCESS":
            metric_rows.append({"Model": spec.label, "Status": status.get("status")})
            cases_by_model[spec.label] = []
            continue
        reranked = rerank_top20(dense, scores)
        scope = validate_reranked_scope(dense, reranked)
        validations[f"{spec.key}_top20_scope"] = scope
        if scope["status"] != "PASS":
            raise AssertionError(f"{spec.key} Top20 scope changed: {scope}")
        metrics, sessions = evaluate_all(snapshots, reranked)
        movement, gold_rows, query_rows = compare_rankings(
            snapshots,
            dense,
            reranked,
            comparison=f"{spec.label} vs C3",
        )
        metric_rows.append(metric_row(spec.label, metrics, movement))
        all_session_rows.extend(session_rows(spec.label, sessions))
        movements.extend({"Model": spec.label, **row} for row in query_rows)
        gold_movements.extend({"Model": spec.label, **row} for row in gold_rows)
        rankings[spec.label] = reranked
        cases_by_model[spec.label] = representative_cases(
            queries, dense, reranked, c3["page_by_id"], c3["query_texts"]
        )
        write_jsonl(args.output_dir / f"rankings/{spec.key.lower()}.jsonl", (
            {
                "model": spec.label,
                "session_id": query_by_id[query_id]["session_code"],
                "query_id": query_id,
                "ranking": ranking,
            }
            for query_id, ranking in reranked.items()
        ))

    instruction_meta["sha256_after_generation"] = sha256_file(Path(instruction_meta["path"]))
    validations["instruction_frozen"] = {
        "status": "PASS"
        if instruction_meta["sha256_before_first_inference"] == instruction_meta["sha256_after_generation"]
        else "FAIL",
        **instruction_meta,
    }
    mem0_status_after = git_path_status("mem0")
    validations["mem0_unmodified"] = {
        "status": "PASS" if mem0_status_before == mem0_status_after else "FAIL",
        "before": mem0_status_before,
        "after": mem0_status_after,
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(f"Post-inference validation failed: {validations}")

    dependency_rows = dependency_subset_rows(queries, rankings)
    write_csv(args.output_dir / "metrics.csv", metric_rows)
    write_csv(args.output_dir / "session_metrics.csv", all_session_rows)
    write_csv(args.output_dir / "movements.csv", gold_movements)
    write_csv(args.output_dir / "query_movements.csv", movements)
    write_csv(args.output_dir / "analysis/dependency_subset.csv", dependency_rows)
    dump_json(args.output_dir / "representative_cases.json", cases_by_model)
    (args.output_dir / "representative_cases.md").write_text(render_cases(cases_by_model), encoding="utf-8")
    report = render_report(metric_rows, all_session_rows, statuses, dependency_rows, validations)
    (args.output_dir / "experiment_report.md").write_text(report, encoding="utf-8")
    metadata = {
        "experiment_name": "MidTerm small zero-shot reranker ablation",
        "created_at_epoch": time.time(),
        "benchmark": {
            "sessions": list(SESSION_CODES),
            "query_count": len(queries),
            "page_count": len(old_pages),
            "eligible_gold_count": eligible_gold,
            "visibility_sha256": stable_hash(visible_by_query),
            "query_texts_sha256": stable_hash(c3["query_texts"]),
            "page_texts_sha256": stable_hash(c3["page_texts"]),
            "candidate_top20_sha256": validations["top20_candidate_source"]["candidate_hash"],
        },
        "c3": {
            "dense_model": "BAAI/bge-small-zh-v1.5",
            "query": "frozen P2 resolved_query",
            "page": "frozen C3 Summary + Keywords",
            "candidate_k": TOP_K,
            "metrics": dense_metrics,
        },
        "reranker_contract": {
            "flow": "C3 Dense Top20 -> reranker -> Top5; C3 order outside Top20",
            "batch_size": BATCH_SIZE,
            "max_length": MAX_LENGTH,
            "dtype": "float16",
            "normalization": "none; rank by each model official CrossEncoder relevance output",
            "dense_reranker_fusion": False,
            "cpu_offload": False,
            "quantization": False,
            "bm25": False,
        },
        "qwen_instruction": instruction_meta,
        "models": statuses,
        "hardware": cuda_hardware(),
        "checkpoint_cache": checkpoint_cache,
        "new_llm_calls": 0,
        "new_embedding_count": 0,
        "full_session_rerun": False,
        "summary_regeneration": False,
        "validations": validations,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)

    print("Model | Status | Micro R@5 | Macro R@5 | R@10 | R@20 | MRR | Promoted | Demoted | Net")
    for row in metric_rows:
        if row["Status"] != "SUCCESS":
            print(f"{row['Model']} | {row['Status']} | - | - | - | - | - | - | - | -")
            continue
        print(
            f"{row['Model']} | SUCCESS | {100*float(row['Micro R@5']):.2f}% | "
            f"{100*float(row['Macro R@5']):.2f}% | {100*float(row['R@10']):.2f}% | "
            f"{100*float(row['R@20']):.2f}% | {float(row['MRR']):.4f} | "
            f"{row['Promoted Gold']} | {row['Demoted Gold']} | {int(row['Net Gold gain']):+d}"
        )


if __name__ == "__main__":
    main()
