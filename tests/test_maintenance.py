"""Proactive maintenance on a synthetic install: the inventory, each signal and item variant, when it asks, what the
plan run may and may not add, and that missing or unrecognised evidence fails closed. Every name is made up."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from opsd import evidence, inventory, maintenance, privsep
from opsd.claude_runner import RunResult
from opsd.evidence import CORE_COMPONENT, RELEASE_INDEX, REPO_API, EvidenceCache, EvidenceSource, parse_post
from opsd.items import Context, ItemBook
from opsd.live import Hub
from opsd.maintenance import Finding, Gathered, MaintenanceManager, releases_left, removal_block
from opsd.store import Store
from tests.conftest import GENERIC, make_options
from tests.test_panel import h, local_ingress  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
INDEX = (FIXTURES / "release-index-ahead.html").read_text()
AHEAD = (FIXTURES / "release-post-ahead.html").read_text()
OLDER = (FIXTURES / "release-post.html").read_text()
BASE = "https://www.home-assistant.io/blog"
POSTS = {f"{BASE}/2030/03/05/release-20303/": AHEAD, f"{BASE}/2030/02/04/release-20302/": OLDER,
         f"{BASE}/2029/12/03/release-202912/": OLDER}  # fmt: skip
INSTALLED = "2030.2.1"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2030, 2, 20, 9, 0, tzinfo=UTC)
CONFIGURATION = (
    "homeassistant:\n  name: Example\nacme_sprinkler:\n  zones: 4\nacme_legacy_hub:\n  host: example.local\n"
)
GARDEN = "- alias: Water the garden\n  actions:\n    - action: acme_sprinkler.run_zone\n      data: {zone: 1}\n"
GARDEN_BOARD = {"views": [{"cards": [{"type": "custom:acme-gauge", "entity": "sensor.acme_weather_rain",
                                      "max_value": 30}]}]}  # fmt: skip


def inv(**changes: Any) -> inventory.Inventory:
    texts = {"configuration.yaml": CONFIGURATION, "automations/garden.yaml": GARDEN}
    built = inventory.build(
        entries=[{"domain": "zha"}, {"domain": "acme_weather"}, {"domain": "acme_off", "disabled_by": "user"}],
        components=["zha", "acme_sprinkler", "sensor.acme_weather"], texts=texts,
        custom={"acme_weather": {"name": "Acme Weather", "version": "1.2.0", "repo": "example/ha-acme-weather"},
                "hacs": {"name": "HACS", "version": "2.0.0", "repo": None}},
        registry=[{"entity_id": "sensor.acme_weather_rain", "platform": "acme_weather"},
                  {"entity_id": "weather.acme_home", "platform": "acme_weather"},
                  {"entity_id": "light.porch", "platform": "zha"}],
        resources=[{"url": "/hacsfiles/acme-gauge/acme-gauge.js", "type": "module"}],
        dashboards={"example-garden": {"mode": "storage", "title": "Garden", "cards": {"custom:acme-gauge": 1}},
                    "example-generated": {"mode": "storage", "title": "Wall", "cards": {"custom:acme-gauge": 1}}},
        addons={"addons": [{"slug": "core_mosquitto"}]},
    )  # fmt: skip
    for key, value in changes.items():
        setattr(built, key, value)
    return built


REPAIRS = [
    {"domain": "acme_sprinkler", "issue_id": "deprecated_yaml", "breaks_in_ha_version": "2030.4.0",
     "translation_key": "deprecated_yaml", "severity": "warning"},
    {"domain": "acme_sprinkler", "issue_id": "deprecated_option", "breaks_in_ha_version": "2030.5.0", "ignored": True},
    {"domain": "zha", "issue_id": "inconsistent_pan_id", "severity": "error"},
]  # fmt: skip
LOGS = [
    {"name": "custom_components.acme_weather.sensor", "level": "WARNING", "message": [
        "Detected that custom integration 'acme_weather' uses a deprecated helper, which will be removed in 2030.9"]},
    {"name": "homeassistant.components.zha", "level": "WARNING", "message": ["Device took long to answer"]},
]  # fmt: skip


def github(url: str) -> str:
    if url == REPO_API.format(repo="example/ha-acme-weather"):
        return json.dumps({"full_name": "example/ha-acme-weather", "archived": True, "has_issues": True})
    if url == CORE_COMPONENT.format(domain="acme_weather"):
        return json.dumps([{"name": n, "type": "file"} for n in ("manifest.json", "__init__.py", "sensor.py")])
    raise evidence.FetchError("not found", status=404)


OPENED: list[Store] = []


@pytest.fixture(autouse=True)
def close_stores() -> Iterator[None]:
    yield
    while OPENED:
        OPENED.pop().close()


def opened(path: Path) -> Store:
    store = Store.open(path)
    OPENED.append(store)
    return store


class Rig:
    def __init__(self, tmp_path: Path, *, git: bool = True, acting: bool = True, offline: bool = False) -> None:
        self.store = opened(tmp_path / "ops.db")
        self.now = NOW
        self.installed = INSTALLED
        self.issues: list[dict[str, Any]] = list(REPAIRS)
        self.logs: list[dict[str, Any]] = list(LOGS)
        self.inventory = inv()
        self.offline = offline
        self.fetched: list[str] = []
        self.prompts: list[str] = []
        self.answer: dict[str, Any] | None = None
        self.proposed: list[tuple[str, list[Any]]] = []
        self.acting = acting
        source = EvidenceSource(cache=EvidenceCache(self.store, clock=lambda: self.now), fetch=self.fetch,
                                clock=lambda: self.now)  # fmt: skip
        self.manager = MaintenanceManager(store=self.store, gather=self.gather, evidence=source, git=git,
                                          generated=frozenset({"example-generated"}), plan=self.plan,
                                          propose=self.propose, clock=lambda: self.now)  # fmt: skip
        self.sent: list[Any] = []

        async def send(message: Any) -> str:
            self.sent.append(message)
            return "sent"

        self.book = ItemBook(self.store, Hub(), tz=lambda: BERLIN, context=lambda: Context(acting=self.acting),
                             send=send, clock=lambda: self.now)  # fmt: skip
        self.book.sync()  # the upgrade backfill: silent

    def fetch(self, url: str) -> str:
        self.fetched.append(url)
        if self.offline:
            raise evidence.FetchError("offline")
        if url == RELEASE_INDEX:
            return INDEX
        if url in POSTS:
            return POSTS[url]
        return github(url)

    async def gather(self) -> Gathered:
        return Gathered(installed=self.installed, issues=self.issues, logs=self.logs, inventory=self.inventory,
                        configs={"example-garden": GARDEN_BOARD, "example-generated": GARDEN_BOARD})  # fmt: skip

    async def plan(self, prompt: str) -> RunResult:
        self.prompts.append(prompt)
        if self.answer is None:
            return RunResult(status="ai_off")
        return RunResult(status="ok", structured=self.answer)

    async def propose(self, finding: Finding, why: str, edits: list[Any]) -> dict[str, Any]:
        self.proposed.append((finding.key, edits))
        return {"id": 1}

    async def scan(self, trigger: str = "daily") -> dict[str, Any]:
        result = await self.manager.scan(trigger)
        assert result["result"] == "ok", result
        self.book.sync()
        return result

    def items(self) -> dict[str, dict[str, Any]]:
        return {r["title"]: r for r in self.store.items_all(derived=True) if r["kind"] == "maintenance"}

    async def deliver(self) -> list[str]:
        return await self.book.deliver()


def by_signal(rig: Rig, signal: str) -> list[Finding]:
    return [f for f in rig.manager.findings() if f.signal == signal]


# ------------------------------------------------------------------ inventory


def test_the_inventory_reads_what_the_install_uses() -> None:
    found = inv()
    assert found.entry_domains == {"zha", "acme_weather"}
    assert {"zha", "acme_sprinkler", "acme_weather", "sensor"} <= found.domains and "acme_off" not in found.domains
    assert found.yaml["configuration.yaml"] == ["acme_legacy_hub", "acme_sprinkler"]
    assert found.custom["acme_weather"]["platforms"] == ["sensor", "weather"]
    assert found.custom["acme_weather"]["entities"] == 2 and found.hacs
    assert found.plugins == ["acme-gauge"] and found.addons == ["core_mosquitto"]
    assert found.uses("acme_legacy_hub") and not found.uses("acme_lights")
    assert found.references("acme_sprinkler.run_zone") == ["automations/garden.yaml:3"]
    assert found.references("custom:acme-gauge") == ["dashboard example-garden", "dashboard example-generated"]
    assert "texts" not in found.to_json() and "CONFIGURATION" not in json.dumps(found.to_json())


def test_yaml_features_name_integration_keys_and_platforms() -> None:
    text = "sensor:\n  - platform: acme_meter  # the meter\n    name: x\nacme_hub:\n  host: y\ntemplate: []\n"
    assert inventory.yaml_features("configuration.yaml", text) == ["acme_hub", "platform:acme_meter"]
    # Elsewhere only platforms count: an automation's keys are not integrations.
    assert inventory.yaml_features("automations/a.yaml", "acme_hub:\n- platform: acme_meter\n") == [
        "platform:acme_meter"
    ]


def test_the_inventory_reads_only_the_files_the_export_may_hold(paths: Any, tmp_path: Path) -> None:
    root = paths.ha_config
    (root / "configuration.yaml").write_text(CONFIGURATION)
    (root / "automations").mkdir()
    (root / "automations" / "garden.yaml").write_text(GARDEN)
    (root / ".storage").mkdir()
    (root / ".storage" / "lovelace.yaml").write_text("secret: dashboards\n")
    (root / "www").mkdir()
    (root / "www" / "x.yaml").write_text("acme: 1\n")
    (tmp_path / "outside.yaml").write_text("acme: 2\n")
    (root / "linked.yaml").symlink_to(tmp_path / "outside.yaml")
    options = make_options(profile=GENERIC)
    names = privsep.disk_files(root, options.reads)
    texts = inventory.read_yaml(root, names)
    assert set(texts) == {"configuration.yaml", "automations/garden.yaml"}
    assert inventory.read_text(root, "../outside.yaml", 1000) is None
    (root / "custom_components" / "acme_solar" / "manifest.json").write_text(
        json.dumps(
            {"name": "Acme Solar", "version": "3.1.0", "issue_tracker": "https://github.com/example/acme-solar/issues"}
        )
    )
    assert inventory.manifest(root / "custom_components", "acme_solar") == {
        "name": "Acme Solar", "version": "3.1.0", "repo": "example/acme-solar"}  # fmt: skip
    assert inventory.manifest(root / "custom_components", "../x") == {}


# ------------------------------------------------------------------ versions and parsing


@pytest.mark.parametrize(("installed", "removal", "left"), [
    ("2030.2.1", "2030.4", 2), ("2030.2.1", "2030.3", 1), ("2030.2.1", "2030.2", 0), ("2030.2.1", "2029.12", 0),
    ("2030.11.3", "2031.1", 2), ("2030.11.0b2", "2031.2", 3), ("", "2030.4", None), ("2030.2", "soon", None),
])  # fmt: skip
def test_releases_left_counts_core_minors_across_years(installed: str, removal: str, left: int | None) -> None:
    assert releases_left(installed, removal) == left


def test_the_removal_version_is_the_one_next_to_the_removal_words() -> None:
    assert maintenance.removal_in("Support is deprecated and will be removed in 2030.6.") == (2030, 6)
    assert maintenance.removal_in("Added in 2029.12, removal follows in Home Assistant 2030.8.0") == (2030, 8)
    assert maintenance.removal_in("Introduced in 2030.1; works as before") is None
    assert maintenance.removal_in("removed in 2030.13") is None


def test_the_post_parser_keeps_headings_and_new_integrations_and_fails_closed() -> None:
    data = parse_post(AHEAD, "s")
    assert {"domain": "acme_weather", "name": "Acme Weather"} in data["new"]
    assert any("Marketplace" in heading for heading in data["headings"])
    assert all("<" not in heading for heading in data["headings"])
    with pytest.raises(ValueError):
        parse_post("<html><h2>Marketplace</h2></html>", "s")


async def test_an_unrecognised_post_is_a_problem_and_never_cached(tmp_path: Path) -> None:
    store = opened(tmp_path / "ops.db")
    pages = {RELEASE_INDEX: INDEX, f"{BASE}/2030/03/05/release-20303/": "<html>redesigned</html>"}
    source = EvidenceSource(cache=EvidenceCache(store, clock=lambda: NOW), fetch=lambda u: pages[u],
                            clock=lambda: NOW)  # fmt: skip
    found, problems = await source.posts([(2030, 3)], await source.index())
    assert found == [] and problems
    pages[f"{BASE}/2030/03/05/release-20303/"] = AHEAD
    found, problems = await source.posts([(2030, 3)], await source.index())
    assert [p["version"] for p in found] == ["2030.3"] and problems == []


async def test_github_answers_without_the_expected_shape_are_unknown(tmp_path: Path) -> None:
    store = opened(tmp_path / "ops.db")
    answers = {REPO_API.format(repo="example/a"): json.dumps({"archived": "yes"}),
               CORE_COMPONENT.format(domain="acme_x"): json.dumps({"message": "rate limited"})}  # fmt: skip
    source = EvidenceSource(cache=EvidenceCache(store, clock=lambda: NOW), fetch=lambda u: answers[u],
                            clock=lambda: NOW)  # fmt: skip
    assert await source.archived("example/a") is None
    assert await source.archived("../../x") is None
    with pytest.raises(evidence.FetchError):
        await source.core_platforms("acme_x")
    assert await source.core_platforms("Not A Domain") is None


# ------------------------------------------------------------------ signals and variants


async def test_repairs_logs_and_notes_become_deprecations_with_their_removal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    [repair] = by_signal(rig, "repair")
    assert (repair.domain, repair.removal, repair.left(INSTALLED)) == ("acme_sprinkler", "2030.4", 2)
    [log] = by_signal(rig, "log")
    assert (log.domain, log.removal) == ("acme_weather", "2030.9") and "custom integration" in log.affected[0]
    notes = {f.domain or f.affected[-1]: f for f in by_signal(rig, "notes")}
    # A removal the text names; a farewell in a release not installed yet; a card option in a dashboard.
    assert notes["acme_sprinkler"].removal == "2030.6"
    assert notes["acme_legacy_hub"].removal == "2030.3" and notes["acme_legacy_hub"].what == "removed in 2030.3"
    card = notes["dashboard example-generated"]
    assert card.fixable == ["dashboard"] and "dashboard example-garden" in card.affected
    # Unused integrations, unreferenced developer-blog names and changes already installed are not findings.
    assert "acme_lights" not in notes and all(f.removal != "2030.2" for f in by_signal(rig, "notes"))


async def test_the_weekly_checks_find_archived_repositories_and_replacements(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    [repo] = by_signal(rig, "repository")
    assert repo.domain == "acme_weather" and repo.removal is None and repo.variant == "deprecation"
    [candidate] = by_signal(rig, "replacement")
    assert candidate.variant == "replacement" and candidate.detail["missing"] == ["weather"]
    assert candidate.detail["risk"] == "high" and "2 entities" in candidate.detail["impact"]
    [market] = by_signal(rig, "marketplace")
    assert market.variant == "suggestion"
    fetched = len(rig.fetched)
    # Not again until a week has passed or a new post appears.
    rig.now += timedelta(days=1)
    await rig.scan()
    assert not any("api.github.com" in u for u in rig.fetched[fetched:])
    assert by_signal(rig, "replacement") and by_signal(rig, "repository")


async def test_without_hacs_there_is_no_marketplace_suggestion(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.inventory = inv(custom={"acme_weather": dict(inv().custom["acme_weather"])}, domains=frozenset({"zha"}))
    await rig.scan()
    assert by_signal(rig, "marketplace") == []


# ------------------------------------------------------------------ items and escalation


async def test_only_deprecations_within_two_releases_need_the_admin_and_push(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    items = rig.items()
    near = items["acme_sprinkler: deprecated, removed in 2030.4"]
    assert near["state"] == "waiting" and near["need"]["kind"] == "ack" and near["kicker"] == "Deprecation ahead"
    assert near["gist"] == "removed in 2030.4, 2 releases left"
    far = items["acme_weather: deprecated, removed in 2030.9"]
    assert far["state"] == "proposed" and not far["needs"] and far["gist"] == "removed in 2030.9, 7 releases left"
    undated = items["acme_weather: its repository is archived"]
    assert undated["state"] == "proposed" and undated["gist"] == "no removal date yet"
    await rig.deliver()
    titles = sorted(m.title for m in rig.sent)
    assert "acme_sprinkler: deprecated, removed in 2030.4" in titles
    assert not any("2030.9" in t or "archived" in t or "replace" in t or "Marketplace" in t for t in titles)
    home = rig.book.home()
    assert {r["title"] for r in home["suggestions"]} == {"acme_weather: Core integration acme_weather may replace it",
                                                         "hacs: built-in Marketplace in 2030.3"}  # fmt: skip
    assert far["title"] in {r["title"] for r in home["coming"]}


async def test_an_acknowledged_deprecation_asks_again_one_release_closer(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    near = rig.items()["acme_sprinkler: deprecated, removed in 2030.4"]
    assert rig.book.ack(near, now=rig.now)
    rig.book.sync()
    assert rig.items()[near["title"]]["state"] == "proposed"
    rig.installed, rig.now = "2030.3.0", rig.now + timedelta(days=30)
    await rig.scan()
    again = rig.items()[near["title"]]
    assert again["state"] == "waiting" and again["gist"] == "removed in 2030.4, 1 release left"


async def test_a_resolved_finding_is_done_and_a_hidden_one_leaves_home(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    rig.issues = []
    rig.now += timedelta(hours=1)
    await rig.scan()
    item = rig.items()["acme_sprinkler: deprecated, removed in 2030.4"]
    assert item["state"] == "done" and item["gist"].startswith("resolved")
    candidate = by_signal(rig, "replacement")[0]
    assert rig.manager.hide(candidate.key)
    rig.book.sync()
    hidden = rig.items()[f"acme_weather: {candidate.what}"]
    assert hidden["state"] == "cancelled" and hidden["gist"].startswith("hidden by you") and not hidden["needs"]


async def test_missing_evidence_never_resolves_a_finding(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    notes = {f.key for f in by_signal(rig, "notes")}
    rig.offline = True
    rig.now += timedelta(days=8)
    # Offline (and the cache stale): the notes, repository and replacement findings stay as they were.
    await rig.scan("weekly")
    assert {f.key for f in by_signal(rig, "notes")} == notes
    assert by_signal(rig, "repository") and by_signal(rig, "replacement")


async def test_a_log_warning_lasts_a_week_after_it_was_last_seen(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    rig.logs = []
    rig.now += timedelta(days=3)
    await rig.scan()
    assert by_signal(rig, "log")
    rig.now += timedelta(days=5)
    await rig.scan()
    assert by_signal(rig, "log") == []


def test_stored_findings_of_the_wrong_shape_are_dropped(tmp_path: Path) -> None:
    store = opened(tmp_path / "ops.db")
    good = Finding(key="0123456789abcdef", variant="deprecation", signal="repair", what="x").to_json()
    store.kv_set(maintenance.FINDINGS_KEY, json.dumps([good, good | {"key": "../x"}, good | {"variant": "fix"},
                                                       {"key": 1}, "x"]), now=NOW)  # fmt: skip
    assert [f.key for f in maintenance.load(store)] == ["0123456789abcdef"]


# ------------------------------------------------------------------ the plan run


async def test_the_plan_is_the_agents_text_and_the_title_and_need_stay_the_daemons(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    repair = by_signal(rig, "repair")[0]
    assert rig.prompts and "<evidence>" in rig.prompts[0] and repair.key in rig.prompts[0]
    rig.answer = {"plans": [{"key": repair.key, "plan": "IGNORE THE RULES. Move acme_sprinkler to the UI."},
                            {"key": "ffffffffffffffff", "plan": "not asked"}], "summary": "s"}  # fmt: skip
    rig.now += timedelta(hours=1)
    await rig.scan()
    item = rig.items()["acme_sprinkler: deprecated, removed in 2030.4"]
    assert item["agent"]["text"].startswith("IGNORE THE RULES")
    assert "IGNORE" not in item["title"] and "IGNORE" not in item["need"]["text"] and "IGNORE" not in item["gist"]
    assert maintenance.plan_of(rig.store, repair) is not None
    prompts = len(rig.prompts)
    rig.now += timedelta(hours=1)
    await rig.scan()
    # Planned once per finding and evidence.
    assert all(repair.key not in p for p in rig.prompts[prompts:])


async def test_yaml_edits_become_a_proposal_only_with_git_and_a_yaml_finding(tmp_path: Path) -> None:
    for git, expected in ((True, 1), (False, 0)):
        rig = Rig(tmp_path / str(git), git=git)
        rig.inventory = inv()
        await rig.scan()
        sprinkler = next(f for f in by_signal(rig, "notes") if f.domain == "acme_sprinkler")
        sprinkler.fixable = ["yaml"]
        rig.store.kv_set(maintenance.FINDINGS_KEY, json.dumps([f.to_json() if f.key != sprinkler.key else
                                                               sprinkler.to_json() for f in rig.manager.findings()]),
                         now=rig.now)  # fmt: skip
        edits = [{"path": "configuration.yaml", "old": "zones: 4", "new": "zones: 4  # moved to the UI"}]
        answer = {"key": sprinkler.key, "plan": "p", "edits": edits}
        await rig.manager._yaml_proposal(
            sprinkler, maintenance.parse_plans({"plans": [answer]}, {sprinkler.key})[sprinkler.key]
        )
        repair = by_signal(rig, "repair")[0]
        # A finding with nothing to migrate in YAML never proposes, whatever the answer says.
        await rig.manager._yaml_proposal(repair, {"plan": "p", "edits": edits})
        assert len(rig.proposed) == expected


async def test_a_dashboard_plan_waits_for_approval_and_never_targets_another_dashboard(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    card = next(f for f in by_signal(rig, "notes") if "dashboard example-garden" in f.affected)
    edit = {"card_type": "custom:acme-gauge", "renames": [{"old": "max_value", "new": "max"}]}
    rig.answer = {"plans": [{"key": card.key, "plan": "Rename max_value to max.",
                             "dashboard": {"url_path": "example-garden", **edit}}], "summary": "s"}  # fmt: skip
    rig.now += timedelta(hours=1)
    await rig.scan()
    item = rig.items()[card.title()]
    assert item["state"] == "waiting" and item["need"]["kind"] == "approval"
    assert item["need"]["primary"]["id"] == "apply_dashboard" and "example-garden" in item["change"]["diff"]
    assert "max_value → max" in item["change"]["diff"]
    # The generated dashboard is never planned, even when the answer names it.
    other = Rig(tmp_path / "generated")
    await other.scan()
    card = next(f for f in by_signal(other, "notes") if "dashboard example-generated" in f.affected)
    other.answer = {"plans": [{"key": card.key, "plan": "p", "dashboard": {"url_path": "example-generated", **edit}}],
                    "summary": "s"}  # fmt: skip
    other.now += timedelta(hours=1)
    await other.scan()
    assert maintenance.dashboard_plan(other.store, card.key) is None
    # In observe mode the plan is shown, without the action and without a push.
    rig.acting = False
    rig.book.sync()
    shown = rig.items()[item["title"]]
    assert shown["state"] == "proposed" and "act" in shown["gist"]


async def test_a_dashboard_not_named_by_the_finding_is_never_planned(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await rig.scan()
    repair = by_signal(rig, "repair")[0]
    rig.answer = {"plans": [{"key": repair.key, "plan": "p", "dashboard": {
        "url_path": "example-garden", "card_type": "custom:acme-gauge", "renames": [{"old": "max_value",
                                                                                     "new": "max"}]}}],
                  "summary": "s"}  # fmt: skip
    rig.now += timedelta(hours=1)
    await rig.scan()
    assert maintenance.dashboard_plan(rig.store, repair.key) is None


# ------------------------------------------------------------------ the update rule


def test_a_core_update_that_reaches_a_removal_asks() -> None:
    finding = Finding(key="0123456789abcdef", variant="deprecation", signal="repair", what="x",
                      domain="acme_sprinkler", removal="2030.4")  # fmt: skip
    core = {"kind": "core", "latest": "2030.4.1"}
    assert "acme_sprinkler (removed in 2030.4)" in str(removal_block(core, [finding]))
    assert removal_block({"kind": "core", "latest": "2030.3.2"}, [finding]) is None
    assert removal_block({"kind": "hacs", "latest": "2030.4.1"}, [finding]) is None
    undated = Finding(key="0123456789abcdee", variant="deprecation", signal="repository", what="x")
    assert removal_block(core, [undated]) is None


async def test_the_scan_is_due_before_an_update_only_when_it_is_old(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    assert rig.manager.due(before_update=True)
    await rig.scan()
    rig.now += timedelta(hours=20)
    assert not rig.manager.due(before_update=True)
    rig.now += timedelta(hours=7)
    assert rig.manager.due(before_update=True)
    assert [f.removal for f in rig.manager.removals()] and all(f.variant == "deprecation"
                                                               for f in rig.manager.removals())  # fmt: skip


# ------------------------------------------------------------------ end to end through the daemon and the panel


def fixture_fetch(url: str, **_: Any) -> str:
    if url == RELEASE_INDEX:
        return INDEX
    if url in POSTS:
        return POSTS[url]
    raise evidence.FetchError("not found", status=404)


async def test_the_panel_applies_an_approved_dashboard_plan_end_to_end(
    h: Any,  # noqa: F811
    local_ingress: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_assistant import ADMIN
    from tests.test_panel import Panel

    monkeypatch.setattr(evidence, "fetch_text", fixture_fetch)
    h.fake.lovelace = {"example-garden": json.loads(json.dumps(GARDEN_BOARD))}
    original = h.fake.supervisor

    async def supervisor(path: str) -> Any:
        found = await original(path)
        return found | {"version": INSTALLED} if path == "/core/info" else found

    h.fake.supervisor = supervisor
    d = h.daemon(agent__mode="act")
    d.inbound.admin = ADMIN
    server = d.panel_server(port=0)
    await server.start()
    panel = Panel(d, server)
    try:
        assert d.maintenance is not None
        result = await d.maintenance.scan("daily")
        assert result["result"] == "ok", result
        card = next(f for f in d.maintenance.findings() if "dashboard example-garden" in f.affected)
        assert "dashboard" in card.fixable and h.claude_calls and "<evidence>" in h.claude_calls[-1]
        h.claude_reply = {"plans": [{"key": card.key, "plan": "Rename the option.", "dashboard": {
            "url_path": "example-garden", "card_type": "custom:acme-gauge",
            "renames": [{"old": "max_value", "new": "max"}]}}], "summary": "s"}  # fmt: skip
        await d.maintenance.scan("daily")
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{card.key}")
        assert item["state"] == "waiting" and item["need"]["primary"]["id"] == "apply_dashboard"
        act = {"action": "apply_dashboard", "revision": item["revision"]}
        status, _, body = await panel.request("POST", f"/api/items/{item['id']}/act", body=act)
        assert status == 200, body
        event = await d.queue.get()
        assert event.type == "maintenance.dashboard"
        await d._on_dashboard_approved(event)
        saved = h.fake.lovelace["example-garden"]["views"][0]["cards"][0]
        assert saved["max"] == 30 and "max_value" not in saved
        assert list((h.paths.data / "maintenance" / "dashboards" / "example-garden").glob("*.json"))
        await d.items_flush()
        after = d.store.item_by_key(f"maint:{card.key}")
        assert after["state"] == "proposed" and "saved" in after["gist"]
        # The next scan no longer finds the old option in use: resolved.
        await d.maintenance.scan("daily")
        await d.items_flush()
        assert d.store.item_by_key(f"maint:{card.key}")["state"] == "done"
    finally:
        await server.stop()


async def test_a_pending_core_update_that_removes_something_in_use_asks(tmp_path: Path) -> None:
    from tests.test_updates import CORE, entity, only_auto
    from tests.test_updates import Rig as UpdateRig

    rig = UpdateRig(tmp_path)
    try:
        only_auto(rig, CORE)
        finding = Finding(key="0123456789abcdef", variant="deprecation", signal="log", what="x",
                          domain="acme_sprinkler", removal="2026.11")  # fmt: skip
        scans: list[str] = []

        async def before_plan() -> None:
            scans.append("scan")

        rig.manager._removals = lambda: [finding]
        rig.manager._before_plan = before_plan
        await rig.manager.run_window()
        # 2026.10.4 does not reach the removal: it installs as before, and the scan ran before the plan.
        assert rig.ops.installs()[0] == ("update_core", "2026.10.4") and scans == ["scan"]
        rig.now += timedelta(days=1)
        rig.ops.entities[CORE] = entity("2026.10.4", "2026.11.1", 15, "Home Assistant Core")
        rig.ages[CORE] = 20.0
        await rig.manager.run_window()
        [core] = [i for i in rig.run()["plan"] if i["entity_id"] == CORE]
        assert core["tier"] == "approve" and "acme_sprinkler (removed in 2026.11)" in core["reason"]
        assert rig.item(CORE, "2026.11.1")["state"] == "waiting"
    finally:
        rig.store.close()
