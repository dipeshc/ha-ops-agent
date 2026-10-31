"""Before/after health for updates: a light probe and the regression rule.

The probe reads only what the rule needs (Core state, entity states, the
entity registry, config entries, repairs), so it can run every minute or
so while an update settles. The rule is deliberately strict, because a
false "healthy" leaves the house broken at 04:00 while a false regression
only costs a rollback to the version that was running a minute ago:

- Core not running, or the API unreachable;
- any custom integration (`custom_components/`) with more unavailable
  entities or failed entries than before, or with all of its entities gone;
- a new failed config entry (any integration);
- a new unavailable entity in a key domain (locks, lights, covers, ...),
  or five or more new unavailable entities elsewhere;
- a new repair of severity error or critical (warnings are reported only;
  HACS's own "restart required" repair is expected after an install);
- a watched entity (the house profile's `updates.watched_entities`: here both
  Bluetooth locks and the alarm) that was available and is not;
- an add-on that was running and is not (add-on updates only);
- a probe that could not read something (fail closed).
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from opsd.health import HealthAPI, derive_custom_integrations, derive_entities, derive_failed_entries, derive_repairs

_LOGGER = logging.getLogger(__name__)

KEY_DOMAINS = frozenset(
    {"lock", "alarm_control_panel", "climate", "cover", "fan", "light", "switch", "water_heater", "valve", "siren"}
)
NEW_UNAVAILABLE_LIMIT = 5
REGRESSING_REPAIR_SEVERITIES = frozenset({"error", "critical"})
DOWN_STATES = frozenset({"unavailable", "unknown", "missing"})


@dataclass(frozen=True, slots=True)
class HealthPoint:
    at: str
    core_state: str | None = None
    core_version: str | None = None
    api_up: bool = False
    # entity_id -> platform
    unavailable: dict[str, str] = field(default_factory=dict)
    # custom domain -> {entities, unavailable, entries_failed}
    custom: dict[str, dict[str, int]] = field(default_factory=dict)
    # entry_id -> domain
    failed_entries: dict[str, str] = field(default_factory=dict)
    # "domain:issue_id" -> "severity|translation_key"
    repairs: dict[str, str] = field(default_factory=dict)
    # watched entity -> state ("missing" when absent)
    watched: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> HealthPoint:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


async def probe(api: HealthAPI, custom_domains: Iterable[str], *, watched: Iterable[str] = (),
                now: datetime | None = None) -> HealthPoint:  # fmt: skip
    """Read the facts the regression rule needs; anything unreadable is recorded in `errors`.

    `watched`: entities that must stay available across the update (the house profile's list).
    """
    errors: list[str] = []

    async def read(name: str, default: Any, coro: Any) -> Any:
        try:
            return await coro
        except Exception as err:  # noqa: BLE001 - recorded, and an error fails the comparison closed
            errors.append(f"{name}: {type(err).__name__}: {str(err)[:160]}")
            return default

    core = await read("core_info", {}, api.supervisor("/core/info"))
    states: list[dict[str, Any]] = await read("states", [], api.core_rest("/states"))
    registry: list[dict[str, Any]] = await read("entity_registry", [], api.ws("config/entity_registry/list"))
    entries: list[dict[str, Any]] = await read("config_entries", [], api.ws("config_entries/get"))
    issues = await read("repairs", {"issues": []}, api.ws("repairs/list_issues"))
    unavailable, counts = derive_entities(states, registry)
    custom = derive_custom_integrations(custom_domains, counts, unavailable, entries)
    by_id = {s.get("entity_id"): s for s in states}
    raw_issues = issues.get("issues", []) if isinstance(issues, dict) else []
    repairs = {
        key: f"{info.get('severity')}|{info.get('translation_key')}" for key, info in derive_repairs(raw_issues).items()
    }
    # The Supervisor's `/core/info` has no `state` (as of Supervisor 2026.10):
    # Core counts as running when its API answers with the entity states.
    reported = core.get("state") if isinstance(core, dict) else None
    core_state = str(reported) if reported else ("running" if states else "not responding")
    return HealthPoint(
        at=(now or datetime.now(UTC)).isoformat(timespec="seconds"),
        core_state=core_state,
        core_version=str(core.get("version")) if isinstance(core, dict) and core.get("version") else None,
        api_up=bool(states),
        unavailable={e: str(v["platform"]) for e, v in unavailable.items()},
        custom={
            d: {k: int(v.get(k, 0)) for k in ("entities", "unavailable", "entries_failed")} for d, v in custom.items()
        },
        failed_entries={e: str(v.get("domain")) for e, v in derive_failed_entries(entries).items()},
        repairs=repairs,
        watched={e: str(by_id[e].get("state")) if e in by_id else "missing" for e in watched},
        errors=errors,
    )


@dataclass(frozen=True, slots=True)
class Comparison:
    regressions: list[str]
    notes: list[str]

    @property
    def healthy(self) -> bool:
        return not self.regressions


def _expected_repair(key: str, value: str) -> bool:
    """HACS raises a "restart required" repair after installing an integration; the restart clears it."""
    return key.startswith("hacs:") and value.endswith("|restart_required")


def compare(
    before: HealthPoint,
    after: HealthPoint,
    *,
    expected_unavailable: frozenset[str] = frozenset(),
    addon_states: Mapping[str, tuple[str | None, str | None]] | None = None,
) -> Comparison:
    """Regressions (any one means roll back) and notes (reported only)."""
    regressions: list[str] = []
    notes: list[str] = []
    if after.errors:
        regressions.append("health could not be read: " + "; ".join(after.errors[:3]))
    if after.core_state != "running" or not after.api_up:
        regressions.append(f"Core is not running (state {after.core_state}, API up: {after.api_up})")

    for domain, was in sorted(before.custom.items()):
        now = after.custom.get(domain)
        if now is None:
            regressions.append(f"custom integration {domain} is gone")
            continue
        if now.get("entries_failed", 0) > was.get("entries_failed", 0):
            regressions.append(f"custom integration {domain}: config entry failed")
        if now.get("unavailable", 0) > was.get("unavailable", 0):
            more = now["unavailable"] - was.get("unavailable", 0)
            regressions.append(f"custom integration {domain}: {more} more entities unavailable")
        if was.get("entities", 0) > 0 and now.get("entities", 0) == 0:
            regressions.append(f"custom integration {domain}: all entities gone")

    for entry_id in sorted(set(after.failed_entries) - set(before.failed_entries)):
        regressions.append(f"config entry failed: {after.failed_entries[entry_id]}")

    new = sorted(set(after.unavailable) - set(before.unavailable) - expected_unavailable)
    key = [e for e in new if e.split(".", 1)[0] in KEY_DOMAINS]
    if key:
        regressions.append(f"{len(key)} key entities newly unavailable: {', '.join(key[:6])}")
    other = [e for e in new if e not in key]
    if len(other) >= NEW_UNAVAILABLE_LIMIT:
        regressions.append(f"{len(other)} entities newly unavailable: {', '.join(other[:6])}")
    elif other:
        notes.append(f"newly unavailable: {', '.join(other)}")

    for repair in sorted(set(after.repairs) - set(before.repairs)):
        value = after.repairs[repair]
        if _expected_repair(repair, value):
            continue
        severity = value.split("|", 1)[0]
        if severity in REGRESSING_REPAIR_SEVERITIES:
            regressions.append(f"new {severity} repair: {repair}")
        else:
            notes.append(f"new {severity} repair: {repair}")

    for entity, was_state in sorted(before.watched.items()):
        now_state = after.watched.get(entity, "missing")
        if was_state not in DOWN_STATES and now_state in DOWN_STATES:
            regressions.append(f"{entity} is {now_state} (was {was_state})")

    for slug, (was_addon, now_addon) in sorted((addon_states or {}).items()):
        if was_addon == "started" and now_addon != "started":
            regressions.append(f"add-on {slug} is {now_addon or 'unknown'} (was started)")
    return Comparison(regressions, notes)
