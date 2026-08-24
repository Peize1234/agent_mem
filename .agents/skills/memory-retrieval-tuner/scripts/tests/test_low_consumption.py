from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = next(path for path in Path(__file__).resolve().parents if (path / "pyproject.toml").exists())
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO_ROOT))

import tuner.generated_source_artifacts as generated_source_artifacts  # noqa: E402
from tuner.io_utils import sha256_file  # noqa: E402
from tuner.low_consumption import low_consumption_enabled, set_low_consumption_mode  # noqa: E402
from tuner.models import Candidate  # noqa: E402


def _load_run_tuner_module():
    path = SCRIPTS / "run_tuner.py"
    spec = importlib.util.spec_from_file_location("memory_retrieval_tuner_run_tuner", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_low_consumption_cli_disables_post_baseline_llm_paths() -> None:
    module = _load_run_tuner_module()
    try:
        config = module.parse_args(["dataset=exp/test.xlsx", "low_consumption=true"])
        assert low_consumption_enabled() is True
        assert config.overrides["search"]["low_consumption"]["enabled"] is True
        assert config.overrides["search"]["research"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["QueryRepresentation"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["QueryRewritePrompt"]["enabled"] is False
    finally:
        set_low_consumption_mode(False)


def test_low_consumption_reuses_baseline_source_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkpoint_path = tmp_path / "baseline_checkpoints.jsonl"
    checkpoint_path.write_text("", encoding="utf-8")
    baseline_manifest = tmp_path / "baseline_manifest.json"
    baseline_payload = {
        "schema": 8,
        "status": "COMPLETE",
        "session_id": "S001",
        "checkpoints_path": str(checkpoint_path),
        "checkpoints_sha256": sha256_file(checkpoint_path),
        "source_variant": "production",
        "stateful_replay": False,
        "effective_memory_config": {
            "midterm": {
                "top_k_pages": 5,
                "midterm_rag_threshold": 0.1,
                "page_summary_prompt": "baseline prompt",
            },
            "fine_grained_longterm": {},
        },
        "production_config": {
            "top_k_pages": 5,
            "midterm_rag_threshold": 0.1,
            "page_summary_prompt": "baseline prompt",
        },
    }
    baseline_manifest.write_text(json.dumps(baseline_payload), encoding="utf-8")

    candidate = Candidate(
        name="source-changing-candidate",
        stage="stage_2",
        config={
            "backend": "production_midterm",
            "manifest_paths": [str(baseline_manifest)],
            "manifest_sha256": {str(baseline_manifest.resolve()): sha256_file(baseline_manifest)},
            "midterm_rag_threshold": 0.2,
            "page_summary_prompt": "candidate prompt",
            "source_config_overrides": {"midterm": {"page_summary_prompt": "candidate prompt"}},
            "source_generation_spec": {
                "source_identity": {"kind": "candidate-source"},
                "stateful_replay": True,
            },
        },
        provenance={},
        complexity=2,
    )

    def fail_if_generated(**_kwargs):
        raise AssertionError("candidate-specific source generation must not run in low-consumption mode")

    monkeypatch.setattr(generated_source_artifacts, "generate_production_sources", fail_if_generated)
    set_low_consumption_mode(True)
    try:
        prepared = generated_source_artifacts.prepare_generated_source_candidate(
            candidate,
            sessions=["S001"],
            registry=object(),  # low-consumption path returns before registry/source generation is used
            run_dir=tmp_path / "run",
            ranking_depth=20,
            max_parallel_sessions=1,
            max_parallel_llm_calls=1,
            gpu_count=0,
        )
    finally:
        set_low_consumption_mode(False)

    assert prepared.config["low_consumption_mode"] is True
    assert prepared.provenance["deferred_generation_stats"]["llm_calls"] == 0
    assert prepared.provenance["deferred_generation_stats"]["embedding_calls"] == 0
    assert prepared.provenance["low_consumption_baseline_manifest_paths"] == [str(baseline_manifest.resolve())]

    synthetic_manifest = Path(prepared.config["manifest_paths"][0])
    synthetic_payload = json.loads(synthetic_manifest.read_text(encoding="utf-8"))
    assert synthetic_payload["checkpoints_path"] == str(checkpoint_path)
    assert synthetic_payload["effective_memory_config"]["midterm"]["midterm_rag_threshold"] == 0.2
    assert synthetic_payload["effective_memory_config"]["midterm"]["page_summary_prompt"] == "candidate prompt"
    assert synthetic_payload["low_consumption_reuse"]["approximate"] is True

    unchanged_baseline = json.loads(baseline_manifest.read_text(encoding="utf-8"))
    assert unchanged_baseline == baseline_payload
