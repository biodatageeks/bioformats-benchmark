#!/usr/bin/env python3
"""Run isolated, repeatable BCF timing and peak-RSS benchmarks."""

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

from benchmarks.common import BCF_PATH

RUNNERS = {
    "polars-bio": "benchmarks.bench_bcf_polars_bio",
    "snputils": "benchmarks.bench_bcf_snputils",
}
SNPUTILS_REF = "bdb1a56b52a6b16210d60e347d33d023dc98352f"


def parse_result(output: str, prefix: str) -> dict:
    for line in output.splitlines():
        if line.startswith(prefix):
            return json.loads(line.removeprefix(prefix))
    raise RuntimeError(f"process did not emit {prefix!r}:\n{output}")


def run_module(
    python: str,
    module: str,
    env: dict[str, str],
    *,
    result_prefix: str = "BENCHMARK_RESULT:",
) -> dict:
    completed = subprocess.run(
        [python, "-m", module],
        check=False,
        capture_output=True,
        text=True,
        env=env,
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
    return parse_result(completed.stdout, result_prefix)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def summarize(results: list[dict]) -> dict:
    times = [result["time_seconds"] for result in results]
    memories = [result["peak_rss_mb"] for result in results]
    return {
        "runs": len(results),
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
        "raw": results,
    }


def compare_summaries(summary: dict[str, dict]) -> dict:
    polars = summary["polars-bio"]
    snputils = summary["snputils"]
    polars_time = polars["time_seconds_median"]
    snputils_time = snputils["time_seconds_median"]
    polars_memory = polars["peak_rss_mb_median"]
    snputils_memory = snputils["peak_rss_mb_median"]
    return {
        "snputils_time_speedup": round(polars_time / snputils_time, 3),
        "polars_bio_peak_rss_advantage": round(snputils_memory / polars_memory, 3),
        "polars_bio_peak_rss_reduction_percent": round(
            100 * (1 - polars_memory / snputils_memory), 1
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--skip-verify", action="store_true")
    parser.add_argument("--output", default="results/bcf_benchmark_results.json")
    args = parser.parse_args()
    if args.runs < 1 or args.threads < 1:
        parser.error("--runs and --threads must be positive")

    bcf_path = Path(BCF_PATH).expanduser().resolve()
    if not bcf_path.is_file():
        parser.error(f"BCF file does not exist: {bcf_path}")

    env = os.environ.copy()
    env.update(
        {
            "BCF_PATH": str(bcf_path),
            "BENCH_VARIANT": "dosage",
            "THREAD_NUM": str(args.threads),
            "POLARS_MAX_THREADS": str(args.threads),
            "RAYON_NUM_THREADS": str(args.threads),
            "OMP_NUM_THREADS": str(args.threads),
            "OPENBLAS_NUM_THREADS": str(args.threads),
            "MKL_NUM_THREADS": str(args.threads),
            "VECLIB_MAXIMUM_THREADS": str(args.threads),
            "NUMEXPR_NUM_THREADS": str(args.threads),
            "TQDM_DISABLE": "1",
        }
    )

    equivalence = None
    if not args.skip_verify:
        equivalence = run_module(
            args.python,
            "benchmarks.verify_bcf_equivalence",
            env,
            result_prefix="BCF_EQUIVALENCE:",
        )

    raw: dict[str, list[dict]] = {name: [] for name in RUNNERS}
    names = list(RUNNERS)
    for round_index in range(args.runs):
        order = names if round_index % 2 == 0 else list(reversed(names))
        for order_index, name in enumerate(order, start=1):
            print(f"\nRound {round_index + 1}/{args.runs}: {name}")
            result = run_module(args.python, RUNNERS[name], env)
            result["round"] = round_index + 1
            result["order_in_round"] = order_index
            raw[name].append(result)

    summary = {name: summarize(results) for name, results in raw.items()}
    metadata = {
        "format": "BCF",
        "variant": "dosage",
        "path": str(bcf_path),
        "file_size_bytes": bcf_path.stat().st_size,
        "file_sha256": file_sha256(bcf_path),
        "threads": args.threads,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "logical_cpu_count": psutil.cpu_count(logical=True),
        "physical_cpu_count": psutil.cpu_count(logical=False),
        "memory_total_bytes": psutil.virtual_memory().total,
        "polars_bio_version": importlib.metadata.version("polars-bio"),
        "polars_version": importlib.metadata.version("polars"),
        "pyarrow_version": importlib.metadata.version("pyarrow"),
        "numpy_version": importlib.metadata.version("numpy"),
        "snputils_version": importlib.metadata.version("snputils"),
        "snputils_ref": SNPUTILS_REF,
        "polars_bio_ref": os.environ.get("POLARS_BIO_REF"),
        "datafusion_bio_formats_ref": os.environ.get("DATAFUSION_BIO_FORMATS_REF"),
        "cache_state": "warm; full equivalence scan precedes timed rounds",
        "timing_scope": "read, decode, dosage conversion, and materialization; imports excluded",
        "memory_metric": "fresh-process peak RSS including retained materialized output",
    }
    payload = {
        "metadata": metadata,
        "equivalence": equivalence,
        "results": summary,
        "comparison": compare_summaries(summary),
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"\nBCF_BENCHMARK_SUMMARY:{json.dumps(payload, sort_keys=True)}")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
