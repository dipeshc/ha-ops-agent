"""Git invocations that cannot be steered into running programs.

Repository config and attributes can name executables (fsmonitor, hooks,
pagers, editors, external diff, diff drivers, textconv, clean/smudge/process
filters, signing programs, credential helpers, askpass, proxies, remote
transports) or redirect where git reads and writes (includes, URL rewrites,
another work tree). Every git call the agent makes goes through
`git_argv`/`git_env`:

- system and global config are ignored, attributes come from the empty tree
  instead of the work tree, and `-c` overrides (which win over repository
  config) neutralise every program-naming key above, including every filter
  and diff driver the repository config defines, whatever its name;
- the only transport is SSH (`GIT_ALLOW_PROTOCOL`, which overrides every
  `protocol.*` key), askpass and the editor are disabled in the environment;
- before each call the repository's own config is listed (`--no-includes`,
  which runs nothing) and the call is refused when it holds a key a `-c`
  override cannot neutralise: `include.*`, `includeIf.*`, `url.*`,
  `core.gitProxy`, `core.worktree`, `core.alternateRefsCommand`, a remote's
  `uploadpack`/`receivepack`/`vcs`, or a driver name that cannot be
  written as a `-c` key.
"""

import os
import re
import subprocess
from pathlib import Path

# Git knows the empty tree without it being stored in the repository.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

# The transports git may use; tests that push to a local bare repository widen it to `file`.
ALLOWED_PROTOCOLS = "ssh"

SAFE_CONFIG = (
    "core.fsmonitor=",
    "core.hooksPath=/dev/null",
    "core.pager=cat",
    "core.editor=:",
    "sequence.editor=:",
    "core.askPass=",
    "core.attributesFile=/dev/null",
    "core.sshCommand=ssh",
    "credential.helper=",
    "diff.external=",
    "commit.gpgSign=false",
    "tag.gpgSign=false",
    "push.gpgSign=false",
    "gpg.program=false",
    "gpg.openpgp.program=false",
    "gpg.x509.program=false",
    "gpg.ssh.program=false",
    "gpg.ssh.defaultKeyCommand=false",
    "protocol.allow=never",
    "submodule.recurse=false",
    "fetch.recurseSubmodules=false",
    "push.recurseSubmodules=no",
    "pager.log=false",
    "pager.diff=false",
    "pager.show=false",
    "color.ui=false",
    "log.showSignature=false",
)
_FILTER_RE = re.compile(r"(?:^|\s)filter=(\S+)")
_DRIVER_VARS = {"filter": ("clean", "smudge", "process"), "diff": ("command", "textconv"), "merge": ("driver",)}
_REMOTE_PROGRAMS = ("uploadpack", "receivepack", "vcs")
_UNSAFE_KEYS = ("core.gitproxy", "core.worktree", "core.alternaterefscommand")
_UNSAFE_SECTIONS = ("include", "includeif", "url")
_DRIVER_NAME_RE = re.compile(r"[^=\s]{1,200}")


class UnsafeRepoConfigError(subprocess.SubprocessError):
    """The repository's own git config holds a key the agent refuses to run git with."""


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


def _split_key(key: str) -> tuple[str, str, str]:
    """`(section, subsection, variable)` of a listed key; the section and variable are lower case."""
    section, _, rest = key.partition(".")
    subsection, _, variable = rest.rpartition(".")
    return section.lower(), subsection, variable.lower()


def repo_config_keys(repo: Path) -> list[str]:
    """Every key in the repository's own config files (local and worktree), includes not followed."""
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), "-c", f"safe.directory={repo}", "config", "--list", "--no-includes", "-z",
             "--show-scope"],
            capture_output=True, timeout=30, env=git_env(), check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired as err:
        raise UnsafeRepoConfigError("git config: timed out") from err
    if done.returncode != 0:
        # Not a repository, or a config file git cannot parse: let the real call report it.
        return []
    keys: list[str] = []
    fields = done.stdout.split(b"\0")
    # With --show-scope -z each entry is `scope\0key\nvalue\0`.
    for scope, entry in zip(fields[0::2], fields[1::2], strict=False):
        if scope.decode(errors="replace") in ("local", "worktree"):
            keys.append(entry.decode(errors="replace").partition("\n")[0])
    return keys


def config_overrides(keys: list[str]) -> list[str]:
    """`-c` items neutralising every driver the config defines; refuses keys no override can neutralise."""
    drivers: dict[str, set[str]] = {kind: set() for kind in _DRIVER_VARS}
    for key in keys:
        section, subsection, variable = _split_key(key)
        if section in _UNSAFE_SECTIONS or key.lower() in _UNSAFE_KEYS:
            raise UnsafeRepoConfigError(f"refusing to run git: the repository config sets {key.split('.')[0]}.*")
        if section == "remote" and variable in _REMOTE_PROGRAMS:
            raise UnsafeRepoConfigError(f"refusing to run git: the repository config sets remote.*.{variable}")
        if section in drivers and subsection:
            if not _DRIVER_NAME_RE.fullmatch(subsection):
                raise UnsafeRepoConfigError(f"refusing to run git: unusable {section} driver name")
            drivers[section].add(subsection)
    out: list[str] = []
    for kind, names in drivers.items():
        for name in sorted(names):
            out += [f"{kind}.{name}.{variable}=" for variable in _DRIVER_VARS[kind]]
            if kind == "filter":
                out.append(f"filter.{name}.required=false")
    return out


def git_argv(repo: Path, *args: str) -> list[str]:
    """The argv for one git call in `repo`; raises `UnsafeRepoConfigError` on a config it cannot neutralise."""
    argv = ["git", "--no-pager", f"--attr-source={EMPTY_TREE}", "-C", str(repo), "-c", f"safe.directory={repo}"]
    config = list(SAFE_CONFIG)
    config += config_overrides(repo_config_keys(repo))
    # Belt and braces for attribute files naming a filter the config does not define (yet).
    for name in sorted(filter_drivers(repo)):
        if _DRIVER_NAME_RE.fullmatch(name):
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
            "GIT_ALLOW_PROTOCOL": ALLOWED_PROTOCOLS,
            # Set but empty: git then never falls back to core.askPass or SSH_ASKPASS.
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "SSH_ASKPASS_REQUIRE": "never",
            "GIT_EDITOR": ":",
            "GIT_SEQUENCE_EDITOR": ":",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    env.update(extra or {})
    return env
