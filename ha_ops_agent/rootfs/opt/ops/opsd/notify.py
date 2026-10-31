"""Companion push to the admin, with a relay fallback while Core is down."""

import json
import logging
import re
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from opsd.jsonstore import read_json, write_json_atomic
from opsd.selfguard import SELF_SLUG

_LOGGER = logging.getLogger(__name__)

# Daily caps per bucket. "safety" (deterministic daemon pushes: critical items,
# Core down, the kill switch) has its own reserve that Claude's pushes cannot use
# up. "user" (answers to the admin's own messages) has its own budget so chat and
# the agent's pushes cannot starve each other. "normal" (everything else, Needs
# you included) is capped by `notify.max_pushes_per_day`.
SAFETY_MAX_PER_DAY = 8
# Above the chat turn cap (30) so the daily-limit notice and reminders still arrive.
USER_MAX_PER_DAY = 40
BUCKETS = ("normal", "safety", "user")
_THREAD_RE = re.compile(r"[0-9a-f]{16}")
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


def deep_link(thread_id: str) -> str:
    """The panel URL that opens a chat thread (the sub-path form; verify ingress forwards it)."""
    if not _THREAD_RE.fullmatch(thread_id):
        return panel_url()
    return f"{panel_url()}/t/{thread_id}"


@dataclass(frozen=True, slots=True)
class Push:
    title: str
    message: str
    urgent: bool = False
    tag: str = "ha-ops"
    kind: str = "normal"  # normal | safety (deterministic) | user (replies)
    # Companion action dicts (`action`, `title`, ...), built by `channels`.
    actions: tuple[dict[str, Any], ...] = ()
    # Where tapping the notification opens the panel (a chat thread's deep link).
    url: str | None = None


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


def extract_registration(config_entries: dict[str, Any], notify_service: str) -> dict[str, Any] | None:
    """The mobile_app registration behind `notify.<notify_service>`, for the relay.

    The legacy notify service is named `mobile_app_<slug(device_name)>`.
    """
    wanted = notify_service.removeprefix("mobile_app_")
    for entry in config_entries.get("data", {}).get("entries", []):
        if entry.get("domain") != "mobile_app":
            continue
        data = entry.get("data", {})
        if slugify(str(data.get("device_name", ""))) != wanted:
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
    """Rate-limited sender. Non-urgent pushes share a small daily budget."""

    def __init__(
        self,
        *,
        state_file: Path,
        push_cache_file: Path,
        notify_service: str,
        max_per_day: int,
        call_service: Callable[[str, str, dict[str, Any]], Any],
        relay: Callable[[dict[str, Any], Push], None] = post_relay,
        blocked: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._state_file = state_file
        self._push_cache_file = push_cache_file
        self._service = notify_service
        self._max_per_day = max_per_day
        self._call_service = call_service
        self._relay = relay
        # Why the device may get no push at all (its user is not an active HA admin), or None.
        self._blocked = blocked

    def _allowance(self, push: Push, today: str) -> tuple[bool, dict[str, Any]]:
        state: dict[str, Any] = read_json(self._state_file, {})
        if state.get("day") != today:
            state = {"day": today, **dict.fromkeys(BUCKETS, 0)}
        bucket = self._bucket(push)
        limit = {"safety": SAFETY_MAX_PER_DAY, "user": USER_MAX_PER_DAY}.get(bucket, self._max_per_day)
        return state.get(bucket, 0) < limit, state

    @staticmethod
    def _bucket(push: Push) -> str:
        return push.kind if push.kind in BUCKETS else "normal"

    def send(self, push: Push, *, core_up: bool, now: datetime) -> str:
        """Returns `sent`, `relayed`, `rate_limited` or `failed`. Never raises."""
        if not self._service:
            # No `notify.service` option and no `notify_service` in the house profile: nowhere to push.
            _LOGGER.warning("no notify service configured; push dropped", extra={"title": push.title})
            return "failed"
        if self._blocked() is not None:
            # The device is not the admin's: notifications go to the admin only, so it gets
            # nothing, safety pushes included; the System sheet and the health item say why.
            _LOGGER.error("notify device is not an active admin's; push dropped", extra={"title": push.title})
            return "failed"
        today = now.date().isoformat()
        allowed, state = self._allowance(push, today)
        if not allowed:
            _LOGGER.warning("push rate limited", extra={"title": push.title, "urgent": push.urgent})
            return "rate_limited"
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
            bucket = self._bucket(push)
            state[bucket] = state.get(bucket, 0) + 1
            write_json_atomic(self._state_file, state)
        _LOGGER.info("push", extra={"outcome": outcome, "title": push.title, "urgent": push.urgent})
        return outcome

    def cache_registration(self, config_entries_file: Path) -> bool:
        """Refresh the relay registration from Core's storage (read-only)."""
        if not self._service:
            return False
        try:
            entries = json.loads(config_entries_file.read_text())
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("cannot read config entries", extra={"error": str(err)[:200]})
            return False
        registration = extract_registration(entries, self._service)
        if registration is None:
            _LOGGER.warning("no mobile_app registration for notify service", extra={"service": self._service})
            return False
        write_json_atomic(self._push_cache_file, registration, mode=0o600)
        return True
