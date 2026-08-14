"""Common utilities for bioformats reading benchmarks."""

import json
import os
import resource
import sys
import time

# Data file paths (format-specific env vars override defaults)
BAM_PATH = os.environ.get(
    "BAM_PATH", "/Users/mwiewior/research/data/WES/NA12878.proper.wes.md.chr1.bam"
)
VCF_PATH = os.environ.get(
    "VCF_PATH", "/Users/mwiewior/research/data/VCF/homo_sapiens-chr1.vcf.gz"
)
BCF_PATH = os.environ.get(
    "BCF_PATH", "/Users/mwiewior/research/data/BCF/ALL.chr22.phased.bcf"
)
FASTQ_PATH = os.environ.get(
    "FASTQ_PATH", "/Users/mwiewior/research/data/FASTQ/ERR194158.fastq.bgz"
)
# biobear needs .gz extension — use the regular gzip version
FASTQ_PATH_BB = os.environ.get(
    "FASTQ_PATH_BB", "/Users/mwiewior/research/data/FASTQ/ERR194158.fastq.gz"
)

# Benchmark variant (controlled by BENCH_VARIANT env var)
# BAM: "with_tags" or "without_tags"
# VCF: "with_info" or "without_info"
# BCF: "dosage"
# FASTQ: "all_columns"
BENCH_VARIANT = os.environ.get("BENCH_VARIANT", "with_tags")

# BAM tags present in the test file
BAM_TAGS = [
    "E2",
    "MD",
    "MQ",
    "NM",
    "OC",
    "OP",
    "OQ",
    "PG",
    "RG",
    "UQ",
    "XN",
    "XT",
    "ZQ",
]


def run_benchmark(fn, name, columns=None):
    """Run a benchmark function, measuring time and peak RSS.

    Prints a JSON result line prefixed with BENCHMARK_RESULT: for the
    orchestrator to parse.

    Args:
        fn: Callable that returns row_count (int) or (row_count, columns_list) tuple.
        name: Human-readable benchmark name.
        columns: Pre-collected column names (pass this to avoid expensive
                 schema collection inside the timed section).
    """
    print(f"Starting benchmark: {name}")

    start = time.perf_counter()
    result_data = fn()
    elapsed = time.perf_counter() - start

    if isinstance(result_data, tuple):
        row_count, fn_columns = result_data
        if columns is None:
            columns = fn_columns
    else:
        row_count = result_data
        if columns is None:
            columns = []

    # Peak RSS from resource module (bytes on macOS, KiB on Linux).
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak_rss_mb = (
        usage.ru_maxrss / (1024 * 1024)
        if sys.platform == "darwin"
        else usage.ru_maxrss / 1024
    )

    result = {
        "name": name,
        "time_seconds": round(elapsed, 3),
        "peak_rss_mb": round(peak_rss_mb, 1),
        "row_count": row_count,
        "column_count": len(columns),
        "columns": columns,
    }

    print(f"Completed: {name}")
    print(f"  Time: {elapsed:.3f}s")
    print(f"  Peak RSS: {peak_rss_mb:.1f} MB")
    print(f"  Row count: {row_count}")
    print(f"  Column count: {len(columns)}")
    print(f"BENCHMARK_RESULT:{json.dumps(result)}")
