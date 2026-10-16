"""Map actual 成色 values to the combined template or maintained files.

All three supported grades use the maintained local combined image. One or
two grades use their exact local gallery files. Nothing is inferred from title
text.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.material_pipeline.image_validation import MAX_SYNC_IMAGE_BYTES, image_dimensions


class GradeDetailError(ValueError):
    pass


@dataclass(frozen=True)
class GradeDetailImage:
    slot: str
    path: Path
    upload_name: str
    sha256: str
    size_bytes: int
    width: int
    height: int


@dataclass(frozen=True)
class GradeDetailPlan:
    grades: tuple[str, ...]
    images: tuple[GradeDetailImage, ...]
    strategy: str = "individual_gallery"


COMMON_SLOTS = ("1", "2", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14")
GRADE_IMAGE_SLOTS = {"9新": "9新", "90新": "9新", "95新": "95新", "99新": "99新"}
NO_GRADE_IMAGE = "准新品"
SUPPORTED_GRADES = frozenset(GRADE_IMAGE_SLOTS) | {NO_GRADE_IMAGE}
THREE_GRADE_SLOTS = frozenset({"99新", "95新", "9新"})
SOURCE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def _name(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip()


def _grade_group_name(value: Any) -> str:
    """Normalize only DouDian's decorative brackets around the grade group."""
    name = _name(value)
    if name.startswith("【") and name.endswith("】"):
        return _name(name[1:-1])
    return name


def _grade_values(detail: Mapping[str, Any]) -> tuple[str, ...]:
    specs = detail.get("specs")
    if not isinstance(specs, list):
        raise GradeDetailError("商品详情没有可核对的规格")
    groups = [group for group in specs if isinstance(group, Mapping)
              and _grade_group_name(group.get("name")) == "成色"]
    if len(groups) != 1:
        raise GradeDetailError("成色规格组缺失或重复")
    values = groups[0].get("values")
    if not isinstance(values, list) or not values:
        raise GradeDetailError("成色规格没有值")
    grades = tuple(_name(value.get("name")) for value in values if isinstance(value, Mapping))
    if len(grades) != len(values) or len(grades) != len(set(grades)):
        raise GradeDetailError("成色规格值为空、重复或结构异常")
    if any(grade not in SUPPORTED_GRADES for grade in grades):
        raise GradeDetailError("成色规格值未在已维护图库中明确支持：" + ",".join(grades))
    if NO_GRADE_IMAGE in grades and grades != (NO_GRADE_IMAGE,):
        raise GradeDetailError("准新品与其他成色混用，不能自动决定成色图")
    return grades


def _one_file(root: Path, slot: str) -> Path:
    matches = [path for path in root.iterdir()
               if path.is_file() and not path.is_symlink()
               and path.suffix.casefold() in SOURCE_SUFFIXES
               and not path.name.startswith(("._", "~$"))
               and _name(path.stem) == slot]
    if len(matches) != 1:
        raise GradeDetailError(f"详情图库槽位 {slot} 缺失或有多个同名文件")
    return matches[0]


def _image(slot: str, path: Path) -> GradeDetailImage:
    data = path.read_bytes()
    if not data or len(data) > MAX_SYNC_IMAGE_BYTES:
        raise GradeDetailError(f"详情图 {slot} 为空或超过 3 MiB")
    if data.startswith(b"\xff\xd8"):
        suffix = ".jpg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise GradeDetailError(f"详情图 {slot} 内容格式不是 JPEG/PNG/WebP")
    width, height = image_dimensions(data)
    if width <= 0 or height <= 0:
        raise GradeDetailError(f"详情图 {slot} 无法识别尺寸")
    return GradeDetailImage(
        slot=slot, path=path, upload_name=path.stem.strip() + suffix,
        sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data),
        width=width, height=height,
    )


def build_grade_detail_plan(detail: Mapping[str, Any], root: str | Path) -> GradeDetailPlan:
    detail_root = Path(root).expanduser().resolve()
    if not detail_root.is_dir():
        raise GradeDetailError("商品详情图库不存在")
    grades = _grade_values(detail)
    if grades == (NO_GRADE_IMAGE,):
        images = tuple(_image(slot, _one_file(detail_root, slot)) for slot in COMMON_SLOTS)
        if len({image.sha256 for image in images}) != len(images):
            raise GradeDetailError("详情图库存在内容重复的槽位")
        return GradeDetailPlan(grades=grades, images=images, strategy="common_only")
    grade_slots = tuple(GRADE_IMAGE_SLOTS[grade] for grade in grades)
    if len(set(grade_slots)) != len(grade_slots):
        raise GradeDetailError("多个成色规格映射到同一张成色图，需人工核对")
    slots = COMMON_SLOTS[:2] + grade_slots + COMMON_SLOTS[2:]
    images = tuple(_image(slot, _one_file(detail_root, slot)) for slot in slots)
    if len({image.sha256 for image in images}) != len(images):
        raise GradeDetailError("详情图库存在内容重复的槽位")
    return GradeDetailPlan(grades=grades, images=images)


def build_grade_only_plan(detail: Mapping[str, Any], root: str | Path) -> GradeDetailPlan:
    """Choose an existing combined image for all three grades, else local files."""
    detail_root = Path(root).expanduser().resolve()
    if not detail_root.is_dir():
        raise GradeDetailError("商品详情图库不存在")
    grades = _grade_values(detail)
    if grades == (NO_GRADE_IMAGE,):
        return GradeDetailPlan(grades=grades, images=(), strategy="no_grade_image")
    slots = tuple(GRADE_IMAGE_SLOTS[grade] for grade in grades)
    if len(set(slots)) != len(slots):
        raise GradeDetailError("多个成色规格映射到同一张成色图，需人工核对")
    if len(slots) == 3 and frozenset(slots) == THREE_GRADE_SLOTS:
        combined = _image("三合一", _one_file(detail_root, "三合一"))
        return GradeDetailPlan(
            grades=grades, images=(combined,), strategy="combined_gallery",
        )
    images = tuple(_image(slot, _one_file(detail_root, slot)) for slot in slots)
    if len({image.sha256 for image in images}) != len(images):
        raise GradeDetailError("不同成色图的文件内容重复")
    return GradeDetailPlan(grades=grades, images=images, strategy="individual_gallery")
