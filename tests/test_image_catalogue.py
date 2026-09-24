import tempfile
import unittest
from pathlib import Path

from image_catalogue import (find_model_image_dirs, resolve_cross_store_image_dir,
                             resolve_white_library_image_dir)
from brand_model_rules import image_model_leaf_allowed


class ImageCatalogueTests(unittest.TestCase):
    def test_xiaomi_and_redmi_leaf_identity_stays_separate(self):
        self.assertTrue(image_model_leaf_allowed("小米", "小米13"))
        self.assertFalse(image_model_leaf_allowed("小米", "Redmi 13"))
        self.assertTrue(image_model_leaf_allowed("红米", "Redmi 13"))
        self.assertFalse(image_model_leaf_allowed("红米", "小米13"))

    def test_finds_layered_store_structure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            layered = root / "3C数码及配件" / "二手手机" / "小米" / "REDMI K80"
            (layered / "主图").mkdir(parents=True)
            (layered / "主图" / "主图.png").write_bytes(b"image")
            self.assertEqual(find_model_image_dirs(root, "K80"), [str(layered)])

    def test_does_not_confuse_variants(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ("REDMI K80", "REDMI K80 Pro"):
                (root / name / "白底图").mkdir(parents=True)
                (root / name / "白底图" / "白底图_1.png").write_bytes(name.encode())
            self.assertEqual(find_model_image_dirs(root, "K80"), [str(root / "REDMI K80")])

    def test_returns_all_duplicates_for_operator_resolution(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for parent in ("成员甲", "成员乙"):
                (root / parent / "苹果 iPhone 14 Pro" / "主图").mkdir(parents=True)
                (root / parent / "苹果 iPhone 14 Pro" / "主图" / "主图.png").write_bytes(parent.encode())
            self.assertEqual(len(find_model_image_dirs(root, "iPhone 14 Pro")), 2)

    def test_ignores_plain_folder_without_image_markers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "REDMI K80").mkdir()
            self.assertEqual(find_model_image_dirs(root, "K80"), [])

    def test_cross_store_uses_identical_exact_model_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "shop_empty"
            current.mkdir()
            for shop in ("shop_a", "shop_b"):
                target = root / shop / "Redmi" / "REDMI K80" / "白底图"
                target.mkdir(parents=True)
                (target / "白底图_1.png").write_bytes(b"same-image")
            selected, status, candidates = resolve_cross_store_image_dir(root, current, "Redmi K80")
            self.assertEqual(status, "unique_content")
            self.assertEqual(len(candidates), 2)
            self.assertIn("shop_a", selected)

    def test_cross_store_refuses_conflicting_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            current = root / "shop_empty"
            current.mkdir()
            for shop, content in (("shop_a", b"one"), ("shop_b", b"two")):
                target = root / shop / "Redmi" / "REDMI K80" / "主图"
                target.mkdir(parents=True)
                (target / "主图.png").write_bytes(content)
            selected, status, candidates = resolve_cross_store_image_dir(root, current, "K80")
            self.assertIsNone(selected)
            self.assertEqual(status, "conflicting_sources")
            self.assertEqual(len(candidates), 2)

    def test_white_library_requires_exact_brand_and_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for brand in ("小米", "荣耀"):
                target = root / brand / "Redmi K80" / "白底图"
                target.mkdir(parents=True)
                (target / "白底图_1.png").write_bytes(brand.encode())
            selected, status, candidates = resolve_white_library_image_dir(
                root, "Redmi", "K80"
            )
            self.assertEqual(status, "exact")
            self.assertEqual(len(candidates), 1)
            self.assertIn("小米", selected)

    def test_empty_model_folder_is_not_usable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "荣耀" / "荣耀 400 Pro" / "白底图").mkdir(parents=True)
            self.assertEqual(find_model_image_dirs(root, "400 Pro"), [])


if __name__ == "__main__":
    unittest.main()
