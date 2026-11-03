"""A run is charged its run time (never a share estimate); no "null" turns."""

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opsd.claude_runner import RunConfig, RunResult
from opsd.store import Store
from tests.conftest import FakeHA
from tests.test_assistant import ADMIN
from tests.test_daemon import Harness


def info(five: float, seven: float) -> dict[str, Any]:
    return {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": five},
                                                    "seven_day": {"utilization": seven}}}  # fmt: skip


@pytest.fixture
def h(paths: Any, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


async def test_a_run_is_charged_its_run_time_whatever_the_utilisation_says(h: Harness) -> None:
    d = h.daemon(claude__plan="max20")
    d.inbound.admin = ADMIN
    assert d.assistant is not None and d.store is not None and d.usage is not None

    async def run_claude(cfg: RunConfig, prompt: str, **_: Any) -> RunResult:
        # As seen live: whole percents that do not move during a short run.
        return RunResult(status="ok", turns=5, duration_s=11.3, rate_first=info(6, 28), rate_last=info(6, 28),
                         cost_usd=0.04, structured={"reply": "fine", "summary": ""})  # fmt: skip

    d.runner.run_claude = run_claude
    thread = d.assistant.main_thread()
    assert thread is not None
    from tests.test_chat_ux import say

    await say(d.assistant, thread, "hi")
    view = d.usage.view(datetime.now(UTC))
    assert view["run_time_5h_s"] == pytest.approx(11.3) and view["run_time_7d_s"] == pytest.approx(11.3)
    assert view["run_time_7d_cap_s"] == round(240 * 3600 * 0.05 * 0.8)
    assert view["account_5h_percent"] == 6.0 and view["account_7d_percent"] == 28.0
    # The reported cost is counted for the day (the optional daily cost cap reads it).
    assert view["cost_today_usd"] == pytest.approx(0.04) and view["daily_max_usd"] is None


def test_the_store_refuses_empty_agent_turns(tmp_path: Path) -> None:
    store = Store.open(tmp_path / "ops.db")
    try:
        store.create_thread("0123456789abcdef", principal="admin", channel="panel", now=datetime.now(UTC))
        for text in ("", "   "):
            with pytest.raises(ValueError, match="needs text"):
                store.append_turn("0123456789abcdef", role="agent", text=text, now=datetime.now(UTC))
    finally:
        store.close()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_closed_menu_never_renders_as_null_text() -> None:
    """Regression: `append(null)` rendered the word "null" under every agent reply."""
    from tests.test_panel_shell import _funcs, _run, _views

    result = _run(
        _views("{menuFor: null}")
        + _funcs("minutes")
        + 'const agent = V.turnEls({ id: 7, role: "agent", text: "Fine.", at: "2026-01-27T06:27:00Z",'
        ' meta: { duration_s: 11, push: "skipped_watching", sources: ["x"] } });'
        'const empty = V.turnEls({ id: 8, role: "agent", text: null, meta: {} });'
        "console.log(JSON.stringify({ n: agent.length, nulls: agent.filter((x) => x == null).length,"
        " empty: empty.length, null_text: agent.some((x) => x.textContent.includes('null')),"
        " s: [minutes(0), minutes(12), minutes(61), minutes(5760)] }));"
    )
    assert result == {"n": 2, "nulls": 0, "empty": 0, "null_text": False,
                      "s": ["0", "<1", "1", "96"]}  # fmt: skip
