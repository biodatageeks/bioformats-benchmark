#!/usr/bin/env python3
"""Benchmark equivalent VCF/BCF genotype matrices across Python readers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path

import psutil

READERS = ("pysam", "pyvcf3", "cyvcf2", "oxbow", "polars-bio", "snputils")
SUPPORTED = {
    "VCF": set(READERS),
    "BCF": set(READERS) - {"pyvcf3"},
}
DISTRIBUTIONS = {
    "pysam": "pysam",
    "pyvcf3": "PyVCF3",
    "cyvcf2": "cyvcf2",
    "oxbow": "oxbow",
    "polars-bio": "polars-bio",
    "snputils": "snputils",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_result(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("GENOTYPE_RESULT:"):
            return json.loads(line.removeprefix("GENOTYPE_RESULT:"))
    raise RuntimeError(f"child did not emit GENOTYPE_RESULT:\n{output}")


def run_one(python: str, env: dict[str, str], timeout: int) -> dict:
    completed = subprocess.run(
        [python, "-m", "benchmarks.genotype_matrix"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return parse_result(completed.stdout)


def summarize(runs: list[dict]) -> dict:
    times = [run["time_seconds"] for run in runs]
    memories = [run["peak_rss_mb"] for run in runs]
    return {
        "runs": len(runs),
        "mode": runs[0]["mode"],
        "time_seconds_median": round(statistics.median(times), 3),
        "time_seconds_mean": round(statistics.mean(times), 3),
        "time_seconds_stdev": round(statistics.stdev(times), 3)
        if len(times) > 1
        else 0.0,
        "peak_rss_mb_median": round(statistics.median(memories), 1),
        "peak_rss_mb_mean": round(statistics.mean(memories), 1),
        "peak_rss_mb_stdev": round(statistics.stdev(memories), 1)
        if len(memories) > 1
        else 0.0,
        "raw": runs,
    }


def installed_versions() -> dict[str, str]:
    versions = {}
    for reader, distribution in DISTRIBUTIONS.items():
        try:
            versions[reader] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[reader] = "not-installed"
    for distribution in ("numpy", "polars", "pyarrow"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument(
        "--formats", nargs="+", choices=("vcf", "bcf"), default=["vcf", "bcf"]
    )
    parser.add_argument("--readers", nargs="+", choices=READERS, default=list(READERS))
    parser.add_argument(
        "--vcf",
        default="/Users/mwiewior/research/data/BCF/ALL.chr22.phased.first-25000.vcf.gz",
    )
    parser.add_argument(
        "--bcf",
        default="/Users/mwiewior/research/data/BCF/ALL.chr22.phased.first-25000.bcf",
    )
    parser.add_argument("--expected-rows", type=int, default=25000)
    parser.add_argument("--expected-samples", type=int, default=2548)
    parser.add_argument("--oxbow-batch-size", type=int, default=8192)
    parser.add_argument("--output", default="results/genotype_reader_benchmark.json")
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive")

    paths = {
        "VCF": Path(args.vcf).expanduser().resolve(),
        "BCF": Path(args.bcf).expanduser().resolve(),
    }
    formats = [value.upper() for value in args.formats]
    for file_format in formats:
        if not paths[file_format].is_file():
            parser.error(f"{file_format} file does not exist: {paths[file_format]}")

    combinations = [
        (file_format, reader)
        for file_format in formats
        for reader in args.readers
        if reader in SUPPORTED[file_format]
    ]
    unsupported = [
        {
            "format": file_format,
            "reader": reader,
            "reason": "format not supported by reader",
        }
        for file_format in formats
        for reader in args.readers
        if reader not in SUPPORTED[file_format]
    ]
    raw = {f"{file_format}:{reader}": [] for file_format, reader in combinations}

    base_env = os.environ.copy()
    base_env.update(
        {
            "GENOTYPE_VCF_PATH": str(paths["VCF"]),
            "GENOTYPE_BCF_PATH": str(paths["BCF"]),
            "GENOTYPE_EXPECTED_ROWS": str(args.expected_rows),
            "GENOTYPE_EXPECTED_SAMPLES": str(args.expected_samples),
            "OXBOW_BATCH_SIZE": str(args.oxbow_batch_size),
            "THREAD_NUM": "1",
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "TQDM_DISABLE": "1",
        }
    )

    for round_index in range(args.runs):
        shift = round_index % len(combinations)
        order = combinations[shift:] + combinations[:shift]
        if round_index % 2:
            order.reverse()
        for order_index, (file_format, reader) in enumerate(order, start=1):
            print(
                f"\nRound {round_index + 1}/{args.runs}, "
                f"{order_index}/{len(order)}: {reader} {file_format}"
            )
            env = base_env.copy()
            env["GENOTYPE_FORMAT"] = file_format
            env["GENOTYPE_READER"] = reader
            result = run_one(args.python, env, args.timeout)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            raw[f"{file_format}:{reader}"].append(result)

    all_runs = [run for runs in raw.values() for run in runs]
    reference = all_runs[0]
    checksum_fields = (
        "rows",
        "samples",
        "dosage_values",
        "position_sha256",
        "sample_sha256",
        "dosage_sha256",
    )
    for result in all_runs[1:]:
        for field in checksum_fields:
            if result[field] != reference[field]:
                raise AssertionError(
                    f"output mismatch for {result['reader']} {result['format']} in {field}: "
                    f"{result[field]!r} != {reference[field]!r}"
                )

    results = {file_format: {} for file_format in formats}
    for file_format, reader in combinations:
        results[file_format][reader] = summarize(raw[f"{file_format}:{reader}"])

    comparisons = {}
    for file_format, readers in results.items():
        polars = readers.get("polars-bio")
        if polars is None:
            continue
        comparisons[file_format] = {
            reader: {
                "polars_bio_time_speedup": round(
                    summary["time_seconds_median"] / polars["time_seconds_median"], 3
                ),
                "polars_bio_peak_rss_advantage": round(
                    summary["peak_rss_mb_median"] / polars["peak_rss_mb_median"], 3
                ),
            }
            for reader, summary in readers.items()
            if reader != "polars-bio"
        }

    payload = {
        "metadata": {
            "workload": "full GT-to-biallelic-ALT-dosage materialization",
            "threads": 1,
            "rows": args.expected_rows,
            "samples": args.expected_samples,
            "dosage_values": args.expected_rows * args.expected_samples,
            "vcf_path": str(paths["VCF"]),
            "vcf_size_bytes": paths["VCF"].stat().st_size,
            "vcf_sha256": file_sha256(paths["VCF"]),
            "bcf_path": str(paths["BCF"]),
            "bcf_size_bytes": paths["BCF"].stat().st_size,
            "bcf_sha256": file_sha256(paths["BCF"]),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "memory_total_bytes": psutil.virtual_memory().total,
            "versions": installed_versions(),
            "polars_bio_ref": os.environ.get("POLARS_BIO_REF"),
            "datafusion_bio_formats_ref": os.environ.get("DATAFUSION_BIO_FORMATS_REF"),
            "polars_bio_build_profile": os.environ.get("POLARS_BIO_BUILD_PROFILE"),
            "polars_bio_rustflags": os.environ.get("POLARS_BIO_RUSTFLAGS"),
            "timing_scope": "parse, GT decode, ALT-dosage conversion, and row-major int8 materialization; imports excluded",
            "memory_metric": "fresh-process peak RSS including retained comparable output",
            "cache_state": "warm filesystem cache; deterministic rotating reader order",
        },
        "equivalence": {
            "equivalent": True,
            **{field: reference[field] for field in checksum_fields},
        },
        "unsupported": unsupported,
        "results": results,
        "comparison": comparisons,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nGENOTYPE_BENCHMARK_SUMMARY:{json.dumps(payload, sort_keys=True)}")
    print(f"Wrote {output}")


if __name__ == "__main__":
    main()
