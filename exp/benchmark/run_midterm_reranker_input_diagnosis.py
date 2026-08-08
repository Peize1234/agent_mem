"""Offline S001-S005 diagnosis of reranker truncation, packing, and candidate pools.

S001-S005 are development/diagnosis data in this stage.  The script consumes
only immutable snapshots and caches produced by earlier stages; it never runs
the conversational benchmark or calls an online LLM.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import os
import resource
import statistics
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import (  # noqa: E402
    ChineseBM25Index,
    append_reranked_candidates,
    cosine_rank,
    evaluate_rankings,
    latency_stats,
    load_jsonl,
    page_representation,
    rrf_fuse,
    stable_hash,
    write_jsonl,
)
from exp.benchmark.run_midterm_multi_session_validation import (  # noqa: E402
    load_s001_reranker_scores,
    movement_category,
    session_candidate_rankings,
    visible_pages_by_query,
)
from exp.benchmark.run_midterm_rerank_tuning import (  # noqa: E402
    BASE_RERANKER,
    PRODUCTION_EMBEDDING,
    expanded_union,
    fixed_budget_union,
    rank_of,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from mem0.reranker.huggingface_reranker import HuggingFaceReranker  # noqa: E402


OUTPUT_DEFAULT = REPO_ROOT / "exp/results/midterm_reranker_input_diagnosis_no_thinking"
MULTI_RESULT_DEFAULT = REPO_ROOT / "exp/results/midterm_multi_session_validation_no_thinking"
S001_RESULT_DEFAULT = REPO_ROOT / "exp/results/midterm_retrieval_experiments_no_thinking"
MAX_LENGTH = 512
OUTPUT_K = 5
RRF_CONSTANT = 60
SESSION_CODES = ("S001", "S002", "S003", "S004", "S005")
INPUT_STATUS = "S001-S005_TUNED"
NEW_RERANKER = "BAAI/bge-reranker-v2-m3"

UNIQUE_INPUT_CONFIGS: tuple[dict[str, Any], ...] = (
    {"id": "R0_P8_CURRENT", "mode": "auto", "order": ("summary", "keywords")},
    {"id": "R1_KEYWORDS_FIRST", "mode": "auto", "order": ("keywords", "summary")},
    {"id": "R2_PACK_A", "mode": "budgeted", "order": ("user", "keywords", "summary")},
    {"id": "R3_USER_SUMMARY", "mode": "budgeted", "order": ("user", "summary")},
    {"id": "R4_PACK_B", "mode": "budgeted", "order": ("keywords", "user", "summary")},
    {
        "id": "PACK_C_BALANCED",
        "mode": "balanced",
        "order": ("user", "keywords", "summary"),
        "caps": {"user": 128, "keywords": 96},
    },
)

INPUT_ALIASES = {
    "R2_USER_KEYWORDS_SUMMARY_BUDGETED": "R2_PACK_A",
    "PACK_A_USER_KEYWORDS_GUARANTEED": "R2_PACK_A",
    "R4_KEYWORDS_USER_SUMMARY_BUDGETED": "R4_PACK_B",
    "PACK_B_KEYWORDS_USER_GUARANTEED": "R4_PACK_B",
}

CANDIDATE_CONFIGS: tuple[dict[str, Any], ...] = (
    {"id": "C0_DENSE20", "strategy": "dense", "candidate_k": 20},
    {"id": "C1_BM25_20", "strategy": "bm25", "candidate_k": 20},
    {"id": "C2_RRF60_20", "strategy": "rrf", "candidate_k": 20},
    {"id": "C3_FIXED_UNION20", "strategy": "fixed_union", "candidate_k": 20},
    {"id": "C4_UNION15X15", "strategy": "expanded_union", "candidate_k": 15},
    {"id": "C5_UNION20X20", "strategy": "expanded_union", "candidate_k": 20},
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose S001-S005 reranker input truncation and candidate pools")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DEFAULT)
    parser.add_argument("--multi-result-dir", type=Path, default=MULTI_RESULT_DEFAULT)
    parser.add_argument("--s001-result-dir", type=Path, default=S001_RESULT_DEFAULT)
    parser.add_argument("--latency-warmups", type=int, default=3)
    parser.add_argument("--latency-repeats", type=int, default=1)
    parser.add_argument("--torch-threads", type=int, default=min(os.cpu_count() or 1, 8))
    return parser.parse_args()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in fieldnames})


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as file:
        return [dict(row) for row in csv.DictReader(file)]


def file_state(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def session_code(session_id: str) -> str:
    return session_id.split("_", 1)[0]


def percentile(values: Sequence[float], q: float) -> float | None:
    return float(np.percentile(np.asarray(values, dtype=float), q)) if values else None


def keyword_text(page: Mapping[str, Any]) -> str:
    value = page.get("keywords") or []
    return ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)


def field_text(page: Mapping[str, Any], field: str) -> str:
    if field == "user":
        return f"[User Question]\n{str(page.get('user_input') or '').strip()}"
    if field == "keywords":
        return f"[Keywords]\n{keyword_text(page)}"
    if field == "summary":
        return f"[Summary]\n{str(page.get('summary') or '').strip()}"
    raise ValueError(f"Unknown packing field: {field}")


def auto_document_text(page: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    if config["id"] == "R0_P8_CURRENT":
        return page_representation(page, "P8")
    values = []
    for field in config["order"]:
        if field == "summary":
            values.append(str(page.get("summary") or "").strip())
        elif field == "keywords":
            value = keyword_text(page)
            values.append(f"Keywords: {value}" if value else "")
        else:
            raise ValueError(f"Auto packing cannot use field {field}")
    return "\n".join(value for value in values if value)


def pack_document_ids(
    tokenizer: Any,
    query_text: str,
    page: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    max_length: int,
) -> tuple[list[int], dict[str, Any]]:
    started = time.perf_counter()
    query_ids = tokenizer.encode(query_text, add_special_tokens=False)
    special_tokens = tokenizer.num_special_tokens_to_add(pair=True)
    document_budget = max(0, max_length - special_tokens - len(query_ids))
    remaining = document_budget
    document_ids: list[int] = []
    allocations: dict[str, dict[str, int]] = {}
    caps = config.get("caps") or {}
    for field in config["order"]:
        ids = tokenizer.encode(field_text(page, field), add_special_tokens=False)
        cap = min(int(caps.get(field, len(ids))), len(ids))
        take = min(cap, remaining)
        document_ids.extend(ids[:take])
        allocations[field] = {"full_tokens": len(ids), "allocated_tokens": take}
        remaining -= take
    document_text = tokenizer.decode(document_ids, skip_special_tokens=True)
    pair_length = len(
        tokenizer(
            query_text,
            document_text,
            add_special_tokens=True,
            truncation=False,
        )["input_ids"]
    )
    while pair_length > max_length and document_ids:
        overflow = min(pair_length - max_length, len(document_ids))
        del document_ids[-overflow:]
        remaining += overflow
        for field in reversed(config["order"]):
            allocated = allocations[field]["allocated_tokens"]
            removed = min(allocated, overflow)
            allocations[field]["allocated_tokens"] -= removed
            overflow -= removed
            if not overflow:
                break
        document_text = tokenizer.decode(document_ids, skip_special_tokens=True)
        pair_length = len(
            tokenizer(
                query_text,
                document_text,
                add_special_tokens=True,
                truncation=False,
            )["input_ids"]
        )
    if pair_length > max_length:
        raise AssertionError(f"Packed pair exceeds budget: {pair_length}>{max_length}")
    return document_ids, {
        "query_tokens": len(query_ids),
        "special_tokens": special_tokens,
        "document_budget": document_budget,
        "document_tokens": len(document_ids),
        "unused_tokens": remaining,
        "field_allocations": allocations,
        "packing_ms": (time.perf_counter() - started) * 1000.0,
    }


def build_input(
    tokenizer: Any,
    query_text: str,
    page: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    max_length: int,
) -> dict[str, Any]:
    started = time.perf_counter()
    if config["mode"] == "auto":
        text = auto_document_text(page, config)
        return {
            "mode": "auto",
            "text": text,
            "document_ids": None,
            "packing_ms": (time.perf_counter() - started) * 1000.0,
            "packing_metadata": None,
        }
    document_ids, metadata = pack_document_ids(tokenizer, query_text, page, config, max_length=max_length)
    return {
        "mode": "token_ids",
        "text": tokenizer.decode(document_ids, skip_special_tokens=True),
        "document_ids": document_ids,
        "packing_ms": metadata["packing_ms"],
        "packing_metadata": metadata,
    }


def load_snapshots(
    s001_result_dir: Path,
    multi_result_dir: Path,
) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    snapshots: dict[str, dict[str, Any]] = {}
    source_files: list[Path] = []
    s001_dir = s001_result_dir / "snapshot"
    s001_manifest = load_json(s001_dir / "snapshot_manifest.json")
    snapshots[str(s001_manifest["session_id"])] = {
        "manifest": s001_manifest,
        "queries": load_jsonl(s001_dir / "queries.jsonl"),
        "pages": load_jsonl(s001_dir / "pages.jsonl"),
        "visibility": load_jsonl(s001_dir / "query_page_visibility.jsonl"),
    }
    source_files.extend(s001_dir / name for name in ("snapshot_manifest.json", "queries.jsonl", "pages.jsonl", "query_page_visibility.jsonl"))
    for code in SESSION_CODES[1:]:
        snapshot_dir = multi_result_dir / "snapshots" / code
        manifest = load_json(snapshot_dir / "snapshot_manifest.json")
        snapshots[str(manifest["session_id"])] = {
            "manifest": manifest,
            "queries": load_jsonl(snapshot_dir / "queries.jsonl"),
            "pages": load_jsonl(snapshot_dir / "pages.jsonl"),
            "visibility": load_jsonl(snapshot_dir / "query_page_visibility.jsonl"),
        }
        source_files.extend(
            snapshot_dir / name
            for name in ("snapshot_manifest.json", "queries.jsonl", "pages.jsonl", "query_page_visibility.jsonl")
        )
    if sorted(session_code(session_id) for session_id in snapshots) != list(SESSION_CODES):
        raise ValueError(f"Expected exact S001-S005 snapshots, got {sorted(snapshots)}")
    for session_id, snapshot in snapshots.items():
        if int(snapshot["manifest"].get("future_page_leak_count") or 0):
            raise ValueError(f"Future Page leak in immutable snapshot {session_id}")
    return snapshots, source_files


def load_query_vectors(
    s001_result_dir: Path,
    multi_result_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[float]]:
    from exp.benchmark.run_midterm_rerank_tuning import load_existing_embedding_vectors

    vectors: dict[str, list[float]] = {}
    for session_id, snapshot in snapshots.items():
        required = [f"Q0:{query['query_id']}:0" for query in snapshot["queries"]]
        root = s001_result_dir if session_code(session_id) == "S001" else multi_result_dir
        prefix = "queries-" if session_code(session_id) == "S001" else "heldout-"
        vectors.update(
            load_existing_embedding_vectors(
                root,
                model_name=PRODUCTION_EMBEDDING,
                prefix=prefix,
                required_ids=required,
            )
        )
    return vectors


def build_candidate_components(
    snapshots: Mapping[str, Mapping[str, Any]],
    query_vectors: Mapping[str, Sequence[float]],
) -> dict[str, dict[str, dict[str, list[dict[str, Any]]]]]:
    result: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for session_id, snapshot in snapshots.items():
        dense, bm25, rrf = session_candidate_rankings(snapshot, query_vectors)
        result[session_id] = {
            str(query["query_id"]): {
                "dense": dense[str(query["query_id"])],
                "bm25": bm25[str(query["query_id"])],
                "rrf": rrf[str(query["query_id"])],
            }
            for query in snapshot["queries"]
        }
    return result


def candidate_ranking(
    components: Mapping[str, Sequence[Mapping[str, Any]]],
    config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    strategy = str(config["strategy"])
    candidate_k = int(config["candidate_k"])
    if strategy in {"dense", "bm25", "rrf"}:
        ranking = [dict(item) for item in components[strategy]]
        return ranking, min(candidate_k, len(ranking))
    if strategy == "fixed_union":
        return fixed_budget_union(components["dense"], components["bm25"], candidate_k)
    if strategy == "expanded_union":
        return expanded_union(components["dense"], components["bm25"], candidate_k)
    raise ValueError(f"Unknown candidate strategy: {strategy}")


def all_candidate_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    components: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> tuple[
    dict[str, dict[str, dict[str, list[dict[str, Any]]]]],
    dict[str, dict[str, dict[str, int]]],
]:
    rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    counts: dict[str, dict[str, dict[str, int]]] = {}
    for config in CANDIDATE_CONFIGS:
        config_id = str(config["id"])
        rankings[config_id] = {}
        counts[config_id] = {}
        for session_id, snapshot in snapshots.items():
            rankings[config_id][session_id] = {}
            counts[config_id][session_id] = {}
            for query in snapshot["queries"]:
                query_id = str(query["query_id"])
                ranking, count = candidate_ranking(components[session_id][query_id], config)
                rankings[config_id][session_id][query_id] = ranking
                counts[config_id][session_id][query_id] = count
    return rankings, counts


class PairScoreCache:
    """Pair-granular cache so expanded pools reuse already scored candidates."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = load_jsonl(path)
        self.by_key = {str(row["cache_key"]): row for row in self.rows if row.get("status") == "SUCCESS"}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(
        *,
        query_id: str,
        page_id: str,
        query_text: str,
        built_input: Mapping[str, Any],
        input_config: str,
        model_name: str,
        max_length: int,
    ) -> str:
        document_value = (
            built_input["document_ids"] if built_input["document_ids"] is not None else built_input["text"]
        )
        return stable_hash(
            {
                "query_id": query_id,
                "page_id": page_id,
                "query_text": query_text,
                "document": document_value,
                "input_config": input_config,
                "model": model_name,
                "max_length": max_length,
                "score_semantics": "raw-logit-plus-sigmoid-v1",
            }
        )

    def append(self, rows: Sequence[Mapping[str, Any]]) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            for raw in rows:
                row = dict(raw)
                file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                self.by_key[str(row["cache_key"])] = row

    def seed_current_scores(
        self,
        *,
        snapshots: Mapping[str, Mapping[str, Any]],
        s001_result_dir: Path,
        multi_result_dir: Path,
        tokenizer: Any,
    ) -> dict[str, Any]:
        s001_scores = load_s001_reranker_scores(s001_result_dir)
        heldout_rows = [
            row
            for row in load_jsonl(multi_result_dir / "cache/rerank/local_scores.jsonl")
            if row.get("status") == "SUCCESS"
            and row.get("reranker_model") == BASE_RERANKER
            and row.get("rerank_representation") == "P8"
        ]
        scores_by_query = {
            **s001_scores,
            **{
                str(row["query_id"]): {
                    str(page_id): float(score) for page_id, score in row["scores_by_page"].items()
                }
                for row in heldout_rows
            },
        }
        query_by_id = {
            str(query["query_id"]): query for snapshot in snapshots.values() for query in snapshot["queries"]
        }
        page_by_id = {
            str(page["page_id"]): page for snapshot in snapshots.values() for page in snapshot["pages"]
        }
        new_rows = []
        existing = 0
        config = UNIQUE_INPUT_CONFIGS[0]
        for query_id, scores in scores_by_query.items():
            query_text = str(query_by_id[query_id]["original_query"])
            for page_id, score in scores.items():
                built = build_input(tokenizer, query_text, page_by_id[page_id], config, max_length=MAX_LENGTH)
                cache_key = self.key(
                    query_id=query_id,
                    page_id=page_id,
                    query_text=query_text,
                    built_input=built,
                    input_config=str(config["id"]),
                    model_name=BASE_RERANKER,
                    max_length=MAX_LENGTH,
                )
                if cache_key in self.by_key:
                    existing += 1
                    continue
                clipped = min(max(float(score), 1e-9), 1.0 - 1e-9)
                new_rows.append(
                    {
                        "cache_key": cache_key,
                        "query_id": query_id,
                        "page_id": page_id,
                        "input_config": config["id"],
                        "model": BASE_RERANKER,
                        "max_length": MAX_LENGTH,
                        "raw_score": math.log(clipped / (1.0 - clipped)),
                        "score": float(score),
                        "inference_share_ms": None,
                        "origin": "imported_immutable_previous_stage_cache",
                        "status": "SUCCESS",
                    }
                )
        if new_rows:
            self.append(new_rows)
        return {"seeded": len(new_rows), "already_present": existing, "source_query_count": len(scores_by_query)}

    def score(
        self,
        *,
        query_id: str,
        query_text: str,
        page_ids: Sequence[str],
        built_inputs: Mapping[str, Mapping[str, Any]],
        input_config: str,
        model: HuggingFaceReranker,
        max_length: int,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        keys = {
            page_id: self.key(
                query_id=query_id,
                page_id=page_id,
                query_text=query_text,
                built_input=built_inputs[page_id],
                input_config=input_config,
                model_name=str(model.config.model),
                max_length=max_length,
            )
            for page_id in page_ids
        }
        missing = [page_id for page_id in page_ids if keys[page_id] not in self.by_key]
        self.hits += len(page_ids) - len(missing)
        self.misses += len(missing)
        inference_ms = 0.0
        if missing:
            import torch

            modes = {str(built_inputs[page_id]["mode"]) for page_id in missing}
            if len(modes) != 1:
                raise ValueError(f"Mixed input modes in one score batch: {modes}")
            if modes == {"auto"}:
                encoded = model.tokenizer(
                    [[query_text, str(built_inputs[page_id]["text"])] for page_id in missing],
                    padding=True,
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                ).to(model.device)
            else:
                encoded = model.tokenizer(
                    [[query_text, str(built_inputs[page_id]["text"])] for page_id in missing],
                    padding=True,
                    truncation=False,
                    return_tensors="pt",
                )
                if int(encoded["input_ids"].shape[1]) > max_length:
                    raise AssertionError(
                        f"Budgeted pair exceeds model limit: {int(encoded['input_ids'].shape[1])}>{max_length}"
                    )
                encoded = encoded.to(model.device)
            started = time.perf_counter()
            with torch.no_grad():
                raw = model.model(**encoded).logits.squeeze(-1).detach().cpu().numpy()
            inference_ms = (time.perf_counter() - started) * 1000.0
            raw_values = np.atleast_1d(raw).astype(float).tolist()
            normalized = HuggingFaceReranker._normalize_scores(raw_values)
            share = inference_ms / len(missing)
            rows = []
            for page_id, raw_score, score in zip(missing, raw_values, normalized):
                rows.append(
                    {
                        "cache_key": keys[page_id],
                        "query_id": query_id,
                        "page_id": page_id,
                        "input_config": input_config,
                        "model": str(model.config.model),
                        "max_length": max_length,
                        "raw_score": raw_score,
                        "score": score,
                        "inference_share_ms": share,
                        "origin": "local_inference",
                        "status": "SUCCESS",
                    }
                )
            self.append(rows)
        scores = {page_id: float(self.by_key[keys[page_id]]["score"]) for page_id in page_ids}
        cached_estimated_ms = sum(
            float(self.by_key[keys[page_id]].get("inference_share_ms") or 0.0) for page_id in page_ids
        )
        return scores, {
            "candidate_count": len(page_ids),
            "new_pair_count": len(missing),
            "cached_pair_count": len(page_ids) - len(missing),
            "new_inference_ms": inference_ms,
            "cached_estimated_inference_ms": cached_estimated_ms,
            "packing_ms": sum(float(built_inputs[page_id]["packing_ms"]) for page_id in page_ids),
        }

    def prefill_configuration(
        self,
        *,
        snapshots: Mapping[str, Mapping[str, Any]],
        candidate_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
        candidate_counts: Mapping[str, Mapping[str, int]],
        input_config: Mapping[str, Any],
        model: HuggingFaceReranker,
        max_length: int,
    ) -> dict[str, Any]:
        """Batch missing pairs across queries without changing any model inputs or scores."""
        import torch

        jobs: list[dict[str, Any]] = []
        already_cached = 0
        for session_id, snapshot in snapshots.items():
            pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
            for query in snapshot["queries"]:
                query_id = str(query["query_id"])
                query_text = str(query["original_query"])
                count = int(candidate_counts[session_id][query_id])
                for item in candidate_rankings[session_id][query_id][:count]:
                    page_id = str(item["page_id"])
                    built = build_input(
                        model.tokenizer,
                        query_text,
                        pages_by_id[page_id],
                        input_config,
                        max_length=max_length,
                    )
                    cache_key = self.key(
                        query_id=query_id,
                        page_id=page_id,
                        query_text=query_text,
                        built_input=built,
                        input_config=str(input_config["id"]),
                        model_name=str(model.config.model),
                        max_length=max_length,
                    )
                    if cache_key in self.by_key:
                        already_cached += 1
                        continue
                    jobs.append(
                        {
                            "cache_key": cache_key,
                            "query_id": query_id,
                            "page_id": page_id,
                            "query_text": query_text,
                            "text": str(built["text"]),
                            "mode": str(built["mode"]),
                        }
                    )

        batch_size = int(model.config.batch_size)
        for start in range(0, len(jobs), batch_size):
            batch = jobs[start : start + batch_size]
            modes = {str(job["mode"]) for job in batch}
            if len(modes) != 1:
                raise ValueError(f"Mixed modes in prefill batch: {modes}")
            encoded = model.tokenizer(
                [[str(job["query_text"]), str(job["text"])] for job in batch],
                padding=True,
                truncation=modes == {"auto"},
                max_length=max_length if modes == {"auto"} else None,
                return_tensors="pt",
            )
            if int(encoded["input_ids"].shape[1]) > max_length:
                raise AssertionError(
                    f"Budgeted prefill pair exceeds model limit: {int(encoded['input_ids'].shape[1])}>{max_length}"
                )
            encoded = encoded.to(model.device)
            started = time.perf_counter()
            with torch.no_grad():
                raw = model.model(**encoded).logits.squeeze(-1).detach().cpu().numpy()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            raw_values = np.atleast_1d(raw).astype(float).tolist()
            normalized = HuggingFaceReranker._normalize_scores(raw_values)
            share = elapsed_ms / len(batch)
            self.append(
                [
                    {
                        "cache_key": job["cache_key"],
                        "query_id": job["query_id"],
                        "page_id": job["page_id"],
                        "input_config": input_config["id"],
                        "model": str(model.config.model),
                        "max_length": max_length,
                        "raw_score": raw_score,
                        "score": score,
                        "inference_share_ms": share,
                        "origin": "local_batched_prefill",
                        "status": "SUCCESS",
                    }
                    for job, raw_score, score in zip(batch, raw_values, normalized)
                ]
            )
        return {
            "input_config": input_config["id"],
            "already_cached": already_cached,
            "new_pair_count": len(jobs),
            "batch_size": batch_size,
        }


def locate_keyword_tokens(tokenizer: Any, document: str, keyword_line: str) -> tuple[int | None, int | None]:
    if not keyword_line:
        return None, None
    document_ids = tokenizer.encode(document, add_special_tokens=False)
    keyword_ids = tokenizer.encode(keyword_line, add_special_tokens=False)
    if keyword_ids and document_ids[-len(keyword_ids) :] == keyword_ids:
        return len(document_ids) - len(keyword_ids), len(document_ids)
    for start in range(len(document_ids) - len(keyword_ids), -1, -1):
        if document_ids[start : start + len(keyword_ids)] == keyword_ids:
            return start, start + len(keyword_ids)
    return None, None


def pair_audit_row(
    tokenizer: Any,
    *,
    session_id: str,
    query: Mapping[str, Any],
    page: Mapping[str, Any],
    candidate_rank: int,
    movement: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    query_text = str(query["original_query"])
    document = page_representation(page, "P8")
    query_ids = tokenizer.encode(query_text, add_special_tokens=False)
    document_ids = tokenizer.encode(document, add_special_tokens=False)
    before_ids = tokenizer(query_text, document, truncation=False)["input_ids"]
    encoded = tokenizer(query_text, document, truncation=True, max_length=MAX_LENGTH)
    sequence_ids = encoded.sequence_ids()
    actual_document_ids = [token for token, sequence_id in zip(encoded["input_ids"], sequence_ids) if sequence_id == 1]
    keyword_line = f"Keywords: {keyword_text(page)}" if keyword_text(page) else ""
    keyword_start, keyword_end = locate_keyword_tokens(tokenizer, document, keyword_line)
    visible_document_tokens = len(actual_document_ids)
    if keyword_start is None or keyword_end is None:
        keyword_visibility = "NO_KEYWORDS"
    elif visible_document_tokens >= keyword_end:
        keyword_visibility = "KEYWORDS_FULLY_VISIBLE"
    elif visible_document_tokens <= keyword_start:
        keyword_visibility = "KEYWORDS_NOT_VISIBLE"
    else:
        keyword_visibility = "KEYWORDS_PARTIALLY_VISIBLE"
    pair_tokens = len(before_ids)
    actual_tokens = len(encoded["input_ids"])
    truncated_tokens = max(0, pair_tokens - actual_tokens)
    if pair_tokens <= 256:
        bucket = "<=256"
    elif pair_tokens <= 512:
        bucket = "257-512"
    elif pair_tokens <= 768:
        bucket = "513-768"
    elif pair_tokens <= 1024:
        bucket = "769-1024"
    elif pair_tokens <= 2048:
        bucket = "1025-2048"
    else:
        bucket = ">2048"
    gold_ids = {str(page_id) for page_id in query.get("eligible_gold_page_ids") or []}
    row = {
        "session_id": session_id,
        "session_code": session_code(session_id),
        "query_id": query["query_id"],
        "page_id": page["page_id"],
        "candidate_rank": candidate_rank,
        "is_eligible_gold": str(page["page_id"]) in gold_ids,
        "movement_category": movement,
        "query_tokens": len(query_ids),
        "document_tokens": len(document_ids),
        "special_tokens": tokenizer.num_special_tokens_to_add(pair=True),
        "pair_tokens_before_truncation": pair_tokens,
        "actual_pair_tokens": actual_tokens,
        "effective_max_length": MAX_LENGTH,
        "truncated_tokens": truncated_tokens,
        "truncation_ratio": truncated_tokens / pair_tokens if pair_tokens else 0.0,
        "was_truncated": truncated_tokens > 0,
        "actual_query_tokens": sequence_ids.count(0),
        "actual_document_tokens": visible_document_tokens,
        "document_prefix_preserved": actual_document_ids == document_ids[:visible_document_tokens],
        "keywords_start_token": keyword_start,
        "keywords_end_token": keyword_end,
        "keywords_visibility": keyword_visibility,
        "length_bucket": bucket,
    }
    debug = {
        "session_id": session_id,
        "query_id": query["query_id"],
        "page_id": page["page_id"],
        "movement_category": movement,
        "original_query": query_text,
        "full_P8": document,
        "actual_token_count": actual_tokens,
        "pair_tokens_before_truncation": pair_tokens,
        "truncated_input_preview": tokenizer.decode(encoded["input_ids"], skip_special_tokens=False),
        "truncated_document_preview": tokenizer.decode(actual_document_ids, skip_special_tokens=True),
        "keywords_visible": keyword_visibility,
    }
    return row, debug


def summarize_audit_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def stats(selected: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        count = len(selected)
        visibility = Counter(str(row["keywords_visibility"]) for row in selected)
        truncated = sum(bool(row["was_truncated"]) for row in selected)
        return {
            "count": count,
            "truncated_count": truncated,
            "truncation_rate": truncated / count if count else None,
            "pair_over_512_count": sum(int(row["pair_tokens_before_truncation"]) > MAX_LENGTH for row in selected),
            "pair_over_512_rate": (
                sum(int(row["pair_tokens_before_truncation"]) > MAX_LENGTH for row in selected) / count
                if count
                else None
            ),
            "mean_pair_tokens": statistics.fmean(float(row["pair_tokens_before_truncation"]) for row in selected)
            if selected
            else None,
            "mean_truncation_ratio": statistics.fmean(float(row["truncation_ratio"]) for row in selected)
            if selected
            else None,
            "keyword_visibility_counts": dict(visibility),
            "keywords_fully_visible_rate": visibility["KEYWORDS_FULLY_VISIBLE"] / count if count else None,
            "keywords_partially_visible_rate": visibility["KEYWORDS_PARTIALLY_VISIBLE"] / count if count else None,
            "keywords_not_visible_rate": visibility["KEYWORDS_NOT_VISIBLE"] / count if count else None,
            "length_buckets": dict(Counter(str(row["length_bucket"]) for row in selected)),
        }

    groups: dict[str, list[Mapping[str, Any]]] = {
        "ALL_CANDIDATES": list(rows),
        "ALL_GOLD_CANDIDATES": [row for row in rows if row["is_eligible_gold"]],
        "DEMOTED_OUT_OF_TOP5": [row for row in rows if row["movement_category"] == "DEMOTED_OUT_OF_TOP5"],
        "PROMOTED_INTO_TOP5": [row for row in rows if row["movement_category"] == "PROMOTED_INTO_TOP5"],
        "PRESERVED_HIT": [row for row in rows if row["movement_category"] == "PRESERVED_HIT"],
        "STILL_MISS": [row for row in rows if row["movement_category"] == "STILL_MISS"],
        "NON_DEMOTED_GOLD": [
            row
            for row in rows
            if row["is_eligible_gold"] and row["movement_category"] != "DEMOTED_OUT_OF_TOP5"
        ],
    }
    return {
        "groups": {name: stats(selected) for name, selected in groups.items()},
        "by_session": {
            code: stats([row for row in rows if row["session_code"] == code]) for code in SESSION_CODES
        },
        "by_session_gold": {
            code: stats(
                [row for row in rows if row["session_code"] == code and row["is_eligible_gold"]]
            )
            for code in SESSION_CODES
        },
    }


def audit_truncation(
    tokenizer: Any,
    snapshots: Mapping[str, Mapping[str, Any]],
    candidate_rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    candidate_counts: Mapping[str, Mapping[str, Mapping[str, int]]],
    components: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    current_quality_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    debug_rows: list[dict[str, Any]] = []
    for session_id, snapshot in snapshots.items():
        pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            ranking = candidate_rankings["C2_RRF60_20"][session_id][query_id]
            count = candidate_counts["C2_RRF60_20"][session_id][query_id]
            gold_ids = {str(page_id) for page_id in query.get("eligible_gold_page_ids") or []}
            for candidate_rank, item in enumerate(ranking[:count], start=1):
                page_id = str(item["page_id"])
                movement = "NON_GOLD_CANDIDATE"
                if page_id in gold_ids:
                    baseline_rank = rank_of(components[session_id][query_id]["dense"], page_id)
                    quality_rank = rank_of(current_quality_rankings[session_id][query_id], page_id)
                    if baseline_rank is None or quality_rank is None:
                        raise ValueError(f"Gold rank missing in truncation audit: {query_id}/{page_id}")
                    movement = movement_category(baseline_rank, quality_rank)
                row, debug = pair_audit_row(
                    tokenizer,
                    session_id=session_id,
                    query=query,
                    page=pages_by_id[page_id],
                    candidate_rank=candidate_rank,
                    movement=movement,
                )
                rows.append(row)
                if (
                    session_code(session_id) == "S005" and movement == "DEMOTED_OUT_OF_TOP5"
                ) or (
                    session_code(session_id) == "S001"
                    and movement == "PROMOTED_INTO_TOP5"
                    and not any(
                        existing["session_id"] == session_id
                        and existing["movement_category"] == "PROMOTED_INTO_TOP5"
                        for existing in debug_rows
                    )
                ):
                    debug_rows.append(debug)
    tokenizer_config = {
        "tokenizer_class": type(tokenizer).__name__,
        "truncation_side": tokenizer.truncation_side,
        "padding_side": tokenizer.padding_side,
        "model_max_length": int(tokenizer.model_max_length),
        "configured_max_length": MAX_LENGTH,
        "pair_special_tokens": tokenizer.num_special_tokens_to_add(pair=True),
        "truncation_argument": True,
        "resolved_pair_strategy": "longest_first",
        "empirical_document_prefix_preserved_rate": sum(bool(row["document_prefix_preserved"]) for row in rows)
        / len(rows),
    }
    summary = {"tokenizer": tokenizer_config, **summarize_audit_rows(rows)}
    return rows, summary, debug_rows


def pooled_queries(
    snapshots: Mapping[str, Mapping[str, Any]],
    session_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    selected = set(session_ids or snapshots)
    return [
        query
        for session_id, snapshot in snapshots.items()
        if session_id in selected
        for query in snapshot["queries"]
    ]


def pooled_rankings(
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    session_ids: Sequence[str] | None = None,
) -> dict[str, Sequence[Mapping[str, Any]]]:
    selected = set(session_ids or rankings)
    return {
        query_id: ranking
        for session_id, session_rankings in rankings.items()
        if session_id in selected
        for query_id, ranking in session_rankings.items()
    }


def candidate_recall(
    queries: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Sequence[Mapping[str, Any]]],
    counts: Mapping[str, int],
) -> tuple[float, int, int]:
    hits = 0
    total = 0
    for query in queries:
        query_id = str(query["query_id"])
        candidate_ids = {
            str(item["page_id"]) for item in rankings[query_id][: int(counts[query_id])]
        }
        for page_id in query.get("eligible_gold_page_ids") or []:
            total += 1
            hits += int(str(page_id) in candidate_ids)
    return hits / total if total else 0.0, hits, total


def movement_counts(
    queries: Sequence[Mapping[str, Any]],
    baseline: Mapping[str, Sequence[Mapping[str, Any]]],
    contender: Mapping[str, Sequence[Mapping[str, Any]]],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for query in queries:
        query_id = str(query["query_id"])
        for page_id in query.get("eligible_gold_page_ids") or []:
            baseline_rank = rank_of(baseline[query_id], str(page_id))
            contender_rank = rank_of(contender[query_id], str(page_id))
            if baseline_rank is None or contender_rank is None:
                raise ValueError(f"Gold omitted from full ranking: {query_id}/{page_id}")
            counts[movement_category(baseline_rank, contender_rank)] += 1
    return counts


def score_configuration(
    *,
    snapshots: Mapping[str, Mapping[str, Any]],
    candidate_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    candidate_counts: Mapping[str, Mapping[str, int]],
    input_config: Mapping[str, Any],
    tokenizer: Any,
    model: HuggingFaceReranker,
    cache: PairScoreCache,
    max_length: int,
) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, dict[str, dict[str, Any]]]]:
    cache.prefill_configuration(
        snapshots=snapshots,
        candidate_rankings=candidate_rankings,
        candidate_counts=candidate_counts,
        input_config=input_config,
        model=model,
        max_length=max_length,
    )
    final_rankings: dict[str, dict[str, list[dict[str, Any]]]] = {}
    timing_by_session: dict[str, dict[str, dict[str, Any]]] = {}
    for session_id, snapshot in snapshots.items():
        final_rankings[session_id] = {}
        timing_by_session[session_id] = {}
        pages_by_id = {str(page["page_id"]): page for page in snapshot["pages"]}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            query_text = str(query["original_query"])
            ranking = candidate_rankings[session_id][query_id]
            actual_count = int(candidate_counts[session_id][query_id])
            page_ids = [str(item["page_id"]) for item in ranking[:actual_count]]
            built_inputs = {
                page_id: build_input(
                    tokenizer,
                    query_text,
                    pages_by_id[page_id],
                    input_config,
                    max_length=max_length,
                )
                for page_id in page_ids
            }
            scores, timing = cache.score(
                query_id=query_id,
                query_text=query_text,
                page_ids=page_ids,
                built_inputs=built_inputs,
                input_config=str(input_config["id"]),
                model=model,
                max_length=max_length,
            )
            ordered = sorted(page_ids, key=lambda page_id: (-float(scores[page_id]), page_id))
            final_rankings[session_id][query_id] = append_reranked_candidates(
                ranking,
                ordered,
                candidate_k=actual_count,
            )
            timing_by_session[session_id][query_id] = timing
    return final_rankings, timing_by_session


def evaluate_configuration(
    *,
    config_id: str,
    candidate_id: str,
    input_id: str,
    snapshots: Mapping[str, Mapping[str, Any]],
    candidate_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    candidate_counts: Mapping[str, Mapping[str, int]],
    final_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    dense_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    timing: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    per_session: list[dict[str, Any]] = []
    for session_id, snapshot in snapshots.items():
        queries = snapshot["queries"]
        metrics, _ = evaluate_rankings(queries, final_rankings[session_id])
        candidate_value, candidate_hits, total = candidate_recall(
            queries,
            candidate_rankings[session_id],
            candidate_counts[session_id],
        )
        movement = movement_counts(queries, dense_rankings[session_id], final_rankings[session_id])
        counts = [candidate_counts[session_id][str(query["query_id"])] for query in queries]
        timing_rows = list(timing[session_id].values())
        per_session.append(
            {
                "config_id": config_id,
                "candidate_id": candidate_id,
                "input_id": input_id,
                "session_id": session_id,
                "session_code": session_code(session_id),
                "selection_status": INPUT_STATUS,
                "eligible_gold_count": total,
                "candidate_recall": candidate_value,
                "candidate_hits": candidate_hits,
                "mean_candidate_count": statistics.fmean(counts) if counts else 0.0,
                "p95_candidate_count": percentile(counts, 95),
                "recall_at_5": metrics["recall_at_5"],
                "recall_at_10": metrics["recall_at_10"],
                "mrr": metrics["mrr"],
                "ndcg_at_5": metrics["ndcg_at_5"],
                "mean_gold_rank": metrics["mean_gold_rank"],
                "median_gold_rank": metrics["median_gold_rank"],
                "promoted": movement["PROMOTED_INTO_TOP5"],
                "demoted": movement["DEMOTED_OUT_OF_TOP5"],
                "preserved_hit": movement["PRESERVED_HIT"],
                "still_miss": movement["STILL_MISS"],
                "net_gain": movement["PROMOTED_INTO_TOP5"] - movement["DEMOTED_OUT_OF_TOP5"],
                "packing_mean_ms": statistics.fmean(float(row["packing_ms"]) for row in timing_rows)
                if timing_rows
                else 0.0,
                "rerank_estimated_mean_ms": statistics.fmean(
                    float(row["cached_estimated_inference_ms"]) for row in timing_rows
                )
                if timing_rows
                else 0.0,
            }
        )
    all_queries = pooled_queries(snapshots)
    metrics, _ = evaluate_rankings(all_queries, pooled_rankings(final_rankings))
    candidate_value, candidate_hits, total = candidate_recall(
        all_queries,
        pooled_rankings(candidate_rankings),
        {
            query_id: count
            for session_counts in candidate_counts.values()
            for query_id, count in session_counts.items()
        },
    )
    movement = movement_counts(all_queries, pooled_rankings(dense_rankings), pooled_rankings(final_rankings))
    all_counts = [count for session_counts in candidate_counts.values() for count in session_counts.values()]
    aggregate = {
        "config_id": config_id,
        "candidate_id": candidate_id,
        "input_id": input_id,
        "scope": "S001-S005 development/diagnosis aggregate; not held-out",
        "selection_status": INPUT_STATUS,
        "session_count": len(snapshots),
        "eligible_gold_count": total,
        "candidate_recall": candidate_value,
        "candidate_hits": candidate_hits,
        "mean_candidate_count": statistics.fmean(all_counts),
        "p95_candidate_count": percentile(all_counts, 95),
        "recall_at_5": metrics["recall_at_5"],
        "recall_at_10": metrics["recall_at_10"],
        "mrr": metrics["mrr"],
        "ndcg_at_5": metrics["ndcg_at_5"],
        "mean_gold_rank": metrics["mean_gold_rank"],
        "median_gold_rank": metrics["median_gold_rank"],
        "macro_session_recall_at_5": statistics.fmean(float(row["recall_at_5"]) for row in per_session),
        "macro_session_mrr": statistics.fmean(float(row["mrr"]) for row in per_session),
        "macro_session_ndcg_at_5": statistics.fmean(float(row["ndcg_at_5"]) for row in per_session),
        "promoted": movement["PROMOTED_INTO_TOP5"],
        "demoted": movement["DEMOTED_OUT_OF_TOP5"],
        "preserved_hit": movement["PRESERVED_HIT"],
        "still_miss": movement["STILL_MISS"],
        "net_gain": movement["PROMOTED_INTO_TOP5"] - movement["DEMOTED_OUT_OF_TOP5"],
        "packing_mean_ms": statistics.fmean(float(row["packing_mean_ms"]) for row in per_session),
        "rerank_estimated_mean_ms": statistics.fmean(
            float(row["rerank_estimated_mean_ms"]) for row in per_session
        ),
    }
    return aggregate, per_session


def selection_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        float(row["recall_at_5"]),
        float(row["macro_session_recall_at_5"]),
        float(row["ndcg_at_5"]),
        float(row["mrr"]),
        -int(row["demoted"]),
        -float(row["mean_candidate_count"]),
        -float(row.get("total_e2e_p95_ms") or row.get("rerank_estimated_mean_ms") or 0.0),
    )


def load_current_quality_scores(
    s001_result_dir: Path,
    multi_result_dir: Path,
) -> dict[str, dict[str, float]]:
    heldout = {
        str(row["query_id"]): {
            str(page_id): float(score) for page_id, score in row["scores_by_page"].items()
        }
        for row in load_jsonl(multi_result_dir / "cache/rerank/local_scores.jsonl")
        if row.get("status") == "SUCCESS"
        and row.get("reranker_model") == BASE_RERANKER
        and row.get("rerank_representation") == "P8"
    }
    return {**load_s001_reranker_scores(s001_result_dir), **heldout}


def current_quality_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    candidate_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    candidate_counts: Mapping[str, Mapping[str, int]],
    scores: Mapping[str, Mapping[str, float]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    result: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for session_id, snapshot in snapshots.items():
        result[session_id] = {}
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            ranking = candidate_rankings[session_id][query_id]
            count = int(candidate_counts[session_id][query_id])
            page_ids = [str(item["page_id"]) for item in ranking[:count]]
            missing = [page_id for page_id in page_ids if page_id not in scores.get(query_id, {})]
            if missing:
                raise ValueError(f"Frozen Quality score cache missing {query_id}: {missing}")
            ordered = sorted(page_ids, key=lambda page_id: (-float(scores[query_id][page_id]), page_id))
            result[session_id][query_id] = append_reranked_candidates(ranking, ordered, candidate_k=count)
    return result


def validate_frozen_quality(
    snapshots: Mapping[str, Mapping[str, Any]],
    quality_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> dict[str, Any]:
    by_code = {session_code(session_id): session_id for session_id in snapshots}
    s001, _ = evaluate_rankings(
        snapshots[by_code["S001"]]["queries"], quality_rankings[by_code["S001"]]
    )
    heldout_ids = [by_code[code] for code in SESSION_CODES[1:]]
    heldout, _ = evaluate_rankings(
        pooled_queries(snapshots, heldout_ids), pooled_rankings(quality_rankings, heldout_ids)
    )
    expected = {"s001_recall_at_5": 10 / 21, "s002_s005_recall_at_5": 44 / 133}
    actual = {
        "s001_recall_at_5": float(s001["recall_at_5"]),
        "s002_s005_recall_at_5": float(heldout["recall_at_5"]),
    }
    if any(not math.isclose(actual[key], value, abs_tol=1e-12) for key, value in expected.items()):
        raise RuntimeError(f"Frozen Quality reproduction failed: expected={expected}, actual={actual}")
    pooled, _ = evaluate_rankings(pooled_queries(snapshots), pooled_rankings(quality_rankings))
    return {"status": "PASS", "expected": expected, "actual": actual, "pooled_metrics": pooled}


def candidate_pool_ablation(
    snapshots: Mapping[str, Mapping[str, Any]],
    components: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    candidate_rankings: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
    candidate_counts: Mapping[str, Mapping[str, Mapping[str, int]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    aggregate_rows: list[dict[str, Any]] = []
    per_session_rows: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for config in CANDIDATE_CONFIGS:
        config_id = str(config["id"])
        for session_id, snapshot in snapshots.items():
            queries = snapshot["queries"]
            rankings = candidate_rankings[config_id][session_id]
            counts = candidate_counts[config_id][session_id]
            candidate_value, hits, total = candidate_recall(queries, rankings, counts)
            metrics, _ = evaluate_rankings(queries, rankings)
            actual_counts = list(counts.values())
            per_session_rows.append(
                {
                    "candidate_id": config_id,
                    "session_id": session_id,
                    "session_code": session_code(session_id),
                    "selection_status": INPUT_STATUS,
                    "eligible_gold_count": total,
                    "candidate_recall": candidate_value,
                    "candidate_hits": hits,
                    "mean_candidate_count": statistics.fmean(actual_counts),
                    "p95_candidate_count": percentile(actual_counts, 95),
                    "candidate_top5_recall": metrics["recall_at_5"],
                    "candidate_top5_mrr": metrics["mrr"],
                    "candidate_top5_ndcg_at_5": metrics["ndcg_at_5"],
                }
            )
        all_queries = pooled_queries(snapshots)
        rankings = pooled_rankings(candidate_rankings[config_id])
        counts = {
            query_id: count
            for session_counts in candidate_counts[config_id].values()
            for query_id, count in session_counts.items()
        }
        candidate_value, hits, total = candidate_recall(all_queries, rankings, counts)
        metrics, _ = evaluate_rankings(all_queries, rankings)
        matching = [row for row in per_session_rows if row["candidate_id"] == config_id]
        aggregate_rows.append(
            {
                "candidate_id": config_id,
                "strategy": config["strategy"],
                "configured_k": config["candidate_k"],
                "scope": "S001-S005 development/diagnosis aggregate; not held-out",
                "selection_status": INPUT_STATUS,
                "eligible_gold_count": total,
                "candidate_recall": candidate_value,
                "candidate_hits": hits,
                "mean_candidate_count": statistics.fmean(counts.values()),
                "p95_candidate_count": percentile(list(counts.values()), 95),
                "candidate_top5_recall": metrics["recall_at_5"],
                "candidate_top5_mrr": metrics["mrr"],
                "candidate_top5_ndcg_at_5": metrics["ndcg_at_5"],
                "macro_candidate_recall": statistics.fmean(float(row["candidate_recall"]) for row in matching),
            }
        )

    for session_id, snapshot in snapshots.items():
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            dense = components[session_id][query_id]["dense"]
            bm25 = components[session_id][query_id]["bm25"]
            for page_id in query.get("eligible_gold_page_ids") or []:
                page_id = str(page_id)
                dense_rank = rank_of(dense, page_id)
                bm25_rank = rank_of(bm25, page_id)
                if dense_rank is None or bm25_rank is None:
                    raise ValueError(f"Visible Gold missing component rank: {query_id}/{page_id}")
                row: dict[str, Any] = {
                    "session_id": session_id,
                    "session_code": session_code(session_id),
                    "query_id": query_id,
                    "gold_page_id": page_id,
                    "dense_rank": dense_rank,
                    "bm25_rank": bm25_rank,
                    "in_dense20": dense_rank <= 20,
                    "in_bm25_20": bm25_rank <= 20,
                }
                for config in CANDIDATE_CONFIGS:
                    config_id = str(config["id"])
                    ranking = candidate_rankings[config_id][session_id][query_id]
                    count = candidate_counts[config_id][session_id][query_id]
                    row[f"in_{config_id.lower()}"] = page_id in {
                        str(item["page_id"]) for item in ranking[:count]
                    }
                row["preservation_category"] = (
                    "IN_DENSE_OR_BM25_BUT_DROPPED_BY_RRF20"
                    if (row["in_dense20"] or row["in_bm25_20"]) and not row["in_c2_rrf60_20"]
                    else "PRESERVED_BY_RRF20"
                    if row["in_c2_rrf60_20"]
                    else "NOT_IN_DENSE_OR_BM25_TOP20"
                )
                details.append(row)
    return aggregate_rows, per_session_rows, details


def winner_movement_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    dense_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    schemes: Mapping[str, Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]],
) -> list[dict[str, Any]]:
    rows = []
    for session_id, snapshot in snapshots.items():
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            for page_id in query.get("eligible_gold_page_ids") or []:
                page_id = str(page_id)
                baseline_rank = rank_of(dense_rankings[session_id][query_id], page_id)
                row: dict[str, Any] = {
                    "session_id": session_id,
                    "session_code": session_code(session_id),
                    "query_id": query_id,
                    "gold_page_id": page_id,
                    "baseline_dense_rank": baseline_rank,
                    "baseline_hit5": bool(baseline_rank and baseline_rank <= OUTPUT_K),
                }
                for scheme_id, rankings in schemes.items():
                    final_rank = rank_of(rankings[session_id][query_id], page_id)
                    if baseline_rank is None or final_rank is None:
                        raise ValueError(f"Winner ranking missing Gold: {scheme_id}/{query_id}/{page_id}")
                    row[f"{scheme_id}_rank"] = final_rank
                    row[f"{scheme_id}_hit5"] = final_rank <= OUTPUT_K
                    row[f"{scheme_id}_movement"] = movement_category(baseline_rank, final_rank)
                rows.append(row)
    return rows


def direct_scores(
    model: HuggingFaceReranker,
    query_text: str,
    page_ids: Sequence[str],
    built_inputs: Mapping[str, Mapping[str, Any]],
    *,
    max_length: int,
) -> dict[str, float]:
    import torch

    modes = {str(built_inputs[page_id]["mode"]) for page_id in page_ids}
    if modes == {"auto"}:
        documents = [{"page_id": page_id, "memory": built_inputs[page_id]["text"]} for page_id in page_ids]
        return {
            str(item["page_id"]): float(item["rerank_score"])
            for item in model.rerank(query_text, documents, top_k=len(documents))
        }
    if modes != {"token_ids"}:
        raise ValueError(f"Mixed direct score modes: {modes}")
    query_ids = model.tokenizer.encode(query_text, add_special_tokens=False)
    features = [
        model.tokenizer.prepare_for_model(
            query_ids,
            list(built_inputs[page_id]["document_ids"] or []),
            add_special_tokens=True,
            truncation=False,
            return_attention_mask=True,
        )
        for page_id in page_ids
    ]
    encoded = model.tokenizer.pad(features, padding=True, return_tensors="pt").to(model.device)
    if int(encoded["input_ids"].shape[1]) > max_length:
        raise AssertionError(f"Direct packed batch exceeded max_length={max_length}")
    with torch.no_grad():
        raw = model.model(**encoded).logits.squeeze(-1).detach().cpu().numpy()
    normalized = HuggingFaceReranker._normalize_scores(np.atleast_1d(raw).astype(float).tolist())
    return {page_id: score for page_id, score in zip(page_ids, normalized)}


def candidate_for_query(
    *,
    query_text: str,
    pages: Sequence[Mapping[str, Any]],
    query_vector: Sequence[float],
    page_vectors: Mapping[str, Sequence[float]],
    candidate_config: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    dense = cosine_rank(query_vector, pages, page_vectors)
    texts = {str(page["page_id"]): page_representation(page, "P0") for page in pages}
    bm25 = ChineseBM25Index(pages, texts).rank(query_text)
    return candidate_ranking({"dense": dense, "bm25": bm25, "rrf": rrf_fuse((dense, bm25), rank_constant=60)}, candidate_config)


def benchmark_selected_e2e(
    *,
    output_dir: Path,
    snapshots: Mapping[str, Mapping[str, Any]],
    schemes: Sequence[Mapping[str, Any]],
    input_configs: Mapping[str, Mapping[str, Any]],
    candidate_configs: Mapping[str, Mapping[str, Any]],
    model: HuggingFaceReranker,
    warmups: int,
    repeats: int,
    snapshot_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    latency_dir = output_dir / "cache/latency"
    contract_path = latency_dir / "contract.json"
    rows_path = latency_dir / "measurements.csv"
    contract = {
        "version": 2,
        "snapshot_hash": snapshot_hash,
        "schemes": list(schemes),
        "warmups": warmups,
        "repeats": repeats,
        "max_length": MAX_LENGTH,
        "clock": "time.perf_counter",
        "includes": "query embedding + Dense + Chinese BM25 + candidate construction + packing + local reranker + Top5",
        "identical_chain_policy": "measure once and reuse the same sample under winner aliases",
    }
    if contract_path.exists() and rows_path.exists() and load_json(contract_path) == contract:
        rows = [
            {
                **row,
                **{
                    key: float(row[key])
                    for key in ("candidate_generation_ms", "packing_ms", "reranker_ms", "total_e2e_ms")
                },
                "repeat": int(row["repeat"]),
            }
            for row in read_csv(rows_path)
        ]
        reused = True
        embed_status = load_json(output_dir / "cache/latency/embedding_model_status.json")
    else:
        latency_dir.mkdir(parents=True, exist_ok=True)
        embedding_cache = EmbeddingCache(latency_dir / "embedding_cache")
        load_started = time.perf_counter()
        embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
        embed_status = {
            **(embedding_cache.status.get(PRODUCTION_EMBEDDING) or {}),
            "observed_load_call_ms": (time.perf_counter() - load_started) * 1000.0,
        }
        dump_json(output_dir / "cache/latency/embedding_model_status.json", embed_status)
        visible_by_session = {
            session_id: visible_pages_by_query(snapshot) for session_id, snapshot in snapshots.items()
        }

        def run_one(session_id: str, query: Mapping[str, Any], scheme: Mapping[str, Any]) -> dict[str, float]:
            query_id = str(query["query_id"])
            query_text = str(query["original_query"])
            pages = visible_by_session[session_id][query_id]
            page_vectors = {str(page["page_id"]): page["stored_embedding"] for page in pages}
            total_started = time.perf_counter()
            candidate_started = time.perf_counter()
            query_vector = embedder.embed(query_text, "search")
            ranking, count = candidate_for_query(
                query_text=query_text,
                pages=pages,
                query_vector=query_vector,
                page_vectors=page_vectors,
                candidate_config=candidate_configs[str(scheme["candidate_id"])],
            )
            candidate_ms = (time.perf_counter() - candidate_started) * 1000.0
            page_by_id = {str(page["page_id"]): page for page in pages}
            page_ids = [str(item["page_id"]) for item in ranking[:count]]
            packing_started = time.perf_counter()
            built_inputs = {
                page_id: build_input(
                    model.tokenizer,
                    query_text,
                    page_by_id[page_id],
                    input_configs[str(scheme["input_id"])],
                    max_length=MAX_LENGTH,
                )
                for page_id in page_ids
            }
            packing_ms = (time.perf_counter() - packing_started) * 1000.0
            reranker_started = time.perf_counter()
            scores = direct_scores(model, query_text, page_ids, built_inputs, max_length=MAX_LENGTH)
            sorted(page_ids, key=lambda page_id: (-scores[page_id], page_id))[:OUTPUT_K]
            reranker_ms = (time.perf_counter() - reranker_started) * 1000.0
            return {
                "candidate_generation_ms": candidate_ms,
                "packing_ms": packing_ms,
                "reranker_ms": reranker_ms,
                "total_e2e_ms": (time.perf_counter() - total_started) * 1000.0,
            }

        unique_schemes: list[Mapping[str, Any]] = []
        canonical_by_signature: dict[tuple[str, str], Mapping[str, Any]] = {}
        for scheme in schemes:
            signature = (str(scheme["candidate_id"]), str(scheme["input_id"]))
            if signature not in canonical_by_signature:
                canonical_by_signature[signature] = scheme
                unique_schemes.append(scheme)
        first_session_id, first_snapshot = next(iter(snapshots.items()))
        first_query = first_snapshot["queries"][0]
        for scheme in unique_schemes:
            for _ in range(max(warmups, 0)):
                run_one(first_session_id, first_query, scheme)
        rows = []
        for repeat in range(max(repeats, 1)):
            for session_id, snapshot in snapshots.items():
                for query in snapshot["queries"]:
                    measurements = {
                        (str(scheme["candidate_id"]), str(scheme["input_id"])): run_one(
                            session_id,
                            query,
                            scheme,
                        )
                        for scheme in unique_schemes
                    }
                    for scheme in schemes:
                        signature = (str(scheme["candidate_id"]), str(scheme["input_id"]))
                        rows.append(
                            {
                                "scheme_id": scheme["id"],
                                "candidate_id": scheme["candidate_id"],
                                "input_id": scheme["input_id"],
                                "session_id": session_id,
                                "session_code": session_code(session_id),
                                "query_id": query["query_id"],
                                "repeat": repeat + 1,
                                "measurement_reused_for_identical_chain": (
                                    str(canonical_by_signature[signature]["id"]) != str(scheme["id"])
                                ),
                                **measurements[signature],
                            }
                        )
        write_csv(rows_path, rows)
        dump_json(contract_path, contract)
        embedding_cache.release(PRODUCTION_EMBEDDING)
        reused = False
    summary_rows = []
    for scheme in schemes:
        selected = [row for row in rows if row["scheme_id"] == scheme["id"]]
        summary: dict[str, Any] = {
            "scheme_id": scheme["id"],
            "candidate_id": scheme["candidate_id"],
            "input_id": scheme["input_id"],
            "warmups": warmups,
            "repeats": repeats,
            "cold_start_excluded": True,
        }
        for component in ("candidate_generation_ms", "packing_ms", "reranker_ms", "total_e2e_ms"):
            stats = latency_stats(float(row[component]) for row in selected)
            summary.update({f"{component}_{key}": value for key, value in stats.items()})
        summary_rows.append(summary)
    return rows, summary_rows, {
        "status": "PASS",
        "measurement_cache_reused": reused,
        "contract": contract,
        "embedding_model": embed_status,
    }


def future_finetuning_stats(
    snapshots: Mapping[str, Mapping[str, Any]],
    candidate_rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    candidate_counts: Mapping[str, Mapping[str, int]],
) -> dict[str, Any]:
    by_session = {}
    for session_id, snapshot in snapshots.items():
        positives = sum(len(query.get("eligible_gold_page_ids") or []) for query in snapshot["queries"])
        hard_negatives = 0
        for query in snapshot["queries"]:
            query_id = str(query["query_id"])
            gold = {str(page_id) for page_id in query.get("eligible_gold_page_ids") or []}
            candidates = {
                str(item["page_id"])
                for item in candidate_rankings[session_id][query_id][: candidate_counts[session_id][query_id]]
            }
            hard_negatives += len(candidates - gold)
        by_session[session_code(session_id)] = {
            "session_id": session_id,
            "positive_pairs": positives,
            "hard_negative_pairs": hard_negatives,
        }
    return {
        "status": "STATS_ONLY_NO_TRAINING",
        "positive_definition": "Q -> eligible exact Gold Page",
        "hard_negative_definition": "best candidate pool Page that is not an eligible exact Gold",
        "per_session": by_session,
        "total_positive_pairs": sum(row["positive_pairs"] for row in by_session.values()),
        "total_hard_negative_pairs": sum(row["hard_negative_pairs"] for row in by_session.values()),
    }


def metric_delta_text(current: Mapping[str, Any], contender: Mapping[str, Any]) -> str:
    delta = 100 * (float(contender["recall_at_5"]) - float(current["recall_at_5"]))
    hits = round(delta / 100 * int(current["eligible_gold_count"]))
    return f"{current['recall_at_5']:.2%} → {contender['recall_at_5']:.2%} ({delta:+.2f} pp, {hits:+d} Gold)"


def render_report(summary: Mapping[str, Any]) -> str:
    audit = summary["truncation_summary"]
    groups = audit["groups"]
    d0 = summary["winners"]["D0_CURRENT_FROZEN_QUALITY"]
    d1 = summary["winners"]["D1_BEST_INPUT_FIX"]
    d2 = summary["winners"]["D2_BEST_CANDIDATE_INPUT"]
    s005 = summary["s005_comparison"]
    latency = {row["scheme_id"]: row for row in summary["latency_summary"]}
    candidate = {row["candidate_id"]: row for row in summary["candidate_pool_ablation"]}
    direct = summary["direct_answers"]
    lines = [
        "# S001–S005 Reranker Input 与 Candidate Pool 诊断",
        "",
        "> Selection status: **S001-S005_TUNED**。本轮数据不再是 held-out；任何新候选都必须冻结后到 S006+ 验证。",
        "",
        "## 1. 基线复现与实验边界",
        "",
        f"Frozen Quality 精确复现：S001 R@5={summary['baseline_reproduction']['actual']['s001_recall_at_5']:.2%}；S002–S005 R@5={summary['baseline_reproduction']['actual']['s002_s005_recall_at_5']:.2%}。没有重跑完整 Session、没有生成 Page/长期记忆、没有调用 DeepSeek，也没有修改生产检索代码。",
        "",
        "## 2. 真实 tokenizer truncation audit",
        "",
        f"实际 tokenizer={audit['tokenizer']['tokenizer_class']}，configured/model max length={audit['tokenizer']['configured_max_length']}/{audit['tokenizer']['model_max_length']}，truncation side={audit['tokenizer']['truncation_side']}，pair strategy={audit['tokenizer']['resolved_pair_strategy']}，pair special tokens={audit['tokenizer']['pair_special_tokens']}。",
        "",
        "| Group | Count | >512 | Truncation rate | Mean pair tokens | KW fully | KW partial | KW absent |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in (
        "ALL_CANDIDATES",
        "ALL_GOLD_CANDIDATES",
        "DEMOTED_OUT_OF_TOP5",
        "PROMOTED_INTO_TOP5",
        "PRESERVED_HIT",
        "STILL_MISS",
    ):
        row = groups[name]
        lines.append(
            f"| {name} | {row['count']} | {row['pair_over_512_count']} ({row['pair_over_512_rate'] or 0:.2%}) | "
            f"{row['truncation_rate'] or 0:.2%} | {row['mean_pair_tokens'] or 0:.1f} | "
            f"{row['keywords_fully_visible_rate'] or 0:.2%} | {row['keywords_partially_visible_rate'] or 0:.2%} | "
            f"{row['keywords_not_visible_rate'] or 0:.2%} |"
        )
    lines.extend(
        [
            "",
            summary["truncation_causal_assessment"],
            "",
            "实际截断后的输入样例保存在 `truncation_debug_cases.jsonl`，包含所有 S005 demoted Gold 和一个 S001 promoted Gold。",
            "",
            "## 3. Reranker input / packing screening（固定 RRF60 Top20）",
            "",
            "| Input | Micro R@5 | Macro R@5 | MRR | NDCG@5 | Promoted | Demoted | Net | S005 R@5 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    by_session = summary["input_by_session"]
    for row in summary["input_ablation"]:
        if row.get("is_alias"):
            continue
        s005_row = next(
            item
            for item in by_session
            if item["input_id"] == row["input_id"] and item["session_code"] == "S005"
        )
        lines.append(
            f"| {row['input_id']} | {row['recall_at_5']:.2%} | {row['macro_session_recall_at_5']:.2%} | "
            f"{row['mrr']:.4f} | {row['ndcg_at_5']:.4f} | {row['promoted']} | {row['demoted']} | "
            f"{row['net_gain']:+d} | {s005_row['recall_at_5']:.2%} |"
        )
    lines.extend(
        [
            "",
            f"D1 Best Input Fix：`{d1['input_id']}`，{metric_delta_text(d0, d1)}。",
            "",
            "## 4. Candidate pool",
            "",
            "| Candidate | Recall | Hits | Mean K | p95 K | First-stage R@5 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary["candidate_pool_ablation"]:
        lines.append(
            f"| {row['candidate_id']} | {row['candidate_recall']:.2%} | {row['candidate_hits']}/{row['eligible_gold_count']} | "
            f"{row['mean_candidate_count']:.2f} | {row['p95_candidate_count']:.1f} | {row['candidate_top5_recall']:.2%} |"
        )
    union = candidate["C5_UNION20X20"]
    rrf = candidate["C2_RRF60_20"]
    lines.extend(
        [
            "",
            f"Dense20 ∪ BM2520 coverage={union['candidate_recall']:.2%}；RRF20={rrf['candidate_recall']:.2%}。共有 {summary['rrf_dropped_union_gold_count']} 个 Gold 在 Dense20/BM2520 至少一路出现、但被 RRF20 压缩丢失。",
            "",
            "## 5. Candidate × Top2 Input joint ablation",
            "",
            "| Candidate | Input | Candidate R | Final R@5 | Macro R@5 | MRR | NDCG@5 | Promoted | Demoted | Net |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in summary["joint_ablation"]:
        lines.append(
            f"| {row['candidate_id']} | {row['input_id']} | {row['candidate_recall']:.2%} | "
            f"{row['recall_at_5']:.2%} | {row['macro_session_recall_at_5']:.2%} | {row['mrr']:.4f} | "
            f"{row['ndcg_at_5']:.4f} | {row['promoted']} | {row['demoted']} | {row['net_gain']:+d} |"
        )
    lines.extend(
        [
            "",
            f"D2 Best Candidate + Input：`{d2['candidate_id']} + {d2['input_id']}`，{metric_delta_text(d0, d2)}。Candidate coverage 提高并不自动转化为 final Top5；具体 hard-negative effect 见上表。",
            "",
            "## 6. max_length / model",
            "",
            f"bge-reranker-base 的 1024 状态：**{summary['base_1024']['status']}**。原因：{summary['base_1024']['reason']}",
            "",
            f"新 reranker：{summary['new_reranker']['status']}。{summary['new_reranker']['reason']}",
            "",
            "## 7. S005",
            "",
            f"Current Frozen Quality：R@5={s005['D0']['recall_at_5']:.2%}、promoted={s005['D0']['promoted']}、demoted={s005['D0']['demoted']}。D1：R@5={s005['D1']['recall_at_5']:.2%}、demoted={s005['D1']['demoted']}；D2：R@5={s005['D2']['recall_at_5']:.2%}、demoted={s005['D2']['demoted']}。相对当前 7 个 demotion，D2 恢复 {s005['recovered_current_demotions']} 个，并新增 demotion {s005['new_demotions']} 个。",
            "",
            "## 8. Warm E2E latency",
            "",
            "| Scheme | Candidate mean/p95 | Packing mean/p95 | Reranker mean/p95 | Total mean/p95 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for scheme_id in ("D0_CURRENT_FROZEN_QUALITY", "D1_BEST_INPUT_FIX", "D2_BEST_CANDIDATE_INPUT"):
        row = latency[scheme_id]
        lines.append(
            f"| {scheme_id} | {row['candidate_generation_ms_mean']:.2f}/{row['candidate_generation_ms_p95']:.2f} ms | "
            f"{row['packing_ms_mean']:.2f}/{row['packing_ms_p95']:.2f} ms | "
            f"{row['reranker_ms_mean']:.2f}/{row['reranker_ms_p95']:.2f} ms | "
            f"{row['total_e2e_ms_mean']:.2f}/{row['total_e2e_ms_p95']:.2f} ms |"
        )
    lines.extend(
        [
            "",
            "## 9. 结论与下一阶段",
            "",
            f"最终诊断状态：**{summary['diagnosis_status']}**。",
            "",
            summary["recommendation"],
            "",
            "## 10. 直接问题回答",
            "",
        ]
    )
    for index, answer in enumerate(direct, start=1):
        lines.append(f"{index}. {answer}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    if args.latency_warmups < 2 or args.latency_repeats < 1:
        raise ValueError("Latency requires at least two warmups and one measured repeat")
    import torch

    torch.set_num_threads(max(1, args.torch_threads))
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    s001_result_dir = args.s001_result_dir.resolve()
    multi_result_dir = args.multi_result_dir.resolve()
    snapshots, source_files = load_snapshots(s001_result_dir, multi_result_dir)
    upstream_files = [
        *source_files,
        s001_result_dir / "rerank_tuning/cache/rerank/local_scores.jsonl",
        multi_result_dir / "cache/rerank/local_scores.jsonl",
        multi_result_dir / "validation_summary.json",
    ]
    upstream_before = [file_state(path) for path in upstream_files]
    snapshot_hash = stable_hash({session_id: snapshot["manifest"]["snapshot_hash"] for session_id, snapshot in snapshots.items()})
    query_vectors = load_query_vectors(s001_result_dir, multi_result_dir, snapshots)
    components = build_candidate_components(snapshots, query_vectors)
    candidate_rankings, candidate_counts = all_candidate_rankings(snapshots, components)
    dense_rankings = {
        session_id: {
            query_id: values["dense"] for query_id, values in session_components.items()
        }
        for session_id, session_components in components.items()
    }

    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    model_started = time.perf_counter()
    model = HuggingFaceReranker(
        {
            "provider": "huggingface",
            "model": BASE_RERANKER,
            "batch_size": 32,
            "max_length": MAX_LENGTH,
            "normalize": True,
        }
    )
    model_load = {
        "model": BASE_RERANKER,
        "load_ms": (time.perf_counter() - model_started) * 1000.0,
        "device": str(model.device),
        "torch_threads": torch.get_num_threads(),
        "max_rss_delta_kb": max(0, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before_rss),
    }
    tokenizer = model.tokenizer
    model_config = model.model.config
    base_1024 = {
        "model": BASE_RERANKER,
        "requested_max_length": 1024,
        "status": "UNSUPPORTED",
        "reason": (
            f"tokenizer.model_max_length={tokenizer.model_max_length}; "
            f"model.max_position_embeddings={getattr(model_config, 'max_position_embeddings', None)}. "
            "The architecture supports the configured 512-token pair, not a 1024-token sequence."
        ),
    }

    current_scores = load_current_quality_scores(s001_result_dir, multi_result_dir)
    current_rankings = current_quality_rankings(
        snapshots,
        candidate_rankings["C2_RRF60_20"],
        candidate_counts["C2_RRF60_20"],
        current_scores,
    )
    baseline_reproduction = validate_frozen_quality(snapshots, current_rankings)

    audit_csv_path = output_dir / "truncation_audit.csv"
    audit_summary_path = output_dir / "truncation_summary.json"
    audit_debug_path = output_dir / "truncation_debug_cases.jsonl"
    audit_cache_hit = all(path.exists() for path in (audit_csv_path, audit_summary_path, audit_debug_path))
    if audit_cache_hit:
        truncation_summary = load_json(audit_summary_path)
    else:
        audit_rows, truncation_summary, debug_rows = audit_truncation(
            tokenizer,
            snapshots,
            candidate_rankings,
            candidate_counts,
            components,
            current_rankings,
        )
        write_csv(audit_csv_path, audit_rows)
        dump_json(audit_summary_path, truncation_summary)
        write_jsonl(audit_debug_path, debug_rows)

    candidate_aggregate, candidate_by_session, complementarity_detail = candidate_pool_ablation(
        snapshots,
        components,
        candidate_rankings,
        candidate_counts,
    )
    write_csv(output_dir / "candidate_pool_ablation.csv", candidate_aggregate)
    write_csv(output_dir / "candidate_pool_by_session.csv", candidate_by_session)
    write_csv(output_dir / "candidate_complementarity_detail.csv", complementarity_detail)

    score_cache_path = output_dir / "cache/reranker_pair_scores.jsonl"
    score_cache_before = file_state(score_cache_path) if score_cache_path.exists() else None
    score_cache = PairScoreCache(score_cache_path)
    seed_status = score_cache.seed_current_scores(
        snapshots=snapshots,
        s001_result_dir=s001_result_dir,
        multi_result_dir=multi_result_dir,
        tokenizer=tokenizer,
    )
    input_aggregate: list[dict[str, Any]] = []
    input_by_session: list[dict[str, Any]] = []
    input_rankings: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    input_config_by_id = {str(config["id"]): config for config in UNIQUE_INPUT_CONFIGS}
    for config in UNIQUE_INPUT_CONFIGS:
        config_id = str(config["id"])
        if config_id == "R0_P8_CURRENT":
            final = current_rankings
            timing = {
                session_id: {
                    str(query["query_id"]): {
                        "packing_ms": 0.0,
                        "cached_estimated_inference_ms": 0.0,
                    }
                    for query in snapshot["queries"]
                }
                for session_id, snapshot in snapshots.items()
            }
        else:
            final, timing = score_configuration(
                snapshots=snapshots,
                candidate_rankings=candidate_rankings["C2_RRF60_20"],
                candidate_counts=candidate_counts["C2_RRF60_20"],
                input_config=config,
                tokenizer=tokenizer,
                model=model,
                cache=score_cache,
                max_length=MAX_LENGTH,
            )
        input_rankings[config_id] = final
        aggregate, per_session = evaluate_configuration(
            config_id=f"C2_RRF60_20__{config_id}",
            candidate_id="C2_RRF60_20",
            input_id=config_id,
            snapshots=snapshots,
            candidate_rankings=candidate_rankings["C2_RRF60_20"],
            candidate_counts=candidate_counts["C2_RRF60_20"],
            final_rankings=final,
            dense_rankings=dense_rankings,
            timing=timing,
        )
        input_aggregate.append(aggregate)
        input_by_session.extend(per_session)

    for alias, canonical in INPUT_ALIASES.items():
        source = next(row for row in input_aggregate if row["input_id"] == canonical)
        input_aggregate.append({**source, "input_id": alias, "canonical_input_id": canonical, "is_alias": True})
        input_by_session.extend(
            {**row, "input_id": alias, "canonical_input_id": canonical, "is_alias": True}
            for row in list(input_by_session)
            if row["input_id"] == canonical and not row.get("is_alias")
        )
    write_csv(output_dir / "reranker_input_ablation.csv", input_aggregate)
    write_csv(output_dir / "reranker_input_by_session.csv", input_by_session)

    unique_rows = [row for row in input_aggregate if not row.get("is_alias")]
    top2_inputs = sorted(unique_rows, key=selection_key, reverse=True)[:2]
    best_input_fix = max(
        (row for row in unique_rows if row["input_id"] != "R0_P8_CURRENT"),
        key=selection_key,
    )
    user_input_ids = {"R2_PACK_A", "R3_USER_SUMMARY", "R4_PACK_B", "PACK_C_BALANCED"}
    best_user_input = max(
        (row for row in unique_rows if row["input_id"] in user_input_ids),
        key=selection_key,
    )
    joint_aggregate: list[dict[str, Any]] = []
    joint_by_session: list[dict[str, Any]] = []
    joint_rankings: dict[tuple[str, str], dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for input_row in top2_inputs:
        input_id = str(input_row["input_id"])
        input_config = input_config_by_id[input_id]
        for candidate_id in ("C2_RRF60_20", "C3_FIXED_UNION20", "C4_UNION15X15", "C5_UNION20X20"):
            if candidate_id == "C2_RRF60_20":
                final = input_rankings[input_id]
                timing = {
                    session_id: {
                        str(query["query_id"]): {
                            "packing_ms": 0.0,
                            "cached_estimated_inference_ms": 0.0,
                        }
                        for query in snapshot["queries"]
                    }
                    for session_id, snapshot in snapshots.items()
                }
            else:
                final, timing = score_configuration(
                    snapshots=snapshots,
                    candidate_rankings=candidate_rankings[candidate_id],
                    candidate_counts=candidate_counts[candidate_id],
                    input_config=input_config,
                    tokenizer=tokenizer,
                    model=model,
                    cache=score_cache,
                    max_length=MAX_LENGTH,
                )
            joint_rankings[(candidate_id, input_id)] = final
            aggregate, per_session = evaluate_configuration(
                config_id=f"{candidate_id}__{input_id}",
                candidate_id=candidate_id,
                input_id=input_id,
                snapshots=snapshots,
                candidate_rankings=candidate_rankings[candidate_id],
                candidate_counts=candidate_counts[candidate_id],
                final_rankings=final,
                dense_rankings=dense_rankings,
                timing=timing,
            )
            joint_aggregate.append(aggregate)
            joint_by_session.extend(per_session)
    write_csv(output_dir / "candidate_reranker_joint_ablation.csv", joint_aggregate)
    write_csv(output_dir / "candidate_reranker_joint_by_session.csv", joint_by_session)

    d0 = next(row for row in unique_rows if row["input_id"] == "R0_P8_CURRENT")
    d1 = best_input_fix
    d2 = max(joint_aggregate, key=selection_key)
    d0_rankings = input_rankings["R0_P8_CURRENT"]
    d1_rankings = input_rankings[str(d1["input_id"])]
    d2_rankings = joint_rankings[(str(d2["candidate_id"]), str(d2["input_id"]))]
    movement_rows = winner_movement_rows(
        snapshots,
        dense_rankings,
        {"D0": d0_rankings, "D1": d1_rankings, "D2": d2_rankings},
    )
    write_csv(output_dir / "reranker_rank_movement.csv", movement_rows)

    max_position_embeddings = int(getattr(model_config, "max_position_embeddings", 0) or 0)
    model_rows = [
        {
            **model_load,
            "max_length": MAX_LENGTH,
            "tokenizer_model_max_length": tokenizer.model_max_length,
            "max_position_embeddings": max_position_embeddings,
            "status": "AVAILABLE",
        },
        base_1024,
    ]
    improvement_gold = round((float(d2["recall_at_5"]) - float(d0["recall_at_5"])) * int(d0["eligible_gold_count"]))
    base_stable = bool(
        improvement_gold >= 2
        and float(d2["macro_session_recall_at_5"]) >= float(d0["macro_session_recall_at_5"])
        and float(d2["ndcg_at_5"]) >= float(d0["ndcg_at_5"])
        and int(d2["demoted"]) < int(d0["demoted"])
    )
    new_model_cache = Path.home() / ".cache/huggingface/hub" / f"models--{NEW_RERANKER.replace('/', '--')}"
    if base_stable:
        new_reranker = {
            "triggered": False,
            "executed": False,
            "status": "NOT_TRIGGERED",
            "reason": "Best base packing/candidate met the predeclared stability criteria; no model expansion was needed.",
        }
    elif not new_model_cache.exists():
        new_reranker = {
            "triggered": True,
            "executed": False,
            "status": "UNAVAILABLE_NOT_CACHED",
            "reason": (
                f"{NEW_RERANKER} is not present in the existing HuggingFace cache. "
                "Network/model download was intentionally not introduced into this isolated diagnosis."
            ),
        }
    else:
        new_reranker = {
            "triggered": True,
            "executed": False,
            "status": "UNAVAILABLE_RUNTIME_PATH_NOT_IMPLEMENTED",
            "reason": "The model cache appeared unexpectedly; no unreviewed model path was executed.",
        }
    model_rows.append({"model": NEW_RERANKER, **new_reranker})
    write_csv(output_dir / "model_ablation.csv", model_rows)

    candidate_config_by_id = {str(config["id"]): config for config in CANDIDATE_CONFIGS}
    selected_schemes = [
        {"id": "D0_CURRENT_FROZEN_QUALITY", "candidate_id": "C2_RRF60_20", "input_id": "R0_P8_CURRENT"},
        {"id": "D1_BEST_INPUT_FIX", "candidate_id": "C2_RRF60_20", "input_id": d1["input_id"]},
        {"id": "D2_BEST_CANDIDATE_INPUT", "candidate_id": d2["candidate_id"], "input_id": d2["input_id"]},
    ]
    latency_rows, latency_summary, latency_status = benchmark_selected_e2e(
        output_dir=output_dir,
        snapshots=snapshots,
        schemes=selected_schemes,
        input_configs=input_config_by_id,
        candidate_configs=candidate_config_by_id,
        model=model,
        warmups=args.latency_warmups,
        repeats=args.latency_repeats,
        snapshot_hash=snapshot_hash,
    )
    write_csv(output_dir / "latency_measurements.csv", latency_rows)
    write_csv(output_dir / "latency_summary.csv", latency_summary)

    s005_id = next(session_id for session_id in snapshots if session_code(session_id) == "S005")
    def s005_row(rows: Sequence[Mapping[str, Any]], candidate_id: str, input_id: str) -> Mapping[str, Any]:
        return next(
            row
            for row in rows
            if row["session_code"] == "S005"
            and row["candidate_id"] == candidate_id
            and row["input_id"] == input_id
        )
    s005_d0 = s005_row(input_by_session, "C2_RRF60_20", "R0_P8_CURRENT")
    s005_d1 = s005_row(input_by_session, "C2_RRF60_20", str(d1["input_id"]))
    s005_d2 = s005_row(joint_by_session, str(d2["candidate_id"]), str(d2["input_id"]))
    current_demoted_ids = {
        row["gold_page_id"]
        for row in movement_rows
        if row["session_id"] == s005_id and row["D0_movement"] == "DEMOTED_OUT_OF_TOP5"
    }
    d2_demoted_ids = {
        row["gold_page_id"]
        for row in movement_rows
        if row["session_id"] == s005_id and row["D2_movement"] == "DEMOTED_OUT_OF_TOP5"
    }
    s005_comparison = {
        "D0": dict(s005_d0),
        "D1": dict(s005_d1),
        "D2": dict(s005_d2),
        "current_demoted_gold_ids": sorted(current_demoted_ids),
        "d2_demoted_gold_ids": sorted(d2_demoted_ids),
        "recovered_current_demotions": len(current_demoted_ids - d2_demoted_ids),
        "new_demotions": len(d2_demoted_ids - current_demoted_ids),
    }

    audit_groups = truncation_summary["groups"]
    demoted_audit = audit_groups["DEMOTED_OUT_OF_TOP5"]
    non_demoted_audit = audit_groups["NON_DEMOTED_GOLD"]
    input_gain_gold = round(
        (float(d1["recall_at_5"]) - float(d0["recall_at_5"])) * int(d0["eligible_gold_count"])
    )
    candidate_gain_gold = round(
        (float(d2["recall_at_5"]) - float(d0["recall_at_5"])) * int(d0["eligible_gold_count"])
    )
    truncation_signal = bool(
        input_gain_gold >= 2
        and float(demoted_audit["truncation_rate"] or 0.0)
        > float(non_demoted_audit["truncation_rate"] or 0.0) + 0.10
    )
    candidate_signal = candidate_gain_gold >= 2
    if truncation_signal and candidate_signal:
        diagnosis_status = "MULTI_FACTOR"
    elif truncation_signal:
        diagnosis_status = "INPUT_TRUNCATION_WAS_MAJOR_CAUSE"
    elif candidate_signal:
        diagnosis_status = "CANDIDATE_COMPRESSION_WAS_MAJOR_CAUSE"
    else:
        diagnosis_status = "GENERIC_RERANKER_REMAINS_MAIN_BOTTLENECK"
    truncation_causal_assessment = (
        f"DEMOTED Gold truncation rate={demoted_audit['truncation_rate'] or 0:.2%}，non-DEMOTED Gold="
        f"{non_demoted_audit['truncation_rate'] or 0:.2%}；D1 input fix 相对 D0 净增 {input_gain_gold} Gold。"
        + (
            "两项证据共同支持 truncation/packing 是主要原因。"
            if truncation_signal
            else "差异和实际增益不足以把 512-token truncation 判定为主要原因。"
        )
    )
    rrf_dropped = sum(
        row["preservation_category"] == "IN_DENSE_OR_BM25_BUT_DROPPED_BY_RRF20"
        for row in complementarity_detail
    )
    material_candidate = bool(
        improvement_gold >= 2
        and float(d2["macro_session_recall_at_5"]) >= float(d0["macro_session_recall_at_5"])
    )
    if diagnosis_status == "GENERIC_RERANKER_REMAINS_MAIN_BOTTLENECK":
        recommendation = (
            "Input packing 和 candidate expansion 均未形成稳定的多 Session 净提升；停止继续手调 S001-S005。"
            "本轮没有形成值得冻结的新候选，因此暂不消耗 S006+；若继续研发，先在 development 数据上评估"
            "一个可用的新 reranker 或构造训练数据，形成冻结候选后再进入 S006+ held-out。本轮不训练。"
        )
    else:
        recommendation = (
            "形成了新的 S001-S005 tuned candidate；参数已冻结到 frozen_next_validation_config.json。"
            "下一步只能直接到 S006+ 做 held-out，不得根据 S006+ 再调参后继续称 held-out。"
        )

    best_candidate_rankings = candidate_rankings[str(d2["candidate_id"])]
    best_candidate_counts = candidate_counts[str(d2["candidate_id"])]
    finetuning_stats = future_finetuning_stats(snapshots, best_candidate_rankings, best_candidate_counts)
    dump_json(output_dir / "future_finetuning_dataset_stats.json", finetuning_stats)
    if material_candidate:
        frozen_next = {
            "selection_status": INPUT_STATUS,
            "next_validation_scope": "S006+ unseen held-out Sessions",
            "query_representation": "Q0 current user query",
            "candidate_representation": "P0 production embedding_text",
            "dense_model": PRODUCTION_EMBEDDING,
            "bm25": "repository Chinese BM25 tokenizer/implementation",
            "candidate_strategy": d2["candidate_id"],
            "candidate_budget": candidate_config_by_id[str(d2["candidate_id"])],
            "reranker_representation": d2["input_id"],
            "packing_strategy": input_config_by_id[str(d2["input_id"])],
            "reranker_model": BASE_RERANKER,
            "max_length": MAX_LENGTH,
            "output_k": OUTPUT_K,
            "parameter_tuning_on_s006_plus_allowed": False,
        }
        dump_json(output_dir / "frozen_next_validation_config.json", frozen_next)
        frozen_next_status = "GENERATED"
    else:
        frozen_next_status = "NOT_GENERATED_NO_MATERIAL_STABLE_CANDIDATE"

    winners = {
        "D0_CURRENT_FROZEN_QUALITY": d0,
        "D1_BEST_INPUT_FIX": d1,
        "D2_BEST_CANDIDATE_INPUT": d2,
    }
    direct_answers = [
        f"当前实际 configured max_length={MAX_LENGTH}；tokenizer model_max_length={tokenizer.model_max_length}。",
        f"truncation=True 对 pair 解析为 longest_first，truncation_side={tokenizer.truncation_side}、padding_side={tokenizer.padding_side}。",
        f"P8 candidate >512：{audit_groups['ALL_CANDIDATES']['pair_over_512_count']}/{audit_groups['ALL_CANDIDATES']['count']}（{audit_groups['ALL_CANDIDATES']['pair_over_512_rate'] or 0:.2%}）。",
        f"Gold candidate >512：{audit_groups['ALL_GOLD_CANDIDATES']['pair_over_512_count']}/{audit_groups['ALL_GOLD_CANDIDATES']['count']}（{audit_groups['ALL_GOLD_CANDIDATES']['pair_over_512_rate'] or 0:.2%}）。",
        f"DEMOTED Gold truncation={demoted_audit['truncation_rate'] or 0:.2%}，non-DEMOTED Gold={non_demoted_audit['truncation_rate'] or 0:.2%}；{truncation_causal_assessment}",
        f"所有 P8 candidate keywords：fully={audit_groups['ALL_CANDIDATES']['keywords_fully_visible_rate'] or 0:.2%}、partial={audit_groups['ALL_CANDIDATES']['keywords_partially_visible_rate'] or 0:.2%}、not visible={audit_groups['ALL_CANDIDATES']['keywords_not_visible_rate'] or 0:.2%}。",
        f"Keywords 前置 R1 R@5={next(row for row in unique_rows if row['input_id']=='R1_KEYWORDS_FIRST')['recall_at_5']:.2%}，当前 R0={d0['recall_at_5']:.2%}。",
        f"包含 User Input 的最佳方案 {best_user_input['input_id']} R@5={best_user_input['recall_at_5']:.2%}。",
        f"Token-budgeted packing 最佳为 {best_user_input['input_id']}；相对 Frozen Quality：{metric_delta_text(d0, best_user_input)}。",
        f"512→1024：未运行，状态 {base_1024['status']}；模型架构 max_position_embeddings={max_position_embeddings}。",
        f"S005 demotion：7 → {s005_d2['demoted']}；恢复 {s005_comparison['recovered_current_demotions']}，新增 {s005_comparison['new_demotions']}。",
        f"Dense20∪BM2520 theoretical/actual exact coverage={next(row for row in candidate_aggregate if row['candidate_id']=='C5_UNION20X20')['candidate_recall']:.2%}。",
        f"RRF20 丢失 Dense20/BM2520 已找到 Gold={rrf_dropped}。",
        f"Fixed Union20 candidate recall={next(row for row in candidate_aggregate if row['candidate_id']=='C3_FIXED_UNION20')['candidate_recall']:.2%}；RRF20={next(row for row in candidate_aggregate if row['candidate_id']=='C2_RRF60_20')['candidate_recall']:.2%}。",
        f"Expanded Union 最佳 joint 为 {d2['candidate_id']}，mean candidate count={d2['mean_candidate_count']:.2f}；是否值得由 final R@5/latency 共同判断。",
        f"Candidate Recall={d2['candidate_recall']:.2%}，Final R@5={d2['recall_at_5']:.2%}；提高覆盖并未被假定等于提高 Top5。",
        f"当前主要瓶颈：{diagnosis_status}。",
        f"generic bge-reranker-base：{'仍值得作为下一阶段冻结候选' if material_candidate else '当前没有稳定证据支持继续作为默认 reranker'}。",
        f"bge-reranker-v2-m3：executed={new_reranker['executed']}，status={new_reranker['status']}。",
        f"新模型对比：{new_reranker['reason']}",
        "仍无证据要求修改 Page Prompt；所有实验只重组既有字段。",
        "仍无必要 Query Rewrite；Query 始终固定 Q0。",
        f"Fine-tuning：{'只建议作为未来方向评估' if diagnosis_status == 'GENERIC_RERANKER_REMAINS_MAIN_BOTTLENECK' else '当前不优先'}；本轮仅统计数据，未训练。",
        f"本轮最佳 S001-S005 tuned 方案：{d2['candidate_id']} + {d2['input_id']} + {BASE_RERANKER}@512 → Top5。",
        f"相对 Frozen Quality：{metric_delta_text(d0, d2)}。",
        f"是否冻结进入 S006+：{frozen_next_status}。本任务未运行 S006+。",
    ]
    summary = {
        "experiment": "S001-S005 reranker input truncation and candidate pool diagnosis",
        "selection_status": INPUT_STATUS,
        "sessions": list(snapshots),
        "snapshot_hash": snapshot_hash,
        "baseline_reproduction": baseline_reproduction,
        "truncation_summary": truncation_summary,
        "truncation_causal_assessment": truncation_causal_assessment,
        "candidate_pool_ablation": candidate_aggregate,
        "candidate_pool_by_session": candidate_by_session,
        "rrf_dropped_union_gold_count": rrf_dropped,
        "input_ablation": input_aggregate,
        "input_by_session": input_by_session,
        "top2_input_ids": [row["input_id"] for row in top2_inputs],
        "joint_ablation": joint_aggregate,
        "joint_by_session": joint_by_session,
        "winners": winners,
        "base_1024": base_1024,
        "new_reranker": new_reranker,
        "model_load": model_load,
        "s005_comparison": s005_comparison,
        "latency_summary": latency_summary,
        "latency_status": latency_status,
        "future_finetuning_dataset_stats": finetuning_stats,
        "diagnosis_status": diagnosis_status,
        "recommendation": recommendation,
        "frozen_next_validation_config_status": frozen_next_status,
        "direct_answers": direct_answers,
    }
    dump_json(output_dir / "diagnosis_summary.json", summary)
    (output_dir / "diagnosis_report.md").write_text(render_report(summary), encoding="utf-8")

    upstream_after = [file_state(path) for path in upstream_files]
    score_cache_after = file_state(score_cache_path)
    score_cache_unchanged = score_cache_before == score_cache_after if score_cache_before else None
    cache_validation = {
        "status": "PASS" if upstream_before == upstream_after and score_cache_unchanged is not False else "FAIL",
        "immutable_upstream_before": upstream_before,
        "immutable_upstream_after": upstream_after,
        "immutable_upstream_unchanged": upstream_before == upstream_after,
        "score_cache_before": score_cache_before,
        "score_cache_after": score_cache_after,
        "score_cache_unchanged_on_this_run": score_cache_unchanged,
        "truncation_audit_cache_reused": audit_cache_hit,
        "pair_cache_hits": score_cache.hits,
        "pair_cache_misses": score_cache.misses,
        "seed_status": seed_status,
        "latency_cache_reused": latency_status["measurement_cache_reused"],
        "full_session_runs": 0,
        "memory_regeneration_calls": 0,
        "deepseek_calls": 0,
    }
    dump_json(output_dir / "cache_reuse_validation.json", cache_validation)
    if cache_validation["status"] != "PASS":
        raise RuntimeError(f"Cache integrity failure: {cache_validation}")
    model.model.to("cpu")
    del model
    gc.collect()

    print(
        json.dumps(
            {
                "baseline_reproduction": baseline_reproduction,
                "truncation": {
                    "all_candidate_over_512_rate": audit_groups["ALL_CANDIDATES"]["pair_over_512_rate"],
                    "gold_over_512_rate": audit_groups["ALL_GOLD_CANDIDATES"]["pair_over_512_rate"],
                    "demoted_truncation_rate": demoted_audit["truncation_rate"],
                    "keywords_not_visible_rate": audit_groups["ALL_CANDIDATES"]["keywords_not_visible_rate"],
                },
                "rrf_dropped_union_gold_count": rrf_dropped,
                "winners": winners,
                "s005": s005_comparison,
                "diagnosis_status": diagnosis_status,
                "new_reranker": new_reranker,
                "frozen_next_validation_config": frozen_next_status,
                "cache": cache_validation,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
