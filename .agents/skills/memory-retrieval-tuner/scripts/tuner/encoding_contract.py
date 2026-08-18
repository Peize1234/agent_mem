from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class EncodingContract:
    """Immutable, model-specific query/document encoding instructions."""

    query_prefix: str = ""
    document_prefix: str = ""
    query_instruction: str = ""
    document_instruction: str = ""
    query_prompt_name: str | None = None
    document_prompt_name: str | None = None
    normalize_embeddings: bool = False
    pooling: str | None = None
    source: str = "model-default"
    confidence: str = "explicit"

    def serializable(self) -> dict[str, Any]:
        return asdict(self)

    def text(self, value: str, *, action: str) -> str:
        if action == "search":
            return f"{self.query_instruction}{self.query_prefix}{value}"
        return f"{self.document_instruction}{self.document_prefix}{value}"

    def prompt_name(self, *, action: str) -> str | None:
        return self.query_prompt_name if action == "search" else self.document_prompt_name


def _load_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, Mapping) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _snapshot_path(path: str | None) -> Path | None:
    if not path:
        return None
    root = Path(path)
    if (root / "config.json").exists() or (root / "modules.json").exists():
        return root
    snapshots = sorted((root / "snapshots").glob("*")) if (root / "snapshots").exists() else []
    return snapshots[-1] if snapshots else root


def _pooling(snapshot: Path | None) -> str | None:
    if snapshot is None:
        return None
    for path in snapshot.glob("*_Pooling/config.json"):
        config = _load_json(path)
        modes = [
            name.removeprefix("pooling_mode_")
            for name, enabled in config.items()
            if name.startswith("pooling_mode_") and enabled is True
        ]
        if modes:
            return "+".join(sorted(modes))
    return None


def _sentence_transformer_prompts(snapshot: Path | None) -> tuple[dict[str, str], str | None]:
    if snapshot is None:
        return {}, None
    config = _load_json(snapshot / "config_sentence_transformers.json")
    prompts = config.get("prompts") or {}
    if not isinstance(prompts, Mapping):
        prompts = {}
    return {str(key): str(value) for key, value in prompts.items()}, (
        str(config.get("default_prompt_name")) if config.get("default_prompt_name") else None
    )


def _readme(snapshot: Path | None) -> str:
    if snapshot is None:
        return ""
    for name in ("README.md", "README.MD", "readme.md"):
        path = snapshot / name
        if path.exists():
            try:
                return path.read_text(encoding="utf-8", errors="replace")[:500_000]
            except OSError:
                return ""
    return ""


def resolve_encoding_contract(
    *,
    model_id: str,
    local_path: str | None,
    metadata_evidence: Mapping[str, Any] | None = None,
) -> tuple[EncodingContract | None, str | None]:
    """Resolve a safe contract from model-owned files or documented model families.

    Returning ``None`` is deliberate: an instruction-tuned model must not be
    benchmarked with a guessed bare ``encode(text)`` call.
    """

    evidence = dict(metadata_evidence or {})
    explicit = evidence.get("encoding_contract")
    if isinstance(explicit, Mapping):
        return EncodingContract(
            **{key: value for key, value in explicit.items() if key in EncodingContract.__annotations__}
        ), None

    snapshot = _snapshot_path(local_path)
    prompts, default_prompt = _sentence_transformer_prompts(snapshot)
    pooling = _pooling(snapshot)
    query_prompt = next((name for name in ("query", "query_instruction", default_prompt) if name in prompts), None)
    document_prompt = next((name for name in ("document", "passage", "corpus") if name in prompts), None)
    if prompts:
        return (
            EncodingContract(
                query_prompt_name=query_prompt,
                document_prompt_name=document_prompt,
                normalize_embeddings=bool(evidence.get("normalize_embeddings", False)),
                pooling=pooling,
                source="config_sentence_transformers.json",
            ),
            None,
        )

    lowered = model_id.lower()
    readme = _readme(snapshot).lower()
    if "intfloat/" in lowered and "e5" in lowered:
        return (
            EncodingContract(
                query_prefix="query: ",
                document_prefix="passage: ",
                normalize_embeddings=True,
                pooling=pooling,
                source="official-e5-family-contract",
            ),
            None,
        )
    if "bge-m3" in lowered:
        return EncodingContract(normalize_embeddings=True, pooling=pooling, source="official-bge-m3-contract"), None
    if re.search(r"(^|/)bge-(large|base|small)-(zh|en)(-|$)", lowered):
        instruction = (
            "Represent this sentence for searching relevant passages: "
            if "-en" in lowered
            else "为这个句子生成表示以用于检索相关文章："
        )
        return (
            EncodingContract(
                query_instruction=instruction,
                normalize_embeddings=True,
                pooling=pooling,
                source="official-bge-v1-contract",
            ),
            None,
        )
    if "gte-qwen" in lowered or "qwen3-embedding" in lowered:
        instruction_match = re.search(r"(?:instruction|instruct)[^\n]{0,80}[:：]\s*[`\"']([^`\"'\n]{8,300})", readme)
        if instruction_match:
            return (
                EncodingContract(
                    query_instruction=instruction_match.group(1).strip() + "\n",
                    normalize_embeddings=True,
                    pooling=pooling,
                    source="official-model-card-instruction",
                ),
                None,
            )
        return None, "model family requires a query instruction but no reliable model-owned instruction was found"

    tags = {str(value).lower() for value in evidence.get("tags") or []}
    architecture = " ".join(str(value) for value in evidence.get("architectures") or []).lower()
    sentence_transformer_owned = bool(
        snapshot
        and (snapshot / "modules.json").exists()
        and ("sentence-transformers" in tags or "sentence-transformers" in str(evidence.get("library_name") or ""))
    )
    if sentence_transformer_owned or "sentence-transformer" in architecture:
        return EncodingContract(pooling=pooling, source="sentence-transformers-model-default"), None
    return None, "no reliable query/document encoding contract in model config or official family metadata"


class SentenceTransformerEncodingAdapter:
    def __init__(self, model: Any, contract: EncodingContract):
        self.model = model
        self.contract = contract

    def encode(self, texts: Sequence[str], *, action: str) -> list[list[float]]:
        if not texts:
            return []
        values = [self.contract.text(str(text), action=action) for text in texts]
        kwargs: dict[str, Any] = {
            "convert_to_numpy": True,
            "normalize_embeddings": self.contract.normalize_embeddings,
        }
        prompt_name = self.contract.prompt_name(action=action)
        if prompt_name:
            kwargs["prompt_name"] = prompt_name
        vectors = self.model.encode(values, **kwargs)
        return [[float(value) for value in vector] for vector in vectors]
