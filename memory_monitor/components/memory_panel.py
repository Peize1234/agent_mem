from __future__ import annotations

from memory_monitor.components.common import render_records


def render(st, snapshot: dict, step_run: dict | None = None) -> None:
    tabs = st.tabs(
        [
            "短期记忆",
            "中期 Sessions",
            "中期 Pages",
            "长期记忆",
            "用户画像",
            "Jobs",
        ]
    )
    sections = (
        snapshot.get("short_term", []),
        snapshot.get("midterm_sessions", []),
        snapshot.get("midterm_pages", []),
        snapshot.get("long_term", []),
        snapshot.get("profile", []),
    )
    for tab, records in zip(tabs[:5], sections):
        with tab:
            render_records(st, records)
            _render_step_changes(st, step_run, _section_for_records(records, snapshot))
    with tabs[5]:
        st.markdown("#### Migration")
        render_records(st, snapshot.get("jobs", {}).get("migration", []))
        st.markdown("#### Profile")
        render_records(st, snapshot.get("jobs", {}).get("profile", []))
        if step_run:
            diff = step_run.get("diff") or {}
            _render_diff(st, "Migration 本步骤变化", diff.get("migration_jobs"))
            _render_diff(st, "Profile 本步骤变化", diff.get("profile_jobs"))


def _section_for_records(records: list[dict], snapshot: dict) -> str:
    mapping = (
        ("short_term", snapshot.get("short_term", [])),
        ("midterm_sessions", snapshot.get("midterm_sessions", [])),
        ("midterm_pages", snapshot.get("midterm_pages", [])),
        ("long_term", snapshot.get("long_term", [])),
        ("profile", snapshot.get("profile", [])),
    )
    return next((name for name, candidate in mapping if records is candidate), "")


def _render_step_changes(st, step_run: dict | None, section: str) -> None:
    if not step_run or not section:
        return
    _render_diff(st, "本步骤变化", (step_run.get("diff") or {}).get(section))


def _render_diff(st, title: str, diff: dict | None) -> None:
    if not diff:
        return
    st.markdown(f"#### {title}")
    metrics = st.columns(4)
    metrics[0].metric("当前数量", diff.get("after_count", 0))
    metrics[1].metric("新增", len(diff.get("added") or []))
    metrics[2].metric("更新", len(diff.get("updated") or []))
    metrics[3].metric("删除", len(diff.get("deleted") or []))
    for label, key in (("新增记录", "added"), ("更新记录", "updated"), ("删除记录", "deleted")):
        records = diff.get(key) or []
        if records:
            with st.expander(label):
                st.dataframe(records, use_container_width=True, hide_index=True)
