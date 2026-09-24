#!/usr/bin/env python3
"""Minimal live acceptance check; it cannot invoke a DouDian write method."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Mapping

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway


def _product_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("data", "list", "products", "product_list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _product_id(item: Mapping[str, Any]) -> str | None:
    for key in ("product_id", "product_id_str", "id"):
        value = str(item.get(key) or "").strip()
        if value.isdigit():
            return value
    return None


def _first_sku_id(value: Any) -> str | None:
    if isinstance(value, dict):
        candidate = str(value.get("sku_id") or "").strip()
        if candidate.isdigit():
            return candidate
        for nested in value.values():
            found = _first_sku_id(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _first_sku_id(nested)
            if found:
                return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="DouDian read-only API acceptance check")
    parser.add_argument("shop_id", help="numeric DouDian shop id")
    parser.add_argument("--env", default="env.local", help="credential env file")
    parser.add_argument("--detail", action="store_true", help="also read the first product detail")
    args = parser.parse_args()

    gateway = DoudianReadOnlyGateway(Path(args.env), args.shop_id)
    reader = DoudianProductReader(gateway)
    listing = reader.list_products(page=1, size=1)
    rows = _product_rows(listing)
    print(f"LIST_OK shop={args.shop_id} returned={len(rows)}")
    if args.detail and rows:
        product_id = _product_id(rows[0])
        if not product_id:
            print("DETAIL_SKIP reason=no_numeric_product_id")
        else:
            detail = reader.get_product_detail(product_id, show_draft=True)
            print(f"DETAIL_OK shop={args.shop_id} product_id={product_id} fields={len(detail)}")
            sku_id = _first_sku_id(detail)
            if not sku_id:
                print("SKU_SKIP reason=no_sku_id_in_product_detail")
            else:
                sku = reader.get_sku_detail(sku_id=sku_id)
                print(f"SKU_OK shop={args.shop_id} sku_id={sku_id} fields={len(sku)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
