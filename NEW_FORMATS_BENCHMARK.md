# Formats added after polars-bio 0.35.1

The release build of polars-bio master `29b04c5159135319d387dfcda9b7b84420dea549` passed all **72 large-input runs**, with **zero oracle mismatches** across all rows and columns of the shared tables described below. The 54 polars-bio runs checked **393,448,050 cells**; all measured coordinate, occupancy and B-factor differences were exactly zero. A separate full-table mmCIF comparison also passed against Biopython `MMCIF2Dict`.

This is an end-to-end **read-to-Polars-DataFrame** comparison. Reference readers include the Python work needed to produce that same table; these ratios are not standalone parser or codec speedups. Residue geometry, unselected provenance fields, remote I/O and compression variants are outside this benchmark.

Raw measurements, input manifests, checksums, package versions and build fingerprint: [large results](results/new_formats.json) and [small integration run](results/new_formats_smoke.json). The package version still reports `0.35.1`; the Git revision and native extension SHA-256 identify the tested unreleased code.

## Inputs

| Format | Input and scaling | Logical MiB read | Rows per run |
|---|---|---:|---:|
| A2M | hh-suite query converted to dotted A2M; 1,201 copies with unique record IDs | 64.56 | 70,859 |
| A3M | hh-suite query A3M; 2,089 copies with unique record IDs | 65.00 | 123,251 |
| Stockholm | Pfam PF00001 + interleaved Rfam RF00001 + annotated hmmalign output; 182 copies of the three complete alignments | 64.14 | 152,516 |
| PDB | RCSB 1JJ2; eight source occurrences through the collection API | 65.90 | 788,344 |
| mmCIF | RCSB 4V9D; two source occurrences through the collection API | 68.23 | 584,708 |
| Foldcomp | 24-entry official example database repeated 32 times; 768 uniquely keyed/named entries with rebuilt offsets | 1.60 | 868,192 |

PDB/mmCIF collections deliberately reuse real files, so these are warm-cache throughput workloads, not claims about corpus diversity or disk bandwidth. Foldcomp is only 1.60 MiB compressed but expands to 868,192 atom rows. The text workloads are approximately 64 MiB each. `--target-mib` and `--foldcomp-copies` can increase these sizes.

Sources: [RCSB 1JJ2](https://files.rcsb.org/download/1JJ2.pdb.gz), [RCSB 4V9D](https://files.rcsb.org/download/4V9D.cif.gz), [pinned MSA fixture provenance](https://github.com/biodatageeks/polars-bio/blob/29b04c5159135319d387dfcda9b7b84420dea549/tests/data/io/msa/README.md), and [pinned structure/Foldcomp fixture manifest](https://github.com/biodatageeks/polars-bio/blob/29b04c5159135319d387dfcda9b7b84420dea549/tests/data/structure/manifest.json). Every downloaded and generated input is SHA-256 checked. Large files are excluded from Git.

## Wall time

Median seconds from three fresh processes per configuration; lower is better. `t` sets DataFusion target partitions and the Polars thread pool. Reference adapters run at `t=1`.

| Format | Reference adapter | Reference seconds | polars-bio t1 | t4 | t8 | Reference / polars-bio t8 |
|---|---|---:|---:|---:|---:|---:|
| A2M | Biopython SeqIO → Polars | 0.1963 | 0.0435 | 0.0420 | 0.0431 | 4.55× |
| A3M | Biopython SeqIO → Polars | 0.2664 | 0.0490 | 0.0499 | 0.0504 | 5.29× |
| Stockholm | Easel + annotation lines → Polars | 0.4466 | 0.0924 | 0.0572 | 0.0550 | 8.11× |
| PDB | Gemmi → Polars | 4.7208 | 0.5072 | 0.1718 | 0.1269 | 37.20× |
| mmCIF | Gemmi → Polars | 1.0853 | 1.2957 | 0.6936 | 0.7099 | 1.53× |
| Foldcomp | official Foldcomp + Gemmi → Polars | 4.7517 | 0.4038 | 0.0985 | 0.0912 | 52.07× |

A2M/A3M always use one source partition in this implementation. Stockholm, PDB and Foldcomp benefit from additional partitions. mmCIF is slower than its Gemmi adapter at t1 (1.2957 vs 1.0853 s), improves at t4 (0.6936 s), and gains nothing further at t8; it has only two source files to distribute. The [profiling follow-up](NEW_FORMATS_PROFILING.md) measures the causes and demonstrates 5.45× mmCIF scaling with eight sources, with zero oracle mismatches.

## Peak process memory

Median high-water RSS in MiB, captured before IPC export and verification. It includes imports, parsing intermediates, allocator high-water marks and the final DataFrame; it is not incremental output memory.

| Format | Reference | polars-bio t1 | t4 | t8 |
|---|---:|---:|---:|---:|
| A2M | 298.6 | 282.4 | 281.0 | 284.2 |
| A3M | 322.2 | 283.1 | 286.0 | 285.9 |
| Stockholm | 482.6 | 287.4 | 314.7 | 345.2 |
| PDB | 498.1 | 547.0 | 949.7 | 1240.7 |
| mmCIF | 1000.9 | 1489.4 | 2102.0 | 2105.7 |
| Foldcomp | 377.7 | 457.5 | 469.9 | 484.8 |

Parallel structure reads trade memory for speed: PDB rises from 547 MiB at t1 to 1,241 MiB at t8; mmCIF rises from 1,489 MiB to about 2,102 MiB at t4. The complete raw timings and sample standard deviations are retained in the JSON, including slower samples.

## Data-quality contract

| Format | Compared columns | Independent oracle |
|---|---|---|
| A2M / A3M | `name`, nullable `description`, verbatim `sequence` including case, dots and gaps | Biopython `SeqIO` |
| Stockholm | `alignment_id`, `name`, assembled `sequence`, complete ordered GS/GR tag-value bags | Easel `MSAFile`; literal annotation lines for fields omitted by its high-level API |
| PDB | 15 columns: source/entry/atom ordinals, model, chain, author residue ID, insertion code, atom/residue names, alternate ID, XYZ, occupancy, B-factor | Gemmi, with source model/serial identities restoring PDB record order |
| mmCIF | The 15 atom columns plus 12 raw fields: atom ID, record type, author/label chain, label entity/sequence IDs, author/label atom and residue names, element and charge | Gemmi raw category; mandatory independent Biopython `MMCIF2Dict` full-table check |
| Foldcomp | The 15 atom columns plus database key and lookup name | Official `foldcomp.get_data` raw coordinates/B-factors; official PDB decode + Gemmi for identities |

Each comparison checks exact schema/dtypes, nonempty row count, unique identity keys, complete key population, null masks, strings and nested annotation bags. Sorting is by stable identities and never drops rows or duplicates. Every numeric value must be finite. The predeclared absolute tolerances are 1e-9 for text and 1e-4 for the native-versus-official Foldcomp Float32 decode, with zero relative tolerance. **Observed maximum error was 0.0 for every numeric column in every large run**, so no tolerance was needed to obtain the reported agreement.

The measured v1.13.0 provider has its own Rust CIF tokenizer. Gemmi and Biopython `MMCIF2Dict` therefore supply two independent parsing checks. Foldcomp checks decoder/adapter compatibility on reconstructed coordinates; it does not assert lossless equality with the original uncompressed structure.

### Oracle normalization and cost

- PDB 1JJ2 repeats chain names after other chains. Gemmi groups those chain parts in its hierarchy, whereas atom ordinals follow source record order. The adapter maps unique `(model, serial)` identities back to the original records, with strict missing/extra/duplicate checks. This is regression-tested. Gemmi stores occupancy/B-factor as Float32, so those PDB fields are restored to their serialized two-decimal precision; XYZ stays Float64.
- Stockholm preserves complete per-sequence GS/GR annotations; the extra literal-line pass is included in its reference timing.
- The official Foldcomp API exposes precise coordinates via `get_data`, and atom identities through `decompress` PDB text. Its adapter calls both, then constructs the same Polars table. This includes two decoder calls and Python conversion work, and explains part of the large end-to-end ratio. No codec-only speed claim is made.

## Reproduction and validation

Run the commands in the [README](README.md#formats-added-after-polars-bio-0351). `setup_new_formats.sh` creates a separate environment, installs pinned oracle dependencies, builds the requested source with `maturin develop --release --locked --uv`, and records the extension and Cargo.lock hashes. The runner rejects a missing/non-release build record, a different imported native artifact, or changed dataset bytes.

The measured machine was an Apple M3 Max (16 logical CPUs, 64 GiB RAM), macOS 15.7.9 arm64, Python 3.12.2. All workloads used the local filesystem with warm OS caches. Imports, process startup, sorting, IPC transport and comparison are excluded from wall time. Read setup, provider construction, parsing and materialization are included. Processes run sequentially and reader order rotates between iterations. Results are specific to this machine and workload; the exact versions and compiler are recorded in the JSON.

Validation: 17 harness tests passed, including intentional sequence, identity, duplicate-row, missing-row, annotation, dtype, null, nonfinite-value and coordinate corruptions. The small six-format suite passed at t1/t4 (18 runs); the large suite passed three repetitions at t1/t4/t8 (72 runs) plus the independent mmCIF comparison. Failures write `status: failed` and exit nonzero; there is no skip-verification option.
