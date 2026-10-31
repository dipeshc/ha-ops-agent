"""opsctl git verbs: fixed argv, strict revisions, readable paths only."""

import subprocess
from pathlib import Path

import pytest

from opsd import githistory
from opsd.githistory import HistoryError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo where secrets.yaml and .storage were (wrongly) committed once."""
    ha = tmp_path / "ha"
    (ha / "automations").mkdir(parents=True)
    (ha / ".storage").mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ha)], check=True)
    (ha / "configuration.yaml").write_text("homeassistant:\n")
    (ha / "secrets.yaml").write_text("api_key: TOPSECRET123\n")
    (ha / ".storage" / "auth").write_text('{"token": "STORAGESECRET"}')
    (ha / "automations" / "lights.yaml").write_text("- id: one\n")
    git(ha, "add", "-A")
    git(ha, "commit", "-q", "-m", "first")
    (ha / "automations" / "lights.yaml").write_text("- id: two\n")
    (ha / "secrets.yaml").write_text("api_key: TOPSECRET456\n")
    git(ha, "commit", "-q", "-am", "second")
    # A hostile repo config must not run anything.
    git(ha, "config", "core.pager", "touch PWNED_PAGER")
    git(ha, "config", "diff.external", "touch PWNED_DIFF")
    git(ha, "config", "core.fsmonitor", "touch PWNED_FSMONITOR")
    (ha / ".gitattributes").write_text("*.yaml diff=evil\n")
    git(ha, "config", "diff.evil.textconv", "touch PWNED_TEXTCONV")
    return ha


def no_pwn(repo: Path) -> None:
    assert not list(repo.glob("PWNED*")), list(repo.glob("PWNED*"))
    assert not list(Path.cwd().glob("PWNED*"))


def test_log_and_status(repo: Path) -> None:
    assert "second" in githistory.log(repo, 5, None)
    assert "second" in githistory.log(repo, 5, "automations")
    assert githistory.status(repo) == ""
    no_pwn(repo)


def test_diff_withholds_secret_paths(repo: Path) -> None:
    out = githistory.diff(repo, "HEAD~1..HEAD", None)
    assert "id: two" in out and "TOPSECRET" not in out and "withheld" in out
    no_pwn(repo)


def test_show_commit_and_file(repo: Path) -> None:
    out = githistory.show(repo, "HEAD~1")
    assert "first" in out and "id: one" in out
    assert "TOPSECRET" not in out and "STORAGESECRET" not in out
    assert githistory.show(repo, "HEAD:configuration.yaml") == "homeassistant:\n"
    no_pwn(repo)


def test_show_refuses_blob_and_secret_paths(repo: Path) -> None:
    blob = git(repo, "rev-parse", "HEAD:secrets.yaml").strip()
    tree = git(repo, "rev-parse", "HEAD^{tree}").strip()
    for spec in (
        blob,
        blob[:12],
        tree,
        "HEAD:secrets.yaml",
        "HEAD~1:secrets.yaml",
        "HEAD:.storage/auth",
        "HEAD:automations/../secrets.yaml",
        "HEAD:./secrets.yaml",
        f"{blob}:x",
        "HEAD:automations/*",
    ):
        with pytest.raises(HistoryError):
            githistory.show(repo, spec)


@pytest.mark.parametrize(
    "rev",
    [
        "--no-index",
        "--no-ind",
        "--output=/tmp/x",
        "-c",
        "--ext-diff",
        "--textconv",
        "HEAD@{1}",
        "HEAD...HEAD~1",
        "HEAD..",
        "..HEAD",
        "main",
        "HEAD~1..HEAD..HEAD",
        ":/first",
        "HEAD:secrets.yaml",
        "-1",
        "HEAD~99999",
    ],
)
def test_diff_rejects_option_like_and_odd_revisions(repo: Path, rev: str) -> None:
    with pytest.raises(HistoryError):
        githistory.diff(repo, rev, None)


@pytest.mark.parametrize(
    "path",
    [
        "secrets.yaml",
        ".storage",
        ".storage/auth",
        "..",
        "../ha",
        "automations/../secrets.yaml",
        ":(glob)**/secrets*",
        "*",
        "automations/*",
        "--output=x",
        "-p",
        "/etc/passwd",
        "automations//lights.yaml",
        ".",
    ],
)
def test_paths_are_validated(repo: Path, path: str) -> None:
    with pytest.raises(HistoryError):
        githistory.log(repo, 5, path)
    with pytest.raises(HistoryError):
        githistory.diff(repo, "HEAD~1", path)


def test_literal_pathspec_does_not_glob(repo: Path) -> None:
    # A file literally named like a pathspec magic word cannot widen the match.
    assert githistory.diff(repo, "HEAD~1", "automations/lights.yaml").count("diff --git") == 1


def test_log_count_is_clamped(repo: Path) -> None:
    assert len(githistory.log(repo, 10_000, None).splitlines()) == 2


def test_hostile_filter_driver_never_runs(tmp_path: Path) -> None:
    from opsd.gitops import Repo
    from opsd.privsep import build_export

    ha = tmp_path / "ha"
    ha.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(ha)], check=True)
    (ha / "configuration.yaml").write_text("a: 1\n")
    (ha / "notes.md").write_text("# Notes\n")
    git(ha, "add", ".")
    git(ha, "commit", "-q", "-m", "init")
    (ha / ".gitattributes").write_text("* filter=evil\n")
    (ha / ".git" / "info").mkdir(exist_ok=True)
    (ha / ".git" / "info" / "attributes").write_text("*.md filter=evil2\n")
    for name in ("evil", "evil2"):
        git(ha, "config", f"filter.{name}.clean", f"touch {tmp_path}/PWNED_CLEAN_{name}; cat")
        git(ha, "config", f"filter.{name}.smudge", f"touch {tmp_path}/PWNED_SMUDGE_{name}; cat")
        git(ha, "config", f"filter.{name}.process", f"touch {tmp_path}/PWNED_PROCESS_{name}")
    (ha / "configuration.yaml").write_text("a: 2\n")
    (ha / "notes.md").write_text("# Notes\n\n## x\n")
    githistory.status(ha)
    githistory.diff(ha, "HEAD", None)
    build_export(ha, tmp_path / "export")
    (ha / "configuration.yaml").write_text("a: 1\n")
    (ha / ".gitattributes").unlink()
    Repo(ha, tmp_path / "k", tmp_path / "kh").commit_paths(["notes.md"], "docs(notes): record test")
    assert not list(tmp_path.glob("PWNED*")), list(tmp_path.glob("PWNED*"))
