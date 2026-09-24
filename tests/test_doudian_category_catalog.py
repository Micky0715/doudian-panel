import unittest

from doudian_api import DoudianConfigError
from integrations.doudian import DoudianCategoryReader, PhonePropertyCatalogue
from integrations.doudian.catalog import PropertyCatalogueError


class FakeGateway:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {
            "code": 10000,
            "data": {"data": [{
                "property_id": 6448,
                "property_name": "屏幕形态",
                "options": [{"name": "直屏", "value_id": 346803}],
            }]},
        }


class DoudianCategoryReaderTests(unittest.TestCase):
    def test_reads_category_properties_once_and_uses_cache(self):
        gateway = FakeGateway()
        reader = DoudianCategoryReader(gateway)
        first = reader.get_properties(1000007628)
        second = reader.get_properties("1000007628")
        self.assertEqual(first, second)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0], (
            "product.getCatePropertyV2", {"category_leaf_id": 1000007628},
        ))

    def test_explicit_refresh_bypasses_cache(self):
        gateway = FakeGateway()
        reader = DoudianCategoryReader(gateway)
        reader.get_properties(1000007628)
        reader.get_properties(1000007628, refresh=True)
        self.assertEqual(len(gateway.calls), 2)

    def test_finds_property_by_exact_name(self):
        reader = DoudianCategoryReader(FakeGateway())
        self.assertEqual(reader.find_property(1000007628, "屏幕形态")["property_id"], 6448)
        self.assertIsNone(reader.find_property(1000007628, "屏幕"))

    def test_rejects_invalid_category(self):
        with self.assertRaises(DoudianConfigError):
            DoudianCategoryReader(FakeGateway()).get_properties("phone")


class PhonePropertyCatalogueTests(unittest.TestCase):
    def test_screen_shape_ids_match_live_catalogue(self):
        catalogue = PhonePropertyCatalogue.from_file()
        self.assertEqual(catalogue.category_id, 1000007628)
        prop = catalogue.get_property("screen_shape")
        self.assertEqual(prop["property_id"], 6448)
        self.assertEqual(
            {item["name"]: item["value_id"] for item in prop["options"]},
            {
                "直屏": 346803,
                "曲面屏": 11719,
                "四曲面屏": 675620,
                "全等深微四曲屏": 676520,
                "悬浮四曲屏": 676521,
            },
        )

    def test_builds_exact_selection_without_guessing(self):
        catalogue = PhonePropertyCatalogue.from_file()
        self.assertEqual(catalogue.build_product_format_selection(
            "screen_shape", "全等深微四曲屏",
        ), {
            "6448": [{"value": 676520, "name": "全等深微四曲屏", "diy_type": 0}],
        })
        with self.assertRaises(PropertyCatalogueError):
            catalogue.resolve_option("screen_shape", "四曲屏")


if __name__ == "__main__":
    unittest.main()
