from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from .io_utils import atomic_write_json, atomic_write_text, write_csv, write_jsonl
from .models import CandidateResult


def config_diff(baseline: Mapping[str, Any], best: Mapping[str, Any]) -> dict[str, Any]:
    keys = sorted(set(baseline) | set(best))
    return {
        key: {"baseline": baseline.get(key), "best": best.get(key)}
        for key in keys
        if baseline.get(key) != best.get(key)
    }


def _leaderboard_rows(
    tune_results: Sequence[CandidateResult],
    validation_by_name: Mapping[str, CandidateResult],
    *,
    k: int,
) -> list[dict[str, Any]]:
    rows = []
    for tune in tune_results:
        validation = validation_by_name.get(tune.name)
        rows.append(
            {
                "candidate": tune.name,
                "stage": tune.stage,
                f"tune_R@{k}": tune.metrics.get("recall_at_k"),
                f"validation_R@{k}": validation.metrics.get("recall_at_k") if validation else None,
                f"validation_macro_R@{k}": validation.metrics.get("macro_session_recall_at_k") if validation else None,
                f"validation_R@{2 * k}": validation.metrics.get("recall_at_2k") if validation else None,
                f"validation_R@{4 * k}": validation.metrics.get("recall_at_4k") if validation else None,
                "validation_MRR": validation.metrics.get("mrr") if validation else None,
                "session_stddev": validation.metrics.get("session_stddev") if validation else None,
                "LLM_calls": tune.llm_calls + (validation.llm_calls if validation else 0),
                "embedding_calls": tune.embedding_calls + (validation.embedding_calls if validation else 0),
                "runtime_seconds": tune.runtime_seconds + (validation.runtime_seconds if validation else 0),
                "status": validation.status if validation else "NOT_VALIDATED",
            }
        )
    return rows


def write_outputs(
    *,
    run_dir: Path,
    dataset_audit: Mapping[str, Any],
    split: Mapping[str, Any],
    k: int,
    baseline_tune: CandidateResult,
    baseline_validation: CandidateResult,
    tune_results: Sequence[CandidateResult],
    validation_by_name: Mapping[str, CandidateResult],
    best: CandidateResult,
    selection: Mapping[str, Any],
    overfit: Sequence[str],
    skipped_branches: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
    stop_reason: str,
    run_metadata: dict[str, Any],
) -> None:
    leaderboard = _leaderboard_rows(tune_results, validation_by_name, k=k)
    leaderboard.sort(key=lambda row: (-(row.get(f"validation_R@{k}") or -1), row["candidate"]))
    write_csv(run_dir / "leaderboard.csv", leaderboard)
    atomic_write_json(
        run_dir / "best_config.json",
        {
            "candidate": best.name,
            "config": best.config,
            "validation_metrics": best.metrics,
            "selection": dict(selection),
        },
    )
    atomic_write_json(run_dir / "best_config_diff.json", config_diff(baseline_tune.config, best.config))
    write_jsonl(run_dir / "requirement_results.jsonl", best.requirement_rows)
    write_csv(run_dir / "session_metrics.csv", best.session_rows)
    atomic_write_json(run_dir / "run_metadata.json", run_metadata)

    baseline_metric = float(baseline_validation.metrics.get("recall_at_k") or 0.0)
    best_metric = float(best.metrics.get("recall_at_k") or 0.0)
    improvement_pp = (best_metric - baseline_metric) * 100.0
    tune_rows = {result.name: result for result in tune_results}

    def optional_metric(metrics: Mapping[str, Any], name: str) -> str:
        value = metrics.get(name)
        return "N/A" if value is None else f"{float(value):.4f}"

    def displayed(validation: CandidateResult | None, metric: str) -> str:
        return f"{float(validation.metrics.get(metric) or 0):.4f}" if validation else "—"

    lines = [
        "# Memory Retrieval Tuning Report",
        "",
        "## Dataset",
        "",
        f"- Path: `{dataset_audit.get('dataset')}`",
        f"- SHA-256: `{dataset_audit.get('dataset_sha256')}`",
        f"- Sessions / Queries / requirements: {dataset_audit.get('session_count')} / "
        f"{dataset_audit.get('query_count')} / {dataset_audit.get('gold_requirement_count')}",
        f"- Audit: **{dataset_audit.get('status')}**",
        f"- Primary metric: **R@{k}**",
        f"- Deeper diagnostics: **R@{2 * k} / R@{4 * k}**",
        "",
        "## Split",
        "",
        f"- Method: `{split.get('method')}` ({split.get('confidence')})",
        f"- Tune Sessions: {', '.join(split.get('tune_sessions') or [])}",
        f"- Validation Sessions: "
        f"{'every Session held out once across LOSO folds' if split.get('method') == 'leave_one_session_out' else ', '.join(split.get('validation_sessions') or []) or 'none (exploratory)'}",
        "",
        "## Baseline and best stable configuration",
        "",
        f"- Baseline validation R@{k}: {baseline_metric:.4f}",
        f"- Best stable: **{best.name}**",
        f"- Best validation R@{k}: {best_metric:.4f}",
        f"- Improvement: **{improvement_pp:+.2f} pp**",
        f"- R@{2 * k} / R@{4 * k}: {float(best.metrics.get('recall_at_2k') or 0):.4f} / "
        f"{float(best.metrics.get('recall_at_4k') or 0):.4f}",
        f"- MRR: {float(best.metrics.get('mrr') or 0):.4f}",
        f"- Macro R@{k} / session stddev: {float(best.metrics.get('macro_session_recall_at_k') or 0):.4f} / "
        f"{float(best.metrics.get('session_stddev') or 0):.4f}",
        "",
        "## Memory-layer metrics",
        "",
        f"- ShortTerm coverage: {float(best.metrics.get('shortterm_coverage') or 0):.4f}",
        f"- MidTerm R@{k}: {optional_metric(best.metrics, 'midterm_recall_at_k')}",
        f"- LongTerm R@{k}: {optional_metric(best.metrics, 'longterm_recall_at_k')} "
        f"({best.metrics.get('longterm_metric_source') or 'candidate'})",
        f"- Short+target union: {optional_metric(best.metrics, 'target_layer_union')}",
        f"- All-memory R@{k} / query completion: {optional_metric(best.metrics, 'all_memory_union')} / "
        f"{optional_metric(best.metrics, 'query_completion')}",
        f"- Production baseline layer metrics: `{run_metadata.get('baseline_full_metrics')}`",
        "",
        "## Tune and validation finalists",
        "",
        f"| Candidate | Tune R@{k} | Validation R@{k} | Macro R@{k} | R@{2 * k} | R@{4 * k} | MRR | Status |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in leaderboard:
        validation = validation_by_name.get(str(row["candidate"]))
        tune = tune_rows[str(row["candidate"])]
        lines.append(
            f"| {row['candidate']} | {float(tune.metrics.get('recall_at_k') or 0):.4f} | "
            f"{displayed(validation, 'recall_at_k')} | "
            f"{displayed(validation, 'macro_session_recall_at_k')} | "
            f"{displayed(validation, 'recall_at_2k')} | "
            f"{displayed(validation, 'recall_at_4k')} | "
            f"{displayed(validation, 'mrr')} | "
            f"{validation.status if validation else 'NOT_VALIDATED'} |"
        )
    lines.extend(
        (
            "",
            "## Diagnostics and conditional search",
            "",
            f"- Classification: `{diagnostics.get('regime')}`",
            f"- Evidence: `{dict(diagnostics)}`",
            f"- Skipped branches: `{list(skipped_branches)}`",
            f"- Stop reason: `{stop_reason}`",
            f"- OVERFIT configs: `{list(overfit)}`",
            "",
            "## Cost, cache, and parallel execution",
            "",
            f"- New LLM calls: {run_metadata.get('llm_calls', 0)}",
            f"- Embedding calls: {run_metadata.get('embedding_calls', 0)}",
            f"- Reused artifacts: {run_metadata.get('reused_artifacts', [])}",
            f"- Runtime: {float(run_metadata.get('runtime_seconds') or 0):.3f}s",
            f"- Actual concurrency: `{run_metadata.get('execution')}`",
            f"- Estimated serial work: {float(run_metadata.get('estimated_serial_work_seconds') or 0):.3f}s",
            f"- Estimated parallel time saved: {float(run_metadata.get('parallel_time_saved_seconds') or 0):.3f}s",
            "",
            "## Dataset warnings",
            "",
        )
    )
    warnings = list(dataset_audit.get("warnings") or [])
    lines.extend(f"- `{item.get('code')}`: {item}" for item in warnings)
    if not warnings:
        lines.append("- None")
    lines.extend(
        (
            "",
            "## Limitations",
            "",
            f"- Baseline backend: `{run_metadata.get('baseline_backend')}`. Exact complete production traces are preferred; "
            "the source-turn BM25 adapter is used only as a fallback/candidate.",
            "- Frozen rankings are compared only when dataset hash and source IDs validate; incomplete artifacts are excluded.",
            "- New LLM prompt generation is intentionally deferred and skipped unless a repository-native generator with matching provenance is available.",
        )
    )
    atomic_write_text(run_dir / "final_report.md", "\n".join(lines) + "\n")
