from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import yaml

from .encoding_contract import resolve_encoding_contract
from .io_utils import atomic_write_json
from .low_consumption import (
    assert_cuda_headroom,
    cuda_memory_snapshot,
    release_local_model_memory,
    required_cuda_free_gib,
)

MODEL_KINDS = {"embedding", "reranker"}
UNSUPPORTED_TRANSFORMER_RUNTIME_FORMAT_MARKERS = frozenset({"gguf"})
TRANSFORMER_RUNTIME_ALLOW_PATTERNS = (
    "*.json",
    "*.txt",
    "*.model",
    "*.py",
    "*.safetensors",
    "pytorch_model*.bin",
)
TRANSFORMER_RUNTIME_IGNORE_PATTERNS = (
    "*.gguf",
    "*.onnx",
    "*.onnx_data",
    "openvino/*",
    "*.tflite",
    "*.mlmodel",
)
EMBEDDING_SMOKE_LONG_TEXT = (
    "贵州茅台年度报告显示经营活动现金流、营业收入、净利润、存货周转和资本开支均需结合同比变化分析。"
    * 128
)
ONLINE_REFERENCE_MODELS = {
    "embedding": (
        "Qwen/Qwen3-Embedding-0.6B",
        "BAAI/bge-m3",
        "Alibaba-NLP/gte-multilingual-base",
    ),
    "reranker": (
        "BAAI/bge-reranker-v2-m3",
        "Alibaba-NLP/gte-multilingual-reranker-base",
    ),
}
_SMOKE_RESULT_CACHE: dict[tuple[str, str, str, str], "ModelCandidate"] = {}
_SMOKE_RESULT_CACHE_LOCK = threading.RLock()
FINANCE_MARKERS = {"finance", "financial", "finbert", "finmteb", "财经", "金融"}
MULTILINGUAL_MARKERS = {"multilingual", "chinese", "zh", "bge", "gte", "e5", "qwen", "m3"}
QUALITY_METRIC_MARKERS = (
    "ndcg",
    "map",
    "mrr",
    "recall",
    "precision",
    "accuracy",
    "average_precision",
    "ap@",
    "f1",
    "hit",
)
C_MTEB_DATASET_MARKERS = (
    "t2retrieval",
    "t2reranking",
    "mmarcoretrieval",
    "mmarcoreranking",
    "duretrieval",
    "cmedqa",
    "ecomretrieval",
    "videoretrieval",
)
MULTILINGUAL_DATASET_MARKERS = ("miracl", "mmarco", "mldr", "mrtydi")
MTEB_DATASET_MARKERS = (
    "arguana",
    "climatefever",
    "cqadupstack",
    "dbpedia",
    "fever",
    "fiqa",
    "hotpotqa",
    "msmarco",
    "nfcorpus",
    "nq",
    "quora",
    "scidocs",
    "scifact",
    "touche",
    "trec-covid",
    "treccovid",
)


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
    selection_score: float | None = None
    selection_bucket: str | None = None
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
    for method_name in ("to_dict", "model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                converted = method()
            except (TypeError, ValueError):
                continue
            if isinstance(converted, Mapping):
                return converted
    nested = getattr(value, "data", None)
    if isinstance(nested, Mapping):
        return nested
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


def _string_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [str(item) for item in values if str(item).strip()]


def _label(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("type") or value.get("id") or "")
    return str(value or "")


def _finite_score(value: Any) -> float | None:
    if isinstance(value, str):
        value = value.strip()
        if value.endswith("%"):
            value = value[:-1].strip()
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _benchmark_family(text: str, fallback: str) -> str:
    lowered = text.lower()
    if "finmteb" in lowered or "fin-mteb" in lowered:
        return "FinMTEB"
    if any(marker in lowered for marker in ("c-mteb", "cmteb", "c_mteb", *C_MTEB_DATASET_MARKERS)):
        return "C-MTEB"
    if any(marker in lowered for marker in ("miracl", "multilingual", *MULTILINGUAL_DATASET_MARKERS)):
        return "Multilingual"
    if "mteb" in lowered or any(marker in lowered for marker in MTEB_DATASET_MARKERS):
        return "MTEB"
    return fallback or "Other"


def _task_family(value: Any) -> str:
    text = _label(value)
    lowered = text.lower()
    if any(marker in lowered for marker in ("rerank", "re-rank", "ranking")):
        return "Reranking"
    if any(marker in lowered for marker in ("retrieval", "search")):
        return "Retrieval"
    return text or "Unknown"


def _metric_items(value: Any) -> list[tuple[str, float]]:
    rows: list[tuple[str, float]] = []
    if isinstance(value, Mapping):
        metric_name = str(value.get("name") or value.get("type") or value.get("metric") or "")
        score = _finite_score(value.get("value", value.get("score")))
        if metric_name and score is not None:
            rows.append((metric_name, score))
        else:
            for key, raw_score in value.items():
                parsed = _finite_score(raw_score)
                if parsed is not None:
                    rows.append((str(key), parsed))
    elif isinstance(value, (list, tuple)):
        for item in value:
            rows.extend(_metric_items(item))
    return rows


def _benchmark_scores(model_index: Any) -> list[dict[str, Any]]:
    indexes = model_index if isinstance(model_index, (list, tuple)) else [model_index]
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, float]] = set()
    for raw_index in indexes:
        index = _as_mapping(raw_index)
        if not index:
            continue
        index_name = str(index.get("name") or index.get("type") or "")
        raw_results = index.get("results") or index.get("result") or []
        results = raw_results if isinstance(raw_results, (list, tuple)) else [raw_results]
        for raw_result in results:
            result = _as_mapping(raw_result)
            if not result:
                continue
            raw_task = result.get("task") or result.get("task_name") or result.get("type")
            raw_dataset = result.get("dataset") or result.get("dataset_name") or result.get("name")
            task = _task_family(raw_task)
            dataset = _label(raw_dataset) or "Unknown"
            context = " ".join(
                (
                    index_name,
                    _label(raw_task),
                    dataset,
                    str(raw_task),
                    str(raw_dataset),
                    str(result.get("source") or ""),
                )
            )
            benchmark = _benchmark_family(context, index_name)
            for metric, score in _metric_items(result.get("metrics") or result.get("metric") or []):
                key = (benchmark, task, dataset, metric, score)
                if key in seen:
                    continue
                seen.add(key)
                parsed.append(
                    {
                        "benchmark": benchmark,
                        "task": task,
                        "dataset": dataset,
                        "metric": metric,
                        "score": score,
                    }
                )
    return parsed


def _metadata_evidence(data: Mapping[str, Any]) -> dict[str, Any]:
    card = _as_mapping(data.get("cardData") or data.get("card_data") or {})
    model_index = (
        card.get("model-index") or card.get("model_index") or data.get("model-index") or data.get("model_index") or []
    )
    benchmark_scores = _benchmark_scores(model_index)
    config = _as_mapping(data.get("config") or {})
    architectures = _string_values(config.get("architectures") or card.get("architecture"))
    benchmark_text = " ".join((str(model_index), str(benchmark_scores))).lower()
    return {
        "pipeline_tag": str(data.get("pipeline_tag") or "") or None,
        "library_name": str(data.get("library_name") or "") or None,
        "languages": _string_values(card.get("language") or card.get("languages")),
        "datasets": _string_values(card.get("datasets") or card.get("dataset")),
        "architectures": architectures,
        "benchmark_scores": benchmark_scores[:200],
        "benchmark_results": benchmark_scores[:40],
        "benchmark_metadata_present": bool(benchmark_scores or model_index),
        "mteb_evidence": any(marker in benchmark_text for marker in ("mteb", "c-mteb", "cmteb")),
        "finance_benchmark_evidence": any(marker in benchmark_text for marker in ("finmteb", "finance", "financial")),
        "retrieval_benchmark_evidence": any(marker in benchmark_text for marker in ("retrieval", "rerank")),
        "license": _license(data),
        "tags": _string_values(data.get("tags")),
    }


def _estimated_memory_gib(parameters: int | None) -> float | None:
    return parameters * 4 / 1024**3 * 1.25 if parameters else None


def _resource_fit(candidate: ModelCandidate, envelope: ResourceEnvelope) -> tuple[bool, str]:
    if envelope.gpu_count <= 0 or envelope.gpu_memory_gib is None or envelope.gpu_memory_gib <= 0:
        return False, "GPU_REQUIRED_NO_GPU: local embedding/reranker inference cannot use CPU"
    needed = candidate.estimated_memory_gib
    if needed is None:
        if candidate.cache_status != "CACHED":
            return False, (
                "parameter count unavailable for an uncached model; refusing an unbounded download before CUDA smoke"
            )
        return True, "parameter count unavailable for cached model; require real CUDA smoke test"
    precision = "float16" if envelope.gpu_memory_gib <= 6.0 else "float32"
    effective_needed = needed / 2 if precision == "float16" else needed
    if effective_needed > envelope.gpu_memory_gib * 0.8:
        return False, (
            f"estimated {effective_needed:.1f} GiB at {precision} exceeds the current GPU's "
            "conservative free-memory envelope"
        )
    if envelope.free_disk_gib is not None and needed > envelope.free_disk_gib * 0.8:
        return False, f"estimated {needed:.1f} GiB exceeds free disk envelope"
    return True, f"estimated footprint {effective_needed:.1f} GiB at {precision} fits CUDA resource envelope"


def _preferred_device(candidate: ModelCandidate, envelope: ResourceEnvelope) -> str:
    del candidate
    if envelope.gpu_count <= 0 or envelope.gpu_memory_gib is None or envelope.gpu_memory_gib <= 0:
        raise RuntimeError("GPU_REQUIRED_NO_GPU: local model inference cannot use CPU")
    return "cuda"


def _transformer_runtime_compatible(candidate: ModelCandidate) -> tuple[bool, str]:
    """Only admit artifacts loadable by the configured HF transformer runtimes."""

    markers = {
        str(candidate.model_id).casefold(),
        *(str(tag).casefold() for tag in candidate.tags),
        str(candidate.metadata_evidence.get("library_name") or "").casefold(),
    }
    matched = sorted(
        marker
        for marker in UNSUPPORTED_TRANSFORMER_RUNTIME_FORMAT_MARKERS
        if any(marker in value for value in markers)
    )
    if matched:
        return False, (
            "unsupported artifact format for the configured SentenceTransformer/Transformers runtime: "
            + ", ".join(matched)
        )
    return True, "artifact format is compatible with the configured transformer runtime"


def _prepare_cuda_smoke(device: str) -> None:
    import torch

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA_REQUIRED_UNAVAILABLE: PyTorch cannot initialize CUDA")
    torch.cuda.synchronize(device)
    torch.cuda.reset_peak_memory_stats(device)


def _record_cuda_smoke_headroom(candidate: ModelCandidate, device: str) -> None:
    import torch

    torch.cuda.synchronize(device)
    allocated_bytes = torch.cuda.max_memory_allocated(device)
    reserved_bytes = torch.cuda.max_memory_reserved(device)
    gib = float(1024**3)
    snapshot = cuda_memory_snapshot(device)
    candidate.resource_usage.update(
        {
            "cuda_peak_allocated_gib": allocated_bytes / gib,
            "cuda_peak_reserved_gib": reserved_bytes / gib,
            "cuda_free_after_representative_smoke_gib": snapshot["free_gib"],
            "cuda_total_memory_gib": snapshot["total_gib"],
            "cuda_required_free_gib": required_cuda_free_gib(snapshot["total_gib"]),
        }
    )
    assert_cuda_headroom(
        device,
        context=f"representative {candidate.model_type} smoke for {candidate.model_id}",
    )


def _task_match(
    model_id: str,
    tags: Sequence[str],
    model_type: str,
    evidence: Mapping[str, Any] | None = None,
) -> bool:
    metadata = evidence or {}
    text = " ".join(
        [
            model_id,
            *tags,
            str(metadata.get("pipeline_tag") or ""),
            str(metadata.get("library_name") or ""),
            *[str(value) for value in metadata.get("architectures") or []],
            str(metadata.get("benchmark_scores") or ""),
        ]
    ).lower()
    if model_type == "reranker":
        return any(marker in text for marker in ("rerank", "cross-encoder", "text-ranking"))
    explicit_embedding = any(
        marker in text for marker in ("embedding", "feature-extraction", "sentence-similarity", "bge", "e5", "gte")
    )
    reranker_only = any(marker in text for marker in ("rerank", "cross-encoder", "text-ranking"))
    if reranker_only:
        return False
    return explicit_embedding or bool(metadata.get("retrieval_benchmark_evidence"))


def _quality_benchmark_score(row: Mapping[str, Any], model_type: str) -> bool:
    metric = str(row.get("metric") or "").lower()
    task = str(row.get("task") or "").lower()
    if not any(marker in metric for marker in QUALITY_METRIC_MARKERS):
        return False
    if model_type == "reranker":
        return "rerank" in task or "ranking" in task or "retrieval" in task
    return "retrieval" in task


def _comparison_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("benchmark") or "Other").casefold(),
        str(row.get("task") or "Unknown").casefold(),
        str(row.get("dataset") or "Unknown").casefold(),
        str(row.get("metric") or "Unknown").casefold(),
    )


def _normalize_mixed_percentage(values: Mapping[tuple[str, str], float]) -> dict[tuple[str, str], float]:
    has_fraction = any(0.0 <= value <= 1.0 for value in values.values())
    has_percentage = any(1.0 < value <= 100.0 for value in values.values())
    if not (has_fraction and has_percentage):
        return dict(values)
    return {key: value / 100.0 if 1.0 < value <= 100.0 else value for key, value in values.items()}


def _benchmark_weight(benchmark: str, *, finance: bool) -> float:
    lowered = benchmark.casefold()
    if finance:
        if "finmteb" in lowered or "finance" in lowered or "financial" in lowered:
            return 2.0
        if "c-mteb" in lowered or "cmteb" in lowered:
            return 0.8
        return 0.4
    if "c-mteb" in lowered or "cmteb" in lowered:
        return 1.25
    if "multilingual" in lowered:
        return 1.15
    return 1.0


def _annotate_relative_benchmark_scores(candidates: Sequence[ModelCandidate]) -> None:
    """Compare raw scores only inside the same benchmark/task/dataset/metric group."""

    groups: dict[tuple[str, str, str, str], dict[tuple[str, str], float]] = {}
    rows_by_candidate: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        identity = (candidate.model_id, str(candidate.revision or ""))
        rows = [
            row
            for row in candidate.metadata_evidence.get("benchmark_scores") or []
            if isinstance(row, Mapping) and _quality_benchmark_score(row, candidate.model_type)
        ]
        rows_by_candidate[identity] = rows
        for row in rows:
            score = _finite_score(row.get("score"))
            if score is None:
                continue
            group = groups.setdefault(_comparison_key(row), {})
            group[identity] = max(score, group.get(identity, -math.inf))

    comparisons: dict[tuple[str, str], list[dict[str, Any]]] = {key: [] for key in rows_by_candidate}
    for key, raw_values in groups.items():
        values = _normalize_mixed_percentage(raw_values)
        if len(values) < 2:
            continue
        for identity, score in values.items():
            lower = sum(value < score for value in values.values())
            tied = sum(value == score for value in values.values())
            percentile = (lower + max(0, tied - 1) / 2.0) / max(1, len(values) - 1)
            comparisons[identity].append(
                {
                    "benchmark": key[0],
                    "task": key[1],
                    "dataset": key[2],
                    "metric": key[3],
                    "normalized_score": score,
                    "relative_percentile": percentile,
                    "peer_count": len(values),
                }
            )

    for candidate in candidates:
        identity = (candidate.model_id, str(candidate.revision or ""))
        candidate_comparisons = comparisons.get(identity) or []
        relative: dict[str, float] = {}
        for bucket, finance in (("general", False), ("finance", True)):
            weighted = [
                (
                    float(row["relative_percentile"]),
                    _benchmark_weight(str(row["benchmark"]), finance=finance),
                )
                for row in candidate_comparisons
            ]
            denominator = sum(weight for _, weight in weighted)
            relative[bucket] = sum(score * weight for score, weight in weighted) / denominator if denominator else 0.0
        candidate.metadata_evidence["benchmark_comparisons"] = candidate_comparisons
        candidate.metadata_evidence["benchmark_relative_scores"] = relative
        candidate.metadata_evidence["quality_benchmark_score_count"] = len(rows_by_candidate.get(identity) or [])


def _candidate_score_value(candidate: ModelCandidate, *, finance: bool) -> float:
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
    relative = float((evidence.get("benchmark_relative_scores") or {}).get("finance" if finance else "general") or 0)
    comparable_scores = evidence.get("benchmark_comparisons") or []
    architecture_text = " ".join(str(value) for value in evidence.get("architectures") or []).lower()
    architecture_bonus = 1.5 * int(
        any(marker in architecture_text for marker in ("encoder", "embedding", "sentence", "crossencoder"))
    )
    benchmark_presence = 5 * int(bool(evidence.get("retrieval_benchmark_evidence")))
    benchmark_presence += 4 * int(bool(evidence.get("mteb_evidence")))
    benchmark_presence += 10 * int(finance and bool(evidence.get("finance_benchmark_evidence")))
    domain_fit = domain if finance else multilingual
    metadata_fallback = domain_fit * 10 + multilingual * 3 + license_bonus + architecture_bonus + benchmark_presence
    if comparable_scores:
        return float(50.0 * relative + 0.2 * metadata_fallback)
    return float(metadata_fallback)


def _candidate_score(candidate: ModelCandidate, *, finance: bool) -> tuple[float, int, str]:
    return (
        _candidate_score_value(candidate, finance=finance),
        candidate.downloads,
        candidate.model_id,
    )


def _is_finance_candidate(candidate: ModelCandidate) -> bool:
    evidence = candidate.metadata_evidence
    if evidence.get("finance_benchmark_evidence"):
        return True
    text = " ".join([candidate.model_id, *candidate.tags]).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    marker_match = bool(tokens & {value for value in FINANCE_MARKERS if value.isascii()}) or any(
        value in text for value in FINANCE_MARKERS if not value.isascii()
    )
    return marker_match and bool(evidence.get("retrieval_benchmark_evidence"))


def _local_metadata(snapshot: Path) -> dict[str, Any]:
    def read(name: str) -> Mapping[str, Any]:
        path = snapshot / name
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, Mapping) else {}
        except (OSError, ValueError):
            return {}

    config = read("config.json")
    sentence_config = read("config_sentence_transformers.json")
    card: Mapping[str, Any] = {}
    readme = snapshot / "README.md"
    if readme.exists():
        try:
            raw = readme.read_text(encoding="utf-8", errors="replace")
            if raw.startswith("---"):
                _, frontmatter, _ = raw.split("---", 2)
                loaded = yaml.safe_load(frontmatter)
                card = loaded if isinstance(loaded, Mapping) else {}
        except (OSError, ValueError, yaml.YAMLError):
            card = {}
    model_index_file = read("model-index.json")
    if model_index_file and not (card.get("model-index") or card.get("model_index")):
        card = {**card, "model-index": model_index_file.get("model-index") or model_index_file}
    tags = [*list(card.get("tags") or []), *(["sentence-transformers"] if (snapshot / "modules.json").exists() else [])]
    evidence = _metadata_evidence(
        {
            "cardData": card,
            "config": config,
            "pipeline_tag": card.get("pipeline_tag"),
            "library_name": "sentence-transformers" if (snapshot / "modules.json").exists() else None,
            "tags": tags,
        }
    )
    evidence["normalize_embeddings"] = bool(sentence_config.get("similarity_fn_name") == "cosine")
    return evidence


def _candidate_identity(candidate: ModelCandidate) -> tuple[str, str]:
    return candidate.model_id.casefold(), str(candidate.revision or "")


def _merge_evidence(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    merged = {**left, **right}
    for key in ("languages", "datasets", "architectures", "tags"):
        merged[key] = sorted({str(value) for value in [*(left.get(key) or []), *(right.get(key) or [])]})
    scores: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, float]] = set()
    for row in [*(left.get("benchmark_scores") or []), *(right.get("benchmark_scores") or [])]:
        if not isinstance(row, Mapping):
            continue
        score = _finite_score(row.get("score"))
        if score is None:
            continue
        key = (
            str(row.get("benchmark") or ""),
            str(row.get("task") or ""),
            str(row.get("dataset") or ""),
            str(row.get("metric") or ""),
            score,
        )
        if key not in seen:
            seen.add(key)
            scores.append(dict(row))
    merged["benchmark_scores"] = scores
    merged["benchmark_results"] = scores[:40]
    merged["benchmark_metadata_present"] = bool(
        scores or left.get("benchmark_metadata_present") or right.get("benchmark_metadata_present")
    )
    for key in ("mteb_evidence", "finance_benchmark_evidence", "retrieval_benchmark_evidence"):
        merged[key] = bool(left.get(key) or right.get(key))
    return merged


def _merge_candidate(existing: ModelCandidate, incoming: ModelCandidate) -> ModelCandidate:
    existing.source = "+".join(sorted(set(existing.source.split("+")) | set(incoming.source.split("+"))))
    existing.license = incoming.license or existing.license
    existing.tags = sorted(set(existing.tags) | set(incoming.tags))
    existing.downloads = max(existing.downloads, incoming.downloads)
    if incoming.parameter_count is not None:
        existing.parameter_count = incoming.parameter_count
        existing.estimated_memory_gib = incoming.estimated_memory_gib
    existing.metadata_evidence = _merge_evidence(existing.metadata_evidence, incoming.metadata_evidence)
    if incoming.cache_status == "CACHED" and incoming.local_path:
        existing.cache_status = "CACHED"
        existing.local_path = incoming.local_path
    return existing


def _add_candidate(
    candidates: dict[tuple[str, str], ModelCandidate],
    candidate: ModelCandidate,
) -> None:
    key = _candidate_identity(candidate)
    if key in candidates:
        _merge_candidate(candidates[key], candidate)
    else:
        candidates[key] = candidate


def _cached_snapshot(local: Mapping[str, Any] | None, revision: str | None) -> Path | None:
    if not local or not revision or revision not in set(local.get("revisions") or []):
        return None
    snapshot = Path(str(local["cache_root"])) / "snapshots" / revision
    return snapshot if snapshot.exists() else None


def _snapshot_has_model_weights(snapshot: Path, *, model_type: str) -> bool:
    """Reject HF snapshots whose symlinks point at unfinished blobs."""

    if model_type == "reranker":
        names = ("model.safetensors", "pytorch_model.bin", "model.safetensors.index.json")
    else:
        names = (
            "model.safetensors",
            "pytorch_model.bin",
            "model.safetensors.index.json",
            "pytorch_model.bin.index.json",
        )
    return any((snapshot / name).is_file() for name in names)


class ModelDiscovery:
    """Cache-aware Hugging Face discovery with unified quality ranking."""

    def __init__(
        self,
        *,
        output_path: Path,
        resources: ResourceEnvelope,
        cache_root: Path | None = None,
        api: HuggingFaceApi | None = None,
        online_access: bool = True,
        frozen_model_revisions: Mapping[str, Mapping[str, Sequence[str]]] | None = None,
    ):
        self.output_path = output_path
        self.resources = resources
        self.cache_root = cache_root or _hf_cache_root()
        self._api = api
        self.online_access = bool(online_access)
        self.frozen_model_revisions = {
            str(model_type): {
                str(model_id): tuple(sorted({str(revision) for revision in revisions if str(revision)}))
                for model_id, revisions in models.items()
            }
            for model_type, models in (frozen_model_revisions or {}).items()
        }
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
        evidence = _metadata_evidence(data)
        if not model_id or not _task_match(model_id, tags, model_type, evidence):
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
            metadata_evidence=evidence,
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
        if (
            self.resources.gpu_count <= 0
            or self.resources.gpu_memory_gib is None
            or self.resources.gpu_memory_gib <= 0
        ):
            self.events.append(
                {
                    "model_type": model_type,
                    "status": "UNAVAILABLE_NO_GPU",
                    "reason": "GPU_REQUIRED_NO_GPU: skipped local embedding/reranker discovery; CPU fallback is disabled",
                }
            )
            self.flush()
            return []
        requested_network = bool(allow_network)
        allow_network = requested_network and self.online_access
        if requested_network and not allow_network:
            self.events.append(
                {
                    "model_type": model_type,
                    "status": "ONLINE_DISCOVERY_FROZEN_FOR_RESUME",
                    "reason": "resume uses only immutable local model snapshots; new online candidates are disabled",
                }
            )
        local = local_huggingface_models(self.cache_root)
        candidates: dict[tuple[str, str], ModelCandidate] = {}
        for model_id, metadata in local.items():
            revisions = metadata.get("revisions") or []
            refs = metadata.get("refs") or {}
            revision = str(refs.get("main") or revisions[-1]) if refs.get("main") or revisions else None
            snapshot = Path(str(metadata["cache_root"])) / "snapshots" / str(revision) if revision else None
            local_evidence = _local_metadata(snapshot) if snapshot and snapshot.exists() else {}
            tags = sorted(set(re.split(r"[/_\-.]+", model_id.lower())) | set(local_evidence.get("tags") or []))
            if not _task_match(model_id, tags, model_type, local_evidence):
                continue
            _add_candidate(
                candidates,
                ModelCandidate(
                    model_id=model_id,
                    model_type=model_type,
                    source="local_huggingface_cache",
                    revision=revision,
                    license=local_evidence.get("license"),
                    tags=tags,
                    local_path=str(snapshot if snapshot and snapshot.exists() else metadata["cache_root"]),
                    cache_status="CACHED",
                    metadata_evidence=local_evidence,
                ),
            )
        if allow_network:
            queries = (
                [
                    "multilingual embedding",
                    "chinese embedding",
                    "qwen3 embedding",
                    "bge m3",
                    "Alibaba-NLP/gte-multilingual-base",
                    "financial embedding",
                    "finance retrieval",
                    "finmteb embedding",
                ]
                if model_type == "embedding"
                else [
                    "multilingual reranker",
                    "chinese reranker",
                    "bge reranker v2 m3",
                    "financial reranker",
                    "finance reranking",
                ]
            )
            api = self._api_client()
            for query in queries:
                try:
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
                            cached_path = _cached_snapshot(local.get(candidate.model_id), candidate.revision)
                            if cached_path is not None:
                                candidate.cache_status = "CACHED"
                                candidate.local_path = str(cached_path)
                            _add_candidate(candidates, candidate)
                except Exception as exc:
                    self.events.append(
                        {
                            "model_type": model_type,
                            "status": "SEARCH_UNAVAILABLE",
                            "query": query,
                            "reason": str(exc),
                        }
                    )

            model_info = getattr(api, "model_info", None)
            if callable(model_info):
                # Search ranking alone can omit strong official families when a
                # model card exposes benchmark tables only as Markdown. Fetch a
                # small, auditable reference set directly, then rank/smoke it by
                # the same rules as every other online result.
                for model_id in ONLINE_REFERENCE_MODELS[model_type]:
                    try:
                        info = model_info(model_id, files_metadata=False)
                        seeded = self._from_info(info, model_type=model_type, source="huggingface_reference_model")
                        if seeded is not None:
                            seeded.metadata_evidence["online_reference_model"] = True
                            cached_path = _cached_snapshot(local.get(seeded.model_id), seeded.revision)
                            if cached_path is not None:
                                seeded.cache_status = "CACHED"
                                seeded.local_path = str(cached_path)
                            _add_candidate(candidates, seeded)
                    except Exception as exc:
                        self.events.append(
                            {
                                "model_type": model_type,
                                "model_id": model_id,
                                "status": "REFERENCE_METADATA_UNAVAILABLE",
                                "reason": str(exc),
                            }
                        )
                online_candidates = [
                    candidate
                    for candidate in candidates.values()
                    if "huggingface_search" in candidate.source
                    and not candidate.metadata_evidence.get("benchmark_scores")
                ]
                for candidate in sorted(online_candidates, key=lambda item: item.downloads, reverse=True)[:24]:
                    try:
                        info = model_info(candidate.model_id, revision=candidate.revision, files_metadata=False)
                        enriched = self._from_info(info, model_type=model_type, source="huggingface_model_info")
                        if enriched is not None:
                            cached_path = _cached_snapshot(local.get(enriched.model_id), enriched.revision)
                            if cached_path is not None:
                                enriched.cache_status = "CACHED"
                                enriched.local_path = str(cached_path)
                            _add_candidate(candidates, enriched)
                    except Exception as exc:
                        self.events.append(
                            {
                                "model_type": model_type,
                                "model_id": candidate.model_id,
                                "revision": candidate.revision,
                                "status": "METADATA_ENRICHMENT_UNAVAILABLE",
                                "reason": str(exc),
                            }
                        )

        screened: list[ModelCandidate] = []
        for candidate in candidates.values():
            frozen_models = self.frozen_model_revisions.get(model_type) or {}
            if frozen_models:
                allowed_revisions = frozen_models.get(candidate.model_id)
                if allowed_revisions is None or str(candidate.revision or "") not in allowed_revisions:
                    candidate.status = "UNAVAILABLE_NOT_IN_RESUME_MODEL_SET"
                    candidate.selection_reason = "candidate identity was not present in the interrupted run"
                    candidate.error = "RESUME_MODEL_SET_FROZEN: refusing to expand a resumed experiment"
                    self.events.append(candidate.serializable())
                    continue
            compatible, compatibility_reason = _transformer_runtime_compatible(candidate)
            if not compatible:
                candidate.status = "UNAVAILABLE_RUNTIME_FORMAT"
                candidate.selection_reason = compatibility_reason
                candidate.error = f"UNSUPPORTED_RUNTIME_FORMAT: {compatibility_reason}"
                self.events.append(candidate.serializable())
                continue
            fits, reason = _resource_fit(candidate, self.resources)
            candidate.selection_reason = reason
            if not fits:
                candidate.status = "UNAVAILABLE_RESOURCE"
                self.events.append(candidate.serializable())
                continue
            candidate.resource_usage["preferred_device"] = _preferred_device(candidate, self.resources)
            if self.resources.gpu_memory_gib is not None and self.resources.gpu_memory_gib <= 6.0:
                candidate.resource_usage.update(
                    {
                        "inference_precision": "float16",
                        "inference_batch_size": 1,
                        "small_gpu_low_memory_mode": True,
                    }
                )
            screened.append(candidate)

        _annotate_relative_benchmark_scores(screened)
        general_pool = [item for item in screened if not _is_finance_candidate(item)]
        if len(general_pool) < general_limit:
            general_pool = screened
        general = sorted(general_pool, key=lambda item: _candidate_score(item, finance=False), reverse=True)
        references = [item for item in general if item.metadata_evidence.get("online_reference_model")]
        if references:
            # Reserve the general slots for directly verified official-family
            # candidates; missing model-index metrics must not let unrelated
            # high-download derivatives crowd all of them out.
            general = [
                *sorted(references, key=lambda item: _candidate_score(item, finance=False), reverse=True),
                *(item for item in general if not item.metadata_evidence.get("online_reference_model")),
            ]
        finance = sorted(
            [item for item in screened if _is_finance_candidate(item)],
            key=lambda item: _candidate_score(item, finance=True),
            reverse=True,
        )
        selected: list[ModelCandidate] = []
        ranked_buckets = [
            *((candidate, "general") for candidate in general[:general_limit]),
            *((candidate, "finance") for candidate in finance[:finance_limit]),
        ]
        for candidate, bucket in ranked_buckets:
            if _candidate_identity(candidate) not in {_candidate_identity(item) for item in selected}:
                finance_bucket = bucket == "finance"
                candidate.selection_bucket = bucket
                candidate.selection_score = _candidate_score_value(candidate, finance=finance_bucket)
                comparisons = candidate.metadata_evidence.get("benchmark_comparisons") or []
                candidate.selection_reason = (
                    f"{candidate.selection_reason}; unified {bucket} metadata score={candidate.selection_score:.3f}; "
                    f"structured benchmark scores={len(candidate.metadata_evidence.get('benchmark_scores') or [])}; "
                    f"comparable benchmark groups={len(comparisons)}; license={candidate.license or 'unknown'}; "
                    f"cache_status={candidate.cache_status}; cache affects download cost only"
                )
                selected.append(candidate)
        self.events.extend(candidate.serializable() for candidate in selected)
        self.flush()
        return selected

    def ensure_available(self, candidate: ModelCandidate, *, allow_download: bool) -> ModelCandidate:
        started = time.perf_counter()
        requested_download = bool(allow_download)
        allow_download = requested_download and self.online_access
        if requested_download and not allow_download:
            candidate.resource_usage["online_download_frozen_for_resume"] = True
        compatible, compatibility_reason = _transformer_runtime_compatible(candidate)
        if not compatible:
            candidate.status = "UNAVAILABLE_RUNTIME_FORMAT"
            candidate.error = f"UNSUPPORTED_RUNTIME_FORMAT: {compatibility_reason}"
            candidate.resource_usage["availability_seconds"] = time.perf_counter() - started
            self.events.append(candidate.serializable())
            self.flush()
            return candidate
        local_path = Path(candidate.local_path) if candidate.local_path else None
        if (
            candidate.cache_status == "CACHED"
            and local_path is not None
            and local_path.exists()
            and candidate.revision
            and local_path.name == candidate.revision
            and _snapshot_has_model_weights(local_path, model_type=candidate.model_type)
        ):
            candidate.status = "AVAILABLE"
            candidate.resource_usage["cache_reused_without_download"] = True
            candidate.resource_usage["availability_seconds"] = time.perf_counter() - started
            self.events.append(candidate.serializable())
            self.flush()
            return candidate
        try:
            from huggingface_hub import snapshot_download

            candidate.local_path = snapshot_download(
                repo_id=candidate.model_id,
                revision=candidate.revision,
                cache_dir=str(self.cache_root),
                local_files_only=not allow_download,
                allow_patterns=list(TRANSFORMER_RUNTIME_ALLOW_PATTERNS),
                ignore_patterns=list(TRANSFORMER_RUNTIME_IGNORE_PATTERNS),
            )
            candidate.cache_status = "DOWNLOADED" if allow_download else "CACHED"
            candidate.status = "AVAILABLE"
            if not candidate.revision:
                match = re.search(r"/snapshots/([^/]+)", candidate.local_path)
                candidate.revision = match.group(1) if match else None
            if not candidate.revision:
                raise RuntimeError("Hugging Face cache did not expose an immutable model revision")
            if not _snapshot_has_model_weights(Path(candidate.local_path), model_type=candidate.model_type):
                raise RuntimeError("Hugging Face snapshot is incomplete: model weights are missing")
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
        if not str(device).startswith("cuda"):
            candidate.status = "UNAVAILABLE_GPU_REQUIRED"
            candidate.error = "GPU_REQUIRED: CPU smoke tests and CPU inference fallback are disabled"
            self.events.append(candidate.serializable())
            self.flush()
            return candidate
        smoke_key = (
            str(candidate.model_type),
            str(candidate.model_id),
            str(candidate.revision or ""),
            str(device),
        )
        with _SMOKE_RESULT_CACHE_LOCK:
            cached = _SMOKE_RESULT_CACHE.get(smoke_key)
        if cached is not None:
            from copy import deepcopy

            reused = deepcopy(cached)
            reused.resource_usage = dict(reused.resource_usage)
            reused.resource_usage["smoke_cache_hit"] = True
            self.events.append(reused.serializable())
            self.flush()
            return reused
        started = time.perf_counter()
        embedder: Any | None = None
        reranker: Any | None = None
        try:
            _prepare_cuda_smoke(device)
            if candidate.model_type == "embedding":
                from mem0.utils.factory import EmbedderFactory

                contract, reason = resolve_encoding_contract(
                    model_id=candidate.model_id,
                    local_path=candidate.local_path,
                    metadata_evidence={**candidate.metadata_evidence, "tags": candidate.tags},
                )
                if contract is None:
                    raise RuntimeError(reason or "encoding contract unavailable")
                candidate.encoding_contract = contract.serializable()
                runtime_model_kwargs: dict[str, Any] = {"device": device}
                precision = str(candidate.resource_usage.get("inference_precision") or "")
                if device.startswith("cuda") and precision:
                    runtime_model_kwargs["model_kwargs"] = {"torch_dtype": precision}
                embedder = EmbedderFactory.create(
                    "huggingface",
                    {
                        "model": candidate.local_path or candidate.model_id,
                        "revision": candidate.revision,
                        "model_kwargs": runtime_model_kwargs,
                        "encoding_contract": candidate.encoding_contract,
                    },
                    None,
                )
                vectors = embedder.embed_batch(["贵州茅台经营现金流", "profit and cash flow"], "search")
                if len(vectors) != 2 or not len(vectors[0]):
                    raise RuntimeError("embedding smoke test returned invalid vectors")
                representative = embedder.embed_batch([EMBEDDING_SMOKE_LONG_TEXT], "add")
                if len(representative) != 1 or not len(representative[0]):
                    raise RuntimeError("representative long-text embedding smoke test returned an invalid vector")
                candidate.resource_usage["embedding_dimension"] = int(len(vectors[0]))
            else:
                from mem0.utils.factory import RerankerFactory

                reranker = RerankerFactory.create(
                    "sentence_transformer",
                    {
                        "model": candidate.local_path or candidate.model_id,
                        "device": device,
                        "revision": candidate.revision,
                        "local_files_only": bool(candidate.local_path),
                        "model_kwargs": (
                            {"torch_dtype": str(candidate.resource_usage["inference_precision"])}
                            if candidate.resource_usage.get("inference_precision")
                            else {}
                        ),
                        "batch_size": int(candidate.resource_usage.get("inference_batch_size") or 1),
                        "top_k": 2,
                    },
                )
                ranked = reranker.rerank(
                    "经营活动现金流、盈利质量和资本开支是否匹配",
                    [
                        {"memory": EMBEDDING_SMOKE_LONG_TEXT},
                        {"memory": f"股本结构和偿债能力分析。{EMBEDDING_SMOKE_LONG_TEXT}"},
                    ],
                    top_k=2,
                )
                if len(ranked) != 2 or any("rerank_score" not in row for row in ranked):
                    raise RuntimeError("reranker smoke test returned invalid scores")
            _record_cuda_smoke_headroom(candidate, device)
            candidate.status = "SMOKE_PASSED"
        except Exception as exc:
            candidate.status = "UNAVAILABLE_SMOKE_FAILED"
            candidate.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Do not retain sequentially screened transformer models.  This is
            # especially important for CPU inference on low-memory hosts where
            # glibc otherwise keeps several models' arenas resident.
            embedder = None
            reranker = None
            release_local_model_memory()
        candidate.resource_usage["smoke_seconds"] = time.perf_counter() - started
        candidate.resource_usage["model_memory_released_after_smoke"] = True
        with _SMOKE_RESULT_CACHE_LOCK:
            from copy import deepcopy

            _SMOKE_RESULT_CACHE[smoke_key] = deepcopy(candidate)
        self.events.append(candidate.serializable())
        self.flush()
        return candidate

    def flush(self) -> None:
        atomic_write_json(
            self.output_path,
            {
                "cache_root": str(self.cache_root),
                "online_access": self.online_access,
                "frozen_model_revisions": self.frozen_model_revisions,
                "resources": asdict(self.resources),
                "models": self.events,
            },
        )
