from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DemoLabConfig:
    simulation_root: Path = Path(".memory_monitor_runs")
    memory_config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "DemoLabConfig":
        config_path = os.getenv("MEMORY_MONITOR_MEMORY_CONFIG")
        return cls(
            simulation_root=Path(os.getenv("MEMORY_MONITOR_SIMULATION_ROOT", ".memory_monitor_runs")).expanduser(),
            memory_config_path=Path(config_path).expanduser() if config_path else None,
        )
