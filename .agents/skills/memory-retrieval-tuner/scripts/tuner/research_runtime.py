from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .benchmark_support import redact_secrets
from .io_utils import stable_hash
from .production_runtime import create_tuner_policy_llm


def _secret_values(value: Any, *, sensitive: bool = False) -> set[str]:
    values: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            lowered = str(key).lower()
            child_sensitive = sensitive or any(
                marker in lowered for marker in ("api_key", "password", "secret", "token", "credential")
            )
            values.update(_secret_values(item, sensitive=child_sensitive))
    elif isinstance(value, (list, tuple)):
        for item in value:
            values.update(_secret_values(item, sensitive=sensitive))
    elif sensitive and isinstance(value, str) and value:
        values.add(value)
    return values


@dataclass
class ResearchLLMRuntime:
    memory_config: dict[str, Any]
    llm_mode: str
    _llm: Any | None = field(default=None, init=False, repr=False)

    @property
    def model_config(self) -> dict[str, Any]:
        return {
            "source": "production_memory_config",
            "llm_mode": self.llm_mode,
            "llm": redact_secrets(self.memory_config.get("llm") or {}),
        }

    @property
    def model_config_hash(self) -> str:
        return stable_hash(self.model_config)

    @property
    def secrets(self) -> set[str]:
        return _secret_values(self.memory_config)

    def generate_response(self, messages: list[dict[str, str]], *, response_format: dict[str, Any]) -> Any:
        if self._llm is None:
            try:
                self._llm = create_tuner_policy_llm(self.memory_config, llm_mode=self.llm_mode)
            except Exception as exc:
                raise RuntimeError(f"Research LLM initialization failed: {exc}") from exc
        return self._llm.generate_response(messages=messages, response_format=response_format)
