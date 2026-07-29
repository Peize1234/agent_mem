from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from typing import Any, Dict, Optional

from mem0.memory.main import Memory, _build_answer_prompt_messages
from memory_monitor.runtime.demo_background_worker import DemoBackgroundWorkerManager


class DemoMemory(Memory):
    """Memory runtime with explicit, isolated controls for the demo lab."""

    def __init__(self, config):
        self._demo_events: list[Dict[str, Any]] = []
        self._demo_events_lock = threading.Lock()
        super().__init__(config)

    def _create_background_worker_manager(self) -> DemoBackgroundWorkerManager:
        return DemoBackgroundWorkerManager(
            self.db,
            self._background_config(),
            process_midterm=self._background_process_midterm,
            process_longterm=self._background_process_longterm,
            process_profile=self._background_process_profile,
            commit_migration_outputs=self._commit_migration_stage_outputs,
            discard_migration_outputs=self._discard_migration_stage_outputs,
            startup_cleanup=self._cleanup_orphan_staging_outputs,
            event_recorder=self._record_demo_event,
        )

    def _with_midterm_search_results(self, query, filters, long_term_memories):
        """Add mid-term results without mutating visit counters during demo retrieval."""
        if not self._midterm_enabled():
            return long_term_memories
        results = [{**memory, "source": "long_term"} for memory in long_term_memories]
        results.extend(self.midterm_retriever.search(query, filters, record_visits=False))
        return results

    def retrieve_context_for_demo(
        self,
        query: str,
        *,
        user_id: str,
        session_id: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Retrieve and freeze layered context without invoking the answer model."""
        effective_session_id = session_id or user_id
        context = deepcopy(
            self._retrieve_context(
                query,
                user_id=user_id,
                session_id=effective_session_id,
                **kwargs,
            )
        )
        memories = context.get("retrieved_memories") or []
        context["short_term"] = deepcopy(context.get("short_term_messages") or [])
        context["mid_term"] = [
            deepcopy(item)
            for item in memories
            if isinstance(item, dict) and str(item.get("source") or "").startswith("mid_term")
        ]
        context["long_term"] = [
            deepcopy(item)
            for item in memories
            if isinstance(item, dict) and not str(item.get("source") or "").startswith("mid_term")
        ]
        context["user_profile"] = deepcopy(context.get("profile") or {})
        context["context_hash"] = self.context_hash(context)
        return context

    def build_prompt_from_context(
        self,
        context: Dict[str, Any],
        *,
        reference_information: Any = None,
    ) -> list[Dict[str, str]]:
        """Build answer-model messages from the supplied frozen context only."""
        frozen_context = deepcopy(context)
        expected_hash = frozen_context.pop("context_hash", None)
        actual_hash = self.context_hash(frozen_context)
        if expected_hash is not None and expected_hash != actual_hash:
            raise ValueError("Frozen demo context no longer matches its context_hash")
        return deepcopy(_build_answer_prompt_messages(frozen_context, reference_information))

    def generate_response_for_demo(
        self,
        prompt_messages: list[Dict[str, str]],
        **kwargs: Any,
    ) -> Any:
        """Send exactly the displayed prompt messages to the configured model."""
        return self.llm.generate_response(messages=deepcopy(prompt_messages), **kwargs)

    def commit_demo_turn(
        self,
        *,
        simulation_id: str,
        turn_id: str,
        user_id: str,
        run_id: str,
        user_message: str,
        assistant_message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Commit one turn through the parent's persisted idempotency boundary."""
        commit_metadata = deepcopy(metadata) if metadata else {}
        idempotency_key = f"demo-turn:{simulation_id}:{turn_id}"
        return super().add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id=user_id,
            run_id=run_id,
            metadata=commit_metadata or None,
            idempotency_key=idempotency_key,
        )

    @staticmethod
    def context_hash(context: Dict[str, Any]) -> str:
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _record_demo_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        lock = getattr(self, "_demo_events_lock", None)
        if lock is None:
            lock = self._demo_events_lock = threading.Lock()
        with lock:
            self._demo_events.append({"event_type": event_type, **deepcopy(payload)})

    def demo_events(self) -> list[Dict[str, Any]]:
        lock = getattr(self, "_demo_events_lock", None)
        if lock is None:
            return deepcopy(self._demo_events)
        with lock:
            return deepcopy(self._demo_events)

    def close(self) -> bool:
        closed = super().close()
        if not closed:
            return False
        vector_store = getattr(self, "vector_store", None)
        client = getattr(vector_store, "client", None)
        close = getattr(client, "close", None)
        if callable(close) and not getattr(self, "_demo_vector_client_closed", False):
            close()
            self._demo_vector_client_closed = True
        return True

    @property
    def demo_background_worker(self) -> DemoBackgroundWorkerManager:
        """Return the manually controlled worker through a demo-only public API."""
        worker = self._ensure_background_workers()
        if not isinstance(worker, DemoBackgroundWorkerManager):
            raise RuntimeError("DemoMemory requires DemoBackgroundWorkerManager")
        return worker
