"""Grouped add-on options: schema, translations, strict parsing that fails closed, and the panel notice."""

import re
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from opsd.baseline import Baseline, diff
from opsd.config import GROUPS, Options, Paths
from opsd.health import Snapshot
from opsd.jsonstore import write_json_atomic
from tests.conftest import ADDON_ROOT
from tests.test_daemon import Harness, h  # noqa: F401
from tests.test_panel import Panel, local_ingress, p  # noqa: F401

CONFIG = yaml.safe_load((ADDON_ROOT / "config.yaml").read_text())
TRANSLATIONS = yaml.safe_load((ADDON_ROOT / "translations" / "en.yaml").read_text())["configuration"]


def get(options: Options, dotted: str) -> Any:
    group, name = dotted.split(".")
    return getattr(getattr(options, group), name)


def _schema_check(spec: str) -> tuple[str, str]:
    match = re.fullmatch(r"(\w+)(?:\((.*)\))?(\?)?", spec)
    assert match is not None, spec
    return match.group(1), match.group(2) or ""


def test_schema_matches_the_dataclasses() -> None:
    schema, defaults = CONFIG["schema"], CONFIG["options"]
    groups = {k: v for k, v in schema.items() if isinstance(v, dict)}
    assert list(groups) == list(GROUPS)
    for group, cls in GROUPS.items():
        assert set(groups[group]) == {f.name for f in fields(cls)}, group
        for f in fields(cls):
            spec = groups[group][f.name]
            default = cls().__getattribute__(f.name)
            check = f.metadata.get("check")
            optional = (spec[0] if isinstance(spec, list) else spec).endswith("?")
            # Optional keys are absent from `options:` (the form hides them); required ones have a default there.
            # The token is the exception: optional (it may be empty) but shown.
            shown = f.name in defaults.get(group, {})
            assert optional != shown or (group, f.name) == ("claude", "token"), f"{group}.{f.name}"
            if isinstance(spec, list):
                assert isinstance(default, tuple)
                continue
            kind, arg = _schema_check(spec)
            if kind in ("int", "float"):
                low, high = (float(x) for x in arg.split(","))
                assert check is not None and (check.low, check.high) == (low, high), f"{group}.{f.name}"
            elif kind == "list":
                assert check is not None and check.choices == tuple(arg.split("|")), f"{group}.{f.name}"
            elif kind == "match":
                assert check is not None and f"^{check.pattern}$" == arg, f"{group}.{f.name}"
            elif kind == "password":
                assert check is not None and check.secret and not f.repr
            else:
                assert kind == "bool" and isinstance(default, bool), f"{group}.{f.name}"
    assert set(schema) == set(GROUPS), "no top-level options outside the groups"


def test_defaults_parse_cleanly_and_match_the_code() -> None:
    options = Options.from_mapping(CONFIG["options"])
    assert options.valid
    expected = Options()
    for group in GROUPS:
        for f in fields(GROUPS[group]):
            dotted = f"{group}.{f.name}"
            assert get(options, dotted) == get(expected, dotted), dotted
    # No house values in the add-on's defaults (they live in the house profile).
    assert options.updates.need_approval == () and options.notify.service == ""
    assert options.capabilities.reloadable_domains == ""
    assert not options.acting and not options.auto_updates_on


def test_translations_name_every_option() -> None:
    schema = CONFIG["schema"]
    assert set(TRANSLATIONS) == set(schema)
    for key, spec in schema.items():
        assert TRANSLATIONS[key]["name"] and TRANSLATIONS[key]["description"], key
        if isinstance(spec, dict):
            assert set(TRANSLATIONS[key]["fields"]) == set(spec), key
            for name, text in TRANSLATIONS[key]["fields"].items():
                assert text["name"] and text["description"], f"{key}.{name}"


def test_load_reads_options_json(paths: Paths) -> None:
    assert Options.load(paths) == Options()
    # Options as the Supervisor saves them: one plan set, the rest defaults.
    write_json_atomic(
        paths.options_file, CONFIG["options"] | {"claude": {**CONFIG["options"]["claude"], "plan": "max5"}}
    )
    loaded = Options.load(paths)
    assert loaded.valid and loaded.claude.plan == "max5" and loaded.agent.mode == "observe"
    paths.options_file.write_text("{not json")
    broken = Options.load(paths)
    assert broken.problems == ("options.json cannot be read (JSONDecodeError)",) and not broken.acting
    paths.options_file.write_text("[]")
    assert Options.load(paths).problems == ("options.json is not an object",)


# ------------------------------------------------------------------ strict checks


@pytest.mark.parametrize(
    ("raw", "problem"),
    [
        ({"agent": {"mode": "ACT"}}, "agent.mode must be one of observe, act"),
        ({"agent": "act"}, "agent must be a group of options"),
        ({"notify": {"max_pushes_per_day": 21}}, "notify.max_pushes_per_day must be between 0 and 20"),
        ({"notify": {"max_pushes_per_day": 2.5}}, "notify.max_pushes_per_day must be a whole number"),
        ({"notify": {"max_pushes_per_day": True}}, "notify.max_pushes_per_day must be a whole number"),
        ({"notify": {"service": "notify.mobile_app_x"}}, "notify.service must be in the form (mobile_app_[a-z0-9_]+)?"),
        ({"notify": {"quiet_start": "7:00"}}, "notify.quiet_start must be in the form"),
        ({"health": {"check_time": "24:00"}}, "health.check_time must be in the form"),
        ({"updates": {"min_age_days": 3}}, "updates.min_age_days must be between 7 and 90"),
        ({"usage": {"max_share_percent": 30}}, "usage.max_share_percent must be between 0.5 and 25"),
        ({"usage": {"max_share_percent": "5"}}, "usage.max_share_percent must be a number"),
        ({"claude": {"model_standard": "--dangerously-skip-permissions"}}, "claude.model_standard must be in the form"),
        ({"claude": {"model_standard": "Claude Sonnet"}}, "claude.model_standard must be in the form"),
        ({"claude": {"token": 12345}}, "claude.token must be text"),
        ({"llm": {"provider": "openai"}}, "llm.provider must be one of claude, none"),
        ({"limits": {"log_level": "trace"}}, "limits.log_level must be one of debug, info, warning, error"),
        ({"updates": {"never": "update.a"}}, "updates.never must be a list of strings"),
    ],
)
def test_bad_values_fail_closed(raw: dict[str, Any], problem: str) -> None:
    options = Options.from_mapping({"agent": {"mode": "act"}, "updates": {"auto_install": "on"}} | raw)
    assert len(options.problems) == 1 and options.problems[0].startswith(problem), options.problems
    assert not options.valid and not options.acting and not options.auto_updates_on


def test_problems_never_echo_values() -> None:
    options = Options.from_mapping({"claude": {"token": ["sk-ant-oat01-secret"]}, "chat": {"activity_days": 77},
                                    "notify": {"service": "mobile_app_SECRETVALUE"}})  # fmt: skip
    text = " ".join(options.problems)
    assert "secret" not in text.lower() and "SECRETVALUE" not in text and "77" not in text
    assert "sk-ant" not in repr(Options.from_mapping({"claude": {"token": "sk-ant-oat01-x"}}))


def test_tighten_only_lists_keep_their_valid_entries() -> None:
    options = Options.from_mapping({"updates": {"never": ["update.a", "light.kitchen", "update.b"]},
                                    "capabilities": {"never": ["notify.admin", "rm -rf"]}})  # fmt: skip
    assert options.updates.never == ("update.a", "update.b") and options.capabilities.never == ("notify.admin",)
    assert options.problems == ("updates.never: every entry must be in the form update\\.[a-z0-9_]{1,128}",
                                "capabilities.never: every entry must be in the form "
                                "[a-z][a-z0-9_]{0,40}\\.[a-z0-9_.]{1,60}")  # fmt: skip


def test_invalid_options_raise_a_health_item() -> None:
    now = datetime.now(UTC)
    snapshot = Snapshot(taken_at=now.isoformat(), backups={"newest_complete_age_h": 1, "newest_any_age_h": 1})
    baseline = Baseline(created_at=now.isoformat())
    assert not [i for i in diff(snapshot, baseline, now) if i.kind == "options"]
    problems = Options.from_mapping({"agent": {"mode": "ACT"}}).problems
    [item] = [i for i in diff(snapshot, baseline, now, options_problems=problems) if i.kind == "options"]
    assert item.key == "options:invalid" and item.severity == "warning" and "agent.mode" in item.summary


async def test_state_reports_the_provider_and_invalid_options(p: Panel) -> None:  # noqa: F811
    _, _, state = await p.request("GET", "/api/state")
    assert state["options"] is None
    p.d.options = Options.from_mapping({"agent": {"mode": "ACT"}})
    _, _, state = await p.request("GET", "/api/state")
    assert state["options"]["problems"] == ["agent.mode must be one of observe, act"]
    p.d.options = Options.from_mapping({"llm": {"provider": "none"}})
    _, _, state = await p.request("GET", "/api/state")
    assert state["options"]["provider"] == "AI is off: llm.provider is none"


def test_activity_days_option_reaches_the_activity_log(tmp_path: Path) -> None:
    from opsd.activity import ActivityLog
    from opsd.live import Hub
    from opsd.store import Store

    store = Store.open(tmp_path / "ops.db")
    try:
        log = ActivityLog(
            store, Hub(), context_days=Options.from_mapping({"chat": {"activity_days": 2}}).chat.activity_days
        )
        ActivityLog(store, Hub(), clock=lambda: datetime(2020, 1, 1, tzinfo=UTC)).record("check", "old")
        log.record("check", "new")
        text = log.context(UTC)
        assert "new" in text and "old" not in text
    finally:
        store.close()


async def test_a_health_check_with_invalid_options_reports_them_without_ai(h: Harness) -> None:  # noqa: F811
    await h.daemon(strict=False, agent__mode="ACT").health_cycle("catch_up")
    d = h.daemon(strict=False, agent__mode="ACT")
    assert not d.options.valid and not d.options.acting
    outcome = await d.health_cycle("daily")
    assert h.claude_calls == [] and outcome.result == "options_invalid"
    assert [i.key for i in d.open_items()] == ["options:invalid"]
