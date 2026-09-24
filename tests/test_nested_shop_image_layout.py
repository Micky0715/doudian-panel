import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock


sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.sync_api", MagicMock())

import test_doudian_batch as batch


class NestedShopImageLayoutTests(unittest.TestCase):
    def setUp(self):
        self.original_base = batch.IMAGE_BASE_DIR

    def tearDown(self):
        batch.IMAGE_BASE_DIR = self.original_base

    def test_browser_resolver_accepts_extra_category_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "非标准导入目录" / "二手手机" / "OPPO" / "OPPO K12s"
            (model / "主图").mkdir(parents=True)
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            batch.IMAGE_BASE_DIR = os.fspath(root)

            resolved = batch.get_image_dir({
                "brand_cn": "OPPO", "brand_normalized": "OPPO", "model": "K12s",
            })

            self.assertEqual(Path(resolved), model)

    def test_browser_resolver_rejects_wrong_brand_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "非标准导入目录" / "vivo" / "OPPO K12s"
            (model / "主图").mkdir(parents=True)
            (model / "主图" / "主图.jpg").write_bytes(b"main")
            batch.IMAGE_BASE_DIR = os.fspath(root)

            resolved = batch.get_image_dir({
                "brand_cn": "OPPO", "brand_normalized": "OPPO", "model": "K12s",
            })

            self.assertNotEqual(Path(resolved), model)


if __name__ == "__main__":
    unittest.main()
