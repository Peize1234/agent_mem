import copy
import hashlib
import json
import logging
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from mem0.memory.midterm import vector_rows
from mem0.memory.memory_evolution import forgetting_factor, memory_strength, unique_ids
from mem0.utils.factory import VectorStoreFactory
from mem0.utils.timestamps import beijing_now_iso

logger = logging.getLogger(__name__)


def promotion_source_version(session_payload: Dict[str, Any]) -> str:
    """Hash only promoted content, excluding volatile recall and heat state."""
    source = {
        "summary": str(session_payload.get("summary") or "").strip(),
        "keywords": list(session_payload.get("summary_keywords") or []),
        "page_ids": unique_ids(session_payload.get("page_ids") or []),
    }
    canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
        self._memory_locks_guard = threading.Lock()
        self._memory_locks: Dict[str, threading.RLock] = {}
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

    def _retention_half_life_hours(self) -> float:
        configured = getattr(self.config, "cross_session_retention_half_life_hours", None)
        if configured is not None:
            return float(configured)
        legacy_midterm_half_life = getattr(self.config.midterm, "retention_half_life_hours", None)
        return float(legacy_midterm_half_life) * 4 if legacy_midterm_half_life is not None else 720.0

    def _retention_floor(self) -> float:
        return float(
            getattr(
                self.config,
                "cross_session_retention_floor",
                getattr(self.config.midterm, "retention_floor", 0.2),
            )
        )

    def _reinforcement_gain(self) -> float:
        configured = getattr(self.config, "cross_session_reinforcement_gain", None)
        if configured is not None:
            return float(configured)
        return float(getattr(self.config.midterm, "reinforcement_gain", 0.25))

    def _rag_threshold(self) -> float:
        return float(
            getattr(
                self.config,
                "cross_session_longterm_rag_threshold",
                getattr(self.config, "longterm_rag_threshold", 0.1),
            )
        )

    def _memory_lock(self, memory_id: str):
        """Return the stable per-record lock without holding the guard during I/O."""
        with self._memory_locks_guard:
            lock = self._memory_locks.get(memory_id)
            if lock is None:
                lock = threading.RLock()
                self._memory_locks[memory_id] = lock
            return lock

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

    def promote_session(
        self,
        session_id: str,
        midterm_memory,
        *,
        expected_source_version: Optional[str] = None,
        lease_is_current: Optional[Callable[[], bool]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Idempotently promote an eligible session using a deterministic record ID."""
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
        current_source_version = promotion_source_version(session_payload)
        if expected_source_version and current_source_version != expected_source_version:
            return None
        if lease_is_current is not None and not lease_is_current():
            raise RuntimeError("stale promotion job lease")

        user_id = session_payload.get("user_id")
        if user_id in (None, ""):
            return None
        user_id = str(user_id)
        memory_id = self._memory_id(user_id, session_id)
        summary = str(session_payload.get("summary") or "").strip()
        if not summary:
            return None
        page_ids = unique_ids(session_payload.get("page_ids") or [])
        keywords = list(session_payload.get("summary_keywords") or [])
        evidence = self._evidence(midterm_memory, page_ids)

        # Only promotions/recalls for this deterministic record serialize. The
        # lock-pool guard is never held across embedding or vector-store I/O.
        with self._memory_lock(memory_id):
            existing = self.get(memory_id)
            existing_payload = dict(getattr(existing, "payload", None) or {}) if existing else {}
            now = beijing_now_iso()
            if existing and all(
                (
                    existing_payload.get("memory") == summary,
                    existing_payload.get("keywords", []) == keywords,
                    existing_payload.get("source_page_ids", []) == page_ids,
                    existing_payload.get("evidence", []) == evidence,
                    existing_payload.get("source_version") == current_source_version,
                )
            ):
                return existing_payload
            cross_recall_count = int(existing_payload.get("recall_count", 0) or 0)
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
                "source_version": current_source_version,
                "evidence": evidence,
                "promoted_at": existing_payload.get("promoted_at") or now,
                "updated_at": now,
                "recall_count": cross_recall_count,
                "last_recall_at": existing_payload.get("last_recall_at"),
                "memory_strength": memory_strength(
                    cross_recall_count,
                    self._reinforcement_gain(),
                ),
            }
            stored_payload = self._stored_payload(payload)
            vector = self.embedding_model.embed(self._embedding_text(payload), "update" if existing else "add")
            if lease_is_current is not None and not lease_is_current():
                raise RuntimeError("stale promotion job lease")
            latest_session = midterm_memory.get_session(session_id)
            latest_payload = dict(getattr(latest_session, "payload", None) or {}) if latest_session else {}
            if promotion_source_version(latest_payload) != current_source_version:
                return None
            if existing:
                self.store.update(vector_id=memory_id, vector=vector, payload=stored_payload)
            else:
                self.store.insert(vectors=[vector], ids=[memory_id], payloads=[stored_payload])
            return payload

    def search(
        self,
        query: str,
        *,
        user_id: str,
        top_k: int,
        threshold: Optional[float] = None,
        now: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
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
        candidates = []
        for row in rows:
            raw_score = float(getattr(row, "score", 0.0) or 0.0)
            payload = getattr(row, "payload", None) or {}
            if str(payload.get("user_id") or "") != str(user_id):
                continue
            strength = memory_strength(
                payload.get("recall_count", 0) or 0,
                self._reinforcement_gain(),
            )
            retention = forgetting_factor(
                payload,
                self.config.midterm,
                now=now,
                recall_count_key="recall_count",
                anchor_keys=("last_recall_at", "promoted_at"),
                half_life_hours=self._retention_half_life_hours(),
                retention_floor=self._retention_floor(),
                reinforcement_gain=self._reinforcement_gain(),
            )
            final_score = raw_score * retention
            candidates.append(
                {
                    "id": str(row.id),
                    "memory": payload.get("memory") or payload.get("summary") or payload.get("data", ""),
                    "summary": payload.get("summary"),
                    "keywords": payload.get("keywords", []),
                    "score": final_score,
                    "raw_rag_score": raw_score,
                    "forgetting_factor": retention,
                    "memory_strength": strength,
                    "final_score": final_score,
                    "source": self.SOURCE,
                    "user_id": payload.get("user_id"),
                    "source_midterm_session_id": payload.get("source_midterm_session_id"),
                    "source_run_id": payload.get("source_run_id"),
                    "source_page_ids": payload.get("source_page_ids", []),
                    "source_version": payload.get("source_version"),
                    "promoted_at": payload.get("promoted_at"),
                    "created_at": payload.get("promoted_at"),
                    "updated_at": payload.get("updated_at"),
                    "recall_count": payload.get("recall_count", 0),
                    "last_recall_at": payload.get("last_recall_at"),
                }
            )
        candidates.sort(key=lambda item: item["final_score"], reverse=True)
        effective_threshold = self._rag_threshold() if threshold is None else float(threshold)
        return [item for item in candidates if item["raw_rag_score"] >= effective_threshold][:top_k]

    def record_valid_recalls(self, memory_ids: List[str], *, recalled_at: Optional[str] = None) -> None:
        now = recalled_at or beijing_now_iso()
        for memory_id in unique_ids(memory_ids):
            with self._memory_lock(memory_id):
                row = self.get(memory_id)
                if not row:
                    continue
                payload = dict(getattr(row, "payload", None) or {})
                payload["recall_count"] = int(payload.get("recall_count", 0) or 0) + 1
                payload["memory_strength"] = memory_strength(
                    payload["recall_count"],
                    self._reinforcement_gain(),
                )
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
