from __future__ import annotations

import json
from typing import Any


def render_json(st, value: Any, *, label: str = "详情", expanded: bool = False) -> None:
    with st.expander(label, expanded=expanded):
        st.code(json.dumps(value, ensure_ascii=False, indent=2, default=str), language="json")


def render_records(st, records: list[dict], *, empty: str = "暂无记录") -> None:
    if not records:
        st.caption(empty)
        return
    st.caption(f"{len(records)} 条记录")
    st.dataframe(records, use_container_width=True, hide_index=True)
    selected = st.selectbox(
        "查看记录详情",
        range(len(records)),
        format_func=lambda index: _record_label(records[index], index),
    )
    render_json(st, records[selected], label="选中记录", expanded=False)


def _record_label(record: dict, index: int) -> str:
    identifier = (
        record.get("id") or record.get("job_id") or record.get("attribute_key") or record.get("attribute_id") or index
    )
    return str(identifier)
