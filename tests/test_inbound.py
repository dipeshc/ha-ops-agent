"""Inbound events: admin verification, spoofing, replay, flooding; channels and the event stream."""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import ws
from opsd.channels import Action, ActionKind, BudgetClass, CompanionPush, Message, push_kind
from opsd.events import Dispatcher, Event, EventQueue, Priority
from opsd.inbound import (
    EVENT_TYPE,
    AdminIdentity,
    InboundGate,
    Refusal,
    admin_identity,
    is_active_admin,
    verify_action,
)
from opsd.notify import Notifier
from opsd.policy import Origin
from opsd.rpc import RejectionLimiter
from opsd.store import Store

NOW = datetime(2026, 1, 26, 12, 0, tzinfo=UTC)
ADMIN = AdminIdentity(user_id="u-admin", device_id="dev-1")
ACTION = "OPS_REPLY_" + "ab" * 16


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    db = Store.open(tmp_path / "ops.db")
    # The gate checks expiry against the real clock, so the action must outlive today, not a fixed date.
    db.issue_action(ACTION, kind="REPLY", tag="ha-ops-thread-1", thread_id="t1", now=NOW,
                    expires_at=max(NOW, datetime.now(UTC)) + timedelta(days=1))  # fmt: skip
    yield db
    db.close()


def tap(user: Any = "u-admin", action: Any = ACTION, **data: Any) -> dict[str, Any]:
    return {
        "event_type": EVENT_TYPE,
        "data": {"action": action, "sourceDeviceID": "dev-1", **data},
        "origin": "REMOTE",
        "context": {"id": "c", "user_id": user},
    }


def test_verified_reply_becomes_an_event(store: Store) -> None:
    event = verify_action(tap(reply_text="why did\x07 the fridge alert?\n", sourceDeviceID="dev-1"), ADMIN, store, NOW)
    assert isinstance(event, Event) and event.type == "user.message" and event.priority is Priority.USER
    # Possibly forged by Core-resident code: chat origin, never full admin.
    assert event.principal.user_id == "u-admin" and event.principal.origin == "admin-chat"
    assert event.payload["reply_text"] == "why did the fridge alert?" and event.payload["thread_id"] == "t1"
    assert event.dedupe_key == f"action:{ACTION}"


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (tap(user="u-household"), "not from the admin user"),
        (tap(user=None), "not from the admin user"),
        (tap(user=["u-admin"]), "not from the admin user"),
        ({"event_type": EVENT_TYPE, "data": {"action": ACTION}}, "not from the admin user"),
        ({**tap(), "event_type": "call_service"}, "unexpected event type"),
        ({**tap(), "data": "x"}, "no event data"),
        (tap(action="OPS_APPROVE_123"), "not an agent action"),
        (tap(action="OPS_REPLY_" + "AB" * 16), "not an agent action"),
        (tap(action=None), "not an agent action"),
        (tap(action="OPS_DONE_" + "ab" * 16), "unknown, expired or already used"),
        (tap(sourceDeviceID="dev-2"), "from another device"),
        (tap(sourceDevicePermanentID="ABCD-other"), "from another device"),
        (tap(sourceDeviceID=["dev-1"]), "from another device"),
    ],
)
def test_spoofed_or_forged_events_are_refused(store: Store, event: dict[str, Any], reason: str) -> None:
    result = verify_action(event, ADMIN, store, NOW)
    assert isinstance(result, Refusal) and reason in result.reason
    # A refusal never consumes the real action.
    assert store.consume_action(ACTION, now=NOW) is not None


def test_replay_and_expiry(store: Store) -> None:
    assert isinstance(verify_action(tap(), ADMIN, store, NOW), Event)
    replay = verify_action(tap(), ADMIN, store, NOW)
    assert isinstance(replay, Refusal) and "already used" in replay.reason
    later = "OPS_DONE_" + "cd" * 16
    store.issue_action(later, kind="DONE", tag="t", thread_id=None, now=NOW, expires_at=NOW + timedelta(hours=1))
    expired = verify_action(tap(action=later), ADMIN, store, NOW + timedelta(hours=2))
    assert isinstance(expired, Refusal)


def test_unknown_admin_or_store_refuses_everything(store: Store) -> None:
    assert verify_action(tap(), None, store, NOW) == Refusal("admin identity unknown")
    assert verify_action(tap(), ADMIN, None, NOW) == Refusal("store unavailable")
    no_device = AdminIdentity(user_id="u-admin")
    # A named device must be the admin's; with no known id or slug, no device name can match.
    assert verify_action(tap(sourceDeviceID="anything"), no_device, store, NOW) == Refusal("from another device")


def test_the_real_ios_event_shape_is_accepted(store: Store) -> None:
    """iOS sends only `action` (and `reply_text` for replies); no source device at all."""
    real = {"event_type": EVENT_TYPE, "data": {"action": ACTION}, "origin": "REMOTE",
            "context": {"id": "01J", "parent_id": None, "user_id": "u-admin"}}  # fmt: skip
    event = verify_action(real, ADMIN, store, NOW)
    assert isinstance(event, Event) and event.principal.origin is Origin.ADMIN_CHAT
    # Single use still holds, and so does the user check.
    assert verify_action(real, ADMIN, store, NOW) == Refusal("unknown, expired or already used action")
    forged = {**real, "context": {"user_id": "u-household"}}
    assert verify_action(forged, ADMIN, store, NOW) == Refusal("not from the admin user")


def test_a_named_device_may_be_the_registration_id_or_its_slug(store: Store) -> None:
    admin = AdminIdentity(user_id="u-admin", device_id="dev-1", slug="admin_phone")
    assert isinstance(verify_action(tap(sourceDeviceID="admin_phone"), admin, store, NOW), Event)


def test_admin_identity_from_registration() -> None:
    entries = {
        "data": {
            "entries": [
                {"domain": "mobile_app", "data": {"device_name": "Guest Phone", "user_id": "u-guest"}},
                {
                    "domain": "mobile_app",
                    "data": {"device_name": "Admin Phone", "user_id": "u-admin", "device_id": "dev-1"},
                },
            ]
        }
    }
    assert admin_identity(entries, "mobile_app_admin_phone") == AdminIdentity("u-admin", "dev-1", "admin_phone")
    assert admin_identity(entries, "mobile_app_nobody") is None
    no_user = {"data": {"entries": [{"domain": "mobile_app", "data": {"device_name": "Admin Phone"}}]}}
    assert admin_identity(no_user, "mobile_app_admin_phone") is None
    for junk in ({}, {"data": []}, {"data": {"entries": "x"}}, {"data": {"entries": [1, {"domain": "mobile_app"}]}}):
        assert admin_identity(junk, "mobile_app_admin_phone") is None


def test_is_active_admin() -> None:
    admin = {"id": "u1", "is_active": True, "system_generated": False, "group_ids": ["system-admin"]}
    assert is_active_admin([admin], "u1")
    assert not is_active_admin([{**admin, "group_ids": ["system-users"]}], "u1")
    assert not is_active_admin([{**admin, "is_active": False}], "u1")
    assert not is_active_admin([{**admin, "system_generated": True}], "u1")
    assert not is_active_admin([admin], "u2") and not is_active_admin("x", "u1") and not is_active_admin([1], "u1")


async def test_gate_audits_refusals_with_a_limit_and_queues_verified(store: Store) -> None:
    queue = EventQueue()
    gate = InboundGate(store=store, queue=queue, admin=ADMIN, limiter=RejectionLimiter(5))
    for _ in range(50):
        await gate.on_event(tap(user="u-household"))
    refused = [r for r in store.audit_rows(limit=100) if r["decision"] == "refused"]
    assert len(refused) == 5 and refused[0]["principal"] == "user:u-household"
    assert len(queue) == 0
    await gate.on_event(tap())
    assert len(queue) == 1 and (await queue.get()).type == "user.message"
    accepted = [r for r in store.audit_rows(limit=100) if r["decision"] == "accepted"]
    assert accepted and accepted[0]["capability"] == f"inbound.{EVENT_TYPE}"
    store.close()
    await gate.on_event(tap())  # a broken store refuses, never raises


# ----------------------------------------------------------------- channels


def test_companion_push_issues_actions(store: Store, tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []
    notifier = Notifier(state_file=tmp_path / "n.json", push_cache_file=tmp_path / "p.json",
                        notify_service="mobile_app_admin_phone", max_per_day=3,
                        call_service=lambda d, s, data: calls.append(data))  # fmt: skip
    ids = iter(["OPS_REPLY_" + "11" * 16, "OPS_DONE_" + "22" * 16])
    channel = CompanionPush(notifier, store, new_id=lambda kind: next(ids))
    message = Message("Ask", "body", tag="ha-ops-thread-9", thread_id="t9", budget_class=BudgetClass.USER,
                      actions=(Action(ActionKind.REPLY, "Reply", text_input=True),
                               Action(ActionKind.DONE, "Done", authentication_required=True)))  # fmt: skip
    assert channel.send(message, core_up=True, now=NOW) == "sent"
    actions = calls[0]["data"]["actions"]
    assert actions[0] == {"action": "OPS_REPLY_" + "11" * 16, "title": "Reply", "behavior": "textInput"}
    assert actions[1]["authenticationRequired"] is True
    event = verify_action(tap(action="OPS_DONE_" + "22" * 16), ADMIN, store, NOW)
    assert isinstance(event, Event) and event.type == "user.action" and event.payload["thread_id"] == "t9"
    no_store = CompanionPush(notifier, None)
    assert no_store.send(message, core_up=True, now=NOW) == "sent" and "actions" not in calls[-1]["data"]


def test_budget_classes_map_to_buckets(tmp_path: Path) -> None:
    assert push_kind(Message("t", "b", budget_class=BudgetClass.SAFETY)) == "safety"
    # An urgent agent push is time-sensitive but counts in the normal bucket, never the safety reserve.
    assert push_kind(Message("t", "b", urgent=True)) == "normal"
    assert push_kind(Message("t", "b")) == "normal"
    assert push_kind(Message("t", "b", urgent=True, budget_class=BudgetClass.USER)) == "user"
    notifier = Notifier(state_file=tmp_path / "n.json", push_cache_file=tmp_path / "p.json",
                        notify_service="mobile_app_x", max_per_day=0, call_service=lambda *a: None)  # fmt: skip
    channel = CompanionPush(notifier, None)
    # User replies have their own bucket: an exhausted agent budget does not block them.
    assert channel.send(Message("t", "b"), core_up=True, now=NOW) == "rate_limited"
    assert channel.send(Message("t", "b", budget_class=BudgetClass.USER), core_up=True, now=NOW) == "sent"


# ------------------------------------------------------------- event queue


async def test_queue_priority_dedupe_and_bound() -> None:
    queue = EventQueue(maxsize=3)
    assert queue.put(Event("triage", "s", Priority.TRIAGE, dedupe_key="a"))
    assert not queue.put(Event("triage", "s", Priority.TRIAGE, dedupe_key="a"))
    assert queue.put(Event("user", "s", Priority.USER))
    assert queue.put(Event("safety", "s", Priority.SAFETY))
    assert not queue.put(Event("overflow", "s", Priority.SAFETY)) and queue.dropped == 1
    order = [(await queue.get()).type for _ in range(3)]
    assert order == ["safety", "user", "triage"]
    assert queue.busy("a")
    queue.done(Event("triage", "s", Priority.TRIAGE, dedupe_key="a"))
    assert not queue.busy("a")


async def test_dispatcher_runs_handlers_concurrently_and_survives_crashes() -> None:
    queue = EventQueue()
    seen: list[str] = []
    release = asyncio.Event()

    async def slow(event: Event) -> None:
        await release.wait()
        seen.append(event.type)

    async def fast(event: Event) -> None:
        seen.append(event.type)
        release.set()

    async def boom(event: Event) -> None:
        raise RuntimeError("bug")

    dispatcher = Dispatcher(queue, {"slow": slow, "fast": fast, "boom": boom}, max_in_flight=3)
    task = asyncio.create_task(dispatcher.run())
    queue.put(Event("slow", "s", Priority.TRIAGE, dedupe_key="slow"))
    queue.put(Event("boom", "s", Priority.TRIAGE))
    queue.put(Event("fast", "s", Priority.TRIAGE))
    for _ in range(50):
        if len(seen) == 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    assert seen == ["fast", "slow"] and not queue.busy("slow")


# ------------------------------------------------------------ event stream


class FakeWS:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.commands: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    async def command(self, msg_type: str, **fields: Any) -> Any:
        self.commands.append((msg_type, fields))
        return None

    async def send_command(self, msg_type: str, **fields: Any) -> int:
        self.commands.append((msg_type, fields))
        return 1

    async def recv_json(self, *, idle_s: float | None = None) -> dict[str, Any]:
        await asyncio.sleep(0)
        if not self.messages:
            raise ws.WebSocketError("closed by server")
        item = self.messages.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[no-any-return]

    async def close(self) -> None:
        self.closed = True


async def test_event_stream_subscribes_filters_limits_and_reconnects() -> None:
    event = {"event_type": EVENT_TYPE, "data": {}}
    first = FakeWS([{"type": "result"}, {"type": "event", "event": {"event_type": "other"}},
                    *[{"type": "event", "event": event}] * 5])  # fmt: skip
    second = FakeWS([TimeoutError()])
    connections = iter([first, second])
    received: list[dict[str, Any]] = []
    sleeps: list[float] = []

    async def connect() -> Any:
        try:
            return next(connections)
        except StopIteration:
            raise asyncio.CancelledError from None

    async def on_event(e: dict[str, Any]) -> None:
        received.append(e)

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    stream = ws.EventStream(
        connect, (EVENT_TYPE,), on_event, max_events_per_min=3, backoff_sleep=sleep, clock=lambda: 0.0
    )
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert first.commands[0] == ("subscribe_events", {"event_type": EVENT_TYPE})
    assert len(received) == 3 and stream.dropped == 2
    assert first.closed and second.closed and not stream.connected
    assert sleeps[:2] == [1.0, 2.0]


async def test_event_stream_connect_failure_backs_off() -> None:
    attempts = 0

    async def connect() -> Any:
        nonlocal attempts
        attempts += 1
        if attempts > 8:
            raise asyncio.CancelledError
        raise OSError("core down")

    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def on_event(_: dict[str, Any]) -> None:
        return None

    stream = ws.EventStream(connect, (EVENT_TYPE,), on_event, backoff_sleep=sleep, max_backoff=30)
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


async def test_event_stream_survives_a_crashing_callback() -> None:
    event = {"event_type": EVENT_TYPE, "data": {}}
    connections = iter([FakeWS([{"type": "event", "event": event}]), FakeWS([])])

    async def connect() -> Any:
        try:
            return next(connections)
        except StopIteration:
            raise asyncio.CancelledError from None

    async def on_event(_: dict[str, Any]) -> None:
        raise KeyError("bug")

    async def sleep(_: float) -> None:
        return None

    stream = ws.EventStream(connect, (EVENT_TYPE,), on_event, backoff_sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await stream.run()
