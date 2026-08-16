# BGEN genotype-reader benchmark

Run date: 2026-08-16 (re-run on the batch-buffer probability path, [datafusion-bio-formats#226](https://github.com/biodatageeks/datafusion-bio-formats/pull/226))

This benchmark compares polars-bio, snputils, the `bgen` package, and pysnptools
on BGEN genotype matrices. Every reader must produce the same ordered variant
positions, the same sample order, and the same C-contiguous `float32` array.
The runner refuses to publish a result when those disagree.

## Result

### Whole chromosome, ALT dosage

993,881 variants by 2,548 samples, 2,532,408,788 dosage values. Medians of two
fresh-process runs. Lower is better.

| Reader | Time | Peak RSS | Speed relative to snputils |
|---|---:|---:|---:|
| **polars-bio**, 8 partitions | **7.475 s** | 23,052 MB | **1.886× faster** |
| polars-bio, 4 partitions | 10.281 s | 22,817 MB | 1.371× faster |
| bgen | 10.779 s | 21,797 MB | 1.308× faster |
| snputils | 14.098 s | 21,952 MB | 1.000× |
| polars-bio, 2 partitions | 14.837 s | 22,459 MB | 0.950× |
| polars-bio, 1 partition | 27.837 s | 22,473 MB | 0.506× |

polars-bio is **1.886× faster at eight partitions**, a 47.0% reduction in wall
time, roughly matches snputils at two, and is 1.98× slower at one: snputils' BGEN
reader is a single-threaded C extension built around libdeflate, and polars-bio
spends its extra time building Arrow arrays and handing them to Polars. The
advantage here comes from partition parallelism, not from a faster per-core
decoder, and the table reports both points rather than only the favourable one.

Peak RSS is now 5.0% above snputils, down from 18.2%. polars-bio still carries
more output — its `genotypes` struct holds `PLOIDY` alongside `DS`, which the
other readers do not produce, and the matrix crosses Arrow, Polars, and NumPy
before it is returned — but the decoder no longer stages each variant in its own
allocation before copying it into the batch, which is where most of that
overhead was.

Scaling against one partition is 1.88× at two, 2.71× at four and 3.72× at eight.
It is better here than on the slice below, and for a reason worth knowing: a
payload range is capped at the smaller of `max_range_bytes` (16 MiB) and one
partition's byte share. On a 160 MB file the 16 MiB cap binds and the scan gets
about ten chunks to spread; on the 5 MB slice the partition share binds and it
gets only `target + 1`. See [Partition scaling](#partition-scaling).

### Chromosome slice, both workloads

25,000 variants by 2,548 samples, the same slice the
[VCF/BCF genotype benchmark](GENOTYPE_READER_BENCHMARK.md) uses. Medians of
three fresh-process runs.

| Reader | Dosage (phased) | Dosage (unphased) | Probabilities (phased) | Probabilities (unphased) |
|---|---:|---:|---:|---:|
| **polars-bio**, 8 partitions | **0.196 s** | **0.203 s** | **0.337 s** | **0.302 s** |
| polars-bio, 4 partitions | 0.314 s | 0.323 s | 0.481 s | 0.441 s |
| polars-bio, 2 partitions | 0.563 s | 0.555 s | 0.788 s | 0.745 s |
| polars-bio, 1 partition | 0.653 s | 0.646 s | 0.925 s | 0.866 s |
| snputils | 0.334 s | 0.335 s | 0.389 s | 0.381 s |
| bgen | 0.279 s | 0.326 s | 0.329 s | 0.324 s |
| pysnptools | unsupported | 1.918 s | unsupported | 2.201 s |

polars-bio is fastest at eight partitions in every column: 1.70× snputils on
phased dosage, 1.65× on unphased dosage, 1.15× on phased probabilities and 1.26×
on unphased probabilities.

Probabilities are measured through the fixed-width layout, which emits no
per-sample list offsets and NaN-pads a narrower sample to the file's widest.
That layout previously rejected the phased file outright, because plink2 leaves
461 of its 25,000 variants unphased and the widths therefore mix; the phased
column above was 1.296 s through the nested layout before it was supported.

**The result is still reported per workload,** and the honest comparison is
against the `bgen` package rather than only against snputils: polars-bio passes
it on unphased probabilities and remains marginally behind it on the phased file
(0.337 s against 0.329 s). At one partition polars-bio is slower than every
other reader in every column; the advantage is partition parallelism.

pysnptools cannot read the phased files at all: `pysnptools.distreader.Bgen`
asserts unphased input. Its cells are recorded as unsupported rather than slow.

### Partition scaling

Speedup against the same reader at one partition, from the table above:

| Partitions | Dosage (phased) | Dosage (unphased) | Probabilities (phased) | Probabilities (unphased) |
|---:|---:|---:|---:|---:|
| 2 | 1.16× | 1.16× | 1.17× | 1.16× |
| 4 | 2.08× | 2.00× | 1.92× | 1.96× |
| 8 | 3.33× | 3.18× | 2.75× | 2.87× |

**The two-partition step is worth 1.16×, not 2×, and the cause is planning
rather than decoding.** BGEN payload ranges are capped at one partition's byte
share, and a variant's payload cannot be split across ranges, so a scan is
handed `target_partitions + 1` indivisible chunks — which never divide evenly
into `target_partitions`. One partition always takes two chunks. At two
partitions the planned split is 87.2% / 12.8%, and 1 / 0.872 = 1.15.

Measured directly on the Rust scan, capping ranges at a fixed 128 KiB instead
and changing nothing else: two partitions go from 1.16× to 1.82×, four from
2.10× to 3.21×, eight from 3.64× to 5.09×, with one partition unchanged. The
decoder is not the ceiling here; the largest partition is.

That is left as a known limitation rather than tuned away, because finer ranges
mean more object-store requests — cheap against a local file, expensive against
remote storage — so the fix is a trade-off that deserves its own change.

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
| datafusion-bio-formats | [`cdcad67`](https://github.com/biodatageeks/datafusion-bio-formats/commit/cdcad67), branch `agent/bgen-probability-perf` ([#226](https://github.com/biodatageeks/datafusion-bio-formats/pull/226), not yet merged) |
| polars-bio branch build | [`ad93755`](https://github.com/biodatageeks/polars-bio/commit/ad93755), built against the provider commit above |
| snputils | [`482c6d1`](https://github.com/AI-sandbox/snputils/commit/482c6d1dfd6c4001935dfaec81ae01a5e0ec3e53) |
| bgen / pysnptools | 1.10.0 / 0.5.15 |
| polars-bio / Polars / PyArrow / NumPy | 0.33.1 (branch build) / 1.40.1 / 24.0.0 / 2.4.4 |
| Python | 3.11.13 |
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
