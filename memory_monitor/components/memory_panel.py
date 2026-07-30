from __future__ import annotations

from memory_monitor.components.common import render_records


def render(
    st,
    snapshot: dict,
    step_run: dict | None = None,
    *,
    key_prefix: str,
) -> None:
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
    section_names = ("short_term", "midterm_sessions", "midterm_pages", "long_term", "profile")
    for tab, records, section_name in zip(tabs[:5], sections, section_names):
        with tab:
            render_records(st, records, key_prefix=f"{key_prefix}:{section_name}")
            _render_step_changes(
                st,
                step_run,
                _section_for_records(records, snapshot),
                key_prefix=f"{key_prefix}:{section_name}",
            )
    with tabs[5]:
        st.markdown("#### Migration")
        render_records(
            st,
            snapshot.get("jobs", {}).get("migration", []),
            key_prefix=f"{key_prefix}:migration_jobs",
        )
        st.markdown("#### Profile")
        render_records(
            st,
            snapshot.get("jobs", {}).get("profile", []),
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


def _section_for_records(records: list[dict], snapshot: dict) -> str:
    mapping = (
        ("short_term", snapshot.get("short_term", [])),
        ("midterm_sessions", snapshot.get("midterm_sessions", [])),
        ("midterm_pages", snapshot.get("midterm_pages", [])),
        ("long_term", snapshot.get("long_term", [])),
        ("profile", snapshot.get("profile", [])),
    )
    return next((name for name, candidate in mapping if records is candidate), "")


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
                st.dataframe(records, width="stretch", hide_index=True)
