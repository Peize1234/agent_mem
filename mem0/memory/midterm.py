import copy
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from mem0.memory.memory_evolution import unique_ids
from mem0.utils.factory import VectorStoreFactory
from mem0.utils.retrieval_fusion import normalized_score_fuse, rrf_fuse
from mem0.utils.timestamps import beijing_now_iso

logger = logging.getLogger(__name__)


@dataclass
class MidTermSearchResult:
    id: str
    payload: Dict[str, Any]
    score: float


PAGE_REPRESENTATION_ALIASES = {
    "P0": "production",
    "P1": "summary",
    "P2": "user",
    "P3": "raw_dialogue",
    "P4": "user_assistant",
    "P5": "user_summary",
    "P6": "summary_raw",
    "P7": "user_keywords",
    "P8": "summary_keywords",
}


def page_embedding_text(payload: Dict[str, Any], representation: str = "production") -> str:
    """Build the production Page embedding text for one configured representation."""
    representation = PAGE_REPRESENTATION_ALIASES.get(str(representation), str(representation))
    summary = str(payload.get("summary") or "").strip()
    user = str(payload.get("user_input") or "").strip()
    assistant = str(payload.get("assistant_response") or "").strip()
    raw = str(payload.get("raw_dialogue") or "").strip()
    keywords = payload.get("keywords") or []
    keywords_text = ", ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords)
    keyword_line = f"Keywords: {keywords_text}" if keywords_text else ""
    # Keep the historical Production/P0 representation exact, including the
    # role marker when a malformed Page has an empty field.
    user_line = f"User: {user}"
    assistant_line = f"Assistant: {assistant}"

    variants = {
        "production": (summary, keyword_line, user_line),
        "summary": (summary,),
        "user": (user,),
        "raw_dialogue": (raw,),
        "user_assistant": (user_line, assistant_line),
        "user_summary": (user_line, summary),
        "summary_raw": (summary, raw),
        "user_keywords": (user_line, keyword_line),
        "summary_keywords": (summary, keyword_line),
    }
    if representation not in variants:
        raise ValueError(f"Unknown Page representation: {representation}")
    return "\n".join(part for part in variants[representation] if part)


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

    def page_embedding_text(self, payload: Dict[str, Any]) -> str:
        return page_embedding_text(payload, getattr(self.config, "page_representation", "production"))

    @staticmethod
    def page_field_texts(payload: Dict[str, Any]) -> Dict[str, str]:
        keywords = payload.get("keywords") or []
        return {
            "summary": str(payload.get("summary") or ""),
            "keywords": " ".join(str(item) for item in keywords) if isinstance(keywords, list) else str(keywords),
            "user_input": str(payload.get("user_input") or ""),
            "raw_dialogue": str(payload.get("raw_dialogue") or ""),
        }

    def _with_page_field_vectors(self, payload: Dict[str, Any], memory_action: str) -> Dict[str, Any]:
        reranker = getattr(self.config, "reranker", None)
        if reranker is None or reranker.method != "multi_vector_maxsim":
            return payload
        field_vectors = {
            field: self.embedding_model.embed(text, memory_action)
            for field, text in self.page_field_texts(payload).items()
            if text
        }
        return {**payload, "_field_vectors": field_vectors}

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
        stored_payload = self._with_page_field_vectors(self._stored_page_payload(payload), "add")
        self.pages_store.insert(vectors=[vector], ids=[page_id], payloads=[stored_payload])

    def update_page(self, page_id: str, payload: Dict[str, Any], reembed: bool = False) -> None:
        vector = self.embedding_model.embed(self.page_embedding_text(payload), "update") if reembed else None
        stored_payload = self._stored_page_payload(payload)
        if reembed:
            stored_payload = self._with_page_field_vectors(stored_payload, "update")
        self.pages_store.update(vector_id=page_id, vector=vector, payload=stored_payload)

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
        fetch_k = max(top_k * 4, top_k)
        rows = self.pages_store.search(
            query=query,
            vectors=vector,
            top_k=fetch_k,
            filters=filters,
        )
        dense = [row for row in rows if self.output_is_visible(getattr(row, "payload", None) or {})]
        if getattr(self.config, "retrieval_method", "dense") == "dense":
            return dense[:top_k]

        try:
            sparse_rows = self.pages_store.keyword_search(query=query, top_k=fetch_k, filters=filters)
        except Exception as exc:
            logger.warning("MidTerm BM25 Page retrieval failed; using dense candidates: %s", exc)
            return dense[:top_k]
        sparse = [row for row in (sparse_rows or []) if self.output_is_visible(getattr(row, "payload", None) or {})]
        dense_values = [{"id": str(row.id), "score": float(getattr(row, "score", 0.0) or 0.0)} for row in dense]
        sparse_values = [{"id": str(row.id), "score": float(getattr(row, "score", 0.0) or 0.0)} for row in sparse]
        if getattr(self.config, "fusion_method", "normalized_score") == "rrf":
            fused = rrf_fuse(
                [dense_values, sparse_values],
                rank_constant=int(getattr(self.config, "rrf_rank_constant", 60)),
            )
        else:
            fused = normalized_score_fuse(
                dense_values,
                sparse_values,
                dense_weight=float(getattr(self.config, "dense_weight", 0.7)),
            )
        payload_by_id = {str(row.id): dict(getattr(row, "payload", None) or {}) for row in [*dense, *sparse]}
        return [
            MidTermSearchResult(
                id=str(row["id"]),
                payload=payload_by_id[str(row["id"])],
                score=float(row["score"]),
            )
            for row in fused[:top_k]
            if str(row["id"]) in payload_by_id
        ]

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
