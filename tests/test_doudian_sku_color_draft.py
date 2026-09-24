import copy
import json
import unittest

from scripts.doudian_sku_color_draft_once import verify_draft


class SkuColorDraftVerificationTests(unittest.TestCase):
    def setUp(self):
        self.before = {
            "product_id_str": "90001", "status": 0, "check_status": 1,
            "name": "iQOO Z9", "pic": "old-main", "description": "old-detail",
            "product_format_new": json.dumps({"1493": [{"Value": 9, "Name": "Z9"}]}),
            "specs": [{"name": "颜色", "values": [
                {"id": 1, "name": "青"}, {"id": 2, "name": "黑"},
            ]}],
            "spec_prices": [{"sku_id": "1", "price": 100, "stock_num": 5}],
            "spec_pics": [],
        }
        self.expected = {
            "1": "https://p3.example/obj/ecom-shop-material/green",
            "2": "https://p3.example/obj/ecom-shop-material/black",
        }

    def test_exact_spec_id_mapping_accepts_platform_path_normalization(self):
        after = copy.deepcopy(self.before)
        after["spec_pics"] = [
            {"spec_detail_id": 1, "pic": "ecom-shop-material/green"},
            {"spec_detail_id": 2, "pic": "ecom-shop-material/black"},
        ]
        self.assertEqual(verify_draft(self.before, after, self.expected), [])

    def test_swapped_colors_and_sku_changes_fail(self):
        after = copy.deepcopy(self.before)
        after["spec_pics"] = [
            {"spec_detail_id": 1, "pic": "ecom-shop-material/black"},
            {"spec_detail_id": 2, "pic": "ecom-shop-material/green"},
        ]
        after["spec_prices"][0]["stock_num"] = 0
        errors = verify_draft(self.before, after, self.expected)
        self.assertTrue(any("规格值 ID" in error for error in errors))
        self.assertTrue(any("spec_prices" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
