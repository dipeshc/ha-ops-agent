"""`ha.history` (`opsctl history`): read-only trends, bounded, no attributes beyond unit and name."""

import dataclasses
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from opsd import history as history_mod
from opsd.capabilities import build_parser
from opsd.config import Paths
from opsd.runner import CHAT, FOLLOWUP, SUMMARY, TRIAGE
from opsd.supervisor import ApiError
from opsd.ws import CommandError
from tests.conftest import make_options
from tests.test_rpc_effects import World

NOW = datetime.now(UTC)


class FakeCore:
    def __init__(self) -> None:
        self.states: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.statistics: dict[str, list[dict[str, Any]]] = {}
        self.registry: dict[str, dict[str, Any]] = {}
        self.max_bytes: int | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def core(self, path: str) -> Any:
        entity = path.removeprefix("/states/")
        if entity not in self.states:
            raise ApiError("GET", path, 404, "not found")
        return self.states[entity]

    async def ws(self, msg_type: str, **fields: Any) -> Any:
        if msg_type == "config/entity_registry/get":
            if fields["entity_id"] not in self.registry:
                raise CommandError("not_found: Entity not found")
            return self.registry[fields["entity_id"]]
        self.calls.append((msg_type, fields))
        if msg_type == "recorder/statistics_during_period":
            return {k: v for k, v in self.statistics.items() if k in fields["statistic_ids"]}
        assert msg_type == "history/history_during_period" and fields["no_attributes"] is True
        return {k: v for k, v in self.history.items() if k in fields["entity_ids"]}


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    fake = FakeCore()

    class Live:
        def __init__(self, _client: Any, *, max_bytes: int | None = None) -> None:
            fake.max_bytes = max_bytes

        async def __aenter__(self) -> FakeCore:
            return fake

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(history_mod, "LiveAPI", Live)
    return fake


@pytest.fixture
def world(paths: Paths, core: FakeCore) -> Iterator[World]:
    w = World(paths)
    w.env = w.server._env = dataclasses.replace(w.env, client=core)  # type: ignore[arg-type]
    w.run(allowed=CHAT.capabilities)
    yield w
    w.store.close()


async def history(world: World, **args: Any) -> Any:
    response = await world.call("history", **args)
    return json.loads(response["output"]) if response["ok"] else response


def changes(n: int, *, start: datetime = NOW - timedelta(hours=20), state: Any = None) -> list[dict[str, Any]]:
    step = timedelta(hours=19) / max(1, n)
    return [{"s": str(state if state is not None else 20 + i % 5), "lu": (start + i * step).timestamp()}
            for i in range(n)]  # fmt: skip


def test_every_profile_that_answers_can_read_history() -> None:
    for profile in (CHAT, FOLLOWUP, TRIAGE):
        assert "ha.history" in profile.capabilities
    assert "ha.history" not in SUMMARY.capabilities
    parsed = build_parser().parse_args(["history", "sensor.fridge", "--window", "7d", "--mode", "daily"])
    assert (parsed.verb, parsed.entity, parsed.window, parsed.mode) == ("history", "sensor.fridge", "7d", "daily")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"entity": "sensor.x", "window": "32d"}, "between 1h and 31d"),
        ({"entity": "sensor.x", "window": "8d"}, "raw is limited to 7d"),
        ({"entity": "sensor.x", "window": "0h"}, "bad request"),
        ({"entity": "sensor.x", "window": "1w"}, "bad request"),
        ({"entity": "sensor.x", "window": "30d", "mode": "hourly"}, "use daily"),
        ({"entity": "sensor.x", "mode": "weekly"}, "bad request"),
        ({"entity": "Sensor.X"}, "bad request"),
        ({"entity": "sensor.x; rm -rf /"}, "bad request"),
        ({"entity": "../../etc/passwd"}, "bad request"),
        ({}, "entity is required"),
    ],
)
async def test_arguments_are_validated(world: World, args: dict[str, Any], error: str) -> None:
    response = await world.call("history", **args)
    assert not response["ok"] and error in response["error"]


async def test_callers_outside_a_run_are_refused(world: World, core: FakeCore) -> None:
    world.server.end_run()
    core.states["sensor.x"] = {"state": "1"}
    response = await world.call("history", entity="sensor.x")
    assert not response["ok"] and "only available inside an agent run" in response["error"]
    assert core.calls == []


async def test_raw_changes_are_thinned_to_500_keeping_the_ends(world: World, core: FakeCore) -> None:
    core.states["sensor.fridge"] = {"state": "4.1", "attributes": {"unit_of_measurement": "°C",
                                    "friendly_name": "Fridge", "api_key": "secret-xyz"}}  # fmt: skip
    core.history["sensor.fridge"] = changes(1200)
    out = await history(world, entity="sensor.fridge", window="1d")
    assert out["total_changes"] == 1200 and len(out["points"]) == 500 and out["thinned"] is True
    first = datetime.fromtimestamp(core.history["sensor.fridge"][0]["lu"], UTC).strftime("%Y-%m-%dT%H:%M")
    assert out["points"][0][0] == first and out["points"][-1][1] == core.history["sensor.fridge"][-1]["s"]
    assert out["unit"] == "°C" and out["name"] == "Fridge" and "secret-xyz" not in json.dumps(out)
    assert set(out) == {"entity_id", "state", "unit", "name", "window", "mode", "total_changes", "points", "thinned"}


@pytest.mark.parametrize(
    ("entity", "attrs"),
    [
        ("camera.porch", {"access_token": "tok-123", "entity_picture": "/api/camera_proxy/camera.porch?token=tok-123"}),
        ("person.alex", {"latitude": 52.1, "longitude": 4.2, "gps_accuracy": 17.25}),
        ("device_tracker.phone", {"latitude": 52.1, "longitude": 4.2, "friendly_name": "Phone"}),
        ("media_player.tv", {"media_title": "x", "entity_picture": "/api/media_player_proxy/x?token=t"}),
    ],
)
async def test_state_only_domains_never_return_attributes(
    world: World, core: FakeCore, entity: str, attrs: dict[str, Any]
) -> None:
    core.states[entity] = {"state": "home", "attributes": attrs}
    core.history[entity] = [{"s": "home", "lu": NOW.timestamp() - 60, "a": attrs}]
    out = await history(world, entity=entity, window="6h")
    text = json.dumps(out)
    assert out["state"] == "home" and "unit" not in out and "name" not in out
    for key, value in attrs.items():
        assert key not in text and str(value) not in text


async def test_missing_entity(world: World) -> None:
    out = await history(world, entity="sensor.nope")
    assert "not found" in out["error"]


async def test_statistics_first_then_history_fallback(world: World, core: FakeCore) -> None:
    core.states["sensor.power"] = {"state": "120", "attributes": {"unit_of_measurement": "W"}}
    day = (NOW - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    core.statistics["sensor.power"] = [{"start": day.timestamp() * 1000, "mean": 101.23456, "min": 3.0, "max": 950.0}]
    out = await history(world, entity="sensor.power", window="7d", mode="daily")
    assert core.max_bytes == history_mod.MAX_WS_BYTES
    assert out["source"] == "statistics" and out["periods"] == [
        {"start": day.astimezone(UTC).strftime("%Y-%m-%dT%H:%M"), "mean": 101.2, "min": 3.0, "max": 950.0}
    ]
    assert [c[0] for c in core.calls] == ["recorder/statistics_during_period"]
    # No long-term statistics (no state_class): computed from the history instead.
    core.calls.clear()
    core.states["binary_sensor.door"] = {"state": "off"}
    core.history["binary_sensor.door"] = changes(10, state="on") + changes(30, start=NOW - timedelta(hours=40),
                                                                           state="off")  # fmt: skip
    out = await history(world, entity="binary_sensor.door", window="2d", mode="daily")
    assert (
        out["source"] == "history (sample mean)" and [c[0] for c in core.calls][-1] == "history/history_during_period"
    )
    assert sum(p["n"] for p in out["periods"]) == 40
    assert all(0.0 <= p["mean"] <= 1.0 for p in out["periods"])
    core.states["sensor.mode"] = {"state": "eco"}
    core.history["sensor.mode"] = changes(5, state="eco")
    out = await history(world, entity="sensor.mode", window="1d", mode="hourly")
    assert "not numeric or binary" in out["error"]


async def test_output_stays_bounded(world: World, core: FakeCore) -> None:
    core.states["sensor.text"] = {"state": "x"}
    core.history["sensor.text"] = changes(2000, state="y" * 500)
    response = await world.call("history", entity="sensor.text", window="1d")
    out = json.loads(response["output"])
    assert len(response["output"]) <= history_mod.MAX_JSON_CHARS + 2000
    assert all(len(state) <= history_mod.MAX_STATE_CHARS for _, state in out["points"])
    assert out["thinned"] is True


def test_capability_tools_use_the_house_timezone(paths: Paths) -> None:
    from zoneinfo import ZoneInfo

    from opsd.daemon import Daemon
    from tests.test_daemon import FakeClient

    d = Daemon(paths, make_options(notify__quiet_start=""), FakeClient())  # type: ignore[arg-type]
    try:
        d.require_privsep = False
        d.tz = ZoneInfo("Europe/Berlin")
        assert d.rpc_server()._env.tz() == ZoneInfo("Europe/Berlin")
    finally:
        d.close()


@pytest.mark.parametrize(
    "entity",
    [
        "input_text.alarm_code",
        "text.note",
        "calendar.family",
        "todo.shopping",
        "conversation.home_assistant",
        "sensor.admin_phone_geocoded_location",
        "sensor.phone_last_notification",
        "sensor.phone_wifi_ssid",
        "sensor.phone_wifi_bssid",
        "sensor.router_public_ip",
        "sensor.nas_ip_address",
        "sensor.tablet_last_used_app",
    ],
)
async def test_privacy_sensitive_entities_are_refused(world: World, core: FakeCore, entity: str) -> None:
    core.states[entity] = {"state": "secret value 1234"}
    core.history[entity] = [{"s": "secret value 1234", "lu": NOW.timestamp() - 60}]
    out = await history(world, entity=entity, window="6h")
    assert out == {"error": f"{entity} is not available to the agent (privacy)"}
    assert core.calls == []


async def test_phone_sensors_are_refused_except_battery_and_storage(world: World, core: FakeCore) -> None:
    for entity in ("sensor.admin_phone_activity", "sensor.admin_phone_battery_level"):
        core.registry[entity] = {"entity_id": entity, "platform": "mobile_app"}
        core.states[entity] = {"state": "Walking" if "activity" in entity else "81",
                               "attributes": {"unit_of_measurement": "%"}}  # fmt: skip
        core.history[entity] = [{"s": core.states[entity]["state"], "lu": NOW.timestamp() - 60}]
    out = await history(world, entity="sensor.admin_phone_activity", window="6h")
    assert "privacy" in out["error"] and "Walking" not in json.dumps(out)
    out = await history(world, entity="sensor.admin_phone_battery_level", window="6h")
    assert out["state"] == "81" and out["unit"] == "%"


def test_entities_lookup_uses_the_same_rules(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd import queries
    from opsd.capabilities import Args

    states = [
        {"entity_id": "person.alex", "state": "home", "attributes": {"latitude": 52.4, "longitude": 4.1,
         "entity_picture": "/x?token=t"}, "last_changed": "2026-01-27T00:00:00+00:00"},
        {"entity_id": "sensor.admin_phone_geocoded_location", "state": "1 Main St", "attributes": {}},
        {"entity_id": "sensor.admin_phone_steps", "state": "4021", "attributes": {}},
        {"entity_id": "sensor.fridge", "state": "4.1", "attributes": {"unit_of_measurement": "°C",
         "friendly_name": "Fridge", "api_key": "k-123"}},
    ]  # fmt: skip
    registry = [{"entity_id": "sensor.admin_phone_steps", "platform": "mobile_app"},
                {"entity_id": "sensor.admin_phone_geocoded_location", "platform": "mobile_app"}]  # fmt: skip

    class Client:
        def core(self, _path: str) -> Any:
            return states

    async def ws(_client: Any, msg_type: str, **_: Any) -> Any:
        assert msg_type == "config/entity_registry/list"
        return registry

    monkeypatch.setattr(queries, "_ws", ws)
    ctx: Any = type("Ctx", (), {"client": Client()})()

    def one(entity: str) -> Any:
        return queries.cmd_entities(ctx, Args({"entity": entity, "platform": None, "unavailable": False,
                                               "limit": 10}))  # fmt: skip

    person = one("person.alex")
    assert person == {"entity_id": "person.alex", "state": "home", "last_changed": "2026-01-27T00:00:00+00:00"}
    assert "privacy" in one("sensor.admin_phone_geocoded_location")["error"]
    assert "privacy" in one("sensor.admin_phone_steps")["error"]
    assert one("sensor.fridge") == {"entity_id": "sensor.fridge", "state": "4.1", "unit": "°C", "name": "Fridge"}
    listed = queries.cmd_entities(ctx, Args({"entity": None, "platform": "mobile_app", "unavailable": False,
                                             "limit": 10}))  # fmt: skip
    assert listed["count"] == 2 and {r["state"] for r in listed["entities"]} == {"(private)"}
