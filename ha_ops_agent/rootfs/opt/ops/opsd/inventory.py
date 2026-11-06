"""What this install configures, read-only: the inventory proactive maintenance matches notices against (spec
maintenance.md M7-M8). Config entries, YAML keys and platforms, custom integrations, the readable YAML text (in
memory for the scan only) and dashboard card types; names are matched, never followed.
"""

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from opsd import safefs
from opsd.conversation import clean_text
from opsd.evidence import DOMAIN_RE, addon_repo

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REFS = 12
_TOP_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]{1,63}):", re.MULTILINE)
_PLATFORM_RE = re.compile(r"^\s*-?\s*platform:\s*['\"]?([a-z0-9_]{2,64})['\"]?\s*(?:#.*)?$", re.MULTILINE)
# Files whose `platform:` lines are triggers: automations, scripts and blueprints.
_TRIGGER_FILES_RE = re.compile(r"(?:automations?|scripts?|blueprints)(?:/|\.ya?ml$)")
# Container keys whose mappings are cards (not views, badges or picture elements).
_CARD_KEYS = frozenset({"cards", "card"})
_CARD_TYPE_RE = re.compile(r"(?:custom:)?[a-z0-9][a-z0-9_-]{0,79}")
# Home Assistant's rule for a storage dashboard's url_path: lower case, with a hyphen.
_DASHBOARD_PATH_RE = re.compile(r"[a-z0-9][a-z0-9_]*(?:-[a-z0-9_]+)+")
MAX_DASHBOARDS = 20
# An entity id as a dashboard config or a template names it.
_ENTITY_ID_RE = re.compile(r"(?<![A-Za-z0-9_.])[a-z_][a-z0-9_]{0,63}\.[a-z0-9_]{1,128}(?![A-Za-z0-9_])")
# Per dashboard and per integration: at most this many entity ids are kept (in memory, for the scan).
MAX_ENTITY_IDS = 2000
# "*" in `Inventory.dashboards`: the dashboard list itself could not be read.
ALL_DASHBOARDS = "*"
# A YAML file that exposes entities to a voice assistant or HomeKit (its text names one of these).
_EXPOSURE_RE = re.compile(r"(?m)^\s*(?:homekit|alexa|google_assistant|include_entities|exclude_entities)\s*:")
# Top-level keys of configuration.yaml that are Home Assistant's own structure, not integrations.
_STRUCTURE = frozenset({"homeassistant", "default_config", "frontend", "http", "logger", "recorder", "automation",
                        "script", "scene", "template", "group", "sensor", "binary_sensor", "input_boolean",
                        "input_number", "input_select", "input_text", "input_datetime", "input_button", "counter",
                        "timer", "alert", "lovelace", "panel_custom", "shell_command", "notify"})  # fmt: skip


@dataclass(slots=True)
class Inventory:
    entry_domains: frozenset[str] = frozenset()
    domains: frozenset[str] = frozenset()
    # file -> its YAML features (`<key>` for an integration key, `platform:<name>`)
    yaml: dict[str, list[str]] = field(default_factory=dict)
    # domain -> {name, version, repo, platforms}
    custom: dict[str, dict[str, Any]] = field(default_factory=dict)
    # url_path ("" is the default dashboard) -> {cards: {card type: count}}
    dashboards: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    # The YAML text, for `references` (in memory only, never stored).
    texts: dict[str, str] = field(default_factory=dict, repr=False)
    # Readable config files whose text could not be read ("config files" when none could be listed).
    unread: list[str] = field(default_factory=list)

    def entity_usage(self, domain: str) -> dict[str, Any]:
        """Where the entities of a custom integration (its registry rows) are referenced: the entity ids used, the
        config files and dashboards that name them, whether a file that names one exposes entities to a voice
        assistant or HomeKit, and the sources that could not be read (spec maintenance.md M5d)."""
        ids = set((self.custom.get(domain) or {}).get("entity_ids") or [])
        used: set[str] = set()
        files: set[str] = set()
        exposed = False
        for path, text in sorted(self.texts.items()):
            named = ids & set(_ENTITY_ID_RE.findall(text))
            if named:
                used |= named
                files.add(path)
                exposed = exposed or bool(_EXPOSURE_RE.search(text))
        boards: set[str] = set()
        unknown = list(self.unread)
        for url_path, board in sorted(self.dashboards.items()):
            name = "dashboards" if url_path == ALL_DASHBOARDS else f"dashboard {url_path or 'overview'}"
            if board.get("unreadable"):
                unknown.append(name)
                continue
            named = ids & set(board.get("entities") or [])
            if named:
                used |= named
                boards.add(url_path or "overview")
        return {"entities": len(ids), "used": len(used), "files": sorted(files), "dashboards": sorted(boards),
                "exposed": exposed, "unknown": unknown}  # fmt: skip

    def yaml_domains(self) -> set[str]:
        found: set[str] = set()
        for features in self.yaml.values():
            found.update(f.removeprefix("platform:") for f in features)
        return found

    def configured_domains(self) -> frozenset[str]:
        """The integrations set up here: config entries, YAML integration keys and platforms, custom ones."""
        return frozenset(self.entry_domains | set(self.custom) | self.yaml_domains())

    def configured(self, domain: str | None) -> bool:
        """Whether `domain` is set up here (`configured_domains`); a loaded component alone is not."""
        return bool(domain) and domain in self.configured_domains()

    def references(self, term: str, *, limit: int = MAX_REFS) -> list[str]:
        """Where a term is used: `<file>:<line>` in the readable YAML, and `dashboard <url_path>` for a card type."""
        if len(term) < 3:
            return []
        found: list[str] = []
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])")
        for path, text in sorted(self.texts.items()):
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    found.append(f"{path}:{number}")
                    if len(found) >= limit:
                        return found
        for url_path, board in sorted(self.dashboards.items()):
            if term in (board.get("cards") or {}):
                found.append(f"dashboard {url_path or 'overview'}")
        return found[:limit]


def yaml_features(path: str, text: str) -> list[str]:
    """The integration keys (configuration.yaml and packages only) and the platforms a YAML file names. In an
    automation, script or blueprint file `platform:` names a trigger (`state`, `event`, `time`), not an integration
    set up here, so those files name none."""
    found: list[str] = []
    if not path.endswith((".yaml", ".yml")):
        return found
    if path == "configuration.yaml" or path.startswith("packages/"):
        found += [k for k in _TOP_KEY_RE.findall(text) if k not in _STRUCTURE]
    if not _TRIGGER_FILES_RE.match(path):
        found += [f"platform:{p}" for p in _PLATFORM_RE.findall(text)]
    return sorted(set(found))


def read_text(root: Path, rel: str, limit: int) -> str | None:
    """A regular file under the config root, never through a symlink, at most `limit` bytes; None otherwise."""
    data = safefs.read_or_none(root / rel, limit) if ".." not in PurePosixPath(rel).parts else None
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_yaml(root: Path, names: Iterable[str], *, unread: list[str] | None = None) -> dict[str, str]:
    """The readable YAML files' text, and the template macros' (`custom_templates/*.jinja`), `names` from the
    export's own allowlist, within the size limits; a file that could not be read is added to `unread`."""
    texts: dict[str, str] = {}
    budget = MAX_TOTAL_BYTES
    for name in names:
        if not name.endswith((".yaml", ".yml")) and not (name.startswith("custom_templates/") and
                                                         name.endswith(".jinja")):  # fmt: skip
            continue
        text = read_text(root, name, MAX_FILE_BYTES)
        size = len(text.encode()) if text is not None else 0
        if text is None or size > budget:
            if unread is not None:
                unread.append(name)
            continue
        budget -= size
        texts[name] = text
    return texts


def manifest(custom_components: Path, domain: str) -> dict[str, Any]:
    """A custom integration's name, version and GitHub repository from its manifest (cleaned)."""
    if not DOMAIN_RE.fullmatch(domain):
        return {}
    text = read_text(custom_components, f"{domain}/manifest.json", MAX_MANIFEST_BYTES)
    try:
        raw = json.loads(text) if text else {}
    except ValueError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    repo = next((r for r in (addon_repo(str(raw.get(k) or "")) for k in ("issue_tracker", "documentation")) if r),
                None)  # fmt: skip
    return {"name": clean_text(str(raw.get("name") or domain), 60),
            "version": clean_text(str(raw.get("version") or ""), 30), "repo": repo}  # fmt: skip


# HACS's catalog (`.storage/hacs.repositories`; spec maintenance.md M5c): read by the daemon only, bounded.
CATALOG_MAX_BYTES = 32 * 1024 * 1024
CATALOG_MAX_ENTRIES = 20_000
CATALOG_TEXT = 300
_FULL_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_CATEGORY_RE = re.compile(r"[a-z_]{1,32}")


def _catalog_entry(raw: Any) -> dict[str, Any] | None:
    """One catalog entry, cleaned (untrusted text capped); None for one without a usable repository name."""
    if not isinstance(raw, Mapping):
        return None
    name = raw.get("full_name")
    if not isinstance(name, str) or not _FULL_NAME_RE.fullmatch(name) or ".." in name:
        return None
    category = str(raw.get("category") or "")
    domain = str(raw.get("domain") or "")
    stars = raw.get("stargazers_count")
    listed = raw.get("topics")
    topics: list[Any] = listed if isinstance(listed, list) else []
    updated = raw.get("last_updated")
    return {
        "full_name": name,
        "category": category if _CATEGORY_RE.fullmatch(category) else "",
        "domain": domain if DOMAIN_RE.fullmatch(domain) else None,
        "name": clean_text(str(raw.get("manifest_name") or ""), 80),
        "description": clean_text(str(raw.get("description") or ""), CATALOG_TEXT),
        "topics": [clean_text(t, 50) for t in topics[:20] if isinstance(t, str)],
        "stars": stars if isinstance(stars, int) and not isinstance(stars, bool) and 0 <= stars < 10**9 else 0,
        "last_updated": clean_text(updated, 40) if isinstance(updated, str) else "",
        "last_version": clean_text(str(raw.get("last_version") or ""), 40),
        "archived": raw.get("archived") is True,
        # HACS installed it here: the source of a custom integration's repository when its manifest names none.
        "installed": raw.get("installed") is True,
    }


def hacs_catalog(path: Path) -> list[dict[str, Any]] | None:
    """HACS's own cached catalog of repositories, read-only: a regular file never read through a link, at most
    `CATALOG_MAX_BYTES`, of HACS's storage shape (`data` maps repository ids to entries, or lists them). None when it
    is absent, unreadable, too large or of another shape: the caller then has no catalog (fails closed)."""
    data = safefs.read_or_none(path, CATALOG_MAX_BYTES)
    if data is None:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    body = raw.get("data") if isinstance(raw, Mapping) else None
    entries: list[Any]
    if isinstance(body, Mapping):
        entries = list(body.values())
    elif isinstance(body, list):
        entries = body
    else:
        return None
    found = [e for e in (_catalog_entry(r) for r in entries[:CATALOG_MAX_ENTRIES]) if e is not None]
    return found


def _walk(node: Any, container: str | None = None) -> Iterator[tuple[str | None, Mapping[str, Any]]]:
    if isinstance(node, Mapping):
        yield container, node
        for key, child in node.items():
            yield from _walk(child, str(key))
    elif isinstance(node, list):
        for child in node:
            yield from _walk(child, container)


def entity_ids_in(config: Any) -> list[str]:
    """The entity ids a dashboard config names anywhere (a card's `entity`, `entities`, a template), capped."""
    found: set[str] = set()
    stack = [config]
    while stack and len(found) < MAX_ENTITY_IDS:
        node = stack.pop()
        if isinstance(node, Mapping):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and "." in node:
            found.update(_ENTITY_ID_RE.findall(node[:4000]))
    return sorted(found)[:MAX_ENTITY_IDS]


def card_types(config: Any) -> dict[str, int]:
    """How many cards of each type a dashboard config holds (nested stacks included)."""
    found: Counter[str] = Counter()
    for container, node in _walk(config):
        kind = node.get("type")
        if container in _CARD_KEYS and isinstance(kind, str) and _CARD_TYPE_RE.fullmatch(kind):
            found[kind] += 1
    return dict(found)


async def read_dashboards(ws: Callable[..., Awaitable[Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(each dashboard's card types by url_path, problems), read-only. The default dashboard is "" here; one Home
    Assistant generates (no stored config) is left out."""
    found: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    try:
        listed = await ws("lovelace/dashboards/list")
    except Exception as err:  # noqa: BLE001 - a missing source is a problem, never a crash
        found[ALL_DASHBOARDS] = {"cards": {}, "unreadable": True}
        return found, [f"dashboards unreadable: {clean_text(str(err), 120)}"]
    boards: list[Mapping[str, Any]] = [{"url_path": ""}]
    boards += [b for b in listed if isinstance(b, Mapping)][:MAX_DASHBOARDS] if isinstance(listed, list) else []
    for board in boards:
        url_path = board.get("url_path")
        if not isinstance(url_path, str) or (url_path and not _DASHBOARD_PATH_RE.fullmatch(url_path)):
            continue
        try:
            config = await ws("lovelace/config", url_path=url_path) if url_path else await ws("lovelace/config")
        except Exception as err:  # noqa: BLE001 - "no config" is normal for a generated default dashboard
            if url_path:
                problems.append(f"dashboard {url_path}: {clean_text(str(err), 120)}")
                # What it references is unknown, never "nothing" (M5d).
                found[url_path] = {"cards": {}, "unreadable": True}
            continue
        if isinstance(config, Mapping):
            found[url_path] = {"cards": card_types(config), "entities": entity_ids_in(config)}
    return found, problems


def build(*, entries: Any, components: Any, texts: Mapping[str, str], custom: Mapping[str, Mapping[str, Any]],
          registry: Any, dashboards: Mapping[str, dict[str, Any]],
          problems: Iterable[str] = (), unread: Iterable[str] = ()) -> Inventory:  # fmt: skip
    """The inventory from what was read (pure; every input may be missing or malformed)."""
    entry_domains = {
        str(e.get("domain"))
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, Mapping) and not e.get("disabled_by") and DOMAIN_RE.fullmatch(str(e.get("domain") or ""))
    }
    loaded = {part for c in components for part in str(c).split(".")} if isinstance(components, list) else set()
    platforms: dict[str, set[str]] = {}
    for entry in registry if isinstance(registry, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("entity_id"), str):
            platforms.setdefault(str(entry.get("platform")), set()).add(str(entry["entity_id"]).split(".", 1)[0])
    ids: dict[str, list[str]] = {}
    for entry in registry if isinstance(registry, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("entity_id"), str):
            ids.setdefault(str(entry.get("platform")), []).append(str(entry["entity_id"]))
    custom_out = {d: dict(info) | {"platforms": sorted(platforms.get(d, set())),
                                   "entities": sum(1 for e in registry if isinstance(e, Mapping)
                                                   and e.get("platform") == d) if isinstance(registry, list) else 0,
                                   "entity_ids": sorted(set(ids.get(d, [])))[:MAX_ENTITY_IDS]}
                  for d, info in sorted(custom.items())}  # fmt: skip
    return Inventory(
        entry_domains=frozenset(entry_domains),
        domains=frozenset(d for d in entry_domains | loaded if DOMAIN_RE.fullmatch(d)),
        yaml={path: features for path, text in sorted(texts.items()) if (features := yaml_features(path, text))},
        custom=custom_out,
        dashboards=dict(dashboards),
        problems=list(problems),
        texts=dict(texts),
        unread=list(unread),
    )
