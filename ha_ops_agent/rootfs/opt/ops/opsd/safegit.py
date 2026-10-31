"""Git invocations that cannot be steered into running programs.

Repository config and attributes can name executables (fsmonitor, hooks,
pagers, external diff, textconv, clean/smudge/process filters). Every git
call the agent makes goes through `git_argv`/`git_env`, which neutralise
those, read attributes from the empty tree instead of the work tree, and
ignore system and global config.
"""

import os
import re
from pathlib import Path

# Git knows the empty tree without it being stored in the repository.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

SAFE_CONFIG = (
    "core.fsmonitor=",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "core.attributesFile=/dev/null",
    "core.sshCommand=ssh",
    "diff.external=",
    "pager.log=false",
    "pager.diff=false",
    "pager.show=false",
    "color.ui=false",
    "log.showSignature=false",
)
_FILTER_RE = re.compile(r"(?:^|\s)filter=([A-Za-z0-9_.-]+)")


def filter_drivers(repo: Path) -> set[str]:
    """Filter driver names referenced by in-repo attribute files."""
    names: set[str] = set()
    for attributes in (repo / ".gitattributes", repo / ".git" / "info" / "attributes"):
        try:
            text = attributes.read_text(errors="replace")
        except OSError:
            continue
        names.update(_FILTER_RE.findall(text))
    return names


def git_argv(repo: Path, *args: str) -> list[str]:
    argv = ["git", "--no-pager", f"--attr-source={EMPTY_TREE}", "-C", str(repo), "-c", f"safe.directory={repo}"]
    config = list(SAFE_CONFIG)
    # Belt and braces for $GIT_DIR/info/attributes, which --attr-source does not replace.
    for name in sorted(filter_drivers(repo)):
        config += [f"filter.{name}.clean=", f"filter.{name}.smudge=", f"filter.{name}.process=",
                   f"filter.{name}.required=false"]  # fmt: skip
    for item in config:
        argv += ["-c", item]
    return [*argv, *args]


def git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TZ")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    env.update(extra or {})
    return env
