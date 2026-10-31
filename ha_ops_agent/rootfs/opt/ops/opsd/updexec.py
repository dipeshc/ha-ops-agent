"""Update executors: backups, installs and rollbacks on HA's own mechanisms.

These are the daemon's own powers. Their registry rows are `internal`, so
they are never on the socket, the CLI, the guard's verb list or a prompt:
nothing Claude produces can call them. Every call still goes through
`policy.decide` (code tier, the update rule as tier resolver, the
`agent-self` origin, `observe` mode, option tightening) and the audit log,
and refuses on anything but "execute".

Native mechanisms, by target (API shapes checked read-only against a live
install where noted):

- Backup: Core's backup manager, `backup/generate_with_automatic_settings`
  (ws). It uses the admin's automatic backup settings, including the
  encryption key, so the agent never handles the key; the result (from
  ws `backup/info`) must include Home Assistant, its database and every
  installed add-on, and be encrypted on the local agent (`hassio.local`).
  Its retention is Core's own (the automatic backup settings).
- Core: install with Supervisor `POST /core/update {"version", "backup":
  false}`; roll back with the same call and the previous version, but only
  when the recorder schema did not change (read from the database file,
  read-only). Otherwise, or when it cannot be read, the rollback is
  escalated to the admin (restore the backup, which needs the key).
- Add-on: install with Supervisor `POST /store/addons/<slug>/update
  {"backup": false}`. Restoring an add-on from an encrypted backup needs the
  key, so an add-on regression is escalated to the admin, never rolled back
  by the agent. (The version the Supervisor reports after a restore may lag
  the entity briefly; the agent never restores, so it accepts that.)
- HACS: install with Core `update.install {"entity_id", "version"}` (HACS
  entities support specific versions: `supported_features` 23); roll back
  with `update.install` and the previous version. An integration then
  needs a Core restart (`/core/check` first).
- HAOS (approval only): install with Supervisor `POST /os/update
  {"version"}`. Since Supervisor 2026.07.0 (supervisor#6982, "Make
  /os/update stop auto-rebooting after OS update") that call only installs
  the bundle into the other RAUC slot: it logs "Install of Home Assistant
  Operating System <v> success; reboot required", sets `version_pending`
  in `/os/info` (supervisor#7006) and raises the `reboot_required` issue.
  The host keeps running the old version until it reboots. The admin's
  approval covers that reboot, so the daemon then calls `POST /host/reboot
  {}` itself, once, in the same run and window (never `force`). Both calls
  pass `selfguard` only inside `approved_os_flow`. The OS is never rolled
  back by the agent: a boot back into the old slot (RAUC fallback) is
  escalated.
"""

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from opsd import selfguard
from opsd.api import CORE_WS_URL, LiveAPI
from opsd.capabilities import CAPABILITIES, Capability
from opsd.config import Options
from opsd.policy import (
    SELF_UPDATE_ENTITY,
    Decision,
    Origin,
    PolicyTier,
    Resolution,
    UpdateFeature,
    decide,
    resolve_update,
    same_version,
)
from opsd.store import Store, StoreError, UpdateJob
from opsd.supervisor import ApiError, SupervisorClient
from opsd.updiff import HealthPoint, probe
from opsd.ws import HAWebSocket

_LOGGER = logging.getLogger(__name__)

LONG_CALL_S = 1800.0
REBOOT_CALL_S = 120.0
# Supervisor log lines read to find the OS manager's and host control's own lines.
OS_LOG_SCAN_LINES = 1500
# A websocket ping must come back this fast for Core to count as responsive.
PING_MAX_S = 5.0
SERVICE_CALL_S = 900.0
EXECUTABLE_KINDS = frozenset({"core", "addon", "hacs", "os", "firmware"})
# Kinds that can only ever run with an approval (never AUTO, whatever the plan says).
APPROVAL_ONLY_KINDS = frozenset({"os", "firmware"})
# Kinds the agent never rolls back (a regression is escalated).
NO_ROLLBACK_KINDS = frozenset({"os", "firmware"})
OS_ENTITY = "update.home_assistant_operating_system_update"
SLUG_RE = re.compile(r"[a-z0-9_]{1,64}")
BACKUP_SLUG_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
ENTITY_RE = re.compile(r"update\.[a-z0-9_]{1,128}")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
OS_LOG_LOGGERS = ("[supervisor.os.", "[supervisor.host.", "[supervisor.resolution.module]")
OS_LOG_LINE_CHARS = 300


def os_log_lines(text: str, limit: int) -> list[str]:
    """Lines from the OS manager, host control and resolution issues (reboot, install, slots), newest last."""
    found = []
    for raw in text.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not any(tag in line for tag in OS_LOG_LOGGERS):
            continue
        if "[supervisor.resolution.module]" in line and "reboot" not in line.lower():
            continue
        found.append(line[:OS_LOG_LINE_CHARS])
    return found[-limit:] if limit > 0 else []


class ExecutorRefused(RuntimeError):
    """Policy, the hard limits or the audit log refused the call; nothing was changed."""


class UpdateOps(Protocol):
    """Every Supervisor/Core call the update pipeline makes; the live and fake implementations match it."""

    async def jobs(self) -> Any: ...
    async def backup_manager(self) -> dict[str, Any]: ...
    async def create_backup(self) -> str | None: ...
    async def core_backups(self) -> list[dict[str, Any]]: ...
    async def installed_addons(self) -> list[str]: ...
    async def recorder_schema(self) -> int | None: ...
    async def core_info(self) -> dict[str, Any]: ...
    async def core_alive(self) -> bool: ...
    async def addon_info(self, slug: str) -> dict[str, Any]: ...
    async def entity_state(self, entity_id: str) -> dict[str, Any] | None: ...
    async def update_core(self, version: str) -> None: ...
    async def update_addon(self, slug: str) -> None: ...
    async def install_entity(self, entity_id: str, version: str | None) -> None: ...
    async def update_os(self, version: str) -> None: ...
    async def os_info(self) -> dict[str, Any]: ...
    async def host_info(self) -> dict[str, Any]: ...
    async def reboot_host(self) -> None: ...
    async def resolution(self) -> dict[str, Any]: ...
    async def os_log(self, limit: int) -> list[str]: ...
    async def core_check(self) -> None: ...
    async def core_restart(self) -> None: ...
    async def release_notes(self, entity_id: str) -> str | None: ...
    async def probe(self, custom_domains: list[str]) -> HealthPoint: ...


class LiveUpdateOps:
    """`UpdateOps` over the Supervisor API and the Core REST/websocket proxy."""

    def __init__(self, client: SupervisorClient, recorder_db: Path | None = None,
                 watched: Sequence[str] = ()) -> None:  # fmt: skip
        self._client = client
        self._recorder_db = recorder_db
        # The house profile's `updates.watched_entities` (must stay available across an update).
        self._watched = tuple(watched)

    async def _sup(self, path: str, method: str = "GET", body: Any | None = None, limit_s: float | None = None) -> Any:
        return await asyncio.to_thread(self._client.supervisor, path, method, body, timeout=limit_s)

    async def _ws(self, msg_type: str, **fields: Any) -> Any:
        async with LiveAPI(self._client) as api:
            return await api.ws(msg_type, **fields)

    async def jobs(self) -> Any:
        return await self._sup("/jobs/info")

    async def backup_manager(self) -> dict[str, Any]:
        """Core's backup manager: `state` and `next_automatic_backup` (ws `backup/info`).

        Only these keys leave this method. The agent never reads `backup/config/info`, which
        carries the backup encryption key.
        """
        info = await self._ws("backup/info")
        if not isinstance(info, dict):
            raise ValueError("unexpected backup/info result")
        return {k: info.get(k) for k in ("state", "next_automatic_backup", "last_completed_automatic_backup")}

    async def create_backup(self) -> str | None:
        """Start a backup with the admin's automatic settings; returns Core's backup job id."""
        result = await self._ws("backup/generate_with_automatic_settings")
        job = result.get("backup_job_id") if isinstance(result, dict) else None
        return str(job) if job else None

    async def core_backups(self) -> list[dict[str, Any]]:
        """Backups as Core's backup manager lists them (ws `backup/info`), only the fields checked."""
        info = await self._ws("backup/info")
        found = info.get("backups") if isinstance(info, dict) else None
        keys = ("backup_id", "name", "date", "with_automatic_settings", "homeassistant_included",
                "homeassistant_version", "database_included", "addons", "folders", "agents")  # fmt: skip
        return [{k: b.get(k) for k in keys} for b in found if isinstance(b, dict)] if isinstance(found, list) else []

    async def installed_addons(self) -> list[str]:
        data = await self._sup("/addons")
        found = data.get("addons") if isinstance(data, dict) else None
        return (
            [str(a["slug"]) for a in found if isinstance(a, dict) and a.get("slug")] if isinstance(found, list) else []
        )

    async def recorder_schema(self) -> int | None:
        """The recorder's schema version from its SQLite file (read-only); None if not readable."""
        path = self._recorder_db
        if path is None:
            return None

        def read() -> int | None:
            if not path.exists():
                return None
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
            try:
                row = conn.execute(
                    "SELECT schema_version FROM schema_changes ORDER BY change_id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            return int(row[0]) if row and isinstance(row[0], int) else None

        try:
            return await asyncio.to_thread(read)
        except (sqlite3.Error, OSError) as err:
            _LOGGER.warning("recorder schema unreadable", extra={"error": str(err)[:200]})
            return None

    async def core_info(self) -> dict[str, Any]:
        data = await self._sup("/core/info")
        return data if isinstance(data, dict) else {}

    async def core_alive(self) -> bool:
        """Core is up and responsive, not merely accepting connections.

        `/core/info` carries no state on this Supervisor, so: the REST API answers, `/api/config`
        answers with a version, and a websocket `ping` comes back as `pong` within
        `PING_MAX_S` (a heartbeat through Core's event loop, which a hung Core fails).
        """
        if not await asyncio.to_thread(self._client.core_alive):
            return False
        try:
            config = await asyncio.to_thread(self._client.core, "/config")
        except ApiError:
            return False
        if not isinstance(config, dict) or not config.get("version"):
            return False
        return await self._ws_ping()

    async def _ws_ping(self) -> bool:
        ws = HAWebSocket(CORE_WS_URL, self._client.token, timeout=PING_MAX_S)
        try:
            async with asyncio.timeout(PING_MAX_S):
                await ws.connect()
                msg_id = await ws.send_command("ping")
                while True:
                    reply = await ws.recv_json()
                    if reply.get("id") == msg_id and reply.get("type") == "pong":
                        return True
        except (TimeoutError, OSError, ValueError, RuntimeError) as err:
            _LOGGER.info("core heartbeat failed", extra={"error": str(err)[:120]})
            return False
        finally:
            with contextlib.suppress(Exception):
                await ws.close()

    async def addon_info(self, slug: str) -> dict[str, Any]:
        data = await self._sup(f"/addons/{slug}/info")
        return data if isinstance(data, dict) else {}

    async def entity_state(self, entity_id: str) -> dict[str, Any] | None:
        try:
            data = await asyncio.to_thread(self._client.core, f"/states/{entity_id}")
        except ApiError as err:
            if err.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    async def update_core(self, version: str) -> None:
        await self._sup("/core/update", "POST", {"version": version, "backup": False}, limit_s=LONG_CALL_S)

    async def update_addon(self, slug: str) -> None:
        await self._sup(f"/store/addons/{slug}/update", "POST", {"backup": False}, limit_s=LONG_CALL_S)

    async def install_entity(self, entity_id: str, version: str | None) -> None:
        body = {"entity_id": entity_id} | ({"version": version} if version is not None else {})
        await asyncio.to_thread(self._client.core, "/services/update/install", "POST", body, timeout=SERVICE_CALL_S)

    async def update_os(self, version: str) -> None:
        """HAOS: installs into the other slot and returns; activating it needs `reboot_host` (supervisor#6982)."""
        await self._sup(selfguard.OS_UPDATE_PATH, "POST", {"version": version}, limit_s=LONG_CALL_S)

    async def os_info(self) -> dict[str, Any]:
        """`version` (running), `version_pending` (installed, waits for a reboot), `boot` and `boot_slots`."""
        data = await self._sup("/os/info")
        return data if isinstance(data, dict) else {}

    async def host_info(self) -> dict[str, Any]:
        data = await self._sup("/host/info")
        return {"boot_timestamp": data.get("boot_timestamp")} if isinstance(data, dict) else {}

    async def reboot_host(self) -> None:
        """The Supervisor stops Core and the add-ons (this one too), then reboots; the reply may never come."""
        await self._sup(selfguard.HOST_REBOOT_PATH, "POST", {}, limit_s=REBOOT_CALL_S)

    async def resolution(self) -> dict[str, Any]:
        """Supervisor resolution issues and suggestions (type, context, reference only)."""
        data = await self._sup("/resolution/info")
        if not isinstance(data, dict):
            return {}
        keys = ("type", "context", "reference")
        return {
            name: [{k: i.get(k) for k in keys} for i in data.get(name) or [] if isinstance(i, dict)]
            for name in ("issues", "suggestions")
        }

    async def os_log(self, limit: int) -> list[str]:
        """The Supervisor's OS-manager and host-control log lines (this boot's journal only), newest last."""
        text = await asyncio.to_thread(self._client.logs, "/supervisor/logs", OS_LOG_SCAN_LINES)
        return os_log_lines(text, limit)

    async def core_check(self) -> None:
        await self._sup("/core/check", "POST", {}, limit_s=300.0)

    async def core_restart(self) -> None:
        await self._sup("/core/restart", "POST", {}, limit_s=LONG_CALL_S)

    async def release_notes(self, entity_id: str) -> str | None:
        notes = await self._ws("update/release_notes", entity_id=entity_id)
        return notes if isinstance(notes, str) else None

    async def probe(self, custom_domains: list[str]) -> HealthPoint:
        async with LiveAPI(self._client) as api:
            return await probe(api, custom_domains, watched=self._watched)


def outcome_unknown(err: ApiError) -> bool:
    """A timeout or dropped connection: the Supervisor may still be working on it."""
    return err.status is None


def resolve_install(decision: Decision) -> Resolution:
    """`update.install`'s tier resolver: the update rule for this update, as planned and re-checked."""
    return resolve_update(decision)


@dataclass(frozen=True, slots=True)
class BackupRecord:
    name: str
    slug: str
    homeassistant: str
    addons: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "slug": self.slug, "homeassistant": self.homeassistant,
                "addons": list(self.addons), "encrypted": True, "automatic": True, "verified": True}  # fmt: skip


LOCAL_AGENT = "hassio.local"


def verify_backup(record: Mapping[str, Any], *, addons: Iterable[str]) -> BackupRecord:
    """A pre-update backup must be encrypted on this machine and hold Home Assistant, its database and every add-on."""
    if record.get("with_automatic_settings") is not True:
        raise ExecutorRefused("backup was not made with the automatic backup settings")
    if record.get("homeassistant_included") is not True:
        raise ExecutorRefused("backup does not include Home Assistant")
    if record.get("database_included") is not True:
        raise ExecutorRefused("backup excludes the Home Assistant database")
    agents = record.get("agents")
    local = agents.get(LOCAL_AGENT) if isinstance(agents, dict) else None
    if not isinstance(local, dict):
        raise ExecutorRefused("backup is not stored on this machine")
    if local.get("protected") is not True:
        raise ExecutorRefused("backup is not encrypted")
    raw = record.get("addons")
    included = {str(a.get("slug")) for a in raw if isinstance(a, dict)} if isinstance(raw, list) else set()
    missing = sorted(set(addons) - included)
    if missing:
        raise ExecutorRefused(f"backup is missing add-ons: {', '.join(missing[:5])}")
    slug = str(record.get("backup_id") or "")
    if not BACKUP_SLUG_RE.fullmatch(slug):
        raise ExecutorRefused("unexpected backup id")
    name = str(record.get("name") or "")[:80]
    version = str(record.get("homeassistant_version") or "")
    return BackupRecord(name=name, slug=slug, homeassistant=version, addons=tuple(sorted(included)))


class Executors:
    """Authorised, audited calls into `UpdateOps`. Refusals raise `ExecutorRefused`."""

    def __init__(
        self,
        *,
        ops: UpdateOps,
        store: Store | None,
        options: Options,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        registry: Mapping[str, Capability] = CAPABILITIES,
        approval_proof: Callable[[str | None, UpdateJob], bool] | None = None,
    ) -> None:
        self.ops = ops
        # Whether an approval record proves this job may run at the APPROVE tier.
        self._approval_proof = approval_proof or (lambda _a, _j: False)
        self._store = store
        self._options = options
        self._clock = clock
        self._registry = registry

    def _authorize(
        self,
        name: str,
        args: dict[str, Any],
        resolution: Resolution | None = None,
        *,
        approved: bool = False,
        gate_updates: bool = True,
        admin: bool = False,
    ) -> int:
        """Policy, then audit. `approved`: a verified approval lifts exactly the APPROVE tier, nothing stricter.

        `gate_updates=False`: not part of an update (a standalone backup), so the update switch does
        not apply. `admin`: the admin asked in the panel, so observe mode does not hold it back.
        """
        cap = self._registry.get(name)
        if cap is None or not cap.enabled or not cap.internal:
            raise ExecutorRefused(f"{name} is not a daemon executor")
        # The admin's switch, checked here too (the pipeline checks it first). A rollback
        # only returns to the version that was running, so it is not gated by it.
        if gate_updates and name != "update.rollback" and not self._options.auto_updates_on:
            raise ExecutorRefused("automatic updates are off (add-on option updates.auto_install)")
        tighten = {n: PolicyTier.APPROVE for n in self._options.capabilities.require_approval}
        tighten.update({n: PolicyTier.NEVER for n in self._options.capabilities.never})
        verdict = decide(
            capability=cap.name,
            default_tier=cap.tier,
            effects=cap.effects,
            origin=Origin.AGENT_SELF,
            allowed_origins=cap.origins,
            acting=self._options.acting or admin,
            resolution=resolution,
            tighten=tighten,
        )
        store = self._store
        if store is None:
            raise ExecutorRefused("audit log unavailable")
        try:
            audit_id = store.audit_begin(
                at=self._clock(), run_id=None, principal="daemon", channel="updates", capability=cap.name, args=args,
                tier=str(verdict.tier), decision=verdict.outcome, notify_admin=verdict.notify_admin,
            )  # fmt: skip
        except (StoreError, sqlite3.Error, OSError) as err:
            raise ExecutorRefused("audit log unavailable") from err
        if verdict.execute:
            return audit_id
        if approved and verdict.outcome == "needs_approval" and verdict.tier is PolicyTier.APPROVE:
            self.finish(audit_id, "approved", "approval verified")
            return store.audit_begin(
                at=self._clock(), run_id=None, principal="daemon", channel="updates", capability=cap.name, args=args,
                tier=str(verdict.tier), decision="approved", notify_admin=True,
            )  # fmt: skip
        self.finish(audit_id, "refused", "; ".join(verdict.reasons))
        raise ExecutorRefused(f"{cap.name}: {verdict.outcome} ({'; '.join(verdict.reasons)})"[:300])

    def finish(self, audit_id: int, outcome: str, detail: str = "") -> None:
        if self._store is None:
            return
        try:
            self._store.audit_finish(audit_id, outcome, detail[:300])
        except (StoreError, sqlite3.Error, OSError) as err:
            _LOGGER.error("audit update failed", extra={"error": str(err)[:200], "audit_id": audit_id})

    @staticmethod
    def _check_job(job: UpdateJob) -> None:
        slug = job.target.get("slug")
        try:
            # The agent never updates itself or the Supervisor (opsd.selfguard).
            selfguard.check_update(job.entity_id, job.kind, slug if isinstance(slug, str) else None, job.title)
        except selfguard.SelfTargetError as err:
            raise ExecutorRefused(str(err)[:200]) from err
        if job.kind not in EXECUTABLE_KINDS:
            raise ExecutorRefused(f"{job.kind} updates are never executed by the agent")
        if not ENTITY_RE.fullmatch(job.entity_id) or job.entity_id == SELF_UPDATE_ENTITY:
            raise ExecutorRefused("not an installable update entity")
        for version in (job.from_version, job.to_version):
            if not VERSION_RE.fullmatch(version):
                raise ExecutorRefused("bad version string")
        if job.kind == "addon":
            slug = job.target.get("slug")
            if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug) or selfguard.is_self_slug(slug):
                raise ExecutorRefused("bad or forbidden add-on slug")
        if job.kind == "os" and job.entity_id != OS_ENTITY:
            raise ExecutorRefused("not the operating system update")

    async def _call(self, audit_id: int, call: Any) -> str:
        """`done`, or `unknown` when the call timed out or the connection dropped. Other errors refuse."""
        try:
            await call
        except ApiError as err:
            if outcome_unknown(err):
                self.finish(audit_id, "unknown", str(err)[:300])
                return "unknown"
            self.finish(audit_id, "error", str(err)[:300])
            raise ExecutorRefused(f"refused by Home Assistant: {str(err)[:200]}") from err
        except (OSError, ValueError, RuntimeError) as err:
            self.finish(audit_id, "error", str(err)[:300])
            raise ExecutorRefused(f"call failed: {str(err)[:200]}") from err
        self.finish(audit_id, "ok")
        return "done"

    # ------------------------------------------------------------ backup

    async def backup(self, *, standalone: bool = False, admin: bool = False) -> tuple[str, str | None]:
        """Start a backup with the admin's automatic settings: (`done` | `unknown`, Core's job id).

        `standalone`: "Back up now" or the self-heal, not a pre-update backup (see `_authorize`).
        """
        args = {"settings": "automatic", "by": "admin" if admin else "daemon"}
        audit_id = self._authorize("backup.create", args, gate_updates=not standalone, admin=admin)
        job: str | None = None

        async def go() -> None:
            nonlocal job
            job = await self.ops.create_backup()

        outcome = await self._call(audit_id, go())
        return outcome, job

    # ----------------------------------------------------------- install

    def _approved(self, job: UpdateJob) -> bool:
        approval = job.target.get("approval")
        return self._approval_proof(approval if isinstance(approval, str) else None, job)

    async def install(self, job: UpdateJob, decision: Decision) -> str:
        """Install exactly the planned version. `done` or `unknown` (then the caller polls)."""
        self._check_job(job)
        approved = self._approved(job)
        if job.kind in APPROVAL_ONLY_KINDS and not approved:
            raise ExecutorRefused(f"{job.kind} updates need an approval")
        args = {"entity": job.entity_id, "kind": job.kind, "from": job.from_version, "to": job.to_version}
        if approved:
            args["approval"] = str(job.target.get("approval"))
        audit_id = self._authorize("update.install", args, resolve_install(decision), approved=approved)
        match job.kind:
            case "core":
                return await self._call(audit_id, self.ops.update_core(job.to_version))
            case "addon":
                return await self._call(audit_id, self.ops.update_addon(str(job.target["slug"])))
            case "os":
                with selfguard.approved_os_flow(selfguard.OS_UPDATE_PATH):
                    return await self._call(audit_id, self.ops.update_os(job.to_version))
            case "firmware":
                features = job.target.get("supported_features")
                specific = isinstance(features, int) and bool(features & UpdateFeature.SPECIFIC_VERSION)
                version = job.to_version if specific else None
                return await self._call(audit_id, self.ops.install_entity(job.entity_id, version))
            case _:
                return await self._call(audit_id, self.ops.install_entity(job.entity_id, job.to_version))

    async def restart_core(self, job: UpdateJob, decision: Decision, *, purpose: str) -> str:
        """Load a HACS integration (after install or rollback): config check, then a Core restart."""
        self._check_job(job)
        args = {"entity": job.entity_id, "step": "restart", "purpose": purpose[:40]}
        install = purpose == "install"
        audit_id = self._authorize("update.install" if install else "update.rollback", args,
                                   resolve_install(decision) if install else None,
                                   approved=install and self._approved(job))  # fmt: skip
        try:
            await self.ops.core_check()
        except ApiError as err:
            self.finish(audit_id, "refused", f"config check failed: {str(err)[:200]}")
            raise ExecutorRefused(f"config check failed: {str(err)[:200]}") from err
        return await self._call(audit_id, self.ops.core_restart())

    async def reboot_for_os(self, job: UpdateJob, decision: Decision) -> str:
        """Activate an approved, installed HAOS update: one host reboot. `done` or `unknown` (the host went down).

        Refuses unless this OS job consumed a verified approval and the Supervisor reports exactly its
        version as installed and pending (`version_pending`) while the old one still runs.
        """
        self._check_job(job)
        if job.kind != "os":
            raise ExecutorRefused("only an operating system update reboots the host")
        approved = self._approved(job)
        if not approved:
            raise ExecutorRefused("the reboot needs the approval of this operating system update")
        try:
            info = await self.ops.os_info()
        except (ApiError, OSError, ValueError) as err:
            raise ExecutorRefused(f"OS state unreadable: {str(err)[:150]}") from err
        if not same_version(info.get("version_pending"), job.to_version):
            pending = info.get("version_pending")
            raise ExecutorRefused(f"{job.to_version} is not installed and pending (pending: {pending})")
        if not same_version(info.get("version"), job.from_version):
            raise ExecutorRefused(f"the host does not run {job.from_version} (it runs {info.get('version')})")
        args = {"entity": job.entity_id, "kind": "os", "step": "reboot", "from": job.from_version,
                "to": job.to_version, "approval": str(job.target.get("approval")) if approved else "auto"}  # fmt: skip
        audit_id = self._authorize("update.install", args, resolve_install(decision), approved=approved)
        with selfguard.approved_os_flow(selfguard.HOST_REBOOT_PATH):
            return await self._call(audit_id, self.ops.reboot_host())

    # ---------------------------------------------------------- rollback

    async def rollback(self, job: UpdateJob) -> str:
        """Return to `from_version` by a version pin (Core, HACS). Add-ons are never restored by the agent."""
        self._check_job(job)
        if job.kind == "addon":
            raise ExecutorRefused("an add-on rollback needs the backup's encryption key: escalated to the admin")
        if job.kind in NO_ROLLBACK_KINDS:
            raise ExecutorRefused(f"{job.kind} updates cannot be rolled back by the agent: escalated to the admin")
        args = {"entity": job.entity_id, "kind": job.kind, "to": job.from_version}
        audit_id = self._authorize("update.rollback", args)
        if job.kind == "core":
            return await self._call(audit_id, self.ops.update_core(job.from_version))
        return await self._call(audit_id, self.ops.install_entity(job.entity_id, job.from_version))


# The registry rows' executor references (daemon-internal entry points).


async def exec_backup(executors: Executors) -> tuple[str, str | None]:
    return await executors.backup()


async def exec_install(executors: Executors, job: UpdateJob, decision: Decision) -> str:
    return await executors.install(job, decision)


async def exec_rollback(executors: Executors, job: UpdateJob) -> str:
    return await executors.rollback(job)
