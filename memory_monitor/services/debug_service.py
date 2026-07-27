from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from memory_monitor.services.monitor_repository import MonitorRepository
from memory_monitor.services.vector_repository import VectorStoreMonitorRepository


class MemoryDebugService:
    """High-level, policy-aware API consumed by the Streamlit pages."""

    def __init__(
        self,
        repository: MonitorRepository,
        vector_repository: Optional[VectorStoreMonitorRepository] = None,
        *,
        memory=None,
        allow_writes: bool = False,
    ):
        self.repository = repository
        self.vector_repository = vector_repository or VectorStoreMonitorRepository(memory)
        self.memory = memory
        self.allow_writes = allow_writes

    def dashboard(self) -> Dict[str, Any]:
        migration = self.repository.aggregate_job_metrics("memory_migration_jobs")
        profile = self.repository.aggregate_job_metrics("profile_update_jobs")
        return {
            "migration_statuses": self.repository.scalar_counts("memory_migration_jobs", "status"),
            "profile_statuses": self.repository.scalar_counts("profile_update_jobs", "status"),
            "migration": self._rates(migration),
            "profile": self._rates(profile),
            "worker": self.worker_status(),
            "average_duration_ms": self._average_durations(),
        }

    def worker_status(self) -> Dict[str, Any]:
        if not self.allow_writes:
            return {"available": False, "alive": False, "mode": "external_or_unknown"}
        worker = getattr(self.memory, "_background_worker", None) if self.memory is not None else None
        if worker is None:
            return {"available": False, "alive": False, "mode": "external_or_unknown"}
        return {
            "available": True,
            "alive": worker.threads_alive(),
            "mode": getattr(worker.config, "execution_mode", "auto"),
            "enabled": worker.enabled,
        }

    def list_traces(self, **kwargs) -> Dict[str, Any]:
        return self.repository.trace_summaries(**kwargs)

    def trace(self, trace_id: str) -> Dict[str, Any]:
        return {
            "events": self.repository.trace_events(trace_id)["items"],
            "messages": self.repository.page("messages", filters={"trace_id": trace_id})["items"],
            "migration_jobs": self.repository.page("memory_migration_jobs", filters={"trace_id": trace_id})["items"],
            "profile_jobs": self.repository.page("profile_update_jobs", filters={"trace_id": trace_id})["items"],
            "profile_values": self.repository.page("user_profile_values", filters={"trace_id": trace_id})["items"],
            "vectors": {
                name: self.vector_repository.page(name, filters={"trace_id": trace_id})["items"]
                for name in self.vector_repository.available_collections()
            },
        }

    def list_jobs(self, job_type: str, **kwargs) -> Dict[str, Any]:
        return self.repository.list_jobs(job_type, **kwargs)

    def job_detail(self, job_id: str, job_type: str) -> Dict[str, Any]:
        job = self.repository.get_job(job_id, job_type)
        if job is None:
            raise KeyError(f"Unknown {job_type} job: {job_id}")
        return {
            "job": job,
            "events": self.repository.page(
                "memory_observation_events",
                filters={"job_id": job_id},
                order_by="created_at",
                descending=False,
            )["items"],
            "messages": (
                self.repository.page("messages", filters={"migration_job_id": job_id})["items"]
                if job_type == "migration"
                else []
            ),
        }

    def browse_table(self, table: str, **kwargs) -> Dict[str, Any]:
        return self.repository.page(table, **kwargs)

    def list_tables(self) -> list[str]:
        return self.repository.list_tables()

    def table_schema(self, table: str) -> list[Dict[str, Any]]:
        return self.repository.table_schema(table)

    def list_vector_collections(self) -> list[str]:
        return self.vector_repository.available_collections()

    def browse_vectors(self, collection: str, **kwargs) -> Dict[str, Any]:
        if collection == "midterm_tree":
            return self.vector_repository.session_tree(**kwargs)
        return self.vector_repository.page(collection, **kwargs)

    def profile(self, user_id: str) -> Dict[str, Any]:
        values = self.repository.page("user_profile_values", filters={"user_id": user_id}, page_size=500)["items"]
        jobs = self.repository.page("profile_update_jobs", filters={"user_id": user_id}, page_size=200)["items"]
        events = self.repository.page(
            "memory_observation_events",
            filters={"user_id": user_id},
            page_size=500,
            order_by="created_at",
        )["items"]
        return {
            "values": values,
            "attributes": self.repository.page("profile_attributes", page_size=500)["items"],
            "jobs": jobs,
            "events": [event for event in events if event.get("event_type", "").startswith("profile.")],
        }

    def retry_job(self, job_id: str, job_type: str) -> bool:
        self._require_write_access()
        self._require_memory()
        return self.memory.retry_background_job(job_id, job_type)

    def process_next_job(self, job_type: str) -> bool:
        self._require_write_access()
        self._require_memory()
        if job_type == "migration":
            return self.memory.process_next_migration_job()
        if job_type == "profile":
            return self.memory.process_next_profile_job()
        raise ValueError("job_type must be 'migration' or 'profile'")

    def process_job(self, job_id: str, job_type: str) -> bool:
        self._require_write_access()
        self._require_memory()
        if job_type == "migration":
            return self.memory.process_migration_job(job_id)
        if job_type == "profile":
            return self.memory.process_profile_job(job_id)
        raise ValueError("job_type must be 'migration' or 'profile'")

    def _average_durations(self) -> Dict[str, float]:
        return {
            stage: self.repository.average_event_duration(f"{stage}.succeeded")
            for stage in ("midterm", "longterm", "profile")
        }

    @staticmethod
    def _rates(metrics: Dict[str, Any]) -> Dict[str, Any]:
        total = int(metrics.get("total") or 0)
        oldest = metrics.get("oldest_pending_at")
        wait_seconds = 0.0
        if oldest:
            parsed = datetime.fromisoformat(oldest)
            now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
            wait_seconds = max((now - parsed).total_seconds(), 0.0)
        return {
            **metrics,
            "success_rate": (metrics["succeeded"] / total) if total else 0.0,
            "retry_rate": (metrics["retried"] / total) if total else 0.0,
            "degraded_rate": (metrics["degraded"] / total) if total else 0.0,
            "oldest_pending_wait_seconds": wait_seconds,
        }

    def _require_write_access(self) -> None:
        if not self.allow_writes:
            raise PermissionError("Monitor write operations are disabled")

    def _require_memory(self) -> None:
        if self.memory is None:
            raise RuntimeError("No Memory instance is attached to the monitor")
