"""Small, per-shop cache for audited DouDian material URLs.

The product API accepts only a DouDian material ``byte_url``.  Uploading the
same local bytes before every product is slow and creates unnecessary copies,
so cache entries are keyed by the material folder and the content SHA-256.
An entry is never trusted blindly: the gateway re-reads its material detail
before reuse and only keeps it when the platform still returns a URL.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping


class MaterialUrlCache:
    """File-backed cache scoped to exactly one shop.

    It stores material metadata only; short-lived upload authorisations and
    local absolute source paths are deliberately never persisted.
    """

    SCHEMA_VERSION = 1

    def __init__(self, root: str | Path, shop_id: str | int):
        self.shop_id = str(shop_id).strip()
        self.path = Path(root).expanduser().resolve() / self.shop_id / "materials.json"

    @staticmethod
    def _key(folder_id: str, sha256: str) -> str:
        return f"{folder_id}:{sha256}"

    def get(self, folder_id: str, sha256: str) -> dict[str, Any] | None:
        value = self._load().get("items", {}).get(self._key(folder_id, sha256))
        return dict(value) if isinstance(value, Mapping) else None

    def put(self, *, folder_id: str, sha256: str, material_id: str,
            byte_url: str, size_bytes: int, name: str) -> None:
        value = self._load()
        items = value.setdefault("items", {})
        items[self._key(folder_id, sha256)] = {
            "folder_id": str(folder_id), "sha256": str(sha256),
            "material_id": str(material_id), "byte_url": str(byte_url),
            "size_bytes": int(size_bytes), "name": Path(name).name,
            "verified_at": int(time.time()),
        }
        self._save(value)

    def refresh(self, *, folder_id: str, sha256: str, byte_url: str) -> None:
        value = self._load()
        item = value.setdefault("items", {}).get(self._key(folder_id, sha256))
        if not isinstance(item, dict):
            return
        item["byte_url"] = str(byte_url)
        item["verified_at"] = int(time.time())
        self._save(value)

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": self.SCHEMA_VERSION, "shop_id": self.shop_id, "items": {}}
        if not isinstance(value, dict) or str(value.get("shop_id") or "") != self.shop_id:
            return {"schema_version": self.SCHEMA_VERSION, "shop_id": self.shop_id, "items": {}}
        if not isinstance(value.get("items"), dict):
            value["items"] = {}
        return value

    def _save(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
