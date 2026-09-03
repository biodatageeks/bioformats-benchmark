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
| `stream_count` — count all pixels | 1.21 s / 784 MB | 0.83 s / 232 MB | 0.34 s | **0.24 s** | 0.24 s |
| `collect_all` — full joined table | 1.94 s / 1467 MB | 0.93 s / 1839 MB | 0.37 s | **0.28 s** | 0.29 s |
| `region` — 20 Mb box, 773,355 rows | 0.11 s / 256 MB | 0.11 s / 276 MB | 0.06 s | **0.04 s** | 0.04 s |

Numbers are median wall time / peak RSS.

## Takeaways

- polars-bio is **faster than or equal to the cooler baseline serially on
  every workload** (1.5x on count, 2.1x on full materialization, parity on
  the region fetch) and **2.7-6.9x faster at 4 partitions**.
- The provider reads pixel data through a **direct-chunk fast path**: chunk
  file addresses are indexed once through libhdf5, then reads are plain file
  I/O + zlib-rs inflation + byte unshuffling in Rust — the libhdf5 global
  lock (which previously capped parallel speedups near 1.4x) is not in the
  data path at all. Every column is validated against a libhdf5 reference
  read at index time and falls back to ordinary hdf5 reads on any mismatch.
- Scaling saturates at 4 partitions on this laptop-class machine (memory
  bandwidth and the final Polars concat), not at 2 as before.
- Streaming count keeps a flat ~230-480 MB footprint vs cooler's 784 MB
  chunked pass; region queries now beat cooler's dedicated `matrix().fetch`
  while composing with arbitrary Polars expressions.
- Benchmarking earlier revisions exposed two polars-bio issues, both fixed
  and included here: typed optimizer literals silently disabling numeric
  predicate pushdown on the scan path, and lock-bound HDF5 decoding.

Reproduce with:

```bash
./setup.sh                                    # downloads + verifies test.mcool
.venv/bin/python run_cool.py --iterations 3   # verify + benchmark + results JSON
```
