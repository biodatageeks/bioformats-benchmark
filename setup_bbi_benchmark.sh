#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BBI_VENV="${BBI_VENV:-.venv-bbi}"
BBI_PYTHON="${BBI_PYTHON:-3.11.13}"
if [[ "$BBI_VENV" = /* ]]; then
    BBI_VENV_DIR="$BBI_VENV"
else
    BBI_VENV_DIR="$SCRIPT_DIR/$BBI_VENV"
fi

echo "=== Creating BBI benchmark venv ($BBI_VENV, Python $BBI_PYTHON) ==="
uv venv --python "$BBI_PYTHON" "$BBI_VENV_DIR"
uv pip install --python "$BBI_VENV_DIR/bin/python" \
    polars-bio==0.34.0 polars==1.40.1 pyarrow==24.0.0 \
    psutil==7.2.2 matplotlib==3.10.9 maturin==1.13.3

# Replace the release wheel with an unreleased checkout for candidate runs.
if [ -n "${POLARS_BIO_SOURCE:-}" ]; then
    echo "=== Building polars-bio from: $POLARS_BIO_SOURCE ==="
    POLARS_BIO_RUSTFLAGS="${POLARS_BIO_RUSTFLAGS:--C target-cpu=native}"
    (
        unset CONDA_PREFIX
        source "$BBI_VENV_DIR/bin/activate"
        cd "$POLARS_BIO_SOURCE"
        RUSTFLAGS="$POLARS_BIO_RUSTFLAGS" maturin develop --release --locked --uv
    )
fi

echo "=== BBI benchmark packages ==="
uv pip list --python "$BBI_VENV_DIR/bin/python"
