"""Gold requirement parsing for the enterprise-finance memory benchmark.

The V3 dataset uses full-width parentheses to express alternatives.  A top-level
semicolon separates independent requirements, whereas semicolons inside a pair
of parentheses separate members of one OR requirement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

TURN_ID_RE = re.compile(r"S\d{3}-Q\d{3}", re.IGNORECASE)
EMPTY_GOLD_VALUES = {"", "无", "none", "null", "nan"}


@dataclass(frozen=True)
class GoldRequirement:
    """One denominator unit, containing one or more alternative source turns."""

    members: tuple[str, ...]
    raw_text: str

    @property
    def is_or(self) -> bool:
        return len(self.members) > 1

    def hit_by(self, retrieved_turn_ids: Iterable[str]) -> bool:
        retrieved = {str(value).upper() for value in retrieved_turn_ids}
        return any(member in retrieved for member in self.members)


def _split_top_level(value: str) -> list[str]:
    groups: list[str] = []
    start = 0
    depth = 0
    matching = {"（": "）", "(": ")"}
    closing = {"）", ")"}
    stack: list[str] = []
    for index, character in enumerate(value):
        if character in matching:
            stack.append(matching[character])
            depth += 1
        elif character in closing:
            if not stack or stack.pop() != character:
                raise ValueError(f"Gold 表达式括号不匹配：{value}")
            depth -= 1
        elif character in {"；", ";"} and depth == 0:
            groups.append(value[start:index].strip())
            start = index + 1
    if stack:
        raise ValueError(f"Gold 表达式括号不闭合：{value}")
    groups.append(value[start:].strip())
    if any(not group for group in groups):
        raise ValueError(f"Gold 表达式包含空 requirement：{value}")
    return groups


def _strip_outer_parentheses(value: str) -> tuple[str, bool]:
    pairs = {"（": "）", "(": ")"}
    if not value or value[0] not in pairs:
        return value, False
    expected = pairs[value[0]]
    if value[-1] != expected:
        raise ValueError(f"Gold OR group 括号不匹配：{value}")
    depth = 0
    for index, character in enumerate(value):
        if character in pairs:
            depth += 1
        elif character in {"）", ")"}:
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return value, False
    return value[1:-1].strip(), True


def parse_gold_requirements(value: Any) -> tuple[GoldRequirement, ...]:
    """Parse independent and parenthesized-OR Gold requirements.

    Examples:
      ``A；B`` -> ``{A}``, ``{B}``
      ``（A；B）`` -> ``{A,B}``
      ``（A；B）；C`` -> ``{A,B}``, ``{C}``
    """

    text = str(value or "").strip()
    if text.lower() in EMPTY_GOLD_VALUES:
        return ()
    requirements: list[GoldRequirement] = []
    for raw_group in _split_top_level(text):
        inner, parenthesized = _strip_outer_parentheses(raw_group)
        member_texts = re.split(r"[；;]", inner) if parenthesized else [inner]
        members: list[str] = []
        for member_text in member_texts:
            matches = [match.upper() for match in TURN_ID_RE.findall(member_text)]
            if len(matches) != 1:
                raise ValueError(f"Gold member 必须且只能包含一个 turn id：{member_text!r}（来自 {text!r}）")
            if matches[0] not in members:
                members.append(matches[0])
        if parenthesized and len(members) < 2:
            raise ValueError(f"括号 OR group 至少需要两个不同成员：{raw_group}")
        requirements.append(GoldRequirement(tuple(members), raw_group))
    return tuple(requirements)
