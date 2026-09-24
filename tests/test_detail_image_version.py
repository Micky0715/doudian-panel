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


if __name__ == "__main__":
    unittest.main()
