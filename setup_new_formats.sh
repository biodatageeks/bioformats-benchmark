#!/usr/bin/env bash
set -euo pipefail

BENCH_ROOT="$(cd "$(dirname "$0")" && pwd)"
: "${POLARS_BIO_SOURCE:?Set POLARS_BIO_SOURCE to the polars-bio master checkout}"
cd "$BENCH_ROOT"
uv venv --python 3.12 .venv-newformats
uv pip install --python .venv-newformats/bin/python -r requirements-new-formats.txt
mkdir -p data/new_formats
(
    unset CONDA_PREFIX
    export VIRTUAL_ENV="$BENCH_ROOT/.venv-newformats"
    cd "$POLARS_BIO_SOURCE"
    "$VIRTUAL_ENV/bin/maturin" develop --release --locked --uv
) 2>&1 | tee data/new_formats/build.log
.venv-newformats/bin/python - "$POLARS_BIO_SOURCE" <<'PY'
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1]).resolve()
native = Path(importlib.import_module("polars_bio.polars_bio").__file__)
record = {
    "profile": "release",
    "source_revision": subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip(),
    "tracked_diff": subprocess.check_output(["git", "-C", str(source), "diff", "HEAD", "--"], text=True),
    "rustc": subprocess.check_output(["rustc", "--version"], text=True).strip(),
    "rustflags": os.environ.get("RUSTFLAGS", ""),
    "command": "maturin develop --release --locked --uv",
    "extension_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
    "cargo_lock_sha256": hashlib.sha256((source / "Cargo.lock").read_bytes()).hexdigest(),
}
Path("data/new_formats/build.json").write_text(json.dumps(record, indent=2) + "\n")
PY
.venv-newformats/bin/python prepare_new_formats.py "$@"
