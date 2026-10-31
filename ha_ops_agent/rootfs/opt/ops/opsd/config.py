"""Paths and add-on options.

Options are grouped one level deep (`agent.mode`, `updates.window`, ...),
as in `config.yaml`.

Every value is checked here as well as by the Supervisor's schema. A value
that fails a check keeps its default and is recorded in `problems`; with
any problem the agent fails closed (no act mode, no automatic updates, no
AI runs) and the daily health check raises an item until it is fixed.

The house profile (`opsd.profile`, `ops/profile.yaml` in the HA config)
rides along as `Options.profile`; a refused profile is a problem too. Where
an option and the profile meet, the properties below combine them: the
option wins for the push target and the reload allowlist, and lists that
only tighten (`need_approval`) are joined.
"""

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from opsd import profile as house_profile
from opsd.guard import Reads
from opsd.profile import HouseProfile
from opsd.selfguard import SELF_SLUG

_LOGGER = logging.getLogger(__name__)

__all__ = ["SELF_SLUG", "Options", "Paths"]
# The unprivileged account the AI runs as (created in the Dockerfile).
OPS_USER = "ops"
# The HA config is mapped below a root-only directory so the ops user cannot
# reach it at all; the AI sees a filtered, read-only export instead.
HA_CONFIG_MOUNT = "/mnt/ha/config"


@dataclass(frozen=True, slots=True)
class Paths:
    """Filesystem layout; every root can be overridden for tests."""

    data: Path
    ha_config: Path
    run: Path
    opt: Path
    export_root: Path = Path("/run/ops-claude")
    socket_dir: Path = Path("/run/ops-sock")
    # Chat and investigation runs write proposed file contents here (owned by `ops`, wiped per run).
    drafts_dir: Path = Path("/run/ops-drafts")

    @classmethod
    def from_env(cls) -> Paths:
        return cls(
            data=Path(os.environ.get("OPS_DATA_DIR", "/data")),
            ha_config=Path(os.environ.get("OPS_HA_CONFIG", HA_CONFIG_MOUNT)),
            run=Path(os.environ.get("OPS_RUN_DIR", "/run/ops")),
            opt=Path(os.environ.get("OPS_OPT_DIR", "/opt/ops")),
            export_root=Path(os.environ.get("OPS_EXPORT_ROOT", "/run/ops-claude")),
            socket_dir=Path(os.environ.get("OPS_SOCKET_DIR", "/run/ops-sock")),
            drafts_dir=Path(os.environ.get("OPS_DRAFTS_DIR", "/run/ops-drafts")),
        )

    @property
    def options_file(self) -> Path:
        return self.data / "options.json"

    @property
    def state_file(self) -> Path:
        return self.data / "state.json"

    @property
    def baseline_file(self) -> Path:
        return self.data / "baseline.json"

    @property
    def history_file(self) -> Path:
        return self.data / "history.jsonl"

    @property
    def notify_state_file(self) -> Path:
        return self.data / "notify.json"

    @property
    def open_items_file(self) -> Path:
        return self.data / "open-items.json"

    @property
    def lock_file(self) -> Path:
        return self.data / "run.lock"

    @property
    def runs_dir(self) -> Path:
        return self.data / "runs"

    @property
    def snapshots_dir(self) -> Path:
        return self.data / "snapshots"

    @property
    def secrets_dir(self) -> Path:
        return self.data / "secrets"

    @property
    def push_cache_file(self) -> Path:
        return self.secrets_dir / "push.json"

    @property
    def oauth_token_file(self) -> Path:
        return self.secrets_dir / "claude_oauth_token"

    @property
    def ssh_key(self) -> Path:
        return self.data / "ssh" / "github_deploy"

    @property
    def known_hosts(self) -> Path:
        """GitHub's published host keys, shipped in the image (no trust on first use)."""
        return self.opt / "ssh" / "known_hosts"

    @property
    def claude_config_dir(self) -> Path:
        """Owned by the ops user; the only part of /data it can reach."""
        return self.data / "claude-ops"

    @property
    def claude_home(self) -> Path:
        return self.data / "claude-ops" / "home"

    @property
    def export_dir(self) -> Path:
        """Read-only copy of the tracked, secret-free config: the AI's cwd."""
        return self.export_root / "config"

    @property
    def socket_path(self) -> Path:
        return self.socket_dir / "opsd.sock"

    @property
    def panel_shell(self) -> Path:
        return self.opt / "panel" / "index.html"

    @property
    def store_file(self) -> Path:
        """SQLite: audit log, tasks, budget ledger, issued actions (root only)."""
        return self.data / "ops.db"

    @property
    def prompts_dir(self) -> Path:
        return self.opt / "prompts"

    @property
    def run_prompt_file(self) -> Path:
        """The composed system prompt of the current run, readable by the ops user."""
        return self.export_root / "system-prompt.md"

    @property
    def custom_components(self) -> Path:
        return self.ha_config / "custom_components"

    @property
    def secrets_yaml(self) -> Path:
        return self.ha_config / "secrets.yaml"

    @property
    def core_config_entries(self) -> Path:
        return self.ha_config / ".storage" / "core.config_entries"


# ------------------------------------------------------------------ options v2

_HHMM = r"([01][0-9]|2[0-3]):[0-5][0-9]"
# The first character stops a model id that looks like a CLI flag.
MODEL_PATTERN = r"[a-z0-9][a-z0-9.-]{1,63}"
UPDATE_ENTITY = r"update\.[a-z0-9_]{1,128}"
ENTITY = r"[a-z_]{1,64}\.[a-z0-9_]{1,128}"
CAPABILITY = r"[a-z][a-z0-9_]{0,40}\.[a-z0-9_.]{1,60}"
RELOADABLE = r"([a-z0-9_]{1,64}(,[a-z0-9_]{1,64}){0,39})?"


@dataclass(frozen=True, slots=True)
class Check:
    """How one option is validated beyond its type (the type is the default's)."""

    choices: tuple[str, ...] = ()
    pattern: str = ""
    low: float | None = None
    high: float | None = None
    secret: bool = False

    def expected(self) -> str:
        if self.choices:
            return "one of " + ", ".join(self.choices)
        if self.low is not None and self.high is not None:
            return f"between {self.low:g} and {self.high:g}"
        return f"in the form {self.pattern}" if self.pattern else "set"

    def accepts(self, value: str | float) -> bool:
        if isinstance(value, str):
            if self.choices and value not in self.choices:
                return False
            return not self.pattern or re.fullmatch(self.pattern, value) is not None
        return (self.low is None or value >= self.low) and (self.high is None or value <= self.high)


def _meta(check: Check) -> dict[str, Check]:
    return {"check": check}


def _one_of(*choices: str) -> dict[str, Check]:
    return _meta(Check(choices=choices))


def _match(pattern: str) -> dict[str, Check]:
    return _meta(Check(pattern=pattern))


def _range(low: float, high: float) -> dict[str, Check]:
    return _meta(Check(low=low, high=high))


_TIME = _match(_HHMM)
_TIME_OR_EMPTY = _match(f"({_HHMM})?")
_MODEL = _match(MODEL_PATTERN)
_MODEL_OR_EMPTY = _match(f"({MODEL_PATTERN})?")
_SECRET = _meta(Check(secret=True))


@dataclass(frozen=True, slots=True)
class AgentOptions:
    mode: str = field(default="observe", metadata=_one_of("observe", "act"))


@dataclass(frozen=True, slots=True)
class LlmOptions:
    # `none`: deterministic alerts only.
    provider: str = field(default="claude", metadata=_one_of("claude", "none"))


@dataclass(frozen=True, slots=True)
class ClaudeOptions:
    token: str = field(default="", repr=False, metadata=_SECRET)
    # Subscription tier for the run-time share estimate: auto (detect, else pro), pro, max5, max20.
    plan: str = field(default="auto", metadata=_one_of("auto", "pro", "max5", "max20"))
    model_standard: str = field(default="claude-sonnet-5-5", metadata=_MODEL)
    # Empty: `model_standard`.
    model_fast: str = field(default="", metadata=_MODEL_OR_EMPTY)


@dataclass(frozen=True, slots=True)
class ChatOptions:
    # How far back the chat prompt reads the activity log.
    activity_days: int = field(default=7, metadata=_range(1, 30))


@dataclass(frozen=True, slots=True)
class NotifyOptions:
    # Empty: the house profile's `notify_service` (no pushes without either).
    service: str = field(default="", metadata=_match(r"(mobile_app_[a-z0-9_]+)?"))
    max_pushes_per_day: int = field(default=3, metadata=_range(0, 20))
    # Quiet hours, local HH:MM (may wrap midnight); empty turns them off.
    quiet_start: str = field(default="22:30", metadata=_TIME_OR_EMPTY)
    quiet_end: str = field(default="07:00", metadata=_TIME_OR_EMPTY)


@dataclass(frozen=True, slots=True)
class HealthOptions:
    check_time: str = field(default="05:30", metadata=_TIME)
    expected_unavailable: tuple[str, ...] = field(default=(), metadata=_match(ENTITY))


@dataclass(frozen=True, slots=True)
class UpdatesOptions:
    window: str = field(default="04:00", metadata=_TIME)
    # The admin's master switch for executing AUTO-tier updates (with `agent.mode: act`).
    auto_install: str = field(default="off", metadata=_one_of("off", "on"))
    # Core patches must be at least this old (days); values below 7 are ignored (tighten only).
    min_age_days: int = field(default=7, metadata=_range(7, 90))
    # A capability-free review of AUTO candidates' release notes (can only hold updates back).
    review: bool = True
    # With the review on: a candidate whose release notes could not be reviewed (AI off,
    # capped, failed) is held for approval instead of installed.
    hold_if_unreviewed: bool = True
    need_approval: tuple[str, ...] = field(default=(), metadata=_match(UPDATE_ENTITY))
    never: tuple[str, ...] = field(default=(), metadata=_match(UPDATE_ENTITY))


@dataclass(frozen=True, slots=True)
class CapabilityOptions:
    # Tighten capability tiers by name (never loosen): see policy.decide.
    require_approval: tuple[str, ...] = field(default=(), metadata=_match(CAPABILITY))
    never: tuple[str, ...] = field(default=(), metadata=_match(CAPABILITY))
    # Integrations the agent may reload (comma-separated domains). opsd.reload's
    # never-reload list wins over this. A string, not a list, so an install upgraded from an
    # older version (no key in its options) keeps starting. Empty: the house profile's
    # `reload.domains` (see `Options.reloadable`).
    reloadable_domains: str = field(default="", metadata=_match(RELOADABLE))

    @property
    def reloadable(self) -> frozenset[str]:
        return frozenset(d.strip() for d in self.reloadable_domains.split(",") if d.strip())


@dataclass(frozen=True, slots=True)
class UsageOptions:
    # The most the agent may use of any subscription window (5-hour, 7-day), in percent.
    max_share_percent: float = field(default=5.0, metadata=_range(0.5, 25))


@dataclass(frozen=True, slots=True)
class LimitsOptions:
    """Advanced settings: all optional in the add-on form."""

    run_max_turns: int = field(default=30, metadata=_range(5, 100))
    run_max_usd: float = field(default=1.5, metadata=_range(0.1, 20))
    log_level: str = field(default="info", metadata=_one_of("debug", "info", "warning", "error"))
    git_push: bool = True


class RuntimeProblems:
    """Problems found after start (`Options.runtime`), shared by every holder of the same `Options`.

    One today: the notify device's user is not an active HA admin (`daemon.refresh_admin`). Any entry fails
    the agent closed exactly like a refused option.
    """

    __slots__ = ("_found",)

    def __init__(self) -> None:
        self._found: dict[str, str] = {}

    def set(self, key: str, problem: str) -> None:
        self._found[key] = problem

    def clear(self, key: str) -> None:
        self._found.pop(key, None)

    def get(self, key: str) -> str | None:
        return self._found.get(key)

    def all(self) -> tuple[str, ...]:
        return tuple(self._found.values())


GROUPS: dict[str, type[Any]] = {
    "agent": AgentOptions,
    "llm": LlmOptions,
    "claude": ClaudeOptions,
    "chat": ChatOptions,
    "notify": NotifyOptions,
    "health": HealthOptions,
    "updates": UpdatesOptions,
    "capabilities": CapabilityOptions,
    "usage": UsageOptions,
    "limits": LimitsOptions,
}


def _parse_value(name: str, default: Any, check: Check, value: Any) -> tuple[Any, str | None]:
    """The checked value, or the default and why `value` was refused (never echoing the value)."""
    if isinstance(default, tuple):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return default, f"{name} must be a list of strings"
        good = tuple(v for v in value if check.accepts(v))
        # Tighten-only lists keep their valid entries; the bad ones are reported.
        return good, None if len(good) == len(value) else f"{name}: every entry must be {check.expected()}"
    if isinstance(default, bool):
        return (value, None) if isinstance(value, bool) else (default, f"{name} must be true or false")
    if isinstance(default, float):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return default, f"{name} must be a number"
        number = float(value)
        return (number, None) if check.accepts(number) else (default, f"{name} must be {check.expected()}")
    if isinstance(default, int):
        if not isinstance(value, int) or isinstance(value, bool):
            return default, f"{name} must be a whole number"
        return (value, None) if check.accepts(value) else (default, f"{name} must be {check.expected()}")
    if not isinstance(value, str):
        return default, f"{name} must be text"
    if check.secret:
        return value, None
    return (value, None) if check.accepts(value) else (default, f"{name} must be {check.expected()}")


def _parse_group(group: str, cls: type[Any], raw: Mapping[str, Any]) -> tuple[Any, list[str]]:
    defaults = cls()
    values: dict[str, Any] = {}
    problems: list[str] = []
    for f in fields(cls):
        if f.name not in raw:
            continue
        name = f"{group}.{f.name}"
        value, problem = _parse_value(name, getattr(defaults, f.name), f.metadata.get("check", Check()), raw[f.name])
        values[f.name] = value
        if problem is not None:
            problems.append(problem)
    return cls(**values), problems


@dataclass(frozen=True, slots=True)
class Options:
    """Add-on options from the Supervisor (`/data/options.json`), grouped as in `config.yaml`."""

    agent: AgentOptions = field(default_factory=AgentOptions)
    llm: LlmOptions = field(default_factory=LlmOptions)
    claude: ClaudeOptions = field(default_factory=ClaudeOptions)
    chat: ChatOptions = field(default_factory=ChatOptions)
    notify: NotifyOptions = field(default_factory=NotifyOptions)
    health: HealthOptions = field(default_factory=HealthOptions)
    updates: UpdatesOptions = field(default_factory=UpdatesOptions)
    capabilities: CapabilityOptions = field(default_factory=CapabilityOptions)
    usage: UsageOptions = field(default_factory=UsageOptions)
    limits: LimitsOptions = field(default_factory=LimitsOptions)
    # Not an option: the house profile (generic when there is none or it was refused).
    profile: HouseProfile = field(default_factory=HouseProfile)
    # Not an option: refused values and a refused profile (any one fails the agent closed).
    problems: tuple[str, ...] = ()
    # Not an option: problems found while running (`RuntimeProblems`); they fail the agent closed too.
    runtime: RuntimeProblems = field(default_factory=RuntimeProblems, compare=False, repr=False)

    @property
    def all_problems(self) -> tuple[str, ...]:
        """The refused values, a refused profile and the runtime problems."""
        return (*self.problems, *self.runtime.all())

    @property
    def valid(self) -> bool:
        return not self.all_problems

    @property
    def acting(self) -> bool:
        """Invalid options never act."""
        return self.valid and self.agent.mode == "act"

    @property
    def auto_updates_on(self) -> bool:
        """Anything but an explicit `on` with valid options is off (fail closed)."""
        return self.valid and self.updates.auto_install == "on"

    @property
    def notify_service(self) -> str:
        """The push target: the option, else the house profile's; empty means no pushes."""
        return self.notify.service or self.profile.notify_service

    @property
    def reloadable(self) -> frozenset[str]:
        """The reload allowlist: the option when set, else the house profile's (the never-lists still win)."""
        return self.capabilities.reloadable or frozenset(self.profile.reload.domains)

    @property
    def need_approval(self) -> frozenset[str]:
        """Updates that always ask: the option and the house profile together."""
        return frozenset(self.updates.need_approval) | frozenset(self.profile.updates.need_approval)

    @property
    def git_on(self) -> bool:
        """Git proposals (and the agent's git reads and commits): only when the house profile enables git."""
        return self.profile.git.enabled

    @property
    def available(self) -> frozenset[str]:
        """What `Capability.requires` may name on this install: `git` and the enabled overlays."""
        return self.profile.overlays | ({"git"} if self.git_on else set())

    @property
    def reads(self) -> Reads:
        """What the AI may read: the base lists plus the house profile's documents."""
        return Reads.with_extra(self.profile.docs.read_extra)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, profile: HouseProfile | None = None,
                     profile_problems: tuple[str, ...] = ()) -> Options:  # fmt: skip
        """Grouped options; unknown keys are ignored. Never raises for bad values."""
        groups: dict[str, dict[str, Any]] = {}
        problems: list[str] = []
        for group in GROUPS:
            value = raw.get(group, {})
            if not isinstance(value, Mapping):
                problems.append(f"{group} must be a group of options")
                value = {}
            groups[group] = dict(value)
        parsed: dict[str, Any] = {}
        for group, group_cls in GROUPS.items():
            parsed[group], found = _parse_group(group, group_cls, groups[group])
            problems.extend(found)
        return replace(cls(**parsed), profile=profile or HouseProfile(),
                       problems=(*problems, *profile_problems))  # fmt: skip

    @classmethod
    def load(cls, paths: Paths) -> Options:
        """Defaults when the file is missing; an unreadable file fails closed like a bad value.

        The house profile is read from the HA config at the same time (generic when absent).
        """
        profile, profile_problems = house_profile.load(paths.ha_config)
        try:
            raw = json.loads(paths.options_file.read_text())
        except FileNotFoundError:
            raw = {}
        except (OSError, ValueError) as err:
            return cls(profile=profile, problems=(f"options.json cannot be read ({type(err).__name__})",
                                                  *profile_problems))  # fmt: skip
        if not isinstance(raw, dict):
            return cls(profile=profile, problems=("options.json is not an object", *profile_problems))
        return cls.from_mapping(raw, profile=profile, profile_problems=profile_problems)

    def log_notes(self) -> None:
        """One log line per refused value."""
        for problem in self.problems:
            _LOGGER.error("invalid add-on option or house profile; act mode, automatic updates and AI stay off",
                          extra={"problem": problem})  # fmt: skip


class CredentialKind(StrEnum):
    """Which provider credential `claude.token` holds."""

    SETUP_TOKEN = "setup_token"  # noqa: S105 - a kind label, not a secret
    API_KEY = "api_key"


# The provider documents its API keys as `sk-ant-api...`. A setup token (`claude setup-token`) shares the
# `sk-ant-` stem (`sk-ant-oat...`), so the stem alone would take every setup token for a key.
API_KEY_PREFIX = "sk-ant-api"


def credential_kind(secret: str) -> CredentialKind | None:
    """The kind of a credential by its documented prefix; None when there is none. Never logs the value."""
    secret = secret.strip()
    if not secret:
        return None
    return CredentialKind.API_KEY if secret.startswith(API_KEY_PREFIX) else CredentialKind.SETUP_TOKEN


def claude_token(options: Options, paths: Paths) -> str:
    """The provider credential (a setup token or an API key): add-on option first, then a file in /data."""
    if options.claude.token.strip():
        return options.claude.token.strip()
    try:
        return paths.oauth_token_file.read_text().strip()
    except FileNotFoundError:
        return ""
