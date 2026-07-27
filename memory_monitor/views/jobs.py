from __future__ import annotations

from memory_monitor.components.common import filter_values, render_json


def render(st, service) -> None:
    st.header("Background queues")
    job_type = st.radio("Queue", ["migration", "profile"], horizontal=True)
    columns = st.columns(5)
    status = columns[0].text_input("status")
    user_id = columns[1].text_input("user_id")
    run_id = columns[2].text_input("run_id")
    trace_id = columns[3].text_input("trace_id")
    job_id = columns[4].text_input("job_id")
    filters = filter_values(status=status, user_id=user_id, run_id=run_id, trace_id=trace_id, job_id=job_id)
    paging = st.columns(2)
    jobs = service.list_jobs(
        job_type,
        filters=filters,
        page=int(paging[0].number_input("Page", min_value=1, value=1, key=f"{job_type}_job_page")),
        page_size=int(
            paging[1].selectbox("Page size", [20, 50, 100, 200], index=1, key=f"{job_type}_job_page_size")
        ),
    )
    st.caption(f"{jobs['total']} jobs")
    st.dataframe(jobs["items"], use_container_width=True, hide_index=True)

    selected = st.text_input("Selected job_id", value=job_id)
    if selected:
        try:
            render_json(st, service.job_detail(selected, job_type), label="Job detail", expanded=True)
        except KeyError as exc:
            st.warning(str(exc))

    if not service.allow_writes:
        st.info("Read-only mode: retry and execution controls are disabled.")
        return
    confirmed = st.checkbox("I confirm this action may mutate task and memory state")
    retry, execute, next_job = st.columns(3)
    if retry.button("Retry dead job", disabled=not (confirmed and selected)):
        st.success(f"Retried: {service.retry_job(selected, job_type)}")
    if execute.button("Execute selected", disabled=not (confirmed and selected)):
        st.success(f"Executed: {service.process_job(selected, job_type)}")
    if next_job.button("Execute next", disabled=not confirmed):
        st.success(f"Executed: {service.process_next_job(job_type)}")
