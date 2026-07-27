from __future__ import annotations


def render(st, service) -> None:
    st.header("Memory Dashboard")
    data = service.dashboard()
    migration = data["migration"]
    profile = data["profile"]
    columns = st.columns(6)
    columns[0].metric("Migration success", f"{migration['success_rate']:.1%}")
    columns[1].metric("Profile success", f"{profile['success_rate']:.1%}")
    columns[2].metric("Retry rate", f"{migration['retry_rate']:.1%}")
    columns[3].metric("Degraded rate", f"{migration['degraded_rate']:.1%}")
    columns[4].metric("Dead jobs", migration["dead"] + profile["dead"])
    columns[5].metric(
        "Oldest pending",
        f"{max(migration['oldest_pending_wait_seconds'], profile['oldest_pending_wait_seconds']):.0f}s",
    )

    worker = data["worker"]
    st.info(f"Worker mode: {worker.get('mode')} · alive: {worker.get('alive')} · attached: {worker.get('available')}")
    left, right = st.columns(2)
    left.subheader("Migration queue")
    left.bar_chart(data["migration_statuses"])
    right.subheader("Profile queue")
    right.bar_chart(data["profile_statuses"])
    st.subheader("Average observed duration")
    st.dataframe(
        [{"stage": stage, "average_ms": round(duration, 2)} for stage, duration in data["average_duration_ms"].items()],
        use_container_width=True,
        hide_index=True,
    )
