"""Trusted local client process: holds the BFV secret key and serves the browser UI.

Run: ``python -m pmr.client --server http://127.0.0.1:8700`` and open the printed URL.

The browser talks only to this process (loopback). This process talks to the PIR server with
ciphertexts, random tokens and public block indices. Audio played by the browser is built
exclusively from decrypted PIR responses held in memory here; this process never opens the
server's audio files and there is no code path that downloads audio in clear.

UI API (all require the per-launch token, as header ``X-PMR-Token`` or query ``t=``):
  GET  /api/catalog                  public catalog + protocol parameters
  POST /api/play                     {"track": i, "mode": "progressive"|"complete"} -> job
  GET  /api/jobs/<id>                progress and statistics
  GET  /api/jobs/<id>/audio.wav      WAV stream built from decrypted blocks (Range supported)
  POST /api/jobs/<id>/cancel
  POST /api/jobs/<id>/playback       browser playback events (stalls, start time)
  GET  /api/wire                     what the PIR server received (method, path, sizes)
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from ..audio.wavio import wav_header
from ..httpbase import HttpError, JsonHandler, LoopbackHTTPServer
from .pirclient import PirHttpClient

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
JOB_RE = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
MAX_JOBS_KEPT = 4


@dataclass
class Job:
    id: str
    track: int
    mode: str
    total_pcm: int
    total_blocks: int
    sample_rate: int
    started: float = field(default_factory=time.perf_counter)
    pcm: bytearray = field(default_factory=bytearray)
    blocks_done: int = 0
    state: str = "running"  # running | complete | cancelled | error
    error: str | None = None
    first_block_s: float | None = None
    finished_s: float | None = None
    result: dict[str, Any] | None = None
    playback: dict[str, Any] = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)
    cond: threading.Condition = field(default_factory=threading.Condition)

    def header(self) -> bytes:
        return wav_header(self.total_pcm, self.sample_rate)

    def status(self) -> dict[str, Any]:
        with self.cond:
            now = time.perf_counter() - self.started
            done_pcm = len(self.pcm)
            return {
                "id": self.id, "track": self.track, "mode": self.mode, "state": self.state, "error": self.error,
                "blocks_done": self.blocks_done, "blocks_total": self.total_blocks,
                "pcm_done": done_pcm, "pcm_total": self.total_pcm,
                "audio_s_available": done_pcm / (2 * self.sample_rate),
                "audio_s_total": self.total_pcm / (2 * self.sample_rate),
                "elapsed_s": self.finished_s if self.finished_s is not None else now,
                "first_block_s": self.first_block_s,
                "result": self.result, "playback": self.playback,
            }


class ClientApp:
    def __init__(self, server_url: str, *, batch: int = 8, first_batch: int = 1, policy: str = "pad"):
        self.server_url = server_url
        self.pir = PirHttpClient(server_url, record_wire=True)
        self.pir.open_session()
        self.catalog = self.pir.catalog
        self.batch, self.first_batch, self.policy = batch, first_batch, policy
        self.token = secrets.token_urlsafe(18)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()  # one retrieval at a time (bounded client work)

    def public_info(self) -> dict[str, Any]:
        p = self.pir.params
        return {"catalog": self.catalog, "backend": p["backend"], "layout": p["layout"], "server": p["server"],
                "client": {"policy": self.policy, "batch": self.batch, "first_batch": self.first_batch,
                           "setup": self.pir.setup_timings, "setup_bytes": self.pir.setup_bytes}}

    def start(self, track: int, mode: str) -> Job:
        if mode not in ("progressive", "complete"):
            raise HttpError(400, "mode must be 'progressive' or 'complete'")
        tracks = self.catalog["tracks"]
        if not isinstance(track, int) or not 0 <= track < len(tracks):
            raise HttpError(400, "invalid track")
        with self._lock:
            for j in self.jobs.values():  # a new selection cancels the running retrieval
                if j.state == "running":
                    j.cancel.set()
            job = Job(secrets.token_urlsafe(9), track, mode, int(tracks[track]["pcm_bytes"]),
                      self.pir.layout.num_blocks if self.policy == "pad" else self.pir.layout.blocks_for_record(track),
                      int(self.catalog["audio"]["sample_rate"]))
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS_KEPT:
                oldest = next(iter(self.jobs))
                self.jobs[oldest].cancel.set()
                del self.jobs[oldest]
        threading.Thread(target=self._run, args=(job,), name=f"job-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        def on_block(j: int, chunk: bytes) -> None:
            with job.cond:
                job.pcm += chunk
                job.blocks_done += 1
                if job.first_block_s is None and chunk:
                    job.first_block_s = time.perf_counter() - job.started
                job.cond.notify_all()

        try:
            with self._run_lock:
                if job.cancel.is_set():
                    raise InterruptedError
                res = self.pir.retrieve(job.track, policy=self.policy, first_batch=self.first_batch,
                                        batch=self.batch, on_block=on_block, cancel=job.cancel)
            with job.cond:
                job.state = "complete" if res.complete else "cancelled"
                job.result = {"timings_ms": {k: (round(v, 2) if math.isfinite(v) else None) for k, v in res.timings.items()},
                              "bytes": res.bytes,
                              "server_compute_ms": round(res.server_compute_ms, 2),
                              "server_total_ms": round(res.server_total_ms, 2)}
        except InterruptedError:
            with job.cond:
                job.state = "cancelled"
        except Exception as exc:
            with job.cond:
                job.state, job.error = "error", f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            with job.cond:
                job.finished_s = time.perf_counter() - job.started
                job.cond.notify_all()

    def job(self, jid: str) -> Job:
        if not JOB_RE.match(jid):
            raise HttpError(404, "unknown job")
        with self._lock:
            j = self.jobs.get(jid)
        if j is None:
            raise HttpError(404, "unknown job")
        return j

    def wire_log(self, limit: int = 40) -> list[dict[str, Any]]:
        entries = self.pir.transport.log[-limit:]
        return [{"method": e.method, "path": e.path, "request_bytes": len(e.request_body), "status": e.status,
                 "response_bytes": e.response_body_len} for e in entries]


def make_handler(app: ClientApp):
    class Handler(JsonHandler):
        max_body = 64 * 1024

        def _auth(self, qs: dict[str, list[str]]) -> None:
            tok = self.headers.get("X-PMR-Token") or (qs.get("t") or [""])[0]
            if not secrets.compare_digest(tok, app.token):
                raise HttpError(403, "missing or invalid UI token (open the URL printed by the client)")

        def _static(self, name: str) -> None:
            path = os.path.normpath(os.path.join(STATIC_DIR, name))
            if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
                raise HttpError(404, "not found")
            with open(path, "rb") as fh:
                body = fh.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_bytes(200, body, ctype, {
                "Content-Security-Policy": "default-src 'self'; media-src 'self'; style-src 'self'; script-src 'self'",
                "Referrer-Policy": "no-referrer"})

        def _audio(self, job: Job) -> None:
            header = job.header()
            total = len(header) + job.total_pcm
            start, end, status = 0, total - 1, 200
            rng = self.headers.get("Range")
            if rng:
                m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
                if not m or (m.group(1) == "" and m.group(2) == ""):
                    raise HttpError(416, "bad range")
                if m.group(1) == "":
                    start = max(0, total - int(m.group(2)))
                else:
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), total - 1)
                if start > end or start >= total:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206
            with job.cond:
                produced = len(header) + len(job.pcm)
                stopped = job.state != "running"
            if stopped and produced < total:
                if start >= produced:  # retrieval stopped early: nothing at or after `start` will ever exist
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{produced}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, produced - 1)  # serve what exists with a truthful Content-Length
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            if self.command == "HEAD":
                return
            pos = start
            while pos <= end:
                if pos < len(header):
                    piece = header[pos:min(len(header), end + 1)]
                else:
                    off = pos - len(header)
                    with job.cond:
                        while len(job.pcm) <= off and job.state == "running":
                            job.cond.wait(timeout=1.0)
                        avail = len(job.pcm)
                        if avail <= off:  # retrieval stopped early (cancel/error): end the stream
                            self.close_connection = True
                            return
                        piece = bytes(job.pcm[off:min(avail, end + 1 - len(header))])
                self.wfile.write(piece)
                pos += len(piece)

        def handle_request(self, method: str) -> None:
            path, qs = self.parsed()
            parts = [p for p in path.split("/") if p]
            if method in ("GET", "HEAD") and path in ("/", "/index.html"):
                return self._static("index.html")
            if method in ("GET", "HEAD") and len(parts) == 2 and parts[0] == "static":
                return self._static(parts[1])
            if not parts or parts[0] != "api":
                raise HttpError(404, "not found")
            self._auth(qs)
            if method == "GET" and parts == ["api", "catalog"]:
                return self.send_json(200, app.public_info())
            if method == "GET" and parts == ["api", "wire"]:
                return self.send_json(200, {"server": app.server_url, "entries": app.wire_log()})
            if method == "POST" and parts == ["api", "play"]:
                try:
                    doc = json.loads(self.read_body() or b"{}")
                except ValueError:
                    raise HttpError(400, "invalid JSON") from None
                job = app.start(doc.get("track"), doc.get("mode", "progressive"))
                return self.send_json(201, job.status())
            if len(parts) >= 3 and parts[1] == "jobs":
                job = app.job(parts[2])
                if len(parts) == 3 and method == "GET":
                    return self.send_json(200, job.status())
                if len(parts) == 4 and parts[3] == "audio.wav" and method in ("GET", "HEAD"):
                    return self._audio(job)
                if len(parts) == 4 and parts[3] == "cancel" and method == "POST":
                    job.cancel.set()
                    return self.send_json(200, {"cancelled": True})
                if len(parts) == 4 and parts[3] == "playback" and method == "POST":
                    try:
                        doc = json.loads(self.read_body() or b"{}")
                    except ValueError:
                        raise HttpError(400, "invalid JSON") from None
                    allowed = {"startup_s", "stalls", "stall_s", "ended", "position_s", "events"}
                    with job.cond:
                        job.playback.update({k: v for k, v in doc.items() if k in allowed})
                    return self.send_json(200, {"ok": True})
            raise HttpError(404, "not found")

    return Handler


def install_catalog_read_guard(protected_dir: str) -> None:
    """Audit hook: abort if this process ever opens a file under ``protected_dir``.

    Used by tests and available as ``--forbid-reads DIR`` to demonstrate that playback does not
    read the server's audio files.
    """
    protected = os.path.realpath(protected_dir) + os.sep

    def hook(event: str, args: tuple) -> None:
        if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
            p = os.fsdecode(args[0])
            if os.path.realpath(p).startswith(protected):
                raise PermissionError(f"trusted client attempted to open server audio: {p}")

    sys.addaudithook(hook)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pmr.client", description="Trusted local PIR client + browser UI")
    ap.add_argument("--server", default="http://127.0.0.1:8700", help="PIR server base URL (loopback)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8701)
    ap.add_argument("--batch", type=int, default=8, help="blocks per request after the first")
    ap.add_argument("--first-batch", type=int, default=1, help="blocks in the first request (startup latency)")
    ap.add_argument("--policy", choices=["pad", "stop"], default="pad",
                    help="pad: always fetch all catalog blocks (hides length); stop: stop at track end (leaks length)")
    ap.add_argument("--forbid-reads", default=None, help="abort if any file under this directory is opened")
    ap.add_argument("--ready-file", default=None)
    a = ap.parse_args(argv)
    if a.forbid_reads:
        install_catalog_read_guard(a.forbid_reads)
    app = ClientApp(a.server, batch=a.batch, first_batch=a.first_batch, policy=a.policy)
    httpd = LoopbackHTTPServer(a.host, a.port, make_handler(app))
    host, port = httpd.server_address[0], httpd.server_address[1]
    url = f"http://{host}:{port}/?t={app.token}"
    if a.ready_file:
        with open(a.ready_file + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"listen": {"host": host, "port": port, "pid": os.getpid()}, "url": url,
                       "token": app.token}, fh)
        os.replace(a.ready_file + ".tmp", a.ready_file)
    print(f"Trusted client ready. Open in a browser on this machine:\n  {url}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    signal.signal(signal.SIGINT, lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start())
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        app.pir.close()
    return 0
