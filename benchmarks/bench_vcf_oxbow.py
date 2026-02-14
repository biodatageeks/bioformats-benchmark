"""Benchmark: VCF -> oxbow -> Polars LazyFrame."""

from benchmarks.common import VCF_PATH, BENCH_VARIANT, run_benchmark

import oxbow as ox

# Collect schema outside timed section
if BENCH_VARIANT == "without_info":
    schema_names = ox.from_vcf(VCF_PATH, info_fields=[]).pl(lazy=True).collect_schema().names()
else:
    schema_names = ox.from_vcf(VCF_PATH).pl(lazy=True).collect_schema().names()


def benchmark():
    if BENCH_VARIANT == "without_info":
        return ox.from_vcf(VCF_PATH, info_fields=[]).pl(lazy=True).count().collect().item(0, 0)
    else:
        return ox.from_vcf(VCF_PATH).pl(lazy=True).count().collect().item(0, 0)


run_benchmark(benchmark, f"oxbow_vcf_{BENCH_VARIANT}", columns=schema_names)
