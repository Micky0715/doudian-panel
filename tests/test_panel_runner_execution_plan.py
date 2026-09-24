import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import panel_runner
from app.listing_execution import build_execution_plan, store_execution_plan


def ready(product_id):
    return {
        "product_id": str(product_id),
        "name": f"99新华为 P60 二手手机 {product_id}",
        "decision": "ready",
        "brand": "华为",
        "model": "P60",
        "platform_model": "P60",
        "screen_shape": "直屏",
        "condition": "99新",
        "image_dir": "/images/156450040/华为/P60",
        "main_image_count": 1,
        "auxiliary_image_count": 4,
        "detail_image_count": 14,
        "execution_mode": "publish",
        "warnings": [],
        "reasons": [],
    }


class FakePage:
    def __init__(self):
        self.goto_calls = []

    def goto(self, *args, **kwargs):
        self.goto_calls.append((args, kwargs))

    def wait_for_timeout(self, _value):
        pass


class PanelRunnerExecutionPlanTests(unittest.TestCase):
    def test_plan_skips_scan_and_does_not_return_to_draft_between_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {
                "schema_version": 1,
                "shop_id": "156450040",
                "ready": 2,
                "manual_required": 0,
                "out_of_scope": 0,
                "decisions": [ready("1"), ready("2")],
            }
            report_path = root / "report.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            plan_path = store_execution_plan(
                build_execution_plan(
                    report, report_path, action="publish", now=int(time.time())
                ),
                root / "plans",
            )
            page = FakePage()
            processed = []

            def process(_page, product_id, _name):
                processed.append(product_id)
                return True, 0.1

            with (
                patch.object(panel_runner, "EXECUTION_PLAN_PATH", str(plan_path)),
                patch.object(panel_runner, "SHOP_ID", "156450040"),
                patch.dict(
                    panel_runner.os.environ,
                    {"DP_TARGET_PRODUCT_ID": "", "DP_SAVE_DRAFT_ONLY": "0"},
                ),
                patch.object(panel_runner.dd, "collect_all_draft_ids", side_effect=AssertionError("scan called")),
                patch.object(panel_runner.dd, "load_processed_log", return_value={"products": {}}),
                patch.object(panel_runner.dd, "parse_title", return_value={"brand_cn": "华为", "model": "P60"}),
                patch.object(panel_runner.dd, "process_single_product", side_effect=process),
                patch.object(panel_runner.dd, "mark_product_done"),
                patch.object(panel_runner, "classify_image_error", return_value=("", "")),
                patch.object(panel_runner, "emit_progress"),
                patch.object(panel_runner, "emit_errors"),
                patch.object(panel_runner, "should_stop", return_value=False),
            ):
                panel_runner.run_batch_with_stop(page, test_mode=True, test_n=1)

            self.assertEqual(processed, ["1", "2"])
            self.assertEqual(page.goto_calls, [])


if __name__ == "__main__":
    unittest.main()
