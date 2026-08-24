from __future__ import annotations

from memory_monitor.components import pipeline_graph
from memory_monitor.models import BackgroundStepConfig, PipelineStep, StepStatus
from memory_monitor.services.demo_repository import StepAlreadyRunningError

_PRIMARY_ACTIONS = (
    ("next", "下一步"),
    ("answer", "运行到模型回答"),
    ("memory", "运行记忆阶段"),
    ("all", "运行全部"),
)
_SECONDARY_ACTIONS = (
    ("remaining", "运行剩余步骤"),
    ("retry", "重试失败步骤"),
    ("reset", "重置当前轮"),
)
_MEMORY_GATE_CONTROLS = (
    (PipelineStep.RUN_SHORTTERM, "提交本轮记忆", "提交"),
    (PipelineStep.RUN_MIDTERM, "添加中期记忆", "中期"),
    (PipelineStep.RUN_LONGTERM, "执行细粒度长期记忆", "细粒度长期"),
    (PipelineStep.RUN_PROFILE, "抽取用户画像", "用户画像"),
)

_STEP_LABELS = {
    PipelineStep.CAPTURE_INPUT.value: "捕获输入",
    PipelineStep.RETRIEVE_CONTEXT.value: "分层检索",
    PipelineStep.AGENTIC_RETRIEVAL.value: "Agentic 检索",
    PipelineStep.BUILD_PROMPT.value: "构建 Prompt",
    PipelineStep.GENERATE_RESPONSE.value: "模型回答",
    PipelineStep.RUN_SHORTTERM.value: "提交本轮记忆",
    PipelineStep.RUN_MIDTERM.value: "添加中期记忆",
    PipelineStep.RUN_LONGTERM.value: "执行细粒度长期记忆",
    PipelineStep.RUN_PROFILE.value: "抽取用户画像",
}


def render_controls(
    st,
    *,
    key_prefix: str,
    disabled: bool,
    steps: list[dict] | None = None,
) -> tuple[str | None, str | None]:
    caption = getattr(st, "caption", None)
    if callable(caption):
        caption("Demo 调度队列：提交操作后由独立 worker 异步推进；下方 Core Jobs 反映生产状态。")
    step_runs = steps or []
    failed = failed_steps(step_runs)
    target = None
    if failed:
        target = st.selectbox(
            "失败步骤",
            [step["step"] for step in failed],
            format_func=lambda value: _STEP_LABELS.get(value, value),
            key=f"{key_prefix}:retry_target",
        )

    selected = _render_action_row(
        st,
        _PRIMARY_ACTIONS,
        key_prefix=key_prefix,
        disabled=disabled,
        failed=failed,
    )
    secondary = _render_action_row(
        st,
        _SECONDARY_ACTIONS,
        key_prefix=key_prefix,
        disabled=disabled,
        failed=failed,
    )
    return selected or secondary, target


def _render_action_row(
    st,
    actions,
    *,
    key_prefix: str,
    disabled: bool,
    failed: list[dict],
) -> str | None:
    columns = st.columns(len(actions))
    selected = None
    for column, (action, label) in zip(columns, actions):
        action_disabled = disabled or (action == "retry" and not failed)
        if column.button(
            label,
            key=f"{key_prefix}:action:{action}",
            disabled=action_disabled,
            width="stretch",
        ):
            selected = action
    return selected


def render_memory_gates(
    st,
    pipeline,
    repository,
    *,
    simulation_id: str,
    session_id: str,
    turn_id: str | None,
    steps: list[dict] | None = None,
) -> None:
    """Render one persisted hold toggle for each selected-turn memory step."""
    st.caption("本轮记忆步骤开关")
    if turn_id is None:
        st.caption("创建轮次后可独立设置四个记忆步骤是否允许执行。")
        return

    if steps is None:
        repository.assert_turn_belongs_to_session(turn_id, session_id)
        steps = repository.list_steps(turn_id)
    memory_steps = _memory_step_map(steps)
    key_prefix = f"memory_gate:{simulation_id}:{turn_id}"
    fingerprint = _gate_fingerprint(memory_steps)
    marker_key = f"{key_prefix}:persisted"
    if st.session_state.get(marker_key) != fingerprint:
        _store_gate_widget_state(st.session_state, key_prefix, memory_steps)

    columns = st.columns(4)
    for column, (step, label, _summary_label) in zip(columns, _MEMORY_GATE_CONTROLS):
        current = memory_steps[step]
        status = current["status"]
        column.toggle(
            label,
            key=f"{key_prefix}:{step.value}",
            disabled=status != StepStatus.PENDING.value,
            help=(
                "该开关只决定本步骤是否允许执行，不会单独启动流程。请通过“下一步”“运行记忆阶段”"
                "或“运行全部”启动；如果已启动流程因该步骤关闭而阻塞，重新开启后会恢复执行。"
            ),
            on_change=_apply_memory_gate,
            args=(
                st.session_state,
                pipeline,
                repository,
                simulation_id,
                session_id,
                turn_id,
                step.value,
            ),
        )

    summary = " · ".join(
        f"{label}{'已关闭' if memory_steps[step].get('is_held') else '可执行'}"
        for step, _widget_label, label in _MEMORY_GATE_CONTROLS
    )
    st.caption(summary)
    notice_key = f"{key_prefix}:gate_notice"
    if notice := st.session_state.pop(notice_key, None):
        st.caption(notice)


def _apply_memory_gate(
    session_state,
    pipeline,
    repository,
    simulation_id: str,
    session_id: str,
    turn_id: str,
    step_name: str,
) -> None:
    step = PipelineStep(step_name)
    key_prefix = f"memory_gate:{simulation_id}:{turn_id}"
    key = f"{key_prefix}:{step.value}"
    desired_runnable = bool(session_state[key])

    try:
        result = pipeline.set_memory_step_runnable(
            turn_id,
            step,
            desired_runnable,
            session_id=session_id,
        )
        if desired_runnable:
            notice = (
                "已解除阻塞，正在恢复已启动的流程。"
                if result["resumed"]
                else "已开启，等待点击“下一步”或其他运行按钮。"
            )
        else:
            notice = "已关闭，该步骤将在运行流程时保持 pending。"
        session_state[f"{key_prefix}:gate_notice"] = notice
    finally:
        _store_gate_widget_state(
            session_state,
            key_prefix,
            _memory_step_map(repository.list_steps(turn_id)),
        )


def _memory_step_map(steps: list[dict]) -> dict[PipelineStep, dict]:
    return {
        PipelineStep(step["step"]): step
        for step in steps
        if PipelineStep(step["step"]) in {item[0] for item in _MEMORY_GATE_CONTROLS}
    }


def _gate_fingerprint(steps: dict[PipelineStep, dict]) -> tuple[tuple[str, str, bool], ...]:
    return tuple(
        (step.value, steps[step]["status"], bool(steps[step].get("is_held")))
        for step, _widget_label, _summary_label in _MEMORY_GATE_CONTROLS
    )


def _store_gate_widget_state(session_state, key_prefix: str, steps: dict[PipelineStep, dict]) -> None:
    for step, _widget_label, _summary_label in _MEMORY_GATE_CONTROLS:
        current = steps[step]
        session_state[f"{key_prefix}:{step.value}"] = (
            not bool(current.get("is_held"))
            if current["status"] in {StepStatus.PENDING.value, StepStatus.FAILED.value}
            else True
        )
    session_state[f"{key_prefix}:persisted"] = _gate_fingerprint(steps)


def apply_action(
    st,
    pipeline,
    repository,
    turn_id: str,
    session_id: str,
    action: str,
    *,
    target: str | None = None,
) -> None:
    try:
        if action == "next":
            result = pipeline.run_next_step(turn_id, session_id=session_id)
        elif action == "answer":
            result = pipeline.run_to_answer(turn_id, session_id=session_id)
        elif action == "memory":
            result = pipeline.run_memory_stage(turn_id, session_id=session_id)
        elif action == "all":
            result = pipeline.run_all(turn_id, session_id=session_id)
        elif action == "remaining":
            result = pipeline.run_remaining(turn_id, session_id=session_id)
        elif action == "retry":
            if target is None:
                st.info("当前没有失败步骤可重试。")
                return
            result = pipeline.retry_step(turn_id, target, session_id=session_id)
        elif action == "reset":
            result = pipeline.reset_turn(turn_id, session_id=session_id)
        else:
            raise ValueError(f"Unknown Demo action: {action}")

        if isinstance(result, dict) and result.get("blocked"):
            if result["blocked"] == "running":
                st.caption("本轮已有步骤处于 queued/running，状态区域会自动更新。")
            else:
                st.warning("存在失败步骤，请选择失败节点后单独重试。")
            return
        if action == "reset":
            st.caption("当前轮已安全重置。")
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
    return [step for step in steps if step["status"] == StepStatus.FAILED.value]


def progress(
    steps: list[dict],
    background_config: BackgroundStepConfig | dict | None = None,
) -> float:
    return pipeline_graph.progress(steps, background_config)[2]
