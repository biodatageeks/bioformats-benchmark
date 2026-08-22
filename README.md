# bioformats-benchmark

Benchmark comparing genomic file-reading performance across Python bioinformatics
libraries, measuring execution time, peak memory usage, and partition scalability.

## Libraries Tested

| Library | Mode | Formats |
|---------|------|---------|
| **pysam** | eager | BAM, VCF, FASTQ |
| **PyVCF3** | eager | VCF |
| **cyvcf2** | eager | VCF, BCF |
| **oxbow** | eager | BAM, VCF, FASTQ |
| **oxbow** | lazy/streaming | BAM, VCF, BCF, FASTQ |
| **biobear** | eager | BAM, VCF, FASTQ |
| **polars-bio** | eager | BAM, VCF, FASTQ |
| **polars-bio** | lazy/streaming | BAM, VCF, BCF, BGEN, PGEN, BigWig, BigBed, FASTQ |
| **snputils** | eager | VCF, BCF, BGEN |
| **bgen** | eager | BGEN |
| **pysnptools** | eager | BGEN (unphased only) |

## Test Variants

| Format | Variant | Description |
|--------|---------|-------------|
| BAM | `with_tags` | All 13 optional BAM tags included |
| BAM | `without_tags` | Core SAM fields only |
| VCF | `with_info` | All INFO fields parsed |
| VCF | `without_info` | Fixed fields + FORMAT only (INFO excluded) |
| BCF | `dosage` | All phased GT calls converted to an `Int8` ALT-dosage matrix |
| VCF/BCF | `genotype-matrix` | Identical 25,000 x 2,548 row-major `Int8` ALT-dosage matrix |
| BGEN | `dosage` | Expected copies of the second encoded allele as a `float32` matrix |
| BGEN | `probabilities` | Complete `float32` genotype-probability tensor |
| BigWig | four BBI scaling workloads | Arrow streaming, Polars count, all-column aggregate, and literal all-column collection |
| BigBed | four BBI scaling workloads | Arrow streaming, Polars count, all-column aggregate, and literal all-column collection |
| FASTQ | `all_columns` | All columns (name, sequence, quality, comment) |

## Data Requirements

| Format | File | Source |
|--------|------|--------|
| BAM | `NA12878.proper.wes.md.chr1.bam` (~2 GB) | Extract from full WES BAM with `samtools view -b ... chr1` |
| VCF | `homo_sapiens-chr1.vcf.gz` | Ensembl (downloaded by `setup.sh`) |
| BCF | `ALL.chr22.phased.bcf` (~129 MiB) | IGSR/1000 Genomes GRCh38 phased chromosome 22 callset, converted by `setup.sh` |
| BGEN | `chr22.full.bgen` (~153 MiB), `chr22.first-25000[.unphased].bgen` | Exported from the same chromosome 22 callset by `setup.sh` with plink2 |
| BigWig | `GSM7256643_...GRCh38.bigWig` (~546 MiB) | NCBI GEO, downloaded and checksum-verified by `setup.sh` |
| BigBed | `ENCFF001JBR.bigBed` (~16 MiB) | ENCODE, downloaded and checksum-verified by `setup.sh` |
| FASTQ | `ERR194158.fastq.gz` | EBI SRA (downloaded by `setup.sh`) |

The BCF fixture contains 993,881 biallelic variants and 2,548 samples. The
dosage workload therefore materializes 2,532,408,788 `Int8` values. `setup.sh`
verifies the source VCF SHA-256
(`b428192af4f02507585c3775e59251974c71a968daa895a9a47acb337140614c`),
and each run records the generated BCF SHA-256 in its result metadata.

The BGEN fixtures are exported from the same callset, so the BGEN benchmark
compares the same variants and sample order as the VCF/BCF one. See
[BGEN_BENCHMARK.md](BGEN_BENCHMARK.md) for the results, which include an
element-wise check against the independent `bgen` package.

The PGEN fixtures come from that same callset via `plink2 --make-pgen`, so the
PGEN benchmark compares the same variants and sample order again. See
[PGEN_BENCHMARK.md](PGEN_BENCHMARK.md) for the results, which include an
element-wise check against pgenlib, PLINK 2's reference reader, and a self-test
proving that check can fail.

The BigWig/BigBed sweep compares every partition count from one through eight.
See [BBI_BENCHMARK.md](BBI_BENCHMARK.md) for the issue 238 candidate's
whole-file scaling results, correctness fingerprints, and memory tradeoff.

The cross-reader VCF/BCF matrix uses rows in
`chr22:10516173-16717478` from that same callset: exactly 25,000 variants,
2,548 samples, and 63,700,000 dosage cells. `setup.sh` derives both indexed
formats from the full BCF so every reader sees the same ordered records.

## Quick Start

```bash
# 1. Setup environment and download VCF/FASTQ data
bash setup.sh

# 2. Activate venv
source .venv/bin/activate

# 3. Run all benchmarks (BAM + VCF + FASTQ)
python run_benchmarks.py

# 4. Run a single format
python run_benchmarks.py --format bam
python run_benchmarks.py --format vcf
python run_benchmarks.py --format fastq

# 5. Verify and benchmark BCF in isolated child processes (3 runs each)
for t in 1 2 4 8; do
  python run_bcf_benchmarks.py --threads "$t" \
    --output "results/bcf_benchmark_t${t}.json"
done

# 6. Run a single benchmark standalone
DATA_PATH=/path/to/file.bam BENCH_VARIANT=with_tags python -m benchmarks.bench_bam_pysam

# 7. Compare pysam, PyVCF3, cyvcf2, Oxbow, polars-bio, and snputils at t=1
python run_genotype_matrix_benchmarks.py --runs 3

# 8. Generate report
python generate_report.py

# 9. Generate BCF-only publication figures for the genotype-reader comparison
python generate_genotype_reader_figures.py \
  --output-dir /path/to/polars-bio/docs/blog/posts/figures/bcf-readers

# 10. Measure every BigWig/BigBed partition count from one through eight
./setup_bbi_benchmark.sh
.venv-bbi/bin/python run_bbi_benchmarks.py \
  --python .venv-bbi/bin/python \
  --partitions 1 2 3 4 5 6 7 8 \
  --runs 5 \
  --label candidate \
  --output results/bbi_scaling_candidate.json

# 11. Plot one run, or compare baseline and candidate result files
python generate_bbi_figures.py \
  --input results/bbi_scaling_candidate.json \
  --output-dir results/bbi-figures

# 12. Validate the BBI benchmark harness
.venv-bbi/bin/pytest tests/test_bbi_benchmark.py
```

To benchmark an unreleased polars-bio checkout, point setup at the checkout.
Setup installs it with `maturin develop --release --locked` and defaults to
`RUSTFLAGS="-C target-cpu=native"`:

```bash
POLARS_BIO_SOURCE=/path/to/polars-bio bash setup.sh
source .venv/bin/activate
POLARS_BIO_REF=<polars-bio-commit> \
DATAFUSION_BIO_FORMATS_REF=<formats-pr-commit> \
python run_bcf_benchmarks.py
```

For an issue-238 before/after comparison, set `POLARS_BIO_SOURCE` when running
`setup_bbi_benchmark.sh` and build polars-bio once against the
released `datafusion-bio-formats` revision and once against the candidate
revision. Give the runs distinct `--label` and `--output` values, then pass both
JSON files to `generate_bbi_figures.py`. Use `--physical-partitions serial` for
the pre-partitioning baseline; candidate runs use the strict default
`--physical-partitions requested`.

### BigWig/BigBed scalability correctness

`run_bbi_benchmarks.py` launches every measurement in a fresh child process and
sets `POLARS_MAX_THREADS`, `RAYON_NUM_THREADS`, `TOKIO_WORKER_THREADS`, and
DataFusion `target_partitions` to the same `t`. The default sweep is every
integer from one through eight. Combination order rotates and reverses between
rounds to reduce cache and thermal bias. Each child also inspects the physical
plan after timing and records the BBI scan's advertised output partition count.
Candidate sweeps fail unless that count equals `t`. When the provider reports
index-derived data-byte estimates, the runner verifies that the layout is stable
across repetitions and records its coefficient of variation and maximum-to-mean
ratio for each `t`.

The four workloads separate source scalability from downstream materialization:

- `arrow_stream_all` requests and drains every Arrow column without retaining
  the whole file. It measures the provider plus the Python Arrow stream and
  records the source batch count.
- `polars_count` executes `pl.len()` end to end. Polars currently requests the
  first public column (`chrom`), so this is intentionally not described as an
  empty-projection DataFusion `count(*)` workload.
- `polars_aggregate_all` requests every column and reduces row count, chromosome
  bytes, coordinates, and payload values to a correctness fingerprint.
- `polars_collect_all` literally materializes every row and column in a Polars
  DataFrame. It records retained chunk count, estimated DataFrame size, and peak
  RSS in addition to wall time.

After the timed workload, every child performs a separate untimed all-column
validation scan. Two independently seeded, order-independent row-hash sums plus
row count, coordinate sums, chromosome bytes, and payload aggregates must match
across all workloads and every `t` before results are written. BigBed performs
ten timed scans per child by default because the fixture is too short for a
stable single timing; the JSON records both the iteration count and per-scan
time. Each raw sample also records ambient CPU use measured immediately before
launch. The configured `--max-system-cpu-percent` value (or `null` when the
optional abort gate is disabled) is recorded in result metadata.

### BCF fairness and correctness

Both BCF runners read the same file, project only `FORMAT/GT`, and materialize
the same ALT-dosage values. `snputils` returns its native 2-D NumPy `int8`
matrix and exposes no BCF reader thread-count option. `polars-bio` keeps the
source lazy, projection-pushes `GT`, directly decodes the BCF allele bytes into
nullable Arrow `Int8` dosage, and collects with Polars' streaming engine; its
equivalent output is a list column with one list per variant. The tracked report
includes a polars-bio `t=1,2,4,8` scaling sweep against the serial snputils
control.

Before timing, `benchmarks.verify_bcf_equivalence` compares all variant keys,
the complete sample order, and all 2.53 billion dosage values in bounded row
chunks. Timing and peak RSS then run in fresh child processes. Reader order is
reversed on alternating rounds to reduce cache/order bias.

### Cross-reader VCF/BCF fairness

`run_genotype_matrix_benchmarks.py` uses fresh child processes and caps all
known native thread pools at one thread. Source opening, header/schema
discovery, parsing, GT decoding, biallelic ALT-dosage conversion, and final
row-major NumPy `int8` materialization are timed. Imports and thread-pool
configuration are excluded. Every completed run must match the same position,
sample-order, and all-cell SHA-256 values across both file formats before a
result is accepted. Peak RSS includes the retained comparable matrix. Oxbow
uses bounded Arrow record batches; polars-bio uses lazy scans and streaming
collection. PyVCF3's BCF cell is recorded as unsupported because the library
only reads text VCF.

See [GENOTYPE_READER_BENCHMARK.md](GENOTYPE_READER_BENCHMARK.md) for the
output-equivalent pysam/PyVCF3/cyvcf2/Oxbow/polars-bio/snputils comparison, and
[BCF_BENCHMARK.md](BCF_BENCHMARK.md) for the exact-head full-cohort scaling and
correctness proof.

polars-bio must be built release with `-C target-cpu=native` before any timing
run. A plain `maturin develop` is a debug build and measured 3.1x slower on the
PGEN slice — enough to invert the comparison against snputils. The PGEN runner
records the loaded extension's size in its result metadata so the profile can be
checked afterwards.

## Configuration

- **Data file paths**: Defaults in `benchmarks/common.py`; BCF is overridable with `BCF_PATH`
- **Benchmark variant**: Controlled by `BENCH_VARIANT` (`dosage` for BCF)
- **Number of runs**: Set in `run_benchmarks.py` (`NUM_RUNS` constant, default: 2)
- **BCF runs/partitions**: `run_bcf_benchmarks.py --runs 3 --threads 1`; the
  thread value controls polars-bio target partitions and thread caps, while the
  pinned snputils BCF reader remains serial
- **BBI runs/partitions**: `run_bbi_benchmarks.py --runs 5 --partitions 1 2 3 4 5 6 7 8`
- **BBI paths**: `BIGWIG_PATH` and `BIGBED_PATH`, or the matching runner options

## Output

Results are written to:
- `results/benchmark_results.json` — raw benchmark data (grouped by format and variant)
- `results/bcf_benchmark_t{1,2,4,8}.json` — BCF raw runs, environment metadata, and summary statistics for the scaling sweep
- `results/genotype_reader_benchmark.json` — t=1 VCF/BCF reader matrix with raw timing/RSS, medians, and equivalence hashes
- `results/pgen_reader_benchmark.json`, `results/pgen_reader_benchmark_full_cohort.json` — PGEN reader matrix with raw timing/RSS, medians, equivalence hashes, the polars-bio build fingerprint, and the element-wise pgenlib verification
- `results/bbi_scaling*.json` — BigWig/BigBed raw runs, correctness fingerprints, throughput, speedup, and parallel efficiency for each `t`
- `results/bbi-figures/{bigwig,bigbed}-scaling.png` — wall-time, throughput, speedup, and efficiency curves
- `results/report.md` — formatted markdown report with tables, speedup analysis, code snippets, and reproduction instructions
- `BCF_BENCHMARK.md` — tracked BCF result report for the reviewed PR/branch refs
- `GENOTYPE_READER_BENCHMARK.md` — tracked output-equivalent t=1 VCF/BCF reader comparison
- `PGEN_BENCHMARK.md` — tracked PGEN polars-bio/snputils/pgenlib comparison
- `BBI_BENCHMARK.md` — tracked BigWig/BigBed t=1–8 scalability comparison

## Project Structure

```
benchmarks/
  common.py                     # Shared config, paths, run_benchmark()
  bench_bam_pysam.py            # BAM benchmarks (6 files)
  bench_bam_oxbow_eager.py
  bench_bam_oxbow_lazy.py
  bench_bam_biobear_eager.py
  bench_bam_polars_bio_eager.py
  bench_bam_polars_bio_lazy.py
  bench_vcf_pysam.py            # VCF benchmarks (6 files)
  bench_vcf_oxbow_eager.py
  bench_vcf_oxbow_lazy.py
  bench_vcf_biobear_eager.py
  bench_vcf_polars_bio_eager.py
  bench_vcf_polars_bio_lazy.py
  bcf_common.py                  # Shared, semantically matched dosage workload
  bench_bcf_polars_bio.py        # Lazy/streaming BCF -> dosage lists
  bench_bcf_snputils.py           # BCF -> dosage ndarray
  verify_bcf_equivalence.py       # Full row/sample/genotype comparison
  bbi_common.py                  # BBI paths + fresh-process timing utility
  bench_bbi_polars_bio.py        # BBI source, count, aggregate, and collect workloads
  genotype_matrix.py              # One fresh-process VCF/BCF reader workload
  bench_fastq_pysam.py          # FASTQ benchmarks (6 files)
  bench_fastq_oxbow_eager.py
  bench_fastq_oxbow_lazy.py
  bench_fastq_biobear_eager.py
  bench_fastq_polars_bio_eager.py
  bench_fastq_polars_bio_lazy.py
run_benchmarks.py               # Multi-format orchestrator
run_bcf_benchmarks.py           # Isolated BCF correctness/timing/RSS runner
run_bbi_benchmarks.py           # BigWig/BigBed t=1..8 scalability runner
setup_bbi_benchmark.sh          # Exact Python/package environment for BBI runs
tests/test_bbi_benchmark.py     # BBI runner and plotting validation tests
run_genotype_matrix_benchmarks.py # pysam/PyVCF3/cyvcf2/Oxbow/polars/snputils
generate_genotype_reader_figures.py # Timing, memory, and scaling plots
generate_bbi_figures.py         # BBI scalability and before/after plots
run_thread_benchmarks.py        # polars-bio thread scaling (BAM)
generate_report.py              # Report generator
setup.sh                        # Environment + data setup
```
