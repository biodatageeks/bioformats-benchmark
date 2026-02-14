"""Benchmark: FASTQ -> oxbow -> Polars LazyFrame."""

from benchmarks.common import FASTQ_PATH, run_benchmark

import oxbow as ox

# Collect schema outside timed section
schema_names = ox.from_fastq(FASTQ_PATH).pl(lazy=True).collect_schema().names()


def benchmark():
    return ox.from_fastq(FASTQ_PATH).pl(lazy=True).count().collect().item(0, 0)


run_benchmark(benchmark, "oxbow_fastq", columns=schema_names)
