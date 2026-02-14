"""Benchmark: BAM -> polars-bio -> Polars LazyFrame."""

import os
from benchmarks.common import BAM_PATH, BAM_TAGS, BENCH_VARIANT, run_benchmark

import polars_bio as pb

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))
try:
    pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
except AttributeError:
    pass  # older polars-bio versions don't have set_option

# Collect schema outside timed section
if BENCH_VARIANT == "with_tags":
    schema_names = pb.scan_bam(BAM_PATH, tag_fields=BAM_TAGS).collect_schema().names()
else:
    schema_names = pb.scan_bam(BAM_PATH).collect_schema().names()


def benchmark():
    if BENCH_VARIANT == "with_tags":
        return pb.scan_bam(BAM_PATH, tag_fields=BAM_TAGS).count().collect().item(0, 0)
    else:
        return pb.scan_bam(BAM_PATH).count().collect().item(0, 0)


run_benchmark(benchmark, f"polars_bio_bam_{BENCH_VARIANT}_t{THREAD_NUM}", columns=schema_names)
