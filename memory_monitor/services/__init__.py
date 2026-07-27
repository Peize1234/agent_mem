__all__ = [
    "MemoryDebugService",
    "MonitorRepository",
    "SimulationService",
    "VectorStoreMonitorRepository",
]


def __getattr__(name):
    """Load services lazily so SQLite-only monitoring has no Mem0 runtime dependency."""

    if name == "MemoryDebugService":
        from memory_monitor.services.debug_service import MemoryDebugService

        return MemoryDebugService
    if name == "MonitorRepository":
        from memory_monitor.services.monitor_repository import MonitorRepository

        return MonitorRepository
    if name == "SimulationService":
        from memory_monitor.services.simulation_service import SimulationService

        return SimulationService
    if name == "VectorStoreMonitorRepository":
        from memory_monitor.services.vector_repository import VectorStoreMonitorRepository

        return VectorStoreMonitorRepository
    raise AttributeError(name)
