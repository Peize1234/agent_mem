"""Concurrency protection for one production reranker instance."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any

from mem0.configs.rerankers.config import RerankerConfig


class RerankerConcurrencyGuard:
    """Bound sync and async calls for one instance with a non-blocking semaphore."""

    def __init__(self, delegate: Any, max_concurrency: int = 1):
        self.delegate = delegate
        self.max_concurrency = int(max_concurrency)
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._semaphore = threading.BoundedSemaphore(self.max_concurrency)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def rerank(self, query: str, documents: list[dict[str, Any]], top_k: int | None = None):
        with self._semaphore:
            return self.delegate.rerank(query, documents, top_k)

    @asynccontextmanager
    async def _async_slot(self):
        # A blocking acquire inside the default executor can deadlock when all
        # executor threads wait for the same GPU slot. Polling keeps the event
        # loop and executor available to the active rerank call.
        while not self._semaphore.acquire(blocking=False):
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._semaphore.release()

    async def rerank_async(
        self,
        query: str,
        documents: list[dict[str, Any]],
        top_k: int | None = None,
    ):
        async with self._async_slot():
            return await asyncio.to_thread(self.delegate.rerank, query, documents, top_k)


def create_layer_reranker(backend: RerankerConfig | None, *, timeout_seconds: float) -> Any | None:
    """Build one independently guarded production reranker for a retrieval layer."""
    if backend is None:
        return None

    # Import lazily so the guard remains usable without loading provider factories.
    from mem0.utils.factory import RerankerFactory

    delegate = RerankerFactory.create(
        backend.provider,
        backend.config,
        timeout_seconds=timeout_seconds,
    )
    return RerankerConcurrencyGuard(delegate, max_concurrency=backend.max_concurrency)
