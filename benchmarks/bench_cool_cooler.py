"""Benchmark: Cooler (.cool/.mcool) -> cooler chunked pandas -> Polars.

This is the abdenlab/oxbow#180 baseline: the reference `cooler` package reads
pixels as pandas chunks, each chunk is converted to Polars, and the chunks are
concatenated into one DataFrame — the "clunky" LazyFrame workaround that
native scanning is meant to replace.
"""

import os

from benchmarks.common import run_benchmark
from benchmarks.cool_common import (
    COOL_CHUNK_ROWS,
    COOL_WORKLOAD,
    JOINED_COLUMNS,
    REGION,
    cooler_uri,
    workload_name,
)

import cooler
import polars as pl

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))

CLR = cooler.Cooler(cooler_uri())


def _chunk_bounds():
    total = CLR.info["nnz"]
    for lo in range(0, total, COOL_CHUNK_ROWS):
        yield lo, min(lo + COOL_CHUNK_ROWS, total)


def benchmark():
    if COOL_WORKLOAD == "stream_count":
        selector = CLR.pixels(join=False)
        return sum(len(selector[lo:hi]) for lo, hi in _chunk_bounds())
    if COOL_WORKLOAD == "collect_all":
        selector = CLR.pixels(join=True)
        df = pl.concat(
            [pl.from_pandas(selector[lo:hi]) for lo, hi in _chunk_bounds()]
        )
        return df.height
    if COOL_WORKLOAD == "region":
        pixels = CLR.matrix(balance=False, as_pixels=True, join=True).fetch(REGION)
        return pl.from_pandas(pixels).height
    raise ValueError(f"unsupported COOL_WORKLOAD: {COOL_WORKLOAD!r}")


run_benchmark(benchmark, workload_name("cooler", THREAD_NUM), columns=JOINED_COLUMNS)
