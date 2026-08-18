from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .encoding_contract import SentenceTransformerEncodingAdapter, resolve_encoding_contract
from .io_utils import atomic_write_json


MODEL_KINDS = {"embedding", "reranker"}
FINANCE_MARKERS = {"finance", "financial", "finbert", "finmteb", "财经", "金融"}
MULTILINGUAL_MARKERS = {"multilingual", "chinese", "zh", "bge", "gte", "e5", "qwen", "m3"}


class HuggingFaceApi(Protocol):
    def list_models(self, **kwargs: Any) -> Iterable[Any]: ...

    def model_info(self, repo_id: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class ResourceEnvelope:
    gpu_count: int
    gpu_memory_gib: float | None
    available_memory_gib: float | None
    free_disk_gib: float | None


@dataclass
class ModelCandidate:
    model_id: str
    model_type: str
    source: str
    revision: str | None = None
    license: str | None = None
    tags: list[str] = field(default_factory=list)
    downloads: int = 0
    parameter_count: int | None = None
    estimated_memory_gib: float | None = None
    local_path: str | None = None
    cache_status: str = "NOT_CHECKED"
    status: str = "SCREENED"
    selection_reason: str = ""
    resource_usage: dict[str, Any] = field(default_factory=dict)
    metadata_evidence: dict[str, Any] = field(default_factory=dict)
    encoding_contract: dict[str, Any] | None = None
    error: str | None = None

    def serializable(self) -> dict[str, Any]:
        return asdict(self)


def _hf_cache_root() -> Path:
    configured = os.environ.get("HF_HUB_CACHE")
    if configured:
        return Path(configured).expanduser()
    hf_home = os.environ.get("HF_HOME")
    return Path(hf_home).expanduser() / "hub" if hf_home else Path.home() / ".cache" / "huggingface" / "hub"


def _decode_cache_name(name: str) -> str | None:
    if not name.startswith("models--"):
        return None
    parts = name[len("models--") :].split("--")
    return "/".join(parts) if len(parts) >= 2 else None


def local_huggingface_models(cache_root: Path | None = None) -> dict[str, dict[str, Any]]:
    root = cache_root or _hf_cache_root()
    result: dict[str, dict[str, Any]] = {}
    if not root.exists():
        return result
    for model_root in sorted(root.glob("models--*")):
        model_id = _decode_cache_name(model_root.name)
        if not model_id:
            continue
        refs: dict[str, str] = {}
        refs_root = model_root / "refs"
        if refs_root.exists():
            for path in refs_root.rglob("*"):
                if path.is_file():
                    try:
                        refs[str(path.relative_to(refs_root))] = path.read_text(encoding="utf-8").strip()
                    except OSError:
                        continue
        snapshots = [path for path in (model_root / "snapshots").glob("*") if path.is_dir()]
        result[model_id] = {
            "model_id": model_id,
            "cache_root": str(model_root),
            "refs": refs,
            "revisions": sorted(path.name for path in snapshots),
        }
    return result


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    data = getattr(value, "__dict__", None)
    return data if isinstance(data, Mapping) else {}


def _model_id(value: Any) -> str:
    data = _as_mapping(value)
    return str(data.get("id") or data.get("modelId") or data.get("model_id") or "")


def _parameter_count(data: Mapping[str, Any]) -> int | None:
    safetensors = data.get("safetensors")
    if isinstance(safetensors, Mapping):
        total = safetensors.get("total")
        if total is not None:
            try:
                return int(total)
            except (TypeError, ValueError):
                pass
        parameters = safetensors.get("parameters")
        if isinstance(parameters, Mapping):
            try:
                return sum(int(value) for value in parameters.values())
            except (TypeError, ValueError):
                pass
    return None


def _license(data: Mapping[str, Any]) -> str | None:
    card = _as_mapping(data.get("cardData") or data.get("card_data") or {})
    if card.get("license"):
        return str(card["license"])
    for tag in data.get("tags") or []:
        if str(tag).startswith("license:"):
            return str(tag).split(":", 1)[1]
    return None


def _metadata_evidence(data: Mapping[str, Any]) -> dict[str, Any]:
    card = _as_mapping(data.get("cardData") or data.get("card_data") or {})

    def string_values(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        values = value if isinstance(value, (list, tuple, set)) else [value]
        return [str(item) for item in values if str(item).strip()]

    model_index = card.get("model-index") or card.get("model_index") or []
    benchmark_results: list[dict[str, Any]] = []

    def collect_results(value: Any) -> None:
        if isinstance(value, Mapping):
            if value.get("task") or value.get("dataset") or value.get("metrics"):
                benchmark_results.append(
                    {
                        key: value.get(key)
                        for key in ("task", "dataset", "metrics", "name", "type")
                        if value.get(key) not in (None, "")
                    }
                )
            for nested in value.values():
                collect_results(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect_results(nested)

    collect_results(model_index)
    config = _as_mapping(data.get("config") or {})
    architectures = string_values(config.get("architectures") or card.get("architecture"))
    benchmark_text = " ".join(str(value) for value in benchmark_results).lower()
    return {
        "pipeline_tag": str(data.get("pipeline_tag") or "") or None,
        "library_name": str(data.get("library_name") or "") or None,
        "languages": string_values(card.get("language") or card.get("languages")),
        "datasets": string_values(card.get("datasets") or card.get("dataset")),
        "architectures": architectures,
        "benchmark_results": benchmark_results[:40],
        "benchmark_metadata_present": bool(benchmark_results),
        "mteb_evidence": any(marker in benchmark_text for marker in ("mteb", "c-mteb", "cmteb")),
        "finance_benchmark_evidence": any(marker in benchmark_text for marker in ("finmteb", "finance", "financial")),
        "retrieval_benchmark_evidence": any(marker in benchmark_text for marker in ("retrieval", "rerank")),
    }


def _estimated_memory_gib(parameters: int | None) -> float | None:
    return parameters * 4 / 1024**3 * 1.25 if parameters else None


def _resource_fit(candidate: ModelCandidate, envelope: ResourceEnvelope) -> tuple[bool, str]:
    needed = candidate.estimated_memory_gib
    if needed is None:
        return True, "parameter count unavailable; require smoke test"
    available = (
        envelope.gpu_memory_gib if envelope.gpu_count and envelope.gpu_memory_gib else envelope.available_memory_gib
    )
    if available is not None and needed > available * 0.8:
        return False, f"estimated {needed:.1f} GiB exceeds conservative resource envelope"
    if envelope.free_disk_gib is not None and needed > envelope.free_disk_gib * 0.8:
        return False, f"estimated {needed:.1f} GiB exceeds free disk envelope"
    return True, f"estimated footprint {needed:.1f} GiB fits resource envelope"


def _task_match(model_id: str, tags: Sequence[str], model_type: str) -> bool:
    text = " ".join([model_id, *tags]).lower()
    if model_type == "reranker":
        return any(marker in text for marker in ("rerank", "cross-encoder", "text-ranking"))
    return any(
        marker in text for marker in ("embedding", "feature-extraction", "sentence-similarity", "bge", "e5", "gte")
    )


def _candidate_score(candidate: ModelCandidate, *, finance: bool) -> tuple[float, int, str]:
    evidence = candidate.metadata_evidence
    text = " ".join(
        [
            candidate.model_id,
            *candidate.tags,
            *[str(value) for value in evidence.get("languages") or []],
            *[str(value) for value in evidence.get("datasets") or []],
            str(evidence.get("benchmark_results") or ""),
        ]
    ).lower()
    multilingual = sum(marker in text for marker in MULTILINGUAL_MARKERS)
    domain = sum(marker in text for marker in FINANCE_MARKERS)
    license_bonus = 1 if candidate.license and candidate.license.lower() not in {"unknown", "other"} else 0
    benchmark_bonus = 6 * int(bool(evidence.get("retrieval_benchmark_evidence")))
    benchmark_bonus += 5 * int(bool(evidence.get("mteb_evidence")))
    benchmark_bonus += 10 * int(finance and bool(evidence.get("finance_benchmark_evidence")))
    domain_fit = domain if finance else multilingual
    return (
        float(domain_fit * 10 + multilingual * 3 + license_bonus + benchmark_bonus),
        candidate.downloads,
        candidate.model_id,
    )


def _is_finance_candidate(candidate: ModelCandidate) -> bool:
    evidence = candidate.metadata_evidence
    if evidence.get("finance_benchmark_evidence"):
        return True
    tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", " ".join([candidate.model_id, *candidate.tags]).lower()))
    return bool(tokens & FINANCE_MARKERS)


def _local_metadata(snapshot: Path) -> dict[str, Any]:
    def read(name: str) -> Mapping[str, Any]:
        path = snapshot / name
        if not path.exists():
            return {}
        try:
            value = __import__("json").loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, Mapping) else {}
        except (OSError, ValueError):
            return {}

    config = read("config.json")
    sentence_config = read("config_sentence_transformers.json")
    return {
        "library_name": "sentence-transformers" if (snapshot / "modules.json").exists() else None,
        "architectures": list(config.get("architectures") or []),
        "normalize_embeddings": bool(sentence_config.get("similarity_fn_name") == "cosine"),
        "tags": ["sentence-transformers"] if (snapshot / "modules.json").exists() else [],
        "benchmark_results": [],
        "benchmark_metadata_present": False,
    }


class ModelDiscovery:
    """Local-first Hugging Face discovery with failure-isolated download and smoke testing."""

    def __init__(
        self,
        *,
        output_path: Path,
        resources: ResourceEnvelope,
        cache_root: Path | None = None,
        api: HuggingFaceApi | None = None,
    ):
        self.output_path = output_path
        self.resources = resources
        self.cache_root = cache_root or _hf_cache_root()
        self._api = api
        self.events: list[dict[str, Any]] = []

    def _api_client(self) -> HuggingFaceApi:
        if self._api is not None:
            return self._api
        from huggingface_hub import HfApi

        self._api = HfApi()
        return self._api

    def _from_info(self, value: Any, *, model_type: str, source: str) -> ModelCandidate | None:
        data = _as_mapping(value)
        model_id = _model_id(value)
        tags = [str(tag) for tag in data.get("tags") or []]
        if not model_id or not _task_match(model_id, tags, model_type):
            return None
        parameters = _parameter_count(data)
        return ModelCandidate(
            model_id=model_id,
            model_type=model_type,
            source=source,
            revision=str(data.get("sha") or "") or None,
            license=_license(data),
            tags=tags,
            downloads=int(data.get("downloads") or 0),
            parameter_count=parameters,
            estimated_memory_gib=_estimated_memory_gib(parameters),
            metadata_evidence=_metadata_evidence(data),
        )

    def discover(
        self,
        *,
        model_type: str,
        allow_network: bool,
        general_limit: int = 2,
        finance_limit: int = 2,
    ) -> list[ModelCandidate]:
        if model_type not in MODEL_KINDS:
            raise ValueError(f"Unsupported model type: {model_type}")
        local = local_huggingface_models(self.cache_root)
        candidates: dict[str, ModelCandidate] = {}
        for model_id, metadata in local.items():
            tags = re.split(r"[/_\-.]+", model_id.lower())
            if not _task_match(model_id, tags, model_type):
                continue
            revisions = metadata.get("revisions") or []
            refs = metadata.get("refs") or {}
            revision = str(refs.get("main") or revisions[-1]) if refs.get("main") or revisions else None
            snapshot = Path(str(metadata["cache_root"])) / "snapshots" / str(revision) if revision else None
            local_evidence = _local_metadata(snapshot) if snapshot and snapshot.exists() else {}
            candidates[model_id] = ModelCandidate(
                model_id=model_id,
                model_type=model_type,
                source="local_huggingface_cache",
                revision=revision,
                tags=list(tags),
                local_path=str(snapshot if snapshot and snapshot.exists() else metadata["cache_root"]),
                cache_status="CACHED",
                metadata_evidence=local_evidence,
            )
        if allow_network:
            queries = (
                ["multilingual embedding", "chinese embedding", "financial embedding", "finance retrieval"]
                if model_type == "embedding"
                else ["multilingual reranker", "chinese reranker", "financial reranker", "finance reranking"]
            )
            try:
                api = self._api_client()
                for query in queries:
                    for value in api.list_models(
                        search=query,
                        sort="downloads",
                        limit=12,
                        full=True,
                        cardData=True,
                        fetch_config=True,
                    ):
                        candidate = self._from_info(value, model_type=model_type, source="huggingface_search")
                        if candidate is not None:
                            cached = local.get(candidate.model_id)
                            if cached:
                                candidate.cache_status = "CACHED"
                                revisions = cached.get("revisions") or []
                                refs = cached.get("refs") or {}
                                revision = (
                                    str(refs.get("main") or revisions[-1]) if refs.get("main") or revisions else None
                                )
                                snapshot = Path(str(cached["cache_root"])) / "snapshots" / str(revision)
                                candidate.local_path = str(snapshot if snapshot.exists() else cached["cache_root"])
                            candidates.setdefault(candidate.model_id, candidate)
            except Exception as exc:
                self.events.append({"model_type": model_type, "status": "SEARCH_UNAVAILABLE", "reason": str(exc)})

        screened: list[ModelCandidate] = []
        for candidate in candidates.values():
            fits, reason = _resource_fit(candidate, self.resources)
            candidate.selection_reason = reason
            if not fits:
                candidate.status = "UNAVAILABLE_RESOURCE"
                self.events.append(candidate.serializable())
                continue
            screened.append(candidate)

        local_screened = sorted(
            [item for item in screened if item.cache_status == "CACHED"],
            key=lambda item: _candidate_score(item, finance=False),
            reverse=True,
        )
        general = sorted(screened, key=lambda item: _candidate_score(item, finance=False), reverse=True)
        finance = sorted(
            [item for item in screened if _is_finance_candidate(item)],
            key=lambda item: _candidate_score(item, finance=True),
            reverse=True,
        )
        selected: list[ModelCandidate] = []
        general_pool = [*local_screened[:general_limit], *general]
        general_selected: list[ModelCandidate] = []
        for candidate in general_pool:
            if candidate.model_id not in {item.model_id for item in general_selected}:
                general_selected.append(candidate)
            if len(general_selected) >= general_limit:
                break
        for candidate in [*general_selected, *finance[:finance_limit]]:
            if candidate.model_id not in {item.model_id for item in selected}:
                candidate.selection_reason = (
                    f"{candidate.selection_reason}; selected by model-card/config evidence for multilingual/Chinese retrieval"
                    + (" and finance-domain signals" if candidate in finance else "")
                    + f"; license={candidate.license or 'unknown'}, downloads={candidate.downloads}"
                    + (
                        "; model-card benchmark metadata present"
                        if candidate.metadata_evidence.get("benchmark_metadata_present")
                        else "; benchmark metadata unavailable"
                    )
                )
                selected.append(candidate)
        self.events.extend(candidate.serializable() for candidate in selected)
        self.flush()
        return selected

    def ensure_available(self, candidate: ModelCandidate, *, allow_download: bool) -> ModelCandidate:
        started = time.perf_counter()
        try:
            from huggingface_hub import snapshot_download

            candidate.local_path = snapshot_download(
                repo_id=candidate.model_id,
                revision=candidate.revision,
                cache_dir=str(self.cache_root),
                local_files_only=not allow_download,
            )
            candidate.cache_status = "DOWNLOADED" if allow_download else "CACHED"
            candidate.status = "AVAILABLE"
            if not candidate.revision:
                match = re.search(r"/snapshots/([^/]+)", candidate.local_path)
                candidate.revision = match.group(1) if match else None
            if not candidate.revision:
                raise RuntimeError("Hugging Face cache did not expose an immutable model revision")
        except Exception as exc:
            candidate.status = "UNAVAILABLE"
            candidate.error = f"{type(exc).__name__}: {exc}"
        candidate.resource_usage["availability_seconds"] = time.perf_counter() - started
        self.events.append(candidate.serializable())
        self.flush()
        return candidate

    def smoke_test(self, candidate: ModelCandidate, *, device: str = "cpu") -> ModelCandidate:
        if candidate.status != "AVAILABLE":
            return candidate
        started = time.perf_counter()
        try:
            if candidate.model_type == "embedding":
                from sentence_transformers import SentenceTransformer

                model = SentenceTransformer(candidate.local_path or candidate.model_id, device=device)
                contract, reason = resolve_encoding_contract(
                    model_id=candidate.model_id,
                    local_path=candidate.local_path,
                    metadata_evidence={**candidate.metadata_evidence, "tags": candidate.tags},
                )
                if contract is None:
                    raise RuntimeError(reason or "encoding contract unavailable")
                candidate.encoding_contract = contract.serializable()
                vectors = SentenceTransformerEncodingAdapter(model, contract).encode(
                    ["贵州茅台经营现金流", "profit and cash flow"], action="search"
                )
                if len(vectors) != 2 or not len(vectors[0]):
                    raise RuntimeError("embedding smoke test returned invalid vectors")
                candidate.resource_usage["embedding_dimension"] = int(len(vectors[0]))
            else:
                from sentence_transformers import CrossEncoder

                model = CrossEncoder(candidate.local_path or candidate.model_id, device=device)
                scores = model.predict([["现金流", "经营活动现金流改善"], ["现金流", "股本结构"]])
                if len(scores) != 2:
                    raise RuntimeError("reranker smoke test returned invalid scores")
            candidate.status = "SMOKE_PASSED"
        except Exception as exc:
            candidate.status = "UNAVAILABLE_SMOKE_FAILED"
            candidate.error = f"{type(exc).__name__}: {exc}"
        candidate.resource_usage["smoke_seconds"] = time.perf_counter() - started
        self.events.append(candidate.serializable())
        self.flush()
        return candidate

    def flush(self) -> None:
        atomic_write_json(
            self.output_path,
            {
                "cache_root": str(self.cache_root),
                "resources": asdict(self.resources),
                "models": self.events,
            },
        )
