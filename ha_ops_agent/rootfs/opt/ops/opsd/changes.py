"""Config changes as git proposals (`change.propose`, `facts.propose`, apply, revert).

Proposing (a chat or investigation run, or "Remember this" in the panel):
the run writes each file's full new contents to its drafts directory
(`/run/ops-drafts/<path>`, the only place its `Write` tool reaches) and
calls `opsctl propose-change`. The daemon reads the drafts without following
links (each path part opened with `O_NOFOLLOW`, regular file, one link, owned
by `ops`, 64 KB at most), checks the path allowlist (`changepaths`), the
content rules on every added line, and that YAML parses. It then builds a
commit with git plumbing only (`hash-object`, `mktree`, `commit-tree`,
`update-ref`) on `refs/ops/proposals/<id>`, based on the current `main`: the
working tree and the index are never touched, and the ref is never pushed.
Five proposals a day, five open, seven days to live.

Applying or reverting is never a capability the AI can call. The admin
approves it in the panel in one step (`approvals.py`, kind `change`),
bound to the proposal's commit, base, file hashes and creation time. The
daemon then, holding the git lock: re-checks the binding; refuses unless
the tree is clean and the touched files are unchanged since the base (else
"stale, re-propose"); consumes the approval; writes the files
atomically (no symlinks anywhere on the way); has Core check the config;
reloads by path (automations and templates; alerts take effect at the next
restart, which the agent never does); and only then commits exactly those
paths on `main` and pushes fast-forward. A failed check, reload or commit
restores the previous contents exactly (and reloads again); a reload that
fails twice is escalated. HA config paths need `mode: act`; the facts
document may be applied in observe mode (still the admin's approval). A revert is the
same pipeline writing the contents the change replaced.
"""

import asyncio
import contextlib
import difflib
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opsd import configcheck
from opsd.api import LiveAPI
from opsd.approvals import ApprovalError, Approvals, is_change
from opsd.capabilities import CAPABILITIES, Args, CapabilityError
from opsd.changepaths import (
    CREATABLE_ROOT,
    MAX_FILE_BYTES,
    MAX_FILES,
    SUPPRESS_PATH,
    ProposalRules,
    allowed_path,
    fix_path,
    is_doc,
    split_paths,
)
from opsd.channels import BudgetClass, Message
from opsd.config import Options
from opsd.conversation import clean_text
from opsd.gitops import COMMIT_RE, REF_PREFIX, GitBusyError, GitError, Repo
from opsd.policy import Origin, PolicyTier, decide
from opsd.privsep import ops_account
from opsd.runctx import ExecContext, RunContext
from opsd.store import Store, StoreError
from opsd.supervisor import SupervisorClient

_LOGGER = logging.getLogger(__name__)

MAX_PER_DAY = 5
MAX_OPEN = 5
MAX_REFUSALS_PER_DAY = 20
PROPOSAL_TTL = timedelta(days=7)
DIFF_CHARS = 60_000
WHY_CHARS = 600
EFFECT_CHARS = 300
FACT_CHARS = 500
PROFILES = frozenset({"chat", "investigate"})
FACTS_SECTION = "## Noted through the ops agent"
# Reload per top-level directory. Alerts have no reload; they take effect at the next Core restart.
RELOADS: dict[str, tuple[str, str]] = {"automations": ("automation", "reload"), "templates": ("template", "reload")}
CHANGE_ESCALATION_TAG = "ha-ops-change"
# An update's config change (opsd.updatefix) may also edit this file, inside one integration's block.
CONFIGURATION_PATH = "configuration.yaml"


def needs_restart(path: str) -> bool:
    """Files only a Core restart loads (no reload service for them)."""
    return path == CONFIGURATION_PATH or path.startswith("alerts/")


CheckConfig = Callable[[], Awaitable[tuple[bool, str]]]
Reload = Callable[[str, str], Awaitable[None]]


class ChangeRefused(ValueError):
    """The proposal is refused; the message is safe to show the model and the admin."""


# ----------------------------------------------------------------- content

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"!(?:secret|include\w*|env_var)\b"), "YAML tags !secret, !include and !env_var are not allowed"),
    (re.compile(r"\b(?:shell_command|command_line|rest_command|python_script|pyscript)\b"),
     "shell_command, command_line, rest_command, python_script and pyscript are not allowed"),
    (re.compile(r"\bhassio\."), "hassio services (add-ons, host, backups) are not allowed"),
    (re.compile(r"\bhomeassistant\.(?:restart|stop)\b"), "restarting or stopping Home Assistant is not allowed"),
    (re.compile(r"\b(?:lock|alarm_control_panel)\.[a-z0-9_]+"),
     "locks and the alarm are out of bounds: no entity or service of those domains"),
    (re.compile(r"\bdomain:\s*['\"]?(?:lock|alarm_control_panel|cover)\b"),
     "device actions on locks, the alarm or covers are not allowed"),
    (re.compile(r"\bcover\.[a-z0-9_]*garage[a-z0-9_]*"), "garage doors are out of bounds"),
)  # fmt: skip
_NOTIFY_RE = re.compile(r"\bnotify\.([a-z0-9_]+)")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CRITICAL_GROUP = "critical_alerts"


def added_lines(old: str, new: str) -> list[str]:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    return [line[1:] for line in diff if line.startswith("+") and not line.startswith("+++")]


def check_content(path: str, old: bytes | None, new: bytes, *, notify_service: str,
                  entities: frozenset[str] | None, guarded: frozenset[str] | None = frozenset(),
                  fix_domains: frozenset[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Line rules as a pre-filter, then the fail-closed allowlist on the parsed file (`configcheck`).

    Returns the admin's summary for this file: line counts and the actions and target entities the
    change adds or removes (computed here from both versions, never from the model's account).
    """
    if len(new) > MAX_FILE_BYTES:
        raise ChangeRefused(f"{path} is larger than {MAX_FILE_BYTES // 1024} KB")
    try:
        text = new.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ChangeRefused(f"{path} is not UTF-8 text") from err
    if _CONTROL_RE.search(text):
        raise ChangeRefused(f"{path} contains control characters")
    old_text = old.decode("utf-8", errors="replace") if old else ""
    allowed_notify = {notify_service.removeprefix("notify."), CRITICAL_GROUP}
    added = added_lines(old_text, text)
    for line in added:
        for pattern, why in _RULES:
            if pattern.search(line):
                raise ChangeRefused(f"{path}: {why} (line: {clean_text(line, 120)!r})")
        for name in _NOTIFY_RE.findall(line):
            if name not in allowed_notify:
                raise ChangeRefused(f"{path}: only notify.{notify_service.removeprefix('notify.')} and"
                                    f" notify.{CRITICAL_GROUP} may be used (found notify.{name})")  # fmt: skip
    removed = len(removed_lines(old_text, text))
    summary: dict[str, Any] = {"path": path, "added": len(added), "removed": removed}
    if path == CONFIGURATION_PATH:
        # Only an update's own config change (opsd.updatefix), inside the block of the integration whose
        # break it fixes; the allowlist walk below does not know this file's shape.
        if fix_domains is None:
            raise ChangeRefused(f"{path} can only be changed by an update's breaking-change fix")
        try:
            configcheck.check_configuration_edit(old_text, text, domains=fix_domains)
        except configcheck.ConfigRefused as err:
            raise ChangeRefused(str(err)) from err
        return summary
    if path.endswith(".jinja"):
        # Template macros (an update fix only): no link, so a macro never points a browser anywhere.
        if fix_domains is None or not path.startswith("custom_templates/"):
            raise ChangeRefused(f"{path} can only be changed by an update's breaking-change fix")
        for line in added:
            if "://" in line:
                raise ChangeRefused(f"{path}: no URLs in a template macro (line: {clean_text(line, 120)!r})")
        return summary
    if path == SUPPRESS_PATH:
        # Only reached with the suppress overlay on (`check_paths`); the core never imports it otherwise.
        from opsd.house import suppress  # noqa: PLC0415

        rules, problems = suppress.parse_file(text)
        if problems:
            raise ChangeRefused(f"{path}: {problems[0]}")
        return summary | {"rules": [f"{r.key} while {r.verifier} {r.params}" for r in rules]}
    if not path.endswith(".yaml"):
        return summary
    try:
        configcheck.check_file(path, text, entities=entities, admin_notify=notify_service, guarded=guarded)
    except configcheck.ConfigRefused as err:
        raise ChangeRefused(str(err)) from err
    before, after = configcheck.collect(old_text), configcheck.collect(text)
    return summary | {
        "actions_added": sorted(after.actions - before.actions),
        "actions_removed": sorted(before.actions - after.actions),
        "entities_added": sorted(after.entities - before.entities),
        "entities_removed": sorted(before.entities - after.entities),
    }


def removed_lines(old: str, new: str) -> list[str]:
    diff = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
    return [line[1:] for line in diff if line.startswith("-") and not line.startswith("---")]


# ------------------------------------------------------------------ drafts


def read_draft(root: Path, rel: str, *, owner: int | None) -> bytes:
    """A draft's bytes, never following a link: each part opened with O_NOFOLLOW below `root`."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags_dir)
    except OSError as err:
        raise ChangeRefused("there is no drafts directory in this run") from err
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, flags_dir, dir_fd=fd)
            except OSError as err:
                raise ChangeRefused(f"no draft for {rel}: write it to {root}/{rel} first") from err
            os.close(fd)
            fd = next_fd
        try:
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except OSError as err:
            raise ChangeRefused(f"no draft for {rel}: write it to {root}/{rel} first") from err
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ChangeRefused(f"the draft for {rel} is not a plain file")
            if owner is not None and info.st_uid != owner:
                raise ChangeRefused(f"the draft for {rel} was not written by the agent")
            if info.st_size > MAX_FILE_BYTES:
                raise ChangeRefused(f"{rel} is larger than {MAX_FILE_BYTES // 1024} KB")
            data = os.read(file_fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(file_fd)
    finally:
        os.close(fd)
    if len(data) > MAX_FILE_BYTES:
        raise ChangeRefused(f"{rel} is larger than {MAX_FILE_BYTES // 1024} KB")
    return data


def drop_draft(root: Path, rel: str) -> None:
    """Delete a consumed draft (so a corrected one can be written fresh); never follows links."""
    flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        fd = os.open(root, flags_dir)
    except OSError:
        return
    try:
        parts = rel.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, flags_dir, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        os.unlink(parts[-1], dir_fd=fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------- live config files


class LiveFiles:
    """The live config, written without following links (each directory opened with O_NOFOLLOW)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _parent(self, rel: str, *, create: bool, created: list[str]) -> int:
        flags_dir = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(self.root, flags_dir)
        done: list[str] = []
        try:
            for part in rel.split("/")[:-1]:
                done.append(part)
                try:
                    next_fd = os.open(part, flags_dir, dir_fd=fd)
                except FileNotFoundError:
                    if not create or done[0] != CREATABLE_ROOT:
                        raise
                    os.mkdir(part, 0o755, dir_fd=fd)
                    created.append("/".join(done))
                    next_fd = os.open(part, flags_dir, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except BaseException:
            os.close(fd)
            raise
        return fd

    def read(self, rel: str) -> bytes | None:
        try:
            fd = self._parent(rel, create=False, created=[])
        except FileNotFoundError:
            return None
        try:
            try:
                info = os.stat(rel.rsplit("/", 1)[-1], dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"{rel} is not a regular file")
            file_fd = os.open(rel.rsplit("/", 1)[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                chunks = []
                while chunk := os.read(file_fd, 1 << 16):
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(file_fd)
        finally:
            os.close(fd)

    def write(self, rel: str, data: bytes | None, created: list[str]) -> None:
        """Replace atomically (temp file, fsync, rename), or delete when `data` is None."""
        name = rel.rsplit("/", 1)[-1]
        fd = self._parent(rel, create=data is not None, created=created)
        try:
            # Keep the file's owner and mode (a new file takes the directory's owner and 0644).
            owner = os.fstat(fd)
            mode, uid, gid = 0o644, owner.st_uid, owner.st_gid
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError(f"{rel} is not a regular file")
                mode, uid, gid = stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid
            except FileNotFoundError:
                pass
            if data is None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=fd)
                return
            tmp = f".ops-{secrets.token_hex(6)}.tmp"
            file_fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(file_fd, view) :]
                if (uid, gid) != (os.geteuid(), os.getegid()):
                    os.fchown(file_fd, uid, gid)
                os.fchmod(file_fd, mode)
                os.fsync(file_fd)
            finally:
                os.close(file_fd)
            os.replace(tmp, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            os.close(fd)

    def remove_dirs(self, created: list[str]) -> None:
        for rel in reversed(created):
            try:
                os.rmdir(self.root / rel)
            except FileNotFoundError:
                pass
            except OSError as err:
                _LOGGER.warning("directory not removed", extra={"path": rel, "error": str(err)[:200]})


# --------------------------------------------------------------- proposals


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    new: bytes


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _diff(path: str, old: bytes | None, new: bytes | None) -> str:
    before = (old or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    after = (new or b"").decode("utf-8", errors="replace").splitlines(keepends=True)
    fromfile = f"a/{path}" if old is not None else "/dev/null"
    return "".join(difflib.unified_diff(before, after, fromfile=fromfile, tofile=f"b/{path}"))


def check_paths(paths: list[str], *, kind: str, rules: ProposalRules, fix: bool = False,
                suppress: bool = False) -> None:  # fmt: skip
    """`rules`: the house profile's proposal rules; `suppress`: the suppress overlay is on."""
    if not paths:
        raise ChangeRefused("name at least one file")
    if kind == "facts" and ((rules.facts and paths == [rules.facts]) or (suppress and paths == [SUPPRESS_PATH])):
        # Documentation the daemon writes itself (house facts, known false positives).
        return
    if len(paths) > MAX_FILES:
        raise ChangeRefused(f"at most {MAX_FILES} files per proposal")
    for path in paths:
        if fix:
            if not fix_path(path):
                raise ChangeRefused(
                    f"{path!r} is not a file an update's breaking-change fix may change "
                    "(automations, alerts, templates, custom_templates, a configuration.yaml block)"
                )
            continue
        if not allowed_path(path, rules):
            raise ChangeRefused(
                f"{path!r} cannot be changed by the agent. Allowed: {rules.describe()} (lower-case names)"
            )
    if kind == "facts":
        raise ChangeRefused(f"a house fact changes {rules.facts or 'the house facts document'} only")


def check_subject(subject: str) -> None:
    if not COMMIT_RE.fullmatch(subject):
        raise ChangeRefused(
            "subject must be a conventional commit subject, e.g. 'fix(alerts): raise the fridge alert to 42 f'"
            " (feat|fix|docs|style|refactor|test|chore, optional scope, lower-case start, at most 70 characters)"
        )


def build_proposal(store: Store, repo: Repo, options: Options, *, kind: str, files: list[FileChange], subject: str,
                   why: str, effect: str, requested_by: str, day: str, now: datetime, thread_id: str | None = None,
                   run_id: str | None = None, fact_id: int | None = None,
                   entities: frozenset[str] | None = None,
                   fix_domains: frozenset[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Validate and record one proposal as a commit on refs/ops/proposals/<id> (blocking: call in a thread).

    `fix_domains` (opsd.updatefix only): the integrations an update's config change is for; it may then
    also edit those blocks of `configuration.yaml`.
    """
    check_subject(subject)
    if not options.git_on:
        raise ChangeRefused("git proposals are off on this install (the house profile does not enable git)")
    check_paths([f.path for f in files], kind=kind, rules=options.profile.proposal_rules, fix=fix_domains is not None,
                suppress=options.profile.overlay("suppress"))  # fmt: skip
    if len({f.path for f in files}) != len(files):
        raise ChangeRefused("each file may appear once")
    if not why.strip():
        raise ChangeRefused("say why the change is needed")
    if len(store.changes(states=("open",), limit=MAX_OPEN + 1)) >= MAX_OPEN:
        raise ChangeRefused(f"{MAX_OPEN} proposals are already waiting for the admin")
    if store.count(day, "change_proposals") >= MAX_PER_DAY:
        raise ChangeRefused(f"today's limit of {MAX_PER_DAY} proposals is reached")
    try:
        head = repo.main_head()
        tree = repo.run("rev-parse", f"{head}^{{tree}}").strip()
        rows: list[dict[str, Any]] = []
        diffs: list[str] = []
        summary: list[dict[str, Any]] = []
        guarded = watched_helpers(repo, head, options.notify_service)
        for change in files:
            entry = repo.entry(head, change.path)
            old = repo.file_at(head, change.path) if entry is not None else None
            if old == change.new:
                raise ChangeRefused(f"{change.path} is unchanged")
            summary.append(check_content(change.path, old, change.new, notify_service=options.notify_service,
                                         entities=entities, guarded=guarded,
                                         fix_domains=fix_domains))  # fmt: skip
            parts = change.path.split("/")
            blob = repo.hash_blob(change.new)
            tree = repo.tree_with(tree, parts, blob, new_dirs=parts[0] == CREATABLE_ROOT)
            rows.append({"path": change.path, "old_blob": entry[2] if entry else None, "new_blob": blob,
                         "old_sha256": _sha(old), "new_sha256": _sha(change.new)})  # fmt: skip
            diffs.append(_diff(change.path, old, change.new))
        commit = repo.commit_tree(tree, head, subject)
    except GitError as err:
        raise ChangeRefused(f"git: {clean_text(str(err), 200)}") from err
    row = store.create_change(
        kind=kind, subject=subject, why=clean_text(why, WHY_CHARS), effect=clean_text(effect, EFFECT_CHARS),
        state="open", requested_by=requested_by, now=now, expires_at=now + PROPOSAL_TTL, files=rows,
        diff="".join(diffs)[:DIFF_CHARS], base_sha=head, commit_sha=commit, thread_id=thread_id, run_id=run_id,
        fact_id=fact_id, summary=summary,
    )  # fmt: skip
    ref = f"{REF_PREFIX}{row['id']}"
    try:
        repo.create_ref(ref, commit)
    except GitError as err:
        store.move_change(row["id"], expect=("open",), to="failed", now=now, detail=f"git: {str(err)[:200]}")
        raise ChangeRefused(f"git: {clean_text(str(err), 200)}") from err
    store.move_change(row["id"], expect=("open",), to="open", now=now, ref=ref)
    store.bump(day, "change_proposals")
    if fact_id is not None:
        store.link_fact(fact_id, int(row["id"]))
    store.audit(at=now, run_id=run_id, principal=requested_by, channel="changes", capability=f"{kind}.propose",
                args={"id": row["id"], "paths": [f.path for f in files], "commit": commit}, tier="propose",
                decision="proposed", outcome="ok")  # fmt: skip
    found = store.change(int(row["id"]))
    if found is None:
        raise ChangeRefused("the proposal could not be saved")
    return found


def watched_helpers(repo: Repo, head: str, admin_notify: str) -> frozenset[str] | None:
    """Helpers that existing automations doing more than the allowlist react to (None: not all readable)."""
    names = [n.decode(errors="replace") for n in repo.raw("ls-tree", "-r", "-z", "--name-only", head, "--",
                                                          "automations").split(b"\0") if n]  # fmt: skip
    texts: list[str | None] = []
    for name in names:
        if not name.endswith(".yaml"):
            continue
        try:
            data = repo.file_at(head, name)
        except GitError:
            return None
        texts.append(data.decode("utf-8", errors="replace") if data is not None else None)
    return configcheck.guarded_helpers(texts, admin_notify=admin_notify)


def record_refusal(store: Store, *, kind: str, subject: str, why: str, reason: str, requested_by: str, day: str,
                   now: datetime, thread_id: str | None = None, run_id: str | None = None) -> None:  # fmt: skip
    """Keep refused attempts visible in the panel (capped per day)."""
    if not store.bump(day, "change_refusals", limit=MAX_REFUSALS_PER_DAY):
        return
    store.create_change(kind=kind, subject=clean_text(subject, 120) or "(no subject)", why=clean_text(why, WHY_CHARS),
                        effect="", state="refused", requested_by=requested_by, now=now, expires_at=now,
                        detail=clean_text(reason, 300), thread_id=thread_id, run_id=run_id)  # fmt: skip
    store.audit(at=now, run_id=run_id, principal=requested_by, channel="changes", capability=f"{kind}.propose",
                args={"subject": clean_text(subject, 120)}, tier="propose", decision="refused", outcome="refused",
                detail=clean_text(reason, 300))  # fmt: skip


def with_fact(document: str, fact: str, day: str) -> str:
    """The facts document with one more bullet in the agent's section (created at the end if missing)."""
    line = f"- {fact} (noted {day})\n"
    if FACTS_SECTION not in document:
        return document.rstrip("\n") + f"\n\n{FACTS_SECTION}\n\n{line}"
    start = document.index(FACTS_SECTION)
    nxt = document.find("\n## ", start + len(FACTS_SECTION))
    if nxt == -1:
        return document.rstrip("\n") + "\n" + line
    head, tail = document[:nxt].rstrip("\n"), document[nxt:]
    return f"{head}\n{line}{tail}"


def clean_fact(text: str) -> str:
    fact = " ".join(clean_text(text, FACT_CHARS).split()).lstrip("#>-* ").strip()
    if not fact:
        raise ChangeRefused("the fact is empty")
    return fact


def binding(repo: Repo, row: Mapping[str, Any], action: str) -> dict[str, Any] | None:
    """What an approval binds to, recomputed from git objects (None if anything no longer matches)."""
    try:
        if action == "apply" and (not row.get("ref") or repo.ref(str(row["ref"])) != row.get("commit_sha")):
            return None
        files: dict[str, list[str | None]] = {}
        for entry in row["files"]:
            path = str(entry["path"])
            new = repo.file_at(str(row["commit_sha"]), path)
            old = repo.file_at(str(row["base_sha"]), path)
            if _sha(new) != entry.get("new_sha256") or _sha(old) != entry.get("old_sha256"):
                return None
            files[path] = [_sha(old), _sha(new)]
        found: dict[str, Any] = {"proposal": int(row["id"]), "action": action, "commit": row["commit_sha"],
                                 "base": row["base_sha"], "files": files, "created_at": row["created_at"]}  # fmt: skip
        if action == "revert":
            applied = row.get("applied_sha")
            if not applied or not repo.is_ancestor(str(applied), repo.main_head()):
                return None
            found["applied"] = applied
        return found
    except GitError:
        return None


# ---------------------------------------------------------- capabilities


async def registry_entities(client: SupervisorClient) -> frozenset[str] | None:
    """Entity ids in the registry, or None when it cannot be read (then targets are refused)."""
    try:
        async with LiveAPI(client) as api:
            rows = await api.ws("config/entity_registry/list")
    except Exception as err:  # noqa: BLE001 - any failure means "unknown", which refuses targets
        _LOGGER.warning("entity registry unavailable for a proposal", extra={"error": str(err)[:200]})
        return None
    if not isinstance(rows, list):
        return None
    return frozenset(r["entity_id"] for r in rows if isinstance(r, dict) and isinstance(r.get("entity_id"), str))


def _run(ctx: ExecContext) -> RunContext:
    if ctx.run is None or ctx.run.profile not in PROFILES:
        raise CapabilityError("config changes are proposed from a chat or an investigation the admin started")
    return ctx.run


def _repo(ctx: ExecContext) -> tuple[Store, Repo]:
    if ctx.env.store is None or ctx.env.repo is None:
        raise CapabilityError("the store or the config repository is not available")
    return ctx.env.store, ctx.env.repo


def _day(ctx: ExecContext, now: datetime) -> str:
    return now.astimezone(ctx.env.tz()).date().isoformat()


def validate_propose(ctx: ExecContext, args: Args) -> Args:
    _run(ctx)
    for name in ("paths", "subject", "why"):
        if not (args.opt_text(name) or "").strip():
            raise CapabilityError(f"{name} is required")
    return args


async def exec_propose(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    store, repo = _repo(ctx)
    now = datetime.now(UTC)
    paths = split_paths(args.text("paths"))
    subject, why, effect = args.text("subject"), args.text("why"), args.opt_text("effect") or ""
    who = run.principal.label()
    drafts = ctx.paths.drafts_dir
    try:
        check_paths(paths, kind="change", rules=ctx.options.profile.proposal_rules)
        account = ops_account()
        files = [FileChange(p, read_draft(drafts, p, owner=account.uid if account else None)) for p in paths]
        entities = await registry_entities(ctx.client)
        found = await asyncio.to_thread(build_proposal, store, repo, ctx.options, kind="change", files=files,
                                        subject=subject, why=why, effect=effect, requested_by=who,
                                        day=_day(ctx, now), now=now, thread_id=run.thread_id,
                                        run_id=run.run_id, entities=entities)  # fmt: skip
    except ChangeRefused as err:
        record_refusal(store, kind="change", subject=subject, why=why, reason=str(err), requested_by=who,
                       day=_day(ctx, now), now=now, thread_id=run.thread_id, run_id=run.run_id)  # fmt: skip
        raise CapabilityError(f"proposal refused: {err}") from err
    finally:
        for path in paths:
            if allowed_path(path, ctx.options.profile.proposal_rules):
                drop_draft(drafts, path)
    return (
        f"ok: proposal #{found['id']} ({', '.join(paths)}) is waiting for the admin in Needs you on Home"
        " (Proposals). Only the admin can apply it: they approve or reject it there. Tell them"
        " what it changes and why; do not claim it is applied."
    )


def validate_fact(ctx: ExecContext, args: Args) -> Args:
    _run(ctx)
    try:
        return args.replace(text=clean_fact(args.opt_text("text") or ""))
    except ChangeRefused as err:
        raise CapabilityError(str(err)) from err


async def exec_fact(ctx: ExecContext, args: Args) -> str:
    run = _run(ctx)
    store, repo = _repo(ctx)
    now = datetime.now(UTC)
    fact = args.text("text")
    why = args.opt_text("why") or "noted in a chat with the admin"
    fact_id = store.add_fact(fact, thread_id=run.thread_id, turn_id=None, now=now)
    try:
        found = await asyncio.to_thread(propose_fact, store, repo, ctx.options, fact=fact, why=why,
                                        requested_by=run.principal.label(), day=_day(ctx, now), now=now,
                                        fact_id=fact_id, thread_id=run.thread_id, run_id=run.run_id)  # fmt: skip
    except ChangeRefused as err:
        raise CapabilityError(f"proposal refused: {err}") from err
    return (
        f"ok: house fact proposal #{found['id']} is waiting for the admin in Needs you on Home. Only the admin can"
        " apply it (they approve or reject it in the panel)."
    )


def propose_fact(store: Store, repo: Repo, options: Options, *, fact: str, why: str, requested_by: str, day: str,
                 now: datetime, fact_id: int | None = None, thread_id: str | None = None,
                 run_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """One house fact as a proposal against the profile's house facts document (blocking: call in a thread)."""
    path = options.profile.docs.facts
    if not path or not options.git_on:
        raise ChangeRefused("this install has no house facts document (the house profile's docs.facts and git)")
    try:
        current = repo.file_at(repo.main_head(), path) or b"# House facts\n"
    except GitError as err:
        raise ChangeRefused(f"git: {clean_text(str(err), 200)}") from err
    document = with_fact(current.decode("utf-8", errors="replace"), clean_fact(fact), day)
    subject = "docs(facts): add a fact noted through the ops agent"
    return build_proposal(store, repo, options, kind="facts", files=[FileChange(path, document.encode())],
                          subject=subject, why=why, effect="Documentation only: no Home Assistant behaviour changes.",
                          requested_by=requested_by, day=day, now=now, thread_id=thread_id, run_id=run_id,
                          fact_id=fact_id)  # fmt: skip


def exec_internal(_ctx: ExecContext, _args: Args) -> str:
    """`change.apply` / `change.revert` are daemon-only; the socket never reaches them."""
    raise CapabilityError("only the daemon applies changes, after the admin's approval")


# ----------------------------------------------------------------- manager


@dataclass(slots=True)
class Outcome:
    state: str
    detail: str = ""


class ChangeManager:
    def __init__(
        self,
        *,
        store: Store,
        repo: Repo,
        approvals: Approvals,
        options: Options,
        live: LiveFiles,
        check_config: CheckConfig,
        reload: Reload,
        send: Callable[[Message], Awaitable[str]],
        publish: Callable[[], None] = lambda: None,
        lock: asyncio.Lock | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.repo = repo
        self.approvals = approvals
        self.options = options
        self.live = live
        self._check_config = check_config
        self._reload = reload
        self._send = send
        self._publish = publish
        self.lock = lock or asyncio.Lock()
        self._clock = clock
        # `ActivityLog.record` (set by the daemon).
        self.activity: Callable[..., Any] | None = None

    # ------------------------------------------------------------ admin side

    def _audit(self, capability: str, row_id: int, *, outcome: str, detail: str = "", by: str = "daemon") -> None:
        try:
            self.store.audit(at=self._clock(), run_id=None, principal=by, channel="changes", capability=capability,
                             args={"id": row_id}, tier="approve", decision=outcome, outcome=outcome,
                             detail=detail[:300])  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    async def request(self, change_id: int, action: str, user: str, shown: list[str] | None = None) -> dict[str, Any]:
        """A pending change approval (the panel approves it in the same request: `confirm`).

        `shown`: the file hashes the panel rendered the diff for; they must be this proposal's,
        so an approval is only ever requested for the diff the admin was looking at.
        """
        row = self.store.change(change_id)
        expect = "open" if action == "apply" else "applied"
        if row is None or row["state"] != expect:
            raise ApprovalError(f"this proposal cannot be {'applied' if action == 'apply' else 'reverted'} now")
        if sorted(shown or []) != sorted(str(f["new_sha256"]) for f in row["files"]):
            raise ApprovalError("review the current diff first (the panel did not show this version)")
        if not self.options.acting and not all(is_doc(f["path"]) for f in row["files"]):
            raise ApprovalError("observe mode: Home Assistant config changes apply only in agent.mode: act")
        found = await asyncio.to_thread(binding, self.repo, row, action)
        if found is None:
            raise ApprovalError("the proposal no longer matches git; it cannot be approved")
        return self.approvals.request_change(found, action=action, title=row["subject"], user=user)

    async def confirm(self, approval_id: str, user: str) -> str:
        found = self.store.approval(approval_id)
        if found is None or not is_change(found):
            return "unknown"
        plan = found["plan"]
        row = self.store.change(int((plan.get("binding") or {}).get("proposal") or 0))
        now_binding = await asyncio.to_thread(binding, self.repo, row, str(plan.get("action"))) if row else None
        return self.approvals.confirm_change(approval_id, user=user, binding_now=now_binding)

    def reject(self, change_id: int, user: str) -> bool:
        row = self.store.change(change_id)
        if row is None or not self.store.move_change(change_id, expect=("open",), to="rejected", now=self._clock(),
                                                     decided_at=self._clock().isoformat(timespec="seconds"),
                                                     detail=f"rejected by {user[:40]}"):  # fmt: skip
            return False
        self._drop_ref(row)
        self.approvals.void_subject(f"change:{change_id}:apply", detail="rejected")
        if row.get("fact_id"):
            self.store.decide_fact(int(row["fact_id"]), "rejected", now=self._clock())
        self._audit("change.reject", change_id, outcome="rejected", by=f"panel:{user[:64]}")
        self._publish()
        return True

    def expire(self) -> int:
        now = self._clock()
        expired = 0
        for row in self.store.changes(states=("open",), limit=100):
            if now < datetime.fromisoformat(row["expires_at"]):
                continue
            if self.store.move_change(row["id"], expect=("open",), to="expired", now=now):
                self._drop_ref(row)
                self.approvals.void_subject(f"change:{row['id']}:apply", detail="the proposal expired")
                expired += 1
        return expired

    def _drop_ref(self, row: Mapping[str, Any]) -> None:
        if not row.get("ref"):
            return
        try:
            self.repo.delete_ref(str(row["ref"]))
        except GitError as err:
            _LOGGER.warning("proposal ref not deleted", extra={"ref": row["ref"], "error": str(err)[:200]})

    # ------------------------------------------------------------- pipeline

    def _authorize(self, action: str, row_id: int, *, doc_only: bool) -> None:
        """Policy and audit: the consumed approval lifts exactly the APPROVE tier."""
        cap = CAPABILITIES[f"change.{action}"]
        tighten = {n: PolicyTier.APPROVE for n in self.options.capabilities.require_approval}
        tighten.update({n: PolicyTier.NEVER for n in self.options.capabilities.never})
        verdict = decide(
            capability=cap.name,
            default_tier=cap.tier,
            effects=cap.effects,
            origin=Origin.AGENT_SELF,
            allowed_origins=cap.origins,
            acting=self.options.acting or doc_only,
            tighten=tighten,
        )
        if verdict.tier is not PolicyTier.APPROVE:
            self._audit(cap.name, row_id, outcome="refused", detail="; ".join(verdict.reasons))
            raise ChangeRefused(f"{cap.name} is not allowed ({'; '.join(verdict.reasons)})")

    def _preconditions(self, row: Mapping[str, Any], action: str) -> tuple[str, str, dict[str, bytes | None]]:
        """`(status, detail, before)`: status `ok`, `busy` or `stale`; `before` the bytes expected now."""
        paths = [str(f["path"]) for f in row["files"]]
        if not self.repo.on_main():
            return "busy", "the config repository is not on main", {}
        head = self.repo.main_head()
        dirty = self.repo.dirty_paths()
        if dirty:
            return "busy", f"the config has uncommitted changes ({', '.join(dirty[:3])}); try again later", {}
        before: dict[str, bytes | None] = {}
        if action == "apply":
            moved = self.repo.changed_between(str(row["base_sha"]), head, paths)
            if moved:
                return "stale", f"changed on main since it was proposed: {', '.join(moved)}; re-propose", {}
            for path in paths:
                before[path] = self.repo.file_at(head, path)
        else:
            for entry in row["files"]:
                current = self.repo.file_at(head, str(entry["path"]))
                if _sha(current) != entry.get("new_sha256"):
                    return "stale", f"{entry['path']} changed after the change was applied; revert it by hand", {}
                before[str(entry["path"])] = current
        for path, expected in before.items():
            try:
                if self.live.read(path) != expected:
                    return "busy", f"{path} differs from git in the working tree", {}
            except OSError as err:
                return "stale", f"{path}: {clean_text(str(err), 120)}", {}
        return "ok", "", before

    def _targets(self, row: Mapping[str, Any], action: str) -> dict[str, bytes | None]:
        source = str(row["commit_sha"] if action == "apply" else row["base_sha"])
        return {str(f["path"]): self.repo.file_at(source, str(f["path"])) for f in row["files"]}

    def _write_all(self, files: Mapping[str, bytes | None], created: list[str]) -> None:
        for path, data in files.items():
            self.live.write(path, data, created)

    def _restore(self, before: Mapping[str, bytes | None], created: list[str]) -> str:
        try:
            self._write_all(before, [])
            self.live.remove_dirs(created)
        except OSError as err:
            _LOGGER.error("restore failed", extra={"error": str(err)[:300]})
            return f" Restoring the previous files FAILED: {clean_text(str(err), 120)}."
        return " The previous files were restored."

    async def _reloads(self, paths: list[str]) -> None:
        tops = {p.split("/", 1)[0] for p in paths}
        if "custom_templates" in tops:
            # Macros first, then the templates that import them.
            await self._reload("homeassistant", "reload_custom_templates")
            tops.add("templates")
        for top in sorted(tops):
            if top in RELOADS:
                await self._reload(*RELOADS[top])

    async def execute(self, approval_id: str) -> Outcome:
        """Apply or revert the change an approved approval names (one git write at a time)."""
        found = self.store.approval(approval_id)
        if found is None or not is_change(found) or found["state"] != "approved":
            return Outcome("refused", "no approved change approval")
        plan = found["plan"]
        action = str(plan.get("action"))
        change_id = int((plan.get("binding") or {}).get("proposal") or 0)
        async with self.lock:
            try:
                outcome = await self._execute(approval_id, change_id, action)
            except ChangeRefused as err:
                self.approvals.void_subject(f"change:{change_id}:{action}", detail=str(err)[:200])
                outcome = Outcome("refused", str(err))
                # The card says why (the row keeps its state unless the refusal moved it, e.g. stale).
                expect = ("open",) if action == "apply" else ("applied",)
                verb = "applied" if action == "apply" else "reverted"
                self.store.move_change(change_id, expect=expect, to=expect[0], now=self._clock(),
                                       detail=f"Not {verb}: {err}"[:300])  # fmt: skip
            except (GitError, OSError, StoreError, sqlite3.Error) as err:
                _LOGGER.error("change pipeline error", extra={"id": change_id, "error": str(err)[:300]})
                outcome = Outcome("failed", clean_text(str(err), 200))
        self._audit(f"change.{action}", change_id, outcome=outcome.state, detail=outcome.detail)
        if self.activity is not None:
            self.activity("change", f"Config change #{change_id} ({action}): {outcome.state}", outcome.detail,
                          needs_admin=outcome.state in ("failed", "refused"), ref={"change": change_id},
                          item=f"change:{change_id}")  # fmt: skip
        self._publish()
        return outcome

    async def _execute(self, approval_id: str, change_id: int, action: str) -> Outcome:
        row = self.store.change(change_id)
        expect = "open" if action == "apply" else "applied"
        if row is None or row["state"] != expect:
            raise ChangeRefused(f"proposal #{change_id} is not {expect}")
        paths = [str(f["path"]) for f in row["files"]]
        doc_only = all(is_doc(p) for p in paths)
        if not self.options.acting and not doc_only:
            raise ChangeRefused("observe mode: Home Assistant config changes apply only in agent.mode: act")
        self._authorize(action, change_id, doc_only=doc_only)

        async def consume() -> bool:
            current = await asyncio.to_thread(binding, self.repo, row, action)
            return self.approvals.consume_change(approval_id, change_id, current)

        return await self._pipeline(row, action, consume=consume, doc_only=doc_only)

    async def _pipeline(self, row: Mapping[str, Any], action: str, *, consume: Callable[[], Awaitable[bool]],
                        doc_only: bool) -> Outcome:  # fmt: skip
        """Write, check, reload, commit and push one change (or its revert); restore on any failure.

        `consume`: proves the right to run it (the approval) and is called once the preconditions hold.
        """
        change_id = int(row["id"])
        expect = "open" if action == "apply" else "applied"
        paths = [str(f["path"]) for f in row["files"]]
        status, detail, before = await asyncio.to_thread(self._preconditions, row, action)
        now = self._clock()
        if status == "stale":
            if action == "apply" and self.store.move_change(change_id, expect=("open",), to="stale", now=now,
                                                            detail=detail):  # fmt: skip
                self._drop_ref(row)
            raise ChangeRefused(f"stale: {detail}")
        if status != "ok":
            raise ChangeRefused(f"{detail}; approve again when it is clean")
        if not await consume():
            raise ChangeRefused("the approval is no longer valid for this change")
        running = "applying" if action == "apply" else "reverting"
        if not self.store.move_change(change_id, expect=(expect,), to=running, now=now):
            raise ChangeRefused("the proposal changed meanwhile")
        self._audit(f"change.{action}", change_id, outcome="approved", detail="approval consumed")
        # The card shows "Approved — applying…" while it runs.
        self._publish()
        targets = await asyncio.to_thread(self._targets, row, action)
        created: list[str] = []
        try:
            await asyncio.to_thread(self._write_all, targets, created)
        except OSError as err:
            note = await asyncio.to_thread(self._restore, before, created)
            return self._fail(change_id, running, f"writing failed: {clean_text(str(err), 120)}.{note}")
        self._audit(f"change.{action}", change_id, outcome="written", detail=", ".join(paths))
        if not doc_only:
            valid, why = await self._check_config()
            if not valid:
                note = await asyncio.to_thread(self._restore, before, created)
                await self._notify(
                    f"Config change #{change_id} not applied",
                    f"Core's config check failed: {clean_text(why, 300)}.{note}",
                    urgent=False,
                )
                return self._fail(change_id, running, f"config check failed: {clean_text(why, 200)}.{note}")
            self._audit(f"change.{action}", change_id, outcome="checked", detail="config check passed")
            try:
                await self._reloads(paths)
            except Exception as err:  # noqa: BLE001 - any reload failure restores the previous config
                note = await asyncio.to_thread(self._restore, before, created)
                again = await self._reload_again(paths)
                await self._notify(f"Config change #{change_id} rolled back",
                                   f"The reload failed ({clean_text(str(err), 160)}).{note}{again} Check the"
                                   " automations and templates.", urgent=True)  # fmt: skip
                return self._fail(change_id, running, f"reload failed: {clean_text(str(err), 160)}.{note}{again}")
        subject = str(row["subject"]) if action == "apply" else f"chore(ops-agent): revert config change {change_id}"
        try:
            sha = await asyncio.to_thread(self.repo.commit_paths, paths, subject)
        except GitError as err:
            note = await asyncio.to_thread(self._restore, before, created)
            again = "" if doc_only else await self._reload_again(paths)
            await self._notify(
                f"Config change #{change_id} rolled back",
                f"The commit failed ({clean_text(str(err), 160)}).{note}{again}",
                urgent=not doc_only,
            )
            return self._fail(change_id, running, f"commit failed: {clean_text(str(err), 160)}.{note}{again}")
        pushed = await self._push()
        note = " It takes effect at the next Core restart (the agent never restarts Core for a change)." if any(
            needs_restart(p) for p in paths) else ""  # fmt: skip
        stamp = self._clock().isoformat(timespec="seconds")
        if action == "apply":
            self.store.move_change(change_id, expect=(running,), to="applied", now=self._clock(), applied_sha=sha,
                                   decided_at=stamp, detail=f"committed {sha[:10]}{pushed}.{note}")  # fmt: skip
            self._drop_ref(row)
            if row.get("fact_id"):
                self.store.decide_fact(int(row["fact_id"]), "applied", now=self._clock())
        else:
            self.store.move_change(change_id, expect=(running,), to="reverted", now=self._clock(), reverted_sha=sha,
                                   detail=f"reverted in {sha[:10]}{pushed}.{note}")  # fmt: skip
        return Outcome("applied" if action == "apply" else "reverted", f"{sha[:10]}{pushed}{note}")

    async def _reload_again(self, paths: list[str]) -> str:
        try:
            await self._reloads(paths)
        except Exception as err:  # noqa: BLE001 - reported to the admin
            return f" Reloading the restored config also failed: {clean_text(str(err), 120)}."
        return " The restored config was reloaded."

    async def _push(self) -> str:
        if not self.options.limits.git_push:
            return ", not pushed (limits.git_push is off)"
        try:
            await asyncio.to_thread(self.repo.push)
        except GitBusyError, GitError:
            return ", not pushed yet (origin moved or unreachable)"
        return ", pushed"

    def _fail(self, change_id: int, running: str, detail: str) -> Outcome:
        self.store.move_change(change_id, expect=(running,), to="failed", now=self._clock(), detail=detail[:300])
        return Outcome("failed", detail)

    async def _notify(self, title: str, body: str, *, urgent: bool) -> None:
        budget = BudgetClass.SAFETY if urgent else BudgetClass.AGENT
        try:
            await self._send(Message(f"Ops Agent: {title}"[:70], body[:900], urgent=urgent, budget_class=budget,
                                     tag=CHANGE_ESCALATION_TAG))  # fmt: skip
        except Exception:
            _LOGGER.exception("change notice failed")

    def recover(self) -> int:
        """At start: a change interrupted mid-write is finished if it was committed, else restored."""
        fixed = 0
        for row in self.store.changes(states=("applying", "reverting"), limit=20):
            action = "apply" if row["state"] == "applying" else "revert"
            try:
                head = self.repo.main_head()
                target = self._targets(row, action)
                if all(self.repo.file_at(head, p) == data for p, data in target.items()):
                    state = "applied" if action == "apply" else "reverted"
                    sha = {"applied_sha": head} if action == "apply" else {"reverted_sha": head}
                    self.store.move_change(row["id"], expect=(row["state"],), to=state, now=self._clock(),
                                           detail="finished after a restart", **sha)  # fmt: skip
                else:
                    before = {p: self.repo.file_at(head, p) for p in target}
                    note = self._restore(before, [])
                    self.store.move_change(
                        row["id"],
                        expect=(row["state"],),
                        to="failed",
                        now=self._clock(),
                        detail=f"interrupted by a restart.{note} Reload or restart if needed.",
                    )
            except (GitError, OSError) as err:
                self.store.move_change(
                    row["id"],
                    expect=(row["state"],),
                    to="failed",
                    now=self._clock(),
                    detail=f"interrupted; could not recover: {clean_text(str(err), 160)}",
                )
            fixed += 1
        return fixed
