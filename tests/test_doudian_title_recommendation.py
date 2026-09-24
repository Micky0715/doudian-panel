import json
import unittest

from integrations.doudian.title_recommendation import (
    TitleRecommendationError, recommend_title_for_submission,
)


class FakeGateway:
    def __init__(self, prefix="99新Xiaomi/小米 "):
        self.prefix = prefix
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "product.getProductUpdateRule":
            return {"data": {"recommend_name_rule": {
                "satisfy_prefix": "true", "prefix_prop_has_brand": True,
                "property_ids": "[3320,1687]",
            }}}
        if method == "product.getRecommendName":
            return {"data": {"recommend_name": [{
                "recommend_scene": "product_name_prefix",
                "recommend_value": self.prefix,
            }]}}
        raise AssertionError(method)


def detail(**overrides):
    value = {
        "name": "小米 17T 二手手机",
        "name_prefix": "99新Xiaomi/小米",
        "standard_brand_id": 261169705,
        "spu_id": 7660337699752100137,
        "category_detail": {
            "first_cid": 1000007020, "first_cname": "3C数码及配件",
            "second_cid": 1000007628, "second_cname": "二手手机",
            "third_cid": 0, "third_cname": "", "fourth_cid": 0,
        },
        "product_format_new": json.dumps({
            "1493": [{"Value": 649832, "Name": "小米 17T"}],
            "3320": [{"Value": 18972, "Name": "99新"}],
        }),
    }
    value.update(overrides)
    return value


class TitleRecommendationTests(unittest.TestCase):
    def test_uses_live_rule_and_preserves_trailing_space(self):
        gateway = FakeGateway()
        plan = recommend_title_for_submission(gateway, detail())
        self.assertEqual(plan.name_prefix, "99新Xiaomi/小米 ")
        self.assertEqual(plan.name, "99新Xiaomi/小米 小米 17T 二手手机")
        self.assertEqual(plan.property_ids, ("3320", "1687"))
        self.assertEqual(gateway.calls[0], (
            "product.getProductUpdateRule",
            {"category_id": 1000007628, "standard_brand_id": 261169705,
             "spu_id": 7660337699752100137},
        ))
        request = gateway.calls[1][1]
        self.assertEqual(request["category_leaf_id"], 1000007628)
        self.assertEqual(request["second_cid_name"], "二手手机")
        self.assertEqual(request["select_property"]["3320"], [
            {"value": 18972, "name": "99新"},
        ])
        self.assertEqual(request["select_property"]["1687"], [
            {"value": 0, "name": "Xiaomi/小米"},
        ])

    def test_existing_folded_prefix_is_not_duplicated(self):
        gateway = FakeGateway()
        plan = recommend_title_for_submission(gateway, detail(
            name="99新Xiaomi/小米 小米 17T 二手手机",
        ))
        self.assertEqual(plan.name, "99新Xiaomi/小米 小米 17T 二手手机")

    def test_missing_space_or_required_property_is_blocked(self):
        with self.assertRaises(TitleRecommendationError):
            recommend_title_for_submission(FakeGateway("99新Xiaomi/小米"), detail())
        with self.assertRaises(TitleRecommendationError):
            recommend_title_for_submission(FakeGateway(), detail(
                product_format_new=json.dumps({"1493": [{"Value": 1, "Name": "M"}]}),
            ))


if __name__ == "__main__":
    unittest.main()
