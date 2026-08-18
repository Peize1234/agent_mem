from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_json, load_json
from .models import Dataset


def _session_weight(dataset: Dataset, session_id: str, shortterm_window: int) -> tuple[int, int, int]:
    turns = dataset.sessions[session_id]
    positions = {turn.query_id: turn.turn_index for turn in turns}
    eligible = 0
    long_distance = 0
    for turn in turns:
        recent = {item.query_id for item in turns[max(0, turn.turn_index - shortterm_window) : turn.turn_index]}
        for requirement in turn.requirements:
            eligible += int(not any(member in recent for member in requirement.members))
            long_distance += sum(
                int(member in positions and turn.turn_index - positions[member] > shortterm_window)
                for member in requirement.members
            )
    return eligible, long_distance, len(turns)


def create_or_load_split(
    dataset: Dataset,
    *,
    output_dir: Path,
    seed: int,
    shortterm_window: int,
) -> dict[str, Any]:
    path = output_dir / "split_manifest.json"
    if path.exists():
        existing = load_json(path)
        if existing.get("dataset_sha256") != dataset.sha256:
            raise ValueError("Existing split_manifest.json belongs to a different dataset")
        if int(existing.get("seed")) != seed:
            raise ValueError("Existing split_manifest.json uses a different seed")
        if int(existing.get("shortterm_qa_turns", -1)) != shortterm_window:
            raise ValueError("Existing split_manifest.json uses a different ShortTerm QA window")
        return existing

    session_ids = sorted(dataset.sessions)
    session_count = len(session_ids)
    randomizer = random.Random(seed)
    tie_breakers = {session_id: randomizer.random() for session_id in session_ids}
    ordered = sorted(
        session_ids,
        key=lambda session_id: (*_session_weight(dataset, session_id, shortterm_window), tie_breakers[session_id]),
        reverse=True,
    )
    weights = {
        session_id: {
            "eligible_requirements": _session_weight(dataset, session_id, shortterm_window)[0],
            "long_dependencies": _session_weight(dataset, session_id, shortterm_window)[1],
            "queries": _session_weight(dataset, session_id, shortterm_window)[2],
        }
        for session_id in session_ids
    }

    folds: list[dict[str, Any]] = []
    if session_count >= 8:
        validation_count = max(1, round(session_count * 0.30))
        # Greedy round-robin over difficulty-sorted Sessions approximates stratification.
        validation = sorted(
            ordered[index] for index in range(0, len(ordered), max(1, session_count // validation_count))
        )
        validation = validation[:validation_count]
        tune = sorted(set(session_ids) - set(validation))
        method = "deterministic_stratified_holdout"
        confidence = "standard"
    elif session_count >= 5:
        validation = sorted(ordered[:2])
        tune = sorted(set(session_ids) - set(validation))
        method = "deterministic_holdout"
        confidence = "reduced"
    elif session_count >= 3:
        method = "leave_one_session_out"
        folds = [
            {
                "fold": index + 1,
                "tune_sessions": sorted(set(session_ids) - {held_out}),
                "validation_sessions": [held_out],
            }
            for index, held_out in enumerate(ordered)
        ]
        # There is no privileged first fold. The orchestrator aggregates tune
        # and out-of-fold validation results across every frozen fold.
        tune = []
        validation = []
        confidence = "low_leave_one_session_out"
    else:
        method = "exploratory_only"
        tune = session_ids
        validation = []
        confidence = "exploratory_only"

    manifest = {
        "dataset_sha256": dataset.sha256,
        "seed": seed,
        "shortterm_qa_turns": shortterm_window,
        "unit": "session",
        "method": method,
        "confidence": confidence,
        "tune_sessions": tune,
        "validation_sessions": validation,
        "folds": folds,
        "session_stats": weights,
        "frozen": True,
    }
    if method != "leave_one_session_out" and set(tune) & set(validation):
        raise AssertionError("Tune and validation Sessions overlap")
    atomic_write_json(path, manifest)
    return manifest
