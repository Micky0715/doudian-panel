import copy
import json
import unittest

from scripts.doudian_submit_verified_full_media_once import (
    _only_unit_stock_increments, _verified_report_issues,
)


class SubmitVerifiedFullMediaTests(unittest.TestCase):
    def setUp(self):
        self.detail = {
            "product_id_str": "11", "status": 0, "check_status": 1, "draft_status": 1,
            "name": "手机", "name_prefix": "95新", "name_suffix": None,
            "pic": ["m1", "m2", "m3", "m4"],
            "description": "|".join("https://byte.example/d%d" % value for value in range(14)),
            "spec_pics": [{"spec_detail_id": "c1", "pic": "c"}],
            "specs": [], "spec_prices": [{"sku_id": "s1", "stock_num": 0}],
            "brand_id": 1, "standard_brand_id": 2, "spec_id": 3,
            "freight_id": 4, "product_type": 0,
            "product_format_new": json.dumps({
                "1493": [{"value": 10, "name": "M", "diy_type": 0}],
                "6448": [{"value": 20, "name": "直屏", "diy_type": 0}],
            }),
        }
        self.report = {
            "shop_id": "1", "product_id": "11", "result": "verified_draft",
            "verification_errors": [], "publish": False,
            "white": ["w1", "w2", "w3"], "target_detail_count": 14,
            "colors": [{"name": "黑"}], "sku_count": 1,
            "model_option": {"value": 10, "name": "M"},
            "screen_shape": {"value": 20, "name": "直屏"},
        }

    def test_unchanged_verified_draft_is_ready(self):
        self.report["target_detail_count"] = 15
        self.assertEqual(_verified_report_issues(
            self.report, self.detail, copy.deepcopy(self.detail), "1", "11",
        ), [])

    def test_changed_colour_data_blocks_publish(self):
        live = copy.deepcopy(self.detail)
        live["spec_pics"] = []
        issues = _verified_report_issues(self.report, self.detail, live, "1", "11")
        self.assertIn("draft_changed_since_verification", issues)
        self.assertIn("color_count_changed", issues)

    def test_only_explicit_verified_rate_limit_failure_can_be_retried(self):
        retry_report = {
            **self.report,
            "result": "publish_rejected_or_unknown_needs_manual_review",
            "publish": True,
            "error": (
                "DoudianAPIError: product.editV2 failed: code=60000 "
                "sub_code=isv.request-limited message=请求太频繁，请稍后重试"
            ),
        }
        self.assertIn("source_report_not_verified", _verified_report_issues(
            retry_report, self.detail, copy.deepcopy(self.detail), "1", "11",
        ))
        self.assertEqual(_verified_report_issues(
            retry_report, self.detail, copy.deepcopy(self.detail), "1", "11",
            allow_rate_limit_retry=True,
        ), [])

    def test_only_explicit_verified_read_timeout_can_recover(self):
        timeout_report = {
            **self.report,
            "result": "failed_after_draft_write_needs_manual_review",
            "publish": True,
            "api_code": 10000,
            "publish_api_code": None,
            "publish_payload_sha256": None,
            "error": "TimeoutError: The read operation timed out",
        }
        self.assertIn("source_report_not_verified", _verified_report_issues(
            timeout_report, self.detail, copy.deepcopy(self.detail), "1", "11",
        ))
        self.assertEqual(_verified_report_issues(
            timeout_report, self.detail, copy.deepcopy(self.detail), "1", "11",
            allow_verified_read_timeout_recovery=True,
        ), [])

        changed = copy.deepcopy(timeout_report)
        changed["publish_payload_sha256"] = "already-started"
        self.assertIn("source_report_not_verified", _verified_report_issues(
            changed, self.detail, copy.deepcopy(self.detail), "1", "11",
            allow_verified_read_timeout_recovery=True,
        ))

    def test_only_explicit_unit_stock_sync_can_recover(self):
        original = copy.deepcopy(self.detail)
        saved = copy.deepcopy(original)
        saved["spec_prices"][0]["stock_num"] += 1
        stock_report = {
            **self.report,
            "result": "unverified_needs_manual_review",
            "verification_errors": ["spec_prices 改变"],
            "publish": True,
            "api_code": 10000,
            "publish_api_code": None,
            "publish_payload_sha256": None,
        }
        self.assertTrue(_only_unit_stock_increments(original, saved))
        self.assertIn("source_report_not_verified", _verified_report_issues(
            stock_report, saved, copy.deepcopy(saved), "1", "11", original=original,
        ))
        self.assertEqual(_verified_report_issues(
            stock_report, saved, copy.deepcopy(saved), "1", "11",
            allow_unit_stock_sync_recovery=True, original=original,
        ), [])

        unsafe = copy.deepcopy(saved)
        unsafe["spec_prices"][0]["price"] = 99
        self.assertFalse(_only_unit_stock_increments(original, unsafe))
        self.assertIn("source_report_not_verified", _verified_report_issues(
            stock_report, unsafe, copy.deepcopy(unsafe), "1", "11",
            allow_unit_stock_sync_recovery=True, original=original,
        ))


if __name__ == "__main__":
    unittest.main()
