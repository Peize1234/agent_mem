from __future__ import annotations


def render_history(st, messages: list[dict]) -> None:
    st.subheader("完整原始对话")
    if not messages:
        st.caption("尚无对话。输入第一条消息开始演示。")
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])


def chat_input(st, *, disabled: bool = False):
    return st.chat_input("输入用户问题", disabled=disabled)
