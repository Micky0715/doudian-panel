import json
import urllib.parse
import unittest

from gaia_api import GaiaAPIError, GaiaClient, GaiaConfig, build_model_catalogue


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    def read(self): return self.payload
    def __enter__(self): return self
    def __exit__(self, *args): return False


def config():
    return GaiaConfig("https://example.test", "132457-测试", "stock-secret", "price-secret")


class GaiaClientTests(unittest.TestCase):
    def test_form_protocol_and_gateway_unwrap(self):
        captured = {}
        def opener(request, timeout):
            captured["request"] = request
            return FakeResponse({"code": 200, "success": True, "backendResponse": {"status": 200, "content": []}})
        list(GaiaClient(config(), opener=opener).iter_stock(max_pages=1))
        request = captured["request"]
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://example.test/api-gateway/api")
        self.assertEqual(form["apiName"], ["sale.bigdata.itemgoods.stock.data"])
        self.assertEqual(form["user"], ["132457-测试"])
        params = json.loads(form["jsonParams"][0])
        self.assertEqual(params["params"], {"cate3Name": "手机"})
        self.assertEqual(params["pageNumber"], 0)

    def test_pages_until_empty_even_if_last_is_true(self):
        calls = []
        def opener(request, timeout):
            page = json.loads(urllib.parse.parse_qs(request.data.decode())["jsonParams"][0])["pageNumber"]
            calls.append(page)
            rows = [{"型号名称": f"型号{page}"}] if page < 2 else []
            backend = {"status": 200, "last": True, "content": rows}
            return FakeResponse({"code": 200, "success": True, "backendResponse": json.dumps(backend, ensure_ascii=False)})
        rows = list(GaiaClient(config(), opener=opener).iter_stock())
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(len(rows), 2)

    def test_gateway_error_does_not_expose_token(self):
        def opener(request, timeout):
            return FakeResponse({"code": 10005, "message": "授权码与接口不匹配", "success": False})
        with self.assertRaises(GaiaAPIError) as caught:
            list(GaiaClient(config(), opener=opener).iter_stock(max_pages=1))
        self.assertNotIn("stock-secret", str(caught.exception))

    def test_price_uses_string_subs(self):
        captured = {}
        def opener(request, timeout):
            captured.update(json.loads(urllib.parse.parse_qs(request.data.decode())["jsonParams"][0]))
            return FakeResponse({"code": 200, "success": True, "backendResponse": {"status": 200, "content": []}})
        list(GaiaClient(config(), opener=opener).iter_price_detail(
            start_date="2026-08-18", end_date="2026-08-18", channel="京东严选", max_pages=1))
        self.assertEqual(captured["stringSubs"]["dataType"], "出库")
        self.assertEqual(captured["stringSubs"]["yxChannelName"], "京东严选")


class CatalogueTests(unittest.TestCase):
    def test_extracts_and_deduplicates_stock_models(self):
        rows = [
            {"品牌名称": "Apple", "型号名称": "苹果 iPhone XR"},
            {"品牌名称": "Apple", "型号名称": "苹果 iPhone XR"},
            {"品牌名称": "小米", "型号名称": "Redmi K80"},
            {"品牌名称": "Apple", "型号名称": ""},
        ]
        self.assertEqual(len(build_model_catalogue(rows, "stock")), 2)

    def test_extracts_price_model_fields(self):
        result = build_model_catalogue([{"品牌": "华为（HUAWEI）", "型号": "华为 FreeBuds Pro 4"}], "price")
        self.assertEqual(result[0]["model"], "华为 FreeBuds Pro 4")


if __name__ == "__main__":
    unittest.main()
