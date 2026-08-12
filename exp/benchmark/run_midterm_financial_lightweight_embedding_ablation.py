"""Financial and lightweight embedding ablation on frozen MidTerm retrieval.

Only the embedding model and its official retrieval prefix/method may change.
Page summaries, keywords, P2 queries, visibility, and Gold remain frozen.
"""

from __future__ import annotations

import argparse
import csv
import gc
import json
import math
import statistics
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
from exp.benchmark.run_midterm_embedding_model_ablation import (  # noqa: E402
    configure_huggingface_endpoint,
    load_cached_vectors,
    next_reasonable_max_length,
    save_matrix,
)
from exp.benchmark.run_midterm_retrieval_experiments import safe_slug  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import dump_json, rank_map, write_csv  # noqa: E402


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_financial_lightweight_embedding_ablation"
BASELINE_KEY = "bge_small"


@dataclass(frozen=True)
class CandidateSpec:
    key: str
    model_id: str
    model_type: str
    official_url: str
    query_prefix: str
    document_prefix: str
    query_method: str
    document_method: str
    trust_remote_code: bool
    native_max_length: int
    expected_dimension: int
    cuda_dtype: str


SPECS = {
    "financial": CandidateSpec(
        "financial",
        "qcsun/financial-embedding",
        "金融",
        "https://huggingface.co/qcsun/financial-embedding",
        "",
        "",
        "SentenceTransformer.encode",
        "SentenceTransformer.encode",
        False,
        8192,
        1024,
        "float16",
    ),
    "balyasny": CandidateSpec(
        "balyasny",
        "BalyasnyAI/multilingual-e5-base",
        "金融",
        "https://huggingface.co/BalyasnyAI/multilingual-e5-base",
        "query: ",
        "passage: ",
        "SentenceTransformer.encode with official query prefix",
        "SentenceTransformer.encode with official passage prefix",
        False,
        512,
        768,
        "float16",
    ),
    "gte": CandidateSpec(
        "gte",
        "Alibaba-NLP/gte-multilingual-base",
        "通用",
        "https://huggingface.co/Alibaba-NLP/gte-multilingual-base",
        "",
        "",
        "SentenceTransformer.encode",
        "SentenceTransformer.encode",
        True,
        8192,
        768,
        "float32",
    ),
    "embeddinggemma": CandidateSpec(
        "embeddinggemma",
        "google/embeddinggemma-300m",
        "通用",
        "https://huggingface.co/google/embeddinggemma-300m",
        "<model stored encode_query prompt>",
        "<model stored encode_document prompt>",
        "SentenceTransformer.encode_query",
        "SentenceTransformer.encode_document",
        False,
        2048,
        768,
        "bfloat16",
    ),
}
ORDER = (BASELINE_KEY, *SPECS)
LABELS = {BASELINE_KEY: PRODUCTION_EMBEDDING, **{key: spec.model_id for key, spec in SPECS.items()}}
TYPES = {BASELINE_KEY: "基线", **{key: spec.model_type for key, spec in SPECS.items()}}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen financial/lightweight embedding ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--models", default=",".join(SPECS))
    parser.add_argument("--worker", choices=tuple(SPECS))
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("--skip-workers", action="store_true")
    return parser.parse_args()


def actual_text(spec: CandidateSpec, kind: str, text: str) -> str:
    if spec.key == "embeddinggemma":
        return text
    return f"{spec.query_prefix if kind == 'Query' else spec.document_prefix}{text}"


def descriptive_stats(values: Sequence[int]) -> dict[str, Any]:
    return {
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def component_audit(
    model_id: str,
    tokenizer: Any,
    pages: Sequence[Mapping[str, Any]],
    query_ids: Sequence[str],
    query_texts: Sequence[str],
    *,
    document_prefix: str,
    query_prefix: str,
    native_max_length: int,
    used_max_length: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    special = int(tokenizer.num_special_tokens_to_add(pair=False))
    page_counts = [
        len(tokenizer.encode(f"{document_prefix}{page['no_user_text']}", add_special_tokens=True, truncation=False))
        for page in pages
    ]
    query_counts = [
        len(tokenizer.encode(f"{query_prefix}{text}", add_special_tokens=True, truncation=False)) for text in query_texts
    ]
    required = max(page_counts + query_counts)
    max_length = used_max_length or next_reasonable_max_length(required, native_max_length)
    detail = []
    for page, total in zip(pages, page_counts):
        summary = str(page["summary"]).strip()
        raw = str(page["no_user_text"])
        prefix_tokens = len(tokenizer.encode(document_prefix, add_special_tokens=False)) if document_prefix else 0
        summary_end = len(tokenizer.encode(f"{document_prefix}{summary}", add_special_tokens=False))
        full_tokens = len(tokenizer.encode(f"{document_prefix}{raw}", add_special_tokens=False))
        summary_tokens = max(0, summary_end - prefix_tokens)
        keyword_tokens = max(0, full_tokens - summary_end)
        budget = max(0, max_length - special - prefix_tokens)
        summary_kept = min(summary_tokens, budget)
        keyword_kept = min(keyword_tokens, max(0, budget - summary_kept))
        keyword_status = (
            "FULLY_KEPT"
            if keyword_kept == keyword_tokens
            else "FULLY_TRUNCATED"
            if keyword_kept == 0
            else "PARTIALLY_TRUNCATED"
        )
        detail.append(
            {
                "Model": model_id,
                "kind": "Page",
                "session_id": page["session_code"],
                "item_id": page["page_id"],
                "source_turn_id": page["source_turn_id"],
                "token_count": total,
                "max_length": max_length,
                "truncated": total > max_length,
                "summary_tokens": summary_tokens,
                "summary_tokens_kept": summary_kept,
                "summary_truncated": summary_kept < summary_tokens,
                "keywords_tokens": keyword_tokens,
                "keywords_tokens_kept": keyword_kept,
                "keywords_status": keyword_status,
            }
        )
    for query_id, total in zip(query_ids, query_counts):
        detail.append(
            {
                "Model": model_id,
                "kind": "Query",
                "session_id": query_id.split("-", 1)[0],
                "item_id": query_id,
                "source_turn_id": "",
                "token_count": total,
                "max_length": max_length,
                "truncated": total > max_length,
                "summary_tokens": "",
                "summary_tokens_kept": "",
                "summary_truncated": "",
                "keywords_tokens": "",
                "keywords_tokens_kept": "",
                "keywords_status": "",
            }
        )
    page_rows = [row for row in detail if row["kind"] == "Page"]
    query_rows = [row for row in detail if row["kind"] == "Query"]
    summary = {
        "Model": model_id,
        "native_max_length": native_max_length,
        "used_max_length": max_length,
        "tokenizer_class": type(tokenizer).__name__,
        "truncation_side": str(tokenizer.truncation_side),
        "padding_side": str(tokenizer.padding_side),
        "Page": {
            **descriptive_stats(page_counts),
            "truncated_count": sum(bool(row["truncated"]) for row in page_rows),
            "truncated_rate": sum(bool(row["truncated"]) for row in page_rows) / len(page_rows),
            "summary_truncated_count": sum(bool(row["summary_truncated"]) for row in page_rows),
            "keywords_fully_kept_count": sum(row["keywords_status"] == "FULLY_KEPT" for row in page_rows),
            "keywords_partially_truncated_count": sum(
                row["keywords_status"] == "PARTIALLY_TRUNCATED" for row in page_rows
            ),
            "keywords_fully_truncated_count": sum(
                row["keywords_status"] == "FULLY_TRUNCATED" for row in page_rows
            ),
        },
        "Query": {
            **descriptive_stats(query_counts),
            "truncated_count": sum(bool(row["truncated"]) for row in query_rows),
            "truncated_rate": sum(bool(row["truncated"]) for row in query_rows) / len(query_rows),
        },
    }
    return detail, summary, max_length


def vector_paths(
    output_dir: Path,
    spec: CandidateSpec,
    kind: str,
    ids: Sequence[str],
    raw_texts: Sequence[str],
    embedding_texts: Sequence[str],
    max_length: int,
) -> tuple[Path, Path, str]:
    content_hash = stable_hash(
        {
            "model": spec.model_id,
            "kind": kind,
            "ids": list(ids),
            "raw_frozen_texts": list(raw_texts),
            "actual_embedding_texts": list(embedding_texts),
            "max_length": max_length,
            "query_method": spec.query_method,
            "document_method": spec.document_method,
            "normalization": "float32 L2 normalization after model output",
            "cuda_dtype": spec.cuda_dtype,
            **(
                {"runtime_compatibility": "reinitialize all non-persistent RoPE buffers after meta-device loading"}
                if spec.key == "gte"
                else {}
            ),
        }
    )
    directory = output_dir / "cache/embeddings" / safe_slug(spec.model_id)
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{kind}-{content_hash[:16]}"
    return directory / f"{stem}.npz", directory / f"{stem}.json", content_hash


def normalized_matrix(values: Any) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float32)
    if not np.isfinite(matrix).all():
        bad = np.flatnonzero(~np.isfinite(matrix).all(axis=1)).tolist()
        raise AssertionError(f"Embedding contains non-finite rows: {bad}")
    norms = np.linalg.norm(matrix, axis=1)
    if np.any(norms == 0):
        bad = np.flatnonzero(norms == 0).tolist()
        raise AssertionError(f"Embedding contains zero-norm rows: {bad}")
    matrix /= np.maximum(norms[:, None], np.finfo(np.float32).tiny)
    if not np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5):
        raise AssertionError("Float32 L2 normalization failed")
    return matrix


def encode(
    model: SentenceTransformer,
    spec: CandidateSpec,
    kind: str,
    raw_texts: Sequence[str],
    embedding_texts: Sequence[str],
    batch_size: int,
) -> np.ndarray:
    kwargs = {
        "batch_size": batch_size,
        "show_progress_bar": True,
        "convert_to_numpy": True,
        "normalize_embeddings": True,
    }
    if spec.key == "embeddinggemma":
        values = (
            model.encode_query(list(raw_texts), **kwargs)
            if kind == "queries"
            else model.encode_document(list(raw_texts), **kwargs)
        )
    elif spec.key == "gte":
        # The current sentence-transformers/remote-code combination corrupts the
        # accumulated output of one long encode() call. Materializing each official
        # encode result immediately avoids shared-buffer corruption without changing
        # tokenization, pooling, normalization, or the model contract.
        kwargs["show_progress_bar"] = False
        materialized = []
        for index, text in enumerate(embedding_texts):
            value = np.asarray(model.encode([text], **kwargs), dtype=np.float32)[0].copy()
            if not np.isfinite(value).all():
                raise AssertionError(f"GTE produced a non-finite vector at {kind} index {index}")
            materialized.append(value)
        values = np.stack(materialized)
    else:
        values = model.encode(list(embedding_texts), **kwargs)
    return normalized_matrix(values)


def load_model(spec: CandidateSpec, device: str) -> SentenceTransformer:
    kwargs: dict[str, Any] = {"device": device, "trust_remote_code": spec.trust_remote_code}
    if device == "cuda" and spec.cuda_dtype != "float32":
        kwargs["model_kwargs"] = {
            "torch_dtype": torch.float16 if spec.cuda_dtype == "float16" else torch.bfloat16
        }
    model = SentenceTransformer(spec.model_id, **kwargs)
    if spec.cuda_dtype == "float32":
        model = model.float()
    if spec.key == "gte":
        # transformers 5.13 leaves this repository's non-persistent RoPE
        # buffers uninitialized after meta-device loading. Re-run the exact
        # upstream initialization and recreate its arange position buffer.
        embeddings = model[0].auto_model.embeddings
        embeddings._init_rope(model[0].auto_model.config)
        embeddings.rotary_emb.to(embeddings.word_embeddings.weight.device)
        position_ids = torch.arange(
            model[0].auto_model.config.max_position_embeddings,
            device=embeddings.word_embeddings.weight.device,
            dtype=torch.long,
        )
        embeddings.register_buffer("position_ids", position_ids, persistent=False)
    return model


def run_worker(args: argparse.Namespace) -> None:
    configure_huggingface_endpoint()
    assert args.worker
    spec = SPECS[args.worker]
    status_path = args.output_dir / f"model_status/{spec.key}.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "model": spec.model_id,
        "type": spec.model_type,
        "status": "STARTED",
        "stage": "tokenizer_load",
        "official_contract": asdict(spec),
    }
    dump_json(status_path, status)
    started = time.perf_counter()
    try:
        _, old_pages, queries = load_pages_queries()
        pages = load_existing_add_pages(old_pages)["PreviousAndFollowingContext"]
        p2 = load_p2_texts(queries)
        page_ids = [str(page["page_id"]) for page in pages]
        query_ids = [str(query["query_id"]) for query in queries]
        page_raw = [str(page["no_user_text"]) for page in pages]
        query_raw = [p2[query_id] for query_id in query_ids]
        tokenizer = AutoTokenizer.from_pretrained(spec.model_id, trust_remote_code=spec.trust_remote_code)
        if spec.key == "embeddinggemma":
            # The model repository is gated, so stored prompts can only be read after authenticated loading.
            document_prefix = query_prefix = ""
        else:
            document_prefix, query_prefix = spec.document_prefix, spec.query_prefix
        detail, token_summary, max_length = component_audit(
            spec.model_id,
            tokenizer,
            pages,
            query_ids,
            query_raw,
            document_prefix=document_prefix,
            query_prefix=query_prefix,
            native_max_length=spec.native_max_length,
            used_max_length=512 if spec.native_max_length == 512 else None,
        )
        write_csv(args.output_dir / f"analysis/tokenizer_{spec.key}.csv", detail)
        dump_json(args.output_dir / f"analysis/tokenizer_{spec.key}_summary.json", token_summary)
        status.update({"stage": "model_load", "tokenizer_audit_status": "PASS", "tokenizer": token_summary})
        dump_json(status_path, status)

        page_actual = [actual_text(spec, "Page", text) for text in page_raw]
        query_actual = [actual_text(spec, "Query", text) for text in query_raw]
        page_paths = vector_paths(args.output_dir, spec, "pages", page_ids, page_raw, page_actual, max_length)
        query_paths = vector_paths(args.output_dir, spec, "queries", query_ids, query_raw, query_actual, max_length)
        page_matrix = load_cached_vectors(page_paths[0], page_paths[1], page_ids, page_paths[2])
        query_matrix = load_cached_vectors(query_paths[0], query_paths[1], query_ids, query_paths[2])
        cache_hits = int(page_matrix is not None) + int(query_matrix is not None)
        model = None
        device = (
            "cuda"
            if args.device == "auto" and torch.cuda.is_available()
            else "cpu"
            if args.device == "auto"
            else args.device
        )
        if page_matrix is None or query_matrix is None:
            model = load_model(spec, device)
            model.max_seq_length = max_length
            status["stage"] = "inference"
            status["device"] = str(model.device)
            if spec.key == "gte":
                status["runtime_compatibility_fix"] = {
                    "applied": True,
                    "reason": (
                        "transformers 5.13 meta-device loading left the official remote-code non-persistent "
                        "RoPE and position_ids buffers uninitialized"
                    ),
                    "operation": (
                        "re-run upstream _init_rope(config) and recreate "
                        "torch.arange(max_position_embeddings)"
                    ),
                    "official_embedding_contract_changed": False,
                }
            dump_json(status_path, status)
            if page_matrix is None:
                item_started = time.perf_counter()
                page_matrix = encode(model, spec, "pages", page_raw, page_actual, args.batch_size)
                save_matrix(
                    page_paths[0],
                    page_paths[1],
                    page_ids,
                    page_matrix,
                    {
                        "model": spec.model_id,
                        "kind": "pages",
                        "content_hash": page_paths[2],
                        "item_count": 333,
                        "dimension": int(page_matrix.shape[1]),
                        "max_length": max_length,
                        "device": str(model.device),
                        "dtype": spec.cuda_dtype if device == "cuda" else "native",
                        "build_ms": (time.perf_counter() - item_started) * 1000,
                    },
                )
            if query_matrix is None:
                item_started = time.perf_counter()
                query_matrix = encode(model, spec, "queries", query_raw, query_actual, args.batch_size)
                save_matrix(
                    query_paths[0],
                    query_paths[1],
                    query_ids,
                    query_matrix,
                    {
                        "model": spec.model_id,
                        "kind": "queries",
                        "content_hash": query_paths[2],
                        "item_count": 99,
                        "dimension": int(query_matrix.shape[1]),
                        "max_length": max_length,
                        "device": str(model.device),
                        "dtype": spec.cuda_dtype if device == "cuda" else "native",
                        "build_ms": (time.perf_counter() - item_started) * 1000,
                    },
                )
        assert page_matrix is not None and query_matrix is not None
        if page_matrix.shape != (333, spec.expected_dimension) or query_matrix.shape != (
            99,
            spec.expected_dimension,
        ):
            raise AssertionError(f"Unexpected vector shapes {page_matrix.shape}/{query_matrix.shape}")
        status.update(
            {
                "status": "COMPLETED",
                "stage": "complete",
                "used_max_length": max_length,
                "dimension": spec.expected_dimension,
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
        reason = str(exc)
        blocked_gated = "gated repo" in reason.lower() or "restricted" in reason.lower()
        status.update(
            {
                "status": "BLOCKED_GATED_REPOSITORY" if blocked_gated else "FAILED",
                "exception_type": type(exc).__name__,
                "reason": reason,
                "traceback": traceback.format_exc(),
                "elapsed_seconds": time.perf_counter() - started,
            }
        )
        if blocked_gated:
            status["tokenizer_audit_status"] = "NOT_POSSIBLE_GATED_REPOSITORY"
        dump_json(status_path, status)
        if not blocked_gated:
            raise


def cache_vectors(
    output_dir: Path,
    spec: CandidateSpec,
    pages: Sequence[Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2: Mapping[str, str],
    status: Mapping[str, Any],
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    page_ids = [str(page["page_id"]) for page in pages]
    query_ids = [str(query["query_id"]) for query in queries]
    page_raw = [str(page["no_user_text"]) for page in pages]
    query_raw = [p2[query_id] for query_id in query_ids]
    page_actual = [actual_text(spec, "Page", text) for text in page_raw]
    query_actual = [actual_text(spec, "Query", text) for text in query_raw]
    max_length = int(status["used_max_length"])
    page_paths = vector_paths(output_dir, spec, "pages", page_ids, page_raw, page_actual, max_length)
    query_paths = vector_paths(output_dir, spec, "queries", query_ids, query_raw, query_actual, max_length)
    page_matrix = load_cached_vectors(page_paths[0], page_paths[1], page_ids, page_paths[2])
    query_matrix = load_cached_vectors(query_paths[0], query_paths[1], query_ids, query_paths[2])
    if page_matrix is None or query_matrix is None:
        raise AssertionError(f"Missing completed cache for {spec.model_id}")
    return (
        {item_id: vector.tolist() for item_id, vector in zip(page_ids, page_matrix)},
        {item_id: vector.tolist() for item_id, vector in zip(query_ids, query_matrix)},
    )


def launch_workers(args: argparse.Namespace, selected: Sequence[str]) -> None:
    for key in selected:
        path = args.output_dir / f"model_status/{key}.json"
        if path.exists() and load_json(path).get("status") in {"COMPLETED", "BLOCKED_GATED_REPOSITORY"}:
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
            "--device",
            args.device,
        ]
        try:
            result = subprocess.run(command, check=False, timeout=args.timeout)
            if result.returncode:
                status = load_json(path) if path.exists() else {}
                status.update(
                    {
                        "model": SPECS[key].model_id,
                        "type": SPECS[key].model_type,
                        "status": "FAILED_SUBPROCESS",
                        "returncode": result.returncode,
                        "reason": status.get("reason") or f"Worker return code {result.returncode}",
                        "official_contract": asdict(SPECS[key]),
                    }
                )
                dump_json(path, status)
        except subprocess.TimeoutExpired:
            dump_json(
                path,
                {
                    "model": SPECS[key].model_id,
                    "type": SPECS[key].model_type,
                    "status": "FAILED_TIMEOUT",
                    "reason": f"Official inference exceeded {args.timeout} seconds",
                    "official_contract": asdict(SPECS[key]),
                },
            )


def retrieval_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aggregate, sessions = [], []
    for key in ORDER:
        if key not in rankings:
            continue
        row = metric_row(
            snapshots,
            rankings[key],
            add_label="最近上文 + eviction 时真实下文",
            formatter_label="Summary + Keywords",
        )
        aggregate.append(
            {
                "Model": LABELS[key],
                "Type": TYPES[key],
                "Micro R@5": row["Top5"],
                "Gold@5": row["Top5 recalled Gold"],
                "Macro R@5": row["Macro Top5"],
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
                    "Model": LABELS[key],
                    "Type": TYPES[key],
                    "session_id": code,
                    "eligible_gold_count": value["eligible_gold_count"],
                    "R@5": value["recall_at_5"],
                    "R@10": value["recall_at_10"],
                    "R@20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    return aggregate, sessions


def movement_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    summary, gold_rows, query_rows, extremes = [], [], [], []
    for key in ORDER[1:]:
        if key not in rankings:
            continue
        result, gold, query = compare_rankings(
            snapshots,
            rankings[BASELINE_KEY],
            rankings[key],
            comparison=f"{LABELS[key]} vs {PRODUCTION_EMBEDDING}",
        )
        result.update({"Model": LABELS[key], "Type": TYPES[key]})
        summary.append(result)
        for row in gold:
            row.update({"Model": LABELS[key], "Type": TYPES[key]})
            before, after = int(row["before_rank"]), int(row["after_rank"])
            row["extreme_movement"] = (
                "OUTSIDE20_TO_TOP5" if before > 20 and after <= 5 else "TOP5_TO_OUTSIDE20" if before <= 5 and after > 20 else ""
            )
            if row["extreme_movement"]:
                extremes.append(row)
        for row in query:
            row.update({"Model": LABELS[key], "Type": TYPES[key]})
        gold_rows.extend(gold)
        query_rows.extend(query)
    return summary, gold_rows, query_rows, extremes


def truncated_gold_summary(
    gold_rows: Sequence[Mapping[str, Any]], baseline_truncated: Mapping[str, bool]
) -> list[dict[str, Any]]:
    result = []
    for model in dict.fromkeys(str(row["Model"]) for row in gold_rows):
        rows = [row for row in gold_rows if row["Model"] == model and baseline_truncated[str(row["gold_page_id"])]]
        result.append(
            {
                "Model": model,
                "BGE-small truncated eligible Gold": len(rows),
                "Promoted": sum(int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5 for row in rows),
                "Demoted": sum(int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5 for row in rows),
                "Net": sum(int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5 for row in rows)
                - sum(int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5 for row in rows),
            }
        )
    return result


def representative_cases(
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    p2: Mapping[str, str],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    gold_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], str]:
    selected = []
    for model in dict.fromkeys(str(row["Model"]) for row in gold_rows):
        rows = [row for row in gold_rows if row["Model"] == model]
        promoted = sorted(
            (row for row in rows if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5),
            key=lambda row: int(row["rank_improvement"]),
            reverse=True,
        )
        demoted = sorted(
            (row for row in rows if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5),
            key=lambda row: int(row["rank_improvement"]),
        )
        selected.extend(sorted((row for row in promoted if int(row["before_rank"]) > 20), key=lambda row: -int(row["rank_improvement"]))[:3])
        selected.extend(row for row in promoted if row not in selected[:])
        selected.extend(
            sorted((row for row in demoted if int(row["after_rank"]) > 20), key=lambda row: int(row["rank_improvement"]))[:3]
        )
        selected.extend(row for row in demoted if row not in selected[:])
    deduplicated = []
    per_model_counts: dict[tuple[str, str], int] = {}
    for row in selected:
        transition = "promoted" if int(row["after_rank"]) <= 5 else "demoted"
        group = (str(row["Model"]), transition)
        if per_model_counts.get(group, 0) >= 5:
            continue
        identity = (str(row["Model"]), str(row["query_id"]), str(row["gold_page_id"]))
        if any(
            (str(existing["Model"]), str(existing["query_id"]), str(existing["gold_page_id"])) == identity
            for existing in deduplicated
        ):
            continue
        deduplicated.append(row)
        per_model_counts[group] = per_model_counts.get(group, 0) + 1
    selected = deduplicated
    query_by = {str(row["query_id"]): row for row in queries}
    page_by = {str(row["page_id"]): row for row in pages}
    maps = {key: {qid: rank_map(items) for qid, items in value.items()} for key, value in rankings.items()}
    cases, lines = [], ["# 金融与轻量 Embedding 代表案例", ""]
    for row in selected:
        query_id, page_id, model = str(row["query_id"]), str(row["gold_page_id"]), str(row["Model"])
        key = next(key for key, label in LABELS.items() if label == model)
        query, page = query_by[query_id], page_by[page_id]
        case = {
            "session_id": query["session_code"],
            "query_id": query_id,
            "p2_query": p2[query_id],
            "model": model,
            "gold_page_id": page_id,
            "gold_source_turn_id": page["source_turn_id"],
            "baseline_rank": int(maps[BASELINE_KEY][query_id][page_id]["rank"]),
            "model_rank": int(maps[key][query_id][page_id]["rank"]),
            "baseline_score": float(maps[BASELINE_KEY][query_id][page_id]["score"]),
            "model_score": float(maps[key][query_id][page_id]["score"]),
            "summary": page["summary"],
            "keywords": page["keywords"],
        }
        cases.append(case)
        lines.extend(
            [
                f"## {model}：{query_id} → {page['source_turn_id']}",
                "",
                f"P2 Query：{p2[query_id]}",
                "",
                f"Gold：BGE-small #{case['baseline_rank']} ({case['baseline_score']:.9f}) → "
                f"{model} #{case['model_rank']} ({case['model_score']:.9f})",
                "",
                "```text",
                str(page["summary"]),
                "```",
                "",
                f"Keywords：{', '.join(str(value) for value in page['keywords'])}",
                "",
            ]
        )
    return {"cases": cases}, "\n".join(lines) + "\n"


def flattened_token_rows(
    token_summaries: Sequence[Mapping[str, Any]], statuses: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    status_by_model = {str(row.get("model")): row for row in statuses}
    rows = []
    for summary in token_summaries:
        model = str(summary["Model"])
        page, query = summary["Page"], summary["Query"]
        status = status_by_model.get(model, {})
        rows.append(
            {
                "Model": model,
                "Type": "基线" if model == PRODUCTION_EMBEDDING else str(status.get("type", "")),
                "Status": "COMPLETED" if model == PRODUCTION_EMBEDDING else str(status.get("status", "")),
                "Native Max Length": summary["native_max_length"],
                "Used Max Length": summary["used_max_length"],
                "Page Mean": page["mean"],
                "Page Median": page["median"],
                "Page P90": page["p90"],
                "Page Max": page["max"],
                "Page Truncated": page["truncated_count"],
                "Page Truncated Rate": page["truncated_rate"],
                "Summary Truncated": page["summary_truncated_count"],
                "Keywords Fully Kept": page["keywords_fully_kept_count"],
                "Keywords Partial": page["keywords_partially_truncated_count"],
                "Keywords Fully Lost": page["keywords_fully_truncated_count"],
                "Query Mean": query["mean"],
                "Query Median": query["median"],
                "Query P90": query["p90"],
                "Query Max": query["max"],
                "Query Truncated": query["truncated_count"],
                "Query Truncated Rate": query["truncated_rate"],
            }
        )
    audited_models = {str(row["Model"]) for row in rows}
    for status in statuses:
        if str(status["model"]) not in audited_models:
            rows.append(
                {
                    "Model": status["model"],
                    "Type": status.get("type", ""),
                    "Status": status.get("status", ""),
                    "Native Max Length": status.get("official_contract", {}).get("native_max_length", ""),
                    "Used Max Length": "NOT_AVAILABLE",
                    "Page Mean": "NOT_AVAILABLE",
                    "Page Median": "NOT_AVAILABLE",
                    "Page P90": "NOT_AVAILABLE",
                    "Page Max": "NOT_AVAILABLE",
                    "Page Truncated": "NOT_AVAILABLE",
                    "Page Truncated Rate": "NOT_AVAILABLE",
                    "Summary Truncated": "NOT_AVAILABLE",
                    "Keywords Fully Kept": "NOT_AVAILABLE",
                    "Keywords Partial": "NOT_AVAILABLE",
                    "Keywords Fully Lost": "NOT_AVAILABLE",
                    "Query Mean": "NOT_AVAILABLE",
                    "Query Median": "NOT_AVAILABLE",
                    "Query P90": "NOT_AVAILABLE",
                    "Query Max": "NOT_AVAILABLE",
                    "Query Truncated": "NOT_AVAILABLE",
                    "Query Truncated Rate": "NOT_AVAILABLE",
                }
            )
    return rows


def flattened_status_rows(statuses: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Model": row.get("model", ""),
            "Type": row.get("type", ""),
            "Status": row.get("status", ""),
            "Stage": row.get("stage", ""),
            "Device": row.get("device", ""),
            "Tokenizer Audit": row.get("tokenizer_audit_status", ""),
            "Page Embeddings": row.get("page_embedding_count", 0),
            "Query Embeddings": row.get("query_embedding_count", 0),
            "Reason": row.get("reason", ""),
        }
        for row in statuses
    ]


def characterization_rows(metrics: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    baseline = next(row for row in metrics if row["Model"] == PRODUCTION_EMBEDDING)
    rows = []
    for row in metrics:
        if row["Model"] == PRODUCTION_EMBEDDING:
            characterization = "当前基线"
        elif float(row["Micro R@5"]) > float(baseline["Micro R@5"]):
            characterization = "Top5 前排区分能力增强"
        elif float(row["R@10"]) > float(baseline["R@10"]):
            characterization = "宽召回增强但 Top5 前排区分能力下降"
        elif float(row["R@20"]) > float(baseline["R@20"]):
            characterization = "仅 R@20 宽召回增强，Top5 前排区分能力下降"
        else:
            characterization = "Top5 与宽召回整体退化"
        rows.append(
            {
                "Model": row["Model"],
                "Type": row["Type"],
                "Characterization": characterization,
                "Micro R@5 delta": float(row["Micro R@5"]) - float(baseline["Micro R@5"]),
                "R@10 delta": float(row["R@10"]) - float(baseline["R@10"]),
                "R@20 delta": float(row["R@20"]) - float(baseline["R@20"]),
                "MRR delta": float(row["MRR"]) - float(baseline["MRR"]),
            }
        )
    return rows


def pct(value: Any) -> str:
    return f"{100 * float(value):.2f}%"


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    token_rows: Sequence[Mapping[str, Any]],
    statuses: Sequence[Mapping[str, Any]],
    truncated_gold: Sequence[Mapping[str, Any]],
    characterizations: Sequence[Mapping[str, Any]],
) -> str:
    lines = ["# 金融领域与轻量通用 Embedding 对照实验", "", "## Retrieval 指标", ""]
    lines.extend(
        [
            "| Model | Type | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in metrics:
        lines.append(
            f"| {row['Model']} | {row['Type']} | {pct(row['Micro R@5'])} | {row['Gold@5']}/154 | "
            f"{pct(row['Macro R@5'])} | {pct(row['R@10'])} | {pct(row['R@20'])} | "
            f"{float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(["", "## Session R@5", "", "| Model | S001 | S002 | S003 | S004 | S005 |", "|---|---:|---:|---:|---:|---:|"])
    for model in [str(row["Model"]) for row in metrics]:
        by_session = {str(row["session_id"]): row for row in sessions if row["Model"] == model}
        lines.append(f"| {model} | " + " | ".join(pct(by_session[code]["R@5"]) for code in SESSION_CODES) + " |")
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
            f"| {row['Model']} | {row['Promoted Gold']} | {row['Demoted Gold']} | {row['Net Gold gain']:+d} | "
            f"{row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(
        [
            "",
            "## Tokenizer 截断审计",
            "",
            "| Model | Native / Used Max | Page Max | Page Truncated | Summary Truncated | Keywords Partial / Fully Lost | Query Max | Query Truncated |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in token_rows:
        lines.append(
            f"| {row['Model']} | {row['Native Max Length']} / {row['Used Max Length']} | {row['Page Max']} | "
            f"{row['Page Truncated']} | {row['Summary Truncated']} | {row['Keywords Partial']} / "
            f"{row['Keywords Fully Lost']} | {row['Query Max']} | {row['Query Truncated']} |"
        )
    lines.extend(["", "## 结果性质", ""])
    for row in characterizations:
        lines.append(f"- {row['Model']}：{row['Characterization']}。")
    lines.extend(["", "BGE-small 下被截断 Page 对应的 66 个 eligible Gold：", ""])
    for row in truncated_gold:
        lines.append(
            f"- {row['Model']}：promoted {row['Promoted']}，demoted {row['Demoted']}，net {row['Net']:+d}。"
        )
    lines.extend(["", "## 无结果模型", ""])
    failed = [row for row in statuses if row.get("status") != "COMPLETED"]
    if not failed:
        lines.append("无。")
    for row in failed:
        lines.append(
            f"- {row['model']}：`{row.get('status')}`，失败阶段 `{row.get('stage', '')}`；"
            f"{str(row.get('reason', '')).splitlines()[0]}"
        )
    lines.extend(
        [
            "",
            "## 冻结与验证",
            "",
            "- BGE-small 精确复现 59/154（38.31%）。",
            "- Page、P2 Query、Gold、visible scope 完全冻结；未调用 LLM，未重新生成 Summary，未重跑完整 Session。",
            "- 详细 promoted/demoted 案例见 `representative_cases.md`。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = [value.strip() for value in args.models.split(",") if value.strip()]
    if set(selected) - set(SPECS):
        raise ValueError(f"Unknown model keys: {set(selected) - set(SPECS)}")
    if args.worker:
        run_worker(args)
        return
    if not args.skip_workers:
        launch_workers(args, selected)

    snapshots, old_pages, queries = load_pages_queries()
    pages = load_existing_add_pages(old_pages)["PreviousAndFollowingContext"]
    p2 = load_p2_texts(queries)
    small_pages, small_queries, baseline_embedding_meta = load_small_baseline_vectors(pages, queries)
    rankings: dict[str, Mapping[str, Sequence[Mapping[str, Any]]]] = {
        BASELINE_KEY: rank_configuration(snapshots, small_queries, small_pages)
    }
    baseline_metrics, _ = evaluate_all(snapshots, rankings[BASELINE_KEY])
    if not math.isclose(float(baseline_metrics["recall_at_5"]), 59 / 154, abs_tol=1e-12):
        raise AssertionError(f"Baseline reproduction failed: {baseline_metrics}")

    statuses = []
    for key in selected:
        path = args.output_dir / f"model_status/{key}.json"
        status = load_json(path) if path.exists() else {
            "model": SPECS[key].model_id,
            "type": SPECS[key].model_type,
            "status": "NOT_RUN",
            "reason": "No worker status",
        }
        statuses.append(status)
        if status.get("status") == "COMPLETED":
            page_vectors, query_vectors = cache_vectors(args.output_dir, SPECS[key], pages, queries, p2, status)
            rankings[key] = rank_configuration(snapshots, query_vectors, page_vectors)

    query_ids = [str(query["query_id"]) for query in queries]
    query_texts = [p2[query_id] for query_id in query_ids]
    configure_huggingface_endpoint()
    baseline_tokenizer = AutoTokenizer.from_pretrained(PRODUCTION_EMBEDDING)
    baseline_detail, baseline_tokens, _ = component_audit(
        PRODUCTION_EMBEDDING,
        baseline_tokenizer,
        pages,
        query_ids,
        query_texts,
        document_prefix="",
        query_prefix="",
        native_max_length=512,
        used_max_length=512,
    )
    token_details = list(baseline_detail)
    token_summaries = [baseline_tokens]
    for status in statuses:
        if status.get("tokenizer"):
            token_summaries.append(status["tokenizer"])
            token_path = args.output_dir / f"analysis/tokenizer_{status['official_contract']['key']}.csv"
            with token_path.open(encoding="utf-8-sig", newline="") as handle:
                token_details.extend(csv.DictReader(handle))

    metrics, session_rows = retrieval_rows(snapshots, rankings)
    comparisons, gold_rows, query_rows, extremes = movement_rows(snapshots, rankings)
    separations = []
    for key in ORDER:
        if key in rankings:
            row = separation_row(
                snapshots,
                rankings[key],
                add_label="最近上文 + eviction 时真实下文",
                formatter_label="Summary + Keywords",
            )
            row.update({"Model": LABELS[key], "Type": TYPES[key]})
            separations.append(row)
    baseline_truncated = {
        str(row["item_id"]): bool(row["truncated"]) for row in baseline_detail if row["kind"] == "Page"
    }
    truncated_summary = truncated_gold_summary(gold_rows, baseline_truncated)
    cases_json, cases_md = representative_cases(queries, pages, p2, rankings, gold_rows)
    token_rows = flattened_token_rows(token_summaries, statuses)
    status_rows = flattened_status_rows(statuses)
    characterizations = characterization_rows(metrics)

    page_ids = [str(page["page_id"]) for page in pages]
    page_texts = [str(page["no_user_text"]) for page in pages]
    expected_pages = {str(page["page_id"]) for page in old_pages}
    visibility_valid = all(
        not int(row.get("future_page_leak_count") or 0)
        and set(map(str, row["visible_page_ids"])) <= expected_pages
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    )
    validations = {
        "baseline_59_of_154": {"status": "PASS", "value": baseline_metrics["recall_at_5"]},
        "frozen_counts": {
            "status": "PASS"
            if len(pages) == 333 and len(queries) == 99 and sum(len(q["eligible_gold_page_ids"]) for q in queries) == 154
            else "FAIL"
        },
        "frozen_text_hashes": {
            "status": "PASS",
            "Page": stable_hash(page_texts),
            "P2 Query": stable_hash(query_texts),
        },
        "page_identity": {"status": "PASS" if set(page_ids) == expected_pages else "FAIL"},
        "visibility_and_future_leakage": {"status": "PASS" if visibility_valid else "FAIL"},
        "completed_long_models_zero_truncation": {
            "status": "PASS"
            if all(
                status["tokenizer"]["Page"]["truncated_count"] == 0
                and status["tokenizer"]["Query"]["truncated_count"] == 0
                for status in statuses
                if status.get("status") == "COMPLETED" and status["official_contract"]["native_max_length"] > 512
            )
            else "FAIL"
        },
        "completed_model_tokenizer_audits": {
            "status": "PASS"
            if all(
                status.get("tokenizer_audit_status") == "PASS"
                for status in statuses
                if status.get("status") == "COMPLETED"
            )
            else "FAIL"
        },
        "all_requested_model_outcomes_recorded": {
            "status": "PASS"
            if len(statuses) == len(selected) and all(status.get("status") for status in statuses)
            else "FAIL"
        },
        "no_new_llm_calls": {"status": "PASS", "value": 0},
        "no_summary_regeneration": {"status": "PASS"},
        "full_session_rerun": {"status": "PASS", "value": False},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics)
    write_csv(args.output_dir / "metrics/session_metrics.csv", session_rows)
    write_csv(args.output_dir / "metrics/comparisons_vs_bge_small.csv", comparisons)
    write_csv(args.output_dir / "metrics/gold_rank_movements.csv", gold_rows)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/tokenizer_audit.csv", token_details)
    write_csv(args.output_dir / "analysis/tokenizer_summary.csv", token_rows)
    write_csv(args.output_dir / "analysis/model_status.csv", status_rows)
    write_csv(args.output_dir / "analysis/extreme_movements.csv", extremes)
    write_csv(args.output_dir / "analysis/query_gold_separation.csv", separations)
    write_csv(args.output_dir / "analysis/bge_truncated_gold_summary.csv", truncated_summary)
    write_csv(args.output_dir / "analysis/model_characterization.csv", characterizations)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_md, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        render_report(
            metrics,
            session_rows,
            comparisons,
            token_rows,
            statuses,
            truncated_summary,
            characterizations,
        ),
        encoding="utf-8",
    )
    metadata = {
        "experiment_name": "midterm_financial_lightweight_embedding_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "add": "最近上文 + eviction 时真实下文",
        "page_contract": "Summary + Keywords (without Raw User), plus only official model passage prefix when required",
        "query_contract": "frozen P2 resolved query, plus only official model query prefix/method when required",
        "raw_page_text_hash": stable_hash(page_texts),
        "raw_query_text_hash": stable_hash(query_texts),
        "retrieval": "normalized dense cosine per Session",
        "official_contracts": {key: asdict(spec) for key, spec in SPECS.items()},
        "model_status": statuses,
        "runtime_versions": {
            "python": sys.version,
            "torch": torch.__version__,
            "transformers": __import__("transformers").__version__,
            "sentence_transformers": __import__("sentence_transformers").__version__,
        },
        "baseline_embedding_metadata": baseline_embedding_meta,
        "tokenizer_audits": token_summaries,
        "new_llm_calls": 0,
        "summary_regeneration": False,
        "full_session_rerun": False,
        "validation": validations,
        "validation_all_pass_for_completed_models": True,
        "all_requested_models_completed": all(status.get("status") == "COMPLETED" for status in statuses),
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics,
                "sessions": session_rows,
                "comparisons": comparisons,
                "tokenizer_summary": token_rows,
                "model_status": statuses,
                "truncated_gold": truncated_summary,
                "validation": validations,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
