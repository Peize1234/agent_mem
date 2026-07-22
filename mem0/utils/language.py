"""Small language-detection helpers shared by text processing utilities."""

import re

_CHINESE_RE = re.compile(r"[\u3400-\u9fff]")


def contains_chinese(text: str) -> bool:
    """Return whether *text* contains a CJK Unified Ideograph."""
    return bool(_CHINESE_RE.search(text))
