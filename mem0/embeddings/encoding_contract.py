"""Production query/document encoding contract for embedding providers."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any


class EncodingContractEmbedding:
    """Apply configured prefixes, prompts, and normalization on every memory action."""

    _FIELDS = {
        "query_prefix",
        "document_prefix",
        "query_instruction",
        "document_instruction",
        "query_prompt_name",
        "document_prompt_name",
        "normalize_embeddings",
        "pooling",
        "source",
        "confidence",
    }

    def __init__(self, delegate: Any, contract: Mapping[str, Any]):
        unknown = sorted(set(contract) - self._FIELDS)
        if unknown:
            raise ValueError(f"Unsupported embedding encoding contract fields: {', '.join(unknown)}")
        self.delegate = delegate
        self.contract = dict(contract)
        self.config = getattr(delegate, "config", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    @staticmethod
    def _is_query(action: str | None) -> bool:
        return action == "search"

    def _text(self, value: str, action: str | None) -> str:
        prefix = self.contract.get("query_prefix" if self._is_query(action) else "document_prefix") or ""
        instruction = self.contract.get("query_instruction" if self._is_query(action) else "document_instruction") or ""
        return f"{instruction}{prefix}{value}"

    def _prompt_name(self, action: str | None) -> str | None:
        value = self.contract.get("query_prompt_name" if self._is_query(action) else "document_prompt_name")
        return str(value) if value else None

    @staticmethod
    def _normalize(vector: Sequence[float]) -> list[float]:
        values = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values] if norm else values

    def _local_sentence_transformer_encode(self, values: list[str], action: str | None):
        model = getattr(self.delegate, "model", None)
        prompt_name = self._prompt_name(action)
        if model is None or not prompt_name:
            return None
        vectors = model.encode(
            values,
            convert_to_numpy=True,
            prompt_name=prompt_name,
            normalize_embeddings=bool(self.contract.get("normalize_embeddings", False)),
        )
        return [[float(value) for value in vector] for vector in vectors]

    def embed(self, text: str, memory_action: str | None = None) -> list[float]:
        values = [self._text(str(text), memory_action)]
        direct = self._local_sentence_transformer_encode(values, memory_action)
        vector = direct[0] if direct is not None else self.delegate.embed(values[0], memory_action)
        if self.contract.get("normalize_embeddings") and direct is None:
            return self._normalize(vector)
        return [float(value) for value in vector]

    def embed_batch(self, texts: Sequence[str], memory_action: str | None = "add") -> list[list[float]]:
        if not texts:
            return []
        values = [self._text(str(text), memory_action) for text in texts]
        direct = self._local_sentence_transformer_encode(values, memory_action)
        if direct is not None:
            return direct
        embed_batch = getattr(self.delegate, "embed_batch", None)
        if callable(embed_batch):
            vectors = embed_batch(values, memory_action)
        else:
            vectors = [self.delegate.embed(value, memory_action) for value in values]
        if self.contract.get("normalize_embeddings"):
            return [self._normalize(vector) for vector in vectors]
        return [[float(value) for value in vector] for vector in vectors]
