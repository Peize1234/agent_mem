import copy
import logging
import math
import threading
from typing import Any, Dict, List, Optional

from mem0.memory.memory_evolution import unique_ids
from mem0.utils.factory import VectorStoreFactory
from mem0.utils.timestamps import beijing_now_iso

logger = logging.getLogger(__name__)


def vector_rows(listed) -> List[Any]:
    if isinstance(listed, (list, tuple)) and listed and isinstance(listed[0], list):
        return listed[0]
    if isinstance(listed, (list, tuple)):
        return list(listed)
    return []


def derived_output_is_visible(payload: Dict[str, Any]) -> bool:
    """Manual rows are committed; background rows require an explicit commit."""
    return not payload.get("source_job_id") or payload.get("output_state") == "committed"


def keyword_overlap(left: List[str], right: List[str]) -> float:
    left_set = {str(item).strip().lower() for item in left or [] if str(item).strip()}
    right_set = {str(item).strip().lower() for item in right or [] if str(item).strip()}
    if not left_set or not right_set:
        return 0.0
    return len(left_set & right_set) / len(left_set | right_set)


def compute_recency(
    last_visit_turn_index: int,
    current_turn_index: int,
    tau_turns: float,
) -> float:
    """Return session visit recency from conversation distance, never wall time."""
    distance_turns = max(float(current_turn_index) - float(last_visit_turn_index), 0.0)
    tau = float(tau_turns)
    if tau <= 0:
        raise ValueError("tau_turns must be positive")
    return math.exp(-distance_turns / tau)


def compute_session_heat(
    payload: Dict[str, Any],
    config,
    current_turn_index: int,
) -> float:
    recency = compute_recency(
        int(payload["last_visit_turn_index"]),
        current_turn_index,
        config.heat_recency_tau_turns,
    )
    return (
        config.heat_alpha * float(payload["N_visit"])
        + config.heat_beta * float(payload["L_interaction"])
        + config.heat_gamma * recency
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
        current_turn_index_provider,
        primary_vector_store=None,
        output_is_visible=None,
        vector_store_timeout_seconds: Optional[float] = None,
    ):
        self.provider = provider
        self.base_vector_config = base_vector_config
        self.base_collection_name = base_collection_name
        self.embedding_model = embedding_model
        self.config = config
        self.current_turn_index_provider = current_turn_index_provider
        self.primary_vector_store = primary_vector_store
        self.output_is_visible = output_is_visible or derived_output_is_visible
        self.vector_store_timeout_seconds = vector_store_timeout_seconds
        self.pages_collection_name = f"{base_collection_name}_midterm_pages"
        self.sessions_collection_name = f"{base_collection_name}_midterm_sessions"
        self._evolution_lock = threading.RLock()
        self._reserved_sequence_max: Dict[tuple[tuple[str, str], ...], int] = {}
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
        return [row for row in rows if self.output_is_visible(getattr(row, "payload", None) or {})][:top_k]

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
        return [row for row in rows if self.output_is_visible(getattr(row, "payload", None) or {})][:top_k]

    def delete_session(self, session_id: str) -> None:
        self.sessions_store.delete(vector_id=session_id)

    @staticmethod
    def _sequence_scope(filters: Dict[str, Any]) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (key, str(value))
                for key, value in (filters or {}).items()
                if key in ("user_id", "agent_id", "run_id") and value not in (None, "")
            )
        )

    def reserve_page_sequences(self, filters: Dict[str, Any], count: int) -> List[int]:
        """Reserve stable, monotonically increasing Page order within one conversation scope."""
        count = max(int(count), 0)
        if count == 0:
            return []
        scope = self._sequence_scope(filters)
        with self._evolution_lock:
            rows = self.list_pages(filters=dict(scope), top_k=10000, include_uncommitted=True)
            stored_max = max(
                (
                    int(payload["page_sequence"])
                    for row in rows
                    if (payload := (getattr(row, "payload", None) or {})).get("page_sequence") is not None
                ),
                default=0,
            )
            start = max(stored_max, self._reserved_sequence_max.get(scope, 0)) + 1
            self._reserved_sequence_max[scope] = start + count - 1
            return list(range(start, start + count))

    def current_turn_index(self, filters: Dict[str, Any]) -> int:
        """Return the latest persisted conversation turn for a Session scope."""
        return int(self.current_turn_index_provider(filters))

    def record_valid_recalls(
        self,
        page_ids: List[str],
        *,
        recall_turn_index: int,
        recalled_at: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Move Page and Session recall anchors after they enter model context."""
        normalized_page_ids = unique_ids(page_ids)
        if not normalized_page_ids:
            return []

        now = recalled_at or beijing_now_iso()
        current_index = int(recall_turn_index)
        updated_sessions: List[Dict[str, Any]] = []
        with self._evolution_lock:
            session_ids: List[tuple[Any, int]] = []
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
                        "last_recall_turn_index": current_index,
                        "updated_at": now,
                    }
                )
                self.update_page(page_id, payload, reembed=False)
                session_ids.append((payload.get("session_id"), current_index))

            session_turn_indices = {
                session_id: current_index for session_id, current_index in session_ids if session_id not in (None, "")
            }
            for session_id in unique_ids(session_turn_indices):
                session = self.get_session(session_id)
                if not session:
                    continue
                payload = dict(getattr(session, "payload", None) or {})
                count = int(payload.get("valid_recall_count", 0) or 0) + 1
                visit_count = int(payload.get("N_visit", 0) or 0) + 1
                current_index = session_turn_indices[session_id]
                payload.update(
                    {
                        "valid_recall_count": count,
                        "N_visit": visit_count,
                        "last_recall_at": now,
                        "last_visit_turn_index": current_index,
                        "R_recency": 1.0,
                        "updated_at": now,
                    }
                )
                payload["H_segment"] = compute_session_heat(payload, self.config, current_index)
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
