# bioformats-benchmark

Benchmark comparing BAM, VCF, and FASTQ file reading performance across Python bioinformatics libraries, measuring execution time and peak memory usage.

## Libraries Tested

| Library | Mode | Formats |
|---------|------|---------|
| **pysam** | eager | BAM, VCF, FASTQ |
| **oxbow** | eager | BAM, VCF, FASTQ |
| **oxbow** | lazy | BAM, VCF, FASTQ |
| **biobear** | eager | BAM, VCF, FASTQ |
| **polars-bio** | eager | BAM, VCF, FASTQ |
| **polars-bio** | lazy | BAM, VCF, FASTQ |

## Test Variants

| Format | Variant | Description |
|--------|---------|-------------|
| BAM | `with_tags` | All 13 optional BAM tags included |
| BAM | `without_tags` | Core SAM fields only |
| VCF | `with_info` | All INFO fields parsed |
| VCF | `without_info` | Fixed fields + FORMAT only (INFO excluded) |
| FASTQ | `all_columns` | All columns (name, sequence, quality, comment) |

## Data Requirements

| Format | File | Source |
|--------|------|--------|
| BAM | `NA12878.proper.wes.md.chr1.bam` (~2 GB) | Extract from full WES BAM with `samtools view -b ... chr1` |
| VCF | `homo_sapiens-chr1.vcf.gz` | Ensembl (downloaded by `setup.sh`) |
| FASTQ | `ERR194158.fastq.gz` | EBI SRA (downloaded by `setup.sh`) |

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

# 5. Run a single benchmark standalone
DATA_PATH=/path/to/file.bam BENCH_VARIANT=with_tags python -m benchmarks.bench_bam_pysam

# 6. Generate report
python generate_report.py
```

## Configuration

- **Data file paths**: Defaults in `benchmarks/common.py` and `run_benchmarks.py`, overridable via `DATA_PATH` env var
- **Benchmark variant**: Controlled by `BENCH_VARIANT` env var (`with_tags`, `without_tags`, `with_info`, `without_info`, `all_columns`)
- **Number of runs**: Set in `run_benchmarks.py` (`NUM_RUNS` constant, default: 2)
- **Thread control**: All benchmarks run single-threaded via env vars set in `benchmarks/common.py`

## Output

Results are written to:
- `results/benchmark_results.json` — raw benchmark data (grouped by format and variant)
- `results/report.md` — formatted markdown report with tables, speedup analysis, code snippets, and reproduction instructions

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
  bench_fastq_pysam.py          # FASTQ benchmarks (6 files)
  bench_fastq_oxbow_eager.py
  bench_fastq_oxbow_lazy.py
  bench_fastq_biobear_eager.py
  bench_fastq_polars_bio_eager.py
  bench_fastq_polars_bio_lazy.py
run_benchmarks.py               # Multi-format orchestrator
run_thread_benchmarks.py        # polars-bio thread scaling (BAM)
generate_report.py              # Report generator
setup.sh                        # Environment + data setup
```
