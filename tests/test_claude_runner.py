import gzip
import json
import sys
from pathlib import Path

import pytest

from opsd.claude_runner import (
    CREDENTIAL_ENV,
    RunConfig,
    build_argv,
    build_env,
    interpret_result,
    parse_final_json,
    run_claude,
    tree_rss_mb,
)
from opsd.config import CredentialKind, credential_kind
from opsd.redact import Redactor
from tests.conftest import OPT


def config(tmp_path: Path, binary: str = "claude", **kw: object) -> RunConfig:
    return RunConfig(
        model="claude-sonnet-5-5",
        max_turns=30,
        budget_usd=1.5,
        system_prompt_file=OPT / "prompts" / "system-append.md",
        result_schema_file=OPT / "prompts" / "triage-result.schema.json",
        config_dir=tmp_path / "claude",
        home=tmp_path / "claude" / "home",
        cwd=tmp_path,
        binary=binary,
        **kw,  # type: ignore[arg-type]
    )


def test_argv_is_locked_down(tmp_path: Path) -> None:
    argv = build_argv(config(tmp_path), "do it")
    joined = " ".join(argv)
    argv = argv[argv.index("nice") :]
    assert argv[:3] == ["nice", "-n", "10"]
    assert "--permission-mode dontAsk" in joined
    assert "--tools Read,Grep,Glob,Bash" in joined
    assert "--max-turns 30" in joined and "--max-budget-usd 1.50" in joined
    assert "--no-session-persistence" in joined and "--strict-mcp-config" in joined
    assert "--bare" not in argv and "--dangerously-skip-permissions" not in argv
    schema = json.loads(argv[argv.index("--json-schema") + 1])
    assert schema["required"] == ["result", "summary"]


def test_env_never_carries_supervisor_token(tmp_path: Path) -> None:
    env = build_env({"PATH": "/usr/bin", "SUPERVISOR_TOKEN": "s3cret", "HASSIO_TOKEN": "x"}, config(tmp_path), "oat")
    assert "SUPERVISOR_TOKEN" not in env and "HASSIO_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat"
    assert env["PATH"].startswith("/opt/ops/bin:")
    assert env["DISABLE_UPDATES"] == "1"


# Obviously fake credentials of each documented shape; neither is, or resembles, a real one.
API_KEY = "sk-ant-api03-TEST-ONLY-NOT-REAL"
SETUP_TOKEN = "sk-ant-oat01-TEST-ONLY-NOT-REAL"


def test_the_credential_kind_is_read_from_its_documented_prefix() -> None:
    assert credential_kind(API_KEY) is CredentialKind.API_KEY
    assert credential_kind(f"  {API_KEY}\n") is CredentialKind.API_KEY
    # A setup token shares the `sk-ant-` stem, so the stem alone must not make it a key.
    for token in (SETUP_TOKEN, "sk-ant-oat-xyz", "oat-token-abcdef", "SK-ANT-API03-TEST-ONLY-NOT-REAL"):
        assert credential_kind(token) is CredentialKind.SETUP_TOKEN, token
    assert credential_kind("") is None and credential_kind("  ") is None


@pytest.mark.parametrize(("kind", "secret"), [(CredentialKind.SETUP_TOKEN, SETUP_TOKEN),
                                              (CredentialKind.API_KEY, API_KEY)])  # fmt: skip
def test_the_environment_carries_exactly_one_credential(tmp_path: Path, kind: CredentialKind, secret: str) -> None:
    # Leftovers in the daemon's own environment never reach the run.
    base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "leftover-key", "CLAUDE_CODE_OAUTH_TOKEN": "leftover-token",
            "ANTHROPIC_AUTH_TOKEN": "leftover-bearer"}  # fmt: skip
    env = build_env(base, config(tmp_path, credential=kind), secret)
    assert env[CREDENTIAL_ENV[kind]] == secret
    others = {"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"} - {CREDENTIAL_ENV[kind]}
    assert not others & set(env)
    assert [k for k, v in env.items() if v == secret] == [CREDENTIAL_ENV[kind]]


async def test_an_api_key_run_exports_only_the_key(tmp_path: Path, meminfo: Path) -> None:
    seen = tmp_path / "seen"
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "key=$ANTHROPIC_API_KEY oauth=${{CLAUDE_CODE_OAUTH_TOKEN-absent}}" > "{seen}"\n'
        # A provider that echoed the key back must not get it into the transcript or the error.
        'echo "{\\"type\\":\\"system\\",\\"note\\":\\"$ANTHROPIC_API_KEY\\"}"\n'
        'echo "auth failed for $ANTHROPIC_API_KEY" >&2\n'
        'echo \'{"type":"result","subtype":"success","result":"{}","num_turns":1}\'\n'
    )
    script.chmod(0o755)
    cfg = config(tmp_path, str(script), credential=CredentialKind.API_KEY)
    transcript = tmp_path / "t.gz"
    result = await run_claude(cfg, "p", token=API_KEY, transcript_path=transcript, redactor=Redactor([API_KEY]),
                              meminfo=meminfo)  # fmt: skip
    assert result.status == "ok", result.error
    assert seen.read_text().strip() == f"key={API_KEY} oauth=absent"
    text = gzip.open(transcript, "rt").read()
    assert API_KEY not in text and "TEST-ONLY" not in text and '"result"' in text
    assert API_KEY not in repr(result) and API_KEY not in repr(cfg)
    # Without a result event the stderr tail becomes the error: still masked.
    failing = tmp_path / "fake-claude-fails"
    failing.write_text('#!/bin/sh\necho "invalid x-api-key $ANTHROPIC_API_KEY" >&2\nexit 1\n')
    failing.chmod(0o755)
    result = await run_claude(config(tmp_path, str(failing), credential=CredentialKind.API_KEY), "p", token=API_KEY,
                              transcript_path=transcript, redactor=Redactor([API_KEY]), meminfo=meminfo)  # fmt: skip
    assert result.status != "ok" and "invalid x-api-key" in (result.error or "")
    assert "TEST-ONLY" not in repr(result)


def test_parse_final_json() -> None:
    text = 'Done. Here: {"a": 1} and finally {"result": "ok", "nested": {"s": "a } b"}}'
    assert parse_final_json(text) == {"result": "ok", "nested": {"s": "a } b"}}
    assert parse_final_json("no json") is None


def test_interpret_result() -> None:
    ok = interpret_result({"type": "result", "subtype": "success", "total_cost_usd": 0.31, "num_turns": 7,
                           "structured_output": {"result": "ok"}})  # fmt: skip
    assert (ok.status, ok.cost_usd, ok.turns, ok.structured) == ("ok", 0.31, 7, {"result": "ok"})
    turns = interpret_result({"subtype": "error_max_turns", "is_error": True})
    assert turns.status == "max_turns"
    fallback = interpret_result({"subtype": "success", "result": 'text {"result": "escalated"}'})
    assert fallback.structured == {"result": "escalated"}


def test_tree_rss(tmp_path: Path) -> None:
    for pid, ppid, rss in ((10, 1, 1024 * 300), (11, 10, 1024 * 200), (12, 11, 1024 * 100), (20, 1, 1024 * 999)):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "stat").write_text(f"{pid} (proc name) S {ppid} 0 0")
        (d / "status").write_text(f"Name:\tx\nVmRSS:\t{rss} kB\n")
    assert tree_rss_mb(10, tmp_path) == 600


FAKE_CLAUDE = """#!{python}
import json, sys, time
mode = {mode!r}
print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
print(json.dumps({{"type": "assistant", "message": "token sk-ant-oat01-SHOULDBEHIDDEN"}}), flush=True)
if mode == "sleep":
    time.sleep(30)
print(json.dumps({{"type": "result", "subtype": "success", "total_cost_usd": 0.12, "num_turns": 3,
                  "structured_output": {{"result": "ok", "summary": "fine", "acks": [], "notify": None,
                                         "ops_log": None}}}}), flush=True)
"""


def fake_binary(tmp_path: Path, mode: str) -> str:
    path = tmp_path / f"fake-claude-{mode}"
    path.write_text(FAKE_CLAUDE.format(python=sys.executable, mode=mode))
    path.chmod(0o755)
    return str(path)


async def test_run_claude_success_and_redacted_transcript(tmp_path: Path, meminfo: Path) -> None:
    cfg = config(tmp_path, fake_binary(tmp_path, "ok"))
    transcript = tmp_path / "run" / "t.jsonl.gz"
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(cfg, "p", token="t", transcript_path=transcript, redactor=Redactor(),
                              proc_dir=proc_dir, meminfo=meminfo)  # fmt: skip
    assert result.status == "ok" and result.cost_usd == 0.12 and result.structured is not None
    text = gzip.open(transcript, "rt").read()
    assert "SHOULDBEHIDDEN" not in text and '"type": "result"' in text


async def test_memory_guard_kills_run(tmp_path: Path) -> None:
    low = tmp_path / "meminfo-low"
    low.write_text("MemTotal: 3884000 kB\nMemAvailable: 100000 kB\n")
    cfg = config(tmp_path, fake_binary(tmp_path, "sleep"), poll_s=0.1)
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(cfg, "p", token="t", transcript_path=tmp_path / "t.gz", redactor=Redactor(),
                              proc_dir=proc_dir, meminfo=low)  # fmt: skip
    assert result.status == "aborted_memory_guard"


async def test_timeout(tmp_path: Path, meminfo: Path) -> None:
    cfg = config(tmp_path, fake_binary(tmp_path, "sleep"), timeout_s=0.5)
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(cfg, "p", token="t", transcript_path=tmp_path / "t.gz", redactor=Redactor(),
                              proc_dir=proc_dir, meminfo=meminfo)  # fmt: skip
    assert result.status == "timeout"


@pytest.mark.parametrize("bad", ["", "not json"])
def test_parse_final_json_bad(bad: str) -> None:
    assert parse_final_json(bad) is None


GROUP_CLAUDE = """#!{python}
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({pidfile!r}, "w").write(str(child.pid))
time.sleep(60)
"""


async def test_timeout_kills_whole_process_group(tmp_path: Path, meminfo: Path) -> None:
    import os

    pidfile = tmp_path / "child.pid"
    script = tmp_path / "fake-claude-group"
    script.write_text(GROUP_CLAUDE.format(python=sys.executable, pidfile=str(pidfile)))
    script.chmod(0o755)
    cfg = config(tmp_path, str(script), timeout_s=1.5)
    proc_dir = tmp_path / "proc"
    proc_dir.mkdir()
    result = await run_claude(cfg, "p", token="t", transcript_path=tmp_path / "t.gz", redactor=Redactor(),
                              proc_dir=proc_dir, meminfo=meminfo)  # fmt: skip
    assert result.status == "timeout"
    child = int(pidfile.read_text())
    import asyncio

    for _ in range(20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("grandchild survived")


async def test_missing_binary_is_an_error_not_a_crash(tmp_path: Path, meminfo: Path) -> None:
    cfg = config(tmp_path, str(tmp_path / "does-not-exist"))
    result = await run_claude(cfg, "p", token="t", transcript_path=tmp_path / "t.gz", redactor=Redactor(),
                              meminfo=meminfo)  # fmt: skip
    assert result.status == "error"


def test_no_new_privs_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import opsd.claude_runner as runner

    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/setpriv" if name == "setpriv" else None)
    argv = build_argv(config(tmp_path), "p")
    assert argv[:3] == ["/usr/bin/setpriv", "--no-new-privs", "--"] and argv[3] == "nice"
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    assert build_argv(config(tmp_path), "p")[0] == "nice"
