import json
import unittest

from doudian_product_service import (
    ProductTransformError, build_model_edit_payload, build_property_edit_payload,
    replace_model_property,
)


class ProductTransformTests(unittest.TestCase):
    def setUp(self):
        self.original = json.dumps({
            "1493": [{
                "Value": 0, "Name": "16P", "PropertyId": 1493,
                "PropertyName": "型号", "diy_type": 1, "measure_info": None,
            }],
            "1687": [{"Value": 99, "Name": "Apple", "PropertyId": 1687}],
            "3320": [{"Value": 18972, "Name": "99新", "PropertyId": 3320}],
        }, ensure_ascii=False)
        self.option = {"value": "454970", "name": "iPhone 16 Pro"}

    def test_replaces_only_model_and_uses_platform_enum(self):
        result = json.loads(replace_model_property(self.original, self.option))
        self.assertEqual(result["1493"][0]["value"], 454970)
        self.assertEqual(result["1493"][0]["name"], "iPhone 16 Pro")
        self.assertEqual(result["1493"][0]["diy_type"], 0)
        self.assertEqual(result["1687"][0]["name"], "Apple")
        self.assertEqual(result["3320"][0]["name"], "99新")
        self.assertNotIn("Name", result["1687"][0])
        self.assertNotIn("PropertyId", result["1687"][0])

    def test_builds_minimal_edit_payload(self):
        payload = build_model_edit_payload(
            {"product_id_str": "3838000000000000000", "product_format_new": self.original},
            self.option,
        )
        self.assertEqual(set(payload), {"product_id", "product_format_new"})
        self.assertEqual(payload["product_id"], "3838000000000000000")

    def test_rejects_missing_or_invalid_data(self):
        with self.assertRaises(ProductTransformError):
            replace_model_property("not-json", self.option)
        with self.assertRaises(ProductTransformError):
            build_model_edit_payload({"product_format_new": self.original}, self.option)
        with self.assertRaises(ProductTransformError):
            replace_model_property(self.original, {"name": "iPhone 16 Pro"})

    def test_builds_multiple_standard_property_replacements(self):
        payload = build_property_edit_payload(
            {"product_id": "3", "product_format_new": self.original},
            {1493: self.option, 6448: {"value": "346803", "name": "直屏"}},
        )
        properties = json.loads(payload["product_format_new"])
        self.assertEqual(properties["1493"][0]["value"], 454970)
        self.assertEqual(properties["6448"][0], {
            "value": 346803, "name": "直屏", "diy_type": 0,
        })


if __name__ == "__main__":
    unittest.main()
