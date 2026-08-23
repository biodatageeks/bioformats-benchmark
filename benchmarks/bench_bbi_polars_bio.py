"""Fresh-process polars-bio BigWig/BigBed scalability benchmark."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import re
import subprocess
import sys
from pathlib import Path

import polars as pl
import polars_bio as pb

from benchmarks.bbi_common import (
    FORMATS,
    WORKLOADS,
    BenchmarkSample,
    file_sha256,
    input_path,
    run_bbi_benchmark,
)

FORMAT = os.environ.get("BBI_FORMAT", "bigwig").lower()
WORKLOAD = os.environ.get("BBI_WORKLOAD", "polars_count").lower()
THREADS = int(os.environ.get("THREAD_NUM", "1"))
ITERATIONS = int(os.environ.get("BBI_ITERATIONS", "1"))

if FORMAT not in FORMATS:
    raise ValueError(f"unsupported BBI format: {FORMAT!r}")
if WORKLOAD not in WORKLOADS:
    raise ValueError(f"unsupported BBI workload: {WORKLOAD!r}")
if THREADS < 1:
    raise ValueError("THREAD_NUM must be positive")

pb.set_option("datafusion.execution.target_partitions", str(THREADS))


def scan():
    path = input_path(FORMAT)
    if FORMAT == "bigwig":
        return pb.scan_bigwig(path, use_zero_based=True)
    return pb.scan_bigbed(path, schema="rest", use_zero_based=True)


def physical_partition_info() -> dict[str, int | list[int]]:
    """Read source partition metadata outside the timed scope."""
    from polars_bio.context import ctx
    from polars_bio.polars_bio import (
        BigBedReadOptions,
        BigWigReadOptions,
        InputFormat,
        ReadOptions,
        py_read_table,
        py_register_table,
    )

    path = input_path(FORMAT)
    if FORMAT == "bigwig":
        input_format = InputFormat.BigWig
        read_options = ReadOptions(
            bigwig_read_options=BigWigReadOptions(zero_based=True)
        )
        exec_prefix = "BigWigExec:"
    else:
        input_format = InputFormat.BigBed
        read_options = ReadOptions(
            bigbed_read_options=BigBedReadOptions(zero_based=True, schema="rest")
        )
        exec_prefix = "BigBedExec:"

    table = py_register_table(
        ctx,
        path,
        f"bbi_benchmark_{FORMAT}",
        input_format,
        read_options,
    )
    plan = py_read_table(ctx, table.name).execution_plan()

    def find_exec(node):
        if node.display().lstrip().startswith(exec_prefix):
            return node
        for child in node.children():
            result = find_exec(child)
            if result is not None:
                return result
        return None

    exec_node = find_exec(plan)
    if exec_node is None:
        raise AssertionError(f"{exec_prefix.removesuffix(':')} not found in plan")
    display = exec_node.display()
    estimate_match = re.search(r"estimated_data_bytes=\[([^]]*)\]", display)
    estimated_data_bytes = []
    if estimate_match:
        estimated_data_bytes = [
            int(value.strip())
            for value in estimate_match.group(1).split(",")
            if value.strip()
        ]
    return {
        "physical_partition_count": int(exec_node.partition_count),
        "estimated_data_bytes": estimated_data_bytes,
    }


def datafusion_arrow_batches():
    """Yield every projected batch through the direct DataFusion Arrow path."""
    from polars_bio.context import ctx
    from polars_bio.polars_bio import (
        BigBedReadOptions,
        BigWigReadOptions,
        InputFormat,
        ReadOptions,
        py_read_table,
        py_register_table,
    )

    path = input_path(FORMAT)
    if FORMAT == "bigwig":
        input_format = InputFormat.BigWig
        read_options = ReadOptions(
            bigwig_read_options=BigWigReadOptions(zero_based=True)
        )
    else:
        input_format = InputFormat.BigBed
        read_options = ReadOptions(
            bigbed_read_options=BigBedReadOptions(zero_based=True, schema="rest")
        )

    table = py_register_table(
        ctx,
        path,
        f"bbi_arrow_stream_{FORMAT}",
        input_format,
        read_options,
    )
    for record_batch in py_read_table(ctx, table.name).execute_stream():
        yield record_batch.to_pyarrow()


def datafusion_arrow_stream() -> BenchmarkSample:
    """Stream every projected Arrow column without retaining the whole file."""
    rows = 0
    record_batches = 0
    columns = None
    for batch in datafusion_arrow_batches():
        rows += batch.num_rows
        record_batches += 1
        names = tuple(batch.schema.names)
        if columns is None:
            columns = names
        elif names != columns:
            raise AssertionError(f"Arrow schema changed within one scan: {names!r}")

    return (
        {"rows": rows, "columns": ",".join(columns or ())},
        {"record_batches": record_batches},
    )


def content_expressions() -> list[pl.Expr]:
    """Build the order-independent all-column content fingerprint."""
    row = pl.struct(pl.all())
    expressions = [
        pl.len().alias("rows"),
        row.hash(seed=0, seed_1=1, seed_2=2, seed_3=3).sum().alias("row_hash_sum_1"),
        row.hash(seed=11, seed_1=13, seed_2=17, seed_3=19)
        .sum()
        .alias("row_hash_sum_2"),
        pl.col("chrom").str.len_bytes().cast(pl.UInt64).sum().alias("chrom_bytes"),
        pl.col("start").cast(pl.UInt64).sum().alias("start_sum"),
        pl.col("end").cast(pl.UInt64).sum().alias("end_sum"),
    ]
    if FORMAT == "bigwig":
        expressions.append(pl.col("value").cast(pl.Float64).sum().alias("value_sum"))
    else:
        expressions.append(
            pl.col("rest").str.len_bytes().cast(pl.UInt64).sum().alias("rest_bytes")
        )
    return expressions


def extract_content_fingerprint(result: pl.DataFrame) -> dict[str, int | float | str]:
    return {
        column: float(result.item(0, column))
        if column == "value_sum"
        else int(result.item(0, column))
        for column in result.columns
    }


def frame_content_fingerprint(frame: pl.DataFrame) -> dict[str, int | float | str]:
    return extract_content_fingerprint(frame.select(content_expressions()))


def arrow_content_fingerprint() -> dict[str, int | float | str]:
    """Replay the direct Arrow path and digest every emitted value, untimed."""
    combined: dict[str, int | float | str] = {}
    hash_fields = {"row_hash_sum_1", "row_hash_sum_2"}
    for batch in datafusion_arrow_batches():
        current = frame_content_fingerprint(pl.from_arrow(batch, rechunk=False))
        for key, value in current.items():
            if key == "value_sum":
                combined[key] = float(combined.get(key, 0.0)) + float(value)
            elif key in hash_fields:
                combined[key] = (int(combined.get(key, 0)) + int(value)) % (1 << 64)
            else:
                combined[key] = int(combined.get(key, 0)) + int(value)
    if not combined:
        raise AssertionError("direct Arrow validation scan emitted no rows")
    return combined


def benchmark() -> BenchmarkSample:
    if WORKLOAD == "arrow_stream_all":
        return datafusion_arrow_stream()

    source = scan()
    if WORKLOAD == "polars_count":
        result = source.select(pl.len().alias("rows")).collect(engine="streaming")
        return {"rows": int(result.item(0, "rows"))}, {}

    if WORKLOAD == "polars_collect_all":
        result = source.collect(engine="streaming")
        return (
            {"rows": result.height, "columns": ",".join(result.columns)},
            {
                "output_chunks": max(
                    result[column].n_chunks() for column in result.columns
                ),
                "estimated_size_mb": result.estimated_size("mb"),
            },
        )

    expressions = [
        pl.len().alias("rows"),
        pl.col("chrom").str.len_bytes().cast(pl.UInt64).sum().alias("chrom_bytes"),
        pl.col("start").cast(pl.UInt64).sum().alias("start_sum"),
        pl.col("end").cast(pl.UInt64).sum().alias("end_sum"),
    ]
    if FORMAT == "bigwig":
        expressions.append(pl.col("value").cast(pl.Float64).sum().alias("value_sum"))
    else:
        expressions.append(
            pl.col("rest").str.len_bytes().cast(pl.UInt64).sum().alias("rest_bytes")
        )

    result = source.select(expressions).collect(engine="streaming")
    return (
        {
            column: float(result.item(0, column))
            if column == "value_sum"
            else int(result.item(0, column))
            for column in result.columns
        },
        {},
    )


def content_fingerprint() -> dict[str, int | float | str]:
    """Replay this workload's data path and digest every emitted row, untimed."""
    if WORKLOAD == "arrow_stream_all":
        return arrow_content_fingerprint()

    source = scan()
    if WORKLOAD == "polars_collect_all":
        return frame_content_fingerprint(source.collect(engine="streaming"))
    return extract_content_fingerprint(
        source.select(content_expressions()).collect(engine="streaming")
    )


def environment_info() -> dict[str, object]:
    versions = {
        distribution: importlib.metadata.version(distribution)
        for distribution in ("polars-bio", "polars", "pyarrow")
    }
    package = Path(pb.__file__).parent
    extensions = sorted(package.glob("*.so"))
    source_root = next(
        (
            parent
            for parent in package.parents
            if (parent / "Cargo.toml").is_file() and (parent / ".git").exists()
        ),
        None,
    )
    source = None
    if source_root is not None:
        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        git_diff = subprocess.run(
            ["git", "diff", "--binary", "HEAD"],
            cwd=source_root,
            check=True,
            capture_output=True,
        ).stdout
        untracked_paths = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=source_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        declared_patch = None
        if patch_value := os.environ.get("POLARS_BIO_PATCH"):
            patch_path = Path(patch_value).expanduser().resolve()
            if not patch_path.is_file():
                raise FileNotFoundError(
                    f"declared polars-bio patch is missing: {patch_path}"
                )
            declared_patch = {
                "path": str(patch_path),
                "sha256": file_sha256(patch_path),
            }
        source = {
            "root": str(source_root),
            "git_head": git_head,
            "tracked_diff_sha256": hashlib.sha256(git_diff).hexdigest(),
            "untracked_paths": untracked_paths,
            "declared_patch": declared_patch,
            "cargo_toml_sha256": file_sha256(source_root / "Cargo.toml"),
            "cargo_lock_sha256": file_sha256(source_root / "Cargo.lock"),
        }
    return {
        "python": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "versions": versions,
        "polars_bio_build": {
            "module_path": str(package),
            "editable_install": source is not None,
            "source": source,
            "extensions": [
                {
                    "name": extension.name,
                    "size_bytes": extension.stat().st_size,
                    "sha256": file_sha256(extension),
                }
                for extension in extensions
            ],
            "declared_profile": os.environ.get("POLARS_BIO_BUILD_PROFILE"),
            "declared_rustflags": os.environ.get("POLARS_BIO_RUSTFLAGS"),
        },
    }


def main() -> None:
    run_bbi_benchmark(
        benchmark,
        format_name=FORMAT,
        workload=WORKLOAD,
        threads=THREADS,
        iterations=ITERATIONS,
        physical_partition_info=physical_partition_info,
        content_fingerprint=content_fingerprint,
        environment_info=environment_info,
    )


if __name__ == "__main__":
    main()
