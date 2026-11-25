#!/usr/bin/env python3
"""Summarize results/sealpir/raw/*.jsonl into summary.json, summary.csv and summary_tables.md.

Statistics are over timed trials only (warm-up excluded): n, median, min, max, q1, q3
(inclusive quartiles). Normalized "per 8192 payload bytes" values scale a per-block value by
8192 / payload_bytes_of_that_block. Nothing is estimated here; derived values are labeled.
"""
import csv
import glob
import json
import os
import re
import statistics
import sys

ROOT = os.environ.get("PMR_ROOT") or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))
from pmr.reporting import public_report

RES = os.path.join(ROOT, "results", "sealpir")
RAW = os.path.join(RES, "raw")
LOGS = os.path.join(RES, "logs")


def stat(values):
    v = sorted(values)
    if not v:
        return None
    q = statistics.quantiles(v, n=4, method="inclusive") if len(v) > 1 else [v[0], v[0], v[0]]
    return {"n": len(v), "median": statistics.median(v), "min": v[0], "max": v[-1], "q1": q[0], "q3": q[2]}


def load():
    runs = {}
    for path in sorted(glob.glob(os.path.join(RAW, "*.jsonl"))):
        label = os.path.basename(path)[:-6]
        rows = [json.loads(line) for line in open(path) if line.strip()]
        runs[label] = rows
    return runs


def summarize_trials(rows):
    trials = [r for r in rows if "timings_ms" in r and not r.get("warmup")]
    if not trials:
        return None
    # Per-trial sums of measured parts (labeled as derived in the tables).
    for r in trials:
        t = r["timings_ms"]
        if "client_decode" in t:
            t["derived_client_load_decode"] = t["reply_deserialize"] + t["client_decode"]
        if "client_decode_sum" in t:
            t["derived_client_load_decode_sum"] = t["reply_deserialize_sum"] + t["client_decode_sum"]
        if "server_init_sum" in t:
            t["derived_server_setup_sum"] = (t["server_init_sum"] + t["galois_set_sum"]
                                             + t["db_set_database_sum"] + t["db_preprocess_ntt_sum"])
    out = {"config": trials[0]["config"], "n_trials": len(trials),
           "n_warmup": sum(1 for r in rows if r.get("warmup")),
           "correct": sum(1 for r in trials if r.get("correct")),
           "timings_ms": {}, "sizes_bytes": {}}
    if "reuse_correct" in trials[0]:
        out["reuse_correct"] = sum(1 for r in trials if r.get("reuse_correct"))
    for group in ("timings_ms", "sizes_bytes"):
        keys = sorted({k for r in trials for k, v in r[group].items() if isinstance(v, (int, float))})
        for k in keys:
            out[group][k] = stat([r[group][k] for r in trials if isinstance(r[group].get(k), (int, float))])
    out["peak_rss_bytes"] = stat([r["peak_rss_bytes"] for r in trials])
    out["build"] = trials[0].get("build")
    return out


def rusage(label):
    p = os.path.join(LOGS, "run_%s.rusage.json" % label)
    return json.load(open(p)) if os.path.exists(p) else None


def probe_outcome(label):
    err = open(os.path.join(LOGS, "run_%s.stderr.log" % label)).read()
    m = re.search(r"(Assertion `[^']+' failed)", err)
    ru = rusage(label) or {}
    return {"exit_code": ru.get("exit_code"), "assertion": m.group(1) if m else None,
            "where": (re.search(r"(pir(?:_server)?\.cpp:\d+)", err) or [None])[0] if m else None}


def fmt(x, nd=2):
    if x is None:
        return "–"
    if abs(x) >= 1000 and nd <= 2:
        return "{:,.0f}".format(x)
    return ("{:,.%df}" % nd).format(x)


def mmm(s, nd=2, scale=1.0):
    """median [min–max]"""
    if not s:
        return "–"
    return "%s [%s–%s]" % (fmt(s["median"] * scale, nd), fmt(s["min"] * scale, nd), fmt(s["max"] * scale, nd))


def med(s, nd=2, scale=1.0):
    return fmt(s["median"] * scale, nd) if s else "–"


def main():
    runs = load()
    summary = {"trials": {}, "probes": {}, "instances": {}, "calibration": []}
    for label, rows in runs.items():
        if label.startswith("P_"):
            summary["probes"][label] = {"rows": rows, **probe_outcome(label)}
        elif label.startswith("I_"):
            summary["instances"][label] = rows[0] if rows else None
        else:
            s = summarize_trials(rows)
            if s:
                s["process_rusage"] = rusage(label)
                summary["trials"][label] = s
    cal = os.path.join(RES, "calibration_seal_ops.jsonl")
    if os.path.exists(cal):
        summary["calibration"] = [json.loads(l) for l in open(cal) if l.strip()]
    with open(os.path.join(RES, "summary.json"), "w") as f:
        json.dump(public_report(summary, root=ROOT), f, indent=2)
        f.write("\n")

    # Flat CSV: one row per (label, group, metric).
    with open(os.path.join(RES, "summary.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["label", "mode", "variant", "items", "size_per_item", "d", "use_symmetric", "group",
                    "metric", "n", "median", "min", "max", "q1", "q3"])
        for label, s in summary["trials"].items():
            c = s["config"]
            for group in ("timings_ms", "sizes_bytes"):
                for k, st in s[group].items():
                    w.writerow([label, c["mode"], c["variant"], c["number_of_items"], c["size_per_item"], c["d"],
                                c["use_symmetric"], group, k, st["n"], st["median"], st["min"], st["max"],
                                st["q1"], st["q3"]])
            st = s["peak_rss_bytes"]
            w.writerow([label, c["mode"], c["variant"], c["number_of_items"], c["size_per_item"], c["d"],
                        c["use_symmetric"], "memory", "peak_rss_bytes", st["n"], st["median"], st["min"],
                        st["max"], st["q1"], st["q3"]])

    T = summary["trials"]
    md = []
    MiB = 1.0 / (1024 * 1024)

    # ---- Table A1: per-block communication and correctness (default build).
    md.append("<!-- TABLE:A1 -->")
    md.append("| R | item (B) | BFV plaintexts (nvec) | query (B) | reply (B) | Galois keys (B) | correct | peak RSS (MiB) |")
    md.append("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20, 40, 100):
        for S in (8192, 10240):
            s = T.get("A_R%d_S%d" % (R, S))
            if not s:
                continue
            z = s["sizes_bytes"]
            md.append("| %d | %d | %s | %s | %s | %s | %d/%d | %s |" % (
                R, S, s["config"]["nvec"][0], med(z["query"], 0), med(z["reply"], 0), med(z["galois_keys"], 0),
                s["correct"], s["n_trials"], fmt(s["peak_rss_bytes"]["max"] * MiB, 1)))
    s = T.get("A_R10_S8192_pubkey")
    if s:
        z = s["sizes_bytes"]
        md.append("| 10 (public-key query) | 8192 | %s | %s | %s | %s | %d/%d | %s |" % (
            s["config"]["nvec"][0], med(z["query"], 0), med(z["reply"], 0), med(z["galois_keys"], 0),
            s["correct"], s["n_trials"], fmt(s["peak_rss_bytes"]["max"] * MiB, 1)))
    md.append("")

    # ---- Table A2: per-block computation, SealPIR API path (default build), ms, median [min–max].
    md.append("<!-- TABLE:A2 -->")
    md.append("| R | item (B) | generate_reply | mod switch + serialize | server total | of which expand_query (timed alone) |")
    md.append("|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20, 40, 100):
        for S in (8192, 10240):
            s = T.get("A_R%d_S%d" % (R, S))
            if not s:
                continue
            t = s["timings_ms"]
            md.append("| %d | %d | %s | %s | %s | %s |" % (
                R, S, mmm(t["reply_gen"]), mmm(t["reply_modswitch_serialize"]), mmm(t["server_reply_total"]),
                mmm(t["expand_only"])))
    md.append("")

    # ---- Table A2R: reuse path (expanded query reused) and client side, ms, median [min–max].
    md.append("<!-- TABLE:A2R -->")
    md.append("| R | item (B) | reuse path: products + iNTT | reuse path: mod switch + serialize | client: query gen (encrypt + serialize) | client: reply load + decode |")
    md.append("|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20, 40, 100):
        for S in (8192, 10240):
            s = T.get("A_R%d_S%d" % (R, S))
            if not s:
                continue
            t = s["timings_ms"]
            md.append("| %d | %d | %s | %s | %s | %s |" % (
                R, S, mmm(t["reuse_reply_gen"]), mmm(t["reuse_modswitch_serialize"]),
                mmm(t["query_gen_serialized"]), mmm(t["derived_client_load_decode"])))
    md.append("")

    # ---- Table A3: setup costs (default build), ms.
    md.append("<!-- TABLE:A3 -->")
    md.append("| R | item (B) | client keygen (PIRClient ctor) | Galois keygen (12 elts) | Galois serialize | Galois load + set | server init | set_database | preprocess (NTT) |")
    md.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20, 40, 100):
        for S in (8192, 10240):
            s = T.get("A_R%d_S%d" % (R, S))
            if not s:
                continue
            t = s["timings_ms"]
            md.append("| %d | %d | %s | %s | %s | %s | %s | %s | %s |" % (
                R, S, med(t["client_keygen"]), med(t["galois_keygen"]), med(t["galois_serialize"]),
                med(t["galois_deserialize_set"]), med(t["server_init"]), med(t["db_set_database"]),
                med(t["db_preprocess_ntt"])))
    md.append("")

    # ---- Table A4: normalized per 8192 payload bytes (derived from medians).
    md.append("<!-- TABLE:A4 -->")
    md.append("| R | item (B) | reply B per 8192 payload B | SealPIR API server ms per 8192 B | reuse path server ms per 8192 B (incl. mod switch + serialize) | reuse path products + iNTT only, ms per 8192 B |")
    md.append("|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20, 40, 100):
        for S in (8192, 10240):
            s = T.get("A_R%d_S%d" % (R, S))
            if not s:
                continue
            t, z, k = s["timings_ms"], s["sizes_bytes"], 8192.0 / S
            md.append("| %d | %d | %s | %s | %s | %s |" % (
                R, S, fmt(z["reply"]["median"] * k, 0), fmt(t["server_reply_total"]["median"] * k, 2),
                fmt(t["reuse_server_block_total"]["median"] * k, 2), fmt(t["reuse_reply_gen"]["median"] * k, 2)))
    md.append("")

    # ---- Table B: whole-track retrieval.
    md.append("<!-- TABLE:B -->")
    md.append("| R | variant | blocks | query (B) | Galois keys (B) | total reply (B) | reply / payload | server time per track (s) | per-block server (ms, median over blocks) | client load + decode per track (s) | server setup per track (s) | correct | peak RSS (MiB) |")
    md.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for R in (10, 20):
        for V in ("api", "reuse"):
            s = T.get("B_R%d_%s" % (R, V))
            if not s:
                continue
            t, z = s["timings_ms"], s["sizes_bytes"]
            client = t["derived_client_load_decode_sum"]["median"]
            if V == "api":
                setup_s = fmt(t["derived_server_setup_sum"]["median"] / 1000, 2) + " (ctor + keys + DB, per block)"
            else:
                setup_s = fmt(t["db_encode_ntt_all_blocks"]["median"] / 1000, 2) + " (DB encode + NTT)"
            md.append("| %d | %s | %d | %s | %s | %s | %s | %s | %s | %s | %s | %d/%d | %s |" % (
                R, V, z["blocks"]["median"], med(z["query"], 0), med(z["galois_keys"], 0), med(z["reply_total"], 0),
                fmt(z["reply_total"]["median"] / z["payload_bytes"]["median"], 3),
                mmm(t["server_query_total"], 3, 1 / 1000.0), mmm(t["per_block_server_median"], 2),
                fmt(client / 1000, 3), setup_s, s["correct"], s["n_trials"], fmt(s["peak_rss_bytes"]["max"] * MiB, 1)))
    md.append("")

    # ---- Table B2: per-track server time split (reuse variant) .
    md.append("<!-- TABLE:B2 -->")
    md.append("| R | variant | query deserialize (ms) | expand + NTT once (ms) | sum of block replies (ms) | sum of mod switch + serialize (ms) |")
    md.append("|---:|---|---:|---:|---:|---:|")
    for R in (10, 20):
        for V in ("api", "reuse"):
            s = T.get("B_R%d_%s" % (R, V))
            if not s:
                continue
            t = s["timings_ms"]
            md.append("| %d | %s | %s | %s | %s | %s |" % (
                R, V, med(t["query_deserialize"]), med(t.get("expand_and_ntt_once")) if V == "reuse" else "(inside each block reply)",
                med(t["reply_gen_sum"], 1), med(t["reply_modswitch_serialize_sum"], 1)))
    md.append("")

    # ---- Table C: d = 2.
    md.append("<!-- TABLE:C -->")
    md.append("| config | nvec | query ciphertexts | query (B) | reply ciphertexts | reply (B) | server total (ms) | expand_query alone (ms) | client decode (ms) | correct |")
    md.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label in ("A_R100_S8192", "C_R100_S8192_d2"):
        s = T.get(label)
        if not s:
            continue
        t, z = s["timings_ms"], s["sizes_bytes"]
        md.append("| %s (d=%d) | %s | %s | %s | %s | %s | %s | %s | %s | %d/%d |" % (
            label, s["config"]["d"], s["config"]["nvec"], med(z["query_ciphertexts"], 0), med(z["query"], 0),
            med(z["reply_ciphertexts"], 0), med(z["reply"], 0), mmm(t["server_reply_total"]),
            mmm(t["expand_only"]), mmm(t["client_decode"]), s["correct"], s["n_trials"]))
    md.append("")

    # ---- Sensitivity: default vs avoid-branching SEAL build.
    md.append("<!-- TABLE:SENS -->")
    md.append("| metric (median) | SEAL default build | SEAL_AVOID_BRANCHING=ON build |")
    md.append("|---|---:|---:|")
    pairs = [("A_R10_S8192", "server_reply_total", "A R=10, 8192 B: SealPIR server total (ms)"),
             ("A_R10_S8192", "reuse_server_block_total", "A R=10, 8192 B: reuse block total (ms)"),
             ("A_R10_S8192", "reuse_reply_gen", "A R=10, 8192 B: reuse products + iNTT (ms)"),
             ("A_R100_S8192", "server_reply_total", "A R=100, 8192 B: SealPIR server total (ms)"),
             ("A_R100_S8192", "expand_only", "A R=100, 8192 B: expand_query alone (ms)"),
             ("C_R100_S8192_d2", "server_reply_total", "C R=100, d=2: server total (ms)"),
             ("B_R10_api", "server_query_total", "B R=10 api: server per track (ms)"),
             ("B_R10_reuse", "server_query_total", "B R=10 reuse: server per track (ms)"),
             ("B_R20_api", "server_query_total", "B R=20 api: server per track (ms)"),
             ("B_R20_reuse", "server_query_total", "B R=20 reuse: server per track (ms)")]
    for label, key, name in pairs:
        a, b = T.get(label), T.get(label + "_ab")
        md.append("| %s | %s | %s |" % (name, med(a["timings_ms"].get(key)) if a else "–",
                                        med(b["timings_ms"].get(key)) if b else "–"))
    md.append("")

    # ---- Calibration.
    md.append("<!-- TABLE:CAL -->")
    cal = [c for c in summary["calibration"] if c["plain_modulus"] == 65537]
    if cal:
        ops = ["ntt_fwd", "ntt_inv", "mul_plain_ntt", "add_inplace", "mod_switch_next", "apply_galois",
               "block_R10_products", "block_R10_full", "save_last_level_zstd"]
        md.append("| operation (N=4096, t=65537), median ms over 300 reps | " + " | ".join(c["build"] for c in cal) + " |")
        md.append("|---|" + "---:|" * len(cal))
        for op in ops:
            md.append("| %s | %s |" % (op, " | ".join(fmt(c["ops_ms"][op]["median"], 4) if op in c["ops_ms"] else "n/a"
                                                     for c in cal)))
    md.append("")

    # ---- Instances.
    md.append("<!-- TABLE:INST -->")
    md.append("| R | item (B) | PIRServer instances | Galois keys in memory per instance (B) | DB NTT plaintexts per instance (B) | mean RSS growth per instance (B) | setup per instance (ms) |")
    md.append("|---:|---:|---:|---:|---:|---:|---:|")
    for label, r in sorted(summary["instances"].items()):
        if not r:
            continue
        md.append("| %d | %d | %d | %s | %s | %s | %s |" % (
            r["config"]["number_of_items"], r["config"]["size_per_item"], r["instances"],
            fmt(r["galois_keys_in_memory_bytes"], 0), fmt(r["db_ntt_plaintext_bytes_per_instance"], 0),
            fmt(r["rss_bytes_per_instance_mean"], 0), fmt(r["setup_ms_per_instance"], 2)))
    md.append("")

    # ---- Probes.
    md.append("<!-- TABLE:PROBE -->")
    md.append("| probe | outcome |")
    md.append("|---|---|")
    for label, p in sorted(summary["probes"].items()):
        if p["assertion"]:
            outcome = "abort (exit %s): %s at %s" % (p["exit_code"], p["assertion"], p["where"])
        else:
            row = p["rows"][0] if p["rows"] else {}
            outcome = "ok (exit %s): elements_per_plaintext=%s, nvec=%s" % (
                p["exit_code"], row.get("elements_per_plaintext"), row.get("nvec"))
        md.append("| %s | %s |" % (label, outcome))
    md.append("")

    with open(os.path.join(RES, "summary_tables.md"), "w") as f:
        f.write("\n".join(md))
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    sys.exit(main())
