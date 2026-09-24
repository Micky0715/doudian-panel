import unittest

from app.gaia_listing.domain.mapping_contract import (
    GaiaMappingContractError,
    ModelMappingRecord,
    QualityMappingRecord,
)
from app.gaia_listing.domain.template_contract import (
    GAIA_TEMPLATE_HEADERS,
    GaiaTemplateContractError,
    GaiaTemplateRow,
)


class GaiaTemplateContractTests(unittest.TestCase):
    def test_latest_template_has_eight_writable_columns(self):
        self.assertEqual(len(GAIA_TEMPLATE_HEADERS), 8)
        self.assertEqual(GAIA_TEMPLATE_HEADERS[2], "是否质检链接（1-是，0-否）")

    def test_phone_row_uses_confirmed_fixed_values(self):
        row = GaiaTemplateRow("2", "sku-100", "quality-99", "手机")
        self.assertEqual(
            row.values(),
            ("2", "A", "1", "sku-100", "quality-99", "", "D且E且G", "a"),
        )

    def test_non_phone_strategy_is_blank(self):
        self.assertEqual(
            GaiaTemplateRow("1", "sku-200", "quality-95", "平板").values()[6],
            "",
        )

    def test_missing_quality_id_blocks_generation(self):
        with self.assertRaises(GaiaTemplateContractError):
            GaiaTemplateRow("2", "sku-100", "", "手机")

    def test_rule_prefix_is_lowercase_a_without_changing_other_prefixes(self):
        for category in ("手机", "平板", "笔记本"):
            values = GaiaTemplateRow("2", "sku-100", "99", category).values()
            self.assertEqual("a", values[7])
            self.assertEqual("A", values[1])
            self.assertEqual("", values[5])

    def test_uploaded_model_mapping_requires_model_and_standard_sku(self):
        self.assertEqual(ModelMappingRecord("Apple", "iPhone 16", "123").standard_sku_id, "123")
        with self.assertRaises(GaiaMappingContractError):
            ModelMappingRecord("Apple", "", "123")

    def test_quality_mapping_requires_real_id(self):
        with self.assertRaises(GaiaMappingContractError):
            QualityMappingRecord("95新", "")


if __name__ == "__main__":
    unittest.main()
