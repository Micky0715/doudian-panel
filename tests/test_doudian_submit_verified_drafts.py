import json
import unittest

from scripts.doudian_submit_verified_drafts import (
    _load_candidates, _payload, _preflight,
)


def detail(**overrides):
    value = {
        "product_id": "90001", "status": 0, "check_status": 1, "name": "测试手机",
        "pic": "https://example.invalid/main.jpg", "description": "<img>",
        "spec_prices": [{"price": 100}], "freight_id": 123,
        "product_format_new": json.dumps({
            "1493": [{"Value": 1, "Name": "Z9x", "diy_type": 0}],
            "6448": [{"Value": 2, "Name": "直屏", "diy_type": 0}],
            "3320": [{"Value": 3, "Name": "99新", "diy_type": 0}],
        }),
        "category_detail": {
            "first_cid": 1000007020, "first_cname": "3C数码及配件",
            "second_cid": 1000007628, "second_cname": "二手手机",
            "third_cid": 0, "third_cname": "", "fourth_cid": 0,
        },
    }
    value.update(overrides)
    return value


class SubmitVerifiedDraftTests(unittest.TestCase):
    source_item = {
        "product_id": "90001", "result": "verified",
        "model": {"value": 1, "name": "Z9x"},
        "screen_shape": {"value": 2, "name": "直屏"},
    }

    def test_preflight_requires_complete_draft_and_matching_standard_properties(self):
        issues, checks = _preflight(detail(), self.source_item)
        self.assertEqual(issues, [])
        self.assertEqual(checks["screen"]["name"], "直屏")
        issues, _ = _preflight(detail(pic=""), self.source_item)
        self.assertIn("missing_pic", issues)
        issues, _ = _preflight(detail(product_format_new=json.dumps({"1493": [{"Value": 1, "Name": "Z9x"}]})), self.source_item)
        self.assertIn("missing_screen", issues)

    def test_payload_uses_live_recommended_title(self):
        class Gateway:
            def call(self, method, params):
                if method == "product.getProductUpdateRule":
                    return {"data": {"recommend_name_rule": {
                        "satisfy_prefix": True, "property_ids": [3320],
                    }}}
                return {"data": {"recommend_name": [{
                    "recommend_scene": "product_name_prefix",
                    "recommend_value": "99新测试品牌 ",
                }]}}

        payload = _payload(detail(), Gateway())
        self.assertEqual(set(payload), {
            "product_id", "product_format_new", "name", "name_prefix", "commit",
        })
        self.assertTrue(payload["commit"])
        self.assertEqual(payload["name_prefix"], "99新测试品牌 ")
        self.assertEqual(payload["name"], "99新测试品牌 测试手机")

    def test_source_must_be_verified_save_draft_report(self):
        report = {"shop_id": "10001", "commit": False, "items": [self.source_item]}
        self.assertEqual(_load_candidates(report, "10001", 1), [self.source_item])
        with self.assertRaises(ValueError):
            _load_candidates({**report, "commit": True}, "10001", 1)


if __name__ == "__main__":
    unittest.main()
