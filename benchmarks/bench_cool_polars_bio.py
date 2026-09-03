"""Benchmark: Cooler (.cool/.mcool) -> polars-bio -> Polars."""

import os

from benchmarks.common import run_benchmark
from benchmarks.cool_common import (
    COOL_WORKLOAD,
    JOINED_COLUMNS,
    REGION,
    cooler_uri,
    workload_name,
)

import polars as pl
import polars_bio as pb

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))
pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))

URI = cooler_uri()


def _region_filter():
    chrom, start, end = REGION
    # cooler's matrix().fetch(region) constrains BOTH axes of the contact
    # matrix; the chrom1/start1/end1 conjuncts are pushdown-eligible, the
    # second-axis conjuncts are applied client-side.
    return (
        (pl.col("chrom1") == chrom)
        & (pl.col("start1") >= start)
        & (pl.col("end1") <= end)
        & (pl.col("chrom2") == chrom)
        & (pl.col("start2") >= start)
        & (pl.col("end2") <= end)
    )


def benchmark():
    if COOL_WORKLOAD == "stream_count":
        return (
            pb.scan_cool(URI, use_zero_based=True)
            .count()
            .collect(engine="streaming")
            .item(0, 0)
        )
    if COOL_WORKLOAD == "collect_all":
        return pb.scan_cool(URI, use_zero_based=True).collect().height
    if COOL_WORKLOAD == "region":
        return (
            pb.scan_cool(URI, use_zero_based=True)
            .filter(_region_filter())
            .collect()
            .height
        )
    raise ValueError(f"unsupported COOL_WORKLOAD: {COOL_WORKLOAD!r}")


run_benchmark(
    benchmark, workload_name("polars_bio", THREAD_NUM), columns=JOINED_COLUMNS
)
