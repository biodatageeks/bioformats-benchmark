# BGEN genotype-reader benchmark

Run date: 2026-08-18 for the whole-chromosome dosage table, re-measured against
[datafusion-bio-formats#234](https://github.com/biodatageeks/datafusion-bio-formats/pull/234)
and [#235](https://github.com/biodatageeks/datafusion-bio-formats/pull/235),
which cut the one-partition scan by 2.1x. The slice tables below are from the
2026-08-16 session ([#226](https://github.com/biodatageeks/datafusion-bio-formats/pull/226)
and [#227](https://github.com/biodatageeks/datafusion-bio-formats/pull/227)) and
were **not** re-measured; they are marked where they appear.

This benchmark compares polars-bio, snputils, the `bgen` package, and pysnptools
on BGEN genotype matrices. Every reader must produce the same ordered variant
positions, the same sample order, and the same C-contiguous `float32` array.
The runner refuses to publish a result when those disagree.

## Result

### Whole chromosome, ALT dosage

993,881 variants by 2,548 samples, 2,532,408,788 dosage values. Medians of three
fresh-process runs, all readers interleaved in one session. Lower is better.

| Reader | Time | Peak RSS | Speed relative to snputils |
|---|---:|---:|---:|
| **polars-bio**, 8 partitions | **3.789 s** | 23,276 MB | **5.755× faster** |
| polars-bio, 4 partitions | 5.419 s | 22,236 MB | 4.024× faster |
| polars-bio, 2 partitions | 8.255 s | 21,909 MB | 2.642× faster |
| **polars-bio, 1 partition** | **14.142 s** | 24,291 MB | **1.542× faster** |
| bgen | 15.680 s | 20,377 MB | 1.391× faster |
| snputils | 21.807 s | 21,020 MB | 1.000× |

**polars-bio is now the fastest of the three at one thread**, 1.109× the `bgen`
package and 1.542× snputils, where it used to be 1.93× slower than snputils. The
three one-partition runs were 13.600 s, 14.181 s and 14.142 s, all below the
`bgen` package's slowest of 15.813 s, so the ranges do not overlap.

Earlier revisions of this document explained the one-thread gap as polars-bio
"building Arrow arrays and handing them to Polars". That was wrong, and
measuring it is what closed the gap: Arrow construction is **four milliseconds**
of a one-partition scan, because the decoder writes Arrow's layout as it goes.
The cost was the per-sample decode loop, and
[Where the time went](#where-the-time-went) has the account.

**Peak RSS on this path is not a stable measurement, and should not be read to
three digits.** An earlier revision of this document recorded the rise from
20,462 MB to 24,291 MB as an unexplained regression. It is not one. Peak RSS here
is set almost entirely by one call in the benchmark adapter, and that call's peak
varies by gigabytes between runs of identical code.

Instrumenting the polars-bio dosage adapter stage by stage:

| Stage | Peak RSS |
|---|---:|
| after `collect()` | 12.80 GB, ±35 MB across five runs |
| after `combine_chunks()` | **16.8 / 20.9 / 21.3 / 21.4 / 22.3 GB** |
| after both SHA-256 passes | unchanged |

`combine_chunks()` concatenates the scan's 340 Arrow chunks into one, so both the
chunked and the combined copy of 12.8 GB of genotype data exist at once; where
the peak lands inside that window is up to the allocator. The 3.8 GB "regression"
is smaller than the 5.5 GB spread of that single call, and the decoder changes
cannot explain it in any case — the Rust scan's own peak is 804 MB before and
797 MB after, and #234 is inlining, which cannot allocate differently at all.

Two things are worth taking from that table rather than from the RSS column.
**The genotype data itself is stable at 12.80 GB** for a 10.13 GB answer, and the
0.5 GB difference above 12.66 GB is everything else in the process. **2.53 GB of
those 12.80 are `PLOIDY`**, which the `genotypes` struct always carries and no
projection can drop; the returned matrix is a view into the combined Arrow table,
so that column stays resident for the life of the result. Making `PLOIDY`
projectable is the one real memory saving visible here.

The comparison readers do not pay the chunk-consolidation cost at all: they build
their matrix directly. That is a property of this adapter, not of the readers,
and it is why the RSS column is reported but not leaned on.

Scaling against one partition is 1.71× at two, 2.61× at four and 3.73× at eight,
down from 1.88×/3.11×/4.98×. That is Amdahl arithmetic rather than a regression —
the serial baseline is 1.9× faster than it was, so the same parallel work divides
a smaller total.

### Chromosome slice, both workloads

*From the 2026-08-16 session; **not** re-measured against the decode changes, so
the one-partition rows here are still the old, slower loop. The whole-chromosome
table above is the current one.*

25,000 variants by 2,548 samples, the same slice the
[VCF/BCF genotype benchmark](GENOTYPE_READER_BENCHMARK.md) uses. Medians of
three fresh-process runs.

| Reader | Dosage (phased) | Dosage (unphased) | Probabilities (phased) | Probabilities (unphased) |
|---|---:|---:|---:|---:|
| **polars-bio**, 8 partitions | **0.166 s** | **0.174 s** | **0.297 s** | **0.264 s** |
| polars-bio, 4 partitions | 0.236 s | 0.229 s | 0.368 s | 0.332 s |
| polars-bio, 2 partitions | 0.391 s | 0.396 s | 0.585 s | 0.540 s |
| polars-bio, 1 partition | 0.636 s | 0.651 s | 0.922 s | 0.873 s |
| snputils | 0.331 s | 0.333 s | 0.399 s | 0.374 s |
| bgen | 0.282 s | 0.322 s | 0.333 s | 0.327 s |
| pysnptools | unsupported | 1.926 s | unsupported | 2.184 s |

polars-bio is fastest at eight partitions in every column: 1.99× snputils on
phased dosage, 1.91× on unphased dosage, 1.34× on phased probabilities and 1.42×
on unphased probabilities. It is also ahead of the `bgen` package in all four,
which it was not before payload ranges were balanced.

Probabilities are measured through the fixed-width layout, which emits no
per-sample list offsets and NaN-pads a narrower sample to the file's widest.
That layout previously rejected the phased file outright, because plink2 leaves
461 of its 25,000 variants unphased and the widths therefore mix; the phased
column above was 1.296 s through the nested layout before it was supported.

**The result is still reported per workload.** At one partition polars-bio was
slower than every other reader in every column of this slice session. That is no
longer true of the whole chromosome, where it is now the fastest of the three at
one thread; the slice has not been re-measured and its one-partition rows should
be read as historical.

pysnptools cannot read the phased files at all: `pysnptools.distreader.Bgen`
asserts unphased input. Its cells are recorded as unsupported rather than slow.

### Partition scaling

*Slice figures, from the 2026-08-16 session; not re-measured.* Speedup against
the same reader at one partition, from the slice table above:

| Partitions | Dosage (phased) | Dosage (unphased) | Probabilities (phased) | Probabilities (unphased) |
|---:|---:|---:|---:|---:|
| 2 | 1.63× | 1.64× | 1.58× | 1.62× |
| 4 | 2.69× | 2.84× | 2.51× | 2.63× |
| 8 | 3.83× | 3.74× | 3.10× | 3.31× |

Scaling is sub-linear and the table says so. Two partitions return about 1.6×,
eight about 3.1–3.8× on this 4.9 MB slice — a small file amortises the fixed
per-scan cost over less work. The whole chromosome now returns 3.73× at eight,
down from 4.98× in this session, because its one-partition baseline got 1.9×
faster while the parallel work did not.

**These figures used to be far worse, for a reason worth recording.** A BGEN
payload range was capped at one partition's byte share, and a variant's payload
cannot be split across ranges, so a scan was handed `target_partitions + 1`
indivisible chunks — which never divide evenly into `target_partitions`. One
partition always took two. At two partitions the planned split was 87.2% / 12.8%,
and 1 / 0.872 = 1.15, which was exactly the measured two-partition speedup: 1.16×
in every column. Ranges now target several per partition, bounded below so a scan
does not issue reads far under a useful object size
([#227](https://github.com/biodatageeks/datafusion-bio-formats/pull/227)).

The remaining ceiling is not the decoder. Measured on the Rust scan alone, eight
partitions reach 4.63× of one, while the end-to-end figures above reach 3.1–3.8×;
the difference is the fixed Python-side cost of handing the result to NumPy,
which does not parallelise.

## Where the time went

A one-partition whole-chromosome scan was 24.3 s of Rust, of which the Python
handoff is about 2 s. `examples/bgen_decode_profile` in the provider splits the
rest by walking the variant records itself and inflating every payload with the
same library the reader uses — the floor any reader of this file pays — and then
running the provider's own scan against it:

| Phase | Before | After |
|---|---:|---:|
| zlib inflate (libdeflate) | 9.5 s | 9.5 s |
| the decode loop | 14.4 s | **2.3 s** |
| Arrow batch construction | 0.004 s | 0.004 s |
| I/O, planning, record parsing | 0.2 s | 0.2 s |

Two things fall out of that table.

**Decompression is a shared floor, not a gap.** 7.61 GB comes out of a 160 MB
file, and every reader here pays about 9.5 s to produce it through libdeflate.
The `bgen` package reads the file in 15.7 s, so its own work is around 6 s; this
provider's is now 2.3 s. Attributing the old gap to decompression was never
consistent with both readers using the same library.

**Arrow construction is four milliseconds.** The decoder writes Arrow's layout as
it goes, so building a batch only wraps buffers. The earlier explanation in this
document — that polars-bio "builds Arrow arrays and hands them to Polars" — was
measuring nothing.

The cost was the loop that turns a decompressed block into output, at 5.15 ns per
genotype over 2.53 billion of them. Two changes account for the difference:

- **The per-sample helpers were out-of-line calls.** `byte_dosage_numerator` and
  `GenotypeBuffers::close_sample` are invoked once per sample and a profile
  showed each as a real frame: 15% and 18% of the scan. Neither was reachable by
  a hint — one already carried `#[inline]` and LLVM declined it; the other's
  callers were marked but it was not. `#[inline(always)]` on both was worth 31%
  of the non-decompression work
  ([#234](https://github.com/biodatageeks/datafusion-bio-formats/pull/234)).
- **The loop decided per sample what the read had already decided for all of
  them.** It gathered through the selected-sample index array even when the
  selection was the whole cohort in file order, recorded a uniform ploidy one
  byte at a time, and checked a missingness that a fully called variant does not
  have. A whole-cohort, diploid, fully called dosage read now fills the values
  buffer straight from the stored byte pairs. That took the non-decompression
  work from 10.2 s to 2.3 s
  ([#235](https://github.com/biodatageeks/datafusion-bio-formats/pull/235)).

The output is bit-identical across both changes, which the equivalence table
below is the check on: the dosages are written by the same expression the
per-sample path uses, not an equivalent one.

## Zero mismatches

The `bgen` package is the independent oracle, the same role it plays in
[snputils' own BGEN benchmark](https://github.com/AI-sandbox/snputils/tree/main/benchmark).
Every other reader is compared against it element by element, in one process,
with no tolerance.

| Comparison | Cells | Differing cells | Largest difference |
|---|---:|---:|---:|
| **polars-bio vs bgen**, dosage, whole chromosome | 2,532,408,788 | **0** | 0 |
| **polars-bio vs bgen**, dosage, slice | 63,700,000 | **0** | 0 |
| **polars-bio vs bgen**, probabilities, phased slice | 254,800,000 | **0** | 0 |
| **polars-bio vs bgen**, probabilities, unphased slice | 191,100,000 | **0** | 0 |
| snputils vs bgen, dosage, whole chromosome | 2,532,408,788 | 126,259,603 | 1.18e-07 |
| snputils vs bgen, dosage, phased slice | 63,700,000 | 4,607,588 | 1.18e-07 |
| snputils vs bgen, probabilities, slice | 254,800,000 | 0 | 0 |
| pysnptools vs bgen, unphased slice, both workloads | 63,700,000 / 191,100,000 | 0 | 0 |

polars-bio reproduces the reference **bit for bit**, in every workload, on every
file, at 1, 2, 4, and 8 partitions, and through both probability layouts. The
comparison counts raw `uint32` bit patterns as well as values, so the `NaN` the
fixed layout pads with matches the reference's `NaN` exactly rather than merely
comparing equal. snputils is bit-identical for probabilities
and for unphased dosage; its phased dosage differs by at most one `float32` step
near 1.0, which is a rounding difference in how it sums haplotype probabilities,
not a decoding difference.

The dosage hash is also identical between the phased and the unphased fixture
(`2ce16fab…`), which is the expected invariant: erasing phase changes how the
probabilities are stored but not the expected allele count they imply.

### Row order

DataFusion coalesces partitions as their batches become ready, so a polars-bio
scan with more than one partition may emit rows out of source order; the runner
counts those inversions in `emission_order_descents`. Content is unaffected: the
value and position hashes above are taken after sorting rows by variant
position, and they are identical at every partition count. The merged BCF
provider behaves the same way, so this is a property of the shared scan path
rather than of the BGEN reader. Sort explicitly when row order matters.

## Equivalent workload

| Property | Slice | Whole chromosome |
|---|---:|---:|
| Variants | 25,000 | 993,881 |
| Samples | 2,548 | 2,548 |
| Dosage values | 63,700,000 | 2,532,408,788 |
| Probability values (phased) | 254,800,000 | not measured |
| Output dtype/layout | C-contiguous row-major `float32` | same |
| Missing genotype | `NaN` | `NaN` |
| Position SHA-256 (slice) | `c93113df749db3d267d9ffcc122d455432da54f70a32c8387a147cf1b2d73218` | `db55a6b0aac688960a47c2c4b180b4d03c897134f4807fe8984750509979e50d` |
| Sample-order SHA-256 | `454b4158e145da471f07a9a2edc3bc2f651f1d1722b28434476fc9b7d6388c6d` | same |

The slice positions and sample order hash to exactly the values recorded in
[GENOTYPE_READER_BENCHMARK.md](GENOTYPE_READER_BENCHMARK.md), because both
benchmarks are derived from the same 1000 Genomes chromosome 22 callset slice.

`dosage` is the expected copy count of the second encoded allele. `probabilities`
is the complete genotype-probability tensor: width 3 for unphased biallelic
records and width 4 for phased ones. plink2 leaves 461 of the 25,000 variants
unphased inside the phased export, so that file mixes widths; every reader pads
the short rows with `NaN` on the right, which is the convention snputils
documents. polars-bio does that padding inside the scan when the fixed-width
layout is selected, which is how that layout became usable on a mixed-width
file.

## Timing contract

Each measurement starts a new Python process. Imports and thread-pool setup are
outside the timer. The timed section includes:

- source opening and header/index discovery;
- block decompression;
- probability decoding;
- variant positions and sample identifiers;
- final C-contiguous `float32` materialization.

Peak RSS is process `ru_maxrss` after the array, positions, and sample IDs are
retained. Hashing runs outside the timer and streams rows in chunks so it does
not add a full-size copy to peak memory. Measurements use a warm filesystem
cache and a deterministically rotated reader order. `OMP`, `OpenBLAS`, `MKL`,
`Accelerate`, and `NumExpr` thread pools are capped at one for every reader;
`POLARS_MAX_THREADS`, Rayon, and DataFusion target partitions follow the
partition count under test.

Reader-native execution is preserved where possible:

- snputils uses its native whole-file dosage decoder, then a second metadata
  pass for positions and sample IDs, because its dosage API does not return
  them. That pass does not decode probabilities and costs about 0.065 s on the
  slice, roughly 20% of its dosage time;
- the `bgen` package fills a preallocated array from its record iterator;
- pysnptools uses its `distreader` and converts probabilities to dosage;
- polars-bio uses a lazy scan with projection pushdown, then converts the Arrow
  result to NumPy. Probability runs select the fixed-width layout
  (`BGEN_PROBABILITY_LAYOUT=fixed`), whose values buffer reshapes into the output
  array without a copy; the default nested layout is measured in
  [datafusion-bio-formats#226](https://github.com/biodatageeks/datafusion-bio-formats/pull/226)
  and is slower here.

## Inputs, builds, and versions

| Item | Value |
|---|---|
| Phased slice | `chr22.first-25000.bgen`, 5,280,919 bytes |
| Phased slice SHA-256 | `e45d2de525500f47…` |
| Unphased slice | `chr22.first-25000.unphased.bgen`, 4,892,969 bytes |
| Unphased slice SHA-256 | `620b7f67de13b76f…` |
| Whole chromosome | `chr22.full.bgen`, 160,522,183 bytes |
| Whole chromosome SHA-256 | `867e8bf0cc162ab0…` |
| Source callset | IGSR/1000 Genomes GRCh38 phased chromosome 22, the same VCF used by the BCF benchmark |
| Export | `plink2 --export bgen-1.2 bits=8`, Layout 2, zlib |
| datafusion-bio-formats, whole-chromosome table | [`5f3dcf3`](https://github.com/biodatageeks/datafusion-bio-formats/commit/5f3dcf3), branch `perf/bgen-bulk-dosage-fill` ([#235](https://github.com/biodatageeks/datafusion-bio-formats/pull/235) stacked on [#234](https://github.com/biodatageeks/datafusion-bio-formats/pull/234); neither merged yet) |
| datafusion-bio-formats, slice tables | [`cbbb489`](https://github.com/biodatageeks/datafusion-bio-formats/commit/cbbb489), branch `agent/bgen-range-granularity` ([#227](https://github.com/biodatageeks/datafusion-bio-formats/pull/227) stacked on [#226](https://github.com/biodatageeks/datafusion-bio-formats/pull/226)) |
| polars-bio branch build | built against whichever provider commit the table above names |
| snputils | [`482c6d1`](https://github.com/AI-sandbox/snputils/commit/482c6d1dfd6c4001935dfaec81ae01a5e0ec3e53) for the slice; `bdb1a56` for the whole chromosome |
| bgen / pysnptools | 1.10.0 / 0.5.15 |
| polars-bio / Polars / PyArrow / NumPy | 0.33.1 (branch build) / 1.40.1 / 24.0.0 / 2.4.4 (slice); 1.42.1 / 24.0.0 / 2.5.2 (whole chromosome) |
| Python | 3.11.13 (slice), 3.12.9 (whole chromosome) |
| Host | Apple M3 Max, 16 CPU cores, 64 GiB RAM, macOS 15.6 arm64 |
| polars-bio build | release, `RUSTFLAGS="-C target-cpu=native"` |

## Reproduce

`setup.sh` installs the pinned readers into `.venv` and creates the BGEN
fixtures from the chromosome 22 callset the BCF benchmark already downloads.
Exporting them needs [plink2](https://www.cog-genomics.org/plink/2.0/) on
`PATH`. Then:

```bash
BGEN_PROBABILITY_LAYOUT=fixed .venv/bin/python run_bgen_benchmarks.py \
  --runs 3 --workloads dosage probabilities \
  --polars-bio-partitions 1 2 4 8 \
  --bgen /path/to/chr22.first-25000.bgen \
  --output results/bgen_reader_phased.json

.venv/bin/python run_bgen_benchmarks.py \
  --runs 2 --workloads dosage \
  --polars-bio-partitions 1 2 4 8 \
  --bgen /path/to/chr22.full.bgen --expected-rows 993881 \
  --output results/bgen_reader_full_cohort.json
```

Each JSON holds environment metadata, the deterministic run order, every raw
result, medians and standard deviations, the equivalence hashes, the
element-wise verification against the `bgen` oracle, and the relative
comparisons.
