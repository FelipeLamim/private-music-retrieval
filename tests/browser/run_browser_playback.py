#!/usr/bin/env python3
"""Headless-browser playback test of the private music-streaming UI (PIR server + BFV).

Normally started by ``scripts/browser_test.sh``, which launches the PIR server(s) and the trusted
client(s) on 127.0.0.1 with ephemeral ports and passes their ``--ready-file`` JSON paths here.
Runs with the separate virtualenv ``.venv-browser`` (Playwright); it imports only the standard-library reporting helper from ``pmr``.

Required tests (ids are used in results/browser/*.json and tests/browser/README.md):
  T1 ui_load               the printed URL opens the landing screen; "Começar a ouvir" opens the
                           player (#ouvir); 10 catalog rows render; #proto shows the server's BFV parameters
  T2 progressive_playback  "Ouvir" in progressive mode -> 'playing', currentTime > 2 s, 'ended'
  T3 audio_content_hash    in-page fetch of the same audio.wav URL, decodeAudioData at 16 kHz,
                           int16 conversion, crypto.subtle SHA-256 vs manifest sha256_pcm
  T4 complete_mode         audio src set only after the job is 'complete', then playback starts;
                           #btn-toggle pauses and resumes
  T5 cancel                #btn-cancel during a (full-catalog) retrieval -> state 'cancelled'
  T6 wire_table            #wire rows hold only random tokens and block indices
  T6b wire_schedule        PIR request sequence is identical for two different tracks
  T7 artifacts             screenshot logs/browser/ui_after_playback.png + this JSON; the metric
                           cards show this retrieval's values (blocks and received bytes = job status)
  T8 ui_states_and_layout  access error without the token, header navigation, no horizontal
                           overflow and visible keyboard focus at 390 px
Extra probes (reported, never change the exit code):
  X1 cancel_before_first_block  cancel before the first block is decrypted
  X2 autoplay_without_flag      UI flows in a browser launched without --autoplay-policy
  X3 playback_after_cancel      what the <audio> element does when a cancelled stream runs out

The test harness talks to the client API directly only to *observe* job state (GET with the UI
token); every action under test goes through the page (clicks, select, the page's own fetches).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

try:
    from playwright.sync_api import Error as PwError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment problem, not a test failure
    print("playwright is not importable: run tests/browser/setup.sh and use .venv-browser/bin/python",
          file=sys.stderr)
    raise SystemExit(2)

SCHEMA = "pmr-browser-playback/1"
ROOT = Path(os.environ.get("PMR_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "src"))
from pmr.reporting import public_report
PIR_TOKEN = r"[A-Za-z0-9_-]{16,64}"
WIRE_WHITELIST = [
    ("GET", re.compile(r"^/v1/catalog$")),
    ("GET", re.compile(r"^/v1/pir/params$")),
    ("POST", re.compile(r"^/v1/pir/sessions$")),
    ("DELETE", re.compile(rf"^/v1/pir/sessions/{PIR_TOKEN}$")),
    ("POST", re.compile(rf"^/v1/pir/sessions/{PIR_TOKEN}/queries$")),
    ("GET", re.compile(rf"^/v1/pir/queries/{PIR_TOKEN}/blocks\?start=\d+&count=\d+$")),
    ("DELETE", re.compile(rf"^/v1/pir/queries/{PIR_TOKEN}$")),
]
BASE_LAUNCH_ARGS = [
    # The UI calls audio.play() from promise callbacks (progressive: after POST /api/play;
    # complete: after polling sees 'complete'). X2 checks the behaviour without this flag.
    "--autoplay-policy=no-user-gesture-required",
    # Fake (null) audio output streams: deterministic on hosts with or without a sound card.
    "--disable-audio-output",
    # Defence in depth: no DNS resolution except loopback; the test must stay on 127.0.0.1.
    "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
]

# --------------------------------------------------------------------------------------------
# In-page instrumentation (passive listeners only; injected before the UI scripts run).
INIT_JS = r"""
(() => {
  if (window.__pmrTest) return;
  const rec = {timeOrigin: performance.timeOrigin, media: [], src: [], samples: [], clicks: [], autoCancel: null};
  window.__pmrTest = rec;
  const now = () => performance.now();
  const cap = (arr, n) => { if (arr.length > n) arr.splice(0, arr.length - n); };
  document.addEventListener('click', (e) => {
    const el = e.target instanceof Element ? e.target : null;
    const tr = el ? el.closest('tr') : null;
    rec.clicks.push({t: now(), id: (el && el.id) || null, text: el ? el.textContent.trim().slice(0, 40) : null,
                     row: (tr && tr.id) || null, trusted: e.isTrusted});
  }, true);
  document.addEventListener('DOMContentLoaded', () => {
    const a = document.getElementById('audio');
    if (!a) return;
    const progress = () => { const p = document.getElementById('progress-text'); return p ? p.textContent : ''; };
    ['loadstart', 'durationchange', 'loadedmetadata', 'loadeddata', 'canplay', 'canplaythrough', 'play', 'playing',
     'waiting', 'stalled', 'suspend', 'pause', 'ended', 'error', 'emptied', 'abort', 'ratechange', 'seeking', 'seeked']
      .forEach((ev) => a.addEventListener(ev, () => {
        rec.media.push({ev, t: now(), ct: a.currentTime, rs: a.readyState, ns: a.networkState, paused: a.paused,
                        rate: a.playbackRate, err: a.error ? a.error.code : null});
        cap(rec.media, 4000);
      }));
    new MutationObserver((muts) => {
      rec.src.push({t: now(), n: muts.length, set: a.hasAttribute('src'), progress: progress()});
    }).observe(a, {attributes: true, attributeFilter: ['src']});
    setInterval(() => {
      rec.samples.push({t: now(), src: a.hasAttribute('src'), ns: a.networkState, rs: a.readyState,
                        ct: a.currentTime, paused: a.paused});
      cap(rec.samples, 6000);
    }, 50);
  });
})();
"""

# Clicks #btn-cancel (through the UI's own handler) as soon as the UI enables it.
ARM_AUTOCANCEL_JS = r"""
() => {
  const b = document.getElementById('btn-cancel');
  const rec = window.__pmrTest;
  rec.autoCancel = {armed: true, t: null};
  const mo = new MutationObserver(() => {
    if (rec.autoCancel.armed && !b.disabled) {
      rec.autoCancel.armed = false;
      rec.autoCancel.t = performance.now();
      mo.disconnect();
      b.click();
    }
  });
  mo.observe(b, {attributes: true, attributeFilter: ['disabled']});
  return b.disabled;
}
"""

HASH_JS = r"""
async ({src, n, rate}) => {
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
  const sha = async (buf) => hex(await crypto.subtle.digest('SHA-256', buf));
  const t0 = performance.now();
  const resp = await fetch(src, {cache: 'no-store'});
  const wav = await resp.arrayBuffer();
  const out = {status: resp.status, contentType: resp.headers.get('content-type'), wavBytes: wav.byteLength,
               fetchMs: performance.now() - t0, secureContext: window.isSecureContext};
  const dv = new DataView(wav);
  const tag = (o) => String.fromCharCode(dv.getUint8(o), dv.getUint8(o + 1), dv.getUint8(o + 2), dv.getUint8(o + 3));
  out.riff = tag(0); out.wave = tag(8);
  let off = 12;
  while (off + 8 <= wav.byteLength) {
    const id = tag(off), len = dv.getUint32(off + 4, true);
    if (id === 'fmt ') out.fmt = {format: dv.getUint16(off + 8, true), channels: dv.getUint16(off + 10, true),
                                  sampleRate: dv.getUint32(off + 12, true), bits: dv.getUint16(off + 22, true)};
    if (id === 'data') { out.dataOffset = off + 8; out.dataBytes = Math.min(len, wav.byteLength - off - 8); break; }
    off += 8 + len + (len & 1);
  }
  if (out.dataOffset == null) return out;
  const pcm = wav.slice(out.dataOffset, out.dataOffset + out.dataBytes);
  out.rawPcmSha256 = await sha(pcm);
  const pdv = new DataView(pcm), ref = new Int16Array(pcm.byteLength >> 1);
  let refMin = 0, refMax = 0, refGe16384 = 0;
  for (let i = 0; i < ref.length; i++) {
    const v = pdv.getInt16(2 * i, true); ref[i] = v;
    if (v < refMin) refMin = v; if (v > refMax) refMax = v; if (v >= 16384) refGe16384++;
  }
  Object.assign(out, {refSamples: ref.length, refMin, refMax, refSamplesGe16384: refGe16384});
  const ctx = new OfflineAudioContext(1, n, rate);
  const t1 = performance.now();
  const ab = await ctx.decodeAudioData(wav.slice(0));
  out.decodeMs = performance.now() - t1;
  const x = ab.getChannelData(0);
  let fmin = 0, fmax = 0;
  for (let i = 0; i < x.length; i++) { if (x[i] < fmin) fmin = x[i]; if (x[i] > fmax) fmax = x[i]; }
  out.decoded = {length: x.length, sampleRate: ab.sampleRate, channels: ab.numberOfChannels, duration: ab.duration,
                 min: fmin, max: fmax};
  const toI16 = (f) => {
    const o = new Int16Array(x.length);
    for (let i = 0; i < x.length; i++) { let v = f(x[i]); o[i] = v > 32767 ? 32767 : (v < -32768 ? -32768 : v); }
    return o;
  };
  const le = (o) => { const b = new ArrayBuffer(o.length * 2), d = new DataView(b);
    for (let i = 0; i < o.length; i++) d.setInt16(2 * i, o[i], true); return b; };
  const cmp = (o) => { let maxd = 0, nd = 0, firstIdx = -1; const m = Math.min(o.length, ref.length);
    for (let i = 0; i < m; i++) { const d = Math.abs(o[i] - ref[i]); if (d) { nd++; if (firstIdx < 0) firstIdx = i; if (d > maxd) maxd = d; } }
    return {maxAbsDiff: maxd, samplesDiffering: nd, firstDiffIndex: firstIdx, lengthDiff: o.length - ref.length}; };
  const req = toI16((s) => Math.round(s * 32768));
  out.requested = Object.assign({formula: 'Math.round(x*32768), clamped to [-32768, 32767]', sha256: await sha(le(req))}, cmp(req));
  const inv = toI16((s) => (s < 0 ? Math.round(s * 32768) : Math.round(s * 32767)));
  out.inverse = Object.assign({formula: 'x<0 ? Math.round(x*32768) : Math.round(x*32767)', sha256: await sha(le(inv))}, cmp(inv));
  // Model of Chromium's int16 -> float conversion (media/base/audio_sample_types.h, FixedSampleTypeTraits::To):
  // float(v) * (1.0f/32767) for v >= 0 and float(v) * (1.0f/32768) for v < 0, in float32 arithmetic.
  const invPos = Math.fround(1 / 32767), invNeg = Math.fround(1 / 32768);
  let notModel = 0, predictedReqDiffs = 0;
  for (let i = 0; i < Math.min(x.length, ref.length); i++) {
    const p = Math.fround(ref[i] * (ref[i] < 0 ? invNeg : invPos));
    if (p !== x[i]) notModel++;
    let q = Math.round(p * 32768); q = q > 32767 ? 32767 : (q < -32768 ? -32768 : q);
    if (q !== ref[i]) predictedReqDiffs++;
  }
  out.chromiumModel = {formula: 'float32(v)*float32(1/32767) if v >= 0 else float32(v)*float32(1/32768)',
                       samplesNotMatchingModel: notModel, predictedDiffsUnderRequested: predictedReqDiffs};
  return out;
}
"""

# X2 control: start a job and call play() on load, without any user activation.
X2A_INIT_JS = r"""
window.addEventListener('load', async () => {
  const ua = () => (navigator.userActivation ? {isActive: navigator.userActivation.isActive,
                                                hasBeenActive: navigator.userActivation.hasBeenActive} : null);
  const done = (v) => { (window.__pmrTest || (window.__pmrTest = {})).noActivation = v; };
  try {
    const tok = new URLSearchParams(location.search).get('t');
    const h = {'X-PMR-Token': tok, 'Content-Type': 'application/json'};
    const j = await (await fetch('/api/play', {method: 'POST', headers: h, body: JSON.stringify({track: 0, mode: 'progressive'})})).json();
    const a = new Audio();
    a.src = '/api/jobs/' + j.id + '/audio.wav?t=' + encodeURIComponent(tok);
    const before = ua();
    let result;
    try { await a.play(); result = 'played'; } catch (e) { result = e.name; }
    a.pause(); a.removeAttribute('src'); a.load();
    await fetch('/api/jobs/' + j.id + '/cancel', {method: 'POST', headers: h});
    done({result, userActivationBefore: before});
  } catch (e) { done({result: 'error: ' + e}); }
});
"""

ROWS_JS = """() => [...document.querySelectorAll('#catalog tbody tr')].map((tr) => ({id: tr.id,
  cells: [...tr.cells].map((td) => td.textContent), buttons: [...tr.querySelectorAll('button')].map((b) => b.textContent)}))"""
WIRE_JS = "() => [...document.querySelectorAll('#wire tbody tr')].map((tr) => [...tr.cells].map((td) => td.textContent))"
STATS_JS = "() => [...document.querySelectorAll('#stats tbody tr')].map((tr) => [tr.cells[0].textContent, tr.cells[1].textContent])"
AUDIO_STATE_JS = """() => { const a = document.getElementById('audio'); return {src: a.getAttribute('src'),
  currentTime: a.currentTime, duration: a.duration, paused: a.paused, ended: a.ended, readyState: a.readyState,
  networkState: a.networkState, playbackRate: a.playbackRate, error: a.error ? {code: a.error.code, message: a.error.message} : null,
  buffered: [...Array(a.buffered.length).keys()].map((i) => [a.buffered.start(i), a.buffered.end(i)])}; }"""
UI_STATE_JS = """() => ({progress: document.getElementById('progress-text').textContent,
  now: document.getElementById('now').textContent, mode: document.getElementById('mode').value,
  cancelDisabled: document.getElementById('btn-cancel').disabled, toggleDisabled: document.getElementById('btn-toggle').disabled,
  toggleLabel: document.getElementById('btn-toggle').getAttribute('aria-label'),
  playState: document.getElementById('play-state').textContent})"""
LANDING_JS = """() => ({landingVisible: !document.getElementById('view-landing').hidden,
  playerHidden: document.getElementById('view-player').hidden, hash: location.hash, title: document.title,
  heading: document.querySelector('#view-landing h1').textContent.trim(),
  cta: document.getElementById('btn-start').textContent.trim()})"""
METRICS_JS = """() => [...document.querySelectorAll('#metrics .metric')].map((m) => ({
  label: m.querySelector('.m-label').textContent, value: m.querySelector('.m-value').textContent,
  pending: m.querySelector('.m-value').classList.contains('pending'), source: m.querySelector('.m-src').textContent}))"""


# --------------------------------------------------------------------------------------------
# helpers
def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(obj: Any) -> Any:
    """Remove local paths and credentials; preserve live handles for protocol checks."""
    return public_report(obj, root=ROOT, hide_handles=False)


def finite(obj: Any) -> Any:
    """JSON-safe copy: non-finite floats become None (the results file is strict JSON)."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj


def fmt_bytes_ptbr(n: int) -> str:
    """Same byte formatting as the UI (pt-BR decimal comma): MB with 2 decimals, kB with 1, else B."""
    if n >= 1e6:
        return f"{n / 1e6:.2f} MB".replace(".", ",")
    if n >= 1e3:
        return f"{n / 1e3:.1f} kB".replace(".", ",")
    return f"{n} B"


def relpath(p: str | Path | None) -> str | None:
    if p is None:
        return None
    try:
        return os.path.relpath(os.path.realpath(p), os.path.realpath(ROOT))
    except ValueError:
        return str(p)


def load_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def own_browser_cmdline() -> list[str] | None:
    """argv of the headless-shell main process started by *this* runner (walks our own children only)."""
    def children(pid: int) -> list[int]:
        out: list[int] = []
        try:
            for tid in os.listdir(f"/proc/{pid}/task"):
                with open(f"/proc/{pid}/task/{tid}/children", encoding="ascii") as fh:
                    out += [int(x) for x in fh.read().split()]
        except OSError:
            pass
        return out

    todo, seen = children(os.getpid()), set()
    while todo:
        pid = todo.pop()
        if pid in seen:
            continue
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if argv and argv[0].endswith("chrome-headless-shell") and not any(a.startswith("--type=") for a in argv):
            return argv
        todo += children(pid)
    return None


def block_schedule(total: int, first_batch: int, batch: int, max_batch: int) -> list[tuple[int, int]]:
    """Same schedule as pmr.client.pirclient.PirHttpClient.retrieve (re-derived, not imported)."""
    batch, first_batch = max(1, min(batch, max_batch)), max(1, min(first_batch, max_batch))
    out, j = [], 0
    while j < total:
        k = min(first_batch if j == 0 else batch, total - j)
        out.append((j, k))
        j += k
    return out


def track_leaks(path: str, track: dict[str, Any]) -> list[str]:
    """Ways in which a wire path could name ``track`` (index, id or title)."""
    problems = []
    dec = urllib.parse.unquote_plus(path)
    p, _, q = dec.partition("?")
    segs = [s for s in p.split("/") if s]
    for s in segs:
        if s == str(track["index"]):
            problems.append(f"path element equals track index {track['index']}")
        if s.lower() == str(track["id"]).lower():
            problems.append(f"path element equals track id {track['id']}")
        if s.lower() == str(track["title"]).lower():
            problems.append("path element equals track title")
    if str(track["title"]).lower() in dec.lower():
        problems.append("path contains track title")
    if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(str(track['id']))}(?![A-Za-z0-9_-])", dec, re.I):
        problems.append(f"path contains track id {track['id']} as a token")
    for kv in [x for x in q.split("&") if x]:
        k, _, v = kv.partition("=")
        if k not in ("start", "count"):
            problems.append(f"unexpected query parameter {k!r}")
    return problems


class TestFailure(Exception):
    pass


class Result:
    def __init__(self, tid: str, name: str, required: bool):
        self.d: dict[str, Any] = {"id": tid, "name": name, "required": required, "status": "running",
                                  "checks": [], "measurements": {}, "notes": []}

    def check(self, ok: bool, what: str, **detail: Any) -> bool:
        item: dict[str, Any] = {"ok": bool(ok), "what": what}
        if detail:
            item["detail"] = detail
        self.d["checks"].append(item)
        print(f"    [{'ok' if ok else 'FAIL'}] {what}" + (f"  {json.dumps(finite(redact(detail)))[:300]}" if detail and not ok else ""),
              flush=True)
        return bool(ok)

    def require(self, ok: bool, what: str, **detail: Any) -> None:
        if not self.check(ok, what, **detail):
            raise TestFailure(what)

    def measure(self, **kv: Any) -> None:
        self.d["measurements"].update(kv)

    def note(self, text: str) -> None:
        self.d["notes"].append(text)


class ClientAPI:
    """Direct, read-mostly access to the trusted client's UI API (observation only)."""

    def __init__(self, ready: dict[str, Any]):
        self.port = int(ready["listen"]["port"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = ready["token"]
        self.url = ready["url"]

    def raw(self, method: str, path: str, body: Any = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-PMR-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def get(self, path: str) -> Any:
        status, raw = self.raw("GET", path)
        if status != 200:
            raise RuntimeError(f"GET {path}: HTTP {status}: {raw[:200]!r}")
        return json.loads(raw)  # Python's json accepts NaN (the browser's JSON.parse does not)

    def job(self, jid: str) -> dict[str, Any]:
        return self.get(f"/api/jobs/{jid}")

    def wait_job(self, jid: str, pred: Callable[[dict[str, Any]], bool], timeout: float,
                 interval: float = 0.1) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        s = self.job(jid)
        while not pred(s):
            if time.monotonic() > deadline:
                raise TestFailure(f"job {jid} did not reach the expected state within {timeout} s (last: {s['state']})")
            time.sleep(interval)
            s = self.job(jid)
        return s


class JobPoller(threading.Thread):
    """Polls GET /api/jobs/<id> (urllib, not the browser) and records (t_send, t_recv, state)."""

    def __init__(self, api: ClientAPI, jid: str, interval: float = 0.04, linger: float = 1.0):
        super().__init__(daemon=True, name=f"poll-{jid}")
        self.api, self.jid, self.interval, self.linger = api, jid, interval, linger
        self.obs: list[tuple[float, float, str, int]] = []
        self.stop_evt = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        done_at = None
        try:
            while not self.stop_evt.is_set():
                t0 = time.time()
                s = self.api.job(self.jid)
                t1 = time.time()
                self.obs.append((t0, t1, s["state"], s["blocks_done"]))
                if s["state"] != "running" and done_at is None:
                    done_at = t1
                if done_at is not None and t1 - done_at > self.linger:
                    break
                time.sleep(self.interval)
        except Exception as exc:  # recorded, checked by the test
            self.error = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------------------------
class Suite:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.logs_dir = ROOT / args.logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.results_path = ROOT / args.results
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        self.mini_ready, self.mini_pir = load_json(args.mini_client), load_json(args.mini_pir)
        self.mini_manifest = load_json(args.mini_manifest)
        self.mini = ClientAPI(self.mini_ready)
        self.full = None
        if args.full_client and args.full_pir:
            self.full_ready, self.full_pir = load_json(args.full_client), load_json(args.full_pir)
            self.full_manifest = load_json(args.full_manifest)
            self.full = ClientAPI(self.full_ready)
        self.rate = float(args.playback_rate)
        tracks = self.mini_manifest["tracks"]
        for opt, val, n in (("--track", args.track, len(tracks)), ("--complete-track", args.complete_track, len(tracks)),
                            ("--cancel-track", args.cancel_track,
                             len((self.full_manifest if self.full else self.mini_manifest)["tracks"]))):
            if val is not None and not 0 <= val < n:
                raise ValueError(f"{opt} {val} is outside 0..{n - 1}")
        by_len = sorted(tracks, key=lambda t: (t["duration_s"], t["index"]))
        self.t2_track = args.track if args.track is not None else by_len[0]["index"]
        self.t4_track = args.complete_track if args.complete_track is not None else \
            next(t["index"] for t in by_len if t["index"] != self.t2_track)
        self.results: list[Result] = []
        self.extras: list[Result] = []
        self.page_logs: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.shared: dict[str, Any] = {}
        self.landing: dict[str, Any] = {}  # landing-screen state seen by open_ui before "Começar a ouvir", per page
        self.doc: dict[str, Any] = {"schema": SCHEMA, "started_utc": utcnow()}
        self.t_start = time.perf_counter()

    # ---------------------------------------------------------------- browser plumbing
    def launch(self, pw, args: list[str]):
        return pw.chromium.launch(headless=True, args=args)

    def new_page(self, browser, tag: str, origin: str, viewport: tuple[int, int] = (1280, 1000)):
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]}, locale="pt-BR",
                                  service_workers="block", accept_downloads=False)
        ctx.add_init_script(INIT_JS)
        page = ctx.new_page()

        def on_console(m):
            self.page_logs.append({"t": utcnow(), "page": tag, "kind": f"console.{m.type}", "text": redact(m.text)})

        def on_pageerror(e):
            self.page_logs.append({"t": utcnow(), "page": tag, "kind": "pageerror", "text": redact(str(e))})

        pending: dict[Any, dict[str, Any]] = {}  # keyed by the Request object (identity)

        def on_request(r):
            entry = {"t": time.time(), "page": tag, "method": r.method, "url": redact(r.url),
                     "resource": r.resource_type, "range": r.headers.get("range"),
                     "loopback_origin": r.url.startswith(origin + "/")}
            pending[r] = entry
            self.requests.append(entry)

        def on_done(r, failed: bool = False):
            entry = pending.pop(r, None)
            try:
                start = r.timing.get("startTime")
            except PwError:
                start = None
            if entry is not None and start and start > 0:
                entry["browser_start_epoch"] = start / 1000.0  # browser clock; "t" is the harness receive time
            if failed:
                self.page_logs.append({"t": utcnow(), "page": tag, "kind": "requestfailed",
                                       "text": f"{r.method} {redact(r.url)}: {r.failure}"})

        def on_failed(r):
            on_done(r, failed=True)

        page.on("console", on_console)
        page.on("pageerror", on_pageerror)
        page.on("request", on_request)
        page.on("requestfailed", on_failed)
        page.on("requestfinished", on_done)
        return ctx, page

    def open_ui(self, browser, api: ClientAPI, tag: str):
        """Open the printed URL (landing screen), click "Começar a ouvir" and wait for the catalog."""
        ctx, page = self.new_page(browser, tag, api.base)
        page.goto(api.url, wait_until="load")
        self.landing[tag] = page.evaluate(LANDING_JS)
        page.click("#btn-start")
        page.wait_for_function("() => !document.getElementById('view-player').hidden && location.hash === '#ouvir'",
                               timeout=10000)
        page.wait_for_function("() => document.querySelectorAll('#catalog tbody tr').length > 0", timeout=20000)
        self.landing[tag]["after_cta"] = page.evaluate(LANDING_JS)
        return ctx, page

    def page_errors(self, tag: str, ignore: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self.page_logs if e["page"] == tag and e["kind"] in ("pageerror", "console.error")
                and not (ignore and ignore in e["text"])]

    def offsite_requests(self, tag: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["page"] == tag and not r["loopback_origin"]
                and not r["url"].startswith(("data:", "blob:", "about:"))]

    @staticmethod
    def click_track(page, index: int, with_timing: bool = False):
        """Click the row's "Ouvir" button; return status and JSON of the POST /api/play response."""
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.split("?")[0].endswith("/api/play"),
                                  timeout=20000) as info:
            page.locator(f"#row-{index} button", has_text="Ouvir").click()
        resp = info.value
        if with_timing:  # browser wall clock: startTime (epoch ms) + phase offsets (ms)
            return resp.status, resp.json(), resp.request.timing
        return resp.status, resp.json()

    @staticmethod
    def rec(page) -> dict[str, Any]:
        return page.evaluate("() => window.__pmrTest")

    @staticmethod
    def first(events: list[dict[str, Any]], name: str, after: float = -1.0) -> dict[str, Any] | None:
        return next((e for e in events if e["ev"] == name and e["t"] > after), None)

    def clock_offset(self, page) -> dict[str, float]:
        """Bracket the page's epoch clock (timeOrigin + now) with time.time()."""
        best = None
        for _ in range(5):
            a = time.time()
            p = page.evaluate("() => performance.timeOrigin + performance.now()") / 1000.0
            b = time.time()
            if best is None or b - a < best[1] - best[0]:
                best = (a, b, p)
        a, b, p = best
        return {"page_minus_python_s": p - (a + b) / 2, "uncertainty_s": (b - a) / 2}

    def run(self, tid: str, name: str, fn: Callable[[Result], None], required: bool = True) -> Result:
        r = Result(tid, name, required)
        (self.results if required else self.extras).append(r)
        r.d["started_utc"] = utcnow()
        t0 = time.perf_counter()
        print(f"== {tid} {name}", flush=True)
        try:
            fn(r)
            if r.d["status"] == "running":
                r.d["status"] = "pass" if all(c["ok"] for c in r.d["checks"]) else "fail"
        except TestFailure as exc:
            r.d["status"], r.d["reason"] = "fail", str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded as an error result
            r.d["status"], r.d["reason"] = "error", f"{type(exc).__name__}: {exc}"
            r.d["traceback"] = redact(traceback.format_exc()[-4000:])
        finally:
            r.d["finished_utc"] = utcnow()
            r.d["duration_s"] = round(time.perf_counter() - t0, 3)
        print(f"   -> {r.d['status']}" + (f": {r.d.get('reason')}" if r.d.get("reason") else ""), flush=True)
        return r

    def screenshot(self, page, name: str) -> str:
        path = self.logs_dir / name
        page.screenshot(path=str(path), full_page=True)
        return relpath(path)

    # ---------------------------------------------------------------- T1
    def t1_ui_load(self, r: Result) -> None:
        page = self.shared["mini_page"]
        land = self.landing.get("mini", {})
        r.check(land.get("landingVisible") is True and land.get("playerHidden") is True,
                "the printed URL opens the landing screen (player hidden)", landing=land)
        r.check(land.get("heading") == "Private Music Listening", "landing shows the product title", heading=land.get("heading"))
        r.check(land.get("cta") == "Começar a ouvir", "landing has the 'Começar a ouvir' action", cta=land.get("cta"))
        after = land.get("after_cta", {})
        r.check(after.get("hash") == "#ouvir" and after.get("landingVisible") is False and after.get("playerHidden") is False,
                "'Começar a ouvir' opens the listening screen (#ouvir)", after=after)
        rows = page.evaluate(ROWS_JS)
        tracks = self.mini_manifest["tracks"]
        r.require(len(rows) == 10, "catalog renders 10 rows", rows=len(rows))
        r.check([row["id"] for row in rows] == [f"row-{i}" for i in range(10)], "row ids are row-0..row-9",
                ids=[row["id"] for row in rows])
        r.check(all(row["buttons"] == ["Ouvir"] for row in rows), "every row has one 'Ouvir' button")
        r.check([row["cells"][1] for row in rows] == [t["title"] for t in tracks], "row titles match the manifest")
        proto = page.text_content("#proto") or ""
        b, lay = self.mini_pir["backend"], self.mini_pir["layout"]
        p = b["params"]
        expect_bfv = (f"BFV N={p['poly_modulus_degree']}, t={p['plain_modulus']}, "
                      f"q={'+'.join(str(x) for x in p['coeff_modulus_bits'])} bits ({p['security_level']})")
        r.check(bool(re.match(r"^BFV N=\d+, t=\d+, q=\d+(\+\d+)* bits \(\w+\)", proto)), "#proto shows BFV parameters",
                proto=proto)
        r.check(proto.startswith(expect_bfv), "#proto BFV values equal the PIR server's /v1/pir/params",
                expected=expect_bfv, proto=proto)
        r.check(f"{lay['num_records']} faixas × {lay['num_blocks']} blocos de {lay['block_bytes']} B" in proto,
                "#proto shows the public layout (records × blocks × block size)")
        r.check(b["label"] in proto, "#proto shows the backend label", label=b["label"])
        errs = self.page_errors("mini")
        r.check(not errs, "no page errors or console errors", errors=errs)
        r.measure(proto=proto, rows=[row["cells"][:4] for row in rows], title=page.title())

    # ---------------------------------------------------------------- T2
    def t2_progressive(self, r: Result) -> None:
        page, api = self.shared["mini_page"], self.mini
        idx = self.t2_track
        t = self.mini_manifest["tracks"][idx]
        rate = self.rate
        r.measure(track={"index": idx, "id": t["id"], "title": t["title"], "duration_s": t["duration_s"],
                         "pcm_bytes": t["pcm_bytes"]}, playback_rate=rate,
                  playback_rate_note="1.0 = real time" if rate == 1.0 else
                  "audio.defaultPlaybackRate/playbackRate set by the test before the click")
        page.select_option("#mode", "progressive")
        if rate != 1.0:
            page.evaluate("(x) => { const a = document.getElementById('audio'); a.defaultPlaybackRate = x; a.playbackRate = x; }",
                          rate)
        status, job = self.click_track(page, idx)
        r.require(status == 201 and job.get("mode") == "progressive", "POST /api/play accepted (progressive)",
                  status=status, mode=job.get("mode"))
        jid = job["id"]
        self.shared["t2_job"] = jid
        page.wait_for_function("() => window.__pmrTest.media.some((e) => e.ev === 'playing')", timeout=20000)
        r.check(True, "audio element fired 'playing'")
        src = page.get_attribute("#audio", "src") or ""
        r.check(src == f"/api/jobs/{jid}/audio.wav?t={urllib.parse.quote(api.token)}",
                "audio src is the client's token-protected WAV stream for this job", src=redact(src))
        page.wait_for_function("() => document.getElementById('audio').currentTime > 2",
                               timeout=int((2.0 / rate + 20) * 1000))
        ct2 = page.evaluate("() => [performance.now(), document.getElementById('audio').currentTime]")
        r.check(True, "currentTime advanced beyond 2 s")
        page.wait_for_function("() => document.getElementById('audio').ended",
                               timeout=int((t["duration_s"] / rate + 40) * 1000))
        r.check(True, "audio element reached 'ended'")
        s = api.wait_job(jid, lambda s: s["state"] != "running" and s.get("playback", {}).get("ended") is True, 10)
        rec, audio = self.rec(page), page.evaluate(AUDIO_STATE_JS)
        click = next((c for c in reversed(rec["clicks"]) if c["row"] == f"row-{idx}"), None)
        ev = rec["media"]
        click_t = click["t"] if click else None
        playing = self.first(ev, "playing", click_t or -1)
        ended = self.first(ev, "ended", click_t or -1)
        waits = [e for e in ev if e["ev"] == "waiting" and playing and playing["t"] < e["t"] and
                 (ended is None or e["t"] < ended["t"])]
        errors = [e for e in ev if e["ev"] == "error"]
        pb = s.get("playback") or {}
        res = s.get("result") or {}
        r.check(s["state"] == "complete", "job state is 'complete'", state=s["state"])
        r.check(s["blocks_done"] == s["blocks_total"] == self.mini_pir["layout"]["num_blocks"],
                "all padded catalog blocks retrieved (policy pad)", blocks_done=s["blocks_done"],
                blocks_total=s["blocks_total"])
        r.check(s["pcm_done"] == s["pcm_total"] == t["pcm_bytes"], "PCM bytes retrieved equal the manifest",
                pcm_done=s["pcm_done"], pcm_total=s["pcm_total"], manifest=t["pcm_bytes"])
        r.check(isinstance(pb.get("startup_s"), (int, float)) and pb["startup_s"] > 0,
                "UI reported playback.startup_s", playback=pb)
        r.check(pb.get("ended") is True, "UI reported playback.ended = true to the client")
        r.check(isinstance(pb.get("stalls"), int), "UI reported playback.stalls", stalls=pb.get("stalls"))
        r.check(not errors, "no media 'error' events", errors=errors)
        r.check(abs(audio["duration"] - t["duration_s"]) < 0.01, "media duration equals the track duration",
                duration=audio["duration"], expected=t["duration_s"])
        r.check(abs(audio["currentTime"] - t["duration_s"]) < 0.05, "playback position at the end equals the duration",
                currentTime=audio["currentTime"])
        r.check(click is not None and click["trusted"], "the 'Ouvir' click was a trusted input event")
        errs = self.page_errors("mini")
        r.check(not errs, "no page errors or console errors", errors=errs)
        r.check(not self.offsite_requests("mini"), "every browser request went to the loopback client origin",
                offsite=self.offsite_requests("mini"))
        media_reqs = [q for q in self.requests if q["page"] == "mini" and f"/api/jobs/{jid}/audio.wav" in q["url"]]
        r.measure(
            job_id=jid,
            first_block_s=s.get("first_block_s"),
            playback=pb,
            result_timings_ms=res.get("timings_ms"), result_bytes=res.get("bytes"),
            server_compute_ms=res.get("server_compute_ms"), server_total_ms=res.get("server_total_ms"),
            job_elapsed_s=s.get("elapsed_s"), blocks=[s["blocks_done"], s["blocks_total"]],
            harness=dict(
                click_to_playing_s=(playing["t"] - click_t) / 1000 if playing and click_t is not None else None,
                click_to_currentTime_gt_2s_s=(ct2[0] - click_t) / 1000 if click_t is not None else None,
                playing_to_ended_s=(ended["t"] - playing["t"]) / 1000 if playing and ended else None,
                expected_playing_to_ended_s=t["duration_s"] / rate,
                waiting_events_after_start=len(waits),
                media_requests=[{"method": q["method"], "range": q["range"]} for q in media_reqs],
            ),
            media_events=[{k: e[k] for k in ("ev", "t", "ct", "rs", "ns")} for e in ev if click_t is None or e["t"] >= click_t],
            final_audio_state=redact(audio),
        )
        self.shared["t2_status"] = s

    # ---------------------------------------------------------------- T3
    def t3_hash(self, r: Result) -> None:
        page = self.shared["mini_page"]
        idx = self.t2_track
        t = self.mini_manifest["tracks"][idx]
        src = page.get_attribute("#audio", "src")
        r.require(bool(src), "audio element has the job's audio.wav URL")
        out = page.evaluate(HASH_JS, {"src": src, "n": int(t["num_samples"]), "rate": int(self.mini_manifest["sample_rate"])})
        exp = t["sha256_pcm"]
        r.check(out.get("secureContext") is True, "page is a secure context (crypto.subtle available on 127.0.0.1)")
        r.check(out["status"] == 200 and (out["contentType"] or "").startswith("audio/wav"), "in-page fetch of audio.wav: 200 audio/wav",
                status=out["status"], contentType=out["contentType"])
        r.check(out.get("riff") == "RIFF" and out.get("wave") == "WAVE" and out.get("fmt") ==
                {"format": 1, "channels": 1, "sampleRate": 16000, "bits": 16}, "WAV header: PCM, mono, 16 kHz, 16-bit",
                fmt=out.get("fmt"))
        r.check(out.get("dataBytes") == t["pcm_bytes"], "WAV data chunk length equals manifest pcm_bytes",
                dataBytes=out.get("dataBytes"))
        r.check(out.get("rawPcmSha256") == exp, "SHA-256 of the PCM bytes received by the browser equals manifest sha256_pcm",
                got=out.get("rawPcmSha256"), expected=exp)
        dec = out.get("decoded") or {}
        r.check(dec.get("length") == t["num_samples"] and dec.get("sampleRate") == 16000 and dec.get("channels") == 1,
                "decodeAudioData output: same sample count, 16 kHz, mono (no resampling)", decoded=dec)
        req, inv = out["requested"], out["inverse"]
        req_match, inv_match = req["sha256"] == exp, inv["sha256"] == exp
        r.measure(requested_conversion_bit_exact=req_match, inverse_conversion_bit_exact=inv_match)
        r.check(req_match or (req["maxAbsDiff"] <= 1 and inv_match),
                "decoded samples -> int16: bit-exact, or within 1 LSB and bit-exact under the decoder's scaling",
                requested=req, inverse=inv)
        model = out["chromiumModel"]
        r.check(model["samplesNotMatchingModel"] == 0,
                "every decoded float equals Chromium's int16->float conversion of the delivered PCM sample",
                model=model)
        if not req_match:
            r.check(req["samplesDiffering"] == model["predictedDiffsUnderRequested"],
                    "the samples that differ under Math.round(x*32768) are exactly those the model predicts",
                    differing=req["samplesDiffering"], predicted=model["predictedDiffsUnderRequested"])
            r.note("Chromium converts int16 to float asymmetrically: float(v)*float(1/32768) for v < 0 and "
                   "float(v)*float(1/32767) for v >= 0 (media/base/audio_sample_types.h, FixedSampleTypeTraits::To). "
                   "Math.round(x*32768) therefore returns v+1 for positive samples near or above 16384 "
                   f"({req['samplesDiffering']} of {out['refSamples']} samples here, all off by exactly 1); "
                   "the inverse x<0 ? round(x*32768) : round(x*32767) recovers every sample, so the SHA-256 matches.")
        r.measure(track={"index": idx, "id": t["id"]}, manifest_sha256_pcm=exp, browser=out)

    # ---------------------------------------------------------------- T6
    def t6_wire(self, r: Result) -> None:
        page, api = self.shared["mini_page"], self.mini
        info = api.get("/api/catalog")
        cl = info["client"]
        lay = info["layout"]
        sched = block_schedule(lay["num_blocks"], cl["first_batch"], cl["batch"],
                               self.mini_pir["limits"]["max_blocks_per_request"])
        last = f"blocks?start={sched[-1][0]}&count={sched[-1][1]}"
        page.wait_for_function(
            "(last) => { const rows = [...document.querySelectorAll('#wire tbody tr')];"
            " return rows.length > 0 && rows[0].cells[0].textContent === 'DELETE' &&"
            " rows.some((r) => r.cells[1].textContent.endsWith(last)); }", arg=last, timeout=15000)
        rows = page.evaluate(WIRE_JS)
        r.require(len(rows) > 0, "#wire table has rows", rows=len(rows))
        t = self.mini_manifest["tracks"][self.t2_track]
        bad_shape = [x for x in rows if not any(x[0] == m and rx.match(x[1]) for m, rx in WIRE_WHITELIST)]
        r.check(not bad_shape, "every #wire path is a whitelisted PIR endpoint with random tokens / block indices",
                offending=bad_shape)
        leaks = {x[1]: track_leaks(x[1], t) for x in rows if track_leaks(x[1], t)}
        r.check(not leaks, "no #wire path contains the selected track's title, id or index as a path element",
                track={"index": t["index"], "id": t["id"], "title": t["title"]}, leaks=leaks)
        any_leaks = {x[1]: p for x in rows for tt in self.mini_manifest["tracks"] for p in [track_leaks(x[1], tt)] if p}
        r.check(not any_leaks, "no #wire path names any catalog track", leaks=any_leaks)
        numeric = [x[1] for x in rows if any(s.isdigit() for s in x[1].split("?")[0].split("/"))]
        r.check(not numeric, "no #wire path has a numeric path element", paths=numeric)
        # rows are newest first; the latest retrieval is the run between the newest POST .../queries and DELETE
        newest_post = next((i for i, x in enumerate(rows) if x[0] == "POST" and x[1].endswith("/queries")), None)
        r.require(newest_post is not None, "#wire shows the query upload of the latest retrieval")
        latest = list(reversed(rows[:newest_post + 1]))
        blocks = [tuple(int(v) for v in re.search(r"start=(\d+)&count=(\d+)", x[1]).groups()) for x in latest
                  if "/blocks?" in x[1]]
        r.check(blocks == sched, "block requests of the latest retrieval follow the public, track-independent schedule",
                got=blocks, expected=sched)
        qtok = {re.match(rf"^/v1/pir/queries/({PIR_TOKEN})", x[1]).group(1) for x in latest if x[1].startswith("/v1/pir/queries/")}
        r.check(len(qtok) == 1, "one fresh query token for all block requests of the retrieval", tokens=len(qtok))
        r.check(all(x[3] in ("200", "201") for x in latest), "all requests of the retrieval succeeded",
                statuses=[x[3] for x in latest])
        r.measure(rows=rows, expected_schedule=sched, policy=cl["policy"], batch=cl["batch"], first_batch=cl["first_batch"])

    # ---------------------------------------------------------------- T7 (screenshot part)
    def t7_screenshot(self, r: Result) -> None:
        page = self.shared["mini_page"]
        stats = page.evaluate(STATS_JS)
        ui = page.evaluate(UI_STATE_JS)
        metrics = page.evaluate(METRICS_JS)
        path = self.screenshot(page, "ui_after_playback.png")
        size = (ROOT / path).stat().st_size
        r.check(size > 10000, "screenshot written", path=path, bytes=size)
        r.check(ui["progress"].startswith("concluída"), "progress line shows the completed retrieval", progress=ui["progress"])
        # the metrics shown to the user come from this retrieval (no placeholders left after completion)
        job = self.shared.get("t2_job")
        s = self.mini.job(job) if job else None
        by_label = {m["label"]: m for m in metrics}
        for label in ("Primeiro áudio", "Início da reprodução", "Recuperação completa", "Velocidade", "Dados recebidos",
                      "Interrupções"):
            m = by_label.get(label)
            r.check(m is not None and not m["pending"] and m["value"] != "—", f"metric '{label}' shows a value", metric=m)
        r.check(all(m["source"] in ("medido no cliente", "medido no navegador", "calculado") for m in metrics),
                "every metric names its source (measured in the client or browser, or calculated)", metrics=metrics)
        if s is not None:
            blocks = dict(stats).get("Blocos recebidos")
            r.check(blocks == f"{s['blocks_done']} de {s['blocks_total']}", "'Blocos recebidos' matches the client's job status",
                    shown=blocks, job={k: s[k] for k in ("blocks_done", "blocks_total", "state")})
            rx = (s.get("result") or {}).get("bytes", {}).get("wire_rx")
            r.check(rx is not None and by_label.get("Dados recebidos", {}).get("value") == fmt_bytes_ptbr(rx),
                    "'Dados recebidos' equals the client's measured wire_rx", shown=by_label.get("Dados recebidos"), wire_rx=rx)
        r.measure(screenshot=path, screenshot_bytes=size, stats_table=stats, ui_state=ui, metrics=metrics)

    # ---------------------------------------------------------------- T8
    def t8_ui_states(self, r: Result) -> None:
        """Error state without the token, navigation both ways, and the layout on a small screen."""
        api, browser = self.mini, self.shared["browser"]
        ctx, page = self.new_page(browser, "ui-no-token", api.base)
        try:
            page.goto(api.base + "/#ouvir", wait_until="load")
            page.wait_for_function("() => !document.getElementById('app-error').hidden", timeout=10000)
            banner = page.text_content("#app-error") or ""
            st = page.evaluate("() => ({cls: document.getElementById('catalog-status').className,"
                               " rows: document.querySelectorAll('#catalog tbody tr').length})")
            r.check(banner.startswith("Acesso negado") and "código de acesso" in banner,
                    "without the token, the player shows an access error", banner=banner)
            r.check(st["cls"] == "muted error" and st["rows"] == 0, "the catalog shows the error and no tracks", state=st)
            page.click("#nav-sobre")
            page.wait_for_function("() => !document.getElementById('view-landing').hidden", timeout=5000)
            page.click("#nav-ouvir")
            page.wait_for_function("() => !document.getElementById('view-player').hidden", timeout=5000)
            r.check(True, "header navigation switches between 'Como funciona' and 'Ouvir'")
        finally:
            ctx.close()
        ctx, page = self.new_page(browser, "ui-small", api.base, viewport=(390, 844))
        try:
            page.goto(api.url, wait_until="load")
            overflow_landing = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            page.click("#btn-start")
            page.wait_for_function("() => !document.getElementById('view-player').hidden"
                                   " && document.querySelectorAll('#catalog tbody tr').length > 0", timeout=20000)
            overflow_player = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
            r.check(overflow_landing <= 0 and overflow_player <= 0, "no horizontal overflow at 390 px (landing and player)",
                    landing=overflow_landing, player=overflow_player)
            btn = page.locator("#row-0 button")
            r.check(btn.is_visible(), "the track buttons are visible at 390 px")
            page.keyboard.press("Tab")
            focused = page.evaluate("() => document.activeElement ? document.activeElement.className + '|' +"
                                    " getComputedStyle(document.activeElement).outlineStyle : null")
            r.check(focused is not None and focused.endswith("|solid"), "keyboard focus is visible (solid outline)",
                    focused=focused)
            r.measure(small_screen_screenshot=self.screenshot(page, "ui_small_screen.png"))
        finally:
            ctx.close()
        errs = [e for e in self.page_errors("ui-small")]
        r.check(not errs, "no page errors or console errors on the small screen", errors=errs)

    # ---------------------------------------------------------------- T4
    def t4_complete(self, r: Result) -> None:
        api, browser = self.mini, self.shared["browser"]
        ctx, page = self.open_ui(browser, api, "mini-complete")
        try:
            idx = self.t4_track
            t = self.mini_manifest["tracks"][idx]
            r.measure(track={"index": idx, "id": t["id"], "title": t["title"], "duration_s": t["duration_s"]},
                      clock=self.clock_offset(page))
            page.select_option("#mode", "complete")
            r.require(page.evaluate("() => document.getElementById('mode').value") == "complete", "mode select set to 'complete'")
            r.check(page.get_attribute("#audio", "src") is None, "audio has no src before the click")
            status, job, play_timing = self.click_track(page, idx, with_timing=True)
            r.require(status == 201 and job.get("mode") == "complete", "POST /api/play accepted (complete)",
                      status=status, mode=job.get("mode"))
            jid = job["id"]
            poller = JobPoller(api, jid)
            poller.start()
            page.wait_for_function("() => document.getElementById('audio').hasAttribute('src')", timeout=60000)
            page.wait_for_function("() => window.__pmrTest.media.some((e) => e.ev === 'playing')", timeout=20000)
            page.wait_for_function("() => document.getElementById('audio').currentTime > 2",
                                   timeout=int((2.0 / self.rate + 20) * 1000))
            r.check(True, "playback started after the src was set ('playing', currentTime > 2 s)")
            # exercise the UI's play/pause toggle
            label_playing = page.get_attribute("#btn-toggle", "aria-label")
            r.check(label_playing == "Pausar", "while playing, the toggle offers 'Pausar'", label=label_playing)
            page.click("#btn-toggle")
            page.wait_for_function("() => document.getElementById('audio').paused", timeout=5000)
            ct_paused = page.evaluate("() => document.getElementById('audio').currentTime")
            page.wait_for_timeout(500)
            ct_paused2 = page.evaluate("() => document.getElementById('audio').currentTime")
            r.check(abs(ct_paused2 - ct_paused) < 1e-6, "#btn-toggle pauses playback", before=ct_paused, after=ct_paused2)
            label_paused = page.get_attribute("#btn-toggle", "aria-label")
            state_paused = page.text_content("#play-state")
            r.check(label_paused == "Tocar" and state_paused == "Pausada.", "paused: toggle offers 'Tocar', state says 'Pausada.'",
                    label=label_paused, state=state_paused)
            page.click("#btn-toggle")
            page.wait_for_function("(x) => document.getElementById('audio').currentTime > x + 0.3", arg=ct_paused, timeout=10000)
            r.check(True, "#btn-toggle resumes playback")
            page.click("#btn-toggle")
            poller.join(timeout=15)
            poller.stop_evt.set()
            r.check(poller.error is None, "job poller ran without errors", error=poller.error)
            rec = self.rec(page)
            s = api.wait_job(jid, lambda s: s["state"] != "running", 10)
            r.check(s["state"] == "complete", "job ended in state 'complete'", state=s["state"])
            click = next((c for c in reversed(rec["clicks"]) if c["row"] == f"row-{idx}"), None)
            r.require(click is not None, "the 'Ouvir' click was recorded in the page")
            src_set = next((m for m in rec["src"] if m["set"] and m["t"] > click["t"]), None)
            r.require(src_set is not None, "src attribute was set (MutationObserver)")
            # (1) page clock vs harness clock (both CLOCK_REALTIME based; offset measured)
            t_src = rec["timeOrigin"] / 1000.0 + src_set["t"] / 1000.0
            clk = r.d["measurements"]["clock"]
            margin = abs(clk["page_minus_python_s"]) + clk["uncertainty_s"] + 0.002
            running_after = [o for o in poller.obs if o[2] == "running" and o[0] > t_src + margin]
            r.check(not running_after, "no harness poll sent after the src was set saw state 'running'",
                    violations=running_after[:3])
            # (2) browser clock only: the job cannot have completed before POST /api/play started + finished_s
            fin = s.get("finished_s") if s.get("finished_s") is not None else s.get("elapsed_s")
            completion_lb = play_timing["startTime"] / 1000.0 + fin
            completion_ub = play_timing["startTime"] / 1000.0 + max(play_timing["responseEnd"], 0) / 1000.0 + fin
            r.check(t_src >= completion_lb - 0.005,
                    "src was set after the earliest possible completion time of the job (5 ms clock tolerance)",
                    src_set_epoch=t_src, completion_window_epoch=[completion_lb, completion_ub])
            # (3) the UI had rendered the 'complete' status object before attaching the audio
            r.check(src_set["progress"].startswith("concluída"), "UI progress line showed 'concluída' when the src was set",
                    progress=src_set["progress"])
            # (4) the audio element stayed idle until then
            before = [x for x in rec["samples"] if click["t"] < x["t"] < src_set["t"]]
            r.check(len(before) > 0 and all(not x["src"] and x["ns"] == 0 for x in before),
                    "audio element had no src and NETWORK_EMPTY until completion (50 ms samples)",
                    samples=len(before), violating=[x for x in before if x["src"] or x["ns"] != 0][:5])
            media_reqs_before = [q for q in self.requests if q["page"] == "mini-complete" and "/audio.wav" in q["url"]
                                 and q["t"] < t_src - margin]
            r.check(not media_reqs_before, "no audio.wav request before the src was set", requests=media_reqs_before)
            r.check(any(o[2] == "running" for o in poller.obs), "harness observed the retrieval in state 'running'",
                    observations=len(poller.obs))
            playing = self.first(rec["media"], "playing", src_set["t"])
            first_complete = next((o for o in poller.obs if o[2] == "complete"), None)
            running = [o for o in poller.obs if o[2] == "running"]
            errs = self.page_errors("mini-complete")
            r.check(not errs, "no page errors or console errors", errors=errs)
            r.check(not self.offsite_requests("mini-complete"), "every browser request went to the loopback client origin")
            res = s.get("result") or {}
            r.measure(
                job_id=jid, first_block_s=s.get("first_block_s"), finished_s=fin, playback=s.get("playback"),
                result_timings_ms=res.get("timings_ms"), result_bytes=res.get("bytes"),
                server_compute_ms=res.get("server_compute_ms"),
                harness=dict(
                    click_to_src_set_s=(src_set["t"] - click["t"]) / 1000,
                    src_set_to_playing_s=(playing["t"] - src_set["t"]) / 1000 if playing else None,
                    click_to_playing_s=(playing["t"] - click["t"]) / 1000 if playing else None,
                    completion_window_epoch=[completion_lb, completion_ub],
                    src_set_epoch=t_src,
                    src_set_after_completion_s=[t_src - completion_ub, t_src - completion_lb],
                    harness_last_running_poll_sent_epoch=running[-1][0] if running else None,
                    harness_first_complete_poll_received_epoch=first_complete[1] if first_complete else None,
                    poll_observations=len(poller.obs),
                ),
                src_mutations=rec["src"],
            )
            self.screenshot(page, "ui_complete_mode.png")
            self.shared["t4_job"] = jid
        finally:
            ctx.close()

    # ---------------------------------------------------------------- T6b
    def t6b_schedule(self, r: Result) -> None:
        wire = self.mini.get("/api/wire")["entries"]
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for e in wire:
            m = re.match(rf"^/v1/pir/queries/({PIR_TOKEN})", e["path"])
            if m:
                q = m.group(1)
                if q not in groups:
                    groups[q] = []
                    order.append(q)
                groups[q].append(f"{e['method']} " + e["path"].replace(q, "<q>"))
        complete = [q for q in order if groups[q] and groups[q][-1] == "DELETE /v1/pir/queries/<q>"]
        r.require(len(complete) >= 2, "/api/wire holds at least two complete retrievals (T2 and T4 tracks)",
                  retrievals=len(complete))
        a, b = groups[complete[-2]], groups[complete[-1]]
        r.check(a == b, "the PIR server saw the same request sequence for two different tracks",
                tracks=[self.t2_track, self.t4_track], first=a, second=b)
        uploads = [e for e in wire if e["method"] == "POST" and e["path"].endswith("/queries")][-2:]
        r.measure(sequence=a, query_upload_bytes=[e["request_bytes"] for e in uploads],
                  tracks=[self.t2_track, self.t4_track])
        r.note("Request/response byte counts vary slightly between queries (compressed ciphertexts); "
               "the sequence of methods, paths and block ranges is identical.")

    # ---------------------------------------------------------------- T5
    def t5_cancel(self, r: Result) -> None:
        api = self.full or self.mini
        manifest = self.full_manifest if self.full else self.mini_manifest
        r.measure(catalog="full" if self.full else "mini")
        if not self.full:
            r.note("no full-catalog services were given: cancel is attempted on the mini catalog (may finish first)")
        ctx, page = self.open_ui(self.shared["browser"], api, "cancel")
        keep = False
        try:
            r.require(page.locator("#catalog tbody tr").count() == len(manifest["tracks"]), "catalog rendered")
            page.select_option("#mode", "progressive")
            candidates = [self.args.cancel_track] if self.args.cancel_track is not None else \
                [t["index"] for t in sorted(manifest["tracks"], key=lambda t: -t["duration_s"])][:2]
            attempt_log = []
            for attempt, idx in enumerate(candidates, 1):
                status, job = self.click_track(page, idx)
                r.require(status == 201, "POST /api/play accepted", status=status)
                jid = job["id"]
                page.wait_for_function("(n) => window.__pmrTest.media.filter((e) => e.ev === 'playing').length >= n",
                                       arg=attempt, timeout=20000)
                page.wait_for_function("() => !document.getElementById('btn-cancel').disabled", timeout=5000)
                pre = api.job(jid)
                t_click = time.time()
                page.click("#btn-cancel")
                attempt_log.append({"track": idx, "job": jid, "state_before_click": pre["state"],
                                    "blocks_before_click": pre["blocks_done"], "blocks_total": pre["blocks_total"],
                                    "audio_s_available_before_click": pre["audio_s_available"]})
                if pre["state"] == "running":
                    break
                r.note(f"attempt {attempt}: retrieval finished before the cancel click; retrying")
            r.measure(attempts=attempt_log)
            r.require(attempt_log[-1]["state_before_click"] == "running", "retrieval was running when #btn-cancel was clicked",
                      attempts=attempt_log)
            page.wait_for_function("() => document.getElementById('progress-text').textContent.startsWith('cancelada')",
                                   timeout=15000)
            t_ui = time.time()
            s = api.wait_job(jid, lambda s: s["state"] != "running", 10)
            ui = page.evaluate(UI_STATE_JS)
            audio = page.evaluate(AUDIO_STATE_JS)
            r.check(s["state"] == "cancelled", "job state became 'cancelled'", state=s["state"])
            r.check(s["blocks_done"] < s["blocks_total"] and s["pcm_done"] < s["pcm_total"],
                    "retrieval stopped before the end", blocks=[s["blocks_done"], s["blocks_total"]],
                    pcm=[s["pcm_done"], s["pcm_total"]])
            r.check(ui["progress"].startswith("cancelada"), "UI progress line shows 'cancelada'", progress=ui["progress"])
            r.check(ui["cancelDisabled"], "#btn-cancel is disabled after the cancellation")
            res_blocks = ((s.get("result") or {}).get("bytes") or {}).get("blocks", 0)
            r.check(res_blocks < s["blocks_total"], "client result reports fewer blocks than the padded catalog",
                    result_blocks=res_blocks)
            errs = self.page_errors("cancel", ignore="ERR_CONTENT_LENGTH_MISMATCH")
            r.check(not errs, "no page errors or console errors (besides the truncated-stream error, see X3)", errors=errs)
            r.check(not self.offsite_requests("cancel"), "every browser request went to the loopback client origin")
            res = s.get("result") or {}
            r.measure(job_id=jid, track_index=attempt_log[-1]["track"], cancel_click_to_ui_cancelled_s=t_ui - t_click,
                      blocks_done=s["blocks_done"], blocks_total=s["blocks_total"],
                      audio_s_available=s["audio_s_available"], audio_s_total=s["audio_s_total"],
                      first_block_s=s.get("first_block_s"), result_timings_ms=res.get("timings_ms"),
                      result_bytes=res.get("bytes"), ui_state=ui, audio_state_at_cancel=redact(audio))
            r.measure(screenshot=self.screenshot(page, "ui_after_cancel.png"))
            self.shared.update(cancel_ctx=ctx, cancel_page=page, cancel_status=s, cancel_job=jid)
            keep = True
        finally:
            if not keep:
                ctx.close()

    # ---------------------------------------------------------------- X3
    def x3_after_cancel(self, r: Result) -> None:
        """What the media element does when the cancelled (truncated) stream runs out."""
        ctx, page = self.shared.pop("cancel_ctx", None), self.shared.get("cancel_page")
        if ctx is None:
            r.d["status"] = "skipped"
            return
        try:
            s, jid = self.shared["cancel_status"], self.shared["cancel_job"]
            avail = s["audio_s_available"]
            # Reach the end of the decrypted audio quickly (observation only).
            page.evaluate("() => { document.getElementById('audio').playbackRate = 16; }")
            last, still, deadline = None, 0, time.monotonic() + 20
            while time.monotonic() < deadline:
                st = page.evaluate(AUDIO_STATE_JS)
                if st["ended"] or st["error"]:
                    break
                still = still + 1 if last is not None and abs(st["currentTime"] - last) < 1e-6 else 0
                if still >= 4:  # position frozen for ~1 s
                    break
                last = st["currentTime"]
                page.wait_for_timeout(250)
            # page.wait_for_timeout keeps Playwright's event dispatch running (time.sleep would delay the
            # request events and therefore their harness timestamps).
            page.wait_for_timeout(3000)  # give the UI time to react (it polls once per second after the cancel)
            t_window = time.time()
            page.wait_for_timeout(2500)  # observation window: requests here mean the player/UI never settled
            st = page.evaluate(AUDIO_STATE_JS)
            ui = page.evaluate(UI_STATE_JS)
            rec = self.rec(page)
            t_cancel_page = max(c["t"] for c in rec["clicks"] if c["id"] == "btn-cancel")
            after = [{k: e[k] for k in ("ev", "t", "ct", "rs", "ns", "err")} for e in rec["media"] if e["t"] >= t_cancel_page]
            media_reqs = [q for q in self.requests if q["page"] == "cancel" and f"/api/jobs/{jid}/audio.wav" in q["url"]]
            mismatch = [e for e in self.page_logs if e["page"] == "cancel" and e["kind"] == "requestfailed"
                        and "ERR_CONTENT_LENGTH_MISMATCH" in e["text"]]
            polls_window = [q for q in self.requests if q["page"] == "cancel" and q["t"] >= t_window
                            and q["url"].split("?")[0].endswith(f"/api/jobs/{jid}")]
            # terminal: 'ended', a media error, or the UI itself stopped the player (src removed)
            terminal = st["ended"] or st["error"] is not None or st["src"] is None
            retries_window = [q for q in media_reqs if q["t"] >= t_window]
            r.measure(audio_s_available=avail, playbackRate_set_to=16, final_audio_state=redact(st), ui_state=ui,
                      media_events_after_cancel_click=after, audio_wav_requests=len(media_reqs),
                      observation_window_s=2.5, settle_before_window_s=3.0,
                      audio_wav_requests_in_window=len(retries_window), ui_status_polls_in_window=len(polls_window),
                      audio_wav_ranges=sorted({q["range"] for q in media_reqs if q["range"]}),
                      content_length_mismatch_failures=len(mismatch),
                      reached_terminal_media_state=terminal,
                      ui_playback_report=(self.full or self.mini).job(jid).get("playback"))
            r.d["status"] = "bug_reproduced" if (not terminal or retries_window or polls_window) else "not_reproduced"
            r.note("After a cancel the WAV header and the 206 Content-Length still announce the whole track. When "
                   "playback reaches the end of the decrypted audio, the client closes each Range request without "
                   "data; Chromium logs ERR_CONTENT_LENGTH_MISMATCH, retries the Range request, and the element "
                   "stays in 'waiting' (no 'ended'/'error'), so the UI keeps polling and posting playback forever.")
            self.screenshot(page, "x3_after_cancel_end_of_audio.png")
        finally:
            ctx.close()

    # ---------------------------------------------------------------- X1
    def x1_cancel_before_first_block(self, r: Result) -> None:
        api = self.full or self.mini
        browser = self.shared["browser"]
        ctx, page = self.open_ui(browser, api, "x1")
        try:
            page.select_option("#mode", "progressive")
            page.evaluate(ARM_AUTOCANCEL_JS)
            status, job = self.click_track(page, 0)
            jid = job["id"]
            s = api.wait_job(jid, lambda s: s["state"] != "running", 15)
            status_raw, raw = api.raw("GET", f"/api/jobs/{jid}")
            page.wait_for_timeout(4000)  # several UI poll periods (250 ms while running, 1 s afterwards)
            ui = page.evaluate(UI_STATE_JS)
            parse = page.evaluate("""async (u) => { const r = await fetch(u, {headers: {'X-PMR-Token': new URLSearchParams(location.search).get('t')}});
                const text = await r.text(); try { JSON.parse(text); return {ok: true, nan: text.includes('NaN')}; }
                catch (e) { return {ok: false, error: e.name + ': ' + e.message, nan: text.includes('NaN')}; } }""",
                                  f"/api/jobs/{jid}")
            rec = self.rec(page)
            polls = [q for q in self.requests if q["page"] == "x1" and q["url"].split("?")[0].endswith(f"/api/jobs/{jid}")]
            r.measure(job_id=jid, server_state=s["state"], blocks_done=s["blocks_done"], first_block_s=s.get("first_block_s"),
                      raw_status_contains_NaN=b"NaN" in raw, raw_status_excerpt=raw[raw.find(b'"timings_ms"'):][:160].decode(errors="replace"),
                      browser_json_parse=parse, ui_after_4s=ui, ui_job_status_requests=len(polls),
                      auto_cancel=rec.get("autoCancel"))
            if s["blocks_done"] > 0:
                r.d["status"] = "inconclusive"
                r.note("the first block was decrypted before the cancel arrived; the NaN path was not exercised")
                return
            bug = (s["state"] == "cancelled" and b"NaN" in raw and not parse["ok"] and not ui["progress"].startswith("cancelada"))
            r.d["status"] = "bug_reproduced" if bug else ("not_reproduced" if ui["progress"].startswith("cancelada") else "observed")
            r.note("Cancelling before the first block is decrypted makes RetrievalResult.timings['first_block_ms'] = NaN "
                   "(pirclient.py); json.dumps emits the bare token NaN, which the browser's JSON.parse rejects, so every "
                   "UI poll of /api/jobs/<id> fails and the page never shows 'cancelada'.")
            self.screenshot(page, "x1_cancel_before_first_block.png")
        finally:
            ctx.close()

    # ---------------------------------------------------------------- X2
    def x2_autoplay(self, r: Result) -> None:
        pw = self.shared["pw"]
        args = [a for a in self.launch_args if not a.startswith("--autoplay-policy")]
        r.measure(launch_args=args)
        b2 = self.launch(pw, args)
        try:
            api = self.mini
            out: dict[str, Any] = {}
            # (a) control: play() from a page that never received input. The attempt runs from an init
            # script on 'load'; the harness makes no CDP evaluate call before reading the result
            # (Playwright evaluates with userGesture=true, which would grant activation).
            ctx, page = self.new_page(b2, "x2a", api.base)
            try:
                ctx.add_init_script(X2A_INIT_JS)
                page.goto(api.url, wait_until="load")
                time.sleep(3.0)
                out["no_activation_play"] = page.evaluate("() => window.__pmrTest.noActivation || null")
            finally:
                ctx.close()
            # (b) progressive and (c) complete through real (trusted) clicks on "Ouvir"
            for mode in ("progressive", "complete"):
                ctx, page = self.open_ui(b2, api, f"x2-{mode}")
                try:
                    page.select_option("#mode", mode)
                    self.click_track(page, self.t4_track)
                    try:
                        page.wait_for_function("() => window.__pmrTest.media.some((e) => e.ev === 'playing') &&"
                                               " document.getElementById('audio').currentTime > 0.5", timeout=15000)
                        played = True
                    except PwError:
                        played = False
                    rec = self.rec(page)
                    click = next((c for c in reversed(rec["clicks"]) if c["row"]), None)
                    playing = self.first(rec["media"], "playing")
                    out[mode] = {"played": played,
                                 "click_to_playing_s": (playing["t"] - click["t"]) / 1000 if playing and click else None,
                                 "events": [e["ev"] for e in rec["media"]][:20],
                                 "console": [e["text"] for e in self.page_logs if e["page"] == f"x2-{mode}"][:5]}
                    page.evaluate("() => document.getElementById('audio').pause()")
                finally:
                    ctx.close()
            r.measure(**out)
            r.d["status"] = "observed"
            r.note("(a) shows whether the headless shell enforces the default autoplay policy; (b)/(c) show whether the "
                   "UI's play() calls succeed without the flag once the page has the user activation of the click.")
        finally:
            b2.close()

    # ---------------------------------------------------------------- environment
    def environment(self, browser) -> dict[str, Any]:
        env: dict[str, Any] = {"python": sys.version.split()[0], "platform": platform.platform()}
        try:
            env["glibc"] = os.confstr("CS_GNU_LIBC_VERSION")
        except (ValueError, OSError):
            pass
        try:
            for line in open("/etc/os-release", encoding="utf-8"):
                if line.startswith("PRETTY_NAME="):
                    env["os"] = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        try:
            env["cpu_model"] = next(line.split(":", 1)[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name"))
        except (OSError, StopIteration):
            pass
        env["cpu_count"] = os.cpu_count()
        env["loadavg_start"] = os.getloadavg()
        import playwright as pwpkg
        drv = Path(pwpkg.__file__).parent / "driver"
        env["playwright"] = {"python_package": importlib.metadata.version("playwright"),
                             "driver": json.load(open(drv / "package" / "package.json"))["version"]}
        node = os.environ.get("PLAYWRIGHT_NODEJS_PATH") or str(drv / "node")
        try:
            nv = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except OSError as exc:
            nv = f"error: {exc}"
        env["node"] = {"path": relpath(node), "version": nv,
                       "source": "PLAYWRIGHT_NODEJS_PATH (unofficial-builds glibc-2.17 build)"
                       if os.environ.get("PLAYWRIGHT_NODEJS_PATH") else "bundled with playwright"}
        bj = json.load(open(drv / "package" / "browsers.json"))
        hs = next(b for b in bj["browsers"] if b["name"] == "chromium-headless-shell")
        exe = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")) / f"chromium_headless_shell-{hs['revision']}" / \
            "chrome-headless-shell-linux64" / "chrome-headless-shell"
        cmdline = own_browser_cmdline()
        try:
            cdp = browser.new_browser_cdp_session()
            ver = cdp.send("Browser.getVersion")
            cdp.detach()
        except PwError as exc:
            ver = {"error": str(exc)}
        sha = None
        if exe.is_file():
            h = hashlib.sha256()
            with open(exe, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()
        env["browser"] = {
            "engine": browser.browser_type.name, "version": browser.version, "headless": True,
            "build": f"Chrome Headless Shell {hs['browserVersion']} (playwright chromium-headless-shell r{hs['revision']})",
            "cdp_version": ver, "executable": relpath(exe), "executable_sha256": sha,
            "launch_args_added_by_test": self.launch_args,
            "command_line": [relpath(a) if a.startswith(str(ROOT)) else
                             re.sub(r"(--user-data-dir=).*", r"\1<temporary profile under TMPDIR>", a) for a in (cmdline or [])],
            "sandbox": "disabled (Playwright default chromiumSandbox=false adds --no-sandbox)",
            "profile": "fresh temporary profile created by Playwright under TMPDIR, deleted on close",
        }
        return env

    # ---------------------------------------------------------------- main
    def main(self) -> int:
        self.launch_args = BASE_LAUNCH_ARGS + list(self.args.browser_arg)
        services = {"mini": {"pir": {k: self.mini_pir[k] for k in ("protocol", "backend", "layout", "limits", "server", "listen")},
                             "client": {"listen": self.mini_ready["listen"], "url": redact(self.mini_ready["url"])},
                             "manifest": relpath(self.args.mini_manifest),
                             "catalog_id": self.mini_manifest["catalog_id"]}}
        if self.full:
            services["full"] = {"pir": {k: self.full_pir[k] for k in ("backend", "layout", "server", "listen")},
                                "client": {"listen": self.full_ready["listen"], "url": redact(self.full_ready["url"])},
                                "manifest": relpath(self.args.full_manifest),
                                "catalog_id": self.full_manifest["catalog_id"]}
        self.doc.update({"command": redact(" ".join([relpath(sys.argv[0]) or sys.argv[0]] + sys.argv[1:])),
                         "playback_rate": self.rate, "services": services,
                         "tracks": {"T2": self.t2_track, "T4": self.t4_track}})
        exit_code = 0
        try:
            with sync_playwright() as pw:
                self.shared["pw"] = pw
                t0 = time.perf_counter()
                browser = self.launch(pw, self.launch_args)
                self.doc["browser_launch_s"] = round(time.perf_counter() - t0, 3)
                self.shared["browser"] = browser
                try:
                    self.doc["environment"] = self.environment(browser)
                    ctx, page = self.open_ui(browser, self.mini, "mini")
                    self.shared["mini_page"] = page
                    try:
                        self.run("T1", "ui_load", self.t1_ui_load)
                        t2 = self.run("T2", "progressive_playback", self.t2_progressive)
                        if t2.d["status"] in ("pass", "fail"):
                            self.run("T3", "audio_content_hash", self.t3_hash)
                            self.run("T6", "wire_table", self.t6_wire)
                        else:  # T3/T6 need the retrieval of T2; record them as not run (counts as not passed)
                            for tid, name in (("T3", "audio_content_hash"), ("T6", "wire_table")):
                                skipped = Result(tid, name, True)
                                skipped.d.update(status="skipped", reason=f"T2 ended with status {t2.d['status']}")
                                self.results.append(skipped)
                        self.run("T7", "artifacts_screenshot", self.t7_screenshot)
                    finally:
                        ctx.close()
                    self.run("T4", "complete_mode", self.t4_complete)
                    self.run("T6b", "wire_schedule_track_independent", self.t6b_schedule)
                    self.run("T5", "cancel", self.t5_cancel)
                    self.run("T8", "ui_states_and_layout", self.t8_ui_states)
                    if not self.args.no_extras:
                        self.run("X3", "playback_after_cancel", self.x3_after_cancel, required=False)
                        self.run("X1", "cancel_before_first_block", self.x1_cancel_before_first_block, required=False)
                        self.run("X2", "autoplay_without_flag", self.x2_autoplay, required=False)
                    if self.shared.get("cancel_ctx") is not None:
                        self.shared.pop("cancel_ctx").close()
                finally:
                    browser.close()
            # the clients must still be alive and serving (the --forbid-reads guard did not trip a request)
            for name, api in (("mini", self.mini), ("full", self.full)):
                if api is not None:
                    st, _ = api.raw("GET", "/api/catalog")
                    self.doc.setdefault("post_checks", {})[f"{name}_client_alive"] = st == 200
        except KeyboardInterrupt:
            self.doc["aborted"] = "interrupted (SIGINT/SIGTERM/alarm)"
            exit_code = 2
        except Exception as exc:  # noqa: BLE001 - infrastructure failure
            self.doc["aborted"] = f"{type(exc).__name__}: {exc}"
            self.doc["traceback"] = redact(traceback.format_exc()[-4000:])
            exit_code = 2
        finally:
            self.finish(exit_code)
        failed = [x.d["id"] for x in self.results if x.d["status"] != "pass"]
        if exit_code == 0 and (failed or not all(self.doc.get("post_checks", {}).values())):
            exit_code = 1
        return exit_code

    def finish(self, exit_code: int) -> None:
        self.doc["finished_utc"] = utcnow()
        self.doc["duration_s"] = round(time.perf_counter() - self.t_start, 3)
        self.doc.setdefault("environment", {})["loadavg_end"] = os.getloadavg()
        self.doc["tests"] = [x.d for x in self.results]
        self.doc["extra_probes"] = [x.d for x in self.extras]
        req = [x.d for x in self.results]
        self.doc["summary"] = {
            "required_passed": sum(1 for x in req if x["status"] == "pass"),
            "required_total": len(req),
            "required_failed": [x["id"] for x in req if x["status"] != "pass"],
            "extra_probes": {x.d["id"]: x.d["status"] for x in self.extras},
            "overall": "pass" if req and all(x["status"] == "pass" for x in req) and exit_code == 0
            and all(self.doc.get("post_checks", {}).values()) else "fail",
        }
        self.doc["page_log_entries"] = len(self.page_logs)
        doc = finite(public_report(self.doc, root=ROOT))
        tmp = self.results_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(tmp, self.results_path)
        runs = self.results_path.parent / "runs"
        runs.mkdir(exist_ok=True)
        stamp = self.doc["started_utc"].replace(":", "").replace("-", "").replace(".", "")
        (runs / f"browser_playback_{stamp}.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False, allow_nan=False) + "\n",
                                                            encoding="utf-8")
        with open(self.logs_dir / "page_console.log", "w", encoding="utf-8") as fh:
            for e in public_report(self.page_logs, root=ROOT):
                fh.write(f"{e['t']} [{e['page']}] {e['kind']}: {e['text']}\n")
        with open(self.logs_dir / "browser_requests.log", "w", encoding="utf-8") as fh:
            fh.write("# time = browser-side request start (B) when Playwright reported it, else harness receive time (H)\n")
            for q in public_report(self.requests, root=ROOT):
                ts, src = (q["browser_start_epoch"], "B") if q.get("browser_start_epoch") else (q["t"], "H")
                fh.write(f"{dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec='milliseconds')} {src} "
                         f"[{q['page']}] {q['method']} {q['url']} ({q['resource']}{', range ' + q['range'] if q['range'] else ''})\n")
        s = self.doc["summary"]
        print(f"\nrequired tests: {s['required_passed']}/{s['required_total']} passed; extra probes: {s['extra_probes']}; "
              f"overall: {s['overall']}\nresults: {relpath(self.results_path)}", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mini-client", required=True, help="trusted client --ready-file JSON (mini catalog)")
    ap.add_argument("--mini-pir", required=True, help="PIR server --ready-file JSON (mini catalog)")
    ap.add_argument("--mini-manifest", default=str(ROOT / "data/catalog/mini/manifest.json"))
    ap.add_argument("--full-client", default=None, help="trusted client ready JSON (full catalog, cancel test)")
    ap.add_argument("--full-pir", default=None)
    ap.add_argument("--full-manifest", default=str(ROOT / "data/catalog/full/manifest.json"))
    ap.add_argument("--playback-rate", type=float, default=1.0, help="playbackRate for T2 (1.0 = real time)")
    ap.add_argument("--track", type=int, default=None, help="T2/T3 track index (default: shortest mini track)")
    ap.add_argument("--complete-track", type=int, default=None, help="T4 track index (default: second shortest)")
    ap.add_argument("--cancel-track", type=int, default=None, help="T5 track index (default: longest)")
    ap.add_argument("--results", default="results/browser/browser_playback_results.json")
    ap.add_argument("--logs-dir", default="logs/browser")
    ap.add_argument("--no-extras", action="store_true", help="skip the non-fatal probes X1/X2")
    ap.add_argument("--browser-arg", action="append", default=[], help="extra Chromium flag (repeatable)")
    ap.add_argument("--deadline-s", type=int, default=600, help="hard limit for the whole run")
    args = ap.parse_args(argv)
    if not 0.25 <= args.playback_rate <= 16:
        ap.error("--playback-rate must be within 0.25..16")

    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGALRM, interrupt)
    signal.alarm(args.deadline_s)
    try:
        suite = Suite(args)
    except (OSError, ValueError, KeyError) as exc:
        print(f"cannot start the browser tests: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return suite.main()


if __name__ == "__main__":
    raise SystemExit(main())
