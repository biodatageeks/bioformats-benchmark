"""Independent readers producing identical, explicitly typed benchmark tables.

No expected value is obtained from polars-bio. Sorting and comparison happen
outside the measured read-to-DataFrame interval. No rows are sampled or joined
away, including alternate sites, HETATM records and repeated sources.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

BAG = pl.List(pl.Struct({"tag": pl.String, "value": pl.String}))
MSA_SCHEMA = {"name": pl.String, "description": pl.String, "sequence": pl.String}
STO_SCHEMA = {
    "alignment_id": pl.String,
    "name": pl.String,
    "sequence": pl.String,
    "gs": BAG,
    "gr": BAG,
}
ATOM_SCHEMA = {
    "source_index": pl.UInt64,
    "entry_index": pl.UInt64,
    "atom_index": pl.UInt64,
    "model_id": pl.Int32,
    "chain_id": pl.String,
    "auth_seq_id": pl.String,
    "insertion_code": pl.String,
    "atom_name": pl.String,
    "residue_name": pl.String,
    "alt_id": pl.String,
    "x": pl.Float64,
    "y": pl.Float64,
    "z": pl.Float64,
    "occupancy": pl.Float64,
    "b_factor": pl.Float64,
}
CIF_EXTRA = {
    "atom_id": pl.String,
    "record_type": pl.String,
    "auth_asym_id": pl.String,
    "label_asym_id": pl.String,
    "label_entity_id": pl.String,
    "label_seq_id": pl.Int64,
    "auth_atom_id": pl.String,
    "label_atom_id": pl.String,
    "auth_comp_id": pl.String,
    "label_comp_id": pl.String,
    "element": pl.String,
    "formal_charge": pl.Int32,
}
FC_EXTRA = {"entry_key": pl.UInt64, "entry_name": pl.String}


def schema(fmt):
    if fmt in ("a2m", "a3m"):
        return MSA_SCHEMA
    if fmt == "sto":
        return STO_SCHEMA
    return ATOM_SCHEMA | (
        CIF_EXTRA if fmt == "mmcif" else FC_EXTRA if fmt == "foldcomp" else {}
    )


def sort_columns(fmt):
    if fmt in ("a2m", "a3m"):
        return ["name"]
    if fmt == "sto":
        return ["alignment_id", "name"]
    return ["source_index", "entry_index", "atom_index"]


def fasta(path):
    from Bio import SeqIO

    with path.open() as handle:
        rows = [
            (
                r.id,
                r.description.split(maxsplit=1)[1]
                if len(r.description.split(maxsplit=1)) == 2
                else None,
                str(r.seq),
            )
            for r in SeqIO.parse(handle, "fasta")
        ]
    return pl.DataFrame(rows, schema=MSA_SCHEMA, orient="row")


def stockholm_annotations(path):
    """Literal GS/GR lines retain tags that Easel's high-level API omits."""
    gs, gr = defaultdict(list), defaultdict(dict)
    with path.open() as handle:
        for line in handle:
            if line.startswith("#=GS"):
                _, name, tag, value = line.rstrip("\n").split(maxsplit=3)
                gs[name].append({"tag": tag, "value": value})
            elif line.startswith("#=GR"):
                _, name, tag, value = line.split()
                gr[name][tag] = gr[name].get(tag, "") + value
            elif line.strip() == "//":
                yield gs, gr
                gs, gr = defaultdict(list), defaultdict(dict)
    if gs or gr:
        raise ValueError("unterminated Stockholm annotations")


def stockholm(path):
    from pyhmmer.easel import MSAFile

    def text(value):
        return value.decode() if isinstance(value, bytes) else value

    rows = []
    annotations = stockholm_annotations(path)
    with MSAFile(str(path), format="stockholm") as handle:
        for ordinal, msa in enumerate(handle):
            gs, gr = next(annotations)
            alignment_id = text(msa.name or msa.accession) or str(ordinal)
            for name, sequence in zip(msa.names, msa.alignment, strict=True):
                name = text(name)
                rows.append(
                    (
                        alignment_id,
                        name,
                        str(sequence),
                        gs.get(name) or None,
                        [{"tag": k, "value": v} for k, v in gr.get(name, {}).items()]
                        or None,
                    )
                )
    if next(annotations, None) is not None:
        raise ValueError("Easel omitted an alignment")
    return pl.DataFrame(rows, schema=STO_SCHEMA, orient="row")


def gemmi_atoms(
    structure, source_index=0, entry_index=0, raw=None, include_serial=False
):
    """Gemmi keeps PDB coordinates in Float64, unlike Biopython PDBParser."""
    ordinal = 0
    residue_ordinal = 0
    for model in structure:
        for chain in model:
            for residue in chain:
                for atom in residue:
                    xyz = (
                        raw["coordinates"][ordinal]
                        if raw
                        else (atom.pos.x, atom.pos.y, atom.pos.z)
                    )
                    yield {
                        **({"_serial": atom.serial} if include_serial else {}),
                        "source_index": source_index,
                        "entry_index": entry_index,
                        "atom_index": ordinal,
                        "model_id": model.num,
                        "chain_id": chain.name,
                        "auth_seq_id": str(residue.seqid.num),
                        "insertion_code": residue.seqid.icode.strip() or None,
                        "atom_name": atom.name,
                        "residue_name": residue.name,
                        "alt_id": None if atom.altloc == "\0" else atom.altloc,
                        "x": xyz[0],
                        "y": xyz[1],
                        "z": xyz[2],
                        # PDB serializes these at two decimal places. Gemmi stores
                        # them in Float32; round back to the file's decimal precision.
                        "occupancy": None if raw else round(atom.occ, 2),
                        "b_factor": raw["b_factors"][residue_ordinal]
                        if raw
                        else round(atom.b_iso, 2),
                    }
                    ordinal += 1
                residue_ordinal += 1
    if raw and ordinal != len(raw["coordinates"]):
        raise ValueError("Foldcomp PDB and raw coordinate counts differ")


def pdb_frame(path, source_index):
    import gemmi

    # Gemmi merges repeated chain parts (e.g. chain 0's terminal magnesium
    # ions in 1JJ2). Restore record order using PDB model/serial identities,
    # never polars-bio's output or coordinate values.
    atoms = {}
    for row in gemmi_atoms(
        gemmi.read_structure(str(path)), source_index, include_serial=True
    ):
        key = (row["model_id"], row.pop("_serial"))
        if key in atoms:
            raise ValueError(f"ambiguous Gemmi model/serial identity: {key}")
        atoms[key] = row
    rows, model = [], 1
    with path.open() as handle:
        for line in handle:
            if line.startswith("MODEL "):
                model = int(line[10:14])
            elif line.startswith(("ATOM  ", "HETATM")):
                row = atoms.pop((model, int(line[6:11])))
                row["atom_index"] = len(rows)
                rows.append(row)
    if atoms:
        raise ValueError("Gemmi emitted atoms absent from the PDB records")
    return pl.DataFrame(rows, schema=ATOM_SCHEMA)


CIF_TAGS = {
    "model_id": "pdbx_PDB_model_num",
    "auth_asym_id": "auth_asym_id",
    "label_asym_id": "label_asym_id",
    "label_entity_id": "label_entity_id",
    "auth_seq_id": "auth_seq_id",
    "label_seq_id": "label_seq_id",
    "insertion_code": "pdbx_PDB_ins_code",
    "atom_id": "id",
    "record_type": "group_PDB",
    "auth_atom_id": "auth_atom_id",
    "label_atom_id": "label_atom_id",
    "auth_comp_id": "auth_comp_id",
    "label_comp_id": "label_comp_id",
    "alt_id": "label_alt_id",
    "element": "type_symbol",
    "x": "Cartn_x",
    "y": "Cartn_y",
    "z": "Cartn_z",
    "occupancy": "occupancy",
    "b_factor": "B_iso_or_equiv",
    "formal_charge": "pdbx_formal_charge",
}


def cif_frame(path, source_index, reader="gemmi"):
    if reader == "biopython":
        from Bio.PDB.MMCIF2Dict import MMCIF2Dict

        raw = MMCIF2Dict(str(path))
        columns = {
            tag: [
                None if v in (".", "?") else v for v in raw.get("_atom_site." + tag, [])
            ]
            for tag in CIF_TAGS.values()
        }
    else:
        import gemmi

        block = gemmi.cif.read(str(path)).sole_block()
        # get_mmcif_category distinguishes quoted strings from unquoted nulls.
        category = block.get_mmcif_category("_atom_site.")
        columns = {
            tag: [None if v is False or v is None else v for v in category.get(tag, [])]
            for tag in CIF_TAGS.values()
        }
    count = len(columns["Cartn_x"])
    data = {}
    for name, tag in CIF_TAGS.items():
        values = columns[tag] or [None] * count
        data[name] = pl.Series(name, values, dtype=pl.String).cast(
            schema("mmcif")[name]
        )
    frame = pl.DataFrame(data).with_columns(
        pl.lit(source_index, dtype=pl.UInt64).alias("source_index"),
        pl.lit(0, dtype=pl.UInt64).alias("entry_index"),
        pl.int_range(0, count, dtype=pl.UInt64).alias("atom_index"),
        pl.coalesce("auth_asym_id", "label_asym_id").alias("chain_id"),
        pl.coalesce("label_atom_id", "auth_atom_id").alias("atom_name"),
        pl.coalesce("label_comp_id", "auth_comp_id").alias("residue_name"),
    )
    return frame.select(list(schema("mmcif")))


def foldcomp_frame(path):
    import foldcomp
    import gemmi

    lookup = {
        int(parts[0]): parts[1]
        for line in Path(str(path) + ".lookup").read_text().splitlines()
        if (parts := line.split())
    }
    frames = []
    with path.open("rb") as handle:
        for ordinal, line in enumerate(
            Path(str(path) + ".index").read_text().splitlines()
        ):
            key, offset, length = map(int, line.split())
            handle.seek(offset)
            payload = handle.read(length)
            # Database records may have the ffindex terminating NUL.
            _, pdb_text = foldcomp.decompress(payload)
            raw = foldcomp.get_data(payload)
            structure = gemmi.read_pdb_string(pdb_text)
            rows = [
                {**row, "entry_key": key, "entry_name": lookup[key]}
                for row in gemmi_atoms(structure, entry_index=ordinal, raw=raw)
            ]
            frames.append(pl.DataFrame(rows, schema=schema("foldcomp")))
    return pl.concat(frames)


def read_frame(case, root, reader, threads):
    fmt = case["format"]
    paths = [root / name for name in case["paths"]]
    if reader == "polars-bio":
        import polars_bio as pb

        pb.set_option("datafusion.execution.target_partitions", str(threads))
        source = [str(p) for p in paths] if fmt in ("pdb", "mmcif") else str(paths[0])
        return getattr(pb, "scan_" + fmt)(source).select(list(schema(fmt))).collect()
    if fmt in ("a2m", "a3m"):
        return fasta(paths[0])
    if fmt == "sto":
        return stockholm(paths[0])
    if fmt == "mmcif":
        return pl.concat([cif_frame(p, i, reader) for i, p in enumerate(paths)])
    if fmt == "pdb":
        return pl.concat([pdb_frame(p, i) for i, p in enumerate(paths)])
    if fmt == "foldcomp":
        return foldcomp_frame(paths[0])
    raise ValueError(fmt)


def compare_frames(actual, expected, fmt):
    """Fail on any type, population, identity, null-mask or value mismatch."""
    if actual.schema != expected.schema or actual.schema != pl.Schema(schema(fmt)):
        raise AssertionError(
            f"{fmt}: schema mismatch: {actual.schema} != {expected.schema}"
        )
    if actual.height != expected.height or not actual.height:
        raise AssertionError(
            f"{fmt}: row count mismatch/empty: {actual.height} != {expected.height}"
        )
    keys = sort_columns(fmt)
    # Keys must be unique in these generated datasets; otherwise sorting alone
    # could hide a dropped row replaced with a duplicate.
    for frame in (actual, expected):
        if frame.select(keys).n_unique() != frame.height:
            raise AssertionError(f"{fmt}: duplicate identity keys")
    actual, expected = actual.sort(keys), expected.sort(keys)
    maximum_errors = {}
    for column, dtype in expected.schema.items():
        left, right = actual[column], expected[column]
        if not left.is_null().equals(right.is_null()):
            raise AssertionError(f"{fmt}.{column}: null-mask mismatch")
        if dtype.is_float():
            a, b = left.drop_nulls().to_numpy(), right.drop_nulls().to_numpy()
            if not np.isfinite(a).all() or not np.isfinite(b).all():
                raise AssertionError(f"{fmt}.{column}: nonfinite value")
            error = float(np.max(np.abs(a - b), initial=0))
            maximum_errors[column] = error
            # Fixed before running: native-vs-official Foldcomp Float32 decode
            # agrees within 1e-4 A (the upstream fixture contract). Text: 1e-9.
            tolerance = 1e-4 if fmt == "foldcomp" else 1e-9
            if error > tolerance:
                raise AssertionError(f"{fmt}.{column}: max error {error} > {tolerance}")
        elif not left.equals(right):
            differing = [
                i
                for i, (a, b) in enumerate(
                    zip(left.to_list(), right.to_list(), strict=True)
                )
                if a != b
            ]
            i = differing[0]
            raise AssertionError(
                f"{fmt}.{column}: {len(differing)} mismatches; row {i}: {left[i]!r} != {right[i]!r}"
            )
    return {
        "rows": actual.height,
        "columns": actual.width,
        "cells_checked": actual.height * actual.width,
        "mismatches": 0,
        "max_absolute_errors": maximum_errors,
    }
