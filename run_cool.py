#!/usr/bin/env python
"""Orchestrate the Cooler (.cool/.mcool) benchmarks.

Runs each (library, workload, threads) configuration in a fresh process,
interleaving iterations across libraries so background drift affects both
sides equally, and writes results to results/cool_results.json.

    .venv/bin/python run_cool.py [--iterations 3] [--resolution 10000]
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent
RESULTS_PATH = REPO_ROOT / "results" / "cool_results.json"

WORKLOADS = ("stream_count", "collect_all", "region")
POLARS_BIO_THREADS = (1, 2, 4, 8)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_child(module: str, workload: str, threads: int, resolution: int):
    env = os.environ.copy()
    env.update(
        {
            "COOL_WORKLOAD": workload,
            "COOL_RESOLUTION": str(resolution),
            "THREAD_NUM": str(threads),
            "POLARS_MAX_THREADS": str(threads),
            "TQDM_DISABLE": "1",
        }
    )
    process = subprocess.run(
        [sys.executable, "-m", module],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(
            f"{module} {workload} t{threads} failed:\n{process.stdout}\n{process.stderr}"
        )
    for line in process.stdout.splitlines():
        if line.startswith("BENCHMARK_RESULT:"):
            return json.loads(line[len("BENCHMARK_RESULT:") :])
    raise RuntimeError(f"{module} produced no BENCHMARK_RESULT line")


def library_versions() -> dict:
    import importlib.metadata

    versions = {}
    for package in ("polars-bio", "polars", "cooler", "pandas", "numpy"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "missing"
    return versions


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--resolution", type=int, default=10000)
    parser.add_argument(
        "--skip-verify", action="store_true", help="skip the equivalence check"
    )
    args = parser.parse_args()

    mcool = Path(
        os.environ.get(
            "MCOOL_PATH", "/Users/mwiewior/research/data/COOL/test.mcool"
        )
    )
    if not mcool.exists():
        print(f"missing dataset: {mcool} — run setup.sh first", file=sys.stderr)
        return 1

    if not args.skip_verify:
        print("verifying polars-bio vs cooler equivalence ...")
        env = os.environ.copy()
        env.update(
            {"COOL_RESOLUTION": str(args.resolution), "TQDM_DISABLE": "1"}
        )
        subprocess.run(
            [sys.executable, "-m", "benchmarks.verify_cool_equivalence"],
            cwd=REPO_ROOT,
            env=env,
            check=True,
        )

    configurations = [
        ("benchmarks.bench_cool_cooler", workload, 1) for workload in WORKLOADS
    ] + [
        ("benchmarks.bench_cool_polars_bio", workload, threads)
        for workload in WORKLOADS
        for threads in POLARS_BIO_THREADS
    ]

    runs = []
    for iteration in range(args.iterations):
        for module, workload, threads in configurations:
            print(f"[iter {iteration + 1}/{args.iterations}] {module} {workload} t{threads}")
            result = run_child(module, workload, threads, args.resolution)
            result["iteration"] = iteration
            runs.append(result)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {
            "path": str(mcool),
            "sha256": file_sha256(mcool),
            "resolution": args.resolution,
        },
        "platform": {
            "machine": platform.machine(),
            "system": platform.system(),
            "python": platform.python_version(),
        },
        "versions": library_versions(),
        "iterations": args.iterations,
        "runs": runs,
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {RESULTS_PATH} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
