"""Minimal read-only Feishu Drive client for product image ingestion.

The connector deliberately exposes only: list a configured folder and download
an already-listed file.  It has no create, move, delete, rename, or sync API.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Iterator, Mapping
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


class FeishuDriveError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class FeishuDriveConfig:
    app_id: str
    app_secret: str
    material_root_folder_token: str

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "FeishuDriveConfig":
        keys = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_MATERIAL_ROOT_FOLDER_TOKEN")
        missing = [key for key in keys if not str(values.get(key) or "").strip()]
        if missing:
            raise FeishuDriveError("Feishu material connector is not configured: " + ", ".join(missing))
        return cls(*(str(values[key]).strip() for key in keys))


@dataclass(frozen=True)
class RemoteDriveEntry:
    token: str
    name: str
    entry_type: str
    mime_type: str
    revision: str
    size_bytes: int

    @classmethod
    def from_api(cls, value: Mapping[str, Any]) -> "RemoteDriveEntry":
        file_token = str(value.get("token") or value.get("file_token") or "").strip()
        name = str(value.get("name") or "").strip()
        entry_type = str(value.get("type") or "file").strip().casefold()
        mime_type = str(value.get("mime_type") or value.get("type") or "").strip()
        revision = str(value.get("version") or value.get("modified_time") or "").strip()
        raw_size = value.get("size") or value.get("size_bytes") or 0
        try:
            size_bytes = int(raw_size)
        except (TypeError, ValueError):
            size_bytes = 0
        if not file_token or not name:
            raise FeishuDriveError("Feishu folder entry has no stable token or name")
        return cls(file_token, name, entry_type, mime_type, revision, size_bytes)


@dataclass(frozen=True)
class RemoteDriveFile:
    file_token: str
    name: str
    mime_type: str
    revision: str
    size_bytes: int

    @classmethod
    def from_entry(cls, value: RemoteDriveEntry) -> "RemoteDriveFile":
        if value.entry_type != "file":
            raise FeishuDriveError("expected a Feishu file entry")
        if not value.revision:
            raise FeishuDriveError("Feishu file metadata has no stable revision")
        return cls(value.token, value.name, value.mime_type, value.revision, value.size_bytes)


class FeishuDriveClient:
    """Read-only Feishu Drive adapter, with injectable opener for tests."""

    def __init__(self, config: FeishuDriveConfig, *, base_url: str = FEISHU_API_BASE,
                 timeout: float = 30.0, opener: Callable[..., Any] | None = None):
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen
        self._tenant_access_token: str | None = None

    def _json(self, request: urllib.request.Request) -> Mapping[str, Any]:
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            # Feishu includes the actionable code/message in the JSON body.
            # Preserve that diagnosis but never include request URLs or tokens.
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                payload = {}
            message = str(payload.get("msg") or payload.get("message") or "forbidden") if isinstance(payload, dict) else "forbidden"
            code = payload.get("code") if isinstance(payload, dict) else None
            suffix = (" code=" + str(code)) if code not in (None, "") else ""
            raise FeishuDriveError("Feishu API HTTP %d%s: %s" % (exc.code, suffix, message)) from exc
        except URLError as exc:
            raise FeishuDriveError("Feishu API network failure: %s" % type(exc.reason).__name__) from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FeishuDriveError("Feishu returned invalid JSON") from exc
        if not isinstance(value, dict) or value.get("code", 0) != 0:
            message = value.get("msg") if isinstance(value, dict) else "invalid response"
            raise FeishuDriveError("Feishu API rejected request: " + str(message))
        return value

    def tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        request = urllib.request.Request(
            self.base_url + "/auth/v3/tenant_access_token/internal",
            data=json.dumps({"app_id": self.config.app_id, "app_secret": self.config.app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"}, method="POST",
        )
        value = self._json(request)
        token = str(value.get("tenant_access_token") or "").strip()
        if not token:
            raise FeishuDriveError("Feishu token response has no tenant_access_token")
        self._tenant_access_token = token
        return token

    def list_entries(self, folder_token: str | None = None, *, page_size: int = 200) -> Iterator[RemoteDriveEntry]:
        target = str(folder_token or self.config.material_root_folder_token).strip()
        if not target:
            raise FeishuDriveError("folder token is required")
        page_token = ""
        while True:
            query = {"folder_token": target, "page_size": str(min(max(page_size, 1), 200))}
            if page_token:
                query["page_token"] = page_token
            request = urllib.request.Request(
                self.base_url + "/drive/v1/files?" + urllib.parse.urlencode(query),
                headers={"Authorization": "Bearer " + self.tenant_access_token()}, method="GET",
            )
            payload = self._json(request).get("data") or {}
            files = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(files, list):
                raise FeishuDriveError("Feishu folder response has no files list")
            for value in files:
                if isinstance(value, dict):
                    yield RemoteDriveEntry.from_api(value)
            if not isinstance(payload, dict) or not payload.get("has_more"):
                break
            # Drive has used both field names across API response versions.
            # ``next_page_token`` is the current response shape; accepting the
            # legacy spelling keeps the read-only client compatible.
            page_token = str(
                payload.get("next_page_token") or payload.get("page_token") or ""
            ).strip()
            if not page_token:
                raise FeishuDriveError("Feishu pagination has_more without page_token")

    def list_folder(self, folder_token: str | None = None, *, page_size: int = 200) -> Iterator[RemoteDriveFile]:
        for entry in self.list_entries(folder_token, page_size=page_size):
            if entry.entry_type == "file":
                yield RemoteDriveFile.from_entry(entry)

    def download(self, file_token: str) -> bytes:
        token = str(file_token).strip()
        if not token:
            raise FeishuDriveError("file token is required")
        request = urllib.request.Request(
            self.base_url + "/drive/v1/files/" + urllib.parse.quote(token) + "/download",
            headers={"Authorization": "Bearer " + self.tenant_access_token()}, method="GET",
        )
        with self._opener(request, timeout=self.timeout) as response:
            data = response.read()
        if not data:
            raise FeishuDriveError("Feishu returned an empty file")
        return data
