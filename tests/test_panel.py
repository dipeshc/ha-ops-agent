"""The ingress panel over real sockets: admin check everywhere, CSRF, limits, SSE, long-poll, presence, XSS."""

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from opsd import panel as panel_mod
from opsd.baseline import Item
from opsd.daemon import Daemon
from opsd.live import MAX_LIVE
from opsd.panel import MAX_BODY, PanelServer
from tests.conftest import FakeHA
from tests.test_assistant import ADMIN
from tests.test_daemon import Harness

ADMIN_HEADERS = {"X-Remote-User-Id": "u-admin", "X-Ingress-Path": "/api/hassio_ingress/abcDEF123"}


@pytest.fixture
def h(paths: Any, fake_ha: FakeHA, monkeypatch: pytest.MonkeyPatch, meminfo: Path) -> Iterator[Harness]:
    harness = Harness(paths, fake_ha, monkeypatch, meminfo)
    yield harness
    for daemon in harness.daemons:
        daemon.close()


@pytest.fixture
def local_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests connect from 127.0.0.1; pretend that is the Supervisor's ingress proxy."""
    monkeypatch.setattr(panel_mod, "INGRESS_PEER", "127.0.0.1")


class Panel:
    def __init__(self, daemon: Daemon, server: PanelServer) -> None:
        self.d = daemon
        self.server = server
        self.port = server.bound_port

    @property
    def csrf(self) -> str:
        return self.server.csrf_token

    async def raw(self, data: bytes, *, wait_s: float = 5.0) -> tuple[int, dict[str, str], bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(data)
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), wait_s)
        except ConnectionError:
            # Closed by the server before it read the request (an accept-time refusal).
            response = b""
        finally:
            writer.close()
        return parse(response)

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        headers: dict[str, str] | None = None,
        admin: bool = True,
        csrf: bool = True,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        sent = dict(ADMIN_HEADERS if admin else {})
        if method == "POST":
            sent["Content-Type"] = "application/json"
            if csrf:
                sent["X-Ops-CSRF"] = self.csrf
        sent.update(headers or {})
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
        head = f"{method} {path} HTTP/1.1\r\nHost: addon\r\n" + "".join(f"{k}: {v}\r\n" for k, v in sent.items())
        status, resp_headers, data = await self.raw(
            (head + f"Content-Length: {len(payload)}\r\n\r\n").encode() + payload
        )
        if resp_headers.get("content-type", "").startswith("application/json") and data:
            return status, resp_headers, json.loads(data)
        return status, resp_headers, data.decode(errors="replace")


def parse(response: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = response.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1]) if lines and lines[0] else 0
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


@pytest.fixture
async def p(h: Harness, local_ingress: None) -> AsyncIterator[Panel]:
    d = h.daemon()
    d.inbound.admin = ADMIN
    server = d.panel_server(port=0)
    await server.start()
    yield Panel(d, server)
    await server.stop()


async def new_thread(p: Panel) -> str:
    """The one chat."""
    status, _, body = await p.request("GET", "/api/chat")
    assert status == 200
    return str(body["thread"])


async def send(p: Panel, thread: str, text: str, client_id: str = "client-0001") -> tuple[int, Any]:
    status, _, body = await p.request("POST", f"/api/threads/{thread}/messages",
                                      body={"text": text, "client_id": client_id})  # fmt: skip
    return status, body


# ------------------------------------------------------------- admin check


async def test_healthz_is_the_only_open_route_and_says_nothing(p: Panel) -> None:
    status, headers, body = await p.request("GET", "/healthz", admin=False)
    assert status == 200 and body == {"ok": True}
    assert headers["cache-control"] == "no-store" and headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/t/0123456789abcdef",
        "/api/state",
        "/api/threads",
        "/api/home",
        "/api/ops",
        "/api/poll?after=0",
        "/api/diagnostics",
    ],
)
@pytest.mark.parametrize("who", [None, "u-household", "", "U-ADMIN", "u-admin2"])
async def test_every_get_needs_the_admin(p: Panel, path: str, who: str | None) -> None:
    headers = {"X-Remote-User-Id": who} if who is not None else {}
    status, _, body = await p.request("GET", path, admin=False, headers=headers)
    assert status == 403
    if path.startswith("/api/"):
        assert body["error"] == "admin_only"
    else:
        assert "Only the admin" in body and "csrf" not in body.lower()


async def test_sse_needs_the_admin_and_closes_when_the_admin_changes(p: Panel) -> None:
    status, _, body = await p.request("GET", "/api/events", admin=False, headers={"X-Remote-User-Id": "u-x"})
    assert status == 403 and body["error"] == "admin_only"
    p.d.inbound.admin = None
    assert (await p.request("GET", "/api/events"))[0] == 403
    assert (await p.request("GET", "/api/state"))[0] == 403


async def test_requests_not_from_the_ingress_proxy_are_refused(h: Harness) -> None:
    d = h.daemon()
    d.inbound.admin = ADMIN
    server = d.panel_server(port=0)
    await server.start()
    try:
        panel = Panel(d, server)
        # Closed at accept time, unread and unanswered, even /healthz.
        for path in ("/", "/api/state", "/api/events", "/healthz"):
            status, _, body = await panel.request("GET", path)
            assert status == 0 and body == "", path
    finally:
        await server.stop()


# -------------------------------------------------------------------- CSRF


async def test_writes_need_csrf_json_and_same_origin(p: Panel) -> None:
    thread = await new_thread(p)
    path = f"/api/threads/{thread}/messages"
    body = {"text": "hi", "client_id": "client-0001"}
    assert (await p.request("POST", path, body=body, csrf=False))[2]["error"] == "csrf"
    assert (await p.request("POST", path, body=body, csrf=False, headers={"X-Ops-CSRF": "nope"}))[0] == 403
    # A cross-site HTML form can only send these content types.
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
        status, _, err = await p.request("POST", path, body=body, headers={"Content-Type": ctype})
        assert status == 415 and err["error"] == "json_required"
    for site in ("cross-site", "same-site"):
        assert (await p.request("POST", path, body=body, headers={"Sec-Fetch-Site": site}))[0] == 403
        assert (await p.request("GET", "/api/state", headers={"Sec-Fetch-Site": site}))[0] == 403
    assert (await p.request("POST", path, body=["not", "an", "object"]))[0] == 400
    assert (await p.request("POST", path, raw_body=b"{bad json"))[0] == 400
    assert p.d.store is not None and p.d.store.turns_page(thread) == []
    status, _, ok = await p.request("POST", path, body=body, headers={"Sec-Fetch-Site": "same-origin"})
    assert status == 202 and ok["state"] == "queued"


async def test_the_token_is_only_in_the_shell_and_state(p: Panel) -> None:
    status, headers, page = await p.request("GET", "/")
    assert status == 200 and f'content="{p.csrf}"' in page
    status, _, state = await p.request("GET", "/api/state")
    assert state["csrf"] == p.csrf
    assert "access-control-allow-origin" not in headers


async def test_methods_other_than_get_and_post_are_refused(p: Panel) -> None:
    for method in ("PUT", "DELETE", "OPTIONS", "HEAD"):
        assert (await p.request(method, "/api/state"))[0] == 405


# ------------------------------------------------------------------ limits


async def test_oversized_bodies_get_413_before_they_are_read(p: Panel) -> None:
    thread = await new_thread(p)
    head = (
        f"POST /api/threads/{thread}/messages HTTP/1.1\r\nX-Remote-User-Id: u-admin\r\n"
        f"Content-Type: application/json\r\nX-Ops-CSRF: {p.csrf}\r\nContent-Length: {MAX_BODY + 1}\r\n\r\n"
    )
    # Only the headers are sent: the server must answer without waiting for the body.
    status, _, body = await p.raw(head.encode(), wait_s=3)
    assert status == 413 and json.loads(body)["error"] == "body_too_large"
    chunked = head.replace(f"Content-Length: {MAX_BODY + 1}", "Transfer-Encoding: chunked")
    assert (await p.raw(chunked.encode() + b"0\r\n\r\n"))[0] == 501
    assert (await p.raw(b"GET / HTTP/1.1\r\n" + b"X-A: b\r\n" * 100 + b"\r\n"))[0] == 431
    assert (await p.raw(b"GET / HTTP/1.1\r\nContent-Length: -5\r\n\r\n"))[0] == 400
    assert (await p.raw(b"garbage\r\n\r\n"))[0] == 400
    assert (await p.raw("GET / HTTP/1.1\r\nContent-Length: ²\r\n\r\n".encode()))[0] == 400
    assert (await p.request("GET", "/api/poll?after=%C2%B2"))[0] == 400
    huge = b'{"text": "x", "client_id": "client-0001", "n": ' + b"9" * 5000 + b"}"
    assert (await p.request("POST", f"/api/threads/{thread}/messages", raw_body=huge))[0] == 400
    smuggle = b"GET /api/state HTTP/1.1\r\nX-Remote-User-Id: u-other\r\nX-Remote-User-Id: u-admin\r\n\r\n"
    assert (await p.raw(smuggle))[0] == 400


async def test_a_2000_character_non_ascii_message_fits(p: Panel) -> None:
    """Regression: the old 4096-byte form cap dropped long non-ASCII messages without an answer."""
    thread = await new_thread(p)
    text = "é€😀" * 666 + "ab"
    assert len(text) == 2000 and len(json.dumps({"text": text}).encode()) > 4096
    status, body = await send(p, thread, text)
    assert status == 202
    assert p.d.store is not None and p.d.store.turns_page(thread)[0].text == text
    status, body = await send(p, thread, "x" * 2001, client_id="client-0002")
    assert status == 413 and body["error"] == "too_long"
    assert (await send(p, thread, "   ", client_id="client-0003"))[0] == 400


async def test_client_id_makes_sending_idempotent_and_one_message_in_flight(p: Panel) -> None:
    thread = await new_thread(p)
    first = await send(p, thread, "hello", "client-aaaa")
    again = await send(p, thread, "hello", "client-aaaa")
    assert first[0] == 202 and again[0] == 200 and again[1]["duplicate"]
    assert again[1]["turn"]["id"] == first[1]["turn"]["id"]
    busy = await send(p, thread, "second", "client-bbbb")
    assert busy[0] == 409 and busy[1]["error"] == "in_flight"
    assert (await send(p, thread, "x", "bad id!"))[0] == 400
    assert (await send(p, "f" * 16, "x"))[0] == 404


# ------------------------------------------------------------------- shell


async def test_shell_base_path_is_validated_and_the_csp_is_strict(p: Panel) -> None:
    status, headers, page = await p.request("GET", "/t/0123456789abcdef")
    # Relative to the page's own URL: the client's X-Ingress-Path is never used.
    assert status == 200 and '<base href="../">' in page
    assert '<base href="./">' in (await p.request("GET", "/"))[2]
    assert '<base href="../../">' in (await p.request("GET", "/t/0123456789abcdef/"))[2]
    nonce = re.search(r"script-src 'nonce-([^']+)'", headers["content-security-policy"])
    assert nonce is not None and f'nonce="{nonce.group(1)}"' in page
    csp = headers["content-security-policy"]
    assert "default-src 'none'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'self'" in csp
    evil = {"X-Ingress-Path": '/x"><script>alert(1)</script>'}
    status, _, page = await p.request("GET", "/", headers=evil)
    assert '<base href="./">' in page and "<script>alert" not in page
    assert (await p.request("GET", "/t/../../etc/passwd"))[0] == 404
    assert (await p.request("GET", "/api/../state"))[0] == 404


# ---------------------------------------------------------------------- XSS


async def test_model_output_and_titles_only_ever_leave_as_json(p: Panel, h: Harness) -> None:
    thread = await new_thread(p)
    evil = '<img src=x onerror=alert(1)><script>alert(2)</script>\n\nevent: state\ndata: {"pwn":1}'
    h.claude_reply = {"reply": evil, "summary": ""}
    assert (await send(p, thread, "<script>alert(3)</script> title"))[0] == 202
    assert p.d.assistant is not None
    await p.d.assistant.drain()
    status, headers, body = await p.request("GET", f"/api/threads/{thread}")
    assert status == 200 and headers["content-type"] == "application/json"
    assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert body["turns"][-1]["text"] == evil and body["thread"]["id"] == thread
    _, _, page = await p.request("GET", f"/t/{thread}")
    assert "<img src=x" not in page and "alert(3)" not in page and "pwn" not in page
    # The SSE frame keeps the text on one data line: no injected events.
    event = p.d.hub.since(0, thread)
    assert event
    from opsd.panel import sse_frame

    frames = [sse_frame(e).decode() for e in event]
    assert all(f.count("\nevent: ") == 1 and f.count("\ndata: ") == 1 for f in frames)


# ---------------------------------------------------------------- SSE, poll


async def read_events(reader: asyncio.StreamReader, until: str, wait_s: float = 5.0) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    async with asyncio.timeout(wait_s):
        while True:
            block = await reader.readuntil(b"\n\n")
            kind, data = "", ""
            for line in block.decode().splitlines():
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    data = line[6:]
            if kind:
                events.append((kind, json.loads(data)))
            if kind == until:
                return events


async def open_stream(p: Panel, query: str = "", headers: dict[str, str] | None = None) -> asyncio.StreamReader:
    reader, writer = await asyncio.open_connection("127.0.0.1", p.port)
    extra = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
    writer.write(f"GET /api/events{query} HTTP/1.1\r\nX-Remote-User-Id: u-admin\r\n{extra}\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert b"200 OK" in head and b"text/event-stream" in head, head
    p.writers.append(writer)  # type: ignore[attr-defined]
    return reader


@pytest.fixture
def streams(p: Panel) -> Iterator[Panel]:
    p.writers = []  # type: ignore[attr-defined]
    yield p
    for writer in p.writers:  # type: ignore[attr-defined]
        writer.close()


async def test_sse_streams_a_turn_from_queued_to_reply(streams: Panel, h: Harness) -> None:
    p = streams
    thread = await new_thread(p)
    reader = await open_stream(p, f"?thread={thread}")
    assert (await read_events(reader, "hello"))[-1][1]["seq"] >= 0
    h.claude_reply = {"reply": "pong", "summary": ""}
    assert (await send(p, thread, "ping"))[0] == 202
    queued = await read_events(reader, "status")
    assert any(k == "turn" and d["data"]["role"] == "admin" for k, d in queued)
    assert p.d.assistant is not None
    await p.d.assistant.drain()
    events = []
    while not any(k == "turn" and d["data"]["role"] == "agent" for k, d in events):
        events += await read_events(reader, "turn")
    reply = next(d for k, d in events if k == "turn" and d["data"]["role"] == "agent")
    assert reply["data"]["text"] == "pong" and reply["thread"] == thread


async def test_live_connections_are_capped_and_freed(streams: Panel) -> None:
    p = streams
    for _ in range(MAX_LIVE):
        await open_stream(p)
    status, _, body = await p.request("GET", "/api/events")
    assert status == 429 and body["error"] == "too_many_live_connections"
    p.d.hub.publish("state", None, {})
    seq = p.d.hub.last_seq
    status, _, body = await p.request("GET", f"/api/poll?after={seq - 1}")
    assert status == 200  # something to return: answered at once, no slot needed
    status, _, body = await p.request("GET", f"/api/poll?after={seq}")
    assert status == 429
    for writer in p.writers:  # type: ignore[attr-defined]
        writer.close()
    for _ in range(50):
        if p.d.hub.live == 0:
            break
        await asyncio.sleep(0.05)
    assert p.d.hub.live == 0
    await open_stream(p)


async def test_sse_resume_and_gap(streams: Panel) -> None:
    p = streams
    for i in range(3):
        p.d.hub.publish("state", None, {"i": i})
    reader = await open_stream(p, headers={"Last-Event-ID": "1"})
    replayed = []
    while len(replayed) < 2:
        replayed += await read_events(reader, "state")
    assert [d["data"]["i"] for _, d in replayed] == [1, 2]
    reader = await open_stream(p, headers={"Last-Event-ID": "99999"})
    assert (await read_events(reader, "reset"))[-1][0] == "reset"
    assert (await p.request("GET", "/api/events?thread=zzz"))[0] == 404
    assert (await p.request("GET", "/api/events", headers={"Last-Event-ID": "-1"}))[0] == 400


async def test_long_poll_returns_the_same_events(p: Panel) -> None:
    seq = p.d.hub.last_seq
    task = asyncio.create_task(p.request("GET", f"/api/poll?after={seq}"))
    await asyncio.sleep(0.2)
    p.d.hub.publish("tasks", None, {})
    status, _, body = await asyncio.wait_for(task, 5)
    assert status == 200 and [e["kind"] for e in body["events"]] == ["tasks"] and body["seq"] == seq + 1
    status, _, body = await p.request("GET", "/api/poll?after=99999")
    assert body["reset"] is True


# ---------------------------------------------------------------- presence


async def test_presence_is_admin_only_rate_limited_and_bounded(p: Panel) -> None:
    thread = await new_thread(p)
    hub = p.d.hub
    status, _, _ = await p.request("POST", "/api/presence", body={"thread": thread, "visible": True}, admin=False,
                                   headers={"X-Remote-User-Id": "u-household"})  # fmt: skip
    assert status == 403 and not hub.is_watching(thread)
    status, _, _ = await p.request("POST", "/api/presence", body={"thread": thread, "visible": True}, csrf=False)
    assert status == 403 and not hub.is_watching(thread)
    status, _, body = await p.request("POST", "/api/presence", body={"thread": thread, "visible": True})
    assert status == 200 and body["watching"] and hub.is_watching(thread)
    # The client cannot ask for a longer lease or name a thread that does not exist.
    status, _, _ = await p.request("POST", "/api/presence", body={"thread": thread, "visible": True, "ttl": 9e9})
    assert status in (200, 429)
    assert (await p.request("POST", "/api/presence", body={"thread": "f" * 16, "visible": True}))[0] == 404
    assert (await p.request("POST", "/api/presence", body={"thread": thread, "visible": "yes"}))[0] == 400
    codes = [(await p.request("POST", "/api/presence", body={"thread": thread, "visible": True}))[0] for _ in range(8)]
    assert 429 in codes


# ---------------------------------------------------------- the rest of the API


async def test_state_ai_switch_and_ops(p: Panel, h: Harness) -> None:
    status, _, state = await p.request("GET", "/api/state")
    assert status == 200 and state["status"] in ("ready", "off") and state["chat"]["cap"] == 30
    assert state["tz"] == str(p.d.tz)
    assert (await p.request("POST", "/api/ai", body={"on": "yes"}))[0] == 400
    status, _, body = await p.request("POST", "/api/ai", body={"on": False})
    assert status == 200 and body["state"]["status"] == "off"
    status, _, ops = await p.request("GET", "/api/ops")
    assert status == 200 and {"items", "history", "status"} <= set(ops)


async def test_ops_items_are_redacted_for_the_panel(p: Panel) -> None:
    """Open items carry raw log samples, which the Ops tab shows: secrets are masked before they leave the daemon."""
    p.d.paths.secrets_yaml.write_text("cam_url: rtsp://user:hunter2@192.168.1.9/live\n")
    sample = "Stream failed for rtsp://user:hunter2@192.168.1.9/live"
    p.d._save_open_items([Item("log:x", "log", "warning", "new ERROR from go2rtc", {"sample": sample, "count": 3})],
                         datetime.now(UTC))  # fmt: skip
    status, _, ops = await p.request("GET", "/api/ops")
    assert status == 200 and "hunter2" not in json.dumps(ops)
    assert ops["items"][0]["detail"]["count"] == 3 and ops["items"][0]["summary"] == "new ERROR from go2rtc"


async def test_one_chat_unread_retry_and_paging(p: Panel, h: Harness) -> None:
    thread = await new_thread(p)
    assert (await p.request("GET", "/api/chat"))[2]["thread"] == thread  # the same chat every time
    p.d.control.set_switch("off", by="test", admin=True)
    status, sent = await send(p, thread, "lock battery?")
    assert p.d.assistant is not None and p.d.store is not None
    await p.d.assistant.drain()
    turn_id = sent["turn"]["id"]
    status, _, body = await p.request("GET", f"/api/threads/{thread}")
    assert [t["role"] for t in body["turns"]] == ["admin", "system"] and body["in_flight"] is None
    assert body["turns"][1]["meta"]["actions"] == ["turn_on_retry"]
    info = (await p.request("GET", "/api/chat"))[2]["info"]
    assert info["id"] == thread and info["unread"]
    assert (await p.request("POST", f"/api/threads/{thread}/read", body={}))[0] == 200
    assert not (await p.request("GET", "/api/chat"))[2]["info"]["unread"]
    # ai_disabled is not retried automatically; the toggle refuses, Retry after turning AI on works.
    auto = await p.request("POST", f"/api/threads/{thread}/autoretry", body={"turn_id": turn_id, "on": True})
    assert auto[0] == 409
    assert (await p.request("POST", f"/api/threads/{thread}/autoretry", body={"turn_id": "x", "on": 1}))[0] == 400
    assert (await p.request("POST", "/api/ai", body={"on": True}))[0] == 200
    h.claude_reply = {"reply": "about 40%", "summary": ""}
    status, _, body = await p.request("POST", f"/api/threads/{thread}/retry", body={"turn_id": turn_id})
    assert status == 202
    assert (await p.request("POST", f"/api/threads/{thread}/retry", body={"turn_id": turn_id}))[0] == 409
    assert (await p.request("POST", f"/api/threads/{thread}/retry", body={"turn_id": 10**9}))[0] == 404
    await p.d.assistant.drain()
    status, _, body = await p.request("GET", f"/api/threads/{thread}")
    assert body["turns"][-1]["text"] == "about 40%" and body["turns"][1]["meta"]["resolved"]
    for i in range(35):
        p.d.store.append_turn(thread, role="agent", text=f"n{i}", now=datetime.now(UTC))
    page = (await p.request("GET", f"/api/threads/{thread}"))[2]
    assert len(page["turns"]) == 30 and page["more"]
    older = (await p.request("GET", f"/api/threads/{thread}?before={page['turns'][0]['id']}"))[2]
    assert older["turns"][-1]["id"] < page["turns"][0]["id"] and not older["more"]
    assert (await p.request("GET", f"/api/threads/{thread}?before=abc"))[0] == 400
    assert (await p.request("GET", "/api/threads/zzzz"))[0] == 404
    status, _, body = await p.request("POST", "/api/run", body={})
    assert status == 200 and isinstance(body["started"], bool)


async def test_slow_requests_hit_the_overall_deadline(p: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_mod, "READ_DEADLINE_S", 0.5)
    reader, writer = await asyncio.open_connection("127.0.0.1", p.port)
    try:
        # Each header line arrives within the per-line timeout, but the request as a whole never finishes.
        writer.write(b"GET /api/state HTTP/1.1\r\n")
        for _ in range(5):
            await asyncio.sleep(0.2)
            with contextlib.suppress(ConnectionError):
                writer.write(b"X-Slow: 1\r\n")
                await writer.drain()
        data = b""
        with contextlib.suppress(ConnectionError):
            data = await asyncio.wait_for(reader.read(), 3)
        assert data == b""
    finally:
        writer.close()


async def test_health_keeps_a_slot_under_a_connection_flood(p: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(panel_mod, "MAX_CONNECTIONS", 4)
    idle = []
    for _ in range(2):
        idle.append(await asyncio.open_connection("127.0.0.1", p.port))
    await asyncio.sleep(0.1)
    # Two of four slots taken: the remaining two only answer /healthz.
    assert (await p.request("GET", "/api/state"))[0] == 503
    assert (await p.request("GET", "/healthz", admin=False))[0] == 200
    for _, writer in idle:
        writer.close()


async def test_sse_rechecks_the_admin_even_while_events_flow(streams: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
    p = streams
    monkeypatch.setattr(panel_mod, "ADMIN_RECHECK_S", 0.2)
    reader = await open_stream(p)
    await read_events(reader, "hello")
    p.d.inbound.admin = None

    async def chatter() -> None:
        for _ in range(40):
            p.d.hub.publish("state", None, {})
            await asyncio.sleep(0.05)

    task = asyncio.create_task(chatter())
    try:
        async with asyncio.timeout(3):
            while await reader.read(4096):
                pass
    finally:
        task.cancel()
    assert p.d.hub.live == 0


async def test_turning_ai_on_is_one_admin_action_in_the_panel(p: Panel, h: Harness) -> None:
    """No phone step. The panel request (ingress admin, CSRF) is the one factor."""
    p.d.control.set_switch("off", by="test", admin=True)
    before = len(h.pushes())
    status, _, body = await p.request("POST", "/api/ai", body={"on": True}, admin=False,
                                      headers={"X-Remote-User-Id": "u-household"})  # fmt: skip
    assert status == 403 and p.d.control.switch == "off"
    status, _, body = await p.request("POST", "/api/ai", body={"on": True})
    assert status == 200 and body["ok"] and body["state"]["ai"]["on"] and len(h.pushes()) == before
    assert p.d.store is not None
    rows = [r for r in p.d.store.audit_rows(limit=5) if r["capability"] == "panel.ai_on"]
    assert [(r["principal"], r["outcome"]) for r in rows] == [("panel:u-admin", "done")]
    # Turning AI off stays one step too.
    status, _, body = await p.request("POST", "/api/ai", body={"on": False})
    assert status == 200 and p.d.control.switch == "off"


def test_panel_prompts_and_pushes_say_approve_and_reject() -> None:
    """One vocabulary toward the admin: Approve / Reject (Cancel only stops something in flight)."""
    import re as _re

    from tests.conftest import OPT

    texts = [(OPT / "panel" / "index.html").read_text(), (OPT / "panel" / "changes.js").read_text()]
    texts += [f.read_text() for f in (OPT / "prompts").rglob("*.md")]
    texts += [f.read_text() for f in (OPT / "opsd").glob("*.py") if f.name != "store.py"]
    for text in texts:
        for word in ("Decline", "Dismiss", "Not now", "Request approval", "declined", "dismissed"):
            assert not _re.search(rf"\b{word}\b", text), word
