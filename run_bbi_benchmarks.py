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
import time
from pathlib import Path

import psutil

from benchmarks.bbi_common import (
    BIGBED_PATH,
    BIGWIG_PATH,
    FORMATS,
    PARTITION_PROBE_KIND,
    WORKLOADS,
    file_sha256,
    fingerprints_match,
)

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_VERSION = 2
CPU_QUIET_SAMPLES = 3
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
HARNESS_PATHS = {
    "runner": Path(__file__).resolve(),
    "child": SCRIPT_DIR / "benchmarks" / "bench_bbi_polars_bio.py",
    "common": SCRIPT_DIR / "benchmarks" / "bbi_common.py",
}
CHILD_ENVIRONMENT_SCRIPT = (
    "import json; from benchmarks.bench_bbi_polars_bio import "
    "configure_runtime, environment_info; configure_runtime(); "
    "print('BBI_ENVIRONMENT:' + json.dumps(environment_info(), sort_keys=True))"
)


def parse_prefixed_json(output: str, prefix: str) -> dict:
    for line in output.splitlines():
        if line.startswith(prefix):
            return json.loads(line.removeprefix(prefix))
    raise RuntimeError(f"child did not emit {prefix}\n{output}")


def parse_result(output: str) -> dict:
    return parse_prefixed_json(output, "BBI_RESULT:")


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


def preflight_environment(python: str, env: dict[str, str], timeout: int) -> dict:
    """Inspect the child build before starting the timed sweep."""
    completed = subprocess.run(
        [
            python,
            "-c",
            CHILD_ENVIRONMENT_SCRIPT,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=SCRIPT_DIR,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            completed.args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return parse_prefixed_json(completed.stdout, "BBI_ENVIRONMENT:")


def round_order(
    combinations: list[tuple[str, str, int]], round_index: int, run_count: int
) -> list[tuple[str, str, int]]:
    """Spread each round's start evenly over the full combination list."""
    shift = round_index * len(combinations) // run_count
    order = combinations[shift:] + combinations[:shift]
    if round_index % 2:
        order.reverse()
    return order


def harness_provenance() -> dict[str, dict[str, str]]:
    return {
        name: {
            "path": str(path.relative_to(SCRIPT_DIR)),
            "sha256": file_sha256(path),
        }
        for name, path in HARNESS_PATHS.items()
    }


def verify_harness_unchanged(expected: dict[str, dict[str, str]]) -> None:
    """Reject a sweep that loaded benchmark code from more than one snapshot."""
    observed = harness_provenance()
    if observed != expected:
        changed = sorted(
            name for name in HARNESS_PATHS if observed.get(name) != expected.get(name)
        )
        raise AssertionError(
            "benchmark harness changed during the sweep: " + ", ".join(changed)
        )


def fixture_provenance(
    paths: dict[str, Path], formats: list[str]
) -> dict[str, dict[str, int | str]]:
    """Snapshot selected input files before any benchmark children launch."""
    snapshots = {}
    for format_name in formats:
        path = paths[format_name]
        before = path.stat()
        digest = file_sha256(path)
        after = path.stat()
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_after != identity_before:
            raise AssertionError(
                f"{format_name} fixture changed while its provenance was captured"
            )
        snapshots[format_name] = {
            "path": str(path),
            "size_bytes": after.st_size,
            "sha256": digest,
        }
    return snapshots


def verify_fixtures_unchanged(
    expected: dict[str, dict[str, int | str]], paths: dict[str, Path]
) -> None:
    """Reject timings collected while an input fixture was replaced."""
    observed = fixture_provenance(paths, list(expected))
    if observed != expected:
        changed = sorted(
            format_name
            for format_name in expected
            if observed.get(format_name) != expected.get(format_name)
        )
        raise AssertionError(
            "benchmark fixtures changed during the sweep: " + ", ".join(changed)
        )


def wait_for_quiet_cpu(maximum: float | None, settle_timeout: float) -> float:
    """Return ambient CPU after a stable quiet window, or fail on timeout."""
    if maximum is None:
        return psutil.cpu_percent(interval=0.2)

    deadline = time.monotonic() + settle_timeout
    quiet_observations: list[float] = []
    while True:
        observed = psutil.cpu_percent(interval=0.2)
        if observed <= maximum:
            quiet_observations.append(observed)
            if len(quiet_observations) == CPU_QUIET_SAMPLES:
                return max(quiet_observations)
        else:
            quiet_observations.clear()
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"ambient CPU did not remain at or below {maximum:.1f}% for "
                f"{CPU_QUIET_SAMPLES} consecutive observations within "
                f"{settle_timeout:.1f} seconds"
            )


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


def validate_unique_values(values: list[str], option: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"{option} values must be unique")


def verify_declared_build_refs(
    environment: dict,
    declared_refs: dict[str, str | None],
    *,
    patch_declared: bool = False,
) -> None:
    declared = {name: value for name, value in declared_refs.items() if value}
    if not declared and not patch_declared:
        return
    source = environment.get("polars_bio_build", {}).get("source")
    if not source:
        raise AssertionError(
            "declared source refs require an editable polars-bio build"
        )
    if source.get("untracked_paths"):
        raise AssertionError(
            f"polars-bio source contains untracked files: {source['untracked_paths']!r}"
        )

    tracked_diff_sha256 = source.get("tracked_diff_sha256")
    declared_patch = source.get("declared_patch")
    if patch_declared and not declared_patch:
        raise AssertionError("declared polars-bio patch is absent from source metadata")
    expected_diff_sha256 = declared_patch["sha256"] if patch_declared else EMPTY_SHA256
    if tracked_diff_sha256 != expected_diff_sha256:
        raise AssertionError(
            "polars-bio tracked diff is neither clean nor identical to its declared "
            f"patch: {tracked_diff_sha256!r} != {expected_diff_sha256!r}"
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
                if run.get("physical_partition_probe") != PARTITION_PROBE_KIND:
                    raise AssertionError(
                        f"{format_name}/{workload} t={run['threads']} has unknown "
                        f"partition probe {run.get('physical_partition_probe')!r}"
                    )
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
                if (
                    physical_partition_expectation == "requested"
                    and not estimated_layout
                ):
                    raise AssertionError(
                        f"{format_name}/{workload} t={requested} omitted required "
                        "data-byte estimates"
                    )
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
                "physical_partition_probe": PARTITION_PROBE_KIND,
            }
    return verified


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument(
        "--max-system-cpu-percent",
        type=float,
        help="wait for ambient aggregate CPU use at or below this percent",
    )
    parser.add_argument(
        "--cpu-settle-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for three quiet CPU observations before aborting",
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--physical-partitions",
        choices=("requested", "serial"),
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

    if args.runs < 1 or args.timeout < 1 or args.cpu_settle_timeout <= 0:
        parser.error("--runs, --timeout, and --cpu-settle-timeout must be positive")
    if args.max_system_cpu_percent is not None and not (
        0 < args.max_system_cpu_percent <= 100
    ):
        parser.error("--max-system-cpu-percent must be in (0, 100]")
    try:
        validate_partition_sweep(args.partitions)
        validate_unique_values(args.formats, "--formats")
        validate_unique_values(args.workloads, "--workloads")
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
    fixture_snapshot = fixture_provenance(paths, args.formats)

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
            "BBI_PHYSICAL_PARTITION_EXPECTATION": args.physical_partitions,
        }
    )
    if patch_value := os.environ.get("POLARS_BIO_PATCH"):
        patch_path = Path(patch_value).expanduser().resolve()
        if not patch_path.is_file():
            parser.error(f"POLARS_BIO_PATCH does not exist: {patch_path}")
        base_env["POLARS_BIO_PATCH"] = str(patch_path)

    declared_refs = {
        "polars_bio_ref": os.environ.get("POLARS_BIO_REF"),
        "datafusion_bio_formats_ref": os.environ.get("DATAFUSION_BIO_FORMATS_REF"),
        "bigtools_ref": os.environ.get("BIGTOOLS_REF"),
    }
    preflight_env = base_env.copy()
    preflight_env.update(
        {
            "THREAD_NUM": "1",
            "POLARS_MAX_THREADS": "1",
            "RAYON_NUM_THREADS": "1",
            "TOKIO_WORKER_THREADS": "1",
        }
    )
    provenance = harness_provenance()
    child_environment = preflight_environment(python, preflight_env, args.timeout)
    verify_harness_unchanged(provenance)
    verify_declared_build_refs(
        child_environment,
        declared_refs,
        patch_declared=bool(base_env.get("POLARS_BIO_PATCH")),
    )

    for round_index in range(args.runs):
        order = round_order(combinations, round_index, args.runs)
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
            ambient_cpu_percent = wait_for_quiet_cpu(
                args.max_system_cpu_percent, args.cpu_settle_timeout
            )
            result = run_one(python, env, args.timeout)
            verify_harness_unchanged(provenance)
            sample_environment = result.pop("environment")
            if sample_environment != child_environment:
                raise AssertionError(
                    "child interpreter environment changed during the sweep"
                )
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            result["ambient_cpu_percent_quiet_window_max"] = ambient_cpu_percent
            raw[f"{format_name}:{workload}:t{partitions}"].append(result)

    verification = verify_fingerprints(raw, args.physical_partitions)
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

    verify_harness_unchanged(provenance)
    verify_fixtures_unchanged(fixture_snapshot, paths)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "metadata": {
            "label": args.label,
            "partitions": args.partitions,
            "formats": args.formats,
            "workloads": args.workloads,
            "physical_partition_expectation": args.physical_partitions,
            "physical_partition_probe": PARTITION_PROBE_KIND,
            "max_system_cpu_percent": args.max_system_cpu_percent,
            "cpu_quiet_samples": (
                CPU_QUIET_SAMPLES if args.max_system_cpu_percent is not None else 1
            ),
            "cpu_settle_timeout_seconds": args.cpu_settle_timeout,
            "files": fixture_snapshot,
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
            "generator": provenance["runner"],
            "harness": provenance,
            "timing_scope": "lazy scan construction, BBI index/header access, decoding, "
            "and the workload-specific Arrow drain, Polars aggregation, or full DataFrame "
            "materialization; collection diagnostics and DataFrame teardown, imports, "
            "thread-pool configuration, source-plan probing, and the untimed workload-path "
            "content replay excluded",
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
