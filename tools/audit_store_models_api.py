#!/usr/bin/env python3
"""Read-only full-store model audit using DouDian's official product/model data."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doudian_api import (  # noqa: E402
    DoudianClient, DoudianConfig, MODEL_PROPERTY_ID, find_property,
    resolve_model_option,
)
from test_doudian_batch import parse_title  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=0,
                        help="Audit only the first N products (use the UI draft count).")
    parser.add_argument("--category", default="",
                        help="Keep products whose category path contains this text.")
    args = parser.parse_args()

    client = DoudianClient(DoudianConfig.from_mapping(os.environ), args.shop_id)
    prop = find_property(client.get_category_properties(), MODEL_PROPERTY_ID) or {}
    drafts = []
    page = 1
    while True:
        last_error = None
        for attempt in range(4):
            try:
                result = client.list_products(page=page, size=100)
                break
            except Exception as exc:
                last_error = exc
                if attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))
        rows = result.get("data", [])
        if not isinstance(rows, list) or not rows:
            break
        drafts.extend(row for row in rows if int(row.get("status", -1)) == 0)
        if args.limit and len(drafts) >= args.limit:
            drafts = drafts[:args.limit]
            break
        if page * 100 >= int(result.get("total", 0)):
            break
        page += 1

    results = []
    for row in drafts:
        category = row.get("category_detail") or {}
        category_path = " > ".join(str(category.get(key) or "") for key in (
            "first_cname", "second_cname", "third_cname", "fourth_cname"
        )).strip(" >")
        if args.category and args.category not in category_path:
            continue
        title = str(row.get("name") or "")
        parsed = parse_title(title)
        model = str(parsed.get("model") or "").strip()
        brand_cn = str(parsed.get("brand_cn") or "").strip()
        brand_norm = str(parsed.get("brand_normalized") or "")
        brand_en = brand_norm.split("/")[0] if brand_norm else ""
        hit = resolve_model_option(
            prop, model, brand_names=tuple(x for x in (brand_cn, brand_en) if x)
        ) if model else None
        results.append({
            "product_id": str(row.get("product_id") or ""),
            "title": title,
            "category": category_path,
            "brand": brand_cn,
            "model": model,
            "official_name": str(hit.get("name") or "") if hit else "",
            "official_value": str(hit.get("value") or "") if hit else "",
            "matched": bool(hit),
        })

    matched = [item for item in results if item["matched"]]
    unmatched = [item for item in results if not item["matched"]]
    payload = {
        "shop_id": args.shop_id,
        "draft_count": len(results),
        "matched_count": len(matched),
        "unmatched_count": len(unmatched),
        "match_rate": round(len(matched) / len(results), 4) if results else 0,
        "unmatched": unmatched,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in (
        "shop_id", "draft_count", "matched_count", "unmatched_count", "match_rate"
    )}, ensure_ascii=False))
    for item in unmatched:
        print("UNMATCHED", item["product_id"], item["brand"], item["model"], item["title"])


if __name__ == "__main__":
    main()
