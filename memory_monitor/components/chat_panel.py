from __future__ import annotations

CHAT_HISTORY_HEIGHT = 700


def render_history(
    st,
    messages: list[dict],
    *,
    simulation_id: str,
    session_id: str,
    height: int = CHAT_HISTORY_HEIGHT,
) -> None:
    st.subheader("完整原始对话")
    with st.container(
        height=height,
        border=True,
        key=f"chat_history_{simulation_id}_{session_id}",
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


def custom_prompt_input(
    st,
    *,
    key: str,
    disabled: bool = False,
):
    with st.expander("回答自定义要求（可选）", expanded=False):
        st.caption("控制本轮回答的格式、侧重点和表达方式，不影响记忆检索。")
        return st.text_area(
            "回答自定义要求",
            placeholder="例如：请按重要性排序，并用表格展示结论与主要风险。",
            key=key,
            disabled=disabled,
            label_visibility="collapsed",
        )
