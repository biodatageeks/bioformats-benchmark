"""Benchmark: VCF -> pysam -> chunked Arrow -> Polars DataFrame.

Optimised variant of bench_vcf_pysam that uses:
  - NumPy pre-allocated arrays for numeric columns (pos, qual)
  - Chunked processing (CHUNK_SIZE records per Arrow RecordBatch)
"""

from benchmarks.common import VCF_PATH, BENCH_VARIANT, run_benchmark

import numpy as np
import pyarrow as pa
import polars as pl
import pysam

CHUNK_SIZE = 100_000


def benchmark():
    include_info = BENCH_VARIANT == "with_info"

    # Open VCF to discover info fields for schema
    with pysam.VariantFile(VCF_PATH) as vcf:
        info_fields = list(vcf.header.info) if include_info else []

    # Build Arrow schema
    fields = [
        pa.field("chrom", pa.utf8()),
        pa.field("pos", pa.int64()),
        pa.field("id", pa.utf8()),
        pa.field("ref", pa.utf8()),
        pa.field("alt", pa.utf8()),
        pa.field("qual", pa.float64()),
        pa.field("filter", pa.utf8()),
    ]
    for field in info_fields:
        fields.append(pa.field(f"info_{field}", pa.utf8()))
    schema = pa.schema(fields)

    batches = []

    with pysam.VariantFile(VCF_PATH) as vcf:
        it = iter(vcf)
        exhausted = False

        while not exhausted:
            # NumPy for numeric columns
            np_pos = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_qual = np.empty(CHUNK_SIZE, dtype=np.float64)

            # Python lists for string columns
            chroms = []
            ids = []
            refs = []
            alts = []
            filters = []
            info_lists = {}
            for field in info_fields:
                info_lists[field] = []

            count = 0
            for rec in it:
                i = count
                chroms.append(rec.chrom)
                np_pos[i] = rec.pos
                ids.append(rec.id)
                refs.append(rec.ref)
                alts.append(
                    ",".join(str(a) for a in rec.alts) if rec.alts else None
                )
                np_qual[i] = rec.qual if rec.qual is not None else float("nan")
                filters.append(
                    ",".join(rec.filter.keys()) if rec.filter else None
                )

                if include_info:
                    for field in info_fields:
                        try:
                            val = rec.info[field]
                            if isinstance(val, tuple):
                                val = ",".join(str(v) for v in val)
                            info_lists[field].append(
                                str(val) if val is not None else None
                            )
                        except KeyError:
                            info_lists[field].append(None)

                count += 1
                if count >= CHUNK_SIZE:
                    break
            else:
                exhausted = True

            if count == 0:
                break

            arrays = [
                pa.array(chroms, type=pa.utf8()),
                pa.array(np_pos[:count], type=pa.int64()),
                pa.array(ids, type=pa.utf8()),
                pa.array(refs, type=pa.utf8()),
                pa.array(alts, type=pa.utf8()),
                pa.array(np_qual[:count], type=pa.float64()),
                pa.array(filters, type=pa.utf8()),
            ]
            for field in info_fields:
                arrays.append(pa.array(info_lists[field], type=pa.utf8()))

            batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
            batches.append(batch)

    table = pa.concat_tables([pa.Table.from_batches([b]) for b in batches])
    df = pl.from_arrow(table)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_chunked_vcf_{BENCH_VARIANT}")
