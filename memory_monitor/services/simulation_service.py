from __future__ import annotations

import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from qdrant_client import QdrantClient

from mem0.configs.base import MemoryConfig
from mem0.configs.production import load_production_memory_config
from mem0.vector_stores.configs import VectorStoreConfig
from memory_monitor.runtime import DemoBackgroundCoordinator, DemoMemory
from memory_monitor.services.demo_pipeline_service import DemoPipelineService
from memory_monitor.services.demo_repository import DemoRepository
from memory_monitor.services.memory_state_service import MemoryStateService

_SIMULATION_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
logger = logging.getLogger(__name__)


@dataclass
class SimulationEnvironment:
    simulation_id: str
    root: Path
    memory: Any
    repository: DemoRepository
    state_service: MemoryStateService
    coordinator: DemoBackgroundCoordinator
    pipeline: DemoPipelineService


class SimulationService:
    """Own isolated history, vectors, demo state, and runtime lifecycles."""

    def __init__(
        self,
        root: str | Path = ".memory_monitor_runs",
        *,
        base_config: Optional[MemoryConfig] = None,
        memory_factory: Callable[[MemoryConfig], Any] = DemoMemory,
        foreground_workers: int = 4,
        branch_workers: int = 1,
        step_lease_seconds: int = 900,
    ):
        self.root = Path(root).expanduser().resolve()
        self.base_config = base_config or load_production_memory_config()
        self.memory_factory = memory_factory
        self.foreground_workers = max(int(foreground_workers), 1)
        self.branch_workers = max(int(branch_workers), 1)
        self.step_lease_seconds = max(int(step_lease_seconds), 1)
        self._environments: Dict[str, SimulationEnvironment] = {}

    def create_environment(self, simulation_id: Optional[str] = None) -> SimulationEnvironment:
        simulation_id = simulation_id or uuid.uuid4().hex
        self._validate_simulation_id(simulation_id)
        if simulation_id in self._environments:
            return self._environments[simulation_id]

        run_root = (self.root / simulation_id).resolve()
        self._assert_inside_root(run_root)
        created = not run_root.exists()
        run_root.mkdir(parents=True, exist_ok=True)
        if not run_root.is_dir():
            raise NotADirectoryError(f"Simulation path is not a directory: {run_root}")

        config = self._simulation_config(simulation_id, run_root)
        memory = None
        coordinator = None
        try:
            memory = self.memory_factory(config)
            self._warm_up_retrieval(memory, simulation_id)
            repository = DemoRepository(run_root / "demo.db")
            state_service = MemoryStateService(memory)
            coordinator = DemoBackgroundCoordinator(
                simulation_id,
                repository,
                foreground_workers=self.foreground_workers,
                branch_workers=self.branch_workers,
                lease_seconds=self.step_lease_seconds,
            )
            pipeline = DemoPipelineService(
                memory,
                repository,
                state_service,
                coordinator=coordinator,
            )
            pipeline.resume_pending_work()
        except Exception:
            if coordinator is not None:
                coordinator.shutdown(wait=True)
            if memory is not None:
                memory.close()
            else:
                self._close_config_vector_client(config)
            if created:
                shutil.rmtree(run_root, ignore_errors=True)
            raise

        environment = SimulationEnvironment(
            simulation_id=simulation_id,
            root=run_root,
            memory=memory,
            repository=repository,
            state_service=state_service,
            coordinator=coordinator,
            pipeline=pipeline,
        )
        self._environments[simulation_id] = environment
        return environment

    @staticmethod
    def _warm_up_retrieval(memory: Any, simulation_id: str) -> None:
        started_at = time.perf_counter()
        try:
            warm_up = getattr(memory, "warm_up_retrieval_for_demo", None)
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.warning(
                "Demo retrieval warmup simulation_id=%s executed=false skipped=false success=false "
                "duration_ms=%.2f error_type=%s error=%s",
                simulation_id,
                duration_ms,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return
        if not callable(warm_up):
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "Demo retrieval warmup simulation_id=%s executed=false skipped=true success=true duration_ms=%.2f",
                simulation_id,
                duration_ms,
            )
            return

        try:
            executed = bool(warm_up())
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            logger.warning(
                "Demo retrieval warmup simulation_id=%s executed=true skipped=false success=false "
                "duration_ms=%.2f error_type=%s error=%s",
                simulation_id,
                duration_ms,
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return

        duration_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "Demo retrieval warmup simulation_id=%s executed=%s skipped=%s success=true duration_ms=%.2f",
            simulation_id,
            str(executed).lower(),
            str(not executed).lower(),
            duration_ms,
        )

    def environment(self, simulation_id: str) -> SimulationEnvironment:
        self._validate_simulation_id(simulation_id)
        if simulation_id in self._environments:
            return self._environments[simulation_id]
        run_root = (self.root / simulation_id).resolve()
        self._assert_inside_root(run_root)
        if not run_root.is_dir():
            raise KeyError(f"Unknown simulation: {simulation_id}")
        return self.create_environment(simulation_id)

    def create_session(
        self,
        simulation_id: str,
        *,
        user_id: str,
        run_id: str,
    ) -> Dict[str, Any]:
        environment = self.environment(simulation_id)
        return environment.repository.create_session(simulation_id, user_id, run_id)

    def find_session(
        self,
        simulation_id: str,
        *,
        user_id: str,
        run_id: str,
    ) -> Dict[str, Any] | None:
        """Read an existing session without producing a SQLite write."""
        environment = self.environment(simulation_id)
        return environment.repository.find_session(simulation_id, user_id, run_id)

    def clear_environment(self, simulation_id: str) -> None:
        self._validate_simulation_id(simulation_id)
        environment = self._environments.get(simulation_id)
        run_root = (self.root / simulation_id).resolve()
        self._assert_inside_root(run_root)
        if environment is not None:
            environment.coordinator.shutdown(wait=True)
            closed = environment.memory.close()
            if closed is False:
                raise RuntimeError(f"Could not safely close simulation memory: {simulation_id}")
        if run_root.exists():
            shutil.rmtree(run_root)
        self._environments.pop(simulation_id, None)

    def close(self) -> None:
        for simulation_id, environment in list(self._environments.items()):
            environment.coordinator.shutdown(wait=True)
            closed = environment.memory.close()
            if closed is False:
                raise RuntimeError(f"Could not safely close simulation memory: {simulation_id}")
            self._environments.pop(simulation_id, None)

    def list_simulations(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            path.name for path in self.root.iterdir() if path.is_dir() and _SIMULATION_ID.fullmatch(path.name)
        )

    def _simulation_config(self, simulation_id: str, run_root: Path) -> MemoryConfig:
        config = self.base_config.model_copy(deep=True)
        config.history_db_path = str(run_root / "history.db")
        qdrant_path = run_root / "qdrant"
        vector_config = {
            "collection_name": f"memory_monitor_{simulation_id}",
            "path": str(qdrant_path),
            "embedding_model_dims": getattr(
                self.base_config.vector_store.config,
                "embedding_model_dims",
                1536,
            ),
            "bm25_language": getattr(
                self.base_config.vector_store.config,
                "bm25_language",
                "en",
            ),
        }
        # A prebuilt embedded client keeps the global vector timeout from
        # turning Demo-local Qdrant into an accidental localhost connection.
        if self.memory_factory is DemoMemory:
            vector_config["client"] = QdrantClient(path=str(qdrant_path))
        config.vector_store = VectorStoreConfig(
            provider="qdrant",
            config=vector_config,
        )
        config.background.enabled = True
        return config

    @staticmethod
    def _close_config_vector_client(config: MemoryConfig) -> None:
        client = getattr(config.vector_store.config, "client", None)
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def _assert_inside_root(self, path: Path) -> None:
        root = self.root.resolve()
        if path == root or not path.is_relative_to(root):
            raise ValueError("Simulation path escapes the configured simulation root")

    @staticmethod
    def _validate_simulation_id(simulation_id: str) -> None:
        if not _SIMULATION_ID.fullmatch(simulation_id):
            raise ValueError("simulation_id may contain only letters, numbers, '_' and '-'")
