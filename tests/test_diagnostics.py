"""The diagnostics export: useful for a bug report, and nothing in it that should not be shared."""

import json
from datetime import UTC, datetime

from opsd import __version__, diagnostics
from opsd.daemon import Daemon
from tests.conftest import SAMPLE_PROFILE, make_options
from tests.test_daemon import Harness
from tests.test_panel import Panel, h, local_ingress, p  # noqa: F401

TOKEN = "sk-ant-oat01-Abcdefghijklmnop0123456789"
SERVICE = "mobile_app_jane_doe_phone"
SECRET_YAML_VALUE = "hunter22secret"  # in the fixture's secrets.yaml
EMAIL = "jane.doe@example.org"


def _audit(d: Daemon, outcome: str, detail: str) -> None:
    assert d.store is not None
    d.store.audit(outcome=outcome, detail=detail, at=datetime.now(UTC), run_id=None, principal="daemon",
                  channel="test", capability="integration.reload", args={"domain": "plex"}, tier=None,
                  decision="allow")  # fmt: skip


async def _daemon_with_history(h: Harness) -> Daemon:  # noqa: F811
    d = h.daemon(claude__token=TOKEN, notify__service=SERVICE, health__expected_unavailable=["light.porch"])
    await d.health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    await d.health_cycle("daily")
    _audit(d, "error", f"token={TOKEN} wifi {SECRET_YAML_VALUE} mail {EMAIL} rtsp://u:pw123456@cam/x")
    _audit(d, "ok", "routine, not exported")
    return d


async def test_the_export_holds_what_a_bug_report_needs(h: Harness) -> None:  # noqa: F811
    d = await _daemon_with_history(h)
    out = diagnostics.build(d)
    assert out["format"] == diagnostics.FORMAT and out["versions"]["agent"] == __version__
    assert out["options"]["agent"]["mode"] == "observe" and out["options"]["updates"]["window"] == "04:00"
    assert out["options"]["health"]["expected_unavailable"] == {"count": 1}
    assert out["profile"]["loaded"] is True and out["profile"]["git"] is True
    assert out["profile"]["watched_entities"] == len(SAMPLE_PROFILE.updates.watched_entities)
    store = out["store"]
    assert store["available"] and store["schema"] > 0 and store["tables"]["audit"] >= 2
    assert [a["outcome"] for a in store["audit_errors"]] == ["error"]
    assert out["health"]["last_result"] and out["health"]["open_items"]["total"] >= 1
    json.dumps(out)  # plain JSON


async def test_secrets_and_house_names_never_leave(h: Harness) -> None:  # noqa: F811
    d = await _daemon_with_history(h)
    out = diagnostics.build(d)
    text = json.dumps(out)
    assert out["options"]["claude"]["token"] == "set" and out["options"]["notify"]["service"] == "set"
    for leak in (TOKEN, SERVICE, "jane_doe", SECRET_YAML_VALUE, EMAIL, "pw123456",
                 # The profile's names: entity ids, integrations, the git author, the house notes.
                 *SAMPLE_PROFILE.updates.watched_entities, SAMPLE_PROFILE.notify_service, "acme_solar", "acme_pool",
                 SAMPLE_PROFILE.git.author_name, SAMPLE_PROFILE.git.author_email, "custom_components",
                 # Entities from the health check and item keys.
                 "light.kitchen", "light.porch", "entities:", "routine, not exported"):  # fmt: skip
        assert leak not in text, leak


async def test_a_missing_store_is_reported_not_raised(h: Harness) -> None:  # noqa: F811
    d = h.daemon(claude__token=TOKEN)
    d.store, d.store_error = None, f"locked by {TOKEN}"
    out = diagnostics.build(d)
    assert out["store"] == {"available": False, "error": "locked by ***"}


def test_every_option_group_is_summarised_and_secrets_are_set_or_unset() -> None:
    summary = diagnostics.options_summary(make_options(claude__token=""))
    assert set(summary) == {"agent", "llm", "claude", "chat", "notify", "health", "updates", "capabilities", "usage",
                            "limits"}  # fmt: skip
    assert summary["claude"]["token"] == "unset" and summary["notify"]["service"] == "unset"


async def test_the_panel_serves_it_to_the_admin(p: Panel) -> None:  # noqa: F811
    status, headers, body = await p.request("GET", "/api/diagnostics")
    assert status == 200 and headers["cache-control"] == "no-store"
    assert body["versions"]["agent"] == __version__
    assert "oat-token-abcdef" not in json.dumps(body)


async def test_failed_runs_are_listed_without_their_output(h: Harness) -> None:  # noqa: F811
    d = h.daemon(claude__token=TOKEN)
    await d.health_cycle("catch_up")
    h.fake.set_unavailable("light.kitchen", hours_ago=8)
    h.claude_status = "timeout"
    await d.health_cycle("daily")
    failed = diagnostics.build(d)["store"]["failed_runs"]
    assert failed and set(failed[0]) == {"at", "profile", "status"} and failed[0]["status"] == "timeout"
