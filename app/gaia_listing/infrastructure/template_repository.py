"""Immutable, validated Gaia upload templates managed by the panel."""

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .excel_inspector import ExcelInspectionError, inspect_xlsx, discover_excel_files, load_profiles


MAX_TEMPLATE_BYTES = 16 * 1024 * 1024


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class TemplateRepository:
    def __init__(self, root, profiles_path):
        self.root = Path(root).resolve()
        self.versions = self.root / "versions"
        self.versions.mkdir(parents=True, exist_ok=True)
        self.profiles_path = profiles_path

    def get(self, version_id):
        if not re.fullmatch(r"[0-9a-f]{32}", str(version_id or "")):
            raise ExcelInspectionError("发品模板版本 ID 无效")
        try:
            metadata = json.loads((self.versions / (version_id + ".json")).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ExcelInspectionError("发品模板版本不存在") from exc
        source = self.versions / (version_id + ".xlsx")
        if metadata.get("id") != version_id or not source.is_file() or _hash(source) != metadata.get("sha256"):
            raise ExcelInspectionError("发品模板版本文件哈希不一致，请重新上传")
        return {**metadata, "path": str(source)}

    def current(self):
        versions = []
        for path in self.versions.glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if metadata.get("id") == path.stem:
                versions.append(metadata)
        if not versions:
            # One-time migration of the existing panel-owned template; never
            # scan Downloads or promote an unrecognized workbook.
            for candidate in discover_excel_files(str(self.root)):
                try:
                    return self.save_path(candidate.path)
                except ExcelInspectionError:
                    continue
            return None
        newest = max(versions, key=lambda item: (item.get("uploaded_at", ""), item["id"]))
        return self.get(newest["id"])

    def save_upload(self, upload):
        name = str(upload.filename or "").replace("\\", "/").split("/")[-1].strip()
        return self._save(name, iter(lambda: upload.stream.read(1024 * 1024), b""), "upload")

    def save_path(self, source_path):
        source = Path(source_path).resolve()
        if not source.is_file() or source.is_symlink() or source.parent != self.root:
            raise ExcelInspectionError("只能导入面板已保存的旧模板")
        with source.open("rb") as handle:
            return self._save(source.name, iter(lambda: handle.read(1024 * 1024), b""), "legacy_managed")

    def _save(self, name, chunks, source_kind):
        if not name or Path(name).suffix.lower() != ".xlsx" or name.startswith("~$"):
            raise ExcelInspectionError("发品模板仅支持 .xlsx 文件")
        version_id = uuid.uuid4().hex
        temporary = self.versions / (version_id + ".upload.xlsx")
        destination = self.versions / (version_id + ".xlsx")
        metadata_temp = self.versions / (version_id + ".json.tmp")
        metadata_path = self.versions / (version_id + ".json")
        size = 0
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as output:
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_TEMPLATE_BYTES:
                        raise ExcelInspectionError("发品模板超过 16 MB 限制")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if not size:
                raise ExcelInspectionError("发品模板为空文件")
            inspection = inspect_xlsx(str(temporary), load_profiles(self.profiles_path))
            profile_ids = {item["id"] for item in inspection["profile_matches"]}
            if inspection["status"] != "recognized" or profile_ids != {"gaia_publish_template"}:
                raise ExcelInspectionError("文件不是唯一识别的盖亚双维成色发品模板")
            metadata = {
                "id": version_id, "name": name[:180], "size": size,
                "sha256": _hash(temporary), "source_kind": source_kind,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            with metadata_temp.open("x", encoding="utf-8") as output:
                json.dump(metadata, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            os.replace(metadata_temp, metadata_path)
            return {**metadata, "path": str(destination)}
        finally:
            for path in (temporary, metadata_temp):
                if path.exists():
                    path.unlink()
