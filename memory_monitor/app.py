from __future__ import annotations

# ruff: noqa: E402

import json
import sys
from importlib.metadata import PackageNotFoundError
from pathlib import Path

# Streamlit executes the entry script with its directory at sys.path[0]. Add the
# repository root so a source checkout works without relying on the caller's CWD.
_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from memory_monitor.config import MemoryMonitorConfig
from memory_monitor.services.debug_service import MemoryDebugService
from memory_monitor.services.monitor_repository import MonitorRepository
from memory_monitor.services.vector_repository import VectorStoreMonitorRepository
from memory_monitor.views import dashboard, data_browser, jobs, profiles, simulator, traces


_CORE_DEPENDENCY_ERRORS = (ModuleNotFoundError, PackageNotFoundError)


def _show_core_dependency_error(st) -> None:
    st.error(
        "This mode requires the full Mem0 runtime. Start it with "
        "`hatch run monitor:start`, or install this checkout with its `monitor` optional dependency."
    )


def _load_attached_memory(config: MemoryMonitorConfig):
    if config.memory_config_path is None:
        return None
    from mem0 import Memory
    from mem0.configs.base import MemoryConfig

    raw = json.loads(config.memory_config_path.read_text(encoding="utf-8"))
    memory_config = MemoryConfig.model_validate(raw)
    memory_config.background.enabled = True
    memory_config.background.execution_mode = "manual"
    memory_config.observability.enabled = config.allow_writes
    if config.allow_writes:
        memory_config.history_db_path = str(config.db_path)
    else:
        # Read-only monitoring must not migrate or write the production SQLite file.
        memory_config.history_db_path = ":memory:"
    return Memory(memory_config)


def _load_memory_config(config_path: str):
    if not config_path:
        return None
    from mem0.configs.base import MemoryConfig

    raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return MemoryConfig.model_validate(raw)


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise RuntimeError("Start the monitor with: hatch run monitor:start") from exc

    st.set_page_config(page_title="Mem0 Memory Monitor", layout="wide")
    config = MemoryMonitorConfig.from_env()
    st.sidebar.title("Mem0 monitor")
    st.sidebar.write(f"Mode: `{config.mode}`")
    st.sidebar.write(f"SQLite: `{config.db_path}`")
    st.sidebar.write(f"Writes enabled: `{config.allow_writes}`")

    @st.cache_resource
    def simulation_service(root: str, config_path: str):
        from memory_monitor.services.simulation_service import SimulationService

        return SimulationService(root, base_config=_load_memory_config(config_path))

    if config.mode == "sandbox":
        try:
            service = simulation_service(
                str(config.simulation_root),
                str(config.memory_config_path or ""),
            )
        except _CORE_DEPENDENCY_ERRORS:
            _show_core_dependency_error(st)
            return
        simulator.render(st, service)
        return

    if not config.db_path.exists():
        st.error(f"SQLite database does not exist: {config.db_path}")
        return

    @st.cache_resource
    def attached_memory(config_path: str, allow_writes: bool, db_path: str):
        runtime_config = MemoryMonitorConfig(
            db_path=config.db_path,
            mode=config.mode,
            allow_writes=allow_writes,
            max_display_length=config.max_display_length,
            simulation_root=config.simulation_root,
            memory_config_path=config.memory_config_path,
        )
        return _load_attached_memory(runtime_config)

    try:
        memory = attached_memory(
            str(config.memory_config_path or ""),
            config.allow_writes,
            str(config.db_path),
        )
    except _CORE_DEPENDENCY_ERRORS:
        _show_core_dependency_error(st)
        return
    repository = MonitorRepository(config.db_path, max_field_length=config.max_display_length)
    vector_repository = VectorStoreMonitorRepository(memory, max_payload_length=config.max_display_length)
    service = MemoryDebugService(
        repository,
        vector_repository,
        memory=memory,
        allow_writes=config.allow_writes,
    )

    tabs = st.tabs(["Dashboard", "Traces", "Jobs", "Data", "Profiles"])
    with tabs[0]:
        dashboard.render(st, service)
    with tabs[1]:
        traces.render(st, service)
    with tabs[2]:
        jobs.render(st, service)
    with tabs[3]:
        data_browser.render(st, service)
    with tabs[4]:
        profiles.render(st, service)


if __name__ == "__main__":
    main()
