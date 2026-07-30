from __future__ import annotations

CHAT_HISTORY_HEIGHT = 650


def render_history(
    st,
    messages: list[dict],
    *,
    simulation_id: str,
    height: int = CHAT_HISTORY_HEIGHT,
) -> None:
    st.subheader("完整原始对话")
    with st.container(
        height=height,
        border=True,
        key=f"chat_history_{simulation_id}",
        autoscroll=False,
    ):
        if not messages:
            st.caption("尚无对话。输入第一条消息开始演示。")
        for message in messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])


def chat_input(
    st,
    *,
    key: str,
    disabled: bool = False,
):
    return st.chat_input("输入用户问题", key=key, disabled=disabled)
