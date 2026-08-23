from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

_THIS_SKILL_ROOT = Path(__file__).resolve().parents[1]
TUNER_SKILL_ROOT = _THIS_SKILL_ROOT.parent / "memory-retrieval-tuner"
TUNER_SCRIPTS = TUNER_SKILL_ROOT / "scripts"
if str(TUNER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(TUNER_SCRIPTS))

from tuner.dataset_audit import audit_dataset  # noqa: E402
from tuner.orchestrator import TunerConfig, run_tuning  # noqa: E402
from tuner.parameter_schema import production_overrides_from_candidate  # noqa: E402


@dataclass(frozen=True)
class TunerRunSummary:
    run_dir: Path
    audit_succeeded: bool
    validation_sufficient: bool
    best_candidate: str | None
    best_candidate_status: str | None
    best_candidate_overfit: bool
    baseline_metrics: dict[str, Any]
    best_validation_metrics: dict[str, Any]
    candidate_config: dict[str, Any]
    run_metadata: dict[str, Any]


def audit_generated_benchmark(
    dataset_path: str | Path,
    *,
    output_dir: str | Path,
    shortterm_qa_turns: int,
) -> dict[str, Any]:
    search_space = yaml.safe_load((TUNER_SKILL_ROOT / "search_space.yaml").read_text(encoding="utf-8"))
    _, audit = audit_dataset(
        Path(dataset_path),
        output_dir=Path(output_dir),
        shortterm_qa_turns=int(shortterm_qa_turns),
        warning_config=(search_space.get("dataset") or {}).get("quality_warnings", {}),
    )
    return audit


def run_existing_tuner(
    *,
    dataset_path: str | Path,
    output_root: str | Path,
    memory_config_path: str | Path | None,
    k: int,
    budget: str,
    target: str,
    seed: int | None,
    llm_mode: str,
    runner: Callable[..., Path] | None = None,
) -> Path:
    """Invoke the existing tuner Python API without reproducing its search."""

    config = TunerConfig(
        dataset=Path(dataset_path),
        k=int(k),
        budget=budget,
        target=target,
        seed=seed,
        output_root=Path(output_root),
        memory_config=Path(memory_config_path) if memory_config_path is not None else None,
        llm_mode=llm_mode,
    )
    selected_runner = runner or run_tuning
    return Path(selected_runner(config, skill_root=TUNER_SKILL_ROOT)).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _leaderboard_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def _baseline_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    baseline = next((row for row in rows if row.get("candidate") == "baseline"), None)
    if baseline is None:
        return {}
    metrics: dict[str, Any] = {}
    for key, value in baseline.items():
        if not key.startswith("validation_") or value in (None, ""):
            continue
        try:
            metrics[key] = float(value)
        except ValueError:
            metrics[key] = value
    return metrics


def inspect_tuner_run(run_dir: str | Path) -> TunerRunSummary:
    root = Path(run_dir).resolve()
    audit = _load_json(root / "dataset_audit.json")
    split = _load_json(root / "split_manifest.json")
    best = _load_json(root / "best_config.json")
    metadata = _load_json(root / "run_metadata.json")
    leaderboard = _leaderboard_rows(root / "leaderboard.csv")
    candidate = str(best.get("candidate") or "") or None
    candidate_row = next((row for row in leaderboard if row.get("candidate") == candidate), None)
    candidate_status = candidate_row.get("status") if candidate_row else None
    validation_metrics = best.get("validation_metrics")
    candidate_config = best.get("config")
    validation_sessions = list(split.get("validation_sessions") or [])
    validation_sufficient = bool(
        (validation_sessions or split.get("method") == "leave_one_session_out")
        and isinstance(validation_metrics, Mapping)
        and validation_metrics.get("recall_at_k") is not None
        and validation_metrics.get("macro_session_recall_at_k") is not None
    )
    return TunerRunSummary(
        run_dir=root,
        audit_succeeded=audit.get("status") in {"PASS", "DATASET_QUALITY_WARNING"} and not audit.get("hard_errors"),
        validation_sufficient=validation_sufficient,
        best_candidate=candidate,
        best_candidate_status=candidate_status,
        best_candidate_overfit=candidate_status == "OVERFIT",
        baseline_metrics=_baseline_metrics(leaderboard),
        best_validation_metrics=dict(validation_metrics) if isinstance(validation_metrics, Mapping) else {},
        candidate_config=dict(candidate_config) if isinstance(candidate_config, Mapping) else {},
        run_metadata=metadata,
    )


def selected_production_overrides(candidate_config: Mapping[str, Any]) -> dict[str, Any]:
    """Use the tuner's canonical Candidate-to-Production translation."""

    return production_overrides_from_candidate(candidate_config)
