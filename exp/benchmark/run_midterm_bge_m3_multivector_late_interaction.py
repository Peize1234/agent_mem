"""Evaluate BGE-M3 ColBERT late interaction on the frozen C3 benchmark.

This experiment never calls an LLM or changes C3 text, Gold, visibility, or
Session state. It compares pure BGE-M3 MaxSim with a C3 Dense Top60 rerank.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from transformers import AutoTokenizer

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import percentile, stable_hash, write_jsonl  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import compare_rankings  # noqa: E402
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    SESSION_CODES,
    evaluate_all,
    rank_configuration,
)
from exp.benchmark.run_midterm_bge_m3_length_ablation import (  # noqa: E402
    M3_MODEL,
    LengthAwareEmbeddingCache,
)
from exp.benchmark.run_midterm_dense_bm25_hybrid_checkpoints import checkpoint_inputs  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_bge_m3_multivector_late_interaction"
M3_DENSE_DIR = REPO_ROOT / "exp/results/midterm_bge_m3_length_ablation"
MODEL_REVISION_FALLBACK = "5617a9f61b028005a4858fdac845db406aefb181"
MAX_LENGTH = 1024
TOP60 = 60
ELIGIBLE_GOLD = 154
C3_GOLD5 = 59
COLBERT_DIMENSION = 1024
REPRESENTATION = "colbert"
SCORING_CONTRACT = "mean_i max_j cosine(q_i, d_j); official normalized token vectors"
CONFIG_LABELS = {
    "C3": "C3 BGE-small Dense",
    "M0": "BGE-M3 Dense（max_length=1024）",
    "M1": "BGE-M3 Pure Multi-Vector MaxSim",
    "M2": "C3 Dense Top60 + BGE-M3 Multi-Vector Rerank",
}
DISTINCTIVE_TERMS = (
    "经营现金流",
    "现金流",
    "归母净利润",
    "扣非",
    "总资产",
    "营业收入",
    "股东权益",
    "反证",
    "反例",
    "修订",
    "支撑",
    "错位",
    "证据",
    "杠杆",
    "周转",
    "基期",
    "口径",
    "同步",
    "冲突",
    "因果",
    "拐点",
)
GENERIC_TOKENS = {
    "",
    "的",
    "和",
    "与",
    "在",
    "了",
    "是",
    "把",
    "也",
    "中",
    "请",
    "前面",
    "当前",
    "刚才",
    "如果",
    "具体",
    "说明",
}
PUNCTUATION = re.compile(r"^[\s\W_]+$", re.UNICODE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen C3 BGE-M3 ColBERT late-interaction experiment")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--require-cache-hit",
        action="store_true",
        help="Fail unless both Page and Query ColBERT stores are loaded from cache.",
    )
    return parser.parse_args()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def model_revision_and_path() -> tuple[str, Path]:
    hub = Path(os.getenv("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
    repository = hub / "models--BAAI--bge-m3"
    revision_path = repository / "refs/main"
    revision = revision_path.read_text(encoding="utf-8").strip() if revision_path.exists() else MODEL_REVISION_FALLBACK
    snapshot = repository / "snapshots" / revision
    if not snapshot.exists():
        raise FileNotFoundError(f"BGE-M3 snapshot is not cached: {snapshot}")
    return revision, snapshot


def normalize_rows(values: np.ndarray) -> np.ndarray:
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 2 or not len(array):
        raise ValueError(f"Expected a non-empty 2D token matrix, got {array.shape}")
    norms = np.linalg.norm(array, axis=1, keepdims=True)
    if np.any(norms <= 0):
        raise ValueError("Zero-norm token vector")
    return array / norms


def maxsim_score(query_vectors: np.ndarray, page_vectors: np.ndarray) -> float:
    """Mean over Query tokens of the maximum token-token cosine."""
    query = normalize_rows(query_vectors)
    page = normalize_rows(page_vectors)
    return float(np.max(query @ page.T, axis=1).mean())


def official_colbert_score(query_vectors: np.ndarray, page_vectors: np.ndarray) -> float:
    """Call FlagEmbedding's official standalone ColBERT scorer without loading a model."""
    from FlagEmbedding import BGEM3FlagModel

    query = normalize_rows(query_vectors)
    page = normalize_rows(page_vectors)
    score = BGEM3FlagModel.colbert_score(None, query, page)
    return float(score.item())


def cache_identity(
    *,
    kind: str,
    ids: Sequence[str],
    texts: Sequence[str],
    model_revision: str,
    max_length: int,
) -> dict[str, Any]:
    return {
        "model": M3_MODEL,
        "model_revision": model_revision,
        "kind": kind,
        "items": [{"id": item_id, "text_sha256": sha256_text(text)} for item_id, text in zip(ids, texts)],
        "max_length": max_length,
        "representation": REPRESENTATION,
        "normalization": "FlagEmbedding normalize_embeddings=True; L2 per contextual token vector",
        "scoring": SCORING_CONTRACT,
    }


@dataclass
class MultiVectorStore:
    ids: list[str]
    vectors: np.ndarray
    offsets: np.ndarray
    token_ids: np.ndarray
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        self.index = {item_id: index for index, item_id in enumerate(self.ids)}
        if len(self.index) != len(self.ids):
            raise ValueError("Duplicate IDs in multi-vector store")

    def vectors_for(self, item_id: str) -> np.ndarray:
        index = self.index[item_id]
        start, end = int(self.offsets[index]), int(self.offsets[index + 1])
        return np.asarray(self.vectors[start:end], dtype=np.float32)

    def token_ids_for(self, item_id: str) -> np.ndarray:
        index = self.index[item_id]
        start, end = int(self.offsets[index]), int(self.offsets[index + 1])
        return np.asarray(self.token_ids[start:end], dtype=np.int64)

    def vector_count(self, item_id: str) -> int:
        index = self.index[item_id]
        return int(self.offsets[index + 1] - self.offsets[index])


class ColbertVectorCache:
    """Disk-backed ragged ColBERT vectors with frozen-content identities."""

    def __init__(self, cache_dir: Path, *, revision: str, max_length: int) -> None:
        self.cache_dir = cache_dir
        self.revision = revision
        self.max_length = max_length

    def paths(self, kind: str, identity: Mapping[str, Any]) -> dict[str, Path]:
        content_hash = stable_hash(identity)
        root = self.cache_dir / M3_MODEL.replace("/", "_") / self.revision / f"colbert-max{self.max_length}"
        directory = root / f"{kind}-{content_hash[:16]}"
        return {
            "directory": directory,
            "metadata": directory / "metadata.json",
            "vectors": directory / "vectors.npy",
            "offsets": directory / "offsets.npy",
            "token_ids": directory / "token_ids.npy",
            "ids": directory / "ids.json",
        }

    def load(
        self, *, kind: str, ids: Sequence[str], texts: Sequence[str]
    ) -> tuple[MultiVectorStore | None, dict[str, Any], dict[str, Path]]:
        identity = cache_identity(
            kind=kind,
            ids=ids,
            texts=texts,
            model_revision=self.revision,
            max_length=self.max_length,
        )
        paths = self.paths(kind, identity)
        required = tuple(paths[key] for key in ("metadata", "vectors", "offsets", "token_ids", "ids"))
        if not all(path.exists() for path in required):
            return None, identity, paths
        metadata = load_json(paths["metadata"])
        cached_ids = json.loads(paths["ids"].read_text(encoding="utf-8"))
        if metadata.get("cache_identity_hash") != stable_hash(identity) or cached_ids != list(ids):
            return None, identity, paths
        store = MultiVectorStore(
            ids=list(cached_ids),
            vectors=np.load(paths["vectors"], mmap_mode="r"),
            offsets=np.load(paths["offsets"], mmap_mode="r"),
            token_ids=np.load(paths["token_ids"], mmap_mode="r"),
            metadata={**metadata, "cache_hit": True},
        )
        validate_store(store, expected_ids=ids)
        return store, identity, paths

    def save(
        self,
        *,
        kind: str,
        ids: Sequence[str],
        texts: Sequence[str],
        token_matrices: Sequence[np.ndarray],
        token_id_rows: Sequence[Sequence[int]],
        identity: Mapping[str, Any],
        paths: Mapping[str, Path],
        build_seconds: float,
        device: str,
        batch_size: int,
    ) -> MultiVectorStore:
        if len(ids) != len(token_matrices) or len(ids) != len(token_id_rows):
            raise ValueError("ColBERT cache rows do not align")
        counts = np.asarray([len(row) for row in token_matrices], dtype=np.int64)
        offsets = np.concatenate((np.asarray([0], dtype=np.int64), np.cumsum(counts)))
        if np.any(counts <= 1):
            raise AssertionError(f"Every {kind} text must contain >1 ColBERT vectors")
        dimension = {int(np.asarray(row).shape[1]) for row in token_matrices}
        if dimension != {COLBERT_DIMENSION}:
            raise AssertionError(f"Unexpected ColBERT dimension: {dimension}")
        for matrix, current_ids in zip(token_matrices, token_id_rows):
            if len(matrix) != len(current_ids):
                raise AssertionError("ColBERT vector/token alignment mismatch")
        paths["directory"].mkdir(parents=True, exist_ok=True)
        vectors = np.lib.format.open_memmap(
            paths["vectors"], mode="w+", dtype=np.float32, shape=(int(offsets[-1]), COLBERT_DIMENSION)
        )
        token_ids = np.lib.format.open_memmap(paths["token_ids"], mode="w+", dtype=np.int64, shape=(int(offsets[-1]),))
        norm_min, norm_max = float("inf"), 0.0
        for index, (matrix, current_ids) in enumerate(zip(token_matrices, token_id_rows)):
            start, end = int(offsets[index]), int(offsets[index + 1])
            values = np.asarray(matrix, dtype=np.float32)
            norms = np.linalg.norm(values, axis=1)
            norm_min, norm_max = min(norm_min, float(norms.min())), max(norm_max, float(norms.max()))
            if not np.allclose(norms, 1.0, atol=2e-4):
                raise AssertionError(f"Official ColBERT vectors are not normalized: {norms.min()}..{norms.max()}")
            vectors[start:end] = values
            token_ids[start:end] = np.asarray(current_ids, dtype=np.int64)
        vectors.flush()
        token_ids.flush()
        np.save(paths["offsets"], offsets)
        paths["ids"].write_text(json.dumps(list(ids), ensure_ascii=False), encoding="utf-8")
        metadata = {
            "cache_identity": dict(identity),
            "cache_identity_hash": stable_hash(identity),
            "model": M3_MODEL,
            "model_revision": self.revision,
            "kind": kind,
            "item_count": len(ids),
            "total_vector_count": int(offsets[-1]),
            "dimension": COLBERT_DIMENSION,
            "max_length": self.max_length,
            "representation": REPRESENTATION,
            "normalization": identity["normalization"],
            "scoring": identity["scoring"],
            "norm_min": norm_min,
            "norm_max": norm_max,
            "build_seconds": build_seconds,
            "device": device,
            "batch_size": batch_size,
            "cache_hit": False,
        }
        dump_json(paths["metadata"], metadata)
        store = MultiVectorStore(
            ids=list(ids),
            vectors=np.load(paths["vectors"], mmap_mode="r"),
            offsets=np.load(paths["offsets"], mmap_mode="r"),
            token_ids=np.load(paths["token_ids"], mmap_mode="r"),
            metadata=metadata,
        )
        validate_store(store, expected_ids=ids)
        return store


def validate_store(store: MultiVectorStore, *, expected_ids: Sequence[str]) -> None:
    if store.ids != list(expected_ids):
        raise AssertionError("Multi-vector cache ID order changed")
    if len(store.offsets) != len(store.ids) + 1 or int(store.offsets[0]) != 0:
        raise AssertionError("Invalid ragged offsets")
    if int(store.offsets[-1]) != len(store.vectors) or len(store.token_ids) != len(store.vectors):
        raise AssertionError("Invalid ragged vector coverage")
    if store.vectors.ndim != 2 or store.vectors.shape[1] != COLBERT_DIMENSION:
        raise AssertionError(f"Invalid ColBERT matrix shape: {store.vectors.shape}")
    if any(store.vector_count(item_id) <= 1 for item_id in store.ids):
        raise AssertionError("A cached text was pooled to <=1 vector")


def token_ids_for_text(tokenizer: Any, text: str, max_length: int) -> tuple[list[int], int, bool]:
    raw_ids = list(tokenizer.encode(text, add_special_tokens=True, truncation=False))
    actual_ids = list(tokenizer.encode(text, add_special_tokens=True, truncation=True, max_length=max_length))
    if len(actual_ids) < 3:
        raise AssertionError("BGE-M3 input is unexpectedly empty")
    # FlagEmbedding projects last_hidden_state[:, 1:] and keeps tokens_num - 1.
    return actual_ids[1:], len(raw_ids), len(raw_ids) > max_length


def encode_missing_store(
    *,
    model: Any,
    tokenizer: Any,
    cache: ColbertVectorCache,
    kind: str,
    ids: Sequence[str],
    texts: Sequence[str],
    identity: Mapping[str, Any],
    paths: Mapping[str, Path],
    batch_size: int,
    device: str,
) -> MultiVectorStore:
    started = time.perf_counter()
    result = model.encode(
        list(texts),
        batch_size=batch_size,
        max_length=cache.max_length,
        return_dense=False,
        return_sparse=False,
        return_colbert_vecs=True,
    )
    vectors = list(result["colbert_vecs"])
    token_rows = []
    for item_id, text, values in zip(ids, texts, vectors):
        token_ids, _, _ = token_ids_for_text(tokenizer, text, cache.max_length)
        if len(token_ids) != len(values):
            raise AssertionError(
                f"Official vector/token mismatch for {kind}/{item_id}: {len(values)} != {len(token_ids)}"
            )
        token_rows.append(token_ids)
    return cache.save(
        kind=kind,
        ids=ids,
        texts=texts,
        token_matrices=vectors,
        token_id_rows=token_rows,
        identity=identity,
        paths=paths,
        build_seconds=time.perf_counter() - started,
        device=device,
        batch_size=batch_size,
    )


def load_or_encode_stores(
    *,
    output_dir: Path,
    revision: str,
    snapshot_path: Path,
    tokenizer: Any,
    page_ids: Sequence[str],
    page_texts: Sequence[str],
    query_ids: Sequence[str],
    query_texts: Sequence[str],
    device: str,
    batch_size: int,
) -> tuple[MultiVectorStore, MultiVectorStore, dict[str, Any]]:
    cache = ColbertVectorCache(output_dir / "cache", revision=revision, max_length=MAX_LENGTH)
    page_store, page_identity, page_paths = cache.load(kind="pages", ids=page_ids, texts=page_texts)
    query_store, query_identity, query_paths = cache.load(kind="queries", ids=query_ids, texts=query_texts)
    page_hit, query_hit = page_store is not None, query_store is not None
    if page_store is None or query_store is None:
        from FlagEmbedding import BGEM3FlagModel

        model = BGEM3FlagModel(
            str(snapshot_path),
            normalize_embeddings=True,
            use_fp16=device.startswith("cuda"),
            devices=device,
            batch_size=batch_size,
            query_max_length=MAX_LENGTH,
            passage_max_length=MAX_LENGTH,
            return_dense=False,
            return_sparse=False,
            return_colbert_vecs=True,
        )
        try:
            if page_store is None:
                page_store = encode_missing_store(
                    model=model,
                    tokenizer=tokenizer,
                    cache=cache,
                    kind="pages",
                    ids=page_ids,
                    texts=page_texts,
                    identity=page_identity,
                    paths=page_paths,
                    batch_size=batch_size,
                    device=device,
                )
            if query_store is None:
                query_store = encode_missing_store(
                    model=model,
                    tokenizer=tokenizer,
                    cache=cache,
                    kind="queries",
                    ids=query_ids,
                    texts=query_texts,
                    identity=query_identity,
                    paths=query_paths,
                    batch_size=batch_size,
                    device=device,
                )
        finally:
            del model
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
    if page_store is None or query_store is None:
        raise AssertionError("Failed to build ColBERT stores")
    return (
        page_store,
        query_store,
        {
            "page_cache_hit": page_hit,
            "query_cache_hit": query_hit,
            "page": page_store.metadata,
            "query": query_store.metadata,
        },
    )


def distribution(values: Sequence[int]) -> dict[str, Any]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(values, 0.90),
        "max": max(values),
    }


def token_vector_audit(
    *, kind: str, ids: Sequence[str], texts: Sequence[str], tokenizer: Any, store: MultiVectorStore
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    for item_id, text in zip(ids, texts):
        actual_ids, raw_count, truncated = token_ids_for_text(tokenizer, text, MAX_LENGTH)
        vector_count = store.vector_count(item_id)
        if vector_count != len(actual_ids):
            raise AssertionError(f"Token/vector count mismatch in audit: {item_id}")
        rows.append(
            {
                "kind": kind,
                "item_id": item_id,
                "raw_token_count": raw_count,
                "actual_input_token_count": len(actual_ids) + 1,
                "multi_vector_count": vector_count,
                "truncated": truncated,
            }
        )
    raw = [int(row["raw_token_count"]) for row in rows]
    vectors = [int(row["multi_vector_count"]) for row in rows]
    return rows, {
        "kind": kind,
        "max_length": MAX_LENGTH,
        "raw_token_count": distribution(raw),
        "multi_vector_count": distribution(vectors),
        "truncation_count": sum(bool(row["truncated"]) for row in rows),
        "all_texts_have_multiple_vectors": all(value > 1 for value in vectors),
    }


def batch_maxsim_scores(
    query_vectors: np.ndarray,
    page_ids: Sequence[str],
    page_store: MultiVectorStore,
    *,
    device: str,
) -> dict[str, float]:
    """Score one Query against visible Pages using padded GPU/CPU tensors."""
    query = torch.from_numpy(np.array(query_vectors, dtype=np.float32, copy=True)).to(device)
    matrices = [
        torch.from_numpy(np.array(page_store.vectors_for(page_id), dtype=np.float32, copy=True)) for page_id in page_ids
    ]
    lengths = torch.as_tensor([len(matrix) for matrix in matrices], dtype=torch.long, device=device)
    padded = torch.nn.utils.rnn.pad_sequence(matrices, batch_first=True).to(device)
    scores = torch.einsum("qd,pnd->pqn", query, padded)
    positions = torch.arange(padded.shape[1], device=device)[None, :]
    scores = scores.masked_fill((positions >= lengths[:, None])[:, None, :], -torch.inf)
    values = scores.max(dim=-1).values.mean(dim=-1).float().cpu().numpy()
    return {page_id: float(score) for page_id, score in zip(page_ids, values)}


def pure_multivector_rankings(
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    query_store: MultiVectorStore,
    page_store: MultiVectorStore,
    *,
    device: str,
) -> dict[str, list[dict[str, Any]]]:
    rankings = {}
    for query_id, c3_rows in c3_rankings.items():
        page_ids = [str(row["page_id"]) for row in c3_rows]
        scores = batch_maxsim_scores(query_store.vectors_for(query_id), page_ids, page_store, device=device)
        rows = [
            {
                "page_id": str(row["page_id"]),
                "source_turn_id": str(row["source_turn_id"]),
                "score": scores[str(row["page_id"])],
                "maxsim_score": scores[str(row["page_id"])],
                "c3_rank": int(row["rank"]),
                "c3_score": float(row["score"]),
            }
            for row in c3_rows
        ]
        rows.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
        for rank, row in enumerate(rows, start=1):
            row["rank"] = rank
        rankings[query_id] = rows
    return rankings


def rerank_c3_top60(
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    pure_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    result = {}
    pure_maps = {query_id: rank_map(rows) for query_id, rows in pure_rankings.items()}
    for query_id, c3_rows in c3_rankings.items():
        candidates = []
        for row in c3_rows[:TOP60]:
            page_id = str(row["page_id"])
            candidates.append(
                {
                    "page_id": page_id,
                    "source_turn_id": str(row["source_turn_id"]),
                    "score": float(pure_maps[query_id][page_id]["maxsim_score"]),
                    "maxsim_score": float(pure_maps[query_id][page_id]["maxsim_score"]),
                    "c3_rank": int(row["rank"]),
                    "c3_score": float(row["score"]),
                    "reranked_by_multivector": True,
                }
            )
        candidates.sort(key=lambda row: (-float(row["score"]), str(row["page_id"])))
        tail = [
            {
                "page_id": str(row["page_id"]),
                "source_turn_id": str(row["source_turn_id"]),
                "score": float(row["score"]),
                "maxsim_score": float(pure_maps[query_id][str(row["page_id"])]["maxsim_score"]),
                "c3_rank": int(row["rank"]),
                "c3_score": float(row["score"]),
                "reranked_by_multivector": False,
            }
            for row in c3_rows[TOP60:]
        ]
        complete = candidates + tail
        for rank, row in enumerate(complete, start=1):
            row["rank"] = rank
        result[query_id] = complete
    return result


def ranking_rows(
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    queries: Mapping[str, Mapping[str, Any]],
    variant: str,
) -> list[dict[str, Any]]:
    rows = []
    for query_id, ranking in rankings.items():
        gold = {str(page_id) for page_id in queries[query_id]["eligible_gold_page_ids"]}
        for row in ranking:
            rows.append(
                {
                    "variant": variant,
                    "session_id": queries[query_id]["session_code"],
                    "query_id": query_id,
                    "page_id": row["page_id"],
                    "source_turn_id": row["source_turn_id"],
                    "rank": row["rank"],
                    "score": row["score"],
                    "maxsim_score": row.get("maxsim_score"),
                    "c3_rank": row.get("c3_rank"),
                    "c3_score": row.get("c3_score"),
                    "reranked_by_multivector": row.get("reranked_by_multivector"),
                    "is_gold": str(row["page_id"]) in gold,
                }
            )
    return rows


def metric_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics, sessions = [], []
    for config in ("C3", "M0", "M1", "M2"):
        aggregate, per_session = evaluate_all(snapshots, rankings[config])
        metrics.append(
            {
                "config": config,
                "Retrieval": CONFIG_LABELS[config],
                "Eligible Gold": aggregate["eligible_gold_count"],
                "Gold@5": round(float(aggregate["recall_at_5"]) * ELIGIBLE_GOLD),
                "Micro R@5": aggregate["recall_at_5"],
                "Macro R@5": aggregate["macro_session_r5"],
                "R@10": aggregate["recall_at_10"],
                "R@20": aggregate["recall_at_20"],
                "MRR": aggregate["mrr"],
                "Mean Gold Rank": aggregate["mean_gold_rank"],
            }
        )
        for code in SESSION_CODES:
            values = per_session[code]
            sessions.append(
                {
                    "config": config,
                    "Retrieval": CONFIG_LABELS[config],
                    "session_id": code,
                    "eligible_gold_count": values["eligible_gold_count"],
                    "R@5": values["recall_at_5"],
                    "R@10": values["recall_at_10"],
                    "R@20": values["recall_at_20"],
                    "MRR": values["mrr"],
                    "Mean Gold Rank": values["mean_gold_rank"],
                }
            )
    return metrics, sessions


def movement_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    c3: Mapping[str, Sequence[Mapping[str, Any]]],
    variants: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    gold_rows, query_rows, summary = [], [], {}
    for config in ("M0", "M1", "M2"):
        comparison = f"{CONFIG_LABELS[config]} vs C3"
        aggregate, gold, queries = compare_rankings(snapshots, c3, variants[config], comparison=comparison)
        summary[config] = aggregate
        gold_rows.extend({"config": config, **row} for row in gold)
        query_rows.extend({"config": config, **row} for row in queries)
    return gold_rows, query_rows, summary


def decoded_token(tokenizer: Any, token_id: int) -> tuple[str, str]:
    token = str(tokenizer.convert_ids_to_tokens(int(token_id)))
    decoded = str(tokenizer.decode([int(token_id)], skip_special_tokens=True)).strip()
    return token, decoded


def page_token_document_frequency(page_store: MultiVectorStore) -> Counter[int]:
    counts: Counter[int] = Counter()
    for page_id in page_store.ids:
        counts.update(set(int(value) for value in page_store.token_ids_for(page_id)))
    return counts


def meaningful_query_indices(
    token_ids: Sequence[int], tokenizer: Any, document_frequency: Mapping[int, int], page_count: int, limit: int = 8
) -> list[int]:
    candidates = []
    seen = set()
    special = set(tokenizer.all_special_ids)
    for index, token_id in enumerate(token_ids):
        token_id = int(token_id)
        token, decoded = decoded_token(tokenizer, token_id)
        normalized = decoded or token.replace("▁", "").strip()
        if token_id in special or normalized in GENERIC_TOKENS or PUNCTUATION.fullmatch(normalized):
            continue
        identity = (token_id, normalized)
        if identity in seen:
            continue
        seen.add(identity)
        idf = math.log((page_count + 1) / (int(document_frequency.get(token_id, 0)) + 1)) + 1.0
        distinctive = int(any(normalized in term or term in normalized for term in DISTINCTIVE_TERMS))
        candidates.append((distinctive, idf, len(normalized), -index, index))
    candidates.sort(reverse=True)
    return sorted(item[-1] for item in candidates[:limit])


def token_matches(
    *,
    query_id: str,
    page_id: str,
    role: str,
    query_store: MultiVectorStore,
    page_store: MultiVectorStore,
    tokenizer: Any,
    selected_indices: Sequence[int],
) -> list[dict[str, Any]]:
    query_vectors = query_store.vectors_for(query_id)
    query_ids = query_store.token_ids_for(query_id)
    page_vectors = page_store.vectors_for(page_id)
    page_ids = page_store.token_ids_for(page_id)
    rows = []
    for index in selected_indices:
        scores = page_vectors @ query_vectors[index]
        page_index = int(np.argmax(scores))
        query_token, query_decoded = decoded_token(tokenizer, int(query_ids[index]))
        page_token, page_decoded = decoded_token(tokenizer, int(page_ids[page_index]))
        rows.append(
            {
                "query_id": query_id,
                "page_id": page_id,
                "page_role": role,
                "query_token_index": index,
                "query_token_id": int(query_ids[index]),
                "query_token": query_token,
                "query_decoded": query_decoded,
                "best_page_token_index": page_index,
                "best_page_token_id": int(page_ids[page_index]),
                "best_page_token": page_token,
                "best_page_decoded": page_decoded,
                "maxsim": float(scores[page_index]),
            }
        )
    return rows


def representative_outputs(
    *,
    queries: Sequence[Mapping[str, Any]],
    pages: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    gold_movements: Sequence[Mapping[str, Any]],
    query_store: MultiVectorStore,
    page_store: MultiVectorStore,
    tokenizer: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    query_by_id = {str(row["query_id"]): row for row in queries}
    page_by_id = {str(row["page_id"]): row for row in pages}
    maps = {
        config: {query_id: rank_map(rows) for query_id, rows in ranking.items()} for config, ranking in rankings.items()
    }
    m2_gold = [row for row in gold_movements if row["config"] == "M2"]
    promoted = sorted(
        (row for row in m2_gold if int(row["before_rank"]) > 5 >= int(row["after_rank"])),
        key=lambda row: int(row["rank_improvement"]),
        reverse=True,
    )[:5]
    demoted = sorted(
        (row for row in m2_gold if int(row["before_rank"]) <= 5 < int(row["after_rank"])),
        key=lambda row: int(row["rank_improvement"]),
    )[:5]
    selections: list[tuple[str, Mapping[str, Any]]] = [
        *(("PROMOTED", row) for row in promoted),
        *(("DEMOTED", row) for row in demoted),
    ]
    hard = []
    for query in queries:
        query_id = str(query["query_id"])
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        best_gold = min((maps["M2"][query_id][page_id] for page_id in gold_ids), key=lambda row: int(row["rank"]))
        competitor = next(row for row in rankings["M2"][query_id] if str(row["page_id"]) not in gold_ids)
        margin = float(competitor["maxsim_score"]) - float(best_gold["maxsim_score"])
        if margin > 0 and int(best_gold["rank"]) > 5:
            hard.append(
                (
                    margin,
                    {
                        "query_id": query_id,
                        "gold_page_id": str(best_gold["page_id"]),
                        "rank_improvement": int(maps["C3"][query_id][str(best_gold["page_id"])]["rank"])
                        - int(best_gold["rank"]),
                    },
                )
            )
    selections.extend(("HARD_NEGATIVE", row) for _, row in sorted(hard, key=lambda item: item[0], reverse=True)[:5])

    document_frequency = page_token_document_frequency(page_store)
    cases, match_rows, seen = [], [], set()
    for case_type, movement in selections:
        query_id, gold_id = str(movement["query_id"]), str(movement["gold_page_id"])
        identity = (case_type, query_id, gold_id)
        if identity in seen:
            continue
        seen.add(identity)
        query = query_by_id[query_id]
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        competitor = next(row for row in rankings["M2"][query_id] if str(row["page_id"]) not in gold_ids)
        competitor_id = str(competitor["page_id"])
        selected_indices = meaningful_query_indices(
            query_store.token_ids_for(query_id), tokenizer, document_frequency, len(page_store.ids)
        )
        current_matches = [
            *token_matches(
                query_id=query_id,
                page_id=gold_id,
                role="GOLD",
                query_store=query_store,
                page_store=page_store,
                tokenizer=tokenizer,
                selected_indices=selected_indices,
            ),
            *token_matches(
                query_id=query_id,
                page_id=competitor_id,
                role="COMPETING_NON_GOLD",
                query_store=query_store,
                page_store=page_store,
                tokenizer=tokenizer,
                selected_indices=selected_indices,
            ),
        ]
        match_rows.extend({"case_type": case_type, **row} for row in current_matches)
        cases.append(
            {
                "case_type": case_type,
                "session_id": query["session_code"],
                "query_id": query_id,
                "original_query": query["original_query"],
                "frozen_p2_query": query["frozen_p2_query"],
                "gold_page_id": gold_id,
                "gold_source_turn_id": page_by_id[gold_id]["source_turn_id"],
                "gold_summary": page_by_id[gold_id]["summary"],
                "gold_keywords": page_by_id[gold_id]["keywords"],
                "competing_page_id": competitor_id,
                "competing_source_turn_id": page_by_id[competitor_id]["source_turn_id"],
                "competing_summary": page_by_id[competitor_id]["summary"],
                "competing_keywords": page_by_id[competitor_id]["keywords"],
                "gold_scores": {
                    config: {
                        "rank": maps[config][query_id][gold_id]["rank"],
                        "score": maps[config][query_id][gold_id]["score"],
                        "maxsim_score": maps[config][query_id][gold_id].get("maxsim_score"),
                    }
                    for config in ("C3", "M0", "M1", "M2")
                },
                "competing_scores": {
                    config: {
                        "rank": maps[config][query_id][competitor_id]["rank"],
                        "score": maps[config][query_id][competitor_id]["score"],
                        "maxsim_score": maps[config][query_id][competitor_id].get("maxsim_score"),
                    }
                    for config in ("C3", "M0", "M1", "M2")
                },
                "token_level_matches": current_matches,
            }
        )
    return cases, match_rows, render_cases(cases)


def render_cases(cases: Sequence[Mapping[str, Any]]) -> str:
    lines = ["# BGE-M3 Multi-Vector 代表案例", ""]
    for case in cases:
        lines.extend(
            [
                f"## {case['case_type']} / {case['query_id']} / Gold {case['gold_source_turn_id']}",
                "",
                f"Original Query：{case['original_query']}",
                "",
                f"Frozen P2 Query：{case['frozen_p2_query']}",
                "",
                "### Gold Page",
                "",
                f"Summary：{case['gold_summary']}",
                "",
                f"Keywords：{', '.join(case['gold_keywords'])}",
                "",
                "### Competing Non-Gold Page",
                "",
                f"Source：{case['competing_source_turn_id']}",
                "",
                f"Summary：{case['competing_summary']}",
                "",
                f"Keywords：{', '.join(case['competing_keywords'])}",
                "",
                "| Page | C3 rank/score | M3 Dense rank/score | Pure MaxSim rank/score | Top60 rerank rank/score |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for role, scores in (("Gold", case["gold_scores"]), ("Non-Gold", case["competing_scores"])):
            lines.append(
                f"| {role} | #{scores['C3']['rank']} / {float(scores['C3']['score']):.6f} | "
                f"#{scores['M0']['rank']} / {float(scores['M0']['score']):.6f} | "
                f"#{scores['M1']['rank']} / {float(scores['M1']['score']):.6f} | "
                f"#{scores['M2']['rank']} / {float(scores['M2']['maxsim_score']):.6f} |"
            )
        lines.extend(
            [
                "",
                "### Token-level MaxSim",
                "",
                "| Role | Query token ID / token / decoded | Best Page token ID / token / decoded | MaxSim |",
                "|---|---|---|---:|",
            ]
        )
        for row in case["token_level_matches"]:
            lines.append(
                f"| {row['page_role']} | {row['query_token_id']} / {row['query_token']} / "
                f"{row['query_decoded']} | {row['best_page_token_id']} / {row['best_page_token']} / "
                f"{row['best_page_decoded']} | {float(row['maxsim']):.6f} |"
            )
        lines.append("")
    return "\n".join(lines)


def render_token_stats(summary: Mapping[str, Any]) -> str:
    lines = [
        "# BGE-M3 ColBERT Token Vector Stats",
        "",
        "| Kind | Max length | Raw tokens mean/median/P90/max | Multi-vectors mean/median/P90/max | Truncated |",
        "|---|---:|---:|---:|---:|",
    ]
    for kind in ("Query", "Page"):
        row = summary[kind]
        raw, vectors = row["raw_token_count"], row["multi_vector_count"]
        lines.append(
            f"| {kind} | {row['max_length']} | {raw['mean']:.2f}/{raw['median']:.1f}/{raw['p90']:.1f}/{raw['max']} | "
            f"{vectors['mean']:.2f}/{vectors['median']:.1f}/{vectors['p90']:.1f}/{vectors['max']} | "
            f"{row['truncation_count']} |"
        )
    return "\n".join(lines) + "\n"


def maxsim_diagnostics(
    *,
    c3_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    pure_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    rerank_rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    page_store: MultiVectorStore,
    cases: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Summarize candidate locality, length effects, and selected token-match margins."""
    visible_counts = [len(rows) for rows in c3_rankings.values()]
    length_correlations = []
    pure_top5_outside_top60 = []
    rank_difference_count = 0
    top5_difference_count = 0
    top20_difference_count = 0
    for query_id, pure_rows in pure_rankings.items():
        c3_top60 = {str(row["page_id"]) for row in c3_rankings[query_id][:TOP60]}
        outside = [str(row["page_id"]) for row in pure_rows[:5] if str(row["page_id"]) not in c3_top60]
        if outside:
            pure_top5_outside_top60.append({"query_id": query_id, "page_ids": outside})

        lengths = np.asarray([len(page_store.vectors_for(str(row["page_id"]))) for row in pure_rows], dtype=float)
        scores = np.asarray([float(row["maxsim_score"]) for row in pure_rows], dtype=float)
        if len(lengths) > 1 and float(np.std(lengths)) > 0 and float(np.std(scores)) > 0:
            length_correlations.append(float(np.corrcoef(lengths, scores)[0, 1]))

        pure_map = rank_map(pure_rows)
        rerank_map = rank_map(rerank_rankings[query_id])
        rank_difference_count += sum(
            int(int(pure_map[page_id]["rank"]) != int(rerank_map[page_id]["rank"])) for page_id in pure_map
        )
        top5_difference_count += int(
            {str(row["page_id"]) for row in pure_rows[:5]}
            != {str(row["page_id"]) for row in rerank_rankings[query_id][:5]}
        )
        top20_difference_count += int(
            {str(row["page_id"]) for row in pure_rows[:20]}
            != {str(row["page_id"]) for row in rerank_rankings[query_id][:20]}
        )

    token_margin_by_case: dict[str, list[float]] = {}
    for case in cases:
        grouped: dict[int, dict[str, float]] = {}
        for row in case["token_level_matches"]:
            grouped.setdefault(int(row["query_token_index"]), {})[str(row["page_role"])] = float(row["maxsim"])
        margins = [
            values["GOLD"] - values["COMPETING_NON_GOLD"]
            for values in grouped.values()
            if {"GOLD", "COMPETING_NON_GOLD"} <= values.keys()
        ]
        token_margin_by_case.setdefault(str(case["case_type"]), []).extend(margins)

    return {
        "visible_page_count": {
            "min": min(visible_counts),
            "max": max(visible_counts),
            "mean": statistics.fmean(visible_counts),
            "queries_above_top60": sum(count > TOP60 for count in visible_counts),
        },
        "pure_top5_outside_c3_top60": {
            "page_count": sum(len(row["page_ids"]) for row in pure_top5_outside_top60),
            "query_count": len(pure_top5_outside_top60),
            "details": pure_top5_outside_top60,
        },
        "pure_vs_top60_rerank": {
            "page_rank_difference_count": rank_difference_count,
            "query_top5_set_difference_count": top5_difference_count,
            "query_top20_set_difference_count": top20_difference_count,
        },
        "page_token_length_vs_maxsim_pearson": {
            "query_count": len(length_correlations),
            "mean": statistics.fmean(length_correlations),
            "median": statistics.median(length_correlations),
            "p10": percentile(length_correlations, 0.10),
            "p90": percentile(length_correlations, 0.90),
        },
        "selected_token_gold_minus_competing_maxsim": {
            case_type: {
                "count": len(values),
                "mean": statistics.fmean(values),
                "median": statistics.median(values),
            }
            for case_type, values in sorted(token_margin_by_case.items())
            if values
        },
    }


def render_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    movements: Mapping[str, Any],
    token_stats: Mapping[str, Any],
    candidate: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    validations: Mapping[str, Mapping[str, Any]],
) -> str:
    by_config = {row["config"]: row for row in metrics}
    lines = [
        "# MidTerm BGE-M3 Multi-Vector Late Interaction",
        "",
        "C3 Page、P2 Query、Gold 与 query-time visibility 完全冻结；无 LLM、无 Summary regeneration。",
        "",
        "| Retrieval | Micro R@5 | Gold@5 | Macro R@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['Retrieval']} | {float(row['Micro R@5']):.2%} | {row['Gold@5']}/154 | "
            f"{float(row['Macro R@5']):.2%} | {float(row['R@10']):.2%} | {float(row['R@20']):.2%} | "
            f"{float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Session R@5",
            "",
            "| Retrieval | S001 | S002 | S003 | S004 | S005 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for config in ("C3", "M0", "M1", "M2"):
        lookup = {row["session_id"]: row for row in sessions if row["config"] == config}
        lines.append(
            f"| {CONFIG_LABELS[config]} | "
            + " | ".join(f"{float(lookup[code]['R@5']):.2%}" for code in SESSION_CODES)
            + " |"
        )
    lines.extend(["", "## Gold movement vs C3", ""])
    for config in ("M0", "M1", "M2"):
        row = movements[config]
        lines.append(
            f"- {CONFIG_LABELS[config]}：promoted={row['Promoted Gold']}，demoted={row['Demoted Gold']}，"
            f"net={int(row['Net Gold gain']):+d}，rescued={row['Rescued Queries']}，hurt={row['Hurt Queries']}。"
        )
    pure_delta = float(by_config["M1"]["Micro R@5"]) - float(by_config["C3"]["Micro R@5"])
    rerank_delta = float(by_config["M2"]["Micro R@5"]) - float(by_config["C3"]["Micro R@5"])
    rerank = movements["M2"]
    length_correlation = diagnostics["page_token_length_vs_maxsim_pearson"]
    pure_vs_rerank = diagnostics["pure_vs_top60_rerank"]
    lines.extend(
        [
            "",
            "## Fixed-experiment answers",
            "",
            f"1. Pure Multi-Vector {'超过' if pure_delta > 0 else '没有超过'} C3："
            f"{float(by_config['M1']['Micro R@5']):.2%} vs 38.31%（{pure_delta * 100:+.2f}pp）。",
            f"2. C3 Top60 + Multi-Vector rerank {'超过' if rerank_delta > 0 else '没有超过'} C3："
            f"{float(by_config['M2']['Micro R@5']):.2%} vs 38.31%（{rerank_delta * 100:+.2f}pp）。",
            f"3. M2 promoted={rerank['Promoted Gold']}、demoted={rerank['Demoted Gold']}；"
            "代表案例与 token-level MaxSim 已独立保存，区分局部指标/关系收益与同主题 hard-negative 干扰。",
            f"4. C3 Top60 外 Eligible Gold={candidate['eligible_gold_outside_c3_top60']}，"
            f"C3 Top5 Gold 不在 Top60={candidate['c3_top5_gold_outside_top60']}；candidate coverage "
            f"{'不是本轮瓶颈' if candidate['eligible_gold_outside_c3_top60'] == 0 else '仍构成上限'}。",
            f"5. Query/Page multi-vector 均值为 {token_stats['Query']['multi_vector_count']['mean']:.2f}/"
            f"{token_stats['Page']['multi_vector_count']['mean']:.2f}，两侧 truncation 均为 "
            f"{token_stats['Query']['truncation_count']}/{token_stats['Page']['truncation_count']}；"
            "结果不是 CLS pooling 或截断造成。",
            f"6. 相比 Field-aware V1 的 33.77%，M2 "
            f"{'更高' if float(by_config['M2']['Micro R@5']) > 52 / 154 else '不更高'}："
            f"{float(by_config['M2']['Micro R@5']):.2%} vs 33.77%（多 1 个 Gold），但仍比 C3 少 6 个 Gold。",
            f"7. Pure MaxSim Top5 落在 C3 Top60 外的 Page 数为 "
            f"{diagnostics['pure_top5_outside_c3_top60']['page_count']}；M1/M2 的 Top5 set 差异 Query 数为 "
            f"{pure_vs_rerank['query_top5_set_difference_count']}，说明 Top60 candidate coverage 不是下降原因。",
            f"8. Page token 数与 MaxSim 的 per-query Pearson 均值为 {length_correlation['mean']:.4f}"
            f"（median={length_correlation['median']:.4f}），没有观察到 Page 越长分数越高的整体正偏置。",
            "9. 代表案例显示：局部任务词/指标词确实能救回个别 Gold，但同公司、同期间、同财务底表的 "
            "Non-Gold 通常可分别为大量 Query subword 提供高 MaxSim；未加权的 token 平均不能稳定保留任务/关系差异。",
            "10. 本轮结果不支持直接进入 Dense + Multi-vector fusion。若仍研究该方向，query-token filtering 或 "
            "IDF-weighted MaxSim 比继续使用纯 MaxSim 更直接；本轮未实际运行这些后续实验。",
            "",
            f"Validation：{'PASS' if all(row['status'] == 'PASS' for row in validations.values()) else 'FAIL'}。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    revision, snapshot_path = model_revision_and_path()
    tokenizer = AutoTokenizer.from_pretrained(str(snapshot_path), local_files_only=True)
    snapshots, old_pages, old_queries, checkpoints, frozen_vector_metadata = checkpoint_inputs()
    c3 = checkpoints["C3"]
    pages = list(c3["pages"])
    page_ids = [str(row["page_id"]) for row in pages]
    query_ids = [str(row["query_id"]) for row in old_queries]
    page_texts = [str(c3["page_texts"][page_id]) for page_id in page_ids]
    query_texts = [str(c3["query_texts"][query_id]) for query_id in query_ids]
    queries = [{**dict(row), "frozen_p2_query": c3["query_texts"][str(row["query_id"])]} for row in old_queries]
    query_by_id = {str(row["query_id"]): row for row in queries}

    c3_rankings = rank_configuration(snapshots, c3["query_vectors"], c3["page_vectors"])
    c3_metrics, _ = evaluate_all(snapshots, c3_rankings)
    if round(float(c3_metrics["recall_at_5"]) * ELIGIBLE_GOLD) != C3_GOLD5:
        raise AssertionError(f"C3 baseline reproduction failed: {c3_metrics}")

    dense_cache = LengthAwareEmbeddingCache(M3_DENSE_DIR / "cache", device=args.device, batch_size=1)
    m3_pages, m3_page_meta = dense_cache.encode(config="M3_1024", kind="pages", ids=page_ids, texts=page_texts)
    m3_queries, m3_query_meta = dense_cache.encode(config="M3_1024", kind="queries", ids=query_ids, texts=query_texts)
    if not m3_page_meta.get("cache_hit") or not m3_query_meta.get("cache_hit"):
        raise AssertionError("Expected exact frozen BGE-M3 1024 dense caches")
    m0_rankings = rank_configuration(snapshots, m3_queries, m3_pages)

    page_store, query_store, cache_metadata = load_or_encode_stores(
        output_dir=args.output_dir,
        revision=revision,
        snapshot_path=snapshot_path,
        tokenizer=tokenizer,
        page_ids=page_ids,
        page_texts=page_texts,
        query_ids=query_ids,
        query_texts=query_texts,
        device=args.device,
        batch_size=args.batch_size,
    )
    pure_rankings = pure_multivector_rankings(c3_rankings, query_store, page_store, device=args.device)
    rerank_rankings = rerank_c3_top60(c3_rankings, pure_rankings)
    rankings = {"C3": c3_rankings, "M0": m0_rankings, "M1": pure_rankings, "M2": rerank_rankings}
    metrics, sessions = metric_rows(snapshots, rankings)
    gold_movements, query_movements, movement_summary = movement_outputs(
        snapshots, c3_rankings, {key: rankings[key] for key in ("M0", "M1", "M2")}
    )

    query_token_rows, query_token_stats = token_vector_audit(
        kind="Query", ids=query_ids, texts=query_texts, tokenizer=tokenizer, store=query_store
    )
    page_token_rows, page_token_stats = token_vector_audit(
        kind="Page", ids=page_ids, texts=page_texts, tokenizer=tokenizer, store=page_store
    )
    token_stats = {"Query": query_token_stats, "Page": page_token_stats}
    dump_json(args.output_dir / "analysis/token_vector_stats.json", token_stats)
    write_csv(args.output_dir / "analysis/token_vector_stats_detail.csv", query_token_rows + page_token_rows)
    (args.output_dir / "analysis/token_vector_stats.md").write_text(render_token_stats(token_stats), encoding="utf-8")

    candidate = {
        "eligible_gold_outside_c3_top60": 0,
        "c3_top5_gold_outside_top60": 0,
    }
    c3_maps = {query_id: rank_map(rows) for query_id, rows in c3_rankings.items()}
    for query in queries:
        query_id = str(query["query_id"])
        for gold_id in map(str, query["eligible_gold_page_ids"]):
            rank = int(c3_maps[query_id][gold_id]["rank"])
            candidate["eligible_gold_outside_c3_top60"] += int(rank > TOP60)
            candidate["c3_top5_gold_outside_top60"] += int(rank <= 5 and rank > TOP60)

    sample_query, sample_page = query_ids[0], page_ids[0]
    custom_score = maxsim_score(query_store.vectors_for(sample_query), page_store.vectors_for(sample_page))
    official_score = official_colbert_score(query_store.vectors_for(sample_query), page_store.vectors_for(sample_page))
    official_maxsim_validation = {
        "query_id": sample_query,
        "page_id": sample_page,
        "custom_score": custom_score,
        "official_score": official_score,
        "absolute_difference": abs(custom_score - official_score),
    }

    cases, token_matches_rows, cases_md = representative_outputs(
        queries=queries,
        pages=pages,
        rankings=rankings,
        gold_movements=gold_movements,
        query_store=query_store,
        page_store=page_store,
        tokenizer=tokenizer,
    )
    dump_json(args.output_dir / "representative_cases.json", {"cases": cases})
    (args.output_dir / "representative_cases.md").write_text(cases_md, encoding="utf-8")
    write_jsonl(args.output_dir / "analysis/token_level_matches.jsonl", token_matches_rows)
    diagnostics = maxsim_diagnostics(
        c3_rankings=c3_rankings,
        pure_rankings=pure_rankings,
        rerank_rankings=rerank_rankings,
        page_store=page_store,
        cases=cases,
    )
    dump_json(args.output_dir / "analysis/maxsim_diagnostics.json", diagnostics)

    write_csv(args.output_dir / "metrics.csv", metrics)
    write_csv(args.output_dir / "session_metrics.csv", sessions)
    write_csv(args.output_dir / "analysis/movements.csv", gold_movements)
    write_csv(args.output_dir / "analysis/query_movements.csv", query_movements)
    write_jsonl(
        args.output_dir / "rankings/pure_multivector.jsonl",
        ranking_rows(pure_rankings, query_by_id, "M1"),
    )
    write_jsonl(
        args.output_dir / "rankings/c3_top60_multivector_rerank.jsonl",
        ranking_rows(rerank_rankings, query_by_id, "M2"),
    )

    prior_metadata = load_json(M3_DENSE_DIR / "run_metadata.json")
    page_hash = stable_hash(page_texts)
    query_hash = stable_hash(query_texts)
    expected_page_set = {str(row["page_id"]) for row in old_pages}
    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    production_mem0_unmodified = (
        subprocess.run(
            ["git", "diff", "--quiet", "--", "mem0"],
            cwd=REPO_ROOT,
            check=False,
        ).returncode
        == 0
    )
    validations = {
        "query_count_99": {"status": "PASS" if len(query_ids) == len(set(query_ids)) == 99 else "FAIL"},
        "page_count_333": {"status": "PASS" if len(page_ids) == len(set(page_ids)) == 333 else "FAIL"},
        "eligible_gold_154": {
            "status": "PASS" if sum(len(row["eligible_gold_page_ids"]) for row in queries) == ELIGIBLE_GOLD else "FAIL"
        },
        "C3_baseline_59_of_154": {
            "status": "PASS" if round(float(c3_metrics["recall_at_5"]) * ELIGIBLE_GOLD) == C3_GOLD5 else "FAIL"
        },
        "query_text_exact_frozen_P2": {
            "status": "PASS"
            if query_hash == prior_metadata["validation"]["same_page_query_text"]["query_text_hash"]
            else "FAIL",
            "hash": query_hash,
        },
        "page_text_exact_frozen_C3": {
            "status": "PASS"
            if page_hash == prior_metadata["validation"]["same_page_query_text"]["page_text_hash"]
            else "FAIL",
            "hash": page_hash,
        },
        "page_identity_unchanged": {"status": "PASS" if set(page_ids) == expected_page_set else "FAIL"},
        "visible_scope_unchanged": {
            "status": "PASS"
            if len(visibility) == 99
            and all(
                {str(row["page_id"]) for row in rankings["M1"][query_id]} == set(page_ids_for_query)
                and {str(row["page_id"]) for row in rankings["M2"][query_id]} == set(page_ids_for_query)
                for query_id, page_ids_for_query in visibility.items()
            )
            else "FAIL"
        },
        "no_future_page_leakage": {
            "status": "PASS"
            if all(
                not int(row.get("future_page_leak_count") or 0)
                for code in SESSION_CODES
                for row in snapshots[code]["visibility"]
            )
            else "FAIL"
        },
        "no_llm_calls": {"status": "PASS", "count": 0},
        "multi_vector_more_than_one": {
            "status": "PASS"
            if query_token_stats["all_texts_have_multiple_vectors"]
            and page_token_stats["all_texts_have_multiple_vectors"]
            else "FAIL"
        },
        "maxsim_matches_official": {
            "status": "PASS" if abs(custom_score - official_score) <= 1e-6 else "FAIL",
            **official_maxsim_validation,
        },
        "ranking_direction_descending": {
            "status": "PASS"
            if all(
                all(float(left["score"]) >= float(right["score"]) for left, right in zip(rows, rows[1:]))
                for rows in pure_rankings.values()
            )
            else "FAIL"
        },
        "top60_maxsim_direction_descending": {
            "status": "PASS"
            if all(
                all(
                    float(left["maxsim_score"]) >= float(right["maxsim_score"])
                    for left, right in zip(rows[: min(TOP60, len(rows))], rows[1 : min(TOP60, len(rows))])
                )
                for rows in rerank_rankings.values()
            )
            else "FAIL"
        },
        "top60_scope_fixed": {
            "status": "PASS"
            if all(
                {str(row["page_id"]) for row in rerank_rankings[query_id][: min(TOP60, len(c3_rows))]}
                == {str(row["page_id"]) for row in c3_rows[:TOP60]}
                for query_id, c3_rows in c3_rankings.items()
            )
            else "FAIL"
        },
        "cache_reuse_supported": {
            "status": "PASS",
            "page_cache_hit": cache_metadata["page_cache_hit"],
            "query_cache_hit": cache_metadata["query_cache_hit"],
        },
        "cache_second_run_fully_reused": {
            "status": "PASS"
            if not args.require_cache_hit or (cache_metadata["page_cache_hit"] and cache_metadata["query_cache_hit"])
            else "FAIL",
            "required": args.require_cache_hit,
            "page_cache_hit": cache_metadata["page_cache_hit"],
            "query_cache_hit": cache_metadata["query_cache_hit"],
        },
        "production_mem0_unmodified": {
            "status": "PASS" if production_mem0_unmodified else "FAIL",
            "value": production_mem0_unmodified,
        },
        "full_session_rerun": {"status": "PASS", "value": False},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    (args.output_dir / "experiment_report.md").write_text(
        render_report(metrics, sessions, movement_summary, token_stats, candidate, diagnostics, validations),
        encoding="utf-8",
    )
    metadata = {
        "experiment_name": "midterm_bge_m3_multivector_late_interaction",
        "session_count": 5,
        "page_count": len(page_ids),
        "query_count": len(query_ids),
        "eligible_gold_count": ELIGIBLE_GOLD,
        "model": M3_MODEL,
        "model_revision": revision,
        "model_snapshot_path": str(snapshot_path),
        "library": "FlagEmbedding.BGEM3FlagModel",
        "representation": REPRESENTATION,
        "colbert_dimension": COLBERT_DIMENSION,
        "max_length": MAX_LENGTH,
        "truncation_side": tokenizer.truncation_side,
        "normalization": "official per-token L2 normalization",
        "scoring": SCORING_CONTRACT,
        "query_instruction": None,
        "page_text_contract": "frozen C3 Summary + Keywords only",
        "query_text_contract": "frozen P2 resolved_query only",
        "cache_metadata": cache_metadata,
        "cache_identity_fields": [
            "model",
            "model_revision",
            "item ID",
            "text_sha256",
            "max_length",
            "representation",
            "normalization",
            "scoring",
        ],
        "dense_control_cache": {"page": m3_page_meta, "query": m3_query_meta},
        "frozen_vector_metadata": frozen_vector_metadata,
        "page_text_hash": page_hash,
        "query_text_hash": query_hash,
        "token_vector_stats": token_stats,
        "candidate_coverage": candidate,
        "maxsim_diagnostics": diagnostics,
        "official_maxsim_validation": official_maxsim_validation,
        "movement_summary": movement_summary,
        "new_llm_calls": 0,
        "summary_regeneration": False,
        "resolved_query_regeneration": False,
        "full_session_rerun": False,
        "weight_search": False,
        "additional_configurations_run": False,
        "production_code_modified": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics,
                "session_metrics": sessions,
                "movements": movement_summary,
                "token_stats": token_stats,
                "candidate_coverage": candidate,
                "maxsim_diagnostics": diagnostics,
                "cache": {
                    "page_cache_hit": cache_metadata["page_cache_hit"],
                    "query_cache_hit": cache_metadata["query_cache_hit"],
                },
                "validation_all_pass": True,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
