"""Privilege separation logic: permission checks, isolation and the config export."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from opsd import privsep
from opsd.claude_runner import RunConfig, run_claude
from opsd.config import Paths
from opsd.privsep import Account, build_export, isolation_problems, reachable_by
from opsd.redact import Redactor
from tests.conftest import OPT

STRANGER = Account(uid=987654, gid=987654, groups=frozenset({987654}))


def test_reachable_by_follows_permission_bits(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o755)
    public = tmp_path / "public.txt"
    public.write_text("x")
    os.chmod(public, 0o644)
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    os.chmod(secret, 0o600)
    assert reachable_by(public, STRANGER, trusted_root=tmp_path)
    assert not reachable_by(secret, STRANGER, trusted_root=tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    inner = vault / "readable.txt"
    inner.write_text("x")
    os.chmod(inner, 0o644)
    os.chmod(vault, 0o700)  # like /mnt/ha: world-readable files below a root-only dir
    try:
        assert not reachable_by(inner, STRANGER, trusted_root=tmp_path)
        link = tmp_path / "link.txt"
        link.symlink_to(inner)
        assert not reachable_by(link, STRANGER, trusted_root=tmp_path)
    finally:
        os.chmod(vault, 0o755)
    assert not reachable_by(tmp_path / "missing", STRANGER)
    assert reachable_by(secret, Account(0, 0, frozenset()))


def test_isolation_problems_reports_exposed_paths(paths: Paths, tmp_path: Path) -> None:
    for parent in (tmp_path, paths.data, paths.ha_config):
        os.chmod(parent, 0o755)
    paths.options_file.write_text("{}")
    os.chmod(paths.options_file, 0o644)
    os.chmod(paths.secrets_yaml, 0o600)
    problems = isolation_problems(paths, STRANGER, trusted_root=tmp_path)
    assert str(paths.options_file) in problems and str(paths.ha_config) in problems
    assert str(paths.secrets_yaml) not in problems
    os.chmod(paths.options_file, 0o600)
    os.chmod(paths.ha_config, 0o700)
    try:
        problems = isolation_problems(paths, STRANGER, trusted_root=tmp_path)
        assert str(paths.options_file) not in problems and str(paths.ha_config) not in problems
    finally:
        os.chmod(paths.ha_config, 0o755)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)  # fmt: skip


def test_build_export_copies_only_readable_tracked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "automations").mkdir(parents=True)
    (repo / ".storage").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "configuration.yaml").write_text("homeassistant:\n")
    (repo / "secrets.yaml").write_text("k: TOPSECRET\n")
    (repo / ".storage" / "auth").write_text("STORAGESECRET")
    (repo / "automations" / "a.yaml").write_text("- id: a\n")
    (repo / "automations" / "link.yaml").symlink_to(repo / "secrets.yaml")
    (repo / "untracked.yaml").write_text("x: 1\n")
    git(repo, "add", "configuration.yaml", "secrets.yaml", ".storage/auth", "automations")
    git(repo, "commit", "-qm", "init")
    (repo / "automations" / "a.yaml").write_text("- id: live-edit\n")
    export = build_export(repo, tmp_path / "export")
    files = sorted(str(p.relative_to(export)) for p in export.rglob("*") if p.is_file() or p.is_symlink())
    assert files == ["automations/a.yaml", "configuration.yaml"]
    assert (export / "automations" / "a.yaml").read_text() == "- id: live-edit\n"  # working tree, not HEAD
    assert stat.S_IMODE((export / "configuration.yaml").stat().st_mode) == 0o644
    assert stat.S_IMODE((export / "automations").stat().st_mode) == 0o755
    (repo / "automations" / "a.yaml").unlink()
    git(repo, "rm", "-q", "--cached", "automations/a.yaml")
    export = build_export(repo, tmp_path / "export")
    assert not (export / "automations" / "a.yaml").exists()
    assert [p.name for p in (tmp_path / "export").iterdir()] == ["config"]


def test_ops_account_lookup() -> None:
    assert privsep.ops_account("no-such-user-xyz") is None
    me = privsep.ops_account(os.environ.get("USER", "root"))
    assert me is None or me.uid >= 0


async def test_runner_passes_user_and_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    captured: dict[str, object] = {}

    async def fake_exec(*argv: str, **kwargs: object) -> None:
        captured.update(kwargs)
        raise OSError("stop here")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(os, "chown", lambda *a: None)
    cfg = RunConfig(model="m", max_turns=1, budget_usd=0.1, system_prompt_file=OPT / "prompts/system-append.md",
                    result_schema_file=OPT / "prompts/triage-result.schema.json", config_dir=tmp_path / "c",
                    home=tmp_path / "c/home", cwd=tmp_path, run_as=(1001, 1002))  # fmt: skip
    result = await run_claude(cfg, "p", token="t", transcript_path=tmp_path / "t.gz", redactor=Redactor())
    assert result.status == "error"
    assert captured["user"] == 1001 and captured["group"] == 1002 and captured["extra_groups"] == []
    env = captured["env"]
    assert isinstance(env, dict) and "SUPERVISOR_TOKEN" not in env


def test_export_skips_files_behind_symlinked_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "automations").mkdir(parents=True)
    (repo / "alerts").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "automations" / "a.yaml").write_text("- id: a\n")
    (repo / "alerts" / "b.yaml").write_text("x: 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    # After the commit, swap tracked directories for symlinks into secrets.
    (repo / ".storage").mkdir()
    (repo / ".storage" / "a.yaml").write_text("STORAGESECRET")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.yaml").write_text("OUTSIDESECRET")
    for name, target in (("automations", repo / ".storage"), ("alerts", outside)):
        for f in (repo / name).iterdir():
            f.unlink()
        (repo / name).rmdir()
        (repo / name).symlink_to(target)
    export = build_export(repo, tmp_path / "export")
    contents = [p.read_text() for p in export.rglob("*") if p.is_file()]
    assert not any("SECRET" in c for c in contents)


def test_directory_with_execute_only_counts_as_reachable(tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o755)
    d = tmp_path / "xonly"
    d.mkdir()
    os.chmod(d, 0o711)
    try:
        assert reachable_by(d, STRANGER, trusted_root=tmp_path)
        os.chmod(d, 0o700)
        assert not reachable_by(d, STRANGER, trusted_root=tmp_path)
    finally:
        os.chmod(d, 0o755)
