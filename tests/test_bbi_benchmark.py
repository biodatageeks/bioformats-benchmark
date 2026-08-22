from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_bbi_figures
import run_bbi_benchmarks


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
            "versions": {"polars-bio": "0.0.0"},
            "files": {"bigwig": {"sha256": "fixture-digest", "size_bytes": 123}},
        },
        "results": {},
        "scaling": {},
    }


class RunnerTests(unittest.TestCase):
    def test_single_sample_summary_marks_stdev_unavailable(self) -> None:
        summary = run_bbi_benchmarks.summarize([sample()])

        self.assertIsNone(summary["time_seconds_stdev"])
        self.assertIsNone(summary["peak_rss_mb_stdev"])

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

    def test_declared_build_refs_must_match_source_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Cargo.lock").write_text(
                "datafusion bfc73ab569be bigtools 925581373907", encoding="utf-8"
            )
            environment = {
                "polars_bio_build": {
                    "source": {"root": directory, "git_head": "f32af9416139"}
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


class FigureValidationTests(unittest.TestCase):
    def test_matching_payloads_are_accepted(self) -> None:
        generate_bbi_figures.validate_payloads(
            [payload(label="baseline"), payload(label="candidate")],
            [Path("baseline.json"), Path("candidate.json")],
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


if __name__ == "__main__":
    unittest.main()
