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
    query_names = []
    ref_names = []
    cigars = []
    next_ref_names = []
    sequences = []
    qualities = []
    tag_lists = {}
    if include_tags:
        tag_lists = {tag: [] for tag in BAM_TAGS}

    query_names_append = query_names.append
    ref_names_append = ref_names.append
    cigars_append = cigars.append
    next_ref_names_append = next_ref_names.append
    sequences_append = sequences.append
    qualities_append = qualities.append
    tag_targets = []
    if include_tags:
        tag_targets = [(tag, tag_lists[tag].append) for tag in BAM_TAGS]

    with pysam.AlignmentFile(BAM_PATH, "rb") as bam:
        it = bam.fetch(until_eof=True)
        exhausted = False

        while not exhausted:
            np_flag = np.empty(CHUNK_SIZE, dtype=np.int32)
            np_ref_start = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_mapq = np.empty(CHUNK_SIZE, dtype=np.int32)
            np_next_ref_start = np.empty(CHUNK_SIZE, dtype=np.int64)
            np_tlen = np.empty(CHUNK_SIZE, dtype=np.int64)

            query_names.clear()
            ref_names.clear()
            cigars.clear()
            next_ref_names.clear()
            sequences.clear()
            qualities.clear()
            if include_tags:
                for tag in BAM_TAGS:
                    tag_lists[tag].clear()

            count = 0
            for read in it:
                i = count
                query_names_append(read.query_name)
                np_flag[i] = read.flag
                ref_names_append(read.reference_name)
                np_ref_start[i] = read.reference_start
                np_mapq[i] = read.mapping_quality
                cigars_append(read.cigarstring)
                next_ref_names_append(read.next_reference_name)
                np_next_ref_start[i] = read.next_reference_start
                np_tlen[i] = read.template_length
                sequences_append(read.query_sequence)
                quals = read.query_qualities
                if quals is None:
                    qualities_append(None)
                else:
                    qualities_append(quals.tolist())

                if include_tags:
                    read_tags = dict(read.tags)
                    for tag, append_tag in tag_targets:
                        value = read_tags.get(tag)
                        append_tag(str(value) if value is not None else None)

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

    table = pa.Table.from_batches(batches, schema=schema)
    df = pl.from_arrow(table)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_chunked_bam_{BENCH_VARIANT}")
