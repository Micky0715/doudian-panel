import unittest

import test_doudian_batch as dd


class TitleMarketingCleanupTests(unittest.TestCase):
    def test_oppo_find_title_without_brand_prefix_is_inferred(self):
        parsed = dd.parse_title("【全款】Find X7 Ultra 二手手机")
        self.assertEqual(parsed["brand_cn"], "OPPO")
        self.assertEqual(parsed["model"], "Find X7 Ultra")

    def test_redmi_is_independent_brand_and_keeps_model_prefix(self):
        parsed = dd.parse_title("99新 Xiaomi/小米 Redmi Turbo 3 二手手机 骁龙8s 小米澎湃OS")
        self.assertEqual(parsed["brand_cn"], "红米")
        self.assertEqual(parsed["brand_normalized"], "Redmi/红米")
        self.assertEqual(parsed["model"], "Redmi Turbo 3")

    def test_xiaomi_hyperos_without_real_model_stays_empty(self):
        compact = dd.parse_title("小米澎湃OS")
        spaced = dd.parse_title("99新 Xiaomi/小米 小米澎湃OS 二手手机")
        self.assertEqual(compact["brand_cn"], "小米")
        self.assertEqual(compact["model"], "")
        self.assertEqual(spaced["model"], "")

    def test_xiaomi_hyperos_suffix_does_not_hide_real_model(self):
        parsed = dd.parse_title("小米 17 小米澎湃OS 二手手机")
        self.assertEqual(parsed["brand_cn"], "小米")
        self.assertEqual(parsed["model"], "17")

    def test_redmi_chinese_prefix_normalizes_to_redmi(self):
        parsed = dd.parse_title("红米 K80 Pro 二手手机")
        self.assertEqual(parsed["brand_cn"], "红米")
        self.assertEqual(parsed["model"], "Redmi K80 Pro")

    def test_redmi_note_14_maps_to_platform_5g_option(self):
        parsed = dd.parse_title("Redmi Note 14 二手手机")
        key, exact = dd._resolve_model_override(
            parsed["model"], parsed["brand_cn"], "Redmi"
        )
        self.assertEqual(key, "Note 14")
        self.assertEqual(exact, "Note 14 5G")

    def test_huawei_prefix_is_case_insensitive(self):
        upper = dd.parse_title("99新 HUAWEI/华为 HUAWEI nova 15 Pro 昆仑玻璃 二手手机")
        mixed = dd.parse_title("99新 Huawei/华为 Huawei nova 15 Pro 昆仑玻璃 二手手机")
        self.assertEqual(upper["brand_cn"], "华为")
        self.assertEqual(upper["model"], "nova 15 Pro")
        self.assertEqual(mixed["model"], "nova 15 Pro")

    def test_channel_marketing_suffixes_are_removed(self):
        parsed = dd.parse_title("99新 OPPO Find X8s 二手手机 严选优品 智能机-Z")
        self.assertEqual(parsed["model"], "Find X8s")

    def test_large_screen_marketing_text_is_not_part_of_model(self):
        parsed = dd.parse_title(
            "99新 honor/荣耀 【24期免息】荣耀 Magic8 Pro 智能大屏二手手机"
        )
        self.assertEqual(parsed["brand_cn"], "荣耀")
        self.assertEqual(parsed["model"], "Magic8 Pro")

    def test_real_variant_suffixes_are_retained(self):
        parsed = dd.parse_title("华为 Pura X Max 典藏版 折叠屏 商务机-Z")
        self.assertEqual(parsed["model"], "Pura X Max 典藏版")

    def test_camera_and_capacity_marketing_are_removed(self):
        iphone = dd.parse_title("苹果 iPhone 14 Pro 高容 二手手机")
        vivo = dd.parse_title("vivo X200s 蔡司潜望长焦 二手手机")
        xiaomi = dd.parse_title("小米 17 光影猎人 二手手机")
        self.assertEqual(iphone["model"], "iPhone 14 Pro")
        self.assertEqual(vivo["model"], "X200s")
        self.assertEqual(xiaomi["model"], "17")

    def test_truncated_phone_tail_is_removed(self):
        parsed = dd.parse_title("华为 Mate X6 二手手")
        self.assertEqual(parsed["model"], "Mate X6")

        combined = dd.parse_title("华为 Mate X6 二手折叠屏手机-Z")
        self.assertEqual(combined["model"], "Mate X6")


if __name__ == "__main__":
    unittest.main()
