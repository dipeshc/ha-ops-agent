"""Dashboards: what the storage dashboards use (read-only), and the one way the agent may change one.

Reading: `lovelace/dashboards/list`, then `lovelace/config` per dashboard (both reads in the self-guard).
For each dashboard the inventory keeps its mode and the card types it uses; YAML-mode dashboards live in
files the agent never writes.

Writing is a migration plan the admin approves (`opsd.maintenance` makes it from the plan run's
suggestion; `approvals.request_dashboard` binds it). The edit is deterministic and narrow: on every card
of one type, rename option keys and optionally change the card type (`Edit`). Nothing else in the JSON
moves. `DashboardWriter.execute`, in this order:

1. the approval is a dashboard approval, approved and unexpired; `dashboard.save` is not tightened to
   `never` by the options; act mode; the dashboard is not the default one, not malformed and not in
   the profile's `dashboards.generated` (`selfguard.dashboard_unwritable`);
2. it reads the dashboard again: the binding recomputed from it (its SHA-256 before, the edit applied,
   the SHA-256 after) must still be the approved one, else nothing is written (the plan is stale);
3. it writes the current JSON to `<data>/maintenance/dashboards/<url_path>/` (0600, root only) and
   reads the file back (no verified backup, no write);
4. it consumes the approval (single use) and saves the new JSON inside
   `selfguard.approved_dashboard_write(url_path)`, which lets exactly that one save through;
5. it reads the dashboard back: anything but the planned JSON (or any error) restores the backup the
   same way, and a restore that does not read back as before is escalated to the admin.
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsd import selfguard
from opsd.approvals import Approvals
from opsd.capabilities import CAPABILITIES, Args, CapabilityError
from opsd.config import Options
from opsd.conversation import clean_text
from opsd.policy import Origin, PolicyTier, decide
from opsd.runctx import ExecContext
from opsd.store import Store, StoreError

_LOGGER = logging.getLogger(__name__)

SAVE_CAPABILITY = "dashboard.save"
# Container keys whose mappings are cards (not views, badges or picture elements).
CARD_KEYS = frozenset({"cards", "card"})
CARD_TYPE_RE = re.compile(r"(?:custom:)?[a-z0-9][a-z0-9_-]{0,79}")
OPTION_RE = re.compile(r"[a-z_][a-z0-9_]{0,40}")
MAX_RENAMES = 8
MAX_DASHBOARDS = 20
MAX_BACKUPS = 10
MAX_PATHS = 40

Ws = Callable[..., Awaitable[Any]]


def digest(config: Any) -> str:
    """SHA-256 of a dashboard's JSON (canonical: sorted keys, compact)."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _walk(node: Any, container: str | None = None) -> Iterator[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(node, Mapping):
        yield container, node
        for key, child in node.items():
            yield from _walk(child, str(key))
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child, container)


def card_types(config: Any) -> dict[str, int]:
    """How many cards of each type a dashboard config holds (nested stacks included)."""
    found: Counter[str] = Counter()
    for container, node in _walk(config):
        kind = node.get("type")
        if container in CARD_KEYS and isinstance(kind, str) and CARD_TYPE_RE.fullmatch(kind):
            found[kind] += 1
    return dict(found)


@dataclass(frozen=True, slots=True)
class Edit:
    """On every card of `card_type`: rename option keys, and optionally change the type to `new_type`."""

    card_type: str
    new_type: str | None = None
    renames: tuple[tuple[str, str], ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {"card_type": self.card_type, "new_type": self.new_type, "renames": [list(r) for r in self.renames]}

    def words(self) -> str:
        parts = [f"{old} → {new}" for old, new in self.renames]
        if self.new_type:
            parts.insert(0, f"type {self.card_type} → {self.new_type}")
        return f"on every {self.card_type} card: " + ", ".join(parts)


def parse_edit(raw: Any) -> Edit | None:
    """A strictly shaped edit (from the plan run's answer, untrusted); None for anything else."""
    if not isinstance(raw, Mapping):
        return None
    card, new = raw.get("card_type"), raw.get("new_type")
    renames_raw = raw.get("renames") or []
    if not isinstance(card, str) or not CARD_TYPE_RE.fullmatch(card):
        return None
    if new is not None and (not isinstance(new, str) or not CARD_TYPE_RE.fullmatch(new) or new == card):
        return None
    if not isinstance(renames_raw, list) or len(renames_raw) > MAX_RENAMES:
        return None
    renames: list[tuple[str, str]] = []
    for entry in renames_raw:
        old_key = entry.get("old") if isinstance(entry, Mapping) else None
        new_key = entry.get("new") if isinstance(entry, Mapping) else None
        if not isinstance(old_key, str) or not isinstance(new_key, str):
            return None
        if not OPTION_RE.fullmatch(old_key) or not OPTION_RE.fullmatch(new_key) or old_key == new_key:
            return None
        # The card's type is not an option: changing it is `new_type`.
        if "type" in (old_key, new_key) or old_key in dict(renames):
            return None
        renames.append((old_key, new_key))
    if not renames and new is None:
        return None
    return Edit(card, new, tuple(renames))


def apply_edit(config: Any, edit: Edit) -> tuple[Any, list[str]]:
    """The config with the edit applied (key order and values kept) and the paths it changed. A card that already
    has a key's new name keeps both as they are: nothing is guessed."""
    changed: list[str] = []

    def walk(node: Any, path: str, container: str | None) -> Any:
        if isinstance(node, Mapping):
            is_card = container in CARD_KEYS and node.get("type") == edit.card_type
            renames = dict(edit.renames) if is_card else {}
            out: dict[str, Any] = {}
            for key, child in node.items():
                name = str(key)
                target = renames.get(name)
                if target is not None and target not in node:
                    out[target] = child
                    changed.append(f"{path}.{name} → {target}")
                elif is_card and name == "type" and edit.new_type:
                    out[name] = edit.new_type
                    changed.append(f"{path}.type → {edit.new_type}")
                else:
                    out[name] = walk(child, f"{path}.{name}" if path else name, name)
            return out
        if isinstance(node, list):
            return [walk(child, f"{path}[{index}]", container) for index, child in enumerate(node)]
        return node

    return walk(config, "", None), changed


def binding(finding: str, url_path: str, config: Any, edit: Edit) -> dict[str, Any] | None:
    """What a dashboard plan's approval binds to, computed from the dashboard as it is; None when the edit changes
    nothing in it."""
    new, changed = apply_edit(config, edit)
    if not changed:
        return None
    return {"finding": finding, "url_path": url_path, "before": digest(config), "after": digest(new),
            "edit": edit.to_json(), "changes": len(changed),
            "paths": [clean_text(p, 160) for p in changed[:MAX_PATHS]]}  # fmt: skip


def edit_of(bound: Mapping[str, Any]) -> Edit | None:
    raw = bound.get("edit")
    if not isinstance(raw, Mapping):
        return None
    renames = [{"old": r[0], "new": r[1]} for r in raw.get("renames") or [] if isinstance(r, list) and len(r) == 2]
    return parse_edit({"card_type": raw.get("card_type"), "new_type": raw.get("new_type"), "renames": renames})


def exec_internal(_ctx: ExecContext, _args: Args) -> str:
    """`dashboard.save` is daemon-only; the socket never reaches it."""
    raise CapabilityError("only the daemon saves a dashboard, after the admin approved its plan")


async def read_all(ws: Ws) -> tuple[dict[str, dict[str, Any]], dict[str, Any], list[str]]:
    """(dashboards by url_path with their mode, title and card types; storage configs by url_path; problems),
    read-only. The default dashboard is "" here; one Home Assistant generates (no stored config) is left out."""
    found: dict[str, dict[str, Any]] = {}
    configs: dict[str, Any] = {}
    problems: list[str] = []
    try:
        listed = await ws("lovelace/dashboards/list")
    except Exception as err:  # noqa: BLE001 - a missing source is a problem, never a crash
        return found, configs, [f"dashboards unreadable: {clean_text(str(err), 120)}"]
    boards: list[Mapping[str, Any]] = [{"url_path": "", "title": "Overview", "mode": "storage"}]
    boards += [b for b in listed if isinstance(b, Mapping)][:MAX_DASHBOARDS] if isinstance(listed, list) else []
    for board in boards:
        url_path = board.get("url_path")
        if not isinstance(url_path, str) or (url_path and not selfguard.DASHBOARD_PATH_RE.fullmatch(url_path)):
            continue
        mode = "yaml" if board.get("mode") == "yaml" else "storage"
        try:
            config = await ws("lovelace/config", url_path=url_path) if url_path else await ws("lovelace/config")
        except Exception as err:  # noqa: BLE001 - "no config" is normal for a generated default dashboard
            if url_path:
                problems.append(f"dashboard {url_path}: {clean_text(str(err), 120)}")
            continue
        if not isinstance(config, Mapping):
            continue
        found[url_path] = {"mode": mode, "title": clean_text(str(board.get("title") or url_path), 60),
                           "cards": card_types(config)}  # fmt: skip
        if mode == "storage":
            configs[url_path] = config
    return found, configs, problems


# ------------------------------------------------------------------ the writer


@dataclass(frozen=True, slots=True)
class Outcome:
    state: str  # applied | refused | stale | restored | escalated
    detail: str


class DashboardWriter:
    """Executes one approved dashboard plan (see the module docstring)."""

    def __init__(self, *, store: Store, options: Options, approvals: Approvals, ws: Ws, backup_dir: Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:  # fmt: skip
        self.store = store
        self.options = options
        self.approvals = approvals
        # A websocket command (`await ws(type, **fields)`); every call passes `selfguard.check_ws`.
        self._ws = ws
        self.backup_dir = backup_dir
        self._clock = clock

    @property
    def generated(self) -> frozenset[str]:
        return self.options.profile.dashboards.generated

    def _audit(self, outcome: str, bound: Mapping[str, Any], detail: str = "") -> None:
        try:
            self.store.audit(at=self._clock(), run_id=None, principal="daemon", channel="maintenance",
                             capability=SAVE_CAPABILITY, args={"url_path": bound.get("url_path"),
                                                               "finding": bound.get("finding")},
                             tier="approve", decision=outcome, outcome=outcome, detail=detail[:300])  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    def allowed(self) -> str | None:
        """Why `dashboard.save` cannot run even with an approval (tightened to never, observe mode), or None."""
        cap = CAPABILITIES[SAVE_CAPABILITY]
        tighten = {n: PolicyTier.APPROVE for n in self.options.capabilities.require_approval}
        tighten.update({n: PolicyTier.NEVER for n in self.options.capabilities.never})
        verdict = decide(capability=cap.name, default_tier=cap.tier, effects=cap.effects, origin=Origin.AGENT_SELF,
                         allowed_origins=cap.origins, acting=self.options.acting, tighten=tighten)  # fmt: skip
        if verdict.tier is not PolicyTier.APPROVE:
            return f"{cap.name} is not allowed ({'; '.join(verdict.reasons)})"
        if not self.options.acting:
            return "observe mode: dashboards change only in agent.mode: act"
        return None

    async def read(self, url_path: str) -> Any:
        return await self._ws("lovelace/config", url_path=url_path)

    async def binding_now(self, bound: Mapping[str, Any]) -> tuple[dict[str, Any] | None, Any]:
        """(the binding recomputed from the dashboard as it is now, its config); (None, None) when unreadable."""
        url_path, edit = str(bound.get("url_path") or ""), edit_of(bound)
        if edit is None or selfguard.dashboard_unwritable(url_path, self.generated) is not None:
            return None, None
        try:
            current = await self.read(url_path)
        except Exception as err:  # noqa: BLE001 - nothing written yet
            _LOGGER.info("dashboard unreadable", extra={"url_path": url_path, "error": str(err)[:160]})
            return None, None
        if not isinstance(current, Mapping):
            return None, None
        return binding(str(bound.get("finding")), url_path, current, edit), current

    def _backup(self, url_path: str, config: Any, before: str) -> Path:
        folder = self.backup_dir / url_path
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkdir's mode is subject to the umask and skipped for an existing folder.
        for directory in (self.backup_dir, folder):
            os.chmod(directory, 0o700)
        stamp = self._clock().strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"{stamp}-{before[:12]}.json"
        data = json.dumps(config, ensure_ascii=False, indent=1).encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if digest(json.loads(path.read_bytes())) != before:
            raise OSError("the backup does not read back as the dashboard")
        for old in sorted(folder.glob("*.json"))[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
        return path

    async def _save(self, url_path: str, config: Any) -> None:
        with selfguard.approved_dashboard_write(url_path, generated=self.generated):
            await self._ws(selfguard.LOVELACE_SAVE, url_path=url_path, config=config)

    async def execute(self, approval_id: str) -> Outcome:
        """Run one approved plan (the module docstring's steps)."""
        found = self.store.approval(approval_id)
        bound = dict(((found or {}).get("plan") or {}).get("binding") or {})
        url_path = str(bound.get("url_path") or "")
        if found is None or found.get("state") != "approved" or (found.get("plan") or {}).get("kind") != "dashboard":
            return Outcome("refused", "no approved dashboard plan")
        problem = self.allowed() or selfguard.dashboard_unwritable(url_path, self.generated)
        if problem is not None:
            self._audit("refused", bound, problem)
            return Outcome("refused", problem)
        now_bound, current = await self.binding_now(bound)
        if now_bound is None or not self.approvals.dashboard_bound(found, now_bound):
            # Voided (single use) so a stale plan is never retried; the next scan makes a new one.
            self.approvals.consume_dashboard(approval_id, now_bound)
            self._audit("stale", bound, "the dashboard changed since the plan was made")
            return Outcome("stale", "the dashboard changed since the plan was made; nothing was written")
        edit = edit_of(bound)
        if edit is None:
            return Outcome("refused", "the plan's edit is not valid")
        new, changed = apply_edit(current, edit)
        try:
            backup = self._backup(url_path, current, str(bound["before"]))
        except OSError as err:
            self._audit("refused", bound, f"backup failed: {err}")
            return Outcome("refused", f"the backup could not be written ({clean_text(str(err), 120)}): nothing changed")
        if not self.approvals.consume_dashboard(approval_id, now_bound):
            self._audit("refused", bound, "approval not valid for this plan")
            return Outcome("refused", "the approval is no longer valid for this plan; nothing changed")
        self._audit("approved", bound, f"backup {backup.name}; {len(changed)} change(s)")
        try:
            await self._save(url_path, new)
            if digest(await self.read(url_path)) != bound["after"]:
                raise RuntimeError("the saved dashboard does not read back as planned")
        except Exception as err:  # noqa: BLE001 - any failure restores the backup
            return await self._restore(url_path, current, bound, clean_text(str(err), 160), backup)
        self._audit("applied", bound, f"{len(changed)} change(s); backup {backup.name}")
        return Outcome("applied", f"{len(changed)} change(s) saved; the previous version is kept as {backup.name}")

    async def _restore(self, url_path: str, old: Any, bound: Mapping[str, Any], why: str, backup: Path) -> Outcome:
        try:
            await self._save(url_path, old)
            if digest(await self.read(url_path)) != bound["before"]:
                raise RuntimeError("the restored dashboard does not read back as it was")
        except Exception as err:  # noqa: BLE001 - escalated with the backup's name
            detail = (f"the save failed ({why}) and restoring the backup failed too ({clean_text(str(err), 120)}): "
                      f"restore {backup.name} by hand (raw configuration editor)")  # fmt: skip
            self._audit("escalated", bound, detail)
            return Outcome("escalated", detail)
        self._audit("restored", bound, why)
        return Outcome("restored", f"the save failed ({why}); the dashboard was restored from {backup.name}")
