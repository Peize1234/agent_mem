from __future__ import annotations

from memory_monitor.components.common import render_json


def render(st, service) -> None:
    st.header("Trace timeline")
    user_id, run_id = st.columns(2)
    user_filter = user_id.text_input("user_id", key="trace_user")
    run_filter = run_id.text_input("run_id", key="trace_run")
    page = st.number_input("Page", min_value=1, value=1, key="trace_page")
    traces = service.list_traces(page=int(page), user_id=user_filter or None, run_id=run_filter or None)
    st.caption(f"{traces['total']} traces")
    st.dataframe(traces["items"], use_container_width=True, hide_index=True)

    trace_id = st.text_input("Open trace_id")
    if not trace_id:
        return
    detail = service.trace(trace_id)
    st.subheader("Events")
    for event in detail["events"]:
        title = f"{event.get('created_at')} · {event.get('event_type')} · {event.get('status')}"
        render_json(st, event, label=title)
    render_json(st, detail["messages"], label="Short-term messages")
    render_json(st, detail["migration_jobs"], label="Migration jobs")
    render_json(st, detail["profile_jobs"], label="Profile jobs")
    render_json(st, detail["vectors"], label="Vector records")
