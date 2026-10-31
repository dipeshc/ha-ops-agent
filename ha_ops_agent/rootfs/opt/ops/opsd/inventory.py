"""What this install uses, read-only: the inventory proactive maintenance matches notices against.

- integrations: the domains of the enabled config entries, and Core's loaded components (both halves of
  `<platform>.<integration>`), which include the ones set up in YAML;
- YAML features: in the config files the agent may read (the tracked files when git is on, else the
  export's allowlist of files on disk, `privsep.disk_files`; never secrets, `.storage/` or hidden
  paths), the top-level integration keys of `configuration.yaml` and `packages/`, and every
  `platform: <name>`. The text is kept in memory for the scan only, to find where a term is used;
- custom integrations (their manifests' name, version and GitHub repository) and the entity
  platforms each one provides here (from the entity registry);
- frontend plugins (the dashboard resources) and every dashboard's mode and card types
  (`opsd.dashboards.read_all`);
- the installed add-ons.

Everything here is data from this install. Names are matched, never followed: nothing in a file or a
manifest decides what the agent does.
"""

import json
import os
import re
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from opsd.conversation import clean_text
from opsd.evidence import addon_repo

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_REFS = 12
_TOP_KEY_RE = re.compile(r"^([a-z_][a-z0-9_]{1,63}):", re.MULTILINE)
_PLATFORM_RE = re.compile(r"^\s*-?\s*platform:\s*['\"]?([a-z0-9_]{2,64})['\"]?\s*(?:#.*)?$", re.MULTILINE)
_DOMAIN_RE = re.compile(r"[a-z0-9_]{1,64}")
_PLUGIN_RE = re.compile(r"/(?:hacsfiles|local)/([A-Za-z0-9_.-]{1,80})/")
_FILE_RE = re.compile(r"/([A-Za-z0-9_.-]{1,80})\.js\b")
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
    plugins: list[str] = field(default_factory=list)
    # url_path ("" is the default dashboard) -> {mode, title, cards}
    dashboards: dict[str, dict[str, Any]] = field(default_factory=dict)
    addons: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    # The YAML text, for `references` (in memory only, never stored).
    texts: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def hacs(self) -> bool:
        return "hacs" in self.domains or "hacs" in self.custom

    def yaml_domains(self) -> set[str]:
        found: set[str] = set()
        for features in self.yaml.values():
            found.update(f.removeprefix("platform:") for f in features)
        return found

    def uses(self, domain: str | None) -> bool:
        return bool(domain) and (domain in self.domains or domain in self.custom or domain in self.yaml_domains())

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

    def to_json(self) -> dict[str, Any]:
        return {"entry_domains": sorted(self.entry_domains), "domains": sorted(self.domains),
                "yaml": {k: v for k, v in sorted(self.yaml.items())}, "custom": self.custom,
                "plugins": self.plugins, "dashboards": self.dashboards, "addons": self.addons,
                "problems": self.problems[:10]}  # fmt: skip


def yaml_features(path: str, text: str) -> list[str]:
    """The integration keys (configuration.yaml and packages only) and the platforms a YAML file names."""
    found: list[str] = []
    if path == "configuration.yaml" or path.startswith("packages/"):
        found += [k for k in _TOP_KEY_RE.findall(text) if k not in _STRUCTURE]
    found += [f"platform:{p}" for p in _PLATFORM_RE.findall(text)]
    return sorted(set(found))


def read_text(root: Path, rel: str, limit: int) -> str | None:
    """A regular file under the config root, never through a symlink, at most `limit` bytes; None otherwise."""
    if ".." in PurePosixPath(rel).parts:
        return None
    try:
        fd = os.open(root / rel, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_yaml(root: Path, names: Iterable[str]) -> dict[str, str]:
    """The readable YAML files' text (`names` from the export's own allowlist), within the size limits."""
    texts: dict[str, str] = {}
    budget = MAX_TOTAL_BYTES
    for name in names:
        if not name.endswith((".yaml", ".yml")):
            continue
        text = read_text(root, name, MAX_FILE_BYTES)
        size = len(text.encode()) if text is not None else 0
        if text is None or size > budget:
            continue
        budget -= size
        texts[name] = text
    return texts


def manifest(custom_components: Path, domain: str) -> dict[str, Any]:
    """A custom integration's name, version and GitHub repository from its manifest (cleaned)."""
    if not _DOMAIN_RE.fullmatch(domain):
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


def plugin_names(resources: Any) -> list[str]:
    found: list[str] = []
    for entry in resources if isinstance(resources, list) else []:
        url = str(entry.get("url") or "") if isinstance(entry, Mapping) else ""
        match = _PLUGIN_RE.search(url) or _FILE_RE.search(url)
        if match and match.group(1) not in found:
            found.append(match.group(1))
    return found[:50]


def build(*, entries: Any, components: Any, texts: Mapping[str, str], custom: Mapping[str, Mapping[str, Any]],
          registry: Any, resources: Any, dashboards: Mapping[str, dict[str, Any]], addons: Any,
          problems: Iterable[str] = ()) -> Inventory:  # fmt: skip
    """The inventory from what was read (pure; every input may be missing or malformed)."""
    entry_domains = {
        str(e.get("domain"))
        for e in (entries if isinstance(entries, list) else [])
        if isinstance(e, Mapping) and not e.get("disabled_by") and _DOMAIN_RE.fullmatch(str(e.get("domain") or ""))
    }
    loaded = {part for c in components for part in str(c).split(".")} if isinstance(components, list) else set()
    platforms: dict[str, set[str]] = {}
    for entry in registry if isinstance(registry, list) else []:
        if isinstance(entry, Mapping) and isinstance(entry.get("entity_id"), str):
            platforms.setdefault(str(entry.get("platform")), set()).add(str(entry["entity_id"]).split(".", 1)[0])
    custom_out = {d: dict(info) | {"platforms": sorted(platforms.get(d, set())),
                                   "entities": sum(1 for e in registry if isinstance(e, Mapping)
                                                   and e.get("platform") == d) if isinstance(registry, list) else 0}
                  for d, info in sorted(custom.items())}  # fmt: skip
    slugs = [str(a.get("slug")) for a in (addons or {}).get("addons", []) if isinstance(a, Mapping)
             and a.get("slug")] if isinstance(addons, Mapping) else []  # fmt: skip
    return Inventory(
        entry_domains=frozenset(entry_domains),
        domains=frozenset(d for d in entry_domains | loaded if _DOMAIN_RE.fullmatch(d)),
        yaml={path: features for path, text in sorted(texts.items()) if (features := yaml_features(path, text))},
        custom=custom_out,
        plugins=plugin_names(resources),
        dashboards=dict(dashboards),
        addons=slugs[:100],
        problems=list(problems),
        texts=dict(texts),
    )
