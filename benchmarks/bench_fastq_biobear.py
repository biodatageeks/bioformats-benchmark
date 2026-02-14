"""Benchmark: FASTQ -> biobear -> Polars DataFrame."""

from benchmarks.common import FASTQ_PATH_BB, run_benchmark

import biobear as bb


def benchmark():
    session = bb.connect()
    df = session.read_fastq_file(FASTQ_PATH_BB).to_polars()
    return (df.select(__import__("polars").len()).item(), df.columns)


run_benchmark(benchmark, "biobear_fastq")
