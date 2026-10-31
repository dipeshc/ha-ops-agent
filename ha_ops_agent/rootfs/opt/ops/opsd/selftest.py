"""Live self-test of the privilege boundary, run by the daemon (as root).

The SSH add-on cannot exec into this container, so the daemon proves the
boundary itself at startup and on demand (panel button), logs the result and
publishes it as the `selftest` attribute of `sensor.ops_agent`.

Checks:
- as `ops` (with no_new_privs): secrets, the HA config, .storage, the s6
  environment and the daemon's /proc environ are unreadable, while a
  deliberately exposed probe file IS readable (positive control);
- the permission self-check flags that deliberate exposure;
- no setuid/setgid binaries on the root filesystem;
- the config and backup mounts sit under /mnt/ha, nothing at the default
  add-on paths;
- files under /data (other than claude-ops/) are private to root;
- the PreToolUse hook denies `cat /etc/passwd` when run as ops;
- `claude --version` works as ops, the uid is the ops uid, NoNewPrivs is 1;
- the store (SQLite) is a root-owned 0600 file that ops cannot read;
- the opsctl socket refuses a uid outside the ops group at connect time, and
  refuses a mutating request from ops while no agent run is bound to it;
- the capability registry is consistent: every executor, validator and
  resolver resolves, the guard's verb list and the opsctl parser are
  generated from it, and every profile names only enabled capabilities.
"""

import contextlib
import json
import os
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opsd.capabilities import CAPABILITIES, build_parser, cli_verbs, resolve_ref
from opsd.claude_runner import no_new_privs_prefix
from opsd.config import Paths
from opsd.privsep import Account, isolation_problems, ops_account
from opsd.profile import OVERLAYS, overlay_installed

FORBIDDEN_MOUNTS = ("/config", "/homeassistant", "/share", "/ssl", "/media", "/addons", "/addon_configs", "/backup")
REQUIRED_MOUNTS = ("/mnt/ha/config", "/mnt/ha/backup")
SKIP_SCAN = ("/proc", "/sys", "/dev", "/run", "/mnt/ha", "/data")

PROBE = r"""
import json, os, sys
out = {"uid": os.getuid(), "gid": os.getgid(), "groups": os.getgroups(), "paths": {}}
for path in json.loads(sys.argv[1]):
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            with open(path, "rb") as handle:
                handle.read(1)
        out["paths"][path] = "readable"
    except FileNotFoundError:
        out["paths"][path] = "missing"
    except OSError as err:
        out["paths"][path] = "denied:" + str(err.errno)
try:
    status = open("/proc/self/status").read()
    out["no_new_privs"] = next((l.split()[1] for l in status.splitlines() if l.startswith("NoNewPrivs:")), "?")
except OSError:
    out["no_new_privs"] = "?"
print(json.dumps(out))
"""

# Connects to the opsctl socket and sends one request; prints what happened.
SOCKET_PROBE = r"""
import json, socket, sys
out = {}
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(sys.argv[1])
    out["connect"] = "ok"
    s.sendall(sys.argv[2].encode() + b"\n")
    data = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    out["response"] = json.loads(data or b"{}")
except OSError as err:
    out["connect"] = "denied:" + str(err.errno)
print(json.dumps(out))
"""
# A uid that is neither root nor in the ops group (Alpine's `nobody`).
OUTSIDER = Account(uid=65534, gid=65534, groups=frozenset({65534}))
UNBOUND_REQUEST = json.dumps({"verb": "ack", "args": {"key": "selftest", "days": 1, "reason": "must be refused"}})

RunAsOps = Callable[[list[str], str | None], subprocess.CompletedProcess[str]]


@dataclass(slots=True)
class Check:
    ok: bool
    detail: str = ""


@dataclass(slots=True)
class SelfTest:
    at: str
    checks: dict[str, Check] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks.values())

    def summary(self) -> str:
        failed = [name for name, check in self.checks.items() if not check.ok]
        return "pass" if self.ok else "fail: " + ", ".join(failed or ["no checks ran"])

    def to_json(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "ok": self.ok,
            "summary": self.summary(),
            "checks": {k: asdict(v) for k, v in self.checks.items()},
        }


def runner_for(account: Account, home: Path) -> RunAsOps:
    def run(argv: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/opt/ops/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
        return subprocess.run(
            [*no_new_privs_prefix(), *argv],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd="/",
            user=account.uid,
            group=account.gid,
            extra_groups=[],
            check=False,
        )

    return run


def secret_targets(paths: Paths, daemon_pid: int) -> list[str]:
    targets = [
        str(paths.options_file),
        str(paths.secrets_dir),
        str(paths.ssh_key),
        str(paths.push_cache_file),
        str(paths.state_file),
        str(paths.store_file),
        str(paths.store_copy),
        "/mnt/ha",
        str(paths.ha_config),
        str(paths.ha_config / "secrets.yaml"),
        str(paths.ha_config / ".storage" / "auth"),
        str(paths.ha_config / ".storage" / "core.config_entries"),
        "/mnt/ha/backup",
        f"/proc/{daemon_pid}/environ",
        "/proc/1/environ",
        str(paths.run),
    ]
    env_dir = Path("/run/s6/container_environment")
    targets.append(str(env_dir))
    with contextlib.suppress(OSError):
        targets += [str(p) for p in env_dir.iterdir()]
    return targets


def check_access(result: dict[str, Any], targets: Iterable[str], probe: str) -> Check:
    seen = result.get("paths", {})
    leaked = [t for t in targets if seen.get(t) == "readable"]
    if leaked:
        return Check(False, f"readable by ops: {leaked}")
    if seen.get(probe) != "readable":
        return Check(False, f"positive control failed: probe {seen.get(probe)}")
    return Check(True, f"{len(seen) - 1} sensitive paths denied or absent; probe readable")


def setuid_files(root: Path = Path("/"), skip: Iterable[str] = SKIP_SCAN) -> list[str]:
    """setuid/setgid files on the root filesystem (like `find / -xdev -perm /6000`)."""
    found: list[str] = []
    device = root.stat().st_dev
    skipped = tuple(skip)
    for directory, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if not os.path.join(directory, d).startswith(skipped)
            and os.lstat(os.path.join(directory, d)).st_dev == device
        ]  # fmt: skip
        for name in filenames:
            path = os.path.join(directory, name)
            try:
                mode = os.lstat(path).st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode) and mode & (stat.S_ISUID | stat.S_ISGID):
                found.append(path)
    return sorted(found)


def mount_points(mountinfo: str) -> list[str]:
    points = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) > 4:
            points.append(fields[4].replace("\\040", " "))
    return points


def check_mounts(mountinfo: str) -> Check:
    points = set(mount_points(mountinfo))
    missing = [m for m in REQUIRED_MOUNTS if m not in points]
    forbidden = [m for m in FORBIDDEN_MOUNTS if m in points]
    if missing or forbidden:
        return Check(False, f"missing {missing}, unexpected {forbidden}")
    return Check(True, "config and backup only under /mnt/ha")


def data_mode_violations(data: Path, exempt: str = "claude-ops") -> list[str]:
    bad: list[str] = []
    top = data.stat().st_mode
    if stat.S_IMODE(top) & 0o066:
        bad.append(f"{data} {oct(stat.S_IMODE(top))}")
    for entry in data.iterdir():
        if entry.name == exempt:
            continue
        for path in [entry, *(entry.rglob("*") if entry.is_dir() and not entry.is_symlink() else [])]:
            mode = stat.S_IMODE(path.lstat().st_mode)
            if not path.is_symlink() and mode & 0o077:
                bad.append(f"{path} {oct(mode)}")
    return bad


def check_hook(run: RunAsOps, hook: Path) -> Check:
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}})
    done = run([str(hook)], event)
    try:
        decision = json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"]
    except json.JSONDecodeError, KeyError, TypeError:
        return Check(False, f"hook output not a decision (exit {done.returncode})")
    return Check(decision == "deny", f"hook decision {decision}")


def check_store(store_file: Path, *, expected_owner: int = 0) -> Check:
    try:
        st = store_file.stat()
    except FileNotFoundError:
        return Check(False, "store missing")
    mode = stat.S_IMODE(st.st_mode)
    ok = mode == 0o600 and st.st_uid == expected_owner
    return Check(ok, f"mode {oct(mode)} owner {st.st_uid}")


def _probe_socket(run: RunAsOps, socket_path: Path) -> dict[str, Any]:
    done = run(["python3", "-I", "-c", SOCKET_PROBE, str(socket_path), UNBOUND_REQUEST], None)
    try:
        value = json.loads(done.stdout)
    except json.JSONDecodeError:
        return {"connect": f"probe failed: {done.stderr.strip()[:200]}"}
    return value if isinstance(value, dict) else {"connect": "probe failed"}


def check_socket(as_ops: RunAsOps, as_outsider: RunAsOps, socket_path: Path) -> Check:
    """Outsiders cannot connect; ops outside the bound run's process tree is refused a mutating verb.

    Holds whether or not a run is active: the probe is never a descendant of the AI run.
    """
    outsider = _probe_socket(as_outsider, socket_path)
    if not str(outsider.get("connect", "")).startswith("denied:"):
        return Check(False, f"uid {OUTSIDER.uid} reached the socket: {outsider.get('connect')}")
    ops = _probe_socket(as_ops, socket_path)
    response = ops.get("response")
    if ops.get("connect") != "ok" or not isinstance(response, dict):
        return Check(False, f"ops could not reach the socket: {ops.get('connect')}")
    if response.get("ok") is not False:
        return Check(False, "mutating request outside a run was NOT refused")
    return Check(True, f"outsider denied; unbound ops refused ({str(response.get('error'))[:80]})")


def check_registry() -> Check:
    from opsd.runner import PROFILES  # noqa: PLC0415 - the runner imports this module

    problems: list[str] = []
    for cap in CAPABILITIES.values():
        # A row for an optional overlay this build does not ship is never offered, so it has nothing to resolve.
        if not all(overlay_installed(name) for name in cap.requires if name in OVERLAYS):
            continue
        for ref in (cap.executor, cap.validator, cap.tier_resolver):
            if ref is None:
                continue
            try:
                resolve_ref(ref)
            except (ImportError, AttributeError, TypeError, ValueError) as err:
                problems.append(f"{ref}: {type(err).__name__}")
    parser_verbs = {a for action in build_parser()._actions if action.dest == "verb" for a in action.choices or ()}
    if parser_verbs - {"help"} != set(cli_verbs()):
        problems.append("opsctl parser and registry disagree")
    for profile in PROFILES.values():
        missing = [n for n in profile.capabilities if n not in CAPABILITIES or not CAPABILITIES[n].enabled]
        if missing:
            problems.append(f"profile {profile.name}: {missing}")
    return Check(not problems, "; ".join(problems) or f"{len(CAPABILITIES)} capabilities consistent")


def run_selftest(
    paths: Paths,
    *,
    daemon_pid: int | None = None,
    account: Account | None = None,
    run: RunAsOps | None = None,
    run_outsider: RunAsOps | None = None,
    mountinfo: str | None = None,
    scan_root: Path = Path("/"),
    expected_uid: int = 1001,
    expected_store_owner: int = 0,
    trusted_root: Path | None = None,
) -> SelfTest:
    result = SelfTest(at=datetime.now(UTC).isoformat(timespec="seconds"))
    account = account or ops_account()
    if account is None:
        result.checks["ops_user"] = Check(False, "ops user missing")
        return result
    result.checks["ops_user"] = Check(account.uid == expected_uid, f"uid {account.uid}")
    run = run or runner_for(account, paths.claude_home)

    probe_dir = paths.export_root
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe = probe_dir / ".selftest-probe"
    probe.write_text("probe\n")
    os.chmod(probe, 0o644)
    try:
        flagged = isolation_problems(paths, account, extra=[probe], trusted_root=trusted_root)
        result.checks["self_check_trips"] = Check(
            str(probe) in flagged, "deliberate exposure detected" if str(probe) in flagged else "probe NOT flagged"
        )
        real = [p for p in flagged if p != str(probe)]
        result.checks["isolation"] = Check(not real, f"exposed: {real}" if real else "no sensitive path reachable")
        targets = secret_targets(paths, daemon_pid or os.getpid())
        done = run(["python3", "-I", "-c", PROBE, json.dumps([*targets, str(probe)])], None)
        try:
            access = json.loads(done.stdout)
        except json.JSONDecodeError:
            access = {}
            result.checks["as_ops_read"] = Check(False, f"probe failed: {done.stderr.strip()[:200]}")
        if access:
            result.checks["as_ops_read"] = check_access(access, targets, str(probe))
            result.checks["as_ops_identity"] = Check(
                access.get("uid") == account.uid and access.get("groups") in ([], [account.gid]),
                f"uid {access.get('uid')} groups {access.get('groups')}",
            )
            result.checks["no_new_privs"] = Check(
                access.get("no_new_privs") == "1", f"NoNewPrivs {access.get('no_new_privs')}"
            )
    finally:
        probe.unlink(missing_ok=True)

    found = setuid_files(scan_root)
    result.checks["no_setuid"] = Check(not found, f"setuid/setgid: {found[:10]}" if found else "none")
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            mountinfo = ""
    result.checks["mounts"] = check_mounts(mountinfo)
    bad = data_mode_violations(paths.data)
    result.checks["data_modes"] = Check(not bad, f"too open: {bad[:10]}" if bad else "private")
    result.checks["hook_denies"] = check_hook(run, paths.opt / "bin" / "guard-hook")
    version = run(["claude", "--version"], None)
    result.checks["claude_runs_as_ops"] = Check(
        version.returncode == 0 and bool(version.stdout.strip()),
        version.stdout.strip()[:80] or version.stderr.strip()[:200],
    )
    result.checks["store_private"] = check_store(paths.store_file, expected_owner=expected_store_owner)
    if paths.store_copy.exists():
        # The copy taken before the upgrade holds the same records: private the same way.
        copy = check_store(paths.store_copy, expected_owner=expected_store_owner)
        result.checks["store_copy_private"] = copy
    result.checks["socket_boundary"] = check_socket(
        run, run_outsider or runner_for(OUTSIDER, Path("/")), paths.socket_path
    )
    result.checks["registry"] = check_registry()
    return result
