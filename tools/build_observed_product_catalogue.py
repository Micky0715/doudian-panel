#!/usr/bin/env python3
"""Build a diagnostic-only phone model catalogue from observed shop products."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


PHONE_CATEGORIES = {"手机", "二手手机"}


def build(source: Path) -> dict:
    grouped = defaultdict(list)
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            model = str(row.get("model") or "").strip()
            brand = str(row.get("brand") or "").strip()
            if not model or row.get("category") not in PHONE_CATEGORIES:
                continue
            grouped[model.casefold()].append((model, brand, row))

    catalogue = []
    rejected_conflicts = []
    for entries in grouped.values():
        brands = {brand.casefold(): brand for _, brand, _ in entries if brand}
        if len(brands) != 1:
            rejected_conflicts.append({
                "model": entries[0][0],
                "brands": sorted({brand for _, brand, _ in entries if brand}),
            })
            continue
        shops = {row.get("shop_id") for _, _, row in entries if row.get("shop_id")}
        product_count = sum(int(row.get("product_count") or 0) for _, _, row in entries)
        catalogue.append({
            "brand": next(iter(brands.values())),
            "model": entries[0][0],
            "rows": product_count,
            "shop_count": len(shops),
            "source": "observed-products",
            "diagnostic_only": True,
        })

    catalogue.sort(key=lambda row: (row["brand"].casefold(), row["model"].casefold()))
    return {
        "metadata": {
            "source": str(source),
            "diagnostic_only": True,
            "warning": "Observed product data is not a platform property enum and must not drive automatic selection.",
            "models": len(catalogue),
            "rejected_brand_conflicts": len(rejected_conflicts),
        },
        "catalogue": catalogue,
        "rejected_brand_conflicts": rejected_conflicts,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    payload = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["metadata"], ensure_ascii=False))


if __name__ == "__main__":
    main()
