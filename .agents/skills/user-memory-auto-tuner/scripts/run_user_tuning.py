#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

SCRIPT_PATH = Path(__file__).resolve()
for candidate in SCRIPT_PATH.parents:
    if (candidate / "pyproject.toml").exists() and (candidate / "mem0").is_dir():
        REPO_ROOT = candidate
        break
else:
    raise RuntimeError("Cannot find repository root")
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPT_PATH.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_PATH.parent))

from build_benchmark import build_benchmark  # noqa: E402
from config_store import (  # noqa: E402
    UserTuningConfigStore,
    deep_merge,
    partition_production_overrides,
    production_override_delta,
    validate_production_overrides,
)
from extract_user_history import extract_user_history  # noqa: E402
from label_dependencies import TwoStageDependencyAnnotator, annotate_history  # noqa: E402
from tuner_bridge import (  # noqa: E402
    audit_generated_benchmark,
    inspect_tuner_run,
    run_existing_tuner,
    selected_production_overrides,
)


@dataclass(frozen=True)
class UserTuningRequest:
    user_id: str
    history_db_path: Path
    output_dir: Path
    labeler_memory_config: Path | None = None
    k: int = 5
    budget: str = "standard"
    target: str = "midterm"
    seed: int | None = None
    llm_mode: str = "real"
    min_sessions: int = 3
    min_gold_sessions: int = 3
    min_valid_dependency_samples: int = 3
    min_retrieval_required_samples: int = 3
    min_retrieval_required_sessions: int = 3
    label_min_confidence: float = 0.75
    verifier_min_confidence: float = 0.75


def _read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"configuration must contain a JSON object: {path}")
    return value


def _create_annotator(
    request: UserTuningRequest,
    *,
    active_overrides: Mapping[str, Any],
) -> TwoStageDependencyAnnotator:
    from mem0.configs.production import load_production_memory_config
    from mem0.utils.factory import LlmFactory

    overrides = dict(active_overrides)
    if request.labeler_memory_config is not None:
        overrides = deep_merge(overrides, _read_json_object(request.labeler_memory_config))
    config = load_production_memory_config(overrides or None, resolve_environment=True)
    llm = LlmFactory.create(
        config.llm.provider,
        config.llm.config,
        timeout_seconds=config.llm_timeout_seconds,
    )
    return TwoStageDependencyAnnotator(
        llm,
        label_min_confidence=request.label_min_confidence,
        verifier_min_confidence=request.verifier_min_confidence,
    )


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _base_result(request: UserTuningRequest) -> dict[str, Any]:
    return {
        "user_id": request.user_id,
        "extracted_session_count": 0,
        "qa_turn_count": 0,
        "valid_dependency_samples": 0,
        "retrieval_required_samples": 0,
        "retrieval_required_sessions": 0,
        "shortterm_only_dependency_samples": 0,
        "shortterm_qa_turns": None,
        "filtered_samples": 0,
        "generated_benchmark_path": None,
        "benchmark_manifest_path": None,
        "dataset_hash": None,
        "benchmark_audit_status": None,
        "tuner_run_dir": None,
        "baseline_metrics": {},
        "best_validation_metrics": {},
        "production_overrides": {},
        "rebuild_required": {},
        "future_apply_notes": {},
        "unsupported_overrides": {},
        "override_classification_reasons": {},
        "override_exclusion_reasons": {},
        "config_version": None,
        "status": "failed",
        "reason": None,
        "cross_session_tuning_status": "CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD",
        "deployment_scope": "SQLITE_RECOMMENDATION_TABLE_ONLY_NOT_LIVE_MEMORY",
        "saved": False,
        "deployed": False,
        "deployed_meaning": "recommended config saved to SQLite; not active in live Memory",
    }


def _finish(request: UserTuningRequest, result: dict[str, Any]) -> dict[str, Any]:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(request.output_dir / "result.json", result)
    return result


def _not_enough_data_reason(
    *,
    session_count: int,
    gold_session_count: int,
    valid_dependency_samples: int,
    request: UserTuningRequest,
) -> dict[str, Any] | None:
    if (
        session_count >= request.min_sessions
        and gold_session_count >= request.min_gold_sessions
        and valid_dependency_samples >= request.min_valid_dependency_samples
    ):
        return None
    return {
        "code": "NOT_ENOUGH_DATA",
        "actual": {
            "sessions": session_count,
            "gold_sessions": gold_session_count,
            "valid_dependency_samples": valid_dependency_samples,
        },
        "required": {
            "sessions": request.min_sessions,
            "gold_sessions": request.min_gold_sessions,
            "valid_dependency_samples": request.min_valid_dependency_samples,
        },
    }


def _not_enough_retrieval_data_reason(
    *,
    retrieval_required_samples: int,
    retrieval_required_sessions: int,
    shortterm_only_dependency_samples: int,
    shortterm_qa_turns: int,
    request: UserTuningRequest,
) -> dict[str, Any] | None:
    if (
        retrieval_required_samples >= request.min_retrieval_required_samples
        and retrieval_required_sessions >= request.min_retrieval_required_sessions
    ):
        return None
    return {
        "code": "NOT_ENOUGH_RETRIEVAL_DATA",
        "actual": {
            "retrieval_required_samples": retrieval_required_samples,
            "retrieval_required_sessions": retrieval_required_sessions,
            "shortterm_only_dependency_samples": shortterm_only_dependency_samples,
            "shortterm_qa_turns": shortterm_qa_turns,
        },
        "required": {
            "retrieval_required_samples": request.min_retrieval_required_samples,
            "retrieval_required_sessions": request.min_retrieval_required_sessions,
        },
    }


def _save_gate_failure(summary: Any) -> str | None:
    if not summary.audit_succeeded:
        return "DATASET_AUDIT_NOT_SUCCESSFUL"
    if not summary.validation_sufficient:
        return "VALIDATION_NOT_SUFFICIENT"
    if summary.run_metadata.get("status") != "COMPLETE":
        return "TUNER_NOT_COMPLETE"
    if int(summary.run_metadata.get("failed_turns") or 0) != 0:
        return "TUNER_HAS_FAILED_TURNS"
    if not summary.best_candidate:
        return "BEST_CANDIDATE_MISSING"
    if summary.best_candidate_overfit:
        return "BEST_CANDIDATE_OVERFIT"
    if summary.best_candidate_status is None:
        return "BEST_CANDIDATE_VALIDATION_ROW_MISSING"
    if summary.best_candidate_status != "VALID":
        return f"BEST_CANDIDATE_{summary.best_candidate_status}"
    if not summary.candidate_config:
        return "BEST_CANDIDATE_CONFIG_MISSING"
    return None


def run_user_tuning(
    request: UserTuningRequest,
    *,
    annotator: TwoStageDependencyAnnotator | None = None,
    tuner_runner: Callable[..., Path] | None = None,
) -> dict[str, Any]:
    request.output_dir.mkdir(parents=True, exist_ok=True)
    result = _base_result(request)
    store = UserTuningConfigStore(request.history_db_path)
    active = None
    try:
        active = store.get_active(request.user_id)
        active_overrides = active.config_overrides if active is not None else {}
        result["config_version"] = active.config_version if active is not None else None

        from mem0.configs.production import load_production_memory_config

        baseline = load_production_memory_config(active_overrides or None, resolve_environment=False)
        shortterm_qa_turns = int(baseline.midterm.short_term_capacity) // 2
        result["shortterm_qa_turns"] = shortterm_qa_turns
        history = extract_user_history(
            user_id=request.user_id,
            history_db_path=request.history_db_path,
            output_dir=request.output_dir,
        )
        result["extracted_session_count"] = len(history.sessions)
        result["qa_turn_count"] = history.qa_turn_count
        selected_annotator = annotator or _create_annotator(request, active_overrides=active_overrides)
        annotations = annotate_history(history, selected_annotator, output_dir=request.output_dir)
        artifacts = build_benchmark(
            annotations,
            output_dir=request.output_dir,
            history_db_path=request.history_db_path,
            shortterm_qa_turns=shortterm_qa_turns,
        )
        result.update(
            {
                "valid_dependency_samples": artifacts.valid_dependency_samples,
                "retrieval_required_samples": artifacts.retrieval_required_samples,
                "retrieval_required_sessions": artifacts.retrieval_required_sessions,
                "shortterm_only_dependency_samples": artifacts.shortterm_only_dependency_samples,
                "filtered_samples": artifacts.filtered_samples,
                "generated_benchmark_path": str(artifacts.benchmark_path),
                "benchmark_manifest_path": str(artifacts.manifest_path),
                "dataset_hash": artifacts.dataset_hash,
            }
        )
        gold_session_count = sum(
            any(turn.status == "VALID" and turn.label.needs_history for turn in session.turns)
            for session in annotations.sessions
        )
        not_enough = _not_enough_data_reason(
            session_count=len(history.sessions),
            gold_session_count=gold_session_count,
            valid_dependency_samples=artifacts.valid_dependency_samples,
            request=request,
        )
        if not_enough is not None:
            result.update({"status": "skipped", "reason": not_enough})
            return _finish(request, result)
        not_enough_retrieval = _not_enough_retrieval_data_reason(
            retrieval_required_samples=artifacts.retrieval_required_samples,
            retrieval_required_sessions=artifacts.retrieval_required_sessions,
            shortterm_only_dependency_samples=artifacts.shortterm_only_dependency_samples,
            shortterm_qa_turns=shortterm_qa_turns,
            request=request,
        )
        if not_enough_retrieval is not None:
            result.update({"status": "skipped", "reason": not_enough_retrieval})
            return _finish(request, result)

        audit = audit_generated_benchmark(
            artifacts.benchmark_path,
            output_dir=request.output_dir / "benchmark_audit",
            shortterm_qa_turns=shortterm_qa_turns,
        )
        result["benchmark_audit_status"] = audit.get("status")
        baseline_config_path = request.output_dir / "baseline_memory_overrides.json"
        _write_json(baseline_config_path, active_overrides)
        tuner_run_dir = run_existing_tuner(
            dataset_path=artifacts.benchmark_path,
            output_root=request.output_dir / "tuner",
            memory_config_path=baseline_config_path if active_overrides else None,
            k=request.k,
            budget=request.budget,
            target=request.target,
            seed=request.seed,
            llm_mode=request.llm_mode,
            runner=tuner_runner,
        )
        result["tuner_run_dir"] = str(tuner_run_dir)
        summary = inspect_tuner_run(tuner_run_dir)
        result["baseline_metrics"] = summary.baseline_metrics
        result["best_validation_metrics"] = summary.best_validation_metrics
        gate_failure = _save_gate_failure(summary)
        if gate_failure is not None:
            result.update({"status": "skipped", "reason": {"code": gate_failure}})
            return _finish(request, result)

        candidate_overrides = selected_production_overrides(summary.candidate_config)
        frozen_config_path = tuner_run_dir / "resolved_memory_config.json"
        if frozen_config_path.exists():
            candidate_overrides = production_override_delta(
                candidate_overrides,
                _read_json_object(frozen_config_path),
            )
        final_overrides = deep_merge(active_overrides, candidate_overrides)
        try:
            final_overrides = validate_production_overrides(final_overrides)
        except Exception as exc:
            result.update(
                {
                    "status": "skipped",
                    "reason": {
                        "code": "INVALID_PRODUCTION_OVERRIDES",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
            return _finish(request, result)
        partition = partition_production_overrides(final_overrides)
        result["rebuild_required"] = partition.rebuild_required
        result["unsupported_overrides"] = partition.unsupported
        result["override_classification_reasons"] = partition.exclusion_reasons
        result["override_exclusion_reasons"] = {
            path: reason
            for path, reason in partition.exclusion_reasons.items()
            if reason == "CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD"
        }
        result["future_apply_notes"] = {
            path: reason for path, reason in partition.exclusion_reasons.items() if reason == "REBUILD_REQUIRED"
        }
        if partition.unsupported:
            result.update(
                {
                    "status": "skipped",
                    "reason": {"code": "CROSS_SESSION_TUNING_UNSUPPORTED_NO_GOLD"},
                }
            )
            return _finish(request, result)
        if not final_overrides:
            result.update({"status": "skipped", "reason": {"code": "NO_RECOMMENDED_OVERRIDES"}})
            return _finish(request, result)
        if active is not None and final_overrides == active.config_overrides:
            result["production_overrides"] = final_overrides
            result.update({"status": "skipped", "reason": {"code": "NO_CONFIG_CHANGE"}})
            return _finish(request, result)

        saved = store.save_active(
            user_id=request.user_id,
            config_overrides=final_overrides,
            source_run_dir=tuner_run_dir,
            dataset_hash=artifacts.dataset_hash,
            validation_metrics=summary.best_validation_metrics,
        )
        result.update(
            {
                "production_overrides": saved.config_overrides,
                "config_version": saved.config_version,
                "status": "saved",
                "reason": None,
                "saved": True,
                "deployed": True,
            }
        )
        return _finish(request, result)
    except Exception as exc:
        result.update(
            {
                "status": "failed",
                "reason": {"code": type(exc).__name__, "error": str(exc)},
                "config_version": active.config_version if active is not None else result.get("config_version"),
            }
        )
        return _finish(request, result)


def _default_history_db_path() -> Path:
    from mem0.configs.production import load_production_memory_config

    return Path(load_production_memory_config(resolve_environment=False).history_db_path)


def _default_output_dir(user_id: str) -> Path:
    safe_user = re.sub(r"[^A-Za-z0-9_.-]+", "_", user_id)[:80] or "user"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return REPO_ROOT / "exp/results/user_memory_auto_tuning" / safe_user / stamp


def parse_args(argv: list[str] | None = None) -> UserTuningRequest:
    parser = argparse.ArgumentParser(description="Offline per-user memory retrieval tuning Demo")
    parser.add_argument("assignments", nargs="*", help="Skill-style key=value arguments")
    parser.add_argument("--user-id")
    parser.add_argument("--history-db-path")
    parser.add_argument("--output-dir")
    parser.add_argument("--labeler-memory-config")
    parser.add_argument("--k", type=int)
    parser.add_argument("--budget")
    parser.add_argument("--target")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--llm-mode", choices=("real", "mock"))
    parser.add_argument("--min-sessions", type=int)
    parser.add_argument("--min-gold-sessions", type=int)
    parser.add_argument("--min-valid-dependency-samples", type=int)
    parser.add_argument("--min-retrieval-required-samples", type=int)
    parser.add_argument("--min-retrieval-required-sessions", type=int)
    parser.add_argument("--label-min-confidence", type=float)
    parser.add_argument("--verifier-min-confidence", type=float)
    namespace = parser.parse_args(argv)
    values: dict[str, Any] = {}
    string_keys = {
        "user_id",
        "history_db_path",
        "output_dir",
        "labeler_memory_config",
        "budget",
        "target",
        "llm_mode",
    }
    for assignment in namespace.assignments:
        if "=" not in assignment:
            parser.error(f"Expected key=value, got {assignment!r}")
        key, raw = assignment.split("=", 1)
        values[key] = raw if key in string_keys else yaml.safe_load(raw)
    for key in (
        "user_id",
        "history_db_path",
        "output_dir",
        "labeler_memory_config",
        "k",
        "budget",
        "target",
        "seed",
        "llm_mode",
        "min_sessions",
        "min_gold_sessions",
        "min_valid_dependency_samples",
        "min_retrieval_required_samples",
        "min_retrieval_required_sessions",
        "label_min_confidence",
        "verifier_min_confidence",
    ):
        value = getattr(namespace, key, None)
        if value is not None:
            values[key] = value
    user_id = str(values.get("user_id") or "").strip()
    if not user_id:
        parser.error("user_id is required")

    def path_value(name: str, default: Path | None = None) -> Path | None:
        raw = values.get(name)
        if raw is None:
            return default
        path = Path(str(raw)).expanduser()
        return path if path.is_absolute() else REPO_ROOT / path

    history_db_path = path_value("history_db_path", _default_history_db_path())
    output_dir = path_value("output_dir", _default_output_dir(user_id))
    if history_db_path is None or output_dir is None:
        raise AssertionError("default paths must be available")
    return UserTuningRequest(
        user_id=user_id,
        history_db_path=history_db_path,
        output_dir=output_dir,
        labeler_memory_config=path_value("labeler_memory_config"),
        k=int(values.get("k") or 5),
        budget=str(values.get("budget") or "standard"),
        target=str(values.get("target") or "midterm"),
        seed=int(values["seed"]) if values.get("seed") is not None else None,
        llm_mode=str(values.get("llm_mode") or "real"),
        min_sessions=int(values.get("min_sessions", 3)),
        min_gold_sessions=int(values.get("min_gold_sessions", 3)),
        min_valid_dependency_samples=int(values.get("min_valid_dependency_samples", 3)),
        min_retrieval_required_samples=int(values.get("min_retrieval_required_samples", 3)),
        min_retrieval_required_sessions=int(values.get("min_retrieval_required_sessions", 3)),
        label_min_confidence=float(values.get("label_min_confidence") or 0.75),
        verifier_min_confidence=float(values.get("verifier_min_confidence") or 0.75),
    )


def main(argv: list[str] | None = None) -> int:
    request = parse_args(argv)
    result = run_user_tuning(request)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"saved", "deployed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
