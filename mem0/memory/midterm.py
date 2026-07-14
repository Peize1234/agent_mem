import copy
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mem0.utils.factory import VectorStoreFactory

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def vector_rows(listed) -> List[Any]:
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
        return listed[0]
    if isinstance(listed, (list, tuple)):
        return list(listed)
    return []


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
        current = datetime.fromisoformat(now or utc_now())
        previous = datetime.fromisoformat(last_visit_time)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
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
    ):
        self.provider = provider
        self.base_vector_config = base_vector_config
        self.base_collection_name = base_collection_name
        self.embedding_model = embedding_model
        self.config = config
        self.primary_vector_store = primary_vector_store
        self.pages_collection_name = f"{base_collection_name}_midterm_pages"
        self.sessions_collection_name = f"{base_collection_name}_midterm_sessions"
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

        return VectorStoreFactory.create(self.provider, config)

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

    def list_pages(self, filters: Optional[Dict[str, Any]] = None, top_k: int = 1000) -> List[Any]:
        return vector_rows(self.pages_store.list(filters=filters, top_k=top_k))

    def search_pages(self, query: str, filters: Optional[Dict[str, Any]] = None, top_k: int = 5) -> List[Any]:
        vector = self.embedding_model.embed(query, "search")
        return self.pages_store.search(query=query, vectors=vector, top_k=top_k, filters=filters)

    def insert_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        embedding_text = self.session_embedding_text(payload)
        vector = self.embedding_model.embed(embedding_text, "add")
        self.sessions_store.insert(vectors=[vector], ids=[session_id], payloads=[self._stored_session_payload(payload)])

    def update_session(self, session_id: str, payload: Dict[str, Any], reembed: bool = False) -> None:
        vector = self.embedding_model.embed(self.session_embedding_text(payload), "update") if reembed else None
        self.sessions_store.update(vector_id=session_id, vector=vector, payload=self._stored_session_payload(payload))

    def get_session(self, session_id: str):
        return self.sessions_store.get(vector_id=session_id)

    def list_sessions(self, filters: Optional[Dict[str, Any]] = None, top_k: int = 1000) -> List[Any]:
        return vector_rows(self.sessions_store.list(filters=filters, top_k=top_k))

    def search_sessions(self, query: str, filters: Optional[Dict[str, Any]] = None, top_k: int = 5) -> List[Any]:
        vector = self.embedding_model.embed(query, "search")
        return self.sessions_store.search(query=query, vectors=vector, top_k=top_k, filters=filters)

    def record_session_visit(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return None
        payload = dict(getattr(session, "payload", None) or {})
        now = utc_now()
        payload["N_visit"] = int(payload.get("N_visit", 0) or 0) + 1
        payload["R_recency"] = compute_recency(payload.get("last_visit_time"), now)
        payload["last_visit_time"] = now
        payload["updated_at"] = now
        payload["H_segment"] = compute_session_heat(payload, self.config)
        self.update_session(session_id, payload, reembed=False)
        return payload

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
