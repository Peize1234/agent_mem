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
import tuner.low_consumption as low_consumption  # noqa: E402
import tuner.model_discovery as model_discovery  # noqa: E402
import tuner.production_midterm_adapter as production_midterm_adapter  # noqa: E402
from tuner.io_utils import sha256_file  # noqa: E402
from tuner.low_consumption import low_consumption_enabled, set_low_consumption_mode  # noqa: E402
from tuner.model_discovery import ModelCandidate, ModelDiscovery, ResourceEnvelope  # noqa: E402
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
        assert "research" not in config.overrides["search"]
        assert config.overrides["search"]["branch_registry"]["QueryRepresentation"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["QueryRewritePrompt"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["MidtermSourceConfig"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["MidtermPageSummaryPrompt"]["enabled"] is False
        assert config.overrides["search"]["branch_registry"]["MidtermSessionMergePrompt"]["enabled"] is False
        assert (
            config.overrides["search"]["branch_registry"]["FineGrainedLongtermExtractionPrompt"]["enabled"]
            is False
        )
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
            "experiment_branch": "PageRepresentation",
            "page_representation": "summary",
            "source_config_overrides": {"midterm": {"page_representation": "summary"}},
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
    assert prepared.config["low_consumption_local_replay_modes"] == ["page_representation_embedding"]
    assert prepared.provenance["deferred_generation_stats"]["llm_calls"] == 0
    assert prepared.provenance["deferred_generation_stats"]["embedding_calls"] == 0
    assert prepared.provenance["low_consumption_baseline_manifest_paths"] == [str(baseline_manifest.resolve())]

    synthetic_manifest = Path(prepared.config["manifest_paths"][0])
    synthetic_payload = json.loads(synthetic_manifest.read_text(encoding="utf-8"))
    assert synthetic_payload["checkpoints_path"] == str(checkpoint_path)
    assert synthetic_payload["effective_memory_config"]["midterm"]["page_representation"] == "summary"
    assert synthetic_payload["low_consumption_reuse"]["approximate"] is True
    assert synthetic_payload["low_consumption_reuse"]["local_replay_modes"] == [
        "page_representation_embedding"
    ]

    unchanged_baseline = json.loads(baseline_manifest.read_text(encoding="utf-8"))
    assert unchanged_baseline == baseline_payload


def test_model_smoke_releases_local_model_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[bool] = []
    configs: list[dict] = []

    class FakeReranker:
        def rerank(self, _query, rows, *, top_k):
            return [{**row, "rerank_score": 1.0} for row in rows[:top_k]]

    from mem0.utils.factory import RerankerFactory

    def create_reranker(_provider, config):
        configs.append(config)
        return FakeReranker()

    monkeypatch.setattr(RerankerFactory, "create", create_reranker)
    monkeypatch.setattr(model_discovery, "release_local_model_memory", lambda: released.append(True))
    monkeypatch.setattr(model_discovery, "_prepare_cuda_smoke", lambda _device: None)
    monkeypatch.setattr(model_discovery, "_record_cuda_smoke_headroom", lambda _candidate, _device: None)
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 8.0, 10.0),
    )
    candidate = ModelCandidate(
        model_id="local/test-reranker",
        model_type="reranker",
        source="test",
        revision="abc123",
        status="AVAILABLE",
    )

    candidate.resource_usage.update({"inference_precision": "float16", "inference_batch_size": 1})
    result = discovery.smoke_test(candidate, device="cuda")

    assert result.status == "SMOKE_PASSED"
    assert result.resource_usage["model_memory_released_after_smoke"] is True
    assert released == [True]
    assert configs[0]["device"] == "cuda"
    assert configs[0]["model_kwargs"] == {"torch_dtype": "float16"}
    assert configs[0]["batch_size"] == 1


def test_low_consumption_bounds_local_embedder_cache_to_one_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mem0.utils.factory import EmbedderFactory

    created: list[object] = []
    released: list[bool] = []

    def create_embedder(*_args, **_kwargs):
        value = object()
        created.append(value)
        return value

    monkeypatch.setattr(EmbedderFactory, "create", create_embedder)
    monkeypatch.setattr(production_midterm_adapter, "release_local_model_memory", lambda: released.append(True))
    production_midterm_adapter._LOCAL_EMBEDDER_CACHE.clear()
    production_midterm_adapter._LOCAL_RERANKER_CACHE.clear()

    class VectorConfig:
        def model_dump(self, *, mode):
            assert mode == "python"
            return {"embedding_model_dims": 3}

    def production_config(model: str):
        return type(
            "ProductionConfig",
            (),
            {
                "embedder": type("EmbedderConfig", (), {"provider": "huggingface", "config": {"model": model}})(),
                "vector_store": type("VectorStoreConfig", (), {"config": VectorConfig()})(),
                "embedding_timeout_seconds": 30.0,
            },
        )()

    set_low_consumption_mode(True)
    try:
        first = production_midterm_adapter.ProductionMidtermAdapter(
            run_dir=tmp_path,
            candidate_hash="first",
            session_id="S001",
            ranking_depth=20,
        )
        second = production_midterm_adapter.ProductionMidtermAdapter(
            run_dir=tmp_path,
            candidate_hash="second",
            session_id="S001",
            ranking_depth=20,
        )
        first_model = first._ensure_local_embedder(production_config("first-model"), {})
        second_model = second._ensure_local_embedder(production_config("second-model"), {})
    finally:
        set_low_consumption_mode(False)
        production_midterm_adapter._LOCAL_EMBEDDER_CACHE.clear()
        production_midterm_adapter._LOCAL_RERANKER_CACHE.clear()

    assert first_model is created[0]
    assert second_model is created[1]
    assert len(created) == 2
    assert released == [True]


def test_low_consumption_evicts_reranker_cache_before_loading_embedder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mem0.utils.factory import EmbedderFactory

    released: list[bool] = []
    production_midterm_adapter._LOCAL_EMBEDDER_CACHE.clear()
    production_midterm_adapter._LOCAL_RERANKER_CACHE.clear()
    production_midterm_adapter._LOCAL_RERANKER_CACHE["previous-reranker"] = object()
    monkeypatch.setattr(EmbedderFactory, "create", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(production_midterm_adapter, "release_local_model_memory", lambda: released.append(True))

    class VectorConfig:
        def model_dump(self, *, mode):
            assert mode == "python"
            return {"embedding_model_dims": 3}

    production_config = type(
        "ProductionConfig",
        (),
        {
            "embedder": type("EmbedderConfig", (), {"provider": "huggingface", "config": {"model": "next"}})(),
            "vector_store": type("VectorStoreConfig", (), {"config": VectorConfig()})(),
            "embedding_timeout_seconds": 30.0,
        },
    )()
    adapter = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="cross-cache-eviction",
        session_id="S001",
        ranking_depth=20,
    )

    set_low_consumption_mode(True)
    try:
        adapter._ensure_local_embedder(production_config, {})
    finally:
        set_low_consumption_mode(False)
        production_midterm_adapter._LOCAL_EMBEDDER_CACHE.clear()
        production_midterm_adapter._LOCAL_RERANKER_CACHE.clear()

    assert released == [True]
    assert production_midterm_adapter._LOCAL_RERANKER_CACHE == {}


def test_low_consumption_chunks_local_embedding_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []

    class FakeEmbedder:
        def embed_batch(self, texts, _action):
            batch_sizes.append(len(texts))
            return [[float(index)] for index, _text in enumerate(texts)]

    adapter = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="chunked",
        session_id="S001",
        ranking_depth=20,
    )
    monkeypatch.setattr(adapter, "_ensure_local_embedder", lambda *_args, **_kwargs: FakeEmbedder())
    set_low_consumption_mode(True)
    try:
        vectors = adapter._embed_local_texts(
            [f"field-{index}" for index in range(10)],
            action="add",
            production_config=object(),
            config={},
        )
    finally:
        set_low_consumption_mode(False)

    assert len(vectors) == 10
    assert batch_sizes == [4, 4, 2]


def test_low_consumption_checkpoints_long_embedding_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEmbedder:
        def embed_batch(self, texts, _action):
            return [[float(len(text))] for text in texts]

    adapter = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="checkpointed",
        session_id="S001",
        ranking_depth=20,
    )
    adapter._local_embedder_identity = "test-embedding-identity"
    monkeypatch.setattr(adapter, "_ensure_local_embedder", lambda *_args, **_kwargs: FakeEmbedder())
    checkpoint_calls: list[int] = []
    original_flush = adapter.flush_local_embedding_cache

    def record_flush() -> None:
        checkpoint_calls.append(adapter.embedding_calls)
        original_flush()

    monkeypatch.setattr(adapter, "flush_local_embedding_cache", record_flush)
    set_low_consumption_mode(True)
    try:
        vectors = adapter._embed_local_texts(
            [f"field-{index}" for index in range(130)],
            action="add",
            production_config=object(),
            config={},
        )
    finally:
        set_low_consumption_mode(False)

    cache_path = adapter.layout.cache_path / "low_consumption_embeddings.json"
    checkpoint = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(vectors) == 130
    assert checkpoint_calls == [64, 128]
    assert len(checkpoint["vectors"]) == 128
    assert adapter._local_embedding_cache_dirty is True

    adapter.flush_local_embedding_cache()
    final_cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(final_cache["vectors"]) == 130
    assert adapter._local_embedding_cache_dirty is False


def test_low_consumption_uses_fp16_batch_one_for_embedding_on_small_gpu() -> None:
    embedding = ModelCandidate(
        model_id="BAAI/bge-m3",
        model_type="embedding",
        source="test",
    )
    reranker = ModelCandidate(
        model_id="BAAI/bge-reranker-v2-m3",
        model_type="reranker",
        source="test",
    )
    envelope = ResourceEnvelope(1, 3.2, 8.0, 20.0)

    set_low_consumption_mode(True)
    try:
        assert model_discovery._preferred_device(embedding, envelope) == "cuda"
        assert model_discovery._preferred_device(reranker, envelope) == "cuda"
    finally:
        set_low_consumption_mode(False)


def test_model_discovery_skips_model_branches_without_gpu(tmp_path: Path) -> None:
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(0, None, 8.0, 20.0),
    )

    assert discovery.discover(model_type="embedding", allow_network=True) == []
    payload = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert payload["models"][-1]["status"] == "UNAVAILABLE_NO_GPU"


def test_model_discovery_never_falls_back_to_cpu_when_gpu_is_too_small() -> None:
    candidate = ModelCandidate(
        model_id="test/too-large",
        model_type="reranker",
        source="test",
        estimated_memory_gib=20.0,
    )

    fits, reason = model_discovery._resource_fit(candidate, ResourceEnvelope(1, 1.0, 64.0, 100.0))

    assert fits is False
    assert "current GPU" in reason


def test_model_discovery_rejects_uncached_model_with_unknown_size() -> None:
    candidate = ModelCandidate(
        model_id="test/unknown-size",
        model_type="embedding",
        source="test",
    )

    fits, reason = model_discovery._resource_fit(candidate, ResourceEnvelope(1, 3.0, 64.0, 100.0))

    assert fits is False
    assert "unbounded download" in reason


def test_model_discovery_rejects_unsupported_gguf_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(1, 3.0, 16.0, 100.0),
        cache_root=tmp_path / "hf" / "hub",
    )

    def unexpected_download(**_kwargs):
        raise AssertionError("an unsupported GGUF repository must be rejected before download")

    monkeypatch.setattr("huggingface_hub.snapshot_download", unexpected_download)
    rejected = discovery.ensure_available(
        ModelCandidate(
            model_id="vendor/reranker-GGUF",
            model_type="reranker",
            source="huggingface_search",
            tags=["gguf", "reranker"],
        ),
        allow_download=True,
    )

    assert rejected.status == "UNAVAILABLE_RUNTIME_FORMAT"
    assert "GGUF" in str(rejected.error).upper()


def test_resumed_model_discovery_never_searches_or_downloads_online(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OfflineApi:
        def list_models(self, **_kwargs):
            raise AssertionError("a resumed run must not expand its frozen model search online")

    local_only_calls: list[bool] = []

    def local_only_download(**kwargs):
        local_only_calls.append(bool(kwargs["local_files_only"]))
        raise FileNotFoundError("not present in the immutable local cache")

    monkeypatch.setattr("huggingface_hub.snapshot_download", local_only_download)
    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(1, 3.0, 16.0, 100.0),
        cache_root=tmp_path / "hf" / "hub",
        api=OfflineApi(),
        online_access=False,
    )

    assert discovery.discover(model_type="embedding", allow_network=True) == []
    unavailable = discovery.ensure_available(
        ModelCandidate(
            model_id="test/not-cached",
            model_type="embedding",
            source="frozen-run",
            revision="immutable-revision",
        ),
        allow_download=True,
    )

    assert unavailable.status == "UNAVAILABLE"
    assert local_only_calls == [True]
    assert unavailable.resource_usage["online_download_frozen_for_resume"] is True
    payload = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    assert payload["online_access"] is False
    assert any(item.get("status") == "ONLINE_DISCOVERY_FROZEN_FOR_RESUME" for item in payload["models"])


def test_resumed_model_discovery_rejects_new_cached_candidate(tmp_path: Path) -> None:
    cache = tmp_path / "hf" / "hub"
    for model_id, revision in (("test/allowed-embedding", "revision-a"), ("test/new-embedding", "revision-b")):
        snapshot = cache / f"models--{model_id.replace('/', '--')}" / "snapshots" / revision
        snapshot.mkdir(parents=True)
        (snapshot / "model.safetensors").write_bytes(b"weights")

    discovery = ModelDiscovery(
        output_path=tmp_path / "models.json",
        resources=ResourceEnvelope(1, 3.0, 16.0, 100.0),
        cache_root=cache,
        online_access=False,
        frozen_model_revisions={"embedding": {"test/allowed-embedding": ["revision-a"]}},
    )

    models = discovery.discover(
        model_type="embedding",
        allow_network=False,
        general_limit=3,
        finance_limit=0,
    )

    assert [model.model_id for model in models] == ["test/allowed-embedding"]
    payload = json.loads((tmp_path / "models.json").read_text(encoding="utf-8"))
    rejected = [item for item in payload["models"] if item.get("model_id") == "test/new-embedding"]
    assert rejected[-1]["status"] == "UNAVAILABLE_NOT_IN_RESUME_MODEL_SET"


def test_low_consumption_honors_candidate_gpu_batch_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []
    guarded: list[str] = []

    class FakeEmbedder:
        def embed_batch(self, texts, _action):
            batch_sizes.append(len(texts))
            return [[1.0] for _text in texts]

    adapter = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="gpu-chunked",
        session_id="S001",
        ranking_depth=20,
    )
    monkeypatch.setattr(adapter, "_ensure_local_embedder", lambda *_args, **_kwargs: FakeEmbedder())
    monkeypatch.setattr(
        production_midterm_adapter,
        "assert_cuda_runtime_headroom",
        lambda _device, *, context: guarded.append(context) or {},
    )
    set_low_consumption_mode(True)
    try:
        vectors = adapter._embed_local_texts(
            [f"field-{index}" for index in range(5)],
            action="add",
            production_config=object(),
            config={"embedding_inference_device": "cuda", "embedding_inference_batch_size": 1},
        )
    finally:
        set_low_consumption_mode(False)

    assert len(vectors) == 5
    assert batch_sizes == [1, 1, 1, 1, 1]
    assert len(guarded) == 6
    assert guarded[0] == "embedding model load (add)"
    assert all("batch_size=1" in context for context in guarded[1:])


def test_low_consumption_reuses_embeddings_across_identical_candidate_runtimes(tmp_path: Path) -> None:
    source = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="screening-candidate",
        session_id="S001",
        ranking_depth=20,
    )
    source_cache = source.layout.cache_path / "low_consumption_embeddings.json"
    source_cache.write_text(
        json.dumps(
            {
                "schema": 1,
                "embedder_identity": "same-model-revision-and-config",
                "vectors": {"action-and-text-hash": [1.0, 2.0]},
            }
        ),
        encoding="utf-8",
    )
    promoted = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="promoted-candidate",
        session_id="S001",
        ranking_depth=20,
    )

    set_low_consumption_mode(True)
    try:
        reused = promoted._load_reusable_local_embedding_vectors("same-model-revision-and-config")
    finally:
        set_low_consumption_mode(False)

    assert reused == 1
    assert promoted._local_embedding_vectors == {"action-and-text-hash": [1.0, 2.0]}
    assert promoted._local_embedding_cache_dirty is True
    assert promoted._reused_local_embedding_cache_paths == [str(source_cache.resolve())]


def test_low_consumption_does_not_reuse_different_embedding_identity(tmp_path: Path) -> None:
    source = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="different-screening-candidate",
        session_id="S001",
        ranking_depth=20,
    )
    (source.layout.cache_path / "low_consumption_embeddings.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "embedder_identity": "different-model-revision",
                "vectors": {"action-and-text-hash": [1.0, 2.0]},
            }
        ),
        encoding="utf-8",
    )
    promoted = production_midterm_adapter.ProductionMidtermAdapter(
        run_dir=tmp_path,
        candidate_hash="different-promoted-candidate",
        session_id="S001",
        ranking_depth=20,
    )

    set_low_consumption_mode(True)
    try:
        reused = promoted._load_reusable_local_embedding_vectors("requested-model-revision")
    finally:
        set_low_consumption_mode(False)

    assert reused == 0
    assert promoted._local_embedding_vectors == {}


def test_cuda_runtime_headroom_reclaims_cache_before_rejecting(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter(
        [
            {"free_gib": 0.1, "total_gib": 4.0, "allocated_gib": 2.0, "reserved_gib": 3.8},
            {"free_gib": 0.8, "total_gib": 4.0, "allocated_gib": 2.0, "reserved_gib": 3.0},
        ]
    )
    emptied: list[bool] = []

    class FakeCuda:
        @staticmethod
        def empty_cache() -> None:
            emptied.append(True)

    monkeypatch.setattr(low_consumption, "cuda_memory_snapshot", lambda _device: next(snapshots))
    monkeypatch.setitem(sys.modules, "torch", type("FakeTorch", (), {"cuda": FakeCuda()})())

    result = low_consumption.assert_cuda_runtime_headroom("cuda", context="test batch")

    assert emptied == [True]
    assert result["free_gib"] == 0.8
    assert result["required_free_gib"] == 0.5


def test_cuda_runtime_headroom_rejects_live_footprint_after_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshots = iter(
        [
            {"free_gib": 0.1, "total_gib": 4.0, "allocated_gib": 3.7, "reserved_gib": 3.8},
            {"free_gib": 0.2, "total_gib": 4.0, "allocated_gib": 3.7, "reserved_gib": 3.7},
        ]
    )

    class FakeCuda:
        @staticmethod
        def empty_cache() -> None:
            return None

    monkeypatch.setattr(low_consumption, "cuda_memory_snapshot", lambda _device: next(snapshots))
    monkeypatch.setitem(sys.modules, "torch", type("FakeTorch", (), {"cuda": FakeCuda()})())

    with pytest.raises(RuntimeError, match="CUDA_HEADROOM_INSUFFICIENT.*test batch"):
        low_consumption.assert_cuda_runtime_headroom("cuda", context="test batch")
