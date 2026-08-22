"""Fresh-process polars-bio BigWig/BigBed scalability benchmark."""

from __future__ import annotations

import os
import re

import polars as pl
import polars_bio as pb

from benchmarks.bbi_common import (
    BenchmarkSample,
    FORMATS,
    WORKLOADS,
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
    estimate_match = re.search(
        r"estimated_data_bytes=\[([^]]*)\]", display
    )
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


def datafusion_arrow_stream() -> BenchmarkSample:
    """Stream every projected Arrow column without retaining the whole file."""
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
    rows = 0
    record_batches = 0
    columns = None
    for record_batch in py_read_table(ctx, table.name).execute_stream():
        batch = record_batch.to_pyarrow()
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
        pl.col("chrom")
        .str.len_bytes()
        .cast(pl.UInt64)
        .sum()
        .alias("chrom_bytes"),
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


run_bbi_benchmark(
    benchmark,
    format_name=FORMAT,
    workload=WORKLOAD,
    threads=THREADS,
    iterations=ITERATIONS,
    physical_partition_info=physical_partition_info,
)
