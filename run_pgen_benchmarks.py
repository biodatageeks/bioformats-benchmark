#!/usr/bin/env python3
"""Benchmark equivalent PGEN genotype matrices across Python readers.

Every reader materializes the same canonical ``float32`` ALT-dosage array from
the same PLINK 2 fileset. The runner rejects any cross-reader disagreement in
shape, variant positions, sample order, or values, so a completed run is
evidence that the readers agree, not just that they finished.

polars-bio MUST be built with an optimized profile before measuring. A plain
``maturin develop`` is a debug build and has been observed 3.1x slower, which is
enough to invert the headline comparison. Build it the way setup.sh does:

    RUSTFLAGS="-C target-cpu=native" maturin develop --release --locked
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

READERS = ("polars-bio", "snputils", "pgenlib")
MODES = ("dosage", "hardcall")
# pgenlib is PLINK 2's own reference implementation and the oracle every other
# reader is checked against.
REFERENCE_READER = "pgenlib"
DISTRIBUTIONS = {
    "polars-bio": "polars-bio",
    "snputils": "snputils",
    "pgenlib": "Pgenlib",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_result(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("PGEN_RESULT:"):
            return json.loads(line.removeprefix("PGEN_RESULT:"))
    raise RuntimeError(f"child did not emit PGEN_RESULT:\n{output}")


def run_one(python: str, env: dict[str, str], timeout: int) -> dict:
    completed = subprocess.run(
        [python, "-m", "benchmarks.pgen_matrix"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
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
        "reader": runs[0]["reader"],
        "threads": runs[0]["threads"],
        "time_seconds_median": round(statistics.median(times), 3),
        "time_seconds_mean": round(statistics.mean(times), 3),
        "time_seconds_stdev": round(statistics.stdev(times), 3)
        if len(times) > 1
        else 0.0,
        "peak_rss_mb_median": round(statistics.median(memories), 1),
        "peak_rss_mb_mean": round(statistics.mean(memories), 1),
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


def polars_bio_build_fingerprint() -> dict[str, object]:
    """Record which polars-bio artifact was measured.

    A debug build is roughly 3x slower here, so the profile is part of the
    result rather than an assumption. The extension size is the cheapest
    reliable discriminator: debug carries symbols and is far larger.
    """
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
            {"name": ext.name, "size_bytes": ext.stat().st_size} for ext in extensions
        ],
        "declared_profile": os.environ.get("POLARS_BIO_BUILD_PROFILE"),
        "declared_rustflags": os.environ.get("POLARS_BIO_RUSTFLAGS"),
    }


def run_verification(
    python: str,
    env: dict[str, str],
    left: str,
    right: str,
    mode: str,
    timeout: int,
    selftest: bool,
) -> dict:
    child_env = env.copy()
    child_env.update(
        {
            "PGEN_READER": right,
            "PGEN_MODE": mode,
            "PGEN_VERIFY_LEFT": left,
            "PGEN_VERIFY_RIGHT": right,
            "THREAD_NUM": "1",
            "POLARS_MAX_THREADS": "1",
        }
    )
    if selftest:
        child_env["PGEN_VERIFY_SELFTEST"] = "1"
    completed = subprocess.run(
        [python, "-m", "benchmarks.pgen_verify"],
        check=True,
        capture_output=True,
        text=True,
        env=child_env,
        timeout=timeout,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("PGEN_VERIFY:"):
            return json.loads(line.removeprefix("PGEN_VERIFY:"))
    raise RuntimeError(f"child did not emit PGEN_VERIFY:\n{completed.stdout}")


def check_equivalence(runs: list[dict]) -> dict:
    """Fail unless every reader produced the same content."""
    reference = next(
        (run for run in runs if run["reader"] == REFERENCE_READER), runs[0]
    )
    # Shape and identity must match exactly for the comparison to mean anything,
    # so a disagreement there is a hard failure.
    for run in runs:
        for field in ("rows", "samples", "values", "position_sha256", "sample_sha256"):
            if run[field] != reference[field]:
                raise AssertionError(
                    f"{run['reader']} (t={run['threads']}) disagrees with "
                    f"{reference['reader']} in {field}: "
                    f"{run[field]!r} != {reference[field]!r}"
                )

    # polars-bio is the reader under test, so it must reproduce the oracle bit
    # for bit at every partition count.
    for run in runs:
        if (
            run["reader"] == "polars-bio"
            and run["value_sha256"] != reference["value_sha256"]
        ):
            raise AssertionError(
                f"polars-bio (t={run['threads']}) does not reproduce "
                f"{reference['reader']} exactly: {run['value_sha256']} != "
                f"{reference['value_sha256']}"
            )

    bit_identical = sorted(
        {
            run["reader"]
            for run in runs
            if run["value_sha256"] == reference["value_sha256"]
        }
    )
    differing = sorted(
        {
            run["reader"]
            for run in runs
            if run["value_sha256"] != reference["value_sha256"]
        }
    )
    return {
        "reference_reader": reference["reader"],
        "rows": reference["rows"],
        "samples": reference["samples"],
        "values": reference["values"],
        "position_sha256": reference["position_sha256"],
        "sample_sha256": reference["sample_sha256"],
        "value_sha256": reference["value_sha256"],
        "readers_checked": sorted({run["reader"] for run in runs}),
        "thread_counts_checked": sorted({run["threads"] for run in runs}),
        "bit_identical_to_reference": bit_identical,
        "differing_from_reference": differing,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=["dosage"])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--readers", nargs="+", choices=READERS, default=list(READERS))
    parser.add_argument(
        "--pgen", default="/Users/mwiewior/research/data/PGEN/chr22.first-25000.pgen"
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
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="skip the element-wise pass; the per-reader hashes still have to agree",
    )
    parser.add_argument("--output", default="results/pgen_reader_benchmark.json")
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive")
    if any(value < 1 for value in args.polars_bio_partitions):
        parser.error("--polars-bio-partitions values must be positive")

    path = Path(args.pgen).expanduser().resolve()
    if not path.is_file():
        parser.error(f"PGEN file does not exist: {path}")
    for suffix in (".pvar", ".psam"):
        companion = path.with_suffix(suffix)
        if not companion.is_file():
            parser.error(f"missing companion: {companion}")

    combinations = []
    for mode in args.modes:
        for reader in args.readers:
            threads = args.polars_bio_partitions if reader == "polars-bio" else [1]
            combinations.extend((mode, reader, count) for count in threads)
    raw = {f"{mode}:{reader}:t{count}": [] for mode, reader, count in combinations}

    base_env = os.environ.copy()
    base_env.update(
        {
            "PGEN_PATH": str(path),
            "PGEN_EXPECTED_ROWS": str(args.expected_rows),
            "PGEN_EXPECTED_SAMPLES": str(args.expected_samples),
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "TQDM_DISABLE": "1",
        }
    )

    for round_index in range(args.runs):
        # Rotate and alternate direction so a reader is not always measured on
        # the same side of any thermal or page-cache drift within a round.
        shift = round_index % len(combinations)
        order = combinations[shift:] + combinations[:shift]
        if round_index % 2:
            order.reverse()
        for order_index, (mode, reader, threads) in enumerate(order, start=1):
            print(
                f"Round {round_index + 1}/{args.runs}, "
                f"{order_index}/{len(order)}: {mode} {reader} t={threads}",
                flush=True,
            )
            env = base_env.copy()
            env["PGEN_READER"] = reader
            env["PGEN_MODE"] = mode
            env["THREAD_NUM"] = str(threads)
            env["POLARS_MAX_THREADS"] = str(threads)
            env["RAYON_NUM_THREADS"] = str(threads)
            result = run_one(args.python, env, args.timeout)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            print(
                f"    {result['time_seconds']:.3f}s  rss={result['peak_rss_mb']:.0f}MB"
                f"  [{result['dtype']}, {result['output_bytes'] / 1e9:.2f} GB]",
                flush=True,
            )
            raw[f"{mode}:{reader}:t{threads}"].append(result)

    # Equivalence is checked within a mode: the two modes deliberately produce
    # different dtypes, so cross-mode hashes are not comparable.
    equivalence = {}
    for mode in args.modes:
        mode_runs = [
            run
            for key, runs in raw.items()
            if key.startswith(f"{mode}:")
            for run in runs
        ]
        equivalence[mode] = check_equivalence(mode_runs)

    verifications = []
    if not args.skip_verification:
        for mode in args.modes:
            for reader in args.readers:
                if reader == REFERENCE_READER:
                    continue
                print(
                    f"\nVerifying {reader} against {REFERENCE_READER} ({mode})",
                    flush=True,
                )
                verifications.append(
                    run_verification(
                        args.python,
                        base_env,
                        reader,
                        REFERENCE_READER,
                        mode,
                        args.timeout,
                        # Prove the comparison can fail, on the reader under test.
                        selftest=(reader == "polars-bio"),
                    )
                )
        for check in verifications:
            if check["left"] == "polars-bio" and check["bitwise_differences"]:
                raise AssertionError(
                    f"polars-bio differs from {check['right']} in "
                    f"{check['bitwise_differences']} of {check['cells']} cells "
                    f"({check['mode']})"
                )

    results = {mode: {} for mode in args.modes}
    for mode, reader, threads in combinations:
        key = reader if reader != "polars-bio" else f"polars-bio-t{threads}"
        results[mode][key] = summarize(raw[f"{mode}:{reader}:t{threads}"])

    # pgenlib is PLINK 2's own reader and the fastest baseline available, so it
    # is the primary reference. snputils is reported alongside it because it is
    # the library this comparison was originally written against.
    comparisons = {}
    for mode, readers in results.items():
        baselines = {name: readers.get(name) for name in ("pgenlib", "snputils")}
        comparisons[mode] = {
            key: {
                f"speedup_over_{name}": round(
                    baseline["time_seconds_median"] / summary["time_seconds_median"], 3
                )
                for name, baseline in baselines.items()
                if baseline is not None and key != name
            }
            | {
                f"peak_rss_ratio_vs_{name}": round(
                    summary["peak_rss_mb_median"] / baseline["peak_rss_mb_median"], 3
                )
                for name, baseline in baselines.items()
                if baseline is not None and key != name
            }
            for key, summary in readers.items()
        }

    payload = {
        "metadata": {
            "pgen_path": str(path),
            "pgen_size_bytes": path.stat().st_size,
            "pgen_sha256": file_sha256(path),
            "pvar_size_bytes": path.with_suffix(".pvar").stat().st_size,
            "rows": args.expected_rows,
            "samples": args.expected_samples,
            "modes": args.modes,
            "polars_bio_partitions": args.polars_bio_partitions,
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
            "workload": "ALT allele dosage per sample per variant, float32, "
            "missing calls as NaN",
            "timing_scope": "fileset open, companion parsing, record decoding, and "
            "final C-contiguous float32 materialization; imports and thread-pool "
            "configuration excluded",
            "equivalence_note": "value and position hashes are taken after sorting "
            "rows by position, because a multi-partition scan may emit rows out of "
            "source order; emission_order_descents records when it did",
        },
        "equivalence": equivalence,
        "verifications": verifications,
        "results": results,
        "comparisons": comparisons,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nAll readers agree. Wrote {output}")


if __name__ == "__main__":
    main()
