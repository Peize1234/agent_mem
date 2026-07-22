"""
BM25 text normalization for consistent keyword matching.

Uses Jieba for Chinese text and spaCy's lemmatizer for English text.
The English path keeps the existing behavior for:
- Verb forms: attending/attends/attended -> attend
- Comparatives/superlatives: older/oldest -> old
- Plurals: memories -> memory
- Avoids over-stemming: organization != organize

Also includes original -ing forms alongside lemmas to handle cases
where spaCy's context-dependent lemmatization produces inconsistent
results (e.g., "meeting" as noun vs verb -> different lemmas).
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import Path
from types import ModuleType

from mem0.utils.language import contains_chinese

logger = logging.getLogger(__name__)

_THOUSANDS_SEPARATOR_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_BASIC_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?|[\u3400-\u4dbf\u4e00-\u9fff]+")

_FINANCE_DICT_PATH = Path(__file__).resolve().parents[1] / "configs" / "finance_bm25_dict.txt"
_jieba_initialized = False
_jieba_lock = threading.Lock()


def _normalize_text(text: str) -> str:
    """Normalize text before BM25 tokenization."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    normalized = _THOUSANDS_SEPARATOR_RE.sub("", normalized)
    return _PERCENT_RE.sub(r"\1", normalized)


def _is_search_token(token: str) -> bool:
    stripped = token.strip()
    return bool(stripped) and any(char.isalnum() for char in stripped)


def _join_tokens(tokens: list[str]) -> str:
    return " ".join(token for token in (token.strip() for token in tokens) if _is_search_token(token))


def _basic_tokenize(text: str) -> str:
    return " ".join(_BASIC_TOKEN_RE.findall(text))


def _load_finance_dict(jieba: ModuleType) -> None:
    """Load the custom finance dictionary once if it is present."""
    global _jieba_initialized

    if _jieba_initialized:
        return

    with _jieba_lock:
        if _jieba_initialized:
            return
        if _FINANCE_DICT_PATH.is_file():
            try:
                jieba.load_userdict(str(_FINANCE_DICT_PATH))
            except Exception as e:
                logger.warning(f"Failed to load finance BM25 dictionary: {e}")
        _jieba_initialized = True


def _segment_chinese_for_bm25(text: str) -> str:
    try:
        import jieba
    except ImportError:
        logger.warning(
            'jieba not installed - Chinese BM25 tokenization disabled. Install it with: pip install "mem0ai[extras]"'
        )
        return text

    try:
        _load_finance_dict(jieba)
        return _join_tokens(jieba.lcut(text, cut_all=False))
    except Exception as e:
        logger.warning(f"Chinese BM25 tokenization failed; using original text: {e}")
        return text


def lemmatize_for_bm25(text: str) -> str:
    """Normalize and tokenize text for BM25 matching.

    Chinese text is segmented with Jieba and English text keeps the existing
    spaCy lemmatization flow. Returns a space-joined string for full-text search.
    """
    normalized_text = _normalize_text(text)
    if not normalized_text:
        return ""

    if contains_chinese(normalized_text):
        return _segment_chinese_for_bm25(normalized_text)

    from mem0.utils.spacy_models import get_nlp_lemma

    nlp = get_nlp_lemma()
    if nlp is None:
        return _basic_tokenize(normalized_text)

    doc = nlp(normalized_text)
    tokens = []

    for token in doc:
        if token.is_punct or token.is_stop:
            continue

        lemma = token.lemma_
        if _is_search_token(lemma):
            tokens.append(lemma)

        # Also add original if it ends in -ing and differs from lemma.
        # This handles noun/verb ambiguity (meeting/meet, attending/attend).
        if token.text.endswith("ing") and token.text != lemma and _is_search_token(token.text):
            tokens.append(token.text)

    return _join_tokens(tokens)
