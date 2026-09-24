import unittest

import test_doudian_batch as dd


class ModelTitleVerifyTests(unittest.TestCase):
    def test_iqoo_12pro_spacing_is_equivalent_in_title_check(self):
        matched, kind = dd._model_appears_in_title(
            "12 Pro", "95新 iQOO iQOO 12Pro 二手手机"
        )
        self.assertTrue(matched)
        self.assertEqual(kind, "normalized")

    def test_model_title_check_ignores_case_and_common_separators(self):
        self.assertTrue(
            dd._model_appears_in_title("GT-8 Pro", "iQOO gt8pro 二手手机")[0]
        )

    def test_model_title_check_still_rejects_a_different_model(self):
        self.assertFalse(
            dd._model_appears_in_title("12 Pro", "iQOO 12 二手手机")[0]
        )

    def test_model_title_check_keeps_plus_omission_compatibility(self):
        matched, kind = dd._model_appears_in_title("X50i+", "荣耀 X50i 二手手机")
        self.assertTrue(matched)
        self.assertEqual(kind, "plus_omitted")


if __name__ == "__main__":
    unittest.main()
