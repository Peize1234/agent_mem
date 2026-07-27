from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class MemoryMonitorConfig:
    """Runtime settings for the debug GUI."""

    db_path: Path
    mode: Literal["read_only", "sandbox"] = "read_only"
    allow_writes: bool = False
    max_display_length: int = 4000
    simulation_root: Path = Path(".memory_monitor_runs")
    memory_config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "MemoryMonitorConfig":
        mem0_root = Path(os.getenv("MEM0_DIR", Path.home() / ".mem0"))
        default_db = mem0_root / "history.db"
        mode = os.getenv("MEMORY_MONITOR_MODE", "read_only")
        if mode not in {"read_only", "sandbox"}:
            raise ValueError("MEMORY_MONITOR_MODE must be 'read_only' or 'sandbox'")
        config_path = os.getenv("MEMORY_MONITOR_MEMORY_CONFIG")
        return cls(
            db_path=Path(os.getenv("MEMORY_MONITOR_DB_PATH", str(default_db))).expanduser(),
            mode=mode,
            allow_writes=os.getenv("MEMORY_MONITOR_ALLOW_WRITES", "").lower() in {"1", "true", "yes"},
            max_display_length=max(int(os.getenv("MEMORY_MONITOR_MAX_DISPLAY_LENGTH", "4000")), 100),
            simulation_root=Path(os.getenv("MEMORY_MONITOR_SIMULATION_ROOT", ".memory_monitor_runs")).expanduser(),
            memory_config_path=Path(config_path).expanduser() if config_path else None,
        )
