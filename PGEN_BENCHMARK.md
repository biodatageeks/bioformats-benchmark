# PGEN genotype-reader benchmark

polars-bio reads PLINK 2 filesets through `read_pgen` / `scan_pgen`, backed by
the `datafusion-bio-format-pgen` provider. This compares it against
[snputils](https://github.com/AI-sandbox/snputils) and against
[pgenlib](https://pypi.org/project/Pgenlib/), PLINK 2's own reference reader,
on the same chromosome 22 callset the BCF and BGEN benchmarks use.

**polars-bio is currently slower than both.** The gap is understood and
mechanical, not mysterious; the causes are in
[Why polars-bio is slower](#why-polars-bio-is-slower).

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

| Reader | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|
| pgenlib `read_dosages_list` | **1.874 s** | 12,382 MB | 1.00× | 1.87× faster |
| snputils (int8 read + widen) | 3.498 s | 14,682 MB | 0.54× | 1.00× |
| polars-bio, 8 partitions | 4.125 s | 19,224 MB | **0.45×** | 0.85× |
| polars-bio, 1 partition | 12.319 s | 19,582 MB | 0.15× | 0.28× |

### Hardcall workload — `int8`, 2.53 GB output

| Reader | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|
| pgenlib `read_list` | **0.846 s** | 5,137 MB | 1.00× | 2.16× faster |
| snputils `genotype_mode="dosage"` | 1.825 s | 5,285 MB | 0.46× | 1.00× |
| polars-bio, 8 partitions | 8.105 s | 18,480 MB | **0.10×** | 0.23× |
| polars-bio, 1 partition | 15.172 s | 19,290 MB | 0.06× | 0.12× |

polars-bio is **2.2× slower than pgenlib** on the dosage workload at eight
partitions, and **9.6× slower** on the hardcall workload, where it must
materialize `float32` and narrow it because it has no int8 representation.

These figures are consistent with snputils' own published benchmark, whose
PGEN panel shows snputils and pgenlib within noise of each other on hardcalls
(0.6 s each on 8 EPYC cores).

## Why polars-bio is slower

Measured decomposition of the dosage workload at one partition:

| Stage | Time |
|---|---:|
| Planning, PVAR/PSAM parsing, metadata columns | 0.48 s |
| Genotype decode + Arrow array construction | ~9.6 s |
| `combine_chunks` — concatenating 189 batches into one 10.13 GB array | 2.37 s |
| **Total** | **~12.3 s** |
| pgenlib, one pass into a preallocated buffer, zero copies | 1.87 s |

Three mechanical causes, in order of size:

**1. Per-cell Arrow builder calls.** `build_ds_array` appends one value at a
time:

```rust
for row in rows {
    for value in values(decoded) {
        builder.values().append_option(*value);   // 2.53 billion calls
    }
    builder.append(true);
}
```

Each call carries a null-check branch, a validity-bitmap bit push, and a
capacity-checked value push: ~3.8 ns/cell against pgenlib's ~0.6 ns/cell. The
GT path already uses a slice append (`append_slice(call)`), makes half as many
calls, and is 1.75 s faster for the same output size — the cost tracks call
count, not bytes.

**2. Dosage derived through an intermediate allele pair.** For a hardcall
fileset each cell goes 2-bit → category → `[u16; 2]` → iterator filter/count →
`f32`:

```rust
fn alt1_hardcall_dosage(call: &[u16; 2]) -> f32 {
    call.iter().filter(|&&allele| allele == 1).count() as f32
}
```

pgenlib goes 2-bit → table lookup → `f32`.

**3. A concatenation pgenlib never performs.** polars-bio emits 189 batches;
one contiguous NumPy array requires concatenating them, which copies 10.13 GB.
`batch_soft_byte_limit` does not change this. Because the decode parallelizes
but this copy does not, partition scaling saturates near 3× rather than
approaching 8×.

Peak RSS follows from the same architecture: polars-bio holds the Arrow buffer
and the NumPy output simultaneously, 19.2 GB against pgenlib's 12.4 GB.

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
