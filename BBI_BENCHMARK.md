# BigWig/BigBed partition scalability

This report measures the block-aware `datafusion-bio-formats` issue 238
candidate from one through eight source partitions. It separates BBI scan and
decode scalability from the downstream cost of aggregating or retaining the
result in Polars.

## Result

The BBI provider scales close to linearly when every column is streamed and
consumed as Arrow batches: BigWig reaches **6.70x** speedup at `t=8` and BigBed
reaches **7.45x**. The source partitions are balanced by compressed block size,
and all 320 fresh-process samples produced matching content fingerprints.

Literal all-column Polars collection has a different curve. BigWig improves
from 4.0892 s at `t=1` to 1.7311 s at `t=2`, then rises to 1.9672 s at `t=8`.
BigBed is small enough to reach 4.33x at `t=7` before fixed overhead dominates.
This gap is downstream of the BBI reader: BigWig's Arrow batch count stays
essentially constant (12,125 at `t=1`, 12,131 at `t=8`), while the retained
Polars DataFrame grows from 12,125 to 96,988 chunks and uses about 4.1 GiB RSS.

Each table cell is the median wall time from five fresh processes, followed by
speedup relative to the same workload at `t=1`. Lower time and higher speedup
are better.

### BigWig

The fixture contains 98,391,029 rows and expands to a 1,542 MiB Polars
DataFrame when all four columns are retained.

| t | Arrow stream all | Polars count | Polars aggregate all | Polars collect all |
|--:|--:|--:|--:|--:|
| 1 | 2.7302 s (1.00x) | 2.9591 s (1.00x) | 3.8259 s (1.00x) | 4.0892 s (1.00x) |
| 2 | 1.4389 s (1.90x) | 1.3442 s (2.20x) | 1.5425 s (2.48x) | 1.7311 s (2.36x) |
| 3 | 0.9504 s (2.87x) | 1.0986 s (2.69x) | 1.5153 s (2.52x) | 1.7633 s (2.32x) |
| 4 | 0.7650 s (3.57x) | 1.2146 s (2.44x) | 1.5854 s (2.41x) | 1.8270 s (2.24x) |
| 5 | 0.5887 s (4.64x) | 1.2402 s (2.39x) | 1.6084 s (2.38x) | 1.8651 s (2.19x) |
| 6 | 0.5057 s (5.40x) | 1.2554 s (2.36x) | 1.6369 s (2.34x) | 1.8782 s (2.18x) |
| 7 | 0.4505 s (6.06x) | 1.2579 s (2.35x) | 1.6647 s (2.30x) | 1.9280 s (2.12x) |
| 8 | 0.4073 s (6.70x) | 1.2696 s (2.33x) | 1.7246 s (2.22x) | 1.9672 s (2.08x) |

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
| 1 | 0.0824 s (1.00x) | 0.0722 s (1.00x) | 0.0823 s (1.00x) | 0.0830 s (1.00x) |
| 2 | 0.0377 s (2.19x) | 0.0368 s (1.96x) | 0.0405 s (2.03x) | 0.0415 s (2.00x) |
| 3 | 0.0259 s (3.18x) | 0.0269 s (2.68x) | 0.0297 s (2.77x) | 0.0308 s (2.70x) |
| 4 | 0.0203 s (4.06x) | 0.0213 s (3.38x) | 0.0236 s (3.49x) | 0.0246 s (3.37x) |
| 5 | 0.0164 s (5.01x) | 0.0176 s (4.10x) | 0.0196 s (4.19x) | 0.0209 s (3.98x) |
| 6 | 0.0143 s (5.77x) | 0.0159 s (4.54x) | 0.0182 s (4.53x) | 0.0193 s (4.30x) |
| 7 | 0.0123 s (6.69x) | 0.0145 s (4.96x) | 0.0183 s (4.51x) | 0.0191 s (4.33x) |
| 8 | 0.0111 s (7.45x) | 0.0138 s (5.21x) | 0.0192 s (4.28x) | 0.0202 s (4.11x) |

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

| format | serial compressed bytes | t=8 bytes per partition | coefficient of variation | maximum / mean |
|:--|--:|:--|--:|--:|
| BigWig | 454,982,217 | 56,684,018–56,987,905 | 0.15% | 1.002 |
| BigBed | 10,164,678 | 1,267,344–1,294,125 | 0.58% | 1.012 |

A compressed block that crosses an ownership boundary can be conservatively
counted by both independent readers, so the per-partition estimates may sum to
slightly more than the serial total. The small balance variation rules out one
slow shard as the cause of the Polars plateau.

## Correctness and memory

Every workload produced the same row count and column set at every `t`.
All-column aggregation additionally checked coordinate sums, chromosome byte
counts, the BigBed trailing-field byte count, and the BigWig signal sum (with a
small tolerance for floating-point aggregation order). The runner refuses to
write a result file if any fingerprint differs or if the physical partition
count does not match the requested value.

Median peak RSS at `t=1` and `t=8` was:

| format / workload | t=1 | t=8 |
|:--|--:|--:|
| BigWig Arrow stream all | 186.7 MiB | 213.4 MiB |
| BigWig Polars count | 213.9 MiB | 236.6 MiB |
| BigWig Polars aggregate all | 214.9 MiB | 253.5 MiB |
| BigWig Polars collect all | 4,069.8 MiB | 4,131.1 MiB |
| BigBed Arrow stream all | 179.8 MiB | 222.3 MiB |
| BigBed Polars count | 193.4 MiB | 234.4 MiB |
| BigBed Polars aggregate all | 195.8 MiB | 261.5 MiB |
| BigBed Polars collect all | 275.2 MiB | 313.3 MiB |

## Method

- Machine: Apple arm64, macOS 15.7.9, 16 physical/logical CPUs, 64 GiB RAM.
- Python 3.11.13, polars-bio 0.34.0 at `f32af94`, Polars 1.40.1, and PyArrow
  24.0.0.
- Candidate: `datafusion-bio-formats` `62d1bcc` and BigTools `17e425e`.
- Every timing runs in a fresh process with `POLARS_MAX_THREADS`,
  `RAYON_NUM_THREADS`, and DataFusion `target_partitions` set to the same `t`.
- Combination order rotates and reverses between rounds to reduce cache and
  thermal bias. Ambient system CPU is checked before each child.
- Timed scope includes lazy scan construction, BBI header/index access,
  decoding, and the workload-specific Arrow drain, Polars aggregation, or full
  DataFrame materialization. Imports and thread-pool configuration are excluded.

The checksum-pinned inputs are:

- BigWig: `GSM7256643_ENCFF713VEX_fold_change_over_control_GRCh38.bigWig`,
  573,021,756 bytes, SHA-256
  `dffcf1a854895d0d91b2b1250db72dc3572ee97c2ef936423735a74c9744b04e`.
- BigBed: `ENCFF001JBR.bigBed`, 16,438,476 bytes, SHA-256
  `b36b6b0886e25876ad06e3845a1b68f8f11b7932c23285c9c5f6301a918bc733`.

The complete five-round data, raw samples, correctness verification, physical
partition diagnostics, and build metadata are tracked in
`results/bbi_scaling_full_scan.json`. The historical release and earlier
candidate sweeps remain available for before/after comparisons. Reproduce the
run with `run_bbi_benchmarks.py` and plot it with `generate_bbi_figures.py`.
