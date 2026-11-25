"""Experiment orchestrator.

python -m pmr.experiments.runner --plan experiments/plans/<plan>.json [--out results/experiments] [--only NAME...]

For each experiment in the plan: (optionally) generate the catalog, start the server process
(PIR or conventional) under ``nice``, wait for readiness, run the client worker process
(warm-up + trials), sample the server's CPU time and peak RSS (/proc/<pid>/status VmHWM,
CPU via /v1/server/stats or /proc/<pid>/stat), stop the server, and store everything raw.

Output: <out>/<run_id>/{run_meta.json, plan.json, <exp>/{server.log, server_ready.json,
client.jsonl, server_stats.json, command.txt}, failures.jsonl}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import sysinfo
from ..reporting import public_report

ROOT = Path(__file__).resolve().parents[3]
PY = sys.executable


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "src" / "pmr").rglob("*")):
        if p.is_file() and p.suffix in (".py", ".js", ".html", ".css"):
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def proc_status(pid: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("VmHWM", "VmRSS"):
                    out[k] = int(v.split()[0])
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        tick = os.sysconf("SC_CLK_TCK")
        out["utime_s"] = int(fields[11]) / tick
        out["stime_s"] = int(fields[12]) / tick
        out["children_cutime_s"] = int(fields[13]) / tick
    except (OSError, IndexError, ValueError):
        pass
    return out


def children_pids(pid: int) -> list[int]:
    try:
        import psutil
        return [c.pid for c in psutil.Process(pid).children(recursive=True)]
    except Exception:
        return []


def ensure_catalog(spec: dict[str, Any]) -> str:
    """Return a catalog directory; generate synthetic catalogs on demand (deterministic)."""
    if "dir" in spec:
        return str(ROOT / spec["dir"])
    name = spec["generate"]["name"]
    d = ROOT / "data" / "catalog" / "generated" / name
    if not (d / "manifest.json").exists():
        g = spec["generate"]
        cmd = [PY, "-m", "pmr.audio.dataset", "generate", "--profile", g.get("profile", "full"), "--out", str(d)]
        if "n_tracks" in g:
            cmd += ["--n-tracks", str(g["n_tracks"])]
        if "duration_s" in g:
            cmd += ["--duration-s", str(g["duration_s"])]
        subprocess.run(["nice", "-n", "10", *cmd], check=True, cwd=str(ROOT), stdout=subprocess.DEVNULL)
    return str(d)


def wait_ready(ready: Path, proc: subprocess.Popen, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready.exists():
            return json.loads(ready.read_text(encoding="utf-8"))
        if proc.poll() is not None:
            raise RuntimeError(f"server exited with code {proc.returncode}")
        time.sleep(0.1)
    raise TimeoutError("server did not become ready")


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def run_experiment(exp: dict[str, Any], defaults: dict[str, Any], out_dir: Path, failures) -> None:
    cfg = {**defaults, **exp}
    name = cfg["name"]
    d = out_dir / name
    d.mkdir(parents=True, exist_ok=True)
    catalog = ensure_catalog(cfg["catalog"])
    kind = cfg.get("kind", "pir")
    ready = d / "server_ready.json"
    ready.unlink(missing_ok=True)
    if kind == "pir":
        s = cfg.get("server", {})
        server_cmd = [PY, "-m", "pmr.server", "--catalog", catalog, "--port", "0", "--ready-file", str(ready),
                      "--backend", s.get("backend", "optimized"), "--packing", s.get("packing", "u16"),
                      "--query-mode", s.get("query_mode", "selector"), "--db-form", s.get("db_form", "ntt"),
                      "--workers", str(s.get("workers", 1)), "--slots", str(s.get("slots", 4096))]
        if not s.get("mod_switch", True):
            server_cmd.append("--no-mod-switch")
        if not s.get("seeded", True):
            server_cmd.append("--no-seeded")
    else:
        s = cfg.get("server", {})
        server_cmd = [PY, "-m", "pmr.conventional", "--catalog", catalog, "--port", "0", "--ready-file", str(ready)]
    if s.get("max_tracks"):
        server_cmd += ["--max-tracks", str(s["max_tracks"])]
    if s.get("rate_limit_mbps"):
        server_cmd += ["--rate-limit-mbps", str(s["rate_limit_mbps"])]
    nice = ["nice", "-n", str(cfg.get("nice", 10))]
    log = open(d / "server.log", "wb")
    t_launch = time.perf_counter()
    server = subprocess.Popen(nice + server_cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
    try:
        info = wait_ready(ready, server, cfg.get("server_timeout_s", 900))
        server_ready_s = time.perf_counter() - t_launch
        port = info["listen"]["port"]
        base = f"http://127.0.0.1:{port}"
        pid = server.pid
        # `nice` execs the interpreter, so pid is the server process itself.
        stats_before = {"proc": proc_status(pid), "children": {c: proc_status(c) for c in children_pids(pid)}}
        c = cfg.get("client", {})
        ckind = {"pir": "pir", "conv_track": "conv_track", "conv_db": "conv_db"}[cfg.get("client_kind", "pir" if kind == "pir" else "conv_track")]
        client_cmd = [PY, "-m", "pmr.experiments.client_worker", "--kind", ckind, "--server", base,
                      "--tracks", ",".join(str(t) for t in cfg["tracks"]), "--warmup", str(cfg.get("warmup", 1)),
                      "--trials", str(cfg.get("trials", 5)), "--batch", str(c.get("batch", 8)),
                      "--first-batch", str(c.get("first_batch", 1)), "--prefetch", str(c.get("prefetch", 2)),
                      "--policy", c.get("policy", "pad"), "--out", str(d / "client.jsonl")]
        (d / "command.txt").write_text(
            public_report("server: " + " ".join(shlex.quote(x) for x in nice + server_cmd) + "\nclient: "
            + " ".join(shlex.quote(x) for x in nice + client_cmd) + "\n", root=ROOT), encoding="utf-8")
        (d / "client.jsonl").unlink(missing_ok=True)
        t0 = time.perf_counter()
        rc = subprocess.run(nice + client_cmd, cwd=str(ROOT), timeout=cfg.get("client_timeout_s", 7200)).returncode
        client_wall = time.perf_counter() - t0
        stats_after = {"proc": proc_status(pid), "children": {c: proc_status(c) for c in children_pids(pid)}}
        app_stats = None
        try:
            app_stats = get_json(base + "/v1/server/stats")
        except Exception as exc:  # conventional server has a smaller stats endpoint
            app_stats = {"error": str(exc)}
        doc = {"experiment": cfg, "catalog_dir": os.path.relpath(catalog, ROOT), "server_ready": info,
               "server_ready_wall_s": server_ready_s, "client_wall_s": client_wall, "client_returncode": rc,
               "server_proc_before": stats_before, "server_proc_after": stats_after, "server_app_stats": app_stats}
        (d / "server_stats.json").write_text(json.dumps(public_report(doc, root=ROOT), indent=1), encoding="utf-8")
        if rc != 0:
            failures.write(json.dumps({"experiment": name, "error": f"client exit {rc}"}) + "\n")
    except Exception as exc:
        failures.write(json.dumps(public_report({"experiment": name, "error": f"{type(exc).__name__}: {exc}"}, root=ROOT)) + "\n")
        print(public_report(f"  FAILED: {exc}", root=ROOT), flush=True)
    finally:
        if server.poll() is None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=30)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        log.close()
        log_path = d / "server.log"
        log_path.write_text(public_report(log_path.read_text(encoding="utf-8", errors="replace"), root=ROOT), encoding="utf-8")
        failures.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pmr.experiments.runner")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", default=str(ROOT / "results" / "experiments"))
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--only", nargs="*", default=None)
    a = ap.parse_args(argv)
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    run_id = a.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "_" + plan["name"]
    out_dir = Path(a.out) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {"run_id": run_id, "plan_file": os.path.relpath(a.plan, ROOT), "argv": sys.argv,
            "code_sha256": code_fingerprint(), "system": sysinfo.collect(), "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (out_dir / "run_meta.json").write_text(json.dumps(public_report(meta, root=ROOT), indent=1), encoding="utf-8")
    (out_dir / "plan.json").write_text(json.dumps(plan, indent=1), encoding="utf-8")
    with open(out_dir / "failures.jsonl", "a", encoding="utf-8") as failures:
        for exp in plan["experiments"]:
            if a.only and exp["name"] not in a.only:
                continue
            print(f"[{time.strftime('%H:%M:%S')}] {exp['name']}", flush=True)
            run_experiment(exp, plan.get("defaults", {}), out_dir, failures)
    meta["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    meta["system_at_end"] = {"loadavg": os.getloadavg()}
    (out_dir / "run_meta.json").write_text(json.dumps(public_report(meta, root=ROOT), indent=1), encoding="utf-8")
    print(f"results: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
