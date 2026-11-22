"""The link lock: one process at a time talks to the node.

The daemon holds the lock while it may use the link. `spark-bridge update` takes it for the
whole update. When the daemon runs, the updater asks it to pause with SIGUSR1 (the daemon's PID
is in the lock file): the daemon closes the link, releases the lock and waits until it can take
it again, then reconnects. This works the same under systemd, launchd or a terminal, needs no
privileges and leaves the supervisor's restart logic alone.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from pathlib import Path


class LinkLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def holder_pid(self) -> int | None:
        """PID written by the current holder (None if unknown)."""
        try:
            txt = self.path.read_text().strip()
            return int(txt) if txt else None
        except (OSError, ValueError):
            return None

    async def acquire(self, timeout: float | None = None, poll: float = 0.5) -> bool:
        loop = asyncio.get_running_loop()
        end = None if timeout is None else loop.time() + timeout
        while not self.try_acquire():
            if end is not None and loop.time() >= end:
                return False
            await asyncio.sleep(poll)
        return True


def lock_path_for(db_path: str) -> Path:
    """spark-bridge.lock next to the state database."""
    return Path(db_path).with_name("spark-bridge.lock")
