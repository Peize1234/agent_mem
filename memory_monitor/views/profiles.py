from __future__ import annotations

from memory_monitor.components.common import render_json


def render(st, service) -> None:
    st.header("User profile")
    user_id = st.text_input("user_id", key="profile_user")
    if not user_id:
        return
    profile = service.profile(user_id)
    st.subheader("Current values")
    st.dataframe(profile["values"], use_container_width=True, hide_index=True)
    render_json(st, profile["attributes"], label="Attribute catalog")
    render_json(st, profile["jobs"], label="Update jobs")
    st.subheader("Update plan and diff")
    for event in profile["events"]:
        if event.get("before") is not None or event.get("after") is not None or event.get("output") is not None:
            render_json(
                st,
                {
                    "event_type": event.get("event_type"),
                    "status": event.get("status"),
                    "before": event.get("before"),
                    "after": event.get("after"),
                    "update_plan": event.get("output"),
                    "error": event.get("error_message"),
                },
                label=f"{event.get('created_at')} · {event.get('event_type')}",
            )
