"""Read capabilities behind `opsctl`, executed inside the privileged daemon.

The AI's `opsctl` is a thin client that sends a validated request over a
unix socket (see `rpc.py`); these executors run in a worker thread with the
daemon's privileges and its shared Supervisor client, receive typed `Args`
from the capability registry, and return data that `rpc` redacts and
size-caps.
"""

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from opsd import __version__, githistory, privacy
from opsd.api import LiveAPI
from opsd.baseline import Baseline, diff
from opsd.capabilities import Args
from opsd.health import collect, custom_domains, derive_custom_integrations, derive_entities
from opsd.jsonstore import read_json, read_jsonl
from opsd.runctx import ExecContext
from opsd.supervisor import ApiError, SupervisorClient

MAX_OUTPUT_CHARS = 40_000
MAX_LOG_LINES = 1000


async def _ws(client: SupervisorClient, msg_type: str, **fields: Any) -> Any:
    async with LiveAPI(client) as api:
        return await api.ws(msg_type, **fields)


def cmd_status(ctx: ExecContext, _: Args) -> Any:
    # The live version and mode win: state.json keeps whatever an earlier version last wrote there.
    state = {**read_json(ctx.paths.state_file, {}), "version": __version__, "mode": ctx.options.agent.mode}
    return {"state": state, "recent_runs": read_jsonl(ctx.paths.history_file)[-5:]}


def cmd_baseline(ctx: ExecContext, _: Args) -> Any:
    raw = read_json(ctx.paths.baseline_file, None)
    if raw is None:
        return {"baseline": None}
    b = Baseline.from_json(raw)
    return {
        "created_at": b.created_at,
        "unavailable_entities": len(b.unavailable),
        "failed_entries": b.failed_entries,
        "repairs": b.repairs,
        "known_log_patterns": len(b.log_fps),
        "custom_integrations_ok": b.custom_ok,
        "acks": b.acks,
    }


def cmd_health(ctx: ExecContext, args: Args) -> Any:
    options = ctx.options
    client = ctx.client

    async def go() -> Any:
        async with LiveAPI(client) as api:
            return await collect(api, ctx.paths, options, run_config_check=False)

    snapshot = asyncio.run(go())
    if args.flag("diff"):
        raw = read_json(ctx.paths.baseline_file, None)
        if raw is None:
            return {"error": "no baseline yet"}
        return [
            i.to_json()
            for i in diff(
                snapshot,
                Baseline.from_json(raw),
                datetime.now(UTC),
                expected_unavailable=frozenset(options.health.expected_unavailable),
            )
        ]
    data = snapshot.to_json()
    data["unavailable"] = {"count": len(snapshot.unavailable), "by_platform": _count_by_platform(snapshot.unavailable)}
    data["log_errors"] = {
        fp: {k: v[k] for k in ("logger", "level", "count", "template")} for fp, v in snapshot.log_errors.items()
    }
    return data


def _count_by_platform(unavailable: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for info in unavailable.values():
        counts[info["platform"]] = counts.get(info["platform"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def cmd_repairs(ctx: ExecContext, _: Args) -> Any:
    client = ctx.client
    issues = asyncio.run(_ws(client, "repairs/list_issues"))
    return {"core": issues.get("issues", []), "supervisor": client.supervisor("/resolution/info")}


def cmd_entities(ctx: ExecContext, args: Args) -> Any:
    client = ctx.client
    states = client.core("/states")
    entity = args.opt_text("entity")
    platform = args.opt_text("platform")
    limit = args.number("limit")
    registry = asyncio.run(_ws(client, "config/entity_registry/list"))
    if entity:
        # Same rules as history: refused privacy-sensitive entities, state/unit/name only.
        withheld = privacy.refusal(entity, privacy.find(registry, entity))
        if withheld is not None:
            return {"error": withheld}
        match = [s for s in states if s.get("entity_id") == entity]
        return privacy.describe(match[0], entity) if match else {"error": f"{entity} not found"}
    unavailable, counts = derive_entities(states, registry)
    if args.flag("unavailable"):
        down = {e: v for e, v in unavailable.items() if not platform or v["platform"] == platform}
        return {"count": len(down), "entities": dict(sorted(down.items())[:limit])}
    if platform:
        reg = {e["entity_id"]: e for e in registry}
        rows = [
            {"entity_id": s["entity_id"], "last_changed": s.get("last_changed"),
             "state": "(private)" if privacy.refusal(s["entity_id"], reg.get(s["entity_id"])) else s.get("state")}
            for s in states
            if reg.get(s["entity_id"], {}).get("platform") == platform
        ]  # fmt: skip
        return {"count": len(rows), "entities": rows[:limit]}
    return {"entities_by_platform": counts, "unavailable_by_platform": _count_by_platform(unavailable)}


def cmd_integrations(ctx: ExecContext, _: Args) -> Any:
    client = ctx.client
    states = client.core("/states")

    async def go() -> tuple[Any, Any]:
        async with LiveAPI(client) as api:
            return await api.ws("config/entity_registry/list"), await api.ws("config_entries/get")

    registry, entries = asyncio.run(go())
    unavailable, counts = derive_entities(states, registry)
    return derive_custom_integrations(custom_domains(ctx.paths.custom_components), counts, unavailable, entries)


def cmd_entries(ctx: ExecContext, args: Args) -> Any:
    domain = args.opt_text("domain")
    failed = args.flag("failed")
    entries = asyncio.run(_ws(ctx.client, "config_entries/get"))
    rows = [
        {k: e.get(k) for k in ("entry_id", "domain", "title", "state", "reason", "disabled_by", "source")}
        for e in entries
        if (not domain or e.get("domain") == domain) and (not failed or e.get("state") != "loaded")
    ]
    return {"count": len(rows), "entries": rows}


def cmd_updates(ctx: ExecContext, _: Args) -> Any:
    options = ctx.options
    client = ctx.client

    async def go() -> Any:
        async with LiveAPI(client) as api:
            return await collect(api, ctx.paths, options, run_config_check=False)

    return asyncio.run(go()).updates


def cmd_backups(ctx: ExecContext, _: Args) -> Any:
    data = ctx.client.supervisor("/backups")
    return [
        {k: b.get(k) for k in ("slug", "name", "date", "type", "size", "protected", "compressed")}
        for b in sorted(data.get("backups", []), key=lambda b: b.get("date", ""), reverse=True)
    ]


def cmd_logs(ctx: ExecContext, args: Args) -> Any:
    lines = max(2, min(MAX_LOG_LINES, args.number("lines")))
    identifier = args.opt_text("identifier")
    slug = args.opt_text("slug")
    grep = args.opt_text("grep")
    source = args.text("source")
    match source:
        case "core":
            path = "/core/logs"
        case "supervisor":
            path = "/supervisor/logs"
        case "host":
            path = f"/host/logs/identifiers/{identifier}" if identifier else "/host/logs"
        case "addon":
            if not slug or not re.fullmatch(r"[a-z0-9_]+", slug):
                return {"error": "--slug is required for addon logs"}
            path = f"/addons/{slug}/logs"
        case _:
            return {"error": f"unknown source {source}"}
    if identifier and not re.fullmatch(r"[A-Za-z0-9_.@-]+", identifier):
        return {"error": "invalid identifier"}
    text = ctx.client.logs(path, lines)
    out = text.splitlines()
    if grep:
        # Literal, case-insensitive: no regex, so no catastrophic backtracking.
        needle = grep.lower()
        out = [line for line in out if needle in line.lower()]
    return "\n".join(out[-lines:])


def cmd_system(ctx: ExecContext, _: Args) -> Any:
    client = ctx.client
    result: dict[str, Any] = {}
    for name, path in (
        ("core", "/core/info"),
        ("supervisor", "/supervisor/info"),
        ("os", "/os/info"),
        ("host", "/host/info"),
    ):
        try:
            info = client.supervisor(path)
        except ApiError as err:
            result[name] = {"error": str(err)[:200]}
            continue
        keep = ("version", "version_latest", "version_pending", "update_available", "state", "boot", "boot_timestamp",
                "disk_free", "disk_total", "disk_used", "channel", "healthy", "supported", "hostname")  # fmt: skip
        result[name] = {k: info.get(k) for k in keep if k in info}
    for name, path in (("core_stats", "/core/stats"), ("supervisor_stats", "/supervisor/stats")):
        try:
            stats = client.supervisor(path)
            result[name] = {k: stats.get(k) for k in ("cpu_percent", "memory_usage", "memory_limit", "memory_percent")}
        except ApiError as err:
            result[name] = {"error": str(err)[:200]}
    return result


def cmd_git_status(ctx: ExecContext, _: Args) -> Any:
    return githistory.status(ctx.paths.ha_config) or "clean"


def cmd_git_log(ctx: ExecContext, args: Args) -> Any:
    return githistory.log(ctx.paths.ha_config, args.number("n"), args.opt_text("path"), ctx.options.reads)


def cmd_git_diff(ctx: ExecContext, args: Args) -> Any:
    return githistory.diff(ctx.paths.ha_config, args.text("range"), args.opt_text("path"), ctx.options.reads)


def cmd_git_show(ctx: ExecContext, args: Args) -> Any:
    return githistory.show(ctx.paths.ha_config, args.text("spec"), ctx.options.reads)
