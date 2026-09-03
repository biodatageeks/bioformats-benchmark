"""Shared configuration for Cooler (.cool/.mcool) benchmarks.

The dataset is the Krietenstein et al. 2021 HFF Micro-C ``test.mcool``
(hg38, chr2 + chr17) published by the open2c project — the canonical
cooltools test dataset. ``setup.sh`` downloads and checksum-verifies it.

Workloads (COOL_WORKLOAD):
- ``stream_count``: count all pixels without materializing the table.
- ``collect_all``: materialize the full joined pixels table
  (chrom1, start1, end1, chrom2, start2, end2, count) as a Polars DataFrame.
- ``region``: materialize the joined pixels of a 20 Mb genomic box
  (both axes constrained), the cooler ``matrix().fetch`` equivalent.
"""

import os

MCOOL_PATH = os.environ.get(
    "MCOOL_PATH", "/Users/mwiewior/research/data/COOL/test.mcool"
)

# Stored resolutions: 1000, 10000, 100000, 1000000.
COOL_RESOLUTION = int(os.environ.get("COOL_RESOLUTION", "10000"))

COOL_WORKLOAD = os.environ.get("COOL_WORKLOAD", "stream_count")
COOL_WORKLOADS = ("stream_count", "collect_all", "region")

# 0-based half-open, aligned to every stored bin size.
REGION = ("chr2", 20_000_000, 40_000_000)

# Chunk size (pixel rows) for the cooler chunked-pandas baseline.
COOL_CHUNK_ROWS = int(os.environ.get("COOL_CHUNK_ROWS", "10000000"))

JOINED_COLUMNS = ["chrom1", "start1", "end1", "chrom2", "start2", "end2", "count"]


def cooler_uri() -> str:
    return f"{MCOOL_PATH}::/resolutions/{COOL_RESOLUTION}"


def workload_name(library: str, threads: int) -> str:
    return f"{library}_cool_{COOL_WORKLOAD}_r{COOL_RESOLUTION}_t{threads}"
