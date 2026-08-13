"""Compute and plot Query-level completion from newly rebuilt S001-S010 artifacts."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from exp.benchmark.analyze_query_completion_s001_s005_v2 import (
    COMPLETION_CONFIGS,
    HISTOGRAM_BINS,
    build_query_completion_rows,
    completion_statistics,
    group_rank,
    load_v3_dataset,
    sha256_file,
    static_dataset_stats,
    write_csv,
    write_jsonl,
)
from exp.benchmark.benchmark_common import ensure_repo_root_on_path

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))
DEFAULT_DATASET = REPO_ROOT / "exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx"
DEFAULT_RECALL_DIR = REPO_ROOT / "exp/results/recall_rebalanced_s001_s010_isolated"
DEFAULT_C3_RANKING = REPO_ROOT / "exp/results/midterm_c3_rebalanced_s001_s010/c3_rankings.jsonl"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "exp/results/full_memory_recall_s001_s010_rebalanced"
SESSION_CODES = tuple(f"S{index:03d}" for index in range(1, 11))
SHORT_TERM_QA_CAPACITY = 3
TOP_K = 5
EXPECTED_DATASET = {
    "query_count": 752,
    "gold_requirement_count": 786,
    "or_requirement_count": 0,
    "shortterm_hit_count": 400,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebalanced S001-S010 Query Completion analysis")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--recall-dir", type=Path, default=DEFAULT_RECALL_DIR)
    parser.add_argument("--c3-ranking", type=Path, default=DEFAULT_C3_RANKING)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_c3_rankings(path: Path) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(path):
        grouped[str(row["query_id"]).upper()].append(row)
    result: dict[str, tuple[str, ...]] = {}
    for query_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: int(row["c3_rank"]))
        ranks = [int(row["c3_rank"]) for row in ordered]
        if ranks != list(range(1, len(rows) + 1)):
            raise AssertionError(f"{query_id}: C3 ranking is not contiguous")
        sources = tuple(str(row["source_turn_id"]).upper() for row in ordered)
        if len(sources) != len(set(sources)):
            raise AssertionError(f"{query_id}: C3 ranking contains duplicate source turns")
        result[query_id] = sources
    if not result:
        raise AssertionError("C3 ranking is empty")
    return result


def load_longterm_provenance(
    recall_dir: Path,
    expected_query_ids: set[str],
) -> tuple[dict[str, tuple[str, ...]], dict[str, Any]]:
    result: dict[str, tuple[str, ...]] = {}
    session_validation: list[dict[str, Any]] = []
    for code in SESSION_CODES:
        result_dir = recall_dir / code
        summary = json.loads((result_dir / "recall_summary.json").read_text(encoding="utf-8"))
        if int(summary.get("failed_turns") or 0):
            raise AssertionError(f"{code}: isolated source run has failed turns")
        rows = load_jsonl(result_dir / "recall_turn_results.jsonl")
        if len(rows) != int(summary["total_turns"]):
            raise AssertionError(f"{code}: recall JSONL count differs from summary")
        for row in rows:
            query_id = str(row["turn_id"]).upper()
            if row.get("error"):
                raise AssertionError(f"{query_id}: retrieval error: {row['error']}")
            if query_id in result:
                raise AssertionError(f"Duplicate LongTerm trace: {query_id}")
            result[query_id] = tuple(
                str(item).upper() for item in (row.get("long_retrieved_turn_ids") or [])[:TOP_K]
            )
        session_validation.append(
            {
                "session_id": code,
                "turn_count": len(rows),
                "failed_turns": int(summary["failed_turns"]),
                "evaluated_turns": int(summary["evaluated_turns"]),
            }
        )
    if set(result) != expected_query_ids:
        raise AssertionError(
            f"LongTerm Query coverage mismatch: missing={len(expected_query_ids - set(result))}, "
            f"extra={len(set(result) - expected_query_ids)}"
        )
    return result, {"status": "PASS", "sessions": session_validation}


def build_results(
    sessions: Mapping[str, Sequence[Any]],
    c3_rankings: Mapping[str, Sequence[str]],
    longterm_ids: Mapping[str, Sequence[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirement_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    expected_routed_queries: set[str] = set()
    for code, turns in sessions.items():
        for turn in turns:
            short_ids = tuple(
                item.query_id
                for item in turns[max(0, turn.turn_index - SHORT_TERM_QA_CAPACITY) : turn.turn_index]
            )
            if any(not group.hit_by(short_ids) for group in turn.gold_groups):
                expected_routed_queries.add(turn.query_id)
            mid_ids = c3_rankings.get(turn.query_id, ())
            long_ids = longterm_ids[turn.query_id]
            rows: list[dict[str, Any]] = []
            for group_index, group in enumerate(turn.gold_groups, start=1):
                short_rank = group_rank(group, short_ids)
                midterm_rank = group_rank(group, mid_ids)
                longterm_rank = group_rank(group, long_ids)
                row = {
                    "requirement_id": f"{turn.query_id}::G{group_index}",
                    "session_id": code,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "group_index": group_index,
                    "gold_members": list(group.members),
                    "is_or": group.is_or,
                    "shortterm_rank": short_rank,
                    "shortterm_hit": short_rank is not None,
                    "midterm_ranking_available": turn.query_id in c3_rankings,
                    "midterm_rank": midterm_rank,
                    "midterm_hit_at_5": midterm_rank is not None and midterm_rank <= TOP_K,
                    "longterm_rank": longterm_rank,
                    "longterm_hit": longterm_rank is not None,
                }
                row["final_hit"] = bool(
                    row["shortterm_hit"] or row["midterm_hit_at_5"] or row["longterm_hit"]
                )
                requirement_rows.append(row)
                rows.append(row)
            query_rows.append(
                {
                    "session_id": code,
                    "query_id": turn.query_id,
                    "turn_index": turn.turn_index,
                    "parsed_gold_groups": [list(group.members) for group in turn.gold_groups],
                    "shortterm_hit": [bool(row["shortterm_hit"]) for row in rows],
                    "midterm_rank": [row["midterm_rank"] for row in rows],
                    "longterm_hit": [bool(row["longterm_hit"]) for row in rows],
                    "final_hit": [bool(row["final_hit"]) for row in rows],
                }
            )
    if set(c3_rankings) != expected_routed_queries:
        raise AssertionError(
            f"C3 routed Query set mismatch: missing={len(expected_routed_queries - set(c3_rankings))}, "
            f"extra={len(set(c3_rankings) - expected_routed_queries)}"
        )
    return requirement_rows, query_rows


def validate(
    dataset_stats: Mapping[str, Any],
    requirement_rows: Sequence[Mapping[str, Any]],
    query_rows: Sequence[Mapping[str, Any]],
    detail_rows: Sequence[Mapping[str, Any]],
    requirement_hits: Mapping[str, Any],
) -> dict[str, Any]:
    actual = {
        "query_count": int(dataset_stats["query_count"]),
        "gold_requirement_count": int(dataset_stats["gold_requirement_count"]),
        "or_requirement_count": int(dataset_stats["or_requirement_count"]),
        "shortterm_hit_count": int(requirement_hits["short"]),
    }
    if actual != EXPECTED_DATASET:
        raise AssertionError(f"Rebalanced workbook baseline changed: {actual}")
    if len(query_rows) != actual["query_count"] or len(requirement_rows) != actual["gold_requirement_count"]:
        raise AssertionError("Query/requirement coverage mismatch")
    if sum(bool(row["is_or"]) for row in requirement_rows) != actual["or_requirement_count"]:
        raise AssertionError("OR requirements were not counted exactly once")
    if sum(int(row["gold_requirement_count"]) for row in detail_rows) != actual["gold_requirement_count"]:
        raise AssertionError("Completion denominators dropped or duplicated Gold requirements")
    for row in detail_rows:
        singles = (
            float(row["shortterm_completion"]),
            float(row["midterm_completion"]),
            float(row["longterm_completion"]),
        )
        pairs = (
            float(row["short_mid_completion"]),
            float(row["short_long_completion"]),
            float(row["mid_long_completion"]),
        )
        if pairs[0] < max(singles[0], singles[1]):
            raise AssertionError(f"{row['query_id']}: Short+Mid monotonicity failed")
        if pairs[1] < max(singles[0], singles[2]):
            raise AssertionError(f"{row['query_id']}: Short+Long monotonicity failed")
        if pairs[2] < max(singles[1], singles[2]):
            raise AssertionError(f"{row['query_id']}: Mid+Long monotonicity failed")
        if float(row["all_memory_completion"]) < max(pairs):
            raise AssertionError(f"{row['query_id']}: all-memory monotonicity failed")
    outside_short = sum(not bool(row["shortterm_hit"]) for row in requirement_rows)
    outside_short_mid_hits = sum(
        not bool(row["shortterm_hit"]) and bool(row["midterm_hit_at_5"]) for row in requirement_rows
    )
    routed_queries = {str(row["query_id"]) for row in requirement_rows if not bool(row["shortterm_hit"])}
    return {
        "status": "PASS",
        "dataset": actual,
        "gold_bearing_query_count": len(detail_rows),
        "no_gold_query_count": int(requirement_hits["no_gold_query_count"]),
        "outside_shortterm_requirement_count": outside_short,
        "midterm_routed_query_count": len(routed_queries),
        "midterm_requirement_hit_at_5_all_gold": int(requirement_hits["mid"]),
        "midterm_outside_shortterm_hit_at_5": outside_short_mid_hits,
        "longterm_requirement_hit_at_5": int(requirement_hits["long"]),
        "all_memory_requirement_hit_count": int(requirement_hits["all_memory"]),
        "or_groups_counted_once": True,
        "query_completion_monotonicity": True,
    }


def histogram_y_limit(detail_rows: Sequence[Mapping[str, Any]]) -> int:
    maximum = 0
    for _, _, field in COMPLETION_CONFIGS:
        counts, _ = np.histogram([float(row[field]) * 100 for row in detail_rows], bins=HISTOGRAM_BINS)
        maximum = max(maximum, int(counts.max()))
    return max(10, int(math.ceil(maximum * 1.08 / 10.0) * 10))


def plot_histogram(
    axis: Any,
    values: Sequence[float],
    stats: Mapping[str, Any],
    title: str,
    y_limit: int,
    *,
    highlight_extremes: bool = False,
) -> None:
    counts, edges = np.histogram(np.asarray(values) * 100, bins=HISTOGRAM_BINS)
    colors = ["#4C78A8"] * len(counts)
    if highlight_extremes:
        colors[0] = "#D55E00"
        colors[-1] = "#009E73"
    axis.bar(edges[:-1], counts, width=np.diff(edges) * 0.9, align="edge", color=colors, edgecolor="white")
    axis.set_xlim(0, 100)
    axis.set_ylim(0, y_limit)
    axis.set_xticks(np.arange(0, 101, 10))
    axis.grid(axis="y", alpha=0.25, linewidth=0.7)
    axis.set_xlabel("Query Completion (%)")
    axis.set_ylabel("Query Count")
    axis.set_title(
        f"{title}\nMean {stats['mean']:.1%} | Median {stats['median']:.1%} | "
        f"100% Complete {stats['full_complete_query_rate']:.1%}",
        fontsize=9,
    )


def plot_matrix(
    output_path: Path,
    details: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Mapping[str, Any]],
    y_limit: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    matrix = (
        (
            ("short", "shortterm_completion"),
            ("short_mid", "short_mid_completion"),
            ("short_long", "short_long_completion"),
        ),
        (
            ("short_mid", "short_mid_completion"),
            ("mid", "midterm_completion"),
            ("mid_long", "mid_long_completion"),
        ),
        (
            ("short_long", "short_long_completion"),
            ("mid_long", "mid_long_completion"),
            ("long", "longterm_completion"),
        ),
    )
    labels = ("ShortTerm", "MidTerm@5", "LongTerm@5")
    figure, axes = plt.subplots(3, 3, figsize=(19, 14), sharex=True, sharey=True)
    for row_index, row in enumerate(matrix):
        for column_index, (key, field) in enumerate(row):
            plot_histogram(
                axes[row_index, column_index],
                [float(detail[field]) for detail in details],
                stats[key],
                str(stats[key]["label"]),
                y_limit,
            )
            if row_index == 0:
                axes[row_index, column_index].annotate(
                    labels[column_index],
                    xy=(0.5, 1.25),
                    xycoords="axes fraction",
                    ha="center",
                    va="bottom",
                    fontsize=13,
                    fontweight="bold",
                )
            if column_index == 0:
                axes[row_index, column_index].annotate(
                    labels[row_index],
                    xy=(-0.27, 0.5),
                    xycoords="axes fraction",
                    ha="right",
                    va="center",
                    rotation=90,
                    fontsize=13,
                    fontweight="bold",
                )
    figure.suptitle("Query Completion Matrix (S001-S010 Rebalanced, Gold-bearing Queries)", fontsize=16, y=0.995)
    figure.tight_layout(rect=(0.04, 0.02, 1, 0.97), h_pad=2.8, w_pad=1.6)
    figure.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(figure)


def plot_all_memory(
    output_path: Path,
    details: Sequence[Mapping[str, Any]],
    stats_by_config: Mapping[str, Mapping[str, Any]],
    y_limit: int,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stats = stats_by_config["all_memory"]
    values = [float(row["all_memory_completion"]) for row in details]
    partial = int(stats["query_count"]) - int(stats["zero_complete_query_count"]) - int(
        stats["full_complete_query_count"]
    )
    figure, axis = plt.subplots(figsize=(11, 6.5))
    plot_histogram(
        axis,
        values,
        stats,
        "ShortTerm + MidTerm + LongTerm",
        y_limit,
        highlight_extremes=True,
    )
    axis.text(
        0.025,
        0.94,
        f"Exact 0%: {stats['zero_complete_query_count']} ({stats['zero_complete_query_rate']:.1%})\n"
        f"Partial: {partial} ({partial / stats['query_count']:.1%})\n"
        f"Exact 100%: {stats['full_complete_query_count']} ({stats['full_complete_query_rate']:.1%})",
        transform=axis.transAxes,
        va="top",
        fontsize=11,
        bbox={"boxstyle": "round,pad=0.45", "facecolor": "white", "edgecolor": "#777777", "alpha": 0.92},
    )
    figure.tight_layout()
    figure.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(figure)


def pairwise_improvements(stats: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    specs = (
        ("short_mid", "short", "mid"),
        ("short_long", "short", "long"),
        ("mid_long", "mid", "long"),
    )
    rows = []
    for pair, first, second in specs:
        best_single = max(float(stats[first]["mean"]), float(stats[second]["mean"]))
        rows.append(
            {
                "configuration": str(stats[pair]["label"]),
                "mean_completion": float(stats[pair]["mean"]),
                "best_constituent_mean": best_single,
                "gain_over_best_constituent": float(stats[pair]["mean"]) - best_single,
            }
        )
    return sorted(rows, key=lambda row: float(row["gain_over_best_constituent"]), reverse=True)


def render_summary(
    stats: Mapping[str, Mapping[str, Any]],
    validation: Mapping[str, Any],
    pairwise: Sequence[Mapping[str, Any]],
    dataset: Path,
) -> str:
    lines = [
        "# Query Completion Summary (S001-S010 Rebalanced)",
        "",
        f"- Dataset: `{dataset}`",
        f"- Queries: {validation['dataset']['query_count']}",
        f"- Gold requirements: {validation['dataset']['gold_requirement_count']}",
        f"- OR groups: {validation['dataset']['or_requirement_count']}",
        f"- Gold-bearing Queries: {validation['gold_bearing_query_count']}",
        f"- Queries without Gold (excluded): {validation['no_gold_query_count']}",
        "",
        "| Configuration | Queries | Mean | Median | P25 | P75 | 0% Complete | 100% Complete | >=50% | >=80% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label, _ in COMPLETION_CONFIGS:
        row = stats[key]
        lines.append(
            f"| {label} | {row['query_count']} | {row['mean']:.2%} | {row['median']:.2%} | "
            f"{row['p25']:.2%} | {row['p75']:.2%} | "
            f"{row['zero_complete_query_count']} ({row['zero_complete_query_rate']:.2%}) | "
            f"{row['full_complete_query_count']} ({row['full_complete_query_rate']:.2%}) | "
            f"{row['completion_ge_50_rate']:.2%} | {row['completion_ge_80_rate']:.2%} |"
        )
    best = pairwise[0]
    lines.extend(
        [
            "",
            "## Requirement-level results and validation",
            "",
            f"- ShortTerm: {validation['dataset']['shortterm_hit_count']}/"
            f"{validation['dataset']['gold_requirement_count']}.",
            f"- MidTerm C3 routed @5: {validation['midterm_outside_shortterm_hit_at_5']}/"
            f"{validation['outside_shortterm_requirement_count']} outside-ShortTerm requirements.",
            f"- MidTerm C3 layer hits over the unified completion denominator: "
            f"{validation['midterm_requirement_hit_at_5_all_gold']}/"
            f"{validation['dataset']['gold_requirement_count']}; unrouted Queries count as misses.",
            f"- LongTerm@5 direct frozen provenance: {validation['longterm_requirement_hit_at_5']}/"
            f"{validation['dataset']['gold_requirement_count']}.",
            f"- Three-layer requirement union: {validation['all_memory_requirement_hit_count']}/"
            f"{validation['dataset']['gold_requirement_count']}.",
            f"- Largest pairwise mean-completion gain over its better constituent: {best['configuration']} "
            f"({best['gain_over_best_constituent']:+.2%}).",
            "- Per-Query pairwise/all-memory monotonicity passed; OR groups are counted once.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    dataset = args.dataset.resolve()
    recall_dir = args.recall_dir.resolve()
    c3_ranking = args.c3_ranking.resolve()
    output_dir = args.output_dir.resolve()
    sessions = load_v3_dataset(dataset, SESSION_CODES)
    dataset_stats = static_dataset_stats(sessions, SHORT_TERM_QA_CAPACITY, TOP_K)
    query_ids = {turn.query_id for turns in sessions.values() for turn in turns}
    c3_rankings = load_c3_rankings(c3_ranking)
    longterm_ids, source_validation = load_longterm_provenance(recall_dir, query_ids)
    requirements, queries = build_results(sessions, c3_rankings, longterm_ids)
    details, requirement_hits = build_query_completion_rows(requirements, queries, longterm_ids)
    stats = completion_statistics(details, requirement_hits)
    analysis_validation = validate(dataset_stats, requirements, queries, details, requirement_hits)
    pairwise = pairwise_improvements(stats)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "requirement_results.csv", requirements)
    write_jsonl(output_dir / "requirement_results.jsonl", requirements)
    write_csv(output_dir / "query_results.csv", queries)
    write_jsonl(output_dir / "query_results.jsonl", queries)
    write_csv(output_dir / "query_completion_details.csv", details)
    write_jsonl(output_dir / "query_completion_details.jsonl", details)
    (output_dir / "dataset_stats.json").write_text(
        json.dumps(dataset_stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "query_completion_stats.json").write_text(
        json.dumps(
            {
                "configurations": stats,
                "validation": analysis_validation,
                "source_recall_validation": source_validation,
                "pairwise_improvements": pairwise,
                "histogram_bins_percent": HISTOGRAM_BINS.tolist(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "run_metadata.json").write_text(
        json.dumps(
            {
                "mode": "new_isolated_session_provenance_plus_rebuilt_midterm_c3",
                "dataset": str(dataset),
                "dataset_sha256": sha256_file(dataset),
                "recall_dir": str(recall_dir),
                "c3_ranking": str(c3_ranking),
                "c3_ranking_sha256": sha256_file(c3_ranking),
                "shortterm_qa_turns": SHORT_TERM_QA_CAPACITY,
                "midterm_top_k": TOP_K,
                "longterm_top_k": TOP_K,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "query_completion_summary.md").write_text(
        render_summary(stats, analysis_validation, pairwise, dataset), encoding="utf-8"
    )
    y_limit = histogram_y_limit(details)
    plot_matrix(output_dir / "query_completion_matrix.png", details, stats, y_limit)
    plot_all_memory(output_dir / "query_completion_all_memory.png", details, stats, y_limit)
    print((output_dir / "query_completion_summary.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
