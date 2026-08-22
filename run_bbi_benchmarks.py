#!/usr/bin/env python3
"""Measure polars-bio BigWig/BigBed scaling from one through eight partitions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

import psutil

from benchmarks.bbi_common import (
    BIGBED_PATH,
    BIGWIG_PATH,
    FORMATS,
    WORKLOADS,
    fingerprints_match,
)

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_VERSION = 2


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
        cwd=SCRIPT_DIR,
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
    estimated_bytes = runs[0].get("estimated_data_bytes", [])
    summary = {
        "runs": len(runs),
        "threads": runs[0]["threads"],
        "physical_partition_count": runs[0]["physical_partition_count"],
        "estimated_data_bytes": estimated_bytes,
        "iterations_per_process": runs[0]["iterations"],
        "time_seconds_median": statistics.median(times),
        "time_seconds_mean": statistics.mean(times),
        "time_seconds_stdev": statistics.stdev(times) if len(times) > 1 else None,
        "peak_rss_mb_median": statistics.median(memories),
        "peak_rss_mb_mean": statistics.mean(memories),
        "peak_rss_mb_stdev": statistics.stdev(memories) if len(memories) > 1 else None,
        "fingerprint": runs[0]["fingerprint"],
        "content_fingerprint": runs[0]["content_fingerprint"],
        "raw": runs,
    }
    if estimated_bytes:
        mean_bytes = statistics.mean(estimated_bytes)
        summary["estimated_data_byte_balance"] = {
            "total": sum(estimated_bytes),
            "minimum": min(estimated_bytes),
            "maximum": max(estimated_bytes),
            "coefficient_of_variation": (
                statistics.pstdev(estimated_bytes) / mean_bytes if mean_bytes else 0.0
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


def validate_partition_sweep(partitions: list[int]) -> None:
    if any(value < 1 for value in partitions):
        raise ValueError("--partitions values must be positive")
    if len(set(partitions)) != len(partitions):
        raise ValueError("--partitions values must be unique")
    if 1 not in partitions:
        raise ValueError("--partitions must include 1 as the scaling baseline")


def verify_declared_build_refs(
    environment: dict, declared_refs: dict[str, str | None]
) -> None:
    declared = {name: value for name, value in declared_refs.items() if value}
    if not declared:
        return
    source = environment.get("polars_bio_build", {}).get("source")
    if not source:
        raise AssertionError(
            "declared source refs require an editable polars-bio build"
        )

    polars_ref = declared.get("polars_bio_ref")
    if polars_ref and not source["git_head"].startswith(polars_ref):
        raise AssertionError(
            f"declared polars-bio ref {polars_ref!r} does not match "
            f"built source head {source['git_head']!r}"
        )

    cargo_lock = (Path(source["root"]) / "Cargo.lock").read_text(encoding="utf-8")
    for name in ("datafusion_bio_formats_ref", "bigtools_ref"):
        value = declared.get(name)
        if value and value not in cargo_lock:
            raise AssertionError(f"declared {name} {value!r} is absent from Cargo.lock")


def verify_fingerprints(
    raw: dict[str, list[dict]], physical_partition_expectation: str
) -> dict:
    verified = {}
    for format_name in FORMATS:
        format_runs = [
            run
            for key, runs in raw.items()
            if key.startswith(f"{format_name}:")
            for run in runs
        ]
        if not format_runs:
            continue
        content_reference = format_runs[0]["content_fingerprint"]
        for run in format_runs[1:]:
            if not fingerprints_match(content_reference, run["content_fingerprint"]):
                raise AssertionError(
                    f"{format_name} t={run['threads']} produced content digest "
                    f"{run['content_fingerprint']!r}, expected {content_reference!r}"
                )

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
            for run in matching:
                expected_limits = {
                    "POLARS_MAX_THREADS": run["threads"],
                    "RAYON_NUM_THREADS": run["threads"],
                    "TOKIO_WORKER_THREADS": run["threads"],
                }
                if run.get("thread_limits") != expected_limits:
                    raise AssertionError(
                        f"{format_name}/{workload} t={run['threads']} used thread "
                        f"limits {run.get('thread_limits')!r}, expected {expected_limits!r}"
                    )
                common_fields = (
                    run["fingerprint"].keys() & run["content_fingerprint"].keys()
                )
                if not common_fields:
                    raise AssertionError(
                        f"{format_name}/{workload} timed and content fingerprints "
                        "share no fields"
                    )
                timed_common = {key: run["fingerprint"][key] for key in common_fields}
                content_common = {
                    key: run["content_fingerprint"][key] for key in common_fields
                }
                if not fingerprints_match(timed_common, content_common):
                    raise AssertionError(
                        f"{format_name}/{workload} t={run['threads']} timed "
                        f"fingerprint {timed_common!r} does not match the independent "
                        f"content scan {content_common!r}"
                    )
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
                expected_physical = {
                    "requested": requested,
                    "serial": 1,
                    "consistent": observed[0],
                }[physical_partition_expectation]
                if observed[0] != expected_physical:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"{observed[0]} physical partitions; expectation "
                        f"{physical_partition_expectation!r} requires {expected_physical}"
                    )
                physical_partitions[f"t{requested}"] = observed[0]

                estimated_layouts = {
                    tuple(run.get("estimated_data_bytes", []))
                    for run in matching
                    if run["threads"] == requested
                }
                if len(estimated_layouts) != 1:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"inconsistent data-byte estimates: {estimated_layouts}"
                    )
                estimated_layout = next(iter(estimated_layouts))
                if estimated_layout and len(estimated_layout) != observed[0]:
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} advertised "
                        f"{observed[0]} partitions but {len(estimated_layout)} estimates"
                    )

            verified[f"{format_name}:{workload}"] = {
                "fingerprint": reference,
                "content_fingerprint": content_reference,
                "requested_threads_checked": sorted(
                    {run["threads"] for run in matching}
                ),
                "physical_partitions_by_requested": physical_partitions,
            }
    return verified


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
    parser.add_argument(
        "--physical-partitions",
        choices=("requested", "serial", "consistent"),
        default="requested",
        help="required relationship between requested and observed source partitions",
    )
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
    try:
        validate_partition_sweep(args.partitions)
    except ValueError as error:
        parser.error(str(error))
    if args.bigwig_iterations < 1 or args.bigbed_iterations < 1:
        parser.error("iteration counts must be positive")

    if os.sep in args.python:
        # Keep a virtualenv's interpreter symlink intact: resolving it to the
        # base installation would drop the venv's site-packages.
        python = str(Path(args.python).expanduser().absolute())
    else:
        python = shutil.which(args.python) or ""
    if not python or not Path(python).is_file():
        parser.error(f"Python interpreter does not exist: {args.python}")

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
                    "TOKIO_WORKER_THREADS": str(partitions),
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
            result = run_one(python, env, args.timeout)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            result["ambient_cpu_percent_before"] = ambient_cpu_percent
            raw[f"{format_name}:{workload}:t{partitions}"].append(result)

    verification = verify_fingerprints(raw, args.physical_partitions)
    all_runs = [run for runs in raw.values() for run in runs]
    environments = {json.dumps(run["environment"], sort_keys=True) for run in all_runs}
    if len(environments) != 1:
        raise AssertionError("child interpreter environment changed during the sweep")
    child_environment = json.loads(next(iter(environments)))
    declared_refs = {
        "polars_bio_ref": os.environ.get("POLARS_BIO_REF"),
        "datafusion_bio_formats_ref": os.environ.get("DATAFUSION_BIO_FORMATS_REF"),
        "bigtools_ref": os.environ.get("BIGTOOLS_REF"),
    }
    verify_declared_build_refs(child_environment, declared_refs)
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
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "label": args.label,
            "partitions": args.partitions,
            "formats": args.formats,
            "workloads": args.workloads,
            "physical_partition_expectation": args.physical_partitions,
            "max_system_cpu_percent": args.max_system_cpu_percent,
            "files": {
                format_name: {
                    "path": str(paths[format_name]),
                    "size_bytes": paths[format_name].stat().st_size,
                    "sha256": file_sha256(paths[format_name]),
                }
                for format_name in args.formats
            },
            "python": child_environment["python"],
            "python_executable": child_environment["python_executable"],
            "platform": child_environment["platform"],
            "machine": child_environment["machine"],
            "logical_cpu_count": psutil.cpu_count(logical=True),
            "physical_cpu_count": psutil.cpu_count(logical=False),
            "memory_total_bytes": psutil.virtual_memory().total,
            "versions": child_environment["versions"],
            "polars_bio_build": child_environment["polars_bio_build"],
            **declared_refs,
            "generator": {
                "path": str(Path(__file__).resolve()),
                "sha256": file_sha256(Path(__file__).resolve()),
            },
            "timing_scope": "lazy scan construction, BBI index/header access, decoding, "
            "and the workload-specific Arrow drain, Polars aggregation, or full DataFrame "
            "materialization; imports, thread-pool configuration, physical-plan inspection, "
            "and the independent content digest excluded",
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
