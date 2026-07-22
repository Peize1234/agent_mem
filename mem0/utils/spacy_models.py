"""
Shared spaCy model loader.

Consolidates spaCy model loading into a single module so that
entity_extraction and lemmatization share one instance instead of
each loading their own copy from disk.
"""

import logging
import threading

logger = logging.getLogger(__name__)

_nlp_full = None
_nlp_lemma = None
_nlp_chinese = None
_load_failed_full = False
_load_failed_lemma = False
_load_failed_chinese = False
_lock = threading.Lock()

_CHINESE_MODEL_NAMES = (
    "zh_core_web_trf",
    "zh_core_web_lg",
    "zh_core_web_md",
    "zh_core_web_sm",
)


def _ensure_model_available():
    """Download en_core_web_sm if spaCy is installed but model is missing."""
    try:
        import spacy
    except ImportError:
        raise ImportError("spaCy is not installed. Install it with: pip install mem0ai[nlp]")

    if not spacy.util.is_package("en_core_web_sm"):
        logger.info("Downloading spaCy model en_core_web_sm...")
        try:
            from spacy.cli import download

            download("en_core_web_sm")
            logger.info("spaCy model en_core_web_sm downloaded successfully")
        except Exception as e:
            raise RuntimeError(
                f"Failed to download spaCy model en_core_web_sm: {e}. "
                "Please install manually: python -m spacy download en_core_web_sm"
            ) from e


def get_nlp_full():
    """Return spaCy model with all pipelines (NER, tagger, etc.) for entity extraction."""
    global _nlp_full, _load_failed_full
    if _load_failed_full:
        return None
    if _nlp_full is not None:
        return _nlp_full
    with _lock:
        if _nlp_full is not None:
            return _nlp_full
        if _load_failed_full:
            return None
        try:
            _ensure_model_available()
            import spacy

            _nlp_full = spacy.load("en_core_web_sm")
            logger.info("spaCy full model loaded")
        except Exception as e:
            logger.warning(f"Failed to load spaCy full model: {e}")
            _load_failed_full = True
            return None
    return _nlp_full


def get_nlp_lemma():
    """Return spaCy model with only lemmatizer for BM25 text processing."""
    global _nlp_lemma, _load_failed_lemma
    if _load_failed_lemma:
        return None
    if _nlp_lemma is not None:
        return _nlp_lemma
    with _lock:
        if _nlp_lemma is not None:
            return _nlp_lemma
        if _load_failed_lemma:
            return None
        try:
            _ensure_model_available()
            import spacy

            _nlp_lemma = spacy.load("en_core_web_sm", disable=["ner", "parser"])
            logger.info("spaCy lemma model loaded")
        except Exception as e:
            logger.warning(f"Failed to load spaCy lemma model: {e}")
            _load_failed_lemma = True
            return None
    return _nlp_lemma


def get_nlp_chinese():
    """Return the best installed Chinese spaCy NER model, without downloading one."""
    global _nlp_chinese, _load_failed_chinese
    if _load_failed_chinese:
        return None
    if _nlp_chinese is not None:
        return _nlp_chinese

    with _lock:
        if _nlp_chinese is not None:
            return _nlp_chinese
        if _load_failed_chinese:
            return None
        try:
            import spacy

            model_name = next((name for name in _CHINESE_MODEL_NAMES if spacy.util.is_package(name)), None)
            if model_name is None:
                raise RuntimeError("no installed Chinese spaCy model (tried " + ", ".join(_CHINESE_MODEL_NAMES) + ")")
            _nlp_chinese = spacy.load(model_name)
            logger.info("Chinese spaCy model loaded: %s", model_name)
        except Exception as e:
            logger.warning(
                "Chinese spaCy NER unavailable; using financial dictionary and strict patterns only: %s",
                e,
            )
            _load_failed_chinese = True
            return None
    return _nlp_chinese
