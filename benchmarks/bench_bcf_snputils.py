"""Benchmark BCF GT dosage materialization through snputils."""

import snputils

from benchmarks.bcf_common import BCF_PATH, validate_shape, validate_variant
from benchmarks.common import run_benchmark

validate_variant()
_result = None


def benchmark():
    global _result

    _result = snputils.read_bcf(
        BCF_PATH,
        fields=["GT"],
        genotype_mode="dosage",
        chromosome_ploidy="autosomal",
    ).genotypes
    if _result is None or _result.ndim != 2:
        raise AssertionError(
            f"expected a 2-D dosage matrix, got {getattr(_result, 'shape', None)}"
        )
    validate_shape(_result.shape[0], _result.shape[1])
    return _result.shape[0]


run_benchmark(benchmark, "snputils_bcf_dosage", columns=["dosage"])
