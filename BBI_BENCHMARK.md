# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.55x** speedup at `t=8` and BigBed
reaches **6.81x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.3867 s at `t=1` to a best 1.7843 s at `t=2`, then rises to 2.1417 s at
`t=8`. BigBed is small enough to reach 4.53x at `t=6` before fixed overhead
dominates.
This gap is downstream of the BBI reader: BigWig's Arrow batch count stays
essentially constant (12,125 at `t=1`, 12,127 at `t=8`), while the retained
Polars DataFrame grows from 12,125 to 96,970 chunks and uses about 4.0 GiB RSS.

Each table cell is the median wall time from five fresh processes, followed by
speedup relative to the same workload at `t=1`. Lower time and higher speedup
are better.

### BigWig

The fixture contains 98,391,029 rows and expands to a 1,542 MiB Polars
DataFrame when all four columns are retained.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.8764 s (1.00x) | 3.1476 s (1.00x) | 4.0114 s (1.00x) | 4.3867 s (1.00x) |
| 2 | 1.4602 s (1.97x) | 1.3522 s (2.33x) | 1.5578 s (2.58x) | 1.7843 s (2.46x) |
| 3 | 0.9738 s (2.95x) | 1.1292 s (2.79x) | 1.5869 s (2.53x) | 1.9668 s (2.23x) |
| 4 | 0.7517 s (3.83x) | 1.2617 s (2.49x) | 1.6648 s (2.41x) | 1.9466 s (2.25x) |
| 5 | 0.5969 s (4.82x) | 1.2586 s (2.50x) | 1.7225 s (2.33x) | 1.9416 s (2.26x) |
| 6 | 0.5040 s (5.71x) | 1.2810 s (2.46x) | 1.7099 s (2.35x) | 2.1441 s (2.05x) |
| 7 | 0.4344 s (6.62x) | 1.2978 s (2.43x) | 1.8091 s (2.22x) | 1.9767 s (2.22x) |
| 8 | 0.3810 s (7.55x) | 1.3036 s (2.41x) | 1.8699 s (2.15x) | 2.1417 s (2.05x) |

`polars_count` is not an empty-projection `count(*)` shortcut. Polars requests
the first public column (`chrom`) for `pl.len()`, so the provider still reads
and decodes data. Its best median is at `t=3`. The all-column aggregation and
collection curves show that further source speedup is hidden by Polars-side
streaming aggregation, chunk bookkeeping, and materialization.

### BigBed

The fixture contains 602,461 rows. Each fresh process performs ten scans and
reports the per-scan time because a single scan is too short for stable timing.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 0.0735 s (1.00x) | 0.0713 s (1.00x) | 0.0847 s (1.00x) | 0.0854 s (1.00x) |
| 2 | 0.0376 s (1.95x) | 0.0360 s (1.98x) | 0.0398 s (2.13x) | 0.0412 s (2.08x) |
| 3 | 0.0258 s (2.85x) | 0.0257 s (2.78x) | 0.0285 s (2.97x) | 0.0296 s (2.89x) |
| 4 | 0.0201 s (3.66x) | 0.0200 s (3.56x) | 0.0226 s (3.75x) | 0.0237 s (3.61x) |
| 5 | 0.0162 s (4.54x) | 0.0171 s (4.17x) | 0.0192 s (4.42x) | 0.0203 s (4.21x) |
| 6 | 0.0139 s (5.28x) | 0.0152 s (4.69x) | 0.0185 s (4.58x) | 0.0189 s (4.53x) |
| 7 | 0.0121 s (6.06x) | 0.0139 s (5.12x) | 0.0186 s (4.55x) | 0.0197 s (4.34x) |
| 8 | 0.0108 s (6.81x) | 0.0143 s (5.00x) | 0.0200 s (4.24x) | 0.0208 s (4.11x) |

At `t=8`, the source scan is only 11 ms. Independent file opens, provider
setup, task scheduling, Polars conversion, and final materialization therefore
represent a large fraction of end-to-end time.

## What the four workloads measure

- `arrow_stream_all` requests and drains every Arrow column without retaining
  the whole file. It isolates provider scan/decode plus the Python Arrow stream.
- `polars_count` runs `pl.len()` through polars-bio. It currently requests
  `chrom`; it is not an empty-projection DataFusion count optimization.
- `polars_aggregate_all` requests every column and reduces row count,
  chromosome bytes, coordinates, and payload values to a fingerprint.
- `polars_collect_all` literally materializes every row and column in a Polars
  DataFrame and records retained chunk count, estimated size, and peak RSS.

The Arrow and Polars paths are therefore not equivalent timers. The former
ends after batches are consumed; the latter includes Polars' streaming bridge,
aggregation or DataFrame construction, chunk retention, and final output.

## Partition balance

Provider setup reads the primary cir-tree leaves and balances their compressed
sizes across source partitions. At `t=8` the assignments are tightly balanced:

| format | serial data bytes | t=8 bytes per partition | coefficient of variation | maximum / mean |
|:--|--:|:--|--:|--:|
| BigWig | 454,982,217 | 56,875,651–56,886,353 | 0.006% | 1.0001 |
| BigBed | 10,164,678 | 1,266,538–1,284,303 | 0.406% | 1.0048 |

A compressed block that crosses an ownership boundary can be conservatively
counted by both independent readers, so the per-partition estimates may sum to
slightly more than the serial total. The small balance variation rules out one
slow shard as the cause of the Polars plateau.

## Correctness and memory

Every timed sample is followed by an untimed all-column replay of that
workload's data path. Arrow batches are hashed through the direct DataFusion
stream, a collected DataFrame is hashed after literal materialization, and the
count and aggregate paths validate through their Polars scan. Two independently
seeded, order-independent row-hash sums verify complete row content across
workloads and every `t`; row count, coordinate sums, chromosome byte counts, the
BigBed trailing-field byte count, and the BigWig signal sum are also checked.
Fields shared by the timed and replay fingerprints are cross-checked directly:
all aggregate fields for the aggregate workload, and row count for count, Arrow
streaming, and collection. Candidate mode refuses to write a result file if any
digest differs or if the physical partition count does not match the requested
value.

Median peak RSS at `t=1` and `t=8` was:

| format / workload | t=1 | t=8 |
|:--|--:|--:|
| BigWig Arrow stream all | 185.2 MiB | 214.4 MiB |
| BigWig Polars count | 203.2 MiB | 233.5 MiB |
| BigWig Polars aggregate all | 205.5 MiB | 251.1 MiB |
| BigWig Polars collect all | 4,063.1 MiB | 4,134.3 MiB |
| BigBed Arrow stream all | 178.7 MiB | 222.3 MiB |
| BigBed Polars count | 192.4 MiB | 232.7 MiB |
| BigBed Polars aggregate all | 194.1 MiB | 257.2 MiB |
| BigBed Polars collect all | 246.1 MiB | 303.1 MiB |

## Method

- Machine: Apple arm64, macOS 15.7.9, 16 physical/logical CPUs, 64 GiB RAM.
- The documented `.venv-bbi/bin/python` environment: Python 3.11.13, Polars
  1.40.1, and PyArrow 24.0.0.
- polars-bio 0.34.0 at `f32af94` plus the tracked dependency-only
  [`polars_bio_issue_443.patch`](benchmarks/polars_bio_issue_443.patch), SHA-256
  `ccb894252bae81ad636d6276a14bcdadcdb0156d8b3c97f957d3e63235851fda`.
- Candidate: `datafusion-bio-formats` `d0a23b5` and BigTools `0d7a572`.
- Every timing runs in a fresh process with `POLARS_MAX_THREADS`,
  `RAYON_NUM_THREADS`, `TOKIO_WORKER_THREADS`, and DataFusion
  `target_partitions` set to the same `t`.
- Round starts are evenly spaced over the full combination list and alternate
  direction to reduce cache and thermal bias. Ambient system CPU is recorded
  before each child; this run did not configure the optional abort threshold.
- Timed scope includes lazy scan construction, BBI header/index access,
  decoding, and the workload-specific Arrow drain, Polars aggregation, or full
  DataFrame materialization. Imports, thread-pool configuration, physical-plan
  inspection, and the untimed content replay are excluded.

The checksum-pinned inputs are:

- BigWig: `GSM7256643_ENCFF713VEX_fold_change_over_control_GRCh38.bigWig`,
  573,021,756 bytes, SHA-256
  `dffcf1a854895d0d91b2b1250db72dc3572ee97c2ef936423735a74c9744b04e`.
- BigBed: `ENCFF001JBR.bigBed`, 16,438,476 bytes, SHA-256
  `b36b6b0886e25876ad06e3845a1b68f8f11b7932c23285c9c5f6301a918bc733`.

Create the pinned BBI environment with `setup_bbi_benchmark.sh`; it accepts only
the declared `f32af94` source plus the exact tracked patch and exports release
build settings inside the build. Supply those same variables to
`run_bbi_benchmarks.py`, as shown in the README. The complete five-round data,
raw samples, correctness verification, physical partition diagnostics, source
patch provenance, build metadata, and hashes of all three harness modules are
tracked in `results/bbi_scaling_full_scan.json`. Plot it with `generate_bbi_figures.py`,
which rejects fixture, content, runtime, build-setting, iteration-protocol,
hardware, partition-set, or schema incompatibilities.
