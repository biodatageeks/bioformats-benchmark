# PGEN genotype-reader benchmark

polars-bio reads PLINK 2 filesets through `read_pgen` / `scan_pgen` for a
DataFrame and `read_pgen_matrix` for a dense NumPy matrix, backed by the
`datafusion-bio-format-pgen` provider. This benchmark builds a matrix, so it
measures `read_pgen_matrix`. This compares it against
[snputils](https://github.com/AI-sandbox/snputils) and against
[pgenlib](https://pypi.org/project/Pgenlib/), PLINK 2's own reference reader,
on the same chromosome 22 callset the BCF and BGEN benchmarks use.

**At equal core count polars-bio is the fastest of the three** — 1.48× pgenlib
on dosage, 1.28× on hardcalls, and 2.08×/1.28× snputils. pgenlib and snputils are
single-threaded, so only the one-partition polars-bio rows are like-for-like;
its multi-partition rows spend cores the others do not use — and buy
surprisingly little for them, which [Scaling](#scaling) takes apart. What is left
of the single-thread gap is one copy, described in
[Where the remaining gap is](#where-the-remaining-gap-is).

Earlier revisions of this document reported polars-bio as the slowest of the
three by a wide margin — 0.30× pgenlib on dosage and 0.09× on hardcalls. Three
provider changes and one polars-bio API closed that and then some; the history
is in [Optimization history](#optimization-history), and the two harness fixes
that also moved the figures are in
[Corrections](#corrections-to-earlier-revisions-of-this-document).

Being faster than pgenlib is a narrower claim than it sounds. pgenlib decodes
straight into the caller's array in one pass and is genuinely efficient at it;
what polars-bio has is a decoder that got faster than that pass and a
materialization path that no longer wastes one. On the parts of the job pgenlib
does best — memory, and a copy it never makes — it is still ahead.

## Two workloads, because "dosage" is overloaded

Conflating these produces a meaningless comparison, and an earlier revision of
this document did exactly that.

| Workload | Values | dtype | Source track |
|---|---|---|---|
| **dosage** | ALT dosage, genuinely fractional | `float32` | PGEN's dosage track, stored `uint16/16384` |
| **hardcall** | ALT allele count: 0, 1, 2, −9 missing | `int8` | PGEN's hardcall track |

They are different data. On a fileset that carries a real dosage track, the
same variant reads as:

```
dosages   : [ 0.125  1.0  1.875  missing ]
hardcalls : [ missing  1  missing  missing ]
```

`int8` cannot represent 0.125, which is why polars-bio's `DS` column is
`Float32` and why a narrower type is not simply available.

Naming differs across libraries and is a trap: **snputils'
`genotype_mode="dosage"` returns the hardcall workload**, as int8 counts.
pgenlib separates them properly — `read_list` for hardcalls,
`read_dosages_list` for dosages.

Each reader is measured on its own fastest native API for each workload, and
charged for any conversion it needs to reach the canonical dtype. polars-bio has
a native column for both sides — `DS` for dosages, `ALT_COUNT` for hardcalls as
`int8`, one byte per genotype — and reads them through `read_pgen_matrix`, its
dense-matrix path. snputils has no native float dosage reader, so it is charged
the int8→float32 widening for the dosage workload.

## Result

993,881 variants by 2,548 samples, 2,532,408,788 values. Medians of three
fresh-process runs, all readers interleaved in one session. Lower is better.

These figures are the second 2026-08-18 session, against provider `5f3dcf3`
and the corrected import warm-up in
[Corrections](#corrections-to-earlier-revisions-of-this-document) item 6, which
moved the snputils column by 23–40%. The
end-to-end tables below, [Scaling](#scaling) and
[Optimization history](#optimization-history) are from that run. The provider-only
probes — [Decode only](#decode-only), the scan scaling table, the stage
attribution and the copy-ceiling table — were **not** re-measured and are from
the earlier `9cccf2e` session; they are marked where they appear.

### Dosage workload — `float32`, 10.13 GB output

Single-threaded readers first; these are the comparable rows.

| Reader | Threads | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|---:|
| **polars-bio** `read_pgen_matrix` | **1** | **1.277 s** | 12,887 MB | **1.48× faster** | **2.08× faster** |
| pgenlib `read_dosages_list` | 1 | 1.884 s | 12,382 MB | 1.00× | 1.41× faster |
| snputils (int8 read + widen) | 1 | 2.651 s | 14,679 MB | 0.71× | 1.00× |
| polars-bio | 4 | 0.502 s | 12,911 MB | 3.75× faster | 5.28× faster |
| polars-bio | 8 | 0.385 s | 12,920 MB | 4.89× faster | 6.89× faster |

At one partition polars-bio is **1.48× faster than pgenlib** and 2.08× faster
than snputils, using 4% more memory doing it — see
[Where the remaining gap is](#where-the-remaining-gap-is).

The four- and eight-partition rows are included because partition parallelism is
what polars-bio offers and the others do not, but they are not like-for-like and
should not be read as one. What they *are* useful for is showing how little that
parallelism buys — see [Scaling](#scaling).

snputils has no native float dosage reader, so part of its 2.651 s is the
int8→float32 widening this workload charges it; its native int8 decode is the
0.875 s in the hardcall table below.

### Hardcall workload — `int8`, 2.53 GB output

| Reader | Threads | Time | Peak RSS | vs pgenlib | vs snputils |
|---|---:|---:|---:|---:|---:|
| **polars-bio** `read_pgen_matrix` | **1** | **0.684 s** | 5,643 MB | **1.28× faster** | **1.28× faster** |
| pgenlib `read_list` | 1 | 0.873 s | 5,137 MB | 1.00× | 1.002× |
| snputils `genotype_mode="dosage"` | 1 | 0.875 s | 5,284 MB | 0.998× | 1.00× |
| polars-bio | 4 | 0.305 s | 5,666 MB | 2.86× faster | 2.87× faster |
| polars-bio | 8 | 0.238 s | 5,676 MB | 3.67× faster | 3.68× faster |

This workload briefly regressed while the direct decoder was landing — 0.694 s to
0.759 s at one partition — because building the matrix opened the fileset twice,
once to learn its shape and once to decode, parsing the 108 MB PVAR each time.
Dosage's decode absorbed that; hardcall's did not. Holding the fileset open
across both recovered it.

pgenlib has drifted by up to 5.7% between sessions in this work, and this
session ran uniformly slower than the previous one: pgenlib +1.2% on dosage and
+5.7% on hardcalls, polars-bio +4.6% and +4.7%. Because everything moved
together, the within-session ratios are what carry meaning, and they barely
changed — 1.475× pgenlib on dosage against 1.524×, 1.276× on hardcalls against
1.265×. Margins within a session stand as measured; do not compare across
sessions to a third decimal place.

The snputils column is the exception: it moved 23% on dosage and 40% on
hardcalls because a harness bug was fixed, not because anything about the
reader changed. That is item 6 below.

polars-bio emits `ALT_COUNT` natively as `int8`, so this workload no longer
charges it a `float32` materialization and a narrowing pass, as earlier
revisions of this document did.

### Decode only

*From the `9cccf2e` session; not re-measured.* Stripping materialization from
the polars-bio side — its scan measured in Rust with no Python and no
contiguous-array consolidation:

| Field | decode |
|---|---:|
| `DS` (float32) | 1.19 s |
| `ALT_COUNT` (int8) | 0.59 s |

`datafusion/bio-format-pgen/examples/pgen_ds_profile.rs` reproduces this; the
third argument selects the field. There is no comparable pgenlib figure here:
`read_dosages_list` decodes *and* fills the caller's array in one pass, so it
has no separable decode stage to measure against. That difference is the
subject of [Where the remaining gap is](#where-the-remaining-gap-is).

### Optimization history

The provider was profiled and optimized against this benchmark in
[datafusion-bio-formats#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232),
and the materialization path in
[polars-bio#436](https://github.com/biodatageeks/polars-bio/pull/436).
Single-partition whole-chromosome, interleaved in one session:

| Change | dosage scan | dosage total |
|---|---:|---:|
| baseline | 11.2 s | 19.15 s |
| Arrow values/validity buffers instead of per-cell `append_option` | ~9.0 s | 13.95 s |
| `DS` joins the single-field fast path | 7.30 s | 9.40 s |
| table-driven `append_codes` + bulk validity | 5.00 s | 7.49 s |
| skip hardcall phase orientation for dosage | 4.13 s | 6.19 s |
| `ALT_COUNT` column, vectorized expansion, difflist buffer reuse | 2.31 s | 4.34 s |
| fuse the common-value + difflist decode | 1.19 s | 3.23 s |
| `read_pgen_matrix` — stream batches into a preallocated array | 1.19 s | 1.84 s |
| parse the `.pvar` across threads | 1.19 s | 1.67 s |
| decode into the destination, no Arrow, no copy | — | 1.35 s |
| open the fileset once instead of twice | — | 1.29 s |
| fetch and decode the input a range at a time, without copying it | — | **1.22 s** |

Medians of three interleaved runs; the eight-partition dosage row varied
0.374–0.382 s, so do not read it to three digits. **This progression is the
earlier 2026-08-18 session**, which ran about 4% faster overall than the session
the result tables above come from — the sequence is internally consistent, and
the last row's 1.22 s is the same read the current table records as 1.277 s.

**15.7× end to end.** The hardcall workload went 2.959 s → 0.653 s over the same
sequence. Note that the last row spans a session boundary — see the drift
paragraph above; isolating the provider puts that row at about 2.8% on dosage
rather than the 5% the totals show. Two of the earlier rows are worth separating
out:

- **`read_pgen_matrix`** removed a whole copy of the values and is 2.36× on its
  own, but it does not touch the scan, which is why the scan column stops moving.
- **The parallel `.pvar` parse** does not touch the scan either. Opening a
  fileset parsed 108 MB of text serially before any partition ran — 0.257 s,
  20% of a four-partition read and a fixed floor under every one. Splitting it
  across threads takes it to 0.068 s.

A third change, parallelizing the copy, does not move the one-partition figures
at all and is covered in [Scaling](#scaling).

Two lessons worth recording:

1. **A wasted iteration.** The dense decode path was optimized first, before
   checking which records `plink2 --make-pgen` actually writes — only 3.8% of
   this fixture takes the dense path, and 81% are `record_type=0x14`. Tracing
   which path records take should have come first.
2. **The bottleneck moved and the plan did not.** After the fused decode the
   scan was 1.19 s but the end-to-end total was still 3.23 s: materialization
   had become 63% of the run while the next planned change was another decoder
   optimization. Re-measuring the split, rather than continuing down the list,
   is what produced the last row.

## Scaling

polars-bio is the only reader here that can use more than one core, so it is
worth being precise about how little that helps on this workload.

| Partitions | dosage | speedup | hardcall | speedup |
|---:|---:|---:|---:|---:|
| 1 | 1.221 s | 1.00× | 0.653 s | 1.00× |
| 4 | 0.489 s | 2.50× | 0.296 s | 2.20× |
| 8 | **0.379 s** | **3.22×** | **0.234 s** | **2.79×** |

Eight partitions is now faster than four, which it was not before. There is only
one pool of threads left: decoders write at the destination, so there is no
second pool copying behind them and nothing to oversubscribe the host with.

### The scan is not the limit

*From the `9cccf2e` session; not re-measured.* Measured with no Python in the
way, the provider's scan scales well:

| Partitions | `DS` scan | speedup | `ALT_COUNT` scan | speedup |
|---:|---:|---:|---:|---:|
| 1 | 1.250 s | 1.00× | 0.586 s | 1.00× |
| 4 | 0.386 s | 3.24× | 0.177 s | 3.32× |
| 8 | 0.248 s | **5.05×** | 0.115 s | **5.09×** |
| 16 | 0.295 s | 4.24× | 0.133 s | 4.40× |

`examples/pgen_scaling_probe.rs` in the provider reproduces this. It also
reports `DependencyRecords`, which counts records a partition must decode to
reconstruct an LD chain but never emits — the obvious suspect for parallel work
that duplicates rather than divides. On this fixture it is **zero**, so LD
dependencies cost nothing at any partition count.

### Where a four-partition run actually goes

*From the `9cccf2e` session; not re-measured.* Attributing every stage of the
dosage read, before and after the `.pvar` parse was parallelized:

| Stage | Arrow path | Direct decode | Scales? |
|---|---:|---:|---|
| `.pvar` parse, at open | 0.07 s | 0.07 s | yes, ~3.8× |
| decode | 0.37 s | 0.38 s | yes |
| copying batches into the array | 0.45 s | **gone** | — |
| everything else | 0.06 s | 0.06 s | |
| **total** | **0.84 s** | **0.51 s** | |

The copy is gone outright, which is both the 1.63× at four partitions and the
reason scaling improved to 3.2×: the stage that capped at ~2.8× no longer
exists, so the run is decode-bound and the decoder scales 5×.

The `.pvar` parse is the only fixed cost left, at 14% of a four-partition dosage
read, and it is paid once. An earlier revision of this path paid it twice —
opening the fileset to ask for the shape, then again to decode — which cost 18%
of a hardcall read and briefly made that workload slower than the Arrow path it
replaced.

### The copy has a hard ceiling

*From the `9cccf2e` session; not re-measured.* Copying the same 10.13 GB into
the destination, varying only whether the destination's pages are already
resident:

| Destination pages | 1 thread | 8 threads | speedup | GB/s at 8 |
|---|---:|---:|---:|---:|
| fresh — what a new array gives you | 0.742 s | 0.262 s | 2.84× | 38.7 |
| already resident | 0.255 s | 0.097 s | 2.62× | 103.9 |

Two things fall out of that table.

**First-touch page faults are two thirds of the copy.** A fresh 10.13 GB
`numpy.empty` costs about 618,000 minor faults, and paying them takes longer
than the memcpy does. Nothing can make those pages resident for free — something
has to touch them once — so this is a floor, not a bug.

**The copy cannot exceed ~2.8× however many threads it gets.** Note both rows
plateau at roughly the same ratio, so this is not lock contention on the fault
path: the resident row reaches 103.9 GB/s, which with read and write is ~208
GB/s of traffic and is this machine's practical memory bandwidth. The fresh row
is bound by the kernel's fault path instead, at a similar ratio for a different
reason.

Copying is now spread across a small thread pool, which is what took scaling
from 1.21× to 1.73×, and the parallel `.pvar` parse took it from there to 1.99×.
Neither is going further.

Pre-faulting the destination in background threads while the scan starts was
tried and is **slower** (1.127 s → 1.329 s at four partitions): touching pages
from Python holds the GIL and starves the copy workers.

### What would move it

Everything that was on this list is done. The `.pvar` parse is threaded, the
decoder writes into the destination array — which deleted the copy stage
outright — and the fileset is opened once rather than once per question asked
of it.

Nothing measured here now has obvious headroom: the only fixed cost is that
single `.pvar` parse, and the rest is decode, which already scales 5×. Further
work should start by re-profiling rather than by picking from this list.

### This is a property of the workload, not of the reader

Every measurement here materializes a dense matrix, which is the one shape that
forces the copy. A streaming or SQL consumer never pays it, and gets the
provider's 5× instead. And the readers being compared against do not scale at
all — pgenlib is single-threaded — so at four partitions polars-bio is 3.81×
faster than pgenlib on dosage and 2.79× on hardcalls, on top of already being
faster at one. (Earlier revisions carried 2.23× and 2.37× here, which were the
pre-direct-decode four-partition figures and had not been updated with the
table above.)

## Where the remaining gap is

Both causes named in earlier revisions of this document have been addressed.

**The two-pass decode is fused.** 81% of this fixture is `record_type=0x14`: one
common genotype for every sample plus a sparse difflist of exceptions. That
record has no per-sample base to reconstruct, so filling a `u8` category per
sample and then reading it back to write the output was one pass more than the
record needs. `DS` and `ALT_COUNT` now fill the Arrow values slice from the
common category and patch the difflist into it directly. Scan: 2.31 s → 1.19 s
for dosage, 1.65 s → 0.59 s for hardcalls.

Note this is the opposite of what a packed-representation optimization would
have done. pgenlib's equivalent is a vectorized `vecset` over `sample_ct/4`
packed bytes followed by `Expand2bitTo8` writing `sample_ct` bytes; a fused fill
writes `sample_ct` and nothing else. For the record type that dominates, packing
would have been a regression.

**The materialization copy is down to one.** Getting a contiguous array through
`read_pgen` consolidated the scan's batches into a second full Arrow buffer
before NumPy ever saw them — a whole extra 10.13 GB. `read_pgen_matrix` streams
batches into a preallocated array instead, so the values are written once.

What was left on the Arrow path, at one partition — *from the `9cccf2e`
session, and superseded by the direct decode below, which is what the headline
figures now measure*:

| Stage | dosage | hardcall |
|---|---:|---:|
| Planning, PVAR/PSAM parsing, metadata columns | ~0.07 s | ~0.07 s |
| Genotype decode into Arrow batches | 1.19 s | 0.59 s |
| One copy, batches → destination array | ~0.4 s | ~0.03 s |
| **Total** | **1.67 s** | **0.69 s** |
| pgenlib, one pass into a preallocated buffer | 1.87 s | 0.87 s |

The copy in the third row is what `read_pgen_matrix` deleted; the current
one-partition figures are 1.221 s and 0.653 s against pgenlib's 1.861 s and
0.826 s. The paragraphs below record why that copy could not be removed while
the values still had to arrive as Arrow.

**That last copy cannot be removed on this path.** Arrow's `ListArray` uses
32-bit offsets, so one batch holds at most 842,811 rows at 2,548 samples and the
matrix can never arrive as a single zero-copy buffer — at least two batches are
required here, and consolidating them is a copy. Closing it means the decoder
writing into the caller's buffer, the way pgenlib does, which is a new
non-DataFrame API rather than a tuning change.

**At one partition that is worth about a quarter of the run, and at four it is
worth over half.** The copy does not parallelize past ~2.8× while the scan does
5×, so it grows into the dominant term as partitions are added — 0.45 s of an
0.84 s four-partition run. [Scaling](#scaling) has the measurements. If this API
is ever built, that is the argument for it.

**This cost does not exist for streaming or SQL consumers** — it is created by
the benchmark's requirement for one contiguous NumPy array, which pgenlib
satisfies for free by decoding straight into the caller's buffer.

Peak RSS is 12.6 GB against pgenlib's 12.1 GB on dosage and 5.5 GB against
5.0 GB on hardcalls, down from 22.3 GB and 8.3 GB earlier in this work. **Memory
is the one axis where pgenlib is still ahead**, but only by 4% and 10%. The Arrow
intermediate is gone; what remains is the scan's in-flight buffers and the
harness's own post-read hashing, which pgenlib pays too.

## Zero mismatches

Every reader is checked against pgenlib with **no tolerance** — cells that
differ bitwise, not cells that differ by more than an epsilon.

| Comparison | Workload | Cells | Differing |
|---|---|---:|---:|
| polars-bio vs pgenlib | dosage | 2,532,408,788 | **0** |
| snputils vs pgenlib | dosage | 2,532,408,788 | **0** |
| polars-bio vs pgenlib | hardcall | 2,532,408,788 | **0** |
| snputils vs pgenlib | hardcall | 2,532,408,788 | **0** |

polars-bio is bit-identical to pgenlib at every partition count in both
workloads.

**snputils is not an independent check.** Its PGEN reader wraps pgenlib and
calls `read_list` directly, so `snputils vs pgenlib` is close to a tautology
and is reported only for completeness. The load-bearing comparison is
`polars-bio vs pgenlib`, which is a genuinely separate implementation — the
provider decodes the format in Rust and shares no code with PLINK 2.

The same fact explains the timings: snputils is pgenlib plus a NumPy wrapper,
so it is not a third implementation outperforming polars-bio. On hardcalls the
two are now within 2 ms of each other — 0.875 s against 0.873 s — which is what
a thin wrapper around the same reader should look like, and is the shape the
import fix in [Corrections](#corrections-to-earlier-revisions-of-this-document)
item 6 restored. Its dosage figure carries the int8→float32 widening on top.

### The comparison can fail

A zero-difference result is worthless if the comparison cannot report a
difference. `benchmarks/pgen_verify.py` corrupts a single cell of the reader
under test and asserts the corruption is detected;
`selftest_single_cell_detected: 1` is recorded in the result files and the run
aborts if it is ever 0.

### Row order

A DataFrame scan with more than one partition may emit rows out of source order.
`read_pgen_matrix` does not: it writes each variant at its own row index, so the
matrix is in PVAR order at every partition count and the recorded descent count
is now **zero everywhere**, where it was 88–101 above one partition. Value and position hashes are taken after sorting by
position, and the raw descent count is recorded per run rather than hidden.

## Timing contract

The timer covers fileset opening, companion discovery and parsing, record
decoding, variant positions and sample identifiers, and final C-contiguous
materialization in the workload's dtype. Imports are excluded — each reader's
module is imported before the clock starts and the cost recorded separately as
`import_seconds`, because it is a one-time process cost paid once however many
filesets are then read, and the magnitudes are not comparable (~0.46 s for
polars-bio's ~228 MB extension against ~0.03 s for pgenlib and snputils).
Thread-pool configuration remains inside the timer; it measures 0.04 ms. Peak
RSS is process `ru_maxrss`; hashing runs outside the timer.
Measurements use a warm filesystem cache and a deterministically rotated,
direction-alternating reader order. `OMP`, `OpenBLAS`, `MKL`, `Accelerate`, and
`NumExpr` pools are capped at one for every reader; `POLARS_MAX_THREADS`,
Rayon, and DataFusion target partitions follow the partition count under test.

pgenlib and snputils read only genotypes natively, so both take variant
positions and sample identifiers from the `.pvar`/`.psam` through the same
helper — they are charged identically for it. polars-bio produces all three
from one scan.

### Build profile is part of the result

polars-bio **must** be built release with `-C target-cpu=native`. A plain
`maturin develop` is a debug build and measured 3.1× slower. The runner records
the loaded extension's path and size in `metadata.polars_bio_build`; release is
~228 MB, debug ~336 MB.

### Corrections to earlier revisions of this document

Recorded because each changed a headline number:

1. An earlier revision claimed polars-bio was **4.214× faster than snputils**.
   That was wrong. It measured snputils through `PGENReader().read()` plus a
   3-D sum (27× its native path) and pgenlib through a per-variant Python loop
   (5.5× its bulk path), and used polars-bio's `GT` rather than `DS` (3.1×).
   Every reader now uses its native API.
2. The dosage and hardcall workloads were conflated, comparing polars-bio's
   float32 dosage column against the others' int8 hardcall counts. They agree
   numerically on this fileset only because it carries no dosage track.
3. The polars-bio adapter materialized a 10 GB intermediate pairs array before
   summing; removing it cut the slice from 1.186 s / 2,308 MB to 0.753 s /
   964 MB with an identical value hash.
4. **The timer charged each reader for importing its own library**, despite the
   contract above having always said imports are excluded. Every measurement
   runs in a fresh process and every adapter imported inside the timed function,
   so the cost was always included: ~0.46 s of polars-bio's figure against
   ~0.03 s of pgenlib's and snputils'. The harness now warms the import for
   every reader alike. **That is worth ~0.43 s of polars-bio's dosage figure.**
5. **The import warm-up did not reach snputils' reader.** Correction (4) moved
   every reader's library import out of the timed region, and the warm-up did
   that by importing each reader's top-level package. snputils loads its readers
   lazily, so that warmed almost nothing: `import snputils` costs ~0.03 s while
   the first touch of `snputils.read_pgen` costs ~0.94 s as the reader module is
   loaded — and that touch happens inside the adapter, on the clock. snputils
   was therefore charged for a module load every other reader had excluded, and
   `import_seconds` recorded 0.017 s, which reads like a fair warm-up but was
   only measuring the cheap package. Warming is now a per-reader callable that
   touches the attribute its adapter calls. **snputils' hardcall figure went
   1.470 s → 0.875 s and its dosage figure 3.462 s → 2.651 s.** The sign of this
   correction is worth stating plainly: the previous figures flattered
   polars-bio. It also removes an implausibility — snputils wraps pgenlib, and
   at 1.470 s against pgenlib's 0.826 s the wrapper appeared to cost 78%; the
   two now differ by 2 ms. `bgen_matrix.py` never had this bug, because it
   imports the real reader at module scope, so the BGEN figures are unaffected.

6. **polars-bio was measured through its DataFrame path**, which is not its
   fastest native API for a dense matrix — the same class of error as (1), which
   had measured pgenlib through a per-variant loop. `read_pgen` costs a second
   full copy of the values and measures 3.225 s / 22.3 GB on the dosage
   workload; `read_pgen_matrix` is the counterpart to `pgenlib.read_list` and
   measures 1.84 s / 13.6 GB.

## Inputs, builds, and versions

| Item | Value |
|---|---|
| Slice | `chr22.first-25000.pgen`, 2,923,281 bytes |
| Whole chromosome | `chr22.full.pgen`, 79,921,211 bytes (+113,320,253-byte `.pvar`) |
| Whole chromosome SHA-256 | `ca2267eb44335ee1…` |
| Source callset | IGSR/1000 Genomes GRCh38 phased chromosome 22, as used by the BCF and BGEN benchmarks |
| Export | `plink2 --make-pgen`, PLINK v2.0.0-a.7.3 M1 (8 Aug 2026) |
| datafusion-bio-formats | [`5f3dcf3`](https://github.com/biodatageeks/datafusion-bio-formats/commit/5f3dcf3); the PGEN provider in it is [#232](https://github.com/biodatageeks/datafusion-bio-formats/pull/232) as merged to master |
| polars-bio branch build | branch `feat/bgen-pr220-bench` ([#436](https://github.com/biodatageeks/polars-bio/pull/436), not merged), pinning the provider commit above |
| snputils / pgenlib | 1.1.1.dev17+gbdb1a56b5 / 0.94.1 |
| polars-bio / Polars / PyArrow / NumPy | 0.33.1 (branch build) / 1.42.1 / 24.0.0 / 2.5.2 |
| Python | 3.12.9 |
| Host | Apple M3 Max, 16 CPU cores, 64 GiB RAM, macOS 15.6 arm64 |
| polars-bio build | release, `RUSTFLAGS="-C target-cpu=native"` |

## Reproduce

The PGEN fixtures come from the chromosome 22 callset the BCF benchmark already
downloads; `setup.sh` exports them with plink2.

Build polars-bio optimized first — not optional, see above:

```bash
cd /path/to/polars-bio
RUSTFLAGS="-C target-cpu=native" maturin develop --release --locked
```

Then:

```bash
POLARS_BIO_BUILD_PROFILE=release POLARS_BIO_RUSTFLAGS="-C target-cpu=native" \
.venv/bin/python run_pgen_benchmarks.py \
  --runs 3 --modes dosage hardcall --polars-bio-partitions 1 8 \
  --pgen /path/to/chr22.full.pgen \
  --expected-rows 993881 --expected-samples 2548 \
  --output results/pgen_reader_benchmark_full_cohort.json
```

Confirm the run measured the artifact you think it did: `metadata.polars_bio_build`
in the result JSON records the declared profile, the rustflags, and the loaded
extension's size.

For the [Scaling](#scaling) figures, add the partition counts to that run and
measure the provider on its own alongside it — the two together are what
separate a scan that will not divide from a run whose scan is only a third of
the time:

```bash
# end to end, per partition count
... --polars-bio-partitions 1 4 8 ...

# the provider alone, no Python, with the scan metrics
cd /path/to/datafusion-bio-formats
RUSTFLAGS="-C target-cpu=native" cargo run --release \
  -p datafusion-bio-format-pgen --example pgen_scaling_probe -- \
  /path/to/chr22.full.pgen DS 1 2 4 8 16
```

The whole-chromosome run holds two full matrices in one process during
verification, peaking near 21 GB. Pass `--skip-verification` on a smaller host;
the per-run equivalence hashes still have to agree.
