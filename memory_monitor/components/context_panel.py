from __future__ import annotations

from memory_monitor.components.common import render_records, render_table


def render(st, context: dict | None, *, key_prefix: str) -> None:
    if not context:
        st.caption("执行“检索上下文”后显示冻结结果。")
        return
    st.caption(f"Context hash: `{context.get('context_hash', '')}`")
    sections = (
        ("短期记忆", context.get("short_term") or context.get("short_term_messages") or []),
        ("中期记忆", context.get("mid_term") or []),
        ("细粒度长期记忆", context.get("fine_grained_longterm") or []),
        ("跨 Session 长期记忆", context.get("promoted_longterm") or []),
    )
    for section, (title, records) in enumerate(sections):
        st.markdown(f"#### {title}")
        render_records(st, records, key_prefix=f"{key_prefix}:section:{section}")
    st.markdown("#### 用户画像")
    profile = context.get("user_profile") or context.get("profile") or {}
    if profile:
        render_table(
            st,
            [{"属性": key, "值": value} for key, value in profile.items()],
            key_prefix=f"{key_prefix}:profile",
        )
    else:
        st.caption("暂无画像")
