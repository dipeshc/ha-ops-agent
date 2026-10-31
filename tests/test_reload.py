"""integration.reload: allowlist, denylist, limits, policy, quota and the deadline."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import reload as reload_mod
from opsd.capabilities import CAPABILITIES, CapabilityError
from opsd.config import Paths
from opsd.policy import CHAT_EFFECTS, Effect, Origin, PolicyTier, decide
from opsd.runctx import Principal
from opsd.runner import CHAT, FOLLOWUP, INVESTIGATE, SUMMARY, TRIAGE
from opsd.store import Store
from tests.conftest import GENERIC, SAMPLE_PROFILE, make_options
from tests.test_daemon import Harness, h  # noqa: F401
from tests.test_rpc_effects import World

NOW = datetime(2026, 1, 29, 12, 0, tzinfo=UTC)
CAP = CAPABILITIES["integration.reload"]
ALLOWED = frozenset({"acme_vacuum", "kasa", "oddlock", "gdo", "blinds", "plugs", "sleepy", "weird"})


class FakeReloadAPI:
    """Realistic shapes: the registry list has no device_class; states carry it in attributes."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = [
            {"entry_id": "rr1", "domain": "acme_vacuum", "title": "Acme Vacuum", "state": "loaded"},
            {"entry_id": "ka1", "domain": "kasa", "title": "Kasa", "state": "loaded"},
            {"entry_id": "ble1", "domain": "nuki", "title": "Front", "state": "loaded"},
            {"entry_id": "odd1", "domain": "oddlock", "title": "Odd", "state": "loaded"},
            {"entry_id": "gar1", "domain": "gdo", "title": "Garage", "state": "loaded"},
            {"entry_id": "bl1", "domain": "blinds", "title": "Blinds", "state": "loaded"},
            {"entry_id": "pl1", "domain": "plugs", "title": "Plugs", "state": "loaded"},
            {"entry_id": "off1", "domain": "sleepy", "title": "Off", "state": "loaded", "disabled_by": "user"},
        ]
        self.registry: list[dict[str, Any]] = [
            {"entity_id": "vacuum.robot_vacuum", "platform": "acme_vacuum", "config_entry_id": "rr1"},
            {"entity_id": "sensor.robot_vacuum_battery", "platform": "acme_vacuum", "config_entry_id": "rr1"},
            {"entity_id": "light.kitchen", "platform": "kasa", "config_entry_id": "ka1"},
            {"entity_id": "lock.front_door", "platform": "nuki", "config_entry_id": "ble1"},
            {"entity_id": "lock.side_gate", "platform": "oddlock", "config_entry_id": "odd1"},
            {"entity_id": "cover.big_door", "platform": "gdo", "config_entry_id": "gar1"},
            {"entity_id": "cover.living_room_blind", "platform": "blinds", "config_entry_id": "bl1"},
            {"entity_id": "switch.coffee", "platform": "plugs", "config_entry_id": "pl1"},
        ]
        self.states: dict[str, dict[str, Any]] = {
            "vacuum.robot_vacuum": {"state": "docked"},
            "sensor.robot_vacuum_battery": {"state": "unavailable"},
            "light.kitchen": {"state": "on"},
            "lock.front_door": {"state": "locked"},
            "lock.side_gate": {"state": "locked"},
            "cover.big_door": {"state": "closed", "attributes": {"device_class": "garage"}},
            "cover.living_room_blind": {"state": "open", "attributes": {"device_class": "blind"}},
            "switch.coffee": {"state": "off", "attributes": {"friendly_name": "Coffee"}},
        }
        self.posts: list[str] = []
        self.recover = True
        self.post_fails = False
        self.post_delay = 0.0
        self.ws_fails = False

    async def ws(self, msg_type: str, **_: Any) -> Any:
        if self.ws_fails:
            raise OSError("websocket closed")
        return {"config_entries/get": self.entries, "config/entity_registry/list": self.registry}[msg_type]

    async def core_rest(self, path: str) -> Any:
        assert path == "/states"
        return [{"entity_id": e, "attributes": {}, **s} for e, s in self.states.items()]

    async def core_post(self, path: str) -> Any:
        self.posts.append(path)
        if self.post_delay:
            await asyncio.sleep(self.post_delay)
        if self.post_fails:
            raise RuntimeError("500")
        if self.recover:
            for s in self.states.values():
                if s["state"] == "unavailable":
                    s["state"] = "on"
        return {"require_restart": False}


async def _no_sleep(_: float) -> None:
    return None


_STORES: dict[Path, Store] = {}


def store_of(paths: Paths) -> Store:
    """The store holding the reload rate state (one per test's data directory)."""
    found = _STORES.get(paths.data)
    if found is None:
        found = _STORES[paths.data] = Store.open(paths.data / "ops.db")
    return found


@pytest.fixture(autouse=True)
def _reset_inflight() -> Iterator[None]:
    reload_mod._INFLIGHT.clear()
    yield
    reload_mod._INFLIGHT.clear()
    while _STORES:
        _STORES.popitem()[1].close()


@pytest.fixture
def api() -> FakeReloadAPI:
    return FakeReloadAPI()


async def reload(api: FakeReloadAPI, paths: Paths, domain: str, **kw: Any) -> dict[str, Any]:
    kw.setdefault("allowed", ALLOWED)
    return await reload_mod.reload_domain(api, store_of(paths), domain, now=kw.pop("now", NOW), sleep=_no_sleep, **kw)


# ---------------------------------------------------------------- allowlist and denylist


def test_default_allowlist() -> None:
    # The option is empty by default and the house profile's list applies; no profile, no reloads.
    assert make_options().capabilities.reloadable == frozenset()
    allowed = make_options().reloadable
    assert allowed == frozenset(SAMPLE_PROFILE.reload.domains)
    assert make_options(profile=GENERIC).reloadable == frozenset()
    assert make_options(capabilities__reloadable_domains="plex").reloadable == {"plex"}
    assert {"esphome", "bluetooth", "nuki"}.isdisjoint(allowed)


async def test_not_allowlisted_is_refused(api: FakeReloadAPI, paths: Paths) -> None:
    with pytest.raises(CapabilityError, match="reloadable_domains"):
        await reload(api, paths, "kasa", allowed={"acme_vacuum"})
    assert api.posts == []


@pytest.mark.parametrize(
    "domain",
    ["nuki", "august", "envisalink", "esphome", "bluetooth", "mqtt", "network", "zeroconf", "usb", "unifi",
     "google_assistant", "alexa", "homekit", "cloud", "hassio", "backup"],
)  # fmt: skip
async def test_denylist_wins_even_when_allowlisted(api: FakeReloadAPI, paths: Paths, domain: str) -> None:
    with pytest.raises(CapabilityError, match="never-reload"):
        await reload(api, paths, domain, allowed={domain})
    assert api.posts == []


@pytest.mark.parametrize("domain", ["NUKI", "nuki ", "acme_vacuum;kasa", "../x", ""])
def test_odd_spellings_are_not_domains(domain: str) -> None:
    assert reload_mod.static_refusal(domain, {domain}) == "not an integration domain"


@pytest.mark.parametrize(("domain", "what"), [("oddlock", r"lock\.side_gate"), ("gdo", r"cover\.big_door")])
async def test_a_domain_providing_a_lock_or_garage_cover_is_refused(
    api: FakeReloadAPI, paths: Paths, domain: str, what: str
) -> None:
    with pytest.raises(CapabilityError, match=what):
        await reload(api, paths, domain)
    assert api.posts == []


async def test_a_cover_without_a_safe_class_is_protected(api: FakeReloadAPI, paths: Paths) -> None:
    api.states["cover.living_room_blind"]["attributes"] = {}
    with pytest.raises(CapabilityError, match="not positively"):
        await reload(api, paths, "blinds")
    api.states["cover.living_room_blind"]["attributes"] = {"device_class": "blind"}
    assert (await reload(api, paths, "blinds"))["ok"]


@pytest.mark.parametrize("name", ["switch.garage_opener", "button.gate_open", "valve.front_door_release"])
async def test_switches_named_like_access_are_protected(api: FakeReloadAPI, paths: Paths, name: str) -> None:
    api.registry.append({"entity_id": name, "platform": "plugs", "config_entry_id": "pl1"})
    with pytest.raises(CapabilityError, match="access control"):
        await reload(api, paths, "plugs")


@pytest.mark.parametrize(
    "name", ["switch.robot_vacuum_child_lock", "switch.acme_weather_key_lock", "switch.oven_control_lock"]
)
async def test_device_local_child_locks_do_not_block(api: FakeReloadAPI, paths: Paths, name: str) -> None:
    api.registry.append({"entity_id": name, "platform": "plugs", "config_entry_id": "pl1"})
    assert (await reload(api, paths, "plugs"))["ok"]


@pytest.mark.parametrize("name", ["switch.front_door_lock", "switch.child_lock_and_front_door", "button.lock"])
async def test_real_locks_still_block_next_to_benign_words(api: FakeReloadAPI, paths: Paths, name: str) -> None:
    api.registry.append({"entity_id": name, "platform": "plugs", "config_entry_id": "pl1"})
    with pytest.raises(CapabilityError, match="access control"):
        await reload(api, paths, "plugs")


@pytest.mark.parametrize("name", ["switch.garage_siren_on_event", "switch.garage_record_audio"])
async def test_a_room_named_garage_is_not_an_access_control(api: FakeReloadAPI, paths: Paths, name: str) -> None:
    api.registry.append({"entity_id": name, "platform": "plugs", "config_entry_id": "pl1"})
    assert (await reload(api, paths, "plugs"))["ok"]


@pytest.mark.parametrize("name", ["switch.garage_door", "switch.garage_door_relay", "button.garage_opener"])
async def test_garage_door_hardware_still_blocks(api: FakeReloadAPI, paths: Paths, name: str) -> None:
    api.registry.append({"entity_id": name, "platform": "plugs", "config_entry_id": "pl1"})
    with pytest.raises(CapabilityError, match="access control"):
        await reload(api, paths, "plugs")


async def test_friendly_name_counts_and_outdoor_does_not(api: FakeReloadAPI, paths: Paths) -> None:
    api.registry.append({"entity_id": "switch.outdoor_lights", "platform": "plugs", "config_entry_id": "pl1"})
    assert (await reload(api, paths, "plugs"))["ok"]
    api.states["switch.coffee"]["attributes"]["friendly_name"] = "Side Gate"
    with pytest.raises(CapabilityError, match="access control"):
        await reload(api, paths, "plugs", now=NOW + timedelta(minutes=11))


async def test_entities_of_the_entry_count_even_on_another_platform(api: FakeReloadAPI, paths: Paths) -> None:
    api.registry.append({"entity_id": "lock.via_mqtt", "platform": "mqtt", "config_entry_id": "ka1"})
    with pytest.raises(CapabilityError, match=r"lock\.via_mqtt"):
        await reload(api, paths, "kasa")
    assert api.posts == []


@pytest.mark.parametrize("what", ["entries", "registry", "states"])
async def test_odd_replies_fail_closed(api: FakeReloadAPI, paths: Paths, what: str) -> None:
    if what == "entries":
        api.entries = {"oops": 1}  # type: ignore[assignment]
    elif what == "registry":
        api.registry = None  # type: ignore[assignment]
    else:
        api.states = None  # type: ignore[assignment]

        async def bad_states(path: str) -> Any:
            return {"message": "not a list"}

        api.core_rest = bad_states  # type: ignore[method-assign]
    with pytest.raises(CapabilityError, match="unexpected"):
        await reload(api, paths, "kasa")
    assert api.posts == []


async def test_unknown_or_disabled_domain_is_refused(api: FakeReloadAPI, paths: Paths) -> None:
    for domain in ("nothere", "sleepy"):
        with pytest.raises(CapabilityError, match="no enabled config entry"):
            await reload(api, paths, domain, allowed={domain})
    assert api.posts == []


async def test_odd_entry_ids_are_never_put_in_a_url(api: FakeReloadAPI, paths: Paths) -> None:
    api.entries.append({"entry_id": "../../x", "domain": "weird", "title": "W", "state": "loaded"})
    with pytest.raises(CapabilityError, match="no enabled config entry"):
        await reload(api, paths, "weird")


async def test_admin_can_block_a_domain(api: FakeReloadAPI, paths: Paths) -> None:
    with pytest.raises(CapabilityError, match=r"capabilities\.never"):
        await reload(api, paths, "kasa", never=("integration.reload.kasa",))


# ---------------------------------------------------------------- limits


async def test_reload_waits_for_entities_and_reports(api: FakeReloadAPI, paths: Paths) -> None:
    result = await reload(api, paths, "acme_vacuum")
    assert api.posts == ["/config/config_entries/entry/rr1/reload"]
    assert result["ok"] and result["recovered"] == ["sensor.robot_vacuum_battery"] and result["still_unavailable"] == []


async def test_always_unavailable_entities_do_not_fail_or_hold_the_reload(api: FakeReloadAPI, paths: Paths) -> None:
    api.recover = False  # the battery sensor was unavailable before and stays so (an unsupported feature)
    sleeps: list[float] = []

    async def counting_sleep(s: float) -> None:
        sleeps.append(s)

    result = await reload_mod.reload_domain(
        api, store_of(paths), "acme_vacuum", now=NOW, sleep=counting_sleep, allowed=ALLOWED, wait_s=60, poll_s=3
    )
    assert result["ok"] and result["newly_unavailable"] == []
    assert result["still_unavailable"] == ["sensor.robot_vacuum_battery"] and sleeps == []
    assert "already unavailable before" in result["message"]


async def test_an_entity_lost_by_the_reload_fails_it(api: FakeReloadAPI, paths: Paths) -> None:
    original = api.core_post

    async def post_and_break(path: str) -> Any:
        out = await original(path)
        api.states["vacuum.robot_vacuum"]["state"] = "unavailable"
        return out

    api.core_post = post_and_break  # type: ignore[method-assign]
    result = await reload(api, paths, "acme_vacuum", wait_s=6, poll_s=3)
    assert not result["ok"] and result["newly_unavailable"] == ["vacuum.robot_vacuum"]
    assert "Newly unavailable: vacuum.robot_vacuum" in result["message"]


async def test_a_dropped_websocket_after_the_reload_is_retried(api: FakeReloadAPI, paths: Paths) -> None:
    original = api.ws
    calls = {"n": 0}

    async def flaky(msg_type: str, **kw: Any) -> Any:
        if msg_type == "config_entries/get" and api.posts:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionResetError("reset by peer")
        return await original(msg_type, **kw)

    api.ws = flaky  # type: ignore[method-assign]
    result = await reload(api, paths, "acme_vacuum")
    assert result["ok"] and calls["n"] == 2


async def test_once_per_domain_per_10_minutes(api: FakeReloadAPI, paths: Paths) -> None:
    await reload(api, paths, "acme_vacuum")
    with pytest.raises(CapabilityError, match="10 minutes"):
        await reload(api, paths, "acme_vacuum", now=NOW + timedelta(minutes=9))
    await reload(api, paths, "kasa", now=NOW + timedelta(minutes=1))
    await reload(api, paths, "acme_vacuum", now=NOW + timedelta(minutes=11))
    assert api.posts.count("/config/config_entries/entry/rr1/reload") == 2


async def test_a_failed_reload_still_counts_and_is_reported(api: FakeReloadAPI, paths: Paths) -> None:
    api.post_fails = True
    api.recover = False
    result = await reload(api, paths, "acme_vacuum", wait_s=6, poll_s=3)
    assert not result["ok"] and result["problems"] and result["still_unavailable"]
    with pytest.raises(CapabilityError, match="10 minutes"):
        await reload(api, paths, "acme_vacuum", now=NOW + timedelta(minutes=1))


@pytest.mark.parametrize("state", ["cleaning", "returning", "paused"])
async def test_never_while_a_vacuum_is_busy(api: FakeReloadAPI, paths: Paths, state: str) -> None:
    api.states["vacuum.robot_vacuum"]["state"] = state
    with pytest.raises(CapabilityError, match="cleaning or paused"):
        await reload(api, paths, "acme_vacuum")
    assert api.posts == []


@pytest.mark.parametrize(
    "content", ["{not json", '["a"]', '{"acme_vacuum": "yesterday"}', '{"acme_vacuum": "2026-01-29T11:55:00"}']
)
async def test_corrupt_state_refuses(api: FakeReloadAPI, paths: Paths, content: str) -> None:
    store_of(paths).kv_set(reload_mod.RELOAD_KEY, content, now=NOW)
    with pytest.raises(CapabilityError, match="reload state"):
        await reload(api, paths, "acme_vacuum")
    assert api.posts == []


async def test_future_dated_state_is_ignored(api: FakeReloadAPI, paths: Paths) -> None:
    store_of(paths).kv_set(
        reload_mod.RELOAD_KEY, json.dumps({"acme_vacuum": (NOW + timedelta(days=3)).isoformat()}), now=NOW
    )
    assert (await reload(api, paths, "acme_vacuum"))["ok"]


async def test_concurrent_requests_reload_once(api: FakeReloadAPI, paths: Paths) -> None:
    results = await asyncio.gather(reload(api, paths, "acme_vacuum"), reload(api, paths, "acme_vacuum"),
                                   return_exceptions=True)  # fmt: skip
    assert sum(isinstance(r, dict) for r in results) == 1
    assert api.posts.count("/config/config_entries/entry/rr1/reload") == 1


async def test_reserve_runs_only_after_every_check(api: FakeReloadAPI, paths: Paths) -> None:
    taken: list[str] = []
    api.states["vacuum.robot_vacuum"]["state"] = "cleaning"
    with pytest.raises(CapabilityError):
        await reload(api, paths, "acme_vacuum", reserve=lambda: taken.append("x") or True)
    assert taken == []
    api.states["vacuum.robot_vacuum"]["state"] = "docked"
    with pytest.raises(CapabilityError, match="daily quota"):
        await reload(api, paths, "acme_vacuum", reserve=lambda: False)
    assert api.posts == []


async def test_deadline_reports_a_partial_reload(api: FakeReloadAPI, paths: Paths) -> None:
    api.entries.append({"entry_id": "rr2", "domain": "acme_vacuum", "title": "Acme Vacuum 2", "state": "loaded"})
    ticks = iter([0.0, 0.0, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5])
    result = await reload(api, paths, "acme_vacuum", clock=lambda: next(ticks, 99.9))
    assert result["attempted"] == 1 and result["skipped"] == ["Acme Vacuum 2"] and not result["ok"]
    assert "deadline" in result["message"]


async def test_nothing_reached_starts_no_lockout(api: FakeReloadAPI, paths: Paths) -> None:
    ticks = iter([0.0])
    result = await reload(api, paths, "acme_vacuum", clock=lambda: next(ticks, 200.0))
    assert result["attempted"] == 0 and api.posts == []
    assert (await reload(api, paths, "acme_vacuum"))["attempted"] == 1


async def test_a_hung_entry_times_out(api: FakeReloadAPI, paths: Paths) -> None:
    api.post_delay = 5
    result = await reload(api, paths, "acme_vacuum", post_timeout_s=1.5)
    assert not result["ok"] and any("no answer" in p for p in result["problems"])


# ---------------------------------------------------------------- policy and profiles


def test_registry_row_is_narrow() -> None:
    assert CAP.effects == frozenset({Effect.RELOAD}) and not CAP.internal and CAP.enabled and CAP.refund_refused
    assert CAP.origins == frozenset({Origin.AGENT_SELF, Origin.ADMIN_CHAT})
    assert CAP.quota.per_day == 6 and list(CAP.args) == ["domain"]
    assert Effect.RELOAD in CHAT_EFFECTS


def test_only_integration_reload_carries_the_chat_allowed_reload_effect() -> None:
    assert {c.name for c in CAPABILITIES.values() if Effect.RELOAD in c.effects} == {"integration.reload"}


def verdict(origin: Origin, acting: bool, never: tuple[str, ...] = ()) -> Any:
    return decide(capability=CAP.name, default_tier=CAP.tier, effects=CAP.effects, origin=origin,
                  allowed_origins=CAP.origins, acting=acting,
                  tighten={n: PolicyTier.NEVER for n in never})  # fmt: skip


def test_act_mode_executes_observe_mode_only_reports() -> None:
    assert verdict(Origin.AGENT_SELF, acting=True).execute
    assert verdict(Origin.ADMIN_CHAT, acting=True).execute
    observe = verdict(Origin.AGENT_SELF, acting=False)
    assert not observe.execute and observe.report_only


def test_origins_outside_the_row_and_the_admin_switch_are_refused() -> None:
    assert verdict(Origin.ADMIN, acting=True).outcome == "denied"
    assert verdict(Origin.AGENT_SELF, acting=True, never=("integration.reload",)).outcome == "denied"


def test_profiles() -> None:
    for profile in (TRIAGE, CHAT, INVESTIGATE):
        assert CAP.name in profile.capabilities
    for profile in (FOLLOWUP, SUMMARY):
        assert CAP.name not in profile.capabilities


# ---------------------------------------------------------------- over the socket


def _live(api: FakeReloadAPI) -> type:
    class Live:
        def __init__(self, *_: Any, **__: Any) -> None:
            pass

        async def __aenter__(self) -> FakeReloadAPI:
            return api

        async def __aexit__(self, *_: object) -> None:
            return None

    return Live


@pytest.fixture
def world(paths: Paths, api: FakeReloadAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    real = reload_mod.reload_domain

    async def fast(api_: Any, store: Any, domain: str, **kw: Any) -> dict[str, Any]:
        return await real(api_, store, domain, sleep=_no_sleep, **kw)

    monkeypatch.setattr(reload_mod, "LiveAPI", _live(api))
    monkeypatch.setattr(reload_mod, "reload_domain", fast)
    w = World(paths, options=make_options(agent__mode="act", capabilities__reloadable_domains="acme_vacuum,kasa,plugs"))
    yield w
    w.store.close()


async def test_socket_triage_run_reloads_and_audits(world: World, api: FakeReloadAPI) -> None:
    run = world.run()
    reply = await world.call("integration-reload", domain="acme_vacuum")
    assert reply["ok"], reply
    assert api.posts and any("Reloaded acme_vacuum" in n for n in run.effects.notes)
    row = next(r for r in world.audit() if r["capability"] == "integration.reload")
    assert row["decision"] == "execute"


async def test_socket_refuses_denylisted_and_unlisted_domains(world: World, api: FakeReloadAPI) -> None:
    world.run()
    reply = await world.call("integration-reload", domain="nuki")
    assert not reply["ok"] and "never-reload" in reply["error"]
    reply = await world.call("integration-reload", domain="plex")
    assert not reply["ok"] and "reloadable_domains" in reply["error"] and api.posts == []


async def test_socket_chat_run_may_reload(world: World, api: FakeReloadAPI) -> None:
    world.run(allowed=CHAT.capabilities, principal=Principal(Origin.ADMIN_CHAT, user_id="u1", channel="panel"))
    assert (await world.call("integration-reload", domain="kasa"))["ok"]


async def test_socket_profile_without_the_capability_is_refused(world: World, api: FakeReloadAPI) -> None:
    world.run(allowed=FOLLOWUP.capabilities)
    reply = await world.call("integration-reload", domain="acme_vacuum")
    assert not reply["ok"] and "not available" in reply["error"] and api.posts == []


async def test_socket_daily_quota(world: World, api: FakeReloadAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reload_mod, "rate_refusal", lambda *_: None)
    for _ in range(3):
        world.server.end_run()
        world.run()
        assert (await world.call("integration-reload", domain="kasa"))["ok"]
        assert (await world.call("integration-reload", domain="kasa"))["ok"]
    world.server.end_run()
    world.run()
    reply = await world.call("integration-reload", domain="kasa")
    assert not reply["ok"] and "daily quota" in reply["error"]


async def test_socket_refusals_do_not_burn_the_daily_quota(world: World, api: FakeReloadAPI) -> None:
    api.registry.append({"entity_id": "switch.garage_opener", "platform": "plugs", "config_entry_id": "pl1"})
    for _ in range(4):
        world.server.end_run()
        world.run()
        for _ in range(2):
            reply = await world.call("integration-reload", domain="plugs")
            assert not reply["ok"] and "access control" in reply["error"]
    day = world.server._day(datetime.now(UTC))
    assert world.store.count(day, "cap:integration.reload") == 0


async def test_socket_observe_mode_reports_only(paths: Paths, api: FakeReloadAPI,
                                                monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    monkeypatch.setattr(reload_mod, "LiveAPI", _live(api))
    w = World(paths, options=make_options(agent__mode="observe", capabilities__reloadable_domains="acme_vacuum"))
    w.run()
    reply = await w.call("integration-reload", domain="acme_vacuum")
    assert api.posts == [] and "observe" in json.dumps(reply)
    w.store.close()
