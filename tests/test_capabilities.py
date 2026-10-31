"""The registry is the single declaration: schema, CLI, guard verbs and prompt table."""

import json
import re

import pytest

from opsd import capabilities, guard
from opsd.capabilities import (
    CAPABILITIES,
    Arg,
    Args,
    Capability,
    RequestError,
    build_parser,
    capability_table,
    cli_verbs,
    parse_request,
    request_verb,
    resolve_ref,
)
from opsd.policy import Effect
from opsd.profile import OVERLAYS, overlay_installed


def shipped(cap: Capability) -> bool:
    """False for a row whose optional overlay (`opsd.house`) this build does not ship."""
    return all(overlay_installed(name) for name in cap.requires if name in OVERLAYS)


def test_every_reference_resolves() -> None:
    for cap in filter(shipped, CAPABILITIES.values()):
        for ref in (cap.executor, cap.validator, cap.tier_resolver):
            if ref is not None:
                assert callable(resolve_ref(ref)), ref


def test_guard_parser_and_schema_are_generated() -> None:
    sub = next(a for a in build_parser()._actions if a.dest == "verb")
    assert (
        set(sub.choices) - {"help"}
        == cli_verbs()
        == {c.verb for c in CAPABILITIES.values() if c.enabled and not c.internal}
    )  # type: ignore[arg-type]
    assert cli_verbs() | {"--help", "-h", "help"} == guard.OPSCTL_VERBS
    assert {"ack", "notify"} <= cli_verbs()


def test_disabled_capability_is_not_offered_anywhere() -> None:
    off = Capability("x.secret", "secret", "s", "opsd.queries:cmd_status", enabled=False)
    registry = {**CAPABILITIES, off.name: off}
    assert "secret" not in cli_verbs(registry)
    with pytest.raises(RequestError, match="unknown verb"):
        parse_request(b'{"verb": "secret"}', registry)
    assert "secret" not in capability_table(["x.secret"], registry)
    with pytest.raises(SystemExit):
        build_parser(registry).parse_args(["secret"])


@pytest.mark.parametrize(
    "bad",
    [
        Capability("nodots", "v", "s", "opsd.queries:cmd_status"),
        Capability("a.b", "Bad Verb", "s", "opsd.queries:cmd_status"),
        Capability("a.b", "help", "s", "opsd.queries:cmd_status"),
        Capability("a.b", "v", "s", "os:system"),
        Capability("a.b", "v", "s", "opsd.queries:cmd_status", validator="builtins:eval"),
        Capability("a.b", "v", "s", "opsd.queries:cmd_status", args={"x": Arg(float)}),
        Capability("a.b", "v", "s", "opsd.queries:cmd_status", args={"x": Arg(bool, positional=True)}),
    ],
)
def test_registry_rejects_bad_rows(bad: Capability) -> None:
    with pytest.raises(ValueError, match=r"bad|duplicate|boolean"):
        capabilities._check_registry([bad])


def test_duplicate_rows_rejected() -> None:
    row = CAPABILITIES["agent.status"]
    with pytest.raises(ValueError, match="duplicate"):
        capabilities._check_registry([row, row])


def test_resolve_ref_refuses_outside_opsd() -> None:
    for ref in ("os:system", "opsd.queries", "opsd.queries:cmd_status; x", "builtins:eval"):
        with pytest.raises(ValueError, match="bad reference"):
            resolve_ref(ref)


def test_effect_arguments() -> None:
    _, args = parse_request(json.dumps({"verb": "notify", "args": {"title": "t", "message": "m"}}).encode())
    assert args.flag("urgent") is False
    _, args = parse_request(
        json.dumps({"verb": "ack", "args": {"key": "update:update.x@2026.10.4", "days": 99}}).encode()
    )
    assert args.number("days") == 30 and args.flag("accept") is False


@pytest.mark.parametrize(
    "request_obj",
    [
        {"verb": "ack", "args": {"key": "a b"}},
        {"verb": "ack", "args": {"key": "x" * 201}},
        {"verb": "ack", "args": {"key": "k", "accept": "true"}},
        {"verb": "notify", "args": {"title": "x" * 61, "message": "m"}},
        {"verb": "notify", "args": {"title": "t", "message": "a\x1bb"}},
        {"verb": "notify", "args": {"title": "t\nx", "message": "m"}},
        {"verb": "notify", "args": {"title": "t", "message": "ok", "commit": True}},
    ],
)
def test_effect_arguments_rejected(request_obj: dict[str, object]) -> None:
    with pytest.raises(RequestError):
        parse_request(json.dumps(request_obj).encode())


def test_args_accessors() -> None:
    args = Args({"n": 3, "s": "x", "b": True, "none": None})
    assert args.number("n") == 3 and args.text("s") == "x" and args.flag("b") and args.opt_text("none") is None
    assert not args.flag("missing") and args.replace(n=4).number("n") == 4 and args.to_json()["s"] == "x"
    with pytest.raises(TypeError):
        args.number("s")
    with pytest.raises(TypeError):
        args.text("n")
    with pytest.raises(TypeError):
        Args({"b": True}).number("b")


def test_request_verb_is_safe_to_log() -> None:
    assert request_verb(b'{"verb": "ack"}') == "ack"
    for raw in (b"[]", b"nope", b'{"verb": "rm -rf /"}', b'{"verb": 3}', b"\xff"):
        assert request_verb(raw) == "?"


def test_capability_table_lists_usage_and_tier() -> None:
    table = capability_table(["agent.status", "notify.admin", "ha.logs", "no.such"])
    assert "`opsctl status`" in table and "opsctl logs <source>" in table
    assert "at most 1 per run" in table and "no.such" not in table


def test_parser_shapes_match_v01() -> None:
    parser = build_parser()
    args = parser.parse_args(["logs", "core", "--lines", "5"])
    assert (args.source, args.lines) == ("core", 5)
    assert parser.parse_args(["git-log", "-n", "3"]).n == 3
    assert parser.parse_args(["health", "--diff"]).diff is True
    assert parser.parse_args(["ack", "--key", "k", "--accept"]).accept is True
    with pytest.raises(SystemExit):
        parser.parse_args(["logs", "kernel"])


def test_read_only_flag() -> None:
    assert all(c.read_only for c in CAPABILITIES.values() if Effect.READ in c.effects)
    assert not CAPABILITIES["baseline.ack"].read_only
    assert re.fullmatch(r"[a-z0-9_.]+", "baseline.ack")
