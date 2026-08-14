"""Fresh-process BGEN genotype-matrix benchmark for one reader.

Every reader materializes the same canonical ``float32`` NumPy array from the
same BGEN file, plus 1-based variant positions and the input sample order.
Imports and thread-pool configuration happen before the timed region; source
opening, header/index discovery, block decompression, probability decoding, and
final materialization happen inside it.

Two workloads are supported:

``dosage``
    ``(variants, samples)`` expected copies of the second encoded allele.

``probabilities``
    ``(variants, samples, width)`` complete genotype probabilities, the layout
    snputils publishes as its headline BGEN result.
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

READER = os.environ["BGEN_READER"].lower()
MODE = os.environ.get("BGEN_MODE", "dosage").lower()
INPUT_PATH = Path(os.environ["BGEN_PATH"]).expanduser()
EXPECTED_ROWS = int(os.environ["BGEN_EXPECTED_ROWS"])
EXPECTED_SAMPLES = int(os.environ["BGEN_EXPECTED_SAMPLES"])
THREAD_NUM = int(os.environ.get("THREAD_NUM", "1"))

if MODE not in {"dosage", "probabilities"}:
    raise ValueError(f"unsupported mode: {MODE!r}")
if EXPECTED_ROWS < 1 or EXPECTED_SAMPLES < 1:
    raise ValueError("BGEN_EXPECTED_ROWS and BGEN_EXPECTED_SAMPLES must be positive")


def _sample_path() -> str:
    candidate = INPUT_PATH.with_suffix(".sample")
    return str(candidate) if candidate.exists() else ""


# --------------------------------------------------------------------------
# polars-bio
# --------------------------------------------------------------------------


def _read_polars_bio() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    genotype_output = "dosage" if MODE == "dosage" else "probability"
    scan = pb.scan_bgen(
        str(INPUT_PATH),
        genotype_output=genotype_output,
        use_zero_based=False,
        projection_pushdown=True,
    )
    frame = scan.select("start", "genotypes").collect()
    positions = frame["start"].to_numpy().astype(np.int64, copy=False)
    table = frame.select("genotypes").to_arrow().column("genotypes").combine_chunks()
    struct = table.chunk(0) if hasattr(table, "chunk") else table

    if MODE == "dosage":
        values = struct.field("DS")
        flat = values.flatten().to_numpy(zero_copy_only=False)
        matrix = np.ascontiguousarray(flat, dtype=np.float32).reshape(len(values), -1)
    else:
        # GP is list<sample: list<state: float32>>. plink2 can leave a few
        # variants unphased inside an otherwise phased file, so probability
        # widths vary by row. Pad each sample to the widest state vector with
        # NaN, which is the layout snputils also produces for mixed widths.
        per_variant = struct.field("GP")
        per_sample = per_variant.values
        offsets = np.asarray(per_sample.offsets, dtype=np.int64)
        widths = np.diff(offsets)
        leaf = np.asarray(
            per_sample.values.to_numpy(zero_copy_only=False), dtype=np.float32
        )[: int(offsets[-1])]
        width = int(widths.max())
        matrix = np.full(
            (len(per_variant) * EXPECTED_SAMPLES, width), np.nan, dtype=np.float32
        )
        if np.all(widths == width):
            matrix[:] = leaf.reshape(-1, width)
        else:
            # `leaf` concatenates each sample's states in row-major order, so the
            # mask of slots a sample actually stores selects exactly those values
            # in the same order.
            matrix[np.arange(width)[None, :] < widths[:, None]] = leaf
        matrix = np.ascontiguousarray(
            matrix.reshape(len(per_variant), EXPECTED_SAMPLES, width)
        )
    samples = [str(name) for name in pb.get_metadata(scan)["header"]["sample_names"]]
    return matrix, positions, samples, f"lazy-streaming-t{THREAD_NUM}"


# --------------------------------------------------------------------------
# snputils
# --------------------------------------------------------------------------


def _read_snputils() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    reader = BGENReader(str(INPUT_PATH))
    if MODE == "dosage":
        matrix = reader.read_dosage()
    else:
        matrix = reader.read(fields=["GP"]).calldata_gp.astype(np.float32, copy=False)
    snpobj = reader.read(fields=["POS", "IID"])
    positions = np.asarray(snpobj.variants_pos, dtype=np.int64)
    samples = [str(sample) for sample in snpobj.samples]
    return np.ascontiguousarray(matrix), positions, samples, "eager-native"


# --------------------------------------------------------------------------
# bgen (the reference oracle)
# --------------------------------------------------------------------------


def _read_bgen_package() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    with BgenReader(str(INPUT_PATH), _sample_path(), delay_parsing=True) as handle:
        samples = [str(sample) for sample in handle.samples]
        positions = np.empty(len(handle), dtype=np.int64)
        matrix = None
        for index, variant in enumerate(handle):
            probabilities = np.asarray(variant.probabilities, dtype=np.float32)
            positions[index] = variant.pos
            if matrix is None:
                matrix = _allocate(len(handle), probabilities.shape)
            if MODE == "dosage":
                matrix[index] = _probabilities_to_dosage(probabilities)
            else:
                width = probabilities.shape[1]
                if width > matrix.shape[2]:
                    # A later variant stores more states than the first one, so
                    # widen the output and NaN-pad the rows already written.
                    widened = np.full(
                        (matrix.shape[0], matrix.shape[1], width),
                        np.nan,
                        dtype=np.float32,
                    )
                    widened[:, :, : matrix.shape[2]] = matrix
                    matrix = widened
                matrix[index].fill(np.nan)
                matrix[index, :, :width] = probabilities
    return matrix, positions, samples, "eager-record-iterator"


def _allocate(rows: int, shape: tuple[int, ...]) -> np.ndarray:
    if MODE == "dosage":
        return np.empty((rows, shape[0]), dtype=np.float32)
    return np.empty((rows, shape[0], shape[1]), dtype=np.float32)


def _probabilities_to_dosage(probabilities: np.ndarray) -> np.ndarray:
    width = probabilities.shape[1]
    if width == 3:  # unphased biallelic allele-count states
        return probabilities @ np.array([0.0, 1.0, 2.0], dtype=np.float32)
    if width == 4:  # phased biallelic, two haplotypes by two alleles
        return probabilities[:, 1] + probabilities[:, 3]
    raise AssertionError(f"unexpected biallelic probability width {width}")


# --------------------------------------------------------------------------
# pysnptools
# --------------------------------------------------------------------------


def _read_pysnptools() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    source = Bgen(str(INPUT_PATH))
    probabilities = source.read(order="C", dtype=np.float32).val.transpose(1, 0, 2)
    positions = np.asarray(source.pos[:, 2], dtype=np.int64)
    samples = [str(sample[1]) for sample in source.iid]
    if MODE == "dosage":
        matrix = _probabilities_to_dosage_3d(probabilities)
    else:
        matrix = probabilities
    return np.ascontiguousarray(matrix, dtype=np.float32), positions, samples, "eager"


def _probabilities_to_dosage_3d(probabilities: np.ndarray) -> np.ndarray:
    width = probabilities.shape[-1]
    if width == 3:
        return probabilities @ np.array([0.0, 1.0, 2.0], dtype=np.float32)
    if width == 4:
        return probabilities[:, :, 1] + probabilities[:, :, 3]
    raise AssertionError(f"unexpected biallelic probability width {width}")


def make_reader(name: str):
    """Import one reader's dependencies and return its adapter."""
    if name == "polars-bio":
        global pb
        import polars_bio as pb

        pb.set_option("datafusion.execution.target_partitions", str(THREAD_NUM))
        return _read_polars_bio
    if name == "snputils":
        global BGENReader
        from snputils.snp.io.read.bgen import BGENReader

        return _read_snputils
    if name == "bgen":
        global BgenReader
        from bgen import BgenReader

        return _read_bgen_package
    if name == "pysnptools":
        global Bgen
        from pysnptools.distreader import Bgen

        return _read_pysnptools
    raise ValueError(f"unsupported reader: {name!r}")


read = make_reader(READER)


def _hash_rows_in_order(matrix: np.ndarray, order: np.ndarray) -> str:
    """Hash the rows in `order` without materializing a reordered copy.

    A whole-chromosome array is tens of gigabytes, so copying it once more to
    sort it would dominate peak memory and distort the measurement.
    """
    digest = hashlib.sha256()
    if np.array_equal(order, np.arange(len(order))):
        digest.update(np.ascontiguousarray(matrix))
        return digest.hexdigest()
    rows_per_chunk = max(1, (16 * 1024 * 1024) // max(1, matrix[0].nbytes))
    for start in range(0, len(order), rows_per_chunk):
        digest.update(np.ascontiguousarray(matrix[order[start : start + rows_per_chunk]]))
    return digest.hexdigest()


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> None:
    start = time.perf_counter()
    matrix, positions, samples, mode = read()
    elapsed = time.perf_counter() - start

    expected_rank = 2 if MODE == "dosage" else 3
    if matrix.ndim != expected_rank:
        raise AssertionError(f"expected a rank-{expected_rank} array, got {matrix.ndim}")
    if matrix.shape[0] != EXPECTED_ROWS or matrix.shape[1] != EXPECTED_SAMPLES:
        raise AssertionError(
            f"expected {(EXPECTED_ROWS, EXPECTED_SAMPLES)} leading dimensions, "
            f"got {matrix.shape[:2]}"
        )
    if matrix.dtype != np.dtype(np.float32) or not matrix.flags.c_contiguous:
        raise AssertionError(
            f"expected a C-contiguous float32 array, got {matrix.dtype}, "
            f"C-contiguous={matrix.flags.c_contiguous}"
        )
    if positions.shape != (EXPECTED_ROWS,):
        raise AssertionError(f"expected {EXPECTED_ROWS} positions, got {positions.shape}")
    if len(samples) != EXPECTED_SAMPLES:
        raise AssertionError(f"expected {EXPECTED_SAMPLES} samples, got {len(samples)}")

    finite = matrix[np.isfinite(matrix)]
    if finite.size:
        upper = 2.0 if MODE == "dosage" else 1.0
        if float(finite.min()) < -1e-6 or float(finite.max()) > upper + 1e-6:
            raise AssertionError(
                f"{MODE} values outside [0, {upper}]: "
                f"[{float(finite.min())}, {float(finite.max())}]"
            )

    # DataFusion coalesces partitions as their batches become ready, so a
    # multi-partition scan may emit rows out of source order. Hashing the
    # position-sorted array compares the content every reader produced,
    # independently of emission order; the emission-order hash is reported too
    # so any reordering stays visible.
    canonical_positions = np.ascontiguousarray(positions, dtype="<i8")
    order = np.argsort(canonical_positions, kind="stable")
    sorted_positions = np.ascontiguousarray(canonical_positions[order])
    descents = int(np.count_nonzero(np.diff(canonical_positions) < 0))
    value_digest = _hash_rows_in_order(matrix, order)
    result = {
        "reader": READER,
        "workload": MODE,
        "mode": mode,
        "path": str(INPUT_PATH.resolve()),
        "threads": THREAD_NUM,
        "time_seconds": round(elapsed, 3),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "rows": int(matrix.shape[0]),
        "samples": int(matrix.shape[1]),
        "width": int(matrix.shape[2]) if MODE == "probabilities" else 1,
        "values": int(matrix.size),
        "dtype": str(matrix.dtype),
        "position_sha256": hashlib.sha256(sorted_positions).hexdigest(),
        "sample_sha256": hashlib.sha256("\0".join(samples).encode()).hexdigest(),
        "value_sha256": value_digest,
        "emission_order_value_sha256": hashlib.sha256(
            np.ascontiguousarray(matrix)
        ).hexdigest(),
        "emission_order_descents": descents,
    }
    print(f"BGEN_RESULT:{json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
