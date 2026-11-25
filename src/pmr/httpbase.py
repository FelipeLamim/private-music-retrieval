"""Small shared HTTP helpers (standard library only) for the PIR server, the conventional
server and the trusted client. Every service binds to a loopback address and rejects requests
whose Host header is not a loopback name (DNS-rebinding protection).
"""
from __future__ import annotations

import ipaddress
import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _finite(obj: Any) -> Any:
    """Replace non-finite floats by None, recursively (for strict JSON output)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def require_loopback(host: str) -> str:
    """Refuse to bind anything that is not a loopback address."""
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host))
    except (OSError, ValueError):
        raise ValueError(f"cannot resolve bind host {host!r}") from None
    if not addr.is_loopback:
        raise ValueError(f"refusing to bind non-loopback address {host!r} ({addr}); this project is loopback-only")
    return str(addr)


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class LoopbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, host: str, port: int, handler_cls):
        super().__init__((require_loopback(host), port), handler_cls)


class JsonHandler(BaseHTTPRequestHandler):
    """Base handler: HTTP/1.1 keep-alive, Host check, JSON helpers, byte accounting."""

    protocol_version = "HTTP/1.1"
    server_version = "pmr"
    sys_version = ""
    max_body = 64 * 1024 * 1024
    quiet = True
    # Headers and body are written separately; without TCP_NODELAY the last segment can wait
    # for a delayed ACK (~40 ms on Linux), which distorts latency measurements.
    disable_nagle_algorithm = True
    rbufsize = 256 * 1024

    # ---- helpers
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - silence default logging
        if not self.quiet:
            super().log_message(fmt, *args)

    def check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        if name not in LOOPBACK_HOSTS:
            raise HttpError(421, "Host header must name a loopback address")

    def parsed(self) -> tuple[str, dict[str, list[str]]]:
        parts = urlsplit(self.path)
        return parts.path, parse_qs(parts.query, keep_blank_values=False, strict_parsing=False)

    def read_body(self, limit: int | None = None) -> bytes:
        limit = self.max_body if limit is None else limit
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(411, "chunked request bodies are not supported; send Content-Length")
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise HttpError(400, "invalid Content-Length") from None
        if n < 0:
            raise HttpError(400, "invalid Content-Length")
        if n > limit:
            raise HttpError(413, f"request body too large (limit {limit} bytes)")
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise HttpError(400, "truncated request body")
        return data

    def send_bytes(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, status: int, obj: Any, headers: dict[str, str] | None = None) -> None:
        # Strict JSON: NaN/Infinity are not valid JSON and break JSON.parse in browsers.
        body = json.dumps(_finite(obj), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_bytes(status, body, "application/json; charset=utf-8", headers)

    def send_error_json(self, status: int, message: str) -> None:
        try:
            self.send_json(status, {"error": message}, {"Connection": "close"} if status >= 500 else None)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if status >= 500:
            self.close_connection = True

    # ---- dispatch
    def handle_request(self, method: str) -> None:  # overridden
        raise HttpError(404, "not found")

    def _dispatch(self, method: str) -> None:
        try:
            self.check_host()
            self.handle_request(method)
        except HttpError as exc:
            self.send_error_json(exc.status, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # do not leak internals
            self.send_error_json(500, f"internal error ({type(exc).__name__})")

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


def serve_in_thread(server: LoopbackHTTPServer) -> threading.Thread:
    th = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    th.start()
    return th


def wait_for_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"service on {host}:{port} did not start within {timeout} s")
