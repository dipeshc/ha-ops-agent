"""Single-run lock so health checks and AI runs never overlap."""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LockBusyError(RuntimeError):
    """Another run holds the lock."""


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive, non-blocking flock for the duration of the block.

    The kernel drops a flock when its holder dies, so a crash or reboot never
    leaves a stale lock behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise LockBusyError(str(path)) from err
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
