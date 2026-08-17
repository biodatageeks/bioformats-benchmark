# PGEN genotype-reader benchmark

polars-bio reads PLINK 2 filesets through `read_pgen` / `scan_pgen`, backed by
the `datafusion-bio-format-pgen` provider. This compares it against
[snputils](https://github.com/AI-sandbox/snputils) and against
[pgenlib](https://pypi.org/project/Pgenlib/), PLINK 2's own reference reader,
on the same chromosome 22 callset the BCF and BGEN benchmarks use.

Every reader materializes the same canonical array: ALT allele dosage per
sample per variant as `float32`, with missing calls as NaN. A run that finishes
is therefore evidence that the readers agree, not merely that they completed.

## Result

### Whole chromosome, ALT dosage

993,881 variants by 2,548 samples, 2,532,408,788 dosage values. Medians of two
fresh-process runs. Lower is better.

| Reader | Time | Peak RSS | Speed relative to snputils |
|---|---:|---:|---:|
| pgenlib | **2.702 s** | 14,529 MB | **12.166× faster** |
| **polars-bio**, 8 partitions | **7.800 s** | 20,325 MB | **4.214× faster** |
| polars-bio, 4 partitions | 9.018 s | 19,529 MB | 3.645× faster |
| polars-bio, 2 partitions | 10.789 s | 19,757 MB | 3.047× faster |
| polars-bio, 1 partition | 15.857 s | 20,673 MB | 2.073× faster |
| snputils | 32.873 s | 23,382 MB | 1.000× |

polars-bio is **4.214× faster than snputils at eight partitions**, a 76.3%
reduction in wall time, and is still 2.073× faster at one partition. That
single-partition win is the notable difference from the BGEN benchmark, where
polars-bio is 1.93× *slower* than snputils at one partition and only overtakes
it through parallelism. PGEN hardcalls are 2-bit packed with no per-variant
decompression step, so there is less per-core work for a C extension to win on.

**pgenlib is 2.9× faster than polars-bio's best result and 12.2× faster than
snputils.** It is a thin C reader that fills a NumPy array directly, with no
Arrow, no Polars, and no query engine. polars-bio's advantage here is over
snputils, not over the format's native reader, and this table reports both.

Peak RSS is 13.1% *below* snputils at eight partitions and below it at every
partition count. pgenlib uses 28.5% less than polars-bio.

### Chromosome slice, ALT dosage

25,000 variants by 2,548 samples, 63,700,000 dosage values. Medians of three
fresh-process runs.

| Reader | Time | Peak RSS | Speed relative to snputils |
|---|---:|---:|---:|
| pgenlib | **0.076 s** | 408 MB | **19.658× faster** |
| **polars-bio**, 8 partitions | **0.622 s** | 986 MB | **2.402× faster** |
| polars-bio, 4 partitions | 0.638 s | 980 MB | 2.342× faster |
| polars-bio, 2 partitions | 0.712 s | 977 MB | 2.098× faster |
| polars-bio, 1 partition | 0.821 s | 964 MB | 1.820× faster |
| snputils | 1.494 s | 1,254 MB | 1.000× |

The slice is 2.9 MB of genotype payload, so fixed costs — interpreter startup
excluded, but Arrow schema construction, companion parsing, and the final NumPy
materialization included — dominate. It is reported because it is the size at
which the BGEN and BCF benchmarks report, not because it says much about
scaling.

### Partition scaling

Speedup against the same reader at one partition:

| Partitions | Whole chromosome | Slice |
|---|---:|---:|
| 2 | 1.47× | 1.15× |
| 4 | 1.76× | 1.29× |
| 8 | **2.03×** | 1.32× |

**Scaling is clearly sublinear, and weaker than the BGEN reader's**, which
reaches 4.98× at eight partitions on the same cohort. Two reasons, neither
speculative:

1. There is less to parallelize. BGEN spends most of its time in per-variant
   zlib decompression, which is embarrassingly parallel. A PGEN hardcall block
   is a 2-bit-packed bitmap; unpacking it is memory-bandwidth-bound, and eight
   partitions contend for the same bandwidth.
2. The tail is serial. Converting the Arrow `genotypes` struct into a
   C-contiguous `float32` matrix happens once, after the scan, on one thread.
   At 2.53 billion cells that conversion is a fixed ~4 s floor that no
   partition count reduces, which is most of the gap between 2.03× and linear.

The single-partition whole-chromosome measurement is also the noisiest in the
set (standard deviation 2.49 s over two runs, against 0.19–0.22 s at higher
partition counts), so the 1→8 ratio is a soft number. Two runs is thin; treat
the scaling column as a direction, not a measurement.

## Zero mismatches

Every reader is checked against pgenlib, PLINK 2's reference implementation,
with **no tolerance**: the comparison counts cells that differ bitwise, not
cells that differ by more than an epsilon.

| Comparison | Cells | Differing |
|---|---:|---:|
| polars-bio vs pgenlib — whole chromosome | 2,532,408,788 | **0** |
| snputils vs pgenlib — whole chromosome | 2,532,408,788 | **0** |
| polars-bio vs pgenlib — slice | 63,700,000 | **0** |
| snputils vs pgenlib — slice | 63,700,000 | **0** |

polars-bio is bit-identical to pgenlib at **every** partition count — 1, 2, 4,
and 8 — on both fixtures, verified through the per-run `value_sha256` as well
as the element-wise pass.

### The comparison can fail

A zero-difference result is worthless if the comparison is incapable of
reporting a difference. `benchmarks/pgen_verify.py` therefore corrupts a single
cell of the reader under test and asserts that the corruption is detected;
`selftest_single_cell_detected: 1` is recorded in both result files, and the
run aborts if it is ever 0.

Three further negative controls were run by hand against the slice while
developing the harness, all detected: a single flipped cell (1 differing cell),
a one-row shift (7,913,320), and reversed sample order (5,183,438).

### Row order

A scan with more than one partition may emit rows out of source order, because
DataFusion coalesces partitions as their batches become ready. On the whole
chromosome the emitted order descends 90–107 times at eight partitions, 90–92
at four, 92 at two, and never at one. Content is unaffected: value and position
hashes are taken after sorting rows by position, and the raw descent count is
recorded per run rather than hidden. The BGEN and BCF providers behave the same
way; it is a property of the shared scan path and is documented in
`docs/features/reading.md` in polars-bio.

## Equivalent workload

The array every reader produces is ALT allele count per sample per variant,
`float32`, C-contiguous, `(variants, samples)`, with missing calls as NaN.
PLINK 2 encodes a missing hardcall as `-9`; every reader normalizes that to NaN
so missing cells compare equal rather than differing on sentinel choice.

Reader-native execution is preserved:

- **pgenlib** fills a preallocated `int8` buffer per variant through
  `PgenReader.read`, then converts once. Variant positions and sample
  identifiers are parsed from the `.pvar` and `.psam` by the harness, because
  pgenlib reads only the `.pgen`. That keeps the oracle independent of
  polars-bio's companion parsing rather than borrowing it.
- **snputils** uses `PGENReader(...).read()` and its returned `calldata_gt`,
  summed across the ploidy axis.
- **polars-bio** uses a lazy scan with projection pushdown, selecting
  `genotype_fields=["GT"]`, then converts the Arrow result to NumPy. The two
  allele slots are summed through strided views directly into the `float32`
  output. An earlier version of this harness materialized an intermediate pairs
  array first, which at whole-chromosome scale is an extra 10 GB and was being
  charged to polars-bio as reader overhead; on the slice, removing it moved
  polars-bio from 1.186 s / 2,308 MB to 0.753 s / 964 MB with an identical
  value hash. The lean path is what the numbers above use.

`genotype_fields=["GT"]` is passed explicitly even though it is the Python
default, because the provider default emits all five genotype children — `GT`,
`PHASED`, `DS`, `DS_STORED`, `HDS` — and measuring that would compare five
representations against the other readers' one.

## Timing contract

The timer covers:

- fileset opening and `.pvar` / `.psam` companion discovery and parsing;
- record decoding;
- variant positions and sample identifiers;
- final C-contiguous `float32` materialization.

Imports and thread-pool configuration are excluded. Peak RSS is process
`ru_maxrss` after the array, positions, and sample IDs are retained. Hashing
runs outside the timer. Measurements use a warm filesystem cache and a
deterministically rotated, direction-alternating reader order, so no reader is
always measured first. `OMP`, `OpenBLAS`, `MKL`, `Accelerate`, and `NumExpr`
thread pools are capped at one for every reader; `POLARS_MAX_THREADS`, Rayon,
and DataFusion target partitions follow the partition count under test.

### Build profile is part of the result

polars-bio **must** be built release with `-C target-cpu=native`. A plain
`maturin develop` produces a debug build that measured 3.1× slower on the slice
(3.71 s against 1.19 s at one partition) — enough to report polars-bio as 2.6×
*slower* than snputils instead of faster. The runner records the loaded
extension's path and size in `metadata.polars_bio_build` so the profile can be
checked after the fact; the release extension is ~228 MB and the debug one
~336 MB.

## Inputs, builds, and versions

| Item | Value |
|---|---|
| Slice | `chr22.first-25000.pgen`, 2,923,281 bytes (+2,900,231-byte `.pvar`) |
| Slice SHA-256 | `a56ef3d4117e02ba…` |
| Whole chromosome | `chr22.full.pgen`, 79,921,211 bytes (+113,320,253-byte `.pvar`) |
| Whole chromosome SHA-256 | `ca2267eb44335ee1…` |
| Source callset | IGSR/1000 Genomes GRCh38 phased chromosome 22, the same VCF used by the BCF and BGEN benchmarks |
| Export | `plink2 --make-pgen`, PLINK v2.0.0-a.7.3 M1 (8 Aug 2026) |
| datafusion-bio-formats | [`e029e08`](https://github.com/biodatageeks/datafusion-bio-formats/commit/e029e08) |
| polars-bio branch build | [`d9cc111`](https://github.com/biodatageeks/polars-bio/commit/d9cc111), branch `feat/bgen-pr220-bench` ([#436](https://github.com/biodatageeks/polars-bio/pull/436), not merged) |
| snputils | 1.1.1.dev17+gbdb1a56b5 |
| pgenlib | 0.94.1 |
| polars-bio / Polars / PyArrow / NumPy | 0.33.1 (branch build) / 1.42.1 / 24.0.0 / 2.5.2 |
| Python | 3.12.9 |
| Host | Apple M3 Max, 16 CPU cores, 64 GiB RAM, macOS 15.6 arm64 |
| polars-bio build | release, `RUSTFLAGS="-C target-cpu=native"` |

The measurements were taken through `.venv-bcf`, which installs polars-bio
editable from a local checkout, rather than through the `.venv` that `setup.sh`
creates. The build fingerprint in each result file records that
(`editable_install: true`).

## Reproduce

The PGEN fixtures are exported from the chromosome 22 callset the BCF benchmark
already downloads, so no additional download is needed. Exporting them needs
[plink2](https://www.cog-genomics.org/plink/2.0/) on `PATH`; `setup.sh` creates
them alongside the BGEN fixtures.

Build polars-bio optimized first — this is not optional, see above:

```bash
cd /path/to/polars-bio
RUSTFLAGS="-C target-cpu=native" maturin develop --release --locked
```

Then:

```bash
.venv/bin/python run_pgen_benchmarks.py \
  --runs 3 --polars-bio-partitions 1 2 4 8 \
  --pgen /path/to/chr22.first-25000.pgen \
  --expected-rows 25000 --expected-samples 2548 \
  --output results/pgen_reader_benchmark.json

.venv/bin/python run_pgen_benchmarks.py \
  --runs 2 --polars-bio-partitions 1 2 4 8 \
  --pgen /path/to/chr22.full.pgen \
  --expected-rows 993881 --expected-samples 2548 \
  --output results/pgen_reader_benchmark_full_cohort.json
```

The whole-chromosome run holds two full matrices in one process during the
element-wise verification pass, peaking near 21 GB. Pass `--skip-verification`
on a smaller host; the per-run equivalence hashes still have to agree.

Each JSON holds environment metadata, the polars-bio build fingerprint, the
deterministic run order, every raw result, medians and standard deviations, the
equivalence hashes, the element-wise verification against pgenlib including its
self-test, and the relative comparisons.
