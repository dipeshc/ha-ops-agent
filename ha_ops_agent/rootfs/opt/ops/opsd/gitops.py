"""The only git writes the agent makes: approved config changes on main, and proposal refs.

Proposals are built with plumbing only (`hash-object`, `mktree`,
`commit-tree`, `update-ref`) on `refs/ops/proposals/<id>`: the working tree
and the index are never touched, and those refs are never pushed (`push`
names `HEAD:refs/heads/main` explicitly). An applied change is committed
with `commit_paths`, which commits exactly the named paths on a clean tree.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from opsd.safegit import git_argv, git_env

_LOGGER = logging.getLogger(__name__)

REF_PREFIX = "refs/ops/proposals/"
SHA_RE = re.compile(r"[0-9a-f]{40}")
_ZERO = "0" * 40
COMMIT_RE = re.compile(r"^(feat|fix|docs|style|refactor|test|chore)(\([a-z0-9-]+\))?: [a-z0-9].{0,70}$")


class GitError(RuntimeError):
    """A git precondition or command failed."""


class GitBusyError(GitError):
    """The working tree is in use (dirty or locked); try again later."""


@dataclass(frozen=True, slots=True)
class Repo:
    path: Path
    ssh_key: Path
    known_hosts: Path
    # (name, email) for the agent's commits: the house profile's `git.author_name` / `git.author_email`.
    author: tuple[str, str] = ("Ops Agent", "ops-agent@localhost.localdomain")
    timeout: float = 120.0

    def _env(self) -> dict[str, str]:
        # GIT_SSH_COMMAND wins over the repo's core.sshCommand (the SSH add-on's key).
        return git_env(
            {
                "GIT_SSH_COMMAND": (
                    f"ssh -i {self.ssh_key} -o IdentitiesOnly=yes -o BatchMode=yes "
                    f"-o UserKnownHostsFile={self.known_hosts} -o GlobalKnownHostsFile=/dev/null "
                    "-o StrictHostKeyChecking=yes -o UpdateHostKeys=no"
                )
            }
        )

    def run(self, *args: str) -> str:
        return self.raw(*args).decode(errors="replace")

    def raw(self, *args: str, data: bytes | None = None, env: dict[str, str] | None = None) -> bytes:
        """One git command; bytes in and out (blobs are not always text)."""
        try:
            done = subprocess.run(
                git_argv(self.path, *args),
                input=data,
                capture_output=True,
                timeout=self.timeout,
                env={**self._env(), **(env or {})},
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as err:
            raise GitError(f"git {args[0]}: {err}") from err
        if done.returncode != 0:
            message = (done.stderr or done.stdout).decode(errors="replace").strip()[:300]
            if "index.lock" in message:
                raise GitBusyError(message)
            raise GitError(f"git {args[0]} failed: {message}")
        return done.stdout

    def dirty_paths(self) -> list[str]:
        # Every untracked file by name (not just its directory), so a new file can be matched exactly.
        out = self.run("status", "--porcelain", "--untracked-files=all")
        return [line[3:] for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------- proposal plumbing

    def main_head(self) -> str:
        """The commit `main` points at."""
        sha = self.run("rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}").strip()
        if not SHA_RE.fullmatch(sha):
            raise GitError("main has no commit")
        return sha

    def on_main(self) -> bool:
        return self.run("symbolic-ref", "--quiet", "HEAD").strip() == "refs/heads/main"

    def ref(self, name: str) -> str | None:
        try:
            sha = self.run("rev-parse", "--verify", "--quiet", f"{name}^{{commit}}").strip()
        except GitError:
            return None
        return sha if SHA_RE.fullmatch(sha) else None

    def tree_entries(self, tree: str) -> list[tuple[str, str, str, str]]:
        """`(mode, type, sha, name)` of one tree level."""
        out = []
        for record in self.raw("ls-tree", "-z", tree).split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            mode, kind, sha = meta.decode().split(" ")
            out.append((mode, kind, sha, name.decode(errors="surrogateescape")))
        return out

    def entry(self, rev: str, path: str) -> tuple[str, str, str] | None:
        """`(mode, type, sha)` at a path in a commit (walked level by level), or None if absent."""
        tree = f"{rev}^{{tree}}"
        found: tuple[str, str, str] | None = None
        for part in path.split("/"):
            if found is not None and found[1] != "tree":
                raise GitError(f"{path}: a parent is not a directory in git")
            level = tree if found is None else found[2]
            match = next((e for e in self.tree_entries(level) if e[3] == part), None)
            if match is None:
                return None
            found = (match[0], match[1], match[2])
        return found

    def file_at(self, rev: str, path: str) -> bytes | None:
        """A regular file's bytes at a commit; None if absent. Symlinks and submodules are refused."""
        found = self.entry(rev, path)
        if found is None:
            return None
        if found[0] not in ("100644", "100755") or found[1] != "blob":
            raise GitError(f"{path} is not a regular file in git (mode {found[0]})")
        return self.raw("cat-file", "blob", found[2])

    def hash_blob(self, data: bytes) -> str:
        return self.raw("hash-object", "-w", "--no-filters", "--stdin", data=data).decode().strip()

    def tree_with(self, tree: str | None, parts: list[str], blob: str, *, new_dirs: bool) -> str:
        """A new tree: `tree` with the blob at `parts` (mode 100644); never touches the index."""
        entries = self.tree_entries(tree) if tree else []
        name = parts[0]
        existing = next((e for e in entries if e[3] == name), None)
        if len(parts) == 1:
            if existing is not None and (existing[1] != "blob" or existing[0] != "100644"):
                raise GitError(f"{name} is not a regular file in git (mode {existing[0]})")
            new = ("100644", "blob", blob, name)
        else:
            if existing is not None and existing[1] != "tree":
                raise GitError(f"{name} is not a directory in git")
            if existing is None and not new_dirs:
                raise GitError(f"{name}/ does not exist; new directories are only allowed below automations/")
            sub = self.tree_with(existing[2] if existing else None, parts[1:], blob, new_dirs=new_dirs)
            new = ("040000", "tree", sub, name)
        rows = [e for e in entries if e[3] != name] + [new]
        data = b"".join(f"{m} {k} {s}\t".encode() + n.encode(errors="surrogateescape") + b"\0" for m, k, s, n in rows)
        return self.raw("mktree", "-z", data=data).decode().strip()

    def commit_tree(self, tree: str, parent: str, message: str) -> str:
        if not COMMIT_RE.fullmatch(message):
            raise GitError(f"not a conventional commit subject: {message!r}")
        name, email = self.author
        who = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
               "GIT_COMMITTER_EMAIL": email}  # fmt: skip
        return self.raw("commit-tree", tree, "-p", parent, "-m", message, env=who).decode().strip()

    def create_ref(self, name: str, sha: str) -> None:
        """Create a proposal ref; refuses to overwrite one that exists."""
        if not name.startswith(REF_PREFIX) or not SHA_RE.fullmatch(sha):
            raise GitError(f"refusing ref {name!r}")
        self.run("update-ref", "--no-deref", name, sha, _ZERO)

    def delete_ref(self, name: str, sha: str | None = None) -> None:
        if not name.startswith(REF_PREFIX):
            raise GitError(f"refusing to delete {name!r}")
        self.run("update-ref", "--no-deref", "-d", name, *([sha] if sha else []))

    def changed_between(self, base: str, head: str, paths: list[str]) -> list[str]:
        out = self.run("diff", "--no-ext-diff", "--no-textconv", "--name-only", base, head, "--", *paths)
        return [line for line in out.splitlines() if line]

    def is_ancestor(self, older: str, newer: str) -> bool:
        try:
            self.run("merge-base", "--is-ancestor", older, newer)
        except GitError:
            return False
        return True

    def commit_paths(self, paths: list[str], message: str) -> str:
        """Commit exactly these paths (changes, additions or deletions) on main; refuses on any other change."""
        if not COMMIT_RE.fullmatch(message):
            raise GitError(f"not a conventional commit subject: {message!r}")
        if not self.on_main():
            raise GitError("not on main")
        others = [p for p in self.dirty_paths() if p not in paths]
        if others:
            raise GitBusyError(f"working tree has other changes: {others[:5]}")
        self.run("add", "-A", "--", *paths)
        self.run(
            "-c", f"user.name={self.author[0]}", "-c", f"user.email={self.author[1]}",
            "commit", "-m", message, "--", *paths,
        )  # fmt: skip
        return self.main_head()

    def push(self) -> None:
        """Fast-forward push to origin/main; never forces, stops if the remote moved."""
        self.run("fetch", "--quiet", "origin", "main")
        behind = int(self.run("rev-list", "--count", "HEAD..origin/main").strip() or "0")
        if behind:
            raise GitError(f"origin/main is {behind} commit(s) ahead; not pushing")
        self.run("push", "origin", "HEAD:refs/heads/main")
