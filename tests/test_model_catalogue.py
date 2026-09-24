import json
import tempfile
import unittest
from pathlib import Path

from model_catalogue import (
    ModelCatalogue, canonical_brand, model_key, versionless_model_candidates,
)


ROWS = [
    {"brand": "小米", "model": "REDMI K80", "source": "excel"},
    {"brand": "小米", "model": "REDMI K80 Pro", "source": "excel"},
    {"brand": "小米", "model": "REDMI K80 (至尊版)", "source": "excel"},
    {"brand": "Apple", "model": "苹果 iPhone 14 Pro", "source": "excel"},
]


class ModelCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.catalogue = ModelCatalogue(ROWS)

    def test_redmi_maps_to_xiaomi_brand_group(self):
        hit = self.catalogue.resolve("Redmi", "K80")
        self.assertEqual(hit["model"], "REDMI K80")
        self.assertEqual(canonical_brand(hit["brand"]), "xiaomi")

    def test_variants_never_collapse(self):
        self.assertEqual(self.catalogue.resolve("小米", "K80 Pro")["model"], "REDMI K80 Pro")
        self.assertEqual(self.catalogue.resolve("小米", "K80 至尊版")["model"], "REDMI K80 (至尊版)")
        self.assertIsNone(self.catalogue.resolve("小米", "K80E"))

    def test_short_catalogue_model_cannot_replace_pro_variant(self):
        platform = ModelCatalogue([{"brand": "realme", "model": "GT8"}])
        self.assertIsNone(platform.resolve("真我", "GT8 Pro"))

    def test_doudian_supreme_alias_keeps_variant(self):
        platform = ModelCatalogue([
            {"brand": "Redmi", "model": "K70"},
            {"brand": "Redmi", "model": "K70至尊"},
        ])
        hit = platform.resolve("Redmi", "Redmi K70 (至尊版)")
        self.assertEqual(hit["model"], "K70至尊")

    def test_brand_prefix_and_formatting_are_safe(self):
        self.assertEqual(model_key("苹果 iPhone 14 Pro"), model_key("iPhone14 Pro"))
        self.assertNotEqual(model_key("iPhone 14 Pro"), model_key("iPhone 14 Pro Max"))

    def test_only_trailing_edition_labels_are_removed(self):
        self.assertEqual(
            versionless_model_candidates("Galaxy A56 (5G版)"),
            ("Galaxy A56",),
        )
        self.assertEqual(
            versionless_model_candidates("nova 14 活力版"),
            ("nova 14",),
        )
        self.assertEqual(
            versionless_model_candidates("K70至尊"),
            ("K70",),
        )
        self.assertEqual(versionless_model_candidates("iPhone 16 Pro Max"), ())

    def test_wrong_brand_does_not_cross_match(self):
        self.assertIsNone(self.catalogue.resolve("华为", "K80"))

    def test_loads_export_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "models.json"
            path.write_text(json.dumps({"catalogue": ROWS}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(len(ModelCatalogue.from_json(path).rows), 4)


if __name__ == "__main__":
    unittest.main()
