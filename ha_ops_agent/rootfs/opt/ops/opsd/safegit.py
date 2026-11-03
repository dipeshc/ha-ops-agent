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
  `core.gitProxy`, `core.worktree`, `core.alternateRefsCommand`, any
  remote other than `origin`, a remote's `uploadpack`/`receivepack`/`vcs`,
  or a driver name that cannot be written as a `-c` key.

That listing and the call are two processes, so a driver added to the
repository config between them would escape the overrides. The agent
therefore never runs a git command that converts work-tree content, the
only place a filter driver runs: nothing is staged with `add` or committed
with `commit`, the live index is never refreshed or edited entry by entry
(`update-index` re-checks racily clean entries by content), and nothing is
compared with `status` or a work-tree `diff`. A commit is built from
`main`'s tree and the approved bytes (`hash-object --stdin --no-filters`,
`mktree`, `commit-tree`, `update-ref`), and `read-tree` levels the index;
the work tree is compared with the index by hashing each file with
`hash-object --no-filters` and comparing the executable bit
(`status_lines`). Every other command the agent
runs reads only the object store, the index or the refs.
"""

import os
import re
import stat
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
        if section == "remote" and subsection != "origin":
            raise UnsafeRepoConfigError("refusing to run git: the repository config defines a remote other than origin")
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


def _run(repo: Path, *args: str, data: bytes | None = None, timeout: float = 60) -> bytes:
    done = subprocess.run(git_argv(repo, *args), input=data, capture_output=True, timeout=timeout, env=git_env(),
                          check=False)  # fmt: skip
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, ["git", args[0]], done.stdout, done.stderr)
    return done.stdout


def _trusts_filemode(repo: Path) -> bool:
    """Whether git compares the executable bit here (`core.fileMode`, true unless the repository says false)."""
    done = subprocess.run(git_argv(repo, "config", "--type=bool", "--get", "core.filemode"), capture_output=True,
                          timeout=30, env=git_env(), check=False)  # fmt: skip
    return done.stdout.strip() != b"false"


def status_lines(repo: Path, *, untracked: bool = True) -> list[str]:
    """`git status --porcelain=v1`-style lines (`XY path`), computed without running any filter.

    X compares the index with `HEAD` (`diff-index --cached`, objects only); Y compares the work tree with the
    index by hashing each tracked file as it is (`hash-object --no-filters`; a link by its target, a missing
    file as deleted); untracked files are listed by `ls-files --others --exclude-standard` (`??`). Unlike
    `git status`, nothing is refreshed and no clean filter runs, whatever the repository config says.
    """
    lines: dict[str, list[str]] = {}
    head = subprocess.run(git_argv(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"), capture_output=True,
                          timeout=60, env=git_env(), check=False)  # fmt: skip
    staged: dict[str, str] = {}
    if head.returncode == 0:
        fields = _run(repo, "diff-index", "--cached", "--no-renames", "--name-status", "-z", "HEAD").split(b"\0")
        for code, name in zip(fields[0::2], fields[1::2], strict=False):
            if code and name:
                staged[name.decode(errors="surrogateescape")] = code.decode()[:1]
    index: dict[str, tuple[str, str]] = {}
    for record in _run(repo, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, sha, _stage = meta.decode().split(" ")
        path = name.decode(errors="surrogateescape")
        index[path] = (mode, sha)
        if head.returncode != 0:
            staged[path] = "A"
    regular: list[str] = []
    worktree: dict[str, str] = {}
    filemode = _trusts_filemode(repo)
    for path, (mode, sha) in index.items():
        if mode == "160000":
            continue
        try:
            info = os.lstat(repo / path)
        except FileNotFoundError:
            worktree[path] = "D"
            continue
        except OSError:
            worktree[path] = "M"
            continue
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(repo / path).encode(errors="surrogateescape")
            found = _run(repo, "hash-object", "--no-filters", "--stdin", data=target).decode().strip()
            if mode != "120000" or found != sha:
                worktree[path] = "M"
        elif stat.S_ISREG(info.st_mode) and mode in ("100644", "100755") and "\n" not in path:
            if filemode and (mode == "100755") != bool(info.st_mode & 0o111):
                worktree[path] = "M"  # the executable bit alone changed
            else:
                regular.append(path)
        else:
            worktree[path] = "M"
    if regular:
        names = "".join(f"{p}\n" for p in regular).encode(errors="surrogateescape")
        hashes = _run(repo, "hash-object", "--no-filters", "--stdin-paths", data=names).decode().split()
        if len(hashes) != len(regular):
            raise subprocess.SubprocessError("git hash-object: unexpected output")
        for path, found in zip(regular, hashes, strict=True):
            if found != index[path][1]:
                worktree[path] = "M"
    for path in sorted({*staged, *worktree}):
        lines.setdefault(path, []).append(f"{staged.get(path, ' ')}{worktree.get(path, ' ')} {path}")
    out = [line for path in sorted(lines) for line in lines[path]]
    if untracked:
        others = _run(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        out += [f"?? {n.decode(errors='surrogateescape')}" for n in others if n]
    return out
