#!/usr/bin/env python3
"""Measure polars-bio BigWig/BigBed scaling from one through eight partitions."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import statistics
import subprocess
import sys
from pathlib import Path

import psutil

from benchmarks.bbi_common import BIGBED_PATH, BIGWIG_PATH, FORMATS, WORKLOADS


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_result(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("BBI_RESULT:"):
            return json.loads(line.removeprefix("BBI_RESULT:"))
    raise RuntimeError(f"child did not emit BBI_RESULT:\n{output}")


def run_one(python: str, env: dict[str, str], timeout: int) -> dict:
    completed = subprocess.run(
        [python, "-m", "benchmarks.bench_bbi_polars_bio"],
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
    estimated_bytes = runs[0].get("estimated_compressed_bytes", [])
    summary = {
        "runs": len(runs),
        "threads": runs[0]["threads"],
        "physical_partition_count": runs[0]["physical_partition_count"],
        "estimated_compressed_bytes": estimated_bytes,
        "iterations_per_process": runs[0]["iterations"],
        "time_seconds_median": statistics.median(times),
        "time_seconds_mean": statistics.mean(times),
        "time_seconds_stdev": statistics.stdev(times) if len(times) > 1 else 0.0,
        "peak_rss_mb_median": statistics.median(memories),
        "peak_rss_mb_mean": statistics.mean(memories),
        "peak_rss_mb_stdev": statistics.stdev(memories) if len(memories) > 1 else 0.0,
        "fingerprint": runs[0]["fingerprint"],
        "raw": runs,
    }
    if estimated_bytes:
        mean_bytes = statistics.mean(estimated_bytes)
        summary["estimated_compressed_byte_balance"] = {
            "total": sum(estimated_bytes),
            "minimum": min(estimated_bytes),
            "maximum": max(estimated_bytes),
            "coefficient_of_variation": (
                statistics.pstdev(estimated_bytes) / mean_bytes
                if mean_bytes
                else 0.0
            ),
            "maximum_to_mean": max(estimated_bytes) / mean_bytes if mean_bytes else 0.0,
        }
    diagnostics = runs[0].get("diagnostics", {})
    if diagnostics:
        if any(run.get("diagnostics", {}).keys() != diagnostics.keys() for run in runs):
            raise AssertionError("diagnostic keys changed across fresh-process runs")
        summary["diagnostics_median"] = {
            key: statistics.median(run["diagnostics"][key] for run in runs)
            for key in diagnostics
        }
    return summary


def fingerprints_match(left: dict, right: dict) -> bool:
    if left.keys() != right.keys():
        return False
    for key, value in left.items():
        if key == "value_sum":
            if not math.isclose(value, right[key], rel_tol=1e-9, abs_tol=1e-4):
                return False
        elif value != right[key]:
            return False
    return True


def verify_fingerprints(raw: dict[str, list[dict]]) -> dict:
    verified = {}
    for format_name in FORMATS:
        for workload in WORKLOADS:
            matching = [
                run
                for key, runs in raw.items()
                if key.startswith(f"{format_name}:{workload}:")
                for run in runs
            ]
            if not matching:
                continue
            reference = matching[0]["fingerprint"]
            for run in matching[1:]:
                if not fingerprints_match(reference, run["fingerprint"]):
                    raise AssertionError(
                        f"{format_name}/{workload} t={run['threads']} produced "
                        f"{run['fingerprint']!r}, expected {reference!r}"
                    )
            physical_partitions = {}
            for requested in sorted({run["threads"] for run in matching}):
                observed = sorted(
                    {
                        run["physical_partition_count"]
                        for run in matching
                        if run["threads"] == requested
                    }
                )
                if len(observed) != 1:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"inconsistent partition counts: {observed}"
                    )
                physical_partitions[f"t{requested}"] = observed[0]

                estimated_layouts = {
                    tuple(run.get("estimated_compressed_bytes", []))
                    for run in matching
                    if run["threads"] == requested
                }
                if len(estimated_layouts) != 1:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"inconsistent compressed-byte estimates: {estimated_layouts}"
                    )
                estimated_layout = next(iter(estimated_layouts))
                if estimated_layout and len(estimated_layout) != observed[0]:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"{observed[0]} partitions but {len(estimated_layout)} estimates"
                    )

            verified[f"{format_name}:{workload}"] = {
                "fingerprint": reference,
                "requested_threads_checked": sorted(
                    {run["threads"] for run in matching}
                ),
                "physical_partitions_by_requested": physical_partitions,
            }
    return verified


def installed_versions() -> dict[str, str]:
    versions = {}
    for distribution in ("polars-bio", "polars", "pyarrow"):
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "not-installed"
    return versions


def polars_bio_build_fingerprint() -> dict[str, object]:
    try:
        import polars_bio
    except ImportError as error:
        return {"error": f"import failed: {error}"}
    package = Path(polars_bio.__file__).parent
    extensions = sorted(package.glob("*.so"))
    return {
        "module_path": str(package),
        "editable_install": not str(package).endswith("site-packages/polars_bio"),
        "extensions": [
            {"name": extension.name, "size_bytes": extension.stat().st_size}
            for extension in extensions
        ],
        "declared_profile": os.environ.get("POLARS_BIO_BUILD_PROFILE"),
        "declared_rustflags": os.environ.get("POLARS_BIO_RUSTFLAGS"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--max-system-cpu-percent",
        type=float,
        help="abort before a sample when ambient aggregate CPU use exceeds this percent",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--partitions", nargs="+", type=int, default=list(range(1, 9)))
    parser.add_argument("--formats", nargs="+", choices=FORMATS, default=list(FORMATS))
    parser.add_argument(
        "--workloads", nargs="+", choices=WORKLOADS, default=list(WORKLOADS)
    )
    parser.add_argument("--bigwig", default=BIGWIG_PATH)
    parser.add_argument("--bigbed", default=BIGBED_PATH)
    parser.add_argument("--bigwig-iterations", type=int, default=1)
    parser.add_argument("--bigbed-iterations", type=int, default=10)
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--output", default="results/bbi_scaling.json")
    args = parser.parse_args()

    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive")
    if args.max_system_cpu_percent is not None and not (
        0 < args.max_system_cpu_percent <= 100
    ):
        parser.error("--max-system-cpu-percent must be in (0, 100]")
    if any(value < 1 for value in args.partitions):
        parser.error("--partitions values must be positive")
    if len(set(args.partitions)) != len(args.partitions):
        parser.error("--partitions values must be unique")
    if args.bigwig_iterations < 1 or args.bigbed_iterations < 1:
        parser.error("iteration counts must be positive")

    paths = {
        "bigwig": Path(args.bigwig).expanduser().resolve(),
        "bigbed": Path(args.bigbed).expanduser().resolve(),
    }
    for format_name in args.formats:
        if not paths[format_name].is_file():
            parser.error(f"{format_name} file does not exist: {paths[format_name]}")

    combinations = [
        (format_name, workload, partitions)
        for format_name in args.formats
        for workload in args.workloads
        for partitions in args.partitions
    ]
    raw = {
        f"{format_name}:{workload}:t{partitions}": []
        for format_name, workload, partitions in combinations
    }
    base_env = os.environ.copy()
    base_env.update(
        {
            "BIGWIG_PATH": str(paths["bigwig"]),
            "BIGBED_PATH": str(paths["bigbed"]),
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
        for order_index, (format_name, workload, partitions) in enumerate(
            order, start=1
        ):
            print(
                f"Round {round_index + 1}/{args.runs}, "
                f"{order_index}/{len(order)}: {format_name} {workload} t={partitions}",
                flush=True,
            )
            env = base_env.copy()
            env.update(
                {
                    "BBI_FORMAT": format_name,
                    "BBI_WORKLOAD": workload,
                    "BBI_ITERATIONS": str(
                        args.bigwig_iterations
                        if format_name == "bigwig"
                        else args.bigbed_iterations
                    ),
                    "THREAD_NUM": str(partitions),
                    "POLARS_MAX_THREADS": str(partitions),
                    "RAYON_NUM_THREADS": str(partitions),
                }
            )
            ambient_cpu_percent = psutil.cpu_percent(interval=0.2)
            if (
                args.max_system_cpu_percent is not None
                and ambient_cpu_percent > args.max_system_cpu_percent
            ):
                raise RuntimeError(
                    f"ambient CPU use is {ambient_cpu_percent:.1f}%, above "
                    f"--max-system-cpu-percent={args.max_system_cpu_percent:.1f}%"
                )
            result = run_one(args.python, env, args.timeout)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            result["ambient_cpu_percent_before"] = ambient_cpu_percent
            raw[f"{format_name}:{workload}:t{partitions}"].append(result)

    verification = verify_fingerprints(raw)
    results = {
        format_name: {
            workload: {
                f"t{partitions}": summarize(
                    raw[f"{format_name}:{workload}:t{partitions}"]
                )
                for partitions in args.partitions
            }
            for workload in args.workloads
        }
        for format_name in args.formats
    }
    scaling = {}
    for format_name, workloads in results.items():
        scaling[format_name] = {}
        for workload, partitions in workloads.items():
            one = partitions.get("t1")
            if one is None:
                continue
            baseline = one["time_seconds_median"]
            scaling[format_name][workload] = {}
            for key, summary in partitions.items():
                count = int(summary["fingerprint"]["rows"])
                speedup = baseline / summary["time_seconds_median"]
                thread_count = int(key.removeprefix("t"))
                scaling[format_name][workload][key] = {
                    "rows_per_second": count / summary["time_seconds_median"],
                    "speedup_vs_t1": speedup,
                    "parallel_efficiency": speedup / thread_count,
                }

    payload = {
        "metadata": {
            "label": args.label,
            "partitions": args.partitions,
            "formats": args.formats,
            "workloads": args.workloads,
            "files": {
                format_name: {
                    "path": str(paths[format_name]),
                    "size_bytes": paths[format_name].stat().st_size,
                    "sha256": file_sha256(paths[format_name]),
                }
                for format_name in args.formats
            },
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "memory_total_bytes": psutil.virtual_memory().total,
            "versions": installed_versions(),
            "polars_bio_build": polars_bio_build_fingerprint(),
            "polars_bio_ref": os.environ.get("POLARS_BIO_REF"),
            "datafusion_bio_formats_ref": os.environ.get("DATAFUSION_BIO_FORMATS_REF"),
            "bigtools_ref": os.environ.get("BIGTOOLS_REF"),
            "timing_scope": "lazy scan construction, BBI index/header access, decoding, "
            "and the workload-specific Arrow drain, Polars aggregation, or full DataFrame "
            "materialization; imports and thread-pool configuration excluded",
        },
        "verification": verification,
        "results": results,
        "scaling": scaling,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"\nAll requested configurations produced matching content. Wrote {output}")


if __name__ == "__main__":
    main()
