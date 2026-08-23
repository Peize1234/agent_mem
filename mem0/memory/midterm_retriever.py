import logging
from typing import Any, Dict, List

from mem0.memory.memory_evolution import forgetting_factor, heat_modulations
from mem0.memory.midterm import compute_recency, compute_session_heat
from mem0.utils.retrieval_reranking import blend_reranker_scores, cosine_similarity

logger = logging.getLogger(__name__)


class MidTermRetriever:
    def __init__(self, midterm_memory, config, *, reranker=None):
        self.midterm_memory = midterm_memory
        self.config = config
        self.reranker = reranker

    def _on_stage(self, stage_name: str, payload: Any) -> None:
        """Diagnostic extension point; production intentionally keeps no search state."""
        del stage_name, payload

    @staticmethod
    def _scope_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value for key, value in (filters or {}).items() if key in ("user_id", "agent_id", "run_id") and value
        }

    @staticmethod
    def _format_session(
        session,
        score: float,
        *,
        session_heat: float | None = None,
        session_recency: float | None = None,
    ) -> Dict[str, Any]:
        payload = getattr(session, "payload", None) or {}
        return {
            "id": str(session.id),
            "memory": payload.get("summary", ""),
            "score": score,
            "source": "mid_term_session",
            "session_id": str(session.id),
            "summary": payload.get("summary", ""),
            "raw_dialogue": None,
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "user_id": payload.get("user_id"),
            "agent_id": payload.get("agent_id"),
            "run_id": payload.get("run_id"),
            "summary_keywords": payload.get("summary_keywords", []),
            "R_recency": session_recency if session_recency is not None else payload.get("R_recency"),
            "H_segment": session_heat if session_heat is not None else payload.get("H_segment"),
            "valid_recall_count": payload.get("valid_recall_count", 0),
            "last_recall_at": payload.get("last_recall_at"),
            "last_visit_turn_index": payload.get("last_visit_turn_index"),
            "source_job_id": payload.get("source_job_id"),
            "source_job_ids": payload.get("source_job_ids", []),
        }

    @staticmethod
    def _format_page(
        page,
        score: float,
        session_score: float = 0.0,
        *,
        raw_rag_score: float | None = None,
        page_forgetting_factor: float = 1.0,
        heat_factor: float = 1.0,
        effective_half_life_turns: float | None = None,
    ) -> Dict[str, Any]:
        payload = getattr(page, "payload", None) or {}
        raw_score = score if raw_rag_score is None else raw_rag_score
        return {
            "id": str(page.id),
            "memory": payload.get("summary") or payload.get("raw_dialogue", ""),
            "score": score,
            "raw_rag_score": raw_score,
            "final_score": score,
            "forgetting_factor": page_forgetting_factor,
            "heat_factor": heat_factor,
            "effective_half_life_turns": effective_half_life_turns,
            "valid_recall_count": payload.get("valid_recall_count", 0),
            "last_recall_at": payload.get("last_recall_at"),
            "last_recall_turn_index": payload.get("last_recall_turn_index"),
            "turn_index": payload.get("turn_index"),
            "page_sequence": payload.get("page_sequence"),
            "source": "mid_term_page",
            "session_id": payload.get("session_id"),
            "summary": payload.get("summary"),
            "raw_dialogue": payload.get("raw_dialogue"),
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
            "user_id": payload.get("user_id"),
            "agent_id": payload.get("agent_id"),
            "run_id": payload.get("run_id"),
            "keywords": payload.get("keywords", []),
            "session_score": session_score,
            "source_job_id": payload.get("source_job_id"),
        }

    @staticmethod
    def _dedupe_pages(pages: List[Any]) -> List[Any]:
        best_by_id: Dict[str, Any] = {}
        for page in pages:
            page_id = str(getattr(page, "id", "") or "")
            if not page_id:
                continue
            current = best_by_id.get(page_id)
            page_score = float(getattr(page, "score", 0.0) or 0.0)
            current_score = float(getattr(current, "score", 0.0) or 0.0) if current is not None else None
            if current is None or page_score > current_score:
                best_by_id[page_id] = page
        return list(best_by_id.values())

    @staticmethod
    def _dedupe_sort_limit_pages(
        pages: List[Dict[str, Any]],
        max_total_pages: int,
    ) -> List[Dict[str, Any]]:
        """Compatibility helper: keep the best final score per page ID."""
        if max_total_pages <= 0:
            return []
        best_by_id: Dict[str, Dict[str, Any]] = {}
        for page in pages:
            page_id = str(page.get("id") or "")
            if not page_id:
                continue
            current = best_by_id.get(page_id)
            if current is None or float(page.get("score") or 0.0) > float(current.get("score") or 0.0):
                best_by_id[page_id] = page
        return sorted(
            best_by_id.values(),
            key=lambda item: float(item.get("score") or 0.0),
            reverse=True,
        )[:max_total_pages]

    def _global_page_candidates(
        self,
        query: str,
        scope_filters: Dict[str, Any],
        target_candidate_count: int,
    ) -> List[Any]:
        if target_candidate_count <= 0:
            return []
        try:
            return self.midterm_memory.search_pages(
                query=query,
                filters=scope_filters,
                top_k=target_candidate_count,
            )
        except Exception as exc:
            logger.warning("Global mid-term page candidate supplementation failed: %s", exc)
            return []

    def _session_payloads(self, sessions: List[Any], pages: List[Any]) -> Dict[str, Dict[str, Any]]:
        payloads = {str(session.id): dict(getattr(session, "payload", None) or {}) for session in sessions}
        get_session = getattr(self.midterm_memory, "get_session", None)
        if not callable(get_session):
            return payloads
        for page in pages:
            page_payload = getattr(page, "payload", None) or {}
            session_id = page_payload.get("session_id")
            if session_id in (None, "") or str(session_id) in payloads:
                continue
            try:
                session = get_session(str(session_id))
            except Exception:
                logger.debug("Failed to load session heat for page candidate", exc_info=True)
                continue
            if session:
                payloads[str(session_id)] = dict(getattr(session, "payload", None) or {})
        return payloads

    def _current_turn_index(self, scope_filters: Dict[str, Any]) -> int:
        return int(self.midterm_memory.current_turn_index(scope_filters))

    def _session_evolution(
        self,
        payloads: Dict[str, Dict[str, Any]],
        current_turn_index: int,
    ) -> tuple[Dict[str, float], Dict[str, float]]:
        payloads = {
            session_id: {
                **payload,
                "last_visit_turn_index": payload.get("last_visit_turn_index") or 0,
                "N_visit": payload.get("N_visit") or 0,
                "L_interaction": payload.get("L_interaction") or 0,
            }
            for session_id, payload in payloads.items()
        }
        recencies = {
            session_id: compute_recency(
                int(payload["last_visit_turn_index"]),
                current_turn_index,
                self.config.heat_recency_tau_turns,
            )
            for session_id, payload in payloads.items()
        }
        heats = {
            session_id: compute_session_heat(payload, self.config, current_turn_index)
            for session_id, payload in payloads.items()
        }
        return recencies, heats

    def _select_sessions(self, query: str, scope_filters: Dict[str, Any]) -> List[Any]:
        sessions = self.midterm_memory.search_sessions(
            query=query,
            filters=scope_filters,
            top_k=int(self.config.top_k_sessions),
        )
        self._on_stage("session_candidates", sessions)
        return sessions

    def _collect_page_candidates(
        self,
        query: str,
        scope_filters: Dict[str, Any],
        sessions: List[Any],
        *,
        exclude_source_job_id: str | None,
    ) -> tuple[List[Any], Dict[str, float]]:
        top_k_pages = int(self.config.top_k_pages)
        page_candidates: List[Any] = []
        session_scores: Dict[str, float] = {}
        for session in sessions:
            session_id = str(session.id)
            session_scores[session_id] = float(getattr(session, "score", 0.0) or 0.0)
            pages = self.midterm_memory.search_pages(
                query=query,
                filters={**scope_filters, "session_id": session_id},
                top_k=top_k_pages,
            )
            if not pages and (getattr(session, "payload", None) or {}).get("page_ids"):
                pages = self.midterm_memory.search_pages(
                    query=query,
                    filters=scope_filters,
                    top_k=max(top_k_pages * len(sessions), top_k_pages),
                )
            page_candidates.extend(
                page
                for page in pages[:top_k_pages]
                if (getattr(page, "payload", None) or {}).get("session_id") == session_id
            )

        routed_candidates = self._dedupe_pages(page_candidates)
        self._on_stage("routed_page_candidates", routed_candidates)
        unique_candidates = list(routed_candidates)
        target_candidate_count = int(getattr(self.config, "midterm_candidate_pool_multiplier", 4)) * int(
            self.config.max_total_pages
        )
        global_candidates: List[Any] = []
        if len(unique_candidates) < target_candidate_count:
            global_candidates = self._global_page_candidates(query, scope_filters, target_candidate_count)
            unique_candidates = self._dedupe_pages([*unique_candidates, *global_candidates])
        if exclude_source_job_id is not None:
            unique_candidates = [
                page
                for page in unique_candidates
                if (getattr(page, "payload", None) or {}).get("source_job_id") != exclude_source_job_id
            ]
        routed_ids = {str(page.id) for page in routed_candidates}
        candidate_ids = {str(page.id) for page in unique_candidates}
        self._on_stage(
            "global_supplement",
            [page for page in global_candidates if str(page.id) in candidate_ids - routed_ids],
        )
        self._on_stage("deduplicated_candidates", unique_candidates)
        return unique_candidates, session_scores

    def _score_page_candidates(
        self,
        sessions: List[Any],
        candidates: List[Any],
        session_scores: Dict[str, float],
        scope_filters: Dict[str, Any],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        session_payloads = self._session_payloads(sessions, candidates)
        current_turn_index = self._current_turn_index(scope_filters)
        recencies, all_heats = self._session_evolution(session_payloads, current_turn_index)
        candidate_session_ids = {
            str((getattr(page, "payload", None) or {}).get("session_id"))
            for page in candidates
            if (getattr(page, "payload", None) or {}).get("session_id") not in (None, "")
        }
        modulations = heat_modulations(
            {session_id: all_heats.get(session_id, 0.0) for session_id in candidate_session_ids},
            minimum=self.config.heat_modulation_min,
            maximum=self.config.heat_modulation_max,
        )
        public_sessions = [
            self._format_session(
                session,
                float(getattr(session, "score", 0.0) or 0.0),
                session_heat=all_heats.get(str(session.id)),
                session_recency=recencies.get(str(session.id)),
            )
            for session in sessions
        ]
        ranked_pages: List[Dict[str, Any]] = []
        for page in candidates:
            payload = getattr(page, "payload", None) or {}
            session_id = str(payload.get("session_id") or "")
            raw_score = float(getattr(page, "score", 0.0) or 0.0)
            modulation = modulations.get(session_id, 1.0)
            retention = forgetting_factor(
                payload,
                self.config,
                current_turn_index=current_turn_index,
                heat_factor=modulation,
            )
            ranked_pages.append(
                self._format_page(
                    page,
                    raw_score * retention,
                    session_score=session_scores.get(session_id, 0.0),
                    raw_rag_score=raw_score,
                    page_forgetting_factor=retention,
                    heat_factor=modulation,
                    effective_half_life_turns=float(self.config.retention_half_life_turns) * modulation,
                )
            )
        ranked_pages.sort(key=lambda item: float(item.get("final_score") or 0.0), reverse=True)
        self._on_stage("scored_candidates", ranked_pages)
        return public_sessions, ranked_pages

    def _apply_reranker(self, query: str, ranked_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        reranker_config = getattr(self.config, "reranker", None)
        method = getattr(reranker_config, "method", "none") if reranker_config is not None else "none"
        if method == "none" or not ranked_pages:
            self._on_stage("reranked_candidates", ranked_pages)
            return ranked_pages
        depth = min(int(reranker_config.rerank_depth), len(ranked_pages))
        head = [dict(row) for row in ranked_pages[:depth]]
        tail = [dict(row) for row in ranked_pages[depth:]]
        try:
            if method == "cross_encoder":
                if self.reranker is None:
                    raise ValueError("MidTerm cross_encoder reranking requires an injected production reranker")
                reranked = self.reranker.rerank(query, head, depth)
                by_id = {str(row.get("id") or ""): dict(row) for row in head}
                reordered = []
                for row in reranked:
                    item_id = str(row.get("id") or "")
                    merged = {**by_id.get(item_id, {}), **dict(row)}
                    merged["first_stage_score"] = by_id.get(item_id, {}).get("score")
                    if merged.get("rerank_score") is not None:
                        merged["score"] = float(merged["rerank_score"])
                        merged["final_score"] = float(merged["rerank_score"])
                    reordered.append(merged)
                reranked_head = reordered
            elif method == "multi_vector_maxsim":
                secondary_scores: Dict[str, float] = {}
                query_vector = self.midterm_memory.embedding_model.embed(query, "search")
                for row in head:
                    stored = self.midterm_memory.get_page(str(row["id"]))
                    payload = getattr(stored, "payload", None) or {}
                    vectors = payload.get("_field_vectors") or {}
                    if not vectors:
                        raise ValueError("MidTerm Page is missing production _field_vectors")
                    secondary_scores[str(row["id"])] = max(
                        cosine_similarity(query_vector, vector) for vector in vectors.values()
                    )
                reranked_head = blend_reranker_scores(
                    head,
                    secondary_scores,
                    dense_weight=float(reranker_config.dense_weight),
                )
            else:
                raise ValueError(f"Unsupported MidTerm reranker method: {method}")
            result = [*reranked_head, *tail]
        except Exception as exc:
            logger.warning("MidTerm reranking failed; using first-stage order: %s", exc)
            result = ranked_pages
        self._on_stage("reranked_candidates", result)
        return result

    def _apply_threshold(self, ranked_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        threshold = float(self.config.midterm_rag_threshold)
        selected = [page for page in ranked_pages if float(page.get("raw_rag_score") or 0.0) >= threshold]
        self._on_stage("threshold_candidates", {"threshold": threshold, "candidates": selected})
        return selected

    def _select_final_pages(self, ranked_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        selected = ranked_pages[: int(self.config.max_total_pages)]
        self._on_stage("final_selection", selected)
        return selected

    def search(
        self,
        query: str,
        filters: Dict[str, Any],
        *,
        record_visits: bool = False,
        candidate_pool_size: int | None = None,
        exclude_source_job_id: str | None = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve candidates without mutating recall state.

        ``record_visits`` and ``candidate_pool_size`` remain accepted for API
        compatibility. Recall is confirmed only by the context assembly layer,
        and the candidate target is always derived from ``max_total_pages``.
        """
        del record_visits, candidate_pool_size
        self._on_stage("search_started", {"query": query, "filters": dict(filters or {})})
        scope_filters = self._scope_filters(filters)
        if not scope_filters:
            return []

        if int(self.config.top_k_sessions) <= 0:
            return []
        sessions = self._select_sessions(query, scope_filters)
        if int(self.config.top_k_pages) <= 0 or int(self.config.max_total_pages) <= 0:
            current_turn_index = self._current_turn_index(scope_filters)
            session_payloads = self._session_payloads(sessions, [])
            recencies, heats = self._session_evolution(session_payloads, current_turn_index)
            public_sessions = [
                self._format_session(
                    session,
                    float(getattr(session, "score", 0.0) or 0.0),
                    session_heat=heats.get(str(session.id)),
                    session_recency=recencies.get(str(session.id)),
                )
                for session in sessions
            ]
            self._on_stage("final_selection", [])
            return public_sessions

        candidates, session_scores = self._collect_page_candidates(
            query,
            scope_filters,
            sessions,
            exclude_source_job_id=exclude_source_job_id,
        )
        public_sessions, ranked_pages = self._score_page_candidates(
            sessions,
            candidates,
            session_scores,
            scope_filters,
        )
        reranked_pages = self._apply_reranker(query, ranked_pages)
        thresholded_pages = self._apply_threshold(reranked_pages)
        return [*public_sessions, *self._select_final_pages(thresholded_pages)]
