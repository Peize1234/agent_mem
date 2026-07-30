from __future__ import annotations

import json
import logging
from datetime import date, datetime, time
from numbers import Number
from typing import Any

logger = logging.getLogger(__name__)


def normalize_table_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return an Arrow-safe display copy without mutating source records."""
    rows = [{key: _normalize_cell(value) for key, value in record.items()} for record in records]
    columns = tuple(dict.fromkeys(key for row in rows for key in row))
    for column in columns:
        non_null = [row.get(column) for row in rows if row.get(column) is not None]
        if len({_value_family(value) for value in non_null}) <= 1:
            continue
        for row in rows:
            if row.get(column) is not None:
                row[column] = _scalar_text(row[column])
    return rows


def render_table(
    st,
    records: list[dict[str, Any]],
    *,
    key_prefix: str,
) -> bool:
    """Render normalized rows, degrading locally to the original JSON."""
    try:
        st.dataframe(
            normalize_table_rows(records),
            width="stretch",
            hide_index=True,
        )
        return True
    except Exception:
        logger.exception("Could not render Memory Monitor table key=%s", key_prefix)
        st.error("表格暂时无法显示，已切换为原始 JSON。")
        render_json(
            st,
            records,
            label="原始记录 JSON",
            expanded=False,
            key=f"{key_prefix}:table_fallback",
        )
        return False


def render_json(
    st,
    value: Any,
    *,
    label: str = "详情",
    expanded: bool = False,
    key: str | None = None,
) -> None:
    with st.expander(label, expanded=expanded, key=key):
        st.code(json.dumps(value, ensure_ascii=False, indent=2, default=str), language="json")


def render_records(
    st,
    records: list[dict],
    *,
    key_prefix: str,
    empty: str = "暂无记录",
) -> None:
    if not records:
        st.caption(empty)
        return
    st.caption(f"{len(records)} 条记录")
    render_table(st, records, key_prefix=key_prefix)
    selected = st.selectbox(
        "查看记录详情",
        range(len(records)),
        format_func=lambda index: _record_label(records[index], index),
        key=f"{key_prefix}:detail_selector",
    )
    render_json(
        st,
        records[selected],
        label="选中记录",
        expanded=False,
        key=f"{key_prefix}:selected_record",
    )


def _record_label(record: dict, index: int) -> str:
    identifier = (
        record.get("id") or record.get("job_id") or record.get("attribute_key") or record.get("attribute_id") or index
    )
    return str(identifier)


def _normalize_cell(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, Number)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, set):
        value = sorted(value, key=str)
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return json.dumps(str(value), ensure_ascii=False, default=str)
    return str(value)


def _value_family(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, Number):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def _scalar_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
