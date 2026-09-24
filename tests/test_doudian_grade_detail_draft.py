import copy
import json
import unittest

from scripts.doudian_grade_detail_draft_once import (
    compose_detail_urls, filter_blocked_detail_urls, select_existing_detail_urls,
    validate_existing_detail_urls, verify_grade_detail,
)


class GradeDetailDraftTests(unittest.TestCase):
    def setUp(self):
        self.before = {
            "product_id_str": "90001", "status": 0, "check_status": 1,
            "name": "小米 15", "name_prefix": "99新Xiaomi/小米",
            "pic": ["https://byte.example/main.jpg"],
            "description": "https://byte.example/old.jpg",
            "product_format_new": json.dumps({"1493": [{"Value": 1, "Name": "15"}]}),
            "specs": [{"name": "颜色", "values": [{"id": 1, "name": "黑色"}]}],
            "spec_prices": [{"sku_id": 1, "stock_num": 1}],
            "spec_pics": [{"spec_detail_id": 1, "pic": "ecom-shop-material/black"}],
        }
        self.urls = [f"https://byte.example/obj/ecom-shop-material/d{index}" for index in range(15)]

    def test_only_description_may_change(self):
        after = copy.deepcopy(self.before)
        after["description"] = "|".join(self.urls)
        self.assertEqual(verify_grade_detail(self.before, after, self.urls), [])

    def test_prefix_or_sku_picture_drift_fails(self):
        after = copy.deepcopy(self.before)
        after["description"] = "|".join(self.urls)
        after["name_prefix"] = None
        after["spec_pics"][0]["pic"] = "ecom-shop-material/other"
        issues = verify_grade_detail(self.before, after, self.urls)
        self.assertTrue(any("name_prefix" in issue for issue in issues))
        self.assertTrue(any("颜色规格图" in issue for issue in issues))

    def test_merge_preserves_all_original_detail_urls_in_order(self):
        originals = [f"https://byte.example/obj/ecom-shop-material/old{i}" for i in range(13)]
        grades = [f"https://byte.example/obj/ecom-shop-material/grade{i}" for i in range(2)]
        merged = compose_detail_urls(originals, grades)
        self.assertEqual(merged, originals[:2] + grades + originals[2:])
        self.assertEqual(merged[:2] + merged[4:], originals)

    def test_merge_can_preserve_six_existing_shop_details(self):
        originals = [f"https://byte.example/obj/ecom-shop-material/old{i}" for i in range(6)]
        grade = ["https://byte.example/obj/ecom-shop-material/nine"]
        merged = compose_detail_urls(originals, grade)
        self.assertEqual(merged, originals[:2] + grade + originals[2:])

    def test_generic_detail_validation_accepts_non_legacy_counts(self):
        for count in (2, 5, 7, 12, 14, 18):
            urls = [f"https://byte.example/obj/ecom-shop-material/d{index}"
                    for index in range(count)]
            validate_existing_detail_urls(urls)
            merged = compose_detail_urls(
                urls, ["https://byte.example/obj/ecom-shop-material/grade"],
            )
            self.assertEqual(len(merged), count + 1)
            self.assertEqual(merged[:2] + merged[3:], urls)

    def test_generic_detail_validation_rejects_unsafe_inputs(self):
        for count in (0, 1, 19):
            urls = [f"https://byte.example/obj/ecom-shop-material/d{index}"
                    for index in range(count)]
            with self.assertRaisesRegex(Exception, "2 至 18"):
                validate_existing_detail_urls(urls)
        with self.assertRaisesRegex(Exception, "URL 无效"):
            validate_existing_detail_urls(["https://byte.example/d0", "file:///tmp/d1"])
        with self.assertRaisesRegex(Exception, "重复"):
            validate_existing_detail_urls([
                "https://a.example/obj/ecom-shop-material/same",
                "https://b.example/obj/ecom-shop-material/same",
            ])

    def test_merge_keeps_existing_exact_grade_and_only_inserts_missing_grade(self):
        existing_grade = "https://byte.example/obj/ecom-shop-material/grade95"
        originals = [
            "https://byte.example/obj/ecom-shop-material/old0",
            "https://byte.example/obj/ecom-shop-material/old1",
            existing_grade,
            "https://byte.example/obj/ecom-shop-material/old3",
        ]
        missing_grade = "https://byte.example/obj/ecom-shop-material/grade99"
        merged = compose_detail_urls(originals, [missing_grade, existing_grade])
        self.assertEqual(merged, originals[:2] + [missing_grade] + originals[2:])
        self.assertEqual(merged.count(existing_grade), 1)

    def test_merge_drops_legacy_purchase_package_detail(self):
        blocked = (
            "https://p3-aio.ecombdimg.com/obj/ecom-shop-material/"
            "jpeg_m_1c7ee38c4ce1e75da8cd70eae3ed619f_sx_142687_www1279-1913"
        )
        originals = [
            "https://byte.example/obj/ecom-shop-material/old0",
            "https://byte.example/obj/ecom-shop-material/old1",
            blocked,
            "https://byte.example/obj/ecom-shop-material/old3",
        ]
        grades = ["https://byte.example/obj/ecom-shop-material/grade0"]
        retained, removed = filter_blocked_detail_urls(originals)
        self.assertEqual(removed, [blocked])
        self.assertNotIn(blocked, compose_detail_urls(originals, grades))
        self.assertEqual(compose_detail_urls(originals, grades), retained[:2] + grades + retained[2:])

    def test_all_three_grades_keep_only_existing_combined_image(self):
        combined = (
            "https://p3-aio.ecombdimg.com/obj/ecom-shop-material/"
            "jpeg_m_d8ba5de976b518f71e4f7621bd9baa58_sx_137187_www1125-1581"
        )
        originals = [
            "https://byte.example/obj/ecom-shop-material/old0",
            "https://byte.example/obj/ecom-shop-material/old1",
            combined,
            "https://byte.example/obj/ecom-shop-material/old3",
        ]
        self.assertEqual(
            compose_detail_urls(originals, [], use_combined_grade_image=True), originals,
        )

    def test_one_or_two_grades_remove_combined_and_insert_individuals(self):
        combined = (
            "https://p3-aio.ecombdimg.com/obj/ecom-shop-material/"
            "jpeg_m_d8ba5de976b518f71e4f7621bd9baa58_sx_137187_www1125-1581"
        )
        originals = [
            "https://byte.example/obj/ecom-shop-material/old0",
            "https://byte.example/obj/ecom-shop-material/old1",
            combined,
            "https://byte.example/obj/ecom-shop-material/old3",
        ]
        grades = ["https://byte.example/obj/ecom-shop-material/99"]
        retained, excluded = select_existing_detail_urls(
            originals, use_combined_grade_image=False,
        )
        self.assertEqual(excluded, [combined])
        self.assertEqual(compose_detail_urls(originals, grades),
                         retained[:2] + grades + retained[2:])

    def test_three_grade_mode_requires_unique_combined_image(self):
        originals = [
            "https://byte.example/obj/ecom-shop-material/old0",
            "https://byte.example/obj/ecom-shop-material/old1",
        ]
        with self.assertRaisesRegex(Exception, "合并成色说明图"):
            compose_detail_urls(originals, [], use_combined_grade_image=True)


if __name__ == "__main__":
    unittest.main()
