"""Local BM25 sparse encoding for pre-tokenized Chinese text."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import mmh3


@dataclass(frozen=True)
class SparseEmbedding:
    """Minimal sparse embedding compatible with Qdrant's BM25 adapter."""

    indices: list[int]
    values: list[float]


class ChineseBM25SparseEncoder:
    """Encode Jieba-tokenized text without loading a remote model."""

    def __init__(self, k: float = 1.2, b: float = 0.75, avg_len: float = 256.0):
        self.k = k
        self.b = b
        self.avg_len = avg_len

    @staticmethod
    def _texts(value: str | Iterable[str]) -> Iterable[str]:
        return [value] if isinstance(value, str) else value

    @staticmethod
    def _tokens(text: str) -> list[str]:
        return [token for token in text.split() if token]

    @staticmethod
    def compute_token_id(token: str) -> int:
        """Return the same stable, non-negative mmh3 index used by BM25 sparse vectors."""
        return abs(mmh3.hash(token))

    def _document_embedding(self, text: str) -> SparseEmbedding:
        tokens = self._tokens(text)
        if not tokens:
            return SparseEmbedding(indices=[], values=[])

        doc_len = len(tokens)
        weights: dict[int, float] = {}
        for token, frequency in Counter(tokens).items():
            token_id = self.compute_token_id(token)
            weight = frequency * (self.k + 1)
            weight /= frequency + self.k * (1 - self.b + self.b * doc_len / self.avg_len)
            weights[token_id] = weight

        indices = sorted(weights)
        return SparseEmbedding(indices=indices, values=[weights[index] for index in indices])

    def embed(self, documents: str | Iterable[str], **_: object) -> Iterator[SparseEmbedding]:
        """Encode documents with BM25 term-frequency weights."""
        for document in self._texts(documents):
            yield self._document_embedding(document)

    def query_embed(self, query: str | Iterable[str], **_: object) -> Iterator[SparseEmbedding]:
        """Encode each unique query token with unit weight."""
        for text in self._texts(query):
            indices = sorted({self.compute_token_id(token) for token in self._tokens(text)})
            yield SparseEmbedding(indices=indices, values=[1.0] * len(indices))
