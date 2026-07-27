from __future__ import annotations

from typing import Any, Dict, List, Optional


class VectorStoreMonitorRepository:
    """Reads memory collections only through Mem0 vector-store interfaces."""

    MAX_PAGE_SIZE = 200
    MAX_SCAN_ROWS = 5000

    def __init__(self, memory=None, *, max_payload_length: int = 4000):
        self.memory = memory
        self.max_payload_length = max(int(max_payload_length), 100)

    def available_collections(self) -> List[str]:
        if self.memory is None:
            return []
        collections = ["longterm"]
        if getattr(self.memory, "_midterm_memory", None) is not None or self.memory._midterm_enabled():
            collections.extend(["midterm_pages", "midterm_sessions"])
        if getattr(self.memory, "_entity_store", None) is not None:
            collections.append("entities")
        return collections

    def page(
        self,
        collection: str,
        *,
        page: int = 1,
        page_size: int = 50,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if self.memory is None:
            return {"items": [], "page": page, "page_size": page_size, "total": 0, "unavailable": True}
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), self.MAX_PAGE_SIZE)
        scan_limit = min(page * page_size, self.MAX_SCAN_ROWS)
        rows = self._list(collection, filters or {}, scan_limit)
        start = (page - 1) * page_size
        items = [self._serialize_row(row) for row in rows[start : start + page_size]]
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": len(rows),
            "has_more": len(rows) == scan_limit,
        }

    def session_tree(
        self,
        *,
        filters: Optional[Dict[str, Any]] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        sessions = self.page(
            "midterm_sessions",
            page=page,
            page_size=page_size,
            filters=filters,
        )
        trees = []
        for session in sessions["items"]:
            page_ids = session.get("payload", {}).get("page_ids") or []
            pages = []
            for page_id in page_ids[: self.MAX_PAGE_SIZE]:
                row = self.memory.midterm_memory.get_page(page_id)
                if row is not None:
                    pages.append(self._serialize_row(row))
            trees.append({"session": session, "pages": pages})
        return {**sessions, "items": trees}

    def _list(self, collection: str, filters: Dict[str, Any], top_k: int) -> List[Any]:
        filters = dict(filters)
        trace_id = filters.pop("trace_id", None)
        if collection == "longterm":
            if trace_id:
                filters["_mem0_trace_id"] = trace_id
            return self._rows(self.memory.vector_store.list(filters=filters or None, top_k=top_k))
        if collection == "midterm_pages":
            if trace_id:
                filters["trace_id"] = trace_id
            return self.memory.midterm_memory.list_pages(filters=filters or None, top_k=top_k)
        if collection == "midterm_sessions":
            rows = self.memory.midterm_memory.list_sessions(
                filters=filters or None,
                top_k=self.MAX_SCAN_ROWS if trace_id else top_k,
            )
            if trace_id:
                rows = [
                    row
                    for row in rows
                    if trace_id
                    in {
                        (getattr(row, "payload", None) or {}).get("created_trace_id"),
                        (getattr(row, "payload", None) or {}).get("last_updated_trace_id"),
                    }
                ]
            return rows[:top_k]
        if collection == "entities":
            if trace_id:
                filters["trace_id"] = trace_id
            store = getattr(self.memory, "_entity_store", None)
            if store is None:
                return []
            return self._rows(store.list(filters=filters or None, top_k=top_k))
        raise ValueError(f"Unsupported vector collection: {collection}")

    @staticmethod
    def _rows(listed) -> List[Any]:
        if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
            return list(listed[0])
        if isinstance(listed, (list, tuple)):
            return list(listed)
        return []

    def _serialize_row(self, row: Any) -> Dict[str, Any]:
        payload = dict(getattr(row, "payload", None) or {})
        return {
            "id": str(getattr(row, "id", "")),
            "score": getattr(row, "score", None),
            "payload": self._truncate(payload),
        }

    def _truncate(self, value: Any) -> Any:
        if isinstance(value, str) and len(value) > self.max_payload_length:
            return f"{value[: self.max_payload_length]}… [truncated {len(value) - self.max_payload_length} chars]"
        if isinstance(value, dict):
            return {key: self._truncate(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._truncate(item) for item in value]
        return value
