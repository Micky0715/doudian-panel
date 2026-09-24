import unittest

import test_doudian_batch as dd


class RedmiParseTests(unittest.TestCase):
    CASES = (
        ("REDMI K80 二手手机", "REDMI K80"),
        ("REDMI K80 Pro 二手手机", "REDMI K80 Pro"),
        ("REDMI K90 Pro Max 二手手机", "REDMI K90 Pro Max"),
        ("REDMI Note 15 Pro+ 二手手机", "REDMI Note 15 Pro+"),
        ("REDMI Turbo 4 Pro 二手手机", "REDMI Turbo 4 Pro"),
        ("REDMI 15R 5G 二手手机", "REDMI 15R 5G"),
        ("Redmi 13C 5G 二手手机", "Redmi 13C 5G"),
        ("Redmi K40 Pro+ 二手手机", "Redmi K40 Pro+"),
        ("红米 7a 二手手机", "Redmi 7a"),
        ("小米 REDMI K80 二手手机", "REDMI K80"),
        ("Xiaomi/小米 REDMI K90 Pro Max 二手手机", "REDMI K90 Pro Max"),
    )

    def test_redmi_titles_use_independent_platform_brand(self):
        for title, expected_model in self.CASES:
            with self.subTest(title=title):
                parsed = dd.parse_title(title)
                plan = dd.resolve_brand_model_plan(parsed)
                self.assertEqual(parsed["brand_cn"], "红米")
                self.assertEqual(parsed["model"], expected_model)
                self.assertEqual(plan["target_brand_value"], "Redmi/红米")
                self.assertFalse(dd._brand_value_matches_plan(plan, "Xiaomi/小米"))


if __name__ == "__main__":
    unittest.main()
