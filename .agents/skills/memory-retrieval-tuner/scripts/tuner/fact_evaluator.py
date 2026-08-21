"""Requirement-context evaluator for complete layered Memory.

It intentionally does not use embedding similarity as a Gold decision.  Facts
with a deterministic representation are checked exactly; ordinary language is
handled by a small controlled semantic judge (or an injected judge in real
experiments).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

_NUMBER = re.compile(r"(?<!\d)[-+]?\d+(?:[,.]\d+)*(?:\.\d+)?%?")
_DATE = re.compile(r"(?:\d{4}年(?:\d{1,2}月(?:\d{1,2}日)?)?|\d{4}/\d{1,2}(?:/\d{1,2})?|\d{4}-\d{1,2}(?:-\d{1,2})?|\d{1,2}月\d{1,2}日)")
_AMOUNT = re.compile(r"[-+]?\d+(?:[,.]\d+)*(?:\.\d+)?\s*(?:元|万元|亿元|美元|USD|RMB|¥|￥|万|亿)")
_UNIT = re.compile(r"(?:%|百分比|百分点|元|万元|亿元|美元|USD|RMB|吨|公斤|千克|公里|小时|天|人|件|次|个)")
_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_ENTITY_SUFFIX = re.compile(
    r"(?:公司|有限公司|集团|银行|证券|基金|股份|科技|智能|装备|控股|指数|股票|产品|型号|ETF)$",
    re.IGNORECASE,
)
_ENTITY_LABEL = re.compile(r"(?:公司名|公司|人名|姓名|产品名|产品|指数名|指数|股票|证券|基金|型号|代码|ETF)", re.IGNORECASE)
_ENTITY_CODE = re.compile(r"\b(?:[A-Z]{1,6}\s*[-/]?\s*\d{0,6}|\d{3,6})\b")
_ALIAS = re.compile(r"(?:别名|alias)\s*[:：]\s*([^,，;；)）]+)", re.IGNORECASE)


@dataclass(frozen=True)
class FactRequirement:
    members: tuple[str, ...]
    raw_text: str

    @property
    def is_or(self) -> bool:
        return len(self.members) > 1


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower().replace(",", "")


def extract_deterministic_facts(value: str) -> dict[str, set[str]]:
    text = str(value or "")
    return {
        "number": {_norm(item) for item in _NUMBER.findall(text)},
        "date": {_norm(item) for item in _DATE.findall(text)},
        "amount": {_norm(item) for item in _AMOUNT.findall(text)},
        "unit": {_norm(item) for item in _UNIT.findall(text)},
    }


def deterministic_fact_match(gold: str, visible: str) -> bool:
    """Check numbers, percentages, dates, amounts and units exactly.

    A number and its unit are treated as one fact where possible (``12%`` does
    not match ``12 元``).  Text with no deterministic token is delegated to
    the semantic judge by :func:`fact_member_hit`.
    """
    expected = extract_deterministic_facts(gold)
    actual = extract_deterministic_facts(visible)
    for key in ("amount", "date", "number"):
        for item in expected[key]:
            if item not in actual[key]:
                return False
    # Units are only mandatory when the Gold explicitly contains a unit.
    return expected["unit"].issubset(actual["unit"])


def controlled_semantic_judge(gold: str, visible: str) -> bool:
    """Conservative token-based semantic judge used only as a fallback.

    Production runs may inject an LLM judge.  This deterministic fallback uses
    all meaningful Gold tokens and therefore cannot turn an unrelated embedding
    neighbour into a Gold hit.
    """
    if _norm(gold) and _norm(gold) in _norm(visible):
        return True
    gold_tokens = {token.lower() for token in _TOKEN.findall(str(gold)) if len(token) > 1}
    visible_tokens = {token.lower() for token in _TOKEN.findall(str(visible))}
    if not gold_tokens:
        return bool(_norm(gold) and _norm(gold) in _norm(visible))
    overlap = len(gold_tokens & visible_tokens) / len(gold_tokens)
    return overlap >= 0.6


def _looks_like_entity(value: str) -> bool:
    """Recognise entity-shaped Gold conservatively.

    This is deliberately not a general NER model.  It only activates for
    explicit entity labels/suffixes or exchange-style codes.  Bare names are
    still accepted by exact matching, but never by token-overlap matching.
    """
    text = str(value or "").strip()
    normalized = _norm(text)
    if not normalized:
        return False
    return bool(
        _ENTITY_SUFFIX.search(text)
        or _ENTITY_LABEL.search(text)
        or _ENTITY_CODE.search(text)
        or _ALIAS.search(text)
    )


def _controlled_entity_match(gold: str, visible: str) -> bool:
    expected = {_norm(gold)}
    # Only aliases explicitly supplied by the benchmark are accepted.  Do not
    # manufacture broad abbreviations (which cause company/person false hits).
    expected.update(_norm(alias) for alias in _ALIAS.findall(str(gold)) if _norm(alias))
    visible_norm = _norm(visible)
    return any(alias and alias in visible_norm for alias in expected)


def fact_member_hit(
    member: str,
    visible_text: str,
    *,
    semantic_judge: Callable[[str, str], bool] | None = None,
) -> bool:
    expected = extract_deterministic_facts(member)
    has_deterministic = any(expected[key] for key in expected)
    if has_deterministic and not deterministic_fact_match(member, visible_text):
        return False
    if has_deterministic:
        # Deterministic fields are sufficient only when the member is made up
        # of those fields; otherwise check its entity/text portion as well.
        residual = _DATE.sub(" ", _AMOUNT.sub(" ", member))
        residual = re.sub(r"\d+(?:[,.]\d+)*(?:\.\d+)?%?", " ", residual)
        residual = _UNIT.sub(" ", residual)
        residual = re.sub(r"[^\w\u4e00-\u9fff]+", " ", residual)
        if not residual.strip():
            return True
        # A deterministic fact may carry an entity (for example, ``华辰公司
        # 收入100万元``).  Keep the entity check conservative as well.
        if _looks_like_entity(residual) and not _controlled_entity_match(residual, visible_text):
            return False
    elif _looks_like_entity(member):
        return _controlled_entity_match(member, visible_text)
    judge = semantic_judge or controlled_semantic_judge
    return bool(judge(member, visible_text))


def parse_required_context(value: str) -> tuple[FactRequirement, ...]:
    """Parse ``(A OR B) AND C`` without changing the fixed denominator."""
    text = str(value or "").strip()
    if not text:
        return ()
    # Protect parenthesized groups while splitting top-level AND.
    groups: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "(（":
            depth += 1
        elif char in ")）":
            depth = max(depth - 1, 0)
        if depth == 0 and (text[index:index + 3].upper() == "AND" or char in "且;；"):
            groups.append(text[start:index].strip())
            start = index + (3 if text[index:index + 3].upper() == "AND" else 1)
    groups.append(text[start:].strip())
    output: list[FactRequirement] = []
    for group in groups:
        group = group.strip(" ()（）")
        members = re.split(r"\s+(?:OR|or)\s+|\s*或\s*|[；;]", group)
        members = tuple(item.strip(" ()（）") for item in members if item.strip(" ()（）"))
        if members:
            output.append(FactRequirement(members, group))
    return tuple(output)


def requirement_hit(requirement: FactRequirement, visible_text: str, *, semantic_judge=None) -> bool:
    return any(fact_member_hit(member, visible_text, semantic_judge=semantic_judge) for member in requirement.members)


def evaluate_required_context(
    required_context: str,
    visible_texts: Iterable[str],
    *,
    semantic_judge=None,
) -> tuple[list[dict[str, object]], list[FactRequirement]]:
    requirements = list(parse_required_context(required_context))
    visible = "\n".join(str(item) for item in visible_texts if item)
    rows = []
    for index, requirement in enumerate(requirements, start=1):
        rows.append(
            {
                "requirement_id": f"context::G{index}",
                "gold_members": list(requirement.members),
                "is_or": requirement.is_or,
                "hit": requirement_hit(requirement, visible, semantic_judge=semantic_judge),
            }
        )
    return rows, requirements


# Stable, discoverable names for downstream evaluator adapters.
evaluate_fact = fact_member_hit
evaluate_requirement = requirement_hit
