from __future__ import annotations

# ruff: noqa: E402

import atexit
import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mem0.configs.base import MemoryConfig
from memory_monitor.components import styles
from memory_monitor.config import DemoLabConfig
from memory_monitor.services.simulation_service import SimulationService
from memory_monitor.views import demo_lab


def _load_memory_config(path: Path | None) -> MemoryConfig:
    if path is None:
        return MemoryConfig()
    return MemoryConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise RuntimeError("Install the monitor extra and run: hatch run monitor:start") from exc

    st.set_page_config(page_title="Agent Memory Demo Lab", page_icon="🧠", layout="wide")
    styles.inject(st)
    config = DemoLabConfig.from_env()

    @st.cache_resource
    def simulation_service(
        root: str,
        config_path: str,
        foreground_workers: int,
        branch_workers: int,
        step_lease_seconds: int,
    ) -> SimulationService:
        path = Path(config_path) if config_path else None
        service = SimulationService(
            root,
            base_config=_load_memory_config(path),
            foreground_workers=foreground_workers,
            branch_workers=branch_workers,
            step_lease_seconds=step_lease_seconds,
        )
        atexit.register(service.close)
        return service

    service = simulation_service(
        str(config.simulation_root),
        str(config.memory_config_path or ""),
        config.foreground_workers,
        config.branch_workers,
        config.step_lease_seconds,
    )
    demo_lab.render(st, service, config)


if __name__ == "__main__":
    main()
