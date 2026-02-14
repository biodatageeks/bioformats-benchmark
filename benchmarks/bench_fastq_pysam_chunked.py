"""Benchmark: FASTQ -> pysam -> chunked Arrow -> Polars DataFrame.

Optimised variant of bench_fastq_pysam that builds Arrow RecordBatches
in chunks to reduce peak memory from Python object overhead.
All 4 columns are strings so no NumPy optimisation applies here.
"""

from benchmarks.common import FASTQ_PATH, run_benchmark

import pyarrow as pa
import polars as pl
import pysam

CHUNK_SIZE = 100_000


def benchmark():
    schema = pa.schema([
        pa.field("name", pa.utf8()),
        pa.field("sequence", pa.utf8()),
        pa.field("quality", pa.utf8()),
        pa.field("comment", pa.utf8()),
    ])

    batches = []

    with pysam.FastxFile(FASTQ_PATH) as fq:
        it = iter(fq)
        exhausted = False

        while not exhausted:
            names = []
            sequences = []
            qualities = []
            comments = []

            count = 0
            for entry in it:
                names.append(entry.name)
                sequences.append(entry.sequence)
                qualities.append(entry.quality)
                comments.append(entry.comment)

                count += 1
                if count >= CHUNK_SIZE:
                    break
            else:
                exhausted = True

            if count == 0:
                break

            batch = pa.RecordBatch.from_arrays(
                [
                    pa.array(names, type=pa.utf8()),
                    pa.array(sequences, type=pa.utf8()),
                    pa.array(qualities, type=pa.utf8()),
                    pa.array(comments, type=pa.utf8()),
                ],
                schema=schema,
            )
            batches.append(batch)

    table = pa.concat_tables([pa.Table.from_batches([b]) for b in batches])
    df = pl.from_arrow(table)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, "pysam_chunked_fastq")
