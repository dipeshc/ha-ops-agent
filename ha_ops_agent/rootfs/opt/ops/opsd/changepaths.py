"""Which config files a change proposal may touch. Stdlib only: the guard hook imports it.

At most these, nothing else: the house facts document (`docs/<name>.md`, from the house
profile), `automations/**/*.yaml`, `alerts/*.yaml` and `templates/*.yaml`, each kind only when
the house profile turns it on (`ProposalRules`; with git off, nothing). Path parts are
lower-case letters, digits, `_` and `-` (no dots, so no `..`, hidden files or other
extensions), at most four directories deep under `automations/`. `configuration.yaml`,
secrets, `custom_components/`, `custom_templates/`, the add-ons, `.storage/`, `ops/`,
top-level Markdown files, `.gitignore` and `.git` can never match.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from opsd.selfguard import self_path

# Learned known-false-positive rules (opsd.house.suppress): written only by the daemon's own proposals,
# never from a model's draft, so it is not in `allowed_path`.
SUPPRESS_PATH = "docs/known-false-positives.yaml"
MAX_FILES = 3
MAX_FILE_BYTES = 64 * 1024
DRAFTS_DIR = "/run/ops-drafts"
# The run's proposal rules for the guard hook (set by the daemon in Claude's environment).
KINDS_ENV = "OPS_PROPOSAL_KINDS"
FACTS_ENV = "OPS_FACTS_PATH"

_PART = r"[a-z0-9][a-z0-9_-]{0,63}"
# The kinds `configcheck` knows how to check, by their directory.
KINDS: dict[str, re.Pattern[str]] = {
    "automations": re.compile(rf"automations/(?:{_PART}/){{0,4}}{_PART}\.yaml"),
    "alerts": re.compile(rf"alerts/{_PART}\.yaml"),
    "templates": re.compile(rf"templates/{_PART}\.yaml"),
}
GLOBS = {"automations": "automations/**/*.yaml", "alerts": "alerts/*.yaml", "templates": "templates/*.yaml"}
FACTS_RE = re.compile(rf"docs/{_PART}\.md")
# Ignored by the repo's .gitignore wherever they are, and never config a proposal should touch.
_DENY_NAMES = frozenset({"secrets.yaml", "ip_bans.yaml", "known_devices.yaml"})
# Directories a proposal may create (only below automations/).
CREATABLE_ROOT = "automations"


@dataclass(frozen=True, slots=True)
class ProposalRules:
    """What a proposal may touch. The default allows nothing (git proposals off)."""

    kinds: frozenset[str] = frozenset()
    facts: str = ""

    @classmethod
    def build(cls, kinds: Iterable[str], facts: str = "") -> ProposalRules:
        """Unknown kinds and a facts path of the wrong form are dropped (fail closed)."""
        return cls(frozenset(k for k in kinds if k in KINDS), facts if FACTS_RE.fullmatch(facts) else "")

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ProposalRules:
        return cls.build(environ.get(KINDS_ENV, "").split(","), environ.get(FACTS_ENV, ""))

    def env(self) -> dict[str, str]:
        return {KINDS_ENV: ",".join(sorted(self.kinds)), FACTS_ENV: self.facts}

    @property
    def on(self) -> bool:
        return bool(self.kinds or self.facts)

    def describe(self) -> str:
        """The allowed paths in words, for refusals and prompts."""
        found = [self.facts] if self.facts else []
        found += [GLOBS[k] for k in KINDS if k in self.kinds]
        return ", ".join(found) or "nothing (git proposals are off)"


# What an update's config fix may touch besides `_FIX` (opsd.updatefix): every kind, never a document.
FIX_RULES = ProposalRules(frozenset(KINDS))


def allowed_path(path: str, rules: ProposalRules) -> bool:
    """True for exactly the allowlisted, normalised relative paths the rules turn on."""
    if len(path) > 300 or path != path.strip() or path.rsplit("/", 1)[-1].lower() in _DENY_NAMES:
        return False
    # The agent never modifies itself (opsd.selfguard), whatever the allowlist below would say.
    if self_path(path):
        return False
    return (bool(rules.facts) and path == rules.facts) or any(
        KINDS[kind].fullmatch(path) for kind in rules.kinds if kind in KINDS
    )


# The files an update's config change proposal (opsd.updatefix) may name; the admin approves each one.
FIX_CONFIGURATION = "configuration.yaml"
_FIX = (
    re.compile(rf"templates/(?:{_PART}/){{0,4}}{_PART}\.yaml"),
    re.compile(rf"custom_templates/(?:{_PART}/){{0,4}}{_PART}\.jinja"),
)


def fix_path(path: str) -> bool:
    """True for exactly the files an update's breaking-change fix proposal may change (never a document)."""
    if len(path) > 300 or path != path.strip() or self_path(path) or path.startswith("docs/"):
        return False
    return path == FIX_CONFIGURATION or allowed_path(path, FIX_RULES) or any(p.fullmatch(path) for p in _FIX)


def is_doc(path: str) -> bool:
    """Documentation only: applying it changes no Home Assistant behaviour."""
    return path == SUPPRESS_PATH or bool(FACTS_RE.fullmatch(path))


def split_paths(value: str) -> list[str]:
    """`a.yaml,b.yaml` -> unique paths, in order (shape only; `allowed_path` decides)."""
    seen: list[str] = []
    for part in value.split(","):
        if part and part not in seen:
            seen.append(part)
    return seen
