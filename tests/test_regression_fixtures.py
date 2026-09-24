import json
import os
import unittest

from test_doudian_batch import resolve_brand_model


class HistoricalRegressionFixturesTest(unittest.TestCase):
    def test_all_historical_cases(self):
        path = os.path.join(os.path.dirname(__file__), "regression_fixtures.json")
        with open(path, encoding="utf-8") as fixture_file:
            fixtures = json.load(fixture_file)
        self.assertGreaterEqual(len(fixtures), 15)
        for case in fixtures:
            with self.subTest(title=case["title"], categories=case.get("categories")):
                resolved = resolve_brand_model(case["title"])
                self.assertEqual(case["expect_brand"], resolved["brand"])
                self.assertEqual(case["expect_model"], resolved["model"])

    def test_stray_channel_marker_before_repeated_apple_brand_is_removed(self):
        resolved = resolve_brand_model(
            "准新品Apple/苹果 z苹果 iPhone 17 Pro 二手手机"
        )
        self.assertEqual("Apple/苹果", resolved["brand"])
        self.assertEqual("iPhone 17 Pro", resolved["model"])

    def test_redmi_14r_keeps_model_defining_5g_suffix(self):
        resolved = resolve_brand_model(
            "99新Xiaomi/小米 Redmi 14R 5G 二手手机"
        )
        self.assertEqual("Redmi/红米", resolved["brand"])
        self.assertEqual("14R 5G", resolved["model"])

    def test_stray_marker_cleanup_does_not_touch_real_z_series_model(self):
        resolved = resolve_brand_model("vivo Z10 Turbo 二手手机")
        self.assertEqual("Z10 Turbo", resolved["model"])
