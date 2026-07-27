from __future__ import annotations

from memory_monitor.components.common import render_records


def render(st, context: dict | None) -> None:
    if not context:
        st.caption("执行“检索上下文”后显示冻结结果。")
        return
    st.caption(f"Context hash: `{context.get('context_hash', '')}`")
    sections = (
        ("短期记忆", context.get("short_term") or context.get("short_term_messages") or []),
        ("中期记忆", context.get("mid_term") or []),
        ("长期记忆", context.get("long_term") or []),
    )
    for title, records in sections:
        st.markdown(f"#### {title}")
        render_records(st, records)
    st.markdown("#### 用户画像")
    profile = context.get("user_profile") or context.get("profile") or {}
    if profile:
        st.dataframe(
            [{"属性": key, "值": value} for key, value in profile.items()],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.caption("暂无画像")
