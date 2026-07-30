from __future__ import annotations

from memory_monitor.components.common import render_json, render_table


def render(st, steps: list[dict], *, key_prefix: str) -> None:
    if not steps:
        st.caption("暂无 Trace。")
        return
    render_table(
        st,
        [
            {
                "step": step["step"],
                "status": step["status"],
                "attempts": step["attempts"],
                "started_at": step.get("started_at"),
                "ended_at": step.get("ended_at"),
                "duration_ms": step.get("duration_ms"),
                "error": step.get("error_message"),
            }
            for step in steps
        ],
        key_prefix=f"{key_prefix}:steps",
    )
    selected = st.selectbox(
        "Trace 步骤",
        [step["step"] for step in steps],
        key=f"{key_prefix}:step_selector",
    )
    step = next(item for item in steps if item["step"] == selected)
    render_json(st, step.get("input"), label="输入", key=f"{key_prefix}:input")
    render_json(st, step.get("output"), label="输出", key=f"{key_prefix}:output")
    render_json(st, step.get("diff"), label="数据库差异", key=f"{key_prefix}:diff")
