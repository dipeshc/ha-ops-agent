"""Policy: the generic tier engine (`decide`) and the update rule (`classify`).

The update rule decides every pending update deterministically:

- AUTO: a patch or a minor of Core, an add-on or a HACS integration, once it has soaked
  (`updates.min_age_days`, never below 7, for a patch; twice that for a minor), by its release date
  where known (GitHub for Core and HACS), else how long the agent has seen it pending. The update
  pipeline then still asks when a repair, a Core breaking change or a held version touches what
  this house uses (`opsd.updates`), and the release-note review may hold it back.
- Soaking: the same candidate before that (or with its age unknown) waits quietly (`waits`): it is
  APPROVE, so the admin may approve it early, but it does not ask.
- APPROVE (asks): Core majors, HAOS, device firmware, any major add-on or HACS change, HACS frontend
  plugins and themes, anything in `updates.need_approval`, the house profile's fragile pieces
  (`opsd.profile`, tier approve), an unknown version scheme, and anything the agent could not roll
  back precisely (an add-on whose slug is unknown, a HACS repository without specific-version
  installs or of an unknown category).
- NEVER (notify only): Core `.0` and `updates.never`; PINNED: a fragile piece the profile pins.
- MANUAL (never offered): the agent itself, the Supervisor, a version the admin skipped, nothing newer.

Options can only tighten these (see `decide`).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntFlag, StrEnum

from opsd.selfguard import is_self_slug

CORE_PATCH_MIN_AGE_DAYS = 7.0
SELF_UPDATE_ENTITY = "update.ha_ops_agent_update"
SELF_TITLE = "ha ops agent"

_HASSIO_SPECIAL = {
    "update.home_assistant_core_update": "core",
    "update.home_assistant_operating_system_update": "os",
    "update.home_assistant_supervisor_update": "supervisor",
}
_FIRMWARE_PLATFORMS = frozenset(
    {
        "esphome",
        "reolink",
        "homeassistant_hardware",
        "homeassistant_connect_zbt2",
        "homeassistant_sky_connect",
        "homeassistant_yellow",
        "raspberry_pi",
        "matter",
        "zha",
        "unifiprotect",
        "shelly",
        "tplink",
        "hue",
    }
)
# HACS repository categories the executor knows how to install and verify.
HACS_CATEGORIES = frozenset({"integration", "plugin", "theme"})


class UpdateFeature(IntFlag):
    """`supported_features` of an update entity (homeassistant.components.update)."""

    INSTALL = 1
    SPECIFIC_VERSION = 2
    PROGRESS = 4
    BACKUP = 8
    RELEASE_NOTES = 16


class Tier(StrEnum):
    AUTO = "auto"
    APPROVE = "approve"
    NEVER = "never"
    # A version pin from the runbook: never installed until a code change lifts it.
    PINNED = "pinned"
    MANUAL = "manual"


class Bump(StrEnum):
    NONE = "none"
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    entity_id: str
    title: str
    installed: str | None
    latest: str | None
    platform: str
    kind: str
    # From the update entity's attributes and registry entry (see health.derive_updates).
    device_class: str | None = None
    skipped_version: str | None = None
    supported_features: int = 0
    in_progress: bool = False
    # HACS repository facts (from `hacs/repository/info`), when known.
    hacs_category: str | None = None
    hacs_repo: str | None = None
    hacs_domain: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    tier: Tier
    bump: Bump
    reason: str
    # A candidate still soaking: APPROVE (the admin may approve it early) without asking him.
    waits: bool = False


@dataclass(frozen=True, slots=True)
class Fragile:
    """A house profile `updates.fragile` entry (the runbook's "Known fragile pieces"), matched by entity,
    HACS repo or integration domain."""

    name: str
    tier: Tier
    reason: str
    entities: frozenset[str] = frozenset()
    repos: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    # Only these bumps are held (empty: every bump).
    bumps: frozenset[Bump] = frozenset()

    def matches(self, info: UpdateInfo, bump: Bump) -> bool:
        if self.bumps and bump not in self.bumps:
            return False
        repo = (info.hacs_repo or "").lower()
        return (
            info.entity_id in self.entities
            or (bool(repo) and repo in self.repos)
            or (info.hacs_domain or "") in self.domains
            or any(d in info.entity_id for d in self.domains)
        )


def parse_version(value: str | None) -> tuple[int, ...] | None:
    """Dotted numeric versions only (`v1.2.3`, `2026.10.3`); dates are not versions."""
    if not value:
        return None
    text = value.strip().lstrip("vV")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if not match or match.group(1) != text.split("+")[0].split("-")[0]:
        return None
    return tuple(int(p) for p in match.group(1).split("."))


def same_version(a: str | None, b: str | None) -> bool:
    """`v1.2.3` and `1.2.3` are the same release; anything unparsable compares as text."""
    if a is None or b is None:
        return False
    return a.strip().lstrip("vV") == b.strip().lstrip("vV")


def version_bump(installed: str | None, latest: str | None) -> Bump:
    old, new = parse_version(installed), parse_version(latest)
    if old is None or new is None:
        return Bump.UNKNOWN
    width = max(len(old), len(new), 3)
    old += (0,) * (width - len(old))
    new += (0,) * (width - len(new))
    if new <= old:
        return Bump.NONE
    if new[0] != old[0]:
        return Bump.MAJOR
    # 0.x minors are breaking by semver convention.
    if new[1] != old[1]:
        return Bump.MAJOR if old[0] == 0 else Bump.MINOR
    return Bump.PATCH


def update_kind(entity_id: str, platform: str, device_class: str | None = None) -> str:
    if entity_id in _HASSIO_SPECIAL:
        return _HASSIO_SPECIAL[entity_id]
    if device_class == "firmware" or platform in _FIRMWARE_PLATFORMS:
        return "firmware"
    if platform == "hassio":
        return "addon"
    if platform == "hacs":
        return "hacs"
    return "other"


def _is_self(info: UpdateInfo, addon_slug: str | None) -> bool:
    return (
        is_self_slug(addon_slug or "")
        or info.entity_id == SELF_UPDATE_ENTITY
        or info.title.strip().lower() == SELF_TITLE
    )


def soak_days(bump: Bump, min_age_days: float) -> float:
    """How long a candidate soaks: `updates.min_age_days` (7 at least) for a patch, twice that for a minor."""
    floor = max(CORE_PATCH_MIN_AGE_DAYS, min_age_days)
    return floor if bump is Bump.PATCH else 2 * floor


def _candidate(label: str, bump: Bump, age: float | None, min_age_days: float) -> Decision:
    soak = soak_days(bump, min_age_days)
    if age is None:
        return Decision(Tier.APPROVE, bump, f"{label} {bump}: release age unknown, so it waits", waits=True)
    if age < soak:
        return Decision(Tier.APPROVE, bump, f"{label} {bump}: soaking for {soak:g} days ({int(age)} so far)",
                        waits=True)  # fmt: skip
    return Decision(Tier.AUTO, bump, f"{label} {bump}, soaked {soak:g} days")


def classify(
    info: UpdateInfo,
    *,
    release_age_days: float | None = None,
    min_age_days: float = CORE_PATCH_MIN_AGE_DAYS,
    approval_updates: frozenset[str] = frozenset(),
    never_updates: frozenset[str] = frozenset(),
    addon_slug: str | None = None,
    fragile: Sequence[Fragile] = (),
) -> Decision:
    """The update rule (module docstring). Every branch can only hold an update back; nothing here loosens.

    `fragile` is the house profile's list (`HouseProfile.fragile`); a piece is never AUTO (the profile
    refuses that tier).
    """
    bump = version_bump(info.installed, info.latest)
    if bump is Bump.NONE:
        return Decision(Tier.MANUAL, bump, "no newer version")
    if _is_self(info, addon_slug):
        return Decision(Tier.MANUAL, bump, "the agent never updates itself")
    if info.skipped_version is not None and same_version(info.skipped_version, info.latest):
        return Decision(Tier.MANUAL, bump, "skipped by the admin (update.skip)")
    if info.entity_id in never_updates:
        return Decision(Tier.NEVER, bump, "listed in updates.never")
    if info.in_progress:
        return Decision(Tier.MANUAL, bump, "an install is already in progress")

    match info.kind:
        case "core":
            latest = parse_version(info.latest)
            if latest is not None and len(latest) >= 3 and latest[2] == 0:
                return Decision(Tier.NEVER, bump, "Core .0 release: wait for a patch")
            if bump in (Bump.MAJOR, Bump.UNKNOWN):
                return Decision(Tier.APPROVE, bump, f"Core {bump} update needs approval")
            if info.entity_id in approval_updates:
                return Decision(Tier.APPROVE, bump, "listed in updates.need_approval")
            return _candidate("Core", bump, release_age_days, min_age_days)
        case "os":
            return Decision(Tier.APPROVE, bump, "operating system updates reboot the host: you approve each one")
        case "supervisor":
            return Decision(Tier.MANUAL, bump, "Supervisor updates itself")
        case "firmware":
            return Decision(Tier.APPROVE, bump, "device firmware needs approval")
        case _:
            pass

    for piece in fragile:
        if piece.matches(info, bump) and piece.tier is not Tier.AUTO:
            return Decision(piece.tier, bump, piece.reason)
    if info.entity_id in approval_updates:
        return Decision(Tier.APPROVE, bump, "listed in updates.need_approval")
    if info.kind == "other":
        return Decision(Tier.APPROVE, bump, f"unrecognised update platform {info.platform}")
    if bump is Bump.UNKNOWN:
        return Decision(Tier.APPROVE, bump, f"{bump} version change")
    if info.kind == "hacs" and info.hacs_category in ("plugin", "theme"):
        # A frontend plugin runs as JavaScript in the admin's HA origin, next to this
        # panel; nothing here can verify that a theme carries none. Always approved by the admin.
        return Decision(Tier.APPROVE, bump, f"HACS frontend {info.hacs_category}: always approved by you")
    if bump is Bump.MAJOR:
        return Decision(Tier.APPROVE, bump, f"{bump} version change")
    rollback = _rollback_problem(info, addon_slug)
    if rollback is not None:
        return Decision(Tier.APPROVE, bump, rollback)
    return _candidate(info.kind, bump, release_age_days, min_age_days)


def _rollback_problem(info: UpdateInfo, addon_slug: str | None) -> str | None:
    """Why the agent could not roll this update back precisely (or escalate it with its backup), or None."""
    if info.kind == "addon" and not addon_slug:
        return "add-on slug unknown: no precise rollback"
    if info.kind == "hacs":
        if not UpdateFeature.SPECIFIC_VERSION & info.supported_features:
            return "no specific-version install: no precise rollback"
        if info.hacs_category not in HACS_CATEGORIES:
            return "HACS repository category unknown"
    return None


# --------------------------------------------------------------------------
# Generic policy engine: one decision for any capability request.
#
# `classify` above stays the update rule; `resolve_update` turns its result
# into a `Resolution` so it can act as the tier resolver of a future
# `update.install` capability.


class PolicyTier(StrEnum):
    """Target tiers, from least to most restrictive."""

    AUTO = "auto"
    AUTO_NOTIFY = "auto_notify"
    APPROVE = "approve"
    NEVER = "never"


_STRICTNESS: dict[PolicyTier, int] = {tier: rank for rank, tier in enumerate(PolicyTier)}


class Origin(StrEnum):
    """Who a request ultimately serves. Nothing else is accepted.

    `admin-chat` is the admin speaking through a channel that code running
    inside Core could also forge (Companion push replies and taps, verified
    by user id and a single-use action id). It may read, manage the admin's
    own reminders and notifications, and reload an allowed integration
    (`CHAT_EFFECTS`), nothing else; anything stronger needs the ingress panel
    (`admin`), which Core-resident code cannot drive.
    """

    ADMIN = "admin"
    ADMIN_CHAT = "admin-chat"
    AGENT_SELF = "agent-self"


class Effect(StrEnum):
    READ = "read"
    # The agent's own bookkeeping (baseline acks).
    AGENT_STATE = "agent-state"
    NOTIFY = "notify"
    SCHEDULE = "schedule"
    # A suggestion the admin acts on elsewhere (an update to approve in the panel). It
    # grants nothing: the approval itself still needs the panel request and the phone tap.
    PROPOSE = "propose"
    # Reload one integration's config entries (opsd.reload). It changes no config and is
    # limited in code (denylist, 10 minutes per domain, daily quota). It is allowed for the
    # agent and for chat, so it is the one HA change a chat may cause.
    RELOAD = "reload"
    MUTATE_HA = "mutate-ha"
    MUTATE_GIT = "mutate-git"


# `observe` mode turns these into "report only". Pushes and acks stay live in
# observe mode.
OBSERVE_BLOCKED_EFFECTS = frozenset({Effect.RELOAD, Effect.MUTATE_HA, Effect.MUTATE_GIT})

# What a possibly-forged chat may cause: reads, reminders/follow-ups for the
# admin, pushes to the admin, proposals the admin must act on in the panel, and
# a rate-limited integration reload. Every other
# effect is `never` for this origin, whatever the capability default (confirm
# in the panel instead). A forged reload costs at most a short outage of one
# allowed integration, at most once per 10 minutes.
CHAT_EFFECTS = frozenset({Effect.READ, Effect.SCHEDULE, Effect.NOTIFY, Effect.PROPOSE, Effect.RELOAD})

# Integrations the agent never reloads, whatever the add-on options or the house profile say (`opsd.reload`).
# The profile can only add to this list (`reload.deny`); a profile naming one of these as reloadable is refused.
# Each group is sorted. Changing the list is a code change and gets a security review.
NEVER_RELOAD_DOMAINS: frozenset[str] = frozenset(
    {
        # The entity platforms that open, guard or sound the house (never an integration to reload).
        "alarm_control_panel", "cover", "lock", "siren", "valve",
        # Locks, alarms, access control and the cameras or hubs that carry them (never reloaded by the agent).
        "abode", "alarmdecoder", "alarmdotcom", "august", "elkm1", "envisalink", "envisalink_new", "esphome",
        "homekit_controller", "konnected", "matter", "nest", "nuki", "nx584", "overkiz", "ring", "risco", "schlage",
        "simplisafe", "switchbot", "tedee", "tesla_fleet", "totalconnect", "unifiprotect", "verisure", "yale",
        "yale_home", "yale_smart_alarm", "yalexs_ble", "zha", "zwave_js",
        # Garage door openers and lighting bridges that also switch other loads.
        "aladdin_connect", "gogogate2", "lutron_caseta", "myq", "ratgdo",
        # Radios, discovery and networks the locks and everything else ride on.
        "bluetooth", "bluetooth_adapters", "dhcp", "mqtt", "network", "otbr", "ssdp", "thread", "unifi", "usb",
        "zeroconf",
        # Bridges, remote access and the plumbing the house, the phone and the agent depend on.
        "alexa", "api", "auth", "backup", "cloud", "frontend", "google_assistant", "hassio", "homeassistant",
        "homekit", "http", "mobile_app", "onboarding", "person", "recorder", "websocket_api", "zone",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Resolution:
    """A tier resolver's answer for one concrete request."""

    tier: PolicyTier
    reason: str


@dataclass(frozen=True, slots=True)
class Verdict:
    tier: PolicyTier
    execute: bool
    report_only: bool
    notify_admin: bool
    reasons: tuple[str, ...]

    @property
    def outcome(self) -> str:
        if self.execute:
            return "execute"
        if self.report_only:
            return "report_only"
        match self.tier:
            case PolicyTier.APPROVE:
                return "needs_approval"
            case _:
                return "denied"


def strictest(*tiers: PolicyTier) -> PolicyTier:
    return max(tiers, key=_STRICTNESS.__getitem__)


def decide(
    *,
    capability: str,
    default_tier: PolicyTier,
    effects: frozenset[Effect],
    origin: Origin,
    allowed_origins: frozenset[Origin],
    acting: bool,
    resolution: Resolution | None = None,
    tighten: Mapping[str, PolicyTier] | None = None,
) -> Verdict:
    """The effective tier is the strictest of every input; nothing can loosen it.

    Inputs: the capability's code default, its resolver's answer for these
    arguments, add-on option tightening, and the mode.
    An origin the capability does not accept is `never`. In `observe` mode an
    executable request with an external effect becomes report-only.
    """
    reasons = [f"default {default_tier}"]
    tiers = [default_tier]
    if origin not in allowed_origins:
        tiers.append(PolicyTier.NEVER)
        reasons.append(f"origin {origin} not accepted")
    if origin is Origin.ADMIN_CHAT and not effects <= CHAT_EFFECTS:
        tiers.append(PolicyTier.NEVER)
        reasons.append(
            "chat-origin requests may only read, schedule, notify, propose and reload an integration; the admin"
            " does the rest in the panel"
        )
    if resolution is not None:
        tiers.append(resolution.tier)
        reasons.append(f"resolver: {resolution.reason}")
    override = (tighten or {}).get(capability)
    if override is not None:
        tiers.append(override)
        reasons.append(f"options tighten to {override}")
    tier = strictest(*tiers)
    executable = tier in (PolicyTier.AUTO, PolicyTier.AUTO_NOTIFY)
    report_only = executable and not acting and bool(effects & OBSERVE_BLOCKED_EFFECTS)
    if report_only:
        reasons.append("observe mode: report only")
    return Verdict(
        tier=tier,
        execute=executable and not report_only,
        report_only=report_only,
        notify_admin=tier is PolicyTier.AUTO_NOTIFY,
        reasons=tuple(reasons),
    )


_UPDATE_TIERS: dict[Tier, PolicyTier] = {
    Tier.AUTO: PolicyTier.AUTO,
    Tier.APPROVE: PolicyTier.APPROVE,
    Tier.NEVER: PolicyTier.NEVER,
    Tier.PINNED: PolicyTier.NEVER,
    Tier.MANUAL: PolicyTier.NEVER,
}


def resolve_update(decision: Decision) -> Resolution:
    """`classify` as a tier resolver: MANUAL is never offered."""
    return Resolution(_UPDATE_TIERS[decision.tier], decision.reason)
