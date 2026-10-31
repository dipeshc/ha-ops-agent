"""Who a capability request serves and what it may touch.

`RunContext` is stamped on the RPC server for the single active AI run,
so the server knows the principal, the profile's allowed capabilities and
the run's hand-off without trusting anything in the request. `ExecContext`
is what an executor receives.
"""

import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Protocol
from zoneinfo import ZoneInfo

from opsd.baseline import Item
from opsd.capabilities import Capability
from opsd.channels import BudgetClass, Message
from opsd.config import Options, Paths
from opsd.gitops import Repo
from opsd.policy import Origin, Verdict
from opsd.redact import Redactor
from opsd.store import Store
from opsd.supervisor import SupervisorClient


@dataclass(frozen=True, slots=True)
class Ack:
    """A baseline acknowledgement a triage run made (`opsctl ack`), applied when the run ends."""

    key: str
    reason: str
    days: int
    accept: bool


# Requests allowed per AI run, whatever their outcome.
REQUEST_BUDGET = 200


@dataclass(frozen=True, slots=True)
class Principal:
    origin: Origin
    # The verified HA user id for `admin`; None for `agent-self`.
    user_id: str | None = None
    channel: str = "internal"

    def label(self) -> str:
        return f"{self.origin}:{self.user_id}" if self.user_id else str(self.origin)


AGENT_SELF = Principal(Origin.AGENT_SELF, channel="scheduler")


@dataclass(slots=True)
class RunEffects:
    """What the run's capability calls did, for the handler's post-processing."""

    acks: list[Ack] = field(default_factory=list)
    notify_outcomes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunContext:
    profile: str
    principal: Principal
    allowed: frozenset[str]
    budget_class: BudgetClass
    started_at: datetime
    # Items handed to this run, by key: the only keys an ack may name.
    handoff: Mapping[str, Item] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: secrets.token_hex(8))
    # The AI process; socket callers must descend from it.
    root_pid: int | None = None
    # Conversation this run answers (chat), and how deep a follow-up chain is.
    thread_id: str | None = None
    generation: int = 0
    requests_left: int = REQUEST_BUDGET
    counts: dict[str, int] = field(default_factory=dict)
    effects: RunEffects = field(default_factory=RunEffects)

    @property
    def has_critical(self) -> bool:
        return any(i.severity == "critical" for i in self.handoff.values())


class EffectSink(Protocol):
    """Daemon-side effects an executor may trigger."""

    def send_message(self, message: Message) -> Awaitable[str]: ...


@dataclass(frozen=True, slots=True)
class ExecEnv:
    paths: Paths
    options: Options
    client: SupervisorClient
    redactor: Callable[[], Redactor]
    store: Store | None = None
    effects: EffectSink | None = None
    tz: Callable[[], tzinfo] = lambda: ZoneInfo("UTC")
    # The live config repository (proposals are built in it with plumbing only).
    repo: Repo | None = None


@dataclass(slots=True)
class ExecContext:
    env: ExecEnv
    capability: Capability
    run: RunContext | None
    verdict: Verdict | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def paths(self) -> Paths:
        return self.env.paths

    @property
    def options(self) -> Options:
        return self.env.options

    @property
    def client(self) -> SupervisorClient:
        return self.env.client
