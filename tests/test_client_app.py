"""Trusted client app: UI auth, Host check, playback modes, WAV exactness, Range requests,
cancellation, and the catalog read-guard audit hook (requirement 8).

The browser-facing endpoints run in-thread for deterministic timing; the audit hook (a
process-global ``sys.addaudithook``) and the ``--forbid-reads`` integration run in child
processes.
"""
from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import threading
import time

import pytest

from conftest import PY, ROOT, SMOKE_DIR, http_request
from pmr.audio.wavio import wav_header
from pmr.pir.engine import BackendConfig
from pmr.server.app import Limits


@pytest.fixture
def client_app(pir_server):
    from pmr.client.app import ClientApp, make_handler
    from pmr.httpbase import LoopbackHTTPServer, serve_in_thread

    created = []

    def _make(catalog_dir, *, config=None, limits=None, batch=8, first_batch=1, policy="pad"):
        svc, server_url = pir_server(catalog_dir, config=config, limits=limits)
        app = ClientApp(server_url, batch=batch, first_batch=first_batch, policy=policy)
        httpd = LoopbackHTTPServer("127.0.0.1", 0, make_handler(app))
        serve_in_thread(httpd)
        created.append((app, httpd))
        return app, f"http://127.0.0.1:{httpd.server_address[1]}", app.token, svc

    yield _make
    for app, httpd in created:
        try:
            httpd.shutdown()
            httpd.server_close()
        finally:
            app.pir.close()


def poll_until(fn, timeout=20.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = fn()
        if val:
            return val
        time.sleep(interval)
    return None


def play(ui_url, token, track, mode="complete"):
    return http_request(ui_url, "POST", "/api/play", json.dumps({"track": track, "mode": mode}).encode(),
                        headers={"Content-Type": "application/json", "X-PMR-Token": token})


# =========================================================================== auth / host / static

def test_ui_token_required(client_app):
    _, ui, token, _ = client_app(SMOKE_DIR)
    assert http_request(ui, "GET", "/api/catalog")[0] == 403
    assert http_request(ui, "GET", "/api/catalog", headers={"X-PMR-Token": "wrong"})[0] == 403
    assert http_request(ui, "GET", "/api/catalog", headers={"X-PMR-Token": token})[0] == 200
    assert http_request(ui, "GET", f"/api/catalog?t={token}")[0] == 200


def test_host_header_checked(client_app):
    _, ui, token, _ = client_app(SMOKE_DIR)
    assert http_request(ui, "GET", f"/api/catalog?t={token}", host="evil.example.com")[0] == 421


def test_static_index_served_without_token(client_app):
    _, ui, token, _ = client_app(SMOKE_DIR)
    status, headers, body = http_request(ui, "GET", "/")
    assert status == 200 and b"<html" in body.lower() or b"<!doctype" in body.lower()
    assert http_request(ui, "GET", "/static/app.js")[0] == 200
    assert http_request(ui, "GET", "/static/../app.py")[0] == 404  # path traversal blocked


# =========================================================================== playback modes + WAV exactness

@pytest.mark.parametrize("mode", ["progressive", "complete"])
def test_play_recovers_exact_pcm(client_app, smoke_catalog, mode):
    manifest, records = smoke_catalog
    _, ui, token, _ = client_app(SMOKE_DIR)
    track = 1
    status, _, data = play(ui, token, track, mode)
    assert status == 201
    jid = json.loads(data)["id"]

    def done():
        s = json.loads(http_request(ui, "GET", f"/api/jobs/{jid}?t={token}")[2])
        return s if s["state"] in ("complete", "error") else None

    s = poll_until(done, timeout=30)
    assert s is not None and s["state"] == "complete", s
    status, headers, wav = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}")
    assert status == 200
    assert headers["content-type"] == "audio/wav"
    assert wav[:44] == wav_header(manifest["tracks"][track]["pcm_bytes"], 16000)
    assert wav[44:] == records[track]
    assert hashlib.sha256(wav[44:]).hexdigest() == manifest["tracks"][track]["sha256_pcm"]


def test_invalid_track_and_mode_400(client_app):
    _, ui, token, _ = client_app(SMOKE_DIR)
    assert play(ui, token, 99)[0] == 400           # index out of range
    assert play(ui, token, -1)[0] == 400
    assert play(ui, token, "x")[0] == 400           # non-integer track
    assert play(ui, token, 0, mode="bogus")[0] == 400


# =========================================================================== Range requests

def test_range_requests(client_app, smoke_catalog):
    manifest, records = smoke_catalog
    _, ui, token, _ = client_app(SMOKE_DIR)
    status, _, data = play(ui, token, 0, "complete")
    jid = json.loads(data)["id"]
    poll_until(lambda: json.loads(http_request(ui, "GET", f"/api/jobs/{jid}?t={token}")[2])["state"] == "complete",
               timeout=30)
    full = wav_header(manifest["tracks"][0]["pcm_bytes"], 16000) + records[0]
    total = len(full)

    st, h, body = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}", headers={"Range": "bytes=0-99"})
    assert st == 206 and len(body) == 100 and body == full[:100]
    assert h["content-range"] == f"bytes 0-99/{total}"

    st, h, body = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}", headers={"Range": "bytes=-100"})
    assert st == 206 and len(body) == 100 and body == full[-100:]

    st, h, body = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}", headers={"Range": "bytes=100-199"})
    assert st == 206 and body == full[100:200]

    for bad in ("bytes=abc", "bytes=-", f"bytes={total}-{total + 10}"):
        st, _, _ = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}", headers={"Range": bad})
        assert st == 416, bad


# =========================================================================== cancellation (rate-limited)

@pytest.fixture
def slow_two_track(make_catalog):
    from conftest import rand_pcm
    return make_catalog([rand_pcm(320000, 1), rand_pcm(320000, 2)])  # ~10 s each -> 40 u16 blocks


def test_cancel_mid_retrieval(client_app, slow_two_track):
    app, ui, token, _ = client_app(slow_two_track, limits=Limits(rate_limit_bps=6e6),
                                   batch=2, first_batch=1, policy="pad")
    status, _, data = play(ui, token, 0, "progressive")
    jid = json.loads(data)["id"]
    assert poll_until(lambda: app.job(jid).status()["blocks_done"] >= 1 and app.job(jid).status()["state"] == "running",
                      timeout=15), "retrieval never started streaming"
    assert http_request(ui, "POST", f"/api/jobs/{jid}/cancel?t={token}")[0] == 200
    final = poll_until(lambda: (app.job(jid).status()["state"] == "cancelled") or None, timeout=20)
    assert final, app.job(jid).status()
    s = app.job(jid).status()
    assert s["blocks_done"] < s["blocks_total"], "cancel did not interrupt mid-retrieval"


def test_new_track_cancels_previous(client_app, slow_two_track):
    app, ui, token, _ = client_app(slow_two_track, limits=Limits(rate_limit_bps=6e6),
                                   batch=2, first_batch=1, policy="pad")
    jid_a = json.loads(play(ui, token, 0, "progressive")[2])["id"]
    assert poll_until(lambda: app.job(jid_a).status()["blocks_done"] >= 1, timeout=15)
    play(ui, token, 1, "progressive")  # selecting a new track cancels the running one
    assert poll_until(lambda: app.job(jid_a).status()["state"] == "cancelled" or None, timeout=20), \
        app.job(jid_a).status()


def test_audio_stream_ends_on_cancel(client_app, slow_two_track):
    app, ui, token, _ = client_app(slow_two_track, limits=Limits(rate_limit_bps=6e6),
                                   batch=2, first_batch=1, policy="pad")
    jid = json.loads(play(ui, token, 0, "progressive")[2])["id"]
    assert poll_until(lambda: app.job(jid).status()["blocks_done"] >= 1, timeout=15)
    total = 44 + app.job(jid).status()["pcm_total"]

    port = int(ui.rsplit(":", 1)[1])
    sock = socket.create_connection(("127.0.0.1", port), timeout=20)
    sock.sendall(f"GET /api/jobs/{jid}/audio.wav?t={token} HTTP/1.1\r\n"
                 f"Host: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())

    def cancel_later():
        time.sleep(0.4)
        app.job(jid).cancel.set()

    threading.Thread(target=cancel_later, daemon=True).start()
    sock.settimeout(20)
    received = bytearray()
    try:
        while True:
            c = sock.recv(65536)
            if not c:
                break
            received += c
    except socket.timeout:
        pass
    finally:
        sock.close()
    head, _, body = bytes(received).partition(b"\r\n\r\n")
    assert head.split(b"\r\n", 1)[0].split()[1] in (b"200", b"206")
    assert len(body) < total, "stream did not end early on cancel"
    assert poll_until(lambda: app.job(jid).status()["state"] == "cancelled" or None, timeout=10)


# =========================================================================== wire log endpoint

def test_wire_log_shows_tokenised_paths(client_app):
    _, ui, token, _ = client_app(SMOKE_DIR)
    jid = json.loads(play(ui, token, 0, "complete")[2])["id"]
    poll_until(lambda: json.loads(http_request(ui, "GET", f"/api/jobs/{jid}?t={token}")[2])["state"] == "complete",
               timeout=30)
    w = json.loads(http_request(ui, "GET", f"/api/wire?t={token}")[2])
    assert w["entries"], "wire log should record PIR-server requests"
    for e in w["entries"]:
        assert "0" <= e["path"][-1] or e["path"].endswith("queries") or "blocks" in e["path"] or "sessions" in e["path"]
        # no catalog identifier appears in a recorded path
        assert "t00" not in e["path"] and "t01" not in e["path"]


# =========================================================================== audit hook (subprocess)

def test_audit_hook_blocks_open_in_protected_dir(tmp_path):
    script = (
        "import sys\n"
        "from pmr.client.app import install_catalog_read_guard\n"
        "d, f = sys.argv[1], sys.argv[2]\n"
        "install_catalog_read_guard(d)\n"
        "try:\n"
        "    open(f, 'rb').read()\n"
        "    print('NO_RAISE')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
    )
    protected = str(SMOKE_DIR)
    target = str(SMOKE_DIR / "manifest.json")
    proc = subprocess.run([PY, "-c", script, protected, target], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "BLOCKED" in proc.stdout and "NO_RAISE" not in proc.stdout


# =========================================================================== --forbid-reads integration

@pytest.mark.integration
def test_forbid_reads_client_retrieval_succeeds(pir_server, launch, smoke_catalog):
    """A full retrieval in a client process started with --forbid-reads data/catalog succeeds,
    proving the client never opens the server's audio files."""
    manifest, records = smoke_catalog
    _, server_url = pir_server(SMOKE_DIR)
    client = launch(
        "pmr.client",
        ["--server", server_url, "--port", "0", "--forbid-reads", str(ROOT / "data" / "catalog")],
        ready_name="ui_ready.json",
    )
    ui, token = client.base_url, client.ready["token"]

    # auth and host checks also hold for the real process
    assert http_request(ui, "GET", "/api/catalog")[0] == 403
    assert http_request(ui, "GET", f"/api/catalog?t={token}", host="evil.example.com")[0] == 421

    status, _, data = http_request(ui, "POST", "/api/play", json.dumps({"track": 0, "mode": "complete"}).encode(),
                                   headers={"Content-Type": "application/json", "X-PMR-Token": token})
    assert status == 201
    jid = json.loads(data)["id"]
    s = poll_until(lambda: (lambda st: st if st["state"] in ("complete", "error") else None)(
        json.loads(http_request(ui, "GET", f"/api/jobs/{jid}?t={token}")[2])), timeout=40)
    assert s is not None and s["state"] == "complete", (s, client.read_log())
    _, _, wav = http_request(ui, "GET", f"/api/jobs/{jid}/audio.wav?t={token}")
    assert wav[44:] == records[0]
    assert client.proc.poll() is None  # the guard never aborted the process
