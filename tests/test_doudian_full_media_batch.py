import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import doudian_full_media_batch as batch


def _snapshot(path: Path) -> None:
    path.write_text(json.dumps({
        "shop_id": "1",
        "items": [
            {"product_id": "11", "name": "手机 A", "category_leaf_id": "1000007628"},
            {"product_id": "12", "name": "手机 B", "category_leaf_id": "1000007628"},
            {"product_id": "99", "name": "非手机", "category_leaf_id": "other"},
        ],
    }), encoding="utf-8")


class FullMediaBatchTests(unittest.TestCase):
    def test_candidate_rows_can_exclude_previously_rejected_products(self):
        snapshot = {"items": [
            {"product_id": "1", "category_leaf_id": "1000007628"},
            {"product_id": "2", "category_leaf_id": "1000007628"},
            {"product_id": "3", "category_leaf_id": "99"},
        ]}
        self.assertEqual(
            [row["product_id"] for row in batch._candidate_rows(
                snapshot, (), frozenset({"1"}),
            )],
            ["2"],
        )

    def test_dry_run_selects_only_safe_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            _snapshot(snapshot)
            calls = []

            def fake_run(args, product_id, snapshot_name, *, execute):
                calls.append((product_id, snapshot_name, execute))
                if product_id == "11":
                    return 0, {"product_id": "11", "model": "A"}, {}, []
                return 2, {}, {}, ["unsafe"]

            argv = ["batch", "1", "--snapshot", str(snapshot), "--folder-id", "0",
                    "--limit", "2", "--preflight-workers", "1",
                    "--report-root", str(root / "reports")]
            with patch.object(sys, "argv", argv), patch.object(batch, "_run_single", fake_run):
                self.assertEqual(batch.main(), 0)
            report = json.loads(next((root / "reports" / "1").glob("*.json")).read_text())
            self.assertEqual(report["summary"]["prepared"], 1)
            self.assertEqual(report["summary"]["skipped_candidates"], 1)
            self.assertEqual(calls, [
                ("11", "手机 A", False), ("12", "手机 B", False),
            ])

    def test_preflight_streams_in_parallel_but_writes_stay_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            _snapshot(snapshot)
            second_preflight_started = threading.Event()
            first_saw_second = []
            writes = []

            def fake_run(args, product_id, snapshot_name, *, execute):
                if execute:
                    writes.append(product_id)
                    return 0, {}, {"result": "verified_draft"}, []
                if product_id == "11":
                    first_saw_second.append(second_preflight_started.wait(timeout=1))
                else:
                    second_preflight_started.set()
                return 0, {"product_id": product_id, "model": product_id}, {}, []

            argv = [
                "batch", "1", "--snapshot", str(snapshot), "--folder-id", "0",
                "--limit", "2", "--preflight-workers", "2", "--execute",
                "--save-draft-only", "--report-root", str(root / "reports"),
            ]
            with patch.object(sys, "argv", argv), patch.object(batch, "_run_single", fake_run):
                self.assertEqual(batch.main(), 0)

            report = json.loads(next((root / "reports" / "1").glob("*.json")).read_text())
            self.assertEqual(first_saw_second, [True])
            self.assertEqual(writes, ["11", "12"])
            self.assertEqual(report["execution_strategy"], "streaming_preflight_serial_write")
            self.assertEqual(report["summary"]["preflight_peak_inflight"], 2)
            self.assertEqual(report["summary"]["draft_verified"], 2)

    def test_publish_requires_online_readback_for_every_prepared_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "snapshot.json"
            _snapshot(snapshot)

            def fake_run(args, product_id, snapshot_name, *, execute):
                if not execute:
                    return 0, {"product_id": product_id, "model": product_id}, {}, []
                if product_id == "11":
                    return 0, {}, {"result": "published_readback", "report": str(root / "one.json")}, []
                return 2, {}, {"result": "api_accepted_pending_or_needs_review",
                               "post_publish_issues": ["online_state_not_confirmed"]}, []

            argv = ["batch", "1", "--snapshot", str(snapshot), "--folder-id", "0",
                    "--limit", "2", "--execute", "--report-root", str(root / "reports")]
            with patch.object(sys, "argv", argv), patch.object(batch, "_run_single", fake_run):
                self.assertEqual(batch.main(), 2)
            report = json.loads(next((root / "reports" / "1").glob("*.json")).read_text())
            self.assertEqual(report["summary"]["published_readback"], 1)
            self.assertEqual(report["summary"]["manual_queue"], 1)


if __name__ == "__main__":
    unittest.main()
