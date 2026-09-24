import tempfile
import unittest
import json
from pathlib import Path
from urllib.request import Request

from integrations.doudian.materials.gateway import (
    DoudianMaterialGateway, DoudianMaterialProtocolError, MAX_SYNC_IMAGE_BYTES,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params, **kwargs):
        self.calls.append((method, params, kwargs))
        return {
            "open.materialToken": {"data": {"auth_query": "https://imagex.test/apply"}},
            "material.uploadImageSync": {"data": {"material_id": "material-1"}},
            "material.queryMaterialDetail": {"data": {"material_info": {"byte_url": "https://byte.example/image.jpg"}}},
        }[method]


class FakeHTTPResponse:
    def __init__(self, value):
        self._payload = json.dumps(value).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeHTTP:
    def __init__(self):
        self.requests = []

    def __call__(self, request, **kwargs):
        self.requests.append((request, kwargs))
        if isinstance(request, str):
            return FakeHTTPResponse({
                "Result": {"UploadAddress": {
                    "UploadHosts": ["tos.example"],
                    "StoreInfos": [{"StoreUri": "tos://upload/file", "Auth": "temporary"}],
                }},
            })
        self._request = request
        return FakeHTTPResponse({"code": 2000, "message": "Success"})


class DoudianMaterialGatewayTests(unittest.TestCase):
    def make_env(self, root):
        path = Path(root) / "env.local"
        path.write_text(
            "DOUDIAN_APP_KEY=k\nDOUDIAN_APP_SECRET=s\nDOUDIAN_APP_TYPE=self_use\n"
            'DOUDIAN_SHOP_TOKENS_JSON={"10001":"token"}\n', encoding="utf-8"
        )
        return path

    def test_upload_is_material_token_then_explicit_material_registration(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakeClient()
            http = FakeHTTP()
            gateway = DoudianMaterialGateway(
                self.make_env(root), "10001", client_factory=lambda *_: fake,
                urlopen_factory=http,
            )
            result = gateway.upload_image(b"img", "主图.jpg", folder_id="123")
            self.assertEqual(result.material_id, "material-1")
            self.assertEqual(result.byte_url, "https://byte.example/image.jpg")
            self.assertEqual([call[0] for call in fake.calls], [
                "open.materialToken", "material.uploadImageSync", "material.queryMaterialDetail",
            ])
            self.assertNotIn(b"img", str(fake.calls).encode())
            self.assertEqual(fake.calls[0][1], {"upload_num": 1, "file_extension": ".jpg"})
            self.assertEqual(fake.calls[1][1]["folder_id"], "123")
            self.assertEqual(fake.calls[1][1]["file_uri"], "tos://upload/file")
            binary_request = http.requests[1][0]
            self.assertIsInstance(binary_request, Request)
            self.assertEqual(binary_request.get_method(), "POST")
            self.assertEqual(binary_request.get_header("Content-crc32"), "bbc2c8ac")
            self.assertEqual(binary_request.data, b"img")

    def test_rejects_oversized_or_invalid_images_before_network(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakeClient()
            gateway = DoudianMaterialGateway(self.make_env(root), "10001", client_factory=lambda *_: fake)
            with self.assertRaisesRegex(DoudianMaterialProtocolError, "3MB"):
                gateway.upload_image(b"x" * (MAX_SYNC_IMAGE_BYTES + 1), "main.jpg", folder_id="123")
            with self.assertRaisesRegex(DoudianMaterialProtocolError, "extension"):
                gateway.upload_image(b"x", "main.txt", folder_id="123")
            with self.assertRaisesRegex(DoudianMaterialProtocolError, "folder_id"):
                gateway.upload_image(b"x", "main.jpg")
            self.assertEqual(fake.calls, [])

    def test_reuses_only_a_revalidated_same_shop_folder_and_content_entry(self):
        with tempfile.TemporaryDirectory() as root:
            fake = FakeClient()
            http = FakeHTTP()
            gateway = DoudianMaterialGateway(
                self.make_env(root), "10001", client_factory=lambda *_: fake,
                urlopen_factory=http, cache_root=Path(root) / "cache",
            )
            first = gateway.upload_image(b"img", "main.jpg", folder_id="123")
            calls_after_first = len(fake.calls)
            second = gateway.upload_image(b"img", "different-name.jpg", folder_id="123")
            self.assertEqual(first, second)
            self.assertEqual([call[0] for call in fake.calls[calls_after_first:]], [
                "material.queryMaterialDetail",
            ])
            self.assertEqual(len(http.requests), 2)  # No second ImageX request or byte upload.
            gateway.upload_image(b"img", "main.jpg", folder_id="456")
            self.assertIn("open.materialToken", [call[0] for call in fake.calls[calls_after_first + 1:]])


if __name__ == "__main__":
    unittest.main()
