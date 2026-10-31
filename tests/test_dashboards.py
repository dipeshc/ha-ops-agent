"""The one dashboard write: approval-only, scoped to the approved dashboard, never a generated one, backed up first
and restored on failure. Every dashboard here is made up."""

import copy
import os
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opsd import dashboards, selfguard
from opsd.approvals import Approvals
from opsd.capabilities import CAPABILITIES, CapabilityError, RequestError, parse_request
from opsd.dashboards import DashboardWriter, Edit, apply_edit, binding, card_types, digest, parse_edit
from opsd.policy import PolicyTier
from opsd.profile import parse_text
from opsd.store import Store
from tests.conftest import make_options

ADMIN = "admin-user-id"
BOARD = "example-energy"
GENERATED = "example-generated"


def config() -> dict[str, Any]:
    return {"views": [{"title": "Home", "sections": [{"type": "grid", "cards": [
        {"type": "custom:acme-gauge", "entity": "sensor.acme_power", "max_value": 5000},
        {"type": "vertical-stack", "cards": [
            {"type": "custom:acme-gauge", "entity": "sensor.acme_solar", "max_value": 3000, "needle": True},
            {"type": "entities", "entities": ["sensor.acme_power"], "max_value": 1},
        ]},
    ]}]}]}  # fmt: skip


EDIT = Edit("custom:acme-gauge", None, (("max_value", "max"),))


class FakeLovelace:
    """Storage dashboards behind the websocket; every command passes the real self-guard first."""

    def __init__(self) -> None:
        self.boards: dict[str, Any] = {BOARD: config(), GENERATED: config()}
        self.saves: list[tuple[str, Any]] = []
        self.fail_save = 0
        self.corrupt_save = False

    async def ws(self, msg_type: str, **fields: Any) -> Any:
        selfguard.check_ws(msg_type, fields)
        if msg_type == "lovelace/config":
            return copy.deepcopy(self.boards[fields["url_path"]])
        if msg_type == "lovelace/config/save":
            if self.fail_save:
                self.fail_save -= 1
                raise RuntimeError("save refused by Core")
            self.saves.append((fields["url_path"], fields["config"]))
            saved = copy.deepcopy(fields["config"])
            if self.corrupt_save:
                saved["views"][0]["title"] = "Changed by someone else"
            self.boards[fields["url_path"]] = saved
            return None
        raise AssertionError(msg_type)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "ops.db")
    yield opened
    opened.close()


def writer(store: Store, tmp_path: Path, fake: FakeLovelace, **options: Any) -> DashboardWriter:
    profile = parse_text(f"dashboards:\n  generated: [{GENERATED}]\n")
    opts = make_options(profile=profile, **({"agent__mode": "act"} | options))
    return DashboardWriter(store=store, options=opts, approvals=Approvals(store), ws=fake.ws,
                           backup_dir=tmp_path / "data" / "maintenance" / "dashboards")  # fmt: skip


async def approved(dw: DashboardWriter, url_path: str = BOARD, edit: Edit = EDIT) -> str:
    current = await dw.read(url_path) if url_path != GENERATED else config()
    bound = binding("0123456789abcdef", url_path, current, edit)
    assert bound is not None
    found = dw.approvals.request_dashboard(bound, title="t", user=ADMIN)
    now_bound, _ = await dw.binding_now(bound)
    state = dw.approvals.confirm_dashboard(found["id"], user=ADMIN, binding_now=now_bound or bound)
    assert state == "approved"
    return str(found["id"])


# ------------------------------------------------------------------ the edit


def test_card_types_count_nested_cards_but_not_views_or_sections() -> None:
    assert card_types(config()) == {"custom:acme-gauge": 2, "vertical-stack": 1, "entities": 1}


def test_apply_edit_renames_only_on_cards_of_the_type_and_keeps_everything_else() -> None:
    new, changed = apply_edit(config(), EDIT)
    cards = new["views"][0]["sections"][0]["cards"]
    assert cards[0] == {"type": "custom:acme-gauge", "entity": "sensor.acme_power", "max": 5000}
    assert cards[1]["cards"][0]["max"] == 3000 and cards[1]["cards"][0]["needle"] is True
    # Another card type keeps its own key of the same name.
    assert cards[1]["cards"][1]["max_value"] == 1
    assert len(changed) == 2 and all("max_value → max" in c for c in changed)
    retyped, changed = apply_edit(config(), Edit("custom:acme-gauge", "gauge", ()))
    assert retyped["views"][0]["sections"][0]["cards"][0]["type"] == "gauge" and len(changed) == 2


def test_apply_edit_never_overwrites_a_key_that_already_has_the_new_name() -> None:
    board = {"views": [{"cards": [{"type": "custom:acme-gauge", "max_value": 1, "max": 2}]}]}
    new, changed = apply_edit(board, EDIT)
    assert new == board and changed == []
    assert binding("0123456789abcdef", BOARD, board, EDIT) is None


@pytest.mark.parametrize("raw", [
    {"card_type": "custom:acme-gauge"},  # nothing to do
    {"card_type": "custom:acme-gauge", "renames": [{"old": "type", "new": "kind"}]},
    {"card_type": "custom:acme-gauge", "renames": [{"old": "max", "new": "max"}]},
    {"card_type": "custom:acme-gauge", "renames": [{"old": "a b", "new": "c"}]},
    {"card_type": "custom:acme-gauge", "renames": [{"old": "a", "new": "b"}, {"old": "a", "new": "c"}]},
    {"card_type": "custom:acme-gauge", "renames": [{"old": f"k{i}", "new": f"n{i}"} for i in range(9)]},
    {"card_type": "<script>", "renames": [{"old": "a", "new": "b"}]},
    {"card_type": "custom:acme-gauge", "new_type": "custom:acme-gauge"},
    {"card_type": "custom:acme-gauge", "new_type": 3},
    "custom:acme-gauge",
])  # fmt: skip
def test_parse_edit_refuses_anything_off_the_narrow_shape(raw: Any) -> None:
    assert parse_edit(raw) is None


# ------------------------------------------------------------------ the guard


def test_the_save_is_refused_outside_an_approved_scope() -> None:
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_ws("lovelace/config/save", {"url_path": BOARD, "config": {}})
    for kind in (
        "lovelace/dashboards/create",
        "lovelace/dashboards/update",
        "lovelace/dashboards/delete",
        "lovelace/resources/create",
        "lovelace/resources/delete",
        "lovelace/config/delete",
    ):
        with pytest.raises(selfguard.SelfTargetError):
            selfguard.check_ws(kind, {"url_path": BOARD})
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_ws("lovelace/config", {"url_path": BOARD, "force": True})
    selfguard.check_ws("lovelace/config", {"url_path": BOARD})
    selfguard.check_ws("lovelace/dashboards/list", {})


def test_the_scope_lets_exactly_one_save_of_its_own_dashboard_through() -> None:
    with selfguard.approved_dashboard_write(BOARD, generated=frozenset()):
        for fields in ({"url_path": "example-other", "config": {}}, {"url_path": BOARD, "config": {}, "force": 1},
                       {"url_path": BOARD, "config": "{}"}):  # fmt: skip
            with pytest.raises(selfguard.SelfTargetError):
                selfguard.check_ws("lovelace/config/save", fields)
        selfguard.check_ws("lovelace/config/save", {"url_path": BOARD, "config": {}})
        with pytest.raises(selfguard.SelfTargetError):
            selfguard.check_ws("lovelace/config/save", {"url_path": BOARD, "config": {}})
    with pytest.raises(selfguard.SelfTargetError):
        selfguard.check_ws("lovelace/config/save", {"url_path": BOARD, "config": {}})


@pytest.mark.parametrize("url_path", ["", GENERATED, "Example-Energy", "../x", "energy", "a" * 101])
def test_the_scope_is_never_opened_for_the_default_a_generated_or_a_malformed_dashboard(url_path: str) -> None:
    with pytest.raises(selfguard.SelfTargetError):  # noqa: SIM117 - the scope itself must refuse
        with selfguard.approved_dashboard_write(url_path, generated=frozenset({GENERATED})):
            pass


def test_dashboard_save_is_an_approval_only_daemon_capability() -> None:
    cap = CAPABILITIES["dashboard.save"]
    assert cap.internal and cap.tier is PolicyTier.APPROVE and not cap.read_only
    with pytest.raises(RequestError):
        parse_request(b'{"verb": "dashboard-save", "args": {}}')
    with pytest.raises(CapabilityError):
        dashboards.exec_internal(None, None)  # type: ignore[arg-type]


def test_profile_key_dashboards_generated_is_strict() -> None:
    assert parse_text("dashboards:\n  generated: [example-generated]\n").dashboards.generated == {"example-generated"}
    for bad in ("dashboards:\n  generated: [Board-X]\n", "dashboards:\n  generated: [board]\n",
                "dashboards:\n  other: [x-y]\n"):  # fmt: skip
        with pytest.raises(ValueError):
            parse_text(bad)
    assert parse_text("").dashboards.generated == frozenset()


# ------------------------------------------------------------------ the writer


async def test_an_approved_plan_is_backed_up_saved_and_read_back(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    dw = writer(store, tmp_path, fake)
    approval = await approved(dw)
    outcome = await dw.execute(approval)
    assert outcome.state == "applied", outcome.detail
    assert fake.saves and fake.saves[0][0] == BOARD
    assert fake.boards[BOARD]["views"][0]["sections"][0]["cards"][0]["max"] == 5000
    backups = list((tmp_path / "data" / "maintenance" / "dashboards" / BOARD).glob("*.json"))
    assert len(backups) == 1 and stat.S_IMODE(os.stat(backups[0]).st_mode) == 0o600
    assert digest(__import__("json").loads(backups[0].read_text())) == digest(config())
    # Single use: the same approval never saves again.
    assert (await dw.execute(approval)).state == "refused" and len(fake.saves) == 1


async def test_a_dashboard_that_changed_after_the_approval_is_never_written(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    dw = writer(store, tmp_path, fake)
    approval = await approved(dw)
    fake.boards[BOARD]["views"][0]["title"] = "Edited in the UI"
    outcome = await dw.execute(approval)
    assert outcome.state == "stale" and fake.saves == []
    assert store.approval(approval)["state"] == "void"


async def test_a_failed_read_back_restores_the_backup(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    fake.corrupt_save = True
    dw = writer(store, tmp_path, fake)
    approval = await approved(dw)
    fake_restore = fake.ws

    async def restore_cleanly(msg_type: str, **fields: Any) -> Any:
        if msg_type == "lovelace/config/save" and len(fake.saves) == 1:
            fake.corrupt_save = False
        return await fake_restore(msg_type, **fields)

    dw._ws = restore_cleanly
    outcome = await dw.execute(approval)
    assert outcome.state == "restored" and len(fake.saves) == 2
    assert digest(fake.boards[BOARD]) == digest(config())


async def test_a_failed_save_and_a_failed_restore_escalate(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    fake.fail_save = 2
    dw = writer(store, tmp_path, fake)
    outcome = await dw.execute(await approved(dw))
    assert outcome.state == "escalated" and "restore" in outcome.detail and ".json" in outcome.detail
    assert digest(fake.boards[BOARD]) == digest(config())


async def test_observe_mode_and_a_never_option_refuse_even_an_approved_plan(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    observe = writer(store, tmp_path, fake, agent__mode="observe")
    act = writer(store, tmp_path, fake)
    approval = await approved(act)
    assert (await observe.execute(approval)).state == "refused"
    never = writer(store, tmp_path, fake, capabilities__never=["dashboard.save"])
    assert (await never.execute(approval)).state == "refused"
    assert fake.saves == []


async def test_a_generated_dashboard_is_refused_even_with_a_forged_approval(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    dw = writer(store, tmp_path, fake)
    bound = binding("0123456789abcdef", GENERATED, config(), EDIT)
    assert bound is not None
    found = dw.approvals.request_dashboard(bound, title="t", user=ADMIN)
    store.decide_approval(found["id"], expect=("pending",), to="approved", now=datetime.now(UTC), by=ADMIN)
    outcome = await dw.execute(found["id"])
    assert outcome.state == "refused" and "generated" in outcome.detail and fake.saves == []


async def test_confirmation_needs_the_admin_who_asked_and_an_unchanged_dashboard(store: Store, tmp_path: Path) -> None:
    fake = FakeLovelace()
    dw = writer(store, tmp_path, fake)
    bound = binding("0123456789abcdef", BOARD, config(), EDIT)
    assert bound is not None
    found = dw.approvals.request_dashboard(bound, title="t", user=ADMIN)
    assert dw.approvals.confirm_dashboard(found["id"], user="someone-else", binding_now=bound) == "refused"
    moved = binding("0123456789abcdef", BOARD, {"views": [{"cards": [{"type": "custom:acme-gauge",
                                                                      "max_value": 9}]}]}, EDIT)  # fmt: skip
    assert dw.approvals.confirm_dashboard(found["id"], user=ADMIN, binding_now=moved) == "void"
    # An update approval path never accepts a dashboard approval.
    assert dw.approvals.confirm(found["id"], user=ADMIN, current=None) in ("refused", "unknown")
