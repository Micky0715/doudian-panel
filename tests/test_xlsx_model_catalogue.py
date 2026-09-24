import unittest

from xlsx_model_catalogue import infer_brand


class XlsxModelCatalogueTests(unittest.TestCase):
    def test_redmi_is_independent_before_xiaomi(self):
        self.assertEqual(infer_brand("Redmi/红米 K80 二手手机", "K80"), "Redmi")

    def test_common_brands(self):
        self.assertEqual(infer_brand("Apple/苹果 iPhone 17", "iPhone 17"), "Apple")
        self.assertEqual(infer_brand("honor/荣耀 400 Pro", "400 Pro"), "Honor")
        self.assertEqual(infer_brand("一加 Ace 6 二手手机", "Ace 6"), "OnePlus")
