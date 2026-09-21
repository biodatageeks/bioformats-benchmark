# Reproduce the new-format diagnostics

First build and prepare the inputs using the repository's [setup instructions](../README.md#formats-added-after-polars-bio-0351). Use the same release extension and checksum-pinned manifest as the correctness benchmark. Run performance processes sequentially on an otherwise idle machine.

## Physical plans and stream stages

From the benchmark repository:

```bash
.venv-newformats/bin/python profile_new_formats.py --format mmcif --partitions 8
.venv-newformats/bin/python profile_new_formats.py --format sto --partitions 8
.venv-newformats/bin/python profile_new_formats.py --format a3m --partitions 8 --plan-only
```

The script sets `POLARS_MAX_THREADS` before importing Polars, sets DataFusion target partitions, and records the actual physical source plan. Use a fresh process for every configuration. To collect all six formats at 1/4/8:

```bash
.venv-newformats/bin/python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

runs = []
for fmt in ("a2m", "a3m", "sto", "pdb", "mmcif", "foldcomp"):
    for partitions in (1, 4, 8):
        result = subprocess.run(
            [sys.executable, "profile_new_formats.py", "--format", fmt,
             "--partitions", str(partitions)],
            check=True, capture_output=True, text=True,
        )
        runs.append(json.loads(result.stdout))
Path("results/local_new_formats_profiles.json").write_text(
    json.dumps({"runs": runs}, indent=2) + "\n"
)
PY
```

The SQL diagnostic projects the same columns as the benchmark, consumes DataFusion batches into PyArrow, measures converting the retained batch list to Polars, then runs the normal Polars plugin separately. It checks row-count agreement. This does **not** replace the mandatory full-table correctness benchmark. Planning is measured separately, but `execute_stream()` also constructs its own plan, so its elapsed time already includes planning. The stored [profile artifact](../results/new_formats_profiles.json) additionally embeds the original build, machine, versions and manifest.

## Eight-source mmCIF control

Repeat the same real file through the collection API; do not concatenate CIF text with duplicate data-block identifiers:

```bash
.venv-newformats/bin/python - <<'PY'
import json
from pathlib import Path

path = Path("data/new_formats/manifest.json")
manifest = json.loads(path.read_text())
case = next(c for c in manifest["cases"] if c["format"] == "mmcif")
case["paths"] = [case["paths"][0]] * 8
case["copies"] = 8
case["logical_bytes"] = case["files"][0]["bytes"] * 8
manifest["cases"] = [case]
manifest["diagnostic"] = "Eight source occurrences of the checked 4V9D file"
path.with_name("mmcif-eight-sources.json").write_text(
    json.dumps(manifest, indent=2) + "\n"
)
PY
.venv-newformats/bin/python run_new_formats.py \
    --manifest data/new_formats/mmcif-eight-sources.json \
    --iterations 3 --threads 1 2 4 8 \
    --output results/local_mmcif_eight_sources.json
.venv-newformats/bin/python profile_new_formats.py \
    --manifest data/new_formats/mmcif-eight-sources.json \
    --format mmcif --partitions 8 --plan-only
```

This retains every existing oracle gate, including Biopython `MMCIF2Dict`. The reference remains single-threaded. Peak RSS for the measured eight-partition run was approximately 7 GiB.

## Native mmCIF stages

The [timing patch](mmcif_stages.patch) adds timers to the pinned native provider without changing parsing behavior. Apply it to a disposable checkout, **not** to Cargo's Git cache or the source used by the benchmark extension. [mmcif_stages.rs](mmcif_stages.rs) measures the complete native stages and the allocated atom-vector capacity. The runner uses mimalloc's v2 feature, matching the measured extension's allocator.

```bash
export BENCHMARK_ROOT="$PWD"
export PROFILE_ROOT="$(mktemp -d)"
export POLARS_BIO_SOURCE=/path/to/polars-bio
git clone https://github.com/biodatageeks/datafusion-bio-formats.git "$PROFILE_ROOT/formats"
git -C "$PROFILE_ROOT/formats" checkout 00487b9edf53cbf36918d7d22a03a79fd0acf863
git -C "$PROFILE_ROOT/formats" apply "$BENCHMARK_ROOT/profiling/mmcif_stages.patch"
mkdir -p "$PROFILE_ROOT/native/src"
cp profiling/mmcif_stages.rs "$PROFILE_ROOT/native/src/main.rs"
# Use the lockfile from the measured polars-bio revision 29b04c5.
cp "$POLARS_BIO_SOURCE/Cargo.lock" "$PROFILE_ROOT/native/Cargo.lock"
.venv-newformats/bin/python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["PROFILE_ROOT"])
dependency = json.dumps(str(root / "formats/datafusion/bio-format-structure"))
(root / "native/Cargo.toml").write_text(
    '[package]\nname = "newformats-native-profile"\n'
    'version = "0.1.0"\nedition = "2021"\n\n[dependencies]\n'
    f'datafusion-bio-format-structure = {{ path = {dependency} }}\n'
    'mimalloc = { version = "=0.1.52", default-features = false, features = ["v2"] }\n'
    'serde_json = "1"\n'
)
PY
cargo build --release --manifest-path "$PROFILE_ROOT/native/Cargo.toml"
for attempt in 1 2 3; do
    BIO_PROFILE_STAGES=1 "$PROFILE_ROOT/native/target/release/newformats-native-profile" \
        "$BENCHMARK_ROOT/data/new_formats/sources/4v9d.cif" 1 \
        > "$PROFILE_ROOT/stages-$attempt.jsonl" \
        2> "$PROFILE_ROOT/stages-$attempt.log"
done
```

The reported binary was compiled with rustc `1.95.0-nightly (57d2fb136 2026-02-01)`, release mode, and macOS linker flags `RUSTFLAGS='-C link-arg=-undefined -C link-arg=dynamic_lookup'`; those inherited extension linker flags are not required by this standalone executable. The saved result records input, patch, binary and resolved lockfile hashes. Formatting the included Rust runner does not change its measured logic.

Each fresh process loads the input before starting its clock. Standard output reports document parsing, combined category/decode/normalization, Arrow creation and destruction stages. Standard error reports the three nested timings (`category_view`, `decode_atoms`, `normalize`) from the patch. Keep those nested measurements separate when calculating totals.

For a qualitative macOS stack sample, start the executable with 24 iterations in the background and attach `/usr/bin/sample PID 5 1 -file output.txt`. Do not use the sampled process for reported stage timing. The checked-in [stack sample](../results/mmcif_stack_sample.txt) and [three-process timings](../results/mmcif_native_profile.json) came from separate runs. Native timings omit I/O and Python/Polars and do not have an independent output comparison; the unchanged production extension supplies the oracle-checked benchmark results.
