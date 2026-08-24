from __future__ import annotations

from dataclasses import dataclass

from memory_monitor.components.common import render_records, render_table


@dataclass(frozen=True)
class QueryRewriteSummary:
    query: str
    retrieval_query: str
    changed: bool
    retrieval_query_recorded: bool

    @property
    def status_label(self) -> str:
        if self.changed:
            return "已改写"
        return "未发生改写 / 原始问题直接用于检索"


def query_rewrite_summary(context: dict) -> QueryRewriteSummary:
    """Build display-only rewrite data from the frozen production context."""
    query = str(context.get("query") or "")
    retrieval_query_recorded = "retrieval_query" in context
    retrieval_query = str(context.get("retrieval_query") or query)
    return QueryRewriteSummary(
        query=query,
        retrieval_query=retrieval_query,
        changed=retrieval_query != query,
        retrieval_query_recorded=retrieval_query_recorded,
    )


def render(st, context: dict | None, *, key_prefix: str) -> None:
    if not context:
        st.caption("执行“分层检索”后显示冻结结果。")
        return
    st.caption(f"Context hash: `{context.get('context_hash', '')}`")
    rewrite = query_rewrite_summary(context)
    st.markdown("#### 问题重写")
    st.markdown("**原始问题：**")
    st.code(rewrite.query, language=None)
    st.caption(f"↓ 问题重写 · {rewrite.status_label}")
    st.markdown("**检索问题：**")
    st.code(rewrite.retrieval_query, language=None)
    if not rewrite.retrieval_query_recorded:
        st.caption("历史 Demo 数据未单独记录 retrieval_query，按原始问题兼容展示。")
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
