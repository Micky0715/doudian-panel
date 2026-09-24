"""Cloud image upload orchestration with immutable source evidence."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Mapping

from integrations.doudian.materials import DoudianMaterialGateway, UploadedMaterial
from integrations.doudian.write_gateway import DoudianSingleProductImageDraftWriter
from integrations.feishu_drive import FeishuDriveClient

from .cache import MaterialMetadataCache
from .domain import CloudImage, MaterialPlan, MaterialPolicyError


@dataclass(frozen=True)
class ResolvedMaterialPlan:
    main: UploadedMaterial
    white_images: tuple[UploadedMaterial, ...]

    @property
    def pic(self) -> str:
        return "|".join([self.main.byte_url, *(item.byte_url for item in self.white_images)])


class CloudMaterialExecutor:
    """Download a pre-planned Feishu image, upload it, and cache evidence."""

    def __init__(self, drive: FeishuDriveClient, materials: DoudianMaterialGateway,
                 cache: MaterialMetadataCache, *, shop_id: str, folder_id: str):
        self.drive = drive
        self.materials = materials
        self.cache = cache
        self.shop_id = str(shop_id).strip()
        self.folder_id = str(folder_id).strip()
        if not self.shop_id.isdigit() or not self.folder_id.isdigit():
            raise MaterialPolicyError("DouDian shop_id and material folder_id are required")

    def resolve(self, plan: MaterialPlan) -> ResolvedMaterialPlan:
        if plan.shop_id != self.shop_id:
            raise MaterialPolicyError("material plan shop does not match executor shop")
        main = self._resolve_image(plan.main)
        whites = tuple(self._resolve_image(item) for item in plan.white_images)
        return ResolvedMaterialPlan(main, whites)

    def resolve_images(self, images: tuple[CloudImage, ...]) -> tuple[UploadedMaterial, ...]:
        """Resolve additional planned images through the same immutable cache.

        SKU colour pictures can reuse the exact Feishu white-image sources.
        Calling this after :meth:`resolve` reuses already validated DouDian
        materials instead of uploading the same bytes a second time.
        """
        return tuple(self._resolve_image(item) for item in images)

    def _resolve_image(self, item: CloudImage) -> UploadedMaterial:
        existing = self.cache.get(
            self.shop_id, self.folder_id, item.file_token, item.revision, item.sha256,
        )
        if existing:
            material_id = str(existing.get("material_id") or "").strip()
            if material_id:
                # Cached material URLs must be re-read: the platform may have
                # removed, expired or re-audited an item after the prior run.
                byte_url = self.materials.get_material_byte_url(material_id)
                if byte_url:
                    return UploadedMaterial(material_id, byte_url, item.sha256, item.size_bytes)
        data = self.drive.download(item.file_token)
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.sha256 or len(data) != item.size_bytes:
            raise MaterialPolicyError("Feishu source changed after material plan was approved")
        uploaded = self.materials.upload_image(data, item.name, folder_id=self.folder_id)
        if uploaded.sha256 != item.sha256:
            raise MaterialPolicyError("DouDian upload evidence does not match the planned source")
        self.cache.put({
            "shop_id": self.shop_id,
            "folder_id": self.folder_id,
            "file_token": item.file_token,
            "revision": item.revision,
            "sha256": item.sha256,
            "material_id": uploaded.material_id,
            "byte_url": uploaded.byte_url,
        })
        return uploaded

    @staticmethod
    def draft_payload(product_id: str | int, product_format_new: str,
                      resolved: ResolvedMaterialPlan) -> Mapping[str, Any]:
        if not str(product_id).strip().isdigit() or not isinstance(product_format_new, str) or not product_format_new.strip():
            raise MaterialPolicyError("product id and fresh product_format_new are required")
        urls = [value for value in resolved.pic.split("|") if value]
        if len(urls) < 3 or len(set(urls)) != len(urls):
            raise MaterialPolicyError("resolved API image set must contain one main and two distinct whites")
        return {
            "product_id": str(product_id).strip(),
            "product_format_new": product_format_new,
            "pic": resolved.pic,
            "commit": False,
        }

    def save_draft_images(self, writer: DoudianSingleProductImageDraftWriter,
                          product_id: str | int, product_format_new: str,
                          resolved: ResolvedMaterialPlan) -> Mapping[str, Any]:
        """The only product write in this pipeline; always `commit=false`."""
        return writer.save_images_to_draft(self.draft_payload(product_id, product_format_new, resolved))
