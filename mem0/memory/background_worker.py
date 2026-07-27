from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from mem0.configs.base import BackgroundTaskConfig, ObservabilityConfig
from mem0.memory.observability import (
    NoOpObservationSink,
    ObservationContext,
    ObservationSink,
    emit_safely,
)

logger = logging.getLogger(__name__)

MigrationHandler = Callable[[Dict[str, Any], List[Dict[str, Any]], bool], None]
ProfileHandler = Callable[[Dict[str, Any]], None]

_RETRY_DELAYS_SECONDS = (2.0, 10.0, 30.0)


class BackgroundWorkerManager:
    """Runs one persistent migration worker and one persistent profile worker."""

    def __init__(
        self,
        db,
        config: Optional[BackgroundTaskConfig],
        *,
        process_midterm: MigrationHandler,
        process_longterm: MigrationHandler,
        process_profile: ProfileHandler,
        observation_sink: Optional[ObservationSink] = None,
        observability_config: Optional[ObservabilityConfig] = None,
    ):
        self.db = db
        self.config = config or BackgroundTaskConfig()
        self.process_midterm = process_midterm
        self.process_longterm = process_longterm
        self.process_profile = process_profile
        self.observation_sink = observation_sink or NoOpObservationSink()
        self.observability_config = observability_config or ObservabilityConfig()
        self._stop_event = threading.Event()
        self._migration_wakeup = threading.Event()
        self._profile_wakeup = threading.Event()
        self._threads: List[threading.Thread] = []
        self._started = False
        self._recovered = False
        self._state_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @property
    def automatic(self) -> bool:
        return self.enabled and getattr(self.config, "execution_mode", "auto") == "auto"

    def start(self) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            if not self._recovered:
                self.db.recover_stale_background_jobs(self.config.stale_running_timeout_seconds)
                self._recovered = True
            if not self.automatic:
                return
            if self._started:
                return
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._migration_loop,
                    name="mem0-memory-migration-worker",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._profile_loop,
                    name="mem0-profile-update-worker",
                    daemon=True,
                ),
            ]
            self._started = True
            for thread in self._threads:
                thread.start()

    def wake_migration(self) -> None:
        if self.automatic:
            self._migration_wakeup.set()

    def wake_profile(self) -> None:
        if self.automatic:
            self._profile_wakeup.set()

    def wake_all(self) -> None:
        self.wake_migration()
        self.wake_profile()

    def _wait(self, wakeup: threading.Event) -> None:
        wakeup.wait(timeout=float(self.config.poll_interval_seconds))
        wakeup.clear()

    @staticmethod
    def _retry_delay(attempt: int) -> float:
        index = min(max(attempt - 1, 0), len(_RETRY_DELAYS_SECONDS) - 1)
        return _RETRY_DELAYS_SECONDS[index]

    def _migration_loop(self) -> None:
        while not self._stop_event.is_set():
            if getattr(self.db, "connection", None) is None:
                return
            try:
                processed = self.process_next_migration_job()
            except Exception:
                logger.exception("Failed to claim a memory migration job")
                self._wait(self._migration_wakeup)
                continue
            if not processed:
                self._wait(self._migration_wakeup)
                continue

    def _profile_loop(self) -> None:
        while not self._stop_event.is_set():
            if getattr(self.db, "connection", None) is None:
                return
            try:
                processed = self.process_next_profile_job()
            except Exception:
                logger.exception("Failed to claim a profile update job")
                self._wait(self._profile_wakeup)
                continue
            if not processed:
                self._wait(self._profile_wakeup)
                continue

    def _observation_context(self, job: Dict[str, Any], job_type: str) -> Optional[ObservationContext]:
        trace_id = job.get("trace_id")
        if not getattr(self.observability_config, "enabled", False) or not trace_id:
            return None
        return ObservationContext(
            trace_id=trace_id,
            capture_payloads=getattr(self.observability_config, "capture_payloads", True),
            max_payload_length=getattr(self.observability_config, "max_payload_length", 20000),
            job_id=job["job_id"],
            job_type=job_type,
            user_id=job.get("user_id") or (job.get("filters") or {}).get("user_id"),
            run_id=job.get("run_id") or (job.get("filters") or {}).get("run_id"),
            session_scope=job.get("session_scope"),
        )

    def _emit_job_event(
        self,
        job: Dict[str, Any],
        job_type: str,
        event_type: str,
        status: str,
        *,
        error: Optional[BaseException] = None,
        output_data: Any = None,
    ) -> None:
        context = self._observation_context(job, job_type)
        if context is None:
            return
        emit_safely(
            self.observation_sink,
            lambda: context.event(
                stage="job",
                event_type=event_type,
                status=status,
                error=error,
                output_data=output_data,
            ),
        )

    def _run_claimed_job(self, job: Dict[str, Any], job_type: str) -> None:
        self._emit_job_event(job, job_type, "job.claimed", "running")
        if job.get("claimed_from_status") == "retry":
            self._emit_job_event(job, job_type, "job.retry_started", "running")
        try:
            if job_type == "migration":
                self._run_migration_job(job)
            else:
                self._run_profile_job(job)
        except Exception as exc:
            logger.exception("Unexpected %s worker failure for job_id=%s", job_type, job.get("job_id"))
            try:
                if job_type == "migration":
                    action = self.db.record_migration_failure(
                        job["job_id"],
                        "unexpected migration worker failure",
                        max_retries=int(self.config.max_retries),
                        retry_delay_seconds=self._retry_delay(int(job.get("attempts", 0)) + 1),
                    )
                    self._emit_retry_result(job, job_type, action, exc)
                    if action == "exhausted":
                        self.db.mark_migration_dead(job["job_id"], "unexpected migration worker failure")
                        self._emit_job_event(job, job_type, "job.dead", "dead", error=exc)
                else:
                    action = self.db.record_profile_failure(
                        job["job_id"],
                        "unexpected profile worker failure",
                        max_retries=int(self.config.max_retries),
                        retry_delay_seconds=self._retry_delay(int(job.get("attempts", 0)) + 1),
                    )
                    self._emit_retry_result(job, job_type, action, exc)
            except Exception:
                logger.exception("Failed to persist unexpected %s worker failure", job_type)

    def process_next_migration_job(self) -> bool:
        """Claim and process the next runnable migration job."""

        job = self.db.claim_next_migration_job()
        if not isinstance(job, dict):
            return False
        self._run_claimed_job(job, "migration")
        return True

    def process_next_profile_job(self) -> bool:
        """Claim and process the next runnable profile update job."""

        job = self.db.claim_next_profile_job()
        if not isinstance(job, dict):
            return False
        self._run_claimed_job(job, "profile")
        return True

    def process_migration_job(self, job_id: str) -> bool:
        """Process a specific runnable migration job while preserving queue order."""

        job = self.db.claim_migration_job(job_id)
        if not isinstance(job, dict):
            return False
        self._run_claimed_job(job, "migration")
        return True

    def process_profile_job(self, job_id: str) -> bool:
        """Process a specific runnable profile job while preserving queue order."""

        job = self.db.claim_profile_job(job_id)
        if not isinstance(job, dict):
            return False
        self._run_claimed_job(job, "profile")
        return True

    def _emit_retry_result(
        self,
        job: Dict[str, Any],
        job_type: str,
        action: str,
        exc: BaseException,
    ) -> None:
        if action == "retry":
            self._emit_job_event(job, job_type, "job.retry_scheduled", "retry", error=exc)
        elif action in {"exhausted", "dead"}:
            self._emit_job_event(job, job_type, "job.retry_exhausted", action, error=exc)
            if action == "dead":
                self._emit_job_event(job, job_type, "job.dead", "dead", error=exc)

    def _record_migration_failure(self, job: Dict[str, Any], stage: str, exc: Exception) -> str:
        job_id = job["job_id"]
        attempt = int(job.get("attempts", 0)) + 1
        logger.warning(
            "Background %s failed for job_id=%s session_scope=%s attempt=%s: %s",
            stage,
            job_id,
            job.get("session_scope"),
            attempt,
            exc,
        )
        action = self.db.record_migration_failure(
            job_id,
            f"{stage}: {exc}",
            max_retries=int(self.config.max_retries),
            retry_delay_seconds=self._retry_delay(attempt),
        )
        self._emit_retry_result(job, "migration", action, exc)
        return action

    def _run_migration_stage(
        self,
        job: Dict[str, Any],
        messages: List[Dict[str, Any]],
        stage: str,
        handler: MigrationHandler,
    ) -> bool:
        job_id = job["job_id"]
        try:
            handler(job, messages, False)
            self.db.mark_migration_stage_done(job_id, stage)
            return True
        except Exception as exc:
            action = self._record_migration_failure(job, stage, exc)
            if action != "exhausted":
                return False

        try:
            self._emit_job_event(job, "migration", "job.degraded_started", "running", output_data={"stage": stage})
            handler(job, messages, True)
            self.db.mark_migration_stage_done(job_id, stage, degraded=True)
            self._emit_job_event(
                job,
                "migration",
                "job.degraded_succeeded",
                "succeeded_degraded",
                output_data={"stage": stage},
            )
            logger.warning(
                "Background %s used degraded storage for job_id=%s session_scope=%s",
                stage,
                job_id,
                job.get("session_scope"),
            )
            return True
        except Exception as degraded_exc:
            logger.error(
                "Background %s degradation failed for job_id=%s session_scope=%s: %s",
                stage,
                job_id,
                job.get("session_scope"),
                degraded_exc,
            )
            self.db.mark_migration_dead(job_id, f"{stage} degradation: {degraded_exc}")
            self._emit_job_event(job, "migration", "job.dead", "dead", error=degraded_exc)
            return False

    def _run_migration_job(self, job: Dict[str, Any]) -> None:
        job_id = job["job_id"]
        messages = self.db.get_migration_job_messages(job_id)
        if not messages:
            self.db.mark_migration_dead(job_id, "migration source messages are missing")
            self._emit_job_event(job, "migration", "job.dead", "dead")
            return

        if not job["midterm_done"]:
            if not self._run_migration_stage(job, messages, "midterm", self.process_midterm):
                return
            job["attempts"] = 0

        if not job["longterm_done"]:
            if not self._run_migration_stage(job, messages, "longterm", self.process_longterm):
                return

        try:
            self.db.finish_migration_job(job_id)
        except Exception as exc:
            action = self._record_migration_failure({**job, "attempts": 0}, "finalize", exc)
            if action == "exhausted":
                self.db.mark_migration_dead(job_id, f"finalize: {exc}")
                self._emit_job_event(job, "migration", "job.dead", "dead", error=exc)

    def _run_profile_job(self, job: Dict[str, Any]) -> None:
        try:
            self.process_profile(job)
            self.db.finish_profile_job(job["job_id"])
        except Exception as exc:
            attempt = int(job.get("attempts", 0)) + 1
            logger.warning(
                "Background profile update failed for job_id=%s user_id=%s attempt=%s: %s",
                job["job_id"],
                job.get("user_id"),
                attempt,
                exc,
            )
            action = self.db.record_profile_failure(
                job["job_id"],
                str(exc),
                max_retries=int(self.config.max_retries),
                retry_delay_seconds=self._retry_delay(attempt),
            )
            self._emit_retry_result(job, "profile", action, exc)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until both queues have no runnable or delayed work."""
        if not self.enabled:
            return not self.db.background_jobs_pending()
        if not self.automatic:
            return not self.db.background_jobs_pending()
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0)
        self.wake_all()
        while self.db.background_jobs_pending():
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._stop_event.wait(min(float(self.config.poll_interval_seconds), remaining))
            else:
                self._stop_event.wait(float(self.config.poll_interval_seconds))
            self.wake_all()
        return True

    def stop(self, *, wait: bool = True, timeout: Optional[float] = None) -> bool:
        with self._state_lock:
            if not self._started:
                return True
            self._stop_event.set()
            self._migration_wakeup.set()
            self._profile_wakeup.set()
            threads = list(self._threads)

        if wait:
            deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0)
            for thread in threads:
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                thread.join(remaining)

        stopped = not any(thread.is_alive() for thread in threads)
        if stopped:
            with self._state_lock:
                self._threads = []
                self._started = False
        return stopped

    def threads_alive(self) -> bool:
        return any(thread.is_alive() for thread in self._threads)
