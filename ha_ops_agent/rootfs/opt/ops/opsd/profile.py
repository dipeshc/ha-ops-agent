"""The house profile: what ties the agent to one house, in one file.

`ops/profile.yaml` in the Home Assistant config (not secret; tracked in git where the config is) is read
once at start (`Options.load`). Without it the agent is generic: no fragile pieces, no watched entities,
no reloadable integrations, no overlays, git proposals off, no house documents or notes. The add-on
options apply either way.

The schema is strict. An unknown key, a wrong type or a value outside its pattern refuses the whole file:
the agent then runs with the generic profile and fails closed like invalid add-on options (no act mode,
no automatic updates, no AI) until the file is fixed. The reason shows on the System sheet and as a
health item.

No value can weaken an invariant. The profile adds caution or names house things, nothing else:

- a fragile piece's tier is approve, pinned or never (never auto), so it can only hold an update back;
- `reload.domains` may not name a domain on the never-reload list in code
  (`policy.NEVER_RELOAD_DOMAINS`), and `reload.deny` only adds to that list;
- document paths are plain relative `.md` files or top-level directories: never hidden, secrets, the
  agent's add-on, git internals or `ops/` (`guard.valid_read_entry`);
- proposal kinds are a subset of the three the config check knows, and the facts document is
  `docs/<name>.md` (`changepaths.ProposalRules`);
- the house notes reach a model only as fenced data;
- `self_slug` only names the panel URL in pushes; the self-guard protects every `*_ha_ops_agent` slug.

`overlays` switches on optional modules in `opsd.house` (vacuum rooms, local patch checks,
known-false-positive rules). This repository ships none of them; a build that adds them under
`opsd/house/` can enable them here. The core imports them only when the profile lists them, and a
profile naming an overlay the build does not ship is refused.
"""

import importlib.util
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opsd.changepaths import FACTS_RE, KINDS, ProposalRules
from opsd.configcheck import ConfigRefused
from opsd.configcheck import parse as parse_yaml
from opsd.guard import valid_read_entry
from opsd.policy import NEVER_RELOAD_DOMAINS, Bump, Fragile, Tier, parse_version
from opsd.selfguard import SELF_SLUG

PROFILE_PATH = "ops/profile.yaml"
MAX_BYTES = 64 * 1024
MAX_LIST = 50
MAX_NOTES = 4000
OVERLAYS = ("vacuums", "localpatches", "suppress")
FRAGILE_TIERS = {"approve": Tier.APPROVE, "pinned": Tier.PINNED, "never": Tier.NEVER}

_ENTITY = re.compile(r"[a-z_]{1,64}\.[a-z0-9_]{1,128}")
_UPDATE_ENTITY = re.compile(r"update\.[a-z0-9_]{1,128}")
_BINARY_SENSOR = re.compile(r"binary_sensor\.[a-z0-9_]{1,128}")
_DOMAIN = re.compile(r"[a-z0-9_]{1,64}")
_REPO = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}/[a-z0-9_.-]{1,100}")
_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_PATCH_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_SERVICE = re.compile(r"mobile_app_[a-z0-9_]{1,64}")
_SLUG = re.compile(r"[a-z0-9]{1,32}_ha_ops_agent")
_LINE = re.compile(r"[^\x00-\x1f\x7f]{1,300}")
# A git identity: no angle brackets or control characters.
_PERSON = re.compile(r"[^\x00-\x1f\x7f<>]{1,64}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}")
_NOTES_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ProfileError(ValueError):
    """The profile is invalid; the message names the key and the problem (never echoing a long value)."""


@dataclass(frozen=True, slots=True)
class VersionGate:
    """Core at `core_from` or later is held (never installed) until the local patch `patch` is present or
    superseded (the `localpatches` overlay), or, without `patch`, until the profile changes."""

    integration: str
    core_from: str
    reason: str
    patch: str = ""


@dataclass(frozen=True, slots=True)
class HealthProfile:
    # Optional state watches (`baseline._watched_items`): host under-voltage and remote access.
    power_entity: str = ""
    remote_entity: str = ""


@dataclass(frozen=True, slots=True)
class UpdatesProfile:
    # Added to the add-on option `updates.need_approval` (never removes an entry).
    need_approval: tuple[str, ...] = ()
    # Must stay available across an update (`updiff`): the locks and the alarm here.
    watched_entities: tuple[str, ...] = ()
    fragile: tuple[Fragile, ...] = ()
    version_gates: tuple[VersionGate, ...] = ()

    @property
    def custom_domains(self) -> frozenset[str]:
        """The fragile pieces' integrations: a developer-blog notice can break them."""
        return frozenset(d for piece in self.fragile for d in piece.domains)


@dataclass(frozen=True, slots=True)
class ReloadProfile:
    # The default allowlist when the add-on option `capabilities.reloadable_domains` is empty.
    domains: tuple[str, ...] = ()
    # Added to the never-reload list in code.
    deny: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class GitProfile:
    """Git proposals: off unless enabled here (no deploy key is generated while off)."""

    enabled: bool = False
    author_name: str = ""
    author_email: str = ""
    proposals: tuple[str, ...] = tuple(KINDS)


@dataclass(frozen=True, slots=True)
class DocsProfile:
    # The runbook and the house facts the prompts point at; `readable` adds files or directories Claude may read.
    runbook: str = ""
    facts: str = ""
    readable: tuple[str, ...] = ()

    @property
    def read_extra(self) -> tuple[str, ...]:
        found = [p for p in (self.runbook, self.facts, *self.readable) if p]
        return tuple(dict.fromkeys(found))


@dataclass(frozen=True, slots=True)
class HouseProfile:
    """The parsed profile; the defaults are the generic agent (no profile file)."""

    # PROFILE_PATH when a file was read and accepted; empty for the generic profile.
    source: str = ""
    # The push target when the add-on option `notify.service` is empty.
    notify_service: str = ""
    self_slug: str = SELF_SLUG
    overlays: frozenset[str] = frozenset()
    house_notes: str = ""
    health: HealthProfile = field(default_factory=HealthProfile)
    updates: UpdatesProfile = field(default_factory=UpdatesProfile)
    reload: ReloadProfile = field(default_factory=ReloadProfile)
    git: GitProfile = field(default_factory=GitProfile)
    docs: DocsProfile = field(default_factory=DocsProfile)

    def overlay(self, name: str) -> bool:
        return name in self.overlays

    @property
    def proposal_rules(self) -> ProposalRules:
        """What a change proposal may touch: nothing while git is off."""
        if not self.git.enabled:
            return ProposalRules()
        return ProposalRules.build(self.git.proposals, self.docs.facts)


# ------------------------------------------------------------------ parsing


def _mapping(value: Any, where: str, allowed: Iterable[str]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ProfileError(f"{where} must be a mapping")
    keys = set(allowed)
    unknown = sorted(k for k in value if k not in keys)
    if unknown:
        raise ProfileError(f"{where}: unknown key {unknown[0][:40]!r} (allowed: {', '.join(sorted(keys))})")
    return value


def _text(value: Any, where: str, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProfileError(f"{where} must be {what}")
    return value


def _texts(value: Any, where: str, pattern: re.Pattern[str], what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise ProfileError(f"{where} must be a list of at most {MAX_LIST} entries")
    found = tuple(_text(v, f"{where}[{i}]", pattern, what) for i, v in enumerate(value))
    if len(set(found)) != len(found):
        raise ProfileError(f"{where} lists an entry twice")
    return found


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ProfileError(f"{where} must be true or false")
    return value


def _required(raw: Mapping[str, Any], where: str, *keys: str) -> None:
    missing = [k for k in keys if k not in raw]
    if missing:
        raise ProfileError(f"{where}: {missing[0]} is required")


def _fragile(raw: Any, where: str) -> Fragile:
    entry = _mapping(raw, where, ("name", "tier", "reason", "entities", "repos", "domains", "bumps"))
    _required(entry, where, "name", "tier", "reason")
    tier = entry["tier"]
    if tier not in FRAGILE_TIERS:
        # Never `auto`: a fragile piece only ever holds an update back.
        raise ProfileError(f"{where}.tier must be one of {', '.join(FRAGILE_TIERS)}")
    entities = _texts(entry.get("entities"), f"{where}.entities", _UPDATE_ENTITY, "update entity ids")
    repos = _texts(entry.get("repos"), f"{where}.repos", _REPO, "lower-case owner/name GitHub repositories")
    domains = _texts(entry.get("domains"), f"{where}.domains", _DOMAIN, "integration domains")
    if not (entities or repos or domains):
        raise ProfileError(f"{where} needs entities, repos or domains to match")
    bumps = _texts(entry.get("bumps"), f"{where}.bumps", re.compile("patch|minor|major|unknown"),
                   "patch, minor, major or unknown")  # fmt: skip
    return Fragile(
        name=_text(entry["name"], f"{where}.name", _NAME, "a lower-case name"),
        tier=FRAGILE_TIERS[tier],
        reason=_text(entry["reason"], f"{where}.reason", _LINE, "one line of at most 300 characters"),
        entities=frozenset(entities),
        repos=frozenset(repos),
        domains=frozenset(domains),
        bumps=frozenset(Bump(b) for b in bumps),
    )


def _gate(raw: Any, where: str, overlays: frozenset[str]) -> VersionGate:
    entry = _mapping(raw, where, ("integration", "core_from", "reason", "patch"))
    _required(entry, where, "integration", "core_from", "reason")
    core_from = entry["core_from"]
    version = parse_version(core_from) if isinstance(core_from, str) else None
    if version is None or len(version) < 2:
        raise ProfileError(f'{where}.core_from must be a Core version as text, like "2026.12"')
    patch = ""
    if "patch" in entry:
        patch = _text(entry["patch"], f"{where}.patch", _PATCH_ID, "a local patch id")
        if "localpatches" not in overlays:
            raise ProfileError(f"{where}.patch needs the localpatches overlay")
    return VersionGate(
        integration=_text(entry["integration"], f"{where}.integration", _DOMAIN, "an integration domain"),
        core_from=core_from,
        reason=_text(entry["reason"], f"{where}.reason", _LINE, "one line of at most 300 characters"),
        patch=patch,
    )


def _list(value: Any, where: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise ProfileError(f"{where} must be a list of at most {MAX_LIST} entries")
    return value


def _doc(value: Any, where: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.endswith(".md") or not valid_read_entry(value):
        raise ProfileError(f"{where} must be a plain relative .md path")
    if pattern is not None and not pattern.fullmatch(value):
        raise ProfileError(f"{where} must be docs/<name>.md (lower-case)")
    return value


def overlay_installed(name: str) -> bool:
    """Whether this build ships the overlay's module. Public builds ship none, so a profile naming one is refused
    instead of failing later at the overlay's first import."""
    try:
        return importlib.util.find_spec(f"opsd.house.{name}") is not None
    except ModuleNotFoundError:
        return False


def parse(data: Any) -> HouseProfile:
    """The profile from parsed YAML. Raises ProfileError on anything outside the schema."""
    top = _mapping(data, "the profile", ("notify_service", "self_slug", "overlays", "house_notes", "health",
                                         "updates", "reload", "git", "docs"))  # fmt: skip
    overlays = _texts(top.get("overlays"), "overlays", re.compile("|".join(OVERLAYS)), "one of " + ", ".join(OVERLAYS))
    missing = [name for name in overlays if not overlay_installed(name)]
    if missing:
        raise ProfileError(f"overlays: {missing[0]} is not installed in this build (opsd.house.{missing[0]})")
    overlay_set = frozenset(overlays)

    health_raw = _mapping(top.get("health"), "health", ("power_entity", "remote_entity"))
    health = HealthProfile(
        power_entity=_text(health_raw.get("power_entity", ""), "health.power_entity",
                           re.compile(f"({_BINARY_SENSOR.pattern})?"), "a binary_sensor entity id"),
        remote_entity=_text(health_raw.get("remote_entity", ""), "health.remote_entity",
                            re.compile(f"({_BINARY_SENSOR.pattern})?"), "a binary_sensor entity id"),
    )  # fmt: skip

    updates_raw = _mapping(top.get("updates"), "updates", ("need_approval", "watched_entities", "fragile",
                                                           "version_gates"))  # fmt: skip
    fragile = tuple(_fragile(f, f"updates.fragile[{i}]") for i, f in
                    enumerate(_list(updates_raw.get("fragile"), "updates.fragile")))  # fmt: skip
    if len({f.name for f in fragile}) != len(fragile):
        raise ProfileError("updates.fragile names a piece twice")
    updates = UpdatesProfile(
        need_approval=_texts(updates_raw.get("need_approval"), "updates.need_approval", _UPDATE_ENTITY,
                             "update entity ids"),
        watched_entities=_texts(updates_raw.get("watched_entities"), "updates.watched_entities", _ENTITY,
                                "entity ids"),
        fragile=fragile,
        version_gates=tuple(_gate(g, f"updates.version_gates[{i}]", overlay_set) for i, g in
                            enumerate(_list(updates_raw.get("version_gates"), "updates.version_gates"))),
    )  # fmt: skip

    reload_raw = _mapping(top.get("reload"), "reload", ("domains", "deny"))
    domains = _texts(reload_raw.get("domains"), "reload.domains", _DOMAIN, "integration domains")
    deny = frozenset(_texts(reload_raw.get("deny"), "reload.deny", _DOMAIN, "integration domains"))
    for domain in domains:
        if domain in NEVER_RELOAD_DOMAINS or domain in deny:
            raise ProfileError(f"reload.domains: {domain} is never reloaded (locks, alarm, access, radios or core"
                               " plumbing, or reload.deny)")  # fmt: skip

    git_raw = _mapping(top.get("git"), "git", ("enabled", "author_name", "author_email", "proposals"))
    enabled = _bool(git_raw.get("enabled", False), "git.enabled")
    if enabled:
        _required(git_raw, "git", "author_name", "author_email")
    git = GitProfile(
        enabled=enabled,
        author_name=_text(git_raw.get("author_name", "Ops Agent"), "git.author_name", _PERSON, "a name"),
        author_email=_text(git_raw.get("author_email", "ops-agent@localhost.localdomain"), "git.author_email",
                           _EMAIL, "an email address"),
        proposals=_texts(git_raw.get("proposals", list(KINDS)), "git.proposals", re.compile("|".join(KINDS)),
                         "one of " + ", ".join(KINDS)),
    )  # fmt: skip

    docs_raw = _mapping(top.get("docs"), "docs", ("runbook", "facts", "readable"))
    readable = _texts(docs_raw.get("readable"), "docs.readable", re.compile(r".{1,300}"), "paths")
    for i, entry in enumerate(readable):
        if not valid_read_entry(entry):
            raise ProfileError(f"docs.readable[{i}] must be a plain relative .md file or a top-level directory"
                               " (never hidden, secrets, git, the add-ons or ops/)")  # fmt: skip
    docs = DocsProfile(
        runbook=_doc(docs_raw["runbook"], "docs.runbook") if "runbook" in docs_raw else "",
        facts=_doc(docs_raw["facts"], "docs.facts", FACTS_RE) if "facts" in docs_raw else "",
        readable=readable,
    )

    notes = top.get("house_notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES or _NOTES_BAD.search(notes):
        raise ProfileError(f"house_notes must be text of at most {MAX_NOTES} characters (no control characters)")

    return HouseProfile(
        source=PROFILE_PATH,
        notify_service=_text(top.get("notify_service", ""), "notify_service", re.compile(f"({_SERVICE.pattern})?"),
                             "mobile_app_<device>"),
        self_slug=_text(top.get("self_slug", SELF_SLUG), "self_slug", _SLUG, "<repository>_ha_ops_agent"),
        overlays=overlay_set,
        house_notes=notes.strip(),
        health=health,
        updates=updates,
        reload=ReloadProfile(domains=domains, deny=deny),
        git=git,
        docs=docs,
    )  # fmt: skip


def parse_text(text: str) -> HouseProfile:
    try:
        data = parse_yaml(text)
    except ConfigRefused as err:
        raise ProfileError(str(err)[:200]) from err
    return parse({} if data is None else data)


def _read(root: Path) -> bytes | None:
    """The profile's bytes, never through a symlink; None when there is no profile."""
    directory = root / Path(PROFILE_PATH).parent
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ProfileError(f"{directory.name}/ is not a plain directory")
    try:
        fd = os.open(root / PROFILE_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as err:
        raise ProfileError(f"cannot be read ({type(err).__name__})") from err
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProfileError("is not a regular file")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ProfileError(f"is larger than {MAX_BYTES // 1024} KB")
    return data


def load(root: Path) -> tuple[HouseProfile, tuple[str, ...]]:
    """The profile under the config root and its problems. Missing: generic. Invalid: generic plus the reason."""
    try:
        raw = _read(root)
        if raw is None:
            return HouseProfile(), ()
        return parse_text(raw.decode("utf-8")), ()
    except (ProfileError, UnicodeDecodeError) as err:
        return HouseProfile(), (f"house profile {PROFILE_PATH}: {str(err)[:300]}",)
