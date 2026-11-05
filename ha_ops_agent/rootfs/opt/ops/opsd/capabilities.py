"""Capability registry: every power the AI can request, declared once (spec capabilities.md K1-K6).

The RPC schema, the `opsctl` parser, the guard hook's verb list and the prompts' capability tables are generated
from these rows. Executors are `"opsd.module:function"` references resolved only in the daemon, so the guard hook
and `opsctl` never load daemon code. `internal=True` rows are the daemon's own powers, never reachable by the AI.
"""

import argparse
import importlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from opsd.policy import Effect, Origin, PolicyTier

MAX_REQUEST_BYTES = 8192

NAME_RE = re.compile(r"[a-z0-9_.]{1,100}")
SLUG_RE = re.compile(r"[a-z0-9_]{1,64}")
IDENT_RE = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
TEXT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,300}")
TITLE_RE = re.compile(r"[^\x00-\x1f\x7f]{1,60}")
# A reason class: any short word passes the schema, and the executor decides what it means (`effects`).
REASON_RE = re.compile(r"[a-z_]{0,32}")
# Multi-line text: newlines and tabs, no other control characters.
MULTILINE_RE = re.compile(r"[^\x00-\x08\x0b-\x1f\x7f]{1,4000}")
ITEM_KEY_RE = re.compile(r"[A-Za-z0-9_.:@/+-]{1,200}")
CHANGE_PATHS_RE = re.compile(r"[a-z0-9_./-]{1,300}(?:,[a-z0-9_./-]{1,300}){0,2}")
SUBJECT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,100}")
FACT_RE = re.compile(r"[^\x00-\x1f\x7f]{1,500}")
UPDATE_ENTITY_RE = re.compile(r"update\.[a-z0-9_]{1,128}")
SUPPRESS_VERIFIER_RE = re.compile(r"[a-z_]{1,40}")
SUPPRESS_VALUE_RE = re.compile(r"[a-z0-9_.]{1,140}")
# Hunk ids of a fork's own changes (opsd.forkrebase.KEEP_RE), or `none`.
FORK_KEEP_RE = re.compile(r"none|[A-Za-z0-9_.-]{1,64}#[1-9][0-9]{0,2}(?:,[A-Za-z0-9_.-]{1,64}#[1-9][0-9]{0,2}){0,39}")
AT_RE = re.compile(r"\+\d{1,4}[mhd]|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
_VERB_RE = re.compile(r"[a-z][a-z0-9-]{0,30}")
_CAP_NAME_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_REF_RE = re.compile(r"opsd(?:\.[a-z_]+)+:[a-z_]+")
# What `Capability.requires` may name: git, or a house overlay (`profile.OVERLAYS`).
REQUIREMENTS = frozenset({"git", "vacuums", "localpatches", "suppress"})

ALL_ORIGINS = frozenset(Origin)


class RequestError(ValueError):
    """The request does not match the schema."""


class CapabilityError(ValueError):
    """A validator or executor refused the request; the message is safe to show the AI."""


@dataclass(frozen=True, slots=True)
class Arg:
    """One typed argument. Off-schema values are refused, integers are clamped."""

    kind: type
    default: Any = None
    choices: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    minimum: int = 0
    maximum: int = 0
    # Two-character `\n` sequences become newlines (the guard forbids real
    # newlines in a shell argument, so multi-line text arrives escaped).
    unescape_newlines: bool = False
    # CLI shape: a positional argument, or a flag (default `--<name>`).
    positional: bool = False
    flag: str | None = None
    help: str = ""


@dataclass(frozen=True, slots=True)
class Quota:
    """Successful executions allowed; 0 means no limit beyond the run's request budget."""

    per_run: int = 0
    per_day: int = 0


@dataclass(frozen=True, slots=True)
class Capability:
    name: str
    verb: str
    summary: str
    # `opsd.module:function`; None for an internal row (the daemon calls its own code, never through a ref).
    executor: str | None = None
    args: Mapping[str, Arg] = field(default_factory=dict)
    tier: PolicyTier = PolicyTier.AUTO
    effects: frozenset[Effect] = frozenset({Effect.READ})
    origins: frozenset[Origin] = ALL_ORIGINS
    quota: Quota = Quota()
    # Runs before policy; may clamp arguments or refuse (CapabilityError).
    validator: str | None = None
    # `(ExecContext, Args) -> Resolution`; refines the tier for these arguments.
    tier_resolver: str | None = None
    # Daemon-only: never reachable over the socket (see the module docstring).
    internal: bool = False
    # A refusal from the executor (CapabilityError, raised before any side effect) gives the
    # daily quota unit back, so refused calls cannot use up the day.
    refund_refused: bool = False
    # Needs git proposals or a house overlay on this install (see the module docstring).
    requires: frozenset[str] = frozenset()
    # `(arg, value)`: a call with that argument value takes no quota unit (its executor bounds it itself).
    quota_free: tuple[str, str] | None = None
    # What the panel shows while a run uses it (required for every AI-reachable row), and, for a read,
    # its name in a reply's Sources footer. Fixed text, never model text or arguments.
    step: str = ""
    source: str = ""

    @property
    def read_only(self) -> bool:
        return self.effects == frozenset({Effect.READ})


@dataclass(frozen=True, slots=True)
class Args:
    """Validated arguments with typed accessors (no argparse.Namespace on the daemon side)."""

    values: Mapping[str, Any]

    def flag(self, name: str) -> bool:
        value = self.values.get(name)
        return value is True

    def number(self, name: str) -> int:
        value = self.values[name]
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{name} is not an integer")
        return value

    def text(self, name: str) -> str:
        value = self.values[name]
        if not isinstance(value, str):
            raise TypeError(f"{name} is not a string")
        return value

    def opt_text(self, name: str) -> str | None:
        value = self.values.get(name)
        return value if isinstance(value, str) else None

    def replace(self, **changes: Any) -> Args:
        return Args({**self.values, **changes})

    def to_json(self) -> dict[str, Any]:
        return dict(self.values)


REGISTRY: tuple[Capability, ...] = (
    Capability(
        "agent.status",
        "status",
        "agent state and the last runs",
        "opsd.queries:cmd_status",
        step="Checking the agent's status",
        source="agent status",
    ),
    Capability(
        "agent.baseline",
        "baseline",
        "accepted baseline and active acks",
        "opsd.queries:cmd_baseline",
        step="Reading the health baseline",
        source="health baseline",
    ),
    Capability(
        "health.snapshot",
        "health",
        "fresh health snapshot (or --diff against the baseline)",
        "opsd.queries:cmd_health",
        {"diff": Arg(bool, False, help="only the items that are new since the baseline")},
        step="Reading the latest health check",
        source="health check",
    ),
    Capability(
        "ha.repairs",
        "repairs",
        "Core repairs and Supervisor resolution issues",
        "opsd.queries:cmd_repairs",
        step="Checking repairs",
        source="repairs",
    ),
    Capability(
        "ha.entities",
        "entities",
        "entity counts, unavailable entities, or one entity's state",
        "opsd.queries:cmd_entities",
        {
            "unavailable": Arg(bool, False),
            "platform": Arg(str, None, pattern=NAME_RE),
            "entity": Arg(str, None, pattern=NAME_RE),
            "limit": Arg(int, 200, minimum=1, maximum=1000),
        },
        step="Reading entity states",
        source="entity states",
    ),
    Capability(
        "ha.integrations",
        "integrations",
        "custom integration health",
        "opsd.queries:cmd_integrations",
        step="Checking integrations",
        source="custom integrations",
    ),
    Capability(
        "ha.entries",
        "entries",
        "config entries with their non-secret settings",
        "opsd.queries:cmd_entries",
        {"failed": Arg(bool, False), "domain": Arg(str, None, pattern=NAME_RE)},
        step="Checking integrations",
        source="config entries",
    ),
    Capability(
        "ha.updates",
        "updates",
        "pending updates with their policy tier",
        "opsd.queries:cmd_updates",
        step="Checking updates",
        source="pending updates",
    ),
    Capability(
        "ha.backups",
        "backups",
        "backups, newest first",
        "opsd.queries:cmd_backups",
        step="Checking backups",
        source="backups",
    ),
    Capability(
        "ha.logs",
        "logs",
        "journal logs",
        "opsd.queries:cmd_logs",
        {
            "source": Arg(str, "core", choices=("core", "supervisor", "host", "addon"), positional=True),
            "lines": Arg(int, 200, minimum=2, maximum=1000),
            "grep": Arg(str, None, pattern=TEXT_RE, help="case-insensitive literal text filter"),
            "identifier": Arg(str, None, pattern=IDENT_RE, help="host journal identifier, e.g. kernel"),
            "slug": Arg(str, None, pattern=SLUG_RE, help="add-on slug for addon logs"),
        },
        step="Checking the logs",
        source="logs",
    ),
    Capability(
        "ha.history",
        "history",
        "one entity's past states: raw changes, or hourly/daily mean/min/max (trends)",
        "opsd.history:cmd_history",
        {
            "entity": Arg(
                str,
                None,
                pattern=re.compile(r"[a-z_][a-z0-9_]{0,63}\.[a-z0-9_]{1,128}"),
                positional=True,
                help="entity_id, e.g. sensor.outdoor_temperature",
            ),
            "window": Arg(str, "24h", pattern=re.compile(r"[1-9][0-9]{0,2}[hd]"), help="how far back: 1h to 31d"),
            "mode": Arg(
                str,
                "raw",
                choices=("raw", "hourly", "daily"),
                help="raw state changes, or mean/min/max per hour or day",
            ),
        },
        validator="opsd.history:validate_history",
        step="Reading the history",
        source="history",
    ),
    Capability(
        "ha.system",
        "system",
        "Core/Supervisor/OS/host versions and resource use",
        "opsd.queries:cmd_system",
        step="Checking the system",
        source="system info",
    ),
    # The one read that reaches the internet: the daemon fetches Home Assistant Core's own files for the installed
    # Core version from GitHub (opsd.coresource). Not in triage's grant (opsd.runner NETWORK_READS).
    Capability(
        "ha.core_compare",
        "core-compare",
        "compare a custom integration with Home Assistant Core's integration of the same domain at the installed"
        " Core version: a summary and a unified diff per file (fetched by the daemon from GitHub)",
        "opsd.coresource:cmd_core_compare",
        {
            "domain": Arg(str, None, pattern=SLUG_RE, positional=True, help="custom integration domain, e.g. acme"),
            "fork": Arg(
                bool,
                False,
                help="also find the Core release the fork was copied from and number its own changes for"
                " propose-rebase --keep",
            ),
        },
        quota=Quota(per_run=3, per_day=12),
        validator="opsd.coresource:validate_compare",
        refund_refused=True,
        step="Comparing with Home Assistant Core",
        source="Core comparison",
    ),
    # No arguments. It reads only the vacuums' registry options and
    # their cached segments (websocket reads; no service call), see opsd.house.vacuums.
    Capability(
        "vacuum.rooms",
        "vacuum-rooms",
        "check the vacuums' rooms against HA's vacuum area mapping (cached map): in sync, or what drifted",
        "opsd.house.vacuums:cmd_rooms",
        requires=frozenset({"vacuums"}),
        step="Checking the vacuum rooms",
        source="vacuum rooms",
    ),
    # The agent maintains HA and reloads an integration when it judges it needed. The domain is
    # the only input; opsd.reload resolves the entries and holds every limit: the admin's
    # allowlist (capabilities.reloadable_domains), a never-reload
    # list that wins over it (locks, alarm, access, radios, plumbing), any domain that provides
    # a lock, alarm panel or non-blind cover, 10 minutes per domain, no vacuum mid-clean.
    # Report-only in observe mode.
    Capability(
        "integration.reload",
        "integration-reload",
        "reload one integration (all its config entries) and wait up to 60 s for its entities; never locks,"
        " the alarm, access or core plumbing; once per domain per 10 minutes",
        "opsd.reload:exec_reload",
        {"domain": Arg(str, None, pattern=re.compile(r"[a-z0-9_]{1,64}"), positional=True, help="e.g. cast")},
        effects=frozenset({Effect.RELOAD}),
        origins=frozenset({Origin.AGENT_SELF, Origin.ADMIN_CHAT}),
        quota=Quota(per_run=2, per_day=6),
        validator="opsd.reload:validate_reload",
        refund_refused=True,
        step="Reloading an integration",
    ),
    Capability(
        "git.status",
        "git-status",
        "tracked files changed in the working tree",
        "opsd.queries:cmd_git_status",
        requires=frozenset({"git"}),
        step="Looking at config history",
        source="git status",
    ),
    Capability(
        "git.log",
        "git-log",
        "recent commits, optionally for one tracked path",
        "opsd.queries:cmd_git_log",
        {"n": Arg(int, 10, minimum=1, maximum=50, flag="-n"), "path": Arg(str, None, pattern=TEXT_RE)},
        requires=frozenset({"git"}),
        step="Looking at config history",
        source="config history",
    ),
    Capability(
        "git.diff",
        "git-diff",
        "diff of readable files: REV (vs the index) or REV..REV",
        "opsd.queries:cmd_git_diff",
        {"range": Arg(str, "HEAD", pattern=TEXT_RE, positional=True), "path": Arg(str, None, pattern=TEXT_RE)},
        requires=frozenset({"git"}),
        step="Looking at config history",
        source="config history",
    ),
    Capability(
        "git.show",
        "git-show",
        "a commit (readable files only) or REV:PATH",
        "opsd.queries:cmd_git_show",
        {"spec": Arg(str, "HEAD", pattern=TEXT_RE, positional=True)},
        requires=frozenset({"git"}),
        step="Looking at config history",
        source="config history",
    ),
    # Reminders and follow-ups. `schedule` effects only, so a chat
    # that Core-resident code might have forged can use them (policy.CHAT_EFFECTS).
    Capability(
        "task.create",
        "remind",
        "schedule a reminder push for the admin, or a follow-up check by the agent",
        "opsd.tasks:exec_create",
        {
            "kind": Arg(str, "reminder", choices=("reminder", "followup")),
            "at": Arg(str, None, pattern=AT_RE, help="local YYYY-MM-DDTHH:MM, or +30m / +2h / +3d"),
            "title": Arg(str, None, pattern=TEXT_RE, help="what to remind about (shown on the push)"),
            "note": Arg(str, None, pattern=MULTILINE_RE, unescape_newlines=True, help="context for a follow-up"),
            "reply": Arg(bool, False, help="let the admin reply to the reminder push"),
            "reason": Arg(
                str,
                "",
                pattern=REASON_RE,
                help="the agent's own tasks: needs_action, physical_task or security to push when due,"
                " note to keep it as a History note",
            ),
        },
        effects=frozenset({Effect.SCHEDULE}),
        quota=Quota(per_run=5, per_day=30),
        validator="opsd.tasks:validate_create",
        step="Scheduling a reminder",
    ),
    Capability(
        "task.list",
        "tasks",
        "open reminders and follow-ups",
        "opsd.tasks:exec_list",
        step="Checking reminders",
        source="reminders",
    ),
    Capability(
        "task.cancel",
        "cancel",
        "cancel a pending reminder or follow-up by id (system tasks cannot be cancelled)",
        "opsd.tasks:exec_cancel",
        {"id": Arg(int, 0, minimum=0, maximum=10**9, help="task id from opsctl tasks")},
        effects=frozenset({Effect.SCHEDULE}),
        quota=Quota(per_run=10, per_day=30),
        step="Cancelling a reminder",
    ),
    # Effects: they only work inside an agent run, within these quotas.
    Capability(
        "notify.admin",
        "notify",
        "send the one push this run may send to the admin's phone, only when the admin must act or decide",
        "opsd.effects:exec_notify",
        {
            "title": Arg(str, None, pattern=TITLE_RE),
            "message": Arg(str, None, pattern=MULTILINE_RE, unescape_newlines=True, help="900 characters at most"),
            "urgent": Arg(bool, False, help="only honoured when a hand-off item is critical"),
            "reason": Arg(
                str,
                "",
                pattern=REASON_RE,
                help="needs_action, physical_task or security for a push; note for a History note only",
            ),
        },
        effects=frozenset({Effect.NOTIFY}),
        origins=frozenset({Origin.AGENT_SELF}),
        quota=Quota(per_run=1, per_day=9),
        validator="opsd.effects:validate_notify",
        # A History note is no push: it never uses the push quota (the executor allows a few per run).
        quota_free=("reason", "note"),
        step="Sending you a push",
    ),
    Capability(
        "update.plan",
        "update-plan",
        "the latest update plan (tiers, reasons, reviews), approvals, holds, results with their OS evidence, and"
        " rollbacks",
        "opsd.updates:cmd_plan",
        step="Reading the update plan",
        source="update plan",
    ),
    # A chat may start a longer investigation in its own thread: background
    # follow-up runs, each a normal capped run, that report back into the thread.
    Capability(
        "task.investigate",
        "investigate",
        "look into something over up to 3 background runs that report back in this conversation",
        "opsd.tasks:exec_investigate",
        {
            "title": Arg(str, None, pattern=TEXT_RE, help="what is being investigated (shown on its item)"),
            "brief": Arg(str, None, pattern=MULTILINE_RE, unescape_newlines=True, help="what to find out and how"),
            "steps": Arg(int, 2, minimum=1, maximum=3, help="most background runs (1-3)"),
        },
        effects=frozenset({Effect.SCHEDULE}),
        origins=frozenset({Origin.ADMIN, Origin.ADMIN_CHAT}),
        quota=Quota(per_run=1, per_day=4),
        validator="opsd.tasks:validate_investigate",
        step="Starting an investigation",
    ),
    # Config changes as git proposals. Proposing grants nothing; applying and reverting
    # are daemon-internal and run only after the admin approves them in the panel (opsd.changes).
    Capability(
        "change.propose",
        "propose-change",
        "propose a config change for the admin to approve: write each file's full new contents to"
        " /run/ops-drafts/<path> first, then name the paths",
        "opsd.changes:exec_propose",
        {
            "paths": Arg(str, None, pattern=CHANGE_PATHS_RE, help="1-3 comma-separated paths, e.g. alerts/x.yaml"),
            "subject": Arg(str, None, pattern=SUBJECT_RE, help="conventional commit subject: 'fix(alerts): ...'"),
            "why": Arg(str, None, pattern=MULTILINE_RE, unescape_newlines=True, help="why it is needed"),
            "effect": Arg(str, None, pattern=TEXT_RE, help="what will behave differently"),
        },
        effects=frozenset({Effect.PROPOSE}),
        origins=frozenset({Origin.ADMIN, Origin.ADMIN_CHAT}),
        quota=Quota(per_run=3, per_day=10),
        validator="opsd.changes:validate_propose",
        requires=frozenset({"git"}),
        step="Proposing a config change",
    ),
    Capability(
        "facts.propose",
        "propose-fact",
        "propose adding one house fact to the house facts document (the admin approves it)",
        "opsd.changes:exec_fact",
        {"text": Arg(str, None, pattern=FACT_RE, help="the fact, one line"), "why": Arg(str, None, pattern=TEXT_RE)},
        effects=frozenset({Effect.PROPOSE}),
        origins=frozenset({Origin.ADMIN, Origin.ADMIN_CHAT}),
        quota=Quota(per_run=2, per_day=5),
        validator="opsd.changes:validate_fact",
        requires=frozenset({"git"}),
        step="Proposing a house fact",
    ),
    # A known-false-positive rule. The verifier is chosen by name from a fixed set in
    # code (opsd.house.suppress), never written by the model; the admin approves the git proposal.
    Capability(
        "suppress.propose",
        "propose-suppression",
        "propose suppressing one open health item while a fixed check proves it harmless (the admin approves)",
        "opsd.house.suppress:exec_suppression",
        {
            "key": Arg(str, None, pattern=ITEM_KEY_RE, help="the exact open item key"),
            "verifier": Arg(
                str,
                None,
                pattern=SUPPRESS_VERIFIER_RE,
                help="entity_available|integration_loaded|log_count_at_most",
            ),
            "value": Arg(str, None, pattern=SUPPRESS_VALUE_RE, help="entity id, domain or count"),
            "reason": Arg(str, None, pattern=TEXT_RE, help="why the item is a false positive here"),
        },
        effects=frozenset({Effect.PROPOSE}),
        origins=frozenset({Origin.ADMIN, Origin.ADMIN_CHAT, Origin.AGENT_SELF}),
        quota=Quota(per_run=1, per_day=3),
        validator="opsd.house.suppress:validate_suppression",
        requires=frozenset({"git", "suppress"}),
        step="Proposing a suppression",
    ),
    # A fork rebase: the AI names the fork, the reason and which of the fork's own changes to keep (hunk ids from
    # `core-compare --fork`); it never supplies file content. The daemon builds the files from Core's source and the
    # fork (opsd.forkrebase) and records a proposal; applying it is `fork.apply`, inside the update pipeline only.
    Capability(
        "fork.propose",
        "propose-rebase",
        "propose rebasing a custom integration that forks a Core one onto the installed Core version, keeping only"
        " the fork's own changes you name (the admin approves it; it is applied in the update window)",
        "opsd.forkrebase:exec_propose",
        {
            "domain": Arg(str, None, pattern=SLUG_RE, positional=True, help="the fork's domain, e.g. acme_fan"),
            "keep": Arg(
                str, None, pattern=FORK_KEEP_RE, help="hunk ids from core-compare --fork (fan.py#2,...) or none"
            ),
            "reason": Arg(str, None, pattern=TEXT_RE, help="why the fork needs the rebase"),
        },
        effects=frozenset({Effect.PROPOSE}),
        origins=frozenset({Origin.ADMIN, Origin.ADMIN_CHAT, Origin.AGENT_SELF}),
        quota=Quota(per_run=1, per_day=3),
        validator="opsd.forkrebase:validate_propose",
        refund_refused=True,
        step="Proposing a fork rebase",
    ),
    Capability(
        "change.apply",
        "change-apply",
        "write an approved change, check the config, reload, commit and push",
        tier=PolicyTier.APPROVE,
        effects=frozenset({Effect.MUTATE_HA, Effect.MUTATE_GIT}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
    Capability(
        "change.revert",
        "change-revert",
        "write back what an applied change replaced, check, reload, commit and push",
        tier=PolicyTier.APPROVE,
        effects=frozenset({Effect.MUTATE_HA, Effect.MUTATE_GIT}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
    # Writes an approved fork rebase's files and restarts Core, as a job of the update pipeline (opsd.updates,
    # opsd.updexec): approval-only and daemon-internal.
    Capability(
        "fork.apply",
        "fork-apply",
        "write an approved fork rebase into custom_components and restart Core, inside the update pipeline",
        tier=PolicyTier.APPROVE,
        effects=frozenset({Effect.MUTATE_HA}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
    # Update executors: daemon-internal, never offered to the AI.
    Capability(
        "backup.create",
        "backup-create",
        "native full backup (Supervisor), verified full with Home Assistant included",
        tier=PolicyTier.AUTO_NOTIFY,
        effects=frozenset({Effect.MUTATE_HA}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
    Capability(
        "update.install",
        "update-install",
        "install one planned update through the native mechanism",
        tier=PolicyTier.AUTO_NOTIFY,
        effects=frozenset({Effect.MUTATE_HA}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
    Capability(
        "update.rollback",
        "update-rollback",
        "return one update to its previous version (version pin or add-on restore)",
        tier=PolicyTier.AUTO_NOTIFY,
        effects=frozenset({Effect.MUTATE_HA}),
        origins=frozenset({Origin.AGENT_SELF}),
        internal=True,
    ),
)


def _check_registry(rows: Iterable[Capability]) -> dict[str, Capability]:
    by_name: dict[str, Capability] = {}
    verbs: set[str] = set()
    for cap in rows:
        if not _CAP_NAME_RE.fullmatch(cap.name) or not _VERB_RE.fullmatch(cap.verb):
            raise ValueError(f"bad capability name or verb: {cap.name} / {cap.verb}")
        if cap.name in by_name or cap.verb in verbs or cap.verb == "help":
            raise ValueError(f"duplicate capability {cap.name} / {cap.verb}")
        for ref in (cap.executor, cap.validator, cap.tier_resolver):
            if ref is not None and not _REF_RE.fullmatch(ref):
                raise ValueError(f"bad reference {ref!r} in {cap.name}")
        for name, arg in cap.args.items():
            if arg.kind not in (bool, int, str) or not re.fullmatch(r"[a-z]{1,20}", name):
                raise ValueError(f"bad argument {name} in {cap.name}")
            if arg.kind is bool and arg.positional:
                raise ValueError(f"boolean {name} cannot be positional")
        if not cap.requires <= REQUIREMENTS:
            raise ValueError(f"unknown requirement in {cap.name}: {sorted(cap.requires - REQUIREMENTS)}")
        if cap.internal and (cap.read_only or cap.origins != frozenset({Origin.AGENT_SELF})):
            raise ValueError(f"internal capability {cap.name} must be a daemon (agent-self) mutation")
        if cap.internal != (cap.executor is None):
            raise ValueError(f"capability {cap.name}: an executor exactly when it is reachable")
        if not cap.internal and not cap.step:
            raise ValueError(f"capability {cap.name} has no step label")
        by_name[cap.name] = cap
        verbs.add(cap.verb)
    return by_name


CAPABILITIES: dict[str, Capability] = _check_registry(REGISTRY)
BY_VERB: dict[str, Capability] = {c.verb: c for c in REGISTRY}


def reachable(registry: Mapping[str, Capability] = CAPABILITIES) -> list[Capability]:
    """What the AI can reach (socket, CLI, guard, prompts): every row that is not daemon-internal."""
    return [c for c in registry.values() if not c.internal]


def internal(registry: Mapping[str, Capability] = CAPABILITIES) -> dict[str, Capability]:
    """The daemon's own powers (never on the socket)."""
    return {c.name: c for c in registry.values() if c.internal}


def cli_verbs(registry: Mapping[str, Capability] = CAPABILITIES) -> frozenset[str]:
    """What the guard hook lets the AI run as `opsctl <verb>`."""
    return frozenset(c.verb for c in reachable(registry))


def schema(registry: Mapping[str, Capability] = CAPABILITIES) -> dict[str, Mapping[str, Arg]]:
    return {c.verb: c.args for c in reachable(registry)}


def resolve_ref(ref: str) -> Callable[..., Any]:
    """Import `opsd.module:function` (daemon side only)."""
    if not _REF_RE.fullmatch(ref):
        raise ValueError(f"bad reference {ref!r}")
    module_name, _, attr = ref.partition(":")
    target = getattr(importlib.import_module(module_name), attr)
    if not callable(target):
        raise TypeError(f"{ref} is not callable")
    return target  # type: ignore[no-any-return]


def _value(name: str, spec: Arg, value: Any) -> Any:
    if value is None:
        return spec.default
    if spec.kind is bool:
        if not isinstance(value, bool):
            raise RequestError(f"{name} must be a boolean")
        return value
    if spec.kind is int:
        if not isinstance(value, int) or isinstance(value, bool):
            raise RequestError(f"{name} must be an integer")
        return max(spec.minimum, min(spec.maximum, value))
    if not isinstance(value, str):
        raise RequestError(f"{name} must be a string")
    if spec.unescape_newlines:
        value = value.replace("\\n", "\n")
    if spec.choices and value not in spec.choices:
        raise RequestError(f"{name} must be one of {', '.join(spec.choices)}")
    if spec.pattern is not None and not spec.pattern.fullmatch(value):
        raise RequestError(f"{name} has invalid characters or length")
    return value


def request_verb(raw: bytes) -> str:
    """A loggable verb from a raw request, whatever its validity."""
    try:
        verb = json.loads(raw).get("verb")
    except AttributeError, ValueError, UnicodeDecodeError:
        return "?"
    return verb if isinstance(verb, str) and _VERB_RE.fullmatch(verb) else "?"


def parse_request(raw: bytes, registry: Mapping[str, Capability] = CAPABILITIES) -> tuple[Capability, Args]:
    """Validate one request line. Raises RequestError for anything off-schema."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError("request too large")
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        raise RequestError("request is not JSON") from err
    if not isinstance(request, dict) or set(request) - {"verb", "args"}:
        raise RequestError("request must be {verb, args}")
    verb = request.get("verb")
    capability = next((c for c in reachable(registry) if c.verb == verb), None) if isinstance(verb, str) else None
    if capability is None:
        raise RequestError("unknown verb")
    args = request.get("args", {})
    if not isinstance(args, dict):
        raise RequestError("args must be an object")
    unknown = set(args) - set(capability.args)
    if unknown:
        raise RequestError(f"unknown argument(s): {', '.join(sorted(map(str, unknown)))}")
    values = {name: _value(name, arg, args.get(name)) for name, arg in capability.args.items()}
    return capability, Args(values)


def build_parser(registry: Mapping[str, Capability] = CAPABILITIES) -> argparse.ArgumentParser:
    """The `opsctl` command line, generated from the registry."""
    parser = argparse.ArgumentParser(prog="opsctl", description="Ops Agent capabilities (output is redacted).")
    sub = parser.add_subparsers(dest="verb", required=True)
    for cap in reachable(registry):
        cmd = sub.add_parser(cap.verb, help=cap.summary, description=cap.summary)
        for name, arg in cap.args.items():
            if arg.positional:
                cmd.add_argument(name, choices=list(arg.choices) or None, help=arg.help or None)
            elif arg.kind is bool:
                cmd.add_argument(arg.flag or f"--{name}", dest=name, action="store_true", default=None, help=arg.help)
            else:
                kind = int if arg.kind is int else str
                cmd.add_argument(arg.flag or f"--{name}", dest=name, type=kind, help=arg.help or None)
    helper = sub.add_parser("help", help="show this help, or one verb's arguments")
    helper.add_argument("topic", nargs="?", choices=sorted(cli_verbs(registry)))
    return parser


def _usage(cap: Capability) -> str:
    parts = [f"opsctl {cap.verb}"]
    for name, arg in cap.args.items():
        if arg.positional:
            parts.append(f"<{name}>")
        elif arg.kind is bool:
            parts.append(f"[{arg.flag or '--' + name}]")
        else:
            parts.append(f"[{arg.flag or '--' + name} {name.upper()}]")
    return " ".join(parts)


def unavailable(cap: Capability, available: frozenset[str]) -> str | None:
    """Why this install cannot offer `cap` (`available`: `git` and the enabled overlays), or None."""
    missing = cap.requires - available
    if not missing:
        return None
    if "git" in missing:
        return f"{cap.verb}: git proposals are off on this install (the house profile does not enable git)"
    return f"{cap.verb} is not part of this install (house overlay {sorted(missing)[0]} is off)"


def capability_table(names: Iterable[str], registry: Mapping[str, Capability] = CAPABILITIES,
                     available: frozenset[str] = REQUIREMENTS) -> str:  # fmt: skip
    """Markdown list of the capabilities a run profile offers on this install, for its system prompt."""
    lines = []
    for name in sorted(names):
        cap = registry.get(name)
        if cap is None or cap.internal or unavailable(cap, available) is not None:
            continue
        kind = "read" if cap.read_only else f"effect, {cap.tier}"
        quota = f", at most {cap.quota.per_run} per run" if cap.quota.per_run else ""
        lines.append(f"- `{_usage(cap)}`: {cap.summary} ({kind}{quota})")
    return "\n".join(lines)
