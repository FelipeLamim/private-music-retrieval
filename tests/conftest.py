"""Shared fixtures and helpers for the PIR test suite.

All servers bind to 127.0.0.1 on an ephemeral port (port 0) and are torn down with
guaranteed finalizers. Nothing here writes outside the project root or touches the network
beyond loopback. Tiny catalogs are built in ``tmp_path`` with real WAV files + a valid
manifest so ``pmr.catalog.load_catalog`` (which re-verifies the PCM SHA-256) accepts them.
"""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from pmr.audio.dataset import ALLOWED_LICENSES, manifest_json
from pmr.audio.wavio import write_wav
from pmr.catalog import load_catalog
from pmr.httpbase import LoopbackHTTPServer, serve_in_thread

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "catalog"
SMOKE_DIR = DATA / "smoke"
MINI_DIR = DATA / "mini"
FULL_DIR = DATA / "full"
PY = sys.executable


# --------------------------------------------------------------------------- PCM / catalogs

def rand_pcm(nbytes: int, seed: int = 0) -> bytes:
    """Deterministic pseudo-random 16-bit PCM of exactly ``nbytes`` bytes (even)."""
    if nbytes % 2:
        raise ValueError("nbytes must be even (16-bit samples)")
    rng = np.random.default_rng(seed)
    return rng.integers(-32768, 32768, nbytes // 2, dtype=np.int16).tobytes()


def write_catalog(out_dir, pcms, *, ids=None, titles=None, catalog_id="testcat") -> Path:
    """Write a minimal but valid catalog (user_own / CC0) with the given PCM payloads."""
    d = Path(out_dir)
    (d / "tracks").mkdir(parents=True, exist_ok=True)
    tracks = []
    for i, pcm in enumerate(pcms):
        if len(pcm) % 2:
            raise ValueError("each PCM payload must have an even number of bytes")
        name = ids[i] if ids else f"t{i:02d}"
        write_wav(d / "tracks" / f"{name}.wav", pcm, 16000)
        n = len(pcm) // 2
        tracks.append({
            "id": name, "index": i, "title": (titles[i] if titles else f"Track {i}"),
            "artist": "Test Artist", "duration_s": n / 16000, "num_samples": n, "pcm_bytes": len(pcm),
            "sha256_pcm": hashlib.sha256(pcm).hexdigest(), "file": f"tracks/{name}.wav",
            "origin": "user_own", "license": "CC0-1.0", "license_url": ALLOWED_LICENSES["CC0-1.0"],
            "attribution": "test", "source_url": None, "modifications": [], "generator": None,
        })
    manifest = {
        "schema_version": 1, "catalog_id": catalog_id,
        "created_by": "pmr.audio.dataset/1 (pmr.audio.synth/1)",
        "sample_rate": 16000, "channels": 1, "sample_width": 2, "tracks": tracks,
    }
    (d / "manifest.json").write_text(manifest_json(manifest), encoding="utf-8")
    return d


@pytest.fixture(scope="session")
def smoke_catalog():
    """(manifest, records) for data/catalog/smoke (4 short synthetic tracks)."""
    return load_catalog(str(SMOKE_DIR))


@pytest.fixture(scope="session")
def smoke_records(smoke_catalog):
    return smoke_catalog[1]


@pytest.fixture
def make_catalog(tmp_path):
    """Factory: build a tiny catalog in tmp_path and return its directory path."""
    counter = {"n": 0}

    def _make(pcms, *, ids=None, titles=None, catalog_id="testcat"):
        counter["n"] += 1
        d = tmp_path / f"cat{counter['n']}"
        return write_catalog(d, pcms, ids=ids, titles=titles, catalog_id=catalog_id)

    return _make


# --------------------------------------------------------------------------- thread servers

@contextlib.contextmanager
def serve(handler_cls):
    httpd = LoopbackHTTPServer("127.0.0.1", 0, handler_cls)
    serve_in_thread(httpd)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def pir_server():
    """Factory starting an in-process PIR server (workers=1) in a thread.

    Returns (service, base_url). Every server created is shut down at teardown.
    """
    from pmr.server.app import PirService, make_handler
    from pmr.pir.engine import BackendConfig

    created = []

    def _start(catalog_dir, config=None, *, limits=None, max_tracks=None):
        svc = PirService(str(catalog_dir), config or BackendConfig(), limits=limits, max_tracks=max_tracks)
        httpd = LoopbackHTTPServer("127.0.0.1", 0, make_handler(svc))
        serve_in_thread(httpd)
        created.append((svc, httpd))
        return svc, f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for svc, httpd in created:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()
        with contextlib.suppress(Exception):
            svc.shutdown()


@pytest.fixture
def conventional_server():
    from pmr.conventional.server import ConventionalService, make_handler

    created = []

    def _start(catalog_dir, *, rate_limit_bps=0.0):
        svc = ConventionalService(str(catalog_dir), rate_limit_bps=rate_limit_bps)
        httpd = LoopbackHTTPServer("127.0.0.1", 0, make_handler(svc))
        serve_in_thread(httpd)
        created.append(httpd)
        return svc, f"http://127.0.0.1:{httpd.server_address[1]}"

    yield _start
    for httpd in created:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


# --------------------------------------------------------------------------- subprocess servers

class Subprocess:
    def __init__(self, proc: subprocess.Popen, ready: dict, log_path: Path):
        self.proc = proc
        self.ready = ready
        self.log_path = log_path
        self.port = ready["listen"]["port"]
        self.host = ready["listen"]["host"]
        self.base_url = f"http://{self.host}:{self.port}"

    def read_log(self) -> str:
        with contextlib.suppress(Exception):
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        return ""


@pytest.fixture
def launch():
    """Factory launching ``python -m <module> <args>`` with a ready-file, returning a Subprocess."""
    started: list[subprocess.Popen] = []
    tmp = ROOT / ".tmp"
    tmp.mkdir(exist_ok=True)

    def _launch(module, args, *, ready_name, timeout=60.0):
        ready_file = tmp / ready_name
        with contextlib.suppress(FileNotFoundError):
            ready_file.unlink()
        log_path = tmp / (ready_name + ".log")
        log = open(log_path, "wb")
        # The fixture owns the ready-file path (absolute) so it always matches what we poll.
        full = [PY, "-m", module, *args, "--ready-file", str(ready_file)]
        proc = subprocess.Popen(full, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        started.append(proc)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ready_file.exists():
                with open(ready_file, encoding="utf-8") as fh:
                    ready = json.load(fh)
                return Subprocess(proc, ready, log_path)
            if proc.poll() is not None:
                log.flush()
                raise RuntimeError(f"{module} exited early ({proc.returncode}):\n{log_path.read_text(errors='replace')}")
            time.sleep(0.05)
        raise TimeoutError(f"{module} did not become ready in {timeout}s")

    yield _launch

    for proc in started:
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        with contextlib.suppress(Exception):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


# --------------------------------------------------------------------------- HTTP helpers

def http_request(base_url, method, path, body=None, headers=None, host=None, timeout=30.0):
    """Single HTTP request (fresh connection). Returns (status, headers_lowercased, body_bytes)."""
    hostport = base_url[len("http://"):].rstrip("/")
    h, _, p = hostport.partition(":")
    port = int(p or 80)
    conn = http.client.HTTPConnection(h, port, timeout=timeout)
    hdrs = {"Host": host if host is not None else f"{h}:{port}"}
    if body is not None:
        hdrs.setdefault("Content-Type", "application/octet-stream")
    hdrs.update(headers or {})
    try:
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def raw_request(port, raw_bytes, read_timeout=3.0):
    """Send raw bytes on a loopback TCP socket; return the full raw response bytes."""
    s = socket.create_connection(("127.0.0.1", port), timeout=read_timeout)
    try:
        s.sendall(raw_bytes)
        s.settimeout(read_timeout)
        chunks = []
        with contextlib.suppress(socket.timeout, OSError):
            while True:
                c = s.recv(65536)
                if not c:
                    break
                chunks.append(c)
        return b"".join(chunks)
    finally:
        s.close()


def status_line(raw: bytes) -> bytes:
    return raw.split(b"\r\n", 1)[0]


def split_http_messages(raw: bytes):
    """Parse captured client->server bytes into (head, body) pairs.

    ``head`` is the request line + headers (textual); ``body`` is the Content-Length payload
    (opaque ciphertext). Scanning only ``head`` for short identifiers avoids false positives
    from a short ASCII id appearing by chance inside a random ciphertext body.
    """
    import re
    msgs = []
    i = 0
    while True:
        j = raw.find(b"\r\n\r\n", i)
        if j == -1:
            break
        head = raw[i:j]
        m = re.search(rb"[Cc]ontent-[Ll]ength:\s*(\d+)", head)
        body_len = int(m.group(1)) if m else 0
        body = raw[j + 4:j + 4 + body_len]
        msgs.append((head, body))
        i = j + 4 + body_len
    return msgs


# --------------------------------------------------------------------------- recording proxy

class RecordingProxy:
    """Loopback TCP proxy that forwards to an upstream and tees every client->upstream byte.

    Used by the privacy tests to inspect the exact request line, headers and body the client
    sends to the PIR server (not only the structured wire log).
    """

    def __init__(self, upstream_host: str, upstream_port: int):
        self.upstream = (upstream_host, upstream_port)
        self.captured = bytearray()
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(32)
        self.port = self._srv.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._stop = False
        self._threads: list[threading.Thread] = []
        t = threading.Thread(target=self._accept, daemon=True)
        t.start()
        self._threads.append(t)

    def _accept(self):
        while not self._stop:
            try:
                cs, _ = self._srv.accept()
            except OSError:
                return
            t = threading.Thread(target=self._handle, args=(cs,), daemon=True)
            t.start()
            self._threads.append(t)

    def _handle(self, cs):
        try:
            us = socket.create_connection(self.upstream)
        except OSError:
            cs.close()
            return

        def c2u():
            try:
                while True:
                    data = cs.recv(65536)
                    if not data:
                        break
                    with self._lock:
                        self.captured += data
                    us.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    us.shutdown(socket.SHUT_WR)

        def u2c():
            try:
                while True:
                    data = us.recv(65536)
                    if not data:
                        break
                    cs.sendall(data)
            except OSError:
                pass
            finally:
                with contextlib.suppress(OSError):
                    cs.shutdown(socket.SHUT_WR)

        t1 = threading.Thread(target=c2u, daemon=True)
        t2 = threading.Thread(target=u2c, daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        with contextlib.suppress(OSError):
            cs.close()
        with contextlib.suppress(OSError):
            us.close()

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self.captured)

    def close(self):
        self._stop = True
        with contextlib.suppress(OSError):
            self._srv.close()


@pytest.fixture
def recording_proxy():
    proxies: list[RecordingProxy] = []

    def _make(base_url):
        hostport = base_url[len("http://"):].rstrip("/")
        h, _, p = hostport.partition(":")
        proxy = RecordingProxy(h, int(p))
        proxies.append(proxy)
        return proxy

    yield _make
    for proxy in proxies:
        proxy.close()
