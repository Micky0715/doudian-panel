import copy
import unittest

from scripts.doudian_launch_approved_full_media_once import _preflight_issues


class LaunchApprovedFullMediaTests(unittest.TestCase):
    def setUp(self):
        self.live = {
            "product_id_str": "11", "status": 1, "check_status": 7, "draft_status": 0,
            "name": "99新 honor/荣耀 荣耀 400 Pro 二手手机", "name_prefix": "99新 honor/荣耀 ",
            "pic": ["m1", "m2", "m3", "m4", "m5"],
            "description": "|".join("https://byte/d%d" % value for value in range(14)),
            "spec_pics": [{"spec_detail_id": str(value)} for value in range(4)],
            "spec_prices": [{"sku_id": str(value)} for value in range(48)],
        }
        self.report = {
            "shop_id": "1", "product_id": "11",
            "result": "api_accepted_pending_or_needs_review", "publish_api_code": 10000,
            "post_publish_issues": ["online_state_not_confirmed", "pic_changed_after_submit"],
            "grade_image_strategy": "combined_gallery", "target_detail_count": 14,
            "recommended_title": self.live["name"],
            "uploaded": [{"kind": "grade", "source": "/gallery/三合一.jpeg"}],
            "after_publish": {
                "main_count": 5, "detail_count": 14, "color_count": 4, "sku_count": 48,
            },
        }

    def test_exact_approved_waiting_launch_is_ready(self):
        self.assertEqual(_preflight_issues(self.report, self.live, "1", "11"), [])

    def test_wrong_state_or_content_blocks_launch(self):
        changed = copy.deepcopy(self.live)
        changed["check_status"] = 1
        changed["spec_prices"].pop()
        issues = _preflight_issues(self.report, changed, "1", "11")
        self.assertIn("product_not_approved_waiting_launch", issues)
        self.assertIn("sku_count_changed", issues)

    def test_only_known_pending_reasons_are_accepted(self):
        report = copy.deepcopy(self.report)
        report["post_publish_issues"].append("sku_fields_changed")
        self.assertIn(
            "unexpected_source_pending_reasons",
            _preflight_issues(report, self.live, "1", "11"),
        )


if __name__ == "__main__":
    unittest.main()
