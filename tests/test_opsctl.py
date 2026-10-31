"""opsctl verbs end to end: client parser -> RPC schema -> daemon-side queries."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from opsd import opsctl, queries, rpc
from opsd.baseline import seed
from opsd.capabilities import CAPABILITIES, resolve_ref
from opsd.config import Paths
from opsd.jsonstore import append_jsonl, write_json_atomic
from opsd.redact import build_redactor
from opsd.runctx import ExecEnv
from opsd.supervisor import ApiError
from tests.conftest import NOW, FakeHA, make_options
from tests.test_capabilities import shipped


class FakeClient:
    token = "sup-token-123456"

    def __init__(self, fake: FakeHA) -> None:
        self.fake = fake
        self.log_text = "line one\nERROR acme_solar via_device boom\nrtsp://admin:pw123456@cam/stream ok\n"
        self.posts: list[str] = []

    def supervisor(self, path: str, method: str = "GET", body: Any = None) -> Any:
        if method != "GET":
            self.posts.append(path)
        if path in ("/core/stats", "/supervisor/stats"):
            return {"cpu_percent": 1.0, "memory_usage": 5, "memory_limit": 10, "memory_percent": 50.0}
        if path == "/core/info":
            return {"version": "2026.10.3", "state": "running", "boot": True}
        if path == "/host/info":
            raise ApiError("GET", path, 500, "nope")
        return {"version": "x"} if path.endswith("/info") and path != "/resolution/info" else _sup(self.fake, path)

    def core(self, path: str) -> Any:
        assert path == "/states"
        return [
            *self.fake.states,
            {
                "entity_id": "camera.door",
                "state": "idle",
                "attributes": {"entity_picture": "/api/x?token=abcdef123456"},
            },
        ]

    def logs(self, path: str, lines: int) -> str:
        self.last_logs = (path, lines)
        return self.log_text


def _sup(fake: FakeHA, path: str) -> Any:
    return {
        "/resolution/info": {"issues": [], "unhealthy": [], "unsupported": []},
        "/backups": {"backups": fake.backups},
    }[path]


@pytest.fixture
def env(paths: Paths, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> FakeClient:
    client = FakeClient(fake_ha)

    class FakeLive:
        def __init__(self, _c: Any) -> None:
            pass

        async def __aenter__(self) -> FakeHA:
            return fake_ha

        async def __aexit__(self, *_: object) -> None:
            return None

    original = queries.collect

    async def collect(api: Any, *a: Any, **kw: Any) -> Any:
        kw["meminfo_path"] = meminfo
        return await original(api, *a, **kw)

    monkeypatch.setattr(queries, "LiveAPI", FakeLive)
    monkeypatch.setattr(queries, "collect", collect)
    client.server = rpc.RpcServer(make_env(paths, client))  # type: ignore[attr-defined]
    return client


def make_env(paths: Paths, client: Any) -> ExecEnv:
    return ExecEnv(
        paths=paths, options=make_options(), client=client, redactor=lambda: build_redactor(paths.secrets_yaml)
    )


SERVER: list[rpc.RpcServer] = []


@pytest.fixture(autouse=True)
def _server(request: pytest.FixtureRequest) -> None:
    SERVER.clear()
    if "env" in request.fixturenames:
        SERVER.append(request.getfixturevalue("env").server)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    """What opsctl would send, dispatched exactly as the daemon does."""
    import asyncio

    args = opsctl.build_parser().parse_args(list(argv))
    request = {k: v for k, v in vars(args).items() if k != "verb" and v is not None}
    raw = json.dumps({"verb": args.verb, "args": request}).encode()
    response = asyncio.run(SERVER[0].dispatch(raw))
    if response["ok"]:
        return 0, response["output"]
    return 1, "error: " + response["error"]


def test_status_and_baseline(env: FakeClient, paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    write_json_atomic(paths.state_file, {"last_result": "ok"})
    append_jsonl(paths.history_file, {"at": "x", "result": "ok"})
    code, out = run(capsys, "status")
    assert code == 0 and json.loads(out)["state"]["last_result"] == "ok"
    assert json.loads(run(capsys, "baseline")[1]) == {"baseline": None}


async def _snapshot(fake: FakeHA, paths: Paths, meminfo: Path) -> Any:
    from opsd.health import collect

    return await collect(fake, paths, make_options(), now=NOW, meminfo_path=meminfo)


def test_health_and_diff(
    env: FakeClient, paths: Paths, capsys: pytest.CaptureFixture[str], fake_ha: FakeHA, meminfo: Path
) -> None:
    import asyncio

    code, out = run(capsys, "health")
    data = json.loads(out)
    assert code == 0 and data["unavailable"]["count"] == 1 and "config_check" in data
    assert env.posts == []  # health never POSTs /core/check
    assert json.loads(run(capsys, "health", "--diff")[1]) == {"error": "no baseline yet"}
    write_json_atomic(paths.baseline_file, seed(asyncio.run(_snapshot(fake_ha, paths, meminfo)), NOW).to_json())
    assert json.loads(run(capsys, "baseline")[1])["unavailable_entities"] == 1
    fake_ha.set_unavailable("light.kitchen", hours_ago=10)
    keys = [i["key"] for i in json.loads(run(capsys, "health", "--diff")[1])]
    assert "entities:acme_solar" in keys


def test_entities_integrations_entries(env: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    out = json.loads(run(capsys, "entities")[1])
    assert out["unavailable_by_platform"] == {"zha": 1}
    assert json.loads(run(capsys, "entities", "--unavailable", "--platform", "zha")[1])["count"] == 1
    assert json.loads(run(capsys, "entities", "--platform", "acme_solar")[1])["count"] == 3
    one = run(capsys, "entities", "--entity", "camera.door")[1]
    assert "abcdef123456" not in one
    assert "error" in json.loads(run(capsys, "entities", "--entity", "light.nope")[1])
    assert json.loads(run(capsys, "integrations")[1])["acme_solar"]["entities"] == 3
    assert json.loads(run(capsys, "entries", "--domain", "zha")[1])["count"] == 1
    assert json.loads(run(capsys, "entries", "--failed")[1])["count"] == 0


def test_repairs_updates_backups_system(env: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    assert json.loads(run(capsys, "repairs")[1])["core"] == []
    assert json.loads(run(capsys, "updates")[1])[0]["entity_id"] == "update.home_assistant_core_update"
    assert json.loads(run(capsys, "backups")[1])[0]["name"] == "auto"
    system = json.loads(run(capsys, "system")[1])
    assert system["core"]["version"] == "2026.10.3" and "error" in system["host"]


def test_logs_literal_grep_and_redaction(env: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    out = run(capsys, "logs", "core", "--grep", "VIA_DEVICE")[1]
    assert out.strip() == "ERROR acme_solar via_device boom"
    # Regex metacharacters are literal: no ReDoS, and '.*' matches nothing here.
    assert run(capsys, "logs", "core", "--grep", "(a+)+$")[1].strip() == ""
    assert run(capsys, "logs", "core", "--grep", ".*")[1].strip() == ""
    assert "pw123456" not in run(capsys, "logs", "core")[1]
    run(capsys, "logs", "host", "--identifier", "kernel", "--lines", "99999")
    assert env.last_logs == ("/host/logs/identifiers/kernel", 1000)
    assert "error" in json.loads(run(capsys, "logs", "addon")[1])
    code, out = run(capsys, "logs", "host", "--identifier", "../../etc")
    assert code == 1 and "invalid characters" in out
    run(capsys, "logs", "addon", "--slug", "core_example_broker")
    assert env.last_logs[0] == "/addons/core_example_broker/logs"


def test_output_secrets_and_cap(env: FakeClient, capsys: pytest.CaptureFixture[str]) -> None:
    env.log_text = "wifi hunter22secret\n" + "x" * 50_000
    out = run(capsys, "logs", "core", "--lines", "5")[1]
    assert "hunter22secret" not in out
    assert "truncated" in out or len(out) < opsctl.MAX_OUTPUT_CHARS + 200


def test_git_verbs_via_cli(env: FakeClient, paths: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    ha = paths.ha_config
    subprocess.run(["git", "init", "-q", "-b", "main", str(ha)], check=True)
    (ha / ".gitignore").write_text("*\n!*/\n!/configuration.yaml\n!/.gitignore\n")
    (ha / "configuration.yaml").write_text("homeassistant:\n")
    subprocess.run(["git", "-C", str(ha), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(ha), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], check=True
    )
    assert run(capsys, "git-status")[1].strip() == "clean"
    assert "init" in run(capsys, "git-log", "-n", "3")[1]
    assert run(capsys, "git-show", "HEAD:configuration.yaml")[1].strip() == "homeassistant:"
    code, out = run(capsys, "git-show", "HEAD:secrets.yaml")
    assert code == 1 and "not readable" in out
    code, out = run(capsys, "git-diff", "--", "--output=/tmp/x")
    assert code == 1 and "invalid revision" in out
    with pytest.raises(SystemExit):
        opsctl.build_parser().parse_args(["git-diff", "--output=/tmp/x"])


@pytest.mark.parametrize(
    ("request_obj", "reason"),
    [
        ({"verb": "rm", "args": {}}, "unknown verb"),
        ({"verb": "status", "args": {"x": 1}}, "unknown argument"),
        ({"verb": "status", "args": [], "more": 1}, "verb, args"),
        ({"verb": "status", "args": []}, "args must be an object"),
        ({"verb": "logs", "args": {"source": "kernel"}}, "one of"),
        ({"verb": "logs", "args": {"lines": "10"}}, "integer"),
        ({"verb": "logs", "args": {"lines": True}}, "integer"),
        ({"verb": "health", "args": {"diff": 1}}, "boolean"),
        ({"verb": "logs", "args": {"grep": "a\nb"}}, "invalid characters"),
        ({"verb": "logs", "args": {"grep": "a\u0000"}}, "invalid characters"),
        ({"verb": "logs", "args": {"slug": "../x"}}, "invalid characters"),
        ({"verb": "entities", "args": {"platform": "Acme Solar;rm"}}, "invalid characters"),
        ({"verb": "git-show", "args": {"spec": "x" * 301}}, "invalid characters"),
        ({"verb": 7}, "unknown verb"),
    ],
)
def test_rpc_schema_rejects(request_obj: dict[str, Any], reason: str) -> None:
    with pytest.raises(rpc.RequestError, match=reason):
        rpc.parse_request(json.dumps(request_obj).encode())


def test_rpc_parse_clamps_and_defaults() -> None:
    cap, args = rpc.parse_request(b'{"verb": "logs", "args": {"lines": 99999}}')
    assert cap.verb == "logs" and args.number("lines") == 1000 and args.text("source") == "core"
    assert args.opt_text("grep") is None
    with pytest.raises(rpc.RequestError, match="too large"):
        rpc.parse_request(b"{" + b" " * rpc.MAX_REQUEST_BYTES + b"}")
    with pytest.raises(rpc.RequestError, match="not JSON"):
        rpc.parse_request(b"\xff\xfe")
    for cap in filter(shipped, CAPABILITIES.values()):
        assert callable(resolve_ref(cap.executor))


def test_rpc_bad_request_is_reported(env: FakeClient) -> None:
    import asyncio

    response = asyncio.run(env.server.dispatch(b'{"verb": "nope"}'))  # type: ignore[attr-defined]
    assert response == {"ok": False, "error": "bad request: unknown verb"}


async def test_socket_roundtrip_and_permissions(
    env: FakeClient, paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import dataclasses
    import stat

    # AF_UNIX paths are short on macOS; bind relative to a short cwd.
    monkeypatch.chdir(tmp_path)
    short = dataclasses.replace(paths, socket_dir=Path("s"))
    server = rpc.RpcServer(make_env(short, env))
    await server.start(group_gid=None)
    try:
        assert stat.S_IMODE(Path("s/opsd.sock").stat().st_mode) == 0o660
        monkeypatch.setenv("OPS_SOCKET_DIR", "s")
        code = await asyncio.to_thread(opsctl.main, ["status"])
        assert code == 0
        raw = await asyncio.to_thread(rpc.call, Path("s/opsd.sock"), "logs", {"source": "nope"})
        assert raw["ok"] is False and "one of" in raw["error"]
    finally:
        await server.stop()


def test_client_without_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPS_SOCKET_DIR", "missing")
    assert opsctl.main(["status"]) == 1
    assert "cannot reach" in capsys.readouterr().err


def test_rpc_rejects_unexpected_peer(env: FakeClient) -> None:
    server = rpc.RpcServer(env.server._env, allowed_uids=frozenset({12345}))  # type: ignore[attr-defined]
    assert server._allowed_uids == frozenset({12345})
    assert rpc.peer_uid(None) is None


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert opsctl.main(["help"]) == 0
    assert "git-diff" in capsys.readouterr().out


async def test_rpc_limits(env: FakeClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import time

    server: rpc.RpcServer = env.server  # type: ignore[attr-defined]
    run = make_run(requests_left=2)
    server.begin_run(run)
    for _ in range(2):
        assert (await server.dispatch(b'{"verb": "status"}'))["ok"]
    exhausted = await server.dispatch(b'{"verb": "status"}')
    assert not exhausted["ok"] and "budget" in exhausted["error"]
    server.end_run()

    def slow(_ctx: Any, _args: Any) -> str:
        time.sleep(0.4)
        return "late"

    server._executors["opsd.queries:cmd_status"] = slow
    monkeypatch.setattr(rpc, "REQUEST_TIMEOUT_S", 0.05)
    timed_out = await server.dispatch(b'{"verb": "status"}')
    assert timed_out == {"ok": False, "error": "query timed out"}
    # The worker thread still runs, so its slot stays taken until it finishes.
    assert server._sem._value == rpc.MAX_CONCURRENT - 1
    await asyncio.sleep(0.6)
    assert server._sem._value == rpc.MAX_CONCURRENT


async def test_socket_denies_unknown_peer_and_overload(
    env: FakeClient, paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import dataclasses

    monkeypatch.chdir(tmp_path)
    short = dataclasses.replace(paths, socket_dir=Path("p"))
    # macOS has no SO_PEERCRED: an unknown peer uid must be denied, not allowed.
    server = rpc.RpcServer(make_env(short, env), allowed_uids=frozenset({0}))
    await server.start(group_gid=None)
    try:
        # Denied either way: macOS (no SO_PEERCRED) answers with an error, Linux drops the connection.
        try:
            response = await asyncio.to_thread(rpc.call, Path("p/opsd.sock"), "status", {})
        except ConnectionResetError, BrokenPipeError:
            response = {"ok": False, "error": "peer not allowed"}
        assert response == {"ok": False, "error": "peer not allowed"}
        server._allowed_uids = None
        server._connections = rpc.MAX_CONNECTIONS
        response = await asyncio.to_thread(rpc.call, Path("p/opsd.sock"), "status", {})
        assert response == {"ok": False, "error": "too many concurrent connections"}
    finally:
        await server.stop()


def make_run(**kw: Any) -> Any:
    from datetime import UTC, datetime

    from opsd.channels import BudgetClass
    from opsd.runctx import AGENT_SELF, RunContext
    from opsd.runner import READ_CAPABILITIES

    return RunContext(profile="triage", principal=AGENT_SELF, allowed=READ_CAPABILITIES,
                      budget_class=BudgetClass.AGENT, started_at=datetime.now(UTC), **kw)  # fmt: skip
