#!/usr/bin/env python3
"""Run a command and report its wall time, exit status and peak RSS (no /usr/bin/time here).

Usage: rss_run.py OUT.json -- cmd [args...]
The child's stdout/stderr are inherited. Peak RSS comes from getrusage(RUSAGE_CHILDREN),
whose ru_maxrss is the largest RSS of any waited-for child, in KiB on Linux.
"""
import json
import resource
import subprocess
import sys
import time


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print(__doc__, file=sys.stderr)
        return 2
    out_path, cmd = sys.argv[1], sys.argv[3:]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd)
    wall = time.perf_counter() - t0
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    info = {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "wall_s": round(wall, 3),
        "user_s": round(ru.ru_utime, 3),
        "sys_s": round(ru.ru_stime, 3),
        "max_rss_kib": ru.ru_maxrss,
    }
    with open(out_path, "w") as f:
        json.dump(info, f, indent=2)
        f.write("\n")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
