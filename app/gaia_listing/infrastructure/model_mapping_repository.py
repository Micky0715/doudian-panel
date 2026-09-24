"""Persist validated, operator-uploaded Gaia model mapping versions.

The original upload is copied into this module's own directory.  No Gaia or
DouDian API call is made here, and rejected uploads never become versions.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ..domain.mapping_contract import GaiaMappingContractError, ModelMappingRecord
from .model_mapping_import import import_model_mapping, import_model_mapping_with_report


MAX_MAPPING_BYTES = 32 * 1024 * 1024
ALLOWED_SUFFIXES = {".xlsx", ".csv"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ModelMappingRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def latest(self) -> dict | None:
        versions = []
        for path in self.root.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (isinstance(payload, dict) and payload.get("id") == path.stem
                    and payload.get("suffix") in ALLOWED_SUFFIXES
                    and (self.root / (path.stem + payload["suffix"])).is_file()):
                versions.append(payload)
        if not versions:
            return None
        latest = max(versions, key=lambda item: (item.get("uploaded_at", ""), item["id"]))
        return self.get(latest["id"])[0]

    def get(self, version_id: str) -> tuple[dict, Path]:
        """Resolve one immutable version and verify its bytes before every use."""
        if not re.fullmatch(r"[0-9a-f]{32}", str(version_id or "")):
            raise GaiaMappingContractError("型号映射版本 ID 无效")
        metadata_path = self.root / (version_id + ".json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GaiaMappingContractError("型号映射版本不存在") from exc
        suffix = metadata.get("suffix")
        if metadata.get("id") != version_id or suffix not in ALLOWED_SUFFIXES:
            raise GaiaMappingContractError("型号映射版本元数据无效")
        source = self.root / (version_id + suffix)
        if not source.is_file() or _sha256_file(source) != metadata.get("sha256"):
            raise GaiaMappingContractError("型号映射版本文件哈希不一致，请重新上传")
        index_hash = str(metadata.get("index_sha256") or "")
        if index_hash:
            index_path = self.root / (version_id + ".index.json")
            if not index_path.is_file() or _sha256_file(index_path) != index_hash:
                raise GaiaMappingContractError("型号映射规范索引哈希不一致，请重新上传")
        return metadata, source

    def load_records(self, version_id: str) -> tuple[dict, List[ModelMappingRecord]]:
        """Load a hash-verified canonical index, with old-version fallback."""
        metadata, source = self.get(version_id)
        index_hash = str(metadata.get("index_sha256") or "")
        if not index_hash:
            return metadata, import_model_mapping(str(source))
        index_path = self.root / (version_id + ".index.json")
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            records = [ModelMappingRecord(**item) for item in payload]
        except (OSError, json.JSONDecodeError, TypeError, GaiaMappingContractError) as exc:
            raise GaiaMappingContractError("型号映射规范索引内容无效") from exc
        if len(records) != metadata.get("record_count"):
            raise GaiaMappingContractError("型号映射规范索引行数不一致")
        return metadata, records

    def save_upload(self, upload) -> dict:
        name = str(upload.filename or "").replace("\\", "/").split("/")[-1].strip()
        return self._save_chunks(name, iter(lambda: upload.stream.read(1024 * 1024), b""), "upload")

    def save_path(self, source_path: str | Path) -> dict:
        """Copy and validate a server-side source without modifying it."""
        source = Path(source_path).expanduser().resolve()
        if not source.is_file() or source.is_symlink():
            raise GaiaMappingContractError("型号映射表不存在或不是普通文件")
        name = source.name
        with source.open("rb") as handle:
            return self._save_chunks(name, iter(lambda: handle.read(1024 * 1024), b""), "downloads")

    def _save_chunks(self, name: str, chunks, source_kind: str) -> dict:
        suffix = Path(name).suffix.lower()
        if not name or suffix not in ALLOWED_SUFFIXES or name.startswith("~$"):
            raise GaiaMappingContractError("型号映射表仅支持 .xlsx 或 .csv，且不能是 Excel 临时文件")

        version_id = uuid.uuid4().hex
        temporary = self.root / (version_id + ".upload" + suffix)
        destination = self.root / (version_id + suffix)
        metadata_path = self.root / (version_id + ".json")
        metadata_temp = self.root / (version_id + ".json.tmp")
        index_path = self.root / (version_id + ".index.json")
        index_temp = self.root / (version_id + ".index.json.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                for chunk in chunks:
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_MAPPING_BYTES:
                        raise GaiaMappingContractError("型号映射表超过 32 MB 限制")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise GaiaMappingContractError("型号映射表为空文件")

            import_report = import_model_mapping_with_report(str(temporary))
            records = import_report.records
            index_bytes = json.dumps(
                [
                    {
                        "brand": row.brand,
                        "model": row.model,
                        "standard_sku_id": row.standard_sku_id,
                        "category": row.category,
                    }
                    for row in records
                ],
                ensure_ascii=False, separators=(",", ":"),
            ).encode("utf-8")
            with index_temp.open("xb") as handle:
                handle.write(index_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            metadata = {
                "id": version_id,
                "name": name[:180],
                "suffix": suffix,
                "sha256": digest.hexdigest(),
                "size": size,
                "record_count": len(records),
                "source_data_rows": import_report.source_data_rows,
                "accepted_rows": import_report.accepted_rows,
                "duplicate_rows": import_report.duplicate_rows,
                "mainland_filter_applied": import_report.mainland_filter_applied,
                "mapping_policy": "explicit_mainland_only_v2" if import_report.mainland_filter_applied else "operator_mapping_v1",
                "excluded_non_mainland_rows": import_report.excluded_non_mainland_rows,
                "excluded_non_mainland_sku_count": import_report.excluded_non_mainland_sku_count,
                "index_sha256": hashlib.sha256(index_bytes).hexdigest(),
                "index_size": len(index_bytes),
                "category_counts": dict(sorted(Counter(row.category for row in records).items())),
                "source_kind": source_kind,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            with metadata_temp.open("x", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.replace(index_temp, index_path)
            os.replace(metadata_temp, metadata_path)
            return metadata
        finally:
            for path in (temporary, metadata_temp, index_temp):
                if path.exists():
                    path.unlink()
