"""Benchmark: BAM -> pysam -> Polars DataFrame."""

from benchmarks.common import BAM_PATH, BAM_TAGS, BENCH_VARIANT, run_benchmark

import polars as pl
import pysam


def benchmark():
    include_tags = BENCH_VARIANT == "with_tags"

    records = {
        "query_name": [],
        "flag": [],
        "reference_name": [],
        "reference_start": [],
        "mapping_quality": [],
        "cigar": [],
        "next_reference_name": [],
        "next_reference_start": [],
        "template_length": [],
        "query_sequence": [],
        "query_qualities": [],
    }
    if include_tags:
        for tag in BAM_TAGS:
            records[f"tag_{tag}"] = []

    with pysam.AlignmentFile(BAM_PATH, "rb") as bam:
        for read in bam.fetch(until_eof=True):
            records["query_name"].append(read.query_name)
            records["flag"].append(read.flag)
            records["reference_name"].append(read.reference_name)
            records["reference_start"].append(read.reference_start)
            records["mapping_quality"].append(read.mapping_quality)
            records["cigar"].append(read.cigarstring)
            records["next_reference_name"].append(read.next_reference_name)
            records["next_reference_start"].append(read.next_reference_start)
            records["template_length"].append(read.template_length)
            records["query_sequence"].append(read.query_sequence)
            records["query_qualities"].append(
                read.query_qualities.tolist() if read.query_qualities is not None else None
            )
            if include_tags:
                for tag in BAM_TAGS:
                    try:
                        records[f"tag_{tag}"].append(str(read.get_tag(tag)))
                    except KeyError:
                        records[f"tag_{tag}"].append(None)

    df = pl.DataFrame(records)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_bam_{BENCH_VARIANT}")
