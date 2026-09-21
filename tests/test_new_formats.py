"""Prove the correctness gate fails on corruption rather than accepting counts."""

import json
from pathlib import Path

import polars as pl
import pytest

from benchmarks.new_formats import compare_frames, fasta, pdb_frame, schema, stockholm
from prepare_new_formats import scale_fasta, scale_foldcomp, scale_stockholm


def test_record_scaling_preserves_case_gaps_and_descriptions(tmp_path):
    source = tmp_path / "seed.a3m"
    source.write_text(">one a long description\nACde--FG\nH\n>two\nA.-G\n")
    output = tmp_path / "large.a3m"
    scale_fasta(source, output, 3)
    frame = fasta(output)
    assert frame.height == 6
    assert frame["name"].to_list() == [
        f"copy{i}_{n}" for i in range(3) for n in ["one", "two"]
    ]
    assert frame["sequence"].to_list() == ["ACde--FGH", "A.-G"] * 3
    assert frame["description"].to_list() == ["a long description", None] * 3


def test_stockholm_scaling_preserves_interleaving_and_annotation_bags(tmp_path):
    source = tmp_path / "seed.sto"
    source.write_text(
        "# STOCKHOLM 1.0\n#=GF ID family\n#=GS seq AC accession\n"
        "seq AC-D\n#=GR seq PP 99.9\n\nseq EF\n#=GR seq PP 87\n//\n"
    )
    target = tmp_path / "large.sto"
    scale_stockholm([source], target, 3)
    frame = stockholm(target)
    assert frame["alignment_id"].to_list() == [f"copy{i}_seed_family" for i in range(3)]
    assert frame["sequence"].to_list() == ["AC-DEF"] * 3
    assert frame["gs"].to_list() == [[{"tag": "AC", "value": "accession"}]] * 3
    assert frame["gr"].to_list() == [[{"tag": "PP", "value": "99.987"}]] * 3


def test_foldcomp_scaling_rebuilds_offsets_keys_and_names(tmp_path):
    source, target = tmp_path / "seed", tmp_path / "large"
    source.write_bytes(b"FCMPone\0FCMPtwo_long\0")
    Path(str(source) + ".index").write_text("3\t0\t8\n9\t8\t13\n")
    Path(str(source) + ".lookup").write_text("3\tone\t0\n9\ttwo\t0\n")
    Path(str(source) + ".dbtype").write_bytes(b"\x00\x00\x00\x00")
    scale_foldcomp(source, target, 4)
    entries = [
        tuple(map(int, line.split()))
        for line in Path(str(target) + ".index").read_text().splitlines()
    ]
    payload = target.read_bytes()
    assert len(entries) == 8
    assert [e[0] for e in entries] == list(range(8))
    for ordinal, (_, offset, length) in enumerate(entries):
        assert (
            payload[offset : offset + length]
            == [b"FCMPone\0", b"FCMPtwo_long\0"][ordinal % 2]
        )
    assert (
        Path(str(target) + ".lookup").read_text().splitlines()[-1] == "7\tcopy3_two\t0"
    )


@pytest.fixture
def alignment():
    return pl.DataFrame(
        {
            "name": ["a", "b"],
            "description": [None, "description"],
            "sequence": ["Ac.-", "A--G"],
        },
        schema=schema("a3m"),
    )


def test_order_independent_complete_comparison(alignment):
    assert compare_frames(alignment.reverse(), alignment, "a3m")["mismatches"] == 0


def test_pdb_oracle_restores_repeated_chain_source_order(tmp_path):
    path = tmp_path / "chains.pdb"
    path.write_text(
        "ATOM      1  CA  ALA A   1       1.000   2.000   3.000  1.00 10.00           C  \n"
        "TER\n"
        "ATOM      3  CA  ALA B   1       4.000   5.000   6.000  1.00 10.00           C  \n"
        "TER\n"
        "HETATM    5 MG    MG A   2       7.000   8.000   9.000  1.00 10.00          MG  \n"
        "END\n"
    )
    frame = pdb_frame(path, 0)
    assert frame["chain_id"].to_list() == ["A", "B", "A"]
    assert frame["atom_index"].to_list() == [0, 1, 2]
    assert frame["x"].to_list() == [1.0, 4.0, 7.0]


@pytest.mark.parametrize(
    "mutation", ["sequence", "identity", "null", "drop", "duplicate", "type"]
)
def test_gate_rejects_corruption(alignment, mutation):
    if mutation == "sequence":
        changed = alignment.with_columns(pl.col("sequence").str.to_uppercase())
    elif mutation == "identity":
        changed = alignment.with_columns(pl.lit("wrong").alias("name"))
    elif mutation == "null":
        changed = alignment.with_columns(pl.col("description").fill_null(""))
    elif mutation == "drop":
        changed = alignment.head(1)
    elif mutation == "duplicate":
        changed = pl.concat([alignment.head(1)] * 2)
    else:
        changed = alignment.with_columns(pl.col("name").cast(pl.Categorical))
    with pytest.raises(AssertionError):
        compare_frames(changed, alignment, "a3m")


def atom_frame():
    row = {name: None for name in schema("pdb")}
    row.update(
        source_index=0,
        entry_index=0,
        atom_index=0,
        model_id=1,
        chain_id="A",
        auth_seq_id="1",
        atom_name="CA",
        residue_name="ALA",
        x=1.0,
        y=2.0,
        z=3.0,
    )
    return pl.DataFrame([row], schema=schema("pdb"))


@pytest.mark.parametrize("value", [1.01, float("nan"), float("inf"), None])
def test_gate_rejects_coordinate_and_finite_mask_corruption(value):
    expected = atom_frame()
    changed = expected.with_columns(pl.lit(value, dtype=pl.Float64).alias("x"))
    with pytest.raises(AssertionError):
        compare_frames(changed, expected, "pdb")


def test_annotations_are_compared():
    row = {
        "alignment_id": "a",
        "name": "seq",
        "sequence": "AC",
        "gs": [{"tag": "AC", "value": "X"}],
        "gr": [{"tag": "PP", "value": "99"}],
    }
    expected = pl.DataFrame([row], schema=schema("sto"))
    changed = pl.DataFrame(
        [{**row, "gr": [{"tag": "PP", "value": "98"}]}], schema=schema("sto")
    )
    with pytest.raises(AssertionError, match="gr"):
        compare_frames(changed, expected, "sto")


def test_source_pins_cover_all_six_formats():
    pins = json.loads(
        (Path(__file__).parents[1] / "new_formats_sources.json").read_text()
    )
    assert all(len(digest) == 64 for digest in pins.values())
    assert {
        "io/msa/query.a3m",
        "io/msa/query_dotted.a2m",
        "io/msa/RF00001.sto",
        "structure/1ubq.pdb",
        "structure/1ubq.cif",
        "structure/example_db",
    } <= pins.keys()
