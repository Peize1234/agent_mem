from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Mapping

from memory_monitor.models import (
    BACKGROUND_STEPS,
    FOREGROUND_STEPS,
    PIPELINE_STEPS,
    BackgroundStepConfig,
    PipelineStep,
)

_STEP_LABELS = {
    PipelineStep.CAPTURE_INPUT: "捕获输入",
    PipelineStep.RETRIEVE_CONTEXT: "检索上下文",
    PipelineStep.BUILD_PROMPT: "构建 Prompt",
    PipelineStep.GENERATE_RESPONSE: "模型回答",
    PipelineStep.COMMIT_TURN: "提交当前轮",
    PipelineStep.RUN_MIDTERM: "中期记忆",
    PipelineStep.RUN_LONGTERM: "长期记忆",
    PipelineStep.RUN_PROFILE: "用户画像",
    PipelineStep.REFRESH_STATE: "刷新状态",
}
_STATUS_LABELS = {
    "pending": "pending · 等待",
    "running": "running · 执行中",
    "succeeded": "succeeded · 成功",
    "failed": "failed · 失败",
    "skipped": "skipped · 已跳过",
}


@dataclass(frozen=True)
class PipelineNode:
    step: PipelineStep
    label: str
    status: str
    attempts: int
    duration_ms: float | None
    error: str | None
    enabled: bool
    current: bool


def build_nodes(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None,
) -> dict[PipelineStep, PipelineNode]:
    config = (
        background_config
        if isinstance(background_config, BackgroundStepConfig)
        else BackgroundStepConfig.from_mapping(background_config)
    )
    step_map = {PipelineStep(item["step"]): item for item in steps}
    current_step = _current_step(step_map, config)
    return {
        step: PipelineNode(
            step=step,
            label=_STEP_LABELS[step],
            status=step_map[step]["status"],
            attempts=int(step_map[step].get("attempts") or 0),
            duration_ms=step_map[step].get("duration_ms"),
            error=step_map[step].get("error_message"),
            enabled=config.enabled(step),
            current=step is current_step,
        )
        for step in PIPELINE_STEPS
    }


def progress(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None,
) -> tuple[int, int, float]:
    config = (
        background_config
        if isinstance(background_config, BackgroundStepConfig)
        else BackgroundStepConfig.from_mapping(background_config)
    )
    effective = (*FOREGROUND_STEPS, *config.enabled_background_steps(), PipelineStep.REFRESH_STATE)
    step_map = {PipelineStep(item["step"]): item for item in steps}
    completed = sum(step_map[step]["status"] in {"succeeded", "skipped"} for step in effective)
    return completed, len(effective), completed / len(effective)


def render_html(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None,
) -> str:
    nodes = build_nodes(steps, background_config)
    completed, total, _ratio = progress(steps, background_config)
    foreground = '<div class="demo-arrow">→</div>'.join(_node_html(nodes[step]) for step in FOREGROUND_STEPS)
    branches = "".join(f'<div class="demo-branch">{_node_html(nodes[step])}</div>' for step in BACKGROUND_STEPS)
    return (
        '<div class="demo-pipeline">'
        '<div class="demo-pipeline-summary">'
        f"<span>有效进度 {completed}/{total}</span>"
        "<span>三类后台任务使用独立有序队列</span>"
        "</div>"
        f'<div class="demo-foreground">{foreground}</div>'
        '<div class="demo-fork-stem"></div>'
        f'<div class="demo-branches">{branches}</div>'
        '<div class="demo-merge-stem"></div>'
        f'<div class="demo-refresh">{_node_html(nodes[PipelineStep.REFRESH_STATE])}</div>'
        "</div>"
    )


def _node_html(node: PipelineNode) -> str:
    classes = ["demo-node", node.status]
    if node.current:
        classes.append("current")
    if not node.enabled:
        classes.append("disabled")
    duration = f" · {node.duration_ms:.0f} ms" if node.duration_ms is not None else ""
    disabled = " · 本轮未启用" if not node.enabled else ""
    error = ""
    if node.error:
        short_error = node.error[:90] + ("…" if len(node.error) > 90 else "")
        error = f'<div class="demo-node-error" title="{html.escape(node.error)}">{html.escape(short_error)}</div>'
    return (
        f'<div class="{" ".join(classes)}">'
        f'<div class="demo-node-title">{html.escape(node.label)}</div>'
        f'<div class="demo-node-status">{html.escape(_STATUS_LABELS.get(node.status, node.status))}</div>'
        f'<div class="demo-node-meta">尝试 {node.attempts}{duration}{disabled}</div>'
        f"{error}</div>"
    )


def _current_step(
    step_map: dict[PipelineStep, dict[str, Any]],
    config: BackgroundStepConfig,
) -> PipelineStep | None:
    for status in ("running", "failed"):
        match = next(
            (
                step
                for step in PIPELINE_STEPS
                if step_map[step]["status"] == status and (step not in BACKGROUND_STEPS or config.enabled(step))
            ),
            None,
        )
        if match is not None:
            return match
    return next(
        (
            step
            for step in PIPELINE_STEPS
            if step_map[step]["status"] == "pending" and (step not in BACKGROUND_STEPS or config.enabled(step))
        ),
        None,
    )
