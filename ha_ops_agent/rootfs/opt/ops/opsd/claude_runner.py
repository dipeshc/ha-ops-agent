"""Run `claude -p` headless with a memory guard, timeout and redacted transcript."""

import asyncio
import contextlib
import ctypes
import gzip
import json
import logging
import os
import re
import shutil
import signal
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsd.redact import Redactor

_LOGGER = logging.getLogger(__name__)

_ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL", "TZ")
# The only extra variables a run gets: what the guard hook reads (`guard.READ_ENV`, `changepaths.*_ENV`).
GUARD_ENV = frozenset({"OPS_READ_EXTRA", "OPS_PROPOSAL_KINDS", "OPS_FACTS_PATH"})
TOOLS = "Read,Grep,Glob,Bash"
# Chat and investigation runs may also write proposal drafts (guard: only below the drafts directory).
DRAFT_TOOLS = "Read,Grep,Glob,Bash,Write"
PIPE_DRAIN_S = 30.0


@dataclass(frozen=True, slots=True)
class RunConfig:
    model: str
    max_turns: int
    budget_usd: float
    system_prompt_file: Path
    result_schema_file: Path
    config_dir: Path
    home: Path
    cwd: Path
    binary: str = "claude"
    timeout_s: float = 20 * 60
    rss_limit_mb: int = 900
    host_min_available_mb: int = 400
    poll_s: float = 2.0
    nice: int = 10
    # (uid, gid) to drop to; None only in tests on a developer machine.
    run_as: tuple[int, int] | None = None
    tools: str = TOOLS
    # The house profile's documents and proposal rules for the guard hook (`GUARD_ENV` keys only).
    guard_env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunResult:
    status: str
    cost_usd: float = 0.0
    turns: int = 0
    structured: dict[str, Any] | None = None
    error: str = ""
    transcript: str = ""
    denials: list[str] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cache_read: int = 0
    duration_s: float = 0.0
    # Epoch seconds when a subscription usage limit resets, if Claude Code said.
    limit_reset: float | None = None
    # First and last `rate_limit_info` seen during the run (account utilisation per window).
    rate_first: dict[str, Any] | None = None
    rate_last: dict[str, Any] | None = None
    # A plan tier hint from the stream (`pro`, `max5`, `max20`), if Claude Code exposes one.
    plan_hint: str | None = None
    # Tool calls the guard hook never decided, or that were denied although it allowed them:
    # the CLI's behaviour changed (a timeout falls through, a new rule), raised as a health item.
    guard_anomalies: list[str] = field(default_factory=list)


_TIER_RE = re.compile(r"max[_ -]?(5|20)x|\bpro\b", re.IGNORECASE)


def plan_hint(event: dict[str, Any]) -> str | None:
    """`pro` / `max5` / `max20` from a stream event that names the subscription tier (to confirm live)."""
    for key in ("rateLimitTier", "rate_limit_tier", "subscriptionType", "subscription_type"):
        value = event.get(key)
        if isinstance(value, str):
            match = _TIER_RE.search(value)
            if match:
                return f"max{match.group(1)}" if match.group(1) else "pro"
    return None


# Subscription usage limits and API rate limits, as Claude Code reports them
# ("Claude AI usage limit reached|<epoch>", "rate_limit_error", HTTP 429, ...).
_LIMIT_RE = re.compile(r"usage limit|rate[ _-]?limit|\b429\b|overloaded|limit reached", re.IGNORECASE)
_RESET_RE = re.compile(r"limit reached\|(\d{9,11})")


def usage_limit(text: str) -> tuple[bool, float | None]:
    """Whether `text` reports a usage or rate limit, and the reset time if given."""
    if not _LIMIT_RE.search(text):
        return False, None
    match = _RESET_RE.search(text)
    return True, float(match.group(1)) if match else None


PR_SET_NO_NEW_PRIVS = 38


def no_new_privs_prefix() -> list[str]:
    """`setpriv --no-new-privs` so nothing Claude starts can gain privileges (setuid etc.)."""
    setpriv = shutil.which("setpriv")
    return [setpriv, "--no-new-privs", "--"] if setpriv else []


def set_no_new_privs() -> None:
    """Fallback when setpriv is absent: prctl(PR_SET_NO_NEW_PRIVS) in the child (Linux only)."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")


def fresh_state(config_dir: Path, home: Path, run_as: tuple[int, int] | None) -> None:
    """Claude Code's state directory, empty for every run.

    Nothing a run leaves there (session history, caches, settings it wrote) reaches the next
    run. The token comes from the environment, never from this directory. `rmtree` removes
    links without following them; the parent (`/data`) is root-owned, so `ops` cannot swap
    the directory itself for a link, and a link left in its place is only unlinked.
    """
    if config_dir.is_symlink():
        config_dir.unlink()
    elif config_dir.exists():
        shutil.rmtree(config_dir)
    for directory in (config_dir, home):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        if run_as is not None:
            os.chown(directory, *run_as)


GUARD_DECISIONS = "guard-decisions.log"


def guard_anomalies(tool_calls: list[tuple[str, str]], decisions_text: str, denials: list[dict[str, Any]]) -> list[str]:
    """Calls the guard hook never decided, and denials of calls it allowed.

    `tool_calls` are `(id, tool)` from the stream; each decision line is `id<TAB>tool<TAB>verdict`.
    A hook input without an id is matched by tool name instead.
    """
    by_id: dict[str, str] = {}
    unnamed: dict[str, int] = {}
    for line in decisions_text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        call, tool, verdict = parts
        if call != "-":
            by_id[call] = verdict
        else:
            unnamed[tool] = unnamed.get(tool, 0) + 1
    problems = []
    for call, tool in tool_calls:
        if call in by_id:
            continue
        if unnamed.get(tool, 0) > 0:
            unnamed[tool] -= 1
            continue
        problems.append(f"{tool} call the guard hook never decided (timeout or crash)")
    for denial in denials:
        call = str(denial.get("tool_use_id") or "")
        if by_id.get(call) == "allow":
            problems.append(f"{denial.get('tool_name')} call denied by Claude Code although the guard allowed it")
    return problems[:10]


MAX_DECISIONS_BYTES = 1_000_000


def read_decisions(path: Path) -> tuple[str, str | None]:
    """The guard's decision log, read by root from a directory `ops` controls: never through a link,
    only a regular file, at most 1 MB. Anything else is an anomaly (and no decisions)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return "", None
    except OSError as err:
        return "", f"the guard decision log could not be opened safely ({type(err).__name__})"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return "", "the guard decision log is not a regular file"
        if info.st_size > MAX_DECISIONS_BYTES:
            return "", "the guard decision log is larger than 1 MB"
        return os.read(fd, MAX_DECISIONS_BYTES).decode(errors="replace"), None
    finally:
        os.close(fd)


def build_argv(cfg: RunConfig, prompt: str) -> list[str]:
    schema = cfg.result_schema_file.read_text()
    return [
        *no_new_privs_prefix(),
        "nice", "-n", str(cfg.nice),
        cfg.binary, "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--model", cfg.model,
        "--max-turns", str(cfg.max_turns),
        "--max-budget-usd", f"{cfg.budget_usd:.2f}",
        "--permission-mode", "dontAsk",
        "--tools", cfg.tools,
        "--append-system-prompt-file", str(cfg.system_prompt_file),
        "--json-schema", schema,
        "--no-session-persistence",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers": {}}',
    ]  # fmt: skip


def build_env(base: dict[str, str], cfg: RunConfig, oauth_token: str) -> dict[str, str]:
    """An allowlisted environment: the Supervisor token is never passed to Claude."""
    env = {k: base[k] for k in _ENV_PASSTHROUGH if k in base}
    env.update(
        {
            "HOME": str(cfg.home),
            "CLAUDE_CONFIG_DIR": str(cfg.config_dir),
            "CLAUDE_CODE_OAUTH_TOKEN": oauth_token,
            "USE_BUILTIN_RIPGREP": "0",
            "DISABLE_UPDATES": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    env.update({k: v for k, v in cfg.guard_env.items() if k in GUARD_ENV})
    env["PATH"] = "/opt/ops/bin:" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return env


def parse_final_json(text: str) -> dict[str, Any] | None:
    """The last top-level JSON object in free text (fallback when structured output is absent)."""
    depth = 0
    end = -1
    in_string = False
    for index in range(len(text) - 1, -1, -1):
        char = text[index]
        if in_string:
            if char == '"' and (index == 0 or text[index - 1] != "\\"):
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "}":
            if depth == 0:
                end = index
            depth += 1
        elif char == "{":
            depth -= 1
            if depth == 0 and end >= 0:
                try:
                    value = json.loads(text[index : end + 1])
                except json.JSONDecodeError:
                    end = -1
                    continue
                return value if isinstance(value, dict) else None
    return None


def interpret_result(event: dict[str, Any]) -> RunResult:
    subtype = str(event.get("subtype", ""))
    status = {
        "success": "ok",
        "error_max_turns": "max_turns",
        "error_max_budget_usd": "budget",
        "error_during_execution": "error",
    }.get(subtype, "error" if event.get("is_error") else "ok")
    if event.get("is_error") and status == "ok":
        status = "error"
    structured = event.get("structured_output")
    if not isinstance(structured, dict):
        structured = parse_final_json(str(event.get("result", "")))
    denials = [str(d.get("tool_name", d)) for d in event.get("permission_denials", []) if isinstance(d, dict)]
    raw_usage = event.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    tokens_in = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens"))
    limit_reset: float | None = None
    if status != "ok":
        limited, limit_reset = usage_limit(str(event.get("result", "")))
        if limited:
            status = "usage_limited"
    return RunResult(
        status=status,
        cost_usd=float(event.get("total_cost_usd") or 0.0),
        turns=int(event.get("num_turns") or 0),
        structured=structured,
        error="" if status == "ok" else str(event.get("result", subtype))[:500],
        denials=denials,
        tokens_in=tokens_in,
        tokens_out=int(usage.get("output_tokens") or 0),
        tokens_cache_read=int(usage.get("cache_read_input_tokens") or 0),
        duration_s=float(event.get("duration_ms") or 0) / 1000,
        limit_reset=limit_reset,
    )


def _tool_uses(event: dict[str, Any]) -> list[tuple[str, str]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    items = content if isinstance(content, list) else []
    return [
        (str(item.get("id")), str(item.get("name")))
        for item in items
        if isinstance(item, dict) and item.get("type") == "tool_use"
    ]


def tree_rss_mb(root_pid: int, proc: Path = Path("/proc")) -> int:
    """Resident memory of a process and all its descendants."""
    parents: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # The command name may contain spaces; fields resume after the last ')'.
        fields_after = stat.rsplit(")", 1)[-1].split()
        if len(fields_after) > 1:
            parents[int(entry.name)] = int(fields_after[1])
    tree = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in tree and pid not in tree:
                tree.add(pid)
                changed = True
    total_kb = 0
    for pid in tree:
        try:
            for line in (proc / str(pid) / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total_kb += int(line.split()[1])
                    break
        except OSError, ValueError, IndexError:
            continue
    return total_kb // 1024


def host_available_mb(meminfo: Path = Path("/proc/meminfo")) -> int:
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Terminate the whole process group (claude plus its Bash/opsctl children)."""
    # start_new_session=True makes the child a group leader: pgid == pid.
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), 15)
    except TimeoutError:
        _signal_group(proc.pid, signal.SIGKILL)
        await proc.wait()
    # Stragglers that ignored SIGTERM after their parent exited.
    _signal_group(proc.pid, signal.SIGKILL)


async def run_claude(
    cfg: RunConfig,
    prompt: str,
    *,
    oauth_token: str,
    transcript_path: Path,
    redactor: Redactor,
    proc_dir: Path = Path("/proc"),
    meminfo: Path = Path("/proc/meminfo"),
    on_start: Callable[[int], None] | None = None,
) -> RunResult:
    """Run one headless session for any profile. Never raises for run failures; returns a status.

    `on_start` receives the Claude pid as soon as it exists (the RPC server
    binds socket callers to that process tree).
    """
    argv = build_argv(cfg, prompt)
    env = build_env(dict(os.environ), cfg, oauth_token)
    try:
        fresh_state(cfg.config_dir, cfg.home, cfg.run_as)
    except OSError as err:
        return RunResult(status="error", error=f"cannot reset the Claude state directory: {err}")
    drop: dict[str, Any] = {}
    if cfg.run_as is not None:
        drop = {"user": cfg.run_as[0], "group": cfg.run_as[1], "extra_groups": []}
        if not no_new_privs_prefix() and sys.platform == "linux":
            drop["preexec_fn"] = set_no_new_privs
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cfg.cwd),
            limit=32 * 1024 * 1024,
            start_new_session=True,
            **drop,
        )
    except OSError as err:
        return RunResult(status="error", error=f"cannot start claude: {err}")
    if on_start is not None:
        on_start(proc.pid)
    result_event: dict[str, Any] | None = None
    tool_calls: list[tuple[str, str]] = []
    rate_events: list[dict[str, Any]] = []
    hints: list[str] = []
    guard_reason = ""

    async def pump_stdout() -> None:
        nonlocal result_event
        if proc.stdout is None:
            raise RuntimeError("claude stdout not captured")
        with gzip.open(transcript_path, "wt") as out:
            async for raw in proc.stdout:
                line = redactor.redact(raw.decode(errors="replace"))
                out.write(line)
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "assistant":
                    tool_calls.extend(_tool_uses(event))
                elif event.get("type") == "result":
                    result_event = event
                elif event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                    rate_events.append(event["rate_limit_info"])
                elif event.get("type") == "system" and (hint := plan_hint(event)):
                    hints.append(hint)

    async def pump_stderr() -> list[str]:
        if proc.stderr is None:
            raise RuntimeError("claude stderr not captured")
        tail: list[str] = []
        async for raw in proc.stderr:
            tail = [*tail[-19:], redactor.redact(raw.decode(errors="replace").rstrip())]
        return tail

    async def guard() -> None:
        nonlocal guard_reason
        while proc.returncode is None:
            await asyncio.sleep(cfg.poll_s)
            try:
                rss = tree_rss_mb(proc.pid, proc_dir)
                available = host_available_mb(meminfo)
            except OSError:
                continue
            if rss > cfg.rss_limit_mb or available < cfg.host_min_available_mb:
                guard_reason = f"memory guard: claude tree {rss} MB, host available {available} MB"
                _LOGGER.error("memory guard tripped", extra={"rss_mb": rss, "available_mb": available})
                await _stop(proc)
                return

    stdout_task = asyncio.create_task(pump_stdout())
    stderr_task = asyncio.create_task(pump_stderr())
    guard_task = asyncio.create_task(guard())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), cfg.timeout_s)
    except TimeoutError:
        timed_out = True
        await _stop(proc)
    except BaseException:
        await _stop(proc)
        raise
    finally:
        guard_task.cancel()
    # A grandchild holding the pipe open must not hang the daemon.
    _signal_group(proc.pid, signal.SIGKILL)
    stderr_tail: list[str] = []
    try:
        await asyncio.wait_for(stdout_task, PIPE_DRAIN_S)
        stderr_tail = await asyncio.wait_for(stderr_task, PIPE_DRAIN_S)
    except TimeoutError:
        stdout_task.cancel()
        stderr_task.cancel()
        _LOGGER.warning("claude output pipes did not close; abandoned")

    if guard_reason:
        result = RunResult(status="aborted_memory_guard", error=guard_reason)
    elif timed_out:
        result = RunResult(status="timeout", error=f"exceeded {cfg.timeout_s:.0f}s")
    elif result_event is None:
        tail = " | ".join(stderr_tail)[-500:]
        limited, reset = usage_limit(tail)
        result = RunResult(
            status="usage_limited" if limited else "error", error=tail or f"exit {proc.returncode}", limit_reset=reset
        )
    else:
        result = interpret_result(result_event)
    result.transcript = str(transcript_path)
    if rate_events:
        result.rate_first, result.rate_last = rate_events[0], rate_events[-1]
        if result.rate_last.get("status") == "rejected" and result.status != "ok":
            result.status = "usage_limited"
            reset = result.rate_last.get("resetsAt")
            result.limit_reset = float(reset) if isinstance(reset, (int, float)) else result.limit_reset
    result.plan_hint = hints[-1] if hints else None
    decisions, problem = read_decisions(cfg.config_dir / GUARD_DECISIONS)
    raw_denials = (result_event or {}).get("permission_denials")
    denials = [d for d in raw_denials if isinstance(d, dict)] if isinstance(raw_denials, list) else []
    result.guard_anomalies = ([problem] if problem else []) + guard_anomalies(tool_calls, decisions, denials)
    if result.guard_anomalies:
        _LOGGER.error("guard hook check failed", extra={"anomalies": result.guard_anomalies})
    try:
        # Leave nothing behind between runs either.
        fresh_state(cfg.config_dir, cfg.home, cfg.run_as)
    except OSError as err:
        _LOGGER.warning("Claude state directory not cleared after the run", extra={"error": str(err)[:200]})
    _LOGGER.info(
        "claude run finished",
        extra={"status": result.status, "cost_usd": result.cost_usd, "turns": result.turns, "exit": proc.returncode},
    )
    return result
