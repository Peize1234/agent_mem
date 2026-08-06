from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable


class BoundedTimeoutExecutor:
    """Small bounded worker pool for call sites without a native timeout.

    Timed-out functions may still return later, but both worker and queue counts
    are fixed so repeated timeouts cannot create an unbounded number of threads.
    """

    def __init__(self, *, max_workers: int = 2, max_pending: int = 4, thread_name_prefix: str):
        if max_workers < 1:
            raise ValueError("max_workers must be greater than or equal to 1")
        if max_pending < 1:
            raise ValueError("max_pending must be greater than or equal to 1")
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._thread_name_prefix = thread_name_prefix
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._capacity = threading.BoundedSemaphore(max_workers + max_pending)
        self._state_lock = threading.Lock()
        self._shutdown = False

    def run(
        self,
        function: Callable[..., Any],
        *args: Any,
        timeout_seconds: float,
        operation_name: str,
        **kwargs: Any,
    ) -> Any:
        with self._state_lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new calls after shutdown")
            if not self._capacity.acquire(blocking=False):
                raise TimeoutError(f"{operation_name} timed out because timeout capacity is exhausted")
            try:
                future = self._executor.submit(function, *args, **kwargs)
            except BaseException:
                self._capacity.release()
                raise
        future.add_done_callback(lambda _: self._capacity.release())
        done, _ = wait((future,), timeout=float(timeout_seconds))
        if not done:
            raise TimeoutError(f"{operation_name} timed out after {float(timeout_seconds):g} seconds")
        return future.result()

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        """Stop accepting calls and idempotently shut down the executor."""
        with self._state_lock:
            self._shutdown = True
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
