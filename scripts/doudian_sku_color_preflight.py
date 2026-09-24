"""Read-only SKU colour image audit for one explicit DouDian product.

No material upload, editV2 call, browser click or publication occurs here.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.listing_preflight.legacy_resolver import resolve_brand_model
from app.sku_color_images import ColorGalleryError, resolve_color_images
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway


DEFAULT_GALLERY = "/Users/atrenew/商品图片/店铺SKU颜色图"


def preflight(detail: dict, gallery_root: str) -> dict:
    title = " ".join(part for part in (
        str(detail.get("name_prefix") or "").strip(),
        str(detail.get("name") or "").strip(),
    ) if part)
    parsed = dict(resolve_brand_model(title))
    brand = str(parsed.get("target_brand_cn") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not brand or not model:
        raise ColorGalleryError("商品标题未能唯一解析品牌和型号")
    plan = resolve_color_images(gallery_root, brand, model, detail)
    matched_all = len(plan.images) == plan.color_count and not plan.missing and not plan.invalid
    return {
        "shop_agnostic_gallery": True,
        "product_id": str(detail.get("product_id_str") or detail.get("product_id") or ""),
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "draft_status": detail.get("draft_status"),
        "existing_spec_pics_count": len(detail.get("spec_pics") or []),
        "sku_count": len(detail.get("spec_prices") or []),
        "title": title,
        "brand": brand,
        "model": model,
        "gallery_folder": str(plan.model_directory),
        "color_count": plan.color_count,
        "matched": [
            {"color": image.color_name, "spec_detail_id": image.spec_detail_id,
             "file": image.path.name, "sha256": image.sha256,
             "upload_name": image.upload_name, "format_warning": image.format_warning,
             "size_bytes": image.size_bytes, "dimensions": [image.width, image.height]}
            for image in plan.images
        ],
        "missing": list(plan.missing),
        "invalid": [{"color": name, "reason": reason} for name, reason in plan.invalid],
        "ready_for_material_upload": matched_all,
        "ready_for_draft_write": matched_all and detail.get("status") == 0
        and detail.get("check_status") == 1 and not detail.get("spec_pics"),
        "product_write_verified": False,
        "uploaded": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only exact SKU colour gallery check")
    parser.add_argument("shop_id", nargs="?", help="target shop ID for live API read")
    parser.add_argument("product_id", nargs="?", help="target product ID for live API read")
    parser.add_argument("--snapshot-file", help="offline product-detail snapshot with a detail object")
    parser.add_argument("--gallery-root", default=DEFAULT_GALLERY)
    parser.add_argument("--env", default="env.local")
    args = parser.parse_args()
    if args.snapshot_file:
        if args.shop_id or args.product_id:
            parser.error("--snapshot-file cannot be combined with live IDs")
        payload = json.loads(Path(args.snapshot_file).read_text(encoding="utf-8"))
        detail = payload.get("detail", payload)
        if not isinstance(detail, dict):
            parser.error("snapshot has no product detail object")
    else:
        if not args.shop_id or not args.product_id:
            parser.error("live read requires shop_id and product_id")
        reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, args.shop_id))
        detail = dict(reader.get_product_detail(args.product_id, show_draft=True))
        returned_id = str(detail.get("product_id_str") or detail.get("product_id") or "")
        if returned_id != args.product_id:
            raise ColorGalleryError("商品详情 ID 与请求不一致，拒绝匹配")
    try:
        result = preflight(detail, args.gallery_root)
    except ColorGalleryError as exc:
        result = {"ready_for_material_upload": False, "uploaded": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_material_upload"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
