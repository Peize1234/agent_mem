from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from memory_monitor.components.common import render_json, render_records, render_table


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


def midterm_retrieval_rows(records: list[dict]) -> list[dict[str, Any]]:
    """Return the production MidTerm Page score chain in display order."""
    return [
        {
            "Page": record.get("id"),
            "Session": record.get("session_id"),
            "摘要": record.get("summary") or record.get("memory"),
            "raw_rag_score（原始）": record.get("raw_rag_score"),
            "× forgetting_factor（保留）": record.get("forgetting_factor"),
            "→ final_score（最终）": record.get("final_score"),
            "heat_factor": record.get("heat_factor"),
            "effective_half_life_turns": record.get("effective_half_life_turns"),
            "valid_recall_count": record.get("valid_recall_count"),
            "last_recall_turn_index": record.get("last_recall_turn_index"),
            "turn_index": record.get("turn_index"),
        }
        for record in records
        if _is_midterm_page(record)
    ]


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
    st.markdown("#### 短期记忆")
    render_records(
        st,
        context.get("short_term") or context.get("short_term_messages") or [],
        key_prefix=f"{key_prefix}:section:shortterm",
    )
    st.markdown("#### 中期记忆")
    _render_midterm_retrieval(
        st,
        context.get("mid_term") or [],
        key_prefix=f"{key_prefix}:midterm",
    )
    sections = (
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


def _render_midterm_retrieval(st, records: list[dict], *, key_prefix: str) -> None:
    if not records:
        st.caption("暂无记录")
        return
    pages = [record for record in records if _is_midterm_page(record)]
    sessions = [record for record in records if not _is_midterm_page(record)]
    st.caption(f"{len(records)} 条记录")
    if sessions:
        st.markdown("##### Session 候选")
        render_table(
            st,
            [
                {
                    "Session": record.get("session_id") or record.get("id"),
                    "摘要": record.get("summary") or record.get("memory"),
                    "R_recency": record.get("R_recency"),
                    "H_segment": record.get("H_segment"),
                    "valid_recall_count": record.get("valid_recall_count"),
                }
                for record in sessions
            ],
            key_prefix=f"{key_prefix}:sessions",
        )
    if pages:
        st.markdown("##### Page 遗忘与最终分数")
        render_table(st, midterm_retrieval_rows(pages), key_prefix=f"{key_prefix}:pages")
        selected_page = st.selectbox(
            "查看 Page 分数链路",
            range(len(pages)),
            format_func=lambda index: str(pages[index].get("id") or index),
            key=f"{key_prefix}:page_selector",
        )
        page = pages[selected_page]
        st.markdown(
            f"`raw_rag_score {_format_score(page.get('raw_rag_score'))}` "
            f"× `forgetting_factor {_format_score(page.get('forgetting_factor'))}` "
            f"→ `final_score {_format_score(page.get('final_score'))}`"
        )
        if page.get("rerank_score") is not None or page.get("first_stage_score") is not None:
            st.caption("final_score 含生产重排后的结果，因此可能不等于当前展示的遗忘乘积。")
    selected = st.selectbox(
        "查看中期完整字段",
        range(len(records)),
        format_func=lambda index: str(records[index].get("id") or index),
        key=f"{key_prefix}:detail_selector",
    )
    render_json(
        st,
        records[selected],
        label="选中中期记录原始 JSON",
        expanded=False,
        key=f"{key_prefix}:selected_record",
    )


def _is_midterm_page(record: dict) -> bool:
    return record.get("source") == "mid_term_page" or "raw_rag_score" in record


def _format_score(value: Any) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return "不适用"
