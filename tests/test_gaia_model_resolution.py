import unittest

from app.gaia_listing.application.model_resolution import (
    resolve_model_records,
    resolve_standard_sku,
    resolve_operator_models,
)
from app.gaia_listing.domain.mapping_contract import ModelMappingRecord


class GaiaModelResolutionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            ModelMappingRecord("苹果", "iPhone 17", "sku-1", "手机"),
            ModelMappingRecord("Apple", "iPhone 17", "sku-2", "手机"),
            ModelMappingRecord("红米", "REDMI K90", "sku-3", "手机"),
        ]

    def test_shared_brand_and_model_normalisation_returns_all_sku_candidates(self):
        result = resolve_model_records(self.rows, brand="Apple", model="Apple iPhone 17")
        self.assertEqual("exact", result.strategy)
        self.assertEqual(["sku-1", "sku-2"], [item.standard_sku_id for item in result.records])
        self.assertFalse(result.as_dict()["unique_sku"])

    def test_version_suffix_fallback_never_guesses_one_variant(self):
        result = resolve_model_records(self.rows, brand="红米", model="K90 运营商定制版")
        self.assertEqual("version_suffix_removed", result.strategy)
        self.assertEqual("sku-3", result.records[0].standard_sku_id)

    def test_standard_sku_is_the_authoritative_unique_key(self):
        result = resolve_standard_sku(self.rows, "sku-2")
        self.assertEqual("standard_sku_id", result.strategy)
        self.assertEqual("iPhone 17", result.records[0].model)

    def test_one_model_expands_all_skus_and_repeated_input_is_deduplicated(self):
        result = resolve_operator_models(self.rows, [
            {"model": "iPhone 17"}, {"brand": "Apple", "model": "IPHONE17"},
        ])
        self.assertTrue(result["success"])
        self.assertEqual(["sku-1", "sku-2"], result["selected_sku_ids"])
        self.assertEqual(2, result["deduplicated_count"])
        self.assertTrue(result["results"][0]["unique_model"])
        self.assertFalse(result["results"][0]["unique_sku"])

    def test_explicit_brand_prefix_does_not_expand_other_brands(self):
        rows = [ModelMappingRecord(brand, model, str(index)) for index, (brand, model) in
                enumerate([("小米", "小米 15"), ("一加", "一加 15"), ("iQOO", "iQOO 15")])]
        result = resolve_operator_models(rows, [{"model": "小米15"}])
        self.assertEqual(["0"], result["selected_sku_ids"])
        self.assertFalse(resolve_operator_models(rows, [{"model": "15"}])["success"])

    def test_missing_or_non_phone_models_do_not_produce_partial_selection(self):
        rows = self.rows + [ModelMappingRecord("苹果", "iPhone 17", "not-phone", "平板")]
        self.assertEqual(["sku-1", "sku-2"], resolve_operator_models(rows, [{"model": "iPhone17"}])["selected_sku_ids"])
        result = resolve_operator_models(rows, [{"model": "iPhone17"}, {"model": "不存在"}])
        self.assertFalse(result["success"])
        self.assertEqual([], result["selected_sku_ids"])

    def test_partial_selection_keeps_only_unambiguous_phone_models(self):
        rows = self.rows + [
            ModelMappingRecord("甲牌", "Common X", "sku-a", "手机"),
            ModelMappingRecord("乙牌", "Common X", "sku-b", "手机"),
        ]
        result = resolve_operator_models(rows, [
            {"brand": "苹果", "model": "iPhone 17"},
            {"model": "iPhone 18 Pro"},
            {"model": "Common X"},
            {"brand": "红米", "model": "K90"},
        ], allow_partial=True)
        self.assertTrue(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(["sku-1", "sku-2", "sku-3"], result["selected_sku_ids"])
        self.assertEqual(2, result["matched_model_count"])
        self.assertEqual(2, result["skipped_model_count"])
        self.assertEqual([(2, "not_found"), (3, "ambiguous")], [
            (item["index"], item["reason"]) for item in result["skipped_models"]])

    def test_partial_selection_still_blocks_without_safe_rows_or_above_sku_limit(self):
        missing = resolve_operator_models(self.rows, [{"model": "iPhone 18 Pro"}], allow_partial=True)
        self.assertFalse(missing["success"])
        self.assertEqual([], missing["selected_sku_ids"])
        rows = [ModelMappingRecord("苹果", "iPhone 17", str(n)) for n in range(2001)]
        oversized = resolve_operator_models(rows, [
            {"model": "iPhone 17"}, {"model": "iPhone 18 Pro"},
        ], allow_partial=True)
        self.assertFalse(oversized["success"])
        self.assertEqual([], oversized["selected_sku_ids"])

    def test_prior_platform_rejection_excludes_only_bad_sku_without_guessing_model(self):
        result = resolve_operator_models(self.rows, [{"model": "iPhone 17"}],
                                         rejected_skus={"sku-1": "old-batch"})
        self.assertTrue(result["success"])
        self.assertEqual(["sku-2"], result["selected_sku_ids"])
        self.assertEqual(["sku-1"], result["results"][0]["excluded_sku_ids"])
        self.assertEqual("old-batch", result["excluded_skus"][0]["rejected_batch_id"])

        no_safe_variant = resolve_operator_models(self.rows, [{"model": "REDMI K90"}],
                                                  allow_partial=True,
                                                  rejected_skus={"sku-3": "old-batch"})
        self.assertFalse(no_safe_variant["success"])
        self.assertEqual("platform_rejected_sku", no_safe_variant["skipped_models"][0]["reason"])

        ambiguous = [ModelMappingRecord("甲牌", "Common X", "sku-a", "手机"),
                     ModelMappingRecord("乙牌", "Common X", "sku-b", "手机")]
        collision = resolve_operator_models(ambiguous, [{"model": "Common X"}],
                                            allow_partial=True,
                                            rejected_skus={"sku-a": "old-batch"})
        self.assertFalse(collision["success"], "quarantine must not resolve cross-brand ambiguity")

    def test_limit_is_expanded_sku_count_not_number_of_models(self):
        rows = [ModelMappingRecord("苹果", "iPhone 17", str(n)) for n in range(2001)]
        result = resolve_operator_models(rows, [{"model": "iPhone17"}])
        self.assertFalse(result["success"])
        self.assertIn("2000", result["message"])


if __name__ == "__main__":
    unittest.main()
