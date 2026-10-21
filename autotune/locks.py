"""Advisory process locks over fcntl.flock.

The kernel drops a flock when the holding process exits for any reason,
including SIGKILL, so a stale lock file never blocks a later holder. Lock
files are never unlinked: removing one while another process waits on it
would let two processes hold "the same" lock on different inodes.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

FILE_MODE = 0o600


class LockHeld(RuntimeError):
    """Raised by `with FileLock(...)` when a non-blocking acquire is refused."""


class FileLock:
    def __init__(self, path: str | Path, blocking: bool = False) -> None:
        self.path = Path(path)
        self.blocking = blocking
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            flags = fcntl.LOCK_EX if self.blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    return False
                raise
            try:
                os.fchmod(fd, FILE_MODE)
            except PermissionError:
                pass
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise LockHeld(str(self.path))
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def is_locked(path: str | Path) -> bool:
    """Probe with a non-blocking exclusive lock, releasing it immediately.

    Does not create the lock file. The probe briefly holds the lock, so a
    concurrent non-blocking acquire in another process can be refused.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    except PermissionError:
        fd = os.open(path, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True
            raise
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def holder_pid(path: str | Path) -> int | None:
    """Return the pid recorded by the current holder, or None if unknown."""
    try:
        text = Path(path).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() else None
