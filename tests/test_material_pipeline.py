import hashlib
import tempfile
import unittest
from pathlib import Path

from app.material_pipeline import CloudImage, MaterialBundle, MaterialPolicyError, build_material_plan
from app.material_pipeline.cache import MaterialMetadataCache


def image(token, revision, body=b"image", name="image.jpg"):
    return CloudImage(token, revision, name, hashlib.sha256(body).hexdigest(), len(body))


class MaterialPipelineTests(unittest.TestCase):
    def test_real_main_uses_two_distinct_white_images(self):
        main = image("main", "v1", b"main", "主图.jpg")
        whites = tuple(image(f"w{index}", "v1", str(index).encode(), f"白底图_{index}.jpg") for index in range(1, 3))
        plan = build_material_plan(MaterialBundle("156450040", "vivo", "X200", main, whites))
        self.assertEqual(plan.source, "feishu_drive")
        self.assertEqual(plan.as_dict()["main"]["file_token"], "main")

        with self.assertRaisesRegex(MaterialPolicyError, "distinct content"):
            build_material_plan(MaterialBundle("156450040", "vivo", "X200", main, (main, whites[0])))

    def test_missing_main_uses_first_of_three_white_images_as_explicit_fallback(self):
        whites = tuple(
            image(f"w{index}", "v1", str(index).encode(), f"白底图_{index}.jpg")
            for index in range(1, 5)
        )
        plan = build_material_plan(MaterialBundle("156450040", "vivo", "X200", None, whites))
        self.assertTrue(plan.main_substituted)
        self.assertEqual(plan.main.file_token, "w1")
        self.assertEqual([item.file_token for item in plan.white_images], ["w2", "w3", "w4"])

        with self.assertRaisesRegex(MaterialPolicyError, "requires three"):
            build_material_plan(MaterialBundle("156450040", "vivo", "X200", None, whites[:2]))

    def test_metadata_cache_is_keyed_by_remote_revision_and_digest(self):
        with tempfile.TemporaryDirectory() as root:
            cache = MaterialMetadataCache(Path(root) / "cache")
            record = {
                "shop_id": "156450040", "folder_id": "123", "file_token": "token",
                "revision": "version", "sha256": "a" * 64,
                "material_id": "m1", "byte_url": "https://byte.example/image.jpg",
            }
            path = cache.put(record)
            self.assertTrue(path.is_file())
            self.assertEqual(cache.get("156450040", "123", "token", "version", "a" * 64)["material_id"], "m1")
            self.assertIsNone(cache.get("156450040", "456", "token", "version", "a" * 64))
            self.assertIsNone(cache.get("156450040", "123", "token", "version-2", "a" * 64))


if __name__ == "__main__":
    unittest.main()
