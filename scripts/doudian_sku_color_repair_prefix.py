"""Restore a canary draft's observed title prefix loss without re-uploading.

Only valid when the original canary report has exactly one verification error:
``name_prefix 改变``. This script never publishes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.doudian.write_gateway import DoudianSingleProductSpecImageDraftWriter
from scripts.doudian_sku_color_draft_once import _product_id, _stable, _write_json, verify_draft


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair only observed SKU canary name_prefix loss")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--canary-stem", required=True,
                        help="path before .before.json/.report.json from the canary run")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env", default="env.local")
    args = parser.parse_args()
    if not args.shop_id.isdigit() or not args.product_id.isdigit():
        parser.error("shop_id and product_id must be numeric")
    stem = Path(args.canary_stem)
    before = json.loads(stem.with_suffix(".before.json").read_text(encoding="utf-8"))["detail"]
    report = json.loads(stem.with_suffix(".report.json").read_text(encoding="utf-8"))
    if (_product_id(before) != args.product_id or report.get("shop_id") != args.shop_id
            or report.get("product_id") != args.product_id
            or report.get("verification_errors") != ["name_prefix 改变"]):
        raise RuntimeError("快照、报告或唯一异常不符合前缀修复条件")
    prefix = str(before.get("name_prefix") or "")
    if not prefix.strip():
        raise RuntimeError("原始商品没有可恢复的前缀")
    rows = report.get("uploaded") or []
    if not isinstance(rows, list) or not rows or any(
        not isinstance(row, dict) or not str(row.get("material_id") or "").isdigit()
        for row in rows
    ):
        raise RuntimeError("原验收报告缺少完整素材 ID")

    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, args.shop_id))
    current = dict(reader.get_product_detail(args.product_id, show_draft=True))
    if _product_id(current) != args.product_id:
        raise RuntimeError("当前商品 ID 不一致")
    materials = DoudianMaterialGateway(args.env, args.shop_id)
    urls = [materials.get_material_byte_url(str(row["material_id"])) for row in rows]
    if any(not url for url in urls):
        raise RuntimeError("已有素材无法回读有效图片 URL")
    expected = {str(row["spec_detail_id"]): url for row, url in zip(rows, urls)}
    if verify_draft(before, current, expected) != ["name_prefix 改变"]:
        raise RuntimeError("当前商品状态发生新变化，拒绝盲目修复")
    payload = {
        "product_id": args.product_id,
        "product_format_new": _stable(normalize_product_format_for_edit(
            parse_product_format(current.get("product_format_new")))),
        "spec_pic": ",".join(urls),
        "name_prefix": prefix,
        "commit": False,
    }
    digest = hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()
    print(_stable({"shop_id": args.shop_id, "product_id": args.product_id,
                   "restore_name_prefix": prefix, "color_count": len(rows),
                   "payload_sha256": digest, "execute": args.execute, "publish": False}))
    if not args.execute:
        return 0
    repair_path = stem.with_suffix(".repair.json")
    evidence = {"created_at": int(time.time()), "shop_id": args.shop_id,
                "product_id": args.product_id, "commit": False, "result": "write_started",
                "payload_sha256": digest}
    _write_json(repair_path, evidence)
    writer = DoudianSingleProductSpecImageDraftWriter(args.env, args.shop_id, args.product_id)
    result = writer.save_spec_images_to_draft(payload, color_count=len(rows))
    evidence["api_code"] = result.get("code")
    evidence["result"] = "write_accepted_readback_pending"
    _write_json(repair_path, evidence)
    after = dict(reader.get_product_detail(args.product_id, show_draft=True))
    errors = verify_draft(before, after, expected)
    evidence["verification_errors"] = errors
    evidence["result"] = "verified_restored" if not errors else "unverified_needs_manual_review"
    _write_json(repair_path, evidence)
    print(_stable({"result": evidence["result"], "errors": errors,
                   "report": str(repair_path), "publish": False}))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
