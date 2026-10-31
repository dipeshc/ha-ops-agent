"""PreToolUse hook: a positive allowlist over every tool call Claude makes.

This is defence in depth. The real boundary is the OS (see `privsep.py`):
Claude runs as the unprivileged `ops` user inside a read-only export of the
tracked config and cannot reach the HA config, /data or any token.

On top of that, the hook denies everything except `opsctl <verb> ...` (a
thin client to the daemon), Read/Grep/Glob inside the export, and (proposal runs)
Write of a proposal draft: an allowlisted config path below the drafts
directory, at most 64 KB. Only chat and investigation runs are given the
Write tool at all; the daemon reads drafts without following links. Bad input,
NUL bytes or an internal error deny with exit code 2. A *timed-out* hook
does not block in Claude Code (the call falls through to the permission
rules), so the hook does no I/O beyond `realpath` and one appended line and
finishes in milliseconds; the managed deny rules and the OS boundary cover
the rest. That line (`DECISIONS_FILE` in Claude's state directory: the tool
call id, the tool and the verdict) lets the daemon notice a call the hook
never decided (a timeout or crash) after the run.
"""

import json
import os
import re
import shlex
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from opsd.capabilities import cli_verbs
from opsd.changepaths import DRAFTS_DIR, MAX_FILE_BYTES, ProposalRules, allowed_path
from opsd.selfguard import self_path

# Claude's whole world: the read-only export of tracked, secret-free config.
HA = PurePosixPath(os.environ.get("OPS_EXPORT_DIR", "/run/ops-claude/config"))
ALLOWED_TOOLS = frozenset({"Read", "Grep", "Glob", "Bash", "TodoWrite", "StructuredOutput", "Write"})
DRAFTS = PurePosixPath(os.environ.get("OPS_DRAFTS_DIR", DRAFTS_DIR))
# Generated from the capability registry; the daemon still validates every request.
OPSCTL_VERBS = cli_verbs() | {"--help", "-h", "help"}
# What every install may read; the house profile's documents (`docs:`) add to it, never remove.
READ_DIRS = ("automations", "alerts", "templates", "custom_templates", "custom_components", "blueprints", "scripts",
             "packages")  # fmt: skip
READ_FILES = frozenset({"README.md"})
# Compared lower-cased (`denied`): `Secrets.yaml` is the same file to anyone reading it.
DENY_FILES = frozenset({"secrets.yaml", "secrets.yml", "ip_bans.yaml", "known_devices.yaml"})
# Directories never read, exported or named as a house document, in any letter case.
DENY_DIRS = frozenset({"secrets"})
# The house profile's extra documents for this run (set by the daemon in Claude's environment).
READ_ENV = "OPS_READ_EXTRA"
_READ_ENTRY = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}/){0,3}[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_UNQUOTED_FORBIDDEN = set(";&|`$<>\\(){}*?[]!#\n\r")
_MAX_COMMAND = 2000
DECISIONS_FILE = "guard-decisions.log"


class GuardInputError(ValueError):
    """The hook payload is not what Claude Code documents."""


def _normalize(path: str, ha: PurePosixPath) -> PurePosixPath | None:
    if not path.startswith("/"):
        path = str(ha / path)
    parts: list[str] = []
    for part in PurePosixPath(path).parts[1:]:
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return PurePosixPath("/", *parts)


def relative_config_path(path: str, ha: PurePosixPath = HA) -> PurePosixPath | None:
    """The path relative to the config root after resolving symlinks, or None if outside it."""
    if not path or "\x00" in path:
        return None
    norm = _normalize(path, ha)
    if norm is None:
        return None
    real = PurePosixPath(os.path.realpath(norm))
    root = PurePosixPath(os.path.realpath(ha))
    try:
        rel = real.relative_to(root)
    except ValueError:
        return None
    return rel if rel.parts else None


def denied(part: str) -> bool:
    """A path part (file or directory) that is never read or exported, in any letter case."""
    lowered = part.lower()
    return lowered in DENY_FILES or lowered in DENY_DIRS


def valid_read_entry(entry: str) -> bool:
    """A document the house profile may make readable: a plain relative `.md` file or a top-level directory.

    Never hidden, a secret, git internals, the add-ons, `/data`, `ops/` or a deploy key (`selfguard.self_path`).
    """
    if not _READ_ENTRY.fullmatch(entry) or self_path(entry) or self_path(f"{entry}/"):
        return False
    if any(denied(part) for part in entry.split("/")):
        return False
    return entry.endswith(".md") or ("/" not in entry and "." not in entry)


@dataclass(frozen=True, slots=True)
class Reads:
    """What Claude may read: the base lists plus the house profile's valid documents."""

    files: frozenset[str] = READ_FILES
    dirs: frozenset[str] = frozenset(READ_DIRS)

    @classmethod
    def with_extra(cls, extra: Iterable[str]) -> Reads:
        """Invalid entries are dropped (fail closed: nothing more becomes readable)."""
        good = [e for e in extra if e and valid_read_entry(e)]
        files = {e for e in good if e.endswith(".md")}
        dirs = {e for e in good if not e.endswith(".md")}
        return cls(READ_FILES | files, frozenset(READ_DIRS) | dirs)


# In the hook: the run's documents from the daemon; in the daemon itself: the base lists (it passes its own).
READS = Reads.with_extra(os.environ.get(READ_ENV, "").split(","))
PROPOSALS = ProposalRules.from_env(os.environ)


def readable_rel(rel: PurePosixPath, reads: Reads | None = None) -> bool:
    reads = reads or READS
    if any(part.startswith(".") for part in rel.parts) or any(denied(p) for p in rel.parts):
        return False
    if len(rel.parts) == 1:
        name = rel.parts[0]
        return name in reads.files or name.endswith((".yaml", ".yml"))
    return rel.parts[0] in reads.dirs or rel.as_posix() in reads.files


def readable(path: str, ha: PurePosixPath = HA, reads: Reads | None = None) -> bool:
    """Tracked config files only; never secrets, storage, databases or the agent's data."""
    rel = relative_config_path(path, ha)
    return rel is not None and readable_rel(rel, reads)


def searchable(path: str, ha: PurePosixPath = HA, reads: Reads | None = None) -> bool:
    """Grep/Glob roots and git pathspecs: an allowed subtree or an allowed file."""
    reads = reads or READS
    rel = relative_config_path(path, ha)
    if rel is None:
        return False
    return readable_rel(rel, reads) or (len(rel.parts) == 1 and rel.parts[0] in reads.dirs)


def draft_problem(path: str, content: str, drafts: PurePosixPath = DRAFTS,
                  rules: ProposalRules | None = None) -> str | None:  # fmt: skip
    """A Write is allowed only for an allowlisted config path below the drafts directory."""
    rules = rules or PROPOSALS
    rel = relative_config_path(path, drafts)
    if rel is None or not path.startswith(f"{drafts}/"):
        return f"Write is only for proposal drafts: {drafts}/<config path>"
    if not allowed_path(str(rel), rules) or str(rel) != path[len(str(drafts)) + 1 :]:
        return f"{rel} cannot be proposed ({rules.describe()})"
    if len(content.encode("utf-8", errors="replace")) > MAX_FILE_BYTES:
        return f"a draft is at most {MAX_FILE_BYTES // 1024} KB"
    return None


def check_shell_syntax(command: str) -> str | None:
    """Reject shell features outside single quotes; double quotes must be inert."""
    quote: str | None = None
    for char in command:
        if quote == "'":
            if char == "'":
                quote = None
            elif char in "\n\r":
                return "newline in argument"
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char in "$`\\\n\r!":
                return f"'{char}' inside double quotes"
            continue
        if char in "'\"":
            quote = char
        elif char in _UNQUOTED_FORBIDDEN:
            return f"shell metacharacter {char!r} (run one plain command at a time)"
    if quote:
        return "unterminated quote"
    return None


def check_bash(command: str) -> str | None:
    if "\x00" in command:
        return "NUL byte in command"
    if len(command) > _MAX_COMMAND:
        return "command too long"
    problem = check_shell_syntax(command)
    if problem:
        return problem
    try:
        argv = shlex.split(command)
    except ValueError as err:
        return f"unparseable command: {err}"
    if not argv:
        return "empty command"
    if any(arg.startswith("~") for arg in argv):
        return "home-directory paths are not allowed"
    if argv[0] not in ("opsctl", "/opt/ops/bin/opsctl"):
        return "only `opsctl <verb>` commands are available (git history: opsctl git-log|git-diff|git-show)"
    if len(argv) < 2 or argv[1] not in OPSCTL_VERBS:
        return "unknown opsctl verb (run `opsctl --help`)"
    return None


def _string(data: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise GuardInputError(f"{key} must be a string")
    if "\x00" in value:
        raise GuardInputError(f"NUL byte in {key}")
    return value


def decide(event: Any) -> str | None:
    """Return a denial reason, or None to defer to the normal permission rules."""
    if not isinstance(event, dict):
        raise GuardInputError("hook input is not an object")
    tool = event.get("tool_name")
    data = event.get("tool_input")
    if not isinstance(tool, str):
        raise GuardInputError("tool_name must be a string")
    if tool not in ALLOWED_TOOLS:
        return f"tool {tool} is not available to the ops agent"
    if not isinstance(data, dict):
        raise GuardInputError("tool_input must be an object")
    if tool == "Bash":
        return check_bash(_string(data, "command") or "")
    if tool == "Write":
        return draft_problem(_string(data, "file_path") or "", _string(data, "content") or "")
    if tool == "Read":
        path = _string(data, "file_path") or ""
        return None if readable(path) else f"reading {path} is not allowed"
    if tool == "Grep":
        grep_path = _string(data, "path", required=False)
        for key in ("glob", "type"):
            _string(data, key, required=False)
        if not grep_path:
            return "Grep needs an explicit path under automations/, alerts/, docs/, custom_components/ or a config file"
        return None if searchable(grep_path) else f"grep in {grep_path} is not allowed"
    if tool == "Glob":
        pattern = _string(data, "pattern") or ""
        path = _string(data, "path", required=False) or str(HA)
        if ".." in pattern or pattern.startswith(("/", "~")):
            return "glob pattern must be relative and stay inside its root"
        if path.rstrip("/") == str(HA):
            return None
        return None if searchable(path) else f"glob in {path} is not allowed"
    return None


def _deny(reason: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def _record(event: Any, verdict: str) -> None:
    """One line per decision for the daemon's after-run check; never affects the decision."""
    state = os.environ.get("CLAUDE_CONFIG_DIR")
    if not state:
        return
    call = event.get("tool_use_id") if isinstance(event, dict) else None
    tool = event.get("tool_name") if isinstance(event, dict) else None
    line = "\t".join(str(x)[:100].replace("\t", " ").replace("\n", " ") for x in (call or "-", tool or "-", verdict))
    try:
        fd = os.open(os.path.join(state, DECISIONS_FILE), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)
    except OSError:
        return


def main() -> int:
    event: Any = None
    try:
        event = json.load(sys.stdin)
        reason = decide(event)
    except Exception as err:  # noqa: BLE001 - fail closed on anything unexpected
        message = f"guard-hook rejected the call: {type(err).__name__}"
        _deny(message)
        print(message, file=sys.stderr)
        _record(event, "deny")
        return 2
    if reason is None:
        _record(event, "allow")
        return 0
    _deny(reason)
    _record(event, "deny")
    return 0


if __name__ == "__main__":
    sys.exit(main())
