"""One draft canary: insert actual 成色 images into approved existing details.

Only required individual grade files are uploaded. A three-grade product uses
the existing combined grade image; one- or two-grade products remove that
generic image and insert exact local grade files. Explicitly blocked legacy
assets are always removed. Never publishes a product.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from app.grade_detail_images import build_grade_only_plan
from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.doudian.write_gateway import DoudianSingleProductGradeDetailDraftWriter
from scripts.doudian_apply_local_images import detail_urls
from scripts.doudian_sku_color_draft_once import (
    GUARDED_FIELDS, _platform_path, _product_id, _source_fingerprint, _stable, _write_json,
)


class GradeDetailCanaryError(RuntimeError):
    pass


BLOCKED_DETAIL_MATERIAL_MARKERS = frozenset({
    # Legacy "加购套餐说明" table. It was inherited from upstream drafts and
    # must not be carried into any newly prepared or published description.
    "1c7ee38c4ce1e75da8cd70eae3ed619f",
})

COMBINED_GRADE_DETAIL_MATERIAL_MARKERS = frozenset({
    # Existing generic "成色说明" image containing 99新 / 95新 / 9新.
    "d8ba5de976b518f71e4f7621bd9baa58",
})


def filter_blocked_detail_urls(original_urls: list[str]) -> tuple[list[str], list[str]]:
    """Return retained and blocked URLs without changing retained ordering."""
    retained: list[str] = []
    blocked: list[str] = []
    for url in original_urls:
        target = blocked if any(marker in _platform_path(url)
                                for marker in BLOCKED_DETAIL_MATERIAL_MARKERS) else retained
        target.append(url)
    return retained, blocked


def validate_existing_detail_urls(original_urls: list[str]) -> None:
    """Validate a generic existing detail gallery before uploading anything.

    The full-media workflow used to accept only the two historically observed
    gallery sizes (6 and 13).  Gallery length is not an identity signal,
    though: safety comes from preserving every unknown URL, removing only
    explicitly audited legacy assets, and verifying the exact readback.  Keep
    a bounded 2..18 input so one or two grade images can still fit within the
    platform's 20-image description limit.
    """
    if not 2 <= len(original_urls) <= 18:
        raise GradeDetailCanaryError("原详情图必须为 2 至 18 张")
    if any(not url.startswith(("https://", "http://")) for url in original_urls):
        raise GradeDetailCanaryError("原详情图 URL 无效")
    paths = [_platform_path(url) for url in original_urls]
    if any(not path for path in paths) or len(set(paths)) != len(paths):
        raise GradeDetailCanaryError("原详情图 URL 重复或无法规范化")


def select_existing_detail_urls(original_urls: list[str], *,
                                use_combined_grade_image: bool) -> tuple[list[str], list[str]]:
    """Filter legacy assets and apply the combined-grade-image policy."""
    retained, excluded = filter_blocked_detail_urls(original_urls)
    combined = [url for url in retained if any(
        marker in _platform_path(url) for marker in COMBINED_GRADE_DETAIL_MATERIAL_MARKERS
    )]
    if use_combined_grade_image:
        if len(combined) != 1:
            raise GradeDetailCanaryError("三种成色商品未找到唯一的合并成色说明图")
    else:
        retained = [url for url in retained if url not in combined]
        excluded.extend(combined)
    return retained, excluded


def compose_detail_urls(original_urls: list[str], grade_urls: list[str], *,
                        use_combined_grade_image: bool = False,
                        no_grade_image: bool = False) -> list[str]:
    """Select the combined grade image or insert exact individual grade files."""
    if no_grade_image and use_combined_grade_image:
        raise GradeDetailCanaryError("准新品不能使用合并成色说明图")
    valid_grade_count = (len(grade_urls) == 0 if use_combined_grade_image or no_grade_image
                         else 1 <= len(grade_urls) <= 2)
    if not valid_grade_count:
        raise GradeDetailCanaryError("详情原图或成色图数量不符合首次验收边界")
    validate_existing_detail_urls(original_urls)
    retained, _ = select_existing_detail_urls(
        original_urls, use_combined_grade_image=use_combined_grade_image,
    )
    if len(retained) < 2:
        raise GradeDetailCanaryError("过滤禁用详情图后少于 2 张原始详情图")
    grade_paths = [_platform_path(url) for url in grade_urls]
    if len(set(grade_paths)) != len(grade_paths):
        raise GradeDetailCanaryError("待补成色图 URL 重复")
    retained_paths = {_platform_path(url) for url in retained}
    # Older drafts may already contain one of the exact individual grade
    # images. Preserve it in place and insert only the missing grade images.
    new_grade_urls = [url for url, path in zip(grade_urls, grade_paths)
                      if path not in retained_paths]
    urls = retained[:2] + new_grade_urls + retained[2:]
    if len(urls) > 20:
        raise GradeDetailCanaryError("合并后的详情图超过 20 张")
    if (any(not url.startswith(("https://", "http://")) for url in urls)
            or len({_platform_path(url) for url in urls}) != len(urls)):
        raise GradeDetailCanaryError("详情图 URL 无效或重复")
    return urls


def _spec_material_urls(shop_id: str, product_id: str, current: Mapping[str, Any],
                        color_report: Mapping[str, Any], materials: DoudianMaterialGateway) -> list[str]:
    if (color_report.get("shop_id") != shop_id or color_report.get("product_id") != product_id
            or color_report.get("commit") is not False):
        raise GradeDetailCanaryError("颜色图报告的店铺、商品或提交模式不一致")
    rows = color_report.get("uploaded") or []
    if not isinstance(rows, list) or not rows:
        raise GradeDetailCanaryError("颜色图报告没有素材 ID")
    current_rows = current.get("spec_pics") or []
    if not isinstance(current_rows, list) or len(current_rows) != len(rows):
        raise GradeDetailCanaryError("当前颜色规格图数量与前次验收不一致")
    current_map = {str(row.get("spec_detail_id")): _platform_path(str(row.get("pic") or ""))
                   for row in current_rows if isinstance(row, Mapping)}
    urls: list[str] = []
    for row in rows:
        material_id = str(row.get("material_id") or "")
        spec_id = str(row.get("spec_detail_id") or "")
        if not material_id.isdigit() or not spec_id.isdigit():
            raise GradeDetailCanaryError("颜色图报告含无效素材或规格值 ID")
        url = materials.get_material_byte_url(material_id)
        if not url or current_map.get(spec_id) != _platform_path(url):
            raise GradeDetailCanaryError("当前颜色图与已审核素材 URL 不一致")
        urls.append(url)
    if len(current_map) != len(rows):
        raise GradeDetailCanaryError("当前规格图存在重复或多余颜色")
    return urls


def verify_grade_detail(before: Mapping[str, Any], after: Mapping[str, Any],
                        expected_urls: list[str]) -> list[str]:
    issues: list[str] = []
    if _product_id(before) != _product_id(after):
        issues.append("商品 ID 改变")
    for key in GUARDED_FIELDS:
        if key == "description":
            continue
        if key == "product_format_new":
            left = normalize_product_format_for_edit(parse_product_format(before.get(key)))
            right = normalize_product_format_for_edit(parse_product_format(after.get(key)))
            if left != right:
                issues.append("非目标商品属性改变")
        elif before.get(key) != after.get(key):
            issues.append(key + " 改变")
    if before.get("spec_pics") != after.get("spec_pics"):
        issues.append("颜色规格图改变")
    actual_urls = detail_urls(after.get("description"))
    if [_platform_path(url) for url in actual_urls] != [_platform_path(url) for url in expected_urls]:
        issues.append("详情图数量、顺序或 URL 与目标不一致")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload exact grade details to one draft; never publish")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--sku-color-report", required=True)
    parser.add_argument("--detail-root", default="/Users/atrenew/手机商详")
    parser.add_argument("--folder-id", default="0")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--report-root", default="panel_data/doudian_grade_detail_canary")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id), str(args.product_id)
    if not shop_id.isdigit() or not product_id.isdigit() or not args.folder_id.isdigit():
        parser.error("shop_id, product_id and folder_id must be numeric")
    color_report = json.loads(Path(args.sku_color_report).read_text(encoding="utf-8"))
    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
    before = dict(reader.get_product_detail(product_id, show_draft=True))
    if _product_id(before) != product_id or before.get("status") != 0 or before.get("check_status") != 1:
        raise GradeDetailCanaryError("目标已不是待处理草稿，拒绝写入")
    if not isinstance(before.get("name_prefix"), str) or not before["name_prefix"].strip():
        raise GradeDetailCanaryError("标题前缀为空，当前验收需先保留非空前缀")
    plan = build_grade_only_plan(before, args.detail_root)
    use_combined = plan.strategy == "combined_existing"
    original_urls = detail_urls(before.get("description"))
    if len(original_urls) != 13 or any(not url.startswith(("https://", "http://")) for url in original_urls):
        raise GradeDetailCanaryError("首次成色图验收只接受现有 13 张有效详情图的草稿；拒绝覆盖或重复插入")
    if len({_platform_path(url) for url in original_urls}) != len(original_urls):
        raise GradeDetailCanaryError("现有详情图 URL 有重复，需人工核对")
    main_count = len(before.get("pic") or [])
    color_count = len(before.get("spec_pics") or [])
    retained_original_urls, excluded_urls = select_existing_detail_urls(
        original_urls, use_combined_grade_image=use_combined,
    )
    target_count = len(retained_original_urls) + len(plan.images)
    if main_count + color_count + target_count > 50:
        raise GradeDetailCanaryError("主图、规格图和详情图合计超过 50 张")
    materials = DoudianMaterialGateway(
        args.env, shop_id, cache_root="panel_data/doudian_material_cache",
    )
    spec_urls = _spec_material_urls(shop_id, product_id, before, color_report, materials)
    print(_stable({"shop_id": shop_id, "product_id": product_id,
                   "grades": plan.grades, "grade_slots": [image.slot for image in plan.images],
                   "grade_image_strategy": plan.strategy,
                   "existing_detail_count": len(original_urls),
                   "retained_existing_detail_count": len(retained_original_urls),
                   "excluded_detail_urls": excluded_urls,
                   "new_detail_count": target_count,
                   "execute": args.execute,
                   "publish": False}))
    if not args.execute:
        return 0

    stamp = int(time.time())
    stem = Path(args.report_root) / shop_id / f"{stamp}_{product_id}"
    report_path = stem.with_suffix(".report.json")
    _write_json(stem.with_suffix(".before.json"), {"captured_at": stamp, "detail": before})
    evidence: dict[str, Any] = {
        "shop_id": shop_id, "product_id": product_id, "grades": list(plan.grades),
        "grade_image_strategy": plan.strategy,
        "slots": [image.slot for image in plan.images], "commit": False,
        "original_detail_count": len(original_urls),
        "retained_original_detail_count": len(retained_original_urls),
        "excluded_detail_urls": excluded_urls,
        "uploaded": [], "result": "started",
    }
    _write_json(report_path, evidence)
    try:
        uploaded = []
        for image in plan.images:
            data = image.path.read_bytes()
            if len(data) != image.size_bytes or hashlib.sha256(data).hexdigest() != image.sha256:
                raise GradeDetailCanaryError("详情图库在预检后发生变化：" + image.slot)
            material = materials.upload_image(data, image.upload_name, folder_id=args.folder_id)
            uploaded.append(material)
            evidence["uploaded"].append({
                "slot": image.slot, "material_id": material.material_id,
                "sha256": material.sha256,
            })
            _write_json(report_path, evidence)
        latest = dict(reader.get_product_detail(product_id, show_draft=True))
        if _source_fingerprint(latest) != _source_fingerprint(before):
            raise GradeDetailCanaryError("上传期间商品发生变化；素材已上传，拒绝写商品")
        new_urls = compose_detail_urls(
            original_urls, [material.byte_url for material in uploaded],
            use_combined_grade_image=use_combined,
            no_grade_image=plan.strategy == "no_grade_image",
        )
        payload = {
            "product_id": product_id,
            "product_format_new": _stable(normalize_product_format_for_edit(
                parse_product_format(before.get("product_format_new")))),
            "description": "|".join(new_urls),
            "spec_pic": ",".join(spec_urls),
            "name_prefix": before["name_prefix"],
            "commit": False,
        }
        evidence["payload_sha256"] = hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()
        evidence["result"] = "write_started"
        _write_json(report_path, evidence)
        writer = DoudianSingleProductGradeDetailDraftWriter(args.env, shop_id, product_id)
        result = writer.save_grade_details_to_draft(
            payload, detail_count=len(new_urls), color_count=len(spec_urls),
        )
        evidence["api_code"] = result.get("code")
        evidence["result"] = "write_accepted_readback_pending"
        _write_json(report_path, evidence)
        after = dict(reader.get_product_detail(product_id, show_draft=True))
        _write_json(stem.with_suffix(".after.json"), {"captured_at": int(time.time()), "detail": after})
        issues = verify_grade_detail(before, after, new_urls)
        evidence["verification_errors"] = issues
        evidence["result"] = "verified_draft" if not issues else "unverified_needs_manual_review"
        _write_json(report_path, evidence)
        print(_stable({"result": evidence["result"], "errors": issues,
                       "report": str(report_path), "publish": False}))
        return 0 if not issues else 2
    except Exception as exc:
        evidence["result"] = "failed_needs_manual_review" if evidence["result"].startswith("write_") else "failed_before_product_write"
        evidence["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _write_json(report_path, evidence)
        print(_stable({"result": evidence["result"], "error": evidence["error"],
                       "report": str(report_path), "publish": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
