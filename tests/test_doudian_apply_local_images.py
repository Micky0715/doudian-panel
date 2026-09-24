import tempfile
import unittest
from pathlib import Path

from scripts.doudian_apply_local_images import (
    LocalImagePolicyError, build_dynamic_main_area_plan, detail_html,
    detail_urls, read_detail_image_set, validate_local_image_set, verify_image_readback,
    verify_media_readback,
)
from integrations.doudian.materials import MAX_SYNC_IMAGE_BYTES


def png_600(seed: bytes) -> bytes:
    """A header-only PNG is sufficient for the script's stdlib dimension parser."""
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + (600).to_bytes(4, "big") + (600).to_bytes(4, "big") + seed


class LocalImageApiScriptTests(unittest.TestCase):
    def write_image(self, root: Path, name: str, content: bytes) -> Path:
        path = root / name
        path.write_bytes(content)
        return path

    def test_local_set_accepts_two_distinct_white_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = self.write_image(root, "main.png", png_600(b"main"))
            white1 = self.write_image(root, "white1.png", png_600(b"white1"))
            white2 = self.write_image(root, "white2.png", png_600(b"white2"))
            resolved_main, whites = validate_local_image_set(main, [white1, white2])
            self.assertEqual(resolved_main.name, "main.png")
            self.assertEqual(len(whites), 2)

    def test_local_set_rejects_duplicate_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = self.write_image(root, "main.png", png_600(b"same"))
            white1 = self.write_image(root, "white1.png", png_600(b"same"))
            white2 = self.write_image(root, "white2.png", png_600(b"two"))
            white3 = self.write_image(root, "white3.png", png_600(b"three"))
            with self.assertRaisesRegex(LocalImagePolicyError, "distinct content"):
                validate_local_image_set(main, [white1, white2, white3])

    def test_readback_requires_uploaded_main_as_first_image(self):
        expected = "https://byte/main|https://byte/one|https://byte/two|https://byte/three"
        good = {"pic": expected}
        good_list = {"pic": expected.split("|")}
        wrong = {"pic": "https://byte/one|https://byte/main|https://byte/two|https://byte/three"}
        self.assertEqual(verify_image_readback(good, expected), [])
        self.assertEqual(verify_image_readback(good_list, expected), [])
        self.assertIn("main_image_does_not_match_uploaded_store_main", verify_image_readback(wrong, expected))

    def test_missing_main_uses_first_white_once_and_needs_three_whites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            whites = [self.write_image(root, f"white{index}.png", png_600(str(index).encode()))
                      for index in range(1, 5)]
            plan = build_dynamic_main_area_plan(None, whites)
            self.assertTrue(plan.main_substituted)
            self.assertEqual(plan.main.path, whites[0].resolve())
            self.assertEqual([item.path for item in plan.auxiliary], [path.resolve() for path in whites[1:]])
            with self.assertRaisesRegex(LocalImagePolicyError, "three distinct"):
                build_dynamic_main_area_plan(None, whites[:2])

    def test_oversized_extra_white_is_omitted_when_three_valid_whites_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = self.write_image(root, "main.png", png_600(b"main"))
            whites = [self.write_image(root, f"white{index}.png", png_600(str(index).encode()))
                      for index in range(1, 4)]
            oversized = self.write_image(root, "white4.png", png_600(b"four") + b"x" * (MAX_SYNC_IMAGE_BYTES + 1))
            plan = build_dynamic_main_area_plan(main, [*whites, oversized])
            self.assertEqual([item.path for item in plan.auxiliary], [path.resolve() for path in whites])
            self.assertEqual([(item.name, item.reason) for item in plan.skipped_auxiliary], [
                ("white4.png", "local image exceeds DouDian synchronous 3MB limit: white4.png"),
            ])

    def test_insufficient_valid_whites_keeps_each_omission_for_reporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            main = self.write_image(root, "main.png", png_600(b"main"))
            valid = self.write_image(root, "white1.png", png_600(b"one"))
            oversized = self.write_image(root, "white2.png", png_600(b"two") + b"x" * (MAX_SYNC_IMAGE_BYTES + 1))
            with self.assertRaisesRegex(LocalImagePolicyError, "two distinct uploadable") as raised:
                build_dynamic_main_area_plan(main, [valid, oversized])
            self.assertEqual([(item.name, item.reason) for item in raised.exception.skipped_auxiliary], [
                ("white2.png", "local image exceeds DouDian synchronous 3MB limit: white2.png"),
            ])

    def test_detail_readback_requires_exact_fourteen_urls(self):
        urls = [f"https://byte.example/detail-{index}.jpg" for index in range(1, 15)]
        good = {"pic": "https://byte.example/main|https://byte.example/one|https://byte.example/two|https://byte.example/three",
                "description": detail_html(urls)}
        self.assertEqual(verify_media_readback(good, good["pic"], urls), [])
        self.assertIn("detail_image_order_or_count_does_not_match_uploaded_set",
                      verify_media_readback({**good, "description": detail_html(urls[:-1] + [urls[-2]])}, good["pic"], urls))

    def test_detail_urls_accepts_documented_url_list_and_reads_legacy_html(self):
        urls = ["https://byte.example/one.jpg", "https://byte.example/two.jpg"]
        self.assertEqual(detail_urls("|".join(urls)), urls)
        self.assertEqual(detail_urls('<p><img src="https://byte.example/one.jpg"/></p>'), urls[:1])


if __name__ == "__main__":
    unittest.main()
