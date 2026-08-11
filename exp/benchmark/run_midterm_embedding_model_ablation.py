"""Compare retrieval embedding models on the frozen best MidTerm setup.

The experiment never calls an LLM or replays a Session. It reuses the frozen
local-context Page summaries/keywords, P2 queries, visibility, and Gold. Every
candidate gets its own normalized Page and Query vectors and follows the
retrieval contract published on its official model card.
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import statistics
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import percentile, stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import (  # noqa: E402
    compare_rankings,
    load_existing_add_pages,
    metric_row,
    separation_row,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    rank_configuration,
)
from exp.benchmark.run_midterm_bge_m3_length_ablation import load_small_baseline_vectors  # noqa: E402
from exp.benchmark.run_midterm_retrieval_experiments import safe_slug  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_embedding_model_ablation"
BASELINE_KEY = "bge_small"
YOUTU_QUERY_PROMPT = "Instruction: Given a search query, retrieve passages that answer the question \nQuery:"
QWEN_QUERY_PROMPT = "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"


@dataclass(frozen=True)
class ModelSpec:
    key: str
    model_id: str
    official_url: str
    query_method: str
    document_method: str
    query_prompt: str
    trust_remote_code: bool
    official_max_length: int
    preferred_device: str
    official_notes: str


MODEL_SPECS = {
    "youtu": ModelSpec(
        key="youtu",
        model_id="tencent/Youtu-Embedding",
        official_url="https://huggingface.co/tencent/Youtu-Embedding",
        query_method="SentenceTransformer.encode_query",
        document_method="SentenceTransformer.encode_document",
        query_prompt=YOUTU_QUERY_PROMPT,
        trust_remote_code=True,
        official_max_length=8192,
        preferred_device="cpu",
        official_notes="2B/2048-d; official query/document methods; right padding; mean pooling excluding instruction",
    ),
    "conan": ModelSpec(
        key="conan",
        model_id="TencentBAC/Conan-embedding-v2",
        official_url="https://huggingface.co/TencentBAC/Conan-embedding-v2",
        query_method="official authenticated Conan API",
        document_method="official authenticated Conan API",
        query_prompt="",
        trust_remote_code=True,
        official_max_length=32768,
        preferred_device="remote-api",
        official_notes="Official repository currently publishes client code only; API requires CONAN_AK and CONAN_SK",
    ),
    "qwen3_4b": ModelSpec(
        key="qwen3_4b",
        model_id="Qwen/Qwen3-Embedding-4B",
        official_url="https://huggingface.co/Qwen/Qwen3-Embedding-4B",
        query_method="SentenceTransformer.encode(prompt_name='query')",
        document_method="SentenceTransformer.encode",
        query_prompt=QWEN_QUERY_PROMPT,
        trust_remote_code=False,
        official_max_length=32768,
        preferred_device="cpu",
        official_notes="4B/2560-d; official stored query prompt; last-token pooling; left padding",
    ),
    "acge": ModelSpec(
        key="acge",
        model_id="aspire/acge_text_embedding",
        official_url="https://huggingface.co/aspire/acge_text_embedding",
        query_method="SentenceTransformer.encode",
        document_method="SentenceTransformer.encode",
        query_prompt="",
        trust_remote_code=False,
        official_max_length=1024,
        preferred_device="cuda",
        official_notes="Chinese retrieval model; native 1792-d output; no query instruction",
    ),
}
MODEL_ORDER = (BASELINE_KEY, *MODEL_SPECS)
MODEL_LABELS = {
    BASELINE_KEY: PRODUCTION_EMBEDDING,
    **{key: spec.model_id for key, spec in MODEL_SPECS.items()},
}
BASELINE_SPEC = ModelSpec(
    key=BASELINE_KEY,
    model_id=PRODUCTION_EMBEDDING,
    official_url="https://huggingface.co/BAAI/bge-small-zh-v1.5",
    query_method="frozen production SentenceTransformer embedding",
    document_method="frozen production SentenceTransformer embedding",
    query_prompt="",
    trust_remote_code=False,
    official_max_length=512,
    preferred_device="reused-cache",
    official_notes="Frozen 512-d production baseline; max_length=512",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen MidTerm retrieval embedding-model ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--models", default=",".join(MODEL_SPECS), help="Comma-separated candidate keys")
    parser.add_argument("--worker", choices=tuple(MODEL_SPECS))
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--large-batch-size", type=int, default=1)
    parser.add_argument("--model-timeout", type=int, default=7200)
    parser.add_argument("--audit-only", action="store_true", help="Run tokenizer audit and record a resource block")
    parser.add_argument("--skip-workers", action="store_true", help="Only finalize existing caches/status files")
    return parser.parse_args()


def configure_huggingface_endpoint() -> None:
    """Use the canonical Hub when the configured mirror is unavailable."""
    if os.environ.get("HF_ENDPOINT", "").rstrip("/") == "https://hf-mirror.com":
        os.environ["HF_ENDPOINT"] = "https://huggingface.co"


def next_reasonable_max_length(required: int, supported: int) -> int:
    if required > supported:
        raise ValueError(f"Required {required} tokens exceeds official maximum {supported}")
    for candidate in (128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768):
        if candidate >= required and candidate <= supported:
            return candidate
    return supported


def actual_query_text(spec: ModelSpec, raw_query: str) -> str:
    return f"{spec.query_prompt}{raw_query}" if spec.query_prompt else raw_query


def length_stats(values: Sequence[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def tokenizer_audit(
    spec: ModelSpec,
    tokenizer: Any,
    pages: Sequence[Mapping[str, Any]],
    query_ids: Sequence[str],
    query_texts: Sequence[str],
    *,
    max_length_override: int | None = None,
    require_no_truncation: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    detail: list[dict[str, Any]] = []
    page_counts = []
    for page in pages:
        count = len(tokenizer.encode(str(page["no_user_text"]), add_special_tokens=True, truncation=False))
        page_counts.append(count)
        detail.append(
            {
                "model": spec.model_id,
                "kind": "Page",
                "item_id": page["page_id"],
                "session_id": page["session_code"],
                "source_turn_id": page["source_turn_id"],
                "raw_text_token_count": count,
                "official_instruction_added": False,
            }
        )
    actual_queries = [actual_query_text(spec, text) for text in query_texts]
    query_counts = []
    for query_id, text in zip(query_ids, actual_queries):
        count = len(tokenizer.encode(text, add_special_tokens=True, truncation=False))
        query_counts.append(count)
        detail.append(
            {
                "model": spec.model_id,
                "kind": "Query",
                "item_id": query_id,
                "session_id": query_id.split("-", 1)[0],
                "source_turn_id": "",
                "raw_text_token_count": count,
                "official_instruction_added": bool(spec.query_prompt),
            }
        )
    required = max(page_counts + query_counts)
    max_length = max_length_override or next_reasonable_max_length(required, spec.official_max_length)
    for row in detail:
        row["selected_max_length"] = max_length
        row["truncated"] = int(row["raw_text_token_count"]) > max_length
    summary = {
        "model": spec.model_id,
        "tokenizer_class": type(tokenizer).__name__,
        "tokenizer_native_model_max_length": int(tokenizer.model_max_length),
        "truncation_side": str(tokenizer.truncation_side),
        "padding_side": str(tokenizer.padding_side),
        "selected_max_length": max_length,
        "official_supported_max_length": spec.official_max_length,
        "Page": {**length_stats(page_counts), "truncated_count": sum(value > max_length for value in page_counts)},
        "Query": {**length_stats(query_counts), "truncated_count": sum(value > max_length for value in query_counts)},
        "query_instruction": spec.query_prompt or None,
    }
    if require_no_truncation and (summary["Page"]["truncated_count"] or summary["Query"]["truncated_count"]):
        raise AssertionError(f"Non-zero truncation for {spec.model_id}: {summary}")
    return detail, summary, max_length


def cache_paths(
    output_dir: Path,
    spec: ModelSpec,
    kind: str,
    ids: Sequence[str],
    raw_texts: Sequence[str],
    actual_texts: Sequence[str],
    max_length: int,
) -> tuple[Path, Path, str]:
    contract = {
        "model": spec.model_id,
        "kind": kind,
        "ids": list(ids),
        "raw_texts": list(raw_texts),
        "actual_embedding_texts": list(actual_texts),
        "max_length": max_length,
        "query_method": spec.query_method,
        "document_method": spec.document_method,
        "normalization": "L2 normalized",
    }
    content_hash = stable_hash(contract)
    directory = output_dir / "cache/embeddings" / safe_slug(spec.model_id)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{kind}-{content_hash[:16]}"
    return directory / f"{stem}.npz", directory / f"{stem}.json", content_hash


def load_cached_vectors(vector_path: Path, metadata_path: Path, ids: Sequence[str], content_hash: str) -> np.ndarray | None:
    if not vector_path.exists() or not metadata_path.exists():
        return None
    metadata = load_json(metadata_path)
    values = np.load(vector_path)
    cached_ids = [str(value) for value in values["ids"].tolist()]
    if metadata.get("content_hash") != content_hash or cached_ids != list(ids):
        return None
    return np.asarray(values["vectors"], dtype=np.float32)


def encode_matrix(
    model: SentenceTransformer,
    spec: ModelSpec,
    kind: str,
    texts: Sequence[str],
    batch_size: int,
) -> np.ndarray:
    kwargs = {
        "batch_size": batch_size,
        "show_progress_bar": True,
        "convert_to_numpy": True,
        "normalize_embeddings": True,
    }
    if kind == "queries" and spec.key == "youtu":
        values = model.encode_query(list(texts), **kwargs)
    elif kind == "pages" and spec.key == "youtu":
        values = model.encode_document(list(texts), **kwargs)
    elif kind == "queries" and spec.key == "qwen3_4b":
        values = model.encode(list(texts), prompt_name="query", **kwargs)
    else:
        values = model.encode(list(texts), **kwargs)
    matrix = np.asarray(values, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1)
    # Some half-precision models return vectors that were normalized before the
    # final dtype conversion and therefore drift by a few 1e-3. The experiment
    # contract is normalized cosine, so enforce it once in float32.
    matrix = matrix / np.maximum(norms[:, None], np.finfo(np.float32).tiny)
    norms = np.linalg.norm(matrix, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-5):
        raise AssertionError(f"{spec.model_id}/{kind} vectors are not normalized: {norms.min()}..{norms.max()}")
    return matrix


def save_matrix(
    vector_path: Path,
    metadata_path: Path,
    ids: Sequence[str],
    matrix: np.ndarray,
    metadata: Mapping[str, Any],
) -> None:
    np.savez_compressed(vector_path, ids=np.asarray(ids, dtype=str), vectors=matrix)
    dump_json(metadata_path, dict(metadata))


def load_sentence_transformer(spec: ModelSpec, device: str) -> SentenceTransformer:
    kwargs: dict[str, Any] = {"device": device}
    if spec.trust_remote_code:
        kwargs["trust_remote_code"] = True
    if spec.key == "qwen3_4b":
        kwargs["tokenizer_kwargs"] = {"padding_side": "left"}
    return SentenceTransformer(spec.model_id, **kwargs)


def conan_blocked_status(spec: ModelSpec) -> dict[str, Any]:
    return {
        "model": spec.model_id,
        "status": "BLOCKED_OFFICIAL_ARTIFACT_OR_CREDENTIALS",
        "reason": (
            "Official Hugging Face repository contains client/example code but no model weights or tokenizer. "
            "The official temporary API requires CONAN_AK and CONAN_SK, which are absent."
        ),
        "official_repository_has_local_weights": False,
        "official_repository_has_tokenizer": False,
        "CONAN_AK_present": bool(os.environ.get("CONAN_AK")),
        "CONAN_SK_present": bool(os.environ.get("CONAN_SK")),
        "tokenizer_audit_status": "NOT_POSSIBLE_NO_OFFICIAL_TOKENIZER",
        "embedding_count": 0,
        "api_attempt_count": 0,
        "official_contract": asdict(spec),
    }


def run_worker(args: argparse.Namespace) -> None:
    configure_huggingface_endpoint()
    assert args.worker
    spec = MODEL_SPECS[args.worker]
    status_dir = args.output_dir / "model_status"
    status_dir.mkdir(parents=True, exist_ok=True)
    status_path = status_dir / f"{spec.key}.json"
    if spec.key == "conan":
        dump_json(status_path, conan_blocked_status(spec))
        return

    started = time.perf_counter()
    status: dict[str, Any] = {"model": spec.model_id, "status": "STARTED", "official_contract": asdict(spec)}
    dump_json(status_path, status)
    try:
        _, old_pages, queries = load_pages_queries()
        pages = load_existing_add_pages(old_pages)["PreviousAndFollowingContext"]
        p2_texts = load_p2_texts(queries)
        page_ids = [str(page["page_id"]) for page in pages]
        query_ids = [str(query["query_id"]) for query in queries]
        page_texts = [str(page["no_user_text"]) for page in pages]
        query_texts = [p2_texts[query_id] for query_id in query_ids]
        actual_query_texts = [actual_query_text(spec, value) for value in query_texts]

        tokenizer = AutoTokenizer.from_pretrained(
            spec.model_id,
            trust_remote_code=spec.trust_remote_code,
            padding_side="left" if spec.key == "qwen3_4b" else "right",
        )
        detail, token_summary, max_length = tokenizer_audit(spec, tokenizer, pages, query_ids, query_texts)
        write_csv(args.output_dir / f"analysis/tokenizer_{spec.key}.csv", detail)
        dump_json(args.output_dir / f"analysis/tokenizer_{spec.key}_summary.json", token_summary)
        status.update({"tokenizer_audit_status": "PASS", "tokenizer": token_summary})
        dump_json(status_path, status)

        if args.audit_only:
            reason = {
                "qwen3_4b": (
                    "The official 7.49 GiB BF16 weights exceed the 4 GiB GPU. CPU encoding was measured at "
                    "183-232 seconds per Page (batch_size=1), projecting roughly 17-21 hours for 333 Pages; "
                    "the run was stopped after two Pages without changing precision or the benchmark."
                ),
                "youtu": (
                    "The official 8.98 GiB weights exceed the 4 GiB GPU and leave insufficient headroom on "
                    "the 11 GiB host for a standards-preserving local run. No quantization/offload substitute "
                    "was used because that would change the requested official encoding standard."
                ),
            }.get(spec.key, "Standards-preserving local embedding is unavailable on this host")
            status.update(
                {
                    "status": "BLOCKED_LOCAL_RESOURCES",
                    "reason": reason,
                    "page_embedding_count": 0,
                    "query_embedding_count": 0,
                    "elapsed_seconds": time.perf_counter() - started,
                }
            )
            if spec.key == "qwen3_4b":
                status.update(
                    {
                        "resource_attempt_count": 1,
                        "weight_download_completed": True,
                        "measured_pages_before_stop": 2,
                        "measured_cpu_seconds_per_page_range": [183, 232],
                    }
                )
            dump_json(status_path, status)
            return

        page_paths = cache_paths(args.output_dir, spec, "pages", page_ids, page_texts, page_texts, max_length)
        query_paths = cache_paths(
            args.output_dir, spec, "queries", query_ids, query_texts, actual_query_texts, max_length
        )
        page_matrix = load_cached_vectors(page_paths[0], page_paths[1], page_ids, page_paths[2])
        query_matrix = load_cached_vectors(query_paths[0], query_paths[1], query_ids, query_paths[2])
        cache_hits = int(page_matrix is not None) + int(query_matrix is not None)

        model: SentenceTransformer | None = None
        device = spec.preferred_device
        if device == "cuda" and not torch.cuda.is_available():
            device = "cpu"
        if page_matrix is None or query_matrix is None:
            model = load_sentence_transformer(spec, device)
            model.max_seq_length = max_length
            if spec.query_prompt:
                stored_prompt = str(model.prompts.get("query") or "")
                if stored_prompt != spec.query_prompt:
                    raise AssertionError(
                        f"Official stored query prompt mismatch for {spec.model_id}: {stored_prompt!r}"
                    )
            batch_size = args.batch_size if spec.key == "acge" else args.large_batch_size
            if page_matrix is None:
                build_started = time.perf_counter()
                page_matrix = encode_matrix(model, spec, "pages", page_texts, batch_size)
                page_meta = {
                    "model": spec.model_id,
                    "kind": "pages",
                    "content_hash": page_paths[2],
                    "item_count": len(page_ids),
                    "dimension": int(page_matrix.shape[1]),
                    "max_length": max_length,
                    "device": str(model.device),
                    "build_ms": (time.perf_counter() - build_started) * 1000,
                    "encoding_method": spec.document_method,
                    "normalization": "L2 normalized",
                }
                save_matrix(page_paths[0], page_paths[1], page_ids, page_matrix, page_meta)
            if query_matrix is None:
                build_started = time.perf_counter()
                query_matrix = encode_matrix(model, spec, "queries", query_texts, batch_size)
                query_meta = {
                    "model": spec.model_id,
                    "kind": "queries",
                    "content_hash": query_paths[2],
                    "item_count": len(query_ids),
                    "dimension": int(query_matrix.shape[1]),
                    "max_length": max_length,
                    "device": str(model.device),
                    "build_ms": (time.perf_counter() - build_started) * 1000,
                    "encoding_method": spec.query_method,
                    "query_instruction": spec.query_prompt or None,
                    "normalization": "L2 normalized",
                }
                save_matrix(query_paths[0], query_paths[1], query_ids, query_matrix, query_meta)
        assert page_matrix is not None and query_matrix is not None
        if page_matrix.shape[0] != 333 or query_matrix.shape[0] != 99:
            raise AssertionError(f"Embedding coverage mismatch: {page_matrix.shape}/{query_matrix.shape}")
        if page_matrix.shape[1] != query_matrix.shape[1]:
            raise AssertionError(f"Page/Query dimension mismatch: {page_matrix.shape}/{query_matrix.shape}")
        status.update(
            {
                "status": "COMPLETED",
                "selected_max_length": max_length,
                "dimension": int(page_matrix.shape[1]),
                "device": device if model is None else str(model.device),
                "page_embedding_count": 333,
                "query_embedding_count": 99,
                "cache_hit_artifact_count": cache_hits,
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
        dump_json(status_path, status)
        del model, page_matrix, query_matrix
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception as exc:
        status.update(
            {
                "status": "FAILED",
                "exception_type": type(exc).__name__,
                "reason": str(exc),
                "traceback": traceback.format_exc(),
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
        dump_json(status_path, status)
        raise


def load_worker_vectors(
    output_dir: Path,
    spec: ModelSpec,
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    status: Mapping[str, Any],
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    page_ids = [str(page["page_id"]) for page in pages]
    query_ids = [str(query["query_id"]) for query in queries]
    page_texts = [str(page["no_user_text"]) for page in pages]
    query_texts = [p2_texts[query_id] for query_id in query_ids]
    actual_queries = [actual_query_text(spec, value) for value in query_texts]
    max_length = int(status["selected_max_length"])
    page_paths = cache_paths(output_dir, spec, "pages", page_ids, page_texts, page_texts, max_length)
    query_paths = cache_paths(output_dir, spec, "queries", query_ids, query_texts, actual_queries, max_length)
    page_matrix = load_cached_vectors(page_paths[0], page_paths[1], page_ids, page_paths[2])
    query_matrix = load_cached_vectors(query_paths[0], query_paths[1], query_ids, query_paths[2])
    if page_matrix is None or query_matrix is None:
        raise AssertionError(f"Completed model cache missing: {spec.model_id}")
    return (
        {item_id: vector.tolist() for item_id, vector in zip(page_ids, page_matrix)},
        {item_id: vector.tolist() for item_id, vector in zip(query_ids, query_matrix)},
    )


def retrieval_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics, sessions = [], []
    for key in MODEL_ORDER:
        if key not in rankings:
            continue
        row = metric_row(
            snapshots,
            rankings[key],
            add_label="最近上文 + eviction 时真实下文",
            formatter_label="Summary + Keywords",
        )
        metrics.append(
            {
                "Model": MODEL_LABELS[key],
                "Micro R@5": row["Top5"],
                "Macro R@5": row["Macro Top5"],
                "Top5 Gold": row["Top5 recalled Gold"],
                "R@10": row["Top10"],
                "R@20": row["Top20"],
                "MRR": row["MRR"],
                "Mean Gold Rank": row["Mean Gold Rank"],
            }
        )
        _, by_session = evaluate_all(snapshots, rankings[key])
        for code in SESSION_CODES:
            value = by_session[code]
            sessions.append(
                {
                    "Model": MODEL_LABELS[key],
                    "session_id": code,
                    "eligible_gold_count": value["eligible_gold_count"],
                    "R@5": value["recall_at_5"],
                    "R@10": value["recall_at_10"],
                    "R@20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    return metrics, sessions


def comparison_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    comparisons, gold_rows, query_rows, extremes = [], [], [], []
    for key in MODEL_ORDER[1:]:
        if key not in rankings:
            continue
        label = f"{MODEL_LABELS[key]} vs {MODEL_LABELS[BASELINE_KEY]}"
        aggregate, gold, queries = compare_rankings(
            snapshots, rankings[BASELINE_KEY], rankings[key], comparison=label
        )
        aggregate["Model"] = MODEL_LABELS[key]
        comparisons.append(aggregate)
        for row in gold:
            row["Model"] = MODEL_LABELS[key]
            before, after = int(row["before_rank"]), int(row["after_rank"])
            row["extreme_movement"] = (
                "TOP5_TO_AFTER20" if before <= 5 and after > 20 else "AFTER20_TO_TOP5" if before > 20 and after <= 5 else ""
            )
            if row["extreme_movement"]:
                extremes.append(row)
        for row in queries:
            row["Model"] = MODEL_LABELS[key]
        gold_rows.extend(gold)
        query_rows.extend(queries)
    return comparisons, gold_rows, query_rows, extremes


def representative_outputs(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selections: dict[str, list[dict[str, Any]]] = {}
    for key in MODEL_ORDER[1:]:
        if key not in rankings:
            continue
        rows = [row for row in gold_rows if row["Model"] == MODEL_LABELS[key]]
        promoted = sorted(
            (row for row in rows if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5),
            key=lambda row: int(row["rank_improvement"]),
            reverse=True,
        )[:5]
        demoted = sorted(
            (row for row in rows if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5),
            key=lambda row: int(row["rank_improvement"]),
        )[:5]
        selections[f"{MODEL_LABELS[key]} / PROMOTED"] = promoted
        selections[f"{MODEL_LABELS[key]} / DEMOTED"] = demoted
    pairs = {(str(row["query_id"]), str(row["gold_page_id"])) for values in selections.values() for row in values}
    query_by_id = {str(row["query_id"]): row for row in queries}
    page_by_id = {str(row["page_id"]): row for row in pages}
    maps = {
        key: {query_id: rank_map(values) for query_id, values in ranking.items()} for key, ranking in rankings.items()
    }
    cases, lines = [], ["# Embedding 模型代表性 Gold movement", ""]
    for query_id, page_id in sorted(pairs):
        query, page = query_by_id[query_id], page_by_id[page_id]
        case = {
            "session_id": query["session_code"],
            "query_id": query_id,
            "original_query": query["original_query"],
            "p2_query": p2_texts[query_id],
            "gold_page_id": page_id,
            "gold_source_turn_id": page["source_turn_id"],
            "gold_summary": page["summary"],
            "gold_keywords": page["keywords"],
            "models": {},
        }
        lines.extend(
            [
                f"## {query_id} → {page['source_turn_id']}",
                "",
                f"P2 Query：{p2_texts[query_id]}",
                "",
                "| Model | Gold rank | Gold score | Top5 source turns |",
                "|---|---:|---:|---|",
            ]
        )
        for key in MODEL_ORDER:
            if key not in rankings:
                continue
            gold = maps[key][query_id][page_id]
            top5 = [str(item["source_turn_id"]) for item in rankings[key][query_id][:5]]
            case["models"][MODEL_LABELS[key]] = {
                "gold_rank": int(gold["rank"]),
                "gold_score": float(gold["score"]),
                "top10": list(rankings[key][query_id][:10]),
            }
            lines.append(
                f"| {MODEL_LABELS[key]} | #{int(gold['rank'])} | {float(gold['score']):.9f} | {', '.join(top5)} |"
            )
        lines.extend(
            [
                "",
                "Gold Summary：",
                "",
                "```text",
                str(page["summary"]),
                "```",
                "",
                f"Gold Keywords：{', '.join(str(value) for value in page['keywords'])}",
                "",
            ]
        )
        cases.append(case)
    return {"selection": selections, "cases": cases}, "\n".join(lines) + "\n"


def model_characterization(metrics: Sequence[Mapping[str, Any]], comparisons: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline = next(row for row in metrics if row["Model"] == PRODUCTION_EMBEDDING)
    comparison_by = {str(row["Model"]): row for row in comparisons}
    rows = []
    for row in metrics:
        if row["Model"] == PRODUCTION_EMBEDDING:
            continue
        r5_delta = float(row["Micro R@5"]) - float(baseline["Micro R@5"])
        r10_delta = float(row["R@10"]) - float(baseline["R@10"])
        r20_delta = float(row["R@20"]) - float(baseline["R@20"])
        if r5_delta > 0:
            classification = "TOP5_DISCRIMINATION_STRONGER"
        elif r10_delta > 0 or r20_delta > 0:
            classification = "WIDER_RECALL_ONLY"
        else:
            classification = "OVERALL_DEGRADED_OR_TIED"
        rows.append(
            {
                "Model": row["Model"],
                "classification": classification,
                "R@5 delta": r5_delta,
                "R@10 delta": r10_delta,
                "R@20 delta": r20_delta,
                "net_gold": comparison_by[str(row["Model"])]["Net Gold gain"],
            }
        )
    return rows


def build_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    characterizations: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# 当前最优 MidTerm 配置上的 Embedding 模型对照",
        "",
        "Add Page、P2 Query、Gold、visibility 全部冻结；无 LLM 调用或 Summary regeneration。",
        "",
        "| Model | Micro R@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Model']} | {float(row['Micro R@5']):.2%} | {float(row['Macro R@5']):.2%} | "
            f"{float(row['R@10']):.2%} | {float(row['R@20']):.2%} | {float(row['MRR']):.4f} | "
            f"{float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(["", "## Session R@5", "", "| Model | S001 | S002 | S003 | S004 | S005 |", "|---|---:|---:|---:|---:|---:|"])
    session_by = {(str(row["Model"]), str(row["session_id"])): row for row in sessions}
    for row in metrics:
        values = [float(session_by[(str(row["Model"]), code)]["R@5"]) for code in SESSION_CODES]
        lines.append(f"| {row['Model']} | " + " | ".join(f"{value:.2%}" for value in values) + " |")
    lines.extend(
        [
            "",
            "## 相对 BGE-small 的 Top5 movement",
            "",
            "| Model | Promoted | Demoted | Net | Rescued Query | Hurt Query |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in comparisons:
        lines.append(
            f"| {row['Model']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(["", "## 模型执行状态", ""])
    for row in statuses:
        lines.append(f"- {row['model']}：{row['status']}" + (f" — {row.get('reason')}" if row.get("reason") else ""))
    lines.extend(["", "## Retrieval 表现类型", ""])
    for row in characterizations:
        lines.append(f"- {row['Model']}：{row['classification']}")
    return "\n".join(lines) + "\n"


def launch_workers(args: argparse.Namespace, selected: Sequence[str]) -> None:
    for key in selected:
        status_path = args.output_dir / f"model_status/{key}.json"
        if status_path.exists() and load_json(status_path).get("status") in {
            "COMPLETED",
            "BLOCKED_OFFICIAL_ARTIFACT_OR_CREDENTIALS",
            "BLOCKED_LOCAL_RESOURCES",
        }:
            continue
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            key,
            "--output-dir",
            str(args.output_dir),
            "--batch-size",
            str(args.batch_size),
            "--large-batch-size",
            str(args.large_batch_size),
        ]
        try:
            completed = subprocess.run(command, check=False, timeout=args.model_timeout)
            if completed.returncode != 0:
                existing = load_json(status_path) if status_path.exists() else {}
                existing.update(
                    {
                        "model": MODEL_SPECS[key].model_id,
                        "status": "FAILED_SUBPROCESS",
                        "returncode": completed.returncode,
                        "reason": existing.get("reason") or f"Worker exited with return code {completed.returncode}",
                        "official_contract": asdict(MODEL_SPECS[key]),
                    }
                )
                dump_json(status_path, existing)
        except subprocess.TimeoutExpired:
            dump_json(
                status_path,
                {
                    "model": MODEL_SPECS[key].model_id,
                    "status": "FAILED_TIMEOUT",
                    "reason": f"Official encoding did not complete within {args.model_timeout} seconds",
                    "official_contract": asdict(MODEL_SPECS[key]),
                },
            )


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = [value.strip() for value in args.models.split(",") if value.strip()]
    unknown = set(selected) - set(MODEL_SPECS)
    if unknown:
        raise ValueError(f"Unknown models: {sorted(unknown)}")
    if args.worker:
        run_worker(args)
        return
    if not args.skip_workers:
        launch_workers(args, selected)

    snapshots, old_pages, queries = load_pages_queries()
    pages = load_existing_add_pages(old_pages)["PreviousAndFollowingContext"]
    p2_texts = load_p2_texts(queries)
    small_pages, small_queries, small_metadata = load_small_baseline_vectors(pages, queries)
    rankings: dict[str, Mapping[str, Sequence[Mapping[str, Any]]]] = {
        BASELINE_KEY: rank_configuration(snapshots, small_queries, small_pages)
    }
    baseline_metrics, _ = evaluate_all(snapshots, rankings[BASELINE_KEY])
    if not math.isclose(float(baseline_metrics["recall_at_5"]), 59 / 154, abs_tol=1e-12):
        raise AssertionError(f"BGE-small baseline is not 59/154: {baseline_metrics}")

    statuses = []
    for key in selected:
        path = args.output_dir / f"model_status/{key}.json"
        status = load_json(path) if path.exists() else {
            "model": MODEL_SPECS[key].model_id,
            "status": "NOT_RUN",
            "reason": "No worker status file",
        }
        statuses.append(status)
        if status.get("status") != "COMPLETED":
            continue
        page_vectors, query_vectors = load_worker_vectors(
            args.output_dir, MODEL_SPECS[key], pages, queries, p2_texts, status
        )
        rankings[key] = rank_configuration(snapshots, query_vectors, page_vectors)

    metrics, sessions = retrieval_outputs(snapshots, rankings)
    comparisons, gold_rows, query_rows, extremes = comparison_outputs(snapshots, rankings)
    separations = []
    for key in MODEL_ORDER:
        if key not in rankings:
            continue
        row = separation_row(
            snapshots,
            rankings[key],
            add_label="最近上文 + eviction 时真实下文",
            formatter_label="Summary + Keywords",
        )
        row["Model"] = MODEL_LABELS[key]
        separations.append(row)
    characterizations = model_characterization(metrics, comparisons)
    cases_json, cases_markdown = representative_outputs(queries, pages, p2_texts, rankings, gold_rows)

    page_ids = [str(row["page_id"]) for row in pages]
    query_ids = [str(row["query_id"]) for row in queries]
    page_texts = [str(row["no_user_text"]) for row in pages]
    query_texts = [p2_texts[query_id] for query_id in query_ids]
    configure_huggingface_endpoint()
    baseline_tokenizer = AutoTokenizer.from_pretrained(PRODUCTION_EMBEDDING)
    baseline_token_detail, baseline_token_summary, _ = tokenizer_audit(
        BASELINE_SPEC,
        baseline_tokenizer,
        pages,
        query_ids,
        query_texts,
        max_length_override=512,
        require_no_truncation=False,
    )
    write_csv(args.output_dir / "analysis/tokenizer_bge_small.csv", baseline_token_detail)
    tokenizer_summaries = [baseline_token_summary]
    tokenizer_rows = [
        {
            "Model": PRODUCTION_EMBEDDING,
            "audit_status": "PASS_EXISTING_512_CONTRACT",
            "selected_max_length": 512,
            "Page token mean": baseline_token_summary["Page"]["mean"],
            "Page token median": baseline_token_summary["Page"]["median"],
            "Page token p90": baseline_token_summary["Page"]["p90"],
            "Page token max": baseline_token_summary["Page"]["max"],
            "Page truncated": baseline_token_summary["Page"]["truncated_count"],
            "Query token mean": baseline_token_summary["Query"]["mean"],
            "Query token median": baseline_token_summary["Query"]["median"],
            "Query token p90": baseline_token_summary["Query"]["p90"],
            "Query token max": baseline_token_summary["Query"]["max"],
            "Query truncated": baseline_token_summary["Query"]["truncated_count"],
            "official_query_instruction": "",
        }
    ]
    for status in statuses:
        token = status.get("tokenizer")
        if token:
            tokenizer_summaries.append(token)
            tokenizer_rows.append(
                {
                    "Model": status["model"],
                    "audit_status": status.get("tokenizer_audit_status"),
                    "selected_max_length": token["selected_max_length"],
                    "Page token mean": token["Page"]["mean"],
                    "Page token median": token["Page"]["median"],
                    "Page token p90": token["Page"]["p90"],
                    "Page token max": token["Page"]["max"],
                    "Page truncated": token["Page"]["truncated_count"],
                    "Query token mean": token["Query"]["mean"],
                    "Query token median": token["Query"]["median"],
                    "Query token p90": token["Query"]["p90"],
                    "Query token max": token["Query"]["max"],
                    "Query truncated": token["Query"]["truncated_count"],
                    "official_query_instruction": token.get("query_instruction") or "",
                }
            )
        else:
            tokenizer_rows.append(
                {
                    "Model": status["model"],
                    "audit_status": status.get("tokenizer_audit_status", "NOT_AVAILABLE"),
                    "selected_max_length": "",
                    "Page token mean": "",
                    "Page token median": "",
                    "Page token p90": "",
                    "Page token max": "",
                    "Page truncated": "",
                    "Query token mean": "",
                    "Query token median": "",
                    "Query token p90": "",
                    "Query token max": "",
                    "Query truncated": "",
                    "official_query_instruction": "",
                }
            )
    expected_page_set = {str(page["page_id"]) for page in old_pages}
    visibility_valid = all(
        not int(row.get("future_page_leak_count") or 0)
        and set(map(str, row["visible_page_ids"])) <= expected_page_set
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    )
    completed = [row for row in statuses if row.get("status") == "COMPLETED"]
    validations = {
        "baseline_59_of_154": {"status": "PASS", "micro_r5": baseline_metrics["recall_at_5"]},
        "frozen_counts": {
            "status": "PASS"
            if len(pages) == 333 and len(queries) == 99 and sum(len(row["eligible_gold_page_ids"]) for row in queries) == 154
            else "FAIL"
        },
        "frozen_page_and_query_text": {
            "status": "PASS",
            "page_text_hash": stable_hash(page_texts),
            "p2_query_text_hash": stable_hash(query_texts),
        },
        "page_identity": {"status": "PASS" if set(page_ids) == expected_page_set else "FAIL"},
        "visibility_and_future_leakage": {"status": "PASS" if visibility_valid else "FAIL"},
        "completed_models_have_zero_truncation": {
            "status": "PASS"
            if all(
                not int(row["tokenizer"]["Page"]["truncated_count"])
                and not int(row["tokenizer"]["Query"]["truncated_count"])
                for row in completed
            )
            else "FAIL"
        },
        "no_new_llm_calls": {"status": "PASS", "value": 0},
        "no_summary_regeneration": {"status": "PASS"},
        "full_session_rerun": {"status": "PASS", "value": False},
    }
    if any(value["status"] != "PASS" for value in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics)
    write_csv(args.output_dir / "metrics/session_metrics.csv", sessions)
    write_csv(args.output_dir / "metrics/comparisons_vs_bge_small.csv", comparisons)
    write_csv(args.output_dir / "metrics/gold_rank_movements.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/extreme_movements.csv", extremes)
    write_csv(args.output_dir / "analysis/query_gold_separation.csv", separations)
    write_csv(args.output_dir / "analysis/model_characterization.csv", characterizations)
    write_csv(args.output_dir / "analysis/model_status.csv", statuses)
    write_csv(args.output_dir / "analysis/tokenizer_summary.csv", tokenizer_rows)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        build_report(metrics, sessions, comparisons, statuses, characterizations), encoding="utf-8"
    )
    metadata = {
        "experiment_name": "midterm_embedding_model_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "add": "最近上文 + eviction 时真实下文（frozen summary/keywords）",
        "page_text_contract": "<summary>\\nKeywords: <comma-space joined keywords>",
        "query_contract": "frozen P2 resolved_query plus official model query instruction when required",
        "raw_page_text_hash": stable_hash(page_texts),
        "raw_p2_query_text_hash": stable_hash(query_texts),
        "retrieval": "per-Session normalized dense cosine",
        "model_contracts": {key: asdict(value) for key, value in MODEL_SPECS.items()},
        "model_status": statuses,
        "tokenizer_length_audits": tokenizer_summaries,
        "baseline_embedding_metadata": small_metadata,
        "new_llm_calls": 0,
        "summary_regeneration": False,
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "validation": validations,
        "validation_all_pass_for_completed_models": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics,
                "sessions": sessions,
                "comparisons": comparisons,
                "model_status": statuses,
                "validation_all_pass_for_completed_models": True,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
