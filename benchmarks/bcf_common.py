"""Shared BCF dosage workload used by the polars-bio and snputils runners."""

import os

from benchmarks.common import BCF_PATH

BCF_VARIANT = os.environ.get("BENCH_VARIANT", "dosage")
EXPECTED_ROWS = int(os.environ.get("BCF_EXPECTED_ROWS", "993881"))
EXPECTED_SAMPLES = int(os.environ.get("BCF_EXPECTED_SAMPLES", "2548"))
CORE_COLUMNS = ["chrom", "start", "id", "ref", "alt"]

# The IGSR chromosome 22 fixture is phased, diploid, and biallelic. Keeping this
# map strict makes an unexpected genotype representation fail the benchmark
# instead of silently producing incomparable output.
GT_TO_DOSAGE = {
    "0|0": 0,
    "0|1": 1,
    "1|0": 1,
    "1|1": 2,
    ".|.": -1,
    "./.": -1,
}


def validate_variant() -> None:
    if BCF_VARIANT != "dosage":
        raise ValueError(
            f"unsupported BCF benchmark variant: {BCF_VARIANT!r}; expected 'dosage'"
        )


def polars_bio_bcf_scan():
    """Return the lazy, projection-pushed BCF scan before materialization."""
    import polars_bio as pb

    return pb.scan_vcf(
        BCF_PATH,
        info_fields=[],
        format_fields=["GT"],
        use_zero_based=False,
        projection_pushdown=True,
    )


def dosage_expression():
    """Convert nested phased GT strings into snputils-compatible ALT dosage."""
    import polars as pl

    return (
        pl.col("genotypes")
        .struct.field("GT")
        .list.eval(pl.element().replace_strict(GT_TO_DOSAGE, return_dtype=pl.Int8))
        .alias("dosage")
    )


def validate_shape(row_count: int, sample_count: int | None = None) -> None:
    if EXPECTED_ROWS and row_count != EXPECTED_ROWS:
        raise AssertionError(f"expected {EXPECTED_ROWS} BCF rows, got {row_count}")
    if (
        sample_count is not None
        and EXPECTED_SAMPLES
        and sample_count != EXPECTED_SAMPLES
    ):
        raise AssertionError(
            f"expected {EXPECTED_SAMPLES} BCF samples, got {sample_count}"
        )
