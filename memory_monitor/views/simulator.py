from __future__ import annotations

import json

from memory_monitor.components.common import render_json


def render(st, simulation_service) -> None:
    st.header("Sandbox simulator")
    simulation_id = st.text_input("simulation_id", value=st.session_state.get("simulation_id", ""))
    if st.button("Create isolated sandbox"):
        environment = simulation_service.create_environment(simulation_id or None)
        st.session_state["simulation_id"] = environment.simulation_id
        st.success(f"Created {environment.simulation_id} at {environment.root}")
        simulation_id = environment.simulation_id
    if not simulation_id:
        st.info("Create a sandbox before submitting conversations.")
        return

    user_id, run_id = st.columns(2)
    user = user_id.text_input("user_id", value="sim-user")
    run = run_id.text_input("run_id", value="sim-run")
    user_message = st.text_area("User message")
    assistant_message = st.text_area("Assistant message")
    capacity = st.number_input("Short-term capacity", min_value=0, value=4, step=2)
    enabled = st.columns(3)
    midterm = enabled[0].checkbox("Mid-term", value=True)
    longterm = enabled[1].checkbox("Long-term", value=True)
    profile = enabled[2].checkbox("Profile", value=True)

    if st.button("Submit one turn", disabled=not (user_message and assistant_message)):
        result = simulation_service.submit_turn(
            simulation_id,
            user_id=user,
            run_id=run,
            user_message=user_message,
            assistant_message=assistant_message,
            short_term_capacity=int(capacity),
            midterm_enabled=midterm,
            longterm_enabled=longterm,
            profile_enabled=profile,
        )
        render_json(st, result, label="Submission result", expanded=True)

    batch_json = st.text_area(
        "Batch JSON",
        value='[{"user_message": "hello", "assistant_message": "hi"}]',
        help="Each item may omit user_id/run_id; current values will be used.",
    )
    if st.button("Submit batch"):
        turns = json.loads(batch_json)
        normalized = [
            {
                "user_id": item.get("user_id", user),
                "run_id": item.get("run_id", run),
                "user_message": item["user_message"],
                "assistant_message": item["assistant_message"],
            }
            for item in turns
        ]
        render_json(
            st,
            simulation_service.submit_batch(
                simulation_id,
                normalized,
                short_term_capacity=int(capacity),
                midterm_enabled=midterm,
                longterm_enabled=longterm,
                profile_enabled=profile,
            ),
            label="Batch result",
            expanded=True,
        )

    st.subheader("Manual worker")
    job_id, job_type = st.columns(2)
    selected_job = job_id.text_input("job_id")
    selected_type = job_type.selectbox("job_type", ["migration", "profile"])
    actions = st.columns(4)
    if actions[0].button("Next migration"):
        before = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        processed = simulation_service.process_next_migration_job(simulation_id)
        after = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        render_json(
            st,
            {
                "processed": processed,
                "before": before,
                "after": after,
                "changes": simulation_service.compare_snapshots(before, after),
            },
            label="Migration execution",
            expanded=True,
        )
    if actions[1].button("Next profile"):
        before = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        processed = simulation_service.process_next_profile_job(simulation_id)
        after = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        render_json(
            st,
            {
                "processed": processed,
                "before": before,
                "after": after,
                "changes": simulation_service.compare_snapshots(before, after),
            },
            label="Profile execution",
            expanded=True,
        )
    if actions[2].button("Selected", disabled=not selected_job):
        before = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        processed = simulation_service.process_job(simulation_id, selected_job, selected_type)
        after = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        render_json(
            st,
            {
                "processed": processed,
                "before": before,
                "after": after,
                "changes": simulation_service.compare_snapshots(before, after),
            },
            label="Selected job execution",
            expanded=True,
        )
    if actions[3].button("All pending"):
        before = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        processed = simulation_service.process_all_pending(simulation_id)
        after = simulation_service.snapshot(simulation_id, user_id=user, run_id=run)
        render_json(
            st,
            {
                "processed": processed,
                "before": before,
                "after": after,
                "changes": simulation_service.compare_snapshots(before, after),
            },
            label="All pending execution",
            expanded=True,
        )

    render_json(
        st,
        simulation_service.snapshot(simulation_id, user_id=user, run_id=run),
        label="Current snapshot",
    )
    confirm = st.checkbox("I confirm deletion of this sandbox")
    if st.button("Delete sandbox", disabled=not confirm):
        simulation_service.clear_environment(simulation_id)
        st.session_state.pop("simulation_id", None)
        st.success("Sandbox deleted")
