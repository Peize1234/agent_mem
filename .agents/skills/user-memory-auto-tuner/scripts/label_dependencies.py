from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Protocol

from auto_tuner_models import (
    AnnotatedSession,
    AnnotatedTurn,
    AnnotationResult,
    DependencyLabel,
    ExtractedHistory,
    VerificationVerdict,
)

_BENCHMARK_ID = re.compile(r"S\d{3}-Q\d{3}")

_LABELER_SYSTEM_PROMPT = """你是历史依赖 Benchmark Labeler。只判断回答当前问题真正需要的历史信息。
规则：
1. 只能引用输入中列出的 earlier turns，禁止引用当前或未来 turn；
2. 主题相关不等于必要，当前问题和常识足以回答时 needs_history=false；
3. requirements 之间是 AND；同一 requirement 中多个 dependency_ids 仅表示等价来源 OR；
4. required_contexts 与 dependency_ids 一一对应，必须是对应历史 QA 原文中的最小逐字证据；
5. 多个历史 turn 只有提供等价信息时才能放进同一 OR requirement；
6. 当前回答中新出现、历史中不存在的信息不能作为 Gold。
只返回 JSON：
{"needs_history": bool, "requirements": [{"dependency_ids": [str], "required_contexts": [str]}],
 "dependency_type": str, "confidence": 0.0-1.0}
"""

_VERIFIER_SYSTEM_PROMPT = """你是独立的历史依赖 Verifier。审查候选标签，不要补造 Gold。
逐项检查：依赖 ID 存在且早于当前问题；每段 required context 有历史原文依据；依赖确实是回答所必需；
多个 requirements 的 AND 与单个 requirement 内等价来源的 OR 是否合理。任何一项不确定都应判 invalid。
只返回 JSON：
{"valid": bool, "dependencies_necessary": bool, "context_supported": bool, "logic_valid": bool,
 "confidence": 0.0-1.0, "issues": [str]}
"""


class JsonLLM(Protocol):
    def generate_response(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any],
    ) -> Any: ...


def _json_object(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    text = str(response or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("LLM response does not contain a JSON object")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("LLM response JSON root must be an object")
    return value


def _history_payload(earlier_turns: list[dict[str, str]]) -> str:
    return json.dumps(earlier_turns, ensure_ascii=False, indent=2)


class TwoStageDependencyAnnotator:
    def __init__(
        self,
        llm: JsonLLM,
        *,
        label_min_confidence: float = 0.75,
        verifier_min_confidence: float = 0.75,
    ):
        self.llm = llm
        self.label_min_confidence = float(label_min_confidence)
        self.verifier_min_confidence = float(verifier_min_confidence)

    def label(
        self,
        *,
        benchmark_id: str,
        question: str,
        answer: str,
        earlier_turns: list[dict[str, str]],
    ) -> DependencyLabel:
        prompt = {
            "current": {"id": benchmark_id, "question": question, "answer": answer},
            "earlier_turns": earlier_turns,
        }
        response = self.llm.generate_response(
            messages=[
                {"role": "system", "content": _LABELER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
            ],
            response_format={"type": "json_object"},
        )
        return DependencyLabel.model_validate(_json_object(response))

    def verify(
        self,
        *,
        benchmark_id: str,
        question: str,
        answer: str,
        earlier_turns: list[dict[str, str]],
        label: DependencyLabel,
    ) -> VerificationVerdict:
        prompt = {
            "current": {"id": benchmark_id, "question": question, "answer": answer},
            "earlier_turns": earlier_turns,
            "candidate_label": label.model_dump(mode="json"),
        }
        response = self.llm.generate_response(
            messages=[
                {"role": "system", "content": _VERIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, indent=2)},
            ],
            response_format={"type": "json_object"},
        )
        return VerificationVerdict.model_validate(_json_object(response))


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", normalized)


def deterministic_validation(
    *,
    label: DependencyLabel,
    verifier: VerificationVerdict,
    earlier_turns: dict[str, dict[str, str]],
    label_min_confidence: float,
    verifier_min_confidence: float,
) -> tuple[str, tuple[str, ...]]:
    issues: list[str] = []
    if label.confidence < label_min_confidence:
        issues.append("LABEL_LOW_CONFIDENCE")
    if verifier.confidence < verifier_min_confidence:
        issues.append("VERIFIER_LOW_CONFIDENCE")
    if not verifier.valid:
        issues.append("VERIFIER_REJECTED")
    if label.needs_history and not verifier.dependencies_necessary:
        issues.append("DEPENDENCY_NOT_NECESSARY")
    if not verifier.context_supported:
        issues.append("CONTEXT_NOT_SUPPORTED")
    if not verifier.logic_valid:
        issues.append("INVALID_AND_OR_LOGIC")
    issues.extend(str(issue) for issue in verifier.issues if str(issue).strip())

    if not label.needs_history:
        if label.requirements:
            issues.append("INDEPENDENT_LABEL_HAS_DEPENDENCIES")
        return ("INVALID" if issues else "INDEPENDENT", tuple(dict.fromkeys(issues)))
    if not label.requirements:
        issues.append("MISSING_DEPENDENCY")

    for requirement in label.requirements:
        if len(requirement.dependency_ids) != len(requirement.required_contexts):
            issues.append("DEPENDENCY_CONTEXT_ARITY_MISMATCH")
            continue
        if len(set(requirement.dependency_ids)) != len(requirement.dependency_ids):
            issues.append("DUPLICATE_OR_MEMBER")
        for dependency_id, required_context in zip(
            requirement.dependency_ids,
            requirement.required_contexts,
            strict=True,
        ):
            if not _BENCHMARK_ID.fullmatch(dependency_id):
                issues.append(f"INVALID_DEPENDENCY_ID:{dependency_id}")
                continue
            historical = earlier_turns.get(dependency_id)
            if historical is None:
                issues.append(f"MISSING_OR_FUTURE_DEPENDENCY:{dependency_id}")
                continue
            context = _normalized_text(required_context)
            source = _normalized_text(f"{historical['question']}\n{historical['answer']}")
            if not context:
                issues.append(f"EMPTY_REQUIRED_CONTEXT:{dependency_id}")
            elif context not in source:
                issues.append(f"REQUIRED_CONTEXT_NOT_VERBATIM:{dependency_id}")
    return ("INVALID" if issues else "VALID", tuple(dict.fromkeys(issues)))


def annotate_history(
    history: ExtractedHistory,
    annotator: TwoStageDependencyAnnotator,
    *,
    output_dir: str | Path | None = None,
) -> AnnotationResult:
    annotated_sessions: list[AnnotatedSession] = []
    for session_number, session in enumerate(history.sessions, start=1):
        session_id = f"S{session_number:03d}"
        earlier: list[dict[str, str]] = []
        annotated_turns: list[AnnotatedTurn] = []
        for turn_number, turn in enumerate(session.turns, start=1):
            benchmark_id = f"{session_id}-Q{turn_number:03d}"
            if not earlier:
                label = DependencyLabel(needs_history=False, confidence=1.0)
                verifier = None
                status = "INDEPENDENT"
                issues: tuple[str, ...] = ()
            else:
                try:
                    label = annotator.label(
                        benchmark_id=benchmark_id,
                        question=turn.question,
                        answer=turn.answer,
                        earlier_turns=earlier,
                    )
                    verifier = annotator.verify(
                        benchmark_id=benchmark_id,
                        question=turn.question,
                        answer=turn.answer,
                        earlier_turns=earlier,
                        label=label,
                    )
                    status, issues = deterministic_validation(
                        label=label,
                        verifier=verifier,
                        earlier_turns={item["id"]: item for item in earlier},
                        label_min_confidence=annotator.label_min_confidence,
                        verifier_min_confidence=annotator.verifier_min_confidence,
                    )
                except Exception as exc:
                    label = DependencyLabel(needs_history=False, confidence=0.0)
                    verifier = None
                    status = "INVALID"
                    issues = (f"ANNOTATION_ERROR:{type(exc).__name__}:{exc}",)
            annotated_turns.append(
                AnnotatedTurn(
                    benchmark_id=benchmark_id,
                    source_turn_index=turn.source_turn_index,
                    question=turn.question,
                    answer=turn.answer,
                    label=label,
                    verifier=verifier,
                    status=status,
                    validation_issues=issues,
                )
            )
            earlier.append(
                {
                    "id": benchmark_id,
                    "question": turn.question,
                    "answer": turn.answer,
                }
            )
        annotated_sessions.append(
            AnnotatedSession(
                benchmark_session_id=session_id,
                source_session_scope=session.session_scope,
                user_id=session.user_id,
                run_id=session.run_id,
                agent_id=session.agent_id,
                turns=tuple(annotated_turns),
            )
        )
    result = AnnotationResult(user_id=history.user_id, sessions=tuple(annotated_sessions))
    if output_dir is not None:
        destination = Path(output_dir).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "dependency_labels.json").write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return result
