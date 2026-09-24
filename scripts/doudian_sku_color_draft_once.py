"""Canary: bind exact local first-level colour pictures to one DouDian draft.

Default mode is read-only. ``--execute`` uploads only the matched images to
the selected shop's material centre, then sends one editV2(commit=false)
request. It never submits or publishes a product and refuses a live listing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from app.listing_preflight.legacy_resolver import resolve_brand_model
from app.listing_preflight.source_title import source_title
from app.sku_color_images import ColorImagePlan, resolve_color_images
from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.doudian.write_gateway import DoudianSingleProductSpecImageDraftWriter


DEFAULT_GALLERY = "/Users/atrenew/商品图片/店铺SKU颜色图"
GUARDED_FIELDS = (
    "name", "name_prefix", "name_suffix", "status", "check_status", "brand_id",
    "standard_brand_id", "spec_id", "specs", "spec_prices", "pic", "description",
    "product_format_new",
)


class SkuColorDraftError(RuntimeError):
    pass


def _product_id(detail: Mapping[str, Any]) -> str:
    return str(detail.get("product_id_str") or detail.get("product_id") or "").strip()


def _source_plan(detail: Mapping[str, Any], gallery_root: str, *,
                 allow_existing: bool = False,
                 require_complete: bool = True) -> ColorImagePlan:
    title = _source_title(detail)
    parsed = resolve_brand_model(title)
    brand = str(parsed.get("target_brand_cn") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not brand or not model:
        raise SkuColorDraftError("商品标题未能唯一解析品牌和型号")
    plan = resolve_color_images(gallery_root, brand, model, detail)
    if require_complete and (
        len(plan.images) != plan.color_count or plan.missing or plan.invalid
    ):
        raise SkuColorDraftError("颜色图未全部精确匹配，拒绝上传或写商品：" +
                                 str({"missing": plan.missing, "invalid": plan.invalid}))
    if detail.get("spec_pics") and not allow_existing:
        raise SkuColorDraftError("目标商品已有规格图；首次验收不覆盖既有颜色图")
    main_count = len(detail.get("pic") or []) if isinstance(detail.get("pic"), list) else 0
    description = str(detail.get("description") or "")
    detail_count = len(re.findall(r"<img\b", description, flags=re.I))
    if main_count + detail_count + len(plan.images) > 50:
        raise SkuColorDraftError("主图、详情图和颜色规格图合计超过平台 50 张限制")
    return plan


def _source_title(detail: Mapping[str, Any]) -> str:
    """Backward-compatible wrapper for the shared title-field normalizer."""
    return source_title(detail)


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_fingerprint(detail: Mapping[str, Any]) -> str:
    body = {key: detail.get(key) for key in GUARDED_FIELDS}
    body["spec_pics"] = detail.get("spec_pics")
    return hashlib.sha256(_stable(body).encode("utf-8")).hexdigest()


def _read_bytes_checked(image) -> bytes:
    data = image.path.read_bytes()
    if hashlib.sha256(data).hexdigest() != image.sha256 or len(data) != image.size_bytes:
        raise SkuColorDraftError("图库图片在预检后发生变化：" + image.path.name)
    return data


def _platform_path(url: str) -> str:
    path = urlsplit(url).path if "://" in url else url
    return path.split("/obj/", 1)[-1].lstrip("/")


def verify_draft(before: Mapping[str, Any], after: Mapping[str, Any],
                 expected: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    if _product_id(after) != _product_id(before):
        errors.append("商品 ID 改变")
    for key in GUARDED_FIELDS:
        if key == "product_format_new":
            left = normalize_product_format_for_edit(parse_product_format(before.get(key)))
            right = normalize_product_format_for_edit(parse_product_format(after.get(key)))
            if left != right:
                errors.append("非目标商品属性改变")
        elif after.get(key) != before.get(key):
            errors.append(key + " 改变")
    actual: dict[str, str] = {}
    rows = after.get("spec_pics") or []
    if not isinstance(rows, list):
        errors.append("回读规格图结构异常")
    else:
        for row in rows:
            if not isinstance(row, Mapping):
                errors.append("回读规格图条目异常")
                continue
            spec_id = str(row.get("spec_detail_id") or "")
            if spec_id in actual:
                errors.append("回读规格值 ID 重复")
            actual[spec_id] = _platform_path(str(row.get("pic") or ""))
    if actual != {key: _platform_path(url) for key, url in expected.items()}:
        errors.append("规格值 ID 与图片 URL 未一一对应")
    return errors


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact SKU colour draft canary; never publish")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--execute", action="store_true", help="upload materials then save draft")
    parser.add_argument("--folder-id", default="0", help="target material folder; 0 is the shop root")
    parser.add_argument("--gallery-root", default=DEFAULT_GALLERY)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--report-root", default="panel_data/doudian_sku_color_canary")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit() or not args.folder_id.isdigit():
        parser.error("shop_id, product_id and folder_id must be numeric")

    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
    before = dict(reader.get_product_detail(product_id, show_draft=True))
    if _product_id(before) != product_id:
        raise SkuColorDraftError("商品详情 ID 与指定商品不一致")
    if before.get("status") != 0 or before.get("check_status") != 1:
        raise SkuColorDraftError("目标当前不是待处理草稿（status=0/check_status=1）；拒绝改动在售商品")
    plan = _source_plan(before, args.gallery_root)
    print(_stable({
        "shop_id": shop_id, "product_id": product_id, "status": before.get("status"),
        "color_count": plan.color_count, "colors": [
            {"name": image.color_name, "spec_detail_id": image.spec_detail_id,
             "file": str(image.path), "sha256": image.sha256, "upload_name": image.upload_name}
            for image in plan.images
        ], "execute": args.execute, "publish": False,
    }))
    if not args.execute:
        return 0

    stamp = int(time.time())
    output = Path(args.report_root) / shop_id / f"{stamp}_{product_id}"
    _write_json(output.with_suffix(".before.json"), {"captured_at": stamp, "detail": before})
    evidence: dict[str, Any] = {
        "shop_id": shop_id, "product_id": product_id, "commit": False,
        "folder_id": args.folder_id, "uploaded": [], "result": "started",
    }
    report_path = output.with_suffix(".report.json")
    _write_json(report_path, evidence)
    try:
        materials = DoudianMaterialGateway(
            args.env, shop_id, cache_root="panel_data/doudian_material_cache",
        )
        uploaded = []
        for image in plan.images:
            material = materials.upload_image(
                _read_bytes_checked(image), image.upload_name, folder_id=args.folder_id,
            )
            uploaded.append((image, material))
            evidence["uploaded"].append({
                "color": image.color_name, "spec_detail_id": image.spec_detail_id,
                "material_id": material.material_id, "sha256": material.sha256,
            })
            _write_json(report_path, evidence)

        latest = dict(reader.get_product_detail(product_id, show_draft=True))
        if _source_fingerprint(latest) != _source_fingerprint(before):
            raise SkuColorDraftError("上传期间商品已被他人修改；素材已上传，但拒绝写商品")
        expected = {image.spec_detail_id: material.byte_url for image, material in uploaded}
        payload = {
            "product_id": product_id,
            "product_format_new": _stable(normalize_product_format_for_edit(
                parse_product_format(before.get("product_format_new")))),
            "spec_pic": ",".join(material.byte_url for _, material in uploaded),
            "commit": False,
        }
        if before.get("name_prefix"):
            payload["name_prefix"] = str(before["name_prefix"])
        evidence["payload_sha256"] = hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()
        evidence["result"] = "write_started"
        _write_json(report_path, evidence)
        writer = DoudianSingleProductSpecImageDraftWriter(args.env, shop_id, product_id)
        result = writer.save_spec_images_to_draft(payload, color_count=plan.color_count)
        evidence["api_code"] = result.get("code")
        evidence["result"] = "write_accepted_readback_pending"
        _write_json(report_path, evidence)
        after = dict(reader.get_product_detail(product_id, show_draft=True))
        _write_json(output.with_suffix(".after.json"), {"captured_at": int(time.time()), "detail": after})
        errors = verify_draft(before, after, expected)
        evidence["verification_errors"] = errors
        evidence["result"] = "verified_draft" if not errors else "unverified_needs_manual_review"
        _write_json(report_path, evidence)
        print(_stable({"result": evidence["result"], "report": str(report_path),
                       "errors": errors, "publish": False}))
        return 0 if not errors else 2
    except Exception as exc:
        evidence["result"] = "failed_needs_manual_review" if evidence["result"].startswith("write_") else "failed_before_product_write"
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _write_json(report_path, evidence)
        print(_stable({"result": evidence["result"], "report": str(report_path),
                       "error": evidence["error"], "publish": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
