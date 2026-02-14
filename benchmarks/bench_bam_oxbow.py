"""Benchmark: BAM -> oxbow -> Polars LazyFrame."""

from benchmarks.common import BAM_PATH, BENCH_VARIANT, run_benchmark

import oxbow as ox

# Collect schema outside timed section
if BENCH_VARIANT == "without_tags":
    schema_names = ox.from_bam(BAM_PATH, tag_defs=[]).pl(lazy=True).collect_schema().names()
else:
    schema_names = ox.from_bam(BAM_PATH).pl(lazy=True).collect_schema().names()


def benchmark():
    if BENCH_VARIANT == "without_tags":
        return ox.from_bam(BAM_PATH, tag_defs=[]).pl(lazy=True).count().collect().item(0, 0)
    else:
        return ox.from_bam(BAM_PATH).pl(lazy=True).count().collect().item(0, 0)


run_benchmark(benchmark, f"oxbow_bam_{BENCH_VARIANT}", columns=schema_names)
