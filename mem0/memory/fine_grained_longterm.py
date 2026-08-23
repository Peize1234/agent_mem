"""Production retrieval for per-QA FineGrainedLongTerm facts."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Dict, Optional

from mem0.configs.base import MemoryItem
from mem0.utils.lemmatization import lemmatize_for_bm25
from mem0.utils.scoring import ENTITY_BOOST_WEIGHT, get_bm25_params, normalize_bm25, score_and_rank

logger = logging.getLogger(__name__)


def merge_vector_candidates(*routes: Sequence[Any]) -> list[Any]:
    """Keep the highest semantic score for every vector ID across retrieval routes."""
    by_id: dict[str, Any] = {}
    for route in routes:
        for row in route or []:
            row_id = str(getattr(row, "id", "") or (row.get("id") if isinstance(row, Mapping) else ""))
            if not row_id:
                continue
            score = float(getattr(row, "score", 0.0) or (row.get("score", 0.0) if isinstance(row, Mapping) else 0.0))
            current = by_id.get(row_id)
            current_score = (
                float(
                    getattr(current, "score", 0.0)
                    or (current.get("score", 0.0) if isinstance(current, Mapping) else 0.0)
                )
                if current is not None
                else None
            )
            if current is None or score > current_score:
                by_id[row_id] = row
    return sorted(
        by_id.values(),
        key=lambda row: float(
            getattr(row, "score", 0.0) or (row.get("score", 0.0) if isinstance(row, Mapping) else 0.0)
        ),
        reverse=True,
    )


def fine_grained_entity_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Entity links are user/agent scoped and deliberately cross Session boundaries."""
    return {
        key: value
        for key, value in (filters or {}).items()
        if key in ("user_id", "agent_id") and value not in (None, "")
    }


class FineGrainedLongTermRetriever:
    """One production algorithm with separate sync/async I/O orchestration."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedding_model: Any,
        entity_store_provider: Callable[[], Any],
        config: Any,
        reranker: Any = None,
        stage_output_is_visible: Callable[[Dict[str, Any]], bool] | None = None,
        payload_is_expired: Callable[[Dict[str, Any]], bool] | None = None,
        entity_extractor: Callable[[str], Sequence[tuple[str, str]]] | None = None,
        entity_boost_provider: Callable[[Sequence[tuple[str, str]], Dict[str, Any]], Dict[str, float]] | None = None,
        entity_boost_provider_async: Callable[[Sequence[tuple[str, str]], Dict[str, Any]], Any] | None = None,
        bm25_preprocessor: Callable[..., str] = lemmatize_for_bm25,
        bm25_language: str | None = None,
    ) -> None:
        self.vector_store = vector_store
        self.embedding_model = embedding_model
        self.entity_store_provider = entity_store_provider
        self.config = config
        self.reranker = reranker
        self.stage_output_is_visible = stage_output_is_visible or (lambda payload: True)
        self.payload_is_expired = payload_is_expired or (lambda payload: False)
        self.entity_extractor = entity_extractor or (lambda query: [])
        self.entity_boost_provider = entity_boost_provider
        self.entity_boost_provider_async = entity_boost_provider_async
        self.bm25_preprocessor = bm25_preprocessor
        self.bm25_language = bm25_language

    def _internal_limit(self, top_k: int) -> int:
        return max(int(top_k) * int(self.config.candidate_pool_multiplier), 60)

    @staticmethod
    def _all_session_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(filters or {})
        if result.get("user_id") and result.get("run_id"):
            result.pop("run_id", None)
        return result

    def _bm25_scores(self, query: str, lemmatized: str, keyword_results: Sequence[Any]) -> Dict[str, float]:
        scores: Dict[str, float] = {}
        midpoint, steepness = get_bm25_params(query, lemmatized=lemmatized)
        for memory in keyword_results or []:
            memory_id = str(getattr(memory, "id", "") or "")
            raw_score = float(getattr(memory, "score", 0.0) or 0.0)
            if memory_id and raw_score > 0:
                scores[memory_id] = normalize_bm25(raw_score, midpoint, steepness)
        return scores

    def _build_candidates(
        self,
        semantic_results: Sequence[Any],
        filters: Dict[str, Any],
        *,
        show_expired: bool,
    ) -> tuple[list[dict[str, Any]], Dict[str, float]]:
        current_run_id = (filters or {}).get("run_id")
        candidates: list[dict[str, Any]] = []
        session_weights: Dict[str, float] = {}
        for memory in semantic_results:
            payload = dict(getattr(memory, "payload", None) or {})
            if not self.stage_output_is_visible(payload):
                continue
            if not show_expired and self.payload_is_expired(payload):
                continue
            memory_id = str(memory.id)
            candidates.append({"id": memory_id, "score": float(memory.score), "payload": payload})
            session_weights[memory_id] = (
                1.0
                if not current_run_id or str(payload.get("run_id") or "") == str(current_run_id)
                else float(self.config.other_session_weight)
            )
        return candidates, session_weights

    def _coarse_rank(
        self,
        candidates: list[dict[str, Any]],
        bm25_scores: Dict[str, float],
        entity_boosts: Dict[str, float],
        session_weights: Dict[str, float],
        *,
        threshold: float,
        top_k: int,
        explain: bool,
    ) -> list[dict[str, Any]]:
        reranker_config = self.config.reranker
        depth = max(top_k, int(reranker_config.rerank_depth)) if reranker_config.method != "none" else top_k
        return score_and_rank(
            semantic_results=candidates,
            bm25_scores=bm25_scores,
            entity_boosts=entity_boosts,
            threshold=threshold,
            top_k=depth,
            explain=explain,
            session_weights=session_weights,
            semantic_weight=self.config.semantic_weight,
            bm25_weight=self.config.bm25_weight,
            entity_weight=self.config.entity_weight,
        )

    def _reranker_documents(self, scored: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        documents = []
        for row in scored:
            payload = dict(row.get("payload") or {})
            documents.append(
                {
                    **dict(row),
                    "memory": payload.get("data") or "",
                    "first_stage_score": float(row.get("score") or 0.0),
                }
            )
        return documents

    @staticmethod
    def _finalize_reranked(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        finalized = []
        for row in rows:
            item = dict(row)
            item["first_stage_score"] = float(item.get("first_stage_score", item.get("score")) or 0.0)
            if item.get("rerank_score") is not None:
                item["score"] = float(item["rerank_score"])
            finalized.append(item)
        return finalized

    def _rerank(self, query: str, scored: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
        if self.config.reranker.method == "none" or not scored:
            return scored[:top_k]
        if self.config.reranker.method != "cross_encoder":
            raise ValueError("FineGrainedLongTerm supports only cross_encoder reranking")
        if self.reranker is None:
            raise ValueError("FineGrainedLongTerm cross_encoder requires an injected production reranker")
        documents = self._reranker_documents(scored)
        try:
            reranked = self.reranker.rerank(query, documents, len(documents))
            return self._finalize_reranked(reranked)[:top_k]
        except Exception as exc:
            logger.warning("FineGrainedLongTerm reranking failed; using first-stage order: %s", exc)
            return scored[:top_k]

    async def _rerank_async(
        self,
        query: str,
        scored: list[dict[str, Any]],
        top_k: int,
    ) -> list[dict[str, Any]]:
        if self.config.reranker.method == "none" or not scored:
            return scored[:top_k]
        if self.config.reranker.method != "cross_encoder":
            raise ValueError("FineGrainedLongTerm supports only cross_encoder reranking")
        if self.reranker is None:
            raise ValueError("FineGrainedLongTerm cross_encoder requires an injected production reranker")
        documents = self._reranker_documents(scored)
        try:
            rerank_async = getattr(self.reranker, "rerank_async", None)
            if callable(rerank_async):
                result = await rerank_async(query, documents, len(documents))
            else:
                result = await asyncio.to_thread(self.reranker.rerank, query, documents, len(documents))
            return self._finalize_reranked(result)[:top_k]
        except Exception as exc:
            logger.warning("Async FineGrainedLongTerm reranking failed; using first-stage order: %s", exc)
            return scored[:top_k]

    @staticmethod
    def _normalize_entity_text(value: str) -> str:
        return " ".join(value.strip().lower().split())

    def _dedupe_entities(self, query_entities: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
        seen: set[str] = set()
        deduped: list[tuple[str, str]] = []
        for entity_type, entity_text in query_entities[:8]:
            key = self._normalize_entity_text(entity_text)
            if key and key not in seen:
                seen.add(key)
                deduped.append((entity_type, entity_text))
        return deduped

    def _accumulate_entity_boosts(
        self,
        matches_by_entity: Sequence[Any],
        *,
        threshold: float | None = None,
    ) -> Dict[str, float]:
        threshold = float(self.config.entity_similarity_threshold if threshold is None else threshold)
        memory_boosts: Dict[str, float] = {}
        for matches in matches_by_entity:
            if isinstance(matches, BaseException):
                logger.warning("Entity boost search failed for one entity: %s", matches)
                continue
            for match in matches:
                similarity = float(getattr(match, "score", 0.0) or 0.0)
                if similarity < threshold:
                    continue
                payload = getattr(match, "payload", None) or {}
                linked_memory_ids = payload.get("linked_memory_ids", [])
                if not isinstance(linked_memory_ids, list):
                    continue
                count = max(len(linked_memory_ids), 1)
                count_weight = 1.0 / (1.0 + 0.001 * ((count - 1) ** 2))
                boost = similarity * ENTITY_BOOST_WEIGHT * count_weight
                for memory_id in linked_memory_ids:
                    if memory_id:
                        key = str(memory_id)
                        memory_boosts[key] = max(memory_boosts.get(key, 0.0), boost)
        return memory_boosts

    def _entity_matches(self, query_entities: Sequence[tuple[str, str]], filters: Dict[str, Any]) -> list[Any]:
        deduped = self._dedupe_entities(query_entities)
        if not deduped:
            return []
        try:
            texts = [text for _, text in deduped]
            embeddings = self.embedding_model.embed_batch(texts, "search")
            if len(embeddings) != len(texts):
                logger.warning("embed_batch returned %d vectors for %d entity texts", len(embeddings), len(texts))
                return []
            entity_store = self.entity_store_provider()
            filters = fine_grained_entity_filters(filters)

            def search_entity(text: str, embedding: Sequence[float]):
                return entity_store.search(query=text, vectors=embedding, top_k=500, filters=filters)

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(search_entity, text, embedding) for text, embedding in zip(texts, embeddings)]
                results = []
                for future in futures:
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        results.append(exc)
            return results
        except Exception as exc:
            logger.warning("Entity boost computation failed: %s", exc)
            return []

    def _entity_boosts(self, query_entities: Sequence[tuple[str, str]], filters: Dict[str, Any]) -> Dict[str, float]:
        return self._accumulate_entity_boosts(self._entity_matches(query_entities, filters))

    async def _entity_matches_async(
        self,
        query_entities: Sequence[tuple[str, str]],
        filters: Dict[str, Any],
    ) -> list[Any]:
        deduped = self._dedupe_entities(query_entities)
        if not deduped:
            return []
        try:
            texts = [text for _, text in deduped]
            embeddings = await asyncio.to_thread(self.embedding_model.embed_batch, texts, "search")
            if len(embeddings) != len(texts):
                logger.warning("embed_batch returned %d vectors for %d entity texts", len(embeddings), len(texts))
                return []
            entity_store = await asyncio.to_thread(self.entity_store_provider)
            entity_filters = fine_grained_entity_filters(filters)
            semaphore = asyncio.Semaphore(4)

            async def search_entity(text: str, embedding: Sequence[float]):
                async with semaphore:
                    return await asyncio.to_thread(
                        entity_store.search,
                        query=text,
                        vectors=embedding,
                        top_k=500,
                        filters=entity_filters,
                    )

            results = await asyncio.gather(
                *(search_entity(text, embedding) for text, embedding in zip(texts, embeddings)),
                return_exceptions=True,
            )
            return list(results)
        except Exception as exc:
            logger.warning("Async entity boost computation failed: %s", exc)
            return []

    async def _entity_boosts_async(
        self,
        query_entities: Sequence[tuple[str, str]],
        filters: Dict[str, Any],
    ) -> Dict[str, float]:
        return self._accumulate_entity_boosts(await self._entity_matches_async(query_entities, filters))

    @staticmethod
    def _format_results(scored_results: Sequence[Mapping[str, Any]], *, explain: bool) -> list[dict[str, Any]]:
        promoted_payload_keys = [
            "user_id",
            "agent_id",
            "run_id",
            "actor_id",
            "role",
            "attributed_to",
            "expiration_date",
            "source_turn_index",
        ]
        core_keys = {
            "data",
            "hash",
            "created_at",
            "updated_at",
            "id",
            "text_lemmatized",
            "attributed_to",
            *promoted_payload_keys,
        }
        results = []
        for scored in scored_results:
            payload = dict(scored.get("payload") or {})
            if not payload.get("data"):
                continue
            item = MemoryItem(
                id=str(scored["id"]),
                memory=payload.get("data", ""),
                hash=payload.get("hash"),
                created_at=payload.get("created_at"),
                updated_at=payload.get("updated_at"),
                score=float(scored.get("score") or 0.0),
            ).model_dump()
            for key in promoted_payload_keys:
                if key in payload:
                    item[key] = payload[key]
            metadata = {key: value for key, value in payload.items() if key not in core_keys}
            if metadata:
                item["metadata"] = {**(item.get("metadata") or {}), **metadata}
            if explain and scored.get("score_details"):
                item["score_details"] = scored["score_details"]
            if scored.get("rerank_score") is not None:
                item["rerank_score"] = float(scored["rerank_score"])
                item["first_stage_score"] = float(scored.get("first_stage_score") or 0.0)
            results.append(item)
        return results

    def collect_candidate_signals(
        self,
        query: str,
        filters: Dict[str, Any],
        *,
        internal_limit: int,
        query_vector: Sequence[float] | None = None,
        show_expired: bool = False,
        entity_thresholds: Sequence[float] | None = None,
    ) -> dict[str, Any]:
        """Collect the production first-stage signals for search or frozen replay."""
        lemmatized = self.bm25_preprocessor(query, language=self.bm25_language)
        query_entities = self.entity_extractor(query)
        query_vector = list(query_vector) if query_vector is not None else self.embedding_model.embed(query, "search")
        all_filters = self._all_session_filters(filters)
        semantic_current = self.vector_store.search(
            query=query, vectors=query_vector, top_k=internal_limit, filters=filters
        )
        semantic_all = (
            self.vector_store.search(query=query, vectors=query_vector, top_k=internal_limit, filters=all_filters)
            if all_filters != filters
            else []
        )
        semantic = merge_vector_candidates(semantic_current, semantic_all)
        keyword_current = self.vector_store.keyword_search(query=lemmatized, top_k=internal_limit, filters=filters)
        keyword_all = (
            self.vector_store.keyword_search(query=lemmatized, top_k=internal_limit, filters=all_filters)
            if all_filters != filters
            else []
        )
        keyword = merge_vector_candidates(keyword_current, keyword_all)
        candidates, session_weights = self._build_candidates(
            semantic,
            filters,
            show_expired=show_expired,
        )
        thresholds = tuple(entity_thresholds or (float(self.config.entity_similarity_threshold),))
        if query_entities and entity_thresholds is None and self.entity_boost_provider is not None:
            entity_boosts = {f"{thresholds[0]:.1f}": self.entity_boost_provider(query_entities, filters)}
        else:
            matches = self._entity_matches(query_entities, filters) if query_entities else []
            entity_boosts = {
                f"{float(threshold):.1f}": self._accumulate_entity_boosts(matches, threshold=float(threshold))
                for threshold in thresholds
            }
        return {
            "semantic_candidates": candidates,
            "current_session_candidate_ids": [str(getattr(row, "id", "") or "") for row in semantic_current],
            "bm25_scores": self._bm25_scores(query, lemmatized, keyword),
            "entity_boosts_by_threshold": entity_boosts,
            "current_run_id": (filters or {}).get("run_id"),
            "session_weights": session_weights,
        }

    async def collect_candidate_signals_async(
        self,
        query: str,
        filters: Dict[str, Any],
        *,
        internal_limit: int,
        show_expired: bool = False,
    ) -> dict[str, Any]:
        """Async I/O orchestration for the same production first-stage signals."""
        lemmatized, query_entities, query_vector = await asyncio.gather(
            asyncio.to_thread(self.bm25_preprocessor, query, language=self.bm25_language),
            asyncio.to_thread(self.entity_extractor, query),
            asyncio.to_thread(self.embedding_model.embed, query, "search"),
        )
        all_filters = self._all_session_filters(filters)
        semantic_current, semantic_all = await asyncio.gather(
            asyncio.to_thread(
                self.vector_store.search,
                query=query,
                vectors=query_vector,
                top_k=internal_limit,
                filters=filters,
            ),
            asyncio.to_thread(
                self.vector_store.search,
                query=query,
                vectors=query_vector,
                top_k=internal_limit,
                filters=all_filters,
            )
            if all_filters != filters
            else asyncio.sleep(0, result=[]),
        )
        semantic = merge_vector_candidates(semantic_current, semantic_all)
        keyword_current, keyword_all = await asyncio.gather(
            asyncio.to_thread(
                self.vector_store.keyword_search,
                query=lemmatized,
                top_k=internal_limit,
                filters=filters,
            ),
            asyncio.to_thread(
                self.vector_store.keyword_search,
                query=lemmatized,
                top_k=internal_limit,
                filters=all_filters,
            )
            if all_filters != filters
            else asyncio.sleep(0, result=[]),
        )
        keyword = merge_vector_candidates(keyword_current, keyword_all)
        candidates, session_weights = self._build_candidates(
            semantic,
            filters,
            show_expired=show_expired,
        )
        threshold = float(self.config.entity_similarity_threshold)
        if query_entities and self.entity_boost_provider_async is not None:
            entity_boosts = await self.entity_boost_provider_async(query_entities, filters)
        else:
            matches = await self._entity_matches_async(query_entities, filters) if query_entities else []
            entity_boosts = self._accumulate_entity_boosts(matches, threshold=threshold)
        return {
            "semantic_candidates": candidates,
            "current_session_candidate_ids": [str(getattr(row, "id", "") or "") for row in semantic_current],
            "bm25_scores": self._bm25_scores(query, lemmatized, keyword),
            "entity_boosts_by_threshold": {f"{threshold:.1f}": entity_boosts},
            "current_run_id": (filters or {}).get("run_id"),
            "session_weights": session_weights,
        }

    def search(
        self,
        query: str,
        filters: Dict[str, Any],
        *,
        top_k: int | None = None,
        threshold: float | None = None,
        explain: bool = False,
        show_expired: bool = False,
    ) -> list[dict[str, Any]]:
        top_k = int(top_k or self.config.top_k)
        threshold = max(float(threshold or 0.0), float(self.config.rag_threshold))
        internal_limit = self._internal_limit(top_k)
        signals = self.collect_candidate_signals(
            query,
            filters,
            internal_limit=internal_limit,
            show_expired=show_expired,
        )
        entity_key = f"{float(self.config.entity_similarity_threshold):.1f}"
        coarse = self._coarse_rank(
            signals["semantic_candidates"],
            signals["bm25_scores"],
            signals["entity_boosts_by_threshold"].get(entity_key, {}),
            signals["session_weights"],
            threshold=threshold,
            top_k=top_k,
            explain=explain,
        )
        return self._format_results(self._rerank(query, coarse, top_k), explain=explain)

    def rank_frozen(
        self,
        query: str,
        semantic_candidates: Sequence[Mapping[str, Any]],
        *,
        bm25_scores: Mapping[str, float] | None = None,
        entity_boosts: Mapping[str, float] | None = None,
        current_run_id: str | None = None,
        current_session_candidate_ids: Sequence[str] | None = None,
        top_k: int | None = None,
        threshold: float | None = None,
        explain: bool = True,
    ) -> list[dict[str, Any]]:
        """Rank a frozen production candidate pool with the production algorithm.

        This entry point is intentionally I/O-free. Benchmark replay can freeze
        source retrieval signals without reimplementing candidate construction,
        session weighting, hybrid scoring, thresholding, or reranking.
        """
        top_k = int(top_k or self.config.top_k)
        threshold = max(float(threshold or 0.0), float(self.config.rag_threshold))
        internal_limit = self._internal_limit(top_k)
        current_ids = {str(value) for value in (current_session_candidate_ids or [])}
        current = [row for row in semantic_candidates if str(row.get("id") or "") in current_ids][:internal_limit]
        semantic_candidates = merge_vector_candidates(current, list(semantic_candidates)[:internal_limit])
        candidates = []
        session_weights: Dict[str, float] = {}
        for row in semantic_candidates:
            memory_id = str(row.get("id") or "")
            payload = dict(row.get("payload") or {})
            if not memory_id or not self.stage_output_is_visible(payload) or self.payload_is_expired(payload):
                continue
            candidates.append({"id": memory_id, "score": float(row.get("score") or 0.0), "payload": payload})
            session_weights[memory_id] = (
                1.0
                if not current_run_id or str(payload.get("run_id") or "") == str(current_run_id)
                else float(self.config.other_session_weight)
            )
        coarse = self._coarse_rank(
            candidates,
            {str(key): float(value) for key, value in (bm25_scores or {}).items()},
            {str(key): float(value) for key, value in (entity_boosts or {}).items()},
            session_weights,
            threshold=threshold,
            top_k=top_k,
            explain=explain,
        )
        return self._format_results(self._rerank(query, coarse, top_k), explain=explain)

    async def search_async(
        self,
        query: str,
        filters: Dict[str, Any],
        *,
        top_k: int | None = None,
        threshold: float | None = None,
        explain: bool = False,
        show_expired: bool = False,
    ) -> list[dict[str, Any]]:
        top_k = int(top_k or self.config.top_k)
        threshold = max(float(threshold or 0.0), float(self.config.rag_threshold))
        internal_limit = self._internal_limit(top_k)
        signals = await self.collect_candidate_signals_async(
            query,
            filters,
            internal_limit=internal_limit,
            show_expired=show_expired,
        )
        entity_key = f"{float(self.config.entity_similarity_threshold):.1f}"
        coarse = self._coarse_rank(
            signals["semantic_candidates"],
            signals["bm25_scores"],
            signals["entity_boosts_by_threshold"].get(entity_key, {}),
            signals["session_weights"],
            threshold=threshold,
            top_k=top_k,
            explain=explain,
        )
        reranked = await self._rerank_async(query, coarse, top_k)
        return self._format_results(reranked, explain=explain)
