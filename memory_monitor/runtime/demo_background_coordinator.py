from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from memory_monitor.models import FOREGROUND_STEPS, MEMORY_STEPS, PIPELINE_STEPS, PipelineStep, StepStatus
from memory_monitor.services.demo_repository import DemoRepository, StepAlreadyRunningError, StepHeldError

logger = logging.getLogger(__name__)

StepRunner = Callable[[str, PipelineStep, str, str], dict[str, Any]]
StepSettledCallback = Callable[[str, PipelineStep, str], None]


@dataclass(frozen=True)
class SubmissionResult:
    step: PipelineStep
    submitted: bool
    status: str
    reason: str | None = None


class DemoBackgroundCoordinator:
    """Sandbox dispatcher for foreground chains and four ordered memory queues."""

    def __init__(
        self,
        simulation_id: str,
        repository: DemoRepository,
        *,
        foreground_workers: int = 4,
        branch_workers: int = 1,
        lease_seconds: int = 900,
    ):
        self.simulation_id = simulation_id
        self.repository = repository
        self.lease_seconds = max(int(lease_seconds), 1)
        branch_worker_count = int(branch_workers)
        if branch_worker_count != 1:
            raise ValueError("Each Demo memory branch must use exactly one FIFO worker")
        self._foreground_executor = ThreadPoolExecutor(
            max_workers=max(int(foreground_workers), 1),
            thread_name_prefix=f"demo-{simulation_id}-foreground",
        )
        self._branch_executors = {
            step: ThreadPoolExecutor(
                max_workers=branch_worker_count,
                thread_name_prefix=f"demo-{simulation_id}-{step.value.removeprefix('run_')}",
            )
            for step in MEMORY_STEPS
        }
        self._futures: dict[tuple[str, PipelineStep], Future] = {}
        self._leases: dict[tuple[str, PipelineStep], str] = {}
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._accepting = True
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat,
            name=f"demo-{simulation_id}-lease-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def submit(
        self,
        turn_id: str,
        step: PipelineStep | str,
        session_id: str,
        runner: StepRunner,
        *,
        retry: bool = False,
        on_settled: StepSettledCallback | None = None,
    ) -> SubmissionResult:
        step = PipelineStep(step)
        if step not in PIPELINE_STEPS:
            raise ValueError(f"Pipeline step is not executable: {step.value}")
        key = (turn_id, step)
        with self._lock:
            if not self._accepting:
                raise RuntimeError(f"Demo background coordinator is closed: {self.simulation_id}")
            existing_future = self._futures.get(key)
            if existing_future is not None and not existing_future.done():
                current = self.repository.get_step(turn_id, step)
                return SubmissionResult(
                    step,
                    False,
                    (current or {}).get("status", StepStatus.QUEUED.value),
                    "already_registered",
                )

            step_run = self.repository.get_step(turn_id, step)
            if step_run is None:
                raise KeyError(f"Unknown demo step: turn={turn_id} step={step.value}")
            status = step_run["status"]
            if status in {StepStatus.SUCCEEDED.value, StepStatus.SKIPPED.value}:
                return SubmissionResult(step, False, status, "already_complete")
            if status in {StepStatus.QUEUED.value, StepStatus.RUNNING.value}:
                return SubmissionResult(step, False, status, "repository_lease")
            if step_run.get("is_held"):
                return SubmissionResult(step, False, status, "held")
            if retry and status != StepStatus.FAILED.value:
                raise ValueError(f"Only failed steps can be retried: turn={turn_id} step={step.value}")
            if not retry and status == StepStatus.FAILED.value:
                return SubmissionResult(step, False, status, "retry_required")

            try:
                token = self.repository.queue_step(
                    turn_id,
                    step,
                    lease_seconds=self.lease_seconds,
                    retry=retry,
                )
            except StepAlreadyRunningError:
                current = self.repository.get_step(turn_id, step)
                return SubmissionResult(
                    step,
                    False,
                    (current or {}).get("status", StepStatus.QUEUED.value),
                    "repository_lease",
                )
            except StepHeldError:
                return SubmissionResult(step, False, StepStatus.PENDING.value, "held")
            if token is None:
                current = self.repository.get_step(turn_id, step)
                return SubmissionResult(step, False, (current or {}).get("status", status), "not_queued")

            executor = self._executor_for(step)
            try:
                future = executor.submit(
                    self._run_registered,
                    turn_id,
                    step,
                    token,
                    session_id,
                    runner,
                )
            except Exception as exc:
                self.repository.fail_queued_step(turn_id, step, token, exc)
                raise
            self._futures[key] = future
            self._leases[key] = token
            future.add_done_callback(
                lambda completed, task_key=key, lease_token=token, selected_session=session_id: self._forget(
                    task_key,
                    lease_token,
                    selected_session,
                    completed,
                    on_settled,
                )
            )
            return SubmissionResult(step, True, StepStatus.QUEUED.value)

    def submit_many(
        self,
        turn_id: str,
        session_id: str,
        steps: tuple[PipelineStep, ...],
        runner: StepRunner,
        *,
        on_settled: StepSettledCallback | None = None,
    ) -> dict[PipelineStep, SubmissionResult]:
        return {
            step: self.submit(
                turn_id,
                step,
                session_id,
                runner,
                on_settled=on_settled,
            )
            for step in steps
        }

    def submit_enabled_branches(
        self,
        turn_id: str,
        session_id: str,
        steps: tuple[PipelineStep, ...],
        runner: StepRunner,
        *,
        on_settled: StepSettledCallback | None = None,
    ) -> dict[PipelineStep, SubmissionResult]:
        """Submit selected memory queues without coupling their failures."""
        selected = tuple(PipelineStep(step) for step in steps)
        invalid = [step.value for step in selected if step not in MEMORY_STEPS]
        if invalid:
            raise ValueError(f"Only memory branches can be submitted together: {invalid}")
        return self.submit_many(
            turn_id,
            session_id,
            selected,
            runner,
            on_settled=on_settled,
        )

    def submit_shortterm(
        self,
        turn_id: str,
        session_id: str,
        runner: StepRunner,
        *,
        retry: bool = False,
        on_settled: StepSettledCallback | None = None,
    ) -> SubmissionResult:
        return self.submit(
            turn_id,
            PipelineStep.RUN_SHORTTERM,
            session_id,
            runner,
            retry=retry,
            on_settled=on_settled,
        )

    def submit_midterm(self, turn_id: str, session_id: str, runner: StepRunner, **kwargs) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_MIDTERM, session_id, runner, **kwargs)

    def submit_longterm(self, turn_id: str, session_id: str, runner: StepRunner, **kwargs) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_LONGTERM, session_id, runner, **kwargs)

    def submit_profile(self, turn_id: str, session_id: str, runner: StepRunner, **kwargs) -> SubmissionResult:
        return self.submit(turn_id, PipelineStep.RUN_PROFILE, session_id, runner, **kwargs)

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
            item["status"] in {StepStatus.QUEUED.value, StepStatus.RUNNING.value}
            and (selected_step is None or PipelineStep(item["step"]) is selected_step)
            for item in self.repository.list_steps(turn_id)
        )

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._accepting = False
            executors = (self._foreground_executor, *self._branch_executors.values())
        for executor in executors:
            executor.shutdown(wait=wait, cancel_futures=False)
        self._heartbeat_stop.set()
        self._heartbeat_thread.join(timeout=1)

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

    def _executor_for(self, step: PipelineStep) -> ThreadPoolExecutor:
        if step in FOREGROUND_STEPS:
            return self._foreground_executor
        return self._branch_executors[step]

    def _run_registered(
        self,
        turn_id: str,
        step: PipelineStep,
        token: str,
        session_id: str,
        runner: StepRunner,
    ) -> dict[str, Any] | None:
        if not self.repository.start_step(
            turn_id,
            step,
            token,
            lease_seconds=self.lease_seconds,
        ):
            return None
        return runner(turn_id, step, token, session_id)

    def _heartbeat(self) -> None:
        interval = max(min(self.lease_seconds / 3, 30), 0.2)
        while not self._heartbeat_stop.wait(interval):
            with self._lock:
                leases = tuple(self._leases.items())
            for (turn_id, step), token in leases:
                try:
                    renewed = self.repository.renew_step_lease(
                        turn_id,
                        step,
                        token,
                        lease_seconds=self.lease_seconds,
                    )
                except Exception:
                    logger.exception(
                        "Could not renew Demo task lease turn=%s step=%s",
                        turn_id,
                        step.value,
                    )
                    continue
                if not renewed:
                    with self._lock:
                        if self._leases.get((turn_id, step)) == token:
                            self._leases.pop((turn_id, step), None)

    def _forget(
        self,
        key: tuple[str, PipelineStep],
        token: str,
        session_id: str,
        future: Future,
        on_settled: StepSettledCallback | None,
    ) -> None:
        turn_id, step = key
        error = future.exception()
        if error is not None:
            current = self.repository.get_step(turn_id, step)
            if current is not None and current.get("lock_token") == token:
                try:
                    if current["status"] == StepStatus.QUEUED.value:
                        self.repository.fail_queued_step(turn_id, step, token, error)
                    elif current["status"] == StepStatus.RUNNING.value:
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
                    logger.exception(
                        "Could not persist Demo task failure turn=%s step=%s",
                        turn_id,
                        step.value,
                    )
            logger.error(
                "Demo task failed turn=%s step=%s",
                turn_id,
                step.value,
                exc_info=(type(error), error, error.__traceback__),
            )
        if on_settled is not None:
            try:
                on_settled(turn_id, step, session_id)
            except Exception:
                logger.exception(
                    "Demo task completion callback failed turn=%s step=%s",
                    turn_id,
                    step.value,
                )
        with self._lock:
            if self._futures.get(key) is future:
                self._futures.pop(key, None)
            if self._leases.get(key) == token:
                self._leases.pop(key, None)
            self._idle.notify_all()
