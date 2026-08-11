"""Benchmark BCF GT dosage materialization through a streaming polars-bio scan."""

import os

import polars_bio as pb

from benchmarks.bcf_common import (
    dosage_expression,
    polars_bio_bcf_scan,
    validate_shape,
    validate_variant,
)
from benchmarks.common import run_benchmark

THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))
pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
validate_variant()
_result = None


def benchmark():
    global _result

    # Project FORMAT/GT at the DataFusion source, convert it to Int8 dosage in
    # Polars, and materialize with the streaming engine. The result is one list
    # per variant and is logically equivalent to snputils' 2-D dosage ndarray.
    _result = (
        polars_bio_bcf_scan().select(dosage_expression()).collect(engine="streaming")
    )
    validate_shape(_result.height)
    return _result.height


run_benchmark(
    benchmark,
    f"polars_bio_bcf_dosage_t{THREAD_NUM}",
    columns=["dosage"],
)
