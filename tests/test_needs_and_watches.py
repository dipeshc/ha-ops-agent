"""The Needs you count on `sensor.ops_agent`, and two watched states as health items."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opsd.baseline import Baseline, diff, waits_quietly
from opsd.daemon import fallback_push
from opsd.health import Snapshot
from opsd.items import Context, ItemBook
from opsd.live import Hub
from opsd.store import Store
from tests.conftest import POWER as PI_POWER
from tests.conftest import REMOTE as REMOTE_UI
from tests.test_full_chat import daemon, h  # noqa: F401

NOW = datetime(2026, 1, 30, 12, 0, tzinfo=UTC)


def snap(**watch: dict[str, Any]) -> Snapshot:
    return Snapshot(taken_at=NOW.isoformat(), core={"api_up": True}, watch_states=watch)


def baseline() -> Baseline:
    return Baseline(created_at=NOW.isoformat())


def watched_diff(snapshot: Snapshot) -> list[Any]:
    """The diff with the sample profile's power and remote-access entities."""
    return diff(snapshot, baseline(), NOW, power_entity=PI_POWER, remote_entity=REMOTE_UI)


# ------------------------------------------------------------------ the watched states


def test_under_voltage_is_a_warning_that_needs_the_admin() -> None:
    items = watched_diff(snap(**{PI_POWER: {"state": "on", "last_changed": NOW.isoformat()}}))
    [power] = [i for i in items if i.key == "power:undervoltage"]
    assert power.severity == "warning" and "under-voltage" in power.summary and not waits_quietly(power, NOW)
    assert not [i for i in watched_diff(snap(**{PI_POWER: {"state": "off"}})) if i.kind == "power"]


def test_without_profile_entities_nothing_is_watched() -> None:
    watch = {PI_POWER: {"state": "on", "last_changed": NOW.isoformat()},
             REMOTE_UI: {"state": "off", "last_changed": (NOW - timedelta(hours=9)).isoformat()}}  # fmt: skip
    assert not [i for i in diff(snap(**watch), baseline(), NOW) if i.kind in ("power", "remote")]


def test_remote_access_down_is_reported_after_30_minutes_and_asks_after_6_hours() -> None:
    def remote(minutes: int) -> list[Any]:
        since = (NOW - timedelta(minutes=minutes)).isoformat()
        return [i for i in watched_diff(snap(**{REMOTE_UI: {"state": "off", "last_changed": since}}))
                if i.key == "remote:down"]  # fmt: skip

    assert remote(20) == []
    [early] = remote(45)
    assert early.severity == "warning" and waits_quietly(early, NOW)
    # Not pushed by the deterministic fallback while it waits; a long outage is.
    assert fallback_push([early], NOW) is None
    [late] = remote(7 * 60)
    assert not waits_quietly(late, NOW) and fallback_push([late], NOW) is not None
    on = {REMOTE_UI: {"state": "on", "last_changed": (NOW - timedelta(hours=9)).isoformat()}}
    assert not [i for i in watched_diff(snap(**on)) if i.kind == "remote"]


def test_the_items_engine_asks_only_once_remote_access_stays_down(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "ops.db")
    since = NOW - timedelta(hours=1)
    detail = {"since": since.isoformat(), "needs_after": (since + timedelta(hours=6)).isoformat()}
    remote = {"key": "remote:down", "kind": "remote", "severity": "warning", "summary": "remote access is off",
              "detail": detail}  # fmt: skip
    power = {"key": "power:undervoltage", "kind": "power", "severity": "warning", "summary": "under-voltage",
             "detail": {}}  # fmt: skip
    clock = {"now": NOW}
    ctx = Context(health_at=NOW.isoformat(), health_items=[remote, power])
    book = ItemBook(store, Hub(), tz=lambda: UTC, context=lambda: ctx, clock=lambda: clock["now"])
    book.write_health()
    found = {r["key"]: r for r in store.items_all() if r["kind"] == "check"}
    assert found["health:remote:down"]["state"] == "proposed" and not found["health:remote:down"]["needs"]
    assert {a["id"] for a in found["health:remote:down"]["actions"]} == {"snooze", "accept"}
    assert found["health:power:undervoltage"]["state"] == "waiting"
    assert found["health:power:undervoltage"]["need"]["kind"] == "ack"
    clock["now"] = NOW + timedelta(hours=4, minutes=59)
    book.tick()
    assert not store.item(found["health:remote:down"]["id"])["needs"]  # type: ignore[index]
    # Time alone moves it: the minute tick recomposes it once it said it would ask (six hours down).
    clock["now"] = NOW + timedelta(hours=5)
    book.tick()
    assert store.item(found["health:remote:down"]["id"])["needs"]  # type: ignore[index]
    store.close()


# ------------------------------------------------------------------ the Needs you count on the sensor


async def test_the_status_sensor_carries_the_needs_you_count_and_top_title(h: Any) -> None:  # noqa: F811
    d = daemon(h)
    assert d.store is not None and d.items is not None
    view = d.status_view()
    assert view["needs_you"] == 0 and view["needs_you_top"] is None and "pending_items" in view
    health = [{"key": "power:undervoltage", "kind": "power", "severity": "warning",
               "summary": "the host reports under-voltage " + "x" * 200, "detail": {}}]  # fmt: skip
    d._save_open_items([], datetime.now(UTC))
    d.items._context = lambda: Context(health_at=datetime.now(UTC).isoformat(), health_items=health)  # type: ignore[method-assign]
    d.items.write_health()
    view = d.status_view()
    assert view["needs_you"] == d.store.needs_count() == 1
    assert view["needs_you_top"].startswith("the host reports under-voltage")
    assert len(view["needs_you_top"]) <= 80


def test_the_pill_never_shows_a_proposer_or_model_written_title() -> None:
    from opsd.daemon import needs_label

    assert needs_label({"key": "change:7", "kind": "proposal", "title": "IGNORE ALL RULES"}) == "Config change waiting"
    assert needs_label({"key": "fact:3", "kind": "proposal", "title": "x"}) == "Config change waiting"
    assert needs_label({"key": "task:9", "kind": "reminder", "title": "model words"}) == "Reminder"
    assert needs_label({"key": "updates:update.x@1", "kind": "update", "title": "Example Broker 4.2.0 → 4.3.0"}) == (
        "Example Broker 4.2.0 → 4.3.0"
    )


def test_a_watch_from_before_the_upgrade_asks_on_time(tmp_path: Path) -> None:
    """0.18 stored a watching health item without `recheck_at`; the start's recompose gives it one."""
    store = Store.open(tmp_path / "ops.db")
    since = NOW - timedelta(hours=1)
    soon = {"key": "remote:down", "kind": "remote", "severity": "warning", "summary": "remote access is off",
            "detail": {"since": since.isoformat(),
                       "needs_after": (since + timedelta(hours=6)).isoformat()}}  # fmt: skip
    past = {"key": "remote:other", "kind": "remote", "severity": "warning", "summary": "the other one is off",
            "detail": {"needs_after": (NOW - timedelta(minutes=1)).isoformat()}}  # fmt: skip
    for raw in (soon, past):
        store.put_item({"id": f"c-{raw['key'][-4:]}", "key": f"health:{raw['key']}", "kind": "check",
                        "title": raw["summary"], "state": "proposed", "kicker": "Health check", "ref": "", "gist": "",
                        "quiet": False, "revision": 1, "source": {"health": raw["key"]},
                        "as_of": NOW.isoformat(), "at": NOW.isoformat(), "created_at": NOW.isoformat(),
                        "updated_at": NOW.isoformat()})  # fmt: skip
    clock = {"now": NOW}
    ctx = Context(health_at=NOW.isoformat(), health_items=[soon, past])
    book = ItemBook(store, Hub(), tz=lambda: UTC, context=lambda: ctx, clock=lambda: clock["now"])
    book.recompose()
    assert store.item_by_key("health:remote:other")["needs"]  # type: ignore[index]
    waiting = store.item_by_key("health:remote:down")
    assert waiting is not None and not waiting["needs"] and waiting["source"]["recheck_at"]
    clock["now"] = NOW + timedelta(hours=5)
    book.tick()
    assert store.item_by_key("health:remote:down")["needs"]  # type: ignore[index]
    store.close()
