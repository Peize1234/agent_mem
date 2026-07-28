from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class _TimeoutCall:
    function: Callable[..., Any]
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    finished: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None


class BoundedTimeoutExecutor:
    """Small daemon worker pool for call sites without a native timeout.

    Timed-out functions may still return later, but both worker and queue counts
    are fixed so repeated timeouts cannot create an unbounded number of threads.
    """

    def __init__(self, *, max_workers: int = 2, max_pending: int = 4, thread_name_prefix: str):
        self._queue: queue.Queue[_TimeoutCall] = queue.Queue(maxsize=max_pending)
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._threads: list[threading.Thread] = []
        self._start_guard = threading.Lock()

    def _ensure_started(self) -> None:
        if self._threads:
            return
        with self._start_guard:
            if self._threads:
                return
            for index in range(self._max_workers):
                thread = threading.Thread(
                    target=self._worker,
                    name=f"{self._thread_name_prefix}-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def _worker(self) -> None:
        while True:
            call = self._queue.get()
            try:
                call.result = call.function(*call.args, **call.kwargs)
            except BaseException as exc:
                call.error = exc
            finally:
                call.finished.set()
                self._queue.task_done()

    def run(
        self,
        function: Callable[..., Any],
        *args: Any,
        timeout_seconds: float,
        operation_name: str,
        **kwargs: Any,
    ) -> Any:
        self._ensure_started()
        call = _TimeoutCall(function=function, args=args, kwargs=kwargs)
        try:
            self._queue.put_nowait(call)
        except queue.Full as exc:
            raise TimeoutError(f"{operation_name} timed out because timeout capacity is exhausted") from exc
        if not call.finished.wait(float(timeout_seconds)):
            raise TimeoutError(f"{operation_name} timed out after {float(timeout_seconds):g} seconds")
        if call.error is not None:
            raise call.error
        return call.result
