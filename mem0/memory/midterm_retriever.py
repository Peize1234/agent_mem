from typing import Any, Dict, List, Tuple


class MidTermRetriever:
    def __init__(self, midterm_memory, config):
        self.midterm_memory = midterm_memory
        self.config = config
        self.last_search_stats = {
            "retrieved_sessions": 0,
            "candidate_pages_before_dedupe": 0,
            "candidate_pages_after_dedupe": 0,
            "returned_pages": 0,
        }

    @staticmethod
    def _scope_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
        return {
            key: value
            for key, value in (filters or {}).items()
            if key in ("user_id", "agent_id", "run_id") and value
        }

    @staticmethod
    def _format_session(session, score: float) -> Dict[str, Any]:
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
            "H_segment": payload.get("H_segment"),
        }

    @staticmethod
    def _format_page(page, score: float, session_score: float = 0.0) -> Dict[str, Any]:
        payload = getattr(page, "payload", None) or {}
        return {
            "id": str(page.id),
            "memory": payload.get("summary") or payload.get("raw_dialogue", ""),
            "score": score,
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
        }

    @staticmethod
    def _dedupe_sort_limit_pages(pages: List[Dict[str, Any]], max_total_pages: int) -> Tuple[List[Dict[str, Any]], int]:
        """Keep the best score per page ID, sort globally, then apply the total page cap."""
        if max_total_pages <= 0:
            return [], 0

        best_by_id: Dict[str, Dict[str, Any]] = {}
        for page in pages:
            page_id = str(page.get("id") or "")
            if not page_id:
                continue
            current = best_by_id.get(page_id)
            if current is None or float(page.get("score") or 0.0) > float(current.get("score") or 0.0):
                best_by_id[page_id] = page

        sorted_pages = sorted(best_by_id.values(), key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return sorted_pages[:max_total_pages], len(sorted_pages)

    def search(self, query: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        scope_filters = self._scope_filters(filters)
        self.last_search_stats = {
            "retrieved_sessions": 0,
            "candidate_pages_before_dedupe": 0,
            "candidate_pages_after_dedupe": 0,
            "returned_pages": 0,
        }
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
        self.last_search_stats["retrieved_sessions"] = len(sessions)

        results: List[Dict[str, Any]] = []
        page_candidates: List[Dict[str, Any]] = []
        for session in sessions:
            session_score = float(getattr(session, "score", 0.0) or 0.0)
            session_payload = getattr(session, "payload", None) or {}
            session_id = str(session.id)
            self.midterm_memory.record_session_visit(session_id)
            results.append(self._format_session(session, session_score))

            page_filters = {**scope_filters, "session_id": session_id}
            pages = []
            if top_k_pages > 0 and max_total_pages > 0:
                pages = self.midterm_memory.search_pages(
                    query=query,
                    filters=page_filters,
                    top_k=top_k_pages,
                )
            if not pages and top_k_pages > 0 and max_total_pages > 0 and session_payload.get("page_ids"):
                # Fallback for vector stores with limited filtering support.
                pages = self.midterm_memory.search_pages(
                    query=query,
                    filters=scope_filters,
                    top_k=max(top_k_pages * top_k_sessions, top_k_pages),
                )

            session_page_count = 0
            for page in pages:
                page_payload = getattr(page, "payload", None) or {}
                if page_payload.get("session_id") != session_id:
                    continue
                page_score = float(getattr(page, "score", 0.0) or 0.0)
                page_candidates.append(self._format_page(page, page_score, session_score=session_score))
                session_page_count += 1
                if session_page_count >= top_k_pages:
                    break

        pages, unique_page_count = self._dedupe_sort_limit_pages(page_candidates, max_total_pages)
        self.last_search_stats.update(
            {
                "candidate_pages_before_dedupe": len(page_candidates),
                "candidate_pages_after_dedupe": unique_page_count,
                "returned_pages": len(pages),
            }
        )
        results.extend(pages)

        return results
