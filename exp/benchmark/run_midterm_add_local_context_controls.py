"""Run embedding reproducibility and no-User controls for local-context Add Pages.

No LLM, Session, Add Prompt, Search Prompt, Gold, visibility, or Page identity is
changed. Search is the frozen P2 query embedding and retrieval is per-Session
dense cosine throughout.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from exp.benchmark.benchmark_common import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))

from exp.benchmark.midterm_retrieval_eval import load_jsonl, percentile, stable_hash  # noqa: E402
from exp.benchmark.run_midterm_add_local_context_ablation import (  # noqa: E402
    OUTPUT_DIR,
    VARIANT_LABELS,
    all_queries,
)
from exp.benchmark.run_midterm_add_search_cross_ablation import (  # noqa: E402
    PRODUCTION_EMBEDDING,
    SESSION_CODES,
    evaluate_all,
    load_p2_texts,
    load_pages_queries,
    load_query_vectors,
    page_text,
    rank_configuration,
    validate_old_reproduction,
)
from exp.benchmark.run_midterm_retrieval_experiments import EmbeddingCache  # noqa: E402
from exp.benchmark.run_query_rewrite_cross_session_diagnosis import (  # noqa: E402
    dump_json,
    rank_map,
    write_csv,
)


CONTROL_DIR = OUTPUT_DIR
ADD_VARIANTS = ("Production", "PreviousContext", "PreviousAndFollowingContext")
FORMATTERS = ("SummaryKeywordsUser", "SummaryKeywords")
FORMATTER_LABELS = {
    "SummaryKeywordsUser": "Summary + Keywords + User",
    "SummaryKeywords": "Summary + Keywords",
}
SEARCH_LABEL = "上下文引用解析后检索（P2）"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local-context Add embedding/formatter controls")
    parser.add_argument("--output-dir", type=Path, default=CONTROL_DIR)
    return parser.parse_args()


def summary_keywords_text(summary: str, keywords: Sequence[str]) -> str:
    """Use the production formatter with the User component intentionally empty."""
    return page_text(summary, keywords, "")


def load_existing_add_pages(old_pages: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    old_by_turn = {str(page["source_turn_id"]): page for page in old_pages}
    result: dict[str, list[dict[str, Any]]] = {variant: [] for variant in ADD_VARIANTS}
    for old in old_pages:
        full_text = page_text(str(old["summary"]), list(old["keywords"]), str(old["user_input"]))
        if full_text != str(old["current_embedding_text"]):
            raise AssertionError(f"Production Page formatter mismatch: {old['source_turn_id']}")
        result["Production"].append(
            {
                "session_code": old["session_code"],
                "source_turn_id": old["source_turn_id"],
                "source_turn_index": old["source_turn_index"],
                "page_id": old["page_id"],
                "summary": old["summary"],
                "keywords": list(old["keywords"]),
                "user_input": old["user_input"],
                "full_text": full_text,
                "no_user_text": summary_keywords_text(str(old["summary"]), list(old["keywords"])),
            }
        )
    paths = {
        "PreviousContext": OUTPUT_DIR / "pages/PreviousContext_pages.jsonl",
        "PreviousAndFollowingContext": OUTPUT_DIR / "pages/PreviousAndFollowingContext_pages.jsonl",
    }
    for variant, path in paths.items():
        rows = load_jsonl(path)
        if len(rows) != 333:
            raise AssertionError(f"{variant}: expected 333 frozen Pages")
        for row in rows:
            turn = str(row["source_turn_id"])
            old = old_by_turn[turn]
            if (
                str(row["old_page_id"]) != str(old["page_id"])
                or str(row["source_user"]) != str(old["user_input"])
                or int(row["source_turn_index"]) != int(old["source_turn_index"])
            ):
                raise AssertionError(f"{variant}: Page identity/User mismatch at {turn}")
            full_text = page_text(str(row["summary"]), list(row["keywords"]), str(row["source_user"]))
            if full_text != str(row["embedding_text"]):
                raise AssertionError(f"{variant}: existing formatter mismatch at {turn}")
            result[variant].append(
                {
                    "session_code": row["session_id"],
                    "source_turn_id": turn,
                    "source_turn_index": row["source_turn_index"],
                    "page_id": row["old_page_id"],
                    "summary": row["summary"],
                    "keywords": list(row["keywords"]),
                    "user_input": row["source_user"],
                    "full_text": full_text,
                    "no_user_text": summary_keywords_text(str(row["summary"]), list(row["keywords"])),
                }
            )
    expected_ids = {str(page["page_id"]) for page in old_pages}
    for variant in ADD_VARIANTS:
        result[variant].sort(key=lambda row: (str(row["session_code"]), int(row["source_turn_index"])))
        if {str(row["page_id"]) for row in result[variant]} != expected_ids:
            raise AssertionError(f"{variant}: logical Page set changed")
        if any(
            str(row["full_text"]) != f"{row['no_user_text']}\nUser: {str(row['user_input']).strip()}"
            for row in result[variant]
        ):
            raise AssertionError(f"{variant}: no-User formatter changes more than the User line")
    return result


def load_existing_context_vectors(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, dict[str, list[float]]], dict[str, Any]]:
    cache = EmbeddingCache(OUTPUT_DIR / "cache/embeddings")
    vectors: dict[str, dict[str, list[float]]] = {}
    metadata = {}
    for variant in ADD_VARIANTS[1:]:
        selected = pages[variant]
        ids = [f"{variant}:{row['source_turn_id']}" for row in selected]
        texts = [str(row["full_text"]) for row in selected]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"{variant}-runtime-local-context-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        if not item_meta.get("cache_hit"):
            raise AssertionError(f"{variant}: expected existing full formatter vector cache")
        vectors[variant] = {
            str(row["page_id"]): encoded[f"{variant}:{row['source_turn_id']}"] for row in selected
        }
        metadata[variant] = item_meta
    return vectors, metadata


def encode_controls(
    output_dir: Path,
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, list[float]], dict[str, dict[str, list[float]]], dict[str, Any], EmbeddingCache]:
    cache = EmbeddingCache(output_dir / "controls/cache/embeddings")
    production = pages["Production"]
    fresh_ids = [f"ProductionFreshFull:{row['page_id']}" for row in production]
    fresh_texts = [str(row["full_text"]) for row in production]
    encoded, fresh_meta = cache.encode(
        PRODUCTION_EMBEDDING,
        "production-fresh-full-text-S001-S005",
        fresh_ids,
        fresh_texts,
        measure_individual=False,
    )
    fresh = {
        str(row["page_id"]): encoded[f"ProductionFreshFull:{row['page_id']}"] for row in production
    }
    no_user: dict[str, dict[str, list[float]]] = {}
    no_user_meta = {}
    for variant in ADD_VARIANTS:
        selected = pages[variant]
        ids = [f"NoUser:{variant}:{row['page_id']}" for row in selected]
        texts = [str(row["no_user_text"]) for row in selected]
        encoded, item_meta = cache.encode(
            PRODUCTION_EMBEDDING,
            f"no-user-{variant}-S001-S005",
            ids,
            texts,
            measure_individual=False,
        )
        no_user[variant] = {
            str(row["page_id"]): encoded[f"NoUser:{variant}:{row['page_id']}"] for row in selected
        }
        no_user_meta[variant] = item_meta
    metadata = {"FreshProductionFull": fresh_meta, "NoUser": no_user_meta}
    if any(int(value["dimension"]) != 512 or int(value["item_count"]) != 333 for value in no_user_meta.values()):
        raise AssertionError("No-User embedding coverage/dimension mismatch")
    if int(fresh_meta["dimension"]) != 512 or int(fresh_meta["item_count"]) != 333:
        raise AssertionError("Fresh Production embedding coverage/dimension mismatch")
    return fresh, no_user, metadata, cache


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    first = np.asarray(left, dtype=np.float64)
    second = np.asarray(right, dtype=np.float64)
    denominator = float(np.linalg.norm(first) * np.linalg.norm(second))
    return float(np.dot(first, second) / denominator) if denominator else 0.0


def vector_reproducibility(
    pages: Sequence[Mapping[str, Any]],
    stored: Mapping[str, Sequence[float]],
    fresh: Mapping[str, Sequence[float]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, similarities = [], []
    for page in pages:
        page_id = str(page["page_id"])
        old = np.asarray(stored[page_id], dtype=np.float64)
        new = np.asarray(fresh[page_id], dtype=np.float64)
        value = cosine(old, new)
        similarities.append(value)
        rows.append(
            {
                "session_id": page["session_code"],
                "source_turn_id": page["source_turn_id"],
                "page_id": page_id,
                "stored_fresh_cosine": value,
                "l2_distance": float(np.linalg.norm(old - new)),
                "max_abs_difference": float(np.max(np.abs(old - new))),
            }
        )
    summary = {
        "page_count": len(similarities),
        "cosine_mean": statistics.fmean(similarities),
        "cosine_min": min(similarities),
        "cosine_p5": percentile(similarities, 0.05),
        "cosine_p50": percentile(similarities, 0.50),
        "cosine_p95": percentile(similarities, 0.95),
        "cosine_max": max(similarities),
    }
    return rows, summary


def metric_row(
    snapshots: Mapping[str, Mapping[str, Any]],
    ranking: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    add_label: str,
    formatter_label: str,
) -> dict[str, Any]:
    metrics, _ = evaluate_all(snapshots, ranking)
    return {
        "Add 方式": add_label,
        "Formatter": formatter_label,
        "Query 检索方式": SEARCH_LABEL,
        "Eligible Gold": metrics["eligible_gold_count"],
        "Top5 recalled Gold": round(float(metrics["recall_at_5"]) * int(metrics["eligible_gold_count"])),
        "Top5": metrics["recall_at_5"],
        "Macro Top5": metrics["macro_session_r5"],
        "Top10": metrics["recall_at_10"],
        "Top20": metrics["recall_at_20"],
        "MRR": metrics["mrr"],
        "Mean Gold Rank": metrics["mean_gold_rank"],
    }


def compare_rankings(
    snapshots: Mapping[str, Mapping[str, Any]],
    before: Mapping[str, Sequence[Mapping[str, Any]]],
    after: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    comparison: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    before_maps = {query_id: rank_map(ranking) for query_id, ranking in before.items()}
    after_maps = {query_id: rank_map(ranking) for query_id, ranking in after.items()}
    promoted = demoted = rescued = hurt = 0
    gold_rows, query_rows, improvements = [], [], []
    top5_identical = 0
    for query in all_queries(snapshots):
        query_id = str(query["query_id"])
        gold_ids = [str(value) for value in query["eligible_gold_page_ids"]]
        old_ranks = {gold: int(before_maps[query_id][gold]["rank"]) for gold in gold_ids}
        new_ranks = {gold: int(after_maps[query_id][gold]["rank"]) for gold in gold_ids}
        query_promoted = sum(old_ranks[gold] > 5 and new_ranks[gold] <= 5 for gold in gold_ids)
        query_demoted = sum(old_ranks[gold] <= 5 and new_ranks[gold] > 5 for gold in gold_ids)
        promoted += query_promoted
        demoted += query_demoted
        old_hit, new_hit = any(rank <= 5 for rank in old_ranks.values()), any(rank <= 5 for rank in new_ranks.values())
        transition = (
            "RESCUED"
            if not old_hit and new_hit
            else "HURT"
            if old_hit and not new_hit
            else "UNCHANGED_HIT"
            if old_hit
            else "UNCHANGED_MISS"
        )
        rescued += int(transition == "RESCUED")
        hurt += int(transition == "HURT")
        old_top5 = [str(item["page_id"]) for item in before[query_id][:5]]
        new_top5 = [str(item["page_id"]) for item in after[query_id][:5]]
        top5_identical += int(old_top5 == new_top5)
        query_rows.append(
            {
                "comparison": comparison,
                "session_id": query["session_code"],
                "query_id": query_id,
                "transition": transition,
                "before_gold_ranks": old_ranks,
                "after_gold_ranks": new_ranks,
                "promoted_gold": query_promoted,
                "demoted_gold": query_demoted,
                "top5_page_ids_identical": old_top5 == new_top5,
            }
        )
        for gold in gold_ids:
            old_item = before_maps[query_id][gold]
            new_item = after_maps[query_id][gold]
            improvement = int(old_item["rank"]) - int(new_item["rank"])
            improvements.append(improvement)
            gold_rows.append(
                {
                    "comparison": comparison,
                    "session_id": query["session_code"],
                    "query_id": query_id,
                    "gold_page_id": gold,
                    "gold_source_turn_id": old_item["source_turn_id"],
                    "before_rank": int(old_item["rank"]),
                    "after_rank": int(new_item["rank"]),
                    "rank_improvement": improvement,
                    "before_score": float(old_item["score"]),
                    "after_score": float(new_item["score"]),
                    "score_difference": float(new_item["score"]) - float(old_item["score"]),
                }
            )
    comparison_row = {
        "Comparison": comparison,
        "Promoted Gold": promoted,
        "Demoted Gold": demoted,
        "Net Gold gain": promoted - demoted,
        "Rescued Queries": rescued,
        "Hurt Queries": hurt,
        "Mean Gold rank improvement": statistics.fmean(improvements),
        "Median Gold rank improvement": statistics.median(improvements),
        "Top5 rankings identical query count": top5_identical,
        "Top5 rankings identical all queries": top5_identical == len(query_rows),
    }
    return comparison_row, gold_rows, query_rows


def separation_row(
    snapshots: Mapping[str, Mapping[str, Any]],
    ranking: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    add_label: str,
    formatter_label: str,
) -> dict[str, Any]:
    margins = []
    for query in all_queries(snapshots):
        query_id = str(query["query_id"])
        gold_ids = {str(value) for value in query["eligible_gold_page_ids"]}
        gold_scores = [float(item["score"]) for item in ranking[query_id] if str(item["page_id"]) in gold_ids]
        nongold_scores = [float(item["score"]) for item in ranking[query_id] if str(item["page_id"]) not in gold_ids]
        margins.append(max(gold_scores) - max(nongold_scores))
    return {
        "Add 方式": add_label,
        "Formatter": formatter_label,
        "Query count": len(margins),
        "Best Gold - strongest NonGold mean": statistics.fmean(margins),
        "Best Gold - strongest NonGold median": statistics.median(margins),
        "Positive separation rate": sum(value > 0 for value in margins) / len(margins),
    }


def tokenizer_control(
    pages: Mapping[str, Sequence[Mapping[str, Any]]],
    tokenizer: Any,
    max_length: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    special = int(tokenizer.num_special_tokens_to_add(pair=False))
    budget = max_length - special
    detail, summary_rows = [], []
    for variant in ADD_VARIANTS:
        for formatter in FORMATTERS:
            selected = pages[variant]
            for page in selected:
                summary_text = str(page["summary"]).strip()
                keywords_text = "Keywords: " + ", ".join(str(value) for value in page["keywords"])
                user_text = f"User: {str(page['user_input']).strip()}" if formatter == "SummaryKeywordsUser" else ""
                full_text = str(page["full_text"] if formatter == "SummaryKeywordsUser" else page["no_user_text"])
                summary_ids = tokenizer.encode(summary_text, add_special_tokens=False)
                keyword_ids = tokenizer.encode(keywords_text, add_special_tokens=False)
                user_ids = tokenizer.encode(user_text, add_special_tokens=False) if user_text else []
                full_ids = tokenizer.encode(full_text, add_special_tokens=False)
                if list(summary_ids) + list(keyword_ids) + list(user_ids) != list(full_ids):
                    raise AssertionError(f"Tokenizer component mismatch: {variant}/{page['source_turn_id']}")
                remaining = budget
                summary_kept = min(len(summary_ids), remaining)
                remaining -= summary_kept
                keyword_kept = min(len(keyword_ids), remaining)
                remaining -= keyword_kept
                user_kept = min(len(user_ids), remaining)
                keyword_status = (
                    "FULLY_KEPT"
                    if keyword_kept == len(keyword_ids)
                    else "FULLY_TRUNCATED"
                    if keyword_kept == 0
                    else "PARTIALLY_TRUNCATED"
                )
                user_status = (
                    "NOT_PRESENT"
                    if not user_ids
                    else "FULLY_KEPT"
                    if user_kept == len(user_ids)
                    else "FULLY_TRUNCATED"
                    if user_kept == 0
                    else "PARTIALLY_TRUNCATED"
                )
                detail.append(
                    {
                        "session_id": page["session_code"],
                        "source_turn_id": page["source_turn_id"],
                        "page_id": page["page_id"],
                        "Add 方式": VARIANT_LABELS[variant],
                        "Formatter": FORMATTER_LABELS[formatter],
                        "raw_token_count_including_special": len(full_ids) + special,
                        "truncated": len(full_ids) + special > max_length,
                        "summary_tokens_total": len(summary_ids),
                        "summary_tokens_kept": summary_kept,
                        "summary_truncated": summary_kept < len(summary_ids),
                        "keywords_tokens_total": len(keyword_ids),
                        "keywords_tokens_kept": keyword_kept,
                        "keywords_truncation_status": keyword_status,
                        "user_tokens_total": len(user_ids),
                        "user_tokens_kept": user_kept,
                        "user_truncation_status": user_status,
                    }
                )
            current = [
                row
                for row in detail
                if row["Add 方式"] == VARIANT_LABELS[variant]
                and row["Formatter"] == FORMATTER_LABELS[formatter]
            ]
            summary_rows.append(
                {
                    "Add 方式": VARIANT_LABELS[variant],
                    "Formatter": FORMATTER_LABELS[formatter],
                    "Page count": len(current),
                    "512-token truncated Pages": sum(bool(row["truncated"]) for row in current),
                    "Summary truncated Pages": sum(bool(row["summary_truncated"]) for row in current),
                    "Keywords fully kept Pages": sum(
                        row["keywords_truncation_status"] == "FULLY_KEPT" for row in current
                    ),
                    "Keywords partially truncated Pages": sum(
                        row["keywords_truncation_status"] == "PARTIALLY_TRUNCATED" for row in current
                    ),
                    "Keywords fully truncated Pages": sum(
                        row["keywords_truncation_status"] == "FULLY_TRUNCATED" for row in current
                    ),
                    "User fully kept Pages": sum(row["user_truncation_status"] == "FULLY_KEPT" for row in current),
                    "User partially truncated Pages": sum(
                        row["user_truncation_status"] == "PARTIALLY_TRUNCATED" for row in current
                    ),
                    "User fully truncated Pages": sum(
                        row["user_truncation_status"] == "FULLY_TRUNCATED" for row in current
                    ),
                }
            )
    return detail, summary_rows


def session_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[tuple[str, str], Mapping[str, Sequence[Mapping[str, Any]]]],
) -> list[dict[str, Any]]:
    rows = []
    for (variant, formatter), ranking in rankings.items():
        _, sessions = evaluate_all(snapshots, ranking)
        for code in SESSION_CODES:
            value = sessions[code]
            rows.append(
                {
                    "Session": code,
                    "Add 方式": VARIANT_LABELS[variant],
                    "Formatter": FORMATTER_LABELS[formatter],
                    "Top5": value["recall_at_5"],
                    "Top10": value["recall_at_10"],
                    "Top20": value["recall_at_20"],
                    "MRR": value["mrr"],
                    "Mean Gold Rank": value["mean_gold_rank"],
                }
            )
    return rows


def selected_case_rows(
    snapshots: Mapping[str, Mapping[str, Any]],
    rankings: Mapping[tuple[str, str], Mapping[str, Sequence[Mapping[str, Any]]]],
    transitions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    query_ids = {"S005-Q042"}
    for variant in ADD_VARIANTS:
        label = f"{VARIANT_LABELS[variant]}：去除 User vs 原始 formatter"
        relevant = [row for row in transitions if row["comparison"] == label]
        rescued = sorted(
            (row for row in relevant if row["transition"] == "RESCUED"),
            key=lambda row: max(row["before_gold_ranks"].values()) - min(row["after_gold_ranks"].values()),
            reverse=True,
        )
        hurt = sorted(
            (row for row in relevant if row["transition"] == "HURT"),
            key=lambda row: max(row["after_gold_ranks"].values()) - min(row["before_gold_ranks"].values()),
            reverse=True,
        )
        query_ids.update(str(row["query_id"]) for row in [*rescued[:3], *hurt[:3]])
    queries = {str(row["query_id"]): row for row in all_queries(snapshots)}
    output = []
    maps = {
        key: {query_id: rank_map(ranking) for query_id, ranking in by_query.items()} for key, by_query in rankings.items()
    }
    for query_id in sorted(query_ids):
        query = queries[query_id]
        for gold in query["eligible_gold_page_ids"]:
            gold_id = str(gold)
            source_turn_id = maps[("Production", "SummaryKeywordsUser")][query_id][gold_id]["source_turn_id"]
            row = {
                "Session": query["session_code"],
                "Query ID": query_id,
                "Gold Page ID": gold_id,
                "Gold source_turn_id": source_turn_id,
            }
            for variant in ADD_VARIANTS:
                for formatter in FORMATTERS:
                    item = maps[(variant, formatter)][query_id][gold_id]
                    prefix = f"{VARIANT_LABELS[variant]} | {FORMATTER_LABELS[formatter]}"
                    row[f"{prefix} rank"] = int(item["rank"])
                    row[f"{prefix} score"] = float(item["score"])
            output.append(row)
    return output


def build_report(
    reencode_metrics: Sequence[Mapping[str, Any]],
    reproducibility: Mapping[str, Any],
    reencode_comparison: Mapping[str, Any],
    formatter_metrics: Sequence[Mapping[str, Any]],
    formatter_comparisons: Sequence[Mapping[str, Any]],
    separation: Sequence[Mapping[str, Any]],
    tokenizer_summary: Sequence[Mapping[str, Any]],
    sessions: Sequence[Mapping[str, Any]],
    cases: Sequence[Mapping[str, Any]],
) -> str:
    lines = [
        "# MidTerm Add Local Context — Control Experiments",
        "",
        "Search 全部固定为上下文引用解析后检索（P2）；没有调用 LLM，也没有修改 Prompt、Gold、visibility 或 Query。",
        "",
        "## 1. Production embedding 可复现性问题",
        "",
        "| Production vector | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in reencode_metrics:
        lines.append(
            f"| {row['Formatter']} | {float(row['Top5']):.2%} | {float(row['Macro Top5']):.2%} | "
            f"{float(row['Top10']):.2%} | {float(row['Top20']):.2%} | {float(row['MRR']):.4f} | "
            f"{float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            f"- Stored→fresh promoted/demoted/net：{reencode_comparison['Promoted Gold']}/"
            f"{reencode_comparison['Demoted Gold']}/{int(reencode_comparison['Net Gold gain']):+d}。",
            f"- 99 个 Query 的 Top5 Page ID 完全一致："
            f"{reencode_comparison['Top5 rankings identical all queries']} "
            f"({reencode_comparison['Top5 rankings identical query count']}/99)。",
            f"- Page vector cosine：mean={reproducibility['cosine_mean']:.12f}，"
            f"min={reproducibility['cosine_min']:.12f}，p5={reproducibility['cosine_p5']:.12f}，"
            f"p50={reproducibility['cosine_p50']:.12f}，p95={reproducibility['cosine_p95']:.12f}。",
            "",
            "## 2. Raw User / token budget 对上下文 Add 的影响",
            "",
            "| Add 方式 | Formatter | Top5 | Macro Top5 | Top10 | Top20 | MRR | Mean Gold Rank |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in formatter_metrics:
        lines.append(
            f"| {row['Add 方式']} | {row['Formatter']} | {float(row['Top5']):.2%} | "
            f"{float(row['Macro Top5']):.2%} | {float(row['Top10']):.2%} | {float(row['Top20']):.2%} | "
            f"{float(row['MRR']):.4f} | {float(row['Mean Gold Rank']):.2f} |"
        )
    lines.extend(
        [
            "",
            "### Formatter movement",
            "",
            "| Comparison | Promoted | Demoted | Net | Rescued Q | Hurt Q |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in formatter_comparisons:
        lines.append(
            f"| {row['Comparison']} | {row['Promoted Gold']} | {row['Demoted Gold']} | "
            f"{int(row['Net Gold gain']):+d} | {row['Rescued Queries']} | {row['Hurt Queries']} |"
        )
    lines.extend(
        [
            "",
            "### Token budget",
            "",
            "| Add 方式 | Formatter | Truncated | Summary truncated | Keywords fully kept | Keywords partial | Keywords fully lost |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in tokenizer_summary:
        lines.append(
            f"| {row['Add 方式']} | {row['Formatter']} | {row['512-token truncated Pages']} | "
            f"{row['Summary truncated Pages']} | {row['Keywords fully kept Pages']} | "
            f"{row['Keywords partially truncated Pages']} | {row['Keywords fully truncated Pages']} |"
        )
    lines.extend(
        [
            "",
            "### Query-conditioned separation",
            "",
            "| Add 方式 | Formatter | Mean best-Gold minus strongest-NonGold | Median | Positive rate |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in separation:
        lines.append(
            f"| {row['Add 方式']} | {row['Formatter']} | "
            f"{float(row['Best Gold - strongest NonGold mean']):.6f} | "
            f"{float(row['Best Gold - strongest NonGold median']):.6f} | "
            f"{float(row['Positive separation rate']):.2%} |"
        )
    session_lookup = {
        (str(row["Session"]), str(row["Add 方式"]), str(row["Formatter"])): float(row["Top5"])
        for row in sessions
    }
    lines.extend(
        [
            "",
            "### Session Top5",
            "",
            "| Session | Production Full | Production No User | Previous Full | Previous No User | Both Full | Both No User |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for code in SESSION_CODES:
        values = [
            session_lookup[(code, VARIANT_LABELS[variant], FORMATTER_LABELS[formatter])]
            for variant in ADD_VARIANTS
            for formatter in FORMATTERS
        ]
        lines.append(f"| {code} | " + " | ".join(f"{value:.2%}" for value in values) + " |")
    lines.extend(
        [
            "",
            f"S002 的前后文方案从 Full 的 "
            f"{session_lookup[('S002', VARIANT_LABELS['PreviousAndFollowingContext'], FORMATTER_LABELS['SummaryKeywordsUser'])]:.2%} "
            f"升至 No User 的 "
            f"{session_lookup[('S002', VARIANT_LABELS['PreviousAndFollowingContext'], FORMATTER_LABELS['SummaryKeywords'])]:.2%}，"
            f"但仍低于 No User Production 的 "
            f"{session_lookup[('S002', VARIANT_LABELS['Production'], FORMATTER_LABELS['SummaryKeywords'])]:.2%}，"
            "因此只属于部分恢复。",
        ]
    )
    lines.extend(
        [
            "",
            "### Selected rank controls",
            "",
            "下面列出 S005-Q042 及自动选择的 no-User RESCUED/HURT Query；完整 score 在 CSV 中。",
            "",
            "| Query | Gold source | Production Full→No User | Previous Full→No User | Both Full→No User |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in cases:
        values = []
        for variant in ADD_VARIANTS:
            full_key = f"{VARIANT_LABELS[variant]} | {FORMATTER_LABELS['SummaryKeywordsUser']} rank"
            no_user_key = f"{VARIANT_LABELS[variant]} | {FORMATTER_LABELS['SummaryKeywords']} rank"
            values.append(f"#{row[full_key]}→#{row[no_user_key]}")
        lines.append(
            f"| {row['Query ID']} | {row['Gold source_turn_id']} | " + " | ".join(values) + " |"
        )
    metric_lookup = {
        (str(row["Add 方式"]), str(row["Formatter"])): row for row in formatter_metrics
    }
    production_no_user = metric_lookup[(VARIANT_LABELS["Production"], FORMATTER_LABELS["SummaryKeywords"])]
    both_full = metric_lookup[
        (VARIANT_LABELS["PreviousAndFollowingContext"], FORMATTER_LABELS["SummaryKeywordsUser"])
    ]
    both_no_user = metric_lookup[
        (VARIANT_LABELS["PreviousAndFollowingContext"], FORMATTER_LABELS["SummaryKeywords"])
    ]
    token_lookup = {
        (str(row["Add 方式"]), str(row["Formatter"])): row for row in tokenizer_summary
    }
    both_full_tokens = token_lookup[
        (VARIANT_LABELS["PreviousAndFollowingContext"], FORMATTER_LABELS["SummaryKeywordsUser"])
    ]
    both_no_user_tokens = token_lookup[
        (VARIANT_LABELS["PreviousAndFollowingContext"], FORMATTER_LABELS["SummaryKeywords"])
    ]
    s005 = next(
        row for row in cases if row["Query ID"] == "S005-Q042" and row["Gold source_turn_id"] == "S005-Q036"
    )
    s005_values = []
    for variant in ADD_VARIANTS:
        full_key = f"{VARIANT_LABELS[variant]} | {FORMATTER_LABELS['SummaryKeywordsUser']} rank"
        no_user_key = f"{VARIANT_LABELS[variant]} | {FORMATTER_LABELS['SummaryKeywords']} rank"
        s005_values.append(f"{VARIANT_LABELS[variant]} #{s005[full_key]}→#{s005[no_user_key]}")
    lines.extend(
        [
            "",
            "## 控制结论",
            "",
            f"- Fresh Production Top5 与 stored 的差值："
            f"{float(reencode_metrics[1]['Top5']) - float(reencode_metrics[0]['Top5']):+.2%}。",
            f"- 去除 User 后，前后文方案相对去除 User 的 Production：Top5 "
            f"{float(both_no_user['Top5']) - float(production_no_user['Top5']):+.2%}，Top10 "
            f"{float(both_no_user['Top10']) - float(production_no_user['Top10']):+.2%}，Top20 "
            f"{float(both_no_user['Top20']) - float(production_no_user['Top20']):+.2%}。",
            f"- 前后文方案自身去除 User 后：Top5 "
            f"{float(both_no_user['Top5']) - float(both_full['Top5']):+.2%}，Top10 "
            f"{float(both_no_user['Top10']) - float(both_full['Top10']):+.2%}，Top20 "
            f"{float(both_no_user['Top20']) - float(both_full['Top20']):+.2%}；并非所有 cutoff 同时改善。",
            f"- User 位于 formatter 末尾，因此删除 User 没有改变前后文方案的 Summary 截断数 "
            f"({both_full_tokens['Summary truncated Pages']}→{both_no_user_tokens['Summary truncated Pages']})，"
            f"也没有改变 Keywords partial/full-loss 数 "
            f"({both_full_tokens['Keywords partially truncated Pages']}/{both_full_tokens['Keywords fully truncated Pages']}→"
            f"{both_no_user_tokens['Keywords partially truncated Pages']}/{both_no_user_tokens['Keywords fully truncated Pages']})。",
            f"- S005-Q042 没有被上下文方案救回：{'；'.join(s005_values)}。Production 的改善来自其他候选 Page "
            "移除 User 后的相对排序变化；该 Gold 在原 formatter 中本就未编码到 User。",
            "- 因此 Raw User 确实对 Top5 有小幅干扰，但当前结果不支持把后续重点完全归因于删除 User；"
            "上下文 Summary 自身超过 token budget 的问题仍然存在。",
            "- 本控制实验没有修改或生成任何 Add/Search Prompt，也没有生成下一版 Prompt。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, old_pages, queries = load_pages_queries()
    p2_texts = load_p2_texts(queries)
    query_vectors = load_query_vectors(queries)
    pages = load_existing_add_pages(old_pages)
    stored_vectors = {str(page["page_id"]): page["stored_embedding"] for page in old_pages}
    reproduction, old_rankings = validate_old_reproduction(snapshots, stored_vectors, query_vectors)
    context_full_vectors, existing_cache_metadata = load_existing_context_vectors(pages)
    fresh_vectors, no_user_vectors, embedding_metadata, embedding_cache = encode_controls(args.output_dir, pages)

    fresh_ranking = rank_configuration(snapshots, query_vectors["Search-P2"], fresh_vectors)
    full_rankings = {
        "Production": fresh_ranking,
        "PreviousContext": rank_configuration(
            snapshots, query_vectors["Search-P2"], context_full_vectors["PreviousContext"]
        ),
        "PreviousAndFollowingContext": rank_configuration(
            snapshots, query_vectors["Search-P2"], context_full_vectors["PreviousAndFollowingContext"]
        ),
    }
    no_user_rankings = {
        variant: rank_configuration(snapshots, query_vectors["Search-P2"], no_user_vectors[variant])
        for variant in ADD_VARIANTS
    }
    rankings = {
        **{(variant, "SummaryKeywordsUser"): full_rankings[variant] for variant in ADD_VARIANTS},
        **{(variant, "SummaryKeywords"): no_user_rankings[variant] for variant in ADD_VARIANTS},
    }

    vector_rows, vector_summary = vector_reproducibility(pages["Production"], stored_vectors, fresh_vectors)
    stored_metric = metric_row(
        snapshots,
        old_rankings["OLD_P2"],
        add_label=VARIANT_LABELS["Production"],
        formatter_label="Stored Production embedding",
    )
    fresh_metric = metric_row(
        snapshots,
        fresh_ranking,
        add_label=VARIANT_LABELS["Production"],
        formatter_label="Fresh re-encoded Production embedding",
    )
    reencode_comparison, reencode_gold, reencode_queries = compare_rankings(
        snapshots,
        old_rankings["OLD_P2"],
        fresh_ranking,
        comparison="Fresh re-encoded Production vs stored Production",
    )

    formatter_metrics = [
        metric_row(
            snapshots,
            rankings[(variant, formatter)],
            add_label=VARIANT_LABELS[variant],
            formatter_label=FORMATTER_LABELS[formatter],
        )
        for variant in ADD_VARIANTS
        for formatter in FORMATTERS
    ]
    formatter_comparisons, formatter_gold, formatter_transitions = [], [], []
    for variant in ADD_VARIANTS:
        comparison = f"{VARIANT_LABELS[variant]}：去除 User vs 原始 formatter"
        aggregate, gold_rows, query_rows = compare_rankings(
            snapshots,
            full_rankings[variant],
            no_user_rankings[variant],
            comparison=comparison,
        )
        formatter_comparisons.append(aggregate)
        formatter_gold.extend(gold_rows)
        formatter_transitions.extend(query_rows)
    no_user_cross_comparison, no_user_cross_gold, no_user_cross_queries = compare_rankings(
        snapshots,
        no_user_rankings["Production"],
        no_user_rankings["PreviousAndFollowingContext"],
        comparison="去除 User 后：Production Add + 最近上文及下文 vs 原 Production Add",
    )
    formatter_comparisons.append(no_user_cross_comparison)
    formatter_gold.extend(no_user_cross_gold)
    formatter_transitions.extend(no_user_cross_queries)

    separation = [
        separation_row(
            snapshots,
            rankings[(variant, formatter)],
            add_label=VARIANT_LABELS[variant],
            formatter_label=FORMATTER_LABELS[formatter],
        )
        for variant in ADD_VARIANTS
        for formatter in FORMATTERS
    ]
    embedder = embedding_cache._load_model(PRODUCTION_EMBEDDING)
    tokenizer = embedder.model.tokenizer
    max_length = int(embedder.model.max_seq_length)
    tokenizer_detail, tokenizer_summary = tokenizer_control(pages, tokenizer, max_length)
    per_session = session_rows(snapshots, rankings)
    case_rows = selected_case_rows(snapshots, rankings, formatter_transitions)

    visibility = {
        str(row["query_id"]): [str(page_id) for page_id in row["visible_page_ids"]]
        for code in SESSION_CODES
        for row in snapshots[code]["visibility"]
    }
    page_ids = {str(page["page_id"]) for page in old_pages}
    validations = {
        "Stored Production P2 reproduction": reproduction["B"],
        "Frozen counts": {
            "status": "PASS"
            if len(old_pages) == 333
            and len(queries) == 99
            and sum(len(query["eligible_gold_page_ids"]) for query in queries) == 154
            else "FAIL",
            "page_count": len(old_pages),
            "query_count": len(queries),
            "eligible_gold_count": sum(len(query["eligible_gold_page_ids"]) for query in queries),
        },
        "Frozen Page identity": {
            "status": "PASS"
            if all({str(row["page_id"]) for row in pages[variant]} == page_ids for variant in ADD_VARIANTS)
            else "FAIL"
        },
        "Frozen visibility": {
            "status": "PASS"
            if len(visibility) == 99 and all(set(values) <= page_ids for values in visibility.values())
            else "FAIL",
            "sha256": stable_hash(visibility),
        },
        "Frozen P2 Query": {
            "status": "PASS" if len(p2_texts) == len(query_vectors["Search-P2"]) == 99 else "FAIL",
            "query_count": len(p2_texts),
        },
        "No-User deletion-only formatter": {"status": "PASS", "page_count_per_variant": 333},
        "No new LLM calls": {"status": "PASS", "new_llm_calls": 0},
    }
    if any(value["status"] != "PASS" for value in validations.values()):
        raise AssertionError(validations)

    metrics_dir = args.output_dir / "metrics"
    analysis_dir = args.output_dir / "analysis"
    write_csv(metrics_dir / "production_reencode_control.csv", [stored_metric, fresh_metric])
    write_csv(metrics_dir / "production_reencode_gold_rank_differences.csv", reencode_gold)
    write_csv(metrics_dir / "page_formatter_no_user_control.csv", formatter_metrics)
    write_csv(metrics_dir / "page_formatter_no_user_comparisons.csv", formatter_comparisons)
    write_csv(metrics_dir / "no_user_gold_rank_differences.csv", formatter_gold)
    write_csv(metrics_dir / "no_user_session_metrics.csv", per_session)
    write_csv(analysis_dir / "production_embedding_reproducibility.csv", vector_rows)
    write_csv(analysis_dir / "production_reencode_query_top5_identity.csv", reencode_queries)
    write_csv(analysis_dir / "no_user_query_transitions.csv", formatter_transitions)
    write_csv(analysis_dir / "no_user_separation.csv", separation)
    write_csv(analysis_dir / "no_user_tokenizer_audit.csv", tokenizer_detail)
    write_csv(analysis_dir / "no_user_tokenizer_summary.csv", tokenizer_summary)
    write_csv(analysis_dir / "selected_case_rank_control.csv", case_rows)

    report = build_report(
        [stored_metric, fresh_metric],
        vector_summary,
        reencode_comparison,
        formatter_metrics,
        formatter_comparisons,
        separation,
        tokenizer_summary,
        per_session,
        case_rows,
    )
    (args.output_dir / "control_experiment_report.md").write_text(report, encoding="utf-8")
    metadata = {
        "experiment_name": "midterm_add_local_context_controls",
        "parent_experiment": str(OUTPUT_DIR),
        "session_count": 5,
        "page_count": 333,
        "evaluation_query_count": 99,
        "eligible_gold_count": 154,
        "search": SEARCH_LABEL,
        "embedding_model": PRODUCTION_EMBEDDING,
        "embedding_mode": "add",
        "dimension": 512,
        "production_summary_keywords_regenerated": False,
        "context_summaries_keywords_regenerated": False,
        "new_llm_calls": 0,
        "full_session_rerun": False,
        "query_embeddings_reused": 99,
        "stored_production_page_vectors_reused": 333,
        "fresh_production_full_page_embeddings": 333,
        "new_no_user_page_embeddings": 999,
        "existing_context_full_vector_cache": existing_cache_metadata,
        "control_embedding_metadata": embedding_metadata,
        "production_vector_reproducibility": vector_summary,
        "production_reencode_top5_identity": reencode_comparison,
        "validation": validations,
        "validation_all_pass": all(value["status"] == "PASS" for value in validations.values()),
        "gold_changed": False,
        "visible_page_ids_changed": False,
        "query_changed": False,
        "prompt_changed": False,
        "automatic_next_prompt_generated": False,
    }
    dump_json(args.output_dir / "control_run_metadata.json", metadata)
    print(
        json.dumps(
            {
                "production_reencode": [stored_metric, fresh_metric],
                "production_vector_reproducibility": vector_summary,
                "production_top5_identity": reencode_comparison,
                "formatter_metrics": formatter_metrics,
                "formatter_comparisons": formatter_comparisons,
                "tokenizer": tokenizer_summary,
                "validation_all_pass": metadata["validation_all_pass"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
