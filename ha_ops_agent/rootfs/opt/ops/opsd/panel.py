"""The admin-only ingress panel: app shell, JSON API and live updates.

A small hand-rolled HTTP/1.1 server (one request per connection) behind the
Supervisor's ingress proxy. `/healthz` is the only unauthenticated route and
returns no data. Everything else, GETs and the live stream included:

- must come from the Supervisor's ingress address, and
- must carry the ingress user header naming the admin (constant-time
  compare; refused while the admin is unknown). A live stream re-checks it
  at every ping.

State-changing requests are POSTs with `Content-Type: application/json`
(a cross-site form cannot send that without a CORS preflight, and no CORS
headers are ever sent), the per-process token in `X-Ops-CSRF` (the page
gets it in a `<meta>`; a cross-site page cannot read it), and, when the
browser sends it, `Sec-Fetch-Site: same-origin`. Bodies are capped at 16
KiB and refused with 413 before they are read; chunked bodies are refused.
Headers are capped in count and size. Live connections (SSE streams and
waiting long-polls) are capped by the hub, all connections here.

Connections from any address other than the Supervisor's (ingress proxy
and watchdog) are closed unread; the whole request must arrive within 10 s;
the last connection slots are kept for `/healthz`.

Responses never echo model text into HTML: the shell is a static file with
only a relative base path (from the route, never a header), the token and a
CSP nonce substituted, and all data goes out as JSON for the page to render as text.
Panel messages keep the `admin-chat` origin.
"""

import asyncio
import contextlib
import hmac
import html
import http
import json
import logging
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opsd.live import Hub, LiveEvent, Subscriber

_LOGGER = logging.getLogger(__name__)

# Ingress traffic always comes from the Supervisor's address on the hassio network.
INGRESS_PEER = "172.30.32.2"
# Set by the Supervisor's ingress proxy for the logged-in HA user (verify live).
INGRESS_USER_HEADER = "x-remote-user-id"
CSRF_HEADER = "x-ops-csrf"
MAX_BODY = 16 * 1024
MAX_HEADERS = 64
MAX_LINE = 8192
MAX_CONNECTIONS = 32
# The last slots are kept for `/healthz`, so a connection flood cannot get the add-on restarted.
HEALTH_RESERVE = 2
READ_TIMEOUT_S = 10.0
# The whole request (line, headers, body) must arrive within this.
READ_DEADLINE_S = 10.0
# A live stream re-checks the admin at least this often, whatever the traffic.
ADMIN_RECHECK_S = 60.0
WRITE_TIMEOUT_S = 10.0
SSE_PING_S = 15.0
SSE_MAX_S = 30 * 60.0
SSE_RETRY_MS = 3000
POLL_HOLD_S = 25.0
PRESENCE_BURST = 4
PRESENCE_PER_S = 1.0
MAX_CHAT_CHARS = 2000

THREAD_RE = re.compile(r"[0-9a-f]{16}")
# The shell is served at the panel root and at deep links: a chat thread, an item (`t/<item id>`, from its push).
_SHELL_RE = re.compile(r"/(?:t/([0-9a-f]{16}|[a-z]-[0-9a-z]{1,40})/?|updates/?)?")
_PART_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SCRIPT_RE = re.compile(r"[a-z]{1,32}\.js")
# ASCII digits only: `str.isdigit()` also accepts characters `int()` refuses.
_DIGITS_RE = re.compile(r"[0-9]{1,12}")
_SAME_ORIGIN = frozenset({"same-origin"})
_SHELL_FETCH_SITES = frozenset({"same-origin", "none"})
_JSON_CSP = "default-src 'none'; frame-ancestors 'none'"
_FALLBACK_SHELL = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8"><base href="{{BASE}}">'
    '<meta name="ops-csrf" content="{{CSRF}}"><title>Ops Agent</title></head>'
    "<body><p>The panel files are missing from this image.</p></body></html>"
)


class HttpError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: bytes
    peer: str


@dataclass(slots=True)
class Response:
    status: int
    body: bytes
    ctype: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


class PanelApi(Protocol):
    """The daemon side of the JSON API (`opsd.panelapi`)."""

    async def handle(
        self, method: str, parts: tuple[str, ...], query: Mapping[str, str], body: dict[str, Any] | None, user: str
    ) -> tuple[int, dict[str, Any]]: ...

    def thread_exists(self, thread_id: str) -> bool: ...


class RateLimiter:
    """Token bucket: `burst` at once, refilled at `per_s`."""

    def __init__(self, burst: int, per_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._burst = float(burst)
        self._per_s = per_s
        self._clock = clock
        self._tokens = float(burst)
        self._at = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._at) * self._per_s)
        self._at = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


def json_response(status: int, payload: Mapping[str, Any]) -> Response:
    return Response(status, json.dumps(payload, default=str).encode(), "application/json")


def _error(err: HttpError) -> Response:
    return json_response(err.status, {"error": err.code, "message": err.message})


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def sse_frame(event: LiveEvent) -> bytes:
    """One SSE event. `json.dumps` escapes newlines, so data stays on one line whatever the text."""
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {json.dumps(event.to_json(), default=str)}\n\n".encode()


class PanelServer:
    def __init__(
        self,
        *,
        port: int,
        api: PanelApi,
        hub: Hub,
        admin_user: Callable[[], str | None] = lambda: None,
        csrf_token: str | None = None,
        shell: str | None = None,
        scripts: Mapping[str, str] | None = None,
    ) -> None:
        self._port = port
        self._api = api
        self._hub = hub
        self._admin_user = admin_user
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self._shell = shell or _FALLBACK_SHELL
        # Static same-origin scripts the shell loads later under its own nonce (e.g. changes.js).
        self._scripts = {
            name: text.encode() for name, text in (scripts or {}).items() if text and _SCRIPT_RE.fullmatch(name)
        }
        self._presence = RateLimiter(PRESENCE_BURST, PRESENCE_PER_S)
        self._connections = 0
        self._server: asyncio.Server | None = None

    @staticmethod
    def load_shell(path: Path) -> str | None:
        try:
            return path.read_text()
        except OSError as err:
            _LOGGER.error("panel shell missing", extra={"path": str(path), "error": str(err)[:200]})
            return None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self._port)  # noqa: S104

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("panel server not started")
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        self._hub.close_all()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------ transport

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Only the Supervisor talks to this port (ingress proxy and watchdog): drop anyone else unread.
        peer = str((writer.get_extra_info("peername") or ("", 0))[0])
        if peer != INGRESS_PEER or self._connections >= MAX_CONNECTIONS:
            writer.close()
            return
        health_only = self._connections >= MAX_CONNECTIONS - HEALTH_RESERVE
        self._connections += 1
        try:
            await self._serve(reader, writer, health_only=health_only)
        except ConnectionError, TimeoutError, ValueError, asyncio.IncompleteReadError:
            # Disconnects, slow or oversized lines (StreamReader's own limit): nothing to answer.
            pass
        except Exception:
            _LOGGER.exception("panel request crashed")
        finally:
            self._connections -= 1
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *, health_only: bool) -> None:
        peer = str((writer.get_extra_info("peername") or ("", 0))[0])
        try:
            request = await asyncio.wait_for(self._read(reader, peer), READ_DEADLINE_S)
        except HttpError as err:
            await self._write(writer, _error(err))
            return
        if health_only and request.path != "/healthz":
            await self._write(writer, _error(HttpError(503, "busy", "too many connections")))
            return
        if request.method == "GET" and request.path == "/api/events":
            try:
                user = self._authorise(request, api=True)
                await self._stream(request, reader, writer, user)
            except HttpError as err:
                await self._write(writer, _error(err))
            return
        response = await self.respond(request)
        await self._write(writer, response)

    async def _read(self, reader: asyncio.StreamReader, peer: str) -> Request:
        line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)
        if len(line) > MAX_LINE:
            raise HttpError(414, "request_line_too_long")
        parts = line.decode("latin-1").strip().split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
            raise HttpError(400, "bad_request_line")
        method, target, _ = parts
        headers: dict[str, str] = {}
        count = 0
        while (raw := await asyncio.wait_for(reader.readline(), READ_TIMEOUT_S)) not in (b"\r\n", b"\n", b""):
            count += 1
            if count > MAX_HEADERS or len(raw) > MAX_LINE:
                raise HttpError(431, "headers_too_large")
            name, sep, value = raw.decode("latin-1").partition(":")
            if not sep:
                raise HttpError(400, "bad_header")
            key = name.strip().lower()
            if key in headers and key in ("content-length", INGRESS_USER_HEADER, CSRF_HEADER, "content-type"):
                raise HttpError(400, "duplicate_header")
            headers[key] = value.strip()
        if "transfer-encoding" in headers:
            raise HttpError(501, "chunked_not_supported")
        raw_length = headers.get("content-length", "0") or "0"
        if not _DIGITS_RE.fullmatch(raw_length):
            raise HttpError(400, "bad_content_length")
        length = int(raw_length)
        if length > MAX_BODY:
            raise HttpError(413, "body_too_large", f"at most {MAX_BODY} bytes")
        body = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT_S) if length else b""
        path, _, raw_query = target.partition("?")
        try:
            pairs = urllib.parse.parse_qsl(raw_query, max_num_fields=8, strict_parsing=False)
        except ValueError as err:
            raise HttpError(400, "bad_query") from err
        return Request(method, path, dict(pairs), headers, body, peer)

    async def _write(self, writer: asyncio.StreamWriter, response: Response) -> None:
        reason = http.HTTPStatus(response.status).phrase
        headers = {
            "Content-Type": response.ctype,
            "Content-Length": str(len(response.body)),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": _JSON_CSP,
            "Connection": "close",
            **response.headers,
        }
        head = f"HTTP/1.1 {response.status} {reason}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        writer.write(head.encode("latin-1") + b"\r\n" + response.body)
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)

    # -------------------------------------------------------------- policy

    def _authorise(self, request: Request, *, api: bool) -> str:
        """The admin's user id, or an HttpError. Every route but `/healthz` goes through here."""
        if request.peer != INGRESS_PEER:
            raise HttpError(403, "ingress_only")
        admin = self._admin_user()
        user = request.headers.get(INGRESS_USER_HEADER, "")
        if admin is None or not user or not hmac.compare_digest(user.encode(), admin.encode()):
            _LOGGER.warning(
                "panel request refused: not the admin",
                extra={"path": request.path[-40:], "method": request.method, "header_present": bool(user)},
            )
            raise HttpError(403, "admin_only", "Only the admin can use this panel.")
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in (_SAME_ORIGIN if api else _SHELL_FETCH_SITES):
            raise HttpError(403, "cross_site")
        return admin

    def _check_write(self, request: Request) -> dict[str, Any]:
        """CSRF header, JSON content type and a JSON object body."""
        if _media_type(request.headers.get("content-type", "")) != "application/json":
            raise HttpError(415, "json_required")
        token = request.headers.get(CSRF_HEADER, "")
        if not token or not hmac.compare_digest(token.encode(), self.csrf_token.encode()):
            raise HttpError(403, "csrf", "Missing or stale token; reload the panel.")
        if not request.body:
            return {}
        try:
            body = json.loads(request.body.decode("utf-8"))
        except (ValueError, RecursionError) as err:
            # ValueError covers bad UTF-8, bad JSON and integers past Python's digit limit.
            raise HttpError(400, "bad_json") from err
        if not isinstance(body, dict):
            raise HttpError(400, "bad_json", "the body must be a JSON object")
        return body

    # ------------------------------------------------------------- routing

    async def respond(self, request: Request) -> Response:
        """Everything except the SSE stream (which needs the socket)."""
        if request.path == "/healthz":
            return json_response(200, {"ok": True})
        if request.method not in ("GET", "POST"):
            return _error(HttpError(405, "method_not_allowed"))
        is_api = request.path.startswith("/api/")
        try:
            user = self._authorise(request, api=is_api)
            script = self._scripts.get(request.path.removeprefix("/"))
            if not is_api and script is not None and request.method == "GET":
                # Static code, no data; executes only with the page's nonce (script-src 'nonce-...').
                return Response(200, script, "text/javascript; charset=utf-8", {"Cache-Control": "no-store"})
            if not is_api:
                return self._shell_response(request)
            return await self._api_response(request, user)
        except HttpError as err:
            if not is_api and err.status == 403:
                body = f"<!doctype html><title>Ops Agent</title><p>{html.escape(err.message)}</p>".encode()
                return Response(403, body, "text/html; charset=utf-8")
            return _error(err)

    def _shell_response(self, request: Request) -> Response:
        if request.method != "GET" or _SHELL_RE.fullmatch(request.path) is None:
            raise HttpError(404, "not_found")
        # Relative to the page's own URL (whatever the ingress prefix), from our route alone: no header trusted.
        depth = request.path.count("/") - 1
        base = "../" * depth if depth > 0 else "./"
        nonce = secrets.token_urlsafe(16)
        page = (
            self._shell.replace("{{BASE}}", html.escape(base, quote=True))
            .replace("{{CSRF}}", html.escape(self.csrf_token, quote=True))
            .replace("{{NONCE}}", nonce)
        )
        csp = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self';"
            " img-src 'self' data:; font-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'self'"
        )
        return Response(200, page.encode(), "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    async def _api_response(self, request: Request, user: str) -> Response:
        parts = tuple(request.path[len("/api/") :].strip("/").split("/"))
        if len(parts) > 4 or not all(_PART_RE.fullmatch(p) for p in parts):
            raise HttpError(404, "not_found")
        body: dict[str, Any] | None = None
        if request.method == "POST":
            body = self._check_write(request)
        if parts == ("poll",) and request.method == "GET":
            return await self._poll(request)
        if parts == ("presence",) and request.method == "POST":
            return self._presence_post(body or {})
        status, payload = await self._api.handle(request.method, parts, request.query, body, user)
        if parts == ("state",) and status == 200:
            payload = {**payload, "csrf": self.csrf_token}
        return json_response(status, payload)

    # --------------------------------------------------------------- live

    def _thread_param(self, request: Request) -> str | None:
        thread = request.query.get("thread")
        if thread is None or thread == "":
            return None
        if not THREAD_RE.fullmatch(thread) or not self._api.thread_exists(thread):
            raise HttpError(404, "no_such_thread")
        return thread

    @staticmethod
    def _seq_param(value: str | None) -> int | None:
        if value is None or value == "":
            return None
        if not _DIGITS_RE.fullmatch(value):
            raise HttpError(400, "bad_seq")
        return int(value)

    def _presence_post(self, body: dict[str, Any]) -> Response:
        thread = body.get("thread")
        visible = body.get("visible")
        if not isinstance(thread, str) or not THREAD_RE.fullmatch(thread) or not isinstance(visible, bool):
            raise HttpError(400, "bad_presence")
        if not self._api.thread_exists(thread):
            raise HttpError(404, "no_such_thread")
        if not self._presence.allow():
            raise HttpError(429, "slow_down")
        self._hub.touch(thread, visible=visible)
        return json_response(200, {"ok": True, "watching": self._hub.is_watching(thread)})

    async def _poll(self, request: Request) -> Response:
        thread = self._thread_param(request)
        after = self._seq_param(request.query.get("after"))
        if after is None:
            return json_response(200, {"seq": self._hub.last_seq, "events": []})
        try:
            events = await self._hub.wait(after, thread, POLL_HOLD_S)
        except OverflowError as err:
            raise HttpError(429, "too_many_live_connections") from err
        if events is None:
            return json_response(200, {"seq": self._hub.last_seq, "reset": True, "events": []})
        last = events[-1].seq if events else after
        return json_response(200, {"seq": last, "events": [e.to_json() for e in events]})

    async def _stream(
        self, request: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, user: str
    ) -> None:
        thread = self._thread_param(request)
        resume = self._seq_param(request.headers.get("last-event-id") or request.query.get("after"))
        sub = self._hub.subscribe(thread)
        if sub is None:
            raise HttpError(429, "too_many_live_connections")
        try:
            head = (
                "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\nCache-Control: no-store\r\n"
                "X-Accel-Buffering: no\r\nX-Content-Type-Options: nosniff\r\nConnection: close\r\n\r\n"
                f"retry: {SSE_RETRY_MS}\n\n"
            )
            writer.write(head.encode())
            if resume is None:
                writer.write(f"event: hello\ndata: {json.dumps({'seq': self._hub.last_seq})}\n\n".encode())
            else:
                replay = self._hub.since(resume, thread)
                if replay is None:
                    writer.write(f"event: reset\ndata: {json.dumps({'seq': self._hub.last_seq})}\n\n".encode())
                else:
                    for event in replay:
                        writer.write(sse_frame(event))
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
            await self._pump(sub, reader, writer, user)
        finally:
            self._hub.unsubscribe(sub)

    async def _pump(
        self, sub: Subscriber, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, user: str
    ) -> None:
        deadline = time.monotonic() + SSE_MAX_S
        closed = asyncio.ensure_future(reader.read(1))
        dropped = asyncio.ensure_future(sub.dropped.wait())
        try:
            checked = time.monotonic()
            while time.monotonic() < deadline:
                if time.monotonic() - checked >= ADMIN_RECHECK_S:
                    if not self._still_admin(user):
                        return
                    checked = time.monotonic()
                getter = asyncio.ensure_future(sub.queue.get())
                done, _ = await asyncio.wait({getter, closed, dropped}, timeout=SSE_PING_S,
                                             return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
                if getter not in done:
                    getter.cancel()
                if closed in done or dropped in done:
                    return
                if getter in done:
                    writer.write(sse_frame(getter.result()))
                else:
                    if not self._still_admin(user):
                        return
                    writer.write(b"event: ping\ndata: {}\n\n")
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
        finally:
            for pending in (closed, dropped):
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, ConnectionError):
                    await pending

    def _still_admin(self, user: str) -> bool:
        admin = self._admin_user()
        return admin is not None and hmac.compare_digest(user.encode(), admin.encode())
