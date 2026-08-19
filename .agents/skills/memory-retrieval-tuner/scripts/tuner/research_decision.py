from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .artifact_registry import ArtifactRegistry
from .benchmark_support import redact_secrets
from .io_utils import append_jsonl, stable_hash
from .research_evidence import ResearchEvidence
from .research_policy import LegalAction, legal_actions_hash

RESEARCH_DECISION_SCHEMA = "research_branch_decision_v1"
RESEARCH_SYSTEM_PROMPT = """You are the research decision layer for a Memory Retrieval tuner.
Choose only from the Python-provided legal_actions. Return one strict JSON object with this schema:
{"action_ids":["A01"],"rationale":"evidence-based reason","deprioritized":[{"action_id":"A02","reason":"temporary reason"}]}

Rules:
- action_ids and deprioritized.action_id must be IDs present in legal_actions.
- Never invent a Branch, parameter, prompt, model, or action ID.
- Never modify the search space, budgets, max rounds, resource gates, or production code.
- A stop action must be selected alone. Select at most max_actions branch actions.
- Every required_now action must be selected.
- Every legal branch action that is not selected must appear in deprioritized with a concrete research reason.
- A stop action does not require a deprioritized entry and must not be used to skip a required branch reason.
- Selected actions cannot also be deprioritized.
- DEPRIORITIZED is temporary; do not claim a Branch is EXHAUSTED or BLOCKED.
- Use only the supplied Tune evidence. Held-out Validation, Gold dependencies, benchmark answers, and future turns are unavailable and must not be inferred.
- Prefer the smallest experiment set that best distinguishes the diagnosed failure regime.
"""


class ResearchDecisionValidationError(ValueError):
    pass


@dataclass
class ResearchDecisionStats:
    llm_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    decisions: int = 0
    fallback_decisions: int = 0
    cache_hits: int = 0

    def serializable(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ResearchDecisionResult:
    decision_id: str
    stage_index: int
    selected_actions: tuple[LegalAction, ...]
    deprioritized: tuple[dict[str, Any], ...]
    rationale: str
    status: str
    fallback_used: bool
    cache_hit: bool
    attempt_count: int
    deterministic_plan: tuple[dict[str, Any], ...]
    evidence_hash: str

    @property
    def stop_action(self) -> LegalAction | None:
        return next((action for action in self.selected_actions if action.action_type == "stop"), None)

    def serializable(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "stage_index": self.stage_index,
            "selected_actions": [action.serializable() for action in self.selected_actions],
            "deprioritized": list(self.deprioritized),
            "rationale": self.rationale,
            "status": self.status,
            "fallback_used": self.fallback_used,
            "cache_hit": self.cache_hit,
            "attempt_count": self.attempt_count,
            "deterministic_plan": list(self.deterministic_plan),
            "evidence_hash": self.evidence_hash,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_text(value: Any, secrets: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        return redact_secrets({str(key): _redact_text(item, secrets) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return [_redact_text(item, secrets) for item in value]
    if not isinstance(value, str):
        return redact_secrets(value)
    text = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "***REDACTED***")
    return re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*([:=])\s*([^\s,}\]]+)",
        r"\1\2***REDACTED***",
        text,
    )


def _parse_response(raw_response: Any) -> dict[str, Any]:
    if isinstance(raw_response, Mapping):
        value = dict(raw_response)
    elif isinstance(raw_response, str) and raw_response.strip():
        try:
            value = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise ResearchDecisionValidationError(f"invalid JSON: {exc}") from exc
    else:
        raise ResearchDecisionValidationError("empty response")
    if not isinstance(value, dict):
        raise ResearchDecisionValidationError("response root must be a JSON object")
    allowed_keys = {"action_ids", "rationale", "deprioritized"}
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        raise ResearchDecisionValidationError(f"unknown response fields: {unknown}")
    return value


def _validate_response(
    parsed: Mapping[str, Any],
    *,
    legal_actions: Sequence[LegalAction],
    max_actions: int,
    require_complete_deprioritized: bool = True,
) -> tuple[list[LegalAction], list[dict[str, str]], str]:
    action_by_id = {action.action_id: action for action in legal_actions}
    raw_ids = parsed.get("action_ids")
    if not isinstance(raw_ids, list) or not raw_ids or not all(isinstance(item, str) for item in raw_ids):
        raise ResearchDecisionValidationError("action_ids must be a non-empty list of strings")
    if len(set(raw_ids)) != len(raw_ids):
        raise ResearchDecisionValidationError("action_ids must not contain duplicates")
    unknown = [action_id for action_id in raw_ids if action_id not in action_by_id]
    if unknown:
        raise ResearchDecisionValidationError(f"unknown or illegal action_id: {unknown}")
    selected = [action_by_id[action_id] for action_id in raw_ids]
    stop_actions = [action for action in selected if action.action_type == "stop"]
    if stop_actions and len(selected) != 1:
        raise ResearchDecisionValidationError("a stop action must be selected alone")
    branch_actions = [action for action in selected if action.action_type == "branch"]
    if len(branch_actions) > max_actions:
        raise ResearchDecisionValidationError(f"selected {len(branch_actions)} actions, max_actions={max_actions}")
    required_ids = {action.action_id for action in legal_actions if action.required_now}
    if not required_ids.issubset(set(raw_ids)):
        raise ResearchDecisionValidationError(f"required actions were omitted: {sorted(required_ids - set(raw_ids))}")
    rationale = parsed.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ResearchDecisionValidationError("rationale must be a non-empty string")
    raw_deprioritized = parsed.get("deprioritized") or []
    if not isinstance(raw_deprioritized, list):
        raise ResearchDecisionValidationError("deprioritized must be a list")
    deprioritized: list[dict[str, str]] = []
    seen_deprioritized: set[str] = set()
    for item in raw_deprioritized:
        if not isinstance(item, Mapping):
            raise ResearchDecisionValidationError("each deprioritized item must be an object")
        action_id = item.get("action_id")
        reason = item.get("reason")
        if not isinstance(action_id, str) or action_id not in action_by_id:
            raise ResearchDecisionValidationError(f"invalid deprioritized action_id: {action_id!r}")
        if action_id in raw_ids:
            raise ResearchDecisionValidationError(f"selected action cannot be deprioritized: {action_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise ResearchDecisionValidationError(f"deprioritized reason is required for {action_id}")
        if action_id not in seen_deprioritized:
            deprioritized.append({"action_id": action_id, "reason": reason.strip()})
            seen_deprioritized.add(action_id)
    missing = [
        action.action_id
        for action in legal_actions
        if action.action_type == "branch"
        and action.action_id not in raw_ids
        and action.action_id not in seen_deprioritized
    ]
    if require_complete_deprioritized and missing:
        raise ResearchDecisionValidationError(f"missing deprioritized reason for unselected branch actions: {missing}")
    return selected, deprioritized, rationale.strip()


class ResearchDecisionEngine:
    def __init__(
        self,
        *,
        runtime: Any,
        artifact_registry: ArtifactRegistry,
        trace_path: Path,
        max_attempts: int = 3,
        model_config: Mapping[str, Any] | None = None,
        model_config_hash: str | None = None,
        secrets: Sequence[str] = (),
    ):
        self.runtime = runtime
        self.artifact_registry = artifact_registry
        self.trace_path = trace_path
        self.max_attempts = max(1, min(3, int(max_attempts)))
        self.model_config = redact_secrets(dict(model_config or getattr(runtime, "model_config", {}) or {}))
        self.model_config_hash = str(
            model_config_hash or getattr(runtime, "model_config_hash", "") or stable_hash(self.model_config)
        )
        self.secrets = tuple(secrets or getattr(runtime, "secrets", ()) or ())
        self.stats = ResearchDecisionStats()

    def _trace(self, event: Mapping[str, Any]) -> None:
        append_jsonl(self.trace_path, redact_secrets(dict(event)))

    def decide(
        self,
        *,
        stage_index: int,
        evidence: ResearchEvidence,
        legal_actions: Sequence[LegalAction],
        deterministic_plan: Sequence[Mapping[str, Any]],
        max_actions: int,
        identity_context: Mapping[str, Any],
    ) -> ResearchDecisionResult:
        legal_payload = [action.serializable() for action in legal_actions]
        user_payload = {
            "research_decision_schema": RESEARCH_DECISION_SCHEMA,
            "evidence": evidence.payload,
            "deterministic_plan": list(deterministic_plan),
            "legal_actions": legal_payload,
            "max_actions": max_actions,
        }
        base_user_prompt = json.dumps(user_payload, ensure_ascii=False, sort_keys=True)
        base_prompt_hash = stable_hash({"system": RESEARCH_SYSTEM_PROMPT, "user": base_user_prompt})
        identity = {
            "schema": RESEARCH_DECISION_SCHEMA,
            "decision_trigger": "next_stage_branch_selection",
            **dict(identity_context),
            "evidence_hash": evidence.evidence_hash,
            "legal_actions_hash": legal_actions_hash(legal_actions),
            "research_prompt_hash": base_prompt_hash,
            "model_config_hash": self.model_config_hash,
        }
        decision_id = stable_hash(identity)
        action_by_id = {action.action_id: action for action in legal_actions}

        def produce() -> dict[str, Any]:
            previous_error: str | None = None
            for attempt in range(1, self.max_attempts + 1):
                retry_payload = dict(user_payload)
                if previous_error:
                    retry_payload["previous_validation_error"] = previous_error
                    retry_payload["correction_instruction"] = (
                        "Return corrected strict JSON using only legal action_id values. "
                        "Every unselected branch action must include a deprioritized reason. "
                        "A stop action does not require a deprioritized entry."
                    )
                user_prompt = json.dumps(retry_payload, ensure_ascii=False, sort_keys=True)
                prompt_hash = stable_hash({"system": RESEARCH_SYSTEM_PROMPT, "user": user_prompt})
                common = {
                    "decision_id": decision_id,
                    "stage_index": stage_index,
                    "trigger": "next_stage_branch_selection",
                    "attempt": attempt,
                    "timestamp": _now(),
                    "system_prompt": RESEARCH_SYSTEM_PROMPT,
                    "user_evidence_prompt": user_prompt,
                    "prompt_hash": prompt_hash,
                    "model": self.model_config,
                    "raw_response": None,
                    "parsed_response": None,
                    "validation_result": None,
                    "error": None,
                    "latency_seconds": None,
                    "fallback_used": False,
                    "final_validated_actions": [],
                    "deterministic_plan": list(deterministic_plan),
                }
                self._trace({**common, "event": "REQUEST", "status": "REQUEST"})
                started = time.perf_counter()
                raw_response: Any = None
                parsed: dict[str, Any] | None = None
                try:
                    self.stats.llm_calls += 1
                    raw_response = self.runtime.generate_response(
                        messages=[
                            {"role": "system", "content": RESEARCH_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        response_format={"type": "json_object"},
                    )
                    parsed = _parse_response(raw_response)
                    selected, deprioritized, rationale = _validate_response(
                        parsed,
                        legal_actions=legal_actions,
                        max_actions=max_actions,
                    )
                except Exception as exc:
                    self.stats.failed_calls += 1
                    previous_error = str(_redact_text(f"{type(exc).__name__}: {exc}", self.secrets))
                    self._trace(
                        {
                            **common,
                            "event": "RESPONSE",
                            "timestamp": _now(),
                            "raw_response": _redact_text(raw_response, self.secrets),
                            "parsed_response": redact_secrets(parsed),
                            "validation_result": {"valid": False, "error": previous_error},
                            "error": previous_error,
                            "latency_seconds": time.perf_counter() - started,
                            "status": "FAILED",
                            "fallback_used": attempt == self.max_attempts,
                        }
                    )
                    continue
                self.stats.successful_calls += 1
                selected_payload = [action.serializable() for action in selected]
                self._trace(
                    {
                        **common,
                        "event": "RESPONSE",
                        "timestamp": _now(),
                        "raw_response": _redact_text(raw_response, self.secrets),
                        "parsed_response": redact_secrets(parsed),
                        "validation_result": {"valid": True, "error": None},
                        "latency_seconds": time.perf_counter() - started,
                        "status": "VALIDATED",
                        "final_validated_actions": selected_payload,
                    }
                )
                return {
                    "status": "VALIDATED",
                    "action_ids": [action.action_id for action in selected],
                    "deprioritized": deprioritized,
                    "rationale": rationale,
                    "fallback_used": False,
                    "attempt_count": attempt,
                }
            fallback_ids = [str(item["action_id"]) for item in deterministic_plan]
            fallback_actions = [action_by_id[action_id].serializable() for action_id in fallback_ids]
            self._trace(
                {
                    "event": "FALLBACK",
                    "decision_id": decision_id,
                    "stage_index": stage_index,
                    "trigger": "next_stage_branch_selection",
                    "attempt": self.max_attempts,
                    "timestamp": _now(),
                    "system_prompt": RESEARCH_SYSTEM_PROMPT,
                    "user_evidence_prompt": base_user_prompt,
                    "prompt_hash": base_prompt_hash,
                    "model": self.model_config,
                    "raw_response": None,
                    "parsed_response": None,
                    "validation_result": {"valid": False, "error": previous_error},
                    "error": previous_error,
                    "latency_seconds": None,
                    "status": "DETERMINISTIC_FALLBACK",
                    "fallback_used": True,
                    "final_validated_actions": fallback_actions,
                    "deterministic_plan": list(deterministic_plan),
                }
            )
            return {
                "status": "DETERMINISTIC_FALLBACK",
                "action_ids": fallback_ids,
                "deprioritized": [],
                "rationale": "Research LLM failed validation three times; used registry.select() unchanged.",
                "fallback_used": True,
                "attempt_count": self.max_attempts,
            }

        value, cache_hit = self.artifact_registry.materialize_once(identity, produce)
        payload = dict(value.get("payload") or {})
        selected_ids = list(payload.get("action_ids") or [])
        if any(action_id not in action_by_id for action_id in selected_ids):
            raise RuntimeError("cached Research Decision references an action outside the current legal action space")
        selected, normalized_deprioritized, _ = _validate_response(
            {
                "action_ids": selected_ids,
                "rationale": str(payload.get("rationale") or "cached decision"),
                "deprioritized": list(payload.get("deprioritized") or []),
            },
            legal_actions=legal_actions,
            max_actions=max_actions,
            require_complete_deprioritized=not bool(payload.get("fallback_used")),
        )
        selected_actions = tuple(selected)
        payload["deprioritized"] = [] if payload.get("fallback_used") else normalized_deprioritized
        if cache_hit:
            self.stats.cache_hits += 1
            self._trace(
                {
                    "event": "CACHE_HIT",
                    "decision_id": decision_id,
                    "stage_index": stage_index,
                    "trigger": "next_stage_branch_selection",
                    "attempt": 0,
                    "timestamp": _now(),
                    "system_prompt": RESEARCH_SYSTEM_PROMPT,
                    "user_evidence_prompt": base_user_prompt,
                    "prompt_hash": base_prompt_hash,
                    "model": self.model_config,
                    "raw_response": None,
                    "parsed_response": payload,
                    "validation_result": {"valid": True, "cache_identity": identity},
                    "error": None,
                    "latency_seconds": 0.0,
                    "status": "CACHE_HIT",
                    "fallback_used": bool(payload.get("fallback_used")),
                    "final_validated_actions": [action.serializable() for action in selected_actions],
                    "deterministic_plan": list(deterministic_plan),
                }
            )
        self.stats.decisions += 1
        if payload.get("fallback_used"):
            self.stats.fallback_decisions += 1
        return ResearchDecisionResult(
            decision_id=decision_id,
            stage_index=stage_index,
            selected_actions=selected_actions,
            deprioritized=tuple(
                {
                    **dict(item),
                    "branch": action_by_id[str(item["action_id"])].branch,
                    "generation_round": action_by_id[str(item["action_id"])].generation_round,
                }
                for item in payload.get("deprioritized") or []
            ),
            rationale=str(payload.get("rationale") or ""),
            status=str(payload.get("status") or "UNKNOWN"),
            fallback_used=bool(payload.get("fallback_used")),
            cache_hit=cache_hit,
            attempt_count=int(payload.get("attempt_count") or 0),
            deterministic_plan=tuple(dict(item) for item in deterministic_plan),
            evidence_hash=evidence.evidence_hash,
        )
