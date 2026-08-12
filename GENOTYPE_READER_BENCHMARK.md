# VCF/BCF genotype-reader benchmark

Run date: 2026-08-12

This benchmark compares pysam, PyVCF3, cyvcf2, Oxbow, polars-bio, and
snputils on one output-equivalent genotype workload. Every successful reader
must retain the same ordered positions, sample order, and complete row-major
NumPy `int8` biallelic ALT-dosage matrix. The VCF and BCF inputs are generated
from the same callset slice.

## Result

The values below are medians of two fresh-process runs with all known native
thread pools capped at one thread. Lower is better.

| Reader | VCF time | BCF time | BCF time relative to polars-bio |
|---|---:|---:|---:|
| pysam | 29.181 s | 28.328 s | 179.291× |
| PyVCF3 | 86.731 s | unsupported | — |
| cyvcf2 | 3.906 s | 1.903 s | 12.044× |
| Oxbow | 698.807 s | 13.465 s | 85.222× |
| **polars-bio** | 7.936 s | **0.158 s** | **1.000×** |
| snputils | **1.306 s** | 0.842 s | 5.329× |

For BCF, polars-bio is **5.329× faster** than snputils at the comparable
one-thread point, a reduction of **81.2% in wall time**. For text VCF,
snputils' specialized GT reader is fastest; polars-bio is 6.077× slower. The
result is deliberately reported by format rather than presenting the BCF win
as a universal parser claim.

| Reader | VCF peak RSS | BCF peak RSS |
|---|---:|---:|
| pysam | 139.9 MB | 105.8 MB |
| PyVCF3 | 122.3 MB | unsupported |
| cyvcf2 | **93.4 MB** | **93.6 MB** |
| Oxbow | 1,509.8 MB | 1,389.2 MB |
| **polars-bio** | 790.4 MB | 321.5 MB |
| snputils | 383.8 MB | 481.1 MB |

On this standardized subset output, polars-bio uses **33.2% less peak RSS**
than snputils for BCF. cyvcf2 and pysam use less memory because their
record-at-a-time adapters fill the preallocated matrix directly. Peak RSS
includes the retained 63.7 MB dosage matrix and therefore compares completed
outputs, not parser-only streaming.

The separate full-cohort scaling benchmark in
[BCF_BENCHMARK.md](BCF_BENCHMARK.md) covers 993,881 rows and 2.53 billion
dosage cells. At that scale, polars-bio is 1.622× faster and uses 73.6% less
peak RSS than snputils at one thread, then scales to a 5.577× speed-up at eight
partitions.

## Equivalent workload

Both indexed inputs contain the inclusive region
`chr22:10516173-16717478`:

| Property | Required and observed value |
|---|---:|
| Variants | 25,000 |
| Samples | 2,548 |
| Dosage values | 63,700,000 |
| Output dtype/layout | C-contiguous row-major NumPy `int8` |
| Missing dosage | `-1` |
| Position SHA-256 | `c93113df749db3d267d9ffcc122d455432da54f70a32c8387a147cf1b2d73218` |
| Sample-order SHA-256 | `454b4158e145da471f07a9a2edc3bc2f651f1d1722b28434476fc9b7d6388c6d` |
| Dosage SHA-256 | `6bfb17024850718742ca5dedb865ae01d20494c9afbb4df274a7e9fcd9657a22` |

The dosage rule is `0|0 → 0`, `0|1`/`1|0 → 1`, `1|1 → 2`, and any
missing allele → `-1`. The runner rejects unexpected dimensions, non-contiguous
or non-`int8` output, out-of-range dosage, or any cross-reader/cross-format hash
mismatch.

As an independent site check, the fixture has zero duplicate chromosome/position
pairs, and bidirectional `bcftools isec -C` reports zero sites unique to either
the VCF or BCF input.

## Timing contract

Each measurement starts a new Python process. Imports and thread-pool setup are
outside the timer. The timed section includes:

- source opening and header/schema discovery;
- input parsing and GT decoding;
- conversion to biallelic ALT dosage;
- final C-contiguous row-major `int8` materialization.

Peak RSS is process `ru_maxrss` after the comparable matrix, positions, and
sample IDs have been retained. Measurements use a warm filesystem cache and a
deterministically rotated reader order. `POLARS_MAX_THREADS`, Rayon, OpenMP,
OpenBLAS, MKL, Accelerate, NumExpr, and DataFusion target partitions are all
capped at one.

Reader-native execution is preserved where possible:

- pysam, PyVCF3, and cyvcf2 fill a preallocated matrix from their iterators;
- snputils uses its native eager dosage reader;
- Oxbow consumes bounded Arrow record batches;
- polars-bio uses a lazy scan, projection pushdown, and streaming collection.

PyVCF3 cannot read BCF, so its BCF cell is explicitly unsupported. Oxbow's VCF
time is reproducible across both rounds. The adapter uses Oxbow's compact
`genotype_by="field"` layout and bounded Arrow batches; the alternative sample
layout creates 2,548 top-level columns, while `samples_nested=True` only adds
another struct wrapper.

A post-run 1,000-row diagnostic separated Oxbow batch construction from the
common normalization. VCF source creation, schema discovery, and first-batch
materialization took 27.433 s; converting that already-materialized batch to
the dosage matrix took 0.060 s. The equivalent BCF stages took 0.499 s and
0.067 s. The wide text-VCF batch construction therefore dominates Oxbow's
result, not the benchmark's NumPy dosage conversion. This remains a full-output
benchmark and should not be interpreted as an Oxbow row-count benchmark.

## Raw runs

| Format | Reader | Round 1 time / RSS | Round 2 time / RSS |
|---|---|---:|---:|
| VCF | pysam | 29.225 s / 139.3 MB | 29.137 s / 140.4 MB |
| VCF | PyVCF3 | 86.395 s / 122.5 MB | 87.068 s / 122.1 MB |
| VCF | cyvcf2 | 3.916 s / 93.1 MB | 3.896 s / 93.8 MB |
| VCF | Oxbow | 699.657 s / 1,503.6 MB | 697.956 s / 1,516.1 MB |
| VCF | polars-bio | 7.974 s / 792.0 MB | 7.897 s / 788.8 MB |
| VCF | snputils | 1.310 s / 401.8 MB | 1.303 s / 365.8 MB |
| BCF | pysam | 28.499 s / 98.0 MB | 28.156 s / 113.7 MB |
| BCF | cyvcf2 | 1.913 s / 94.6 MB | 1.894 s / 92.6 MB |
| BCF | Oxbow | 13.420 s / 1,438.4 MB | 13.509 s / 1,340.1 MB |
| BCF | polars-bio | 0.159 s / 321.4 MB | 0.157 s / 321.7 MB |
| BCF | snputils | 0.838 s / 481.8 MB | 0.846 s / 480.5 MB |

## Inputs, builds, and versions

| Item | Value |
|---|---|
| VCF | `ALL.chr22.phased.first-25000.vcf.gz`, 5,690,789 bytes |
| VCF SHA-256 | `8dc7141b6773987167bb7edc12fbd21e2f81ba88d5578c61828915da4e675e89` |
| BCF | `ALL.chr22.phased.first-25000.bcf`, 4,666,229 bytes |
| BCF SHA-256 | `7a58230c2e091a52c2a6792b418a7c37303525579a6da0b110c33d6d6be00b1b` |
| datafusion-bio-formats | [`5e47f85`](https://github.com/biodatageeks/datafusion-bio-formats/commit/5e47f8595037d6b03b784f8dec137d904cafae1d) |
| polars-bio feature build | [`03eae00`](https://github.com/biodatageeks/polars-bio/commit/03eae0069cd245498fa416b4f42c541421d0cacc) |
| snputils | [`bdb1a56`](https://github.com/AI-sandbox/snputils/commit/bdb1a56b52a6b16210d60e347d33d023dc98352f) |
| pysam / PyVCF3 / cyvcf2 / Oxbow | 0.24.0 / 1.0.4 / 0.31.4 / 0.8.1 |
| polars-bio / snputils | 0.33.1 / 1.1.1.dev17+gbdb1a56b5 |
| Polars / PyArrow / NumPy | 1.42.1 / 24.0.0 / 2.5.2 |
| Python | 3.12.9 |
| Host | Apple M3 Max, 16 CPU cores, 64 GiB RAM, macOS 15.6 arm64 |
| polars-bio build | release, `RUSTFLAGS="-C target-cpu=native"` |

The benchmarked polars-bio commit pins every datafusion-bio-formats crate to
the exact PR head above. Later documentation-only commits do not change the
measured extension.

## Reproduce

After `setup.sh` has created the equivalent indexed subset inputs and installed
the pinned dependencies:

```bash
POLARS_BIO_REF=03eae0069cd245498fa416b4f42c541421d0cacc \
DATAFUSION_BIO_FORMATS_REF=5e47f8595037d6b03b784f8dec137d904cafae1d \
POLARS_BIO_BUILD_PROFILE=release \
POLARS_BIO_RUSTFLAGS='-C target-cpu=native' \
.venv/bin/python run_genotype_matrix_benchmarks.py \
  --runs 2 --output results/genotype_reader_benchmark.json
```

The output JSON contains environment metadata, the deterministic run order,
every raw result, medians, standard deviations, equivalence hashes, and relative
comparisons. Publication figures are generated directly from that JSON:

```bash
.venv/bin/python generate_genotype_reader_figures.py \
  --input results/genotype_reader_benchmark.json \
  --scaling-dir results \
  --output-dir /path/to/polars-bio/docs/blog/posts/figures/vcf-bcf-readers
```
