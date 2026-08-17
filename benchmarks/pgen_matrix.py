"""Read one PLINK 2 fileset into a canonical genotype matrix.

Two workloads, because "dosage" means different things in different libraries
and conflating them produces a meaningless comparison:

``dosage``
    ALT dosage as ``float32``. This is PGEN's dosage track, which stores
    ``uint16/16384`` and is genuinely fractional — a real dosage fileset holds
    values like 0.125. polars-bio emits it as ``DS``; pgenlib reads it with
    ``read_dosages_list``. snputils has no native float dosage reader, so its
    int8 hardcalls are cast, and the cast is charged to snputils.

``hardcall``
    ALT allele count as ``int8``: 0, 1, 2, or -9 for missing. This is a
    different track from the dosage one. pgenlib reads it with ``read_list``
    and snputils with ``genotype_mode="dosage"`` — note that snputils' name for
    this workload is "dosage" even though the values are hardcall counts.
    polars-bio has no int8 representation, so its ``DS`` output is cast, and
    the cast is charged to polars-bio.

On a fileset with no dosage track the two workloads produce numerically equal
values, which is why they can be cross-checked; they are still distinct
operations with distinct costs.

Each reader uses its own fastest native API. Earlier revisions of this harness
used non-native paths — a per-variant pgenlib loop and snputils' 3-D allele
reader — which understated both by 5.5x and 27x respectively.
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
MODE = os.environ.get("PGEN_MODE", "dosage").lower()
INPUT_PATH = Path(os.environ["PGEN_PATH"]).expanduser()
EXPECTED_ROWS = int(os.environ["PGEN_EXPECTED_ROWS"])
EXPECTED_SAMPLES = int(os.environ["PGEN_EXPECTED_SAMPLES"])
THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))

# PLINK 2's missing sentinel, used by the int8 workload. The float32 workload
# uses NaN, because -9 is a valid-looking float and NaN is not.
MISSING_I8 = np.int8(-9)


def _companion(suffix: str) -> Path:
    return INPUT_PATH.with_suffix(suffix)


def _prefix() -> str:
    return str(INPUT_PATH)[: -len(".pgen")]


def _positions_from_pvar() -> np.ndarray:
    return np.array(
        [
            int(line.split("\t")[1])
            for line in _companion(".pvar").read_text().splitlines()
            if line and not line.startswith("#")
        ],
        dtype=np.int64,
    )


def _samples_from_psam() -> list[str]:
    lines = [line for line in _companion(".psam").read_text().splitlines() if line]
    iid_column = lines[0].lstrip("#").split("\t").index("IID")
    return [line.split("\t")[iid_column] for line in lines[1:]]


def _read_polars_bio() -> tuple[np.ndarray, np.ndarray, list[str]]:
    import polars_bio as pb

    pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
    # Fewer, larger record batches cut the cost of consolidating the scan's
    # chunked output into one contiguous array: 2.23s -> 1.39s on the whole
    # chromosome. This is a caller-tunable DataFusion option, so polars-bio is
    # measured with it set, the same way every other reader gets its own
    # fastest native path.
    pb.set_option("datafusion.execution.batch_size", "262144")
    # ALT_COUNT is polars-bio's native int8 hardcall column; DS is its float32
    # dosage column. Using each for its own workload keeps this comparable to
    # pgenlib's read_list / read_dosages_list split.
    field = "ALT_COUNT" if MODE == "hardcall" else "DS"
    frame = (
        pb.scan_pgen(
            str(INPUT_PATH),
            genotype_fields=[field],
            use_zero_based=False,
            projection_pushdown=True,
        )
        .select("start", "genotypes")
        .collect()
    )
    positions = frame["start"].to_numpy().astype(np.int64, copy=False)
    table = frame.select("genotypes").to_arrow().column("genotypes").combine_chunks()
    struct = table.chunk(0) if hasattr(table, "chunk") else table
    values = struct.field(field)
    inner = values.flatten()
    flat = inner.to_numpy(zero_copy_only=False)
    if MODE == "hardcall":
        narrowed = np.ascontiguousarray(flat, dtype=np.int8)
        if inner.null_count:
            narrowed[np.asarray(inner.is_null())] = MISSING_I8
        matrix = narrowed.reshape(len(values), -1)
    else:
        matrix = np.ascontiguousarray(flat, dtype=np.float32).reshape(len(values), -1)

    header = pb.get_metadata(pb.scan_pgen(str(INPUT_PATH), genotype_fields=[field]))[
        "header"
    ]
    return matrix, positions, list(header["sample_names"])


def _read_snputils() -> tuple[np.ndarray, np.ndarray, list[str]]:
    import snputils

    obj = snputils.read_pgen(
        _prefix(),
        genotype_mode="dosage",
        fields=["GT"],
        chromosome_ploidy="autosomal",
    )
    calls = np.asarray(obj.genotypes)
    if MODE == "dosage":
        # snputils has no native float dosage reader; the widening is its cost.
        widened = calls.astype(np.float32)
        widened[calls < 0] = np.nan
        matrix = np.ascontiguousarray(widened)
    else:
        matrix = np.ascontiguousarray(calls, dtype=np.int8)
    # fields=["GT"] is snputils' fastest genotype-only path but leaves the
    # variant/sample tables unpopulated, so positions and identifiers come from
    # the companions — the same helper pgenlib uses, so both reference readers
    # are charged identically for them.
    return matrix, _positions_from_pvar(), _samples_from_psam()


def _read_pgenlib() -> tuple[np.ndarray, np.ndarray, list[str]]:
    import pgenlib

    reader = pgenlib.PgenReader(str(INPUT_PATH).encode())
    rows = reader.get_variant_ct()
    cols = reader.get_raw_sample_ct()
    indices = np.arange(rows, dtype=np.uint32)
    if MODE == "dosage":
        matrix = np.empty((rows, cols), dtype=np.float32)
        reader.read_dosages_list(indices, matrix)
    else:
        matrix = np.empty((rows, cols), dtype=np.int8)
        reader.read_list(indices, matrix)
    reader.close()

    # pgenlib reads only the .pgen, so positions and sample identifiers come
    # from the companions. That keeps the oracle independent of polars-bio.
    return matrix, _positions_from_pvar(), _samples_from_psam()


READERS = {
    "polars-bio": _read_polars_bio,
    "snputils": _read_snputils,
    "pgenlib": _read_pgenlib,
}
MODES = ("dosage", "hardcall")
DTYPES = {"dosage": np.float32, "hardcall": np.int8}


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
    if MODE not in MODES:
        raise SystemExit(f"unknown mode {MODE!r}; expected one of {MODES}")

    start = time.perf_counter()
    matrix, positions, samples = read()
    elapsed = time.perf_counter() - start

    expected_dtype = np.dtype(DTYPES[MODE])
    if matrix.shape != (EXPECTED_ROWS, EXPECTED_SAMPLES):
        raise AssertionError(
            f"expected {(EXPECTED_ROWS, EXPECTED_SAMPLES)}, got {matrix.shape}"
        )
    if matrix.dtype != expected_dtype or not matrix.flags.c_contiguous:
        raise AssertionError(
            f"expected a C-contiguous {expected_dtype} array, got {matrix.dtype}, "
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
    # actually descended.
    order = np.argsort(positions, kind="stable")
    descents = int((np.diff(positions) < 0).sum())
    missing = (
        int(np.isnan(matrix).sum()) if MODE == "dosage" else int((matrix < 0).sum())
    )

    result = {
        "reader": READER,
        "mode": MODE,
        "threads": THREAD_NUM,
        "rows": int(matrix.shape[0]),
        "samples": int(matrix.shape[1]),
        "values": int(matrix.size),
        "output_bytes": int(matrix.nbytes),
        "dtype": str(matrix.dtype),
        "time_seconds": round(elapsed, 4),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "value_sha256": _hash_rows_in_order(matrix, order),
        "position_sha256": hashlib.sha256(
            np.ascontiguousarray(positions[order]).tobytes()
        ).hexdigest(),
        "sample_sha256": hashlib.sha256("\n".join(samples).encode()).hexdigest(),
        "emission_order_descents": descents,
        "missing_cells": missing,
    }
    print("PGEN_RESULT:" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
