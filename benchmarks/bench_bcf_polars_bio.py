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

    # Project direct typed FORMAT/GT dosage at the DataFusion source, normalize
    # null to snputils' -1 sentinel, and materialize with the streaming engine.
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
