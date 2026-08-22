"""Shared utilities for isolated BigWig/BigBed scalability benchmarks."""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from collections.abc import Callable

Scalar = int | float | str
BenchmarkSample = tuple[dict[str, Scalar], dict[str, int | float]]

BIGWIG_PATH = os.environ.get(
    "BIGWIG_PATH",
    "/Users/mwiewior/research/data/BBI/"
    "GSM7256643_ENCFF713VEX_fold_change_over_control_GRCh38.bigWig",
)
BIGBED_PATH = os.environ.get(
    "BIGBED_PATH", "/Users/mwiewior/research/data/BBI/ENCFF001JBR.bigBed"
)

FORMATS = ("bigwig", "bigbed")
WORKLOADS = (
    "arrow_stream_all",
    "polars_count",
    "polars_aggregate_all",
    "polars_collect_all",
)


def input_path(format_name: str) -> str:
    if format_name == "bigwig":
        return BIGWIG_PATH
    if format_name == "bigbed":
        return BIGBED_PATH
    raise ValueError(f"unsupported BBI format: {format_name!r}")


def peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        return usage.ru_maxrss / (1024 * 1024)
    return usage.ru_maxrss / 1024


def run_bbi_benchmark(
    operation: Callable[[], BenchmarkSample],
    *,
    format_name: str,
    workload: str,
    threads: int,
    iterations: int,
    physical_partition_info: Callable[[], dict[str, int | list[int]]],
    content_fingerprint: Callable[[], dict[str, Scalar]],
    environment_info: Callable[[], dict[str, object]],
) -> None:
    """Time repeated scans and emit one machine-readable child result."""
    if iterations < 1:
        raise ValueError("BBI_ITERATIONS must be positive")

    samples = []
    started = time.perf_counter()
    for _ in range(iterations):
        samples.append(operation())
    elapsed = time.perf_counter() - started

    fingerprints = [fingerprint for fingerprint, _ in samples]
    if any(result != fingerprints[0] for result in fingerprints[1:]):
        raise AssertionError("repeated BBI scans produced different fingerprints")
    diagnostics = [diagnostic for _, diagnostic in samples]
    if any(result != diagnostics[0] for result in diagnostics[1:]):
        raise AssertionError("repeated BBI scans produced different diagnostics")

    measured_peak_rss_mb = peak_rss_mb()
    partition_info = physical_partition_info()
    verified_content = content_fingerprint()
    result = {
        "format": format_name,
        "workload": workload,
        "threads": threads,
        **partition_info,
        "iterations": iterations,
        "time_seconds": elapsed / iterations,
        "total_time_seconds": elapsed,
        "peak_rss_mb": measured_peak_rss_mb,
        "fingerprint": fingerprints[0],
        "content_fingerprint": verified_content,
        "diagnostics": diagnostics[0],
        "environment": environment_info(),
        "thread_limits": {
            name: int(os.environ[name])
            for name in (
                "POLARS_MAX_THREADS",
                "RAYON_NUM_THREADS",
                "TOKIO_WORKER_THREADS",
            )
        },
    }
    print(f"BBI_RESULT:{json.dumps(result, sort_keys=True)}")
