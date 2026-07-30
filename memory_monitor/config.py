from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DemoLabConfig:
    simulation_root: Path = Path(".memory_monitor_runs")
    memory_config_path: Path | None = None
    foreground_workers: int = 4
    branch_workers: int = 1
    step_lease_seconds: int = 900
    poll_interval_seconds: float = 0.4
    completion_animation_seconds: float = 0.8

    @classmethod
    def from_env(cls) -> "DemoLabConfig":
        config_path = os.getenv("MEMORY_MONITOR_MEMORY_CONFIG")
        return cls(
            simulation_root=Path(os.getenv("MEMORY_MONITOR_SIMULATION_ROOT", ".memory_monitor_runs")).expanduser(),
            memory_config_path=Path(config_path).expanduser() if config_path else None,
            foreground_workers=max(int(os.getenv("MEMORY_MONITOR_FOREGROUND_WORKERS", "4")), 1),
            branch_workers=max(int(os.getenv("MEMORY_MONITOR_BRANCH_WORKERS", "1")), 1),
            step_lease_seconds=max(int(os.getenv("MEMORY_MONITOR_STEP_LEASE_SECONDS", "900")), 1),
            poll_interval_seconds=max(float(os.getenv("MEMORY_MONITOR_POLL_INTERVAL_SECONDS", "0.4")), 0.1),
            completion_animation_seconds=max(
                float(os.getenv("MEMORY_MONITOR_COMPLETION_ANIMATION_SECONDS", "0.8")),
                0,
            ),
        )
