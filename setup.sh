#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# === Main venv (current polars-bio + pinned deps, 2026-07 benchmark run) ===

echo "=== Creating main venv (.venv) with Python 3.12 ==="
uv venv --python 3.12 .venv

echo "=== Installing benchmark dependencies (pinned to the 2026-07 run) ==="
# NOTE: the allocator-regression fix (mimalloc default, biodatageeks/polars-bio#402)
# landed after 0.32.0. To reproduce the 2026-07 BAM/VCF numbers, build polars-bio
# from source with that fix (or use the next release once published) rather than the
# PyPI 0.32.0 wheel below.
uv pip install --python .venv/bin/python \
    polars-bio==0.32.0 \
    "snputils @ git+https://github.com/AI-sandbox/snputils.git@bdb1a56b52a6b16210d60e347d33d023dc98352f" \
    pysam==0.24.0 PyVCF3==1.0.4 cyvcf2==0.31.4 oxbow==0.8.1 biobear==0.23.7 \
    bgen==1.10.0 "pysnptools[bgen]==0.5.15" \
    polars==1.42.1 pyarrow==24.0.0 \
    psutil matplotlib notebook maturin

# Set POLARS_BIO_SOURCE to benchmark an unreleased checkout (for example, the
# feat/bcf-pr218-bench branch). The release wheel above is replaced in-place.
if [ -n "${POLARS_BIO_SOURCE:-}" ]; then
    echo "=== Building polars-bio from: $POLARS_BIO_SOURCE ==="
    POLARS_BIO_RUSTFLAGS="${POLARS_BIO_RUSTFLAGS:--C target-cpu=native}"
    (
        unset CONDA_PREFIX
        source "$SCRIPT_DIR/.venv/bin/activate"
        cd "$POLARS_BIO_SOURCE"
        RUSTFLAGS="$POLARS_BIO_RUSTFLAGS" maturin develop --release --locked
    )
fi

echo "=== Main venv packages ==="
uv pip list --python .venv/bin/python

# === Baseline venv (polars-bio 0.22.0 from PyPI) ===

echo ""
echo "=== Creating baseline venv (.venv-baseline) with Python 3.12 ==="
uv venv --python 3.12 .venv-baseline

echo "=== Installing polars-bio 0.22.0 from PyPI ==="
uv pip install --python .venv-baseline/bin/python \
    polars-bio==0.22.0 polars pyarrow psutil

echo "=== Baseline venv packages ==="
uv pip list --python .venv-baseline/bin/python

# === Data downloads ===

# BAM: expected at /Users/mwiewior/research/data/WES/NA12878.proper.wes.md.chr1.bam
# (user must provide this file — extracted from full WES BAM with samtools)

# VCF: Ensembl chr1 variation
VCF_DIR="/Users/mwiewior/research/data/VCF"
VCF_FILE="${VCF_DIR}/homo_sapiens-chr1.vcf.gz"
if [ ! -f "$VCF_FILE" ]; then
    echo ""
    echo "=== Downloading VCF (Ensembl chr1 variation) ==="
    mkdir -p "$VCF_DIR"
    curl -L -o "$VCF_FILE" \
        "https://ftp.ensembl.org/pub/current_variation/vcf/homo_sapiens/homo_sapiens-chr1.vcf.gz"
    curl -L -o "${VCF_FILE}.tbi" \
        "https://ftp.ensembl.org/pub/current_variation/vcf/homo_sapiens/homo_sapiens-chr1.vcf.gz.tbi" \
        2>/dev/null || echo "  (no .tbi index available, skipping)"
    echo "  VCF downloaded to: $VCF_FILE"
else
    echo "=== VCF already exists: $VCF_FILE (skipping download) ==="
fi

# BCF: phased, biallelic GRCh38 chromosome 22 callset used by snputils.
BCF_DIR="/Users/mwiewior/research/data/BCF"
BCF_VCF_FILE="${BCF_DIR}/ALL.chr22.phased.vcf.gz"
BCF_FILE="${BCF_DIR}/ALL.chr22.phased.bcf"
BCF_URL="https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/data_collections/1000_genomes_project/release/20190312_biallelic_SNV_and_INDEL/ALL.chr22.shapeit2_integrated_snvindels_v2a_27022019.GRCh38.phased.vcf.gz"
BCF_VCF_SHA256="b428192af4f02507585c3775e59251974c71a968daa895a9a47acb337140614c"
if [ ! -f "$BCF_VCF_FILE" ]; then
    echo ""
    echo "=== Downloading 1000 Genomes chromosome 22 source VCF ==="
    mkdir -p "$BCF_DIR"
    curl -L -o "$BCF_VCF_FILE" "$BCF_URL"
else
    echo "=== BCF source VCF already exists: $BCF_VCF_FILE (skipping download) ==="
fi

actual_bcf_vcf_sha256="$("$SCRIPT_DIR/.venv/bin/python" -c \
    'import hashlib, sys; print(hashlib.file_digest(open(sys.argv[1], "rb"), "sha256").hexdigest())' \
    "$BCF_VCF_FILE")"
if [ "$actual_bcf_vcf_sha256" != "$BCF_VCF_SHA256" ]; then
    echo "BCF source VCF checksum mismatch: $actual_bcf_vcf_sha256" >&2
    exit 1
fi

if [ ! -f "$BCF_FILE" ]; then
    command -v bcftools >/dev/null || {
        echo "bcftools is required to create the BCF benchmark fixture" >&2
        exit 1
    }
    echo "=== Converting chromosome 22 VCF to BCF ==="
    bcftools view --threads "${THREAD_NUM:-8}" -Ob -o "$BCF_FILE" "$BCF_VCF_FILE"
fi

if [ ! -f "${BCF_FILE}.csi" ]; then
    bcftools index "$BCF_FILE"
fi

# Reader matrix: a deterministic 25,000-variant, 2,548-sample slice. Keeping
# the wide cohort while bounding the row count makes the pure-Python readers
# feasible and preserves 63.7 million directly comparable GT dosage cells.
GENOTYPE_REGION="chr22:10516173-16717478"
GENOTYPE_VCF_FILE="${BCF_DIR}/ALL.chr22.phased.first-25000.vcf.gz"
GENOTYPE_BCF_FILE="${BCF_DIR}/ALL.chr22.phased.first-25000.bcf"
if [ ! -f "$GENOTYPE_VCF_FILE" ]; then
    bcftools view --threads 1 -r "$GENOTYPE_REGION" -Oz \
        -o "$GENOTYPE_VCF_FILE" "$BCF_FILE"
fi
if [ ! -f "${GENOTYPE_VCF_FILE}.tbi" ]; then
    bcftools index --threads 1 -t "$GENOTYPE_VCF_FILE"
fi
if [ ! -f "$GENOTYPE_BCF_FILE" ]; then
    bcftools view --threads 1 -r "$GENOTYPE_REGION" -Ob \
        -o "$GENOTYPE_BCF_FILE" "$BCF_FILE"
fi
if [ ! -f "${GENOTYPE_BCF_FILE}.csi" ]; then
    bcftools index --threads 1 "$GENOTYPE_BCF_FILE"
fi
for genotype_file in "$GENOTYPE_VCF_FILE" "$GENOTYPE_BCF_FILE"; do
    genotype_rows="$(bcftools index -n "$genotype_file")"
    genotype_samples="$(bcftools query -l "$genotype_file" | wc -l | tr -d ' ')"
    if [ "$genotype_rows" != "25000" ] || [ "$genotype_samples" != "2548" ]; then
        echo "Unexpected reader-matrix shape for $genotype_file: ${genotype_rows}x${genotype_samples}" >&2
        exit 1
    fi
done

# === BGEN fixtures ===
#
# The BGEN reader benchmark uses the same chromosome 22 callset as the BCF
# benchmark, exported by plink2, so both benchmarks compare the same variants
# and the same sample order. Two exports are produced: the phased one that the
# source callset implies, and an unphased one, because pysnptools' BGEN reader
# rejects phased input.
BGEN_DIR="/Users/mwiewior/research/data/BGEN"
BGEN_SLICE="${BGEN_DIR}/chr22.first-25000.bgen"
BGEN_SLICE_UNPHASED="${BGEN_DIR}/chr22.first-25000.unphased.bgen"
BGEN_FULL="${BGEN_DIR}/chr22.full.bgen"

if [ ! -f "$BGEN_SLICE" ] || [ ! -f "$BGEN_SLICE_UNPHASED" ] || [ ! -f "$BGEN_FULL" ]; then
    command -v plink2 >/dev/null || {
        echo "plink2 is required to create the BGEN benchmark fixtures." >&2
        echo "Download it from https://www.cog-genomics.org/plink/2.0/" >&2
        exit 1
    }
    mkdir -p "$BGEN_DIR"
fi

# plink2's bgen-1.2 export writes an empty zlib payload for very small blocks,
# so these fixtures are only valid at the cohort sizes used here.
if [ ! -f "$BGEN_SLICE" ]; then
    echo "=== Exporting phased BGEN slice ==="
    plink2 --vcf "$GENOTYPE_VCF_FILE" --export bgen-1.2 bits=8 \
        --out "${BGEN_DIR}/chr22.first-25000"
fi
if [ ! -f "$BGEN_SLICE_UNPHASED" ]; then
    echo "=== Exporting unphased BGEN slice ==="
    plink2 --vcf "$GENOTYPE_VCF_FILE" --make-pgen erase-phase \
        --out "${BGEN_DIR}/chr22.first-25000.unphased.tmp"
    plink2 --pfile "${BGEN_DIR}/chr22.first-25000.unphased.tmp" \
        --export bgen-1.2 bits=8 --out "${BGEN_DIR}/chr22.first-25000.unphased"
    rm -f "${BGEN_DIR}"/chr22.first-25000.unphased.tmp.*
fi
if [ ! -f "$BGEN_FULL" ]; then
    echo "=== Exporting whole-chromosome BGEN ==="
    plink2 --vcf "$BCF_VCF_FILE" --export bgen-1.2 bits=8 \
        --out "${BGEN_DIR}/chr22.full"
fi

# === PGEN fixtures ===
#
# The PGEN reader benchmark uses the same chromosome 22 callset as the BCF and
# BGEN benchmarks, so all three compare the same variants and sample order.
# Unlike BGEN these are hardcalls: --make-pgen writes the 2-bit-packed genotype
# block plus the .pvar/.psam companions the readers discover by basename.
PGEN_DIR="/Users/mwiewior/research/data/PGEN"
PGEN_SLICE="${PGEN_DIR}/chr22.first-25000.pgen"
PGEN_FULL="${PGEN_DIR}/chr22.full.pgen"

if [ ! -f "$PGEN_SLICE" ] || [ ! -f "$PGEN_FULL" ]; then
    command -v plink2 >/dev/null || {
        echo "plink2 is required to create the PGEN benchmark fixtures." >&2
        echo "Download it from https://www.cog-genomics.org/plink/2.0/" >&2
        exit 1
    }
    mkdir -p "$PGEN_DIR"
fi

if [ ! -f "$PGEN_SLICE" ]; then
    echo "=== Exporting PGEN slice ==="
    plink2 --vcf "$GENOTYPE_VCF_FILE" --make-pgen \
        --out "${PGEN_DIR}/chr22.first-25000"
fi
if [ ! -f "$PGEN_FULL" ]; then
    echo "=== Exporting whole-chromosome PGEN ==="
    plink2 --vcf "$BCF_VCF_FILE" --make-pgen --out "${PGEN_DIR}/chr22.full"
fi

# The readers discover .pvar and .psam from the .pgen basename, so a fileset
# missing a companion fails at open rather than mid-scan. Check here instead.
for pgen_prefix in "${PGEN_DIR}/chr22.first-25000" "${PGEN_DIR}/chr22.full"; do
    for companion in pvar psam; do
        if [ ! -f "${pgen_prefix}.${companion}" ]; then
            echo "Missing PGEN companion: ${pgen_prefix}.${companion}" >&2
            exit 1
        fi
    done
done

# === FASTQ: EBI SRA ===
FASTQ_DIR="/Users/mwiewior/research/data/FASTQ"
FASTQ_FILE="${FASTQ_DIR}/ERR194158.fastq.gz"
if [ ! -f "$FASTQ_FILE" ]; then
    echo ""
    echo "=== Downloading FASTQ (EBI SRA ERR194158) ==="
    mkdir -p "$FASTQ_DIR"
    curl -L -o "$FASTQ_FILE" \
        "https://ftp.sra.ebi.ac.uk/vol1/fastq/ERR194/ERR194158/ERR194158.fastq.gz" \
        2>/dev/null \
    || wget -O "$FASTQ_FILE" \
        "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/ERR194/ERR194158/ERR194158.fastq.gz"
    echo "  FASTQ downloaded to: $FASTQ_FILE"
else
    echo "=== FASTQ already exists: $FASTQ_FILE (skipping download) ==="
fi

echo ""
echo "=== Setup complete ==="
echo ""
echo "Venvs:"
echo "  Main (pre-release): .venv/bin/python"
echo "  Baseline (0.22.0):  .venv-baseline/bin/python"
echo ""
echo "Data files:"
echo "  BAM:   /Users/mwiewior/research/data/WES/NA12878.proper.wes.md.chr1.bam"
echo "  VCF:   $VCF_FILE"
echo "  BCF:   $BCF_FILE"
echo "  Reader-matrix VCF: $GENOTYPE_VCF_FILE"
echo "  Reader-matrix BCF: $GENOTYPE_BCF_FILE"
echo "  BGEN slice (phased):   $BGEN_SLICE"
echo "  BGEN slice (unphased): $BGEN_SLICE_UNPHASED"
echo "  BGEN whole chromosome: $BGEN_FULL"
echo "  FASTQ: $FASTQ_FILE"
