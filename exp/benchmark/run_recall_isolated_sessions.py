"""Run the existing recall benchmark with one isolated local-Qdrant runtime per Session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from exp.benchmark.benchmark_common import ensure_repo_root_on_path, load_dataset, load_json

REPO_ROOT = ensure_repo_root_on_path(Path(__file__))
DEFAULT_CONFIG = REPO_ROOT / "exp/benchmark/recall_rebalanced_s001_s010.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "exp/results/recall_rebalanced_s001_s010_isolated"
DEFAULT_RUNTIME_DIR = REPO_ROOT / "exp/runtime/recall_rebalanced_s001_s010_isolated"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run recall Sessions in independent local-Qdrant processes")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--runtime-dir", type=Path, default=DEFAULT_RUNTIME_DIR)
    parser.add_argument("--max-parallel", type=int, default=4)
    return parser.parse_args()


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def session_config(
    base: dict[str, Any],
    *,
    sheet_name: str,
    code: str,
    output_dir: Path,
    runtime_dir: Path,
) -> dict[str, Any]:
    config = json.loads(json.dumps(base))
    config["benchmark"]["run_name"] = f"recall_rebalanced_{code.lower()}"
    config["benchmark"]["output_dir"] = str(output_dir / code)
    config["dataset"]["include_sheets"] = [sheet_name]
    config["dataset"]["max_sessions"] = 1
    config["storage"]["runtime_dir"] = str(runtime_dir / code)
    config["storage"]["collection_name"] = f"recall_rebalanced_{code.lower()}"
    config["execution"]["session_concurrency"] = 1
    return config


def validate_run(result_dir: Path, expected_turns: int) -> dict[str, Any]:
    summary_path = result_dir / "recall_summary.json"
    if not summary_path.exists():
        raise RuntimeError(f"Missing recall summary: {summary_path}")
    summary = load_json(summary_path)
    actual_turns = int(summary.get("total_turns") or 0)
    failed_turns = int(summary.get("failed_turns") or 0)
    if actual_turns != expected_turns or failed_turns:
        raise RuntimeError(
            f"Incomplete recall run at {result_dir}: turns={actual_turns}/{expected_turns}, failed={failed_turns}"
        )
    return summary


async def run_one(
    semaphore: asyncio.Semaphore,
    *,
    config_path: Path,
    result_dir: Path,
    expected_turns: int,
) -> dict[str, Any]:
    async with semaphore:
        log_path = result_dir / "runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment.update({"PYTHONNOUSERSITE": "1", "MEM0_TELEMETRY": "False"})
        with log_path.open("w", encoding="utf-8") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "exp.benchmark.run_recall_benchmark",
                "--config",
                str(config_path),
                cwd=REPO_ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            return_code = await process.wait()
        if return_code:
            raise RuntimeError(f"Recall subprocess failed ({return_code}); see {log_path}")
        summary = validate_run(result_dir, expected_turns)
        return {
            "result_dir": str(result_dir),
            "turn_count": int(summary["total_turns"]),
            "evaluated_turns": int(summary["evaluated_turns"]),
            "elapsed_seconds": float(summary["effective"]["elapsed_seconds"]),
            "long": summary["long"],
            "mid": summary["mid"],
            "all": summary["all"],
        }


async def main() -> None:
    args = parse_args()
    config_path = args.config.resolve()
    output_dir = args.output_dir.resolve()
    runtime_dir = args.runtime_dir.resolve()
    base = load_json(config_path)
    dataset_path = (REPO_ROOT / str(base["dataset"]["path"])).resolve()
    sheet_names = list(base["dataset"]["include_sheets"])
    sessions = load_dataset(dataset_path, include_sheets=sheet_names)
    session_by_sheet = {session.sheet_name: session for session in sessions}
    if set(session_by_sheet) != set(sheet_names):
        raise RuntimeError("Configured Session sheets do not match the selected workbook")

    semaphore = asyncio.Semaphore(max(1, args.max_parallel))
    tasks = []
    for sheet_name in sheet_names:
        code = sheet_name.split("_", 1)[0]
        result_dir = output_dir / code
        child_config = session_config(
            base,
            sheet_name=sheet_name,
            code=code,
            output_dir=output_dir,
            runtime_dir=runtime_dir,
        )
        child_config_path = output_dir / "configs" / f"{code}.json"
        dump_json(child_config_path, child_config)
        tasks.append(
            run_one(
                semaphore,
                config_path=child_config_path,
                result_dir=result_dir,
                expected_turns=len(session_by_sheet[sheet_name].turns),
            )
        )
    results = await asyncio.gather(*tasks)
    result = {
        "mode": "isolated_session_subprocesses",
        "base_config": str(config_path),
        "dataset": str(dataset_path),
        "session_count": len(results),
        "total_turns": sum(row["turn_count"] for row in results),
        "failed_turns": 0,
        "sessions": results,
    }
    dump_json(output_dir / "isolated_run_summary.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
