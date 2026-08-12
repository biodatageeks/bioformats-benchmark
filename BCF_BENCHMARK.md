# BCF dosage benchmark: polars-bio vs snputils

Run date: 2026-08-12

This benchmark compares the exact genotype-dosage workload from the snputils
BCF benchmark with a lazy/streaming polars-bio implementation. Both readers
consume the same BCF, select only `FORMAT/GT`, convert every call to biallelic
ALT dosage, and retain the complete materialized output.

The snputils call is unchanged from the pinned upstream
[`benchmark/read_bcf.py`](https://github.com/AI-sandbox/snputils/blob/bdb1a56b52a6b16210d60e347d33d023dc98352f/benchmark/read_bcf.py):
`snputils.read_bcf(path, fields=["GT"], genotype_mode="dosage",
chromosome_ploidy="autosomal").genotypes`.

## Result

| Reader | Output representation | Median time | Mean ± SD | Median peak RSS | Mean ± SD |
|---|---|---:|---:|---:|---:|
| polars-bio | 993,881-row `List(Int8)` column, list width 2,548 | 5.186 s | 5.184 ± 0.016 s | 2,640.8 MB | 2,641.1 ± 0.8 MB |
| snputils | 993,881 × 2,548 NumPy `int8` matrix | 8.874 s | 8.851 ± 0.052 s | 10,066.5 MB | 10,066.8 ± 0.8 MB |

For this full-dosage materialization workload, polars-bio is **1.711× faster**
(**41.6% less wall time**) and uses **73.8% less peak RSS**, or **3.812× lower
peak RSS**. These results apply to the exact full-cohort dosage workload; they
do not imply the same ratio for metadata-only scans, sparse samples, filtered
queries, or other BCF schemas.

The performance change is architectural. The original polars-bio benchmark
routed every genotype through generic noodles values, owned strings, and then a
Polars string-to-dosage expression; that path took about 227 seconds. The new
explicit dosage mode validates borrowed BCF FORMAT series and writes nullable
Arrow `Int8` buffers directly. Its common fixed-diploid Int8 path bulk-decodes
the raw allele bytes without genotype strings or per-cell Arrow builder calls.
BGZF input and Arrow record batches remain bounded and streaming, which avoids
snputils' resident decompressed-input buffer while also beating its specialized
eager decoder here.

## Correctness and comparability

The timed outputs use different physical schemas but identical logical cells:

- polars-bio: one `List(Int8)` dosage value per variant, with 2,548 elements;
- snputils: one NumPy `int8` matrix with shape `(993881, 2548)`.

Before timing, the full equivalence gate compared:

- all 993,881 rows by chromosome, 1-based position, ID, REF, and ALT;
- all 2,548 sample IDs in exact order;
- all 2,532,408,788 dosage cells in bounded row chunks.

The comparison passed. Evidence hashes from the verified output are:

| Evidence | SHA-256 |
|---|---|
| Row-major dosage bytes | `a0a2fb3b997e7ac5b7abebee5d85d437098c25fd4cc0178eb88830547f0062cb` |
| Position array bytes | `db55a6b0aac688960a47c2c4b180b4d03c897134f4807fe8984750509979e50d` |

The source returns nullable typed dosage (`0|0 → 0`, `0|1`/`1|0 → 1`, `1|1 →
2`, missing → null). The benchmark normalizes null to snputils' `-1` sentinel
before comparison and retention. Invalid reserved values, values after
vector-end, unrepresentable dosage, and multiallelic records fail rather than
silently changing the workload.

## Raw runs

Each measurement ran in a fresh child process. The full equivalence scan ran
first, so these are warm-cache measurements. Execution order alternated by
round: polars-bio/snputils, snputils/polars-bio, polars-bio/snputils.

| Round | Order | Reader | Time | Peak RSS |
|---:|---:|---|---:|---:|
| 1 | 1 | polars-bio | 5.186 s | 2,640.8 MB |
| 1 | 2 | snputils | 8.888 s | 10,066.3 MB |
| 2 | 1 | snputils | 8.874 s | 10,067.7 MB |
| 2 | 2 | polars-bio | 5.167 s | 2,642.0 MB |
| 3 | 1 | polars-bio | 5.198 s | 2,640.5 MB |
| 3 | 2 | snputils | 8.792 s | 10,066.5 MB |

Wall time includes file reading, decoding, dosage conversion, and complete
materialization; module imports and one-time reader configuration are outside
the timer for both readers. Peak RSS is process `ru_maxrss`, measured after the
result is retained. DataFusion, Polars, Rayon, OpenMP, BLAS, and related thread
controls were all set to one thread.

polars-bio was built with `maturin develop --release --locked` and
`RUSTFLAGS="-C target-cpu=native -C link-arg=-undefined -C
link-arg=dynamic_lookup"`. The latter two flags provide macOS Python-extension
linkage; `-C target-cpu=native` is the CPU optimization flag. The build profile
and full flags are stored in the machine-readable result metadata.

## Inputs and exact revisions

| Item | Value |
|---|---|
| BCF | `ALL.chr22.phased.bcf`, 135,128,073 bytes (128.87 MiB) |
| BCF SHA-256 | `b61c6aaa746416306a01b3aa92db23b5e1f4faf7296a114ed32d8e64a400a250` |
| Source VCF SHA-256 | `b428192af4f02507585c3775e59251974c71a968daa895a9a47acb337140614c` |
| datafusion-bio-formats PR head | [`e2e8179`](https://github.com/biodatageeks/datafusion-bio-formats/commit/e2e81793bfc448491158af61823648d499f1e69a) |
| polars-bio feature branch commit | [`2e51aeb`](https://github.com/biodatageeks/polars-bio/commit/2e51aeb222d7d438cf646af618307c56d06b08ca) |
| snputils commit | [`bdb1a56`](https://github.com/AI-sandbox/snputils/commit/bdb1a56b52a6b16210d60e347d33d023dc98352f) |
| polars-bio | 0.33.1 |
| snputils | 1.1.1.dev17+gbdb1a56b5 |
| Python / NumPy | 3.12.9 / 2.5.2 |
| Polars / PyArrow | 1.42.1 / 24.0.0 |
| Threads | 1 |
| Host | MacBook Pro, Apple M3 Max (16 CPU cores), 64 GiB RAM, macOS 15.6 arm64 |

## Reproduce

```bash
git clone https://github.com/biodatageeks/polars-bio.git
git -C polars-bio checkout 2e51aeb222d7d438cf646af618307c56d06b08ca

git clone https://github.com/biodatageeks/bioformats-benchmark.git
cd bioformats-benchmark
git checkout feat/bcf-format

POLARS_BIO_SOURCE="$(cd ../polars-bio && pwd)" \
POLARS_BIO_RUSTFLAGS='-C target-cpu=native -C link-arg=-undefined -C link-arg=dynamic_lookup' \
bash setup.sh

POLARS_BIO_REF=2e51aeb222d7d438cf646af618307c56d06b08ca \
DATAFUSION_BIO_FORMATS_REF=e2e81793bfc448491158af61823648d499f1e69a \
POLARS_BIO_BUILD_PROFILE=release \
POLARS_BIO_RUSTFLAGS='-C target-cpu=native -C link-arg=-undefined -C link-arg=dynamic_lookup' \
.venv/bin/python run_bcf_benchmarks.py --runs 3 --threads 1
```

The orchestrator refuses unexpected fixture dimensions, runs the full
equivalence gate by default, alternates reader order, records exact package and
hardware metadata, and writes the machine-readable payload to
`results/bcf_benchmark_results.json`.
