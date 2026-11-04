"""Rebase a custom-integration fork onto Home Assistant Core: built by the daemon, applied only when approved.

A fork is a custom integration with the domain of a Core integration (`custom_components/<domain>/` shadowing
`homeassistant/components/<domain>/`). The AI only names the fork, the reason, and which of the fork's own
changes to keep (`fork.propose`, verb `propose-rebase`); it never supplies file content. The daemon builds
everything else from two sources it trusts:

1. **Base**: Core's files for the exact installed Core version (`opsd.coresource`: GitHub's contents API and raw
   file host, each file checked against the git blob SHA the listing gives for that tag).
2. **The fork's delta**: the Core release the fork was copied from is the newest tag among the installed one and
   the `.0` releases of the last six months (`coresource.window_tags`) whose files differ least from the fork's.
   The fork's own changes are the unified-diff hunks from that release to the fork (`fork_hunks`, numbered
   `<file>#<n>`). Only the hunks the AI keeps are applied onto the base, each by its exact old text plus context,
   which must occur exactly once (`apply_hunks`); anything else refuses, never guesses.

The result covers the files the fork, the base and the fork's origin all have; every other file of the fork stays
as it is. It must parse (`ast.parse`), keep the fork's domain in `manifest.json` (which gets `version` set to the
Core version, as a custom integration needs one), import nothing that neither the base nor the fork imports,
reference only modules the fork has, add no file, and stay within `MAX_RESULT_BYTES`.

The proposal is a record in the store's key-value table (`PROPOSAL_KEY`) plus the result's bytes under
`/data/fork-rebase/<id>/new/` (root-only). Its approval (`approvals`, kind `fork_rebase`) binds the proposal,
the domain, the Core version, the fork's origin, the kept hunks, the SHA-256 of the result's bytes and of the
fork's current files (`binding`, recomputed from disk by `binding_now`). It is applied only through the update
pipeline (`updates.UpdateManager`, a job of kind `fork_rebase`): an encrypted backup, the old files snapshotted
(`ForkFiles`), the new ones written, Core's config check, a Core restart inside its approved scope, the health
diff, and a restore with a second restart on any failure or regression.
"""

import ast
import asyncio
import contextlib
import difflib
import hashlib
import itertools
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsd import maintenance, reload, selfguard
from opsd.api import LiveAPI
from opsd.capabilities import FORK_KEEP_RE, Args, CapabilityError
from opsd.conversation import clean_text, fence
from opsd.coresource import (
    FORK_MONTHS,
    MAX_FILE_BYTES,
    CoreSource,
    CoreSourceError,
    LocalFiles,
    custom_dir,
    cut_text,
    domain_fd,
    installed_core_version,
    listed,
    read_at,
    read_custom,
    valid_domain,
    valid_file,
    valid_tag,
    window_tags,
)
from opsd.jsonstore import write_json_atomic
from opsd.runctx import ExecContext
from opsd.store import Store, StoreError
from opsd.supervisor import ApiError

_LOGGER = logging.getLogger(__name__)

CONTEXT = 3
MAX_RESULT_BYTES = 1024 * 1024
MAX_KEEP = 40
MAX_DIFF_BYTES = 32 * 1024
PROPOSAL_KEY = "fork:proposal:"
FILES_DIR = "fork-rebase"
PROPOSAL_ID_RE = re.compile(r"[0-9a-f]{16}")
HUNK_ID_RE = re.compile(r"([A-Za-z0-9_.-]{1,64})#([1-9][0-9]{0,2})")
# What the AI may pass as `--keep`: hunk ids, comma-separated, or `none` (the registry's pattern).
KEEP_RE = FORK_KEEP_RE
JOB_KIND = "fork_rebase"
ENTITY_PREFIX = "fork."
# A proposal's life: open (waits for the admin), applying (its job runs), applied, rolled_back, failed, rejected,
# stale (the fork, Core or a newer proposal moved past it).
STATES = frozenset({"open", "applying", "applied", "rolled_back", "failed", "rejected", "stale"})
FINAL = frozenset({"applied", "rolled_back", "failed", "rejected", "stale"})
KEEP_PROPOSALS = 20
# The detail a commit leaves when its push did not go out (a fork rebase's, a config change's with a trailing
# period and an optional note).
STALE_PUSH_RE = re.compile(
    r"(?P<head>.*?)(?P<verb>committed|reverted in) (?P<sha>[0-9a-f]{7,40}), not pushed yet "
    r"\(origin moved or unreachable\)(?P<tail>.*)",
    re.DOTALL,
)
# The least share of the fork's Python lines that must match its origin release for "still needed" to be
# believed: a fork that is far from every release cannot be told from Core's changes.
MIN_SIMILARITY = 0.6


class RebaseRefused(ValueError):
    """The rebase cannot be built or applied as asked; the message is safe to show."""


def entity_of(domain: str) -> str:
    """The update job's entity id for a fork rebase (never an `update.` entity)."""
    return f"{ENTITY_PREFIX}{domain}"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ hunks


@dataclass(frozen=True, slots=True)
class Hunk:
    """One of the fork's own changes against its origin: the origin's lines (context included) and the fork's."""

    id: str
    file: str
    old_start: int
    new_start: int
    old: tuple[str, ...]
    new: tuple[str, ...]

    @property
    def counts(self) -> tuple[int, int]:
        """(added, removed) lines."""
        matcher = difflib.SequenceMatcher(None, self.old, self.new, autojunk=False)
        added = removed = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != "equal":
                removed += i2 - i1
                added += j2 - j1
        return added, removed

    def text(self) -> str:
        lines = [f"@@ {self.id} -{self.old_start},{len(self.old)} +{self.new_start},{len(self.new)} @@"]
        matcher = difflib.SequenceMatcher(None, self.old, self.new, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                lines += [" " + line.rstrip("\r\n") for line in self.old[i1:i2]]
                continue
            lines += ["-" + line.rstrip("\r\n") for line in self.old[i1:i2]]
            lines += ["+" + line.rstrip("\r\n") for line in self.new[j1:j2]]
        return "\n".join(lines)


def fork_hunks(origin: Mapping[str, str], fork: Mapping[str, str]) -> list[Hunk]:
    """The fork's changes against its origin, per file both have, numbered `<file>#<n>` (unified-diff grouping
    with `CONTEXT` lines, so hunks never share a line)."""
    found: list[Hunk] = []
    for name in sorted(set(origin) & set(fork)):
        a = origin[name].splitlines(keepends=True)
        b = fork[name].splitlines(keepends=True)
        matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for n, group in enumerate(matcher.get_grouped_opcodes(CONTEXT), 1):
            i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
            found.append(Hunk(f"{name}#{n}", name, i1 + 1, j1 + 1, tuple(a[i1:i2]), tuple(b[j1:j2])))
    return found


def apply_hunks(base: str, hunks: Sequence[Hunk], *, version: str) -> str:
    """`base` with each hunk's origin lines replaced by the fork's. Each hunk's origin lines (context included) must
    occur exactly once in `base`, and no two hunks may touch the same lines; otherwise RebaseRefused."""
    lines = base.splitlines(keepends=True)
    spans: list[tuple[int, int, Hunk]] = []
    for hunk in hunks:
        size = len(hunk.old)
        if size == 0:
            raise RebaseRefused(f"{hunk.id} has no lines to anchor it in Core {version}'s {hunk.file}")
        first = hunk.old[0]
        found = [
            i for i in range(len(lines) - size + 1) if lines[i] == first and tuple(lines[i : i + size]) == hunk.old
        ]
        if len(found) != 1:
            why = "its lines are not in it" if not found else "its lines occur more than once"
            raise RebaseRefused(f"{hunk.id} does not apply cleanly to Core {version}'s {hunk.file}: {why}")
        spans.append((found[0], found[0] + size, hunk))
    spans.sort(key=lambda s: s[0])
    for (_, end, one), (start, _, two) in itertools.pairwise(spans):
        if start < end:
            raise RebaseRefused(f"{one.id} and {two.id} overlap in Core {version}'s {one.file}")
    out: list[str] = []
    at = 0
    for start, end, hunk in spans:
        out += lines[at:start]
        out += hunk.new
        at = end
    out += lines[at:]
    return "".join(out)


def parse_keep(raw: str) -> list[str]:
    """Hunk ids from `--keep` (`none` keeps nothing), each once."""
    if raw == "none":
        return []
    if not KEEP_RE.fullmatch(raw):
        raise RebaseRefused("--keep is hunk ids like fan.py#2, comma-separated, or none")
    ids: list[str] = []
    for part in raw.split(","):
        if part not in ids:
            ids.append(part)
    return ids[:MAX_KEEP]


# ------------------------------------------------------------------ the fork's origin


@dataclass(slots=True)
class Origin:
    tag: str
    texts: dict[str, str]
    score: int


def _score(origin: Mapping[str, str], fork: Mapping[str, str]) -> int:
    """Changed lines between an origin candidate and the fork; a fork file the candidate lacks counts whole."""
    total = 0
    for name, text in fork.items():
        if name not in origin:
            total += len(text.splitlines())
            continue
        a, b = origin[name].splitlines(), text.splitlines()
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if tag != "equal":
                total += (i2 - i1) + (j2 - j1)
    return total


def find_origin(source: CoreSource, domain: str, installed: str, fork: Mapping[str, str]) -> Origin:
    """The newest Core tag (the installed one, then the `.0` releases of the last `FORK_MONTHS` months) whose files
    differ least from the fork's. RebaseRefused when no tag in the window has the integration."""
    best: Origin | None = None
    for tag in window_tags(installed):
        listing = source.listing(tag, domain)
        if listing.missing:
            continue
        texts = {name: source.text(listing, name) for name in sorted(set(fork) & set(listing.files))}
        score = _score(texts, fork)
        if best is None or score < best.score:
            best = Origin(tag, texts, score)
        if score == 0:
            break
    if best is None:
        raise RebaseRefused(f"no Core release of the last months searched has an integration named {domain}")
    return best


# ------------------------------------------------------------------ checks


def _imports(tree: ast.AST) -> set[tuple[int, str, str]]:
    """(level, module, name) of every import (`import a.b` is (0, "a.b", ""))."""
    found: set[tuple[int, str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update((0, alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.update((node.level, node.module or "", alias.name) for alias in node.names)
    return found


def _parse(name: str, text: str) -> ast.AST:
    try:
        return ast.parse(text, filename=name)
    except (SyntaxError, ValueError) as err:
        raise RebaseRefused(f"the rebased {name} is not valid Python: {str(err)[:120]}") from err


def check_result(result: dict[str, str], *, base: Mapping[str, str], fork: LocalFiles, domain: str,
                 version: str) -> dict[str, str]:  # fmt: skip
    """The content checks (module docstring); returns the result with the manifest's version set."""
    extra = sorted(set(result) - set(fork.files))
    if extra:
        raise RebaseRefused(f"the rebase would add files the fork does not have: {', '.join(extra[:5])}")
    if "manifest.json" not in result:
        raise RebaseRefused("the rebase must cover manifest.json (the fork, its origin and Core all have one)")
    try:
        manifest = json.loads(result["manifest.json"])
        fork_manifest = json.loads(fork.files["manifest.json"])
    except ValueError as err:
        raise RebaseRefused(f"manifest.json is not JSON: {str(err)[:80]}") from err
    if not isinstance(manifest, dict) or not isinstance(fork_manifest, dict):
        raise RebaseRefused("manifest.json is not a JSON object")
    if fork_manifest.get("domain") != domain or manifest.get("domain") != domain:
        raise RebaseRefused(f"manifest.json must keep the fork's domain {domain}")
    manifest["version"] = version
    result = {**result, "manifest.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"}
    allowed: set[tuple[int, str, str]] = set()
    for name, text in [*base.items(), *fork.files.items()]:
        if name.endswith(".py"):
            try:
                allowed |= _imports(ast.parse(text, filename=name))
            except SyntaxError, ValueError:
                continue
    modules = {n.removesuffix(".py") for n in fork.files if n.endswith(".py")} | set(fork.dirs)
    for name, text in sorted(result.items()):
        if not name.endswith(".py"):
            continue
        imported = _imports(_parse(name, text))
        new = sorted(imported - allowed)
        if new:
            level, module, what = new[0]
            shown = f"{'.' * level}{module}" + (f" import {what}" if what else "")
            raise RebaseRefused(f"the rebased {name} imports {shown}, which neither Core's base nor the fork imports")
        for level, module, what in imported:
            if level > 1:
                raise RebaseRefused(f"the rebased {name} imports from outside the integration")
            target = module.split(".")[0] if module else what
            if level == 1 and module and target not in modules:
                raise RebaseRefused(f"the rebased {name} imports .{module}, which the fork does not have")
    size = sum(len(text.encode()) for text in result.values())
    if size > MAX_RESULT_BYTES or any(len(t.encode()) > MAX_FILE_BYTES for t in result.values()):
        raise RebaseRefused(f"the rebased integration is larger than {MAX_RESULT_BYTES // 1024} KiB")
    return result


# ------------------------------------------------------------------ the build


@dataclass(slots=True)
class Build:
    domain: str
    core: str
    origin: str
    keep: list[str]
    hunks: list[Hunk]
    files: dict[str, bytes]
    old: dict[str, bytes]
    unchanged: list[str] = field(default_factory=list)

    @property
    def result_hash(self) -> str:
        return files_hash(self.files)

    @property
    def fork_hash(self) -> str:
        return files_hash(self.old)


def files_hash(files: Mapping[str, bytes]) -> str:
    """SHA-256 over each file's name and the SHA-256 of its bytes, in name order."""
    canonical = json.dumps({name: sha256(data) for name, data in sorted(files.items())}, sort_keys=True)
    return sha256(canonical.encode())


def build(source: CoreSource, custom_components: Path, domain: str, installed: str, keep: list[str]) -> Build:
    """The rebased files of a fork (module docstring). RebaseRefused or CoreSourceError when it cannot be built."""
    fork = read_custom(custom_components, domain)
    if "manifest.json" not in fork.files:
        raise RebaseRefused(f"custom_components/{domain}/manifest.json is not readable text")
    listing = source.listing(installed, domain)
    if listing.missing:
        raise RebaseRefused(f"Home Assistant Core {installed} has no integration named {domain}: not a fork")
    base = {name: source.text(listing, name) for name in sorted(set(fork.files) & set(listing.files))}
    origin = find_origin(source, domain, installed, fork.files)
    hunks = fork_hunks(origin.texts, fork.files)
    known = {h.id for h in hunks}
    unknown = [k for k in keep if k not in known]
    if unknown:
        raise RebaseRefused(f"no such hunk of the fork against Core {origin.tag}: {', '.join(unknown[:5])}")
    covered = sorted(set(fork.files) & set(base) & set(origin.texts))
    result: dict[str, str] = {}
    for name in covered:
        kept = [h for h in hunks if h.file == name and h.id in keep]
        result[name] = apply_hunks(base[name], kept, version=installed)
    result = check_result(result, base=base, fork=fork, domain=domain, version=installed)
    files = {name: text.encode() for name, text in result.items()}
    old = {name: fork.files[name].encode() for name in files}
    return Build(domain, installed, origin.tag, sorted(keep), hunks, files, old,
                 unchanged=sorted(set(fork.files) - set(files)))  # fmt: skip


def diff_text(old: Mapping[str, bytes], new: Mapping[str, bytes], domain: str) -> str:
    """The per-file diff from the fork as it is to the rebased files, capped at `MAX_DIFF_BYTES`."""
    parts = []
    for name in sorted(new):
        a, b = old.get(name, b"").decode(), new[name].decode()
        lines = difflib.unified_diff(a.splitlines(), b.splitlines(), f"custom_components/{domain}/{name} (now)",
                                     f"custom_components/{domain}/{name} (rebased)", lineterm="", n=3)  # fmt: skip
        parts.append("\n".join(lines))
    body = "\n\n".join(p for p in parts if p)
    data = body.encode()
    if len(data) > MAX_DIFF_BYTES:
        cut = data[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore")
        body = cut + f"\n[diff truncated at {MAX_DIFF_BYTES // 1024} KiB]"
    return body


# ------------------------------------------------------------------ proposals


def _dir(data: Path, proposal_id: str) -> Path:
    if not PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise RebaseRefused("not a proposal id")
    return data / FILES_DIR / proposal_id


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _read_regular(path: Path, limit: int = MAX_FILE_BYTES) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    return None if len(data) > limit else data


def _write_private(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load(store: Store, proposal_id: str) -> dict[str, Any] | None:
    if not PROPOSAL_ID_RE.fullmatch(proposal_id):
        return None
    try:
        raw = json.loads(store.kv_get(PROPOSAL_KEY + proposal_id) or "null")
    except StoreError, ValueError:
        return None
    return raw if isinstance(raw, dict) and raw.get("id") == proposal_id and raw.get("state") in STATES else None


def proposals(store: Store) -> list[dict[str, Any]]:
    """Every kept proposal, newest first."""
    found = []
    for key in store.kv_prefix(PROPOSAL_KEY, limit=200):
        record = load(store, key.removeprefix(PROPOSAL_KEY))
        if record is not None:
            found.append(record)
    return sorted(found, key=lambda r: str(r.get("created_at")), reverse=True)


def move(store: Store, proposal_id: str, *, expect: Iterable[str], to: str, now: datetime, detail: str = "",
         **fields: Any) -> bool:  # fmt: skip
    """Compare-and-set a proposal's state (and fields); False when it is not in `expect`."""
    if to not in STATES:
        raise ValueError(f"unknown proposal state {to}")
    with store.atomic():
        record = load(store, proposal_id)
        if record is None or record["state"] not in set(expect):
            return False
        record.update(fields, state=to, updated_at=now.isoformat(timespec="seconds"))
        if detail:
            record["detail"] = clean_text(detail, 300)
        store.kv_set(PROPOSAL_KEY + proposal_id, json.dumps(record), now=now)
    return True


def save(store: Store, data: Path, made: Build, *, reason: str, by: str, run_id: str | None,
         now: datetime) -> dict[str, Any]:  # fmt: skip
    """Record a proposal and its result's bytes; it supersedes any open proposal for the same fork."""
    proposal_id = secrets.token_hex(8)
    folder = _private_dir(_dir(data, proposal_id) / "new")
    _private_dir(folder.parent.parent)
    os.chmod(folder.parent, 0o700)
    for name, content in made.files.items():
        if not valid_file(name):
            raise RebaseRefused(f"not a file name: {name[:40]}")
        _write_private(folder / name, content)
    kept = [h for h in made.hunks if h.id in made.keep]
    record = {
        "id": proposal_id, "domain": made.domain, "core": made.core, "origin": made.origin, "keep": made.keep,
        "reason": clean_text(reason, 300), "by": by[:80], "run_id": run_id, "state": "open", "detail": "",
        "files": {n: {"old": sha256(made.old[n]), "new": sha256(made.files[n])} for n in sorted(made.files)},
        "unchanged": made.unchanged[:40], "result": made.result_hash, "fork": made.fork_hash,
        "hunks": [{"id": h.id, "added": h.counts[0], "removed": h.counts[1], "kept": h.id in made.keep}
                  for h in made.hunks][:80],
        "kept_text": fence("\n\n".join(h.text() for h in kept), 8000),
        "diff": diff_text(made.old, made.files, made.domain),
        "created_at": now.isoformat(timespec="seconds"), "updated_at": now.isoformat(timespec="seconds"),
    }  # fmt: skip
    with store.atomic():
        for old in proposals(store):
            if old["domain"] == made.domain and old["state"] == "open":
                move(
                    store, str(old["id"]), expect=("open",), to="stale", now=now, detail="a newer proposal replaced it"
                )
        store.kv_set(PROPOSAL_KEY + proposal_id, json.dumps(record), now=now)
    prune(store, data, now=now)
    return record


def prune(store: Store, data: Path, *, now: datetime) -> None:
    """Keep the newest `KEEP_PROPOSALS` proposals; a finished one's files go with it."""
    for old in proposals(store)[KEEP_PROPOSALS:]:
        if old["state"] in FINAL:
            store.kv_set(PROPOSAL_KEY + str(old["id"]), "", now=now)
            shutil.rmtree(_dir(data, str(old["id"])), ignore_errors=True)


def binding(record: Mapping[str, Any]) -> dict[str, Any]:
    """What an approval of this proposal is about (`approvals.fork_hash`)."""
    return {"proposal": record["id"], "domain": record["domain"], "core": record["core"], "origin": record["origin"],
            "keep": list(record["keep"]), "result": record["result"], "fork": record["fork"]}  # fmt: skip


def new_files(data: Path, record: Mapping[str, Any]) -> dict[str, bytes] | None:
    """The proposal's rebased bytes as stored, each checked against its recorded SHA-256; None if any differs."""
    folder = _dir(data, str(record["id"])) / "new"
    found: dict[str, bytes] = {}
    for name, sums in (record.get("files") or {}).items():
        content = _read_regular(folder / name) if valid_file(str(name)) else None
        if content is None or sha256(content) != (sums or {}).get("new"):
            return None
        found[str(name)] = content
    return found or None


def current_files(custom_components: Path, record: Mapping[str, Any]) -> dict[str, bytes] | None:
    """The fork's files the proposal replaces, as they are on disk now (`coresource.domain_fd`: never through a
    link, at any level); None if unreadable."""
    found: dict[str, bytes] = {}
    try:
        with domain_fd(custom_components, str(record["domain"])) as root:
            for name in record.get("files") or {}:
                content = read_at(root, str(name)) if valid_file(str(name)) else None
                if content is None:
                    return None
                found[str(name)] = content
    except CoreSourceError, OSError:
        return None
    return found


def binding_now(store: Store, data: Path, custom_components: Path, proposal_id: str,
                core_version: str | None) -> dict[str, Any] | None:  # fmt: skip
    """The binding recomputed from what is on disk and the live Core version; None when anything moved (the
    stored result, the fork's files, Core's version) or the proposal is not live."""
    record = load(store, proposal_id)
    if record is None or record["state"] not in ("open", "applying"):
        return None
    new = new_files(data, record)
    old = current_files(custom_components, record)
    if new is None or old is None or core_version != record["core"]:
        return None
    return binding(record) | {"result": files_hash(new), "fork": files_hash(old)}


# ------------------------------------------------------------------ writing the files


@dataclass(frozen=True, slots=True)
class Saved:
    mode: int
    uid: int
    gid: int
    sha: str


class ForkFiles:
    """Snapshot, write and restore one proposal's files in `custom_components/<domain>/`.

    Every access goes through a descriptor of the domain's directory reached from the config directory with
    `O_DIRECTORY | O_NOFOLLOW` at each level (`coresource.domain_fd`), so swapping `custom_components` or the
    domain's directory for a link never redirects a read or a write. Within it: regular files only (`stat` without
    following links), each written to a fresh randomly named temporary file (`O_CREAT | O_EXCL | O_NOFOLLOW`),
    written in full, given the old file's mode and owner, fsynced and renamed over the old one (both names relative
    to the same descriptor), then the directory fsynced; a failed write removes its temporary file."""

    def __init__(self, data: Path, custom_components: Path) -> None:
        self.data = data
        self.custom = custom_components

    @contextlib.contextmanager
    def _root(self, domain: str) -> Iterator[int]:
        try:
            with domain_fd(self.custom, domain) as fd:
                yield fd
        except CoreSourceError as err:
            raise RebaseRefused(str(err)) from err

    @staticmethod
    def _check_name(domain: str, name: str) -> None:
        path = f"custom_components/{domain}/{name}"
        if not valid_file(name) or name.startswith(".") or selfguard.self_path(path):
            raise RebaseRefused(f"{path[:80]} may not be written")

    def snapshot(self, record: Mapping[str, Any]) -> None:
        """Keep the files the proposal replaces (bytes, mode, owner) under its own directory, once."""
        folder = _dir(self.data, str(record["id"])) / "old"
        meta_path = folder / "meta.json"
        if meta_path.exists():
            return
        domain = str(record["domain"])
        _private_dir(folder)
        meta: dict[str, Any] = {}
        with self._root(domain) as root:
            for name in record["files"]:
                self._check_name(domain, str(name))
                info = os.stat(str(name), dir_fd=root, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise RebaseRefused(f"{name} is not a regular file")
                content = read_at(root, str(name))
                if content is None:
                    raise RebaseRefused(f"{name} could not be read")
                _write_private(folder / str(name), content)
                meta[str(name)] = {"mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
                                   "sha": sha256(content)}  # fmt: skip
        write_json_atomic(meta_path, meta, mode=0o600)

    def saved(self, record: Mapping[str, Any]) -> dict[str, tuple[bytes, Saved]] | None:
        """The snapshot, checked against its own SHA-256s; None when there is none or it does not match."""
        folder = _dir(self.data, str(record["id"])) / "old"
        try:
            meta = json.loads((folder / "meta.json").read_text())
        except OSError, ValueError:
            return None
        found: dict[str, tuple[bytes, Saved]] = {}
        for name, info in meta.items() if isinstance(meta, dict) else []:
            content = _read_regular(folder / name) if valid_file(str(name)) else None
            if content is None or not isinstance(info, dict) or sha256(content) != info.get("sha"):
                return None
            found[str(name)] = (content, Saved(int(info["mode"]), int(info["uid"]), int(info["gid"]), str(info["sha"])))
        return found if set(found) == set(record["files"]) else None

    def _write(self, root: int, domain: str, name: str, content: bytes, saved: Saved) -> None:
        self._check_name(domain, name)
        try:
            info = os.stat(name, dir_fd=root, follow_symlinks=False)
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            raise RebaseRefused(f"{name} is not a regular file (never written through a link)")
        tmp = f".ops-{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
        try:
            try:
                view = memoryview(content)
                while view:
                    view = view[os.write(fd, view) :]
                os.fchmod(fd, saved.mode & 0o777)
                if os.geteuid() == 0:
                    os.fchown(fd, saved.uid, saved.gid)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=root, dst_dir_fd=root)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=root)
            raise

    def _put(self, record: Mapping[str, Any], files: Mapping[str, tuple[bytes, Saved]]) -> None:
        domain = str(record["domain"])
        with self._root(domain) as root:
            for name in sorted(files):
                content, meta = files[name]
                self._write(root, domain, name, content, meta)
            os.fsync(root)

    def write(self, record: Mapping[str, Any], files: Mapping[str, bytes]) -> None:
        """Write the rebased files (after `snapshot`), keeping each old file's mode and owner."""
        saved = self.saved(record)
        if saved is None:
            raise RebaseRefused("the snapshot of the fork's files is missing or damaged; nothing was written")
        if set(files) - set(saved):
            raise RebaseRefused("the rebase names a file the snapshot does not hold; nothing was written")
        self._put(record, {name: (files[name], saved[name][1]) for name in files})

    def restore(self, record: Mapping[str, Any]) -> None:
        """Put the snapshot back (bytes, mode and owner)."""
        saved = self.saved(record)
        if saved is None:
            raise RebaseRefused("the snapshot of the fork's files is missing or damaged")
        self._put(record, saved)

    def restored(self, record: Mapping[str, Any]) -> bool:
        """The fork's files on disk are the snapshot again."""
        saved = self.saved(record)
        now = current_files(self.custom, record)
        return saved is not None and now is not None and all(sha256(now[n]) == s.sha for n, (_, s) in saved.items())

    def untouched(self, record: Mapping[str, Any]) -> bool:
        """The fork's files on disk are exactly the ones the proposal was built from (nothing was written)."""
        now = current_files(self.custom, record)
        files = record.get("files") or {}
        return (
            now is not None
            and set(now) == set(files)
            and all(sha256(now[n]) == (files[n] or {}).get("old") for n in files)
        )


# ------------------------------------------------------------------ the capability


def validate_propose(ctx: ExecContext, args: Args) -> Args:
    """Inside a run, for a custom integration on this install, with well-formed hunk ids (before any quota)."""
    if ctx.run is None:
        raise CapabilityError("propose-rebase is only available inside an agent run")
    domain = args.opt_text("domain") or ""
    if selfguard.self_path(f"custom_components/{domain}/") or selfguard.is_self_slug(domain):
        raise CapabilityError("the agent never changes itself")
    refusal = reload.never_touched(domain, ctx.options.profile.rebase.deny)
    if refusal is not None:
        raise CapabilityError(f"no rebase: {refusal}")
    try:
        custom_dir(ctx.paths.custom_components, domain)
        parse_keep(args.opt_text("keep") or "")
    except (CoreSourceError, RebaseRefused) as err:
        raise CapabilityError(str(err)) from err
    if not (args.opt_text("reason") or "").strip():
        raise CapabilityError("--reason is required: why the fork needs the rebase")
    store = ctx.env.store
    if ctx.run.profile == "maintenance_plan" and not (store is not None and authoritative_finding(store, domain)):
        # The agent's own plan proposes a rebase only for what Home Assistant vouches for (a repair or the release
        # notes), never because of a log line, which any integration can write. The admin can still ask in the chat.
        raise CapabilityError("the maintenance plan proposes a rebase only for a repair or a release-notes finding")
    return args


def authoritative_finding(store: Store, domain: str) -> bool:
    """A maintenance finding about `domain` from a signal Home Assistant vouches for (a repair, the release notes)."""
    return any(f.domain == domain and f.signal in maintenance.AUTHORITATIVE for f in maintenance.load(store))


def live_access_refusal(client: Any, domain: str, deny: Iterable[str]) -> str | None:
    """`reload.access_refusal` against Core now (fail closed: an unreadable answer refuses)."""

    async def check() -> str | None:
        async with LiveAPI(client) as api:
            return await reload.access_refusal(api, domain, deny)

    try:
        return asyncio.run(check())
    except (CapabilityError, ApiError, OSError, ValueError, RuntimeError) as err:
        return f"could not check {domain} against the access-hardware rules ({str(err)[:80]})"


def exec_propose(ctx: ExecContext, args: Args) -> str:
    """Build the rebase and record it as a proposal for the admin's approval (nothing is written to the fork)."""
    store = ctx.env.store
    if store is None:
        raise CapabilityError("the store is unavailable")
    domain = args.text("domain")
    refusal = live_access_refusal(ctx.client, domain, ctx.options.profile.rebase.deny)
    if refusal is not None:
        raise CapabilityError(f"no rebase: {refusal}")
    installed = installed_core_version(ctx)
    source = CoreSource(ctx.paths.data, store=store)
    try:
        made = build(source, ctx.paths.custom_components, domain, installed, parse_keep(args.text("keep")))
    except (CoreSourceError, RebaseRefused) as err:
        raise CapabilityError(f"no rebase proposed: {err}") from err
    finally:
        source.prune(installed)
    if made.files == made.old:
        raise CapabilityError(f"no rebase proposed: {domain} already matches Core {installed} plus the kept hunks")
    run = ctx.run
    record = save(store, ctx.paths.data, made, reason=args.text("reason"),
                  by=run.principal.label() if run else "daemon", run_id=run.run_id if run else None,
                  now=datetime.now(UTC))  # fmt: skip
    kept = ", ".join(made.keep) or "none"
    files = "; ".join(f"{n} ({sha256(made.old[n])[:8]} → {sha256(made.files[n])[:8]})" for n in sorted(made.files))
    return (f"Proposed rebase {record['id']}: {domain} onto Home Assistant Core {installed}. The fork matches Core"
            f" {made.origin} best; kept hunks: {kept}. Files rebased: {files}."
            + (f" Left as they are: {', '.join(made.unchanged)}." if made.unchanged else "")
            + " Nothing is written until the admin approves it on its item (Rebase onto Core) on Home; it is then"
            " applied in the update window with a backup, a health check and automatic rollback.")  # fmt: skip


def fork_report(source: CoreSource, custom: LocalFiles, domain: str, installed: str, *,
                budget: int = MAX_DIFF_BYTES) -> str:  # fmt: skip
    """`core-compare <domain> --fork`: the fork's origin and its own changes as numbered hunks, fenced."""
    origin = find_origin(source, domain, installed, custom.files)
    hunks = fork_hunks(origin.texts, custom.files)
    lines = [f"Fork origin: Home Assistant Core {origin.tag} (the newest release whose files differ least from the"
             f" fork: {origin.score} changed lines; searched the installed {installed} and the .0 releases of the"
             f" last {FORK_MONTHS} months).",
             f"The fork's own changes against Core {origin.tag}, numbered for `opsctl propose-rebase {domain} --keep"
             " <ids>` (" + listed([f"{h.id} +{h.counts[0]} -{h.counts[1]}" for h in hunks], "hunks")
             + "):"]  # fmt: skip
    head = "\n".join(lines)
    room = max(512, budget - len(head.encode()) - 40)
    body = cut_text("\n\n".join(h.text() for h in hunks), room, "hunks", "the list above names every hunk")
    return "\n".join([head, "<fork-hunks>", fence(body, len(body) + 1), "</fork-hunks>"])


# ------------------------------------------------------------------ is the fork still needed


@dataclass(frozen=True, slots=True)
class ForkState:
    """Whether a fork still carries changes its installed Core version lacks, and a fingerprint of that check."""

    needed: bool
    summary: str
    fingerprint: str


def fork_fingerprint(custom: LocalFiles, installed: str) -> str | None:
    """The installed Core minor and the fork's file hashes: what a "still needed" answer holds for. None when the
    installed version has no minor."""
    at = maintenance.minor(installed)
    if at is None:
        return None
    return f"{at[0]}.{at[1]}:{files_hash({n: t.encode() for n, t in custom.files.items()})}"


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    size = len(needle)
    return size > 0 and any(
        haystack[i] == needle[0] and list(haystack[i : i + size]) == list(needle)
        for i in range(len(haystack) - size + 1)
    )


def _carried(hunk: Hunk, core: str | None) -> bool:
    """True when the fork's change in `hunk` is not in Core's text of the file: an added line block Core lacks, or
    a removed block Core still has."""
    if core is None:
        return True
    lines = [line.strip() for line in core.splitlines()]
    matcher = difflib.SequenceMatcher(None, hunk.old, hunk.new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        added = [line.strip() for line in hunk.new[j1:j2]]
        if added:
            if not _contains(lines, added):
                return True
        elif _contains(lines, [line.strip() for line in hunk.old[i1:i2]]):
            return True
    return False


def _manifest_differs(core: str, fork: str) -> bool:
    try:
        a, b = json.loads(core), json.loads(fork)
    except ValueError:
        return core.strip() != fork.strip()
    if not isinstance(a, dict) or not isinstance(b, dict):
        return bool(a != b)
    return {k: v for k, v in a.items() if k != "version"} != {k: v for k, v in b.items() if k != "version"}


def _similarity(origin: Mapping[str, str], fork: Mapping[str, str]) -> float:
    """The share of the fork's Python lines that the origin release has in the same place (1.0: no `.py`)."""
    total = matched = 0
    for name, text in fork.items():
        if not name.endswith(".py"):
            continue
        b = text.splitlines()
        total += len(b)
        if name in origin:
            blocks = difflib.SequenceMatcher(None, origin[name].splitlines(), b, autojunk=False).get_matching_blocks()
            matched += sum(block.size for block in blocks)
    return matched / total if total else 1.0


def fork_state(source: CoreSource, custom_components: Path, domain: str, installed: str) -> ForkState:
    """What `core-compare --fork` shows, decided by the daemon: the fork's own changes (hunks against its origin
    release) that Core's installed version lacks. RebaseRefused or CoreSourceError when it cannot be told."""
    fork = read_custom(custom_components, domain)
    print_ = fork_fingerprint(fork, installed)
    if print_ is None:
        raise RebaseRefused(f"Core version {installed[:30]!r} has no minor")
    listing = source.listing(installed, domain)
    if listing.missing:
        return ForkState(False, "", print_)
    core = {name: source.text(listing, name) for name in sorted(set(fork.files) & set(listing.files))}
    origin = find_origin(source, domain, installed, fork.files)
    if _similarity(origin.texts, fork.files) < MIN_SIMILARITY:
        raise RebaseRefused("the fork is too far from every Core release searched to tell its own changes")
    counts: dict[str, int] = {}
    for hunk in fork_hunks(origin.texts, fork.files):
        # A custom integration's manifest always differs by its `version`, which is no change of the fork's own.
        if hunk.file != "manifest.json" and _carried(hunk, core.get(hunk.file)):
            counts[hunk.file] = counts.get(hunk.file, 0) + 1
    if "manifest.json" in core and _manifest_differs(core["manifest.json"], fork.files["manifest.json"]):
        counts["manifest.json"] = 1
    # Only code counts as the fork's own: a README, hacs.json or translations are no change to the integration.
    only_fork = {n for n in set(fork.files) - set(listing.files) if n.endswith(".py")}
    if not counts and not only_fork:
        return ForkState(False, "", print_)
    labels = {name: f"{name}: {n} change{'s' if n != 1 else ''}" for name, n in counts.items()}
    labels |= {name: f"{name}: new file" for name in only_fork}
    shown = [labels[name] for name in sorted(labels)[:3]]
    counts = dict.fromkeys(labels, 1)
    more = len(counts) - len(shown)
    summary = "; ".join(shown) + (f"; and {more} more file{'s' if more != 1 else ''}" if more > 0 else "")
    return ForkState(True, clean_text(summary, 120), print_)


def _stale(detail: Any) -> re.Match[str] | None:
    return STALE_PUSH_RE.fullmatch(str(detail or ""))


def stale_pushes(store: Store) -> bool:
    """Whether any applied rebase or config change still says its commit is "not pushed yet" (store reads only)."""
    if any(r.get("state") == "applied" and _stale(r.get("detail")) for r in proposals(store)):
        return True
    return any(_stale(r.get("detail")) for r in store.changes(states=("applied", "reverted"), limit=50))


def settle_pushes(store: Store, repo: Any, *, now: datetime) -> int:
    """Applied rebases and config changes whose commit was left "not pushed yet": once `origin/main` holds the
    commit (a later push or fetch carried it), the record says it is pushed. Local git reads only; returns how many
    were updated."""
    settled = 0
    try:
        for record in proposals(store):
            found = _stale(record.get("detail"))
            if record.get("state") != "applied" or found is None:
                continue
            if repo.is_ancestor(found.group("sha"), "refs/remotes/origin/main") and move(
                store, str(record["id"]), expect=("applied",), to="applied", now=now,
                detail=f"{found.group('head')}committed {found.group('sha')}, pushed{found.group('tail')}",
            ):  # fmt: skip
                settled += 1
        for row in store.changes(states=("applied", "reverted"), limit=50):
            found = _stale(row.get("detail"))
            sha = row.get("applied_sha" if row["state"] == "applied" else "reverted_sha")
            if found is None or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
                continue
            if repo.is_ancestor(sha, "refs/remotes/origin/main") and store.move_change(
                int(row["id"]), expect=(row["state"],), to=row["state"], now=now,
                detail=clean_text(f"{found.group('head')}{found.group('verb')} {found.group('sha')}, pushed"
                                  f"{found.group('tail')}", 300),
            ):  # fmt: skip
                settled += 1
    except Exception:  # noqa: BLE001 - a git or store failure leaves the status as it was
        _LOGGER.info("push status not checked")
    return settled


def exec_internal(_ctx: ExecContext, _args: Args) -> Any:
    raise CapabilityError("fork.apply is a daemon executor and cannot be requested")


def proposal_summary(record: Mapping[str, Any]) -> str:
    kept = ", ".join(record.get("keep") or []) or "none"
    return (
        f"{record['domain']} onto Core {record['core']} (fork origin {record['origin']}); kept hunks: {kept};"
        f" files: {', '.join(sorted(record.get('files') or {}))}"
    )


def valid_record(record: Mapping[str, Any]) -> bool:
    return (valid_domain(str(record.get("domain"))) and valid_tag(str(record.get("core")))
            and valid_tag(str(record.get("origin"))))  # fmt: skip


def for_run(store: Store, run_id: str) -> list[dict[str, Any]]:
    """The proposals a run made, for its chat card (the daemon's record, never model text)."""
    found = []
    for record in proposals(store):
        if record.get("run_id") == run_id and valid_record(record):
            kept = sum(1 for h in record.get("hunks") or [] if isinstance(h, dict) and h.get("kept"))
            found.append({"id": record["id"], "domain": record["domain"], "core": record["core"], "kept": kept})
    return found[:3]
