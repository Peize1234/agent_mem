from __future__ import annotations

from memory_monitor.models import OPTIONAL_PIPELINE_STEPS, PIPELINE_STEPS, PipelineStep
from memory_monitor.services.demo_repository import StepAlreadyRunningError

_ACTIONS = (
    ("next", "下一步"),
    ("answer", "运行到模型回答"),
    ("commit", "运行到提交"),
    ("all", "运行全部"),
    ("retry", "重试当前步骤"),
    ("skip", "跳过可选步骤"),
    ("reset", "重置当前轮"),
)


def render_controls(st, *, disabled: bool) -> str | None:
    columns = st.columns(len(_ACTIONS))
    selected = None
    for column, (action, label) in zip(columns, _ACTIONS):
        if column.button(label, disabled=disabled, use_container_width=True):
            selected = action
    return selected


def apply_action(st, pipeline, repository, turn_id: str, action: str) -> None:
    current = current_step(repository.list_steps(turn_id))
    try:
        if action == "next":
            pipeline.run_next_step(turn_id)
        elif action == "answer":
            pipeline.run_until(turn_id, PipelineStep.GENERATE_RESPONSE)
        elif action == "commit":
            pipeline.run_until(turn_id, PipelineStep.COMMIT_TURN)
        elif action == "all":
            pipeline.run_until(turn_id, PipelineStep.REFRESH_STATE)
        elif action == "retry":
            if current is None or current["status"] != "failed":
                st.info("当前没有失败步骤可重试。")
                return
            pipeline.retry_step(turn_id, current["step"])
        elif action == "skip":
            if current is None or PipelineStep(current["step"]) not in OPTIONAL_PIPELINE_STEPS:
                st.info("当前步骤不可跳过。")
                return
            pipeline.skip_step(turn_id, current["step"])
        elif action == "reset":
            pipeline.reset_turn(turn_id)
        st.rerun()
    except StepAlreadyRunningError as exc:
        st.warning(str(exc))
    except Exception as exc:
        st.error(str(exc))


def render_steps(st, steps: list[dict]) -> None:
    rows = [
        {
            "步骤": step["step"],
            "状态": step["status"],
            "尝试": step["attempts"],
            "耗时(ms)": round(step["duration_ms"], 2) if step.get("duration_ms") is not None else None,
            "错误": step.get("error_message"),
        }
        for step in steps
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)


def current_step(steps: list[dict]) -> dict | None:
    complete = {"succeeded", "skipped"}
    return next((step for step in steps if step["status"] not in complete), None)


def progress(steps: list[dict]) -> float:
    completed = sum(step["status"] in {"succeeded", "skipped"} for step in steps)
    return completed / len(PIPELINE_STEPS)
