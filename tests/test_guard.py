import io
import json
from pathlib import Path, PurePosixPath

import pytest

from opsd import guard
from opsd.opsctl import build_parser
from tests.conftest import SAMPLE_PROFILE


def bash(cmd: object) -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def denied(event: object) -> bool:
    """A reason or a GuardInputError both end as a deny (main() fails closed)."""
    try:
        return guard.decide(event) is not None
    except guard.GuardInputError:
        return True


@pytest.mark.parametrize(
    "command",
    [
        "opsctl status",
        "opsctl logs core --lines 300 --grep 'acme_solar via_device'",
        'opsctl entities --unavailable --platform "acme_solar"',
        "opsctl git-log -n 5 --path automations",
        "opsctl git-diff HEAD~1..HEAD --path alerts/smoke.yaml",
        "opsctl git-show HEAD:configuration.yaml",
        "/opt/ops/bin/opsctl help",
    ],
)
def test_allowed_commands(command: str) -> None:
    assert guard.decide(bash(command)) is None


@pytest.mark.parametrize(
    "command",
    [
        # The reported bypass and its relatives: raw git is gone entirely.
        "git -C /homeassistant diff --no-index .. alerts 'da?a/options.json'",
        "git -C /homeassistant diff --no-ind .. alerts",
        "git -C /homeassistant log --output=/homeassistant/x",
        "git -C /homeassistant -c core.pager=sh log",
        "git -C /homeassistant diff --ext-diff HEAD",
        "git -C /homeassistant show --textconv HEAD:secrets.yaml",
        "git -C /homeassistant show 1a2b3c4d",
        "git -C /homeassistant log -- ':(glob)**/secrets*'",
        "git -C /homeassistant status",
        "/usr/bin/git -C /homeassistant log",
        "cat /data/options.json",
        "opsctl status && cat /homeassistant/secrets.yaml",
        "opsctl status; env",
        "opsctl logs core | tee /tmp/x",
        "opsctl $(echo status)",
        "opsctl `id`",
        'opsctl logs core --grep "$SUPERVISOR_TOKEN"',
        'opsctl logs core --grep "!!"',
        "opsctl core-check",
        "opsctl restart-core",
        "/bin/rm -rf /homeassistant",
        "sh -c 'opsctl status'",
        "python3 -c 'print(1)'",
        "SUPERVISOR_TOKEN=x opsctl status",
        "opsctl status > /homeassistant/notes.md",
        "opsctl status\ncat /etc/passwd",
        "opsctl status\x00cat",
        "grep -r token /data",
        "ls /run/ops",
        "opsctl logs core --grep ~/x",
        "opsctl git-show HEAD:secrets*",
        "opsctl 'unterminated",
        "opsctl " + "x" * 3000,
        "",
    ],
)
def test_denied_commands(command: str) -> None:
    assert denied(bash(command))


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        ("/run/ops-claude/config/configuration.yaml", True),
        ("/run/ops-claude/config/automations/lights.yaml", True),
        ("/run/ops-claude/config/custom_components/acme_solar/light.py", True),
        # Only with a house profile that names it (test_house_documents_are_readable).
        ("/run/ops-claude/config/RUNBOOK.md", False),
        ("/run/ops-claude/config/templates/status.yaml", True),
        ("/run/ops-claude/config/custom_templates/attention.jinja", True),
        ("automations/lights.yaml", True),
        ("/run/ops-claude/config", False),
        ("/mnt/ha/config/configuration.yaml", False),
        ("/homeassistant/configuration.yaml", False),
        ("/run/ops-claude/config/secrets.yaml", False),
        ("/run/ops-claude/config/automations/secrets.yaml", False),
        ("/run/ops-claude/config/.storage/core.config_entries", False),
        ("/run/ops-claude/config/home-assistant_v2.db", False),
        ("/run/ops-claude/config/automations/../secrets.yaml", False),
        ("/run/ops-claude/config/custom_components/x/../../.storage/auth", False),
        ("../data/options.json", False),
        ("/data/options.json", False),
        ("/run/ops/supervisor_token", False),
        ("/proc/self/environ", False),
        ("/run/ops-claude/config/.git/config", False),
        ("/run/ops-claude/config/ip_bans.yaml", False),
        ("/run/ops-claude/config/automations/x.yaml\x00", False),
        ("", False),
    ],
)
def test_read_paths(path: str, ok: bool) -> None:
    assert denied({"tool_name": "Read", "tool_input": {"file_path": path}}) is not ok


def test_symlink_out_of_allowed_tree_is_refused(tmp_path: Path) -> None:
    ha = tmp_path / "ha"
    (ha / "automations").mkdir(parents=True)
    (ha / "secrets.yaml").write_text("x: y\n")
    (ha / "automations" / "link.yaml").symlink_to(ha / "secrets.yaml")
    (ha / "automations" / "ok.yaml").write_text("a: 1\n")
    root = PurePosixPath(ha)
    assert guard.readable(str(ha / "automations" / "ok.yaml"), root)
    assert not guard.readable(str(ha / "automations" / "link.yaml"), root)


def test_grep_needs_safe_path() -> None:
    def grep(**kw: object) -> str | None:
        return guard.decide({"tool_name": "Grep", "tool_input": {"pattern": "x", **kw}})

    assert grep() is not None
    assert grep(path="/run/ops-claude/config") is not None
    assert grep(path="/run/ops-claude/config/automations") is None
    assert grep(path="/run/ops-claude/config/.storage") is not None
    assert grep(path="/data") is not None
    with pytest.raises(guard.GuardInputError):
        grep(path="/run/ops-claude/config/automations", glob=["*"])


def test_glob_and_other_tools() -> None:
    assert guard.decide({"tool_name": "Glob", "tool_input": {"pattern": "**/*.yaml"}}) is None
    assert (
        guard.decide({"tool_name": "Glob", "tool_input": {"pattern": "*", "path": "/run/ops-claude/config/alerts"}})
        is None
    )
    assert guard.decide({"tool_name": "Glob", "tool_input": {"pattern": "../data/*"}}) is not None
    assert guard.decide({"tool_name": "Glob", "tool_input": {"pattern": "/data/*"}}) is not None
    assert guard.decide({"tool_name": "Glob", "tool_input": {"pattern": "*", "path": "/data"}}) is not None
    for tool in ("Edit", "WebFetch", "WebSearch", "Agent", "NotebookEdit", "mcp__x__y"):
        assert guard.decide({"tool_name": tool, "tool_input": {}}) is not None
    # Write is only for proposal drafts (see test_changes for the path rules).
    assert guard.decide({"tool_name": "Write", "tool_input": {"file_path": "/data/x", "content": ""}}) is not None


@pytest.mark.parametrize(
    "event",
    [
        [],
        {"tool_name": 5, "tool_input": {}},
        {"tool_name": "Bash", "tool_input": "opsctl status"},
        {"tool_name": "Bash", "tool_input": {"command": ["opsctl", "status"]}},
        {"tool_name": "Read", "tool_input": {"file_path": None}},
        {"tool_name": "Read", "tool_input": {"file_path": "/run/ops-claude/config/a\x00.yaml"}},
    ],
)
def test_malformed_input_fails_closed(event: object) -> None:
    assert denied(event)


def run_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: str) -> tuple[int, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = guard.main()
    return code, capsys.readouterr().out


def test_hook_output_format(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = run_main(monkeypatch, capsys, json.dumps(bash("cat /etc/shadow")))
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
    code, out = run_main(monkeypatch, capsys, json.dumps(bash("opsctl status")))
    assert code == 0 and out == ""


@pytest.mark.parametrize("payload", ["not json", "[]", '{"tool_name": "Bash", "tool_input": 3}', "\x00"])
def test_hook_fails_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: str) -> None:
    code, out = run_main(monkeypatch, capsys, payload)
    assert code == 2 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_fails_closed_on_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(_: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "decide", boom)
    code, _ = run_main(monkeypatch, capsys, json.dumps(bash("opsctl status")))
    assert code == 2


def test_hook_verbs_match_opsctl() -> None:
    parser = build_parser()
    sub = next(a for a in parser._actions if a.dest == "verb")
    verbs = set(sub.choices)  # type: ignore[arg-type]
    assert verbs | {"--help", "-h"} == guard.OPSCTL_VERBS
    assert "core-check" not in verbs


def test_house_documents_are_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook reads the house profile's documents from the run's environment (`OPS_READ_EXTRA`)."""
    assert not guard.readable("/run/ops-claude/config/docs/facts.md")
    monkeypatch.setattr(guard, "READS", guard.Reads.with_extra(",".join(SAMPLE_PROFILE.docs.read_extra).split(",")))
    for path in ("RUNBOOK.md", "docs/facts.md", "docs/local-patches.yaml"):
        assert not denied({"tool_name": "Read", "tool_input": {"file_path": f"/run/ops-claude/config/{path}"}})
    assert guard.searchable("/run/ops-claude/config/docs")
    assert not guard.readable("/run/ops-claude/config/docs/../secrets.yaml")
    assert not guard.readable("/run/ops-claude/config/ops/profile.yaml")


@pytest.mark.parametrize(
    "entry",
    [
        ".storage",
        ".storage/auth",
        "secrets.yaml",
        "docs/secrets.yaml",
        "ops",
        "ops/profile.yaml",
        "addons",
        "data",
        "ssl",
        "backup",
        "../etc",
        "/etc/passwd",
        "home-assistant.log",
        "home-assistant_v2.db",
        "docs/../secrets.yaml",
        "a/b/c/d/e.md",
        "github_deploy.md",
        "ha-ops-agent.md",
        "x.yaml",
        "",
    ],
)
def test_a_profile_cannot_make_secrets_or_the_agent_readable(entry: str) -> None:
    """A hostile profile (or environment) entry is dropped: nothing outside the base lists becomes readable."""
    assert not guard.valid_read_entry(entry)
    assert guard.Reads.with_extra([entry]) == guard.Reads()


@pytest.mark.parametrize(
    "command",
    [
        "opsctl ack --key 'entities:acme_solar' --days 1 --reason 'user notified'",
        "opsctl ack --key update:update.home_assistant_core_update@2026.10.4 --days 3",
        "opsctl notify --title 'Acme Solar down' --message 'All lights unavailable' --urgent",
        "opsctl notify --title x --message '- Acme Solar failed\\n- rollback recommended'",
        "opsctl help ack",
        "opsctl remind --at +2h --title 'Replace smoke alarm batteries' --reply",
        "opsctl remind --kind followup --at 2026-01-27T09:00 --title 'Re-check fridge'",
        "opsctl tasks",
        "opsctl cancel --id 42",
    ],
)
def test_effect_commands_pass_the_guard(command: str) -> None:
    assert guard.decide(bash(command)) is None


@pytest.mark.parametrize(
    "command",
    [
        'opsctl notify --title x --message "- a\\n- b"',
        "opsctl notify --title x --message $(cat /etc/passwd)",
        "opsctl notify --title x --message '- a\n- b'",
    ],
)
def test_effect_commands_cannot_smuggle_shell(command: str) -> None:
    assert denied(bash(command))


@pytest.mark.parametrize(
    "path",
    ["Secrets.yaml", "SECRETS.yml", "secrets.YML", "automations/Secrets.yaml", "packages/IP_Bans.yaml",
     "Known_Devices.yaml", "blueprints/secrets.Yaml"],
)  # fmt: skip
def test_deny_files_in_any_letter_case(path: str) -> None:
    """The deny list is compared lower-cased."""
    assert not guard.readable_rel(PurePosixPath(path))
    assert denied({"tool_name": "Read", "tool_input": {"file_path": f"/run/ops-claude/config/{path}"}})


@pytest.mark.parametrize("entry", ["secrets", "Secrets", "SECRETS", "secrets/notes.md", "Secrets/x.md"])
def test_a_secrets_directory_is_never_a_house_document(entry: str) -> None:
    """The guard hook refuses what the deny list names, however it is spelled."""
    assert not guard.valid_read_entry(entry)
    assert guard.Reads.with_extra([entry]) == guard.Reads()
    assert not guard.readable_rel(PurePosixPath(entry if "/" in entry else f"{entry}/x.md"))
