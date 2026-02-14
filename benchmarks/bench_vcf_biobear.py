"""Benchmark: VCF -> biobear -> Polars DataFrame.

Only runs for BENCH_VARIANT == "without_info" (biobear fails on full info VCF).
"""

import sys
from benchmarks.common import VCF_PATH, BENCH_VARIANT, run_benchmark

import biobear as bb
import polars as pl


if BENCH_VARIANT != "without_info":
    print(f"SKIP: biobear VCF only supports without_info variant (got {BENCH_VARIANT})")
    sys.exit(0)


def benchmark():
    session = bb.connect()
    df = session.sql(f"""
        SELECT chrom, pos, id, ref, alt, qual, filter
        FROM vcf_scan('{VCF_PATH}')
    """).to_polars()
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"biobear_vcf_{BENCH_VARIANT}")
