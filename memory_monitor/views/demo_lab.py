from __future__ import annotations

import html
import logging
import sqlite3
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
from memory_monitor.models import (
    CORE_JOB_ACTIVE_STATUSES,
    BackgroundStepConfig,
    PipelineStep,
    TERMINAL_STEP_STATUSES,
)

logger = logging.getLogger(__name__)

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
    "long_term": [],  # legacy snapshots only
    "fine_grained_longterm": [],
    "promoted_longterm": [],
    "profile": [],
    "jobs": {"migration": [], "longterm_extraction": [], "profile": [], "promotion": []},
}
_WORKSPACE_SECTIONS = (
    ("执行流程", "pipeline"),
    ("分层检索", "context"),
    ("最终 Prompt", "prompt"),
    ("模型调用", "generation"),
    ("数据库和任务", "database"),
    ("Trace", "trace"),
)
_ACTIVE_JOB_STATUSES = CORE_JOB_ACTIVE_STATUSES


def render(st, simulation_service, config) -> None:
    header = st.empty()
    restoring = all(st.session_state.get(key) for key in _SESSION_KEYS[:3])
    with header.container():
        _render_header(st, simulation_service, None, restoring=restoring)

    simulation, user, run, create_clicked = _render_scope_controls(st)
    environment = None
    session = None
    open_error = None
    if create_clicked:
        try:
            with st.spinner("正在打开沙盒并预热检索组件…"):
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
            logger.exception("Could not open Demo sandbox simulation=%s", simulation)
            open_error = exc
    else:
        environment, session = _restore_environment(st, simulation_service)

    with header.container():
        _render_header(st, simulation_service, environment)

    if open_error is not None:
        st.error(f"打开沙盒失败（{type(open_error).__name__}）：{_error_summary(open_error)}")
        return
    if environment is None or session is None:
        st.info("先打开一个隔离沙盒。")
        return

    repository = environment.repository
    pipeline = environment.pipeline

    left, right = st.columns([0.92, 1.62], gap="large")
    with left:
        _render_chat_history_workspace(
            st,
            repository,
            simulation_id=environment.simulation_id,
            session_id=session["session_id"],
            poll_interval_seconds=config.poll_interval_seconds,
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
    session_id = st.session_state.get("demo_session_id")
    environment = None
    if not all((simulation_id, user_id, run_id)):
        return None, None
    try:
        environment = simulation_service.environment(simulation_id)
        repository = environment.repository
        session = repository.get_session(session_id) if session_id else None
        if session is not None and (
            session["simulation_id"] != simulation_id or session["user_id"] != user_id or session["run_id"] != run_id
        ):
            session = None
        if session is None:
            session = repository.find_session(simulation_id, user_id, run_id)
        if session is None:
            st.warning("未找到已保存的会话，请点击“打开沙盒”重新建立会话。")
            return environment, None
        st.session_state["demo_session_id"] = session["session_id"]
        return environment, session
    except sqlite3.OperationalError as exc:
        logger.warning(
            "Demo session restore query failed simulation=%s",
            simulation_id,
            exc_info=True,
        )
        if _is_database_locked(exc):
            st.warning("数据库正忙，当前沙盒将在下一次刷新时自动恢复。")
            if environment is not None and session_id:
                return environment, {
                    "session_id": session_id,
                    "simulation_id": simulation_id,
                    "user_id": user_id,
                    "run_id": run_id,
                }
        else:
            st.error(f"恢复沙盒失败（{type(exc).__name__}）：{_error_summary(exc)}")
        return None, None
    except Exception as exc:
        logger.exception("Could not restore Demo sandbox simulation=%s", simulation_id)
        st.error(f"恢复沙盒失败（{type(exc).__name__}）：{_error_summary(exc)}")
        return None, None


def _render_header(st, simulation_service, environment, *, restoring: bool = False) -> None:
    title, sandbox = st.columns([1.45, 1], vertical_alignment="center")
    with title:
        st.markdown('<h1 class="demo-lab-title">Agent Memory · Demo Lab</h1>', unsafe_allow_html=True)
        st.markdown(
            '<p class="demo-lab-subtitle">Demo 调度队列异步推进；Memory.add 创建的 Core Jobs 使用真实 lease、重试和状态。</p>',
            unsafe_allow_html=True,
        )
    with sandbox:
        if environment is None:
            st.caption("正在恢复沙盒…" if restoring else "尚未打开沙盒")
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


def _render_chat_history_workspace(
    st,
    repository,
    *,
    simulation_id: str,
    session_id: str,
    poll_interval_seconds: float,
) -> None:
    @st.fragment(run_every=poll_interval_seconds)
    def chat_history_workspace() -> None:
        try:
            chat_panel.render_history(
                st,
                repository.raw_messages(session_id),
                simulation_id=simulation_id,
                session_id=session_id,
            )
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Could not query Demo chat history session=%s",
                session_id,
                exc_info=True,
            )
            if _is_database_locked(exc):
                st.warning("对话数据库正忙，将在下一次刷新时自动重试。")
            else:
                st.error(f"对话记录查询失败（{type(exc).__name__}）：{_error_summary(exc)}")
        except Exception:
            logger.exception("Could not render Demo chat history session=%s", session_id)
            st.error("对话记录暂时无法加载。")
            if st.button("重新加载对话", key=f"chat_reload:{simulation_id}:{session_id}"):
                st.rerun(scope="fragment")

    chat_history_workspace()


def _render_right_workspace(
    st,
    environment,
    session_id: str,
    *,
    poll_interval_seconds: float,
) -> None:
    repository = environment.repository
    auto_refresh_enabled = _right_workspace_auto_refresh_enabled(environment, session_id)
    run_every = poll_interval_seconds if auto_refresh_enabled else None

    @st.fragment(run_every=run_every)
    def right_workspace() -> None:
        try:
            turns = repository.list_turns(session_id)
            turn_id = synchronize_selected_turn_id(st.session_state, turns)
            turn = next((item for item in turns if item["turn_id"] == turn_id), None)
            steps = repository.list_steps(turn_id) if turn_id is not None else []
            active_turns = repository.list_active_turns(session_id)
            completed_turns = [item for item in turns if item.get("completed_at") is not None]
            backend_jobs_active = False if active_turns else _session_has_active_jobs(environment, session_id)
            if auto_refresh_enabled and not _right_workspace_needs_polling(
                turn,
                steps,
                active_turns,
                backend_jobs_active=backend_jobs_active,
            ):
                st.rerun()
            with st.container(key=f"right_workspace_{environment.simulation_id}_{session_id}"):
                _render_right_workspace_content(
                    st,
                    environment,
                    session_id,
                    turn,
                    steps,
                    active_turns,
                    completed_turns,
                )
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Could not query Demo right workspace session=%s",
                session_id,
                exc_info=True,
            )
            if _is_database_locked(exc):
                st.warning("数据库正忙，右侧状态将在下一次刷新时自动恢复。")
            else:
                st.error(f"右侧数据查询失败（{type(exc).__name__}）：{_error_summary(exc)}")
        except Exception:
            logger.exception("Could not render Demo right workspace session=%s", session_id)
            st.error("右侧工作区暂时无法加载。")
            if st.button(
                "重新加载右侧区域",
                key=f"right_reload:{environment.simulation_id}:{session_id}",
            ):
                st.rerun(scope="fragment")

    right_workspace()


def _render_right_workspace_content(
    st,
    environment,
    session_id: str,
    turn: dict | None,
    steps: list[dict],
    active_turns: list[dict],
    completed_turns: list[dict],
) -> None:
    repository = environment.repository
    pipeline = environment.pipeline
    turn_id = turn["turn_id"] if turn is not None else None
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

    with st.container(key=f"right_controls_{environment.simulation_id}_{session_id}_{turn_id}"):
        pipeline_panel.render_memory_gates(
            st,
            pipeline,
            repository,
            simulation_id=environment.simulation_id,
            session_id=session_id,
            turn_id=turn_id,
            steps=steps,
        )
        action, target = pipeline_panel.render_controls(
            st,
            key_prefix=f"pipeline:{environment.simulation_id}:{session_id}:{turn_id}",
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

    _render_turn_navigation(
        st,
        active_turns,
        completed_turns,
        turn_id,
        simulation_id=environment.simulation_id,
        session_id=session_id,
    )

    selected_config = BackgroundStepConfig.from_mapping(turn)
    step_map = {step["step"]: step for step in steps}
    key_scope = f"details:{environment.simulation_id}:{session_id}:{turn_id}"
    labels = [label for label, _section in _WORKSPACE_SECTIONS]
    selected = st.segmented_control(
        "工作区页面",
        labels,
        default=labels[0],
        key=f"workspace_section:{environment.simulation_id}:{session_id}:{turn_id}",
        label_visibility="collapsed",
        width="stretch",
    )
    section = dict(_WORKSPACE_SECTIONS).get(selected or labels[0], "pipeline")
    if section == "pipeline":
        with st.container(border=False, key=f"{key_scope}:pipeline"):
            pipeline_panel.render_steps(st, steps, selected_config)
    elif section == "context":
        with st.container(height=515, border=False, key=f"{key_scope}:context"):
            retrieve = step_map.get(PipelineStep.RETRIEVE_CONTEXT.value)
            context_panel.render(
                st,
                (retrieve or {}).get("output"),
                key_prefix=f"{key_scope}:retrieval",
            )
    elif section == "prompt":
        with st.container(height=515, border=False, key=f"{key_scope}:prompt"):
            prompt = step_map.get(PipelineStep.BUILD_PROMPT.value)
            prompt_panel.render_prompt(st, (prompt or {}).get("output"))
    elif section == "generation":
        with st.container(height=515, border=False, key=f"{key_scope}:generation"):
            generation = step_map.get(PipelineStep.GENERATE_RESPONSE.value)
            prompt_panel.render_generation(
                st,
                (generation or {}).get("output"),
                key_prefix=f"{key_scope}:generation",
            )
    elif section == "database":
        with st.container(
            height=515,
            border=False,
            key=f"database_detail_{environment.simulation_id}_{session_id}_{turn_id}",
        ):
            try:
                current = pipeline_panel.current_step(steps, selected_config)
                memory_panel.render(
                    st,
                    None,
                    _display_step(steps, current),
                    key_prefix=f"{key_scope}:records",
                    state_loader=lambda sections: _latest_session_state(
                        st,
                        environment,
                        session_id,
                        sections=sections,
                    ),
                )
            except sqlite3.OperationalError as exc:
                logger.warning(
                    "Could not query Demo database panel session=%s turn=%s",
                    session_id,
                    turn_id,
                    exc_info=True,
                )
                if _is_database_locked(exc):
                    st.warning("数据库正忙，本分区将在下一次刷新时自动恢复。")
                else:
                    st.error(f"数据库分区查询失败（{type(exc).__name__}）：{_error_summary(exc)}")
    else:
        with st.container(height=515, border=False, key=f"{key_scope}:trace"):
            trace_panel.render(st, steps, key_prefix=f"{key_scope}:trace")


def _latest_session_state(_st, environment, session_id: str, *, sections=None) -> dict:
    """Read selected live backend partitions without consulting step snapshots."""
    session = environment.repository.get_session(session_id)
    if session is None:
        return _EMPTY_SNAPSHOT
    state_reader = getattr(environment.state_service, "current_state", None)
    if not callable(state_reader):
        state_reader = environment.state_service.snapshot
    return state_reader(
        user_id=session["user_id"],
        run_id=session["run_id"],
        sections=sections,
    )


def _right_workspace_auto_refresh_enabled(environment, session_id: str) -> bool:
    try:
        if environment.repository.list_active_turns(session_id):
            return True
        return _session_has_active_jobs(environment, session_id)
    except Exception:
        logger.warning(
            "Could not determine Demo right workspace refresh state session=%s; keeping polling enabled",
            session_id,
            exc_info=True,
        )
        return True


def _session_has_active_jobs(environment, session_id: str) -> bool:
    session = environment.repository.get_session(session_id)
    if session is None:
        return False
    checker = getattr(environment.state_service, "has_active_jobs", None)
    if callable(checker):
        return bool(checker(user_id=session["user_id"], run_id=session["run_id"]))
    state_reader = getattr(environment.state_service, "current_state", None)
    if not callable(state_reader):
        state_reader = environment.state_service.snapshot
    state = state_reader(
        user_id=session["user_id"],
        run_id=session["run_id"],
        sections={"migration_jobs", "longterm_extraction_jobs", "profile_jobs", "promotion_jobs"},
    )
    jobs = state.get("jobs") or {}
    migration_active = any(
        _ACTIVE_JOB_STATUSES.intersection({job.get("status"), job.get("midterm_status"), job.get("longterm_status")})
        for job in jobs.get("migration") or []
    )
    profile_active = any(job.get("status") in _ACTIVE_JOB_STATUSES for job in jobs.get("profile") or [])
    extraction_active = any(job.get("status") in _ACTIVE_JOB_STATUSES for job in jobs.get("longterm_extraction") or [])
    promotion_active = any(job.get("status") in _ACTIVE_JOB_STATUSES for job in jobs.get("promotion") or [])
    return migration_active or profile_active or extraction_active or promotion_active


def _right_workspace_needs_polling(
    turn: dict | None,
    steps: list[dict],
    active_turns: list[dict],
    *,
    backend_jobs_active: bool,
) -> bool:
    if active_turns or backend_jobs_active:
        return True
    if turn is None or turn.get("completed_at") is not None:
        return False
    if not steps:
        return True
    return any(step.get("status") not in TERMINAL_STEP_STATUSES for step in steps)


def _render_turn_navigation(
    st,
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
        format_func=lambda value: _turn_label(completed_turns, value),
        placeholder="选择已完成轮次",
        key=f"history_turn:{simulation_id}:{session_id}:selected:{selected_turn_id}",
    )
    if selected and selected != selected_turn_id:
        st.session_state["demo_turn_id"] = selected
        st.rerun(scope="fragment")


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
                st.rerun(scope="fragment")


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
        f"chat_reload:{simulation_id}:",
        f"right_reload:{simulation_id}:",
        f"workspace_section:{simulation_id}:",
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


def _turn_label(turns: list[dict], turn_id: str) -> str:
    turn = next(item for item in turns if item["turn_id"] == turn_id)
    return f"{_summarize(turn['user_message'], 32)} · 已完成"


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


def _is_database_locked(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower() or "busy" in str(exc).lower()


def _error_summary(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else "未知错误"
    return message[:240]
