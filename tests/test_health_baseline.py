from datetime import timedelta
from pathlib import Path

import pytest

from opsd.baseline import Baseline, advance, apply_ack, diff, seed, seed_exclusions
from opsd.config import Paths
from opsd.health import (
    collect,
    derive_backups,
    derive_log_errors,
    derive_memory,
    derive_oom,
    derive_repairs,
)
from tests.conftest import NOW, FakeHA, make_options


async def snap(fake: FakeHA, paths: Paths, meminfo: Path, now=NOW):  # type: ignore[no-untyped-def]
    return await collect(fake, paths, make_options(), now=now, core_release_age=lambda v: 9.0, meminfo_path=meminfo)


async def test_collect_snapshot(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    s = await snap(fake_ha, paths, meminfo)
    assert s.core["api_up"] and s.core["version"] == "2026.10.3"
    assert set(s.unavailable) == {"sensor.old_thing"}
    assert s.custom_integrations["acme_solar"]["entities"] == 3
    assert s.custom_integrations["acme_solar"]["entries_loaded"] == 1
    assert s.updates[0]["tier"] == "auto"  # Core patch older than a week
    assert s.memory["available_mb"] == 2437
    assert s.backups["newest_complete_name"] == "full-1" and s.backups["unprotected"] == []
    assert s.config_check == {"ok": True}
    assert not [e for e in s.errors if not e.startswith("git")]


async def test_seed_then_no_changes(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    s = await snap(fake_ha, paths, meminfo)
    baseline = seed(s, NOW)
    assert diff(s, baseline, NOW) == []


async def test_baseline_of_many_unavailable_is_quiet(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    for i in range(245):
        fake_ha.states.append(
            {
                "entity_id": f"sensor.dead_{i}",
                # Sensors at `unknown` have no reading yet; only `unavailable` is down.
                "state": "unavailable" if i % 2 == 0 else "unknown",
                "last_changed": "2026-01-01T00:00:00+00:00",
            }
        )
    s = await snap(fake_ha, paths, meminfo)
    baseline = seed(s, NOW)
    assert len(baseline.unavailable) == 124
    assert diff(s, baseline, NOW) == []


async def test_custom_integration_breaking_is_critical(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    """The Acme Solar-after-Core-update case: every fork entity goes unavailable."""
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        fake_ha.set_unavailable(entity, hours_ago=8)
    items = {i.key: i for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW)}
    assert items["entities:acme_solar"].severity == "critical"
    assert items["entities:acme_solar"].detail["count"] == 3
    assert items["custom:acme_solar"].severity == "critical"


ACME_SOLAR = ("light.kitchen", "light.porch", "fan.bedroom")


async def test_expected_unavailable_entities_do_not_break_a_custom_integration(
    fake_ha: FakeHA, paths: Paths, meminfo: Path
) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    for entity in ACME_SOLAR:
        fake_ha.set_unavailable(entity, hours_ago=8)
    s = await snap(fake_ha, paths, meminfo)
    assert diff(s, baseline, NOW, expected_unavailable=frozenset(ACME_SOLAR)) == []
    # Naming only some: the rest no longer reach all entities, but each stays a critical entities item.
    items = {i.key: i for i in diff(s, baseline, NOW, expected_unavailable=frozenset(ACME_SOLAR[:2]))}
    assert "custom:acme_solar" not in items
    assert items["entities:acme_solar"].severity == "critical"
    assert items["entities:acme_solar"].detail["entities"] == ["fan.bedroom"]
    # An expected entity of another integration does not change the count.
    items = {i.key: i for i in diff(s, baseline, NOW, expected_unavailable=frozenset({"sensor.old_thing"}))}
    assert items["custom:acme_solar"].severity == "critical"
    # The snapshot itself still lists them as unavailable.
    assert set(ACME_SOLAR) <= set(s.unavailable)


async def test_the_seed_accepts_expected_unavailable_custom_entities(
    fake_ha: FakeHA, paths: Paths, meminfo: Path
) -> None:
    for entity in ACME_SOLAR:
        fake_ha.set_unavailable(entity, hours_ago=48)
    s = await snap(fake_ha, paths, meminfo)
    assert seed_exclusions(s)["custom_broken"] == ["acme_solar"]
    expected = frozenset(ACME_SOLAR[:2])
    excluded = seed_exclusions(s, expected)
    assert excluded["custom_broken"] == [] and excluded["custom_unavailable"] == ["fan.bedroom"]
    baseline = seed(s, NOW, expected_unavailable=expected)
    assert {"light.kitchen", "light.porch"} <= set(baseline.unavailable)
    assert "fan.bedroom" not in baseline.unavailable
    assert [i.key for i in diff(s, baseline, NOW, expected_unavailable=expected)] == ["entities:acme_solar"]
    everything = frozenset(ACME_SOLAR)
    assert diff(s, seed(s, NOW, expected_unavailable=everything), NOW, expected_unavailable=everything) == []


def add_zha_entity(fake: FakeHA) -> None:
    fake.states.append({"entity_id": "sensor.zha_temp", "state": "21", "last_changed": "2026-01-01T00:00:00+00:00",
                        "attributes": {}})  # fmt: skip
    fake.registry.append({"entity_id": "sensor.zha_temp", "platform": "zha", "config_entry_id": "zha1"})


async def test_flapping_entities_are_ignored_except_custom(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    add_zha_entity(fake_ha)
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.set_unavailable("sensor.zha_temp", hours_ago=1)
    assert [i.key for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW)] == []
    # A fresh, partial custom-integration outage is critical immediately.
    fake_ha.set_unavailable("light.kitchen", hours_ago=0.1)
    items = {i.key: i for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW)}
    assert items["entities:acme_solar"].severity == "critical" and "custom:acme_solar" not in items


async def test_failed_entry_logs_repairs_and_thresholds(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.entries[0]["state"] = "setup_retry"
    fake_ha.issues.append({"domain": "acme_solar", "issue_id": "deprecated", "severity": "warning"})
    fake_ha.system_log.append(
        {"name": "custom_components.acme_weather", "level": "ERROR", "message": ["boom 42"], "count": 3}
    )
    fake_ha.backups = [fake_ha.backups[1]]
    fake_ha.kernel_log = "Out of memory: Killed process 123 (python3) total-vm:1kB"
    fake_ha.config_ok = False
    keys = {i.key: i for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW)}
    assert keys["entry:lut1"].severity == "critical"  # custom integration entry
    assert "repair:acme_solar:deprecated" in keys
    assert any(k.startswith("log:") for k in keys)
    assert "backup:complete_stale" in keys  # the newest backup lacks the add-ons
    assert keys["host:oom:1000:1"].severity == "critical"
    assert keys["config:invalid"].severity == "critical"
    assert keys["custom:acme_solar"].severity == "critical"


async def test_core_down_item(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.core_state = "error"
    keys = {i.key for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW)}
    assert "core:down" in keys


async def test_new_update_reported_once(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    s1 = await snap(fake_ha, paths, meminfo)
    baseline = seed(s1, NOW)
    fake_ha.states[-1]["attributes"]["latest_version"] = "2026.10.5"
    s2 = await snap(fake_ha, paths, meminfo)
    items = diff(s2, baseline, NOW)
    assert [i.key for i in items] == ["update:update.home_assistant_core_update@2026.10.5"]
    assert diff(s2, advance(baseline, s2, NOW), NOW) == []


async def test_ack_expiry_and_accept(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    add_zha_entity(fake_ha)
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.set_unavailable("sensor.zha_temp", hours_ago=10)
    s = await snap(fake_ha, paths, meminfo)
    item = next(i for i in diff(s, baseline, NOW) if i.key == "entities:zha")
    acked = apply_ack(Baseline.from_json(baseline.to_json()), item, reason="known", days=2, accept=False, now=NOW)
    assert all(i.key != "entities:zha" for i in diff(s, acked, NOW))
    later = NOW + timedelta(days=3)
    assert any(i.key == "entities:zha" for i in diff(s, acked, later))
    accepted = apply_ack(Baseline.from_json(baseline.to_json()), item, reason="unplugged", days=1, accept=True, now=NOW)
    assert all(i.key != "entities:zha" for i in diff(s, accepted, later))


async def test_critical_ack_rules(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        fake_ha.set_unavailable(entity, hours_ago=10)
    s = await snap(fake_ha, paths, meminfo)
    items = {i.key: i for i in diff(s, baseline, NOW)}
    claude = apply_ack(Baseline.from_json(baseline.to_json()), items["custom:acme_solar"], reason="x", days=30,
                       accept=True, now=NOW)  # fmt: skip
    assert claude.custom_ok["acme_solar"] is True and claude.acks["custom:acme_solar"]["accepted"] is False
    assert any(i.key == "custom:acme_solar" for i in diff(s, claude, NOW + timedelta(days=4)))
    # advance() never records a custom integration as known-broken by itself.
    assert advance(claude, s, NOW).custom_ok["acme_solar"] is True
    user = apply_ack(Baseline.from_json(baseline.to_json()), items["custom:acme_solar"], reason="x", days=30,
                     accept=True, now=NOW, by="user")  # fmt: skip
    assert user.custom_ok["acme_solar"] is False
    assert advance(user, s, NOW).custom_ok["acme_solar"] is False
    assert all(i.key != "custom:acme_solar" for i in diff(s, user, NOW + timedelta(days=40)))


async def test_recovered_entity_leaves_baseline(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.states[3]["state"] = "on"
    baseline = advance(baseline, await snap(fake_ha, paths, meminfo), NOW)
    assert baseline.unavailable == []
    fake_ha.set_unavailable("sensor.old_thing", hours_ago=10)
    assert any(i.key == "entities:zha" for i in diff(await snap(fake_ha, paths, meminfo), baseline, NOW))


def test_derivations() -> None:
    assert (
        derive_memory("MemTotal: 2048 kB\nMemAvailable: 1024 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")["swap_used_pct"]
        == 0.0
    )
    assert derive_oom("x\nOut of memory: Killed process 9 (claude) y") == ["Out of memory: Killed process 9 (claude)"]
    assert derive_repairs([{"domain": "a", "issue_id": "b", "ignored": True}]) == {}
    logs = derive_log_errors(
        [
            {"name": "x", "level": "ERROR", "message": ["fail 1"], "count": 2},
            {"name": "x", "level": "ERROR", "message": ["fail 2"], "count": 3},
            {"name": "x", "level": "WARNING", "message": ["meh"], "count": 1},
        ]
    )
    assert len(logs) == 1 and next(iter(logs.values()))["count"] == 5
    assert derive_backups([], NOW)["newest_complete_age_h"] is None


@pytest.mark.parametrize("hours", [2.0])
def test_backup_age(hours: float) -> None:
    out = derive_backups([{"name": "f", "type": "full", "date": (NOW - timedelta(hours=hours)).isoformat()}], NOW)
    assert out["newest_complete_age_h"] == hours


async def test_expected_unavailable_never_alerts(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    fake_ha.states.append({"entity_id": "lock.front_door", "state": "locked",
                           "last_changed": "2026-01-01T00:00:00+00:00", "attributes": {}})  # fmt: skip
    fake_ha.registry.append({"entity_id": "lock.front_door", "platform": "august"})
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.set_unavailable("lock.front_door", hours_ago=30)
    s = await snap(fake_ha, paths, meminfo)
    assert any(i.key == "entities:august" for i in diff(s, baseline, NOW))
    assert diff(s, baseline, NOW, expected_unavailable=frozenset({"lock.front_door"})) == []


def test_is_down_semantics() -> None:
    from opsd.health import is_down

    for entity in ("event.k", "scene.s", "button.b", "switch.chime", "sensor.x", "update.u", "notify.n", "image.i"):
        assert not is_down(entity, "unknown"), entity
        assert is_down(entity, "unavailable"), entity
    for entity in ("light.l", "fan.f", "lock.l", "cover.c", "climate.c", "media_player.m", "alarm_control_panel.a"):
        assert is_down(entity, "unknown"), entity
    assert not is_down("light.l", "on")


# ------------------------------------------- refactor 13: access devices exposed to Assist

from opsd.health import access_kind, derive_assist_exposure  # noqa: E402


def test_access_kind() -> None:
    assert access_kind("lock.front_door", {}) == "lock"
    assert access_kind("alarm_control_panel.house_alarm", {}) == "alarm"
    assert access_kind("cover.big_door", {"device_class": "garage"}) == "garage"
    assert access_kind("cover.driveway", {"device_class": "gate"}) == "garage"
    assert access_kind("cover.garage_door", {}) == "garage"
    assert access_kind("cover.living_room_blinds", {"device_class": "blind"}) is None
    assert access_kind("light.kitchen", {}) is None


def test_derive_assist_exposure() -> None:
    states = [{"entity_id": "cover.big_door", "attributes": {"device_class": "garage"}}]
    exposure = {
        "exposed_entities": {
            "lock.front": {"conversation": True, "cloud.alexa": False},
            "lock.back": {"conversation": False, "cloud.google_assistant": True},
            "cover.big_door": {"conversation": True},
            "light.kitchen": {"conversation": True},
            "alarm_control_panel.home": {"conversation": "yes"},
            "lock.weird": "nope",
        }
    }
    assert derive_assist_exposure(exposure, states) == {"cover.big_door": "garage", "lock.front": "lock"}
    for bad in (None, [], {"exposed": {}}, {"exposed_entities": []}):
        with pytest.raises(ValueError, match="unexpected"):
            derive_assist_exposure(bad, states)


async def test_assist_exposure_is_critical_until_the_admin_accepts(
    fake_ha: FakeHA, paths: Paths, meminfo: Path
) -> None:
    fake_ha.states.append({"entity_id": "lock.front", "state": "locked", "attributes": {}})
    fake_ha.exposed["lock.front"] = {"conversation": True}
    snap = await collect(fake_ha, paths, make_options(), now=NOW, meminfo_path=meminfo)
    assert snap.assist_exposed == {"lock.front": "lock"}
    baseline = seed(snap, NOW)
    assert baseline.accepted_exposures == []
    [item] = [i for i in diff(snap, baseline, NOW) if i.kind == "assist_exposure"]
    assert item.key == "assist:lock.front" and item.severity == "critical"
    # The AI cannot accept it; the admin can.
    claude = apply_ack(baseline, item, reason="fine", days=30, accept=True, now=NOW)
    assert claude.accepted_exposures == [] and claude.acks[item.key]["accepted"] is False
    user = apply_ack(seed(snap, NOW), item, reason="intended", days=30, accept=True, now=NOW, by="user")
    assert user.accepted_exposures == ["lock.front"]
    user.acks.clear()
    assert not [i for i in diff(snap, user, NOW) if i.kind == "assist_exposure"]
    # Removing the exposure drops the acceptance, so re-exposing alerts again.
    fake_ha.exposed["lock.front"] = {"conversation": False}
    quiet = await collect(fake_ha, paths, make_options(), now=NOW, meminfo_path=meminfo)
    assert advance(user, quiet, NOW).accepted_exposures == []


async def test_assist_check_failure_is_a_warning(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    fake_ha.exposed = "broken"  # type: ignore[assignment]
    snap = await collect(fake_ha, paths, make_options(), now=NOW, meminfo_path=meminfo)
    assert snap.assist_exposed is None and any(e.startswith("assist_exposure") for e in snap.errors)
    baseline = seed(snap, NOW)
    baseline.accepted_exposures = ["lock.front"]
    assert advance(baseline, snap, NOW).accepted_exposures == ["lock.front"]  # unknown keeps acceptances
    [item] = [i for i in diff(snap, baseline, NOW) if i.kind == "assist_exposure"]
    assert item.key == "assist:check_failed" and item.severity == "warning"


async def test_config_check_timeout_is_unknown_not_invalid(fake_ha: FakeHA, paths: Paths, meminfo: Path) -> None:
    baseline = seed(await snap(fake_ha, paths, meminfo), NOW)
    fake_ha.config_check_times_out = True
    s = await snap(fake_ha, paths, meminfo)
    assert s.config_check["ok"] is None and "timed out" in s.config_check["error"]
    assert "config:invalid" not in {i.key for i in diff(s, baseline, NOW)}
