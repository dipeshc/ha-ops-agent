"""The one budget (run-time budget, profile run caps, cost cap), kill switch, usage-limit backoff and sensors."""

import asyncio
import dataclasses
import json
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from opsd.aicontrol import SWITCH_ENTITY, AiControl, Switch
from opsd.budget import (
    FRACTION,
    PERCENT,
    UNIT_KEY,
    Caps,
    Period,
    reading_unit,
    share_fraction,
    utilisation,
)
from opsd.claude_runner import RunResult, interpret_result, raw_utilisation, run_claude, usage_limit
from opsd.config import Options, UsageOptions
from opsd.redact import Redactor
from opsd.runctx import AGENT_SELF, RunContext
from opsd.store import ACTION_KEY, Store
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


def reserve(db: Store, caps: Caps, *, profile: str = "chat", at: datetime = NOW, run_id: str | None = None,
            budget_class: str = "user") -> Any:  # fmt: skip
    return db.reserve_run(run_id=run_id or f"r{at.timestamp()}{profile}{id(caps)}{threading.get_ident()}",
                          profile=profile, budget_class=budget_class, period=Period.at(at, UTC), at=at, max_turns=15,
                          budget_usd=0.5, caps=caps)  # fmt: skip


# ------------------------------------------------------------------ budget


def test_share_option_scales_run_time_and_is_clamped() -> None:
    base = Caps.from_options(Options())
    # B = 40 h x 3600 x 5 %; runs stop at 80 % of it, user-class runs at half of that; the 5-hour budget is B / 5.
    assert base.share_limit == 0.05 and base.week_seconds == 7200
    assert base.stop("seven_day") == 5760 and base.stop("seven_day", user=True) == 2880
    assert base.stop("five_hour") == pytest.approx(1152) and base.stop("five_hour", user=True) == pytest.approx(576)
    double = Caps.from_options(make_options(usage__max_share_percent=10))
    assert double.week_seconds == 2 * base.week_seconds
    assert share_fraction(0.1) == 0.005 and share_fraction(80) == 0.25
    # Past the schema range (as a hand-edited options.json could be): the code range still holds.
    assert Caps.from_options(Options(usage=UsageOptions(max_share_percent=90))).share_limit == 0.25


def test_plan_resolution() -> None:
    pro, max20 = Caps.from_options(Options()), Caps.from_options(make_options(claude__plan="max20"))
    assert pro.week_seconds == 40 * 3600 * 0.05 and pro.window_seconds == pro.week_seconds * 0.2
    assert max20.week_seconds == 6 * pro.week_seconds


def test_a_legacy_auto_plan_loads_as_pro() -> None:
    legacy = Options.from_mapping({"claude": {"plan": "auto"}})
    assert legacy.valid and legacy.claude.plan == "auto"
    assert Caps.from_options(legacy).week_seconds == Caps.from_options(Options()).week_seconds
    assert Options().claude.plan == "pro"


def test_utilisation_parsing() -> None:
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 42, "resetsAt": 1},
                                                    "seven_day": {"utilization": 12, "resetsAt": 2}}}  # fmt: skip
    assert reading_unit(info) == PERCENT
    assert utilisation(info, "five_hour") == 0.42 and utilisation(info, "seven_day") == 0.12
    assert utilisation({"rateLimitType": "five_hour", "utilization": 30}, "five_hour") == 0.3
    for bad in (None, {}, {"unifiedWindows": {"five_hour": {"utilization": "x"}}}, {"rateLimitType": "five_hour",
                "utilization": -1}, {"rateLimitType": "five_hour", "utilization": True},
                {"rateLimitType": "five_hour", "utilization": float("nan")},
                {"rateLimitType": "five_hour", "utilization": float("inf")}):  # fmt: skip
        assert utilisation(bad, "five_hour") is None
    # The run's log line carries the readings exactly as reported, with their JSON type, so the unit can be checked.
    assert raw_utilisation(info) == {"five_hour": {"value": 42, "type": "int"},
                                     "seven_day": {"value": 12, "type": "int"}}  # fmt: skip
    assert raw_utilisation({"rateLimitType": "seven_day", "utilization": 0.09}) == {
        "seven_day": {"value": 0.09, "type": "float"}}  # fmt: skip
    assert (
        raw_utilisation(None) == {} and raw_utilisation({"unifiedWindows": {"five_hour": {"utilization": "x"}}}) == {}
    )


# ------------------------------------------------------------ reservation


def test_reservation_replaced_by_actuals(db: Store) -> None:
    caps = Caps.from_options(Options())
    res = reserve(db, caps, run_id="a")
    assert res.turns == 15 and res.seconds == 180.0
    usage = db.usage(P, at=NOW)
    assert usage["window"]["seconds"] == 180.0 and usage["window"]["turns"] == 15
    db.finish_run("a", status="ok", at=NOW, turns=3, tokens_in=1000, tokens_out=200, tokens_cache_read=5000,
                  duration_s=20, model="m", acct_5h=0.3, acct_7d=0.1)  # fmt: skip
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


def seed(db: Store, caps: Caps, setup: str, *, windowed: Caps | None = None) -> None:
    """A finished run whose history makes the window cap `setup` refuse a subscription run."""
    budget = windowed or caps
    at = NOW - timedelta(days=2) if setup == "seconds_7d" else NOW - timedelta(minutes=30)
    assert not isinstance(reserve(db, caps, run_id="seed", at=at, budget_class="agent"), str)
    seconds = {"seconds_5h": budget.stop("five_hour"), "seconds_7d": budget.stop("seven_day")}.get(setup, 1)
    db.finish_run("seed", status="ok", at=at + timedelta(minutes=20), turns=1, tokens_in=1, tokens_out=1,
                  tokens_cache_read=0, model="m", duration_s=seconds,
                  acct_5h=0.95 if setup == "account" else 0.2, acct_7d=0.1)  # fmt: skip


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("seconds_5h", "share_cap:five_hour"),
        ("seconds_7d", "share_cap:seven_day"),
        ("account", "account_busy:five_hour"),
        ("profile_runs", "run_cap_reached:chat"),
    ],
)
def test_each_cap_refuses(db: Store, setup: str, reason: str) -> None:
    caps = Caps.from_options(Options())
    if setup == "profile_runs":
        caps = dataclasses.replace(caps, profile_runs={"chat": 0})
    else:
        seed(db, caps, setup)
    assert reserve(db, caps, budget_class="agent") == reason


def test_a_reservation_is_trimmed_to_the_budget_left_and_user_runs_stop_at_half(db: Store) -> None:
    caps = Caps.from_options(Options())
    assert not isinstance(reserve(db, caps, run_id="seed", at=NOW - timedelta(minutes=30), budget_class="agent"), str)
    used = caps.stop("five_hour", user=True) - 100
    db.finish_run("seed", status="ok", at=NOW - timedelta(minutes=10), turns=1, tokens_in=1, tokens_out=1,
                  tokens_cache_read=0, model="m", duration_s=used)  # fmt: skip
    # A user-class run gets what is left of half the budget; a monitoring run what is left of 80 % of it.
    chat = reserve(db, caps, run_id="chat")
    assert not isinstance(chat, str) and chat.seconds == pytest.approx(100)
    db.finish_run("chat", status="ok", at=NOW, turns=1, tokens_in=1, tokens_out=1, tokens_cache_read=0, model="m",
                  duration_s=100)  # fmt: skip
    assert reserve(db, caps, run_id="chat2") == "share_reserve:five_hour"
    triage = reserve(db, caps, profile="triage", run_id="triage", budget_class="agent")
    assert not isinstance(triage, str)
    assert triage.seconds == pytest.approx(caps.stop("five_hour") - caps.stop("five_hour", user=True))


def test_chat_at_its_cap_leaves_triage_half_the_stop_line_on_the_default_plan(db: Store) -> None:
    caps = Caps.from_options(Options())
    assert caps.plan == "pro"
    at = NOW - timedelta(hours=1)
    n = 0
    # Chat runs until chat itself is refused for the 5-hour window.
    while not isinstance(chat := reserve(db, caps, run_id=f"c{n}", at=at), str):
        db.finish_run(f"c{n}", status="ok", at=at, turns=1, tokens_in=1, tokens_out=1, tokens_cache_read=0,
                      model="m", duration_s=chat.seconds)  # fmt: skip
        n += 1
    assert chat == "share_reserve:five_hour" and n > 0
    used = db.usage(P, at=NOW)["window"]["seconds"]
    assert caps.stop("five_hour", user=True) - 60 < used <= caps.stop("five_hour", user=True)
    triage = reserve(db, caps, profile="triage", run_id="t", budget_class="agent")
    assert not isinstance(triage, str)
    # Monitoring keeps at least half of the stop line: a full triage reservation (600 s) fits.
    assert caps.stop("five_hour") - used >= caps.stop("five_hour") / 2 and triage.seconds == 600


def test_the_daily_cost_cap_trims_each_run_and_refuses_when_spent(db: Store) -> None:
    caps = dataclasses.replace(Caps.from_options(make_options(limits__daily_max_usd=1.0)), windows=False)
    assert caps.daily_usd == 1.0 and Caps.from_options(Options()).daily_usd is None
    first = reserve(db, caps, run_id="a")
    assert not isinstance(first, str) and first.budget_usd == 0.5
    assert db.add_cost(P.day, 0.7) == pytest.approx(0.7) and db.cost_today(P.day) == pytest.approx(0.7)
    second = reserve(db, caps, run_id="b")
    assert not isinstance(second, str) and second.budget_usd == pytest.approx(0.3)
    db.add_cost(P.day, 0.28)
    assert reserve(db, caps, run_id="c") == "cost_cap:day"
    # A new day starts from nothing; a non-finite or negative cost never counts.
    assert db.add_cost("2026-01-27", float("nan")) == 0.0 and db.add_cost("2026-01-27", -1) == 0.0
    later = NOW + timedelta(days=1)
    assert not isinstance(reserve(db, caps, run_id="d", at=later), str)


@pytest.mark.parametrize("setup", ["seconds_5h", "seconds_7d", "account", "profile_runs"])
def test_an_api_key_skips_the_share_rule_but_not_the_run_caps(db: Store, setup: str) -> None:
    windowed = Caps.from_options(Options())
    caps = Caps.from_options(Options(), windows=False)
    if setup == "profile_runs":
        caps = dataclasses.replace(caps, profile_runs={"chat": 0})
    else:
        # The same history that makes each window cap refuse a subscription run (test_each_cap_refuses).
        seed(db, caps, setup, windowed=windowed)
        assert isinstance(reserve(db, windowed, budget_class="agent"), str)
    res = reserve(db, caps)
    if setup == "profile_runs":
        assert res == "run_cap_reached:chat"
        return
    assert not isinstance(res, str) and res.seconds == caps.seconds_for("chat") and res.budget_usd == 0.5


def test_an_api_key_shows_no_window_caps(db: Store) -> None:
    w = World(db, make_options(claude__plan="max20"))
    view = w.guard.view(NOW)
    assert view["run_time_7d_cap_s"] == round(240 * 3600 * 0.05 * 0.8)
    assert "plan" not in view and not any("share" in key for key in view)
    w.guard._api_key = lambda: True
    assert w.guard.caps().windows is False
    view = w.guard.view(NOW)
    assert view["run_time_5h_cap_s"] is None and view["run_time_7d_cap_s"] is None
    json.dumps(view, allow_nan=False)  # no unbounded value leaks into the sensor or the panel


def test_crashed_runs_keep_their_reservation(db: Store) -> None:
    caps = Caps.from_options(Options())
    reserve(db, caps, run_id="crash")
    assert db.close_stale_runs(at=NOW) == 1
    usage = db.usage(P, at=NOW)
    assert usage["window"]["seconds"] == 180.0 and usage["last_run"]["status"] == "interrupted"


def test_prune_bounds_the_store(db: Store) -> None:
    old = NOW - timedelta(days=120)
    db.issue_action("OPS_DONE_" + "a" * 32, kind="DONE", tag="t", thread_id=None, now=old, expires_at=old)
    db.issue_action("OPS_DONE_" + "b" * 32, kind="DONE", tag="t", thread_id=None, now=NOW,
                    expires_at=NOW + timedelta(days=3))  # fmt: skip
    reserve(db, Caps.from_options(Options()), run_id="old", at=old)
    db.bump(old.date().isoformat(), "chat_turns")
    settled = db.hold_push("{}", title="sent after quiet hours", now=old)
    db.settle_held([settled], to="delivered", outcome="sent", now=old)
    held = db.hold_push("{}", title="still held", now=old)
    fact = db.add_fact("an old fact", thread_id=None, turn_id=None, now=old)
    assert db.decide_fact(fact, "rejected", now=old)
    db.kv_set("evidence:old", "{}", now=old)
    db.kv_set("evidence:fresh", "{}", now=NOW)
    counts = db.prune(now=NOW)
    assert counts["actions"] == 1 and counts["runs"] == 1 and counts["counters"] == 1
    assert db.kv_get(f"{ACTION_KEY}OPS_DONE_{'b' * 32}") != "" and db.count(old.date().isoformat(), "chat_turns") == 0
    assert [h["id"] for h in db.held_pushes(states=("held", "delivered"))] == [held]
    assert db.fact(fact) is None
    # The cloud evidence cache is bounded too.
    assert db.kv_get("evidence:old") == "" and db.kv_get("evidence:fresh") == "{}"


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


RATE_CLAUDE = """#!{python}
import json
def out(x): print(json.dumps(x), flush=True)
out({{"type": "system", "subtype": "init"}})
out({{"type": "rate_limit_event", "rate_limit_info": {{"status": "allowed", "unifiedWindows": {{
    "five_hour": {{"utilization": 20, "resetsAt": 1}}, "seven_day": {{"utilization": 10, "resetsAt": 2}}}}}}}})
out({{"type": "rate_limit_event", "rate_limit_info": {{"status": "{status}", "resetsAt": 1793000000,
    "unifiedWindows": {{"five_hour": {{"utilization": 23, "resetsAt": 1}},
                       "seven_day": {{"utilization": 0.105, "resetsAt": 2}}}}}}}})
out({{"type": "result", "subtype": "success", "is_error": {is_error}, "result": "x", "num_turns": 2}})
"""


@pytest.mark.parametrize(("status", "is_error", "expected"), [("allowed", "False", "ok"),
                                                              ("rejected", "True", "usage_limited")])  # fmt: skip
async def test_stream_rate_limit_events(tmp_path: Path, meminfo: Path, status: str, is_error: str,  # noqa: PLR0917
                                        expected: str, caplog: pytest.LogCaptureFixture) -> None:  # fmt: skip
    caplog.set_level("INFO")
    script = tmp_path / "fake-rate"
    script.write_text(RATE_CLAUDE.format(python=sys.executable, status=status, is_error=is_error))
    script.chmod(0o755)
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(config(tmp_path, str(script)), "p", token="t", transcript_path=tmp_path / "t.gz",
                              redactor=Redactor(), proc_dir=proc_dir, meminfo=meminfo)  # fmt: skip
    # 23 and 0.105 in one payload: a non-integer value makes the whole payload fractions (23 is over the limit).
    assert result.status == expected and reading_unit(result.rate_last) == FRACTION
    assert utilisation(result.rate_last, "seven_day", FRACTION) == pytest.approx(0.105)
    assert utilisation(result.rate_last, "five_hour", FRACTION) == 1.0
    [line] = [r for r in caplog.records if r.getMessage() == "claude run finished"]
    assert line.utilization == {"five_hour": {"value": 23, "type": "int"},  # type: ignore[attr-defined]
                                "seven_day": {"value": 0.105, "type": "float"}}  # fmt: skip
    assert line.utilization_unit == FRACTION  # type: ignore[attr-defined]
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
    assert ai_state == "enabled" and ai["run_time_5h_s"] == 180.0 and ai["run_time_7d_cap_s"] == 5760
    assert ai["account_5h_percent"] is None and "plan" not in ai and not any("share" in key for key in ai)


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


def test_the_utilisation_unit_is_judged_over_the_whole_payload() -> None:
    def info(five: object, seven: object = None) -> dict[str, Any]:
        windows = {"five_hour": {"utilization": five}} | ({"seven_day": {"utilization": seven}} if seven is not None
                                                           else {})  # fmt: skip
        return {"unifiedWindows": windows}

    busy = Caps.from_options(Options()).account_busy
    # Any non-integer value: fractions, for every window of the payload (as seen live: 0.09 is 9 %).
    assert reading_unit(info(0.09)) == FRACTION and utilisation(info(0.09), "five_hour", FRACTION) == 0.09
    assert reading_unit(info(1, 0.4)) == FRACTION and utilisation(info(1, 0.4), "five_hour", FRACTION) == 1.0
    assert (utilisation(info(0.95), "five_hour", FRACTION) or 0) >= busy  # the account is busy: the agent yields
    # Over the limit (a fraction past 1.0) is a full window.
    assert reading_unit(info(1.2)) == FRACTION and utilisation(info(1.2), "five_hour", FRACTION) == 1.0
    # Integers over 1: percents.
    assert reading_unit(info(95, 28)) == PERCENT and utilisation(info(95, 28), "five_hour", PERCENT) == 0.95
    assert utilisation(info(250), "five_hour", PERCENT) == 1.0
    # Every value 0 or 1 (Node writes 1.0 as 1): ambiguous, the caller uses the learned unit.
    for ambiguous in (info(1), info(0, 1), info(1.0), info(0)):
        assert reading_unit(ambiguous) is None
    assert utilisation(info(1), "five_hour", PERCENT) == 0.01 and utilisation(info(1), "five_hour", FRACTION) == 1.0
    for bad in (True, False, "9", float("nan"), float("inf"), -1, -0.5):
        assert utilisation(info(bad), "five_hour", FRACTION) is None and reading_unit(info(bad)) is None


async def test_an_ambiguous_reading_uses_the_unit_learned_last(tmp_path: Path, caplog: pytest.LogCaptureFixture
                                                               ) -> None:  # fmt: skip
    from opsd.runner import AgentRunner

    store = Store.open(tmp_path / "ops.db")
    try:
        runner = AgentRunner.__new__(AgentRunner)
        runner.store = store

        def unit(*values: float) -> str:
            windows = {f"w{i}": {"utilization": v} for i, v in enumerate(values)}
            return runner._unit(RunResult(status="ok", rate_last={"unifiedWindows": windows}))

        caplog.set_level("WARNING")
        assert unit(1) == PERCENT and "unit unknown" in caplog.text  # nothing learned yet: percent, warned
        assert unit(0.3, 0) == FRACTION and store.kv_get(UNIT_KEY) == FRACTION
        assert unit(1, 0) == FRACTION  # ambiguous: the learned unit
        assert unit(40, 1) == PERCENT and unit(1) == PERCENT
    finally:
        store.close()


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


def test_the_agents_investigations_never_use_the_runs_kept_for_plan_a_fix(db: Store) -> None:
    """The investigate cap (6) is unchanged; the agent's own investigations stop 2 short of it, so the admin's Plan a
    fix taps always have runs left. The refusal names the actual limit."""
    from opsd.chatcards import refusal_card

    # No run-time windows (an API key), so only the run caps decide here.
    caps = dataclasses.replace(Caps.from_options(Options()), windows=False)
    for n in range(4):
        assert not isinstance(reserve(db, caps, profile="investigate", run_id=f"inv{n}"), str)
    assert reserve(db, caps, profile="investigate", run_id="inv4") == "run_cap_reached:investigate"
    period = Period.at(NOW, UTC)
    plans = [db.reserve_run(run_id=f"plan{n}", profile="investigate", budget_class="user", period=period, at=NOW,
                            max_turns=15, budget_usd=0.5, caps=caps, owner=True) for n in range(3)]  # fmt: skip
    assert [isinstance(p, str) for p in plans] == [False, False, True] and plans[2] == "run_cap_reached:investigate"
    text = refusal_card("run_cap_reached", "investigate", now=NOW, tz=UTC).text
    assert text.startswith("Today's limit for investigations and Plan a fix is reached (6 runs a day")
    assert "chat runs" not in text
    assert "20 runs a day" in refusal_card("run_cap_reached", "chat", now=NOW, tz=UTC).text


def test_the_maintenance_check_has_its_own_visible_daily_cap() -> None:
    from opsd.budget import DEFAULT_PROFILE_RUNS, RUNS_PER_DAY

    assert RUNS_PER_DAY["maintenance_plan"] == DEFAULT_PROFILE_RUNS == 3
