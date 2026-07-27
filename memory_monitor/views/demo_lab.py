from __future__ import annotations

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
from memory_monitor.models import PipelineStep


def render(st, simulation_service) -> None:
    st.title("Agent Memory · Demo Lab")
    st.caption("冻结检索上下文，逐步观察 Prompt、提交、后台迁移与画像更新。")

    simulation_id, user_id, run_id, create = st.columns([2, 2, 2, 1])
    simulation = simulation_id.text_input(
        "simulation_id",
        value=st.session_state.get("demo_simulation_id", "demo"),
    )
    user = user_id.text_input("user_id", value=st.session_state.get("demo_user_id", "demo-user"))
    run = run_id.text_input("run_id", value=st.session_state.get("demo_run_id", "demo-run"))
    create_clicked = create.button("打开沙盒", use_container_width=True)

    environment = None
    session = None
    if create_clicked or st.session_state.get("demo_simulation_id") == simulation:
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

    action = pipeline_panel.render_controls(st, disabled=not bool(turn_id))
    if action and turn_id:
        pipeline_panel.apply_action(st, pipeline, repository, turn_id, session["session_id"], action)

    left, right = st.columns([0.9, 1.6], gap="large")
    with left:
        chat_panel.render_history(st, repository.raw_messages(session["session_id"]))
        user_message = chat_panel.chat_input(st)
        if user_message:
            turn = pipeline.create_turn(
                session["session_id"],
                user_id=user,
                run_id=run,
                user_message=user_message,
            )
            st.session_state["demo_turn_id"] = turn["turn_id"]
            st.rerun()
        if turns:
            turn_options = [turn["turn_id"] for turn in reversed(turns)]
            selected_turn = st.selectbox(
                "当前轮次",
                turn_options,
                index=turn_options.index(turn_id),
                format_func=lambda value: _turn_label(turns, value),
            )
            if selected_turn != turn_id:
                st.session_state["demo_turn_id"] = selected_turn
                st.rerun()

    if not turn_id:
        with right:
            st.info("在左侧输入问题后，可逐步执行当前轮。")
        return

    steps = repository.list_steps(turn_id)
    selected_turn = repository.assert_turn_belongs_to_session(turn_id, session["session_id"])
    current = pipeline_panel.current_step(steps)
    display_step = _display_step(steps, current)
    snapshot = environment.state_service.snapshot(
        user_id=selected_turn["user_id"],
        run_id=selected_turn["run_id"],
    )
    retrieve = repository.get_step(turn_id, PipelineStep.RETRIEVE_CONTEXT)
    prompt = repository.get_step(turn_id, PipelineStep.BUILD_PROMPT)
    generation = repository.get_step(turn_id, PipelineStep.GENERATE_RESPONSE)

    with right:
        st.progress(pipeline_panel.progress(steps))
        upper_tabs = st.tabs(["执行流程", "检索上下文", "最终 Prompt", "模型调用"])
        with upper_tabs[0]:
            pipeline_panel.render_steps(st, steps)
        with upper_tabs[1]:
            context_panel.render(st, (retrieve or {}).get("output"))
        with upper_tabs[2]:
            prompt_panel.render_prompt(st, (prompt or {}).get("output"))
        with upper_tabs[3]:
            prompt_panel.render_generation(st, (generation or {}).get("output"))

        st.divider()
        state_tab, trace_tab = st.tabs(["数据库和任务实时结果", "Trace"])
        with state_tab:
            memory_panel.render(st, snapshot, display_step)
        with trace_tab:
            trace_panel.render(st, steps)

    with st.sidebar:
        st.markdown("### 沙盒")
        st.code(str(environment.root), language="text")
        confirm = st.checkbox("确认删除当前沙盒")
        if st.button("删除沙盒", disabled=not confirm, use_container_width=True):
            simulation_service.clear_environment(simulation)
            for key in ("demo_simulation_id", "demo_session_id", "demo_turn_id"):
                st.session_state.pop(key, None)
            st.rerun()


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
    if current and current["status"] == "failed":
        return current
    completed = [step for step in steps if step["status"] in {"succeeded", "skipped"}]
    return completed[-1] if completed else current
