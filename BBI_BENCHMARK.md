# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **7.29x** speedup at `t=8` and BigBed
reaches **6.67x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.1212 s at `t=1` to a best 1.6390 s at `t=3`, then rises to 1.9161 s at
`t=8`. BigBed is small enough to reach 4.35x at `t=7` before fixed overhead
dominates.
This gap is downstream of the BBI reader: BigWig's Arrow batch count stays
essentially constant (12,125 at `t=1`, 12,127 at `t=8`), while the retained
Polars DataFrame grows from 12,125 to 96,970 chunks and uses about 4.1 GiB RSS.

Each table cell is the median wall time from five fresh processes, followed by
speedup relative to the same workload at `t=1`. Lower time and higher speedup
are better.

### BigWig

The fixture contains 98,391,029 rows and expands to a 1,542 MiB Polars
DataFrame when all four columns are retained.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.7803 s (1.00x) | 3.0190 s (1.00x) | 3.8712 s (1.00x) | 4.1212 s (1.00x) |
| 2 | 1.4420 s (1.93x) | 1.3420 s (2.25x) | 1.5368 s (2.52x) | 1.6936 s (2.43x) |
| 3 | 0.9485 s (2.93x) | 1.0432 s (2.89x) | 1.4860 s (2.61x) | 1.6390 s (2.51x) |
| 4 | 0.7565 s (3.68x) | 1.2068 s (2.50x) | 1.5676 s (2.47x) | 1.7780 s (2.32x) |
| 5 | 0.5808 s (4.79x) | 1.2305 s (2.45x) | 1.5919 s (2.43x) | 1.8022 s (2.29x) |
| 6 | 0.4929 s (5.64x) | 1.2377 s (2.44x) | 1.6080 s (2.41x) | 1.8290 s (2.25x) |
| 7 | 0.4265 s (6.52x) | 1.2447 s (2.43x) | 1.6531 s (2.34x) | 1.8497 s (2.23x) |
| 8 | 0.3815 s (7.29x) | 1.2414 s (2.43x) | 1.6906 s (2.29x) | 1.9161 s (2.15x) |

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
| 1 | 0.0707 s (1.00x) | 0.0695 s (1.00x) | 0.0809 s (1.00x) | 0.0805 s (1.00x) |
| 2 | 0.0367 s (1.93x) | 0.0351 s (1.98x) | 0.0389 s (2.08x) | 0.0398 s (2.02x) |
| 3 | 0.0250 s (2.83x) | 0.0254 s (2.74x) | 0.0283 s (2.86x) | 0.0292 s (2.76x) |
| 4 | 0.0195 s (3.63x) | 0.0199 s (3.49x) | 0.0222 s (3.65x) | 0.0233 s (3.46x) |
| 5 | 0.0160 s (4.42x) | 0.0166 s (4.19x) | 0.0185 s (4.37x) | 0.0196 s (4.11x) |
| 6 | 0.0137 s (5.16x) | 0.0150 s (4.64x) | 0.0177 s (4.56x) | 0.0185 s (4.34x) |
| 7 | 0.0119 s (5.96x) | 0.0138 s (5.05x) | 0.0176 s (4.60x) | 0.0185 s (4.35x) |
| 8 | 0.0106 s (6.67x) | 0.0133 s (5.24x) | 0.0187 s (4.32x) | 0.0198 s (4.06x) |

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

Every timed sample is followed by an untimed all-column validation scan. Two
independently seeded, order-independent row-hash sums verify complete row
content across workloads and every `t`; row count, coordinate sums, chromosome
byte counts, the BigBed trailing-field byte count, and the BigWig signal sum are
also checked. Candidate mode refuses to write a result file if any digest
differs or if the physical partition count does not match the requested value.

Median peak RSS at `t=1` and `t=8` was:

| format / workload | t=1 | t=8 |
|:--|--:|--:|
| BigWig Arrow stream all | 181.1 MiB | 211.7 MiB |
| BigWig Polars count | 201.1 MiB | 229.9 MiB |
| BigWig Polars aggregate all | 202.0 MiB | 245.7 MiB |
| BigWig Polars collect all | 4,061.3 MiB | 4,124.9 MiB |
| BigBed Arrow stream all | 176.8 MiB | 221.1 MiB |
| BigBed Polars count | 188.0 MiB | 229.2 MiB |
| BigBed Polars aggregate all | 190.3 MiB | 254.0 MiB |
| BigBed Polars collect all | 245.0 MiB | 301.4 MiB |

## Method

- Machine: Apple arm64, macOS 15.7.9, 16 physical/logical CPUs, 64 GiB RAM.
- Python 3.11.13, polars-bio 0.34.0 at `f32af94`, Polars 1.40.1, and PyArrow
  24.0.0.
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
  inspection, and the independent content digest are excluded.

The checksum-pinned inputs are:

- BigWig: `GSM7256643_ENCFF713VEX_fold_change_over_control_GRCh38.bigWig`,
  573,021,756 bytes, SHA-256
  `dffcf1a854895d0d91b2b1250db72dc3572ee97c2ef936423735a74c9744b04e`.
- BigBed: `ENCFF001JBR.bigBed`, 16,438,476 bytes, SHA-256
  `b36b6b0886e25876ad06e3845a1b68f8f11b7932c23285c9c5f6301a918bc733`.

Create the pinned BBI environment with `setup_bbi_benchmark.sh`. The complete
five-round data, raw samples, correctness verification, physical
partition diagnostics, and build metadata are tracked in
`results/bbi_scaling_full_scan.json`. The historical release and earlier
candidate sweeps use result schema version 1 and record their generator commit;
the four-workload sweep uses schema version 2. Reproduce the run with
`run_bbi_benchmarks.py` and plot it with `generate_bbi_figures.py`, which rejects
fixture, hardware, partition-set, or schema incompatibilities.
