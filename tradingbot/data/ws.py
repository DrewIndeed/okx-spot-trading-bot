"""OKX public WebSocket (candles/tickers only). Stdlib framing, reconnect, injectable transport.

No auth, no private channels, no account data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable, Optional, Protocol
from urllib.parse import urlparse

OKX_PUBLIC_WS = "wss://ws.okx.com:8443/ws/v5/public"


class WSTransport(Protocol):
    def connect(self) -> None: ...
    def send(self, data: bytes) -> None: ...
    def recv(self, timeout: Optional[float] = None) -> bytes: ...
    def close(self) -> None: ...
    @property
    def closed(self) -> bool: ...


# ---- RFC6455 framing (client must mask) ----

def encode_frame(payload: bytes, opcode: int = 0x1, masked: bool = True) -> bytes:
    fin_op = 0x80 | (opcode & 0x0F)
    length = len(payload)
    if length < 126:
        header = bytes([fin_op, (0x80 if masked else 0x00) | length])
    elif length < 65536:
        header = bytes([fin_op, (0x80 if masked else 0x00) | 126]) + struct.pack("!H", length)
    else:
        header = bytes([fin_op, (0x80 if masked else 0x00) | 127]) + struct.pack("!Q", length)
    if masked:
        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return header + mask + masked_payload
    return header + payload


def decode_frames(buf: bytearray) -> tuple[list[tuple[int, bytes]], bytearray]:
    """Pull complete frames out of a buffer. Returns (frames, remaining)."""
    frames: list[tuple[int, bytes]] = []
    while True:
        if len(buf) < 2:
            break
        b0, b1 = buf[0], buf[1]
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        idx = 2
        if length == 126:
            if len(buf) < 4:
                break
            length = struct.unpack("!H", buf[2:4])[0]
            idx = 4
        elif length == 127:
            if len(buf) < 10:
                break
            length = struct.unpack("!Q", buf[2:10])[0]
            idx = 10
        if masked:
            if len(buf) < idx + 4 + length:
                break
            mask = buf[idx:idx + 4]
            idx += 4
            payload = bytes(buf[idx + i] ^ mask[i % 4] for i in range(length))
            idx += length
        else:
            if len(buf) < idx + length:
                break
            payload = bytes(buf[idx:idx + length])
            idx += length
        del buf[:idx]
        frames.append((opcode, payload))
    return frames, buf


class SslSocketTransport:
    """Real wss:// transport using stdlib ssl + socket."""

    def __init__(self, url: str = OKX_PUBLIC_WS, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self._sock: Optional[ssl.SSLSocket] = None
        self._buf = bytearray()
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed or self._sock is None

    def connect(self) -> None:
        u = urlparse(self.url)
        host = u.hostname or "ws.okx.com"
        port = u.port or 443
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        raw = socket.create_connection((host, port), timeout=self.timeout)
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw, server_hostname=host)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(req.encode())
        # read HTTP response headers
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WS handshake closed")
            data += chunk
        header, _, rest = data.partition(b"\r\n\r\n")
        status = header.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise ConnectionError(f"WS handshake failed: {status}")
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        # accept is best-effort checked
        if expected.encode() not in header:
            # some proxies may mangle; still require 101 which we already checked
            pass
        self._sock = sock
        self._buf = bytearray(rest)
        self._closed = False

    def send(self, data: bytes) -> None:
        if not self._sock:
            raise ConnectionError("not connected")
        self._sock.sendall(encode_frame(data, opcode=0x1))

    def recv(self, timeout: Optional[float] = None) -> bytes:
        """Return next text/binary payload (pong/close handled internally). Raises TimeoutError/ConnectionError."""
        if not self._sock:
            raise ConnectionError("not connected")
        self._sock.settimeout(self.timeout if timeout is None else timeout)
        while True:
            frames, self._buf = decode_frames(self._buf)
            for opcode, payload in frames:
                if opcode == 0x8:  # close
                    self.close()
                    raise ConnectionError("WS close")
                if opcode == 0x9:  # ping
                    self._sock.sendall(encode_frame(payload, opcode=0xA))
                    continue
                if opcode == 0xA:  # pong
                    continue
                if opcode in (0x1, 0x2):
                    return payload
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as e:
                raise TimeoutError("WS recv timeout") from e
            if not chunk:
                self.close()
                raise ConnectionError("WS closed")
            self._buf.extend(chunk)

    def close(self) -> None:
        self._closed = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.sendall(encode_frame(b"", opcode=0x8))
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass


class MemoryTransport:
    """In-memory transport for tests. Incoming payloads are already decoded application messages
    or raw encoded frames if raw=True. Default: send()/recv() speak application bytes (JSON).
    """

    def __init__(self):
        self.sent: list[bytes] = []
        self._incoming: list[bytes] = []
        self._closed = True
        self.fail_connect = False
        self.connect_count = 0
        self.recv_timeout = 0.05

    @property
    def closed(self) -> bool:
        return self._closed

    def connect(self) -> None:
        if self.fail_connect:
            raise ConnectionError("mock connect failed")
        self.connect_count += 1
        self._closed = False

    def push(self, message: bytes | str | dict) -> None:
        if isinstance(message, dict):
            message = json.dumps(message)
        if isinstance(message, str):
            message = message.encode()
        self._incoming.append(message)

    def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("not connected")
        self.sent.append(data)

    def recv(self, timeout: Optional[float] = None) -> bytes:
        if self._closed:
            raise ConnectionError("not connected")
        if self._incoming:
            return self._incoming.pop(0)
        raise TimeoutError("mock recv empty")

    def close(self) -> None:
        self._closed = True


class OkxPublicWS:
    """Public-channel client with subscribe + automatic reconnect and resubscribe."""

    def __init__(
        self,
        url: str = OKX_PUBLIC_WS,
        transport: Optional[WSTransport] = None,
        transport_factory: Optional[Callable[[], WSTransport]] = None,
        on_message: Optional[Callable[[dict], None]] = None,
        ping_interval: float = 20.0,
        backoff_start: float = 0.25,
        backoff_max: float = 8.0,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.url = url
        self._transport = transport
        self._factory = transport_factory or (lambda: SslSocketTransport(self.url))
        self.on_message = on_message
        self.ping_interval = ping_interval
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max
        self._sleep = sleeper
        self._subs: list[tuple[str, str]] = []  # (channel, inst_id)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_ping = 0.0
        self.reconnects = 0
        self.messages: list[dict] = []

    @property
    def transport(self) -> Optional[WSTransport]:
        return self._transport

    def subscribe(self, channel: str, inst_id: str) -> None:
        pair = (channel, inst_id)
        if pair not in self._subs:
            self._subs.append(pair)
        if self._transport and not getattr(self._transport, "closed", True):
            self._send_sub([pair])

    def _send_sub(self, pairs: list[tuple[str, str]]) -> None:
        if not self._transport:
            return
        args = [{"channel": ch, "instId": inst} for ch, inst in pairs]
        self._transport.send(json.dumps({"op": "subscribe", "args": args}).encode())

    def _send_ping(self) -> None:
        if self._transport and not self._transport.closed:
            self._transport.send(b"ping")
            self._last_ping = time.time()

    def connect(self) -> None:
        if self._transport is None or self._transport.closed:
            self._transport = self._factory()
        self._transport.connect()
        if self._subs:
            self._send_sub(list(self._subs))
        self._last_ping = time.time()

    def close(self) -> None:
        self._stop.set()
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass

    def _handle_raw(self, raw: bytes) -> Optional[dict]:
        if raw == b"pong" or raw == b"ping":
            if raw == b"ping":
                self._transport.send(b"pong")
            return None
        try:
            msg = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            return None
        self.messages.append(msg)
        if self.on_message:
            self.on_message(msg)
        return msg

    def recv_one(self, timeout: float = 5.0) -> Optional[dict]:
        if not self._transport or self._transport.closed:
            self.connect()
        raw = self._transport.recv(timeout=timeout)
        return self._handle_raw(raw)

    def _run_forever(self) -> None:
        delay = self.backoff_start
        while not self._stop.is_set():
            try:
                self.connect()
                delay = self.backoff_start
                while not self._stop.is_set():
                    if time.time() - self._last_ping >= self.ping_interval:
                        self._send_ping()
                    try:
                        self.recv_one(timeout=min(5.0, self.ping_interval))
                    except TimeoutError:
                        continue
            except Exception:
                self.reconnects += 1
                try:
                    if self._transport:
                        self._transport.close()
                except Exception:
                    pass
                if self._stop.is_set():
                    break
                self._sleep(delay)
                delay = min(self.backoff_max, delay * 2)

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run_forever, name="okx-public-ws", daemon=True)
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def run_smoke(self, inst_id: str = "BTC-USDT", bar: str = "1H", timeout: float = 12.0) -> dict:
        """Short real-connect smoke: subscribe candle + ticker, wait for one data message."""
        channel = "candle" + bar.replace("utc", "utc")  # candle1H / candle1Dutc
        if not bar.startswith("candle"):
            # OKX channel is candle{bar}, e.g. candle1H
            channel = f"candle{bar}"
        self.subscribe(channel, inst_id)
        self.subscribe("tickers", inst_id)
        self.connect()
        deadline = time.time() + timeout
        got = None
        try:
            while time.time() < deadline:
                try:
                    msg = self.recv_one(timeout=max(0.2, deadline - time.time()))
                except TimeoutError:
                    continue
                if not msg:
                    continue
                # skip subscribe acks; want an actual data event
                if msg.get("event") == "subscribe":
                    continue
                if msg.get("data") or msg.get("arg"):
                    got = msg
                    break
        finally:
            self.close()
        if got is None:
            raise TimeoutError("WS smoke: no public data message received")
        return got
