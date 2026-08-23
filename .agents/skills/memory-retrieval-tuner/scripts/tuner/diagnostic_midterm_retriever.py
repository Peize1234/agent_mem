from __future__ import annotations

from copy import deepcopy
from typing import Any

from mem0.memory.midterm_retriever import MidTermRetriever


class DiagnosticMidTermRetriever(MidTermRetriever):
    """Record production stages without owning or reimplementing retrieval."""

    def __init__(self, midterm_memory: Any, config: Any, *, reranker: Any = None):
        super().__init__(midterm_memory, config, reranker=reranker)
        self.last_search_diagnostics: dict[str, Any] = {}

    @staticmethod
    def _serialized(value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): DiagnosticMidTermRetriever._serialized(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [DiagnosticMidTermRetriever._serialized(item) for item in value]
        if hasattr(value, "id") and hasattr(value, "payload"):
            return {
                "id": str(value.id),
                "score": float(getattr(value, "score", 0.0) or 0.0),
                "payload": deepcopy(getattr(value, "payload", None) or {}),
            }
        return deepcopy(value)

    def _on_stage(self, stage_name: str, payload: Any) -> None:
        if stage_name == "search_started":
            self.last_search_diagnostics = {}
        serialized = self._serialized(payload)
        self.last_search_diagnostics[stage_name] = serialized
        aliases = {
            "session_candidates": "selected_sessions",
            "routed_page_candidates": "routed_page_pool",
            "global_supplement": "global_supplement_pool",
            "deduplicated_candidates": "deduplicated_candidate_pool",
            "scored_candidates": "pre_threshold_ranking",
            "final_selection": "final_visible_pages",
        }
        if stage_name in aliases:
            self.last_search_diagnostics[aliases[stage_name]] = deepcopy(serialized)
        if stage_name == "threshold_candidates" and isinstance(serialized, dict):
            passed = list(serialized.get("candidates") or [])
            passed_ids = {str(row.get("id") or "") for row in passed}
            ranked = list(self.last_search_diagnostics.get("reranked_candidates") or [])
            self.last_search_diagnostics["threshold"] = serialized.get("threshold")
            self.last_search_diagnostics["threshold_filtered_candidates"] = [
                row for row in ranked if str(row.get("id") or "") not in passed_ids
            ]
