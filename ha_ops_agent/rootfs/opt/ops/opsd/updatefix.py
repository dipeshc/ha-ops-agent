"""The config change an update needs, as a one-tap proposal.

When the update rule finds that a Core update needs a change in this house's config (Home Assistant's
own notes name an integration the house uses, or a repair says something breaks in that version), the
update waits and the capability-free release-note review may write the change as exact-text edits. The
daemon turns them into an ordinary proposal on `refs/ops/proposals/<id>` (`changes.build_proposal`: the
path allowlist, the content rules, the fail-closed YAML allowlist, five a day, five open), recorded for
exactly that update (`requested_by` = `update-fix:<entity>@<version>`). The admin approves it in one
step on its item, and the normal change pipeline checks the config, reloads, commits and pushes. Once it
is applied the update is no longer held by it, and installs in the next window. Nothing here applies
anything; the admin can always approve the update itself instead.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from opsd.changepaths import fix_path
from opsd.changes import ChangeRefused, FileChange, build_proposal
from opsd.config import Options
from opsd.conversation import clean_text
from opsd.gitops import GitError, Repo
from opsd.store import Store

PREFIX = "update-fix:"
MAX_EDITS = 6
MAX_FILES = 3
MAX_EDIT_CHARS = 2000
_SUBJECT_RE = re.compile(r"[a-z0-9 ():._-]{1,70}")


@dataclass(frozen=True, slots=True)
class Edit:
    path: str
    old: str
    new: str


def requested_by(entity_id: str, version: str) -> str:
    return f"{PREFIX}{entity_id}@{version}"[:100]


def parse_edits(raw: Any) -> list[Edit]:
    """Strictly shaped edits from the review's answer; anything else is dropped."""
    found: list[Edit] = []
    for entry in raw[:MAX_EDITS] if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            continue
        path, old, new = entry.get("path"), entry.get("old"), entry.get("new")
        if not isinstance(path, str) or not isinstance(old, str) or not isinstance(new, str):
            continue
        if old and old != new and len(old) <= MAX_EDIT_CHARS and len(new) <= MAX_EDIT_CHARS:
            found.append(Edit(path.strip(), old, new))
    return found


def fix_record(store: Store, entity_id: str, version: str) -> dict[str, Any] | None:
    """The newest proposal made for this update, as `{"change": id, "state": state}` (the update's link)."""
    who = requested_by(entity_id, version)
    row = next((r for r in store.changes(limit=50) if r.get("requested_by") == who and r["state"] != "refused"), None)
    return {"change": int(row["id"]), "state": str(row["state"])} if row is not None else None


def apply_edits(repo: Repo, edits: list[Edit]) -> list[FileChange]:
    """The new contents of each edited file on `main`: each `old` must occur exactly once in a fix path."""
    if not edits:
        raise ChangeRefused("no edits")
    head = repo.main_head()
    texts: dict[str, str] = {}
    for edit in edits:
        if not fix_path(edit.path):
            raise ChangeRefused(f"{edit.path[:80]!r} is not a file an update's config change may touch")
        if edit.path not in texts:
            data = repo.file_at(head, edit.path)
            if data is None:
                raise ChangeRefused(f"{edit.path} does not exist on main")
            texts[edit.path] = data.decode("utf-8", errors="replace")
        if len(texts) > MAX_FILES:
            raise ChangeRefused(f"at most {MAX_FILES} files")
        if texts[edit.path].count(edit.old) != 1:
            raise ChangeRefused(f"{edit.path}: the text to replace must occur exactly once")
        texts[edit.path] = texts[edit.path].replace(edit.old, edit.new, 1)
    return [FileChange(path, text.encode()) for path, text in sorted(texts.items())]


def propose(store: Store, repo: Repo, options: Options, *, item: Mapping[str, Any], why: str, edits: list[Edit],
            domains: frozenset[str], day: str, now: datetime,
            entities: frozenset[str] | None) -> dict[str, Any]:  # fmt: skip
    """Record the update's config change as a proposal (blocking: call in a thread). ChangeRefused when not."""
    entity, version = str(item["entity_id"]), str(item["latest"])
    try:
        files = apply_edits(repo, edits)
    except GitError as err:
        raise ChangeRefused(f"git: {clean_text(str(err), 120)}") from err
    title = re.sub(r"[^a-z0-9 ._-]+", "", str(item.get("title") or entity).lower())[:30].strip() or "update"
    subject = f"fix(updates): adapt config to {title} {version}"[:70]
    return build_proposal(
        store, repo, options, kind="change", files=files,
        subject=subject if _SUBJECT_RE.fullmatch(subject) else "fix(updates): adapt config for an update",
        why=clean_text(why, 590) or "Home Assistant's notes for this update",
        effect=f"Needed by {entity} {version}, which waits for it: once applied, the update installs in the next "
        "window.", requested_by=requested_by(entity, version), day=day, now=now, entities=entities,
        fix_domains=domains,
    )  # fmt: skip
