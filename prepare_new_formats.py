#!/usr/bin/env python3
"""Prepare checksum-pinned real inputs and deterministic, format-aware scaling."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import shutil
import urllib.request
from pathlib import Path

REVISION = "29b04c5159135319d387dfcda9b7b84420dea549"
RAW = f"https://raw.githubusercontent.com/biodatageeks/polars-bio/{REVISION}/tests/data"
LARGE = {
    "1jj2.pdb": (
        "https://files.rcsb.org/download/1JJ2.pdb.gz",
        "f3eeb133fedd51dfb3d98b50c338c76bb1e7c5474416744eccd8862a7b7308bb",
    ),
    "4v9d.cif": (
        "https://files.rcsb.org/download/4V9D.cif.gz",
        "08e7f999b088c75dfec79b8cc5e5f9abf1b669a9bdc82d92da97d4c78dbce69d",
    ),
}


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch(url, path, expected):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with urllib.request.urlopen(url, timeout=120) as response:
            data = response.read()
        if url.endswith(".gz"):
            data = gzip.decompress(data)
        temporary = path.with_suffix(path.suffix + ".download")
        temporary.write_bytes(data)
        if sha256(temporary) != expected:
            raise ValueError(f"download checksum mismatch: {url}")
        temporary.replace(path)
    if sha256(path) != expected:
        raise ValueError(f"input checksum mismatch: {path}")


def scale_fasta(source, target, copies):
    """Keep sequence bytes/case/gaps intact; make record IDs unique."""
    lines = source.read_text().splitlines(keepends=True)
    with target.open("w") as out:
        for copy in range(copies):
            for line in lines:
                out.write(f">copy{copy}_" + line[1:] if line.startswith(">") else line)
            out.write("\n")


def scale_stockholm(sources, target, copies):
    """Concatenate complete alignments including interleaved annotation blocks."""
    with target.open("w") as out:
        for copy in range(copies):
            for source in sources:
                for line in source.read_text().splitlines(keepends=True):
                    if line.startswith("#=GF ID"):
                        line = (
                            f"#=GF ID copy{copy}_{source.stem}_"
                            + line.split(maxsplit=2)[2]
                        )
                    out.write(line)
                out.write("\n")


def scale_foldcomp(source, target, copies):
    """Rebuild the index and lookup; never concatenate binary DBs blindly."""
    payload = source.read_bytes()
    index = [
        tuple(map(int, line.split()))
        for line in Path(str(source) + ".index").read_text().splitlines()
    ]
    lookup = {
        int(parts[0]): parts[1]
        for line in Path(str(source) + ".lookup").read_text().splitlines()
        if (parts := line.split())
    }
    with (
        target.open("wb") as out,
        Path(str(target) + ".index").open("w") as idx,
        Path(str(target) + ".lookup").open("w") as names,
    ):
        for copy in range(copies):
            for ordinal, (key, offset, length) in enumerate(index):
                new_key = copy * len(index) + ordinal
                record = payload[offset : offset + length]
                if len(record) != length or not record.startswith(b"FCMP"):
                    raise ValueError("invalid source Foldcomp index")
                idx.write(f"{new_key}\t{out.tell()}\t{length}\n")
                names.write(f"{new_key}\tcopy{copy}_{lookup[key]}\t0\n")
                out.write(record)
    shutil.copyfile(str(source) + ".dbtype", str(target) + ".dbtype")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/new_formats"))
    parser.add_argument("--target-mib", type=float, default=64)
    parser.add_argument("--foldcomp-copies", type=int, default=32)
    parser.add_argument(
        "--small",
        action="store_true",
        help="use one copy of each small fixture for tests",
    )
    args = parser.parse_args()
    if args.target_mib <= 0 or args.foldcomp_copies <= 0:
        parser.error("sizes must be positive")
    root = args.data_dir.resolve()
    sources = root / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    pins = json.loads(Path(__file__).with_name("new_formats_sources.json").read_text())
    provenance = []
    for relative, expected in pins.items():
        path = sources / Path(relative).name
        url = f"{RAW}/{relative}"
        fetch(url, path, expected)
        provenance.append(
            {"path": str(path.relative_to(root)), "url": url, "sha256": expected}
        )
    if not args.small:
        for name, (url, expected) in LARGE.items():
            fetch(url, sources / name, expected)
            provenance.append(
                {"path": f"sources/{name}", "url": url, "sha256": expected}
            )
    target_bytes = args.target_mib * 1024**2
    cases = []
    for fmt, name in [("a2m", "query_dotted.a2m"), ("a3m", "query.a3m")]:
        src = sources / name
        copies = 1 if args.small else math.ceil(target_bytes / src.stat().st_size)
        dest = root / f"scaled.{fmt}"
        scale_fasta(src, dest, copies)
        cases.append(
            {
                "format": fmt,
                "paths": [dest.name],
                "copies": copies,
                "oracle": "biopython",
            }
        )
    sto_sources = [
        sources / name
        for name in ["PF00001.sto", "RF00001.sto", "PF00001_hmmalign.sto"]
    ]
    copies = (
        1
        if args.small
        else math.ceil(target_bytes / sum(p.stat().st_size for p in sto_sources))
    )
    scale_stockholm(sto_sources, root / "scaled.sto", copies)
    cases.append(
        {"format": "sto", "paths": ["scaled.sto"], "copies": copies, "oracle": "easel"}
    )
    for fmt, name in [
        ("pdb", "1ubq.pdb" if args.small else "1jj2.pdb"),
        ("mmcif", "1ubq.cif" if args.small else "4v9d.cif"),
    ]:
        path = sources / name
        copies = 1 if args.small else math.ceil(target_bytes / path.stat().st_size)
        cases.append(
            {
                "format": fmt,
                "paths": [f"sources/{name}"] * copies,
                "copies": copies,
                "oracle": "gemmi",
            }
        )
    copies = 1 if args.small else args.foldcomp_copies
    scale_foldcomp(sources / "example_db", root / "scaled.foldcomp", copies)
    cases.append(
        {
            "format": "foldcomp",
            "paths": ["scaled.foldcomp"],
            "copies": copies,
            "oracle": "foldcomp",
        }
    )
    for case in cases:
        paths = list(dict.fromkeys(case["paths"]))
        if case["format"] == "foldcomp":
            paths += [
                case["paths"][0] + suffix for suffix in [".index", ".lookup", ".dbtype"]
            ]
        case["files"] = [
            {
                "path": name,
                "bytes": (root / name).stat().st_size,
                "sha256": sha256(root / name),
            }
            for name in paths
        ]
        case["logical_bytes"] = sum(
            (root / name).stat().st_size for name in case["paths"]
        )
    manifest = {
        "schema_version": 1,
        "fixture_revision": REVISION,
        "small": args.small,
        "sources": provenance,
        "cases": cases,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(root / "manifest.json")


if __name__ == "__main__":
    main()
