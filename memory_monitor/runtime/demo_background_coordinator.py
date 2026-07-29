from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from memory_monitor.models import BACKGROUND_STEPS, PipelineStep, StepStatus
from memory_monitor.services.demo_repository import DemoRepository, StepAlreadyRunningError

BackgroundStepRunner = Callable[[str, PipelineStep, str, str], dict[str, Any]]


@dataclass(frozen=True)
class SubmissionResult:
    step: PipelineStep
    submitted: bool
    status: str
    reason: str | None = None


class DemoBackgroundCoordinator:
    """Sandbox-scoped dispatcher with one ordered queue per background stage."""

    def __init__(
        self,
        simulation_id: str,
        repository: DemoRepository,
        *,
        lease_seconds: int = 300,
    ):
        self.simulation_id = simulation_id
        self.repository = repository
        self.lease_seconds = max(int(lease_seconds), 1)
        self._executors = {
            PipelineStep.RUN_MIDTERM: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"demo-{simulation_id}-midterm",
            ),
            PipelineStep.RUN_LONGTERM: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"demo-{simulation_id}-longterm",
            ),
            PipelineStep.RUN_PROFILE: ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"demo-{simulation_id}-profile",
            ),
        }
        self._futures: dict[tuple[str, PipelineStep], Future] = {}
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._accepting = True

    def submit_midterm(
        self,
        turn_id: str,
        session_id: str,
        runner: BackgroundStepRunner,
        *,
        retry: bool = False,
    ) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_MIDTERM, session_id, runner, retry=retry)

    def submit_longterm(
        self,
        turn_id: str,
        session_id: str,
        runner: BackgroundStepRunner,
        *,
        retry: bool = False,
    ) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_LONGTERM, session_id, runner, retry=retry)

    def submit_profile(
        self,
        turn_id: str,
        session_id: str,
        runner: BackgroundStepRunner,
        *,
        retry: bool = False,
    ) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_PROFILE, session_id, runner, retry=retry)

    def submit(
        self,
        turn_id: str,
        step: PipelineStep | str,
        session_id: str,
        runner: BackgroundStepRunner,
        *,
        retry: bool = False,
    ) -> SubmissionResult:
        step = PipelineStep(step)
        if step not in BACKGROUND_STEPS:
            raise ValueError(f"Not a background pipeline step: {step.value}")
        key = (turn_id, step)
        with self._lock:
            if not self._accepting:
                raise RuntimeError(f"Demo background coordinator is closed: {self.simulation_id}")
            existing_future = self._futures.get(key)
            if existing_future is not None and not existing_future.done():
                return SubmissionResult(step, False, StepStatus.RUNNING.value, "already_registered")

            step_run = self.repository.get_step(turn_id, step)
            if step_run is None:
                raise KeyError(f"Unknown demo step: turn={turn_id} step={step.value}")
            status = step_run["status"]
            if status in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
                return SubmissionResult(step, False, status, "already_complete")
            if retry and status != StepStatus.FAILED.value:
                raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step.value}")
            if not retry and status == StepStatus.FAILED.value:
                return SubmissionResult(step, False, status, "retry_required")

            try:
                token = self.repository.claim_step(turn_id, step, lease_seconds=self.lease_seconds)
            except StepAlreadyRunningError:
                return SubmissionResult(step, False, StepStatus.RUNNING.value, "repository_lease")
            if token is None:
                current = self.repository.get_step(turn_id, step)
                return SubmissionResult(step, False, (current or {}).get("status", status), "not_claimed")
            try:
                future = self._executors[step].submit(runner, turn_id, step, token, session_id)
            except Exception as exc:
                self.repository.fail_step(
                    turn_id,
                    step,
                    token,
                    input_data={"submission": True},
                    error=exc,
                    duration_ms=0,
                    before_snapshot_id=None,
                    after_snapshot_id=None,
                    diff={},
                )
                raise
            self._futures[key] = future
            future.add_done_callback(
                lambda completed, task_key=key, lease_token=token: self._forget(
                    task_key,
                    lease_token,
                    completed,
                )
            )
            return SubmissionResult(step, True, StepStatus.RUNNING.value)

    def submit_enabled_branches(
        self,
        turn_id: str,
        session_id: str,
        steps: tuple[PipelineStep, ...],
        runner: BackgroundStepRunner,
    ) -> dict[PipelineStep, SubmissionResult]:
        return {step: self.submit(turn_id, step, session_id, runner) for step in steps}

    def is_running(self, turn_id: str | None = None, step: PipelineStep | str | None = None) -> bool:
        selected_step = PipelineStep(step) if step is not None else None
        with self._lock:
            for (future_turn_id, future_step), future in self._futures.items():
                if turn_id is not None and future_turn_id != turn_id:
                    continue
                if selected_step is not None and future_step is not selected_step:
                    continue
                if not future.done():
                    return True
        if turn_id is None:
            return False
        return any(
            item["status"] == StepStatus.RUNNING.value
            and (selected_step is None or PipelineStep(item["step"]) is selected_step)
            for item in self.repository.list_steps(turn_id)
            if PipelineStep(item["step"]) in BACKGROUND_STEPS
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._accepting = False
            executors = tuple(self._executors.values())
        for executor in executors:
            executor.shutdown(wait=wait, cancel_futures=False)

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0)
        with self._idle:
            while self._futures:
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                if remaining == 0:
                    return False
                self._idle.wait(remaining)
            return True

    @property
    def accepting(self) -> bool:
        with self._lock:
            return self._accepting

    def _forget(
        self,
        key: tuple[str, PipelineStep],
        token: str,
        future: Future,
    ) -> None:
        error = future.exception()
        if error is not None:
            turn_id, step = key
            current = self.repository.get_step(turn_id, step)
            if (
                current is not None
                and current["status"] == StepStatus.RUNNING.value
                and current.get("lock_token") == token
            ):
                try:
                    self.repository.fail_step(
                        turn_id,
                        step,
                        token,
                        input_data={"coordinator": True},
                        error=error,
                        duration_ms=0,
                        before_snapshot_id=None,
                        after_snapshot_id=None,
                        diff={},
                    )
                except Exception:
                    pass
        with self._lock:
            if self._futures.get(key) is future:
                self._futures.pop(key, None)
            self._idle.notify_all()
