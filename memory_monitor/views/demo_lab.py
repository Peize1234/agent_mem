from __future__ import annotations

import html
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from memory_monitor.components import (
    chat_panel,
    context_panel,
    memory_panel,
    pipeline_panel,
    prompt_panel,
    trace_panel,
)
from memory_monitor.models import BACKGROUND_STEPS, BackgroundStepConfig, PipelineStep

_SESSION_KEYS = (
    "demo_simulation_id",
    "demo_user_id",
    "demo_run_id",
    "demo_session_id",
    "demo_turn_id",
    "demo_retry_target",
)
_CONFIG_CONTROLS = (
    ("run_midterm", "执行中期记忆"),
    ("run_longterm", "执行长期记忆"),
    ("run_profile", "执行用户画像"),
)


def render(st, simulation_service) -> None:
    environment, session = _restore_environment(st, simulation_service)
    _render_header(st, simulation_service, environment)

    simulation, user, run, create_clicked = _render_scope_controls(st)
    if create_clicked:
        try:
            environment = simulation_service.create_environment(simulation)
            session = simulation_service.create_session(simulation, user_id=user, run_id=run)
            st.session_state.update(
                {
                    "demo_simulation_id": simulation,
                    "demo_user_id": user,
                    "demo_run_id": run,
                    "demo_session_id": session["session_id"],
                }
            )
            st.session_state.pop("demo_turn_id", None)
            st.rerun()
        except Exception as exc:
            st.error(str(exc))
            return

    if environment is None or session is None:
        st.info("先打开一个隔离沙盒。")
        return

    repository = environment.repository
    pipeline = environment.pipeline
    turns = repository.list_turns(session["session_id"])
    turn_id = synchronize_selected_turn_id(st.session_state, turns)
    selected_turn = repository.assert_turn_belongs_to_session(turn_id, session["session_id"]) if turn_id else None
    background_config = _render_background_config(
        st,
        pipeline,
        repository,
        selected_turn,
        session["session_id"],
        environment.simulation_id,
    )
    steps = repository.list_steps(turn_id) if turn_id else []
    action, retry_target = pipeline_panel.render_controls(st, disabled=not bool(turn_id), steps=steps)
    if action and turn_id:
        pipeline_panel.apply_action(
            st,
            pipeline,
            repository,
            turn_id,
            session["session_id"],
            action,
            retry_target=retry_target,
        )

    left, right = st.columns([0.92, 1.62], gap="large")
    with left:
        if turns:
            turn_options = [turn["turn_id"] for turn in reversed(turns)]
            selected_turn_id = st.selectbox(
                "当前轮次",
                turn_options,
                index=turn_options.index(turn_id),
                format_func=lambda value: _turn_label(turns, value),
            )
            if selected_turn_id != turn_id:
                st.session_state["demo_turn_id"] = selected_turn_id
                st.rerun()
        chat_panel.render_history(st, repository.raw_messages(session["session_id"]))
        user_message = chat_panel.chat_input(st)
        if user_message:
            new_turn_config = background_config if not turns else BackgroundStepConfig()
            turn = pipeline.create_turn(
                session["session_id"],
                user_id=session["user_id"],
                run_id=session["run_id"],
                user_message=user_message,
                background_config=new_turn_config,
            )
            st.session_state["demo_turn_id"] = turn["turn_id"]
            st.rerun()

    with right:
        if not turn_id:
            st.info("在左侧输入问题后，可逐步执行当前轮。")
            return
        _render_live_details(
            st,
            environment,
            session["session_id"],
            turn_id,
            background_config,
        )


def _restore_environment(st, simulation_service):
    simulation_id = st.session_state.get("demo_simulation_id")
    user_id = st.session_state.get("demo_user_id")
    run_id = st.session_state.get("demo_run_id")
    if not all((simulation_id, user_id, run_id)):
        return None, None
    try:
        environment = simulation_service.create_environment(simulation_id)
        session = simulation_service.create_session(simulation_id, user_id=user_id, run_id=run_id)
        st.session_state["demo_session_id"] = session["session_id"]
        return environment, session
    except Exception as exc:
        st.error(f"恢复沙盒失败：{exc}")
        return None, None


def _render_header(st, simulation_service, environment) -> None:
    title, sandbox = st.columns([1.45, 1], vertical_alignment="center")
    with title:
        st.markdown('<h1 class="demo-lab-title">Agent Memory · Demo Lab</h1>', unsafe_allow_html=True)
        st.markdown(
            '<p class="demo-lab-subtitle">冻结上下文，逐步观察前台回答与三个独立后台记忆流程。</p>',
            unsafe_allow_html=True,
        )
    with sandbox:
        if environment is None:
            st.caption("尚未打开沙盒")
            return
        summary, manage = st.columns([3.2, 1], vertical_alignment="center")
        with summary:
            path = str(environment.root)
            st.markdown(
                '<div class="demo-sandbox-summary">'
                f'<span class="demo-sandbox-id">沙盒：{html.escape(environment.simulation_id)}</span>'
                f'<span class="demo-sandbox-path" title="{html.escape(path)}">路径：{html.escape(path)}</span>'
                "</div>",
                unsafe_allow_html=True,
            )
        with manage:
            with st.popover("沙盒管理", use_container_width=True):
                st.caption(f"simulation_id：`{environment.simulation_id}`")
                st.code(str(environment.root), language="text")
                confirm_key = f"demo_delete_confirm:{environment.simulation_id}"
                confirm = st.checkbox("确认删除当前沙盒", key=confirm_key)
                if st.button(
                    "删除沙盒",
                    disabled=not confirm,
                    type="primary",
                    use_container_width=True,
                    key=f"demo_delete:{environment.simulation_id}",
                ):
                    try:
                        with st.spinner("正在安全停止后台队列并删除沙盒…"):
                            simulation_service.clear_environment(environment.simulation_id)
                    except Exception as exc:
                        st.error(f"删除失败，沙盒保持可恢复：{exc}")
                        return
                    _clear_sandbox_session_state(st.session_state, environment.simulation_id)
                    st.rerun()


def _render_scope_controls(st):
    simulation_column, user_column, run_column, create_column = st.columns(
        [2, 2, 2, 1],
        vertical_alignment="bottom",
    )
    simulation = simulation_column.text_input(
        "simulation_id",
        value=st.session_state.get("demo_simulation_id", "demo"),
    )
    user = user_column.text_input("user_id", value=st.session_state.get("demo_user_id", "demo-user"))
    run = run_column.text_input("run_id", value=st.session_state.get("demo_run_id", "demo-run"))
    create_clicked = create_column.button("打开沙盒", use_container_width=True)
    return simulation, user, run, create_clicked


def _render_background_config(
    st,
    pipeline,
    repository,
    turn: dict | None,
    session_id: str,
    simulation_id: str,
) -> BackgroundStepConfig:
    st.caption("本轮后台配置")
    if turn is None:
        config = BackgroundStepConfig()
        locked = False
        key_scope = f"{simulation_id}:draft"
    else:
        config = repository.background_config(turn["turn_id"])
        locked = turn.get("background_submitted_at") is not None
        key_scope = f"{simulation_id}:{turn['turn_id']}"

    values = {}
    columns = st.columns(3)
    for column, (field, label) in zip(columns, _CONFIG_CONTROLS):
        key = f"demo_background_config:{key_scope}:{field}"
        if key not in st.session_state:
            st.session_state[key] = getattr(config, field)
        values[field] = column.toggle(
            label,
            key=key,
            disabled=locked,
            help="后台分支提交后配置锁定；未启用分支保持 pending。",
        )
    selected = BackgroundStepConfig.from_mapping(values)
    if turn is not None and not locked and selected != config:
        pipeline.update_background_config(turn["turn_id"], selected, session_id=session_id)
        config = selected
    if locked:
        st.caption("后台分支已提交，本轮配置已锁定。")
    return config


def _render_live_details(st, environment, session_id: str, turn_id: str, initial_config: BackgroundStepConfig) -> None:
    repository = environment.repository
    auto_refresh = _session_has_running_background(repository, session_id)
    fragment = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)

    def render_details() -> None:
        selected_turn = repository.assert_turn_belongs_to_session(turn_id, session_id)
        steps = repository.list_steps(turn_id)
        config = repository.background_config(turn_id) if selected_turn else initial_config
        current = pipeline_panel.current_step(steps, config)
        display_step = _display_step(steps, current)
        snapshot = environment.state_service.snapshot(
            user_id=selected_turn["user_id"],
            run_id=selected_turn["run_id"],
        )
        retrieve = repository.get_step(turn_id, PipelineStep.RETRIEVE_CONTEXT)
        prompt = repository.get_step(turn_id, PipelineStep.BUILD_PROMPT)
        generation = repository.get_step(turn_id, PipelineStep.GENERATE_RESPONSE)

        tabs = st.tabs(["执行流程", "检索上下文", "最终 Prompt", "模型调用", "数据库和任务", "Trace"])
        with tabs[0], st.container(height=510, border=False):
            pipeline_panel.render_steps(st, steps, config)
        with tabs[1], st.container(height=510, border=False):
            context_panel.render(st, (retrieve or {}).get("output"))
        with tabs[2], st.container(height=510, border=False):
            prompt_panel.render_prompt(st, (prompt or {}).get("output"))
        with tabs[3], st.container(height=510, border=False):
            prompt_panel.render_generation(st, (generation or {}).get("output"))
        with tabs[4], st.container(height=510, border=False):
            memory_panel.render(st, snapshot, display_step)
        with tabs[5], st.container(height=510, border=False):
            trace_panel.render(st, steps)

        if auto_refresh and not _session_has_running_background(repository, session_id):
            st.rerun()

    if fragment is None:
        render_details()
        return
    decorated = fragment(run_every="1s" if auto_refresh else None)(render_details)
    decorated()


def _session_has_running_background(repository, session_id: str) -> bool:
    background_values = {step.value for step in BACKGROUND_STEPS}
    return any(
        step["step"] in background_values and step["status"] == "running"
        for turn in repository.list_turns(session_id)
        for step in repository.list_steps(turn["turn_id"])
    )


def _clear_sandbox_session_state(session_state: MutableMapping[str, Any], simulation_id: str) -> None:
    for key in _SESSION_KEYS:
        session_state.pop(key, None)
    prefixes = (
        f"demo_background_config:{simulation_id}:",
        f"demo_delete_confirm:{simulation_id}",
        f"demo_delete:{simulation_id}",
    )
    for key in list(session_state):
        if key.startswith(prefixes):
            session_state.pop(key, None)


def resolve_selected_turn_id(
    turns: Sequence[Mapping[str, Any]],
    current_turn_id: str | None,
) -> str | None:
    """Resolve a selection strictly within the currently displayed demo session."""
    turn_ids = [str(turn["turn_id"]) for turn in turns]
    if current_turn_id in turn_ids:
        return current_turn_id
    return turn_ids[-1] if turn_ids else None


def synchronize_selected_turn_id(
    session_state: MutableMapping[str, Any],
    turns: Sequence[Mapping[str, Any]],
) -> str | None:
    """Synchronize only the turn selection without clearing unrelated page state."""
    resolved = resolve_selected_turn_id(turns, session_state.get("demo_turn_id"))
    if resolved is None:
        session_state.pop("demo_turn_id", None)
    else:
        session_state["demo_turn_id"] = resolved
    return resolved


def _turn_label(turns: list[dict], turn_id: str) -> str:
    turn = next(item for item in turns if item["turn_id"] == turn_id)
    message = turn["user_message"]
    return f"{message[:28]}{'…' if len(message) > 28 else ''}"


def _display_step(steps: list[dict], current: dict | None) -> dict | None:
    failed = [step for step in steps if step["status"] == "failed"]
    if failed:
        return failed[-1]
    completed = [step for step in steps if step["status"] in {"succeeded", "skipped"}]
    return completed[-1] if completed else current
