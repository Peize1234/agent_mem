import copy
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from mem0.memory.midterm import vector_rows
from mem0.memory.memory_evolution import unique_ids
from mem0.utils.factory import VectorStoreFactory
from mem0.utils.timestamps import beijing_now_iso

logger = logging.getLogger(__name__)


class CrossSessionLongTermMemory:
    """User-scoped promoted memories stored outside existing long-term memory."""

    SOURCE = "cross_session_long_term"

    def __init__(
        self,
        *,
        provider: str,
        base_vector_config,
        base_collection_name: str,
        embedding_model,
        config,
        primary_vector_store=None,
        vector_store_timeout_seconds: Optional[float] = None,
    ) -> None:
        self.provider = provider
        self.base_vector_config = base_vector_config
        self.embedding_model = embedding_model
        self.config = config
        self.primary_vector_store = primary_vector_store
        self.vector_store_timeout_seconds = vector_store_timeout_seconds
        self.collection_name = f"{base_collection_name}_cross_session_longterm"
        self._lock = threading.RLock()
        self.store = self._create_store()

    def _base_config_dict(self) -> Dict[str, Any]:
        if isinstance(self.base_vector_config, dict):
            return copy.deepcopy(self.base_vector_config)
        if hasattr(self.base_vector_config, "model_dump"):
            try:
                return self.base_vector_config.model_dump()
            except Exception:
                logger.debug("Vector config model_dump failed for cross-session store", exc_info=True)
        return copy.deepcopy(getattr(self.base_vector_config, "__dict__", {}))

    def _create_store(self):
        config = self._base_config_dict()
        config["collection_name"] = self.collection_name
        if self.provider == "qdrant" and self.primary_vector_store is not None:
            client = getattr(self.primary_vector_store, "client", None)
            if client is not None:
                config["client"] = client
        return VectorStoreFactory.create(
            self.provider,
            config,
            timeout_seconds=self.vector_store_timeout_seconds,
        )

    @staticmethod
    def _memory_id(user_id: str, session_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mem0:cross-session-longterm:{user_id}:{session_id}"))

    @staticmethod
    def _embedding_text(payload: Dict[str, Any]) -> str:
        keywords = payload.get("keywords") or []
        keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
        return "\n".join(
            part
            for part in [
                str(payload.get("memory") or payload.get("summary") or ""),
                f"Keywords: {keywords_text}" if keywords_text else "",
            ]
            if part
        )

    def _stored_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = self._embedding_text(payload)
        return {
            **payload,
            "data": data,
            "text_lemmatized": data.lower(),
            "source": self.SOURCE,
        }

    def get(self, memory_id: str):
        return self.store.get(vector_id=memory_id)

    def list(self, filters: Optional[Dict[str, Any]] = None, top_k: int = 1000) -> List[Any]:
        return vector_rows(self.store.list(filters=filters, top_k=top_k))[:top_k]

    def _evidence(self, midterm_memory, page_ids: List[str]) -> List[Dict[str, Any]]:
        evidence = []
        for page_id in unique_ids(page_ids):
            page = midterm_memory.get_page(page_id)
            if not page:
                continue
            payload = getattr(page, "payload", None) or {}
            evidence.append(
                {
                    "page_id": page_id,
                    "summary": payload.get("summary"),
                    "raw_dialogue": payload.get("raw_dialogue"),
                    "created_at": payload.get("created_at"),
                }
            )
        return evidence

    def promote_session(self, session_id: str, midterm_memory) -> Optional[Dict[str, Any]]:
        """Idempotently promote an eligible session using a deterministic record ID."""
        with self._lock:
            session = midterm_memory.get_session(session_id)
            if not session:
                return None
            session_payload = dict(getattr(session, "payload", None) or {})
            recall_count = int(session_payload.get("valid_recall_count", 0) or 0)
            absolute_heat = float(session_payload.get("H_segment", 0.0) or 0.0)
            if recall_count < int(self.config.midterm.promotion_min_recall_count):
                return None
            if absolute_heat < float(self.config.midterm.promotion_heat_threshold):
                return None

            user_id = session_payload.get("user_id")
            if user_id in (None, ""):
                return None
            user_id = str(user_id)
            memory_id = self._memory_id(user_id, session_id)
            existing = self.get(memory_id)
            existing_payload = dict(getattr(existing, "payload", None) or {}) if existing else {}
            now = beijing_now_iso()
            summary = str(session_payload.get("summary") or "").strip()
            if not summary:
                return None
            page_ids = unique_ids(session_payload.get("page_ids") or [])
            keywords = list(session_payload.get("summary_keywords") or [])
            evidence = self._evidence(midterm_memory, page_ids)
            if existing and all(
                (
                    existing_payload.get("memory") == summary,
                    existing_payload.get("keywords", []) == keywords,
                    existing_payload.get("source_page_ids", []) == page_ids,
                    existing_payload.get("evidence", []) == evidence,
                )
            ):
                return existing_payload
            payload = {
                "id": memory_id,
                "source": self.SOURCE,
                "user_id": user_id,
                "memory": summary,
                "summary": summary,
                "keywords": keywords,
                "source_midterm_session_id": str(session_id),
                "source_run_id": session_payload.get("run_id"),
                "source_page_ids": page_ids,
                "evidence": evidence,
                "promoted_at": existing_payload.get("promoted_at") or now,
                "updated_at": now,
                "recall_count": int(existing_payload.get("recall_count", 0) or 0),
                "last_recall_at": existing_payload.get("last_recall_at"),
            }
            stored_payload = self._stored_payload(payload)
            vector = self.embedding_model.embed(self._embedding_text(payload), "update" if existing else "add")
            if existing:
                self.store.update(vector_id=memory_id, vector=vector, payload=stored_payload)
            else:
                self.store.insert(vectors=[vector], ids=[memory_id], payloads=[stored_payload])
            return payload

    def search(self, query: str, *, user_id: str, top_k: int, threshold: float) -> List[Dict[str, Any]]:
        """Search only by user scope; source run IDs are evidence, never filters."""
        if not user_id or top_k <= 0:
            return []
        vector = self.embedding_model.embed(query, "search")
        rows = self.store.search(
            query=query,
            vectors=vector,
            top_k=max(top_k * 4, top_k),
            filters={"user_id": user_id},
        )
        results = []
        for row in rows:
            raw_score = float(getattr(row, "score", 0.0) or 0.0)
            if raw_score < float(threshold):
                continue
            payload = getattr(row, "payload", None) or {}
            results.append(
                {
                    "id": str(row.id),
                    "memory": payload.get("memory") or payload.get("summary") or payload.get("data", ""),
                    "summary": payload.get("summary"),
                    "keywords": payload.get("keywords", []),
                    "score": raw_score,
                    "raw_rag_score": raw_score,
                    "source": self.SOURCE,
                    "user_id": payload.get("user_id"),
                    "source_midterm_session_id": payload.get("source_midterm_session_id"),
                    "source_run_id": payload.get("source_run_id"),
                    "source_page_ids": payload.get("source_page_ids", []),
                    "promoted_at": payload.get("promoted_at"),
                    "created_at": payload.get("promoted_at"),
                    "updated_at": payload.get("updated_at"),
                    "recall_count": payload.get("recall_count", 0),
                    "last_recall_at": payload.get("last_recall_at"),
                }
            )
            if len(results) >= top_k:
                break
        return results

    def record_valid_recalls(self, memory_ids: List[str], *, recalled_at: Optional[str] = None) -> None:
        now = recalled_at or beijing_now_iso()
        with self._lock:
            for memory_id in unique_ids(memory_ids):
                row = self.get(memory_id)
                if not row:
                    continue
                payload = dict(getattr(row, "payload", None) or {})
                payload["recall_count"] = int(payload.get("recall_count", 0) or 0) + 1
                payload["last_recall_at"] = now
                payload["updated_at"] = now
                self.store.update(vector_id=memory_id, vector=None, payload=payload)

    def reset(self) -> None:
        if hasattr(self.store, "delete_col"):
            self.store.delete_col()
        elif hasattr(self.store, "reset"):
            self.store.reset()
        else:
            logger.warning("Cross-session long-term store does not support reset")
