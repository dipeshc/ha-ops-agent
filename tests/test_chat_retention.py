"""An item's conversation goes with its item, summaries have no capabilities, and purged threads leave the live
replay buffer."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeHA, conversation
from tests.test_chat_ux import setup
from tests.test_daemon import Harness

OLD = datetime.now(UTC) - timedelta(days=40)


@pytest.fixture
def h(paths: Any, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


def test_a_conversation_goes_with_its_item(h: Harness) -> None:
    _d, a, store, _, _ = setup(h)
    thread = conversation(a)
    store.append_turn(thread, role="admin", text="an old question", now=OLD)
    store.prune(now=datetime.now(UTC))
    # The question is resolved for 40 days only: it and its conversation stay.
    assert store.get_thread(thread) is not None
    assert [t.text for t in store.turns_page(thread)] == ["an old question"]
    later = datetime.now(UTC) + timedelta(days=120)
    store.prune(now=later)
    # Its item resolved more than 90 days ago is gone, and its turns with it.
    assert store.get_thread(thread) is None
    assert store._all("SELECT id FROM turns") == []


def test_summary_run_still_has_no_capabilities() -> None:
    from opsd.runner import PROFILES, SUMMARY

    assert SUMMARY.capabilities == frozenset() and "title" not in PROFILES
