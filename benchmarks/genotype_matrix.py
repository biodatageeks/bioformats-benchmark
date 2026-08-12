"""Fresh-process VCF/BCF GT-to-dosage benchmark for one reader.

Every supported reader materializes the same row-major ``int8`` NumPy matrix,
1-based variant positions, and input sample order. Imports and reader-specific
configuration happen before the timed region; parsing, decoding, dosage
conversion, and final materialization happen inside it.
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

FORMAT = os.environ["GENOTYPE_FORMAT"].lower()
READER = os.environ["GENOTYPE_READER"].lower()
EXPECTED_ROWS = int(os.environ.get("GENOTYPE_EXPECTED_ROWS", "25000"))
EXPECTED_SAMPLES = int(os.environ.get("GENOTYPE_EXPECTED_SAMPLES", "2548"))
OXBOW_BATCH_SIZE = int(os.environ.get("OXBOW_BATCH_SIZE", "8192"))
INPUT_PATH = Path(
    os.environ["GENOTYPE_VCF_PATH" if FORMAT == "vcf" else "GENOTYPE_BCF_PATH"]
).expanduser()

if FORMAT not in {"vcf", "bcf"}:
    raise ValueError(f"unsupported format: {FORMAT!r}")
if EXPECTED_ROWS < 1 or EXPECTED_SAMPLES < 1:
    raise ValueError(
        "GENOTYPE_EXPECTED_ROWS and GENOTYPE_EXPECTED_SAMPLES must be positive"
    )


def _empty_output() -> tuple[np.ndarray, np.ndarray]:
    return (
        np.empty((EXPECTED_ROWS, EXPECTED_SAMPLES), dtype=np.int8),
        np.empty(EXPECTED_ROWS, dtype=np.int64),
    )


def _missing_or_dosage(gt) -> int:
    if gt is None or any(allele is None for allele in gt):
        return -1
    return sum(allele == 1 for allele in gt)


def _read_pysam() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    matrix, positions = _empty_output()
    source = pysam.VariantFile(str(INPUT_PATH))
    samples = list(source.header.samples)
    row = 0
    for record in source:
        if row >= EXPECTED_ROWS:
            raise AssertionError(f"pysam returned more than {EXPECTED_ROWS} rows")
        positions[row] = record.pos
        matrix[row] = np.fromiter(
            (_missing_or_dosage(call.get("GT")) for call in record.samples.values()),
            dtype=np.int8,
            count=len(samples),
        )
        row += 1
    source.close()
    return matrix[:row], positions[:row], samples, "eager-preallocated"


def _read_pyvcf3() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    matrix, positions = _empty_output()
    source = vcf.Reader(filename=str(INPUT_PATH))
    samples = list(source.samples)
    row = 0
    for record in source:
        if row >= EXPECTED_ROWS:
            raise AssertionError(f"PyVCF3 returned more than {EXPECTED_ROWS} rows")
        positions[row] = record.POS
        matrix[row] = np.fromiter(
            (
                -1
                if call.data.GT is None or "." in call.data.GT
                else call.data.GT.count("1")
                for call in record.samples
            ),
            dtype=np.int8,
            count=len(samples),
        )
        row += 1
    return matrix[:row], positions[:row], samples, "eager-preallocated"


def _read_cyvcf2() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    matrix, positions = _empty_output()
    source = cyvcf2.VCF(str(INPUT_PATH))
    samples = list(source.samples)
    row = 0
    for record in source:
        if row >= EXPECTED_ROWS:
            raise AssertionError(f"cyvcf2 returned more than {EXPECTED_ROWS} rows")
        positions[row] = record.POS
        alleles = record.genotype.array()[:, :2]
        dosage = np.count_nonzero(alleles == 1, axis=1).astype(np.int8, copy=False)
        dosage[np.any(alleles < 0, axis=1)] = -1
        matrix[row] = dosage
        row += 1
    source.close()
    return matrix[:row], positions[:row], samples, "eager-preallocated"


def _read_snputils() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    if FORMAT == "bcf":
        result = snputils.read_bcf(
            INPUT_PATH,
            fields=["GT", "POS", "IID"],
            genotype_mode="dosage",
            chromosome_ploidy="autosomal",
        )
    else:
        result = snputils.read_vcf(
            INPUT_PATH,
            fields=["POS"],
            genotype_mode="dosage",
            chromosome_ploidy="autosomal",
        )
    return (
        np.ascontiguousarray(result.genotypes, dtype=np.int8),
        np.asarray(result.variants_pos, dtype=np.int64),
        [str(sample) for sample in result.samples],
        "eager-native",
    )


def _read_polars_bio() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    scan_options = {
        "info_fields": [],
        "format_fields": ["GT"],
        "use_zero_based": False,
        "projection_pushdown": True,
    }
    if FORMAT == "bcf":
        scan_options["genotype_output"] = "dosage"

    scan = pb.scan_vcf(str(INPUT_PATH), **scan_options)
    samples = [
        str(sample) for sample in pb.get_metadata(scan)["header"]["sample_names"]
    ]
    if FORMAT == "bcf":
        dosage = (
            pl.col("genotypes")
            .struct.field("GT")
            .list.eval(pl.element().fill_null(-1))
            .alias("dosage")
        )
    else:
        genotype = pl.element()
        dosage = (
            pl.col("genotypes")
            .struct.field("GT")
            .list.eval(
                pl.when(genotype.str.contains(".", literal=True))
                .then(pl.lit(-1, dtype=pl.Int8))
                .otherwise(genotype.str.count_matches("1").cast(pl.Int8))
            )
            .alias("dosage")
        )

    frame = scan.select("start", dosage).collect(engine="streaming")
    dosage_array = frame["dosage"].rechunk().to_arrow()
    values = dosage_array.values.to_numpy(zero_copy_only=False)
    matrix = np.ascontiguousarray(values, dtype=np.int8).reshape(
        frame.height, len(samples)
    )
    positions = np.asarray(frame["start"].to_numpy(), dtype=np.int64)
    return matrix, positions, samples, "lazy-streaming"


def _read_oxbow() -> tuple[np.ndarray, np.ndarray, list[str], str]:
    factory = ox.from_vcf if FORMAT == "vcf" else ox.from_bcf
    source = factory(
        str(INPUT_PATH),
        fields=["pos"],
        info_fields=[],
        genotype_fields=["GT"],
        genotype_by="field",
        samples="*",
        samples_nested=False,
        batch_size=OXBOW_BATCH_SIZE,
    )
    gt_type = source.schema.field("GT").type
    samples = [field.name for field in gt_type.fields]
    matrix, positions = _empty_output()
    offset = 0

    for native_batch in source.batches():
        batch = pa.record_batch(native_batch)
        row_count = batch.num_rows
        end = offset + row_count
        if end > EXPECTED_ROWS:
            raise AssertionError(f"Oxbow returned more than {EXPECTED_ROWS} rows")
        positions[offset:end] = np.asarray(
            batch.column(batch.schema.get_field_index("pos")).to_numpy(
                zero_copy_only=False
            ),
            dtype=np.int64,
        )
        gt = batch.column(batch.schema.get_field_index("GT"))
        transposed = np.empty((len(samples), row_count), dtype=np.int8)
        for sample_index in range(len(samples)):
            allele_lists = gt.field(sample_index).field("allele")
            lengths = pc.list_value_length(allele_lists)
            if lengths.null_count or not np.all(
                lengths.to_numpy(zero_copy_only=False) == 2
            ):
                raise AssertionError("benchmark fixture must contain diploid GT calls")
            allele_values = pc.fill_null(allele_lists.values, -1).to_numpy(
                zero_copy_only=False
            )
            alleles = np.asarray(allele_values, dtype=np.int32).reshape(row_count, 2)
            sample_dosage = np.count_nonzero(alleles == 1, axis=1).astype(
                np.int8, copy=False
            )
            sample_dosage[np.any(alleles < 0, axis=1)] = -1
            transposed[sample_index] = sample_dosage
        matrix[offset:end] = transposed.T
        offset = end

    return matrix[:offset], positions[:offset], samples, "streaming-arrow-batches"


if READER == "pysam":
    import pysam

    read = _read_pysam
elif READER == "pyvcf3":
    if FORMAT == "bcf":
        raise ValueError("PyVCF3 does not support BCF input")
    import vcf

    read = _read_pyvcf3
elif READER == "cyvcf2":
    import cyvcf2

    read = _read_cyvcf2
elif READER == "snputils":
    import snputils

    read = _read_snputils
elif READER == "polars-bio":
    import polars as pl
    import polars_bio as pb

    pb.set_option("datafusion.execution.target_partitions", "1")
    read = _read_polars_bio
elif READER == "oxbow":
    import oxbow as ox
    import pyarrow as pa
    import pyarrow.compute as pc

    read = _read_oxbow
else:
    raise ValueError(f"unsupported reader: {READER!r}")


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> None:
    start = time.perf_counter()
    matrix, positions, samples, mode = read()
    elapsed = time.perf_counter() - start

    if matrix.shape != (EXPECTED_ROWS, EXPECTED_SAMPLES):
        raise AssertionError(
            f"expected matrix {(EXPECTED_ROWS, EXPECTED_SAMPLES)}, got {matrix.shape}"
        )
    if matrix.dtype != np.dtype(np.int8) or not matrix.flags.c_contiguous:
        raise AssertionError(
            f"expected a row-major int8 matrix, got {matrix.dtype}, "
            f"C-contiguous={matrix.flags.c_contiguous}"
        )
    if positions.shape != (EXPECTED_ROWS,):
        raise AssertionError(
            f"expected {EXPECTED_ROWS} positions, got {positions.shape}"
        )
    if len(samples) != EXPECTED_SAMPLES:
        raise AssertionError(f"expected {EXPECTED_SAMPLES} samples, got {len(samples)}")
    if int(matrix.min()) < -1 or int(matrix.max()) > 2:
        raise AssertionError("dosage matrix contains values outside {-1, 0, 1, 2}")

    canonical_positions = np.ascontiguousarray(positions, dtype="<i8")
    result = {
        "reader": READER,
        "format": FORMAT.upper(),
        "mode": mode,
        "path": str(INPUT_PATH.resolve()),
        "time_seconds": round(elapsed, 3),
        "peak_rss_mb": round(_peak_rss_mb(), 1),
        "rows": matrix.shape[0],
        "samples": matrix.shape[1],
        "dosage_values": matrix.size,
        "dtype": str(matrix.dtype),
        "position_sha256": hashlib.sha256(canonical_positions).hexdigest(),
        "sample_sha256": hashlib.sha256("\0".join(samples).encode()).hexdigest(),
        "dosage_sha256": hashlib.sha256(matrix).hexdigest(),
    }
    print(f"GENOTYPE_RESULT:{json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
