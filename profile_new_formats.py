#!/usr/bin/env python3
"""Diagnose source partitions, planning cost, and Arrow/Polars materialization.

Run each configuration in a fresh process. This is a diagnostic companion to
run_new_formats.py, not a replacement for its mandatory oracle checks.
"""

import argparse
import json
import os
import time
from pathlib import Path


def plan_tree(plan):
    return {
        "display": plan.display().strip(),
        "partitions": plan.partition_count,
        "children": [plan_tree(child) for child in plan.children()],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/new_formats/manifest.json")
    )
    parser.add_argument("--format", required=True)
    parser.add_argument("--partitions", type=int, required=True)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.partitions < 1:
        parser.error("--partitions must be positive")
    os.environ["POLARS_MAX_THREADS"] = str(args.partitions)
    import polars as pl
    import polars_bio as pb
    from polars_bio.context import ctx
    from polars_bio.polars_bio import py_read_sql

    from benchmarks.new_formats import read_frame, schema

    root = args.manifest.resolve().parent
    case = next(
        c
        for c in json.loads(args.manifest.read_text())["cases"]
        if c["format"] == args.format
    )
    pb.set_option("datafusion.execution.target_partitions", str(args.partitions))
    paths = [str(root / name) for name in case["paths"]]
    started = time.perf_counter()
    if args.format in ("pdb", "mmcif"):
        pb.register_structure("profile", paths, format=args.format)
    elif args.format == "foldcomp":
        pb.register_foldcomp("profile", paths[0])
    else:
        getattr(pb, "register_" + args.format)(paths[0], "profile")
    columns = ", ".join('"' + c + '"' for c in schema(args.format))
    df = py_read_sql(ctx, f"SELECT {columns} FROM profile")
    registered = time.perf_counter()
    plan = df.execution_plan()
    planned = time.perf_counter()
    result = {
        "format": args.format,
        "requested_partitions": args.partitions,
        "polars_threads": pl.thread_pool_size(),
        "registration_seconds": registered - started,
        "physical_planning_seconds": planned - registered,
        "plan": plan_tree(plan),
    }
    if not args.plan_only:
        # execute_stream makes its own plan, so this time includes planning.
        started = time.perf_counter()
        stream = df.execute_stream()
        stream_ready = time.perf_counter()
        tables = []
        first_batch = None
        for batch in stream:
            tables.append(batch.to_pyarrow())
            if first_batch is None:
                first_batch = time.perf_counter() - started
        finished = time.perf_counter()
        result.update(
            arrow_stream_setup_seconds=stream_ready - started,
            arrow_collect_seconds=finished - started,
            first_batch_seconds=first_batch,
            arrow_rows=sum(b.num_rows for b in tables),
            arrow_batches=len(tables),
        )
        started = time.perf_counter()
        result["polars_rows"] = pl.from_arrow(tables).height
        result["arrow_to_polars_seconds"] = time.perf_counter() - started
        del tables
        started = time.perf_counter()
        frame = read_frame(case, root, "polars-bio", args.partitions)
        result["polars_plugin_seconds"] = time.perf_counter() - started
        assert frame.height == result["arrow_rows"] == result["polars_rows"]
    print(json.dumps(result))


if __name__ == "__main__":
    main()
