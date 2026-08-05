from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from mem0.configs.base import BackgroundTaskConfig

logger = logging.getLogger(__name__)

MigrationHandler = Callable[[Dict[str, Any], List[Dict[str, Any]], bool], None]
ProfileHandler = Callable[[Dict[str, Any]], Optional[bool]]
CommitOutputsHandler = Callable[[Dict[str, Any], str, str, bool], None]
DiscardOutputsHandler = Callable[[Dict[str, Any], str, str], Optional[str]]
StartupCleanupHandler = Callable[[], Dict[str, Any]]


class LeaseHeartbeat:
    """Extend one claimed job lease with short fenced SQLite updates."""

    def __init__(
        self,
        *,
        heartbeat: Callable[[], bool],
        interval_seconds: float,
        name: str,
    ):
        self._heartbeat = heartbeat
        self._interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                if not self._heartbeat():
                    return
            except Exception:
                logger.exception("Failed to extend background job lease")
                return

    def request_stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def stop(self, timeout: Optional[float] = None) -> None:
        self.request_stop()
        self.join(timeout)

    def is_alive(self) -> bool:
        return self._thread.is_alive()


class BackgroundWorkerManager:
    """Run independent persistent workers for midterm, longterm, and profile jobs."""

    def __init__(
        self,
        db,
        config: Optional[BackgroundTaskConfig],
        *,
        process_midterm: MigrationHandler,
        process_longterm: MigrationHandler,
        process_profile: ProfileHandler,
        commit_migration_outputs: Optional[CommitOutputsHandler] = None,
        discard_migration_outputs: Optional[DiscardOutputsHandler] = None,
        startup_cleanup: Optional[StartupCleanupHandler] = None,
    ):
        self.db = db
        self.config = config or BackgroundTaskConfig()
        self.process_midterm = process_midterm
        self.process_longterm = process_longterm
        self.process_profile = process_profile
        self.commit_migration_outputs = commit_migration_outputs or (lambda job, stage, token, degraded: None)
        self.discard_migration_outputs = discard_migration_outputs or (lambda job, stage, token: None)
        self.startup_cleanup = startup_cleanup
        self._stop_event = threading.Event()
        self._midterm_wakeup = threading.Event()
        self._longterm_wakeup = threading.Event()
        self._profile_wakeup = threading.Event()
        self._threads: List[threading.Thread] = []
        self._watchdog_thread: Optional[threading.Thread] = None
        self._heartbeats: set[LeaseHeartbeat] = set()
        self._heartbeats_lock = threading.Lock()
        self._started = False
        self._state_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def start(self) -> None:
        if not self.enabled:
            return
        with self._state_lock:
            if self._started:
                return
            recovered = self.db.recover_expired_background_leases(self.config.max_stale_recoveries)
            logger.info(
                "recovered stale migration stages migration=%s midterm=%s longterm=%s",
                recovered.get("migration", 0),
                recovered.get("midterm", 0),
                recovered.get("longterm", 0),
            )
            logger.info("recovered stale profile jobs profile=%s", recovered.get("profile", 0))
            if self.startup_cleanup is not None:
                try:
                    cleaned = self.startup_cleanup()
                    logger.info(
                        "cleaned orphan staging outputs midterm=%s longterm=%s cleanup_errors=%s",
                        cleaned.get("midterm_discarded", 0),
                        cleaned.get("longterm_discarded", 0),
                        len(cleaned.get("cleanup_errors", [])),
                    )
                except Exception:
                    logger.exception("Failed to clean orphan staging outputs during startup")
            self._stop_event.clear()
            self._threads = [
                threading.Thread(
                    target=self._migration_stage_loop,
                    args=("midterm", self._midterm_wakeup, self.process_midterm),
                    name="mem0-midterm-memory-worker",
                    daemon=True,
                ),
                threading.Thread(
                    target=self._migration_stage_loop,
                    args=("longterm", self._longterm_wakeup, self.process_longterm),
                    name="mem0-longterm-memory-worker",
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
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="mem0-background-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            logger.info("background workers started")

    def _watchdog_loop(self) -> None:
        while not self._stop_event.wait(float(self.config.watchdog_interval_seconds)):
            if getattr(self.db, "connection", None) is None:
                return
            
            try:
                recovered = self.db.recover_expired_background_leases(self.config.max_stale_recoveries)
                if any(recovered.values()):
                    self.wake_all()
            except Exception:
                logger.exception("Failed to recover expired background leases")

    def _start_migration_heartbeat(self, job: Dict[str, Any], stage: str) -> LeaseHeartbeat:
        token = job[f"{stage}_lease_token"]
        heartbeat = LeaseHeartbeat(
            heartbeat=lambda: self.db.heartbeat_migration_stage(
                job["job_id"],
                stage,
                token,
                self.config.lease_timeout_seconds,
            ),
            interval_seconds=self.config.heartbeat_interval_seconds,
            name=f"mem0-{stage}-heartbeat-{job['job_id']}",
        )
        with self._heartbeats_lock:
            self._heartbeats = {item for item in self._heartbeats if item.is_alive()}
            self._heartbeats.add(heartbeat)
            heartbeat.start()
        return heartbeat

    def _start_profile_heartbeat(self, job: Dict[str, Any]) -> LeaseHeartbeat:
        token = job["lease_token"]
        heartbeat = LeaseHeartbeat(
            heartbeat=lambda: self.db.heartbeat_profile_job(
                job["job_id"],
                token,
                self.config.lease_timeout_seconds,
            ),
            interval_seconds=self.config.heartbeat_interval_seconds,
            name=f"mem0-profile-heartbeat-{job['job_id']}",
        )
        with self._heartbeats_lock:
            self._heartbeats = {item for item in self._heartbeats if item.is_alive()}
            self._heartbeats.add(heartbeat)
            heartbeat.start()
        return heartbeat

    def _stop_heartbeat(self, heartbeat: LeaseHeartbeat) -> None:
        heartbeat.request_stop()
        heartbeat.join(0)
        if not heartbeat.is_alive():
            with self._heartbeats_lock:
                self._heartbeats.discard(heartbeat)

    def wake_midterm(self) -> None:
        if self.enabled:
            self._midterm_wakeup.set()

    def wake_longterm(self) -> None:
        if self.enabled:
            self._longterm_wakeup.set()

    def wake_migration(self) -> None:
        """Compatibility wake-up for both independent migration stages."""
        self.wake_midterm()
        self.wake_longterm()

    def wake_profile(self) -> None:
        if self.enabled:
            self._profile_wakeup.set()

    def wake_all(self) -> None:
        self.wake_migration()
        self.wake_profile()

    def _wait(self, wakeup: threading.Event) -> None:
        wakeup.wait(timeout=float(self.config.poll_interval_seconds))
        wakeup.clear()

    def _retry_delay(self, attempt: int) -> float:
        delays = self.config.retry_delays_seconds
        if not delays:
            return 0
        index = min(max(attempt - 1, 0), len(delays) - 1)
        return delays[index]

    def _migration_stage_loop(
        self,
        stage: str,
        wakeup: threading.Event,
        handler: MigrationHandler,
    ) -> None:
        while not self._stop_event.is_set():
            if getattr(self.db, "connection", None) is None:
                return
            try:
                job = self.db.claim_next_migration_stage(stage, self.config.lease_timeout_seconds)
            except Exception:
                logger.exception("Failed to claim a migration stage stage=%s", stage)
                self._wait(wakeup)
                continue
            if not isinstance(job, dict):
                self._wait(wakeup)
                continue
            try:
                self._run_migration_stage(job, stage, handler)
            except Exception as exc:
                logger.exception(
                    "Unexpected migration stage failure job_id=%s session_scope=%s stage=%s "
                    "attempt=%s worker=%s error=%s",
                    job.get("job_id"),
                    job.get("session_scope"),
                    stage,
                    int(job.get(f"{stage}_attempts", 0)) + 1,
                    threading.current_thread().name,
                    exc,
                )
                self._persist_unexpected_stage_failure(job, stage, handler, exc)

    def _profile_loop(self) -> None:
        while not self._stop_event.is_set():
            if getattr(self.db, "connection", None) is None:
                return
            try:
                job = self.db.claim_next_profile_job(self.config.lease_timeout_seconds)
            except Exception:
                logger.exception("Failed to claim a profile update job")
                self._wait(self._profile_wakeup)
                continue
            if not isinstance(job, dict):
                self._wait(self._profile_wakeup)
                continue
            try:
                self._run_profile_job(job)
            except Exception as exc:
                logger.exception(
                    "Unexpected profile worker failure job_id=%s user_id=%s attempt=%s worker=%s error=%s",
                    job.get("job_id"),
                    job.get("user_id"),
                    int(job.get("attempts", 0)) + 1,
                    threading.current_thread().name,
                    exc,
                )
                try:
                    self.db.record_profile_failure(
                        job["job_id"],
                        job["lease_token"],
                        f"unexpected profile worker failure: {exc}",
                        max_retries=int(self.config.max_retries),
                        retry_delay_seconds=self._retry_delay(int(job.get("attempts", 0)) + 1),
                    )
                except Exception:
                    logger.exception("Failed to persist unexpected profile worker failure")

    def _record_migration_stage_failure(self, job: Dict[str, Any], stage: str, exc: Exception) -> str:
        job_id = job["job_id"]
        attempt = int(job.get(f"{stage}_attempts", 0)) + 1
        logger.warning(
            "Background migration stage failed job_id=%s job_type=migration session_scope=%s stage=%s "
            "attempts=%s recovery_count=%s lease_token=%s worker=%s last_error=%s",
            job_id,
            job.get("session_scope"),
            stage,
            attempt,
            job.get(f"{stage}_recovery_count", 0),
            str(job.get(f"{stage}_lease_token") or "")[:8],
            threading.current_thread().name,
            exc,
        )
        return self.db.record_migration_stage_failure(
            job_id,
            stage,
            job[f"{stage}_lease_token"],
            str(exc),
            max_retries=int(self.config.max_retries),
            retry_delay_seconds=self._retry_delay(attempt),
        )

    def _discard_outputs(self, job: Dict[str, Any], stage: str, lease_token: str) -> Optional[str]:
        try:
            return self.discard_migration_outputs(job, stage, lease_token)
        except Exception as exc:
            logger.exception(
                "Failed to clean up discarded stage outputs job_id=%s job_type=migration stage=%s "
                "session_scope=%s attempts=%s recovery_count=%s lease_token=%s cleanup_error=%s",
                job.get("job_id"),
                stage,
                job.get("session_scope"),
                job.get(f"{stage}_attempts", 0),
                job.get(f"{stage}_recovery_count", 0),
                str(lease_token or "")[:8],
                exc,
            )
            return f"{type(exc).__name__}: {exc}"

    def _run_degraded_migration_stage(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        messages: List[Dict[str, Any]],
    ) -> bool:
        job_id = job["job_id"]
        lease_token = job[f"{stage}_lease_token"]
        try:
            handler(job, messages, True)
            if not self.db.migration_stage_lease_is_current(job_id, stage, lease_token):
                self._discard_outputs(job, stage, lease_token)
                return False
            self.commit_migration_outputs(job, stage, lease_token, True)
            if not self.db.mark_migration_stage_succeeded(
                job_id,
                stage,
                lease_token,
                degraded=True,
            ):
                self._discard_outputs(job, stage, lease_token)
                return False
            logger.warning(
                "Background stage used degraded storage job_id=%s session_scope=%s stage=%s worker=%s",
                job_id,
                job.get("session_scope"),
                stage,
                threading.current_thread().name,
            )
            return True
        except Exception as degraded_exc:
            logger.error(
                "Background stage degradation failed job_id=%s session_scope=%s stage=%s attempt=%s "
                "worker=%s error=%s",
                job_id,
                job.get("session_scope"),
                stage,
                int(job.get(f"{stage}_attempts", 0)) + 1,
                threading.current_thread().name,
                degraded_exc,
            )
            cleanup_error = self._discard_outputs(job, stage, lease_token)
            self.db.mark_migration_stage_discarded(
                job_id,
                stage,
                lease_token,
                f"degradation: {degraded_exc}",
                cleanup_error=cleanup_error,
            )
            return False

    def _handle_migration_stage_failure(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        messages: List[Dict[str, Any]],
        exc: Exception,
    ) -> bool:
        action = self._record_migration_stage_failure(job, stage, exc)
        if action == "stale_lease":
            self._discard_outputs(job, stage, job[f"{stage}_lease_token"])
            return False
        if action != "exhausted":
            return False
        return self._run_degraded_migration_stage(job, stage, handler, messages)

    def _persist_unexpected_stage_failure(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        exc: Exception,
    ) -> None:
        try:
            messages = self.db.get_migration_job_messages(job["job_id"])
            if not messages:
                lease_token = job[f"{stage}_lease_token"]
                cleanup_error = self._discard_outputs(job, stage, lease_token)
                self.db.mark_migration_stage_discarded(
                    job["job_id"],
                    stage,
                    lease_token,
                    "migration source messages are missing",
                    cleanup_error=cleanup_error,
                )
                return
            self._handle_migration_stage_failure(job, stage, handler, messages, exc)
        except Exception:
            logger.exception(
                "Failed to persist unexpected migration failure job_id=%s stage=%s",
                job.get("job_id"),
                stage,
            )

    def _run_migration_stage(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
    ) -> bool:
        job_id = job["job_id"]
        lease_token = job[f"{stage}_lease_token"]
        heartbeat = self._start_migration_heartbeat(job, stage)
        try:
            messages = self.db.get_migration_job_messages(job_id)
            if not messages:
                cleanup_error = self._discard_outputs(job, stage, lease_token)
                self.db.mark_migration_stage_discarded(
                    job_id,
                    stage,
                    lease_token,
                    "migration source messages are missing",
                    cleanup_error=cleanup_error,
                )
                return False
            
            if bool(job.get(f"{stage}_force_degraded")) or int(
                job.get(f"{stage}_attempts", 0)
            ) > int(self.config.max_retries):
                return self._run_degraded_migration_stage(job, stage, handler, messages)

            try:
                handler(job, messages, False)
                if not self.db.migration_stage_lease_is_current(job_id, stage, lease_token):
                    self._discard_outputs(job, stage, lease_token)
                    return False
                self.commit_migration_outputs(job, stage, lease_token, False)
            except Exception as exc:
                return self._handle_migration_stage_failure(job, stage, handler, messages, exc)
            
            if not self.db.mark_migration_stage_succeeded(job_id, stage, lease_token):
                self._discard_outputs(job, stage, lease_token)
                return False
            return True
        finally:
            self._stop_heartbeat(heartbeat)

    def _run_profile_job(self, job: Dict[str, Any]) -> None:
        heartbeat = self._start_profile_heartbeat(job)
        try:
            try:
                committed = self.process_profile(job)
                if committed is False:
                    logger.info(
                        "Background profile job abandoned because lease is stale "
                        "job_id=%s user_id=%s attempts=%s recovery_count=%s lease_token=%s",
                        job["job_id"],
                        job.get("user_id"),
                        job.get("attempts", 0),
                        job.get("recovery_count", 0),
                        str(job.get("lease_token") or "")[:8],
                    )
                    return
                
                if committed is None:
                    finished = self.db.finish_profile_job(job["job_id"], job["lease_token"])
                    if not finished:
                        logger.info(
                            "Background profile job abandoned because lease is stale "
                            "job_id=%s user_id=%s attempts=%s recovery_count=%s lease_token=%s",
                            job["job_id"],
                            job.get("user_id"),
                            job.get("attempts", 0),
                            job.get("recovery_count", 0),
                            str(job.get("lease_token") or "")[:8],
                        )

            except Exception as exc:
                attempt = int(job.get("attempts", 0)) + 1
                logger.warning(
                    "Background profile update failed job_id=%s job_type=profile user_id=%s attempts=%s "
                    "recovery_count=%s lease_token=%s worker=%s last_error=%s",
                    job["job_id"],
                    job.get("user_id"),
                    attempt,
                    job.get("recovery_count", 0),
                    str(job.get("lease_token") or "")[:8],
                    threading.current_thread().name,
                    exc,
                )
                
                self.db.record_profile_failure(
                    job["job_id"],
                    job["lease_token"],
                    str(exc),
                    max_retries=int(self.config.max_retries),
                    retry_delay_seconds=self._retry_delay(attempt),
                )
        finally:
            self._stop_heartbeat(heartbeat)

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Wait until all runnable, delayed, or running stages and profile jobs finish."""
        if not self.enabled:
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
        deadline = None if timeout is None else time.monotonic() + max(float(timeout), 0)
        with self._state_lock:
            if not self._started:
                return True
            self._stop_event.set()
            self._midterm_wakeup.set()
            self._longterm_wakeup.set()
            self._profile_wakeup.set()
            threads = list(self._threads)
            watchdog_thread = self._watchdog_thread
            with self._heartbeats_lock:
                heartbeats = list(self._heartbeats)

        for heartbeat in heartbeats:
            heartbeat.request_stop()

        if wait:
            for thread in threads:
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                thread.join(remaining)
            if watchdog_thread is not None:
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                watchdog_thread.join(remaining)
            with self._heartbeats_lock:
                heartbeats = list(self._heartbeats)
            for heartbeat in heartbeats:
                heartbeat.request_stop()
                remaining = None if deadline is None else max(deadline - time.monotonic(), 0)
                heartbeat.join(remaining)

        stopped = not any(thread.is_alive() for thread in threads)
        stopped = stopped and (watchdog_thread is None or not watchdog_thread.is_alive())
        with self._heartbeats_lock:
            active_heartbeats = list(self._heartbeats)
        stopped = stopped and not any(heartbeat.is_alive() for heartbeat in active_heartbeats)
        if stopped:
            with self._state_lock:
                self._threads = []
                self._watchdog_thread = None
                self._started = False
            with self._heartbeats_lock:
                self._heartbeats.clear()
        return stopped

    def threads_alive(self) -> bool:
        watchdog_alive = self._watchdog_thread is not None and self._watchdog_thread.is_alive()
        with self._heartbeats_lock:
            heartbeat_alive = any(heartbeat.is_alive() for heartbeat in self._heartbeats)
        return any(thread.is_alive() for thread in self._threads) or watchdog_alive or heartbeat_alive
