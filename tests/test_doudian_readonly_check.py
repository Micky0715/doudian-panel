import unittest

from scripts.doudian_readonly_check import _first_sku_id, _product_id, _product_rows


class ReadOnlyCheckHelpersTests(unittest.TestCase):
    def test_extracts_documented_product_shapes(self):
        rows = _product_rows({"data": [{"product_id": "3838000000000000000"}]})
        self.assertEqual(_product_id(rows[0]), "3838000000000000000")

    def test_finds_nested_sku_without_confusing_product_id(self):
        detail = {
            "product_id": "3838000000000000000",
            "sku_list": [{"sku_id": "1770000000001"}],
        }
        self.assertEqual(_first_sku_id(detail), "1770000000001")
        self.assertIsNone(_first_sku_id({"product_id": "3838000000000000000"}))


if __name__ == "__main__":
    unittest.main()
