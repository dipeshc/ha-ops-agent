"""Read-only git history for the AI, with fixed argv and path filtering.

The AI never runs git itself. These functions accept only:
- revisions matching `HEAD` or a hex sha, with an optional `~N` or `^`
  suffix, resolved to a commit (blob and tree ids are refused);
- ranges `A..B` of such revisions;
- paths matching a strict character set that also pass the guard's
  readability rules.
Diff and show output only ever covers paths that pass `readable_rel`, so a
secret that was once committed cannot be read back from history.
"""

import re
import subprocess
from pathlib import Path, PurePosixPath

from opsd.guard import Reads, readable_rel, searchable
from opsd.safegit import git_argv, git_env

REV_RE = re.compile(r"(HEAD|[0-9a-f]{7,40})(~[0-9]{1,3}|\^{1,3})?")
PATH_RE = re.compile(r"[A-Za-z0-9_@+][A-Za-z0-9._@+/-]{0,200}")
MAX_LOG = 50
TIMEOUT_S = 30


class HistoryError(ValueError):
    """Invalid input or a git failure, safe to show to the AI."""


def _git(repo: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            git_argv(repo, *args), capture_output=True, text=True, timeout=TIMEOUT_S, env=git_env(), check=False
        )
    except subprocess.TimeoutExpired as err:
        raise HistoryError("git timed out") from err
    if done.returncode != 0:
        raise HistoryError(f"git {args[0]} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def check_rev(value: str) -> str:
    if not REV_RE.fullmatch(value):
        raise HistoryError(f"invalid revision {value!r}: use HEAD, HEAD~N or a commit sha")
    return value


def resolve_commit(repo: Path, rev: str) -> str:
    """The full sha of a commit; trees, blobs and tags are refused."""
    check_rev(rev)
    sha = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise HistoryError(f"{rev} is not a commit")
    return sha


def check_path(repo: Path, value: str, reads: Reads | None = None) -> str:
    """A repo-relative path or directory that the AI may see."""
    if not PATH_RE.fullmatch(value) or ".." in value.split("/") or "//" in value:
        raise HistoryError(f"invalid path {value!r}")
    if not searchable(str(PurePosixPath(repo) / value), PurePosixPath(repo), reads):
        raise HistoryError(f"path {value!r} is not readable")
    return value.rstrip("/")


def _readable_names(names: str, reads: Reads | None) -> tuple[list[str], int]:
    kept: list[str] = []
    dropped = 0
    for name in (n for n in names.splitlines() if n):
        rel = PurePosixPath(name)
        if ".." not in rel.parts and not rel.is_absolute() and readable_rel(rel, reads):
            kept.append(name)
        else:
            dropped += 1
    return kept, dropped


def status(repo: Path) -> str:
    return _git(repo, "status", "--porcelain=v1", "--untracked-files=no", "--no-renames")


def log(repo: Path, count: int, path: str | None, reads: Reads | None = None) -> str:
    count = max(1, min(MAX_LOG, count))
    args = ["log", "--no-color", "--no-decorate", "--date=short", "--format=%h %ad %an: %s", f"-n{count}"]
    if path:
        args += ["--", check_path(repo, path, reads)]
    return _git(repo, *args)


def diff(repo: Path, rev_range: str, path: str | None, reads: Reads | None = None) -> str:
    parts = rev_range.split("..")
    if len(parts) == 1:
        revs = [resolve_commit(repo, parts[0])]
    elif len(parts) == 2:
        revs = [f"{resolve_commit(repo, parts[0])}..{resolve_commit(repo, parts[1])}"]
    else:
        raise HistoryError("use REV or REV..REV")
    pathspec = ["--", check_path(repo, path, reads)] if path else []
    names, dropped = _readable_names(
        _git(repo, "diff", "--no-ext-diff", "--name-only", "--no-renames", *revs, *pathspec), reads
    )
    if not names:
        return f"no readable changes ({dropped} path(s) withheld)"
    out = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", *revs, "--", *names)
    return out + (f"\n[{dropped} non-readable path(s) withheld]\n" if dropped else "")


def show(repo: Path, spec: str, reads: Reads | None = None) -> str:
    rev, sep, path = spec.partition(":")
    sha = resolve_commit(repo, rev)
    if sep:
        if not PATH_RE.fullmatch(path) or ".." in path.split("/") or not readable_rel(PurePosixPath(path), reads):
            raise HistoryError(f"path {path!r} is not readable")
        if _git(repo, "cat-file", "-t", f"{sha}:{path}").strip() != "blob":
            raise HistoryError(f"{path} is not a file in {rev}")
        return _git(repo, "cat-file", "blob", f"{sha}:{path}")
    header = _git(repo, "show", "--no-patch", "--no-color", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n%B", sha)
    names, dropped = _readable_names(
        _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha), reads
    )
    if not names:
        return header + f"\n(no readable files changed; {dropped} withheld)\n"
    patch = _git(repo, "show", "--no-ext-diff", "--no-textconv", "--no-color", "--format=", sha, "--", *names)
    return header + patch + (f"\n[{dropped} non-readable path(s) withheld]\n" if dropped else "")
