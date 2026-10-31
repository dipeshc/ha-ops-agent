"""The panel's JSON API, on the daemon side (`panel.py` has already checked the admin and CSRF).

Every input is validated here again by shape (ids, lengths, enums); the
handlers call the same store and assistant methods the push channel uses,
so the panel has no powers of its own: messages run as `admin-chat`.
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

from opsd import __version__, diagnostics, proposals
from opsd.approvals import LIVE, ApprovalError, approvable
from opsd.changes import ChangeRefused, propose_fact
from opsd.conversation import CHAT_TURNS_PER_DAY
from opsd.items import ACTION_RE, ITEM_ID_RE, ItemBook
from opsd.panel import MAX_CHAT_CHARS, THREAD_RE
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
}
USER_ACK_DAYS = {"snooze": 7, "accept": 30}
USAGE_DAYS = timedelta(days=7)
MAX_CHART_RUNS = 2000
FIVE_HOURS = timedelta(hours=5)
WEEK = timedelta(days=7)
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_DIGITS_RE = re.compile(r"[0-9]{1,12}")
_CONTROL_KEEP_NL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
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

    def thread_exists(self, thread_id: str) -> bool:
        store = self._d.store
        try:
            return (
                store is not None
                and THREAD_RE.fullmatch(thread_id) is not None
                and (store.get_thread(thread_id) is not None)
            )
        except StoreError, sqlite3.Error:
            return False

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
            case ("GET", ("usage",)):
                return 200, self.usage_history()
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
        if self._d.store is None or self._d.assistant is None:
            return 503, {"error": "store_unavailable"}
        match (method, parts):
            case ("GET", ("chat",)):
                return self.chat()
            case ("GET", ("threads", thread)):
                return self.thread(thread, query)
            case ("POST", ("threads", thread, "messages")):
                return await self.message(thread, body, user)
            case ("POST", ("threads", thread, "retry")):
                return self.retry(thread, body)
            case ("POST", ("threads", thread, "autoretry")):
                return self.autoretry(thread, body)
            case ("POST", ("threads", thread, "read")):
                return self.read(thread, body)
            case ("POST", ("threads", thread, "cancel")):
                return self.cancel_investigation(thread, body)
            case ("POST", ("threads", thread, "remember")):
                return await self.remember(thread, body, user)
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
        try:
            used = d.store.count(d.day(now), "chat_turns") if d.store is not None else 0
        except StoreError, sqlite3.Error:
            used = 0
        return {
            "status": status,
            "ai": {"on": control.get("ai_switch") == "on", "state": ai_state,
                   "backoff_until": control.get("ai_backoff_until")},
            "usage": {k: view.get(k) for k in ("agent_share_5h_percent", "agent_share_7d_percent",
                                               "agent_share_5h_estimated", "agent_share_7d_estimated",
                                               "max_share_percent", "share_stop_percent", "account_5h_percent",
                                               "account_7d_percent",
                                               "today", "last_run", "run_time_5h_s", "run_time_5h_cap_s")},
            "run": {"profile": current.profile, "thread": current.thread_id} if current is not None else None,
            "queue": queued,
            "chat": {"used": used, "cap": CHAT_TURNS_PER_DAY,
                     "thread": d.assistant.main_thread() if d.assistant else None},
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

    def _item_mark(self, key: str) -> dict[str, Any]:
        """For the older views: the item a record belongs to, and whether it is in Needs you (read, not inferred)."""
        found = self._d.store.item_by_key(key) if self._d.store is not None else None
        if found is None:
            return {"item": None, "item_state": None, "needs": False}
        return {"item": found["id"], "item_state": found["state"], "needs": bool(found["needs"])}

    def ops(self) -> dict[str, Any]:
        d = self._d
        # Item details carry raw log samples; the panel shows them, so they get the same redaction as pushes.
        redactor = d.redactor()
        return {
            "items": [redactor.redact_obj(i.to_json()) | self._item_mark(f"health:{i.key}") for i in d.open_items()],
            "needs_count": self._needs_count(),
            "history": list(reversed(d.history()[-30:])),
            "status": d.status_view(),
            "profile": d.profile_view(),
        }

    def usage_history(self) -> dict[str, Any]:
        """Agent share of the rolling 5-hour and 7-day windows, hourly over the last 7 days (for the chart).

        169 points over at most `MAX_CHART_RUNS` runs (two weeks of a few dozen runs a day in practice):
        a plain loop is cheaper than anything clever at this size, and the ceiling bounds it.
        """
        d = self._d
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        view = d.usage.view(datetime.now(UTC)) if d.usage is not None else {}
        runs = d.store.run_shares(since=now - USAGE_DAYS - WEEK)[-MAX_CHART_RUNS:] if d.store is not None else []
        points = []
        for step in range(USAGE_DAYS // timedelta(hours=1) + 1):
            at = now - USAGE_DAYS + timedelta(hours=step)
            s5 = sum(r[1] for r in runs if at - FIVE_HOURS < r[0] <= at)
            s7 = sum(r[2] for r in runs if at - WEEK < r[0] <= at)
            points.append({"t": at.isoformat(timespec="seconds"), "s5": round(100 * s5, 3), "s7": round(100 * s7, 3)})
        return {
            "points": points,
            "max_share_percent": view.get("max_share_percent"),
            "share_stop_percent": view.get("share_stop_percent"),
            "today": view.get("today"),
        }

    async def _run(self, kind: str, args: dict[str, Any], user: str) -> Result:
        """A high-impact action: the admin's one panel request (ingress admin header and CSRF token) runs it.

        Approvals stay bound to the exact plan item, its hash and the item revision the admin saw (a phone
        confirmation would add no second factor, so there is none).
        """
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
        share = view.get("agent_share_7d_percent")
        ai_on = d.control.snapshot(datetime.now(UTC)).get("ai_switch") == "on"
        ai = ("on" if ai_on else "off") + (f", {share} % of 7-day budget" if share is not None else "")
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
        rows = self._store.items_page(kinds=HISTORY_FILTERS[name], needs_only=name == "needs", before=before,
                                      limit=PAGE_HISTORY + 1)  # fmt: skip
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
            return status, {"item": book.view(fresh, events=True)}
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
            approval = await d.changes.request(change_id, "apply" if action == "apply" else "revert", user, shown)
            return await self._run("approve_change", {"approval": approval["id"]}, user)
        if key.startswith("maint:"):
            return await self._act_maintenance(found, action, user)
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
            approval = updates.request_reboot(int(source.get("job") or 0), user=user)
            return await self._run("approve_reboot", {"approval": approval["id"]}, user)
        latest = next(iter(self._store.recent_update_runs(limit=1)), None)
        item = next((i for i in (latest or {}).get("plan", []) if i.get("entity_id") == entity
                     and same_version(i.get("latest"), version)), None)  # fmt: skip
        if latest is None or item is None or not approvable(item):
            return 409, {"error": "not_available", "message": "This update cannot be approved now."}
        run = int(latest["id"])
        if action == "reject":
            updates.approvals.reject_update(item, plan_run=run, user=user)
            proposals.settle(self._store, entity, now=datetime.now(UTC), detail="rejected")
            self._d.hub.publish("updates", None, {})
            return 200, {}
        when = "now" if action == "approve_now" else "window"
        approval = updates.approvals.request(item, plan_run=run, user=user, when=when)
        proposals.settle(self._store, entity, now=datetime.now(UTC), detail="approval requested in the panel")
        return await self._run("approve_update", {"approval": approval["id"]}, user)

    async def _act_maintenance(self, found: Mapping[str, Any], action: str, user: str) -> Result:
        """A maintenance item: hide it, reject its dashboard plan, or approve that plan (the daemon re-checks it)."""
        d = self._d
        manager = d.maintenance
        finding = str((found.get("source") or {}).get("finding") or "")
        if manager is None or not finding:
            return 503, {"error": "store_unavailable"}
        if action == "dismiss":
            done = manager.hide(finding)
        elif action == "reject":
            done = manager.reject_dashboard(finding)
        elif action == "apply_dashboard":
            shown = [str(x) for x in (found.get("change") or {}).get("shown") or []]
            approval = await d.request_dashboard(finding, user, shown)
            return await self._run("approve_dashboard", {"approval": approval["id"]}, user)
        else:
            done = False
        return (200, {}) if done else (409, {"error": "not_available", "message": "Not open any more."})

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

    # ---------------------------------------------------------------- chats

    def chat(self) -> Result:
        """The one conversation with the admin (created on first use)."""
        thread = self._assistant.main_thread()
        return 200, {"thread": thread, "info": self._store.get_thread(thread)}

    def _thread_or_404(self, thread: str) -> dict[str, Any] | None:
        """Only the one chat is reachable."""
        if not THREAD_RE.fullmatch(thread) or thread != self._assistant.main_thread():
            return None
        return self._store.get_thread(thread)

    def thread(self, thread: str, query: Mapping[str, str]) -> Result:
        row = self._thread_or_404(thread)
        if row is None:
            return 404, {"error": "no_such_thread"}
        before = query.get("before")
        before_id = _int(before) if before is not None else None
        if before is not None and before_id is None:
            return _bad("bad_cursor")
        turns = self._store.turns_page(thread, before=before_id, limit=PAGE_TURNS)
        more = bool(turns) and bool(self._store.turns_page(thread, before=turns[0].id, limit=1))
        return 200, {
            "thread": row,
            "turns": [t.to_json() for t in turns],
            "more": more,
            "in_flight": self._assistant.live_status(thread),
        }

    async def message(self, thread: str, body: dict[str, Any], user: str) -> Result:
        if self._thread_or_404(thread) is None:
            return 404, {"error": "no_such_thread"}
        text = body.get("text")
        client_id = body.get("client_id")
        if not isinstance(text, str) or not text.strip():
            return _bad("empty", "Type a message first.")
        if len(text) > MAX_CHAT_CHARS:
            return 413, {"error": "too_long", "message": f"At most {MAX_CHAT_CHARS} characters."}
        if not isinstance(client_id, str) or not _CLIENT_ID_RE.fullmatch(client_id):
            return _bad("bad_client_id")
        principal = Principal(Origin.ADMIN_CHAT, user_id=user, channel="panel")
        sent = await self._assistant.submit(thread, text, principal=principal, channel="panel", client_id=client_id)
        turn = sent.turn.to_json() if sent.turn is not None else None
        match sent.result:
            case "queued":
                return 202, {"turn": turn, "state": "queued", "position": sent.position}
            case "duplicate":
                return 200, {"turn": turn, "state": turn["status"] if turn else "unknown", "duplicate": True}
            case "in_flight":
                return 409, {"error": "in_flight", "message": "Wait for the answer to your last message.", "turn": turn}
            case "refused":
                return 200, {"turn": turn, "state": "refused"}
        return 503, {"error": "busy", "message": "Not sent: the agent is busy.", "turn": turn}

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
        if self._thread_or_404(thread) is None:
            return 404, {"error": "no_such_thread"}
        upto = body.get("turn_id")
        upto_id = _int(upto) if upto is not None else None
        if upto is not None and upto_id is None:
            return _bad("bad_turn")
        if self._store.mark_read(thread, upto=upto_id):
            self._assistant.thread_changed(thread)
        return 200, {"ok": True}

    # -------------------------------------------------- thread management

    def cancel_investigation(self, thread: str, body: dict[str, Any]) -> Result:
        """Stop an investigation (one step: stopping is the safe direction)."""
        task_id = _int(body.get("task_id"))
        if task_id is None or self._thread_or_404(thread) is None:
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
        text = _CONTROL_KEEP_NL.sub("", raw).strip()
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
