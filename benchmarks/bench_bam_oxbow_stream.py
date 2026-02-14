"""Benchmark: BAM -> oxbow batches -> pl.from_arrow per batch (streaming)."""

from benchmarks.common import BAM_PATH, BENCH_VARIANT, run_benchmark

import oxbow as ox
import polars as pl


def benchmark():
    if BENCH_VARIANT == "without_tags":
        ds = ox.from_bam(BAM_PATH, tag_defs=[], batch_size=8192)
    else:
        ds = ox.from_bam(BAM_PATH, batch_size=8192)
    row_count = 0
    schema_names = None
    for batch in ds.batches():
        df = pl.from_arrow(batch)
        if schema_names is None:
            schema_names = df.columns
        row_count += df.lazy().count().collect().item(0, 0)
    return (row_count, schema_names or [])


run_benchmark(benchmark, f"oxbow_stream_bam_{BENCH_VARIANT}")
