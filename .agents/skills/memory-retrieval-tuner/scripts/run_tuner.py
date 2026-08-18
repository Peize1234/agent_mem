#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parents[1]
for candidate in SCRIPT_PATH.parents:
    if (candidate / "pyproject.toml").exists() and (candidate / "mem0").is_dir():
        REPO_ROOT = candidate
        break
else:
    raise RuntimeError("Cannot find repository root")
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPT_PATH.parent))

from tuner.dataset_audit import DatasetAuditFailed  # noqa: E402
from tuner.orchestrator import TunerConfig, run_tuning  # noqa: E402


KNOWN_KEYS = {
    "dataset",
    "k",
    "budget",
    "target",
    "sessions",
    "seed",
    "resume",
    "output_dir",
    "source_run",
    "memory_config",
    "llm_mode",
    "max_parallel_sessions",
    "max_parallel_candidates",
    "max_parallel_llm_calls",
}


def _parse_sessions(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    values: list[str] = []
    for item in value.split(","):
        item = item.strip().upper()
        if "-" in item:
            start, end = item.split("-", 1)
            if start.startswith("S") and end.startswith("S") and start[1:].isdigit() and end[1:].isdigit():
                values.extend(f"S{index:03d}" for index in range(int(start[1:]), int(end[1:]) + 1))
                continue
        values.append(item)
    return tuple(dict.fromkeys(values))


def _nested_override(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    cursor = target
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def parse_args(argv: list[str] | None = None) -> TunerConfig:
    parser = argparse.ArgumentParser(description="Audit and automatically tune memory retrieval")
    parser.add_argument("assignments", nargs="*", help="Skill-style key=value arguments")
    parser.add_argument("--dataset")
    parser.add_argument("--k", type=int)
    parser.add_argument("--budget")
    parser.add_argument("--target")
    parser.add_argument("--sessions")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--resume")
    parser.add_argument("--output-dir")
    parser.add_argument("--source-run")
    parser.add_argument("--memory-config")
    parser.add_argument("--llm-mode", choices=("real", "mock"))
    parser.add_argument("--max-parallel-sessions", type=int)
    parser.add_argument("--max-parallel-candidates", type=int)
    parser.add_argument("--max-parallel-llm-calls", type=int)
    namespace = parser.parse_args(argv)
    values: dict[str, Any] = {}
    overrides: dict[str, Any] = {}
    for assignment in namespace.assignments:
        if "=" not in assignment:
            parser.error(f"Expected key=value, got {assignment!r}")
        key, raw_value = assignment.split("=", 1)
        parsed_value = yaml.safe_load(raw_value)
        if key in KNOWN_KEYS:
            values[key] = parsed_value
        else:
            _nested_override(overrides, key, parsed_value)
    for key in KNOWN_KEYS:
        attribute = key
        value = getattr(namespace, attribute, None)
        if value is not None:
            values[key] = value
    if not values.get("dataset"):
        parser.error("dataset is required")
    dataset = Path(str(values["dataset"]))
    if not dataset.is_absolute():
        dataset = REPO_ROOT / dataset
    output_root = Path(str(values.get("output_dir") or "exp/results/auto_tuning"))
    if not output_root.is_absolute():
        output_root = REPO_ROOT / output_root
    resume = Path(str(values["resume"])) if values.get("resume") else None
    if resume and not resume.is_absolute():
        resume = REPO_ROOT / resume
    source_run = Path(str(values["source_run"])) if values.get("source_run") else None
    if source_run and not source_run.is_absolute():
        source_run = REPO_ROOT / source_run
    memory_config = Path(str(values.get("memory_config") or (SKILL_ROOT / "memory_config.json")))
    if not memory_config.is_absolute():
        memory_config = REPO_ROOT / memory_config
    return TunerConfig(
        dataset=dataset,
        k=int(values.get("k") or 5),
        budget=str(values.get("budget") or "standard"),
        target=str(values.get("target") or "midterm"),
        sessions=_parse_sessions(str(values.get("sessions") or "")),
        seed=int(values["seed"]) if values.get("seed") is not None else None,
        output_root=output_root,
        resume=resume,
        source_run=source_run,
        memory_config=memory_config,
        llm_mode=str(values.get("llm_mode") or "real"),
        max_parallel_sessions=(
            int(values["max_parallel_sessions"]) if values.get("max_parallel_sessions") is not None else None
        ),
        max_parallel_candidates=(
            int(values["max_parallel_candidates"]) if values.get("max_parallel_candidates") is not None else None
        ),
        max_parallel_llm_calls=(
            int(values["max_parallel_llm_calls"]) if values.get("max_parallel_llm_calls") is not None else None
        ),
        overrides=overrides,
    )


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    try:
        run_dir = run_tuning(config, skill_root=SKILL_ROOT)
    except DatasetAuditFailed:
        print("DATASET_AUDIT_FAILED", file=sys.stderr)
        return 2
    print(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
