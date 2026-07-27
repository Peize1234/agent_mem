from __future__ import annotations

import json
import time
from copy import deepcopy
from typing import Any, Dict, Optional

from memory_monitor.models.demo_pipeline import (
    OPTIONAL_PIPELINE_STEPS,
    PIPELINE_STEPS,
    PipelineStep,
    StepStatus,
)
from memory_monitor.services.demo_repository import DemoRepository
from memory_monitor.services.memory_state_service import MemoryStateService


class PipelineStepError(RuntimeError):
    """Wrap a step failure with the scope needed to diagnose and retry it."""


class DemoPipelineService:
    """Persisted, single-step orchestration composed around ``DemoMemory``."""

    def __init__(
        self,
        memory,
        repository: DemoRepository,
        state_service: MemoryStateService,
        *,
        lease_seconds: int = 300,
        generation_kwargs: Optional[Dict[str, Any]] = None,
    ):
        self.memory = memory
        self.repository = repository
        self.state_service = state_service
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
    ) -> Dict[str, Any]:
        return self.repository.create_turn(
            session_id,
            user_id=user_id,
            run_id=run_id,
            user_message=user_message,
            turn_id=turn_id,
        )

    def run_next_step(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        for step_run in self.repository.list_steps(turn_id):
            if step_run["status"] not in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
                return self.run_step(turn_id, PipelineStep(step_run["step"]), session_id=session_id)
        return {"turn_id": turn_id, "complete": True, "steps": self.repository.list_steps(turn_id)}

    def run_step(self, turn_id: str, step: PipelineStep | str, *, session_id: str) -> Dict[str, Any]:
        step = PipelineStep(step)
        turn = self._require_turn(turn_id, session_id)
        existing = self._require_step(turn_id, step)
        if existing["status"] in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
            return existing
        self._require_prerequisites(turn_id, step)

        token = self.repository.claim_step(turn_id, step, lease_seconds=self.lease_seconds)
        if token is None:
            return self._require_step(turn_id, step)

        started_at = time.perf_counter()
        input_data = self._step_input(turn, step)
        before = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
        before_id = self.repository.create_snapshot(turn_id, step, "before", before)
        after_id = None
        diff = None
        try:
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
        except Exception as exc:
            duration_ms = (time.perf_counter() - started_at) * 1000
            try:
                after = self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"])
                after_id = self.repository.create_snapshot(turn_id, step, "after", after)
                diff = self.state_service.compare(before, after)
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

    def retry_step(self, turn_id: str, step: PipelineStep | str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        step = PipelineStep(step)
        current = self._require_step(turn_id, step)
        if current["status"] != StepStatus.FAILED.value:
            raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step.value}")
        return self.run_step(turn_id, step, session_id=session_id)

    def skip_step(
        self,
        turn_id: str,
        step: PipelineStep | str,
        *,
        session_id: str,
        reason: str = "Skipped by demo operator",
    ) -> Dict[str, Any]:
        step = PipelineStep(step)
        if step not in OPTIONAL_PIPELINE_STEPS:
            raise ValueError(f"Pipeline step is not optional: {step.value}")
        turn = self._require_turn(turn_id, session_id)
        current = self._require_step(turn_id, step)
        if current["status"] in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
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
        self._require_turn(turn_id, session_id)
        target_step = PipelineStep(target_step)
        target_index = PIPELINE_STEPS.index(target_step)
        for step in PIPELINE_STEPS[: target_index + 1]:
            current = self._require_step(turn_id, step)
            if current["status"] in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
                continue
            self.run_step(turn_id, step, session_id=session_id)
        return self._require_step(turn_id, target_step)

    def reset_turn(self, turn_id: str, *, session_id: str) -> Dict[str, Any]:
        self._require_turn(turn_id, session_id)
        self.repository.reset_turn(turn_id)
        return self._require_turn(turn_id, session_id)

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

        if step is PipelineStep.RUN_MIGRATION:
            return self._run_background_job(turn_id, "migration"), {}

        if step is PipelineStep.RUN_PROFILE:
            return self._run_background_job(turn_id, "profile"), {}

        if step is PipelineStep.REFRESH_STATE:
            return self.state_service.snapshot(user_id=turn["user_id"], run_id=turn["run_id"]), {}

        raise ValueError(f"Unsupported pipeline step: {step.value}")

    def _run_background_job(self, turn_id: str, job_type: str) -> Dict[str, Any]:
        commit = self._step_output(turn_id, PipelineStep.COMMIT_TURN)
        job_id = (commit.get("background") or {}).get(f"{job_type}_job_id")
        if not job_id:
            return {"job_type": job_type, "job_id": None, "processed": False, "status": "not_created"}
        worker = self.memory.demo_background_worker
        event_offset = len(self.memory.demo_events())
        processed = (
            worker.process_migration_job(job_id) if job_type == "migration" else worker.process_profile_job(job_id)
        )
        status = worker.get_job_status(job_id, job_type)
        status_name = (status or {}).get("status")
        if status_name not in {"succeeded", "succeeded_degraded"}:
            raise RuntimeError(
                f"{job_type} job did not complete successfully: "
                f"job_id={job_id} status={status_name} processed={processed}"
            )
        return {
            "job_type": job_type,
            "job_id": job_id,
            "processed": processed,
            "status": status_name,
            "job": status,
            "events": self.memory.demo_events()[event_offset:],
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
        if step in {PipelineStep.RUN_MIGRATION, PipelineStep.RUN_PROFILE}:
            commit = self._step_output(turn_id, PipelineStep.COMMIT_TURN)
            job_type = "migration" if step is PipelineStep.RUN_MIGRATION else "profile"
            return {
                "job_type": job_type,
                "job_id": (commit.get("background") or {}).get(f"{job_type}_job_id"),
            }
        return {"user_id": turn["user_id"], "run_id": turn["run_id"]}

    def _require_prerequisites(self, turn_id: str, step: PipelineStep) -> None:
        target_index = PIPELINE_STEPS.index(step)
        step_runs = {item["step"]: item for item in self.repository.list_steps(turn_id)}
        incomplete = [
            prerequisite.value
            for prerequisite in PIPELINE_STEPS[:target_index]
            if step_runs[prerequisite.value]["status"] not in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}
        ]
        if incomplete:
            raise RuntimeError(
                f"Pipeline prerequisites are incomplete for turn={turn_id} step={step.value}: {incomplete}"
            )

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
