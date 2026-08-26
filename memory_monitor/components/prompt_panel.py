from __future__ import annotations

from memory_monitor.components.common import render_json


def render_prompt(st, prompt_output: dict | None) -> None:
    if not prompt_output:
        st.caption("执行“构造 Prompt”后显示实际模型输入。")
        return
    st.markdown("### 本轮回答要求")
    custom_prompt = prompt_output.get("custom_prompt")
    if custom_prompt:
        st.code(custom_prompt, language="text")
    else:
        st.caption("本轮未设置额外回答要求")
    st.markdown("### 实际发送给模型的 Prompt / messages")
    st.caption(f"Context hash: `{prompt_output.get('context_hash', '')}`")
    for index, message in enumerate(prompt_output.get("messages") or []):
        st.markdown(f"**{index + 1}. {message.get('role', 'message')}**")
        st.code(message.get("content", ""), language="text")


def render_generation(st, generation_output: dict | None, *, key_prefix: str) -> None:
    if not generation_output:
        st.caption("执行“模型回答”后显示调用结果。")
        return
    st.markdown("#### 助手回答")
    st.markdown(generation_output.get("assistant_message", ""))
    render_json(
        st,
        generation_output.get("raw_response"),
        label="模型原始返回",
        key=f"{key_prefix}:raw_response",
    )
