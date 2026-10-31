"""Usage observability and the kill switch for AI runs.

- Sensors (REST-set like `sensor.ops_agent`, re-published with it):
  `sensor.ops_agent_ai` (state: enabled or why AI is blocked; attributes:
  the agent's share of each window, the account's utilisation, run time,
  runs and tokens today per profile, the last run) and
  `sensor.ops_agent_runs_today` (`state_class: total` with a daily
  `last_reset`, so HA long-term statistics work).
- Deterministic notices (never written by the AI), at most once per reason
  per day, only when the kill switch was refused or recreated (a run held by
  the budget is shown in the panel and the chat card, never pushed).
- Kill switch: `input_boolean.ops_agent_ai_enabled`, created through the
  websocket if missing (no YAML), watched on the daemon's one Core
  websocket. Off stops the running AI process at once.
"""

import asyncio
import hmac
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, tzinfo
from typing import Any

from opsd.aicontrol import SWITCH_ENTITY, SWITCH_NAME, AiControl, Switch
from opsd.api import LiveAPI
from opsd.budget import Caps, Period
from opsd.channels import BudgetClass, Message
from opsd.claude_runner import RunResult
from opsd.config import Options
from opsd.runctx import RunContext
from opsd.store import Store, StoreError
from opsd.supervisor import ApiError, SupervisorClient
from opsd.ws import HAWebSocket

_LOGGER = logging.getLogger(__name__)

NOTICE_TAG = "ha-ops-usage"
_SWITCH_ID = SWITCH_ENTITY.split(".", 1)[1]
_REASONS = {
    "ai_disabled": "the AI kill switch is off",
    "usage_limited": "The AI provider reported a usage limit; waiting before trying again",
    "run_cap_reached": "today's run limit for this kind of run is reached",
    "share_cap": "the agent's share of the subscription window is used up",
    "share_reserve": "chat has used half of the agent's share; the rest is kept for monitoring",
    "account_busy": "the subscription is nearly at its limit from other use; the agent yields",
}


def _pct(value: float | None) -> float | None:
    # Three decimals: a single chat run is a few thousandths of a percent of a window.
    return None if value is None else round(100 * value, 3)


class UsageGuard:
    def __init__(
        self,
        *,
        store: Store,
        control: AiControl,
        options: Options,
        client: SupervisorClient,
        tz: Callable[[], tzinfo],
        send: Callable[[Message], Awaitable[str]],
        kill: Callable[[str], bool],
        admin_user: Callable[[], str | None] = lambda: None,
        api_key: Callable[[], bool] = lambda: False,
    ) -> None:
        self._admin_user = admin_user
        # True when the credential is a provider API key: no subscription windows apply.
        self._api_key = api_key
        self.store = store
        self.control = control
        self.options = options
        self.client = client
        self._tz = tz
        self._send = send
        self._kill = kill

    # ------------------------------------------------------------- figures

    def period(self, now: datetime) -> Period:
        return Period.at(now, self._tz())

    def caps(self) -> Caps:
        return Caps.from_options(self.options, detected_plan=self.store.kv_get("ai.plan_hint") or None,
                                 windows=not self._api_key())  # fmt: skip

    def view(self, now: datetime) -> dict[str, Any]:
        """Everything the sensors and the panel show."""
        caps = self.caps()
        try:
            usage = self.store.usage(self.period(now), at=now)
        except (StoreError, sqlite3.Error) as err:
            return {"error": str(err)[:200], **self.control.snapshot(now)}
        window, week, day = usage["window"], usage["week"], usage["day"]
        account = usage.get("account") or {}
        last = usage.get("last_run") or {}
        # With an API key there is no plan and no window cap to show (they are unbounded, not JSON).
        windows = caps.windows
        return {
            **self.control.snapshot(now),
            "plan": caps.plan if windows else None,
            "max_share_percent": round(caps.share_limit * 100, 2),
            "share_stop_percent": round(caps.share_stop * 100, 2) if windows else None,
            "agent_share_5h_percent": _pct(window["share_5h"]),
            "agent_share_7d_percent": _pct(week["share_7d"]),
            # True when the figure includes run-time estimates (utilisation moved less than its resolution).
            "agent_share_5h_estimated": window["estimated_runs"] > 0,
            "agent_share_7d_estimated": week["estimated_runs"] > 0,
            "account_5h_percent": _pct(account.get("five_hour")),
            "account_7d_percent": _pct(account.get("seven_day")),
            "run_time_5h_s": window["seconds"],
            "run_time_5h_cap_s": round(caps.window_seconds) if windows else None,
            "run_time_7d_s": week["seconds"],
            "run_time_7d_cap_s": round(caps.week_seconds) if windows else None,
            "today": {k: day[k] for k in ("runs", "turns", "tokens", "cache_read")},
            "profiles_today": usage.get("profiles", {}),
            "last_run": {k: last.get(k) for k in ("profile", "status", "started_at", "turns", "tokens",
                                                  "tokens_cache_read", "duration_s", "model", "share_5h")}
            if last else None,
        }  # fmt: skip

    async def publish(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        view = self.view(now)
        if "error" in view:
            return
        midnight = datetime.combine(now.astimezone(self._tz()).date(), datetime.min.time(), self._tz())
        reset = midnight.isoformat()
        sensors: list[tuple[str, object, dict[str, Any]]] = [
            ("sensor.ops_agent_ai", view["ai_state"], {**view, "friendly_name": "Ops Agent AI", "icon": "mdi:robot"}),
            ("sensor.ops_agent_runs_today", view["today"]["runs"],
             {"unit_of_measurement": "runs", "state_class": "total", "last_reset": reset,
              "friendly_name": "Ops Agent AI runs today"}),
        ]  # fmt: skip
        for entity, state, attrs in sensors:
            try:
                await _to_thread(self.client.set_state, entity, str(state), attrs)
            except ApiError as err:
                _LOGGER.warning("usage sensor publish failed", extra={"entity": entity, "error": str(err)[:200]})
                return

    # ------------------------------------------------------------- notices

    async def notice(
        self, key: str, title: str, body: str, now: datetime | None = None, *, quiet_ok: bool = False
    ) -> str | None:
        """A deterministic push from the safety bucket, at most once per key per day."""
        now = now or datetime.now(UTC)
        try:
            if not self.store.bump(self.period(now).day, f"notice:{key}", limit=1):
                return None
        except (StoreError, sqlite3.Error) as err:
            _LOGGER.error("notice rate limit unavailable; not sending", extra={"error": str(err)[:200]})
            return None
        message = Message(title, body, urgent=False, tag=NOTICE_TAG, budget_class=BudgetClass.SAFETY, quiet_ok=quiet_ok)
        return await self._send(message)

    # ----------------------------------------------------------- after run

    async def after_run(self, profile: object, result: RunResult, context: RunContext) -> None:
        now = datetime.now(UTC)
        if result.plan_hint:
            self.store.kv_set("ai.plan_hint", result.plan_hint, now=now)
        if result.status == "usage_limited":
            self.control.usage_limited(result.limit_reset, now=now)
        elif result.status == "ok":
            self.control.run_succeeded(now)
        await self.publish(now)

    # ----------------------------------------------------------- kill switch

    def _is_admin(self, user: object) -> bool:
        admin = self._admin_user()
        return isinstance(user, str) and admin is not None and hmac.compare_digest(user.encode(), admin.encode())

    async def ensure_switch(self) -> None:
        """Create the UI helper if it does not exist (storage-backed; no YAML).

        The first creation starts it on (the daemon's own act). A helper that
        existed and was deleted is recreated off, and the admin is told.
        """
        try:
            async with LiveAPI(self.client) as api:
                items = await api.ws("input_boolean/list")
                if any(isinstance(i, dict) and i.get("id") == _SWITCH_ID for i in items or []):
                    self.control.mark_created()
                    return
                recreate = self.control.helper_created
                await api.ws("input_boolean/create", name=SWITCH_NAME, icon="mdi:robot-outline", initial=not recreate)
        except (OSError, RuntimeError, ValueError) as err:
            _LOGGER.warning("cannot ensure the AI kill switch helper", extra={"error": str(err)[:200]})
            return
        self.control.mark_created()
        if recreate:
            self.control.set_switch("off", by="daemon", admin=True)
            _LOGGER.warning("AI kill switch helper was deleted; recreated off")
            await self.notice(
                "switch_recreated", "Ops Agent: AI kill switch recreated",
                f"'{SWITCH_NAME}' had been deleted, so AI runs stopped. It is back, off; turn it on to resume.",
                quiet_ok=True,
            )  # fmt: skip
        else:
            self.control.set_switch("on", by="daemon", admin=True)
            _LOGGER.info("created the AI kill switch helper", extra={"entity": SWITCH_ENTITY})

    async def sync_switch(self) -> None:
        """Read the helper's state once: after (re)subscribing, a change made while disconnected counts."""
        try:
            state = await _to_thread(self.client.core, f"/states/{SWITCH_ENTITY}")
        except ApiError as err:
            if err.status == 404:
                await self._switch_missing("sync")
            return
        if isinstance(state, dict):
            context = state.get("context") if isinstance(state.get("context"), dict) else {}
            await self.apply_switch(str(state.get("state")), user=(context or {}).get("user_id"), by="sync")

    async def _switch_missing(self, by: str) -> None:
        # Missing counts as off (fail closed) until it is recreated.
        await self.apply_switch("missing", user=None, by=by)
        await self.ensure_switch()

    async def apply_switch(self, state: str, *, user: object, by: str) -> None:
        action = self.control.set_switch(state, by=by, admin=self._is_admin(user))
        if action is Switch.KILL:
            self._kill("the AI kill switch was turned off")
        elif action is Switch.REVERT:
            self._kill("the AI kill switch was turned on by someone other than the admin")
            await self.set_switch(on=False)
            await self.notice(
                "switch_refused", "Ops Agent: AI kept off",
                f"'{SWITCH_NAME}' was turned on by someone other than the admin, so the agent turned it off again. "
                "Turn it on from the admin account (or the Ops Agent panel) to resume.",
                quiet_ok=True,
            )  # fmt: skip
        await self.publish()

    async def resume(self, admin_user: str) -> bool:
        """The admin resumed AI from the panel: accepted as the admin's act."""
        if not self._is_admin(admin_user):
            return False
        self.control.set_switch("on", by=f"panel:{admin_user[:64]}", admin=True)
        await self.set_switch(on=True)
        await self.publish()
        return True

    async def on_switch_event(self, event: dict[str, Any]) -> None:
        trigger = (event.get("variables") or {}).get("trigger") or {}
        if not isinstance(trigger, dict):
            return
        to_state = trigger.get("to_state")
        if "to_state" in trigger and to_state is None:
            # The helper was deleted.
            await self._switch_missing("event")
            return
        if not isinstance(to_state, dict):
            return
        raw_context = to_state.get("context")
        context: dict[str, Any] = raw_context if isinstance(raw_context, dict) else {}
        user = context.get("user_id")
        await self.apply_switch(str(to_state.get("state")), user=user, by=f"user:{str(user)[:64]}")

    async def set_switch(self, *, on: bool) -> None:
        service = "turn_on" if on else "turn_off"
        try:
            await _to_thread(self.client.call_service, "input_boolean", service, {"entity_id": SWITCH_ENTITY})
        except ApiError as err:
            _LOGGER.warning("cannot set the AI kill switch", extra={"error": str(err)[:200]})

    async def subscribe_switch(self, ws: HAWebSocket) -> None:
        """Watch the helper on the daemon's one Core websocket, then catch up on its current state."""
        await ws.command("subscribe_trigger", trigger={"platform": "state", "entity_id": SWITCH_ENTITY})
        await self.sync_switch()


async def _to_thread(func: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(func, *args)
