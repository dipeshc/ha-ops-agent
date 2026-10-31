"""The live privilege self-test, exercised with fakes for the as-ops parts."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from opsd import selftest
from opsd.config import Paths
from opsd.privsep import Account
from opsd.store import Store

OPS = Account(uid=1001, gid=1001, groups=frozenset({1001}))
MOUNTINFO_OK = (
    "1 0 0:1 / / rw - overlay overlay rw\n"
    "2 1 8:8 /supervisor/homeassistant /mnt/ha/config rw - ext4 /dev/mmcblk0p8 rw\n"
    "3 1 8:8 /supervisor/backup /mnt/ha/backup ro - ext4 /dev/mmcblk0p8 rw\n"
)


def done(stdout: str, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, "")


def fake_run(
    *, leak: str | None = None, hook: str = "deny", nnp: str = "1", socket_reply: dict[str, Any] | None = None
) -> Any:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if argv[0] == "python3" and argv[3] == selftest.SOCKET_PROBE:
            reply = socket_reply or {"connect": "ok", "response": {"ok": False, "error": "no agent run is active"}}
            return done(json.dumps(reply))
        if argv[0] == "python3":
            paths = json.loads(argv[-1])
            result = {p: ("readable" if p in (paths[-1], leak) else "denied:13") for p in paths}
            return done(json.dumps({"uid": 1001, "gid": 1001, "groups": [], "paths": result, "no_new_privs": nnp}))
        if argv[0] == "claude":
            return done("2.1.308 (Claude Code)\n")
        return done(json.dumps({"hookSpecificOutput": {"permissionDecision": hook}}))

    return run


OUTSIDER_DENIED = fake_run(socket_reply={"connect": "denied:13"})


def private_data(paths: Paths) -> None:
    Store.open(paths.store_file).close()
    os.chmod(paths.data.parent, 0o755)
    os.chmod(paths.data, 0o711)
    for p in paths.data.rglob("*"):
        os.chmod(p, 0o700 if p.is_dir() else 0o600)


def scan_root(tmp_path: Path) -> Path:
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "ok").write_text("x")
    return root


def test_selftest_passes_with_a_sound_boundary(paths: Paths, tmp_path: Path) -> None:
    private_data(paths)
    result = selftest.run_selftest(
        paths,
        daemon_pid=1,
        account=OPS,
        run=fake_run(),
        run_outsider=OUTSIDER_DENIED,
        mountinfo=MOUNTINFO_OK,
        scan_root=scan_root(tmp_path),
        trusted_root=tmp_path,
        expected_store_owner=os.getuid(),
    )
    failing = {k: v.detail for k, v in result.checks.items() if not v.ok}
    # isolation uses real permission bits of the tmp tree; ignore it on a dev machine.
    failing.pop("isolation", None)
    assert failing == {}
    assert not (paths.export_root / ".selftest-probe").exists()


def test_selftest_catches_each_regression(paths: Paths, tmp_path: Path) -> None:
    os.chmod(tmp_path, 0o755)
    root = scan_root(tmp_path)
    suid = root / "bin" / "su"
    suid.write_text("x")
    os.chmod(suid, 0o4755)
    (paths.data / "history.jsonl").write_text("{}")
    os.chmod(paths.data / "history.jsonl", 0o644)
    bad_mounts = MOUNTINFO_OK + "4 1 8:8 /supervisor/homeassistant /homeassistant rw - ext4 x rw\n"
    result = selftest.run_selftest(
        paths,
        daemon_pid=1,
        account=OPS,
        run=fake_run(
            leak=str(paths.options_file),
            hook="allow",
            nnp="0",
            socket_reply={"connect": "ok", "response": {"ok": True, "output": "queued"}},
        ),
        run_outsider=fake_run(socket_reply={"connect": "ok", "response": {"ok": False}}),
        mountinfo=bad_mounts,
        scan_root=root,
        trusted_root=tmp_path,
    )
    failed = {k for k, v in result.checks.items() if not v.ok}
    assert {"as_ops_read", "no_setuid", "mounts", "data_modes", "hook_denies", "no_new_privs",
            "store_private", "socket_boundary"} <= failed  # fmt: skip
    assert not result.ok and result.summary().startswith("fail: ")
    assert result.checks["self_check_trips"].ok  # the deliberate probe is always flagged


def test_selftest_without_ops_user(paths: Paths, monkeypatch: Any) -> None:
    monkeypatch.setattr(selftest, "ops_account", lambda: None)
    result = selftest.run_selftest(paths)
    assert not result.ok and "ops_user" in result.summary()


def test_positive_control_must_succeed() -> None:
    check = selftest.check_access({"paths": {"/s": "denied:13", "/probe": "denied:13"}}, ["/s"], "/probe")
    assert not check.ok and "positive control" in check.detail


def test_mount_parsing() -> None:
    assert selftest.mount_points("1 0 0:1 / /a\\040b rw - x y z") == ["/a b"]
    assert selftest.check_mounts(MOUNTINFO_OK).ok
    assert not selftest.check_mounts("1 0 0:1 / / rw - x y z").ok


def test_to_json_shape(paths: Paths) -> None:
    result = selftest.SelfTest(at="t", checks={"a": selftest.Check(True, "d")})
    assert result.to_json() == {"at": "t", "ok": True, "summary": "pass", "checks": {"a": {"ok": True, "detail": "d"}}}
    assert selftest.SelfTest(at="t").summary() == "fail: no checks ran"


def test_check_registry_passes() -> None:
    check = selftest.check_registry()
    assert check.ok, check.detail


def test_check_store_and_socket(tmp_path: Path) -> None:
    assert not selftest.check_store(tmp_path / "missing.db").ok
    db = tmp_path / "ops.db"
    db.write_text("")
    os.chmod(db, 0o644)
    assert not selftest.check_store(db, expected_owner=os.getuid()).ok
    os.chmod(db, 0o600)
    assert selftest.check_store(db, expected_owner=os.getuid()).ok
    assert not selftest.check_store(db, expected_owner=0 if os.getuid() else 1).ok
    sock = tmp_path / "s.sock"
    ok_ops = fake_run()
    assert selftest.check_socket(ok_ops, OUTSIDER_DENIED, sock).ok
    reached = fake_run(socket_reply={"connect": "ok", "response": {"ok": False}})
    assert "reached the socket" in selftest.check_socket(ok_ops, reached, sock).detail
    accepted = fake_run(socket_reply={"connect": "ok", "response": {"ok": True}})
    assert "NOT refused" in selftest.check_socket(accepted, OUTSIDER_DENIED, sock).detail
    unreachable = fake_run(socket_reply={"connect": "denied:13"})
    assert "could not reach" in selftest.check_socket(unreachable, OUTSIDER_DENIED, sock).detail

    def garbage(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, "not json", "boom")

    assert not selftest.check_socket(garbage, OUTSIDER_DENIED, sock).ok
