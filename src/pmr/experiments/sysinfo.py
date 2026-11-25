"""Hardware/software description recorded with every experiment run."""
from __future__ import annotations

import os
import platform
import sys
import time
from typing import Any


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def mem_total_kib() -> int | None:
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return None


def os_release() -> str:
    for line in _read("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return platform.platform()


def package_versions() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        from importlib.metadata import version
    except ImportError:  # pragma: no cover
        return out
    for name in ("tenseal", "numpy", "scipy", "psutil", "matplotlib"):
        try:
            out[name] = version(name)
        except Exception:
            out[name] = "not installed"
    return out


def collect() -> dict[str, Any]:
    try:
        load = os.getloadavg()
    except OSError:
        load = (float("nan"),) * 3
    return {
        "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cpu_model": cpu_model(),
        "logical_cpus": os.cpu_count(),
        "mem_total_kib": mem_total_kib(),
        "os": os_release(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "libc": "-".join(platform.libc_ver()),
        "python": sys.version.split()[0],
        "python_impl": platform.python_implementation(),
        "packages": package_versions(),
        "seal": "Microsoft SEAL 4.1.1 (bundled in TenSEAL 0.3.14, per TenSEAL cmake/seal.cmake)",
        "loadavg_at_start": load,
        "env_threads": {k: os.environ.get(k) for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")},
        "note": "Virtualized shared machine; CPU frequency scaling and co-tenant load are not controlled.",
    }
