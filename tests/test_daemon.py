"""End-to-end health cycles against the fake HA, with the AI stubbed."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from opsd import daemon as daemon_mod
from opsd.claude_runner import RunConfig, RunResult
from opsd.config import Paths
from opsd.daemon import Daemon, fallback_push, next_run_at
from opsd.jsonstore import read_json, read_jsonl, write_json_atomic
from tests.conftest import FakeHA, make_options


class FakeClient:
    token = "sup-token-123456"

    def __init__(self) -> None:
        self.services: list[tuple[str, str, dict[str, Any]]] = []
        self.states: dict[str, tuple[str, dict[str, Any]]] = {}

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self.services.append((domain, service, data))

    def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        self.states[entity_id] = (state, attributes)


class Harness:
    def __init__(self, paths: Paths, fake: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> None:
        self.paths = paths
        self.fake = fake
        self.client = FakeClient()
        self.claude_calls: list[str] = []
        self.claude_reply: dict[str, Any] | None = None
        self.claude_status = "ok"

        class FakeLive:
            def __init__(self, _client: Any) -> None:
                pass

            async def __aenter__(self) -> FakeHA:
                return fake

            async def __aexit__(self, *_: object) -> None:
                return None

        original_collect = daemon_mod.collect

        async def collect(api: Any, *args: Any, **kwargs: Any) -> Any:
            kwargs["meminfo_path"] = meminfo
            kwargs["core_release_age"] = lambda v: 9.0
            return await original_collect(api, *args, **kwargs)

        # Called during a fake run, as the AI calling opsctl would (raw request lines).
        self.in_run_requests: list[dict[str, Any]] = []
        self.in_run_responses: list[dict[str, Any]] = []
        self.current: Daemon | None = None

        async def run_claude(cfg: RunConfig, prompt: str, **kwargs: Any) -> RunResult:
            self.claude_calls.append(prompt)
            on_start = kwargs.get("on_start")
            if on_start is not None:
                on_start(4242)
            assert self.current is not None and self.current.rpc is not None
            for request in self.in_run_requests:
                raw = json.dumps(request).encode()
                self.in_run_responses.append(await self.current.rpc.dispatch(raw))
            return RunResult(status=self.claude_status, cost_usd=0.2, turns=4, structured=self.claude_reply)

        monkeypatch.setattr(daemon_mod, "LiveAPI", FakeLive)
        monkeypatch.setattr(daemon_mod, "collect", collect)
        self._run_claude = run_claude
        self.exports: list[Path] = []
        self.daemons: list[Daemon] = []

    def build_export(self, repo: Path, export_root: Path) -> Path:
        target = export_root / "config"
        target.mkdir(parents=True, exist_ok=True)
        self.exports.append(target)
        return target

    def daemon(self, **options: Any) -> Daemon:
        # Quiet hours off unless a test sets them: results must not depend on the time of day.
        opts = make_options(**{"claude__token": "oat-token-abcdef", "notify__quiet_start": "", **options})
        daemon = Daemon(self.paths, opts, self.client)  # type: ignore[arg-type]
        daemon.require_privsep = False  # no `ops` account on a developer machine
        daemon.runner.run_claude = self._run_claude
        daemon.runner.host_available_mb = lambda: 2400
        daemon.runner.build_export = self.build_export
        daemon.rpc = daemon.rpc_server()
        self.daemons.append(daemon)
        self.current = daemon
        return daemon

    def pushes(self) -> list[dict[str, Any]]:
        return [d for (dom, _, d) in self.client.services if dom == "notify"]


@pytest.fixture
def h(paths: Paths, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


async def test_first_run_seeds_and_notifies_once(h: Harness) -> None:
    outcome = await h.daemon().health_cycle("catch_up")
    assert outcome.result == "seeded"
    assert read_json(h.paths.baseline_file, None) is not None
    assert len(h.pushes()) == 1 and "Baseline recorded" in h.pushes()[0]["message"]
    assert h.client.states["sensor.ops_agent"][0] == "ok"
    assert h.claude_calls == []

    outcome = await h.daemon().health_cycle("daily")
    assert outcome.result == "ok" and outcome.items == 0
    assert h.claude_calls == [] and len(h.pushes()) == 1


async def test_broken_custom_integration_runs_claude_and_notifies(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        h.fake.set_unavailable(entity, hours_ago=8)
    h.in_run_requests = [
        {"verb": "ack", "args": {"key": "entities:acme_solar", "days": 1, "reason": "user notified"}},
        {"verb": "notify", "args": {"title": "Acme Solar down", "urgent": True,
                                    "message": "All Acme Solar entities unavailable; rollback recommended."}},
    ]  # fmt: skip
    h.claude_reply = {"result": "escalated", "summary": "Acme Solar down after update"}
    outcome = await h.daemon().health_cycle("daily")
    assert len(h.claude_calls) == 1 and "entities:acme_solar" in h.claude_calls[0]
    assert "hunter22secret" not in h.claude_calls[0]
    assert h.exports, "The AI must run in the export, not the live config"
    claude_push, safety = h.pushes()[-2:]
    assert claude_push["title"] == "Agent: Acme Solar down" and claude_push["data"]["tag"] == "ha-ops-agent"
    # The deterministic safety push always fires for critical items, even after the AI's urgent push.
    assert safety["title"] == "Ops Agent: critical" and safety["data"]["push"] == {
        "interruption-level": "time-sensitive"
    }
    record = outcome.record
    # custom:acme_solar was not acked, so the sensor stays at "attention".
    assert record["unacked"] == ["custom:acme_solar"]
    assert h.client.states["sensor.ops_agent"][0] == "attention"
    assert read_jsonl(h.paths.history_file)[-1]["cost_usd"] == 0.2


async def test_without_token_sends_deterministic_fallback(h: Harness) -> None:
    await h.daemon(claude__token="").health_cycle("catch_up")
    h.fake.config_ok = False
    outcome = await h.daemon(claude__token="").health_cycle("daily")
    assert h.claude_calls == [] and outcome.result == "no_token"
    last = h.pushes()[-1]
    assert last["title"] == "Ops Agent: critical" and "configuration check failed" in last["message"]


async def test_silent_claude_on_critical_item_still_alerts(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.config_ok = False
    h.claude_reply = {"result": "ok", "summary": "nothing", "acks": [], "notify": None, "ops_log": None}
    await h.daemon().health_cycle("daily")
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"


async def test_claude_failure_falls_back(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    h.claude_status = "aborted_memory_guard"
    outcome = await h.daemon().health_cycle("daily")
    assert outcome.result == "aborted_memory_guard"
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"


def test_next_run_at() -> None:
    tz = ZoneInfo("Europe/Berlin")
    now = datetime(2027, 10, 30, 12, 0, tzinfo=UTC)  # 14:00 CEST
    assert next_run_at(now, "14:30", tz) == datetime(2027, 10, 30, 12, 30, tzinfo=UTC)
    assert next_run_at(now, "04:00", tz) == datetime(2027, 10, 31, 3, 0, tzinfo=UTC)  # CET from 31 Oct


def test_fallback_push_empty_for_info_only() -> None:
    from opsd.baseline import Item

    assert fallback_push([Item("update:x@1", "update", "info", "x")]) is None


async def test_core_down_alert_uses_relay(h: Harness) -> None:
    d = h.daemon()
    write_json_atomic(h.paths.push_cache_file, {"push_url": "u", "push_token": "t", "registration_info": {}})
    relayed: list[Any] = []
    d.notifier._relay = lambda reg, push: relayed.append(push)

    def supervisor(path: str) -> dict[str, str]:
        return {"state": "error"}

    d.client.supervisor = supervisor  # type: ignore[attr-defined]
    t0 = datetime(2026, 1, 31, 12, 0, tzinfo=UTC)
    await d._core_down(t0)
    await d._core_down(t0.replace(minute=6))
    assert relayed and relayed[0].urgent and "Core is down" in relayed[0].title
    await d._core_up(t0.replace(minute=20))
    assert h.pushes()[-1]["title"] == "Ops Agent: Core is back"
    assert d.state.core_down_since is None


async def test_non_urgent_claude_push_does_not_suppress_critical(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    h.in_run_requests = [
        {"verb": "ack", "args": {"key": "entities:acme_solar", "days": 30, "accept": True, "reason": "fine"}},
        {"verb": "notify", "args": {"title": "FYI", "message": "a light is off"}},
    ]
    h.claude_reply = {"result": "ok", "summary": "fine"}
    await h.daemon().health_cycle("daily")
    titles = [p["title"] for p in h.pushes()]
    assert titles[-2:] == ["Agent: FYI", "Ops Agent: critical"]
    baseline = read_json(h.paths.baseline_file, {})
    ack = baseline["acks"]["entities:acme_solar"]
    assert ack["accepted"] is False and ack["by"] == "claude"
    assert "light.kitchen" not in baseline["unavailable"]  # the AI cannot accept critical


async def test_rate_limited_claude_push_still_gets_safety_net(h: Harness) -> None:
    d = h.daemon(notify__max_pushes_per_day=0)
    await d.health_cycle("catch_up")
    h.fake.config_ok = False
    h.claude_reply = {"result": "escalated", "summary": "s", "acks": [],
                      "notify": {"title": "x", "message": "y", "urgent": False}, "ops_log": None}  # fmt: skip
    await h.daemon(notify__max_pushes_per_day=0).health_cycle("daily")
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"


async def test_seed_does_not_accept_broken_custom_integration(h: Harness) -> None:
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        h.fake.set_unavailable(entity, hours_ago=48)
    await h.daemon().health_cycle("catch_up")
    seed_push = h.pushes()[-1]
    assert "NOT accepted" in seed_push["message"] and "acme_solar" in seed_push["message"]
    assert seed_push["data"]["push"] == {"interruption-level": "time-sensitive"}
    baseline = read_json(h.paths.baseline_file, {})
    assert baseline["custom_ok"] == {"hacs": True, "acme_solar": True}
    assert "light.kitchen" not in baseline["unavailable"]
    h.claude_reply = {"result": "ok", "summary": "", "acks": [], "notify": None, "ops_log": None}
    outcome = await h.daemon().health_cycle("daily")
    assert {"custom:acme_solar", "entities:acme_solar"} <= set(outcome.record["item_keys"])
    # The baseline still does not record acme_solar as known-broken.
    assert read_json(h.paths.baseline_file, {})["custom_ok"]["acme_solar"] is True


async def test_user_ack_from_panel_can_accept(h: Harness) -> None:
    d = h.daemon(claude__token="")
    await d.health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    await d.health_cycle("daily")
    assert [i.key for i in d.open_items()] == ["entities:acme_solar"]
    assert await d.user_ack("entities:acme_solar", days=30, accept=True)
    assert not await d.user_ack("nope", days=1, accept=False)
    baseline = read_json(h.paths.baseline_file, {})
    assert "light.kitchen" in baseline["unavailable"] and baseline["acks"]["entities:acme_solar"]["by"] == "user"
    outcome = await d.health_cycle("daily")
    assert outcome.result == "ok"


async def test_run_cap(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    h.claude_status = "timeout"
    d = h.daemon()
    for _ in range(daemon_mod.MAX_CLAUDE_RUNS_PER_DAY + 2):
        await d.health_cycle("daily")
    assert len(h.claude_calls) == daemon_mod.MAX_CLAUDE_RUNS_PER_DAY
    from opsd.budget import Period

    now = datetime.now(UTC)
    assert d.store.usage(Period.at(now, d.tz), at=now)["day"]["runs"] == daemon_mod.MAX_CLAUDE_RUNS_PER_DAY


async def test_manual_trigger_cap(h: Harness) -> None:
    d = h.daemon()
    results = []
    for _ in range(daemon_mod.MAX_MANUAL_TRIGGERS_PER_DAY + 1):
        results.append(d.request_cycle("manual"))
        if results[-1]:
            d.queue.done(await d.queue.get())
    assert results.count(True) == daemon_mod.MAX_MANUAL_TRIGGERS_PER_DAY and results[-1] is False


async def test_lock_released_after_crash(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    d = h.daemon()

    async def boom(_trigger: str) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(d, "_health_cycle_locked", boom)
    with pytest.raises(RuntimeError):
        await d.health_cycle("daily")
    from opsd.lock import run_lock

    with run_lock(h.paths.lock_file):
        pass
    assert not d._cycle_running.locked()


def test_core_down_threshold_and_jobs() -> None:
    from datetime import timedelta

    assert daemon_mod.core_down_threshold("running", jobs_busy=False, in_update_window=False) == timedelta(minutes=5)
    assert daemon_mod.core_down_threshold("startup", jobs_busy=False, in_update_window=False) == timedelta(minutes=20)
    assert daemon_mod.core_down_threshold("running", jobs_busy=False, in_update_window=True) == timedelta(minutes=20)
    assert daemon_mod.core_down_threshold("running", jobs_busy=True, in_update_window=False) == timedelta(minutes=45)
    jobs = {
        "jobs": [
            {
                "name": "backup_manager",
                "done": True,
                "child_jobs": [{"name": "home_assistant_core_update", "done": False}],
            }
        ]
    }
    assert daemon_mod.supervisor_jobs_busy(jobs)
    assert not daemon_mod.supervisor_jobs_busy({"jobs": [{"name": "home_assistant_core_update", "done": True}]})
    assert not daemon_mod.supervisor_jobs_busy(None)
    tz = ZoneInfo("Europe/Berlin")
    at_0430 = datetime(2026, 1, 31, 3, 30, tzinfo=UTC)
    assert daemon_mod.in_window(at_0430, "04:00", tz) and not daemon_mod.in_window(at_0430, "16:00", tz)


async def test_core_down_deferred_while_supervisor_busy(h: Harness) -> None:
    d = h.daemon()
    sent: list[Any] = []
    d.notifier._relay = lambda reg, push: sent.append(push)
    write_json_atomic(h.paths.push_cache_file, {"push_url": "u", "push_token": "t", "registration_info": {}})

    def supervisor(path: str) -> dict[str, Any]:
        if path == "/jobs/info":
            return {"jobs": [{"name": "home_assistant_core_update", "done": False}]}
        return {"state": "running"}

    d.client.supervisor = supervisor  # type: ignore[attr-defined]
    t0 = datetime(2026, 1, 31, 20, 0, tzinfo=UTC)
    await d._core_down(t0)
    await d._core_down(t0.replace(minute=30))
    assert sent == []
    await d._core_down(t0.replace(minute=50))
    assert sent and sent[0].urgent


async def test_isolation_failure_blocks_claude(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd.privsep import Account

    await h.daemon().health_cycle("catch_up")
    h.fake.config_ok = False
    d = h.daemon()
    d.require_privsep = True
    d.runner.ops_account = lambda: Account(1001, 1001, frozenset({1001}))
    d.runner.isolation_problems = lambda paths, account: ["/data/options.json"]
    outcome = await d.health_cycle("daily")
    assert outcome.result == "isolation_failed" and h.claude_calls == []
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"
    d.runner.ops_account = lambda: None
    assert (await d.health_cycle("daily")).result == "isolation_failed"


async def test_privsep_run_drops_to_ops(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd.privsep import Account

    await h.daemon().health_cycle("catch_up")
    h.fake.config_ok = False
    seen: list[RunConfig] = []

    async def run_claude(cfg: RunConfig, prompt: str, **_: Any) -> RunResult:
        seen.append(cfg)
        return RunResult(status="ok", structured=None)

    d = h.daemon()
    d.runner.run_claude = run_claude
    d.runner.ops_account = lambda: Account(1001, 1002, frozenset({1002}))
    d.runner.isolation_problems = lambda paths, account: []
    d.require_privsep = True
    # Without a passing live self-test the AI is blocked.
    assert (await d.health_cycle("daily")).result == "isolation_failed" and seen == []
    from opsd.selftest import Check, SelfTest

    d.selftest = SelfTest(at="now", checks={"all": Check(True)})
    await d.health_cycle("daily")
    assert seen[0].run_as == (1001, 1002) and seen[0].cwd == h.paths.export_dir
    assert d.status_view()["selftest"] == "pass"


async def test_startup_resets_stale_outage(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    d = h.daemon()

    async def no_rpc() -> None:
        return None

    monkeypatch.setattr(d, "start_rpc", no_rpc)
    d.state.core_down_since = "2026-01-01T00:00:00+00:00"
    d.state.core_down_notified = True

    async def stop(*_: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(d, "resolve_timezone", stop)
    # Cancelled (SIGTERM): it stops cleanly after sending the pushes in flight.
    await d.run_forever()
    assert d.state.core_down_since is None and not d.state.core_down_notified


def add_restart_scenario(h: Harness) -> None:
    """After a host reboot, stateless custom entities sit at `unknown`."""
    (h.paths.custom_components / "acme_pool").mkdir()
    (h.paths.custom_components / "acme_pool" / "manifest.json").write_text("{}")
    h.fake.entries.append({"entry_id": "env1", "domain": "acme_pool", "title": "Acme Pool", "state": "loaded"})
    old = "2026-01-01T00:00:00+00:00"
    stateless = [
        *(("acme_solar", f"event.keypad_{i}_button") for i in range(20)),
        *(("acme_solar", f"scene.scene_{i}") for i in range(10)),
        *(("acme_pool", f"button.house_alarm_panic_{i}") for i in range(2)),
        ("acme_pool", "switch.house_alarm_panel_chime"),
    ]
    for platform, entity_id in stateless:
        h.fake.states.append({"entity_id": entity_id, "state": "2026-01-20T10:00:00+00:00", "last_changed": old,
                              "attributes": {}})  # fmt: skip
        h.fake.registry.append({"entity_id": entity_id, "platform": platform, "config_entry_id": "x"})
    h.fake.states.append({"entity_id": "alarm_control_panel.house_alarm", "state": "disarmed", "last_changed": old,
                          "attributes": {}})  # fmt: skip
    h.fake.registry.append({"entity_id": "alarm_control_panel.house_alarm", "platform": "acme_pool"})


def reboot_to_unknown(h: Harness, hours_ago: float) -> None:
    for st in h.fake.states:
        if st["entity_id"].split(".")[0] in ("event", "scene", "button") or st["entity_id"].endswith("_chime"):
            h.fake.set_unavailable(st["entity_id"], hours_ago=hours_ago)
            st["state"] = "unknown"


async def test_restart_unknown_on_stateless_entities_is_not_an_outage(h: Harness) -> None:
    add_restart_scenario(h)
    await h.daemon().health_cycle("catch_up")
    pushes_before = len(h.pushes())
    reboot_to_unknown(h, hours_ago=10)  # well past the flap window
    outcome = await h.daemon().health_cycle("daily")
    assert outcome.result == "ok" and outcome.items == 0
    assert len(h.pushes()) == pushes_before and h.claude_calls == []


async def test_real_acme_solar_outage_is_still_critical(h: Harness) -> None:
    add_restart_scenario(h)
    await h.daemon().health_cycle("catch_up")
    reboot_to_unknown(h, hours_ago=10)
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        h.fake.set_unavailable(entity, hours_ago=0.2)  # fresh, hard outage: no flap delay
    outcome = await h.daemon(claude__token="").health_cycle("daily")
    assert "entities:acme_solar" in outcome.record["item_keys"]
    push = h.pushes()[-1]
    assert push["title"] == "Ops Agent: critical" and "3 custom integration acme_solar" in push["message"]


async def test_unknown_on_device_domains_waits_for_flap_window(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        h.fake.set_unavailable(entity, hours_ago=1)
        next(s for s in h.fake.states if s["entity_id"] == entity)["state"] = "unknown"
    assert (await h.daemon(claude__token="").health_cycle("daily")).items == 0
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        next(s for s in h.fake.states if s["entity_id"] == entity)["last_changed"] = "2026-01-01T00:00:00+00:00"
    outcome = await h.daemon(claude__token="").health_cycle("daily")
    assert "entities:acme_solar" in outcome.record["item_keys"]


async def test_items_raised_by_010_clear_on_next_check(h: Harness) -> None:
    from opsd.baseline import Item

    add_restart_scenario(h)
    d = h.daemon(claude__token="")
    await d.health_cycle("catch_up")
    reboot_to_unknown(h, hours_ago=10)
    # What an older version left behind: open critical items in the panel and on the sensor.
    d._save_open_items([Item("entities:acme_solar", "entities", "critical", "144 custom integration acme_solar ...")],
                       datetime.now(UTC))  # fmt: skip
    pushes_before = len(h.pushes())
    outcome = await d.health_cycle("daily")
    assert outcome.items == 0 and d.open_items() == [] and d.state.pending_items == 0
    assert len(h.pushes()) == pushes_before
    assert h.client.states["sensor.ops_agent"][0] == "ok"


# ------------------------------------------------- effects as capabilities


async def test_in_run_effects(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    for entity in ("light.kitchen", "light.porch", "fan.bedroom"):
        h.fake.set_unavailable(entity, hours_ago=8)
    h.in_run_requests = [
        {"verb": "status"},
        {"verb": "ack", "args": {"key": "entities:acme_solar", "days": 1, "reason": "user notified"}},
        {"verb": "ack", "args": {"key": "custom:acme_solar", "days": 30, "accept": True}},
        {
            "verb": "notify",
            "args": {"title": "Acme Solar down", "message": "All Acme Solar entities unavailable", "urgent": True},
        },
        {"verb": "notify", "args": {"title": "again", "message": "second push"}},
    ]
    h.claude_reply = {"result": "escalated", "summary": "Acme Solar down"}
    outcome = await h.daemon().health_cycle("daily")
    assert [r["ok"] for r in h.in_run_responses] == [True, True, True, True, False]
    assert "accept refused" in h.in_run_responses[2]["output"]
    titles = [p["title"] for p in h.pushes()]
    # The AI's in-run push, then the deterministic safety push.
    assert titles[-2:] == ["Agent: Acme Solar down", "Ops Agent: critical"]
    baseline = read_json(h.paths.baseline_file, {})
    assert baseline["acks"]["entities:acme_solar"]["by"] == "claude"
    assert baseline["acks"]["custom:acme_solar"]["accepted"] is False
    assert outcome.record["unacked"] == []
    assert outcome.record["in_run_effects"] == {"acks": 2, "pushes": 1}
    audit = h.current.store.audit_rows(limit=50) if h.current else []
    assert {r["capability"] for r in audit} >= {"agent.status", "baseline.ack", "notify.admin"}
    assert h.current is not None and h.current.rpc is not None and h.current.rpc.active_run is None


async def test_in_run_acks_survive_a_failed_run(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.set_unavailable("sensor.old_thing", hours_ago=0)
    next(s for s in h.fake.states if s["entity_id"] == "light.porch")["state"] = "on"
    h.fake.config_ok = False
    h.in_run_requests = [{"verb": "ack", "args": {"key": "config:invalid", "days": 2}}]
    h.claude_status = "timeout"
    outcome = await h.daemon().health_cycle("daily")
    assert outcome.result == "timeout" and "config:invalid" not in outcome.record["unacked"]
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"


async def test_run_is_unbound_even_when_claude_crashes(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.config_ok = False
    d = h.daemon()

    async def boom(*_: Any, **__: Any) -> RunResult:
        assert d.rpc is not None and d.rpc.active_run is not None
        raise RuntimeError("runner bug")

    d.runner.run_claude = boom
    with pytest.raises(RuntimeError):
        await d.health_cycle("daily")
    assert d.rpc is not None and d.rpc.active_run is None


async def test_state_json_loads(h: Harness) -> None:
    from opsd.jsonstore import write_json_atomic as write

    today = datetime.now(UTC).date().isoformat()
    write(h.paths.state_file, {"spend_day": today, "spend_usd": 1.25, "last_result": "ok"})
    d = h.daemon()
    assert d.state.last_result == "ok"


# ------------------------------------------------- scheduler and dispatcher


async def test_schedule_catch_up_then_daily(h: Harness) -> None:
    from opsd.daemon import HEALTH_TASK, STATUS_TASK

    d = h.daemon()
    d.setup_schedule(startup_delay_s=0)
    task = d.store.system_task(HEALTH_TASK)
    assert task is not None and task.payload == {"trigger": "catch_up"}
    assert d.store.system_task(STATUS_TASK) is not None
    d.scheduler.tick()
    event = await d.queue.get()
    assert event.type == "health.requested" and event.payload["trigger"] == "catch_up"
    await d.dispatcher.handle(_acquire(d, event))
    assert h.client.states["sensor.ops_agent"][0] == "ok"
    task = d.store.system_task(HEALTH_TASK)
    assert task is not None and task.state == "pending" and task.payload == {"trigger": "daily"}
    assert task.next_fire_at is not None and datetime.fromisoformat(task.next_fire_at) > datetime.now(UTC)
    # Not stale any more: a restart schedules the next daily run, not a catch-up.
    d.setup_schedule(startup_delay_s=0)
    task = d.store.system_task(HEALTH_TASK)
    assert task is not None and task.payload == {"trigger": "daily"}
    assert datetime.fromisoformat(task.next_fire_at or "") > datetime.now(UTC)


def _acquire(d: Daemon, event: Any) -> Any:
    """The dispatcher releases a slot after each event; take one as `run` would."""
    d.dispatcher._slots._value -= 1
    return event


async def test_restart_resumes_a_task_caught_mid_run(h: Harness) -> None:
    from opsd.daemon import HEALTH_TASK

    d = h.daemon()
    d.setup_schedule(startup_delay_s=0)
    d.scheduler.tick()
    task = d.store.system_task(HEALTH_TASK)
    assert task is not None and task.state == "running"
    d.close()
    d2 = h.daemon()
    d2.state.last_health_at = None
    d2.setup_schedule(startup_delay_s=0)
    d2.scheduler.tick()
    event = await d2.queue.get()
    assert event.type == "health.requested" and event.payload["trigger"] == "catch_up"


async def test_manual_request_dedupes_with_scheduled_check(h: Harness) -> None:
    d = h.daemon()
    assert d.request_cycle("manual")
    assert not d.request_cycle("manual") and not d.request_cycle("core_recovered")
    d.setup_schedule(startup_delay_s=0)
    d.scheduler.tick()  # the due daily check is covered by the queued manual one
    assert len(d.queue) == 1
    from opsd.daemon import HEALTH_TASK

    task = d.store.system_task(HEALTH_TASK)
    assert task is not None and task.state == "pending"


async def test_status_refresh_and_unknown_task(h: Harness) -> None:
    from opsd.events import Event, Priority

    d = h.daemon()
    d.setup_schedule(startup_delay_s=0)
    from opsd.daemon import STATUS_TASK

    status = d.store.system_task(STATUS_TASK)
    assert status is not None
    d.store.claim_due(datetime.fromisoformat(status.next_fire_at or ""))
    event = Event("status.refresh", "scheduler", Priority.MAINTENANCE, {"task_id": status.id})
    await d.dispatcher.handle(_acquire(d, event))
    assert "sensor.ops_agent" in h.client.states
    after = d.store.system_task(STATUS_TASK)
    assert after is not None and after.state == "pending"
    await d.dispatcher.handle(_acquire(d, Event("schedule.fired", "scheduler", Priority.FOLLOWUP, {"task_id": 999})))
    await d.dispatcher.handle(_acquire(d, Event("user.message", "companion", Priority.USER)))
    await d.dispatcher.handle(_acquire(d, Event("no.such", "x", Priority.USER)))


async def test_overdue_check_requests_a_cycle(h: Harness) -> None:
    from datetime import timedelta

    d = h.daemon()
    now = datetime.now(UTC)
    d._check_overdue(now)
    assert len(d.queue) == 0  # within the startup grace
    later = now + timedelta(hours=3)
    d._check_overdue(later)
    assert len(d.queue) == 1
    d.queue.done(await d.queue.get())
    d._check_overdue(later + timedelta(hours=1))
    assert len(d.queue) == 0  # retried at most every 6 h
    d.state.last_health_at = later.isoformat()
    d._check_overdue(later + timedelta(hours=7))
    assert len(d.queue) == 0


async def test_health_handler_survives_a_crash(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd.events import Event, Priority

    d = h.daemon()

    async def boom(_trigger: str) -> None:
        raise RuntimeError("crash")

    monkeypatch.setattr(d, "health_cycle", boom)
    await d.dispatcher.handle(_acquire(d, Event("health.requested", "manual", Priority.TRIAGE, {"trigger": "manual"})))
    assert h.client.states["sensor.ops_agent"][0] == "failed"


async def test_manual_trigger_refused_when_store_broken(h: Harness) -> None:
    d = h.daemon()
    d.store.close()
    assert not d.request_cycle("manual")
    assert d.request_cycle("core_recovered")  # never depends on the store


async def test_seed_reports_assist_exposure(h: Harness) -> None:
    h.fake.states.append({"entity_id": "lock.front_door", "state": "locked", "attributes": {}})
    h.fake.exposed["lock.front_door"] = {"conversation": True}
    await h.daemon(claude__token="").health_cycle("catch_up")
    seed_push = h.pushes()[-1]
    assert "exposed to Assist: lock.front_door" in seed_push["message"]
    assert seed_push["data"]["push"] == {"interruption-level": "time-sensitive"}
    outcome = await h.daemon(claude__token="").health_cycle("daily")
    assert "assist:lock.front_door" in outcome.record["item_keys"]
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"


async def test_refresh_admin_requires_an_active_admin(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd.jsonstore import write_json_atomic as write

    users: list[dict[str, Any]] = [{"id": "u1", "is_active": True, "group_ids": ["system-admin"]}]
    original_ws = h.fake.ws

    async def ws(msg_type: str, **fields: Any) -> Any:
        if msg_type == "config/auth/list":
            return users
        return await original_ws(msg_type, **fields)

    monkeypatch.setattr(h.fake, "ws", ws)
    d = h.daemon()
    assert await d.refresh_admin() is None  # no registration file yet
    entries = {"data": {"entries": [{"domain": "mobile_app",
                                     "data": {"device_name": "Admin Phone", "user_id": "u1"}}]}}  # fmt: skip
    write(h.paths.core_config_entries, entries)
    identity = await d.refresh_admin()
    assert identity is not None and identity.user_id == "u1" and d.inbound.admin == identity
    assert d.status_view()["inbound"] == "subscribed"
    users[0]["group_ids"] = ["system-users"]
    assert await d.refresh_admin() is None and d.inbound.admin is None


async def test_a_notify_device_of_a_non_admin_fails_closed(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile's (or option's) device belongs to someone who is not an active admin."""
    from opsd.channels import BudgetClass, Message
    from opsd.jsonstore import write_json_atomic as write

    users: list[dict[str, Any]] = [{"id": "u2", "is_active": True, "group_ids": ["system-users"]}]
    original_ws = h.fake.ws

    async def ws(msg_type: str, **fields: Any) -> Any:
        if msg_type == "config/auth/list":
            return users
        return await original_ws(msg_type, **fields)

    monkeypatch.setattr(h.fake, "ws", ws)
    entries = {"data": {"entries": [{"domain": "mobile_app",
                                     "data": {"device_name": "Admin Phone", "user_id": "u2"}}]}}  # fmt: skip
    write(h.paths.core_config_entries, entries)
    d = h.daemon(agent__mode="act", updates__auto_install="on")
    assert d.options.acting and d.options.valid
    assert await d.refresh_admin() is None
    # Fail closed: no act mode, no automatic updates, no AI runs.
    assert not d.options.valid and not d.options.acting and not d.options.auto_updates_on
    assert d.runner.options.runtime.get(daemon_mod.NOTIFY_NOT_ADMIN) is not None  # every holder sees it
    # No push reaches the device, safety pushes included (notifications go to the admin only).
    now = datetime.now(UTC)
    assert await d.send(Message("t", "normal", budget_class=BudgetClass.AGENT), core_up=True, now=now) == "failed"
    safety = Message("t", "core down", urgent=True, budget_class=BudgetClass.SAFETY)
    assert await d.send(safety, core_up=True, now=now) == "failed" and h.pushes() == []
    # The System sheet and the health check say why.
    assert "not an active Home Assistant admin" in str(d.profile_view()["notify_problem"])
    notice = d.options_notice()
    assert notice is not None and any("not an active" in p for p in notice["problems"])
    await d.health_cycle("catch_up")
    outcome = await d.health_cycle("daily")
    assert "options:invalid" in outcome.record["item_keys"]
    # Made an admin again: the block lifts.
    users[0]["group_ids"] = ["system-admin"]
    assert await d.refresh_admin() is not None and d.options.valid and d.options.acting
    assert await d.send(safety, core_up=True, now=now) == "sent"


async def test_degraded_without_store(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd.store import Store, StoreUnavailable

    def unavailable(*_: Any, **__: Any) -> Any:
        raise StoreUnavailable("SQLITE_FULL: database or disk is full")

    monkeypatch.setattr(Store, "open_or_recover", unavailable)
    d = h.daemon()
    assert d.store is None and d.scheduler is None and "SQLITE_FULL" in (d.store_error or "")
    assert await d.alert_store_unavailable() == "sent"
    alert = h.pushes()[-1]
    assert alert["title"] == "Ops Agent: agent degraded" and alert["data"]["push"] == {
        "interruption-level": "time-sensitive"
    }
    await d.health_cycle("catch_up")
    h.fake.config_ok = False
    outcome = await d.health_cycle("daily")
    # No AI run without the ledger and audit log; the safety push still goes out.
    assert outcome.result == "store_unavailable" and h.claude_calls == []
    assert h.pushes()[-1]["title"] == "Ops Agent: critical"
    assert not d.request_cycle("manual") and d.request_cycle("core_recovered")
    d.setup_schedule(startup_delay_s=0)
    assert d.status_view()["store"].startswith("unavailable")
    assert d.rpc is not None
    response = await d.rpc.dispatch(b'{"verb": "status"}')
    assert response["ok"]  # in-process reads (no uid separation in tests) work unaudited
    d.close()


async def test_agent_push_cannot_impersonate_a_safety_push(h: Harness) -> None:
    await h.daemon().health_cycle("catch_up")
    h.fake.config_ok = False
    h.in_run_requests = [{"verb": "notify", "args": {"title": "Ops Agent: Core is back", "message": "all fine",
                                                     "urgent": True}}]  # fmt: skip
    h.claude_reply = {"result": "ok", "summary": "s"}
    await h.daemon().health_cycle("daily")
    agent, safety = h.pushes()[-2:]
    assert agent["title"] == "Agent: Ops Agent: Core is back" and agent["data"]["tag"] == "ha-ops-agent"
    assert safety["title"] == "Ops Agent: critical" and safety["data"]["tag"] == "ha-ops-fallback"
