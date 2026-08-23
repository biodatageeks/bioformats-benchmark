# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.38x** speedup at `t=8` and BigBed
reaches **6.79x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.1911 s at `t=1` to a best 1.6573 s at `t=3`, then rises to 1.8780 s at
`t=8`. BigBed is small enough to reach 4.70x at `t=6` before fixed overhead
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
| 1 | 2.8304 s (1.00x) | 3.1864 s (1.00x) | 3.9979 s (1.00x) | 4.1911 s (1.00x) |
| 2 | 1.4546 s (1.95x) | 1.3441 s (2.37x) | 1.5562 s (2.57x) | 1.6591 s (2.53x) |
| 3 | 0.9664 s (2.93x) | 1.1451 s (2.78x) | 1.6190 s (2.47x) | 1.6573 s (2.53x) |
| 4 | 0.7477 s (3.79x) | 1.2515 s (2.55x) | 1.7718 s (2.26x) | 1.7946 s (2.34x) |
| 5 | 0.6006 s (4.71x) | 1.2488 s (2.55x) | 1.6660 s (2.40x) | 1.7905 s (2.34x) |
| 6 | 0.4976 s (5.69x) | 1.2750 s (2.50x) | 1.7198 s (2.32x) | 1.8375 s (2.28x) |
| 7 | 0.4329 s (6.54x) | 1.2743 s (2.50x) | 1.7359 s (2.30x) | 1.8352 s (2.28x) |
| 8 | 0.3835 s (7.38x) | 1.2862 s (2.48x) | 1.7867 s (2.24x) | 1.8780 s (2.23x) |

`polars_count` executes `pl.len()` end to end through the Polars plugin path; it
is not a direct DataFusion `count(*)` control. The harness does not introspect
the exact projection in that timed plugin plan, so the whole-file scalability
conclusion relies on the explicitly all-column aggregation and collection
curves. Aggregation has its best median at `t=2`, while collection's `t=2` and
`t=3` medians differ by less than 2 ms; after that, further source
speedup is hidden by Polars-side streaming aggregation, chunk bookkeeping, and
materialization. Several `t=2` Polars ratios are modestly superlinear relative
to their `t=1` medians. Those points reflect pipeline overlap between parallel
decode and downstream aggregation/materialization, plus a comparatively slow
single-partition reference; they are not evidence that the reader itself does
more than linear work. The efficiency figure marks 100% as a reference line.

### BigBed

The fixture contains 602,461 rows. Each fresh process performs ten scans and
reports the per-scan time because a single scan is too short for stable timing.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 0.0725 s (1.00x) | 0.0725 s (1.00x) | 0.0834 s (1.00x) | 0.0830 s (1.00x) |
| 2 | 0.0373 s (1.94x) | 0.0359 s (2.02x) | 0.0398 s (2.10x) | 0.0399 s (2.08x) |
| 3 | 0.0255 s (2.84x) | 0.0256 s (2.83x) | 0.0285 s (2.93x) | 0.0284 s (2.92x) |
| 4 | 0.0198 s (3.67x) | 0.0201 s (3.61x) | 0.0223 s (3.74x) | 0.0223 s (3.73x) |
| 5 | 0.0160 s (4.52x) | 0.0169 s (4.28x) | 0.0189 s (4.41x) | 0.0189 s (4.40x) |
| 6 | 0.0138 s (5.27x) | 0.0152 s (4.76x) | 0.0180 s (4.63x) | 0.0177 s (4.70x) |
| 7 | 0.0120 s (6.02x) | 0.0140 s (5.17x) | 0.0184 s (4.53x) | 0.0184 s (4.52x) |
| 8 | 0.0107 s (6.79x) | 0.0138 s (5.25x) | 0.0193 s (4.33x) | 0.0191 s (4.35x) |

At `t=8`, the source scan is only 11 ms. Independent file opens, provider
setup, task scheduling, Polars conversion, and final materialization therefore
represent a large fraction of end-to-end time.

### Dispersion and admitted load

All 320 launch windows passed the declared CPU admission rule: the recorded
three-observation maxima range from 0.0% to 20.0%, with a 10.8% median. No raw
sample was discarded. The tables below show sample standard deviation divided
by the median for each five-process cell, making the retained variance explicit.

#### BigWig relative timing dispersion (sample stdev / median)

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 1.2% | 3.2% | 1.6% | 3.2% |
| 2 | 0.6% | 0.5% | 9.6% | 1.1% |
| 3 | 1.1% | 4.2% | 9.2% | 8.3% |
| 4 | 0.6% | 7.2% | 6.3% | 5.9% |
| 5 | 1.5% | 1.9% | 3.1% | 2.5% |
| 6 | 1.3% | 7.1% | 5.7% | 2.7% |
| 7 | 1.2% | 3.9% | 3.6% | 6.8% |
| 8 | 0.7% | 8.8% | 5.9% | 4.9% |

#### BigBed relative timing dispersion (sample stdev / median)

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.1% | 2.3% | 1.2% | 0.9% |
| 2 | 0.9% | 0.4% | 0.6% | 0.9% |
| 3 | 1.0% | 0.6% | 0.4% | 0.2% |
| 4 | 0.8% | 0.3% | 0.3% | 0.6% |
| 5 | 1.0% | 0.2% | 2.1% | 0.5% |
| 6 | 0.6% | 1.3% | 0.9% | 3.3% |
| 7 | 0.6% | 1.2% | 1.8% | 1.3% |
| 8 | 0.6% | 1.4% | 7.9% | 7.3% |

Sixty of 64 cells stay below 8%. The four higher-dispersion cells are BigWig
aggregate `t=2` and `t=3` (9.6% and 9.2%), collect `t=3` (8.3%), and count
`t=8` (8.8%). The last retains one 1.5412 s observation beside four
1.2822–1.3043 s samples. No observation was discarded, and the raw samples and
absolute standard deviations remain in the JSON, so adjacent small differences
should be interpreted with this dispersion.

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
| BigWig Arrow stream all | 181.1 MiB | 215.4 MiB |
| BigWig Polars count | 205.1 MiB | 234.5 MiB |
| BigWig Polars aggregate all | 204.7 MiB | 247.5 MiB |
| BigWig Polars collect all | 4,063.2 MiB | 4,125.8 MiB |
| BigBed Arrow stream all | 179.4 MiB | 222.0 MiB |
| BigBed Polars count | 191.4 MiB | 231.3 MiB |
| BigBed Polars aggregate all | 193.3 MiB | 256.7 MiB |
| BigBed Polars collect all | 246.0 MiB | 302.2 MiB |

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
- Candidate mode requires one index-derived data-byte estimate per advertised
  partition. Legacy serial comparison mode permits the diagnostic to be absent
  because the clean v1.10.0 provider predates it, but still requires one source
  partition at every requested thread count.
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
