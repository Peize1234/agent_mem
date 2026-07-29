"""Service layer for the isolated demo lab."""

from __future__ import annotations

from importlib import import_module

_EXPORTS = {
    "DemoPipelineService": ("memory_monitor.services.demo_pipeline_service", "DemoPipelineService"),
    "DemoRepository": ("memory_monitor.services.demo_repository", "DemoRepository"),
    "MemoryStateService": ("memory_monitor.services.memory_state_service", "MemoryStateService"),
    "SimulationService": ("memory_monitor.services.simulation_service", "SimulationService"),
    "TurnSessionMismatchError": ("memory_monitor.services.demo_repository", "TurnSessionMismatchError"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)
