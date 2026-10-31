"""Shared fixtures: temp paths and a fake HA (states, registry, entries...)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd.config import Options, Paths
from opsd.profile import HouseProfile, parse_text
from opsd.supervisor import ApiError

ADDON_ROOT = Path(__file__).resolve().parents[1] / "ha_ops_agent"
OPT = ADDON_ROOT / "rootfs" / "opt" / "ops"
# Tests run against the real clock because the daemon uses datetime.now().
NOW = datetime.now(UTC).replace(microsecond=0)
# The sample house the fixtures describe (made-up integrations, two locks and an alarm, git on; no overlays).
SAMPLE_PROFILE_FILE = Path(__file__).parent / "fixtures" / "profile.yaml"
SAMPLE_PROFILE = parse_text(SAMPLE_PROFILE_FILE.read_text())
# No profile file: the generic agent.
GENERIC = HouseProfile()
# Names the fixtures use, from the sample profile.
WATCHED = SAMPLE_PROFILE.updates.watched_entities
POWER = SAMPLE_PROFILE.health.power_entity
REMOTE = SAMPLE_PROFILE.health.remote_entity
FACTS = SAMPLE_PROFILE.docs.facts
NOTIFY = SAMPLE_PROFILE.notify_service


def make_options(*, strict: bool = True, profile: HouseProfile = SAMPLE_PROFILE, **dotted: Any) -> Options:
    """Options from `group__field=value` keywords (`agent__mode="act"`), parsed and checked like options.json.

    `profile`: the sample house by default; `GENERIC` for an install without a profile.
    """
    raw: dict[str, dict[str, Any]] = {}
    for key, value in dotted.items():
        group, name = key.split("__")
        # As JSON has them: lists, not tuples.
        raw.setdefault(group, {})[name] = list(value) if isinstance(value, tuple) else value
    options = Options.from_mapping(raw, profile=profile)
    assert options.valid or not strict, options.problems
    return options


def iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    ha = tmp_path / "homeassistant"
    (ha / "custom_components" / "acme_solar").mkdir(parents=True)
    (ha / "custom_components" / "acme_solar" / "manifest.json").write_text("{}")
    (ha / "custom_components" / "hacs").mkdir()
    (ha / "custom_components" / "hacs" / "manifest.json").write_text("{}")
    (ha / "secrets.yaml").write_text("wifi_password: hunter22secret\napi_key: 'abcdef123456'\n")
    (ha / "notes.md").write_text("# Notes\n\nNewest first.\n\n## 2026-01-24\n\nOld entry.\n")
    data = tmp_path / "data"
    data.mkdir()
    return Paths(
        data=data,
        ha_config=ha,
        run=tmp_path / "run",
        opt=OPT,
        export_root=tmp_path / "export",
        socket_dir=tmp_path / "sock",
        drafts_dir=tmp_path / "drafts",
    )


class FakeHA:
    """A configurable stand-in for the Core/Supervisor APIs used by `collect`."""

    def __init__(self) -> None:
        old = iso(NOW - timedelta(days=3))
        self.states: list[dict[str, Any]] = [
            {"entity_id": "light.kitchen", "state": "on", "last_changed": old, "attributes": {}},
            {"entity_id": "light.porch", "state": "off", "last_changed": old, "attributes": {}},
            {"entity_id": "fan.bedroom", "state": "off", "last_changed": old, "attributes": {}},
            {"entity_id": "sensor.old_thing", "state": "unavailable", "last_changed": old, "attributes": {}},
            {
                "entity_id": "update.home_assistant_core_update",
                "state": "on",
                "last_changed": old,
                "attributes": {"title": "Home Assistant Core", "installed_version": "2026.10.3",
                               "latest_version": "2026.10.4"},
            },
        ]  # fmt: skip
        self.registry: list[dict[str, Any]] = [
            {"entity_id": "light.kitchen", "platform": "acme_solar", "config_entry_id": "lut1"},
            {"entity_id": "light.porch", "platform": "acme_solar", "config_entry_id": "lut1"},
            {"entity_id": "fan.bedroom", "platform": "acme_solar", "config_entry_id": "lut1"},
            {"entity_id": "sensor.old_thing", "platform": "zha", "config_entry_id": "zha1"},
            {"entity_id": "update.home_assistant_core_update", "platform": "hassio"},
        ]
        self.entries: list[dict[str, Any]] = [
            {"entry_id": "lut1", "domain": "acme_solar", "title": "Acme Solar", "state": "loaded"},
            {"entry_id": "zha1", "domain": "zha", "title": "ZHA", "state": "loaded"},
        ]
        self.issues: list[dict[str, Any]] = []
        self.system_log: list[dict[str, Any]] = []
        self.addons = [{"slug": "core_mosquitto", "name": "Mosquitto broker"},
                       {"slug": "local_ha_ops_agent", "name": "HA Ops Agent"}]  # fmt: skip
        everything = {"homeassistant": True, "addons": ["core_mosquitto", "local_ha_ops_agent"], "folders": []}
        self.backups = [
            {"slug": "a", "name": "full-1", "date": iso(NOW - timedelta(days=2)), "type": "full", "protected": True,
             "content": everything},
            {"slug": "b", "name": "auto", "date": iso(NOW - timedelta(hours=10)), "type": "partial", "protected": True,
             "content": {"homeassistant": True, "addons": [], "folders": []}},
        ]  # fmt: skip
        # Core's backup manager (ws backup/info): empty unless a test sets it.
        self.backup_info: dict[str, Any] = {"backups": [], "next_automatic_backup": None, "state": "idle"}
        self.kernel_log = ""
        self.resolution: dict[str, Any] = {"issues": [], "unhealthy": [], "unsupported": []}
        self.exposed: dict[str, dict[str, bool]] = {"light.kitchen": {"conversation": True}}
        self.config_ok = True
        self.config_check_times_out = False
        self.core_state = "running"
        self.calls: list[tuple[str, Any]] = []
        # Acme Vacuum vacuums by entity id: {"options": registry options, "segments": [{id, name}]}.
        # Add the entity to `registry` too, as the collector only reads registered vacuums.
        self.vacuums: dict[str, dict[str, Any]] = {}
        self.areas: list[dict[str, Any]] = []
        self.ws_fields: list[tuple[str, dict[str, Any]]] = []

    async def ws(self, msg_type: str, **fields: Any) -> Any:
        self.calls.append(("ws", msg_type))
        self.ws_fields.append((msg_type, fields))
        if msg_type == "config/area_registry/list":
            return self.areas
        if msg_type == "config/entity_registry/get":
            vacuum = self.vacuums[fields["entity_id"]]
            return {"entity_id": fields["entity_id"], "platform": "acme_vacuum", "options": vacuum["options"]}
        if msg_type == "vacuum/get_segments":
            return {"segments": self.vacuums[fields["entity_id"]]["segments"]}
        return {
            "config/entity_registry/list": self.registry,
            "config_entries/get": self.entries,
            "repairs/list_issues": {"issues": self.issues},
            "system_log/list": self.system_log,
            "homeassistant/expose_entity/list": {"exposed_entities": self.exposed},
            "backup/info": self.backup_info,
        }[msg_type]

    async def core_post(self, path: str) -> Any:
        self.calls.append(("core_post", path))
        return {"require_restart": False}

    async def core_rest(self, path: str) -> Any:
        assert path == "/states"
        if self.core_state != "running":
            raise RuntimeError("core down")
        return self.states

    async def supervisor(self, path: str) -> Any:
        return {
            "/core/info": {"version": "2026.10.3", "state": self.core_state},
            "/host/info": {"boot_timestamp": 1000, "disk_total": 57.8, "disk_free": 48.2},
            "/supervisor/info": {"version": "2026.10.3"},
            "/os/info": {"version": "17.2"},
            "/resolution/info": self.resolution,
            "/backups": {"backups": self.backups},
            "/addons": {"addons": self.addons},
        }[path]

    async def supervisor_post(self, path: str, *, http_timeout: float | None = None) -> Any:
        assert path == "/core/check"
        if self.config_check_times_out:
            raise ApiError("POST", path, None, "timed out")
        if not self.config_ok:
            raise RuntimeError("Invalid config for 'automation'")
        return {}

    async def logs(self, path: str, lines: int) -> str:
        return self.kernel_log

    def set_unavailable(self, entity_id: str, hours_ago: float = 12) -> None:
        for st in self.states:
            if st["entity_id"] == entity_id:
                st["state"] = "unavailable"
                st["last_changed"] = iso(NOW - timedelta(hours=hours_ago))


@pytest.fixture
def fake_ha() -> FakeHA:
    return FakeHA()


@pytest.fixture
def meminfo(tmp_path: Path) -> Path:
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 3884000 kB\nMemAvailable: 2496000 kB\nSwapTotal: 1280000 kB\nSwapFree: 1135000 kB\n")
    return path


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture(autouse=True)
def isolated_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's ~/.gitconfig (signing, hooks) out of test repos."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def no_cloud_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloud fetches (opsd.evidence) never reach the network in tests: they fail like an offline house."""
    from opsd import evidence

    def offline(url: str, **_: Any) -> str:
        raise evidence.FetchError("offline in tests")

    monkeypatch.setattr(evidence, "fetch_text", offline)
