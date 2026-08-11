"""Benchmark: VCF -> polars-bio -> Polars LazyFrame."""

import os
from benchmarks.common import VCF_PATH, BENCH_VARIANT, run_benchmark

import polars_bio as pb

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))
try:
    pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
except AttributeError:
    pass  # older polars-bio versions don't have set_option

# Collect schema outside timed section
if BENCH_VARIANT == "without_info":
    schema_names = pb.scan_vcf(VCF_PATH, info_fields=[]).collect_schema().names()
else:
    schema_names = pb.scan_vcf(VCF_PATH).collect_schema().names()


# Use the streaming engine: LazyFrame.count() on the default in-memory engine
# materializes every column of the whole file before aggregating (~GBs); the
# streaming engine pushes the count down and frees batches as they flow, so peak
# memory reflects the reader, not the materialized dataset.
def benchmark():
    if BENCH_VARIANT == "without_info":
        return pb.scan_vcf(VCF_PATH, info_fields=[]).count().collect(engine="streaming").item(0, 0)
    else:
        return pb.scan_vcf(VCF_PATH).count().collect(engine="streaming").item(0, 0)


run_benchmark(benchmark, f"polars_bio_vcf_{BENCH_VARIANT}_t{THREAD_NUM}", columns=schema_names)
