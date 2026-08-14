#!/usr/bin/env python3
"""Generate BCF publication figures from the tracked genotype-reader results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

READERS = ("pysam", "cyvcf2", "oxbow", "polars-bio", "snputils")
LABELS = {
    "pysam": "pysam",
    "cyvcf2": "cyvcf2",
    "oxbow": "Oxbow",
    "polars-bio": "polars-bio",
    "snputils": "snputils",
}
BAR_COLORS = [
    "#9ca3af" if reader != "polars-bio" else "#f58518" for reader in READERS
]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def bcf_bars(payload: dict, metric: str, ylabel: str, output: Path) -> None:
    x = np.arange(len(READERS))
    figure, axis = plt.subplots(figsize=(9.5, 5.5), constrained_layout=True)
    values = [
        payload["results"].get("BCF", {}).get(reader, {}).get(metric, np.nan)
        for reader in READERS
    ]
    bars = axis.bar(x, values, width=0.62, color=BAR_COLORS)
    axis.bar_label(
        bars,
        labels=["" if np.isnan(value) else f"{value:g}" for value in values],
        padding=3,
        fontsize=9,
    )

    axis.set_ylabel(ylabel)
    axis.set_xticks(x, [LABELS[reader] for reader in READERS])
    axis.set_ylim(bottom=0)
    axis.margins(y=0.12)
    axis.grid(axis="y", alpha=0.22)
    axis.spines[["top", "right"]].set_visible(False)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180, transparent=False)
    plt.close(figure)


def scaling_plot(results_dir: Path, output: Path) -> None:
    threads = (1, 2, 4, 8)
    payloads = [
        load_json(results_dir / f"bcf_benchmark_t{thread}.json") for thread in threads
    ]
    polars_times = [
        payload["results"]["polars-bio"]["time_seconds_median"] for payload in payloads
    ]
    snputils_times = [
        payload["results"]["snputils"]["time_seconds_median"] for payload in payloads
    ]

    figure, axis = plt.subplots(figsize=(8.5, 5), constrained_layout=True)
    axis.plot(threads, polars_times, marker="o", linewidth=2.2, label="polars-bio")
    axis.plot(
        threads,
        snputils_times,
        marker="o",
        linewidth=2.2,
        label="snputils (serial control)",
    )
    axis.set_xlabel("Configured thread/partition count")
    axis.set_ylabel("Median wall time (seconds)")
    axis.set_xticks(threads)
    axis.set_ylim(bottom=0)
    axis.grid(alpha=0.25)
    axis.legend(frameon=False)
    axis.spines[["top", "right"]].set_visible(False)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180, transparent=False)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="results/genotype_reader_benchmark.json", type=Path
    )
    parser.add_argument("--scaling-dir", default="results", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    payload = load_json(args.input)
    bcf_bars(
        payload,
        "time_seconds_median",
        "Median wall time (seconds)",
        args.output_dir / "bcf-reader-time.png",
    )
    bcf_bars(
        payload,
        "peak_rss_mb_median",
        "Peak RSS (MB)",
        args.output_dir / "bcf-reader-memory.png",
    )
    scaling_plot(args.scaling_dir, args.output_dir / "bcf-thread-scaling.png")


if __name__ == "__main__":
    main()
