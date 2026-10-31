"""Baseline of accepted state, and the diff that decides whether Claude runs."""

from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd.health import Snapshot, hours_since, parse_time

FLAP_HOURS = 6.0
LOG_GROWTH_FACTOR = 10
LOG_FP_TTL_DAYS = 30
# A complete backup (Home Assistant, its database and every add-on, any type) at least weekly.
BACKUP_COMPLETE_MAX_AGE_H = 7 * 24
BACKUP_ANY_MAX_AGE_H = 26
DISK_FREE_MIN_PCT = 15.0
MEM_AVAILABLE_MIN_MB = 600
SWAP_USED_MAX_PCT = 50.0
# Items the daemon pushes itself (once per distinct drift) and never hands to triage.
VACUUM_ROOMS_KIND = "vacuum_rooms"
DAEMON_PUSHED_KINDS = frozenset({VACUUM_ROOMS_KIND})


@dataclass(frozen=True, slots=True)
class Item:
    """One change worth a look. `key` is stable across runs so it can be acked."""

    key: str
    kind: str
    severity: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Baseline:
    created_at: str
    unavailable: list[str] = field(default_factory=list)
    failed_entries: list[str] = field(default_factory=list)
    repairs: list[str] = field(default_factory=list)
    resolution: list[str] = field(default_factory=list)
    log_fps: dict[str, dict[str, Any]] = field(default_factory=dict)
    updates_seen: dict[str, str] = field(default_factory=dict)
    custom_ok: dict[str, bool] = field(default_factory=dict)
    boot_timestamp: int | None = None
    oom_seen: int = 0
    # Access entities the admin accepted as exposed to Assist (panel only).
    accepted_exposures: list[str] = field(default_factory=list)
    acks: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Per vacuum, the fingerprint of the room drift already reported (each drift is reported once).
    vacuum_rooms_seen: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Baseline:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    def active_acks(self, now: datetime) -> set[str]:
        active: set[str] = set()
        for key, ack in self.acks.items():
            until = ack.get("until")
            if until is None or datetime.fromisoformat(until) > now:
                active.add(key)
        return active


def _custom_ok(info: dict[str, Any]) -> bool:
    """A custom integration is healthy if its entries load and not all entities are down."""
    if info.get("entries_failed"):
        return False
    entities = int(info.get("entities", 0))
    return not (entities and info.get("unavailable", 0) >= entities)


CRITICAL_ACK_MAX_DAYS = 3
# The model's own snooze of a warning ( a prompt-injected triage must not
# silence a warning for a month). Accepting and the admin's own snoozes are not capped.
WARNING_SNOOZE_MAX_DAYS = 7


def seed_exclusions(snapshot: Snapshot) -> dict[str, list[str]]:
    """What the first run refuses to accept: anything that would be critical."""
    custom = set(snapshot.custom_integrations)
    return {
        "custom_broken": sorted(d for d, i in snapshot.custom_integrations.items() if not _custom_ok(i)),
        "custom_unavailable": sorted(e for e, v in snapshot.unavailable.items() if v["platform"] in custom),
        "custom_failed_entries": sorted(e for e, v in snapshot.failed_entries.items() if v.get("domain") in custom),
        "critical_repairs": sorted(k for k, v in snapshot.repairs.items() if v.get("severity") == "critical"),
        "assist_exposed": sorted(snapshot.assist_exposed or {}),
    }


def seed(snapshot: Snapshot, now: datetime) -> Baseline:
    """Accept the current state (first run), except anything that would be critical.

    Custom integration outages, their failed entries and critical repairs stay
    out of the baseline so they are reported on the next run instead of being
    silently accepted.
    """
    excluded = seed_exclusions(snapshot)
    return Baseline(
        created_at=now.isoformat(timespec="seconds"),
        unavailable=sorted(set(snapshot.unavailable) - set(excluded["custom_unavailable"])),
        failed_entries=sorted(set(snapshot.failed_entries) - set(excluded["custom_failed_entries"])),
        repairs=sorted(set(snapshot.repairs) - set(excluded["critical_repairs"])),
        resolution=sorted(snapshot.resolution.get("issues", [])),
        log_fps={fp: {"count": v["count"], "last_seen": snapshot.taken_at} for fp, v in snapshot.log_errors.items()},
        updates_seen={u["entity_id"]: str(u["latest"]) for u in snapshot.updates},
        custom_ok=dict.fromkeys(snapshot.custom_integrations, True),
        boot_timestamp=snapshot.boot_timestamp,
        oom_seen=len(snapshot.oom_kills),
    )


# Home Assistant's encrypted automatic backups are Supervisor type "partial" on every install, so the Supervisor
# raises no_current_backup although complete backups exist. The issue is reported only
# when no complete encrypted backup finished in the last NO_CURRENT_BACKUP_HOURS, and then as critical.
NO_CURRENT_BACKUP = "no_current_backup:system:"
NO_CURRENT_BACKUP_HOURS = 26


def complete_backup_within(snapshot: Snapshot, hours: float) -> tuple[bool, str]:
    """Whether a complete, encrypted backup finished within `hours`, and the evidence either way."""
    backups = snapshot.backups or {}
    age = backups.get("newest_complete_age_h")
    if age is None:
        return False, "no complete backup found"
    if backups.get("newest_complete_encrypted") is False:
        return False, f"the newest complete backup ({age} h old) is not encrypted"
    if float(age) > hours:
        return False, f"the newest complete backup is {age} h old (more than {hours:g} h)"
    return True, f"a complete encrypted backup finished {age} h ago"


def diff(
    snapshot: Snapshot,
    baseline: Baseline,
    now: datetime,
    *,
    expected_unavailable: frozenset[str] = frozenset(),
    options_problems: tuple[str, ...] = (),
    guard_anomalies: tuple[str, ...] = (),
    backup_failure: str | None = None,
    power_entity: str = "",
    remote_entity: str = "",
) -> list[Item]:
    """Items that are new relative to the baseline and not acked.

    `expected_unavailable` lists entities the runbook documents as normally
    off or knowingly broken; they never raise an item. `power_entity` and
    `remote_entity` are the house profile's optional state watches.
    `options_problems` (refused add-on option values) raise one item every
    check until they are fixed; `guard_anomalies` (tool calls the guard hook
    never decided, or denials of calls it allowed, in a recent Claude run)
    raise one item while they are recent.
    """
    items: list[Item] = []
    custom = set(snapshot.custom_integrations)

    if snapshot.core.get("state") not in (None, "running") or not snapshot.core.get("api_up", True):
        items.append(
            Item(
                "core:down",
                "core",
                "critical",
                f"Core state {snapshot.core.get('state')}, API up: {snapshot.core.get('api_up')}",
            )
        )

    # Newly unavailable entities, grouped per integration. Recent flips are
    # skipped so a device rebooting during the check does not count, except
    # for custom integrations, where even a partial fresh outage is critical.
    new_by_platform: dict[str, list[str]] = {}
    known = set(baseline.unavailable) | expected_unavailable
    for entity_id, info in snapshot.unavailable.items():
        if entity_id in known:
            continue
        age = hours_since(info.get("last_changed"), now)
        # Only a hard `unavailable` on a custom integration skips the flap filter;
        # `unknown` (still settling after a restart) always waits it out.
        bypass = info["platform"] in custom and info.get("state") == "unavailable"
        if not bypass and age is not None and age < FLAP_HOURS:
            continue
        new_by_platform.setdefault(info["platform"], []).append(entity_id)
    for platform, entities in sorted(new_by_platform.items()):
        is_custom = platform in custom
        items.append(
            Item(
                f"entities:{platform}",
                "entities",
                "critical" if is_custom else "warning",
                f"{len(entities)} {'custom integration ' if is_custom else ''}{platform} entities newly unavailable",
                {"entities": sorted(entities), "count": len(entities), "custom": is_custom},
            )
        )

    for domain, info in sorted(snapshot.custom_integrations.items()):
        was_ok = baseline.custom_ok.get(domain, True)
        # Entities that are merely `unknown` and still inside the flap window
        # (e.g. right after a restart) do not make the integration "broken" yet.
        settling = sum(
            1
            for v in snapshot.unavailable.values()
            if v["platform"] == domain
            and v.get("state") != "unavailable"
            and (age := hours_since(v.get("last_changed"), now)) is not None
            and age < FLAP_HOURS
        )
        settled = {**info, "unavailable": int(info.get("unavailable", 0)) - settling}
        if was_ok and not _custom_ok(settled):
            items.append(
                Item(
                    f"custom:{domain}",
                    "custom_integration",
                    "critical",
                    f"custom integration {domain} broken: {info.get('unavailable')}/{info.get('entities')} "
                    f"entities unavailable, {info.get('entries_failed')} failed entries",
                    dict(info),
                )
            )

    for entry_id, info in sorted(snapshot.failed_entries.items()):
        if entry_id in baseline.failed_entries:
            continue
        is_custom = info.get("domain") in custom
        items.append(
            Item(
                f"entry:{entry_id}",
                "config_entry",
                "critical" if is_custom else "warning",
                f"{info.get('domain')} '{info.get('title')}' is {info.get('state')}",
                dict(info),
            )
        )

    for key, info in sorted(snapshot.repairs.items()):
        if key in baseline.repairs:
            continue
        severity = "critical" if info.get("severity") == "critical" else "warning"
        items.append(Item(f"repair:{key}", "repair", severity, f"new repair {key}", dict(info)))

    for issue in snapshot.resolution.get("issues", []):
        if issue in baseline.resolution:
            continue
        item = Item(f"supervisor:{issue}", "supervisor_issue", "warning", f"Supervisor issue {issue}")
        if issue == NO_CURRENT_BACKUP:
            fine, evidence = complete_backup_within(snapshot, NO_CURRENT_BACKUP_HOURS)
            if fine:
                continue
            item = replace(item, severity="critical",
                           summary=f"{item.summary} (the complete-backup check failed: {evidence})")  # fmt: skip
        items.append(item)
    for flag in ("unhealthy", "unsupported"):
        for reason in snapshot.resolution.get(flag, []):
            items.append(Item(f"supervisor:{flag}:{reason}", "supervisor_issue", "warning", f"system {flag}: {reason}"))

    for fp, info in sorted(snapshot.log_errors.items()):
        seen = baseline.log_fps.get(fp)
        if seen is None:
            items.append(Item(f"log:{fp}", "log", "warning", f"new {info['level']} from {info['logger']}", dict(info)))
        elif info["count"] >= LOG_GROWTH_FACTOR * max(1, int(seen.get("count", 1))):
            items.append(
                Item(
                    f"log:{fp}:growth",
                    "log",
                    "warning",
                    f"{info['logger']} error count grew {seen.get('count')} -> {info['count']}",
                    dict(info),
                )
            )

    for update in snapshot.updates:
        if baseline.updates_seen.get(update["entity_id"]) == str(update["latest"]):
            continue
        items.append(
            Item(
                f"update:{update['entity_id']}@{update['latest']}",
                "update",
                "info",
                f"{update['title']} {update['installed']} -> {update['latest']} ({update['tier']}: {update['reason']})",
                dict(update),
            )
        )

    items.extend(_assist_items(snapshot, baseline))
    items.extend(_vacuum_room_items(snapshot, baseline))
    items.extend(_local_patch_items(snapshot))
    items.extend(_threshold_items(snapshot, baseline))
    items.extend(_watched_items(snapshot, now, power_entity=power_entity, remote_entity=remote_entity))
    if options_problems:
        items.append(Item("options:invalid", "options", "warning",
                          "add-on options invalid, so act mode, automatic updates and AI are off: "
                          + "; ".join(options_problems)[:400], {"problems": list(options_problems)}))  # fmt: skip
    if guard_anomalies:
        items.append(Item("guard:anomaly", "agent", "warning",
                          "the guard hook check failed in a recent Claude run (did a Claude Code update change"
                          f" behaviour?): {'; '.join(guard_anomalies)}"[:400],
                          {"anomalies": list(guard_anomalies)}))  # fmt: skip
    if backup_failure is not None:
        items.append(Item("backup:run_failed", "backup", "warning",
                          f"the last backup the agent started failed: {backup_failure}"[:300]))  # fmt: skip
    acked = baseline.active_acks(now)
    return [i for i in items if i.key not in acked]


def _assist_items(snapshot: Snapshot, baseline: Baseline) -> list[Item]:
    """A lock, alarm or garage door exposed to Assist is always critical (invariant 3)."""
    if snapshot.assist_exposed is None:
        return [
            Item(
                "assist:check_failed",
                "assist_exposure",
                "warning",
                "could not verify that no lock, alarm or garage door is exposed to Assist",
            )
        ]
    accepted = set(baseline.accepted_exposures)
    return [
        Item(
            f"assist:{entity_id}",
            "assist_exposure",
            "critical",
            f"{entity_id} ({kind}) is exposed to Assist",
            {"entity_id": entity_id, "kind": kind},
        )
        for entity_id, kind in snapshot.assist_exposed.items()
        if entity_id not in accepted
    ]


def _vacuum_room_items(snapshot: Snapshot, baseline: Baseline) -> list[Item]:
    """One warning per vacuum whose rooms drifted from HA's area mapping, once per distinct drift."""
    items = []
    for entity_id, info in sorted(snapshot.vacuum_rooms.items()):
        fingerprint = info.get("fingerprint")
        if not fingerprint or baseline.vacuum_rooms_seen.get(entity_id) == fingerprint:
            continue
        detail = {k: info.get(k) for k in ("missing", "unmapped", "renamed", "lines")}
        items.append(Item(f"vacuum_rooms:{entity_id}:{fingerprint}", VACUUM_ROOMS_KIND, "warning",
                          str(info.get("summary") or "")[:600], {"entity_id": entity_id, **detail}))  # fmt: skip
    return items


def vacuum_rooms_seen(baseline: Baseline, snapshot: Snapshot) -> dict[str, str]:
    """Drifts reported so far: the current ones, plus earlier ones for a vacuum this check could not read."""
    seen = {
        entity_id: fingerprint
        for entity_id, fingerprint in baseline.vacuum_rooms_seen.items()
        if "error" in snapshot.vacuum_rooms.get(entity_id, {"error": "not read"})
    }
    for entity_id, info in snapshot.vacuum_rooms.items():
        if info.get("fingerprint"):
            seen[entity_id] = str(info["fingerprint"])
    return seen


REMOTE_DOWN_AFTER = timedelta(minutes=30)
# Remote access often drops for a while (the cloud relay, the internet): it asks the admin only after this long.
REMOTE_NEEDS_AFTER = timedelta(hours=6)


def _watched_items(snapshot: Snapshot, now: datetime, *, power_entity: str = "",
                   remote_entity: str = "") -> list[Item]:  # fmt: skip
    """Host under-voltage (needs someone at the host) and remote access down for more than 30 minutes.

    Both entities come from the house profile (`health.power_entity`, `health.remote_entity`); none, no items.
    """
    items: list[Item] = []
    power = (snapshot.watch_states.get(power_entity) or {}) if power_entity else {}
    if power.get("state") == "on":
        items.append(Item("power:undervoltage", "power", "warning",
                          "the host reports under-voltage: check its power supply and cable (this needs you at"
                          " the device)", {"entity_id": power_entity,
                                           "since": power.get("last_changed")}))  # fmt: skip
    remote = (snapshot.watch_states.get(remote_entity) or {}) if remote_entity else {}
    since = parse_time(str(remote.get("last_changed") or ""))
    if remote.get("state") == "off" and since is not None and now - since >= REMOTE_DOWN_AFTER:
        hours = (now - since).total_seconds() / 3600
        items.append(Item("remote:down", "remote", "warning",
                          f"remote access ({remote_entity}) has been off for {hours:.1f} h",
                          {"entity_id": remote_entity, "since": since.isoformat(timespec="seconds"),
                           "needs_after": (since + REMOTE_NEEDS_AFTER).isoformat(timespec="seconds")}))  # fmt: skip
    return items


def waits_quietly(item: Item, now: datetime) -> bool:
    """A health item that is shown but does not ask the admin yet (`needs_after` still ahead)."""
    after = parse_time(str(item.detail.get("needs_after") or ""))
    return after is not None and now < after


def _local_patch_items(snapshot: Snapshot) -> list[Item]:
    """Local hotfixes: missing ones are warnings, superseded ones a hint, an invalid list a warning."""
    found = snapshot.local_patches or {}
    if not found:
        return []
    if not found.get("ok", True):
        return [Item("local_patches:invalid", "local_patch", "warning",
                     f"the local patch list cannot be checked: {found.get('error')}")]  # fmt: skip
    items = []
    for patch_id, result in sorted((found.get("patches") or {}).items()):
        if result.get("state") == "missing":
            items.append(Item(f"local_patch:{patch_id}", "local_patch", "warning",
                              f"local hotfix missing: {patch_id} ({result.get('path')}); a HACS redownload probably"
                              f" removed it ({result.get('detail')})", dict(result)))  # fmt: skip
        elif result.get("state") == "superseded":
            items.append(Item(f"local_patch:{patch_id}:superseded", "local_patch", "info",
                              f"local hotfix {patch_id} is superseded ({result.get('detail')}): remove its entry from"
                              " docs/local-patches.yaml", dict(result)))  # fmt: skip
    return items


def _threshold_items(snapshot: Snapshot, baseline: Baseline) -> list[Item]:
    items: list[Item] = []
    backups = snapshot.backups
    complete_age = backups.get("newest_complete_age_h")
    if complete_age is None or complete_age > BACKUP_COMPLETE_MAX_AGE_H:
        what = (f"is {complete_age} h old (more than 7 days)" if complete_age is not None
                else "was not found")  # fmt: skip
        items.append(Item("backup:complete_stale", "backup", "warning",
                          f"newest complete backup (Home Assistant, database and every add-on) {what}",
                          dict(backups)))  # fmt: skip
    any_age = backups.get("newest_any_age_h")
    if any_age is None or any_age > BACKUP_ANY_MAX_AGE_H:
        items.append(Item("backup:stale", "backup", "warning", f"newest backup of any kind is {any_age} h old",
                          dict(backups)))  # fmt: skip
    unprotected = backups.get("unprotected") or []
    if unprotected:
        # A backup without encryption holds every secret of the house.
        items.append(Item("backup:unprotected", "backup", "warning",
                          f"{backups.get('unprotected_count', len(unprotected))} unencrypted backup(s) on /backup:"
                          f" {', '.join(map(str, unprotected))}"[:300],
                          {"names": list(unprotected)}))  # fmt: skip
    if snapshot.disk and snapshot.disk.get("free_pct", 100) < DISK_FREE_MIN_PCT:
        items.append(
            Item("disk:low", "disk", "warning", f"disk free {snapshot.disk['free_pct']}%", dict(snapshot.disk))
        )
    mem = snapshot.memory
    if mem and mem.get("available_mb", 10**6) < MEM_AVAILABLE_MIN_MB:
        items.append(Item("memory:low", "memory", "critical", f"memory available {mem['available_mb']} MB", dict(mem)))
    if mem and mem.get("swap_used_pct", 0) > SWAP_USED_MAX_PCT:
        items.append(Item("memory:swap", "memory", "warning", f"swap used {mem['swap_used_pct']}%", dict(mem)))
    rebooted = (
        baseline.boot_timestamp is not None
        and snapshot.boot_timestamp is not None
        and snapshot.boot_timestamp != baseline.boot_timestamp
    )
    if rebooted:
        items.append(
            Item(f"host:reboot:{snapshot.boot_timestamp}", "host", "warning", "host rebooted since last check")
        )
    # The kernel log window restarts at every boot.
    previous_ooms = 0 if rebooted else baseline.oom_seen
    if len(snapshot.oom_kills) > previous_ooms:
        new = snapshot.oom_kills[previous_ooms:]
        items.append(
            Item(
                f"host:oom:{snapshot.boot_timestamp}:{len(snapshot.oom_kills)}",
                "host",
                "critical",
                f"{len(new)} OOM kill(s): {', '.join(new)[:200]}",
                {"kills": new},
            )
        )
    check = snapshot.config_check
    if check and check.get("ok") is False:
        items.append(Item("config:invalid", "config", "critical", "configuration check failed", dict(check)))
    git = snapshot.git
    if git.get("dirty"):
        items.append(Item("git:dirty", "git", "info", f"{len(git['dirty'])} uncommitted path(s)", dict(git)))
    if git.get("ahead") or git.get("behind"):
        items.append(
            Item(
                "git:diverged",
                "git",
                "info",
                f"ahead {git.get('ahead')}, behind {git.get('behind')} origin/main",
                dict(git),
            )
        )
    return items


def advance(baseline: Baseline, snapshot: Snapshot, now: datetime) -> Baseline:
    """Fold a processed snapshot into the baseline.

    Recovered entities, entries and repairs drop out (so a recurrence counts
    as new), updates are remembered so each version is reported once, log
    fingerprints are refreshed and expire after 30 days unseen, and expired
    acks are removed.
    """
    log_fps = dict(baseline.log_fps)
    for fp, info in snapshot.log_errors.items():
        previous = log_fps.get(fp, {})
        log_fps[fp] = {"count": max(int(previous.get("count", 0)), int(info["count"])), "last_seen": snapshot.taken_at}
    cutoff = now - timedelta(days=LOG_FP_TTL_DAYS)
    log_fps = {fp: v for fp, v in log_fps.items() if datetime.fromisoformat(v["last_seen"]).astimezone(UTC) >= cutoff}
    return Baseline(
        created_at=baseline.created_at,
        unavailable=sorted(set(baseline.unavailable) & set(snapshot.unavailable)),
        failed_entries=sorted(set(baseline.failed_entries) & set(snapshot.failed_entries)),
        repairs=sorted(set(baseline.repairs) & set(snapshot.repairs)),
        resolution=sorted(set(baseline.resolution) & set(snapshot.resolution.get("issues", []))),
        log_fps=log_fps,
        updates_seen={u["entity_id"]: str(u["latest"]) for u in snapshot.updates},
        # Only a user ack may record a custom integration as known-broken.
        custom_ok={
            d: True if _custom_ok(i) else baseline.custom_ok.get(d, True)
            for d, i in snapshot.custom_integrations.items()
        },
        boot_timestamp=snapshot.boot_timestamp,
        oom_seen=len(snapshot.oom_kills),
        # An exposure that was removed and comes back is reported again.
        accepted_exposures=sorted(
            set(baseline.accepted_exposures) & set(snapshot.assist_exposed or {})
            if snapshot.assist_exposed is not None
            else set(baseline.accepted_exposures)
        ),
        acks={k: v for k, v in baseline.acks.items() if k in baseline.active_acks(now)},
        # A drift that resolves drops out, so a later one is reported again.
        vacuum_rooms_seen=vacuum_rooms_seen(baseline, snapshot),
    )


def apply_ack(
    baseline: Baseline,
    item: Item,
    *,
    reason: str,
    days: int,
    accept: bool,
    now: datetime,
    by: str = "claude",
) -> Baseline:
    """Ack an item for `days`; `accept` folds permanent conditions into the baseline.

    Claude may only snooze a critical item for a few days and may never
    accept one; that takes the user (via the admin panel).
    """
    days = max(1, min(30, days))
    if item.severity == "critical" and by != "user":
        accept = False
        days = min(days, CRITICAL_ACK_MAX_DAYS)
    if item.severity == "warning" and by != "user" and not accept:
        days = min(days, WARNING_SNOOZE_MAX_DAYS)
    baseline.acks[item.key] = {
        "reason": reason[:300],
        "until": (now + timedelta(days=days)).isoformat(timespec="seconds"),
        "acked_at": now.isoformat(timespec="seconds"),
        "by": by,
        "accepted": accept,
    }
    if not accept:
        return baseline
    match item.kind:
        case "entities":
            baseline.unavailable = sorted(set(baseline.unavailable) | set(item.detail.get("entities", [])))
        case "config_entry":
            baseline.failed_entries = sorted({*baseline.failed_entries, item.key.removeprefix("entry:")})
        case "repair":
            baseline.repairs = sorted({*baseline.repairs, item.key.removeprefix("repair:")})
        case "custom_integration":
            baseline.custom_ok[item.key.removeprefix("custom:")] = False
        case "assist_exposure" if item.detail.get("entity_id"):
            baseline.accepted_exposures = sorted({*baseline.accepted_exposures, str(item.detail["entity_id"])})
        case "log":
            fp = item.key.split(":")[1]
            baseline.log_fps[fp] = {"count": int(item.detail.get("count", 1)), "last_seen": now.isoformat()}
        case _:
            pass
    return baseline
