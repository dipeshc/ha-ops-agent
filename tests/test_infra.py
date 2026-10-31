"""Config, lock, git, websocket framing, panel and static add-on files."""

import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from opsd import ws
from opsd.config import Options, Paths, claude_token
from opsd.gitops import GitBusyError, GitError, Repo
from opsd.jsonstore import append_jsonl, read_json, read_jsonl, write_json_atomic
from opsd.lock import LockBusyError, run_lock
from tests.conftest import ADDON_ROOT, OPT


def test_options_parsing_and_token(paths: Paths) -> None:
    write_json_atomic(paths.options_file, {"agent": {"mode": "act"}, "notify": {"max_pushes_per_day": 2},
                                           "updates": {"need_approval": ["update.a"]},
                                           "claude": {"token": "sk-ant-oat-xyz"}, "unknown": 1})  # fmt: skip
    opts = Options.load(paths)
    assert opts.acting and opts.notify.max_pushes_per_day == 2 and opts.updates.need_approval == ("update.a",)
    assert "sk-ant" not in repr(opts)
    assert claude_token(opts, paths) == "sk-ant-oat-xyz"
    bad = Options.from_mapping({"notify": {"max_pushes_per_day": "3"}})
    assert bad.problems == ("notify.max_pushes_per_day must be a whole number",)
    paths.secrets_dir.mkdir(parents=True)
    paths.oauth_token_file.write_text("from-file\n")
    assert claude_token(Options(), paths) == "from-file"


def test_jsonstore(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b.json"
    write_json_atomic(target, {"x": 1})
    assert read_json(target, None) == {"x": 1} and target.stat().st_mode & 0o777 == 0o600
    assert read_json(tmp_path / "missing", "d") == "d"
    append_jsonl(tmp_path / "h.jsonl", {"a": 1})
    (tmp_path / "h.jsonl").open("a").write("garbage\n")
    append_jsonl(tmp_path / "h.jsonl", {"a": 2})
    assert [r["a"] for r in read_jsonl(tmp_path / "h.jsonl")] == [1, 2]


def test_run_lock(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with run_lock(lock), pytest.raises(LockBusyError), run_lock(lock):
        pass
    with run_lock(lock):
        pass


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git(work, "checkout", "-q", "-b", "main")
    (work / "notes.md").write_text("# Notes\n")
    git(work, "add", ".")
    git(work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(work, "push", "-q", "origin", "main")
    return remote, work


def test_git_commit_and_push(repos: tuple[Path, Path], tmp_path: Path) -> None:
    remote, work = repos
    repo = Repo(work, tmp_path / "key", tmp_path / "kh", author=("Alex Example", "alex@example.com"))
    (work / "notes.md").write_text("# Notes\n\n## new\n")
    with pytest.raises(GitError, match="conventional"):
        repo.commit_paths(["notes.md"], "Updated stuff")
    (work / "other.yaml").write_text("x: 1\n")
    with pytest.raises(GitBusyError):
        repo.commit_paths(["notes.md"], "docs(notes): record ops agent triage 2026-01-31")
    (work / "other.yaml").unlink()
    repo.commit_paths(["notes.md"], "docs(notes): record ops agent triage 2026-01-31")
    message = git(work, "log", "-1", "--format=%an|%B")
    assert message.startswith("Alex Example|docs(notes)") and "Co-Authored" not in message
    repo.push()
    assert git(remote, "log", "-1", "--format=%s").strip().startswith("docs(notes)")


def test_git_push_refuses_when_remote_ahead(repos: tuple[Path, Path], tmp_path: Path) -> None:
    remote, work = repos
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "x.txt").write_text("x")
    git(other, "add", ".")
    git(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "remote work")
    git(other, "push", "-q", "origin", "main")
    with pytest.raises(GitError, match="ahead"):
        Repo(work, tmp_path / "key", tmp_path / "kh").push()


def test_ws_accept_key_and_frames() -> None:
    assert ws.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    async def roundtrip(payload: bytes) -> tuple[bool, int, bytes]:
        reader = asyncio.StreamReader()
        reader.feed_data(ws.encode_frame(ws.OP_TEXT, payload, mask=b"\x01\x02\x03\x04"))
        reader.feed_eof()
        return await ws.read_frame(reader)

    for size in (5, 300, 70_000):
        data = b"x" * size
        assert asyncio.run(roundtrip(data)) == (True, ws.OP_TEXT, data)


def test_static_files_are_valid() -> None:
    managed = json.loads((ADDON_ROOT / "rootfs/etc/claude-code/managed-settings.json").read_text())
    assert managed["permissions"]["defaultMode"] == "dontAsk"
    assert managed["allowManagedHooksOnly"] and managed["allowManagedPermissionRulesOnly"]
    # dontAsk refuses everything the allow list does not name.
    assert "deny" not in managed["permissions"]
    # Write only for proposal drafts (the guard narrows it to allowlisted config paths).
    # Claude Code matches file-writing tools against Edit(path) rules; a Write(path) rule never
    # matches, so dontAsk would deny every draft (verified against the CLI).
    assert managed["permissions"]["allow"] == [
        "Read(//run/ops-claude/config/**)",
        "Bash(opsctl *)",
        "Edit(//run/ops-drafts/**)",
    ]
    assert "onFailure" not in json.dumps(managed)
    assert managed["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/opt/ops/bin/guard-hook"
    json.loads((OPT / "prompts/triage-result.schema.json").read_text())
    config = (ADDON_ROOT / "config.yaml").read_text()
    for needle in (
        "hassio_role: manager",
        "init: false",
        "token: password?",
        "panel_admin: true",
        "path: /mnt/ha/config",
        "path: /mnt/ha/backup",
        "backup_pre: /opt/ops/bin/ops-backup-pre",
    ):
        assert needle in config
    for script in ("rootfs/opt/ops/bin/opsctl", "rootfs/opt/ops/bin/guard-hook", "rootfs/opt/ops/bin/ops-backup-pre",
                   "rootfs/etc/s6-overlay/s6-rc.d/opsd/run",
                   "rootfs/etc/s6-overlay/scripts/init-ops.sh"):  # fmt: skip
        assert (ADDON_ROOT / script).stat().st_mode & 0o111, script


def _glob_re(glob: str) -> re.Pattern[str]:
    """A Claude Code permission path glob (gitignore style) as a regex: `**` spans slashes, `*` does not."""
    out = ""
    i = 0
    while i < len(glob):
        if glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out)


def test_only_the_export_and_the_drafts_are_allowed() -> None:
    """dontAsk refuses whatever no allow rule names: the sensitive places match none of them."""
    managed = json.loads((ADDON_ROOT / "rootfs/etc/claude-code/managed-settings.json").read_text())
    allowed = [_glob_re(m.group(1)) for r in managed["permissions"]["allow"]
               if (m := re.fullmatch(r"(?:Read|Edit)\(/(/.*)\)", r))]  # fmt: skip
    export = "/run/ops-claude/config"
    assert any(p.fullmatch(f"{export}/automations/kitchen/fridge.yaml") for p in allowed)
    for path in ("/run/ops/state.json", "/run/ops-sock/opsd.sock", "/run/s6/container_environment/SUPERVISOR_TOKEN",
                 "/run/s6-rc/x", "/run/service/opsd/run", "/data/options.json", "/mnt/ha/config/secrets.yaml",
                 "/etc/claude-code/managed-settings.json", "/proc/1/environ"):  # fmt: skip
        assert not any(p.fullmatch(path) for p in allowed), path


def test_release_age_uses_the_hardened_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The health check's GitHub lookup goes through evidence.fetch_text (host allowlist)."""
    from opsd import api, evidence

    asked: list[str] = []

    def fetch(url: str, **_: object) -> str:
        asked.append(url)
        return json.dumps({"published_at": "2026-01-20T00:00:00Z"})

    monkeypatch.setattr(evidence, "fetch_text", fetch)
    age = api.github_release_age_days("home-assistant/core", "2026.10.4")
    assert asked == ["https://api.github.com/repos/home-assistant/core/releases/tags/2026.10.4"]
    assert age is not None and age > 9
    assert api.github_release_age_days("../x", "1") is None and len(asked) == 1
    monkeypatch.setattr(evidence, "fetch_text", lambda url, **_: "not json")
    assert api.github_release_age_days("a/b", "1") is None


def test_the_hardened_fetch_refuses_any_other_host() -> None:
    """The check `fetch_text` (and every redirect it follows) runs first; tests never reach the network."""
    from opsd.evidence import FetchError, _check_url

    with pytest.raises(FetchError, match="not an allowed source"):
        _check_url("https://evil.example.com/x")
    with pytest.raises(FetchError, match="not an allowed source"):
        _check_url("http://api.github.com/repos/a/b/releases/tags/1")
    _check_url("https://api.github.com/repos/a/b/releases/tags/1")
