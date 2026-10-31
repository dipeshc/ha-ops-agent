"""Privilege separation: the AI runs as `ops` and sees only a filtered export.

The OS enforces the boundary, not the hook:
- the HA config is mounted below `/mnt/ha` (root, 0700), so `ops` cannot
  traverse to it;
- `/data` is 0711 and everything in it except `claude-ops/` is root-only;
- the AI's cwd is `/run/ops-claude/config`, a root-owned, read-only copy of
  the files that pass the guard's readability rules (with the house profile's
  documents), rebuilt before every run: the tracked ones when git is on; when
  it is off, only a strict allowlist of config files on disk, each redacted
  (`disk_allowed`);
- the Supervisor token only exists in root processes' environments, which a
  different uid cannot read.
`isolation_problems` re-checks the sensitive paths with the real permission
bits before every AI run; any exposure blocks the run.
"""

import grp
import os
import pwd
import re
import shutil
import stat
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from opsd.config import OPS_USER, Paths
from opsd.guard import READS, Reads, denied, readable_rel
from opsd.redact import Redactor, build_redactor
from opsd.safegit import git_argv, git_env


@dataclass(frozen=True, slots=True)
class Account:
    uid: int
    gid: int
    groups: frozenset[int]


def ops_account(name: str = OPS_USER) -> Account | None:
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return None
    groups = {entry.pw_gid} | {g.gr_gid for g in grp.getgrall() if name in g.gr_mem}
    return Account(entry.pw_uid, entry.pw_gid, frozenset(groups))


def _allows(st: os.stat_result, account: Account, *, read: bool, execute: bool) -> bool:
    if account.uid == 0:
        return True
    if st.st_uid == account.uid:
        r, x = stat.S_IRUSR, stat.S_IXUSR
    elif st.st_gid in account.groups:
        r, x = stat.S_IRGRP, stat.S_IXGRP
    else:
        r, x = stat.S_IROTH, stat.S_IXOTH
    return (not read or bool(st.st_mode & r)) and (not execute or bool(st.st_mode & x))


def reachable_by(path: Path, account: Account, *, trusted_root: Path | None = None) -> bool:
    """Could `account` open `path` for reading (traversal + read bits, symlinks resolved)?

    `trusted_root` (tests only) skips the traversal check for directories above it.
    """
    try:
        real = Path(os.path.realpath(path))
        target = real.stat()
    except FileNotFoundError, NotADirectoryError:
        return False
    skip = set(Path(os.path.realpath(trusted_root)).parents) if trusted_root else set()
    for ancestor in reversed(real.parents):
        if ancestor in skip:
            continue
        try:
            if not _allows(ancestor.stat(), account, read=False, execute=True):
                return False
        except FileNotFoundError:
            return False
    # A directory counts as reachable with execute alone: names inside it can
    # be opened when known, even without permission to list it.
    if stat.S_ISDIR(target.st_mode):
        return _allows(target, account, read=False, execute=True)
    return _allows(target, account, read=True, execute=False)


def sensitive_paths(paths: Paths) -> list[Path]:
    return [
        paths.options_file,
        paths.secrets_dir,
        paths.ssh_key,
        paths.state_file,
        paths.store_file,
        paths.store_copy,
        paths.ha_config,
        paths.ha_config / "secrets.yaml",
        paths.ha_config / ".storage",
        Path("/run/s6/container_environment"),
        paths.run,
    ]


def isolation_problems(
    paths: Paths, account: Account, extra: Iterable[Path] = (), *, trusted_root: Path | None = None
) -> list[str]:
    return [str(p) for p in [*sensitive_paths(paths), *extra] if reachable_by(p, account, trusted_root=trusted_root)]


# Git off: no git tracking filters the config, so the export is a strict allowlist of files on disk:
# YAML in the config root and its standard folders, template macros, and the house profile's named `.md`
# documents, each run through the redactor. Never secrets, `.storage/` or any hidden path, databases, logs,
# `custom_components/`, `www/`, `deps/`, `tts/`, backups or links (none of them is on the allowlist).
DISK_YAML_DIRS = frozenset({"automations", "scripts", "packages", "alerts", "templates", "blueprints"})
DISK_MACRO_DIR = "custom_templates"
_NEVER_NAME = re.compile(r"\.(?:db|log|sqlite)(?:[.-]|$)", re.IGNORECASE)
MAX_DISK_FILE_BYTES = 256 * 1024
MAX_DISK_TOTAL_BYTES = 16 * 1024 * 1024
MAX_EXPORT_FILES = 5000


def tracked_readable_files(repo: Path, reads: Reads | None = None) -> list[str]:
    done = subprocess.run(
        git_argv(repo, "ls-files", "-z", "--cached"), capture_output=True, timeout=60, env=git_env(), check=True
    )
    names: list[str] = []
    for raw in done.stdout.split(b"\0"):
        if not raw:
            continue
        name = raw.decode(errors="replace")
        rel = PurePosixPath(name)
        if ".." in rel.parts or rel.is_absolute() or not readable_rel(rel, reads):
            continue
        names.append(name)
    return names


def disk_allowed(rel: PurePosixPath, docs: frozenset[str] = frozenset()) -> bool:
    """The git-off allowlist (see `DISK_YAML_DIRS`); `docs`: the house profile's named `.md` documents."""
    parts = rel.parts
    if not parts or ".." in parts or any(p.startswith(".") or denied(p) for p in parts):
        return False
    name = parts[-1]
    if _NEVER_NAME.search(name):
        return False
    if name.endswith(".md"):
        return rel.as_posix() in docs
    if name.endswith((".yaml", ".yml")):
        return len(parts) == 1 or parts[0] in DISK_YAML_DIRS
    return len(parts) == 2 and parts[0] == DISK_MACRO_DIR and name.endswith(".jinja")


def disk_files(root: Path, reads: Reads | None = None) -> list[str]:
    """The allowlisted files on disk (git off); hidden directories, links and other folders are never entered."""
    reads = reads or READS
    docs = frozenset(f for f in reads.files if f.endswith(".md"))
    top_dirs = DISK_YAML_DIRS | {DISK_MACRO_DIR} | {d.split("/")[0] for d in docs if "/" in d}
    names: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        rel_dir = PurePosixPath(Path(current).relative_to(root).as_posix())
        top = rel_dir == PurePosixPath(".")
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and (not top or d in top_dirs))
        for name in sorted(files):
            rel = PurePosixPath(name) if top else rel_dir / name
            if disk_allowed(rel, docs) and readable_rel(rel, reads):
                names.append(rel.as_posix())
                if len(names) >= MAX_EXPORT_FILES:
                    return names
    return names


def _copy_redacted(source: Path, destination: Path, redactor: Redactor, budget: list[int]) -> None:
    """One git-off file: text only, at most `MAX_DISK_FILE_BYTES`, within the total budget, redacted."""
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return
        data = os.read(fd, MAX_DISK_FILE_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_DISK_FILE_BYTES or len(data) > budget[0]:
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    budget[0] -= len(data)
    destination.write_text(redactor.redact(text))


def build_export(repo: Path, export_root: Path, *, reads: Reads | None = None, git: bool = True,
                 redactor: Redactor | None = None) -> Path:  # fmt: skip
    """Copy readable files (never symlinks) into a fresh root-owned tree: the tracked ones when `git`, else the
    allowlisted files on disk, each redacted (`redactor`, by default the values in `secrets.yaml` and the
    credential shapes)."""
    export_root.mkdir(parents=True, exist_ok=True)
    # World-readable on purpose: the export only holds files the ops user may read.
    os.chmod(export_root, 0o755)  # noqa: S103
    staging = export_root / f".staging-{os.getpid()}-{time.monotonic_ns()}"
    staging.mkdir(mode=0o755)
    root = Path(os.path.realpath(repo))
    names = tracked_readable_files(repo, reads) if git else disk_files(repo, reads)
    redact = None if git else (redactor or build_redactor(repo / "secrets.yaml"))
    budget = [MAX_DISK_TOTAL_BYTES]
    for name in names:
        source = repo / name
        if source.is_symlink() or not source.is_file():
            continue
        # An intermediate directory may be a symlink: judge the resolved path.
        resolved = Path(os.path.realpath(source))
        try:
            resolved_rel = PurePosixPath(resolved.relative_to(root).as_posix())
        except ValueError:
            continue
        if not readable_rel(resolved_rel, reads):
            continue
        destination = staging / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        if redact is None:
            shutil.copyfile(source, destination, follow_symlinks=False)
        else:
            _copy_redacted(source, destination, redact, budget)
            if not destination.exists():
                continue
        os.chmod(destination, 0o644)
    for directory in [staging, *(p for p in staging.rglob("*") if p.is_dir())]:
        os.chmod(directory, 0o755)  # noqa: S103
    target = export_root / "config"
    previous = export_root / f".previous-{time.monotonic_ns()}"
    if target.exists():
        target.rename(previous)
    staging.rename(target)
    shutil.rmtree(previous, ignore_errors=True)
    return target
