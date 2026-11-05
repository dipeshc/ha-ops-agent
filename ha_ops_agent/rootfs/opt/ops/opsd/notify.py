"""The one push channel: a `Message` to the admin's Companion app, with a relay fallback while Core is down.

Spec items-and-pushes.md N4 (daily budgets per `BudgetClass`), N5 (target and delivery), N6 (admin only), N10
(push actions: every action id is recorded, so an inbound tap can be matched to a push the agent sent).
"""

import json
import logging
import re
import secrets
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from opsd.channels import Action, ActionKind, BudgetClass, Message
from opsd.jsonstore import read_json, write_json_atomic
from opsd.selfguard import SELF_SLUG
from opsd.store import Store

_LOGGER = logging.getLogger(__name__)

# Daily caps per budget class; the agent class is capped by `notify.max_pushes_per_day`.
SAFETY_MAX_PER_DAY = 8
USER_MAX_PER_DAY = 40
ACTION_TTL = timedelta(days=3)
ACTION_PREFIX = "OPS"
# The add-on's slug for push links: the house profile's `self_slug` (validated there), set once at start.
_PANEL = {"slug": SELF_SLUG}
_OWN_SLUG_RE = re.compile(r"[a-z0-9]{1,32}_ha_ops_agent")


def set_panel_slug(slug: str) -> None:
    _PANEL["slug"] = slug


def own_slug(default: str = SELF_SLUG) -> str:
    """The add-on's slug from its container hostname, which the Supervisor sets to the slug with `-` for `_`.

    An add-on installed from a repository has a slug like `0a1b2c3d_ha_ops_agent`, so push links need it
    unless the house profile names it. Anything that does not look like this add-on's slug gives `default`.
    """
    host = socket.gethostname().replace("-", "_")
    return host if _OWN_SLUG_RE.fullmatch(host) else default


def panel_url() -> str:
    """The ingress panel's URL, which a tapped push opens."""
    return f"/hassio/ingress/{_PANEL['slug']}"


def new_action_id(kind: ActionKind) -> str:
    """128 random bits; the id is the only thing a forged tap could not know."""
    return f"{ACTION_PREFIX}_{kind}_{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class Push:
    """What goes on the wire: a message with its Companion action dicts."""

    title: str
    message: str
    urgent: bool = False
    tag: str = "ha-ops"
    actions: tuple[dict[str, Any], ...] = ()
    url: str | None = None


def _action_data(action_id: str, action: Action) -> dict[str, Any]:
    data: dict[str, Any] = {"action": action_id, "title": action.title[:40]}
    if action.text_input:
        data["behavior"] = "textInput"
    return data


# The Companion app's command that removes the notification with the same tag.
CLEAR_MESSAGE = "clear_notification"


def slugify(value: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.lower())).strip("_")


def push_data(push: Push) -> dict[str, Any]:
    panel = panel_url()
    url = push.url if push.url is not None and push.url.startswith(panel) else panel
    data: dict[str, Any] = {"tag": push.tag, "group": "ha-ops", "url": url}
    if push.urgent:
        data["push"] = {"interruption-level": "time-sensitive"}
    if push.actions:
        data["actions"] = [dict(a) for a in push.actions]
    return data


def extract_registration(config_entries: dict[str, Any], notify_service: str, user_id: str,
                         device_id: str | None = None) -> dict[str, Any] | None:  # fmt: skip
    """The admin's mobile_app registration behind `notify.<notify_service>`, for the relay.

    The legacy notify service is named `mobile_app_<slug(device_name)>`. Several registrations can carry that
    name (another user's phone named alike), so only the one `inbound.admin_identity` chose qualifies: the
    admin's `user_id` (and its `device_id` when known).
    """
    wanted = notify_service.removeprefix("mobile_app_")
    for entry in config_entries.get("data", {}).get("entries", []):
        if entry.get("domain") != "mobile_app":
            continue
        data = entry.get("data", {})
        if slugify(str(data.get("device_name", ""))) != wanted or data.get("user_id") != user_id:
            continue
        if device_id is not None and data.get("device_id") != device_id:
            continue
        app_data = data.get("app_data", {})
        if "push_url" not in app_data or "push_token" not in app_data:
            return None
        registration = {
            "push_url": app_data["push_url"],
            "push_token": app_data["push_token"],
            "registration_info": {
                "app_id": data.get("app_id"),
                "app_version": data.get("app_version"),
                "webhook_id": data.get("webhook_id"),
            },
        }
        if data.get("os_version"):
            registration["registration_info"]["os_version"] = data["os_version"]
        return registration
    return None


def relay_payload(registration: dict[str, Any], push: Push) -> dict[str, Any]:
    """Same body Core's mobile_app notify platform posts to the push relay."""
    return {
        "message": push.message,
        "title": push.title,
        "data": push_data(push),
        "push_token": registration["push_token"],
        "registration_info": registration["registration_info"],
    }


def post_relay(registration: dict[str, Any], push: Push, timeout: float = 10.0) -> None:
    body = json.dumps(relay_payload(registration, push)).encode()
    req = urllib.request.Request(
        registration["push_url"], data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status not in (200, 201, 202):
            raise RuntimeError(f"relay returned {resp.status}")


class Notifier:
    """Sends a `Message` within its budget class's daily cap; the one push channel."""

    def __init__(
        self,
        *,
        state_file: Path,
        push_cache_file: Path,
        notify_service: str,
        max_per_day: int,
        call_service: Callable[[str, str, dict[str, Any]], Any],
        store: Store | None = None,
        relay: Callable[[dict[str, Any], Push], None] = post_relay,
        blocked: Callable[[], str | None] = lambda: None,
        new_id: Callable[[ActionKind], str] = new_action_id,
    ) -> None:
        self._state_file = state_file
        self._push_cache_file = push_cache_file
        self._service = notify_service
        self._call_service = call_service
        self._store = store
        self._relay = relay
        # Why the device may get no push at all (its user is not an active HA admin), or None.
        self._blocked = blocked
        self._new_id = new_id
        self._limits = {BudgetClass.SAFETY: SAFETY_MAX_PER_DAY, BudgetClass.USER: USER_MAX_PER_DAY,
                        BudgetClass.AGENT: max_per_day}  # fmt: skip

    def _allowance(self, bucket: BudgetClass, today: str) -> tuple[bool, dict[str, Any]]:
        state: dict[str, Any] = read_json(self._state_file, {})
        if state.get("day") != today:
            state = {"day": today, **dict.fromkeys(map(str, BudgetClass), 0)}
        return state.get(str(bucket), 0) < self._limits[bucket], state

    def _actions(self, message: Message, now: datetime) -> tuple[dict[str, Any], ...]:
        """The push's buttons, each id recorded for the inbound tap; none without the store (unverifiable)."""
        if self._store is None:
            return ()
        found = []
        for action in message.actions:
            action_id = self._new_id(action.kind)
            self._store.issue_action(action_id, kind=str(action.kind), tag=message.tag, thread_id=message.thread_id,
                                     now=now, expires_at=now + ACTION_TTL)  # fmt: skip
            found.append(_action_data(action_id, action))
        return tuple(found)

    def send(self, message: Message, *, core_up: bool, now: datetime) -> str:
        """Returns `sent`, `relayed`, `rate_limited` or `failed`. Never raises."""
        if not self._service:
            # No `notify.service` option and no `notify_service` in the house profile: nowhere to push.
            _LOGGER.warning("no notify service configured; push dropped", extra={"title": message.title})
            return "failed"
        if self._blocked() is not None:
            # The device is not the admin's: notifications go to the admin only, so it gets
            # nothing, safety pushes included; the System sheet and the health item say why.
            _LOGGER.error("notify device is not an active admin's; push dropped", extra={"title": message.title})
            return "failed"
        bucket = message.budget_class
        allowed, state = self._allowance(bucket, now.date().isoformat())
        if not allowed:
            _LOGGER.warning("push rate limited", extra={"title": message.title, "urgent": message.urgent})
            return "rate_limited"
        push = Push(message.title, message.body, urgent=message.urgent, tag=message.tag,
                    actions=self._actions(message, now), url=message.url)  # fmt: skip
        outcome = "failed"
        if core_up:
            try:
                self._call_service(
                    "notify",
                    self._service,
                    {"title": push.title, "message": push.message, "data": push_data(push)},
                )
                outcome = "sent"
            except Exception as err:  # noqa: BLE001 - fall through to the relay
                _LOGGER.warning("core notify failed", extra={"error": str(err)[:200]})
        if outcome == "failed":
            registration = read_json(self._push_cache_file, None)
            if registration:
                try:
                    self._relay(registration, push)
                    outcome = "relayed"
                except (urllib.error.URLError, OSError, RuntimeError) as err:
                    _LOGGER.error("relay push failed", extra={"error": str(err)[:200]})
            else:
                _LOGGER.error("no cached push registration for relay fallback")
        if outcome != "failed":
            state[str(bucket)] = state.get(str(bucket), 0) + 1
            write_json_atomic(self._state_file, state)
        _LOGGER.info("push", extra={"outcome": outcome, "title": push.title, "urgent": push.urgent})
        return outcome

    def clear(self, tag: str, *, core_up: bool) -> str:
        """Withdraw the delivered push with this tag (Companion `clear_notification`): outside the daily budgets and
        quiet hours, since it only removes something. Returns `sent`, `relayed` or `failed`. Never raises."""
        if not self._service or self._blocked() is not None or not tag:
            return "failed"
        push = Push("", CLEAR_MESSAGE, tag=tag)
        if core_up:
            try:
                self._call_service("notify", self._service, {"message": CLEAR_MESSAGE, "data": {"tag": tag}})
                return "sent"
            except Exception as err:  # noqa: BLE001 - fall through to the relay
                _LOGGER.warning("core notify clear failed", extra={"error": str(err)[:200]})
        registration = read_json(self._push_cache_file, None)
        if not registration:
            return "failed"
        try:
            self._relay(registration, push)
        except (urllib.error.URLError, OSError, RuntimeError) as err:
            _LOGGER.warning("relay clear failed", extra={"error": str(err)[:200]})
            return "failed"
        return "relayed"

    def cache_registration(self, config_entries_file: Path, user_id: str | None, device_id: str | None = None,
                           ) -> bool:  # fmt: skip
        """Refresh the relay registration from Core's storage (read-only): the verified admin's only.

        Without a verified admin (`user_id` None) the cache is left as it is.
        """
        if not self._service or not user_id:
            return False
        try:
            entries = json.loads(config_entries_file.read_text())
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("cannot read config entries", extra={"error": str(err)[:200]})
            return False
        registration = extract_registration(entries, self._service, user_id, device_id)
        if registration is None:
            _LOGGER.warning("no mobile_app registration for notify service", extra={"service": self._service})
            return False
        write_json_atomic(self._push_cache_file, registration, mode=0o600)
        return True
