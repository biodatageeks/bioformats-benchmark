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

## Scaling result

Each point is the median of three fresh-process runs. `t` sets DataFusion target
partitions and the Polars/Rayon/OpenMP/BLAS thread caps for polars-bio. The
pinned snputils BCF reader has no worker-count argument, so its column is a
repeated single-threaded control measured under the same conditions.

| `t` | polars-bio median | Scale-up vs `t=1` | Parallel efficiency | polars-bio peak RSS | snputils control | polars-bio vs snputils |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 5.083 s | 1.000× | 100.0% | 2,645.3 MB | 8.655 s / 10,067.9 MB | 1.703× faster |
| 2 | 2.926 s | 1.737× | 86.8% | 2,647.5 MB | 8.724 s / 10,069.2 MB | 2.982× faster |
| 4 | 1.616 s | 3.146× | 78.6% | 2,652.3 MB | 8.749 s / 10,068.8 MB | 5.414× faster |
| 8 | 0.885 s | 5.744× | 71.8% | 2,662.4 MB | 8.718 s / 10,069.0 MB | 9.851× faster |

At the apples-to-apples single-thread point, polars-bio is **1.703× faster**
(**41.3% less wall time**) and uses **73.7% less peak RSS**. At `t=8`, it is
**5.744× faster than its own `t=1` result** and **9.851× faster than the serial
snputils control**, while retaining a **73.6% peak-RSS reduction**.

These numbers apply to this full-cohort dosage workload. They do not imply the
same ratios for metadata-only scans, sparse sample projections, filtered
queries, or other BCF schemas.

## Why parallel scaling initially appeared flat

The fixture has `##contig=<ID=chr22>` without a declared length. Before PR head
`952ef22`, the BCF CSI estimator could split a contig only when that optional
header length existed. The requested `t=2/4/8` values therefore all produced a
single physical partition, even though the benchmark correctly set DataFusion
target partitions.

The fix is format-level and not GT-specific: it derives a safe coordinate upper
bound and non-empty leaf-bin positions from the CSI `min_shift`, `depth`, and bin
IDs. Any indexed BCF projection can now use balanced subregions when contig
lengths are absent. A diagnostic `t=4` plan after the fix reports four CSI
regions, four physical partitions, and execution of partitions 0 through 3.

## Does snputils process BCF in parallel?

Not in the pinned implementation. Its
[`BCFReader.read`](https://github.com/AI-sandbox/snputils/blob/bdb1a56b52a6b16210d60e347d33d023dc98352f/snputils/snp/io/read/bcf.py)
API has no thread or partition parameter. It serially walks BGZF members into
one decompressed byte buffer and invokes one bulk C/NumPy decode path. The
stable 8.6–8.7 second measurements across the sweep are therefore controls, not
snputils `t=1/2/4/8` results. Environment thread caps are still applied to its
dependencies to prevent incidental BLAS oversubscription.

## Correctness and comparability

The timed outputs use different physical schemas but identical logical cells:

- polars-bio: one 2,548-wide `List(Int8)` dosage value per variant;
- snputils: one NumPy `int8` matrix with shape `(993881, 2548)`.

Before timing at every sweep point, the full equivalence gate compared:

- all 993,881 rows by chromosome, 1-based position, ID, REF, and ALT;
- all 2,548 sample IDs in exact order;
- all 2,532,408,788 dosage cells in bounded row chunks.

All four gates passed. Evidence hashes from the verified output are:

| Evidence | SHA-256 |
|---|---|
| Row-major dosage bytes | `a0a2fb3b997e7ac5b7abebee5d85d437098c25fd4cc0178eb88830547f0062cb` |
| Position array bytes | `db55a6b0aac688960a47c2c4b180b4d03c897134f4807fe8984750509979e50d` |

The source returns nullable typed dosage (`0|0 → 0`, `0|1`/`1|0 → 1`, `1|1 →
2`, missing → null). The timed polars-bio expression normalizes null to
snputils' `-1` sentinel before retention. Invalid reserved values, values after
vector-end, unrepresentable dosage, and multiallelic records fail rather than
silently changing the workload.

## Raw runs

The full equivalence scan ran before each three-round group, making these
warm-cache measurements. Reader order alternated each round.

| `t` | Round | Order | polars-bio time / RSS | snputils time / RSS |
|---:|---:|---|---:|---:|
| 1 | 1 | polars-bio → snputils | 5.072 s / 2,647.6 MB | 8.613 s / 10,068.9 MB |
| 1 | 2 | snputils → polars-bio | 5.122 s / 2,643.8 MB | 8.655 s / 10,067.4 MB |
| 1 | 3 | polars-bio → snputils | 5.083 s / 2,645.3 MB | 8.662 s / 10,067.9 MB |
| 2 | 1 | polars-bio → snputils | 2.912 s / 2,647.5 MB | 8.724 s / 10,069.3 MB |
| 2 | 2 | snputils → polars-bio | 2.943 s / 2,648.7 MB | 8.738 s / 10,069.2 MB |
| 2 | 3 | polars-bio → snputils | 2.926 s / 2,647.5 MB | 8.720 s / 10,068.4 MB |
| 4 | 1 | polars-bio → snputils | 1.626 s / 2,654.7 MB | 8.749 s / 10,069.9 MB |
| 4 | 2 | snputils → polars-bio | 1.607 s / 2,651.4 MB | 8.676 s / 10,068.3 MB |
| 4 | 3 | polars-bio → snputils | 1.616 s / 2,652.3 MB | 8.789 s / 10,068.8 MB |
| 8 | 1 | polars-bio → snputils | 0.881 s / 2,661.7 MB | 8.593 s / 10,069.2 MB |
| 8 | 2 | snputils → polars-bio | 0.885 s / 2,662.9 MB | 8.718 s / 10,066.8 MB |
| 8 | 3 | polars-bio → snputils | 0.889 s / 2,662.4 MB | 8.737 s / 10,069.0 MB |

Wall time includes file reading, decoding, dosage conversion, null-to-sentinel
normalization, and complete materialization; module imports and one-time reader
configuration are outside the timer. Peak RSS is process `ru_maxrss`, measured
after retaining the result.

polars-bio was built with `maturin develop --release --locked` and
`RUSTFLAGS="-C target-cpu=native -C link-arg=-undefined -C
link-arg=dynamic_lookup"`. The latter two flags provide macOS Python-extension
linkage; `-C target-cpu=native` is the CPU optimization flag. The build profile
and full flags are stored in every machine-readable result.

## Inputs and exact revisions

| Item | Value |
|---|---|
| BCF | `ALL.chr22.phased.bcf`, 135,128,073 bytes (128.87 MiB) |
| BCF SHA-256 | `b61c6aaa746416306a01b3aa92db23b5e1f4faf7296a114ed32d8e64a400a250` |
| Source VCF SHA-256 | `b428192af4f02507585c3775e59251974c71a968daa895a9a47acb337140614c` |
| datafusion-bio-formats PR head | [`952ef22`](https://github.com/biodatageeks/datafusion-bio-formats/commit/952ef22271085847374e97f9ca1d4d344d122ddb) |
| polars-bio feature branch commit | [`3d85916`](https://github.com/biodatageeks/polars-bio/commit/3d85916c85c9f74710ce707efa7696d589728c37) |
| snputils commit | [`bdb1a56`](https://github.com/AI-sandbox/snputils/commit/bdb1a56b52a6b16210d60e347d33d023dc98352f) |
| polars-bio / snputils | 0.33.1 / 1.1.1.dev17+gbdb1a56b5 |
| Python / NumPy | 3.12.9 / 2.5.2 |
| Polars / PyArrow | 1.42.1 / 24.0.0 |
| polars-bio `t` sweep | 1, 2, 4, 8 target partitions and thread caps |
| snputils BCF parallelism | serial; no reader thread-count option |
| Host | MacBook Pro, Apple M3 Max (16 CPU cores), 64 GiB RAM, macOS 15.6 arm64 |

## Reproduce

```bash
git clone https://github.com/biodatageeks/polars-bio.git
git -C polars-bio checkout 3d85916c85c9f74710ce707efa7696d589728c37

git clone https://github.com/biodatageeks/bioformats-benchmark.git
cd bioformats-benchmark
git checkout feat/bcf-format

POLARS_BIO_SOURCE="$(cd ../polars-bio && pwd)" \
POLARS_BIO_RUSTFLAGS='-C target-cpu=native -C link-arg=-undefined -C link-arg=dynamic_lookup' \
bash setup.sh

for t in 1 2 4 8; do
  POLARS_BIO_REF=3d85916c85c9f74710ce707efa7696d589728c37 \
  DATAFUSION_BIO_FORMATS_REF=952ef22271085847374e97f9ca1d4d344d122ddb \
  POLARS_BIO_BUILD_PROFILE=release \
  POLARS_BIO_RUSTFLAGS='-C target-cpu=native -C link-arg=-undefined -C link-arg=dynamic_lookup' \
  .venv/bin/python run_bcf_benchmarks.py \
    --runs 3 --threads "$t" --output "results/bcf_benchmark_t${t}.json"
done
```

The orchestrator refuses unexpected fixture dimensions, runs the full
equivalence gate by default, alternates reader order, records exact package and
hardware metadata, and writes one machine-readable payload per sweep point.
