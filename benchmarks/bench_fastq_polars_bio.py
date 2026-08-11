"""Benchmark: FASTQ -> polars-bio -> Polars LazyFrame."""

import os
from benchmarks.common import FASTQ_PATH, run_benchmark

import polars_bio as pb

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))
try:
    pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
except AttributeError:
    pass  # older polars-bio versions don't have set_option

# Collect schema outside timed section
schema_names = pb.scan_fastq(FASTQ_PATH).collect_schema().names()


# Use the streaming engine: LazyFrame.count() on the default in-memory engine
# materializes every column of the whole file before aggregating (~GBs); the
# streaming engine pushes the count down and frees batches as they flow, so peak
# memory reflects the reader, not the materialized dataset.
def benchmark():
    return pb.scan_fastq(FASTQ_PATH).count().collect(engine="streaming").item(0, 0)


run_benchmark(benchmark, f"polars_bio_fastq_t{THREAD_NUM}", columns=schema_names)
