#!/usr/bin/env python3
"""Build a browser-free, fail-closed listing preflight report."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from app.listing_preflight import (
    DetailImageInspector,
    ImageAssetInspector,
    ListingPreflightInspector,
    PreflightReport,
)
from app.listing_preflight.inspector import PHONE_CATEGORY_ID, load_reviewed_screen_shapes
from app.listing_preflight.legacy_resolver import resolve_brand_model
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway, PhonePropertyCatalogue
from model_catalogue import ModelCatalogue


def latest_current_snapshot(root: Path, shop_id: str) -> Path:
    directory = root / shop_id
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            int(value.get("schema_version") or 0) == 2
            and int(value.get("status_filter", -1)) == 0
            and int(value.get("check_status_filter", -1)) == 1
        ):
            return path
    raise RuntimeError(f"no current draft snapshot for shop {shop_id}")


def normalized_detail(reader: DoudianProductReader, product_id: str) -> dict[str, Any]:
    detail = reader.get_product_detail(product_id, show_draft=True)
    return {
        "product_id": str(detail.get("product_id") or detail.get("product_id_str") or product_id),
        "name_prefix": str(detail.get("name_prefix") or ""),
        "product_format_new": detail.get("product_format_new"),
        "product_format": detail.get("product_format"),
        "draft_status": detail.get("draft_status"),
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "update_time": detail.get("update_time"),
    }


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def select_snapshot_items(
    snapshot: Mapping[str, Any], requested_ids: list[str]
) -> list[Mapping[str, Any]]:
    items = [row for row in (snapshot.get("items") or []) if isinstance(row, Mapping)]
    if not requested_ids:
        return items
    if len(requested_ids) != len(set(requested_ids)) or any(
        not value.isdigit() for value in requested_ids
    ):
        raise RuntimeError("product ids must be unique numeric values")
    by_id = {str(row.get("product_id") or ""): row for row in items}
    missing = [value for value in requested_ids if value not in by_id]
    if missing:
        raise RuntimeError("products absent from current draft snapshot: " + ",".join(missing))
    return [by_id[value] for value in requested_ids]


def main() -> int:
    parser = argparse.ArgumentParser(description="DouDian listing preflight")
    parser.add_argument("shop_id")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--snapshot-root", default="panel_data/doudian_draft_snapshots")
    parser.add_argument("--output-root", default="panel_data/listing_preflight")
    parser.add_argument("--image-root", default="/Users/atrenew/商品图片")
    parser.add_argument(
        "--detail-image-root",
        default=os.environ.get("DP_DETAIL_IMAGE_DIR", "/Users/atrenew/手机商详"),
    )
    parser.add_argument("--standard-models", default="panel_data/gaia_excel_phone_models.json")
    parser.add_argument("--platform-models", default="panel_data/doudian_phone_models.json")
    parser.add_argument("--model-properties", default="config/phone_model_properties.json")
    parser.add_argument("--no-white-as-main", action="store_true")
    parser.add_argument(
        "--product-id", action="append", default=[],
        help="only inspect this product from the current full snapshot (repeatable)",
    )
    args = parser.parse_args()
    shop_id = str(args.shop_id).strip()
    if not shop_id.isdigit():
        raise RuntimeError("shop id must be numeric")

    snapshot_path = latest_current_snapshot(Path(args.snapshot_root), shop_id)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    selected_items = select_snapshot_items(snapshot, args.product_id)
    reader = DoudianProductReader(DoudianReadOnlyGateway(Path(args.env), shop_id))
    details: dict[str, Mapping[str, Any]] = {}
    for item in selected_items:
        if str(item.get("category_leaf_id") or "") == PHONE_CATEGORY_ID:
            product_id = str(item.get("product_id") or "")
            details[product_id] = normalized_detail(reader, product_id)

    inspector = ListingPreflightInspector(
        resolver=resolve_brand_model,
        standard_catalogue=ModelCatalogue.from_json(args.standard_models),
        platform_catalogue=ModelCatalogue.from_json(args.platform_models),
        property_catalogue=PhonePropertyCatalogue.from_file(),
        image_inspector=ImageAssetInspector(
            args.image_root, shop_id, allow_white_as_main=not args.no_white_as_main,
        ),
        detail_image_inspector=DetailImageInspector(args.detail_image_root),
        reviewed_screen_shapes=load_reviewed_screen_shapes(args.model_properties),
    )
    decisions = tuple(
        inspector.inspect(item, details.get(str(item.get("product_id") or ""), {}))
        for item in selected_items
    )
    report = PreflightReport(
        schema_version=1,
        shop_id=shop_id,
        captured_at=int(time.time()),
        source_snapshot=str(snapshot_path.resolve()),
        ready=sum(item.decision == "ready" for item in decisions),
        manual_required=sum(item.decision == "manual_required" for item in decisions),
        out_of_scope=sum(item.decision == "out_of_scope" for item in decisions),
        decisions=decisions,
    )
    output = Path(args.output_root) / shop_id / f"{report.captured_at}.json"
    atomic_write(output, report.to_dict())
    print(
        f"PREFLIGHT_OK shop={shop_id} total={len(decisions)} ready={report.ready} "
        f"manual={report.manual_required} out_of_scope={report.out_of_scope} "
        f"file={output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
