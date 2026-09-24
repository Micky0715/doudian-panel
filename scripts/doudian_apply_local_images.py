"""Save one explicit local image set to an existing DouDian draft through API.

This is a deliberately narrow bridge while the Feishu Drive application is
waiting for read-only permission approval.  It accepts *explicit* local paths;
it does not search loosely by filename and it never submits a product.

The sequence is:

1. validate one square main image and at least two distinct square white images;
2. upload their bytes to the DouDian material centre to obtain platform URLs;
3. write exactly one draft with ``product.editV2(commit=false)``;
4. read back the ordered image set; rollback the old ``pic`` on any mismatch.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import re
import json
import os
from pathlib import Path
import struct
import tempfile
import time
from typing import Any, Mapping, Sequence

from doudian_api import read_env_file
from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway, MAX_SYNC_IMAGE_BYTES, UploadedMaterial
from integrations.doudian.write_gateway import DoudianSingleProductImageDraftWriter


ALLOWED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
MINIMUM_WHITE_IMAGES = 2


class LocalImagePolicyError(ValueError):
    """Raised before any material or product API write is made.

    ``skipped_auxiliary`` lets a batch report distinguish a product-level
    shortage from the individual source files that caused it.
    """

    def __init__(self, message: str, *, skipped_auxiliary: Sequence["SkippedImage"] = ()):
        super().__init__(message)
        self.skipped_auxiliary = tuple(skipped_auxiliary)


@dataclass(frozen=True)
class LocalImage:
    path: Path
    name: str
    sha256: str
    size_bytes: int
    image_bytes: bytes


@dataclass(frozen=True)
class LocalMainAreaPlan:
    """One ordered 1:1 gallery prepared from exact local sources.

    A real main image is preferred.  When it is missing, the first unique
    white-background image becomes the cover and is removed from the
    auxiliary list, matching the production browser runner's no-main mode.
    """

    main: LocalImage
    auxiliary: tuple[LocalImage, ...]
    main_substituted: bool
    skipped_auxiliary: tuple["SkippedImage", ...] = ()


@dataclass(frozen=True)
class SkippedImage:
    """One source image intentionally omitted without blocking its product."""

    name: str
    reason: str


def _image_dimensions(image_bytes: bytes) -> tuple[int, int]:
    """Read JPEG/PNG dimensions without loading a third-party image library."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        return struct.unpack(">II", image_bytes[16:24])
    if image_bytes[:2] != b"\xff\xd8":
        return (0, 0)
    offset = 2
    length = len(image_bytes)
    while offset < length:
        if image_bytes[offset] != 0xFF:
            offset += 1
            continue
        offset += 1
        while offset < length and image_bytes[offset] == 0xFF:
            offset += 1
        if offset >= length:
            break
        marker = image_bytes[offset]
        offset += 1
        if marker in (0xD8, 0xD9):
            continue
        if offset + 2 > length:
            break
        segment_length = struct.unpack(">H", image_bytes[offset:offset + 2])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if offset + 7 <= length:
                height, width = struct.unpack(">HH", image_bytes[offset + 3:offset + 7])
                return (width, height)
            break
        offset += max(2, segment_length)
    return (0, 0)


def _read_square_image(path: str | Path) -> LocalImage:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LocalImagePolicyError(f"local image does not exist: {resolved}")
    if resolved.suffix.casefold() not in ALLOWED_IMAGE_SUFFIXES:
        raise LocalImagePolicyError(f"unsupported image extension: {resolved.name}")
    data = resolved.read_bytes()
    if not data:
        raise LocalImagePolicyError(f"local image is empty: {resolved.name}")
    if len(data) > MAX_SYNC_IMAGE_BYTES:
        raise LocalImagePolicyError(
            f"local image exceeds DouDian synchronous 3MB limit: {resolved.name}"
        )
    width, height = _image_dimensions(data)
    if width < 600 or height < 600 or width != height:
        raise LocalImagePolicyError(
            f"local image must be a square at least 600px: {resolved.name} ({width}x{height})"
        )
    return LocalImage(
        path=resolved, name=resolved.name, sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data), image_bytes=data,
    )


def _read_detail_image(path: str | Path) -> LocalImage:
    """Read one detail image without applying the 1:1 main-area constraint."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise LocalImagePolicyError(f"local detail image does not exist: {resolved}")
    if resolved.suffix.casefold() not in ALLOWED_IMAGE_SUFFIXES:
        raise LocalImagePolicyError(f"unsupported image extension: {resolved.name}")
    data = resolved.read_bytes()
    if not data:
        raise LocalImagePolicyError(f"local detail image is empty: {resolved.name}")
    if len(data) > MAX_SYNC_IMAGE_BYTES:
        raise LocalImagePolicyError(
            f"local detail image exceeds DouDian synchronous 3MB limit: {resolved.name}"
        )
    width, height = _image_dimensions(data)
    if width <= 0 or height <= 0:
        raise LocalImagePolicyError(f"local detail image is not a supported raster image: {resolved.name}")
    return LocalImage(
        path=resolved, name=resolved.name, sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data), image_bytes=data,
    )


def build_dynamic_main_area_plan(
    main_path: str | Path | None, white_paths: Sequence[str | Path],
) -> LocalMainAreaPlan:
    """Implement the browser runner's dynamic main/white policy.

    - With a real main: require at least two distinct *uploadable* white
      images and use up to four of them.  An invalid extra white image is
      recorded and omitted instead of blocking the product.
    - Without one: require three distinct white images, use the first as the
      cover and the remainder as auxiliaries (up to the 1:1 five-slot limit).
    """
    main = _read_square_image(main_path) if main_path else None
    unique_whites: list[LocalImage] = []
    skipped: list[SkippedImage] = []
    seen = {main.sha256} if main else set()
    for path in white_paths:
        try:
            item = _read_square_image(path)
        except LocalImagePolicyError as exc:
            skipped.append(SkippedImage(Path(path).name, str(exc)))
            continue
        if item.sha256 in seen:
            if main and item.sha256 == main.sha256:
                raise LocalImagePolicyError("main and white images must have distinct content")
            continue
        seen.add(item.sha256)
        unique_whites.append(item)
    if main:
        if len(unique_whites) < MINIMUM_WHITE_IMAGES:
            raise LocalImagePolicyError(
                "real main image requires at least two distinct uploadable white images",
                skipped_auxiliary=skipped,
            )
        return LocalMainAreaPlan(main, tuple(unique_whites[:4]), False, tuple(skipped))
    if len(unique_whites) < MINIMUM_WHITE_IMAGES + 1:
        raise LocalImagePolicyError(
            "missing main image requires at least three distinct uploadable white images",
            skipped_auxiliary=skipped,
        )
    return LocalMainAreaPlan(unique_whites[0], tuple(unique_whites[1:5]), True, tuple(skipped))


def validate_local_image_set(main_path: str | Path, white_paths: Sequence[str | Path]) -> tuple[LocalImage, tuple[LocalImage, ...]]:
    """Validate exact local paths before any external API call.

    Local sources intentionally receive no filename/model fuzzy matching here:
    the caller must provide the already-resolved material set.  This keeps the
    API bridge unable to silently select a neighbouring phone model's image.
    """
    plan = build_dynamic_main_area_plan(main_path, white_paths)
    return plan.main, plan.auxiliary


def read_detail_image_set(paths: Sequence[str | Path]) -> tuple[LocalImage, ...]:
    images = tuple(_read_detail_image(path) for path in paths)
    if len(images) != 14:
        raise LocalImagePolicyError("exactly fourteen detail images are required")
    if len({item.sha256 for item in images}) != len(images):
        raise LocalImagePolicyError("detail images must have distinct content")
    return images


def _format_for_edit(detail: Mapping[str, Any]) -> str:
    raw = detail.get("product_format_new")
    if not isinstance(raw, str) or not raw.strip():
        raise LocalImagePolicyError("draft has no product_format_new to preserve")
    return json.dumps(
        normalize_product_format_for_edit(parse_product_format(raw)),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _pic_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split("|") if item.strip()]
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())
            elif isinstance(item, Mapping):
                raw = item.get("url") or item.get("byte_url")
                if isinstance(raw, str) and raw.strip():
                    urls.append(raw.strip())
        return urls
    return []


def verify_image_readback(detail: Mapping[str, Any], expected_pic: str) -> list[str]:
    actual, expected = _pic_urls(detail.get("pic")), _pic_urls(expected_pic)
    if len(expected) < 4:
        return ["expected_api_pic_set_invalid"]
    if not actual:
        return ["product_pic_missing_after_write"]
    issues: list[str] = []
    if actual[0] != expected[0]:
        issues.append("main_image_does_not_match_uploaded_store_main")
    if actual != expected:
        issues.append("product_image_order_or_count_does_not_match_uploaded_set")
    return issues


_DESCRIPTION_IMAGE_RE = re.compile(
    r"<img\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL,
)


def detail_html(urls: Sequence[str]) -> str:
    """Return the documented ``product.editV2.description`` URL-list value.

    Despite the historical name, this API field is *not* rich HTML.  DouDian
    expects its own audited material URLs joined by ``|``.  Sending an ``img``
    tag makes the platform reject an otherwise valid material URL as an
    external/invalid link.
    """
    if len(urls) != 14 or any(not str(url).startswith(("https://", "http://")) for url in urls):
        raise LocalImagePolicyError("detail image list requires exactly fourteen platform URLs")
    return "|".join(str(url).strip() for url in urls)


def detail_urls(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    # Current API contract: a pipe-delimited list.  Keep HTML extraction only
    # for read-only comparison of old browser-created drafts during migration.
    urls = _pic_urls(value)
    if urls and all(url.startswith(("https://", "http://")) for url in urls):
        return urls
    return [match.group(2).strip() for match in _DESCRIPTION_IMAGE_RE.finditer(value) if match.group(2).strip()]


def verify_media_readback(detail: Mapping[str, Any], expected_pic: str,
                          expected_detail_urls: Sequence[str]) -> list[str]:
    issues = verify_image_readback(detail, expected_pic)
    actual_detail_urls = detail_urls(detail.get("description"))
    if len(expected_detail_urls) != 14:
        issues.append("expected_detail_set_invalid")
    elif actual_detail_urls != list(expected_detail_urls):
        issues.append("detail_image_order_or_count_does_not_match_uploaded_set")
    return issues


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _upload_local_set(gateway: DoudianMaterialGateway, main: LocalImage,
                      whites: Sequence[LocalImage], folder_id: str) -> tuple[UploadedMaterial, tuple[UploadedMaterial, ...]]:
    uploaded_main = gateway.upload_image(main.image_bytes, main.name, folder_id=folder_id)
    if uploaded_main.sha256 != main.sha256:
        raise LocalImagePolicyError("DouDian material evidence does not match local main image")
    uploaded_whites = tuple(
        gateway.upload_image(item.image_bytes, item.name, folder_id=folder_id)
        for item in whites
    )
    if any(uploaded.sha256 != local.sha256 for uploaded, local in zip(uploaded_whites, whites)):
        raise LocalImagePolicyError("DouDian material evidence does not match local white image")
    urls = [uploaded_main.byte_url, *(item.byte_url for item in uploaded_whites)]
    if len(urls) < 4 or len(set(urls)) != len(urls):
        raise LocalImagePolicyError("DouDian material upload did not produce four distinct platform URLs")
    return uploaded_main, uploaded_whites


def main() -> int:
    parser = argparse.ArgumentParser(description="Save one explicit local image set to one DouDian draft through API")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--main", required=True, help="exact local main image path")
    parser.add_argument("--white", action="append", default=[], help="exact local white-image path; repeat at least 3 times")
    parser.add_argument("--folder-id", required=True,
                        help="explicit DouDian image material folder ID; never guessed by name")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true", help="save draft only; never submits")
    parser.add_argument("--report-root", default="panel_data/local_image_api_reports")
    args = parser.parse_args()

    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit():
        raise SystemExit("shop_id and product_id must be numeric")
    report: dict[str, Any] = {
        "schema_version": 1, "created_at": int(time.time()), "shop_id": shop_id,
        "product_id": product_id, "mode": "execute" if args.execute else "dry_run",
        "operation": "open.materialToken+ImageX+material.uploadImageSync+product.editV2(commit=false,pic)",
        "result": "blocked", "submit": False,
    }
    report_path = Path(args.report_root) / shop_id / f"{report['created_at']}_{product_id}.json"
    try:
        folder_id = str(args.folder_id).strip()
        if not folder_id.isdigit():
            raise LocalImagePolicyError("folder_id must be numeric")
        main_image, white_images = validate_local_image_set(args.main, args.white)
        report["local_source"] = {
            "main": {"name": main_image.name, "sha256": main_image.sha256, "size_bytes": main_image.size_bytes},
            "white_images": [
                {"name": item.name, "sha256": item.sha256, "size_bytes": item.size_bytes}
                for item in white_images
            ],
        }
        values = read_env_file(args.env)
        reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
        before = dict(reader.get_product_detail(product_id, show_draft=True))
        if before.get("status") != 0 or str(before.get("check_status")) != "1":
            raise LocalImagePolicyError("product is not a pending draft")
        # The read API returns ``pic`` as either a pipe-joined string or a
        # list, depending on the product's history.  Normalise it before the
        # write boundary so a failed verification restores the exact prior
        # ordered URL list rather than rejecting an otherwise recoverable
        # draft solely due to response representation.
        old_pic_urls = _pic_urls(before.get("pic"))
        old_pic = "|".join(old_pic_urls)
        if not old_pic:
            raise LocalImagePolicyError("rollback requires existing pic evidence")
        product_format_new = _format_for_edit(before)
        report.update({"before_pic": old_pic_urls, "draft_name": before.get("name")})
        if not args.execute:
            report["result"] = "local_source_verified_dry_run"
        else:
            gateway = DoudianMaterialGateway(args.env, shop_id)
            uploaded_main, uploaded_whites = _upload_local_set(
                gateway, main_image, white_images, folder_id
            )
            expected_pic = "|".join([uploaded_main.byte_url, *(item.byte_url for item in uploaded_whites)])
            writer = DoudianSingleProductImageDraftWriter(args.env, shop_id, product_id)
            response = writer.save_images_to_draft({
                "product_id": product_id, "product_format_new": product_format_new,
                "pic": expected_pic, "commit": False,
            })
            after = dict(reader.get_product_detail(product_id, show_draft=True))
            issues = verify_image_readback(after, expected_pic)
            report.update({
                "write_code": response.get("code"), "uploaded_material_ids": [
                    uploaded_main.material_id, *(item.material_id for item in uploaded_whites)
                ], "expected_pic": _pic_urls(expected_pic), "after_pic": _pic_urls(after.get("pic")),
                "verification_issues": issues,
            })
            if issues:
                rollback = writer.save_images_to_draft({
                    "product_id": product_id, "product_format_new": product_format_new,
                    "pic": old_pic, "commit": False,
                })
                report.update({"result": "rolled_back", "rollback_code": rollback.get("code")})
            else:
                report["result"] = "verified_draft_saved"
    except Exception as exc:
        report["error"] = str(exc)
    _atomic_json(report_path, report)
    print(json.dumps({"result": report["result"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if report["result"] in {"local_source_verified_dry_run", "verified_draft_saved"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
