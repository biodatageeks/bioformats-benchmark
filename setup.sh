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
    pysam==0.24.0 oxbow==0.8.1 biobear==0.23.7 \
    polars==1.42.1 pyarrow==24.0.0 \
    psutil matplotlib notebook

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

# FASTQ: EBI SRA
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
echo "  FASTQ: $FASTQ_FILE"
