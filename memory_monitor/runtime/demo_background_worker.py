from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Optional

from mem0.memory.background_worker import BackgroundWorkerManager

logger = logging.getLogger(__name__)

DemoEventRecorder = Callable[[str, Dict[str, Any]], None]


class DemoBackgroundWorkerManager(BackgroundWorkerManager):
    """Manual facade over the production worker's complete job state machine."""

    def __init__(self, *args, event_recorder: Optional[DemoEventRecorder] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._event_recorder = event_recorder
        self._manual_lock = threading.RLock()
        self._recovered = False

    def start(self) -> None:
        """Recover stale work but deliberately avoid starting background threads."""
        with self._state_lock:
            if self._recovered or not self.enabled:
                return
            self.db.recover_stale_background_jobs(self.config.stale_running_timeout_seconds)
            self._recovered = True

    def wake_migration(self) -> None:
        return None

    def wake_profile(self) -> None:
        return None

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Report queue state without waiting for threads that do not exist."""
        return not self.db.background_jobs_pending()

    def process_next_migration_job(self) -> bool:
        return self._process_claimed_job("migration")

    def process_next_profile_job(self) -> bool:
        return self._process_claimed_job("profile")

    def process_migration_job(self, job_id: str) -> bool:
        return self._process_claimed_job("migration", job_id)

    def process_profile_job(self, job_id: str) -> bool:
        return self._process_claimed_job("profile", job_id)

    def get_job_status(self, job_id: str, job_type: str) -> Optional[Dict[str, Any]]:
        return self.db.get_background_job(job_id, job_type)

    def _process_claimed_job(self, job_type: str, job_id: Optional[str] = None) -> bool:
        if job_type not in {"migration", "profile"}:
            raise ValueError("job_type must be 'migration' or 'profile'")

        with self._manual_lock:
            claim = self.db.claim_migration_job if job_type == "migration" else self.db.claim_profile_job
            claim_next = self.db.claim_next_migration_job if job_type == "migration" else self.db.claim_next_profile_job
            job = claim(job_id) if job_id is not None else claim_next()
            if not isinstance(job, dict):
                return False

            self._record("job.started", {"job_type": job_type, "job_id": job["job_id"]})
            try:
                if job_type == "migration":
                    self._run_migration_job(job)
                else:
                    self._run_profile_job(job)
            except Exception as exc:
                self._record_unexpected_failure(job_type, job, exc)
            status = self.db.get_background_job(job["job_id"], job_type)
            self._record(
                "job.finished",
                {
                    "job_type": job_type,
                    "job_id": job["job_id"],
                    "status": (status or {}).get("status"),
                },
            )
            return True

    def _record_unexpected_failure(self, job_type: str, job: Dict[str, Any], exc: Exception) -> None:
        logger.exception("Unexpected demo %s job failure for job_id=%s", job_type, job.get("job_id"))
        attempt = int(job.get("attempts", 0)) + 1
        if job_type == "migration":
            action = self.db.record_migration_failure(
                job["job_id"],
                f"unexpected migration worker failure: {exc}",
                max_retries=int(self.config.max_retries),
                retry_delay_seconds=self._retry_delay(attempt),
            )
            if action == "exhausted":
                self.db.mark_migration_dead(job["job_id"], f"unexpected migration worker failure: {exc}")
        else:
            self.db.record_profile_failure(
                job["job_id"],
                f"unexpected profile worker failure: {exc}",
                max_retries=int(self.config.max_retries),
                retry_delay_seconds=self._retry_delay(attempt),
            )
        self._record(
            "job.failed",
            {
                "job_type": job_type,
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
