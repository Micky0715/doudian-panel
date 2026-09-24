import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_doudian_batch as batch


class MainImagePathTests(unittest.TestCase):
    def test_unique_model_named_image_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            main_dir = Path(folder) / "主图"
            main_dir.mkdir()
            image = main_dir / "真我 GT8 Pro.jpg"
            image.write_bytes(b"image")
            with patch.object(batch, "get_image_dir", return_value=folder):
                self.assertEqual(batch.get_main_image_path({}), str(image))

    def test_multiple_nonstandard_images_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            main_dir = Path(folder) / "主图"
            main_dir.mkdir()
            (main_dir / "a.jpg").write_bytes(b"a")
            (main_dir / "b.jpg").write_bytes(b"b")
            with patch.object(batch, "get_image_dir", return_value=folder):
                self.assertEqual(
                    batch.get_main_image_path({}), str(main_dir / "主图.png")
                )


if __name__ == "__main__":
    unittest.main()
