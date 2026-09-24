import unittest

import test_doudian_batch as dd


class SpecificTitleTests(unittest.TestCase):
    def test_huawei_and_honor_inference(self):
        cases = (
            ("【95】Magic Vs3 国行 全网通", "荣耀", "Magic Vs3"),
            ("【95新】Galaxy S25 Ultra 国行 全网通", "三星", "Galaxy S25 Ultra"),
            ("华为 nova 14 活力版 二手手机", "华为", "nova 14 活力版"),
            ("华为 Mate 70 Pro 优享版 国行", "华为", "Mate 70 Pro 优享版"),
            # 解析层保留真实变体；图片目录匹配层可再决定是否安全复用基础款素材。
            ("华为 P50 Pro (麒麟版) 二手手机", "华为", "P50 Pro (麒麟版)"),
        )
        for title, expected_brand, expected_model in cases:
            with self.subTest(title=title):
                parsed = dd.parse_title(title)
                self.assertEqual(parsed["brand_cn"], expected_brand)
                self.assertEqual(parsed["model"], expected_model)

    def test_variant_names_remain_distinct(self):
        models = [
            dd.parse_title("荣耀 Power 二手手机")["model"],
            dd.parse_title("荣耀 Power 2 二手手机")["model"],
            dd.parse_title("荣耀 X60 二手手机")["model"],
            dd.parse_title("荣耀 X60 GT 二手手机")["model"],
        ]
        self.assertEqual(len(set(models)), 4)


if __name__ == "__main__":
    unittest.main()
