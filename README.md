# bioformats-benchmark

Benchmark comparing BAM, VCF, BCF, and FASTQ file reading performance across Python bioinformatics libraries, measuring execution time and peak memory usage.

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
| **polars-bio** | lazy/streaming | BAM, VCF, BCF, FASTQ |
| **snputils** | eager | VCF, BCF |

## Test Variants

| Format | Variant | Description |
|--------|---------|-------------|
| BAM | `with_tags` | All 13 optional BAM tags included |
| BAM | `without_tags` | Core SAM fields only |
| VCF | `with_info` | All INFO fields parsed |
| VCF | `without_info` | Fixed fields + FORMAT only (INFO excluded) |
| BCF | `dosage` | All phased GT calls converted to an `Int8` ALT-dosage matrix |
| VCF/BCF | `genotype-matrix` | Identical 25,000 x 2,548 row-major `Int8` ALT-dosage matrix |
| FASTQ | `all_columns` | All columns (name, sequence, quality, comment) |

## Data Requirements

| Format | File | Source |
|--------|------|--------|
| BAM | `NA12878.proper.wes.md.chr1.bam` (~2 GB) | Extract from full WES BAM with `samtools view -b ... chr1` |
| VCF | `homo_sapiens-chr1.vcf.gz` | Ensembl (downloaded by `setup.sh`) |
| BCF | `ALL.chr22.phased.bcf` (~129 MiB) | IGSR/1000 Genomes GRCh38 phased chromosome 22 callset, converted by `setup.sh` |
| FASTQ | `ERR194158.fastq.gz` | EBI SRA (downloaded by `setup.sh`) |

The BCF fixture contains 993,881 biallelic variants and 2,548 samples. The
dosage workload therefore materializes 2,532,408,788 `Int8` values. `setup.sh`
verifies the source VCF SHA-256
(`b428192af4f02507585c3775e59251974c71a968daa895a9a47acb337140614c`),
and each run records the generated BCF SHA-256 in its result metadata.

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

# 9. Generate publication figures for the genotype-reader comparison
python generate_genotype_reader_figures.py \
  --output-dir /path/to/polars-bio/docs/blog/posts/figures/vcf-bcf-readers
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

## Configuration

- **Data file paths**: Defaults in `benchmarks/common.py`; BCF is overridable with `BCF_PATH`
- **Benchmark variant**: Controlled by `BENCH_VARIANT` (`dosage` for BCF)
- **Number of runs**: Set in `run_benchmarks.py` (`NUM_RUNS` constant, default: 2)
- **BCF runs/partitions**: `run_bcf_benchmarks.py --runs 3 --threads 1`; the
  thread value controls polars-bio target partitions and thread caps, while the
  pinned snputils BCF reader remains serial

## Output

Results are written to:
- `results/benchmark_results.json` — raw benchmark data (grouped by format and variant)
- `results/bcf_benchmark_t{1,2,4,8}.json` — BCF raw runs, environment metadata, and summary statistics for the scaling sweep
- `results/genotype_reader_benchmark.json` — t=1 VCF/BCF reader matrix with raw timing/RSS, medians, and equivalence hashes
- `results/report.md` — formatted markdown report with tables, speedup analysis, code snippets, and reproduction instructions
- `BCF_BENCHMARK.md` — tracked BCF result report for the reviewed PR/branch refs
- `GENOTYPE_READER_BENCHMARK.md` — tracked output-equivalent t=1 VCF/BCF reader comparison

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
  genotype_matrix.py              # One fresh-process VCF/BCF reader workload
  bench_fastq_pysam.py          # FASTQ benchmarks (6 files)
  bench_fastq_oxbow_eager.py
  bench_fastq_oxbow_lazy.py
  bench_fastq_biobear_eager.py
  bench_fastq_polars_bio_eager.py
  bench_fastq_polars_bio_lazy.py
run_benchmarks.py               # Multi-format orchestrator
run_bcf_benchmarks.py           # Isolated BCF correctness/timing/RSS runner
run_genotype_matrix_benchmarks.py # pysam/PyVCF3/cyvcf2/Oxbow/polars/snputils
generate_genotype_reader_figures.py # Timing, memory, and scaling plots
run_thread_benchmarks.py        # polars-bio thread scaling (BAM)
generate_report.py              # Report generator
setup.sh                        # Environment + data setup
```
