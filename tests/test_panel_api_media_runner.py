import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import panel_api_media_runner as runner
from panel_api_media_runner import summarise_media_report


class PanelApiMediaReportTests(unittest.TestCase):
    def test_full_batch_uses_complete_snapshot_count(self):
        self.assertEqual(runner.effective_limit("batch", 999, 150), 150)
        self.assertEqual(runner.effective_limit("test", 1, 150), 1)

    def test_preflight_worker_count_is_bounded_and_has_safe_default(self):
        self.assertEqual(runner.effective_preflight_workers(None), 2)
        self.assertEqual(runner.effective_preflight_workers("invalid"), 2)
        self.assertEqual(runner.effective_preflight_workers("0"), 1)
        self.assertEqual(runner.effective_preflight_workers("9"), 4)

    def test_batch_progress_is_forwarded_while_child_is_running(self):
        class FakeProcess:
            stdout = io.StringIO(
                '##PANEL_PROGRESS{"total":1,"processed":1,"success":1,"failed":0,"skipped":0,"current":""}\n'
                '{"report":"/tmp/report.json"}\n'
            )

            def wait(self):
                return 0

        with (
            patch("panel_api_media_runner.subprocess.Popen", return_value=FakeProcess()),
            patch("panel_api_media_runner.emit") as emit,
        ):
            code, lines = runner.run_batch_command(["fake"])
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 2)
        emit.assert_called_once_with(
            "progress", total=1, processed=1, success=1,
            failed=0, skipped=0, current="",
        )

    def test_partial_auxiliary_omission_is_warning_not_product_failure(self):
        report = {
            "summary": {"prepared": 1, "image_warning_products": 1},
            "items": [{"product_id": "1", "name": "手机 A", "result": "verified_draft"}],
            "image_warnings": [{
                "product_id": "1", "name": "手机 A", "uploaded_main_area_count": 4,
                "omitted_auxiliary": [{"name": "白底图_4.png", "reason": "file_too_large"}],
            }],
            "skipped_candidates": [], "manual_queue": [],
        }
        progress, warnings, errors = summarise_media_report(report, save_draft_only=True)
        self.assertEqual(progress["success"], 1)
        self.assertEqual(progress["failed"], 0)
        self.assertEqual(len(warnings), 1)
        self.assertIn("白底图_4.png", warnings[0]["reason"])
        self.assertEqual(errors, [])

    def test_legacy_draft_verified_spelling_remains_compatible(self):
        report = {
            "summary": {"prepared": 1},
            "items": [{"product_id": "1", "name": "手机 A", "result": "draft_verified"}],
            "image_warnings": [], "skipped_candidates": [], "manual_queue": [],
        }
        progress, _, _ = summarise_media_report(report, save_draft_only=True)
        self.assertEqual(progress["success"], 1)

    def test_real_readback_failure_is_manual_error(self):
        report = {
            "summary": {"prepared": 1},
            "items": [{"product_id": "1", "name": "手机 A", "result": "rolled_back"}],
            "image_warnings": [], "skipped_candidates": [],
            "manual_queue": [{"product_id": "1", "name": "手机 A", "reason": "media_readback_failed"}],
        }
        progress, warnings, errors = summarise_media_report(report, save_draft_only=True)
        self.assertEqual(progress["success"], 0)
        self.assertEqual(progress["failed"], 1)
        self.assertEqual(warnings, [])
        self.assertEqual(errors[0]["error_type"], "API 写入或回读失败")

    def test_api_accepted_is_not_counted_as_published(self):
        report = {
            "summary": {"prepared": 1},
            "items": [{"product_id": "1", "name": "手机 A", "result": "accepted"}],
            "image_warnings": [], "skipped_candidates": [], "manual_queue": [],
        }
        progress, _, _ = summarise_media_report(report, save_draft_only=False)
        self.assertEqual(progress["success"], 0)

    def test_online_readback_is_counted_as_published(self):
        report = {
            "summary": {"prepared": 1},
            "items": [{"product_id": "1", "name": "手机 A", "result": "published_readback"}],
            "image_warnings": [], "skipped_candidates": [], "manual_queue": [],
        }
        progress, _, _ = summarise_media_report(report, save_draft_only=False)
        self.assertEqual(progress["success"], 1)

    def test_latest_verified_report_can_resume_without_rewriting_media(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / "panel_data" / "doudian_full_media_canary" / "1"
            reports.mkdir(parents=True)
            path = reports / "1_11.report.json"
            path.write_text(json.dumps({
                "shop_id": "1", "product_id": "11", "result": "verified_draft",
                "verification_errors": [], "publish": False,
            }), encoding="utf-8")
            with patch.object(runner, "WORK_DIR", root):
                self.assertEqual(runner._latest_verified_full_media_report("1", "11"), path.resolve())
