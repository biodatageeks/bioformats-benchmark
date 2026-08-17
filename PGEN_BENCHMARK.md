# PGEN genotype-reader benchmark

polars-bio reads PLINK 2 filesets through `read_pgen` / `scan_pgen` for a
DataFrame and `read_pgen_matrix` for a dense NumPy matrix, backed by the
`datafusion-bio-format-pgen` provider. This benchmark builds a matrix, so it
measures `read_pgen_matrix`. This compares it against
[snputils](https://github.com/AI-sandbox/snputils) and against
[pgenlib](https://pypi.org/project/Pgenlib/), PLINK 2's own reference reader,
on the same chromosome 22 callset the BCF and BGEN benchmarks use.

**At equal core count polars-bio is second of the three**, a few percent behind
pgenlib and comfortably ahead of snputils. pgenlib and snputils are
single-threaded, so only the one-partition polars-bio rows are like-for-like;
its eight-partition rows spend eight cores the others do not use. What is left
of the gap is one copy, described in
[Where the remaining gap is](#where-the-remaining-gap-is).

Earlier revisions of this document reported polars-bio as the slowest of the
three by a wide margin. Two provider changes and one polars-bio API closed that;
the history is in [Optimization history](#optimization-history), and the two
harness fixes that also moved the figures are in
[Corrections](#corrections-to-earlier-revisions-of-this-document).

## Two workloads, because "dosage" is overloaded

Conflating these produces a meaningless comparison, and an earlier revision of
this document did exactly that.

| Workload | Values | dtype | Source track |
|---|---|---|---|
| **dosage** | ALT dosage, genuinely fractional | `float32` | PGEN's dosage track, stored `uint16/16384` |
| **hardcall** | ALT allele count: 0, 1, 2, −9 missing | `int8` | PGEN's hardcall track |

They are different data. On a fileset that carries a real dosage track, the
same variant reads as:

```
dosages   : [ 0.125  1.0  1.875  missing ]
hardcalls : [ missing  1  missing  missing ]
```

`int8` cannot represent 0.125, which is why polars-bio's `DS` column is
`Float32` and why a narrower type is not simply available.

Naming differs across libraries and is a trap: **snputils'
`genotype_mode="dosage"` returns the hardcall workload**, as int8 counts.
pgenlib separates them properly — `read_list` for hardcalls,
`read_dosages_list` for dosages.

Each reader is measured on its own fastest native API for each workload, and
charged for any conversion it needs to reach the canonical dtype. polars-bio has
a native column for both sides — `DS` for dosages, `ALT_COUNT` for hardcalls as
`int8`, one byte per genotype — and reads them through `read_pgen_matrix`, its
dense-matrix path. snputils has no native float dosage reader, so it is charged
the int8→float32 widening for the dosage workload.

## Result

993,881 variants by 2,548 samples, 2,532,408,788 values. Medians of three
fresh-process runs, all readers interleaved in one session. Lower is better.

### Dosage workload — `float32`, 10.13 GB output

Single-threaded readers first; these are the comparable rows.

| Reader | Threads | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|---:|
| pgenlib `read_dosages_list` | 1 | **1.779 s** | 12,380 MB | 1.00× | 1.79× faster |
| **polars-bio** `read_pgen_matrix` | **1** | **1.849 s** | 13,621 MB | **0.96×** | **1.72× faster** |
| snputils (int8 read + widen) | 1 | 3.181 s | 14,680 MB | 0.56× | 1.00× |
| polars-bio | 8 | 1.522 s | 16,448 MB | 1.17× | 2.09× faster |

At one partition polars-bio is **1.04× pgenlib's time** and 1.72× faster than
snputils. The eight-partition row is included because partition parallelism is
what polars-bio offers and the others do not, but it is not a like-for-like
comparison and should not be read as one.

snputils has no native float dosage reader, so part of its 3.181 s is the
int8→float32 widening this workload charges it; its native int8 decode is the
1.487 s in the hardcall table below.

### Hardcall workload — `int8`, 2.53 GB output

| Reader | Threads | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|---:|
| pgenlib `read_list` | 1 | **0.827 s** | 5,136 MB | 1.00× | 1.80× faster |
| **polars-bio** `read_pgen_matrix` | **1** | **0.940 s** | 5,877 MB | **0.88×** | **1.58× faster** |
| snputils `genotype_mode="dosage"` | 1 | 1.487 s | 5,274 MB | 0.56× | 1.00× |
| polars-bio | 8 | 0.761 s | 6,967 MB | 1.09× | 1.95× faster |

polars-bio emits `ALT_COUNT` natively as `int8`, so this workload no longer
charges it a `float32` materialization and a narrowing pass, as earlier
revisions of this document did.

### Decode only

Stripping materialization from the polars-bio side — its scan measured in Rust
with no Python and no contiguous-array consolidation:

| Field | decode |
|---|---:|
| `DS` (float32) | 1.19 s |
| `ALT_COUNT` (int8) | 0.59 s |

`datafusion/bio-format-pgen/examples/pgen_ds_profile.rs` reproduces this; the
third argument selects the field. There is no comparable pgenlib figure here:
`read_dosages_list` decodes *and* fills the caller's array in one pass, so it
has no separable decode stage to measure against. That difference is the
subject of [Where the remaining gap is](#where-the-remaining-gap-is).

### Optimization history

The provider was profiled and optimized against this benchmark in
[datafusion-bio-formats#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232),
and the materialization path in
[polars-bio#436](https://github.com/biodatageeks/polars-bio/pull/436).
Single-partition whole-chromosome, interleaved in one session:

| Change | dosage scan | dosage total |
|---|---:|---:|
| baseline | 11.2 s | 19.15 s |
| Arrow values/validity buffers instead of per-cell `append_option` | ~9.0 s | 13.95 s |
| `DS` joins the single-field fast path | 7.30 s | 9.40 s |
| table-driven `append_codes` + bulk validity | 5.00 s | 7.49 s |
| skip hardcall phase orientation for dosage | 4.13 s | 6.19 s |
| `ALT_COUNT` column, vectorized expansion, difflist buffer reuse | 2.31 s | 4.34 s |
| fuse the common-value + difflist decode | 1.19 s | 3.23 s |
| `read_pgen_matrix` — stream batches into a preallocated array | 1.19 s | **1.85 s** |

**10.4× end to end**, and 2.35× of that in the last two rows. The hardcall
workload went 2.959 s → 0.940 s over the same two changes.

Two lessons worth recording:

1. **A wasted iteration.** The dense decode path was optimized first, before
   checking which records `plink2 --make-pgen` actually writes — only 3.8% of
   this fixture takes the dense path, and 81% are `record_type=0x14`. Tracing
   which path records take should have come first.
2. **The bottleneck moved and the plan did not.** After the fused decode the
   scan was 1.19 s but the end-to-end total was still 3.23 s: materialization
   had become 63% of the run while the next planned change was another decoder
   optimization. Re-measuring the split, rather than continuing down the list,
   is what produced the last row.

## Where the remaining gap is

Both causes named in earlier revisions of this document have been addressed.

**The two-pass decode is fused.** 81% of this fixture is `record_type=0x14`: one
common genotype for every sample plus a sparse difflist of exceptions. That
record has no per-sample base to reconstruct, so filling a `u8` category per
sample and then reading it back to write the output was one pass more than the
record needs. `DS` and `ALT_COUNT` now fill the Arrow values slice from the
common category and patch the difflist into it directly. Scan: 2.31 s → 1.19 s
for dosage, 1.65 s → 0.59 s for hardcalls.

Note this is the opposite of what a packed-representation optimization would
have done. pgenlib's equivalent is a vectorized `vecset` over `sample_ct/4`
packed bytes followed by `Expand2bitTo8` writing `sample_ct` bytes; a fused fill
writes `sample_ct` and nothing else. For the record type that dominates, packing
would have been a regression.

**The materialization copy is down to one.** Getting a contiguous array through
`read_pgen` consolidated the scan's batches into a second full Arrow buffer
before NumPy ever saw them — a whole extra 10.13 GB. `read_pgen_matrix` streams
batches into a preallocated array instead, so the values are written once.

What is left, at one partition:

| Stage | dosage | hardcall |
|---|---:|---:|
| Planning, PVAR/PSAM parsing, metadata columns | ~0.2 s | ~0.2 s |
| Genotype decode into Arrow batches | 1.19 s | 0.59 s |
| One copy, batches → destination array | ~0.5 s | ~0.2 s |
| **Total** | **1.85 s** | **0.94 s** |
| pgenlib, one pass into a preallocated buffer | 1.78 s | 0.83 s |

**That last copy cannot be removed on this path.** Arrow's `ListArray` uses
32-bit offsets, so one batch holds at most 842,811 rows at 2,548 samples and the
matrix can never arrive as a single zero-copy buffer — at least two batches are
required here, and consolidating them is a copy. Closing it means the decoder
writing into the caller's buffer, the way pgenlib does, which is a new
non-DataFrame API rather than a tuning change. The gap it would close is a few
percent.

**This cost does not exist for streaming or SQL consumers** — it is created by
the benchmark's requirement for one contiguous NumPy array, which pgenlib
satisfies for free by decoding straight into the caller's buffer.

Peak RSS follows from the same architecture: polars-bio briefly holds a batch
and the NumPy output, 13.3 GB against pgenlib's 12.1 GB on the dosage workload.
That is down from 17.8 GB, which was the second Arrow buffer.

## Zero mismatches

Every reader is checked against pgenlib with **no tolerance** — cells that
differ bitwise, not cells that differ by more than an epsilon.

| Comparison | Workload | Cells | Differing |
|---|---|---:|---:|
| polars-bio vs pgenlib | dosage | 2,532,408,788 | **0** |
| snputils vs pgenlib | dosage | 2,532,408,788 | **0** |
| polars-bio vs pgenlib | hardcall | 2,532,408,788 | **0** |
| snputils vs pgenlib | hardcall | 2,532,408,788 | **0** |

polars-bio is bit-identical to pgenlib at every partition count in both
workloads.

**snputils is not an independent check.** Its PGEN reader wraps pgenlib and
calls `read_list` directly, so `snputils vs pgenlib` is close to a tautology
and is reported only for completeness. The load-bearing comparison is
`polars-bio vs pgenlib`, which is a genuinely separate implementation — the
provider decodes the format in Rust and shares no code with PLINK 2.

The same fact explains the timings: snputils is pgenlib plus a NumPy wrapper,
so it is not a third implementation outperforming polars-bio, and its ~0.87 s
overhead over raw pgenlib on the same call is the wrapper.

### The comparison can fail

A zero-difference result is worthless if the comparison cannot report a
difference. `benchmarks/pgen_verify.py` corrupts a single cell of the reader
under test and asserts the corruption is detected;
`selftest_single_cell_detected: 1` is recorded in the result files and the run
aborts if it is ever 0.

### Row order

A scan with more than one partition may emit rows out of source order. On the
whole chromosome the emitted order descends 73–114 times at eight partitions
and never at one. Value and position hashes are taken after sorting by
position, and the raw descent count is recorded per run rather than hidden.

## Timing contract

The timer covers fileset opening, companion discovery and parsing, record
decoding, variant positions and sample identifiers, and final C-contiguous
materialization in the workload's dtype. Imports are excluded — each reader's
module is imported before the clock starts and the cost recorded separately as
`import_seconds`, because it is a one-time process cost paid once however many
filesets are then read, and the magnitudes are not comparable (~0.46 s for
polars-bio's ~228 MB extension against ~0.03 s for pgenlib and snputils).
Thread-pool configuration remains inside the timer; it measures 0.04 ms. Peak
RSS is process `ru_maxrss`; hashing runs outside the timer.
Measurements use a warm filesystem cache and a deterministically rotated,
direction-alternating reader order. `OMP`, `OpenBLAS`, `MKL`, `Accelerate`, and
`NumExpr` pools are capped at one for every reader; `POLARS_MAX_THREADS`,
Rayon, and DataFusion target partitions follow the partition count under test.

pgenlib and snputils read only genotypes natively, so both take variant
positions and sample identifiers from the `.pvar`/`.psam` through the same
helper — they are charged identically for it. polars-bio produces all three
from one scan.

### Build profile is part of the result

polars-bio **must** be built release with `-C target-cpu=native`. A plain
`maturin develop` is a debug build and measured 3.1× slower. The runner records
the loaded extension's path and size in `metadata.polars_bio_build`; release is
~228 MB, debug ~336 MB.

### Corrections to earlier revisions of this document

Recorded because each changed a headline number:

1. An earlier revision claimed polars-bio was **4.214× faster than snputils**.
   That was wrong. It measured snputils through `PGENReader().read()` plus a
   3-D sum (27× its native path) and pgenlib through a per-variant Python loop
   (5.5× its bulk path), and used polars-bio's `GT` rather than `DS` (3.1×).
   Every reader now uses its native API.
2. The dosage and hardcall workloads were conflated, comparing polars-bio's
   float32 dosage column against the others' int8 hardcall counts. They agree
   numerically on this fileset only because it carries no dosage track.
3. The polars-bio adapter materialized a 10 GB intermediate pairs array before
   summing; removing it cut the slice from 1.186 s / 2,308 MB to 0.753 s /
   964 MB with an identical value hash.
4. **The timer charged each reader for importing its own library**, despite the
   contract above having always said imports are excluded. Every measurement
   runs in a fresh process and every adapter imported inside the timed function,
   so the cost was always included: ~0.46 s of polars-bio's figure against
   ~0.03 s of pgenlib's and snputils'. The harness now warms the import for
   every reader alike. **Charged the old way, polars-bio's dosage read is
   2.26 s rather than 1.849 s** — 1.27× pgenlib rather than 1.04×.
5. **polars-bio was measured through its DataFrame path**, which is not its
   fastest native API for a dense matrix — the same class of error as (1), which
   had measured pgenlib through a per-variant loop. `read_pgen` costs a second
   full copy of the values and measures 3.225 s / 22.3 GB on the dosage
   workload; `read_pgen_matrix` is the counterpart to `pgenlib.read_list` and
   measures 1.849 s / 13.3 GB.

## Inputs, builds, and versions

| Item | Value |
|---|---|
| Slice | `chr22.first-25000.pgen`, 2,923,281 bytes |
| Whole chromosome | `chr22.full.pgen`, 79,921,211 bytes (+113,320,253-byte `.pvar`) |
| Whole chromosome SHA-256 | `ca2267eb44335ee1…` |
| Source callset | IGSR/1000 Genomes GRCh38 phased chromosome 22, as used by the BCF and BGEN benchmarks |
| Export | `plink2 --make-pgen`, PLINK v2.0.0-a.7.3 M1 (8 Aug 2026) |
| datafusion-bio-formats | [`1fc3673`](https://github.com/biodatageeks/datafusion-bio-formats/commit/1fc3673), branch `perf/pgen-batch-array-build` ([#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232), not merged) |
| polars-bio branch build | branch `feat/bgen-pr220-bench` ([#436](https://github.com/biodatageeks/polars-bio/pull/436), not merged), pinning the provider commit above |
| snputils / pgenlib | 1.1.1.dev17+gbdb1a56b5 / 0.94.1 |
| polars-bio / Polars / PyArrow / NumPy | 0.33.1 (branch build) / 1.42.1 / 24.0.0 / 2.5.2 |
| Python | 3.12.9 |
| Host | Apple M3 Max, 16 CPU cores, 64 GiB RAM, macOS 15.6 arm64 |
| polars-bio build | release, `RUSTFLAGS="-C target-cpu=native"` |

## Reproduce

The PGEN fixtures come from the chromosome 22 callset the BCF benchmark already
downloads; `setup.sh` exports them with plink2.

Build polars-bio optimized first — not optional, see above:

```bash
cd /path/to/polars-bio
RUSTFLAGS="-C target-cpu=native" maturin develop --release --locked
```

Then:

```bash
POLARS_BIO_BUILD_PROFILE=release POLARS_BIO_RUSTFLAGS="-C target-cpu=native" \
.venv/bin/python run_pgen_benchmarks.py \
  --runs 3 --modes dosage hardcall --polars-bio-partitions 1 8 \
  --pgen /path/to/chr22.full.pgen \
  --expected-rows 993881 --expected-samples 2548 \
  --output results/pgen_reader_benchmark_full_cohort.json
```

Confirm the run measured the artifact you think it did: `metadata.polars_bio_build`
in the result JSON records the declared profile, the rustflags, and the loaded
extension's size.

The whole-chromosome run holds two full matrices in one process during
verification, peaking near 21 GB. Pass `--skip-verification` on a smaller host;
the per-run equivalence hashes still have to agree.
