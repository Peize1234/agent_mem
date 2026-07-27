from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from mem0 import Memory
from mem0.configs.base import MemoryConfig
from mem0.vector_stores.configs import VectorStoreConfig
from memory_monitor.services.debug_service import MemoryDebugService
from memory_monitor.services.monitor_repository import MonitorRepository
from memory_monitor.services.vector_repository import VectorStoreMonitorRepository

_SIMULATION_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


@dataclass
class SimulationEnvironment:
    simulation_id: str
    root: Path
    memory: Any
    debug_service: MemoryDebugService
    midterm_handler: Callable
    longterm_handler: Callable


class SimulationService:
    """Creates isolated Mem0 instances and drives their public manual-worker API."""

    def __init__(
        self,
        root: str | Path = ".memory_monitor_runs",
        *,
        base_config: Optional[MemoryConfig] = None,
        memory_factory: Callable[[MemoryConfig], Any] = Memory,
    ):
        self.root = Path(root).expanduser().resolve()
        self.base_config = base_config or MemoryConfig()
        self.memory_factory = memory_factory
        self._environments: Dict[str, SimulationEnvironment] = {}

    def create_environment(self, simulation_id: Optional[str] = None) -> SimulationEnvironment:
        simulation_id = simulation_id or uuid.uuid4().hex
        self._validate_simulation_id(simulation_id)
        if simulation_id in self._environments:
            return self._environments[simulation_id]

        run_root = (self.root / simulation_id).resolve()
        self._assert_inside_root(run_root)
        run_root.mkdir(parents=True, exist_ok=False)
        qdrant_path = run_root / "qdrant"
        config = self.base_config.model_copy(deep=True)
        config.history_db_path = str(run_root / "history.db")
        config.vector_store = VectorStoreConfig(
            provider="qdrant",
            config={
                "collection_name": f"memory_monitor_{simulation_id}",
                "path": str(qdrant_path),
                "embedding_model_dims": getattr(self.base_config.vector_store.config, "embedding_model_dims", 1536),
            },
        )
        config.background.enabled = True
        config.background.execution_mode = "manual"
        config.observability.enabled = True

        try:
            memory = self.memory_factory(config)
        except Exception:
            shutil.rmtree(run_root, ignore_errors=True)
            raise
        repository = MonitorRepository(config.history_db_path)
        vectors = VectorStoreMonitorRepository(memory)
        debug_service = MemoryDebugService(repository, vectors, memory=memory, allow_writes=True)
        midterm_handler = memory._background_worker.process_midterm
        longterm_handler = memory._background_worker.process_longterm

        def process_midterm(job, messages, degraded):
            if not (job.get("metadata") or {}).get("memory_monitor_midterm_enabled", True):
                return None
            previous = memory.config.midterm.enabled
            memory.config.midterm.enabled = True
            try:
                return midterm_handler(job, messages, degraded)
            finally:
                memory.config.midterm.enabled = previous

        def process_longterm(job, messages, degraded):
            if (job.get("metadata") or {}).get("memory_monitor_longterm_enabled", True):
                return longterm_handler(job, messages, degraded)
            return None

        memory._background_worker.process_midterm = process_midterm
        memory._background_worker.process_longterm = process_longterm
        environment = SimulationEnvironment(
            simulation_id=simulation_id,
            root=run_root,
            memory=memory,
            debug_service=debug_service,
            midterm_handler=midterm_handler,
            longterm_handler=longterm_handler,
        )
        self._environments[simulation_id] = environment
        return environment

    def submit_turn(
        self,
        simulation_id: str,
        *,
        user_id: str,
        run_id: str,
        user_message: str,
        assistant_message: str,
        short_term_capacity: int = 10,
        midterm_enabled: bool = True,
        longterm_enabled: bool = True,
        profile_enabled: bool = True,
    ) -> Dict[str, Any]:
        environment = self.environment(simulation_id)
        memory = environment.memory
        memory.config.midterm.short_term_capacity = max(int(short_term_capacity), 0)
        memory.config.midterm.enabled = bool(midterm_enabled)
        memory.config.profile.enabled = bool(profile_enabled)
        before = self.snapshot(simulation_id, user_id=user_id, run_id=run_id)
        result = memory.add(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            ],
            user_id=user_id,
            run_id=run_id,
            metadata={
                "memory_monitor_midterm_enabled": bool(midterm_enabled),
                "memory_monitor_longterm_enabled": bool(longterm_enabled),
            },
        )
        after = self.snapshot(simulation_id, user_id=user_id, run_id=run_id)
        trace = environment.debug_service.trace(result["observability"]["trace_id"])
        return {
            "result": result,
            "before": before,
            "after": after,
            "changes": self.compare_snapshots(before, after),
            "trace": trace,
        }

    def submit_batch(
        self,
        simulation_id: str,
        turns: Iterable[Dict[str, str]],
        **options: Any,
    ) -> list[Dict[str, Any]]:
        return [
            self.submit_turn(
                simulation_id,
                user_id=turn["user_id"],
                run_id=turn["run_id"],
                user_message=turn["user_message"],
                assistant_message=turn["assistant_message"],
                **options,
            )
            for turn in turns
        ]

    def process_next_migration_job(self, simulation_id: str) -> bool:
        return self.environment(simulation_id).memory.process_next_migration_job()

    def process_next_profile_job(self, simulation_id: str) -> bool:
        return self.environment(simulation_id).memory.process_next_profile_job()

    def process_job(self, simulation_id: str, job_id: str, job_type: str) -> bool:
        memory = self.environment(simulation_id).memory
        if job_type == "migration":
            return memory.process_migration_job(job_id)
        if job_type == "profile":
            return memory.process_profile_job(job_id)
        raise ValueError("job_type must be 'migration' or 'profile'")

    def process_all_pending(self, simulation_id: str, *, max_jobs: int = 1000) -> Dict[str, int]:
        memory = self.environment(simulation_id).memory
        counts = {"migration": 0, "profile": 0}
        for _ in range(max(int(max_jobs), 0)):
            migration = memory.process_next_migration_job()
            profile = memory.process_next_profile_job()
            counts["migration"] += int(migration)
            counts["profile"] += int(profile)
            if not migration and not profile:
                break
        return counts

    def snapshot(self, simulation_id: str, *, user_id: str, run_id: str) -> Dict[str, Any]:
        environment = self.environment(simulation_id)
        service = environment.debug_service
        filters = {"user_id": user_id, "run_id": run_id}
        vectors = {}
        for collection in service.vector_repository.available_collections():
            vectors[collection] = service.vector_repository.page(
                collection,
                filters=filters,
                page_size=200,
            )["items"]
        return {
            "messages": service.repository.page(
                "messages",
                filters={"session_scope": f"run_id={run_id}&user_id={user_id}"},
                page_size=200,
            )["items"],
            "migration_jobs": service.repository.page(
                "memory_migration_jobs",
                filters={"session_scope": f"run_id={run_id}&user_id={user_id}"},
                page_size=200,
            )["items"],
            "profile_jobs": service.repository.page(
                "profile_update_jobs",
                filters={"user_id": user_id, "run_id": run_id},
                page_size=200,
            )["items"],
            "profile_values": service.repository.page(
                "user_profile_values",
                filters={"user_id": user_id},
                page_size=200,
            )["items"],
            "events": service.repository.page(
                "memory_observation_events",
                filters={"user_id": user_id, "run_id": run_id},
                page_size=500,
                order_by="created_at",
                descending=False,
            )["items"],
            "vectors": vectors,
        }

    @staticmethod
    def compare_snapshots(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        """Return the records added or changed between two bounded snapshots."""

        def changed(rows_before, rows_after, key):
            previous = {row.get(key): row for row in rows_before}
            return [row for row in rows_after if row.get(key) not in previous or previous[row.get(key)] != row]

        before_messages = {row.get("id"): row for row in before.get("messages", [])}
        added_messages = [
            row for row in after.get("messages", []) if row.get("id") not in before_messages
        ]
        evicted_messages = [
            row
            for row in after.get("messages", [])
            if row.get("status") in {"pending", "processing", "failed"}
            and (
                row.get("id") not in before_messages
                or before_messages[row.get("id")].get("status") == "active"
            )
        ]
        after_message_ids = {row.get("id") for row in after.get("messages", [])}
        removed_messages = [
            row for row in before.get("messages", []) if row.get("id") not in after_message_ids
        ]
        vector_changes = {}
        collections = set(before.get("vectors", {})) | set(after.get("vectors", {}))
        for collection in collections:
            vector_changes[collection] = changed(
                before.get("vectors", {}).get(collection, []),
                after.get("vectors", {}).get(collection, []),
                "id",
            )
        return {
            "added_messages": added_messages,
            "evicted_messages": evicted_messages,
            "removed_messages": removed_messages,
            "created_migration_jobs": [
                row
                for row in after.get("migration_jobs", [])
                if row.get("job_id") not in {item.get("job_id") for item in before.get("migration_jobs", [])}
            ],
            "migration_job_changes": changed(
                before.get("migration_jobs", []),
                after.get("migration_jobs", []),
                "job_id",
            ),
            "created_profile_jobs": [
                row
                for row in after.get("profile_jobs", [])
                if row.get("job_id") not in {item.get("job_id") for item in before.get("profile_jobs", [])}
            ],
            "profile_job_changes": changed(
                before.get("profile_jobs", []),
                after.get("profile_jobs", []),
                "job_id",
            ),
            "profile_changes": changed(
                before.get("profile_values", []),
                after.get("profile_values", []),
                "attribute_id",
            ),
            "new_events": changed(
                before.get("events", []),
                after.get("events", []),
                "event_id",
            ),
            "vector_changes": vector_changes,
        }

    def environment(self, simulation_id: str) -> SimulationEnvironment:
        try:
            return self._environments[simulation_id]
        except KeyError as exc:
            raise KeyError(f"Unknown simulation: {simulation_id}") from exc

    def clear_environment(self, simulation_id: str) -> None:
        self._validate_simulation_id(simulation_id)
        environment = self._environments.pop(simulation_id, None)
        run_root = (self.root / simulation_id).resolve()
        self._assert_inside_root(run_root)
        if environment is not None:
            environment.memory.close()
        if run_root.exists():
            shutil.rmtree(run_root)

    def _assert_inside_root(self, path: Path) -> None:
        root = self.root.resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("Simulation path escapes the configured simulation root")

    @staticmethod
    def _validate_simulation_id(simulation_id: str) -> None:
        if not _SIMULATION_ID.fullmatch(simulation_id):
            raise ValueError("simulation_id may contain only letters, numbers, '_' and '-'")
