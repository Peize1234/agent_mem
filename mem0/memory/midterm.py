import copy
import logging
import math
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from mem0.memory.memory_evolution import memory_strength, unique_ids
from mem0.utils.factory import VectorStoreFactory
from mem0.utils.timestamps import BEIJING_TIMEZONE, beijing_now_iso

logger = logging.getLogger(__name__)


def vector_rows(listed) -> List[Any]:
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
        return listed[0]
    if isinstance(listed, (list, tuple)):
        return list(listed)
    return []


def derived_output_is_visible(payload: Dict[str, Any]) -> bool:
    """Manual/legacy rows are committed; background rows require an explicit commit."""
    return not payload.get("source_job_id") or payload.get("output_state") == "committed"


def keyword_overlap(left: List[str], right: List[str]) -> float:
    left_set = {str(item).strip().lower() for item in left or [] if str(item).strip()}
    right_set = {str(item).strip().lower() for item in right or [] if str(item).strip()}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def compute_recency(last_visit_time: Optional[str], now: Optional[str] = None, tau_hours: float = 24.0) -> float:
    if not last_visit_time:
        return 1.0
    try:
        current = datetime.fromisoformat(now or beijing_now_iso())
        previous = datetime.fromisoformat(last_visit_time)
        if current.tzinfo is None:
            current = current.replace(tzinfo=BEIJING_TIMEZONE)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=BEIJING_TIMEZONE)
        elapsed_hours = max((current - previous).total_seconds() / 3600.0, 0.0)
    except (TypeError, ValueError):
        return 1.0
    return math.exp(-elapsed_hours / tau_hours)


def compute_session_heat(payload: Dict[str, Any], config) -> float:
    return (
        config.heat_alpha * float(payload.get("N_visit", 0) or 0)
        + config.heat_beta * float(payload.get("L_interaction", 0) or 0)
        + config.heat_gamma * float(payload.get("R_recency", 0) or 0)
    )


class MidTermMemory:
    def __init__(
        self,
        *,
        provider: str,
        base_vector_config,
        base_collection_name: str,
        embedding_model,
        config,
        primary_vector_store=None,
        output_is_visible=None,
        vector_store_timeout_seconds: Optional[float] = None,
    ):
        self.provider = provider
        self.base_vector_config = base_vector_config
        self.base_collection_name = base_collection_name
        self.embedding_model = embedding_model
        self.config = config
        self.primary_vector_store = primary_vector_store
        self.output_is_visible = output_is_visible or derived_output_is_visible
        self.vector_store_timeout_seconds = vector_store_timeout_seconds
        self.pages_collection_name = f"{base_collection_name}_midterm_pages"
        self.sessions_collection_name = f"{base_collection_name}_midterm_sessions"
        self._evolution_lock = threading.RLock()
        self.pages_store = self._create_store(self.pages_collection_name)
        self.sessions_store = self._create_store(self.sessions_collection_name)

    def _base_config_dict(self) -> Dict[str, Any]:
        if isinstance(self.base_vector_config, dict):
            return copy.deepcopy(self.base_vector_config)
        if hasattr(self.base_vector_config, "model_dump"):
            try:
                return self.base_vector_config.model_dump()
            except Exception:
                logger.debug("Vector config model_dump failed for midterm store", exc_info=True)
        return copy.deepcopy(getattr(self.base_vector_config, "__dict__", {}))

    def _create_store(self, collection_name: str):
        config = self._base_config_dict()
        config["collection_name"] = collection_name

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
    def page_embedding_text(payload: Dict[str, Any]) -> str:
        keywords = payload.get("keywords") or []
        if isinstance(keywords, list):
            keywords_text = ", ".join(str(item) for item in keywords)
        else:
            keywords_text = str(keywords)
        return "\n".join(
            part
            for part in [
                payload.get("summary", ""),
                f"Keywords: {keywords_text}" if keywords_text else "",
                f"User: {payload.get('user_input', '')}",
            ]
            if part
        )

    def _stored_page_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        embedding_text = self.page_embedding_text(payload)
        return {
            **payload,
            "data": embedding_text,
            "text_lemmatized": embedding_text.lower(),
            "source": "mid_term_page",
        }

    @staticmethod
    def session_embedding_text(payload: Dict[str, Any]) -> str:
        keywords = payload.get("summary_keywords") or []
        if isinstance(keywords, list):
            keywords_text = ", ".join(str(item) for item in keywords)
        else:
            keywords_text = str(keywords)
        return "\n".join(
            part
            for part in [
                payload.get("summary", ""),
                f"Keywords: {keywords_text}" if keywords_text else "",
            ]
            if part
        )

    def _stored_session_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        embedding_text = self.session_embedding_text(payload)
        return {
            **payload,
            "data": embedding_text,
            "text_lemmatized": embedding_text.lower(),
            "source": "mid_term_session",
        }

    def insert_page(self, page_id: str, payload: Dict[str, Any]) -> None:
        embedding_text = self.page_embedding_text(payload)
        vector = self.embedding_model.embed(embedding_text, "add")
        self.pages_store.insert(vectors=[vector], ids=[page_id], payloads=[self._stored_page_payload(payload)])

    def update_page(self, page_id: str, payload: Dict[str, Any], reembed: bool = False) -> None:
        vector = self.embedding_model.embed(self.page_embedding_text(payload), "update") if reembed else None
        self.pages_store.update(vector_id=page_id, vector=vector, payload=self._stored_page_payload(payload))

    def get_page(self, page_id: str):
        return self.pages_store.get(vector_id=page_id)

    def list_pages(
        self,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 1000,
        *,
        include_uncommitted: bool = False,
    ) -> List[Any]:
        rows = vector_rows(self.pages_store.list(filters=filters, top_k=max(top_k * 4, top_k)))
        if not include_uncommitted:
            rows = [row for row in rows if self.output_is_visible(getattr(row, "payload", None) or {})]
        return rows[:top_k]

    def search_pages(self, query: str, filters: Optional[Dict[str, Any]] = None, top_k: int = 5) -> List[Any]:
        vector = self.embedding_model.embed(query, "search")
        rows = self.pages_store.search(
            query=query,
            vectors=vector,
            top_k=max(top_k * 4, top_k),
            filters=filters,
        )
        return [
            row
            for row in rows
            if self.output_is_visible(getattr(row, "payload", None) or {})
        ][:top_k]

    def delete_page(self, page_id: str) -> None:
        self.pages_store.delete(vector_id=page_id)

    def insert_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        embedding_text = self.session_embedding_text(payload)
        vector = self.embedding_model.embed(embedding_text, "add")
        self.sessions_store.insert(vectors=[vector], ids=[session_id], payloads=[self._stored_session_payload(payload)])

    def update_session(self, session_id: str, payload: Dict[str, Any], reembed: bool = False) -> None:
        vector = self.embedding_model.embed(self.session_embedding_text(payload), "update") if reembed else None
        self.sessions_store.update(vector_id=session_id, vector=vector, payload=self._stored_session_payload(payload))

    def get_session(self, session_id: str):
        return self.sessions_store.get(vector_id=session_id)

    def list_sessions(
        self,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 1000,
        *,
        include_uncommitted: bool = False,
    ) -> List[Any]:
        rows = vector_rows(self.sessions_store.list(filters=filters, top_k=max(top_k * 4, top_k)))
        if not include_uncommitted:
            rows = [row for row in rows if self.output_is_visible(getattr(row, "payload", None) or {})]
        return rows[:top_k]

    def search_sessions(
        self,
        query: str,
        filters: Optional[Dict[str, Any]] = None,
        top_k: int = 5,
        *,
        include_uncommitted: bool = False,
    ) -> List[Any]:
        vector = self.embedding_model.embed(query, "search")
        rows = self.sessions_store.search(
            query=query,
            vectors=vector,
            top_k=max(top_k * 4, top_k),
            filters=filters,
        )
        if include_uncommitted:
            return rows[:top_k]
        return [
            row
            for row in rows
            if self.output_is_visible(getattr(row, "payload", None) or {})
        ][:top_k]

    def delete_session(self, session_id: str) -> None:
        self.sessions_store.delete(vector_id=session_id)

    def record_valid_recalls(
        self,
        page_ids: List[str],
        *,
        recalled_at: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Reinforce unique pages and their sessions after they enter model context."""
        normalized_page_ids = unique_ids(page_ids)
        if not normalized_page_ids:
            return []

        now = recalled_at or beijing_now_iso()
        updated_sessions: List[Dict[str, Any]] = []
        with self._evolution_lock:
            session_ids: List[str] = []
            for page_id in normalized_page_ids:
                page = self.get_page(page_id)
                if not page:
                    continue
                payload = dict(getattr(page, "payload", None) or {})
                count = int(payload.get("valid_recall_count", 0) or 0) + 1
                payload.update(
                    {
                        "valid_recall_count": count,
                        "last_recall_at": now,
                        "memory_strength": memory_strength(count, self.config.reinforcement_gain),
                        "updated_at": now,
                    }
                )
                self.update_page(page_id, payload, reembed=False)
                session_ids.append(payload.get("session_id"))

            for session_id in unique_ids(session_ids):
                session = self.get_session(session_id)
                if not session:
                    continue
                payload = dict(getattr(session, "payload", None) or {})
                count = int(payload.get("valid_recall_count", payload.get("N_visit", 0)) or 0) + 1
                payload.update(
                    {
                        "valid_recall_count": count,
                        "N_visit": count,
                        "last_recall_at": now,
                        "last_visit_time": now,
                        "R_recency": 1.0,
                        "memory_strength": memory_strength(count, self.config.reinforcement_gain),
                        "updated_at": now,
                    }
                )
                payload["H_segment"] = compute_session_heat(payload, self.config)
                self.update_session(session_id, payload, reembed=False)
                updated_sessions.append({"id": session_id, **payload})

        return updated_sessions

    def reset(self) -> None:
        for store_name, store in (("midterm_pages", self.pages_store), ("midterm_sessions", self.sessions_store)):
            try:
                if hasattr(store, "delete_col"):
                    store.delete_col()
                elif hasattr(store, "reset"):
                    logger.warning("%s store does not expose delete_col; falling back to reset.", store_name)
                    store.reset()
                else:
                    logger.warning("%s store does not support delete_col or reset.", store_name)
            except Exception as exc:
                logger.warning("Failed to delete %s collection: %s", store_name, exc)
