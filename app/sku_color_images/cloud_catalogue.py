"""Match trusted Feishu white-image names to existing SKU colour values.

This module performs no Drive traversal, download or platform write.  Its input
has already been scoped to one exact ``白底图库/手机/品牌/型号`` directory and
validated by :mod:`app.material_pipeline`.  File stems are matched with the
same exact NFKC/case-insensitive rule used by the local SKU colour catalogue.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.material_pipeline.domain import CloudImage

from .catalogue import ColorGalleryError


def _label(value: Any) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip(),
    ).casefold()


@dataclass(frozen=True)
class CloudColorImage:
    color_name: str
    spec_detail_id: str
    image: CloudImage


@dataclass(frozen=True)
class CloudColorImagePlan:
    images: tuple[CloudColorImage, ...]
    missing: tuple[str, ...]
    invalid: tuple[tuple[str, str], ...]
    color_count: int

    @property
    def complete(self) -> bool:
        return (
            len(self.images) == self.color_count
            and not self.missing
            and not self.invalid
        )


def resolve_cloud_color_images(
    candidates: Sequence[CloudImage], detail: Mapping[str, Any],
) -> CloudColorImagePlan:
    """Map exact colour filenames to the product's first specification group."""
    specs = detail.get("specs")
    if not isinstance(specs, list) or not specs or not isinstance(specs[0], Mapping):
        raise ColorGalleryError("商品详情没有可核对的第一组规格")
    first = specs[0]
    if _label(first.get("name")) != "颜色":
        raise ColorGalleryError("第一组规格不是颜色；平台仅支持第一组添加规格图")
    values = first.get("values")
    if not isinstance(values, list) or not values:
        raise ColorGalleryError("颜色规格没有有效的颜色值")

    by_name: dict[str, list[CloudImage]] = {}
    for image in candidates:
        key = _label(Path(image.name).stem)
        if key:
            by_name.setdefault(key, []).append(image)

    images: list[CloudColorImage] = []
    missing: list[str] = []
    invalid: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise ColorGalleryError("颜色规格值结构异常")
        name = str(value.get("name") or "").strip()
        spec_id = str(value.get("id") or "").strip()
        key = _label(name)
        if not key or not spec_id.isdigit() or key in seen_names or spec_id in seen_ids:
            raise ColorGalleryError("颜色名称或规格值 ID 缺失、重复")
        seen_names.add(key)
        seen_ids.add(spec_id)
        matches = by_name.get(key, [])
        if not matches:
            missing.append(name)
        elif len(matches) != 1:
            invalid.append((name, "同色存在多个云盘图片文件，拒绝猜测选哪张"))
        else:
            images.append(CloudColorImage(name, spec_id, matches[0]))
    return CloudColorImagePlan(
        tuple(images), tuple(missing), tuple(invalid), len(values),
    )
