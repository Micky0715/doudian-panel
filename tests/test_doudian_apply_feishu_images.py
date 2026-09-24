import unittest

from scripts.doudian_apply_feishu_images import _pic_urls, _verify_readback


class ApplyFeishuImagesTests(unittest.TestCase):
    def test_readback_requires_exact_first_image_and_order(self):
        expected = "https://byte.example/main.jpg|https://byte.example/w1.jpg|https://byte.example/w2.jpg|https://byte.example/w3.jpg"
        self.assertEqual(_verify_readback({"pic": expected}, expected), [])
        issues = _verify_readback({"pic": "https://byte.example/w1.jpg|" + expected}, expected)
        self.assertIn("main_image_does_not_match_planned_first_image", issues)
        self.assertIn("product_image_order_or_count_does_not_match_uploaded_set", issues)

    def test_picture_normalisation_does_not_treat_unknown_shape_as_success(self):
        self.assertEqual(_pic_urls({"url": "https://wrong.shape"}), [])
        self.assertEqual(_pic_urls([{"byte_url": "https://byte.example/a.jpg"}]), ["https://byte.example/a.jpg"])


if __name__ == "__main__":
    unittest.main()
