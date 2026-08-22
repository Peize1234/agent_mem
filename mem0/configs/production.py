from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Mapping

from mem0.configs.base import MemoryConfig

DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"
DEEPSEEK_API_KEY_PLACEHOLDER = "${DEEPSEEK_API_KEY}"

_PRODUCTION_MEMORY_OVERRIDES: dict[str, Any] = {
    "llm": {
        "provider": "deepseek",
        "config": {
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "temperature": 0.1,
            "top_p": 0.1,
            "top_k": 1,
        },
    },
    "embedder": {
        "provider": "huggingface",
        "config": {
            "model": "BAAI/bge-small-zh-v1.5",
            "embedding_dims": 512,
        },
    },
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "embedding_model_dims": 512,
            "bm25_language": "zh",
            "on_disk": True,
        },
    },
}


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def load_production_memory_config(
    overrides: Mapping[str, Any] | None = None,
    *,
    resolve_environment: bool = True,
) -> MemoryConfig:
    """Return the repository Production Memory configuration.

    ``MemoryConfig`` remains the schema/default authority. This loader owns
    only repository deployment choices such as providers and model IDs.
    Callers that persist configuration provenance can retain the environment
    placeholder; live runtimes resolve the API key from the process environment.
    """

    production = deepcopy(_PRODUCTION_MEMORY_OVERRIDES)
    production["llm"]["config"]["api_key"] = (
        os.getenv(DEEPSEEK_API_KEY_ENV) if resolve_environment else DEEPSEEK_API_KEY_PLACEHOLDER
    )
    if overrides:
        production = _deep_merge(production, overrides)
    return MemoryConfig(**production)
