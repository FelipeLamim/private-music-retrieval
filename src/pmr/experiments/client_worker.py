"""Client process for experiments (one process per configuration, so client CPU and peak RSS
are measured in isolation). Writes one JSON line per trial.

python -m pmr.experiments.client_worker --kind pir|conv_track|conv_db --server URL
       --tracks 0,3,6 --warmup 1 --trials 5 --out FILE [--batch 8 --first-batch 1 --policy pad]
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
import traceback

from .playback import simulate
from ..reporting import public_report


def _vm(field: str) -> int | None:
    try:
        with open("/proc/self/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith(field + ":"):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _events_compact(events) -> dict:
    return {"j": [e.j for e in events], "requested": [round(e.requested, 6) for e in events],
            "received": [round(e.received, 6) for e in events], "decoded": [round(e.decoded, 6) for e in events],
            "response_bytes": [e.response_bytes for e in events], "pcm_bytes": [e.pcm_bytes for e in events]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["pir", "conv_track", "conv_db"], required=True)
    ap.add_argument("--server", required=True)
    ap.add_argument("--tracks", required=True)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--first-batch", type=int, default=1)
    ap.add_argument("--prefetch", type=int, default=2)
    ap.add_argument("--policy", choices=["pad", "stop"], default="pad")
    ap.add_argument("--start-thresholds", default="0.256,1.0,2.0")
    ap.add_argument("--keep-events", action="store_true")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    tracks = [int(x) for x in a.tracks.split(",") if x != ""]
    thresholds = [float(x) for x in a.start_thresholds.split(",")]
    out = open(a.out, "a", encoding="utf-8")

    def emit(doc: dict) -> None:
        out.write(json.dumps(public_report(doc, root=os.environ.get("PMR_ROOT")), allow_nan=True) + "\n")
        out.flush()

    try:
        t0, c0 = time.perf_counter(), _cpu()
        if a.kind == "pir":
            from ..client.pirclient import PirHttpClient
            client = PirHttpClient(a.server)
            client.open_session()
            setup = {"wall_ms": (time.perf_counter() - t0) * 1e3, "cpu_s": _cpu() - c0,
                     **client.setup_timings, "session_setup_body_bytes": client.setup_bytes,
                     "rss_after_setup_kib": _vm("VmRSS")}
            lengths = client.layout.record_lengths
            bytes_per_s = 2 * int(client.catalog["audio"]["sample_rate"])
        else:
            from ..conventional.client import ConventionalClient
            client = ConventionalClient(a.server)
            setup = {"wall_ms": (time.perf_counter() - t0) * 1e3, "cpu_s": _cpu() - c0}
            lengths = client.lengths
            bytes_per_s = 2 * int(client.catalog["audio"]["sample_rate"])
        emit({"type": "setup", "kind": a.kind, "pid": os.getpid(), **setup})

        plan = [("warmup", tracks[k % len(tracks)]) for k in range(a.warmup)]
        plan += [("trial", t) for _ in range(a.trials) for t in tracks]
        for n, (phase, idx) in enumerate(plan):
            c_start, w_start = _cpu(), time.perf_counter()
            try:
                if a.kind == "pir":
                    res = client.retrieve(idx, policy=a.policy, first_batch=a.first_batch, batch=a.batch,
                                          prefetch=a.prefetch)
                    pcm, timings, sizes, events = res.pcm, res.timings, res.bytes, res.events
                    extra = {"server_compute_ms": res.server_compute_ms, "server_total_ms": res.server_total_ms,
                             "complete": res.complete,
                             "requests": {k: [r[k] for r in res.requests] for k in
                                          ("start", "count", "compute_ms", "server_ms", "t_req", "t_rcv", "bytes")}}
                elif a.kind == "conv_track":
                    res = client.retrieve_track(idx)
                    pcm, timings, sizes, events, extra = res.pcm, res.timings, res.bytes, res.events, {"complete": True}
                else:
                    res = client.retrieve_via_database(idx)
                    pcm, timings, sizes, events, extra = res.pcm, res.timings, res.bytes, res.events, {"complete": True}
                wall = time.perf_counter() - w_start
                cpu = _cpu() - c_start
                ok = len(pcm) == lengths[idx]
                import hashlib
                arrivals = [(e.decoded, e.pcm_bytes) for e in events]
                stalls = {f"{th:g}": simulate(arrivals, lengths[idx], bytes_per_s=bytes_per_s, start_s=th)
                          for th in thresholds}
                doc = {"type": phase, "n": n, "track": idx, "ok": ok, "sha256": hashlib.sha256(pcm).hexdigest(),
                       "wall_s": wall, "client_cpu_s": cpu, "timings_ms": timings, "bytes": sizes,
                       "audio_s": lengths[idx] / bytes_per_s, "playback_model": stalls,
                       "client_rss_kib": _vm("VmRSS"), "client_hwm_kib": _vm("VmHWM"), **extra}
                if a.keep_events or phase == "trial":
                    doc["events"] = _events_compact(events)
                emit(doc)
            except Exception as exc:
                emit({"type": "failure", "phase": phase, "n": n, "track": idx,
                      "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        emit({"type": "end", "client_hwm_kib": _vm("VmHWM"), "client_cpu_total_s": _cpu()})
        client.close()
        return 0
    except Exception as exc:
        emit({"type": "fatal", "error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()})
        return 1
    finally:
        out.close()


if __name__ == "__main__":
    sys.exit(main())
