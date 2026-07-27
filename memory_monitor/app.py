from __future__ import annotations

# ruff: noqa: E402

import json
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mem0.configs.base import MemoryConfig
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
    config = DemoLabConfig.from_env()

    @st.cache_resource
    def simulation_service(root: str, config_path: str) -> SimulationService:
        path = Path(config_path) if config_path else None
        return SimulationService(root, base_config=_load_memory_config(path))

    service = simulation_service(
        str(config.simulation_root),
        str(config.memory_config_path or ""),
    )
    demo_lab.render(st, service)


if __name__ == "__main__":
    main()
