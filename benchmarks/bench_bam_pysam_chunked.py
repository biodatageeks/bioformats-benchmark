"""Benchmark: BAM -> pysam -> chunked Arrow -> Polars DataFrame.

Optimised variant of bench_bam_pysam that uses:
  - NumPy pre-allocated arrays for numeric columns (zero-copy to Arrow)
  - Python lists only for variable-length string/bytes columns
  - Chunked processing (CHUNK_SIZE records per Arrow RecordBatch)
"""

from benchmarks.common import BAM_PATH, BAM_TAGS, BENCH_VARIANT, run_benchmark

import numpy as np
import pyarrow as pa
import polars as pl
import pysam

CHUNK_SIZE = 100_000


def benchmark():
    include_tags = BENCH_VARIANT == "with_tags"

    # Build Arrow schema
    fields = [
        pa.field("query_name", pa.utf8()),
        pa.field("flag", pa.int32()),
        pa.field("reference_name", pa.utf8()),
        pa.field("reference_start", pa.int64()),
        pa.field("mapping_quality", pa.int32()),
        pa.field("cigar", pa.utf8()),
        pa.field("next_reference_name", pa.utf8()),
        pa.field("next_reference_start", pa.int64()),
        pa.field("template_length", pa.int64()),
        pa.field("query_sequence", pa.utf8()),
        pa.field("query_qualities", pa.list_(pa.uint8())),
    ]
    if include_tags:
        for tag in BAM_TAGS:
            fields.append(pa.field(f"tag_{tag}", pa.utf8()))
    schema = pa.schema(fields)

    batches = []

    with pysam.AlignmentFile(BAM_PATH, "rb") as bam:
        it = bam.fetch(until_eof=True)
        exhausted = False

        while not exhausted:
            # --- pre-allocate numpy arrays for numeric columns ---
            np_flag = np.empty(CHUNK_SIZE, dtype=np.int32)
            np_ref_start = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_mapq = np.empty(CHUNK_SIZE, dtype=np.int32)
            np_next_ref_start = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_tlen = np.empty(CHUNK_SIZE, dtype=np.int64)

            # --- python lists for variable-length columns ---
            query_names = []
            ref_names = []
            cigars = []
            next_ref_names = []
            sequences = []
            qualities = []
            tag_lists = {}
            if include_tags:
                for tag in BAM_TAGS:
                    tag_lists[tag] = []

            count = 0
            for read in it:
                i = count
                query_names.append(read.query_name)
                np_flag[i] = read.flag
                ref_names.append(read.reference_name)
                np_ref_start[i] = read.reference_start
                np_mapq[i] = read.mapping_quality
                cigars.append(read.cigarstring)
                next_ref_names.append(read.next_reference_name)
                np_next_ref_start[i] = read.next_reference_start
                np_tlen[i] = read.template_length
                sequences.append(read.query_sequence)
                quals = read.query_qualities
                qualities.append(quals.tolist() if quals is not None else None)

                if include_tags:
                    for tag in BAM_TAGS:
                        try:
                            tag_lists[tag].append(str(read.get_tag(tag)))
                        except KeyError:
                            tag_lists[tag].append(None)

                count += 1
                if count >= CHUNK_SIZE:
                    break
            else:
                # iterator exhausted before reaching CHUNK_SIZE
                exhausted = True

            if count == 0:
                break

            # Trim numpy arrays to actual count
            arrays = [
                pa.array(query_names, type=pa.utf8()),
                pa.array(np_flag[:count], type=pa.int32()),
                pa.array(ref_names, type=pa.utf8()),
                pa.array(np_ref_start[:count], type=pa.int64()),
                pa.array(np_mapq[:count], type=pa.int32()),
                pa.array(cigars, type=pa.utf8()),
                pa.array(next_ref_names, type=pa.utf8()),
                pa.array(np_next_ref_start[:count], type=pa.int64()),
                pa.array(np_tlen[:count], type=pa.int64()),
                pa.array(sequences, type=pa.utf8()),
                pa.array(qualities, type=pa.list_(pa.uint8())),
            ]
            if include_tags:
                for tag in BAM_TAGS:
                    arrays.append(pa.array(tag_lists[tag], type=pa.utf8()))

            batch = pa.RecordBatch.from_arrays(arrays, schema=schema)
            batches.append(batch)

    table = pa.concat_tables([pa.Table.from_batches([b]) for b in batches])
    df = pl.from_arrow(table)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_chunked_bam_{BENCH_VARIANT}")
