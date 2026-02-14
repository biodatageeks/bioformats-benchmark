"""Benchmark: FASTQ -> pysam -> Polars DataFrame."""

from benchmarks.common import FASTQ_PATH, run_benchmark

import polars as pl
import pysam


def benchmark():
    records = {
        "name": [],
        "sequence": [],
        "quality": [],
        "comment": [],
    }

    with pysam.FastxFile(FASTQ_PATH) as fq:
        for entry in fq:
            records["name"].append(entry.name)
            records["sequence"].append(entry.sequence)
            records["quality"].append(entry.quality)
            records["comment"].append(entry.comment)

    df = pl.DataFrame(records)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, "pysam_fastq")
