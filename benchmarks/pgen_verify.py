"""Element-wise comparison of two PGEN readers in one process.

The benchmark runner records a SHA-256 per reader, which answers "identical or
not" but not "how different". This module loads two readers' arrays together and
reports the exact number of differing cells and the largest difference, so a
claim of zero mismatches is backed by a count rather than by a tolerance.

Set PGEN_VERIFY_SELFTEST=1 to additionally corrupt one cell of the left array
and confirm the comparison reports it. A check that cannot fail is not evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from benchmarks import pgen_matrix


def _load(reader: str) -> np.ndarray:
    """Read one array by reusing the benchmark's reader adapters."""
    matrix, positions, _samples = pgen_matrix.make_reader(reader)()
    order = np.argsort(np.ascontiguousarray(positions, dtype="<i8"), kind="stable")
    return np.ascontiguousarray(matrix[order])


def _compare(left: np.ndarray, right: np.ndarray) -> dict:
    if np.issubdtype(left.dtype, np.floating):
        both_missing = np.isnan(left) & np.isnan(right)
    else:
        # The int8 workload uses PLINK 2's -9 sentinel rather than NaN.
        both_missing = (left < 0) & (right < 0)
    differing = (left != right) & ~both_missing
    count = int(np.count_nonzero(differing))
    if count:
        delta = np.abs(
            left[differing].astype(np.float64) - right[differing].astype(np.float64)
        )
        max_abs = float(np.max(delta))
        larger = np.maximum(np.abs(left[differing]), np.abs(right[differing])).astype(
            np.float64
        )
        nonzero = larger > 0
        max_relative = (
            float(np.max(delta[nonzero] / larger[nonzero])) if nonzero.any() else 0.0
        )
    else:
        max_abs = 0.0
        max_relative = 0.0
    return {
        "value_differences": count,
        "max_abs_difference": max_abs,
        "max_relative_difference": max_relative,
    }


def main() -> None:
    left_name = os.environ["PGEN_VERIFY_LEFT"]
    right_name = os.environ["PGEN_VERIFY_RIGHT"]
    left = _load(left_name)
    right = _load(right_name)

    if left.shape != right.shape:
        raise AssertionError(f"shape mismatch: {left.shape} != {right.shape}")

    view = np.uint32 if left.dtype.itemsize == 4 else np.uint8
    bitwise = int(np.count_nonzero(left.view(view) != right.view(view)))
    result = {
        "left": left_name,
        "right": right_name,
        "mode": os.environ.get("PGEN_MODE", "dosage"),
        "path": str(Path(os.environ["PGEN_PATH"]).resolve()),
        "cells": int(left.size),
        "bitwise_differences": bitwise,
        **_compare(left, right),
    }

    if os.environ.get("PGEN_VERIFY_SELFTEST"):
        # Perturb a single cell and confirm the comparison notices. This guards
        # against a comparison that reports zero because it is comparing an
        # array with itself, or because a shape/NaN path swallowed everything.
        probe = left.copy()
        flat = probe.reshape(-1)
        index = flat.size // 2
        flat[index] = 1 if flat[index] != 1 else 2
        detected = _compare(probe, right)["value_differences"]
        result["selftest_single_cell_detected"] = int(detected)
        if detected < 1:
            raise AssertionError(
                "self-test failed: a deliberately corrupted cell was not detected, "
                "so a zero-difference result from this comparison means nothing"
            )

    print(f"PGEN_VERIFY:{json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
