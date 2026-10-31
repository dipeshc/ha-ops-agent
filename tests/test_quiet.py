"""Quiet hours: windows (wrap-around, DST), what is never held, persistence, coalescing and the push cap."""

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from opsd import quiet
from opsd.channels import Action, ActionKind, BudgetClass, Message, agent_message
from opsd.daemon import Daemon
from tests.conftest import FakeHA
from tests.test_daemon import Harness

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture
def h(paths: Any, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


def at(text: str, tz: ZoneInfo = BERLIN) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=tz).astimezone(UTC)


# ----------------------------------------------------------------- window


def test_window_parsing_and_off() -> None:
    assert quiet.window("22:30", "07:00") == quiet.Window(time(22, 30), time(7, 0))
    for start, end in (("", "07:00"), ("22:30", ""), ("7:00", "08:00"), ("24:00", "07:00"), ("07:00", "07:00")):
        assert quiet.window(start, end) is None
    assert not quiet.is_quiet(None, datetime.now(UTC), UTC)


@pytest.mark.parametrize(
    ("local", "expected"),
    [("2026-01-28T22:29", False), ("2026-01-28T22:30", True), ("2026-01-28T23:59", True),
     ("2026-01-29T00:00", True), ("2026-01-29T06:59", True), ("2026-01-29T07:00", False),
     ("2026-01-29T12:00", False)],
)  # fmt: skip
def test_wrap_around_window(local: str, expected: bool) -> None:
    assert quiet.is_quiet(quiet.window("22:30", "07:00"), at(local), BERLIN) is expected


def test_same_day_window_and_end_times() -> None:
    win = quiet.window("13:00", "15:00")
    assert win is not None
    assert quiet.is_quiet(win, at("2026-01-28T14:00"), BERLIN) and not quiet.is_quiet(
        win, at("2026-01-28T15:00"), BERLIN
    )
    night = quiet.window("22:30", "07:00")
    assert night is not None
    assert quiet.ends_at(night, at("2026-01-28T23:00"), BERLIN) == at("2026-01-29T07:00")
    assert quiet.ends_at(night, at("2026-01-29T03:00"), BERLIN) == at("2026-01-29T07:00")


def test_dst_days_keep_the_local_wall_clock() -> None:
    night = quiet.window("22:30", "07:00")
    assert night is not None
    # Fall back (31 Oct 2027 in Berlin): the night is 9.5 hours long, it still ends at 07:00 CET.
    end = quiet.ends_at(night, at("2027-10-30T23:00"), BERLIN)
    assert end == datetime(2027, 10, 31, 6, 0, tzinfo=UTC) and end.astimezone(BERLIN).hour == 7
    assert quiet.is_quiet(night, datetime(2027, 10, 31, 1, 30, tzinfo=UTC), BERLIN)  # 02:30 CET, the repeated hour
    # Spring forward (28 Mar 2027): ends at 07:00 CEST.
    end = quiet.ends_at(night, at("2027-03-27T23:00"), BERLIN)
    assert end == datetime(2027, 3, 28, 5, 0, tzinfo=UTC) and end.astimezone(BERLIN).hour == 7


def test_what_is_never_held() -> None:
    plain = Message("t", "b")
    assert not quiet.bypass(plain)
    for message in (Message("t", "b", urgent=True), Message("t", "b", budget_class=BudgetClass.SAFETY),
                    Message("t", "b", quiet_ok=True)):  # fmt: skip
        assert quiet.bypass(message)


def test_messages_round_trip_through_the_store_format() -> None:
    message = Message("Reminder: x", "body", tag="ha-ops-agent-task-1", thread_id="a" * 16,
                      actions=(Action(ActionKind.DONE, "Done"), Action(ActionKind.REPLY, "Reply", text_input=True)),
                      budget_class=BudgetClass.USER, url="/t/x", action_ttl=timedelta(hours=2))  # fmt: skip
    assert quiet.from_json(quiet.to_json(message)) == message


# ----------------------------------------------------------------- daemon


def around_now(d: Daemon, *, covering: bool) -> dict[str, str]:
    now = datetime.now(UTC)
    start = now - timedelta(hours=1) if covering else now + timedelta(hours=2)
    end = now + timedelta(hours=1) if covering else now + timedelta(hours=3)
    return {"quiet_start": start.strftime("%H:%M"), "quiet_end": end.strftime("%H:%M")}


def daemon(h: Harness, *, covering: bool) -> Daemon:
    probe = h.daemon()
    options = around_now(probe, covering=covering)
    d = h.daemon(**{f"notify__{key}": value for key, value in options.items()})
    d.tz = UTC
    return d


async def test_non_urgent_pushes_are_held_and_urgent_ones_are_not(h: Harness) -> None:
    d = daemon(h, covering=True)
    before = len(h.pushes())
    assert await d.send_now(agent_message("Reply", "the fridge is fine", urgent=False)) == "held"
    assert await d.send_now(Message("Ops Agent: updates", "plan", budget_class=BudgetClass.AGENT)) == "held"
    assert await d.send_now(Message("Ops Agent: critical", "core down", urgent=True,
                                    budget_class=BudgetClass.SAFETY)) == "sent"  # fmt: skip
    assert len(h.pushes()) == before + 1
    view = d.held_view(datetime.now(UTC))
    assert view is not None and view["count"] == 2 and view["until"]


async def test_reminders_the_admin_set_ring_but_agent_ones_wait(h: Harness) -> None:
    d = daemon(h, covering=True)
    assert d.store is not None and d.assistant is not None
    now = datetime.now(UTC)
    mine, _ = d.store.create_task(kind="reminder", title="take the bins out", owner="admin", created_by="chat:x",
                                  next_fire_at=now, payload={}, idempotency_key="r1", now=now)  # fmt: skip
    agents, _ = d.store.create_task(kind="reminder", title="check batteries", owner="agent", created_by="triage:x",
                                    next_fire_at=now, payload={}, idempotency_key="r2", now=now)  # fmt: skip
    assert await d.assistant.deliver_reminder(mine, now) == "sent"
    assert await d.assistant.deliver_reminder(agents, now) == "held"


async def test_held_pushes_survive_a_restart_and_go_out_one_each(h: Harness) -> None:
    d = daemon(h, covering=True)
    for title in ("Reply", "Follow-up: fridge", "Reminder: batteries"):
        assert await d.send_now(agent_message(title, "x", urgent=False)) == "held"
    d.close()
    later = daemon(h, covering=False)  # restarted after quiet hours
    before = len(h.pushes())
    assert await later.flush_held(datetime.now(UTC)) == "sent"
    titles = [p["title"] for p in h.pushes()[before:]]
    assert titles == ["Agent: Reply", "Agent: Follow-up: fridge", "Agent: Reminder: batteries"]
    assert await later.flush_held(datetime.now(UTC)) is None  # nothing left
    view = later.held_view(datetime.now(UTC))
    assert view is not None and view["count"] == 0


async def test_one_held_push_goes_out_unchanged_and_nothing_flushes_during_quiet_hours(h: Harness) -> None:
    d = daemon(h, covering=True)
    await d.send_now(agent_message("Reply", "exact text", urgent=False))
    assert await d.flush_held(datetime.now(UTC)) is None  # still quiet
    d.options = after_quiet(d)
    before = len(h.pushes())
    assert await d.flush_held(datetime.now(UTC)) == "sent"
    [push] = h.pushes()[before:]
    assert push["title"] == "Agent: Reply" and push["message"] == "exact text"


def after_quiet(d: Daemon) -> Any:
    """The same options with a window that does not cover now (quiet hours are over)."""
    return dataclasses.replace(d.options, notify=dataclasses.replace(d.options.notify, **around_now(d, covering=False)))


async def test_the_push_cap_leaves_overflow_in_the_panel(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    d = daemon(h, covering=True)
    await d.send_now(agent_message("Reply", "a", urgent=False))
    await d.send_now(agent_message("Follow-up", "b", urgent=False))
    d.options = after_quiet(d)
    monkeypatch.setattr(d.channel, "send", lambda message, **kw: "rate_limited")
    assert await d.flush_held(datetime.now(UTC)) == "rate_limited"
    view = d.held_view(datetime.now(UTC))
    assert view is not None and view["count"] == 0 and view["undelivered"] == ["Agent: Reply", "Agent: Follow-up"]
    # A failed send is tried again later instead.
    retry = daemon(h, covering=True)
    await retry.send_now(agent_message("Reply", "c", urgent=False))
    retry.options = after_quiet(retry)
    monkeypatch.setattr(retry.channel, "send", lambda message, **kw: "failed")
    assert await retry.flush_held(datetime.now(UTC)) == "failed"
    view = retry.held_view(datetime.now(UTC))
    assert view is not None and view["count"] == 1


async def test_the_panel_state_reports_held_pushes(h: Harness) -> None:
    from opsd.panelapi import PanelHandlers

    d = daemon(h, covering=True)
    await d.send_now(agent_message("Reply", "a", urgent=False))
    state = PanelHandlers(d).state()
    assert state["held"]["count"] == 1 and state["held"]["until"]


def test_options_schema_validates_hh_mm() -> None:
    from tests.conftest import ADDON_ROOT

    config = (ADDON_ROOT / "config.yaml").read_text()
    assert 'quiet_start: "22:30"' in config and 'quiet_end: "07:00"' in config
    assert "quiet_start: match(^(([01][0-9]|2[0-3]):[0-5][0-9])?$)" in config
    assert "quiet_end: match(^(([01][0-9]|2[0-3]):[0-5][0-9])?$)" in config


# ------------------------------------------------- configcheck event_data


def test_event_data_is_skipped_only_in_event_triggers() -> None:
    from opsd import configcheck

    ok = ("alias: R\ntrigger:\n  - platform: event\n    event_type: x\n    event_data:\n      action: released\n"
          "action:\n  - action: light.turn_off\n")  # fmt: skip
    configcheck.check_file("automations/r.yaml", ok, entities=frozenset(), admin_notify="m")
    hidden = ("alias: R\ntrigger:\n  - platform: state\n    entity_id: sensor.x\naction:\n  - action: light.turn_on\n"
              "    data:\n      event_data:\n        device_id: abc\n")  # fmt: skip
    with pytest.raises(configcheck.ConfigRefused, match="device_id"):
        configcheck.check_file("automations/r.yaml", hidden, entities=frozenset(), admin_notify="m")


@pytest.mark.parametrize(
    ("start", "end", "on"),
    [("22:30", "07:00", True), ("20:00", "08:00", True), ("19:59", "08:00", False), ("08:00", "21:00", False),
     ("08:00", "20:00", True), ("", "", False)],
)  # fmt: skip
def test_windows_longer_than_12_hours_are_refused(start: str, end: str, on: bool) -> None:
    assert (quiet.window(start, end) is not None) is on
    assert (quiet.problem(start, end) is not None) is (not on and bool(start))


def test_a_too_long_window_disables_quiet_hours_with_a_warning(h: Harness, caplog: pytest.LogCaptureFixture) -> None:
    d = h.daemon(notify__quiet_start="18:00", notify__quiet_end="09:00")
    assert d.quiet_window() is None
    assert any("quiet hours disabled" in r.getMessage() for r in caplog.records)


async def test_kill_switch_notices_are_safety_pushes_never_held(h: Harness) -> None:
    d = daemon(h, covering=True)
    assert d.usage is not None
    before = len(h.pushes())
    assert await d.usage.notice("switch_refused", "Ops Agent: AI kept off", "x") == "sent"
    assert await d.usage.notice("switch_refused", "Ops Agent: AI kept off", "x") is None  # once a day
    assert [p["title"] for p in h.pushes()[before:]] == ["Ops Agent: AI kept off"]
    view = d.held_view(datetime.now(UTC))
    assert view is not None and view["count"] == 0
