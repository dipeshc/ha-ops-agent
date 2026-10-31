"""The diagnostics export: what a bug report needs, as JSON the admin can read before sharing it.

Built from an allowlist, never by dumping state: versions, the options with every secret and the push
target reduced to "set"/"unset" and every list reduced to its length, what the house profile switches on
(counts, never names), store table counts, recent failures and a health summary. The few free-text fields
(problems, audit details, health source errors) pass through the agent's redactor and are truncated.
Entity ids, item keys, chat text, prompts and model output never leave.
"""

import dataclasses
import platform
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opsd import __version__
from opsd.config import Options
from opsd.jsonstore import read_json
from opsd.redact import Redactor
from opsd.store import StoreError

if TYPE_CHECKING:
    from opsd.daemon import Daemon

FORMAT = 1
RECENT = timedelta(days=7)
MAX_TEXT = 200
MAX_ERRORS = 20
# Failed or refused capability calls; the rest of the audit log is routine.
ERROR_OUTCOMES = frozenset({"error", "failed", "refused"})
# Option values that name a person or a device, kept as set/unset like secrets.
PERSONAL = frozenset({("notify", "service")})


def _text(redactor: Redactor, value: object) -> str:
    return redactor.redact(str(value))[:MAX_TEXT]


def options_summary(options: Options) -> dict[str, dict[str, Any]]:
    """Every add-on option by group: scalars as set, secrets and personal values as set/unset, lists as a count."""
    groups: dict[str, dict[str, Any]] = {}
    for group in dataclasses.fields(options):
        value = getattr(options, group.name)
        if not dataclasses.is_dataclass(value) or group.name == "profile":
            continue
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            item = getattr(value, f.name)
            check = f.metadata.get("check")
            if (check is not None and check.secret) or (group.name, f.name) in PERSONAL:
                out[f.name] = "set" if str(item).strip() else "unset"
            elif isinstance(item, (list, tuple, set, frozenset)):
                out[f.name] = {"count": len(item)}
            elif item is None or isinstance(item, (bool, int, float)):
                out[f.name] = item
            else:
                out[f.name] = str(item)[:64]
        groups[group.name] = out
    return groups


def profile_summary(options: Options, redactor: Redactor) -> dict[str, Any]:
    """What the house profile switches on, without naming anything in the house."""
    profile = options.profile
    return {
        "loaded": bool(profile.source),
        "problems": [_text(redactor, p) for p in options.problems if p.startswith("house profile")],
        "overlays": sorted(profile.overlays),
        "git": profile.git.enabled,
        "proposals": sorted(profile.git.proposals) if profile.git.enabled else [],
        "push_target": "set" if profile.notify_service else "unset",
        "health_entities": sum(bool(e) for e in (profile.health.power_entity, profile.health.remote_entity)),
        "need_approval": len(profile.updates.need_approval),
        "watched_entities": len(profile.updates.watched_entities),
        "fragile": len(profile.updates.fragile),
        "version_gates": len(profile.updates.version_gates),
        "reload_domains": len(profile.reload.domains),
        "reload_deny": len(profile.reload.deny),
        "docs": {k: bool(getattr(profile.docs, k)) for k in ("runbook", "facts")},
        "house_notes": bool(profile.house_notes),
    }


def _latest_snapshot(daemon: Daemon) -> Mapping[str, Any]:
    files = sorted(daemon.paths.snapshots_dir.glob("*.json")) if daemon.paths.snapshots_dir.is_dir() else []
    found = read_json(files[-1], {}) if files else {}
    return found if isinstance(found, dict) else {}


def _store_summary(daemon: Daemon, now: datetime, redactor: Redactor) -> dict[str, Any]:
    store = daemon.store
    if store is None:
        return {"available": False, "error": _text(redactor, daemon.store_error)}
    try:
        audit = [
            {"at": r.get("at"), "capability": r.get("capability"), "outcome": r.get("outcome"),
             "detail": _text(redactor, r.get("detail") or "")}
            for r in store.audit_rows(limit=500)
            if r.get("outcome") in ERROR_OUTCOMES and str(r.get("at") or "") >= (now - RECENT).isoformat()
        ][:MAX_ERRORS]  # fmt: skip
        return {
            "available": True,
            "schema": store.schema_version(),
            "tables": store.table_counts(),
            "needs_you": store.needs_count(),
            "failed_runs": store.recent_failed_runs(since=now - RECENT, limit=MAX_ERRORS),
            "audit_errors": audit,
        }
    except (StoreError, sqlite3.Error) as err:
        return {"available": False, "error": _text(redactor, f"{type(err).__name__}: {err}")}


def build(daemon: Daemon, *, now: datetime | None = None) -> dict[str, Any]:
    """The export. Safe to share once read: see the module docstring for what it holds."""
    now = now or datetime.now(UTC)
    redactor = daemon.redactor()
    options = daemon.options
    snapshot = _latest_snapshot(daemon)
    found_core = snapshot.get("core")
    core: Mapping[str, Any] = found_core if isinstance(found_core, dict) else {}
    status = daemon.status_view()
    items = daemon.open_items()
    severities: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for item in items:
        severities[item.severity] = severities.get(item.severity, 0) + 1
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    found_updates = status.get("updates")
    updates: Mapping[str, Any] = found_updates if isinstance(found_updates, dict) else {}
    return {
        "format": FORMAT,
        "generated_at": now.isoformat(timespec="seconds"),
        "versions": {
            "agent": __version__,
            "core": core.get("version"),
            "supervisor": core.get("supervisor_version"),
            "os": core.get("os_version"),
            "python": platform.python_version(),
            "arch": platform.machine(),
        },
        "options": options_summary(options),
        "options_valid": options.valid,
        "problems": [_text(redactor, p) for p in options.all_problems],
        "profile": profile_summary(options, redactor),
        "store": _store_summary(daemon, now, redactor),
        "health": {
            "last_check_at": status.get("last_health_at"),
            "last_result": status.get("last_result"),
            "last_run_status": status.get("last_run_status"),
            "core_down_since": status.get("core_down_since"),
            "core_state": core.get("state"),
            "snapshot_at": snapshot.get("taken_at"),
            "source_errors": [_text(redactor, e) for e in (snapshot.get("errors") or [])][:MAX_ERRORS],
            "open_items": {"total": len(items), "by_severity": severities, "by_kind": kinds},
            "selftest": _text(redactor, status.get("selftest", "")),
            "inbound": status.get("inbound"),
            "ai_state": status.get("ai_state"),
            "ai_credential": daemon.credential_kind(),
            "updates": {k: updates.get(k) for k in ("option", "paused", "effective")} if updates else None,
        },
    }
