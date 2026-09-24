"""Upload image bytes to DouDian material centre and return a platform URL.

The product API cannot consume a local path.  This gateway therefore uses the
material-token route issued by DouDian for the current shop, uploads bytes into
the temporary ImageX slot, then registers the resulting ``file_uri`` in the
DouDian material centre.  Only an audited material ``byte_url`` may cross the
product-write boundary.

This module never accepts a local path or a Feishu link as a product URL and it
never deletes material assets.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import mimetypes
from pathlib import Path
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zlib

from doudian_api import DoudianClient
from integrations.doudian.token_store import EnvTokenStore
from .cache import MaterialUrlCache


MAX_SYNC_IMAGE_BYTES = 3 * 1024 * 1024

MATERIAL_DETAIL_ATTEMPTS = 6
MATERIAL_DETAIL_WAIT_SECONDS = 2.0
MATERIAL_HTTP_TIMEOUT_SECONDS = 30


class DoudianMaterialProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadedMaterial:
    material_id: str
    byte_url: str
    sha256: str
    size_bytes: int


def _data(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("data") or {}
    if not isinstance(value, dict):
        raise DoudianMaterialProtocolError("DouDian material API response data is not an object")
    return value


def _required(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    raise DoudianMaterialProtocolError("DouDian material API missing " + " or ".join(keys))


class DoudianMaterialGateway:
    """Turn image bytes into an audited DouDian ``byte_url``.

    The explicit ``folder_id`` is mandatory.  Choosing a material folder by
    position or a fuzzy name would recreate the same target-guessing bug that
    the rest of the product pipeline intentionally avoids.
    """

    def __init__(self, env_path: str | Path, shop_id: str | int, *,
                 client_factory: Callable[..., Any] | None = None,
                 urlopen_factory: Callable[..., Any] | None = None,
                 cache_root: str | Path | None = None):
        self.shop_id = str(shop_id).strip()
        if not self.shop_id.isdigit():
            raise ValueError("shop id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)
        self._urlopen = urlopen_factory or urlopen
        self._cache = MaterialUrlCache(cache_root, self.shop_id) if cache_root else None

    def _call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._client.call(method, params)

    @staticmethod
    def _response_json(response: Any, *, stage: str) -> Mapping[str, Any]:
        try:
            value = json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DoudianMaterialProtocolError(
                f"DouDian material {stage} response is not JSON"
            ) from exc
        if not isinstance(value, dict):
            raise DoudianMaterialProtocolError(
                f"DouDian material {stage} response is not an object"
            )
        return value

    def _request_imagex_slot(self, suffix: str) -> tuple[str, str, str]:
        """Return a one-time ImageX upload URL, authorization and file URI.

        ``auth_query`` and the returned authorization are short-lived secrets;
        neither is logged, persisted, or returned to callers.
        """
        token = self._call("open.materialToken", {"upload_num": 1, "file_extension": suffix})
        auth_query = _required(_data(token), "auth_query")
        try:
            with self._urlopen(auth_query, timeout=MATERIAL_HTTP_TIMEOUT_SECONDS) as response:
                applied = self._response_json(response, stage="ImageX slot request")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DoudianMaterialProtocolError(
                f"DouDian material ImageX slot request failed: {type(exc).__name__}"
            ) from exc
        try:
            address = applied["Result"]["UploadAddress"]
            hosts = address["UploadHosts"]
            slots = address["StoreInfos"]
            host = str(hosts[0]).strip()
            slot = slots[0]
        except (KeyError, IndexError, TypeError) as exc:
            raise DoudianMaterialProtocolError(
                "DouDian material ImageX slot response has no usable upload address"
            ) from exc
        if not isinstance(slot, dict):
            raise DoudianMaterialProtocolError("DouDian material ImageX slot is not an object")
        file_uri = _required(slot, "StoreUri")
        authorization = _required(slot, "Auth")
        if not host:
            raise DoudianMaterialProtocolError("DouDian material ImageX slot has no upload host")
        return host, file_uri, authorization

    def _upload_binary_to_imagex(self, image_bytes: bytes, clean_name: str) -> str:
        suffix = Path(clean_name).suffix.casefold()
        host, file_uri, authorization = self._request_imagex_slot(suffix)
        upload_url = f"https://{host}/upload/v1/{file_uri}"
        content_type = mimetypes.guess_type(clean_name)[0] or "application/octet-stream"
        request = Request(
            upload_url,
            data=image_bytes,
            headers={
                "Authorization": authorization,
                "Content-CRC32": f"{zlib.crc32(image_bytes) & 0xffffffff:08x}",
                "Content-Type": content_type,
            },
            method="POST",
        )
        try:
            with self._urlopen(request, timeout=MATERIAL_HTTP_TIMEOUT_SECONDS) as response:
                uploaded = self._response_json(response, stage="ImageX byte upload")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise DoudianMaterialProtocolError(
                f"DouDian material ImageX byte upload failed: {type(exc).__name__}"
            ) from exc
        if int(uploaded.get("code") or 0) != 2000:
            message = str(uploaded.get("message") or uploaded.get("msg") or "unknown error")
            raise DoudianMaterialProtocolError(
                f"DouDian material ImageX byte upload was rejected: {message[:200]}"
            )
        return file_uri

    def _material_byte_url(self, material_id: str) -> str:
        """Read the platform's current audited URL for one material ID."""
        detail = self._call("material.queryMaterialDetail", {"material_id": material_id})
        detail_data = _data(detail)
        material_info = detail_data.get("material_info")
        if not isinstance(material_info, dict):
            material_info = detail_data
        return str(material_info.get("byte_url") or "").strip()

    def get_material_byte_url(self, material_id: str) -> str:
        """Re-read the current audited URL for a cached material evidence ID."""
        clean_id = str(material_id or "").strip()
        if not clean_id:
            raise DoudianMaterialProtocolError("material_id is required")
        return self._material_byte_url(clean_id)

    def upload_image(self, image_bytes: bytes, name: str, *, folder_id: str | None = None) -> UploadedMaterial:
        if not isinstance(image_bytes, bytes) or not image_bytes:
            raise DoudianMaterialProtocolError("image upload requires non-empty bytes")
        if len(image_bytes) > MAX_SYNC_IMAGE_BYTES:
            raise DoudianMaterialProtocolError("image exceeds DouDian synchronous 3MB limit")
        clean_name = Path(name).name.strip()
        if not clean_name or Path(clean_name).suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise DoudianMaterialProtocolError("image name must have a supported extension")
        clean_folder_id = str(folder_id or "").strip()
        if not clean_folder_id:
            raise DoudianMaterialProtocolError("material folder_id is required")
        digest = hashlib.sha256(image_bytes).hexdigest()
        if self._cache:
            cached = self._cache.get(clean_folder_id, digest)
            if cached:
                material_id = str(cached.get("material_id") or "").strip()
                if material_id:
                    # A cache hit still performs one inexpensive platform
                    # read.  If the material was deleted or not audited any
                    # more, it is not reused as product evidence.
                    cached_url = self._material_byte_url(material_id)
                    if cached_url:
                        self._cache.refresh(
                            folder_id=clean_folder_id, sha256=digest, byte_url=cached_url,
                        )
                        return UploadedMaterial(
                            material_id=material_id, byte_url=cached_url,
                            sha256=digest, size_bytes=len(image_bytes),
                        )
        file_uri = self._upload_binary_to_imagex(image_bytes, clean_name)
        params: dict[str, Any] = {
            "file_uri": file_uri,
            "folder_id": clean_folder_id,
            "material_name": clean_name,
            "need_distinct": True,
        }
        uploaded = self._call("material.uploadImageSync", params)
        material_id = _required(_data(uploaded), "material_id")
        # Images are asynchronously audited.  A material URL can be used only
        # after the detail endpoint returns ``byte_url``.  Do not write a
        # product if the audit evidence is absent.
        byte_url = ""
        for attempt in range(MATERIAL_DETAIL_ATTEMPTS):
            byte_url = self._material_byte_url(material_id)
            if byte_url:
                break
            if attempt + 1 < MATERIAL_DETAIL_ATTEMPTS:
                time.sleep(MATERIAL_DETAIL_WAIT_SECONDS)
        if not byte_url:
            raise DoudianMaterialProtocolError(
                f"DouDian material {material_id} has no audited byte_url yet"
            )
        result = UploadedMaterial(
            material_id=material_id,
            byte_url=byte_url,
            sha256=digest,
            size_bytes=len(image_bytes),
        )
        if self._cache:
            self._cache.put(
                folder_id=clean_folder_id, sha256=digest, material_id=material_id,
                byte_url=byte_url, size_bytes=len(image_bytes), name=clean_name,
            )
        return result
