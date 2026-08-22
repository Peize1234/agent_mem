from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from mem0.configs.base import BackgroundTaskConfig

logger = logging.getLogger(__name__)

MigrationHandler = Callable[[Dict[str, Any], List[Dict[str, Any]], bool], None]
ProfileHandler = Callable[[Dict[str, Any]], Optional[bool]]
PromotionHandler = Callable[[Dict[str, Any]], Optional[str]]
CommitOutputsHandler = Callable[[Dict[str, Any], str, str, bool], None]
DiscardOutputsHandler = Callable[[Dict[str, Any], str, str], Optional[str]]
StartupCleanupHandler = Callable[[], Dict[str, Any]]
AsyncMigrationHandler = Callable[[Dict[str, Any], List[Dict[str, Any]], bool], Awaitable[None]]
AsyncProfileHandler = Callable[[Dict[str, Any]], Awaitable[Optional[bool]]]
AsyncPromotionHandler = Callable[[Dict[str, Any]], Awaitable[Optional[str]]]
AsyncCommitOutputsHandler = Callable[[Dict[str, Any], str, str, bool], Awaitable[None]]
AsyncDiscardOutputsHandler = Callable[[Dict[str, Any], str, str], Awaitable[Optional[str]]]
LongtermExtractionHandler = Callable[[Dict[str, Any]], None]
AsyncLongtermExtractionHandler = Callable[[Dict[str, Any]], Awaitable[None]]
LongtermExtractionCommitHandler = Callable[[Dict[str, Any], str], None]
LongtermExtractionDiscardHandler = Callable[[Dict[str, Any], str], Optional[str]]


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
    """Run independent persistent workers for migration, profile, and promotion jobs."""

    def __init__(
        self,
        db,
        config: Optional[BackgroundTaskConfig],
        *,
        process_midterm: MigrationHandler,
        process_longterm: MigrationHandler,
        process_profile: ProfileHandler,
        process_promotion: Optional[PromotionHandler] = None,
        process_midterm_async: Optional[AsyncMigrationHandler] = None,
        process_longterm_async: Optional[AsyncMigrationHandler] = None,
        process_profile_async: Optional[AsyncProfileHandler] = None,
        process_promotion_async: Optional[AsyncPromotionHandler] = None,
        commit_migration_outputs: Optional[CommitOutputsHandler] = None,
        commit_migration_outputs_async: Optional[AsyncCommitOutputsHandler] = None,
        discard_migration_outputs: Optional[DiscardOutputsHandler] = None,
        discard_migration_outputs_async: Optional[AsyncDiscardOutputsHandler] = None,
        startup_cleanup: Optional[StartupCleanupHandler] = None,
        process_longterm_extraction: Optional[LongtermExtractionHandler] = None,
        process_longterm_extraction_async: Optional[AsyncLongtermExtractionHandler] = None,
        commit_longterm_extraction_outputs: Optional[LongtermExtractionCommitHandler] = None,
        discard_longterm_extraction_outputs: Optional[LongtermExtractionDiscardHandler] = None,
    ):
        self.db = db
        self.config = config or BackgroundTaskConfig()
        self.process_midterm = process_midterm
        self.process_longterm = process_longterm
        self.process_profile = process_profile
        self.process_promotion = process_promotion
        self.process_midterm_async = process_midterm_async
        self.process_longterm_async = process_longterm_async
        self.process_profile_async = process_profile_async
        self.process_promotion_async = process_promotion_async
        self.commit_migration_outputs = commit_migration_outputs or (lambda job, stage, token, degraded: None)
        self.commit_migration_outputs_async = commit_migration_outputs_async
        self.discard_migration_outputs = discard_migration_outputs or (lambda job, stage, token: None)
        self.discard_migration_outputs_async = discard_migration_outputs_async
        self.startup_cleanup = startup_cleanup
        self.process_longterm_extraction = process_longterm_extraction
        self.process_longterm_extraction_async = process_longterm_extraction_async
        self.commit_longterm_extraction_outputs = commit_longterm_extraction_outputs or (lambda job, token: None)
        self.discard_longterm_extraction_outputs = discard_longterm_extraction_outputs or (lambda job, token: None)
        self._stop_event = threading.Event()
        self._midterm_wakeup = threading.Event()
        self._longterm_wakeup = threading.Event()
        self._profile_wakeup = threading.Event()
        self._promotion_wakeup = threading.Event()
        self._longterm_extraction_wakeup = threading.Event()
        self._threads: List[threading.Thread] = []
        self._watchdog_thread: Optional[threading.Thread] = None
        self._heartbeats: set[LeaseHeartbeat] = set()
        self._heartbeats_lock = threading.Lock()
        self._async_wakeups: Dict[str, List[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {
            "midterm": [],
            "longterm": [],
            "profile": [],
            "promotion": [],
            "longterm_extraction": [],
        }
        self._async_wakeups_lock = threading.Lock()
        self._started = False
        self._state_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    @staticmethod
    def _worker_name(single_name: str, numbered_prefix: str, index: int, count: int) -> str:
        if count == 1:
            return single_name
        return f"{numbered_prefix}-{index}"

    def _build_worker_threads(self) -> List[threading.Thread]:
        threads = []
        for index in range(1, int(self.config.midterm_worker_count) + 1):
            threads.append(
                threading.Thread(
                    target=self._migration_stage_loop,
                    args=(
                        "midterm",
                        self._midterm_wakeup,
                        self.process_midterm,
                        self.process_midterm_async,
                        int(self.config.midterm_worker_concurrency),
                    ),
                    name=self._worker_name(
                        "mem0-midterm-memory-worker",
                        "mem0-midterm-memory-worker",
                        index,
                        int(self.config.midterm_worker_count),
                    ),
                    daemon=True,
                )
            )
        for index in range(1, int(self.config.longterm_worker_count) + 1):
            threads.append(
                threading.Thread(
                    target=self._migration_stage_loop,
                    args=(
                        "longterm",
                        self._longterm_wakeup,
                        self.process_longterm,
                        self.process_longterm_async,
                        int(self.config.longterm_worker_concurrency),
                    ),
                    name=self._worker_name(
                        "mem0-longterm-memory-worker",
                        "mem0-longterm-memory-worker",
                        index,
                        int(self.config.longterm_worker_count),
                    ),
                    daemon=True,
                )
            )
        if self.process_longterm_extraction is not None:
            for index in range(1, int(self.config.longterm_worker_count) + 1):
                threads.append(
                    threading.Thread(
                        target=self._longterm_extraction_loop,
                        args=(int(self.config.longterm_worker_concurrency),),
                        name=self._worker_name(
                            "mem0-fine-grained-longterm-worker",
                            "mem0-fine-grained-longterm-worker",
                            index,
                            int(self.config.longterm_worker_count),
                        ),
                        daemon=True,
                    )
                )
        for index in range(1, int(self.config.profile_worker_count) + 1):
            threads.append(
                threading.Thread(
                    target=self._profile_loop,
                    args=(int(self.config.profile_worker_concurrency),),
                    name=self._worker_name(
                        "mem0-profile-update-worker",
                        "mem0-user-profile-worker",
                        index,
                        int(self.config.profile_worker_count),
                    ),
                    daemon=True,
                )
            )
        if self.process_promotion is not None:
            for index in range(1, int(self.config.promotion_worker_count) + 1):
                threads.append(
                    threading.Thread(
                        target=self._promotion_loop,
                        args=(int(self.config.promotion_worker_concurrency),),
                        name=self._worker_name(
                            "mem0-promotion-worker",
                            "mem0-promotion-worker",
                            index,
                            int(self.config.promotion_worker_count),
                        ),
                        daemon=True,
                    )
                )
        return threads

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
            logger.info(
                "recovered stale fine-grained longterm jobs longterm_extraction=%s",
                recovered.get("longterm_extraction", 0),
            )
            logger.info("recovered stale promotion jobs promotion=%s", recovered.get("promotion", 0))
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
            self._threads = self._build_worker_threads()
            self._started = True
            for thread in self._threads:
                thread.start()
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                name="mem0-background-watchdog",
                daemon=True,
            )
            self._watchdog_thread.start()
            logger.info(
                "background workers started midterm=%s x %s longterm=%s x %s profile=%s x %s promotion=%s x %s",
                self.config.midterm_worker_count,
                self.config.midterm_worker_concurrency,
                self.config.longterm_worker_count,
                self.config.longterm_worker_concurrency,
                self.config.profile_worker_count,
                self.config.profile_worker_concurrency,
                self.config.promotion_worker_count if self.process_promotion is not None else 0,
                self.config.promotion_worker_concurrency,
            )

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

    def _register_async_wakeup(self, queue_name: str, event: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        with self._async_wakeups_lock:
            self._async_wakeups[queue_name].append((loop, event))

    def _unregister_async_wakeup(self, queue_name: str, event: asyncio.Event) -> None:
        with self._async_wakeups_lock:
            self._async_wakeups[queue_name] = [
                item for item in self._async_wakeups[queue_name] if item[1] is not event
            ]

    def _notify_async_workers(self, queue_name: str) -> None:
        with self._async_wakeups_lock:
            wakeups = list(self._async_wakeups[queue_name])
        for loop, event in wakeups:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                # The worker unregisters the event while its loop is closing.
                continue

    def wake_midterm(self) -> None:
        if self.enabled:
            self._midterm_wakeup.set()
            self._notify_async_workers("midterm")

    def wake_longterm(self) -> None:
        if self.enabled:
            self._longterm_wakeup.set()
            self._notify_async_workers("longterm")
            self._longterm_extraction_wakeup.set()
            self._notify_async_workers("longterm_extraction")

    def wake_migration(self) -> None:
        """Compatibility wake-up for both independent migration stages."""
        self.wake_midterm()
        self.wake_longterm()

    def wake_profile(self) -> None:
        if self.enabled:
            self._profile_wakeup.set()
            self._notify_async_workers("profile")

    def wake_promotion(self) -> None:
        if self.enabled and self.process_promotion is not None:
            self._promotion_wakeup.set()
            self._notify_async_workers("promotion")

    def wake_all(self) -> None:
        self.wake_migration()
        self.wake_profile()
        self.wake_promotion()

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
        async_handler: Optional[AsyncMigrationHandler],
        concurrency: int,
    ) -> None:
        asyncio.run(
            self._migration_stage_event_loop(
                stage,
                wakeup,
                handler,
                async_handler,
                concurrency,
            )
        )

    async def _migration_stage_event_loop(
        self,
        stage: str,
        wakeup: threading.Event,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
        concurrency: int,
    ) -> None:
        wake_event = asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        self._register_async_wakeup(stage, wake_event)
        try:
            while not self._stop_event.is_set():
                if getattr(self.db, "connection", None) is None:
                    break
                wake_event.clear()
                wakeup.clear()
                claimed_any = False
                while len(in_flight) < concurrency and not self._stop_event.is_set():
                    try:
                        job = self.db.claim_next_migration_stage(stage, self.config.lease_timeout_seconds)
                    except Exception:
                        logger.exception("Failed to claim a migration stage stage=%s", stage)
                        break
                    if not isinstance(job, dict):
                        break
                    claimed_any = True
                    in_flight.add(
                        asyncio.create_task(
                            self._execute_migration_stage(job, stage, handler, async_handler),
                            name=f"mem0-{stage}-job-{job['job_id']}",
                        )
                    )

                if self._stop_event.is_set():
                    break
                if len(in_flight) >= concurrency:
                    await self._reap_completed(in_flight, timeout=None)
                elif in_flight:
                    await self._wait_for_progress(in_flight, wake_event)
                elif not claimed_any:
                    await self._wait_for_wakeup(wake_event)
        finally:
            self._unregister_async_wakeup(stage, wake_event)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    async def _execute_migration_stage(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
    ) -> None:
        try:
            await self._run_migration_stage_async(job, stage, handler, async_handler)
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
            await self._persist_unexpected_stage_failure_async(job, stage, handler, async_handler, exc)

    def _longterm_extraction_loop(self, concurrency: int) -> None:
        asyncio.run(self._longterm_extraction_event_loop(concurrency))

    async def _longterm_extraction_event_loop(self, concurrency: int) -> None:
        wake_event = asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        self._register_async_wakeup("longterm_extraction", wake_event)
        try:
            while not self._stop_event.is_set():
                if getattr(self.db, "connection", None) is None:
                    break
                wake_event.clear()
                self._longterm_extraction_wakeup.clear()
                claimed_any = False
                while len(in_flight) < concurrency and not self._stop_event.is_set():
                    try:
                        job = self.db.claim_next_longterm_extraction_job(self.config.lease_timeout_seconds)
                    except Exception:
                        logger.exception("Failed to claim a fine-grained LongTerm extraction job")
                        break
                    if not isinstance(job, dict):
                        break
                    claimed_any = True
                    in_flight.add(
                        asyncio.create_task(
                            self._run_longterm_extraction_job_async(job),
                            name=f"mem0-fine-grained-longterm-job-{job['job_id']}",
                        )
                    )
                if self._stop_event.is_set():
                    break
                if len(in_flight) >= concurrency:
                    await self._reap_completed(in_flight, timeout=None)
                elif in_flight:
                    await self._wait_for_progress(in_flight, wake_event)
                elif not claimed_any:
                    await self._wait_for_wakeup(wake_event)
        finally:
            self._unregister_async_wakeup("longterm_extraction", wake_event)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    async def _longterm_extraction_heartbeat_async(
        self,
        job: Dict[str, Any],
        finished: asyncio.Event,
    ) -> None:
        while True:
            try:
                await asyncio.wait_for(finished.wait(), timeout=float(self.config.heartbeat_interval_seconds))
                return
            except asyncio.TimeoutError:
                try:
                    current = await asyncio.to_thread(
                        self.db.heartbeat_longterm_extraction_job,
                        job["job_id"],
                        job["lease_token"],
                        self.config.lease_timeout_seconds,
                    )
                    if not current:
                        return
                except Exception:
                    logger.exception("Failed to extend fine-grained LongTerm extraction lease")
                    return

    async def _run_longterm_extraction_job_async(self, job: Dict[str, Any]) -> None:
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._longterm_extraction_heartbeat_async(job, finished),
            name=f"mem0-fine-grained-longterm-heartbeat-{job['job_id']}",
        )
        lease_token = job["lease_token"]
        try:
            try:
                if self.process_longterm_extraction_async is not None:
                    result = self.process_longterm_extraction_async(job)
                elif self.process_longterm_extraction is not None:
                    result = await asyncio.to_thread(self.process_longterm_extraction, job)
                else:
                    raise RuntimeError("fine-grained LongTerm extraction handler is unavailable")
                if inspect.isawaitable(result):
                    await result
                if not self.db.longterm_extraction_job_lease_is_current(job["job_id"], lease_token):
                    await asyncio.to_thread(self.discard_longterm_extraction_outputs, job, lease_token)
                    return
                await asyncio.to_thread(self.commit_longterm_extraction_outputs, job, lease_token)
                if not self.db.complete_longterm_extraction_job(job["job_id"], lease_token):
                    await asyncio.to_thread(self.discard_longterm_extraction_outputs, job, lease_token)
            except Exception as exc:
                try:
                    await asyncio.to_thread(self.discard_longterm_extraction_outputs, job, lease_token)
                except Exception:
                    logger.exception(
                        "Failed to discard fine-grained LongTerm staging outputs job_id=%s",
                        job.get("job_id"),
                    )
                attempt = int(job.get("attempts", 0)) + 1
                retryable = getattr(exc, "retryable", None)
                if retryable is None:
                    retryable = not isinstance(exc, (TypeError, ValueError))
                action = self.db.record_longterm_extraction_failure(
                    job["job_id"],
                    lease_token,
                    f"{type(exc).__name__}: {exc}",
                    max_retries=int(self.config.max_retries) if retryable else 0,
                    retry_delay_seconds=self._retry_delay(attempt),
                )
                logger.warning(
                    "Fine-grained LongTerm extraction failed job_id=%s turn_index=%s attempt=%s action=%s error=%s",
                    job.get("job_id"),
                    job.get("turn_index"),
                    attempt,
                    action,
                    exc,
                )
        finally:
            finished.set()
            await heartbeat

    def _profile_loop(self, concurrency: int) -> None:
        asyncio.run(self._profile_event_loop(concurrency))

    async def _profile_event_loop(self, concurrency: int) -> None:
        wake_event = asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        self._register_async_wakeup("profile", wake_event)
        try:
            while not self._stop_event.is_set():
                if getattr(self.db, "connection", None) is None:
                    break
                wake_event.clear()
                self._profile_wakeup.clear()
                claimed_any = False
                while len(in_flight) < concurrency and not self._stop_event.is_set():
                    try:
                        job = self.db.claim_next_profile_job(self.config.lease_timeout_seconds)
                    except Exception:
                        logger.exception("Failed to claim a profile update job")
                        break
                    if not isinstance(job, dict):
                        break
                    claimed_any = True
                    in_flight.add(
                        asyncio.create_task(
                            self._execute_profile_job(job),
                            name=f"mem0-profile-job-{job['job_id']}",
                        )
                    )

                if self._stop_event.is_set():
                    break
                if len(in_flight) >= concurrency:
                    await self._reap_completed(in_flight, timeout=None)
                elif in_flight:
                    await self._wait_for_progress(in_flight, wake_event)
                elif not claimed_any:
                    await self._wait_for_wakeup(wake_event)
        finally:
            self._unregister_async_wakeup("profile", wake_event)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    async def _execute_profile_job(self, job: Dict[str, Any]) -> None:
        try:
            await self._run_profile_job_async(job)
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

    def _promotion_loop(self, concurrency: int) -> None:
        asyncio.run(self._promotion_event_loop(concurrency))

    async def _promotion_event_loop(self, concurrency: int) -> None:
        wake_event = asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        self._register_async_wakeup("promotion", wake_event)
        try:
            while not self._stop_event.is_set():
                if getattr(self.db, "connection", None) is None:
                    break
                wake_event.clear()
                self._promotion_wakeup.clear()
                claimed_any = False
                while len(in_flight) < concurrency and not self._stop_event.is_set():
                    try:
                        job = self.db.claim_next_promotion_job(self.config.lease_timeout_seconds)
                    except Exception:
                        logger.exception("Failed to claim a promotion job")
                        break
                    if not isinstance(job, dict):
                        break
                    claimed_any = True
                    in_flight.add(
                        asyncio.create_task(
                            self._execute_promotion_job(job),
                            name=f"mem0-promotion-job-{job['job_id']}",
                        )
                    )

                if self._stop_event.is_set():
                    break
                if len(in_flight) >= concurrency:
                    await self._reap_completed(in_flight, timeout=None)
                elif in_flight:
                    await self._wait_for_progress(in_flight, wake_event)
                elif not claimed_any:
                    await self._wait_for_wakeup(wake_event)
        finally:
            self._unregister_async_wakeup("promotion", wake_event)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    async def _execute_promotion_job(self, job: Dict[str, Any]) -> None:
        try:
            await self._run_promotion_job_async(job)
        except Exception as exc:
            logger.exception(
                "Unexpected promotion worker failure job_id=%s session_id=%s attempt=%s worker=%s error=%s",
                job.get("job_id"),
                job.get("source_midterm_session_id"),
                int(job.get("attempts", 0)) + 1,
                threading.current_thread().name,
                exc,
            )
            try:
                self.db.retry_promotion_job(
                    job["job_id"],
                    job["lease_token"],
                    f"unexpected promotion worker failure: {exc}",
                    max_retries=int(self.config.max_retries),
                    retry_delay_seconds=self._retry_delay(int(job.get("attempts", 0)) + 1),
                )
            except Exception:
                logger.exception("Failed to persist unexpected promotion worker failure")

    async def _wait_for_wakeup(self, wake_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(wake_event.wait(), timeout=float(self.config.poll_interval_seconds))
        except asyncio.TimeoutError:
            pass

    async def _wait_for_progress(self, in_flight: set[asyncio.Task], wake_event: asyncio.Event) -> None:
        wake_task = asyncio.create_task(wake_event.wait())
        done, _ = await asyncio.wait(
            [*in_flight, wake_task],
            timeout=float(self.config.poll_interval_seconds),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wake_task not in done:
            wake_task.cancel()
            await asyncio.gather(wake_task, return_exceptions=True)
        self._consume_done_tasks(in_flight, done)

    async def _reap_completed(self, in_flight: set[asyncio.Task], timeout: Optional[float]) -> None:
        done, _ = await asyncio.wait(in_flight, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        self._consume_done_tasks(in_flight, done)

    @staticmethod
    def _consume_done_tasks(in_flight: set[asyncio.Task], done) -> None:
        completed = in_flight.intersection(done)
        in_flight.difference_update(completed)
        for task in completed:
            try:
                task.result()
            except Exception:
                # Per-job runners already log and persist unexpected failures.
                logger.exception("Background job task escaped its failure handler")

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

    async def _invoke_migration_handler_async(
        self,
        job: Dict[str, Any],
        messages: List[Dict[str, Any]],
        degraded: bool,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
    ) -> None:
        if async_handler is not None:
            result = async_handler(job, messages, degraded)
        else:
            result = await asyncio.to_thread(handler, job, messages, degraded)
        if inspect.isawaitable(result):
            await result

    async def _commit_outputs_async(
        self,
        job: Dict[str, Any],
        stage: str,
        lease_token: str,
        degraded: bool,
    ) -> None:
        if self.commit_migration_outputs_async is not None:
            result = self.commit_migration_outputs_async(job, stage, lease_token, degraded)
        else:
            result = await asyncio.to_thread(
                self.commit_migration_outputs,
                job,
                stage,
                lease_token,
                degraded,
            )
        if inspect.isawaitable(result):
            await result

    async def _discard_outputs_async(
        self,
        job: Dict[str, Any],
        stage: str,
        lease_token: str,
    ) -> Optional[str]:
        try:
            if self.discard_migration_outputs_async is not None:
                result = self.discard_migration_outputs_async(job, stage, lease_token)
            else:
                result = await asyncio.to_thread(self.discard_migration_outputs, job, stage, lease_token)
            if inspect.isawaitable(result):
                return await result
            return result
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

    async def _migration_heartbeat_async(
        self,
        job: Dict[str, Any],
        stage: str,
        finished: asyncio.Event,
    ) -> None:
        token = job[f"{stage}_lease_token"]
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=float(self.config.heartbeat_interval_seconds),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self.db.heartbeat_migration_stage(
                    job["job_id"],
                    stage,
                    token,
                    self.config.lease_timeout_seconds,
                ):
                    return
            except Exception:
                logger.exception("Failed to extend background job lease")
                return

    async def _profile_heartbeat_async(self, job: Dict[str, Any], finished: asyncio.Event) -> None:
        token = job["lease_token"]
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=float(self.config.heartbeat_interval_seconds),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self.db.heartbeat_profile_job(
                    job["job_id"],
                    token,
                    self.config.lease_timeout_seconds,
                ):
                    return
            except Exception:
                logger.exception("Failed to extend background job lease")
                return

    async def _promotion_heartbeat_async(self, job: Dict[str, Any], finished: asyncio.Event) -> None:
        token = job["lease_token"]
        while not finished.is_set():
            try:
                await asyncio.wait_for(
                    finished.wait(),
                    timeout=float(self.config.heartbeat_interval_seconds),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                if not self.db.heartbeat_promotion_job(
                    job["job_id"],
                    token,
                    self.config.lease_timeout_seconds,
                ):
                    return
            except Exception:
                logger.exception("Failed to extend promotion job lease")
                return

    async def _run_degraded_migration_stage_async(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
        messages: List[Dict[str, Any]],
    ) -> bool:
        job_id = job["job_id"]
        lease_token = job[f"{stage}_lease_token"]
        try:
            await self._invoke_migration_handler_async(job, messages, True, handler, async_handler)
            if not self.db.migration_stage_lease_is_current(job_id, stage, lease_token):
                await self._discard_outputs_async(job, stage, lease_token)
                return False
            await self._commit_outputs_async(job, stage, lease_token, True)
            if not self.db.mark_migration_stage_succeeded(job_id, stage, lease_token, degraded=True):
                await self._discard_outputs_async(job, stage, lease_token)
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
            cleanup_error = await self._discard_outputs_async(job, stage, lease_token)
            self.db.mark_migration_stage_discarded(
                job_id,
                stage,
                lease_token,
                f"degradation: {degraded_exc}",
                cleanup_error=cleanup_error,
            )
            return False

    async def _handle_migration_stage_failure_async(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
        messages: List[Dict[str, Any]],
        exc: Exception,
    ) -> bool:
        action = self._record_migration_stage_failure(job, stage, exc)
        if action == "stale_lease":
            await self._discard_outputs_async(job, stage, job[f"{stage}_lease_token"])
            return False
        if action != "exhausted":
            return False
        return await self._run_degraded_migration_stage_async(
            job,
            stage,
            handler,
            async_handler,
            messages,
        )

    async def _persist_unexpected_stage_failure_async(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler],
        exc: Exception,
    ) -> None:
        try:
            messages = self.db.get_migration_job_messages(job["job_id"])
            if not messages:
                lease_token = job[f"{stage}_lease_token"]
                cleanup_error = await self._discard_outputs_async(job, stage, lease_token)
                self.db.mark_migration_stage_discarded(
                    job["job_id"],
                    stage,
                    lease_token,
                    "migration source messages are missing",
                    cleanup_error=cleanup_error,
                )
                return
            await self._handle_migration_stage_failure_async(
                job,
                stage,
                handler,
                async_handler,
                messages,
                exc,
            )
        except Exception:
            logger.exception(
                "Failed to persist unexpected migration failure job_id=%s stage=%s",
                job.get("job_id"),
                stage,
            )

    async def _run_migration_stage_async(
        self,
        job: Dict[str, Any],
        stage: str,
        handler: MigrationHandler,
        async_handler: Optional[AsyncMigrationHandler] = None,
    ) -> bool:
        job_id = job["job_id"]
        lease_token = job[f"{stage}_lease_token"]
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._migration_heartbeat_async(job, stage, finished),
            name=f"mem0-{stage}-heartbeat-{job_id}",
        )
        try:
            messages = self.db.get_migration_job_messages(job_id)
            if not messages:
                cleanup_error = await self._discard_outputs_async(job, stage, lease_token)
                self.db.mark_migration_stage_discarded(
                    job_id,
                    stage,
                    lease_token,
                    "migration source messages are missing",
                    cleanup_error=cleanup_error,
                )
                return False

            if bool(job.get(f"{stage}_force_degraded")) or int(job.get(f"{stage}_attempts", 0)) > int(
                self.config.max_retries
            ):
                return await self._run_degraded_migration_stage_async(
                    job,
                    stage,
                    handler,
                    async_handler,
                    messages,
                )

            try:
                await self._invoke_migration_handler_async(job, messages, False, handler, async_handler)
                if not self.db.migration_stage_lease_is_current(job_id, stage, lease_token):
                    await self._discard_outputs_async(job, stage, lease_token)
                    return False
                await self._commit_outputs_async(job, stage, lease_token, False)
            except Exception as exc:
                return await self._handle_migration_stage_failure_async(
                    job,
                    stage,
                    handler,
                    async_handler,
                    messages,
                    exc,
                )

            if not self.db.mark_migration_stage_succeeded(job_id, stage, lease_token):
                await self._discard_outputs_async(job, stage, lease_token)
                return False
            return True
        finally:
            finished.set()
            await heartbeat

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
                retryable = getattr(exc, "retryable", None)
                if retryable is None:
                    retryable = not isinstance(exc, (TypeError, ValueError))
                finish_reason = getattr(exc, "finish_reason", None)
                prompt_tokens = getattr(exc, "prompt_tokens", None)
                completion_tokens = getattr(exc, "completion_tokens", None)
                reasoning_tokens = getattr(exc, "reasoning_tokens", None)
                logger.warning(
                    "Background profile update failed job_id=%s job_type=profile user_id=%s error_type=%s "
                    "finish_reason=%s prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s attempts=%s "
                    "recovery_count=%s lease_token=%s worker=%s retryable=%s last_error=%s",
                    job["job_id"],
                    job.get("user_id"),
                    type(exc).__name__,
                    finish_reason,
                    prompt_tokens,
                    completion_tokens,
                    reasoning_tokens,
                    attempt,
                    job.get("recovery_count", 0),
                    str(job.get("lease_token") or "")[:8],
                    threading.current_thread().name,
                    retryable,
                    exc,
                )

                self.db.record_profile_failure(
                    job["job_id"],
                    job["lease_token"],
                    f"{type(exc).__name__}: {exc}",
                    max_retries=int(self.config.max_retries) if retryable else 0,
                    retry_delay_seconds=self._retry_delay(attempt),
                )
        finally:
            self._stop_heartbeat(heartbeat)

    async def _run_profile_job_async(self, job: Dict[str, Any]) -> None:
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._profile_heartbeat_async(job, finished),
            name=f"mem0-profile-heartbeat-{job['job_id']}",
        )
        try:
            try:
                if self.process_profile_async is not None:
                    committed = self.process_profile_async(job)
                else:
                    committed = await asyncio.to_thread(self.process_profile, job)
                if inspect.isawaitable(committed):
                    committed = await committed
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
                    finished_job = self.db.finish_profile_job(job["job_id"], job["lease_token"])
                    if not finished_job:
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
                retryable = getattr(exc, "retryable", None)
                if retryable is None:
                    retryable = not isinstance(exc, (TypeError, ValueError))
                logger.warning(
                    "Background profile update failed job_id=%s job_type=profile user_id=%s error_type=%s "
                    "finish_reason=%s prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s attempts=%s "
                    "recovery_count=%s lease_token=%s worker=%s retryable=%s last_error=%s",
                    job["job_id"],
                    job.get("user_id"),
                    type(exc).__name__,
                    getattr(exc, "finish_reason", None),
                    getattr(exc, "prompt_tokens", None),
                    getattr(exc, "completion_tokens", None),
                    getattr(exc, "reasoning_tokens", None),
                    attempt,
                    job.get("recovery_count", 0),
                    str(job.get("lease_token") or "")[:8],
                    threading.current_thread().name,
                    retryable,
                    exc,
                )
                self.db.record_profile_failure(
                    job["job_id"],
                    job["lease_token"],
                    f"{type(exc).__name__}: {exc}",
                    max_retries=int(self.config.max_retries) if retryable else 0,
                    retry_delay_seconds=self._retry_delay(attempt),
                )
        finally:
            finished.set()
            await heartbeat

    async def _run_promotion_job_async(self, job: Dict[str, Any]) -> None:
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._promotion_heartbeat_async(job, finished),
            name=f"mem0-promotion-heartbeat-{job['job_id']}",
        )
        try:
            try:
                if self.process_promotion_async is not None:
                    discard_reason = self.process_promotion_async(job)
                elif self.process_promotion is not None:
                    discard_reason = await asyncio.to_thread(self.process_promotion, job)
                else:
                    discard_reason = "promotion handler is unavailable"
                if inspect.isawaitable(discard_reason):
                    discard_reason = await discard_reason

                if discard_reason:
                    self.db.discard_promotion_job(
                        job["job_id"],
                        job["lease_token"],
                        str(discard_reason),
                    )
                    return

                if not self.db.complete_promotion_job(job["job_id"], job["lease_token"]):
                    logger.info(
                        "Promotion job abandoned because lease is stale job_id=%s session_id=%s",
                        job["job_id"],
                        job.get("source_midterm_session_id"),
                    )
            except Exception as exc:
                attempt = int(job.get("attempts", 0)) + 1
                retryable = getattr(exc, "retryable", None)
                if retryable is None:
                    retryable = not isinstance(exc, (TypeError, ValueError))
                logger.warning(
                    "Background promotion failed job_id=%s user_id=%s session_id=%s attempts=%s "
                    "recovery_count=%s lease_token=%s worker=%s retryable=%s last_error=%s",
                    job["job_id"],
                    job.get("user_id"),
                    job.get("source_midterm_session_id"),
                    attempt,
                    job.get("recovery_count", 0),
                    str(job.get("lease_token") or "")[:8],
                    threading.current_thread().name,
                    retryable,
                    exc,
                )
                self.db.retry_promotion_job(
                    job["job_id"],
                    job["lease_token"],
                    f"{type(exc).__name__}: {exc}",
                    max_retries=int(self.config.max_retries) if retryable else 0,
                    retry_delay_seconds=self._retry_delay(attempt),
                )
        finally:
            finished.set()
            await heartbeat

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
            self._promotion_wakeup.set()
            self._longterm_extraction_wakeup.set()
            threads = list(self._threads)
            watchdog_thread = self._watchdog_thread

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
                # Worker-owned async heartbeats drain with their job. These legacy
                # thread heartbeats only exist for direct synchronous compatibility calls.
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
