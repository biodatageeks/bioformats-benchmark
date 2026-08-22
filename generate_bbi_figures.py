#!/usr/bin/env python3
"""Generate BigWig/BigBed t=1..8 scalability figures from benchmark JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

COLORS = {
    "count": "#f58518",
    "decode": "#54a24b",
    "arrow_stream_all": "#4c78a8",
    "polars_count": "#f58518",
    "polars_aggregate_all": "#54a24b",
    "polars_collect_all": "#e45756",
}
IDEAL = "#9ca3af"
WORKLOAD_LABELS = {
    "count": "Polars count",
    "decode": "Polars decode",
    "arrow_stream_all": "Arrow stream, all columns",
    "polars_count": "Polars count",
    "polars_aggregate_all": "Polars aggregate, all columns",
    "polars_collect_all": "Polars collect, all columns",
}


def load_payloads(paths: list[Path]) -> list[dict]:
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    validate_payloads(payloads, paths)
    return payloads


def validate_payloads(payloads: list[dict], paths: list[Path]) -> None:
    """Reject plots whose inputs cannot support a fair visual comparison."""
    if not payloads:
        raise ValueError("at least one benchmark payload is required")
    labels = [payload.get("metadata", {}).get("label") for payload in payloads]
    if any(not label for label in labels) or len(set(labels)) != len(labels):
        raise ValueError("benchmark payload labels must be present and unique")

    reference = payloads[0]["metadata"]
    required_environment = (
        "platform",
        "machine",
        "logical_cpu_count",
        "physical_cpu_count",
        "memory_total_bytes",
        "versions",
    )
    for payload, path in zip(payloads, paths):
        schema_version = payload.get("schema_version")
        if schema_version not in (1, 2):
            raise ValueError(
                f"{path} has unsupported schema_version={schema_version!r}"
            )
        metadata = payload.get("metadata", {})
        missing = [field for field in required_environment if field not in metadata]
        if missing:
            raise ValueError(f"{path} is missing environment metadata: {missing}")
        if metadata["partitions"] != reference["partitions"]:
            raise ValueError(f"{path} uses a different partition sweep")
        for field in required_environment[:-1]:
            if metadata[field] != reference[field]:
                raise ValueError(f"{path} uses different benchmark hardware: {field}")

        for format_name in set(reference["files"]) & set(metadata["files"]):
            expected = reference["files"][format_name]
            actual = metadata["files"][format_name]
            for field in ("sha256", "size_bytes"):
                if actual.get(field) != expected.get(field):
                    raise ValueError(
                        f"{path} uses a different {format_name} fixture: {field}"
                    )


def plot_format(payloads: list[dict], format_name: str, output: Path) -> None:
    figure, axes = plt.subplots(2, 2, figsize=(10, 7), constrained_layout=True)
    metrics = (
        ("time", "Median wall time", "seconds"),
        ("throughput", "Throughput", "rows / second"),
        ("speedup", "Speedup vs t=1", "speedup"),
        ("efficiency", "Parallel efficiency", "speedup / t"),
    )

    for payload_index, payload in enumerate(payloads):
        label = payload["metadata"].get("label", "run")
        partitions = payload["metadata"]["partitions"]
        workloads = payload["results"].get(format_name, {})
        for workload, summaries in workloads.items():
            scaling = payload["scaling"][format_name][workload]
            keys = [f"t{partition}" for partition in partitions]
            series = {
                "time": [summaries[key]["time_seconds_median"] for key in keys],
                "throughput": [scaling[key]["rows_per_second"] for key in keys],
                "speedup": [scaling[key]["speedup_vs_t1"] for key in keys],
                "efficiency": [scaling[key]["parallel_efficiency"] for key in keys],
            }
            workload_label = WORKLOAD_LABELS.get(workload, workload)
            line_label = (
                workload_label if len(payloads) == 1 else f"{label}: {workload_label}"
            )
            for axis, (metric, _, _) in zip(axes.flat, metrics):
                axis.plot(
                    partitions,
                    series[metric],
                    marker="o",
                    label=line_label,
                    color=COLORS.get(workload),
                    linestyle=("-", "--", ":", "-.")[payload_index % 4],
                )

    all_partitions = sorted(
        {
            partition
            for payload in payloads
            for partition in payload["metadata"]["partitions"]
        }
    )
    axes[1, 0].plot(
        all_partitions,
        all_partitions,
        linestyle="--",
        color=IDEAL,
        label="ideal linear",
    )
    for axis, (_, title, ylabel) in zip(axes.flat, metrics):
        axis.set_title(title, loc="left", fontsize=11)
        axis.set_xlabel("DataFusion target partitions")
        axis.set_ylabel(ylabel)
        axis.set_xticks(all_partitions)
        axis.grid(alpha=0.22)
        axis.spines[["top", "right"]].set_visible(False)
        axis.legend(frameon=False, fontsize=8)

    figure.suptitle(f"polars-bio {format_name} scalability", fontsize=14)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", nargs="+", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    payloads = load_payloads(args.input)
    formats = sorted(
        {format_name for payload in payloads for format_name in payload["results"]}
    )
    for format_name in formats:
        plot_format(
            payloads,
            format_name,
            args.output_dir / f"{format_name}-scaling.png",
        )
    print(f"wrote {len(formats)} figure(s) to {args.output_dir}")


if __name__ == "__main__":
    main()
