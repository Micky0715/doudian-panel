import copy
import json
import unittest

from scripts.doudian_submit_full_media_canary_once import SOURCE_REPORT, _snapshot_issues


class FullMediaSubmissionGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
        path = SOURCE_REPORT.with_name(SOURCE_REPORT.name.replace(".report.json", ".after.json"))
        cls.saved = json.loads(path.read_text(encoding="utf-8"))["detail"]

    def test_unchanged_verified_draft_is_eligible(self):
        self.assertEqual(_snapshot_issues(self.report, self.saved, self.saved), [])

    def test_wrong_brand_is_blocked(self):
        live = copy.deepcopy(self.saved)
        live["standard_brand_id"] = 261169705
        self.assertIn("standard_brand_not_redmi", _snapshot_issues(self.report, self.saved, live))

    def test_changed_colors_are_blocked(self):
        live = copy.deepcopy(self.saved)
        live["spec_pics"] = []
        self.assertIn("expected_three_color_images", _snapshot_issues(self.report, self.saved, live))

    def test_single_zero_to_one_stock_sync_is_audited_but_allowed(self):
        live = copy.deepcopy(self.saved)
        live["spec_prices"][0]["stock_num"] = 1
        self.assertEqual(_snapshot_issues(self.report, self.saved, live), [])

    def test_price_change_is_blocked(self):
        live = copy.deepcopy(self.saved)
        live["spec_prices"][0]["price"] += 1
        self.assertIn("spec_prices_changed_since_media_verification",
                      _snapshot_issues(self.report, self.saved, live))

    def test_non_draft_is_blocked(self):
        live = copy.deepcopy(self.saved)
        live["status"] = 1
        self.assertIn("product_not_editable_draft", _snapshot_issues(self.report, self.saved, live))


if __name__ == "__main__":
    unittest.main()
