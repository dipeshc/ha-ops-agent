"""The main chat and earlier conversations are never pruned,
summaries have no capabilities, and purged threads leave the live replay buffer."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import FakeHA
from tests.test_chat_ux import setup
from tests.test_daemon import Harness

OLD = datetime.now(UTC) - timedelta(days=40)


@pytest.fixture
def h(paths: Any, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


def test_the_main_chat_is_never_pruned(h: Harness) -> None:
    _d, a, store, _, _ = setup(h)
    main = a.main_thread()
    store.append_turn(main, role="admin", text="an old question", now=OLD)
    store.prune(now=datetime.now(UTC))
    assert store.get_thread(main) is not None
    assert [t.text for t in store.turns_page(main)] == ["an old question"]


def test_summary_run_still_has_no_capabilities() -> None:
    from opsd.runner import PROFILES, SUMMARY

    assert SUMMARY.capabilities == frozenset() and "title" not in PROFILES
