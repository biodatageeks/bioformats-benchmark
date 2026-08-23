# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.42x** speedup at `t=8` and BigBed
reaches **6.86x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.4376 s at `t=1` to a best 1.7876 s at `t=2`, then rises to 2.0578 s at
`t=8`. BigBed is small enough to reach 4.50x at `t=6` before fixed overhead
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
| 1 | 2.8485 s (1.00x) | 3.1881 s (1.00x) | 4.1008 s (1.00x) | 4.4376 s (1.00x) |
| 2 | 1.4684 s (1.94x) | 1.3499 s (2.36x) | 1.5639 s (2.62x) | 1.7876 s (2.48x) |
| 3 | 0.9812 s (2.90x) | 1.2149 s (2.62x) | 1.5871 s (2.58x) | 1.8409 s (2.41x) |
| 4 | 0.7515 s (3.79x) | 1.3021 s (2.45x) | 1.7321 s (2.37x) | 1.9490 s (2.28x) |
| 5 | 0.6014 s (4.74x) | 1.2810 s (2.49x) | 1.7118 s (2.40x) | 1.9911 s (2.23x) |
| 6 | 0.5017 s (5.68x) | 1.4314 s (2.23x) | 1.7140 s (2.39x) | 1.9815 s (2.24x) |
| 7 | 0.4348 s (6.55x) | 1.3606 s (2.34x) | 1.7617 s (2.33x) | 2.0526 s (2.16x) |
| 8 | 0.3838 s (7.42x) | 1.4698 s (2.17x) | 1.7890 s (2.29x) | 2.0578 s (2.16x) |

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
| 1 | 0.0740 s (1.00x) | 0.0733 s (1.00x) | 0.0846 s (1.00x) | 0.0863 s (1.00x) |
| 2 | 0.0377 s (1.97x) | 0.0364 s (2.02x) | 0.0401 s (2.11x) | 0.0412 s (2.10x) |
| 3 | 0.0260 s (2.85x) | 0.0257 s (2.85x) | 0.0287 s (2.95x) | 0.0298 s (2.90x) |
| 4 | 0.0200 s (3.71x) | 0.0203 s (3.62x) | 0.0225 s (3.77x) | 0.0236 s (3.65x) |
| 5 | 0.0163 s (4.54x) | 0.0172 s (4.26x) | 0.0189 s (4.48x) | 0.0201 s (4.29x) |
| 6 | 0.0139 s (5.33x) | 0.0153 s (4.78x) | 0.0179 s (4.73x) | 0.0192 s (4.50x) |
| 7 | 0.0120 s (6.18x) | 0.0141 s (5.21x) | 0.0187 s (4.53x) | 0.0194 s (4.44x) |
| 8 | 0.0108 s (6.86x) | 0.0144 s (5.10x) | 0.0198 s (4.28x) | 0.0205 s (4.21x) |

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
| BigWig Arrow stream all | 182.0 MiB | 214.7 MiB |
| BigWig Polars count | 204.0 MiB | 236.2 MiB |
| BigWig Polars aggregate all | 208.2 MiB | 248.4 MiB |
| BigWig Polars collect all | 4,064.6 MiB | 4,128.8 MiB |
| BigBed Arrow stream all | 179.6 MiB | 223.6 MiB |
| BigBed Polars count | 192.9 MiB | 231.1 MiB |
| BigBed Polars aggregate all | 194.0 MiB | 257.5 MiB |
| BigBed Polars collect all | 247.1 MiB | 303.1 MiB |

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
- The runner snapshots its own, child, and shared harness hashes before
  preflight, then verifies them after preflight, after every child, and before
  writing output; a mixed-harness sweep is rejected.
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
