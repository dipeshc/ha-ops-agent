"""Minimal asyncio WebSocket client for the HA Core websocket API (ws:// only).

Commands use one short-lived connection; `EventStream` keeps one persistent
subscription (Companion action taps and the AI kill switch).

Implements just what the Core API needs: client-masked text frames,
fragmentation, ping/pong and close. The Supervisor proxy is plain HTTP on the
internal network, so TLS is out of scope.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import struct
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from typing import Any

from opsd import selfguard

_LOGGER = logging.getLogger(__name__)

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    """Protocol or connection failure."""


class CommandError(RuntimeError):
    """The Core API answered a command with `success: false`."""


def accept_key(key: str) -> str:
    digest = hashlib.sha1((key + _GUID).encode(), usedforsecurity=False).digest()
    return base64.b64encode(digest).decode()


def encode_frame(opcode: int, payload: bytes, *, mask: bytes | None = None) -> bytes:
    """A single final frame; clients must mask (RFC 6455 section 5.3)."""
    mask = mask if mask is not None else os.urandom(4)
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 1 << 16:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + mask + masked


async def read_frame(reader: asyncio.StreamReader, max_bytes: int = MAX_MESSAGE_BYTES) -> tuple[bool, int, bytes]:
    """Return (fin, opcode, payload) for the next frame."""
    first, second = await reader.readexactly(2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", await reader.readexactly(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", await reader.readexactly(8))
    if length > max_bytes:
        raise WebSocketError(f"frame too large: {length}")
    mask = await reader.readexactly(4) if second & 0x80 else None
    payload = await reader.readexactly(length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


class HAWebSocket:
    """One authenticated connection to `ws://supervisor/core/websocket`."""

    def __init__(self, url: str, token: str, *, timeout: float = 30.0, max_bytes: int = MAX_MESSAGE_BYTES) -> None:
        # A smaller cap for callers that should never need a large reply (e.g. history).
        self._max_bytes = min(max_bytes, MAX_MESSAGE_BYTES)
        self._url = urllib.parse.urlsplit(url)
        self._token = token
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1

    async def __aenter__(self) -> HAWebSocket:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        host = self._url.hostname or "supervisor"
        port = self._url.port or 80
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=MAX_MESSAGE_BYTES), self._timeout
        )
        self._reader, self._writer = reader, writer
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self._url.path or '/'} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._timeout)
        lines = head.decode(errors="replace").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WebSocketError(f"upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:]) if k}
        if headers.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("bad Sec-WebSocket-Accept")
        await self._authenticate()

    async def _authenticate(self) -> None:
        hello = await self.recv_json()
        if hello.get("type") != "auth_required":
            raise WebSocketError(f"unexpected greeting: {hello.get('type')}")
        await self.send_json({"type": "auth", "access_token": self._token})
        reply = await self.recv_json()
        if reply.get("type") != "auth_ok":
            raise WebSocketError("authentication failed")

    async def close(self) -> None:
        if self._writer is None:
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            self._writer.write(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
            await self._writer.drain()
        self._writer.close()
        with contextlib.suppress(ConnectionError, RuntimeError):
            await self._writer.wait_closed()
        self._writer = None
        self._reader = None

    async def send_json(self, message: dict[str, Any]) -> None:
        if message.get("type") != "auth":
            try:
                # Every command, whichever method sends it (opsd.selfguard).
                selfguard.check_ws(str(message.get("type", "")), {k: v for k, v in message.items() if k != "type"})
            except selfguard.SelfTargetError as err:
                raise CommandError(f"{message.get('type')}: refused: {err}") from err
        if self._writer is None:
            raise WebSocketError("not connected")
        self._writer.write(encode_frame(OP_TEXT, json.dumps(message).encode()))
        await self._writer.drain()

    async def recv_json(self, *, idle_s: float | None = None) -> dict[str, Any]:
        """The next JSON message; `idle_s` overrides the connection default (idle subscriptions)."""
        if self._reader is None or self._writer is None:
            raise WebSocketError("not connected")
        buffer = bytearray()
        while True:
            fin, opcode, payload = await asyncio.wait_for(
                read_frame(self._reader, self._max_bytes), idle_s or self._timeout
            )
            if opcode == OP_PING:
                self._writer.write(encode_frame(OP_PONG, payload))
                await self._writer.drain()
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                raise WebSocketError("closed by server")
            if opcode in (OP_TEXT, OP_BINARY, OP_CONT):
                buffer += payload
                if len(buffer) > self._max_bytes:
                    raise WebSocketError("message too large")
                if fin:
                    value = json.loads(buffer)
                    if not isinstance(value, dict):
                        raise WebSocketError("non-object message")
                    return value

    async def send_command(self, msg_type: str, **fields: Any) -> int:
        """Send a command without waiting for its result (pings on a subscription)."""
        msg_id = self._next_id
        self._next_id += 1
        await self.send_json({"id": msg_id, "type": msg_type, **fields})
        return msg_id

    async def command(self, msg_type: str, **fields: Any) -> Any:
        """Send one command and wait for its result, skipping unrelated messages."""
        # send_json runs the self-target guard (opsd.selfguard) before anything is written.
        msg_id = self._next_id
        self._next_id += 1
        await self.send_json({"id": msg_id, "type": msg_type, **fields})
        while True:
            reply = await self.recv_json()
            if reply.get("id") != msg_id or reply.get("type") != "result":
                continue
            if not reply.get("success"):
                error = reply.get("error") or {}
                raise CommandError(f"{msg_type}: {error.get('code')} {error.get('message')}")
            return reply.get("result")


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
_STREAM_ERRORS = (WebSocketError, CommandError, OSError, TimeoutError, asyncio.IncompleteReadError, ValueError)


class EventStream:
    """A persistent `subscribe_events` connection that reconnects with backoff.

    Only the listed event types are subscribed. Each `event` message's
    `event` object goes to `on_event`; anything beyond `max_events_per_min`
    is dropped, so a flood on Core's bus cannot flood the daemon. A ping
    every `ping_interval` keeps the connection observable: no message at all
    for `idle_timeout` means it is dead and is replaced.
    """

    def __init__(
        self,
        connect: Callable[[], Awaitable[HAWebSocket]],
        event_types: tuple[str, ...],
        on_event: EventCallback,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
        ping_interval: float = 50.0,
        idle_timeout: float = 130.0,
        max_events_per_min: int = 30,
        backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        subscribe: Callable[[HAWebSocket], Awaitable[None]] | None = None,
        accept: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """`subscribe`/`accept` replace the default `subscribe_events` + event-type filter (e.g. a trigger)."""
        self._subscribe = subscribe
        self._accept = accept
        self._connect = connect
        self._event_types = event_types
        self._on_event = on_event
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._ping_interval = ping_interval
        self._idle_timeout = idle_timeout
        self._max_events = max_events_per_min
        self._sleep = backoff_sleep
        self._clock = clock
        self._window = clock()
        self._in_window = 0
        self.dropped = 0
        self.connected = False

    def _admit(self) -> bool:
        now = self._clock()
        if now - self._window >= 60:
            if self.dropped:
                _LOGGER.warning("inbound events dropped by rate limit", extra={"dropped": self.dropped})
            self._window, self._in_window, self.dropped = now, 0, 0
        self._in_window += 1
        if self._in_window > self._max_events:
            self.dropped += 1
            return False
        return True

    async def _ping(self, ws: HAWebSocket) -> None:
        while True:
            await asyncio.sleep(self._ping_interval)
            await ws.send_command("ping")

    async def run_once(self) -> None:
        """One connection's lifetime; raises when it ends."""
        ws = await self._connect()
        pinger: asyncio.Task[None] | None = None
        try:
            if self._subscribe is not None:
                await self._subscribe(ws)
            else:
                for event_type in self._event_types:
                    await ws.command("subscribe_events", event_type=event_type)
            self.connected = True
            _LOGGER.info("event subscription up", extra={"event_types": list(self._event_types)})
            pinger = asyncio.create_task(self._ping(ws))
            while True:
                message = await ws.recv_json(idle_s=self._idle_timeout)
                event = message.get("event")
                if message.get("type") != "event" or not isinstance(event, dict):
                    continue
                wanted = (
                    self._accept(event) if self._accept is not None else event.get("event_type") in self._event_types
                )
                if not wanted or not self._admit():
                    continue
                await self._on_event(event)
        finally:
            self.connected = False
            if pinger is not None:
                pinger.cancel()
            await ws.close()

    async def run(self) -> None:
        backoff = self._initial_backoff
        while True:
            started = self._clock()
            try:
                await self.run_once()
            except _STREAM_ERRORS as err:
                _LOGGER.warning("event subscription lost", extra={"error": f"{type(err).__name__}: {str(err)[:200]}"})
            except Exception:
                # Inbound events must never take the daemon (and its safety net) down.
                _LOGGER.exception("event subscription crashed; reconnecting")
            if self._clock() - started > 60:
                backoff = self._initial_backoff
            await self._sleep(backoff)
            backoff = min(self._max_backoff, backoff * 2)
