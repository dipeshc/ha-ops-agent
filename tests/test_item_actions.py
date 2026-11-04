"""Every item the panel shows is actionable: a conversation on every item, and Plan a fix (on the finding itself),
Remind me and Hide on maintenance findings and suggestions. Synthetic names only."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import maintenance
from opsd.daemon import Daemon
from opsd.items import Context, ItemBook
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
        # Every item takes questions in its own conversation; "Ask the agent" is no longer an action.
        assert item["conversation"] == "open" and "ask" not in [a["id"] for a in item["actions"]], item["title"]
        assert item["actions"] or item["need"] or item["state"] == "running", item["title"]
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
    assert [a["id"] for a in suggestion["actions"]] == ["plan_fix", "remind_month", "remind_quarter", "dismiss"]
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


async def test_asking_about_an_item_runs_in_its_own_conversation(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        store_findings(d.store, SUGGESTION, DEPRECATION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        other = d.store.item_by_key(f"maint:{DEPRECATION.key}")
        assert item is not None and other is not None
        view = (await panel.request("GET", f"/api/items/{item['id']}"))[2]
        assert view["item"]["conversation"] == "open" and "ask" not in [a["id"] for a in view["item"]["actions"]]
        h.claude_reply = {"reply": "Keep it until Core's version has the weather entity.", "summary": "s"}
        # The client cannot steer the message elsewhere: the thread and the item come from the URL's item record.
        body = {"text": "What should I do about it?", "client_id": "ask-0001", "item": other["id"],
                "thread": f"item:{other['id']}"}  # fmt: skip
        status, _, sent = await panel.request("POST", f"/api/items/{item['id']}/messages", body=body)
        assert status == 202 and sent["turn"]["meta"]["item"] == item["id"]
        assert sent["turn"]["thread"] == f"item:{item['id']}" and sent["turn"]["meta"]["item_title"] == item["title"]
        await d.assistant.drain()
        prompt = h.claude_calls[-1]
        block = prompt[prompt.index("<item>") : prompt.index("</item>")]
        assert item["id"] in block and "Core has acme_weather" in block and "Plan a fix" in block
        # The item's text is data inside the fence: no tag of its own survives.
        assert block.count("<") == 1
        status, _, convo = await panel.request("GET", f"/api/items/{item['id']}/thread")
        assert status == 200 and [t["role"] for t in convo["turns"]] == ["admin", "agent"] and not convo["read_only"]
        # The other item's conversation is its own, and empty.
        status, _, empty = await panel.request("GET", f"/api/items/{other['id']}/thread")
        assert status == 200 and empty["turns"] == []
        for path in ("/api/items/m-000000000000/messages", "/api/items/../thread"):
            status, _, _refused = await panel.request("POST", path, body={"text": "x", "client_id": "ask-0002"})
            assert status == 404
    finally:
        await panel.server.stop()


async def test_plan_a_fix_runs_on_the_finding_itself(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        store_findings(d.store, SUGGESTION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert item is not None
        act = {"action": "plan_fix", "revision": item["revision"]}
        status, _, body = await panel.request("POST", f"/api/items/{item['id']}/act", body=act)
        assert status == 200 and body["message"] == "Planning a fix here. You get a push when it is ready."
        [task] = [t for t in d.store.open_investigations()]
        assert task.payload["item"] == item["id"] and task.owner == "admin" and task.payload["plan_fix"]
        assert task.thread_id == f"item:{item['id']}"
        assert task.payload["origin"] == "admin-chat" and "custom integration's own code" in task.payload["note"]
        # The finding itself is In progress, with the plan's named phases and Stop; there is no second item.
        await d.items_flush()
        running = body["item"]
        assert (
            running["id"] == item["id"]
            and running["state"] == "running"
            and running["gist"].startswith("planning a fix")
        )
        assert running["phase"]["names"] == ["Read the evidence", "Write the plan"]
        assert [a["id"] for a in running["actions"]] == ["stop"]
        assert d.store.item_by_key(f"task:{task.id}") is None
        assert [r["id"] for r in d.items.home()["running"]] == [item["id"]]
        # One plan per item at a time, and the capability's daily quota counts it.
        assert d.store.count(d.day(datetime.now(UTC)), "cap:task.investigate") == 1
        act = {"action": "plan_fix", "revision": running["revision"]}
        status, _, _again = await panel.request("POST", f"/api/items/{item['id']}/act", body=act)
        assert status == 409
        h.claude_reply = {"reply": "Plan: 1. keep it. It needs an engineering session.", "summary": "s", "next": None}
        for due in d.store.claim_due(datetime.now(UTC) + timedelta(seconds=1)):
            await d.assistant.on_task_fired(due)
        await d.assistant.drain()
        prompt = h.claude_calls[-1]
        assert "<item>" in prompt and item["id"] in prompt and "Plan a fix" in prompt
        turns = d.store.turns_page(task.thread_id, limit=20)
        # The plan is shown once, on the finding: no agent turn repeats it; the card says where it is.
        assert [t for t in turns if t.role == "agent"] == []
        [card] = [t for t in turns if t.status == "investigation"]
        assert "under The agent says" in card.text
        # Done: the plan is the finding's "The agent says", and the finding is a suggestion again.
        await d.items_flush()
        done = d.store.item(item["id"])
        assert done is not None and done["state"] == "proposed" and done["agent"]["text"].startswith("Plan: 1.")
        # The admin was not watching: one push about the finding, tagged with it, Reply into its conversation.
        push = h.pushes()[-1]
        assert push["title"].startswith("Agent: Plan ready") and push["data"]["tag"] == item["id"]
        assert "REPLY" in str(push["data"].get("actions"))
    finally:
        await panel.server.stop()


async def test_stop_on_a_running_plan_cancels_it(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        store_findings(d.store, SUGGESTION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert item is not None
        _, _, body = await panel.request("POST", f"/api/items/{item['id']}/act",
                                         body={"action": "plan_fix", "revision": item["revision"]})  # fmt: skip
        stop = {"action": "stop", "revision": body["item"]["revision"]}
        status, _, _stopped = await panel.request("POST", f"/api/items/{item['id']}/act", body=stop)
        assert status == 200 and d.store.open_investigations() == []
        await d.items_flush()
        after = d.store.item(item["id"])
        assert after is not None and after["state"] == "proposed" and "plan_fix" in [a["id"] for a in after["actions"]]
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


def plan_done(store: Store, item: str, runs: list[str], *, plan: str = "1. Rebase. 2. Verify.") -> int:
    """A finished "Plan a fix" on `item` whose steps were `runs`."""
    task, _ = store.create_task(kind="followup", title="Plan a fix: x", owner="admin", created_by="panel:u",
                                next_fire_at=NOW, payload={"investigation": True, "plan_fix": True, "item": item,
                                                           "max_steps": 2, "step": 0},
                                idempotency_key=f"plan:{item}", now=NOW, thread_id=f"item:{item}")  # fmt: skip
    store.claim_due(NOW + timedelta(seconds=1))
    payload = task.payload | {"step": 2, "runs": runs, "plan": plan, "sources": ["custom_components/acme_fan"]}
    store.finish_task(task.id, "done", now=NOW, payload=payload)
    return task.id


def test_a_proposal_the_plan_made_is_decided_on_the_finding(tmp_path: Path) -> None:
    from opsd import forkrebase

    store = Store.open(tmp_path / "ops.db")
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    store_findings(store, SUGGESTION)
    finding = store.item_by_key(f"maint:{SUGGESTION.key}")
    assert finding is not None
    plan_done(store, finding["id"], ["run-plan-1"])
    record = {"id": "0123456789abcdef", "domain": "acme_fan", "core": "2026.11.2", "origin": "2026.10.1",
              "keep": ["h1"], "reason": "keep the fan preset", "by": "admin-chat:u", "run_id": "run-plan-1",
              "state": "open", "detail": "", "files": {"fan.py": {"old": "a", "new": "b"}}, "unchanged": [],
              "result": "f" * 64, "fork": "e" * 64, "hunks": [{"id": "h1", "added": 3, "removed": 1, "kept": True}],
              "kept_text": "h1", "diff": "+x", "created_at": NOW.isoformat(),
              "updated_at": NOW.isoformat()}  # fmt: skip
    store.kv_set(forkrebase.PROPOSAL_KEY + record["id"], json.dumps(record), now=NOW)
    folded = store.item(finding["id"])
    fork = store.item_by_key(f"fork:{record['id']}")
    assert folded is not None and fork is not None
    # One item in Needs you: the finding, with the rebase's approval, diff and facts, and the plan as its words.
    assert folded["needs"] and folded["need"]["primary"] == {"id": "approve_rebase", "label": "Rebase tonight"}
    assert folded["change"]["shown"] == ["f" * 64] and folded["agent"]["text"] == "1. Rebase. 2. Verify."
    assert folded["source"]["linked"] == {"fork": record["id"]}
    assert any(f["label"] == "Fork changes kept" for f in folded["facts"])
    # The rebase's own item is kept for History: quiet, following the finding, never asking or pushing.
    assert not fork["needs"] and fork["quiet"] and fork["follows"] == folded["key"] and fork["state"] == "proposed"
    assert fork["actions"] == [] and not fork["source"]["pushes"]
    home = book.home()
    assert [r["id"] for r in home["needs"]] == [finding["id"]]
    # One row: the folded rebase is shown on its finding, not in Coming up.
    assert fork["id"] not in [r["id"] for k in ("running", "coming", "suggestions", "today") for r in home[k]]
    # Rejected: the finding is a suggestion again (with its plan); the rebase is cancelled in History.
    forkrebase.move(store, record["id"], expect=("open",), to="rejected", now=NOW, detail="rejected")
    again = store.item(finding["id"])
    assert again is not None and again["state"] == "proposed" and not again["needs"]
    assert "linked" not in again["source"] and again["agent"]["text"] == "1. Rebase. 2. Verify."
    store.close()


def test_a_config_change_the_plan_made_is_applied_from_the_finding(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "ops.db")
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    store_findings(store, DEPRECATION)
    finding = store.item_by_key(f"maint:{DEPRECATION.key}")
    assert finding is not None
    plan_done(store, finding["id"], ["run-plan-2"])
    files = [{"path": "automations/acme.yaml", "new_sha256": "a" * 64}]
    change = store.create_change(kind="change", subject="fix(automations): acme_sprinkler", why="w", effect="e",
                                 state="open", requested_by="admin-chat:u", now=NOW, run_id="run-plan-2",
                                 expires_at=NOW + timedelta(days=7), files=files,
                                 diff="+++ b/automations/acme.yaml\n+x")  # fmt: skip
    folded = store.item(finding["id"])
    own = store.item_by_key(f"change:{change['id']}")
    assert folded is not None and own is not None
    assert folded["needs"] and folded["need"]["primary"]["id"] == "apply"
    assert folded["source"]["linked"] == {"change": change["id"]} and folded["change"]["shown"] == ["a" * 64]
    assert not own["needs"] and own["quiet"] and own["follows"] == folded["key"]
    assert [r["id"] for r in book.home()["needs"]] == [finding["id"]]
    store.close()


def test_a_plan_that_stopped_says_why_and_offers_a_retry(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "ops.db")
    ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    store_findings(store, SUGGESTION)
    finding = store.item_by_key(f"maint:{SUGGESTION.key}")
    assert finding is not None
    task, _ = store.create_task(kind="followup", title="Plan a fix: x", owner="admin", created_by="panel:u",
                                next_fire_at=NOW, payload={"investigation": True, "plan_fix": True,
                                                           "item": finding["id"], "max_steps": 2, "step": 1,
                                                           "started_at": NOW.isoformat()},
                                idempotency_key="p", now=NOW, thread_id=f"item:{finding['id']}")  # fmt: skip
    running = store.item(finding["id"])
    assert running is not None and running["state"] == "running" and running["phase"]["current"] == 1
    assert running["gist"] == "planning a fix · step 2 of 2"
    store.claim_due(NOW + timedelta(seconds=1))
    store.finish_task(task.id, "failed", now=NOW, payload=task.payload | {"stopped": "the AI usage limit"})
    failed = store.item(finding["id"])
    assert failed is not None and failed["state"] == "proposed" and "the plan could not finish" in failed["gist"]
    assert {"id": "plan_fix", "label": "Retry the plan"} in failed["actions"]
    store.close()


async def test_the_finding_rejects_the_rebase_its_plan_proposed(h: Harness, local_ingress: None) -> None:  # noqa: F811
    from opsd import forkrebase

    d, panel = await started(h)
    try:
        assert d.store is not None
        store_findings(d.store, SUGGESTION)
        finding = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert finding is not None
        plan_done(d.store, finding["id"], ["run-plan-3"])
        record = {"id": "fedcba9876543210", "domain": "acme_fan", "core": "2026.11.2", "origin": "2026.10.1",
                  "keep": [], "reason": "r", "by": "admin-chat:u", "run_id": "run-plan-3", "state": "open",
                  "detail": "", "files": {"fan.py": {"old": "a", "new": "b"}}, "unchanged": [], "result": "f" * 64,
                  "fork": "e" * 64, "hunks": [], "kept_text": "", "diff": "+x", "created_at": NOW.isoformat(),
                  "updated_at": NOW.isoformat()}  # fmt: skip
        d.store.kv_set(forkrebase.PROPOSAL_KEY + record["id"], json.dumps(record), now=NOW)
        await d.items_flush()
        folded = d.store.item(finding["id"])
        assert folded is not None and folded["needs"]
        status, _, body = await panel.request("POST", f"/api/items/{finding['id']}/act",
                                              body={"action": "reject", "revision": folded["revision"]})  # fmt: skip
        assert status == 200 and not body["item"]["need"]
        stored = forkrebase.load(d.store, record["id"])
        assert stored is not None and stored["state"] == "rejected"
    finally:
        await panel.server.stop()


async def test_a_question_is_an_item_that_opens_at_once(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        h.claude_reply = {"reply": "Both acme locks report 80 %.", "summary": ""}
        body = {"text": "How are the\nacme lock batteries?", "client_id": "q-client-01"}
        status, _, sent = await panel.request("POST", "/api/ask", body=body)
        item = sent["item"]
        assert status == 202 and item["kind"] == "question" and item["state"] == "running"
        assert item["title"] == "How are the acme lock batteries?" and item["conversation"] == "open"
        assert sent["turn"]["thread"] == f"ask:{item['id']}"
        # The same client id is the same question, never a second one.
        status, _, again = await panel.request("POST", "/api/ask", body=body)
        assert again["item"]["id"] == item["id"] and again["duplicate"]
        await d.assistant.drain()
        done = d.store.item(item["id"])
        assert done is not None and done["state"] == "done" and done["gist"].startswith("answered ")
        assert done["agent"]["text"] == "Both acme locks report 80 %." and not done["needs"]
        status, _, hist = await panel.request("GET", "/api/history?filter=questions")
        assert [i["id"] for i in hist["items"]] == [item["id"]]
        # Questions live on Ask, never on Home.
        home = (await panel.request("GET", "/api/home"))[2]
        assert item["id"] not in [i["id"] for rows in home["sections"].values() for i in rows]
        status, _, other = await panel.request("GET", "/api/history?filter=changes")
        assert item["id"] not in [i["id"] for i in other["items"]]
        # A follow-up runs it again, in its own conversation.
        h.claude_reply = {"reply": "Change them in spring.", "summary": ""}
        follow = {"text": "When should I change the acme lock batteries?", "client_id": "q-client-02"}
        status, _, _ = await panel.request("POST", f"/api/items/{item['id']}/messages", body=follow)
        running = d.store.item(item["id"])
        assert status == 202 and running is not None and running["state"] == "running"
        await d.assistant.drain()
        page = (await panel.request("GET", f"/api/items/{item['id']}/thread"))[2]
        assert [t["text"] for t in page["turns"]][-1] == "Change them in spring."
        for bad in ({"text": "", "client_id": "q-client-03"}, {"text": "x", "client_id": "no"}):
            assert (await panel.request("POST", "/api/ask", body=bad))[0] == 400
        assert (await panel.request("POST", "/api/ask", body={"text": "x" * 2001, "client_id": "q-client-04"}))[
            0
        ] == 413
        assert (await panel.request("POST", "/api/ask", body=body, csrf=False))[0] == 403
        assert (await panel.request("POST", "/api/ask", body=body, admin=False))[0] == 403
    finally:
        await panel.server.stop()


def test_a_finding_that_asks_keeps_its_question_and_carries_nothing(tmp_path: Path) -> None:
    """A deprecation that asks (its deadline is near) keeps its need while a plan runs; its plan's proposal is not
    folded into it but asks on its own item."""
    from opsd import forkrebase

    store = Store.open(tmp_path / "ops.db")
    ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    near = Finding(key="00112233445566aa", variant="deprecation", signal="repair", what="deprecated",
                   domain="acme_fan", removal=f"{NOW.year}.2", affected=["integration acme_fan"],
                   sources=["repair acme_fan:deprecated"], quote="Repair acme_fan:deprecated")  # fmt: skip
    store_findings(store, near, installed=f"{NOW.year}.1.0")
    asking = store.item_by_key(f"maint:{near.key}")
    assert asking is not None and asking["needs"] and asking["need"]["kind"] == "ack"
    task, _ = store.create_task(kind="followup", title="Plan a fix: x", owner="admin", created_by="panel:u",
                                next_fire_at=NOW, payload={"investigation": True, "plan_fix": True,
                                                           "item": asking["id"], "max_steps": 2, "step": 0},
                                idempotency_key="near", now=NOW, thread_id=f"item:{asking['id']}")  # fmt: skip
    running = store.item(asking["id"])
    assert running is not None and running["needs"] and running["need"]["kind"] == "ack"
    assert "planning a fix" in running["gist"] and "stop" in [a["id"] for a in running["actions"]]
    store.claim_due(NOW + timedelta(seconds=1))
    store.finish_task(task.id, "done", now=NOW, payload=task.payload | {"runs": ["run-near"], "plan": "p"})
    record = {"id": "aabbccddeeff0011", "domain": "acme_fan", "core": "2026.11.2", "origin": "2026.10.1",
              "keep": [], "reason": "r", "by": "admin-chat:u", "run_id": "run-near", "state": "open", "detail": "",
              "files": {"fan.py": {"old": "a", "new": "b"}}, "unchanged": [], "result": "f" * 64, "fork": "e" * 64,
              "hunks": [], "kept_text": "", "diff": "+x", "created_at": NOW.isoformat(),
              "updated_at": NOW.isoformat()}  # fmt: skip
    store.kv_set(forkrebase.PROPOSAL_KEY + record["id"], json.dumps(record), now=NOW)
    both = store.item(asking["id"])
    # The finding asks by itself, so it carries nothing: no link, none of the rebase's actions, a fact naming it.
    assert both is not None and both["need"]["kind"] == "ack" and "linked" not in both["source"]
    assert not {"approve_rebase", "reject"} & {a["id"] for a in both["actions"]}
    assert any(f["label"] == "Proposed by the plan" for f in both["facts"])
    # The rebase asks on its own item, which follows the finding for information.
    fork = store.item_by_key(f"fork:{record['id']}")
    assert fork is not None and fork["needs"] and fork["need"]["primary"]["id"] == "approve_rebase"
    assert fork["follows"] == both["key"] and not fork["quiet"]
    store.close()


def test_a_hidden_finding_with_a_proposal_is_still_one_item(tmp_path: Path) -> None:
    from opsd import forkrebase

    store = Store.open(tmp_path / "ops.db")
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    store_findings(store, SUGGESTION)
    finding = store.item_by_key(f"maint:{SUGGESTION.key}")
    assert finding is not None
    store.kv_set(maintenance.HIDDEN_KEY + SUGGESTION.key, SUGGESTION.evidence, now=NOW)
    hidden = store.item(finding["id"])
    assert hidden is not None and hidden["state"] == "cancelled"
    plan_done(store, finding["id"], ["run-hidden"])
    record = {"id": "1122334455667788", "domain": "acme_weather", "core": "2026.11.2", "origin": "2026.10.1",
              "keep": [], "reason": "r", "by": "admin-chat:u", "run_id": "run-hidden", "state": "open", "detail": "",
              "files": {"w.py": {"old": "a", "new": "b"}}, "unchanged": [], "result": "f" * 64, "fork": "e" * 64,
              "hunks": [], "kept_text": "", "diff": "+x", "created_at": NOW.isoformat(),
              "updated_at": NOW.isoformat()}  # fmt: skip
    store.kv_set(forkrebase.PROPOSAL_KEY + record["id"], json.dumps(record), now=NOW)
    asks = [r for r in store.items_all() if r["needs"]]
    assert [r["id"] for r in asks] == [finding["id"]]
    assert [r["id"] for r in book.home()["needs"]] == [finding["id"]]
    store.close()


async def test_ask_checks_its_limits_before_it_makes_a_question(h: Harness, local_ingress: None) -> None:  # noqa: F811
    d, panel = await started(h)
    try:
        assert d.store is not None and d.assistant is not None
        # A client id already used for a message in another conversation: a clear 409, and no question.
        store_findings(d.store, SUGGESTION)
        await d.items_flush()
        item = d.store.item_by_key(f"maint:{SUGGESTION.key}")
        assert item is not None
        h.claude_reply = {"reply": "ok", "summary": ""}
        status, _, _ = await panel.request("POST", f"/api/items/{item['id']}/messages",
                                           body={"text": "first", "client_id": "shared-01"})  # fmt: skip
        assert status == 202
        await d.assistant.drain()
        status, _, body = await panel.request("POST", "/api/ask", body={"text": "second", "client_id": "shared-01"})
        assert status == 409 and body["error"] == "client_id_used"
        status, _, body = await panel.request("POST", f"/api/items/{item['id']}/messages",
                                              body={"text": "x", "client_id": "shared-01"})  # fmt: skip
        assert status == 200 and body["duplicate"]
        # The day's message limit reached: 429, and no question left Running.
        for _ in range(30):
            d.store.bump(d.day(datetime.now(UTC)), "chat_turns")
        status, _, body = await panel.request("POST", "/api/ask", body={"text": "third", "client_id": "fresh-001"})
        assert status == 429 and body["error"] == "chat_cap"
        assert d.store.items_all(prefix="ask:") == []
    finally:
        await panel.server.stop()


NEAR = Finding(key="99887766554433aa", variant="deprecation", signal="repair", what="deprecated", domain="acme_pump",
               removal=f"{NOW.year}.2", affected=["integration acme_pump"], sources=["repair acme_pump:deprecated"],
               quote="Repair acme_pump:deprecated")  # fmt: skip


def plan_change(store: Store, item: str, run: str, *, state: str = "open") -> dict[str, Any]:
    plan_done(store, item, [run])
    files = [{"path": "automations/acme_pump.yaml", "new_sha256": "b" * 64}]
    return store.create_change(kind="change", subject="fix(automations): acme_pump", why="w", effect="e",
                               state=state, requested_by="admin-chat:u", now=NOW, run_id=run,
                               expires_at=NOW + timedelta(days=7), files=files,
                               diff="+++ b/automations/acme_pump.yaml\n+x")  # fmt: skip


async def test_reject_on_a_finding_with_its_own_dashboard_need_rejects_the_dashboard(
    h: Harness,  # noqa: F811
    local_ingress: None,  # noqa: F811
) -> None:
    d, panel = await started(h)
    try:
        assert d.store is not None
        store_findings(d.store, NEAR, installed=f"{NOW.year}.1.0")
        bound = {"url_path": "acme-pump", "paths": ["views[0].cards[1]"], "changes": 1, "after": "c" * 64}
        d.store.kv_set(maintenance.DASHBOARD_KEY + NEAR.key, json.dumps({"url_path": "acme-pump", "binding": bound}),
                       now=NOW)  # fmt: skip
        finding = d.store.item_by_key(f"maint:{NEAR.key}")
        assert finding is not None and finding["need"]["primary"]["id"] == "apply_dashboard"
        change = plan_change(d.store, finding["id"], "run-pump-1")
        await d.items_flush()
        finding = d.store.item(finding["id"])
        own = d.store.item_by_key(f"change:{change['id']}")
        assert finding is not None and own is not None and "linked" not in finding["source"]
        assert own["needs"] and own["need"]["primary"]["id"] == "apply"
        status, _, _ = await panel.request("POST", f"/api/items/{finding['id']}/act",
                                           body={"action": "reject", "revision": finding["revision"]})  # fmt: skip
        assert status == 200
        # The dashboard plan is gone; the config change is still open on its own item.
        assert not maintenance.dashboard_plan(d.store, NEAR.key)
        row = d.store.change(change["id"])
        assert row is not None and row["state"] == "open"
    finally:
        await panel.server.stop()


async def test_an_urgent_change_failure_asks_on_its_own_item_while_the_finding_asks(tmp_path: Path) -> None:
    from opsd.changes import UNCLEAN_KEY

    sent: list[Any] = []

    async def send(message: Any) -> str:
        sent.append(message)
        return "sent"

    store = Store.open(tmp_path / "ops.db")
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True), send=send)
    store_findings(store, NEAR, installed=f"{NOW.year}.1.0")
    finding = store.item_by_key(f"maint:{NEAR.key}")
    assert finding is not None and finding["needs"]
    with store.atomic():
        change = plan_change(store, finding["id"], "run-pump-2", state="failed")
        store.kv_set(f"{UNCLEAN_KEY}{change['id']}", "the restore failed", now=NOW)
    own = store.item_by_key(f"change:{change['id']}")
    assert own is not None and own["needs"] and own["need"]["kind"] == "ack" and own["source"]["urgent"]
    assert "may be half applied" in own["need"]["text"]
    book.flush()
    await book.deliver()
    assert any(m.tag == own["id"] and m.urgent for m in sent)
    assert book.ack(own, now=NOW)
    cleared = store.item(own["id"])
    assert cleared is not None and not cleared["needs"]
    store.close()


async def test_ok_on_a_finding_carrying_a_failed_change_clears_it(tmp_path: Path) -> None:
    from opsd.changes import UNCLEAN_KEY

    store = Store.open(tmp_path / "ops.db")
    book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: NOW, context=lambda: Context(acting=True))
    store_findings(store, SUGGESTION)
    finding = store.item_by_key(f"maint:{SUGGESTION.key}")
    assert finding is not None and not finding["needs"]
    with store.atomic():
        change = plan_change(store, finding["id"], "run-sugg-1", state="failed")
        store.kv_set(f"{UNCLEAN_KEY}{change['id']}", "the restore failed", now=NOW)
    carried = store.item(finding["id"])
    assert carried is not None and carried["needs"] and "may be half applied" in carried["need"]["text"]
    assert book.ack(carried, now=NOW)
    assert [r["id"] for r in store.items_all() if r["needs"]] == []
    store.close()
