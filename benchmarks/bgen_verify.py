"""Element-wise comparison of two BGEN readers in one process.

The benchmark runner records a SHA-256 per reader, which answers "identical or
not" but not "how different". This module loads two readers' arrays together and
reports the exact number of differing cells and the largest difference, so a
claim of zero mismatches is backed by a count rather than by a tolerance.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from benchmarks import bgen_matrix

READERS = ("polars-bio", "snputils", "bgen", "pysnptools")


def _load(reader: str) -> np.ndarray:
    """Read one array by reusing the benchmark's reader adapters."""
    matrix, positions, _samples, _mode = bgen_matrix.make_reader(reader)()
    order = np.argsort(np.ascontiguousarray(positions, dtype="<i8"), kind="stable")
    return np.ascontiguousarray(matrix[order])


def main() -> None:
    left_name = os.environ["BGEN_VERIFY_LEFT"]
    right_name = os.environ["BGEN_VERIFY_RIGHT"]
    left = _load(left_name)
    right = _load(right_name)

    if left.shape != right.shape:
        raise AssertionError(f"shape mismatch: {left.shape} != {right.shape}")

    bitwise = int(
        np.count_nonzero(left.view(np.uint32) != right.view(np.uint32))
    )
    both_nan = np.isnan(left) & np.isnan(right)
    differing = (left != right) & ~both_nan
    value_differences = int(np.count_nonzero(differing))
    if value_differences:
        delta = np.abs(left[differing].astype(np.float64) - right[differing].astype(np.float64))
        max_abs = float(np.max(delta))
        # Relative difference is only meaningful where the compared value is not
        # zero; a difference against zero is reported by max_abs alone.
        larger = np.maximum(np.abs(left[differing]), np.abs(right[differing])).astype(
            np.float64
        )
        nonzero = larger > 0
        max_relative = float(np.max(delta[nonzero] / larger[nonzero])) if nonzero.any() else 0.0
    else:
        max_abs = 0.0
        max_relative = 0.0

    result = {
        "left": left_name,
        "right": right_name,
        "workload": os.environ.get("BGEN_MODE", "dosage"),
        "path": str(Path(os.environ["BGEN_PATH"]).resolve()),
        "cells": int(left.size),
        "bitwise_differences": bitwise,
        "value_differences": value_differences,
        "max_abs_difference": max_abs,
        "max_relative_difference": max_relative,
    }
    print(f"BGEN_VERIFY:{json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
