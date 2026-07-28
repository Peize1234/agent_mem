from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import BinaryIO, Dict


class ProcessInstanceLock:
    """A small process-scoped file lock for one history database.

    Locks are reference-counted inside one process so multiple in-process SDK
    objects do not contend with each other. The operating-system lock still
    rejects another service process using the same database.
    """

    _registry_guard = threading.Lock()
    _registry: Dict[str, Dict[str, object]] = {}

    def __init__(self, history_db_path: str, *, enabled: bool = True):
        self.enabled = bool(enabled and history_db_path != ":memory:")
        self.lock_path = (
            str(Path(history_db_path).expanduser().resolve()) + ".lock" if self.enabled else None
        )
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> None:
        if not self.enabled or self._acquired:
            return
        lock_path = self.lock_path
        if lock_path is None:
            return
        with self._registry_guard:
            registered = self._registry.get(lock_path)
            if registered is not None:
                registered["count"] = int(registered["count"]) + 1
                self._acquired = True
                return

            Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(lock_path, "a+b")
            try:
                self._lock_file(lock_file)
            except OSError as exc:
                lock_file.close()
                raise RuntimeError(
                    "Another Memory instance is already using this history database"
                ) from exc
            self._registry[lock_path] = {"file": lock_file, "count": 1}
            self._acquired = True

    def release(self) -> None:
        if not self.enabled or not self._acquired:
            return
        lock_path = self.lock_path
        if lock_path is None:
            return
        with self._registry_guard:
            registered = self._registry.get(lock_path)
            if registered is None:
                self._acquired = False
                return
            count = int(registered["count"]) - 1
            if count > 0:
                registered["count"] = count
            else:
                lock_file = registered["file"]
                try:
                    self._unlock_file(lock_file)
                finally:
                    lock_file.close()
                    self._registry.pop(lock_path, None)
            self._acquired = False

    @staticmethod
    def _lock_file(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_file(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass
