"""Verify that polars-bio and cooler produce equivalent cool benchmark outputs.

Precedent: verify_bcf_equivalence.py. Run before trusting benchmark numbers:

    .venv/bin/python -m benchmarks.verify_cool_equivalence

For each workload the same fingerprint (row count, count sum, and coordinate
checksums where applicable) is computed through both implementations and must
match exactly. Exits non-zero on any mismatch.
"""

import sys

import cooler
import polars as pl
import polars_bio as pb

from benchmarks.cool_common import REGION, cooler_uri

URI = cooler_uri()


def _fingerprint(df: pl.DataFrame) -> dict:
    return {
        "rows": df.height,
        "count_sum": int(df["count"].sum()),
        "start1_sum": int(df["start1"].cast(pl.Int64).sum()),
        "start2_sum": int(df["start2"].cast(pl.Int64).sum()),
    }


def main() -> int:
    clr = cooler.Cooler(URI)
    failures = []

    # stream_count
    ours = (
        pb.scan_cool(URI, use_zero_based=True)
        .count()
        .collect(engine="streaming")
        .item(0, 0)
    )
    theirs = clr.info["nnz"]
    if ours != theirs:
        failures.append(f"stream_count: polars_bio={ours} cooler={theirs}")

    # collect_all (fingerprint computed lazily on our side to bound memory;
    # cooler side uses the selector's pandas chunks)
    ours_fp = (
        pb.scan_cool(URI, use_zero_based=True)
        .select(
            rows=pl.len(),
            count_sum=pl.col("count").cast(pl.Int64).sum(),
            start1_sum=pl.col("start1").cast(pl.Int64).sum(),
            start2_sum=pl.col("start2").cast(pl.Int64).sum(),
        )
        .collect(engine="streaming")
        .row(0, named=True)
    )
    selector = clr.pixels(join=True)
    theirs_fp = {"rows": 0, "count_sum": 0, "start1_sum": 0, "start2_sum": 0}
    chunk = 10_000_000
    for lo in range(0, clr.info["nnz"], chunk):
        part = selector[lo : lo + chunk]
        theirs_fp["rows"] += len(part)
        theirs_fp["count_sum"] += int(part["count"].sum())
        theirs_fp["start1_sum"] += int(part["start1"].astype("int64").sum())
        theirs_fp["start2_sum"] += int(part["start2"].astype("int64").sum())
    if dict(ours_fp) != theirs_fp:
        failures.append(f"collect_all: polars_bio={dict(ours_fp)} cooler={theirs_fp}")

    # region
    chrom, start, end = REGION
    ours_region = (
        pb.scan_cool(URI, use_zero_based=True)
        .filter(
            (pl.col("chrom1") == chrom)
            & (pl.col("start1") >= start)
            & (pl.col("end1") <= end)
            & (pl.col("chrom2") == chrom)
            & (pl.col("start2") >= start)
            & (pl.col("end2") <= end)
        )
        .collect()
    )
    theirs_region = pl.from_pandas(
        clr.matrix(balance=False, as_pixels=True, join=True).fetch(REGION)
    )
    ours_r, theirs_r = _fingerprint(ours_region), _fingerprint(theirs_region)
    if ours_r != theirs_r:
        failures.append(f"region: polars_bio={ours_r} cooler={theirs_r}")

    if failures:
        for failure in failures:
            print(f"MISMATCH {failure}", file=sys.stderr)
        return 1
    print(f"COOL_EQUIVALENCE_OK uri={URI} region={REGION}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
