import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_doudian_batch as batch


class DetailImageVersionTests(unittest.TestCase):
    def _images(self, root: Path):
        paths = []
        for index in range(batch.DETAIL_IMAGE_COUNT):
            path = root / (f"{index + 1}.jpg")
            path.write_bytes(f"image-{index}".encode("ascii"))
            paths.append(str(path))
        return paths

    def test_fingerprint_changes_when_image_content_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._images(Path(temp_dir))
            first = batch.calculate_detail_image_version(paths)
            Path(paths[4]).write_bytes(b"updated-image")
            second = batch.calculate_detail_image_version(paths)
            self.assertNotEqual(first, second)

    def test_incomplete_set_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = self._images(Path(temp_dir))
            with self.assertRaises(ValueError):
                batch.calculate_detail_image_version(paths[:12])

    def test_skip_requires_count_and_matching_recorded_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            version_root = os.path.join(temp_dir, "versions")
            with patch.object(batch, "DETAIL_VERSION_DIR", version_root):
                refresh, _ = batch.should_refresh_detail_images("shop", "product", 14, "v1")
                self.assertTrue(refresh)

                batch.save_detail_image_version("shop", "product", "v1")
                refresh, _ = batch.should_refresh_detail_images("shop", "product", 14, "v1")
                self.assertFalse(refresh)

                refresh, _ = batch.should_refresh_detail_images("shop", "product", 14, "v2")
                self.assertTrue(refresh)

                refresh, _ = batch.should_refresh_detail_images("shop", "product", 13, "v1")
                self.assertTrue(refresh)

    def test_nearly_new_paths_and_version_have_no_grade_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for slot in (1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14):
                (root / f"{slot}.jpg").write_bytes(str(slot).encode())
            (root / "99新.jpg").write_bytes(b"not-for-nearly-new")
            with patch.object(batch, "DETAIL_IMAGE_DIR", str(root)):
                paths = batch.get_detail_image_paths({"condition": "准新品"})
            self.assertEqual(len(paths), 13)
            self.assertFalse(any("99新" in path for path in paths))
            version = batch.calculate_detail_image_version(paths, 13)
            self.assertTrue(version)
            with patch.object(batch, "DETAIL_VERSION_DIR", str(root / "versions")):
                batch.save_detail_image_version("shop", "product", version)
                self.assertEqual(batch.should_refresh_detail_images(
                    "shop", "product", 13, version, 13,
                )[0], False)


if __name__ == "__main__":
    unittest.main()
