from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_registry import ArtifactRegistry, load_frozen_query_overrides
from .benchmark_support import expand_env_placeholders, load_json
from .io_utils import load_json as load_json_file
from .io_utils import sha256_file, stable_hash
from .models import Candidate
from .production_midterm_adapter import load_checkpoints
from .retrieval_primitives import page_representation, session_representation


DERIVED_ARTIFACT_SCHEMA = 1
PAGE_FIELDS = ("summary", "keywords", "user_input")


@dataclass(frozen=True)
class DerivedArtifactResult:
    path: Path
    sha256: str
    reused: bool
    embedding_calls: int
    identity: dict[str, Any]


class _Encoder:
    def encode(self, texts: Sequence[str], *, action: str) -> list[list[float]]:
        raise NotImplementedError


class _ProductionEncoder(_Encoder):
    def __init__(self, manifest_path: Path):
        from mem0.configs.base import MemoryConfig
        from mem0.utils.factory import EmbedderFactory

        manifest = load_json(manifest_path)
        config_path = Path(str(manifest["memory_config_path"]))
        config = MemoryConfig(**expand_env_placeholders(load_json(config_path)))
        self.model = EmbedderFactory.create(
            config.embedder.provider,
            config.embedder.config,
            config.vector_store.config,
            timeout_seconds=config.embedding_timeout_seconds,
        )

    def encode(self, texts: Sequence[str], *, action: str) -> list[list[float]]:
        if not texts:
            return []
        batch = getattr(self.model, "embed_batch", None)
        if callable(batch):
            return [[float(value) for value in vector] for vector in batch(list(texts), action)]
        return [[float(value) for value in self.model.embed(text, action)] for text in texts]


class _SentenceTransformerEncoder(_Encoder):
    def __init__(self, model_id_or_path: str, *, device: str):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_id_or_path, device=device)

    def encode(self, texts: Sequence[str], *, action: str) -> list[list[float]]:
        del action
        if not texts:
            return []
        values = self.model.encode(list(texts), convert_to_numpy=True, normalize_embeddings=False)
        return [[float(value) for value in vector] for vector in values]


def _point_fingerprint(point: Mapping[str, Any]) -> str:
    return stable_hash({"id": str(point.get("id")), "payload": point.get("payload") or {}})


def _field_text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field) or ""
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value)


class DerivedArtifactBuilder:
    """Build content-addressed Query/Page/Session vector derivatives from production checkpoints."""

    def __init__(self, registry: ArtifactRegistry):
        self.registry = registry

    @staticmethod
    def _manifest_paths(baseline: Candidate) -> list[Path]:
        paths = [Path(str(value)) for value in baseline.config.get("manifest_paths") or []]
        if not paths:
            raise ValueError("Derived artifact generation requires production MidTerm manifests")
        return paths

    def build(
        self,
        *,
        dataset_sha256: str,
        session_scope: Sequence[str],
        baseline: Candidate,
        query_representation: str = "original",
        query_artifact_path: Path | None = None,
        query_artifact_variant: str | None = None,
        page_representation_name: str = "production",
        embedding_model_id: str = "production",
        embedding_revision: str | None = None,
        embedding_local_path: str | None = None,
        include_field_vectors: bool = False,
        device: str = "cpu",
    ) -> DerivedArtifactResult:
        manifests = self._manifest_paths(baseline)
        manifest_hashes = {str(path.resolve()): sha256_file(path) for path in manifests}
        query_artifact_sha = sha256_file(query_artifact_path) if query_artifact_path else None
        identity = {
            "schema": DERIVED_ARTIFACT_SCHEMA,
            "dataset_sha256": dataset_sha256,
            "session_scope": sorted(session_scope),
            "production_config": {
                "manifests": manifest_hashes,
                "midterm": {
                    key: baseline.config.get(key) for key in ("top_k_sessions", "top_k_pages", "max_total_pages")
                },
            },
            "prompt_hashes": load_json(manifests[0]).get("prompt_hashes") or {},
            "model": {"id": embedding_model_id, "revision": embedding_revision},
            "query_representation": query_representation,
            "query_artifact_sha256": query_artifact_sha,
            "query_artifact_variant": query_artifact_variant,
            "page_representation": page_representation_name,
            "include_field_vectors": include_field_vectors,
        }

        def produce() -> dict[str, Any]:
            checkpoints = load_checkpoints(manifests)
            selected = {
                query_id: checkpoint
                for query_id, checkpoint in checkpoints.items()
                if str((checkpoint.get("filters") or {}).get("run_id") or "") in set(session_scope)
                or any(query_id.startswith(session.split("_", 1)[0].upper()) for session in session_scope)
            }
            if not selected:
                raise ValueError("No production checkpoints matched the requested Session scope")
            overrides: dict[str, str] = {}
            if query_artifact_path is not None:
                overrides = load_frozen_query_overrides(
                    {
                        "query_artifact_path": str(query_artifact_path),
                        "query_artifact_variant": query_artifact_variant,
                    }
                )
            encoder: _Encoder
            if embedding_model_id == "production":
                encoder = _ProductionEncoder(manifests[0])
            else:
                encoder = _SentenceTransformerEncoder(embedding_local_path or embedding_model_id, device=device)

            query_ids = sorted(selected)
            query_texts = {
                query_id: overrides.get(query_id, str(selected[query_id]["query"])) for query_id in query_ids
            }
            reembed_queries = embedding_model_id != "production" or query_representation != "original"
            query_vectors = (
                dict(zip(query_ids, encoder.encode([query_texts[value] for value in query_ids], action="search")))
                if reembed_queries
                else {}
            )

            pages: dict[str, Mapping[str, Any]] = {}
            sessions: dict[str, Mapping[str, Any]] = {}
            for checkpoint in selected.values():
                for point in checkpoint.get("pages") or []:
                    pages.setdefault(_point_fingerprint(point), point)
                for point in checkpoint.get("sessions") or []:
                    sessions.setdefault(_point_fingerprint(point), point)

            reembed_pages = embedding_model_id != "production" or page_representation_name != "production"
            page_vectors = {}
            if reembed_pages:
                fingerprints = sorted(pages)
                texts = [
                    page_representation(pages[value].get("payload") or {}, page_representation_name)
                    for value in fingerprints
                ]
                page_vectors = dict(zip(fingerprints, encoder.encode(texts, action="add")))
            session_vectors = {}
            if embedding_model_id != "production":
                fingerprints = sorted(sessions)
                texts = [session_representation(sessions[value].get("payload") or {}) for value in fingerprints]
                session_vectors = dict(zip(fingerprints, encoder.encode(texts, action="add")))

            field_vectors: dict[str, dict[str, list[float]]] = {}
            if include_field_vectors:
                field_requests: list[tuple[str, str, str]] = []
                for fingerprint in sorted(pages):
                    payload = pages[fingerprint].get("payload") or {}
                    for field in PAGE_FIELDS:
                        field_requests.append((fingerprint, field, _field_text(payload, field)))
                vectors = encoder.encode([item[2] for item in field_requests], action="add")
                for (fingerprint, field, _), vector in zip(field_requests, vectors):
                    field_vectors.setdefault(fingerprint, {})[field] = vector

            embedding_calls = (
                len(query_vectors)
                + len(page_vectors)
                + len(session_vectors)
                + sum(len(value) for value in field_vectors.values())
            )
            return {
                "schema": DERIVED_ARTIFACT_SCHEMA,
                "identity": identity,
                "query_texts": query_texts,
                "query_vectors": query_vectors,
                "page_vectors": page_vectors,
                "session_vectors": session_vectors,
                "field_vectors": field_vectors,
                "embedding_calls": embedding_calls,
                "counts": {
                    "queries": len(query_ids),
                    "pages": len(pages),
                    "sessions": len(sessions),
                },
            }

        value, reused = self.registry.materialize_once(identity, produce)
        path = self.registry.artifact_path(identity)
        payload = value.get("payload") or {}
        return DerivedArtifactResult(
            path=path,
            sha256=sha256_file(path),
            reused=reused,
            embedding_calls=0 if reused else int(payload.get("embedding_calls") or 0),
            identity=identity,
        )


def load_derived_payload(path: Path) -> dict[str, Any]:
    value = load_json_file(path)
    if value.get("status") != "COMPLETE":
        raise ValueError(f"Derived artifact is incomplete: {path}")
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("schema") != DERIVED_ARTIFACT_SCHEMA:
        raise ValueError(f"Unsupported derived artifact: {path}")
    return payload


def point_fingerprint(point: Mapping[str, Any]) -> str:
    return _point_fingerprint(point)
