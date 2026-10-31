"""Inbound events from the admin's phone, verified before anything acts on them.

The Companion app reports a tap on a notification button (or a text reply)
as a `mobile_app_notification_action` event on Core's bus, which the
daemon's persistent websocket subscription receives. An event becomes an
`Event` on the queue only if all of these hold, otherwise it is refused and
audited (refusals are rate-limited):

1. Its `context.user_id` is the admin's HA user id. HA sets the context from
   the authenticated connection that fired the event, so another user
   cannot claim to be the admin. The admin's id is taken from the
   registration of the configured admin device and confirmed as an active
   administrator; if either step fails, every event is refused.
2. Its action id has the agent's shape and was issued by this daemon on a
   push to the admin's device (`issued_actions`), is unexpired, and has not
   been used: it is consumed in the same transaction, so a replay fails.
3. If the event names a source device (`sourceDeviceID` or
   `sourceDevicePermanentID`), it must be the admin's: the registration's
   device id or the device's slug. An event that names another device is
   refused.

Observed behaviour: the iOS Companion app sends only
`action` in this event's data (plus `reply_text` for a text reply, by the
same code path; not yet seen in the recorder), with no source device at
all, and `context.user_id` set to the admin. Requiring `sourceDeviceID`
therefore refused every real tap. What a tap proves is "the admin's HA
user fired an event naming an unguessable, single-use action id that this
daemon put on a push to the admin's device" - not which device tapped.

Everything accepted here carries the `admin-chat` origin, never `admin`:
code running inside Core with an admin token can fire these events with the
admin's user id, so the policy engine lets them only read, manage reminders
and notify. Approvals and every other high-impact action need the ingress
panel request (ingress admin-user check, CSRF token); no tap grants one.

Verified live: `context.user_id` is populated and `action` carries the id.
Still unverified: `reply_text` on a real reply, and the
`sourceDevicePermanentID` field name on other platforms.
"""

import hmac
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from opsd.events import Event, EventQueue, Priority
from opsd.notify import slugify
from opsd.policy import Origin
from opsd.rpc import RejectionLimiter
from opsd.runctx import Principal
from opsd.store import Store, StoreError

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE = "mobile_app_notification_action"
ACTION_RE = re.compile(r"OPS_(REPLY|SNOOZE|DONE)_[0-9a-f]{32}")
MAX_REPLY_CHARS = 2000
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_EVENT_FOR_KIND = {
    "REPLY": ("user.message", Priority.USER),
    "SNOOZE": ("user.action", Priority.USER),
    "DONE": ("user.action", Priority.USER),
}


@dataclass(frozen=True, slots=True)
class AdminIdentity:
    user_id: str
    device_id: str | None = None
    # The registration's device name as a slug (`notify.mobile_app_<slug>`).
    slug: str | None = None


@dataclass(frozen=True, slots=True)
class Refusal:
    reason: str


def admin_identity(config_entries: Mapping[str, Any], notify_service: str, users: Any = None) -> AdminIdentity | None:
    """The HA user behind `notify.<notify_service>`'s mobile_app registration.

    Several registrations can slug to the same name (another user's phone named alike). With
    `users` (`config/auth/list`) the one whose user is an active admin wins, and none qualifying means
    no admin; without it, the first match (the caller checks).
    """
    wanted = notify_service.removeprefix("mobile_app_")
    data_root = config_entries.get("data")
    entries = data_root.get("entries", []) if isinstance(data_root, dict) else []
    candidates: list[AdminIdentity] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("domain") != "mobile_app":
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or slugify(str(data.get("device_name", ""))) != wanted:
            continue
        user_id = data.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            continue
        device_id = data.get("device_id")
        candidates.append(AdminIdentity(user_id, device_id if isinstance(device_id, str) and device_id else None,
                                        wanted))  # fmt: skip
    if users is None:
        return candidates[0] if candidates else None
    return next((c for c in candidates if is_active_admin(users, c.user_id)), None)


def is_active_admin(users: Any, user_id: str) -> bool:
    """`config/auth/list`: the user exists, is active, not system-generated, in `system-admin`."""
    for user in users if isinstance(users, list) else []:
        if not isinstance(user, dict) or user.get("id") != user_id:
            continue
        groups = user.get("group_ids")
        return (
            user.get("is_active") is True
            and user.get("system_generated") is not True
            and isinstance(groups, list)
            and "system-admin" in groups
        )
    return False


def _clean_reply(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value).strip()[:MAX_REPLY_CHARS]
    return text or None


def verify_action(
    event: Mapping[str, Any], admin: AdminIdentity | None, store: Store | None, now: datetime
) -> Event | Refusal:
    if event.get("event_type") != EVENT_TYPE:
        return Refusal("unexpected event type")
    if admin is None:
        return Refusal("admin identity unknown")
    context = event.get("context")
    user_id = context.get("user_id") if isinstance(context, dict) else None
    if not isinstance(user_id, str) or not hmac.compare_digest(user_id.encode(), admin.user_id.encode()):
        return Refusal("not from the admin user")
    data = event.get("data")
    if not isinstance(data, dict):
        return Refusal("no event data")
    action = data.get("action")
    if not isinstance(action, str) or not ACTION_RE.fullmatch(action):
        return Refusal("not an agent action")
    # Optional (iOS sends none); when present it must name the admin's device.
    known = [v for v in (admin.device_id, admin.slug) if v]
    for field in ("sourceDeviceID", "sourceDevicePermanentID"):
        if field not in data:
            continue
        device = data[field]
        if not isinstance(device, str) or not any(hmac.compare_digest(device.encode(), k.encode()) for k in known):
            return Refusal("from another device")
    if store is None:
        return Refusal("store unavailable")
    issued = store.consume_action(action, now=now)
    if issued is None:
        return Refusal("unknown, expired or already used action")
    kind = action.split("_")[1]
    event_type, priority = _EVENT_FOR_KIND[kind]
    return Event(
        type=event_type,
        source="companion",
        priority=priority,
        payload={
            "action": kind,
            "action_id": action,
            "tag": issued.get("tag"),
            "thread_id": issued.get("thread_id"),
            "reply_text": _clean_reply(data.get("reply_text")),
        },
        principal=Principal(Origin.ADMIN_CHAT, user_id=admin.user_id, channel="companion"),
        dedupe_key=f"action:{action}",
        at=now,
    )


class InboundGate:
    """Verifies, audits and enqueues inbound events (the EventStream callback)."""

    def __init__(
        self,
        *,
        store: Store | None,
        queue: EventQueue,
        admin: AdminIdentity | None = None,
        limiter: RejectionLimiter | None = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self.admin = admin
        self._limiter = limiter or RejectionLimiter(30)

    def _audit(self, event: Mapping[str, Any], *, decision: str, detail: str, now: datetime) -> None:
        if self._store is None:
            return
        context = event.get("context")
        user = context.get("user_id") if isinstance(context, dict) else None
        data = event.get("data")
        action = data.get("action") if isinstance(data, dict) else None
        try:
            self._store.audit(
                at=now,
                run_id=None,
                principal=f"user:{str(user)[:64]}" if user else "unknown",
                channel="companion",
                capability=f"inbound.{str(event.get('event_type'))[:60]}",
                args={"action": str(action)[:80]} if action else {},
                tier=None,
                decision=decision,
                outcome=decision,
                detail=detail,
            )
        except (StoreError, sqlite3.Error, OSError) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    async def on_event(self, event: dict[str, Any]) -> None:
        now = datetime.now(UTC)
        try:
            result = verify_action(event, self.admin, self._store, now)
        except (StoreError, sqlite3.Error, OSError) as err:
            result = Refusal(f"verification failed: {type(err).__name__}")
        if isinstance(result, Refusal):
            if self._limiter.allow():
                self._audit(event, decision="refused", detail=result.reason, now=now)
            _LOGGER.warning("inbound event refused", extra={"reason": result.reason})
            return
        queued = self._queue.put(result)
        self._audit(event, decision="accepted", detail=result.type if queued else "duplicate or queue full", now=now)
        _LOGGER.info("inbound event accepted", extra={"event": result.type, "queued": queued})
