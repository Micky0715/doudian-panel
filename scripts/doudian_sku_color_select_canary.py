"""Read-only selection of one current draft with complete exact SKU colour images."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from scripts.doudian_listing_preflight import latest_current_snapshot
from scripts.doudian_sku_color_preflight import DEFAULT_GALLERY, preflight


def main() -> int:
    parser = argparse.ArgumentParser(description="Find one exact SKU colour draft canary")
    parser.add_argument("shop_id")
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--gallery-root", default=DEFAULT_GALLERY)
    parser.add_argument("--snapshot-root", default="panel_data/doudian_draft_snapshots")
    args = parser.parse_args()
    if not args.shop_id.isdigit() or not 1 <= args.limit <= 100:
        parser.error("shop_id must be numeric and limit must be 1..100")
    source = latest_current_snapshot(Path(args.snapshot_root), args.shop_id)
    snapshot = json.loads(source.read_text(encoding="utf-8"))
    candidates = [row for row in snapshot.get("items", [])
                  if isinstance(row, dict) and str(row.get("category_leaf_id")) == "1000007628"]
    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, args.shop_id))
    for row in candidates[:args.limit]:
        product_id = str(row.get("product_id") or "")
        if not product_id.isdigit():
            continue
        try:
            detail = reader.get_product_detail(product_id, show_draft=True)
            if str(detail.get("product_id_str") or detail.get("product_id") or "") != product_id:
                raise ValueError("回读商品 ID 不一致")
            result = preflight(detail, args.gallery_root)
            summary = {
                "product_id": product_id, "title": result["title"],
                "status": result["status"], "check_status": result["check_status"],
                "color_count": result["color_count"], "matched": len(result["matched"]),
                "missing": result["missing"], "invalid": result["invalid"],
                "existing_spec_pics_count": result["existing_spec_pics_count"],
                "ready_for_draft_write": result["ready_for_draft_write"],
            }
        except Exception as exc:
            summary = {"product_id": product_id, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        print(json.dumps(summary, ensure_ascii=False))
        if summary.get("ready_for_draft_write"):
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
