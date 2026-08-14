# BGEN genotype-reader benchmark

Run date: 2026-08-14 (re-run on the reviewed commits)

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
| **polars-bio**, 8 partitions | **7.073 s** | 25,936 MB | **1.895× faster** |
| polars-bio, 4 partitions | 9.585 s | 25,911 MB | 1.398× faster |
| bgen | 10.436 s | 21,779 MB | 1.284× faster |
| snputils | 13.403 s | 21,947 MB | 1.000× |
| polars-bio, 2 partitions | 13.689 s | 25,962 MB | 0.979× |
| polars-bio, 1 partition | 24.568 s | 25,232 MB | 0.546× |

polars-bio matches snputils at two partitions, passes it at four, and is
**1.895× faster at eight**, a 47.2% reduction in wall time. At one partition it
is 1.83× slower: snputils'
BGEN reader is a single-threaded C extension built around libdeflate, and
polars-bio spends its extra time building Arrow arrays and handing them to
Polars. The advantage here comes from partition parallelism, not from a faster
per-core decoder, and the table reports both points rather than only the
favourable one.

The tables above come from one complete suite run. A confirmation run on the
final reviewed commits, after the last round of review fixes, measured 6.862 s
at eight partitions against snputils' 13.368 s (1.948x), so the documented
figures are slightly conservative.

polars-bio also carries more output. Its `genotypes` struct holds `PLOIDY`
alongside `DS`, which the other readers do not produce, and the dosage matrix
crosses Arrow, Polars, and NumPy before it is returned. That shows up in peak
RSS, which is 18.2% above snputils.

### Chromosome slice, both workloads

25,000 variants by 2,548 samples, the same slice the
[VCF/BCF genotype benchmark](GENOTYPE_READER_BENCHMARK.md) uses. Medians of
three fresh-process runs.

| Reader | Dosage (phased) | Dosage (unphased) | Probabilities (phased) | Probabilities (unphased) |
|---|---:|---:|---:|---:|
| **polars-bio**, 8 partitions | **0.188 s** | **0.190 s** | 1.296 s | 0.593 s |
| polars-bio, 4 partitions | 0.297 s | 0.296 s | 1.488 s | 0.774 s |
| polars-bio, 1 partition | 0.585 s | 0.594 s | 2.057 s | 1.314 s |
| snputils | 0.315 s | 0.316 s | 0.365 s | 0.358 s |
| bgen | 0.273 s | 0.312 s | **0.316 s** | **0.314 s** |
| pysnptools | unsupported | 1.852 s | unsupported | 2.089 s |

For dosage, polars-bio is 1.68× faster than snputils at eight partitions and
1.06× faster at four. For the complete probability tensor it is slower
everywhere: 3.6× slower at its best point on the phased file and 1.7× slower on
the unphased file. The probability path returns a nested Arrow list that is
converted to Polars and back before NumPy sees it, and that conversion does not
parallelize, so extra partitions help far less than they do for dosage.

**The result is reported per workload.** polars-bio's BGEN advantage is a dosage
advantage; it is not a claim that polars-bio is a faster BGEN parser in general.

pysnptools cannot read the phased files at all: `pysnptools.distreader.Bgen`
asserts unphased input. Its cells are recorded as unsupported rather than slow.

## Zero mismatches

The `bgen` package is the independent oracle, the same role it plays in
[snputils' own BGEN benchmark](https://github.com/AI-sandbox/snputils/tree/main/benchmark).
Every other reader is compared against it element by element, in one process,
with no tolerance.

| Comparison | Cells | Differing cells | Largest difference |
|---|---:|---:|---:|
| **polars-bio vs bgen**, dosage, whole chromosome | 2,532,408,788 | **0** | 0 |
| **polars-bio vs bgen**, dosage, slice | 63,700,000 | **0** | 0 |
| **polars-bio vs bgen**, probabilities, slice | 254,800,000 | **0** | 0 |
| snputils vs bgen, dosage, whole chromosome | 2,532,408,788 | 126,259,603 | 1.18e-07 |
| snputils vs bgen, dosage, phased slice | 63,700,000 | 4,607,588 | 1.18e-07 |
| snputils vs bgen, probabilities, slice | 254,800,000 | 0 | 0 |
| pysnptools vs bgen, unphased slice, both workloads | 63,700,000 / 191,100,000 | 0 | 0 |

polars-bio reproduces the reference **bit for bit**, in every workload, on every
file, at 1, 2, 4, and 8 partitions. snputils is bit-identical for probabilities
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
documents.

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
  result to NumPy.

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
| datafusion-bio-formats | [`b96628f`](https://github.com/biodatageeks/datafusion-bio-formats/commit/b96628f9e3e40689828246de78949a79ace2e29d) |
| polars-bio branch build | [`ef9b5c2`](https://github.com/biodatageeks/polars-bio/commit/ef9b5c2) |
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
.venv/bin/python run_bgen_benchmarks.py \
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
