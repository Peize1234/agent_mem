from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from mem0.configs.midterm_prompts import MIDTERM_PAGE_SUMMARY_PROMPT, MIDTERM_SESSION_MERGE_PROMPT
from mem0.configs.prompts import ADDITIVE_EXTRACTION_PROMPT
from mem0.memory.midterm_updater import PRODUCTION_PAGE_CONTEXT_CONTRACT

_SUMMARY_HEADING = "## summary 要求"

CONSERVATIVE_ADD_INSTRUCTIONS = """## 保守写入补充要求

该 Page 将用于后续记忆检索。保持原有结构，只补充保留当前轮真实出现且有独立检索价值的主体、时间、指标、关系、判断以及对前文的修订关系。不要加入对话中没有的信息，也不要为了变短删除仍能区分本轮的信息。"""

CONTEXT_AWARE_ADD_INSTRUCTIONS = """## 上下文感知写入补充要求

Production 输入固定包含前序 Page raw_dialogue、当前 QA 和按 source turn_index 截取的后续 QA。前后文只用于消解当前轮的指代、承接、比较、反证或修订关系；摘要主体仍是当前待总结 QA。禁止把前后文的独立事实写成当前轮事实，尤其禁止把后续才出现的新事实、数字、任务或结论归因到当前轮，禁止生成多轮综合摘要。"""

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
            "context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
            "kind": "memory_write",
        },
        "context_aware_add": {
            "prompt": _insert_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, CONTEXT_AWARE_ADD_INSTRUCTIONS),
            "context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
            "kind": "memory_write",
        },
        "evidence_focused_summary": {
            "prompt": _insert_instructions(MIDTERM_PAGE_SUMMARY_PROMPT, EVIDENCE_FOCUSED_SUMMARY_INSTRUCTIONS),
            "context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
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
            "context_contract": PRODUCTION_PAGE_CONTEXT_CONTRACT,
            "kind": "page_summary",
        },
    }


def controlled_session_merge_prompt_variants() -> dict[str, str]:
    """Preserve the production schema while refining Session representation."""
    return {
        "session_merge_evidence_preserving": (
            MIDTERM_SESSION_MERGE_PROMPT
            + "\n\n受控 Tune 变体：合并时保留能区分各 Page 的主体、期间、指标、修订关系和证据限制；"
            "不得引入输入 Page 中不存在的事实，输出格式保持不变。"
        ),
        "session_merge_deduplicated": (
            MIDTERM_SESSION_MERGE_PROMPT
            + "\n\n受控 Tune 变体：去除重复公共背景，保留各 Page 新增或修订的信息及准确检索词；"
            "不得丢失数值、单位、日期和实体，输出格式保持不变。"
        ),
    }


def controlled_fine_grained_longterm_prompt_variants() -> dict[str, str]:
    """Refine per-QA fine-grained Long-term extraction without changing its contract."""
    return {
        "fine_grained_longterm_fact_preserving": (
            ADDITIVE_EXTRACTION_PROMPT
            + "\n\n受控 Tune 变体：仅从当前完整 QA 提取可长期复用的细粒度事实，保留实体、日期、数值、"
            "单位和适用条件；不得复制无关背景或推断未来信息。"
        ),
        "fine_grained_longterm_conservative": (
            ADDITIVE_EXTRACTION_PROMPT
            + "\n\n受控 Tune 变体：优先精确、可归因且跨后续问题仍有用的信息；存在冲突或证据不足时不写入，"
            "输出格式保持不变。"
        ),
    }


def controlled_session_longterm_prompt_variants() -> dict[str, str]:
    """Compatibility alias for historical registries/artifacts."""
    return controlled_fine_grained_longterm_prompt_variants()


class PromptOverrideLLM:
    """Instance-scoped Prompt replacement for isolated tuner source runs."""

    def __init__(
        self,
        delegate: Any,
        *,
        page_summary_prompt: str | None = None,
        session_merge_prompt: str | None = None,
        fine_grained_longterm_extraction_prompt: str | None = None,
        session_longterm_extraction_prompt: str | None = None,
    ):
        self.delegate = delegate
        self._page_summary_prompt = page_summary_prompt
        self._session_merge_prompt = session_merge_prompt
        self._fine_grained_longterm_extraction_prompt = (
            fine_grained_longterm_extraction_prompt or session_longterm_extraction_prompt
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    def _messages(self, messages: Any) -> Any:
        if not isinstance(messages, list):
            return messages
        updated = deepcopy(messages)
        for message in updated:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            content = str(message.get("content") or "")
            if self._page_summary_prompt and content == MIDTERM_PAGE_SUMMARY_PROMPT:
                message["content"] = self._page_summary_prompt
            elif self._session_merge_prompt and content == MIDTERM_SESSION_MERGE_PROMPT:
                message["content"] = self._session_merge_prompt
            elif self._fine_grained_longterm_extraction_prompt and content.startswith(ADDITIVE_EXTRACTION_PROMPT):
                message["content"] = self._fine_grained_longterm_extraction_prompt + content[len(ADDITIVE_EXTRACTION_PROMPT) :]
        return updated

    def generate_response(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            args = (self._messages(args[0]), *args[1:])
        elif "messages" in kwargs:
            kwargs = {**kwargs, "messages": self._messages(kwargs["messages"])}
        return self.delegate.generate_response(*args, **kwargs)

    async def generate_response_async(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            args = (self._messages(args[0]), *args[1:])
        elif "messages" in kwargs:
            kwargs = {**kwargs, "messages": self._messages(kwargs["messages"])}
        method = getattr(self.delegate, "generate_response_async", None)
        if method is not None:
            return await method(*args, **kwargs)
        return self.delegate.generate_response(*args, **kwargs)

    async def agenerate_response(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            args = (self._messages(args[0]), *args[1:])
        elif "messages" in kwargs:
            kwargs = {**kwargs, "messages": self._messages(kwargs["messages"])}
        method = getattr(self.delegate, "agenerate_response", None)
        if method is not None:
            return await method(*args, **kwargs)
        return await self.generate_response_async(*args, **kwargs)
