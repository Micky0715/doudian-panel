"""Apply one exact Feishu image set to one existing DouDian draft.

This is the migration entry point for images. It is deliberately one product
at a time, saves a draft only, and verifies that the *first* product image is
the approved real main or explicitly recorded white-image fallback before
reporting success. It never submits the product and never starts the browser
fallback by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from app.material_pipeline import (
    CloudMaterialExecutor,
    FeishuMaterialCatalog,
    FeishuMaterialLayout,
    build_material_plan,
)
from app.material_pipeline.cache import MaterialMetadataCache
from doudian_api import read_env_file
from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.doudian.write_gateway import DoudianSingleProductImageDraftWriter
from integrations.feishu_drive import FeishuDriveClient, FeishuDriveConfig


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


def _format_for_edit(detail: Mapping[str, Any]) -> str:
    return json.dumps(
        normalize_product_format_for_edit(parse_product_format(detail["product_format_new"])),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _pic_urls(value: Any) -> list[str]:
    """Normalise the read API's known string/list forms without guessing."""
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


def _verify_readback(detail: Mapping[str, Any], expected_pic: str) -> list[str]:
    actual = _pic_urls(detail.get("pic"))
    expected = _pic_urls(expected_pic)
    issues: list[str] = []
    if not expected or len(expected) < 4:
        return ["expected_api_pic_set_invalid"]
    if not actual:
        return ["product_pic_missing_after_write"]
    if actual[0] != expected[0]:
        issues.append("main_image_does_not_match_planned_first_image")
    if actual[:len(expected)] != expected:
        issues.append("product_image_order_or_count_does_not_match_uploaded_set")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Save one Feishu image set to one DouDian draft through API")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("brand")
    parser.add_argument("model")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--folder-id", required=True,
                        help="explicit DouDian material-centre folder ID; never inferred")
    parser.add_argument("--cache-root", default="panel_data/material_metadata_cache")
    parser.add_argument("--report-root", default="panel_data/feishu_image_migration_reports")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit():
        raise SystemExit("shop_id and product_id must be numeric")

    report: dict[str, Any] = {
        "schema_version": 1, "created_at": int(time.time()), "shop_id": shop_id,
        "product_id": product_id, "brand": args.brand.strip(), "model": args.model.strip(),
        "mode": "execute" if args.execute else "dry_run", "operation": "product.editV2(commit=false,pic)",
        "browser_fallback": "manual_confirmation_required", "result": "blocked",
    }
    report_path = Path(args.report_root) / shop_id / f"{report['created_at']}_{product_id}.json"
    try:
        values = read_env_file(args.env)
        folder_id = str(args.folder_id).strip()
        if not folder_id.isdigit():
            raise ValueError("folder_id must be numeric")
        drive = FeishuDriveClient(FeishuDriveConfig.from_mapping(values))
        bundle = FeishuMaterialCatalog(
            drive, layout=FeishuMaterialLayout.from_mapping(values),
        ).resolve_bundle(shop_id, args.brand, args.model)
        plan = build_material_plan(bundle)
        report["source_plan"] = plan.as_dict()
        if not args.execute:
            report["result"] = "source_verified_dry_run"
        else:
            reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
            before = dict(reader.get_product_detail(product_id, show_draft=True))
            if before.get("status") != 0 or before.get("check_status") != 1:
                raise ValueError("product is not a pending draft")
            old_pic = before.get("pic")
            if not isinstance(old_pic, str) or not old_pic.strip():
                raise ValueError("rollback requires existing string pic evidence")
            executor = CloudMaterialExecutor(
                drive, DoudianMaterialGateway(args.env, shop_id),
                MaterialMetadataCache(args.cache_root), shop_id=shop_id, folder_id=folder_id,
            )
            resolved = executor.resolve(plan)
            product_format_new = _format_for_edit(before)
            writer = DoudianSingleProductImageDraftWriter(args.env, shop_id, product_id)
            response = executor.save_draft_images(writer, product_id, product_format_new, resolved)
            after = dict(reader.get_product_detail(product_id, show_draft=True))
            issues = _verify_readback(after, resolved.pic)
            report.update({
                "write_code": response.get("code"), "before_pic": _pic_urls(old_pic),
                "expected_pic": _pic_urls(resolved.pic), "after_pic": _pic_urls(after.get("pic")),
                "main_substituted": plan.main_substituted,
                "omitted_white_images": [
                    {"name": item.name, "reason": item.reason}
                    for item in plan.omitted_white_images
                ],
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
    return 0 if report["result"] in {"source_verified_dry_run", "verified_draft_saved"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
