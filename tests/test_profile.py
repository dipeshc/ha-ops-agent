"""The house profile: strict schema, the generic agent without one, and hostile profiles.

A profile can add caution and name house things; no value in it can weaken an invariant (the daemon executes
and Claude proposes; locks, the alarm and garage doors are never operable; untrusted text is data; the agent
never modifies itself).
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import reload as reload_mod
from opsd import selfguard
from opsd.baseline import NO_CURRENT_BACKUP, Baseline, diff
from opsd.capabilities import CAPABILITIES, capability_table, unavailable
from opsd.changepaths import allowed_path
from opsd.config import Options, Paths
from opsd.health import Snapshot
from opsd.policy import Tier, UpdateFeature, UpdateInfo, classify
from opsd.privsep import build_export
from opsd.profile import PROFILE_PATH, HouseProfile, ProfileError, load, parse_text
from opsd.runner import CHAT
from tests.conftest import GENERIC, NOW, SAMPLE_PROFILE, SAMPLE_PROFILE_FILE, iso, make_options
from tests.test_daemon import Harness, h  # noqa: F401
from tests.test_rpc_effects import World


def write_profile(root: Path, text: str) -> None:
    (root / "ops").mkdir(parents=True, exist_ok=True)
    (root / PROFILE_PATH).write_text(text)


# ------------------------------------------------------------------ no profile: the generic agent


def test_no_profile_is_the_generic_agent(paths: Paths) -> None:
    profile, problems = load(paths.ha_config)
    assert profile == HouseProfile() and problems == ()
    options = Options.load(paths)
    assert options.valid and options.profile == GENERIC
    assert not options.git_on and options.available == frozenset()
    assert options.notify_service == "" and options.need_approval == frozenset() and options.reloadable == frozenset()
    assert options.reads.files == {"README.md"} and "docs" not in options.reads.dirs
    # The option still names a push target and a reload allowlist on an install without a profile.
    options = Options.from_mapping({"notify": {"service": "mobile_app_x"},
                                    "capabilities": {"reloadable_domains": "plex"}})  # fmt: skip
    assert options.notify_service == "mobile_app_x" and options.reloadable == {"plex"}


def test_the_sample_profile_parses() -> None:
    assert SAMPLE_PROFILE.source == PROFILE_PATH and SAMPLE_PROFILE.git.enabled
    assert parse_text(SAMPLE_PROFILE_FILE.read_text()) == SAMPLE_PROFILE


def test_an_empty_profile_is_generic_but_loaded(paths: Paths) -> None:
    write_profile(paths.ha_config, "# nothing yet\n")
    profile, problems = load(paths.ha_config)
    assert problems == () and profile.source == PROFILE_PATH and not profile.git.enabled


async def test_a_generic_daemon_checks_health_without_git(h: Harness) -> None:  # noqa: F811
    d = h.daemon(profile=GENERIC)
    assert d.repo is None and d.changes is None
    outcome = await d.health_cycle("catch_up")
    assert outcome.result == "seeded"
    # No notify service anywhere: the push is dropped, never sent to a guessed device.
    assert h.pushes() == []
    view = d.profile_view()
    assert view == {"path": PROFILE_PATH, "loaded": False, "problems": [], "git": False, "overlays": [],
                    "push_target": False, "notify_problem": None}  # fmt: skip


async def test_without_git_proposals_and_overlays_are_refused(paths: Paths) -> None:
    world = World(paths, options=make_options(agent__mode="act", profile=GENERIC))
    try:
        world.run(allowed=frozenset({"change.propose", "facts.propose", "vacuum.rooms", "git.log", "health.read"}))
        refused = await world.call("propose-change", paths="alerts/x.yaml", subject="fix(alerts): x", why="y")
        assert refused["ok"] is False and "git proposals are off" in refused["error"]
        fact = await world.call("propose-fact", text="the fridge is at 5 C", why="asked")
        assert fact["ok"] is False and "git proposals are off" in fact["error"]
        rooms = await world.call("vacuum-rooms")
        assert rooms["ok"] is False and "house overlay vacuums is off" in rooms["error"]
        log = await world.call("git-log")
        assert log["ok"] is False and "git proposals are off" in log["error"]
    finally:
        world.store.close()


def test_the_prompt_offers_only_what_this_install_has() -> None:
    generic = capability_table(CHAT.capabilities, available=frozenset())
    assert "propose-change" not in generic and "vacuum-rooms" not in generic and "git-log" not in generic
    assert "integration-reload" in generic and "remind" in generic
    house = capability_table(CHAT.capabilities, available=SAMPLE_PROFILE.overlays | {"git"})
    assert "propose-change" in house and "vacuum-rooms" not in house
    with_overlay = capability_table(CHAT.capabilities, available=frozenset({"git", "vacuums"}))
    assert "vacuum-rooms" in with_overlay
    for name in ("change.propose", "facts.propose", "suppress.propose", "git.status"):
        assert unavailable(CAPABILITIES[name], frozenset()) is not None


PLANTED = (
    "secrets.yaml", "secrets.yml", "automations/secrets.yaml", ".storage/auth", ".storage/core.config_entries.yaml",
    ".cloud/x.yaml", "custom_components/x/y.py", "custom_components/x/services.yaml", "www/x.yaml", "deps/x.yaml",
    "tts/x.yaml", "backups/x.yaml", "home-assistant.log", "home-assistant.log.1", "home-assistant_v2.db",
    "home-assistant_v2.db-wal", "zigbee.db.yaml", "ops/profile.yaml", "esphome/garage.yaml", "image/x.yaml",
    "custom_templates/nested/x.jinja", "automations/notes.txt", "docs/other.md", "known_devices.yaml",
    # Any letter case.
    "packages/SECRETS.yml", "automations/Known_Devices.yaml", "IP_BANS.yaml",
)  # fmt: skip
EXPORTED = (
    "configuration.yaml", "scenes.yml", "automations/a.yaml", "automations/kitchen/b.yaml", "scripts/s.yaml",
    "packages/p.yml", "alerts/fridge.yaml", "templates/t.yaml", "blueprints/automation/x/bp.yaml",
    "custom_templates/m.jinja",
)  # fmt: skip
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJlMTIzNDU2"


def plant(root: Path) -> None:
    for rel in (*PLANTED, *EXPORTED):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"x: 1  # {rel}\n")
    (root / "secrets.yaml").write_text("wifi_password: hunter22secret\n")
    (root / "outside").mkdir()
    (root / "outside" / "evil.yaml").write_text("leak: OUTSIDE\n")


def exported(export: Path) -> list[str]:
    return sorted(str(p.relative_to(export)) for p in export.rglob("*") if p.is_file() or p.is_symlink())


def test_the_export_without_git_is_a_strict_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "config"
    plant(root)
    (root / "automations" / "link.yaml").symlink_to(root / "secrets.yaml")
    (root / "packages" / "outside").symlink_to(root / "outside")
    (root / "outside.yaml").symlink_to(root / "outside" / "evil.yaml")
    (root / "automations" / "huge.yaml").write_bytes(b"#" * (300 * 1024))
    (root / "automations" / "binary.yaml").write_bytes(b"\xff\xfe\x00")
    export = build_export(root, tmp_path / "export", git=False)
    assert exported(export) == sorted(EXPORTED)
    # With the house profile's documents: its named .md files, never whole directories.
    (root / "RUNBOOK.md").write_text("# Runbook\n")
    (root / "docs" / "facts.md").write_text("# Facts\n")
    export = build_export(root, tmp_path / "export", git=False, reads=make_options().reads)
    assert exported(export) == sorted([*EXPORTED, "RUNBOOK.md", "docs/facts.md"])


def test_a_secrets_file_in_another_letter_case_is_never_exported(tmp_path: Path) -> None:
    """Any letter case (a fresh tree: the developer's filesystem may fold case)."""
    root = tmp_path / "config"
    (root / "scripts").mkdir(parents=True)
    (root / "Secrets.yaml").write_text("wifi_password: hunter22secret\n")
    (root / "scripts" / "SECRETS.yml").write_text("token: hunter22secret\n")
    (root / "configuration.yaml").write_text("homeassistant:\n")
    assert exported(build_export(root, tmp_path / "export", git=False)) == ["configuration.yaml"]


def test_a_planted_secret_or_token_is_never_exported(tmp_path: Path) -> None:
    root = tmp_path / "config"
    plant(root)
    (root / "configuration.yaml").write_text(
        "http:\n  password: !secret wifi_password\nrest:\n  - headers:\n"
        "      Authorization: Bearer abcdefghijklmnop123\n"
        f"camera:\n  - url: rtsp://admin:pw123456@10.0.0.9/live\njwt: {TOKEN}\nleaked: hunter22secret\n"
        "anthropic: sk-ant-oat01-abcdefghijkl\n"
    )
    export = build_export(root, tmp_path / "export", git=False)
    text = "".join((export / name).read_text() for name in exported(export))
    assert not (export / "secrets.yaml").exists() and "secrets.yaml" not in exported(export)
    for secret in ("hunter22secret", TOKEN, "abcdefghijklmnop123", "pw123456", "sk-ant-oat01", "OUTSIDE"):
        assert secret not in text, secret
    assert "rtsp://***:***@10.0.0.9" in text and "camera:" in text  # redacted in place, the rest stays
    # The run's own tokens are redacted too (the runner passes them).
    (root / "scripts" / "s.yaml").write_text("note: oat-token-abcdef\n")
    from opsd.redact import build_redactor

    export = build_export(root, tmp_path / "export", git=False,
                          redactor=build_redactor(root / "secrets.yaml", ["oat-token-abcdef"]))  # fmt: skip
    assert "oat-token-abcdef" not in (export / "scripts" / "s.yaml").read_text()


def test_the_export_without_git_has_a_total_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opsd import privsep

    root = tmp_path / "config"
    (root / "automations").mkdir(parents=True)
    for i in range(5):
        (root / "automations" / f"a{i}.yaml").write_text("x" * 1000)
    monkeypatch.setattr(privsep, "MAX_DISK_TOTAL_BYTES", 2500)
    assert len(exported(build_export(root, tmp_path / "export", git=False))) == 2


# ------------------------------------------------------------------ strict schema


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("colour: blue\n", "unknown key 'colour'"),
        ("health:\n  power: binary_sensor.x\n", "health: unknown key 'power'"),
        (
            "updates:\n  fragile:\n    - name: x\n      tier: approve\n      reason: r\n      domains: [x]\n"
            "      extra: 1\n",
            "updates.fragile[0]: unknown key 'extra'",
        ),
        ("git:\n  enabled: yes please\n", "git.enabled must be true or false"),
        ("git:\n  enabled: true\n", "git: author_name is required"),
        ("notify_service: notify.mobile_app_x\n", "notify_service must be mobile_app_<device>"),
        ("overlays: [vacuums, plugins]\n", "overlays[1] must be one of"),
        ("overlays: [vacuums, vacuums]\n", "overlays lists an entry twice"),
        ("overlays: [suppress]\n", "overlays: suppress is not installed in this build"),
        ("updates:\n  version_gates:\n    - integration: x\n      core_from: soon\n      reason: r\n", "core_from"),
        (
            "updates:\n  version_gates:\n    - integration: x\n      core_from: '2026.12'\n      reason: r\n"
            "      patch: p\n",
            "needs the localpatches overlay",
        ),
        ("house_notes: " + "x" * 4001 + "\n", "house_notes must be text of at most 4000"),
        ("notify_service: !secret phone\n", "explicit YAML tags"),
        ("a: &x 1\nb: *x\n", "anchors"),
        ("- just\n- a list\n", "the profile must be a mapping"),
    ],
)
def test_the_schema_is_strict(text: str, error: str) -> None:
    with pytest.raises(ProfileError) as found:
        parse_text(text)
    assert error in str(found.value)


def test_a_refused_profile_fails_the_agent_closed(paths: Paths, h: Harness) -> None:  # noqa: F811
    write_profile(paths.ha_config, "notify_service: mobile_app_x\nreload:\n  domains: [lock]\n")
    options = Options.load(paths)
    assert not options.valid and options.profile == GENERIC
    [problem] = options.problems
    assert problem.startswith(f"house profile {PROFILE_PATH}: reload.domains: lock is never reloaded")
    # Act mode and automatic updates stay off however the add-on options are set (fail closed).
    acting = Options.from_mapping({"agent": {"mode": "act"}, "updates": {"auto_install": "on"}},
                                  profile=options.profile, profile_problems=options.problems)  # fmt: skip
    assert not acting.acting and not acting.auto_updates_on
    d = h.daemon()
    d.options = acting
    assert d.profile_view()["problems"] == [problem] and d.options_notice() is not None


def test_a_symlinked_profile_is_refused(paths: Paths, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text("notify_service: mobile_app_x\n")
    (paths.ha_config / "ops").mkdir()
    os.symlink(target, paths.ha_config / PROFILE_PATH)
    profile, problems = load(paths.ha_config)
    assert profile == GENERIC and problems and "cannot be read" in problems[0]


# ------------------------------------------------------------------ hostile profiles cannot weaken invariants


@pytest.mark.parametrize(
    "domain", ["lock", "alarm_control_panel", "cover", "nuki", "envisalink", "ratgdo", "esphome",
               "bluetooth", "hassio", "homeassistant"],
)  # fmt: skip
def test_a_profile_listing_a_never_reload_domain_is_refused(domain: str) -> None:
    with pytest.raises(ProfileError, match=f"reload.domains: {domain} is never reloaded"):
        parse_text(f"reload:\n  domains: [plex, {domain}]\n")


N3_DOMAINS = ("schlage", "switchbot", "aladdin_connect", "abode", "verisure", "alarmdotcom", "overkiz", "nest", "ring",
              "simplisafe", "august", "yale_smart_alarm", "nuki", "tedee", "ratgdo", "myq", "gogogate2", "risco",
              "totalconnect", "elkm1", "envisalink", "dhcp", "ssdp", "bluetooth_adapters")  # fmt: skip


@pytest.mark.parametrize("domain", N3_DOMAINS)
def test_lock_alarm_and_garage_vendors_are_never_reloaded(domain: str) -> None:
    """Whatever the option or a profile says."""
    assert "never-reload" in (reload_mod.static_refusal(domain, {domain}) or "")
    with pytest.raises(ProfileError, match="is never reloaded"):
        parse_text(f"reload:\n  domains: [{domain}]\n")


def test_the_never_reload_list_is_sorted_by_group() -> None:
    import inspect
    import re

    from opsd import policy

    source = inspect.getsource(policy)
    block = source[source.index("NEVER_RELOAD_DOMAINS: frozenset") : source.index(")  # fmt: skip", source.index(
        "NEVER_RELOAD_DOMAINS: frozenset"))]  # fmt: skip
    groups = [re.findall(r'"([a-z0-9_]+)"', g) for g in re.split(r"\n\s*# [^\n]*", block)[1:]]
    assert len(groups) == 5 and all(g == sorted(g) for g in groups)
    assert sum(len(g) for g in groups) == len(policy.NEVER_RELOAD_DOMAINS)


def test_reload_deny_only_adds() -> None:
    profile = parse_text("reload:\n  domains: [plex]\n  deny: [acme_solar]\n")
    assert reload_mod.static_refusal("acme_solar", {"acme_solar"}, (), profile.reload.deny) is not None
    # Nothing in a profile can take a domain off the list in code, even with the option allowlisting it.
    options = make_options(capabilities__reloadable_domains="lock,nuki,plex", profile=profile)
    for domain in ("lock", "nuki"):
        assert "never-reload" in (reload_mod.static_refusal(domain, options.reloadable, (), profile.reload.deny) or "")
    with pytest.raises(ProfileError, match=r"reload.domains: plex is never reloaded"):
        parse_text("reload:\n  domains: [plex]\n  deny: [plex]\n")


def test_a_fragile_piece_can_only_hold_back() -> None:
    with pytest.raises(ProfileError, match="tier must be one of approve, pinned, never"):
        parse_text("updates:\n  fragile:\n    - name: x\n      tier: auto\n      reason: r\n      domains: [x]\n")
    # A piece that matches Core or the OS changes nothing about them: those rules come first.
    core = UpdateInfo("update.home_assistant_core_update", "Core", "2026.10.3", "2026.10.4", "hassio", "core")
    os_update = UpdateInfo("update.home_assistant_operating_system_update", "OS", "17.2", "17.3", "hassio", "os")
    wide = parse_text("updates:\n  fragile:\n    - name: all\n      tier: never\n      reason: r\n"
                      "      domains: [home_assistant]\n")  # fmt: skip
    assert classify(os_update, fragile=wide.updates.fragile).tier is Tier.APPROVE
    assert classify(core, release_age_days=30, fragile=wide.updates.fragile).tier is Tier.AUTO
    # The agent itself stays manual whatever a profile says.
    me = UpdateInfo("update.ha_ops_agent_update", "HA Ops Agent", "0.1.0", "0.1.1", "hassio", "addon",
                    supported_features=int(UpdateFeature.SPECIFIC_VERSION))  # fmt: skip
    assert classify(me, addon_slug="abcd1234_ha_ops_agent", fragile=wide.updates.fragile).tier is Tier.MANUAL


@pytest.mark.parametrize("slug", ["core_ssh", "local_ha_ops_agent_2", "../x", "self", ""])
def test_self_slug_must_name_the_agent(slug: str) -> None:
    with pytest.raises(ProfileError, match="self_slug"):
        parse_text(f"self_slug: '{slug}'\n")


@pytest.mark.parametrize("slug", ["local_ha_ops_agent", "abcd1234_ha_ops_agent", "self", "SELF"])
def test_the_self_guard_protects_every_slug_of_the_agent(slug: str) -> None:
    """Whatever `self_slug` a profile names, no install of the agent can update or be updated as another add-on."""
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_http("POST", f"/store/addons/{slug.lower()}/update", {"backup": False})
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_update("update.x_update", "addon", slug=slug)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("readable", "['.storage']"), ("readable", "['secrets.yaml']"), ("readable", "[ops]"),
        ("readable", "['ops/profile.yaml']"), ("readable", "[addons]"), ("readable", "['home-assistant.log']"),
        ("readable", "['../etc']"), ("runbook", "secrets.yaml"), ("runbook", "'.storage/auth.md'"),
        ("facts", "RUNBOOK.md"), ("facts", "'docs/../ops/profile.md'"), ("facts", "configuration.yaml"),
    ],
)  # fmt: skip
def test_documents_cannot_reach_secrets_or_the_agent(key: str, value: str) -> None:
    with pytest.raises(ProfileError, match=f"docs.{key}"):
        parse_text(f"docs:\n  {key}: {value}\n")


@pytest.mark.parametrize("kinds", ["[configuration]", "[custom_components]", "[ops]", "['alerts/*.yaml']"])
def test_proposal_kinds_are_the_three_the_config_check_knows(kinds: str) -> None:
    with pytest.raises(ProfileError, match=r"git.proposals"):
        parse_text(f"git:\n  enabled: true\n  author_name: a\n  author_email: a@b.cd\n  proposals: {kinds}\n")


def test_proposal_rules_never_reach_the_agent_or_config() -> None:
    profile = parse_text("git:\n  enabled: true\n  author_name: a\n  author_email: a@b.cd\ndocs:\n"
                         "  facts: docs/facts.md\n")  # fmt: skip
    rules = profile.proposal_rules
    assert allowed_path("docs/facts.md", rules) and allowed_path("alerts/x.yaml", rules)
    for path in (
        "ops/profile.yaml",
        "configuration.yaml",
        "RUNBOOK.md",
        "docs/other.md",
        "secrets.yaml",
        "custom_components/x/__init__.py",
        "addons/ha-ops-agent/config.yaml",
    ):
        assert not allowed_path(path, rules), path
    # Git off: nothing at all, whatever the documents say.
    assert not parse_text("docs:\n  facts: docs/facts.md\n").proposal_rules.on


def test_git_author_cannot_inject_options() -> None:
    for name in ("'a <b@c.de>'", '"x\\ny"'):
        with pytest.raises(ProfileError, match=r"git.author_name"):
            parse_text(f"git:\n  enabled: true\n  author_name: {name}\n  author_email: a@b.cd\n")
    with pytest.raises(ProfileError, match=r"git.author_email"):
        parse_text("git:\n  enabled: true\n  author_name: a\n  author_email: '--upload-pack=x'\n")


def test_the_house_profile_only_adds_need_approval() -> None:
    options: Any = make_options(updates__need_approval=["update.a_update"])
    assert options.need_approval >= set(SAMPLE_PROFILE.updates.need_approval) | {"update.a_update"}


# ------------------------------------------------------------------ the partial-backup issue is generic


NO_BACKUP_KEY = "supervisor:no_current_backup:system:"


def backup_issue_items(age_h: float | None, encrypted: bool | None = True) -> list[Any]:
    snapshot = Snapshot(taken_at="t", resolution={"issues": [NO_CURRENT_BACKUP]},
                        backups={"newest_complete_age_h": age_h, "newest_complete_encrypted": encrypted})  # fmt: skip
    return [i for i in diff(snapshot, Baseline(created_at="t"), datetime.now(UTC)) if i.key == NO_BACKUP_KEY]


def test_no_current_backup_is_quiet_while_a_complete_encrypted_backup_is_recent() -> None:
    assert backup_issue_items(5.0) == []


@pytest.mark.parametrize(
    ("age_h", "encrypted", "evidence"),
    [(30.0, True, "more than 26 h"), (None, None, "no complete backup found"), (5.0, False, "not encrypted")],
)
def test_without_that_backup_the_issue_is_critical(age_h: float | None, encrypted: bool | None, evidence: str) -> None:
    [item] = backup_issue_items(age_h, encrypted)
    assert item.severity == "critical" and evidence in item.summary and "check failed" in item.summary


async def test_a_generic_agent_does_not_raise_no_current_backup(h: Harness) -> None:  # noqa: F811
    """No profile, no overlay: HA's "partial" encrypted backups do not raise the Supervisor's issue."""
    h.fake.backups[0]["date"] = iso(NOW - timedelta(hours=3))  # a complete encrypted backup 3 h ago
    d = h.daemon(profile=GENERIC)
    assert not d.options.profile.overlays
    await d.health_cycle("catch_up")
    h.fake.resolution = {"issues": [{"type": "no_current_backup", "context": "system"}], "unhealthy": [],
                         "unsupported": []}  # fmt: skip
    await d.health_cycle("daily")
    assert NO_BACKUP_KEY not in {i.key for i in d.open_items()} and h.claude_calls == []
    # The evidence goes away: the issue is reported, critical.
    h.fake.backups[0]["date"] = iso(NOW - timedelta(days=3))
    await d.health_cycle("daily")
    [item] = [i for i in d.open_items() if i.key == NO_BACKUP_KEY]
    assert item.severity == "critical" and "check failed" in item.summary


def test_the_published_example_profile_is_valid() -> None:
    example = parse_text((Path(__file__).resolve().parents[1] / "examples" / "profile.yaml").read_text())
    assert example.notify_service == "mobile_app_my_phone" and not example.git.enabled and not example.overlays
    assert {f.name for f in example.updates.fragile} == {"my_custom_hvac", "sprinkler_hub"}
    assert example.reload.domains == ("cast", "hue", "plex") and example.docs.facts == "docs/facts.md"
