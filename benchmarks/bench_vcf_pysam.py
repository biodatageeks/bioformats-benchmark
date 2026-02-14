"""Benchmark: VCF -> pysam -> Polars DataFrame."""

from benchmarks.common import VCF_PATH, BENCH_VARIANT, run_benchmark

import polars as pl
import pysam


def benchmark():
    include_info = BENCH_VARIANT == "with_info"

    records = {
        "chrom": [],
        "pos": [],
        "id": [],
        "ref": [],
        "alt": [],
        "qual": [],
        "filter": [],
    }
    info_fields = []

    with pysam.VariantFile(VCF_PATH) as vcf:
        if include_info:
            info_fields = list(vcf.header.info)
            for field in info_fields:
                records[f"info_{field}"] = []

        for rec in vcf:
            records["chrom"].append(rec.chrom)
            records["pos"].append(rec.pos)
            records["id"].append(rec.id)
            records["ref"].append(rec.ref)
            records["alt"].append(
                ",".join(str(a) for a in rec.alts) if rec.alts else None
            )
            records["qual"].append(rec.qual)
            records["filter"].append(
                ",".join(rec.filter.keys()) if rec.filter else None
            )

            if include_info:
                for field in info_fields:
                    try:
                        val = rec.info[field]
                        if isinstance(val, tuple):
                            val = ",".join(str(v) for v in val)
                        records[f"info_{field}"].append(
                            str(val) if val is not None else None
                        )
                    except KeyError:
                        records[f"info_{field}"].append(None)

    df = pl.DataFrame(records)
    return (df.select(pl.len()).item(), df.columns)


run_benchmark(benchmark, f"pysam_vcf_{BENCH_VARIANT}")
