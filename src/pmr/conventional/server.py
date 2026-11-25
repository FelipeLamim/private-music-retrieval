"""Conventional (NON-PRIVATE) baseline server. Run: ``python -m pmr.conventional --catalog DIR``.

Kept deliberately separate from the PIR server (different module, process and port) so that
the private playback path can never fall back to it.

  GET /v1/catalog                public catalog metadata (same document as the PIR server)
  GET /v1/tracks/<index>/pcm     direct single-track download: the server LEARNS the track index
  GET /v1/database               whole-database download (trivial PIR): every track, in index order
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time

from ..catalog import load_catalog, public_catalog
from ..httpbase import HttpError, JsonHandler, LoopbackHTTPServer


class ConventionalService:
    def __init__(self, catalog_dir: str, *, max_tracks: int | None = None, rate_limit_bps: float = 0.0):
        t0 = time.perf_counter()
        self.manifest, self.records = load_catalog(catalog_dir, max_tracks=max_tracks)
        self.setup_s = time.perf_counter() - t0
        self.catalog = public_catalog(self.manifest)
        self.rate_limit_bps = rate_limit_bps
        self.access_log: list[dict] = []  # what a conventional provider can record
        self._lock = threading.Lock()


def make_handler(service: ConventionalService):
    class Handler(JsonHandler):
        def _stream(self, chunks: list[bytes], total: int) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(total))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command == "HEAD":
                return
            bps = service.rate_limit_bps
            t0, sent = time.monotonic(), 0
            for data in chunks:
                for off in range(0, len(data), 65536):
                    part = data[off:off + 65536]
                    self.wfile.write(part)
                    sent += len(part)
                    if bps > 0:
                        delay = t0 + sent * 8 / bps - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)

        def handle_request(self, method: str) -> None:
            path, _ = self.parsed()
            parts = [p for p in path.split("/") if p]
            if method in ("GET", "HEAD") and path == "/healthz":
                return self.send_json(200, {"ok": True})
            if method == "GET" and path == "/v1/catalog":
                return self.send_json(200, service.catalog)
            if method in ("GET", "HEAD") and len(parts) == 4 and parts[:2] == ["v1", "tracks"] and parts[3] == "pcm":
                try:
                    idx = int(parts[2])
                except ValueError:
                    raise HttpError(400, "track index must be an integer") from None
                if not 0 <= idx < len(service.records):
                    raise HttpError(404, "no such track")
                with service._lock:
                    service.access_log.append({"t": time.time(), "track": idx})
                rec = service.records[idx]
                return self._stream([rec], len(rec))
            if method in ("GET", "HEAD") and path == "/v1/database":
                total = sum(len(r) for r in service.records)
                return self._stream(service.records, total)
            if method == "GET" and path == "/v1/server/stats":
                return self.send_json(200, {"pid": os.getpid(), "cpu_s": time.process_time(),
                                            "access_log_entries": len(service.access_log)})
            raise HttpError(404, "not found")

    return Handler


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pmr.conventional", description="Conventional (non-private) baseline server")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8702)
    ap.add_argument("--max-tracks", type=int, default=None)
    ap.add_argument("--rate-limit-mbps", type=float, default=0.0)
    ap.add_argument("--ready-file", default=None)
    a = ap.parse_args(argv)
    service = ConventionalService(a.catalog, max_tracks=a.max_tracks, rate_limit_bps=a.rate_limit_mbps * 1e6)
    httpd = LoopbackHTTPServer(a.host, a.port, make_handler(service))
    port = httpd.server_address[1]
    if a.ready_file:
        with open(a.ready_file + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"listen": {"host": httpd.server_address[0], "port": port, "pid": os.getpid()},
                       "setup_s": service.setup_s}, fh)
        os.replace(a.ready_file + ".tmp", a.ready_file)
    print(f"CONVENTIONAL (non-private) server on http://{httpd.server_address[0]}:{port}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
