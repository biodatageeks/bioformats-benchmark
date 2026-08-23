#!/usr/bin/env python3
"""Generate BigWig/BigBed t=1..8 scalability figures from benchmark JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from benchmarks.bbi_common import fingerprints_match

COLORS = {
    "arrow_stream_all": "#4c78a8",
    "polars_count": "#f58518",
    "polars_aggregate_all": "#54a24b",
    "polars_collect_all": "#e45756",
}
IDEAL = "#9ca3af"
WORKLOAD_LABELS = {
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
        "partitions",
        "files",
        "platform",
        "machine",
        "logical_cpu_count",
        "physical_cpu_count",
        "memory_total_bytes",
        "python",
        "versions",
    )
    comparable_environment = (
        "platform",
        "machine",
        "logical_cpu_count",
        "physical_cpu_count",
        "memory_total_bytes",
    )
    for payload, path in zip(payloads, paths):
        schema_version = payload.get("schema_version")
        if schema_version not in (1, 2):
            raise ValueError(
                f"{path} has unsupported schema_version={schema_version!r}"
            )
        if len(payloads) > 1 and schema_version != 2:
            raise ValueError(
                f"{path} cannot be compared without schema-v2 harness provenance"
            )
        metadata = payload.get("metadata", {})
        missing = [field for field in required_environment if field not in metadata]
        requires_build_metadata = schema_version == 2 or len(payloads) > 1
        if requires_build_metadata and "polars_bio_build" not in metadata:
            missing.append("polars_bio_build")
        if len(payloads) > 1 and "harness" not in metadata:
            missing.append("harness")
        if missing:
            raise ValueError(f"{path} is missing environment metadata: {missing}")
        if sorted(metadata["partitions"]) != sorted(reference["partitions"]):
            raise ValueError(f"{path} uses a different partition sweep")
        for field in comparable_environment:
            if metadata[field] != reference[field]:
                raise ValueError(f"{path} uses different benchmark hardware: {field}")
        if metadata["python"] != reference["python"]:
            raise ValueError(f"{path} uses a different Python runtime")
        for field in ("max_system_cpu_percent", "cpu_quiet_samples"):
            if metadata.get(field) != reference.get(field):
                raise ValueError(
                    f"{path} uses a different CPU admission protocol: {field}"
                )
        if metadata.get("physical_partition_probe") != reference.get(
            "physical_partition_probe"
        ):
            raise ValueError(f"{path} uses a different physical partition probe")
        for dependency in ("polars", "pyarrow"):
            if metadata["versions"].get(dependency) != reference["versions"].get(
                dependency
            ):
                raise ValueError(
                    f"{path} uses a different {dependency} runtime version"
                )
        if requires_build_metadata:
            for field in ("declared_profile", "declared_rustflags"):
                if metadata["polars_bio_build"].get(field) != reference[
                    "polars_bio_build"
                ].get(field):
                    raise ValueError(
                        f"{path} uses a different polars-bio build setting: {field}"
                    )
        expectation = metadata.get("physical_partition_expectation")
        if schema_version == 2 and expectation not in ("requested", "serial"):
            raise ValueError(
                f"{path} has unsupported physical partition expectation: "
                f"{expectation!r}"
            )

    for left_index, left_payload in enumerate(payloads):
        left_metadata = left_payload["metadata"]
        for right_index in range(left_index + 1, len(payloads)):
            right_payload = payloads[right_index]
            right_metadata = right_payload["metadata"]
            right_path = paths[right_index]
            if (
                left_payload["schema_version"] == 2
                and right_payload["schema_version"] == 2
                and left_metadata["harness"] != right_metadata["harness"]
            ):
                raise ValueError(f"{right_path} uses a different benchmark harness")
            common_formats = set(left_metadata["files"]) & set(right_metadata["files"])
            for format_name in common_formats:
                expected = left_metadata["files"][format_name]
                actual = right_metadata["files"][format_name]
                for field in ("sha256", "size_bytes"):
                    if actual.get(field) != expected.get(field):
                        raise ValueError(
                            f"{right_path} uses a different {format_name} fixture: "
                            f"{field}"
                        )

                expected_content = strongest_format_fingerprint(
                    left_payload, format_name
                )
                actual_content = strongest_format_fingerprint(
                    right_payload, format_name
                )
                common_fields = expected_content.keys() & actual_content.keys()
                if not common_fields:
                    raise ValueError(
                        f"{right_path} has no comparable {format_name} content "
                        "fingerprint"
                    )
                expected_common = {key: expected_content[key] for key in common_fields}
                actual_common = {key: actual_content[key] for key in common_fields}
                if not fingerprints_match(expected_common, actual_common):
                    raise ValueError(
                        f"{right_path} contains different {format_name} content"
                    )

                left_workloads = left_payload["results"].get(format_name, {})
                right_workloads = right_payload["results"].get(format_name, {})
                for workload in left_workloads.keys() & right_workloads.keys():
                    left_summaries = left_workloads[workload]
                    right_summaries = right_workloads[workload]
                    for partition in left_summaries.keys() & right_summaries.keys():
                        expected_iterations = left_summaries[partition].get(
                            "iterations_per_process"
                        )
                        actual_iterations = right_summaries[partition].get(
                            "iterations_per_process"
                        )
                        if (
                            expected_iterations is None
                            or actual_iterations is None
                            or actual_iterations != expected_iterations
                        ):
                            raise ValueError(
                                f"{right_path} uses a different iteration protocol for "
                                f"{format_name}/{workload}/{partition}"
                            )
                        expected_runs = left_summaries[partition].get("runs")
                        actual_runs = right_summaries[partition].get("runs")
                        if (
                            expected_runs is None
                            or actual_runs is None
                            or actual_runs != expected_runs
                        ):
                            raise ValueError(
                                f"{right_path} uses a different fresh-process sampling "
                                f"protocol for {format_name}/{workload}/{partition}"
                            )


def strongest_format_fingerprint(payload: dict, format_name: str) -> dict:
    """Return the strongest recorded correctness fingerprint for one format."""
    checks = [
        check
        for key, check in payload.get("verification", {}).items()
        if key.startswith(f"{format_name}:")
    ]
    content_fingerprints = [
        check["content_fingerprint"]
        for check in checks
        if check.get("content_fingerprint")
    ]
    candidates = content_fingerprints or [
        check["fingerprint"] for check in checks if check.get("fingerprint")
    ]
    if not candidates:
        raise ValueError(f"missing {format_name} correctness fingerprint")
    return max(candidates, key=len)


def plot_partitions(payload: dict) -> list[int]:
    """Return monotonically increasing partition counts for connected curves."""
    return sorted(payload["metadata"]["partitions"])


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
        partitions = plot_partitions(payload)
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
        axis.legend(
            frameon=True,
            framealpha=0.9,
            facecolor="white",
            edgecolor="none",
            fontsize=8,
        )

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
