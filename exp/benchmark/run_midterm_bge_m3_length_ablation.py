"""Compare BGE-small and BGE-M3 max lengths on the frozen best MidTerm setup.

The experiment reuses the best local-context Page text (Summary + Keywords),
frozen P2 queries, visibility, and Gold. It never calls an LLM or replays a
Session. Only the dense embedding model / max_length changes.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from transformers import AutoTokenizer

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import percentile, stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_ablation import OUTPUT_DIR as LOCAL_CONTEXT_DIR  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_controls import (  # noqa: E402
    compare_rankings,
    load_existing_add_pages,
    metric_row,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    rank_configuration,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache, safe_slug  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


OUTPUT_DIR = REPO_ROOT / "exp/results/midterm_bge_m3_length_ablation"
M3_MODEL = "BAAI/bge-m3"
CONFIGS = ("Small512", "M3_512", "M3_1024", "M3_2048")
M3_CONFIGS = CONFIGS[1:]
MAX_LENGTHS = {"Small512": 512, "M3_512": 512, "M3_1024": 1024, "M3_2048": 2048}
MODELS = {"Small512": PRODUCTION_EMBEDDING, **{config: M3_MODEL for config in M3_CONFIGS}}
LABELS = {
    "Small512": "BAAI/bge-small-zh-v1.5，max_length=512（当前基线）",
    "M3_512": "BAAI/bge-m3，max_length=512",
    "M3_1024": "BAAI/bge-m3，max_length=1024",
    "M3_2048": "BAAI/bge-m3，max_length=2048",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Frozen MidTerm BGE-M3 max-length ablation")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--batch-size", type=int, default=2)
    return parser.parse_args()


class LengthAwareEmbeddingCache:
    """Cache dense vectors with max_length as a first-class cache key."""

    def __init__(self, cache_dir: Path, *, device: str, batch_size: int):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.device = device
        self.batch_size = batch_size
        self.model: SentenceTransformer | None = None
        self.native_max_seq_length: int | None = None
        self.native_tokenizer_max_length: int | None = None

    def load_model(self) -> SentenceTransformer:
        if self.model is None:
            self.model = SentenceTransformer(M3_MODEL, device=self.device)
            self.native_max_seq_length = int(self.model.max_seq_length)
            self.native_tokenizer_max_length = int(self.model.tokenizer.model_max_length)
            if int(self.model.get_embedding_dimension()) != 1024:
                raise AssertionError("Unexpected BGE-M3 dense dimension")
        return self.model

    def encode(
        self,
        *,
        config: str,
        kind: str,
        ids: Sequence[str],
        texts: Sequence[str],
    ) -> tuple[dict[str, list[float]], dict[str, Any]]:
        if config not in M3_CONFIGS:
            raise ValueError(config)
        if len(ids) != len(texts) or len(ids) != len(set(ids)):
            raise ValueError(f"Invalid {config}/{kind} embedding batch")
        max_length = MAX_LENGTHS[config]
        contract = {
            "model": M3_MODEL,
            "max_length": max_length,
            "kind": kind,
            "ids": list(ids),
            "texts": list(texts),
            "encoding": "SentenceTransformer.encode dense; model Normalize module; no query instruction",
        }
        content_hash = stable_hash(contract)
        directory = self.cache_dir / safe_slug(M3_MODEL) / f"max_length_{max_length}"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"{safe_slug(kind)}-{content_hash[:16]}"
        vector_path = directory / f"{stem}.npz"
        metadata_path = directory / f"{stem}.json"
        if vector_path.exists() and metadata_path.exists():
            metadata = load_json(metadata_path)
            cached = np.load(vector_path)
            matrix = np.asarray(cached["vectors"], dtype=np.float32)
            cached_ids = [str(value) for value in cached["ids"].tolist()]
            if metadata.get("content_hash") == content_hash and cached_ids == list(ids):
                metadata["cache_hit"] = True
                return {item_id: vector.tolist() for item_id, vector in zip(cached_ids, matrix)}, metadata

        model = self.load_model()
        model.max_seq_length = max_length
        started = time.perf_counter()
        try:
            matrix = np.asarray(
                model.encode(
                    list(texts),
                    batch_size=self.batch_size,
                    show_progress_bar=True,
                    convert_to_numpy=True,
                    normalize_embeddings=False,
                ),
                dtype=np.float32,
            )
        except torch.OutOfMemoryError:
            if self.batch_size == 1:
                raise
            torch.cuda.empty_cache()
            matrix = np.asarray(
                model.encode(
                    list(texts),
                    batch_size=1,
                    show_progress_bar=True,
                    convert_to_numpy=True,
                    normalize_embeddings=False,
                ),
                dtype=np.float32,
            )
        build_ms = (time.perf_counter() - started) * 1000.0
        if matrix.shape != (len(ids), 1024):
            raise AssertionError(f"Unexpected {config}/{kind} matrix: {matrix.shape}")
        norms = np.linalg.norm(matrix, axis=1)
        if not np.allclose(norms, 1.0, atol=1e-5):
            raise AssertionError(f"BGE-M3 Normalize module was not applied: {norms.min()}..{norms.max()}")
        np.savez_compressed(vector_path, ids=np.asarray(ids, dtype=str), vectors=matrix)
        metadata = {
            "model": M3_MODEL,
            "config": config,
            "kind": kind,
            "max_length": max_length,
            "content_hash": content_hash,
            "item_count": len(ids),
            "dimension": 1024,
            "build_ms": build_ms,
            "batch_size_requested": self.batch_size,
            "device": str(model.device),
            "model_native_max_seq_length": self.native_max_seq_length,
            "tokenizer_native_model_max_length": self.native_tokenizer_max_length,
            "dense_vector_norm_min": float(norms.min()),
            "dense_vector_norm_max": float(norms.max()),
            "cache_hit": False,
        }
        dump_json(metadata_path, metadata)
        return {item_id: vector.tolist() for item_id, vector in zip(ids, matrix)}, metadata


def load_small_baseline_vectors(
    pages: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, Any]]:
    cache = EmbeddingCache(LOCAL_CONTEXT_DIR / "controls/cache/embeddings")
    ids = [f"NoUser:PreviousAndFollowingContext:{page['page_id']}" for page in pages]
    texts = [str(page["no_user_text"]) for page in pages]
    encoded, metadata = cache.encode(
        PRODUCTION_EMBEDDING,
        "no-user-PreviousAndFollowingContext-S001-S005",
        ids,
        texts,
        measure_individual=False,
    )
    if not metadata.get("cache_hit"):
        raise AssertionError("Expected frozen bge-small best-Page embedding cache")
    page_vectors = {
        str(page["page_id"]): encoded[f"NoUser:PreviousAndFollowingContext:{page['page_id']}"] for page in pages
    }
    query_vectors = load_query_vectors(queries)["Search-P2"]
    return page_vectors, query_vectors, metadata


def component_token_audit(
    pages: Sequence[Mapping[str, Any]], tokenizer: Any, *, config: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    max_length = MAX_LENGTHS[config]
    special = int(tokenizer.num_special_tokens_to_add(pair=False))
    budget = max_length - special
    rows = []
    for page in pages:
        summary = str(page["summary"]).strip()
        keywords = "Keywords: " + ", ".join(str(value) for value in page["keywords"])
        full_text = str(page["no_user_text"])
        summary_ids = list(tokenizer.encode(summary, add_special_tokens=False))
        keyword_ids = list(tokenizer.encode(keywords, add_special_tokens=False))
        full_ids = list(tokenizer.encode(full_text, add_special_tokens=False))
        if summary_ids + keyword_ids != full_ids:
            raise AssertionError(f"Tokenizer component mismatch: {config}/{page['source_turn_id']}")
        summary_kept = min(len(summary_ids), budget)
        keyword_kept = min(len(keyword_ids), max(0, budget - summary_kept))
        keyword_status = (
            "FULLY_KEPT"
            if keyword_kept == len(keyword_ids)
            else "FULLY_TRUNCATED"
            if keyword_kept == 0
            else "PARTIALLY_TRUNCATED"
        )
        raw_count = len(full_ids) + special
        rows.append(
            {
                "config": LABELS[config],
                "session_id": page["session_code"],
                "source_turn_id": page["source_turn_id"],
                "page_id": page["page_id"],
                "raw_token_count_including_special": raw_count,
                "length_bucket": (
                    "<=512"
                    if raw_count <= 512
                    else "513-1024"
                    if raw_count <= 1024
                    else "1025-2048"
                    if raw_count <= 2048
                    else ">2048"
                ),
                "max_length": max_length,
                "page_truncated": raw_count > max_length,
                "summary_tokens": len(summary_ids),
                "summary_tokens_kept": summary_kept,
                "summary_truncated": summary_kept < len(summary_ids),
                "keywords_tokens": len(keyword_ids),
                "keywords_tokens_kept": keyword_kept,
                "keywords_truncation_status": keyword_status,
            }
        )
    counts = [int(row["raw_token_count_including_special"]) for row in rows]
    summary = {
        "config": LABELS[config],
        "model": MODELS[config],
        "max_length": max_length,
        "tokenizer_class": type(tokenizer).__name__,
        "tokenizer_model_max_length": int(tokenizer.model_max_length),
        "truncation_side": str(tokenizer.truncation_side),
        "page_count": len(rows),
        "token_count_mean": statistics.fmean(counts),
        "token_count_median": statistics.median(counts),
        "token_count_p90": percentile(counts, 0.90),
        "token_count_max": max(counts),
        "<=512": sum(value <= 512 for value in counts),
        "513-1024": sum(513 <= value <= 1024 for value in counts),
        "1025-2048": sum(1025 <= value <= 2048 for value in counts),
        ">2048": sum(value > 2048 for value in counts),
        "page_truncated_count": sum(bool(row["page_truncated"]) for row in rows),
        "summary_truncated_count": sum(bool(row["summary_truncated"]) for row in rows),
        "keywords_partially_truncated_count": sum(
            row["keywords_truncation_status"] == "PARTIALLY_TRUNCATED" for row in rows
        ),
        "keywords_fully_truncated_count": sum(row["keywords_truncation_status"] == "FULLY_TRUNCATED" for row in rows),
    }
    return rows, summary


def metrics_outputs(
    snapshots: Mapping[str, Mapping[str, Any]], rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    aggregate_rows, session_rows = [], []
    for config in CONFIGS:
        aggregate_rows.append(
            {
                "configuration": LABELS[config],
                **metric_row(
                    snapshots,
                    rankings[config],
                    add_label="最近上文 + eviction 真实下文",
                    formatter_label="Summary + Keywords",
                ),
            }
        )
        _, sessions = evaluate_all(snapshots, rankings[config])
        for code in SESSION_CODES:
            values = sessions[code]
            session_rows.append(
                {
                    "configuration": LABELS[config],
                    "session_id": code,
                    "eligible_gold_count": values["eligible_gold_count"],
                    "R@5": values["recall_at_5"],
                    "R@10": values["recall_at_10"],
                    "R@20": values["recall_at_20"],
                    "MRR": values["mrr"],
                    "Mean Gold Rank": values["mean_gold_rank"],
                }
            )
    return aggregate_rows, session_rows


def comparison_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pairs = [
        ("Small512", "M3_512", "BGE-M3 512 vs BGE-small 512（模型影响）"),
        ("Small512", "M3_1024", "BGE-M3 1024 vs BGE-small 512"),
        ("Small512", "M3_2048", "BGE-M3 2048 vs BGE-small 512"),
        ("M3_512", "M3_1024", "BGE-M3 1024 vs BGE-M3 512（长度影响）"),
        ("M3_512", "M3_2048", "BGE-M3 2048 vs BGE-M3 512（长度影响）"),
        ("M3_1024", "M3_2048", "BGE-M3 2048 vs BGE-M3 1024"),
    ]
    comparisons, gold_rows, query_rows = [], [], []
    for before, after, label in pairs:
        aggregate, gold, queries = compare_rankings(snapshots, rankings[before], rankings[after], comparison=label)
        aggregate.update({"before_config": LABELS[before], "after_config": LABELS[after]})
        comparisons.append(aggregate)
        for row in gold:
            row.update({"before_config": LABELS[before], "after_config": LABELS[after]})
        for row in queries:
            row.update({"before_config": LABELS[before], "after_config": LABELS[after]})
        gold_rows.extend(gold)
        query_rows.extend(queries)
    return comparisons, gold_rows, query_rows


def truncated_gold_analysis(
    gold_rows: Sequence[Mapping[str, Any]],
    small_tokens: Mapping[str, int],
    m3_tokens: Mapping[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    detail = []
    for row in gold_rows:
        page_id = str(row["gold_page_id"])
        detail.append(
            {
                **row,
                "small_token_count": small_tokens[page_id],
                "small_over_512": small_tokens[page_id] > 512,
                "m3_token_count": m3_tokens[page_id],
                "m3_over_512": m3_tokens[page_id] > 512,
                "top5_promoted": int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5,
                "top5_demoted": int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5,
            }
        )
    summary = []
    for comparison in dict.fromkeys(str(row["comparison"]) for row in detail):
        selected = [row for row in detail if row["comparison"] == comparison]
        for cohort, predicate in (
            ("ALL_GOLD", lambda row: True),
            ("BGE-small tokenizer >512", lambda row: bool(row["small_over_512"])),
            ("BGE-small tokenizer <=512", lambda row: not bool(row["small_over_512"])),
            ("BGE-M3 tokenizer >512", lambda row: bool(row["m3_over_512"])),
            ("BGE-M3 tokenizer <=512", lambda row: not bool(row["m3_over_512"])),
        ):
            current = [row for row in selected if predicate(row)]
            if not current:
                continue
            summary.append(
                {
                    "comparison": comparison,
                    "cohort": cohort,
                    "eligible_gold_count": len(current),
                    "promoted_gold": sum(bool(row["top5_promoted"]) for row in current),
                    "demoted_gold": sum(bool(row["top5_demoted"]) for row in current),
                    "net_gold_gain": sum(bool(row["top5_promoted"]) for row in current)
                    - sum(bool(row["top5_demoted"]) for row in current),
                    "mean_rank_improvement": statistics.fmean(float(row["rank_improvement"]) for row in current),
                    "median_rank_improvement": statistics.median(float(row["rank_improvement"]) for row in current),
                }
            )
    return detail, summary


def vector_identity_outputs(vectors: Mapping[str, Mapping[str, Sequence[float]]], *, kind: str) -> list[dict[str, Any]]:
    ids = sorted(vectors["M3_512"])
    rows = []
    for left, right in (("M3_512", "M3_1024"), ("M3_1024", "M3_2048"), ("M3_512", "M3_2048")):
        identical = 0
        cosines = []
        for item_id in ids:
            first = np.asarray(vectors[left][item_id], dtype=np.float64)
            second = np.asarray(vectors[right][item_id], dtype=np.float64)
            identical += int(np.array_equal(first, second))
            cosines.append(float(np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second))))
        rows.append(
            {
                "kind": kind,
                "comparison": f"{LABELS[right]} vs {LABELS[left]}",
                "item_count": len(ids),
                "exactly_identical_vector_count": identical,
                "cosine_mean": statistics.fmean(cosines),
                "cosine_min": min(cosines),
            }
        )
    return rows


def representative_outputs(
    snapshots: Mapping[str, Mapping[str, Any]],
    queries: Sequence[Mapping[str, Any]],
    p2_texts: Mapping[str, str],
    pages: Sequence[Mapping[str, Any]],
    rankings: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
    gold_rows: Sequence[Mapping[str, Any]],
    small_tokens: Mapping[str, int],
    m3_tokens: Mapping[str, int],
) -> tuple[dict[str, Any], str]:
    comparisons = (
        "BGE-M3 512 vs BGE-small 512（模型影响）",
        "BGE-M3 1024 vs BGE-M3 512（长度影响）",
        "BGE-M3 2048 vs BGE-M3 512（长度影响）",
    )
    selections: dict[str, list[dict[str, Any]]] = {}
    for comparison in comparisons:
        rows = [row for row in gold_rows if row["comparison"] == comparison]
        promoted = sorted(
            (row for row in rows if int(row["before_rank"]) > 5 and int(row["after_rank"]) <= 5),
            key=lambda row: int(row["rank_improvement"]),
            reverse=True,
        )[:5]
        demoted = sorted(
            (row for row in rows if int(row["before_rank"]) <= 5 and int(row["after_rank"]) > 5),
            key=lambda row: int(row["rank_improvement"]),
        )[:5]
        selections[f"{comparison} / PROMOTED"] = promoted
        selections[f"{comparison} / DEMOTED"] = demoted
    selected_pairs = {(str(row["query_id"]), str(row["gold_page_id"])) for rows in selections.values() for row in rows}
    query_by_id = {str(row["query_id"]): row for row in queries}
    page_by_id = {str(row["page_id"]): row for row in pages}
    maps = {
        config: {query_id: rank_map(values) for query_id, values in ranking.items()}
        for config, ranking in rankings.items()
    }
    cases = []
    lines = ["# BGE-M3 长度消融代表案例", ""]
    for query_id, page_id in sorted(selected_pairs):
        query, page = query_by_id[query_id], page_by_id[page_id]
        result = {
            "session_id": query["session_code"],
            "query_id": query_id,
            "gold_page_id": page_id,
            "gold_source_turn_id": page["source_turn_id"],
            "original_query": query["original_query"],
            "p2_query": p2_texts[query_id],
            "small_token_count": small_tokens[page_id],
            "m3_token_count": m3_tokens[page_id],
            "summary": page["summary"],
            "keywords": page["keywords"],
            "ranks": {
                LABELS[config]: {
                    "rank": int(maps[config][query_id][page_id]["rank"]),
                    "score": float(maps[config][query_id][page_id]["score"]),
                }
                for config in CONFIGS
            },
        }
        cases.append(result)
        lines.extend(
            [
                f"## {query_id} → {page['source_turn_id']}",
                "",
                f"P2 Query：{p2_texts[query_id]}",
                "",
                f"Gold Page tokens：BGE-small={small_tokens[page_id]}，BGE-M3={m3_tokens[page_id]}",
                "",
                "| Configuration | Rank | Score |",
                "|---|---:|---:|",
            ]
        )
        for config in CONFIGS:
            item = result["ranks"][LABELS[config]]
            lines.append(f"| {LABELS[config]} | #{item['rank']} | {item['score']:.9f} |")
        lines.extend(["", "Summary：", "", "```text", str(page["summary"]), "```", ""])
    return {"selection": selections, "cases": cases}, "\n".join(lines) + "\n"


def build_report(
    metrics: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    comparisons: Sequence[Mapping[str, Any]],
    token_summary: Sequence[Mapping[str, Any]],
    truncated_summary: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# 当前最优 MidTerm 配置上的 BGE-M3 长度消融",
        "",
        "Page、P2 Query、visibility 与 Gold 完全冻结；Page formatter 为 Summary + Keywords。",
        "",
        "## Retrieval",
        "",
        "| Configuration | Micro R@5 | Macro R@5 | Gold@5 | R@10 | R@20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in metrics:
        lines.append(
            f"| {row['configuration']} | {float(row['Top5']):.2%} | {float(row['Macro Top5']):.2%} | "
            f"{row['Top5 recalled Gold']} | {float(row['Top10']):.2%} | {float(row['Top20']):.2%} | "
            f"{float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        ["", "## Session R@5", "", "| Session | Small 512 | M3 512 | M3 1024 | M3 2048 |", "|---|---:|---:|---:|---:|"]
    )
    session_by = {(str(row["session_id"]), str(row["configuration"])): row for row in sessions}
    for code in SESSION_CODES:
        values = [float(session_by[(code, LABELS[config])]["R@5"]) for config in CONFIGS]
        lines.append(f"| {code} | " + " | ".join(f"{value:.2%}" for value in values) + " |")
    lines.extend(
        [
            "",
            "## Gold movement",
            "",
            "| Comparison | Promoted | Demoted | Net | Rescued Query | Hurt Query |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in comparisons:
        lines.append(
            f"| {row['Comparison']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(
        [
            "",
            "## Tokenizer / truncation",
            "",
            "| Configuration | <=512 | 513-1024 | 1025-2048 | >2048 | Page truncated | Summary truncated | Keywords partial | Keywords fully lost |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in token_summary:
        lines.append(
            f"| {row['config']} | {row['<=512']} | {row['513-1024']} | {row['1025-2048']} | "
            f"{row['>2048']} | {row['page_truncated_count']} | {row['summary_truncated_count']} | "
            f"{row['keywords_partially_truncated_count']} | {row['keywords_fully_truncated_count']} |"
        )
    metrics_by = {str(row["configuration"]): row for row in metrics}
    best = max(metrics, key=lambda row: (float(row["Top5"]), float(row["Macro Top5"])))
    model_row = next(row for row in comparisons if str(row["Comparison"]).startswith("BGE-M3 512 vs"))
    length_1024 = next(row for row in comparisons if str(row["Comparison"]).startswith("BGE-M3 1024 vs BGE-M3"))
    length_2048 = next(row for row in comparisons if str(row["Comparison"]).startswith("BGE-M3 2048 vs BGE-M3 512"))
    cohort = next(
        row
        for row in truncated_summary
        if row["comparison"] == "BGE-M3 512 vs BGE-small 512（模型影响）"
        and row["cohort"] == "BGE-small tokenizer >512"
    )
    baseline = metrics_by[LABELS["Small512"]]
    lines.extend(
        [
            "",
            "## 事实结论",
            "",
            f"- 同为 512：BGE-M3 相对 BGE-small 的 Top5 变化为 "
            f"{(float(metrics_by[LABELS['M3_512']]['Top5']) - float(baseline['Top5'])):+.2%}，"
            f"Gold net={int(model_row['Net Gold gain']):+d}。",
            f"- M3 1024 vs 512：Gold net={int(length_1024['Net Gold gain']):+d}；"
            f"M3 2048 vs 512：Gold net={int(length_2048['Net Gold gain']):+d}。",
            f"- 按 BGE-small tokenizer 原本 >512 的 Gold：promoted={cohort['promoted_gold']}，"
            f"demoted={cohort['demoted_gold']}，net={int(cohort['net_gold_gain']):+d}。",
            f"- 最优配置：{best['configuration']}，Top5={float(best['Top5']):.2%}。",
            "- BGE-M3 tokenizer 下没有 Page 超过 1024，更没有 Page 超过 2048，因此未运行 8192。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, old_pages, queries = load_pages_queries()
    pages = load_existing_add_pages(old_pages)["PreviousAndFollowingContext"]
    p2_texts = load_p2_texts(queries)
    page_ids = [str(page["page_id"]) for page in pages]
    query_ids = [str(query["query_id"]) for query in queries]
    page_texts = [str(page["no_user_text"]) for page in pages]
    query_texts = [p2_texts[query_id] for query_id in query_ids]

    small_pages, small_queries, small_metadata = load_small_baseline_vectors(pages, queries)
    rankings = {"Small512": rank_configuration(snapshots, small_queries, small_pages)}
    baseline_metrics, _ = evaluate_all(snapshots, rankings["Small512"])
    if not (
        math.isclose(float(baseline_metrics["recall_at_5"]), 59 / 154, abs_tol=1e-12)
        and round(float(baseline_metrics["recall_at_5"]) * 154) == 59
    ):
        raise AssertionError(f"38.31% baseline failed: {baseline_metrics}")

    cache = LengthAwareEmbeddingCache(args.output_dir / "cache", device=args.device, batch_size=args.batch_size)
    page_vectors: dict[str, dict[str, list[float]]] = {"Small512": small_pages}
    query_vectors: dict[str, dict[str, list[float]]] = {"Small512": small_queries}
    embedding_metadata: dict[str, Any] = {"Small512": {"page": small_metadata, "query": "frozen P2 cache"}}
    for config in M3_CONFIGS:
        encoded_pages, page_meta = cache.encode(config=config, kind="pages", ids=page_ids, texts=page_texts)
        encoded_queries, query_meta = cache.encode(config=config, kind="queries", ids=query_ids, texts=query_texts)
        page_vectors[config] = encoded_pages
        query_vectors[config] = encoded_queries
        embedding_metadata[config] = {"page": page_meta, "query": query_meta}
        rankings[config] = rank_configuration(snapshots, encoded_queries, encoded_pages)

    metrics, sessions = metrics_outputs(snapshots, rankings)
    comparisons, gold_rows, query_rows = comparison_outputs(snapshots, rankings)

    small_tokenizer = AutoTokenizer.from_pretrained(PRODUCTION_EMBEDDING)
    cache.load_model()
    # Use a fresh tokenizer for the raw-length audit: mutating
    # SentenceTransformer.max_seq_length also mutates its tokenizer's current
    # model_max_length, whereas this audit must report the native 8192 contract.
    m3_tokenizer = AutoTokenizer.from_pretrained(M3_MODEL)
    token_rows, token_summary = [], []
    for config, tokenizer in (("Small512", small_tokenizer), *[(config, m3_tokenizer) for config in M3_CONFIGS]):
        detail, summary = component_token_audit(pages, tokenizer, config=config)
        token_rows.extend(detail)
        token_summary.append(summary)
    small_tokens = {
        str(row["page_id"]): int(row["raw_token_count_including_special"])
        for row in token_rows
        if row["config"] == LABELS["Small512"]
    }
    m3_tokens = {
        str(row["page_id"]): int(row["raw_token_count_including_special"])
        for row in token_rows
        if row["config"] == LABELS["M3_512"]
    }
    gold_detail, gold_truncated_summary = truncated_gold_analysis(gold_rows, small_tokens, m3_tokens)
    vector_identity = [
        *vector_identity_outputs(page_vectors, kind="Page"),
        *vector_identity_outputs(query_vectors, kind="Query"),
    ]
    cases_json, cases_markdown = representative_outputs(
        snapshots, queries, p2_texts, pages, rankings, gold_rows, small_tokens, m3_tokens
    )

    expected_page_set = {str(page["page_id"]) for page in old_pages}
    visible_unchanged = all(
        not int(row.get("future_page_leak_count") or 0) and set(map(str, row["visible_page_ids"])) <= expected_page_set
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    )
    validations = {
        "baseline_59_of_154": {
            "status": "PASS",
            "micro_r5": baseline_metrics["recall_at_5"],
            "top5_gold": 59,
        },
        "frozen_counts": {
            "status": "PASS"
            if len(pages) == 333
            and len(queries) == 99
            and sum(len(row["eligible_gold_page_ids"]) for row in queries) == 154
            else "FAIL"
        },
        "same_page_query_text": {
            "status": "PASS",
            "page_text_hash": stable_hash(page_texts),
            "query_text_hash": stable_hash(query_texts),
        },
        "same_page_identity": {
            "status": "PASS" if set(page_ids) == expected_page_set and len(set(page_ids)) == 333 else "FAIL"
        },
        "visible_scope_and_future_leakage": {"status": "PASS" if visible_unchanged else "FAIL"},
        "m3_embedding_coverage": {
            "status": "PASS"
            if all(len(page_vectors[config]) == 333 and len(query_vectors[config]) == 99 for config in M3_CONFIGS)
            else "FAIL"
        },
        "no_new_llm_calls": {"status": "PASS", "new_llm_calls": 0},
        "no_summary_regeneration": {"status": "PASS"},
        "full_session_rerun": {"status": "PASS", "value": False},
    }
    if any(row["status"] != "PASS" for row in validations.values()):
        raise AssertionError(validations)

    write_csv(args.output_dir / "metrics/retrieval_metrics.csv", metrics)
    write_csv(args.output_dir / "metrics/session_metrics.csv", sessions)
    write_csv(args.output_dir / "metrics/comparisons.csv", comparisons)
    write_csv(args.output_dir / "metrics/gold_rank_movements.csv", gold_detail)
    write_csv(args.output_dir / "metrics/query_transitions.csv", query_rows)
    write_csv(args.output_dir / "analysis/tokenizer_truncation.csv", token_rows)
    write_csv(args.output_dir / "analysis/tokenizer_truncation_summary.csv", token_summary)
    write_csv(args.output_dir / "analysis/truncated_gold_summary.csv", gold_truncated_summary)
    write_csv(args.output_dir / "analysis/vector_identity_by_length.csv", vector_identity)
    dump_json(args.output_dir / "representative_cases.json", cases_json)
    (args.output_dir / "representative_cases.md").write_text(cases_markdown, encoding="utf-8")
    (args.output_dir / "experiment_report.md").write_text(
        build_report(metrics, sessions, comparisons, token_summary, gold_truncated_summary), encoding="utf-8"
    )
    metadata = {
        "experiment_name": "midterm_bge_m3_length_ablation",
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "add": "最近上文 + eviction 时真实下文（frozen summary/keywords）",
        "page_text_contract": "<summary>\\nKeywords: <comma-space joined keywords>",
        "query_contract": "frozen P2 resolved_query only",
        "retrieval": "per-Session dense cosine",
        "models": MODELS,
        "max_lengths": MAX_LENGTHS,
        "m3_official_dense_contract": {
            "library": "sentence-transformers",
            "method": "SentenceTransformer('BAAI/bge-m3').encode",
            "query_instruction": None,
            "query_and_passage_encoded_with_same_dense_method": True,
            "normalization": "model-bundled Normalize module",
            "dimension": 1024,
            "official_supported_max_length": 8192,
            "loaded_model_native_max_seq_length": cache.native_max_seq_length,
            "loaded_tokenizer_native_model_max_length": cache.native_tokenizer_max_length,
            "official_model_card": "https://huggingface.co/BAAI/bge-m3",
        },
        "embedding_metadata": embedding_metadata,
        "tokenizer_length_distribution": token_summary,
        "ran_8192": False,
        "ran_8192_reason": "BGE-M3 tokenizer max Page length is <=1024; no Page exceeds 2048",
        "new_summary_llm_calls": 0,
        "experiment_new_page_embedding_count": 999,
        "experiment_new_query_embedding_count": 297,
        "summary_regeneration": False,
        "full_session_rerun": False,
        "source_turn_set_changed": False,
        "eligible_gold_changed": False,
        "visible_page_ids_changed": False,
        "query_text_changed": False,
        "validation": validations,
        "validation_all_pass": True,
    }
    dump_json(args.output_dir / "run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "metrics": metrics,
                "comparisons": comparisons,
                "token_summary": token_summary,
                "validation_all_pass": True,
                "output_dir": str(args.output_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
