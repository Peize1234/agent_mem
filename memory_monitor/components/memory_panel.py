from __future__ import annotations

from collections.abc import Callable, Iterable

from memory_monitor.components.common import render_records, render_table
from memory_monitor.models import MEMORY_STATE_SECTIONS

_SECTION_LABELS = {
    "short_term": "短期记忆",
    "midterm_sessions": "中期 Sessions",
    "midterm_pages": "中期 Pages",
    "fine_grained_longterm": "细粒度长期记忆",
    "promoted_longterm": "跨 Session 长期记忆",
    "profile": "用户画像",
}
_SECTIONS = tuple(
    [(_SECTION_LABELS[section], section) for section in MEMORY_STATE_SECTIONS if section in _SECTION_LABELS]
    + [("Jobs", "jobs")]
)


def render(
    st,
    snapshot: dict | None,
    step_run: dict | None = None,
    *,
    key_prefix: str,
    state_loader: Callable[[Iterable[str]], dict] | None = None,
) -> None:
    labels = [label for label, _section in _SECTIONS]
    selected = st.segmented_control(
        "数据库分区",
        labels,
        default=labels[0],
        key=f"{key_prefix}:section",
        label_visibility="collapsed",
    )
    section = dict(_SECTIONS).get(selected or labels[0], "short_term")
    requested_sections = (
        {"migration_jobs", "longterm_extraction_jobs", "profile_jobs", "promotion_jobs"}
        if section == "jobs"
        else {section}
    )
    current_state = state_loader(requested_sections) if state_loader is not None else snapshot or {}
    if section == "jobs":
        st.caption("Core/Production Jobs 状态来自真实后台表；Demo 调度队列只负责手动触发。")
        st.markdown("#### Migration")
        render_records(
            st,
            current_state.get("jobs", {}).get("migration", []),
            key_prefix=f"{key_prefix}:migration_jobs",
        )
        st.markdown("#### Fine-grained LongTerm Extraction")
        render_records(
            st,
            current_state.get("jobs", {}).get("longterm_extraction", []),
            key_prefix=f"{key_prefix}:longterm_extraction_jobs",
        )
        st.markdown("#### Profile")
        render_records(
            st,
            current_state.get("jobs", {}).get("profile", []),
            key_prefix=f"{key_prefix}:profile_jobs",
        )
        st.markdown("#### Promotion")
        render_records(
            st,
            current_state.get("jobs", {}).get("promotion", []),
            key_prefix=f"{key_prefix}:promotion_jobs",
        )
        if step_run:
            diff = step_run.get("diff") or {}
            _render_diff(
                st,
                "Migration 本步骤变化",
                diff.get("migration_jobs"),
                key=f"{key_prefix}:migration_diff",
            )
            _render_diff(
                st,
                "Profile 本步骤变化",
                diff.get("profile_jobs"),
                key=f"{key_prefix}:profile_diff",
            )
            _render_diff(
                st,
                "Fine-grained LongTerm 本步骤变化",
                diff.get("longterm_extraction_jobs"),
                key=f"{key_prefix}:longterm_extraction_diff",
            )
            _render_diff(
                st,
                "Promotion 本步骤变化",
                diff.get("promotion_jobs"),
                key=f"{key_prefix}:promotion_diff",
            )
        return

    records = current_state.get(section, [])
    render_records(st, records, key_prefix=f"{key_prefix}:{section}")
    if section == "midterm_sessions":
        _render_memory_evolution(st, records, key_prefix=f"{key_prefix}:evolution")
    _render_step_changes(
        st,
        step_run,
        section,
        key_prefix=f"{key_prefix}:{section}",
    )


def _render_step_changes(
    st,
    step_run: dict | None,
    section: str,
    *,
    key_prefix: str,
) -> None:
    if not step_run or not section:
        return
    _render_diff(
        st,
        "本步骤变化",
        (step_run.get("diff") or {}).get(section),
        key=f"{key_prefix}:step_diff",
    )


def _render_diff(st, title: str, diff: dict | None, *, key: str) -> None:
    if not diff:
        return
    st.markdown(f"#### {title}")
    metrics = st.columns(4)
    metrics[0].metric("当前数量", diff.get("after_count", 0))
    metrics[1].metric("新增", len(diff.get("added") or []))
    metrics[2].metric("更新", len(diff.get("updated") or []))
    metrics[3].metric("删除", len(diff.get("deleted") or []))
    for label, diff_key in (("新增记录", "added"), ("更新记录", "updated"), ("删除记录", "deleted")):
        records = diff.get(diff_key) or []
        if records:
            with st.expander(label, key=f"{key}:{diff_key}"):
                render_table(st, records, key_prefix=f"{key}:{diff_key}")


def _render_memory_evolution(st, records: list[dict], *, key_prefix: str) -> None:
    """Show production MidTerm heat/recall fields without reimplementing decay."""
    if not records:
        return
    st.markdown("#### 记忆演化")
    evolution = []
    for row in records:
        payload = row.get("payload") or {}
        evolution.append(
            {
                "Session": row.get("id"),
                "N_visit": payload.get("N_visit"),
                "L_interaction": payload.get("L_interaction"),
                "R_recency": payload.get("R_recency"),
                "H_segment": payload.get("H_segment"),
                "valid_recall_count": payload.get("valid_recall_count"),
                "last_recall_at": payload.get("last_recall_at"),
                "page_ids": payload.get("page_ids"),
                "promotion_threshold": payload.get("promotion_threshold"),
                "promotion_eligible": payload.get("promotion_eligible"),
                "promotion_job_id": payload.get("promotion_job_id"),
                "promotion_job_status": payload.get("promotion_job_status"),
                "promoted_longterm_id": payload.get("promoted_longterm_id"),
            }
        )
    render_table(st, evolution, key_prefix=key_prefix)
    st.caption("Heat、forgetting 和 promotion eligibility 均来自生产 MidTerm 字段与配置。")
