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

    regression = dict(run_metadata.get("full_memory_regression") or {})
    regression_metrics = regression.get("metrics") if regression.get("status") == "AVAILABLE" else None

    def regression_metric(name: str) -> str:
        if not isinstance(regression_metrics, Mapping):
            return "N/A"
        return optional_metric(regression_metrics, name)

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
        f"- ShortTerm window: **{run_metadata.get('shortterm_qa_turns')} QA turns** "
        f"(`{(run_metadata.get('shortterm_window_validation') or {}).get('status')}`)",
        "",
        "## Split",
        "",
        f"- Method: `{split.get('method')}` ({split.get('confidence')})",
        f"- Tune Sessions: "
        f"{'fold-specific (N-1 Sessions per fold)' if split.get('method') == 'leave_one_session_out' else ', '.join(split.get('tune_sessions') or [])}",
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
        f"- MidTerm winner R@{k}: {optional_metric(best.metrics, 'midterm_recall_at_k')}",
        f"- Full-memory regression: `{regression.get('status') or 'SKIPPED'}` (backend: `{regression.get('backend')}`)",
        f"- Regression ShortTerm coverage: {regression_metric('shortterm_coverage')}",
        f"- Regression LongTerm R@{k}: {regression_metric('longterm_recall_at_k')}",
        f"- Regression Short+Mid union: {regression_metric('target_layer_union')}",
        f"- Regression All-memory R@{k} / query completion: "
        f"{regression_metric('all_memory_union')} / {regression_metric('query_completion')}",
        f"- Regression skipped reason: {regression.get('reason') or 'N/A'}",
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
    stage_history = list(run_metadata.get("stage_history") or [])
    lines.extend(
        (
            "",
            "## Staged successive filtering",
            "",
            f"- Executed stages: {len(stage_history)}",
            f"- Stop reason: `{stop_reason}`",
            "",
            f"| Stage | Branches | Candidates | Best Tune R@{k} | Improvement pp | Diagnostic after |",
            "|---:|---|---:|---:|---:|---|",
        )
    )
    for stage in stage_history:
        lines.append(
            f"| {stage.get('stage_index')} | {', '.join(stage.get('branches') or [])} | "
            f"{stage.get('candidate_count', 0)} | "
            f"{float(stage.get('best_recall_at_k') or 0):.4f} | "
            f"{float(stage.get('improvement_pp') or 0):+.2f} | "
            f"{(stage.get('diagnostic_after') or {}).get('regime') or stage.get('status')} |"
        )
    branch_events = list(run_metadata.get("branch_events") or [])
    lines.extend(("", "## Experiment Branches", ""))
    for event in branch_events:
        lines.append(
            f"- Stage {event.get('stage_index')} `{event.get('branch')}`: "
            f"**{event.get('status')}**, candidates={event.get('candidate_count', 0)}"
            + (f", reason={event.get('reason')}" if event.get("reason") else "")
        )
    if not branch_events:
        lines.append("- None")
    coverage = dict(run_metadata.get("branch_coverage") or {})
    lines.extend(
        (
            "",
            "## Diagnostics and conditional search",
            "",
            f"- Classification: `{diagnostics.get('regime')}`",
            f"- Evidence: `{dict(diagnostics)}`",
            f"- Relevant branches: `{coverage.get('relevant_branches', [])}`",
            f"- Attempted branches: `{coverage.get('attempted_branches', {})}`",
            f"- Exhausted branches: `{coverage.get('exhausted_branches', [])}`",
            f"- Remaining branches: `{coverage.get('remaining_branches', [])}`",
            f"- Budget/resource blocked branches: `{coverage.get('blocked_branches', [])}`",
            f"- Relevant branch coverage complete: `{coverage.get('relevant_coverage_complete')}`",
            f"- Skipped branches: `{list(skipped_branches)}`",
            f"- Stop reason: `{stop_reason}`",
            f"- OVERFIT configs: `{list(overfit)}`",
            "",
            "## Cost, cache, and parallel execution",
            "",
            f"- Cumulative LLM calls: {run_metadata.get('llm_calls', 0)}",
            f"- Cumulative embedding calls: {run_metadata.get('embedding_calls', 0)}",
            f"- Source generation LLM / embedding calls: "
            f"{run_metadata.get('source_generation_llm_calls', 0)} / "
            f"{run_metadata.get('source_generation_embedding_calls', 0)}",
            f"- Branch generation LLM / embedding calls: "
            f"{run_metadata.get('branch_generation_llm_calls', 0)} / "
            f"{run_metadata.get('branch_generation_embedding_calls', 0)}",
            f"- Reused artifacts: {run_metadata.get('reused_artifacts', [])}",
            f"- Model discovery: `{run_metadata.get('model_discovery_path')}`",
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
            f"- MidTerm baseline backend: `{run_metadata.get('midterm_baseline_backend')}`; "
            "winner selection uses only production-checkpoint MidTerm validation.",
            f"- Full-memory regression baseline backend: "
            f"`{run_metadata.get('full_memory_regression_baseline_backend')}`; it never enters winner selection.",
            f"- MidTerm baseline prompt provenance: "
            f"`{(run_metadata.get('midterm_baseline_provenance') or {}).get('prompt_provenance') or 'explicit prompt hashes validated'}`.",
            "- Frozen rankings are eligible only when dataset provenance and the `production_midterm_v1` retrieval contract validate.",
            "- Query/Page/embedding variants are derived from production MidTerm checkpoints and remain explicitly marked as Benchmark candidates, not production behavior.",
            "- Query Prompt generation requires standard/deep budget; Add/Page Prompt generation requires deep budget. Missing API/model resources are recorded as unavailable rather than replaced by surrogate artifacts.",
        )
    )
    atomic_write_text(run_dir / "final_report.md", "\n".join(lines) + "\n")
