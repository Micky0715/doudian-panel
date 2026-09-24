import struct
import tempfile
import unittest
from pathlib import Path

from app.sku_color_images import ColorGalleryError, resolve_color_images


def _png(width=800, height=800):
    # The catalogue checks source identity and IHDR dimensions; the platform
    # material endpoint performs the full raster decode on actual upload.
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


def _detail(*colors):
    return {"specs": [{"name": "颜色", "values": [
        {"id": str(index), "name": name} for index, name in enumerate(colors, 101)
    ]}]}


class SkuColorCatalogueTests(unittest.TestCase):
    def test_explicit_model_override_can_resolve_gallery_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "Redmi" / "Redmi Note 14"
            model.mkdir(parents=True)
            (model / "黑色.png").write_bytes(_png())
            plan = resolve_color_images(
                folder, "红米", "Note 14 5G", _detail("黑色"),
            )
            self.assertEqual(plan.model_directory, model.resolve())

    def test_exact_color_and_spec_id_with_extra_suffix_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "vivo" / "vivo X200 Pro"
            model.mkdir(parents=True)
            (model / "白月光.png").write_bytes(_png())
            (model / "白月光_2.png").write_bytes(_png())
            plan = resolve_color_images(folder, "vivo", "X200 Pro", _detail("白月光", "辰夜黑"))
            self.assertEqual([(item.color_name, item.spec_detail_id, item.path.name)
                              for item in plan.images], [("白月光", "101", "白月光.png")])
            self.assertEqual(plan.missing, ("辰夜黑",))

    def test_redmi_does_not_cross_into_xiaomi(self):
        with tempfile.TemporaryDirectory() as folder:
            for brand, leaf in (("Redmi", "Redmi 13"), ("小米", "小米 13")):
                model = Path(folder) / brand / leaf
                model.mkdir(parents=True)
                (model / "黑色.png").write_bytes(_png())
            redmi = resolve_color_images(folder, "红米", "13", _detail("黑色"))
            xiaomi = resolve_color_images(folder, "小米", "13", _detail("黑色"))
            self.assertEqual(redmi.brand_directory.name, "Redmi")
            self.assertEqual(xiaomi.brand_directory.name, "小米")

    def test_literal_model_wins_but_normalized_collision_blocks(self):
        with tempfile.TemporaryDirectory() as folder:
            for leaf in ("vivo X200 Ultra", "vivo X200Ultra"):
                model = Path(folder) / "vivo" / leaf
                model.mkdir(parents=True)
                (model / "银调.png").write_bytes(_png())
            exact = resolve_color_images(folder, "vivo", "X200 Ultra", _detail("银调"))
            self.assertEqual(exact.model_directory.name, "vivo X200 Ultra")
            with self.assertRaisesRegex(ColorGalleryError, "多个匹配项"):
                resolve_color_images(folder, "vivo", "X200-Ultra", _detail("银调"))

    def test_duplicate_colour_extensions_are_ambiguous_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "vivo" / "vivo X200 Pro"
            model.mkdir(parents=True)
            (model / "白月光.png").write_bytes(_png())
            (model / "白月光.jpg").write_bytes(b"\xff\xd8")
            plan = resolve_color_images(folder, "vivo", "X200 Pro", _detail("白月光"))
            self.assertEqual(plan.images, ())
            self.assertIn("多个图片", plan.invalid[0][1])

    def test_colour_must_be_first_spec_group(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "vivo" / "vivo X200 Pro"
            model.mkdir(parents=True)
            detail = {"specs": [{"name": "内存", "values": [{"id": 1, "name": "128G"}]},
                                {"name": "颜色", "values": [{"id": 2, "name": "白色"}]}]}
            with self.assertRaisesRegex(ColorGalleryError, "第一组规格不是颜色"):
                resolve_color_images(folder, "vivo", "X200 Pro", detail)

    def test_invalid_image_is_reported_without_colour_substitution(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "vivo" / "vivo X200 Pro"
            model.mkdir(parents=True)
            (model / "白月光.png").write_bytes(_png(800, 600))
            (model / "辰夜黑.png").write_bytes(_png())
            plan = resolve_color_images(folder, "vivo", "X200 Pro", _detail("白月光", "辰夜黑"))
            self.assertEqual([item.color_name for item in plan.images], ["辰夜黑"])
            self.assertIn("1:1", plan.invalid[0][1])

    def test_png_content_under_jpg_name_keeps_exact_colour_and_corrects_upload_name(self):
        with tempfile.TemporaryDirectory() as folder:
            model = Path(folder) / "iQOO" / "iQOO Z9"
            model.mkdir(parents=True)
            (model / "山野青.jpg").write_bytes(_png())
            plan = resolve_color_images(folder, "iQOO", "Z9", _detail("山野青"))
            self.assertEqual(plan.images[0].path.name, "山野青.jpg")
            self.assertEqual(plan.images[0].upload_name, "山野青.png")
            self.assertTrue(plan.images[0].format_warning)


if __name__ == "__main__":
    unittest.main()
