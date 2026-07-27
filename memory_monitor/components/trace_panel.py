from __future__ import annotations

from memory_monitor.components.common import render_json


def render(st, steps: list[dict]) -> None:
    if not steps:
        st.caption("暂无 Trace。")
        return
    st.dataframe(
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
        use_container_width=True,
        hide_index=True,
    )
    selected = st.selectbox("Trace 步骤", [step["step"] for step in steps])
    step = next(item for item in steps if item["step"] == selected)
    render_json(st, step.get("input"), label="输入")
    render_json(st, step.get("output"), label="输出")
    render_json(st, step.get("diff"), label="数据库差异")
