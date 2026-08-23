#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BBI_VENV="${BBI_VENV:-.venv-bbi}"
BBI_PYTHON="${BBI_PYTHON:-3.11.13}"
POLARS_BIO_REF="${POLARS_BIO_REF-f32af9416139a8bc9f1565b61b13bad3af738a39}"
DATAFUSION_BIO_FORMATS_REF="${DATAFUSION_BIO_FORMATS_REF-d0a23b59271e697c78f421c70a2e48a43cb89a73}"
BIGTOOLS_REF="${BIGTOOLS_REF-0d7a5728eb39ee97fddef59cd3da469186bec90d}"
POLARS_BIO_PATCH="${POLARS_BIO_PATCH-$SCRIPT_DIR/benchmarks/polars_bio_issue_443.patch}"
export POLARS_BIO_BUILD_PROFILE="${POLARS_BIO_BUILD_PROFILE:-release}"
export POLARS_BIO_RUSTFLAGS="${POLARS_BIO_RUSTFLAGS:--C target-cpu=native}"
export POLARS_BIO_REF DATAFUSION_BIO_FORMATS_REF BIGTOOLS_REF POLARS_BIO_PATCH
if [ "$POLARS_BIO_BUILD_PROFILE" != "release" ]; then
    echo "POLARS_BIO_BUILD_PROFILE must be release" >&2
    exit 1
fi
if [[ "$BBI_VENV" = /* ]]; then
    BBI_VENV_DIR="$BBI_VENV"
else
    BBI_VENV_DIR="$SCRIPT_DIR/$BBI_VENV"
fi

echo "=== Creating BBI benchmark venv ($BBI_VENV, Python $BBI_PYTHON) ==="
uv venv --python "$BBI_PYTHON" "$BBI_VENV_DIR"
uv pip install --python "$BBI_VENV_DIR/bin/python" \
    polars-bio==0.34.0 polars==1.40.1 pyarrow==24.0.0 \
    psutil==7.2.2 matplotlib==3.10.9 maturin==1.13.3 pytest==8.4.2

# Replace the release wheel with an unreleased checkout for candidate runs.
if [ -n "${POLARS_BIO_SOURCE:-}" ]; then
    echo "=== Building polars-bio from: $POLARS_BIO_SOURCE ==="
    actual_polars_bio_ref="$(git -C "$POLARS_BIO_SOURCE" rev-parse HEAD)"
    if [ "$actual_polars_bio_ref" != "$POLARS_BIO_REF" ]; then
        echo "polars-bio HEAD mismatch: $actual_polars_bio_ref != $POLARS_BIO_REF" >&2
        exit 1
    fi
    if [ -n "$(git -C "$POLARS_BIO_SOURCE" ls-files --others --exclude-standard)" ]; then
        echo "polars-bio source contains untracked files" >&2
        exit 1
    fi
    current_diff_sha256="$(git -C "$POLARS_BIO_SOURCE" diff --binary HEAD | shasum -a 256 | awk '{print $1}')"
    empty_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    if [ -n "$POLARS_BIO_PATCH" ]; then
        expected_patch_sha256="$(shasum -a 256 "$POLARS_BIO_PATCH" | awk '{print $1}')"
        if [ "$current_diff_sha256" = "$empty_sha256" ]; then
            git -C "$POLARS_BIO_SOURCE" apply --check "$POLARS_BIO_PATCH"
            git -C "$POLARS_BIO_SOURCE" apply "$POLARS_BIO_PATCH"
            current_diff_sha256="$(git -C "$POLARS_BIO_SOURCE" diff --binary HEAD | shasum -a 256 | awk '{print $1}')"
        fi
        if [ "$current_diff_sha256" != "$expected_patch_sha256" ]; then
            echo "polars-bio tracked diff does not match $POLARS_BIO_PATCH" >&2
            exit 1
        fi
    elif [ "$current_diff_sha256" != "$empty_sha256" ]; then
        echo "polars-bio source must be clean when POLARS_BIO_PATCH is empty" >&2
        exit 1
    fi
    (
        unset CONDA_PREFIX
        source "$BBI_VENV_DIR/bin/activate"
        cd "$POLARS_BIO_SOURCE"
        RUSTFLAGS="$POLARS_BIO_RUSTFLAGS" maturin develop \
            --release --locked --uv
    )
fi

echo "=== BBI benchmark packages ==="
uv pip list --python "$BBI_VENV_DIR/bin/python"
