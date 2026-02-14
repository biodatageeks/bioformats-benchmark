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
    chroms = []
    ids = []
    refs = []
    alts = []
    filters = []
    info_lists = {}
    if include_info:
        info_lists = {field: [] for field in info_fields}

    chroms_append = chroms.append
    ids_append = ids.append
    refs_append = refs.append
    alts_append = alts.append
    filters_append = filters.append
    info_targets = []
    if include_info:
        info_targets = [(field, info_lists[field].append) for field in info_fields]

    with pysam.VariantFile(VCF_PATH) as vcf:
        it = iter(vcf)
        exhausted = False

        while not exhausted:
            # NumPy for numeric columns
            np_pos = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_qual = np.empty(CHUNK_SIZE, dtype=np.float64)

            chroms.clear()
            ids.clear()
            refs.clear()
            alts.clear()
            filters.clear()
            if include_info:
                for field in info_fields:
                    info_lists[field].clear()

            count = 0
            for rec in it:
                i = count
                chroms_append(rec.chrom)
                np_pos[i] = rec.pos
                ids_append(rec.id)
                refs_append(rec.ref)
                rec_alts = rec.alts
                if rec_alts:
                    alts_append(",".join(str(a) for a in rec_alts))
                else:
                    alts_append(None)
                rec_qual = rec.qual
                np_qual[i] = rec_qual if rec_qual is not None else np.nan
                rec_filter = rec.filter
                filters_append(",".join(rec_filter.keys()) if rec_filter else None)

                if include_info:
                    rec_info = rec.info
                    for field, append_info in info_targets:
                        val = rec_info.get(field)
                        if isinstance(val, tuple):
                            append_info(",".join(str(v) for v in val))
                        else:
                            append_info(str(val) if val is not None else None)

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

    table = pa.Table.from_batches(batches, schema=schema)
    df = pl.from_arrow(table)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_chunked_vcf_{BENCH_VARIANT}")
