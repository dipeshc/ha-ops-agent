"""The one budget (share rule, run time, profile run caps), kill switch, usage-limit backoff and sensors."""

import asyncio
import dataclasses
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from opsd.aicontrol import SWITCH_ENTITY, AiControl, Switch
from opsd.budget import Caps, Period, resolve_plan, run_share, share_fraction, utilisation
from opsd.claude_runner import RunResult, interpret_result, plan_hint, run_claude, usage_limit
from opsd.config import Options, UsageOptions
from opsd.redact import Redactor
from opsd.runctx import AGENT_SELF, RunContext
from opsd.store import Store
from opsd.supervisor import ApiError
from opsd.usage import UsageGuard
from tests.conftest import make_options
from tests.test_claude_runner import config

NOW = datetime(2026, 1, 26, 18, 0, tzinfo=UTC)
P = Period.at(NOW, UTC)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Store]:
    store = Store.open(tmp_path / "ops.db")
    yield store
    store.close()


def reserve(db: Store, caps: Caps, *, profile: str = "chat", at: datetime = NOW, run_id: str | None = None) -> Any:
    return db.reserve_run(run_id=run_id or f"r{at.timestamp()}{profile}{id(caps)}{threading.get_ident()}",
                          profile=profile, budget_class="user", period=Period.at(at, UTC), at=at, max_turns=15,
                          budget_usd=0.5, caps=caps)  # fmt: skip


# ------------------------------------------------------------------ budget


def test_share_option_scales_run_time_and_is_clamped() -> None:
    base = Caps.from_options(Options())
    assert base.share_limit == 0.05 and base.share_stop == pytest.approx(0.04)
    double = Caps.from_options(make_options(usage__max_share_percent=10))
    assert double.week_seconds == 2 * base.week_seconds
    assert share_fraction(0.1) == 0.005 and share_fraction(80) == 0.25
    # Past the schema range (as a hand-edited options.json could be): the code range still holds.
    assert Caps.from_options(Options(usage=UsageOptions(max_share_percent=90))).share_limit == 0.25


def test_plan_resolution() -> None:
    assert resolve_plan("auto", None) == "pro" and resolve_plan("auto", "max20") == "max20"
    assert resolve_plan("max5", "max20") == "max5" and resolve_plan("auto", "weird") == "pro"
    pro, max20 = Caps.from_options(Options()), Caps.from_options(make_options(claude__plan="max20"))
    assert pro.week_seconds == 40 * 3600 * 0.05 and pro.window_seconds == pro.week_seconds * 0.2
    assert max20.week_seconds == 6 * pro.week_seconds


def test_utilisation_parsing() -> None:
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.42, "resetsAt": 1},
                                                    "seven_day": {"utilization": 12.5, "resetsAt": 2}}}  # fmt: skip
    assert utilisation(info, "five_hour") == 0.42 and utilisation(info, "seven_day") == 0.125
    assert utilisation({"rateLimitType": "five_hour", "utilization": 0.3}, "five_hour") == 0.3
    for bad in (None, {}, {"unifiedWindows": {"five_hour": {"utilization": "x"}}}, {"rateLimitType": "five_hour",
                "utilization": -1}, {"rateLimitType": "five_hour", "utilization": True}):  # fmt: skip
        assert utilisation(bad, "five_hour") is None
    assert run_share(
        {"rateLimitType": "five_hour", "utilization": 0.40},
        {"rateLimitType": "five_hour", "utilization": 0.43},
        "five_hour",
    ) == pytest.approx(0.03)
    assert run_share(None, info, "five_hour") is None
    assert run_share(info, {"rateLimitType": "five_hour", "utilization": 0.1}, "five_hour") == 0.0


# ------------------------------------------------------------ reservation


def test_reservation_replaced_by_actuals(db: Store) -> None:
    caps = Caps.from_options(Options())
    res = reserve(db, caps, run_id="a")
    assert res.turns == 15 and res.seconds == 180.0
    usage = db.usage(P, at=NOW)
    assert usage["window"]["seconds"] == 180.0 and usage["window"]["turns"] == 15
    db.finish_run("a", status="ok", at=NOW, turns=3, tokens_in=1000, tokens_out=200, tokens_cache_read=5000,
                  cost_usd=0.01, duration_s=20, model="m", share_5h=0.002, share_7d=0.0004, acct_5h=0.3,
                  acct_7d=0.1)  # fmt: skip
    usage = db.usage(P, at=NOW)
    assert (usage["window"]["tokens"], usage["window"]["turns"], usage["window"]["seconds"]) == (1200, 3, 20)
    assert usage["day"]["cache_read"] == 5000 and usage["account"] == {"five_hour": 0.3, "seven_day": 0.1}
    assert usage["last_run"]["profile"] == "chat" and usage["profiles"]["chat"]["runs"] == 1


def test_concurrent_reservations_cannot_both_take_the_last_slot(tmp_path: Path) -> None:
    path = tmp_path / "ops.db"
    Store.open(path).close()
    caps = dataclasses.replace(Caps.from_options(Options()), profile_runs={"chat": 1})
    results: list[Any] = []

    def worker(i: int) -> None:
        store = Store.open(path)
        try:
            results.append(reserve(store, caps, run_id=f"race{i}"))
        finally:
            store.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if not isinstance(r, str)) == 1
    assert {r for r in results if isinstance(r, str)} == {"run_cap_reached:chat"}


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("share_5h", "share_cap:five_hour"),
        ("share_7d", "share_cap:seven_day"),
        ("account", "account_busy:five_hour"),
        ("seconds", "share_cap:run_time"),
        ("profile_runs", "run_cap_reached:chat"),
    ],
)
def test_each_cap_refuses(db: Store, setup: str, reason: str) -> None:
    caps = Caps.from_options(Options())
    if setup == "profile_runs":
        caps = dataclasses.replace(caps, profile_runs={"chat": 0})
    if setup in ("share_5h", "share_7d", "account", "seconds"):
        assert not isinstance(reserve(db, caps, run_id="seed", at=NOW - timedelta(minutes=30)), str)
        db.finish_run("seed", status="ok", at=NOW - timedelta(minutes=10), turns=1, tokens_in=1, tokens_out=1,
                      tokens_cache_read=0, cost_usd=0, model="m",
                      duration_s=caps.window_seconds if setup == "seconds" else 1,
                      share_5h=0.039 if setup == "share_5h" else 0.0,
                      share_7d=0.039 if setup == "share_7d" else 0.0,
                      acct_5h=0.95 if setup == "account" else 0.2, acct_7d=0.1)  # fmt: skip
    assert reserve(db, caps) == reason


def test_crashed_runs_keep_their_reservation(db: Store) -> None:
    caps = Caps.from_options(Options())
    reserve(db, caps, run_id="crash")
    assert db.close_stale_runs(at=NOW) == 1
    usage = db.usage(P, at=NOW)
    assert usage["window"]["seconds"] == 180.0 and usage["last_run"]["status"] == "interrupted"


def test_prune_bounds_the_store(db: Store) -> None:
    old = NOW - timedelta(days=120)
    db.issue_action("OPS_DONE_" + "a" * 32, kind="DONE", tag="t", thread_id=None, now=old, expires_at=old)
    reserve(db, Caps.from_options(Options()), run_id="old", at=old)
    counts = db.prune(now=NOW)
    assert counts["actions"] == 1 and counts["runs"] == 1


# ---------------------------------------------------------- claude output


def test_tokens_duration_and_usage_limit_from_result() -> None:
    ok = interpret_result(
        {
            "subtype": "success",
            "num_turns": 3,
            "duration_ms": 12500,
            "usage": {
                "input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 9000,
                "output_tokens": 70,
            },
        }
    )
    assert (ok.tokens_in, ok.tokens_out, ok.tokens_cache_read, ok.duration_s) == (150, 70, 9000, 12.5)
    limited = interpret_result(
        {"subtype": "success", "is_error": True, "result": "Claude AI usage limit reached|1793000000"}
    )
    assert limited.status == "usage_limited" and limited.limit_reset == 1793000000
    assert usage_limit("API Error: 429 rate_limit_error") == (True, None)
    assert usage_limit("all good") == (False, None)
    assert interpret_result({"subtype": "error_during_execution", "result": "boom"}).status == "error"
    assert plan_hint({"type": "system", "rateLimitTier": "default_claude_max_20x"}) == "max20"
    assert plan_hint({"subscriptionType": "pro"}) == "pro" and plan_hint({"subscription_type": None}) is None


RATE_CLAUDE = """#!{python}
import json
def out(x): print(json.dumps(x), flush=True)
out({{"type": "system", "subtype": "init", "rateLimitTier": "default_claude_max_5x"}})
out({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed", "unifiedWindows": {{
    "five_hour": {{"utilization": 0.20, "resetsAt": 1}}, "seven_day": {{"utilization": 0.10, "resetsAt": 2}}}}}}}})
out({{"type": "rate_limit_event", "rate_limit_info": {{"status": "{status}", "resetsAt": 1793000000,
    "unifiedWindows": {{"five_hour": {{"utilization": 0.23, "resetsAt": 1}},
                       "seven_day": {{"utilization": 0.105, "resetsAt": 2}}}}}}}})
out({{"type": "result", "subtype": "success", "is_error": {is_error}, "result": "x", "num_turns": 2}})
"""


@pytest.mark.parametrize(("status", "is_error", "expected"), [("allowed", "False", "ok"),
                                                              ("rejected", "True", "usage_limited")])  # fmt: skip
async def test_stream_rate_limit_events(tmp_path: Path, meminfo: Path, status: str, is_error: str,
                                        expected: str) -> None:  # fmt: skip
    script = tmp_path / "fake-rate"
    script.write_text(RATE_CLAUDE.format(python=sys.executable, status=status, is_error=is_error))
    script.chmod(0o755)
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(config(tmp_path, str(script)), "p", oauth_token="t", transcript_path=tmp_path / "t.gz",
                              redactor=Redactor(), proc_dir=proc_dir, meminfo=meminfo)  # fmt: skip
    assert result.status == expected and result.plan_hint == "max5"
    assert run_share(result.rate_first, result.rate_last, "five_hour") == pytest.approx(0.03)
    if expected == "usage_limited":
        assert result.limit_reset == 1793000000


# ------------------------------------------------------------ AI control


def test_kill_switch_and_backoff(db: Store) -> None:
    control = AiControl(db)
    assert control.refusal(NOW) is None and control.switch == "unknown"
    assert control.set_switch("off", by="user:u1", admin=False, now=NOW) is Switch.KILL  # off from anyone
    assert control.refusal(NOW) == "ai_disabled"
    assert control.set_switch("off", by="poll", admin=False) is Switch.NONE
    # A non-admin "on" is refused and must be reverted.
    assert control.set_switch("on", by="user:guest", admin=False, now=NOW) is Switch.REVERT
    assert control.refusal(NOW) == "ai_disabled"
    assert control.set_switch("on", by="user:u1", admin=True, now=NOW) is Switch.ENABLED
    assert control.refusal(NOW) is None
    # Once the helper exists, unknown/unavailable/missing counts as off.
    control.mark_created(NOW)
    assert control.set_switch("unavailable", by="poll", admin=False, now=NOW) is Switch.KILL
    assert control.refusal(NOW) == "ai_disabled"
    control.set_switch("on", by="user:u1", admin=True, now=NOW)
    until, first = control.usage_limited(None, now=NOW)
    assert first and until == NOW + timedelta(hours=1) and control.refusal(NOW) == "usage_limited"
    until, first = control.usage_limited(None, now=NOW)
    assert not first and until == NOW + timedelta(hours=2)
    reset = (NOW + timedelta(hours=7)).timestamp()
    until, _ = control.usage_limited(reset, now=NOW)
    assert until == NOW + timedelta(hours=7)
    for _ in range(10):
        until, _ = control.usage_limited(None, now=NOW)
    assert until == NOW + timedelta(hours=24)
    assert control.refusal(NOW + timedelta(hours=25)) is None
    control.run_succeeded(NOW)
    assert control.usage_limited(None, now=NOW)[1]
    audit = {r["capability"] for r in db.audit_rows(limit=100)}
    assert {"ai.switch_off", "ai.switch_on", "ai.switch_refused", "ai.backoff"} <= audit
    assert AiControl(None).refusal(NOW) is None


# ---------------------------------------------------------------- guard


class Client:
    token = "t"

    def __init__(self) -> None:
        self.states: dict[str, tuple[str, dict[str, Any]]] = {}
        self.services: list[tuple[str, str, dict[str, Any]]] = []
        self.switch: str | None = "on"
        self.switch_user: str | None = "u-admin"

    def set_state(self, entity: str, state: str, attrs: dict[str, Any]) -> None:
        self.states[entity] = (state, attrs)

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.services.append((domain, service, data))

    def core(self, path: str) -> Any:
        if self.switch is None:
            raise ApiError("GET", path, 404, "not found")
        return {"entity_id": SWITCH_ENTITY, "state": self.switch, "context": {"user_id": self.switch_user}}


class World:
    def __init__(self, db: Store, options: Options | None = None) -> None:
        self.db = db
        self.client = Client()
        self.sent: list[Any] = []
        self.kills: list[str] = []
        self.control = AiControl(db)

        async def send(message: Any) -> str:
            self.sent.append(message)
            return "sent"

        def kill(reason: str) -> bool:
            self.kills.append(reason)
            return True

        self.guard = UsageGuard(
            store=db,
            control=self.control,
            options=options or Options(),
            client=self.client,
            tz=lambda: UTC,
            send=send,
            kill=kill,
            admin_user=lambda: "u-admin",
        )  # type: ignore[arg-type]


def ctx() -> RunContext:
    from opsd.channels import BudgetClass

    return RunContext(profile="chat", principal=AGENT_SELF, allowed=frozenset(), budget_class=BudgetClass.USER,
                      started_at=NOW)  # fmt: skip


async def test_usage_limit_backs_off_without_a_push(db: Store) -> None:
    w = World(db)
    for _ in range(3):
        await w.guard.after_run("chat", RunResult(status="usage_limited", limit_reset=None), ctx())
    assert w.sent == [] and w.control.refusal(datetime.now(UTC)) == "usage_limited"


async def test_sensors_are_graphable(db: Store) -> None:
    w = World(db)
    reserve(db, Caps.from_options(Options()), run_id="s1", at=datetime.now(UTC))
    await w.guard.publish()
    states = w.client.states
    assert set(states) == {"sensor.ops_agent_ai", "sensor.ops_agent_runs_today"}
    runs, attrs = states["sensor.ops_agent_runs_today"]
    assert runs == "1" and attrs["state_class"] == "total" and "last_reset" in attrs
    ai_state, ai = states["sensor.ops_agent_ai"]
    assert ai_state == "enabled" and ai["plan"] == "pro" and ai["max_share_percent"] == 5.0
    assert ai["agent_share_5h_percent"] is not None and "usd_estimate" not in ai


class Live:
    created: ClassVar[list[str]] = []
    exists = False

    def __init__(self, _c: Any) -> None:
        pass

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def ws(self, msg_type: str, **fields: Any) -> Any:
        Live.created.append(msg_type)
        if msg_type == "input_boolean/create":
            Live.created.append(f"initial={fields.get('initial')}")
        return [{"id": "ops_agent_ai_enabled"}] if Live.exists else []


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> type[Live]:
    import opsd.usage as usage_mod

    Live.created, Live.exists = [], False
    monkeypatch.setattr(usage_mod, "LiveAPI", Live)
    return Live


async def test_kill_switch_events_and_sync(db: Store, live: type[Live]) -> None:
    w = World(db)
    await w.guard.ensure_switch()  # first creation: on, by the daemon
    assert live.created == ["input_boolean/list", "input_boolean/create", "initial=True"]
    assert w.control.switch == "on" and w.control.helper_created
    off = {"variables": {"trigger": {"to_state": {"state": "off", "context": {"user_id": "u-guest"}}}}}
    await w.guard.on_switch_event(off)
    assert w.kills == ["the AI kill switch was turned off"] and w.control.switch == "off"
    await w.guard.on_switch_event({"variables": {"trigger": "junk"}})
    # A non-admin turning it on is reverted, audited and notified once.
    on_guest = {"variables": {"trigger": {"to_state": {"state": "on", "context": {"user_id": "u-guest"}}}}}
    await w.guard.on_switch_event(on_guest)
    await w.guard.on_switch_event(on_guest)
    assert w.control.switch == "off"
    assert w.client.services.count(("input_boolean", "turn_off", {"entity_id": SWITCH_ENTITY})) == 2
    assert len([m for m in w.sent if m.title == "Ops Agent: AI kept off"]) == 1
    # The admin turning it on works (read on (re)subscribing, with the state's context).
    w.client.switch, w.client.switch_user = "on", "u-admin"
    await w.guard.sync_switch()
    assert w.control.switch == "on"
    # Deleted helper: off at once (fail closed), recreated off, admin told.
    w.client.switch = None
    live.created.clear()
    await w.guard.sync_switch()
    assert w.control.switch == "off" and w.control.refusal(datetime.now(UTC)) == "ai_disabled"
    assert "initial=False" in live.created
    assert any(m.title == "Ops Agent: AI kill switch recreated" for m in w.sent)


async def test_panel_resume_is_the_admins_act(db: Store, live: type[Live]) -> None:
    w = World(db)
    w.control.mark_created()
    w.control.set_switch("off", by="t", admin=True)
    assert not await w.guard.resume("u-guest")
    assert w.control.switch == "off"
    assert await w.guard.resume("u-admin")
    assert w.control.switch == "on"
    assert ("input_boolean", "turn_on", {"entity_id": SWITCH_ENTITY}) in w.client.services


async def test_no_admin_known_means_no_one_can_enable(db: Store) -> None:
    w = World(db)
    w.guard._admin_user = lambda: None
    w.control.set_switch("off", by="t", admin=True)
    await w.guard.apply_switch("on", user="u-admin", by="poll")
    assert w.control.switch == "off"


# ------------------------------------------------------------- kill mid-run


async def test_kill_switch_stops_a_running_triage_but_not_the_safety_push(
    paths: Any, fake_ha: Any, monkeypatch: pytest.MonkeyPatch, meminfo: Path
) -> None:
    from tests.test_daemon import Harness

    h = Harness(paths, fake_ha, monkeypatch, meminfo)
    try:
        await h.daemon().health_cycle("catch_up")
        h.fake.config_ok = False
        d = h.daemon()
        started = asyncio.Event()

        async def slow(cfg: Any, prompt: str, **kw: Any) -> RunResult:
            started.set()
            await asyncio.sleep(30)
            return RunResult(status="ok")

        d.runner.run_claude = slow
        cycle = asyncio.create_task(d.health_cycle("daily"))
        await asyncio.wait_for(started.wait(), 5)
        assert d.usage is not None
        await d.usage.apply_switch("off", user="u1", by="user:u1")
        outcome = await asyncio.wait_for(cycle, 5)
        assert outcome.result == "killed"
        assert h.pushes()[-1]["title"] == "Ops Agent: critical"
        # And nothing new starts while the switch is off.
        assert (await d.health_cycle("daily")).result == "ai_disabled"
        assert (
            d.store is not None
            and d.store.usage(Period.at(datetime.now(UTC), d.tz), at=datetime.now(UTC))["last_run"]["status"]
            == "killed"
        )
    finally:
        for daemon in h.daemons:
            daemon.close()


def test_unit_is_fixed_per_run_and_errs_toward_more_usage() -> None:
    def info(value: float) -> dict[str, Any]:
        return {"rateLimitType": "five_hour", "utilization": value}

    # Both readings at or below 1: fraction (records more than reading them as percent).
    assert run_share(info(0.5), info(0.8), "five_hour") == pytest.approx(0.3)
    # Any reading above 1: both are percent, never a mix.
    assert run_share(info(0.9), info(1.4), "five_hour") == pytest.approx(0.005)
    assert run_share(info(40), info(43), "five_hour") == pytest.approx(0.03)
    assert utilisation(info(0.7), "five_hour") == 0.7 and utilisation(info(70), "five_hour") == 0.7


def test_prune_keeps_the_rolling_week(db: Store) -> None:
    caps = Caps.from_options(Options())
    six_days = datetime.now(UTC) - timedelta(days=6)
    reserve(db, caps, run_id="recent", at=six_days)
    db.bump((datetime.now(UTC) - timedelta(days=6)).date().isoformat(), "x")
    counts = db.prune(now=datetime.now(UTC), keep=timedelta(days=1))  # a too-short keep is raised to 8 days
    assert counts["runs"] == 0 and counts["counters"] == 0
    assert db.usage(Period.at(datetime.now(UTC), UTC), at=datetime.now(UTC))["week"]["runs"] == 1


async def test_a_deleted_helper_is_off_at_once_and_recreated(db: Store, live: type[Live]) -> None:
    w = World(db)
    await w.guard.ensure_switch()
    live.created.clear()
    await w.guard.on_switch_event({"variables": {"trigger": {"from_state": {"state": "on"}, "to_state": None}}})
    assert w.control.switch == "off" and "initial=False" in live.created


async def test_one_websocket_carries_taps_and_the_switch(
    paths: Any, fake_ha: Any, monkeypatch: pytest.MonkeyPatch, meminfo: Path
) -> None:
    from opsd.inbound import EVENT_TYPE
    from tests.test_daemon import Harness
    from tests.test_inbound import FakeWS

    h = Harness(paths, fake_ha, monkeypatch, meminfo)
    try:
        d = h.daemon()
        assert d.usage is not None
        tap = {"event_type": EVENT_TYPE, "data": {"action": "x"}}
        switch = {"variables": {"trigger": {"to_state": {"state": "off", "context": {}}}}}
        socket = FakeWS([{"type": "event", "event": tap}, {"type": "event", "event": switch},
                         {"type": "event", "event": {"event_type": "state_changed"}}])  # fmt: skip
        connections = iter([socket])

        async def connect() -> Any:
            try:
                return next(connections)
            except StopIteration:
                raise asyncio.CancelledError from None

        seen: list[str] = []

        async def tapped(event: dict[str, Any]) -> None:
            seen.append("tap")

        async def switched(event: dict[str, Any]) -> None:
            seen.append("switch")

        async def synced() -> None:
            seen.append("sync")

        d._ws_connect = connect  # type: ignore[method-assign]
        d.inbound.on_event = tapped  # type: ignore[method-assign]
        d.usage.on_switch_event = switched  # type: ignore[method-assign]
        d.usage.sync_switch = synced  # type: ignore[method-assign]
        stream = d.event_stream()
        stream._sleep = lambda _s: asyncio.sleep(0)
        with pytest.raises(asyncio.CancelledError):
            await stream.run()
        assert [c[0] for c in socket.commands][:2] == ["subscribe_events", "subscribe_trigger"]
        assert seen == ["sync", "tap", "switch"]
    finally:
        for daemon in h.daemons:
            daemon.close()
