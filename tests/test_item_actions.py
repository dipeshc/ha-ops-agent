"""Every item the panel shows is actionable: "Ask the agent" on every item, and Plan a fix, Remind me and Hide on
maintenance findings and suggestions. Synthetic names only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import maintenance
from opsd.daemon import Daemon
from opsd.items import ASK, Context, ItemBook
from opsd.live import Hub
from opsd.maintenance import Finding
from opsd.store import Store
from tests.test_assistant import ADMIN
from tests.test_daemon import Harness
from tests.test_panel import Panel, h, local_ingress  # noqa: F401

NOW = datetime.now(UTC).replace(microsecond=0)
# A custom integration Core now covers (no deadline), and a removal with a deadline months away.
SUGGESTION = Finding(key="0123456789abcdef", variant="replacement", signal="replacement", what="Core may replace it",
                     domain="acme_weather", affected=["integration acme_weather"], detail={"risk": "medium"},
                     sources=["Core integrations"], quote="Core has acme_weather")  # fmt: skip
REMOVAL = f"{NOW.year + 1}.6"
DEPRECATION = Finding(key="fedcba9876543210", variant="deprecation", signal="repair", what="deprecated",
                      domain="acme_sprinkler", removal=REMOVAL, affected=["integration acme_sprinkler"],
                      sources=["repair acme_sprinkler:deprecated_yaml"], quote="Repair acme_sprinkler:deprecated_yaml",
                      steps=["Open the repair.", "Move the YAML to the UI."])  # fmt: skip


def store_findings(store: Store, *found: Finding, installed: str = f"{NOW.year}.1.0") -> None:
    """A scan's result, written as the scan writes it (one transaction: the items are composed once)."""
    with store.atomic():
        store.kv_set(maintenance.FINDINGS_KEY, json.dumps([f.to_json() for f in found]), now=NOW)
        store.kv_set(maintenance.META_KEY, json.dumps({"at": NOW.isoformat(), "installed": installed}), now=NOW)


def shown(book: ItemBook) -> list[dict[str, Any]]:
    """Every item on Home that is not resolved, as the panel receives it."""
    home = book.home()
    return [book.view(r) for k in ("needs", "running", "coming", "suggestions") for r in home[k]]


def assert_actionable(book: ItemBook) -> list[dict[str, Any]]:
    items = shown(book)
    assert items
    for item in items:
        assert item["actions"] and item["actions"][-1] == ASK, item["title"]
    return items


# ------------------------------------------------------------------ the rule, over every item kind


def test_every_item_the_panel_shows_offers_an_action(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "ops.db")
    health = [{"key": "entities:acme_zha", "severity": "warning", "summary": "2 acme_zha entities down"},
              {"key": "custom:acme_solar", "severity": "critical", "summary": "acme_solar broken"},
              {"key": "git:dirty", "severity": "info", "summary": "local changes"},
              {"key": "remote:down", "severity": "warning", "summary": "remote access down",
               "detail": {"needs_after": (NOW + timedelta(hours=6)).isoformat()}}]  # fmt: skip
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW,
                    context=lambda: Context(health_at=NOW.isoformat(), health_items=health, acting=True))  # fmt: skip
    book.write_health()
    store.create_task(
        kind="reminder",
        title="Water the plants",
        owner="admin",
        created_by="chat:r",
        next_fire_at=NOW + timedelta(days=1),
        payload={"note": "n"},
        idempotency_key="r1",
        now=NOW,
    )
    store.create_task(kind="followup", title="Look at the solar inverter", owner="admin", created_by="chat:r",
                      next_fire_at=NOW, payload={"investigation": True, "note": "n", "max_steps": 2},
                      idempotency_key="i1", now=NOW, thread_id=None)  # fmt: skip
    store_findings(store, SUGGESTION, DEPRECATION)
    who = f"{maintenance.PROPOSER}{DEPRECATION.key}"
    store.create_change(kind="change", subject="fix(maintenance): migrate acme_sprinkler", why="w", effect="e",
                        state="open", requested_by=who, now=NOW, expires_at=NOW + timedelta(days=7))  # fmt: skip
    store.create_change(kind="change", subject="fix(automations): kitchen", why="w", effect="e", state="open",
                        requested_by="chat", now=NOW, expires_at=NOW + timedelta(days=7))  # fmt: skip
    items = assert_actionable(book)
    kinds = {i["kind"] for i in items}
    assert {"check", "reminder", "investigation", "maintenance", "proposal"} <= kinds
    # The owner's case: a suggestion about a custom integration can be planned, put off or hidden.
    [suggestion] = [i for i in items if i["title"].startswith("acme_weather")]
    assert [a["id"] for a in suggestion["actions"]] == ["plan_fix", "remind_month", "remind_quarter", "dismiss", "ask"]
    [removal] = [i for i in items if i["title"].startswith("acme_sprinkler")]
    assert "remind_deadline" in [a["id"] for a in removal["actions"]]
    store.close()


async def test_every_update_item_offers_an_action(tmp_path: Path) -> None:
    from tests.test_updates import BROKER, CARD, Rig, only_auto

    rig = Rig(tmp_path)
    try:
        rig.ages = {BROKER: 2.0, CARD: None}
        only_auto(rig, BROKER, CARD)
        await rig.manager.run_window("manual")
        assert_actionable(rig.book)
    finally:
        rig.store.close()


# ------------------------------------------------------------------ the panel


async def started(h: Harness) -> tuple[Daemon, Panel]:  # noqa: F811
    d = h.daemon(agent__mode="act")
    d.inbound.admin = ADMIN
    server = d.panel_server(port=0)
    await server.start()
    return d, Panel(d, server)


async def test_ask_the_agent_sends_the_item_with_the_message(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        store_findings(d.store, SUGGESTION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert item is not None
        view = (await panel.request("GET", f"/api/items/{item['id']}"))[2]
        assert view["item"]["actions"][-1] == {"id": "ask", "label": "Ask the agent", "style": "text"}
        thread = d.assistant.main_thread()
        h.claude_reply = {"reply": "Keep it until Core's version has the weather entity.", "summary": "s"}
        body = {"text": "What should I do about it?", "client_id": "ask-0001", "item": item["id"]}
        status, _, sent = await panel.request("POST", f"/api/threads/{thread}/messages", body=body)
        assert status == 202 and sent["turn"]["meta"]["item"] == item["id"]
        assert sent["turn"]["meta"]["item_title"] == item["title"]
        await d.assistant.drain()
        prompt = h.claude_calls[-1]
        block = prompt[prompt.index("<item>") : prompt.index("</item>")]
        assert item["id"] in block and "Core has acme_weather" in block and "Plan a fix" in block
        # The item's text is data inside the fence: no tag of its own survives.
        assert block.count("<") == 1
        bad = {"text": "and this?", "client_id": "ask-0002", "item": "m-000000000000"}
        status, _, refused = await panel.request("POST", f"/api/threads/{thread}/messages", body=bad)
        assert status == 400 and refused["error"] == "bad_item"
    finally:
        await panel.server.stop()


async def test_plan_a_fix_starts_an_investigation_with_the_item(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        store_findings(d.store, SUGGESTION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert item is not None
        act = {"action": "plan_fix", "revision": item["revision"]}
        status, _, body = await panel.request("POST", f"/api/items/{item['id']}/act", body=act)
        assert status == 200 and "planning a fix" in body["message"]
        [task] = [t for t in d.store.open_investigations()]
        assert task.payload["item"] == item["id"] and task.owner == "admin" and task.thread_id
        assert task.payload["origin"] == "admin-chat" and "custom integration's own code" in task.payload["note"]
        # One investigation per chat, and the capability's daily quota counts it.
        assert d.store.count(d.day(datetime.now(UTC)), "cap:task.investigate") == 1
        status, _, again = await panel.request("POST", f"/api/items/{item['id']}/act", body=act)
        assert status == 409 and "already running" in again["message"]
        h.claude_reply = {"reply": "Plan: 1. ... It needs an engineering session.", "summary": "s", "next": None}
        await d.assistant.on_task_fired(task)
        await d.assistant.drain()
        prompt = h.claude_calls[-1]
        assert "<item>" in prompt and item["id"] in prompt and "Plan a fix" in prompt
        statuses = [t.status for t in d.store.turns_page(task.thread_id, limit=20)]
        assert "investigation" in statuses
    finally:
        await panel.server.stop()


@pytest.mark.parametrize(("action", "days"), [("remind_month", 30), ("remind_quarter", 90), ("remind_deadline", None)])
async def test_remind_me_later_sets_a_reminder_about_the_item(
    h: Harness,  # noqa: F811
    local_ingress: None,  # noqa: F811
    action: str,
    days: int | None,
) -> None:
    d, panel = await started(h)
    try:
        assert d.store is not None
        store_findings(d.store, SUGGESTION, DEPRECATION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{DEPRECATION.key}")
        assert item is not None
        status, _, body = await panel.request("POST", f"/api/items/{item['id']}/act",
                                              body={"action": action, "revision": item["revision"]})  # fmt: skip
        assert status == 200 and body["message"].startswith("Reminder set")
        [task] = [t for t in d.store.open_tasks() if t.kind == "reminder"]
        due = datetime.fromisoformat(str(task.next_fire_at))
        expected = (datetime.now(UTC) + timedelta(days=days)) if days else DEPRECATION.asks_at()
        assert expected is not None and abs((due - expected).total_seconds()) < 120
        assert task.payload["item"] == item["id"] and task.owner == "admin"
        # A finding without a deadline has nothing to be reminded of near it: not offered.
        other = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert other is not None and "remind_deadline" not in [a["id"] for a in other["actions"]]
    finally:
        await panel.server.stop()
