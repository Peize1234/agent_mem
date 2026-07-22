#!/usr/bin/env python3
"""Diagnose Chinese BM25/entity code loading and optionally reset entities."""

import argparse
import inspect
import json
import sys
from copy import deepcopy
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

import mem0  # noqa: E402
from mem0.utils import entity_extraction, lemmatization  # noqa: E402
from mem0.utils.entity_extraction import extract_entities  # noqa: E402
from mem0.utils.lemmatization import lemmatize_for_bm25  # noqa: E402


def _reset_entity_store(config_path: Path) -> None:
    """Reset only the separately named Entity Store collection."""
    from mem0.configs.base import MemoryConfig
    from mem0.memory.main import _entity_collection_name
    from mem0.utils.factory import VectorStoreFactory

    config = MemoryConfig(**json.loads(config_path.read_text(encoding="utf-8")))
    entity_config = deepcopy(config.vector_store.config)
    entity_config.collection_name = _entity_collection_name(
        config.vector_store.provider,
        config.vector_store.config.collection_name,
    )
    entity_store = VectorStoreFactory.create(config.vector_store.provider, entity_config)
    reset = getattr(entity_store, "reset", None)
    if not callable(reset):
        raise RuntimeError(
            "The configured vector store cannot reset the Entity Store independently; nothing was deleted."
        )
    reset()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="用户长期关注华夏沪深300ETF。")
    parser.add_argument(
        "--reset-entity-store",
        action="store_true",
        help="Reset only the Entity Store collection; requires --config.",
    )
    parser.add_argument("--config", type=Path, help="MemoryConfig JSON used only to identify the Entity Store.")
    args = parser.parse_args()

    print(f"mem0.__file__: {mem0.__file__}")
    print(f"entity_extraction: {inspect.getsourcefile(entity_extraction)}")
    print(f"lemmatization: {inspect.getsourcefile(lemmatization)}")
    print(f"lemmatize_for_bm25: {lemmatize_for_bm25(args.text)}")
    print(f"extract_entities: {extract_entities(args.text)}")

    if args.reset_entity_store:
        if args.config is None:
            parser.error("--reset-entity-store requires --config so the exact Entity Store can be identified")
        _reset_entity_store(args.config)
        print("Entity Store reset complete; long-term memory, SQLite, profiles, and mid-term memory were untouched.")


if __name__ == "__main__":
    main()
