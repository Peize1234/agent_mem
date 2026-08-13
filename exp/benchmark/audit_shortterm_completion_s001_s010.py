"""Audit ShortTerm Query completion when frozen Mid/Long artifacts are incompatible."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from exp.benchmark.analyze_query_completion_s001_s005_v2 import (
    HISTOGRAM_BINS,
    REPO_ROOT,
    load_v3_dataset,
    sha256_file,
    source_qa_hash,
    static_dataset_stats,
)

DEFAULT_DATASET = REPO_ROOT / "exp/金融分析数据集_前10个Session长短期记忆再平衡.xlsx"
FROZEN_SOURCE_DATASET = REPO_ROOT / "exp/enterprise_finance_memory_sessions_100_v3_realistic_2023_2025.xlsx"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "exp/results/shortterm_completion_s001_s010_rebalanced"
SESSION_CODES = tuple(f"S{index:03d}" for index in range(1, 11))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="S001-S010 rebalanced workbook ShortTerm completion audit")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False) + "\n")


def compare_source_qa(
    selected_sessions: Mapping[str, Sequence[Any]], frozen_sessions: Mapping[str, Sequence[Any]]
) -> dict[str, Any]:
    changed_query_ids: list[str] = []
    for session_code in SESSION_CODES:
        selected_by_id = {turn.query_id: turn for turn in selected_sessions[session_code]}
        frozen_by_id = {turn.query_id: turn for turn in frozen_sessions[session_code]}
        if set(selected_by_id) != set(frozen_by_id):
            raise AssertionError(f"Query set differs from the reference workbook: {session_code}")
        for query_id, selected in selected_by_id.items():
            frozen = frozen_by_id[query_id]
            if (selected.question, selected.answer) != (frozen.question, frozen.answer):
                changed_query_ids.append(query_id)
    return {
        "selected_source_qa_sha256": source_qa_hash(selected_sessions),
        "frozen_source_qa_sha256": source_qa_hash(frozen_sessions),
        "source_qa_matches_frozen_artifacts": not changed_query_ids,
        "changed_query_count": len(changed_query_ids),
        "changed_query_ids": changed_query_ids,
    }


def build_shortterm_rows(
    sessions: Mapping[str, Sequence[Any]], qa_turns: int = 3
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirement_rows: list[dict[str, Any]] = []
    query_rows: list[dict[str, Any]] = []
    for session_code, turns in sessions.items():
        for turn in turns:
            visible_ids = tuple(
                candidate.query_id for candidate in turns[max(0, turn.turn_index - qa_turns) : turn.turn_index]
            )
            hits: list[bool] = []
            for group_index, group in enumerate(turn.gold_groups, start=1):
                hit = group.hit_by(visible_ids)
                hits.append(hit)
                requirement_rows.append(
                    {
                        "requirement_id": f"{turn.query_id}::G{group_index}",
                        "session_id": session_code,
                        "query_id": turn.query_id,
                        "turn_index": turn.turn_index,
                        "group_index": group_index,
                        "gold_members": list(group.members),
                        "is_or": group.is_or,
                        "shortterm_hit": hit,
                    }
                )
            if hits:
                hit_count = sum(hits)
                query_rows.append(
                    {
                        "session_id": session_code,
                        "query_id": turn.query_id,
                        "gold_requirement_count": len(hits),
                        "shortterm_hit_count": hit_count,
                        "shortterm_completion": hit_count / len(hits),
                    }
                )
    return requirement_rows, query_rows


def aggregate_shortterm(
    dataset_stats: Mapping[str, Any], requirement_rows: Sequence[Mapping[str, Any]], query_rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    values = np.asarray([float(row["shortterm_completion"]) for row in query_rows])
    if len(requirement_rows) != int(dataset_stats["gold_requirement_count"]):
        raise AssertionError("Gold requirement count mismatch")
    if sum(bool(row["is_or"]) for row in requirement_rows) != int(dataset_stats["or_requirement_count"]):
        raise AssertionError("OR requirement count mismatch")
    hit_count = sum(bool(row["shortterm_hit"]) for row in requirement_rows)
    if hit_count != int(dataset_stats["shortterm_requirement_count"]):
        raise AssertionError("ShortTerm requirement hit count mismatch")
    zero_count = int(np.count_nonzero(values == 0))
    full_count = int(np.count_nonzero(values == 1))
    return {
        "query_count": len(values),
        "no_gold_query_count": int(dataset_stats["query_count"]) - len(values),
        "gold_requirement_count": len(requirement_rows),
        "requirement_hit_count": hit_count,
        "requirement_recall": hit_count / len(requirement_rows),
        "mean": float(np.mean(values)),
        "median": float(np.median(values)),
        "p25": float(np.percentile(values, 25)),
        "p75": float(np.percentile(values, 75)),
        "zero_complete_query_count": zero_count,
        "zero_complete_query_rate": zero_count / len(values),
        "full_complete_query_count": full_count,
        "full_complete_query_rate": full_count / len(values),
        "completion_ge_50_rate": float(np.mean(values >= 0.5)),
        "completion_ge_80_rate": float(np.mean(values >= 0.8)),
    }


def session_rows(query_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for session_code in SESSION_CODES:
        rows = [row for row in query_rows if row["session_id"] == session_code]
        values = np.asarray([float(row["shortterm_completion"]) for row in rows])
        requirement_count = sum(int(row["gold_requirement_count"]) for row in rows)
        hit_count = sum(int(row["shortterm_hit_count"]) for row in rows)
        result.append(
            {
                "session_id": session_code,
                "gold_query_count": len(rows),
                "gold_requirement_count": requirement_count,
                "shortterm_hit_count": hit_count,
                "shortterm_requirement_recall": hit_count / requirement_count,
                "mean_query_completion": float(np.mean(values)),
                "full_complete_query_count": int(np.count_nonzero(values == 1)),
                "zero_complete_query_count": int(np.count_nonzero(values == 0)),
            }
        )
    return result


def plot_shortterm(path: Path, query_rows: Sequence[Mapping[str, Any]], summary: Mapping[str, Any]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    values = np.asarray([float(row["shortterm_completion"]) for row in query_rows]) * 100
    counts, edges = np.histogram(values, bins=HISTOGRAM_BINS)
    figure, axis = plt.subplots(figsize=(11, 6.5))
    axis.bar(edges[:-1], counts, width=np.diff(edges) * 0.9, align="edge", color="#4C78A8", edgecolor="white")
    axis.set_xlim(0, 100)
    axis.set_ylim(0, int(np.ceil(counts.max() * 1.12 / 10) * 10))
    axis.set_xticks(np.arange(0, 101, 10))
    axis.set_xlabel("Query Completion (%)")
    axis.set_ylabel("Query Count")
    axis.grid(axis="y", alpha=0.25, linewidth=0.7)
    axis.set_title(
        "ShortTerm Query Completion (S001-S010, 3 QA turns)\n"
        f"Mean {summary['mean']:.1%} | Median {summary['median']:.1%} | "
        f"100% Complete {summary['full_complete_query_rate']:.1%}"
    )
    figure.tight_layout()
    figure.savefig(path, dpi=220, bbox_inches="tight")
    plt.close(figure)


def render_summary(
    dataset_path: Path,
    dataset_stats: Mapping[str, Any],
    summary: Mapping[str, Any],
    comparison: Mapping[str, Any],
) -> str:
    return "\n".join(
        [
            "# S001-S010 Rebalanced Workbook: ShortTerm Completion Audit",
            "",
            f"- Dataset: `{dataset_path}`",
            f"- Queries: {dataset_stats['query_count']}",
            f"- Gold requirements: {dataset_stats['gold_requirement_count']}",
            f"- OR groups: {dataset_stats['or_requirement_count']}",
            f"- Gold-bearing Queries: {summary['query_count']}",
            f"- Queries without Gold: {summary['no_gold_query_count']}",
            "",
            "## ShortTerm (latest 3 QA turns)",
            "",
            f"- Requirement recall: {summary['requirement_hit_count']}/{summary['gold_requirement_count']} "
            f"({summary['requirement_recall']:.2%})",
            f"- Mean Query Completion: {summary['mean']:.2%}",
            f"- Median / P25 / P75: {summary['median']:.2%} / {summary['p25']:.2%} / {summary['p75']:.2%}",
            f"- 0% Complete: {summary['zero_complete_query_count']} ({summary['zero_complete_query_rate']:.2%})",
            f"- 100% Complete: {summary['full_complete_query_count']} ({summary['full_complete_query_rate']:.2%})",
            f"- Completion >=50%: {summary['completion_ge_50_rate']:.2%}",
            f"- Completion >=80%: {summary['completion_ge_80_rate']:.2%}",
            "",
            "## Frozen artifact compatibility",
            "",
            f"- Source QA matches existing frozen artifacts: {comparison['source_qa_matches_frozen_artifacts']}",
            f"- Changed Query inputs/answers: {comparison['changed_query_count']}",
            "- Existing frozen C3 rankings and LongTerm provenance cover only the prior dataset contract and cannot be "
            "reused for this workbook.",
            "- MidTerm, LongTerm, pairwise, and three-layer completion were intentionally not emitted. They require a "
            "new S001-S010 retrieval run on this exact workbook.",
        ]
    ) + "\n"


def main() -> int:
    args = parse_args()
    dataset_path = args.dataset.resolve()
    output_dir = args.output_dir.resolve()
    selected_sessions = load_v3_dataset(dataset_path, SESSION_CODES)
    frozen_sessions = load_v3_dataset(FROZEN_SOURCE_DATASET, SESSION_CODES)
    comparison = compare_source_qa(selected_sessions, frozen_sessions)
    if comparison["source_qa_matches_frozen_artifacts"]:
        raise AssertionError("Expected the rebalanced workbook to differ from the existing frozen source QA")
    dataset_stats = static_dataset_stats(selected_sessions, 3, 3)
    requirements, queries = build_shortterm_rows(selected_sessions)
    summary = aggregate_shortterm(dataset_stats, requirements, queries)
    sessions = session_rows(queries)

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "shortterm_requirement_results.csv", requirements)
    write_jsonl(output_dir / "shortterm_requirement_results.jsonl", requirements)
    write_csv(output_dir / "shortterm_query_completion.csv", queries)
    write_jsonl(output_dir / "shortterm_query_completion.jsonl", queries)
    write_csv(output_dir / "shortterm_session_summary.csv", sessions)
    (output_dir / "shortterm_completion_stats.json").write_text(
        json.dumps(
            {
                "dataset_stats": dataset_stats,
                "shortterm": summary,
                "frozen_artifact_compatibility": comparison,
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
                "mode": "shortterm_only_offline_audit",
                "dataset": str(dataset_path),
                "dataset_sha256": sha256_file(dataset_path),
                "frozen_source_dataset": str(FROZEN_SOURCE_DATASET),
                "frozen_source_dataset_sha256": sha256_file(FROZEN_SOURCE_DATASET),
                "new_llm_calls": 0,
                "new_embedding_count": 0,
                "session_rerun": False,
                "midterm_longterm_results_emitted": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / "shortterm_completion_summary.md").write_text(
        render_summary(dataset_path, dataset_stats, summary, comparison), encoding="utf-8"
    )
    plot_shortterm(output_dir / "shortterm_query_completion.png", queries, summary)
    print((output_dir / "shortterm_completion_summary.md").read_text(encoding="utf-8"))
    print(f"Results directory: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
