"""Adversarial tests for the first mutating socket surface: binding, profiles, quotas, audit."""

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opsd import rpc
from opsd.baseline import Item
from opsd.channels import BudgetClass, Message
from opsd.config import Options, Paths
from opsd.policy import Origin
from opsd.redact import build_redactor
from opsd.rpc import Peer, RpcServer, descends_from
from opsd.runctx import AGENT_SELF, ExecEnv, Principal, RunContext
from opsd.runner import READ_CAPABILITIES, TRIAGE
from opsd.store import Store
from tests.conftest import make_options

OPS_UID = 1001
CLAUDE_PID = 500
ITEMS = {
    "entities:zha": Item("entities:zha", "entities", "warning", "2 zha entities newly unavailable"),
    "custom:acme_solar": Item("custom:acme_solar", "custom_integration", "critical", "acme_solar broken"),
}


class Sink:
    def __init__(self) -> None:
        self.messages: list[Message] = []
        self.outcome = "sent"

    async def send_message(self, message: Message) -> str:
        self.messages.append(message)
        return self.outcome


class World:
    def __init__(self, paths: Paths, *, enforce: bool = False, options: Options | None = None) -> None:
        self.store = Store.open(paths.store_file)
        self.sink = Sink()
        self.paths = paths
        self.env = ExecEnv(
            paths=paths,
            options=options or make_options(agent__mode="act"),
            client=None,  # type: ignore[arg-type]
            redactor=lambda: build_redactor(paths.secrets_yaml),
            store=self.store,
            effects=self.sink,
        )
        tree = {CLAUDE_PID: 1, 501: CLAUDE_PID, 502: 501, 900: 1}
        self.server = RpcServer(
            self.env,
            allowed_uids=frozenset({0, OPS_UID}) if enforce else None,
            in_run_tree=lambda pid, root: _walk(tree, pid, root),
        )

    def run(self, *, allowed: frozenset[str] = TRIAGE.capabilities, handoff: dict[str, Item] | None = None,
            principal: Principal = AGENT_SELF) -> RunContext:  # fmt: skip
        ctx = RunContext(profile="triage", principal=principal, allowed=allowed, budget_class=BudgetClass.AGENT,
                         started_at=datetime.now(UTC), handoff=ITEMS if handoff is None else handoff)  # fmt: skip
        self.server.begin_run(ctx)
        self.server.attach_process(CLAUDE_PID)
        return ctx

    async def call(self, verb: str, peer: Peer = rpc.LOCAL, **args: Any) -> dict[str, Any]:
        return await self.server.dispatch(json.dumps({"verb": verb, "args": args}).encode(), peer)

    def audit(self) -> list[dict[str, Any]]:
        return list(reversed(self.store.audit_rows(limit=1000)))


def _walk(tree: dict[int, int], pid: int, root: int) -> bool:
    while pid in tree:
        if pid == root:
            return True
        pid = tree[pid]
    return pid == root


@pytest.fixture
def world(paths: Paths) -> Iterator[World]:
    w = World(paths)
    yield w
    w.store.close()


@pytest.fixture
def strict(paths: Paths) -> Iterator[World]:
    w = World(paths, enforce=True)
    yield w
    w.store.close()


# ------------------------------------------------------------------ binding


async def test_effects_refused_outside_a_run(world: World) -> None:
    for verb, args in (("ack", {"key": "entities:zha"}), ("notify", {"title": "t", "message": "m"})):
        response = await world.call(verb, **args)
        assert not response["ok"] and "only available inside an agent run" in response["error"]
    assert world.sink.messages == []
    assert all(r["decision"] == "rejected" for r in world.audit())


async def test_strict_binding_by_uid_and_process_tree(strict: World) -> None:
    ops_claude_child = Peer(pid=502, uid=OPS_UID)
    stranger_ops = Peer(pid=900, uid=OPS_UID)
    root = Peer(pid=1, uid=0)
    # No run: ops is refused everything; root is refused always.
    assert (await strict.call("status", ops_claude_child))["error"] == "no agent run is active"
    assert (await strict.call("status", root))["error"] == "peer not allowed"
    assert (await strict.call("status", Peer(pid=2, uid=4242)))["error"] == "peer not allowed"
    assert (await strict.call("status", Peer(pid=None, uid=None)))["error"] == "peer not allowed"
    strict.run()
    assert (await strict.call("ack", ops_claude_child, key="entities:zha"))["ok"]
    assert "not part of the active agent run" in (await strict.call("status", stranger_ops))["error"]
    assert "not part of the active agent run" in (await strict.call("status", Peer(pid=None, uid=OPS_UID)))["error"]
    assert (await strict.call("ack", root, key="entities:zha"))["error"] == "peer not allowed"
    strict.server.end_run()
    assert (await strict.call("ack", ops_claude_child, key="entities:zha"))["error"] == "no agent run is active"


async def test_run_not_attached_to_a_process_refuses_ops(strict: World) -> None:
    ctx = RunContext(profile="triage", principal=AGENT_SELF, allowed=TRIAGE.capabilities,
                     budget_class=BudgetClass.AGENT, started_at=datetime.now(UTC), handoff=ITEMS)  # fmt: skip
    strict.server.begin_run(ctx)
    response = await strict.call("status", Peer(pid=502, uid=OPS_UID))
    assert "not part of the active agent run" in response["error"]


def test_only_one_run_can_be_bound(world: World) -> None:
    world.run()
    with pytest.raises(RuntimeError, match="another agent run"):
        world.run()


def test_descends_from_proc(tmp_path: Path) -> None:
    for pid, ppid in ((10, 1), (11, 10), (12, 11), (20, 1)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (odd ) name) S {ppid} 0 0")
    assert descends_from(12, 10, tmp_path) and descends_from(10, 10, tmp_path)
    assert not descends_from(20, 10, tmp_path) and not descends_from(99, 10, tmp_path)
    (tmp_path / "30").mkdir()
    (tmp_path / "30" / "stat").write_text("30 (x) S notanumber")
    assert not descends_from(30, 10, tmp_path)
    # A ppid loop never hangs.
    for pid, ppid in ((40, 41), (41, 40)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (x) S {ppid}")
    assert not descends_from(40, 10, tmp_path)


# --------------------------------------------------------------- profiles


async def test_profile_allowlist(world: World) -> None:
    world.run(allowed=READ_CAPABILITIES)
    response = await world.call("ack", key="entities:zha")
    assert not response["ok"] and "not available in the triage profile" in response["error"]
    assert (await world.call("status"))["ok"]


async def test_origin_not_accepted(world: World) -> None:
    world.run(principal=Principal(Origin.ADMIN, user_id="u1", channel="companion"))
    response = await world.call("ack", key="entities:zha")
    assert not response["ok"] and "origin admin not accepted" in response["error"]


# -------------------------------------------------------------------- ack


async def test_ack_validation_and_clamps(world: World) -> None:
    run = world.run()
    unknown = await world.call("ack", key="made:up", days=1)
    assert not unknown["ok"] and "unknown item key" in unknown["error"]
    critical = await world.call("ack", key="custom:acme_solar", days=30, accept=True, reason="fine")
    assert critical["ok"] and "accept refused" in critical["output"] and "at most 3 days" in critical["output"]
    plain = await world.call("ack", key="entities:zha", days=99, accept=True)
    assert plain["ok"]
    acks = {a.key: a for a in run.effects.acks}
    assert acks["custom:acme_solar"].accept is False and acks["custom:acme_solar"].days == 3
    assert acks["entities:zha"].accept is True and acks["entities:zha"].days == 30
    assert acks["entities:zha"].reason == "no reason given"


async def test_replayed_ack_from_an_earlier_run_is_refused(world: World) -> None:
    world.run()
    request = {"key": "entities:zha", "days": 2}
    assert (await world.call("ack", **request))["ok"]
    world.server.end_run()
    assert not (await world.call("ack", **request))["ok"]
    later = world.run(handoff={"custom:acme_solar": ITEMS["custom:acme_solar"]})
    replay = await world.call("ack", **request)
    assert not replay["ok"] and "unknown item key" in replay["error"] and later.effects.acks == []


# ----------------------------------------------------------------- notify


async def test_notify_one_per_run_and_urgent_only_with_critical(world: World) -> None:
    world.run(handoff={"entities:zha": ITEMS["entities:zha"]})
    first = await world.call("notify", title="Zigbee", message="two sensors down\\ncheck the hub", urgent=True)
    assert first["ok"] and "urgent downgraded" in first["output"]
    second = await world.call("notify", title="again", message="spam")
    assert not second["ok"] and "1 call(s) per run" in second["error"]
    [message] = world.sink.messages
    assert message.urgent is False and message.body == "two sensors down\ncheck the hub"
    assert message.title == "Agent: Zigbee" and message.tag == "ha-ops-agent"
    assert message.budget_class is BudgetClass.AGENT


async def test_notify_urgent_with_critical_and_redaction(world: World) -> None:
    run = world.run()
    response = await world.call("notify", title="Acme Solar", message="password: hunter22secret leaked", urgent=True)
    assert response["ok"] and world.sink.messages[0].urgent
    assert "hunter22secret" not in world.sink.messages[0].body and run.effects.notify_outcomes == ["sent"]


async def test_notify_rate_limited_is_reported_and_counts(world: World) -> None:
    world.sink.outcome = "rate_limited"
    world.run()
    response = await world.call("notify", title="t", message="m")
    assert response["ok"] and "not delivered: rate_limited" in response["output"]
    assert not (await world.call("notify", title="t", message="m"))["ok"]


async def test_notify_daily_quota_across_runs(world: World) -> None:
    for _ in range(9):
        world.run()
        assert (await world.call("notify", title="t", message="m"))["ok"]
        world.server.end_run()
    world.run()
    response = await world.call("notify", title="t", message="m")
    assert not response["ok"] and "daily quota" in response["error"]


async def test_notify_empty_message_refused(world: World) -> None:
    world.run()
    assert "message is required" in (await world.call("notify", title="t", message="   "))["error"]
    assert "message is required" in (await world.call("notify", title="t"))["error"]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        (make_options(agent__mode="act", capabilities__never=("notify.admin",)), "not allowed by policy"),
        (
            make_options(agent__mode="act", capabilities__require_approval=("notify.admin",)),
            "needs the admin's approval",
        ),
    ],
)
async def test_options_tighten_effects(paths: Paths, options: Options, message: str) -> None:
    world = World(paths, options=options)
    try:
        world.run()
        response = await world.call("notify", title="t", message="m")
        assert not response["ok"] and message in response["error"] and world.sink.messages == []
    finally:
        world.store.close()


# ---------------------------------------------------------- floods, audit


async def test_flood_is_bounded_by_budget_and_audit_limit(world: World) -> None:
    run = world.run()
    for _ in range(rpc.AUDIT_REJECTIONS_PER_MIN + 40):
        await world.server.dispatch(b'{"verb": "rm", "args": {}}')
    rejected = [r for r in world.audit() if r["decision"] == "rejected"]
    assert len(rejected) == rpc.AUDIT_REJECTIONS_PER_MIN
    assert run.requests_left == 200 - rpc.AUDIT_REJECTIONS_PER_MIN - 40
    for _ in range(run.requests_left):
        await world.server.dispatch(b'{"verb": "status"}')
    response = await world.server.dispatch(b'{"verb": "status"}')
    assert not response["ok"] and "budget" in response["error"]


async def test_every_request_is_audited(world: World) -> None:
    run = world.run()
    await world.call("status")
    await world.call("ack", key="entities:zha", days=2)
    await world.call("ack", key="nope")
    await world.server.dispatch(b"not json")
    rows = world.audit()
    assert [(r["capability"], r["outcome"]) for r in rows] == [
        ("agent.status", "ok"),
        ("baseline.ack", "ok"),
        ("baseline.ack", "rejected"),
        ("?", "rejected"),
    ]
    assert all(r["run_id"] == run.run_id and r["principal"] == "agent-self" for r in rows)
    assert json.loads(rows[1]["args"])["days"] == 2


async def test_audit_failure_refuses_request(world: World) -> None:
    world.run()
    world.store.close()
    response = await world.call("ack", key="entities:zha")
    assert response == {"ok": False, "error": "audit log unavailable; request refused"}


async def test_effects_need_a_store(paths: Paths) -> None:
    world = World(paths)
    world.store.close()
    server = RpcServer(replace(world.env, store=None))
    ctx = RunContext(profile="triage", principal=AGENT_SELF, allowed=TRIAGE.capabilities,
                     budget_class=BudgetClass.AGENT, started_at=datetime.now(UTC), handoff=ITEMS)  # fmt: skip
    server.begin_run(ctx)
    response = await server.dispatch(b'{"verb": "ack", "args": {"key": "entities:zha"}}')
    assert response["error"] == "audit log unavailable; request refused"


async def test_effects_without_a_sink_fail_cleanly(paths: Paths) -> None:
    world = World(paths)
    try:
        server = RpcServer(replace(world.env, effects=None))
        ctx = RunContext(profile="triage", principal=AGENT_SELF, allowed=TRIAGE.capabilities,
                         budget_class=BudgetClass.AGENT, started_at=datetime.now(UTC), handoff=ITEMS)  # fmt: skip
        server.begin_run(ctx)
        notify = await server.dispatch(b'{"verb": "notify", "args": {"title": "t", "message": "m"}}')
        assert not notify["ok"] and not any(ctx.counts.values())
    finally:
        world.store.close()


async def test_concurrent_calls_cannot_both_pass_the_quota(world: World) -> None:
    import asyncio

    gate = asyncio.Event()
    original = world.sink.send_message

    async def slow(message: Message) -> str:
        await gate.wait()
        return await original(message)

    world.sink.send_message = slow  # type: ignore[method-assign]
    world.run()
    first = asyncio.create_task(world.call("notify", title="a", message="one"))
    second = asyncio.create_task(world.call("notify", title="b", message="two"))
    await asyncio.sleep(0.05)
    gate.set()
    results = [await first, await second]
    assert sorted(r["ok"] for r in results) == [False, True] and len(world.sink.messages) == 1


async def test_failed_execution_refunds_the_run_quota(paths: Paths) -> None:
    world = World(paths)
    try:
        server = RpcServer(replace(world.env, effects=None))
        ctx = RunContext(profile="triage", principal=AGENT_SELF, allowed=TRIAGE.capabilities,
                         budget_class=BudgetClass.AGENT, started_at=datetime.now(UTC), handoff=ITEMS)  # fmt: skip
        server.begin_run(ctx)
        await server.dispatch(b'{"verb": "notify", "args": {"title": "t", "message": "m"}}')
        assert ctx.counts["notify.admin"] == 0
        assert world.store.count(datetime.now(UTC).date().isoformat(), "cap:notify.admin") == 1
    finally:
        world.store.close()


async def test_chat_origin_cannot_use_mutating_effects(world: World) -> None:
    world.run(principal=Principal(Origin.ADMIN_CHAT, user_id="u1", channel="companion"))
    assert (await world.call("status"))["ok"]
    for verb, args in (("ack", {"key": "entities:zha"}),):
        response = await world.call(verb, **args)
        assert not response["ok"] and ("not accepted" in response["error"] or "chat-origin" in response["error"])
