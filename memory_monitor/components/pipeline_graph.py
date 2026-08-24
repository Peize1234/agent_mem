from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Mapping

from memory_monitor.components.llm_call_formatter import (
    format_json_document,
    format_model_answer,
    format_prompt_messages,
    is_json_document,
    split_mixed_text_and_json,
)
from memory_monitor.models import (
    MEMORY_STEPS,
    PIPELINE_STEPS,
    TERMINAL_STEP_STATUSES,
    VISIBLE_PIPELINE_STEPS,
    BackgroundStepConfig,
    PipelineStep,
    StepStatus,
)

QUERY_REWRITE_NODE = "query_rewrite"
DISPLAY_FOREGROUND_STEPS = (
    PipelineStep.CAPTURE_INPUT,
    QUERY_REWRITE_NODE,
    PipelineStep.RETRIEVE_CONTEXT,
    PipelineStep.AGENTIC_RETRIEVAL,
    PipelineStep.BUILD_PROMPT,
    PipelineStep.GENERATE_RESPONSE,
)

_STEP_LABELS = {
    PipelineStep.CAPTURE_INPUT: "捕获输入",
    QUERY_REWRITE_NODE: "问题重写",
    PipelineStep.RETRIEVE_CONTEXT: "分层检索",
    PipelineStep.AGENTIC_RETRIEVAL: "Agentic 检索",
    PipelineStep.BUILD_PROMPT: "构建 Prompt",
    PipelineStep.GENERATE_RESPONSE: "模型回答",
    PipelineStep.RUN_SHORTTERM: "提交本轮记忆",
    PipelineStep.RUN_MIDTERM: "添加中期记忆",
    PipelineStep.RUN_LONGTERM: "执行细粒度长期记忆",
    PipelineStep.RUN_PROFILE: "抽取用户画像",
    PipelineStep.COMPLETE_TURN: "完成本轮",
}
_STATUS_LABELS = {
    StepStatus.PENDING.value: "pending · 等待",
    StepStatus.QUEUED.value: "queued · 已排队",
    StepStatus.RUNNING.value: "running · 执行中",
    StepStatus.SUCCEEDED.value: "succeeded · 成功",
    StepStatus.FAILED.value: "failed · 可重试",
    StepStatus.SKIPPED.value: "skipped · 系统不适用",
}
_FENCED_CODE_START = re.compile(r"^ {0,3}(?:(?P<backticks>`{3,})[^`\r\n]*|(?P<tildes>~{3,})[^\r\n]*)(?:\r?\n|$)")


@dataclass(frozen=True)
class PipelineNode:
    step: PipelineStep | str
    label: str
    status: str
    attempts: int
    duration_ms: float | None
    error: str | None
    enabled: bool
    current: bool
    held: bool = False
    detail: str | None = None
    llm_calls: tuple[dict[str, Any], ...] = ()
    tool_calls: tuple[dict[str, Any], ...] = ()
    virtual: bool = False


def build_nodes(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None,
) -> dict[PipelineStep | str, PipelineNode]:
    # Retain the argument for callers that still load legacy per-turn config;
    # current memory controls derive exclusively from persisted step holds.
    del background_config
    step_map = {PipelineStep(item["step"]): item for item in steps}
    complete_status = completion_status(steps)
    current_step = _current_step(step_map, complete_status)
    nodes = {}
    for step in VISIBLE_PIPELINE_STEPS:
        if step is PipelineStep.COMPLETE_TURN:
            item = {"status": complete_status, "attempts": 0}
        else:
            item = step_map[step]
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        llm_calls = output.get("llm_calls") if isinstance(output.get("llm_calls"), list) else []
        tool_calls = output.get("tool_calls") if isinstance(output.get("tool_calls"), list) else []
        if not tool_calls and isinstance(output.get("tool_trace"), list):
            tool_calls = output["tool_trace"]
        nodes[step] = PipelineNode(
            step=step,
            label=_STEP_LABELS[step],
            status=item["status"],
            attempts=int(item.get("attempts") or 0),
            duration_ms=item.get("duration_ms"),
            error=item.get("error_message"),
            enabled=not bool(item.get("is_held"))
            and not (step is PipelineStep.AGENTIC_RETRIEVAL and output.get("agentic_status") == "disabled"),
            current=step is current_step,
            held=bool(item.get("is_held")),
            detail=_node_detail(step, item, step_map),
            # QueryResolver runs inside RETRIEVE_CONTEXT. Its calls are shown
            # on the derived rewrite node below, never duplicated here.
            llm_calls=() if step is PipelineStep.RETRIEVE_CONTEXT else tuple(llm_calls),
            tool_calls=tuple(tool_calls),
        )
    retrieve_item = step_map[PipelineStep.RETRIEVE_CONTEXT]
    retrieve_output = (
        retrieve_item.get("output") if isinstance(retrieve_item.get("output"), dict) else {}
    )
    retrieve_llm_calls = (
        retrieve_output.get("llm_calls") if isinstance(retrieve_output.get("llm_calls"), list) else []
    )
    nodes[QUERY_REWRITE_NODE] = PipelineNode(
        step=QUERY_REWRITE_NODE,
        label=_STEP_LABELS[QUERY_REWRITE_NODE],
        status=retrieve_item["status"],
        attempts=int(retrieve_item.get("attempts") or 0),
        duration_ms=None,
        error=retrieve_item.get("error_message"),
        enabled=True,
        current=PipelineStep.RETRIEVE_CONTEXT is current_step,
        detail=_query_rewrite_detail(retrieve_item, retrieve_output),
        llm_calls=tuple(retrieve_llm_calls),
        virtual=True,
    )
    return nodes


def completion_status(steps: list[dict[str, Any]]) -> str:
    step_map = {PipelineStep(item["step"]): item for item in steps}
    if step_map[PipelineStep.GENERATE_RESPONSE]["status"] != StepStatus.SUCCEEDED.value:
        return StepStatus.PENDING.value
    statuses = [step_map[step]["status"] for step in MEMORY_STEPS]
    if all(status in TERMINAL_STEP_STATUSES for status in statuses):
        return StepStatus.SUCCEEDED.value
    if StepStatus.FAILED.value in statuses:
        return StepStatus.FAILED.value
    if StepStatus.RUNNING.value in statuses:
        return StepStatus.RUNNING.value
    if StepStatus.QUEUED.value in statuses:
        return StepStatus.QUEUED.value
    return StepStatus.PENDING.value


def progress(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None = None,
) -> tuple[int, int, float]:
    step_map = {PipelineStep(item["step"]): item for item in steps}
    completed = sum(step_map[step]["status"] in TERMINAL_STEP_STATUSES for step in PIPELINE_STEPS)
    complete = completion_status(steps) == StepStatus.SUCCEEDED.value
    total = len(PIPELINE_STEPS) + 1
    completed += int(complete)
    return completed, total, completed / total


def render_html(
    steps: list[dict[str, Any]],
    background_config: BackgroundStepConfig | Mapping[str, object] | None,
) -> str:
    nodes = build_nodes(steps, background_config)
    completed, total, _ratio = progress(steps, background_config)
    foreground = '<div class="demo-arrow">→</div>'.join(
        _node_html(nodes[step]) for step in DISPLAY_FOREGROUND_STEPS
    )
    branches = "".join(f'<div class="demo-memory-branch">{_node_html(nodes[step])}</div>' for step in MEMORY_STEPS)
    return (
        '<div class="demo-pipeline">'
        '<div class="demo-pipeline-summary">'
        f"<span>实时进度 {completed}/{total}</span>"
        "<span>四个记忆分支使用独立有序队列</span>"
        "</div>"
        '<div class="demo-pipeline-scroll">'
        '<div class="demo-flow-row">'
        f'<div class="demo-foreground-chain">{foreground}</div>'
        f'<div class="demo-parallel-arrow" aria-hidden="true">{_render_parallel_arrow_svg()}</div>'
        f'<div class="demo-fork" aria-hidden="true">{_render_fork_svg()}</div>'
        f'<div class="demo-memory-column">{branches}</div>'
        f'<div class="demo-merge" aria-hidden="true">{_render_merge_svg()}</div>'
        f'<div class="demo-complete-node">{_node_html(nodes[PipelineStep.COMPLETE_TURN])}</div>'
        "</div>"
        "</div>"
        "</div>"
    )


def _render_parallel_arrow_svg() -> str:
    return (
        '<svg class="demo-parallel-arrow-svg" width="100%" height="100%" viewBox="0 0 20 12" '
        'preserveAspectRatio="none">'
        '<defs><marker id="demo-parallel-arrowhead" viewBox="0 0 6 6" refX="5" refY="3" '
        'markerWidth="5" markerHeight="5" orient="auto" markerUnits="strokeWidth">'
        '<path d="M 0 0 L 6 3 L 0 6 z" class="demo-connector-arrowhead" /></marker></defs>'
        '<line x1="0" y1="6" x2="18" y2="6" marker-end="url(#demo-parallel-arrowhead)" />'
        "</svg>"
    )


def _render_fork_svg() -> str:
    arms = "".join(
        f'<line class="demo-connector-arm demo-fork-output" x1="50" y1="{center}" '
        f'x2="96" y2="{center}" marker-end="url(#demo-fork-arrow)" />'
        for center in (12.5, 37.5, 62.5, 87.5)
    )
    return (
        '<svg class="demo-connector-svg" width="100%" height="100%" viewBox="0 0 100 100" '
        'preserveAspectRatio="none">'
        '<defs><marker id="demo-fork-arrow" viewBox="0 0 6 6" refX="5" refY="3" '
        'markerWidth="5" markerHeight="5" orient="auto" markerUnits="strokeWidth">'
        '<path d="M 0 0 L 6 3 L 0 6 z" class="demo-connector-arrowhead" /></marker></defs>'
        '<line class="demo-connector-input" x1="0" y1="50" x2="50" y2="50" />'
        '<line class="demo-connector-trunk" x1="50" y1="12.5" x2="50" y2="87.5" />'
        f"{arms}</svg>"
    )


def _render_merge_svg() -> str:
    arms = "".join(
        f'<line class="demo-connector-arm demo-merge-input" x1="0" y1="{center}" x2="50" y2="{center}" />'
        for center in (12.5, 37.5, 62.5, 87.5)
    )
    return (
        '<svg class="demo-connector-svg" width="100%" height="100%" viewBox="0 0 100 100" '
        'preserveAspectRatio="none">'
        '<defs><marker id="demo-merge-arrow" viewBox="0 0 6 6" refX="5" refY="3" '
        'markerWidth="5" markerHeight="5" orient="auto" markerUnits="strokeWidth">'
        '<path d="M 0 0 L 6 3 L 0 6 z" class="demo-connector-arrowhead" /></marker></defs>'
        f"{arms}"
        '<line class="demo-connector-trunk" x1="50" y1="12.5" x2="50" y2="87.5" />'
        '<line class="demo-connector-output" x1="50" y1="50" x2="96" y2="50" '
        'marker-end="url(#demo-merge-arrow)" />'
        "</svg>"
    )


def _node_html(node: PipelineNode) -> str:
    classes = ["demo-node", node.status]
    if node.virtual:
        classes.append("virtual")
    if node.step is PipelineStep.AGENTIC_RETRIEVAL:
        classes.append("agentic-retrieval")
    if node.current:
        classes.append("current")
    if not node.enabled:
        classes.append("disabled")
    if node.held:
        classes.append("held")
    duration = f" · {node.duration_ms:.0f} ms" if node.duration_ms is not None else ""
    tooltip = ' title="四个记忆步骤全部完成后，本轮自动完成"' if node.step is PipelineStep.COMPLETE_TURN else ""
    error = ""
    if node.error:
        short_error = node.error[:90] + ("…" if len(node.error) > 90 else "")
        error = f'<div class="demo-node-error" title="{html.escape(node.error)}">{html.escape(short_error)}</div>'
    hide_repeated_detail = node.step is PipelineStep.AGENTIC_RETRIEVAL and node.detail == "无需补充"
    detail = (
        f'<div class="demo-node-detail">{html.escape(node.detail)}</div>'
        if node.detail and not hide_repeated_detail
        else ""
    )
    if node.step is PipelineStep.COMPLETE_TURN:
        attempts = ""
    elif node.virtual:
        attempts = "展示派生"
    else:
        attempts = f"{node.attempts} 次{duration}"
    show_agentic_call_stats = (
        node.step is PipelineStep.AGENTIC_RETRIEVAL
        and node.status in {StepStatus.SUCCEEDED.value, StepStatus.FAILED.value}
        and node.attempts > 0
        and node.enabled
        and node.detail != "历史兼容（原流程无此步骤）"
    )
    if node.step is PipelineStep.AGENTIC_RETRIEVAL:
        show_llm_badge = show_agentic_call_stats
        show_tool_badge = show_agentic_call_stats
    else:
        show_llm_badge = bool(node.llm_calls)
        show_tool_badge = bool(node.tool_calls)
    badges = []
    if show_llm_badge:
        badges.append(f'<span class="demo-node-badge">LLM ×{len(node.llm_calls)}</span>')
    if show_tool_badge:
        badges.append(f'<span class="demo-node-badge">工具 ×{len(node.tool_calls)}</span>')
    badge_html = f'<div class="demo-node-badges">{"".join(badges)}</div>' if badges else ""
    popover = _trace_popover_html(node)
    return (
        f'<div class="{" ".join(classes)}"{tooltip}>'
        f'<div class="demo-node-title">{html.escape(node.label)}</div>'
        f'<div class="demo-node-status">{html.escape(_node_status_label(node))}</div>'
        f'<div class="demo-node-meta">{attempts}</div>'
        f"{badge_html}{detail}{error}{popover}</div>"
    )


def _trace_popover_html(node: PipelineNode) -> str:
    if not node.llm_calls:
        return ""
    sections = [f'<div class="demo-popover-title">{html.escape(node.label)}调用详情</div>']
    for fallback_sequence, call in enumerate(node.llm_calls, start=1):
        sequence = int(call.get("sequence") or fallback_sequence)
        purpose = str(call.get("purpose") or node.label)
        status = str(call.get("status") or "unknown")
        duration = call.get("duration_ms")
        duration_label = f"{float(duration):.0f} ms" if isinstance(duration, (int, float)) else "耗时未知"
        meta = f"{status} · {duration_label}"
        if call.get("error_message"):
            meta += f" · 错误：{call['error_message']}"
        sections.append(
            '<section class="demo-popover-call">'
            f'<div class="demo-popover-call-title">模型调用 {sequence} · {html.escape(purpose)}</div>'
            f'<div class="demo-popover-call-meta">{html.escape(meta)}</div>'
            f"{_prompt_html(call.get('messages'))}"
            f"{_answer_html(format_model_answer(call))}"
            "</section>"
        )
    return '<div class="demo-node-popover" role="tooltip">' + "".join(sections) + "</div>"


def _prompt_html(messages: Any) -> str:
    message_sections = "".join(
        '<div class="demo-prompt-message">'
        f'<div class="demo-prompt-role">{html.escape(message.role)}</div>'
        f"{_render_call_text_html(message.content)}"
        "</div>"
        for message in format_prompt_messages(messages)
    )
    return (
        '<div class="demo-call-block demo-call-prompt">'
        '<div class="demo-call-heading">Prompt</div>'
        f"{message_sections}</div>"
    )


def _answer_html(answer: str) -> str:
    return (
        '<div class="demo-call-block demo-call-answer">'
        '<div class="demo-call-heading">模型回答</div>'
        f"{_render_call_text_html(answer)}"
        "</div>"
    )


def _render_call_text_html(content: str) -> str:
    if is_json_document(content):
        return _json_text_html(content)
    return "".join(
        _json_text_html(part.content) if part.is_json else _markdown_text_html(part.content)
        for part in split_mixed_text_and_json(content)
    )


def _json_text_html(content: str) -> str:
    """Render already-formatted JSON as inert code without changing trace data."""
    formatted = format_json_document(content)
    json_content = formatted if formatted is not None else content
    return f'<div class="demo-markdown-text demo-json-block">\n\n```json\n{json_content}\n```\n\n</div>'


def _markdown_text_html(content: str) -> str:
    """Embed untrusted text in the popover while letting Streamlit parse Markdown."""
    # Blank lines end the surrounding raw-HTML block, so Streamlit parses the
    # body as Markdown. Raw HTML is escaped only outside Markdown code spans;
    # escaping fenced code first would make its entities visible after the code
    # renderer performs its own mandatory escaping.
    safe_content = _escape_markdown_html(content)
    return f'<div class="demo-markdown-text">\n\n{safe_content}\n\n</div>'


def _escape_markdown_html(content: str) -> str:
    """Escape raw HTML without double-encoding Markdown code spans."""
    escaped_parts = []
    markdown_buffer = []
    closing_fence = None

    def flush_markdown_buffer() -> None:
        if markdown_buffer:
            escaped_parts.append(_escape_html_outside_inline_code("".join(markdown_buffer)))
            markdown_buffer.clear()

    for line in content.splitlines(keepends=True):
        if closing_fence is not None:
            escaped_parts.append(line)
            if closing_fence.fullmatch(line):
                closing_fence = None
            continue

        match = _FENCED_CODE_START.match(line)
        if match is None:
            markdown_buffer.append(line)
            continue

        flush_markdown_buffer()
        fence = match.group("backticks") or match.group("tildes")
        closing_fence = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*(?:\r?\n|$)")
        escaped_parts.append(line)

    flush_markdown_buffer()
    return "".join(escaped_parts)


def _escape_html_outside_inline_code(content: str) -> str:
    escaped_parts = []
    cursor = 0
    plain_start = 0

    while cursor < len(content):
        if content[cursor] != "`":
            cursor += 1
            continue
        if _is_backslash_escaped(content, cursor):
            cursor += 1
            continue

        opener_end = cursor + 1
        while opener_end < len(content) and content[opener_end] == "`":
            opener_end += 1
        run_length = opener_end - cursor
        closer_start = _find_backtick_closer(content, opener_end, run_length)
        if closer_start is None:
            cursor = opener_end
            continue

        escaped_parts.append(html.escape(content[plain_start:cursor], quote=False))
        closer_end = closer_start + run_length
        escaped_parts.append(content[cursor:closer_end])
        cursor = closer_end
        plain_start = closer_end

    escaped_parts.append(html.escape(content[plain_start:], quote=False))
    return "".join(escaped_parts)


def _find_backtick_closer(content: str, cursor: int, run_length: int) -> int | None:
    while cursor < len(content):
        candidate = content.find("`", cursor)
        if candidate < 0:
            return None
        candidate_end = candidate + 1
        while candidate_end < len(content) and content[candidate_end] == "`":
            candidate_end += 1
        if candidate_end - candidate == run_length:
            return candidate
        cursor = candidate_end
    return None


def _is_backslash_escaped(content: str, cursor: int) -> bool:
    backslashes = 0
    cursor -= 1
    while cursor >= 0 and content[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _node_detail(
    step: PipelineStep,
    item: dict[str, Any],
    step_map: dict[PipelineStep, dict[str, Any]],
) -> str | None:
    if step is PipelineStep.COMPLETE_TURN:
        return None
    if step is PipelineStep.RUN_SHORTTERM and item["status"] == StepStatus.SUCCEEDED.value:
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        created = output.get("memory_add_created") if isinstance(output.get("memory_add_created"), dict) else {}
        if created:
            extraction_count = len(created.get("longterm_extraction_job_ids") or [])
            return (
                "ShortTerm + Migration + "
                f"LongTerm Extraction×{extraction_count} + Profile"
            )
    if step is PipelineStep.AGENTIC_RETRIEVAL:
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        agentic_status = output.get("agentic_status")
        if item["status"] == StepStatus.FAILED.value or agentic_status == "failed":
            return "执行失败"
        if agentic_status in {"degraded", "failed_degraded"}:
            return "降级"
        if agentic_status == "disabled":
            return "未启用"
        if agentic_status == "not_needed":
            return "无需补充"
        if agentic_status in {"supplemented", "retrieved"}:
            return "已补充"
        if agentic_status == "no_relevant_memory":
            return "未检索到相关记忆"
        if agentic_status == "legacy_compatible":
            return "历史兼容（原流程无此步骤）"
    if item["status"] == StepStatus.PENDING.value and item.get("is_held"):
        return "重新勾选后继续执行"
    if item["status"] == StepStatus.FAILED.value:
        return "选择该失败节点可单独重试"
    if step in MEMORY_STEPS[1:] and item["status"] == StepStatus.PENDING.value:
        shortterm_status = step_map[PipelineStep.RUN_SHORTTERM]["status"]
        if shortterm_status == StepStatus.FAILED.value:
            return "短期提交失败，等待重试"
        if shortterm_status != StepStatus.SUCCEEDED.value:
            return "等待短期记忆完成"
    return None


def _query_rewrite_detail(item: dict[str, Any], output: dict[str, Any]) -> str:
    if "retrieval_query" not in output:
        if item["status"] in {
            StepStatus.PENDING.value,
            StepStatus.QUEUED.value,
            StepStatus.RUNNING.value,
        }:
            return "等待分层检索结果"
        return "历史数据未记录检索问题"
    query = str(output.get("query") or "")
    retrieval_query = str(output.get("retrieval_query") or query)
    return "已改写" if retrieval_query != query else "未发生改写"


def _node_status_label(node: PipelineNode) -> str:
    if node.step is PipelineStep.AGENTIC_RETRIEVAL and node.detail in {
        "未启用",
        "无需补充",
        "已补充",
        "未检索到相关记忆",
        "降级",
        "执行失败",
    }:
        return f"{node.status} · {node.detail}"
    if node.status == StepStatus.PENDING.value and node.held:
        return "pending · 已阻塞"
    if node.status == StepStatus.PENDING.value and node.step in MEMORY_STEPS[1:] and node.detail:
        return "pending · 等待依赖"
    return _STATUS_LABELS.get(node.status, node.status)


def _current_step(
    step_map: dict[PipelineStep, dict[str, Any]],
    complete_status: str,
) -> PipelineStep | None:
    for status in (
        StepStatus.RUNNING.value,
        StepStatus.QUEUED.value,
        StepStatus.FAILED.value,
        StepStatus.PENDING.value,
    ):
        match = next((step for step in PIPELINE_STEPS if step_map[step]["status"] == status), None)
        if match is not None:
            return match
    return PipelineStep.COMPLETE_TURN if complete_status == StepStatus.SUCCEEDED.value else None
