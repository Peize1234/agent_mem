from __future__ import annotations

from collections.abc import Callable, Iterable

from memory_monitor.components.common import render_records, render_table

_SECTIONS = (
    ("短期记忆", "short_term"),
    ("中期 Sessions", "midterm_sessions"),
    ("中期 Pages", "midterm_pages"),
    ("长期记忆", "long_term"),
    ("用户画像", "profile"),
    ("Jobs", "jobs"),
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
    requested_sections = {"migration_jobs", "profile_jobs"} if section == "jobs" else {section}
    current_state = state_loader(requested_sections) if state_loader is not None else snapshot or {}
    if section == "jobs":
        st.markdown("#### Migration")
        render_records(
            st,
            current_state.get("jobs", {}).get("migration", []),
            key_prefix=f"{key_prefix}:migration_jobs",
        )
        st.markdown("#### Profile")
        render_records(
            st,
            current_state.get("jobs", {}).get("profile", []),
            key_prefix=f"{key_prefix}:profile_jobs",
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
        return

    records = current_state.get(section, [])
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
