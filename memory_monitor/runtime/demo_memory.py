from __future__ import annotations

import hashlib
import json
import threading
from copy import deepcopy
from typing import Any, Dict, Optional

from mem0.memory.main import (
    Memory,
    _build_session_scope,
    build_answer_prompt_messages_from_context,
)
from memory_monitor.runtime.demo_background_worker import DemoBackgroundWorkerManager
from memory_monitor.runtime.llm_trace import TracedToolExecutor, ensure_traced_llm


class DemoMemory(Memory):
    """Memory runtime with explicit, isolated controls for the demo lab."""

    def __init__(self, config):
        self._demo_events: list[Dict[str, Any]] = []
        self._demo_events_lock = threading.Lock()
        self._demo_retrieval_warmup_lock = threading.Lock()
        self._demo_retrieval_warmed_up = False
        super().__init__(config)
        ensure_traced_llm(self)

    def _create_background_worker_manager(self) -> DemoBackgroundWorkerManager:
        return DemoBackgroundWorkerManager(
            self.db,
            self._background_config(),
            process_midterm=self._background_process_midterm,
            process_longterm=self._background_process_longterm,
            process_profile=self._background_process_profile,
            process_longterm_extraction=self._background_process_longterm_extraction,
            process_promotion=self._background_process_promotion,
            commit_migration_outputs=self._commit_migration_stage_outputs,
            discard_migration_outputs=self._discard_migration_stage_outputs,
            commit_longterm_extraction_outputs=self._commit_longterm_extraction_outputs,
            discard_longterm_extraction_outputs=self._discard_longterm_extraction_outputs,
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
        agentic_enabled = self.agentic_retrieval_enabled()
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
        context["fine_grained_longterm"] = [
            deepcopy(item)
            for item in memories
            if isinstance(item, dict) and item.get("source") == "long_term"
        ]
        context["promoted_longterm"] = [
            deepcopy(item)
            for item in memories
            if isinstance(item, dict) and item.get("source") == "cross_session_long_term"
        ]
        context["user_profile"] = deepcopy(context.get("profile") or {})
        if agentic_enabled:
            context["agentic_retrieval"] = True
        context["context_hash"] = self.context_hash(context)
        return context

    def warm_up_retrieval_for_demo(self) -> bool:
        """Warm the Demo's read-only retrieval path once for this memory instance."""
        with self._demo_retrieval_warmup_lock:
            if self._demo_retrieval_warmed_up:
                return False
            for query in ("金融分析预热", "retrieval warmup"):
                self.retrieve_context_for_demo(
                    query,
                    user_id="__demo_warmup_user__",
                    session_id="__demo_warmup_session__",
                )
            self._demo_retrieval_warmed_up = True
            return True

    def build_prompt_from_context(
        self,
        context: Dict[str, Any],
        *,
        reference_information: Any = None,
        agentic_memory_supplement: Optional[str] = None,
        agentic_answer: Optional[str] = None,
    ) -> list[Dict[str, str]]:
        """Build answer-model messages from the supplied frozen context only."""
        frozen_context = self._validated_frozen_context(context)
        if not agentic_memory_supplement and agentic_answer:
            agentic_memory_supplement = agentic_answer
        return deepcopy(
            build_answer_prompt_messages_from_context(
                frozen_context,
                reference_information,
                agentic_memory_supplement=agentic_memory_supplement or "",
            )
        )

    def generate_response_for_demo(
        self,
        prompt_messages: list[Dict[str, str]],
        **kwargs: Any,
    ) -> Any:
        """Send exactly the displayed prompt messages to the configured model."""
        return self.llm.generate_response(messages=deepcopy(prompt_messages), **kwargs)

    def generate_agentic_response_for_demo(
        self,
        context: Dict[str, Any],
        *,
        reference_information: Any = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Run the core Agentic flow from the exact frozen retrieval context."""
        frozen_context = self._validated_frozen_context(context)
        return self._run_agentic_retrieval_from_context(
            frozen_context,
            reference_information=reference_information,
            generation_kwargs=kwargs,
            record_midterm_visits=False,
        )

    def _create_agentic_tool_executor(
        self,
        *,
        user_id: str,
        session_id: str,
        record_midterm_visits: bool,
        exclude_midterm_page_ids: Optional[set[str]] = None,
    ) -> TracedToolExecutor:
        """Decorate the core executor only for this Demo instance's active trace."""
        executor_kwargs = {
            "user_id": user_id,
            "session_id": session_id,
            "record_midterm_visits": record_midterm_visits,
        }
        if exclude_midterm_page_ids is not None:
            executor_kwargs["exclude_midterm_page_ids"] = exclude_midterm_page_ids
        return TracedToolExecutor(
            super()._create_agentic_tool_executor(**executor_kwargs)
        )

    def _validated_frozen_context(self, context: Dict[str, Any]) -> Dict[str, Any]:
        frozen_context = deepcopy(context)
        expected_hash = frozen_context.pop("context_hash", None)
        # Step-level trace data is monitor metadata added after retrieval. It
        # must remain visible in the persisted output without entering the
        # production prompt/Agentic context or invalidating the frozen hash.
        frozen_context.pop("llm_calls", None)
        frozen_context.pop("tool_calls", None)
        actual_hash = self.context_hash(frozen_context)
        if expected_hash is not None and expected_hash != actual_hash:
            raise ValueError("Frozen demo context no longer matches its context_hash")
        return frozen_context

    def agentic_retrieval_enabled(self) -> bool:
        config = getattr(getattr(self, "config", None), "agentic_retrieval", None)
        return bool(config and config.enabled)

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
        result = super().add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id=user_id,
            run_id=run_id,
            metadata=commit_metadata or None,
            idempotency_key=idempotency_key,
        )
        # ``Memory.add`` intentionally returns only the historical migration
        # and profile IDs.  Read the extraction IDs back from the same core DB
        # so the Demo can explain every job created by this call without
        # maintaining a parallel state store.
        background = result.setdefault("background", {})
        try:
            extraction_jobs = self.db.list_longterm_extraction_jobs(
                session_scope=self.session_scope_for_demo(user_id=user_id, run_id=run_id)
            )
            background["longterm_extraction_job_ids"] = [
                str(job["job_id"])
                for job in extraction_jobs
                if job.get("source_operation_key") == idempotency_key
            ]
        except Exception:
            # Older/duck-typed test memories may not expose the new table yet.
            background.setdefault("longterm_extraction_job_ids", [])
        return result

    @staticmethod
    def session_scope_for_demo(*, user_id: str, run_id: str) -> str:
        """Expose the exact core scope builder used by ``Memory.add()``."""
        return _build_session_scope({"user_id": user_id, "run_id": run_id})

    @staticmethod
    def context_hash(context: Dict[str, Any]) -> str:
        payload = deepcopy(context)
        payload.pop("context_hash", None)
        payload.pop("llm_calls", None)
        payload.pop("tool_calls", None)
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

    @property
    def demo_background_worker(self) -> DemoBackgroundWorkerManager:
        """Return the manually controlled worker through a demo-only public API."""
        worker = self._ensure_background_workers()
        if not isinstance(worker, DemoBackgroundWorkerManager):
            raise RuntimeError("DemoMemory requires DemoBackgroundWorkerManager")
        return worker
