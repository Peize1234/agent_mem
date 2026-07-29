from __future__ import annotations

from memory_monitor.components import pipeline_graph
from memory_monitor.models import BackgroundStepConfig, PipelineStep
from memory_monitor.services.demo_repository import StepAlreadyRunningError

_ACTIONS = (
    ("next", "下一步"),
    ("answer", "运行到模型回答"),
    ("commit", "运行到提交"),
    ("all", "运行全部"),
    ("retry", "重试"),
    ("reset", "重置当前轮"),
)

_STEP_LABELS = {
    PipelineStep.CAPTURE_INPUT.value: "捕获输入",
    PipelineStep.RETRIEVE_CONTEXT.value: "检索上下文",
    PipelineStep.BUILD_PROMPT.value: "构建 Prompt",
    PipelineStep.GENERATE_RESPONSE.value: "模型回答",
    PipelineStep.COMMIT_TURN.value: "提交当前轮",
    PipelineStep.RUN_MIDTERM.value: "中期记忆",
    PipelineStep.RUN_LONGTERM.value: "长期记忆",
    PipelineStep.RUN_PROFILE.value: "用户画像",
    PipelineStep.REFRESH_STATE.value: "刷新状态",
}


def render_controls(st, *, disabled: bool, steps: list[dict] | None = None) -> tuple[str | None, str | None]:
    failed = failed_steps(steps or [])
    retry_target = None
    if len(failed) > 1:
        retry_target = st.selectbox(
            "重试目标",
            [step["step"] for step in failed],
            format_func=lambda value: _STEP_LABELS.get(value, value),
            key="demo_retry_target",
        )
    elif len(failed) == 1:
        retry_target = failed[0]["step"]

    columns = st.columns(len(_ACTIONS))
    selected = None
    for column, (action, label) in zip(columns, _ACTIONS):
        action_disabled = disabled or (action == "retry" and not failed)
        if column.button(label, disabled=action_disabled, use_container_width=True):
            selected = action
    return selected, retry_target


def apply_action(
    st,
    pipeline,
    repository,
    turn_id: str,
    session_id: str,
    action: str,
    *,
    retry_target: str | None = None,
) -> None:
    try:
        result = None
        if action == "next":
            result = pipeline.run_next_step(turn_id, session_id=session_id)
        elif action == "answer":
            result = pipeline.run_until(turn_id, PipelineStep.GENERATE_RESPONSE, session_id=session_id)
        elif action == "commit":
            result = pipeline.run_until(turn_id, PipelineStep.COMMIT_TURN, session_id=session_id)
        elif action == "all":
            result = pipeline.run_all(turn_id, session_id=session_id)
        elif action == "retry":
            if retry_target is None:
                st.info("当前没有失败步骤可重试。")
                return
            result = pipeline.retry_step(turn_id, retry_target, session_id=session_id)
        elif action == "reset":
            result = pipeline.reset_turn(turn_id, session_id=session_id)

        if isinstance(result, dict) and result.get("blocked"):
            if result["blocked"] in {"running", "background_running"}:
                st.info("本轮后台任务仍在执行；页面会自动更新，期间可以继续创建下一轮。")
            else:
                st.warning("存在失败步骤，请选择失败节点后重试。")
            return
        st.rerun()
    except StepAlreadyRunningError as exc:
        st.warning(str(exc))
    except Exception as exc:
        st.error(str(exc))


def render_steps(
    st,
    steps: list[dict],
    background_config: BackgroundStepConfig | dict | None = None,
) -> None:
    st.markdown(
        pipeline_graph.render_html(steps, background_config),
        unsafe_allow_html=True,
    )


def current_step(
    steps: list[dict],
    background_config: BackgroundStepConfig | dict | None = None,
) -> dict | None:
    nodes = pipeline_graph.build_nodes(steps, background_config)
    current = next((node for node in nodes.values() if node.current), None)
    return next((step for step in steps if current is not None and step["step"] == current.step.value), None)


def failed_steps(steps: list[dict]) -> list[dict]:
    return [step for step in steps if step["status"] == "failed"]


def progress(
    steps: list[dict],
    background_config: BackgroundStepConfig | dict | None = None,
) -> float:
    return pipeline_graph.progress(steps, background_config)[2]
