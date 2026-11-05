"""The panel's JSON API on the daemon side (spec panel.md routes, N10-N19); `panel.py` checked admin and CSRF.

Every input is validated again by shape; the handlers call the same store and assistant methods as the push
channel, so the panel has no powers of its own (messages run as `admin-chat`).
"""

import asyncio
import base64
import binascii
import json
import logging
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opsd import __version__, diagnostics
from opsd.approvals import LIVE, ApprovalError, approvable
from opsd.budget import RUNS_PER_DAY
from opsd.changes import ChangeRefused, propose_fact
from opsd.conversation import clean_text
from opsd.items import ACTION_RE, ITEM_ID_RE, REMIND, ItemBook, thread_of
from opsd.panel import MAX_CHAT_CHARS
from opsd.policy import Origin, same_version
from opsd.runctx import Principal
from opsd.store import MAX_FACT_CHARS, Store, StoreError

if TYPE_CHECKING:
    from opsd.assistant import Assistant
    from opsd.daemon import Daemon

_LOGGER = logging.getLogger(__name__)

PAGE_TURNS = 30
PAGE_HISTORY = 50
HISTORY_FILTERS: dict[str, tuple[str, ...] | None] = {
    "all": None,
    "needs": None,
    "changes": ("proposal", "update", "reload"),
    "checks": ("check", "backup"),
    "questions": ("question",),
}
USER_ACK_DAYS = {"snooze": 7, "accept": 30}
# What a finding offers for the proposal its plan made (`items.Composer._linked`).
_LINKED_ACTIONS = frozenset({"approve_rebase", "approve_rebase_now", "apply", "revert", "reject"})
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
# What a conversation takes, under `api/items/<id>/<verb>`.
_CONVERSATION_POSTS = frozenset({"messages", "retry", "autoretry", "read", "cancel", "remember"})
_DIGITS_RE = re.compile(r"[0-9]{1,12}")
MAX_FACTS_PER_DAY = 50
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\+00:00)?")
_APPROVAL_RE = re.compile(r"[0-9a-f]{32}")
_RULE_RE = re.compile(r"[0-9a-f]{16}")

Result = tuple[int, dict[str, Any]]


def _bad(code: str, message: str = "") -> Result:
    return 400, {"error": code, "message": message or code}


def _int(value: object, *, maximum: int = 10**12) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 < value <= maximum:
        return value
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value) and 0 < int(value) <= maximum:
        return int(value)
    return None


def _cursor(at: str, ident: str) -> str:
    return base64.urlsafe_b64encode(f"{at}|{ident}".encode()).decode().rstrip("=")


def _uncursor(raw: str) -> tuple[str, str] | None:
    if not raw or len(raw) > 200:
        return None
    try:
        text = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    at, _, ident = text.partition("|")
    if not _ISO_RE.fullmatch(at) or not ITEM_ID_RE.fullmatch(ident):
        return None
    return at, ident


class PanelHandlers:
    def __init__(self, daemon: Daemon) -> None:
        self._d = daemon

    @property
    def _store(self) -> Store:
        store = self._d.store
        if store is None:
            raise StoreError("store unavailable")
        return store

    @property
    def _assistant(self) -> Assistant:
        assistant = self._d.assistant
        if assistant is None:
            raise StoreError("store unavailable")
        return assistant

    def thread_for(self, ident: str) -> str | None:
        """The conversation of the item `ident` (live events and presence): derived from the stored record, never
        named by the client. None for an unknown item."""
        try:
            found = self._conversation(ident)
        except StoreError, sqlite3.Error:
            return None
        return found[1] if found is not None else None

    def _conversation(self, ident: str) -> tuple[dict[str, Any], str] | None:
        """(the item, its thread): an item's own conversation or a question's. None for an unknown item or a thread
        the store does not have."""
        store, book = self._d.store, self._d.items
        if store is None or book is None or not ITEM_ID_RE.fullmatch(ident):
            return None
        record = book.get(ident)
        if record is None:
            return None
        thread = thread_of(record)
        if thread is None or store.get_thread(thread) is None:
            return None
        return record, thread

    async def handle(
        self, method: str, parts: tuple[str, ...], query: Mapping[str, str], body: dict[str, Any] | None, user: str
    ) -> Result:
        try:
            return await self._route(method, parts, query, body or {}, user)
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("panel API store error", extra={"error": str(err)[:200], "route": "/".join(parts)[:60]})
            return 503, {"error": "store_unavailable"}

    async def _route(
        self, method: str, parts: tuple[str, ...], query: Mapping[str, str], body: dict[str, Any], user: str
    ) -> Result:
        match (method, parts):
            case ("GET", ("state",)):
                return 200, self.state()
            case ("GET", ("ops",)):
                return 200, self.ops()
            case ("POST", ("ai",)):
                return await self.ai(body, user)
            case ("POST", ("run",)):
                return 200, {"started": self._d.request_cycle("manual")}
            case ("POST", ("selftest",)):
                return 200, {"result": (await self._d.run_selftest()).to_json()}
            case ("GET", ("updates",)):
                return self.updates_view()
            case ("POST", ("updates", "auto")):
                return await self.updates_auto(body, user)
            case ("GET", ("suppressions",)):
                return self.suppressions()
            case ("POST", ("suppressions", rule_id, "revoke")):
                return await self.revoke_suppression(rule_id, user)
            case ("POST", ("backups", "now")):
                return await self.backup_now(user)
            case ("GET", ("home",)):
                return self.home()
            case ("GET", ("items", ident)):
                return self.item(ident)
            case ("POST", ("items", ident, "act")):
                return await self.item_act(ident, body, user)
            case ("GET", ("history",)):
                return self.history(query)
            case ("GET", ("diagnostics",)):
                return 200, diagnostics.build(self._d)
        if self._d.store is None or self._d.assistant is None or self._d.items is None:
            return 503, {"error": "store_unavailable"}
        match (method, parts):
            case ("POST", ("ask",)):
                return await self.ask(body, user)
            case ("GET", ("items", ident, "thread")):
                return self.thread(ident, query)
            case ("POST", ("items", ident, verb)) if verb in _CONVERSATION_POSTS:
                found = self._conversation(ident)
                if found is None:
                    return 404, {"error": "no_such_item"}
                return await self._conversation_post(verb, found, body, user)
        return 404, {"error": "not_found"}

    # ---------------------------------------------------------------- header

    def state(self) -> dict[str, Any]:
        d = self._d
        now = datetime.now(UTC)
        control = d.control.snapshot(now)
        view = d.usage.view(now) if d.usage is not None else {}
        current = d.runner.current
        queued = d.assistant.queued if d.assistant is not None else 0
        ai_state = str(control.get("ai_state"))
        if ai_state == "ai_disabled":
            status = "off"
        elif ai_state != "enabled":
            status = "paused"
        elif d.runner.running:
            status = "working"
        elif queued or d.runner.busy:
            status = "waiting"
        else:
            status = "ready"
        chat = (view.get("profiles_today") or {}).get("chat") or {}
        return {
            "status": status,
            "ai": {"on": control.get("ai_switch") == "on", "state": ai_state,
                   "backoff_until": control.get("ai_backoff_until"), "credential": d.credential_kind()},
            "usage": {k: view.get(k) for k in ("run_time_5h_s", "run_time_5h_cap_s", "run_time_7d_s",
                                               "run_time_7d_cap_s", "account_5h_percent", "account_7d_percent",
                                               "cost_today_usd", "daily_max_usd", "today", "last_run")},
            "run": {"profile": current.profile, "thread": current.thread_id} if current is not None else None,
            "queue": queued,
            "chat": {"used": int(chat.get("runs") or 0), "cap": RUNS_PER_DAY["chat"]},
            "options": d.options_notice(),
            "held": d.held_view(now),
            # The house's time zone: the panel shows task times in it, whatever the browser's zone.
            "tz": str(d.tz),
            "version": __version__,
            "store": d.store is not None,
            # For the System sheet; there is no digest time (by design: no routine pushes).
            "quiet": {"from": d.options.notify.quiet_start or None, "to": d.options.notify.quiet_end or None},
            "mode": d.options.agent.mode,
            "updates": {"auto": d.updates.control().get("effective") if d.updates is not None else False,
                        "window": d.options.updates.window},
            "needs_count": self._needs_count(),
        }  # fmt: skip

    def _needs_count(self) -> int | None:
        try:
            return self._d.store.needs_count() if self._d.store is not None else None
        except StoreError, sqlite3.Error:
            return None

    def ops(self) -> dict[str, Any]:
        """The System sheet's status and house profile."""
        return {"status": self._d.status_view(), "profile": self._d.profile_view()}

    async def _run(self, kind: str, args: dict[str, Any], user: str) -> Result:
        """A high-impact action: the admin's one panel request (ingress admin header and CSRF token) runs it."""
        try:
            done = await self._d.panel_action(kind, args, user)
        except Exception:
            _LOGGER.exception("panel action failed", extra={"kind": kind})
            done = False
        self._audit_action(kind, args, user, "done" if done else "failed")
        self._d.hub.publish("state", None, {})
        if not done:
            return 409, {"error": "not_done", "message": "Not done: it changed or expired meanwhile."}
        return 200, {"ok": True, "state": self.state()}

    def _audit_action(self, kind: str, args: dict[str, Any], user: str, outcome: str) -> None:
        try:
            self._store.audit(at=datetime.now(UTC), run_id=None, principal=f"panel:{user[:64]}", channel="panel",
                              capability=f"panel.{kind}", args=args, tier=None, decision="admin",
                              outcome=outcome)  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    async def ai(self, body: dict[str, Any], user: str) -> Result:
        on = body.get("on")
        if not isinstance(on, bool):
            return _bad("bad_ai", "`on` must be true or false")
        if self._d.usage is None:
            return 503, {"error": "store_unavailable"}
        if on:
            return await self._run("ai_on", {}, user)
        await self._d.panel_ai(False, user)
        return 200, {"ok": True, "state": self.state()}

    def updates_view(self) -> Result:
        """The System sheet's automatic-updates switch and backup row (the updates themselves are items)."""
        if self._d.updates is None:
            return 503, {"error": "store_unavailable"}
        backups = self._d.backups
        return 200, {"control": self._d.updates.control(), "backup": backups.view() if backups is not None else None}

    def suppressions(self) -> Result:
        """Known-false-positive rules with their state, the recent decisions and the file's problems."""
        d = self._d
        if not d.options.profile.overlay("suppress"):
            return 200, {"rules": [], "problems": [], "max": 0}
        from opsd.house import suppress  # noqa: PLC0415 - a house overlay, loaded only when the profile enables it

        rules, problems = d.suppression_rules()
        revoked = d.suppression_revoked()
        today = datetime.now(UTC).astimezone(d.tz).date()
        return 200, {"rules": [r.view(today, r.id in revoked) for r in rules], "problems": problems,
                     "max": suppress.MAX_RULES}  # fmt: skip

    async def revoke_suppression(self, rule_id: str, user: str) -> Result:
        """Stop a rule at once (the safe direction: the item is reported again); a learned rule's
        removal from docs/known-false-positives.yaml is proposed for the admin to approve."""
        d = self._d
        if not _RULE_RE.fullmatch(rule_id) or d.store is None or not d.options.profile.overlay("suppress"):
            return 404, {"error": "not_found"}
        from opsd.house import suppress  # noqa: PLC0415 - a house overlay

        rules, _ = d.suppression_rules()
        rule = next((r for r in rules if r.id == rule_id), None)
        if rule is None:
            return 404, {"error": "not_found"}
        now = datetime.now(UTC)
        revoked = sorted(d.suppression_revoked() | {rule_id})
        d.store.kv_set(suppress.REVOKED_KEY, json.dumps(revoked), now=now)
        d.store.audit(at=now, run_id=None, principal=f"admin:{user[:64]}", channel="panel",
                      capability="suppress.revoke", args={"key": rule.key, "rule": rule_id}, tier=None,
                      decision="admin", outcome="revoked")  # fmt: skip
        proposal = None
        if not rule.built_in and d.changes is not None and d.repo is not None:
            try:
                found = await asyncio.to_thread(
                    suppress.propose_suppression, d.store, d.repo, d.options, rule=None, remove_id=rule_id,
                    why=f"Revoked in the panel: {rule.key}", requested_by=f"panel:{user[:64]}",
                    day=d.day(now), now=now,
                )  # fmt: skip
                proposal = found["id"]
            except ChangeRefused as err:
                _LOGGER.warning("revocation proposal refused", extra={"error": str(err)[:200]})
        d.hub.publish("changes", None, {})
        return 200, {"ok": True, "proposal": proposal}

    async def backup_now(self, user: str) -> Result:
        """ "Back up now": a high-impact action (`_run`), at most one start an hour, one at a time."""
        backups = self._d.backups
        if backups is None:
            return 503, {"error": "store_unavailable"}
        if backups.running:
            return 409, {"error": "backup_running", "message": "A backup is already running."}
        refusal = backups.manual_refusal()
        if refusal is not None:
            return 429, {"error": "backup_rate", "message": refusal}
        return await self._run("backup_now", {}, user)

    async def updates_auto(self, body: dict[str, Any], user: str) -> Result:
        """Pausing is immediate; resuming is high-impact (see `_run`)."""
        on = body.get("on")
        if not isinstance(on, bool):
            return _bad("bad_updates", "`on` must be true or false")
        updates = self._d.updates
        if updates is None:
            return 503, {"error": "store_unavailable"}
        if not on:
            await self._d.panel_updates(False, user)
            return 200, {"ok": True, "control": updates.control()}
        if not self._d.options.auto_updates_on:
            return 409, {"error": "option_off", "message": "Turn on updates.auto_install in the add-on options first."}
        if not updates.paused() and updates.failed_block() is None:
            return 200, {"ok": True, "control": updates.control()}
        return await self._run("updates_on", {}, user)

    # ---------------------------------------------------------------- items

    @property
    def _book(self) -> ItemBook:
        book = self._d.items
        if book is None:
            raise StoreError("store unavailable")
        return book

    def _local(self, value: Any) -> str:
        try:
            return datetime.fromisoformat(str(value)).astimezone(self._d.tz).strftime("%H:%M")
        except ValueError, TypeError:
            return "?"

    def home(self) -> Result:
        """`api/home`: the sections, the badge (the same table, the same moment) and the status strip."""
        d, book = self._d, self._book
        sections = book.home()
        count = self._store.needs_count()
        state = d.state
        checked = f"checked {self._local(state.last_health_at)}" if state.last_health_at else "no check yet"
        health_open = sum(1 for r in sections["needs"] if r["kind"] == "check")
        calm = None
        if not sections["needs"]:
            result = "nothing new since the baseline" if state.last_result == "ok" else state.last_result
            calm = (
                f"Checked at {self._local(state.last_health_at)}, {result}. Nothing needs you."
                if state.last_health_at
                else "No health check yet. Nothing needs you."
            )
        last = (d.backups.view().get("last") or {}) if d.backups is not None else {}
        if last.get("state") == "ok":
            backup = f"{self._local(last.get('finished_at'))}, encrypted"
        elif last.get("state") == "failed":
            backup = f"failed {self._local(last.get('finished_at'))}"
        else:
            backup = "Home Assistant's automatic backups"
        control = d.updates.control() if d.updates is not None else {}
        window = (
            f"tonight {control.get('window')}"
            if control.get("effective")
            else f"report only: {control.get('reason') or 'unavailable'}"
        )
        view = d.usage.view(datetime.now(UTC)) if d.usage is not None else {}
        used, cap = view.get("run_time_7d_s"), view.get("run_time_7d_cap_s")
        ai_on = d.control.snapshot(datetime.now(UTC)).get("ai_switch") == "on"
        budget = f", {round(100 * used / cap)} % of 7-day run time" if isinstance(used, (int, float)) and cap else ""
        ai = ("on" if ai_on else "off") + budget
        health = f"{checked}, " + (f"{health_open} open" if health_open else "nothing new")
        strip = [{"label": "Health", "value": health},
                 {"label": "Backup", "value": backup}, {"label": "Next window", "value": window},
                 {"label": "AI", "value": ai}]  # fmt: skip
        return 200, {
            "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
            "needs_count": count,
            "sections": {name: [book.view(r) for r in rows] for name, rows in sections.items()},
            "calm": calm,
            "strip": strip,
        }

    def item(self, ident: str) -> Result:
        found = self._book.get(ident)
        if found is None:
            return 404, {"error": "no_such_item"}
        return 200, {"item": self._book.view(found, events=True)}

    def history(self, query: Mapping[str, str]) -> Result:
        """`api/history`: newest first, 50 a page; `counts.needs` is the badge."""
        name = query.get("filter", "all")
        if name not in HISTORY_FILTERS:
            return _bad("bad_filter")
        raw = query.get("before")
        before = _uncursor(raw) if raw is not None else None
        if raw is not None and before is None:
            return _bad("bad_cursor")
        rows = self._store.items_page(
            kinds=HISTORY_FILTERS[name], needs_only=name == "needs", before=before, limit=PAGE_HISTORY + 1
        )
        more = len(rows) > PAGE_HISTORY
        rows = rows[:PAGE_HISTORY]
        cursor = _cursor(str(rows[-1]["at"]), str(rows[-1]["id"])) if more and rows else None
        return 200, {"items": [self._book.view(r) for r in rows], "counts": {"needs": self._store.needs_count()},
                     "more": more, "cursor": cursor}  # fmt: skip

    async def item_act(self, ident: str, body: dict[str, Any], user: str) -> Result:
        """`api/items/<id>/act`: one offered action, for the revision the admin saw; the daemon re-checks it all."""
        book = self._book
        found = book.get(ident)
        if found is None:
            return 404, {"error": "no_such_item"}
        action, revision = body.get("action"), body.get("revision")
        if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
            return _bad("bad_action")
        if isinstance(revision, bool) or not isinstance(revision, int):
            return _bad("bad_revision")
        if revision != int(found["revision"]):
            return 409, {"error": "revision", "message": f"The plan changed: {book.changed_since(found, revision)}",
                         "item": book.view(found, events=True)}  # fmt: skip
        offered = book.offered(found)
        if action not in offered:
            return 409, {"error": "not_available", "message": "That action is not offered any more.",
                         "item": book.view(found, events=True)}  # fmt: skip
        try:
            status, result = await self._act(found, action, user)
        except ApprovalError as err:
            status, result = 409, {"error": "not_available", "message": str(err)}
        self._audit_item(found, action, user, status)
        fresh = book.get(ident) or found
        if status == 200:
            return status, {k: v for k, v in result.items() if k == "message"} | {"item": book.view(fresh, events=True)}
        if result.get("error") == "not_done":
            result = {"error": "not_available", "message": result.get("message")}
        return status, {k: v for k, v in result.items() if k in ("error", "message")} | {
            "item": book.view(fresh, events=True)
        }

    def _audit_item(self, found: Mapping[str, Any], action: str, user: str, status: int) -> None:
        try:
            self._store.audit(at=datetime.now(UTC), run_id=None, principal=f"admin:{user[:64]}", channel="panel",
                              capability="item.act", args={"item": found["id"], "action": action,
                                                           "revision": found["revision"]},
                              tier=None, decision="admin", outcome=str(status))  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    async def _act(self, found: Mapping[str, Any], action: str, user: str) -> Result:
        """One offered action, already bound to the revision the admin saw (`item_act`)."""
        d, book = self._d, self._book
        source = found.get("source") or {}
        key = str(found["key"])
        now = datetime.now(UTC)
        if action == "ack":
            return (
                (200, {})
                if book.ack(found, now=now)
                else (409, {"error": "not_available", "message": "Nothing to acknowledge."})
            )
        if action == "resume_updates":
            return await self._run("updates_on", {}, user)
        if action == "withdraw":
            return self._withdraw(found, user)
        if key.startswith("updates:"):
            return await self._act_update(found, action, user)
        if key.startswith("change:"):
            change_id = int(source.get("change") or 0)
            if action == "reject":
                done = d.changes is not None and d.changes.reject(change_id, user)
                return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})
            if d.changes is None:
                return 503, {"error": "store_unavailable"}
            shown = [str(x) for x in (found.get("change") or {}).get("shown") or []]
            approval = await d.changes.approve(change_id, "apply" if action == "apply" else "revert", user, shown)
            return await self._run("approve_change", {"approval": approval["id"]}, user)
        if key.startswith("maint:"):
            return await self._act_maintenance(found, action, user)
        if key.startswith("fork:"):
            return await self._act_fork(found, action, user)
        if key.startswith("fact:") and action == "dismiss":
            done = self._store.decide_fact(int(source.get("fact") or 0), "rejected", now=now)
            self._d.hub.publish("facts", None, {})
            return (200, {}) if done else (409, {"error": "not_available", "message": "Not pending any more."})
        if key.startswith("health:"):
            health = str(source.get("health") or "")
            mode = "accept" if action == "accept" else "snooze"
            done = await d.user_ack(health, days=USER_ACK_DAYS[mode], accept=mode == "accept")
            return (200, {}) if done else (409, {"error": "not_available", "message": "No longer open."})
        if key.startswith("task:"):
            task_id = int(source.get("task") or 0)
            if action == "stop" and d.assistant is not None and isinstance(source.get("thread"), str):
                done = d.assistant.cancel_investigation(str(source["thread"]), task_id) == "ok"
            else:
                done = self._store.admin_cancel_task(task_id, now=now)
            self._d.hub.publish("tasks", None, {})
            return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})
        return 409, {"error": "not_available", "message": "That action is not offered any more."}

    async def _act_update(self, found: Mapping[str, Any], action: str, user: str) -> Result:
        d = self._d
        updates = d.updates
        if updates is None:
            return 503, {"error": "store_unavailable"}
        source = found.get("source") or {}
        entity, version = str(source.get("entity")), str(source.get("version"))
        if action == "reboot_window":
            approval = updates.approve_reboot(int(source.get("job") or 0), user=user)
            return await self._run("approve_reboot", {"approval": approval["id"]}, user)
        latest = next(iter(self._store.recent_update_runs(limit=1)), None)
        item = next((i for i in (latest or {}).get("plan", []) if i.get("entity_id") == entity
                     and same_version(i.get("latest"), version)), None)  # fmt: skip
        if latest is None or item is None or not approvable(item):
            return 409, {"error": "not_available", "message": "This update cannot be approved now."}
        run = int(latest["id"])
        if action == "reject":
            updates.approvals.reject_update(item, plan_run=run, user=user)
            self._d.hub.publish("updates", None, {})
            return 200, {}
        when = "now" if action == "approve_now" else "window"
        approval = await updates.approve(item, plan_run=run, user=user, when=when)
        return await self._run("approve_update", {"approval": approval["id"]}, user)

    async def _act_maintenance(self, found: Mapping[str, Any], action: str, user: str) -> Result:
        """A maintenance item: plan a fix (an investigation on the item itself), stop it, a reminder, hide it until
        it changes; and the actions of a proposal its plan made (`_act_linked`)."""
        d = self._d
        manager = d.maintenance
        source = found.get("source") or {}
        finding = str(source.get("finding") or "")
        if manager is None or not finding:
            return 503, {"error": "store_unavailable"}
        linked = source.get("linked") if isinstance(source.get("linked"), dict) else {}
        if action in _LINKED_ACTIONS and linked:
            # A proposal the finding's plan made is decided here: the same checks as on its own item, bound to the
            # revision and the diff hash the admin saw on the finding.
            return await self._act_linked(found, linked, action, user)
        if action in ("stop", "cancel"):
            # Stop a running plan, or cancel one that waits for a cap to lift.
            thread = thread_of(found)
            task = source.get("plan_task")
            if d.assistant is None or thread is None or not isinstance(task, int):
                return 409, {"error": "not_available", "message": "Nothing is being planned."}
            done = d.assistant.cancel_investigation(thread, task) == "ok"
            self._d.hub.publish("tasks", None, {})
            return (200, {}) if done else (409, {"error": "not_available", "message": "Not running any more."})
        if action == "plan_fix" or action in REMIND:
            if d.assistant is None:
                return 503, {"error": "store_unavailable"}
            if action == "plan_fix":
                outcome = d.assistant.plan_fix(found, user=user)
                message = "Planning a fix here. You get a push when it is ready."
            else:
                at = self._remind_at(action, finding)
                if at is None:
                    return 409, {"error": "not_available", "message": "This item has no deadline to be reminded of."}
                outcome = d.assistant.remind_about(found, at=at, user=user)
                message = f"Reminder set for {at.astimezone(d.tz):%Y-%m-%d}."
            return (200, {"message": message}) if outcome == "ok" else (409, {"error": "not_available",
                                                                                "message": outcome})  # fmt: skip
        if action == "dismiss":
            done = manager.hide(finding)
        elif action == "reopen":
            # The admin overrides the agent's "not affected": the finding behaves as without a verdict.
            done = manager.reopen(finding)
        else:
            done = False
        return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})

    async def _act_linked(self, found: Mapping[str, Any], linked: Mapping[str, Any], action: str,
                          user: str) -> Result:  # fmt: skip
        """An action on the proposal folded into a finding, run as that proposal's own action."""
        source = dict(found.get("source") or {})
        if isinstance(linked.get("fork"), str):
            return await self._act_fork(dict(found) | {"source": source | {"fork": linked["fork"]}}, action, user)
        change_id = linked.get("change")
        if not isinstance(change_id, int) or action not in ("apply", "revert", "reject"):
            return 409, {"error": "not_available", "message": "That action is not offered any more."}
        d = self._d
        if action == "reject":
            done = d.changes is not None and d.changes.reject(change_id, user)
            return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})
        if d.changes is None:
            return 503, {"error": "store_unavailable"}
        shown = [str(x) for x in (found.get("change") or {}).get("shown") or []]
        approval = await d.changes.approve(change_id, action, user, shown)
        return await self._run("approve_change", {"approval": approval["id"]}, user)

    async def _act_fork(self, found: Mapping[str, Any], action: str, user: str) -> Result:
        """A fork rebase proposal: approve it for the update window or now (the daemon re-checks the files on disk
        and the live Core version against what the admin saw), or reject it."""
        d = self._d
        proposal = str((found.get("source") or {}).get("fork") or "")
        if action == "reject":
            done = d.reject_fork(proposal, user)
            return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})
        if action not in ("approve_rebase", "approve_rebase_now"):
            return 409, {"error": "not_available", "message": "That action is not offered any more."}
        shown = [str(x) for x in (found.get("change") or {}).get("shown") or []]
        when = "now" if action == "approve_rebase_now" else "window"
        approval = await d.approve_fork(proposal, user, shown, when)
        return await self._run("approve_fork", {"approval": approval["id"]}, user)

    def _remind_at(self, action: str, finding: str) -> datetime | None:
        """When a maintenance reminder fires: in 30 or 90 days, or 30 days before the finding's deadline."""
        now = datetime.now(UTC)
        days = REMIND[action]
        if days is not None:
            return now + timedelta(days=days)
        manager = self._d.maintenance
        found = next((f for f in manager.findings() if f.key == finding), None) if manager is not None else None
        start = found.asks_at() if found is not None else None
        return start if start is not None and start > now else None

    def _withdraw(self, found: Mapping[str, Any], user: str) -> Result:
        """Withdraw the approval an item holds (always safe, one step)."""
        d = self._d
        approval = (found.get("source") or {}).get("approval")
        withdrawn = isinstance(approval, str) and _APPROVAL_RE.fullmatch(approval) is not None and (
            self._store.decide_approval(approval, expect=LIVE, to="void", now=datetime.now(UTC), by=user,
                                        detail="withdrawn by you"))  # fmt: skip
        if withdrawn:
            d.hub.publish("updates", None, {})
            d.hub.publish("changes", None, {})
            return 200, {}
        return 409, {"error": "not_pending", "message": "Nothing is waiting to be withdrawn."}

    # --------------------------------------------------------- conversations

    def thread(self, ident: str, query: Mapping[str, str]) -> Result:
        """`api/items/<id>/thread`: the item's conversation, a page of turns (oldest first)."""
        found = self._conversation(ident)
        if found is None:
            return 404, {"error": "no_such_item"}
        _record, thread = found
        before = query.get("before")
        before_id = _int(before) if before is not None else None
        if before is not None and before_id is None:
            return _bad("bad_cursor")
        turns = self._store.turns_page(thread, before=before_id, limit=PAGE_TURNS)
        more = bool(turns) and bool(self._store.turns_page(thread, before=turns[0].id, limit=1))
        return 200, {
            "thread": self._store.get_thread(thread),
            "turns": [t.to_json() for t in turns],
            "more": more,
            "in_flight": self._assistant.live_status(thread),
        }

    async def _conversation_post(self, verb: str, found: tuple[dict[str, Any], str], body: dict[str, Any],
                                 user: str) -> Result:  # fmt: skip
        record, thread = found
        if verb == "read":
            return self.read(thread, body)
        match verb:
            case "messages":
                return await self.message(record, thread, body, user)
            case "retry":
                return self.retry(thread, body)
            case "autoretry":
                return self.autoretry(thread, body)
            case "cancel":
                return self.cancel_investigation(thread, body)
        return await self.remember(thread, body, user)

    @staticmethod
    def _refuse_message(body: dict[str, Any]) -> Result | None:
        """Why a message body is refused (empty, too long, no client id), or None."""
        text = body.get("text")
        client_id = body.get("client_id")
        if not isinstance(text, str) or not text.strip():
            return _bad("empty", "Type a message first.")
        if len(text) > MAX_CHAT_CHARS:
            return 413, {"error": "too_long", "message": f"At most {MAX_CHAT_CHARS} characters."}
        if not isinstance(client_id, str) or not _CLIENT_ID_RE.fullmatch(client_id):
            return _bad("bad_client_id")
        return None

    async def message(self, record: dict[str, Any], thread: str, body: dict[str, Any], user: str) -> Result:
        """A message in an item's (or a question's) conversation; the run gets that item's record, fenced. The
        thread and the item come from the stored record, never from the request."""
        refused = self._refuse_message(body)
        if refused is not None:
            return refused
        text, client_id = str(body["text"]), str(body["client_id"])
        principal = Principal(Origin.ADMIN_CHAT, user_id=user, channel="panel")
        sent = await self._assistant.submit(thread, text, principal=principal, channel="panel", client_id=client_id,
                                            item=record)  # fmt: skip
        return self._sent(sent.result, sent.turn.to_json() if sent.turn is not None else None, sent.position)

    @staticmethod
    def _sent(result: str, turn: dict[str, Any] | None, position: int) -> Result:
        match result:
            case "queued":
                return 202, {"turn": turn, "state": "queued", "position": position}
            case "duplicate":
                return 200, {"turn": turn, "state": turn["status"] if turn else "unknown", "duplicate": True}
            case "in_flight":
                return 409, {"error": "in_flight", "message": "Wait for the answer to your last message.", "turn": turn}
            case "refused":
                return 200, {"turn": turn, "state": "refused"}
            case "client_id_used":
                return 409, {"error": "client_id_used", "message": "That message id was used for another message;"
                             " send it again."}  # fmt: skip
        return 503, {"error": "busy", "message": "Not sent: the agent is busy.", "turn": turn}

    async def ask(self, body: dict[str, Any], user: str) -> Result:
        """`api/ask`: a question about anything without an item. It becomes an item of its own (Running, then Done
        with the answer) and the panel opens it at once."""
        refused = self._refuse_message(body)
        if refused is not None:
            return refused
        text, client_id = str(body["text"]), str(body["client_id"])
        principal = Principal(Origin.ADMIN_CHAT, user_id=user, channel="panel")
        record, sent = await self._assistant.ask(text, principal=principal, channel="panel", client_id=client_id)
        if record is None and sent.result == "client_id_used":
            return self._sent(sent.result, None, 0)
        if record is None:
            return 503, {"error": "busy", "message": "Not sent: the question could not be stored."}
        status, result = self._sent(sent.result, sent.turn.to_json() if sent.turn is not None else None,
                                    sent.position)  # fmt: skip
        return status, result | {"item": self._book.view(record, events=True)}

    def _turn_id(self, body: dict[str, Any]) -> int | None:
        return _int(body.get("turn_id"))

    def retry(self, thread: str, body: dict[str, Any]) -> Result:
        turn_id = self._turn_id(body)
        if turn_id is None:
            return _bad("bad_turn")
        result = self._assistant.retry_turn(thread, turn_id)
        status = {"queued": 202, "not_found": 404, "not_retryable": 409, "cap": 429, "busy": 503}[result]
        return status, {"result": result} if status == 202 else {"error": result}

    def autoretry(self, thread: str, body: dict[str, Any]) -> Result:
        turn_id = self._turn_id(body)
        on = body.get("on")
        if turn_id is None or not isinstance(on, bool):
            return _bad("bad_autoretry")
        result = self._assistant.set_auto_retry(thread, turn_id, on=on)
        status = {"ok": 200, "not_found": 404, "not_retryable": 409}[result]
        return status, {"result": result} if status == 200 else {"error": result}

    def read(self, thread: str, body: dict[str, Any]) -> Result:
        upto = body.get("turn_id")
        upto_id = _int(upto) if upto is not None else None
        if upto is not None and upto_id is None:
            return _bad("bad_turn")
        if self._store.mark_read(thread, upto=upto_id):
            self._assistant.thread_changed(thread)
        return 200, {"ok": True}

    def cancel_investigation(self, thread: str, body: dict[str, Any]) -> Result:
        """Stop an investigation in this conversation (one step: stopping is the safe direction)."""
        task_id = _int(body.get("task_id"))
        if task_id is None:
            return _bad("bad_task")
        result = self._assistant.cancel_investigation(thread, task_id)
        status = {"ok": 200, "not_found": 404, "not_open": 409}[result]
        return status, {"ok": True} if status == 200 else {"error": result}

    async def remember(self, thread: str, body: dict[str, Any], user: str) -> Result:
        """A house fact, proposed as a git change to the house facts document (applied once the admin approves it).

        Without git or a facts document (house profile) the fact is kept in the store and the proposal is refused.
        """
        turn_id = self._turn_id(body)
        raw = body.get("text")
        turn = self._store.get_turn(turn_id) if turn_id is not None else None
        if turn is None or turn.thread_id != thread or turn.role not in ("admin", "agent"):
            return 404, {"error": "no_such_turn"}
        if not isinstance(raw, str) or len(raw) > MAX_FACT_CHARS:
            return 413, {"error": "too_long", "message": f"At most {MAX_FACT_CHARS} characters."}
        text = clean_text(raw, MAX_FACT_CHARS)
        if not text:
            return _bad("empty", "Write the fact to remember.")
        now = datetime.now(UTC)
        if not self._store.bump(self._d.day(now), "fact_proposals", limit=MAX_FACTS_PER_DAY):
            return 429, {"error": "fact_cap", "message": "Too many proposals today."}
        fact_id = self._store.add_fact(text, thread_id=thread, turn_id=turn.id, now=now)
        self._assistant.audit("fact.propose", "admin", args={"fact": fact_id, "thread": thread, "turn": turn.id},
                              outcome="ok")  # fmt: skip
        change, refused = None, None
        repo = self._d.repo
        try:
            if repo is None:
                raise ChangeRefused("git proposals are off on this install, so the fact is not written to a file")
            found = await asyncio.to_thread(
                propose_fact,
                self._store,
                repo,
                self._d.options,
                fact=text,
                why="Remembered from a chat in the panel",
                requested_by=f"panel:{user[:64]}",
                day=self._d.day(now),
                now=now,
                fact_id=fact_id,
                thread_id=thread,
            )
            change = found["id"]
        except ChangeRefused as err:
            refused = str(err)
        self._assistant.publish("facts", None, {})
        self._d.hub.publish("changes", None, {})
        return 201, {"id": fact_id, "change": change, "refused": refused}

    # ----------------------------------------------------------- reminders
