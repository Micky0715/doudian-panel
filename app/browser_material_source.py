"""Stage cloud-first listing images for the Playwright browser workflow.

Playwright's file chooser accepts filesystem paths, while the authoritative
material library lives in Feishu Drive.  This module resolves the existing
cloud material plan and writes an immutable, digest-verified local staging
copy.  The staging copy is transport cache only; it is never used to resolve
model identity or choose images.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from integrations.feishu_drive import FeishuDriveClient, FeishuDriveConfig

from .material_pipeline import (
    FeishuMaterialCatalog,
    FeishuMaterialLayout,
    MaterialPlan,
    build_material_plan,
)
from .material_pipeline.domain import ALLOWED_IMAGE_SUFFIXES, CloudImage


class BrowserMaterialSourceError(RuntimeError):
    """Cloud material could not be safely staged for browser upload."""


@dataclass(frozen=True)
class BrowserMaterialSelection:
    directory: Path
    plan: MaterialPlan


class _DownloadCachingDrive:
    """Reuse catalogue validation downloads during the subsequent staging."""

    def __init__(self, drive):
        self._drive = drive
        self.config = drive.config
        self._downloads: dict[str, bytes] = {}

    def list_entries(self, *args, **kwargs):
        return self._drive.list_entries(*args, **kwargs)

    def download(self, file_token: str) -> bytes:
        token = str(file_token).strip()
        if token not in self._downloads:
            self._downloads[token] = self._drive.download(token)
        return self._downloads[token]

    def clear_download_cache(self) -> None:
        self._downloads.clear()


def _safe_segment(value: str, fallback: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._+-]+", "_", str(value).strip())
    clean = clean.strip("._")
    return clean[:80] or fallback


def _suffix(image: CloudImage) -> str:
    suffix = Path(image.name).suffix.casefold()
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        raise BrowserMaterialSourceError("云盘图片扩展名不受支持")
    return suffix


class BrowserCloudMaterialStager:
    """Resolve Feishu material and expose a browser-compatible local directory."""

    def __init__(self, catalog, drive, cache_root: Path | str):
        self.catalog = catalog
        self.drive = drive
        self.cache_root = Path(cache_root).expanduser().resolve()

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "BrowserCloudMaterialStager":
        config = FeishuDriveConfig.from_mapping(values)
        raw_drive = FeishuDriveClient(config)
        drive = _DownloadCachingDrive(raw_drive)
        catalog = FeishuMaterialCatalog(
            drive,
            layout=FeishuMaterialLayout.from_mapping(values),
        )
        default_root = Path(__file__).resolve().parents[1] / "panel_data" / "browser_cloud_material_cache"
        cache_root = str(values.get("DP_BROWSER_CLOUD_CACHE_ROOT") or default_root).strip()
        return cls(catalog, drive, cache_root)

    @staticmethod
    def _plan_key(plan: MaterialPlan) -> str:
        identity = [
            (item.file_token, item.revision, item.sha256, item.size_bytes)
            for item in (plan.main, *plan.white_images)
        ]
        raw = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:20]

    @staticmethod
    def _verified_bytes(image: CloudImage, drive) -> bytes:
        data = drive.download(image.file_token)
        if len(data) != image.size_bytes:
            raise BrowserMaterialSourceError("云盘图片下载字节数发生变化")
        if hashlib.sha256(data).hexdigest().casefold() != image.sha256.casefold():
            raise BrowserMaterialSourceError("云盘图片下载摘要发生变化")
        return data

    @staticmethod
    def _write_verified(path: Path, image: CloudImage, drive) -> None:
        if path.is_file():
            try:
                if path.stat().st_size == image.size_bytes:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    if digest.casefold() == image.sha256.casefold():
                        return
            except OSError:
                pass

        data = BrowserCloudMaterialStager._verified_bytes(image, drive)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".stage-", delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def stage(self, shop_id: str, brand: str, model: str) -> BrowserMaterialSelection:
        try:
            bundle = self.catalog.resolve_bundle(shop_id, brand, model)
            plan = build_material_plan(bundle)
            destination = (
                self.cache_root
                / _safe_segment(shop_id, "shop")
                / ("%s__%s" % (_safe_segment(brand, "brand"), _safe_segment(model, "model")))
                / self._plan_key(plan)
            )
            self._write_verified(
                destination / "主图" / ("主图" + _suffix(plan.main)),
                plan.main,
                self.drive,
            )
            for index, image in enumerate(plan.white_images, start=1):
                self._write_verified(
                    destination / "白底图" / ("白底图_%d%s" % (index, _suffix(image))),
                    image,
                    self.drive,
                )
            return BrowserMaterialSelection(destination, plan)
        except BrowserMaterialSourceError:
            raise
        except Exception as exc:
            raise BrowserMaterialSourceError(str(exc)) from exc
        finally:
            clear = getattr(self.drive, "clear_download_cache", None)
            if callable(clear):
                clear()
