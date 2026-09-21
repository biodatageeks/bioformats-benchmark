# Why mmCIF is slower at one partition, and why scaling flattens

The tested polars-bio build is still master `29b04c5159135319d387dfcda9b7b84420dea549`, with datafusion-bio-formats `v1.13.0` (`00487b9edf53cbf36918d7d22a03a79fd0acf863`). No production parser changes were made for this investigation. The causes differ by format: A2M/A3M have a single-partition implementation; mmCIF partitions by whole input source; Stockholm pays for a serial boundary scan; and final DataFrame materialization limits some otherwise parallel readers.

The mmCIF provider uses a **Rust CIF parser**, not Gemmi. This corrects the initial PR description. Gemmi and Biopython are independent oracles for the measured build.

## Actual source parallelism

Increasing `target_partitions` requests parallelism; it does not guarantee that a provider implements it. The physical source plans recorded by [profile_new_formats.py](profile_new_formats.py) show:

| Format / original workload | Requested 1 | Requested 4 | Requested 8 | Explanation |
|---|---:|---:|---:|---|
| A2M | 1 | 1 | 1 | Source plan hardcodes one partition |
| A3M | 1 | 1 | 1 | Same shared implementation |
| Stockholm | 1 | 4 | 8 | Whole-alignment ranges, after a serial boundary scan |
| PDB, eight sources | 1 | 4 | 8 | Whole-source parallelism |
| mmCIF, two sources | 1 | 2 | 2 | Capped by the two input sources |
| Foldcomp, 768 entries | 1 | 4 | 8 | Independent indexed entries |

Source evidence:

- [FastaLikeTableProvider::scan](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-msa/src/fastalike.rs#L122) ignores the session and constructs `UnknownPartitioning(1)`. The [MSA design](https://github.com/biodatageeks/polars-bio/blob/29b04c5159135319d387dfcda9b7b84420dea549/openspec/changes/add-msa-alignment-formats/design.md#L121) deliberately preserves an ordered, single-partition stream with the query first. Concatenating more records does not enable parallel reads.
- [StructureTableProvider::scan](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-structure/src/table_provider.rs#L158) uses `target_partitions.min(sources.len()).max(1)`. A large individual CIF entry is not split into parallel atom ranges.
- [Stockholm partition planning](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-msa/src/stockholm/table_provider.rs#L133) scans all lines sequentially to locate `//` boundaries and count alignment ordinals before starting the parallel parse. This is repeated for each new physical plan, with no retained boundary index.

On the 64.14 MiB Stockholm workload that planning pass costs **23.9–24.1 ms** at 4/8 partitions, compared with 0.7 ms at one. This is about 42% of the approximately 57 ms eight-partition diagnostic plugin scan. The extra serial pass explains much of the plateau even though the actual source has eight partitions. A single alignment also cannot be split by this implementation.

## mmCIF: the expensive stage is building owned atom rows

The original end-to-end benchmark takes **1.296 s for polars-bio versus 1.085 s for the Gemmi adapter** at one partition: polars-bio takes 19.4% longer. These are equivalent output tables, but the internal work differs. The Gemmi oracle reads raw CIF columns and casts them into Polars; the native reader additionally constructs a complete structural atom model, assigns hierarchy ordinals and validates duplicate sites before producing Arrow columns.

A release-built native diagnostic isolated one 4V9D file, 292,354 atoms. Median milliseconds from three fresh processes, without the sampling profiler attached:

| Native stage | Median ms |
|---|---:|
| Tokenize and build the raw CIF document | 102.6 |
| Materialize the borrowed category-column view | 63.4 |
| Decode rows into owned `Atom` objects | 276.9 |
| Normalize hierarchy and validate atom sites | 65.9 |
| Build the selected 27 Arrow columns | 80.6 |
| Drop the CIF document | 0.9 |
| Drop the normalized entry | 13.4 |
| Whole measured native pipeline | 602.6 |

Individual stage medians need not sum exactly to the median total. This diagnostic excludes input I/O, Python and Polars; it is not another end-to-end benchmark. It drops the parsed document before building Arrow, whereas the production source can retain it while emitting the entry. The nested `category_and_decode_seconds` field in the raw JSON must not be added to its three component stages.

The row-construction and normalization stages account for about **57%** of the measured native time. Code and the separate stack sample explain the work inside them:

- [`value()`](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-structure/src/mmcif.rs#L11) hashes a constant tag name for each requested field of each atom. The decoder also parses numbers and allocates owned strings, including author and label identities.
- [`Atom`](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-structure/src/model.rs#L10) occupies **512 bytes** on this target before its strings' heap storage. The 292,354-atom vector grows to capacity 524,288, reserving **256 MiB** for its slots alone. Many fields are built even when they are absent from the Arrow projection.
- [`normalize()`](https://github.com/biodatageeks/datafusion-bio-formats/blob/00487b9edf53cbf36918d7d22a03a79fd0acf863/datafusion/bio-format-structure/src/model.rs#L64) constructs model/chain/residue maps and a duplicate-site set, cloning string keys during the per-atom loop. Arrow construction then traverses the owned rows again to make column arrays.
- The macOS stack sample contains decoder, hash, numeric parsing and allocation stacks. Stage timings identify where time goes; they do not separately quantify how much a proposed hash/allocation optimization would save.

This makes the owned intermediate representation the first optimization target. The tokenizer matters, but it is only about 17% of this native measurement. The mmCIF Arrow collection itself takes 1.244 s for the two-source workload at one partition, close to the 1.338 s diagnostic Polars path: most time is already spent before final Polars materialization.

## Control: give mmCIF eight sources

The control repeats the same checked 4V9D file through the collection API eight times: **272.90 MiB**, **2,338,832 rows**, 27 compared columns. This is a warm-cache source-parallelism experiment, not eight distinct structures or a disk-bandwidth test. The physical plan confirms eight source partitions at `target_partitions=8`.

| Reader / requested partitions | Median seconds | Speedup over polars-bio 1 | Median peak RSS MiB |
|---|---:|---:|---:|
| Gemmi adapter / 1 | 4.386 | — | 1790.9 |
| polars-bio / 1 | 5.133 | 1.00× | 2380.1 |
| polars-bio / 2 | 2.707 | 1.90× | 3320.1 |
| polars-bio / 4 | 1.552 | 3.31× | 5017.4 |
| polars-bio / 8 | 0.941 | **5.45×** | 7122.4 |

Three fresh-process repetitions per configuration; all **15 timed runs passed** the original full-table oracle checks, including **757,781,568 cells** across 12 polars-bio runs. The additional independent Biopython-versus-Gemmi comparison also passed. There were **zero mismatches and zero measured numeric errors**. The original two-source plateau therefore does not establish a general mmCIF concurrency failure. Scaling works when independent sources exist, at a substantial memory cost.

## PDB and Foldcomp already scale; materialization matters

In the original repeated benchmark, PDB improves **4.00×** (0.507 → 0.127 s) and Foldcomp **4.43×** (0.404 → 0.091 s) from one to eight requested partitions. They do not exhibit an absence of scalability.

The separate one-run-per-configuration diagnostic compares projected DataFusion-to-PyArrow collection with a later full Polars plugin scan:

| Workload | Arrow collection 1 / 4 / 8, ms | Polars plugin 1 / 4 / 8, ms |
|---|---|---|
| PDB | 479.8 / 148.6 / 112.3 | 517.4 / 174.6 / 167.3 |
| Foldcomp | 325.6 / 83.2 / 44.1 | 398.7 / 104.4 / 96.8 |

Foldcomp's Arrow collection improves **7.39×**, while the Polars path has a larger floor. The [plugin](https://github.com/biodatageeks/polars-bio/blob/29b04c5159135319d387dfcda9b7b84420dea549/polars_bio/io.py#L5042) iterates over the combined batch stream, converts each batch to a Polars DataFrame and yields it to the collector. Once parallel decoding becomes short, conversion, collection and memory traffic are more visible.

These diagnostic runs are not repeated performance estimates. Arrow collection includes execution planning and PyArrow batch wrapping; converting the retained batch list is a separate path with different overlap and rechunk behavior. Do not add its conversion time to the plugin time or treat their difference as an exact measurement of GIL overhead. The profiles do not establish a global GIL or scheduler problem.

## Changes supported by the evidence

1. **mmCIF:** resolve column references once per block, reserve the known atom count, and reduce duplicated string ownership and normalization-key allocations. A more substantial option is an atom-level columnar path that avoids the large row model. Preserve duplicate-site checks, null semantics, source identities and the oracle contract; no speedup from these changes has been measured yet.
2. **A2M/A3M:** implement record-boundary parallelism with an explicit query-first/order contract. Merely raising the partition setting or concatenating more records cannot change the existing source plan.
3. **Stockholm:** reduce or reuse boundary-discovery work with input-change detection, while preserving interleaved alignment, annotation and fallback-ordinal semantics. A single alignment still needs a different partitioning strategy.
4. **Benchmark interpretation:** report actual source partitions alongside requested counts, and distinguish many-source throughput from single-file parsing. Profile the Arrow-to-Polars path separately before changing its batching or interface.

All diagnostic code, timing instrumentation and reproduction commands are in [profiling/README.md](profiling/README.md). Raw evidence: [physical plans and stream stages](results/new_formats_profiles.json), [native mmCIF stages](results/mmcif_native_profile.json), [qualitative stack sample](results/mmcif_stack_sample.txt), and [oracle-checked eight-source benchmark](results/mmcif_eight_sources.json).
