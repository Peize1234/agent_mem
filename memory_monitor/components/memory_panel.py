from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from memory_monitor.components.common import render_json, render_records, render_table
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
    if section == "midterm_sessions":
        _render_memory_evolution(st, records, key_prefix=f"{key_prefix}:evolution")
    elif section == "midterm_pages":
        _render_midterm_pages(st, records, key_prefix=f"{key_prefix}:forgetting")
    else:
        render_records(st, records, key_prefix=f"{key_prefix}:{section}")
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
    """Show the current production-derived Session state above engineering fields."""
    if not records:
        st.caption("暂无记录")
        return
    st.markdown("#### 记忆演化")
    selected = st.selectbox(
        "查看 Session 当前状态",
        range(len(records)),
        format_func=lambda index: str(records[index].get("id") or index),
        key=f"{key_prefix}:selector",
    )
    state = records[selected].get("monitor_state") or {}
    metrics = st.columns(4)
    metrics[0].metric("当前热度", _format_number(state.get("current_H_segment")))
    metrics[1].metric("当前新鲜度", _format_number(state.get("current_R_recency")))
    metrics[2].metric("有效召回次数", _format_count(state.get("valid_recall_count")))
    metrics[3].metric("满足 Promotion", _format_eligibility(state.get("current_promotion_eligible")))
    st.caption("当前值按最新 Turn 只读计算；Promotion 生产判定仍可在下表中与数据库保存值分别查看。")
    render_table(st, midterm_session_evolution_rows(records), key_prefix=key_prefix)
    _render_record_details(
        st,
        records,
        selected,
        label="完整 Session 工程字段（payload 原始字段 + monitor_state）",
        key_prefix=f"{key_prefix}:details",
    )


def midterm_session_evolution_rows(records: list[dict]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        payload = record.get("payload") or {}
        state = record.get("monitor_state") or {}
        threshold = state.get("promotion_threshold") or {}
        rows.append(
            {
                "Session": record.get("id"),
                "当前 Turn": state.get("current_turn_index"),
                "保存 R_recency": state.get("stored_R_recency"),
                "当前 R_recency": state.get("current_R_recency"),
                "保存 H_segment": state.get("stored_H_segment"),
                "当前 H_segment": state.get("current_H_segment"),
                "heat_factor": state.get("heat_factor"),
                "N_visit": payload.get("N_visit"),
                "L_interaction": payload.get("L_interaction"),
                "valid_recall_count": state.get("valid_recall_count"),
                "last_visit_turn_index": payload.get("last_visit_turn_index"),
                "last_recall_at": payload.get("last_recall_at"),
                "Promotion 召回阈值": threshold.get("min_valid_recall_count"),
                "Promotion 热度阈值": threshold.get("heat_threshold"),
                "生产保存值达标": state.get("stored_promotion_eligible", state.get("promotion_eligible")),
                "当前观测值达标": state.get("current_promotion_eligible"),
                "promotion_job_id": state.get("promotion_job_id"),
                "promotion_job_status": state.get("promotion_job_status"),
                "promoted_longterm_id": state.get("promoted_longterm_id"),
                "page_ids": payload.get("page_ids"),
            }
        )
    return rows


def _render_midterm_pages(st, records: list[dict], *, key_prefix: str) -> None:
    if not records:
        st.caption("暂无记录")
        return
    st.markdown("#### Page 当前遗忘状态")
    render_table(st, midterm_page_forgetting_rows(records), key_prefix=key_prefix)
    _render_record_details(
        st,
        records,
        0,
        label="完整 Page 工程字段（payload 原始字段 + monitor_state）",
        key_prefix=f"{key_prefix}:details",
    )
    st.caption("从未发生有效召回时，“距上次有效召回”为不适用；遗忘仍以 Page 创建 Turn 为生产锚点。")


def midterm_page_forgetting_rows(records: list[dict]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        payload = record.get("payload") or {}
        state = record.get("monitor_state") or {}
        rows.append(
            {
                "Page": record.get("id"),
                "所属 Session": state.get("session_id", payload.get("session_id")),
                "当前 Turn": state.get("current_turn_index"),
                "距上次有效召回 (Turn)": _format_recall_distance(
                    state.get("turns_since_last_valid_recall"),
                    state.get("last_recall_turn_index"),
                ),
                "遗忘锚点距离 (Turn)": state.get("turns_since_decay_anchor"),
                "有效召回次数": state.get("valid_recall_count", payload.get("valid_recall_count")),
                "当前 Session 热度": state.get("current_H_segment"),
                "当前 heat factor": state.get("heat_factor"),
                "基础半衰期 (Turn)": state.get("base_half_life_turns"),
                "当前有效半衰期 (Turn)": state.get("effective_half_life_turns"),
                "当前 retention / forgetting factor": state.get("retention"),
                "turn_index": state.get("turn_index", payload.get("turn_index")),
                "last_recall_turn_index": state.get(
                    "last_recall_turn_index",
                    payload.get("last_recall_turn_index"),
                ),
            }
        )
    return rows


def _render_record_details(
    st,
    records: list[dict],
    default_index: int,
    *,
    label: str,
    key_prefix: str,
) -> None:
    selected = st.selectbox(
        "查看完整记录",
        range(len(records)),
        index=default_index,
        format_func=lambda index: str(records[index].get("id") or index),
        key=f"{key_prefix}:selector",
    )
    render_json(
        st,
        records[selected],
        label=label,
        expanded=False,
        key=f"{key_prefix}:json",
    )


def _format_number(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "不适用"


def _format_count(value: Any) -> str:
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return "不适用"


def _format_eligibility(value: Any) -> str:
    if value is None:
        return "不适用"
    return "是" if value else "否"


def _format_recall_distance(distance: Any, last_recall_turn_index: Any) -> Any:
    if last_recall_turn_index is None:
        return "不适用（从未召回）"
    return distance
