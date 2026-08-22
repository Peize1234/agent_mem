from __future__ import annotations

import atexit
import json
import logging
import os
import sys
from pathlib import Path

# ruff: noqa: E402

os.environ.setdefault("MEM0_TELEMETRY", "false")

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from mem0.configs.base import MemoryConfig
from mem0.configs.production import load_production_memory_config
from memory_monitor.components import styles
from memory_monitor.config import DemoLabConfig
from memory_monitor.services.simulation_service import SimulationService
from memory_monitor.views import demo_lab

logger = logging.getLogger(__name__)


def _load_memory_config(path: Path | None) -> MemoryConfig:
    if path is None:
        return load_production_memory_config()
    overrides = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(overrides, dict):
        raise ValueError("Memory config override must contain a JSON object")
    return load_production_memory_config(overrides)


def main() -> None:
    try:
        import streamlit as st
    except ImportError as exc:
        raise RuntimeError("Install the monitor extra and run: hatch run monitor:start") from exc

    st.set_page_config(page_title="Agent Memory Demo Lab", page_icon="🧠", layout="wide")
    try:
        styles.inject(st)
        config = DemoLabConfig.from_env()

        @st.cache_resource(show_spinner="正在初始化隔离环境…")
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
    except Exception as exc:
        logger.exception("Memory Monitor page initialization failed")
        st.error(f"页面加载失败（{type(exc).__name__}）：{_safe_error_message(exc)}")
        if st.button("重新加载", key="app:reload"):
            st.rerun()


def _safe_error_message(exc: Exception) -> str:
    return "初始化或页面渲染未完成，请查看服务端日志。"


if __name__ == "__main__":
    main()
