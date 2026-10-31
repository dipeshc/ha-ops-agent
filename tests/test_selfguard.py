"""The agent never modifies itself: every path to the add-on, the Supervisor or the host is refused."""

import asyncio
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from opsd import reload as reload_mod
from opsd import selfguard
from opsd.capabilities import CAPABILITIES, CapabilityError, parse_request
from opsd.changepaths import allowed_path, fix_path
from opsd.changes import ChangeRefused, check_paths
from opsd.config import Paths
from opsd.store import UpdateJob
from opsd.supervisor import ApiError, SupervisorClient
from opsd.updexec import ExecutorRefused, Executors
from opsd.ws import CommandError, HAWebSocket
from tests.conftest import SAMPLE_PROFILE, make_options

SELF_HTTP = [
    ("POST", "/addons/local_ha_ops_agent/options", {"options": {}}),
    ("POST", "/addons/local_ha_ops_agent/start", None),
    ("POST", "/addons/local_ha_ops_agent/stop", None),
    ("POST", "/addons/local_ha_ops_agent/restart", None),
    ("POST", "/addons/local_ha_ops_agent/rebuild", None),
    ("POST", "/addons/local_ha_ops_agent/uninstall", None),
    ("POST", "/addons/local_ha_ops_agent/update", None),
    ("POST", "/addons/local_ha_ops_agent/stdin", "x"),
    ("POST", "/addons/local_ha_ops_agent/security", {"protected": False}),
    ("POST", "/addons/self/options", {}),
    ("POST", "/addons/self/restart", None),
    ("DELETE", "/addons/local_ha_ops_agent", None),
    ("POST", "/store/addons/local_ha_ops_agent/update", {"backup": False}),
    ("POST", "/store/addons/local_ha_ops_agent/install", None),
    ("POST", "/store/addons/self/update", None),
    ("POST", "/supervisor/update", None),
    ("POST", "/supervisor/restart", None),
    ("POST", "/supervisor/options", {}),
    ("POST", "/supervisor/repair", None),
    ("POST", "/supervisor/reload", None),
    ("POST", "/host/reboot", None),
    ("POST", "/host/reboot", {}),
    # Outside the approved HAOS flow the OS update is refused like the rest of /os.
    ("POST", "/os/update", {"version": "17.4"}),
    ("POST", "/host/shutdown", None),
    ("POST", "/host/options", {}),
    ("POST", "/os/datadisk/move", {"device": "x"}),
    ("POST", "/os/boot-slot", {}),
    ("POST", "/backups/abc123/restore/full", {}),
    ("POST", "/backups/abc123/restore/partial", {"addons": ["local_ha_ops_agent"]}),
    ("POST", "/core/api/services/hassio/addon_stop", {"addon": "local_ha_ops_agent"}),
    ("POST", "/core/api/services/hassio/host_reboot", {}),
    ("POST", "/core/api/services/hassio/backup_full", {}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.ha_ops_agent_update"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.home_assistant_supervisor_update"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.home_assistant_operating_system_update"}),
    (
        "POST",
        "/core/api/services/update/install",
        {"target": {"entity_id": ["update.x", "update.ha_ops_agent_update"]}},
    ),
    ("POST", "/core/api/services/update/skip", {"entity_id": "update.ha_ops_agent_update"}),
    ("POST", "/core/api/services/update/install", {}),
    # Case (review r2, HIGH 1).
    ("POST", "/core/api/services/HASSIO/addon_restart", {"addon": "self"}),
    ("POST", "/core/api/services/Update/install", {"entity_id": "update.ha_ops_agent_update"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "UPDATE.HA_OPS_AGENT_UPDATE"}),
    ("POST", "/Addons/self/restart", None),
    # Selectors and non-entity targets (HIGH 2).
    ("POST", "/core/api/services/update/install", {"entity_id": "all"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "none"}),
    ("POST", "/core/api/services/update/install", {"area_id": "utility_room"}),
    ("POST", "/core/api/services/update/install", {"target": {"device_id": "abc"}}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.x", "floor_id": "up"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.x", "label_id": ["l"]}),
    ("POST", "/core/api/services/update/install", {"entity_id": "light.kitchen"}),
    # Core's Supervisor proxy (HIGH 3).
    ("POST", "/core/api/hassio/addons/self/restart", None),
    ("POST", "/core/api/hassio/supervisor/restart", None),
    ("POST", "/core/api/hassio_ingress/abc/x", None),
    # Renamed entities and repositories (MEDIUM).
    ("POST", "/core/api/services/update/install", {"entity_id": "update.ha_ops_agent_update_2"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.my_ha_ops_agent"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.home_assistant_supervisor_update_2"}),
    ("POST", "/store/repositories", {"repository": "https://example.com/x"}),
    # Path tricks (MEDIUM).
    ("POST", "/addons/%6cocal_ha_ops_agent/restart", None),
    ("POST", "/addons/x/../self/restart", None),
    ("POST", "/addons\\self/restart", None),
    ("POST", "/addons/self/restart\x00", None),
    # Spelling tricks.
    ("post", "//addons//local_ha_ops_agent//restart", None),
    ("POST", "/addons/local_ha_ops_agent/restart/", None),
    ("POST", "/addons/local_ha_ops_agent/restart?force=1", None),
    ("PUT", "/supervisor/options", {}),
    # What the denylist still let through, and anything else the agent never sends.
    ("POST", "/core/stop", {}),
    ("POST", "/addons/core_mosquitto/stop", None),
    ("POST", "/addons/core_mosquitto/uninstall", None),
    ("POST", "/addons/core_mosquitto/options", {"options": {}}),
    ("POST", "/backups/new/full", {"name": "x"}),
    ("DELETE", "/backups/ops-pre-update-1", None),
    ("POST", "/core/api/services/homeassistant/restart", {}),
    ("POST", "/core/api/services/homeassistant/stop", {}),
    ("POST", "/core/api/services/lock/unlock", {"entity_id": "lock.front_door"}),
    ("POST", "/core/api/services/input_boolean/turn_on", {"entity_id": "input_boolean.guest_mode"}),
    ("POST", "/core/api/services/notify/persistent_notification", {"message": "x"}),
    ("POST", "/core/api/states/lock.front_door", {"state": "unlocked"}),
    ("POST", "/core/api/states/sensor.ops_agent/x", {"state": "ok"}),
    ("POST", "/core/update", {"version": "2026.10.4"}),
    ("POST", "/core/update", {"version": "2026.10.4", "backup": False, "extra": 1}),
    ("POST", "/core/restart", {"safe_mode": True}),
    ("POST", "/core/check?x=1", {}),
    ("POST", "/store/addons/core_example_broker/update", {"backup": True}),
    ("POST", "/core/api/services/update/install", {"target": {"entity_id": ["update.hue_bridge_firmware"]}}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.x", "backup": True}),
    ("POST", "/core/api/config/config_entries/entry/abc/reload", {"x": 1}),
    ("PATCH", "/core/api/states/sensor.ops_agent", {}),
    ("OPTIONS", "/supervisor/options", None),
]

ALLOWED_HTTP = [
    ("GET", "/addons/self/info", None),
    ("GET", "/supervisor/info", None),
    ("GET", "/host/logs", None),
    ("POST", "/core/update", {"version": "2026.10.4", "backup": False}),
    ("POST", "/core/check", {}),
    ("POST", "/core/restart", {}),
    ("POST", "/store/addons/core_example_broker/update", {"backup": False}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.hue_bridge_firmware", "version": "1.2"}),
    ("POST", "/core/api/services/update/install", {"entity_id": "update.hue_bridge_firmware"}),
    ("POST", "/core/api/config/config_entries/entry/abc/reload", {}),
    ("POST", "/core/api/config/config_entries/entry/01JD8KZQ3M5N6P7R8S9T0V1W2X/reload", {}),
    ("POST", "/core/api/config/core/check_config", None),
    ("POST", "/core/api/services/automation/reload", {}),
    ("POST", "/core/api/services/template/reload", {}),
    ("POST", "/core/api/services/homeassistant/reload_custom_templates", {}),
    ("POST", "/core/api/services/input_boolean/turn_off", {"entity_id": "input_boolean.ops_agent_ai_enabled"}),
    ("POST", "/core/api/services/notify/mobile_app_admin_phone", {"title": "t", "message": "m"}),
    ("POST", "/core/api/states/sensor.ops_agent", {"state": "ok"}),
    ("POST", "/core/api/states/sensor.ops_agent_share_5h", {"state": "1"}),
]


@pytest.mark.parametrize(("method", "path", "body"), SELF_HTTP)
def test_http_self_targets_are_refused(method: str, path: str, body: Any) -> None:
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_http(method, path, body)


@pytest.mark.parametrize(("method", "path", "body"), ALLOWED_HTTP)
def test_ordinary_calls_still_pass(method: str, path: str, body: Any) -> None:
    selfguard.check_http(method, path, body)


# ---------------------------------------------- the approved HAOS flow: exact and scoped


def test_the_os_flow_lets_through_exactly_the_one_call_it_names() -> None:
    install_refused = [
        ("POST", "/host/reboot", {}),  # the install scope is not the reboot scope
        ("POST", "/os/update", {"version": "17.4", "backup": True}),
        ("POST", "/os/update", {}),
        ("POST", "/os/update", None),
        ("POST", "/os/update", {"version": ""}),
        ("POST", "/os/update?version=17.5", {"version": "17.4"}),
        ("PUT", "/os/update", {"version": "17.4"}),
        ("POST", "/os/boot-slot", {"boot_slot": "A"}),
        ("POST", "/os/datadisk/move", {"device": "x"}),
        ("POST", "/OS/update", {"version": "17.4"}),
        ("POST", "/os/%75pdate", {"version": "17.4"}),
    ]
    reboot_refused = [
        ("POST", "/host/reboot", {"force": True}),  # never skip the offline-migration check
        ("POST", "/host/reboot?force=1", {}),
        ("POST", "/host/reboot#x", {}),
        ("POST", "/host/reboot", {"force": False}),
        ("POST", "/host/shutdown", {}),
        ("POST", "/host/options", {}),
        ("POST", "/os/update", {"version": "17.4"}),
        ("POST", "/supervisor/restart", None),
        ("POST", "/addons/self/restart", None),
        ("POST", "/core/api/services/hassio/host_reboot", {}),
    ]
    for scope, refused in (("/os/update", install_refused), ("/host/reboot", reboot_refused)):
        for method, path, body in refused:
            with selfguard.approved_os_flow(scope), pytest.raises(selfguard.SelfTargetError):
                selfguard.check_http(method, path, body)
    with selfguard.approved_os_flow("/os/update"):
        selfguard.check_http("POST", "/os/update", {"version": "17.4"})
    for body in ({}, None):
        with selfguard.approved_os_flow("/host/reboot"):
            selfguard.check_http("POST", "/host/reboot", body)
    # The scope ends with the block.
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_http("POST", "/host/reboot", {})


def test_the_os_flow_is_single_use() -> None:
    """Review finding 3: one approved call per scope; a refused attempt does not use it up."""
    with selfguard.approved_os_flow("/host/reboot"):
        with pytest.raises(selfguard.SelfTargetError):
            selfguard.check_http("POST", "/host/reboot", {"force": True})
        selfguard.check_http("POST", "/host/reboot", {})
        with pytest.raises(selfguard.SelfTargetError):
            selfguard.check_http("POST", "/host/reboot", {})
    with selfguard.approved_os_flow("/os/update"):
        selfguard.check_http("POST", "/os/update", {"version": "17.4"})
        with pytest.raises(selfguard.SelfTargetError):
            selfguard.check_http("POST", "/os/update", {"version": "17.4"})


async def test_a_task_created_inside_the_os_flow_loses_it_when_the_block_ends() -> None:
    """Review finding 3: the reviewer's probe. A task spawned inside inherits the scope object, which closes."""
    release = asyncio.Event()
    seen: list[str] = []

    async def lingering() -> None:
        await release.wait()
        try:
            selfguard.check_http("POST", "/host/reboot", {})
            seen.append("allowed")
        except selfguard.SelfTargetError:
            seen.append("refused")

    with selfguard.approved_os_flow("/host/reboot"):
        task = asyncio.create_task(lingering())
    release.set()
    await task
    assert seen == ["refused"]
    # And a task spawned inside cannot take the call before the executor does and then leave it to it.
    with selfguard.approved_os_flow("/host/reboot"):
        selfguard.check_http("POST", "/host/reboot", {})
        task = asyncio.create_task(lingering())
        await task
    assert seen == ["refused", "refused"]


@pytest.mark.parametrize("path", ["/host/shutdown", "/os/boot-slot", "/supervisor/restart", "/addons/self/restart", ""])
def test_the_os_flow_names_only_the_update_and_the_reboot(path: str) -> None:
    with pytest.raises(ValueError, match="not an approved OS-flow path"), selfguard.approved_os_flow(path):
        pass


async def test_the_os_flow_does_not_leak_into_other_tasks() -> None:
    seen: list[str] = []
    entered = asyncio.Event()
    done = asyncio.Event()

    async def other() -> None:
        await entered.wait()
        try:
            selfguard.check_http("POST", "/host/reboot", {})
            seen.append("allowed")
        except selfguard.SelfTargetError:
            seen.append("refused")
        done.set()

    task = asyncio.create_task(other())
    with selfguard.approved_os_flow("/host/reboot"):
        entered.set()
        await done.wait()
        # A worker thread started for the call itself (asyncio.to_thread) carries the scope.
        await asyncio.to_thread(selfguard.check_http, "POST", "/host/reboot", {})
    await task
    assert seen == ["refused"]


@pytest.mark.parametrize(("method", "path", "body"), SELF_HTTP)
def test_the_client_refuses_before_any_network(
    method: str, path: str, body: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_network(*_: Any, **__: Any) -> Any:
        raise AssertionError("a self-target call reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    client = SupervisorClient("token")
    with pytest.raises(ApiError) as caught:
        client.request(method, path, body)
    assert caught.value.status == 403 and "refused" in str(caught.value)


SELF_WS_EXTRA = [
    ("call_service", {"domain": "HASSIO", "service": "addon_restart"}),
    ("CALL_SERVICE", {"domain": "hassio", "service": "addon_restart"}),
    ("call_service", {"domain": "update", "service": "install", "target": {"entity_id": "all"}}),
    ("call_service", {"domain": "update", "service": "install", "target": {"area_id": "x"}}),
    (
        "call_service",
        {"domain": "Update", "service": "install", "service_data": {"entity_id": "UPDATE.HA_OPS_AGENT_UPDATE"}},
    ),
    ("Supervisor/api", {"endpoint": "/supervisor/restart", "method": "post"}),
]
SELF_WS = [
    *SELF_WS_EXTRA,
    ("supervisor/api", {"endpoint": "/addons/local_ha_ops_agent/restart", "method": "post"}),
    ("supervisor/api", {"endpoint": "/supervisor/info", "method": "get"}),
    ("hassio/addon/stop", {}),
    ("backup/restore", {"backup_id": "x"}),
    ("execute_script", {"sequence": []}),
    ("call_service", {"domain": "hassio", "service": "addon_restart", "service_data": {"addon": "self"}}),
    ("call_service", {"domain": "update", "service": "install", "target": {"entity_id": "update.ha_ops_agent_update"}}),
    ("call_service", {"domain": "update", "service": "install"}),
    # Only the commands the agent sends, with their pinned arguments.
    ("call_service", {"domain": "light", "service": "turn_on"}),
    ("config_entries/delete", {"entry_id": "x"}),
    ("config/entity_registry/update", {"entity_id": "lock.x"}),
    ("subscribe_events", {"event_type": "state_changed"}),
    ("subscribe_events", {}),
    ("subscribe_trigger", {"trigger": {"platform": "state", "entity_id": "lock.front_door"}}),
    ("input_boolean/create", {"name": "Guest mode"}),
    ("Backup/info", {}),
]


@pytest.mark.parametrize(("msg_type", "fields"), SELF_WS)
def test_ws_self_targets_are_refused(msg_type: str, fields: dict[str, Any]) -> None:
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_ws(msg_type, fields)


class Writer:
    def __init__(self) -> None:
        self.frames: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.frames.append(data)

    async def drain(self) -> None:
        return None


def connected() -> tuple[HAWebSocket, Writer]:
    ws = HAWebSocket("ws://supervisor/core/websocket", "token")
    writer = Writer()
    ws._writer = writer  # type: ignore[assignment]
    return ws, writer


@pytest.mark.parametrize(("msg_type", "fields"), SELF_WS)
def test_every_websocket_send_path_refuses_before_writing(msg_type: str, fields: dict[str, Any]) -> None:
    for send in ("command", "send_command", "send_json"):
        ws, writer = connected()
        with pytest.raises(CommandError, match="refused"):
            if send == "send_json":
                asyncio.run(ws.send_json({"id": 1, "type": msg_type, **fields}))
            else:
                asyncio.run(getattr(ws, send)(msg_type, **fields))
        assert writer.frames == [], send


def test_websocket_auth_and_pings_still_go_out() -> None:
    ws, writer = connected()
    asyncio.run(ws.send_json({"type": "auth", "access_token": "t"}))
    asyncio.run(ws.send_command("ping"))
    assert len(writer.frames) == 2


@pytest.mark.parametrize(
    "msg_type", ["config_entries/get", "backup/generate_with_automatic_settings", "vacuum/get_segments"]
)
def test_ws_reads_and_backups_pass(msg_type: str) -> None:
    selfguard.check_ws(msg_type, {"id": 7})


def test_the_agents_own_subscriptions_and_switch_pass() -> None:
    selfguard.check_ws("subscribe_events", {"id": 1, "event_type": "mobile_app_notification_action"})
    trigger = {"platform": "state", "entity_id": "input_boolean.ops_agent_ai_enabled"}
    selfguard.check_ws("subscribe_trigger", {"id": 2, "trigger": trigger})
    selfguard.check_ws("input_boolean/create", {"id": 3, "name": "Ops Agent AI enabled", "icon": "mdi:robot-outline",
                                                "initial": False})  # fmt: skip


SELF_PATHS = [
    "addons/ha-ops-agent/config.yaml",
    "addons/ha-ops-agent/rootfs/opt/ops/opsd/selfguard.py",
    "addons/other/thing.yaml",
    ".git/config",
    "automations/.git/hooks/x.yaml",
    "/data/options.json",
    "data/options.json",
    "/ssl/key.pem",
    "automations/ha_ops_agent/x.yaml",
    "alerts/ha-ops-agent.yaml",
    "automations/github_deploy.yaml",
]


@pytest.mark.parametrize("path", SELF_PATHS)
def test_proposals_can_never_name_the_agent(path: str) -> None:
    rules = SAMPLE_PROFILE.proposal_rules
    assert selfguard.self_path(path)
    assert not allowed_path(path, rules)
    with pytest.raises(ChangeRefused):
        check_paths([path], kind="change", rules=rules)


def test_ordinary_proposal_paths_still_pass() -> None:
    for path in ("automations/hall/lights.yaml", "alerts/fridge.yaml", "templates/status.yaml"):
        assert allowed_path(path, SAMPLE_PROFILE.proposal_rules)


def test_the_house_profile_is_never_a_proposal_path() -> None:
    """ops/ holds the agent's own configuration: no rules, however wide, reach it."""
    assert selfguard.self_path("ops/profile.yaml")
    assert not allowed_path("ops/profile.yaml", SAMPLE_PROFILE.proposal_rules)
    assert not fix_path("ops/profile.yaml")


def job(entity_id: str, kind: str, slug: str | None = None) -> UpdateJob:
    return UpdateJob(id=1, run_id=1, seq=1, entity_id=entity_id, kind=kind, title="t", from_version="1.0.0",
                     to_version="1.0.1", target={"slug": slug} if slug else {}, state="planned", install_attempts=0,
                     rollback_attempts=0, detail="", regressions=[], started_at=None,
                     finished_at=None, updated_at="")  # fmt: skip


@pytest.mark.parametrize(
    "bad",
    [
        job("update.ha_ops_agent_update", "addon", "local_ha_ops_agent"),
        job("update.some_addon_update", "addon", "local_ha_ops_agent"),
        job("update.some_addon_update", "addon", "self"),
        job("update.home_assistant_supervisor_update", "supervisor"),
        job("update.anything", "supervisor"),
    ],
)
def test_update_jobs_never_target_the_agent_or_the_supervisor(bad: UpdateJob) -> None:
    with pytest.raises(ExecutorRefused):
        Executors._check_job(bad)


def test_update_approval_requests_for_the_agent_are_refused(paths: Paths) -> None:
    from opsd.proposals import validate_request
    from opsd.runctx import ExecContext, ExecEnv

    cap = CAPABILITIES["approval.request"]
    _, args = parse_request(json.dumps({"verb": cap.verb, "args": {"entity": "update.ha_ops_agent_update"}}).encode())
    env = ExecEnv(paths=paths, options=make_options(), client=None, redactor=lambda: None)  # type: ignore[arg-type]

    class Run:
        profile = "chat"

    ctx = ExecContext(env=env, capability=cap, run=Run())  # type: ignore[arg-type]
    with pytest.raises(CapabilityError, match="never updates itself"):
        validate_request(ctx, args)


def test_a_renamed_agent_update_entity_is_refused() -> None:
    for entity in ("update.ha_ops_agent_update_2", "update.local_ha_ops_agent_update"):
        with pytest.raises(ExecutorRefused):
            Executors._check_job(job(entity, "addon", "core_x"))


def test_titled_jobs() -> None:
    import dataclasses

    base = job("update.renamed", "hacs")
    for title in ("HA Ops Agent", "Home Assistant Supervisor"):
        with pytest.raises(ExecutorRefused):
            Executors._check_job(dataclasses.replace(base, title=title))
    Executors._check_job(dataclasses.replace(base, title="Mini Media Player"))
    os_job = dataclasses.replace(job("update.home_assistant_operating_system_update", "os"), title="HAOS")
    Executors._check_job(os_job)


@pytest.mark.parametrize("domain", ["lutron_caseta", "myq", "gogogate2", "lock", "alarm_control_panel", "cover"])
def test_garage_and_access_are_never_reloaded(domain: str) -> None:
    assert "never-reload" in (reload_mod.static_refusal(domain, {domain}) or "")


def test_the_acme_solar_is_never_reloaded_here() -> None:
    """The house profile adds it to the never-reload list (`reload.deny`)."""
    assert "never-reload" in (
        reload_mod.static_refusal("acme_solar", {"acme_solar"}, (), SAMPLE_PROFILE.reload.deny) or ""
    )


# ------------------------------------------------- end to end: the legitimate call sites still work


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req: urllib.request.Request, timeout: float | None = None) -> Any:
        self.calls.append((req.get_method(), req.full_url))

        class Resp:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"result": "ok", "data": {}}'

        return Resp()


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    monkeypatch.setattr(urllib.request, "urlopen", rec)
    return rec


def test_update_executor_calls_still_reach_the_supervisor(net: Recorder) -> None:
    from opsd.updexec import LiveUpdateOps

    ops = LiveUpdateOps(SupervisorClient("token"))
    asyncio.run(ops.update_core("2026.10.5"))
    asyncio.run(ops.core_check())
    asyncio.run(ops.core_restart())
    asyncio.run(ops.update_addon("core_example_broker"))

    async def in_flow(path: str, call: Any) -> None:
        with selfguard.approved_os_flow(path):
            await call

    asyncio.run(in_flow("/os/update", ops.update_os("17.4")))
    asyncio.run(in_flow("/host/reboot", ops.reboot_host()))
    asyncio.run(ops.install_entity("update.hue_bridge_firmware", "1.2"))
    paths = [url.removeprefix("http://supervisor") for _, url in net.calls]
    assert paths == ["/core/update", "/core/check", "/core/restart", "/store/addons/core_example_broker/update",
                     "/os/update", "/host/reboot",
                     "/core/api/services/update/install"]  # fmt: skip
    with pytest.raises(ApiError, match="refused"):
        asyncio.run(ops.update_addon(selfguard.SELF_SLUG))
    # Outside the approved flow neither OS call leaves the process.
    for call in (ops.update_os("17.4"), ops.reboot_host()):
        with pytest.raises(ApiError, match="refused"):
            asyncio.run(call)
    assert len(net.calls) == 7


def test_reload_and_check_config_still_reach_core(net: Recorder) -> None:
    from opsd.api import LiveAPI

    client = SupervisorClient("token")
    asyncio.run(LiveAPI(client).core_post("/config/config_entries/entry/01JD8KZQ3M5N6P7R8S9T0V1W2X/reload"))
    client.core("/config/core/check_config", "POST", None)
    client.call_service("automation", "reload", {})
    client.call_service("notify", "mobile_app_admin_phone", {"message": "x"})
    assert len(net.calls) == 4


def test_release_notes_websocket_command_is_allowed() -> None:
    ws, writer = connected()
    asyncio.run(ws.send_command("update/release_notes", entity_id="update.home_assistant_core_update"))
    asyncio.run(ws.send_command("backup/generate_with_automatic_settings"))
    assert len(writer.frames) == 2


@pytest.mark.parametrize("domain", ["hassio", "backup", "homekit", "cloud", "mobile_app"])
def test_reload_can_never_target_the_platform(domain: str) -> None:
    assert reload_mod.static_refusal(domain, {domain}) is not None


def test_no_capability_argument_can_carry_a_self_slug_into_a_url(tmp_path: Path) -> None:
    """Every enabled, Claude-reachable mutating capability: its arguments are refused or never reach HTTP unguarded.

    The central guard sits under every executor (SupervisorClient.request and HAWebSocket.command), so this
    checks the inventory: any capability that may change Home Assistant is one we reviewed for self-targets.
    """
    reviewed = {
        "integration.reload",  # domain only; DENY_DOMAINS + the HTTP guard
        "change.propose", "facts.propose", "suppress.propose",  # changepaths.allowed_path + self_path
        "approval.request",  # validate_request + Executors._check_job
        "task.create", "task.cancel", "task.investigate",  # schedule only
        "baseline.ack", "notify.admin",  # agent state, pushes
    }  # fmt: skip
    reachable = {c.name for c in CAPABILITIES.values() if c.enabled and not c.internal and not c.read_only}
    assert reachable <= reviewed, f"new mutating capability needs a self-target review: {reachable - reviewed}"
    internal = {c.name for c in CAPABILITIES.values() if c.enabled and c.internal}
    # change.autofix: paths through changepaths.allowed_path (self_path) or configuration.yaml only.
    assert internal <= {"change.apply", "change.revert", "change.autofix", "backup.create",
                        "update.install", "update.rollback"}  # fmt: skip
