import hashlib
import hmac
import io
import json
import unittest
from unittest.mock import patch

from doudian_api import (
    DoudianAPIError, DoudianClient, DoudianConfig, DoudianConfigError,
    calculate_sign, create_self_use_access_token, find_exact_option, find_property, normalize_model_name,
    resolve_model_option, serialize_params,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def read(self):
        return self.payload
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def config():
    return DoudianConfig.from_mapping({
        "DOUDIAN_APP_KEY": "key-sensitive",
        "DOUDIAN_APP_SECRET": "secret-sensitive",
        "DOUDIAN_SHOP_TOKENS_JSON": '{"7970406":"token-sensitive"}',
        "DOUDIAN_SHOP_REFRESH_TOKENS_JSON": '{"7970406":"refresh-sensitive"}',
    })


class ConfigTests(unittest.TestCase):
    def test_repr_redacts_all_credentials(self):
        rendered = repr(config())
        for secret in ("key-sensitive", "secret-sensitive", "token-sensitive", "refresh-sensitive"):
            self.assertNotIn(secret, rendered)

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(DoudianConfigError):
            DoudianConfig.from_mapping({
                "DOUDIAN_APP_KEY": "k", "DOUDIAN_APP_SECRET": "s",
                "DOUDIAN_SHOP_TOKENS_JSON": "invalid",
            })


class SigningTests(unittest.TestCase):
    def test_nested_keys_are_sorted_and_chinese_is_preserved(self):
        self.assertEqual(
            serialize_params({"z": 2, "a": {"b": "型号", "a": 1}}),
            '{"a":{"a":1,"b":"型号"},"z":2}',
        )

    def test_hmac_signature_uses_official_pattern(self):
        body = '{"cid":0}'
        plain = f"secretapp_keykeymethodshop.getShopCategoryparam_json{body}timestamp1720000000v2secret"
        expected = hmac.new(b"secret", plain.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(
            calculate_sign("key", "secret", "shop.getShopCategory", 1720000000, body), expected
        )


class ClientTests(unittest.TestCase):
    @patch("doudian_api.time.time", return_value=1720000000)
    def test_read_request_is_post_json(self, _):
        captured = {}
        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 10000, "data": []})
        self.assertEqual(DoudianClient(config(), "7970406", opener=opener).get_shop_categories(), [])
        request = captured["request"]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.data, b'{"cid":0}')
        self.assertNotIn("param_json", request.full_url)
        self.assertNotIn("secret-sensitive", request.full_url)

    def test_api_error_is_structured_without_credentials(self):
        def opener(request, timeout):
            return FakeResponse({"code": 40003, "sub_code": "invalid", "sub_msg": "cid required"})
        with self.assertRaises(DoudianAPIError) as caught:
            DoudianClient(config(), "7970406", opener=opener).get_shop_categories()
        self.assertEqual(caught.exception.code, 40003)
        self.assertNotIn("sensitive", str(caught.exception))

    def test_product_list_uses_v2_endpoint(self):
        captured = {}
        def opener(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse({"code": 10000, "data": {"data": [], "total": 0}})
        result = DoudianClient(config(), "7970406", opener=opener).list_products()
        self.assertEqual(result["total"], 0)
        self.assertIn("/product/listV2", captured["url"])

    def test_optional_gateway_headers_are_added_without_changing_body(self):
        captured = {}
        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 10000, "data": {}})
        DoudianClient(config(), "7970406", opener=opener).call(
            "open.binaryupload", {"file_content": "safe"},
            headers={"from": "sdk", "sdk-type": "python"},
        )
        request = captured["request"]
        self.assertEqual(request.data, b'{"file_content":"safe"}')
        self.assertEqual(request.get_header("From"), "sdk")
        self.assertEqual(request.get_header("Sdk-type"), "python")

    def test_self_use_token_create_has_no_access_token_dependency(self):
        captured = {}
        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 10000, "data": {
                "access_token": "next-access", "refresh_token": "next-refresh",
                "shop_id": "7970406", "expires_in": 604800,
            }})
        result = create_self_use_access_token(
            "key", "secret", "7970406", opener=opener,
        )
        request = captured["request"]
        self.assertEqual(result["shop_id"], "7970406")
        self.assertIn("/token/create?", request.full_url)
        self.assertNotIn("access_token=", request.full_url)
        self.assertEqual(json.loads(request.data), {
            "code": "", "grant_type": "authorization_self", "shop_id": "7970406",
        })


class ModelTests(unittest.TestCase):
    def setUp(self):
        # getCatePropertyV2 uses property_id/property_name on the wire.
        self.properties = [{"property_id": 1493, "property_name": "型号", "options": [
            {"value": "454966", "name": "iPhone 16"},
            {"value": "454970", "name": "iPhone 16 Pro"},
            {"value": "454971", "name": "iPhone 16 Pro Max"},
        ]}]

    def test_exact_model_is_selected(self):
        model = find_property(self.properties, 1493)
        self.assertEqual(find_exact_option(model, "iphone 16 pro")["value"], "454970")

    def test_legacy_id_shape_is_also_supported(self):
        self.assertIsNotNone(find_property([{"id": 1493, "name": "型号"}], 1493))

    def test_variants_are_not_confused(self):
        model = find_property(self.properties, 1493)
        self.assertIsNone(find_exact_option(model, "iPhone 16P"))
        self.assertEqual(find_exact_option(model, "iPhone 16 Pro Max")["value"], "454971")

    def test_safe_normalization_handles_platform_formatting(self):
        prop = {"options": [{"value": "1", "name": "HUAWEI nova 14 (活力版)"}]}
        match = resolve_model_option(prop, "nova14 活力版", brand_names=("HUAWEI", "华为"))
        self.assertEqual(match["value"], "1")

    def test_safe_normalization_preserves_sku_suffixes(self):
        prop = {"options": [
            {"value": "base", "name": "iPhone 16"},
            {"value": "pro", "name": "iPhone 16 Pro"},
            {"value": "plus", "name": "Mate 70 Pro+"},
        ]}
        self.assertEqual(resolve_model_option(prop, "iphone16")["value"], "base")
        self.assertEqual(resolve_model_option(prop, "Mate70Pro+")["value"], "plus")
        self.assertIsNone(resolve_model_option(prop, "iPhone 16 Pro Max"))
        self.assertNotEqual(normalize_model_name("Mate 70 Pro+"), normalize_model_name("Mate 70 Pro"))

    def test_ambiguous_normalized_names_are_rejected(self):
        prop = {"options": [
            {"value": "1", "name": "Pura 90 Pro"},
            {"value": "2", "name": "Pura90 Pro"},
        ]}
        self.assertEqual(resolve_model_option(prop, "Pura  90 Pro")["value"], "1")

    def test_exact_platform_spelling_breaks_normalized_alias_tie(self):
        prop = {"options": [
            {"value": "212545", "name": "iPhone 14 Pro"},
            {"value": "276305", "name": "iPhone 14pro"},
        ]}
        self.assertEqual(resolve_model_option(prop, "iPhone 14 Pro")["value"], "212545")

    def test_official_case_and_spaces_break_real_platform_duplicate_ties(self):
        x60 = {"options": [
            {"value": "265979", "name": "X60 PRO"},
            {"value": "47796", "name": "X60 Pro"},
            {"value": "272427", "name": "X60PRO"},
            {"value": "229255", "name": "X60Pro"},
        ]}
        nova = {"options": [
            {"value": "534793", "name": "nova 14 Ultra"},
            {"value": "541830", "name": "nova 14 ultra"},
        ]}
        self.assertEqual(resolve_model_option(x60, "X60 Pro")["value"], "47796")
        self.assertEqual(resolve_model_option(nova, "nova 14 Ultra")["value"], "534793")


if __name__ == "__main__":
    unittest.main()
