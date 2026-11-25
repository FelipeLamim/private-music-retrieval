"""Summarize raw experiment runs into tables (CSV/JSON/Markdown/LaTeX) and figures.

python experiments/analyze.py RUN_DIR [RUN_DIR ...] [--out results/summary] [--figures results/figures]

Every number written here is computed from results/experiments/<run>/<exp>/client.jsonl and
server_stats.json; each output file records the run directories it came from.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics as st
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def q(values, p):
    v = sorted(x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x)))
    if not v:
        return float("nan")
    k = (len(v) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def agg(values) -> dict[str, float]:
    v = [x for x in values if x is not None and not (isinstance(x, float) and math.isnan(x))]
    if not v:
        return {"n": 0, "median": float("nan"), "min": float("nan"), "max": float("nan"), "q1": float("nan"), "q3": float("nan")}
    return {"n": len(v), "median": st.median(v), "min": min(v), "max": max(v), "q1": q(v, 0.25), "q3": q(v, 0.75),
            "mean": st.fmean(v), "stdev": st.stdev(v) if len(v) > 1 else 0.0}


def load_manifest_hashes(catalog_dir: str) -> list[str]:
    m = json.loads((ROOT / catalog_dir / "manifest.json").read_text(encoding="utf-8"))
    return [t["sha256_pcm"] for t in m["tracks"]]


def proc_cpu(p: dict) -> float:
    return (p.get("utime_s") or 0.0) + (p.get("stime_s") or 0.0)


def summarize_experiment(d: Path) -> dict[str, Any] | None:
    cj, sj = d / "client.jsonl", d / "server_stats.json"
    if not cj.exists() or not sj.exists():
        return None
    rows = [json.loads(l) for l in cj.read_text(encoding="utf-8").splitlines() if l.strip()]
    stats = json.loads(sj.read_text(encoding="utf-8"))
    exp = stats["experiment"]
    trials = [r for r in rows if r["type"] == "trial"]
    warm = [r for r in rows if r["type"] == "warmup"]
    fails = [r for r in rows if r["type"] in ("failure", "fatal")]
    setup = next((r for r in rows if r["type"] == "setup"), {})
    hashes = load_manifest_hashes(stats["catalog_dir"])
    correct = [r["ok"] and r.get("sha256") == hashes[r["track"]] for r in trials]
    n_retrievals = len(trials) + len(warm)
    b, a = stats["server_proc_before"], stats["server_proc_after"]
    srv_cpu = proc_cpu(a["proc"]) - proc_cpu(b["proc"])
    for pid, pa in a.get("children", {}).items():
        pb = b.get("children", {}).get(pid, {})
        srv_cpu += proc_cpu(pa) - proc_cpu(pb)
    hwm_main = a["proc"].get("VmHWM")
    hwm_children = sum(v.get("VmHWM") or 0 for v in a.get("children", {}).values())
    ready = stats.get("server_ready", {})
    srv = ready.get("server", {})
    kind = exp.get("client_kind", "pir" if exp.get("kind", "pir") == "pir" else "conv_track")
    T = lambda key: [r["timings_ms"].get(key) for r in trials]  # noqa: E731
    B = lambda key: [r["bytes"].get(key) for r in trials]  # noqa: E731
    out: dict[str, Any] = {
        "experiment": exp["name"], "kind": kind, "dir": str(d.relative_to(ROOT)),
        "server_cfg": exp.get("server", {}), "client_cfg": exp.get("client", {}),
        "label": (ready.get("backend") or {}).get("label", kind),
        "num_records": (ready.get("layout") or {}).get("num_records"),
        "num_blocks": (ready.get("layout") or {}).get("num_blocks"),
        "block_bytes": (ready.get("layout") or {}).get("block_bytes"),
        "trials": len(trials), "warmups": len(warm), "failures": len(fails), "all_correct": all(correct) and bool(trials),
        "tracks": sorted({r["track"] for r in trials}),
        "audio_s": agg([r["audio_s"] for r in trials]),
        "total_s": agg([x / 1e3 for x in T("total_ms")]),
        "ttfa_ms": agg(T("first_block_ms")),
        "wall_s": agg([r["wall_s"] for r in trials]),
        "client_cpu_s": agg([r["client_cpu_s"] for r in trials]),
        "throughput_x": agg([r["audio_s"] / (r["timings_ms"]["total_ms"] / 1e3) for r in trials if r["timings_ms"].get("total_ms")]),
        "wire_tx": agg(B("wire_tx")), "wire_rx": agg(B("wire_rx")), "pcm": agg(B("pcm")),
        "expansion_rx": agg([r["bytes"]["wire_rx"] / r["bytes"]["pcm"] for r in trials if r["bytes"].get("pcm")]),
        "server_cpu_s_per_retrieval": srv_cpu / n_retrievals if n_retrievals else float("nan"),
        "server_hwm_mib": ((hwm_main or 0) + hwm_children) / 1024 if hwm_main else float("nan"),
        "server_hwm_main_mib": (hwm_main or 0) / 1024,
        "client_hwm_mib": max((r.get("client_hwm_kib") or 0) for r in trials) / 1024 if trials else float("nan"),
        "server_setup_s": srv.get("setup_s"), "server_preprocess_s": srv.get("preprocess_s"),
        "server_ready_wall_s": stats.get("server_ready_wall_s"), "workers": srv.get("workers"),
        "client_setup": {k: setup.get(k) for k in ("wall_ms", "keygen_ms", "setup_material_ms", "session_upload_ms",
                                                   "session_setup_body_bytes", "cpu_s")},
        "playback_model": {},
    }
    if kind == "pir":
        out.update({
            "query_gen_ms": agg(T("query_gen_ms")), "query_upload_ms": agg(T("query_upload_ms")),
            "server_prepare_ms": agg(T("server_prepare_ms")), "client_decode_s": agg([x / 1e3 for x in T("client_decode_ms")]),
            "server_compute_s": agg([r["server_compute_ms"] / 1e3 for r in trials]),
            "server_total_s": agg([r["server_total_ms"] / 1e3 for r in trials]),
            "query_body": agg(B("query_body")), "response_bodies": agg(B("response_bodies")), "blocks": agg(B("blocks")),
            "server_compute_ms_per_block": agg([r["server_compute_ms"] / r["bytes"]["blocks"] for r in trials if r["bytes"].get("blocks")]),
            "client_decode_ms_per_block": agg([r["timings_ms"]["client_decode_ms"] / r["bytes"]["blocks"] for r in trials if r["bytes"].get("blocks")]),
            "response_bytes_per_block": agg([r["bytes"]["response_bodies"] / r["bytes"]["blocks"] for r in trials if r["bytes"].get("blocks")]),
        })
    for th in ("0.256", "1", "2"):
        sel = [r["playback_model"][th] for r in trials if th in r.get("playback_model", {})]
        if sel:
            out["playback_model"][th] = {"startup_s": agg([x["startup_s"] for x in sel]),
                                         "stalls": agg([x["stalls"] for x in sel]), "stall_s": agg([x["stall_s"] for x in sel])}
    return out


def summarize_run(run: Path) -> dict[str, Any]:
    meta = json.loads((run / "run_meta.json").read_text(encoding="utf-8"))
    plan = json.loads((run / "plan.json").read_text(encoding="utf-8"))
    exps = []
    for e in plan["experiments"]:
        s = summarize_experiment(run / e["name"])
        if s:
            exps.append(s)
    failures = [json.loads(l) for l in (run / "failures.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()] \
        if (run / "failures.jsonl").exists() else []
    return {"run": str(run.relative_to(ROOT)), "plan": plan["name"], "description": plan.get("description"),
            "code_sha256": meta.get("code_sha256"), "system": meta.get("system"), "started_utc": meta.get("started_utc"),
            "finished_utc": meta.get("finished_utc"), "failures": failures, "experiments": exps}


FLAT = [("experiment", None), ("label", None), ("num_records", None), ("num_blocks", None), ("trials", None),
        ("all_correct", None), ("total_s", "median"), ("total_s", "min"), ("total_s", "max"), ("ttfa_ms", "median"),
        ("ttfa_ms", "min"), ("ttfa_ms", "max"), ("query_gen_ms", "median"), ("query_upload_ms", "median"),
        ("server_prepare_ms", "median"), ("server_compute_s", "median"), ("server_compute_ms_per_block", "median"),
        ("client_decode_s", "median"), ("client_decode_ms_per_block", "median"), ("query_body", "median"),
        ("wire_tx", "median"), ("wire_rx", "median"), ("expansion_rx", "median"), ("response_bytes_per_block", "median"),
        ("client_cpu_s", "median"), ("server_cpu_s_per_retrieval", None), ("server_hwm_mib", None),
        ("client_hwm_mib", None), ("server_preprocess_s", None), ("server_setup_s", None), ("throughput_x", "median"),
        ("audio_s", "median")]


def flat_row(e: dict) -> dict[str, Any]:
    row = {}
    for key, sub in FLAT:
        v = e.get(key)
        name = key if sub is None else f"{key}_{sub}"
        row[name] = (v or {}).get(sub) if sub and isinstance(v, dict) else v
    pm = e.get("playback_model", {}).get("1")
    row["startup_1s_median"] = pm["startup_s"]["median"] if pm else None
    row["stalls_1s_max"] = pm["stalls"]["max"] if pm else None
    row["stall_s_1s_max"] = pm["stall_s"]["max"] if pm else None
    return row


def write_tables(summary: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    name = summary["plan"]
    (out / f"{name}.json").write_text(json.dumps(summary, indent=1, allow_nan=True), encoding="utf-8")
    rows = [flat_row(e) for e in summary["experiments"]]
    if rows:
        with open(out / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)


# ----------------------------------------------------------------------------- figures
# Figures are drawn at their printed size (report text width = 6.0 in) so that the fonts below are the
# real sizes on paper (body text is 11 pt). No titles inside the images: the LaTeX caption describes
# each figure; panels carry short (a), (b), ... titles. Decimal comma, as in the text.

TEXTWIDTH_IN = 6.0
COLORS = {"opt_u16": "#2f6fdf", "opt_u16_exp": "#1f8a4c", "did_u8": "#b42318", "did_u16": "#e07b39",
          "opt_u8": "#7f9fe8", "conv_track": "#444444", "conv_db": "#9a9a9a"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.dpi": 150, "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.pad_inches": 0.03,
        "font.size": 9, "axes.titlesize": 9.5, "axes.titleweight": "bold", "axes.titlepad": 6,
        "axes.labelsize": 9, "xtick.labelsize": 8.5, "ytick.labelsize": 8.5, "legend.fontsize": 8.5,
        "axes.grid": True, "grid.alpha": 0.3, "grid.linewidth": 0.6, "axes.linewidth": 0.8,
        "lines.linewidth": 1.6, "lines.markersize": 5, "pdf.fonttype": 42, "ps.fonttype": 42,
        "axes.spines.top": False, "axes.spines.right": False,
    })
    return plt


def _comma(x, nd=None):
    if nd is None:
        ax_ = abs(x)
        nd = 0 if ax_ >= 100 else 1 if ax_ >= 10 else 2 if ax_ >= 0.1 else 3 if ax_ >= 0.001 else 4
    s = f"{x:,.{nd}f}"
    return s.replace(",", "\u2009").replace(".", ",").replace("\u2009", ".")


def _comma_axis(ax, axis="x"):
    """Decimal-comma tick labels with the same number of decimals on the whole axis."""
    import math
    from matplotlib.ticker import FuncFormatter

    def fmt(v, _p, a=(ax.xaxis if axis == "x" else ax.yaxis)):
        # smallest number of decimals that represents every tick of the axis exactly
        locs = list(a.get_majorticklocs())
        nd = 0
        while nd < 4 and any(abs(round(t, nd) - t) > 1e-9 * max(1.0, abs(t)) for t in locs):
            nd += 1
        return _comma(v, nd)

    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(FuncFormatter(fmt))


def _log_ticks(ax, axis="y"):
    """Plain-number ticks on a log axis: 1-2-5 per decade for short ranges, decades otherwise."""
    import math
    from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator
    lo, hi = (ax.get_ylim() if axis == "y" else ax.get_xlim())
    lo, hi = max(lo, 1e-12), max(hi, 1e-12)
    decades = math.log10(hi / lo)
    subs = (1, 2, 5) if decades <= 2.6 else (1,)
    ticks = [m * 10 ** e for e in range(math.floor(math.log10(lo)) - 1, math.ceil(math.log10(hi)) + 1)
             for m in subs if lo <= m * 10 ** e <= hi]
    a = ax.yaxis if axis == "y" else ax.xaxis
    a.set_major_locator(FixedLocator(ticks))
    a.set_minor_locator(NullLocator())
    a.set_major_formatter(FuncFormatter(lambda v, _p: _comma(v, 0 if v >= 1 else (1 if v >= 0.1 else (2 if v >= 0.01 else 3)))))


def save(fig, figdir: Path, name: str, sources: list[str]) -> None:
    figdir.mkdir(parents=True, exist_ok=True)
    fig.savefig(figdir / f"{name}.pdf")
    fig.savefig(figdir / f"{name}.png")
    (figdir / f"{name}.sources.txt").write_text("\n".join(sources) + "\n", encoding="utf-8")


def _hbars(ax, labels, med, lo, hi, colors, unit, log=True, set_labels=True, invert=True):
    import numpy as np
    y = np.arange(len(med))
    has_range = any(lo) or any(hi)  # no error bars (and no stray caps) when only medians are drawn
    ax.barh(y, med, xerr=[lo, hi] if has_range else None, capsize=2.5, color=colors, height=0.62,
            error_kw={"elinewidth": 0.9, "capthick": 0.9})
    if set_labels:
        ax.set_yticks(y)
        ax.set_yticklabels(labels)
    if invert:
        ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    if log:
        ax.set_xscale("log")
    xmax = max(m + h for m, h in zip(med, hi))
    for yi, m, h in zip(y, med, hi):
        ax.text((m + h) * (1.12 if log else 1.0) + (0 if log else xmax * 0.02), yi, f"{_comma(m)} {unit}",
                va="center", ha="left", fontsize=8)
    if log:
        ax.set_xlim(min(med) / 2.5, xmax * 9)
    else:
        ax.set_xlim(0, xmax * 1.32)


def fig_e1(summary: dict, figdir: Path) -> None:
    plt = _plt()
    names = {"did_u8": "Didático, 1 B/slot", "did_u16": "Didático, 2 B/slot", "opt_u8": "Otimizado, 1 B/slot",
             "opt_u16": "Otimizado, 2 B/slot", "opt_u16_exp": "Otimizado, 2 B/slot,\nconsulta expandida",
             "conv_track": "Convencional: faixa", "conv_db": "Convencional:\nbanco inteiro"}
    E = [e for e in summary["experiments"] if e["experiment"] in names]
    labels = [names[e["experiment"]] for e in E]
    colors = [COLORS[e["experiment"]] for e in E]
    # Two panels side by side that share the configuration labels (revision 2: compact figure; the time to
    # the first playable audio is in Table tab:e1). Units in the panel titles; the caption states that the
    # axes are logarithmic.
    fig, axes = plt.subplots(1, 2, figsize=(TEXTWIDTH_IN, 2.75), sharey=True, constrained_layout=True)
    panels = (("total_s", "(a) Recuperação completa (s)", "s", 1),
              ("wire_rx", "(b) Bytes recebidos (MB)", "MB", 1e-6))
    for i, (ax, (key, title, unit, scale)) in enumerate(zip(axes, panels)):
        med = [e[key]["median"] * scale for e in E]
        lo = [(e[key]["median"] - e[key]["min"]) * scale for e in E]
        hi = [(e[key]["max"] - e[key]["median"]) * scale for e in E]
        _hbars(ax, labels, med, lo, hi, colors, unit, set_labels=(i == 0), invert=(i == 0))
        ax.set_title(title, loc="left")
        ax.tick_params(axis="y", length=0)
        _log_ticks(ax, "x")  # plain numbers with a decimal comma, as in the other figures
    fig.get_layout_engine().set(w_pad=0.08, wspace=0.06)
    save(fig, figdir, "e1_overview", [summary["run"]])
    plt.close(fig)


def fig_e2(summary: dict, figdir: Path) -> None:
    plt = _plt()
    from matplotlib.ticker import MaxNLocator
    names = {"ref": "Referência", "db_plain": "Banco sem NTT", "db_raw": "Sem pré-processamento",
             "no_modsw": "Sem troca de módulo", "no_seed": "Consulta sem semente", "workers2": "2 processos",
             "workers4": "4 processos", "batch1": "1 bloco por pedido", "batch32": "32 blocos por pedido",
             "w4_batch32": "4 processos, 32 blocos", "stop_policy": "Política stop"}
    E = [e for e in summary["experiments"] if e["experiment"] in names]
    labels = [names[e["experiment"]] for e in E]
    colors = ["#1f8a4c" if e["experiment"] == "ref" else "#2f6fdf" for e in E]
    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH_IN, 6.2), sharey=True, constrained_layout=True)
    axes = axes.ravel()
    panels = (("total_s", "(a) Recuperação (s)", 1), ("server_compute_ms_per_block", "(b) Servidor/bloco (ms)", 1),
              ("wire_rx", "(c) Recebido (MB)", 1e-6), ("server_hwm_mib", "(d) Memória servidor (MiB)", 1))
    for i, (ax, (key, title, scale)) in enumerate(zip(axes, panels)):
        med = [(e[key]["median"] if isinstance(e[key], dict) else e[key]) * scale for e in E]
        _hbars(ax, labels, med, [0] * len(E), [0] * len(E), colors, "", log=False, set_labels=(i == 0), invert=(i == 0))
        ax.set_title(title, loc="left")
        ax.set_xlim(0, max(med) * 1.38)
        ax.xaxis.set_major_locator(MaxNLocator(4))
        _comma_axis(ax, "x")
    fig.get_layout_engine().set(h_pad=0.1, hspace=0.08, w_pad=0.1, wspace=0.12)
    save(fig, figdir, "e2_ablation", [summary["run"]])
    plt.close(fig)


def _legend_below(fig, axes, ncol):
    # compact spacing so that the legend is not wider than the text (6 in)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="outside lower center", ncol=ncol, frameon=False,
               handlelength=1.8, handletextpad=0.5, columnspacing=1.4)


def fig_e3(summary: dict, figdir: Path) -> None:
    plt = _plt()
    by = {}
    for e in summary["experiments"]:
        base, r = e["experiment"].rsplit("_R", 1)
        by.setdefault(base, []).append((int(r), e))
    style = {"opt_u16": ("Otimizado, seletora", "o"), "opt_u16_exp": ("Otimizado, expandida", "s"),
             "did_u8": ("Didático, 1 B/slot", "^"), "conv_track": ("Convencional: faixa", "x"),
             "conv_db": ("Convencional: banco inteiro", "d")}
    # Three panels in one row (revision 2: compact figure; the query sizes are in the text and in
    # Table tab:e3-bytes, which is still generated).
    # (0.25 in narrower than the text: the tight bounding box grows by the last panel title at the right edge)
    fig, axes = plt.subplots(1, 3, figsize=(TEXTWIDTH_IN - 0.25, 2.85), constrained_layout=True)
    for base, (lab, mk) in style.items():
        es = sorted(by.get(base, []), key=lambda x: x[0])
        if not es:
            continue
        xs = [r for r, _ in es]
        kw = dict(marker=mk, color=COLORS[base], label=lab)
        axes[0].plot(xs, [e["total_s"]["median"] for _, e in es], **kw)
        axes[1].plot(xs, [e["wire_rx"]["median"] / 1e6 for _, e in es], **kw)
        if base.startswith(("opt", "did")):
            axes[2].plot(xs, [e["server_compute_ms_per_block"]["median"] for _, e in es], **kw)
        else:
            axes[2].plot([], [], **kw)
    titles = ("(a) Recuperação (s)", "(b) Recebido (MB)", "(c) Servidor/bloco (ms)")
    for ax, t in zip(axes, titles):
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_title(t, loc="left")
        ax.set_xlabel("faixas no catálogo (R)")
        ax.set_xticks([2, 5, 10, 20, 40, 80, 160])
        ax.set_xticklabels(["2", "5", "10", "20", "40", "80", "160"], fontsize=8)
        ax.minorticks_off()
        _log_ticks(ax, "y")
    # legend no wider than the 6.0 in text width (tighter column spacing than _legend_below)
    h, l = axes[0].get_legend_handles_labels()
    fig.legend(h, l, loc="outside lower center", ncol=3, frameon=False,
               handlelength=1.6, handletextpad=0.4, columnspacing=0.9)
    save(fig, figdir, "e3_catalog_scaling", [summary["run"]])
    plt.close(fig)


def fig_e4(summary: dict, figdir: Path) -> None:
    plt = _plt()
    by = {}
    for e in summary["experiments"]:
        base, d = e["experiment"].rsplit("_d", 1)
        by.setdefault(base, []).append((int(d), e))
    style = {"opt_u16": ("Otimizado, 2 B/slot", "o"), "did_u8": ("Didático, 1 B/slot", "^"),
             "conv_track": ("Convencional: faixa", "x")}
    fig, axes = plt.subplots(1, 3, figsize=(TEXTWIDTH_IN, 2.9), constrained_layout=True)
    for base, (lab, mk) in style.items():
        es = sorted(by.get(base, []))
        if not es:
            continue
        xs = [d for d, _ in es]
        kw = dict(marker=mk, color=COLORS[base], label=lab)
        axes[0].plot(xs, [e["total_s"]["median"] for _, e in es], **kw)
        axes[1].plot(xs, [e["ttfa_ms"]["median"] for _, e in es], **kw)
        axes[2].plot(xs, [e["wire_rx"]["median"] / 1e6 for _, e in es], **kw)
    for ax, t in zip(axes, ("(a) Recuperação (s)", "(b) Primeiro áudio (ms)", "(c) Recebido (MB)")):
        ax.set_title(t, loc="left")
        ax.set_xlabel("duração das faixas (s)")
        ax.set_yscale("log")
        ax.set_xticks([10, 30, 60, 120])
        _log_ticks(ax, "y")
    _legend_below(fig, axes, 3)
    save(fig, figdir, "e4_duration_scaling", [summary["run"]])
    plt.close(fig)


def fig_e6(summary: dict, figdir: Path) -> None:
    plt = _plt()
    by = {}
    for e in summary["experiments"]:
        base, rate = e["experiment"].rsplit("_", 1)
        by.setdefault(base, []).append((float(rate.replace("mbps", "")), e))
    style = {"opt_u16": ("Otimizado, 2 B/slot", "o"), "did_u8": ("Didático, 1 B/slot", "^"),
             "conv_track": ("Convencional: faixa", "x")}
    fig, axes = plt.subplots(2, 2, figsize=(TEXTWIDTH_IN, 5.1), constrained_layout=True)
    axes = axes.ravel()
    for base, (lab, mk) in style.items():
        es = sorted(by.get(base, []))
        if not es:
            continue
        xs = [r for r, _ in es]
        kw = dict(marker=mk, color=COLORS[base], label=lab)
        axes[0].plot(xs, [e["playback_model"]["1"]["startup_s"]["median"] for _, e in es], **kw)
        axes[1].plot(xs, [e["playback_model"]["1"]["stall_s"]["median"] for _, e in es], **kw)
        axes[2].plot(xs, [e["throughput_x"]["median"] for _, e in es], **kw)
    axes[2].axhline(1.0, color="k", lw=0.9, ls="--")
    axes[2].text(5.1, 1.15, "tempo real", fontsize=8)
    axes[2].set_yscale("log")
    for ax, t in zip(axes[:3], ("(a) Início da reprodução (s)", "(b) Tempo parado (s)",
                                "(c) Vazão (× tempo real)")):
        ax.set_title(t, loc="left")
        ax.set_xlabel("enlace emulado (Mbit/s)")
        ax.set_xscale("log")
        ax.set_xticks([5, 10, 25])
        ax.set_xticklabels(["5", "10", "25"])
        ax.minorticks_off()
    _log_ticks(axes[2], "y")
    _comma_axis(axes[0], "y")
    _comma_axis(axes[1], "y")
    axes[3].axis("off")
    h, l = axes[0].get_legend_handles_labels()
    axes[3].legend(h, l, loc="center", frameon=False, fontsize=9,
                   title="(a) e (b): modelo com buffer\ninicial de 1 s", title_fontsize=8.5)
    fig.get_layout_engine().set(h_pad=0.12, hspace=0.1)  # room between the rows of panels
    save(fig, figdir, "e6_link_emulation", [summary["run"]])
    plt.close(fig)


def fig_timeline(run: Path, exps: list[str], figdir: Path, name: str, legend: dict | None = None) -> None:
    """Audio available vs time for the first trial of each experiment (progressive playback).

    `legend` holds matplotlib legend placement keywords (default: upper left)."""
    plt = _plt()
    names = {"opt_u16": "Otimizado, 2 B/slot", "did_u8": "Didático, 1 B/slot", "conv_track": "Convencional: faixa"}
    fig, ax = plt.subplots(figsize=(4.6, 3.0), constrained_layout=True)
    tmax = 0.0
    for exp in exps:
        rows = [json.loads(l) for l in (run / exp / "client.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        tr = next((r for r in rows if r["type"] == "trial"), None)
        if not tr:
            continue
        ev = tr["events"]
        acc, xs, ys = 0, [0.0], [0.0]
        for td, nb in zip(ev["decoded"], ev["pcm_bytes"]):
            acc += nb
            xs.append(td)
            ys.append(acc / 32000)
        tmax = max(tmax, xs[-1])
        base = exp.rsplit("_", 1)[0] if exp.endswith("mbps") else exp
        ax.step(xs, ys, where="post", label=names.get(base, exp), color=COLORS.get(base))
    ax.plot([0, tmax], [0, tmax], "k--", lw=0.9, label="consumo em tempo real")
    ax.set_xlabel("tempo desde o clique (s)")
    ax.set_ylabel("áudio decifrado disponível (s)")
    _comma_axis(ax, "x")
    _comma_axis(ax, "y")
    ax.legend(frameon=False, **(legend or {"loc": "upper left"}))
    save(fig, figdir, name, [str(run.relative_to(ROOT))])
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default=str(ROOT / "results" / "summary"))
    ap.add_argument("--figures", default=str(ROOT / "results" / "figures"))
    a = ap.parse_args(argv)
    figdir = Path(a.figures)
    for r in a.runs:
        run = Path(r).resolve()
        s = summarize_run(run)
        write_tables(s, Path(a.out))
        print(f"{s['plan']}: {len(s['experiments'])} experiments, failures={len(s['failures'])}")
        fn = {"e1_main": fig_e1, "e2_ablation": fig_e2, "e3_catalog": fig_e3, "e4_duration": fig_e4, "e6_link": fig_e6}.get(s["plan"])
        if fn:
            fn(s, figdir)
        if s["plan"] == "e6_link":
            fig_timeline(run, ["opt_u16_5mbps", "did_u8_5mbps", "conv_track_5mbps"], figdir, "e6_timeline_5mbps")
        if s["plan"] == "e1_main":
            # legend in the empty area between the curves and the real-time line
            fig_timeline(run, ["opt_u16", "did_u8"], figdir, "e1_timeline",
                         legend={"loc": "lower right", "bbox_to_anchor": (1.0, 0.2)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
