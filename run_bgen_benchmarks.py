#!/usr/bin/env python3
"""Benchmark equivalent BGEN genotype matrices across Python readers.

Every reader materializes the same canonical ``float32`` array from the same
BGEN file. The runner rejects any cross-reader disagreement in shape, variant
positions, sample order, or values, so a completed run is evidence that the
readers agree, not just that they finished.
"""

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

READERS = ("polars-bio", "snputils", "bgen", "pysnptools")
# The `bgen` package is the independent oracle every other reader is checked
# against; snputils uses it the same way in its own published benchmark.
REFERENCE_READER = "bgen"
DISTRIBUTIONS = {
    "polars-bio": "polars-bio",
    "snputils": "snputils",
    "bgen": "bgen",
    "pysnptools": "pysnptools",
}
WORKLOADS = ("dosage", "probabilities")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bgen_is_phased(path: Path) -> bool:
    """Read the phased flag of the first variant's probability block."""
    import struct
    import zlib

    with path.open("rb") as handle:
        offset, header_length, _variants, n_samples = struct.unpack(
            "<IIII", handle.read(16)
        )
        handle.seek(4 + header_length - 4)
        compression = struct.unpack("<I", handle.read(4))[0] & 0b11
        handle.seek(offset + 4)

        def text(size_bytes: int) -> None:
            length = struct.unpack(
                "<H" if size_bytes == 2 else "<I", handle.read(size_bytes)
            )[0]
            handle.read(length)

        text(2)  # variant ID
        text(2)  # rsid
        text(2)  # chromosome
        handle.read(4)  # position
        for _ in range(struct.unpack("<H", handle.read(2))[0]):
            text(4)  # allele
        block_length = struct.unpack("<I", handle.read(4))[0]
        if compression == 0:
            block = handle.read(block_length)
        else:
            handle.read(4)  # declared decompressed length
            payload = handle.read(block_length - 4)
            if compression == 1:
                block = zlib.decompress(payload)
            else:
                import zstandard

                block = zstandard.ZstdDecompressor().decompress(payload)
    return bool(block[8 + n_samples])


def parse_result(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("BGEN_RESULT:"):
            return json.loads(line.removeprefix("BGEN_RESULT:"))
    raise RuntimeError(f"child did not emit BGEN_RESULT:\n{output}")


def run_one(python: str, env: dict[str, str], timeout: int) -> dict:
    completed = subprocess.run(
        [python, "-m", "benchmarks.bgen_matrix"],
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
        "threads": runs[0]["threads"],
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
        "value_sha256": runs[0]["value_sha256"],
        "emission_order_descents": [run["emission_order_descents"] for run in runs],
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


def run_verification(
    python: str, env: dict[str, str], left: str, right: str, workload: str, timeout: int
) -> dict:
    """Compare two readers element-wise in one process."""
    child_env = env.copy()
    child_env.update(
        {
            "BGEN_READER": right,
            "BGEN_MODE": workload,
            "BGEN_VERIFY_LEFT": left,
            "BGEN_VERIFY_RIGHT": right,
            "THREAD_NUM": "1",
            "POLARS_MAX_THREADS": "1",
        }
    )
    completed = subprocess.run(
        [python, "-m", "benchmarks.bgen_verify"],
        check=True,
        capture_output=True,
        text=True,
        env=child_env,
        timeout=timeout,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("BGEN_VERIFY:"):
            return json.loads(line.removeprefix("BGEN_VERIFY:"))
    raise RuntimeError(f"child did not emit BGEN_VERIFY:\n{completed.stdout}")


def check_equivalence(runs: list[dict]) -> dict:
    """Fail unless every reader produced the same content for a workload."""
    by_workload: dict[str, list[dict]] = {}
    for run in runs:
        by_workload.setdefault(run["workload"], []).append(run)

    checked = {}
    for workload, workload_runs in by_workload.items():
        reference = next(
            (run for run in workload_runs if run["reader"] == REFERENCE_READER),
            workload_runs[0],
        )
        # Shape and identity must match exactly for the comparison to mean
        # anything, so a disagreement there is a hard failure.
        for run in workload_runs:
            for field in (
                "rows",
                "samples",
                "width",
                "values",
                "position_sha256",
                "sample_sha256",
            ):
                if run[field] != reference[field]:
                    raise AssertionError(
                        f"{workload}: {run['reader']} (t={run['threads']}) disagrees "
                        f"with {reference['reader']} in {field}: "
                        f"{run[field]!r} != {reference[field]!r}"
                    )

        # polars-bio is the reader under test, so it must reproduce the oracle
        # bit for bit at every partition count.
        for run in workload_runs:
            if run["reader"] == "polars-bio" and run["value_sha256"] != reference["value_sha256"]:
                raise AssertionError(
                    f"{workload}: polars-bio (t={run['threads']}) does not reproduce "
                    f"{reference['reader']} exactly: {run['value_sha256']} != "
                    f"{reference['value_sha256']}"
                )

        # Other readers may round differently; record which ones matched rather
        # than hiding the difference behind a tolerance.
        bit_identical = sorted(
            {
                run["reader"]
                for run in workload_runs
                if run["value_sha256"] == reference["value_sha256"]
            }
        )
        differing = sorted(
            {
                run["reader"]
                for run in workload_runs
                if run["value_sha256"] != reference["value_sha256"]
            }
        )
        checked[workload] = {
            "reference_reader": reference["reader"],
            "rows": reference["rows"],
            "samples": reference["samples"],
            "width": reference["width"],
            "values": reference["values"],
            "position_sha256": reference["position_sha256"],
            "sample_sha256": reference["sample_sha256"],
            "value_sha256": reference["value_sha256"],
            "readers_checked": sorted({run["reader"] for run in workload_runs}),
            "thread_counts_checked": sorted({run["threads"] for run in workload_runs}),
            "bit_identical_to_reference": bit_identical,
            "differing_from_reference": differing,
        }
    return checked


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--readers", nargs="+", choices=READERS, default=list(READERS))
    parser.add_argument(
        "--workloads", nargs="+", choices=WORKLOADS, default=["dosage"]
    )
    parser.add_argument(
        "--bgen",
        default="/Users/mwiewior/research/data/BGEN/chr22.first-25000.bgen",
    )
    parser.add_argument("--expected-rows", type=int, default=25000)
    parser.add_argument("--expected-samples", type=int, default=2548)
    parser.add_argument(
        "--polars-bio-partitions",
        nargs="+",
        type=int,
        default=[1],
        help="target_partitions values to measure for polars-bio; other readers "
        "are single-threaded and always run once per round",
    )
    parser.add_argument("--output", default="results/bgen_reader_benchmark.json")
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive")
    if any(value < 1 for value in args.polars_bio_partitions):
        parser.error("--polars-bio-partitions values must be positive")

    path = Path(args.bgen).expanduser().resolve()
    if not path.is_file():
        parser.error(f"BGEN file does not exist: {path}")

    phased = bgen_is_phased(path)
    # pysnptools' BGEN reader asserts unphased input, so a phased file has no
    # comparable pysnptools result rather than a slow one.
    unsupported = []
    readers = []
    for reader in args.readers:
        if reader == "pysnptools" and phased:
            unsupported.append(
                {
                    "reader": reader,
                    "reason": "pysnptools.distreader.Bgen requires unphased BGEN input",
                }
            )
            continue
        readers.append(reader)
    if not readers:
        parser.error("every requested reader is unsupported for this file")

    combinations = []
    for workload in args.workloads:
        for reader in readers:
            threads = args.polars_bio_partitions if reader == "polars-bio" else [1]
            combinations.extend(
                (workload, reader, count) for count in threads
            )
    raw = {f"{workload}:{reader}:t{count}": [] for workload, reader, count in combinations}

    base_env = os.environ.copy()
    base_env.update(
        {
            "BGEN_PATH": str(path),
            "BGEN_EXPECTED_ROWS": str(args.expected_rows),
            "BGEN_EXPECTED_SAMPLES": str(args.expected_samples),
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
        for order_index, (workload, reader, threads) in enumerate(order, start=1):
            print(
                f"\nRound {round_index + 1}/{args.runs}, "
                f"{order_index}/{len(order)}: {reader} {workload} t={threads}"
            )
            env = base_env.copy()
            env["BGEN_READER"] = reader
            env["BGEN_MODE"] = workload
            env["THREAD_NUM"] = str(threads)
            env["POLARS_MAX_THREADS"] = str(threads)
            env["RAYON_NUM_THREADS"] = str(threads)
            result = run_one(args.python, env, args.timeout)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            raw[f"{workload}:{reader}:t{threads}"].append(result)

    all_runs = [run for runs in raw.values() for run in runs]
    equivalence = check_equivalence(all_runs)

    verifications = []
    for workload in args.workloads:
        for reader in readers:
            if reader == REFERENCE_READER:
                continue
            print(f"\nVerifying {reader} against {REFERENCE_READER} ({workload})")
            verifications.append(
                run_verification(
                    args.python, base_env, reader, REFERENCE_READER, workload, args.timeout
                )
            )
    for check in verifications:
        if check["left"] == "polars-bio" and check["bitwise_differences"]:
            raise AssertionError(
                f"polars-bio differs from {check['right']} in "
                f"{check['bitwise_differences']} of {check['cells']} cells"
            )

    results: dict[str, dict] = {workload: {} for workload in args.workloads}
    for workload, reader, threads in combinations:
        key = reader if reader != "polars-bio" else f"polars-bio-t{threads}"
        results[workload][key] = summarize(raw[f"{workload}:{reader}:t{threads}"])

    comparisons = {}
    for workload, readers in results.items():
        snputils = readers.get("snputils")
        if snputils is None:
            continue
        comparisons[workload] = {
            key: {
                "speedup_over_snputils": round(
                    snputils["time_seconds_median"] / summary["time_seconds_median"], 3
                ),
                "peak_rss_ratio_vs_snputils": round(
                    summary["peak_rss_mb_median"] / snputils["peak_rss_mb_median"], 3
                ),
            }
            for key, summary in readers.items()
            if key != "snputils"
        }

    payload = {
        "metadata": {
            "workloads": args.workloads,
            "bgen_path": str(path),
            "bgen_size_bytes": path.stat().st_size,
            "bgen_sha256": file_sha256(path),
            "rows": args.expected_rows,
            "samples": args.expected_samples,
            "phased": phased,
            "polars_bio_partitions": args.polars_bio_partitions,
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
            "timing_scope": "source open, header/index discovery, block decompression, "
            "probability decoding, and final C-contiguous float32 materialization; "
            "imports and thread-pool configuration excluded",
            "equivalence_note": "value and position hashes are taken after sorting rows "
            "by variant position, so they compare content independently of the order in "
            "which DataFusion coalesces partitions",
        },
        "unsupported": unsupported,
        "equivalence": equivalence,
        "verification": verifications,
        "results": results,
        "comparisons": comparisons,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"\nWrote {output_path}")
    for workload, readers in results.items():
        print(f"\n{workload}:")
        for reader, summary in sorted(
            readers.items(), key=lambda item: item[1]["time_seconds_median"]
        ):
            print(
                f"  {reader:<22} {summary['time_seconds_median']:>8.3f} s  "
                f"{summary['peak_rss_mb_median']:>9.1f} MB"
            )


if __name__ == "__main__":
    main()
