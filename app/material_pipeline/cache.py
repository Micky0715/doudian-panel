"""Small local metadata cache; the cloud drive remains the source of truth."""

from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path
from typing import Any, Mapping


class MaterialMetadataCache:
    """Cache only immutable source-to-DouDian material evidence, never tokens."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def key(shop_id: str, folder_id: str, file_token: str, revision: str, sha256: str) -> str:
        """Scope cache evidence to one DouDian shop and material folder."""
        source = "\x1f".join((
            str(shop_id).strip(), str(folder_id).strip(), file_token.strip(),
            revision.strip(), sha256.strip().lower(),
        ))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    def get(self, shop_id: str, folder_id: str, file_token: str, revision: str, sha256: str) -> Mapping[str, Any] | None:
        path = self.root / (self.key(shop_id, folder_id, file_token, revision, sha256) + ".json")
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def put(self, record: Mapping[str, Any]) -> Path:
        required = ("shop_id", "folder_id", "file_token", "revision", "sha256", "material_id", "byte_url")
        missing = [key for key in required if not str(record.get(key) or "").strip()]
        if missing:
            raise ValueError("material cache record missing: " + ", ".join(missing))
        self.root.mkdir(parents=True, exist_ok=True)
        filename = self.key(
            record["shop_id"], record["folder_id"], record["file_token"],
            record["revision"], record["sha256"],
        ) + ".json"
        target = self.root / filename
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(record), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        return target
