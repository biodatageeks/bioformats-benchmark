# Cooler (.cool/.mcool) benchmark results

polars-bio native cooler scans versus the reference
[cooler](https://github.com/open2c/cooler) package's chunked-pandas approach —
the [abdenlab/oxbow#180](https://github.com/abdenlab/oxbow/issues/180)
baseline that native scanning replaces.

- **Dataset**: open2c HFF Micro-C `test.mcool` (Krietenstein et al. 2021,
  hg38 chr2 + chr17), resolution 10000 → 24,521,334 pixels.
  SHA-256 `a77252c0…` (see `results/cool_results.json`).
- **Machine**: macOS arm64, median of 3 interleaved fresh-process runs.
- **Versions**: polars-bio 0.34.0-dev (cool branch), polars 1.40.1,
  cooler 0.10.4, pandas 3.0.3, numpy 2.4.4.
- **Equivalence**: `benchmarks/verify_cool_equivalence.py` passed before the
  timed runs — identical row counts, count sums, and coordinate checksums for
  every workload across both implementations.

| Workload | cooler (chunked pandas) | polars-bio t1 | t2 | t4 | t8 |
|---|---|---|---|---|---|
| `stream_count` — count all pixels | 1.27 s / 814 MB | 1.96 s / 233 MB | 1.34 s / 231 MB | 1.37 s | 1.51 s |
| `collect_all` — full joined table | 2.06 s / 1537 MB | 2.11 s / 2106 MB | **1.37 s** / 2108 MB | 1.41 s | 1.52 s |
| `region` — 20 Mb box, 773,355 rows | **0.12 s** / 298 MB | 0.20 s / 282 MB | 0.15 s / 281 MB | **0.14 s** | 0.16 s |

Numbers are median wall time / peak RSS.

## Takeaways

- **Streaming count** matches cooler's speed at two partitions while using
  **3.5× less memory** (231 MB vs 814 MB) — the polars-bio path streams
  fixed-size batches and never materializes the table.
- **Full materialization** is 1.5× faster than the chunked-pandas concat at
  two or more partitions. Peak RSS is higher because the whole joined Polars
  frame plus one in-flight batch is held; the cooler figure holds the pandas
  chunks plus the concat.
- **Region queries** ride the cooler CSR indexes on both sides: polars-bio
  first-axis predicate pushdown prunes pixel row ranges via
  `chrom_offset`/`bin1_offset` and lands within ~1.2× of cooler's dedicated
  `matrix().fetch` (0.14 s vs 0.12 s) — while composing with arbitrary Polars
  expressions instead of a region string.
- **Parallel scaling flattens past t2** on this file: libhdf5 serializes raw
  chunk reads behind a global process lock, so extra partitions only
  parallelize decode/join work. This is a known and documented limitation.
- During benchmarking the region workload exposed a polars-bio bug where the
  Polars optimizer's typed literals (`UInt32`) silently disabled numeric
  predicate pushdown on the scan path for all formats — fixed and validated
  (region went from 1.95 s to 0.20 s at t1); the numbers above include the
  fix.

Reproduce with:

```bash
./setup.sh                                    # downloads + verifies test.mcool
.venv/bin/python run_cool.py --iterations 3   # verify + benchmark + results JSON
```
