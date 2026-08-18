from __future__ import annotations

from typing import Any, Mapping

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT
from mem0.memory.midterm_updater import MidTermUpdater


_SUMMARY_HEADING = "## summary 要求"

CONSERVATIVE_ADD_INSTRUCTIONS = """## 保守写入补充要求

该 Page 将用于后续记忆检索。保持原有结构，只补充保留当前轮真实出现且有独立检索价值的主体、时间、指标、关系、判断以及对前文的修订关系。不要加入对话中没有的信息，也不要为了变短删除仍能区分本轮的信息。"""

CONTEXT_AWARE_ADD_INSTRUCTIONS = """## 上下文感知写入补充要求

输入会包含当前轮之前当时可见的最近对话。上下文只用于消解当前轮的指代、承接、比较、反证或修订关系；摘要主体仍是当前待总结对话。禁止把上文的独立事实写成当前轮事实，禁止生成多轮综合摘要。"""

EVIDENCE_FOCUSED_SUMMARY_INSTRUCTIONS = """## 检索证据保留要求

在当前轮真实出现时，优先保留任务、实体、时间范围、关键指标、证据、比较关系、结论与限制。摘要应提供多个准确检索入口，但不得机械堆砌数字或引入未出现的信息。"""


def _insert_instructions(production: str, instructions: str) -> str:
    if production.count(_SUMMARY_HEADING) != 1:
        raise ValueError("Production Page summary Prompt has no unique summary heading")
    return production.replace(_SUMMARY_HEADING, f"{instructions}\n\n{_SUMMARY_HEADING}")


def controlled_page_prompt_variants(
    diagnostic: Mapping[str, Any],
    failure_summary: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    regime = str(diagnostic.get("regime") or "balanced_or_plateau")
    failure_profile = ", ".join(f"{key}={value}" for key, value in sorted((failure_summary or {}).items())) or "none"
    diagnostic_instruction = (
        "本次 Tune 诊断显示深层候选覆盖不足。保持原格式，增强当前轮中能够定位 Page 的实体、指标、时间、关系和证据限制；禁止补写 Gold、未来轮次或对话中未出现的信息。"
        if regime == "candidate_coverage_bottleneck"
        else "本次 Tune 诊断显示 Session 间表现不稳定。保持原格式，优先保留能区分当前轮与邻近 Page 的任务、关系和状态，避免重复公共背景。"
    )
    return {
        "conservative_add": {
            "prompt": _insert_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, CONSERVATIVE_ADD_INSTRUCTIONS),
            "context_mode": "none",
            "kind": "memory_write",
        },
        "context_aware_add": {
            "prompt": _insert_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, CONTEXT_AWARE_ADD_INSTRUCTIONS),
            "context_mode": "previous_visible",
            "kind": "memory_write",
        },
        "evidence_focused_summary": {
            "prompt": _insert_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, EVIDENCE_FOCUSED_SUMMARY_INSTRUCTIONS),
            "context_mode": "none",
            "kind": "page_summary",
        },
        "diagnostic_controlled_summary": {
            "prompt": _insert_instructions(
                MIDTERM_PAGE_SUMMARY_PROMPT,
                (
                    "## 当前 Tune 诊断驱动的受控补充要求\n\n"
                    f"{diagnostic_instruction}\n\n"
                    f"Tune requirement 的无内容聚合失败特征：{failure_profile}。"
                ),
            ),
            "context_mode": "none",
            "kind": "page_summary",
        },
    }


class ContextAwareMidTermUpdater(MidTermUpdater):
    """Tuner-only wrapper that augments summary input without changing Page payloads."""

    def __init__(self, *args: Any, context_by_dialogue: Mapping[tuple[str, str], str], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._context_by_dialogue = dict(context_by_dialogue)

    def _summarize_page(
        self,
        user_input: str,
        assistant_response: str,
        *,
        allow_fallback: bool = True,
    ) -> tuple[str, list[str]]:
        context = self._context_by_dialogue.get((str(user_input), str(assistant_response)), "")
        if not context:
            return super()._summarize_page(user_input, assistant_response, allow_fallback=allow_fallback)
        augmented = f"上文（只用于理解当前轮）：\n{context}\n\n当前待总结对话中的用户：\n{user_input}"
        return super()._summarize_page(augmented, assistant_response, allow_fallback=allow_fallback)


def visible_context_by_dialogue(turns: list[Any], *, qa_window: int) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    history: list[Any] = []
    for turn in turns:
        previous = history[-max(1, qa_window) :]
        result[(str(turn.question), str(turn.answer))] = "\n\n".join(
            f"用户：{item.question}\n助手：{item.answer}" for item in previous
        )
        history.append(turn)
    return result
