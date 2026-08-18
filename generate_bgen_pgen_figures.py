#!/usr/bin/env python3
"""Generate BGEN and PGEN publication figures from the tracked results.

Figures accompany the tables in the blog post rather than replacing them: a bar
gives the shape at a glance, the table keeps the digits the claims rest on.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
OUT = Path.home() / "CLionProjects/polars-bio/docs/blog/posts/figures/genotype-readers-2026-08"
HIGHLIGHT = "#f58518"
MUTED = "#9ca3af"


def _bars(ax, labels, values, title, xlabel):
    colors = [HIGHLIGHT if "polars-bio" in name else MUTED for name in labels]
    bars = ax.barh(labels, values, color=colors)
    ax.invert_yaxis()
    ax.set_xlabel(xlabel)
    ax.set_title(title, loc="left", fontsize=11)
    ax.spines[["top", "right"]].set_visible(False)
    for bar, value in zip(bars, values):
        ax.text(
            bar.get_width() * 1.01,
            bar.get_y() + bar.get_height() / 2,
            f"{value:.3f}",
            va="center",
            fontsize=9,
        )
    ax.set_xlim(0, max(values) * 1.18)


def bgen(results):
    r = results["dosage"]
    labels = ["polars-bio", "bgen", "snputils"]
    values = [
        r["polars-bio-t1"]["time_seconds_median"],
        r["bgen"]["time_seconds_median"],
        r["snputils"]["time_seconds_median"],
    ]
    fig, ax = plt.subplots(figsize=(7, 2.4))
    _bars(ax, labels, values, "BGEN dosage, one thread — chr22, 2.53B genotypes", "seconds (lower is better)")
    fig.tight_layout()
    fig.savefig(OUT / "bgen-one-thread.png", dpi=160)
    plt.close(fig)


def bgen_scaling(results):
    r = results["dosage"]
    parts = [1, 2, 4, 8]
    times = [r[f"polars-bio-t{p}"]["time_seconds_median"] for p in parts]
    base = times[0]
    speedups = [base / t for t in times]
    fig, (left, right) = plt.subplots(1, 2, figsize=(9, 3.1))
    left.plot(parts, times, marker="o", color=HIGHLIGHT)
    left.set_xscale("log", base=2)
    left.set_xticks(parts, [str(p) for p in parts])
    left.set_xlabel("partitions")
    left.set_ylabel("seconds")
    left.set_title("polars-bio BGEN scaling", loc="left", fontsize=11)
    left.spines[["top", "right"]].set_visible(False)
    right.plot(parts, speedups, marker="o", color=HIGHLIGHT, label="measured")
    right.plot(parts, parts, linestyle="--", color=MUTED, label="linear")
    right.set_xscale("log", base=2)
    right.set_xticks(parts, [str(p) for p in parts])
    right.set_xlabel("partitions")
    right.set_ylabel("speedup vs 1 partition")
    right.set_title(f"{speedups[-1]:.2f}× at eight", loc="left", fontsize=11)
    right.legend(frameon=False, fontsize=9)
    right.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(OUT / "bgen-scaling.png", dpi=160)
    plt.close(fig)


def pgen(results):
    fig, axes = plt.subplots(1, 2, figsize=(9, 2.6))
    for ax, mode, title in zip(axes, ("hardcall", "dosage"), ("PGEN hardcall (int8)", "PGEN dosage (float32)")):
        r = results[mode]
        labels = ["polars-bio", "pgenlib", "snputils"]
        values = [
            r["polars-bio-t1"]["time_seconds_median"],
            r["pgenlib"]["time_seconds_median"],
            r["snputils"]["time_seconds_median"],
        ]
        _bars(ax, labels, values, title, "seconds")
    fig.tight_layout()
    fig.savefig(OUT / "pgen-one-thread.png", dpi=160)
    plt.close(fig)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    bgen_results = json.loads((HERE / "results/bgen_matrix_reader.json").read_text())["results"]
    pgen_results = json.loads((HERE / "results/pgen_import_fix.json").read_text())["results"]
    bgen(bgen_results)
    bgen_scaling(bgen_results)
    pgen(pgen_results)
    print(f"wrote figures to {OUT}")


if __name__ == "__main__":
    main()
