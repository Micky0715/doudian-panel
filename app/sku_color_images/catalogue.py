"""Match existing first-level colour values to operator-maintained image files.

Source layout: 商品图片/店铺SKU颜色图/<brand>/<model>/<colour>.<extension>.
This module neither uploads files nor changes a product. It deliberately
rejects ambiguous brand/model folders and never guesses a colour synonym.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.material_pipeline.image_validation import (
    ALLOWED_IMAGE_SUFFIXES,
    MAX_SYNC_IMAGE_BYTES,
    image_dimensions,
)
from brand_model_rules import image_model_leaf_allowed, load_rules, model_exact_overrides
from model_catalogue import model_key


class ColorGalleryError(ValueError):
    """The gallery or product specification cannot be matched safely."""


@dataclass(frozen=True)
class ColorImage:
    color_name: str
    spec_detail_id: str
    path: Path
    sha256: str
    size_bytes: int
    width: int
    height: int
    upload_name: str
    format_warning: str = ""


@dataclass(frozen=True)
class ColorImagePlan:
    brand_directory: Path
    model_directory: Path
    images: tuple[ColorImage, ...]
    missing: tuple[str, ...]
    invalid: tuple[tuple[str, str], ...]
    color_count: int


def _label(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip()).casefold()


def _brand_rule(brand: str) -> tuple[str, tuple[str, ...]]:
    normalized = _label(brand).replace(" ", "")
    matches = []
    for key, row in load_rules()["platform_brands"].items():
        aliases = (key, row.get("value", ""), *row.get("aliases", []))
        if normalized in {_label(value).replace(" ", "") for value in aliases}:
            matches.append((key, tuple(str(value) for value in aliases if value)))
    if len(matches) != 1:
        raise ColorGalleryError("品牌无法唯一对应店铺 SKU 颜色图库目录")
    return matches[0]


def _contained_directory(root: Path, item: Path) -> bool:
    if item.is_symlink() or not item.is_dir():
        return False
    try:
        item.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _literal_model(value: str, aliases: tuple[str, ...]) -> str:
    text = _label(value)
    for alias in sorted(aliases, key=len, reverse=True):
        prefix = _label(alias)
        if text.startswith(prefix + " "):
            return text[len(prefix):].strip()
    return text


def _model_directory(brand_dir: Path, brand_key: str, aliases: tuple[str, ...],
                     model: str) -> Path:
    expected_key = model_key(model)
    if not expected_key:
        raise ColorGalleryError("商品型号为空")
    lookup_keys = [expected_key]
    for source_name, target_name in model_exact_overrides().items():
        source_key = model_key(source_name)
        if model_key(target_name) == expected_key and source_key != expected_key:
            lookup_keys.append(source_key)
    candidates = []
    for lookup_key in lookup_keys:
        candidates = [
            folder for folder in brand_dir.iterdir()
            if _contained_directory(brand_dir, folder)
            and image_model_leaf_allowed(brand_key, folder.name)
            and model_key(folder.name) == lookup_key
        ]
        if candidates:
            break
    if not candidates:
        raise ColorGalleryError("图库没有品牌和型号精确对应的目录")
    literal = _literal_model(model, aliases)
    literal_matches = [folder for folder in candidates
                       if _literal_model(folder.name, aliases) == literal]
    chosen = literal_matches or candidates
    if len(chosen) != 1:
        raise ColorGalleryError("型号目录存在多个匹配项，需运营消除歧义：" +
                                "、".join(sorted(folder.name for folder in chosen)))
    return chosen[0]


def _read_color_image(path: Path, color_name: str, spec_detail_id: str) -> ColorImage:
    if path.is_symlink() or not path.is_file():
        raise ColorGalleryError("颜色图不是普通文件")
    if path.suffix.casefold() not in ALLOWED_IMAGE_SUFFIXES:
        raise ColorGalleryError("颜色图格式不支持")
    size = path.stat().st_size
    if size < 1:
        raise ColorGalleryError("颜色图为空")
    if size > MAX_SYNC_IMAGE_BYTES:
        raise ColorGalleryError("颜色图超过 API 素材上传 3 MiB 限制")
    data = path.read_bytes()
    if len(data) != size:
        raise ColorGalleryError("读取过程中颜色图发生变化")
    if data.startswith(b"\xff\xd8"):
        actual_suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual_suffix = ".png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        actual_suffix = ".webp"
    else:
        raise ColorGalleryError("颜色图内容不是可识别的 JPEG/PNG/WebP")
    width, height = image_dimensions(data)
    if width < 600 or height < 600 or width != height:
        raise ColorGalleryError("颜色图必须为至少 600px 的可识别 1:1 正方形")
    allowed_suffixes = {actual_suffix}
    if actual_suffix == ".jpg":
        allowed_suffixes.add(".jpeg")
    return ColorImage(
        color_name=color_name, spec_detail_id=spec_detail_id, path=path,
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=size,
        width=width, height=height,
        upload_name=path.stem + actual_suffix,
        format_warning=("源文件扩展名与真实格式不同；上传时按真实格式命名"
                        if path.suffix.casefold() not in allowed_suffixes else ""),
    )


def resolve_color_images(root: str | Path, brand: str, model: str,
                         detail: Mapping[str, Any]) -> ColorImagePlan:
    """Return only exact, unambiguous images for a product detail snapshot.

    The gallery is shared among stores. The store/product identity must be
    enforced by the caller before any material or product API write.
    Missing/invalid individual colours are reported, not substituted.
    """
    gallery_root = Path(root).resolve()
    if not gallery_root.is_dir():
        raise ColorGalleryError("店铺 SKU 颜色图库不存在")
    brand_key, aliases = _brand_rule(brand)
    brand_dirs = [
        folder for folder in gallery_root.iterdir()
        if _contained_directory(gallery_root, folder)
        and _label(folder.name).replace(" ", "") in {
            _label(alias).replace(" ", "") for alias in aliases
        }
    ]
    if len(brand_dirs) != 1:
        raise ColorGalleryError("图库品牌目录缺失或存在多个同义目录")
    brand_dir = brand_dirs[0]
    model_dir = _model_directory(brand_dir, brand_key, aliases, model)

    specs = detail.get("specs")
    if not isinstance(specs, list) or not specs or not isinstance(specs[0], Mapping):
        raise ColorGalleryError("商品详情没有可核对的第一组规格")
    first = specs[0]
    if _label(first.get("name")) != "颜色":
        raise ColorGalleryError("第一组规格不是颜色；平台仅支持第一组添加规格图")
    values = first.get("values")
    if not isinstance(values, list) or not values:
        raise ColorGalleryError("颜色规格没有有效的颜色值")

    image_files = [item for item in model_dir.iterdir()
                   if item.is_file() and not item.is_symlink()
                   and item.suffix.casefold() in ALLOWED_IMAGE_SUFFIXES
                   and not item.name.startswith(("._", "~$"))]
    images: list[ColorImage] = []
    missing: list[str] = []
    invalid: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    seen_ids: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise ColorGalleryError("颜色规格值结构异常")
        name, spec_id = str(value.get("name") or "").strip(), str(value.get("id") or "").strip()
        key = _label(name)
        if not key or not spec_id.isdigit() or key in seen_names or spec_id in seen_ids:
            raise ColorGalleryError("颜色名称或规格值 ID 缺失、重复")
        seen_names.add(key)
        seen_ids.add(spec_id)
        matches = [file for file in image_files if _label(file.stem) == key]
        if not matches:
            missing.append(name)
            continue
        if len(matches) != 1:
            invalid.append((name, "同色存在多个图片文件，拒绝猜测选哪张"))
            continue
        try:
            images.append(_read_color_image(matches[0], name, spec_id))
        except (ColorGalleryError, OSError) as exc:
            invalid.append((name, str(exc)))
    return ColorImagePlan(
        brand_directory=brand_dir, model_directory=model_dir,
        images=tuple(images), missing=tuple(missing), invalid=tuple(invalid),
        color_count=len(values),
    )
