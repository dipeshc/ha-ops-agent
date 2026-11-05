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
    Repo(ha, tmp_path / "k", tmp_path / "kh").commit_files(
        {"notes.md": (ha / "notes.md").read_bytes()}, "docs(notes): record test"
    )
    assert not list(tmp_path.glob("PWNED*")), list(tmp_path.glob("PWNED*"))


def _hostile(tmp_path: Path) -> tuple[Path, Path]:
    """A committed repo and a script that leaves a marker file when anything runs it."""
    ha = tmp_path / "ha"
    (ha / "automations").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(ha)], check=True)
    (ha / "automations" / "a.yaml").write_text("- id: one\n")
    git(ha, "add", "-A")
    git(ha, "commit", "-q", "-m", "init")
    script = tmp_path / "program.sh"
    script.write_text(f'#!/bin/sh\ntouch "{tmp_path}/PWNED_$(basename "$0")_$$"\nexit 1\n')
    script.chmod(0o755)
    return ha, script


def _markers(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("PWNED*"))


def test_repo_config_cannot_set_a_signing_program(tmp_path: Path) -> None:
    from opsd.gitops import Repo

    ha, script = _hostile(tmp_path)
    git(ha, "config", "commit.gpgSign", "true")
    git(ha, "config", "gpg.program", str(script))
    git(ha, "config", "gpg.format", "ssh")
    git(ha, "config", "gpg.ssh.program", str(script))
    repo = Repo(ha, tmp_path / "k", tmp_path / "kh")
    head = repo.main_head()
    commit = repo.commit_tree(repo.run("rev-parse", f"{head}^{{tree}}").strip(), head, "fix(a): test")
    assert "gpgsig" not in repo.run("cat-file", "commit", commit)
    (ha / "automations" / "a.yaml").write_text("- id: two\n")
    repo.commit_files({"automations/a.yaml": (ha / "automations/a.yaml").read_bytes()}, "fix(a): test")
    assert not _markers(tmp_path)


def test_only_ssh_transport_is_allowed(tmp_path: Path) -> None:
    from opsd.safegit import git_argv, git_env

    ha, _ = _hostile(tmp_path)
    git(ha, "config", "protocol.ext.allow", "always")
    git(ha, "config", "protocol.file.allow", "always")
    other = tmp_path / "other.git"
    subprocess.run(["git", "init", "-q", "--bare", str(other)], check=True)
    for url in (f"ext::sh -c touch% {tmp_path}/PWNED_ext", str(other)):
        done = subprocess.run(git_argv(ha, "fetch", url, "main"), capture_output=True, env=git_env(), check=False)
        assert done.returncode != 0 and b"not allowed" in done.stderr
    assert not _markers(tmp_path)


def test_push_names_a_checked_remote_url_never_the_config(tmp_path: Path) -> None:
    from opsd.gitops import GitError, Repo

    ha, _ = _hostile(tmp_path)
    git(ha, "remote", "add", "origin", f"ext::sh -c touch% {tmp_path}/PWNED_ext")
    git(ha, "config", "protocol.ext.allow", "always")
    with pytest.raises(GitError, match="not a GitHub SSH URL"):
        Repo(ha, tmp_path / "k", tmp_path / "kh").push()
    git(ha, "remote", "set-url", "origin", "git@github.com:example/config.git")
    assert Repo(ha, tmp_path / "k", tmp_path / "kh").remote_url() == "git@github.com:example/config.git"
    assert not _markers(tmp_path)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.gitProxy", "PROGRAM"),
        ("include.path", "PATH/extra.cfg"),
        ("includeIf.gitdir:/.path", "PATH/extra.cfg"),
        ("url.ext::sh -c touch% x #.insteadOf", "git@github.com:"),
        ("url.PATH/.pushInsteadOf", "git@github.com:"),
        ("core.worktree", "PATH"),
        ("core.alternateRefsCommand", "PROGRAM"),
        ("remote.origin.uploadpack", "PROGRAM"),
        ("remote.origin.receivepack", "PROGRAM"),
        ("remote.origin.vcs", "evil"),
        ("remote.mirror.url", "PATH"),
    ],
)
def test_repo_config_a_override_cannot_neutralise_refuses_git(tmp_path: Path, key: str, value: str) -> None:
    from opsd.gitops import GitError, Repo

    ha, script = _hostile(tmp_path)
    git(ha, "config", key, value.replace("PROGRAM", str(script)).replace("PATH", str(tmp_path)))
    with pytest.raises(GitError, match="refusing to run git"):
        Repo(ha, tmp_path / "k", tmp_path / "kh").main_head()
    with pytest.raises(HistoryError, match="refusing to run git"):
        githistory.status(ha)
    assert not _markers(tmp_path)


def test_filter_and_diff_drivers_of_any_name_never_run(tmp_path: Path) -> None:
    from opsd.gitops import Repo

    ha, script = _hostile(tmp_path)
    (ha / ".git" / "info").mkdir(exist_ok=True)
    (ha / ".git" / "info" / "attributes").write_text("*.yaml filter=a:b diff=c:d\n")
    for variable in ("clean", "smudge", "process"):
        git(ha, "config", f"filter.a:b.{variable}", str(script))
    git(ha, "config", "filter.a:b.required", "true")
    git(ha, "config", "diff.c:d.command", str(script))
    git(ha, "config", "diff.c:d.textconv", str(script))
    (ha / "automations" / "a.yaml").write_text("- id: two\n")
    githistory.diff(ha, "HEAD", None)
    Repo(ha, tmp_path / "k", tmp_path / "kh").commit_files(
        {"automations/a.yaml": (ha / "automations/a.yaml").read_bytes()}, "fix(a): test"
    )
    githistory.show(ha, "HEAD", None)
    assert not _markers(tmp_path)


def test_credential_helpers_and_askpass_never_run(tmp_path: Path) -> None:
    from opsd.safegit import git_argv, git_env

    ha, script = _hostile(tmp_path)
    git(ha, "config", "credential.helper", f"!{script}")
    git(ha, "config", "core.askPass", str(script))
    done = subprocess.run(
        git_argv(ha, "credential", "fill"),
        input=b"protocol=https\nhost=example.invalid\n\n",
        capture_output=True,
        env=git_env({"SSH_ASKPASS": str(script)}) | {"GIT_ASKPASS": ""},
        check=False,
        timeout=30,
    )
    assert done.returncode != 0
    assert not _markers(tmp_path)


def test_editor_and_hooks_never_run(tmp_path: Path) -> None:
    from opsd.gitops import Repo

    ha, script = _hostile(tmp_path)
    git(ha, "config", "core.editor", str(script))
    git(ha, "config", "sequence.editor", str(script))
    hook = ha / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {tmp_path}/PWNED_hook\n")
    hook.chmod(0o755)
    (ha / "automations" / "a.yaml").write_text("- id: two\n")
    Repo(ha, tmp_path / "k", tmp_path / "kh").commit_files(
        {"automations/a.yaml": (ha / "automations/a.yaml").read_bytes()}, "fix(a): test"
    )
    assert not _markers(tmp_path)


def test_a_filter_driver_added_after_the_overrides_were_built_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The config snapshot and the git call are two processes: a driver added in between must find nothing to run."""
    from opsd import health, safegit
    from opsd.gitops import Repo

    ha, script = _hostile(tmp_path)
    git(ha, "config", "remote.origin.url", "git@github.com:acme/house.git")
    git(ha, "update-ref", "refs/remotes/origin/main", "HEAD")
    real = safegit.git_argv

    def racing(repo: Path, *args: str) -> list[str]:
        argv = real(repo, *args)
        # The race: written after the overrides were computed, before git runs.
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("* filter=late\n")
        with (repo / ".git" / "config").open("a") as config:
            config.write(f'[filter "late"]\n\tclean = {script}\n\tsmudge = {script}\n\trequired = false\n')
        return argv

    # The reproduction: under the same race, `add` (what the agent used to run) does run the driver.
    (ha / "automations" / "a.yaml").write_text("- id: two\n")
    subprocess.run(racing(ha, "add", "-A", "--", "automations/a.yaml"), capture_output=True, env=safegit.git_env(),
                   check=False)  # fmt: skip
    assert _markers(tmp_path), "the race reproduction should run the late driver through add"
    for marker in _markers(tmp_path):
        marker.unlink()
    subprocess.run(["git", "-C", str(ha), "-c", "filter.late.clean=cat", "reset", "-q"], check=True)
    git(ha, "config", "--remove-section", "filter.late")

    # Every git call goes through `safegit.run`, so this one patch covers gitops, githistory and health too.
    monkeypatch.setattr(safegit, "git_argv", racing)
    repo = Repo(ha, tmp_path / "k", tmp_path / "kh")
    (ha / "automations" / "a.yaml").write_text("- id: three\n")
    assert repo.dirty_paths() == ["automations/a.yaml"]
    assert githistory.status(ha) == " M automations/a.yaml\n"
    assert health.git_status(ha)["dirty"] == ["automations/a.yaml"]
    githistory.diff(ha, "HEAD", None)
    head = repo.commit_files(
        {"automations/a.yaml": (ha / "automations/a.yaml").read_bytes()}, "fix(automations): bump a"
    )
    assert repo.file_at(head, "automations/a.yaml") == b"- id: three\n"
    assert not _markers(tmp_path), _markers(tmp_path)


def test_status_lines_match_git_status_on_a_plain_repo(tmp_path: Path) -> None:
    from opsd.safegit import status_lines

    ha, _ = _hostile(tmp_path)
    (ha / "automations" / "b.yaml").write_text("- id: b\n")
    git(ha, "add", "automations/b.yaml")
    (ha / "automations" / "a.yaml").write_text("- id: changed\n")
    (ha / "new.yaml").write_text("x: 1\n")
    (ha / "automations" / "b.yaml").unlink()
    assert sorted(status_lines(ha)) == sorted(git(ha, "status", "--porcelain", "--untracked-files=all").splitlines())


def test_a_late_driver_never_runs_while_committing_even_with_racily_clean_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Touching another tracked file makes the live index re-check it by content; committing never does."""
    import os

    from opsd import safegit
    from opsd.gitops import Repo

    ha, script = _hostile(tmp_path)
    (ha / "automations" / "side_b.yaml").write_text("- id: side_b\n")
    git(ha, "add", "-A")
    git(ha, "commit", "-q", "-m", "side")
    real = safegit.git_argv

    def racing(repo: Path, *args: str) -> list[str]:
        argv = real(repo, *args)
        (repo / ".git" / "info").mkdir(exist_ok=True)
        (repo / ".git" / "info" / "attributes").write_text("* filter=late\n")
        with (repo / ".git" / "config").open("a") as config:
            config.write(f'[filter "late"]\n\tclean = {script}\n\tsmudge = {script}\n\tprocess = {script}\n')
        return argv

    monkeypatch.setattr(safegit, "git_argv", racing)
    # Same content, new times: racily clean for the live index.
    os.utime(ha / "automations" / "side_b.yaml", None)
    (ha / "automations" / "a.yaml").write_text("- id: example\n")
    head = Repo(ha, tmp_path / "k", tmp_path / "kh").commit_files(
        {"automations/a.yaml": b"- id: example\n"}, "fix(automations): update example"
    )
    assert not _markers(tmp_path), _markers(tmp_path)
    assert git(ha, "show", f"{head}:automations/a.yaml") == "- id: example\n"


def test_a_link_swapped_in_at_the_path_cannot_change_what_is_committed(tmp_path: Path) -> None:
    from opsd.gitops import Repo

    ha, _ = _hostile(tmp_path)
    secret = tmp_path / "side_secret.txt"
    secret.write_text("acme-secret-value\n")
    approved = b"- id: approved\n"
    (ha / "automations" / "a.yaml").unlink()
    (ha / "automations" / "a.yaml").symlink_to(secret)
    repo = Repo(ha, tmp_path / "k", tmp_path / "kh")
    head = repo.commit_files({"automations/a.yaml": approved}, "fix(automations): approve example")
    assert repo.file_at(head, "automations/a.yaml") == approved
    assert "acme-secret-value" not in git(ha, "log", "-p", "-1")
    # A deletion removes the path, and a new file is a regular 100644 blob.
    head = repo.commit_files({"automations/a.yaml": None, "automations/new/side.yaml": b"- id: side\n"},
                             "fix(automations): replace example")  # fmt: skip
    assert repo.entry(head, "automations/a.yaml") is None
    assert repo.entry(head, "automations/new/side.yaml")[0] == "100644"  # type: ignore[index]


def test_status_lines_report_an_executable_bit_change(tmp_path: Path) -> None:
    from opsd.safegit import status_lines

    ha, _ = _hostile(tmp_path)
    (ha / "automations" / "a.yaml").chmod(0o755)
    assert status_lines(ha) == [" M automations/a.yaml"]


def test_status_lines_ignore_the_executable_bit_when_git_does(tmp_path: Path) -> None:
    from opsd.safegit import status_lines

    ha, _ = _hostile(tmp_path)
    git(ha, "config", "core.fileMode", "false")
    (ha / "automations" / "a.yaml").chmod(0o755)
    assert status_lines(ha) == []
