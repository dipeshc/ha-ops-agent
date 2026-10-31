"""Release notes and repository facts from the cloud: fetched, parsed and cached, read-only.

In use:

- `fetch_text`, the one way the agent reads the web: HTTPS to `ALLOWED_HOSTS` only, redirects
  included, at most `MAX_BYTES` (the health check's release ages, `opsd.api`).
- `EvidenceSource.core`, Home Assistant's own notes for a Core update: the release blog post's
  "Backward-incompatible changes" (one item per integration, with its `/integrations/<domain>/`
  link), its developer-blog list and "Farewell to the following" (removed integrations), and the
  GitHub release body of every patch between the installed and the target version (the
  integrations each line touches, and lines marked `(breaking-change)`). The update rule asks the
  admin when one of them names an integration this house uses (`opsd.updates`).

Kept for the next release (proactive maintenance), not used by the update rule: the release-note
parsing for add-ons, HACS and HAOS (`EvidenceSource.notes`) and the repository resolution
(`EvidenceSource.repository`, `addon_candidates`).

Everything fetched is untrusted third-party text: only extracted, cleaned and length-limited fields
are kept, never markup, and a model sees them fenced. Answers are cached in the store
(`CACHE_TTL`); a failed or unrecognised answer is never cached, and makes the evidence
`available=False`, which the update rule treats as "could not check: ask".
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from opsd.conversation import clean_text
from opsd.policy import parse_version
from opsd.store import Store, StoreError

_LOGGER = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"www.home-assistant.io", "api.github.com"})
MAX_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT_S = 20.0
CACHE_PREFIX = "evidence:"
CACHE_TTL = timedelta(hours=12)
CACHE_KEEP = timedelta(days=30)
RELEASE_INDEX = "https://www.home-assistant.io/blog/categories/release-notes/"
CORE_RELEASE = "https://api.github.com/repos/home-assistant/core/releases/tags/{tag}"
REPO_API = "https://api.github.com/repos/{repo}"
# GitHub answered "slow down" (403/429): no GitHub call until then, and the tracker counts as unavailable.
BACKOFF_KEY = "evidence:github_backoff_until"
BACKOFF = timedelta(minutes=30)
# The most patch bodies or blog posts one update looks at (beyond that: not enough evidence).
MAX_STEPS = 6
TEXT_CHARS = 900
TITLE_CHARS = 80
TERMS_MAX = 12
_BREAK_LINE_RE = re.compile(
    r"breaking|backward[- ]incompatible|⚠|manual (?:step|action|migration)|action required|requires? (?:a )?manual"
    r"|no longer supported|removed support|has been removed|deprecated .* removed",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"<code>(.*?)</code>", re.DOTALL)
_TICK_RE = re.compile(r"`([^`\n]{3,80})`")
_TAG_RE = re.compile(r"<[^>]+>")
_HA_ALERT_RE = re.compile(r"<ha-alert\b[^>]*>.*?</ha-alert>", re.DOTALL | re.IGNORECASE)
_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{2,80}")
_DOTTED_RE = re.compile(r"\b[a-z][a-z0-9_]{1,63}\.[a-z0-9_]{2,128}\b")
_INTEGRATION_LINK_RE = re.compile(r'href="/integrations/([a-z0-9_]+)(?:\.[a-z0-9_]+)?/?"')
_DOCS_RE = re.compile(r"\[([a-z0-9_]+) docs\]")
_REPO_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})")
_SAFE_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")


class FetchError(RuntimeError):
    """A cloud source could not be read (offline, refused, too large, not allowed)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Change:
    """One extracted breaking change or notice (cleaned, untrusted text)."""

    kind: str  # breaking | removed | devblog | notes
    title: str
    domain: str | None
    text: str
    terms: tuple[str, ...]
    source: str

    @property
    def id(self) -> str:
        raw = f"{self.kind}|{self.source}|{self.title}|{self.text[:200]}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "title": self.title, "domain": self.domain, "text": self.text,
                "terms": list(self.terms), "source": self.source}  # fmt: skip

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> Change:
        return cls(kind=str(raw.get("kind")), title=str(raw.get("title") or ""), domain=raw.get("domain") or None,
                   text=str(raw.get("text") or ""), terms=tuple(str(t) for t in raw.get("terms") or []),
                   source=str(raw.get("source") or ""))  # fmt: skip


@dataclass(slots=True)
class Evidence:
    """What is known about one update's changes. `available` False: not enough to be confident."""

    available: bool
    changes: list[Change] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"available": self.available, "changes": len(self.changes), "touched": sorted(self.touched)[:30],
                "sources": self.sources[:8], "problems": self.problems[:5]}  # fmt: skip


# ------------------------------------------------------------------ text


def plain(fragment: str, limit: int = TEXT_CHARS) -> str:
    """Markup removed, entities decoded, whitespace collapsed, capped: data only."""
    text = html.unescape(_TAG_RE.sub(" ", fragment))
    return clean_text(re.sub(r"\s+", " ", text), limit)


def terms_of(fragment: str) -> tuple[str, ...]:
    """Code identifiers a break names (`<code>` or backticks) and dotted names (`light.turn_on`)."""
    raw = [html.unescape(_TAG_RE.sub("", c)).strip() for c in _CODE_RE.findall(fragment)]
    raw += _TICK_RE.findall(fragment)
    raw += _DOTTED_RE.findall(plain(fragment, 4000))
    found: list[str] = []
    for entry in raw:
        token = entry.strip().strip("()")
        if _TERM_RE.fullmatch(token) and token not in found and not token.startswith(("http", "www.")):
            found.append(token)
    return tuple(found[:TERMS_MAX])


def _section(text: str, anchor: str) -> str:
    """From `name="<anchor>"` to the next `<h2`."""
    start = text.find(f'name="{anchor}"')
    if start < 0:
        return ""
    end = text.find("<h2", start)
    return text[start : end if end > 0 else len(text)]


# ------------------------------------------------------------------ parsers


def release_post_url(index_html: str, version: tuple[int, ...]) -> str | None:
    """The blog post of Core minor `version` (year, month) from the release-notes category page."""
    year, month = version[0], version[1]
    match = re.search(rf'href="(/blog/\d{{4}}/\d{{2}}/\d{{2}}/release-{year}{month:d}/)"', index_html)
    if match is None:
        match = re.search(rf'href="(/blog/\d{{4}}/\d{{2}}/\d{{2}}/release-{year}{month:02d}/)"', index_html)
    return f"https://www.home-assistant.io{match.group(1)}" if match else None


_DETAILS_RE = re.compile(
    r'<details class="details-block-item">.*?<summary[^>]*>\s*<span>(.*?)</span>.*?</summary>\s*'
    r'<div class="details-block-content"[^>]*>(.*?)</div>\s*</details>',
    re.DOTALL,
)
_LI_RE = re.compile(r"<li>(.*?)</li>", re.DOTALL)


def parse_release_post(page: str, source: str) -> list[Change]:
    """Backward-incompatible changes, developer-blog items and removed integrations of a Core release post.

    Fails closed (ValueError: missing evidence, never cached): the section's anchor must be there and
    hold at least one recognised item. Every Core minor has some; none parsed means the page changed.
    """
    changes: list[Change] = []
    section = _section(page, "backward-incompatible-changes")
    if not section:
        raise ValueError("release post without a recognised backward-incompatible section")
    found = _DETAILS_RE.findall(section)
    if not found or 'class="details-block' not in section:
        raise ValueError("release post: no backward-incompatible items recognised")
    for title, body in found:
        link = _INTEGRATION_LINK_RE.search(body)
        changes.append(Change("breaking", plain(title, TITLE_CHARS), link.group(1) if link else None, plain(body),
                              terms_of(body), source))  # fmt: skip
    dev = section.find("developer blog")
    if dev >= 0:
        listing = section[dev : section.find("</ul>", dev) if section.find("</ul>", dev) > 0 else len(section)]
        for item in _LI_RE.findall(listing):
            terms = terms_of(item)
            if terms:
                changes.append(Change("devblog", plain(item, TITLE_CHARS * 2), None, plain(item), terms, source))
    farewell = page.find('name="farewell-to-the-following"')
    if farewell >= 0:
        stop = min(x for x in (page.find("<h2", farewell), page.find("<h3", farewell + 10), len(page)) if x > 0)
        for item in _LI_RE.findall(page[farewell:stop]):
            name = re.search(r"<strong>(.*?)</strong>", item, re.DOTALL)
            if name is None:
                continue
            title = plain(name.group(1), TITLE_CHARS)
            domain = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or None
            changes.append(Change("removed", title, domain, plain(item), (), source))
    return changes


def parse_github_body(body: str, source: str) -> tuple[list[Change], set[str]]:
    """A Core patch release body: the integrations it touches and its `(breaking-change)` lines."""
    touched: set[str] = set()
    changes: list[Change] = []
    for line in body.splitlines():
        domains = _DOCS_RE.findall(line)
        touched.update(domains)
        if "breaking-change" in line or "breaking change" in line.lower():
            text = plain(line)
            changes.append(Change("breaking", text[:TITLE_CHARS], domains[0] if domains else None, text,
                                  terms_of(line), source))  # fmt: skip
    return changes, touched


def parse_changelog(notes: str, *, title: str, domain: str | None, source: str) -> list[Change]:
    """Release-note lines (add-ons, HACS, HAOS) that announce a breaking change or a manual step."""
    changes: list[Change] = []
    for block in re.split(r"\n\s*\n|\n(?=\s*[-*] )", notes):
        if _BREAK_LINE_RE.search(block):
            text = plain(block)
            changes.append(Change("notes", clean_text(title, TITLE_CHARS), domain, text, terms_of(block), source))
    return changes[:20]


# ------------------------------------------------------------------ fetching


class _SameHosts(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:  # noqa: PLR0917
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS:
        raise FetchError(f"not an allowed source: {parts.hostname}")


def fetch_text(url: str, *, timeout: float = FETCH_TIMEOUT_S) -> str:
    """GET over HTTPS from an allowed host (redirects too), at most `MAX_BYTES`."""
    _check_url(url)
    opener = urllib.request.build_opener(_SameHosts())
    accept = "application/vnd.github+json" if "api.github.com" in url else "text/html"
    request = urllib.request.Request(url, headers={"User-Agent": "ha-ops-agent", "Accept": accept})
    try:
        with opener.open(request, timeout=timeout) as resp:
            data = bytes(resp.read(MAX_BYTES + 1))
    except urllib.error.HTTPError as err:
        raise FetchError(f"{urllib.parse.urlsplit(url).hostname}: HTTP {err.code}", status=err.code) from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise FetchError(f"{urllib.parse.urlsplit(url).hostname}: {str(err)[:120]}") from err
    if len(data) > MAX_BYTES:
        raise FetchError("answer larger than the limit")
    return data.decode("utf-8", errors="replace")


class EvidenceCache:
    """Parsed evidence in the store's key-value table, by source; fresh for `CACHE_TTL`."""

    def __init__(self, store: Store, *, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._clock = clock

    @staticmethod
    def _key(name: str) -> str:
        return CACHE_PREFIX + hashlib.sha256(name.encode()).hexdigest()[:32]

    def get(self, name: str, *, stale_ok: bool = False) -> Any | None:
        try:
            raw = self._store.kv_get(self._key(name))
            stored = json.loads(raw) if raw else None
        except StoreError, ValueError, OSError:
            return None
        if not isinstance(stored, dict):
            return None
        at = datetime.fromisoformat(str(stored.get("at")))
        if not stale_ok and self._clock() - at > CACHE_TTL:
            return None
        return stored.get("data")

    def put(self, name: str, data: Any) -> None:
        now = self._clock()
        try:
            self._store.kv_set(self._key(name), json.dumps({"at": now.isoformat(), "data": data}), now=now)
        except (StoreError, OSError) as err:
            _LOGGER.warning("evidence cache write failed", extra={"error": str(err)[:200]})

    def prune(self) -> None:
        now = self._clock()
        try:
            for key, raw in self._store.kv_prefix(CACHE_PREFIX, limit=500).items():
                with_at = json.loads(raw)
                if now - datetime.fromisoformat(str(with_at.get("at"))) > CACHE_KEEP:
                    self._store.kv_set(key, "", now=now)
        except (StoreError, ValueError, OSError, AttributeError) as err:
            _LOGGER.info("evidence cache prune skipped", extra={"error": str(err)[:120]})


class EvidenceSource:
    """Fetches, parses and caches the evidence for one update at a time."""

    def __init__(
        self,
        *,
        cache: EvidenceCache,
        fetch: Callable[[str], str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache = cache
        # Looked up at call time, so tests can keep the network out (tests/conftest.py).
        self._fetch = fetch
        self._clock = clock

    async def _get(self, url: str, parse: Callable[[str], Any], *, fresh: bool = False) -> Any:
        """Parsed and cached on success only: a failed or unrecognised answer is fetched again next time."""
        cached = None if fresh else self._cache.get(url)
        if cached is not None:
            return cached
        try:
            text = await asyncio.to_thread(self._fetch or fetch_text, url)
            data = parse(text)
        except FetchError as err:
            _LOGGER.info("evidence unavailable", extra={"source": url[:120], "error": str(err)[:160]})
            raise
        except (ValueError, KeyError, TypeError) as err:
            raise FetchError(f"unreadable answer from {urllib.parse.urlsplit(url).hostname}: {str(err)[:120]}") from err
        self._cache.put(url, data)
        return data

    # ------------------------------------------------------------ Core

    async def core(self, installed: str, target: str) -> Evidence:
        old, new = parse_version(installed), parse_version(target)
        if old is None or new is None or len(old) < 3 or len(new) < 3:
            return Evidence(available=False, problems=["Core versions are not year.month.patch"])
        evidence = Evidence(available=True)
        try:
            if old[:2] == new[:2]:
                await self._core_patches(evidence, new[0], new[1], range(old[2] + 1, new[2] + 1))
            else:
                minors = _minors_between(old, new)
                if len(minors) > MAX_STEPS:
                    raise FetchError(f"{len(minors)} Core minors at once")
                index = await self._get(RELEASE_INDEX, lambda text: {"html": _release_links(text)})
                for year, month in minors:
                    url = release_post_url(str(index.get("html") or ""), (year, month))
                    if url is None:
                        # The cached index may predate the post: read it again once.
                        index = await self._get(RELEASE_INDEX, lambda text: {"html": _release_links(text)}, fresh=True)
                        url = release_post_url(str(index.get("html") or ""), (year, month))
                    if url is None:
                        raise FetchError(f"no release post found for {year}.{month}")

                    def parse(text: str, source: str = url) -> list[dict[str, Any]]:
                        return [c.to_json() for c in parse_release_post(text, source)]

                    items = await self._get(url, parse)
                    evidence.changes.extend(Change.from_json(c) for c in items)
                    evidence.sources.append(url)
                await self._core_patches(evidence, new[0], new[1], range(1, new[2] + 1))
        except FetchError as err:
            evidence.available = False
            evidence.problems.append(str(err)[:200])
        return evidence

    async def _core_patches(self, evidence: Evidence, year: int, month: int, patches: range) -> None:
        if len(patches) > MAX_STEPS:
            raise FetchError(f"{len(patches)} Core patches at once")
        for patch in patches:
            tag = f"{year}.{month}.{patch}"
            url = CORE_RELEASE.format(tag=tag)

            def parse(text: str, u: str = url) -> dict[str, Any]:
                body = json.loads(text).get("body")
                if not isinstance(body, str):
                    raise ValueError("no release body")
                changes, touched = parse_github_body(body, u)
                return {"changes": [c.to_json() for c in changes], "touched": sorted(touched)}

            data = await self._get(url, parse)
            evidence.changes.extend(Change.from_json(c) for c in data.get("changes") or [])
            evidence.touched.update(str(d) for d in data.get("touched") or [])
            evidence.sources.append(url)

    # ------------------------------------------------------------ add-ons, HACS, HAOS

    @staticmethod
    def notes(notes: str | None, *, title: str, domain: str | None, source: str) -> Evidence:
        """From the release notes Core serves for the update entity (no fetch here).

        Core's own banners are not notes: for HAOS it serves only "A reboot is required after install",
        so HAOS has no notes.
        """
        notes = _HA_ALERT_RE.sub("", notes or "")
        if not notes.strip():
            return Evidence(available=False, problems=["no release notes"])
        return Evidence(available=True, changes=parse_changelog(notes, title=title, domain=domain, source=source),
                        sources=[source])  # fmt: skip

    # ------------------------------------------------------------ repositories

    def _backing_off(self) -> bool:
        try:
            until = self._cache._store.kv_get(BACKOFF_KEY)
            return bool(until) and self._clock() < datetime.fromisoformat(until)
        except StoreError, ValueError, OSError:
            return False

    def _note_failure(self, err: Exception) -> None:
        if any(code in str(err) for code in ("403", "429")):
            until = self._clock() + BACKOFF
            try:
                self._cache._store.kv_set(BACKOFF_KEY, until.isoformat(), now=self._clock())
            except (StoreError, OSError) as store_err:
                _LOGGER.warning("backoff not recorded", extra={"error": str(store_err)[:120]})

    async def repository(self, repo: str) -> tuple[str, bool] | None:
        """(current full name, issues enabled) of a GitHub repository, following renames (search does not:
        home-assistant/addons is home-assistant/apps now); None when it does not exist. FetchError when GitHub
        cannot be asked now."""
        if not _SAFE_REPO_RE.fullmatch(repo) or ".." in repo:
            return None
        if self._backing_off():
            raise FetchError("GitHub asked to slow down; trying again later")

        def parse(text: str) -> dict[str, Any]:
            raw = json.loads(text)
            name = raw.get("full_name")
            if not isinstance(name, str) or not _SAFE_REPO_RE.fullmatch(name):
                raise ValueError("no repository name")
            return {"full_name": name, "has_issues": raw.get("has_issues") is True}

        try:
            data = await self._get(REPO_API.format(repo=repo), parse)
        except FetchError as err:
            if err.status == 404:
                return None
            self._note_failure(err)
            raise
        return str(data["full_name"]), bool(data["has_issues"])


def addon_repo(url: str | None) -> str | None:
    match = _REPO_RE.match(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


def addon_candidates(name: str, *urls: str | None) -> list[tuple[str, str | None]]:
    """Where an add-on's issues live, best first: its own repository (a GitHub repository root, from the
    store's `url`), then the folder's repository searched for the add-on's name. Several URLs may share a field."""
    roots: list[tuple[str, str | None]] = []
    folders: list[tuple[str, str | None]] = []
    for url in (u for given in urls for u in (given or "").split()):
        repo = addon_repo(url)
        if repo is None:
            continue
        rest = url.split(repo, 1)[1].strip("/")
        target = folders if rest.startswith("tree/") else roots
        entry = (repo, name if target is folders else None)
        if entry not in roots + folders:
            target.append(entry)
    return roots + folders


def _release_links(text: str) -> str:
    """Only the release-post links of the category page are kept (the cache stays small); none is unrecognised."""
    links = re.findall(r'href="(/blog/\d{4}/\d{2}/\d{2}/release-\d{5,6}/)"', text)
    if not links:
        raise ValueError("release-notes index without release posts")
    return "\n".join(f'href="{m}"' for m in links)


def _minors_between(old: tuple[int, ...], new: tuple[int, ...]) -> list[tuple[int, int]]:
    """Core minors after `old`'s, up to and including `new`'s (year, month)."""
    found: list[tuple[int, int]] = []
    year, month = old[0], old[1]
    while (year, month) < (new[0], new[1]) and len(found) <= MAX_STEPS:
        month += 1
        if month > 12:
            year, month = year + 1, 1
        found.append((year, month))
    return found
