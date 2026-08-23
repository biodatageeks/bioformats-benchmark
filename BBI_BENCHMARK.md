# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.60x** speedup at `t=8` and BigBed
reaches **6.94x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.3909 s at `t=1` to a best 1.7686 s at `t=2`, then rises to 2.1148 s at
`t=8`. BigBed is small enough to reach 4.60x at `t=6` before fixed overhead
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
| 1 | 2.9067 s (1.00x) | 3.1981 s (1.00x) | 4.1087 s (1.00x) | 4.3909 s (1.00x) |
| 2 | 1.4745 s (1.97x) | 1.3422 s (2.38x) | 1.5583 s (2.64x) | 1.7686 s (2.48x) |
| 3 | 0.9764 s (2.98x) | 1.1715 s (2.73x) | 1.6401 s (2.51x) | 1.9640 s (2.24x) |
| 4 | 0.7467 s (3.89x) | 1.2453 s (2.57x) | 1.7172 s (2.39x) | 1.9859 s (2.21x) |
| 5 | 0.5999 s (4.85x) | 1.3304 s (2.40x) | 1.7388 s (2.36x) | 2.0958 s (2.10x) |
| 6 | 0.5051 s (5.75x) | 1.3581 s (2.35x) | 1.7347 s (2.37x) | 2.0923 s (2.10x) |
| 7 | 0.4355 s (6.67x) | 1.3730 s (2.33x) | 1.8384 s (2.23x) | 2.0947 s (2.10x) |
| 8 | 0.3826 s (7.60x) | 1.3777 s (2.32x) | 1.8036 s (2.28x) | 2.1148 s (2.08x) |

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
| 1 | 0.0745 s (1.00x) | 0.0746 s (1.00x) | 0.0856 s (1.00x) | 0.0864 s (1.00x) |
| 2 | 0.0385 s (1.94x) | 0.0367 s (2.03x) | 0.0405 s (2.11x) | 0.0413 s (2.09x) |
| 3 | 0.0260 s (2.86x) | 0.0261 s (2.86x) | 0.0286 s (3.00x) | 0.0296 s (2.92x) |
| 4 | 0.0199 s (3.75x) | 0.0203 s (3.68x) | 0.0226 s (3.79x) | 0.0235 s (3.68x) |
| 5 | 0.0162 s (4.60x) | 0.0173 s (4.32x) | 0.0190 s (4.52x) | 0.0203 s (4.26x) |
| 6 | 0.0140 s (5.34x) | 0.0151 s (4.93x) | 0.0181 s (4.73x) | 0.0188 s (4.60x) |
| 7 | 0.0121 s (6.18x) | 0.0142 s (5.24x) | 0.0188 s (4.55x) | 0.0194 s (4.46x) |
| 8 | 0.0107 s (6.94x) | 0.0152 s (4.91x) | 0.0196 s (4.36x) | 0.0208 s (4.15x) |

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
| BigWig Arrow stream all | 182.7 MiB | 216.2 MiB |
| BigWig Polars count | 205.9 MiB | 238.2 MiB |
| BigWig Polars aggregate all | 206.8 MiB | 252.3 MiB |
| BigWig Polars collect all | 4,064.7 MiB | 4,134.2 MiB |
| BigBed Arrow stream all | 180.0 MiB | 223.9 MiB |
| BigBed Polars count | 193.2 MiB | 231.9 MiB |
| BigBed Polars aggregate all | 195.5 MiB | 256.2 MiB |
| BigBed Polars collect all | 247.2 MiB | 303.7 MiB |

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
tracked in `results/bbi_scaling_full_scan.json`. Plot it with
`generate_bbi_figures.py`, which rejects fixture, content, runtime,
build-setting, iteration-protocol, hardware, partition-set, schema, or
harness-version incompatibilities.
