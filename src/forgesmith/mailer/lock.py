"""Single-writer advisory lock for live send runs.

The lock is an OS file lock, released automatically when the process dies, so
a crash never leaves a permanent "sending" state. The file itself is never
unlinked (unlinking a locked file is racy on Windows).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType

from forgesmith.mailer.models import LockHeldError

_WINDOWS = os.name == "nt"
if _WINDOWS:  # pragma: no cover - platform split
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if _WINDOWS:  # pragma: no cover - platform split
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - platform split
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            holder = self._read_pid()
            os.close(fd)
            raise LockHeldError(str(self.path), holder) from exc
        self._fd = fd
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid():<10}".encode("ascii"))
        os.fsync(fd)

    def _read_pid(self) -> int | None:
        try:
            raw = self.path.read_text("ascii", errors="ignore").strip()
            return int(raw) if raw else None
        except (OSError, ValueError):
            return None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if _WINDOWS:  # pragma: no cover - platform split
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - platform split
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
