import json
import unittest
from io import BytesIO
from urllib.error import HTTPError

from integrations.feishu_drive.client import FeishuDriveClient, FeishuDriveConfig, FeishuDriveError


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if request.full_url.endswith("tenant_access_token/internal"):
            return Response(json.dumps({"code": 0, "tenant_access_token": "tenant"}).encode())
        if "/drive/v1/files?" in request.full_url:
            return Response(json.dumps({"code": 0, "data": {"files": [{
                "token": "file-1", "name": "主图.jpg", "mime_type": "image/jpeg",
                "version": "v1", "size": 12, "type": "file",
            }], "has_more": False}}).encode())
        if request.full_url.endswith("/drive/v1/files/file-1/download"):
            return Response(b"image bytes")
        raise AssertionError(request.full_url)


class PaginatedOpener:
    def __init__(self):
        self.calls = 0

    def __call__(self, request, timeout):
        del timeout
        if request.full_url.endswith("tenant_access_token/internal"):
            return Response(json.dumps({"code": 0, "tenant_access_token": "tenant"}).encode())
        self.calls += 1
        if self.calls == 1:
            return Response(json.dumps({"code": 0, "data": {
                "files": [{"token": "one", "name": "one", "type": "folder", "version": "v1"}],
                "has_more": True, "next_page_token": "page-2",
            }}).encode())
        return Response(json.dumps({"code": 0, "data": {
            "files": [{"token": "two", "name": "two", "type": "folder", "version": "v1"}],
            "has_more": False,
        }}).encode())


class FeishuDriveClientTests(unittest.TestCase):
    def test_config_requires_explicit_cloud_credentials(self):
        with self.assertRaisesRegex(FeishuDriveError, "not configured"):
            FeishuDriveConfig.from_mapping({})

    def test_read_only_list_and_download(self):
        config = FeishuDriveConfig.from_mapping({
            "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
            "FEISHU_MATERIAL_ROOT_FOLDER_TOKEN": "folder",
        })
        opener = Opener()
        client = FeishuDriveClient(config, base_url="https://unit.test/open-apis", opener=opener)
        files = list(client.list_folder())
        self.assertEqual(files[0].file_token, "file-1")
        self.assertEqual(client.download("file-1"), b"image bytes")
        self.assertTrue(all(request.get_method() in {"GET", "POST"} for request in opener.requests))
        self.assertFalse(any("delete" in request.full_url or "move" in request.full_url for request in opener.requests))

    def test_http_permission_error_is_reported_without_request_secret(self):
        config = FeishuDriveConfig.from_mapping({
            "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
            "FEISHU_MATERIAL_ROOT_FOLDER_TOKEN": "folder",
        })

        def forbidden(_request, timeout):
            del timeout
            raise HTTPError(
                "https://unit.test/open-apis/drive/v1/files", 403, "Forbidden", {},
                BytesIO(b'{"code":1061004,"msg":"bot lacks permission for requested resource"}'),
            )

        client = FeishuDriveClient(config, opener=forbidden)
        with self.assertRaisesRegex(FeishuDriveError, "HTTP 403 code=1061004"):
            list(client.list_entries())

    def test_list_entries_accepts_current_next_page_token_field(self):
        config = FeishuDriveConfig.from_mapping({
            "FEISHU_APP_ID": "app", "FEISHU_APP_SECRET": "secret",
            "FEISHU_MATERIAL_ROOT_FOLDER_TOKEN": "folder",
        })
        client = FeishuDriveClient(config, opener=PaginatedOpener())
        self.assertEqual([entry.name for entry in client.list_entries()], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
