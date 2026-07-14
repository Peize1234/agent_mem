from typing import Any, Dict, List


class MidTermRetriever:
    def __init__(self, midterm_memory, config):
        self.midterm_memory = midterm_memory
        self.config = config

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

    def search(self, query: str, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        scope_filters = self._scope_filters(filters)
        if not scope_filters:
            return []

        sessions = self.midterm_memory.search_sessions(
            query=query,
            filters=scope_filters,
            top_k=self.config.top_k_sessions,
        )

        results: List[Dict[str, Any]] = []
        seen_pages = set()
        for session in sessions:
            session_score = float(getattr(session, "score", 0.0) or 0.0)
            session_payload = getattr(session, "payload", None) or {}
            session_id = str(session.id)
            self.midterm_memory.record_session_visit(session_id)
            results.append(self._format_session(session, session_score))

            page_filters = {**scope_filters, "session_id": session_id}
            pages = self.midterm_memory.search_pages(
                query=query,
                filters=page_filters,
                top_k=self.config.top_k_pages,
            )
            if not pages and session_payload.get("page_ids"):
                # Fallback for vector stores with limited filtering support.
                pages = self.midterm_memory.search_pages(
                    query=query,
                    filters=scope_filters,
                    top_k=max(self.config.top_k_pages * self.config.top_k_sessions, self.config.top_k_pages),
                )

            for page in pages:
                page_payload = getattr(page, "payload", None) or {}
                if page_payload.get("session_id") != session_id:
                    continue
                page_id = str(page.id)
                if page_id in seen_pages:
                    continue
                seen_pages.add(page_id)
                page_score = float(getattr(page, "score", 0.0) or 0.0)
                results.append(self._format_page(page, page_score, session_score=session_score))

        return results
