from __future__ import annotations

from copy import deepcopy
from typing import Any

from mem0.memory.memory_evolution import forgetting_factor, heat_modulations
from mem0.memory.midterm_retriever import MidTermRetriever


class DiagnosticMidTermRetriever(MidTermRetriever):
    """Skill-only production replay that retains every retrieval stage."""

    def __init__(self, midterm_memory: Any, config: Any):
        super().__init__(midterm_memory, config)
        self.last_search_diagnostics: dict[str, Any] = {}

    def _session_evolution(
        self,
        payloads: dict[str, dict[str, Any]],
        current_turn_index: int,
    ) -> tuple[dict[str, float], dict[str, float]]:
        normalized = {
            session_id: {
                **payload,
                "last_visit_turn_index": payload.get("last_visit_turn_index") or 0,
                "N_visit": payload.get("N_visit") or 0,
                "L_interaction": payload.get("L_interaction") or 0,
            }
            for session_id, payload in payloads.items()
        }
        return super()._session_evolution(normalized, current_turn_index)

    def search(
        self,
        query: str,
        filters: dict[str, Any],
        *,
        record_visits: bool = False,
        candidate_pool_size: int | None = None,
        exclude_source_job_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del record_visits, candidate_pool_size
        self.last_search_diagnostics = {}
        scope_filters = self._scope_filters(filters)
        if not scope_filters:
            return []

        top_k_sessions = int(self.config.top_k_sessions)
        top_k_pages = int(self.config.top_k_pages)
        max_total_pages = int(self.config.max_total_pages)
        if top_k_sessions <= 0:
            return []

        sessions = self.midterm_memory.search_sessions(
            query=query,
            filters=scope_filters,
            top_k=top_k_sessions,
        )
        if top_k_pages <= 0 or max_total_pages <= 0:
            current_turn_index = self._current_turn_index(scope_filters)
            session_payloads = self._session_payloads(sessions, [])
            recencies, heats = self._session_evolution(session_payloads, current_turn_index)
            return [
                self._format_session(
                    session,
                    float(getattr(session, "score", 0.0) or 0.0),
                    session_heat=heats.get(str(session.id)),
                    session_recency=recencies.get(str(session.id)),
                )
                for session in sessions
            ]

        routed_pages: list[Any] = []
        session_scores: dict[str, float] = {}
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
                    top_k=max(top_k_pages * top_k_sessions, top_k_pages),
                )
            matching = [
                page for page in pages if (getattr(page, "payload", None) or {}).get("session_id") == session_id
            ]
            routed_pages.extend(matching[:top_k_pages])

        unique_candidates = self._dedupe_pages(routed_pages)
        routed_candidate_ids = {str(page.id) for page in unique_candidates}
        multiplier = int(getattr(self.config, "midterm_candidate_pool_multiplier", 4))
        target_candidate_count = multiplier * max_total_pages
        global_pages: list[Any] = []
        if len(unique_candidates) < target_candidate_count:
            global_pages = self._global_page_candidates(query, scope_filters, target_candidate_count)
            unique_candidates = self._dedupe_pages([*unique_candidates, *global_pages])
        if exclude_source_job_id is not None:
            unique_candidates = [
                page
                for page in unique_candidates
                if (getattr(page, "payload", None) or {}).get("source_job_id") != exclude_source_job_id
            ]
        candidate_ids = {str(page.id) for page in unique_candidates}
        global_supplement_ids = {
            str(page.id) for page in global_pages if str(page.id) in candidate_ids - routed_candidate_ids
        }

        session_payloads = self._session_payloads(sessions, unique_candidates)
        candidate_session_ids = {
            str((getattr(page, "payload", None) or {}).get("session_id"))
            for page in unique_candidates
            if (getattr(page, "payload", None) or {}).get("session_id") not in (None, "")
        }
        current_turn_index = self._current_turn_index(scope_filters)
        recencies, all_heats = self._session_evolution(session_payloads, current_turn_index)
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
        diagnostic_pages: list[dict[str, Any]] = []
        for page in unique_candidates:
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
            formatted = self._format_page(
                page,
                raw_score * retention,
                session_score=session_scores.get(session_id, 0.0),
                raw_rag_score=raw_score,
                page_forgetting_factor=retention,
                heat_factor=modulation,
                effective_half_life_turns=float(self.config.retention_half_life_turns) * modulation,
            )
            formatted.update(
                {
                    "routed_candidate": str(page.id) in routed_candidate_ids,
                    "global_supplement": str(page.id) in global_supplement_ids,
                    "selected_session_count": len(sessions),
                    "session_routed_page_count": len(routed_candidate_ids),
                    "global_supplement_page_count": len(global_supplement_ids),
                    "dedup_candidate_count": len(unique_candidates),
                    "candidate_pool_count": len(unique_candidates),
                }
            )
            diagnostic_pages.append(formatted)

        diagnostic_pages.sort(key=lambda item: float(item.get("final_score") or 0.0), reverse=True)
        threshold = float(self.config.midterm_rag_threshold)
        for rank, page in enumerate(diagnostic_pages, start=1):
            page["rank_before_threshold"] = rank
            page["threshold_passed"] = float(page.get("raw_rag_score") or 0.0) >= threshold
            page["threshold_filtered"] = not page["threshold_passed"]
        post_threshold = [page for page in diagnostic_pages if page["threshold_passed"]]
        for rank, page in enumerate(post_threshold, start=1):
            page["final_rank"] = rank
            page["final_visible"] = rank <= max_total_pages

        self.last_search_diagnostics = {
            "selected_sessions": deepcopy(public_sessions),
            "routed_page_pool": deepcopy(
                [page for page in diagnostic_pages if page["routed_candidate"]]
            ),
            "global_supplement_pool": deepcopy(
                [page for page in diagnostic_pages if page["global_supplement"]]
            ),
            "deduplicated_candidate_pool": deepcopy(diagnostic_pages),
            "pre_threshold_ranking": deepcopy(diagnostic_pages),
            "threshold_filtered_candidates": deepcopy(
                [page for page in diagnostic_pages if page["threshold_filtered"]]
            ),
            "final_visible_pages": deepcopy([page for page in post_threshold if page["final_visible"]]),
            "threshold": threshold,
            "max_total_pages": max_total_pages,
            "candidate_pool_count": len(diagnostic_pages),
            "post_threshold_count": len(post_threshold),
        }
        public_pages = []
        diagnostic_fields = {
            "routed_candidate",
            "global_supplement",
            "selected_session_count",
            "session_routed_page_count",
            "global_supplement_page_count",
            "dedup_candidate_count",
            "candidate_pool_count",
            "rank_before_threshold",
            "threshold_passed",
            "threshold_filtered",
            "final_rank",
            "final_visible",
        }
        for page in post_threshold[:max_total_pages]:
            public_pages.append({key: deepcopy(value) for key, value in page.items() if key not in diagnostic_fields})
        return [*public_sessions, *public_pages]
