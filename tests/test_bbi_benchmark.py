from __future__ import annotations

import copy
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import generate_bbi_figures
import run_bbi_benchmarks
from benchmarks import bench_bbi_polars_bio
from benchmarks.bbi_common import fingerprints_match, run_bbi_benchmark


def sample(*, threads: int = 1, physical_partitions: int = 1) -> dict:
    fingerprint = {"rows": 3, "value_sum": 1.25}
    return {
        "threads": threads,
        "physical_partition_count": physical_partitions,
        "estimated_data_bytes": [100] * physical_partitions,
        "iterations": 1,
        "time_seconds": 0.5,
        "peak_rss_mb": 10.0,
        "fingerprint": fingerprint,
        "content_fingerprint": {
            **fingerprint,
            "row_hash_sum_1": 11,
            "row_hash_sum_2": 17,
        },
        "diagnostics": {},
        "thread_limits": {
            "POLARS_MAX_THREADS": threads,
            "RAYON_NUM_THREADS": threads,
            "TOKIO_WORKER_THREADS": threads,
        },
    }


def payload(*, label: str = "candidate") -> dict:
    return {
        "schema_version": 2,
        "metadata": {
            "label": label,
            "partitions": [1, 2],
            "platform": "test-platform",
            "machine": "test-machine",
            "logical_cpu_count": 8,
            "physical_cpu_count": 4,
            "memory_total_bytes": 1024,
            "python": "3.11.13",
            "versions": {
                "polars-bio": "0.0.0",
                "polars": "1.0.0",
                "pyarrow": "2.0.0",
            },
            "polars_bio_build": {
                "declared_profile": "release",
                "declared_rustflags": "-C target-cpu=native",
            },
            "harness": {
                "runner": {"path": "run.py", "sha256": "runner-digest"},
                "child": {"path": "child.py", "sha256": "child-digest"},
                "common": {"path": "common.py", "sha256": "common-digest"},
            },
            "physical_partition_expectation": "requested",
            "files": {"bigwig": {"sha256": "fixture-digest", "size_bytes": 123}},
        },
        "verification": {
            "bigwig:decode": {"fingerprint": {"rows": 3, "value_sum": 1.25}}
        },
        "results": {
            "bigwig": {
                "decode": {
                    "t1": {"iterations_per_process": 1},
                    "t2": {"iterations_per_process": 1},
                }
            }
        },
        "scaling": {},
    }


class CommonRunnerTests(unittest.TestCase):
    def test_parallel_float_reduction_order_is_tolerated_between_iterations(
        self,
    ) -> None:
        values = iter((104752471.3413033, 104752471.34130344))

        def operation():
            return {"rows": 3, "value_sum": next(values)}, {}

        with (
            mock.patch.dict(
                "os.environ",
                {
                    "POLARS_MAX_THREADS": "2",
                    "RAYON_NUM_THREADS": "2",
                    "TOKIO_WORKER_THREADS": "2",
                },
            ),
            redirect_stdout(io.StringIO()),
        ):
            run_bbi_benchmark(
                operation,
                format_name="bigwig",
                workload="polars_aggregate_all",
                threads=2,
                iterations=2,
                physical_partition_info=lambda: {
                    "physical_partition_count": 2,
                    "estimated_data_bytes": [100, 100],
                },
                content_fingerprint=lambda: {
                    "rows": 3,
                    "value_sum": 104752471.34130338,
                },
                environment_info=dict,
            )

    def test_float_drift_beyond_tolerance_is_rejected(self) -> None:
        self.assertFalse(
            fingerprints_match(
                {"rows": 3, "value_sum": 1.0},
                {"rows": 3, "value_sum": 1.001},
            )
        )

    def test_float_tolerance_scales_with_large_aggregate(self) -> None:
        self.assertTrue(
            fingerprints_match(
                {"rows": 3, "value_sum": 104752471.34130},
                {"rows": 3, "value_sum": 104752471.34135},
            )
        )


class RunnerTests(unittest.TestCase):
    def test_single_sample_summary_marks_stdev_unavailable(self) -> None:
        summary = run_bbi_benchmarks.summarize([sample()])

        self.assertIsNone(summary["time_seconds_stdev"])
        self.assertIsNone(summary["peak_rss_mb_stdev"])

    def test_summary_records_balance_and_diagnostic_medians(self) -> None:
        first = sample(threads=2, physical_partitions=2)
        second = copy.deepcopy(first)
        first["estimated_data_bytes"] = [90, 110]
        second["estimated_data_bytes"] = [90, 110]
        first["diagnostics"] = {"record_batches": 10}
        second["diagnostics"] = {"record_batches": 12}

        summary = run_bbi_benchmarks.summarize([first, second])

        self.assertEqual(summary["estimated_data_byte_balance"]["total"], 200)
        self.assertAlmostEqual(
            summary["estimated_data_byte_balance"]["coefficient_of_variation"],
            0.1,
        )
        self.assertEqual(summary["diagnostics_median"]["record_batches"], 11)

    def test_requested_partition_and_thread_limits_are_verified(self) -> None:
        raw = {
            "bigwig:polars_count:t1": [sample()],
            "bigwig:polars_count:t2": [sample(threads=2, physical_partitions=2)],
        }

        verified = run_bbi_benchmarks.verify_fingerprints(raw, "requested")

        self.assertEqual(
            verified["bigwig:polars_count"]["physical_partitions_by_requested"],
            {"t1": 1, "t2": 2},
        )

    def test_content_mismatch_across_threads_is_rejected(self) -> None:
        changed = sample(threads=2, physical_partitions=2)
        changed["content_fingerprint"]["row_hash_sum_2"] += 1
        raw = {
            "bigwig:polars_count:t1": [sample()],
            "bigwig:polars_count:t2": [changed],
        }

        with self.assertRaisesRegex(AssertionError, "content digest"):
            run_bbi_benchmarks.verify_fingerprints(raw, "requested")

    def test_timed_fingerprint_must_match_independent_content_scan(self) -> None:
        changed = sample()
        changed["fingerprint"]["rows"] = 2

        with self.assertRaisesRegex(AssertionError, "independent content scan"):
            run_bbi_benchmarks.verify_fingerprints(
                {"bigwig:polars_count:t1": [changed]}, "requested"
            )

    def test_wrong_physical_partition_count_is_rejected(self) -> None:
        raw = {"bigwig:polars_count:t2": [sample(threads=2, physical_partitions=1)]}

        with self.assertRaisesRegex(AssertionError, "requires 2"):
            run_bbi_benchmarks.verify_fingerprints(raw, "requested")

    def test_wrong_tokio_limit_is_rejected(self) -> None:
        changed = sample()
        changed["thread_limits"]["TOKIO_WORKER_THREADS"] = 8

        with self.assertRaisesRegex(AssertionError, "thread limits"):
            run_bbi_benchmarks.verify_fingerprints(
                {"bigwig:polars_count:t1": [changed]}, "requested"
            )

    def test_partition_sweep_requires_t1_baseline(self) -> None:
        with self.assertRaisesRegex(ValueError, "must include 1"):
            run_bbi_benchmarks.validate_partition_sweep([2, 4, 8])

    def test_round_starts_are_spread_across_the_full_sweep(self) -> None:
        combinations = [("bigwig", "count", value) for value in range(10)]

        orders = [
            run_bbi_benchmarks.round_order(combinations, index, 5) for index in range(5)
        ]

        self.assertEqual([order[0][2] for order in orders], [0, 1, 4, 5, 8])
        self.assertTrue(all(sorted(order) == sorted(combinations) for order in orders))

    def test_declared_build_refs_must_match_source_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.lock").write_text(
                "datafusion bfc73ab569be bigtools 925581373907", encoding="utf-8"
            )
            environment = {
                "polars_bio_build": {
                    "source": {
                        "root": directory,
                        "git_head": "f32af9416139",
                        "tracked_diff_sha256": run_bbi_benchmarks.EMPTY_SHA256,
                        "untracked_paths": [],
                        "declared_patch": None,
                    }
                }
            }

            run_bbi_benchmarks.verify_declared_build_refs(
                environment,
                {
                    "polars_bio_ref": "f32af94",
                    "datafusion_bio_formats_ref": "bfc73ab",
                    "bigtools_ref": "9255813",
                },
            )

            with self.assertRaisesRegex(AssertionError, "does not match"):
                run_bbi_benchmarks.verify_declared_build_refs(
                    environment, {"polars_bio_ref": "wrong"}
                )

            environment["polars_bio_build"]["source"]["tracked_diff_sha256"] = (
                "unrecorded"
            )
            with self.assertRaisesRegex(AssertionError, "declared patch"):
                run_bbi_benchmarks.verify_declared_build_refs(
                    environment, {"polars_bio_ref": "f32af94"}
                )

    def test_declared_patch_is_checked_even_without_source_refs(self) -> None:
        environment = {
            "polars_bio_build": {
                "source": {
                    "root": "/unused",
                    "tracked_diff_sha256": "wrong",
                    "declared_patch": {"sha256": "expected"},
                    "untracked_paths": [],
                }
            }
        }

        with self.assertRaisesRegex(AssertionError, "neither clean nor identical"):
            run_bbi_benchmarks.verify_declared_build_refs(
                environment,
                {
                    "polars_bio_ref": None,
                    "datafusion_bio_formats_ref": None,
                    "bigtools_ref": None,
                },
                patch_declared=True,
            )

    def test_git_diff_is_independent_of_user_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", root], check=True)
            subprocess.run(
                ["git", "-C", root, "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", root, "config", "user.name", "Test"], check=True
            )
            first = root / "first.txt"
            second = root / "second.txt"
            lines = [f"line {index}" for index in range(24)]
            lines[2] = ""
            original = "\n".join(lines) + "\n"
            first.write_text(original, encoding="utf-8")
            second.write_text(original, encoding="utf-8")
            subprocess.run(
                ["git", "-C", root, "add", "first.txt", "second.txt"], check=True
            )
            subprocess.run(["git", "-C", root, "commit", "-qm", "fixture"], check=True)
            for path, prefix in ((first, "first"), (second, "second")):
                changed = lines.copy()
                changed[1] = f"{prefix} early change"
                changed[20] = f"{prefix} late change"
                path.write_text("\n".join(changed) + "\n", encoding="utf-8")
            expected = bench_bbi_polars_bio.git_tracked_diff(root)
            order_file = root / "diff-order"
            order_file.write_text("second.txt\nfirst.txt\n", encoding="utf-8")
            for key, value in (
                ("diff.algorithm", "histogram"),
                ("core.abbrev", "12"),
                ("diff.noprefix", "true"),
                ("diff.mnemonicPrefix", "true"),
                ("diff.orderFile", str(order_file)),
                ("diff.interHunkContext", "1000"),
                ("diff.suppressBlankEmpty", "true"),
            ):
                subprocess.run(["git", "-C", root, "config", key, value], check=True)

            self.assertEqual(bench_bbi_polars_bio.git_tracked_diff(root), expected)

    @mock.patch("run_bbi_benchmarks.harness_provenance")
    def test_harness_change_during_sweep_is_rejected(self, provenance) -> None:
        expected = {
            name: {"path": str(path), "sha256": "before"}
            for name, path in run_bbi_benchmarks.HARNESS_PATHS.items()
        }
        changed = copy.deepcopy(expected)
        changed["child"]["sha256"] = "after"
        provenance.return_value = changed

        with self.assertRaisesRegex(AssertionError, "harness changed.*child"):
            run_bbi_benchmarks.verify_harness_unchanged(expected)

    @mock.patch("run_bbi_benchmarks.subprocess.run")
    def test_child_runs_from_repository_directory(self, subprocess_run) -> None:
        subprocess_run.return_value = mock.Mock(
            stdout='BBI_RESULT:{"ok": true}\n', stderr="", returncode=0
        )

        result = run_bbi_benchmarks.run_one("python", {}, 10)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(
            subprocess_run.call_args.kwargs["cwd"], run_bbi_benchmarks.SCRIPT_DIR
        )

    @mock.patch("run_bbi_benchmarks.subprocess.run")
    def test_preflight_reads_environment_before_the_sweep(self, subprocess_run) -> None:
        subprocess_run.return_value = mock.Mock(
            stdout='BBI_ENVIRONMENT:{"python": "3.11.13"}\n',
            stderr="",
            returncode=0,
        )

        environment = run_bbi_benchmarks.preflight_environment("python", {}, 10)

        self.assertEqual(environment, {"python": "3.11.13"})
        self.assertEqual(
            subprocess_run.call_args.kwargs["cwd"], run_bbi_benchmarks.SCRIPT_DIR
        )


class FigureValidationTests(unittest.TestCase):
    def test_matching_payloads_are_accepted(self) -> None:
        generate_bbi_figures.validate_payloads(
            [payload(label="baseline"), payload(label="candidate")],
            [Path("baseline.json"), Path("candidate.json")],
        )

    def test_single_schema_one_payload_remains_plottable(self) -> None:
        legacy = payload()
        legacy["schema_version"] = 1
        legacy["metadata"].pop("polars_bio_build")
        legacy["metadata"].pop("physical_partition_expectation")

        generate_bbi_figures.validate_payloads([legacy], [Path("legacy.json")])

    def test_schema_one_payload_cannot_enter_a_comparison(self) -> None:
        legacy = payload(label="legacy")
        legacy["schema_version"] = 1

        with self.assertRaisesRegex(ValueError, "schema-v2 harness provenance"):
            generate_bbi_figures.validate_payloads(
                [legacy, payload(label="candidate")],
                [Path("legacy.json"), Path("candidate.json")],
            )

    def test_fixture_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["files"]["bigwig"]["sha256"] = "other"

        with self.assertRaisesRegex(ValueError, "different bigwig fixture"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_hardware_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["physical_cpu_count"] = 16

        with self.assertRaisesRegex(ValueError, "different benchmark hardware"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_runtime_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["python"] = "3.13.0"

        with self.assertRaisesRegex(ValueError, "different Python runtime"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_build_setting_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["polars_bio_build"]["declared_profile"] = "debug"

        with self.assertRaisesRegex(ValueError, "different polars-bio build setting"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_harness_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["harness"]["child"]["sha256"] = "different"

        with self.assertRaisesRegex(ValueError, "different benchmark harness"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_iteration_protocol_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["results"]["bigwig"]["decode"]["t1"]["iterations_per_process"] = 10

        with self.assertRaisesRegex(ValueError, "different iteration protocol"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_partition_argument_order_does_not_block_comparison(self) -> None:
        changed = payload(label="candidate")
        changed["metadata"]["partitions"] = [2, 1]

        generate_bbi_figures.validate_payloads(
            [payload(label="baseline"), changed],
            [Path("baseline.json"), Path("candidate.json")],
        )

    def test_unenforced_partition_expectation_is_rejected(self) -> None:
        changed = payload(label="candidate")
        changed["metadata"]["physical_partition_expectation"] = "consistent"

        with self.assertRaisesRegex(ValueError, "partition expectation"):
            generate_bbi_figures.validate_payloads([changed], [Path("candidate.json")])

    def test_dependency_version_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["metadata"]["versions"]["polars"] = "different"

        with self.assertRaisesRegex(ValueError, "different polars runtime"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_content_mismatch_is_rejected(self) -> None:
        changed = copy.deepcopy(payload(label="candidate"))
        changed["verification"]["bigwig:decode"]["fingerprint"]["rows"] = 2

        with self.assertRaisesRegex(ValueError, "different bigwig content"):
            generate_bbi_figures.validate_payloads(
                [payload(label="baseline"), changed],
                [Path("baseline.json"), Path("candidate.json")],
            )

    def test_later_payload_format_mismatch_is_rejected(self) -> None:
        first = payload(label="first")
        second = payload(label="second")
        third = payload(label="third")
        for item in (second, third):
            item["metadata"]["files"] = {
                "bigbed": {"sha256": "bigbed-digest", "size_bytes": 456}
            }
            item["verification"] = {"bigbed:decode": {"fingerprint": {"rows": 5}}}
        third["verification"]["bigbed:decode"]["fingerprint"]["rows"] = 6

        with self.assertRaisesRegex(ValueError, "different bigbed content"):
            generate_bbi_figures.validate_payloads(
                [first, second, third],
                [Path("first.json"), Path("second.json"), Path("third.json")],
            )

    def test_missing_partition_or_file_metadata_is_rejected(self) -> None:
        for field in ("partitions", "files"):
            changed = payload(label="candidate")
            del changed["metadata"][field]
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(ValueError, "missing environment metadata"),
            ):
                generate_bbi_figures.validate_payloads(
                    [changed], [Path("candidate.json")]
                )

    def test_plot_partitions_are_sorted(self) -> None:
        changed = payload()
        changed["metadata"]["partitions"] = [1, 8, 2, 4]

        self.assertEqual(generate_bbi_figures.plot_partitions(changed), [1, 2, 4, 8])


if __name__ == "__main__":
    unittest.main()
