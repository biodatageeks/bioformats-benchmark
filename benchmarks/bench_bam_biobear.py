"""Benchmark: BAM -> biobear -> Polars DataFrame."""

from benchmarks.common import BAM_PATH, BENCH_VARIANT, run_benchmark

import biobear as bb
import polars as pl


def benchmark():
    session = bb.connect()
    if BENCH_VARIANT == "without_tags":
        df = session.sql(f"""
            SELECT name, flag, reference, start, "end", mapping_quality, cigar,
                   mate_reference, sequence, quality_score
            FROM bam_scan('{BAM_PATH}')
        """).to_polars()
    else:
        df = session.read_bam_file(BAM_PATH).to_polars()
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"biobear_bam_{BENCH_VARIANT}")
