import os
import tempfile
import unittest

import panel_runner


class ShopImageBaseTests(unittest.TestCase):
    def test_legacy_category_layer_is_selected(self):
        with tempfile.TemporaryDirectory() as root:
            leaf = os.path.join(root, "3C数码及配件", "二手手机")
            os.makedirs(leaf)
            self.assertEqual(panel_runner.resolve_shop_image_base(root), leaf)

    def test_flat_shop_root_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "vivo"))
            self.assertEqual(panel_runner.resolve_shop_image_base(root), root)


if __name__ == "__main__":
    unittest.main()
