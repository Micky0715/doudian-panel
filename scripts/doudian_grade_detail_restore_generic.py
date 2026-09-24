"""One-product correction: retain the original detail URLs and insert grade URLs.

This script exists for the 2026-09-14 canary only. It never publishes and
refuses to write if the product changed since the recorded canary readback.
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
from integrations.doudian.write_gateway import DoudianSingleProductGradeDetailDraftWriter
from scripts.doudian_apply_local_images import detail_urls
from scripts.doudian_grade_detail_draft_once import _spec_material_urls, verify_grade_detail
from scripts.doudian_sku_color_draft_once import (
    _platform_path, _product_id, _source_fingerprint, _stable, _write_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore original generic detail URLs on one draft")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--canary-stem", required=True)
    parser.add_argument("--sku-color-report", required=True)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id), str(args.product_id)
    if not shop_id.isdigit() or not product_id.isdigit():
        parser.error("shop_id and product_id must be numeric")
    stem = Path(args.canary_stem)
    original = json.loads(stem.with_suffix(".before.json").read_text(encoding="utf-8"))["detail"]
    canary_after = json.loads(stem.with_suffix(".after.json").read_text(encoding="utf-8"))["detail"]
    canary_report = json.loads(stem.with_suffix(".report.json").read_text(encoding="utf-8"))
    color_report = json.loads(Path(args.sku_color_report).read_text(encoding="utf-8"))
    if (canary_report.get("shop_id") != shop_id or canary_report.get("product_id") != product_id
            or canary_report.get("result") != "verified_draft" or canary_report.get("commit") is not False
            or canary_report.get("grades") != ["99新", "95新"]):
        raise ValueError("原始验收报告与本次修正目标不一致")
    if _product_id(original) != product_id or _product_id(canary_after) != product_id:
        raise ValueError("历史快照商品 ID 不一致")
    old_urls = detail_urls(original.get("description"))
    canary_urls = detail_urls(canary_after.get("description"))
    if len(old_urls) != 13 or len(canary_urls) != 15:
        raise ValueError("历史详情图数量不符合本次修正条件")
    grade_urls = canary_urls[2:4]
    if len({_platform_path(url) for url in old_urls + grade_urls}) != 15:
        raise ValueError("原始通用图与成色图发生重复")
    target_urls = old_urls[:2] + grade_urls + old_urls[2:]

    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
    current = dict(reader.get_product_detail(product_id, show_draft=True))
    if (_source_fingerprint(current) != _source_fingerprint(canary_after)
            or current.get("spec_pics") != canary_after.get("spec_pics")):
        raise ValueError("当前商品与验收后快照不同，拒绝覆盖")
    if current.get("status") != 0 or current.get("check_status") != 1:
        raise ValueError("商品已不是未发布草稿")
    materials = DoudianMaterialGateway(args.env, shop_id, cache_root="panel_data/doudian_material_cache")
    uploaded = canary_report.get("uploaded") or []
    for index, grade in enumerate(("99新", "95新")):
        row = uploaded[index + 2]
        if row.get("slot") != grade:
            raise ValueError("验收报告中的成色图位置不一致")
        audited = materials.get_material_byte_url(str(row.get("material_id")))
        if _platform_path(audited) != _platform_path(grade_urls[index]):
            raise ValueError("成色图与已审核素材 URL 不一致")
    spec_urls = _spec_material_urls(shop_id, product_id, current, color_report, materials)
    print(_stable({"shop_id": shop_id, "product_id": product_id, "original_generic_count": len(old_urls),
                   "grade_count": len(grade_urls), "target_count": len(target_urls),
                   "execute": args.execute, "publish": False}))
    if not args.execute:
        return 0

    report_path = stem.with_suffix(".restore-generic.json")
    report = {"shop_id": shop_id, "product_id": product_id, "commit": False,
              "original_generic_count": 13, "grades": ["99新", "95新"], "result": "write_started"}
    _write_json(report_path, report)
    payload = {
        "product_id": product_id,
        "product_format_new": _stable(normalize_product_format_for_edit(
            parse_product_format(current.get("product_format_new")))),
        "description": "|".join(target_urls),
        "spec_pic": ",".join(spec_urls),
        "name_prefix": current["name_prefix"],
        "commit": False,
    }
    report["payload_sha256"] = hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()
    _write_json(report_path, report)
    try:
        writer = DoudianSingleProductGradeDetailDraftWriter(args.env, shop_id, product_id)
        result = writer.save_grade_details_to_draft(payload, detail_count=15, color_count=len(spec_urls))
        report["api_code"] = result.get("code")
        report["result"] = "write_accepted_readback_pending"
        _write_json(report_path, report)
        final = dict(reader.get_product_detail(product_id, show_draft=True))
        _write_json(stem.with_suffix(".restored-after.json"),
                    {"captured_at": int(time.time()), "detail": final})
        issues = verify_grade_detail(original, final, target_urls)
        report["verification_errors"] = issues
        report["result"] = "verified_restored" if not issues else "unverified_needs_manual_review"
        _write_json(report_path, report)
        print(_stable({"result": report["result"], "errors": issues,
                       "report": str(report_path), "publish": False}))
        return 0 if not issues else 2
    except Exception as exc:
        report["result"] = "failed_needs_manual_review"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _write_json(report_path, report)
        print(_stable({"result": report["result"], "error": report["error"],
                       "report": str(report_path), "publish": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
