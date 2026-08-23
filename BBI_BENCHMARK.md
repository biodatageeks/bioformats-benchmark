# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.43x** speedup at `t=8` and BigBed
reaches **6.80x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.2067 s at `t=1` to a best 1.6315 s at `t=2`, then rises to 1.9718 s at
`t=8`. BigBed is small enough to reach 4.80x at `t=6` before fixed overhead
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
| 1 | 2.8491 s (1.00x) | 3.1379 s (1.00x) | 4.0852 s (1.00x) | 4.2067 s (1.00x) |
| 2 | 1.4595 s (1.95x) | 1.3529 s (2.32x) | 1.5667 s (2.61x) | 1.6315 s (2.58x) |
| 3 | 0.9677 s (2.94x) | 1.1040 s (2.84x) | 1.5715 s (2.60x) | 1.6559 s (2.54x) |
| 4 | 0.7480 s (3.81x) | 1.2454 s (2.52x) | 1.6702 s (2.45x) | 1.7650 s (2.38x) |
| 5 | 0.5953 s (4.79x) | 1.2752 s (2.46x) | 1.7021 s (2.40x) | 1.7970 s (2.34x) |
| 6 | 0.5029 s (5.67x) | 1.2659 s (2.48x) | 1.7092 s (2.39x) | 1.8161 s (2.32x) |
| 7 | 0.4357 s (6.54x) | 1.3048 s (2.40x) | 1.7458 s (2.34x) | 1.8826 s (2.23x) |
| 8 | 0.3832 s (7.43x) | 1.3107 s (2.39x) | 1.8025 s (2.27x) | 1.9718 s (2.13x) |

`polars_count` executes `pl.len()` end to end through the Polars plugin path; it
is not a direct DataFusion `count(*)` control. The harness does not introspect
the exact projection in that timed plugin plan, so the whole-file scalability
conclusion relies on the explicitly all-column aggregation and collection
curves. Both have their best medians at `t=2`, after which further source
speedup is hidden by Polars-side streaming aggregation, chunk bookkeeping, and
materialization.

### BigBed

The fixture contains 602,461 rows. Each fresh process performs ten scans and
reports the per-scan time because a single scan is too short for stable timing.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 0.0735 s (1.00x) | 0.0725 s (1.00x) | 0.0856 s (1.00x) | 0.0857 s (1.00x) |
| 2 | 0.0382 s (1.92x) | 0.0368 s (1.97x) | 0.0403 s (2.12x) | 0.0402 s (2.13x) |
| 3 | 0.0256 s (2.87x) | 0.0258 s (2.81x) | 0.0286 s (3.00x) | 0.0285 s (3.01x) |
| 4 | 0.0199 s (3.70x) | 0.0202 s (3.58x) | 0.0226 s (3.79x) | 0.0225 s (3.80x) |
| 5 | 0.0162 s (4.53x) | 0.0173 s (4.19x) | 0.0191 s (4.47x) | 0.0190 s (4.51x) |
| 6 | 0.0139 s (5.28x) | 0.0152 s (4.78x) | 0.0182 s (4.71x) | 0.0178 s (4.80x) |
| 7 | 0.0120 s (6.10x) | 0.0140 s (5.17x) | 0.0188 s (4.56x) | 0.0183 s (4.68x) |
| 8 | 0.0108 s (6.80x) | 0.0141 s (5.14x) | 0.0195 s (4.39x) | 0.0196 s (4.38x) |

At `t=8`, the source scan is only 11 ms. Independent file opens, provider
setup, task scheduling, Polars conversion, and final materialization therefore
represent a large fraction of end-to-end time.

### Dispersion and admitted load

All 320 launch windows passed the declared CPU admission rule: the recorded
three-observation maxima range from 0.0% to 20.0%, with a 9.9% median. No raw
sample was discarded. The tables below show sample standard deviation divided
by the median for each five-process cell, making the retained variance explicit.

#### BigWig relative timing dispersion (sample stdev / median)

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.0% | 5.4% | 2.8% | 2.1% |
| 2 | 0.4% | 0.3% | 14.5% | 1.1% |
| 3 | 1.2% | 2.8% | 3.2% | 7.2% |
| 4 | 0.5% | 1.4% | 2.6% | 2.9% |
| 5 | 1.7% | 20.7% | 2.0% | 2.4% |
| 6 | 1.2% | 15.5% | 2.7% | 0.9% |
| 7 | 6.6% | 4.2% | 3.7% | 2.3% |
| 8 | 0.9% | 4.0% | 1.8% | 2.3% |

#### BigBed relative timing dispersion (sample stdev / median)

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.4% | 3.9% | 2.7% | 2.3% |
| 2 | 1.4% | 411.7% | 0.9% | 0.7% |
| 3 | 1.1% | 0.5% | 0.6% | 0.4% |
| 4 | 0.3% | 1.5% | 0.5% | 0.6% |
| 5 | 0.8% | 1.1% | 0.9% | 1.8% |
| 6 | 0.6% | 0.8% | 0.9% | 1.6% |
| 7 | 0.4% | 0.9% | 2.3% | 1.6% |
| 8 | 53.9% | 7.5% | 1.7% | 1.9% |

Fifty-nine of 64 cells stay below 8%. BigBed count `t=2` retains one 0.3754 s
interruption beside four 0.0364–0.0371 s samples, and BigBed Arrow `t=8`
retains one 0.0238 s sample beside four 0.0107–0.0109 s samples. BigWig count
`t=5` and `t=6` and aggregate `t=2` each retain one slower sample, producing
20.7%, 15.5%, and 14.5% relative dispersion. These values do not change the
cell medians. The raw samples and absolute standard deviations remain in the
JSON, so adjacent small differences should be interpreted with this dispersion.

## What the four workloads measure

- `arrow_stream_all` requests and drains every Arrow column without retaining
  the whole file. It isolates provider scan/decode plus the Python Arrow stream.
- `polars_count` runs `pl.len()` through polars-bio. It is not a direct
  DataFusion `count(*)` control, and the exact plugin projection is not recorded.
- `polars_aggregate_all` requests every column and reduces row count,
  chromosome bytes, coordinates, and payload values to a fingerprint.
- `polars_collect_all` literally materializes every row and column in a Polars
  DataFrame and records retained chunk count, estimated size, and peak RSS after
  the elapsed timestamp. Diagnostics and DataFrame teardown are excluded.

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
digest differs or if an equivalently configured direct DataFusion source-plan
probe does not advertise the requested partition count. The probe matches the
timed Arrow construction and is a source-level proxy for the three Polars
plugin workloads; it does not introspect the exact Polars plugin plan.

Median peak RSS at `t=1` and `t=8` was:

| format / workload | t=1 | t=8 |
|:--|--:|--:|
| BigWig Arrow stream all | 180.9 MiB | 213.8 MiB |
| BigWig Polars count | 203.3 MiB | 234.0 MiB |
| BigWig Polars aggregate all | 205.7 MiB | 252.5 MiB |
| BigWig Polars collect all | 4,062.8 MiB | 4,134.1 MiB |
| BigBed Arrow stream all | 180.1 MiB | 224.1 MiB |
| BigBed Polars count | 190.8 MiB | 230.6 MiB |
| BigBed Polars aggregate all | 195.8 MiB | 255.9 MiB |
| BigBed Polars collect all | 248.2 MiB | 303.4 MiB |

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
- Input size and SHA-256 are captured before any child launches and rechecked
  after the sweep; results are rejected if either fixture changed.
- Round starts are evenly spaced over the full combination list and alternate
  direction to reduce cache and thermal bias. Before each child, three
  consecutive 200 ms aggregate-CPU observations must be at or below 20%; the
  maximum of that quiet window is recorded, and a 300-second timeout aborts the
  sweep if the machine does not settle.
- Timed scope includes lazy scan construction, BBI header/index access,
  decoding, and the workload-specific Arrow drain, Polars aggregation, or full
  DataFrame materialization. Collection diagnostics and DataFrame teardown,
  imports, thread-pool configuration, source-plan probing, and the untimed
  content replay are excluded.

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
build-setting, iteration-protocol, run-count, hardware, partition-set, schema,
or harness-version incompatibilities.
