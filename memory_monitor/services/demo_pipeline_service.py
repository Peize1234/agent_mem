from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy
from typing import Any, Dict, Optional

from memory_monitor.models import (
    CORE_JOB_ACTIVE_STATUSES,
    FOREGROUND_STEPS,
    MEMORY_STEPS,
    OPTIONAL_PIPELINE_STEPS,
    PIPELINE_STEPS,
    TERMINAL_STEP_STATUSES,
    BackgroundStepConfig,
    PipelineStep,
    StepStatus,
    dependencies_for,
)
from memory_monitor.runtime.demo_background_coordinator import (
    DemoBackgroundCoordinator,
    SubmissionResult,
)
from memory_monitor.runtime.llm_trace import LLMTrace, ensure_traced_llm, trace_llm_calls
from memory_monitor.services.demo_repository import DemoRepository
from memory_monitor.services.memory_state_service import MemoryStateService

logger = logging.getLogger(__name__)

TARGET_ANSWER = "answer"
TARGET_MEMORY = "memory"
TARGET_ALL = "all"

STEP_SNAPSHOT_SECTIONS = {
    # Memory.add() creates the Core jobs together. Keep those rows in the
    # submission diff, while downstream steps own their independent outputs.
    PipelineStep.RUN_SHORTTERM: frozenset(
        {"short_term", "migration_jobs", "longterm_extraction_jobs", "profile_jobs"}
    ),
    PipelineStep.RUN_MIDTERM: frozenset(
        {"midterm_sessions", "midterm_pages", "promotion_jobs", "promoted_longterm"}
    ),
    PipelineStep.RUN_LONGTERM: frozenset({"fine_grained_longterm", "longterm_extraction_jobs"}),
    PipelineStep.RUN_PROFILE: frozenset({"profile"}),
}

STEP_LLM_PURPOSES = {
    PipelineStep.CAPTURE_INPUT: "捕获输入",
    PipelineStep.RETRIEVE_CONTEXT: "问题重写",
    PipelineStep.AGENTIC_RETRIEVAL: "Agentic 检索",
    PipelineStep.BUILD_PROMPT: "构建最终 Prompt",
    PipelineStep.GENERATE_RESPONSE: "生成最终回答",
    PipelineStep.RUN_SHORTTERM: "提交本轮记忆",
    PipelineStep.RUN_MIDTERM: "生成中期记忆",
    PipelineStep.RUN_LONGTERM: "执行细粒度长期记忆抽取",
    PipelineStep.RUN_PROFILE: "抽取用户画像",
}


class PipelineStepError(RuntimeError):
    """Wrap a step failure with the scope needed to diagnose and retry it."""


class BackgroundJobDeferred(RuntimeError):
    """The core queue cannot claim this job yet because an earlier job owns its stage."""

    def __init__(self, message: str, *, output: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.output = deepcopy(output or {})


class DemoPipelineService:
    """Persisted asynchronous DAG orchestration composed around ``DemoMemory``."""

    def __init__(
        self,
        memory,
        repository: DemoRepository,
        state_service: MemoryStateService,
        *,
        coordinator: DemoBackgroundCoordinator | None = None,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.memory = memory
        self.repository = repository
        self.state_service = state_service
        self.coordinator = coordinator
        self.generation_kwargs = deepcopy(generation_kwargs) if generation_kwargs else {}
        self._schedule_lock = threading.RLock()
        ensure_traced_llm(self.memory)

    def create_turn(
        self,
        session_id: str,
        *,
        user_id: str,
        run_id: str,
        user_message: str,
        custom_prompt: Optional[str] = None,
        turn_id: Optional[str] = None,
        background_config: BackgroundStepConfig | Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return self.repository.create_turn(
            session_id,
            user_id=user_id,
            run_id=run_id,
            user_message=user_message,
            custom_prompt=custom_prompt,
            turn_id=turn_id,
            background_config=background_config,
        )

    def update_background_config(
        self,
        turn_id: str,
        config: BackgroundStepConfig | Dict[str, Any],
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        return self.repository.update_background_config(turn_id, config)

    def run_next_step(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        turn = self._require_turn(turn_id, session_id)
        step_runs = self._step_map(turn_id)
        inflight = next(
            (
                step
                for step in PIPELINE_STEPS
                if step_runs[step]["status"] in {StepStatus.QUEUED.value, StepStatus.RUNNING.value}
            ),
            None,
        )
        if inflight is not None:
            return {"turn_id": turn_id, "blocked": "running", "step": inflight.value}
        failed = next(
            (step for step in PIPELINE_STEPS if step_runs[step]["status"] == StepStatus.FAILED.value),
            None,
        )
        if failed is not None:
            return {"turn_id": turn_id, "blocked": "failed", "step": failed.value}

        if turn.get("completed_at") is not None:
            return {"turn_id": turn_id, "complete": True}

        for step in FOREGROUND_STEPS:
            current = step_runs[step]
            if current["status"] != StepStatus.PENDING.value:
                continue
            if self._prerequisites_complete(turn_id, step):
                result = self._submit_step(turn_id, step, session_id)
                return self._submission_payload(turn_id, {step: result})

        if step_runs[PipelineStep.GENERATE_RESPONSE]["status"] == StepStatus.SUCCEEDED.value:
            return self.run_memory_stage(turn_id, session_id=session_id)
        return {"turn_id": turn_id, "complete": False}

    def run_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
    ) -> SubmissionResult | Dict[str, Any]:
        step = PipelineStep(step)
        if step not in PIPELINE_STEPS:
            raise ValueError(f"Pipeline step is not executable: {step.value}")
        self._require_turn(turn_id, session_id)
        existing = self._require_step(turn_id, step)
        if self._is_complete(existing) or existing["status"] in {
            StepStatus.QUEUED.value,
            StepStatus.RUNNING.value,
        }:
            return existing
        if existing["status"] == StepStatus.FAILED.value:
            raise ValueError(f"Use retry_step for a failed step: turn={turn_id} step={step.value}")
        self._require_not_held(existing)
        self._require_prerequisites(turn_id, step)
        if step in MEMORY_STEPS:
            self.repository.mark_background_submitted(turn_id)
        return self._submit_step(turn_id, step, session_id)

    def retry_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
    ) -> SubmissionResult:
        self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        current = self._require_step(turn_id, step)
        if current["status"] != StepStatus.FAILED.value:
            raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step.value}")
        self._require_not_held(current)
        self._require_prerequisites(turn_id, step)
        return self._submit_step(turn_id, step, session_id, retry=True)

    def retryable_steps(self, turn_id: str, *, session_id: str) -> list[Dict[str, Any]]:
        self._require_turn(turn_id, session_id)
        return [step for step in self.repository.list_steps(turn_id) if step["status"] == StepStatus.FAILED.value]

    def hold_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        """Persistently prevent automatic scheduling while keeping the step pending."""
        self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        if step not in MEMORY_STEPS:
            raise ValueError(f"Only pending memory steps can be held: {step.value}")
        return self.repository.hold_step(turn_id, step)

    def release_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        """Release a held memory step without starting or resuming execution."""
        self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        if step not in MEMORY_STEPS:
            raise ValueError(f"Only held memory steps can be released: {step.value}")
        return self.repository.release_step(turn_id, step)

    def set_memory_step_runnable(
        self,
        turn_id: str,
        step: PipelineStep | str,
        runnable: bool,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        """Persist a memory gate and resume only a previously started memory flow."""
        turn = self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        if step not in MEMORY_STEPS:
            raise ValueError(f"Only memory steps have runnable switches: {step.value}")
        current = self._require_step(turn_id, step)
        if current["status"] != StepStatus.PENDING.value:
            return {
                "turn_id": turn_id,
                "step": current,
                "runnable": not bool(current.get("is_held")),
                "changed": False,
                "resumed": False,
                "execution_target": turn.get("execution_target"),
                "submissions": {},
            }

        changed = False
        if runnable and current.get("is_held"):
            current = self.release_step(turn_id, step, session_id=session_id)
            changed = True
        elif not runnable and not current.get("is_held"):
            current = self.hold_step(turn_id, step, session_id=session_id)
            changed = True

        turn = self._require_turn(turn_id, session_id)
        target = turn.get("execution_target")
        resumed = bool(runnable and changed and target in {TARGET_MEMORY, TARGET_ALL})
        submissions = self._advance_turn(turn_id, session_id) if resumed else {}
        return {
            "turn_id": turn_id,
            "step": current,
            "runnable": not bool(current.get("is_held")),
            "changed": changed,
            "resumed": resumed,
            "execution_target": target,
            "submissions": submissions,
        }

    def skip_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
        reason: str = "Disabled by turn configuration",
    ) -> Dict[str, Any]:
        """Compatibility alias for the recoverable memory-step hold operation."""
        step = PipelineStep(step)
        if step not in OPTIONAL_PIPELINE_STEPS:
            raise ValueError(f"Pipeline step is not configurable: {step.value}")
        return self.hold_step(turn_id, step, session_id=session_id)

    def run_until(
        self,
        turn_id: str,
        target_step: PipelineStep | str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        target_step = PipelineStep(target_step)
        if target_step is not PipelineStep.GENERATE_RESPONSE:
            raise ValueError("run_until only supports generate_response in the asynchronous pipeline")
        return self.run_to_answer(turn_id, session_id=session_id)

    def run_to_answer(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        self.repository.set_execution_target(turn_id, TARGET_ANSWER)
        submissions = self._advance_turn(turn_id, session_id)
        return self._submission_payload(turn_id, submissions, execution_target=TARGET_ANSWER)

    def run_memory_stage(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        generation = self._require_step(turn_id, PipelineStep.GENERATE_RESPONSE)
        if generation["status"] != StepStatus.SUCCEEDED.value:
            raise RuntimeError("模型回答完成后才能运行记忆阶段")
        self.repository.set_execution_target(turn_id, TARGET_MEMORY)
        submissions = self._schedule_memory_batch(turn_id, session_id)
        return self._submission_payload(turn_id, submissions, execution_target=TARGET_MEMORY)

    def run_all(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        self.repository.set_execution_target(turn_id, TARGET_ALL)
        submissions = self._advance_turn(turn_id, session_id)
        return self._submission_payload(turn_id, submissions, execution_target=TARGET_ALL)

    def run_remaining(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        return self.run_all(turn_id, session_id=session_id)

    def submit_background_branches(
        self,
        turn_id: str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        return self.run_memory_stage(turn_id, session_id=session_id)

    def has_running_background(self, turn_id: str) -> bool:
        return any(
            step["status"] in {StepStatus.QUEUED.value, StepStatus.RUNNING.value}
            for step in self.repository.list_steps(turn_id)
        )

    def reset_turn(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        self.repository.reset_turn(turn_id)
        return self._require_turn(turn_id, session_id)

    def resume_pending_work(self) -> None:
        self.repository.recover_expired_step_leases()
        for turn in self.repository.list_scheduled_turns():
            self._advance_turn(turn["turn_id"], turn["session_id"])

    def _advance_turn(
        self,
        turn_id: str,
        session_id: str,
    ) -> dict[PipelineStep, SubmissionResult]:
        with self._schedule_lock:
            turn = self._require_turn(turn_id, session_id)
            target = turn.get("execution_target")
            if target not in {TARGET_ANSWER, TARGET_MEMORY, TARGET_ALL}:
                return {}
            step_runs = self._step_map(turn_id)

            if target in {TARGET_ANSWER, TARGET_ALL}:
                for step in FOREGROUND_STEPS:
                    status = step_runs[step]["status"]
                    if status == StepStatus.FAILED.value or status in {
                        StepStatus.QUEUED.value,
                        StepStatus.RUNNING.value,
                    }:
                        return {}
                    if status == StepStatus.PENDING.value:
                        result = self._submit_step(turn_id, step, session_id)
                        return {step: result}
                if target == TARGET_ANSWER:
                    self.repository.set_execution_target(turn_id, None)
                    return {}

            if target in {TARGET_MEMORY, TARGET_ALL}:
                return self._schedule_memory_batch(turn_id, session_id)
            return {}

    def _schedule_memory_batch(
        self,
        turn_id: str,
        session_id: str,
    ) -> dict[PipelineStep, SubmissionResult]:
        """Submit short-term once, then fan out all eligible memory branches."""
        with self._schedule_lock:
            turn = self._require_turn(turn_id, session_id)
            step_runs = self._step_map(turn_id)
            if step_runs[PipelineStep.GENERATE_RESPONSE]["status"] != StepStatus.SUCCEEDED.value:
                return {}

            if turn.get("background_submitted_at") is None:
                self.repository.mark_background_submitted(turn_id)

            # The persisted target remains active while any branch is held.
            # Re-read step rows after recording the batch intent.
            step_runs = self._step_map(turn_id)
            shortterm = step_runs[PipelineStep.RUN_SHORTTERM]
            if shortterm["status"] == StepStatus.FAILED.value or shortterm["status"] in {
                StepStatus.QUEUED.value,
                StepStatus.RUNNING.value,
            }:
                return {}
            if shortterm["status"] == StepStatus.PENDING.value:
                if shortterm.get("is_held"):
                    return {}
                result = self._submit_step(turn_id, PipelineStep.RUN_SHORTTERM, session_id)
                return {PipelineStep.RUN_SHORTTERM: result}

            eligible = []
            for step in MEMORY_STEPS[1:]:
                current = step_runs[step]
                if current["status"] != StepStatus.PENDING.value:
                    continue
                if current.get("is_held"):
                    continue
                if self._prerequisites_complete(turn_id, step):
                    eligible.append(step)
            submissions = (
                self.coordinator.submit_enabled_branches(
                    turn_id,
                    session_id,
                    tuple(eligible),
                    self._run_claimed_step,
                    on_settled=self._on_step_settled,
                )
                if eligible and self.coordinator is not None
                else {}
            )
            if not submissions and all(step_runs[step]["status"] in TERMINAL_STEP_STATUSES for step in MEMORY_STEPS):
                self.repository.set_execution_target(turn_id, None)
            return submissions

    def _submit_step(
        self,
        turn_id: str,
        step: PipelineStep,
        session_id: str,
        *,
        retry: bool = False,
    ) -> SubmissionResult:
        if self.coordinator is None:
            raise RuntimeError("Demo background coordinator is not configured")
        return self.coordinator.submit(
            turn_id,
            step,
            session_id,
            self._run_claimed_step,
            retry=retry,
            on_settled=self._on_step_settled,
        )

    def _on_step_settled(self, turn_id: str, step: PipelineStep, session_id: str) -> None:
        current = self._require_step(turn_id, step)
        if current["status"] in {
            StepStatus.SUCCEEDED.value,
            StepStatus.SKIPPED.value,
            StepStatus.FAILED.value,
        }:
            self._advance_turn(turn_id, session_id)

    def _run_claimed_step(
        self,
        turn_id: str,
        step: PipelineStep,
        token: str,
        session_id: str,
    ) -> Dict[str, Any]:
        turn = self._require_turn(turn_id, session_id)
        self._require_prerequisites(turn_id, step)
        started_at = time.perf_counter()
        input_data: Dict[str, Any] = {}
        before = None
        before_id = None
        after_id = None
        diff: Dict[str, Any] = {}
        mutates_memory = step in MEMORY_STEPS
        current = self._require_step(turn_id, step)
        previous_output = current.get("output") if isinstance(current.get("output"), dict) else {}
        with trace_llm_calls(
            step.value,
            STEP_LLM_PURPOSES[step],
            attempt=int(current.get("attempts") or 0),
            existing_llm_calls=previous_output.get("llm_calls"),
            existing_tool_calls=previous_output.get("tool_calls"),
        ) as trace:
            try:
                input_data = self._step_input(turn, step)
                if mutates_memory:
                    before, before_id, snapshot_error = self._capture_snapshot(turn, step, "before")
                    if snapshot_error:
                        diff["before_snapshot_error"] = snapshot_error
                output, turn_updates = self._execute(turn, step, input_data)
                output = self._output_with_trace(step, output, trace)
                if mutates_memory:
                    after, after_id, snapshot_error = self._capture_snapshot(turn, step, "after")
                    if snapshot_error:
                        diff["after_snapshot_error"] = snapshot_error
                    if before is not None and after is not None:
                        diff.update(
                            self.state_service.compare(
                                before,
                                after,
                                sections=STEP_SNAPSHOT_SECTIONS[step],
                            )
                        )
                duration_ms = (time.perf_counter() - started_at) * 1000
                disabled_agentic = (
                    step is PipelineStep.AGENTIC_RETRIEVAL
                    and isinstance(output, dict)
                    and output.get("agentic_status") == "disabled"
                )
                return self.repository.complete_step(
                    turn_id,
                    step,
                    token,
                    status=StepStatus.SKIPPED if disabled_agentic else StepStatus.SUCCEEDED,
                    input_data=input_data,
                    output_data=output,
                    duration_ms=duration_ms,
                    before_snapshot_id=before_id,
                    after_snapshot_id=after_id,
                    diff=diff,
                    skip_reason="Agentic retrieval is disabled" if disabled_agentic else None,
                    assistant_message=turn_updates.get("assistant_message"),
                    generation=turn_updates.get("generation"),
                    commit=turn_updates.get("commit"),
                )
            except BackgroundJobDeferred as exc:
                if mutates_memory:
                    try:
                        after, after_id, snapshot_error = self._capture_snapshot(turn, step, "after")
                        if snapshot_error:
                            diff["after_snapshot_error"] = snapshot_error
                        if before is not None and after is not None:
                            diff.update(
                                self.state_service.compare(
                                    before,
                                    after,
                                    sections=STEP_SNAPSHOT_SECTIONS[step],
                                )
                            )
                    except Exception as snapshot_exc:
                        # Snapshot inspection must not replace the real queue
                        # deferral that controls the Demo step lifecycle.
                        diff["after_snapshot_error"] = f"{type(snapshot_exc).__name__}: {snapshot_exc}"
                duration_ms = (time.perf_counter() - started_at) * 1000
                return self.repository.defer_step(
                    turn_id,
                    step,
                    token,
                    input_data=input_data,
                    output_data=self._output_with_trace(step, exc.output, trace),
                    reason=str(exc),
                    duration_ms=duration_ms,
                    before_snapshot_id=before_id,
                    after_snapshot_id=after_id,
                    diff=diff,
                )
            except Exception as exc:
                duration_ms = (time.perf_counter() - started_at) * 1000
                self.repository.fail_step(
                    turn_id,
                    step,
                    token,
                    input_data=input_data,
                    output_data=self._failure_output(step, trace),
                    error=exc,
                    duration_ms=duration_ms,
                    before_snapshot_id=before_id,
                    after_snapshot_id=after_id,
                    diff=diff,
                )
                raise PipelineStepError(
                    "Demo pipeline step failed: "
                    f"step={step.value} user_id={turn['user_id']} session_id={turn['session_id']} "
                    f"run_id={turn['run_id']} turn_id={turn_id}: {exc}"
                ) from exc

    def _capture_snapshot(
        self,
        turn: Dict[str, Any],
        step: PipelineStep,
        phase: str,
    ) -> tuple[Dict[str, Any] | None, str | None, str | None]:
        try:
            snapshot = self.state_service.snapshot(
                user_id=turn["user_id"],
                run_id=turn["run_id"],
                sections=STEP_SNAPSHOT_SECTIONS[step],
            )
            snapshot_id = self.repository.create_snapshot(turn["turn_id"], step, phase, snapshot)
            return snapshot, snapshot_id, None
        except Exception as exc:
            return None, None, f"{type(exc).__name__}: {exc}"

    def _execute(
        self,
        turn: Dict[str, Any],
        step: PipelineStep,
        input_data: Dict[str, Any],
    ) -> tuple[Any, Dict[str, Any]]:
        turn_id = turn["turn_id"]
        if step is PipelineStep.CAPTURE_INPUT:
            return deepcopy(input_data), {}

        if step is PipelineStep.RETRIEVE_CONTEXT:
            context = self.memory.retrieve_context_for_demo(
                turn["user_message"],
                user_id=turn["user_id"],
                session_id=turn["run_id"],
            )
            return context, {}

        if step is PipelineStep.AGENTIC_RETRIEVAL:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            if not context.get("agentic_retrieval"):
                return {
                    "agentic_status": "disabled",
                    "agentic_memory_supplement": None,
                    "agentic_answer": None,
                    "iterations": 0,
                    "tool_call_count": 0,
                    "stop_reason": "disabled",
                    "tool_trace": [],
                }, {}
            try:
                result = self.memory.generate_agentic_response_for_demo(
                    context,
                    **self.generation_kwargs,
                )
                metadata = result if isinstance(result, dict) else {}
                agentic_memory_supplement = self.memory._normalize_agentic_supplement_result(result)
            except Exception as exc:
                logger.warning(
                    "Agentic retrieval failed in demo; using the retrieved context only",
                    exc_info=True,
                )
                return {
                    "agentic_status": "degraded",
                    "agentic_memory_supplement": "",
                    "agentic_answer": "",
                    "iterations": 0,
                    "tool_call_count": 0,
                    "stop_reason": "degraded",
                    "tool_trace": [],
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }, {}
            tool_call_count = int(metadata.get("tool_call_count") or 0)
            agentic_status = metadata.get("status")
            if agentic_status not in {"not_needed", "supplemented", "no_relevant_memory", "degraded"}:
                agentic_status = "degraded"
            if agentic_status == "supplemented" and not agentic_memory_supplement:
                agentic_status = "degraded"
            return {
                "agentic_status": agentic_status,
                "agentic_memory_supplement": agentic_memory_supplement,
                # Compatibility alias for persisted Demo consumers.
                "agentic_answer": agentic_memory_supplement,
                "raw_response": result,
                "iterations": int(metadata.get("iterations") or 0),
                "tool_call_count": tool_call_count,
                "stop_reason": agentic_status,
                "tool_trace": deepcopy(metadata.get("tool_trace") or []),
            }, {}

        if step is PipelineStep.BUILD_PROMPT:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            agentic = self._completed_step_output(turn_id, PipelineStep.AGENTIC_RETRIEVAL)
            prompt = self.memory.build_prompt_from_context(
                context,
                agentic_memory_supplement=agentic.get("agentic_memory_supplement") or "",
                custom_prompt=turn.get("custom_prompt"),
            )
            output = {
                "context_hash": self.memory.context_hash(context),
                "agentic_status": agentic.get("agentic_status"),
                "custom_prompt": turn.get("custom_prompt"),
                "messages": prompt,
            }
            return output, {}

        if step is PipelineStep.GENERATE_RESPONSE:
            prompt_output = self._step_output(turn_id, PipelineStep.BUILD_PROMPT)
            messages = prompt_output["messages"]
            raw_response = self.memory.generate_response_for_demo(messages, **self.generation_kwargs)
            assistant_message = self._assistant_text(raw_response)
            output = {
                "context_hash": prompt_output["context_hash"],
                "prompt_messages": messages,
                "assistant_message": assistant_message,
                "raw_response": raw_response,
            }
            return output, {"assistant_message": assistant_message, "generation": output}

        if step is PipelineStep.RUN_SHORTTERM:
            generation = self._step_output(turn_id, PipelineStep.GENERATE_RESPONSE)
            session = self.repository.get_session(turn["session_id"])
            if session is None:
                raise KeyError(f"Unknown demo session: {turn['session_id']}")
            result = self.memory.commit_demo_turn(
                simulation_id=session["simulation_id"],
                turn_id=turn_id,
                user_id=turn["user_id"],
                run_id=turn["run_id"],
                user_message=turn["user_message"],
                assistant_message=generation["assistant_message"],
            )
            background = result.get("background") if isinstance(result, dict) else {}
            return {
                **(result if isinstance(result, dict) else {"result": result}),
                "memory_add_created": {
                    "short_term_messages": 2,
                    "migration_job_id": (background or {}).get("migration_job_id"),
                    "longterm_extraction_job_ids": list(
                        (background or {}).get("longterm_extraction_job_ids") or []
                    ),
                    "profile_job_id": (background or {}).get("profile_job_id"),
                },
            }, {"commit": result}

        if step in MEMORY_STEPS[1:]:
            return self._run_background_job(turn_id, step), {}

        raise ValueError(f"Unsupported pipeline step: {step.value}")

    def _run_background_job(self, turn_id: str, step: PipelineStep) -> Dict[str, Any]:
        commit = self._step_output(turn_id, PipelineStep.RUN_SHORTTERM)
        background = commit.get("background") or {}
        worker = self.memory.demo_background_worker
        if step is PipelineStep.RUN_PROFILE:
            job_type = "profile"
            job_id = background.get("profile_job_id")
            status_field = "status"
            processor = worker.process_profile_job
            stage = None
        else:
            job_type = "migration"
            migration_job_id = background.get("migration_job_id")
            stage = "midterm" if step is PipelineStep.RUN_MIDTERM else "longterm"
            status_field = f"{stage}_status"
            processor = worker.process_midterm_job if step is PipelineStep.RUN_MIDTERM else worker.process_longterm_job

        # Profile remains a single Core queue item. The other memory branches
        # below deliberately keep Migration, Extraction, and Promotion
        # independent because Memory.add() can create them separately.
        if step is PipelineStep.RUN_PROFILE:
            if not job_id:
                return {
                    "job_type": job_type,
                    "job_id": None,
                    "processed": False,
                    "status": "not_created",
                }
            event_offset = len(self.memory.demo_events())
            processed = processor(job_id)
            status = worker.get_job_status(job_id, job_type)
            status_name = (status or {}).get(status_field)
            if status_name not in {"succeeded", "succeeded_degraded"}:
                if not processed and status_name in {"pending", "running", "retry"}:
                    raise BackgroundJobDeferred(
                        f"{step.value} is waiting for an earlier core queue item: job_id={job_id} status={status_name}"
                    )
                raise RuntimeError(
                    f"{step.value} job did not complete successfully: "
                    f"job_id={job_id} status={status_name} processed={processed}"
                )
            return {
                "job_type": job_type,
                "pipeline_step": step.value,
                "job_id": job_id,
                "processed": processed,
                "status": status_name,
                "job": status,
                "events": [
                    event
                    for event in self.memory.demo_events()[event_offset:]
                    if event.get("job_id") == job_id
                ],
            }

        migration_result = None
        migration_pending = False
        if migration_job_id:
            event_offset = len(self.memory.demo_events())
            migration_processed = processor(migration_job_id)
            migration_status = worker.get_job_status(migration_job_id, "migration")
            migration_status_name = (migration_status or {}).get(status_field)
            if migration_status_name not in {"succeeded", "succeeded_degraded"}:
                if not migration_processed and migration_status_name in {"pending", "running", "retry"}:
                    migration_pending = True
                else:
                    raise RuntimeError(
                        f"{step.value} migration job did not complete successfully: "
                        f"job_id={migration_job_id} status={migration_status_name} processed={migration_processed}"
                    )
            migration_result = {
                "job_type": "migration",
                "pipeline_step": step.value,
                "job_id": migration_job_id,
                "processed": migration_processed,
                "status": migration_status_name,
                "job": migration_status,
                "events": [
                    event
                    for event in self.memory.demo_events()[event_offset:]
                    if event.get("job_id") == migration_job_id
                    and event.get("stage") in {None, stage}
                ],
            }

        if step is PipelineStep.RUN_LONGTERM:
            extraction_ids = list(background.get("longterm_extraction_job_ids") or [])
            extraction_results = []
            extraction_pending = False
            extraction_processor = getattr(worker, "process_longterm_extraction_job", None)
            if extraction_ids and extraction_processor is None:
                raise RuntimeError("Demo worker does not expose the Core longterm extraction handler")
            for extraction_id in extraction_ids:
                extraction_status = worker.get_job_status(extraction_id, "longterm_extraction")
                extraction_status_name = (extraction_status or {}).get("status")
                extraction_processed = False
                if extraction_status_name not in {"succeeded", "discarded"}:
                    extraction_processed = extraction_processor(extraction_id)
                    extraction_status = worker.get_job_status(extraction_id, "longterm_extraction")
                    extraction_status_name = (extraction_status or {}).get("status")
                if extraction_status_name in CORE_JOB_ACTIVE_STATUSES:
                    extraction_pending = True
                elif extraction_status_name not in {"succeeded", "discarded"}:
                    raise RuntimeError(
                        f"{step.value} extraction job did not complete successfully: "
                        f"job_id={extraction_id} status={extraction_status_name} processed={extraction_processed}"
                    )
                extraction_results.append(
                    {
                        "job_type": "longterm_extraction",
                        "job_id": extraction_id,
                        "processed": extraction_processed,
                        "status": extraction_status_name,
                        "job": extraction_status,
                    }
                )
            if migration_job_id is None and not extraction_ids:
                return {
                    "job_type": "migration",
                    "job_id": None,
                    "processed": False,
                    "status": "not_created",
                    "longterm_extraction_jobs": [],
                }
            if migration_pending or extraction_pending:
                pending_ids = [
                    str(item["job_id"])
                    for item in extraction_results
                    if item["status"] in CORE_JOB_ACTIVE_STATUSES
                ]
                if migration_pending:
                    pending_ids.insert(0, str(migration_job_id))
                deferred_output = migration_result or {
                    "job_type": "longterm_extraction",
                    "pipeline_step": step.value,
                    "job_id": extraction_ids[0] if len(extraction_ids) == 1 else None,
                    "processed": any(item["processed"] for item in extraction_results),
                    "status": "pending",
                    "job": None,
                    "events": [],
                }
                deferred_output["job_ids"] = [
                    *([migration_job_id] if migration_job_id else []),
                    *extraction_ids,
                ]
                deferred_output["longterm_extraction_jobs"] = extraction_results
                raise BackgroundJobDeferred(
                    f"{step.value} is waiting for Core queue items: {', '.join(pending_ids)}",
                    output=deferred_output,
                )
            if migration_result is None:
                return {
                    "job_type": "longterm_extraction",
                    "pipeline_step": step.value,
                    "job_id": extraction_ids[0] if len(extraction_ids) == 1 else None,
                    "job_ids": extraction_ids,
                    "processed": any(item["processed"] for item in extraction_results),
                    "status": "succeeded" if extraction_results else "not_created",
                    "job": extraction_results[0]["job"] if len(extraction_results) == 1 else None,
                    "longterm_extraction_jobs": extraction_results,
                }
            migration_result["longterm_extraction_jobs"] = extraction_results
            migration_result["job_ids"] = [migration_job_id, *extraction_ids]
            return migration_result

        # Promotion is an independent Core queue. A claimed job may belong to
        # another turn, so expose its real identifiers instead of attributing
        # it to this Demo turn.
        promotion_detail = None
        promotion_details_processor = getattr(worker, "process_next_promotion_job_details", None)
        if promotion_details_processor is not None:
            promotion_detail = promotion_details_processor()
        else:
            promotion_processor = getattr(worker, "process_next_promotion_job", None)
            if promotion_processor is not None and promotion_processor():
                detail_getter = getattr(worker, "get_last_processed_job", None)
                promotion_detail = detail_getter("promotion") if callable(detail_getter) else None
                promotion_detail = promotion_detail or {
                    "job_type": "promotion",
                    "processed": True,
                    "queue_scope": "core_promotion_queue",
                }
        if migration_job_id is None and migration_result is None and promotion_detail is None:
            return {
                "job_type": "migration",
                "job_id": None,
                "processed": False,
                "status": "not_created",
                "promotion_processed": False,
            }
        if migration_pending:
            deferred_output = migration_result or {
                "job_type": "migration",
                "pipeline_step": step.value,
                "job_id": migration_job_id,
                "processed": False,
                "status": "pending",
                "job": None,
                "events": [],
            }
            if promotion_detail is not None:
                deferred_output["promotion_processed"] = bool(promotion_detail.get("processed", True))
                deferred_output["promotion_job_id"] = promotion_detail.get("job_id")
                deferred_output["promotion_source_midterm_session_id"] = promotion_detail.get(
                    "source_midterm_session_id"
                )
                deferred_output["promotion_job"] = promotion_detail
                deferred_output["promotion_queue_scope"] = "core_promotion_queue"
            raise BackgroundJobDeferred(
                f"{step.value} is waiting for Core queue items: {migration_job_id}",
                output=deferred_output,
            )
        if migration_result is None:
            migration_result = {
                "job_type": "promotion",
                "pipeline_step": step.value,
                "job_id": None,
                "processed": bool(promotion_detail),
                "status": (promotion_detail or {}).get("status", "succeeded"),
                "job": None,
                "events": [],
            }
        if promotion_detail is not None:
            promotion_status = promotion_detail.get("status")
            if promotion_status in CORE_JOB_ACTIVE_STATUSES:
                raise BackgroundJobDeferred(
                    f"{step.value} is waiting for Core promotion job: "
                    f"job_id={promotion_detail.get('job_id')} status={promotion_status}",
                    output={
                        **migration_result,
                        "promotion_processed": bool(promotion_detail.get("processed", True)),
                        "promotion_job_id": promotion_detail.get("job_id"),
                        "promotion_source_midterm_session_id": promotion_detail.get(
                            "source_midterm_session_id"
                        ),
                        "promotion_job": promotion_detail,
                        "promotion_queue_scope": "core_promotion_queue",
                    },
                )
            if promotion_status not in {None, "succeeded", "succeeded_degraded"}:
                raise RuntimeError(
                    f"{step.value} promotion job did not complete successfully: "
                    f"job_id={promotion_detail.get('job_id')} status={promotion_status}"
                )
            migration_result["promotion_processed"] = bool(promotion_detail.get("processed", True))
            migration_result["promotion_job_id"] = promotion_detail.get("job_id")
            migration_result["promotion_source_midterm_session_id"] = promotion_detail.get(
                "source_midterm_session_id"
            )
            migration_result["promotion_job"] = promotion_detail
            migration_result["promotion_queue_scope"] = "core_promotion_queue"
        else:
            migration_result["promotion_processed"] = False
        return migration_result

    def _step_input(self, turn: Dict[str, Any], step: PipelineStep) -> Dict[str, Any]:
        turn_id = turn["turn_id"]
        if step is PipelineStep.CAPTURE_INPUT:
            return {
                "turn_id": turn_id,
                "user_id": turn["user_id"],
                "run_id": turn["run_id"],
                "user_message": turn["user_message"],
            }
        if step is PipelineStep.RETRIEVE_CONTEXT:
            return {"query": turn["user_message"], "user_id": turn["user_id"], "run_id": turn["run_id"]}
        if step is PipelineStep.AGENTIC_RETRIEVAL:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            return {
                "context_hash": self.memory.context_hash(context),
                "enabled": bool(context.get("agentic_retrieval")),
            }
        if step is PipelineStep.BUILD_PROMPT:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            agentic = self._completed_step_output(turn_id, PipelineStep.AGENTIC_RETRIEVAL)
            return {
                "context_hash": self.memory.context_hash(context),
                "agentic_status": agentic.get("agentic_status"),
                "agentic_memory_supplement": agentic.get("agentic_memory_supplement"),
                "agentic_answer": agentic.get("agentic_answer"),
                "custom_prompt": turn.get("custom_prompt"),
            }
        if step is PipelineStep.GENERATE_RESPONSE:
            prompt = self._step_output(turn_id, PipelineStep.BUILD_PROMPT)
            return {"context_hash": prompt["context_hash"], "messages": prompt["messages"]}
        if step is PipelineStep.RUN_SHORTTERM:
            generation = self._step_output(turn_id, PipelineStep.GENERATE_RESPONSE)
            return {
                "turn_id": turn_id,
                "session_id": turn["session_id"],
                "user_message": turn["user_message"],
                "assistant_message": generation["assistant_message"],
            }
        if step in MEMORY_STEPS[1:]:
            commit = self._step_output(turn_id, PipelineStep.RUN_SHORTTERM)
            background = commit.get("background") or {}
            job_id = (
                background.get("profile_job_id")
                if step is PipelineStep.RUN_PROFILE
                else background.get("migration_job_id")
            )
            return {
                "pipeline_step": step.value,
                "job_id": job_id,
                "longterm_extraction_job_ids": list(background.get("longterm_extraction_job_ids") or [])
                if step is PipelineStep.RUN_LONGTERM
                else [],
            }
        raise ValueError(f"Unsupported pipeline step: {step.value}")

    def _require_prerequisites(self, turn_id: str, step: PipelineStep) -> None:
        incomplete = [
            prerequisite.value
            for prerequisite in dependencies_for(step, self.repository.background_config(turn_id))
            if not self._is_complete(self._require_step(turn_id, prerequisite))
        ]
        if incomplete:
            raise RuntimeError(
                f"Pipeline prerequisites are incomplete for turn={turn_id} step={step.value}: {incomplete}"
            )

    def _prerequisites_complete(self, turn_id: str, step: PipelineStep) -> bool:
        return all(
            self._is_complete(self._require_step(turn_id, prerequisite))
            for prerequisite in dependencies_for(step, self.repository.background_config(turn_id))
        )

    @staticmethod
    def _require_not_held(step: Dict[str, Any]) -> None:
        if step.get("is_held"):
            raise RuntimeError(f"Pipeline step is held: step={step['step']}")

    def _step_output(self, turn_id: str, step: PipelineStep) -> Any:
        step_run = self._require_step(turn_id, step)
        if step_run["status"] != StepStatus.SUCCEEDED.value:
            raise RuntimeError(f"Required step has not succeeded: turn={turn_id} step={step.value}")
        return deepcopy(step_run.get("output"))

    def _completed_step_output(self, turn_id: str, step: PipelineStep) -> Any:
        step_run = self._require_step(turn_id, step)
        if not self._is_complete(step_run):
            raise RuntimeError(f"Required step is incomplete: turn={turn_id} step={step.value}")
        return deepcopy(step_run.get("output") or {})

    @staticmethod
    def _output_with_trace(step: PipelineStep, output: Any, trace: LLMTrace) -> Any:
        if not isinstance(output, dict):
            if not trace.llm_calls and not trace.tool_calls:
                return output
            output = {"result": output}
        else:
            output = deepcopy(output)
        if trace.llm_calls or "llm_calls" in output or step is PipelineStep.AGENTIC_RETRIEVAL:
            llm_calls = deepcopy(trace.llm_calls)
            if step is PipelineStep.AGENTIC_RETRIEVAL and output.get("agentic_status") == "not_needed":
                # The decision model's accidental prose is not a memory
                # supplement and must not be shown as Agentic result content.
                for call in llm_calls:
                    call["response"] = None
                    call["response_suppressed"] = True
            output["llm_calls"] = llm_calls
        if trace.tool_calls or "tool_calls" in output or step is PipelineStep.AGENTIC_RETRIEVAL:
            output["tool_calls"] = deepcopy(trace.tool_calls)
        return output

    @staticmethod
    def _failure_output(step: PipelineStep, trace: LLMTrace) -> Dict[str, Any]:
        output = {
            "llm_calls": deepcopy(trace.llm_calls),
            "tool_calls": deepcopy(trace.tool_calls),
        }
        if step is PipelineStep.AGENTIC_RETRIEVAL:
            output["agentic_status"] = "failed"
        return output

    def _require_turn(self, turn_id: str, session_id: str) -> Dict[str, Any]:
        return self.repository.assert_turn_belongs_to_session(turn_id, session_id)

    def _require_step(self, turn_id: str, step: PipelineStep) -> Dict[str, Any]:
        step_run = self.repository.get_step(turn_id, step)
        if step_run is None:
            raise KeyError(f"Unknown demo step: turn={turn_id} step={step.value}")
        return step_run

    def _step_map(self, turn_id: str) -> dict[PipelineStep, Dict[str, Any]]:
        return {PipelineStep(item["step"]): item for item in self.repository.list_steps(turn_id)}

    @staticmethod
    def _is_complete(step: Dict[str, Any]) -> bool:
        return step["status"] in TERMINAL_STEP_STATUSES

    @staticmethod
    def _submission_payload(
        turn_id: str,
        submissions: dict[PipelineStep, SubmissionResult],
        *,
        execution_target: str | None = None,
    ) -> Dict[str, Any]:
        return {
            "turn_id": turn_id,
            "scheduled": True,
            "execution_target": execution_target,
            "submissions": {
                step.value: {
                    "submitted": result.submitted,
                    "status": result.status,
                    "reason": result.reason,
                }
                for step, result in submissions.items()
            },
        }

    @staticmethod
    def _assistant_text(response: Any) -> str:
        if isinstance(response, str):
            text = response
        elif isinstance(response, dict):
            text = response.get("content") or response.get("text") or response.get("message")
            if isinstance(text, dict):
                text = text.get("content")
            if text is None:
                text = json.dumps(response, ensure_ascii=False, default=str)
        else:
            text = getattr(response, "content", None) or str(response)
        text = str(text).strip()
        if not text:
            raise ValueError("The answer model returned an empty response")
        return text
