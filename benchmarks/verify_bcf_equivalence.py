"""Verify full-row and full-genotype equivalence for the BCF benchmark."""

import hashlib
import json
import os

import numpy as np
import polars as pl
import polars_bio as pb
import snputils

from benchmarks.bcf_common import (
    BCF_PATH,
    CORE_COLUMNS,
    dosage_expression,
    polars_bio_bcf_scan,
    validate_shape,
    validate_variant,
)

COMPARE_CHUNK_ROWS = int(os.environ.get("BCF_COMPARE_CHUNK_ROWS", "4096"))


def normalized_ids(values) -> np.ndarray:
    return np.fromiter(
        (
            "." if value is None or str(value) in {"", "."} else str(value)
            for value in values
        ),
        dtype=object,
        count=len(values),
    )


def assert_equal(name: str, observed, expected) -> None:
    observed_array = np.asarray(observed)
    expected_array = np.asarray(expected)
    if not np.array_equal(observed_array, expected_array):
        mismatch = np.flatnonzero(observed_array != expected_array)[0]
        raise AssertionError(
            f"{name} differs at row {mismatch}: "
            f"polars-bio={observed_array[mismatch]!r}, snputils={expected_array[mismatch]!r}"
        )


def main() -> None:
    validate_variant()

    raw_scan = polars_bio_bcf_scan()
    metadata = pb.get_metadata(raw_scan)
    polars_samples = metadata["header"]["sample_names"]
    frame = raw_scan.select(
        [*[pl.col(name) for name in CORE_COLUMNS], dosage_expression()]
    ).collect(engine="streaming")

    snp = snputils.read_bcf(
        BCF_PATH,
        fields=["GT", "IID", "REF", "ALT", "#CHROM", "ID", "POS"],
        genotype_mode="dosage",
        chromosome_ploidy="autosomal",
    )
    expected_dosage = snp.genotypes
    if expected_dosage is None or expected_dosage.ndim != 2:
        raise AssertionError("snputils did not return a 2-D dosage matrix")
    if expected_dosage.dtype != np.dtype(np.int8):
        raise AssertionError(
            f"snputils dosage must be int8, got {expected_dosage.dtype}"
        )
    if frame.schema["dosage"] != pl.List(pl.Int8):
        raise AssertionError(
            f"polars-bio dosage must be list[i8], got {frame.schema['dosage']}"
        )

    row_count, sample_count = expected_dosage.shape
    validate_shape(row_count, sample_count)
    validate_shape(frame.height, frame["dosage"].list.len().head(1).item())
    assert_equal("sample order", np.asarray(polars_samples, dtype=object), snp.samples)
    assert_equal("chrom", frame["chrom"].to_numpy(), snp.variants_chrom)
    assert_equal("position", frame["start"].to_numpy(), snp.variants_pos)
    assert_equal("id", normalized_ids(frame["id"]), normalized_ids(snp.variants_id))
    assert_equal("ref", frame["ref"].to_numpy(), snp.variants_ref)
    assert not bool(frame["alt"].str.contains(",", literal=True).any()), (
        "fixture must remain biallelic"
    )
    assert_equal("alt", frame["alt"].to_numpy(), snp.variants_alt)

    dosage_hash = hashlib.sha256()
    observed_series = frame["dosage"]
    for offset in range(0, row_count, COMPARE_CHUNK_ROWS):
        length = min(COMPARE_CHUNK_ROWS, row_count - offset)
        observed = (
            observed_series.slice(offset, length)
            .list.to_array(width=sample_count)
            .to_numpy()
        )
        expected = expected_dosage[offset : offset + length]
        if not np.array_equal(observed, expected):
            mismatch = np.argwhere(observed != expected)[0]
            row, sample = int(mismatch[0]), int(mismatch[1])
            raise AssertionError(
                f"dosage differs at row {offset + row}, sample {sample}: "
                f"polars-bio={observed[row, sample]}, snputils={expected[row, sample]}"
            )
        dosage_hash.update(np.ascontiguousarray(expected).view(np.uint8))

    position_hash = hashlib.sha256(
        np.ascontiguousarray(snp.variants_pos).view(np.uint8)
    ).hexdigest()
    result = {
        "bcf_path": BCF_PATH,
        "rows": row_count,
        "samples": sample_count,
        "dosage_values": row_count * sample_count,
        "polars_bio_dosage_dtype": str(frame.schema["dosage"]),
        "snputils_dosage_dtype": str(expected_dosage.dtype),
        "position_sha256": position_hash,
        "dosage_sha256": dosage_hash.hexdigest(),
        "equivalent": True,
    }
    print(f"BCF_EQUIVALENCE:{json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
