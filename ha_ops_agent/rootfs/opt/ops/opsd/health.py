"""Deterministic health snapshot: collection plus pure derivation functions."""

import asyncio
import logging
import re
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from opsd import policy
from opsd.config import Options, Paths
from opsd.fingerprint import fingerprint, normalize
from opsd.safegit import git_argv, git_env
from opsd.supervisor import ApiError

_LOGGER = logging.getLogger(__name__)

# `ha core check` takes 30-90 s on a small host; the client's 30 s default timed out and read as an invalid config.
CONFIG_CHECK_TIMEOUT_S = 180.0

# What counts as "down" for the newly-unavailable and custom-integration checks.
#
# `unavailable` always means the integration or device cannot be reached.
# `unknown` only means "no value yet", which is the normal resting state of
# many entities, especially right after a Core or host restart:
# - stateless domains whose state is the time of the last press/event/run
#   (button, input_button, event, scene, notify, tts, stt, conversation,
#   ai_task, wake_word, image) are `unknown` until first used;
# - switches with an assumed state (e.g. an alarm panel's chime toggle),
#   sensors without a reading yet, update entities before their first
#   check, and so on.
# So `unknown` counts as down only in domains that represent a physical
# device whose state the integration polls or restores, where staying
# `unknown` for hours means the integration lost the device. The flap filter
# (6 h) still applies to `unknown`, including for custom integrations.
UNKNOWN_MEANINGFUL_DOMAINS = frozenset(
    {"light", "fan", "cover", "lock", "climate", "water_heater", "humidifier", "vacuum", "lawn_mower",
     "valve", "alarm_control_panel", "media_player", "siren"}
)  # fmt: skip
FAILED_ENTRY_STATES = frozenset({"setup_error", "setup_retry", "migration_error", "failed_unload"})
# Covers that open the house (with locks and alarm panels, see `access_kind`). None may be
# exposed to Assist (invariant 3).
ACCESS_COVER_CLASSES = frozenset({"garage", "gate"})
ASSIST = "conversation"
LOG_LEVELS_TRACKED = frozenset({"ERROR", "CRITICAL"})
_OOM_RE = re.compile(r"Out of memory: Killed process \d+ \(([^)]+)\)")


class HealthAPI(Protocol):
    """Everything the collector reads; the live and fake implementations match it."""

    async def ws(self, msg_type: str, **fields: Any) -> Any: ...
    async def core_rest(self, path: str) -> Any: ...
    async def supervisor(self, path: str) -> Any: ...
    async def supervisor_post(self, path: str, *, http_timeout: float | None = None) -> Any: ...
    async def logs(self, path: str, lines: int) -> str: ...


@dataclass(slots=True)
class Snapshot:
    taken_at: str
    core: dict[str, Any] = field(default_factory=dict)
    unavailable: dict[str, dict[str, Any]] = field(default_factory=dict)
    entity_counts: dict[str, int] = field(default_factory=dict)
    failed_entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    repairs: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolution: dict[str, list[str]] = field(default_factory=dict)
    log_errors: dict[str, dict[str, Any]] = field(default_factory=dict)
    updates: list[dict[str, Any]] = field(default_factory=list)
    backups: dict[str, Any] = field(default_factory=dict)
    disk: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)
    oom_kills: list[str] = field(default_factory=list)
    boot_timestamp: int | None = None
    config_check: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    custom_integrations: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Access entities (locks, alarm panels, garage/gate covers) exposed to
    # Assist; None when the exposure list could not be read.
    assist_exposed: dict[str, str] | None = field(default_factory=dict)
    # Local hotfixes to untracked files (docs/local-patches.yaml), see opsd.house.localpatches (overlay).
    local_patches: dict[str, Any] = field(default_factory=dict)
    # Vacuum rooms against HA's vacuum area mapping, per vacuum (see opsd.house.vacuums, overlay).
    vacuum_rooms: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Entities the health check watches by state (the house profile's remote access and host power
    # entities): state and last_changed.
    watch_states: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Snapshot:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def hours_since(value: str | None, now: datetime) -> float | None:
    parsed = parse_time(value)
    return None if parsed is None else (now - parsed).total_seconds() / 3600


# ---------------------------------------------------------------- derivations


def is_down(entity_id: str, state: Any) -> bool:
    if state == "unavailable":
        return True
    return state == "unknown" and entity_id.split(".", 1)[0] in UNKNOWN_MEANINGFUL_DOMAINS


def derive_entities(
    states: Iterable[dict[str, Any]], registry: Iterable[dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """Enabled entities that are down (see UNKNOWN_MEANINGFUL_DOMAINS), with their platform."""
    reg = {e["entity_id"]: e for e in registry if "entity_id" in e}
    unavailable: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for st in states:
        entity_id = st.get("entity_id", "")
        entry = reg.get(entity_id, {})
        if entry.get("disabled_by") or entry.get("hidden_by") == "user":
            continue
        platform = entry.get("platform") or entity_id.split(".", 1)[0]
        counts[platform] = counts.get(platform, 0) + 1
        if is_down(entity_id, st.get("state")):
            unavailable[entity_id] = {
                "state": st.get("state"),
                "platform": platform,
                "config_entry_id": entry.get("config_entry_id"),
                "last_changed": st.get("last_changed"),
            }
    return unavailable, counts


def derive_failed_entries(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    failed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("disabled_by"):
            continue
        if entry.get("state") in FAILED_ENTRY_STATES:
            failed[entry["entry_id"]] = {
                "domain": entry.get("domain"),
                "title": entry.get("title"),
                "state": entry.get("state"),
                "reason": entry.get("reason"),
            }
    return failed


def derive_repairs(issues: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    repairs: dict[str, dict[str, Any]] = {}
    for issue in issues:
        if issue.get("ignored") or issue.get("dismissed_version"):
            continue
        key = f"{issue.get('domain')}:{issue.get('issue_id')}"
        repairs[key] = {
            "severity": issue.get("severity"),
            "translation_key": issue.get("translation_key"),
            "breaks_in_ha_version": issue.get("breaks_in_ha_version"),
            "created": issue.get("created"),
        }
    return repairs


def derive_resolution(info: dict[str, Any]) -> dict[str, list[str]]:
    issues = [f"{i.get('type')}:{i.get('context')}:{i.get('reference') or ''}" for i in info.get("issues", [])]
    return {
        "issues": sorted(issues),
        "unhealthy": sorted(str(x) for x in info.get("unhealthy", [])),
        "unsupported": sorted(str(x) for x in info.get("unsupported", [])),
    }


def derive_log_errors(entries: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group `system_log/list` entries at ERROR+ by message fingerprint."""
    patterns: dict[str, dict[str, Any]] = {}
    for entry in entries:
        level = str(entry.get("level", "")).upper()
        if level not in LOG_LEVELS_TRACKED:
            continue
        messages = entry.get("message") or [""]
        message = messages[0] if isinstance(messages, list) else str(messages)
        logger = str(entry.get("name", "unknown"))
        fp = fingerprint(logger, message)
        existing = patterns.get(fp)
        count = int(entry.get("count", 1))
        if existing:
            existing["count"] += count
            continue
        patterns[fp] = {
            "logger": logger,
            "level": level,
            "template": normalize(message),
            "sample": message[:400],
            "count": count,
            "first_occurred": entry.get("first_occurred"),
        }
    return patterns


LOCAL_AGENT = "hassio.local"


def _addon_slugs(raw: Any) -> set[str]:
    items = raw if isinstance(raw, list) else []
    return {str(a.get("slug") if isinstance(a, dict) else a) for a in items}


def derive_backups(
    backups: Iterable[dict[str, Any]],
    now: datetime,
    *,
    addons: Iterable[str] = (),
    manager: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backups as the health check judges them.

    A backup is *complete* when it holds Home Assistant's config, its database and every
    installed add-on, whatever its Supervisor type: Core's encrypted automatic backups are
    `partial` to the Supervisor. Core's backup manager (`backup/info`, `manager`) is preferred
    where it knows the backup (it also lists backups only on other agents, such as the cloud);
    the Supervisor's `/backups` content is the fallback. An unencrypted backup on /backup is
    listed by name. Names only, never contents.
    """
    installed = set(addons)
    core = {str(b.get("backup_id")): b for b in (manager or {}).get("backups") or [] if isinstance(b, dict)}

    def core_complete(record: dict[str, Any]) -> bool:
        return (
            record.get("homeassistant_included") is True
            and record.get("database_included") is True
            and installed <= _addon_slugs(record.get("addons"))
            and bool(record.get("agents"))
        )

    def supervisor_complete(backup: dict[str, Any]) -> bool:
        content = backup.get("content")
        if not isinstance(content, dict):
            return backup.get("type") == "full"
        return (
            content.get("homeassistant") is True
            and backup.get("homeassistant_exclude_database") is not True
            and installed <= _addon_slugs(content.get("addons"))
        )

    newest_complete: dict[str, Any] | None = None
    newest_any: dict[str, Any] | None = None
    unprotected: list[str] = []
    count = 0
    seen: set[str] = set()
    candidates: list[tuple[dict[str, Any], bool, bool | None]] = []
    for backup in backups:
        count += 1
        slug = str(backup.get("slug"))
        seen.add(slug)
        if backup.get("protected") is False:
            unprotected.append(str(backup.get("name") or slug)[:80])
        record = core.get(slug)
        complete = core_complete(record) if record is not None else supervisor_complete(backup)
        candidates.append((backup, complete, backup.get("protected")))
    for slug, record in core.items():
        if slug in seen:
            continue
        local = (record.get("agents") or {}).get(LOCAL_AGENT) if isinstance(record.get("agents"), dict) else None
        protected = local.get("protected") if isinstance(local, dict) else None
        candidates.append((record, core_complete(record), protected))
    for backup, complete, protected in candidates:
        when = parse_time(backup.get("date"))
        if when is None:
            continue
        entry = {**backup, "_when": when, "_protected": protected}
        if newest_any is None or when > newest_any["_when"]:
            newest_any = entry
        if complete and (newest_complete is None or when > newest_complete["_when"]):
            newest_complete = entry

    def age(b: dict[str, Any] | None) -> float | None:
        return None if b is None else round((now - b["_when"]).total_seconds() / 3600, 1)

    newest_core = core.get(str((newest_complete or {}).get("slug") or (newest_complete or {}).get("backup_id")))
    failed = (newest_core or {}).get("failed_agent_ids")
    return {
        "count": count,
        "newest_complete_name": newest_complete.get("name") if newest_complete else None,
        "newest_complete_age_h": age(newest_complete),
        "newest_complete_encrypted": newest_complete.get("_protected") if newest_complete else None,
        "newest_complete_failed_agents": sorted(map(str, failed))[:5] if isinstance(failed, list) else [],
        "newest_any_name": newest_any.get("name") if newest_any else None,
        "newest_any_type": newest_any.get("type") if newest_any else None,
        "newest_any_age_h": age(newest_any),
        "unprotected": sorted(unprotected)[:10],
        "unprotected_count": len(unprotected),
        "automatic": {
            "configured": bool((manager or {}).get("next_automatic_backup")),
            "last_completed": (manager or {}).get("last_completed_automatic_backup"),
        },
    }


def derive_memory(meminfo: str) -> dict[str, Any]:
    values: dict[str, int] = {}
    for line in meminfo.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name.strip()] = int(parts[0])
    total = values.get("MemTotal", 0)
    swap_total = values.get("SwapTotal", 0)
    swap_used = swap_total - values.get("SwapFree", 0)
    return {
        "total_mb": total // 1024,
        "available_mb": values.get("MemAvailable", 0) // 1024,
        "swap_total_mb": swap_total // 1024,
        "swap_used_mb": swap_used // 1024,
        "swap_used_pct": round(100 * swap_used / swap_total, 1) if swap_total else 0.0,
    }


def access_kind(entity_id: str, attributes: dict[str, Any]) -> str | None:
    """`lock`, `alarm` or `garage` for an entity that opens the house, else None."""
    domain = entity_id.split(".", 1)[0]
    if domain == "lock":
        return "lock"
    if domain == "alarm_control_panel":
        return "alarm"
    if domain == "cover" and (attributes.get("device_class") in ACCESS_COVER_CLASSES or "garage" in entity_id):
        return "garage"
    return None


def derive_assist_exposure(exposure: Any, states: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Access entities exposed to Assist, from `homeassistant/expose_entity/list`.

    Expected shape `{"exposed_entities": {entity_id: {"conversation": bool, ...}}}`
    (to verify live). Raises ValueError on anything else so a format change
    is reported, not read as "nothing exposed".
    """
    if not isinstance(exposure, dict) or not isinstance(exposure.get("exposed_entities"), dict):
        raise ValueError("unexpected expose_entity/list result")
    attributes = {s.get("entity_id", ""): s.get("attributes") or {} for s in states}
    found: dict[str, str] = {}
    for entity_id, assistants in exposure["exposed_entities"].items():
        if not isinstance(entity_id, str) or not isinstance(assistants, dict) or assistants.get(ASSIST) is not True:
            continue
        kind = access_kind(entity_id, attributes.get(entity_id, {}))
        if kind is not None:
            found[entity_id] = kind
    return dict(sorted(found.items()))


def derive_oom(kernel_log: str) -> list[str]:
    return [m.group(0) for m in _OOM_RE.finditer(kernel_log)]


def derive_custom_integrations(
    domains: Iterable[str],
    counts: dict[str, int],
    unavailable: dict[str, dict[str, Any]],
    entries: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Per custom integration: entity totals, unavailable count and entry states."""
    entry_list = list(entries)
    result: dict[str, dict[str, Any]] = {}
    for domain in sorted(domains):
        down = sorted(e for e, v in unavailable.items() if v["platform"] == domain)
        states = [e.get("state") for e in entry_list if e.get("domain") == domain and not e.get("disabled_by")]
        result[domain] = {
            "entities": counts.get(domain, 0),
            "unavailable": len(down),
            "unavailable_sample": down[:10],
            "entries": len(states),
            "entries_loaded": sum(1 for s in states if s == "loaded"),
            "entries_failed": sum(1 for s in states if s in FAILED_ENTRY_STATES),
        }
    return result


ADDON_UNIQUE_SUFFIX = "_version_latest"


def addon_slug_from_unique_id(unique_id: Any) -> str | None:
    """The hassio add-on update entity's unique id is `<slug>_version_latest` (as Core reports it)."""
    if not isinstance(unique_id, str) or not unique_id.endswith(ADDON_UNIQUE_SUFFIX):
        return None
    slug = unique_id.removesuffix(ADDON_UNIQUE_SUFFIX)
    return slug if re.fullmatch(r"[a-z0-9_]{1,64}", slug) and not slug.startswith("home_assistant_") else None


def derive_updates(
    states: Iterable[dict[str, Any]],
    registry: Iterable[dict[str, Any]],
    options: Options,
    addon_slugs: dict[str, str],
    core_release_age_days: float | None,
    *,
    hacs_repos: dict[str, dict[str, Any]] | None = None,
    ages: Mapping[str, float | None] | None = None,
) -> list[dict[str, Any]]:
    """Pending updates (state `on`) with their policy tier.

    Add-on slugs come from the registry unique id (`<slug>_version_latest`),
    falling back to the title match; HACS repositories are matched by the
    update entity's unique id (the repository id). A skipped version turns
    the entity `off`, so it never shows here.
    """
    reg = {e["entity_id"]: e for e in registry if "entity_id" in e}
    repos = hacs_repos or {}
    known_ages = ages or {}
    updates: list[dict[str, Any]] = []
    for st in states:
        entity_id = st.get("entity_id", "")
        if not entity_id.startswith("update.") or st.get("state") != "on":
            continue
        attrs = st.get("attributes", {})
        entry = reg.get(entity_id, {})
        platform = entry.get("platform") or "unknown"
        device_class = attrs.get("device_class")
        slug = addon_slug_from_unique_id(entry.get("unique_id")) if platform == "hassio" else None
        slug = slug or addon_slugs.get(entity_id)
        repo = repos.get(str(entry.get("unique_id"))) if platform == "hacs" else None
        features = attrs.get("supported_features")
        info = policy.UpdateInfo(
            entity_id=entity_id,
            title=str(attrs.get("title") or attrs.get("friendly_name") or entity_id),
            installed=attrs.get("installed_version"),
            latest=attrs.get("latest_version"),
            platform=platform,
            kind=policy.update_kind(entity_id, platform, device_class if isinstance(device_class, str) else None),
            device_class=device_class if isinstance(device_class, str) else None,
            skipped_version=attrs.get("skipped_version") if isinstance(attrs.get("skipped_version"), str) else None,
            supported_features=features if isinstance(features, int) and not isinstance(features, bool) else 0,
            in_progress=attrs.get("in_progress") not in (None, False),
            hacs_category=str(repo.get("category")) if repo and repo.get("category") else None,
            hacs_repo=str(repo.get("full_name")) if repo and repo.get("full_name") else None,
            hacs_domain=str(repo.get("domain")) if repo and repo.get("domain") else None,
        )
        age = core_release_age_days if info.kind == "core" else None
        age = age if age is not None else known_ages.get(entity_id)
        decision = policy.classify(
            info,
            release_age_days=age,
            min_age_days=float(options.updates.min_age_days),
            approval_updates=options.need_approval,
            never_updates=frozenset(options.updates.never),
            addon_slug=slug,
            fragile=options.profile.updates.fragile,
        )
        updates.append(
            {
                "entity_id": entity_id,
                "title": info.title,
                "installed": info.installed,
                "latest": info.latest,
                "kind": info.kind,
                "platform": platform,
                "tier": str(decision.tier),
                "bump": str(decision.bump),
                "reason": decision.reason,
                # Soaking: approvable early, never asks (policy.classify).
                "waits": decision.waits,
                "slug": slug,
                "hacs_category": info.hacs_category,
                "hacs_repo": info.hacs_repo,
                "hacs_domain": info.hacs_domain,
                "supported_features": info.supported_features,
                "release_age_days": round(age, 1) if age is not None else None,
            }
        )
    return sorted(updates, key=lambda u: u["entity_id"])


def hacs_repo(info: Any) -> dict[str, Any]:
    """The facts the update rule needs from `hacs/repository/info` (shape as HACS returns it)."""
    if not isinstance(info, dict) or info.get("id") is None:
        raise ValueError("unexpected hacs/repository/info result")
    return {k: info.get(k) for k in ("full_name", "category", "domain", "installed_version")}


def git_status(repo: Path) -> dict[str, Any]:
    """Local-only git facts (no network): dirty paths and ahead/behind origin/main."""

    def run(*args: str) -> str:
        return subprocess.run(
            git_argv(repo, *args), capture_output=True, text=True, timeout=30, check=True, env=git_env()
        ).stdout

    try:
        dirty = [line[3:] for line in run("status", "--porcelain").splitlines() if line.strip()]
        counts = run("rev-list", "--left-right", "--count", "HEAD...origin/main").split()
        branch = run("rev-parse", "--abbrev-ref", "HEAD").strip()
    except (subprocess.SubprocessError, FileNotFoundError) as err:
        return {"error": str(err)[:200]}
    ahead, behind = (int(counts[0]), int(counts[1])) if len(counts) == 2 else (0, 0)
    return {"dirty": dirty, "ahead": ahead, "behind": behind, "branch": branch}


def custom_domains(custom_components: Path) -> list[str]:
    try:
        return sorted(
            p.name
            for p in custom_components.iterdir()
            if p.is_dir() and not p.name.startswith((".", "__")) and (p / "manifest.json").exists()
        )
    except FileNotFoundError:
        return []


# ------------------------------------------------------------------ collector


async def collect(
    api: HealthAPI,
    paths: Paths,
    options: Options,
    *,
    now: datetime | None = None,
    core_release_age: Callable[[str], float | None] | None = None,
    release_age: Callable[[str, str], float | None] | None = None,
    seen_age: Callable[[str, str], float | None] | None = None,
    run_config_check: bool = True,
    meminfo_path: Path = Path("/proc/meminfo"),
) -> Snapshot:
    """Gather everything; a failing source is recorded in `errors`, never fatal."""
    now = now or datetime.now(UTC)
    snap = Snapshot(taken_at=now.isoformat(timespec="seconds"))

    async def guarded(name: str, default: Any, coro: Awaitable[Any]) -> Any:
        try:
            result = await coro
        except Exception as err:  # noqa: BLE001 - one broken source must not hide the rest
            snap.errors.append(f"{name}: {type(err).__name__}: {str(err)[:200]}")
            _LOGGER.warning("health source failed", extra={"source": name, "error": str(err)[:200]})
            return default
        return result

    core_info = await guarded("core_info", {}, api.supervisor("/core/info"))
    host_info = await guarded("host_info", {}, api.supervisor("/host/info"))
    sup_info = await guarded("supervisor_info", {}, api.supervisor("/supervisor/info"))
    os_info = await guarded("os_info", {}, api.supervisor("/os/info"))
    snap.core = {
        "version": core_info.get("version"),
        "state": core_info.get("state"),
        "supervisor_version": sup_info.get("version"),
        "os_version": os_info.get("version"),
        "api_up": False,
    }
    snap.boot_timestamp = host_info.get("boot_timestamp")
    if host_info.get("disk_total"):
        total = float(host_info["disk_total"])
        free = float(host_info.get("disk_free", 0))
        snap.disk = {"total_gb": round(total, 1), "free_gb": round(free, 1), "free_pct": round(100 * free / total, 1)}

    states: list[dict[str, Any]] = await guarded("states", [], api.core_rest("/states"))
    snap.core["api_up"] = bool(states)
    registry: list[dict[str, Any]] = await guarded("entity_registry", [], api.ws("config/entity_registry/list"))
    entries: list[dict[str, Any]] = await guarded("config_entries", [], api.ws("config_entries/get"))
    issues = await guarded("repairs", {"issues": []}, api.ws("repairs/list_issues"))
    system_log: list[dict[str, Any]] = await guarded("system_log", [], api.ws("system_log/list"))
    resolution = await guarded("resolution", {}, api.supervisor("/resolution/info"))
    backups = await guarded("backups", {"backups": []}, api.supervisor("/backups"))
    addons = await guarded("addons", {"addons": []}, api.supervisor("/addons"))
    kernel = await guarded("kernel_log", "", api.logs("/host/logs/identifiers/kernel", 500))

    snap.unavailable, snap.entity_counts = derive_entities(states, registry)
    # Host power and remote access, as health items (opsd.baseline._watched_items).
    watch = {e for e in (options.profile.health.power_entity, options.profile.health.remote_entity) if e}
    snap.watch_states = {
        str(s.get("entity_id")): {"state": s.get("state"), "last_changed": s.get("last_changed")}
        for s in states
        if s.get("entity_id") in watch
    }
    snap.failed_entries = derive_failed_entries(entries)
    snap.repairs = derive_repairs(issues.get("issues", []) if isinstance(issues, dict) else [])
    snap.resolution = derive_resolution(resolution)
    snap.log_errors = derive_log_errors(system_log)
    manager = await guarded("backup_manager", None, api.ws("backup/info"))
    installed = [str(a.get("slug")) for a in addons.get("addons", []) if isinstance(a, dict) and a.get("slug")]
    snap.backups = derive_backups(backups.get("backups", []), now, addons=installed,
                                  manager=manager if isinstance(manager, dict) else None)  # fmt: skip
    snap.oom_kills = derive_oom(kernel)
    snap.custom_integrations = derive_custom_integrations(
        custom_domains(paths.custom_components), snap.entity_counts, snap.unavailable, entries
    )
    exposure = await guarded("assist_exposure", None, api.ws("homeassistant/expose_entity/list"))
    try:
        snap.assist_exposed = None if exposure is None else derive_assist_exposure(exposure, states)
    except ValueError as err:
        snap.errors.append(f"assist_exposure: {err}")
        snap.assist_exposed = None
    try:
        snap.memory = derive_memory(await asyncio.to_thread(meminfo_path.read_text))
    except OSError as err:
        snap.errors.append(f"meminfo: {err}")

    addon_slugs = _addon_update_entities(states, addons.get("addons", []))
    core_age: float | None = None
    policy_core_entity = "update.home_assistant_core_update"
    core_update = next((s for s in states if s.get("entity_id") == policy_core_entity), None)
    if core_update and core_release_age and core_update.get("state") == "on":
        latest = core_update.get("attributes", {}).get("latest_version")
        if latest:
            core_age = await guarded("core_release_age", None, asyncio.to_thread(core_release_age, latest))
    hacs: dict[str, dict[str, Any]] = {}
    pending = {s.get("entity_id") for s in states if s.get("state") == "on"}
    for entry in registry:
        # One small lookup per pending HACS update (the full repository list is megabytes).
        if entry.get("platform") != "hacs" or entry.get("entity_id") not in pending or not entry.get("unique_id"):
            continue
        repo_id = str(entry["unique_id"])
        raw = await guarded("hacs_repository", None, api.ws("hacs/repository/info", repository_id=repo_id))
        try:
            if raw is not None:
                hacs[repo_id] = hacs_repo(raw)
        except ValueError as err:
            snap.errors.append(f"hacs_repository: {err}")
    # Release ages for everything else: GitHub for HACS repositories, else how long the
    # agent has seen the version pending (a lower bound). Unknown means WAIT.
    ages: dict[str, float | None] = {}
    by_entity = {e.get("entity_id"): e for e in registry}
    for st in states:
        entity_id = str(st.get("entity_id", ""))
        if not entity_id.startswith("update.") or st.get("state") != "on" or entity_id == policy_core_entity:
            continue
        latest = str((st.get("attributes") or {}).get("latest_version") or "")
        repo = hacs.get(str(by_entity.get(entity_id, {}).get("unique_id")), {}).get("full_name")
        age: float | None = None
        if release_age is not None and repo and latest:
            age = await guarded("release_age", None, asyncio.to_thread(release_age, str(repo), latest))
        if age is None and seen_age is not None and latest:
            age = seen_age(entity_id, latest)
        ages[entity_id] = age
    if core_update is not None and core_age is None and seen_age is not None and core_update.get("state") == "on":
        latest = str((core_update.get("attributes") or {}).get("latest_version") or "")
        core_age = seen_age(policy_core_entity, latest) if latest else None
    snap.updates = derive_updates(states, registry, options, addon_slugs, core_age, hacs_repos=hacs, ages=ages)

    if run_config_check and snap.core["api_up"]:
        try:
            await api.supervisor_post("/core/check", http_timeout=CONFIG_CHECK_TIMEOUT_S)
            snap.config_check = {"ok": True}
        except ApiError as err:
            detail = f"config_check: {str(err)[:200]}"
            snap.errors.append(detail)
            _LOGGER.warning("health source failed", extra={"source": "config_check", "error": str(err)[:200]})
            # No HTTP status means the check never answered (timeout, Supervisor busy), so the config is
            # unknown rather than invalid; only an answered failure may raise the critical item.
            snap.config_check = {"ok": None if err.status is None else False, "error": detail}
        except Exception as err:  # noqa: BLE001 - one broken source must not hide the rest
            detail = f"config_check: {type(err).__name__}: {str(err)[:200]}"
            snap.errors.append(detail)
            _LOGGER.warning("health source failed", extra={"source": "config_check", "error": str(err)[:200]})
            snap.config_check = {"ok": False, "error": detail}
    if options.profile.overlay("vacuums"):
        from opsd.house import vacuums  # noqa: PLC0415 - a house overlay, loaded only when the profile enables it

        registered = {str(e.get("entity_id")) for e in registry}
        present = [e for e in vacuums.VACUUMS if e in registered]
        snap.vacuum_rooms = await guarded("vacuum_rooms", {}, vacuums.read(api, present))
        snap.errors.extend(f"vacuum_rooms {e}: {v['error']}" for e, v in snap.vacuum_rooms.items() if "error" in v)
    if options.git_on:
        snap.git = await asyncio.to_thread(git_status, paths.ha_config)
    if options.profile.overlay("localpatches"):
        from opsd.house import localpatches  # noqa: PLC0415 - a house overlay

        try:
            snap.local_patches = await asyncio.to_thread(localpatches.check, paths.ha_config)
        except OSError as err:
            snap.local_patches = {"ok": False, "error": f"local patches: {str(err)[:200]}", "patches": {}}
    return snap


def _addon_update_entities(states: Iterable[dict[str, Any]], addons: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Map add-on update entities to slugs by matching the entity title to the add-on name."""
    by_name = {str(a["name"]).lower(): str(a.get("slug", "")) for a in addons if a.get("name")}
    mapping: dict[str, str] = {}
    for st in states:
        entity_id = st.get("entity_id", "")
        if not entity_id.startswith("update."):
            continue
        title = str(st.get("attributes", {}).get("title", "")).lower()
        if title and title in by_name:
            mapping[entity_id] = by_name[title]
    return mapping
