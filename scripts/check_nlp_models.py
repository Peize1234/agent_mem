#!/usr/bin/env python3
"""Report local NLP dependencies and models without installing anything."""

import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))


def _module_status(module_name: str) -> str:
    return "installed" if importlib.util.find_spec(module_name) is not None else "missing"


def main() -> None:
    spacy_status = _module_status("spacy")
    print(f"spaCy: {spacy_status}")
    print(f"jieba: {_module_status('jieba')}")

    if spacy_status == "missing":
        print("en_core_web_sm: missing")
        print("zh_core_web_sm: missing")
        print("Chinese NLP model: unavailable")
        return

    import spacy

    for model_name in ("en_core_web_sm", "zh_core_web_sm"):
        status = "installed" if spacy.util.is_package(model_name) else "missing"
        print(f"{model_name}: {status}")

    from mem0.utils.spacy_models import get_nlp_chinese

    nlp = get_nlp_chinese()
    if nlp is None:
        print("Chinese NLP model: unavailable")
        return

    language = nlp.meta.get("lang", "zh")
    name = nlp.meta.get("name", type(nlp).__name__)
    loaded_name = name if name.startswith(f"{language}_") else f"{language}_{name}"
    print(f"Chinese NLP model: {loaded_name}")


if __name__ == "__main__":
    main()
