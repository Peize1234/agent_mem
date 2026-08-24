from __future__ import annotations

from copy import deepcopy
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

from mem0.memory.background_worker import BackgroundWorkerManager, LeaseHeartbeat

logger = logging.getLogger(__name__)

DemoEventRecorder = Callable[[str, Dict[str, Any]], None]


class DemoBackgroundWorkerManager(BackgroundWorkerManager):
    """Manual facade over the production stage-level state machines."""

    def __init__(self, *args, event_recorder: Optional[DemoEventRecorder] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._event_recorder = event_recorder
        self._manual_locks = {
            "midterm": threading.Lock(),
            "longterm": threading.Lock(),
            "longterm_extraction": threading.Lock(),
            "profile": threading.Lock(),
            "promotion": threading.Lock(),
        }
        self._recovered = False
        self._manual_condition = threading.Condition()
        self._accepting_manual_work = True
        self._active_manual_calls = 0
        self._last_manual_results: Dict[str, Optional[Dict[str, Any]]] = {}

    def start(self) -> None:
        """Recover stale work but deliberately avoid starting background threads."""
        with self._state_lock:
            if self._recovered or not self.enabled:
                return
            recovered = self.db.recover_expired_background_leases(self.config.max_stale_recoveries)
            logger.info(
                "Demo recovered stale Core jobs migration=%s profile=%s longterm_extraction=%s promotion=%s",
                recovered.get("migration", 0),
                recovered.get("profile", 0),
                recovered.get("longterm_extraction", 0),
                recovered.get("promotion", 0),
            )
            if self.startup_cleanup is not None:
                try:
                    self.startup_cleanup()
                except Exception:
                    logger.exception("Demo orphan staging cleanup failed during startup")
            with self._manual_condition:
                self._accepting_manual_work = True
            self._recovered = True

    def stop(self, *, wait: bool = True, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0)
        with self._manual_condition:
            self._accepting_manual_work = False
            while wait and self._active_manual_calls:
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                if remaining == 0:
                    return False
                self._manual_condition.wait(remaining)
            if self._active_manual_calls:
                return False
        remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
        return super().stop(wait=wait, timeout=remaining)

    def threads_alive(self) -> bool:
        with self._manual_condition:
            manual_work_alive = self._active_manual_calls > 0
        return manual_work_alive or super().threads_alive()

    def _begin_manual_call(self) -> bool:
        with self._manual_condition:
            if not self._accepting_manual_work:
                return False
            self._active_manual_calls += 1
            return True

    def _end_manual_call(self) -> None:
        with self._manual_condition:
            self._active_manual_calls -= 1
            self._manual_condition.notify_all()

    def wake_midterm(self) -> None:
        return None

    def wake_longterm(self) -> None:
        return None

    def wake_profile(self) -> None:
        return None

    def wake_promotion(self) -> None:
        return None

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Report queue state without waiting for threads that do not exist."""
        return not self.db.background_jobs_pending()

    def process_next_midterm_job(self) -> bool:
        return self._process_claimed_stage("midterm")

    def process_next_longterm_job(self) -> bool:
        return self._process_claimed_stage("longterm")

    def process_next_migration_job(self) -> bool:
        midterm_processed = self.process_next_midterm_job()
        longterm_processed = self.process_next_longterm_job()
        return midterm_processed or longterm_processed

    def process_next_profile_job(self) -> bool:
        return self._process_claimed_profile()

    def process_next_longterm_extraction_job(self) -> bool:
        return self._process_claimed_longterm_extraction()

    def process_next_promotion_job(self) -> bool:
        return self._process_claimed_promotion()

    def process_next_promotion_job_details(self) -> Optional[Dict[str, Any]]:
        """Process one Core promotion job and return its persisted identity.

        ``process_next_promotion_job`` stays boolean for compatibility with
        the worker API. The pipeline uses this detail-bearing variant because
        a FIFO Core queue item is not necessarily created by the current Demo
        turn.
        """
        self._last_manual_results["promotion"] = None
        self._process_claimed_promotion()
        return deepcopy(self._last_manual_results.get("promotion"))

    def get_last_processed_job(self, job_type: str) -> Optional[Dict[str, Any]]:
        return deepcopy(self._last_manual_results.get(job_type))

    def process_midterm_job(self, job_id: str) -> bool:
        return self._process_claimed_stage("midterm", job_id)

    def process_longterm_job(self, job_id: str) -> bool:
        return self._process_claimed_stage("longterm", job_id)

    def process_migration_job(self, job_id: str) -> bool:
        """Compatibility helper that manually advances both independent stages."""
        midterm_processed = self.process_midterm_job(job_id)
        longterm_processed = self.process_longterm_job(job_id)
        return midterm_processed or longterm_processed

    def process_profile_job(self, job_id: str) -> bool:
        return self._process_claimed_profile(job_id)

    def process_longterm_extraction_job(self, job_id: str) -> bool:
        return self._process_claimed_longterm_extraction(job_id)

    def process_promotion_job(self, job_id: str) -> bool:
        return self._process_claimed_promotion(job_id)

    def get_job_status(self, job_id: str, job_type: str) -> Optional[Dict[str, Any]]:
        return self.db.get_background_job(job_id, job_type)

    def _process_claimed_stage(self, stage: str, job_id: Optional[str] = None) -> bool:
        if stage not in {"midterm", "longterm"}:
            raise ValueError("stage must be 'midterm' or 'longterm'")
        if not self._begin_manual_call():
            return False
        handler = self.process_midterm if stage == "midterm" else self.process_longterm
        try:
            with self._manual_locks[stage]:
                job = (
                    self.db.claim_migration_stage(job_id, stage, self.config.lease_timeout_seconds)
                    if job_id is not None
                    else self.db.claim_next_migration_stage(stage, self.config.lease_timeout_seconds)
                )
                if not isinstance(job, dict):
                    return False
                self._record("job.started", {"job_type": "migration", "stage": stage, "job_id": job["job_id"]})
                try:
                    self._run_migration_stage(job, stage, handler)
                except Exception as exc:
                    self._record_unexpected_stage_failure(job, stage, exc)
                status = self.db.get_background_job(job["job_id"], "migration")
                stage_status = (status or {}).get(f"{stage}_status")
                self._record(
                    "job.finished",
                    {
                        "job_type": "migration",
                        "stage": stage,
                        "job_id": job["job_id"],
                        "status": stage_status,
                    },
                )
                if stage_status == "discarded":
                    self._record(
                        "job.discarded",
                        {
                            "job_type": "migration",
                            "stage": stage,
                            "job_id": job["job_id"],
                            "attempts": (status or {}).get(f"{stage}_attempts"),
                            "last_error": (status or {}).get(f"{stage}_last_error"),
                        },
                    )
                return True
        finally:
            self._end_manual_call()

    def _process_claimed_profile(self, job_id: Optional[str] = None) -> bool:
        if not self._begin_manual_call():
            return False
        try:
            with self._manual_locks["profile"]:
                job = (
                    self.db.claim_profile_job(job_id, self.config.lease_timeout_seconds)
                    if job_id is not None
                    else self.db.claim_next_profile_job(self.config.lease_timeout_seconds)
                )
                if not isinstance(job, dict):
                    return False
                self._record("job.started", {"job_type": "profile", "job_id": job["job_id"]})
                try:
                    self._run_profile_job(job)
                except Exception as exc:
                    self._record_unexpected_profile_failure(job, exc)
                status = self.db.get_background_job(job["job_id"], "profile")
                profile_status = (status or {}).get("status")
                self._record(
                    "job.finished",
                    {
                        "job_type": "profile",
                        "job_id": job["job_id"],
                        "status": profile_status,
                    },
                )
                if profile_status == "discarded":
                    last_error = (status or {}).get("last_error")
                    if isinstance(last_error, str) and ": " in last_error:
                        last_error = last_error.split(": ", 1)[1]
                    self._record(
                        "job.discarded",
                        {
                            "job_type": "profile",
                            "job_id": job["job_id"],
                            "attempts": (status or {}).get("attempts"),
                            "last_error": last_error,
                        },
                    )
                return True
        finally:
            self._end_manual_call()

    def _process_claimed_longterm_extraction(self, job_id: Optional[str] = None) -> bool:
        """Manually run one real fine-grained extraction job with core leases."""
        if not self._begin_manual_call():
            return False
        heartbeat = None
        try:
            with self._manual_locks["longterm_extraction"]:
                job = (
                    self.db.claim_longterm_extraction_job(job_id, self.config.lease_timeout_seconds)
                    if job_id is not None
                    else self.db.claim_next_longterm_extraction_job(self.config.lease_timeout_seconds)
                )
                if not isinstance(job, dict):
                    return False
                self._record(
                    "job.started",
                    {"job_type": "longterm_extraction", "job_id": job["job_id"]},
                )
                heartbeat = LeaseHeartbeat(
                    heartbeat=lambda: self.db.heartbeat_longterm_extraction_job(
                        job["job_id"],
                        job["lease_token"],
                        self.config.lease_timeout_seconds,
                    ),
                    interval_seconds=self.config.heartbeat_interval_seconds,
                    name=f"demo-longterm-extraction-heartbeat-{job['job_id']}",
                )
                heartbeat.start()
                try:
                    self.process_longterm_extraction(job)
                    if self.db.longterm_extraction_job_lease_is_current(job["job_id"], job["lease_token"]):
                        self.commit_longterm_extraction_outputs(job, job["lease_token"])
                        if not self.db.complete_longterm_extraction_job(job["job_id"], job["lease_token"]):
                            self.discard_longterm_extraction_outputs(job, job["lease_token"])
                    else:
                        self.discard_longterm_extraction_outputs(job, job["lease_token"])
                except Exception as exc:
                    try:
                        self.discard_longterm_extraction_outputs(job, job["lease_token"])
                    except Exception:
                        logger.exception("Demo extraction output cleanup failed job_id=%s", job.get("job_id"))
                    attempt = int(job.get("attempts", 0)) + 1
                    retryable = getattr(exc, "retryable", None)
                    if retryable is None:
                        retryable = not isinstance(exc, (TypeError, ValueError))
                    self.db.record_longterm_extraction_failure(
                        job["job_id"],
                        job["lease_token"],
                        f"{type(exc).__name__}: {exc}",
                        max_retries=int(self.config.max_retries) if retryable else 0,
                        retry_delay_seconds=self._retry_delay(attempt),
                    )
                    self._record(
                        "job.failed",
                        {
                            "job_type": "longterm_extraction",
                            "job_id": job["job_id"],
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                status = self.db.get_background_job(job["job_id"], "longterm_extraction")
                self._record(
                    "job.finished",
                    {
                        "job_type": "longterm_extraction",
                        "job_id": job["job_id"],
                        "status": (status or {}).get("status"),
                    },
                )
                if (status or {}).get("status") == "discarded":
                    self._record(
                        "job.discarded",
                        {
                            "job_type": "longterm_extraction",
                            "job_id": job["job_id"],
                            "attempts": (status or {}).get("attempts"),
                            "last_error": (status or {}).get("last_error"),
                        },
                    )
                return True
        finally:
            if heartbeat is not None:
                self._stop_heartbeat(heartbeat)
            self._end_manual_call()

    def _process_claimed_promotion(self, job_id: Optional[str] = None) -> bool:
        """Manually run one real cross-session promotion job with core leases."""
        self._last_manual_results["promotion"] = None
        if not self._begin_manual_call():
            return False
        heartbeat = None
        try:
            with self._manual_locks["promotion"]:
                job = (
                    self.db.claim_promotion_job(job_id, self.config.lease_timeout_seconds)
                    if job_id is not None
                    else self.db.claim_next_promotion_job(self.config.lease_timeout_seconds)
                )
                if not isinstance(job, dict):
                    return False
                self._record("job.started", {"job_type": "promotion", "job_id": job["job_id"]})
                heartbeat = LeaseHeartbeat(
                    heartbeat=lambda: self.db.heartbeat_promotion_job(
                        job["job_id"],
                        job["lease_token"],
                        self.config.lease_timeout_seconds,
                    ),
                    interval_seconds=self.config.heartbeat_interval_seconds,
                    name=f"demo-promotion-heartbeat-{job['job_id']}",
                )
                heartbeat.start()
                try:
                    discard_reason = self.process_promotion(job)
                    if discard_reason:
                        self.db.discard_promotion_job(job["job_id"], job["lease_token"], str(discard_reason))
                    elif not self.db.complete_promotion_job(job["job_id"], job["lease_token"]):
                        logger.info("Demo promotion job lease became stale job_id=%s", job["job_id"])
                except Exception as exc:
                    attempt = int(job.get("attempts", 0)) + 1
                    retryable = getattr(exc, "retryable", None)
                    if retryable is None:
                        retryable = not isinstance(exc, (TypeError, ValueError))
                    self.db.retry_promotion_job(
                        job["job_id"],
                        job["lease_token"],
                        f"{type(exc).__name__}: {exc}",
                        max_retries=int(self.config.max_retries) if retryable else 0,
                        retry_delay_seconds=self._retry_delay(attempt),
                    )
                    self._record(
                        "job.failed",
                        {
                            "job_type": "promotion",
                            "job_id": job["job_id"],
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                status = self.db.get_background_job(job["job_id"], "promotion")
                self._record(
                    "job.finished",
                    {
                        "job_type": "promotion",
                        "job_id": job["job_id"],
                        "status": (status or {}).get("status"),
                    },
                )
                if (status or {}).get("status") == "discarded":
                    self._record(
                        "job.discarded",
                        {
                            "job_type": "promotion",
                            "job_id": job["job_id"],
                            "attempts": (status or {}).get("attempts"),
                            "last_error": (status or {}).get("last_error"),
                        },
                    )
                self._last_manual_results["promotion"] = {
                    "job_type": "promotion",
                    "job_id": job["job_id"],
                    "source_midterm_session_id": job.get("source_midterm_session_id"),
                    "source_run_id": job.get("source_run_id"),
                    "status": (status or {}).get("status"),
                    "processed": True,
                    "queue_scope": "core_promotion_queue",
                }
                return True
        finally:
            if heartbeat is not None:
                self._stop_heartbeat(heartbeat)
            self._end_manual_call()

    def _record_unexpected_stage_failure(self, job: Dict[str, Any], stage: str, exc: Exception) -> None:
        logger.exception("Unexpected demo migration stage failure job_id=%s stage=%s", job.get("job_id"), stage)
        handler = self.process_midterm if stage == "midterm" else self.process_longterm
        self._persist_unexpected_stage_failure(job, stage, handler, exc)
        self._record(
            "job.failed",
            {
                "job_type": "migration",
                "stage": stage,
                "job_id": job["job_id"],
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )

    def _record_unexpected_profile_failure(self, job: Dict[str, Any], exc: Exception) -> None:
        logger.exception("Unexpected demo profile job failure job_id=%s", job.get("job_id"))
        attempt = int(job.get("attempts", 0)) + 1
        self.db.record_profile_failure(
            job["job_id"],
            job["lease_token"],
            f"unexpected profile worker failure: {exc}",
            max_retries=int(self.config.max_retries),
            retry_delay_seconds=self._retry_delay(attempt),
        )
        self._record(
            "job.failed",
            {
                "job_type": "profile",
                "job_id": job["job_id"],
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )

    def _record(self, event_type: str, payload: Dict[str, Any]) -> None:
        if self._event_recorder is not None:
            try:
                self._event_recorder(event_type, payload)
            except Exception:
                logger.warning("Demo event recorder failed for event_type=%s", event_type, exc_info=True)
