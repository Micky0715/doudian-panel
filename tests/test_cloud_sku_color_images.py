import hashlib
import unittest

from app.material_pipeline import CloudImage
from app.sku_color_images import ColorGalleryError, resolve_cloud_color_images


def image(token, name):
    body = token.encode("utf-8")
    return CloudImage(token, "v1", name, hashlib.sha256(body).hexdigest(), len(body))


def detail(*colours):
    return {"specs": [{"name": "颜色", "values": [
        {"id": str(index), "name": name}
        for index, name in enumerate(colours, 101)
    ]}]}


class CloudSkuColourTests(unittest.TestCase):
    def test_exact_colour_file_stems_map_to_platform_ids(self):
        plan = resolve_cloud_color_images(
            (image("a", "海阔天空.jpg"), image("b", "大漠银月.jpeg")),
            detail("大漠银月", "海阔天空"),
        )
        self.assertTrue(plan.complete)
        self.assertEqual(
            [(item.color_name, item.spec_detail_id, item.image.file_token)
             for item in plan.images],
            [("大漠银月", "101", "b"), ("海阔天空", "102", "a")],
        )

    def test_missing_colour_is_reported_without_guessing(self):
        plan = resolve_cloud_color_images(
            (image("a", "海阔天空.jpg"),), detail("海阔天空", "大漠银月"),
        )
        self.assertFalse(plan.complete)
        self.assertEqual(plan.missing, ("大漠银月",))

    def test_duplicate_colour_files_are_ambiguous(self):
        plan = resolve_cloud_color_images(
            (image("a", "海阔天空.jpg"), image("b", "海阔天空.png")),
            detail("海阔天空"),
        )
        self.assertFalse(plan.complete)
        self.assertIn("多个云盘图片", plan.invalid[0][1])

    def test_first_spec_group_must_be_colour(self):
        with self.assertRaisesRegex(ColorGalleryError, "第一组规格不是颜色"):
            resolve_cloud_color_images(
                (image("a", "海阔天空.jpg"),),
                {"specs": [{"name": "内存", "values": [{"id": "1", "name": "12G"}]}]},
            )


if __name__ == "__main__":
    unittest.main()
