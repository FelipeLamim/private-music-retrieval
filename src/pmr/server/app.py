"""PIR database server (honest-but-curious). Run: ``python -m pmr.server --catalog DIR``.

HTTP API (all JSON unless stated; binary bodies use the ``pmr.pir.wire`` container):
  GET    /v1/catalog                         public catalog metadata
  GET    /v1/pir/params                      BFV parameters, backend config, public layout, limits
  POST   /v1/pir/sessions                    body: optional setup blob -> {"session": token}
  DELETE /v1/pir/sessions/<session>
  POST   /v1/pir/sessions/<session>/queries  body: query container -> {"query": token, ...}
  GET    /v1/pir/queries/<query>/blocks?start=J&count=K   -> answer container (K ciphertexts)
  DELETE /v1/pir/queries/<query>
  GET    /v1/server/stats                    process metrics for the experiment runner
  GET    /healthz

No endpoint accepts a track index, title, file name or key other than public material.
Session and query tokens are random 128-bit values chosen by the server.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..catalog import load_catalog, public_catalog
from ..he.bfv import CryptoError
from ..httpbase import HttpError, JsonHandler, LoopbackHTTPServer
from ..pir import wire
from ..pir.engine import BackendConfig
from .engine_pool import EnginePool, LocalEngine

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@dataclass
class Limits:
    max_sessions: int = 32
    max_queries_per_session: int = 4
    max_blocks_per_request: int = 64
    max_setup_bytes: int = 16 * 1024 * 1024
    max_query_bytes: int = 64 * 1024 * 1024
    max_ciphertext_bytes: int = 512 * 1024
    idle_timeout_s: float = 600.0
    rate_limit_bps: float = 0.0  # optional application-level link emulation (0 = off)


@dataclass
class Session:
    created: float
    last_used: float
    queries: dict[str, float] = field(default_factory=dict)


class PirService:
    def __init__(self, catalog_dir: str, config: BackendConfig, *, workers: int = 1, max_tracks: int | None = None,
                 limits: Limits | None = None):
        t0 = time.perf_counter()
        self.manifest, records = load_catalog(catalog_dir, max_tracks=max_tracks)
        self.load_s = time.perf_counter() - t0
        self.limits = limits or Limits()
        if workers <= 1:
            self.engine: Any = LocalEngine(records, config)
        else:
            lengths = [len(r) for r in records]
            del records
            self.engine = EnginePool(catalog_dir, config, workers, lengths, max_tracks=max_tracks)
        self.config = self.engine.config
        self.layout = self.engine.layout
        self.setup_s = time.perf_counter() - t0
        self.catalog = public_catalog(self.manifest)
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.counters = {"requests": 0, "bytes_in": 0, "bytes_out": 0, "blocks_answered": 0, "errors": 0}

    # -- public description
    def params_doc(self) -> dict[str, Any]:
        lim = self.limits
        return {
            "protocol": "pmr-pir/1",
            "backend": self.config.to_dict(),
            "layout": self.layout.to_dict(),
            "limits": {"max_blocks_per_request": lim.max_blocks_per_request,
                       "max_queries_per_session": lim.max_queries_per_session,
                       "max_ciphertext_bytes": lim.max_ciphertext_bytes},
            "server": {"workers": self.engine.workers, "setup_s": round(self.setup_s, 4),
                       "preprocess_s": round(self.engine.preprocess_s, 4), "catalog_load_s": round(self.load_s, 4)},
        }

    # -- sessions and queries
    def _expire(self) -> None:
        now = time.monotonic()
        stale = [s for s, v in self._sessions.items() if now - v.last_used > self.limits.idle_timeout_s]
        for s in stale:
            self._sessions.pop(s, None)
            self.engine.close_session(s)

    def open_session(self, setup: bytes) -> str:
        with self._lock:
            self._expire()
            if len(self._sessions) >= self.limits.max_sessions:
                raise HttpError(503, "too many sessions")
        setup_blob = None
        if setup:
            kind = "setup.context" if self.config.backend == "didactic" else "setup.galois"
            try:
                (setup_blob,) = wire.unpack(setup, kind, max_count=1, max_blob=self.limits.max_setup_bytes, exact_count=1)
            except wire.WireError as exc:
                raise HttpError(400, f"bad setup message: {exc}") from None
        sid = secrets.token_urlsafe(16)
        try:
            self.engine.open_session(sid, setup_blob)
        except (CryptoError, RuntimeError) as exc:
            raise HttpError(400, f"invalid session material: {exc}") from None
        now = time.monotonic()
        with self._lock:
            self._sessions[sid] = Session(now, now)
        return sid

    def _session(self, sid: str) -> Session:
        if not TOKEN_RE.match(sid):
            raise HttpError(404, "unknown session")
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                raise HttpError(404, "unknown session")
            s.last_used = time.monotonic()
            return s

    def close_session(self, sid: str) -> None:
        self._session(sid)
        with self._lock:
            self._sessions.pop(sid, None)
        self.engine.close_session(sid)

    def open_query(self, sid: str, body: bytes) -> dict[str, Any]:
        s = self._session(sid)
        if len(s.queries) >= self.limits.max_queries_per_session:
            raise HttpError(429, "too many open queries in this session; close one first")
        if self.config.query_mode == "expanded" and self.config.backend == "optimized":
            kind, count = "query.expanded", 1
        else:
            kind, count = "query.selector", self.layout.num_records
        try:
            blobs = wire.unpack(body, kind, max_count=count, max_blob=self.limits.max_ciphertext_bytes, exact_count=count)
        except wire.WireError as exc:
            raise HttpError(400, f"bad query message: {exc}") from None
        qid = secrets.token_urlsafe(16)
        try:
            prepare_ms = self.engine.open_query(sid, qid, blobs)
        except (CryptoError, RuntimeError, KeyError) as exc:
            raise HttpError(400, f"invalid query: {exc}") from None
        with self._lock:
            s.queries[qid] = time.monotonic()
            self._query_owner = getattr(self, "_query_owner", {})
            self._query_owner[qid] = sid
        return {"query": qid, "prepare_ms": round(prepare_ms, 3)}

    def _query_session(self, qid: str) -> str:
        if not TOKEN_RE.match(qid):
            raise HttpError(404, "unknown query")
        with self._lock:
            sid = getattr(self, "_query_owner", {}).get(qid)
        if sid is None:
            raise HttpError(404, "unknown query")
        self._session(sid)
        return sid

    def close_query(self, qid: str) -> None:
        sid = self._query_session(qid)
        with self._lock:
            self._sessions[sid].queries.pop(qid, None)
            self._query_owner.pop(qid, None)
        self.engine.close_query(qid)

    def answer(self, qid: str, start: int, count: int) -> tuple[bytes, float]:
        self._query_session(qid)
        if count < 1 or count > self.limits.max_blocks_per_request:
            raise HttpError(400, f"count must be in 1..{self.limits.max_blocks_per_request}")
        if start < 0 or start + count > self.layout.num_blocks:
            raise HttpError(416, f"blocks must be within 0..{self.layout.num_blocks - 1}")
        blobs, compute_ms = self.engine.answer(qid, list(range(start, start + count)))
        with self._lock:
            self.counters["blocks_answered"] += count
        return wire.pack("answer.blocks", blobs), compute_ms

    def stats(self) -> dict[str, Any]:
        with self._lock:
            sessions = len(self._sessions)
            counters = dict(self.counters)
        return {"pid": os.getpid(), "sessions": sessions, "counters": counters, "workers": self.engine.stats(),
                "main_cpu_s": time.process_time()}

    def shutdown(self) -> None:
        self.engine.shutdown()


def make_handler(service: PirService):
    class Handler(JsonHandler):
        max_body = max(service.limits.max_query_bytes, service.limits.max_setup_bytes) + 1024

        def _count(self, n_in: int, n_out: int) -> None:
            with service._lock:
                service.counters["requests"] += 1
                service.counters["bytes_in"] += n_in
                service.counters["bytes_out"] += n_out

        def _send_rate_limited(self, status: int, body: bytes, ctype: str, headers: dict[str, str]) -> None:
            bps = service.limits.rate_limit_bps
            if bps <= 0:
                self.send_bytes(status, body, ctype, headers)
                return
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            chunk = 16384
            t0 = time.monotonic()
            for off in range(0, len(body), chunk):
                self.wfile.write(body[off:off + chunk])
                due = t0 + (off + chunk) * 8 / bps
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        def handle_request(self, method: str) -> None:
            path, qs = self.parsed()
            parts = [p for p in path.split("/") if p]
            if method in ("GET", "HEAD") and path == "/healthz":
                return self.send_json(200, {"ok": True})
            if method == "GET" and path == "/v1/catalog":
                return self.send_json(200, service.catalog)
            if method == "GET" and path == "/v1/pir/params":
                return self.send_json(200, service.params_doc())
            if method == "GET" and path == "/v1/server/stats":
                return self.send_json(200, service.stats())
            if method == "POST" and parts == ["v1", "pir", "sessions"]:
                body = self.read_body(service.limits.max_setup_bytes + 64)
                sid = service.open_session(body)
                self._count(len(body), 0)
                return self.send_json(201, {"session": sid})
            if len(parts) == 4 and parts[:3] == ["v1", "pir", "sessions"] and method == "DELETE":
                service.close_session(parts[3])
                return self.send_json(200, {"closed": True})
            if len(parts) == 5 and parts[:3] == ["v1", "pir", "sessions"] and parts[4] == "queries" and method == "POST":
                body = self.read_body(service.limits.max_query_bytes)
                result = service.open_query(parts[3], body)
                self._count(len(body), 0)
                return self.send_json(201, result)
            if len(parts) == 5 and parts[:3] == ["v1", "pir", "queries"] and parts[4] == "blocks" and method == "GET":
                try:
                    start = int(qs.get("start", ["0"])[0])
                    count = int(qs.get("count", ["1"])[0])
                except ValueError:
                    raise HttpError(400, "start and count must be integers") from None
                t0 = time.perf_counter()
                body, compute_ms = service.answer(parts[3], start, count)
                total_ms = (time.perf_counter() - t0) * 1e3
                self._count(0, len(body))
                return self._send_rate_limited(200, body, "application/octet-stream", {
                    "X-PMR-Compute-Ms": f"{compute_ms:.3f}", "X-PMR-Server-Ms": f"{total_ms:.3f}"})
            if len(parts) == 4 and parts[:3] == ["v1", "pir", "queries"] and method == "DELETE":
                service.close_query(parts[3])
                return self.send_json(200, {"closed": True})
            raise HttpError(404, "not found")

    return Handler


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m pmr.server", description="PIR database server (loopback only)")
    ap.add_argument("--catalog", required=True, help="catalog directory (manifest.json + tracks/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--backend", choices=["optimized", "didactic"], default="optimized")
    ap.add_argument("--packing", choices=["u8", "u16"], default="u16")
    ap.add_argument("--query-mode", choices=["selector", "expanded"], default="selector")
    ap.add_argument("--db-form", choices=["ntt", "plain", "raw"], default="ntt")
    ap.add_argument("--no-mod-switch", action="store_true")
    ap.add_argument("--no-seeded", action="store_true", help="tell clients not to use seeded ciphertexts")
    ap.add_argument("--slots", type=int, default=4096)
    ap.add_argument("--workers", type=int, default=1, help="worker processes (block-sharded), default 1")
    ap.add_argument("--max-tracks", type=int, default=None)
    ap.add_argument("--rate-limit-mbps", type=float, default=0.0, help="emulate a link of this many Mbit/s (0=off)")
    ap.add_argument("--ready-file", default=None, help="write params JSON here when ready (used by scripts)")
    ap.add_argument("--verbose", action="store_true")
    return ap


def config_from_args(a: argparse.Namespace) -> BackendConfig:
    if a.backend == "didactic":
        return BackendConfig.didactic(a.packing, a.slots)
    return BackendConfig("optimized", a.packing, a.query_mode, a.db_form, not a.no_mod_switch, not a.no_seeded, a.slots)


def main(argv: list[str] | None = None) -> int:
    a = build_arg_parser().parse_args(argv)
    if not 1 <= a.workers <= 8:
        print("--workers must be in 1..8 (shared machine)", file=sys.stderr)
        return 2
    limits = Limits(rate_limit_bps=a.rate_limit_mbps * 1e6)
    service = PirService(a.catalog, config_from_args(a), workers=a.workers, max_tracks=a.max_tracks, limits=limits)
    handler = make_handler(service)
    handler.quiet = not a.verbose
    httpd = LoopbackHTTPServer(a.host, a.port, handler)
    port = httpd.server_address[1]
    doc = service.params_doc()
    doc["listen"] = {"host": httpd.server_address[0], "port": port, "pid": os.getpid()}
    if a.ready_file:
        tmp = a.ready_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, a.ready_file)
    print(f"PIR server ready on http://{httpd.server_address[0]}:{port} ({service.config.label()}, "
          f"R={service.layout.num_records}, B={service.layout.num_blocks}, workers={service.engine.workers}, "
          f"setup {service.setup_s:.2f}s)", flush=True)

    def stop(*_):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        service.shutdown()
    return 0
