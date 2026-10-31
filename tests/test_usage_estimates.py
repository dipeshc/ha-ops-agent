"""Shares below Claude Code's whole-percent resolution are estimated (and labelled); no "null" turns."""

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opsd.budget import Caps, share_inconclusive
from opsd.claude_runner import RunConfig, RunResult
from opsd.store import Store
from tests.conftest import FakeHA, make_options
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


def test_a_zero_delta_is_inconclusive_not_zero() -> None:
    # As seen live on Claude Code 2.1.308: whole percents that do not move during a short run.
    assert share_inconclusive(info(6, 28), info(6, 28), "five_hour")
    assert not share_inconclusive(info(6, 28), info(7, 28), "five_hour")
    assert not share_inconclusive(None, info(6, 28), "five_hour")
    caps = Caps.from_options(_options(), detected_plan="max20")
    # 11 s of a max20 plan: 240 h a week, a fifth of it per 5-hour window.
    assert caps.estimated_share(11.0, "five_hour") == pytest.approx(11 / (240 * 3600 * 0.2))
    assert caps.estimated_share(11.0, "seven_day") == pytest.approx(11 / (240 * 3600))


def _options() -> Any:
    return make_options(claude__plan="auto")


async def test_runs_below_resolution_are_estimated_and_flagged(h: Harness) -> None:
    d = h.daemon(claude__plan="max20")
    d.inbound.admin = ADMIN
    assert d.assistant is not None and d.store is not None and d.usage is not None

    async def run_claude(cfg: RunConfig, prompt: str, **_: Any) -> RunResult:
        return RunResult(status="ok", turns=5, duration_s=11.3, rate_first=info(6, 28), rate_last=info(6, 28),
                         structured={"reply": "fine", "summary": ""})  # fmt: skip

    d.runner.run_claude = run_claude
    thread = d.assistant.main_thread()
    assert thread is not None
    from tests.test_chat_ux import say

    await say(d.assistant, thread, "hi")
    view = d.usage.view(datetime.now(UTC))
    assert view["agent_share_5h_estimated"] is True and view["agent_share_7d_estimated"] is True
    assert 0 < view["agent_share_5h_percent"] < 0.1  # was a misleading 0.0
    assert view["last_run"]["share_5h"] == pytest.approx(11.3 / (240 * 3600 * 0.2))


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
        + _funcs("share")
        + 'const agent = V.turnEls({ id: 7, role: "agent", text: "Fine.", at: "2026-01-27T06:27:00Z",'
        ' meta: { duration_s: 11, push: "skipped_watching", sources: ["x"] } });'
        'const empty = V.turnEls({ id: 8, role: "agent", text: null, meta: {} });'
        "console.log(JSON.stringify({ n: agent.length, nulls: agent.filter((x) => x == null).length,"
        " empty: empty.length, null_text: agent.some((x) => x.textContent.includes('null')),"
        " s: [share(0), share(0.0064, true), share(1.234), share(0.004, false, 2)] }));"
    )
    assert result == {"n": 2, "nulls": 0, "empty": 0, "null_text": False,
                      "s": ["0%", "<0.1% est.", "1.2%", "<0.01%"]}  # fmt: skip
