from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any, Dict, Optional

from memory_monitor.models.demo_pipeline import (
    BACKGROUND_STEPS,
    FOREGROUND_STEPS,
    OPTIONAL_PIPELINE_STEPS,
    BackgroundStepConfig,
    PipelineStep,
    StepStatus,
    dependencies_for,
)
from memory_monitor.runtime.demo_background_coordinator import DemoBackgroundCoordinator, SubmissionResult
from memory_monitor.services.demo_repository import DemoRepository
from memory_monitor.services.memory_state_service import MemoryStateService


class PipelineStepError(RuntimeError):
    """Wrap a step failure with the scope needed to diagnose and retry it."""


class BackgroundJobDeferred(RuntimeError):
    """The core queue cannot claim this job yet because an earlier job owns its stage."""


class DemoPipelineService:
    """Persisted DAG orchestration composed around ``DemoMemory``."""

    def __init__(
        self,
        memory,
        repository: DemoRepository,
        state_service: MemoryStateService,
        *,
        coordinator: DemoBackgroundCoordinator | None = None,
        lease_seconds: int = 300,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.memory = memory
        self.repository = repository
        self.state_service = state_service
        self.coordinator = coordinator
        self.lease_seconds = max(int(lease_seconds), 1)
        self.generation_kwargs = deepcopy(generation_kwargs) if generation_kwargs else {}

    def create_turn(
        self,
        session_id: str,
        *,
        user_id: str,
        run_id: str,
        user_message: str,
        turn_id: Optional[str] = None,
        background_config: BackgroundStepConfig | Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return self.repository.create_turn(
            session_id,
            user_id=user_id,
            run_id=run_id,
            user_message=user_message,
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
        for step in FOREGROUND_STEPS:
            current = step_runs[step]
            if current["status"] == StepStatus.FAILED.value:
                return {"turn_id": turn_id, "blocked": "failed", "step": step.value}
            if current["status"] == StepStatus.RUNNING.value:
                return {"turn_id": turn_id, "blocked": "running", "step": step.value}
            if not self._is_complete(current):
                return self.run_step(turn_id, step, session_id=session_id)

        config = self.repository.background_config(turn_id)
        enabled = config.enabled_background_steps()
        if turn.get("background_submitted_at") is None:
            submissions = self.submit_background_branches(turn_id, session_id=session_id)
            return self._submission_payload(turn_id, submissions)

        step_runs = self._step_map(turn_id)
        failed = [step for step in enabled if step_runs[step]["status"] == StepStatus.FAILED.value]
        if failed:
            return {
                "turn_id": turn_id,
                "blocked": "failed",
                "steps": [step.value for step in failed],
            }
        running = [step for step in enabled if step_runs[step]["status"] == StepStatus.RUNNING.value]
        if running:
            return {
                "turn_id": turn_id,
                "blocked": "background_running",
                "steps": [step.value for step in running],
            }
        pending = [step for step in enabled if step_runs[step]["status"] == StepStatus.PENDING.value]
        if pending:
            submissions = self._submit_steps(turn_id, session_id, tuple(pending))
            return self._submission_payload(turn_id, submissions)

        refresh = step_runs[PipelineStep.REFRESH_STATE]
        if refresh["status"] == StepStatus.FAILED.value:
            return {"turn_id": turn_id, "blocked": "failed", "step": PipelineStep.REFRESH_STATE.value}
        if self._is_complete(refresh):
            return {"turn_id": turn_id, "complete": True, "steps": self.repository.list_steps(turn_id)}
        return self.run_step(turn_id, PipelineStep.REFRESH_STATE, session_id=session_id)

    def run_step(self, turn_id: str, step: PipelineStep | str, *, session_id: str) -> Dict[str, Any]:
        step = PipelineStep(step)
        turn = self._require_turn(turn_id, session_id)
        existing = self._require_step(turn_id, step)
        if self._is_complete(existing):
            return existing
        self._require_enabled(turn_id, step)
        self._require_prerequisites(turn_id, step)

        token = self.repository.claim_step(turn_id, step, lease_seconds=self.lease_seconds)
        if token is None:
            return self._require_step(turn_id, step)
        return self._run_claimed_step(turn, step, token)

    def retry_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
    ) -> Dict[str, Any] | SubmissionResult:
        self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        current = self._require_step(turn_id, step)
        if current["status"] != StepStatus.FAILED.value:
            raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step.value}")
        self._require_enabled(turn_id, step)
        self._require_prerequisites(turn_id, step)
        if step in BACKGROUND_STEPS and self.coordinator is not None:
            return self.coordinator.submit(
                turn_id,
                step,
                session_id,
                self._run_claimed_background_step,
                retry=True,
            )
        return self.run_step(turn_id, step, session_id=session_id)

    def retryable_steps(self, turn_id: str, *, session_id: str) -> list[Dict[str, Any]]:
        self._require_turn(turn_id, session_id)
        return [step for step in self.repository.list_steps(turn_id) if step["status"] == StepStatus.FAILED.value]

    def skip_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
        reason: str = "Skipped by demo operator",
    ) -> Dict[str, Any]:
        """Compatibility API for old Demo callers; UI configuration never uses it."""
        step = PipelineStep(step)
        if step not in OPTIONAL_PIPELINE_STEPS:
            raise ValueError(f"Pipeline step is not optional: {step.value}")
        turn = self._require_turn(turn_id, session_id)
        current = self._require_step(turn_id, step)
        if self._is_complete(current):
            return current
        self._require_prerequisites(turn_id, step)
        token = self.repository.claim_step(turn_id, step, lease_seconds=self.lease_seconds)
        if token is None:
            return self._require_step(turn_id, step)
        before = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
        before_id = self.repository.create_snapshot(turn_id, step, "before", before)
        after_id = self.repository.create_snapshot(turn_id, step, "after", before)
        return self.repository.complete_step(
            turn_id,
            step,
            token,
            status=StepStatus.SKIPPED,
            input_data={"reason": reason},
            output_data={"skipped": True},
            duration_ms=0,
            before_snapshot_id=before_id,
            after_snapshot_id=after_id,
            diff=self.state_service.compare(before, before),
            skip_reason=reason,
        )

    def run_until(
        self,
        turn_id: str,
        target_step: PipelineStep | str,
        *,
        session_id: str,
    ) -> Dict[str, Any]:
        target_step = PipelineStep(target_step)
        self._require_turn(turn_id, session_id)
        if target_step in FOREGROUND_STEPS:
            return self._run_foreground_until(turn_id, target_step, session_id)
        if target_step is PipelineStep.REFRESH_STATE:
            return self.run_all(turn_id, session_id=session_id)
        self._run_foreground_until(turn_id, PipelineStep.COMMIT_TURN, session_id)
        return self.run_step(turn_id, target_step, session_id=session_id)

    def run_all(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._run_foreground_until(turn_id, PipelineStep.COMMIT_TURN, session_id)
        submissions = self.submit_background_branches(turn_id, session_id=session_id)
        return self._submission_payload(turn_id, submissions)

    def submit_background_branches(
        self,
        turn_id: str,
        *,
        session_id: str,
    ) -> dict[PipelineStep, SubmissionResult]:
        self._require_turn(turn_id, session_id)
        self._require_prerequisites(turn_id, PipelineStep.RUN_MIDTERM)
        if self.coordinator is None:
            raise RuntimeError("Demo background coordinator is not configured")
        frozen_turn = self.repository.mark_background_submitted(turn_id)
        config = BackgroundStepConfig.from_mapping(frozen_turn)
        return self._submit_steps(turn_id, session_id, config.enabled_background_steps())

    def has_running_background(self, turn_id: str) -> bool:
        return any(
            step["step"] in {item.value for item in BACKGROUND_STEPS} and step["status"] == StepStatus.RUNNING.value
            for step in self.repository.list_steps(turn_id)
        )

    def reset_turn(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        self.repository.reset_turn(turn_id)
        return self._require_turn(turn_id, session_id)

    def _submit_steps(
        self,
        turn_id: str,
        session_id: str,
        steps: tuple[PipelineStep, ...],
    ) -> dict[PipelineStep, SubmissionResult]:
        if self.coordinator is None:
            raise RuntimeError("Demo background coordinator is not configured")
        return self.coordinator.submit_enabled_branches(
            turn_id,
            session_id,
            steps,
            self._run_claimed_background_step,
        )

    def _run_foreground_until(
        self,
        turn_id: str,
        target_step: PipelineStep,
        session_id: str,
    ) -> Dict[str, Any]:
        target_index = FOREGROUND_STEPS.index(target_step)
        for step in FOREGROUND_STEPS[: target_index + 1]:
            current = self._require_step(turn_id, step)
            if self._is_complete(current):
                continue
            if current["status"] == StepStatus.FAILED.value:
                raise RuntimeError(f"Retry the failed step before continuing: {step.value}")
            self.run_step(turn_id, step, session_id=session_id)
        return self._require_step(turn_id, target_step)

    def _run_claimed_background_step(
        self,
        turn_id: str,
        step: PipelineStep,
        token: str,
        session_id: str,
    ) -> Dict[str, Any]:
        turn = self._require_turn(turn_id, session_id)
        self._require_enabled(turn_id, step)
        self._require_prerequisites(turn_id, step)
        return self._run_claimed_step(turn, step, token)

    def _run_claimed_step(
        self,
        turn: Dict[str, Any],
        step: PipelineStep,
        token: str,
    ) -> Dict[str, Any]:
        turn_id = turn["turn_id"]
        started_at = time.perf_counter()
        input_data: Dict[str, Any] = {}
        before = None
        before_id = None
        after_id = None
        diff = None
        try:
            input_data = self._step_input(turn, step)
            before = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
            before_id = self.repository.create_snapshot(turn_id, step, "before", before)
            output, turn_updates = self._execute(turn, step, input_data)
            after = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
            after_id = self.repository.create_snapshot(turn_id, step, "after", after)
            diff = self.state_service.compare(before, after)
            duration_ms = (time.perf_counter() - started_at) * 1000
            return self.repository.complete_step(
                turn_id,
                step,
                token,
                input_data=input_data,
                output_data=output,
                duration_ms=duration_ms,
                before_snapshot_id=before_id,
                after_snapshot_id=after_id,
                diff=diff,
                assistant_message=turn_updates.get("assistant_message"),
                generation=turn_updates.get("generation"),
                commit=turn_updates.get("commit"),
            )
        except BackgroundJobDeferred as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            try:
                after = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
                after_id = self.repository.create_snapshot(turn_id, step, "after", after)
                diff = self.state_service.compare(before, after) if before is not None else {"snapshot_error": "before"}
            except Exception as snapshot_exc:
                diff = {"snapshot_error": f"{type(snapshot_exc).__name__}: {snapshot_exc}"}
            return self.repository.defer_step(
                turn_id,
                step,
                token,
                input_data=input_data,
                reason=str(exc),
                duration_ms=duration_ms,
                before_snapshot_id=before_id,
                after_snapshot_id=after_id,
                diff=diff,
            )
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            try:
                after = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
                after_id = self.repository.create_snapshot(turn_id, step, "after", after)
                diff = self.state_service.compare(before, after) if before is not None else {"snapshot_error": "before"}
            except Exception as snapshot_exc:
                diff = {"snapshot_error": f"{type(snapshot_exc).__name__}: {snapshot_exc}"}
            self.repository.fail_step(
                turn_id,
                step,
                token,
                input_data=input_data,
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

        if step is PipelineStep.BUILD_PROMPT:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            prompt = self.memory.build_prompt_from_context(context)
            return {"context_hash": self.memory.context_hash(context), "messages": prompt}, {}

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

        if step is PipelineStep.COMMIT_TURN:
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
            return result, {"commit": result}

        if step in BACKGROUND_STEPS:
            return self._run_background_job(turn_id, step), {}

        if step is PipelineStep.REFRESH_STATE:
            return self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"]), {}

        raise ValueError(f"Unsupported pipeline step: {step.value}")

    def _run_background_job(self, turn_id: str, step: PipelineStep) -> Dict[str, Any]:
        commit = self._step_output(turn_id, PipelineStep.COMMIT_TURN)
        background = commit.get("background") or {}
        worker = self.memory.demo_background_worker
        if step is PipelineStep.RUN_PROFILE:
            job_type = "profile"
            job_id = background.get("profile_job_id")
            status_field = "status"
            processor = worker.process_profile_job
        else:
            job_type = "migration"
            job_id = background.get("migration_job_id")
            stage = "midterm" if step is PipelineStep.RUN_MIDTERM else "longterm"
            status_field = f"{stage}_status"
            processor = worker.process_midterm_job if step is PipelineStep.RUN_MIDTERM else worker.process_longterm_job
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
            if not processed and status_name in {"pending", "running"}:
                raise BackgroundJobDeferred(
                    f"{step.value} is waiting for an earlier core queue item: job_id={job_id} status={status_name}"
                )
            raise RuntimeError(
                f"{step.value} job did not complete successfully: "
                f"job_id={job_id} status={status_name} processed={processed}"
            )
        events = [
            event
            for event in self.memory.demo_events()[event_offset:]
            if event.get("job_id") == job_id
            and (step is PipelineStep.RUN_PROFILE or event.get("stage") in {None, stage})
        ]
        return {
            "job_type": job_type,
            "pipeline_step": step.value,
            "job_id": job_id,
            "processed": processed,
            "status": status_name,
            "job": status,
            "events": events,
        }

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
        if step is PipelineStep.BUILD_PROMPT:
            context = self._step_output(turn_id, PipelineStep.RETRIEVE_CONTEXT)
            return {"context_hash": self.memory.context_hash(context)}
        if step is PipelineStep.GENERATE_RESPONSE:
            prompt = self._step_output(turn_id, PipelineStep.BUILD_PROMPT)
            return {"context_hash": prompt["context_hash"], "messages": prompt["messages"]}
        if step is PipelineStep.COMMIT_TURN:
            generation = self._step_output(turn_id, PipelineStep.GENERATE_RESPONSE)
            return {
                "turn_id": turn_id,
                "session_id": turn["session_id"],
                "user_message": turn["user_message"],
                "assistant_message": generation["assistant_message"],
            }
        if step in BACKGROUND_STEPS:
            commit = self._step_output(turn_id, PipelineStep.COMMIT_TURN)
            background = commit.get("background") or {}
            job_id = (
                background.get("profile_job_id")
                if step is PipelineStep.RUN_PROFILE
                else background.get("migration_job_id")
            )
            return {"pipeline_step": step.value, "job_id": job_id}
        return {"user_id": turn["user_id"], "run_id": turn["run_id"]}

    def _require_prerequisites(self, turn_id: str, step: PipelineStep) -> None:
        config = self.repository.background_config(turn_id)
        step_runs = self._step_map(turn_id)
        incomplete = [
            prerequisite.value
            for prerequisite in dependencies_for(step, config)
            if not self._is_complete(step_runs[prerequisite])
        ]
        if incomplete:
            raise RuntimeError(
                f"Pipeline prerequisites are incomplete for turn={turn_id} step={step.value}: {incomplete}"
            )

    def _require_enabled(self, turn_id: str, step: PipelineStep) -> None:
        if step in BACKGROUND_STEPS and not self.repository.background_config(turn_id).enabled(step):
            raise RuntimeError(f"Background step is not enabled for this turn: {step.value}")

    def _step_output(self, turn_id: str, step: PipelineStep) -> Any:
        step_run = self._require_step(turn_id, step)
        if step_run["status"] != StepStatus.SUCCEEDED.value:
            raise RuntimeError(f"Required step has not succeeded: turn={turn_id} step={step.value}")
        return deepcopy(step_run.get("output"))

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
        return step["status"] in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}

    @staticmethod
    def _submission_payload(
        turn_id: str,
        submissions: dict[PipelineStep, SubmissionResult],
    ) -> Dict[str, Any]:
        return {
            "turn_id": turn_id,
            "background_submitted": True,
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
