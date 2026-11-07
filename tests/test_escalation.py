"""The one escalation gate (spec items-and-pushes.md I22, I22a; health.md H16a-H17).

Three parts: the gate itself (store and composers), the regression corpus (every incident of 2026-11-04 to
2026-11-07, sanitised to example ids, each with Needs you empty, plus each reason of the list asking), and the
exhaustive path audit: every composer driven over the product of its kinds, severities, verdict states, deadline
states and ages, every item in Needs you checked against the reason it must have. A new path to Needs you fails
here until it is on the list.
"""

import ast
import itertools
import json
import logging
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opsd import daemon as daemon_mod
from opsd import escalation, verdicts
from opsd import maintenance as maint
from opsd.activity import standalone
from opsd.baseline import VACUUM_ROOMS_KIND
from opsd.changes import UNCLEAN_KEY
from opsd.escalation import BECAUSE, LADDER, Why
from opsd.items import Composer, Context, ItemBook
from opsd.live import Hub
from opsd.policy import Tier
from opsd.store import Store, UpdateJob
from opsd.updates import ABORT_FAILED_KEY
from tests.test_daemon import Harness, h  # noqa: F401

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
OPSD = Path(escalation.__file__).parent
# What a triage run read, by the daemon's own audit rows: a concrete read (verdicts.concrete).
READ = ["entity states light.example_lamp", "config entries"]
GATE_LOGGER = "opsd.escalation"
REFUSED = "need refused by the escalation gate"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    found = Store.open(tmp_path / "ops.db")
    yield found
    found.close()


class Book:
    """An item book over one store with a settable clock and health context."""

    def __init__(self, store: Store, now: datetime = NOW) -> None:
        self.store = store
        self.now = now
        self.items: list[dict[str, Any]] = []
        self.handled: list[dict[str, Any]] = []
        self.book = ItemBook(store, Hub(), tz=lambda: UTC, clock=lambda: self.now,
                             context=lambda: Context(health_at=self.now.isoformat(), health_items=self.items,
                                                     health_handled=self.handled, acting=True))  # fmt: skip

    def check(self, items: list[dict[str, Any]], *, at: datetime | None = None) -> None:
        """One health check: its open items, written as the daemon does."""
        self.now = at or self.now
        self.items = items
        self.book.write_health()

    def record(self, key: str) -> dict[str, Any]:
        found = self.store.item_by_key(key)
        assert found is not None, key
        return found


def needs(store: Store) -> list[dict[str, Any]]:
    return [r for r in store.items_all(open_only=True) if r["needs"]]


def health(key: str, kind: str, severity: str = "warning", summary: str = "", **detail: Any) -> dict[str, Any]:
    return {"key": key, "kind": kind, "severity": severity, "summary": summary or f"{kind} {key}", "detail": detail}


def verdict(store: Store, cause: str, fingerprint: str, state: str, *, by: str, now: datetime = NOW) -> None:
    """A verdict state for one cause, as the check would leave it."""
    match state:
        case "none":
            return
        case "no_answer":
            verdicts.no_answer(store, cause, fingerprint=fingerprint, by=by, now=now, failed=False)
        case "failed":
            verdicts.no_answer(store, cause, fingerprint=fingerprint, by=by, now=now, failed=True,
                               why="run_cap_reached")  # fmt: skip
        case "not_affected" | "fixed" | "fixable":
            verdicts.record(store, cause, fingerprint=fingerprint, verdict=state, reason="checked: expected here",
                            by=by, now=now, evidence=READ)  # fmt: skip
        case "needs_owner_grounded":
            verdicts.record(store, cause, fingerprint=fingerprint, verdict="needs_owner", reason="the hub lost power",
                            by=by, now=now, evidence=READ, owner_action="Plug the hub back in")  # fmt: skip
        case "needs_owner_hedged":
            verdicts.record(store, cause, fingerprint=fingerprint, verdict="needs_owner",
                            reason="the hub probably lost power", by=by, now=now, evidence=READ,
                            owner_action="Plug the hub back in")  # fmt: skip
        case "needs_owner_unread":
            verdicts.record(store, cause, fingerprint=fingerprint, verdict="needs_owner", reason="the hub lost power",
                            by=by, now=now, evidence=[], owner_action="Plug the hub back in")  # fmt: skip
        case _:
            raise AssertionError(state)


def health_verdict(store: Store, raw: dict[str, Any], state: str, now: datetime = NOW) -> None:
    verdict(store, f"health:{raw['key']}", verdicts.health_fingerprint(raw["key"], raw["summary"]), state,
            by="triage", now=now)  # fmt: skip


def assert_well_formed(record: dict[str, Any]) -> None:
    """A record in Needs you gives one reason of the list, the daemon's words for it, and, for a ladder reason,
    what was tried."""
    need = record["need"]
    why = escalation.why_of(need)
    assert why is not None, record["key"]
    assert need["because"] == BECAUSE[why]
    if why in LADDER:
        assert need["tried"] and all(isinstance(t, str) and t for t in need["tried"]), record["key"]


# ------------------------------------------------------------------------------------------------ the gate


def test_the_store_refuses_a_need_without_a_reason(store: Store, caplog: pytest.LogCaptureFixture) -> None:
    stamp = NOW.isoformat()
    base = {"id": "x-1", "key": "x:1", "kind": "check", "kicker": "", "ref": "", "title": "t", "gist": "",
            "revision": 1, "state": "waiting", "as_of": stamp, "at": stamp, "needs_since": stamp,
            "created_at": stamp, "updated_at": stamp}  # fmt: skip
    plain = {"kind": "ack", "text": "t", "primary": {"id": "ack", "label": "OK"}, "secondary": [], "expires_at": None}
    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        for refused in (plain, plain | {"why": "unknown"}, plain | {"why": "no_verdict"}, plain | {"why": "stuck"}):
            store.put_item(base | {"need": refused})
            record = store.item("x-1")
            assert record is not None and record["state"] == "proposed" and not record["needs"]
            assert record["need"] is None and record["needs_since"] is None
        # A failed record keeps its state, without the need.
        store.put_item(base | {"state": "failed", "need": plain})
        assert store.item("x-1")["need"] is None  # type: ignore[index]
    assert caplog.text.count(REFUSED) == 5
    # `stuck` is a ladder reason: it passes only with what was tried; `approval` needs nothing more.
    store.put_item(base | {"need": plain | {"why": "stuck", "tried": ["check: not decided"]}})
    record = store.item("x-1")
    assert record is not None and record["needs"] and record["need"]["because"] == BECAUSE[Why.STUCK]
    store.put_item(base | {"need": plain | {"why": "approval", "because": "the model's own words"}})
    record = store.item("x-1")
    # `because` is always the daemon's words for the reason, never the writer's.
    assert record is not None and record["needs"] and record["need"]["because"] == BECAUSE[Why.APPROVAL]


def test_no_need_is_made_outside_the_gate() -> None:
    """Every need is made by `escalation.need` (through `items.need` or `Composer._ask`) at one of these call sites,
    with its reason: a new site, or a need written as a literal, fails here until it is on the list and in the
    path audit below."""
    known = Counter({
        ("activity.py", "standalone", "Why.FAILED"): 1,
        ("items.py", "need", "why"): 1,
        ("items.py", "_ask", "why"): 1,
        ("items.py", "_update", "Why.APPROVAL"): 1,
        ("items.py", "_settled", "Why.APPROVAL"): 1,
        ("items.py", "_settled", "Why.FAILED"): 1,
        ("items.py", "window", "Why.FAILED"): 2,
        ("items.py", "_change", "Why.APPROVAL"): 1,
        ("items.py", "_change", "Why.FAILED"): 1,
        ("items.py", "_fork", "Why.APPROVAL"): 1,
        ("items.py", "_fork", "Why.FAILED"): 1,
        ("items.py", "_maintenance", "why"): 1,
        ("items.py", "health", "Why.CRITICAL"): 1,
        ("items.py", "health", "Why.PHYSICAL"): 1,
        ("items.py", "_by_hand", "Why.PHYSICAL"): 1,
        ("items.py", "_undecided", "Why.GROUNDED_NEEDS_OWNER"): 1,
        ("items.py", "_undecided", "Why.STUCK"): 1,
    })  # fmt: skip
    found: Counter[tuple[str, str, str]] = Counter()
    literals = []
    for path in sorted(OPSD.rglob("*.py")):
        tree = ast.parse(path.read_text())
        owner: dict[ast.AST, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for child in ast.walk(node):
                    owner.setdefault(child, node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "primary"
                                                  for k in node.keys):  # fmt: skip
                literals.append((path.name, owner.get(node, "<module>")))
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "need" and node.args:
                found[(path.name, owner.get(node, "<module>"), ast.unparse(node.args[0]))] += 1
            elif name == "_ask" and path.name == "items.py" and len(node.args) > 1:
                found[(path.name, owner.get(node, "<module>"), ast.unparse(node.args[1]))] += 1
    # The only literal need is the one `escalation.need` builds.
    assert literals == [("escalation.py", "need")]
    assert found == known


def test_a_need_an_earlier_version_composed_without_a_reason_leaves_needs_at_start(store: Store) -> None:
    """Records a version before the gate wrote stay in the store as they were; at start each one in Needs you passes
    the gate again: a health item is composed anew (Coming up), a standalone failure is `failed`."""
    book = Book(store)
    raw = health("log:0a1b2c3d4e5f", "log")
    book.check([raw])
    stamp = NOW.isoformat()
    old_need = {"kind": "ack", "text": "t", "primary": {"id": "ack", "label": "OK"}, "secondary": [],
                "expires_at": None}  # fmt: skip
    with store._tx() as conn:  # as 0.25.0 before the gate left them
        conn.execute("UPDATE items SET state = 'waiting', need = ? WHERE key = ?",
                     (json.dumps(old_need), f"health:{raw['key']}"))  # fmt: skip
        conn.execute("INSERT INTO items (id, key, kind, kicker, ref, title, state, need, as_of, at, needs_since,"
                     " created_at, updated_at) VALUES ('a-9', 'activity:9', 'backup', '', '',"
                     " 'Backup (self-heal): failed', 'failed', ?, ?, ?, ?, ?, ?)",
                     (json.dumps(old_need), stamp, stamp, stamp, stamp, stamp))  # fmt: skip
    assert store.needs_count() == 2
    book.book.recompose()
    assert book.record(f"health:{raw['key']}")["state"] == "proposed"
    [left] = needs(store)
    assert left["key"] == "activity:9" and left["need"]["why"] == "failed" and left["need"]["tried"]


# --------------------------------------------------------------------------------------- the regression corpus


def test_corpus_release_notes_for_integrations_never_ask(store: Store) -> None:
    """2026-11-04: fifteen release-note items in Needs you. A Core minor's breaking changes never ask, whatever
    their check says (M13e)."""
    book = Book(store)
    findings = []
    for n, state in zip(range(15), itertools.cycle(["none", "no_answer", "failed", "needs_owner_grounded",
                                                    "needs_owner_hedged", "fixable"]), strict=False):  # fmt: skip
        finding = maint.Finding(key=f"{n:016x}", variant="deprecation", signal="notes", what="changes in 2026.12",
                                domain=f"example_{n}", detail={"minor": "2026.12", "integrations": [f"example_{n}"]},
                                sources=["release notes 2026.12"])  # fmt: skip
        verdict(store, maint.cause(finding), finding.evidence, state, by="maintenance_plan")
        findings.append(finding)
    write_findings(store, findings)
    assert len([r for r in store.items_all() if r["kind"] == "maintenance"]) == 15
    assert needs(store) == [] and book.book.outbox == [] and book.book.digest is None


def test_corpus_a_core_minor_plan_saying_no_fix_needed_never_asks(store: Store) -> None:
    """2026-11-05: a Core-minor finding in Needs you with "No fix needed"."""
    Book(store)
    minor = maint.Finding(
        key="00000000000000aa",
        variant="deprecation",
        signal="notes",
        what="changes in 2026.12",
        domain="example_hub",
        detail={"minor": "2026.12", "integrations": ["example_hub"]},
    )
    repair = maint.Finding(key="00000000000000ab", variant="deprecation", signal="repair", what="deprecated YAML",
                           domain="example_hub", sources=["repair example_hub:deprecated_yaml"])  # fmt: skip
    for finding in (minor, repair):
        verdicts.record(store, maint.cause(finding), fingerprint=finding.evidence, verdict="needs_owner",
                        reason="nothing in the config uses it", by="maintenance_plan", now=NOW,
                        evidence=["automations.yaml"], owner_action="No fix needed")  # fmt: skip
    write_findings(store, [minor, repair])
    assert needs(store) == []


def test_corpus_a_hedged_needs_owner_about_trigger_syntax_never_asks(store: Store) -> None:
    """2026-11-06: a wrong, hedged `needs_owner` about trigger syntax. Hedged is unknown (M13g)."""
    Book(store)
    finding = maint.Finding(key="00000000000000ac", variant="deprecation", signal="repair", what="trigger syntax",
                            domain="example_hub", sources=["repair example_hub:trigger_syntax"])  # fmt: skip
    verdicts.record(
        store,
        maint.cause(finding),
        fingerprint=finding.evidence,
        verdict="needs_owner",
        reason="The trigger syntax probably changed; confirm after the update",
        by="maintenance_plan",
        now=NOW,
        evidence=["automations.yaml:12"],
        owner_action="Update the trigger to the new syntax",
    )
    write_findings(store, [finding])
    record = store.item_by_key(f"maint:{finding.key}")
    assert record is not None and not record["needs"] and needs(store) == []


async def test_a_library_error_with_its_devices_off_is_noise(h: Harness) -> None:  # noqa: F811
    """2026-11-07: a new ERROR from a cast library while two speakers are switched off (they were off in the
    baseline) and the entries stay loaded. No observable effect: no AI, no question, no push (H16b)."""
    for n in (1, 2):
        h.fake.states.append({"entity_id": f"media_player.example_speaker_{n}", "state": "unavailable",
                              "last_changed": "2026-01-01T00:00:00+00:00", "attributes": {}})  # fmt: skip
        h.fake.registry.append({"entity_id": f"media_player.example_speaker_{n}", "platform": "example_cast"})
    await h.daemon().health_cycle("catch_up")
    pushes = len(h.pushes())
    h.fake.system_log.append({"name": "examplecast.socket_client", "level": "ERROR", "count": 47,
                              "message": ["[Example Speaker(10.0.0.5):8009] Failed to connect to service, retrying"],
                              "first_occurred": NOW.timestamp()})  # fmt: skip
    d = h.daemon()
    await d.health_cycle("daily")
    assert d.store is not None and h.claude_calls == [] and len(h.pushes()) == pushes
    [item] = [r for r in d.store.items_all(open_only=True) if str(r["key"]).startswith("health:log:")]
    assert item["state"] == "proposed" and not item["needs"] and "no observable effect" in item["gist"]
    effect = next(f for f in item["facts"] if f["label"] == "Effect")
    assert effect["value"].startswith("none observed") and d.store.needs_count() == 0


async def test_lock_timeouts_with_the_locks_available_are_noise(h: Harness) -> None:  # noqa: F811
    """2026-11-07: 47 Bluetooth lock disconnect and timeout errors overnight while the lock stayed locked and
    available. Its integration's entities and entries are as in the baseline: noise, without AI."""
    h.fake.states.append({"entity_id": "lock.example_door", "state": "locked",
                          "last_changed": "2026-01-01T00:00:00+00:00", "attributes": {}})  # fmt: skip
    h.fake.registry.append({"entity_id": "lock.example_door", "platform": "example_ble"})
    await h.daemon().health_cycle("catch_up")
    pushes = len(h.pushes())
    h.fake.system_log += [
        {"name": "example_ble.push", "level": "ERROR", "count": 30, "message": ["Example Door: Disconnected"],
         "first_occurred": NOW.timestamp()},
        {"name": "example_ble.push", "level": "ERROR", "count": 17,
         "message": ["Example Door: Timed out while updating"], "first_occurred": NOW.timestamp()},
    ]  # fmt: skip
    d = h.daemon()
    await d.health_cycle("daily")
    assert d.store is not None and h.claude_calls == [] and len(h.pushes()) == pushes
    logs = [r for r in d.store.items_all(open_only=True) if str(r["key"]).startswith("health:log:")]
    assert len(logs) == 2 and not any(r["needs"] for r in logs) and d.store.needs_count() == 0
    for record in logs:
        effect = next(f for f in record["facts"] if f["label"] == "Effect")
        assert "example_ble's entities" in effect["value"]


async def test_an_error_with_an_effect_is_triaged(h: Harness) -> None:  # noqa: F811
    """An error whose integration lost an entity since the baseline has an effect: it is triaged and the effect is
    a fact on it. Triage answering no verdict at all (2026-11-07) leaves both items quiet in Coming up."""
    h.fake.states.append({"entity_id": "light.example_lamp", "state": "on",
                          "last_changed": "2026-01-01T00:00:00+00:00", "attributes": {}})  # fmt: skip
    h.fake.registry.append({"entity_id": "light.example_lamp", "platform": "example_hub"})
    await h.daemon().health_cycle("catch_up")
    pushes = len(h.pushes())
    h.fake.states[-1] = {
        "entity_id": "light.example_lamp",
        "state": "unavailable",
        "last_changed": (datetime.now(UTC) - timedelta(hours=8)).isoformat(),
        "attributes": {},
    }
    h.fake.system_log.append(
        {
            "name": "homeassistant.components.example_hub",
            "level": "ERROR",
            "count": 3,
            "message": ["Error fetching data from the hub"],
            "first_occurred": NOW.timestamp(),
        }
    )
    h.claude_reply = {"result": "ok", "summary": "checked", "verdicts": []}
    d = h.daemon()
    await d.health_cycle("daily")
    assert d.store is not None and len(h.claude_calls) == 1 and len(h.pushes()) == pushes
    [log] = [r for r in d.store.items_all(open_only=True) if str(r["key"]).startswith("health:log:")]
    effect = next(f for f in log["facts"] if f["label"] == "Effect")
    assert effect["value"].startswith("1 entity unavailable since the baseline (light.example_lamp)")
    assert d.store.needs_count() == 0
    for record in d.store.items_all(open_only=True):
        if str(record["key"]).startswith("health:"):
            assert record["state"] == "proposed" and "could not decide" in record["gist"]


async def test_corpus_a_failed_triage_run_never_asks_or_pushes(h: Harness) -> None:  # noqa: F811
    """A triage run that failed: no warning push, nothing in Needs you; each item says why it is undecided."""
    await h.daemon().health_cycle("catch_up")
    pushes = len(h.pushes())
    h.fake.entries.append({"entry_id": "hub1", "domain": "example_hub", "title": "Example Hub", "state": "setup_retry"})
    h.claude_status = "error"
    d = h.daemon()
    await d.health_cycle("daily")
    assert d.store is not None and len(h.claude_calls) == 1 and len(h.pushes()) == pushes
    record = d.store.item_by_key("health:entry:hub1")
    assert record is not None and not record["needs"] and d.store.needs_count() == 0
    assert "the agent's check failed and will run again by itself" in record["gist"]


def test_corpus_a_capped_check_never_asks(store: Store) -> None:
    """A check its daily allowance held back (or AI off, or the usage limit): Coming up, with the real reason."""
    book = Book(store)
    raw = health("entry:hub1", "config_entry", summary="example_hub is setup_retry")
    verdicts.no_answer(store, "health:entry:hub1", fingerprint=verdicts.health_fingerprint(raw["key"], raw["summary"]),
                       by="triage", now=NOW, failed=True, why="run_cap_reached")  # fmt: skip
    book.check([raw])
    record = book.record("health:entry:hub1")
    assert not record["needs"] and "waits for its daily allowance" in record["gist"] and needs(store) == []


async def test_corpus_triage_returning_only_hedged_not_affected_never_asks(h: Harness) -> None:  # noqa: F811
    """Triage answering only a hedged `not_affected` and reading nothing concrete: no verdict. The item is neither
    snoozed nor asking: it waits in Coming up."""
    await h.daemon().health_cycle("catch_up")
    h.fake.entries.append({"entry_id": "hub1", "domain": "example_hub", "title": "Example Hub", "state": "setup_retry"})
    h.claude_reply = {"result": "ok", "summary": "checked", "verdicts": [
        {"key": "entry:hub1", "verdict": "not_affected", "reason": "probably because the speaker is off"}]}  # fmt: skip
    d = h.daemon()
    await d.health_cycle("daily")
    assert d.store is not None
    record = d.store.item_by_key("health:entry:hub1")
    assert record is not None and record["state"] == "proposed" and not record["needs"]
    assert d.store.needs_count() == 0
    found = verdicts.load(d.store, "health:entry:hub1")
    assert found is None or not found.settled


def test_corpus_a_stale_log_finding_after_a_rebase_never_asks(store: Store) -> None:
    """2026-11-06: a log finding kept after the fork was rebased. A log line has no deadline, and a finding asks
    for a check only when Home Assistant vouches for it (a repair or the release notes)."""
    Book(store)
    finding = maint.Finding(key="00000000000000ad", variant="deprecation", signal="log", what="deprecated call",
                            domain="example_fork", removal="2026.12", sources=["Core log"],
                            quote="example_fork uses something removed in 2026.12")  # fmt: skip
    verdicts.record(store, maint.cause(finding), fingerprint=finding.evidence, verdict="needs_owner",
                    reason="the fork still calls it", by="plan_fix", now=NOW, evidence=["custom_components/x.py"],
                    owner_action="Rebase the fork again")  # fmt: skip
    write_findings(store, [finding])
    assert needs(store) == []


def test_each_reason_on_the_list_asks(store: Store) -> None:
    """The positive cases: each reason of the list does ask, and says so."""
    book = Book(store)
    old = NOW - timedelta(days=8)
    stuck = health("supervisor:example_issue", "supervisor_issue")
    book.check([stuck], at=old)
    owner = health("entry:hub1", "config_entry", summary="example_hub is setup_retry")
    health_verdict(store, owner, "needs_owner_grounded")
    book.check([health("config:invalid", "config", "critical"), owner, stuck,
                health("repair:example_hub:reauth_required", "repair", translation_key="reauth_required")],
               at=NOW)  # fmt: skip
    diff = "+++ b/automations.yaml\n+- id: example\n"
    store.create_change(kind="change", subject="fix(automations): example", why="", effect="", state="open",
                        requested_by="chat", now=NOW, expires_at=NOW + timedelta(days=3), diff=diff,
                        files=[{"path": "automations.yaml", "new_sha256": "0" * 64}],
                        summary=[{"path": "automations.yaml", "added": 1, "removed": 0,
                                  "diff_chars": len(diff)}])  # fmt: skip
    near = maint.Finding(
        key="00000000000000ae",
        variant="deprecation",
        signal="repair",
        what="deprecated option",
        domain="example_hub",
        removal="2026.12",
        sources=["repair example_hub:old_option"],
    )
    write_findings(store, [near])
    standalone_failure = standalone({"id": 7, "kind": "backup", "title": "Backup (self-heal): failed",
                                     "detail": "", "at": NOW.isoformat()}, needs_admin=True)  # fmt: skip
    store.put_item(standalone_failure)
    found = {escalation.why_of(r["need"]): r for r in needs(store)}
    assert set(found) == set(Why)
    for record in found.values():
        assert_well_formed(record)
    assert '"Plug the hub back in"' in found[Why.GROUNDED_NEEDS_OWNER]["need"]["text"]
    assert found[Why.STUCK]["key"] == f"health:{stuck['key']}"
    assert found[Why.DEADLINE]["key"] == f"maint:{near.key}"
    assert found[Why.PHYSICAL]["key"] == "health:repair:example_hub:reauth_required"


def test_an_undecided_warning_asks_only_once_it_is_stuck(store: Store) -> None:
    book = Book(store)
    raw = health("entry:hub1", "config_entry", summary="example_hub is setup_retry")
    start = NOW - timedelta(days=7)
    for day in range(7):
        book.check([raw], at=start + timedelta(days=day))
        record = book.record("health:entry:hub1")
        assert not record["needs"] and "could not decide" in record["gist"]
        assert f"asks you on {(start + timedelta(days=7)):%Y-%m-%d}" in record["gist"]
    book.check([raw], at=NOW)
    record = book.record("health:entry:hub1")
    assert record["needs"] and record["need"]["why"] == "stuck" and record["source"]["pushes"]
    assert any(t.startswith("check: not decided") for t in record["need"]["tried"])
    # It cleared: coming back later starts the clock again.
    book.check([], at=NOW + timedelta(days=1))
    book.check([raw], at=NOW + timedelta(days=2))
    assert not book.record("health:entry:hub1")["needs"]


def test_a_reauth_repair_asks_as_physical(store: Store) -> None:
    book = Book(store)
    book.check([health("repair:example_hub:reauth_required", "repair", translation_key="reauth_required"),
                health("entry:hub2", "config_entry", reason="Reauthentication required"),
                health("repair:example_hub:deprecated_yaml", "repair", translation_key="deprecated_yaml")])  # fmt: skip
    asking = {r["key"]: r for r in needs(store)}
    assert set(asking) == {"health:repair:example_hub:reauth_required", "health:entry:hub2"}
    assert all(r["need"]["why"] == "physical" for r in asking.values())
    assert any(t.startswith("Home Assistant says: reauth_required") for t in
               asking["health:repair:example_hub:reauth_required"]["need"]["tried"])  # fmt: skip


# ------------------------------------------------------------------------------------------ the ladder (I22a)


def test_a_fixable_finding_never_produces_needs_owner(store: Store) -> None:
    """A `fixable` check becomes a proposal: it asks only as that proposal's approval once its finding may ask,
    never as `grounded_needs_owner`, and a finding with an open proposal never asks on its own."""
    Book(store)
    far = maint.Finding(key="00000000000000af", variant="deprecation", signal="repair", what="deprecated YAML",
                        domain="example_hub", sources=["repair example_hub:deprecated_yaml"])  # fmt: skip
    near = maint.Finding(
        key="00000000000000b0",
        variant="deprecation",
        signal="repair",
        what="old option",
        domain="example_hub",
        removal="2026.12",
        sources=["repair example_hub:old_option"],
    )
    for finding, state in ((far, "fixable"), (near, "needs_owner_grounded")):
        verdict(store, maint.cause(finding), finding.evidence, state, by="maintenance_plan")
        diff = "+++ b/configuration.yaml\n+example_hub: {}\n"
        store.create_change(kind="change", subject=f"fix({finding.key})", why="", effect="", state="open",
                            requested_by=f"{maint.PROPOSER}{finding.key}", now=NOW,
                            expires_at=NOW + timedelta(days=60), diff=diff,
                            files=[{"path": "configuration.yaml", "new_sha256": "0" * 64}],
                            summary=[{"path": "configuration.yaml", "added": 1, "removed": 0,
                                      "diff_chars": len(diff)}])  # fmt: skip
    write_findings(store, [far, near])
    asking = needs(store)
    assert all(r["need"]["why"] != "grounded_needs_owner" for r in asking)
    # Only the near finding's proposal asks, as an approval, with the change ready.
    [proposal] = asking
    assert proposal["kind"] == "proposal" and proposal["need"]["why"] == "approval"
    assert proposal["need"]["primary"]["id"] == "apply"
    assert "waits for you" in str(store.item_by_key(f"maint:{near.key}")["gist"])  # type: ignore[index]
    waiting = next(r for r in store.items_all() if r["kind"] == "proposal" and not r["needs"])
    assert waiting["state"] == "proposed" and any(a["id"] == "apply" for a in waiting["actions"])


async def test_a_reloadable_entry_in_setup_retry_is_reloaded_before_any_ask(
    h: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    outcomes = [
        {"ok": True, "message": "Reloaded example_hub (1 of 1 entry)."},
        {"ok": False, "message": "Reloaded example_hub (1 of 1 entry). Problems: Example Hub is setup_retry."},
    ]

    async def reload(api: Any, store: Any, domain: str, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["reserve"]() and h.claude_calls == []  # before triage, within the daily quota
        calls.append(domain)
        return outcomes.pop(0) | {"domain": domain}

    monkeypatch.setattr(daemon_mod, "reload_domain", reload)
    await h.daemon().health_cycle("catch_up")
    pushes = len(h.pushes())
    h.fake.entries.append({"entry_id": "hub1", "domain": "example_hub", "title": "Example Hub", "state": "setup_retry"})
    d = h.daemon(agent__mode="act", capabilities__reloadable_domains="example_hub")
    await d.health_cycle("daily")
    assert d.store is not None and calls == ["example_hub"] and h.claude_calls == []
    # It came back: no item, no AI, no push; a quiet History line says what the daemon did.
    assert d.store.item_by_key("health:entry:hub1") is None and d.store.needs_count() == 0
    assert len(h.pushes()) == pushes
    assert any("before asking: it came back" in str(r["title"]) for r in d.store.items_all(prefix="activity:"))
    # It did not help: recorded on the item, handed to triage with it, and in what was tried if it asks.
    h.claude_reply = {"result": "ok", "summary": "checked", "verdicts": []}
    d.store.kv_set("reload.last", "{}", now=NOW)
    d = h.daemon(agent__mode="act", capabilities__reloadable_domains="example_hub")
    await d.health_cycle("manual")
    assert d.store is not None and calls == ["example_hub", "example_hub"] and len(h.claude_calls) == 1
    assert "reload example_hub: did not help" in h.claude_calls[0]
    record = d.store.item_by_key("health:entry:hub1")
    assert record is not None and not record["needs"]
    later = d.items._composer()  # type: ignore[union-attr]
    raw = next(i for i in later.ctx.health_items if i["key"] == "entry:hub1")
    assert any(t.startswith("reload example_hub: did not help") for t in raw["detail"]["tried"])


async def test_an_entry_the_daemon_may_not_reload_says_so(h: Harness) -> None:  # noqa: F811
    await h.daemon().health_cycle("catch_up")
    h.fake.entries.append({"entry_id": "hub1", "domain": "example_hub", "title": "Example Hub", "state": "setup_retry"})
    h.claude_reply = {"result": "ok", "summary": "checked", "verdicts": []}
    d = h.daemon()
    await d.health_cycle("daily")
    assert "reload example_hub: not permitted" in h.claude_calls[0]


# --------------------------------------------------------------------------------------------- the path audit


HEALTH_KINDS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "log": ("log:0a{tag}", "log", {}),
    "log_noise": ("log:0b{tag}", "log", {"effect": {"none": True, "domain": None}, "count": 47}),
    "log_effect": ("log:0c{tag}", "log", {"effect": {"none": False, "entities_count": 1,
                                                     "entities": ["light.example_lamp"]}}),
    "entities": ("entities:example_{tag}", "entities", {"entities": ["light.example_lamp"]}),
    "entry": ("entry:{tag}", "config_entry", {"domain": "example_hub", "reason": "timeout",
                                              "tried": ["reload example_hub: did not help"]}),
    "entry_reauth": ("entry:r{tag}", "config_entry", {"reason": "Reauthentication required"}),
    "repair": ("repair:example_hub:deprecated_{tag}", "repair", {"translation_key": "deprecated_yaml"}),
    "repair_reauth": ("repair:example_hub:reauth_{tag}", "repair", {"translation_key": "reauth_required"}),
    "access": ("access:lock.example_{tag}", "access", {}),
    "remote_watch": ("remote:down-{tag}", "remote", {"needs_after": (NOW + timedelta(hours=2)).isoformat()}),
    "remote_past": ("remote:past-{tag}", "remote", {"needs_after": (NOW - timedelta(hours=2)).isoformat()}),
    "power": ("power:undervoltage-{tag}", "power", {}),
    "vacuum": ("vacuum_rooms:vacuum.example_{tag}:0f", VACUUM_ROOMS_KIND, {}),
}  # fmt: skip
SEVERITIES = ("info", "warning", "critical")
VERDICT_STATES = ("none", "no_answer", "failed", "not_affected", "fixed", "needs_owner_grounded",
                  "needs_owner_hedged", "needs_owner_unread")  # fmt: skip
AGES = ("fresh", "old")


def expected_health(kind: str, severity: str, state: str, age: str) -> Why | None:
    """The one reason a health item may ask for, from what it is (spec items-and-pushes.md I10, health.md H16a-H16c)."""
    if severity == "info" or kind == "remote_watch":
        return None
    if severity == "critical":
        return Why.CRITICAL
    if kind == "vacuum" or kind in ("entry_reauth", "repair_reauth"):
        return Why.PHYSICAL
    if kind == "log_noise":
        return None
    if kind == "power":
        return Why.PHYSICAL if age == "old" else None
    if state == "needs_owner_grounded":
        return Why.GROUNDED_NEEDS_OWNER
    if state in ("not_affected", "fixed") and kind != "access":
        return None
    return Why.STUCK if kind == "remote_past" or age == "old" else None


@pytest.mark.parametrize("age", AGES)
@pytest.mark.parametrize("state", VERDICT_STATES)
def test_every_composed_health_need_names_a_reason_from_the_list(
    tmp_path: Path, state: str, age: str, caplog: pytest.LogCaptureFixture
) -> None:
    store = Store.open(tmp_path / "ops.db")
    book = Book(store)
    raws: dict[str, tuple[str, str]] = {}
    items = []
    for kind, severity in itertools.product(HEALTH_KINDS, SEVERITIES):
        pattern, raw_kind, detail = HEALTH_KINDS[kind]
        raw = health(pattern.format(tag=severity), raw_kind, severity, **detail)
        raws[f"health:{raw['key']}"] = (kind, severity)
        items.append(raw)
    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        if age == "old":
            book.check(items, at=NOW - timedelta(days=8))
        for raw in items:
            health_verdict(store, raw, state, now=NOW - timedelta(minutes=5))
        book.check(items, at=NOW)
    assert REFUSED not in caplog.text
    for key, (kind, severity) in raws.items():
        record = book.record(key)
        want = expected_health(kind, severity, state, age)
        assert record["needs"] == (want is not None), (key, state, age)
        if want is None:
            continue
        assert_well_formed(record)
        assert escalation.why_of(record["need"]) == want, (key, state, age)
        if want is Why.GROUNDED_NEEDS_OWNER:
            assert '"Plug the hub back in"' in record["need"]["text"]
        if kind == "entry" and want is not None:
            assert "reload example_hub: did not help" in record["need"]["tried"]
    store.close()


FINDINGS: dict[str, dict[str, Any]] = {
    "repair": {"variant": "deprecation", "signal": "repair", "sources": ["repair example_hub:old"]},
    "notes": {"variant": "deprecation", "signal": "notes", "named_removal": True},
    "minor": {"variant": "deprecation", "signal": "notes", "detail": {"minor": "2026.12"}},
    "log": {"variant": "deprecation", "signal": "log", "quote": "example_hub uses something deprecated"},
    "repository": {"variant": "deprecation", "signal": "repository"},
    "replacement": {"variant": "replacement", "signal": "replacement", "detail": {"risk": "low"}},
}
# Installed 2026.11.1 on 2026-11-07: a removal far off, within 30 days of its deadline, or installed already.
REMOVALS = {"none": None, "far": "2027.6", "near": "2026.12", "installed": "2026.11"}
FINDING_STATES = ("none", "no_answer", "failed", "not_affected", "fixable", "needs_owner_grounded",
                  "needs_owner_hedged")  # fmt: skip


def expected_finding(signal_kind: str, removal: str, state: str) -> Why | None:
    """The one reason a finding may ask for (maintenance.md M11, M13d, M13e)."""
    spec = FINDINGS[signal_kind]
    if spec["variant"] == "replacement" or state == "not_affected":
        return None
    checked = spec["signal"] in ("repair", "notes")
    if checked and state in ("no_answer", "needs_owner_hedged"):
        # An `unknown` short of its tries (a hedged answer is one): its check is still to come, it does not ask yet.
        return None
    dated = removal != "none" and (spec["signal"] == "repair" or spec.get("named_removal"))
    if dated and removal in ("near", "installed"):
        return Why.DEADLINE
    if checked and not dated and signal_kind != "minor" and state == "needs_owner_grounded":
        return Why.GROUNDED_NEEDS_OWNER
    return None


def write_findings(store: Store, findings: list[maint.Finding], installed: str = "2026.11.1") -> None:
    with store.atomic():
        store.kv_set(maint.META_KEY, json.dumps({"at": NOW.isoformat(), "installed": installed}), now=NOW)
        store.kv_set(maint.FINDINGS_KEY, json.dumps([f.to_json() for f in findings]), now=NOW)


def test_every_composed_maintenance_need_names_a_reason_from_the_list(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    Book(store)
    cases: dict[str, tuple[str, str, str]] = {}
    findings = []
    for n, (signal_kind, removal, state) in enumerate(itertools.product(FINDINGS, REMOVALS, FINDING_STATES)):
        spec = dict(FINDINGS[signal_kind])
        finding = maint.Finding(key=f"{n:016x}", what="something changes", domain="example_hub",
                                removal=None if signal_kind == "minor" else REMOVALS[removal], **spec)  # fmt: skip
        verdict(store, maint.cause(finding), finding.evidence, state, by="maintenance_plan")
        cases[f"maint:{finding.key}"] = (signal_kind, removal, state)
        findings.append(finding)
    with caplog.at_level(logging.WARNING, logger=GATE_LOGGER):
        write_findings(store, findings)
    assert REFUSED not in caplog.text
    for key, (signal_kind, removal, state) in cases.items():
        record = store.item_by_key(key)
        assert record is not None, key
        want = expected_finding(signal_kind, removal, state)
        assert record["needs"] == (want is not None), (signal_kind, removal, state)
        if want is not None:
            assert_well_formed(record)
            assert escalation.why_of(record["need"]) == want, (signal_kind, removal, state)


JOB_STATES = ("pending", "installing", "verifying", "rolling_back", "succeeded", "reboot_pending", "skipped",
              "failed_pending", "failed_verifying", "failed_reboot", "rolled_back", "rollback_failed")  # fmt: skip
PLAN_ITEMS = {
    "auto": {"tier": Tier.AUTO},
    "auto_held": {"tier": Tier.AUTO, "held": True},
    "soaking": {"tier": Tier.APPROVE, "waits": True},
    "age_unknown": {"tier": Tier.APPROVE, "waits": True, "age_unknown": True},
    "approve": {"tier": Tier.APPROVE},
}


def job_of(state: str) -> UpdateJob:
    stored, failed_from, detail = {
        "failed_pending": ("failed", "pending", "the install call failed"),
        "failed_verifying": ("failed", "verifying", "the version did not change"),
        "failed_reboot": ("failed", "rebooting", "after the reboot the old version still runs"),
    }.get(state, (state, None, "example detail"))
    target: dict[str, Any] = {"failed_from": failed_from} if failed_from else {}
    return UpdateJob(id=1, run_id=1, seq=1, entity_id="update.example_hub", kind="hacs", title="Example Hub",
                     from_version="1.0.0", to_version="1.1.0", target=target, state=stored, install_attempts=1,
                     rollback_attempts=0, detail=detail, regressions=["light.example_lamp unavailable"],
                     started_at=NOW.isoformat(), finished_at=NOW.isoformat(), updated_at=NOW.isoformat())  # fmt: skip


def test_every_composed_update_need_names_a_reason_from_the_list(store: Store) -> None:
    composer = Composer(store, Context(window="04:00"), NOW, UTC)
    live = {"update.example_hub@1.1.0": {"id": "a1", "state": "approved", "run_when": "window",
                                         "subject": "update.example_hub@1.1.0"}}  # fmt: skip
    for plan, job_state, approved in itertools.product(PLAN_ITEMS, (None, *JOB_STATES), (False, True)):
        item = {"entity_id": "update.example_hub", "title": "Example Hub", "installed": "1.0.0", "latest": "1.1.0",
                "kind": "hacs", "reason": "example reason", **PLAN_ITEMS[plan]}  # fmt: skip
        job = job_of(job_state) if job_state else None
        spec = composer._update("update.example_hub", "1.1.0", item, job, live if approved else {}, {}, None, None,
                                [job] if job else [])  # fmt: skip
        if spec is None or spec.need is None:
            continue
        state, need = escalation.gate(spec.state, spec.need)
        assert need is not None, (plan, job_state, approved)
        why = escalation.why_of(need)
        if why is Why.APPROVAL:
            assert need["kind"] == "approval", (plan, job_state)
            assert plan in ("approve", "soaking", "age_unknown") or job_state == "reboot_pending", (plan, job_state)
            assert job_state in (None, "reboot_pending", "skipped", "failed_pending", "failed_verifying",
                                 "failed_reboot"), job_state  # fmt: skip
            assert not (plan in ("soaking", "age_unknown") and job_state is None)
        else:
            assert why is Why.FAILED and job_state in ("failed_pending", "failed_verifying", "failed_reboot",
                                                       "rollback_failed"), (plan, job_state)  # fmt: skip
            assert need["tried"]
        assert state in ("waiting", "failed")
    # An automatic update with no job never asks; neither does one still soaking.
    for plan in ("auto", "soaking"):
        item = {"entity_id": "update.example_hub", "title": "Example Hub", "installed": "1.0.0", "latest": "1.1.0",
                "kind": "hacs", "reason": "r", **PLAN_ITEMS[plan]}  # fmt: skip
        spec = composer._update("update.example_hub", "1.1.0", item, None, {}, {}, None, None, [])
        assert spec is not None and spec.need is None


CHANGE_STATES = ("open", "applying", "reverting", "applied", "reverted", "failed", "rejected", "stale", "expired")


def test_every_composed_change_fork_window_and_entry_need_names_a_reason_from_the_list(store: Store) -> None:
    composer = Composer(store, Context(window="04:00", acting=True), NOW, UTC)
    observing = Composer(store, Context(window="04:00", acting=False), NOW, UTC)
    seen: set[Why] = set()

    def checked(spec: Any, allowed: set[Why]) -> None:
        if spec is None or spec.need is None:
            return
        state, need = escalation.gate(spec.state, spec.need)
        assert need is not None and state in ("waiting", "failed"), spec.key
        why = escalation.why_of(need)
        assert why in allowed, (spec.key, why)
        if why in LADDER:
            assert need["tried"], spec.key
        seen.add(why)

    diff = "+++ b/automations.yaml\n+x\n"
    for n, (state, unclean, complete, proposer) in enumerate(
        itertools.product(CHANGE_STATES, (False, True), (False, True), ("chat", f"{maint.PROPOSER}00000000000000ff")), 1
    ):
        if unclean:
            store.kv_set(f"{UNCLEAN_KEY}{n}", "the restore failed", now=NOW)
        row = {"id": n, "state": state, "subject": "fix(automations): example", "kind": "change",
               "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "requested_by": proposer,
               "files": [{"path": "automations.yaml", "new_sha256": "0" * 64}], "diff": diff if complete else "",
               "summary": [{"path": "automations.yaml", "added": 1, "removed": 0,
                            "diff_chars": len(diff) if complete else 999}], "detail": "x", "why": "", "effect": "",
               "expires_at": (NOW + timedelta(days=3)).isoformat()}  # fmt: skip
        spec = composer._change(row, {})
        checked(spec, {Why.APPROVAL} if state == "open" else {Why.FAILED} if state == "failed" and unclean else set())
    for state, acting in itertools.product(("open", "applying", "applied", "rolled_back", "failed", "rejected",
                                            "stale"), (False, True)):  # fmt: skip
        record = {"id": f"f{state}{acting}", "domain": "example_fork", "core": "2026.11.1",
                  "origin": "2026.10.0", "state": state, "hunks": [], "files": {}, "detail": "example detail",
                  "created_at": NOW.isoformat(), "updated_at": NOW.isoformat()}  # fmt: skip
        spec = (composer if acting else observing)._fork(record)
        allowed = ({Why.APPROVAL} if state == "open" and acting
                   else {Why.FAILED} if state in ("rolled_back", "failed") else set())  # fmt: skip
        checked(spec, allowed)
    for n, (state, aborted_by_failure) in enumerate(itertools.product(
            ("planned", "backing_up", "installing", "done", "aborted", "failed"), (False, True)), 100):  # fmt: skip
        if aborted_by_failure:
            store.kv_set(f"{ABORT_FAILED_KEY}{n}", "the backup failed", now=NOW)
        run = {"id": n, "mode": "act", "state": state, "trigger": "window", "window_day": "2026-11-07",
               "summary": "example", "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(),
               "finished_at": NOW.isoformat()}  # fmt: skip
        checked(composer.window(run), {Why.FAILED})
    for kind, needs_admin in itertools.product(("backup", "reload", "check", "notice", "investigation"),
                                               (False, True)):  # fmt: skip
        record = standalone({"id": 1, "kind": kind, "title": f"{kind}: failed", "detail": "", "at": NOW.isoformat()},
                            needs_admin=needs_admin)  # fmt: skip
        state, need = escalation.gate(record["state"], record["need"])
        if need is not None:
            assert needs_admin and kind not in ("notice", "investigation") and escalation.why_of(need) is Why.FAILED
            assert need["tried"]
            seen.add(Why.FAILED)
    assert seen == {Why.APPROVAL, Why.FAILED}


def test_every_needs_you_item_but_critical_and_approval_says_what_was_tried(store: Store) -> None:
    """Across the corpus's positive cases and the audits: a ladder reason never asks without its record (the gate
    refuses it), and the record is shown with the reason."""
    test_each_reason_on_the_list_asks(store)
    for record in needs(store):
        why = escalation.why_of(record["need"])
        if why in (Why.CRITICAL, Why.APPROVAL):
            continue
        assert record["need"]["tried"], record["key"]
    view = ItemBook(store, Hub(), tz=lambda: UTC, context=Context, clock=lambda: NOW).view(needs(store)[0])
    assert {"why", "because", "tried"} <= set(view["need"])
