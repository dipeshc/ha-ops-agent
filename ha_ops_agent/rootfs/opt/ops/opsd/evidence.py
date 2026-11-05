"""Release notes and repository facts from the cloud: fetched, parsed and cached, read-only.

`fetch_text` is the one way the agent reads the web (HTTPS, `ALLOWED_HOSTS`, `MAX_BYTES`). `EvidenceSource`
serves the update rule (Core notes, spec updates.md) and proactive maintenance (release posts, archived
repositories, Core platforms; spec maintenance.md). Fetched text is untrusted: only cleaned, capped fields
are kept and cached (`CACHE_TTL`); a failed answer is never cached and makes the evidence unavailable.
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import ssl
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
# Home Assistant Core's own source, for `opsd.coresource` only: the contents API and the raw file host.
CORE_SOURCE_HOSTS = frozenset({"api.github.com", "raw.githubusercontent.com"})
MAX_BYTES = 3 * 1024 * 1024
FETCH_TIMEOUT_S = 20.0
CACHE_PREFIX = "evidence:"
CACHE_TTL = timedelta(hours=12)
RELEASE_INDEX = "https://www.home-assistant.io/blog/categories/release-notes/"
CORE_RELEASE = "https://api.github.com/repos/home-assistant/core/releases/tags/{tag}"
REPO_API = "https://api.github.com/repos/{repo}"
CORE_COMPONENT = "https://api.github.com/repos/home-assistant/core/contents/homeassistant/components/{domain}"
# An integration domain, as Home Assistant names them (the one pattern the maintenance code uses).
DOMAIN_RE = re.compile(r"[a-z0-9_]{1,64}")
_POST_RE = re.compile(r'href="(/blog/\d{4}/\d{2}/\d{2}/release-(\d{4})(\d{1,2})/)"')
_NEW_LI_RE = re.compile(r'<a href="/integrations/([a-z0-9_]+)/?">(.*?)</a>', re.DOTALL)
BACKOFF_KEY = "evidence:github_backoff_until"
BACKOFF = timedelta(minutes=30)
# The most patch bodies or blog posts one update looks at (beyond that: not enough evidence).
MAX_STEPS = 6
TEXT_CHARS = 900
TITLE_CHARS = 80
TERMS_MAX = 12
_CODE_RE = re.compile(r"<code>(.*?)</code>", re.DOTALL)
_TICK_RE = re.compile(r"`([^`\n]{3,80})`")
_TAG_RE = re.compile(r"<[^>]+>")
_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]{2,80}")
_DOTTED_RE = re.compile(r"\b[a-z][a-z0-9_]{1,63}\.[a-z0-9_]{2,128}\b")
_INTEGRATION_LINK_RE = re.compile(r'href="/integrations/([a-z0-9_]+)(?:\.[a-z0-9_]+)?/?"')
_DOCS_RE = re.compile(r"\[([a-z0-9_]+) docs\]")
# The note's own integration: its "(<name> documentation)" or "(<name> docs)" link, not one inside its prose.
_DOCS_LINK_RE = re.compile(
    r'<a href="/integrations/([a-z0-9_]+)(?:\.[a-z0-9_]+)?/?"[^>]*>[^<]*\b(?:docs|documentation)\s*</a>', re.IGNORECASE
)
# home-assistant.io inlines a glossary tooltip after a term ("Groups are a way to organize...", with a link to the
# glossary's integration): someone else's words, never part of the note, and never the note's integration.
_GLOSSARY_RE = re.compile(r'<span class="terminology-tooltip">.*?</span>', re.DOTALL)
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

    kind: str  # breaking | removed | devblog
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


# The release-post parser's version: part of every cache key of a parsed post, so a parser fix takes effect at once
# instead of when the cached result expires. Bump it whenever what `parse_release_post` or `parse_post` returns
# changes. 2: glossary tooltips stripped and the note's own documentation link as its integration (0.23.0).
POST_PARSER = 2


def strip_glossary(page: str) -> str:
    """The page without its glossary tooltips (the term itself stays)."""
    return _GLOSSARY_RE.sub("", page)


def note_domain(body: str) -> str | None:
    """A release note's integration: its documentation link, else the last integration link it gives (the note's
    own comes last); never a glossary tooltip's (`strip_glossary` first)."""
    docs = _DOCS_LINK_RE.findall(body)
    if docs:
        return str(docs[-1])
    links = _INTEGRATION_LINK_RE.findall(body)
    return str(links[-1]) if links else None


def parse_release_post(page: str, source: str) -> list[Change]:
    """Backward-incompatible changes, developer-blog items and removed integrations of a Core release post.

    Fails closed (ValueError: missing evidence, never cached): the section's anchor must be there and
    hold at least one recognised item. Every Core minor has some; none parsed means the page changed.
    """
    page = strip_glossary(page)
    changes: list[Change] = []
    section = _section(page, "backward-incompatible-changes")
    if not section:
        raise ValueError("release post without a recognised backward-incompatible section")
    found = _DETAILS_RE.findall(section)
    if not found or 'class="details-block' not in section:
        raise ValueError("release post: no backward-incompatible items recognised")
    for title, body in found:
        changes.append(Change("breaking", plain(title, TITLE_CHARS), note_domain(body), plain(body),
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


def parse_post(page: str, source: str) -> dict[str, Any]:
    """A release post for proactive maintenance: its changes (`parse_release_post`, fail-closed) and the
    integrations it adds (cleaned, untrusted)."""
    page = strip_glossary(page)
    changes = parse_release_post(page, source)
    added: list[dict[str, str]] = []
    for domain, name in _NEW_LI_RE.findall(_section(page, "new-integrations")):
        if all(a["domain"] != domain for a in added):
            added.append({"domain": domain, "name": plain(name, TITLE_CHARS)})
    return {"changes": [c.to_json() for c in changes], "new": added[:40]}


def published_minors(index_html: str) -> list[tuple[int, int]]:
    """The Core minors whose release post the release-notes index lists, newest first."""
    found = {(int(y), int(m)) for _, y, m in _POST_RE.findall(index_html) if 1 <= int(m) <= 12}
    return sorted(found, reverse=True)


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


# ------------------------------------------------------------------ fetching


class _SameHosts(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: frozenset[str]) -> None:
        super().__init__()
        self._hosts = hosts

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:  # noqa: PLR0917
        _check_url(newurl, self._hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str, hosts: frozenset[str] = ALLOWED_HOSTS) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in hosts:
        raise FetchError(f"not an allowed source: {parts.hostname}")


def check_hosts(hosts: frozenset[str]) -> None:
    """A caller can only narrow the hosts: `ALLOWED_HOSTS`, plus Core's source hosts."""
    if not hosts or not hosts <= ALLOWED_HOSTS | CORE_SOURCE_HOSTS:
        raise FetchError("not an allowed source list")


def fetch_text(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_S,
    hosts: frozenset[str] = ALLOWED_HOSTS,
    max_bytes: int = MAX_BYTES,
    raw: bool = False,
) -> str:
    """GET over HTTPS (TLS verified) from an allowed host, at most `max_bytes`. A redirect is followed only to a
    host in `hosts` too. `hosts` can only narrow `ALLOWED_HOSTS` plus `CORE_SOURCE_HOSTS`; `raw` asks GitHub for a
    file's plain contents."""
    check_hosts(hosts)
    _check_url(url, hosts)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()), _SameHosts(hosts)
    )
    accept = "text/plain" if raw else "application/vnd.github+json" if "api.github.com" in url else "text/html"
    request = urllib.request.Request(url, headers={"User-Agent": "ha-ops-agent", "Accept": accept})
    try:
        with opener.open(request, timeout=timeout) as resp:
            data = bytes(resp.read(max_bytes + 1))
    except urllib.error.HTTPError as err:
        raise FetchError(f"{urllib.parse.urlsplit(url).hostname}: HTTP {err.code}", status=err.code) from err
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise FetchError(f"{urllib.parse.urlsplit(url).hostname}: {str(err)[:120]}") from err
    if len(data) > max_bytes:
        raise FetchError("answer larger than the limit")
    return data.decode("utf-8", errors="replace")


class GithubBackoff:
    """GitHub answered "slow down" (HTTP 403 or 429): no call under this key for `BACKOFF`."""

    def __init__(self, store: Store | None, key: str, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._key = key
        self._clock = clock

    def active(self) -> bool:
        if self._store is None:
            return False
        try:
            until = self._store.kv_get(self._key)
            return bool(until) and self._clock() < datetime.fromisoformat(until)
        except StoreError, ValueError, OSError:
            return False

    def note(self, err: FetchError) -> None:
        if err.status not in (403, 429) or self._store is None:
            return
        try:
            self._store.kv_set(self._key, (self._clock() + BACKOFF).isoformat(), now=self._clock())
        except (StoreError, OSError) as store_err:
            _LOGGER.warning("backoff not recorded", extra={"error": str(store_err)[:120]})


class EvidenceCache:
    """Parsed evidence in the store's key-value table, by source; fresh for `CACHE_TTL`."""

    def __init__(self, store: Store, *, clock: Callable[[], datetime]) -> None:
        self.store = store
        self._clock = clock

    @staticmethod
    def _key(name: str) -> str:
        return CACHE_PREFIX + hashlib.sha256(name.encode()).hexdigest()[:32]

    def get(self, name: str, *, stale_ok: bool = False) -> Any | None:
        stored = self.store.kv_json(self._key(name))
        if stored is None:
            return None
        try:
            at = datetime.fromisoformat(str(stored.get("at")))
        except ValueError:
            return None
        if not stale_ok and self._clock() - at > CACHE_TTL:
            return None
        return stored.get("data")

    def put(self, name: str, data: Any) -> None:
        now = self._clock()
        try:
            self.store.kv_set(self._key(name), json.dumps({"at": now.isoformat(), "data": data}), now=now)
        except (StoreError, OSError) as err:
            _LOGGER.warning("evidence cache write failed", extra={"error": str(err)[:200]})


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
        self._backoff = GithubBackoff(cache.store, BACKOFF_KEY, clock)

    async def _get(self, url: str, parse: Callable[[str], Any], *, fresh: bool = False, name: str = "") -> Any:
        """Parsed and cached on success only: a failed or unrecognised answer is fetched again next time. `name`
        keeps a second parse of the same page apart in the cache."""
        key = f"{url}#{name}" if name else url
        cached = None if fresh else self._cache.get(key)
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
        self._cache.put(key, data)
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

                    items = await self._get(url, parse, name=f"notes-p{POST_PARSER}")
                    evidence.changes.extend(Change.from_json(c) for c in items)
                    evidence.sources.append(url)
                await self._core_patches(evidence, new[0], new[1], range(1, new[2] + 1))
        except FetchError as err:
            evidence.available = False
            evidence.problems.append(str(err)[:200])
        return evidence

    async def index(self, *, fresh: bool = False) -> str:
        """The release-notes index (only its release-post links)."""
        found = await self._get(RELEASE_INDEX, lambda text: {"html": _release_links(text)}, fresh=fresh)
        return str(found.get("html") or "")

    async def posts(self, minors: list[tuple[int, int]], index_html: str) -> tuple[list[dict[str, Any]], list[str]]:
        """(each published post among `minors`: {version, url, changes, new}; problems). A post that
        could not be read or parsed is a problem, never a guess."""
        found: list[dict[str, Any]] = []
        problems: list[str] = []
        for year, month in minors[:MAX_STEPS]:
            url = release_post_url(index_html, (year, month))
            if url is None:
                continue

            def parse(text: str, source: str = url) -> dict[str, Any]:
                return parse_post(text, source)

            try:
                data = await self._get(url, parse, name=f"maintenance-p{POST_PARSER}")
            except FetchError as err:
                problems.append(clean_text(str(err), 160))
                continue
            found.append({"version": f"{year}.{month}", "url": url, **data})
        return found, problems

    async def archived(self, repo: str) -> bool | None:
        """Whether a GitHub repository is archived; None when it does not exist or cannot be asked now."""
        if not _SAFE_REPO_RE.fullmatch(repo) or ".." in repo or self._backoff.active():
            return None

        def parse(text: str) -> dict[str, Any]:
            raw = json.loads(text)
            if not isinstance(raw, dict) or not isinstance(raw.get("archived"), bool):
                raise ValueError("no archived flag")
            return {"archived": raw["archived"]}

        try:
            data = await self._get(REPO_API.format(repo=repo), parse, name="archived")
        except FetchError as err:
            self._backoff.note(err)
            return None
        return bool(data.get("archived"))

    async def core_platforms(self, domain: str) -> frozenset[str] | None:
        """The entity platforms a Core integration of this domain provides (its `<platform>.py` files); None when
        Core has no such integration. FetchError when GitHub cannot be asked now."""
        if not DOMAIN_RE.fullmatch(domain):
            return None
        if self._backoff.active():
            raise FetchError("GitHub asked to slow down; trying again later")

        def parse(text: str) -> dict[str, Any]:
            raw = json.loads(text)
            if not isinstance(raw, list):
                raise ValueError("not a directory listing")
            names = [str(e.get("name")) for e in raw if isinstance(e, dict) and e.get("type") == "file"]
            if "manifest.json" not in names:
                raise ValueError("not an integration")
            return {"files": sorted(n.removesuffix(".py") for n in names if n.endswith(".py"))[:200]}

        try:
            data = await self._get(CORE_COMPONENT.format(domain=domain), parse)
        except FetchError as err:
            if err.status == 404:
                return None
            self._backoff.note(err)
            raise
        return frozenset(str(f) for f in data.get("files") or [])

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


def addon_repo(url: str | None) -> str | None:
    match = _REPO_RE.match(url or "")
    return f"{match.group(1)}/{match.group(2)}" if match else None


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
