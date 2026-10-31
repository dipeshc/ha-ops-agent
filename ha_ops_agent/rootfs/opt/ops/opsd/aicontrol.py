"""Whether the AI may run right now: the kill switch and the usage-limit backoff.

- Kill switch: the UI helper `input_boolean.ops_agent_ai_enabled` (created
  by the daemon over the websocket, no YAML). Off stops the running AI
  process and refuses new runs. Deterministic health checks, liveness and
  safety pushes never consult it.
  Turning it on counts only when the verified admin did it (the state's
  `context.user_id`) or the daemon itself (first creation, a panel resume
  by the admin); anyone else's "on" is turned back off, audited and
  notified. Once the helper exists, an unknown, unavailable or missing
  state counts as off (fail closed); a deleted helper is recreated off.
- Backoff: a subscription usage or rate limit blocks runs until Claude Code's
  reset time, or for 1, 2, 4 ... 24 hours. No retry loop.
State lives in the store, so it survives restarts. Every change is audited.
"""

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from opsd import selfguard
from opsd.store import Store, StoreError

_LOGGER = logging.getLogger(__name__)

SWITCH_ENTITY = selfguard.AI_SWITCH_ENTITY
SWITCH_NAME = selfguard.AI_SWITCH_NAME
BACKOFF_BASE = timedelta(hours=1)
BACKOFF_MAX = timedelta(hours=24)


class Switch(StrEnum):
    """What the caller must do after `set_switch`."""

    NONE = "none"
    KILL = "kill"  # AI just became disabled: stop the running run.
    REVERT = "revert"  # A non-admin turned it on: turn it back off and notify.
    ENABLED = "enabled"


class AiControl:
    def __init__(self, store: Store | None) -> None:
        self._store = store
        # Without the store AI never runs anyway; keep an in-memory view.
        self._memory: dict[str, str] = {}

    def _get(self, key: str, default: str = "") -> str:
        if self._store is None:
            return self._memory.get(key, default)
        try:
            return self._store.kv_get(key, default)
        except StoreError, sqlite3.Error:
            return default

    def _set(self, key: str, value: str, now: datetime) -> None:
        self._memory[key] = value
        if self._store is not None:
            self._store.kv_set(key, value, now=now)

    def _audit(self, what: str, by: str, detail: str, now: datetime) -> None:
        if self._store is None:
            return
        try:
            self._store.audit(at=now, run_id=None, principal=by, channel="ai-control", capability=f"ai.{what}",
                              args={}, tier=None, decision=what, outcome=what, detail=detail[:300])  # fmt: skip
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("audit write failed", extra={"error": str(err)[:200]})

    # --------------------------------------------------------------- state

    @property
    def switch(self) -> str:
        """`on`, `off` or `unknown` (unknown blocks once the helper has been created)."""
        return self._get("ai.switch", "unknown")

    @property
    def helper_created(self) -> bool:
        return self._get("ai.switch_created") == "1"

    def mark_created(self, now: datetime | None = None) -> None:
        self._set("ai.switch_created", "1", now or datetime.now(UTC))

    def backoff_until(self) -> datetime | None:
        raw = self._get("ai.backoff_until")
        return datetime.fromisoformat(raw) if raw else None

    def refusal(self, now: datetime) -> str | None:
        """Why the AI may not start now, or None."""
        if self.switch == "off" or (self.switch != "on" and self.helper_created):
            return "ai_disabled"
        until = self.backoff_until()
        if until is not None and until > now:
            return "usage_limited"
        return None

    def snapshot(self, now: datetime) -> dict[str, object]:
        until = self.backoff_until()
        return {
            "ai_switch": self.switch,
            "ai_backoff_until": until.isoformat() if until and until > now else None,
            "ai_state": self.refusal(now) or "enabled",
        }

    # ------------------------------------------------------------- changes

    def set_switch(self, state: str, *, by: str, admin: bool, now: datetime | None = None) -> Switch:
        """Record the helper's state. `admin`: the change is the verified admin's (or the daemon's own).

        Off is always accepted (it only ever stops AI). On is accepted only
        from the admin. Unknown or unavailable
        counts as off once the helper exists.
        """
        now = now or datetime.now(UTC)
        if state not in ("on", "off"):
            state = "off" if self.helper_created else "unknown"
        previous = self.switch
        if state == "on" and not admin:
            if previous == "on":
                return Switch.NONE
            self._audit("switch_refused", by, f"'on' by a non-admin; stays {previous}", now)
            _LOGGER.warning("AI kill switch turned on by a non-admin; reverting", extra={"by": by})
            if previous != "off":
                self._set("ai.switch", "off", now)
            return Switch.REVERT
        if state == previous:
            return Switch.NONE
        self._set("ai.switch", state, now)
        self._audit(f"switch_{state}", by, f"was {previous}", now)
        _LOGGER.warning("AI kill switch changed", extra={"state": state, "by": by})
        if state == "on":
            return Switch.ENABLED
        return Switch.KILL if previous != "off" else Switch.NONE

    def usage_limited(self, reset_epoch: float | None, *, now: datetime | None = None) -> tuple[datetime, bool]:
        """Back off after a usage/rate limit: `(until, first_of_episode)`."""
        now = now or datetime.now(UTC)
        level = int(self._get("ai.backoff_level", "0") or 0) + 1
        step = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (level - 1)))
        until = now + step
        if reset_epoch is not None:
            reset = datetime.fromtimestamp(reset_epoch, UTC)
            if now < reset <= now + BACKOFF_MAX:
                until = max(until, reset)
        self._set("ai.backoff_level", str(level), now)
        self._set("ai.backoff_until", until.isoformat(timespec="seconds"), now)
        self._audit("backoff", "daemon", f"level {level} until {until.isoformat(timespec='seconds')}", now)
        return until, level == 1

    def run_succeeded(self, now: datetime | None = None) -> None:
        if self._get("ai.backoff_level", "0") not in ("", "0"):
            self._set("ai.backoff_level", "0", now or datetime.now(UTC))
