import json
import tempfile
import unittest
from pathlib import Path

from app.listing_preflight import DetailImageInspector, ImageAssetInspector, ListingPreflightInspector
from app.listing_preflight.inspector import extract_property_selection
from integrations.doudian.catalog import PhonePropertyCatalogue
from model_catalogue import ModelCatalogue


class FakeAssets:
    def __init__(self, ok=True):
        self.ok = ok

    def inspect(self, brand, model):
        return {
            "ok": self.ok,
            "image_dir": "/images/model" if self.ok else "",
            "main_count": 1 if self.ok else 0,
            "auxiliary_count": 4 if self.ok else 0,
            "reasons": [] if self.ok else ["model_image_dir_missing"],
        }


class FakeDetails:
    def __init__(self, ok=True):
        self.ok = ok

    def inspect(self, condition):
        return {
            "ok": self.ok,
            "count": 14 if self.ok else 13,
            "condition": condition,
            "files": [],
            "reasons": [] if self.ok else ["detail_image_count_invalid"],
        }


def resolver(_title):
    return {
        "target_brand_cn": "华为",
        "model": "P60",
        "target_model_exact": "",
        "condition": "99新",
    }


def phone_item(product_id="1"):
    return {
        "product_id": product_id,
        "name": "华为 P60 二手手机",
        "category_leaf_id": "1000007628",
    }


class ListingPreflightTests(unittest.TestCase):
    def setUp(self):
        rows = [{"brand": "Huawei", "model": "P60"}]
        self.catalogue = ModelCatalogue(rows)
        self.properties = PhonePropertyCatalogue.from_file()

    def inspector(self, *, assets=True, mappings=None):
        return ListingPreflightInspector(
            resolver=resolver,
            standard_catalogue=self.catalogue,
            platform_catalogue=self.catalogue,
            property_catalogue=self.properties,
            image_inspector=FakeAssets(assets),
            detail_image_inspector=FakeDetails(),
            reviewed_screen_shapes=mappings or [],
        )

    def test_out_of_scope_never_enters_browser(self):
        item = dict(phone_item())
        item["category_leaf_id"] = "1000007679"
        decision = self.inspector().inspect(item, {})
        self.assertEqual(decision.decision, "out_of_scope")

    def test_missing_screen_shape_is_ready_for_draft_only(self):
        decision = self.inspector().inspect(phone_item(), {})
        self.assertEqual(decision.decision, "ready")
        self.assertEqual(decision.execution_mode, "save_draft")
        self.assertIn("screen_shape_unresolved", [row["code"] for row in decision.warnings])

    def test_existing_exact_screen_shape_can_be_ready(self):
        detail = {"product_format_new": json.dumps({
            "6448": [{"value": 346803, "name": "直屏", "diy_type": 0}],
        })}
        decision = self.inspector().inspect(phone_item(), detail)
        self.assertEqual(decision.decision, "ready")
        self.assertEqual(decision.execution_mode, "publish")
        self.assertEqual(decision.screen_shape, "直屏")

    def test_reviewed_mapping_can_be_ready(self):
        mappings = [{"brand": "华为", "model": "P60", "screen_shape": "曲面屏"}]
        decision = self.inspector(mappings=mappings).inspect(phone_item(), {})
        self.assertEqual(decision.decision, "ready")
        self.assertEqual(decision.screen_shape, "曲面屏")

    def test_unknown_mapping_and_missing_images_accumulate_reasons(self):
        mappings = [{"brand": "华为", "model": "P60", "screen_shape": "相似曲面"}]
        decision = self.inspector(assets=False, mappings=mappings).inspect(phone_item(), {})
        codes = [row["code"] for row in decision.reasons]
        self.assertIn("model_image_dir_missing", codes)
        self.assertIn("screen_shape_unresolved", [row["code"] for row in decision.warnings])

    def test_extract_property_selection_rejects_multiple_values(self):
        detail = {"product_format_new": {"6448": [{"value": 1}, {"value": 2}]}}
        self.assertIsNone(extract_property_selection(detail, 6448))

    def test_image_inspector_accepts_unique_content_and_white_substitution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "1" / "华为" / "华为 P60"
            (model / "主图").mkdir(parents=True)
            (model / "白底图").mkdir()
            for index in range(1, 5):
                (model / "白底图" / f"白底图_{index}.jpg").write_bytes(str(index).encode())
            result = ImageAssetInspector(root, "1", allow_white_as_main=True).inspect("华为", "P60")
            self.assertTrue(result["ok"])
            self.assertTrue(result["main_substituted"])
            self.assertEqual(result["auxiliary_count"], 4)

    def test_image_inspector_accepts_legacy_category_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "1" / "3C数码及配件" / "二手手机" / "华为" / "华为 P60"
            (model / "主图").mkdir(parents=True)
            (model / "白底图").mkdir()
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            for index in range(1, 4):
                (model / "白底图" / f"白底图_{index}.jpg").write_bytes(
                    str(index).encode()
                )
            result = ImageAssetInspector(root, "1").inspect("华为", "P60")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["image_dir"]), model.resolve())

    def test_image_inspector_accepts_one_additional_category_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = (
                root / "1" / "非标准导入目录" / "二手手机"
                / "OPPO" / "OPPO K12s"
            )
            (model / "主图").mkdir(parents=True)
            (model / "白底图").mkdir()
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            for index in range(1, 4):
                (model / "白底图" / f"白底图_{index}.jpg").write_bytes(
                    str(index).encode()
                )
            result = ImageAssetInspector(root, "1").inspect("OPPO", "K12s")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["image_dir"]), model.resolve())

    def test_image_inspector_accepts_multiple_import_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = (
                root / "1" / "运营导入" / "第二层" / "3C数码及配件"
                / "二手手机" / "OPPO" / "OPPO K12s"
            )
            (model / "主图").mkdir(parents=True)
            (model / "白底图").mkdir()
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            for index in range(1, 4):
                (model / "白底图" / f"白底图_{index}.jpg").write_bytes(
                    str(index).encode()
                )
            result = ImageAssetInspector(root, "1").inspect("OPPO", "K12s")
            self.assertTrue(result["ok"])
            self.assertEqual(Path(result["image_dir"]), model.resolve())

    def test_image_inspector_uses_explicit_model_override_for_folder_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "1" / "二手手机" / "小米" / "Redmi Note 14"
            (model / "主图").mkdir(parents=True)
            (model / "白底图").mkdir()
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            for index in range(1, 4):
                (model / "白底图" / f"白底图_{index}.jpg").write_bytes(
                    str(index).encode()
                )
            result = ImageAssetInspector(root, "1").inspect("红米", "Note 14 5G")
            self.assertTrue(result["ok"])
            self.assertEqual(result["matched_lookup_model"], "Note 14")
            self.assertEqual(Path(result["image_dir"]), model.resolve())

    def test_image_inspector_rejects_nested_model_under_wrong_brand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "1" / "非标准导入目录" / "vivo" / "OPPO K12s"
            (model / "主图").mkdir(parents=True)
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            result = ImageAssetInspector(root, "1").inspect("OPPO", "K12s")
            self.assertFalse(result["ok"])
            self.assertIn("model_image_dir_missing", result["reasons"])

    def test_detail_image_inspector_requires_exact_fourteen_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stem in ("1", "2", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "99新"):
                (root / f"{stem}.jpg").write_bytes(stem.encode())
            result = DetailImageInspector(root).inspect("99新")
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 14)
            (root / "14.jpg").unlink()
            result = DetailImageInspector(root).inspect("99新")
            self.assertFalse(result["ok"])
            self.assertIn("detail_image_slot_missing:14", result["reasons"])

    def test_detail_image_inspector_rejects_unknown_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            result = DetailImageInspector(directory).inspect("")
            self.assertFalse(result["ok"])
            self.assertIn("condition_unresolved", result["reasons"])

    def test_nearly_new_needs_only_common_detail_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for stem in ("1", "2", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14"):
                (root / f"{stem}.jpg").write_bytes(stem.encode())
            result = DetailImageInspector(root).inspect("准新品")
            self.assertTrue(result["ok"])
            self.assertEqual(result["count"], 13)
            self.assertFalse(any("准新品" in path for path in result["files"]))


if __name__ == "__main__":
    unittest.main()
