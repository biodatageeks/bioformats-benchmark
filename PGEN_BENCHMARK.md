# PGEN genotype-reader benchmark

polars-bio reads PLINK 2 filesets through `read_pgen` / `scan_pgen`, backed by
the `datafusion-bio-format-pgen` provider. This compares it against
[snputils](https://github.com/AI-sandbox/snputils) and against
[pgenlib](https://pypi.org/project/Pgenlib/), PLINK 2's own reference reader,
on the same chromosome 22 callset the BCF and BGEN benchmarks use.

**At equal core count polars-bio is the slowest of the three.** pgenlib and
snputils are single-threaded, so only the one-partition polars-bio rows are
like-for-like; its eight-partition rows spend eight cores to close a gap the
others do not have. The remaining gap is understood and mechanical; the causes
are in [Why polars-bio is slower](#why-polars-bio-is-slower).

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
charged for any conversion it needs to reach the canonical dtype: snputils
widens int8→float32 for the dosage workload, polars-bio narrows float32→int8
for the hardcall workload, since neither has a native path for that side.

## Result

993,881 variants by 2,548 samples, 2,532,408,788 values. Medians of two
fresh-process runs. Lower is better.

### Dosage workload — `float32`, 10.13 GB output

Single-threaded readers first; these are the comparable rows.

| Reader | Threads | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|---:|
| pgenlib `read_dosages_list` | 1 | **1.853 s** | 12,382 MB | 1.00× | 1.86× faster |
| snputils (int8 read + widen) | 1 | 3.446 s | 14,681 MB | 0.54× | 1.00× |
| **polars-bio** | **1** | **6.190 s** | 17,767 MB | **0.30×** | **0.54×** |
| polars-bio | 8 | 2.934 s | 18,299 MB | 0.63× | 1.17× |

At one partition polars-bio is 3.3× slower than pgenlib and 1.8× slower than
snputils. The eight-partition row is included because partition parallelism is
what polars-bio offers and the others do not, but it is not a like-for-like
comparison and should not be read as one.

Note that snputils has no native float dosage reader, so 1.75 s of its 3.446 s
is the int8→float32 widening this workload charges it; its native int8 decode
is the 1.696 s in the hardcall table below.

### Hardcall workload — `int8`, 2.53 GB output

| Reader | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|
| pgenlib `read_list` | **0.827 s** | 5,136 MB | 1.00× | 2.05× faster |
| snputils `genotype_mode="dosage"` | 1.696 s | 5,281 MB | 0.49× | 1.00× |
| polars-bio, 8 partitions | 6.546 s | 18,421 MB | **0.13×** | 0.26× |
| polars-bio, 1 partition | 9.414 s | 17,914 MB | 0.09× | 0.18× |

polars-bio has no int8 representation, so this workload charges it a full
`float32` materialization plus a narrowing pass. The dosage table above is the
one to read for decode performance.

Every earlier revision of this document compared polars-bio at eight
partitions against single-threaded readers and drew a conclusion from it. That
is corrected above: the comparison is at one partition.

### Decode only

Stripping the materialization from both sides — polars-bio's scan measured in
Rust with no Python and no contiguous-array consolidation, against pgenlib's
`read_dosages_list`, both single-threaded:

| | decode |
|---|---:|
| pgenlib | 1.51 s |
| polars-bio, 1 partition | 3.46 s |

**2.3×.** `datafusion/bio-format-pgen/examples/pgen_ds_profile.rs` reproduces
the polars-bio side.

### Optimization history

The provider was profiled and optimized against this benchmark in
[datafusion-bio-formats#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232).
Single-partition whole-chromosome, interleaved against pgenlib in one session:

| Change | scan | total |
|---|---:|---:|
| baseline | 11.2 s | 19.15 s |
| Arrow values/validity buffers instead of per-cell `append_option` | ~9.0 s | 13.95 s |
| `DS` joins the single-field fast path | 7.30 s | 9.40 s |
| table-driven `append_codes` + bulk validity | 5.00 s | 7.49 s |
| skip hardcall phase orientation for dosage | **4.13 s** | **6.19 s** |

3.1× end to end. A wasted iteration is worth recording: the dense decode path
was optimized first, before checking that `plink2 --make-pgen` writes
LD-compressed records — only 3.8% of this fixture takes the dense path, and
81% are `record_type=0x14`. Tracing which path records actually take should
have come first.

These figures are consistent with snputils' own published benchmark, whose
PGEN panel shows snputils and pgenlib within noise of each other on hardcalls
(0.6 s each on 8 EPYC cores).

## Why polars-bio is slower

The per-cell and per-variant overheads described in earlier revisions of this
document — a `ListBuilder::append_option` per genotype cell, a per-variant
`Vec<Option<f32>>`, dosage derived through an intermediate allele pair — were
fixed in [#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232)
and are gone. What remains, measured at one partition:

| Stage | Time |
|---|---:|
| Planning, PVAR/PSAM parsing, metadata columns | 0.48 s |
| Genotype decode + Arrow array construction | 4.13 s |
| Consolidating 189 batches into one 10.13 GB array | ~1.4 s |
| **Total** | **~6.2 s** |
| pgenlib, one pass into a preallocated buffer | 1.85 s |

Two causes, in order of size:

**1. LD reconstruction writes a code per sample that a second pass reads back.**
`plink2 --make-pgen` writes LD-compressed records — on this fixture 81% are
`record_type=0x14` and only 3.8% are eligible for the dense decode. Those
records go through `decode_main_into`, which reconstructs a `u8` category per
sample against the previous record, after which `append_codes` reads it and
writes the `f32` output. pgenlib fuses the two. Profiling the Rust scan alone
puts `decode_difflist`, `Cursor::varint`, and `decode_main_into` together at
roughly a fifth of samples, with per-variant `RecordIndex::record` lookups
another tenth, and no single dominant hotspot beyond that. Fusing decode and
emit means restructuring the decode core around a sink that writes the final
representation directly, while still retaining codes for the next record's LD
base — larger than a perf patch.

**2. One materialization pass pgenlib never performs.** polars-bio emits
batches; a single contiguous NumPy array requires consolidating them, copying
10.13 GB at roughly memory bandwidth. Writing chunks into a preallocated array
instead of `combine_chunks` measures the same (1.376 s vs 1.393 s), so this is
a floor rather than something to tune. A larger `datafusion.execution.batch_size`
reduces it (2.23 s → 1.39 s), which is why the harness sets it. **This cost
does not exist for streaming or SQL consumers** — it is created by the
benchmark's requirement for one contiguous array, which pgenlib satisfies for
free by decoding straight into the caller's buffer.

Peak RSS follows from the same architecture: polars-bio holds the Arrow buffer
and the NumPy output simultaneously, 17.8 GB against pgenlib's 12.4 GB.

Whether the remaining gap is worth closing depends on the use case. For a query
engine the decode feeds Arrow, and the second cost never appears; for a
whole-matrix export, pgenlib is the better tool and is what this benchmark
measures against.

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

### The comparison can fail

A zero-difference result is worthless if the comparison cannot report a
difference. `benchmarks/pgen_verify.py` corrupts a single cell of the reader
under test and asserts the corruption is detected;
`selftest_single_cell_detected: 1` is recorded in the result files and the run
aborts if it is ever 0.

### Row order

A scan with more than one partition may emit rows out of source order. On the
whole chromosome the emitted order descends 90–107 times at eight partitions
and never at one. Value and position hashes are taken after sorting by
position, and the raw descent count is recorded per run rather than hidden.

## Timing contract

The timer covers fileset opening, companion discovery and parsing, record
decoding, variant positions and sample identifiers, and final C-contiguous
materialization in the workload's dtype. Imports and thread-pool configuration
are excluded. Peak RSS is process `ru_maxrss`; hashing runs outside the timer.
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

## Inputs, builds, and versions

| Item | Value |
|---|---|
| Slice | `chr22.first-25000.pgen`, 2,923,281 bytes |
| Whole chromosome | `chr22.full.pgen`, 79,921,211 bytes (+113,320,253-byte `.pvar`) |
| Whole chromosome SHA-256 | `ca2267eb44335ee1…` |
| Source callset | IGSR/1000 Genomes GRCh38 phased chromosome 22, as used by the BCF and BGEN benchmarks |
| Export | `plink2 --make-pgen`, PLINK v2.0.0-a.7.3 M1 (8 Aug 2026) |
| datafusion-bio-formats | [`e029e08`](https://github.com/biodatageeks/datafusion-bio-formats/commit/e029e08) |
| polars-bio branch build | [`d9cc111`](https://github.com/biodatageeks/polars-bio/commit/d9cc111), branch `feat/bgen-pr220-bench` ([#436](https://github.com/biodatageeks/polars-bio/pull/436), not merged) |
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
.venv/bin/python run_pgen_benchmarks.py \
  --runs 2 --modes dosage hardcall --polars-bio-partitions 1 2 4 8 \
  --pgen /path/to/chr22.full.pgen \
  --expected-rows 993881 --expected-samples 2548 \
  --output results/pgen_reader_benchmark_full_cohort.json
```

The whole-chromosome run holds two full matrices in one process during
verification, peaking near 21 GB. Pass `--skip-verification` on a smaller host;
the per-run equivalence hashes still have to agree.
