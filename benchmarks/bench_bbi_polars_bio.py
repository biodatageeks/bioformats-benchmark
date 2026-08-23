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

THREAD_LIMIT_NAMES = (
    "POLARS_MAX_THREADS",
    "RAYON_NUM_THREADS",
    "TOKIO_WORKER_THREADS",
)


def positive_integer_environment(name: str, default: str | None = None) -> int:
    """Read a positive integer before any thread pools are initialized."""
    value = os.environ.get(name, default)
    if value is None:
        raise ValueError(f"{name} must be set before starting the child")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


THREADS = positive_integer_environment("THREAD_NUM", "1")
ITERATIONS = positive_integer_environment("BBI_ITERATIONS", "1")
THREAD_LIMITS = {
    name: positive_integer_environment(name, str(THREADS))
    for name in THREAD_LIMIT_NAMES
}
for thread_limit_name, thread_limit_value in THREAD_LIMITS.items():
    if thread_limit_value != THREADS:
        raise ValueError(
            f"{thread_limit_name}={thread_limit_value} must match THREAD_NUM={THREADS}"
        )
for thread_limit_name, thread_limit_value in THREAD_LIMITS.items():
    os.environ.setdefault(thread_limit_name, str(thread_limit_value))

import polars as pl
import polars_bio as pb

from benchmarks.bbi_common import (
    FORMATS,
    PARTITION_PROBE_KIND,
    WORKLOADS,
    BenchmarkSample,
    file_sha256,
    input_path,
    run_bbi_benchmark,
)

GIT_DIFF_COMMAND = (
    "git",
    "-c",
    "diff.algorithm=myers",
    "-c",
    "core.abbrev=7",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.orderFile=/dev/null",
    "-c",
    "diff.suppressBlankEmpty=false",
    "--no-pager",
    "diff",
    "--binary",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--unified=3",
    "--inter-hunk-context=0",
    "--indent-heuristic",
    "--no-renames",
    "--ignore-submodules=none",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "--abbrev=7",
    "--output-indicator-new=+",
    "--output-indicator-old=-",
    "--output-indicator-context= ",
    "-O/dev/null",
    "HEAD",
)

FORMAT = os.environ.get("BBI_FORMAT", "bigwig").lower()
WORKLOAD = os.environ.get("BBI_WORKLOAD", "polars_count").lower()

if FORMAT not in FORMATS:
    raise ValueError(f"unsupported BBI format: {FORMAT!r}")
if WORKLOAD not in WORKLOADS:
    raise ValueError(f"unsupported BBI workload: {WORKLOAD!r}")

pb.set_option("datafusion.execution.target_partitions", str(THREADS))


def scan():
    path = input_path(FORMAT)
    if FORMAT == "bigwig":
        return pb.scan_bigwig(path, use_zero_based=True)
    return pb.scan_bigbed(path, schema="rest", use_zero_based=True)


def partition_probe_info() -> dict[str, int | str | list[int]]:
    """Inspect an equivalent direct source plan outside the timed scope."""
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
    estimated_data_bytes = parse_estimated_data_bytes(display)
    return {
        "physical_partition_count": int(exec_node.partition_count),
        "physical_partition_probe": PARTITION_PROBE_KIND,
        "estimated_data_bytes": estimated_data_bytes,
    }


def parse_estimated_data_bytes(display: str) -> list[int]:
    """Parse required partition-balance evidence from a BBI source plan."""
    estimate_match = re.search(r"estimated_data_bytes=\[([^]]*)\]", display)
    if estimate_match is None:
        raise AssertionError("BBI source plan omitted estimated_data_bytes")
    estimates = [
        int(value.strip())
        for value in estimate_match.group(1).split(",")
        if value.strip()
    ]
    if not estimates:
        raise AssertionError("BBI source plan reported no estimated_data_bytes")
    return estimates


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
        return {"rows": int(result.item(0, "rows"))}, lambda frame=result: {}

    if WORKLOAD == "polars_collect_all":
        result = source.collect(engine="streaming")
        return (
            {"rows": result.height, "columns": ",".join(result.columns)},
            lambda frame=result: {
                "output_chunks": max(
                    frame[column].n_chunks() for column in frame.columns
                ),
                "estimated_size_mb": frame.estimated_size("mb"),
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
        lambda frame=result: {},
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


def git_tracked_diff(source_root: Path) -> bytes:
    """Return a deterministic diff independent of user Git configuration."""
    return subprocess.run(
        GIT_DIFF_COMMAND,
        cwd=source_root,
        check=True,
        capture_output=True,
    ).stdout


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
        git_diff = git_tracked_diff(source_root)
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
        partition_probe_info=partition_probe_info,
        content_fingerprint=content_fingerprint,
        environment_info=environment_info,
    )


if __name__ == "__main__":
    main()
