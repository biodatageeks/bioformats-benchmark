#!/usr/bin/env python3
"""Compare post-0.35.1 readers, refusing successful results with oracle mismatches."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from prepare_new_formats import sha256

ROOT = Path(__file__).resolve().parent


def child(args):
    # Imports are outside read-to-DataFrame timing, but included in process RSS.
    from benchmarks.new_formats import read_frame

    for name in {
        "polars-bio": ["polars_bio"],
        "biopython": ["Bio.SeqIO", "Bio.PDB.MMCIF2Dict"],
        "easel": ["pyhmmer.easel"],
        "gemmi": ["gemmi"],
        "foldcomp": ["foldcomp", "gemmi"],
    }[args.reader]:
        importlib.import_module(name)
    root = args.manifest.resolve().parent
    case = next(
        c
        for c in json.loads(args.manifest.read_text())["cases"]
        if c["format"] == args.case
    )
    started = time.perf_counter()
    frame = read_frame(case, root, args.reader, args.threads[0])
    elapsed = time.perf_counter() - started
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {
        "format": args.case,
        "reader": args.reader,
        "threads": args.threads[0],
        "seconds": elapsed,
        "peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
        "rows": frame.height,
    }
    # IPC transport, sorting and verification are deliberately outside timing/RSS.
    frame.write_ipc(args.output)
    print("NEW_FORMAT_RESULT:" + json.dumps(result, allow_nan=False))


def run_child(manifest, fmt, reader, threads, output, timeout):
    env = os.environ.copy()
    env.update(
        {
            "POLARS_MAX_THREADS": str(threads),
            "OMP_NUM_THREADS": str(threads),
            "OPENBLAS_NUM_THREADS": str(threads),
            "TQDM_DISABLE": "1",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--child",
            "--manifest",
            str(manifest),
            "--case",
            fmt,
            "--reader",
            reader,
            "--threads",
            str(threads),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"{fmt}/{reader}/t{threads} failed:\n{completed.stdout}\n{completed.stderr}"
        )
    lines = [
        line
        for line in completed.stdout.splitlines()
        if line.startswith("NEW_FORMAT_RESULT:")
    ]
    if len(lines) != 1:
        raise RuntimeError(f"invalid child result: {completed.stdout}")
    return json.loads(lines[0].split(":", 1)[1])


def fingerprint(build_path):
    import polars_bio as pb

    native = importlib.import_module("polars_bio.polars_bio")
    extension = Path(native.__file__)
    build = json.loads(build_path.read_text())
    if (
        build.get("profile") != "release"
        or sha256(extension) != build["extension_sha256"]
    ):
        raise ValueError(
            "release-build record is missing or does not match the imported extension"
        )
    return {**build, "extension_path": str(extension), "python_module": pb.__file__}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def summarize(runs):
    summary = []
    for fmt, reader, threads in sorted(
        {(r["format"], r["reader"], r["threads"]) for r in runs}
    ):
        rows = [
            r
            for r in runs
            if (r["format"], r["reader"], r["threads"]) == (fmt, reader, threads)
        ]
        seconds = [r["seconds"] for r in rows]
        summary.append(
            {
                "format": fmt,
                "reader": reader,
                "threads": threads,
                "runs": len(rows),
                "rows": rows[0]["rows"],
                "seconds_median": statistics.median(seconds),
                "seconds_stdev": statistics.stdev(seconds) if len(rows) > 1 else None,
                "peak_rss_mib_median": statistics.median(
                    r["peak_rss_bytes"] for r in rows
                )
                / 1024**2,
            }
        )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/new_formats/manifest.json")
    )
    parser.add_argument(
        "--build-record", type=Path, default=Path("data/new_formats/build.json")
    )
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--threads", type=int, nargs="+", default=[1, 4, 8])
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=["a2m", "a3m", "sto", "pdb", "mmcif", "foldcomp"],
    )
    parser.add_argument("--output", type=Path, default=Path("results/new_formats.json"))
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--case", help=argparse.SUPPRESS)
    parser.add_argument("--reader", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.iterations < 1 or any(t < 1 for t in args.threads) or args.timeout < 1:
        parser.error("iterations, threads and timeout must be positive")
    if args.child:
        child(args)
        return

    import polars as pl
    import psutil

    from benchmarks.new_formats import compare_frames

    manifest = args.manifest.resolve()
    data = json.loads(manifest.read_text())
    cases = [
        c for c in data["cases"] if not args.formats or c["format"] in args.formats
    ]
    if not cases:
        parser.error("manifest has no selected formats")
    for case in cases:
        for file in case["files"]:
            path = manifest.parent / file["path"]
            if path.stat().st_size != file["bytes"] or sha256(path) != file["sha256"]:
                raise ValueError(f"dataset checksum mismatch: {path}")
    payload = {
        "status": "running",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build": fingerprint(args.build_record),
        "dataset_manifest": data,
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_logical": psutil.cpu_count(),
            "memory_bytes": psutil.virtual_memory().total,
        },
        "versions": {
            name: importlib.metadata.version(name)
            for name in [
                "polars-bio",
                "polars",
                "pyarrow",
                "datafusion",
                "numpy",
                "gemmi",
                "biopython",
                "foldcomp",
                "pyhmmer",
            ]
        },
        "method": {
            "iterations": args.iterations,
            "timing": "fresh subprocess; imports excluded; read + materialize canonical Polars DataFrame; comparison/IPC excluded",
            "rss": "process high-water RSS captured before IPC/verification, includes imports",
            "cache": "warm filesystem cache; untimed oracle read before runs; no OS cache flush",
            "order": "reader order rotated by iteration; sequential processes",
            "tolerances": {
                "text_absolute": 1e-9,
                "foldcomp_absolute": 1e-4,
                "relative": 0,
            },
            "quality": "all rows, all columns in the documented canonical schema; exact types, keys, nulls, strings and annotations",
        },
        "verification": [],
        "runs": [],
    }
    write_json(args.output, payload)
    try:
        with tempfile.TemporaryDirectory(prefix="new-formats-") as temporary:
            temp = Path(temporary)
            for case in cases:
                fmt, oracle = case["format"], case["oracle"]
                print(f"{fmt}: generating complete {oracle} oracle", flush=True)
                reference_path = temp / "reference.arrow"
                run_child(manifest, fmt, oracle, 1, reference_path, args.timeout)
                expected = pl.read_ipc(reference_path, memory_map=False)
                # Require a second independent CIF parser in addition to Gemmi.
                # The measured v1.13.0 provider has its own Rust CIF tokenizer.
                if fmt == "mmcif":
                    independent_path = temp / "independent.arrow"
                    run_child(
                        manifest, fmt, "biopython", 1, independent_path, args.timeout
                    )
                    check = compare_frames(
                        pl.read_ipc(independent_path, memory_map=False), expected, fmt
                    )
                    payload["verification"].append(
                        {"format": fmt, "reader": "biopython-vs-gemmi", **check}
                    )
                configurations = [(oracle, 1)] + [
                    ("polars-bio", t) for t in dict.fromkeys(args.threads)
                ]
                for iteration in range(args.iterations):
                    offset = iteration % len(configurations)
                    for reader, threads in (
                        configurations[offset:] + configurations[:offset]
                    ):
                        print(
                            f"{fmt}: iteration {iteration + 1}/{args.iterations} {reader} t{threads}",
                            flush=True,
                        )
                        result_path = temp / "actual.arrow"
                        result = run_child(
                            manifest, fmt, reader, threads, result_path, args.timeout
                        )
                        check = compare_frames(
                            pl.read_ipc(result_path, memory_map=False), expected, fmt
                        )
                        payload["runs"].append(
                            {**result, "iteration": iteration + 1, "quality": check}
                        )
                        write_json(args.output, payload)
                del expected
        payload["status"] = "passed"
    except Exception as error:
        payload["status"] = "failed"
        payload["error"] = str(error)
        write_json(args.output, payload)
        raise
    payload["summary"] = summarize(payload["runs"])
    write_json(args.output, payload)
    print(f"Wrote {args.output}: {len(payload['runs'])} runs, zero oracle mismatches")


if __name__ == "__main__":
    main()
