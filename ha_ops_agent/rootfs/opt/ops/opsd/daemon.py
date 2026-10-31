"""opsd: wiring of the skeleton, the health-check handler and the liveness loop.

Control flow: event sources (the store-driven task scheduler, the
panel, Core recovering, verified taps from the admin's phone) put `Event`s
on one queue; the dispatcher hands them to handlers. The daily health check
is the `health.check` system task; its handler collects, diffs, runs the
`triage` profile through the agent runner when something changed, applies
the run's effects, and sends the deterministic safety push. The liveness
loop stays independent of the queue and the store: Core-down alerts are
pushed directly from it.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opsd import __version__, dashboards, inventory, maintenance, privsep, proposals, quiet, updatefix
from opsd.activity import ActivityLog
from opsd.aicontrol import AiControl
from opsd.api import CORE_WS_URL, LiveAPI, core_release_age_days, github_release_age_days
from opsd.approvals import ApprovalError, Approvals
from opsd.assistant import Assistant
from opsd.backups import Backups
from opsd.baseline import (
    DAEMON_PUSHED_KINDS,
    Baseline,
    Item,
    advance,
    apply_ack,
    diff,
    honoured_expected,
    seed,
    seed_exclusions,
    vacuum_rooms_seen,
    waits_quietly,
)
from opsd.budget import Period
from opsd.changepaths import SUPPRESS_PATH
from opsd.changes import ChangeManager, ChangeRefused, LiveFiles, registry_entities
from opsd.channels import BudgetClass, CompanionPush, Message
from opsd.claude_runner import RunResult
from opsd.config import Options, Paths, claude_token, credential_kind
from opsd.conversation import clean_text, context_block
from opsd.events import Dispatcher, Event, EventQueue, Priority, Schedule, TaskScheduler
from opsd.evidence import EvidenceCache, EvidenceSource
from opsd.gitops import Repo
from opsd.health import Snapshot, collect, custom_domains
from opsd.inbound import EVENT_TYPE, AdminIdentity, InboundGate, admin_identity, is_active_admin
from opsd.items import Context, ItemBook
from opsd.jsonstore import append_jsonl, read_json, read_jsonl, write_json_atomic
from opsd.live import Hub
from opsd.lock import LockBusyError, run_lock
from opsd.notify import Notifier, own_slug, set_panel_slug
from opsd.panel import PanelServer
from opsd.panelapi import PanelHandlers
from opsd.privsep import ops_account
from opsd.profile import PROFILE_PATH
from opsd.redact import Redactor, build_redactor
from opsd.rpc import RpcServer
from opsd.runctx import AGENT_SELF, ExecEnv, RunEffects
from opsd.runner import (
    GUARD_ANOMALY_DAYS,
    GUARD_ANOMALY_KEY,
    MAINTENANCE_PLAN,
    MAX_CLAUDE_RUNS_PER_DAY,
    TRIAGE,
    UPDATE_REVIEW,
    AgentRunner,
)
from opsd.selfguard import SELF_SLUG
from opsd.selftest import SelfTest, run_selftest
from opsd.state import AgentState
from opsd.store import Store, StoreError, StoreUnavailable, Task
from opsd.supervisor import ApiError, SupervisorClient, supervisor_jobs_busy
from opsd.triage import TriageResult, build_prompt, validate_result
from opsd.updates import UpdateManager
from opsd.updexec import Executors, LiveUpdateOps
from opsd.usage import UsageGuard
from opsd.ws import EventStream, HAWebSocket

if TYPE_CHECKING:
    from opsd.house import suppress

_LOGGER = logging.getLogger(__name__)

__all__ = ["MAX_CLAUDE_RUNS_PER_DAY", "Daemon", "supervisor_jobs_busy"]

LIVENESS_INTERVAL_S = 60
# s6 sends SIGKILL a few seconds after SIGTERM: the pushes in flight get this long.
SHUTDOWN_PUSH_S = 2.5
# Health items are redacted like pushes; the redactor (secrets.yaml) is rebuilt at most this often for them.
ITEMS_REDACTOR_S = 60.0
CORE_DOWN_ALERT_AFTER = timedelta(minutes=5)
CORE_DOWN_ALERT_AFTER_BUSY = timedelta(minutes=20)
CORE_DOWN_ALERT_AFTER_JOBS = timedelta(minutes=45)
STATUS_REFRESH = timedelta(minutes=10)
MAX_MANUAL_TRIGGERS_PER_DAY = 6
KEEP_SNAPSHOTS = 30
STATUS_ENTITY = "sensor.ops_agent"
# `Options.runtime` key: the notify device's user is not an active HA admin (notifications go to the admin only).
NOTIFY_NOT_ADMIN = "notify.not_admin"
NEEDS_TOP_CHARS = 80
# Kinds whose titles can carry a proposer's or the model's words (a commit subject, a reminder title): the
# dashboard pill shows a fixed label for them instead.
NEEDS_LABELS = {"proposal": "Config change waiting", "task": "Reminder"}


def needs_label(record: Mapping[str, Any]) -> str | None:
    """The pill's text for the top Needs you item: its title only where the daemon wrote it."""
    key = str(record.get("key") or "")
    kind = "task" if key.startswith("task:") else str(record.get("kind") or "")
    if kind in NEEDS_LABELS or key.startswith(("change:", "fact:")):
        return NEEDS_LABELS.get(kind, NEEDS_LABELS["proposal"])
    return clean_text(str(record.get("title") or ""), NEEDS_TOP_CHARS) or None


PANEL_PORT = 8099
HEALTH_TASK = "health.check"
STATUS_TASK = "status.refresh"
UPDATE_TASK = "update.window"
# If the scheduler or store ever stops firing the daily check, the liveness
# loop requests one itself once the last check is this old.
HEALTH_OVERDUE = timedelta(hours=26)
OVERDUE_RETRY = timedelta(hours=6)
OVERDUE_GRACE = timedelta(hours=2)
ADMIN_REFRESH_KNOWN_S = 24 * 3600
ADMIN_REFRESH_UNKNOWN_S = 3600


@dataclass(frozen=True, slots=True)
class CycleOutcome:
    result: str
    items: int
    record: dict[str, Any]


def next_run_at(now: datetime, hhmm: str, tz: ZoneInfo) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":"))
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), time(hour, minute), tz)
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), time(hour, minute), tz)
    return candidate.astimezone(UTC)


def in_window(now: datetime, hhmm: str, tz: ZoneInfo, hours: float = 1.5) -> bool:
    hour, minute = (int(x) for x in hhmm.split(":"))
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), time(hour, minute), tz)
    return any(abs((local - (start + timedelta(days=d))).total_seconds()) <= hours * 3600 for d in (-1, 0, 1))


def core_down_threshold(core_state: str, *, jobs_busy: bool, in_update_window: bool) -> timedelta:
    """How long Core may be unreachable before an alert (longer while it is expected)."""
    if jobs_busy:
        return CORE_DOWN_ALERT_AFTER_JOBS
    if core_state in ("startup", "starting") or in_update_window:
        return CORE_DOWN_ALERT_AFTER_BUSY
    return CORE_DOWN_ALERT_AFTER


# A daemon push that neither went out nor was held for after quiet hours.
UNDELIVERED = frozenset({"rate_limited", "failed", "suppressed"})


def _unsent(baseline: Baseline, items: list[Item], outcome: str | None) -> Baseline:
    """A drift whose push was not delivered is reported again at the next check."""
    if outcome not in UNDELIVERED:
        return baseline
    for item in items:
        baseline.vacuum_rooms_seen.pop(str(item.detail.get("entity_id")), None)
    return baseline


def fallback_push(items: list[Item], now: datetime | None = None) -> Message | None:
    """A deterministic summary when the AI could not triage (or skipped a critical item)."""
    at = now or datetime.now(UTC)
    notable = [i for i in items if i.severity in ("critical", "warning") and not waits_quietly(i, at)]
    if not notable:
        return None
    critical = [i for i in notable if i.severity == "critical"]
    head = critical or notable
    lines = [f"- {i.summary}" for i in head[:5]]
    if len(notable) > len(lines):
        lines.append(f"- and {len(notable) - len(lines)} more (see the Ops Agent panel)")
    return Message(
        title="Ops Agent: needs attention" if not critical else "Ops Agent: critical",
        body="\n".join(lines),
        urgent=bool(critical),
        budget_class=BudgetClass.SAFETY if critical else BudgetClass.AGENT,
        tag="ha-ops-fallback",
    )


def baseline_summary(snapshot: Snapshot, expected_unavailable: frozenset[str] = frozenset()) -> tuple[str, bool]:
    """Seed push text, and whether something critical was found (and not accepted)."""
    platforms: dict[str, int] = {}
    for info in snapshot.unavailable.values():
        platforms[info["platform"]] = platforms.get(info["platform"], 0) + 1
    top = ", ".join(f"{p} {n}" for p, n in sorted(platforms.items(), key=lambda kv: -kv[1])[:5])
    text = (
        f"Baseline recorded: {len(snapshot.unavailable)} unavailable entities across {len(platforms)} "
        f"integrations (top: {top}); {len(snapshot.failed_entries)} failed entries; "
        f"{len(snapshot.repairs)} repairs; {len(snapshot.updates)} updates pending. "
        "Future checks alert only on changes."
    )
    excluded = seed_exclusions(snapshot, expected_unavailable)
    problems = []
    if excluded["custom_broken"]:
        problems.append(f"custom integrations broken now: {', '.join(excluded['custom_broken'])}")
    if excluded["custom_unavailable"]:
        problems.append(f"{len(excluded['custom_unavailable'])} custom-integration entities unavailable")
    if excluded["custom_failed_entries"]:
        failed = [str(snapshot.failed_entries[e].get("domain")) for e in excluded["custom_failed_entries"]]
        problems.append(f"failed custom config entries: {', '.join(failed)}")
    if excluded["critical_repairs"]:
        problems.append(f"critical repairs: {', '.join(excluded['critical_repairs'])}")
    if excluded["assist_exposed"]:
        problems.append(f"exposed to Assist: {', '.join(excluded['assist_exposed'])}")
    if problems:
        text += " NOT accepted, will be re-reported: " + "; ".join(problems) + "."
    return text, bool(problems)


class Daemon:
    def __init__(self, paths: Paths, options: Options, client: SupervisorClient) -> None:
        self.paths = paths
        self.options = options
        self.client = client
        # The profile's slug when it names one, else the slug this add-on was installed under.
        slug = options.profile.self_slug
        set_panel_slug(slug if slug != SELF_SLUG else own_slug(SELF_SLUG))
        self.state = AgentState.load(paths.state_file)
        self.store: Store | None = None
        self.store_error: str | None = None
        try:
            self.store, recovered = Store.open_or_recover(paths.store_file)
        except StoreUnavailable as err:
            # Degraded: deterministic checks and safety pushes go on; the AI, quotas,
            # audit-dependent capabilities and inbound events are off until fixed.
            self.store_error = str(err)[:300]
            _LOGGER.critical("store unavailable; running degraded", extra={"error": self.store_error})
            recovered = False
        if self.store is not None and recovered:
            self.store.audit(
                at=datetime.now(UTC), run_id=None, principal="daemon", channel="internal",
                capability="store.recovered", args={}, tier=None, decision="recovered", outcome="recovered",
                detail="corrupt store moved aside and recreated",
            )  # fmt: skip
        self.notifier = Notifier(
            state_file=paths.notify_state_file,
            push_cache_file=paths.push_cache_file,
            notify_service=options.notify_service,
            max_per_day=options.notify.max_pushes_per_day,
            call_service=client.call_service,
            blocked=lambda: self.options.runtime.get(NOTIFY_NOT_ADMIN),
        )
        self.channel = CompanionPush(self.notifier, self.store)
        # Git proposals only when the house profile enables git (no repository, no deploy key otherwise).
        self.repo: Repo | None = None
        if options.git_on:
            self.repo = Repo(paths.ha_config, paths.ssh_key, paths.known_hosts,
                             author=(options.profile.git.author_name, options.profile.git.author_email))  # fmt: skip
        self.tz = ZoneInfo("UTC")
        self._cycle_running = asyncio.Lock()
        self.hub = Hub()
        self._started_at = datetime.now(UTC)
        self._last_overdue_request: datetime | None = None
        # The AI only ever runs as the unprivileged ops user; tests on a
        # developer machine turn this off.
        self.require_privsep = True
        self.selftest: SelfTest | None = None
        self.rpc: RpcServer | None = None
        self.queue = EventQueue()
        self.scheduler = (
            TaskScheduler(self.store, self.queue, next_fire=self._next_fire, to_event=self._task_event)
            if self.store is not None
            else None
        )
        self.dispatcher = Dispatcher(
            self.queue,
            {
                "health.requested": self._on_health_requested,
                "status.refresh": self._on_status_refresh,
                "schedule.fired": self._on_schedule_fired,
                "user.message": self._on_user_message,
                "user.action": self._on_user_action,
                "chat.retry": self._on_chat_retry,
                "updates.window": self._on_update_window,
                "updates.resume": self._on_updates_resume,
                "updates.approved": self._on_updates_approved,
                "changes.execute": self._on_change_approved,
                "maintenance.dashboard": self._on_dashboard_approved,
            },
        )
        self.inbound = InboundGate(store=self.store, queue=self.queue)
        self.assistant: Assistant | None = None
        self.control = AiControl(self.store)
        self.runner = AgentRunner(
            paths=paths,
            options=options,
            store=self.store,
            token=lambda: claude_token(self.options, self.paths),
            period=lambda now: Period.at(now, self.tz),
            selftest=lambda: self.selftest,
            rpc=lambda: self.rpc,
            require_privsep=lambda: self.require_privsep,
            control=self.control,
        )
        self.usage: UsageGuard | None = None
        if self.store is not None:
            self.usage = UsageGuard(
                store=self.store,
                control=self.control,
                options=options,
                client=client,
                tz=lambda: self.tz,
                send=self.send_now,
                kill=self.runner.kill,
                admin_user=lambda: self.inbound.admin.user_id if self.inbound.admin is not None else None,
                api_key=lambda: self.runner.api_key,
            )
            self.runner._caps = self.usage.caps
            self.runner._after_run = self.usage.after_run
        self.updates: UpdateManager | None = None
        # The pending updates the last health check saw (the plan is re-made when they differ).
        self._updates_seen: list[dict[str, Any]] | None = None
        # Proactive maintenance (opsd.maintenance), set up below once the store is known to work.
        self.maintenance: maintenance.MaintenanceManager | None = None
        self.dashboards: dashboards.DashboardWriter | None = None
        self.evidence: EvidenceSource | None = None
        if self.store is not None:
            self.evidence = EvidenceSource(cache=EvidenceCache(self.store, clock=lambda: datetime.now(UTC)))
        if self.store is not None:
            approvals = Approvals(self.store)
            self.updates = UpdateManager(
                store=self.store,
                approvals=approvals,
                executors=Executors(
                    ops=LiveUpdateOps(
                        client,
                        recorder_db=paths.ha_config / "home-assistant_v2.db",
                        watched=options.profile.updates.watched_entities,
                    ),
                    store=self.store,
                    options=options,
                    approval_proof=approvals.proves,
                ),
                options=options,
                snapshot=self._update_snapshot,
                custom_domains=lambda: custom_domains(self.paths.custom_components),
                send=self.send_now,
                review=self._update_review,
                redactor=self.redactor,
                tz=lambda: self.tz,
                hub=self.hub,
                evidence=self.evidence,
                house_domains=self._house_domains,
                propose_fix=self._propose_fix,
                removals=lambda: self.maintenance.removals() if self.maintenance is not None else [],
                before_plan=self._maintenance_before_plan,
            )
        # "Back up now" and the act-mode self-heal (same ops and audited executor as the updates).
        self.backups: Backups | None = None
        if self.store is not None and self.updates is not None:
            self.backups = Backups(ops=self.updates.ops, executors=self.updates.executors, store=self.store,
                                   publish=lambda: self.hub.publish("updates", None, {}))  # fmt: skip
        self._backup_task: asyncio.Task[None] | None = None
        quiet_problem = quiet.problem(options.notify.quiet_start, options.notify.quiet_end)
        if quiet_problem is not None:
            _LOGGER.warning("quiet hours disabled", extra={"reason": quiet_problem})
        # Every git write (applied changes) holds this lock.
        self.git_lock = asyncio.Lock()
        self.changes: ChangeManager | None = None
        if self.store is not None and self.repo is not None:
            self.changes = ChangeManager(
                store=self.store,
                repo=self.repo,
                approvals=Approvals(self.store),
                options=options,
                live=LiveFiles(paths.ha_config),
                check_config=self._check_config,
                reload=self._reload,
                send=self.send_now,
                publish=self._changes_changed,
                lock=self.git_lock,
            )
        if self.store is not None:
            self.assistant = Assistant(
                store=self.store,
                runner=self.runner,
                send=self.send_now,
                redactor=self.redactor,
                tz=lambda: self.tz,
                day=self.day,
                hub=self.hub,
                context=self.chat_context,
                wake=self.scheduler.wake if self.scheduler is not None else None,
            )
        # One chat plus an activity log; entries that need the admin post in the chat.
        self.activity: ActivityLog | None = None
        if self.store is not None and self.assistant is not None:
            self.activity = ActivityLog(
                self.store, self.hub, post=self.assistant.post_activity, context_days=options.chat.activity_days
            )
            self.assistant.activity = self.activity
            if self.updates is not None:
                self.updates.activity = self.activity.record
            if self.backups is not None:
                self.backups.activity = self.activity.record
            if self.changes is not None:
                self.changes.activity = self.activity.record
        if self.store is not None:
            self.maintenance = maintenance.MaintenanceManager(
                store=self.store, gather=self._maintenance_gather, evidence=self.evidence, git=options.git_on,
                generated=options.profile.dashboards.generated, plan=self._maintenance_plan,
                propose=self._propose_maintenance if self.repo is not None else None,
                publish=lambda: self.hub.publish("maintenance", None, {}),
            )  # fmt: skip
            self.dashboards = dashboards.DashboardWriter(
                store=self.store, options=options, approvals=Approvals(self.store), ws=self._ws_command,
                backup_dir=paths.data / "maintenance" / "dashboards",
            )  # fmt: skip
        # One record per item. Every surface reads it; each write of a record it shows writes it too.
        self.items: ItemBook | None = None
        # The loop the item pushes are sent on (an item can be written from an executor thread).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._item_pushes: set[asyncio.Task[Any]] = set()
        # Entries of health.expected_unavailable that open the house, already warned about (never silenced).
        self._expected_refused: set[str] = set()
        # What `sensor.ops_agent` last showed of Needs you (count, top title); see `_needs_changed`.
        self._needs_published: tuple[int, str | None] | None = None
        self._items_redactor: tuple[float, Redactor] | None = None
        if self.store is not None:
            self.items = ItemBook(
                self.store, self.hub, tz=lambda: self.tz, context=self.items_context, send=self.send_now,
            )  # fmt: skip
            self.items.on_change = self._items_changed

    def items_context(self) -> Context:
        """What the items are derived from besides the store: the update gate, the window, the open health items."""
        raw = read_json(self.paths.open_items_file, {})
        updates = self.updates
        cached = self._items_redactor
        clock = datetime.now(UTC).timestamp()
        if cached is None or clock - cached[0] > ITEMS_REDACTOR_S:
            cached = (clock, self.redactor())
            self._items_redactor = cached
        return Context(
            window=self.options.updates.window,
            gate=updates.gate() if updates is not None else "updates unavailable",
            paused=updates.paused() if updates is not None else False,
            health_at=raw.get("at") if isinstance(raw, dict) else None,
            health_items=[i for i in (raw.get("items") or []) if isinstance(i, dict)] if isinstance(raw, dict) else [],
            redact=cached[1].redact,
            acting=self.options.acting,
        )

    def _items_changed(self) -> None:
        """An item was written (from any thread): publish it and decide its pushes, on the daemon's loop."""
        loop = self._loop
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._items_flushed)
            return
        self._items_flushed()

    def _items_flushed(self) -> None:
        if self.items is None:
            return
        self.items.flush()
        self._items_sent()

    def items_now(self) -> None:
        """The minute tick: recompose what asked to be, decide the pushes time made due, send them."""
        if self.items is None:
            return
        self.items.tick()
        self._items_sent()

    def _items_sent(self) -> None:
        """Refresh the sensor when Needs you changed, and send the decided pushes (in the background)."""
        if self.items is None:
            return
        self._needs_changed()
        if not self.items.outbox:
            return
        try:
            task = asyncio.get_running_loop().create_task(self.items.deliver())
        except RuntimeError:
            return
        self._item_pushes.add(task)
        task.add_done_callback(self._item_pushes.discard)

    def needs_view(self) -> tuple[int, str | None]:
        """The Home badge count and the top Needs you item's title (daemon-written, never model text)."""
        if self.items is None or self.store is None:
            return 0, None
        try:
            needs = self.items.home()["needs"]
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.warning("needs view unavailable", extra={"error": str(err)[:200]})
            return 0, None
        return len(needs), needs_label(needs[0]) if needs else None

    def _needs_changed(self) -> None:
        """Re-publish `sensor.ops_agent` when the Needs you count or its top item changed (the dashboard pill)."""
        view = self.needs_view()
        if view == self._needs_published:
            return
        self._needs_published = view
        try:
            task = asyncio.get_running_loop().create_task(self.publish_status())
        except RuntimeError:
            return
        self._item_pushes.add(task)
        task.add_done_callback(self._item_pushes.discard)

    async def items_flush(self) -> None:
        """Tick the items and wait for their pushes (start-up and tests; the loop does it on its own otherwise)."""
        self.items_now()
        if self.items is not None:
            await self.items.deliver()
        if self._item_pushes:
            await asyncio.gather(*self._item_pushes)

    def _changes_changed(self) -> None:
        self.hub.publish("changes", None, {})

    def _note_check(
        self, trigger: str, result: str, items: list[Item], summary: str, snapshot: Snapshot | None
    ) -> None:
        """One activity line per health check (a no-op is one compact line; pushes post in the chat)."""
        if self.activity is None:
            return
        patches = ((snapshot.local_patches if snapshot else None) or {}).get("patches") or {}
        states = [str(p.get("state")) for p in patches.values()]
        patch_line = (f" Local patches: {states.count('present')} present, {states.count('missing')} missing,"
                      f" {states.count('superseded')} superseded.") if patches else ""  # fmt: skip
        if not items:
            self.activity.record("check", f"Health check ({trigger}): nothing new", patch_line.strip(),
                                 ref={"trigger": trigger})  # fmt: skip
            return
        listed = "; ".join(f"[{i.severity}] {i.summary}" for i in items[:10])
        detail = f"{summary[:400]} Items: {listed}.{patch_line}"[:1500]
        # The items themselves are in Needs you (and the check pushed them); this line is the check's record.
        self.activity.record("check", f"Health check ({trigger}): {len(items)} new item(s), {result}", detail,
                             ref={"trigger": trigger},
                             chat=any(i.severity == "critical" for i in items))  # fmt: skip

    async def _check_config(self) -> tuple[bool, str]:
        """Core's own configuration check (the same as `ha core check`)."""
        try:
            result = await asyncio.to_thread(self.client.core, "/config/core/check_config", "POST", None,
                                             timeout=180)  # fmt: skip
        except ApiError as err:
            return False, f"the check could not run: {str(err)[:200]}"
        if isinstance(result, dict) and result.get("result") == "valid":
            return True, ""
        errors = result.get("errors") if isinstance(result, dict) else None
        return False, str(errors or result)[:300]

    async def _reload(self, domain: str, service: str) -> None:
        await asyncio.to_thread(self.client.call_service, domain, service, {})

    async def _on_change_approved(self, event: Event) -> None:
        approval = event.payload.get("approval")
        if isinstance(approval, str) and self.changes is not None:
            await self.changes.execute(approval)

    def chat_context(self) -> str:
        """What is open around a chat, from the daemon's records."""
        store = self.store
        if store is None:
            return ""
        block = context_block(
            approvals=store.approvals(states=("pending", "approved"), limit=20),
            proposals=proposals.refresh(store, now=datetime.now(UTC)),
            holds=list(self.updates.holds().values()) if self.updates is not None else [],
            items=[i.to_json() for i in self.open_items()],
        )
        recent = self.activity.context(self.tz) if self.activity is not None else ""
        return f"{block}\n\n{recent}" if recent else block

    def close(self) -> None:
        if self.store is not None:
            self.store.close()

    # ------------------------------------------------------------ utilities

    def day(self, now: datetime) -> str:
        return now.astimezone(self.tz).date().isoformat()

    def start_backup(self, *, by: str, admin: bool) -> bool:
        """Run one backup in the background (see `opsd.backups`); False if one is already running."""
        if self.backups is None or self.backups.running or (self._backup_task and not self._backup_task.done()):
            return False
        self._backup_task = asyncio.create_task(self._run_backup(by=by, admin=admin))
        return True

    async def _run_backup(self, *, by: str, admin: bool) -> None:
        if self.backups is None:
            return
        record = await self.backups.run(by=by, admin=admin)
        if self.store is not None:
            proposals.settle_backup(self.store, now=datetime.now(UTC), detail=f"backup {record.get('state')}")
        # A backup made is recorded silently (its item on Home and History). A failed one is an item in
        # Needs you (`Backups._note`), which the item book pushes once.

    # ------------------------------------------------- known false positives

    def suppression_rules(self) -> tuple[list[suppress.Rule], list[str]]:
        """The learned rules in docs/known-false-positives.yaml (strictly parsed).

        The suppress house overlay: no rules at all unless the house profile enables it.
        """
        if not self.options.profile.overlay("suppress"):
            return [], []
        from opsd.house import suppress  # noqa: PLC0415 - a house overlay, loaded only when the profile enables it

        try:
            text = (self.paths.ha_config / SUPPRESS_PATH).read_text(errors="replace")
        except FileNotFoundError:
            text = ""
        except OSError as err:
            return [], [f"{SUPPRESS_PATH} unreadable: {type(err).__name__}"]
        parsed: tuple[list[suppress.Rule], list[str]] = suppress.parse_file(text)
        return parsed

    def suppression_revoked(self) -> frozenset[str]:
        if not self.options.profile.overlay("suppress"):
            return frozenset()
        from opsd.house import suppress  # noqa: PLC0415 - a house overlay

        revoked: frozenset[str] = suppress.revoked_ids(
            self.store.kv_get(suppress.REVOKED_KEY) if self.store is not None else ""
        )
        return revoked

    def _suppress(self, items: list[Item], snapshot: Snapshot, now: datetime) -> list[Item]:
        """Drop items a rule's verifier clears (listed in Activity), raise louder the ones it does not."""
        if not self.options.profile.overlay("suppress"):
            return items
        from opsd.house import suppress  # noqa: PLC0415 - a house overlay

        rules, problems = self.suppression_rules()
        applied = suppress.apply(items, snapshot, rules, today=now.astimezone(self.tz).date(),
                                 revoked=self.suppression_revoked())  # fmt: skip
        for decision in [*applied.suppressed, *applied.raised]:
            outcome = "suppressed" if decision.passed else "raised"
            if self.store is not None:
                try:
                    args = {"key": decision.item.key, "rule": decision.rule.id}
                    self.store.audit(at=now, run_id=None, principal="daemon", channel="health",
                                     capability="suppress.decide", args=args, tier=None, decision=outcome,
                                     outcome=outcome, detail=decision.evidence)  # fmt: skip
                except (StoreError, sqlite3.Error) as err:
                    _LOGGER.error("suppression audit failed", extra={"error": str(err)[:200]})
            if decision.passed and self.activity is not None:
                self.activity.record("suppressed", f"Suppressed: {decision.item.summary}"[:160],
                                     f"{decision.rule.reason} Check: {decision.evidence}.",
                                     ref={"key": decision.item.key, "rule": decision.rule.id})  # fmt: skip
        items = applied.items
        if problems:
            items.append(Item("suppress:invalid", "suppression", "warning",
                              f"{SUPPRESS_PATH} has rules the agent ignores: {'; '.join(problems)}"[:300]))  # fmt: skip
        return items

    def guard_anomalies(self, now: datetime) -> tuple[str, ...]:
        """The latest guard-hook anomaly if it is recent (see `runner.GUARD_ANOMALY_KEY`)."""
        if self.store is None:
            return ()
        try:
            record = json.loads(self.store.kv_get(GUARD_ANOMALY_KEY) or "{}")
            at = datetime.fromisoformat(str(record["at"]))
        except StoreError, sqlite3.Error, ValueError, KeyError, TypeError:
            return ()
        if now - at > timedelta(days=GUARD_ANOMALY_DAYS):
            return ()
        found = record.get("anomalies")
        return tuple(f"{record.get('profile')}: {a}" for a in found) if isinstance(found, list) else ()

    def credential_kind(self) -> str | None:
        """Which kind of provider credential is configured (`setup_token`, `api_key`), never the value."""
        kind = credential_kind(claude_token(self.options, self.paths))
        return None if kind is None else str(kind)

    def options_notice(self) -> dict[str, Any] | None:
        """What the panel says about the add-on options: refused values and the provider."""
        options = self.options
        provider = ""
        if options.llm.provider == "none":
            provider = "AI is off: llm.provider is none"
        elif not claude_token(options, self.paths):
            provider = "AI is off: no AI token configured"
        if not (options.all_problems or provider):
            return None
        return {
            "problems": list(options.all_problems),
            "provider": provider,
        }

    def profile_view(self) -> dict[str, Any]:
        """The house profile for the System sheet: loaded or not, why it was refused, and what it switches on."""
        profile = self.options.profile
        return {
            "path": PROFILE_PATH,
            "loaded": bool(profile.source),
            "problems": [p for p in self.options.problems if p.startswith("house profile")],
            "git": self.options.git_on,
            "overlays": sorted(profile.overlays),
            "push_target": bool(self.options.notify_service),
            "notify_problem": self.options.runtime.get(NOTIFY_NOT_ADMIN),
        }

    def redactor(self) -> Redactor:
        secrets = [claude_token(self.options, self.paths), self.client.token]
        return build_redactor(self.paths.secrets_yaml, secrets)

    def history(self) -> list[dict[str, Any]]:
        return read_jsonl(self.paths.history_file)[-100:]

    def status_view(self) -> dict[str, Any]:
        state = self.state
        now = datetime.now(UTC)
        return {
            "version": __version__,
            "mode": self.options.agent.mode,
            "last_health_at": state.last_health_at,
            "last_result": state.last_result,
            "last_summary": state.last_summary,
            "last_run_status": state.last_run_status,
            "pending_items": state.pending_items,
            # The dashboard's single "Ops Agent · N need you" pill.
            **dict(zip(("needs_you", "needs_you_top"), self.needs_view(), strict=True)),
            "core_down_since": state.core_down_since,
            "next_health_check": self.options.health.check_time,
            "selftest": self.selftest.summary() if self.selftest else "not run",
            "selftest_at": self.selftest.at if self.selftest else None,
            "inbound": "subscribed" if self.inbound.admin is not None else "admin unverified",
            "store": "ok" if self.store is not None else f"unavailable: {self.store_error}",
            "updates": self._updates_status(),
            **self.control.snapshot(now),
        }

    def _updates_status(self) -> dict[str, Any] | None:
        if self.updates is None:
            return None
        try:
            control = self.updates.control()
            runs = self.updates.store.recent_update_runs(limit=1)
        except StoreError, sqlite3.Error:
            return {"effective": False, "reason": "store unavailable"}
        latest = runs[0] if runs else None
        return {k: control[k] for k in ("option", "paused", "effective", "reason", "window")} | {
            "last": {k: latest[k] for k in ("window_day", "mode", "state", "summary")} if latest else None
        }

    async def publish_status(self, state_value: str | None = None) -> None:
        value = state_value or self.state.last_result or "unknown"
        attrs = {
            **self.status_view(),
            "friendly_name": "Ops Agent",
            "icon": "mdi:robot-outline",
        }
        try:
            await asyncio.to_thread(self.client.set_state, STATUS_ENTITY, value, attrs)
        except ApiError as err:
            _LOGGER.warning("status publish failed", extra={"error": str(err)[:200]})
            return
        if self.usage is not None:
            await self.usage.publish()

    async def send(self, message: Message, *, core_up: bool, now: datetime) -> str:
        if self._hold(message, now):
            return "held"
        return await asyncio.to_thread(self.channel.send, message, core_up=core_up, now=now)

    def quiet_window(self) -> quiet.Window | None:
        return quiet.window(self.options.notify.quiet_start, self.options.notify.quiet_end)

    def _hold(self, message: Message, now: datetime) -> bool:
        """Quiet hours: store a non-urgent push for later. Any store problem sends it now (never lost)."""
        win = self.quiet_window()
        if self.store is None or quiet.bypass(message) or not quiet.is_quiet(win, now, self.tz):
            return False
        try:
            self.store.hold_push(quiet.to_json(message), title=message.title, now=now)
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("could not hold a push; sending it now", extra={"error": str(err)[:200]})
            return False
        self.hub.publish("state", None, {})
        return True

    async def flush_held(self, now: datetime) -> str | None:
        """After quiet hours: each held push goes out. Daily push caps apply; overflow stays in the panel."""
        if self.store is None or quiet.is_quiet(self.quiet_window(), now, self.tz):
            return None
        try:
            rows = self.store.held_pushes()
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("held pushes unavailable", extra={"error": str(err)[:200]})
            return None
        if not rows:
            return None
        outcome = None
        for row in rows:
            outcome = await asyncio.to_thread(self.channel.send, quiet.from_json(row["message"]),
                                              core_up=self.state.core_down_since is None, now=now)  # fmt: skip
            if outcome == "failed":
                break  # this one and the rest are tried again on the next liveness pass
            to = "delivered" if outcome in ("sent", "relayed") else "undelivered"
            self.store.settle_held([int(row["id"])], to=to, outcome=outcome, now=now)
        self.hub.publish("state", None, {})
        return outcome

    def held_view(self, now: datetime) -> dict[str, Any] | None:
        """For the panel: how many pushes wait and until when; undelivered ones from the last day."""
        if self.store is None:
            return None
        try:
            held = self.store.held_pushes()
            missed = self.store.held_pushes(states=("undelivered",), since=now - timedelta(days=1))
        except StoreError, sqlite3.Error:
            return None
        win = self.quiet_window()
        until = quiet.ends_at(win, now, self.tz).isoformat(timespec="minutes") if win and held else None
        return {"count": len(held), "until": until, "undelivered": [r["title"] for r in missed][-20:]}

    async def send_now(self, message: Message) -> str:
        return await self.send(message, core_up=self.state.core_down_since is None, now=datetime.now(UTC))

    # EffectSink, for capability executors running inside an agent run.
    async def send_message(self, message: Message) -> str:
        if self.assistant is not None:
            message = self.assistant.with_reply(message, now=datetime.now(UTC))
        return await self.send_now(message)

    def _save_snapshot(self, snapshot: Snapshot) -> None:
        self.paths.snapshots_dir.mkdir(parents=True, exist_ok=True)
        name = snapshot.taken_at.replace(":", "-") + ".json"
        write_json_atomic(self.paths.snapshots_dir / name, snapshot.to_json())
        for old in sorted(self.paths.snapshots_dir.glob("*.json"))[:-KEEP_SNAPSHOTS]:
            old.unlink(missing_ok=True)

    # ------------------------------------------------- health.check handler

    async def health_cycle(self, trigger: str) -> CycleOutcome:
        if self._cycle_running.locked():
            return CycleOutcome("skipped_busy", 0, {})
        async with self._cycle_running:
            try:
                with run_lock(self.paths.lock_file):
                    await self.publish_status("running")
                    outcome = await self._health_cycle_locked(trigger)
            except LockBusyError:
                _LOGGER.warning("run lock held; skipping cycle", extra={"trigger": trigger})
                return CycleOutcome("skipped_busy", 0, {})
            await self.publish_status()
            return outcome

    async def _refresh_vacuum_maps(self, api: LiveAPI, trigger: str, now: datetime) -> None:
        """The vacuums overlay's integration reload before the scheduled check (`opsd.house.vacuums.refresh_maps`)."""
        if not self.options.profile.overlay("vacuums"):
            return
        from opsd.house import vacuums  # noqa: PLC0415 - a house overlay, loaded only when the profile enables it

        await vacuums.refresh_maps(self, api, trigger, now)

    async def _health_cycle_locked(self, trigger: str) -> CycleOutcome:
        now = datetime.now(UTC)
        redactor = self.redactor()
        async with LiveAPI(self.client) as api:
            await self._refresh_vacuum_maps(api, trigger, now)
            snapshot = await collect(api, self.paths, self.options, now=now, core_release_age=core_release_age_days)
        self._save_snapshot(snapshot)
        self._updates_seen = snapshot.updates
        if self.backups is not None:
            self.backups.observe(snapshot.backups, now)
        if self.backups is not None and self.backups.self_heal_due(snapshot.backups, acting=self.options.acting):
            self.start_backup(by="agent", admin=False)
        if snapshot.core.get("api_up"):
            await asyncio.to_thread(self.notifier.cache_registration, self.paths.core_config_entries)

        expected = self._expected_unavailable(snapshot)
        raw_baseline = read_json(self.paths.baseline_file, None)
        if raw_baseline is None:
            baseline = seed(snapshot, now, expected_unavailable=expected)
            write_json_atomic(self.paths.baseline_file, baseline.to_json())
            summary, critical = baseline_summary(snapshot, expected)
            outcome = await self.send(
                Message(
                    "Ops Agent is watching",
                    summary,
                    urgent=critical,
                    budget_class=BudgetClass.SAFETY if critical else BudgetClass.AGENT,
                    tag="ha-ops-baseline",
                ),
                core_up=bool(snapshot.core.get("api_up")),
                now=now,
            )
            return self._finish(now, trigger, "seeded", [], summary, notify=outcome, snapshot=snapshot)

        baseline = Baseline.from_json(raw_baseline)
        items = diff(snapshot, baseline, now, expected_unavailable=expected,
                     options_problems=self.options.all_problems, guard_anomalies=self.guard_anomalies(now),
                     backup_failure=self.backups.recent_failure() if self.backups is not None else None,
                     power_entity=self.options.profile.health.power_entity,
                     remote_entity=self.options.profile.health.remote_entity)  # fmt: skip
        items = self._suppress(items, snapshot, now)
        # Vacuum room drift (an overlay): the daemon pushes it itself, once per distinct drift; triage never sees it.
        pushed = [i for i in items if i.kind in DAEMON_PUSHED_KINDS]
        items = [i for i in items if i.kind not in DAEMON_PUSHED_KINDS]
        core_up = bool(snapshot.core.get("api_up"))
        pushed_outcome = await self._push_items(pushed, core_up=core_up, now=now)
        if pushed:
            # Record the push at once, so a failure later in the cycle cannot repeat it tomorrow.
            early = Baseline.from_json(baseline.to_json())
            early.vacuum_rooms_seen = vacuum_rooms_seen(baseline, snapshot)
            write_json_atomic(self.paths.baseline_file, _unsent(early, pushed, pushed_outcome).to_json())
        if not items:
            advanced = _unsent(advance(baseline, snapshot, now), pushed, pushed_outcome)
            write_json_atomic(self.paths.baseline_file, advanced.to_json())
            # Clear the panel's open items once whatever raised them has resolved.
            self._save_open_items(pushed, now)
            if pushed:
                return self._finish(now, trigger, "ok", pushed, "; ".join(i.summary for i in pushed)[:600],
                                    notify=pushed_outcome, unacked=[i.key for i in pushed],
                                    snapshot=snapshot)  # fmt: skip
            return self._finish(now, trigger, "ok", [], "No changes since baseline.", snapshot=snapshot)

        run, triage, effects = await self._triage(items, snapshot, trigger, now, redactor)
        rejected = list(triage.rejected) if triage else []
        baseline = _unsent(advance(baseline, snapshot, now), pushed, pushed_outcome)
        by_key = {i.key: i for i in items}
        acked: set[str] = set()
        # In-run acks were validated when the AI made them.
        for ack in effects.acks:
            baseline = apply_ack(
                baseline, by_key[ack.key], reason=ack.reason, days=ack.days, accept=ack.accept, now=now
            )
            acked.add(ack.key)
        write_json_atomic(self.paths.baseline_file, baseline.to_json())

        notify_outcome = await self._notify(items, triage, core_up=core_up, now=now)
        if effects.notify_outcomes:
            notify_outcome = "+".join([*effects.notify_outcomes, *([notify_outcome] if notify_outcome else [])])
        if pushed_outcome:
            notify_outcome = "+".join(o for o in (notify_outcome, pushed_outcome) if o)
        self._save_open_items([*items, *pushed], now)

        summary = (
            triage.summary
            if triage and triage.summary
            else f"{len(items)} change(s); triage {run.status if run else 'skipped'}"
        )
        result = (
            (triage.result if triage else "failed")
            if run and run.status == "ok"
            else (run.status if run else "untriaged")
        )
        return self._finish(
            now,
            trigger,
            result,
            [*items, *pushed],
            summary,
            notify=notify_outcome,
            run=run,
            rejected=rejected,
            unacked=[*(k for k in by_key if k not in acked), *(i.key for i in pushed)],
            effects=effects,
            snapshot=snapshot,
        )

    async def _push_items(self, items: list[Item], *, core_up: bool, now: datetime) -> str | None:
        """One non-urgent push for the items the daemon reports itself; quiet hours and the daily caps apply."""
        if not items:
            return None
        message = Message("Ops Agent: vacuum rooms changed", "\n".join(i.summary for i in items),
                          budget_class=BudgetClass.AGENT, tag="ha-ops-vacuum-rooms")  # fmt: skip
        return await self.send(message, core_up=core_up, now=now)

    async def _notify(
        self, items: list[Item], triage: TriageResult | None, *, core_up: bool, now: datetime
    ) -> str | None:
        """The deterministic safety net.

        Critical items ALWAYS produce a deterministic urgent push from the
        reserved safety bucket, whatever the AI did or said. Without a usable
        triage, warnings get a non-urgent deterministic summary.
        """
        outcomes: list[str] = []
        critical = [i for i in items if i.severity == "critical"]
        if critical:
            safety = fallback_push(critical)
            if safety is not None:
                outcomes.append(await self.send(safety, core_up=core_up, now=now))
        elif triage is None:
            fallback = fallback_push(items)
            if fallback is not None:
                outcomes.append(await self.send(fallback, core_up=core_up, now=now))
        return "+".join(outcomes) or None

    def _expected_unavailable(self, snapshot: Snapshot) -> frozenset[str]:
        """`health.expected_unavailable`, without anything that opens the house (one warning per entity)."""
        expected, refused = honoured_expected(self.options.health.expected_unavailable, snapshot)
        for entity_id in sorted(set(refused) - self._expected_refused):
            _LOGGER.warning("health.expected_unavailable lists access hardware; it is still reported",
                            extra={"entity_id": entity_id})  # fmt: skip
        self._expected_refused |= set(refused)
        return expected

    def _save_open_items(self, items: list[Item], now: datetime) -> None:
        write_json_atomic(
            self.paths.open_items_file, {"at": now.isoformat(timespec="seconds"), "items": [i.to_json() for i in items]}
        )
        if self.items is None:
            return
        # The health items are items too: cleared ones resolve, new ones enter Needs you.
        try:
            self.items.write_health()
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("health items not written", extra={"error": str(err)[:200]})

    def open_items(self) -> list[Item]:
        raw = read_json(self.paths.open_items_file, {})
        return [Item(**i) for i in raw.get("items", []) if isinstance(i, dict)]

    async def user_ack(self, key: str, *, days: int, accept: bool) -> bool:
        """An admin acknowledgement from the ingress panel (the only way to accept a critical item)."""
        item = next((i for i in self.open_items() if i.key == key), None)
        if item is None:
            return False
        async with self._cycle_running:
            raw = read_json(self.paths.baseline_file, None)
            if raw is None:
                return False
            now = datetime.now(UTC)
            baseline = apply_ack(
                Baseline.from_json(raw), item, reason="acknowledged by admin in panel", days=days,
                accept=accept, now=now, by="user",
            )  # fmt: skip
            write_json_atomic(self.paths.baseline_file, baseline.to_json())
            remaining = [i for i in self.open_items() if i.key != key]
            self._save_open_items(remaining, now)
            self.state.pending_items = len(remaining)
            self.state.save(self.paths.state_file)
        _LOGGER.info("user ack", extra={"key": key, "days": days, "accept": accept})
        await self.publish_status()
        return True

    async def _triage(
        self, items: list[Item], snapshot: Snapshot, trigger: str, now: datetime, redactor: Redactor
    ) -> tuple[RunResult, TriageResult | None, RunEffects]:
        recent = [f"{r.get('at')}: {r.get('result')} - {r.get('summary')}" for r in self.history()[-5:]]
        prompt = redactor.redact(build_prompt(items, snapshot, trigger=trigger, recent=recent))
        outcome = await self.runner.run_agent(
            TRIAGE, prompt, principal=AGENT_SELF, now=now, redactor=redactor, handoff={i.key: i for i in items}
        )
        run = outcome.run
        if outcome.run_dir is not None:
            self.state.last_run_status = run.status
        effects = outcome.context.effects
        if run.status != "ok":
            return run, None, effects
        triage = validate_result(run.structured)
        if outcome.run_dir is not None:
            write_json_atomic(
                outcome.run_dir / "result.json",
                {
                    "run": run.status,
                    "run_id": outcome.context.run_id,
                    "triage": triage.summary,
                    "rejected": triage.rejected,
                },
            )
        return run, triage, effects

    def _finish(
        self,
        now: datetime,
        trigger: str,
        result: str,
        items: list[Item],
        summary: str,
        *,
        notify: str | None = None,
        run: RunResult | None = None,
        rejected: list[str] | None = None,
        unacked: list[str] | None = None,
        effects: RunEffects | None = None,
        snapshot: Snapshot | None = None,
    ) -> CycleOutcome:
        record = {
            "at": now.isoformat(timespec="seconds"),
            "trigger": trigger,
            "result": result,
            "items": len(items),
            "item_keys": [i.key for i in items][:30],
            "unacked": (unacked or [])[:30],
            "summary": summary[:600],
            "notify": notify,
            "run_status": run.status if run else None,
            "cost_usd": round(run.cost_usd, 4) if run else 0.0,
            "turns": run.turns if run else 0,
            "denials": run.denials[:20] if run else [],
            "rejected": rejected or [],
            "in_run_effects": (
                {"acks": len(effects.acks), "pushes": len(effects.notify_outcomes)} if effects else None
            ),
        }
        append_jsonl(self.paths.history_file, record)
        self.state.last_health_at = now.isoformat(timespec="seconds")
        self.state.last_result = "attention" if unacked else ("ok" if result in ("ok", "seeded") else result)
        self.state.last_summary = summary[:600]
        self.state.pending_items = len(unacked or [])
        self.state.version = __version__
        self.state.save(self.paths.state_file)
        self._note_check(trigger, result, items, summary, snapshot)
        _LOGGER.info("health cycle done", extra={k: record[k] for k in ("trigger", "result", "items", "notify")})
        return CycleOutcome(result, len(items), record)

    # --------------------------------------------------------------- ops log

    # ------------------------------------------------------- liveness (independent)

    async def liveness_loop(self) -> None:
        while True:
            await asyncio.sleep(LIVENESS_INTERVAL_S)
            try:
                now = datetime.now(UTC)
                await self.flush_held(now)
                alive = await asyncio.to_thread(self.client.core_alive)
                if alive:
                    await self._core_up(now)
                else:
                    await self._core_down(now)
                self._check_overdue(now)
                # Time moves items too (an expiry, a daily re-surface, "today" on Home).
                self.items_now()
            except Exception:
                _LOGGER.exception("liveness probe failed")

    # ------------------------------------------------------------- dry run

    def _check_overdue(self, now: datetime) -> None:
        """A safety net for the safety net: a daily check that stopped firing is requested here."""
        if now - self._started_at < OVERDUE_GRACE:
            return
        last = self.state.last_health_at
        if last is not None and now - datetime.fromisoformat(last) < HEALTH_OVERDUE:
            return
        if self._last_overdue_request is not None and now - self._last_overdue_request < OVERDUE_RETRY:
            return
        self._last_overdue_request = now
        if self.request_cycle("overdue"):
            _LOGGER.warning("daily health check overdue; requested one", extra={"last_health_at": last})

    async def _core_down(self, now: datetime) -> None:
        if self.state.core_down_since is None:
            self.state.core_down_since = now.isoformat(timespec="seconds")
            self.state.core_down_notified = False
            self.state.save(self.paths.state_file)
            _LOGGER.warning("core API unreachable")
            return
        since = datetime.fromisoformat(self.state.core_down_since)
        if self.state.core_down_notified or now - since < CORE_DOWN_ALERT_AFTER:
            return
        try:
            info = await asyncio.to_thread(self.client.supervisor, "/core/info")
            core_state = str(info.get("state", "unknown"))
        except ApiError:
            core_state = "unknown (Supervisor API error)"
        try:
            jobs_busy = supervisor_jobs_busy(await asyncio.to_thread(self.client.supervisor, "/jobs/info"))
        except ApiError:
            jobs_busy = False
        threshold = core_down_threshold(
            core_state, jobs_busy=jobs_busy, in_update_window=in_window(now, self.options.updates.window, self.tz)
        )
        if now - since < threshold:
            _LOGGER.info("core down, alert deferred", extra={"state": core_state, "jobs_busy": jobs_busy})
            return
        minutes = int((now - since).total_seconds() // 60)
        message = Message(
            "Ops Agent: Core is down",
            f"Home Assistant Core has been unreachable for {minutes} min (Supervisor reports: {core_state}). "
            "The agent is watching and will report when it is back. Push buttons won't work until then.",
            urgent=True,
            budget_class=BudgetClass.SAFETY,
            tag="ha-ops-core-down",
        )
        outcome = await self.send(message, core_up=False, now=now)
        self.state.core_down_notified = outcome in ("sent", "relayed")
        self.state.save(self.paths.state_file)

    async def _core_up(self, now: datetime) -> None:
        if self.state.core_down_since is not None:
            since = datetime.fromisoformat(self.state.core_down_since)
            minutes = int((now - since).total_seconds() // 60)
            if self.state.core_down_notified:
                await self.send(
                    Message(
                        "Ops Agent: Core is back",
                        f"Core is reachable again after about {minutes} min.",
                        budget_class=BudgetClass.SAFETY,
                        tag="ha-ops-core-down",
                    ),
                    core_up=True,
                    now=now,
                )
                self.request_cycle("core_recovered")
            self.state.core_down_since = None
            self.state.core_down_notified = False
            self.state.save(self.paths.state_file)
            await self.publish_status()

    # ------------------------------------------------ events and scheduling

    def request_cycle(self, trigger: str) -> bool:
        """Queue a health check. False if one is queued or running, or the manual cap is hit."""
        if self._cycle_running.locked() or self.queue.busy(HEALTH_TASK):
            return False
        if trigger == "manual":
            try:
                if self.store is None:
                    raise StoreError("store unavailable")
                allowed = self.store.bump(
                    self.day(datetime.now(UTC)), "manual_trigger", limit=MAX_MANUAL_TRIGGERS_PER_DAY
                )
            except (StoreError, sqlite3.Error) as err:
                _LOGGER.error("manual trigger refused: store unavailable", extra={"error": str(err)[:200]})
                return False
            if not allowed:
                _LOGGER.warning("manual trigger cap reached")
                return False
        return self.queue.put(
            Event("health.requested", source=trigger, priority=Priority.TRIAGE, payload={"trigger": trigger},
                  dedupe_key=HEALTH_TASK)
        )  # fmt: skip

    def _next_fire(self, schedule: Schedule, now: datetime) -> datetime:
        if schedule.daily_at is not None:
            return next_run_at(now, schedule.daily_at, self.tz)
        if schedule.every_s is not None:
            return now + timedelta(seconds=schedule.every_s)
        return now + timedelta(days=1)

    def _task_event(self, task: Task) -> Event:
        payload: dict[str, Any] = {"task_id": task.id}
        if task.system_key == HEALTH_TASK:
            trigger = task.payload.get("trigger")
            payload["trigger"] = trigger if trigger in ("daily", "catch_up") else "daily"
            return Event("health.requested", "scheduler", Priority.TRIAGE, payload, dedupe_key=HEALTH_TASK)
        if task.system_key == STATUS_TASK:
            return Event("status.refresh", "scheduler", Priority.MAINTENANCE, payload, dedupe_key=STATUS_TASK)
        if task.system_key == UPDATE_TASK:
            return Event("updates.window", "scheduler", Priority.TRIAGE, payload, dedupe_key=UPDATE_TASK)
        if task.kind == "retry":
            return Event("chat.retry", "scheduler", Priority.USER, payload, dedupe_key=f"task:{task.id}")
        return Event("schedule.fired", "scheduler", Priority.FOLLOWUP, payload, dedupe_key=f"task:{task.id}")

    def setup_schedule(self, startup_delay_s: float) -> None:
        """System tasks, (re)created at every start; a stale health check catches up after a delay."""
        now = datetime.now(UTC)
        if self.store is None or self.scheduler is None:
            return
        self.store.recover_running(now)
        if self.assistant is not None:
            self.assistant.recover(now)
        if self.changes is not None:
            try:
                self.changes.recover()
            except (StoreError, sqlite3.Error, OSError) as err:
                _LOGGER.error("change recovery failed", extra={"error": str(err)[:300]})
        last = self.state.last_health_at
        stale = last is None or now - datetime.fromisoformat(last) > timedelta(hours=24)
        due = (
            now + timedelta(seconds=startup_delay_s)
            if stale
            else next_run_at(now, self.options.health.check_time, self.tz)
        )
        self.store.ensure_system_task(
            HEALTH_TASK,
            title="Daily health check",
            schedule=Schedule(daily_at=self.options.health.check_time).to_json(),
            next_fire_at=due,
            payload={"trigger": "catch_up" if stale else "daily"},
            now=now,
        )
        # No catch-up: a missed window waits for the next night.
        self.store.ensure_system_task(
            UPDATE_TASK,
            title="Nightly update window",
            schedule=Schedule(daily_at=self.options.updates.window).to_json(),
            next_fire_at=next_run_at(now, self.options.updates.window, self.tz),
            payload={},
            now=now,
        )
        self.store.ensure_system_task(
            STATUS_TASK,
            title="Status sensor refresh",
            schedule=Schedule(every_s=int(STATUS_REFRESH.total_seconds())).to_json(),
            next_fire_at=now + STATUS_REFRESH,
            payload={},
            now=now,
        )
        self.scheduler.wake()

    def _complete_task(self, event: Event, payload: dict[str, Any] | None = None) -> None:
        task_id = event.payload.get("task_id")
        if not isinstance(task_id, int) or self.store is None or self.scheduler is None:
            return
        task = self.store.get_task(task_id)
        if task is not None:
            self.scheduler.complete(task, payload=payload)

    async def _on_health_requested(self, event: Event) -> None:
        trigger = str(event.payload.get("trigger", "daily"))
        try:
            if self.updates is not None:
                # Restarts during updates would read as outages; check once they are done.
                await self.updates.wait_idle()
                await self._updates_upkeep(before_check=True)
            await self.health_cycle(trigger)
            await self._updates_upkeep(before_check=False)
            if self.maintenance is not None:
                await self.maintenance.scan(trigger)
        except Exception:
            _LOGGER.exception("health cycle crashed", extra={"trigger": trigger})
            await self.publish_status("failed")
        finally:
            self._complete_task(event, payload={"trigger": "daily"})
            # Daily retention, also when no AI run happened.
            self.runner.prune(datetime.now(UTC))

    async def _updates_upkeep(self, *, before_check: bool) -> None:
        """Before a check: confirm OS updates that waited for a reboot. After it: re-plan if versions changed.

        Never stops the health check: a failure here is logged and the check goes on.
        """
        updates = self.updates
        if updates is None:
            return
        try:
            if before_check:
                await updates.reconcile()
                return
            seen = self._updates_seen
            # Also when the plan predates this start (the start-up refresh failed): items wait for a fresh one.
            older = self.items is not None and self.items.plan_stale
            if (seen is not None and updates.plan_stale(seen)) or older:
                await updates.refresh()
        except Exception:
            _LOGGER.exception("update upkeep failed", extra={"before_check": before_check})

    async def _on_status_refresh(self, event: Event) -> None:
        try:
            # States set over REST vanish on a Core restart; keep re-asserting it.
            if self.state.core_down_since is None and not self._cycle_running.locked():
                await self.publish_status()
            if self.changes is not None:
                self.changes.expire()
        finally:
            self._complete_task(event)

    async def _update_snapshot(self) -> Snapshot:
        async with LiveAPI(self.client) as api:
            return await collect(api, self.paths, self.options, now=datetime.now(UTC),
                                 core_release_age=core_release_age_days, release_age=github_release_age_days,
                                 seen_age=self.updates.seen_age if self.updates is not None else None,
                                 run_config_check=False)  # fmt: skip

    async def _house_domains(self) -> frozenset[str]:
        """The integrations this house uses: its config entries and Core's loaded components (both halves of a
        `<platform>.<integration>` entry). Read-only; empty when either cannot be read (every Core candidate asks)."""
        async with LiveAPI(self.client) as api:
            entries = await api.ws("config_entries/get")
            config = await api.core_rest("/config")
        components = config.get("components") if isinstance(config, dict) else None
        if not isinstance(entries, list) or not isinstance(components, list):
            return frozenset()
        found = {str(e.get("domain")) for e in entries if isinstance(e, dict) and not e.get("disabled_by")}
        found |= {part for c in components for part in str(c).split(".")}
        return frozenset(d for d in found if d and d != "None")

    async def _propose_fix(self, item: Mapping[str, Any], domains: frozenset[str], why: str,
                           edits: list[updatefix.Edit]) -> dict[str, Any]:  # fmt: skip
        """A held update's config change, as a proposal linked to it (opsd.updatefix); ChangeRefused when not."""
        if self.store is None:
            raise ChangeRefused("the store is not available")
        if self.repo is None:
            raise ChangeRefused("git proposals are off on this install")
        now = datetime.now(UTC)
        entities = await registry_entities(self.client)
        row = await asyncio.to_thread(updatefix.propose, self.store, self.repo, self.options, item=item, why=why,
                                      edits=edits, domains=domains, day=self.day(now), now=now,
                                      entities=entities)  # fmt: skip
        self._changes_changed()
        return row

    # ------------------------------------------------------------ maintenance

    def _readable_names(self) -> list[str]:
        """The config files the agent may read (the export's own rules): tracked ones with git, else on disk."""
        root, reads = self.paths.ha_config, self.options.reads
        if self.options.git_on:
            return privsep.tracked_readable_files(root, reads)
        return privsep.disk_files(root, reads)

    async def _maintenance_gather(self) -> maintenance.Gathered:
        """The scan's local reads (read-only: repairs, the log, entries, dashboards, add-ons, readable YAML)."""
        async with LiveAPI(self.client) as api:
            core = await api.supervisor("/core/info")
            issues = await api.ws("repairs/list_issues")
            logs = await api.ws("system_log/list")
            entries = await api.ws("config_entries/get")
            config = await api.core_rest("/config")
            registry = await api.ws("config/entity_registry/list")
            problems: list[str] = []
            try:
                resources = await api.ws("lovelace/resources")
            except Exception as err:  # noqa: BLE001 - a missing source is a problem, never a crash
                resources = []
                problems.append(f"dashboard resources unreadable: {clean_text(str(err), 120)}")
            boards, configs, board_problems = await dashboards.read_all(api.ws)
            addons = await api.supervisor("/addons")
        try:
            names = await asyncio.to_thread(self._readable_names)
        except (subprocess.SubprocessError, OSError) as err:
            # The YAML features are one part of the inventory: the other signals still count.
            names = []
            problems.append(f"config files unreadable: {clean_text(str(err), 120)}")
        texts = await asyncio.to_thread(inventory.read_yaml, self.paths.ha_config, names)
        custom = {d: inventory.manifest(self.paths.custom_components, d)
                  for d in custom_domains(self.paths.custom_components)}  # fmt: skip
        inv = inventory.build(entries=entries, components=(config or {}).get("components") if isinstance(config, dict)
                              else None, texts=texts, custom=custom, registry=registry, resources=resources,
                              dashboards=boards, addons=addons, problems=problems + board_problems)  # fmt: skip
        return maintenance.Gathered(
            installed=str((core or {}).get("version") or "") if isinstance(core, dict) else "",
            issues=issues.get("issues") if isinstance(issues, dict) else [],
            logs=logs, inventory=inv, configs=configs,
        )  # fmt: skip

    async def _maintenance_plan(self, prompt: str) -> RunResult:
        """The capability-free maintenance plan run; skipped (not an error) when AI is off or capped."""
        redactor = self.redactor()
        outcome = await self.runner.run_agent(MAINTENANCE_PLAN, redactor.redact(prompt), principal=AGENT_SELF,
                                              now=datetime.now(UTC), redactor=redactor, wait_s=600.0)  # fmt: skip
        return outcome.run

    async def _maintenance_before_plan(self) -> None:
        if self.maintenance is not None and self.maintenance.due(before_update=True):
            await self.maintenance.scan("before_update")

    async def _propose_maintenance(self, finding: maintenance.Finding, why: str,
                                   edits: list[updatefix.Edit]) -> dict[str, Any]:  # fmt: skip
        """A finding's YAML migration as an ordinary proposal; ChangeRefused when not."""
        if self.store is None or self.repo is None:
            raise ChangeRefused("git proposals are off on this install")
        now = datetime.now(UTC)
        entities = await registry_entities(self.client)
        row = await asyncio.to_thread(maintenance.propose_yaml, self.store, self.repo, self.options,
                                      finding=finding, why=why, edits=edits, day=self.day(now), now=now,
                                      entities=entities)  # fmt: skip
        self._changes_changed()
        return row

    async def _ws_command(self, msg_type: str, **fields: Any) -> Any:
        """One websocket command on its own connection (every one passes the self-guard)."""
        async with LiveAPI(self.client) as api:
            return await api.ws(msg_type, **fields)

    async def request_dashboard(self, finding: str, user: str, shown: list[str]) -> dict[str, Any]:
        """A pending approval for a finding's dashboard plan, exactly as the admin saw it."""
        writer, store = self.dashboards, self.store
        if writer is None or store is None:
            raise ApprovalError("dashboard changes are unavailable")
        problem = writer.allowed()
        if problem is not None:
            raise ApprovalError(problem)
        bound = (maintenance.dashboard_plan(store, finding) or {}).get("binding")
        if not isinstance(bound, dict) or [str(bound.get("after"))] != shown:
            raise ApprovalError("The plan changed; open it again.")
        problem = await writer.mode_problem(str(bound.get("url_path") or ""))
        if problem is not None:
            raise ApprovalError(problem)
        now_bound, _ = await writer.binding_now(bound)
        if now_bound != bound:
            raise ApprovalError("The dashboard changed since the plan was made; the next scan makes a new plan.")
        title = f"Dashboard {bound.get('url_path')}: maintenance plan"
        return writer.approvals.request_dashboard(bound, title=title, user=user)

    async def _confirm_dashboard(self, approval: str, user: str) -> bool:
        writer, store = self.dashboards, self.store
        if writer is None or store is None:
            return False
        found = store.approval(approval) or {}
        bound = (found.get("plan") or {}).get("binding")
        now_bound, _ = await writer.binding_now(bound) if isinstance(bound, dict) else (None, None)
        state = writer.approvals.confirm_dashboard(approval, user=user, binding_now=now_bound)
        if state != "approved":
            return False
        self.queue.put(Event("maintenance.dashboard", "panel", Priority.APPROVAL, {"approval": approval},
                             dedupe_key=f"maintenance.dashboard:{approval}"))  # fmt: skip
        return True

    async def _on_dashboard_approved(self, event: Event) -> None:
        approval = event.payload.get("approval")
        if self.dashboards is None or self.maintenance is None or self.store is None or not isinstance(approval, str):
            return
        found = self.store.approval(approval) or {}
        bound = (found.get("plan") or {}).get("binding") or {}
        finding = str(bound.get("finding") or "")
        try:
            outcome = await self.dashboards.execute(approval)
        except Exception:
            _LOGGER.exception("dashboard plan crashed")
            outcome = dashboards.Outcome("refused", "the dashboard change crashed before it was saved")
        if not finding:
            return
        self.maintenance.record_result(finding, outcome, str(bound.get("url_path") or ""))
        if self.activity is not None:
            self.activity.record("maintenance", f"Dashboard change {outcome.state}", outcome.detail,
                                 item=f"maint:{finding}")  # fmt: skip

    async def _update_review(self, prompt: str) -> RunResult:
        """The capability-free release-note review; skipped (not an error) when AI is off or capped."""
        outcome = await self.runner.run_agent(UPDATE_REVIEW, prompt, principal=AGENT_SELF, now=datetime.now(UTC),
                                              redactor=self.redactor(), wait_s=600.0)  # fmt: skip
        return outcome.run

    async def _on_update_window(self, event: Event) -> None:
        try:
            if self.updates is not None:
                result = await self.updates.run_window("window")
                _LOGGER.info("update window", extra={"result": result.get("result")})
        except Exception:
            _LOGGER.exception("update window crashed")
        finally:
            self._complete_task(event)

    async def _on_updates_approved(self, event: Event) -> None:
        """The admin approved an update for "now": run it through the normal pipeline (it may still wait)."""
        if self.updates is None:
            return
        try:
            await self.updates.run_window("approved")
        except Exception:
            _LOGGER.exception("approved update run crashed")

    async def _on_updates_resume(self, event: Event) -> None:
        if self.updates is None:
            return
        try:
            resumed = await self.updates.resume()
            await self.updates.reconcile()
            if not resumed:
                # A finished run already re-planned; otherwise the plan may predate this start.
                await self.updates.refresh()
        except Exception:
            _LOGGER.exception("update resume crashed")

    async def panel_updates(self, on: bool, admin_user: str) -> None:
        """Pause or resume automatic updates from the panel (resuming is a high-impact action)."""
        if self.updates is not None:
            self.updates.set_paused(not on, by=f"panel:{admin_user[:64]}")
            await self.publish_status()

    async def _on_schedule_fired(self, event: Event) -> None:
        task_id = event.payload.get("task_id")
        task = self.store.get_task(task_id) if isinstance(task_id, int) and self.store is not None else None
        if task is not None and self.assistant is not None and task.system_key is None:
            await self.assistant.on_task_fired(task)
            return
        _LOGGER.warning("no handler for task; marking failed", extra={"task_id": task_id})
        if isinstance(task_id, int) and self.store is not None:
            self.store.finish_task(task_id, "failed", now=datetime.now(UTC))

    async def _on_user_message(self, event: Event) -> None:
        if self.assistant is not None:
            await self.assistant.on_user_message(event)

    async def _on_user_action(self, event: Event) -> None:
        if self.assistant is not None:
            await self.assistant.on_user_action(event)

    async def panel_action(self, kind: str, args: dict[str, Any], user: str) -> bool:
        """A high-impact action the admin asked for in the panel (the panel API has checked admin and CSRF)."""
        if kind == "ai_on" and self.usage is not None:
            done = await self.usage.resume(user)
            self.hub.publish("state", None, {})
            return done
        if kind == "updates_on" and self.updates is not None:
            await self.panel_updates(True, user)
            return True
        if kind == "approve_update" and self.updates is not None:
            approval = args.get("approval")
            if not isinstance(approval, str):
                return False
            state = await self.updates.confirm_approval(approval, user)
            if state == "now":
                self.queue.put(Event("updates.approved", "panel", Priority.APPROVAL, {}, dedupe_key="updates.approved"))
            return state in ("approved", "now")
        if kind == "approve_reboot" and self.updates is not None:
            approval = args.get("approval")
            if not isinstance(approval, str):
                return False
            return await self.updates.confirm_reboot(approval, user) == "approved"
        if kind == "approve_change" and self.changes is not None:
            approval = args.get("approval")
            if not isinstance(approval, str):
                return False
            state = await self.changes.confirm(approval, user)
            if state == "approved":
                self.queue.put(Event("changes.execute", "panel", Priority.APPROVAL, {"approval": approval},
                                     dedupe_key=f"changes.execute:{approval}"))  # fmt: skip
            return state == "approved"
        if kind == "approve_dashboard":
            approval = args.get("approval")
            return isinstance(approval, str) and await self._confirm_dashboard(approval, user)
        if kind == "backup_now":
            return self.start_backup(by=f"panel:{user[:64]}", admin=True)
        if kind == "ack":
            mode = args.get("mode")
            key = args.get("key")
            if isinstance(key, str) and mode in ("snooze", "accept"):
                return await self.user_ack(key, days=30 if mode == "accept" else 7, accept=mode == "accept")
        return False

    async def _on_chat_retry(self, event: Event) -> None:
        task_id = event.payload.get("task_id")
        task = self.store.get_task(task_id) if isinstance(task_id, int) and self.store is not None else None
        if task is None or task.kind != "retry" or self.assistant is None:
            _LOGGER.warning("chat retry without its task", extra={"task_id": task_id})
            return
        await self.assistant.on_retry_fired(task)

    # ----------------------------------------------------------- inbound

    async def refresh_admin(self) -> AdminIdentity | None:
        """The admin is the user behind the configured notify device, confirmed as an active admin."""
        try:
            entries = json.loads(await asyncio.to_thread(self.paths.core_config_entries.read_text))
            identity = admin_identity(entries, self.options.notify_service)
            if identity is not None:
                async with LiveAPI(self.client) as api:
                    users = await api.ws("config/auth/list")
                # Of the registrations with this name, the one whose user is an active admin.
                identity = admin_identity(entries, self.options.notify_service, users)
                if identity is None or not is_active_admin(users, identity.user_id):
                    _LOGGER.error("notify device's user is not an active admin; inbound events refused")
                    identity = None
                    # Never push to (or act for) someone else's device; fail closed.
                    problem = (
                        f"notify device {self.options.notify_service} belongs to a user who is not an active"
                        " Home Assistant admin: no push reaches it, and act mode, updates and AI are off"
                    )
                    self.options.runtime.set(NOTIFY_NOT_ADMIN, problem)
                else:
                    self.options.runtime.clear(NOTIFY_NOT_ADMIN)
        except (OSError, ValueError, RuntimeError) as err:
            _LOGGER.warning("admin identity unavailable; inbound events refused", extra={"error": str(err)[:200]})
            identity = None
        if identity is None and self.inbound.admin is None:
            _LOGGER.warning("no admin identity for the notify device; inbound events refused")
        elif identity is not None and identity != self.inbound.admin:
            _LOGGER.info("admin identity verified", extra={"device_known": identity.device_id is not None})
        changed = identity != self.inbound.admin
        self.inbound.admin = identity
        if changed:
            await self.publish_status()
        return identity

    async def panel_ai(self, on: bool, admin_user: str) -> None:
        """Resume or stop AI from the panel (already checked to be the admin)."""
        if self.usage is None:
            return
        if on:
            await self.usage.resume(admin_user)
        else:
            await self.usage.apply_switch("off", user=admin_user, by=f"panel:{admin_user[:64]}")
            await self.usage.set_switch(on=False)
        self.hub.publish("state", None, {})

    async def admin_loop(self) -> None:
        """Re-resolve the admin identity: hourly until known (Core may be down at start), then daily."""
        while True:
            try:
                identity = await self.refresh_admin()
            except Exception:
                _LOGGER.exception("admin identity refresh crashed")
                identity = None
            await asyncio.sleep(ADMIN_REFRESH_KNOWN_S if identity is not None else ADMIN_REFRESH_UNKNOWN_S)

    async def _ws_connect(self) -> HAWebSocket:
        ws = HAWebSocket(CORE_WS_URL, self.client.token)
        await ws.connect()
        return ws

    # ------------------------------------------------------------ startup

    async def resolve_timezone(self) -> None:
        name = os.environ.get("TZ", "")
        try:
            info = await asyncio.to_thread(self.client.supervisor, "/info")
            name = str(info.get("timezone") or name)
        except ApiError as err:
            _LOGGER.warning("timezone lookup failed", extra={"error": str(err)[:200]})
        try:
            self.tz = ZoneInfo(name or "UTC")
        except ZoneInfoNotFoundError:
            self.tz = ZoneInfo("UTC")

    def rpc_server(self) -> RpcServer:
        account = ops_account() if self.require_privsep else None
        allowed = frozenset({account.uid}) if account is not None else None
        env = ExecEnv(
            paths=self.paths,
            options=self.options,
            client=self.client,
            redactor=self.redactor,
            store=self.store,
            effects=self,
            # Local times in capabilities (reminders, history) are the house's, not UTC.
            tz=lambda: self.tz,
            repo=self.repo,
        )
        on_request = self.assistant.on_capability if self.assistant is not None else None
        return RpcServer(env, allowed_uids=allowed, day=self.day, on_request=on_request)

    async def start_rpc(self) -> RpcServer:
        server = self.rpc_server()
        account = ops_account() if self.require_privsep else None
        await server.start(group_gid=account.gid if account is not None else None)
        self.rpc = server
        return server

    async def run_selftest(self) -> SelfTest:
        """Prove the privilege boundary live; the AI is blocked until this passes."""
        result = await asyncio.to_thread(run_selftest, self.paths, daemon_pid=os.getpid())
        self.selftest = result
        write_json_atomic(self.paths.data / "selftest.json", result.to_json())
        log = _LOGGER.info if result.ok else _LOGGER.error
        log("selftest", extra={"selftest": result.summary(), "checks": result.to_json()["checks"]})
        await self.publish_status()
        return result

    async def run_forever(self) -> None:
        """Until SIGTERM (s6 stopping the service): then the pushes already decided are sent first."""
        main = asyncio.current_task()
        if main is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main.cancel)
        try:
            await self._run_forever()
        except asyncio.CancelledError:
            _LOGGER.info("opsd stopping")
        finally:
            await self.drain_pushes()

    async def drain_pushes(self) -> None:
        """Wait (briefly: s6 kills the process soon after SIGTERM) for item pushes and status updates in flight.

        A push still not sent is not lost: the item records a push only once it was delivered, so the next
        start decides it again.
        """
        pending = {t for t in self._item_pushes if not t.done()}
        if pending:
            await asyncio.wait(pending, timeout=SHUTDOWN_PUSH_S)

    async def _run_forever(self) -> None:
        # An outage recorded before a restart or reboot says nothing about now.
        self.state.core_down_since = None
        self.state.core_down_notified = False
        self.state.save(self.paths.state_file)
        await self.start_rpc()
        await self.resolve_timezone()
        try:
            await self.run_selftest()
        except Exception:
            _LOGGER.exception("selftest crashed")
        _LOGGER.info(
            "opsd started",
            extra={
                "version": __version__,
                "mode": self.options.agent.mode,
                "tz": str(self.tz),
                "options": repr(self.options),
            },
        )

        self._loop = asyncio.get_running_loop()
        if self.items is not None:
            # The options are a source too (the update gate and window, act mode): they change only at a start.
            try:
                self.items.recompose()
            except Exception:
                # The items stay as stored; their sources' next writes compose them again.
                _LOGGER.exception("items not recomposed")
        self.items_now()
        panel_server = self.panel_server()
        await panel_server.start()
        startup_delay = float(os.environ.get("OPS_STARTUP_DELAY_S", "600"))
        if self.scheduler is None:
            await self.alert_store_unavailable()
            await asyncio.gather(
                self.dispatcher.run(), self.liveness_loop(), self.degraded_schedule_loop(startup_delay)
            )
            return
        self.setup_schedule(startup_delay)
        if self.updates is not None:
            # An update run was interrupted (restart, the OS update's reboot): carry on, never re-install
            # blindly. Then confirm OS updates that waited for a reboot, and re-plan from what is installed.
            self.queue.put(Event("updates.resume", "startup", Priority.TRIAGE, {}, dedupe_key=UPDATE_TASK))
        assistant = self.assistant
        workers = [assistant.worker()] if assistant is not None else []
        if self.usage is not None:
            self.usage.store.close_stale_runs(at=datetime.now(UTC))
            await self.usage.ensure_switch()
        await asyncio.gather(
            self.scheduler.run(), self.dispatcher.run(), self.liveness_loop(), self.admin_loop(),
            self.event_stream().run(), *workers,
        )  # fmt: skip

    def event_stream(self) -> EventStream:
        """The one Core websocket: Companion action taps and, with the store, the AI kill switch."""
        usage = self.usage

        def is_switch(event: dict[str, Any]) -> bool:
            return usage is not None and isinstance(event.get("variables"), dict)

        async def subscribe(ws: HAWebSocket) -> None:
            await ws.command("subscribe_events", event_type=EVENT_TYPE)
            if usage is not None:
                await usage.subscribe_switch(ws)

        async def on_event(event: dict[str, Any]) -> None:
            if usage is not None and is_switch(event):
                await usage.on_switch_event(event)
            else:
                await self.inbound.on_event(event)

        return EventStream(
            self._ws_connect, (EVENT_TYPE,), on_event, subscribe=subscribe,
            accept=lambda event: event.get("event_type") == EVENT_TYPE or is_switch(event),
        )  # fmt: skip

    def panel_server(self, port: int = PANEL_PORT) -> PanelServer:
        return PanelServer(
            port=port,
            api=PanelHandlers(self),
            hub=self.hub,
            admin_user=lambda: self.inbound.admin.user_id if self.inbound.admin is not None else None,
            shell=PanelServer.load_shell(self.paths.panel_shell),
            scripts={"changes.js": PanelServer.load_shell(self.paths.panel_shell.with_name("changes.js")) or ""},
        )

    async def alert_store_unavailable(self) -> str:
        """Degraded start: tell the admin once, from the safety budget."""
        message = Message(
            "Ops Agent: agent degraded",
            "The Ops Agent cannot open its store (/data/ops.db) and is running without AI triage, "
            f"capability calls and inbound events. Health checks and safety alerts continue. Error: {self.store_error}",
            urgent=True,
            budget_class=BudgetClass.SAFETY,
            tag="ha-ops-degraded",
        )
        return await self.send(message, core_up=self.state.core_down_since is None, now=datetime.now(UTC))

    async def degraded_schedule_loop(self, startup_delay_s: float) -> None:
        """Without the store: the basic schedule (catch-up if stale, then daily) straight onto the queue."""
        last = self.state.last_health_at
        if last is None or datetime.now(UTC) - datetime.fromisoformat(last) > timedelta(hours=24):
            await asyncio.sleep(startup_delay_s)
            self.request_cycle("catch_up")
        while True:
            due = next_run_at(datetime.now(UTC), self.options.health.check_time, self.tz)
            await asyncio.sleep(max(1.0, (due - datetime.now(UTC)).total_seconds()))
            self.request_cycle("daily")
