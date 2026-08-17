"""Read one PLINK 2 fileset into a canonical float32 ALT-dosage matrix.

Every reader materializes the same array from the same `.pgen`, so a completed
run is evidence that the readers agree, not just that they finished. One reader
per child process, so a measurement never observes another reader's warm state.

The matrix is ALT allele count per sample per variant: 0, 1, or 2 for a diploid
hardcall, NaN where the call is missing. PLINK 2 encodes missing as -9; that is
normalized to NaN here so every reader's missing cells compare equal.
"""

from __future__ import annotations

import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path

import numpy as np

READER = os.environ["PGEN_READER"].lower()
INPUT_PATH = Path(os.environ["PGEN_PATH"]).expanduser()
EXPECTED_ROWS = int(os.environ["PGEN_EXPECTED_ROWS"])
EXPECTED_SAMPLES = int(os.environ["PGEN_EXPECTED_SAMPLES"])
THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))


def _companion(suffix: str) -> Path:
    return INPUT_PATH.with_suffix(suffix)


def _read_polars_bio() -> tuple[np.ndarray, np.ndarray, list[str]]:
    import polars_bio as pb

    pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
    scan = pb.scan_pgen(
        str(INPUT_PATH),
        genotype_fields=["GT"],
        use_zero_based=False,
        projection_pushdown=True,
    )
    frame = scan.select("start", "genotypes").collect()
    positions = frame["start"].to_numpy().astype(np.int64, copy=False)
    table = frame.select("genotypes").to_arrow().column("genotypes").combine_chunks()
    struct = table.chunk(0) if hasattr(table, "chunk") else table

    gt = struct.field("GT")
    samples_per_row = gt.flatten()  # FixedSizeList<uint16, 2>, one per sample
    alleles = samples_per_row.flatten().to_numpy(zero_copy_only=False)
    # Sum the two allele slots straight into the float32 output. The strided
    # views avoid materializing an intermediate pairs array, which at
    # whole-chromosome scale is another 10 GB on top of the Arrow buffer and
    # the output, and would be charged to polars-bio as reader overhead.
    dosage = np.add(alleles[0::2], alleles[1::2], dtype=np.float32)
    if samples_per_row.null_count:
        dosage[np.asarray(samples_per_row.is_null()).reshape(-1)] = np.nan
    matrix = np.ascontiguousarray(dosage.reshape(len(gt), -1))

    header = pb.get_metadata(pb.scan_pgen(str(INPUT_PATH), genotype_fields=["GT"]))[
        "header"
    ]
    return matrix, positions, list(header["sample_names"])


def _read_snputils() -> tuple[np.ndarray, np.ndarray, list[str]]:
    from snputils import PGENReader

    obj = PGENReader(str(INPUT_PATH)).read()
    calls = np.asarray(obj.calldata_gt)
    if calls.ndim == 3:
        # (variants, samples, ploidy) per-haplotype calls.
        missing = (calls < 0).any(axis=2)
        dosage = calls.sum(axis=2).astype(np.float32)
    else:
        missing = calls < 0
        dosage = calls.astype(np.float32)
    dosage[missing] = np.nan
    matrix = np.ascontiguousarray(dosage, dtype=np.float32)
    positions = np.asarray(obj.variants_pos).astype(np.int64, copy=False)
    return matrix, positions, [str(name) for name in np.asarray(obj.samples)]


def _read_pgenlib() -> tuple[np.ndarray, np.ndarray, list[str]]:
    import pgenlib

    reader = pgenlib.PgenReader(str(INPUT_PATH).encode())
    rows = reader.get_variant_ct()
    cols = reader.get_raw_sample_ct()
    counts = np.empty((rows, cols), dtype=np.int8)
    buffer = np.empty(cols, dtype=np.int8)
    for index in range(rows):
        reader.read(index, buffer)
        counts[index] = buffer
    reader.close()

    dosage = counts.astype(np.float32)
    dosage[counts < 0] = np.nan
    matrix = np.ascontiguousarray(dosage)

    # pgenlib reads only the .pgen, so positions and sample identifiers are
    # parsed from the companions here. That keeps the oracle independent of
    # polars-bio rather than borrowing its companion parsing.
    positions = np.array(
        [
            int(line.split("\t")[1])
            for line in _companion(".pvar").read_text().splitlines()
            if line and not line.startswith("#")
        ],
        dtype=np.int64,
    )
    psam_lines = [line for line in _companion(".psam").read_text().splitlines() if line]
    iid_column = psam_lines[0].lstrip("#").split("\t").index("IID")
    samples = [line.split("\t")[iid_column] for line in psam_lines[1:]]
    return matrix, positions, samples


READERS = {
    "polars-bio": _read_polars_bio,
    "snputils": _read_snputils,
    "pgenlib": _read_pgenlib,
}


def make_reader(name: str):
    try:
        return READERS[name]
    except KeyError:
        raise SystemExit(f"unknown reader {name!r}; expected one of {sorted(READERS)}")


read = make_reader(READER)


def _hash_rows_in_order(matrix: np.ndarray, order: np.ndarray) -> str:
    digest = hashlib.sha256()
    for index in order:
        digest.update(np.ascontiguousarray(matrix[index]).tobytes())
    return digest.hexdigest()


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> None:
    start = time.perf_counter()
    matrix, positions, samples = read()
    elapsed = time.perf_counter() - start

    if matrix.shape != (EXPECTED_ROWS, EXPECTED_SAMPLES):
        raise AssertionError(
            f"expected {(EXPECTED_ROWS, EXPECTED_SAMPLES)}, got {matrix.shape}"
        )
    if matrix.dtype != np.dtype(np.float32) or not matrix.flags.c_contiguous:
        raise AssertionError(
            f"expected a C-contiguous float32 array, got {matrix.dtype}, "
            f"C-contiguous={matrix.flags.c_contiguous}"
        )
    if positions.shape != (EXPECTED_ROWS,):
        raise AssertionError(
            f"expected {EXPECTED_ROWS} positions, got {positions.shape}"
        )
    if len(samples) != EXPECTED_SAMPLES:
        raise AssertionError(f"expected {EXPECTED_SAMPLES} samples, got {len(samples)}")

    # A scan with more than one partition may emit rows out of source order, so
    # hash in position order and record separately whether the emission order
    # actually descended. Hiding the reordering would make a real behavior
    # invisible; sorting before hashing keeps the comparison meaningful.
    order = np.argsort(positions, kind="stable")
    descents = int((np.diff(positions) < 0).sum())

    result = {
        "reader": READER,
        "threads": THREAD_NUM,
        "rows": int(matrix.shape[0]),
        "samples": int(matrix.shape[1]),
        "values": int(matrix.size),
        "time_seconds": round(elapsed, 4),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "value_sha256": _hash_rows_in_order(matrix, order),
        "position_sha256": hashlib.sha256(
            np.ascontiguousarray(positions[order]).tobytes()
        ).hexdigest(),
        "sample_sha256": hashlib.sha256("\n".join(samples).encode()).hexdigest(),
        "emission_order_descents": descents,
        "missing_cells": int(np.isnan(matrix).sum()),
    }
    print("PGEN_RESULT:" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
