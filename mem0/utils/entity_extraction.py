"""
Language-aware entity extraction from text.

Extracts three types of entities from a spaCy-processed document:
- **Proper nouns**: Capitalized multi-word sequences (person names, places, brands)
- **Quoted text**: Text in single or double quotes (titles, specific terms)
- **Noun compounds**: Multi-word noun phrases with specific modifiers (e.g., "machine learning")

English text keeps the existing spaCy rules. Chinese text uses an installed
Chinese spaCy NER model when available, plus a finance dictionary and strict
financial-name/security-code patterns. It never passes Chinese text through
the English model.

Public API:
    ``extract_entities(text)`` accepts a string and owns spaCy model loading.
    ``extract_entities_batch(texts)`` uses ``nlp.pipe`` for batched extraction.

Returns:
    List of ``(entity_type, entity_text)`` tuples. English entity types include
    PROPER, QUOTED, TOPIC, and IDENTIFIER; Chinese financial extraction also
    uses FINANCIAL_PRODUCT, INDEX, STOCK, and SECURITY_CODE.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from mem0.utils.language import contains_chinese

logger = logging.getLogger(__name__)

_FINANCE_DICT_PATH = Path(__file__).resolve().parents[1] / "configs" / "finance_bm25_dict.txt"
_MAX_ENTITY_LENGTH = 64
_CHINESE_SENTENCE_MARKS_RE = re.compile(r"[。！？；\r\n]")
_CHINESE_SECURITY_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:SH[.:-]?6\d{5}|SZ[.:-]?[03]\d{5}|BJ[.:-]?[48]\d{5}|[03648]\d{5})(?!\d)",
    re.IGNORECASE,
)
_CHINESE_FINANCIAL_NAME_RE = re.compile(
    r"[A-Za-z0-9\u3400-\u9fff]{2,32}?(?:ETF|LOF|(?:指数|混合|债券|货币|股票|联接)?基金|指数|股票)",
    re.IGNORECASE,
)
_CHINESE_ENTITY_CUES = (
    "投资于",
    "长期关注",
    "长期持有",
    "开始关注",
    "计划购买",
    "计划买入",
    "关注",
    "持有",
    "喜欢",
    "购买",
    "买入",
    "卖出",
    "配置",
    "选择",
    "偏好",
    "看好",
    "投资",
)
_CHINESE_ACCEPTED_NER_LABELS = {
    "PERSON",
    "ORG",
    "GPE",
    "LOC",
    "FAC",
    "PRODUCT",
    "WORK_OF_ART",
    "EVENT",
    "NORP",
    "LAW",
}
_RATIO_EXEMPT_ENTITY_TYPES = {
    "FINANCIAL_PRODUCT",
    "INDEX",
    "STOCK",
    "SECURITY_CODE",
    "IDENTIFIER",
}


@dataclass(frozen=True)
class _EntityCandidate:
    entity_type: str
    text: str
    source: str
    start: int
    end: int
    confidence: float
    priority: int


# Words that are too generic to be useful as entity heads
_GENERIC_HEADS = {
    "thing",
    "stuff",
    "way",
    "time",
    "experience",
    "situation",
    "case",
    "fact",
    "matter",
    "issue",
    "idea",
    "thought",
    "feeling",
    "place",
    "area",
    "part",
    "kind",
    "type",
    "sort",
    "lot",
    "bit",
    "day",
    "year",
    "week",
    "month",
    "moment",
    "instance",
    "example",
    "technique",
    "method",
    "approach",
    "process",
    "step",
    "tool",
    "result",
    "outcome",
    "goal",
    "task",
    "item",
    "topic",
    "scale",
    "size",
    "level",
    "degree",
    "amount",
    "number",
    "style",
    "look",
    "color",
    "colour",
    "shape",
    "form",
    "piece",
    "section",
    "side",
    "end",
    "edge",
    "surface",
    "point",
}

# Entity labels emitted by spaCy that are usually safe to treat as named
# entities. Numeric and temporal labels are intentionally excluded.
_ACCEPTED_NER_LABELS = {
    "PERSON",
    "ORG",
    "GPE",
    "LOC",
    "FAC",
    "PRODUCT",
    "WORK_OF_ART",
    "EVENT",
    "NORP",
    "LAW",
    "LANGUAGE",
}

_REJECTED_NER_LABELS = {
    "DATE",
    "TIME",
    "CARDINAL",
    "ORDINAL",
    "QUANTITY",
    "MONEY",
    "PERCENT",
}

# Generic role words and title-cased English words that should not become
# single-token named entities just because spaCy tagged them as PROPN.
_GENERIC_SINGLE_ENTITY_TERMS = {
    "user",
    "assistant",
    "agent",
    "customer",
    "client",
    "person",
    "people",
    "human",
    "memory",
    "message",
    "conversation",
    "chat",
    "session",
    "system",
    "top",
}

# Modifiers that describe circumstance, not content
_CIRCUMSTANTIAL_MODS = {
    "solo",
    "individual",
    "team",
    "group",
    "joint",
    "collaborative",
    "first",
    "last",
    "next",
    "previous",
    "final",
    "initial",
    "main",
    "side",
    "top",
}

# Adjectives too vague to make a compound entity specific
_NON_SPECIFIC_ADJ = {
    "many",
    "few",
    "several",
    "some",
    "any",
    "all",
    "most",
    "more",
    "less",
    "much",
    "little",
    "enough",
    "various",
    "numerous",
    "multiple",
    "countless",
    "great",
    "good",
    "bad",
    "nice",
    "terrible",
    "awful",
    "awesome",
    "amazing",
    "wonderful",
    "horrible",
    "excellent",
    "poor",
    "best",
    "worst",
    "fine",
    "okay",
    "new",
    "old",
    "recent",
    "past",
    "future",
    "current",
    "previous",
    "next",
    "last",
    "first",
    "latest",
    "early",
    "late",
    "former",
    "modern",
    "ancient",
    "big",
    "small",
    "large",
    "tiny",
    "huge",
    "enormous",
    "long",
    "short",
    "tall",
    "high",
    "low",
    "wide",
    "narrow",
    "thick",
    "thin",
    "deep",
    "shallow",
    "similar",
    "different",
    "same",
    "other",
    "another",
    "such",
    "certain",
    "important",
    "main",
    "major",
    "minor",
    "key",
    "primary",
    "real",
    "actual",
    "true",
    "whole",
    "entire",
    "full",
    "complete",
    "total",
    "basic",
    "simple",
    "interesting",
    "boring",
    "exciting",
    "special",
    "particular",
    "general",
    "common",
    "unique",
    "rare",
    "typical",
    "usual",
    "normal",
    "regular",
    "possible",
    "likely",
    "potential",
    "available",
    "necessary",
    "only",
    "solo",
    "individual",
    "team",
    "group",
    "joint",
    "collaborative",
    "final",
    "initial",
    "side",
}

# Generic tail words to strip from compound entities
_GENERIC_ENDINGS = {
    "work",
    "works",
    "job",
    "jobs",
    "task",
    "tasks",
    "stuff",
    "things",
    "thing",
    "info",
    "information",
    "details",
    "data",
    "content",
    "material",
    "materials",
    "activities",
    "activity",
    "efforts",
    "effort",
    "options",
    "option",
    "choices",
    "choice",
    "results",
    "result",
    "output",
    "outputs",
    "products",
    "product",
    "items",
    "item",
}

# Capitalized single words that are too generic to be proper nouns
_GENERIC_CAPS = {
    "works",
    "items",
    "things",
    "stuff",
    "resources",
    "options",
    "tips",
    "ideas",
    "steps",
    "ways",
    "methods",
    "tools",
    "features",
    "benefits",
    "examples",
    "details",
    "notes",
    "instructions",
    "guidelines",
    "recommendations",
    "suggestions",
    "overview",
    "summary",
    "conclusion",
    "introduction",
    "pros",
    "cons",
    "advantages",
    "disadvantages",
}

# Markdown/formatting markers to skip during extraction
_FORMATTING_MARKERS = {"*", "-", "+", "\u2022", "\u2013", "\u2014", "#", "##", "###", "**", "__"}


def _is_sentence_start(tokens: list, idx: int) -> bool:
    """Check if a token is at the start of a sentence or after formatting."""
    if idx == 0:
        return True
    tok = tokens[idx]
    if tok.is_sent_start:
        return True
    prev = tokens[idx - 1].text
    return prev in ".!?:" or prev in _FORMATTING_MARKERS or "\n" in prev


def _strip_generic_ending(toks: list) -> list:
    """Remove generic trailing words from compound token sequences."""
    if len(toks) <= 1:
        return toks
    last = toks[-1].lemma_.lower() if hasattr(toks[-1], "lemma_") else toks[-1].lower()
    return toks[:-1] if last in _GENERIC_ENDINGS and len(toks) > 2 else toks


def _lemmatize_compound(toks: list) -> str:
    """Join compound tokens, lemmatizing nouns."""
    return " ".join(t.lemma_ if t.pos_ == "NOUN" else t.text for t in toks)


def _has_artifacts(txt: str) -> bool:
    """Check for formatting artifacts that indicate non-entity text."""
    return any(
        [
            "**" in txt or "__" in txt or ":*" in txt,
            re.search(r"\s\*\s|\s\*$|^\*\s", txt),
            "  " in txt or "\n" in txt or "\t" in txt,
            len(txt) > 100,
            txt.startswith(("\u2022", "-", "+", "\u2013", "\u2014")),
        ]
    )


def _clean_text(txt: str) -> str:
    txt = re.sub(r"^\*+\s*|\s*\*+$", "", txt.strip())
    txt = re.sub(r"\s*:+$", "", txt)
    txt = re.sub(r"^\d+\s*\.\s*", "", txt)
    return " ".join(txt.split())


def _norm_text(txt: str) -> str:
    return " ".join(txt.lower().split())


def is_valid_entity(entity_type: str, entity_text: str, source_text: str) -> bool:
    """Return whether an extracted entity is safe and useful to persist."""
    if not isinstance(entity_text, str) or not isinstance(source_text, str):
        return False

    cleaned_entity = _clean_text(entity_text)
    cleaned_source = _clean_text(source_text)
    if not cleaned_entity or not any(char.isalnum() for char in cleaned_entity):
        return False
    if len(cleaned_entity) > _MAX_ENTITY_LENGTH:
        return False
    if _norm_text(cleaned_entity) == _norm_text(cleaned_source):
        return False
    if _CHINESE_SENTENCE_MARKS_RE.search(cleaned_entity):
        return False

    # Two sentence endings indicate prose rather than a named entity. Dots in
    # identifiers such as person.properties.email are intentionally ignored.
    if len(re.findall(r"[!?]|\.(?=\s|$)", cleaned_entity)) >= 2:
        return False

    entity_length = len(re.sub(r"\s+", "", cleaned_entity))
    source_length = len(re.sub(r"\s+", "", cleaned_source))
    if source_length >= 16:
        max_ratio = 0.8 if entity_type.upper() in _RATIO_EXEMPT_ENTITY_TYPES else 0.45
        if entity_length / source_length > max_ratio:
            return False

    return True


def _filter_entities(entities: list[tuple[str, str]], source_text: str) -> list[tuple[str, str]]:
    """Apply the shared quality gate and stable deduplication."""
    filtered = []
    seen = set()
    for entity_type, entity_text in entities:
        cleaned = _clean_text(entity_text)
        key = _norm_text(cleaned)
        if key in seen or not is_valid_entity(entity_type, cleaned, source_text):
            continue
        seen.add(key)
        filtered.append((entity_type, cleaned))
    return filtered


@lru_cache(maxsize=1)
def _load_financial_entity_terms() -> tuple[str, ...]:
    """Load reliable financial terms from the BM25 dictionary once."""
    try:
        terms = {
            line.strip()
            for line in _FINANCE_DICT_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
    except OSError as e:
        logger.warning("Failed to load financial entity dictionary: %s", e)
        return ()

    return tuple(sorted((term for term in terms if _classify_financial_term(term)), key=len, reverse=True))


@lru_cache(maxsize=1)
def _financial_entity_term_set() -> frozenset[str]:
    return frozenset(_load_financial_entity_terms())


def _classify_financial_term(term: str) -> str | None:
    upper = term.upper()
    if "ETF" in upper or "LOF" in upper or "基金" in term or term.endswith("可转债"):
        return "FINANCIAL_PRODUCT"
    if re.fullmatch(r"(?:沪深|中证|上证|科创)\d+", term) or term in {"创业板", "红利低波"}:
        return "INDEX"
    if term.endswith("指数"):
        return "INDEX"
    if term.endswith("股票"):
        return "STOCK"
    return None


def _looks_like_technical_identifier(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)+", text))


def _has_internal_cap_or_digit(text: str) -> bool:
    return any(ch.isdigit() for ch in text) or any(ch.isupper() for ch in text[1:])


def _looks_like_metric_count_token(tok) -> bool:
    return tok.pos_ == "NUM" and bool(re.fullmatch(r"\d[\d,]*(?:\.\d+)?", tok.text))


def _is_metric_list_context(tokens: list, idx: int) -> bool:
    prev_text = tokens[idx - 1].text if idx > 0 else ""
    next_text = tokens[idx + 1].text if idx + 1 < len(tokens) else ""
    return prev_text in {":", ",", ";"} or next_text in {",", ";"}


def _strip_trailing_metric_counts(span_tokens: list, all_tokens: list) -> list:
    while len(span_tokens) > 1 and _looks_like_metric_count_token(span_tokens[-1]):
        tok = span_tokens[-1]
        if "," not in tok.text and not _is_metric_list_context(all_tokens, tok.i):
            break
        span_tokens = span_tokens[:-1]
    return span_tokens


def _is_list_item_name_token(tokens: list, idx: int) -> bool:
    tok = tokens[idx]
    if not tok.text or tok.text in _FORMATTING_MARKERS or not tok.text[0].isupper():
        return False
    if not any(ch.isalpha() for ch in tok.text) or _is_bad_single_name_token(tok):
        return False
    next_tok = tokens[idx + 1] if idx + 1 < len(tokens) else None
    if not next_tok or not _looks_like_metric_count_token(next_tok):
        return False
    return _is_metric_list_context(tokens, idx) or _is_metric_list_context(tokens, idx + 1)


def _is_name_like_token(tok, tokens: list | None = None, idx: int | None = None) -> bool:
    if not tok.text or tok.text in _FORMATTING_MARKERS:
        return False
    if not tok.text[0].isupper():
        return False
    if not any(ch.isalpha() for ch in tok.text):
        return False
    if _is_bad_single_name_token(tok):
        return False
    if tok.pos_ == "PROPN" or tok.tag_ in {"NNP", "NNPS"}:
        return True
    if tokens is not None and idx is not None and _is_list_item_name_token(tokens, idx):
        return True
    if _has_internal_cap_or_digit(tok.text):
        return True
    return (
        tokens is not None
        and idx is not None
        and tok.pos_ == "NOUN"
        and tok.dep_ not in {"compound", "amod"}
        and not _is_sentence_start(tokens, idx)
    )


def _is_bad_single_name_token(tok) -> bool:
    lower = tok.text.lower()
    return lower in _GENERIC_SINGLE_ENTITY_TERMS or lower in _GENERIC_CAPS or tok.is_stop


def _add_candidate(
    candidates: list[_EntityCandidate],
    entity_type: str,
    text: str,
    source: str,
    start: int,
    end: int,
    confidence: float,
    priority: int,
) -> None:
    cleaned = _clean_text(text)
    if not cleaned or len(cleaned) < 2 or _has_artifacts(cleaned):
        return
    candidates.append(
        _EntityCandidate(
            entity_type=entity_type,
            text=cleaned,
            source=source,
            start=start,
            end=end,
            confidence=confidence,
            priority=priority,
        )
    )


def _add_ner_candidates(doc, candidates: list[_EntityCandidate]) -> None:
    tokens = list(doc)
    for ent in doc.ents:
        if ent.label_ in _REJECTED_NER_LABELS or ent.label_ not in _ACCEPTED_NER_LABELS:
            continue
        ent_tokens = _strip_trailing_metric_counts(list(ent), tokens)
        if not ent_tokens:
            continue
        if any(tok.pos_ == "CCONJ" and tok.text.lower() == "and" for tok in ent_tokens):
            continue
        if len(ent_tokens) == 1 and _is_bad_single_name_token(ent_tokens[0]):
            continue
        if (
            len(ent_tokens) == 1
            and ent_tokens[0].dep_ in {"compound", "amod"}
            and ent_tokens[0].head.pos_ in {"NOUN", "PROPN"}
        ):
            continue
        _add_candidate(
            candidates,
            "PROPER",
            "".join(tok.text_with_ws for tok in ent_tokens).strip(),
            "spacy_ner",
            ent_tokens[0].i,
            ent_tokens[-1].i + 1,
            0.95,
            0,
        )


def _add_technical_identifier_candidates(tokens: list, candidates: list[_EntityCandidate]) -> None:
    for tok in tokens:
        if _looks_like_technical_identifier(tok.text):
            _add_candidate(
                candidates,
                "IDENTIFIER",
                tok.text,
                "technical_identifier",
                tok.i,
                tok.i + 1,
                0.9,
                1,
            )


def _add_proper_name_candidates(tokens: list, candidates: list[_EntityCandidate]) -> None:
    allowed_inner_connectors = {"of", "the", "for", "at", "in"}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not _is_name_like_token(tok, tokens, i):
            i += 1
            continue

        span_tokens = [tok]
        j = i + 1
        while j < len(tokens):
            current = tokens[j]
            if _is_name_like_token(current, tokens, j):
                span_tokens.append(current)
                j += 1
                continue
            if (
                current.text.lower() in allowed_inner_connectors
                and j + 1 < len(tokens)
                and _is_name_like_token(tokens[j + 1], tokens, j + 1)
            ):
                span_tokens.extend([current, tokens[j + 1]])
                j += 2
                continue
            break

        name_tokens = [
            t
            for t in span_tokens
            if _is_name_like_token(t, tokens, t.i) or (0 <= t.i < len(tokens) and _is_list_item_name_token(tokens, t.i))
        ]
        if len(name_tokens) > 1 or not _is_bad_single_name_token(name_tokens[0]):
            text = "".join(t.text_with_ws for t in span_tokens).strip()
            _add_candidate(candidates, "PROPER", text, "proper_name_span", i, j, 0.8, 2)
        i = max(j, i + 1)


def _add_quoted_candidates(text: str, candidates: list[_EntityCandidate]) -> None:
    for m in re.finditer(r'"([^"]+)"', text):
        if len(m.group(1).strip()) > 2:
            _add_candidate(candidates, "QUOTED", m.group(1).strip(), "quoted", -1, -1, 0.75, 3)
    for m in re.finditer(r"(?:^|[\s\(\[{,;])'([^']+)'(?=[\s\.,;:!?\)\]]|$)", text):
        if len(m.group(1).strip()) > 2:
            _add_candidate(candidates, "QUOTED", m.group(1).strip(), "quoted", -1, -1, 0.75, 3)


def _add_topic_phrase_candidates(doc, candidates: list[_EntityCandidate]) -> None:
    for chunk in doc.noun_chunks:
        chunk_tokens = list(chunk)
        split_indices: list[int] = []
        poss_splits: list[int] = []
        for idx, tok in enumerate(chunk_tokens):
            if tok.dep_ == "case" and tok.text in {"'s", "\u2019s", "'"}:
                split_indices.append(idx)
                poss_splits.append(idx)
            elif tok.pos_ == "PUNCT" and tok.text in {"'", '"', "\u2018", "\u2019", "\u201c", "\u201d"}:
                split_indices.append(idx)

        if split_indices:
            groups: list[list] = []
            prev = 0
            for split_idx in split_indices:
                if split_idx > prev:
                    groups.append(chunk_tokens[prev:split_idx])
                if split_idx in poss_splits:
                    next_split = next((s for s in split_indices if s > split_idx), None)
                    owned = chunk_tokens[split_idx + 1 : next_split if next_split else len(chunk_tokens)]
                    if owned:
                        first_content = next((t for t in owned if t.pos_ not in {"PUNCT", "PART"}), None)
                        if not (first_content and first_content.text and first_content.text[0].isupper()):
                            prev = next_split if next_split else len(chunk_tokens)
                            continue
                prev = split_idx + 1
            if prev < len(chunk_tokens):
                groups.append(chunk_tokens[prev:])
        else:
            groups = [chunk_tokens]

        for group in groups:
            if not group:
                continue
            head = next((t for t in reversed(group) if t.pos_ in {"NOUN", "PROPN"}), None)
            if not head:
                continue
            head_generic = head.lemma_.lower() in _GENERIC_HEADS
            content = [
                t
                for t in group
                if t.pos_ not in {"DET", "PRON", "PUNCT", "PART", "ADP", "SCONJ", "NUM"}
                and (t.pos_ == "ADJ" or not t.is_stop)
            ]
            if not content:
                continue

            compound_toks = [t for t in content if t.dep_ == "compound"]
            adj_toks = [t for t in content if t.pos_ == "ADJ" or t.dep_ == "amod"]
            has_spec_adj = any(t.lemma_.lower() not in _NON_SPECIFIC_ADJ for t in adj_toks)
            if head_generic and not has_spec_adj and not compound_toks:
                continue

            if compound_toks:
                is_circ = any(t.lemma_.lower() in _CIRCUMSTANTIAL_MODS for t in compound_toks)
                if is_circ:
                    val = head.text
                    if len(val) > 2:
                        _add_candidate(
                            candidates,
                            "TOPIC",
                            val,
                            "topic_phrase",
                            head.i,
                            head.i + 1,
                            0.45,
                            4,
                        )
                else:
                    filtered = _strip_generic_ending(
                        [t for t in content if not (t.pos_ == "ADJ" and t.lemma_.lower() in _NON_SPECIFIC_ADJ)]
                    )
                    if filtered:
                        phrase = " ".join(t.text for t in filtered)
                        if len(phrase) > 3 and " " in phrase:
                            _add_candidate(
                                candidates,
                                "TOPIC",
                                phrase,
                                "topic_phrase",
                                filtered[0].i,
                                filtered[-1].i + 1,
                                0.45,
                                4,
                            )
            elif len(content) > 1 and has_spec_adj:
                filtered = _strip_generic_ending(
                    [
                        t
                        for t in content
                        if not ((t.pos_ == "ADJ" or t.dep_ == "amod") and t.lemma_.lower() in _NON_SPECIFIC_ADJ)
                    ]
                )
                if filtered:
                    phrase = " ".join(t.text for t in filtered)
                    if len(phrase) > 3 and " " in phrase:
                        _add_candidate(
                            candidates,
                            "TOPIC",
                            phrase,
                            "topic_phrase",
                            filtered[0].i,
                            filtered[-1].i + 1,
                            0.45,
                            4,
                        )


def _strip_example_prefix(text: str) -> str:
    cleaned = text.strip()

    for prefix in ("例如", "比如", "譬如", "诸如"):
        if cleaned.startswith(prefix):
            return cleaned[len(prefix) :].lstrip("，,:：、 ")

    if cleaned.startswith("如"):
        remainder = cleaned[1:].lstrip("，,:：、 ")
        if remainder in _financial_entity_term_set():
            return remainder

    return cleaned


def _normalize_chinese_financial_name(text: str) -> str:
    cleaned = _strip_example_prefix(text).strip(" \t:：()（）[]【】")
    cue_positions = [(cleaned.rfind(cue), cue) for cue in _CHINESE_ENTITY_CUES if cue in cleaned]
    if cue_positions:
        position, cue = max(cue_positions, key=lambda item: item[0])
        cleaned = cleaned[position + len(cue) :]
    cleaned = re.sub(r"^(?:用户|客户|本人|我|其|并|也|将|已|开始|计划|长期)+", "", cleaned)
    return _strip_example_prefix(cleaned).lstrip("和及与的")


def _add_chinese_ner_candidates(doc, candidates: list[_EntityCandidate]) -> None:
    for ent in doc.ents:
        if ent.label_ not in _CHINESE_ACCEPTED_NER_LABELS:
            continue
        entity_type = "FINANCIAL_PRODUCT" if ent.label_ == "PRODUCT" else "PROPER"
        _add_candidate(
            candidates,
            entity_type,
            ent.text,
            "chinese_spacy_ner",
            ent.start_char,
            ent.end_char,
            0.9,
            1,
        )


def _add_chinese_financial_candidates(text: str, candidates: list[_EntityCandidate]) -> None:
    for term in _load_financial_entity_terms():
        entity_type = _classify_financial_term(term)
        for match in re.finditer(re.escape(term), text, re.IGNORECASE):
            _add_candidate(
                candidates,
                entity_type,
                match.group(0),
                "financial_dictionary",
                match.start(),
                match.end(),
                0.95,
                2,
            )

    for fragment_match in re.finditer(r"[^，。！？；、,;\r\n]+", text):
        fragment = fragment_match.group(0)
        for match in _CHINESE_FINANCIAL_NAME_RE.finditer(fragment):
            raw_name = _normalize_chinese_financial_name(match.group(0))
            if not raw_name:
                continue
            upper = raw_name.upper()
            if "ETF" in upper or "LOF" in upper or raw_name.endswith("基金"):
                entity_type = "FINANCIAL_PRODUCT"
            elif raw_name.endswith("指数"):
                entity_type = "INDEX"
            else:
                entity_type = "STOCK"
                raw_name = raw_name.removesuffix("股票")
            start = fragment_match.start() + match.start()
            _add_candidate(
                candidates,
                entity_type,
                raw_name,
                "financial_name_pattern",
                start,
                fragment_match.start() + match.end(),
                0.98,
                0,
            )


def _stock_name_before_code(text: str, code_start: int) -> str | None:
    prefix = re.split(r"[，。！？；、,;\r\n]", text[:code_start])[-1]
    prefix = prefix.rstrip(" \t:：（(")
    prefix = _normalize_chinese_financial_name(prefix)
    prefix = re.split(r"(?:和|及|与)", prefix)[-1]
    match = re.search(r"([\u3400-\u9fff]{2,12})$", prefix)
    if not match:
        return None
    name = match.group(1)
    if name in {"证券代码", "股票代码", "基金代码", "产品代码"}:
        return None
    return name


def _add_chinese_security_candidates(text: str, candidates: list[_EntityCandidate]) -> None:
    for match in _CHINESE_SECURITY_CODE_RE.finditer(text):
        _add_candidate(
            candidates,
            "SECURITY_CODE",
            match.group(0).upper(),
            "security_code_pattern",
            match.start(),
            match.end(),
            1.0,
            0,
        )
        stock_name = _stock_name_before_code(text, match.start())
        if stock_name:
            name_start = text.rfind(stock_name, 0, match.start())
            _add_candidate(
                candidates,
                "STOCK",
                stock_name,
                "stock_code_context",
                name_start,
                name_start + len(stock_name),
                0.95,
                1,
            )


def _extract_chinese_entities(text: str, doc=None) -> list[tuple[str, str]]:
    candidates: list[_EntityCandidate] = []

    if doc is None:
        from mem0.utils.spacy_models import get_nlp_chinese

        nlp = get_nlp_chinese()
        if nlp is not None:
            try:
                doc = nlp(text)
            except Exception as e:
                logger.warning("Chinese spaCy NER failed; using strict fallback extraction: %s", e)

    if doc is not None:
        _add_chinese_ner_candidates(doc, candidates)
    _add_chinese_financial_candidates(text, candidates)
    _add_chinese_security_candidates(text, candidates)
    return _filter_entities(_resolve_candidates(candidates), text)


def _spans_overlap(a: _EntityCandidate, b: _EntityCandidate) -> bool:
    if a.start < 0 or b.start < 0:
        return False
    return a.start < b.end and b.start < a.end


def _resolve_candidates(candidates: list[_EntityCandidate]) -> list[tuple[str, str]]:
    deduped_by_text: dict[str, _EntityCandidate] = {}
    for candidate in candidates:
        key = _norm_text(candidate.text)
        current = deduped_by_text.get(key)
        if current is None or (candidate.priority, -candidate.confidence) < (current.priority, -current.confidence):
            deduped_by_text[key] = candidate

    ordered = sorted(
        deduped_by_text.values(),
        key=lambda c: (c.priority, -c.confidence, -(c.end - c.start), c.start),
    )
    accepted: list[_EntityCandidate] = []
    for candidate in ordered:
        if any(
            _spans_overlap(candidate, existing)
            and not (candidate.entity_type == "TOPIC" and " " in candidate.text and existing.entity_type == "PROPER")
            for existing in accepted
        ):
            continue
        accepted.append(candidate)

    accepted.sort(key=lambda c: (c.start if c.start >= 0 else 10**9, c.end, c.priority))
    return [(candidate.entity_type, candidate.text) for candidate in accepted]


def _extract_entities_from_doc(doc) -> list[tuple[str, str]]:
    """Extract typed entity candidates from a spaCy Doc.

    Args:
        doc: A spaCy ``Doc`` object (from ``nlp(text)``).

    Returns:
        Deduplicated list of ``(entity_type, entity_text)`` tuples.
        Entity types include PROPER, QUOTED, TOPIC, and IDENTIFIER.
    """
    tokens = list(doc)
    candidates: list[_EntityCandidate] = []
    _add_ner_candidates(doc, candidates)
    _add_technical_identifier_candidates(tokens, candidates)
    _add_proper_name_candidates(tokens, candidates)
    _add_quoted_candidates(doc.text, candidates)
    _add_topic_phrase_candidates(doc, candidates)
    return _filter_entities(_resolve_candidates(candidates), doc.text)


def extract_entities(text: str) -> list[tuple[str, str]]:
    """Extract typed entity candidates from text."""
    if not text:
        return []
    if contains_chinese(text):
        return _extract_chinese_entities(text)

    from mem0.utils.spacy_models import get_nlp_full

    nlp = get_nlp_full()
    if nlp is None:
        return []
    return _extract_entities_from_doc(nlp(text))


def extract_entities_batch(texts: list[str], batch_size: int = 32) -> list[list[tuple[str, str]]]:
    """Extract entities in batches while preserving single-item language rules."""
    if not texts:
        return []

    results: list[list[tuple[str, str]]] = [[] for _ in texts]
    chinese_indices = [index for index, text in enumerate(texts) if contains_chinese(text)]
    english_indices = [index for index, text in enumerate(texts) if text and not contains_chinese(text)]

    if chinese_indices:
        from mem0.utils.spacy_models import get_nlp_chinese

        chinese_nlp = get_nlp_chinese()
        chinese_docs = {}
        if chinese_nlp is not None:
            try:
                docs = chinese_nlp.pipe((texts[index] for index in chinese_indices), batch_size=batch_size)
                chinese_docs = dict(zip(chinese_indices, docs))
            except Exception as e:
                logger.warning("Batched Chinese spaCy NER failed; using strict fallback extraction: %s", e)
        for index in chinese_indices:
            results[index] = _extract_chinese_entities(texts[index], chinese_docs.get(index))

    if english_indices:
        from mem0.utils.spacy_models import get_nlp_full

        english_nlp = get_nlp_full()
        if english_nlp is not None:
            docs = english_nlp.pipe((texts[index] for index in english_indices), batch_size=batch_size)
            for index, doc in zip(english_indices, docs):
                results[index] = _extract_entities_from_doc(doc)

    return results
