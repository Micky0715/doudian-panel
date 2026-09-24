import copy
import json
import unittest
from types import SimpleNamespace

from doudian_api import DoudianAPIError
from scripts.doudian_full_media_draft_once import (
    FullMediaCanaryError, _category_leaf_id, _post_publish_issues,
    _brand_identity_issue, _main_pic_3_4_issue, _planned_brand, _spec_pic_slots,
    _publish_with_retry, _source_identity, _versionless_cloud_color_issue,
    resolve_official_model, verify_full_media,
)
from scripts.doudian_edit_phone_properties_batch import (
    _model_option, _versionless_model_candidates, model_option_suggestions,
)
from scripts.doudian_sku_color_draft_once import _source_title


class FullMediaDraftTests(unittest.TestCase):

    def test_version_suffix_folder_requires_complete_colour_proof(self):
        plan = SimpleNamespace(model_folder_resolution="version_suffix_removed")
        self.assertIn("基础版图片", _versionless_cloud_color_issue(plan, False))
        self.assertIsNone(_versionless_cloud_color_issue(plan, True))
        self.assertIsNone(_versionless_cloud_color_issue(
            SimpleNamespace(model_folder_resolution="exact"), False,
        ))

    @staticmethod
    def _model_detail(name=""):
        values = [] if not name else [{"value": 0, "name": name, "diy_type": 1}]
        return {"product_format_new": json.dumps({"1493": values})}

    def test_reviewed_iphone_17_pro_max_mapping_chooses_confirmed_code(self):
        prop = {"options": [
            {"value": "559043", "name": "iPhone 17 Pro Max"},
            {"value": "588610", "name": "iphone 17 Pro Max"},
        ]}
        option, source = _model_option(
            self._model_detail("苹果 iPhone 17 Pro Max"),
            "99新Apple/苹果 苹果 iPhone 17 Pro Max 二手手机", prop,
        )
        self.assertEqual(option, {"value": "559043", "name": "iPhone 17 Pro Max"})
        self.assertEqual(source, "reviewed_override")

    def test_reviewed_magic8_rsr_uses_live_short_platform_name(self):
        prop = {"options": [
            {"value": "629505", "name": "Magic8 RSR"},
            {"value": "574268", "name": "Magic8 Pro"},
        ]}
        option, source = _model_option(
            self._model_detail("荣耀 Magic8 RSR 保时捷设计"),
            "95新honor/荣耀 荣耀 Magic8 RSR 保时捷设计 二手手机", prop,
        )
        self.assertEqual(option, {"value": "629505", "name": "Magic8 RSR"})
        self.assertEqual(source, "reviewed_override")

    def test_reviewed_redmi_14r_5g_keeps_gallery_name_but_uses_live_platform_option(self):
        prop = {"options": [
            {"value": "468434", "name": "14R"},
            {"value": "23244", "name": "14"},
        ]}
        option, source = _model_option(
            self._model_detail(),
            "99新Xiaomi/小米 Redmi 14R 5G 二手手机", prop,
        )
        self.assertEqual(option, {"value": "468434", "name": "14R"})
        self.assertEqual(source, "reviewed_override")

    def test_version_suffix_fallback_uses_unique_exact_base_model(self):
        fold_prop = {"options": [
            {"value": "550668", "name": "Galaxy Z Fold7"},
            {"value": "556342", "name": " Z FOLD 7"},
            {"value": "556343", "name": "Galaxy Z Fold 7"},
        ]}
        option, source = _model_option(
            self._model_detail(),
            "99新 Samsung/三星 三星 Galaxy Z Fold7 (5G版) 二手手机", fold_prop,
        )
        self.assertEqual(option, {"value": "550668", "name": "Galaxy Z Fold7"})
        self.assertEqual(source, "version_suffix_removed")

        pura_prop = {"options": [
            {"value": "346855", "name": "Pura 70"},
            {"value": "346856", "name": "Pura 70 Pro"},
        ]}
        option, source = _model_option(
            self._model_detail(),
            "99新 Huawei/华为 华为 Pura 70 北斗卫星消息版 二手手机", pura_prop,
        )
        self.assertEqual(option, {"value": "346855", "name": "Pura 70"})
        self.assertEqual(source, "version_suffix_removed")

    def test_full_edition_match_wins_before_removing_version_suffix(self):
        prop = {"options": [
            {"value": "full", "name": "nova 14 活力版"},
            {"value": "base", "name": "nova 14"},
        ]}
        option, source = _model_option(
            self._model_detail(), "99新 Huawei/华为 nova 14 活力版 二手手机", prop,
        )
        self.assertEqual(option, {"value": "full", "name": "nova 14 活力版"})
        self.assertEqual(source, "resolved")

    def test_temporary_random_rule_is_stable_and_never_uses_fuzzy_candidates(self):
        ambiguous = {"options": [
            {"value": "626914", "name": "Reno 15 C"},
            {"value": "633379", "name": "Reno 15c"},
            {"value": "592091", "name": "Reno15"},
        ]}
        first, first_source = _model_option(
            self._model_detail(), "99新 OPPO Reno15c 二手手机", ambiguous,
        )
        second, second_source = _model_option(
            self._model_detail(), "99新 OPPO Reno15c 二手手机", ambiguous,
        )
        self.assertEqual(first, second)
        self.assertIn(first["value"], {"626914", "633379"})
        self.assertNotEqual(first["value"], "592091")
        self.assertEqual(first_source, "temporary_stable_random")
        self.assertEqual(second_source, "temporary_stable_random")

        fuzzy_only = {"options": [
            {"value": "47725", "name": "X300"},
            {"value": "47723", "name": "X30"},
        ]}
        option, source = _model_option(
            self._model_detail(), "99新 vivo X300 E 二手手机", fuzzy_only,
        )
        self.assertIsNone(option)
        self.assertEqual(source, "unresolved")

    def test_version_suffix_removal_never_guesses_between_duplicate_base_codes(self):
        self.assertEqual(
            _versionless_model_candidates("Galaxy Z Fold7 (5G版)"),
            ("Galaxy Z Fold7",),
        )
        prop = {"options": [
            {"value": "one", "name": "Model X"},
            {"value": "two", "name": "model x"},
        ]}
        option, source = _model_option(
            self._model_detail(), "99新 Brand/品牌 Model X 典藏版 二手手机", prop,
        )
        self.assertIsNone(option)
        self.assertEqual(source, "unresolved")
        suggestions = model_option_suggestions(prop, "Model X 典藏版")
        self.assertEqual({item["value"] for item in suggestions}, {"one", "two"})

    def test_official_registry_disambiguates_live_case_variants(self):
        x60 = {"options": [
            {"value": "265979", "name": "X60 PRO"},
            {"value": "47796", "name": "X60 Pro"},
            {"value": "272427", "name": "X60PRO"},
            {"value": "229255", "name": "X60Pro"},
        ]}
        option, source = _model_option(
            self._model_detail(), "95新 honor/荣耀 荣耀 X60 Pro 二手手机", x60,
        )
        self.assertEqual(option, {"value": "47796", "name": "X60 Pro"})
        self.assertEqual(source, "official_registry")

        nova = {"options": [
            {"value": "534793", "name": "nova 14 Ultra"},
            {"value": "541830", "name": "nova 14 ultra"},
        ]}
        option, source = _model_option(
            self._model_detail(), "99新 Huawei/华为 华为 nova 14 Ultra 二手手机", nova,
        )
        self.assertEqual(option, {"value": "534793", "name": "nova 14 Ultra"})
        self.assertEqual(source, "official_registry")

    def test_official_distinct_model_is_not_collapsed_to_similar_platform_model(self):
        prop = {"options": [
            {"value": "558938", "name": "畅玩70 Plus"},
            {"value": "212285", "name": "畅玩 30 Plus"},
        ]}
        option, source = _model_option(
            self._model_detail(), "95新 honor/荣耀 荣耀 畅玩70m Plus 二手手机", prop,
        )
        self.assertIsNone(option)
        self.assertEqual(source, "unresolved")

    def test_publish_retries_twice_for_explicit_retryable_platform_error(self):
        class FakePublisher:
            def __init__(self):
                self.calls = 0

            def submit_planned_draft(self, submission):
                self.calls += 1
                if self.calls <= 2:
                    raise DoudianAPIError("product.editV2", {
                        "code": 20000,
                        "sub_code": "isp.service-error:2010001",
                        "sub_msg": "校验发货模式失败,请重试",
                    })
                return {"code": 10000}

        publisher = FakePublisher()
        delays = []
        failures = []
        response, attempts, readback = _publish_with_retry(
            publisher, {"product_id": "1", "commit": True},
            read_detail=lambda: {"status": 0, "check_status": 1},
            expected_fingerprint="", retries=2,
            sleep=delays.append,
            on_failure=lambda *args: failures.append(args),
        )
        self.assertEqual(response, {"code": 10000})
        self.assertEqual(attempts, 3)
        self.assertIsNone(readback)
        self.assertEqual(publisher.calls, 3)
        self.assertEqual(delays, [3.0, 6.0])
        self.assertEqual(len(failures), 2)

    def test_publish_does_not_blindly_retry_uncertain_transport_error(self):
        class FakePublisher:
            calls = 0

            def submit_planned_draft(self, submission):
                self.calls += 1
                raise TimeoutError("connection closed after request")

        publisher = FakePublisher()
        delays = []
        with self.assertRaises(TimeoutError):
            _publish_with_retry(
                publisher, {"product_id": "1", "commit": True},
                read_detail=lambda: {"status": 0, "check_status": 1},
                expected_fingerprint="", retries=2, sleep=delays.append,
            )
        self.assertEqual(publisher.calls, 1)
        self.assertEqual(delays, [])

    def test_publish_readback_prevents_duplicate_after_uncertain_response(self):
        class FakePublisher:
            calls = 0

            def submit_planned_draft(self, submission):
                self.calls += 1
                raise TimeoutError("response was lost")

        publisher = FakePublisher()
        live = {"status": 0, "check_status": 3, "product_id": "1"}
        response, attempts, readback = _publish_with_retry(
            publisher, {"product_id": "1", "commit": True},
            read_detail=lambda: live,
            expected_fingerprint="", retries=2,
            sleep=lambda _: self.fail("accepted readback must not retry"),
        )
        self.assertTrue(response["readback_confirmed"])
        self.assertEqual(attempts, 1)
        self.assertIs(readback, live)
        self.assertEqual(publisher.calls, 1)

    def test_publish_does_not_retry_non_retryable_platform_rejection(self):
        class FakePublisher:
            calls = 0

            def submit_planned_draft(self, submission):
                self.calls += 1
                raise DoudianAPIError("product.editV2", {
                    "code": 20000,
                    "sub_code": "isv.invalid-param",
                    "sub_msg": "商品参数错误",
                })

        publisher = FakePublisher()
        with self.assertRaises(DoudianAPIError):
            _publish_with_retry(
                publisher, {"product_id": "1", "commit": True},
                read_detail=lambda: {"status": 0, "check_status": 1},
                expected_fingerprint="", retries=2,
                sleep=lambda _: self.fail("invalid parameter must not retry"),
            )
        self.assertEqual(publisher.calls, 1)

    def test_publish_stops_if_draft_changes_before_retry(self):
        class FakePublisher:
            calls = 0

            def submit_planned_draft(self, submission):
                self.calls += 1
                raise DoudianAPIError("product.editV2", {
                    "code": 20000,
                    "sub_code": "isp.service-error:2010001",
                    "sub_msg": "校验发货模式失败,请重试",
                })

        publisher = FakePublisher()
        failures = []
        with self.assertRaisesRegex(FullMediaCanaryError, "草稿发生变化"):
            _publish_with_retry(
                publisher, {"product_id": "1", "commit": True},
                read_detail=lambda: {"status": 0, "check_status": 1, "name": "changed"},
                expected_fingerprint="definitely-not-current", retries=2,
                sleep=lambda _: self.fail("changed draft must not retry"),
                on_failure=lambda *args: failures.append(args),
            )
        self.assertEqual(publisher.calls, 1)
        self.assertIsNone(failures[0][1])

    def test_source_identity_uses_snapshot_name_when_detail_loses_model(self):
        title, parsed, source = _source_identity({
            "name_prefix": "99新 Redmi/红米 ",
            "name": "Redmi/红米",
        }, "REDMI Turbo 4 二手手机")
        self.assertEqual(source, "snapshot_fallback")
        self.assertEqual(parsed["target_brand_cn"], "红米")
        self.assertEqual(parsed["model"], "Turbo 4")
        self.assertIn("Turbo 4", title)

    def test_source_identity_keeps_healthy_detail_authoritative(self):
        title, parsed, source = _source_identity({
            "name_prefix": "95新 Redmi/红米 ",
            "name": "REDMI K70 Pro 二手手机",
        }, "REDMI Turbo 4 二手手机")
        self.assertEqual(source, "api_detail")
        self.assertEqual(parsed["model"], "K70 Pro")
        self.assertIn("K70 Pro", title)

    def test_existing_main_pic_3_4_must_really_be_three_by_four(self):
        self.assertIsNone(_main_pic_3_4_issue({"main_pic_3_4": []}))
        self.assertIsNone(_main_pic_3_4_issue({
            "main_pic_3_4": ["https://byte/obj/a_www2160-2880"],
        }))
        self.assertIn("不合规", _main_pic_3_4_issue({
            "main_pic_3_4": ["https://byte/obj/a_www2880-2880"],
        }))

    def test_brand_identity_requires_positive_id_and_matching_platform_prefix(self):
        detail = {"standard_brand_id": 663234468}
        self.assertIsNone(_brand_identity_issue(
            detail, brand_cn="红米", brand_en="Redmi", recommended_prefix="95新 Redmi/红米 ",
        ))
        self.assertIn("缺失", _brand_identity_issue(
            {"standard_brand_id": 0}, brand_cn="红米", brand_en="Redmi",
            recommended_prefix="95新 Redmi/红米 ",
        ))
        self.assertIn("不一致", _brand_identity_issue(
            detail, brand_cn="红米", brand_en="Redmi", recommended_prefix="95新 Xiaomi/小米 ",
        ))

    def test_verified_redmi_brand_replaces_wrong_platform_brand(self):
        brand_id, prefix, action = _planned_brand(
            {"standard_brand_id": 261169705, "name_prefix": "95新 Xiaomi/小米 "}, "红米",
        )
        self.assertEqual(brand_id, 663234468)
        self.assertEqual(prefix, "95新 Redmi/红米 ")
        self.assertEqual(action, "replace_with_verified")

    def test_unmapped_brand_preserves_positive_platform_brand(self):
        self.assertEqual(
            _planned_brand({"standard_brand_id": 9, "name_prefix": "9新OPPO "}, "OPPO"),
            (9, "9新OPPO ", "preserve_existing"),
        )
    def setUp(self):
        self.before = {
            "product_id_str": "90001", "status": 0, "check_status": 1, "draft_status": 1,
            "name": "OPPO Find X8s+", "name_prefix": "9新OPPO", "name_suffix": None,
            "pic": ["https://byte.example/old.jpg"], "description": "https://byte.example/old-detail.jpg",
            "product_format_new": json.dumps({"1493": [{"Value": 1}]}),
            "specs": [{"name": "颜色", "values": [{"id": 11, "name": "黑色"}]}],
            "spec_prices": [{"sku_id": 1, "stock_num": 2}], "spec_pics": [],
            "white_back_ground_pic_url": None, "main_pic_3_4": None,
        }
        self.main_urls = [f"https://byte.example/obj/ecom-shop-material/m{i}" for i in range(4)]
        self.detail_urls = [f"https://byte.example/obj/ecom-shop-material/d{i}" for i in range(14)]
        self.color_pairs = [("11", "https://byte.example/obj/ecom-shop-material/c0")]

    def test_exact_media_and_unchanged_spec_structure_pass(self):
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        self.assertEqual(verify_full_media(self.before, after, main_urls=self.main_urls,
                         color_pairs=self.color_pairs, detail_image_urls=self.detail_urls), [])

    def test_white_background_can_be_preserved_or_explicitly_restored(self):
        platform_white = "https://byte.example/obj/ecom-shop-material/white.jpg"
        self.before["white_back_ground_pic_url"] = platform_white
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        self.assertEqual(verify_full_media(
            self.before, after, main_urls=self.main_urls,
            color_pairs=self.color_pairs, detail_image_urls=self.detail_urls,
        ), [])

        recovery_before = copy.deepcopy(self.before)
        recovery_before["white_back_ground_pic_url"] = None
        self.assertEqual(verify_full_media(
            recovery_before, after, main_urls=self.main_urls,
            color_pairs=self.color_pairs, detail_image_urls=self.detail_urls,
            expected_white_background_url=platform_white,
        ), [])

    def test_partial_color_slots_keep_exact_spec_ids(self):
        self.before["specs"][0]["values"] = [
            {"id": 11, "name": "黑色"}, {"id": 12, "name": "白色"},
            {"id": 13, "name": "蓝色"},
        ]
        slots = _spec_pic_slots(self.before, {
            "11": "https://byte.example/black.jpg",
            "13": "https://byte.example/blue.jpg",
        })
        self.assertEqual(slots, [
            "https://byte.example/black.jpg", "", "https://byte.example/blue.jpg",
        ])

    def test_omitted_color_upload_requires_existing_spec_pics_unchanged(self):
        self.before["spec_pics"] = [{"spec_detail_id": 11, "pic": "old.jpg"}]
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        self.assertEqual(verify_full_media(
            self.before, after, main_urls=self.main_urls, color_pairs=None,
            detail_image_urls=self.detail_urls,
        ), [])

    def test_expected_brand_and_prefix_change_are_verified(self):
        self.before["standard_brand_id"] = 261169705
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        after["standard_brand_id"] = 663234468
        after["name_prefix"] = "95新 Redmi/红米 "
        self.assertEqual(verify_full_media(
            self.before, after, main_urls=self.main_urls,
            color_pairs=self.color_pairs, detail_image_urls=self.detail_urls,
            standard_brand_id=663234468, name_prefix="95新 Redmi/红米 ",
        ), [])

    def test_changed_sku_and_wrong_main_order_fail(self):
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls[::-1]
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        after["spec_prices"][0]["stock_num"] = 99
        issues = verify_full_media(self.before, after, main_urls=self.main_urls,
                                   color_pairs=self.color_pairs, detail_image_urls=self.detail_urls)
        self.assertTrue(any("spec_prices" in issue for issue in issues))
        self.assertTrue(any("主图/白底图" in issue for issue in issues))

    def test_model_change_only_preserves_other_properties(self):
        self.before["product_format_new"] = json.dumps({
            "1493": [{"Value": 123, "Name": "旧型号", "diy_type": 1}],
            "201": [{"Value": 99, "Name": "其他属性"}],
        })
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        after["product_format_new"] = json.dumps({
            "1493": [{"value": 488936, "name": "REDMI Turbo 4", "diy_type": 0}],
            "201": [{"value": 99, "name": "其他属性"}],
        })
        expected = {"value": 488936, "name": "REDMI Turbo 4"}
        self.assertEqual(verify_full_media(
            self.before, after, main_urls=self.main_urls, color_pairs=self.color_pairs,
            detail_image_urls=self.detail_urls, model_option=expected,
        ), [])
        wrong = copy.deepcopy(after)
        wrong_props = json.loads(wrong["product_format_new"])
        wrong_props["201"][0]["value"] = 98
        wrong["product_format_new"] = json.dumps(wrong_props)
        issues = verify_full_media(
            self.before, wrong, main_urls=self.main_urls, color_pairs=self.color_pairs,
            detail_image_urls=self.detail_urls, model_option=expected,
        )
        self.assertIn("型号之外的商品属性改变", issues)

    def test_model_and_screen_change_only_preserves_other_properties(self):
        self.before["product_format_new"] = json.dumps({
            "1493": [{"Value": 123, "Name": "旧型号", "diy_type": 1}],
            "6448": [],
            "201": [{"Value": 99, "Name": "其他属性"}],
        })
        after = copy.deepcopy(self.before)
        after["pic"] = self.main_urls
        after["description"] = "|".join(self.detail_urls)
        after["spec_pics"] = [{"spec_detail_id": 11, "pic": "ecom-shop-material/c0"}]
        after["product_format_new"] = json.dumps({
            "1493": [{"value": 488936, "name": "REDMI Turbo 4", "diy_type": 0}],
            "6448": [{"value": 346803, "name": "直屏", "diy_type": 0}],
            "201": [{"value": 99, "name": "其他属性"}],
        })
        self.assertEqual(verify_full_media(
            self.before, after, main_urls=self.main_urls,
            color_pairs=self.color_pairs, detail_image_urls=self.detail_urls,
            model_option={"value": 488936, "name": "REDMI Turbo 4"},
            screen_option={"value": 346803, "name": "直屏"},
        ), [])

    def test_post_publish_allows_only_platform_stock_sync(self):
        saved = copy.deepcopy(self.before)
        saved["product_format_new"] = json.dumps({
            "1493": [{"value": 488936, "name": "REDMI Turbo 4", "diy_type": 0}],
        })
        saved["spec_prices"][0]["stock_num"] = 0
        live = copy.deepcopy(saved)
        live["check_status"] = 3
        live["spec_prices"][0]["stock_num"] = 1
        issues, sync = _post_publish_issues(saved, live)
        self.assertEqual(issues, [])
        self.assertEqual(sync, [{"sku_id": "1", "before": 0, "after": 1}])
        live["spec_prices"][0]["stock_num"] = 5
        issues, sync = _post_publish_issues(saved, live)
        self.assertEqual(issues, [])
        self.assertEqual(sync, [{"sku_id": "1", "before": 0, "after": 5}])
        saved["spec_prices"][0]["stock_num"] = 2
        live["spec_prices"][0]["stock_num"] = 5
        issues, sync = _post_publish_issues(saved, live)
        self.assertEqual(issues, [])
        self.assertEqual(sync, [{"sku_id": "1", "before": 2, "after": 5}])
        live["spec_prices"][0]["stock_num"] = 1
        issues, _ = _post_publish_issues(saved, live)
        self.assertTrue(any("sku_fields_changed" in issue for issue in issues))
        live["spec_prices"][0]["price"] = 99
        issues, _ = _post_publish_issues(saved, live)
        self.assertTrue(any("sku_fields_changed" in issue for issue in issues))

    def test_post_publish_accepts_exact_recommended_visible_title(self):
        saved = copy.deepcopy(self.before)
        live = copy.deepcopy(saved)
        live["check_status"] = 3
        live["name"] = "9新OPPO OPPO Find X8s+"
        live["name_prefix"] = None
        issues, _ = _post_publish_issues(
            saved, live, expected_name="9新OPPO OPPO Find X8s+",
            expected_prefix="9新OPPO ",
        )
        self.assertEqual(issues, [])

    def test_live_option_resolution_requires_unique_numeric_model(self):
        class FakeGateway:
            def call(self, name, params):
                self.last = (name, params)
                return {"data": {"data": [{"property_id": 1493, "options": [
                    {"name": "REDMI Turbo 4", "value": "488936"},
                    {"name": "REDMI Turbo 4 Pro", "value": "488937"},
                ]}]}}

        gateway = FakeGateway()
        detail = {"category_detail": {"first_cid": 1, "second_cid": 1000007628}}
        self.assertEqual(_category_leaf_id(detail), "1000007628")
        self.assertEqual(resolve_official_model(gateway, detail, "Turbo 4", "红米", "Redmi"),
                         {"name": "REDMI Turbo 4", "value": 488936})
        self.assertEqual(gateway.last[1], {"category_leaf_id": 1000007628})
        with self.assertRaises(FullMediaCanaryError):
            resolve_official_model(gateway, detail, "Turbo 5", "红米", "Redmi")

    def test_browser_folded_prefix_is_not_duplicated_for_gallery_match(self):
        self.assertEqual(_source_title({"name_prefix": "95新 Redmi/红米 ",
                                        "name": "95新 Redmi/红米 REDMI Turbo 4 二手手机"}),
                         "95新 Redmi/红米 REDMI Turbo 4 二手手机")
        self.assertEqual(_source_title({"name_prefix": "95新 Redmi/红米 ",
                                        "name": "REDMI Turbo 4 二手手机"}),
                         "95新 REDMI Turbo 4 二手手机")
        self.assertEqual(_source_title({"name_prefix": "99新 Honor/荣耀 ",
                                        "name": "99新 honor/荣耀 荣耀 Magic8 Pro 二手手机"}),
                         "99新 honor/荣耀 荣耀 Magic8 Pro 二手手机")


if __name__ == "__main__":
    unittest.main()
