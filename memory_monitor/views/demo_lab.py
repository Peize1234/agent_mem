from __future__ import annotations

import html
from collections.abc import Mapping, MutableMapping, Sequence
from datetime import datetime
from typing import Any

from memory_monitor.components import (
    chat_panel,
    context_panel,
    memory_panel,
    pipeline_panel,
    prompt_panel,
    trace_panel,
)
from memory_monitor.models import PipelineStep

_SESSION_KEYS = (
    "demo_simulation_id",
    "demo_user_id",
    "demo_run_id",
    "demo_session_id",
    "demo_turn_id",
)
_EMPTY_SNAPSHOT = {
    "short_term": [],
    "midterm_sessions": [],
    "midterm_pages": [],
    "long_term": [],
    "profile": [],
    "jobs": {"migration": [], "profile": []},
}


def render(st, simulation_service, config) -> None:
    environment, session = _restore_environment(st, simulation_service)
    header = st.container()
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
        except Exception as exc:
            st.error(str(exc))
            return

    with header:
        _render_header(st, simulation_service, environment)

    if environment is None or session is None:
        st.info("先打开一个隔离沙盒。")
        return

    repository = environment.repository
    pipeline = environment.pipeline

    left, right = st.columns([0.92, 1.62], gap="large")
    with left:
        chat_panel.render_history(
            st,
            repository.raw_messages(session["session_id"]),
            simulation_id=environment.simulation_id,
        )
        user_message = chat_panel.chat_input(
            st,
            key=f"chat_input:{environment.simulation_id}:{session['session_id']}",
        )
        if user_message:
            turn = pipeline.create_turn(
                session["session_id"],
                user_id=session["user_id"],
                run_id=session["run_id"],
                user_message=user_message,
            )
            st.session_state["demo_turn_id"] = turn["turn_id"]

    with right:
        _render_right_workspace(
            st,
            environment,
            session["session_id"],
            poll_interval_seconds=config.poll_interval_seconds,
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
            '<p class="demo-lab-subtitle">前台链异步执行；短期、中期、长期和画像使用四条独立队列。</p>',
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
            with st.popover(
                "沙盒管理",
                width="stretch",
                key=f"sandbox:{environment.simulation_id}:manage",
            ):
                st.caption(f"simulation_id：`{environment.simulation_id}`")
                st.code(str(environment.root), language="text")
                confirm_key = f"sandbox:{environment.simulation_id}:delete_confirm"
                confirm = st.checkbox("确认删除当前沙盒", key=confirm_key)
                if st.button(
                    "删除沙盒",
                    disabled=not confirm,
                    type="primary",
                    width="stretch",
                    key=f"sandbox:{environment.simulation_id}:delete",
                ):
                    try:
                        with st.spinner("正在等待任务结束、关闭队列并删除沙盒…"):
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
        key="scope:simulation_id",
    )
    user = user_column.text_input(
        "user_id",
        value=st.session_state.get("demo_user_id", "demo-user"),
        key="scope:user_id",
    )
    run = run_column.text_input(
        "run_id",
        value=st.session_state.get("demo_run_id", "demo-run"),
        key="scope:run_id",
    )
    create_clicked = create_column.button(
        "打开沙盒",
        width="stretch",
        key="scope:open_sandbox",
    )
    return simulation, user, run, create_clicked


def _render_right_workspace(
    st,
    environment,
    session_id: str,
    *,
    poll_interval_seconds: float,
) -> None:
    repository = environment.repository
    pipeline = environment.pipeline

    @st.fragment(run_every=poll_interval_seconds)
    def right_workspace() -> None:
        with st.container(key=f"right_workspace_{environment.simulation_id}_{session_id}"):
            turns = repository.list_turns(session_id)
            turn_id = synchronize_selected_turn_id(st.session_state, turns)
            if turn_id is None:
                pipeline_panel.render_memory_gates(
                    st,
                    pipeline,
                    repository,
                    simulation_id=environment.simulation_id,
                    session_id=session_id,
                    turn_id=None,
                )
                st.caption("在左侧输入问题后，可异步执行当前轮。")
                return

            repository.assert_turn_belongs_to_session(turn_id, session_id)
            steps = repository.list_steps(turn_id)
            with st.container(key=f"right_controls_{environment.simulation_id}_{turn_id}"):
                pipeline_panel.render_memory_gates(
                    st,
                    pipeline,
                    repository,
                    simulation_id=environment.simulation_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                action, target = pipeline_panel.render_controls(
                    st,
                    key_prefix=f"pipeline:{environment.simulation_id}:{turn_id}",
                    disabled=False,
                    steps=steps,
                )
                if action:
                    pipeline_panel.apply_action(
                        st,
                        pipeline,
                        repository,
                        turn_id,
                        session_id,
                        action,
                        target=target,
                    )

            # Actions only submit persisted work. Re-read the authoritative
            # rows in this fragment so queued state is visible immediately.
            steps = repository.list_steps(turn_id)
            active_turns = repository.list_active_turns(session_id)
            completed_turns = repository.list_completed_turns(session_id)
            _render_turn_navigation(
                st,
                repository,
                active_turns,
                completed_turns,
                turn_id,
                simulation_id=environment.simulation_id,
                session_id=session_id,
            )

            selected_config = repository.background_config(turn_id)
            current = pipeline_panel.current_step(steps, selected_config)
            display_step = _display_step(steps, current)
            snapshot = _latest_session_state(
                st,
                environment,
                session_id,
            )
            step_map = {step["step"]: step for step in steps}
            retrieve = step_map.get(PipelineStep.RETRIEVE_CONTEXT.value)
            prompt = step_map.get(PipelineStep.BUILD_PROMPT.value)
            generation = step_map.get(PipelineStep.GENERATE_RESPONSE.value)
            key_scope = f"details:{environment.simulation_id}:{turn_id}"

            tabs = st.tabs(
                ["执行流程", "检索上下文", "最终 Prompt", "模型调用", "数据库和任务", "Trace"],
                key=f"{key_scope}:tabs",
            )
            with tabs[0], st.container(height=390, border=False, key=f"{key_scope}:pipeline"):
                pipeline_panel.render_steps(st, steps, selected_config)
            with tabs[1], st.container(height=515, border=False, key=f"{key_scope}:context"):
                context_panel.render(
                    st,
                    (retrieve or {}).get("output"),
                    key_prefix=f"{key_scope}:retrieval",
                )
            with tabs[2], st.container(height=515, border=False, key=f"{key_scope}:prompt"):
                prompt_panel.render_prompt(st, (prompt or {}).get("output"))
            with tabs[3], st.container(height=515, border=False, key=f"{key_scope}:generation"):
                prompt_panel.render_generation(
                    st,
                    (generation or {}).get("output"),
                    key_prefix=f"{key_scope}:generation",
                )
            with tabs[4], st.container(height=515, border=False, key=f"{key_scope}:database"):
                memory_panel.render(
                    st,
                    snapshot,
                    display_step,
                    key_prefix=f"{key_scope}:records",
                )
            with tabs[5], st.container(height=515, border=False, key=f"{key_scope}:trace"):
                trace_panel.render(st, steps, key_prefix=f"{key_scope}:trace")

    right_workspace()


def _latest_session_state(st, environment, session_id: str) -> dict:
    snapshot_row = environment.repository.latest_session_snapshot(session_id)
    if snapshot_row is not None:
        return snapshot_row.get("data") or _EMPTY_SNAPSHOT

    cache_key = f"session_snapshot:{environment.simulation_id}:{session_id}"
    cached = st.session_state.get(cache_key)
    if cached is not None:
        return cached
    session = environment.repository.get_session(session_id)
    if session is None:
        return _EMPTY_SNAPSHOT
    try:
        snapshot = environment.state_service.snapshot(
            user_id=session["user_id"],
            run_id=session["run_id"],
        )
    except Exception as exc:
        snapshot = {**_EMPTY_SNAPSHOT, "snapshot_error": f"{type(exc).__name__}: {exc}"}
    st.session_state[cache_key] = snapshot
    return snapshot


def _render_turn_navigation(
    st,
    repository,
    active_turns: list[dict],
    completed_turns: list[dict],
    selected_turn_id: str,
    *,
    simulation_id: str,
    session_id: str,
) -> None:
    _render_active_turns(st, active_turns, selected_turn_id, simulation_id=simulation_id)
    if not completed_turns:
        return
    options = [turn["turn_id"] for turn in reversed(completed_turns)]
    selected = st.selectbox(
        "历史轮次",
        options,
        index=options.index(selected_turn_id) if selected_turn_id in options else None,
        format_func=lambda value: _turn_label(repository, completed_turns, value),
        placeholder="选择已完成轮次",
        key=f"history_turn:{simulation_id}:{session_id}:selected:{selected_turn_id}",
    )
    if selected and selected != selected_turn_id:
        st.session_state["demo_turn_id"] = selected
        st.rerun(scope="app")


def _render_active_turns(
    st,
    active_turns: list[dict],
    selected_turn_id: str,
    *,
    simulation_id: str,
) -> None:
    if not active_turns:
        st.caption("当前没有未完成轮次。")
        return
    st.caption("活跃轮次")
    for start in range(0, len(active_turns), 3):
        row = active_turns[start : start + 3]
        columns = st.columns(len(row))
        for column, turn in zip(columns, row):
            status = turn["status"]
            message = _summarize(turn["user_message"], 30)
            created = _format_created_at(turn.get("created_at"))
            selected = turn["turn_id"] == selected_turn_id
            failed = " · 有失败" if turn.get("has_failure") else ""
            label = f"{message}\n\n{created} · {turn['completed_steps']}/{turn['total_steps']} · {status}{failed}"
            if column.button(
                label,
                key=f"active_turn:{simulation_id}:{turn['turn_id']}",
                type="primary" if selected else "secondary",
                help=turn["user_message"],
                width="stretch",
            ):
                st.session_state["demo_turn_id"] = turn["turn_id"]
                st.rerun(scope="app")


def _clear_sandbox_session_state(session_state: MutableMapping[str, Any], simulation_id: str) -> None:
    for key in _SESSION_KEYS:
        session_state.pop(key, None)
    scoped_fragments = (
        f"draft_config:{simulation_id}:",
        f"turn_config:{simulation_id}:",
        f"memory_gate:{simulation_id}:",
        f"history_turn:{simulation_id}:",
        f"active_turn:{simulation_id}:",
        f"pipeline:{simulation_id}:",
        f"details:{simulation_id}:",
        f"session_snapshot:{simulation_id}:",
        f"sandbox:{simulation_id}:",
        f"chat_input:{simulation_id}:",
    )
    for key in list(session_state):
        if key.startswith(scoped_fragments):
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


def _turn_label(repository, turns: list[dict], turn_id: str) -> str:
    turn = next(item for item in turns if item["turn_id"] == turn_id)
    summary = repository.turn_summary(turn)
    return f"{_summarize(turn['user_message'], 32)} · {summary['status']} · {summary['completed_steps']}/{summary['total_steps']}"


def _summarize(value: str, limit: int) -> str:
    return f"{value[:limit]}{'…' if len(value) > limit else ''}"


def _format_created_at(value: str | None) -> str:
    if not value:
        return "时间未知"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%H:%M:%S")


def _display_step(steps: list[dict], current: dict | None) -> dict | None:
    failed = [step for step in steps if step["status"] == "failed"]
    if failed:
        return failed[-1]
    completed = [step for step in steps if step["status"] in {"succeeded", "skipped"}]
    return completed[-1] if completed else current
