#!/usr/bin/env python3
"""Write results/sealpir/machine_info.json: hardware, OS, toolchain, library versions, archive hashes.

Reads only system information files (/proc, /sys, /etc/os-release) and project files.
Run from the project root after `source ./env.sh`.
"""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time

ROOT = os.environ.get("PMR_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TP = os.path.join(ROOT, "third_party")
OUT = os.path.join(ROOT, "results", "sealpir", "machine_info.json")


def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout.strip()
    except Exception as e:  # noqa: BLE001 - record the failure instead of crashing
        return "unavailable: %s" % e


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cpuinfo():
    text = open("/proc/cpuinfo").read()
    blocks = [b for b in text.split("\n\n") if b.strip()]
    first = dict(re.findall(r"^(.+?)\s*:\s*(.*)$", blocks[0], re.M))
    phys = {re.search(r"physical id\s*:\s*(\d+)", b).group(1) for b in blocks if "physical id" in b}
    flags = set(first.get("flags", "").split())
    caches = {}
    base = "/sys/devices/system/cpu/cpu0/cache"
    if os.path.isdir(base):
        for idx in sorted(os.listdir(base)):
            p = os.path.join(base, idx)
            try:
                level = open(os.path.join(p, "level")).read().strip()
                ctype = open(os.path.join(p, "type")).read().strip()
                size = open(os.path.join(p, "size")).read().strip()
                caches["L%s_%s" % (level, ctype)] = size
            except OSError:
                pass
    return {
        "model_name": first.get("model name"),
        "logical_cpus": os.cpu_count(),
        "sockets": len(phys) or None,
        "cores_per_socket_reported": first.get("cpu cores"),
        "siblings_per_socket_reported": first.get("siblings"),
        "flags_of_interest": sorted(flags & {"sse4_2", "avx", "avx2", "avx512f", "avx512dq", "avx512ifma",
                                             "bmi2", "adx", "aes", "pclmulqdq"}),
        "cache_cpu0": caches,
        "hypervisor": "hypervisor" in flags,
    }


def meminfo():
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            kib = int(line.split()[1])
            return {"mem_total_kib": kib, "mem_total_gib": round(kib / 1024 / 1024, 2)}
    return {}


def os_release():
    try:
        d = dict(re.findall(r'^(\w+)="?(.*?)"?$', open("/etc/os-release").read(), re.M))
        return d.get("PRETTY_NAME")
    except OSError:
        return None


def archives():
    rows = []
    manifest = os.path.join(TP, "archives", "SOURCES.tsv")
    for line in open(manifest):
        if not line.strip() or line.startswith("#"):
            continue
        name, url, expected = line.rstrip("\n").split("\t")
        path = os.path.join(TP, "archives", name)
        actual = sha256(path) if os.path.exists(path) else None
        rows.append({"file": name, "url": url, "sha256_expected": expected, "sha256_actual": actual,
                     "match": actual == expected})
    return rows


def tenseal_info():
    try:
        import tenseal  # noqa: PLC0415
        so = [os.path.join(os.path.dirname(os.path.dirname(tenseal.__file__)), f)
              for f in os.listdir(os.path.dirname(os.path.dirname(tenseal.__file__)))
              if f.startswith("_sealapi_cpp") and f.endswith(".so")]
        comment = sh(["readelf", "-p", ".comment", so[0]]) if so else None
        comp = re.findall(r"(GCC: .*|clang version [^\n]*)", comment or "")
        return {"tenseal_version": tenseal.__version__,
                "seal_version_pinned_by_tenseal_cmake": "4.1.1 (third_party/meta/tenseal_v0.3.14_seal.cmake)",
                "sealapi_module_comment_section": comp}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main():
    cm = os.path.join(ROOT, ".tools", "cmake", "cmake-3.31.12-linux-x86_64", "bin", "cmake")
    sealpir_meta = json.load(open(os.path.join(TP, "meta", "sealpir_master_commit.json")))
    patch = os.path.join(ROOT, "bench", "sealpir", "patches", "seal-4.0.0-gcc7-ice-rns.patch")
    info = {
        "collected_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cpu": cpuinfo(),
        "memory": meminfo(),
        "os": {"pretty_name": os_release(), "kernel": platform.release(), "machine": platform.machine(),
               "glibc": os.confstr("CS_GNU_LIBC_VERSION")},
        "toolchain": {
            "gxx": sh(["g++", "--version"]).splitlines()[0],
            "gcc": sh(["gcc", "--version"]).splitlines()[0],
            "ld": sh(["ld", "--version"]).splitlines()[0],
            "make": sh(["make", "--version"]).splitlines()[0],
            "cmake": sh([cm, "--version"]).splitlines()[0],
            "cmake_path": os.path.relpath(cm, ROOT),
            "python_for_scripts": sys.version.split()[0],
        },
        "libraries": {
            "seal": {"version": "4.0.0", "source": "https://github.com/microsoft/SEAL/archive/refs/tags/v4.0.0.tar.gz",
                     "builds": {
                         "default": {"prefix": "third_party/install/seal-4.0.0",
                                     "options": "Release (-O3 -DNDEBUG), static, SEAL_USE_ZSTD=ON (zstd 1.4.5), "
                                                "ZLIB/MSGSL/HEXL OFF, SEAL_AVOID_BRANCHING=OFF (default)"},
                         "avoidbranch": {"prefix": "third_party/install/seal-4.0.0-avoidbranch",
                                         "options": "same as default but SEAL_AVOID_BRANCHING=ON"}},
                     "gcc7_workaround_patch": {"file": os.path.relpath(patch, ROOT), "sha256": sha256(patch),
                                               "applied": open(os.path.join(ROOT, "results", "sealpir", "logs",
                                                                            "seal_patch_state.txt")).read().strip()},
                     "unit_tests": "sealtest: see results/sealpir/logs/seal_tests.log and seal-avoidbranch_tests.log"},
            "sealpir": {"commit": sealpir_meta["sha"], "commit_date": sealpir_meta["commit"]["committer"]["date"],
                        "commit_message_first_line": sealpir_meta["commit"]["message"].splitlines()[0],
                        "source": "https://github.com/microsoft/SealPIR/archive/%s.tar.gz" % sealpir_meta["sha"],
                        "bench_flags": "-O3 -std=gnu++1z, assertions enabled (no -DNDEBUG)"},
            "zstd": {"version": "1.4.5", "commit": "b706286adbba780006a47ef92df0ad7a785666b6"},
            "googletest": {"version": "1.11.0", "commit": "e2239ee6043f73722e7aa812a459f54a28552929",
                           "used_for": "SEAL unit tests only"},
            "tenseal_in_venv_for_calibration": tenseal_info(),
        },
        "archives": archives(),
        "run_limits": {"nice": 10, "ulimit_v_kib": 3900000, "build_jobs": 4, "benchmark_threads": 1},
    }
    with open(OUT, "w") as f:
        json.dump(info, f, indent=2)
        f.write("\n")
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
