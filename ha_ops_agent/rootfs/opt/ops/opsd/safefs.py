"""Files the agent reads or writes where a link could be swapped in (spec proposals.md C8, updates.md U28,
capabilities.md K13, profile.md P2, ai-runner.md R9, maintenance.md M8).

Never through a link: files with `O_NOFOLLOW`, directories with `O_DIRECTORY | O_NOFOLLOW` one level at a time;
regular files only, bounded; a replace is a fresh `O_EXCL` temporary file, fsynced, renamed, then the directory
fsynced, and the temporary file is removed when anything fails.
"""

import contextlib
import os
import secrets
import stat
from collections.abc import Iterable
from pathlib import Path

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class NotRegular(OSError):
    """Not a regular file (a link, a directory, a pipe, a device)."""


class TooLarge(OSError):
    """Larger than the caller's limit."""


def read_regular(path: Path | str, limit: int, *, dir_fd: int | None = None) -> bytes:
    """A regular file's bytes, never through a link, at most `limit`. OSError when it cannot be opened (a link
    included), `NotRegular` or `TooLarge` otherwise."""
    fd = os.open(path, _READ_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegular(f"{Path(path).name} is not a regular file")
        chunks: list[bytes] = []
        size = 0
        while size <= limit and (chunk := os.read(fd, min(1 << 16, limit + 1 - size))):
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > limit:
        raise TooLarge(f"{Path(path).name} is larger than {limit} bytes")
    return b"".join(chunks)


def read_or_none(path: Path | str, limit: int, *, dir_fd: int | None = None) -> bytes | None:
    """`read_regular`, or None for anything that is not a readable regular file within `limit`."""
    try:
        return read_regular(path, limit, dir_fd=dir_fd)
    except OSError:
        return None


def open_dir(root: Path, parts: Iterable[str]) -> int:
    """A descriptor of `root/<parts...>`, each level opened with `O_DIRECTORY | O_NOFOLLOW` below the one before
    (the caller closes it). OSError (FileNotFoundError when a level is missing) otherwise."""
    fd = os.open(root, DIR_FLAGS)
    try:
        for part in parts:
            next_fd = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
    except BaseException:
        os.close(fd)
        raise
    return fd


def replace_at(dir_fd: int, name: str, data: bytes, *, mode: int, uid: int, gid: int) -> None:
    """Replace `name` in the directory `dir_fd` with `data` atomically, as `mode` (permission bits only) and
    owned by `uid:gid`."""
    tmp = f".ops-{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            if (uid, gid) != (os.geteuid(), os.getegid()):
                os.fchown(fd, uid, gid)
            os.fchmod(fd, mode & 0o777)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


def write_private(path: Path, data: bytes) -> None:
    """Replace a file in one of the daemon's own directories, 0600 and owned by the daemon."""
    fd = os.open(path.parent, DIR_FLAGS)
    try:
        replace_at(fd, path.name, data, mode=0o600, uid=os.geteuid(), gid=os.getegid())
    finally:
        os.close(fd)


def private_dir(path: Path) -> Path:
    """A directory only the daemon can enter (mkdir's mode is subject to the umask and skipped when it exists)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
