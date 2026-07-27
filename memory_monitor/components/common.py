from __future__ import annotations

import json
from typing import Any


def render_json(st, value: Any, *, label: str = "Details", expanded: bool = False) -> None:
    with st.expander(label, expanded=expanded):
        st.code(json.dumps(value, ensure_ascii=False, indent=2, default=str), language="json")


def filter_values(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}
