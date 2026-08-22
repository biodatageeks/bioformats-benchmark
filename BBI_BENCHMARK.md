# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.21x** speedup at `t=8` and BigBed
reaches **6.64x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.1116 s at `t=1` to a best 1.6507 s at `t=3`, then rises to 1.9203 s at
`t=8`. BigBed is small enough to reach 4.37x at `t=6` before fixed overhead
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
| 1 | 2.7628 s (1.00x) | 3.0088 s (1.00x) | 3.8665 s (1.00x) | 4.1116 s (1.00x) |
| 2 | 1.4472 s (1.91x) | 1.3464 s (2.23x) | 1.5365 s (2.52x) | 1.7072 s (2.41x) |
| 3 | 0.9460 s (2.92x) | 1.0374 s (2.90x) | 1.4739 s (2.62x) | 1.6507 s (2.49x) |
| 4 | 0.7548 s (3.66x) | 1.2030 s (2.50x) | 1.5784 s (2.45x) | 1.8029 s (2.28x) |
| 5 | 0.5794 s (4.77x) | 1.2301 s (2.45x) | 1.5898 s (2.43x) | 1.8135 s (2.27x) |
| 6 | 0.4895 s (5.64x) | 1.2435 s (2.42x) | 1.6086 s (2.40x) | 1.8414 s (2.23x) |
| 7 | 0.4264 s (6.48x) | 1.2475 s (2.41x) | 1.6457 s (2.35x) | 1.8854 s (2.18x) |
| 8 | 0.3830 s (7.21x) | 1.2473 s (2.41x) | 1.6844 s (2.30x) | 1.9203 s (2.14x) |

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
| 1 | 0.0702 s (1.00x) | 0.0695 s (1.00x) | 0.0805 s (1.00x) | 0.0802 s (1.00x) |
| 2 | 0.0365 s (1.92x) | 0.0349 s (1.99x) | 0.0389 s (2.07x) | 0.0399 s (2.01x) |
| 3 | 0.0250 s (2.81x) | 0.0253 s (2.74x) | 0.0282 s (2.86x) | 0.0291 s (2.75x) |
| 4 | 0.0195 s (3.61x) | 0.0198 s (3.50x) | 0.0222 s (3.63x) | 0.0232 s (3.45x) |
| 5 | 0.0158 s (4.45x) | 0.0165 s (4.22x) | 0.0183 s (4.39x) | 0.0197 s (4.07x) |
| 6 | 0.0137 s (5.13x) | 0.0150 s (4.63x) | 0.0176 s (4.57x) | 0.0183 s (4.37x) |
| 7 | 0.0118 s (5.97x) | 0.0137 s (5.08x) | 0.0174 s (4.63x) | 0.0184 s (4.36x) |
| 8 | 0.0106 s (6.64x) | 0.0131 s (5.29x) | 0.0187 s (4.30x) | 0.0194 s (4.13x) |

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
The timed result is cross-checked on every field it exposes. Candidate mode
refuses to write a result file if any digest differs or if the physical
partition count does not match the requested value.

Median peak RSS at `t=1` and `t=8` was:

| format / workload | t=1 | t=8 |
|:--|--:|--:|
| BigWig Arrow stream all | 178.8 MiB | 211.8 MiB |
| BigWig Polars count | 201.2 MiB | 230.8 MiB |
| BigWig Polars aggregate all | 201.2 MiB | 245.3 MiB |
| BigWig Polars collect all | 4,060.9 MiB | 4,123.7 MiB |
| BigBed Arrow stream all | 176.0 MiB | 219.8 MiB |
| BigBed Polars count | 188.1 MiB | 228.6 MiB |
| BigBed Polars aggregate all | 189.6 MiB | 254.5 MiB |
| BigBed Polars collect all | 244.6 MiB | 299.7 MiB |

## Method

- Machine: Apple arm64, macOS 15.7.9, 16 physical/logical CPUs, 64 GiB RAM.
- Python 3.11.13, Polars 1.40.1, and PyArrow 24.0.0.
- polars-bio 0.34.0 at `f32af94` plus the tracked dependency-only
  [`polars_bio_issue_443.patch`](benchmarks/polars_bio_issue_443.patch), SHA-256
  `ccb894252bae81ad636d6276a14bcdadcdb0156d8b3c97f957d3e63235851fda`.
- Candidate: `datafusion-bio-formats` `d0a23b5` and BigTools `0d7a572`.
- Every timing runs in a fresh process with `POLARS_MAX_THREADS`,
  `RAYON_NUM_THREADS`, `TOKIO_WORKER_THREADS`, and DataFusion
  `target_partitions` set to the same `t`.
- Combination order rotates and reverses between rounds to reduce cache and
  thermal bias. Ambient system CPU is recorded before each child; this run did
  not configure the optional abort threshold.
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
patch provenance, and build metadata are tracked in
`results/bbi_scaling_full_scan.json`. Plot it with `generate_bbi_figures.py`,
which rejects fixture, content, runtime, hardware, partition-set, or schema
incompatibilities.
